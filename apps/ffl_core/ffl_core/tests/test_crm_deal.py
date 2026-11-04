"""CRM Deal lines priced as the register charges, with CRM's own amount arithmetic."""
from __future__ import annotations

import frappe

from ffl_core.tests.crm_support import AMMO, PER_GUN, UNIFORM, CRMTestCase, product_of


def _status(kind: str | None = None) -> str:
	filters = {"type": kind} if kind else {"type": ["not in", ["Won", "Lost"]]}
	return frappe.get_all("CRM Deal Status", filters=filters, pluck="name", order_by="position asc", limit=1)[0]


def _gun(serial: str, price: float, status: str = "Active") -> str:
	sn = frappe.get_doc({"doctype": "Serial No", "serial_no": serial, "item_code": PER_GUN, "sell_price": price})
	sn.insert(ignore_permissions=True)
	# Stock is not what is under test: give the gun the status a receipt would.
	frappe.db.set_value("Serial No", sn.name, "status", status, update_modified=False)
	return sn.name


def _deal(*rows):
	return frappe.get_doc({"doctype": "CRM Deal", "status": _status(), "products": list(rows)}).insert(
		ignore_permissions=True)


class TestDealPricing(CRMTestCase):
	def test_lines_take_the_register_price_and_crm_arithmetic(self):
		deal = _deal(
			{"product_code": product_of(AMMO), "qty": 3, "rate": 1},
			{"product_code": product_of(UNIFORM), "qty": 1, "rate": 999, "discount_percentage": 10})
		ammo, gun = deal.products
		self.assertEqual((ammo.rate, ammo.amount, ammo.net_amount, ammo.discount_amount), (24.99, 74.97, 74.97, 0))
		self.assertEqual((gun.rate, gun.amount, gun.discount_amount, gun.net_amount), (589, 589, 58.9, 530.1))
		self.assertEqual((deal.total, deal.net_total), (663.97, 605.07))

	def test_a_per_gun_line_without_a_gun_takes_the_cheapest_in_stock(self):
		_gun("_T-CRM-SN-1", 899)
		_gun("_T-CRM-SN-2", 749)
		_gun("_T-CRM-SN-SOLD", 499, status="Delivered")
		deal = _deal({"product_code": product_of(PER_GUN), "qty": 1})
		self.assertEqual(deal.products[0].rate, 749)

	def test_a_per_gun_line_with_its_gun(self):
		serial = _gun("_T-CRM-SN-3", 899)
		deal = _deal({"product_code": product_of(PER_GUN), "qty": 1, "ffl_serial_no": serial})
		self.assertEqual(deal.products[0].rate, 899)

	def test_a_line_refuses_a_gun_of_another_model_or_out_of_stock(self):
		sold = _gun("_T-CRM-SN-4", 899, status="Delivered")
		with self.assertRaisesRegex(frappe.ValidationError, "no longer in stock"):
			_deal({"product_code": product_of(PER_GUN), "qty": 1, "ffl_serial_no": sold})
		with self.assertRaisesRegex(frappe.ValidationError, "is not a gun of"):
			_deal({"product_code": product_of(UNIFORM), "qty": 1, "ffl_serial_no": _gun("_T-CRM-SN-5", 899)})
		with self.assertRaisesRegex(frappe.ValidationError, "quantity 1"):
			_deal({"product_code": product_of(PER_GUN), "qty": 2, "ffl_serial_no": _gun("_T-CRM-SN-6", 899)})

	def test_a_product_that_is_not_a_pos_item_keeps_crm_pricing(self):
		frappe.get_doc({"doctype": "CRM Product", "product_code": "_T-CRM-SERVICE", "standard_rate": 5}).db_insert()
		deal = _deal({"product_code": "_T-CRM-SERVICE", "product_name": "Service", "qty": 1, "rate": 7, "amount": 7})
		self.assertEqual(deal.products[0].rate, 7)

	def test_a_won_deal_keeps_its_lines_after_the_gun_is_sold(self):
		serial = _gun("_T-CRM-SN-7", 899)
		deal = _deal({"product_code": product_of(PER_GUN), "qty": 1, "ffl_serial_no": serial})
		# Sold at the register; then the deal is marked won and saved again, and later edited.
		frappe.db.set_value("Serial No", serial, "status", "Delivered", update_modified=False)
		frappe.db.set_value("Serial No", serial, "sell_price", 950, update_modified=False)
		deal.reload()
		deal.status = _status("Won")
		deal.save(ignore_permissions=True)
		deal.save(ignore_permissions=True)
		self.assertEqual((deal.products[0].rate, deal.total), (899, 899))
