"""POS-driven 4473 kick-off.

4473s aren't API-createable (POST /Form4473s is 405; a typed disposition
won't be promoted by the FB UI). So we hold the order in Draft + send the
cashier to FastBound's /Form4473s page; FB fires `form4473.completed` when
it's done, and the webhook handler matches the disposition's items back
to this invoice by serial. Submit gating lives in
`ffl_core.compliance.firearm.require_4473_and_nics_for_firearm_sale`.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from ffl_core.utils import commit, split_serials
from ffl_core.compliance.disposition_lock import book_disposition_once
from ffl_core.compliance.mode import paper_4473_mode
from ffl_core.ffl_core.doctype.form_4473_log.form_4473_log import record_4473
from ffl_core.roles import COUNTER_4473_ROLES
from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError, FastBoundError
from ffl_integrations.integration_gate import fastbound_enabled

from ffl_core.firearm import is_antique_item, is_firearm_item, is_regulated_firearm_item


def _check_perm() -> None:
	roles = set(frappe.get_roles())
	if not (roles & set(COUNTER_4473_ROLES)):
		frappe.throw(_("4473s require the Sales User role or above."),
			frappe.PermissionError)


def _firearm_rows(doc) -> list:
	"""Return the cart rows that need a 4473 — regulated firearms only (antiques
	are GCA-exempt and never start a 4473)."""
	return [row for row in (doc.items or []) if is_regulated_firearm_item(row.item_code)]


def _validate_serials(firearm_rows: list, serial_by_item_row: dict) -> None:
	for row in firearm_rows:
		serial = (serial_by_item_row.get(row.name) or "").strip()
		if not serial:
			frappe.throw(_("Missing serial for firearm row {0} ({1}).").format(
				row.idx, row.item_code))


def _firearm_serial_map(rows: list) -> dict:
	"""{row.name: serial} for EVERY firearm row that carries a serial — existing
	rows on a restored/parked transfer as well as freshly-added ones. The 4473
	kickoff's _validate_serials checks all firearm rows, so mapping only the
	newly-sent serials would throw 'Missing serial for firearm row' on a gun
	already on the invoice (breaking 'add a second gun to a parked transfer')."""
	out: dict = {}
	for row in rows:
		ser = (row.serial_no or "").strip()
		if ser and is_regulated_firearm_item(row.item_code):
			out[row.name] = ser
	return out


def _stamp_serials_and_save(doc, serial_by_item_row: dict) -> None:
	for row in (doc.items or []):
		new_serial = (serial_by_item_row.get(row.name) or "").strip()
		if not new_serial:
			continue
		# Multi-unit SKU (has_serial_no=1): write to row.serial_no — ERPNext
		# expects newline-separated serials on the line.
		# Used Firearm (has_serial_no=0, firearm_serial on Item): the Item
		# already carries the serial, but write it to the row too for
		# disposition record clarity.
		row.serial_no = new_serial
	doc.flags.ignore_permissions = True
	# start_4473 is a sanctioned mutator of the webhook-owned 4473 group (it
	# re-holds the order at Pending right after this save); opt out of the
	# stale-save guard so re-opening a 4473 on an already-completed order is never
	# blocked (P1).
	doc.flags.ignore_webhook_4473_guard = True
	doc.save()


def _fb_form4473_list_url(account_num: str) -> str:
	return f"https://cloud.fastbound.com/{account_num}/Form4473s"


@frappe.whitelist()
def start_4473_for_pos_invoice(
	invoice_name: str,
	serial_by_item_row: dict | str,
) -> dict:
	"""Hold a POS / Sales Invoice in Draft while the cashier completes a 4473
	in FastBound's UI.

	Args:
		invoice_name: POS Invoice or Sales Invoice docname.
		serial_by_item_row: {pos_invoice_item.name: "ACTUAL-SERIAL"}.
			Frappe transports dicts as JSON strings; we deserialize defensively.

	Returns:
		{ok: True, fastbound_url, n_firearms}
	"""
	_check_perm()

	if isinstance(serial_by_item_row, str):
		import json
		serial_by_item_row = json.loads(serial_by_item_row or "{}")

	invoice_name = (invoice_name or "").strip()
	if not invoice_name:
		frappe.throw(_("invoice_name is required."))

	doctype = "POS Invoice" if frappe.db.exists("POS Invoice", invoice_name) else "Sales Invoice"
	doc = frappe.get_doc(doctype, invoice_name)  # raises DoesNotExistError if missing in either

	if doc.docstatus == 1:
		frappe.throw(_("Cannot start a new 4473 for a submitted invoice."))

	# A 4473 that ALREADY completed must never be silently re-opened. The webhook
	# stamps Completed with db.set_value, so a POS page loaded BEFORE it landed
	# still renders the orange "Awaiting 4473" button; clicking it used to reset
	# the order to Pending and block Complete Order.
	# Read the DB — never the incoming doc, which is exactly the stale party.
	# A genuine redo goes through pos_4473_recovery.manager_redo_4473: manager-gated,
	# reason-logged, and it cancels the disposition ledger too.
	if (frappe.db.get_value(doctype, invoice_name, "form_4473_status") or "") == "Completed":
		frappe.throw(
			_("This order's 4473 is already complete. Click Refresh 4473 to pick up "
				"the latest status. If it genuinely has to be redone, void the "
				"disposition in FastBound and have a manager run Redo 4473."),
			title=_("4473 Already Complete"),
		)

	firearm_rows = _firearm_rows(doc)
	if not firearm_rows:
		frappe.throw(_("No firearm items in this invoice — 4473 is not needed."))

	_validate_serials(firearm_rows, serial_by_item_row)
	_stamp_serials_and_save(doc, serial_by_item_row)

	# Where the 4473 actually happens decides what "start" even means here.
	if paper_4473_mode():
		# Paper mode: there is no wizard to send anyone to. Starting a 4473 is just
		# HOLDING the ticket — the cashier fills the paper form in, then records it
		# with attest_paper_4473. This branch is also what keeps the customer
		# transfer-in flow alive on a store with no FastBound account:
		# start_transfer_for_pos_invoice ends by calling this function, so a hard
		# requirement on account_num here took the whole transfer service (and the
		# only place is_ffl_transfer is ever set) down with it.
		fastbound_url = ""
	elif not fastbound_enabled():
		# Credentials outlive the decision to stop using the vendor, and the
		# doctype only makes them mandatory while the integration is on — so
		# account_num is usually still filled in and cannot be the thing that stops
		# us. Refuse explicitly instead: sending a cashier to complete a real 4473
		# in a FastBound account whose webhook this site now refuses would split
		# the record across two books and strand the order at Pending forever.
		frappe.throw(_(
			"FastBound is switched off on this site, so a 4473 cannot be started "
			"there. If this store keeps paper 4473s, switch on FFL Settings → Paper "
			"4473 Mode and the counter records them instead."),
			title=_("FastBound Is Switched Off"))
	else:
		account_num = frappe.db.get_single_value("FastBound Settings", "account_num")
		if not account_num:
			frappe.throw(_("FastBound Settings.account_num is not configured."))

		# FastBound's API cannot create a pending 4473 disposition: POST /Dispositions
		# requires a non-null `type`, and any type makes it a non-4473 disposition
		# (is4473=false) — 4473s are born only in FastBound's UI wizard. So we just
		# hold the order and deep-link the cashier to the /Form4473s page; the
		# form4473.completed webhook matches the disposition back here by serial.
		fastbound_url = _fb_form4473_list_url(account_num)
	frappe.db.set_value(doctype, invoice_name, {
		"fastbound_url": fastbound_url,
		"form_4473_status": "Pending",
		"nics_status": "N/A",
	}, update_modified=False)

	commit()
	frappe.publish_realtime(
		"doc_update", {"doctype": doctype, "name": invoice_name},
		after_commit=True,
	)

	return {
		"ok": True,
		"fastbound_url": fastbound_url,
		"n_firearms": len(firearm_rows),
	}


def _check_manager_perm() -> None:
	roles = set(frappe.get_roles())
	if not (roles & {"Sales Manager", "System Manager"}):
		frappe.throw(
			_("Overriding the 4473/NICS gate requires a manager (Sales Manager or "
				"System Manager)."),
			frappe.PermissionError,
		)


@frappe.whitelist()
def manager_override_4473(invoice_name: str, reason: str) -> dict:
	"""Manager force-complete a stuck firearm POS sale.

	The escape hatch for when FastBound is the problem, not us: the 4473 was
	genuinely completed in FastBound but never synced back (a serial edited in
	the shared account so the webhook couldn't match, the disposition deleted by
	a partner FFL, FastBound down). Normally the sale can't submit — the gate
	needs a Retail Sale FFL Disposition (4473 Completed / NICS Proceed) for each
	gun. This mints exactly that, marked `manual_override` with WHO + WHY, so the
	bound book stays honest and the override is auditable.

	Manager-gated (Sales Manager / System Manager) and reason-required. Idempotent
	per (invoice, serial). Does NOT push to FastBound — the operator reconciles the
	FastBound bound book separately.

	Args:
		invoice_name: Draft POS Invoice docname.
		reason: free-text justification (required, stored on each disposition).

	Returns:
		{ok, dispositions: [names], n_firearms}
	"""
	_check_manager_perm()

	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("A reason is required to override the 4473/NICS gate."))

	return _book_hand_asserted_4473(
		invoice_name,
		reason=reason,
		note_prefix=_("Manager 4473 override"),
		action=_("a 4473 override"),
		verb=_("overriding"),
	)


def _book_hand_asserted_4473(invoice_name: str, *, reason: str, note_prefix: str,
		action: str, verb: str) -> dict:
	"""Book one submitted Retail Sale FFL Disposition per gun on `invoice_name`,
	and flip the invoice's 4473/NICS flags so the submit gate passes.

	The bound-book half of "a 4473 happened somewhere this system cannot see" —
	shared by the two callers that can legitimately assert one:

	* ``manager_override_4473`` — FastBound completed the form but never synced it
	  back, and a manager says so on the record;
	* ``attest_paper_4473`` — the store keeps no electronic 4473 at all, and the
	  cashier ticks that the paper form is done (FFL Settings → Paper 4473 Mode).

	Both write the SAME shape, and every record carries ``manual_override`` + WHO +
	WHY. That marking is what lets a reader of the bound book — or a report, or an
	auditor — tell a hand-asserted disposition from one FastBound actually
	committed. Note what it does NOT do: ``covered_serials_for_invoice`` filters on
	type/status/docstatus and never looks at ``manual_override``, so an assertion
	satisfies the submit gate exactly like a verified 4473. That is the whole point
	of both entry points — the marking is for the humans reading afterwards, not a
	second gate.

	Callers own their own preconditions (role, reason, mode); everything below is
	invariant across both and must not diverge.

	Args:
		invoice_name: Draft POS Invoice docname.
		reason: Stored on each disposition as override_reason.
		note_prefix: Leading phrase of the disposition note ("<prefix> by <user>: <reason>").
		action: Named in the "no firearms on this ticket" error ("… — {action} is not needed").
		verb: Named in the "serial missing" error ("before {verb}").

	Returns:
		{ok, dispositions: [names], n_firearms}
	"""
	invoice_name = (invoice_name or "").strip()
	if not frappe.db.exists("POS Invoice", invoice_name):
		frappe.throw(_("POS Invoice {0} not found.").format(invoice_name))
	doc = frappe.get_doc("POS Invoice", invoice_name)
	if doc.docstatus == 1:
		frappe.throw(_("This order is already completed — nothing left to record against it."))

	firearm_rows = [row for row in (doc.items or []) if is_regulated_firearm_item(row.item_code)]
	if not firearm_rows:
		frappe.throw(_("No firearm items in this invoice — {0} is not needed.").format(action))

	missing = [str(row.idx) for row in firearm_rows if not (row.serial_no or "").strip()]
	if missing:
		frappe.throw(_("Enter a serial on every firearm row before {0} (row {1}).").format(
			verb, ", ".join(missing)))

	user = frappe.session.user
	now = frappe.utils.now_datetime()
	created: list[str] = []
	# A firearm line can carry multiple serials newline-joined (qty>1) — split so
	# each gun gets its OWN disposition, never one whose `serial` is the raw
	# "J4940\n7805C" string. Idempotency (a partial
	# webhook sync, a re-clicked override, a double-tapped checkbox) is enforced
	# under an advisory lock so a double click can't double-mint (ledger #1).
	for row in firearm_rows:
		for serial in split_serials(row.serial_no):
			def _insert(serial=serial, item_code=row.item_code):
				disp = frappe.get_doc({
					"doctype": "FFL Disposition",
					"disposition_date": now,
					"disposition_type": "Retail Sale",
					"company": doc.company,
					"customer": doc.customer,
					"item": item_code,
					"serial": serial,
					"linked_pos_invoice": invoice_name,
					"form_4473_status": "Completed",
					"nics_status": "Proceed",
					"manual_override": 1,
					"override_reason": reason,
					"override_by": user,
					"notes": f"{note_prefix} by {user}: {reason}",
				})
				disp.flags.ignore_permissions = True
				disp.insert()
				disp.submit()
				created.append(disp.name)
				return disp.name
			# dedup_key stays "override|…" for BOTH callers on purpose: it is an
			# advisory lock name, not stored data, and the two entry points must
			# serialise against EACH OTHER (a manager override racing a counter
			# attestation on the same gun would otherwise double-book it).
			book_disposition_once(
				dedup_key=f"override|{invoice_name}|{serial}",
				# A CANCELLED one (Redo 4473) is not a booking: the corrected
				# 4473 attested after a redo must book afresh.
				exists_filters={"linked_pos_invoice": invoice_name, "serial": serial,
					"disposition_type": "Retail Sale", "docstatus": ("!=", 2)},
				insert_fn=_insert,
			)

	frappe.db.set_value("POS Invoice", invoice_name, {
		"form_4473_status": "Completed",
		"nics_status": "Proceed",
	}, update_modified=False)
	# The durable 4473 record (Form 4473 Log) — only when THIS call booked the
	# dispositions: a re-clicked override / double-tapped checkbox books nothing
	# (the locked dedupe above) and is the same 4473. An attest after Redo 4473
	# books again, so it logs again — that is a new 4473.
	if created:
		record_4473(pos_invoices=[invoice_name], customer=doc.customer,
			serials=[s for row in firearm_rows for s in split_serials(row.serial_no)],
			nics_status="Proceed", source="Paper / override")
	commit()
	frappe.publish_realtime(
		"doc_update", {"doctype": "POS Invoice", "name": invoice_name},
		after_commit=True,
	)

	# Count guns, not lines — a qty>1 line is several firearms.
	n_firearms = len({s for row in firearm_rows for s in split_serials(row.serial_no)})
	return {"ok": True, "dispositions": created, "n_firearms": n_firearms}


@frappe.whitelist()
def attest_paper_4473(invoice_name: str) -> dict:
	"""Record that this order's 4473s were completed on PAPER, and book the
	bound-book Disposition for every gun on the ticket.

	What the counter's "Paper 4473 completed" checkbox calls. A store with no
	FastBound account keeps paper 4473s and runs NICS by phone or the FBI portal;
	nothing will ever arrive to stamp this order Completed, so the cashier states
	it instead and the sale can be finished.

	Not manager-gated, unlike ``manager_override_4473``, and that difference is
	deliberate. The override exists because something went WRONG and a manager is
	accountable for the exception. This is the store's ordinary, configured way of
	working: the same cashier who filled the paper form in says it is filled in.
	Making it a manager action would put a manager behind every single gun sale.
	The two share a booking path, so the record is identically audited either way —
	``manual_override`` with the attesting user and a reason naming paper mode.

	Available ONLY while FFL Settings → Paper 4473 Mode is on. With FastBound
	running, a cashier must not be able to click past a pending 4473: that is what
	the manager override is for.

	Recording the attestation is all this can do. Whether the form was actually
	completed and the background check actually cleared is a fact about the real
	world (27 CFR 478.102 / 478.124) that no software can verify — hence storing
	WHO said so, and when.

	Idempotent per (invoice, serial): a double-tapped checkbox books one
	disposition per gun, not two.

	Args:
		invoice_name: Draft POS Invoice docname.

	Returns:
		{ok, dispositions: [names], n_firearms}

	Permissions:
		POS 4473 roles (_check_perm).
	"""
	_check_perm()
	if not paper_4473_mode():
		frappe.throw(_(
			"Paper 4473 Mode is off, so 4473s are expected to come from FastBound. "
			"Turn it on in FFL Settings if this store keeps paper 4473s; if a "
			"FastBound 4473 is genuinely stuck, a manager can use the 4473 override "
			"instead."))
	# Paper mode and FastBound can legitimately both be on for a while — during a
	# migration in either direction. What must NOT happen is a cashier ticking
	# "paper 4473 done" past an ELECTRONIC 4473 that is genuinely in flight: that
	# order is waiting on a real form someone is filling in on FastBound's side and
	# the webhook is still coming. A non-empty fastbound_url is exactly the marker
	# that this order was sent there (paper mode leaves it blank), so that is the
	# one combination refused here. Redoing such an order is a manager action with
	# a reason — manager_redo_4473 — not a checkbox.
	if fastbound_enabled() and (frappe.db.get_value(
			"POS Invoice", (invoice_name or "").strip(), "fastbound_url") or "").strip():
		frappe.throw(_(
			"This order was already sent to FastBound for an electronic 4473, so it "
			"cannot be closed out with a paper one. Use Refresh 4473 once the form is "
			"committed there; if it genuinely has to be redone, a manager can run "
			"Redo 4473."),
			title=_("Electronic 4473 In Flight"))
	user = frappe.session.user
	return _book_hand_asserted_4473(
		invoice_name,
		reason=_("Paper 4473 completed at the counter, attested by {0}").format(user),
		note_prefix=_("Paper 4473 attested"),
		action=_("a 4473"),
		verb=_("recording the paper 4473"),
	)


@frappe.whitelist()
def start_transfer_for_pos_invoice(
	serials: list[str] | str,
	fee_item: str,
	fee_amount: float | str,
	customer: str,
	pos_profile: str | None = None,
	invoice_name: str | None = None,
) -> dict:
	"""Build a customer-transfer POS Invoice (N $0 firearm lines + one Transfer
	Fee line) then hand off to the 4473 kickoff.

	A transfer starts from an EMPTY POS cart: there's no saved invoice to mutate,
	and ERPNext won't let the cart save empty client-side. So the whole invoice is
	built server-side — get_item_details fills item_name / uom / income_account (a
	client add_child leaves those blank → mandatory-field error), and is_free_item=1
	makes ERPNext SKIP the price-list fetch so each gun stays $0
	(get_item_details.py:1217). A second save then pins the economics (guns free,
	fee = configured amount) so no price-list lookup can override them. The
	customer is charged only the per-gun Transfer Fee; the guns are already theirs.

	Args:
	    serials: Firearm serials to transfer — list or JSON string.
	    fee_item: The Transfer Fee service Item code.
	    fee_amount: Per-gun fee amount.
	    customer: Buyer Customer name.
	    pos_profile: Optional POS Profile override.
	    invoice_name: Optional existing draft to reuse.

	Returns:
	    The 4473 kickoff result from start_4473_for_pos_invoice.

	Permissions:
	    POS 4473 roles (_check_perm).
	"""
	_check_perm()

	if isinstance(serials, str):
		import json
		serials = json.loads(serials or "[]")
	serials = [s.strip() for s in (serials or []) if s and s.strip()]
	if not serials:
		frappe.throw(_("Select at least one firearm to transfer."))
	fee_amount = flt(fee_amount)

	if not customer:
		frappe.throw(_("Pick the customer receiving the transfer."))
	# The UI blocks dealer transferees; enforce server-side too — the 4473 gate is
	# skipped for FFL dealers, so a dealer transfer would bypass compliance.
	if frappe.db.get_value("Customer", customer, "is_ffl_dealer"):
		frappe.throw(_("Customer transfers are for individuals — {0} is an FFL dealer.").format(customer))

	# Map each serial to its firearm Item up front. One query for the whole set,
	# then validate each: it must be a firearm Item AND actually IN STOCK (Serial
	# No status 'Active') — otherwise a gun already sold/transferred (status
	# Delivered/Inactive) could be transferred a second time. The serial_item
	# DICT also collapses a serial accidentally sent twice into ONE transfer line.
	sn_by_serial = {
		r.name: r for r in frappe.get_all(
			"Serial No", filters={"name": ("in", serials)},
			fields=["name", "item_code", "status"],
		)
	}
	serial_item: dict[str, str] = {}
	for ser in serials:
		sn = sn_by_serial.get(ser)
		if not sn or not is_firearm_item(sn.item_code):
			frappe.throw(_("Serial {0} is not an in-stock firearm.").format(ser))
		# Antiques are GCA-exempt: no transfer-with-4473 exists for them. Sending one
		# through this flow builds the invoice then dead-ends in the 4473 kickoff
		# (its regulated-only row map is empty), so reject up front with the fix.
		if is_antique_item(sn.item_code):
			frappe.throw(_("Serial {0} is an antique firearm — it needs no transfer or 4473. "
				"Ring it as a normal sale (add the transfer fee as a line item).").format(ser))
		if sn.status != "Active":
			frappe.throw(_("Serial {0} isn't in stock (status {1}) — it can't be transferred.").format(
				ser, sn.status or "—"))
		serial_item[ser] = sn.item_code

	# Reuse the in-flight invoice only if it was already saved (cart had items);
	# the empty-cart norm has a client-only __islocal name, so start fresh.
	doc = None
	if invoice_name and frappe.db.exists("POS Invoice", invoice_name):
		doc = frappe.get_doc("POS Invoice", invoice_name)
		if doc.docstatus == 1:
			frappe.throw(_("Cannot start a transfer for a submitted invoice."))
	if not doc:
		if not pos_profile:
			pos_profile = frappe.db.get_value(
				"POS Opening Entry",
				{"user": frappe.session.user, "status": "Open", "docstatus": 1},
				"pos_profile",
			)
		doc = frappe.new_doc("POS Invoice")
		doc.pos_profile = pos_profile
		doc.customer = customer

	doc.is_ffl_transfer = 1
	# The customer's own gun at $0 and the transfer fee are this flow's prices, not a discount: the
	# manager price approval leaves this request's saves alone (later edits at the register are asked).
	from ffl_core.price_approval import mark_system_priced
	mark_system_priced(doc)
	# Reusing a restored/parked transfer: don't re-add a gun already on it (a
	# serial can't be transferred twice) and don't stack a second fee line —
	# otherwise re-opening the dialog from Recent Orders duplicates the gun + fee.
	existing_serials = {
		(row.serial_no or "").strip() for row in doc.items if (row.serial_no or "").strip()
	}
	for ser, item_code in serial_item.items():
		if ser in existing_serials:
			continue  # already a line on this invoice
		row = {
			"item_code": item_code, "qty": 1, "rate": 0, "price_list_rate": 0,
			"is_free_item": 1, "serial_no": ser,
			"use_serial_batch_fields": 1, "allow_zero_valuation_rate": 1,
		}
		# Issue each gun from the warehouse its serial is ACTUALLY in, not the POS
		# profile default that set_missing_values would otherwise supply.
		# Same rule, same reason as manual_order.py's own note: "otherwise
		# update_stock 404s on the gun."
		#
		# This was latent until 2026-07-20 and is now load-bearing: transfer guns
		# used to sit in the sellable warehouse, so the POS default happened to be
		# right. They now live in `Customer Custody`, and this is the ONE exit for
		# every gun the feature takes in — failing here would strand a customer's
		# firearm at the counter with the 4473 already started.
		sn_warehouse = frappe.db.get_value("Serial No", ser, "warehouse")
		if sn_warehouse:
			row["warehouse"] = sn_warehouse
		doc.append("items", row)
	if fee_item and fee_amount:
		# Create the default fee item if this is reached via direct API (the POS
		# dialog already does so via get_transfer_config); lazy so it never runs
		# at install time before the UOM/Item-Group masters exist.
		from ffl_core.setup.transfer_fee import TRANSFER_FEE_ITEM, ensure
		if fee_item == TRANSFER_FEE_ITEM:
			ensure()
		# One fee line, qty = total guns on the invoice (existing + newly added).
		total_guns = len(existing_serials | set(serial_item))
		fee_row = next((r for r in doc.items if r.item_code == fee_item), None)
		if fee_row:
			fee_row.qty = total_guns
			fee_row.rate = fee_row.price_list_rate = fee_amount
		else:
			doc.append("items", {
				"item_code": fee_item, "qty": total_guns,
				"rate": fee_amount, "price_list_rate": fee_amount,
			})

	# Populate the POS defaults the cart UI normally fills: payment modes (POS
	# requires ≥1 — added only when for_validate is False, so the save's own
	# validate pass never adds them), warehouse, currency, debit account, plus
	# item_name / uom / income_account on every row.
	doc.set_missing_values()
	doc.flags.ignore_permissions = True
	doc.save()

	# Pin the transfer economics now that the rows are complete: item_name is set,
	# so this save won't re-run the price-list lookup that could override them.
	# (is_free_item is only the lever that holds the gun at $0 — the customer-
	# transfer *meaning* lives in is_ffl_transfer, not this flag.) Only re-pin the
	# newly-added guns to $0; existing rows on a restored transfer already are.
	for row in doc.items:
		ser = (row.serial_no or "").strip()
		if ser and ser in serial_item and is_firearm_item(row.item_code):
			row.rate = row.price_list_rate = row.discount_amount = 0
			row.is_free_item = 1
		elif row.item_code == fee_item:
			row.rate = row.price_list_rate = fee_amount
			row.discount_amount = 0
	doc.save()

	# Map ALL firearm rows (existing + new) for the kickoff — see _firearm_serial_map.
	return start_4473_for_pos_invoice(doc.name, _firearm_serial_map(doc.items))


# Someone is waiting on a browser tab: one attempt, no retry ladder.
_PDF_TIMEOUT_SECONDS = 20


@frappe.whitelist(methods=["GET"])
def view_4473_pdf(log: str) -> None:
	"""Show a 4473's signed PDF (a Form 4473 Log row) inline in the browser tab.

	Served from the row's private copy when it has one. Otherwise it is fetched
	live from this site's own FastBound account — by the stored form id only, so
	nothing stored can point the server anywhere else — and a copy is queued.
	Every view is written to Access Log.

	Args:
		log: Form 4473 Log name whose PDF to show.

	Returns:
		None — the response body is the PDF (``Content-Type: application/pdf``,
		shown inline).
	"""
	_check_perm()
	frappe.has_permission("Form 4473 Log", "read", doc=log, throw=True)
	row = frappe.db.get_value("Form 4473 Log", log,
		["form4473_id", "pdf_file", "pdf_status", "pos_invoice"], as_dict=True)
	if row.pdf_file:
		# encodings=[]: raw bytes — the default would text-decode the PDF.
		pdf = frappe.get_doc("File", {"file_url": row.pdf_file}).get_content(encodings=[])
	else:
		pdf = _fetch_4473_pdf_live(log, row)
		# Queued now: a GET request is rolled back, never committed, so an
		# after-commit job would be dropped. force: the fetch above just proved
		# FastBound has it, whatever the row's last status says.
		frappe.enqueue("ffl_integrations.fastbound.pdf_archive.store_4473_pdf",
			queue="short", job_id=f"4473-pdf:{log}", deduplicate=True, log_name=log, force=True)

	from frappe.core.doctype.access_log.access_log import make_access_log
	make_access_log(doctype="Form 4473 Log", document=log, file_type="PDF",
		method="ffl_integrations.fastbound.pos_4473.view_4473_pdf")
	frappe.local.response.filename = f"4473-{(row.pos_invoice or '').split(', ')[0] or log}.pdf"
	frappe.local.response.filecontent = pdf
	frappe.local.response.type = "pdf"


def _fetch_4473_pdf_live(log: str, row) -> bytes:
	"""The PDF straight from FastBound, for a row with no private copy yet —
	with a readable refusal for every way that can't work."""
	if not row.form4473_id:
		frappe.throw(_("{0} has no signed 4473 PDF on file — a paper 4473, a manager override, "
			"or a FastBound 4473 whose link never reached the POS.").format(log),
			title=_("No 4473 PDF"))
	try:
		return FastBoundClient.from_settings_fast(timeout=_PDF_TIMEOUT_SECONDS).download_4473_pdf(
			row.form4473_id)
	except FastBoundConfigError:
		frappe.throw(_("FastBound is not enabled or not set up on this site, and this 4473 "
			"has no copy stored in the POS yet."), title=_("FastBound unavailable"))
	except FastBoundError as exc:
		if exc.status_code == 404:
			frappe.throw(_("FastBound could not find this 4473 in the store's account."),
				title=_("4473 not found"))
		frappe.log_error(title=f"View 4473 PDF failed for {log}", message=str(exc))
		frappe.throw(_("FastBound did not return the 4473 PDF. Try again, or open it in FastBound."),
			title=_("FastBound unavailable"))
	except Exception as exc:  # noqa: BLE001 — network: timeout / connection refused
		frappe.log_error(title=f"View 4473 PDF failed for {log}", message=str(exc))
		frappe.throw(_("FastBound did not answer. Try again, or open the 4473 in FastBound."),
			title=_("FastBound unavailable"))
