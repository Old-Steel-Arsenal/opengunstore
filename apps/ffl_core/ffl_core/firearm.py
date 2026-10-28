"""Single source of truth for firearm-domain constants and lookups.

Importers (FastBound, Intake) and the compliance layer all need the same
notion of "is this Item a firearm?" and "what's the manufacturer serial?".
Keep that knowledge in one place.
"""
from __future__ import annotations

import frappe
from frappe.utils import cint

from ffl_core.roles import COUNTER_ROLES


FIREARM = "Firearm"
NEW_FIREARM = "New Firearm"            # legacy — retired; never created
USED_FIREARM = "Used Firearm"          # legacy — retired; never created
AMMUNITION = "Ammunition"
ACCESSORY = "Accessory"

# Type-based leaf groups (post-restructure). Items live in these; the "Used"
# concept moved from Item Group to a per-Item is_used checkbox.
HANDGUN = "Handgun"                    # legacy dead leaf — split into Pistol / Revolver (2026-07-21)
PISTOL = "Pistol"
REVOLVER = "Revolver"
RIFLE = "Rifle"
SHOTGUN = "Shotgun"
LONG_GUN = "Long Gun"                  # legacy — pre-split; kept for existing items
OTHER_FIREARM = "Other Firearm"
# Receivers & frames (catalog master Firearms › Receivers & Frames; owner
# 2026-09-26: the existing "Receiver" group under Firearm) — a firearm group.
RECEIVER = "Receiver"
NFA = "NFA"                            # parent for SOT items
NFA_LEAVES = ("Suppressor", "SBR", "SBS", "DD", "MG", "AOW")

# Canonical firearm-type vocabulary (bound-book wording): the ATF Form 4473
# Section B values plus the NFA classifications. Single source for every copy
# that must agree — the Item / Serial No Select fixtures, the backfill patch's
# field seed, FastBound's outbound item-type allowlist (ffl_integrations
# client._fb_item_type), and the JS dropdowns (ffl.FIREARM_TYPE_OPTIONS in the
# bundle). The fixture and JS copies are format-bound hand copies;
# test_nfa_firearm_types pins them all to this tuple.
ATF_FIREARM_TYPES: tuple[str, ...] = (
	"Pistol", "Revolver", "Rifle", "Shotgun", "Receiver", "Other Firearm",
	"Silencer", "Short Barreled Rifle", "Short Barreled Shotgun",
	"Machine Gun", "Destructive Device", "Any Other Weapon",
)
# The Select options string form (leading blank = "no default chosen").
FIREARM_TYPE_SELECT_OPTIONS = "\n" + "\n".join(ATF_FIREARM_TYPES)

# The NFA subset of that vocabulary, by firearm_type — as opposed to NFA_LEAVES
# above, which is Item GROUP names. Anything in here is registered under the
# National Firearms Act: transferring one is a Form 3/4 with an ATF approval
# wait, not an FFL transfer, so every code path that assumes "ship it to the
# buyer's dealer and book a disposition" has to exclude these rather than
# discover the difference afterwards. Derived here so a new NFA type is added
# once, next to the vocabulary it belongs to, instead of being re-listed inside
# each channel's guard.
# No runtime caller since the GunBroker channel stopped refusing NFA items (the
# Form 3/4 is filed in ATF eForms, outside POS). Kept as the one list of NFA
# firearm types for planned Form 3/4 work and for tests.
NFA_FIREARM_TYPES: frozenset[str] = frozenset({
	"Silencer", "Short Barreled Rifle", "Short Barreled Shotgun",
	"Machine Gun", "Destructive Device", "Any Other Weapon",
})

ALL_FIREARM_GROUPS: frozenset[str] = frozenset({
	# Parent groups
	FIREARM, NFA,
	# Leaves
	PISTOL, REVOLVER, HANDGUN, RIFLE, SHOTGUN, OTHER_FIREARM, RECEIVER,
	*NFA_LEAVES,
	# Legacy aliases, retired (patches/retire_new_used_firearm_item_groups deletes
	# them only where nothing links to them). KEPT here on purpose: on a site where
	# one is still linked (an Item, a submitted line), its Items must keep gating
	# as firearms — bound book, 4473, FastBound.
	NEW_FIREARM, USED_FIREARM, LONG_GUN,
	# PISTOL / REVOLVER membership is load-bearing, not cosmetic: RSR
	# `promote_to_item` builds its Item doc with NO is_firearm field and relies
	# on the `auto_set_is_firearm` before_save hook, which stamps is_firearm=1
	# only when item_group ∈ this set. (`used_firearm_item_doc` sets is_firearm=1
	# directly; RSR does NOT — so a Pistol/Revolver leaf missing here would let a
	# promoted handgun escape firearm gating: bound book / 4473 / FastBound.)
})

# Domain groups: firearms + ammo + accessories. Used by listing status sync
# and other domain logic that wants "anything we sell".
AMMO_LEAVES = ("Pistol Ammo", "Rifle Ammo", "Shotgun Ammo", "Rimfire Ammo")
ACCESSORY_LEAVES = ("Optic", "Magazine", "Holster", "Cleaning", "Light", "Knife", "Safe", "Misc Accessory",
	"Slings & Mounts", "Sights", "Optic Mounts")
# Gun parts — never firearms (a serialized receiver is filed under Firearm).
PARTS = "Parts"
PARTS_LEAVES = ("Uppers", "Barrels", "Bolt Carrier Groups & Charging Handles", "Triggers",
	"Lower Parts & Kits", "Muzzle Devices", "Handguards & Rails", "Stocks & Grips")
FIREARM_DOMAIN_GROUPS: tuple[str, ...] = (
	*ALL_FIREARM_GROUPS,
	AMMUNITION, *AMMO_LEAVES,
	ACCESSORY, *ACCESSORY_LEAVES,
	PARTS, *PARTS_LEAVES,
)


def firearm_item_sql(alias: str = "it") -> str:
	"""The :func:`_is_firearm_row` predicate as a SQL fragment over a `tabItem`
	alias — the explicit ``is_firearm`` flag OR legacy membership in a firearm
	Item Group. The caller binds ``firearm_groups=tuple(ALL_FIREARM_GROUPS)``.
	One home for the rule, so a raw query (the POS sweep, the web stock audit)
	cannot drift from the Python predicate."""
	return f"({alias}.is_firearm = 1 OR {alias}.item_group IN %(firearm_groups)s)"


def _is_firearm_row(row) -> bool:
	"""The firearm test over an already-fetched Item row: the explicit `is_firearm`
	flag, or legacy membership in a firearm Item Group. The single home for this
	predicate — callers that already hold the row apply it without a second read."""
	return bool(row and (row.get("is_firearm") or row.get("item_group") in ALL_FIREARM_GROUPS))


def is_per_serial_priced(row) -> bool:
	"""A gun priced serial by serial — a firearm Item not on uniform pricing (the
	old guns, each its own website product) — over an already-fetched Item row
	(``is_firearm``, ``item_group``, ``uniform_sell_price``). The one test the
	storefront's stock rules and drop-ship sourcing share."""
	return _is_firearm_row(row) and not row.get("uniform_sell_price")


def is_firearm_item(item_code: str | None) -> bool:
	"""True iff the Item is flagged as a firearm.

	Single source of truth: `Item.is_firearm` (Custom Field). The Item Group
	is still used as a fallback for legacy rows the auto-fill hasn't touched
	(insert-time hook stamps `is_firearm=1` on new firearm-group items).
	"""
	if not item_code:
		return False
	return _is_firearm_row(frappe.db.get_value(
		"Item", item_code, ["is_firearm", "item_group"],
		as_dict=True, cache=True,
	))


def _is_regulated_firearm_row(row) -> bool:
	"""A GCA-regulated firearm: a firearm Item that is NOT flagged antique.

	Antiques (pre-1899 / GCA-exempt) are firearms for inventory + serial
	tracking, but invisible to the bound-book / 4473 / FastBound machinery —
	so the compliance layer tests THIS, while the inventory/receive/serial
	paths keep using `_is_firearm_row`. Row-level twin of
	`is_regulated_firearm_item`; callers holding the row apply it directly."""
	return _is_firearm_row(row) and not (row and row.get("is_antique"))


def is_antique_item(item_code: str | None) -> bool:
	"""True iff the Item is flagged as an antique firearm (`Item.is_antique`).

	Antique-ness is a per-SKU fact (a pre-1899 model), so it lives on the Item,
	not the Serial No — every gate resolves it from the line's item_code."""
	if not item_code:
		return False
	return bool(frappe.db.get_value("Item", item_code, "is_antique", cache=True))


def is_regulated_firearm_item(item_code: str | None) -> bool:
	"""True iff the Item is a firearm the GCA regulates — a firearm that is NOT
	an antique. The single predicate the compliance / bound-book / 4473 layer
	gates on (`is_firearm_item` stays the inventory/serial/receive test). One
	cached read covers both the firearm test and the antique flag."""
	if not item_code:
		return False
	return _is_regulated_firearm_row(frappe.db.get_value(
		"Item", item_code, ["is_firearm", "item_group", "is_antique"],
		as_dict=True, cache=True,
	))


def items_with_boundbook_presence(item_codes) -> set:
	"""The subset of item_codes that have a firearm CURRENTLY in the bound book — an
	in-stock (status='Active') Serial No carrying a FastBound id, or one under a
	submitted (docstatus=1) FFL Acquisition. Such an Item is a regulated firearm on
	the books and can't be flagged antique.

	Deliberately excludes sold/transferred/cancelled serials (Delivered/Inactive)
	and DRAFT acquisitions: their A&D entries already stand on their own, and
	flagging the Item antique only affects FUTURE receives. fastbound_item_id is
	never cleared on cancel/disposition, so the status filter is what stops a
	permanent false-positive lock once a gun has cycled out."""
	codes = list({c for c in (item_codes or []) if c})
	if not codes:
		return set()
	# In-stock serials that carry a FastBound bound-book id.
	booked = set(frappe.get_all(
		"Serial No",
		filters={"item_code": ("in", codes), "status": "Active",
			"fastbound_item_id": ("!=", "")},
		pluck="item_code", distinct=True,
	))
	# A just-received gun whose FastBound push hasn't stamped the serial yet is
	# still on the books — catch it via its submitted FFL Acquisition, but only
	# while the serial is still in stock (Active).
	remaining = [c for c in codes if c not in booked]
	if remaining:
		acqs = frappe.get_all(
			"FFL Acquisition",
			filters={"item": ("in", remaining), "docstatus": 1},
			fields=["item", "serial"],
		)
		live = {a.serial for a in acqs if a.serial}
		if live:
			active = set(frappe.get_all(
				"Serial No",
				filters={"name": ("in", list(live)), "status": "Active"},
				pluck="name",
			))
			for a in acqs:
				if a.serial in active:
					booked.add(a.item)
	return booked


def item_has_boundbook_presence(item_code: str | None) -> bool:
	"""True iff this Item already has a firearm recorded in the bound book (see
	items_with_boundbook_presence). The antique flag may be set only on items with
	NO such footprint — a booked gun leaves only via a disposition, never by
	re-flagging its Item antique."""
	if not item_code:
		return False
	return bool(items_with_boundbook_presence([item_code]))


def assert_antique_flag_allowed(item_code: str | None) -> None:
	"""Raise if `item_code` already has a firearm in the bound book. The ONE home
	for the antique-lock rule + message, shared by block_antique_on_booked_item
	(the Item validate hook) and the Receive Goods receive path (which writes
	is_antique via db.set_value, bypassing the hook). Both callers go through this
	so the rule and wording can't drift."""
	if item_has_boundbook_presence(item_code):
		frappe.throw(
			frappe._("Cannot mark Item {0} as Antique — it already has firearm(s) recorded in "
				"the bound book. A gun in the bound book leaves only via a disposition, not by "
				"flagging its Item antique. Antique is for items with no bound-book entries.")
				.format(item_code),
			title=frappe._("Already in Bound Book"),
		)


def audit_trace(doctype: str, name: str, title: str, body: str) -> None:
	"""The never-raise audit pair the receive path leaves behind: an Info
	Comment on the record — permanent, where an audit or an ATF question will
	look — and an Error Log line under ``title`` — greppable across records, but
	cleared by Log Settings. A broken audit line must not be able to stop goods
	coming in the door."""
	try:
		frappe.get_doc({"doctype": "Comment", "comment_type": "Info", "reference_doctype": doctype,
			"reference_name": name, "content": body}).insert(ignore_permissions=True)
	except Exception:  # pragma: no cover — diagnostics must never block a receive
		pass
	try:
		frappe.log_error(title=title, message=body, reference_doctype=doctype, reference_name=name)
	except Exception:  # pragma: no cover
		pass


def log_antique_flip(item_code: str, source: str, year_made=None, extra: str = "") -> None:
	"""Record that an Item was just flagged Antique (0->1), and by whom.

	The antique flag is the one switch that takes a gun out of the bound book
	and off the 4473 — so who threw it, when, and on what evidence has to be
	answerable later. An ordinary Item.save leaves a Version row behind, but the
	receive path writes the flag with ``db.set_value(..., update_modified=False)``
	to keep unrelated Item hooks from firing, which leaves no trace at all. This
	is that trace. Deliberately invisible to the operator: it records the
	decision, it does not question it.

	Written TWICE, on purpose, because the two homes fail differently:

	- a Comment on the Item — permanent, and sitting on the very record an
	  inspection would be looking at. This is the copy that has to survive: an
	  ATF question about why a gun never hit the A&D book can arrive years
	  later, and Frappe's Log Settings clear the Error Log after ~30 days by
	  default.
	- an Error Log line — same facts, centrally greppable across every SKU
	  without opening Items one at a time. Short-lived by design.

	Never raises — a broken audit line must not be able to stop goods coming in
	the door (same rule as ``log_transfer_type_near_miss``). No throttle: a SKU
	becomes antique once, so these are rare and each one matters."""
	body = (
		f"Item: {item_code}\n"
		f"Flagged Antique by: {frappe.session.user}\n"
		f"Source: {source}\n"
		f"Year of manufacture on record: {year_made or '(none given)'}\n"
		f"{extra}\n\n"
		"An antique (GCA-exempt, 27 CFR 478.11: made in or before 1898) is "
		"tracked as serialized inventory but is NOT entered in the FastBound "
		"bound book and takes no 4473/NICS at sale. Since 2026-07-24 the "
		"checkbox alone decides — a year of manufacture is optional provenance, "
		"not a precondition — so this record is the audit trail for that decision."
	)
	audit_trace("Item", item_code, "Item flagged Antique (skips bound book + 4473)", body)


def log_zero_cost_confirmation(serial: str, reason: str, note: str = "",
		purchase_receipt: str = "") -> None:
	"""Record that a gun was received as genuinely FREE, and by whom.

	Built to the same rule as ``log_antique_flip`` above, for the same reason:
	the flag is stamped with ``db.set_value(..., update_modified=False)`` so the
	receive does not fire unrelated Serial No hooks, and that write leaves no
	Version row — no trace at all. This is that trace.

	Why an assertion about COST earns the same treatment as the antique flag: it
	is the one statement that ends the question. A gun with an empty cost gets
	asked about at every stock-cost audit; a gun marked confirmed-free is skipped
	from then on (``custody.audit_legacy_custody_candidates``), and its $0 flows
	into valuation and COGS. So "who said this was free, when, and on what
	receipt" has to be answerable later, and the field alone cannot answer it.

	Written TWICE for the two failure modes, exactly as log_antique_flip is:

	- a Comment on the Serial No — permanent, and on the record an audit or an
	  ATF question would already be looking at;
	- an Error Log line — centrally greppable across a whole receive, but
	  cleared by Log Settings after ~30 days.

	Lines with no Serial No at all (ammo, accessories) are NOT logged here: they
	carry the same pair on the Purchase Receipt Item, and a submitted Purchase
	Receipt already records its own owner + timestamp, so nothing is lost there.

	Never raises — a broken audit line must not be able to stop goods coming in
	the door (same rule as ``log_antique_flip`` / ``log_transfer_type_near_miss``).
	"""
	body = (
		f"Serial: {serial}\n"
		f"Confirmed $0 acquisition cost by: {frappe.session.user}\n"
		f"Reason: {reason or '(none given)'}\n"
		f"Explanation: {note or '(none given)'}\n"
		f"Purchase Receipt: {purchase_receipt or '(none)'}\n\n"
		"A confirmed zero cost is an ASSERTION, not a blank: it says the store "
		"really paid nothing for this firearm (a gift, a promo, a warranty "
		"replacement, a sample). It is what lets a later stock-cost audit skip "
		"this gun instead of asking the owner about it again, and it is what "
		"the stock ledger, the GL and the margin report all report as cost. "
		"A cost nobody looked up is refused at receive instead — see "
		"ffl_core.api.receive_goods._resolve_line_cost."
	)
	audit_trace("Serial No", serial, "Firearm received at a confirmed $0 cost", body)


def block_antique_on_booked_item(doc, method: str | None = None) -> None:
	"""Item validate: enforce the antique-flag rules.

	- Antique is meaningful only for firearms (the field's depends_on is UI-only),
	  so silently clear a stray flag on a non-firearm rather than carry dead data.
	- An Item may be flagged Antique only while it has NO bound-book footprint:
	  reject the 0->1 transition on a booked item. Un-flagging (1->0) and
	  already-antique items with no footprint are unaffected."""
	if not doc.get("is_antique"):
		return
	# Antique only applies to firearms (use the Item Group fallback so a
	# firearm-group item not yet stamped is_firearm at validate time still counts).
	if not _is_firearm_row(doc):
		doc.is_antique = 0
		return
	# Only the transition INTO antique matters — an already-antique item (no
	# footprint by construction) re-saving for other reasons must not re-validate.
	if not doc.is_new() and not doc.has_value_changed("is_antique"):
		return
	assert_antique_flag_allowed(doc.name)


def auto_set_is_firearm(doc, method: str | None = None) -> None:
	"""Item before_save hook: stamp `is_firearm=1` when the Item Group is a
	firearm group. Operators can still toggle the box manually for edge
	cases (e.g., a one-off custom build sold as Accessory).

	Also forces `has_serial_no=1` on every firearm-group Item. Every firearm is
	serial-tracked (the Used Firearm path was unified), and the whole 4473 /
	A&D / POS picker pipeline reads the Serial No table — an Item received
	with has_serial_no=0 books fine in FastBound but is unsellable at the
	counter (2026-09-16: NFA suppressor promoted from RSR with the dialog's
	checkbox left off). Enforced here, not per creator, so the six Item
	creation paths and the manual form all get it.

	The flag is only forced while it can legally be set. ERPNext refuses
	has_serial_no on a non-stock Item ("'Has Serial No' can not be 'Yes' for
	non-stock item" — validate_item_type, which runs BEFORE this before_save
	hook, so it would only fire on the NEXT save and the operator could never
	untick it), and refuses to flip it once uncancelled Stock Ledger Entries
	exist. A non-stock Item filed under a firearm group (a fee or service line)
	and a legacy Item with stock are therefore left alone — the latter needs the
	cancel-and-re-receive recipe instead.
	"""
	if doc.item_group not in ALL_FIREARM_GROUPS:
		return
	if not getattr(doc, "is_firearm", None):
		doc.is_firearm = 1
	if not doc.get("has_serial_no") and cint(doc.get("is_stock_item")) \
			and (doc.is_new() or not doc.stock_ledger_created()):
		doc.has_serial_no = 1
	if doc.get("has_serial_no") and not doc.get("serial_no_series"):
		# Same default the Receive Goods / RSR promote creators use, so
		# submit-time serial validation never nags for the missing series.
		doc.serial_no_series = f"{doc.item_code or doc.name}-.####"


# NOTE: The legacy helpers `get_firearm_serial()` and `get_firearm_serials_bulk()`
# were removed when the Used Firearm path was unified — every firearm is now
# has_serial_no=1 and its serials live in the Serial No table, not on
# Item.firearm_serial. Hot-path callers should query Serial No directly.


@frappe.whitelist()
def firearm_items_in(item_codes: list[str] | str) -> list[str]:
	"""Return the subset of item_codes that are REGULATED firearms (antiques excluded).

	Called from POS JS to decide whether to surface the 4473 button / whether
	the submit gate applies. Antique firearms (GCA-exempt) need no 4473, so
	they are filtered out here — an antique-only cart looks like an accessories
	cart to the POS and completes without the charge-then-hold flow.

	Args:
	    item_codes: Item codes — a list or a JSON string (Frappe transports
	        list args as JSON over the wire).

	Returns:
	    The subset of item_codes that are regulated firearms.
	"""
	frappe.only_for(COUNTER_ROLES)
	if isinstance(item_codes, str):
		import json
		item_codes = json.loads(item_codes or "[]")
	if not item_codes:
		return []
	codes = list({c for c in item_codes if c})
	rows = frappe.get_all(
		"Item",
		filters={"name": ("in", codes)},
		fields=["name", "is_firearm", "item_group", "is_antique"],
	)
	return [r.name for r in rows if _is_regulated_firearm_row(r)]


def _coerce_item_codes(item_codes: list[str] | str) -> list[str]:
	"""Normalise the whitelisted ``item_codes`` arg to a list.

	Frappe transports a list arg as a JSON string, so callers historically
	``json.loads`` it — but a BARE code string (``"TYPE56-762X39"``) isn't JSON
	and blew up with a 500 (``JSONDecodeError``; today's MCP ``available_serials``
	first call). Try JSON; on failure — or a non-list result — treat the raw
	string as a single code."""
	if not isinstance(item_codes, str):
		return list(item_codes or [])
	import json
	try:
		parsed = json.loads(item_codes or "[]")
	except (ValueError, TypeError):
		parsed = None
	return parsed if isinstance(parsed, list) else [item_codes]


def _current_invoice_own_serials(invoice_name: str | None, codes) -> set[str]:
	"""Serials ACTUALLY on a live Draft POS Invoice's own lines (both storage
	models, via serials_for_line), scoped to `codes` — the only serials the
	self-hold exemption may lift (follow-up #7 D).

	Reads GROUND TRUTH from the invoice's lines rather than trusting the
	reservation registry's ref attribution, and requires the name to be a LIVE
	Draft POS Invoice (docstatus=0): a submitted / cancelled / unknown name
	resolves to the empty set, so a caller cannot self-report an arbitrary
	invoice name to open the picker."""
	invoice_name = (invoice_name or "").strip()
	if not invoice_name:
		return set()
	if frappe.db.get_value("POS Invoice", invoice_name, "docstatus") != 0:
		return set()  # only a live Draft grants the exemption
	from ffl_core.serials import serials_for_line
	own: set[str] = set()
	for row in frappe.get_all(
		"POS Invoice Item",
		filters={"parent": invoice_name, "item_code": ("in", list(codes))},
		fields=["serial_no", "serial_and_batch_bundle"],
	):
		own.update(serials_for_line(row))
	return own


def _pos_picker_excluded_serials(item_codes, current_invoice: str | None = None,
		exclude_custody: bool = True) -> set[str]:
	"""The serials a POS firearm picker must NOT offer — a
	gun that is spoken-for or already gone to a dealer. ONE rulebook shared by
	all three pickers (available_serials_for_items / available_serials_with_
	prices / in_stock_firearm_serials) so they can't diverge:

	  1. held on an open counter order / submitted Sales Invoice / Draft
	     consignment — reserved_counter_serials, the single hold registry the
	     counter scan gate and the consignment picker already read;
	  2. consigned_out=1 — already disposed to a dealer, physically off the
	     shelf (a re-sale would double-book the bound book);
	  3. sitting in a per-dealer consignment warehouse — the same physical-
	     location fallback the consignment picker uses to catch flag/warehouse
	     drift (a gun in a consignment warehouse whose consigned_out flag is
	     stale);
	  4. a customer's transfer gun (is_transfer_gun=1) or anything sitting in a
	     customer-custody warehouse — property the store possesses but does not
	     own, so it can never be part of a SALE. Flag and location are checked
	     independently for the same drift reason as rule 3;
	  5. reserved by ANOTHER SALES CHANNEL — a paid web order or a GunBroker
	     buyer (reservations.channel_reserved_serials). This rule closes a hole
	     that predates GunBroker: every OTHER counter gate tests the channel
	     flag, and this picker never did, so a rifle a paid web order already
	     owned was still offered to the cashier (the flag only re-armed the
	     other gates once POS happened to name that same gun). Owner's decision,
	     2026-08-26: the picker excludes reservations from every channel, so a
	     gun claimed online has to be released online before the counter can
	     sell it.

	`item_codes`: the codes being offered (reserved_counter_serials is
	per-item). Returns the union set of serials to drop.

	`exclude_custody` (default True) turns rule 4 off for the ONE picker that
	must see custody guns: the POS customer-transfer dialog
	(in_stock_firearm_serials). Handing a transfer gun back to its owner is the
	entire point of custody — a picker that hid it would strand every gun this
	feature receives. Rules 1-3 still apply there, so a custody gun already held
	on another ticket is still refused.

	`current_invoice` (POS Invoice docname, optional): lift the hold that THIS
	invoice's OWN Draft POS line places on a serial — a gun parked on the invoice
	being edited IS this sale, not someone else's hold, so its own 4473 / Item-
	Details picker must still offer it (a past incident self-lockout,
	serial 21696 vanishing from its own line's dropdown). Exemption is doubly
	gated (follow-up #7 D): the serial must (1) be on THIS invoice's OWN line
	(_current_invoice_own_serials, ground truth) AND (2) have this invoice's own
	POS hold as its WINNING reservation. So SI / consignment / OTHER-invoice POS
	holds and the consigned_out / consignment-warehouse exclusions all stay in
	force, and a double-hold (own POS + submitted SI) fails closed.

	Sibling of manual_order.ignore_invoice (the Create Order / Edit picker's
	own-order exemption): same concept — exempt the editing doc's OWN hold — in
	two rulebooks. This one is gated to a kind='pos' hold because the POS picker
	only owns parked-POS holds; the SI-hold exemption lives in the Create Order
	rulebook. Keep the two in sync when the hold model changes."""
	from ffl_core.reservations import channel_reserved_serials, counter_holds
	from ffl_core.warehouses import consignment_warehouse_names, custody_warehouse_names

	codes = [c for c in dict.fromkeys(item_codes or []) if c]
	if not codes:
		return set()
	current_invoice = (current_invoice or "").strip() or None
	# Ground-truth serials on THIS invoice's own lines — the only ones the
	# self-hold exemption may lift (empty for a bogus / non-Draft name).
	own_serials = _current_invoice_own_serials(current_invoice, codes) if current_invoice else set()
	# A counter pickup's 4473 ticket (api/counter_pickup) carries a gun the
	# ORDER holds (kind='sales_invoice'), and that hold outranks the ticket's
	# own POS hold — so the ticket's picker must also lift its order's hold,
	# and only that order's.
	counter_order = None
	if current_invoice and frappe.db.has_column("POS Invoice", "ffl_counter_order"):
		counter_order = frappe.db.get_value("POS Invoice", current_invoice, "ffl_counter_order")
	excluded: set[str] = set()
	for code in codes:
		for serial, hold in counter_holds(code).items():
			# Self-hold exemption: lift ONLY a serial that is on this invoice's own
			# line AND whose winning reservation is this invoice's own POS hold —
			# or its own counter order's.
			if serial in own_serials and (
					(hold.get("kind") == "pos" and hold.get("ref") == current_invoice)
					or (counter_order and hold.get("kind") == "sales_invoice"
						and hold.get("ref") == counter_order)):
				continue
			excluded.add(serial)
		# Rule 5. NOT exempted by current_invoice: that exemption exists for a
		# gun parked on the POS invoice being edited, which is a hold this same
		# counter placed. A channel reservation is somebody else's paid order on
		# another storefront — no POS invoice can be its rightful owner, so
		# lifting it for "my own" invoice would be exactly the double sale.
		excluded.update(channel_reserved_serials(code))
	consignment_whs = consignment_warehouse_names()
	custody_whs = custody_warehouse_names() if exclude_custody else set()
	# has_column guard: during a deploy the app code is live before
	# `bench migrate` has created this column, and every POS picker render runs
	# through here — selecting a missing column would 500 the counter for the
	# length of that window. Same posture warehouses.custody_warehouse_names
	# already takes; the two must not disagree about the risk.
	fields = ["name", "warehouse", "consigned_out"]
	has_flag = frappe.db.has_column("Serial No", "is_transfer_gun")
	if has_flag:
		fields.append("is_transfer_gun")
	for sn in frappe.get_all(
		"Serial No",
		filters={"item_code": ("in", codes), "status": "Active"},
		fields=fields,
	):
		if sn.consigned_out or sn.warehouse in consignment_whs:
			excluded.add(sn.name)
		elif exclude_custody and (sn.is_transfer_gun or sn.warehouse in custody_whs):
			excluded.add(sn.name)
	return excluded


@frappe.whitelist()
def available_serials_for_items(item_codes: list[str] | str,
		exclude_unavailable: int | bool = True,
		current_invoice: str | None = None) -> dict[str, list[str]]:
	"""Return {item_code: [in-stock serial, ...]} so the POS 4473 dialog can offer
	a pick-list of real on-hand serials instead of free-typing one.

	Every firearm has has_serial_no=1 and its serials live in the Serial No
	table — Item.firearm_serial fallback was removed when the Used Firearm
	path was unified. Codes with nothing on hand map to [] so the caller can
	warn + block the sale.

	Args:
	    item_codes: Item codes — list or JSON string.
	    exclude_unavailable: Default True: drop guns that are
	        held / consigned_out / in a consignment warehouse so the picker
	        never offers a gun that can't actually be sold. Pass False for
	        a caller that needs the raw on-hand list (never the POS
	        pick-list itself).
	    current_invoice: Optional POS Invoice docname — the serial already
	        parked on THIS invoice's own line stays offered (incident
	        a past invoice, see _pos_picker_excluded_serials). The
	        POS 4473 dialog passes it so the serial on the line the cashier
	        is completing doesn't vanish from its own dropdown.

	Returns:
	    {item_code: [in-stock serial, ...]}.
	"""
	frappe.only_for(COUNTER_ROLES)
	codes = list({c for c in _coerce_item_codes(item_codes) if c})
	if not codes:
		return {}
	excluded = _pos_picker_excluded_serials(codes, current_invoice=current_invoice) \
		if frappe.utils.cint(exclude_unavailable) else set()

	by_code: dict[str, list[str]] = {c: [] for c in codes}
	for row in frappe.get_all(
		"Serial No",
		filters={"item_code": ("in", codes), "status": "Active"},
		fields=["name", "item_code"],
		order_by="creation",
	):
		if row.name in excluded:
			continue
		by_code[row.item_code].append(row.name)
	return by_code


# ==================================================== Service ToDo queue

def _ensure_service_todo(serial: str) -> None:
	"""Create an Open service ToDo for `serial` if one doesn't already exist.

	Idempotent — multiple receives / form-edits won't pile up duplicate ToDos.
	The ToDo references the Serial No so clicking it opens the gun's record.
	v1 leaves the ToDo unassigned — gunsmiths claim from /app/todo themselves.
	"""
	if frappe.db.exists("ToDo", {
		"reference_type": "Serial No",
		"reference_name": serial,
		"status": "Open",
	}):
		return
	item_code = frappe.db.get_value("Serial No", serial, "item_code")
	item_label = (
		frappe.db.get_value("Item", item_code, "item_name") if item_code else None
	) or item_code or serial
	todo = frappe.get_doc({
		"doctype": "ToDo",
		"description": f"<b>Service needed</b>: {frappe.utils.escape_html(serial)} "
			f"({frappe.utils.escape_html(item_label)})",
		"reference_type": "Serial No",
		"reference_name": serial,
		"status": "Open",
		"priority": "Medium",
	})
	todo.flags.ignore_permissions = True
	todo.insert()


def _close_service_todos(serial: str) -> None:
	"""Close every Open service ToDo for `serial` — fires when the gunsmith
	clears the needs_service flag via the pill or the Serial No form."""
	names = frappe.get_all("ToDo", filters={
		"reference_type": "Serial No",
		"reference_name": serial,
		"status": "Open",
	}, pluck="name")
	for name in names:
		frappe.db.set_value("ToDo", name, "status", "Closed", update_modified=False)


@frappe.whitelist()
def toggle_serial_service_need(serial: str, value: int | str) -> dict:
	"""Flip Serial No.needs_service AND keep the gunsmith ToDo queue in sync.

	Used by the Service Needed pill on the Item form's Serial Numbers tab.
	Goes through this single helper (instead of raw db.set_value on the client)
	so the ToDo create/close stays glued to the flag — toggling on creates a
	ToDo, toggling off closes the matching Open ones.

	Permission gate: the helpers below elevate via flags.ignore_permissions /
	raw db.set_value to bypass ToDo/Serial No DocPerms (managers vs cashiers).
	Without this gate, any logged-in user could flip needs_service on any
	serial — including silently closing a gunsmith's Open ToDo queue. Restrict
	to roles that already own Serial No writes.

	Args:
	    serial: Serial No name.
	    value: Truthy to flag needs_service (creates a gunsmith ToDo);
	        falsy to clear it (closes matching Open ToDos).

	Returns:
	    {"ok": True, "needs_service": 0|1}.

	Permissions:
	    Frappe write permission on Serial No (a DocType permission check,
	    not a fixed role list — whichever roles carry Serial No write).
	"""
	frappe.has_permission("Serial No", "write", throw=True)
	# Force a string. A client caller can hand us a non-string serial: jQuery's
	# .data('serial') coerces a numeric-looking Serial No name (e.g. "1393829")
	# to a JS Number, which arrives here as an int. frappe.db.exists/get_value
	# would then compare an integer against the varchar `name` column, making
	# MySQL coerce every row and error (1292 Truncated incorrect DECIMAL value)
	# the moment it meets a non-numeric serial like "F01086" in the same table.
	serial = str(serial)
	# Existence first — has_permission(..., doc=<name>) lazily resolves the doc
	# and would otherwise raise DoesNotExistError from deep inside the
	# permission machinery on a missing serial (e.g. a stale browser cache
	# clicking a pill for a deleted row).
	if not frappe.db.exists("Serial No", serial):
		frappe.throw(frappe._("Serial No {0} not found.").format(serial))
	if not frappe.has_permission("Serial No", "write", doc=serial):
		frappe.throw(
			frappe._("Not permitted to change Service Needed on Serial No {0}.").format(serial),
			frappe.PermissionError,
		)
	# `cint` accepts None, '', 'false', integers — won't crash on non-numeric
	# strings from a misbehaving client the way `int(value or 0)` would.
	value = 1 if frappe.utils.cint(value) else 0
	frappe.db.set_value("Serial No", serial, "needs_service", value, update_modified=False)
	if value:
		_ensure_service_todo(serial)
	else:
		_close_service_todos(serial)
	return {"ok": True, "needs_service": value}


def manage_service_todo(doc, method: str | None = None) -> None:
	"""Serial No on_update hook — catches direct form/API edits that bypass the
	pill toggle. Mirrors toggle_serial_service_need's create-on-set,
	close-on-clear behaviour.

	Skip during insert: a brand-new Serial No carries the default needs_service
	value (0); has_value_changed semantics differ across Frappe versions on
	inserts, and we don't want bulk imports of historical Serial Nos to spam
	ToDos or burn an extra get_all per inserted row. The PR-submit path calls
	_ensure_service_todo explicitly after stamping needs_service=1, so the
	hook only needs to track AFTER-insert toggles."""
	if doc.is_new():
		return
	if not doc.has_value_changed("needs_service"):
		return
	if doc.needs_service:
		_ensure_service_todo(doc.name)
	else:
		_close_service_todos(doc.name)


# The per-unit firearm facts that live on the Serial No (the bound book's source
# of truth). ERPNext bulk-inserts serials on Purchase Receipt / Stock Entry
# submit PAST the inherit_serial_firearm_attrs before_save hook, so every receive
# path must stamp these explicitly after the receipt — keep the field set here,
# in ONE place, so a new per-unit field can't be added to one path and missed in
# another (the exact class of bug that left received serials blank).
SERIAL_FIREARM_ATTRS = ("manufacturer", "importer", "model_name", "caliber", "firearm_type")

# The numeric lane of the same set. year_made can't ride the tuple above — its
# consumers .strip() every member — but it is just as much a per-unit fact, and
# splitting it off into some other module is how the "added to one path, missed
# in another" bug above happens. Same list, one line down.
SERIAL_FIREARM_INT_ATTRS = ("year_made",)


def sane_year_made(value) -> int | None:
	"""A year of manufacture, or None if the value can't be one.

	The one home for this test (2026-07-24): the year is what a GCA-exempt
	antique claim rests on, and since the Antique checkbox alone now decides
	whether a gun skips the bound book, nothing downstream will ever question
	the number. Blank / 0 / garbage all collapse to None, which every reader
	takes as "no year on record".

	The window is wide on purpose — firearms predate the republic, and a brand
	new gun sometimes carries next year's stamp. This rejects typos, not claims,
	and it never raises: an unusable year is dropped, never a reason to refuse a
	gun at the door."""
	year = frappe.utils.cint(value or 0)
	if year < 1000 or year > frappe.utils.now_datetime().year + 1:
		return None
	return year


def serial_firearm_attr_updates(src) -> dict:
	"""The non-blank SERIAL_FIREARM_ATTRS (+ the Int ones) pulled from a receive
	source (a Receive Goods line dict / intake payload), ready for
	frappe.db.set_value onto the Serial No. Blank values are skipped so a missing
	field never clobbers one the unit already carries."""
	updates: dict[str, str | int] = {}
	for f in SERIAL_FIREARM_ATTRS:
		v = (src.get(f) or "").strip()
		if v:
			updates[f] = v
	for f in SERIAL_FIREARM_INT_ATTRS:
		year = sane_year_made(src.get(f))
		if year:
			updates[f] = year
	return updates


def inherit_serial_firearm_attrs(doc, method: str | None = None) -> None:
	"""Serial No before_save: a firearm's manufacturer, model + caliber are
	per-unit facts that belong on the Serial No — the Firearms In Stock report and the
	FastBound bound-book push both read them there. When a serial doesn't carry its
	own (e.g. received before this rule, or under a model Item that has them),
	inherit them from the model Item, so the by-serial form shows the same values the
	report does (the report COALESCEs a blank serial field to the Item). An explicit
	per-serial value is never overwritten.

	`importer` is deliberately NOT inherited (2026-08-20). Generic surplus
	buckets (98-8MM and friends) mix import batches under one SKU, and receive
	once seeded the first unit's marking onto the Item — so an Item-level
	importer can be one gun's marking masquerading as the model's. Nothing
	treats it as a per-gun default any more: an importer is stated per physical
	gun at receive (or on the serial directly), or it stays blank.

	`year_made` is deliberately NOT in this list either, though it IS a per-unit
	serial field (SERIAL_FIREARM_INT_ATTRS). Manufacturer / model / caliber / type
	are the same on every unit of a model, so inheriting them fills in a blank with
	a fact. A year is not: two guns of one model can be decades apart, and on an
	antique the year is the provenance behind skipping the bound book. Copying
	the model's year onto a gun nobody recorded a year for would manufacture
	exactly the evidence an inspection is asking about. A blank year stays blank.
	"""
	need_mfg = not (doc.get("manufacturer") or "").strip()
	need_model = not (doc.get("model_name") or "").strip()
	need_cal = not (doc.get("caliber") or "").strip()
	need_type = not (doc.get("firearm_type") or "").strip()
	if not (need_mfg or need_model or need_cal or need_type) or not doc.get("item_code"):
		return  # serial already carries them all — nothing to inherit
	# One cached read covers the firearm check AND the values to inherit (the
	# import path materializes a blank serial, so this is on a hot loop).
	item = frappe.db.get_value(
		"Item", doc.item_code,
		["is_firearm", "item_group", "manufacturer", "model_name", "caliber", "firearm_type"],
		as_dict=True, cache=True,
	)
	if not _is_firearm_row(item):
		return
	if need_mfg and item.manufacturer:
		doc.manufacturer = item.manufacturer
	if need_model and item.model_name:
		doc.model_name = item.model_name
	if need_cal and item.caliber:
		doc.caliber = item.caliber
	if need_type and item.firearm_type:
		doc.firearm_type = item.firearm_type


def _serial_sell_price(uniform_sell_price, standard_rate, sell_price) -> float | None:
	"""Resolve a Serial No's display sell price: uniform-priced items use the
	Item's standard_rate, others the per-serial tag. 0/NULL → None (un-priced):
	Serial No.sell_price is a NOT-NULL Currency column, so a gun received without
	a tag price reads 0, which the POS picker shows as un-priced and sorts last.
	Shared by both serial pickers so the pricing rule lives in one place.
	"""
	price = float(standard_rate or 0) if uniform_sell_price else sell_price
	return price or None


# Public alias — lets cross-app callers (e.g. ffl_woo_sync.woocommerce.mapping)
# import the pricing rule without depending on a private name.
serial_sell_price = _serial_sell_price  # stable cross-app alias (see ffl_woo_sync.woocommerce.mapping)


def effective_serial_price(serial_no: str) -> float:
	"""The price ANY sales channel would actually charge for this gun.

	Pricing is a TWO-field rule (``_serial_sell_price`` above): a uniform-priced
	Item prices every one of its serials from ``Item.standard_rate``, everything
	else uses the per-serial tag. Reading ``Serial No.sell_price`` alone gets it
	wrong in both directions — it blocks a uniform-priced gun whose own field is
	(correctly) 0, and it passes a gun tagged $900 whose uniform parent reads 0
	and would therefore list at $0.

	Returns 0.0 when unpriced, so every channel's un-priced guard is the same
	``<= 0`` test. Lives in ffl_core rather than in a channel: this used to be
	``ffl_woo_sync.woocommerce.sync.effective_serial_price``, and GunBroker's
	``FixedPrice`` has to be the same number the storefront and the counter would
	charge. Two copies of a pricing rule is how a gun ends up listed at two
	different prices on two channels (the 2026-07 mispricing incident is the
	house precedent).

	Args:
	    serial_no: The Serial No to price.

	Returns:
	    The effective sell price as a float, or 0.0 when the gun is un-priced.
	"""
	sn = frappe.db.get_value(
		"Serial No", serial_no, ["item_code", "sell_price"], as_dict=True
	) or frappe._dict()
	# A blank item_code must NOT reach get_value: Frappe reads a falsy docname as
	# "no filter" rather than "no such row", so it would resolve some arbitrary
	# Item and price this gun from a SKU it has nothing to do with. It also threw
	# inside the Woo reconcile sweep, where the per-serial `except Exception`
	# swallowed it into an error string with no message at all (`'SN-001: '`) —
	# invisible in the logs and skipping the serial silently.
	item_code = sn.get("item_code")
	item = (frappe.db.get_value(
		"Item", item_code, ["uniform_sell_price", "standard_rate"], as_dict=True
	) if item_code else None) or frappe._dict()
	# No item_code → no way to resolve a price → fail closed (0), same as any
	# other un-priced gun. Never fall through to "publish it anyway".
	return float(_serial_sell_price(
		item.get("uniform_sell_price"), item.get("standard_rate"), sn.get("sell_price"),
	) or 0)


def serial_price_now(serial_no: str, on=None) -> float:
	"""What the counter charges for this gun today: :func:`effective_serial_price` with its sale
	running — the price the register's serial picker and Create Order put on the line
	(:func:`available_serials_with_prices`). 0.0 when the gun is un-priced or unknown."""
	sn = frappe.db.get_value("Serial No", serial_no,
		["item_code", "sell_price", "sale_sell_price", "sale_from", "sale_to"], as_dict=True)
	if not sn or not sn.item_code:
		return 0.0
	item = frappe.db.get_value("Item", sn.item_code,
		["uniform_sell_price", "standard_rate", "sale_price", "sale_from", "sale_to"], as_dict=True) or frappe._dict()
	regular = _serial_sell_price(item.get("uniform_sell_price"), item.get("standard_rate"), sn.sell_price)
	return float(_apply_sale(regular, *_serial_sale_fields(item.get("uniform_sell_price"), item.get("sale_price"),
		item.get("sale_from"), item.get("sale_to"), sn.sale_sell_price, sn.sale_from, sn.sale_to), on) or 0)


def _serial_dealer_price(uniform_sell_price, dealer_price, dealer_sell_price) -> float | None:
	"""Resolve a Serial No's dealer price: uniform-priced items use the Item's
	dealer_price; per-serial items use Serial No.dealer_sell_price. 0/NULL → None
	(un-priced; the caller decides what that means). Only the osa_consignment extension
	imports this (its partner-store price); no website reads it. Mirrors
	``_serial_sell_price``."""
	price = float(dealer_price or 0) if uniform_sell_price else dealer_sell_price
	return price or None


# Public alias — the osa_consignment extension imports this.
serial_dealer_price = _serial_dealer_price


def _sale_active(sale_price, sale_from, sale_to, on=None) -> bool:
	"""True when a promotional price is set and `on` (default: today, site TZ)
	falls inside its window. Blank dates are open-ended: no `sale_from` means
	the sale starts immediately, no `sale_to` means it runs until cleared —
	the same semantics WooCommerce gives the product's own sale schedule, so
	the counter and the storefront flip on the same calendar day."""
	if not sale_price or float(sale_price) <= 0:
		return False
	on = frappe.utils.getdate(on) if on else frappe.utils.getdate()
	if sale_from and frappe.utils.getdate(sale_from) > on:
		return False
	if sale_to and frappe.utils.getdate(sale_to) < on:
		return False
	return True


def _apply_sale(regular, sale_price, sale_from, sale_to, on=None):
	"""Overlay an active sale on a resolved regular price.

	The sale must undercut the regular price to apply — WooCommerce silently
	DROPS a sale >= regular on save (verified against WC REST), so honouring
	such a value here would make the counter charge a "sale" the storefront
	refuses to show. An un-priced gun (regular None) never gets a sale price:
	the promo is a discount off a price, not a price of its own."""
	if regular and _sale_active(sale_price, sale_from, sale_to, on) \
			and float(sale_price) < float(regular):
		return float(sale_price)
	return regular


def _serial_sale_fields(uniform_sell_price, item_sale_price, item_sale_from,
		item_sale_to, serial_sale_price, serial_sale_from, serial_sale_to):
	"""Resolve which sale triple (price, from, to) governs a serial — the
	same two-level rule as ``_serial_sell_price``: a uniform-priced Item
	promotes every serial from its own sale fields, everything else uses the
	per-serial tag. Keeping the rule shape identical means a gun can never
	take its regular price from one level and its sale from the other."""
	if uniform_sell_price:
		return item_sale_price or 0, item_sale_from, item_sale_to
	return serial_sale_price or 0, serial_sale_from, serial_sale_to


# Public aliases — cross-app callers (ffl_woo_sync.woocommerce.mapping /
# tasks, ffl_core.item_hooks) import the sale rule the same way they import
# serial_sell_price.
sale_active = _sale_active
apply_sale = _apply_sale
serial_sale_fields = _serial_sale_fields


def current_price(regular, sale_price, sale_from, sale_to, on=None) -> float:
	"""What a uniform-priced Item charges today: its sale price while the sale
	window is open and undercuts the regular price, otherwise the regular one.

	The one rule behind the Item Price the register charges
	(``item_hooks.sync_standard_selling_price``) and the price GunBroker's model
	listing carries (``gunbroker.mapping``) — written once so a promotion starts
	and ends on the same day on every channel.
	"""
	return float(_apply_sale(regular, sale_price, sale_from, sale_to, on) or 0) \
		or float(regular or 0)


def item_current_price(item_code: str, on=None, *, row=None) -> float:
	""":func:`current_price` of an Item, read from its own columns.

	Args:
	    item_code: The Item.
	    on: The day to price for (default: today, site time zone).
	    row: Anything carrying ``standard_rate`` / ``sale_price`` / ``sale_from``
	        / ``sale_to`` (an Item document, or a row already read), to price a
	        value that is not in the database yet or save the read.

	Returns:
	    The price, or 0.0 when the Item is unpriced or missing.
	"""
	if row is None:
		row = frappe.db.get_value(
			"Item", item_code, ["standard_rate", "sale_price", "sale_from", "sale_to"],
			as_dict=True) or {}
	return current_price(
		row.get("standard_rate"), row.get("sale_price"), row.get("sale_from"),
		row.get("sale_to"), on)


def validate_item_sale_price(doc, method: str | None = None) -> None:
	"""Item validate: refuse a sale price the storefront would silently drop.

	WooCommerce discards a sale >= regular on save (verified against WC REST),
	so letting one through here would push a promo the shop never shows while
	the POS picker (same guard in _apply_sale) quietly ignores it too —
	better to refuse loudly at the source."""
	flt = frappe.utils.flt
	if doc.get("sale_from") and doc.get("sale_to") \
			and frappe.utils.getdate(doc.get("sale_to")) < frappe.utils.getdate(doc.get("sale_from")):
		frappe.throw(
			frappe._("Sale To cannot be before Sale From."),
			title=frappe._("Invalid Sale Window"))
	sale = flt(doc.get("sale_price"))
	if sale <= 0:
		return
	if _is_firearm_row(doc) and not doc.get("uniform_sell_price"):
		frappe.msgprint(
			frappe._(
				"This is a per-serial firearm: the Item-level sale price is ignored — "
				"set the sale on each Serial No instead."),
			indicator="orange", alert=True)
		return
	rate = flt(doc.get("standard_rate"))
	if not rate or sale >= rate:
		frappe.throw(
			frappe._(
				"Sale Price {0} must be below the regular price {1} — WooCommerce "
				"silently ignores a sale at or above the regular price."
			).format(frappe.utils.fmt_money(sale), frappe.utils.fmt_money(rate)),
			title=frappe._("Invalid Sale Price"))
	# A drop-ship listing shows its own website price, not this tag
	# (`distributor_web_price`, ffl_integrations): the sale must clear that too, or
	# the store would take it and the website silently drop it.
	web = None
	if doc.name and not doc.is_new():
		for fn in frappe.get_hooks("distributor_web_price"):
			web = frappe.get_attr(fn)(doc.name)
			if web is not None:
				break
	if web is not None and sale >= flt(web):
		frappe.throw(
			frappe._(
				"Sale Price {0} must be below the website price {1} — this is a "
				"drop-ship listing, priced online by the Drop-ship Center markup rules, "
				"and WooCommerce ignores a sale at or above it."
			).format(frappe.utils.fmt_money(sale), frappe.utils.fmt_money(web)),
			title=frappe._("Invalid Sale Price"))


def validate_serial_sale_price(doc, method: str | None = None) -> None:
	"""Serial No validate: same guard as the Item side, against the serial's
	own regular tag (per-serial pricing) — or a warning when the parent is
	uniform-priced, whose serials ignore their own tags entirely."""
	flt = frappe.utils.flt
	if doc.get("sale_from") and doc.get("sale_to") \
			and frappe.utils.getdate(doc.get("sale_to")) < frappe.utils.getdate(doc.get("sale_from")):
		frappe.throw(
			frappe._("Sale To cannot be before Sale From."),
			title=frappe._("Invalid Sale Window"))
	sale = flt(doc.get("sale_sell_price"))
	if sale <= 0:
		return
	if doc.get("item_code") and frappe.db.get_value("Item", doc.item_code, "uniform_sell_price"):
		frappe.msgprint(
			frappe._(
				"The parent Item is uniform-priced, so this serial's sale price is "
				"ignored — set the sale on the Item instead."),
			indicator="orange", alert=True)
		return
	rate = flt(doc.get("sell_price"))
	if not rate or sale >= rate:
		frappe.throw(
			frappe._(
				"Sale Price {0} must be below the serial's regular Sell Price {1} — "
				"WooCommerce silently ignores a sale at or above the regular price."
			).format(frappe.utils.fmt_money(sale), frappe.utils.fmt_money(rate)),
			title=frappe._("Invalid Sale Price"))


def is_transfer_intake(acquisition_type, transfer_for_customer,
		is_ffl_transfer=None) -> bool:
	"""True when this receive is a CUSTOMER's gun passing through, not stock we bought.

	A transfer gun's numbers describe one gun's paperwork and nothing else: staff
	must enter a cost and a price to get the receive through, so those boxes get
	placeholders.  On 2026-07-19 a $10 / $35 placeholder pair reached a
	uniform-priced SKU and put four $589.68 pistols on WooCommerce at $35 —
	hence the callers that stamp SKU-LEVEL price/cost consult this first.

	Since 2026-07-20 this answer decides much more than whether a price may
	propagate: it routes the gun to the custody warehouse, zeroes its cost, and
	flags its serial out of every sales channel.  So a THIRD, explicit marker
	was added — and it, not the inferred pair, is what the UI writes:

	* ``is_ffl_transfer`` — the Receive Goods "FFL Transfer" checkbox, and
	  the field stamped on the Purchase Receipt.  An unambiguous declaration.
	* ``transfer_for_customer`` — the "transfer for" link.  Note this names who
	  PICKS THE GUN UP; it is a contact, not by itself a declaration that the
	  receive is a transfer (see assert_transfer_declaration_unambiguous).
	* ``acquisition_type`` — free-text Data mirroring FastBound's vocabulary, so
	  it is matched case- and whitespace-insensitively and is the only marker an
	  API/MCP caller may send.

	Any marker is sufficient, deliberately: this predicate fails CLOSED.  Reading
	a transfer as an ordinary purchase is the incident; reading a purchase as a
	transfer is a visible, correctable mistake.  The ambiguous middle — a pickup
	contact with no declaration at all — never reaches here, because the receive
	path refuses it up front rather than guessing either way.

	Kept next to the pricing helpers so the rule that decides whether a price may
	propagate lives beside the rules that resolve prices.
	"""
	if is_ffl_transfer:
		return True
	if transfer_for_customer:
		return True
	return is_transfer_acquisition_type(acquisition_type)


# The A&D vocabulary values that mean "a customer's gun came in on a transfer".
# **"FFL Transfer" is what production actually stores** — verified against the
# live bound book on 2026-07-20 — so the original `== "transfer"` equality never
# matched there: every transfer this system correctly identified was caught by
# the transfer_for_customer marker instead, and the acquisition_type arm of the
# price guard was quietly dead on the only site that matters. Nothing was
# lost to it (both historical transfers named a pickup customer), but a guard
# that cannot fire is worse than no guard, because it reads like cover.
#
# A fixed set rather than a substring test, deliberately: matching anything
# merely CONTAINING "transfer" would hand zero-cost custody routing — a
# consequential, hard-to-notice reroute — to acquisition types nobody has
# audited. New spellings get added here, on purpose, with a test.
TRANSFER_ACQUISITION_TYPES = frozenset({"transfer", "ffl transfer"})


def is_transfer_acquisition_type(acquisition_type) -> bool:
	"""Case- and whitespace-insensitive membership in the transfer vocabulary.

	Public because three modules share this one contract (this classifier, the
	ambiguity refusal, and custody's legacy detector). It answers only "does
	this AcquireType READ as a transfer?" — deliberately not "may this receive
	be routed into custody", which is a far more destructive question and takes
	the explicit checkbox alone. See ``create_receive``.
	"""
	return (acquisition_type or "").strip().lower() in TRANSFER_ACQUISITION_TYPES


# The A&D vocabulary values that mean "this gun belongs to a consignor, not to
# us". Same shape and same reason as TRANSFER_ACQUISITION_TYPES above: the value
# is free-text Data mirroring FastBound's SmartList (the Receive Goods dropdown
# is populated at runtime from `get_acquire_types`), so an `== "Consignment"`
# equality is a guard that can stop firing the day the spelling changes — and
# this one decides whether a $0 line may be received AT ALL (zero_cost guard,
# 2026-08-08) as well as whether the consignor + commission % get stamped onto
# the serial. The two consumers MUST agree, which is why they share this
# predicate instead of each carrying a literal.
#
# One spelling, because one is what exists: prod's two consignment receipts both
# store "Consignment" exactly. A fixed set rather than a substring test, for the
# same reason as the transfer set — "Consignment Out" is a DISPOSITION of our own
# stock and must never read as "a consignor owns this". New spellings get added
# here, on purpose, with a test.
CONSIGNMENT_ACQUISITION_TYPES = frozenset({"consignment"})


def is_consignment_acquisition_type(acquisition_type) -> bool:
	"""Case- and whitespace-insensitive membership in the consignment vocabulary.

	Public because two modules share this one contract: the receive API (which
	exempts a consignor's legitimately-$0 line from the zero-cost guard) and the
	Purchase Receipt on_submit stamps (consignor + commission %). A spelling that
	one recognises and the other does not would receive a gun at $0 and then fail
	to record whose gun it is.
	"""
	return (acquisition_type or "").strip().lower() in CONSIGNMENT_ACQUISITION_TYPES


def log_transfer_type_near_miss(acquisition_type) -> None:
	"""Record an AcquireType that LOOKS like a transfer but is not in the
	vocabulary — the signal the original dead arm could have raised about
	itself for months and never did.

	Throttled to once an hour per spelling (the same device
	``warehouses._log_heuristic_fallback`` uses): a mis-spelled AcquireType is a
	standing configuration fact, not a transient failure, so it must be visible
	without turning one bad dropdown value into thousands of identical rows.

	Never raises: this is a diagnostic on the receive hot path, and a broken
	logger must not be able to block goods coming in the door."""
	value = (acquisition_type or "").strip()
	if not value or is_transfer_acquisition_type(value):
		return
	if "transfer" not in value.lower():
		return
	try:
		key = f"ffl_core:transfer_type_near_miss:{value.lower()}"
		if frappe.cache().get_value(key):
			return
		frappe.cache().set_value(key, 1, expires_in_sec=3600)
		frappe.log_error(
			title="Receive: AcquireType looks like a transfer but is not recognised",
			message=(
				f"Acquisition type {value!r} contains \"transfer\" but is not in "
				f"the recognised vocabulary {sorted(TRANSFER_ACQUISITION_TYPES)!r}, so "
				"the transfer price-suppression guard did NOT apply to this receive.\n\n"
				"If this spelling means a customer transfer, add it to "
				"ffl_core.firearm.TRANSFER_ACQUISITION_TYPES (with a test). If it "
				"means an ordinary purchase, no action is needed.\n\n"
				"Note: recognising a spelling here does NOT route receives into "
				"custody — only the Receive Goods \"FFL Transfer\" checkbox does "
				"that. (This log is throttled to once an hour per spelling.)"
			),
		)
	except Exception:
		pass


def assert_transfer_declaration_unambiguous(acquisition_type,
		transfer_for_customer, is_ffl_transfer) -> None:
	"""Refuse a receive that names a pickup customer without ever declaring
	itself a transfer.

	``transfer_for_customer`` answers "who collects this gun?", which only makes
	sense on a transfer — but on its own it is an inference, and inference is now
	expensive in both directions: guessing "transfer" reroutes the warehouse and
	zeroes the cost of what might be a real purchase, while guessing "purchase"
	is that incident again.  So the ambiguous case is refused with an instruction
	instead of silently resolved.

	NOT refused (a deliberate narrowing of the literal requirement):
	``acquisition_type="Transfer"`` alongside the pickup contact.  That IS a
	declaration — it is the marker an API/MCP caller sends, being the only one
	they can send — so there is no ambiguity left to resolve, and refusing it
	would break the MCP receive tool for every transfer without making anything
	safer.  The checkbox remains the single source of truth for everything the
	UI writes; this function is what guarantees the two can never disagree
	silently.
	"""
	if not transfer_for_customer:
		return
	if is_ffl_transfer or is_transfer_acquisition_type(acquisition_type):
		return
	frappe.throw(frappe._(
		"This receive names '{0}' as the customer picking the gun up, but is not "
		"marked as a transfer. Tick <b>FFL Transfer</b> if this is a customer's "
		"gun passing through (it will be received into custody at zero cost), or "
		"clear the Transfer for Customer field if this is stock you bought."
	).format(transfer_for_customer))


@frappe.whitelist()
def available_serials_with_prices(item_codes: list[str] | str,
		exclude_unavailable: int | bool = True,
		current_invoice: str | None = None) -> dict[str, list[dict]]:
	"""Like available_serials_for_items but enriches each serial with sell_price
	and purchase_rate (cost), sorted by sell_price ascending so the POS picks
	the cheapest in-stock unit by default. Used by the POS Item Details serial
	picker to render a price-first dropdown.

	Args:
	    item_codes: Item codes — list or JSON string.
	    exclude_unavailable: Default True: drop held /
	        consigned_out / consignment-warehouse guns so the POS picker
	        never offers an unsellable one. Callers that need the RAW list
	        pass False — the scan-to-add / edit price lookup
	        (_serial_ctx_for_item), the checkout-gate summary
	        (pos_pricing_summary) and the Create Order picker
	        (manual_order.available_serials), none of which is the POS
	        pick-list and each of which must keep seeing every on-hand
	        serial (e.g. a serial held only by the very invoice being
	        edited).
	    current_invoice: Optional POS Invoice docname — the serial already
	        parked on THIS invoice's own line stays offered (incident
	        a past invoice); the Item-Details picker passes it so
	        editing a firearm line doesn't drop its own serial from the
	        dropdown.

	Returns:
	    {item_code: [{"serial", "sell_price", "purchase_rate"}, ...]},
	    sorted by sell_price ascending.
	"""
	frappe.only_for(COUNTER_ROLES)
	codes = list({c for c in _coerce_item_codes(item_codes) if c})
	if not codes:
		return {}
	excluded = _pos_picker_excluded_serials(codes, current_invoice=current_invoice) \
		if frappe.utils.cint(exclude_unavailable) else set()

	by_code: dict[str, list[dict]] = {c: [] for c in codes}
	for row in frappe.get_all(
		"Serial No",
		filters={"item_code": ("in", codes), "status": "Active"},
		fields=["name", "item_code", "sell_price", "purchase_rate",
			"sale_sell_price", "sale_from", "sale_to"],
		order_by="creation",
	):
		if row.name in excluded:
			continue
		by_code[row.item_code].append({
			"serial": row.name,
			# Raw tag price; _serial_sell_price normalises it (uniform mode + the
			# 0 → None un-priced rule) in the per-code pass below, once item meta loads.
			"sell_price": row.sell_price,
			"purchase_rate": row.purchase_rate,
			# Raw sale triple — consumed (and popped) by the same per-code pass:
			# an active sale replaces sell_price, so every consumer of this API
			# (POS picker rate, card range, checkout gate, Create Order) charges
			# the promo automatically without knowing sales exist.
			"_sale": (row.sale_sell_price, row.sale_from, row.sale_to),
		})
	# (The legacy has_serial_no=0 / Item.firearm_serial fallback was removed
	# when the Used Firearm path was unified — every firearm is serial-tracked.)
	# Uniform-pricing mode: when Item.uniform_sell_price=1, every serial uses
	# Item.standard_rate as its sell price (one number to change). Per-serial
	# Serial No.sell_price is ignored for those items.
	items_meta = {
		r.name: r for r in frappe.get_all(
			"Item",
			filters={"name": ("in", codes)},
			fields=["name", "uniform_sell_price", "standard_rate",
				"sale_price", "sale_from", "sale_to"],
		)
	}
	for code, serials in by_code.items():
		meta = items_meta.get(code) or {}
		for s in serials:
			regular = _serial_sell_price(
				meta.get("uniform_sell_price"), meta.get("standard_rate"), s["sell_price"])
			# Active-sale overlay (same two-level rule as the regular price):
			# this is THE spot that makes a sale real at the counter — every
			# POS surface prices from this normalised sell_price.
			serial_sale = s.pop("_sale")
			s["sell_price"] = _apply_sale(regular, *_serial_sale_fields(
				meta.get("uniform_sell_price"),
				meta.get("sale_price"), meta.get("sale_from"), meta.get("sale_to"),
				*serial_sale))
	# Sort each code's serials by sell_price asc; None goes last (un-priced).
	for code in by_code:
		by_code[code].sort(key=lambda s: (s["sell_price"] is None, s["sell_price"] or 0))
	return by_code


@frappe.whitelist()
def pos_pricing_summary(item_codes: list[str] | str) -> dict[str, dict]:
	"""Per-item POS pricing summary — the single data source for the POS card
	price range, the add-to-cart rate seed, the picker's auto-pick decision and
	the checkout gate.

	Args:
	    item_codes: Item codes — list or JSON string.

	Returns:
	    {code: {"is_firearm": bool, "uniform": 0|1, "n": <active serials>,
	    "min": float|None, "max": float|None}}. min/max range over the
	    normalised per-serial prices (uniform mode → standard_rate; 0 →
	    None un-priced, excluded from min/max). Non-firearm / unknown codes
	    come back with n=0 so the frontend can cache the negative.
	"""
	frappe.only_for(COUNTER_ROLES)
	if isinstance(item_codes, str):
		import json
		item_codes = json.loads(item_codes or "[]")
	codes = list({c for c in item_codes if c})
	if not codes:
		return {}

	items_meta = {
		r.name: r for r in frappe.get_all(
			"Item",
			filters={"name": ("in", codes)},
			fields=["name", "is_firearm", "item_group", "uniform_sell_price"],
		)
	}
	# exclude_unavailable=False: this drives the POS card range + checkout gate,
	# NOT the pick-list — it must keep counting held guns (B6 scopes the drop to
	# the three pickers, not the gate). A held gun is still blocked at scan-add
	# (_serial_status) and at the pick-list (available_serials_with_prices).
	serials = available_serials_with_prices(codes, exclude_unavailable=False)
	out: dict[str, dict] = {}
	for code in codes:
		meta = items_meta.get(code)
		rows = serials.get(code) or []
		priced = [s["sell_price"] for s in rows if s["sell_price"]]
		out[code] = {
			"is_firearm": _is_firearm_row(meta),
			"uniform": 1 if (meta and meta.get("uniform_sell_price")) else 0,
			"n": len(rows),
			"min": min(priced) if priced else None,
			"max": max(priced) if priced else None,
		}
	return out


@frappe.whitelist()
def get_transfer_config() -> dict:
	"""POS transfer-dialog config: the fee item + default per-firearm fee.

	Lazily creates the TRANSFER-FEE service item on first use — by the time the POS
	is live the site's UOM/Item-Group masters exist, unlike at install/migrate time
	(a fresh install runs hooks/patches before ERPNext's setup wizard). Mirrors
	receive_goods.get_receive_sources for reading FFL Settings.

	Returns:
	    {"fee_item": <service Item code>, "fee_amount": <default fee>}.
	"""
	frappe.only_for(COUNTER_ROLES)
	from ffl_core.setup.transfer_fee import TRANSFER_FEE_ITEM, ensure
	ensure()
	fee_item = frappe.db.get_single_value("FFL Settings", "transfer_fee_item") or TRANSFER_FEE_ITEM
	fee_amount = frappe.db.get_single_value("FFL Settings", "transfer_fee_amount") or 0
	return {"fee_item": fee_item, "fee_amount": float(fee_amount)}


def is_own_license(value: str | None) -> bool:
	"""True when `value` is THIS store's own FFL (``FFL Settings.own_ffl_license``).

	WooCommerce's FFL picker lists us alongside every other dealer, so a local
	buyer can pick the store as the receiving FFL (a web order, 2026-08-15).
	That order is a counter pickup, not a shipment: booking it as an FFL transfer
	would write "we transferred this gun to ourselves" into the bound book while
	the real disposition — a retail sale to a non-licensee, with a 4473 — went
	unrecorded.

	Lives here rather than in ``compliance.ffl_number`` because it reads Settings,
	and that module's first line states it is pure and does no I/O. The shape
	comparison still goes through its ``normalize``: the licence arrives
	hyphenated from WooCommerce and bare from FastBound.

	An unset setting matches NOTHING, blank input included — an empty-vs-empty
	match would sweep every order carrying no licence at all into the pickup lane.
	Until an operator fills the field in, every caller behaves exactly as before.

	Reads ``tabSingles`` directly (via ``utils.single_raw``) instead of
	``frappe.db.get_single_value``, which
	is NOT a style preference: get_single_value resolves the field's type through
	``frappe.get_meta("FFL Settings")``, and on a cold meta cache that loads the
	DocType through the ORM. This runs inside ``list_pending_web_orders`` — a
	function a dozen existing tests exercise with ``frappe.get_all`` /
	``frappe.get_doc`` patched — so those tests fed the mock's rows to Meta and
	blew up. Only on a FRESH site: a warm meta cache never reaches the ORM, which
	is exactly why a persistent dev database passed it and CI did not.
	"""
	from ffl_core.compliance.ffl_number import normalize

	from ffl_core.utils import single_raw

	configured = normalize(single_raw("FFL Settings", "own_ffl_license") or "")
	if not configured:
		return False
	return normalize(value) == configured


@frappe.whitelist()
def in_stock_firearm_serials(exclude_unavailable: int | bool = True) -> list[dict]:
	"""Every in-stock firearm serial, for the POS customer-transfer picker.

	The two cart-driven pickers (available_serials_for_items, firearm_items_in)
	need item_codes up front; a transfer starts before any gun is in the cart,
	so this returns the whole in-stock firearm set instead. Price is informational
	(picker label) — reuses the uniform/per-serial logic of
	available_serials_with_prices; the transfer always adds the gun at $0.

	Custody guns are deliberately NOT excluded here (exclude_custody=False),
	unlike in the two SALE pickers. This dialog is how a customer's gun goes
	home: hiding custody guns from it would strand every gun the custody
	warehouse receives, which is the opposite of the point.

	Args:
	    exclude_unavailable: Default True: drop held /
	        consigned_out / consignment-warehouse guns — a transfer must
	        not offer a gun already spoken for or gone to a dealer. Pass
	        False for the raw in-stock set.

	Returns:
	    [{serial, item_code, item_name, sell_price}], ordered item then
	    serial.
	"""
	frappe.only_for(COUNTER_ROLES)
	rows = frappe.db.sql(
		"""
		SELECT sn.name AS serial, sn.item_code, sn.sell_price,
		       sn.sale_sell_price, sn.sale_from, sn.sale_to,
		       i.item_name, i.uniform_sell_price, i.standard_rate,
		       i.sale_price AS item_sale_price,
		       i.sale_from AS item_sale_from, i.sale_to AS item_sale_to
		FROM `tabSerial No` sn
		INNER JOIN `tabItem` i ON i.name = sn.item_code AND i.is_firearm = 1 AND COALESCE(i.is_antique, 0) = 0
		WHERE sn.status = 'Active'
		ORDER BY i.item_name, sn.name
		""",
		as_dict=True,
	)
	excluded = _pos_picker_excluded_serials(
		{r.item_code for r in rows}, exclude_custody=False) \
		if frappe.utils.cint(exclude_unavailable) else set()
	out = []
	for r in rows:
		if r.serial in excluded:
			continue
		out.append({
			"serial": r.serial,
			"item_code": r.item_code,
			"item_name": r.item_name or r.item_code,
			# Informational only (transfers add at $0) — but show the price the
			# gun would actually sell for, active sale included.
			"sell_price": _apply_sale(
				_serial_sell_price(r.uniform_sell_price, r.standard_rate, r.sell_price),
				*_serial_sale_fields(
					r.uniform_sell_price,
					r.item_sale_price, r.item_sale_from, r.item_sale_to,
					r.sale_sell_price, r.sale_from, r.sale_to)),
		})
	return out


_TYPE_TO_GROUP = {
	# Bound-book firearm_type values (ATF_FIREARM_TYPES) → our type-based
	# Item Groups.
	# Pistol / Revolver map to their OWN leaves — both carry live storefront
	# stock (Woo product_cat Pistol=35, Revolver=5) and stay distinct so the
	# category filter shows them separately.
	"Pistol": PISTOL,
	"Revolver": REVOLVER,
	"Rifle": RIFLE,
	"Shotgun": SHOTGUN,
	"Receiver": RECEIVER,  # owner 2026-09-26: a receiver files under its own firearm group
	"Other Firearm": OTHER_FIREARM,
	# NFA classifications — firearm_type carries the bound-book ATF wording
	# (FastBound's outbound item-type allowlist, client._fb_item_type, passes
	# every ATF_FIREARM_TYPES value through unchanged), while the Item Group
	# leaves keep their short pre-existing names.
	"Silencer": "Suppressor",
	"Short Barreled Rifle": "SBR",
	"Short Barreled Shotgun": "SBS",
	"Machine Gun": "MG",
	"Destructive Device": "DD",
	"Any Other Weapon": "AOW",
	# Legacy aliases — items created before the ATF migration. Drop after
	# all DB rows are migrated. The dead "Handgun" / "Long Gun" leaves fold into
	# Pistol / Rifle so new intake never re-creates an empty Woo "Handgun" /
	# "Long Gun" category (recurrence source: sync._apply_category derives the Woo
	# category from item_group) — and neither leaf exists on a fresh site.
	"Handgun": PISTOL,
	"Long Gun": RIFLE,
	"Other": OTHER_FIREARM,
}


def used_firearm_item_doc(
	*,
	item_code: str,
	item_name: str,
	description: str,
	manufacturer: str | None = None,
	model_name: str | None = None,
	caliber: str | None = None,
	firearm_type: str | None = None,
	standard_rate: float | None = None,
	dealer_price: float | None = None,
	**extras,
) -> dict:
	"""Canonical Item-doc shape for a trade-in / used-intake firearm.

	A firearm's serial is its identity — no new/used distinction in ATF or
	FastBound terms. All firearm Items are has_serial_no=1; the caller must
	submit a Stock Entry / Purchase Receipt with the serial so a Serial No
	record is created (serial lives there, NOT on Item.firearm_serial).

	Item Group resolves to the type leaf (Pistol / Revolver / Rifle / Shotgun /
	Other Firearm; legacy "Handgun" / "Long Gun" fold into Pistol / Rifle) when firearm_type is
	provided; falls back to "Other Firearm" for old callers without firearm_type
	(the legacy "Used Firearm" group is retired — owner 2026-09-26). is_used is
	always set to 1 — that's what this helper is for.
	"""
	item_group = _TYPE_TO_GROUP.get((firearm_type or "").strip(), OTHER_FIREARM)
	return {
		"doctype": "Item",
		"item_code": item_code,
		"item_name": (item_name or item_code)[:140],
		"description": description,
		"item_group": item_group,
		"is_used": 1,
		"is_firearm": 1,
		"stock_uom": "Nos",
		"is_stock_item": 1,
		"has_serial_no": 1,
		"manufacturer": manufacturer or None,
		"model_name": model_name or None,
		"caliber": caliber or None,
		"firearm_type": firearm_type or None,
		"standard_rate": standard_rate,
		"dealer_price": dealer_price,
		"listing_status": "Listed",
		**extras,
	}


def serialized_receipt_row(*, item_code: str, qty: float, warehouse: str,
		basic_rate: float, serial: str | None = None) -> dict:
	"""Stock Entry Detail row for a serialized firearm Material Receipt.

	When ``serial`` is provided, ``use_serial_batch_fields=1`` tells ERPNext
	to read serial_no from the row directly (instead of expecting a Serial
	and Batch Bundle) and create the Serial No record on submit.
	"""
	row = {
		"item_code": item_code,
		"qty": qty,
		"t_warehouse": warehouse,
		"basic_rate": basic_rate,
	}
	if serial:
		row["use_serial_batch_fields"] = 1
		row["serial_no"] = serial
	return row


def issue_serial(item_code: str, serial: str, warehouse: str, *, remarks: str | None = None) -> str:
	"""Material Issue one serialized firearm out of `warehouse`; returns the Stock
	Entry name. The out-bound counterpart of serialized_receipt_row and the single
	home for "issue one gun out" — used by FFL Disposition stock issue, the FastBound
	reconcile, and reassign's first leg. Company is the warehouse's company (a Stock
	Entry's company must match its warehouse). `remarks` lets a caller stamp an audit
	note (e.g. the disposition that triggered it)."""
	company = frappe.db.get_value("Warehouse", warehouse, "company") if warehouse else None
	if not company:
		frappe.throw(
			frappe._("Cannot issue stock for {0}: warehouse {1} has no company.").format(serial, warehouse),
			title=frappe._("Warehouse Misconfigured"))
	from ffl_core.warehouses import custody_warehouse_names

	se = frappe.get_doc({
		"doctype": "Stock Entry",
		"stock_entry_type": "Material Issue",
		"company": company,
		"remarks": remarks,
		"items": [{
			"item_code": item_code,
			"qty": 1,
			"s_warehouse": warehouse,
			"use_serial_batch_fields": 1,
			"serial_no": serial,
			# A customer's gun in custody is held at zero cost by design; issuing it is not a missing valuation.
			"allow_zero_valuation_rate": 1 if warehouse in custody_warehouse_names() else 0,
		}],
	})
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	return se.name


def issue_stock_qty(item_code: str, qty: float, warehouse: str, *,
		remarks: str | None = None, expense_account: str | None = None) -> str:
	"""Material Issue a quantity of a NON-serialized item out of `warehouse`.

	The unserialised counterpart of :func:`issue_serial`, for stock that leaves
	on a sale with no numbered unit to name: ammunition, optics, parts. Written
	here beside its serialized twin so both stock-out shapes are read together —
	they share the company-from-warehouse rule and the ignore_permissions insert,
	and splitting them across modules is how the two drift.

	Refuses a serialized item: one of those must leave through
	:func:`issue_serial` (or a disposition) so the Serial No's own status
	follows the stock, which a bare quantity issue would not update.

	Args:
	    item_code: The non-serialized Item to issue.
	    qty: How many units. Must be positive.
	    warehouse: Where they leave from; its company owns the Stock Entry.
	    remarks: Audit note stamped on the entry.
	    expense_account: Where the cost is debited. **A sale must pass the
	        company's COGS account** — left unset, a Material Issue debits
	        `Company.stock_adjustment_account`, which is where shrinkage and
	        stocktake differences live, and the same box of ammunition would
	        then land in two different accounts depending on which channel sold
	        it. Unset is correct only for a genuine adjustment.

	Returns the Stock Entry name.
	"""
	item_code = (item_code or "").strip()
	qty = frappe.utils.flt(qty)
	if qty <= 0:
		frappe.throw(
			frappe._("Cannot issue {0}: quantity must be positive.").format(item_code),
			title=frappe._("Nothing To Issue"))
	if frappe.db.get_value("Item", item_code, "has_serial_no"):
		frappe.throw(
			frappe._("{0} is serialized — issue it by serial number, not by quantity."
				).format(item_code),
			title=frappe._("Serialized Item"))
	company = frappe.db.get_value("Warehouse", warehouse, "company") if warehouse else None
	if not company:
		frappe.throw(
			frappe._("Cannot issue stock for {0}: warehouse {1} has no company."
				).format(item_code, warehouse or "—"),
			title=frappe._("Warehouse Misconfigured"))
	se = frappe.get_doc({
		"doctype": "Stock Entry",
		"stock_entry_type": "Material Issue",
		"company": company,
		"remarks": remarks,
		"items": [{
			"item_code": item_code,
			"qty": qty,
			"s_warehouse": warehouse,
			# "Difference Account" on the row. Omitted entirely when the caller
			# named none, so the platform default still applies and this cannot
			# introduce a new way to fail.
			**({"expense_account": expense_account} if expense_account else {}),
		}],
	})
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	return se.name


def issue_antique_serial(serial: str, *, remarks: str | None = None) -> str | None:
	"""Take one ANTIQUE (GCA-exempt) gun out of stock with no bound-book entry.

	An antique is a firearm for inventory purposes and not for ATF purposes: no
	FFL Disposition exists to issue it, so a channel that sold one calls this
	instead of booking a transfer. The on-hand guard mirrors
	``disposition_stock.issue_stock_on_submit`` one for one — a serial that is no
	longer ``Active`` with a warehouse has already left — which is what makes a
	retried call a no-op rather than a second issue. Returns the Stock Entry
	name, or None when nothing was issued (already out, or no such serial).

	Refuses a regulated firearm outright: that one leaves stock through a
	disposition or not at all, and a caller reaching here with one has matched
	the wrong gun."""
	serial = (serial or "").strip()
	sn = frappe.db.get_value(
		"Serial No", serial, ["item_code", "warehouse", "status"], as_dict=True,
	) if serial else None
	if not sn:
		return None
	if not is_antique_item(sn.item_code):
		frappe.throw(
			frappe._("{0} is not an antique: a regulated firearm leaves stock through an "
				"FFL Disposition, never through this path.").format(serial),
			title=frappe._("Not an Antique"))
	if sn.status != "Active" or not sn.warehouse:
		return None
	return issue_serial(sn.item_code, serial, sn.warehouse, remarks=remarks)


def transfer_serial(item_code: str, serial: str, from_warehouse: str, to_warehouse: str, *,
		remarks: str | None = None) -> str:
	"""Material Transfer one serialized firearm from `from_warehouse` to
	`to_warehouse`; returns the Stock Entry name. The sibling of issue_serial
	for a move that keeps the firearm on OUR books — outbound-consignment
	ship (main warehouse -> per-dealer consignment warehouse) and return
	(consignment warehouse -> main warehouse) both use this, in opposite
	directions. Unlike issue_serial this never takes the gun off the books:
	the asset just relocates (Dr the destination warehouse's account / Cr the
	source's — net zero P&L, see ffl_core/warehouses.py and
	warehouses.stock_at_consignee_account). Company is the
	SOURCE warehouse's company; ERPNext itself rejects a Stock Entry whose
	two warehouses belong to different companies, so a cross-company call
	surfaces as ERPNext's own validation error, not a special case here."""
	company = frappe.db.get_value("Warehouse", from_warehouse, "company") if from_warehouse else None
	if not company:
		frappe.throw(
			frappe._("Cannot transfer stock for {0}: warehouse {1} has no company.").format(
				serial, from_warehouse),
			title=frappe._("Warehouse Misconfigured"))
	se = frappe.get_doc({
		"doctype": "Stock Entry",
		"stock_entry_type": "Material Transfer",
		"company": company,
		"remarks": remarks,
		"items": [{
			"item_code": item_code,
			"qty": 1,
			"s_warehouse": from_warehouse,
			"t_warehouse": to_warehouse,
			"use_serial_batch_fields": 1,
			"serial_no": serial,
		}],
	})
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	return se.name
