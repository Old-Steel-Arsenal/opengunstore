"""Agreement modes: Required (optional / by item / every sale), Signed (per customer /
each firearm sold), Item Group triggers, and the register's Agreements list.

Site tests against the real doctypes. Sales are unsaved Sales Invoices handed to the
gate directly (``refuse_missing_required`` runs at before_submit; a real submit
needs stock for the guns); the submit / cancel bookkeeping is called the same way.
"""
from __future__ import annotations

import frappe
from frappe.utils import add_days, nowdate

from ffl_core import agreements
from ffl_core.firearm import used_firearm_item_doc
from ffl_core.tests.test_agreements import _Base, _template
from ffl_core.tests.test_customer_profile import _uniq

_NO_RETURN = ("<p>{{ customer_name }} buys {{ item_name }} ({{ manufacturer }} {{ model }}, {{ caliber }}), "
	"serial {{ serial_no }}, for {{ price }}, as is, no returns.</p>")


def _group(parent="All Item Groups", **fields):
	doc = frappe.get_doc({"doctype": "Item Group", "item_group_name": _uniq("Group"),
		"parent_item_group": parent, "is_group": 1, **fields})
	doc.insert(ignore_permissions=True)
	return doc


def _item(group="All Item Groups", **fields):
	doc = frappe.get_doc({"doctype": "Item", "item_code": _uniq("Item"), "item_group": group,
		"stock_uom": "Nos", "is_stock_item": 0, **fields})
	doc.insert(ignore_permissions=True)
	return doc


def _gun():
	if not frappe.db.exists("Caliber", ".357 Magnum"):
		frappe.get_doc({"doctype": "Caliber", "caliber_name": ".357 Magnum"}).insert(ignore_permissions=True)
	code = _uniq("Used Rifle")
	frappe.get_doc(used_firearm_item_doc(item_code=code, item_name=code, description=code,
		caliber=".357 Magnum", firearm_type="Pistol")).insert(ignore_permissions=True)
	serial = "AM" + frappe.generate_hash(length=8).upper()
	frappe.get_doc({"doctype": "Serial No", "serial_no": serial, "item_code": code,
		"company": frappe.defaults.get_global_default("company")}).insert(ignore_permissions=True)
	return code, serial


class Modes(_Base):

	def sale(self, *rows):
		si = frappe.new_doc("Sales Invoice")
		si.customer = self.cust.name
		for item_code, serial in rows:
			si.append("items", {"item_code": item_code, "qty": 1, "rate": 400, "serial_no": serial or ""})
		return si

	def gate(self, si):
		agreements.refuse_missing_required(si)

	def test_by_item_through_the_item_group_and_its_sub_groups(self):
		parent = _group(ffl_required_agreement=self.tpl.name)
		child = _group(parent=parent.name)
		item = _item(group=child.name)
		self.assertEqual(agreements.required_for_item(item.name), self.tpl.name)
		with self.assertRaises(agreements.AgreementRequired):
			self.gate(self.sale((item.name, None)))
		other = _template()
		own = _item(group=child.name, ffl_required_agreement=other.name)
		self.assertEqual(agreements.required_for_item(own.name), other.name, "the Item's own wins")
		_, token = self.request()
		self.sign(token)
		self.gate(self.sale((item.name, None)))

	def test_optional_never_stops_a_sale_even_when_an_item_names_it(self):
		tpl = _template(requirement="Optional")
		item = _item(ffl_required_agreement=tpl.name)
		self.gate(self.sale((item.name, None)))
		row = [r for r in agreements.agreements_for_sale(self.cust.name, [{"item_code": item.name}])["rows"]
			if r["template"] == tpl.name]
		self.assertEqual([(r["required"], r["state"]) for r in row], [(False, "missing")])

	def test_every_sale_needs_it_whatever_is_sold(self):
		tpl = _template(requirement="Required for every sale")
		# Switched off after: the class shares one transaction, and it would gate every later sale.
		self.addCleanup(frappe.db.set_value, "Agreement Template", tpl.name, "active", 0)
		with self.assertRaises(agreements.AgreementRequired):
			self.gate(self.sale((_item().name, None)))
		_, token = self.request(tpl=tpl)
		self.sign(token)
		self.gate(self.sale((_item().name, None)))

	def test_every_sale_leaves_an_ffl_dealer_alone(self):
		tpl = _template(requirement="Required for every sale")
		self.addCleanup(frappe.db.set_value, "Agreement Template", tpl.name, "active", 0)
		frappe.db.set_value("Customer", self.cust.name, "is_ffl_dealer", 1)
		self.gate(self.sale((_item().name, None)))

	def test_the_panel_says_how_each_agreement_is_required(self):
		tpl = _template(requirement="Optional", show_on_counter_panel=1)
		row = [r for r in agreements.counter_panel_rows(self.cust.name) if r["template"] == tpl.name]
		self.assertEqual([(r["state"], r["requirement"]) for r in row], [("missing", "Optional")])

	def test_a_template_saved_before_the_settings_reads_as_by_item(self):
		frappe.db.set_value("Agreement Template", self.tpl.name, {"requirement": None, "scope": None})
		item = _item(ffl_required_agreement=self.tpl.name)
		with self.assertRaises(agreements.AgreementRequired):
			self.gate(self.sale((item.name, None)))


class EachFirearmSold(_Base):

	def setUp(self):
		super().setUp()
		self.tpl = _template(scope="Each firearm sold", body=_NO_RETURN)
		self.code, self.serial = _gun()
		frappe.db.set_value("Item", self.code, "ffl_required_agreement", self.tpl.name)

	def ask(self, serial=None, price=None):
		r = agreements.request_agreement(self.cust.name, self.tpl.name, "qr", 1, None, serial or self.serial, price)
		return r, r["sign_url"].split("#t=", 1)[1]

	def sale(self, name=None):
		si = frappe.new_doc("Sales Invoice")
		si.customer = self.cust.name
		si.append("items", {"item_code": self.code, "qty": 1, "rate": 400, "serial_no": self.serial})
		if name:
			si.name = name
			si.set("__islocal", 0)
		return si

	def test_the_gun_must_be_named_and_its_details_fill_the_text(self):
		with self.assertRaises(frappe.ValidationError):
			agreements.request_agreement(self.cust.name, self.tpl.name, "qr", 1)
		r, _ = self.ask(price=399.5)
		doc = frappe.get_doc("Customer Agreement", r["name"])
		self.assertEqual((doc.serial_no, doc.item_code), (self.serial, self.code))
		self.assertIn(self.serial, doc.body_snapshot)
		self.assertIn(".357 Magnum", doc.body_snapshot)
		self.assertIn("399.50", doc.body_snapshot)

	def test_each_gun_needs_its_own_copy(self):
		with self.assertRaises(agreements.AgreementRequired):
			agreements.refuse_missing_required(self.sale())
		_, token = self.ask()
		self.sign(token)
		agreements.refuse_missing_required(self.sale())
		code2, serial2 = _gun()
		frappe.db.set_value("Item", code2, "ffl_required_agreement", self.tpl.name)
		si = self.sale()
		si.append("items", {"item_code": code2, "qty": 1, "rate": 300, "serial_no": serial2})
		with self.assertRaisesRegex(agreements.AgreementRequired, serial2):
			agreements.refuse_missing_required(si)

	def test_a_copy_belongs_to_the_sale_that_used_it_until_that_sale_is_cancelled(self):
		_, token = self.ask()
		self.sign(token)
		first = self.sale(name="SINV-AGR-TEST-1")
		agreements.mark_firearm_agreements_used(first)
		agreements.refuse_missing_required(self.sale(name="SINV-AGR-TEST-1"))   # the same sale still passes
		with self.assertRaises(agreements.AgreementRequired):
			agreements.refuse_missing_required(self.sale(name="SINV-AGR-TEST-2"))   # selling it again needs a new one
		agreements.release_firearm_agreements(first)
		agreements.refuse_missing_required(self.sale(name="SINV-AGR-TEST-2"))

	def test_a_sale_taking_the_copy_rebuilds_its_pdf_in_place(self):
		r, token = self.ask()
		self.sign(token)
		agreements.finish_signed_agreement(r["name"])
		before = frappe.db.get_value("Customer Agreement", r["name"], "signed_pdf")
		self.assertTrue(before)
		agreements.mark_firearm_agreements_used(self.sale(name="SINV-AGR-TEST-9"))
		agreements.rebuild_signed_pdf(r["name"])
		after = frappe.db.get_value("Customer Agreement", r["name"], "signed_pdf")
		self.assertEqual(frappe.db.get_value("Customer Agreement", r["name"], "reference_name"), "SINV-AGR-TEST-9")
		self.assertEqual(frappe.db.count("File", {"attached_to_doctype": "Customer Agreement",
			"attached_to_name": r["name"], "attached_to_field": "signed_pdf"}), 1)
		self.assertTrue(after)

	def test_the_register_lists_one_row_per_gun_and_never_puts_it_on_the_panel(self):
		frappe.db.set_value("Agreement Template", self.tpl.name, "show_on_counter_panel", 1)
		code2, serial2 = _gun()
		rows = agreements.agreements_for_sale(self.cust.name, [
			{"item_code": self.code, "serial_no": self.serial, "rate": 400},
			{"item_code": code2, "serial_no": serial2, "rate": 300}])["rows"]
		mine = sorted((r["serial_no"], r["required"]) for r in rows if r["template"] == self.tpl.name)
		self.assertEqual(mine, sorted([(self.serial, True), (serial2, False)]))
		self.assertNotIn(self.tpl.name, [r["template"] for r in agreements.counter_panel_rows(self.cust.name)])

	def test_a_per_customer_copy_does_not_count_for_a_gun(self):
		per_customer = agreements.agreement_status(self.cust.name, self.tpl.name)
		self.assertEqual(per_customer["state"], "missing")
		_, token = self.ask()
		self.sign(token)
		self.assertEqual(agreements.agreement_status(self.cust.name, self.tpl.name)["state"], "missing")
		self.assertEqual(agreements.agreement_status(self.cust.name, self.tpl.name, serial_no=self.serial)["state"],
			"signed")
		self.assertIsNone(frappe.db.get_value("Customer Agreement",
			{"serial_no": self.serial, "customer": self.cust.name}, "expires_on"))

	def test_old_signed_copies_do_not_expire_by_days(self):
		frappe.db.set_value("Agreement Template", self.tpl.name, "valid_days", 1)
		r, token = self.ask()
		self.sign(token)
		frappe.db.set_value("Customer Agreement", r["name"], "expires_on", add_days(nowdate(), -5))
		self.assertEqual(agreements.agreement_status(self.cust.name, self.tpl.name, serial_no=self.serial)["state"],
			"signed")
