"""Receive Goods page backend.

Two whitelisted entry points:

  lookup_item(scan)
    Resolve a scanned barcode (or typed item code) to item info needed
    by the UI: code, name, is_firearm, has_serial_no, last cost.
    Used by the scanner input to populate the pending row.

  seller_payment_preview(payload)
    What Submit will pay a private seller (the Pay the seller dialog).

  create_receive(payload)
    Build + insert + submit one Purchase Receipt from the simplified
    UI payload. Existing PR hooks (validate / on_submit) handle FFL
    compliance + Acquisition creation + FastBound enqueue, so this
    function is just a thin wrapper. A purchase from a private seller
    also books how the seller was paid (cash drawer or bank), in the
    same transaction.

The form fields the user touches are kept minimal — supplier, date,
items (item_code/qty/rate/serials). Everything else (warehouse, company,
cost center, etc.) defaults from the Frappe / company config.
"""
from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from ffl_core import cash_drawer, storage
from ffl_core.api._helpers import coerce_payload
from ffl_core.roles import COUNTER_ROLES, STOCK_ROLES
from ffl_core.firearm import (
	_is_firearm_row,
	assert_antique_flag_allowed,
	assert_transfer_declaration_unambiguous,
	is_consignment_acquisition_type,
	is_transfer_intake,
	item_has_boundbook_presence,
	items_with_boundbook_presence,
	log_antique_flip,
	log_transfer_type_near_miss,
	sane_year_made,
	serial_firearm_attr_updates,
)
from ffl_core.setup.zero_cost import OTHER_REASON, ZERO_COST_REASONS
from ffl_core.utils import set_file_privacy


@frappe.whitelist()
def lookup_item(scan: str) -> dict:
	"""Resolve a barcode or item code → item info dict.

	Tries the Item Barcode child table first, then Item code.

	Args:
	    scan: Scanned barcode or typed item code.

	Returns:
	    {found: bool, item_code, item_name, item_group, has_serial_no,
	    is_firearm, is_antique, antique_locked, last_rate, stock_uom,
	    standard_rate, uniform_sell_price, dealer_price, ...} plus, for
	    firearms, the firearm detail fields (manufacturer, model_name,
	    caliber, firearm_type, year_made — importer is never returned: it
	    is a per-gun marking stated at receive, not an Item default).
	"""
	frappe.only_for(COUNTER_ROLES)
	scan = (scan or "").strip()
	if not scan:
		return {"found": False}

	# Try Item Barcode (child table on Item) first
	row = frappe.db.sql(
		"""
		SELECT parent FROM `tabItem Barcode`
		WHERE barcode = %s LIMIT 1
		""",
		(scan,),
		as_dict=True,
	)
	item_code = row[0]["parent"] if row else None

	# Fall back to direct item_code match
	if not item_code and frappe.db.exists("Item", scan):
		item_code = scan

	if not item_code:
		return {"found": False, "scan": scan}

	item = frappe.db.get_value(
		"Item", item_code,
		["item_code", "item_name", "item_group", "is_firearm", "has_serial_no", "stock_uom",
		 "standard_rate", "last_purchase_rate", "uniform_sell_price", "is_antique",
		 "year_made",
		 "manufacturer", "model_name", "caliber", "firearm_type"],
		as_dict=True,
	) or {}

	if not item:
		return {"found": False, "scan": scan}

	# Cost prefill: real purchase history → RSR dealer price → 0.
	# Never fall through to standard_rate (that's the SELLING price; this UI
	# is for receiving stock, where the rate must reflect cost).
	last_rate = float(item.get("last_purchase_rate") or _rsr_dealer_price(item["item_code"]) or 0)

	return {
		"found": True,
		"item_code": item["item_code"],
		"item_name": item.get("item_name") or item["item_code"],
		"item_group": item.get("item_group"),
		"has_serial_no": bool(item.get("has_serial_no")),
		"is_firearm": _is_firearm_row(item),
		"is_antique": bool(item.get("is_antique")),
		# The MODEL's year, if the Item carries one. The card shows it as a hint
		# beside its (blank) Year Made box — the box records THIS gun's year, so
		# it is never pre-filled from the model.
		"year_made": sane_year_made(item.get("year_made")),
		"antique_locked": item_has_boundbook_presence(item["item_code"]),
		"last_rate": last_rate,
		"stock_uom": item.get("stock_uom"),
		"uniform_sell_price": bool(item.get("uniform_sell_price")),
		"standard_rate": float(item.get("standard_rate") or 0),
		**_firearm_fields(item),
	}


@frappe.whitelist()
def create_receive(payload, before_push=None) -> dict:
	"""Build + insert + submit a Purchase Receipt from the page's payload.

	`payload` arrives as a JSON string when called via frappe.call from the
	browser; accept dicts too for internal Python callers.

	Schema:
		{
			"supplier": "ACME-FFL",
			"source": "RSR",                       # required — internal procurement channel
			"acquisition_type": "Purchase",        # optional — FastBound bound-book acquisition type
			"posting_date": "2026-05-05",          # optional, defaults to today
			"posting_time": "14:30:00",            # optional, defaults to now
			"warehouse": "Main - GS",              # optional, defaults to company default
			"notes": "...",                        # optional
			"items": [
				{"item_code": "X", "qty": 100, "rate": 0.50, "serials": [], "service_need": 0},
				{"item_code": "FIREARM-A", "qty": 1, "rate": 500, "serials": ["A001"],
				 "service_need": 1, "manufacturer": "Glock", "model_name": "19",
				 "firearm_type": "Pistol", "caliber": "9mm", "importer": ""},
				{"item_code": "FIREARM-B", "qty": 1, "rate": 0, "serials": ["B001"],
				 "zero_cost_confirmed": 1, "zero_cost_reason": "Supplier gift",
				 "zero_cost_note": "", "manufacturer": "...", "model_name": "..."}
			]
		}

	Any line may carry ``storage_location`` (a Storage Location name): where the
	units are put in the store. A firearm line with several serials fills that slot and
	the next free ones in the same zone; ``serial_locations`` ({serial: location})
	overrides it per gun. A line with no location lands in "unassigned" on the
	Storage Locations page. Ignored for a customer-custody receive.

	`rate` is REQUIRED on every line except a customer-custody receipt
	(is_ffl_transfer, forced to 0 here) and a consignor's stock
	(acquisition_type="Consignment", legitimately $0). A missing rate and a
	rate of 0 are NOT the same thing and neither is accepted silently: an
	acquisition that really was free must say so with `zero_cost_confirmed: 1`
	plus a `zero_cost_reason` from ffl_core.setup.zero_cost.ZERO_COST_REASONS
	("Supplier gift" / "Promotional item" / "Warranty replacement" / "Sample" /
	"Other"), and "Other" additionally requires a free-text `zero_cost_note`.
	The trio lands on the Purchase Receipt Item (ffl_-prefixed) and, for
	firearms, on each Serial No.

	Validation here is minimal — server-side hooks (validate, on_submit)
	enforce the FFL gate + Used Firearm qty=1 + Acquisition creation.

	A purchase from a private seller (``acquisition_source`` "Individual")
	that costs anything says how the seller was paid: ``seller_payment_method``
	(Cash / Zelle / Check / ACH) and, unless Cash, ``seller_payment_reference``
	(check number, Zelle confirmation, ACH reference). Whether and how much is
	paid is one decision (``cash_drawer.seller_payment_at_receipt``), the same
	one ``seller_payment_preview`` gives the page before Submit. The payment is
	booked in the receive's transaction BEFORE the bound-book push, and the
	drawer lock is taken first, before any stock is written, with a bounded
	wait (the same lock order as a count, so the two never deadlock): nothing
	that can fail runs after FastBound has the acquisition, and a push that
	fails rolls the payment back with the receipt. The amount booked must be
	the amount the dialog showed, or the receive is refused before the push. Dr Stock Received But Not Billed / Cr Cash, or the bank account,
	dated today (``cash_drawer.record_goods_payout``). It covers every line, the guns
	included. Which receipts pay: ``cash_drawer.receipt_pays_seller`` (the
	acquisition type blank, "Purchase" or "Individual"); a Consignment,
	Gunsmithing or transfer receipt, a customer-custody one and a dealer
	purchase pay nobody here, and a method sent for one is ignored.

	Args:
	    payload: See Schema above — dict or JSON string.
	    before_push: Internal callers only: a callable run in the receive's transaction just before the bound-book
	        push (a whitelisted call's arguments are never callable).

	Returns:
	    {ok: True, name: <Purchase Receipt>, total: <grand total>,
	    seller_payment: {entry, journal_entry, amount, method, reference,
	    excluded} or None when nobody was paid here}.

	Raises:
	    ValidationError: Payload errors or PR submit failures.

	Permissions:
	    Stock Manager / System Manager.
	"""
	frappe.only_for(STOCK_ROLES)
	return _create_receive(payload, before_push)


def _create_receive(payload, before_push=None) -> dict:
	"""Gate-free body of create_receive, for the one internal caller that has its own gate: the POS Trade-in's
	pay-now buy (api/trade_in._buy_now, a counter job). The receipt, its payout and the bound-book push all run
	with ignore_permissions; nothing below reads the caller's role."""
	payload = coerce_payload(payload)

	# Acquisition source: "FFL Dealer" (purchase from a licensed dealer — FFL
	# supplier required) or "Individual" (bought from a private party — capture
	# the seller Customer + ID; no FFL supplier).
	acquisition_source = (payload.get("acquisition_source") or "").strip() or "FFL Dealer"
	supplier = (payload.get("supplier") or "").strip()
	seller_customer = seller_id_type = seller_id_number = seller_id_document = None
	seller_first_name = seller_middle_name = seller_last_name = seller_suffix = None
	seller_address = None
	payment_method = payment_reference = None
	if acquisition_source == "Individual":
		seller_customer = (payload.get("seller_customer") or "").strip()
		if not seller_customer:
			frappe.throw(_("Seller (Customer) is required for an individual acquisition."))
		if not frappe.db.exists("Customer", seller_customer):
			frappe.throw(_("Customer '{0}' not found.").format(seller_customer))
		# FastBound records an individual seller's first + last name separately, so
		# capture them structured (middle optional) rather than splitting a single
		# free-text name — a one-word name would otherwise miss the last name.
		seller_first_name = (payload.get("seller_first_name") or "").strip()
		seller_middle_name = (payload.get("seller_middle_name") or "").strip()
		seller_last_name = (payload.get("seller_last_name") or "").strip()
		seller_suffix = (payload.get("seller_suffix") or "").strip()
		if not (seller_first_name and seller_last_name):
			frappe.throw(_("Seller first and last name are required for an individual acquisition."))
		seller_id_type = (payload.get("id_type") or "").strip()
		seller_id_number = (payload.get("id_number") or "").strip()
		if not (seller_id_type and seller_id_number):
			frappe.throw(_("Seller ID Type and ID Number are required for an individual acquisition."))
		# §478.125(e): a non-licensee acquisition records name AND address.
		# Same split-field rulebook as the POS trade-in dialog (2-letter
		# state, real ZIP); a legacy API caller may send one seller_address
		# line. Composed here, stamped onto the acquisitions below.
		seller_address = _seller_address(payload)
		# Government-ID scans are sensitive PII — force the file private.
		seller_id_document = _ensure_private_file((payload.get("id_document") or "").strip() or None)
		# How the store paid the seller (checked below, once the lines say what is owed).
		payment_method = (payload.get("seller_payment_method") or "").strip() or None
		payment_reference = (payload.get("seller_payment_reference") or "").strip() or None
		# The PR needs *a* supplier; the real seller identity is on the acquisition.
		supplier = _ensure_individual_supplier()
	elif not supplier:
		frappe.throw(_("Supplier is required."))

	source = (payload.get("source") or "").strip()
	if not source:
		frappe.throw(_("Source is required."))

	acquisition_type = (payload.get("acquisition_type") or "").strip()

	# Inbound customer transfer (a transfer-in on behalf of the customer): the customer this shipment's
	# firearm(s) are received FOR. Carried onto each FFL Acquisition so the
	# Pending Transfer Pickups report can age unclaimed guns.
	transfer_for_customer = (payload.get("transfer_for_customer") or "").strip() or None
	if transfer_for_customer and not frappe.db.exists("Customer", transfer_for_customer):
		frappe.throw(_("Customer '{0}' not found.").format(transfer_for_customer))

	# The Receive Goods "FFL Transfer" checkbox — the explicit declaration
	# that this shipment is a customer's gun passing through. It is what the page
	# writes and what the Purchase Receipt records; the two legacy markers above
	# stay readable so an API/MCP caller predating the checkbox still works.
	is_ffl_transfer = 1 if payload.get("is_ffl_transfer") else 0
	# A pickup contact with no declaration at all is refused rather than guessed
	# (see the function's own docstring — guessing is expensive in both
	# directions now that this answer moves stock and money).
	assert_transfer_declaration_unambiguous(
		acquisition_type, transfer_for_customer, is_ffl_transfer)

	# TWO answers, split by blast radius — do not merge them again.
	#
	# `is_transfer` (PROTECTIVE): reads every marker, including the
	# soft ones. Its only effect is to SUPPRESS write-back of price/cost onto the
	# shared SKU. Over-triggering costs nothing — a price simply doesn't
	# propagate — while under-triggering is the original incident, so it stays
	# generous on purpose.
	#
	# `is_custody` (DESTRUCTIVE): overrides the warehouse, erases the cost,
	# refuses non-firearm lines and marks the gun permanently unsellable. It
	# answers to the explicit checkbox ALONE.
	#
	# That narrowing is not caution, it is a prod finding: a
	# read-only audit found one — acquisition_type "FFL Transfer", no
	# pickup customer, bought from another licensee. This store
	# uses that AcquireType for PURCHASES. Had the soft marker driven custody,
	# every such purchase would have been rerouted into the custody warehouse
	# with its cost erased, and refused outright if an accessory rode along.
	is_transfer = is_transfer_intake(
		acquisition_type, transfer_for_customer, is_ffl_transfer)
	is_custody = bool(is_ffl_transfer)
	# A consignor's gun is legitimately $0 to us — the store never bought it.
	# This is the case the original code declined to add a blanket "cost > 0"
	# rule for (see _resolve_line_cost); naming it lets the guard exempt it
	# explicitly instead of exempting every 0 in the system.
	#
	# Through the shared vocabulary predicate, NOT `== "Consignment"`: the value
	# is free-text mirroring FastBound's runtime SmartList, and an equality that
	# stops matching here would refuse a consignor's $0 gun at the door while
	# compliance/purchase.py stopped stamping the consignor — the two must agree.
	is_consignment_receipt = is_consignment_acquisition_type(acquisition_type)
	# The signal the original dead arm never raised about itself.
	log_transfer_type_near_miss(acquisition_type)

	items_in = payload.get("items") or []
	if not items_in:
		frappe.throw(_("At least one item line is required."))

	# Checked BEFORE the custody warehouse is resolved, because resolving it
	# CREATES it: a rejected document must not leave a brand-new empty warehouse
	# behind on the site.
	if is_custody:
		_assert_transfer_lines_are_firearms(items_in)

	# Custody is NOT a default the caller may override: the Receive Goods page
	# sends no warehouse at all (the server picks one), and an API caller that
	# does send one must not be able to steer a customer's gun into sellable
	# stock — which is precisely how that incident happened.
	warehouse = _custody_warehouse() if is_custody \
		else (payload.get("warehouse") or _default_warehouse())
	posting_date = payload.get("posting_date") or now_datetime().date().isoformat()
	posting_time = payload.get("posting_time")

	pr_items = []
	placements: list[dict] = []  # where each received unit goes (ffl_core.storage)
	firearm_writes: list[tuple[str, dict, list[str], int]] = []
	# Per-SKU retail prices to stamp onto Item.standard_rate (non-firearm + uniform
	# firearms). Per-gun firearms price per-serial via ffl_sell_price instead.
	sku_price_writes: dict[str, float] = {}
	for idx, line in enumerate(items_in, start=1):
		item_code = (line.get("item_code") or "").strip()
		if not item_code:
			frappe.throw(_("Row #{0}: item_code is required.").format(idx))
		# One cached Item read serves every per-row check below — existence, serial
		# tracking, the firearm test, the antique flag, and per-SKU price routing.
		meta = frappe.db.get_value(
			"Item", item_code,
			["is_firearm", "item_group", "has_serial_no", "uniform_sell_price",
				"is_antique"],
			as_dict=True, cache=True,
		)
		if not meta:
			frappe.throw(_("Row #{0}: item '{1}' not found.").format(idx, item_code))
		is_fa = _is_firearm_row(meta)

		qty = float(line.get("qty") or 0)
		if qty <= 0:
			frappe.throw(_("Row #{0}: qty must be > 0.").format(idx))

		# Cost is a REQUIRED box on the Receive Goods card, which is why a
		# transfer ever carried a number at all — staff had to type something to
		# submit. On a transfer that number is discarded here, at the door: the
		# store did not buy this gun, so its cost to us is 0, and 0 is what the
		# stock ledger, the GL and the sales report must all see. (The UI also
		# hides + zeroes the box, but the server is what makes it true.)
		#
		# Everything else goes through _resolve_line_cost, which refuses to let
		# "nobody filled it in" and "it really was free" become the same 0.
		if is_custody:
			rate, zero_confirmed, zero_reason, zero_note = 0.0, 0, None, None
		else:
			rate, zero_confirmed, zero_reason, zero_note = _resolve_line_cost(
				line, idx, item_code, serials_hint=line.get("serials"),
				is_consignment_receipt=is_consignment_receipt)
		serials = [s.strip() for s in (line.get("serials") or []) if s and s.strip()]
		if not is_custody:
			placements.extend(storage.plan_placements(
				item_code=item_code, qty=qty, serials=serials,
				storage_location=line.get("storage_location"),
				serial_locations=line.get("serial_locations")))

		# For firearm Items with has_serial_no=1, ERPNext requires serial_no
		# count == qty. Validate up front for a friendlier error.
		if meta.has_serial_no and len(serials) != int(qty):
			frappe.throw(_(
				"Row #{0}: item '{1}' requires {2} serial number(s), got {3}."
			).format(idx, item_code, int(qty), len(serials)))

		# Firearms: re-confirm the FastBound-required detail fields server-side
		# so a received firearm never reaches the bound book as "Unknown".
		if is_fa:
			fa = {f: (line.get(f) or "").strip() for f in _FIREARM_DETAIL_FIELDS}
			missing = [f for f in ("manufacturer", "model_name", "firearm_type", "caliber") if not fa[f]]
			if missing:
				frappe.throw(_(
					"Row #{0}: firearm '{1}' is missing required detail(s): {2}."
				).format(idx, item_code, ", ".join(missing)))
			# Sell Price is OPTIONAL at receive — staff often don't know the
			# tag price until later. Anything entered (incl. 0) flows through
			# to Serial No.sell_price; managers can set / amend per-gun on the
			# Item form's Serial Numbers tab before the firearm reaches POS.
			#
			# Antique: distinguish an explicit value (0/1) from an OMITTED key — a
			# caller that doesn't send is_antique (an API / MCP payload, or a stale
			# card) must PRESERVE the Item's current flag, not silently reset it to 0.
			antique_val = (1 if line.get("is_antique") else 0) if "is_antique" in line else None
			# Year of manufacture is OPTIONAL provenance on an antique, never a
			# precondition — the checkbox alone decides (2026-07-24). It used to be
			# a hard pre-1899 gate, but the Receive Goods card
			# shipped no year input, so the gate was unsatisfiable from the UI and
			# blocked every antique intake outright. Recorded when given.
			#
			# "Optional" is not "anything goes": a year is only worth storing if it
			# could be a year. A fat-fingered -5 / 18855 would otherwise land in the
			# one field this gun's GCA-exempt claim rests on, and nothing downstream
			# would ever question it. Out-of-range values are dropped, not rejected
			# — the receive still goes through (the box never blocks).
			fa["year_made"] = sane_year_made(line.get("year_made"))
			# Block the 0->1 flip on a booked item. This path writes is_antique via
			# db.set_value (bypassing the validate hook), so go through the shared
			# raiser so the rule + message stay identical to the hook's.
			if antique_val and not meta.is_antique:
				assert_antique_flag_allowed(item_code)
			firearm_writes.append((item_code, fa, serials, antique_val))

		# Per-SKU Sell Price: a non-firearm — or a uniform-priced firearm — is priced
		# once on the Item (one price per SKU), not per-serial. Stamp the entered
		# retail price onto Item.standard_rate (the Item on_update hook syncs it to the
		# POS Item Price). Per-gun firearms keep their per-serial ffl_sell_price below.
		# Same door, the price half: a customer's gun is not for sale at any
		# price, so nothing is recorded for it — not on the SKU (the guard
		# below) and not on the serial either. 0 is this system's "un-priced"
		# convention, which the Woo listing guard independently refuses to list.
		sell_price = 0.0 if is_custody else float(line.get("sell_price") or 0)
		# `not is_transfer`: a customer's gun passing through on a transfer never
		# reprices the store's SKU. Staff must type SOMETHING in the price box to
		# get a transfer through, so what lands here is a placeholder describing
		# one gun's paperwork. On 2026-07-19 a $35 placeholder became
		# Item.standard_rate on a uniform-priced SKU and four $589.68 pistols
		# went to WooCommerce at $35. The number still reaches the
		# SERIAL (via ffl_sell_price below); only the shared SKU is spared.
		if sell_price > 0 and not is_transfer and not (is_fa and not meta.uniform_sell_price):
			sku_price_writes[item_code] = sell_price

		pr_items.append({
			"item_code": item_code,
			"qty": qty,
			"rate": rate,
			"warehouse": warehouse,
			"serial_no": "\n".join(serials) if serials else None,
			"ffl_service_need": 1 if line.get("service_need") else 0,
			"ffl_sell_price": sell_price,
			# ERPNext refuses a $0 stock receipt unless zero valuation is
			# explicitly allowed — without this the custody receive would fail
			# outright instead of booking nothing (the same flag the POS
			# transfer sale sets on its $0 gun line). A confirmed-free purchase
			# is the same $0 row and needs the same permission: an Item with no
			# valuation history (a supplier's gift of a model we have never
			# bought — the headline case for this hatch) would otherwise be
			# refused at pr.submit() with ERPNext's own valuation error.
			"allow_zero_valuation_rate": 1 if (is_custody or zero_confirmed) else 0,
			# Setting rate=0 is NOT enough on its own: a zero rate is falsy, so
			# ERPNext's set_missing_item_details helpfully refills it from the
			# price list / last_purchase_rate — a transfer of a model the store
			# also stocks would silently inherit that model's last cost (caught
			# by test_a_transfer_placeholder_cost_is_forced_to_zero, which saw
			# $400 arrive on a line submitted at $0). is_free_item is ERPNext's
			# own lever for "this line is genuinely worth nothing", and the same
			# one the POS transfer sale uses to hold the gun at $0 on the way
			# back out. price_list_rate is pinned to 0 alongside it so nothing
			# downstream re-derives a rate from it.
			# A confirmed-free purchase needs the SAME lever as a custody line:
			# without it ERPNext re-derives the row's rate from the price list /
			# last purchase rate and the $0 the receiver confirmed silently
			# becomes a real cost (caught in test: rate came back 500).
			**({"is_free_item": 1, "price_list_rate": 0}
				if (is_custody or zero_confirmed) else {}),
			# Per-line consignment commission % (meaningful only when
			# acquisition_type=Consignment); create_acquisitions_for_firearm_lines
			# stamps it onto each serial at intake (blank/0 → the store default).
			"ffl_consignment_commission_pct": float(line.get("consignment_commission_pct") or 0),
			# Why this line is $0, when it is. The receipt row is the SOURCE of
			# this fact — a submitted Purchase Receipt records its own owner and
			# timestamp, and it is the only home for a line with no Serial No at
			# all (ammo, accessories). create_acquisitions_for_firearm_lines
			# copies the trio onto each firearm's Serial No from here, so every
			# path that produces a Purchase Receipt carries it, not just this API.
			**({"ffl_zero_cost_confirmed": 1, "ffl_zero_cost_reason": zero_reason,
				"ffl_zero_cost_note": zero_note}
				if zero_confirmed else {}),
		})

	# A private seller the store buys from is paid with the receipt (incident
	# 2026-10-06: two boxes of ammo bought for $20 cash, and nothing recorded the
	# cash). Not a consignor (paid when the gun sells), not a gun in for
	# gunsmithing, not a customer's gun on a transfer. The one decision the page's
	# dialog also asks (seller_payment_preview), checked before anything is written.
	seller_decision = cash_drawer.seller_payment_at_receipt({
		"ffl_acquisition_source": acquisition_source, "ffl_acquisition_type": acquisition_type,
		"ffl_transfer_for_customer": transfer_for_customer, "is_ffl_transfer": is_custody,
	}, [(r["qty"], r["rate"]) for r in pr_items])
	pays_seller = seller_decision.pays
	if pays_seller:
		cash_drawer.check_payout_method(payment_method, payment_reference, _("seller"),
			fields=("seller_payment_method", "seller_payment_reference"))

	# An Item is antique or not as a whole — reject the same SKU on multiple lines
	# with conflicting Antique settings (would otherwise resolve last-write-wins and
	# silently mis-book one of the serials: a regulated gun skipped, or an antique
	# wrongly booked).
	_antique_by_item: dict[str, int] = {}
	for _code, _fa, _ser, _av in firearm_writes:
		if _av is None:
			continue
		if _code in _antique_by_item and _antique_by_item[_code] != _av:
			frappe.throw(_("Item '{0}' appears on multiple lines with conflicting Antique "
				"settings — receive it under one classification.").format(_code))
		_antique_by_item[_code] = _av

	# A slot that is taken, a disabled location or a slot for loose stock is refused
	# NOW, before anything is written, with a message that says what to pick instead.
	storage.validate_placements(placements, frappe.db.get_value("Warehouse", warehouse, "company"))

	# Persist confirmed firearm details onto each Item BEFORE the PR submits.
	#
	# model / caliber / type DEFINE the model SKU — they're shared by every
	# unit, so keep them current on the Item (truthy filter keeps existing
	# values when the line is blank).
	#
	# manufacturer / importer can vary PER PHYSICAL GUN — old / surplus /
	# imported units under one SKU may have different makers (different
	# arsenals) or importer markings (different import batches). Those live on
	# each Serial No (stamped after submit, below) and the FastBound push reads
	# them there. manufacturer alone still SEEDS a blank Item (a model-level
	# maker is a fact for most SKUs, and the seed never overwrites); importer
	# is NEVER written to the Item (2026-08-20) — that seeding is how one
	# unit's marking became a bucket-wide value on generic SKUs like 98-8MM,
	# and nothing treats Item.importer as a per-gun default any more.
	for fa_item_code, fa, _serials, fa_is_antique in firearm_writes:
		updates = {k: fa[k] for k in ("model_name", "caliber", "firearm_type") if fa[k]}
		if fa["manufacturer"] and not frappe.db.get_value(
				"Item", fa_item_code, "manufacturer"):
			updates["manufacturer"] = fa["manufacturer"]
		# Antique: write only when the line carried an explicit value (0/1, set
		# above; None when the key was omitted). An omitted key PRESERVES the
		# current flag — a caller that doesn't echo is_antique back must not
		# silently un-flag an antique Item back to regulated.
		if fa_is_antique is not None:
			updates["is_antique"] = fa_is_antique
		# Audit the 0->1 flip HERE, at the write, not back at validation: this is
		# the moment the gun actually leaves the bound book's reach, it is past
		# the conflicting-lines check, and the fresh read below is what makes it
		# exactly-once — two lines of the same SKU both ticked Antique see a
		# flipped Item on the second pass and log nothing. The flag is written
		# with update_modified=False (no Version row), so without this there is
		# no record of who decided.
		if fa_is_antique and not frappe.db.get_value("Item", fa_item_code, "is_antique"):
			log_antique_flip(
				fa_item_code, source="Receive Goods", year_made=fa.get("year_made"),
				extra=f"Serial(s): {', '.join(_serials) or '(none)'}",
			)
		# Antique provenance: seed the Item's year_made from the line when one was
		# given, so a year the receiver DID know is durably on record (never
		# overwriting a year the Item already carries). Optional — an antique with
		# no known year receives just fine.
		if fa_is_antique and fa.get("year_made") and not frappe.utils.cint(
				frappe.db.get_value("Item", fa_item_code, "year_made") or 0):
			updates["year_made"] = fa["year_made"]
		if updates:
			frappe.db.set_value("Item", fa_item_code, updates, update_modified=False)

	# Apply per-SKU retail prices to the Item master + the POS Item Price. Targeted
	# writes (not a full Item.save) so an unrelated Item hook (image-gallery / UPC
	# re-sync) can't fire as a side effect of setting a price; the Sell Price the
	# receiver typed is authoritative, so an EXISTING POS price is updated too.
	for sku_item_code, sku_price in sku_price_writes.items():
		_set_item_selling_price(sku_item_code, sku_price)

	pr = frappe.get_doc({
		"doctype": "Purchase Receipt",
		"supplier": supplier,
		"ffl_source": source,
		"ffl_acquisition_type": acquisition_type or None,
		"ffl_acquisition_source": acquisition_source,
		"ffl_transfer_for_customer": transfer_for_customer,
		# The receipt-level answer, recorded rather than re-inferred: every
		# downstream hook reads THIS instead of re-deriving "was it a transfer?"
		# from the two soft markers. Mirrors POS Invoice.is_ffl_transfer at the
		# other end of the gun's stay.
		"is_ffl_transfer": 1 if is_custody else 0,
		"ffl_seller_customer": seller_customer,
		"ffl_seller_id_type": seller_id_type,
		"ffl_seller_id_number": seller_id_number,
		"ffl_seller_id_document": seller_id_document,
		"posting_date": posting_date,
		"posting_time": posting_time,
		"set_warehouse": warehouse,
		"items": pr_items,
		"remarks": (payload.get("notes") or "").strip() or None,
	})
	pr.flags.ignore_permissions = True
	# Receive is atomic with the FastBound bound book: a firearm enters FastBound
	# AND our stock, or neither. The flag makes each Acquisition's on_submit skip
	# its async push so we push synchronously below; a rejection there rolls back
	# the PR + Serial Nos + stock + Acquisitions together. Reset in finally so the
	# flag never leaks to a later request/test.
	frappe.flags.ffl_receive_sync_push = True
	try:
		if pays_seller:
			# The drawer lock first, before the receipt locks any stock row: a count takes the drawer, then the stock
			# rows its register shifts touch, so taking them in the same order here means the two can never deadlock.
			# A drawer kept busy refuses within _DRAWER_LOCK_WAIT seconds, before anything is written.
			cash_drawer.lock_drawer(frappe.db.get_value("Warehouse", warehouse, "company"), wait=_DRAWER_LOCK_WAIT)
		pr.insert()
		notices = len(frappe.local.message_log or [])
		pr.submit()
		if is_custody:
			_drop_no_accounting_notice(notices)

		# Stamp each firearm's per-unit facts onto its Serial No (the Serial No
		# records are materialised by pr.submit()). This is REQUIRED, not just
		# tidy: ERPNext creates Serial No rows via frappe.db.bulk_insert
		# (make_serial_nos), which bypasses doc events — so the
		# inherit_serial_firearm_attrs before_save hook never fires for received
		# guns and can't copy these from the Item. We stamp all four explicitly:
		#   - manufacturer / importer vary per physical gun (old / surplus /
		#     imported units) — the FastBound push prefers these per-serial
		#     values over the model SKU.
		#   - model / caliber define the SKU but are ALSO per-unit Serial No fields
		#     (the bound book records the physical gun); without this they'd stay
		#     blank on the serial, surfacing as a missing model/caliber on the gun.
		#   - year_made is the most per-unit of the lot: two guns of one model can
		#     be decades apart, and on an antique it is the whole GCA-exemption
		#     claim. It lands on THIS gun; the Item keeps only the model-level
		#     default it was first seeded with (2026-07-24).
		for fa_item_code, fa, fa_serials, _fa_is_antique in firearm_writes:
			sn_updates = serial_firearm_attr_updates(fa)
			if not sn_updates:
				continue
			for sn in fa_serials:
				if frappe.db.exists("Serial No", sn):
					frappe.db.set_value("Serial No", sn, sn_updates, update_modified=False)

		# (The confirmed-free trio reaches each Serial No from the Purchase
		# Receipt row instead, inside create_acquisitions_for_firearm_lines —
		# one db round-trip per serial alongside purchase_rate / is_consignment,
		# and it covers every path that submits a receipt, not just this one.)

		# Persist the seller's structured name onto the just-created Acquisitions
		# so the FastBound contact carries first/middle/last directly (set before
		# the push below reads them).
		if acquisition_source == "Individual":
			for acq_name in frappe.get_all(
					"FFL Acquisition",
					{"purchase_receipt": pr.name, "docstatus": 1}, pluck="name"):
				frappe.db.set_value("FFL Acquisition", acq_name, {
					"source_first_name": seller_first_name,
					"source_middle_name": seller_middle_name or None,
					"source_last_name": seller_last_name,
					"source_suffix": seller_suffix or None,
					"source_address": seller_address or None,
				}, update_modified=False)

		# Distributor-order provenance (RSR Direct Connect restock): stamp the
		# sourcing DO onto each just-created Acquisition BEFORE the push below, so
		# the FastBound bound-book acquisition carries the DO as its purchase-order
		# number. Optional additive payload key — blank for manual/individual
		# receives. `distributor_order` is an ffl_integrations custom field on FFL
		# Acquisition; only present when that app is installed, and only stamped when
		# the caller passes the key (so a bare create_receive is unaffected).
		distributor_order = (payload.get("distributor_order") or "").strip()
		if distributor_order and frappe.get_meta("FFL Acquisition").has_field("distributor_order"):
			for acq_name in frappe.get_all(
					"FFL Acquisition",
					{"purchase_receipt": pr.name, "docstatus": 1}, pluck="name"):
				frappe.db.set_value("FFL Acquisition", acq_name, "distributor_order",
					distributor_order, update_modified=False)

		# Put what arrived in the locations the receiver named. Strict, and BEFORE the
		# bound-book push below: a location that no longer fits (someone took the slot a
		# moment ago) rolls this receive back whole instead of leaving FastBound holding
		# an acquisition this system does not.
		storage.apply_placements(placements, voucher_type="Purchase Receipt", voucher_no=pr.name)

		# The money paid to a private seller, booked BEFORE the bound-book push for
		# the same reason as the placements: whatever can refuse it (a frozen
		# period, a missing account, the same goods paid on a cancelled receipt)
		# refuses while nothing has reached FastBound, and nothing that can fail
		# runs after the push. The drawer lock (taken above, before the receipt)
		# is held through the push, bounded by the FastBound client's request
		# timeout; a push that fails rolls the payment back with the receipt.
		# lazy: other drawer entries wait while a private-seller receive pushes
		# (seconds, a few receives a day); upgrade path: book after the push with
		# a FastBound delete on failure, if pushes ever get slow.
		seller_payment = None
		if pays_seller:
			paid = cash_drawer.record_goods_payout(pr.name, payment_method, payment_reference)
			if abs(flt(paid.amount) - flt(seller_decision.amount)) >= 0.005:
				frappe.throw(_("The seller would be paid {0}, not the {1} shown when you clicked Submit. Nothing was saved; "
					"check the lines and submit again.").format(
						frappe.format_value(paid.amount, {"fieldtype": "Currency"}),
						frappe.format_value(seller_decision.amount, {"fieldtype": "Currency"})))
			seller_payment = {
				"entry": paid.entry, "journal_entry": paid.journal_entry, "amount": flt(paid.amount, 2),
				"method": payment_method, "reference": payment_reference, "excluded": paid.excluded,
			}

		# An internal caller's own last writes (the Trade-in dialog's per-gun stamps), before the push like the rest.
		if callable(before_push):
			before_push()

		# Synchronous, atomic FastBound acquisition push (ffl_integrations wires
		# the `ffl_acquisition_sync_push` hook). Raises on rejection → the whole
		# receive rolls back. Empty hook list when FastBound isn't installed.
		# Nothing that can fail comes after it (the commit aside).
		for fn in frappe.get_hooks("ffl_acquisition_sync_push"):
			frappe.get_attr(fn)(pr.name)
	finally:
		frappe.flags.ffl_receive_sync_push = False

	return {
		"ok": True,
		"name": pr.name,
		"total": float(pr.grand_total or pr.total or 0),
		"seller_payment": seller_payment,
	}


# How long a receive waits for the cash drawer lock before refusing (a count merging the day's register shifts can
# hold it); the refusal comes before anything reaches FastBound.
_DRAWER_LOCK_WAIT = 5


@frappe.whitelist()
def seller_payment_preview(payload) -> dict:
	"""What Submit will pay the private seller, before anything is written: the Pay the seller dialog shows exactly
	what the receipt will book (the same rule and amount as create_receive).

	Args:
	    payload: The create_receive payload (dict or JSON string); only the
	        acquisition source / type, the transfer markers and the lines are read.

	Returns:
	    {pays, amount, methods}; pays is false when nobody is paid at receipt
	    (a dealer, Consignment, Gunsmithing or transfer receipt, or nothing
	    costs anything); methods are the ways a seller can be paid.
	"""
	frappe.only_for(STOCK_ROLES)
	payload = coerce_payload(payload)
	decision = cash_drawer.seller_payment_at_receipt({
		"ffl_acquisition_source": (payload.get("acquisition_source") or "").strip() or "FFL Dealer",
		"ffl_acquisition_type": (payload.get("acquisition_type") or "").strip(),
		"ffl_transfer_for_customer": (payload.get("transfer_for_customer") or "").strip() or None,
		"is_ffl_transfer": 1 if payload.get("is_ffl_transfer") else 0,
	}, [(i.get("qty"), i.get("rate")) for i in payload.get("items") or []])
	return {"pays": decision.pays, "amount": decision.amount, "methods": list(cash_drawer.PAYOUT_METHODS)}


def _seller_address(payload: dict) -> str:
	"""Compose + validate the individual seller's address from the form's split
	fields (seller_address_line1 / seller_city / seller_state / seller_zip;
	one legacy seller_address line also accepted) — the same rulebook as the
	POS trade-in dialog, via trade_in.resolve_source_address."""
	from ffl_core.api.trade_in import resolve_source_address
	return resolve_source_address({
		"source_address_line1": payload.get("seller_address_line1"),
		"source_city": payload.get("seller_city"),
		"source_state": payload.get("seller_state"),
		"source_zip": payload.get("seller_zip"),
		"source_address": payload.get("seller_address"),
	})


def _ensure_individual_supplier() -> str:
	"""The generic placeholder Supplier for firearms bought from a private party.
	ERPNext requires a Supplier on every Purchase Receipt; the real seller (a
	Customer) + their ID live on the FFL Acquisition. Non-FFL; created on demand.
	Matched by supplier_name so it works regardless of the Supplier naming rule."""
	name = "Individual Seller"
	existing = frappe.db.get_value("Supplier", {"supplier_name": name}, "name")
	if existing:
		return existing
	group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name") or "All Supplier Groups"
	doc = frappe.get_doc({"doctype": "Supplier", "supplier_name": name, "supplier_group": group})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _ensure_private_file(file_url: str | None) -> str | None:
	"""Force an uploaded File private (moves it to /private/files). Government-ID
	scans are sensitive PII and must never be publicly accessible. Returns the
	(possibly updated) file_url. No-op when already private or not found."""
	if not file_url:
		return file_url
	return set_file_privacy(file_url, private=True) or file_url


@frappe.whitelist()
def search_items(query: str, limit: int = 10) -> list[dict]:
	"""Typeahead search across Item.barcode, Item.item_code (SKU), Item.item_name.

	Ordering: exact barcode / item_code matches first, then LIKE matches
	ranked by item_name. Disabled items excluded.

	Args:
	    query: Substring to match (blank returns []).
	    limit: Row cap, clamped to 1–50 (default 10).

	Returns:
	    Up to `limit` rows with the same shape as `lookup_item`'s
	    successful response (minus the `found` flag), so the JS can hand a
	    picked row directly to the pending-card builder.
	"""
	frappe.only_for(COUNTER_ROLES)
	q = (query or "").strip()
	if not q:
		return []
	limit = min(max(1, int(limit)), 50)
	pattern = f"%{q}%"

	rows = frappe.db.sql(
		"""
		SELECT DISTINCT
			i.name AS item_code,
			i.item_name,
			i.item_group,
			i.is_firearm,
			i.has_serial_no,
			i.stock_uom,
			i.standard_rate,
			i.last_purchase_rate,
			i.uniform_sell_price,
			i.is_antique,
			i.year_made,
			i.manufacturer,
			i.model_name,
			i.caliber,
			i.firearm_type
		FROM `tabItem` i
		LEFT JOIN `tabItem Barcode` b ON b.parent = i.name
		WHERE i.disabled = 0 AND (
			i.name LIKE %(p)s
			OR i.item_name LIKE %(p)s
			OR b.barcode LIKE %(p)s
		)
		ORDER BY
			CASE
				WHEN b.barcode = %(q)s THEN 0
				WHEN i.name = %(q)s THEN 1
				WHEN i.item_name = %(q)s THEN 2
				WHEN i.name LIKE %(prefix)s THEN 3
				WHEN i.item_name LIKE %(prefix)s THEN 4
				ELSE 5
			END,
			i.item_name
		LIMIT %(l)s
		""",
		{"p": pattern, "q": q, "prefix": f"{q}%", "l": limit},
		as_dict=True,
	)

	booked = items_with_boundbook_presence([r["item_code"] for r in rows])
	return [
		{
			"item_code": r["item_code"],
			"item_name": r.get("item_name") or r["item_code"],
			"item_group": r.get("item_group"),
			"has_serial_no": bool(r.get("has_serial_no")),
			"is_firearm": _is_firearm_row(r),
			"is_antique": bool(r.get("is_antique")),
			# Same key lookup_item returns — the typeahead hands its row straight
			# to the card without a second fetch, so a shape drift here means the
			# model's known year goes missing for every gun picked by name (i.e.
			# every used / antique gun, exactly the ones without a barcode).
			"year_made": sane_year_made(r.get("year_made")),
			"antique_locked": r["item_code"] in booked,
			# Cost prefill: history → RSR dealer price → 0. Never standard_rate.
			"last_rate": float(r.get("last_purchase_rate") or _rsr_dealer_price(r["item_code"]) or 0),
			"stock_uom": r.get("stock_uom"),
			"uniform_sell_price": bool(r.get("uniform_sell_price")),
			"standard_rate": float(r.get("standard_rate") or 0),
			**_firearm_fields(r),
		}
		for r in rows
	]


@frappe.whitelist()
def create_item(payload) -> dict:
	"""Quick-create an Item from the Receive Goods page, then return the
	same shape as `lookup_item` so the caller can drop the result straight
	into the pending card.

	Args:
	    payload: Dict or JSON string. Required: item_code, item_group.
	        Optional: item_name (defaults to item_code), has_serial_no (0),
	        stock_uom (defaults to "Nos"), standard_rate, barcode,
	        uniform_sell_price, is_antique (GCA-exempt marker).

	Returns:
	    Same shape as lookup_item's successful response.

	Permissions:
	    Stock Manager / System Manager.
	"""
	frappe.only_for(STOCK_ROLES)
	payload = coerce_payload(payload)

	item_code = (payload.get("item_code") or "").strip()
	if not item_code:
		frappe.throw(_("Item code is required."))
	if frappe.db.exists("Item", item_code):
		frappe.throw(_("Item '{0}' already exists.").format(item_code))

	item_group = (payload.get("item_group") or "").strip()
	if not item_group:
		frappe.throw(_("Item group is required."))
	if not frappe.db.exists("Item Group", item_group):
		frappe.throw(_("Item Group '{0}' not found.").format(item_group))

	has_serial_no = 1 if int(payload.get("has_serial_no") or 0) else 0
	stock_uom = (payload.get("stock_uom") or "Nos").strip() or "Nos"
	item_name = (payload.get("item_name") or item_code).strip()
	standard_rate = float(payload.get("standard_rate") or 0)
	uniform_sell_price = 1 if int(payload.get("uniform_sell_price") or 0) else 0
	# Antique is only meaningful for firearms; harmless on a non-firearm Item
	# (the field's depends_on hides it). Stamped as given so a quick-created
	# antique SKU is born flagged.
	is_antique = 1 if int(payload.get("is_antique") or 0) else 0
	barcode = (payload.get("barcode") or "").strip()

	doc_dict = {
		"doctype": "Item",
		"item_code": item_code,
		"item_name": item_name,
		"item_group": item_group,
		"stock_uom": stock_uom,
		"is_stock_item": 1,
		"has_serial_no": has_serial_no,
		"standard_rate": standard_rate or None,
		"uniform_sell_price": uniform_sell_price,
		"is_antique": is_antique,
	}
	# For Used Firearm-style items (has_serial_no=0 + firearm group), the
	# Used Firearm helper also stamps firearm_serial — we don't need that
	# here because the Receive Goods flow always passes serials at receipt
	# time, not at item-create time.
	doc = frappe.get_doc(doc_dict)
	if has_serial_no and not doc.get("serial_no_series"):
		# Default series so submit-time serial validation does not nag.
		doc.serial_no_series = f"{item_code}-.####"
	doc.flags.ignore_permissions = True

	if barcode:
		doc.append("barcodes", {"barcode": barcode, "barcode_type": "EAN"})

	doc.insert()

	return {
		"found": True,
		"item_code": doc.name,
		"item_name": doc.item_name or doc.name,
		"item_group": doc.item_group,
		"has_serial_no": bool(doc.has_serial_no),
		"is_firearm": _is_firearm_row(doc),
		"is_antique": bool(getattr(doc, "is_antique", 0)),
		"antique_locked": False,
		"last_rate": float(standard_rate or 0),
		"stock_uom": doc.stock_uom,
		"uniform_sell_price": bool(getattr(doc, "uniform_sell_price", 0)),
		"standard_rate": float(doc.standard_rate or 0),
		**_firearm_fields(doc),
	}


# `year_made` is deliberately NOT in this tuple — it is string-only (every
# consumer .strip()s each member) and the year is an Int. It rides
# firearm.SERIAL_FIREARM_INT_ATTRS instead, which is where the per-unit field set
# lives; `sane_year_made` is imported from the same module so the card, the Item
# seed and the Serial No write all apply one definition of "could be a year".
_FIREARM_DETAIL_FIELDS = ("manufacturer", "model_name", "caliber", "firearm_type", "importer")


def _resolve_line_cost(line: dict, idx: int, item_code: str, *, serials_hint,
		is_consignment_receipt: bool) -> tuple[float, int, str | None, str | None]:
	"""Resolve one receive line's cost, refusing to let two different facts
	become the same 0.

	The bug this exists to kill was a single ``or 0``::

	    rate = 0.0 if is_custody else float(line.get("rate") or 0)

	An absent ``rate`` (nobody filled it in) and an explicit 0 (the gun really
	was free) both landed as 0.0, and once written they were indistinguishable
	forever — 11 real purchases went in that way over three months, and every
	later stock-cost audit had to ask the owner gun by gun.

	The three legitimate shapes, kept apart on purpose:

	  * a consignor's gun — the store never bought it, $0 needs no confirmation
	    (the case the original code declined to add a blanket
	    "cost > 0" rule for, and it must keep working untouched);
	  * a real purchase that really was free — allowed, but the receiver has to
	    SAY so and give a reason, which lands on the Serial No as a queryable
	    fact so a later audit can skip the gun without a human;
	  * a real purchase with a cost — must carry one.

	Anything else (missing, blank, 0-without-confirmation, negative) is refused
	here, naming the serial so the receiver knows which card to fix.

	Who reaches which arm: the Receive Goods page always sends a number (an
	empty cost box arrives as 0), so a human who skipped the box lands on the
	"$0 needs confirming" arm. The "no cost entered" arm is for the payload
	callers — the MCP receive tool, and the distributor bridge, which since
	2026-08-08 OMITS rate rather than inventing 0 for a DO line whose unit cost
	nobody knows (``receive_bridge._shape_receive_payload``).

	``flt`` / ``cint`` rather than ``float()`` / truthiness, deliberately: this
	function's whole job is a friendly refusal, so a typed "$500" or "1,200"
	must not become an uncaught ValueError → HTTP 500, and the string "0" or
	"false" (both truthy in Python, both arriving from JSON) must not be able to
	open the escape hatch. Anything ``flt`` cannot read is 0.0, which lands on
	the confirm-or-enter-a-cost arm — fail closed.

	Returns ``(rate, zero_cost_confirmed, zero_cost_reason, zero_cost_note)``.
	"""
	label = ", ".join(s.strip() for s in (serials_hint or []) if s and s.strip()) or item_code
	raw = line.get("rate")
	confirmed = cint(line.get("zero_cost_confirmed"))
	reason = (line.get("zero_cost_reason") or "").strip() or None
	note = (line.get("zero_cost_note") or "").strip() or None

	# A blank string is "nobody filled it in", not "zero" — including "  ",
	# which the old `raw in (None, "")` test let through to float().
	if raw is None or (isinstance(raw, str) and not raw.strip()):
		if is_consignment_receipt:
			return 0.0, 0, None, None
		frappe.throw(_(
			"Row #{0}: no cost entered for {1}. Enter what the store paid, or tick "
			"<b>Cost really was $0</b> and pick a reason if it was free."
		).format(idx, label))

	rate = flt(raw)
	if rate < 0:
		frappe.throw(_("Row #{0}: cost for {1} cannot be negative.").format(idx, label))
	if rate > 0:
		# A price and a "it was free" tick contradict each other; trust neither.
		if confirmed:
			frappe.throw(_(
				"Row #{0}: {1} is marked as $0 but carries a cost of {2}."
			).format(idx, label, rate))
		return rate, 0, None, None

	# rate == 0 from here.
	if is_consignment_receipt:
		return 0.0, 0, None, None
	if not confirmed:
		frappe.throw(_(
			"Row #{0}: cost of $0 for {1} needs confirming. Tick <b>Cost really was $0</b> "
			"and pick a reason, or enter what the store paid."
		).format(idx, label))
	if not reason:
		frappe.throw(_(
			"Row #{0}: {1} is marked as costing $0 — a reason is required so a later "
			"stock-cost audit does not have to ask."
		).format(idx, label))
	if reason not in ZERO_COST_REASONS:
		frappe.throw(_("Row #{0}: {1} is not a valid zero-cost reason.").format(idx, reason))
	# "Other" is the escape hatch for a case nobody foresaw. Without the written
	# explanation it says no more to a later audit than an empty cost did — which
	# is the ambiguity this whole guard exists to remove.
	if reason == OTHER_REASON and not note:
		frappe.throw(_(
			"Row #{0}: {1} is marked as costing $0 for '{2}' — say in one "
			"line what actually happened, so a later audit does not have to ask."
		).format(idx, label, OTHER_REASON))
	return 0.0, 1, reason, note


@frappe.whitelist()
def get_receive_sources() -> list[str]:
	"""Source dropdown values for Receive Goods + FFL Acquisition + Purchase
	Receipt.

	Admin-edited in FFL Settings → Receive Sources (one per line).

	Returns:
	    List of source strings, in configured order.
	"""
	frappe.only_for(COUNTER_ROLES)
	raw = frappe.db.get_single_value("FFL Settings", "receive_sources") or ""
	return [s.strip() for s in raw.split("\n") if s.strip()]


def _firearm_fields(src) -> dict:
	"""Firearm-detail fields the Receive Goods card prefills + confirms.

	`src` may be a dict (SQL row / get_value) or a Frappe Document — both expose .get().

	importer is deliberately absent (2026-08-20): it is a per-gun marking, so
	the card starts it blank and the operator states it for THIS gun. An
	Item-level value must never ride in as a prefill — on generic surplus
	buckets it is one unit's marking masquerading as the model's. The line's
	typed importer still flows to the Serial No via _FIREARM_DETAIL_FIELDS.
	"""
	return {f: src.get(f) for f in _FIREARM_DETAIL_FIELDS if f != "importer"}


def _set_item_selling_price(item_code: str, price: float) -> None:
	"""Stamp `price` onto Item.standard_rate AND the selling Item Price on the
	default price list, creating OR updating the price so the Receive Goods Sell
	Price is authoritative at POS. Targeted writes (no full Item.save) so unrelated
	Item on_update hooks don't fire as a side effect of a price change."""
	if float(frappe.db.get_value("Item", item_code, "standard_rate") or 0) != price:
		frappe.db.set_value("Item", item_code, "standard_rate", price, update_modified=False)
	if price <= 0:
		return  # 0 = un-priced by convention; never create or zero an Item Price
	from ffl_core.item_hooks import _default_selling_price_list
	price_list = _default_selling_price_list()
	if not price_list:
		return
	# The Item Price is what the register charges TODAY: on a uniform-priced
	# item an ACTIVE sale window undercuts the regular price (same overlay the
	# serial picker applies), so stamping the raw regular here mid-sale would
	# flip the POS card back to full price until the daily re-sync.
	charge = price
	meta = frappe.db.get_value(
		"Item", item_code,
		["uniform_sell_price", "sale_price", "sale_from", "sale_to"], as_dict=True)
	if meta and meta.uniform_sell_price:
		from ffl_core.firearm import apply_sale
		charge = float(apply_sale(
			price, meta.sale_price, meta.sale_from, meta.sale_to) or price)
	existing = frappe.db.get_value(
		"Item Price", {"item_code": item_code, "price_list": price_list, "selling": 1}, "name")
	if existing:
		frappe.db.set_value("Item Price", existing, "price_list_rate", charge)
	else:
		frappe.get_doc({
			"doctype": "Item Price", "item_code": item_code, "price_list": price_list,
			"selling": 1, "price_list_rate": charge,
		}).insert(ignore_permissions=True)


def _rsr_dealer_price(item_code: str) -> float:
	"""Resolve RSR dealer_price for an Item if it's RSR-linked, else 0.0.

	Soft-coupled to ffl_integrations: silently returns 0 when the app isn't
	installed (import fails) so ffl_core stays usable standalone. The lookup
	itself is remote (Data Service)-backed now and already degrades to 0.0 on its
	own if that API call fails — this guard is only for "app not installed".
	"""
	if not item_code:
		return 0.0
	try:
		from ffl_integrations.rsr.promote import rsr_dealer_price
	except ImportError:
		return 0.0
	return rsr_dealer_price(item_code)


def _default_warehouse() -> str:
	from ffl_core.api._helpers import default_warehouse
	return default_warehouse(throw_if_missing=True)


def _assert_transfer_lines_are_firearms(items_in: list) -> None:
	"""A transfer document holds the customer's firearm(s) and nothing else.

	Custody is a property of the SHIPMENT, so ticking the box sends EVERY line
	to the custody warehouse at zero cost — a mixed document would quietly turn
	a purchased accessory into $0 stock the store owns but cannot see. There is
	no legitimate mixed case to protect: a non-firearm here is either the
	customer's own accessory (their property, riding along with their gun, which
	has no business entering our inventory) or goods the store bought (which
	belong on their own receive). Both answers are "split the document", so that
	is what the message says.

	Runs as a PRE-PASS, before anything is created — including the custody
	warehouse itself, whose resolver would otherwise leave a new empty warehouse
	behind on a rejected document."""
	for idx, line in enumerate(items_in, start=1):
		item_code = (line.get("item_code") or "").strip()
		if not item_code:
			continue  # the main loop raises a better error for this
		meta = frappe.db.get_value(
			"Item", item_code, ["is_firearm", "item_group", "has_serial_no"],
			as_dict=True, cache=True)
		if not meta:
			continue  # likewise
		if not (_is_firearm_row(meta) and meta.has_serial_no):
			frappe.throw(_(
				"Row #{0}: '{1}' is not a serialized firearm, so it cannot be received "
				"on an FFL Transfer. A transfer holds only the customer's firearm(s) — "
				"purchased goods belong on a separate receive document."
			).format(idx, item_code))


def _drop_no_accounting_notice(mark: int) -> None:
	"""A customer-custody receipt is $0 by design (never valued), so it posts no GL
	entry, and ERPNext's Purchase Receipt says so for every zero-valued row
	(make_item_gl_entries: "No accounting entries for the following warehouses: …")
	— a popup on every gunsmith / transfer custody receive that reads like a missing
	account. Only that notice, only from this submit, is dropped; the receipt and its
	(absent) ledger are untouched."""
	log = frappe.local.message_log
	if not log:
		return
	text = _("No accounting entries for the following warehouses")
	log[mark:] = [m for m in log[mark:] if text not in str(m.get("message") if isinstance(m, dict) else m)]


def _custody_warehouse() -> str:
	"""The company's customer-custody warehouse, created on first use.

	Company is resolved the same way `_default_warehouse` resolves it (the user
	default, then the site default) so a transfer lands in the same company's
	warehouse tree as an ordinary receive; ERPNext's own submit-time validation
	catches a warehouse/company mismatch if a site ever diverges."""
	from ffl_core.warehouses import ensure_custody_warehouse
	company = frappe.defaults.get_user_default("Company") \
		or frappe.db.get_single_value("Global Defaults", "default_company")
	if not company:
		frappe.throw(_(
			"No default Company is configured, so the customer-custody warehouse "
			"cannot be resolved. Set one before receiving a transfer."))
	return ensure_custody_warehouse(company)
