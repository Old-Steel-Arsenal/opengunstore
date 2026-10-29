"""Sports South provider — pure planning / normalisation, and the placement
flow on a site with a fake client (AddHeader → AddDetail → Submit → GetDetail,
refusal withdrawal, resume after a crash, credit-memo fold)."""
from __future__ import annotations

import types
import unittest
from datetime import date

from ffl_integrations.distributor import ss_provider as ssp
from ffl_integrations.distributor.base import PlanningError
from ffl_integrations.sports_south import tasks as ss_tasks


def _settings(**over):
	base = dict(ship_via_ground="G", ship_via_2day="2", firearm_signature=1,
		firearm_adult_signature=0, firearm_insurance=1, other_signature=0,
		other_adult_signature=0, other_insurance=0, price_drift_alert_pct=2.0,
		auto_email_ffl=0, sales_message="", customer_number="100001",
		store_ship_name="_Test Store A", store_ship_line1="1 Shop St",
		store_ship_city="Springfield", store_ship_state="TX", store_ship_zip="70001",
		store_ship_phone="9725551212")
	base.update(over)
	return types.SimpleNamespace(**base)


# Every Sports South Settings ship-to override field blank: "use the company address".
_NO_OVERRIDE = {f"store_ship_{k}": "" for k in
	("name", "attn", "line1", "line2", "city", "state", "zip", "phone")}
_FILLED = [{"sku": "25845", "ordered_qty": 2, "filled_qty": 2, "order_price": 19.5,
	"filled_price": 19.5}]


def _ln(sku, qty=1, *, firearm=False, department="1", cost=10.0, ship_method=None):
	d = {"sku": sku, "item_code": sku, "qty": qty, "ffl_required": firearm,
		"is_firearm": firearm, "department": department, "unit_cost": cost}
	if ship_method:
		d["ship_method"] = ship_method
	return d


class TestPlanning(unittest.TestCase):
	def test_firearms_and_accessories_never_share_an_order(self):
		plans = ssp.SSProvider().plan_fulfillment(
			[_ln("26001", firearm=True, department="26"), _ln("18001", department="18")],
			{"mode": "stocking"})
		self.assertEqual(len(plans), 2)
		self.assertEqual([p["lines"][0]["sku"] for p in plans], ["26001", "18001"])

	def test_handgun_goes_2day_everything_else_ground(self):
		self.assertEqual(ssp.ship_method_for({"department": "26"}), "2Day")
		self.assertEqual(ssp.ship_method_for({"department": "64"}), "2Day")
		self.assertEqual(ssp.ship_method_for({"department": "40"}), "Grnd")
		self.assertEqual(ssp.ship_method_for({"department": "5"}), "Grnd")
		self.assertEqual(ssp.ship_method_for({"department": "40", "ship_method": "2Day"}), "2Day")

	def test_classification_divergence_holds_the_order(self):
		bad = _ln("26001", firearm=True, department="26")
		bad["is_firearm"] = False
		with self.assertRaises(PlanningError):
			ssp.SSProvider().plan_fulfillment([bad], {"mode": "stocking"})

	def test_fds_plan_carries_only_the_transfer_dealer(self):
		ffl = {"license": "5-99-888-01-1A-00001", "premise_city": "Springfield"}
		plans = ssp.SSProvider().plan_fulfillment([_ln("26001", firearm=True, department="26")],
			{"mode": "fds", "destination_ffl": ffl, "ship_to": {"line1": "leak"},
				"consumer_name": "Jane", "contact_num": "555"})
		p = plans[0]
		self.assertEqual(p["order_type"], "Dropship Firearm")
		self.assertEqual(p["account"], "fulfillment")
		self.assertIsNone(p["ship_to"])
		self.assertEqual(p["ship_ffl"], ffl["license"])
		self.assertEqual(p["consumer_name"], "Jane")

	def test_line_class(self):
		self.assertEqual(ssp.line_class("70"), "ammo")
		self.assertEqual(ssp.line_class("48"), "long_gun")
		self.assertEqual(ssp.line_class("96"), "handgun")
		self.assertEqual(ssp.line_class("28"), "accessory")


class TestMoney(unittest.TestCase):
	def test_freight_estimate_by_lane_and_fees(self):
		s = _settings()
		lines = [{"ship_method": "2Day", "unit_cost": 500.0, "wish_qty": 1}]
		# handgun 10.95 + signature 2.00 + insurance max(2.90, 4.75)
		self.assertEqual(ssp.estimate_freight(lines, s, True, signature=True, insurance=True), 17.70)
		acc = [{"ship_method": "Grnd", "unit_cost": 20.0, "wish_qty": 2}]
		self.assertEqual(ssp.estimate_freight(acc, s, False), 10.95)
		self.assertEqual(ssp.estimate_freight(acc, s, False, insurance=True), 13.85)

	def test_ship_via_and_flags(self):
		s = _settings()
		self.assertEqual(ssp.ship_via_for([{"ship_method": "Grnd"}, {"ship_method": "2Day"}], s), "2")
		self.assertEqual(ssp.ship_via_for([{"ship_method": "Grnd"}], s), "G")
		# SS: Ground Economy = empty ShipVIA, only for non-firearm orders with the switch on
		econ = _settings(accessory_ground_economy=1)
		self.assertEqual(ssp.ship_via_for([{"ship_method": "Grnd"}], econ, False), "")
		self.assertEqual(ssp.ship_via_for([{"ship_method": "Grnd"}], econ, True), "G")
		self.assertEqual(ssp.estimate_freight([{"ship_method": "Grnd", "unit_cost": 1, "wish_qty": 1}], econ, False), 7.95)
		self.assertEqual(ssp.signature_flags(s, True),
			{"adult_signature": False, "signature": True, "insurance": True})
		self.assertEqual(ssp.signature_flags(s, False),
			{"adult_signature": False, "signature": False, "insurance": False})
		with self.assertRaises(Exception):
			ssp.ship_via_for([{"ship_method": "Grnd"}], _settings(ship_via_ground=""))

	def test_price_drift(self):
		lines = [{"sku": "1", "unit_cost": 100.0}, {"sku": "2", "unit_cost": 100.0}]
		detail = [{"sku": "1", "filled_qty": 1, "filled_price": 103.0},
			{"sku": "2", "filled_qty": 1, "filled_price": 101.0}]
		d = ssp.price_drift(detail, lines, 2.0)
		self.assertEqual([x["sku"] for x in d], ["1"])
		self.assertEqual(ssp.price_drift(detail, lines, 0), [])


class TestShippingChoice(unittest.TestCase):
	def test_accessory_order_offers_all_four_default_premium(self):
		opts = ssp.shipping_options_for([{"ship_method": "Grnd"}], _settings(), False)
		self.assertEqual([o["code"] for o in opts], ["", "G", "2", "N"])
		self.assertEqual([o["default"] for o in opts], [False, True, False, False])
		self.assertEqual(opts[0]["estimate"], 7.95)
		econ = ssp.shipping_options_for([{"ship_method": "Grnd"}], _settings(accessory_ground_economy=1), False)
		self.assertTrue(econ[0]["default"])

	def test_handgun_air_only_ammo_ground_only(self):
		hg = ssp.shipping_options_for([{"ship_method": "2Day"}], _settings(), True)
		self.assertEqual([o["code"] for o in hg], ["2", "N"])
		self.assertTrue(hg[0]["default"])
		ammo = ssp.shipping_options_for([{"ship_method": "Grnd"}], _settings(), False, has_ammo=True)
		self.assertEqual([o["code"] for o in ammo], ["", "G"])
		lg = ssp.shipping_options_for([{"ship_method": "Grnd"}], _settings(), True)
		self.assertEqual([o["code"] for o in lg], ["G", "2", "N"])

	def test_chosen_service_validated_against_lane(self):
		lines = [{"ship_method": "Grnd"}]
		do = types.SimpleNamespace(name="DO-1", shipping_service="", shipping_service_label="Ground Economy — $7.95")
		self.assertEqual(ssp.chosen_service(do, lines, _settings(), False), "")
		do = types.SimpleNamespace(name="DO-1", shipping_service=None, shipping_service_label="")
		self.assertEqual(ssp.chosen_service(do, lines, _settings(), False), "G")
		do = types.SimpleNamespace(name="DO-1", shipping_service="N", shipping_service_label="Next-Day")
		self.assertEqual(ssp.chosen_service(do, lines, _settings(), False), "N")
		do = types.SimpleNamespace(name="DO-1", shipping_service="G", shipping_service_label="Ground")
		with self.assertRaises(Exception):   # handgun cannot go ground
			ssp.chosen_service(do, [{"ship_method": "2Day"}], _settings(), True)

	def test_freight_estimate_follows_the_chosen_service(self):
		lines = [{"ship_method": "Grnd", "unit_cost": 20.0, "wish_qty": 1}]
		self.assertEqual(ssp.estimate_freight(lines, _settings(), False, service="N"), 42.95)
		self.assertEqual(ssp.estimate_freight(lines, _settings(), False, service=""), 7.95)


class TestNormalisation(unittest.TestCase):
	def test_placement_marks_short_and_cancelled(self):
		lines = [{"sku": "1", "wish_qty": 2, "unit_cost": 10.0},
			{"sku": "2", "wish_qty": 1, "unit_cost": 5.0},
			{"sku": "3", "wish_qty": 1, "unit_cost": 5.0}]
		detail = [{"sku": "1", "ordered_qty": 2, "filled_qty": 1, "order_price": 10.0, "filled_price": 11.0},
			{"sku": "2", "ordered_qty": 1, "filled_qty": 1, "order_price": 5.0, "filled_price": 0}]
		r = ssp.normalize_placement(1119886, detail, lines, freight=10.95)
		by = {a["sku"]: a for a in r["ack_lines"]}
		self.assertEqual((by["1"]["ack_qty"], by["1"]["unit_cost"], by["1"]["item_status"]), (1, 11.0, "SHORT"))
		self.assertEqual((by["2"]["ack_qty"], by["2"]["unit_cost"], by["2"]["item_status"]), (1, 5.0, ""))
		self.assertEqual((by["3"]["ack_qty"], by["3"]["item_status"]), (0, "UNFILLED"))
		self.assertEqual(r["web_ref"], "1119886")
		self.assertEqual(r["totals"], {"subtotal": 16.0, "total": 26.95})

	def test_status_before_and_after_invoice(self):
		detail = [{"sku": "1", "ordered_qty": 2, "filled_qty": 2, "filled_price": 10.0},
			{"sku": "2", "ordered_qty": 1, "filled_qty": 0, "filled_price": 0}]
		pre = ssp.normalize_order_status("1119886", detail, [], [], [])
		self.assertEqual(pre["lines"][0]["ack_qty"], 2)
		self.assertEqual(pre["lines"][0]["invoiced_qty"], 0)
		self.assertEqual(pre["lines"][1]["item_status"], "UNFILLED")   # not final before invoice
		post = ssp.normalize_order_status("1119886", detail,
			[{"S1INV": "700001"}],
			[{"TRACKNO": "1Z999", "SHPDTE": "2026-09-09", "SHPAMT": "10.95"},
			 {"TRACKNO": "1Z998", "SHPDTE": "2026-09-09", "SHPAMT": "10.95"}],
			[{"ITEMNO": "1", "SERIAL": "ABC123"}])
		l = post["lines"][0]
		self.assertEqual((l["invoiced_qty"], l["invoices"], l["tracking_numbers"], l["serials"]),
			(2, ["700001"], ["1Z999", "1Z998"], ["ABC123"]))
		self.assertEqual(post["lines"][1]["item_status"], "CANCELLED")
		self.assertEqual(post["freight"], 10.95)            # per order, not per parcel
		# the generic layer reads CANCELLED as killed → Complete once the rest ships
		from ffl_integrations.distributor.tasks import compute_do_updates
		res = compute_do_updates([{"sku": "1", "wish_qty": 2}, {"sku": "2", "wish_qty": 1}], post)
		self.assertEqual(res["status"], "Complete")
		self.assertEqual(res["lines"]["2"]["line_status"], "Killed")


class TestResumeAndBrands(unittest.TestCase):
	def test_header_state(self):
		self.assertEqual(ssp.header_state([{"ORDNO": "1", "STATUS": "W"}]), "open")
		self.assertEqual(ssp.header_state([{"ORDNO": "1", "STATUS": "S"}]), "submitted")
		self.assertEqual(ssp.header_state([{"REPLY": "ORDER DOES NOT EXIST!"}]), "missing")
		self.assertEqual(ssp.header_state([]), "missing")

	def test_restriction_lists_cover_the_overview(self):
		self.assertIn("Ruger", ssp.BLOCKED_BRANDS)
		self.assertIn("Sig Sauer", ssp.PREAPPROVAL_BRANDS)


class TestCredits(unittest.TestCase):
	def test_apply_credit_folds_shipped_qty(self):
		rows = [{"sku": "1", "invoiced_qty": 2, "item_status": "", "line_status": "Shipped"},
			{"sku": "2", "invoiced_qty": 1, "item_status": "", "line_status": "Shipped"}]
		ch = ss_tasks.apply_credit(rows, {"lines": [{"sku": "1", "qty": -1}, {"sku": "2", "qty": -1},
			{"sku": "9", "qty": -1}]})
		self.assertEqual((rows[0]["invoiced_qty"], rows[0]["item_status"], rows[0]["line_status"]),
			(1, "SHORT", "Shipped"))
		self.assertEqual((rows[1]["invoiced_qty"], rows[1]["item_status"], rows[1]["line_status"]),
			(0, "CANCELLED", "Killed"))
		self.assertEqual(ch[2]["item_status"], "unknown line")

	def test_dates_to_poll(self):
		today = date(2026, 9, 8)
		self.assertEqual(ss_tasks.dates_to_poll(None, today), ["20260907"])
		self.assertEqual(ss_tasks.dates_to_poll(date(2026, 9, 5), today), ["20260906", "20260907"])
		self.assertEqual(ss_tasks.dates_to_poll(date(2026, 9, 7), today), [])
		self.assertEqual(len(ss_tasks.dates_to_poll(date(2026, 1, 1), today)), ss_tasks.MAX_CATCHUP_DAYS)


# ---------------------------------------------------------------- site tests

class TestDeliveryOptions(unittest.TestCase):
	"""Signature / adult signature / insurance chosen in Create Order ride on the
	Distributor Order; without them Settings decide, as before."""

	def test_order_without_counter_options_follows_settings(self):
		do = types.SimpleNamespace(delivery_options_set=0, delivery_signature=0,
			delivery_adult_signature=1, delivery_insurance=0)
		self.assertEqual(ssp.order_flags(do, _settings(), True),
			{"adult_signature": False, "signature": True, "insurance": True})

	def test_order_with_counter_options_goes_out_with_them(self):
		do = types.SimpleNamespace(delivery_options_set=1, delivery_signature=0,
			delivery_adult_signature=1, delivery_insurance=0)
		self.assertEqual(ssp.order_flags(do, _settings(), True),
			{"adult_signature": True, "signature": False, "insurance": False})

	def test_fees_follow_the_chosen_options(self):
		s = _settings(fee_signature=2.0, fee_adult_signature=2.75, fee_insurance_pct=1.0,
			fee_insurance_min=2.9)
		lines = [{"unit_cost": 500.0, "wish_qty": 1}]
		self.assertEqual(ssp.delivery_fees(lines, s), 0.0)
		self.assertAlmostEqual(ssp.delivery_fees(lines, s, signature=True, insurance=True), 7.0)
		self.assertAlmostEqual(ssp.delivery_fees(lines, s, adult_signature=True), 2.75)

	def test_store_order_estimate_adds_the_chosen_fees(self):
		from unittest import mock
		s = _settings(fee_signature=2.0, fee_insurance_pct=1.0, fee_insurance_min=2.9)
		lines = [_ln("MAG", qty=1, cost=600.0, department="10")]
		with mock.patch("frappe.get_cached_doc", lambda *a, **k: s):
			base = ssp.SSProvider().estimate_order_freight(lines, "to_store",
				options={"signature": False})
			picked = ssp.SSProvider().estimate_order_freight(lines, "to_store",
				options={"signature": True, "insurance": True})
		self.assertEqual(base, 0.0)          # $600 of accessories ships free to the store
		self.assertAlmostEqual(picked, 8.0)  # + signature $2 + insurance 1% of $600

	def test_clean_options_keeps_three_booleans(self):
		self.assertEqual(ssp.clean_delivery_options({"insurance": 1, "gift_wrap": True}),
			{"adult_signature": False, "signature": False, "insurance": True})
		self.assertEqual(ssp.clean_delivery_options(None),
			{"adult_signature": False, "signature": False, "insurance": False})


class FakeClient:
	"""Scripted SS client: AddHeader → header number; AddDetail per SKU;
	Submit; DeleteOpenOrder; GetDetail rows."""

	def __init__(self, *, header=1119886, refuse=(), submit=True, delete=True, detail=None,
			ordering_enabled=True, header_status=None):
		self.config = types.SimpleNamespace(ordering_enabled=ordering_enabled)
		self.header, self.refuse, self.submit_ok, self.delete_ok = header, set(refuse), submit, delete
		self.detail_rows = detail or []
		# GetHeader answer for a PRIOR web order: "W" open, "S" submitted, None missing
		self.header_status = header_status
		self.calls = []

	def get_header(self, order_number, customer_order_number=""):
		self.calls.append(("GetHeader", order_number))
		if self.header_status is None:
			return [{"REPLY": "ORDER DOES NOT EXIST!", "_tag": "Table"}]
		return [{"ORDNO": str(order_number), "STATUS": self.header_status, "_tag": "Table"}]

	def add_header(self, **kw):
		self.calls.append(("AddHeader", kw))
		return self.header

	def add_detail(self, order_number, *, sku, qty, order_price, customer_item="", customer_desc=""):
		self.calls.append(("AddDetail", sku))
		return sku not in self.refuse

	def submit(self, order_number):
		self.calls.append(("Submit", order_number))
		return self.submit_ok

	def delete_open_order(self, order_number):
		self.calls.append(("DeleteOpenOrder", order_number))
		return self.delete_ok

	def get_detail(self, order_number, customer_order_number=""):
		self.calls.append(("GetDetail", order_number))
		return list(self.detail_rows)


try:
	import frappe
	from frappe.tests.utils import FrappeTestCase
except Exception:  # pragma: no cover — pure runs without bench
	FrappeTestCase = None


if FrappeTestCase:
	class TestSSOnSite(FrappeTestCase):
		@classmethod
		def setUpClass(cls):
			super().setUpClass()
			from ffl_integrations.sports_south import seed
			seed.ensure_distributor()
			cls.dist = frappe.db.get_value("Distributor", {"provider": "ss"}, "name")
			if not frappe.db.exists("Item", "SS-TEST-ACC"):
				group = "Products" if frappe.db.exists("Item Group", "Products") else "All Item Groups"
				frappe.get_doc({"doctype": "Item", "item_code": "SS-TEST-ACC",
					"item_name": "SS test accessory", "item_group": group, "stock_uom": "Nos",
					"is_stock_item": 1}).insert(ignore_permissions=True)

		def _do(self, order_type="Dropship Accessory", lines=None):
			return frappe.get_doc({
				"doctype": "Distributor Order", "distributor": self.dist,
				"order_type": order_type, "account": "fulfillment",
				"consumer_name": "Jane Buyer", "contact_num": "9725551212",
				"ship_address_line1": "1 Main St", "ship_address_city": "Springfield",
				"ship_address_state": "TX", "ship_address_zip": "70001",
				"lines": lines or [{"sku": "25845", "item_code": "SS-TEST-ACC", "wish_qty": 2,
					"unit_cost": 19.5, "ship_method": "Grnd"}],
			}).insert(ignore_permissions=True)

		def test_counter_picked_options_override_settings_on_placement(self):
			do = self._do()
			do.db_set({"delivery_options_set": 1, "delivery_signature": 0,
				"delivery_adult_signature": 1, "delivery_insurance": 1})
			fake = FakeClient(detail=_FILLED)
			ssp.SSProvider(client=fake)._place(do, fake, _settings(other_signature=1,
				other_insurance=0))
			header = fake.calls[0][1]
			self.assertEqual((header["signature"], header["adult_signature"], header["insurance"]),
				(False, True, True))

		def test_registry_has_ss_and_seed_is_idempotent(self):
			from ffl_integrations.distributor import api as dist_api
			from ffl_integrations.sports_south import seed
			self.assertEqual(dist_api.get_provider("ss").code, "ss")
			self.assertFalse(seed.ensure_distributor())
			self.assertEqual(frappe.db.get_value("Distributor", self.dist, "enabled"), 0)

		def test_brand_restrictions_are_scoped_to_ss_only(self):
			from ffl_integrations.distributor import policy
			from ffl_integrations.sports_south import seed
			for b in ("Ruger", "Sig Sauer"):
				if not frappe.db.exists("Brand", b):
					frappe.get_doc({"doctype": "Brand", "brand": b}).insert(ignore_permissions=True)
			seed.ensure_brand_policies()
			seed.ensure_brand_policies()          # idempotent
			self.assertEqual(frappe.db.count("Dropship Brand Policy",
				{"brand": "Ruger", "distributor": self.dist}), 1)
			# scoped to SS: RSR (no row of its own) still resolves Direct
			self.assertEqual(policy.brand_policy_for("Ruger", self.dist), "To Store")
			self.assertIsNone(policy.brand_policy_for("Ruger", "RSR"))
			self.assertIsNone(policy.brand_policy_for("Ruger"))
			reason = policy.brand_policy_reason("Ruger", self.dist, distributor_name="Sports South")
			self.assertIn("does not fulfil", reason)
			self.assertIn("ship to store only", reason)
			# a global row for another brand still applies to SS
			if not frappe.db.exists("Brand", "Holosun"):
				frappe.get_doc({"doctype": "Brand", "brand": "Holosun"}).insert(ignore_permissions=True)
			policy.ensure_seed_policies()
			self.assertEqual(policy.brand_policy_for("Holosun", self.dist), "To Store")
			# a scoped Direct OK beats the global To Store for that house only
			frappe.get_doc({"doctype": "Dropship Brand Policy", "brand": "Holosun",
				"distributor": self.dist, "policy": "Direct OK"}).insert(ignore_permissions=True)
			self.assertEqual(policy.brand_policy_for("Holosun", self.dist), "Direct OK")
			self.assertEqual(policy.brand_policy_for("Holosun", "RSR"), "To Store")

		def test_place_happy_path_records_web_ref_and_acks(self):
			do = self._do()
			fake = FakeClient(detail=[{"sku": "25845", "ordered_qty": 2, "filled_qty": 2,
				"order_price": 19.5, "filled_price": 19.5}])
			res = ssp.SSProvider(client=fake)._place(do, fake, _settings())
			self.assertEqual([c[0] for c in fake.calls], ["AddHeader", "AddDetail", "Submit", "GetDetail"])
			hdr = fake.calls[0][1]
			self.assertEqual(hdr["po"], do.name)
			self.assertEqual(hdr["ship_via"], "G")
			self.assertEqual(hdr["ship_to"]["name"], "Jane Buyer")
			self.assertEqual(hdr["ship_to"]["zip"], "70001")
			self.assertFalse(hdr["signature"])       # accessory order: no fees
			self.assertEqual(res["web_ref"], "1119886")
			self.assertEqual(res["ack_lines"][0]["ack_qty"], 2)
			self.assertEqual(res["freight"], 10.95)
			self.assertEqual(frappe.db.get_value("Distributor Order", do.name, "web_ref"), "1119886")
			self.assertEqual(do.web_ref, "1119886")   # survives a later do.save() on error

		def test_store_order_without_a_store_address_is_refused_before_ss(self):
			from ffl_integrations.sports_south.api_client import SSConfigError
			do = self._do(order_type="Procure To Store")
			fake = FakeClient()
			with self.assertRaises(SSConfigError):
				ssp.SSProvider(client=fake)._place(do, fake, _settings(store_ship_line1=""))
			self.assertEqual(fake.calls, [], "nothing reaches Sports South")

		# ---- store ship-to: the Settings override, else the company's own address

		def _store_do(self, company):
			do = self._do(order_type="Procure To Store")
			do.company = company
			return do

		def _ship_company(self, *, with_address, state="TX"):
			"""A Company of its own, so the address it prints is not shared site state."""
			from ffl_core.store_identity import company_address

			name = frappe.db.get_value("Company", {"company_name": "_Test SS Ship Store"}, "name") \
				or frappe.get_doc({"doctype": "Company", "company_name": "_Test SS Ship Store",
					"abbr": "TSSS", "default_currency": "USD", "country": "United States",
				}).insert(ignore_permissions=True).name
			self.assertIsNone(company_address(name), "a leftover address on the test company")
			if with_address:
				addr = frappe.get_doc({"doctype": "Address", "address_title": "_Test SS Ship Store",
					"address_type": "Office", "address_line1": "5 Warehouse Way",
					"address_line2": "Dock 2", "city": "Springfield", "state": state, "pincode": "70001",
					"phone": "9725550000", "country": "United States",
					"links": [{"link_doctype": "Company", "link_name": name}],
				}).insert(ignore_permissions=True).name
				self.addCleanup(frappe.delete_doc, "Address", addr, force=True,
					ignore_permissions=True)
			return name

		def test_blank_override_ships_store_orders_to_the_company_address(self):
			do = self._store_do(self._ship_company(with_address=True))
			fake = FakeClient(detail=_FILLED)
			ssp.SSProvider(client=fake)._place(do, fake, _settings(**_NO_OVERRIDE))
			self.assertEqual(fake.calls[0][1]["ship_to"], {
				"name": "_Test SS Ship Store", "attn": "", "line1": "5 Warehouse Way",
				"line2": "Dock 2", "city": "Springfield", "state": "TX", "zip": "70001",
				"phone": "9725550000"})

		def test_any_override_field_ships_the_whole_override_never_a_mix(self):
			from ffl_integrations.sports_south.api_client import SSConfigError
			company = self._ship_company(with_address=True)
			do = self._store_do(company)
			fake = FakeClient(detail=_FILLED)
			ssp.SSProvider(client=fake)._place(do, fake, _settings(store_ship_attn="Receiving"))
			ship_to = fake.calls[0][1]["ship_to"]
			self.assertEqual((ship_to["line1"], ship_to["attn"]), ("1 Shop St", "Receiving"))
			self.assertEqual(ship_to["line2"], "", "not topped up with the company's Dock 2")
			# One filled field alone is still the override: incomplete, so refused —
			# and the message points at the override, not at the company.
			fake2 = FakeClient()
			with self.assertRaises(SSConfigError) as ctx:
				ssp.SSProvider(client=fake2)._place(self._store_do(company), fake2,
					_settings(**{**_NO_OVERRIDE, "store_ship_attn": "Receiving"}))
			self.assertTrue(str(ctx.exception).startswith(
				"Sports South Settings: the ship-to address override"), str(ctx.exception))
			self.assertEqual(fake2.calls, [])

		def test_no_override_and_no_company_address_is_refused_pointing_at_the_company(self):
			from ffl_integrations.sports_south.api_client import SSConfigError
			do = self._store_do(self._ship_company(with_address=False))
			fake = FakeClient()
			with self.assertRaises(SSConfigError) as ctx:
				ssp.SSProvider(client=fake)._place(do, fake, _settings(**_NO_OVERRIDE))
			self.assertIn("company address", str(ctx.exception))
			self.assertIn("_Test SS Ship Store", str(ctx.exception))
			self.assertEqual(fake.calls, [], "nothing reaches Sports South")

		def test_company_address_state_is_sent_as_its_two_letter_code(self):
			"""Address.state is free text ("Texas" and "TX" both live in production); the
			header keeps two characters, so a name would reach Sports South as "Te"."""
			do = self._store_do(self._ship_company(with_address=True, state=" texas "))
			fake = FakeClient(detail=_FILLED)
			ssp.SSProvider(client=fake)._place(do, fake, _settings(**_NO_OVERRIDE))
			self.assertEqual(fake.calls[0][1]["ship_to"]["state"], "TX")

		def test_company_address_with_an_unknown_state_is_refused_as_incomplete(self):
			from ffl_integrations.sports_south.api_client import SSConfigError
			do = self._store_do(self._ship_company(with_address=True, state="Tex"))
			fake = FakeClient()
			with self.assertRaises(SSConfigError) as ctx:
				ssp.SSProvider(client=fake)._place(do, fake, _settings(**_NO_OVERRIDE))
			self.assertIn("company address", str(ctx.exception))
			self.assertEqual(fake.calls, [], "nothing reaches Sports South")

		def test_refused_line_withdraws_the_whole_order(self):
			from ffl_integrations.sports_south.api_client import SSError
			do = self._do(lines=[
				{"sku": "25845", "item_code": "SS-TEST-ACC", "wish_qty": 1, "unit_cost": 1, "ship_method": "Grnd"},
				{"sku": "99999", "item_code": "SS-TEST-ACC", "wish_qty": 1, "unit_cost": 1, "ship_method": "Grnd"}])
			fake = FakeClient(refuse={"99999"})
			with self.assertRaises(SSError) as ctx:
				ssp.SSProvider(client=fake)._place(do, fake, _settings())
			self.assertIn("99999", str(ctx.exception))
			self.assertIn(("DeleteOpenOrder", 1119886), fake.calls)
			self.assertNotIn("Submit", [c[0] for c in fake.calls])
			self.assertEqual(frappe.db.get_value("Distributor Order", do.name, "web_ref"), "")

		def test_submit_false_withdraws(self):
			from ffl_integrations.sports_south.api_client import SSError
			do = self._do()
			fake = FakeClient(submit=False)
			with self.assertRaises(SSError):
				ssp.SSProvider(client=fake)._place(do, fake, _settings())
			self.assertEqual(fake.calls[-1], ("DeleteOpenOrder", 1119886))

		def test_resume_after_crash_never_double_places(self):
			do = self._do()
			frappe.db.set_value("Distributor Order", do.name, "web_ref", "555", update_modified=False)
			do.reload()
			# header already submitted at SS (STATUS != W) → just read it back
			fake = FakeClient(header_status="S", detail=[{"sku": "25845", "ordered_qty": 2,
				"filled_qty": 2, "order_price": 19.5, "filled_price": 19.5}])
			res = ssp.SSProvider(client=fake)._place(do, fake, _settings())
			self.assertEqual([c[0] for c in fake.calls], ["GetHeader", "GetDetail"])
			self.assertTrue(res["resumed"])
			self.assertEqual(res["web_ref"], "555")
			# header still open (W) → deleted, then placed fresh
			do2 = self._do()
			frappe.db.set_value("Distributor Order", do2.name, "web_ref", "556", update_modified=False)
			do2.reload()
			fake2 = FakeClient(header_status="W", header=777)
			res2 = ssp.SSProvider(client=fake2)._place(do2, fake2, _settings())
			self.assertEqual([c[0] for c in fake2.calls][:3], ["GetHeader", "DeleteOpenOrder", "AddHeader"])
			self.assertEqual(res2["web_ref"], "777")
			self.assertEqual(do2.web_ref, "777")             # in-memory doc too
			# header gone at SS (deleted there) → never "resumed" into a phantom
			do3 = self._do()
			frappe.db.set_value("Distributor Order", do3.name, "web_ref", "557", update_modified=False)
			do3.reload()
			fake3 = FakeClient(header_status=None, header=778)
			res3 = ssp.SSProvider(client=fake3)._place(do3, fake3, _settings())
			self.assertEqual([c[0] for c in fake3.calls][:2], ["GetHeader", "AddHeader"])
			self.assertFalse(res3["resumed"])

		def test_unnamed_search_is_one_row_per_upc_with_sources(self):
			"""UPC 810047075431 was SS-only and invisible (search resolved ONE
			house); then it showed TWICE (one row per house). Unnamed search is
			now one product row per UPC, distributor '' ("API"), with the enabled
			houses listed as sources — sourcing is decided in the planner."""
			from ffl_integrations.distributor import api as dist_api
			from ffl_integrations.tests import catalog_stub
			rsr_was = frappe.db.get_value("Distributor", "RSR", "enabled")
			frappe.db.set_value("Distributor", "RSR", "enabled", 1, update_modified=False)
			frappe.db.set_value("Distributor", self.dist, "enabled", 1, update_modified=False)
			stub = catalog_stub.install()
			try:
				catalog_stub.seed_catalog([{"rsr_stock_number": "HSARO", "upc": "810047073659",
					"dealer_price": 171.54, "quantity": 213, "manufacturer": "Holosun",
					"model": "ARO", "short_description": "H-SUN ARO-EVO"}], distributor="rsr")
				catalog_stub.seed_catalog([{"rsr_stock_number": "175939", "upc": "810047073659",
					"dealer_price": 171.54, "quantity": 94, "manufacturer": "Holosun",
					"model": "AROEVOGR2", "short_description": "Holosun AROEVOGR2"}],
					distributor="ss")
				out = dist_api.search("810047073659", limit=10)
				self.assertEqual(len(out), 1)
				row = out[0]
				self.assertEqual(row["distributor"], "")
				self.assertEqual(row["upc"], "810047073659")
				self.assertEqual(sorted(row["sources"]), sorted(["RSR", self.dist]))
				self.assertEqual(row["qty"], 307)
				self.assertEqual({o["distributor"]: o["sku"] for o in row["offers"]},
					{"RSR": "HSARO", self.dist: "175939"})
				# a named house stays scoped and tagged
				only = dist_api.search("810047073659", limit=10, distributor=self.dist)
				self.assertEqual([r["distributor"] for r in only], [self.dist])
				# a disabled house's offer drops out of the sources
				frappe.db.set_value("Distributor", self.dist, "enabled", 0, update_modified=False)
				out = dist_api.search("810047073659", limit=10)
				self.assertEqual(out[0]["sources"], ["RSR"])
				# planner: a UPC-only line resolves per column
				frappe.db.set_value("Distributor", self.dist, "enabled", 1, update_modified=False)
				m = dist_api.fulfillment_matrix([{"upc": "810047073659", "qty": 1}])
				cells = {c["distributor"]: c for c in m["rows"][0]["channels"]}
				self.assertEqual(cells[self.dist]["sku"], "175939")
				self.assertTrue(cells[self.dist]["feasible"])
			finally:
				catalog_stub.uninstall()
				frappe.db.set_value("Distributor", "RSR", "enabled", rsr_was, update_modified=False)
				frappe.db.set_value("Distributor", self.dist, "enabled", 0, update_modified=False)

		def test_store_order_is_priced_by_store_rules_and_the_label_follows_the_lines(self):
			do = self._do("Procure To Store")
			self.assertIn("$5.00", do.shipping_service_label)     # 2 x $19.50, under $500
			fake = FakeClient(detail=_FILLED)
			res = ssp.SSProvider(client=fake)._place(do, fake, _settings())
			self.assertEqual(res["freight"], 5.0)
			do = self._do("Procure To Store")
			do.lines[0].wish_qty = 30                                # $585: ships free
			do.save(ignore_permissions=True)
			self.assertIn("$0.00", do.shipping_service_label)

		def test_shipping_service_chosen_on_the_draft_is_sent(self):
			from ffl_integrations.distributor import api as dist_api
			do = self._do()
			opts = dist_api.shipping_options(do.name)
			self.assertEqual([o["code"] for o in opts], ["", "G", "2", "N"])
			# the draft carries the provider default already
			self.assertEqual(frappe.db.get_value("Distributor Order", do.name, "shipping_service"), "G")
			with self.assertRaises(Exception):
				dist_api.set_shipping_service(do.name, "Z")
			res = dist_api.set_shipping_service(do.name, "N")
			self.assertIn("Next-Day", res["shipping_service_label"])
			do.reload()
			fake = FakeClient(detail=[{"sku": "25845", "ordered_qty": 2, "filled_qty": 2,
				"order_price": 19.5, "filled_price": 19.5}])
			out = ssp.SSProvider(client=fake)._place(do, fake, _settings())
			self.assertEqual(fake.calls[0][1]["ship_via"], "N")
			self.assertEqual(out["freight"], 42.95)
			# Economy = the empty code, distinguishable from "unset"
			do2 = self._do()
			dist_api.set_shipping_service(do2.name, "")
			do2.reload()
			fake2 = FakeClient()
			ssp.SSProvider(client=fake2)._place(do2, fake2, _settings())
			self.assertEqual(fake2.calls[0][1]["ship_via"], "")
			# not on a Queued order
			frappe.db.set_value("Distributor Order", do2.name, "status", "Queued", update_modified=False)
			with self.assertRaises(Exception):
				dist_api.set_shipping_service(do2.name, "G")

		def test_ordering_disabled_fails_closed(self):
			from ffl_integrations.sports_south.api_client import SSConfigError
			do = self._do()
			fake = FakeClient(ordering_enabled=False)
			with self.assertRaises(SSConfigError):
				ssp.SSProvider(client=fake)._place(do, fake, _settings())
			self.assertEqual(fake.calls, [])

		def test_fds_ship_to_is_the_transfer_dealer(self):
			do = self._do(order_type="Dropship Firearm")
			do.transfer_ffl_license = "5-99-888-01-1A-00001"
			do.transfer_ffl_premise_street = "9 Dealer Rd"
			do.transfer_ffl_premise_city = "Frisco"
			do.transfer_ffl_premise_state = "TX"
			do.transfer_ffl_premise_zip = "75034"
			st = ssp.build_ship_to(do, "fds", _settings())
			self.assertEqual(st["line1"], "9 Dealer Rd")
			self.assertEqual(st["attn"], "Jane Buyer")
			self.assertNotIn("1 Main St", st.values())
			self.assertEqual(ssp.signature_flags(_settings(), True)["insurance"], True)

		def test_credit_memo_folds_onto_a_complete_order(self):
			do = self._do()
			for l in do.lines:
				l.invoiced_qty, l.line_status = 2, "Shipped"
			do.save(ignore_permissions=True)
			credit = {"credit_no": "C1", "reason": "CANCELLED", "invoice_no": "700001",
				"total": -19.5, "lines": [{"sku": "25845", "qty": -1}]}
			self.assertTrue(ss_tasks._apply_to_do(do.name, credit))
			self.assertFalse(ss_tasks._apply_to_do(do.name, credit))   # replay is a no-op
			l = frappe.get_doc("Distributor Order", do.name).lines[0]
			self.assertEqual((l.invoiced_qty, l.item_status), (1, "SHORT"))
