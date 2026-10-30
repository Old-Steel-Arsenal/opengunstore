"""POS Trade-in — single entry point for individual trade-ins.

Trade-ins are individual-only (per ATF — store buying a firearm from a
private party is a Form 4473-style record). Trade-ins from FFL or
distributors go through Receive Goods, not this endpoint.

A trade-in applied as credit wraps `ffl_core.api.firearm_intake.create_intake`
with source_type="Trade-in" and defer_booking (Item + DRAFT FFL Acquisition +
masters; the stock receipt waits for checkout), or refreshes an earlier draft
for the same serial (_refresh_pending_draft). A pay-now buy is received like
any purchase from a private seller, on a Purchase Receipt paid with it
(_buy_now -> receive_goods._create_receive).

Booking timing: with apply_credit the gun is
part of a sale that hasn't completed yet — the intake parks a DRAFT
acquisition (defer_booking) and the bound-book entry / stock receipt /
FastBound push all happen at POS Invoice submit
(compliance/trade_in_credit.book_trade_ins_on_submit). A pay-now buy (no
credit) books immediately: the store paid and took the gun.

Accessories and ammo (non-serialized stock: no bound book, no serial) ride the
same dialog, alone or with a gun, as `accessories` lines of item + qty + unit
cost. With credit they are parked as a DRAFT Material Receipt marked as a
trade-in (_park_accessories) and get a credit line of their own, booked at
checkout like the gun; a pay-now buy puts them on the gun's Purchase Receipt.
"""
from __future__ import annotations

import json as _json
import re as _re

import frappe
from frappe import _
from frappe.utils import flt

from ffl_core.acquisition import TRADE_IN_SOURCE
from ffl_core.api._helpers import coerce_payload
from ffl_core.cash_drawer import PAYOUT_METHODS
from ffl_core.customer_name import split_display_name
from ffl_core.firearm import ACCESSORY, AMMUNITION, PARTS, _is_firearm_row, is_per_serial_priced
from ffl_core.roles import COUNTER_ROLES, STOCK_ROLES, TRADE_IN_ROLES
from ffl_core.api.firearm_intake import _check_serial_free, create_intake


# Fields the POS dialog collects. Mapped onto firearm_intake's schema below.
# retail_price is optional — operator usually doesn't price at intake.
# Seller identity is structured (first/last [+ middle/suffix]) and the address
# is REQUIRED: ATF 478.125(e) wants the non-licensee transferor's name AND
# address in the bound book — the old free-text-name-only dialog shipped
# "Unknown - please edit in FastBound" placeholder addresses. The address
# itself is validated separately (_resolve_source_address): the dialog sends
# SPLIT fields (street / city / state / ZIP), legacy API callers may still
# send a single source_address line.
_REQUIRED = (
	"serial_no",
	"manufacturer",
	"model_name",
	# caliber + firearm_type are §478.125(e) bound-book columns (type,
	# caliber/gauge) — without them the FastBound push silently books
	# "Unknown" / "Other Firearm" placeholders.
	"caliber",
	"firearm_type",
	"condition_grade",
	"id_type",
	"id_number",
	"source_name",
	"source_first_name",
	"source_last_name",
	"acquisition_cost",
)

_ADDRESS_PART_KEYS = ("source_address_line1", "source_city", "source_state", "source_zip")
_ZIP_RE = _re.compile(r"^\d{5}(-\d{4})?$")


def resolve_source_address(payload: dict) -> str:
	"""Canonical seller address for the acquisition record.

	Preferred input is the dialog's SPLIT fields — street / city / state /
	ZIP — composed here into the one line ("street, City ST zip") the
	FastBound push parses deterministically (structured input closes the
	free-text-parse fragility). When any split
	field is present, ALL must be, the state must be the 2-letter code and
	the ZIP a real 5/9-digit ZIP. A legacy caller may still send a single
	free-text `source_address` line. Either way an address is required.
	Shared with Receive Goods' individual-seller path (receive_goods.py maps
	its seller_* keys onto this schema) so the two intake forms enforce one
	address rulebook."""
	parts = {k: str(payload.get(k) or "").strip() for k in _ADDRESS_PART_KEYS}
	if any(parts.values()):
		missing = [k for k, v in parts.items() if not v]
		if missing:
			frappe.throw(_("Seller address is incomplete — missing: {0}").format(
				", ".join(missing)))
		from ffl_core.compliance.interstate_tax import US_CODES

		state = parts["source_state"].upper()
		if state not in US_CODES:
			frappe.throw(_("Seller address state must be the 2-letter code (got '{0}').").format(
				parts["source_state"]))
		if not _ZIP_RE.match(parts["source_zip"]):
			frappe.throw(_("Seller address ZIP '{0}' is not a valid ZIP code.").format(
				parts["source_zip"]))
		return "{0}, {1} {2} {3}".format(
			parts["source_address_line1"], parts["source_city"], state, parts["source_zip"])
	legacy = str(payload.get("source_address") or "").strip()
	if legacy:
		return legacy
	frappe.throw(_("Seller address is required (street, city, state, ZIP)."))

# Generational suffixes worth splitting out of a Customer's display name,
# mapped to the canonical casing the POS dialog's Suffix dropdown offers.


@frappe.whitelist()
def create_trade_in_intake(payload) -> dict:
	"""Create a Used Firearm Item + FFL Acquisition for an individual trade-in.

	With payload.apply_credit truthy, the acquisition is parked as a DRAFT
	(booked at checkout submit) and the result carries `credit` — a locked
	negative TRADE-IN-CREDIT line payload for the POS cart. Re-running the
	dialog for a serial that already has a pending draft refreshes that
	draft (new cost honoured) instead of duplicating it.

	Without apply_credit the store buys the gun and pays the seller now:
	payload.payout_method (Cash / Zelle / Check / ACH, and payout_reference
	unless Cash) is required. The gun is received on a Purchase Receipt from
	the seller, dated today, and the payment booked with it (Dr Stock Received
	But Not Billed / Cr Cash or the bank) so the cash drawer count includes it
	(_buy_now).

	Accessories / ammo (payload.accessories, [{item_code, qty, rate}] with
	rate the per-unit value) come in alone (has_firearm 0) or with the gun:
	with apply_credit a draft Material Receipt and a credit line of their own
	(_park_accessories), without it lines on the same paid receipt.

	Args:
	    payload: Trade-in intake fields (same shape as
	        firearm_intake.create_intake plus seller identity, apply_credit,
	        has_firearm and accessories) — dict or JSON string.

	Returns:
	    {ok, item_code, acquisition} (None without a gun): with apply_credit
	    from firearm_intake.create_intake (or _refresh_pending_draft), plus
	    `credits` (one locked line per trade-in record: the gun's carries
	    `acquisition`, the accessories' `stock_entry`), `credit` (the gun's)
	    and `accessory_receipt`; without it from _buy_now
	    (receive_goods._create_receive), plus `purchase_receipt` and `payout`
	    ({entry, journal_entry}).

	Raises:
	    ValidationError: Missing fields / duplicate serial.

	Permissions:
	    roles.TRADE_IN_ROLES: the
	    counter's sales role plus the stock roles; holding only the sales
	    manager role is refused. Gated at entry, covering BOTH branches — the
	    refresh branch (_refresh_pending_draft) rewrites A&D draft seller
	    identity/cost and historically ran ungated (endpoint-gates hardening,
	    2026-08-07). A caller without a stock role files the gun under no Item
	    (one is made for this gun) or an existing per-gun priced firearm Item
	    (_check_counter_item); making or choosing any other Item stays a stock
	    job. Everything behind the gate runs gate-free
	    (firearm_intake.create_intake, receive_goods._create_receive), each of
	    our writes with ignore_permissions; the ERPNext-internal checks against
	    the session user (the gun's stock bundle) rest on the DocPerm
	    setup/pos_compat.ROLE_GRANTS gives the counter.
	"""
	if not set(frappe.get_roles()) & set(TRADE_IN_ROLES):
		frappe.throw(_("Taking a trade-in needs the Sales User role."), frappe.PermissionError)
	payload = coerce_payload(payload)

	accessories = _accessory_lines(payload.get("accessories"))
	# The dialog's "Includes a firearm" box; an API caller that never sends it is trading in a gun, as always.
	has_gun = bool(frappe.utils.cint(payload.get("has_firearm", 1)))
	if not has_gun and not accessories:
		frappe.throw(_("Nothing to trade in: add the firearm or at least one accessory / ammo line."))
	required = _REQUIRED if has_gun else _SELLER_REQUIRED
	missing = [f for f in required if not str(payload.get(f) or "").strip()]
	if missing:
		frappe.throw(_("Missing required fields: {0}").format(", ".join(missing)))

	# The dialog's source_name is a Customer LINK — its value is the Customer
	# docname. Keep the link for the stable FastBound contact externalId and
	# resolve the display name for the acquisition record. A free-text seller
	# (legacy API callers) still works for a credit: no Customer row → no link,
	# name passes through as typed. A pay-now buy pays a Customer (_buy_now).
	customer = str(payload["source_name"]).strip()
	customer_name = frappe.db.get_value("Customer", customer, "customer_name")
	if accessories and not customer_name:
		frappe.throw(_("Pick the seller from Customers to take accessories or ammo in trade."))

	intake_payload = {
		"source_type": TRADE_IN_SOURCE,
		# Optional shared model SKU the operator picked/typed (e.g. "Glock19
		# USED"). When present the gun is filed under it instead of a new
		# one-Item-per-gun auto-mint; blank keeps the legacy behaviour.
		"item_code": (str(payload.get("item_code") or "").strip()) or None,
		"source_name": customer_name or customer,
		"source_customer": customer if customer_name else None,
		"source_first_name": payload["source_first_name"],
		"source_middle_name": payload.get("source_middle_name") or "",
		"source_last_name": payload["source_last_name"],
		"source_suffix": payload.get("source_suffix") or "",
		"source_address": resolve_source_address(payload),
		"source_id_type": payload["id_type"],
		"source_id_number": payload["id_number"],
		"serial": payload.get("serial_no") or "",
		"manufacturer": payload.get("manufacturer") or "",
		"model_name": payload.get("model_name") or "",
		"caliber": payload.get("caliber") or "",
		"importer": payload.get("importer") or "",
		"condition_grade": payload.get("condition_grade") or "",
		"acquisition_cost": flt(payload.get("acquisition_cost")),
		"retail_price": float(payload["retail_price"]) if payload.get("retail_price") else None,
		"firearm_type": payload.get("firearm_type") or None,
		"barrel_length": payload.get("barrel_length") or None,
		"finish": payload.get("finish") or None,
		"year_made": payload.get("year_made") or None,
		# Where the gun is put in the store; parked with the draft when the booking is
		# deferred to checkout.
		"storage_location": (str(payload.get("storage_location") or "").strip()) or None,
	}
	if has_gun:
		# A gun the store sold and takes back is filed under its own Item: decide that before the Item check.
		_check_serial_free(intake_payload)
		_check_counter_item(intake_payload["item_code"])

	apply_credit = frappe.utils.cint(payload.get("apply_credit"))
	payout_method = str(payload.get("payout_method") or "").strip()
	if not apply_credit and payout_method not in PAYOUT_METHODS:
		frappe.throw(_("Say how the seller was paid ({0}), or apply the trade-in as a credit to a sale.").format(
			", ".join(PAYOUT_METHODS)))
	if apply_credit:
		result = {"ok": True, "item_code": None, "acquisition": None}
		if has_gun:
			result.update(
				_refresh_pending_draft(intake_payload)
				# Deferred booking: draft acquisition only — the gun enters the
				# bound book when the POS Invoice carrying the credit row submits.
				or create_intake(intake_payload, defer_booking=True)
			)
		# Hand the POS dialog a locked credit payload to inject into the live
		# cart (POS's own add-item path). The row can't be parked server-side:
		# a credit-only draft trips POS Invoice's non-negative grand-total
		# docfield. Tampering is pointless — the submit gate
		# (compliance/trade_in_credit.py) re-derives the amount from the
		# acquisition and blocks reuse.
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM, ensure
		ensure()  # lazy — never runs at install time before masters exist
		# Rounded to the invoice row's rate precision: the server pin and the
		# submit gate compare at that precision (compliance/locked_lines).
		from ffl_core.compliance.locked_lines import rate_precision
		precision = rate_precision("POS Invoice")
		result["credits"] = []
		if has_gun:
			result["credit"] = {
				"item_code": TRADE_IN_CREDIT_ITEM,
				"rate": flt(-flt(intake_payload["acquisition_cost"]), precision),
				"acquisition": result["acquisition"],
			}
			result["credits"].append(result["credit"])
		if accessories:
			result["accessory_receipt"] = _park_accessories(accessories, customer)
			result["credits"].append({
				"item_code": TRADE_IN_CREDIT_ITEM,
				"rate": flt(-sum(flt(a["qty"]) * flt(a["rate"]) for a in accessories), precision),
				"stock_entry": result["accessory_receipt"],
			})
		return result

	# A reference typed for a bank payment and left behind when the method went back to Cash is not this payment's.
	reference = None if payout_method == "Cash" else str(payload.get("payout_reference") or "").strip() or None
	return _buy_now(intake_payload, has_gun, accessories, payout_method, reference)


def _check_counter_item(item_code: str | None) -> None:
	"""Refuse, before anything is written, an Item a counter user (no roles.STOCK_ROLES) may not file a trade-in
	under. Blank is fine: the trade-in makes this gun its own Item, as always. Otherwise it must be an Item that
	exists (making one, firearm_intake._create_model_item, is a stock job like receive_goods.create_item), is
	enabled (receive_goods.search_items, the dialog's picker, lists enabled Items only), is serialized, and is a
	per-gun priced firearm (firearm.is_per_serial_priced: a firearm not on uniform pricing -- the used guns, each
	priced on its serial). A uniform-priced catalog SKU or a non-firearm is received on Receive Goods."""
	if not item_code or set(frappe.get_roles()) & set(STOCK_ROLES):
		return
	row = frappe.db.get_value("Item", item_code,
		["is_firearm", "item_group", "uniform_sell_price", "has_serial_no", "disabled"], as_dict=True)
	if not row:
		frappe.throw(_("Item {0} does not exist. Pick an existing used firearm item, or leave Item blank and "
			"one is made for this gun.").format(item_code), frappe.PermissionError)
	if row.disabled or not row.has_serial_no or not is_per_serial_priced(row):
		frappe.throw(_("Item {0} is not a used firearm item, so a trade-in cannot be filed under it from the "
			"counter. Pick a used firearm item, or leave Item blank and one is made for this gun.").format(item_code),
			frappe.PermissionError)


# Seller fields every trade-in needs, gun or not (the gun's own are _REQUIRED's rest).
_SELLER_REQUIRED = ("id_type", "id_number", "source_name", "source_first_name", "source_last_name")


def _accessory_lines(raw) -> list[dict]:
	"""The dialog's accessory / ammo lines as [{item_code, qty, rate}] (rate = what the store gives per unit), blank
	rows dropped. Each must be an enabled stock Item that is no firearm and carries no serial: a gun has its own
	section (bound book), and these lines are counted, not serialized."""
	if isinstance(raw, str):
		try:
			raw = _json.loads(raw or "[]")
		except _json.JSONDecodeError:
			frappe.throw(_("Accessories must be a list of item lines."))
	lines = []
	if not isinstance(raw or [], list):
		frappe.throw(_("Accessories must be a list of item lines."))
	for idx, row in enumerate(raw or [], start=1):
		if not isinstance(row or {}, dict):
			frappe.throw(_("Accessory row #{0} is not an item line.").format(idx))
		item_code = str((row or {}).get("item_code") or "").strip()
		if not item_code:
			continue
		qty, rate = flt(row.get("qty")), flt(row.get("rate"))
		item = frappe.db.get_value("Item", item_code,
			["disabled", "is_stock_item", "has_serial_no", "is_firearm", "item_group"], as_dict=True)
		if not item or item.disabled:
			frappe.throw(_("Accessory row #{0}: item {1} does not exist.").format(idx, item_code))
		if _is_firearm_row(item) or item.has_serial_no or not item.is_stock_item:
			frappe.throw(_("Accessory row #{0}: {1} is not an accessory or ammo item. A firearm goes in the Firearm "
				"section; serialized items are received on Receive Goods.").format(idx, item_code))
		if qty <= 0 or rate <= 0:
			frappe.throw(_("Accessory row #{0}: enter the quantity and what the store gives for each.").format(idx))
		lines.append({"item_code": item_code, "qty": qty, "rate": rate})
	return lines


def _park_accessories(lines: list[dict], customer: str) -> str:
	"""A DRAFT Material Receipt of the accessories / ammo a credit trade-in takes in, at the cost given for them,
	marked as a trade-in from ``customer``. Nothing reaches stock until the sale carrying its credit line submits
	(compliance/trade_in_credit.book_trade_ins_on_submit), so an abandoned cart leaves only an inert draft."""
	from ffl_core.api.firearm_intake import _company, _default_warehouse
	from ffl_core.setup.trade_in_credit import TRADE_IN_SELLER_FIELD

	company = _company()
	warehouse = _default_warehouse(company)
	se = frappe.get_doc({
		"doctype": "Stock Entry",
		"stock_entry_type": "Material Receipt",
		"company": company,
		TRADE_IN_SELLER_FIELD: customer,
		"remarks": _("Trade-in from {0}").format(customer),
		"items": [{"item_code": a["item_code"], "qty": a["qty"], "t_warehouse": warehouse,
			"basic_rate": a["rate"], "set_basic_rate_manually": 1} for a in lines],
	})
	se.flags.ignore_permissions = True
	se.insert()
	return se.name


@frappe.whitelist()
def create_accessory_item(item_name: str, item_group: str, sell_price: float | None = None) -> dict:
	"""Make a new accessory / ammo / parts Item from the Trade-in dialog, for something the store has never stocked.

	Args:
	    item_name: The new Item's name; also its code.
	    item_group: A leaf group under Ammunition, Accessory or Parts — never a firearm group.
	    sell_price: Optional selling price (Item.standard_rate).

	Returns:
	    {item_code, item_name}.

	Permissions:
	    roles.TRADE_IN_ROLES, like the trade-in itself: the counter takes in what the seller brings. Only a
	    non-serialized, non-firearm stock Item can be made here; any other Item stays a stock job
	    (receive_goods.create_item).
	"""
	if not set(frappe.get_roles()) & set(TRADE_IN_ROLES):
		frappe.throw(_("Taking a trade-in needs the Sales User role."), frappe.PermissionError)
	name = (item_name or "").strip()
	if not name:
		frappe.throw(_("Enter the item's name."))
	if frappe.db.exists("Item", name):
		frappe.throw(_("Item {0} already exists: pick it from the list.").format(name))
	group = frappe.db.get_value("Item Group", item_group or "", ["is_group", "parent_item_group"], as_dict=True)
	if not group or group.is_group or group.parent_item_group not in TRADE_IN_ACCESSORY_PARENTS:
		frappe.throw(_("Pick an ammo, accessory or parts category for {0}.").format(name))
	item = frappe.get_doc({
		"doctype": "Item", "item_code": name, "item_name": name, "item_group": item_group, "stock_uom": "Nos",
		"is_stock_item": 1, "has_serial_no": 0, "is_firearm": 0,
	})
	item.flags.ignore_permissions = True
	item.insert()
	# Priced after the insert: given to insert(), ERPNext's Item.after_insert makes the selling Item Price as the
	# session user, a permission the counter lacks (firearm_intake._create_item, the same reason).
	if flt(sell_price) > 0:
		from ffl_core.api.receive_goods import _set_item_selling_price
		_set_item_selling_price(item.name, flt(sell_price))
	return {"item_code": item.name, "item_name": item.item_name}


# The parents of the categories a counter-made trade-in Item may go in (mirrored by the dialog's category picker).
TRADE_IN_ACCESSORY_PARENTS = (AMMUNITION, ACCESSORY, PARTS)


def _buy_now(intake: dict, has_gun: bool, accessories: list[dict], method: str, reference: str | None) -> dict:
	"""The store buys the gun (and any accessories / ammo) and pays the seller now: a Receive Goods receipt from
	the seller, paid with it (receive_goods._create_receive, the Receive Goods endpoint's gate-free body) -- the same
	stock, seller payment and bound-book entry as buying from a private seller on the Receive Goods page, dated
	today. Its source TRADE_IN_SOURCE keeps the acquisition a trade-in (bound book, Inventory Receipts). The gun's
	per-unit facts (condition, sell price, description) are stamped as an intake's are. ``intake`` always carries
	the seller; its gun half only when ``has_gun``."""
	from ffl_core.api.firearm_intake import (
		_company, _default_warehouse, _resolve_item, _stamp_serial, _validate_required, _validate_source)
	from ffl_core.api.receive_goods import _create_receive

	if not intake["source_customer"]:
		frappe.throw(_("Pick the seller from Customers to pay them now."))
	items = []
	item_code = None
	if has_gun:
		_validate_required(intake)
		_validate_source(intake)
		# A credit run for this gun that was never checked out left a draft acquisition: paying now replaces it,
		# keeping the Item that run made for the gun. Rolled back with the rest if the buy fails.
		draft = frappe.db.get_value("FFL Acquisition",
			{"serial": intake["serial"].strip(), "docstatus": 0, "source_type": TRADE_IN_SOURCE},
			["name", "item"], as_dict=True)
		if draft:
			held = frappe.db.get_value("POS Invoice Item",
				{"ffl_trade_in_acquisition": draft.name, "docstatus": 0}, "parent")
			if held:
				frappe.throw(_("This gun's trade-in credit is on held sale {0}. Remove the credit line there "
					"first.").format(held))
			intake["item_code"] = intake["item_code"] or draft.item
			frappe.delete_doc("FFL Acquisition", draft.name, ignore_permissions=True)
		_check_serial_free(intake)
		if flt(intake["acquisition_cost"], 2) <= 0:  # a receipt that pays nobody would still reach the bound book
			frappe.throw(_("Enter what the store pays the seller."))
		item_code = _resolve_item(intake)
		# A shared model SKU keeps its own model fields (_create_receive writes the line's onto the Item); this gun's
		# own values reach its Serial No through _stamp_serial.
		model = frappe.db.get_value("Item", item_code, ["model_name", "caliber", "firearm_type"], as_dict=True)
		items.append({
			"item_code": item_code, "qty": 1, "rate": intake["acquisition_cost"], "serials": [intake["serial"]],
			"storage_location": intake["storage_location"],
			**{f: model.get(f) or intake.get(f) or "" for f in ("model_name", "firearm_type", "caliber")},
			**{f: intake.get(f) or "" for f in ("manufacturer", "importer")},
			"year_made": intake["year_made"],
		})
	items.extend({"item_code": a["item_code"], "qty": a["qty"], "rate": a["rate"]} for a in accessories)
	received = _create_receive({
		"acquisition_source": "Individual",
		"source": TRADE_IN_SOURCE,
		"seller_customer": intake["source_customer"],
		"seller_first_name": intake["source_first_name"],
		"seller_middle_name": intake["source_middle_name"],
		"seller_last_name": intake["source_last_name"],
		"seller_suffix": intake["source_suffix"],
		"seller_address": intake["source_address"],
		"id_type": intake["source_id_type"],
		"id_number": intake["source_id_number"],
		"seller_payment_method": method,
		"seller_payment_reference": reference,
		# Where a credit trade-in lands too (the POS Profile's warehouse first), so both kinds sell at the register.
		"warehouse": _default_warehouse(_company()),
		"items": items,
	# The gun's own facts are stamped before the bound-book push: nothing runs after it (_create_receive).
	}, before_push=(lambda: _stamp_serial({**intake, "item_code": item_code})) if has_gun else None)
	paid = received["seller_payment"]
	return {
		"ok": True, "item_code": item_code, "purchase_receipt": received["name"],
		"acquisition": frappe.db.get_value("FFL Acquisition", {"purchase_receipt": received["name"], "docstatus": 1}, "name"),
		"payout": {"entry": paid["entry"], "journal_entry": paid["journal_entry"]},
	}


def _refresh_pending_draft(intake_payload: dict) -> dict | None:
	"""Reuse an abandoned pending draft for the same serial.

	A cashier who abandons a cart leaves a draft Trade-in acquisition behind;
	re-running the dialog must refresh that draft (cost / seller / attrs), not
	duplicate it and not trip the duplicate-serial guard. Returns the same
	shape as create_intake, or None when there's nothing to reuse. The gun's
	Item link deliberately stays as first filed."""
	serial = (intake_payload.get("serial") or "").strip()
	existing = frappe.db.get_value(
		"FFL Acquisition",
		{"serial": serial, "docstatus": 0, "source_type": TRADE_IN_SOURCE},
		["name", "item"], as_dict=True,
	)
	if not existing:
		return None
	from ffl_core.api.firearm_intake import _parked_stamp_payload
	doc = frappe.get_doc("FFL Acquisition", existing.name)
	doc.update({
		"source_name": intake_payload["source_name"],
		"source_customer": intake_payload["source_customer"],
		"source_first_name": intake_payload["source_first_name"],
		"source_middle_name": intake_payload["source_middle_name"] or None,
		"source_last_name": intake_payload["source_last_name"],
		"source_suffix": intake_payload["source_suffix"] or None,
		"source_address": intake_payload["source_address"],
		"source_id_type": intake_payload["source_id_type"],
		"source_id_number": intake_payload["source_id_number"],
		"cost": intake_payload["acquisition_cost"],
		"acquisition_date": frappe.utils.now_datetime(),
		"pending_intake_payload": _json.dumps(_parked_stamp_payload(intake_payload)),
	})
	doc.flags.ignore_permissions = True
	doc.save()
	return {"ok": True, "item_code": existing.item, "acquisition": existing.name}


@frappe.whitelist()
def seller_defaults(customer: str) -> dict:
	"""Prefill for the POS Trade-in dialog's seller section.

	A best-effort structured-name split of the Customer (generational
	suffix pulled out even mid-name — 'pat lee IV Sampleton' →
	first pat, middle lee, last Sampleton, suffix IV) plus their
	primary address as a single line ('addr1, City ST zip' — the shape the
	FastBound push parses). Everything is editable in the dialog; this only
	saves retyping.

	Args:
	    customer: Customer name; unknown/blank returns {}.

	Returns:
	    {source_first_name, source_middle_name, source_last_name,
	    source_suffix, source_address_line1, ...} or {} when unknown.
	"""
	frappe.only_for(COUNTER_ROLES)
	customer = (customer or "").strip()
	if not customer or not frappe.db.exists("Customer", customer):
		return {}
	# db.get_value below bypasses DocPerms — gate explicitly, or any logged-in
	# user could harvest customer addresses/phones through this endpoint.
	if not frappe.has_permission("Customer", "read", doc=customer):
		frappe.throw(
			_("Not permitted to read Customer {0}.").format(customer),
			frappe.PermissionError,
		)
	cust = frappe.db.get_value(
		"Customer", customer,
		["customer_name", "customer_primary_address", "mobile_no", "email_id",
			"ffl_first_name", "ffl_last_name", "first_name", "last_name",
			"middle_name", "name_suffix"],
		as_dict=True,
	) or frappe._dict()

	split = split_display_name(cust.customer_name)
	# Structured identity stored on the Customer wins — the editable "Name"
	# section (ffl_first_name / middle_name / ffl_last_name / name_suffix),
	# then the standard hidden Contact-mirror first_name/last_name for
	# customers created the ERPNext-native way; the display-name split only
	# fills what neither carries.
	first = (cust.ffl_first_name or cust.first_name or "").strip() or split["first"]
	last = (cust.ffl_last_name or cust.last_name or "").strip() or split["last"]
	middle = (cust.middle_name or "").strip() or split["middle"]
	suffix = (cust.name_suffix or "").strip() or split["suffix"]

	parts = customer_address_parts(customer)
	return {
		"source_first_name": first,
		"source_middle_name": middle,
		"source_last_name": last,
		"source_suffix": suffix,
		# Structured parts for the dialog's split address fields…
		"source_address_line1": parts.get("address_line1", ""),
		"source_city": parts.get("city", ""),
		"source_state": parts.get("state", ""),
		"source_zip": parts.get("zip", ""),
		# …and the composed line for legacy consumers.
		"source_address": _compose_address_line(parts),
		"phone": cust.mobile_no or "",
		"email": cust.email_id or "",
	}


def customer_address_parts(customer: str) -> dict:
	"""The Customer's primary Address as structured parts ({address_line1,
	city, state, zip}) straight off the Address record — the dialog prefill
	maps these onto its split fields with no string parsing anywhere. A
	spelled-out state ("Texas") comes back as its code. Empty dict when the
	customer has no usable primary address."""
	from ffl_core.compliance.interstate_tax import normalize_us_state

	primary = frappe.db.get_value("Customer", customer, "customer_primary_address")
	if not primary:
		return {}
	a = frappe.db.get_value(
		"Address", primary,
		["address_line1", "city", "state", "pincode"], as_dict=True,
	)
	if not a or not (a.address_line1 or "").strip():
		return {}
	return {
		"address_line1": a.address_line1.strip(),
		"city": (a.city or "").strip(),
		"state": normalize_us_state(a.state) or (a.state or "").strip(),
		"zip": (a.pincode or "").strip(),
	}


def _compose_address_line(parts: dict) -> str:
	if not parts:
		return ""
	city_state_zip = " ".join(x for x in (
		parts.get("city", ""), parts.get("state", ""), parts.get("zip", "")) if x)
	return ", ".join(x for x in (parts.get("address_line1", ""), city_state_zip) if x)


def customer_address_line(customer: str) -> str:
	"""The Customer's primary Address composed as one line — "addr1, City ST
	zip", the shape the FastBound push parses (client._ADDR_TAIL_RE). Shared
	by the dialog prefill (seller_defaults above) and the push-side fallback
	for acquisitions that carry a source_customer but no free-text
	source_address (Receive Goods Individual). Empty string
	when the customer has no usable primary address."""
	return _compose_address_line(customer_address_parts(customer))
