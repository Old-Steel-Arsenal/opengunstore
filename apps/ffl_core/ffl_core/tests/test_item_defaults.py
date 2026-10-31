"""setup/item_defaults.set_item_default_accounts: the per-company Item Default writer the trade-in credit,
gunsmith deposit and membership items share. Real rows on a throwaway item; FrappeTestCase rolls back."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.setup.item_defaults import set_item_default_accounts
from ffl_core.setup.tax import SERVICE_GROUP

ITEM = "TEST-ITEM-DEFAULTS"


class SetItemDefaultAccounts(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		from ffl_core.setup.item_groups import ensure as ensure_item_groups

		dev_site.run()
		ensure_item_groups()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		cls.a1, cls.a2 = frappe.get_all("Account", filters={"company": cls.company, "root_type": "Income",
			"is_group": 0}, pluck="name", order_by="lft", limit=2)

	def setUp(self):
		if not frappe.db.exists("Item", ITEM):
			frappe.get_doc({"doctype": "Item", "item_code": ITEM, "item_name": ITEM, "item_group": SERVICE_GROUP,
				"stock_uom": "Nos", "is_stock_item": 0, "is_sales_item": 1, "is_firearm": 0}).insert(ignore_permissions=True)
		frappe.db.delete("Item Default", {"parent": ITEM, "parenttype": "Item"})

	def _rows(self):
		return frappe.get_all("Item Default", filters={"parent": ITEM, "parenttype": "Item"},
			fields=["company", "income_account", "deferred_revenue_account", "parentfield", "idx"])

	def test_inserts_updates_and_skips(self):
		account = {"income_account": self.a1}
		only_ours = lambda company: account if company == self.company else None  # noqa: E731

		self.assertEqual(set_item_default_accounts(ITEM, only_ours), 1)
		rows = self._rows()
		self.assertEqual([(r.company, r.income_account, r.parentfield) for r in rows],
			[(self.company, self.a1, "item_defaults")])
		self.assertEqual(set_item_default_accounts(ITEM, only_ours), 0)  # steady state

		account = {"income_account": self.a2}
		self.assertEqual(set_item_default_accounts(ITEM, only_ours), 1)  # updated in place, not a second row
		self.assertEqual([(r.company, r.income_account) for r in self._rows()], [(self.company, self.a2)])

		self.assertEqual(set_item_default_accounts(ITEM, lambda company: None), 0)  # None skips the company
		self.assertEqual(self._rows()[0].income_account, self.a2)

	def test_several_fields_compare_together(self):
		values = {"income_account": self.a1, "deferred_revenue_account": self.a2}
		write = lambda company: values if company == self.company else None  # noqa: E731
		self.assertEqual(set_item_default_accounts(ITEM, write), 1)
		self.assertEqual(set_item_default_accounts(ITEM, write), 0)
		values = {"income_account": self.a1, "deferred_revenue_account": self.a1}
		self.assertEqual(set_item_default_accounts(ITEM, write), 1)  # one differing field is a change
		row = self._rows()[0]
		self.assertEqual((row.income_account, row.deferred_revenue_account), (self.a1, self.a1))
