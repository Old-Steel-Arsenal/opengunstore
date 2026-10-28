"""Counter orders and the agreements their sale needs (agreements.py's sale gate).

A phone / ship-to-FFL order is booked even though the buyer has not signed: the
signing links are emailed, a Dispose & Ship Now is held back, and Dispose on
Pending Order refuses until the agreement is signed — or the clerk overrides it,
which is written on the invoice. A signature after the order binds the gun's copy
to it at Dispose. With FFL Settings › Require Agreements at Sale off, none of
this happens. Real create_order / dispose_order against the site; email is
patched (``frappe.sendmail``).
"""
from __future__ import annotations

import re
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core import agreements
from ffl_core.api import manual_order
from ffl_core.tests.test_agreements import _as, _png, _template
from ffl_core.tests.test_build_order import DEST_FFL, BuildOrderMixin

_TOKEN = re.compile(r"#t=([A-Za-z0-9_-]{43})")


class CounterOrderAgreements(BuildOrderMixin, FrappeTestCase):

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		# Templates only bind the Items that name them, so nothing else is gated.
		cls.per_gun = _template(scope="Each firearm sold",
			body="<p>{{ customer_name }} buys serial {{ serial_no }} for {{ price }}, as is.</p>").name
		cls.per_customer = _template(valid_days=365).name
		cls.gun = "BO-AGR-GUN"
		cls._item(cls.gun)
		frappe.db.set_value("Item", cls.gun, "ffl_required_agreement", cls.per_gun)
		# A used gun takes FFL Settings › Used Item Agreement; the setting is patched
		# per test (agreements._used_item_agreement), never saved.
		cls.used = "BO-AGR-USED"
		cls._item(cls.used)
		frappe.db.set_value("Item", cls.used, "is_used", 1)
		cls.pos = frappe.db.get_value("POS Profile", {"company": cls.company, "disabled": 0}, "name")

	def _order(self, *, dispose_now=0, email="buyer", paid=1, item=None, guns=1):
		serials = [self._stocked_serial("BOAGR" + frappe.generate_hash(length=8).upper(), item_code=item or self.gun)
			for _i in range(guns)]
		serial = serials[0] if guns == 1 else serials
		customer = self._indiv("BO Agr " + frappe.generate_hash(length=6))
		payload = {"buyer_type": "individual", "customer": customer, "pos_profile": self.pos,
			"destination_ffl": DEST_FFL, "dispose_now": dispose_now, "tax_exempt": 1,
			"items": [{"item_code": item or self.gun, "qty": 1, "rate": 650, "serials": [s]} for s in serials]}
		if paid:
			payload.update(payment_received=1, mode_of_payment="Cash")
		if email:
			payload["contact_email"] = f"{email}-{frappe.generate_hash(length=8)}@example.com"
		with patch("frappe.sendmail") as send:
			out = manual_order.create_order(payload)
		return out, serial, send

	def _pending_copy(self, si):
		return frappe.db.get_value("Customer Agreement", {"customer": si.customer, "template": self.per_gun,
			"status": "Pending"}, ["name", "sign_token_hash", "delivery_channel", "serial_no"], as_dict=True)

	def test_a_used_gun_sold_unsigned_is_booked_and_emails_the_link(self):
		with patch.object(agreements, "_used_item_agreement", return_value=self.per_customer):
			out, _serial, send = self._order(item=self.used)
		self.assertTrue(out["ok"])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.docstatus, 1)
		self.assertEqual([(a["template"], a["serial_no"], a["state"]) for a in out["agreements"]],
			[(self.per_customer, None, "sent")])
		self.assertEqual(send.call_args.kwargs["recipients"], [si.contact_email])
		self.assertEqual(frappe.db.get_value("Customer Agreement", {"customer": si.customer,
			"template": self.per_customer}, "status"), "Pending")

	def test_an_unsigned_order_emails_one_copy_per_gun(self):
		out, serial, send = self._order()
		self.assertTrue(out["ok"])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.docstatus, 1)
		self.assertEqual([(a["template"], a["serial_no"], a["state"]) for a in out["agreements"]],
			[(self.per_gun, serial, "sent")])
		self.assertIsNone(out["agreement_email_error"])
		self.assertEqual(send.call_count, 1)
		self.assertEqual(send.call_args.kwargs["recipients"], [si.contact_email])
		self.assertTrue(_TOKEN.search(send.call_args.kwargs["message"]))
		copy = self._pending_copy(si)
		self.assertEqual((copy.serial_no, copy.delivery_channel), (serial, "Email"))
		self.assertIn("650", frappe.db.get_value("Customer Agreement", copy.name, "body_snapshot"),
			"the copy names the gun's price on this order")

	def test_two_guns_email_one_copy_for_both(self):
		out, serials, send = self._order(guns=2)
		self.assertTrue(out["ok"])
		self.assertEqual(sorted((a["serial_no"], a["state"]) for a in out["agreements"]),
			sorted((s, "sent") for s in serials))
		self.assertEqual(send.call_count, 1)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		copy = self._pending_copy(si)
		self.assertEqual(sorted(g.serial_no for g in frappe.get_doc("Customer Agreement", copy.name).firearms),
			sorted(serials))
		# Resending for one gun re-issues the same copy, still covering both.
		with patch("frappe.sendmail"):
			manual_order.resend_order_agreement(si.name, self.per_gun, serials[1])
		self.assertEqual(frappe.db.count("Customer Agreement", {"customer": si.customer, "template": self.per_gun}), 1)
		self.assertEqual(len(frappe.get_doc("Customer Agreement", copy.name).firearms), 2)

	def test_no_email_still_books_the_order_and_says_so(self):
		out, _serial, send = self._order(email=None)
		self.assertTrue(frappe.db.get_value("Sales Invoice", out["sales_invoice"], "docstatus") == 1)
		self.assertEqual([a["state"] for a in out["agreements"]], ["failed"])
		self.assertTrue(out["agreement_email_error"])
		send.assert_not_called()

	def test_a_mail_failure_does_not_undo_the_order(self):
		with patch.object(agreements, "_send_link_email", side_effect=RuntimeError("smtp down")):
			out, _serial, _send = self._order()
		self.assertEqual(frappe.db.get_value("Sales Invoice", out["sales_invoice"], "docstatus"), 1)
		self.assertEqual([a["state"] for a in out["agreements"]], ["failed"])
		self.assertIn("smtp down", out["agreement_email_error"])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertFalse(self._pending_copy(si), "the failed copy is rolled back, not left half-made")

	def test_dispose_now_is_held_back_while_unsigned(self):
		out, serial, _send = self._order(dispose_now=1)
		self.assertFalse(out["disposed"])
		self.assertTrue(out["ship_held_for_agreements"])
		self.assertEqual(frappe.db.get_value("Sales Invoice", out["sales_invoice"], "update_stock"), 0)
		self.assertEqual(frappe.db.get_value("Serial No", serial, "status"), "Active")
		self.assertFalse(self._disp(out["sales_invoice"]))

	def test_an_order_with_no_gun_is_not_held(self):
		# Only a gun waits on Pending Order's Dispose; an accessory order has no such step.
		acc = "BO-AGR-ACC"
		self._item(acc, firearm=False)
		frappe.db.set_value("Item", acc, "ffl_required_agreement", self.per_customer)
		customer = self._indiv("BO Agr " + frappe.generate_hash(length=6))
		payload = {"buyer_type": "individual", "customer": customer, "pos_profile": self.pos,
			"dispose_now": 1, "tax_exempt": 1, "payment_received": 1, "mode_of_payment": "Cash",
			"items": [{"item_code": acc, "qty": 1, "rate": 20}]}
		with patch("frappe.sendmail"):
			out = manual_order.create_order(payload)
		self.assertFalse(out["ship_held_for_agreements"])
		self.assertEqual(frappe.db.get_value("Sales Invoice", out["sales_invoice"], "update_stock"), 1)

	def test_dispose_refuses_until_signed_or_overridden_with_a_comment(self):
		out, serial, _send = self._order()
		si_name = out["sales_invoice"]
		rows = {r["sales_invoice"]: r for r in manual_order.list_pending_dispositions()}
		row = rows[si_name]
		self.assertEqual(row["agreements_missing"], [{"template": self.per_gun, "serial_no": serial}])
		self.assertNotEqual(row["stage"], manual_order.STAGE_BLOCKED, "unsigned is a pill, never a blocked stage")
		with self.assertRaises(agreements.AgreementRequired):
			manual_order.dispose_order(si_name)
		self.assertFalse(self._disp(si_name))
		res = manual_order.dispose_order(si_name, override_agreement=1, reason="Buyer confirmed by phone")
		self.assertEqual(res["disposed_count"], 1)
		self.assertTrue(res["agreement_override"])
		comment = frappe.get_all("Comment", filters={"reference_doctype": "Sales Invoice", "reference_name": si_name,
			"comment_type": "Comment", "content": ("like", "%without the signed agreement%")}, pluck="content")
		self.assertEqual(len(comment), 1)
		for part in (self.per_gun, serial, "Buyer confirmed by phone", frappe.session.user):
			self.assertIn(part, comment[0])

	def test_signed_after_the_order_disposes_and_binds_the_copy(self):
		out, serial, send = self._order()
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		token = _TOKEN.search(send.call_args.kwargs["message"]).group(1)
		with patch("frappe.publish_realtime") as live, _as("Guest"):
			agreements.submit_signature(token, _png(), 1, "Jane Roe")
		self.assertEqual(live.call_args.args[0], "customer_agreement_signed")
		self.assertEqual(live.call_args.args[1], {"customer": si.customer, "agreement": self.per_gun,
			"serial_no": serial})
		self.assertEqual(live.call_args.kwargs["room"], "doctype:Customer Agreement")
		self.assertTrue(live.call_args.kwargs["after_commit"])
		state = manual_order.order_agreements(si.name)["rows"]
		self.assertEqual([(a["serial_no"], a["state"], a["signed"]) for a in state], [(serial, "signed", True)])
		res = manual_order.dispose_order(si.name)
		self.assertEqual(res["disposed_count"], 1)
		self.assertFalse(res["agreement_override"])
		copy = frappe.db.get_value("Customer Agreement", {"customer": si.customer, "serial_no": serial,
			"status": "Signed"}, ["reference_doctype", "reference_name"], as_dict=True)
		self.assertEqual((copy.reference_doctype, copy.reference_name), ("Sales Invoice", si.name))
		self.assertFalse(frappe.get_all("Comment", filters={"reference_name": si.name,
			"content": ("like", "%without the signed agreement%")}))

	def test_resend_issues_a_fresh_link(self):
		out, serial, _send = self._order()
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		before = self._pending_copy(si)
		row = manual_order.order_agreements(si.name)["rows"][0]
		self.assertEqual((row["state"], row["link_open"]), ("pending", True))
		with patch("frappe.sendmail") as send:
			res = manual_order.resend_order_agreement(si.name, self.per_gun, serial, email="other@example.com")
		after = self._pending_copy(si)
		self.assertEqual(after.name, before.name, "the same Pending copy, re-issued")
		self.assertNotEqual(after.sign_token_hash, before.sign_token_hash, "the old link stops working")
		self.assertEqual(res["email"], "other@example.com")
		self.assertEqual(send.call_args.kwargs["recipients"], ["other@example.com"])
		with self.assertRaises(frappe.ValidationError):
			manual_order.resend_order_agreement(si.name, self.per_customer)   # not one this sale needs

	def test_only_create_order_defers_the_gate(self):
		si = frappe.new_doc("Sales Invoice")
		si.customer = self._indiv("BO Agr Desk")
		si.append("items", {"item_code": self.gun, "qty": 1, "rate": 400, "serial_no": "BOAGR-DESK-1"})
		with self.assertRaises(agreements.AgreementRequired):
			agreements.refuse_missing_required(si)
		si.flags.ffl_defer_agreements = True
		agreements.refuse_missing_required(si)

	def test_with_the_sale_gate_off_nothing_waits_on_a_signature(self):
		# Patched, not saved: create_order commits, and a saved "off" would outlive the test.
		with patch.object(agreements, "sale_agreements_enforced", return_value=False):
			out, serial, send = self._order()
			si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
			self.assertEqual(si.docstatus, 1)
			self.assertEqual(out["agreements"], [])
			send.assert_not_called()
			self.assertFalse(frappe.db.exists("Customer Agreement", {"customer": si.customer}))
			row = {r["sales_invoice"]: r for r in manual_order.list_pending_dispositions()}[si.name]
			self.assertEqual((row["agreements"], row["agreements_missing"]), ([], []))
			desk = frappe.new_doc("Sales Invoice")
			desk.customer = si.customer
			desk.append("items", {"item_code": self.gun, "qty": 1, "rate": 400, "serial_no": serial})
			agreements.refuse_missing_required(desk)   # the register / desk gate is off too
			res = manual_order.dispose_order(si.name)
		self.assertEqual(res["disposed_count"], 1)
		self.assertFalse(res["agreement_override"])

	def test_the_sale_gate_switch_is_on_unless_turned_off(self):
		from ffl_core.patches import seed_enforce_sale_agreements
		field = frappe.get_meta("FFL Settings").get_field("enforce_sale_agreements")
		self.assertEqual((field.fieldtype, str(field.default)), ("Check", "1"))
		saved = frappe.db.sql("select value from tabSingles where doctype='FFL Settings' "
			"and field='enforce_sale_agreements'")
		try:
			frappe.db.delete("Singles", {"doctype": "FFL Settings", "field": "enforce_sale_agreements"})
			self.assertTrue(agreements.sale_agreements_enforced(), "an existing site that never saved it: on")
			seed_enforce_sale_agreements.execute()
			self.assertEqual(frappe.db.get_single_value("FFL Settings", "enforce_sale_agreements"), 1)
			frappe.db.set_single_value("FFL Settings", "enforce_sale_agreements", 0)
			self.assertFalse(agreements.sale_agreements_enforced())
			seed_enforce_sale_agreements.execute()
			self.assertFalse(agreements.sale_agreements_enforced(), "a store's choice stays")
		finally:
			frappe.db.set_single_value("FFL Settings", "enforce_sale_agreements", saved[0][0] if saved else 1)
