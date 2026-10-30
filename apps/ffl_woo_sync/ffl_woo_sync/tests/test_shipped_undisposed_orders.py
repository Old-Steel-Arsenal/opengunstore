"""order_audit.shipped_undisposed_orders: the read-only list behind the
shipped-but-not-disposed alarm, on the alarm's own query, with no Error Log."""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from ffl_woo_sync.woocommerce import order_audit, tasks


def _order(woo_status: str, order_status: str = "Pending Fulfillment", hours_ago: int = 48) -> str:
	doc = frappe.get_doc({"doctype": "Woo Online Order", "woo_order_id": frappe.generate_hash(length=8),
		"order_status": order_status, "woo_status": woo_status, "customer_name": "Test Buyer"})
	doc.flags.ignore_permissions = True
	doc.flags.ignore_mandatory = True
	doc.db_insert()
	frappe.db.set_value("Woo Online Order", doc.name, "modified",
		add_to_date(now_datetime(), hours=-hours_ago), update_modified=False)
	return doc.name


class ShippedUndisposedOrders(FrappeTestCase):

	def setUp(self):
		frappe.set_user("Administrator")

	def test_lists_what_the_alarm_reports_and_logs_nothing(self):
		shipped = _order("completed")
		fresh = _order("completed", hours_ago=1)            # inside the grace period
		open_ = _order("processing")
		done = _order("completed", order_status="Transferred")
		with patch("frappe.log_error") as log_error:
			rows = order_audit.shipped_undisposed_orders()
		log_error.assert_not_called()
		names = [r["name"] for r in rows]
		self.assertIn(shipped, names)
		for other in (fresh, open_, done):
			self.assertNotIn(other, names)
		row = next(r for r in rows if r["name"] == shipped)
		self.assertEqual(set(row), {"name", "woo_order_id", "customer_name", "modified"})
		alarm = frappe.db.sql(tasks._SHIPPED_UNDISPOSED_SQL,
			{"grace": tasks._SHIPPED_UNDISPOSED_GRACE_HOURS}, as_dict=True)
		self.assertEqual(names, [r["name"] for r in alarm], "one predicate with the alarm")

	def test_counter_roles_only(self):
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			order_audit.shipped_undisposed_orders()
