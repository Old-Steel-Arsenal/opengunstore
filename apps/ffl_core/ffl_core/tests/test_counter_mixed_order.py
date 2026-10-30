"""Mixed-order rules layer.

`_assert_no_counter_mixed_order` was removed: one counter order may carry both
local stock lines and drop-ship lines, each following its own fulfilment leg
(local = DN / disposition-defer leg, drop-ship = Draft DO leg). The `to_store`
channel has since shipped; its rules and site tests live in test_counter_to_store.py.

Pure unittest with per-item stubs: they answer only the item under test and never
replace frappe.db wholesale.
The on-site orchestration integration tests are in the MixedOrderOrchestration
(FrappeTestCase) section.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ffl_core.api import manual_order


def _exists_known(doctype, name=None, *a, **k):
	if doctype == "Item":
		return name in {"MIX-ACC", "MIX-GUN", "MIX-DS"}
	return None


_GUNS = {"MIX-GUN"}


def _elig(item_code, distributor=None, sku=None, *, is_firearm=False):
	return {"distributor": distributor or "RSR", "sku": sku, "restricted_states": ""}


class MixedOrderRules(unittest.TestCase):
	def setUp(self):
		for p in [
			mock.patch.object(manual_order.frappe.db, "exists", _exists_known),
			mock.patch.object(manual_order, "is_firearm_item", lambda c: c in _GUNS),
			mock.patch.object(
				manual_order, "is_regulated_firearm_item", lambda c: c in _GUNS),
			mock.patch.object(manual_order, "_dropship_eligibility", _elig, create=True),
			# Serial validation of a local gun row queries stock/warehouse; the unit layer only answers "sellable, in the store warehouse".
			mock.patch.object(manual_order, "_validate_serial",
				lambda serial, item_code, seen, ctx=None: "Store - F"),
		]:
			p.start()
			self.addCleanup(p.stop)

	def test_assert_no_counter_mixed_order_is_gone(self):
		# Lifting the ban is not "relaxing a condition" but retiring the assertion
		# entirely: the name must not exist any more, otherwise a rebase could
		# quietly wire it back into create_order.
		self.assertFalse(hasattr(manual_order, "_assert_no_counter_mixed_order"))

	def test_mixed_rows_validate_together(self):
		rows, _serials, has_reg = manual_order._validate_items([
			{"item_code": "MIX-DS", "qty": 2, "rate": 30, "source": "dropship"},
			{"item_code": "MIX-ACC", "qty": 1, "rate": 10},
		])
		self.assertEqual([r.get("source", "") for r in rows], ["dropship", ""])
		self.assertFalse(has_reg)

	def test_mixed_with_local_gun_keeps_regulated(self):
		rows, _serials, has_reg = manual_order._validate_items([
			{"item_code": "MIX-DS", "qty": 1, "rate": 30, "source": "dropship"},
			{"item_code": "MIX-GUN", "qty": 1, "rate": 500, "serials": ["SN-1"]},
		])
		self.assertTrue(has_reg)
		self.assertEqual(rows[1]["serials"], ["SN-1"])


# ---------------------------------------------------------------- integration

import frappe  # noqa: E402
from frappe.tests.utils import FrappeTestCase  # noqa: E402

from ffl_core.tests.test_build_order import BuildOrderMixin  # noqa: E402
from ffl_core.tests import test_counter_dropship_integration as p4b  # noqa: E402
from ffl_integrations.tests import catalog_stub  # noqa: E402

LOCAL_ACC = "MIX-LOCAL-ACC"


class MixedOrderFixtures(BuildOrderMixin, FrappeTestCase):
	"""Mixed-order fixtures + _mixed_counter, with no test_ methods: other modules
	inherit this one, and inheriting MixedOrderOrchestration would re-run its
	cases under the subclass name.

	Reuses the idempotent fixtures of the counter drop-ship integration suite (tax
	template / POS Profile / distributor / drop-ship Item): all are created
	idempotently under a fixed title/name, so feed them the company and call them
	directly; better than copying them and letting two sets of fixtures drift."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		p4b.CounterDropshipIntegration.company = cls.company
		p4b.CounterDropshipIntegration._ensure_default_tax_template()
		p4b.CounterDropshipIntegration._ensure_pos_profile_default_tax()
		p4b.CounterDropshipIntegration._ensure_oos_template()
		cls.stub = catalog_stub.install()
		p4b.CounterDropshipIntegration._dropship_fixture()
		cls._local_stock_acc()

	@classmethod
	def tearDownClass(cls):
		catalog_stub.uninstall()
		super().tearDownClass()

	@classmethod
	def _local_stock_acc(cls):
		"""A real stock accessory with 8 units on hand, the raw material for the local
		DN leg (BuildOrderMixin._item's accessory is is_stock_item=0 and never
		reaches _classify_deferred_lines)."""
		if not frappe.db.exists("Item", LOCAL_ACC):
			frappe.get_doc({
				"doctype": "Item", "item_code": LOCAL_ACC, "item_name": LOCAL_ACC,
				"item_group": "Accessory" if frappe.db.exists("Item Group", "Accessory")
					else "All Item Groups",
				"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 0,
			}).insert(ignore_permissions=True)
		from ffl_core.api.item_admin import get_item_stock
		if float((get_item_stock([LOCAL_ACC]) or {}).get(LOCAL_ACC) or 0) < 8:
			frappe.get_doc({
				"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
				"company": cls.company,
				"items": [{"item_code": LOCAL_ACC, "qty": 8,
					"t_warehouse": cls.warehouse, "basic_rate": 5}],
			}).submit()

	def _pos_profile(self) -> str:
		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		self.assertTrue(pos)
		return pos

	_SHIP_TO = {"address_line1": "5 Elm St", "city": "Springfield",
		"state": "TX", "pincode": "75024"}

	def _mixed_counter(self):
		return manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv("Ann Individual"),
			"pos_profile": self._pos_profile(),
			"dispose_now": 0, "payment_received": 1, "mode_of_payment": "Cash",
			"contact_email": "mix@example.com",
			"ship_to": dict(self._SHIP_TO),
			"items": [
				{"item_code": LOCAL_ACC, "qty": 1, "rate": 20, "serials": []},
				{"item_code": p4b.ACC_ITEM, "qty": 2, "rate": 40, "serials": [],
					"source": "dropship"},
			],
		})


class MixedOrderOrchestration(MixedOrderFixtures):
	"""Mixed order submit -> 1 SI (update_stock=0) + 1 DO (drop-ship lines only) + a pending DN for the local lines."""

	def test_mixed_order_orchestrates_both_legs(self):
		out = self._mixed_counter()
		self.assertTrue(out.get("ok"))
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(int(si.update_stock), 0)

		dos = frappe.get_all("Distributor Order",
			filters={"source_doctype": "Sales Invoice", "source_name": si.name},
			fields=["name", "status"])
		self.assertEqual(len(dos), 1)
		do = frappe.get_doc("Distributor Order", dos[0].name)
		self.assertEqual({l.item_code for l in do.lines}, {p4b.ACC_ITEM})
		self.assertEqual(sum(int(l.wish_qty) for l in do.lines), 2)

		undelivered = manual_order._undelivered_stock_lines(si)
		self.assertEqual({r.item_code for r in undelivered}, {LOCAL_ACC})
		# The local line hangs on the SO awaiting DN fulfilment; nothing delivered yet
		row = undelivered[0]
		so_line = frappe.db.get_value("Sales Order Item", row.so_detail,
			["qty", "delivered_qty"], as_dict=True)
		self.assertEqual(float(so_line.delivered_qty), 0.0)

	def test_do_completion_books_cost_and_leaves_local_leg_alone(self):
		from ffl_integrations.distributor import api as dist_api
		from ffl_integrations.distributor import tasks as dist_tasks

		out = self._mixed_counter()
		si_name = out["sales_invoice"]
		do_name = frappe.get_all("Distributor Order",
			filters={"source_doctype": "Sales Invoice", "source_name": si_name},
			pluck="name")[0]
		# Draft->Queued needs the order paid up (an is_pos fully-paid order has
		# outstanding=0), then step through to Complete.
		# confirm would enqueue the real ordering worker; in tests just walk the state machine.
		dist_api.transition_order(do_name, "Queued")
		dist_api.transition_order(do_name, "Placed")
		dist_api.transition_order(do_name, "Complete")
		result = dist_tasks.complete_counter_order(si_name)
		self.assertEqual(result.get("status"), "complete")
		# Completion only touches the drop-ship leg; the local line still awaits DN fulfilment
		si = frappe.get_doc("Sales Invoice", si_name)
		undelivered = manual_order._undelivered_stock_lines(si)
		self.assertEqual({r.item_code for r in undelivered}, {LOCAL_ACC})
