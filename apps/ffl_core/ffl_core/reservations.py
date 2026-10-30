"""Reserved-serial lookups behind the Item-form 'Pending' badge.

A firearm serial stays Serial No.status='Active' from the moment it is assigned
to an order until disposition/stock-out fires. These helpers surface which
in-stock serials are already spoken for by an open counter order so the Item
form can flag them. The Woo online side lives in ffl_woo_sync (merged client-side).

It is also the one home of the pool every sales channel shares for a uniform-price
firearm or a stocked accessory (the "shared pool" section at the end): the free
guns, the units each channel has sold and not yet shipped, and the counter's
refusal. The channel apps register against two hooks instead of being imported.
"""
from __future__ import annotations

import frappe
from frappe import _

from ffl_core.custody import custody_serials
from ffl_core.firearm import _is_firearm_row, is_regulated_firearm_item
from ffl_core.serials import serials_for_line


def si_firearm_serial_state(si) -> tuple[list[dict], set[str], list[tuple]]:
	"""One pass over an invoice's serial state: (pending, disposed_serials,
	row_serials). row_serials is [(row, serials)] with serials_for_line run
	exactly ONCE per row — the v16 Bundle path costs a get_doc per call, and
	the Pending Order queue needs the same resolution for both the pending
	computation and the Ship dialog's ship_items."""
	row_serials = [(row, serials_for_line(row)) for row in si.items]
	firearm_serials = [
		(row.item_code, serial)
		for row, serials in row_serials
		if is_regulated_firearm_item(row.item_code)
		for serial in serials
	]
	disposed = set(frappe.get_all(
		"FFL Disposition",
		filters={"sales_invoice": si.name, "docstatus": 1},
		pluck="serial"))
	# A counter pickup's gun leaves on its 4473 ticket (api/counter_pickup): a
	# Retail Sale disposition links that POS Invoice, not this order. Read it
	# there, and only once the ticket is SUBMITTED — the sale is real then.
	from ffl_core.api.counter_pickup import is_pickup_order, submitted_pickup_invoices

	if is_pickup_order(si):
		tickets = submitted_pickup_invoices(si.name)
		if tickets:
			disposed |= set(frappe.get_all("FFL Disposition",
				filters={"linked_pos_invoice": ("in", tickets), "docstatus": 1,
					"disposition_type": "Retail Sale"},
				pluck="serial"))
	pending = [{"item_code": ic, "serial": s}
		for (ic, s) in firearm_serials if s not in disposed]
	return pending, disposed, row_serials


def si_pending_firearm_serials(si) -> list[dict]:
	"""Regulated firearm serials on a Sales Invoice doc that have no submitted
	FFL Disposition yet — assigned but not shipped. Shared by the Pending Order
	queue (list_pending_dispositions) and the reserved-serial badge so the two
	cannot drift. Returns [{"item_code", "serial"}]."""
	return si_firearm_serial_state(si)[0]


def _draft_pos_serials(item_code: str) -> list[tuple[str, str]]:
	"""(serial, pos_invoice_name) for `item_code` on Draft (docstatus=0, non-return)
	POS Invoices — the parked-4473 queue holding a gun. The parent-status filter
	needs the join, so we select both serial columns and let serials_for_line()
	resolve either storage model (newline serial_no OR a Serial and Batch Bundle)."""
	rows = frappe.db.sql(
		"""
		SELECT pii.serial_no AS serial_no,
		       pii.serial_and_batch_bundle AS serial_and_batch_bundle,
		       pi.name AS pos_invoice
		FROM `tabPOS Invoice Item` pii
		INNER JOIN `tabPOS Invoice` pi ON pi.name = pii.parent
		WHERE pii.item_code = %(item_code)s
		  AND pi.docstatus = 0
		  AND COALESCE(pi.is_return, 0) = 0
		  AND (COALESCE(pii.serial_no, '') != ''
		       OR COALESCE(pii.serial_and_batch_bundle, '') != '')
		ORDER BY pi.creation ASC, pi.name ASC
		""",
		{"item_code": item_code},
		as_dict=True,
	)
	# ORDER BY (aligned with the consignment provider's draft_line_holds): under a double-hold
	# anomaly (same serial parked on two Draft POS Invoices) reserved_counter_
	# serials keeps the FIRST via setdefault, so a deterministic creation order
	# makes the winning hold — and thus which invoice's picker may re-offer the
	# gun (firearm._pos_picker_excluded_serials) — stable across runs.
	out: list[tuple[str, str]] = []
	for r in rows:
		for s in serials_for_line(r):
			out.append((s, r["pos_invoice"]))
	return out


def counter_holds(item_code: str) -> dict:
	"""Counter-hold registry for one Item's in-stock serials — UNGATED.

	The ENFORCEMENT half of :func:`reserved_counter_serials`. Every in-process
	caller that uses holds to *decide* something must call this one.

	The wrapper's role check exists to stop an under-privileged HTTP caller
	reading which regulated serials sit on which order, and it answers ``{}``
	silently when it fires. For a decision that is the worst possible answer: a
	gate handed ``{}`` concludes "nothing is held" and lets the gun through, and a
	stock count handed ``{}`` over-reports the shelf. That is exactly that
	shape — `webhook._pick_oldest_unreserved_serial` had no hold rule at all, and
	`mapping.stock_for_item` was one refactor away from evaluating in the Guest
	webhook request and quietly pushing the storefront count too high.

	Holds come from an open counter order: a submitted Sales Invoice with a
	destination FFL whose serial is not yet disposed (Create Order), a Draft POS
	Invoice firearm line (parked POS sale), or a Draft Consignment Out line
	("create only — dispose later"). One registry, so a hold added here is
	enforced everywhere at once.

	Returns:
	    {serial: {source, ref, kind, tooltip, url}} — only held serials appear.
	"""
	out: dict[str, dict] = {}
	# Scope to Sales Invoices that actually carry this gun, so we don't get_doc the
	# whole open dealer-transfer backlog on every Item-form open. The item_code
	# filter on the line table mirrors api/item_admin.py's scoped lookup.
	si_item_parents = set(frappe.get_all(
		"Sales Invoice Item",
		filters={"item_code": item_code, "docstatus": 1},
		pluck="parent"))
	# Same base filter as manual_order.list_pending_dispositions
	# (channel_invoice.pending_si_base_filter): it drops the kinds of SI whose fulfilment does NOT live on the SI
	# itself, so si_pending_firearm_serials can never see their guns disposed
	# and would flag them "pending" for good — a web order's revenue SI (its
	# disposition is booked BEFORE the SI exists, fulfillment.dispose_web_order,
	# and links the Woo Online Order; the in-stock hold is the web side's
	# Serial No.woo_reserved / reserved_web_serials), an outbound-consignment
	# settlement SI (disposition on the Consignment Out line), a consolidated
	# POS invoice (the POS Invoice carried the sale), and a return.
	# GunBroker is the same shape of thing and joins the list the same way: its
	# dispositions link the GunBroker Order, so a shipped GunBroker gun would
	# read as "still on an open counter order" for ever and never be sellable
	# again. The exclusions are column-guarded — see channel_invoice.
	from ffl_core.compliance.channel_invoice import pending_si_base_filter

	# An FFL shipment, OR a counter pickup whose gun was ordered to the store
	# and hung on the line at receive (api/counter_pickup) — that gun is the
	# customer's from the moment it arrives, until its 4473 ticket submits.
	# Column-guarded: the app code is live before `bench migrate` adds it.
	arms = [["Sales Invoice", "ffl_destination_ffl", "is", "set"]]
	if frappe.db.has_column("Sales Invoice", "ffl_pickup_in_store"):
		arms.append(["Sales Invoice", "ffl_pickup_in_store", "=", 1])
	pending_sis: list[str] = []
	if si_item_parents:
		base = {"name": ("in", list(si_item_parents)), **pending_si_base_filter()}
		# One query, the arms OR-ed; the database de-duplicates an invoice that
		# is both an FFL shipment and a pickup (it cannot be, but cheaply).
		pending_sis = frappe.get_all("Sales Invoice", filters=base, or_filters=arms,
			pluck="name", order_by="creation asc")
	for name in pending_sis:
		si = frappe.get_doc("Sales Invoice", name)
		for d in si_pending_firearm_serials(si):
			if d["item_code"] != item_code:
				continue
			out[d["serial"]] = {
				"source": "sales_invoice", "ref": name, "kind": "sales_invoice",
				"tooltip": _("On Sales Invoice {0}").format(name),
				"url": f"/app/sales-invoice/{name}",
			}
	for serial, pos in _draft_pos_serials(item_code):
		out.setdefault(serial, {
			"source": "pos", "ref": pos, "kind": "pos",
			"tooltip": _("In a parked POS sale ({0})").format(pos),
			"url": f"/app/pos-invoice/{pos}",
		})
	# Folded LAST, via setdefault: a Draft-consignment hold is the softest of
	# the three (it's a pre-dispose staging record, not a committed order), so a
	# stronger SI/POS hold on the same gun wins — the counter is the source of
	# truth for a gun someone is actively ringing up.
	# Extensions' holds (hooks gunshop_counter_holds, docs/extensions/hooks.md):
	# same shape, merged AFTER the core sources so a serial the counter already
	# holds keeps the counter's reason. A provider that fails is logged and
	# contributes nothing — it cannot name serials, so it cannot block them.
	for hook in frappe.get_hooks("gunshop_counter_holds"):
		try:
			rows = frappe.get_attr(hook)(item_code) or []
		except Exception:
			frappe.log_error(title=f"gunshop_counter_holds provider failed: {hook}")
			continue
		for r in rows:
			if r.get("serial"):
				out.setdefault(r["serial"], {k: r.get(k, "") for k in ("source", "ref", "kind", "tooltip", "url")})
	return out


@frappe.whitelist()
def reserved_counter_serials(item_code: str) -> dict:
	"""Counter-hold registry for one Item's in-stock serials — the DISPLAY half.

	Drives the Pending pill on the Item form's Serial Numbers tab. Same data as
	:func:`counter_holds`, behind the shipping-role gate, because it names which
	regulated serials sit on which order.

	Args:
	    item_code: Item whose in-stock serials to check.

	Returns:
	    {serial: {source, ref, kind, tooltip, url}} — only held serials appear.

	Permissions:
	    Canonical shipping roles. A caller WITHOUT the role gets an empty {} back
	    silently — no error is raised — so an empty result from THIS function is
	    not proof of "no holds". Anything enforcing a rule must call
	    :func:`counter_holds` instead; see its docstring for what went wrong when
	    that distinction did not exist.
	"""
	# Imported lazily because manual_order imports this module — a top-level
	# import would be a cycle.
	from ffl_core.api.manual_order import _has_ship_role

	if not _has_ship_role():
		return {}
	return counter_holds(item_code)


# ===========================================================================
# Cross-channel reservation predicates (GunBroker)
# ===========================================================================
#
# ONE question, ONE answer: "is this gun spoken for by another sales channel?"
# Before GunBroker there was exactly one channel flag (`Serial No.woo_reserved`)
# and every caller tested it by hand. Adding a second channel that way would
# mean auditing a dozen read sites for the new field and getting some of them
# wrong — and each miss is a gun that can be sold twice, which on GunBroker
# cannot be undone (no seller-side cancel; it is a refund and an apology).
#
# Scope discipline: these helpers only answer WHO HOLDS IT. Setting and
# clearing a hold stays with the channel that owns it — ffl_woo_sync keeps
# writing only `woo_reserved`/`woo_order`, ffl_integrations only
# `gb_reserved`/`gb_order`. An implementation that "helpfully" clears another
# channel's reservation is a regression, not a convenience.

# The channel labels, named once. Every app that has to say "this hold is MINE"
# — and there is now more than one — otherwise spells its own name as a literal
# at each read point, and a literal that appears in four files is a literal that
# will one day appear misspelt in a fifth. See :func:`other_channel_reservation`.
CHANNEL_WOO = "woo"
CHANNEL_GUNBROKER = "gunbroker"

CHANNEL_RESERVE_FIELDS: tuple[tuple[str, str, str], ...] = (
	# (flag_field, ref_field, channel label)
	("woo_reserved", "woo_order", CHANNEL_WOO),
	("gb_reserved", "gb_order", CHANNEL_GUNBROKER),
	# A GunBroker auction with a bid: no order yet, but promised to the high
	# bidder. Same channel as the order hold, so GunBroker's own order for the
	# gun is not refused by it; the ref is the listing's item number.
	("gb_bid_locked", "gb_item_id", CHANNEL_GUNBROKER),
)
BID_LOCK_FIELD = "gb_bid_locked"

# Read defensively: a site may have ffl_woo_sync without ffl_integrations, or
# the reverse, and a site mid-migrate has neither app's custom fields yet. A
# missing field means "that channel cannot be holding anything here", never an
# exception — this predicate sits under the counter's scan-to-add and the web
# order placer, and neither may fall over because an app is not installed.
def _channel_fields_present() -> tuple[tuple[str, str, str], ...]:
	meta = frappe.get_meta("Serial No")
	return tuple(spec for spec in CHANNEL_RESERVE_FIELDS if meta.has_field(spec[0]))


def channel_reserve_columns() -> list[str]:
	"""The channel flag/ref columns that exist here, as a SELECT field list.

	:func:`channel_reservation` re-reads any column its caller did not ask for,
	deliberately — "I did not select that" must never be answered as "no channel
	holds it". Callers that are already issuing a SELECT (often a ``for_update``
	one, where a second read would be a different snapshot) splice this in and
	get the answer from the row they locked.
	"""
	return [f for spec in _channel_fields_present() for f in spec[:2]]


def channel_reservation(serial_no: str, *, row: dict | None = None) -> dict | None:
	"""THE predicate for "is this gun held by another sales channel".

	Args:
	    serial_no: The Serial No to check.
	    row: An already-fetched row (from a `for_update` SELECT, say) carrying the
	        channel flag/ref columns. Passing it avoids a second read AND keeps
	        the answer inside the caller's row lock — a re-read would be a
	        different snapshot, which is exactly the race a locked re-check is
	        there to close. Only fields present in the row are consulted.

	Returns:
	    ``{"channel": "woo"|"gunbroker", "ref": <order name>, "field": <flag field>}``
	    for the first channel holding it, or None when no channel does. Order of
	    ``CHANNEL_RESERVE_FIELDS`` decides which is reported when (wrongly) two
	    are set; the answer is "held", and by whom is for the message text.
	"""
	specs = _channel_fields_present()
	if not specs:
		return None
	if row is None:
		wanted = [f for spec in specs for f in spec[:2]]
		row = frappe.db.get_value("Serial No", serial_no, wanted, as_dict=True) or {}
	for flag_field, ref_field, channel in specs:
		# `flag_field not in row` is NOT the same as a falsy flag: a caller that
		# selected a narrow column list simply did not ask about that channel, and
		# treating "not asked" as "not held" is how a partial SELECT turns into a
		# double sale. Fall back to a targeted read instead.
		if flag_field not in row:
			held = frappe.db.get_value("Serial No", serial_no,
				[flag_field, ref_field], as_dict=True) or {}
		else:
			held = row
		if held.get(flag_field):
			return {
				"channel": channel,
				"ref": held.get(ref_field) or "",
				"field": flag_field,
			}
	return None


def channel_reservations(serial_no: str, *, row: dict | None = None) -> list[dict]:
	"""EVERY channel holding this gun, not just the first one to answer.

	:func:`channel_reservation` answers "is it held", which is what a gate needs,
	and it stops at the first hit. A caller deciding whether it may TAKE the gun
	needs the whole list: two flags set at once should not happen, but if they
	ever do, "the first channel reported is mine, so I may proceed" is the one
	conclusion that turns an anomaly into a double sale.

	Args:
	    serial_no: The Serial No to check.
	    row: An already-fetched row carrying the flag/ref columns.

	Returns:
	    A list of ``{"channel", "ref", "field"}``, empty when nothing holds it.
	"""
	specs = _channel_fields_present()
	if not specs:
		return []
	out: list[dict] = []
	for flag_field, ref_field, channel in specs:
		held = row if (row is not None and flag_field in row) else (
			frappe.db.get_value("Serial No", serial_no,
				[flag_field, ref_field], as_dict=True) or {})
		if held.get(flag_field):
			out.append({
				"channel": channel,
				"ref": held.get(ref_field) or "",
				"field": flag_field,
			})
	return out


def other_channel_reservation(serial_no: str, *, own: str,
		row: dict | None = None) -> dict | None:
	"""Is a channel OTHER than ``own`` holding this gun?

	The question a channel asks about ITSELF. "Is this gun spoken for?" and "is
	it spoken for by somebody who is not me" are different questions, and an app
	that asks the first when it means the second starts treating its own orders
	as somebody else's — taking its own listings down mid-fulfilment.

	Args:
	    serial_no: The Serial No to check.
	    own: The asking channel's own label — :data:`CHANNEL_WOO`,
	        :data:`CHANNEL_GUNBROKER`. A label that matches nothing in
	        :data:`CHANNEL_RESERVE_FIELDS` simply excludes nothing, which is the
	        safe direction: the caller over-reports holds rather than missing
	        one.
	    row: An already-fetched row carrying the flag/ref columns.

	Returns:
	    The first non-``own`` hold, or None. A gate deciding whether a gun may be
	    SOLD wants :func:`channel_reservation` instead — for that question, the
	    holder's identity does not matter.
	"""
	for held in channel_reservations(serial_no, row=row):
		if held["channel"] != own:
			return held
	return None


def channel_hold_reason(serial_no: str, held: dict) -> str:
	"""What to tell the person at the counter about a gun another order holds.

	One sentence, two very different pieces of advice, and the difference is the
	reason this is a function rather than a format string:

	* OUR OWN web order can let the gun go — staff cancel or reassign it and POS
	  re-applies the serial by itself. "Release it there first" is the action.
	  The wording is the one that has been on this screen since before there was
	  a second channel, kept verbatim: staff read it at speed and
	  `test_pending_ship` pins it.
	* A MARKETPLACE sale cannot be released at all. GunBroker gives the seller
	  no cancel, so sending somebody to "release it there" sends them looking
	  for a button that does not exist. The only real action is a different gun.
	  `ffl_woo_sync.woocommerce.webhook._held_elsewhere_reason` reached the same
	  conclusion for the web side; this is the counter's copy of it, because
	  ffl_core cannot import that app.

	A missing ref prints as no ref at all, not as a placeholder dash: the
	sentence already contains an em dash, and "reserved by woo order — —" is
	how that looked.
	"""
	ref = (held.get("ref") or "").strip()
	channel = held.get("channel") or ""
	if held.get("field") == BID_LOCK_FIELD:
		return _("Serial {0} has bids in a GunBroker auction{1} — it is promised "
			"to the high bidder until the auction ends. Pick a different gun."
			).format(serial_no, _(" (item {0})").format(ref) if ref else "")
	if channel == CHANNEL_WOO:
		if ref:
			return _("Serial {0} is reserved by web order {1} — release it "
				"there first.").format(serial_no, ref)
		return _("Serial {0} is reserved by a web order — release it there "
			"first.").format(serial_no)
	if ref:
		return _("Serial {0} is reserved by {1} order {2} — that sale is "
			"already made and cannot be released here. Pick a different gun."
			).format(serial_no, channel, ref)
	return _("Serial {0} is reserved by {1} — that sale is already made and "
		"cannot be released here. Pick a different gun.").format(
			serial_no, channel)


def channel_reserved_serials(item_code: str) -> dict[str, dict]:
	"""Every channel-reserved serial of one Item, in one query per channel.

	The set form of :func:`channel_reservation` for callers that already hold (or
	are about to build) a list of an Item's serials — the POS serial pickers and
	the Item-form badge ask about all of them at once, and doing that one
	`get_value` at a time is a query per gun on a page that opens constantly.

	Args:
	    item_code: Item whose serials to check.

	Returns:
	    ``{serial: {"channel": …, "ref": …, "field": …}}`` — only reserved serials
	    appear. Earlier channels in ``CHANNEL_RESERVE_FIELDS`` win a collision,
	    matching :func:`channel_reservation`.
	"""
	out: dict[str, dict] = {}
	if not item_code:
		return out
	for flag_field, ref_field, channel in _channel_fields_present():
		for row in frappe.get_all(
				"Serial No",
				filters={"item_code": item_code, flag_field: 1},
				fields=["name", ref_field]):
			out.setdefault(row["name"], {
				"channel": channel,
				"ref": row.get(ref_field) or "",
				"field": flag_field,
			})
	return out


def shelf_state_columns() -> list[str]:
	"""The Serial No columns :func:`serial_shelf_state` reads, as a SELECT list.

	A function rather than a constant because several of them are custom fields
	owned by other apps and may not exist here. Public so a caller on a hot path
	can SELECT once and hand the same row to both this predicate and whatever
	else it needs off that row, instead of paying two reads.
	"""
	meta = frappe.get_meta("Serial No")
	fields = ["item_code", "status", "warehouse", "is_transfer_gun"]
	for optional in ("consigned_out", "gb_sold_unknown"):
		if meta.has_field(optional):
			fields.append(optional)
	for flag_field, ref_field, _channel in CHANNEL_RESERVE_FIELDS:
		if meta.has_field(flag_field):
			fields.extend((flag_field, ref_field))
	return fields


def serial_shelf_state(serial_no: str, *, row: dict | None = None,
		holds: dict | None = None) -> dict:
	"""Can this gun be sold right now, and if not, why not?

	The union of every "not available" rule the store has, in one place: not
	Active, no warehouse (already shipped out), a customer's transfer gun, out on
	consignment, held by another sales channel, parked on an open counter order,
	or flagged as possibly-already-sold on GunBroker.

	Args:
	    serial_no: The Serial No to judge.
	    row: An already-fetched row carrying the columns (see
	        :func:`channel_reservation` for why passing it matters under a lock).
	    holds: The Item's :func:`counter_holds` registry, when the caller is
	        judging many serials of the same Item and has already fetched it once
	        (that call is the expensive part of this predicate). Left None, it is
	        fetched here.

	Returns:
	    ``{"sellable": bool, "reason": str|None, "detail": dict}``. ``reason`` is
	    one of ``not_found`` / ``not_active`` / ``no_warehouse`` / ``custody`` /
	    ``consigned_out`` / ``sold_unknown`` / ``channel_reserved`` /
	    ``counter_hold``, or None when sellable. ``detail`` carries what the
	    operator message needs (the holding channel and order reference, the
	    counter hold record).

	Rule ORDER is deliberate and is about the message, not the verdict: report
	the most specific, most actionable fact first. "This is a customer's transfer
	gun" tells staff something they can act on; "it's on a counter order" would
	be technically true of the same gun and useless.
	"""
	fields = shelf_state_columns()
	if row is None:
		row = frappe.db.get_value("Serial No", serial_no, fields, as_dict=True)
	if not row:
		return {"sellable": False, "reason": "not_found", "detail": {}}

	if (row.get("status") or "") != "Active":
		return {"sellable": False, "reason": "not_active",
			"detail": {"status": row.get("status") or ""}}
	if not row.get("warehouse"):
		return {"sellable": False, "reason": "no_warehouse", "detail": {}}
	# The flag half of the two-key custody test, read off the row we already
	# hold. The location half (a gun moved into a custody warehouse by hand,
	# without the flag) costs a warehouse-set read per call, so it stays with
	# `ffl_core.custody.is_custody_serial`, which every publish path calls
	# before it sends anything anywhere. Same trade-off, and same wording, as
	# the Woo listing task this absorbed.
	if row.get("is_transfer_gun"):
		return {"sellable": False, "reason": "custody", "detail": {}}
	if row.get("consigned_out"):
		return {"sellable": False, "reason": "consigned_out", "detail": {}}
	# A gun GunBroker has stopped serving while /ItemsSold shows the same SKU:
	# very probably sold there, with the order not yet polled in. Fail CLOSED —
	# this is the one state where being wrong in the other direction means
	# selling a gun that is already somebody else's. Cleared by the order poll
	# matching it, or by a human confirming on GunBroker and pressing the
	# "Clear Sold Unknown" button.
	if row.get("gb_sold_unknown"):
		return {"sellable": False, "reason": "sold_unknown", "detail": {}}

	held = channel_reservation(serial_no, row=row)
	if held:
		return {"sellable": False, "reason": "channel_reserved", "detail": dict(held)}

	item_code = row.get("item_code")
	if item_code:
		hold = (holds if holds is not None else counter_holds(item_code)).get(serial_no)
		if hold:
			return {"sellable": False, "reason": "counter_hold", "detail": dict(hold)}

	return {"sellable": True, "reason": None, "detail": {}}


# ---------------------------------------------------------------------------
# Pre-sale probe (GunBroker)
# ---------------------------------------------------------------------------

CHANNEL_PRESALE_PROBE_HOOK = "channel_presale_probe"


def channel_presale_probe(serial_no: str, *, timeout: int = 3) -> dict | None:
	"""Ask every sales channel whether somebody has ALREADY bought this gun.

	The reservation flags above answer from our own database, and for a
	marketplace that is a snapshot up to fifteen minutes old — an order poll
	interval. This asks the channel itself, live, at the moment a cashier is
	about to ring the gun up.

	INVERTED hook, the same shape as ``distributor_listing_push``: ffl_core
	defines the question and every channel app registers its own answer, so this
	module stays free of any import of ffl_integrations or ffl_woo_sync. No
	registrant (no channel app installed) means None and nothing changes.

	Args:
	    serial_no: The Serial No about to be sold.
	    timeout: Seconds the caller is willing to wait IN TOTAL. Passed to each
	        registrant, which is expected to honour it.

	Returns:
	    The first ``{"blocked": True, "channel": …, "message": …}`` a registrant
	    returns, or None when no channel objects — INCLUDING when a registrant
	    could not reach its channel. This is advisory and FAILS OPEN: it is
	    wired into the scan-to-add path, which must keep working when GunBroker
	    is slow or unreachable. The refusal that must not fail open is the one on
	    ``before_submit``, which each channel app hooks itself.
	"""
	for path in frappe.get_hooks(CHANNEL_PRESALE_PROBE_HOOK):
		try:
			answer = frappe.get_attr(path)(serial_no, timeout=timeout)
		except Exception:  # noqa: BLE001 — a probe is advice, never a blocker
			frappe.logger("ffl_core").warning(
				f"channel_presale_probe registrant {path} failed for {serial_no}",
				exc_info=True)
			continue
		if answer and answer.get("blocked"):
			return answer
	return None


def lock_item_reservations(item_code: str) -> None:
	"""Take the per-Item mutex that every reservation shuffle must hold first.

	Eviction made the web store's order path lock TWO guns in one transaction (the
	disputed rifle and the replacement handed to the order being moved off it),
	which is the classic recipe for AB-BA: two webhooks each holding the gun the
	other wants. Measured, not theorised — a two-process probe deadlocked 2/2 on
	that change and 0/1 on the code before it (``QueryDeadlockError`` on
	``tabSerial No``).

	InnoDB would roll one side back, but ``woo_event`` answers WooCommerce 200
	even when it rolls back, so WC never redelivers and the work is simply lost
	until a sweep two hours later.

	One row, always taken before any Serial No lock, keyed by the Item whose
	reservation set is about to move: two transactions touching the same Item
	serialise, and no cycle can form because neither can hold a gun of that Item
	without first holding this. Callers that touch several Items in one
	transaction must take them in sorted order.

	Lives here, not in the web app, because a second channel now moves the same
	reservation set (GunBroker assigns a unit to a sold line) and must take the
	SAME mutex — two copies of "the lock" would not exclude each other.

	The Item row is a mutex here, nothing more — nothing about it is read or
	written. Contention is per firearm model and lasts one transaction. Spelled as
	an explicit ``SELECT … FOR UPDATE`` rather than a ``get_value(for_update=True)``
	for exactly that reason: no value is wanted, only the lock.
	"""
	if item_code:
		frappe.db.sql(
			"SELECT name FROM `tabItem` WHERE name = %s FOR UPDATE", (item_code,))


# ===========================================================================
# The shared pool of a uniform-price firearm
# ===========================================================================
#
# A uniform-price firearm Item is ONE product standing for several
# interchangeable guns, on every channel at once. A sale on any channel owes a
# UNIT, not a particular gun — the gun is named later, by staff, when it is
# pulled off the rack. Between those two moments the unit is *committed*: it
# reserves no `Serial No`, so none of the per-gun flags above can see it.
#
# So there is one pool and one rule, written once, here:
#
#     sellable = free guns  -  Σ over channels of units that channel has sold
#                               but not yet named a gun for
#
# `uniform_free_serials` is the free guns. Each channel app answers "how many
# units have I committed?" through the `channel_committed_units` hook, so this
# module — and every channel — can ask without importing the others (a test
# enforces that ffl_integrations and ffl_woo_sync never import each other). Each
# channel app hears about changes through `channel_commitments_changed`.

CHANNEL_COMMITTED_UNITS_HOOK = "channel_committed_units"
CHANNEL_COMMITMENTS_CHANGED_HOOK = "channel_commitments_changed"

# How the per-channel breakdown reads in the counter's refusal.
_CHANNEL_LABELS = {CHANNEL_WOO: "web", CHANNEL_GUNBROKER: "GunBroker"}


def uniform_free_serials(item_code: str, *, ignore_hold_ref: str | None = None) -> list[str]:
	"""In-stock guns of a uniform-price firearm Item that NOTHING is holding —
	the free pool before any channel's committed units come off it — oldest
	first.

	One rulebook, shared by every channel's quantity: the storefront count, the
	web order's placeholder naming, the counter's sale gates and GunBroker's
	model quantity all read it. Keys, each closing a different hole:

	* any sales channel's reservation (:func:`channel_reserved_serials`:
	  ``woo_reserved``, ``gb_reserved``, an auction bid lock) — a gun with a
	  GunBroker buyer is spoken for just as surely as one with a web order;
	* ``consigned_out`` — Active and unreserved, but sitting at a partner FFL, so
	  it was never part of the listing's stock;
	* ``is_transfer_gun`` + the custody warehouse — a CUSTOMER's gun in for a
	  transfer hangs off this very SKU, by design; counting it would let a
	  channel sell a rifle the store does not own. Two keys because a flag can go
	  unstamped (a pre-custody receive, a hand-built Stock Entry) while the gun
	  physically sits in custody;
	* ``gb_sold_unknown`` — GunBroker stopped serving the gun's listing while its
	  SKU shows in the sold list: very probably sold, order not yet polled in.
	  Fail CLOSED, the same call :func:`serial_shelf_state` makes; the per-serial
	  listing path already did, this pool did not;
	* :func:`counter_holds` — promised on an open counter order. Deliberately the
	  UNGATED helper: this runs in the Guest webhook request too, and the
	  whitelisted `reserved_counter_serials` answers a Guest with a silent ``{}``
	  that reads as "nothing is held".

	Oldest first (``creation``) so the gun a channel suggests for the next sale is
	the one that has been on the shelf longest — FIFO is the default a clerk can
	override, never a silent reshuffle.

	Args:
	    item_code: The uniform-price firearm Item.
	    ignore_hold_ref: A counter document (``POS Invoice`` / ``Sales Invoice``
	        name) whose own hold must NOT count against the pool — the invoice
	        being submitted, or edited, holds its own gun, and that gun is the one
	        being sold, not a competitor for it.

	Returns:
	    The serial numbers, oldest first.
	"""
	held = counter_holds(item_code)
	if ignore_hold_ref:
		held = {s: h for s, h in held.items() if h.get("ref") != ignore_hold_ref}
	channel_held = channel_reserved_serials(item_code)
	filters = {"item_code": item_code, "status": "Active",
		"consigned_out": 0, "is_transfer_gun": 0}
	# Column-guarded: the field belongs to ffl_integrations, which a POS-only
	# install does not have, and a deploy runs the code before migrate adds it.
	if frappe.db.has_column("Serial No", "gb_sold_unknown"):
		filters["gb_sold_unknown"] = 0
	active = frappe.get_all(
		"Serial No", filters=filters, order_by="creation asc, name asc", pluck="name")
	in_custody = custody_serials(item_code)
	return [s for s in active
		if s not in held and s not in in_custody and s not in channel_held]


def is_uniform_firearm_item(item_code: str) -> bool:
	"""A firearm Item priced as one product for all its guns — the only kind
	whose sales can be owed as a unit before a gun is named."""
	row = frappe.db.get_value(
		"Item", item_code, ["is_firearm", "item_group", "uniform_sell_price"],
		as_dict=True, cache=True)
	return bool(row and _is_firearm_row(row) and row.get("uniform_sell_price"))


def committed_units(item_code: str, *, exclude: str | None = None) -> dict[str, int]:
	"""Units each sales channel has sold but not yet named a gun (or, for a
	non-firearm, shipped) — ``{channel: units}``.

	Asked of every ``channel_committed_units`` registrant
	(``fn(item_code) -> int``). A registrant labels itself with a ``channel``
	attribute on the function (:data:`CHANNEL_WOO` / :data:`CHANNEL_GUNBROKER`);
	without one the hook path stands in, so an unlabelled registrant still counts —
	it just cannot be excluded by name.

	Args:
	    item_code: The Item.
	    exclude: A channel label to leave out — the asking channel when it already
	        counts its own commitments in its own stock rule (the web store does),
	        so they are not subtracted twice.

	Returns:
	    One entry per answering channel, each clamped at zero.

	A registrant that raises PROPAGATES. The caller is deciding how many units to
	offer, and a missing channel reads as "that channel owes nothing" — an
	overstated number, which on a marketplace is a sale of a gun that is not
	there. A WRITER (a quantity push) therefore skips on the exception and keeps
	what it last sent; a GATE decides for itself (see
	:func:`channel_claim_block_reason`).
	"""
	out: dict[str, int] = {}
	for path in frappe.get_hooks(CHANNEL_COMMITTED_UNITS_HOOK):
		fn = frappe.get_attr(path)
		channel = getattr(fn, "channel", None) or path
		if exclude and channel == exclude:
			continue
		out[channel] = out.get(channel, 0) + max(0, int(fn(item_code) or 0))
	return out


def uniform_sellable_units(item_code: str) -> int:
	"""How many units of a uniform-price firearm any channel may still promise:
	the free guns less every channel's committed units, floored at zero.

	Raises whatever :func:`committed_units` raises — see there for why a writer
	must treat that as "do not push", never as zero."""
	free = len(uniform_free_serials(item_code))
	return max(0, free - sum(committed_units(item_code).values()))


def channel_claim_block_reason(item_code: str, *, selling: int = 1,
		ignore_hold_ref: str | None = None) -> str | None:
	"""Why this Item's last free unit(s) may not be sold over the counter — or None.

	A uniform-price order on ANY channel owes a unit and reserves no `Serial No`
	(see the section comment above). Every counter gate tests the per-gun flags,
	which a claim never sets — so between a paid order landing and staff naming
	its gun, those gates saw nothing.

	The failure that motivates it: two free Mosins, two paid web orders, one claim
	each. The storefront correctly reads 0. A staff member sells BOTH at the
	counter — no flag is set, there is no counter hold, nothing objects — and two
	paid orders become unfulfillable.

	The rule is unit-level because the claim is: block when selling would leave
	the free pool short of what is owed, i.e. ``free - selling < claims``. For one
	gun that is ``free <= claims``.

	Args:
	    item_code: The Item of the gun about to be sold.
	    selling: How many guns of this Item the sale takes (one per line).
	    ignore_hold_ref: The counter document selling them, when it already holds
	        them (see :func:`uniform_free_serials`).

	Returns:
	    The refusal text, naming each channel's owed units, or None. None for
	    anything that cannot carry a claim — a non-firearm, a per-serial firearm, a
	    missing Item — so a caller can run it over any serial without asking what
	    kind of Item it is. FAILS OPEN when a channel cannot answer: a counter sale
	    cannot depend on a marketplace app being healthy, and the claim is also
	    caught where it can be (the storefront reads 0 meanwhile).
	"""
	if not is_uniform_firearm_item(item_code):
		return None
	try:
		owed = {c: n for c, n in committed_units(item_code).items() if n}
		if not owed:
			return None
		free = len(uniform_free_serials(item_code, ignore_hold_ref=ignore_hold_ref))
	except Exception:  # noqa: BLE001 — see "FAILS OPEN"
		frappe.log_error(title=f"channel_claim_block_reason failed for {item_code}")
		return None
	claims = sum(owed.values())
	if free - max(1, int(selling)) >= claims:
		return None
	breakdown = ", ".join(
		f"{_CHANNEL_LABELS.get(channel, channel)}: {n}" for channel, n in owed.items())
	return _(
		"{0} sold order(s) are already owed a {1} ({2}) and only {3} free one(s) "
		"remain — selling this one leaves an order unfillable. Fulfil or cancel an "
		"order first."
	).format(claims, item_code, breakdown, free)


# Kept for callers and tests that still use the web-only name.
web_claim_block_reason = channel_claim_block_reason


def notify_channel_commitments_changed(item_code: str, *, source: str) -> None:
	"""Tell every channel that this Item's committed units just moved.

	Call it wherever a channel records or releases a sale it has not yet
	fulfilled: another channel's quantity is derived from this one's commitments,
	and nothing else would tell it (no stock ledger entry is posted until the
	goods leave).

	Args:
	    item_code: The Item whose commitments changed.
	    source: The channel label that changed them (:data:`CHANNEL_WOO`,
	        :data:`CHANNEL_GUNBROKER`). A handler may skip its own source when its
	        own writers already cover the change.

	NEVER raises: it sits inside the transaction that records the sale, which is
	the more important fact — a push that did not get queued is caught by the
	channels' sweeps, an order that failed to save is a sale nobody knows about.
	The handlers only enqueue (after commit); none does network I/O here.
	"""
	item_code = (item_code or "").strip()
	if not item_code:
		return
	try:
		paths = frappe.get_hooks(CHANNEL_COMMITMENTS_CHANGED_HOOK)
	except Exception:  # noqa: BLE001
		return
	for path in paths:
		try:
			frappe.get_attr(path)(item_code, source=source)
		except Exception:  # noqa: BLE001
			try:
				frappe.log_error(
					title=f"channel_commitments_changed handler failed: {path} ({item_code})")
			except Exception:  # noqa: BLE001 — the log is best effort too
				pass
