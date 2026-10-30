"""Source-level guard: the Receive Goods page's zero-cost escape hatch stays in
step with the server that judges it.

The page mirrors two server vocabularies in JavaScript — the zero-cost reasons
(``setup.zero_cost.ZERO_COST_REASONS``) and the consignment acquisition types
(``firearm.CONSIGNMENT_ACQUISITION_TYPES``) — because a dropdown fetched at
runtime is a dropdown that can fail to load, and the one thing this feature may
never do is make a genuinely-free acquisition unrecordable. The cost of
mirroring is drift; this test is what makes drift mechanically detectable.

Drift is not cosmetic here. A reason the page offers but the server does not
know is refused at Submit with the card already gone; a consignment spelling the
server exempts but the page does not would ask a consignor's legitimately-$0 gun
to be "confirmed free", which it is not.

The tiers themselves (which state is refused and why) are pinned in
tests/js/receive_zero_cost.test.js against the same kernel the page runs; the
server side is pinned in test_receive_goods.ZeroCostGuard.
"""
from __future__ import annotations

import os
import re

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.front_end_invariants import block, code_only, read

from ffl_core.firearm import CONSIGNMENT_ACQUISITION_TYPES
from ffl_core.setup.zero_cost import OTHER_REASON, ZERO_COST_REASONS



def _js() -> str:
	return read("ffl_core", "ffl_core", "page", "receive_goods", "receive_goods.js")


def _js_code_only() -> str:
	"""The page source with ``//`` comments stripped.

	The anti-equality guard below hunts for a CODE pattern, and two comments in
	that file quote the very pattern they warn against ("NOT ``=== 'Consignment'``").
	Widening the regex to catch every spelling therefore requires dropping
	comments first, or the warning itself trips the guard.
	"""
	return re.sub(r"//.*$", "", _js(), flags=re.MULTILINE)


def _js_string_array(js: str, const_name: str) -> list[str]:
	"""The single-quoted members of a top-level `const NAME = [ ... ];`."""
	m = re.search(rf"const {const_name} = \[(.*?)\];", js, re.S)
	assert m, f"{const_name} not found in receive_goods.js"
	return re.findall(r"'([^']*)'", m.group(1))


class ReceiveGoodsFrontEndContract(FrappeTestCase):
	def test_the_reason_dropdown_offers_exactly_what_the_server_accepts(self):
		self.assertEqual(
			_js_string_array(_js(), "RG_ZERO_COST_REASONS"), list(ZERO_COST_REASONS),
			msg="receive_goods.js RG_ZERO_COST_REASONS drifted from "
				"setup.zero_cost.ZERO_COST_REASONS — the page would offer a reason "
				"the server refuses, or hide one it accepts.")

	def test_the_page_and_the_server_name_the_same_escape_hatch_reason(self):
		"""'Other' is the one reason that demands a written note. If the two sides
		disagree on its spelling, the page asks for the note on the wrong reason
		— and the server throws on the right one."""
		m = re.search(r"const RG_ZERO_COST_OTHER = '([^']*)';", _js())
		self.assertIsNotNone(m, "RG_ZERO_COST_OTHER not found in receive_goods.js")
		self.assertEqual(m.group(1), OTHER_REASON)
		self.assertIn(OTHER_REASON, ZERO_COST_REASONS)

	def test_neither_page_keeps_its_own_list_of_payment_methods(self):
		"""The ways to pay a seller and which receipts pay come from the server (seller_payment_preview / get_today's
		payout_methods), so the pages cannot drift from cash_drawer.PAYOUT_METHODS or receipt_pays_seller."""
		drawer = read("ffl_core", "ffl_core", "page", "cash_drawer", "cash_drawer.js")
		for name, src in (("receive_goods.js", _js()), ("cash_drawer.js", drawer)):
			self.assertNotIn("'Zelle'", code_only(src), msg=f"{name} hard-codes the payment methods again")
		self.assertIn("this.data.payout_methods", drawer)

	def test_r2_both_pages_use_the_one_shared_payout_check(self):
		"""The method/reference check lives once, in pos_tender_guard.js (payoutProblem, Node-tested),
		and both pages call it -- no page keeps its own copy."""
		drawer = code_only(read("ffl_core", "ffl_core", "page", "cash_drawer", "cash_drawer.js"))
		receive = code_only(_js())
		for name, src in (("receive_goods.js", receive), ("cash_drawer.js", drawer)):
			self.assertIn("ffl.payoutProblem(", src, msg=name)
			self.assertNotRegex(src, r"function (rgSellerPaymentGap|cdrwCheckPayout)\(", msg=name)
		self.assertIn("payoutProblem", read("ffl_core", "public", "js", "pos_tender_guard.js"))

	def test_the_unpaid_list_offers_paid_outside_the_system_with_a_required_note(self):
		"""One click opens the dialog, the note is required, its button is the one confirm; the server call follows."""
		src = code_only(read("ffl_core", "ffl_core", "page", "cash_drawer", "cash_drawer.js"))
		self.assertIn("this.paidOutsideDialog(", src)
		dialog = block(src, "paidOutsideDialog(receipt) {", "\t")
		self.assertIn("reqd: 1", dialog)
		self.assertIn("this.call('mark_paid_outside'", dialog)

	def test_r2_6_the_drawer_page_says_recorded_only_for_a_booked_entry(self):
		"""The drawer page shows "recorded" only when the server names the entry it booked."""
		dialog = block(code_only(read("ffl_core", "ffl_core", "page", "cash_drawer", "cash_drawer.js")),
			"goodsPayoutDialog(receipt) {", "\t")
		self.assertIn("r.message && r.message.entry", dialog)

	def test_a_private_sellers_receive_asks_the_server_then_the_cashier_and_sends_it(self):
		"""Incident 2026-10-06: Submit on an individual receive asks seller_payment_preview first (whether the seller is
		paid and the amount the receipt will book), shows one dialog with the server's amount and methods, and the
		create_receive payload carries the answer."""
		src = code_only(_js())
		sub = block(src, "_submitBuckets() {", "\t")
		self.assertLess(sub.index("this._askSellerPayment()"), sub.index("rgGroupByBucket(this.lines)"))
		ask = block(src, "_askSellerPayment() {", "\t")
		self.assertIn("ffl_core.api.receive_goods.seller_payment_preview", ask)
		self.assertIn("this._stockPayload(stock)", ask)
		dialog = block(src, "_showPayDialog(preview) {", "\t")
		for needed in ("preview.amount", "preview.methods"):
			self.assertIn(needed, dialog)
		payload = block(src, "_stockPayload(lines) {", "\t")
		for key in ("seller_payment_method:", "seller_payment_reference:"):
			self.assertIn(key, payload)
		self.assertIn("this._stockPayload(lines)", block(src, "_submitStock(lines, onDone) {", "\t"))

	def test_consignment_vocabulary_matches_the_server_frozenset(self):
		self.assertEqual(
			set(_js_string_array(_js(), "RG_CONSIGNMENT_ACQUISITION_TYPES")),
			set(CONSIGNMENT_ACQUISITION_TYPES),
			msg="receive_goods.js RG_CONSIGNMENT_ACQUISITION_TYPES drifted from "
				"firearm.CONSIGNMENT_ACQUISITION_TYPES — one side would exempt a "
				"consignor's $0 line and the other would refuse it.")

	def test_no_bare_consignment_equality_survives_on_the_page(self):
		"""`== "Consignment"` on a value FastBound owns
		is the shape this repo already wrote an incident comment about. The page
		had four copies; they all go through the predicate now.

		Matches the comparison in ANY spelling — `===` / `==` / `!==`, either
		operand order, whatever is on the left — not just the one
		`get_value() === 'Consignment'` form the four originals happened to use
		A guard that only catches the spelling already fixed is a
		guard against nothing.
		"""
		self.assertNotRegex(
			_js_code_only(),
			r"""(===?\s*['"]Consignment['"]|['"]Consignment['"]\s*===?)""",
			msg="use _isConsignmentIntake() — the Acquisition Type list is synced "
				"from FastBound at runtime, so an equality can stop matching.")

	def test_the_submitted_payload_carries_the_whole_trio(self):
		"""All three keys, including the note: the server refuses reason "Other"
		without it, and a page that sent only two would make that reason a dead
		option."""
		js = _js()
		# The stock-bucket payload (_submitStock) AND the per-order payload
		# (_submitBuckets → receive_lines) both carry the trio — the server reads
		# it on either path.
		start = js.index("items: lines.map")
		items_map = js[start:js.index("frappe.call({", start)]
		for key in ("zero_cost_confirmed", "zero_cost_reason", "zero_cost_note"):
			self.assertIn(f"{key}:", items_map, msg=f"{key} missing from the submit payload")
		# (built by rgBucketItem, called from _submitBuckets)
		self.assertIn("lines.map(rgBucketItem)", block(code_only(js), "_submitBuckets() {", "\t"))
		bucket_item = block(code_only(js), "function rgBucketItem(l) {", "")
		for key in ("zero_cost_confirmed", "zero_cost_reason", "zero_cost_note"):
			self.assertIn(f"{key}:", bucket_item, msg=f"{key} missing from the receive_lines payload")


class ScanFirstReceiving(FrappeTestCase):
	"""The page asks the server which order a scanned unit
	belongs to BEFORE the card opens, remembers the answer per item, keeps every
	line in a bucket, offers the Incoming panel, and submits one receive per
	bucket. Source-level pins (this repo ships no browser test runner)."""

	def test_scan_asks_the_server_which_order_before_the_card_opens(self):
		src = code_only(_js())
		wire = block(src, "_wireKeyboard() {", "\t")
		self.assertIn("this._matchThenShow(item)", wire)
		match = block(src, "_matchThenShow(item, opts = {}) {", "\t")
		self.assertIn("ffl_integrations.distributor.receive_bridge.match_incoming", match)
		self.assertLess(match.index("match_incoming"), match.index("_askBucket"))

	def test_same_item_reuses_the_last_choice_without_asking_again(self):
		match = block(code_only(_js()), "_matchThenShow(item, opts = {}) {", "\t")
		self.assertIn("this.session.choices[item.item_code]", match)

	def test_every_line_carries_its_bucket(self):
		add = block(code_only(_js()), "_addPending() {", "\t")
		self.assertIn("bucket:", add)
		self.assertIn("staged", add)

	def test_the_typed_serial_is_checked_against_the_order(self):
		src = code_only(_js())
		self.assertIn("this._checkSerialsAgainstOrder()", src)
		check = block(src, "_checkSerialsAgainstOrder() {", "\t")
		self.assertIn("match_incoming", check)

	def test_incoming_panel_loads_whole_order_or_one_parcel(self):
		src = code_only(_js())
		self.assertIn("ffl_integrations.distributor.receive_bridge.list_incoming", src)
		render = block(src, "_renderIncoming(groups) {", "\t")
		for marker in ("btn-load-order", "btn-load-do", "btn-poll-now"):
			self.assertIn(marker, render)
		self.assertIn("ffl_integrations.distributor.tasks.poll_order_now", render)

	def test_the_incoming_panel_is_a_top_right_button_not_a_section(self):
		"""What is on the way lives behind a top-right
		button carrying its count — as a page section it pushed the scan box below
		the fold on every ordinary receive. Staging a parcel closes the dialog, so
		the staged rows are not hidden behind it."""
		src = code_only(_js())
		self.assertNotIn("incoming-section", src)
		btn = block(src, "_incomingButton(count) {", "\t")
		self.assertIn("set_secondary_action", btn)
		self.assertIn("Incoming shipments ({0})", btn)
		opened = block(src, "_openIncoming() {", "\t")
		self.assertIn("frappe.ui.Dialog", opened)
		stage = block(src, "_loadOrderLines(order, lines) {", "\t")
		self.assertIn("this.incomingDialog.hide()", stage)
		# The dialog is appended to <body>, outside .ffl-receive, so the panel's
		# rules carry their own class or the rows render unstyled.
		self.assertIn(".ffl-incoming .incoming-order", _js())
		self.assertIn("addClass('ffl-incoming')", src)

	def test_an_unanswered_incoming_load_never_reads_as_nothing_on_the_way(self):
		"""A failed or still-flying load must not answer "Nothing on the way." —
		that is the answer that books a customer's parcel as plain restock."""
		src = code_only(_js())
		render = block(src, "_renderIncoming(groups) {", "\t")
		self.assertIn("if (!groups) {", render)
		self.assertIn("Loading", render)
		self.assertIn("Could not load incoming orders.", render)
		self.assertLess(render.index("if (!groups) {"), render.index("Nothing on the way."))
		# The error is checked BEFORE the empty check, whatever is
		# cached — a cached [] after a failed refresh used to print "Nothing on
		# the way." with no hint at all.
		self.assertLess(render.index("this.incomingError"), render.index("Nothing on the way."))
		self.assertIn("this.incomingError = true", block(src, "_loadIncoming() {", "\t"))

	def test_submit_books_each_order_bucket_then_stock(self):
		src = code_only(_js())
		sub = block(src, "_submitBuckets() {", "\t")
		self.assertIn("rgGroupByBucket(this.lines)", sub)
		self.assertIn("ffl_integrations.distributor.receive_bridge.receive_lines", sub)
		self.assertIn("failed", sub)
		stock = block(src, "_submitStock(lines, onDone) {", "\t")
		self.assertIn("ffl_core.api.receive_goods.create_receive", stock)
		# Order buckets drain before the stock bucket is sent.
		self.assertLess(src.index("_submitBuckets() {"), src.index("_submitStockBucket(stockLines"))

	def test_an_outside_pos_invoice_is_its_own_bucket(self):
		# An explicit {kind, name} bucket (no name-prefix convention): invoice buckets go
		# to receive_invoice_lines, the panel stages them through stage_invoice (it makes
		# the Items), and the order/invoice call carries the same fields as the stock one.
		src = code_only(_js())
		sub = block(src, "_submitBuckets() {", "\t")
		self.assertIn("bucket.kind === 'invoice'", sub)
		self.assertNotIn("startsWith('inv:')", src)
		self.assertIn("ffl_integrations.distributor.receive_bridge.receive_invoice_lines", sub)
		self.assertIn("lines.map(rgBucketItem)", sub)
		self.assertIn("ffl_integrations.distributor.receive_bridge.stage_invoice",
			block(src, "_stageInvoice(inv) {", "\t"))
		self.assertIn("this._stageInvoice(o)", block(src, "_renderIncoming(groups) {", "\t"))

	def test_scans_return_to_the_staged_invoice_line_and_change_can_leave_it(self):
		src = code_only(_js())
		scan = block(src, "_matchThenShow(item, opts = {}) {", "\t")
		# A remembered choice (stock / another order, e.g. after "change") outranks the
		# invoice line; the invoice line outranks the order lookup.
		self.assertLess(scan.index("this.session.choices[item.item_code]"),
			scan.index("this._invoiceCandidate(item)"))
		self.assertLess(scan.index("this._invoiceCandidate(item)"), scan.index("match_incoming"))
		self.assertIn("opts.skipInvoice", scan)
		self.assertIn("rgInvoiceCandidate(", block(src, "_invoiceCandidate(item) {", "\t"))
		self.assertIn("skipInvoice: true", src)

	def test_a_remembered_order_line_that_is_full_is_asked_about_again(self):
		src = code_only(_js())
		scan = block(src, "_matchThenShow(item, opts = {}) {", "\t")
		self.assertLess(scan.index("rgHasRoom(remembered"), scan.index("if (remembered) {"))

	def test_unmatched_lines_are_listed_with_their_reason_escaped(self):
		stage = block(code_only(_js()), "_stageInvoice(inv) {", "\t")
		self.assertIn("esc(u.sku)", stage)
		self.assertIn("esc(u.reason)", stage)
		self.assertNotIn("unmatched.join", stage)

	def test_saving_the_line_editor_is_an_answer_and_keeps_the_serials(self):
		src = code_only(_js())
		save = block(src, "_saveLineEdit(idx) {", "\t")
		# The save goes through the kernel that marks the row edited (so a staged row the
		# clerk opened and marked Antique is sent with its answer), not a bare Object.assign.
		self.assertIn("rgApplyLineEdit(line", save)
		self.assertNotIn("line.serials = serials", save)
		self.assertIn("line.edited = true", block(src, "function rgApplyLineEdit(line, edit) {", ""))
		self.assertIn("!l.edited", block(src, "function rgBucketItem(l) {", ""))

	def test_serial_inputs_follow_serial_tracking_not_only_the_firearm_flag(self):
		src = code_only(_js())
		pending = block(src, "_showPending(item, cand = null) {", "\t")
		self.assertIn('id="p-serials-wrapper" style="display:${rgNeedsSerials(item)', pending)
		self.assertIn("if (rgNeedsSerials(item)) {\n\t\t\tthis.pending.serialsCtrl", pending)
		add = block(src, "_addPending() {", "\t")
		self.assertIn("rgSerialGap(p, p.qty, p.serials)", add)
		self.assertIn("serials: rgNeedsSerials(line) ? fp('[data-edit-serials]'", src)
		self.assertIn("data-edit-serials-wrapper style=\"margin-top:10px; display:${rgNeedsSerials(l)", src)
		self.assertIn("rgNeedsSerials(l) && l.serials.length !== Math.round(l.qty)",
			block(src, "_reflectReadiness() {", "\t"))
		self.assertIn("rgSerialGap(line, qty, serials)", block(src, "_saveLineEdit(idx) {", "\t"))

	def test_the_order_and_invoice_call_sends_what_the_stock_call_sends(self):
		# Sell price, antique flag and year made were dropped on the order path.
		src = code_only(_js())
		item = block(src, "function rgBucketItem(l) {", "")
		for key in ("sell_price", "is_antique", "year_made", "zero_cost_confirmed"):
			self.assertIn(key, item)

	def test_orders_only_session_needs_no_supplier(self):
		ready = block(code_only(_js()), "_reflectReadiness() {", "\t")
		self.assertIn("ordersOnly", ready)

	def test_supplier_with_open_orders_is_warned(self):
		src = code_only(_js())
		self.assertIn("this._warnOpenOrdersFor(", src)
		warn = block(src, "_warnOpenOrdersFor(supplier) {", "\t")
		self.assertIn("list_incoming", warn)


class ResetForNextReceive(FrappeTestCase):
	"""After a fully successful receive the page stays loaded for the next one.
	The private seller's identity is per receive: carried over, the next private
	purchase would be booked to the previous seller's name, ID and address and
	pushed to the bound book that way; a still-ticked transfer box would take the
	next gun into custody at $0. The batch-receiving context (supplier, source,
	acquisition type) is kept on purpose."""

	SELLER_FIELDS = (
		"seller_customer", "seller_first_name", "seller_middle_name", "seller_last_name",
		"seller_suffix", "seller_id_type", "seller_id_number", "seller_id_document",
		"seller_address_line1", "seller_city", "seller_state", "seller_zip",
	)

	def _reset(self) -> str:
		return code_only(block(_js(), "\t_resetForNext() {", "\t"))

	def test_the_reset_clears_every_seller_identity_field(self):
		reset = self._reset()
		for field in self.SELLER_FIELDS:
			with self.subTest(field=field):
				self.assertIn(f"'{field}'", reset)
		self.assertIn("this.fields[f].set_value('')", reset)

	def test_the_reset_unticks_the_transfer_box_and_its_customer(self):
		reset = self._reset()
		self.assertIn("this.fields.is_ffl_transfer.set_value(0)", reset)
		self.assertIn("this._onTransferChange()", reset)
		self.assertIn("this.fields.transfer_for_customer.set_value('')",
			block(_js(), "\t_onTransferChange() {", "\t"))

	def test_the_reset_keeps_the_batch_receiving_context(self):
		reset = self._reset()
		for field in ("supplier", "source", "acquisition_type", "acquisition_source"):
			with self.subTest(field=field):
				self.assertNotIn(f"fields.{field}.set_value", reset)
				self.assertNotIn(f"'{field}'", reset)


if __name__ == "__main__":
	import unittest

	unittest.main()
