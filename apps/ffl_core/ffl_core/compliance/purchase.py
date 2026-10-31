"""Purchase Receipt firearm-compliance hooks.

Server-side guards on top of ERPNext's standard PR machinery:
  - validate: firearm lines require an FFL-verified supplier.
  - on_submit (Cut 4): create FFL Acquisition records for each firearm Serial No.
  - on_cancel (Cut 8): cancel the Acquisitions and tell FastBound to delete them,
    and put back the ownership stamps of each gun's stay before.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

from ffl_core.acquisition import TRADE_IN_SOURCE, is_trade_in_source, record_firearm_acquisition
from ffl_core.compliance.pay_on_sale import booked_since
from ffl_core.serials import receipt_serials, receipts_of, serials_for_line
from ffl_core.setup.zero_cost import OTHER_REASON, ZERO_COST_REASONS
from ffl_core.warehouses import is_custody_warehouse
from ffl_core.firearm import (
	_close_service_todos,
	_ensure_service_todo,
	audit_trace,
	is_antique_item,
	is_consignment_acquisition_type,
	is_firearm_item,
	is_transfer_intake,
	log_zero_cost_confirmation,
)

# 'Pending' is acceptable: supplier created or imported but never verified —
# don't block a clerk's first PR. 'Failed' / 'Expired' must be re-verified.
ACCEPTABLE_EZ_CHECK = frozenset({"Verified", "Pending"})

# Who a stay of the gun belongs to, at "the store's own gun": every receipt
# starts each serial from here and stamps the owner THIS stay has — a
# consignor, or a pay-on-sale supplier, never both (see the stamping block).
_STORE_OWNED = {
	"is_consignment": 0, "consignment_commission_pct": 0, "consignor": None,
	"pay_on_sale": 0, "pay_on_sale_supplier": None, "srbnb_cost": 0, "pay_on_sale_settled": 0,
}
# The title of the parked-cost trace: the Error Log's title (kept ~14 days by
# default), and the first words of the Serial No comment a bookkeeper searches
# for at month-end (Comment list, Reference Type = Serial No).
PARKED_COST_LEFT = "Pay-on-sale stamp replaced with its cost still parked in SRBNB"
# The title of the note a cancelled receipt leaves on each gun it gave back.
STAMPS_PUT_BACK = "Ownership stamps put back after a cancelled receipt"


def require_ffl_supplier_for_firearm_lines(doc, method: str | None = None) -> None:
	"""Reject Purchase Receipts that put firearms into stock from a supplier
	that isn't an FFL dealer (or whose EZ Check failed/expired)."""
	firearm_rows = [r for r in (doc.items or []) if is_firearm_item(r.item_code)]
	if not firearm_rows:
		return

	# Individual-source receipts (bought from a private party) have no FFL
	# supplier — the seller's identity + ID are recorded on the FFL Acquisition
	# instead (see create_acquisitions_for_firearm_lines). Skip the FFL checks.
	if (getattr(doc, "ffl_acquisition_source", None) or "") == "Individual":
		return

	supplier_name = doc.supplier
	if not supplier_name:
		frappe.throw(_("Cannot receive firearm without a Supplier."))

	supplier = frappe.db.get_value(
		"Supplier",
		supplier_name,
		["is_ffl_dealer", "ffl_number", "ez_check_status"],
		as_dict=True,
	) or {}

	if not supplier.get("is_ffl_dealer"):
		frappe.throw(_(
			"Cannot receive firearm from supplier '{0}' — not registered as FFL Dealer. "
			"Open the Supplier form and use 'Search ATF' to populate FFL information."
		).format(supplier_name))

	if not (supplier.get("ffl_number") or "").strip():
		frappe.throw(_(
			"Supplier '{0}' is marked as FFL Dealer but has no FFL Number on file."
		).format(supplier_name))

	status = supplier.get("ez_check_status") or "Pending"
	if status not in ACCEPTABLE_EZ_CHECK:
		frappe.throw(_(
			"Supplier '{0}' EZ Check status is '{1}'. Re-verify the FFL before receiving firearms."
		).format(supplier_name, status))


def create_acquisitions_for_firearm_lines(doc, method: str | None = None) -> None:
	"""For each firearm line on the submitted PR, create one FFL Acquisition
	per physical unit — keyed by the per-line Serial No(s)."""
	# Receipt-level, so it is resolved once rather than per row: a transfer is a
	# property of the shipment, not of a line.
	#
	# Two answers again, split the same way as in the receive API:
	# `is_transfer` is PROTECTIVE — it only suppresses the Cost mirror onto the
	# shared SKU, so it reads the soft markers too and over-triggers harmlessly.
	# `is_custody` is DESTRUCTIVE — it marks the gun permanently unsellable, so
	# it answers to the recorded checkbox alone. Prod uses the "FFL Transfer"
	# AcquireType for real purchases, and flagging one of those
	# would quietly strand bought inventory nobody can sell.
	is_transfer = is_transfer_intake(
		doc.get("ffl_acquisition_type"), doc.get("ffl_transfer_for_customer"),
		doc.get("is_ffl_transfer"),
	)
	is_custody = bool(doc.get("is_ffl_transfer"))
	# A purchase return sends guns back: a disposition, not an acquisition. It
	# rewrites nothing about the gun's stay below —
	# not its owner, and not its cost either (a PS-7 corrected purchase_rate
	# would otherwise fall back to the receipt's).
	acquiring = not cint(doc.get("is_return"))
	# The amendment of a cancelled receipt replaces what its predecessor replaced
	# — traced then — or, for a receipt cancelled before a cancel put the stamps
	# back, the predecessor's own stamp: no new trace either way.
	predecessor = {s.upper() for s in receipt_serials(doc.amended_from)} \
		if acquiring and doc.get("amended_from") else set()
	# Source kwargs differ by acquisition source: an FFL purchase records the
	# dealer's FFL; an individual purchase records the seller (Customer) + their
	# ID + a scan of the ID document. Computed once, applied to every unit.
	if (getattr(doc, "ffl_acquisition_source", None) or "") == "Individual":
		cust = doc.get("ffl_seller_customer")
		cust_name = frappe.db.get_value("Customer", cust, "customer_name") if cust else None
		source_kwargs = dict(
			source_type=TRADE_IN_SOURCE if is_trade_in_source(doc.get("ffl_source")) else "Individual",
			source_name=cust_name or cust,
			source_customer=cust,
			source_id_type=doc.get("ffl_seller_id_type"),
			source_id_number=doc.get("ffl_seller_id_number"),
			source_id_document=doc.get("ffl_seller_id_document"),
		)
	else:
		supplier = frappe.db.get_value(
			"Supplier", doc.supplier,
			["ffl_number", "supplier_name", "licensee_address"], as_dict=True,
		) or {}
		source_kwargs = dict(
			source_type="FFL Dealer",
			source_name=supplier.get("supplier_name") or doc.supplier,
			source_ffl=supplier.get("ffl_number"),
			source_address=supplier.get("licensee_address"),
		)

	for row in (doc.items or []):
		if not is_firearm_item(row.item_code):
			continue

		# A row cannot be BOTH confirmed-free and priced. The receive API refuses
		# that shape at its door (_resolve_line_cost), but a receipt built by
		# REST / data import / a script never passes that door, and this hook
		# would then write the two halves of the contradiction to different
		# places: purchase_rate = 0 here ("free"), and — via the pay-on-sale
		# block below, which keys off row.rate — srbnb_cost = the very rate we
		# just called zero ("I owe the supplier that much"). The gun would carry
		# both stories at once while zero_cost_confirmed=1 hides it from the
		# stock-cost sweep that exists to find exactly this.
		#
		# The comparison belongs HERE and not in validate: at validate time
		# ERPNext has not yet zeroed a genuine is_free_item row's rate (it still
		# holds the price-list figure), so the check would reject the one path
		# this feature exists to allow. By on_submit the number has settled —
		# which is why the stamping block below already trusts row.rate.
		if row.get("ffl_zero_cost_confirmed") and row.rate is not None \
				and float(row.rate) > 0:
			frappe.throw(_(
				"Receipt {0} line {1} ({2}) is marked as a confirmed free firearm but "
				"carries a cost of {3}. A gun is either free or priced, never both — "
				"clear the free marking or remove the cost before submitting."
			).format(doc.name, row.idx,
				", ".join(serials_for_line(row)) or row.item_code, flt(row.rate, 2)))

		# Mirror the receive cost onto Item.dealer_price — this site's Property
		# Setter relabels dealer_price as "Cost" in the Item form's Pricing
		# section. Without this stamp, Cost reads 0 even though ERPNext's own
		# last_purchase_rate is updating correctly. last-write-wins per row
		# (matches last_purchase_rate semantics).
		#
		# NOT on a transfer: we did not buy a customer's gun, so the number
		# in the cost box is a placeholder for one gun's paperwork, not what the
		# model costs us. On 2026-07-19 a $10 transfer placeholder became the
		# Cost of a Glock 19 SKU. The per-SERIAL purchase_rate below still
		# records it, so the transfer's own paperwork is unaffected.
		if row.rate is not None and float(row.rate) > 0 and not is_transfer:
			frappe.db.set_value(
				"Item", row.item_code, "dealer_price", float(row.rate),
				update_modified=False,
			)

		serials = serials_for_line(row)
		# Antique firearms (pre-1899 / GCA-exempt) get NO bound-book acquisition.
		# Skipping FFL Acquisition creation also cuts the FastBound push at its
		# only upstream source (the Acquisition's on_submit enqueues the push).
		# Antiques are still serialized inventory, so the per-serial bookkeeping
		# below (cost / sell_price / needs_service) still runs for them.
		if not is_antique_item(row.item_code):
			for serial in serials:
				# record_firearm_acquisition submits the FFL Acquisition, whose
				# on_submit hook (ffl_integrations) enqueues the FastBound push
				# after commit. Do NOT enqueue here too — that double-pushed every
				# received firearm (duplicate A&D entry / 'externalId already used').
				record_firearm_acquisition(
					item_code=row.item_code,
					serial=serial,
					cost=row.rate or 0,
					company=doc.company,
					purchase_receipt=doc.name,
					purchase_order=row.get("purchase_order"),
					receive_source=doc.get("ffl_source"),
					transaction_type=doc.get("ffl_acquisition_type"),
					transfer_for_customer=doc.get("ffl_transfer_for_customer"),
					**source_kwargs,
				)

		if not acquiring:
			continue
		# Stamp per-serial bookkeeping ONCE per row (outside the per-serial
		# acquisition loop above — otherwise this block ran N times per
		# firearm row, re-writing every Serial No N×N times). Drives the
		# Item form's Serial Numbers tab + the POS price-picker.
		#
		# Used firearms (has_serial_no=0) have no Serial No record; for those
		# the flags stay on the PR Item line.
		sn_updates: dict = _acquired_stamps(doc, row)
		zero_confirmed = bool(sn_updates["zero_cost_confirmed"])
		# `is not None` (not truthy) — a legitimate $0 sample / warranty
		# firearm should still stamp sell_price=0 onto the Serial No.
		if row.get("ffl_sell_price") is not None:
			sn_updates["sell_price"] = float(row.ffl_sell_price)
		if row.get("ffl_dealer_price") is not None:
			sn_updates["dealer_sell_price"] = float(row.ffl_dealer_price)
		# Always stamp needs_service explicitly (0 or 1) so an amendment that
		# CLEARS the flag actually clears it — otherwise a previously-set
		# needs_service=1 + Open ToDo would linger forever after the gunsmith
		# fixed the firearm and the operator amended ffl_service_need back to 0.
		needs_service = 1 if row.get("ffl_service_need") else 0
		sn_updates["needs_service"] = needs_service
		# The ownership stamps are rewritten on EVERY receipt too — cleared, then
		# set for this stay's owner below. A gun the store sold and later takes in
		# again (a trade-in, a repurchase) is a new acquisition, and a stamp left
		# from its previous stay bills the old owner again: post_consignment_payable
		# books a consignor payable on every sale of an is_consignment serial, and
		# the return of the NEXT sale of a still-stamped, settled gun reversed the
		# pay-on-sale supplier's payable of the EARLIER one. A sales return never
		# reaches this hook, so a returned gun keeps its stamps, as it must.
		# lazy: a gun re-entering stock OUTSIDE a Purchase Receipt (a manual
		# Material Receipt, the FastBound reconcile) keeps its old stamps. Month-end
		# check until then, on Serial Nos in stock (Active): (a) is_consignment=1 while
		# the gun's latest submitted FFL Acquisition is not a Consignment one -> it is
		# the store's own gun, clear the consignment stamp; (b) pay_on_sale=1 and
		# pay_on_sale_settled=1, excluding guns on a submitted, non-return, undelivered
		# invoice (a special order in transit) -> stale stamp, clear it. Nothing is
		# booked either way. Upgrade path: rewrite the stamps from the FFL Acquisition
		# on_submit, which every entry path shares.
		sn_updates.update(_owner_stamps(doc, row))
		# A hand-built Purchase Receipt can tick the box in Desk without ever
		# passing through the receive API, so the gun gets the flag but NOT the
		# custody warehouse or the zeroed cost — "unsellable, yet sitting in
		# sellable stock carrying a real cost". The audit's strong signal filters
		# on is_transfer_gun=0 and would not see it either, so say so out loud
		# rather than leave a half-state nobody can find.
		if is_custody and row.warehouse and not is_custody_warehouse(row.warehouse):
			frappe.log_error(
				title="Custody receipt landed outside the custody warehouse",
				message=(
					f"Purchase Receipt {doc.name} is marked as an FFL Transfer, but row "
					f"{row.idx} ({row.item_code}) was received into {row.warehouse!r}, "
					"which is not a customer-custody warehouse.\n\n"
					"Its serial(s) are flagged unsellable while physically sitting in "
					"sellable stock, most likely still carrying a real cost. This happens "
					"when a Purchase Receipt is built by hand instead of through Receive "
					"Goods. Move the gun(s) to the custody warehouse, or clear the "
					"FFL Transfer flag if this was an ordinary purchase."
				),
			)
		if sn_updates:
			for sn in serials:
				old = frappe.db.get_value("Serial No", sn,
					["pay_on_sale", "pay_on_sale_supplier", "srbnb_cost", "pay_on_sale_settled"], as_dict=True)
				if old is not None:
					if sn.upper() not in predecessor:
						_note_parked_cost_left(sn, old, doc)
					frappe.db.set_value(
						"Serial No", sn,
						sn_updates, update_modified=False,
					)
					# update_modified=False leaves no Version row, so the claim
					# "this gun was free" would otherwise have no author and no
					# date — the same hole log_antique_flip was written for.
					if zero_confirmed:
						log_zero_cost_confirmation(
							sn,
							reason=row.get("ffl_zero_cost_reason") or "",
							note=row.get("ffl_zero_cost_note") or "",
							purchase_receipt=doc.name,
						)
					# Idempotent — _ensure_service_todo skips when an Open ToDo
					# already exists; _close_service_todos closes any open ones
					# when the flag was cleared.
					if needs_service:
						_ensure_service_todo(sn)
					else:
						_close_service_todos(sn)


def _acquired_stamps(doc, row) -> dict:
	"""How this receipt row's guns were acquired: their cost (purchase_rate, with
	purchase_document_no), whether they were confirmed free, and whether they are
	a customer's guns held in custody. Written at submit, and rebuilt with the
	owner (_owner_stamps) when a later receipt of the gun is cancelled. What
	else the row stamps — service need, prices — is the gun's current condition,
	edited during a stay, and a cancel leaves it.

	purchase_rate is stamped explicitly because ERPNext's auto-fill from PR
	Item.rate doesn't run when serials arrive as a newline-separated
	`serial_no` string (vs Serial-and-Batch Bundle) — observed empty/0 on
	otherwise-valid receives."""
	stamps: dict = {}
	# A CONFIRMED-free line stamps purchase_rate = 0 EXPLICITLY, for the same
	# reason needs_service is stamped explicitly: so an AMENDMENT that
	# reclassifies a priced line as free actually clears the old cost instead
	# of leaving $500 on a gun the store now says it got for nothing. (It is
	# not about NULL — Currency columns are NOT NULL, so an unstamped rate
	# reads as 0 either way, which is precisely the ambiguity the new flag,
	# not this line, resolves. Verified by injection: removing this stamp
	# changes nothing on a fresh receive.)
	#
	# The whole trio is copied onto each serial FROM THE RECEIPT ROW, so any
	# path that submits a Purchase Receipt carrying it gets the stamp, not
	# only the Receive Goods API.
	# Written explicitly as 0/None rather than only on the truthy branch —
	# the same rule needs_service and is_transfer_gun (below) each spell out,
	# and for the same reason. These fields are read_only on both doctypes
	# and carry no no_copy, so an amendment that reclassifies a receipt back
	# to an ordinary purchase MUST clear them here: otherwise a single
	# mis-tick leaves a $500 gun permanently marked "confirmed free",
	# invisible to the stock-cost sweep, with no form field able to undo it.
	zero_confirmed = bool(row.get("ffl_zero_cost_confirmed"))
	stamps["zero_cost_confirmed"] = 1 if zero_confirmed else 0
	stamps["zero_cost_reason"] = (
		row.get("ffl_zero_cost_reason") or None) if zero_confirmed else None
	stamps["zero_cost_note"] = (
		row.get("ffl_zero_cost_note") or None) if zero_confirmed else None
	if zero_confirmed:
		stamps.update(_cost_stamps(0.0, doc.name))
	elif row.rate is not None and float(row.rate) > 0:
		stamps.update(_cost_stamps(float(row.rate), doc.name))
	# The per-gun custody mark — the recorded checkbox alone (is_custody in the
	# submit hook). Stamped for EVERY firearm on a custody receipt, including
	# antiques (the submit skips their bound-book acquisition, but an antique
	# transfer is still a customer's property and must be just as unsellable).
	# Written explicitly as 0/1 rather than only on the truthy branch so an
	# amendment that reclassifies a receipt back to a purchase actually clears
	# the flag instead of leaving the gun permanently invisible to the
	# storefront.
	stamps["is_transfer_gun"] = 1 if doc.get("is_ffl_transfer") else 0
	return stamps


def _cost_stamps(purchase_rate: float, receipt: str | None) -> dict:
	"""The gun's cost and the receipt that set it. A row that prices nothing and
	is not confirmed free (a consignor's $0 line) writes neither, and the gun
	keeps the cost of the stay before — the Sales Report prices that stay's
	sale at the serial's current purchase_rate. A cancel replays this rule (B2:
	the last receipt that priced the gun; none → 0). Nothing else fills the
	field: ERPNext v16's bundles never write purchase_rate."""
	stamps = {"purchase_rate": purchase_rate}
	# purchase_document_no only exists on some ERPNext v16 builds (it's
	# present on this site's dev image but absent on the CI base image
	# of the same version). Guard the write so the receive flow works
	# on either build — without this, `db.set_value` raises
	# OperationalError 1054 and aborts the PR submit entirely.
	if "purchase_document_no" in frappe.db.get_table_columns("Serial No"):
		stamps["purchase_document_no"] = receipt
	return stamps


def _owner_stamps(doc, row, consignor: str | None = None) -> dict:
	"""Who this receipt's stay of the guns on `row` belongs to: the seven
	ownership stamps, from "the store's own gun" (_STORE_OWNED) plus this stay's
	owner — a consignor, or a pay-on-sale supplier, never both. The submit writes
	them; a later cancel of another receipt rebuilds them from here,
	passing the consignor it found in the records ("" for none): a cancel never
	creates a Supplier or asks for a name."""
	stamps = dict(_STORE_OWNED)
	# Consignment receipts additionally stamp the consignor + commission %
	# onto every serial in the row, so later payout/profit logic never has to
	# re-derive them from the PR.
	# Through firearm.is_consignment_acquisition_type, not an equality: the
	# receive API exempts a consignor's $0 line from the zero-cost guard on
	# the same answer, and a spelling only one of them recognised would let
	# a gun in at $0 with nobody recorded as owning it.
	if is_consignment_acquisition_type(getattr(doc, "ffl_acquisition_type", "")):
		from ffl_core.consignment import ensure_consignor_supplier
		from ffl_core.setup.consignment import default_commission_pct
		# Per-line commission entered on Receive Goods (Source=Consignment)
		# wins; a blank/zero line falls back to the store-wide default.
		row_pct = float(row.get("ffl_consignment_commission_pct") or 0)
		pct = row_pct if row_pct > 0 else default_commission_pct()
		stamps["is_consignment"] = 1
		stamps["consignment_commission_pct"] = float(pct)
		cust = doc.get("ffl_seller_customer")
		stamps["consignor"] = (consignor or None) if consignor is not None else ensure_consignor_supplier(
			frappe.db.get_value("Customer", cust, "customer_name") if cust else None, cust)
	# A pay-on-sale supplier's OWNED gun is received at full cost parked in
	# Stock Received But Not Billed and settled to a named AP only when it
	# sells (compliance/pay_on_sale.py). Stamp the marker HERE at park time
	# — mutually exclusive with the consignment branch above (a gun
	# is owned XOR consigned), hence elif. srbnb_cost = the rate this PR line
	# credits to SRBNB = the exact figure the sale settlement clears,
	# so srbnb_cost == purchase_rate. Guards: FFL-dealer source only (an
	# Individual receipt has no FFL supplier to bill); rate>0 only (a $0
	# receive credits nothing to SRBNB — nothing to settle).
	elif (getattr(doc, "ffl_acquisition_source", None) or "") != "Individual" \
			and doc.get("supplier") and float(row.rate or 0) > 0 \
			and frappe.db.get_value("Supplier", doc.supplier, "pay_on_sale"):
		stamps["pay_on_sale"] = 1
		stamps["pay_on_sale_supplier"] = doc.supplier
		stamps["srbnb_cost"] = float(row.rate)
	return stamps


def _note_parked_cost_left(serial: str, old, doc) -> None:
	"""The stamp this receipt rewrites may still hold a cost parked for a
	pay-on-sale supplier (settled=0, srbnb_cost>0): the gun left without a
	settlement standing — sold and then credited without coming back, or sent
	back to its supplier outside a sale — and now comes in again.
	The receipt is not blocked (the acquisition must be recorded)
	and nothing is booked here: whether that parked cost is owed depends on
	where the gun went, which the bookkeeper resolves by hand from this trace
	at month-end — the Info comment on the Serial No is the permanent copy
	(firearm.audit_trace).
	The amendment of a cancelled receipt is not such a case: the caller skips the
	guns its predecessor took in."""
	if not cint(old.pay_on_sale) or cint(old.pay_on_sale_settled) or flt(old.srbnb_cost) <= 0:
		return
	body = _("{0}: {1} — its pay-on-sale stamp from {2} was replaced by Purchase Receipt {3} while {4} of its "
		"cost was still parked in Stock Received But Not Billed, unsettled. Nothing was booked: at month-end "
		"close, check by hand whether {2} is owed it.").format(
			PARKED_COST_LEFT, serial, old.pay_on_sale_supplier or _("(no supplier)"), doc.name, flt(old.srbnb_cost, 2))
	audit_trace("Serial No", serial, PARKED_COST_LEFT, body)


def restore_stay_on_cancel(doc, method: str | None = None) -> None:
	"""A cancelled receipt never happened, so each
	gun it took in answers again to the stay before it — rebuilt from the
	records, not from a copy kept on the receipt. The gun's latest standing
	receipt says how that stay was acquired and who owned it (_acquired_stamps,
	_owner_stamps: the branches its submit took); what was booked since says the
	rest — a pay-on-sale stay's settlement and parked cost, a consignment stay's
	consignor. With no standing receipt the gun is the store's own. A purchase
	return rewrote nothing, so its cancel puts nothing back. ERPNext refuses
	to cancel a receipt past a later movement of its serial, so the stay before
	it is the one this receipt ended. What fails here fails the cancel, as in the
	bound-book hook before it: an error is never swallowed, because a database
	error (a deadlock) has already rolled the whole cancel back by then, and a
	receipt that reads as cancelled while it stands is worse than one that stays."""
	if cint(doc.get("is_return")):
		return
	receipts: dict = {}  # a big receipt gives back many guns from one earlier receipt
	for serial, item_code in receipt_serials(doc.name).items():
		if is_firearm_item(item_code):
			_put_stay_back(serial, doc, receipts)


def _put_stay_back(serial: str, doc, receipts: dict) -> None:
	stays = []  # (receipt, row) of the gun's other standing receipts, latest first
	for r in receipts_of(serial):
		if r.voucher_no == doc.name:
			continue
		prev = receipts.get(r.voucher_no) or receipts.setdefault(
			r.voucher_no, frappe.get_doc("Purchase Receipt", r.voucher_no))
		row = next((x for x in prev.items if x.name == r.voucher_detail_no), None)
		if row:
			stays.append((prev, row))
	# No stay before it: the store's own gun, acquired by no receipt, at no cost.
	stamps = {**_STORE_OWNED, **_acquired_stamps(frappe._dict(), frappe._dict()), **_cost_stamps(0.0, None)}
	source = since = None
	for i, (prev, row) in enumerate(stays):
		acquired = _acquired_stamps(prev, row)
		if i == 0:
			source = prev.name
			since = frappe.db.get_value("Stock Ledger Entry", {"voucher_no": prev.name,
				"voucher_detail_no": row.name, "is_cancelled": 0}, "creation")
			consignor = None
			if is_consignment_acquisition_type(prev.get("ffl_acquisition_type")):
				# Whom that stay's sale booked the consignment payable to, else the
				# consignor Supplier by the seller's name — never a new one.
				booked = since and booked_since(serial, since, "ffl_consignment_source")
				cust = prev.get("ffl_seller_customer")
				name = ((cust and frappe.db.get_value("Customer", cust, "customer_name")) or "").strip()
				consignor = booked.party if booked else (
					name and frappe.db.get_value("Supplier", {"supplier_name": name})) or ""
			stamps.update({**acquired, **_owner_stamps(prev, row, consignor=consignor)})
		if "purchase_rate" in acquired:
			# The cost is the last receipt's that wrote one — a consignor's $0 line
			# writes none and leaves the stay before it's (_cost_stamps).
			stamps.update({k: v for k, v in acquired.items() if k in ("purchase_rate", "purchase_document_no")})
			break
	if stamps["pay_on_sale"]:
		booked = since and booked_since(serial, since, party=stamps["pay_on_sale_supplier"])
		stamps["pay_on_sale_settled"] = 1 if booked and flt(booked.credit) > 0 else 0
		if booked:
			# What its settlement (or the reversal of it) booked: the parked cost
			# as it stood, a PS-7 correction included.
			stamps["srbnb_cost"] = stamps["purchase_rate"] = flt(booked.credit or booked.debit)
	# lazy: an owned stay's PS-7 correction is not seen (its receipt's rate comes
	# back), nor a pay-on-sale flag or default commission changed since that
	# receipt (read as they are now); upgrade path: read the value the gun left
	# at off its last outward bundle, and the flag off the Supplier's versions.
	current = frappe.db.get_value("Serial No", serial, list(stamps), as_dict=True)
	if current is None or all((current[k] or 0) == (v or 0) for k, v in stamps.items()):
		return
	frappe.db.set_value("Serial No", serial, stamps, update_modified=False)
	_note_stamps_put_back(serial, doc, source, stamps)


def _note_stamps_put_back(serial: str, doc, source: str | None, stamps: dict) -> None:
	"""The rebuilt stamps replace whatever was on the gun — a commission % edited
	by hand included (accepted on the record)."""
	if stamps["pay_on_sale"]:
		owner = _("pay-on-sale supplier {0} ({1}, {2})").format(stamps["pay_on_sale_supplier"],
			flt(stamps["srbnb_cost"], 2), _("settled") if stamps["pay_on_sale_settled"] else _("unsettled"))
	elif stamps["is_consignment"]:
		owner = _("consignor {0} at {1}%").format(stamps["consignor"] or _("(not found)"),
			flt(stamps["consignment_commission_pct"], 2))
	else:
		owner = _("the store, as its own gun")
	body = _("{0}: {1} — Purchase Receipt {2} was cancelled, so the gun answers again to {3}, from {4}. "
		"This is a record only; nothing was booked.").format(STAMPS_PUT_BACK, serial, doc.name, owner,
			_("Purchase Receipt {0}").format(source) if source else _("no standing receipt"))
	audit_trace("Serial No", serial, STAMPS_PUT_BACK, body)


def cancel_acquisitions_for_firearm_lines(doc, method: str | None = None) -> None:
	"""Cancel every submitted FFL Acquisition linked to this PR; enqueue a
	FastBound DELETE for each that had been pushed."""
	rows = frappe.get_all(
		"FFL Acquisition",
		filters={"purchase_receipt": doc.name, "docstatus": 1},
		fields=["name", "fastbound_id"],
	)
	for row in rows:
		acq = frappe.get_doc("FFL Acquisition", row.name)
		acq.flags.ignore_permissions = True
		acq.cancel()
		if row.fastbound_id:
			frappe.enqueue(
				"ffl_integrations.fastbound.tasks.delete_acquisition_from_fastbound",
				queue="long",
				acquisition_name=row.name,
				fastbound_id=row.fastbound_id,
			)


def validate_zero_cost_confirmation(doc, method=None):
	"""A receipt row that CLAIMS the acquisition was free must say why.

	``receive_goods._resolve_line_cost`` already enforces this at the API door,
	but the stamp is copied onto each serial FROM THE RECEIPT ROW at on_submit —
	and a Purchase Receipt can be built without ever passing that door (by hand
	in Desk, by an import, by a script). Without this check
	``zero_cost_confirmed=1`` can land with a NULL reason: the gun then hides
	from the stock-cost sweep with nothing at all behind the claim, and because
	the fields are read_only there is no form control to undo it.

	Deliberately narrow — it constrains ONLY rows that already carry the tick.
	A row without it is untouched, so no other receive path (4473, custody,
	trade-in, distributor, backfill) needs an exemption to keep working. That
	is what makes it safe to run on every Purchase Receipt.
	"""
	for row in doc.get("items") or []:
		if not row.get("ffl_zero_cost_confirmed"):
			continue
		label = row.get("item_code") or _("row #{0}").format(row.idx)
		reason = (row.get("ffl_zero_cost_reason") or "").strip()
		if not reason:
			frappe.throw(_(
				"{0} is marked as costing $0 but carries no reason \u2014 a later "
				"stock-cost audit would have nothing to read."
			).format(label))
		if reason not in ZERO_COST_REASONS:
			frappe.throw(_("{0}: '{1}' is not a valid zero-cost reason.").format(label, reason))
		if reason == OTHER_REASON and not (row.get("ffl_zero_cost_note") or "").strip():
			frappe.throw(_(
				"{0}: reason \u201cOther\u201d needs an explanation."
			).format(label))
		# Deliberately NOT checking "rate must be 0" here. This hook runs before
		# ERPNext has zeroed an is_free_item row's rate from the price list, so
		# the value read at validate time is the list price, not the $0 the
		# receiver confirmed — asserting on it fails the very path this feature
		# exists to allow (caught in test: three legitimate free receives
		# errored). The rate/confirmed exclusion is enforced where the number is
		# actually final: receive_goods._resolve_line_cost, at the API door.
