"""Telling the other sales channels that GunBroker has taken a gun — and asking.

Two hooks, pointing in opposite directions, and both exist so this package can
talk to ffl_woo_sync without importing it (and vice versa — the rule is
symmetric and enforced by a test).

* ``serial_channel_exit`` — we CALL it. GunBroker took a gun; every other
  storefront has to stop offering it, now.
* ``serial_channel_return`` — we CALL it. GunBroker let go of a gun it held
  (order cancelled, unit dropped or reassigned); the storefronts that took it
  down for us put it back on sale.
* ``channel_presale_probe`` — ffl_core calls it, and we REGISTER for it. A
  cashier is about to ring a gun up; does GunBroker already have a buyer?

The asymmetry in how failure is handled is deliberate and is the important
thing in this module.

Firing ``serial_channel_exit`` must never take down the caller. It runs inside
the transaction that records a GunBroker order, and that record is the more
important of the two facts: a delist that did not happen is caught by the
hourly sweep and by reconcile, whereas an order that failed to save is a sale
nobody knows about. So registrant exceptions are logged and swallowed — the
same choice ``woo_order_cancelled`` makes, and deliberately NOT the choice
``ffl_acquisition_sync_push`` makes (that one rolls the caller back, because an
acquisition missing from the bound book is a federal problem).

The pre-sale probe fails open for a different reason: it is advice on a
scan-to-add path. A GunBroker outage must not stop the counter from selling
guns. The refusal that does NOT fail open is the one on ``before_submit`` in
:mod:`ffl_integrations.gunbroker.doc_events`, which is where an actual sale is
blocked.
"""
from __future__ import annotations

import frappe
from frappe import _

from ffl_integrations.notifications import safe_log_error

EXIT_HOOK = "serial_channel_exit"
RETURN_HOOK = "serial_channel_return"

# What GunBroker did with the gun, as the other channels are told about it.
REASON_SOLD = "sold"          # a buyer paid
REASON_RESERVED = "reserved"  # very probably sold; the order is not in yet
REASON_ENDED = "ended"        # the listing was withdrawn without a sale
REASON_BID = "bid"            # an auction has a bid: promised to the high bidder
REASON_RELEASED = "released"  # GunBroker let go of the gun (serial_channel_return)


def fire_serial_channel_exit(serial_no: str, *, channel: str = "gunbroker",
		reason: str = REASON_SOLD) -> tuple[bool, str]:
	"""Tell every registered channel that this gun has left through GunBroker.

	Args:
	    serial_no: The Serial No GunBroker has taken.
	    channel: Which channel took it. Always "gunbroker" from this package;
	        the parameter exists so the contract reads the same from both ends.
	    reason: One of REASON_SOLD / REASON_RESERVED / REASON_ENDED.

	Returns:
	    ``(ok, error)``. A site with no registrant — ffl_woo_sync not installed,
	    which is a legitimate deployment — is a RECORDED SKIP, not a failure:
	    ``(False, "no serial_channel_exit registrant")``. Nothing raises out of
	    here under any circumstance.
	"""
	return _fire(EXIT_HOOK, serial_no, channel=channel, reason=reason)


def fire_serial_channel_return(serial_no: str, *, channel: str = "gunbroker") -> tuple[bool, str]:
	"""Tell every registered channel that GunBroker no longer holds this gun.

	The undo of :func:`fire_serial_channel_exit`: the gun is back on the shelf
	and the storefronts that delisted it for us may sell it again. Same
	contract and the same never-raise rule, for the same reason — it runs inside
	the transaction that releases the hold.

	Args:
	    serial_no: The Serial No GunBroker released.
	    channel: Which channel released it. Always "gunbroker" from this package.

	Returns:
	    ``(ok, error)`` exactly as :func:`fire_serial_channel_exit`.
	"""
	return _fire(RETURN_HOOK, serial_no, channel=channel, reason=REASON_RELEASED)


def _fire(hook: str, serial_no: str, *, channel: str, reason: str) -> tuple[bool, str]:
	registrants = frappe.get_hooks(hook)
	if not registrants:
		return False, f"no {hook} registrant (ffl_woo_sync not installed)"
	errors: list[str] = []
	for path in registrants:
		try:
			frappe.get_attr(path)(serial_no, channel=channel, reason=reason)
		except Exception as exc:  # noqa: BLE001 — see the module docstring
			errors.append(f"{path}: {type(exc).__name__}: {exc}"[:500])
			safe_log_error(
				f"{hook} failed for {serial_no} ({channel}/{reason})",
				frappe.get_traceback())
	if errors:
		return False, "; ".join(errors)[:2000]
	return True, ""


# ---------------------------------------------------------------------------
# channel_presale_probe registrant (soft layer)
# ---------------------------------------------------------------------------

def presale_probe(serial_no: str, *, timeout: int = 3) -> dict | None:
	"""Does GunBroker already have a buyer for this gun? Advisory answer.

	Registered on ffl_core's ``channel_presale_probe`` so the Create Order
	scan-to-add path can warn a cashier BEFORE the sale is built, instead of
	letting them get all the way to submit and be refused by the hard gate.

	Args:
	    serial_no: The Serial No being scanned.
	    timeout: Seconds available. Passed through to the probe's own budget.

	Returns:
	    ``{"blocked": True, "channel": "gunbroker", "message": …}`` when
	    GunBroker says the listing has a buyer, else None.

	Two things it will NOT do, both because a scan is per-gun and a ticket can
	hold several:

	* Send an HTTP request when there is no cached access token. A login plus a
	  read would eat the entire budget on its own; instead the answer is None
	  and a login is warmed up in the background for the next scan.
	* Send a second request for a gun already probed within the cache window —
	  the hard gate and this share one cache, keyed on the item number.

	Anything that goes wrong — no listing, a timeout, a 403, a GunBroker
	outage — is None. Advice that cannot be obtained is not an accusation.
	"""
	from ffl_integrations.gunbroker import doc_events

	verdict = doc_events.probe_gb_sale(serial_no, timeout=timeout)
	if verdict is None:
		# No listing of its own: a gun of a MODEL listing is not on GunBroker as
		# itself, but the model's count may already be spent there.
		return _model_presale(serial_no, timeout=timeout)
	if verdict.get("state") not in ("sold", doc_events.STATE_BID_LOCKED):
		return None
	if verdict.get("state") == doc_events.STATE_BID_LOCKED:
		message = _(
			"Serial {0} has bids in a GunBroker auction (item {1}) — it is promised "
			"to the high bidder until the auction ends. Do not sell it at the counter."
		).format(serial_no, verdict.get("item_id") or "?")
	else:
		message = _(
			"Serial {0} already has a buyer on GunBroker (item {1}) — do not sell "
			"it at the counter."
		).format(serial_no, verdict.get("item_id") or "?")
	return {"blocked": True, "channel": "gunbroker", "message": message}


def _model_presale(serial_no: str, *, timeout: int) -> dict | None:
	"""The advisory for a gun that belongs to a live MODEL listing: has GunBroker
	sold units of the model that no order has brought in yet, enough that this gun
	is already spoken for? The same question and the same probe as the hard gate on
	``before_submit`` (``doc_events.model_sale_verdict``) — only the unreadable case
	differs: here it is simply no advice."""
	from ffl_integrations.gunbroker import doc_events

	if doc_events._skip():
		return None
	item_code = frappe.db.get_value("Serial No", serial_no, "item_code")
	if not item_code:
		return None
	verdict = doc_events.model_sale_verdict(item_code, selling=1, budget=float(timeout))
	if not verdict or verdict.get("state") != "refuse":
		return None
	return {"blocked": True, "channel": "gunbroker", "message": verdict["message"]}


# ---------------------------------------------------------------------------
# channel_commitments_changed registrant
# ---------------------------------------------------------------------------

def channel_commitments_changed(item_code: str, *, source: str) -> None:
	"""Another channel (or this one) sold or released units of this Item that have
	not shipped, so the quantity GunBroker should show has moved.

	Registered on ffl_core's ``channel_commitments_changed``. A listed
	item-level Item — or a uniform-price gun's MODEL listing, whose quantity is the
	pool these commitments are subtracted from — has its quantity queued (and a
	sold-out one is relisted when units come back); everything else costs one read
	and nothing else. Runs inside
	the transaction that recorded the sale, so it only ENQUEUES, after commit —
	the pushes themselves read the shelf when they run, and a registrant that
	cannot answer makes them skip rather than publish a larger number.

	Not skipped for ``source="gunbroker"``: this channel's own order is already
	reflected on GunBroker when it is placed, but a CANCELLED one returns its
	units to the pool and GunBroker does not put them back by itself. The push
	compares against what was last sent, so the redundant case is a no-op.

	A gun priced one by one is left alone: it lists by serial, and its exits come from
	the serial events.
	"""
	from ffl_integrations.gunbroker import doc_events

	if doc_events._skip():
		return
	doc_events.sync_item_level_listing(item_code)
