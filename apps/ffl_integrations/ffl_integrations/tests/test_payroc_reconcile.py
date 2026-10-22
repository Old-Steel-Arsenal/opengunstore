"""Daily Payroc reconcile — a capture voided/refunded outside the POS is flagged
once (comment + admin alert); healthy captures and repeat runs stay quiet."""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.payroc import reconcile as rec


def _payment(status, operator="SGUO", refunds=None):
	p = {"operator": operator, "transactionResult": {"status": status, "authorizedAmount": 50.0}}
	if refunds is not None:
		p["refunds"] = refunds
	return p


class TestGatewayProblem(FrappeTestCase):
	def test_held_statuses_are_fine(self):
		charge = frappe._dict(amount=50, refunded_amount=0)
		for status in ("READY", "COMPLETE", "ready"):
			self.assertIsNone(rec._gateway_problem(_payment(status), charge))

	def test_void_is_a_problem(self):
		# The prod case: a past invoice read back as VOID.
		charge = frappe._dict(amount=50, refunded_amount=0)
		self.assertIn("VOID", rec._gateway_problem(_payment("VOID"), charge))

	def test_pending_is_not_captured(self):
		charge = frappe._dict(amount=50, refunded_amount=0)
		self.assertIn("PENDING", rec._gateway_problem(_payment("PENDING"), charge))

	def test_void_after_full_pos_refund_is_expected(self):
		# A full POS refund before settlement reverses the charge → VOID.
		charge = frappe._dict(amount=50, refunded_amount=50)
		self.assertIsNone(rec._gateway_problem(_payment("VOID"), charge))
		partial = frappe._dict(amount=50, refunded_amount=20)
		self.assertIsNotNone(rec._gateway_problem(_payment("VOID"), partial))


class TestReconcileRun(FrappeTestCase):
	def setUp(self):
		# Any existing document serves as the comment target.
		self.target = frappe.get_all("Company", limit=1, pluck="name")[0]

	def _run(self, payments, sent=True, draft=False):
		charges = [frappe._dict(ref=ref, amount=50.0, refunded_amount=0,
			doctype="Company", name=self.target, draft=draft) for ref in payments]
		with patch.object(rec, "payroc_ready", return_value=True), \
				patch.object(rec, "_captured_charges", return_value=charges), \
				patch.object(rec, "PayrocClient") as pc, \
				patch.object(rec, "notify_admins", return_value=sent) as notify, \
				patch.object(rec.frappe.db, "commit"):
			pc.from_settings.return_value.retrieve_payment.side_effect = \
				lambda ref: payments[ref]
			found = rec.reconcile_payroc_charges()
		return found, notify

	def test_flags_void_once_and_skips_healthy(self):
		ref = f"TSTV{frappe.generate_hash(length=6)}"
		found, notify = self._run({ref: _payment("VOID"), "TSTOK00001": _payment("READY")})
		self.assertEqual([f["ref"] for f in found], [ref])
		notify.assert_called_once()
		self.assertTrue(frappe.db.exists("Comment", {"reference_name": self.target,
			"content": ("like", f"{rec.MARKER} {ref}%")}))
		# Second run: already commented → no repeat alert.
		found, notify = self._run({ref: _payment("VOID")})
		self.assertEqual(found, [])
		notify.assert_not_called()

	def test_failed_alert_leaves_no_comment_so_next_run_retries(self):
		ref = f"TSTF{frappe.generate_hash(length=6)}"
		found, notify = self._run({ref: _payment("VOID")}, sent=False)
		self.assertEqual(len(found), 1)
		self.assertFalse(frappe.db.exists("Comment", {"reference_name": self.target,
			"content": ("like", f"{rec.MARKER} {ref}%")}))
		found, notify = self._run({ref: _payment("VOID")})
		self.assertEqual(len(found), 1)
		notify.assert_called_once()

	def test_inert_when_payroc_not_ready(self):
		with patch.object(rec, "payroc_ready", return_value=False), \
				patch.object(rec, "PayrocClient") as pc:
			self.assertEqual(rec.reconcile_payroc_charges(), [])
			pc.from_settings.assert_not_called()

	def test_legacy_refund_is_read_from_return_invoices(self):
		"""refund_pos_invoice stamps card_refunded_amount on the RETURN, so a
		legacy single-stamp sale refunded in full must not read as 0 refunded."""
		from frappe.utils import add_days, now_datetime

		def fake_get_all(doctype, filters=None, fields=None, pluck=None, **kw):
			if doctype == "POS Invoice" and "return_against" in (filters or {}):
				return [frappe._dict(name="RET-1", card_refunded_amount=50.0,
					card_oob_refund_reference="")]
			if doctype == "POS Invoice":
				return [frappe._dict(ref="LEGACY0001", amount=50.0,
					refunded_amount=0, name="PSINV-LEGACY")]
			return []

		with patch.object(rec.frappe, "get_all", side_effect=fake_get_all), \
				patch.object(rec.frappe.db, "exists", return_value=False), \
				patch.object(rec.frappe.db, "count", return_value=0):
			charges = rec._captured_charges(add_days(now_datetime(), -7))
		self.assertEqual(len(charges), 1)
		self.assertEqual(charges[0].refunded_amount, 50.0)
		self.assertIsNone(rec._gateway_problem(_payment("VOID"), charges[0]))

	def test_multi_card_refund_does_not_mask_another_cards_void(self):
		"""Card A $50 + card B $100; POS refunds $50 to B. A's row must keep its
		own refunded_amount (0) so a void on A still alerts."""
		from frappe.utils import add_days, now_datetime

		def fake_get_all(doctype, filters=None, fields=None, pluck=None, **kw):
			if doctype == "Card Charge":
				return [frappe._dict(ref="CARDA00001", amount=50.0, refunded_amount=0, name="PSINV-M"),
					frappe._dict(ref="CARDB00001", amount=100.0, refunded_amount=50.0, name="PSINV-M")]
			if doctype == "POS Invoice" and "return_against" in (filters or {}):
				return [frappe._dict(name="RET-M", card_refunded_amount=50.0,
					card_oob_refund_reference="")]
			return []

		with patch.object(rec.frappe, "get_all", side_effect=fake_get_all), \
				patch.object(rec.frappe.db, "count", return_value=2):
			charges = {c.ref: c for c in rec._captured_charges(add_days(now_datetime(), -7))}
		self.assertEqual(charges["CARDA00001"].refunded_amount, 0)
		self.assertIsNotNone(rec._gateway_problem(_payment("VOID"), charges["CARDA00001"]))

	def test_canceled_attempt_does_not_make_a_sale_multi_card(self):
		"""A canceled terminal attempt before the one real capture must not block
		attributing the (manager-confirmed) refund to that capture."""
		from frappe.utils import add_days, now_datetime

		def fake_get_all(doctype, filters=None, fields=None, pluck=None, **kw):
			if doctype == "Card Charge":
				return [frappe._dict(ref="ONLY000001", amount=50.0, refunded_amount=0, name="PSINV-C")]
			if doctype == "POS Invoice" and "return_against" in (filters or {}):
				return [frappe._dict(name="RET-C", card_refunded_amount=50.0,
					card_oob_refund_reference="")]
			return []

		counted = {}

		def fake_count(doctype, filters):
			counted.update(filters)
			return 1  # the CANCELED attempt is excluded by the status filter

		with patch.object(rec.frappe, "get_all", side_effect=fake_get_all), \
				patch.object(rec.frappe.db, "count", side_effect=fake_count):
			charges = rec._captured_charges(add_days(now_datetime(), -7))
		self.assertEqual(counted["status"], ("in", rec.CAPTURED_ROW_STATUSES))
		self.assertNotIn("CANCELED", rec.CAPTURED_ROW_STATUSES)
		self.assertEqual(charges[0].refunded_amount, 50.0)

	def test_held_draft_charge_is_checked(self):
		"""Charge-first-then-4473: the CHARGED row sits on a DRAFT POS Invoice
		while the 4473 is pending. It must be read back inside the window, or a
		draft held longer than `days` is never checked at all."""
		from frappe.utils import add_days, now_datetime
		seen = {}
		seen_fields = []

		def fake_get_all(doctype, filters=None, fields=None, pluck=None, **kw):
			if doctype == "Card Charge":
				seen.update(filters)
				seen_fields.extend(fields or [])
				if filters.get("docstatus") == 1:
					return []  # the draft's row has docstatus 0
				return [frappe._dict(ref="DRAFT00001", amount=50.0, refunded_amount=0, name="PSINV-D", docstatus=0)]
			return []

		with patch.object(rec.frappe, "get_all", side_effect=fake_get_all), \
				patch.object(rec.frappe.db, "count", return_value=1):
			charges = rec._captured_charges(add_days(now_datetime(), -7))
		self.assertEqual(seen["docstatus"], ("<", 2))
		self.assertEqual([c.ref for c in charges], ["DRAFT00001"])
		self.assertIsNotNone(rec._gateway_problem(_payment("VOID"), charges[0]))
		self.assertIn("docstatus", seen_fields)
		self.assertTrue(charges[0].draft)

	def test_counter_cancel_order_refund_counts(self):
		"""cancel_order's card refund PE ("Refund for cancelled <SI>") explains a
		gateway VOID on the counter order's single terminal capture."""
		from frappe.utils import add_days, now_datetime

		def fake_get_all(doctype, filters=None, fields=None, pluck=None, **kw):
			if doctype == "Card Terminal Payment":
				return [frappe._dict(ref="CTR0000001", amount=80.0, name="SINV-C")]
			if doctype == "Payment Entry":
				self.assertEqual(filters["remarks"], ("like", "Refund for cancelled SINV-C.%"))
				return [80.0]
			return []

		with patch.object(rec.frappe, "get_all", side_effect=fake_get_all), \
				patch.object(rec.frappe.db, "count", return_value=1):
			charges = rec._captured_charges(add_days(now_datetime(), -7))
		self.assertEqual(charges[0].refunded_amount, 80.0)
		self.assertIsNone(rec._gateway_problem(_payment("VOID"), charges[0]))

	def test_manager_confirmed_portal_refund_counts(self):
		"""confirm_out_of_band_refund leaves card_refunded_amount at 0 and only
		sets the portal reference — the return's card tender is the amount."""
		def fake_get_all(doctype, filters=None, fields=None, pluck=None, **kw):
			if doctype == "POS Invoice":
				return [frappe._dict(name="RET-OOB", card_refunded_amount=0,
					card_oob_refund_reference="PORTAL-77", docstatus=1)]
			if doctype == "Sales Invoice Payment":
				return [-50.0]
			return []

		with patch.object(rec.frappe, "get_all", side_effect=fake_get_all):
			self.assertEqual(rec._pos_recorded_refund("PSINV-X"), 50.0)

	def test_amended_return_reads_refund_from_cancelled_predecessor(self):
		"""Return RET-1 pushed $50 back, was cancelled and amended to RET-1-1.
		restore_charge_rows zeroed card_refunded_amount on the amendment, so
		the $50 lives only on the cancelled RET-1 — every sibling return counts,
		any docstatus."""
		seen = {}

		def fake_get_all(doctype, filters=None, fields=None, pluck=None, **kw):
			if doctype == "POS Invoice":
				seen.update(filters)
				return [frappe._dict(name="RET-1", card_refunded_amount=50.0,
						card_oob_refund_reference=""),
					frappe._dict(name="RET-1-1", card_refunded_amount=0,
						card_oob_refund_reference="")]
			return []

		with patch.object(rec.frappe, "get_all", side_effect=fake_get_all):
			self.assertEqual(rec._pos_recorded_refund("PSINV-A"), 50.0)
		self.assertEqual(seen["return_against"], "PSINV-A")
		self.assertNotIn("docstatus", seen)

	def test_amendment_chain_is_not_double_counted(self):
		"""RET-2 pushed $20, then was cancelled and amended twice; the hook
		zeroed both successors. The sum is ONE $20 refund, not $60 —
		over-counting would mask a void on a $30 capture."""
		def fake_get_all(doctype, filters=None, fields=None, pluck=None, **kw):
			if doctype == "POS Invoice":
				return [frappe._dict(name="RET-2", card_refunded_amount=20.0,
						card_oob_refund_reference=""),
					frappe._dict(name="RET-2-1", card_refunded_amount=0,
						card_oob_refund_reference=""),
					frappe._dict(name="RET-2-2", card_refunded_amount=0,
						card_oob_refund_reference="")]
			return []

		with patch.object(rec.frappe, "get_all", side_effect=fake_get_all):
			self.assertEqual(rec._pos_recorded_refund("PSINV-B"), 20.0)
		# Real schema: the shared sibling-return query runs.
		self.assertEqual(rec._pos_recorded_refund("NO-SUCH-PSINV-658"), 0)

	def test_captured_charges_query_runs(self):
		# Guards the field names/filters against the real schema.
		from frappe.utils import add_days, now_datetime
		self.assertIsInstance(rec._captured_charges(add_days(now_datetime(), -7)), list)

	def test_cancelled_and_locked_draft_returns_count(self):
		"""A cancelled-never-amended return ($30) and a locked draft return ($20,
		slices pushed but never submitted) both pushed real money back; the
		reconcile counts it exactly like api._block_orphaned_sibling_refunds."""
		def fake_get_all(doctype, filters=None, fields=None, pluck=None, **kw):
			if doctype == "POS Invoice":
				return [frappe._dict(name="RET-CXL", card_refunded_amount=30.0,
						card_oob_refund_reference="", docstatus=2),
					frappe._dict(name="RET-DRAFT", card_refunded_amount=20.0,
						card_oob_refund_reference="", docstatus=0)]
			return []

		with patch.object(rec.frappe, "get_all", side_effect=fake_get_all):
			self.assertEqual(rec._pos_recorded_refund("PSINV-R"), 50.0)

	def test_amended_portal_refund_counts_once(self):
		"""A portal-confirmed return cancelled and amended: the amendment keeps the
		no_copy portal reference, so the $40 portal refund is on both documents
		and must count once, not $80 (an over-count would mask a void)."""
		def fake_get_all(doctype, filters=None, fields=None, pluck=None, **kw):
			if doctype == "POS Invoice":
				return [frappe._dict(name="RET-P", card_refunded_amount=0,
						card_oob_refund_reference="PORTAL-1", docstatus=2),
					frappe._dict(name="RET-P-1", card_refunded_amount=0,
						card_oob_refund_reference="PORTAL-1", docstatus=1)]
			if doctype == "Sales Invoice Payment":
				return [-40.0]
			return []

		with patch.object(rec.frappe, "get_all", side_effect=fake_get_all):
			self.assertEqual(rec._pos_recorded_refund("PSINV-P"), 40.0)

	def test_legacy_draft_invoice_is_checked(self):
		"""A legacy single-stamp POS Invoice held as a DRAFT is read back too
		(docstatus < 2) and carries the draft flag."""
		from frappe.utils import add_days, now_datetime
		seen = {}

		def fake_get_all(doctype, filters=None, fields=None, pluck=None, **kw):
			if doctype == "POS Invoice" and "return_against" in (filters or {}):
				return []
			if doctype == "POS Invoice":
				seen["filters"], seen["fields"] = filters, fields
				return [frappe._dict(ref="LEGDRAFT01", amount=50.0, refunded_amount=0,
					name="PSINV-LD", docstatus=0)]
			return []

		with patch.object(rec.frappe, "get_all", side_effect=fake_get_all), \
				patch.object(rec.frappe.db, "exists", return_value=False), \
				patch.object(rec.frappe.db, "count", return_value=0):
			charges = rec._captured_charges(add_days(now_datetime(), -7))
		self.assertEqual(seen["filters"]["docstatus"], ("<", 2))
		self.assertIn("docstatus", seen["fields"])
		self.assertEqual([c.ref for c in charges], ["LEGDRAFT01"])
		self.assertTrue(charges[0].draft)

	def test_draft_alert_and_comment_never_say_correct_the_books(self):
		"""A voided capture on a held DRAFT: nothing is booked, so the alert row
		and the invoice comment say don't submit / have a manager sort out the
		charge, never "have the books corrected"."""
		ref = f"TSTD{frappe.generate_hash(length=6)}"
		found, notify = self._run({ref: _payment("VOID")}, draft=True)
		self.assertTrue(found[0]["draft"])
		body = notify.call_args.args[1]
		self.assertIn(f"DRAFT: {rec._DRAFT_ACTION}", body)
		comments = frappe.get_all("Comment", filters={"reference_name": self.target,
			"content": ("like", f"{rec.MARKER} {ref}%")}, pluck="content")
		self.assertEqual(len(comments), 1)
		self.assertTrue(comments[0].startswith(f"{rec.MARKER} {ref} {rec.DRAFT_TAG}:"))
		self.assertNotIn("books corrected", comments[0])
		self.assertIn("do not submit", comments[0].lower())

	def test_flagged_draft_is_flagged_again_once_submitted(self):
		"""Flagged while a draft, then submitted anyway: now booked as paid, so
		the next run raises it again with the books wording — once."""
		ref = f"TSTS{frappe.generate_hash(length=6)}"
		self._run({ref: _payment("VOID")}, draft=True)
		found, _ = self._run({ref: _payment("VOID")}, draft=True)
		self.assertEqual(found, [])  # still a draft: already flagged
		found, notify = self._run({ref: _payment("VOID")}, draft=False)
		self.assertEqual([f["ref"] for f in found], [ref])
		self.assertIn("correct the books", notify.call_args.args[1])
		found, _ = self._run({ref: _payment("VOID")}, draft=False)
		self.assertEqual(found, [])

	def _recorded(self, returns, card=()):
		def fake_get_all(doctype, filters=None, fields=None, pluck=None, **kw):
			if doctype == "POS Invoice":
				return [frappe._dict(r) for r in returns]
			if doctype == "Sales Invoice Payment":
				return list(card)
			return []
		with patch.object(rec.frappe, "get_all", side_effect=fake_get_all):
			return rec._pos_recorded_refund("PSINV-X")

	def test_pushed_and_portal_on_one_return_count_once(self):
		"""$40 pushed at the gateway, the rest confirmed as a portal refund, card
		tender -60: one $60 refund on two records, not $100."""
		self.assertEqual(self._recorded([dict(name="R1", docstatus=1,
			card_refunded_amount=40.0, card_oob_refund_reference="PORTAL-9")],
			card=[-60.0]), 60.0)

	def test_portal_reference_on_a_draft_return_is_not_evidence(self):
		"""A draft return's tender row is still editable: its portal confirmation
		does not count; money it actually pushed does."""
		self.assertEqual(self._recorded([dict(name="RD", docstatus=0,
			card_refunded_amount=10.0, card_oob_refund_reference="PORTAL-D")],
			card=[-100.0]), 10.0)

	def test_pre_hook_amendment_inheriting_the_amount_counts_once(self):
		"""An amendment made before restore_charge_rows zeroed it (2026-08-04)
		carries its cancelled predecessor's $50: count it once."""
		self.assertEqual(self._recorded([
			dict(name="RA", docstatus=2, card_refunded_amount=50.0),
			dict(name="RA-1", docstatus=1, amended_from="RA", card_refunded_amount=50.0)]),
			50.0)
