"""The web store's half of the shared pool.

Three things moved here from ffl_core's shared-pool work, and each is pinned
separately because each fails in its own quiet way:

* ``free_serials`` / ``free_units`` became thin delegates. Nothing about the
  storefront count may change except the two deliberate narrowings (a gun flagged
  "possibly sold on GunBroker" leaves the pool; the list is oldest-first).
* the storefront count now subtracts what OTHER channels have sold and not yet
  shipped (GunBroker's claims and ammunition lines). The web store's own are
  already counted by its own rule, so they must not be subtracted twice.
* the web store tells the other channels when its own commitments move — a
  checkout and a cancellation — because nothing else would (no stock ledger entry
  is posted until the goods leave).
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.tests.test_woo_quantity_hold import OTHER, UNIFORM, _ensure_items
from ffl_woo_sync.woocommerce import channel, mapping, webhook


_INTAKE = patch("ffl_woo_sync.woocommerce.webhook.intake_customer", return_value=None)


def setUpModule():
	# The buyer's POS customer at intake reads and writes Customers for real, which these doubles
	# cannot answer; it is tested on the real database in test_web_order_customer.
	_INTAKE.start()


def tearDownModule():
	_INTAKE.stop()

ITEM = "COMMIT-ITEM-001"
MAPPING = "ffl_woo_sync.woocommerce.mapping"
WEBHOOK = "ffl_woo_sync.woocommerce.webhook"


class TheFreePoolIsTheSharedOne(FrappeTestCase):
	def test_free_serials_is_the_core_pool(self):
		with patch.object(mapping, "uniform_free_serials",
				return_value=["SN-1", "SN-2"]) as pool:
			self.assertEqual(mapping.free_serials(ITEM), ["SN-1", "SN-2"])
		pool.assert_called_once_with(ITEM)

	def test_free_units_counts_that_same_list(self):
		with patch.object(mapping, "uniform_free_serials",
				return_value=["SN-1", "SN-2", "SN-3"]):
			self.assertEqual(mapping.free_units(ITEM), 3)

	def test_the_old_names_are_still_there_for_the_tests_that_patch_them(self):
		self.assertTrue(callable(mapping.free_serials))
		self.assertTrue(callable(mapping.free_units))


class TheWebRegistrantOfCommittedUnits(FrappeTestCase):
	def _units(self, row, *, claims=0, lines=0):
		with patch("frappe.db.get_value", return_value=row), \
				patch.object(mapping, "unassigned_web_claims", return_value=claims), \
				patch.object(mapping, "pending_web_line_qty", return_value=lines):
			return mapping.channel_committed_units(ITEM)

	def test_it_labels_itself_as_the_web_store(self):
		self.assertEqual(mapping.channel_committed_units.channel, "woo")

	def test_a_uniform_gun_owes_its_unassigned_claims(self):
		row = {"is_firearm": 1, "item_group": "Firearm", "uniform_sell_price": 1}
		self.assertEqual(self._units(row, claims=3, lines=9), 3)

	def test_an_accessory_owes_its_open_web_lines(self):
		row = {"is_firearm": 0, "item_group": "Ammo", "uniform_sell_price": 0}
		self.assertEqual(self._units(row, claims=9, lines=4), 4)

	def test_a_per_serial_gun_owes_nothing(self):
		"""Its availability is already spent through the serial it reserves."""
		row = {"is_firearm": 1, "item_group": "Firearm", "uniform_sell_price": 0}
		self.assertEqual(self._units(row, claims=9, lines=9), 0)

	def test_a_missing_item_owes_nothing(self):
		self.assertEqual(self._units(None, claims=9, lines=9), 0)


class TheStorefrontCountSubtractsTheOtherChannels(FrappeTestCase):
	"""``local_stock_for_item`` — every writer's quantity comes out of it."""

	def _stock(self, row, *, others, free=None, claims=0, bin_qty=0, web_lines=0):
		seen = {}

		def _committed(item_code, *, exclude=None):
			seen["exclude"] = exclude
			return others

		with patch.object(mapping, "committed_units", side_effect=_committed), \
				patch.object(mapping, "unassigned_web_claims", return_value=claims), \
				patch.object(mapping, "pending_web_line_qty", return_value=web_lines), \
				patch.object(mapping, "free_units", return_value=free or 0), \
				patch.object(mapping, "get_item_stock",
					return_value={ITEM: bin_qty}), \
				patch.object(mapping, "custody_warehouse_qty", return_value=0):
			qty = mapping.local_stock_for_item(ITEM, item_row=row)
		return qty, seen["exclude"]

	GUN = {"is_firearm": 1, "item_group": "Firearm", "uniform_sell_price": 1}
	AMMO = {"is_firearm": 0, "item_group": "Ammo", "uniform_sell_price": 0}

	def test_a_uniform_gun_loses_gunbroker_claims_and_its_own(self):
		qty, exclude = self._stock(self.GUN, free=6, claims=1,
			others={"gunbroker": 2})
		self.assertEqual(qty, 3)
		self.assertEqual(exclude, "woo", "the web store's own claims are counted once")

	def test_ammunition_loses_gunbroker_orders_and_its_own_lines(self):
		qty, exclude = self._stock(self.AMMO, bin_qty=10, web_lines=3,
			others={"gunbroker": 2})
		self.assertEqual(qty, 5)
		self.assertEqual(exclude, "woo")

	def test_an_oversold_shelf_publishes_zero(self):
		self.assertEqual(self._stock(self.GUN, free=1, claims=1,
			others={"gunbroker": 2})[0], 0)
		self.assertEqual(self._stock(self.AMMO, bin_qty=2, web_lines=1,
			others={"gunbroker": 5})[0], 0)

	def test_no_other_channel_changes_nothing(self):
		self.assertEqual(self._stock(self.GUN, free=4, claims=1, others={})[0], 3)
		self.assertEqual(self._stock(self.AMMO, bin_qty=7, web_lines=2, others={})[0], 5)

	def test_a_channel_that_cannot_answer_stops_the_push_instead_of_raising_the_count(self):
		"""The exception reaches the push task, which absorbs and reports it —
		so WooCommerce keeps the last number it was given."""
		with patch.object(mapping, "committed_units",
				side_effect=RuntimeError("GunBroker tables not migrated")), \
				patch.object(mapping, "unassigned_web_claims", return_value=0), \
				patch.object(mapping, "free_units", return_value=5):
			with self.assertRaises(RuntimeError):
				mapping.local_stock_for_item(ITEM, item_row=self.GUN)

	def test_the_push_task_sends_nothing_when_the_count_cannot_be_read(self):
		from ffl_woo_sync.woocommerce import tasks

		with patch("ffl_woo_sync.woocommerce.sync.push_stock",
				side_effect=RuntimeError("count unreadable")) as push, \
				patch.object(tasks, "report_stock_push_failure") as reported:
			tasks.push_stock_task(ITEM)
		push.assert_called_once()
		self.assertTrue(reported.called)


class TheWebHandlerOfCommitmentsChanged(FrappeTestCase):
	def _heard(self, source, *, listed_sites=("retail",)):
		def _get_value(doctype, name=None, fieldname=None, *a, **kw):
			site = (fieldname or "").split("_")[0]
			return "WC-1" if any(fieldname == mapping_f(s) for s in listed_sites) else ""

		def mapping_f(site):
			return channel._f(site, "product_id")

		with patch("frappe.db.get_value", side_effect=_get_value), \
				patch("frappe.enqueue") as enq:
			channel.channel_commitments_changed(ITEM, source=source)
		return [(c.args[0].rsplit(".", 1)[-1], c.kwargs.get("site"))
			for c in enq.call_args_list]

	def test_another_channels_change_re_pushes_the_listed_store(self):
		self.assertEqual(self._heard("gunbroker"), [("push_stock_task", "retail")])

	def test_its_own_change_is_not_echoed(self):
		"""The checkout and cancel paths already push what they changed."""
		self.assertEqual(self._heard("woo"), [])

	def test_an_unlisted_item_enqueues_nothing(self):
		self.assertEqual(self._heard("gunbroker", listed_sites=()), [])


class TheWebStoreAnnouncesItsOwnCommitments(FrappeTestCase):
	"""Checkout and cancellation, against a real order where possible."""

	def test_the_registrations_are_in_hooks(self):
		from ffl_woo_sync import hooks

		self.assertIn("ffl_woo_sync.woocommerce.mapping.channel_committed_units",
			hooks.channel_committed_units)
		self.assertIn("ffl_woo_sync.woocommerce.channel.channel_commitments_changed",
			hooks.channel_commitments_changed)
		for path in (*hooks.channel_committed_units, *hooks.channel_commitments_changed):
			self.assertTrue(callable(frappe.get_attr(path)), path)

	def test_the_items_an_order_commits_skip_the_suppliers_lanes(self):
		order = frappe._dict(
			lines=[
				frappe._dict(item_code="A-LOCAL", lane="local"),
				frappe._dict(item_code="B-BLANK", lane=""),
				frappe._dict(item_code="C-DIRECT", lane="direct"),
				frappe._dict(item_code="D-FDS", lane="fds"),
				frappe._dict(item_code="E-STORE", lane="to_store"),
				frappe._dict(item_code=" ", lane="local"),
			],
			serials=[frappe._dict(item_code="F-CLAIM", fulfillment="local",
				confirmed_serial="", stock_hold_serial="")])
		self.assertEqual(webhook._committed_items(order),
			{"A-LOCAL", "B-BLANK", "E-STORE", "F-CLAIM"})

	def test_checkout_notifies_every_committed_item(self):
		with patch(f"{WEBHOOK}.notify_channel_commitments_changed") as notify:
			webhook._announce_commitments({"B", "A", ""})
		self.assertEqual([(c.args[0], c.kwargs["source"]) for c in notify.call_args_list],
			[("A", "woo"), ("B", "woo")])


class TheCheckoutPathAnnounces(FrappeTestCase):
	"""``_handle_order_created`` end to end, with the doubles the webhook suite uses."""

	def _checkout(self, line_items, **patches):
		from ffl_woo_sync.tests.test_webhook import (
			_order_body,
			_patch_db_exists,
			_patch_new_doc,
		)

		patcher_new, _doc = _patch_new_doc()
		with _patch_db_exists(return_value=None), patcher_new, \
				patch(f"{WEBHOOK}.is_firearm_item", return_value=patches.get("gun", False)), \
				patch(f"{WEBHOOK}.is_regulated_firearm_item",
					return_value=patches.get("gun", False)), \
				patch(f"{WEBHOOK}._is_uniform_price_item", return_value=True), \
				patch(f"{WEBHOOK}._free_units_for_claim", return_value=3), \
				patch("frappe.db.get_value", return_value={
					"is_firearm": int(patches.get("gun", False)),
					"uniform_sell_price": int(patches.get("gun", False)),
					"woo_product_id": "WC-1"}), \
				patch("frappe.db.set_value"), patch("frappe.enqueue"), \
				patch(f"{WEBHOOK}.notify_channel_commitments_changed") as notify:
			webhook._handle_order_created(_order_body(line_items=line_items))
		return sorted((c.args[0], c.kwargs["source"]) for c in notify.call_args_list)

	def test_an_accessory_line_is_announced(self):
		heard = self._checkout([
			{"id": 1, "sku": "AMMO-001", "quantity": 2, "price": "25.00"}])
		self.assertEqual(heard, [("AMMO-001", "woo")])

	def test_a_claimed_unit_of_a_uniform_gun_is_announced(self):
		heard = self._checkout(
			[{"id": 1, "sku": "ITEM-001", "quantity": 1, "price": "500.00"}], gun=True)
		self.assertEqual(heard, [("ITEM-001", "woo")])


class TheCancelPathAnnounces(FrappeTestCase):
	"""``_handle_order_cancelled`` against a REAL order, so the SQL that finds the
	order's items runs against the database rather than being mocked away."""

	def setUp(self):
		super().setUp()
		_ensure_items()

	def _order(self, *, serial_rows=(), lines=()):
		doc = frappe.get_doc({
			"doctype": "Woo Online Order",
			"woo_order_id": f"CC-{frappe.generate_hash(length=8)}",
			"order_status": "Pending Fulfillment", "site": "retail",
			"customer_name": "QA Commit",
			"serials": [{
				"item_code": UNIFORM, "woo_line_id": "1", "unit_index": 0,
				"confirmed_serial": "", "stock_hold_serial": "",
				"fulfillment": "local"} for _ in serial_rows],
			"lines": [{"item_code": code, "qty": qty, "lane": lane}
				for code, qty, lane in lines],
		})
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc

	def _cancel(self, order):
		with patch("ffl_woo_sync.woocommerce.refunds.alert_if_invoiced"), \
				patch(f"{WEBHOOK}._log_error"), \
				patch(f"{WEBHOOK}._push_item_stock_everywhere"), \
				patch("frappe.enqueue"), \
				patch(f"{WEBHOOK}.notify_channel_commitments_changed") as notify:
			webhook._handle_order_cancelled(
				{"id": str(order.woo_order_id), "status": "cancelled"})
		return sorted((c.args[0], c.kwargs["source"]) for c in notify.call_args_list)

	def test_a_cancelled_claim_is_announced(self):
		order = self._order(serial_rows=[1])
		self.assertEqual(self._cancel(order), [(UNIFORM, "woo")])

	def test_a_cancelled_accessory_line_is_announced(self):
		order = self._order(lines=[(OTHER, 2, "local")])
		self.assertEqual(self._cancel(order), [(OTHER, "woo")])

	def test_a_drop_shipped_line_never_held_the_shelf_so_nothing_is_announced(self):
		order = self._order(lines=[(OTHER, 2, "direct"), (OTHER, 1, "fds")])
		self.assertEqual(self._cancel(order), [])

	def test_an_order_with_both_announces_each_item_once(self):
		order = self._order(serial_rows=[1, 2], lines=[(UNIFORM, 2, "local"),
			(OTHER, 3, "")])
		self.assertEqual(self._cancel(order),
			[(OTHER, "woo"), (UNIFORM, "woo")])


class OrderIntakeSurvivesAChannelThatCannotCount(FrappeTestCase):
	"""stock_for_item lets a raising registrant propagate (a push must keep its last
	number); order intake must not, or a paid order is lost behind a 200."""

	def test_a_raising_channel_reads_as_nothing_free_not_an_exception(self):
		with patch.object(mapping, "stock_for_item", side_effect=RuntimeError("gb down")), \
				patch.object(frappe, "log_error") as logged:
			self.assertEqual(webhook._free_units_for_claim(UNIFORM), 0)
		logged.assert_called_once()
