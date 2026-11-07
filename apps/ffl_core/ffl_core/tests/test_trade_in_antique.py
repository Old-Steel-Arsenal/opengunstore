"""POS Trade-in of an antique (pre-1899 receiver): never in the bound book.

With credit the gun is parked as a DRAFT Material Receipt marked as a trade-in (trade_in._park_antique) — no FFL
Acquisition — and its credit line locks to that receipt; checkout submits it and stamps the gun's facts on the new
Serial No. A pay-now buy is an antique line on the seller's paid Purchase Receipt (no acquisition either). An antique
goes only under an antique Item, a regulated gun never under one.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.utils import flt

from ffl_core.setup.trade_in_credit import TRADE_IN_SELLER_FIELD, TRADE_IN_STAMP_FIELD
# The module, not the class: a class imported here would be collected and run a second time.
from ffl_core.tests import test_trade_in_accessories as acc

SELLER, _item = acc.SELLER, acc._item


class TradeInAntique(acc.TradeInAccessories):
	"""Reuses the accessory suite's setup (seller, counter user, POS opening) and helpers, not its tests."""

	@classmethod
	def _drop_inherited_tests(cls) -> None:
		for name in [n for n in dir(acc.TradeInAccessories) if n.startswith("test_")]:
			setattr(cls, name, None)

	def _antique(self, suffix: str, **over) -> dict:
		return self._payload(suffix, **{"accessories": [], "is_antique": 1, "serial_no": f"TIANT-{suffix}",
			"manufacturer": "Steyr", "model_name": "Gew.88", "caliber": "8mm Mauser", "firearm_type": "Rifle",
			"condition_grade": "Good", "acquisition_cost": 200, "retail_price": 650, "year_made": 1891, **over})

	def test_credit_parks_a_receipt_and_never_an_acquisition(self):
		res = self._as_counter(self._antique("credit", apply_credit=1))
		serial = "TIANT-credit"
		self.assertIsNone(res["acquisition"])
		self.assertFalse(frappe.db.exists("FFL Acquisition", {"serial": serial}))
		self.assertEqual(frappe.db.get_value("Item", res["item_code"], ["is_antique", "year_made"]), (1, 1891))
		[credit] = res["credits"]
		self.assertEqual((flt(credit["rate"]), credit["stock_entry"]), (-200.0, res["antique_receipt"]))
		self.assertNotIn("acquisition", credit)
		se = frappe.get_doc("Stock Entry", res["antique_receipt"])
		self.assertEqual((se.docstatus, se.get(TRADE_IN_SELLER_FIELD)), (0, SELLER))
		self.assertEqual(json.loads(se.get(TRADE_IN_STAMP_FIELD))["condition_grade"], "Good")
		self.assertFalse(frappe.db.exists("Serial No", serial), "nothing reaches stock before the sale submits")

		with patch("ffl_integrations.fastbound.tasks.frappe.enqueue") as enqueue:
			self._credit_invoice(res["credits"]).submit()
		self.assertEqual(frappe.db.get_value("Stock Entry", se.name, "docstatus"), 1)
		sn = frappe.db.get_value("Serial No", serial,
			["item_code", "status", "condition_grade", "sell_price", "purchase_rate", "fastbound_item_id"], as_dict=True)
		self.assertEqual((sn.item_code, sn.status, sn.condition_grade), (res["item_code"], "Active", "Good"))
		self.assertEqual((flt(sn.sell_price), flt(sn.purchase_rate)), (650.0, 200.0))
		self.assertFalse(sn.fastbound_item_id)
		self.assertFalse(frappe.db.exists("FFL Acquisition", {"serial": serial}))
		enqueue.assert_not_called()

	def test_rerun_replaces_the_pending_receipt(self):
		first = self._as_counter(self._antique("rerun", apply_credit=1))
		second = self._as_counter(self._antique("rerun", apply_credit=1, acquisition_cost=250))
		# One pending receipt for the gun, at the new value (a freed name may be handed out again).
		drafts = frappe.get_all("Stock Entry Detail", filters={"serial_no": "TIANT-rerun", "docstatus": 0},
			fields=["parent", "basic_rate"])
		self.assertEqual([(d.parent, flt(d.basic_rate)) for d in drafts], [(second["antique_receipt"], 250.0)])
		self.assertEqual(second["item_code"], first["item_code"], "the gun keeps the Item the first run made")
		self.assertEqual(flt(second["credit"]["rate"]), -250.0)

	def test_regulated_rerun_drops_the_antique_draft(self):
		"""Ticked Antique by mistake, then re-run unticked: the antique draft goes, the gun gets a regulated Item."""
		first = self._as_counter(self._antique("untick", apply_credit=1))
		second = self._as_counter(self._antique("untick", apply_credit=1, is_antique=0))
		self.assertFalse(frappe.db.exists("Stock Entry", first["antique_receipt"]))
		self.assertTrue(second["acquisition"])
		self.assertNotEqual(second["item_code"], first["item_code"])
		self.assertEqual(frappe.db.get_value("Item", second["item_code"], "is_antique"), 0)

	def test_pay_now_is_a_paid_receipt_without_an_acquisition(self):
		res = self._as_counter(self._antique("paynow", payout_method="Cash"))
		pr = frappe.get_doc("Purchase Receipt", res["purchase_receipt"])
		self.assertEqual(pr.docstatus, 1)
		self.assertIsNone(res["acquisition"])
		self.assertFalse(frappe.db.exists("FFL Acquisition", {"serial": "TIANT-paynow"}))
		self.assertEqual(frappe.db.get_value("Item", res["item_code"], "is_antique"), 1)
		self.assertEqual(frappe.db.get_value("Serial No", "TIANT-paynow", "status"), "Active")
		self.assertEqual(flt(frappe.db.get_value("Cash Drawer Entry", res["payout"]["entry"], "amount")), 200.0)

	def test_rerun_with_a_chosen_item_still_replaces_the_draft(self):
		antique = _item("TIANT-ANT-RERUN", "Rifle", has_serial_no=1, is_firearm=1, is_antique=1)
		trade_in_call(self._antique("chosen", apply_credit=1, item_code=antique))
		second = trade_in_call(self._antique("chosen", apply_credit=1, item_code=antique, acquisition_cost=260))
		drafts = frappe.get_all("Stock Entry Detail", filters={"serial_no": "TIANT-chosen", "docstatus": 0},
			fields=["parent", "basic_rate"])
		self.assertEqual([(d.parent, flt(d.basic_rate)) for d in drafts], [(second["antique_receipt"], 260.0)])

	def test_regulated_pay_now_with_a_chosen_item_drops_the_credit_draft(self):
		"""Credit run under a chosen Item, cart abandoned, then paid now under the same Item: one acquisition, booked."""
		gun = _item("TIANT-REG-RERUN", "Rifle", has_serial_no=1, is_firearm=1)
		serial = "TIANT-reg-chosen"
		first = trade_in_call(self._payload("reg-chosen", accessories=[], serial_no=serial, item_code=gun, apply_credit=1))
		self.assertEqual(frappe.db.get_value("FFL Acquisition", first["acquisition"], "docstatus"), 0)
		trade_in_call(self._payload("reg-chosen", accessories=[], serial_no=serial, item_code=gun, payout_method="Cash"))
		self.assertEqual(sorted(frappe.get_all("FFL Acquisition", {"serial": serial}, pluck="docstatus")), [1])

	def test_pay_now_replaces_the_antique_draft(self):
		first = self._as_counter(self._antique("paynow-after", apply_credit=1))
		res = self._as_counter(self._antique("paynow-after", payout_method="Cash"))
		self.assertFalse(frappe.db.exists("Stock Entry", {"name": first["antique_receipt"], "docstatus": 0,
			"remarks": ("like", "Antique trade-in%")}))
		self.assertEqual(res["item_code"], first["item_code"])
		self.assertEqual(frappe.db.get_value("Serial No", "TIANT-paynow-after", "status"), "Active")

	def test_a_draft_on_a_held_sale_is_not_replaced(self):
		first = self._as_counter(self._antique("held", apply_credit=1))
		self._credit_invoice(first["credits"])  # saved, not submitted: the sale is held
		with self.assertRaisesRegex(frappe.ValidationError, "held sale"):
			self._as_counter(self._antique("held", apply_credit=1, acquisition_cost=300))
		with self.assertRaisesRegex(frappe.ValidationError, "held sale"):
			self._as_counter(self._antique("held", payout_method="Cash"))

	def test_item_must_match_antique(self):
		regulated = _item("TIANT-REG-RIFLE", "Rifle", has_serial_no=1, is_firearm=1)
		antique = _item("TIANT-ANT-RIFLE", "Rifle", has_serial_no=1, is_firearm=1, is_antique=1)
		with self.assertRaisesRegex(frappe.ValidationError, "is not an antique item"):
			trade_in_call(self._antique("wrong-item", apply_credit=1, item_code=regulated))
		with self.assertRaisesRegex(frappe.ValidationError, "is an antique item"):
			trade_in_call(self._antique("ant-item", apply_credit=1, item_code=antique, is_antique=0))
		res = trade_in_call(self._antique("ant-ok", apply_credit=1, item_code=antique))
		self.assertEqual(res["item_code"], antique)


def trade_in_call(payload: dict) -> dict:
	"""As Administrator (a stock role): the counter may not file under a chosen Item that is not per-gun priced."""
	from ffl_core.api import trade_in
	return trade_in.create_trade_in_intake(payload)


TradeInAntique._drop_inherited_tests()
