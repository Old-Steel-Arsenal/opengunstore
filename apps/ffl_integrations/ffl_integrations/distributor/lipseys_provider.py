"""Lipsey's provider, bound to ``lipseys/api_client.py``. Catalog reads go
through the shared Data Service under distributor code ``lipseys``
(RemoteCatalogMixin); this module is the ordering half.

Three order types, by lane:

* **Stocking / Procure To Store** → ``Order/APIOrder`` on the dealer login. The
  order ships to the address on that Lipsey's account, so it carries none. Lipsey's
  adds the items to an open order of the same kind (long gun / handgun) when one
  exists, so several Distributor Orders can share one Lipsey's order number:
  ``web_ref`` holds the order number(s) and every later match goes by PO first.
* **Dropship Accessory** → ``Order/DropShip`` on the drop-ship login, to the
  buyer's address.
* **Dropship Firearm** → ``Order/DropShipFirearm`` on the drop-ship login, to the
  transfer dealer's premises on file with the ATF for the 15-character FFL number.

The drop-ship lanes exist only while Lipseys Settings has the drop-ship login
filled in (``capabilities``). Lipsey's has no order-status endpoint: the status
of an order is worked out here from the placement answer (per-line fill and
price) and the shipping rows ``Shipping/OneDay`` returns per invoice day, which
``list_invoices`` reads once per settled day into the Distributor Invoice mirror
and ``get_order_status`` reads back.

Placing an order is never retried when its outcome is unknown: Lipsey's
documents no way to look an order up by PO number, so a placement without an
answer goes to Error for a person to check at Lipsey's before anything is
re-sent.

``plan_fulfillment`` (the base template), ``normalize_placement``,
``derive_status``, the order bodies and the shipping-day helpers are pure.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from ffl_integrations.distributor import lanes
from ffl_integrations.distributor.base import DistributorProvider
from ffl_integrations.distributor.remote_catalog_mixin import RemoteCatalogMixin
from ffl_integrations.lipseys.api_client import (
	DEALER, EP_API_ORDER, EP_DROPSHIP, EP_DROPSHIP_FIREARM, FULFILLMENT, SETTINGS_DOCTYPE,
	LipseysConfigError, LipseysError, LipseysPlacementUnknown, errors_of, login_configured,
)

DISTRIBUTOR_CODE = "lipseys"

STORE_CAPABILITIES = frozenset({"search", "catalog", "check_availability", "place_order",
	"to_store"})
DROPSHIP_CAPABILITIES = frozenset({"dropship", "fds"})

# lazy: Lipsey's documents no per-order line cap; 100 keeps one order one quick
# call (a slow answer is an unknown outcome). Raise it once a large order is
# proven on the live API.
MAX_ORDER_LINES = 100

# Lipsey's processes a firearm drop-ship to California only after the dealer's
# California DOJ number is emailed to them with the order reference.
# lazy: not offered at all; the manual path is a store order (or Lipsey's
# website) plus that email. Upgrade path: an FDS step that emails the DOJ number.
FIREARM_DROPSHIP_BLOCKED_STATES = ("CA",)

# Line item_status values (tasks.KILLED_ITEM_STATUSES reads CANCELLED as killed).
STATUS_CANCELLED = "CANCELLED"
STATUS_SHORT = "SHORT"
# A line the answer does not name although Lipsey's answered for an order: it was
# most likely taken under another spelling of the item number, so it counts as
# ordered (never killed while the order may ship it) and a person is told to check.
STATUS_UNCONFIRMED = "UNCONFIRMED"

HOLD_ACCOUNT = "Account hold"

# OneDay answers per invoice day and is meant to be asked once a day, for a day
# that is over. A day counts as settled this many hours after midnight (site
# time), so invoices posted overnight are in before it is read.
# lazy: a fixed settle hour; an invoice Lipsey's posts later than that for an
# earlier day is never read. Upgrade path: re-read the last day once more.
SETTLE_HOUR = 6
MAX_CATCHUP_DAYS = 7
# One cursor per login: a login that keeps failing never stops the other's tracking.
CURSOR_FIELDS = {DEALER: "shipping_polled_through", FULFILLMENT: "dropship_shipping_polled_through"}


class LipseysProvider(RemoteCatalogMixin, DistributorProvider):
	code = DISTRIBUTOR_CODE
	display_name = "Lipsey's"
	settings_doctype = SETTINGS_DOCTYPE
	max_order_lines = MAX_ORDER_LINES
	# OneDay rows carry our PO number: an invoice no POS order matches really
	# was ordered outside the POS.
	invoices_carry_po = True

	def __init__(self, client_factory=None, settings=None):
		# Injectable so tests never build a real client or read the Single.
		self._client_factory = client_factory
		self._settings = settings
		self._clients: dict = {}

	def _get_settings(self):
		if self._settings is not None:
			return self._settings
		import frappe
		return frappe.get_cached_doc(self.settings_doctype)

	def _client(self, account: str):
		if account not in self._clients:
			if self._client_factory is not None:
				self._clients[account] = self._client_factory(account)
			else:
				from ffl_integrations.lipseys.api_client import LipseysClient
				self._clients[account] = LipseysClient.from_settings(account)
		return self._clients[account]

	def capabilities(self) -> set:
		caps = set(STORE_CAPABILITIES)
		if login_configured(self._get_settings(), FULFILLMENT):
			caps |= DROPSHIP_CAPABILITIES
		return caps

	# catalog reads: RemoteCatalogMixin

	def promote_to_item(self, sku: str, opts=None) -> dict:
		from ffl_integrations.lipseys.promote import promote_to_item
		return promote_to_item(sku, opts or {})

	def blocked_states(self, sku: str) -> list:
		"""States a firearm drop-ship of this item cannot go to (California: see
		FIREARM_DROPSHIP_BLOCKED_STATES); nothing for an item that is no firearm."""
		from ffl_integrations.distributor.util import legal_firearm

		row = self.catalog_row(sku)
		return list(FIREARM_DROPSHIP_BLOCKED_STATES) if row and legal_firearm(row, self.code) else []

	def check_availability(self, lines: list) -> list:
		"""ValidateItem per line on the dealer login: live qty, price, blocked,
		allocated. Falls back to the Data Service catalog row (the Sports South
		shape) while Allow live orders is off, the dealer login is empty, or
		Lipsey's cannot be reached — one failure switches the rest of the cart to
		the catalog rather than failing every line in turn."""
		settings = self._get_settings()
		live = bool(settings.get("ordering_enabled")) and login_configured(settings, DEALER)
		out = []
		for ln in lines:
			sku = str(ln.get("sku") or "").strip()
			if not sku:
				continue
			wished = int(ln.get("qty") or 0)
			if live:
				try:
					out.append(availability_row(sku, wished, self._client(DEALER).validate_item(sku)))
					continue
				except Exception:  # noqa: BLE001 — degrade to the catalog, logged
					_log_error(f"Lipsey's ValidateItem failed ({sku}); using the catalog")
					live = False
			row = self.catalog_row(sku) or {}
			qty = int(row.get("qty") or 0)
			out.append({"sku": sku, "qty": qty, "cost": float(row.get("cost") or 0),
				"available": bool(row) and not row.get("discontinued") and qty >= wished and qty > 0,
				"orderable": bool(row) and not row.get("discontinued")})
		return out

	# ---------------------------------------------------------- ordering

	def place_order(self, do_name: str) -> dict:
		import frappe
		do = frappe.get_doc("Distributor Order", do_name)
		return self._place(do)

	def _place(self, do) -> dict:
		settings = self._get_settings()
		if not settings.get("ordering_enabled"):
			raise LipseysConfigError("Lipseys Settings: Allow live orders is off.")
		if (do.web_ref or "").strip():
			# Placed before: Lipsey's has no lookup by PO, so never send it twice.
			raise LipseysError(f"{do.name} already carries Lipsey's order {do.web_ref} — "
				f"it was placed before and is not sent again.", terminal=True)
		lines = [_line(ln) for ln in (do.lines or [])]
		if not lines:
			raise LipseysError(f"{do.name}: no lines to place.", terminal=True)
		mode = lanes.mode_of(do.order_type) or lanes.DEFAULT_MODE
		account = lanes.lane(mode).account
		if account == FULFILLMENT and not login_configured(settings, FULFILLMENT):
			raise LipseysConfigError("Lipseys Settings: the drop-ship login is not filled in, "
				"so drop-ship orders cannot be placed.")
		endpoint, body = order_request(do, mode, lines, settings)
		try:
			env = self._client(account).place(endpoint, body)
			result = normalize_placement(env, lines, consumer_order=endpoint != EP_API_ORDER)
		except LipseysPlacementUnknown as exc:
			raise _unknown_outcome(do.name, exc) from exc
		except LipseysError:
			raise   # refused before or by Lipsey's: nothing was placed
		except Exception as exc:  # noqa: BLE001 — an answer we cannot read is no answer
			raise _unknown_outcome(do.name, exc) from exc
		result["raw"] = {"account": account, "endpoint": endpoint, "request": body, "response": env}
		_alert_placement(do.name, result)
		return result

	def get_order_status(self, do_name: str) -> dict:
		"""No status call exists at Lipsey's: the placement's per-line answer, plus
		every mirrored invoice that bills this order (its PO, or its Lipsey's order
		number on an invoice dated on or after the order was placed)."""
		import frappe

		do = frappe.get_doc("Distributor Order", do_name)
		invoices = invoices_for_order(do)
		return derive_status([_status_line(l) for l in do.lines], do.web_ref or "",
			do.hold_status or "", invoices)

	# ---------------------------------------------------------- invoices / shipping

	def list_invoices(self, since: str, known=()) -> list:
		"""The shipping rows of every settled day not read yet (OneDay, each login
		on its own), one invoice each, for ``distributor.invoices``. ``since`` is not
		used: OneDay answers per day and is asked once per day, so a cursor per login
		on Lipseys Settings (*Shipping polled through*) remembers the last day read;
		a gap is caught up for at most MAX_CATCHUP_DAYS. A failed day stops that
		login's run (the other login still reads) and is asked again next run. Lines
		come from the matched order's placement answer (Lipsey's sends no invoice
		lines); an invoice no POS order matches has none."""
		import frappe
		from frappe.utils import getdate, now_datetime

		settings = self._get_settings()
		if not settings.get("ordering_enabled"):
			return []
		accounts = [a for a in (DEALER, FULFILLMENT) if login_configured(settings, a)]
		if not accounts:
			return []
		now = now_datetime()
		out, cursors, errors = [], {}, []
		for account in accounts:
			field = CURSOR_FIELDS[account]
			# The DB, not the cached document: this run's cursor must be what the last
			# run wrote.
			polled = frappe.db.get_single_value(SETTINGS_DOCTYPE, field)
			for day in due_days(getdate(polled) if polled else None, now):
				try:
					rows = [(account, r) for r in self._client(account).one_day(day)]
				except Exception as exc:  # noqa: BLE001 — retried next run
					_log_error(f"Lipsey's OneDay {day.isoformat()} failed ({account} login)")
					errors.append(f"{account} login, {day.isoformat()}: {_mask(exc)}")
					break
				out.extend(group_shipments(rows, day))
				cursors[field] = day
		known = set(known or ())
		# Everything that can fail is done before a cursor moves: the caller commits
		# the cursors together with the invoices it inserts from this answer.
		# lazy: an invoice the caller then fails to insert (logged per row) is not read
		# again. Upgrade path: a "re-read this day" action that rewinds the cursor.
		invoices = [with_order_lines(inv) for inv in out if inv["invoice_number"] not in known]
		values = dict(cursors)
		if cursors:
			values["last_shipping_poll_at"] = now
		if errors:
			values["last_error"] = "; ".join(errors)
		if values:
			frappe.db.set_single_value(SETTINGS_DOCTYPE, values)
		return invoices

	# ---------------------------------------------------------- freight

	def estimate_order_freight(self, lines: list, mode: str, options=None) -> Optional[float]:
		return estimate_freight(lines, mode, self._get_settings())

	# ---------------------------------------------------------- hub / account

	def account_number(self, account: str) -> str:
		s = self._get_settings()
		if account == FULFILLMENT:
			return (s.get("dropship_email") or "").strip()
		return (s.get("customer_number") or "").strip() or (s.get("dealer_email") or "").strip()

	def hub_summary(self) -> dict:
		return {"live_orders": bool(self._get_settings().get("ordering_enabled"))}

	def connection_test(self) -> dict:
		"""A Local-catalog site also proves the catalog feed on the dealer login."""
		from ffl_integrations.catalog_local import is_local
		from ffl_integrations.lipseys.api_client import probe

		checks = [self.catalog_health_check()]
		if is_local():
			checks.append(self.feed_login_check())
		s = self._get_settings()
		if not s.get("ordering_enabled"):
			checks.append({"label": "Ordering API", "ok": None,
				"detail": "Allow live orders is off in Lipseys Settings."})
			return {"checks": checks}
		checks.append(probe(self._client(DEALER)))
		if login_configured(s, FULFILLMENT):
			checks.append(probe(self._client(FULFILLMENT)))
		else:
			checks.append({"label": "Ordering API (drop-ship login)", "ok": None,
				"detail": "Not filled in: drop-ship orders are unavailable."})
		return {"checks": checks}

	@staticmethod
	def feed_login_check() -> dict:
		"""The local catalog's feed: log in with the dealer login and read the pricing
		feed (a few MB; it proves the allowlist on a data call). The detail never
		carries a credential."""
		from ffl_integrations.catalog_local.drivers import lipseys as feed

		label = "Catalog feed (dealer login)"
		try:
			if feed.credentials() is None:
				return {"label": label, "ok": False, "detail": "The catalog is Local, but Lipseys "
					"Settings has no dealer email and password."}
			data = feed.fetch(feed.PRICING, expect=dict, max_bytes=feed.PRICING_MAX_BYTES,
				deadline_s=feed.PRICING_DEADLINE_S)
		except Exception as exc:  # noqa: BLE001 — one leg reports, the others still run
			return {"label": label, "ok": False, "detail": str(exc)}
		items = data.get("items")
		return {"label": label, "ok": True,
			"detail": f"Signed in; {len(items) if isinstance(items, list) else 0} items priced."}


def estimate_freight(lines: list, mode: str, settings) -> Optional[float]:
	"""Lipsey's freight for ONE order: the Firearms Dropship Program's flat rate per
	firearm order (adult signature included) or the accessory drop-ship rate per
	order. Orders shipped to the shop, and accessory orders while they ship
	overnight (billed at overnight rates), are not estimated (None). Pure over
	``settings``."""
	from ffl_integrations.distributor.freight import flat_rate
	from ffl_integrations.distributor.util import setting_value

	if mode == lanes.DIRECT and settings.get("dropship_overnight"):
		return None
	return flat_rate(lines, mode, consumer={
		"firearm": setting_value(settings, "rate_dropship_firearm", 9.95),
		"accessory": setting_value(settings, "rate_dropship_accessory", 8.00)})


def get_provider() -> LipseysProvider:
	return LipseysProvider()


# ------------------------------------------------------------- helpers

def _log_error(title: str) -> None:
	import frappe
	frappe.log_error(title=title, message=frappe.get_traceback())


def _mask(value) -> str:
	"""Lipsey's error text lands in a Settings field: no email address in it."""
	return re.sub(r"\S+@\S+", "<email>", str(value))


def _unknown_outcome(po: str, exc: Exception) -> LipseysPlacementUnknown:
	return LipseysPlacementUnknown(
		f"{po}: no usable answer from Lipsey's ({exc}) — the order may or may not have been "
		f"placed. Check Lipsey's for PO {po} (the order confirmation email, or your Lipsey's "
		f"account) before placing it again; it is never re-sent automatically.",
		terminal=True, maybe_sent=True)


def _get(ln, key):
	return ln.get(key) if isinstance(ln, dict) else getattr(ln, key, None)


def _line(ln) -> dict:
	return {
		"sku": str(_get(ln, "sku") or "").strip(),
		"item_code": _get(ln, "item_code") or "",
		"description": _get(ln, "description") or "",
		"wish_qty": int(_get(ln, "wish_qty") or 0),
		"unit_cost": float(_get(ln, "unit_cost") or 0),
	}


def _status_line(ln) -> dict:
	return {"sku": _get(ln, "sku") or "", "ack_qty": int(_get(ln, "ack_qty") or 0),
		"item_status": _get(ln, "item_status") or ""}


def _key(value) -> str:
	return str(value or "").strip().upper()


def availability_row(sku: str, wished: int, data: Optional[dict]) -> dict:
	"""ValidateItem ``data`` → the availability shape. None (Lipsey's does not
	know the item) or ``blocked`` = not orderable; ``allocated`` = orderable only
	on an allocation, so not available."""
	if not data:
		return {"sku": sku, "qty": 0, "cost": 0.0, "available": False, "orderable": False}
	qty = int(data.get("qty") or 0)
	orderable = not data.get("blocked")
	return {"sku": sku, "qty": qty, "cost": float(data.get("price") or 0),
		"available": orderable and not data.get("allocated") and qty >= wished and qty > 0,
		"orderable": orderable, "allocated": bool(data.get("allocated")),
		"can_dropship": bool(data.get("canDropship"))}


# ------------------------------------------------------------- order bodies (pure)

def order_items(lines: list) -> list:
	return [{"ItemNo": ln["sku"], "Quantity": int(ln["wish_qty"])} for ln in lines]


def ffl_number(license_no) -> str:
	"""The transfer dealer's licence as Lipsey's wants it: its 15 characters with
	no dashes (``1-23-456-78-9A-12345`` → ``123456789A12345``)."""
	return re.sub(r"[^0-9A-Za-z]", "", str(license_no or "")).upper()


def _state_code(raw) -> str:
	from ffl_core.compliance.interstate_tax import normalize_us_state

	value = str(raw or "").strip()
	return normalize_us_state(value) or (value.upper() if len(value) == 2 else "")


def _refuse(do, message: str):
	raise LipseysError(f"{do.name}: {message}", terminal=True)


def order_request(do, mode: str, lines: list, settings) -> tuple:
	"""``(endpoint, body)`` for this order's lane. Refuses, before anything is
	sent, an order Lipsey's would have to reject or could misdeliver: a buyer
	address without street, city, two-letter state or 5-digit ZIP; a firearm
	drop-ship without a 15-character FFL, the buyer's name and phone, or to a
	blocked state."""
	get = lambda k: str(getattr(do, k, "") or "").strip()  # noqa: E731
	disable_email = bool(settings.get("disable_email"))
	items = order_items(lines)
	if mode == lanes.DIRECT:
		state, zip5 = _state_code(get("ship_address_state")), re.sub(r"\D", "", get("ship_address_zip"))[:5]
		address = {"Name": get("consumer_name"), "AddressLine1": get("ship_address_line1"),
			"AddressLine2": get("ship_address_line2"), "AddressCity": get("ship_address_city"),
			"AddressState": state, "AddressZip": zip5}
		if not (address["Name"] and address["AddressLine1"] and address["AddressCity"]
				and len(state) == 2 and len(zip5) == 5):
			_refuse(do, "the buyer's name and address (street, city, two-letter state, "
				"5-digit ZIP) are incomplete — Lipsey's needs all of them for a drop-ship.")
		# The packing slip prints the billing address; the order holds only the
		# buyer's ship-to, which is what a buyer expects to see there.
		body = {"PoNumber": do.name}
		for prefix in ("Billing", "Shipping"):
			body.update({f"{prefix}{k}": v for k, v in address.items()})
		body.update({"DisableEmail": disable_email, "Items": items,
			"Overnight": bool(settings.get("dropship_overnight"))})
		return EP_DROPSHIP, body
	if mode == lanes.FDS:
		ffl = ffl_number(get("transfer_ffl_license"))
		if len(ffl) != 15:
			_refuse(do, f"the transfer FFL {get('transfer_ffl_license')!r} is not a 15-character "
				f"licence number.")
		state = _state_code(get("transfer_ffl_premise_state"))
		if state in FIREARM_DROPSHIP_BLOCKED_STATES:
			_refuse(do, f"Lipsey's firearm drop-ship to {state} needs the dealer's DOJ number "
				f"by email; order it to the store instead.")
		phone = re.sub(r"\D", "", get("contact_num"))
		if not get("consumer_name") or len(phone) < 10:
			_refuse(do, "Lipsey's needs the buyer's name and phone number on a firearm drop-ship.")
		return EP_DROPSHIP_FIREARM, {"Ffl": ffl, "Po": do.name, "Name": get("consumer_name"),
			"Phone": phone, "DelayShipping": False, "DisableEmail": disable_email, "Items": items}
	return EP_API_ORDER, {"PONumber": do.name, "DisableEmail": disable_email, "Items": items}


# ------------------------------------------------------------- placement answer (pure)

def _money(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def normalize_placement(env: dict, lines: list, *, consumer_order: bool) -> dict:
	"""Lipsey's order answer → the generic place-order shape.

	Per line, ``fulfilledQuantity`` is the acknowledged quantity and ``price`` the
	unit cost; a line filled 0, refused (``orderError``) or missing from the
	answer is CANCELLED, a partial fill SHORT — except a line the answer does not
	name while Lipsey's did answer for an order, which counts as ordered at the
	wished quantity, UNCONFIRMED, for a person to check. APIOrder answers a list of lines
	(each naming the order it joined); the drop-ship calls answer one order with
	``lineItems``, ``accountHold`` and the freight.

	Raises ``LipseysError`` when the answer shows no order at all (refused
	outright, or every line refused with no order number), and
	``LipseysPlacementUnknown`` when a successful answer names no line at all —
	nothing to tell whether an order exists.
	"""
	env = env or {}
	data = env.get("data")
	if consumer_order:
		data = data if isinstance(data, dict) else {}
		results = [r for r in (data.get("lineItems") or []) if isinstance(r, dict)]
		order_numbers = [str(data.get("orderNumber") or "").strip()]
	else:
		results = [r for r in (data if isinstance(data, list) else []) if isinstance(r, dict)]
		order_numbers = [str(r.get("orderNumber") or "").strip() for r in results]
	order_numbers = list(dict.fromkeys(n for n in order_numbers if n))
	if not results and not order_numbers and env.get("success"):
		raise LipseysPlacementUnknown("Lipsey's answered success without any order line",
			terminal=True, maybe_sent=True)
	by_sku: dict = {}
	for r in results:
		for key in (r.get("itemNumber"), r.get("lipseysItemNumber")):
			if _key(key):
				by_sku.setdefault(_key(key), r)
	ack, notes, subtotal = [], [], 0.0
	for ln in lines:
		r = by_sku.get(_key(ln["sku"]))
		if r is None and order_numbers:
			ack.append({"sku": ln["sku"], "ack_qty": ln["wish_qty"], "unit_cost": ln["unit_cost"],
				"item_status": STATUS_UNCONFIRMED, "fully_filled": "Y"})
			subtotal += ln["wish_qty"] * ln["unit_cost"]
			notes.append(f"{ln['sku']}: not named in Lipsey's answer — counted as ordered; "
				f"check Lipsey's order {', '.join(order_numbers)}")
			continue
		if r is None:
			ack.append({"sku": ln["sku"], "ack_qty": 0, "unit_cost": ln["unit_cost"],
				"item_status": STATUS_CANCELLED, "fully_filled": "N"})
			notes.append(f"{ln['sku']}: not in Lipsey's answer")
			continue
		filled = 0 if r.get("orderError") else max(0, int(r.get("fulfilledQuantity") or 0))
		cost = _money(r.get("price")) or ln["unit_cost"]
		if filled == 0:
			status = STATUS_CANCELLED
		elif filled < ln["wish_qty"]:
			status = STATUS_SHORT
		else:
			status = ""
		line_errors = "; ".join(str(e) for e in (r.get("errors") or []) if str(e).strip())
		if status or line_errors:
			notes.append(f"{ln['sku']}: filled {filled} of {ln['wish_qty']}"
				+ (f" — {line_errors}" if line_errors else ""))
		subtotal += filled * cost
		ack.append({"sku": ln["sku"], "ack_qty": filled, "unit_cost": cost,
			"item_status": status, "fully_filled": "Y" if filled >= ln["wish_qty"] else "N"})
	if not order_numbers and not any(a["ack_qty"] for a in ack):
		reason = errors_of(env) or "; ".join(notes) or "no order in the answer"
		raise LipseysError(f"Lipsey's placed nothing: {reason}", terminal=True)
	freight = round(_money(data.get("freight")), 2) if consumer_order else 0.0
	total = _money(data.get("total")) if consumer_order else 0.0
	return {
		"po_num": "", "web_ref": ",".join(order_numbers), "queued": False,
		"hold_status": HOLD_ACCOUNT if consumer_order and data.get("accountHold") else "",
		"ack_lines": ack,
		"totals": {"subtotal": round(subtotal, 2), "total": round(total or subtotal + freight, 2)},
		"freight": freight,
		"notes": notes + ([f"Lipsey's: {errors_of(env)}"] if errors_of(env) else [])
			+ [f"changed: {c}" for c in ((data.get("lineItemChanges") or []) if consumer_order else [])],
	}


def _alert_placement(do_name: str, result: dict) -> None:
	"""Anything short of a clean fill reaches a person: refused, short or missing
	lines, an account hold, Lipsey's own messages."""
	if not (result.get("notes") or result.get("hold_status")):
		return
	from ffl_integrations.notifications import notify_admins

	body = "\n".join(f"  {n}" for n in result.get("notes") or [])
	if result.get("hold_status"):
		body += f"\n  Lipsey's put the order on {result['hold_status'].lower()}."
	notify_admins(f"Lipsey's did not fill {do_name} as ordered",
		f"{do_name} was placed with Lipsey's (order {result.get('web_ref') or '?'}):\n{body}",
		dedup_key=f"lipseys_place_{do_name}")


# ------------------------------------------------------------- status (pure)

def derive_status(do_lines: list, web_ref: str, hold_status: str, invoices: list) -> dict:
	"""The generic poll shape from what this site knows. Once any invoice bills
	the order, every line Lipsey's acknowledged reads shipped at its ack quantity,
	with the invoices' numbers and tracking; a hold ends with the first invoice.
	# lazy: OneDay is per invoice, not per line, so an order Lipsey's ships in two
	# parcels reads fully shipped at the first one; receiving scans what arrives.
	# Upgrade path: per-line invoice data, if Lipsey's ever exposes it."""
	numbers = list(dict.fromkeys(str(i.get("invoice_number") or "").strip()
		for i in invoices if str(i.get("invoice_number") or "").strip()))
	tracking = list(dict.fromkeys(t.strip() for i in invoices
		for t in str(i.get("tracking_numbers") or "").split(",") if t.strip()))
	from ffl_integrations.distributor.tasks import KILLED_ITEM_STATUSES

	lines = []
	for ln in do_lines:
		killed = str(ln.get("item_status") or "").strip().upper() in KILLED_ITEM_STATUSES
		shipped = bool(numbers) and not killed
		ack = int(ln.get("ack_qty") or 0)
		lines.append({"sku": ln["sku"], "ack_qty": ack,
			"invoiced_qty": ack if shipped else 0,
			"item_status": ln.get("item_status") or "",
			"invoices": numbers if shipped else [],
			"tracking_numbers": tracking if shipped else [],
			"serials": [], "unit_cost": None})
	return {"po_num": "", "web_ref": web_ref, "hold_status": "" if numbers else hold_status,
		"lines": lines, "freight": None, "raw": {"invoices": numbers}}


# ------------------------------------------------------------- shipping days (pure)

def due_days(polled_through: Optional[date], now: datetime) -> list:
	"""The settled days not read yet, oldest first: from the day after
	``polled_through`` (first run: only the last settled day) up to the last
	settled day, at most MAX_CATCHUP_DAYS of them."""
	last = now.date() - timedelta(days=1 if now.hour >= SETTLE_HOUR else 2)
	start = polled_through + timedelta(days=1) if polled_through else last
	start = max(start, last - timedelta(days=MAX_CATCHUP_DAYS - 1))
	out, day = [], start
	while day <= last:
		out.append(day)
		day += timedelta(days=1)
	return out


def group_shipments(rows: list, day: date) -> list:
	"""OneDay rows ``[(account, row)]`` → one invoice per invoice number (a
	parcel per row), in the ``distributor.invoices`` shape without lines."""
	by_no: dict = {}
	for account, r in rows:
		no = str(r.get("invoiceNumber") or "").strip()
		if not no:
			continue
		inv = by_no.setdefault(no, {"invoice_number": no, "invoice_date": day.isoformat(),
			"po_number": str(r.get("poNumber") or "").strip(), "order_ref": "", "account": account,
			"total": 0.0, "freight": 0.0, "fees": 0.0, "tracking_numbers": [], "lines": [],
			"_orders": []})
		order = str(r.get("orderNumber") or "").strip()
		if order and order not in inv["_orders"]:
			inv["_orders"].append(order)
		tracking = str(r.get("trackingNumber") or "").strip()
		if tracking and tracking not in inv["tracking_numbers"]:
			inv["tracking_numbers"].append(tracking)
		if not inv["po_number"]:
			inv["po_number"] = str(r.get("poNumber") or "").strip()
	out = []
	for inv in by_no.values():
		inv["order_ref"] = ",".join(inv.pop("_orders"))
		out.append(inv)
	return out


# ------------------------------------------------------------- matching (frappe)

def _tokens(csv) -> list:
	return [t.strip() for t in str(csv or "").split(",") if t.strip()]


def _lipseys_distributors() -> list:
	import frappe
	return frappe.get_all("Distributor", filters={"provider": DISTRIBUTOR_CODE}, pluck="name")


def orders_billed(po_number: str, order_ref: str, invoice_date) -> list:
	"""The Lipsey's Distributor Orders one invoice bills: the order named by its
	PO, and every order whose Lipsey's order number it names, placed on or before
	the invoice day (Lipsey's adds a later store order to an open order, so one
	invoice can bill several of ours; an order placed after the invoice cannot be
	on it)."""
	import frappe
	from frappe.utils import getdate

	houses = _lipseys_distributors()
	if not houses:
		return []
	out = []
	po = (po_number or "").strip()
	if po and frappe.db.get_value("Distributor Order", po, "distributor") in houses:
		out.append(po)
	day = getdate(invoice_date) if invoice_date else None
	for ref in _tokens(order_ref):
		for row in frappe.get_all("Distributor Order", filters={
				"distributor": ["in", houses], "web_ref": ["like", f"%{ref}%"]},
				fields=["name", "web_ref", "placed_at"]):
			if ref not in _tokens(row.web_ref) or row.name in out:
				continue
			if day and row.placed_at and getdate(row.placed_at) > day:
				continue
			out.append(row.name)
	return out


def with_order_lines(inv: dict) -> dict:
	"""The invoice with lines from the placement answer of each order it bills:
	what Lipsey's acknowledged, at the acknowledged unit cost."""
	import frappe

	lines = []
	for name in orders_billed(inv["po_number"], inv["order_ref"], inv["invoice_date"]):
		for ln in frappe.get_all("Distributor Order Line", filters={
				"parenttype": "Distributor Order", "parent": name, "ack_qty": [">", 0]},
				fields=["sku", "item_code", "description", "ack_qty", "unit_cost"],
				order_by="idx asc"):
			cost = float(ln.unit_cost or 0)
			lines.append({"sku": ln.sku, "item_code": ln.item_code, "description": ln.description,
				"qty": int(ln.ack_qty), "unit_cost": cost, "amount": round(cost * int(ln.ack_qty), 2)})
	return {**inv, "lines": lines, "total": round(sum(l["amount"] for l in lines), 2)}


def invoices_for_order(do) -> list:
	"""Mirrored invoices that bill this order: linked to it, naming it as PO, or
	naming its Lipsey's order number and dated on or after it was placed."""
	import frappe
	from frappe.utils import getdate

	dt = "Distributor Invoice"
	fields = ["name", "invoice_number", "invoice_date", "order_ref", "tracking_numbers"]
	base = {"distributor": do.distributor}
	found = {r.name: r for r in frappe.get_all(dt, filters={**base, "distributor_order": do.name},
		fields=fields)}
	found.update({r.name: r for r in frappe.get_all(dt, filters={**base, "po_number": do.name},
		fields=fields)})
	placed = getdate(do.placed_at) if do.placed_at else None
	for ref in _tokens(do.web_ref):
		for r in frappe.get_all(dt, filters={**base, "order_ref": ["like", f"%{ref}%"]}, fields=fields):
			if ref in _tokens(r.order_ref) and not (placed and r.invoice_date
					and getdate(r.invoice_date) < placed):
				found.setdefault(r.name, r)
	return sorted(found.values(), key=lambda r: (str(r.invoice_date or ""), r.invoice_number))
