"""What Frappe's built-in AI agent (Flow) gets from ffl_core.

Flow's Assistant reads gun-store rules from the skills in ``apps/ffl_core/skills`` and
calls whitelisted functions marked ``@frappe.read_only()`` without asking the user.
"""
from __future__ import annotations

from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.test_pos_picker_lockdown import DEALER, ITEM, PickerLockdownFixtures

READ_APIS = (
	"ffl_core.api.agent.firearm_stock_summary",
	"ffl_core.api.agent.serials_for_sale",
	"ffl_core.api.agent.item_stock",
)
# The POS calls these inside its own write transactions, where @frappe.read_only() would
# send their reads to a replica.
POS_INTERNAL_APIS = (
	"ffl_core.firearm.available_serials_with_prices",
	"ffl_core.api.item_admin.get_item_stock",
)
_ARGS = {
	READ_APIS[0]: {},
	READ_APIS[1]: {"item_codes": [ITEM]},
	READ_APIS[2]: {"item_codes": [ITEM]},
}


class FlowCallApiRunsThemReadOnly(FrappeTestCase):
	"""The real path: Flow's call_api tool, which opens frappe.db.read_only() for these.
	No fixtures here, since a read-only transaction cannot start after uncommitted writes."""

	def setUp(self):
		import frappe

		frappe.db.rollback()  # whatever an earlier test in this worker left open

	def test_call_api_runs_each_read_api(self):
		from frappe.flow import ToolContext
		from frappe.flow.tools.builtins import call_api

		ctx = ToolContext(session_id="test", agent="test", user="Administrator")
		for path in READ_APIS:
			with self.subTest(path):
				out = call_api(method=path, args=_ARGS[path], ctx=ctx)
				self.assertIn("result", out)
		summary = call_api(method=READ_APIS[0], args={}, ctx=ctx)["result"]
		self.assertEqual(summary["on_hand"], summary["for_sale"] + summary["not_for_sale"])


class FirearmStockSummary(PickerLockdownFixtures):
	def test_counts_on_hand_and_for_sale(self):
		import frappe
		from ffl_core.api.agent import firearm_stock_summary
		from ffl_core.firearm import transfer_serial
		from ffl_core.warehouses import ensure_consignment_warehouse

		before = firearm_stock_summary()
		free, held, consigned, in_cwh = (f"FLOW-SUM-{t}" for t in ("FREE", "HELD", "CONS", "CWH"))
		for serial in (free, held, consigned, in_cwh):
			self._receive(serial)
		self._draft_cono(held)
		frappe.db.set_value("Serial No", consigned, "consigned_out", 1)
		transfer_serial(ITEM, in_cwh, self.stores, ensure_consignment_warehouse(DEALER, self.company))
		after = firearm_stock_summary()

		self.assertEqual(after["on_hand"] - before["on_hand"], 4)
		self.assertEqual(after["for_sale"] - before["for_sale"], 1)
		self.assertEqual(after["not_for_sale"] - before["not_for_sale"], 3)
		self.assertEqual(after["on_hand"], after["for_sale"] + after["not_for_sale"])
		self.assertEqual(sum(after["by_warehouse"].values()), after["on_hand"])
		self.assertEqual(sum(after["by_type"].values()), after["on_hand"])
		self.assertEqual(after["by_warehouse"].get(self.stores, 0) - before["by_warehouse"].get(self.stores, 0), 3)


class FlowSeesReadOnlyApis(PickerLockdownFixtures):
	def test_read_apis_run_without_approval(self):
		from frappe.flow.tools.builtins import is_read_function

		for path in READ_APIS:
			self.assertTrue(is_read_function(path), path)
		for path in POS_INTERNAL_APIS:
			self.assertFalse(is_read_function(path), path)

	def test_read_apis_write_nothing_with_guns_on_hand(self):
		"""Flow refuses a read API that writes (frappe.db.transaction_writes grows)."""
		import frappe

		self._receive("FLOW-RO-1")
		self._receive("FLOW-RO-2")
		self._draft_cono("FLOW-RO-2")
		for path in READ_APIS:
			before = frappe.db.transaction_writes
			result = frappe.call(path, **_ARGS[path])
			self.assertEqual(frappe.db.transaction_writes, before, path)
			self.assertTrue(result, path)

	def test_skills_sync_to_flow(self):
		import frappe
		from frappe.flow.doctype.flow_skill.flow_skill import sync_skills

		synced = sync_skills()
		for name in ("ffl_core/firearm-inventory", "ffl_core/firearm-records"):
			self.assertIn(name, synced)
			self.assertTrue(frappe.db.get_value("Flow Skill", name, "instructions"))

	def test_inventory_skill_names_real_apis(self):
		"""A renamed endpoint would leave the skill pointing the agent at nothing."""
		import frappe
		from frappe.flow.doctype.flow_skill.flow_skill import sync_skills

		sync_skills()
		text = frappe.db.get_value("Flow Skill", "ffl_core/firearm-inventory", "instructions")
		for path in READ_APIS:
			self.assertIn(path, text)
		self.assertTrue(frappe.db.exists("Report", "Firearms In Stock"))
