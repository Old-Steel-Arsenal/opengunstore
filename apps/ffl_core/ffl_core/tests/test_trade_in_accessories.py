"""POS Trade-in of accessories and ammo (non-serialized stock), alone or with a gun.

With credit the accessories are parked as a DRAFT Material Receipt marked as a trade-in
(trade_in._park_accessories) and get a locked credit line of their own, which the submit gate
(compliance/trade_in_credit) ties to that receipt's value and books at checkout. A pay-now buy puts
them on the seller's paid Purchase Receipt. The counter (Sales User only) does all of it, and may make
a new accessory / ammo Item for what the store has never stocked (trade_in.create_accessory_item).
"""
from __future__ import annotations

import re
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_core.api import trade_in
from ffl_core.compliance.trade_in_credit import validate_trade_in_credit_rows
from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM, TRADE_IN_SELLER_FIELD
from ffl_core.tests.test_trade_in_counter_roles import SALES_USER, _ensure_user

SELLER = "_Test Trade-in Seller"
AMMO = "TIA-9MM-BOX"
MAG = "TIA-G19-MAG"
SALE_ITEM = "TIA-SALE-ACC"


def _item(item_code: str, item_group: str, **fields) -> str:
	if not frappe.db.exists("Item", item_code):
		frappe.get_doc({"doctype": "Item", "item_code": item_code, "item_name": item_code, "stock_uom": "Nos",
			"item_group": item_group, "is_stock_item": 1, **fields}).insert(ignore_permissions=True)
	return item_code


class TradeInAccessories(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		from ffl_core.setup.trade_in_credit import ensure
		ensure()
		from ffl_core.setup.pos_compat import ensure_role_grants
		ensure_role_grants()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		from ffl_core.tests.pos_opening import todays_pos_opening
		todays_pos_opening(cls.company, "FFL Test")
		if not frappe.db.exists("Customer", SELLER):
			frappe.get_doc({"doctype": "Customer", "customer_name": SELLER, "customer_group": "Individual",
				"territory": "All Territories"}).insert(ignore_permissions=True)
		_ensure_user(SALES_USER, "Sales User")
		_item(AMMO, "Pistol Ammo")
		_item(MAG, "Magazine")
		_item(SALE_ITEM, "Accessory", is_stock_item=0)
		from ffl_core.api.firearm_intake import _default_warehouse
		cls.warehouse = _default_warehouse(cls.company)

	def setUp(self) -> None:
		super().setUp()
		from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError

		p = patch.object(FastBoundClient, "from_settings", side_effect=FastBoundConfigError("off in accessory tests"))
		p.start()
		self.addCleanup(p.stop)

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		super().tearDown()

	def _payload(self, suffix: str, gun: bool = True, **over) -> dict:
		base = {
			"id_type": "Driver License", "id_number": f"DL-{suffix}", "source_name": SELLER,
			"source_first_name": "Testy", "source_last_name": "Seller",
			"source_address": "123 Main St, Springfield TX 75024",
			"accessories": [{"item_code": AMMO, "qty": 3, "rate": 12}, {"item_code": MAG, "qty": 2, "rate": 15}],
			"has_firearm": 1 if gun else 0,
		}
		if gun:
			base.update({"serial_no": f"TIA-{suffix}", "manufacturer": "Glock", "model_name": "19", "caliber": "9mm",
				"firearm_type": "Pistol", "condition_grade": "Good", "acquisition_cost": 300})
		base.update(over)
		return base

	def _as_counter(self, payload: dict) -> dict:
		frappe.set_user(SALES_USER)
		try:
			return trade_in.create_trade_in_intake(payload)
		finally:
			frappe.set_user("Administrator")

	def _qty(self, item_code: str) -> float:
		return flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": self.warehouse}, "actual_qty"))

	def _credit_invoice(self, credits: list[dict]):
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = "FFL Test"
		inv.customer = SELLER
		inv.append("items", {"item_code": SALE_ITEM, "qty": 1, "rate": 500, "price_list_rate": 500})
		for c in credits:
			inv.append("items", {"item_code": TRADE_IN_CREDIT_ITEM, "qty": 1, "rate": c["rate"],
				"price_list_rate": c["rate"], "ffl_trade_in_acquisition": c.get("acquisition"),
				"ffl_trade_in_stock_entry": c.get("stock_entry")})
		inv.set_missing_values()
		for row, rate in zip(inv.items, [500] + [c["rate"] for c in credits]):
			row.rate = row.price_list_rate = rate
		inv.flags.ignore_permissions = True
		inv.save()
		inv.payments = []
		inv.append("payments", {"mode_of_payment": "Cash", "amount": inv.grand_total})
		inv.save()
		return inv

	# ------------------------------------------------------------------ credit

	def test_accessories_only_credit_parks_a_draft_receipt(self):
		before = self._qty(AMMO)
		res = self._as_counter(self._payload("acc-credit", gun=False, apply_credit=1))
		self.assertIsNone(res["acquisition"])
		self.assertIsNone(res.get("credit"))
		[credit] = res["credits"]
		self.assertEqual(credit["item_code"], TRADE_IN_CREDIT_ITEM)
		self.assertEqual(flt(credit["rate"]), -66.0)  # 3 x 12 + 2 x 15
		se = frappe.get_doc("Stock Entry", credit["stock_entry"])
		self.assertEqual((se.docstatus, se.stock_entry_type), (0, "Material Receipt"))
		self.assertEqual(se.get(TRADE_IN_SELLER_FIELD), SELLER)
		self.assertEqual(se.owner, SALES_USER)
		self.assertEqual(self._qty(AMMO), before, "nothing reaches stock before the sale submits")

	def test_mixed_credit_books_both_at_checkout(self):
		ammo, mag = self._qty(AMMO), self._qty(MAG)
		res = self._as_counter(self._payload("mixed-credit", apply_credit=1))
		self.assertEqual([("acquisition" in c, "stock_entry" in c) for c in res["credits"]],
			[(True, False), (False, True)])
		self.assertEqual([flt(c["rate"]) for c in res["credits"]], [-300.0, -66.0])
		inv = self._credit_invoice(res["credits"])
		inv.submit()
		self.assertEqual(frappe.db.get_value("FFL Acquisition", res["acquisition"], "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Stock Entry", res["accessory_receipt"], "docstatus"), 1)
		self.assertEqual(self._qty(AMMO), ammo + 3)
		self.assertEqual(self._qty(MAG), mag + 2)
		# Stock in at the value given; the other side is Stock Adjustment, where the credit line books too.
		from ffl_core.setup.trade_in_credit import credit_income_account
		gl = frappe.get_all("GL Entry", filters={"voucher_type": "Stock Entry", "voucher_no": res["accessory_receipt"],
			"is_cancelled": 0}, fields=["account", "debit", "credit"])
		self.assertEqual(sum(flt(g.credit) for g in gl if g.account == credit_income_account(self.company)), 66.0)
		credit_rows = [r for r in inv.items if r.item_code == TRADE_IN_CREDIT_ITEM]
		self.assertEqual({r.income_account for r in credit_rows}, {credit_income_account(self.company)})

	def test_accessory_credit_is_spent_once(self):
		res = self._as_counter(self._payload("spent-once", gun=False, apply_credit=1))
		self._credit_invoice(res["credits"]).submit()
		other = frappe.new_doc("POS Invoice")
		other.docstatus = 1
		other.append("items", {"item_code": TRADE_IN_CREDIT_ITEM, "qty": 1, "rate": -66,
			"ffl_trade_in_stock_entry": res["accessory_receipt"]})
		with self.assertRaisesRegex(frappe.ValidationError, "only be spent once"):
			validate_trade_in_credit_rows(other)

	def _gate(self, **row):
		doc = frappe.new_doc("POS Invoice")
		doc.docstatus = 1
		doc.append("items", {"item_code": TRADE_IN_CREDIT_ITEM, "qty": 1, **row})
		validate_trade_in_credit_rows(doc)

	def test_gate_holds_the_accessory_credit_to_its_receipt(self):
		res = self._as_counter(self._payload("gate", gun=False, apply_credit=1))
		se = res["accessory_receipt"]
		self._gate(rate=-66, ffl_trade_in_stock_entry=se)  # well-formed: passes
		with self.assertRaisesRegex(frappe.ValidationError, "must equal the recorded trade-in value"):
			self._gate(rate=-90, ffl_trade_in_stock_entry=se)
		with self.assertRaisesRegex(frappe.ValidationError, "quantity 1"):
			self._gate(rate=-66, qty=2, ffl_trade_in_stock_entry=se)
		# A Material Receipt that is no trade-in cannot back a credit.
		plain = frappe.get_doc({"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
			"company": self.company, "items": [{"item_code": AMMO, "qty": 1, "t_warehouse": self.warehouse,
				"basic_rate": 66}]}).insert(ignore_permissions=True)
		with self.assertRaisesRegex(frappe.ValidationError, "missing or not submitted"):
			self._gate(rate=-66, ffl_trade_in_stock_entry=plain.name)
		# One row, one record.
		gun = self._as_counter(self._payload("gate-gun", apply_credit=1, accessories=[]))
		with self.assertRaisesRegex(frappe.ValidationError, "no linked trade-in record"):
			self._gate(rate=-300, ffl_trade_in_stock_entry=se, ffl_trade_in_acquisition=gun["acquisition"])

	# ------------------------------------------------------------------ pay now

	def test_accessories_only_pay_now_is_a_paid_receipt(self):
		ammo = self._qty(AMMO)
		res = self._as_counter(self._payload("acc-paynow", gun=False, payout_method="Cash"))
		pr = frappe.get_doc("Purchase Receipt", res["purchase_receipt"])
		self.assertEqual(pr.docstatus, 1)
		self.assertEqual(sorted((r.item_code, flt(r.qty), flt(r.rate)) for r in pr.items),
			[(AMMO, 3.0, 12.0), (MAG, 2.0, 15.0)])
		self.assertIsNone(res["acquisition"])
		self.assertFalse(frappe.db.exists("FFL Acquisition", {"purchase_receipt": pr.name}))
		self.assertEqual(flt(frappe.db.get_value("Cash Drawer Entry", res["payout"]["entry"], "amount")), 66.0)
		self.assertEqual(self._qty(AMMO), ammo + 3)

	def test_mixed_pay_now_pays_for_everything_on_one_receipt(self):
		res = self._as_counter(self._payload("mixed-paynow", payout_method="Cash"))
		pr = frappe.get_doc("Purchase Receipt", res["purchase_receipt"])
		self.assertEqual(len(pr.items), 3)
		self.assertEqual(frappe.db.get_value("FFL Acquisition", res["acquisition"], "docstatus"), 1)
		self.assertTrue(frappe.db.exists("Serial No", "TIA-mixed-paynow"))
		self.assertEqual(flt(frappe.db.get_value("Cash Drawer Entry", res["payout"]["entry"], "amount")), 366.0)

	# ------------------------------------------------------------------ input

	def test_accessory_lines_are_checked(self):
		gun_item = _item("TIA-USED-PISTOL", "Pistol", has_serial_no=1, is_firearm=1)
		serialized = _item("TIA-SERIAL-OPTIC", "Optic", has_serial_no=1)
		cases = {
			"a firearm": ([{"item_code": gun_item, "qty": 1, "rate": 10}], "not an accessory or ammo item"),
			"serialized": ([{"item_code": serialized, "qty": 1, "rate": 10}], "not an accessory or ammo item"),
			"no qty": ([{"item_code": AMMO, "qty": 0, "rate": 10}], "enter the quantity"),
			"no value": ([{"item_code": AMMO, "qty": 1, "rate": 0}], "enter the quantity"),
			"unknown": ([{"item_code": "TIA-NO-SUCH-ITEM", "qty": 1, "rate": 10}], "does not exist"),
		}
		for case, (lines, msg) in cases.items():
			with self.subTest(case=case), self.assertRaisesRegex(frappe.ValidationError, msg):
				self._as_counter(self._payload(f"bad-{case}", gun=False, apply_credit=1, accessories=lines))

	def test_nothing_to_trade_in_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Nothing to trade in"):
			self._as_counter(self._payload("nothing", gun=False, apply_credit=1, accessories=[{"item_code": ""}]))

	def test_without_a_gun_the_gun_fields_are_not_asked_for(self):
		res = self._as_counter(self._payload("no-gun", gun=False, apply_credit=1, serial_no=""))
		self.assertTrue(res["ok"])
		# …but a gun the dialog says is there still needs all of them.
		with self.assertRaisesRegex(frappe.ValidationError, "serial_no"):
			self._as_counter(self._payload("gun-missing", apply_credit=1, serial_no=""))

	def test_an_api_caller_without_the_new_keys_is_unchanged(self):
		payload = self._payload("legacy", apply_credit=1)
		del payload["accessories"], payload["has_firearm"]
		res = self._as_counter(payload)
		self.assertEqual(flt(res["credit"]["rate"]), -300.0)
		self.assertEqual(len(res["credits"]), 1)
		self.assertNotIn("accessory_receipt", res)

	# ------------------------------------------------------------------ new item

	def test_counter_makes_a_new_accessory_item(self):
		frappe.set_user(SALES_USER)
		made = trade_in.create_accessory_item("TIA New Sling", "Slings & Mounts", 25)
		frappe.set_user("Administrator")
		item = frappe.db.get_value("Item", made["item_code"],
			["item_group", "is_stock_item", "has_serial_no", "is_firearm", "standard_rate"], as_dict=True)
		self.assertEqual((item.item_group, item.is_stock_item, item.has_serial_no, item.is_firearm),
			("Slings & Mounts", 1, 0, 0))
		self.assertEqual(flt(item.standard_rate), 25.0)
		self.assertEqual(flt(frappe.db.get_value("Item Price", {"item_code": made["item_code"], "selling": 1},
			"price_list_rate")), 25.0, "sells at the register at the price given")

	def test_new_item_only_in_an_accessory_category(self):
		frappe.set_user(SALES_USER)
		try:
			for group in ("Pistol", "Accessory", "All Item Groups"):
				with self.subTest(group=group), self.assertRaisesRegex(frappe.ValidationError, "category"):
					trade_in.create_accessory_item(f"TIA bad {group}", group)
			with self.assertRaisesRegex(frappe.ValidationError, "already exists"):
				trade_in.create_accessory_item(AMMO, "Pistol Ammo")
		finally:
			frappe.set_user("Administrator")

	def test_new_item_needs_a_trade_in_role(self):
		_ensure_user("tia-accounts@example.com", "Accounts User")
		frappe.set_user("tia-accounts@example.com")
		with self.assertRaises(frappe.PermissionError):
			trade_in.create_accessory_item("TIA refused", "Magazine")

	def test_dialog_category_picker_mirrors_the_server(self):
		path = frappe.get_app_path("ffl_core", "public", "js", "ffl_core.bundle.js")
		js = open(path).read()
		[listed] = re.findall(r"const TRADE_IN_ACCESSORY_PARENTS = \[([^\]]*)\];", js)
		self.assertEqual(tuple(re.findall(r"'([^']+)'", listed)), trade_in.TRADE_IN_ACCESSORY_PARENTS)
