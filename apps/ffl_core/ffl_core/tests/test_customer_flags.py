"""Counter customer panel: flags, the ban gate and manager override, ID lookup, duplicates.

Site tests against the real Customer doctype (fields from
``setup/customer_profile.ensure``). Sales Orders and Sales Invoices are really
inserted; the POS Invoice gate is called on an unsaved document (a real POS
Invoice insert needs an open register shift) and its wiring is read from hooks.py.
"""
from __future__ import annotations

import random
from datetime import timedelta

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now_datetime, nowdate

from ffl_core import customer_flags
from ffl_core.tests.settings_sandbox import SettingsSandbox
from ffl_core.tests.test_customer_profile import _customer, _email, _hooks, _uniq, _user

_SWITCH = ("FFL Settings", "store_customer_id_numbers")
_ITEM = "_Test Ban Gate Item"
_GATE = "ffl_core.customer_flags.refuse_banned_customer"


def _banned(**fields):
	return _customer(customer_status="Banned", status_reason="Shoplifting", **fields)


def _company() -> str:
	return frappe.defaults.get_global_default("company") or frappe.get_all("Company", limit=1, pluck="name")[0]


def _item() -> str:
	if not frappe.db.exists("Item", _ITEM):
		frappe.get_doc({"doctype": "Item", "item_code": _ITEM, "item_name": _ITEM,
			"item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 0}).insert(ignore_permissions=True)
	return _ITEM


def _sales_order(customer):
	so = frappe.get_doc({"doctype": "Sales Order", "company": _company(), "customer": customer,
		"transaction_date": nowdate(), "delivery_date": add_days(nowdate(), 7),
		"items": [{"item_code": _item(), "qty": 1, "rate": 10, "delivery_date": add_days(nowdate(), 7)}]})
	so.flags.ignore_permissions = True
	return so


def _sales_invoice(customer, **fields):
	si = frappe.get_doc({"doctype": "Sales Invoice", "company": _company(), "customer": customer,
		"items": [{"item_code": _item(), "qty": 1, "rate": 10}], **fields})
	si.flags.ignore_permissions = True
	return si


def _override(customer, minutes=customer_flags.OVERRIDE_MINUTES):
	frappe.db.set_value("Customer", customer, {"ban_override_until": now_datetime() + timedelta(minutes=minutes),
		"ban_override_by": "Administrator"}, update_modified=False)


class Wiring(FrappeTestCase):

	def test_the_gate_runs_on_every_counter_sale_doctype(self):
		events = _hooks()["doc_events"]
		for doctype in ("POS Invoice", "Sales Invoice", "Sales Order"):
			self.assertIn(_GATE, events[doctype]["validate"], doctype)

	def test_bound_book_records_are_never_gated(self):
		events = _hooks()["doc_events"]
		for doctype in ("FFL Disposition", "Delivery Note"):
			self.assertNotIn(_GATE, str(events.get(doctype, "")), doctype)


class Flags(SettingsSandbox, FrappeTestCase):

	def test_an_active_customer_has_nothing_to_say(self):
		c = _customer()
		f = customer_flags.get_customer_flags(c.name)
		self.assertEqual((f["status"], f["banned"], f["alerts"], f["ids"]), ("Active", False, [], []))
		self.assertEqual(f["status_reason"], "")

	def test_banned_with_its_reason_and_no_override(self):
		f = customer_flags.get_customer_flags(_banned().name)
		self.assertTrue(f["banned"])
		self.assertEqual(f["status_reason"], "Shoplifting")
		self.assertIsNone(f["override_until"])

	def test_an_open_override_is_not_banned_and_says_until_when(self):
		c = _banned()
		_override(c.name)
		f = customer_flags.get_customer_flags(c.name)
		self.assertFalse(f["banned"])
		self.assertTrue(f["override_until"])

	def test_an_expired_override_is_banned_again(self):
		c = _banned()
		_override(c.name, minutes=-1)
		self.assertTrue(customer_flags.get_customer_flags(c.name)["banned"])

	def test_only_active_alerts(self):
		c = _customer(customer_alerts=[{"message": "Owes $40", "active": 1},
			{"message": "Old note", "active": 0}])
		self.assertEqual([a["message"] for a in customer_flags.get_customer_flags(c.name)["alerts"]],
			["Owes $40"])

	def test_ids_expired_or_expiring_within_30_days_never_with_the_number(self):
		self.set_single(*_SWITCH, 1)
		c = _customer(customer_ids=[
			{"id_type": "Driver License", "id_number": "D1234567", "expiry_date": add_days(nowdate(), -1)},
			{"id_type": "CCW", "id_number": "C1", "expiry_date": add_days(nowdate(), 10)},
			{"id_type": "Passport", "id_number": "P1", "expiry_date": add_days(nowdate(), 90)}])
		f = customer_flags.get_customer_flags(c.name)
		self.assertEqual([(i["id_type"], i["expired"]) for i in f["ids"]],
			[("Driver License", True), ("CCW", False)])
		self.assertNotIn("D1234567", frappe.as_json(f))

	def test_family_and_tags(self):
		head = _customer()
		member = _customer(primary_customer=head.name, relationship="Spouse")
		frappe.get_doc("Customer", head.name).add_tag("VIP")
		self.assertEqual(customer_flags.get_customer_flags(head.name)["family"]["members"], 1)
		self.assertIn("VIP", customer_flags.get_customer_flags(head.name)["tags"])
		fam = customer_flags.get_customer_flags(member.name)["family"]
		self.assertEqual((fam["primary"]["name"], fam["relationship"]), (head.name, "Spouse"))


class WhoMayBan(FrappeTestCase):

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")

	def test_a_clerk_cannot_ban_or_lift_a_ban_or_edit_the_reason(self):
		active, banned = _customer(), _banned()
		frappe.set_user(_user("flags-clerk@example.com", "Sales User"))
		for name, change in ((active.name, {"customer_status": "Banned", "status_reason": "x"}),
				(banned.name, {"customer_status": "Active"}),
				(banned.name, {"status_reason": "Changed"})):
			doc = frappe.get_doc("Customer", name)
			doc.update(change)
			doc.flags.ignore_permissions = True
			with self.assertRaises(frappe.PermissionError, msg=str(change)):
				doc.save()

	def test_a_clerk_cannot_create_a_banned_customer(self):
		frappe.set_user(_user("flags-clerk@example.com", "Sales User"))
		with self.assertRaises(frappe.PermissionError):
			_banned()

	def test_a_clerk_may_still_edit_anything_else(self):
		banned = _banned()
		frappe.set_user(_user("flags-clerk@example.com", "Sales User"))
		doc = frappe.get_doc("Customer", banned.name)
		doc.mobile_no = "5125550100"
		doc.flags.ignore_permissions = True
		doc.save()

	def test_a_manager_bans_and_lifts(self):
		c = _customer()
		frappe.set_user(_user("flags-manager@example.com", "Sales Manager"))
		doc = frappe.get_doc("Customer", c.name)
		doc.update({"customer_status": "Banned", "status_reason": "Threats"})
		doc.flags.ignore_permissions = True
		doc.save()
		doc.customer_status = "Active"
		doc.save()

	def test_a_save_never_writes_the_override_fields(self):
		c = _banned()
		doc = frappe.get_doc("Customer", c.name)
		doc.ban_override_until = now_datetime() + timedelta(days=30)
		doc.ban_override_by = "Administrator"
		doc.save()
		self.assertIsNone(frappe.db.get_value("Customer", c.name, "ban_override_until"))
		# …and a form loaded before an override does not erase it on save.
		stale = frappe.get_doc("Customer", c.name)
		_override(c.name)
		stale.mobile_no = "5125550101"
		stale.save()
		self.assertTrue(frappe.db.get_value("Customer", c.name, "ban_override_until"))


class Override(FrappeTestCase):

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")

	def test_a_manager_opens_30_minutes_and_the_timeline_says_who_and_why(self):
		c = _banned()
		frappe.set_user(_user("flags-manager@example.com", "Sales Manager"))
		out = customer_flags.override_customer_ban(c.name, "Picking up a repair")
		frappe.set_user("Administrator")
		row = frappe.db.get_value("Customer", c.name, ["customer_status", "ban_override_until",
			"ban_override_by"], as_dict=True)
		self.assertEqual((row.customer_status, row.ban_override_by), ("Banned", "flags-manager@example.com"))
		minutes = (row.ban_override_until - now_datetime()).total_seconds() / 60
		self.assertTrue(28 < minutes <= 30, minutes)
		self.assertEqual(out["by"], "flags-manager@example.com")
		self.assertTrue(frappe.db.exists("Comment", {"reference_doctype": "Customer",
			"reference_name": c.name, "content": ("like", "%Picking up a repair%")}))
		self.assertFalse(customer_flags.is_blocked(c.name))

	def test_a_clerk_is_refused(self):
		c = _banned()
		frappe.set_user(_user("flags-clerk@example.com", "Sales User"))
		with self.assertRaises(frappe.PermissionError):
			customer_flags.override_customer_ban(c.name, "please")

	def test_a_reason_is_required(self):
		with self.assertRaises(frappe.ValidationError):
			customer_flags.override_customer_ban(_banned().name, "  ")

	def test_only_a_banned_customer(self):
		with self.assertRaises(frappe.ValidationError):
			customer_flags.override_customer_ban(_customer().name, "why")


class SaleGate(FrappeTestCase):

	def test_a_sales_order_for_a_banned_customer_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Banned"):
			_sales_order(_banned().name).insert()

	def test_a_sales_invoice_for_a_banned_customer_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Banned"):
			_sales_invoice(_banned().name).insert()

	def test_an_open_override_lets_it_through_and_it_outlives_the_window(self):
		c = _banned()
		_override(c.name)
		so = _sales_order(c.name)
		so.insert()
		_override(c.name, minutes=-1)   # window closed: the draft still saves and submits
		so.reload()
		so.save()
		so.submit()

	def test_a_draft_switched_to_a_banned_customer_is_refused(self):
		so = _sales_order(_customer().name)
		so.insert()
		so.customer = _banned().name
		with self.assertRaisesRegex(frappe.ValidationError, "Banned"):
			so.save()

	def test_an_existing_draft_whose_customer_was_banned_later_still_saves(self):
		c = _customer()
		so = _sales_order(c.name)
		so.insert()
		frappe.db.set_value("Customer", c.name, {"customer_status": "Banned", "status_reason": "x"})
		so.reload()
		so.save()

	def test_a_web_order_invoice_is_booked_and_noted_on_the_customer(self):
		c = _banned()
		si = _sales_invoice(c.name)
		si.woo_online_order = "WOO-TEST-BANNED"   # a channel mirror: the goods already left
		si.set("__islocal", 1)   # as on insert
		customer_flags.refuse_banned_customer(si)
		self.assertTrue(frappe.db.exists("Comment", {"reference_doctype": "Customer",
			"reference_name": c.name, "content": ("like", "%WOO-TEST-BANNED%")}))

	def test_returns_and_pos_consolidation_pass(self):
		c = _banned()
		for fields in ({"is_return": 1}, {"is_consolidated": 1}):
			customer_flags.refuse_banned_customer(_sales_invoice(c.name, **fields))

	def test_a_pos_invoice_for_a_banned_customer_is_refused(self):
		inv = frappe.new_doc("POS Invoice")
		inv.customer = _banned().name
		with self.assertRaisesRegex(frappe.ValidationError, "Banned"):
			customer_flags.refuse_banned_customer(inv)


class IdLookupAndDuplicates(SettingsSandbox, FrappeTestCase):

	def test_scan_finds_the_customer_ignoring_case_spaces_and_dashes(self):
		self.set_single(*_SWITCH, 1)
		number = f"D{frappe.generate_hash(length=7).upper()}"
		c = _customer(customer_ids=[{"id_type": "Driver License", "id_number": f"{number[:4]}-{number[4:]}"}])
		self.assertEqual([r["name"] for r in customer_flags.find_customers_by_id_number(number.lower())], [c.name])

	def test_scan_finds_nothing_while_the_store_keeps_no_id_numbers(self):
		self.set_single(*_SWITCH, 0)
		self.assertEqual(customer_flags.find_customers_by_id_number("D1234567"), [])

	def test_duplicates_by_email_alias_phone_and_id(self):
		self.set_single(*_SWITCH, 1)
		email, number = _email(), f"X{frappe.generate_hash(length=8).upper()}"
		phone = f"512{random.randint(1000000, 9999999)}"
		c = _customer(email_id=email, mobile_no=f"+1 ({phone[:3]}) {phone[3:6]}-{phone[6:]}",
			customer_ids=[{"id_type": "Driver License", "id_number": number}])
		rows = customer_flags.find_possible_duplicates(email=email.upper(), phone=phone, id_number=number)
		self.assertEqual([(r["name"], sorted(r["matched"])) for r in rows],
			[(c.name, ["email", "id_number", "phone"])])

	def test_no_keys_no_matches(self):
		self.assertEqual(customer_flags.find_possible_duplicates(email="", phone="12", id_number=""), [])


class PendingOrderBadge(FrappeTestCase):

	def test_counter_rows_by_customer(self):
		banned, fine = _banned(), _customer()
		out = customer_flags.banned_order_rows(customers=[banned.name, fine.name])
		self.assertEqual(out["customers"], [banned.name])

	def test_a_web_order_by_its_billing_email(self):
		if not frappe.db.exists("DocType", "Woo Online Order"):
			self.skipTest("ffl_woo_sync not installed")
		email = _email()
		_banned(email_id=email)
		order = frappe.get_doc({"doctype": "Woo Online Order", "woo_order_id": frappe.generate_hash(length=8),
			"customer_email": email, "customer_name": _uniq("Web")})
		order.flags.ignore_permissions = True
		order.flags.ignore_mandatory = True
		order.db_insert()
		self.assertEqual(customer_flags.banned_order_rows(woo_orders=[order.name])["woo"], [order.name])
