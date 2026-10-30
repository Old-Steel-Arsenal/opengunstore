"""Trade-in credit — the negative POS line that books the money side.

A trade-in intake records the gun (Item + FFL Acquisition + stock receipt) but
historically left the MONEY unrecorded: the credit/cash owed to the seller
never hit the POS invoice, so revenue was overstated and the cash drawer
drifted.

These tests cover the three pieces:
  - setup/trade_in_credit.ensure(): the TRADE-IN-CREDIT item + invoice-row
	custom fields (idempotent, lazy — mirrors setup/transfer_fee).
  - api/trade_in.create_trade_in_intake(apply_credit=…): appends a locked
	negative line to the current (or a fresh) draft POS Invoice.
  - compliance/trade_in_credit.validate_trade_in_credit_rows: submit-time
	gate — a credit row must trace to a submitted Trade-in acquisition, match
	its cost exactly, and never be spent twice.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_core.api import trade_in


def _ns_doc(*credit_rows: dict, docstatus: int = 1, is_return: int = 0,
		grand_total: float = 100.0, name: str = "NS-INV-1",
		doctype: str = "POS Invoice") -> SimpleNamespace:
	"""Doc-like for the validate hook: only credit rows matter to the gate."""
	from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
	items = []
	for i, row in enumerate(credit_rows):
		base = {"idx": i + 1, "item_code": TRADE_IN_CREDIT_ITEM, "qty": 1,
			"rate": -300.0, "ffl_trade_in_acquisition": None}
		base.update(row)
		base.setdefault("amount", base["rate"] * base["qty"])
		items.append(SimpleNamespace(**base))
	return SimpleNamespace(
		doctype=doctype, name=name, docstatus=docstatus,
		is_return=is_return, grand_total=grand_total, items=items,
	)


class TradeInCreditItem(FrappeTestCase):
	def test_ensure_creates_taxable_nonstock_item(self):
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM, ensure
		ensure()
		item = frappe.get_doc("Item", TRADE_IN_CREDIT_ITEM)
		self.assertEqual(item.is_stock_item, 0)
		self.assertEqual(item.has_serial_no, 0)
		self.assertEqual(int(item.is_firearm or 0), 0)
		# Stays in its taxable group (not Service) for report categorization AND
		# so the negative line participates in the sales-tax base — a trade-in
		# reduces the taxable amount (TX §151.007(c)(3)). The tax behaviour is pinned in test_service_tax_exemption.
		self.assertNotEqual(item.item_group, "Service")

	def test_ensure_is_idempotent(self):
		from ffl_core.setup.trade_in_credit import ensure
		ensure()
		self.assertEqual(ensure()["created"], 0)

	def test_ensure_creates_invoice_row_field(self):
		from ffl_core.setup.trade_in_credit import ensure
		ensure()
		for dt in ("POS Invoice Item", "Sales Invoice Item"):
			self.assertTrue(
				frappe.db.exists("Custom Field", f"{dt}-ffl_trade_in_acquisition"),
				f"missing ffl_trade_in_acquisition on {dt}",
			)


class TradeInCreditFlow(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		from ffl_core.setup.trade_in_credit import ensure
		ensure()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.pos_profile = "FFL Test"
		cls._ensure_open_pos_entry()
		cls._ensure_customer("_Test Trade-in Seller")

	def setUp(self):
		super().setUp()
		# A pay-now buy pushes its acquisition to the bound book on receive; these tests are not about FastBound.
		from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError

		p = patch.object(FastBoundClient, "from_settings", side_effect=FastBoundConfigError("off in trade-in credit tests"))
		p.start()
		self.addCleanup(p.stop)

	@staticmethod
	def _ensure_customer(name: str) -> None:
		if frappe.db.exists("Customer", name):
			return
		frappe.get_doc({
			"doctype": "Customer", "customer_name": name,
			"customer_group": "Individual", "territory": "All Territories",
		}).insert(ignore_permissions=True)

	@classmethod
	def _ensure_open_pos_entry(cls) -> None:
		from ffl_core.tests.pos_opening import todays_pos_opening
		todays_pos_opening(cls.company, cls.pos_profile)

	def _payload(self, suffix: str, **over) -> dict:
		base = {
			"serial_no": f"TIC-{suffix}",
			"manufacturer": "Glock",
			"model_name": "19",
			"caliber": "9mm",
			"firearm_type": "Pistol",
			"condition_grade": "Good",
			"id_type": "Driver License",
			"id_number": f"DL-{suffix}",
			"source_name": "_Test Trade-in Seller",
			"source_first_name": "Testy",
			"source_last_name": "Seller",
			"source_address": "123 Main St, Springfield TX 75024",
			"acquisition_cost": 300,
			"payout_method": "Cash",  # a pure buy says how the seller was paid (ignored with apply_credit)
		}
		base.update(over)
		return base

	def _build_credit_invoice(self, acquisition: str) -> "frappe.Document":
		"""A draft POS Invoice carrying the credit row plus a positive accessory
		line (the credit alone would trip POS Invoice's non-negative grand-total
		docfield — the same reason the POS dialog injects the row client-side
		into the live cart instead of parking a credit-only draft)."""
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
		if not frappe.db.exists("Item", "TIC-ACC"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "TIC-ACC", "item_name": "TIC-ACC",
				"item_group": "Accessory", "stock_uom": "Nos",
				"is_stock_item": 0, "has_serial_no": 0,
			}).insert(ignore_permissions=True)
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = self.pos_profile
		inv.customer = "_Test Trade-in Seller"
		inv.append("items", {"item_code": "TIC-ACC", "qty": 1, "rate": 500,
			"price_list_rate": 500})
		inv.append("items", {"item_code": TRADE_IN_CREDIT_ITEM, "qty": 1,
			"rate": -300, "price_list_rate": -300,
			"ffl_trade_in_acquisition": acquisition})
		inv.set_missing_values()
		for row in inv.items:
			if row.item_code == "TIC-ACC":
				row.rate = row.price_list_rate = 500
			elif row.item_code == TRADE_IN_CREDIT_ITEM:
				row.rate = row.price_list_rate = -300
		inv.flags.ignore_permissions = True
		inv.save()
		return inv

	def test_apply_credit_returns_locked_credit_payload(self):
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
		suffix = self._testMethodName
		res = trade_in.create_trade_in_intake(self._payload(suffix, apply_credit=1))
		self.assertTrue(res["ok"])
		credit = res.get("credit")
		self.assertTrue(credit)
		self.assertEqual(credit["item_code"], TRADE_IN_CREDIT_ITEM)
		self.assertEqual(float(credit["rate"]), -300.0)
		self.assertEqual(credit["acquisition"], res["acquisition"])

	def test_apply_credit_defers_booking_until_checkout(self):
		"""A past incident: the traded-in gun entered the bound
		book (FFL Acquisition submitted + Material Receipt + FastBound push) at
		DIALOG time, while the checkout later sat unfinished. With apply_credit
		the intake must only park a DRAFT acquisition — no Serial No, no stock,
		no bound-book entry — until the POS Invoice actually submits."""
		suffix = self._testMethodName
		res = trade_in.create_trade_in_intake(self._payload(suffix, apply_credit=1))
		acq = frappe.get_doc("FFL Acquisition", res["acquisition"])
		self.assertEqual(acq.docstatus, 0, "acquisition must stay Draft until checkout")
		self.assertTrue(acq.pending_intake_payload, "per-unit stamp data must be parked")
		self.assertFalse(frappe.db.exists("Serial No", f"TIC-{suffix}"),
			"no Serial No before checkout completes")
		self.assertFalse(frappe.db.exists("Stock Entry Detail",
			{"serial_no": f"TIC-{suffix}", "docstatus": 1}),
			"no stock receipt before checkout completes")

	def test_cash_buy_still_books_immediately(self):
		"""Without apply_credit (pure cash buy) the store takes the gun and pays
		on the spot — immediate booking remains correct."""
		suffix = self._testMethodName
		res = trade_in.create_trade_in_intake(self._payload(suffix))
		acq = frappe.get_doc("FFL Acquisition", res["acquisition"])
		self.assertEqual(acq.docstatus, 1)
		self.assertTrue(frappe.db.exists("Serial No", f"TIC-{suffix}"))

	def test_reopened_dialog_reuses_pending_draft(self):
		"""Cashier abandons the cart, later re-runs the Trade-in dialog for the
		same serial: the pending draft is refreshed (new cost honoured), not
		duplicated and not blocked by the duplicate-serial guard."""
		suffix = self._testMethodName
		first = trade_in.create_trade_in_intake(self._payload(suffix, apply_credit=1))
		second = trade_in.create_trade_in_intake(
			self._payload(suffix, apply_credit=1, acquisition_cost=350))
		self.assertEqual(first["acquisition"], second["acquisition"])
		self.assertEqual(float(second["credit"]["rate"]), -350.0)
		self.assertEqual(
			float(frappe.db.get_value("FFL Acquisition", first["acquisition"], "cost")),
			350.0,
		)
		self.assertEqual(frappe.db.count("FFL Acquisition",
			{"serial": f"TIC-{suffix}"}), 1)

	def test_apply_credit_intake_does_not_commit(self):
		"""Isolation regression. create_trade_in_intake(apply_credit=1)
		mints a Serial No + FFL Acquisition, THEN calls trade_in_credit.ensure(),
		whose frappe.db.commit() flushes the just-minted per-test serial out of
		the class rollback (IntegrationTestCase rolls back only to the last
		commit, at tearDownClass). The next run then collides on TIC-<testname>
		('already on file'). The intake path must never commit the transaction —
		the one-time bootstrap does that, out of band (frappe.flags.in_test off)."""
		suffix = self._testMethodName
		with patch.object(frappe.db, "commit") as spy:
			res = trade_in.create_trade_in_intake(self._payload(suffix, apply_credit=1))
		self.assertTrue(res["ok"])
		self.assertFalse(
			spy.called,
			"intake committed mid-transaction — escapes rollback and poisons the "
			"next run on the same site",
		)

	def test_without_apply_credit_no_credit_payload(self):
		suffix = self._testMethodName
		res = trade_in.create_trade_in_intake(self._payload(suffix))
		self.assertTrue(res["ok"])
		self.assertFalse(res.get("credit"))

	def test_credit_invoice_submits_and_blocks_reuse(self):
		"""End-to-end: a draft invoice carrying a well-formed credit row passes
		the gate and submits; afterwards the same acquisition is spent — a
		second doc referencing it must be blocked (anti-double-spend)."""
		from ffl_core.compliance.trade_in_credit import validate_trade_in_credit_rows
		suffix = self._testMethodName
		res = trade_in.create_trade_in_intake(self._payload(suffix, apply_credit=1))
		inv = self._build_credit_invoice(res["acquisition"])
		inv.payments = []
		inv.append("payments", {"mode_of_payment": "Cash",
			"amount": inv.grand_total})
		inv.save()
		inv.submit()
		self.assertEqual(inv.docstatus, 1)
		# Same acquisition on a fresh doc → blocked.
		doc = _ns_doc({"ffl_trade_in_acquisition": res["acquisition"]},
			name="OTHER-INV")
		with self.assertRaises(frappe.ValidationError):
			validate_trade_in_credit_rows(doc)

	def test_checkout_submit_books_the_deferred_trade_in(self):
		"""The other half of the deferral: POS Invoice submit is the moment the
		trade completes — the parked draft acquisition must submit (bound book),
		the Material Receipt must materialise the Serial No, and the per-unit
		facts (cost / condition / importer) must be stamped on it."""
		suffix = self._testMethodName
		res = trade_in.create_trade_in_intake(self._payload(
			suffix, apply_credit=1, importer="Interarms", retail_price=450))
		serial = f"TIC-{suffix}"
		self.assertEqual(
			frappe.db.get_value("FFL Acquisition", res["acquisition"], "docstatus"), 0)
		self.assertFalse(frappe.db.exists("Serial No", serial))
		possession_dt = frappe.db.get_value(
			"FFL Acquisition", res["acquisition"], "acquisition_date")

		inv = self._build_credit_invoice(res["acquisition"])
		inv.payments = []
		inv.append("payments", {"mode_of_payment": "Cash", "amount": inv.grand_total})
		inv.save()
		inv.submit()

		acq = frappe.get_doc("FFL Acquisition", res["acquisition"])
		self.assertEqual(acq.docstatus, 1, "checkout submit must book the acquisition")
		# §478.125(e): the bound book records the POSSESSION date (dialog time),
		# and booking must not overwrite it with the checkout moment — a
		# delayed-NICS submit days later would misdate the entry and mask a
		# late booking.
		self.assertEqual(acq.acquisition_date, possession_dt,
			"booking must keep the draft's possession-time acquisition_date")
		self.assertFalse(acq.pending_intake_payload, "stamp data consumed at booking")
		sn = frappe.db.get_value("Serial No", serial,
			["item_code", "purchase_rate", "condition_grade", "importer", "sell_price"],
			as_dict=True)
		self.assertTrue(sn, "Serial No must exist after checkout")
		self.assertEqual(float(sn.purchase_rate), 300.0)
		self.assertEqual(sn.condition_grade, "Good")
		self.assertEqual(sn.importer, "Interarms")
		self.assertEqual(float(sn.sell_price), 450.0)

	# ---- a gun the store sold earlier, traded back in ------------------------

	_STALE_STAMPS = {"is_consignment": 1, "consignment_commission_pct": 20, "pay_on_sale": 1, "srbnb_cost": 150,
		"pay_on_sale_settled": 1, "is_transfer_gun": 1, "needs_service": 1}

	def _sold_gun(self, suffix: str, disposed: bool = True) -> tuple[str, str]:
		"""(item, serial) of a gun the store bought and sold before: its Serial No under its own Item with no
		warehouse, a booked stay closed (``disposed``) by a submitted disposition, and the stamps that stay left."""
		from frappe.utils import add_to_date, now_datetime
		from ffl_core.acquisition import record_firearm_acquisition
		from ffl_core.firearm import used_firearm_item_doc

		item, serial = f"TIC-SOLD-{suffix}", f"TIC-{suffix}"
		frappe.get_doc(used_firearm_item_doc(item_code=item, item_name=item, description=item,
			manufacturer="Glock", model_name="19", firearm_type="Pistol")).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Serial No", "serial_no": serial, "item_code": item}).insert(ignore_permissions=True)
		record_firearm_acquisition(item_code=item, serial=serial, source_type="Trade-in",
			source_name="_Test Trade-in Seller", source_customer="_Test Trade-in Seller", cost=200,
			company=self.company, source_id_type="Driver License", source_id_number="DL-EARLIER",
			acquisition_date=add_to_date(now_datetime(), days=-30))
		if disposed:
			sold = frappe.get_doc({"doctype": "FFL Disposition", "disposition_date": add_to_date(now_datetime(), days=-10),
				"disposition_type": "Retail Sale", "customer": "_Test Trade-in Seller", "company": self.company,
				"item": item, "serial": serial, "form_4473_status": "Completed", "nics_status": "Proceed",
				"fastbound_id": f"FB-{serial}"})
			sold.insert(ignore_permissions=True)
			sold.submit()
		frappe.db.set_value("Serial No", serial, self._STALE_STAMPS, update_modified=False)
		return item, serial

	def _assert_new_stay(self, item: str, serial: str) -> None:
		sn = frappe.db.get_value("Serial No", serial, ["item_code", "warehouse", *self._STALE_STAMPS], as_dict=True)
		self.assertEqual(sn.item_code, item)
		self.assertTrue(sn.warehouse, "the gun is back in stock")
		self.assertEqual({f: flt(sn[f]) for f in self._STALE_STAMPS}, dict.fromkeys(self._STALE_STAMPS, 0),
			"the earlier stay's owner / custody / service stamps must not carry over")
		self.assertEqual(frappe.db.count("FFL Acquisition", {"serial": serial, "docstatus": 1}), 2)

	def test_a_sold_gun_traded_back_for_credit_books_a_new_stay_under_its_own_item(self):
		suffix = self._testMethodName
		item, serial = self._sold_gun(suffix)
		res = trade_in.create_trade_in_intake(self._payload(suffix, apply_credit=1))
		self.assertEqual(res["item_code"], item)
		inv = self._build_credit_invoice(res["acquisition"])
		inv.payments = []
		inv.append("payments", {"mode_of_payment": "Cash", "amount": inv.grand_total})
		inv.save()
		inv.submit()
		self.assertEqual(frappe.db.get_value("FFL Acquisition", res["acquisition"], "docstatus"), 1)
		self._assert_new_stay(item, serial)
		self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 300)

	def test_a_sold_gun_traded_back_for_cash_books_a_new_stay_under_its_own_item(self):
		suffix = self._testMethodName
		item, serial = self._sold_gun(suffix)
		res = trade_in.create_trade_in_intake(self._payload(suffix))
		self.assertEqual(res["item_code"], item)
		self._assert_new_stay(item, serial)

	def test_a_gun_whose_stay_is_still_open_is_refused(self):
		"""Out of stock but never disposed of in the bound book: the store still has it on its books."""
		suffix = self._testMethodName
		self._sold_gun(suffix, disposed=False)
		for over in ({"apply_credit": 1}, {}):
			with self.subTest(**over), self.assertRaisesRegex(frappe.ValidationError, "already on file"):
				trade_in.create_trade_in_intake(self._payload(suffix, **over))

	def test_a_sold_gun_under_another_item_is_refused(self):
		suffix = self._testMethodName
		self._sold_gun(suffix)
		with self.assertRaisesRegex(frappe.ValidationError, "filed under item"):
			trade_in.create_trade_in_intake(self._payload(suffix, item_code=f"OTHER-{suffix}"))

	def test_credit_then_pay_now_for_the_same_gun_replaces_the_draft(self):
		"""The clerk ran it as credit, the customer then wanted cash: the pay-now run replaces the parked draft and
		keeps the Item the credit run made for the gun."""
		suffix = self._testMethodName
		first = trade_in.create_trade_in_intake(self._payload(suffix, apply_credit=1))
		second = trade_in.create_trade_in_intake(self._payload(suffix))
		self.assertEqual(second["item_code"], first["item_code"])
		self.assertFalse(frappe.db.exists("FFL Acquisition", first["acquisition"]))
		self.assertEqual(frappe.get_all("FFL Acquisition", {"serial": f"TIC-{suffix}"}, pluck="docstatus"), [1])

	def _submit_credit_invoice(self, suffix: str) -> "frappe.Document":
		res = trade_in.create_trade_in_intake(self._payload(suffix, apply_credit=1))
		inv = self._build_credit_invoice(res["acquisition"])
		inv.payments = []
		inv.append("payments", {"mode_of_payment": "Cash", "amount": inv.grand_total})
		inv.save()
		inv.submit()
		return inv

	# ---- server-side pin (a past incident) ---------------------
	# Every persisted credit row must be exactly minus the acquisition's cost,
	# whatever the client sent: ERPNext re-prices POS rows from the credit item's
	# auto-inserted Item Price (-869 on prod) and derives a margin from it, so
	# the cart showed +180. The before_validate pin runs BEFORE ERPNext's
	# set_missing_values / calculate_taxes_and_totals, so totals, tax and the
	# payment checks all see the locked amount.

	def _drift(self, row) -> None:
		"""The incident's row shape: list price -869, a derived margin, rate +180."""
		row.rate = 180
		row.price_list_rate = -869
		row.margin_type = "Amount"
		row.margin_rate_or_amount = 1049
		row.discount_percentage = 10
		row.discount_amount = 5

	def _credit_row(self, inv):
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
		return next(r for r in inv.items if r.item_code == TRADE_IN_CREDIT_ITEM)

	def test_draft_save_pins_a_drifted_credit_row(self):
		from ffl_core.setup.locked_lines import LOCKED_RATE_FIELD
		res = trade_in.create_trade_in_intake(self._payload(self._testMethodName, apply_credit=1))
		inv = self._build_credit_invoice(res["acquisition"])
		self._drift(self._credit_row(inv))
		inv.save()
		inv.reload()
		row = self._credit_row(inv)
		self.assertEqual(float(row.rate), -300.0)
		self.assertEqual(float(row.price_list_rate), 0.0)
		self.assertFalse(row.margin_type)
		self.assertEqual(float(row.margin_rate_or_amount), 0.0)
		self.assertEqual(float(row.discount_percentage), 0.0)
		self.assertEqual(float(row.discount_amount), 0.0)
		self.assertEqual(float(row.amount), -300.0)
		self.assertEqual(float(row.get(LOCKED_RATE_FIELD)), -300.0)
		# Net total nets the allowance (500 accessory - 300 credit); the taxable
		# base follows (the credit line carries no exempt template).
		self.assertEqual(float(inv.net_total), 200.0)
		self.assertEqual(float(inv.total_taxes_and_charges), float(inv.grand_total) - 200.0)

	def test_pin_follows_an_acquisition_cost_edit_and_submit_passes(self):
		from ffl_core.setup.locked_lines import LOCKED_RATE_FIELD
		res = trade_in.create_trade_in_intake(self._payload(self._testMethodName, apply_credit=1))
		inv = self._build_credit_invoice(res["acquisition"])
		self.assertEqual(float(self._credit_row(inv).get(LOCKED_RATE_FIELD)), -300.0)
		frappe.db.set_value("FFL Acquisition", res["acquisition"], "cost", 350)
		inv.payments = []
		inv.append("payments", {"mode_of_payment": "Cash", "amount": 500 - 350})
		inv.save()
		row = self._credit_row(inv)
		self.assertEqual(float(row.rate), -350.0)
		self.assertEqual(float(row.get(LOCKED_RATE_FIELD)), -350.0)
		inv.payments[0].amount = inv.grand_total
		inv.save()
		inv.submit()  # the gate agrees with the pin
		self.assertEqual(inv.docstatus, 1)

	def test_pin_and_gate_round_to_the_rate_precision(self):
		"""A sub-cent acquisition cost (API callers may send 180.005) must not
		leave the row pinned at a value the exact-compare gate rejects."""
		res = trade_in.create_trade_in_intake(
			self._payload(self._testMethodName, apply_credit=1, acquisition_cost=180.005))
		self.assertEqual(round(float(res["credit"]["rate"]), 2), float(res["credit"]["rate"]))
		inv = self._build_credit_invoice(res["acquisition"])
		row = self._credit_row(inv)
		self.assertEqual(float(row.rate), round(-180.005, 2))
		inv.payments = []
		inv.append("payments", {"mode_of_payment": "Cash", "amount": inv.grand_total})
		inv.save()
		inv.submit()
		self.assertEqual(inv.docstatus, 1)

	def test_return_rows_are_left_as_copied(self):
		"""A return mirrors the original row (make_sales_return copies its rate,
		which after consolidation is the booked net rate); the pin never moves it
		— re-deriving it from the lock could refund a different figure than was
		booked."""
		from erpnext.accounts.doctype.pos_invoice.pos_invoice import make_sales_return
		from ffl_core.setup.locked_lines import LOCKED_RATE_FIELD
		inv = self._submit_credit_invoice(self._testMethodName)
		ret = make_sales_return(inv.name)
		row = self._credit_row(ret)
		self.assertEqual((float(row.qty), float(row.rate)), (-1.0, -300.0))
		row.set(LOCKED_RATE_FIELD, -999)   # a lock that disagrees: not used on a return
		ret.flags.ignore_permissions = True
		ret.save()
		self.assertEqual(float(self._credit_row(ret).rate), -300.0)

	def test_consolidated_copy_carries_the_pinned_row(self):
		from ffl_core.api.pos_consolidate import consolidate_pos_invoice_now
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
		inv = self._submit_credit_invoice(self._testMethodName)
		with patch.object(frappe.db, "commit"):
			out = consolidate_pos_invoice_now(inv.name)
		self.assertTrue(out["consolidated"])
		si = frappe.db.get_value("POS Invoice", inv.name, "consolidated_invoice")
		rows = frappe.get_all("Sales Invoice Item", filters={"parent": si, "item_code": TRADE_IN_CREDIT_ITEM},
			fields=["rate", "qty", "price_list_rate", "ffl_trade_in_acquisition"])
		self.assertEqual(len(rows), 1)
		self.assertEqual(float(rows[0].rate), -300.0)
		self.assertEqual(float(rows[0].qty), 1.0)
		self.assertEqual(rows[0].ffl_trade_in_acquisition, inv.items[-1].ffl_trade_in_acquisition)

	def test_store_wide_pricing_rule_leaves_the_credit_alone_through_consolidation(self):
		"""A 10% rule on All Item Groups matches the credit item too. The pin keeps
		the row at -cost (price_list_rate 0 gives the rule nothing to discount)
		and strips the rule from the row, so the consolidated copy's submit pass
		— where ERPNext zeroes `rate` on rule-bearing rows — cannot trip the gate."""
		from ffl_core.api.pos_consolidate import consolidate_pos_invoice_now
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
		rule = frappe.get_doc({
			"doctype": "Pricing Rule", "title": "TIC store-wide 10pct", "apply_on": "Item Group",
			"item_groups": [{"item_group": "All Item Groups"}], "selling": 1,
			"price_or_product_discount": "Price", "rate_or_discount": "Discount Percentage",
			"discount_percentage": 10, "company": self.company, "currency": "USD", "priority": 1,
		})
		rule.flags.ignore_permissions = True
		rule.insert()
		try:
			res = trade_in.create_trade_in_intake(self._payload(self._testMethodName, apply_credit=1))
			inv = self._build_credit_invoice(res["acquisition"])
			acc = next(r for r in inv.items if r.item_code == "TIC-ACC")
			self.assertEqual(float(acc.rate), 450.0, "the rule discounts ordinary rows")
			row = self._credit_row(inv)
			self.assertEqual(float(row.rate), -300.0)
			self.assertEqual(float(inv.net_total), 150.0)
			inv.payments = []
			inv.append("payments", {"mode_of_payment": "Cash", "amount": inv.grand_total})
			inv.save()
			inv.submit()
			with patch.object(frappe.db, "commit"):
				out = consolidate_pos_invoice_now(inv.name)
			self.assertTrue(out["consolidated"])
			si = frappe.db.get_value("POS Invoice", inv.name, "consolidated_invoice")
			copy = frappe.get_all("Sales Invoice Item", filters={"parent": si, "item_code": TRADE_IN_CREDIT_ITEM},
				fields=["net_rate", "net_amount", "pricing_rules"])[0]
			self.assertEqual(float(copy.net_rate), -300.0)
			self.assertEqual(float(copy.net_amount), -300.0)
			self.assertFalse(copy.pricing_rules)
		finally:
			frappe.delete_doc("Pricing Rule", rule.name, ignore_permissions=True, force=True)

	# ---- header discount on an invoice with a locked line
	# ERPNext spreads an invoice-level discount over EVERY row's net amount, the
	# negative locked line included: a 10% discount on a $500 sale with a -$300
	# credit books the credit at -$270 (net_rate) while the acquisition cost is
	# $300 — and consolidation copies net_rate as the Sales Invoice rate, where
	# the gate refuses it. Decision: no header discount on such an invoice;
	# discount the goods line instead.

	def test_header_discount_percentage_refused_with_a_credit_line(self):
		res = trade_in.create_trade_in_intake(self._payload(self._testMethodName, apply_credit=1))
		inv = self._build_credit_invoice(res["acquisition"])
		inv.additional_discount_percentage = 10
		with self.assertRaises(frappe.ValidationError) as ctx:
			inv.save()
		self.assertIn("discount", str(ctx.exception).lower())

	def test_header_discount_amount_refused_with_a_credit_line(self):
		res = trade_in.create_trade_in_intake(self._payload(self._testMethodName, apply_credit=1))
		inv = self._build_credit_invoice(res["acquisition"])
		inv.discount_amount = 20
		with self.assertRaises(frappe.ValidationError):
			inv.save()

	def test_header_discount_still_allowed_without_a_locked_line(self):
		self._build_credit_invoice(  # creates TIC-ACC; its invoice is not used
			trade_in.create_trade_in_intake(self._payload(self._testMethodName, apply_credit=1))["acquisition"])
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = self.pos_profile
		inv.customer = "_Test Trade-in Seller"
		inv.append("items", {"item_code": "TIC-ACC", "qty": 1, "rate": 500, "price_list_rate": 500})
		inv.set_missing_values()
		inv.items[0].rate = inv.items[0].price_list_rate = 500
		inv.additional_discount_percentage = 10
		inv.flags.ignore_permissions = True
		inv.save()   # no raise
		self.assertEqual(float(inv.net_total), 450.0)

	# ---- what ERPNext's own validate does after the pin
	# Pricing rules are applied again inside validate, after before_validate: a
	# Rate-type rule on the locked item rewrites its price, a Transaction rule or
	# coupon sets an invoice discount that spreads onto it. Prod has no pricing
	# rules today; if one ever covers these lines the sale is refused, loudly,
	# naming the fix — never booked at a shifted amount.

	def _rule(self, **fields):
		rule = frappe.get_doc({"doctype": "Pricing Rule", "selling": 1, "price_or_product_discount": "Price",
			"company": self.company, "currency": "USD", "priority": 1, **fields})
		rule.flags.ignore_permissions = True
		rule.insert()
		self.addCleanup(frappe.delete_doc, "Pricing Rule", rule.name, ignore_permissions=True, force=True)
		return rule

	def test_rate_rule_on_a_locked_line_is_refused(self):
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
		self._rule(title="TIC rate rule", apply_on="Item Code", items=[{"item_code": TRADE_IN_CREDIT_ITEM}],
			rate_or_discount="Rate", rate=50)
		res = trade_in.create_trade_in_intake(self._payload(self._testMethodName, apply_credit=1))
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._build_credit_invoice(res["acquisition"])
		self.assertIn("pricing rule", str(ctx.exception).lower())

	def test_transaction_discount_rule_is_refused(self):
		self._rule(title="TIC transaction rule", apply_on="Transaction",
			rate_or_discount="Discount Percentage", discount_percentage=10)
		res = trade_in.create_trade_in_intake(self._payload(self._testMethodName, apply_credit=1))
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._build_credit_invoice(res["acquisition"])
		self.assertIn("discount", str(ctx.exception).lower())

	def test_consolidated_copy_rate_is_never_rewritten(self):
		"""A consolidated Sales Invoice copies submitted POS rows and ERPNext does not
		re-total it (calculate_item_values returns early): moving `rate` there would
		leave rate x qty != the booked amount. Only the pricing-rule residue goes."""
		from ffl_core.compliance.locked_lines import pin_locked_lines
		doc = _ns_doc({"ffl_trade_in_acquisition": "ACQ-ANY", "rate": -200.0, "net_rate": -200.0,
			"ffl_locked_rate": -180, "pricing_rules": '["PR-1"]'}, docstatus=0)
		doc.is_consolidated = 1
		doc.doctype = "Sales Invoice"
		pin_locked_lines(doc)
		self.assertEqual(doc.items[0].rate, -200.0)
		self.assertFalse(doc.items[0].pricing_rules)

	def test_tombstone_price_stops_erpnext_auto_inserting_an_item_price(self):
		"""Prod fact: Stock Settings auto-inserts an Item Price from the first
		sale's rate (-869 since 2026-06-27) for any user who may write Item
		Price; every later re-price then sends that rate to the cart. ensure()
		removes live prices for the locked items and leaves an expired tombstone
		that the price lookup never returns but the auto-insert sees as
		'already priced'."""
		from ffl_core.setup.locked_lines import ensure, is_tombstone, locked_items
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
		frappe.db.set_single_value("Stock Settings", "auto_insert_price_list_rate_if_missing", 1)
		frappe.db.set_single_value("Stock Settings", "update_existing_price_list_rate", 0)
		# Control: with no price at all, a save by a price-writing user (the test
		# runs as Administrator) auto-inserts one.
		for ip in frappe.get_all("Item Price", filters={"item_code": TRADE_IN_CREDIT_ITEM}, pluck="name"):
			frappe.delete_doc("Item Price", ip, ignore_permissions=True, force=True)
		res = trade_in.create_trade_in_intake(self._payload(self._testMethodName, apply_credit=1))
		for ip in frappe.get_all("Item Price", filters={"item_code": TRADE_IN_CREDIT_ITEM}, pluck="name"):
			frappe.delete_doc("Item Price", ip, ignore_permissions=True, force=True)
		self._build_credit_invoice(res["acquisition"])
		live = frappe.get_all("Item Price", filters={"item_code": TRADE_IN_CREDIT_ITEM},
			fields=["name", "price_list_rate", "valid_upto"])
		self.assertEqual(len(live), 1, "control: ERPNext should have auto-inserted a price")
		self.assertFalse(is_tombstone(live[0]))
		# The fix: tombstones in, live prices out; a further save inserts nothing.
		ensure()
		prices = frappe.get_all("Item Price", filters={"item_code": ["in", locked_items()]},
			fields=["name", "item_code", "price_list_rate", "valid_from", "valid_upto"])
		self.assertTrue(prices)
		self.assertTrue(all(is_tombstone(p) for p in prices), prices)
		inv = self._build_credit_invoice(res["acquisition"])
		inv.save()
		after = frappe.get_all("Item Price", filters={"item_code": TRADE_IN_CREDIT_ITEM},
			fields=["name", "price_list_rate", "valid_from", "valid_upto"])
		self.assertTrue(all(is_tombstone(p) for p in after), after)
		# And what the cart is sent: no price_list_rate for the credit item.
		from erpnext.stock.get_item_details import apply_price_list
		row = self._credit_row(inv)
		out = apply_price_list({
			"doctype": "POS Invoice", "name": inv.name, "company": inv.company,
			"customer": inv.customer, "currency": inv.currency, "conversion_rate": 1,
			"price_list": inv.selling_price_list, "price_list_currency": inv.price_list_currency,
			"plc_conversion_rate": 1, "transaction_date": inv.posting_date, "pos_profile": inv.pos_profile,
			"items": [{"doctype": row.doctype, "name": row.name, "item_code": row.item_code,
				"qty": 1, "uom": row.uom, "stock_uom": row.stock_uom, "conversion_factor": 1,
				"price_list_rate": row.price_list_rate, "parenttype": "POS Invoice", "parent": inv.name}],
		}, doc=inv)
		self.assertNotIn("price_list_rate", out["children"][0])

	def test_patch_leaves_drafts_to_their_next_save(self):
		"""The patch only retires the live Item Prices (tombstones) and lists the
		drafts with a drifted locked line; it writes no draft — a write outside
		save() leaves ERPNext's per-item tax detail stale. The next save pins it."""
		from ffl_core.patches import pin_locked_invoice_lines
		res = trade_in.create_trade_in_intake(self._payload(self._testMethodName, apply_credit=1))
		inv = self._build_credit_invoice(res["acquisition"])
		row = self._credit_row(inv)
		frappe.db.set_value("POS Invoice Item", row.name, {"rate": 180, "amount": 180}, update_modified=False)
		pin_locked_invoice_lines.execute()
		self.assertEqual(float(frappe.db.get_value("POS Invoice Item", row.name, "rate")), 180.0)
		inv = frappe.get_doc("POS Invoice", inv.name)
		inv.save()
		self.assertEqual(float(self._credit_row(inv).rate), -300.0)
		self.assertEqual(float(inv.net_total), 200.0)

	def test_new_selling_price_list_gets_tombstones(self):
		"""A price list created after the migrate gets its tombstones at once, so
		ERPNext never auto-inserts a live price for a locked item on it."""
		from ffl_core.setup.locked_lines import TOMBSTONE_DATE, locked_items
		pl = frappe.get_doc({"doctype": "Price List", "price_list_name": "TIC Dealer List", "selling": 1,
			"enabled": 1, "currency": "USD"}).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Price List", pl.name, ignore_permissions=True, force=True)
		for item in [i for i in locked_items() if frappe.db.exists("Item", i)]:
			self.assertTrue(frappe.db.exists("Item Price", {"item_code": item, "price_list": pl.name,
				"valid_from": TOMBSTONE_DATE, "valid_upto": TOMBSTONE_DATE}), item)

	def test_negative_line_invoice_consolidates_after_selling_ensure(self):
		"""Regression for the SEV that stranded a real firearm sale: a POS Invoice
		with a negative trade-in line submits at the register, but consolidation
		builds + submits a Sales Invoice, which runs ERPNext's validate_qty and
		rejects negative item rates unless Selling Settings allows them. With the
		flag off a firearm sale silently never leaves stock; setup/selling.ensure
		pins it on so the same invoice consolidates."""
		from ffl_core.api.pos_consolidate import consolidate_pos_invoice_now
		from ffl_core.setup import selling
		inv = self._submit_credit_invoice(self._testMethodName)

		# Simulate the broken prod state, then apply the fix.
		frappe.db.set_single_value("Selling Settings", "allow_negative_rates_for_items", 0)
		selling.ensure()
		# Patch out the commit consolidate_pos_invoice_now does for prod, so the
		# real consolidation (merge log + SI submit + validate_qty) runs inside the
		# test transaction and rolls back — no persistent-DB pollution.
		with patch.object(frappe.db, "commit"):
			out = consolidate_pos_invoice_now(inv.name)

		self.assertTrue(out["consolidated"])
		self.assertEqual(
			frappe.db.get_value("POS Invoice", inv.name, "status"), "Consolidated")

	def test_negative_line_consolidation_blocked_without_the_flag(self):
		"""The dependency the fix rests on: with `allow_negative_rates_for_items`
		off, the consolidated Sales Invoice rejects the negative line. Isolated
		test — the failed submit rolls back with the case."""
		from ffl_core.api.pos_consolidate import consolidate_pos_invoice_now
		inv = self._submit_credit_invoice(self._testMethodName)

		frappe.db.set_single_value("Selling Settings", "allow_negative_rates_for_items", 0)
		# commit patched out for isolation (see the companion test above); the
		# consolidation raises before its commit anyway.
		with patch.object(frappe.db, "commit"), self.assertRaises(frappe.ValidationError):
			consolidate_pos_invoice_now(inv.name)


class TradeInCreditGate(FrappeTestCase):
	"""Submit-time validation of credit rows (SimpleNamespace style — the gate
	reads only doc fields plus the acquisition + reuse lookups)."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		from ffl_core.setup.trade_in_credit import ensure
		ensure()
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", company)
		# One real submitted Trade-in acquisition all gate tests share, booked with nothing paid to its seller (the gate
		# tests spend it as a credit).
		from ffl_core.api.firearm_intake import create_intake
		cls.acq = create_intake({
			"source_type": "Trade-in", "serial": "TIC-GATE-1",
			"manufacturer": "Glock", "model_name": "19", "caliber": "9mm",
			"firearm_type": "Pistol", "condition_grade": "Good",
			"source_id_type": "Driver License", "source_id_number": "DL-GATE-1",
			"source_name": "_Test Trade-in Seller",
			"source_first_name": "Testy", "source_last_name": "Seller",
			"source_address": "123 Main St, Springfield TX 75024",
			"acquisition_cost": 300,
		})["acquisition"]
		# And one parked DRAFT (deferred booking) for the draft-semantics tests.
		res_draft = trade_in.create_trade_in_intake({
			"serial_no": "TIC-GATE-DRAFT-1",
			"manufacturer": "Glock", "model_name": "19", "caliber": "9mm",
			"firearm_type": "Pistol", "condition_grade": "Good",
			"id_type": "Driver License", "id_number": "DL-GATE-2",
			"source_name": "_Test Trade-in Seller",
			"source_first_name": "Testy", "source_last_name": "Seller",
			"source_address": "123 Main St, Springfield TX 75024",
			"acquisition_cost": 300, "apply_credit": 1,
		})
		cls.draft_acq = res_draft["acquisition"]

	def _validate(self, doc):
		from ffl_core.compliance.trade_in_credit import validate_trade_in_credit_rows
		validate_trade_in_credit_rows(doc)

	def test_credit_row_without_acquisition_link_blocked(self):
		doc = _ns_doc({"ffl_trade_in_acquisition": None})
		with self.assertRaises(frappe.ValidationError):
			self._validate(doc)

	def test_credit_row_amount_mismatch_blocked(self):
		doc = _ns_doc({"ffl_trade_in_acquisition": self.acq, "rate": -250.0})
		with self.assertRaises(frappe.ValidationError):
			self._validate(doc)

	def test_credit_row_qty_must_be_one(self):
		doc = _ns_doc({"ffl_trade_in_acquisition": self.acq, "qty": 2})
		with self.assertRaises(frappe.ValidationError):
			self._validate(doc)

	def test_same_acquisition_twice_in_one_doc_blocked(self):
		doc = _ns_doc(
			{"ffl_trade_in_acquisition": self.acq},
			{"ffl_trade_in_acquisition": self.acq},
		)
		with self.assertRaises(frappe.ValidationError):
			self._validate(doc)

	def test_negative_grand_total_only_as_far_as_the_credit(self):
		"""A ticket may go below zero on trade-in credit (the store pays the customer the difference, 2026-10-06),
		never by more than the credit."""
		self._validate(_ns_doc({"ffl_trade_in_acquisition": self.acq}, grand_total=-50.0))  # no raise
		doc = _ns_doc({"ffl_trade_in_acquisition": self.acq}, grand_total=-400.0)
		with self.assertRaises(frappe.ValidationError):
			self._validate(doc)

	def test_clean_credit_row_passes(self):
		doc = _ns_doc({"ffl_trade_in_acquisition": self.acq})
		self._validate(doc)  # no raise

	def test_draft_acquisition_passes_on_pos_invoice(self):
		"""Deferred booking: at POS submit the linked acquisition is still Draft
		(the before_submit hook books it in the same transaction) — the gate must
		accept it, while still enforcing the cost match."""
		self.assertEqual(
			frappe.db.get_value("FFL Acquisition", self.draft_acq, "docstatus"), 0)
		doc = _ns_doc({"ffl_trade_in_acquisition": self.draft_acq})
		self._validate(doc)  # no raise
		bad = _ns_doc({"ffl_trade_in_acquisition": self.draft_acq, "rate": -250.0})
		with self.assertRaises(frappe.ValidationError):
			self._validate(bad)

	def test_draft_acquisition_blocked_on_sales_invoice(self):
		"""A Sales Invoice has no booking hook — a credit row pointing at a
		Draft acquisition there is a real error (only the consolidated copy of
		an already-booked POS row is legitimate)."""
		doc = _ns_doc({"ffl_trade_in_acquisition": self.draft_acq},
			doctype="Sales Invoice", name="NS-SI-1")
		with self.assertRaises(frappe.ValidationError):
			self._validate(doc)

	def test_draft_save_not_blocked(self):
		"""Mid-edit carts may transiently mismatch; the gate fires at submit
		(mirrors the 4473 gate's draft semantics)."""
		doc = _ns_doc({"ffl_trade_in_acquisition": self.acq, "rate": -1.0},
			docstatus=0, grand_total=-1.0)
		self._validate(doc)  # no raise

	def test_gate_compares_at_the_rate_precision(self):
		"""An acquisition cost with sub-cent noise (180.005) vs a row rate that
		ERPNext already rounded to 2 dp must agree, not deadlock the checkout."""
		frappe.db.set_value("FFL Acquisition", self.acq, "cost", 180.005)
		try:
			doc = _ns_doc({"ffl_trade_in_acquisition": self.acq, "rate": round(-180.005, 2)})
			self._validate(doc)  # no raise
			bad = _ns_doc({"ffl_trade_in_acquisition": self.acq, "rate": -180.02})
			with self.assertRaises(frappe.ValidationError):
				self._validate(bad)
		finally:
			frappe.db.set_value("FFL Acquisition", self.acq, "cost", 300)

	def test_pin_leaves_a_pre_fix_return_row_alone(self):
		"""A return made from a sale that predates the lock field carries the
		column's 0 default: 0 is 'no lock', never 'lock to 0' — the mirrored
		credit must not be zeroed."""
		from ffl_core.compliance.locked_lines import pin_locked_lines
		doc = _ns_doc({"ffl_trade_in_acquisition": self.acq, "rate": -300.0, "qty": -1,
			"ffl_locked_rate": 0}, is_return=1)
		self.assertFalse(pin_locked_lines(doc))
		self.assertEqual(doc.items[0].rate, -300.0)
		# Even with a lock on the copied row, a return keeps the rate it copied.
		doc = _ns_doc({"ffl_trade_in_acquisition": self.acq, "rate": -270.0, "qty": -1,
			"ffl_locked_rate": -300}, is_return=1)
		self.assertFalse(pin_locked_lines(doc))
		self.assertEqual(doc.items[0].rate, -270.0)

	def test_header_discount_check_skips_returns_and_consolidated_copies(self):
		"""A return mirrors a sale that may predate the rule; a consolidated
		Sales Invoice carries no header discount (merge zeroes it, the rows'
		net amounts already hold it). Neither may be blocked."""
		from ffl_core.compliance.locked_lines import refuse_header_discount
		for flags in ({"is_return": 1}, {"is_consolidated": 1}):
			doc = _ns_doc({"ffl_trade_in_acquisition": self.acq}, docstatus=0)
			doc.additional_discount_percentage = 10
			doc.discount_amount = 50
			for k, v in flags.items():
				setattr(doc, k, v)
			refuse_header_discount(doc)  # no raise

	def test_return_invoice_skipped(self):
		doc = _ns_doc({"ffl_trade_in_acquisition": self.acq, "rate": 300.0},
			is_return=1)
		self._validate(doc)  # no raise

	def test_non_credit_rows_ignored(self):
		doc = SimpleNamespace(
			doctype="POS Invoice", name="NS-INV-2", docstatus=1, is_return=0,
			grand_total=100.0,
			items=[SimpleNamespace(idx=1, item_code="SOME-ACCESSORY", qty=1,
				rate=100.0, ffl_trade_in_acquisition=None)],
		)
		self._validate(doc)  # no raise


if __name__ == "__main__":
	unittest.main()
