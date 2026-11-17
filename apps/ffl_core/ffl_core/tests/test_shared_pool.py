"""The shared pool of a uniform-price firearm: one free-gun list, one committed-units
sum, one counter refusal — for every sales channel at once.

What each class pins, and why it is a separate class:

* ``UniformFreeSerialsMatchTheOldWebPool`` — the pool moved here from the web app
  (``mapping.free_serials``). It must answer EXACTLY what the old one did on the
  same guns, plus the two deliberate changes: a gun flagged "possibly sold on
  GunBroker" leaves the pool, and the list is oldest-first. Driven against real
  Serial No rows, because the equivalence that matters is the database's.
* ``CommittedUnits`` / ``SellableUnits`` — the hook contract (labelled
  registrants, ``exclude``, a raising registrant propagates).
* ``ClaimBlockReason`` — the counter's refusal, per-channel breakdown.
* ``Notifier`` — never raises.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core import reservations
from ffl_core.reservations import (
	CHANNEL_COMMITMENTS_CHANGED_HOOK,
	CHANNEL_COMMITTED_UNITS_HOOK,
	CHANNEL_GUNBROKER,
	CHANNEL_WOO,
	channel_claim_block_reason,
	channel_reserved_serials,
	committed_units,
	counter_holds,
	notify_channel_commitments_changed,
	uniform_free_serials,
	uniform_sellable_units,
	web_claim_block_reason,
)

HERE = "ffl_core.tests.test_shared_pool"
ITEM = "POOL-UNIFORM-GUN"


# --- fake registrants (resolved by dotted path, as frappe.get_attr does) ---------
def woo_owes_two(item_code):
	return 2


woo_owes_two.channel = CHANNEL_WOO


def gb_owes_one(item_code):
	return 1


gb_owes_one.channel = CHANNEL_GUNBROKER


def unlabelled_owes_three(item_code):
	return 3


def negative_owes(item_code):
	return -4


negative_owes.channel = "negative"


def registrant_boom(item_code):
	raise RuntimeError("channel unreachable")


registrant_boom.channel = "boom"

_HEARD: list = []


def handler_a(item_code, *, source):
	_HEARD.append(("a", item_code, source))


def handler_boom(item_code, *, source):
	_HEARD.append(("boom", item_code, source))
	raise RuntimeError("handler broke")


def handler_c(item_code, *, source):
	_HEARD.append(("c", item_code, source))


class _HookCase(FrappeTestCase):
	def _hooks(self, mapping: dict):
		"""Answer the hooks under test; every other key reaches the real hooks."""
		real = frappe.get_hooks

		def _fake(hook=None, default="_KEEP_", app_name=None):
			if hook in mapping:
				return list(mapping[hook])
			# frappe's own sentinel survives: re-declaring `default=None` makes
			# every unregistered hook answer None instead of [] (docs/testing.md).
			if default == "_KEEP_":
				return real(hook, app_name=app_name)
			return real(hook, default, app_name)

		patcher = patch.object(frappe, "get_hooks", side_effect=_fake)
		patcher.start()
		self.addCleanup(patcher.stop)


class UniformFreeSerialsMatchTheOldWebPool(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		frappe.delete_doc_if_exists("Item", ITEM, force=1)
		frappe.get_doc({
			"doctype": "Item", "item_code": ITEM, "item_name": ITEM,
			"item_group": "All Item Groups", "stock_uom": "Nos",
			"is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
			"uniform_sell_price": 1, "standard_rate": 500,
		}).insert(ignore_permissions=True)

	def setUp(self):
		super().setUp()
		# The class rolls back once, at the end: start every test from an empty
		# shelf so one test's guns are not another's pool. In-transaction, no commit.
		frappe.db.sql("DELETE FROM `tabSerial No` WHERE item_code = %s", (ITEM,))

	def _serial(self, name, *, created=None, **fields):
		"""A real Serial No, Active unless a field says otherwise. Fields another
		app owns are applied only where that app's column exists."""
		frappe.delete_doc_if_exists("Serial No", name, force=1)
		frappe.get_doc({
			"doctype": "Serial No", "serial_no": name, "item_code": ITEM,
		}).insert(ignore_permissions=True)
		values = {"status": "Active", **fields}
		for field in list(values):
			if not frappe.db.has_column("Serial No", field):
				self.skipTest(f"Serial No.{field} is not installed on this bench")
		frappe.db.set_value("Serial No", name, values, update_modified=False)
		if created:
			frappe.db.set_value("Serial No", name, "creation", created,
				update_modified=False)
		return name

	def _legacy_free_serials(self):
		"""The web app's ``mapping.free_serials`` as it stood before the move —
		the reference this test holds the new function to."""
		held = counter_holds(ITEM)
		channel_held = channel_reserved_serials(ITEM)
		active = frappe.get_all(
			"Serial No",
			filters={"item_code": ITEM, "status": "Active", "woo_reserved": 0,
				"consigned_out": 0, "is_transfer_gun": 0},
			pluck="name")
		in_custody = reservations.custody_serials(ITEM)
		return [s for s in active
			if s not in held and s not in in_custody and s not in channel_held]

	def test_the_same_guns_as_the_old_pool_on_a_mixed_shelf(self):
		self._serial("POOL-FREE-1")
		self._serial("POOL-FREE-2")
		self._serial("POOL-WOO", woo_reserved=1, woo_order="WOO-1")
		self._serial("POOL-GB", gb_reserved=1, gb_order="GB-1")
		self._serial("POOL-CONSIGNED", consigned_out=1)
		self._serial("POOL-TRANSFER", is_transfer_gun=1)
		self._serial("POOL-SOLD", status="Delivered")
		self.assertEqual(
			sorted(uniform_free_serials(ITEM)), sorted(self._legacy_free_serials()))
		self.assertEqual(sorted(uniform_free_serials(ITEM)),
			["POOL-FREE-1", "POOL-FREE-2"])

	def test_a_gun_possibly_sold_on_gunbroker_leaves_the_pool(self):
		"""The one deliberate narrowing: the old pool still counted it."""
		self._serial("POOL-OK")
		self._serial("POOL-UNKNOWN", gb_sold_unknown=1)
		self.assertIn("POOL-UNKNOWN", self._legacy_free_serials())
		self.assertEqual(uniform_free_serials(ITEM), ["POOL-OK"])

	def test_oldest_gun_first_whatever_its_name(self):
		self._serial("POOL-ZZZ", created=datetime(2020, 1, 1))
		self._serial("POOL-AAA", created=datetime(2020, 3, 1))
		self._serial("POOL-MMM", created=datetime(2020, 2, 1))
		self.assertEqual(
			uniform_free_serials(ITEM), ["POOL-ZZZ", "POOL-MMM", "POOL-AAA"])

	def test_a_counter_hold_removes_the_gun_unless_it_is_the_callers_own(self):
		self._serial("POOL-H1", created=datetime(2020, 1, 1))
		self._serial("POOL-H2", created=datetime(2020, 1, 2))
		holds = {"POOL-H1": {"source": "pos", "ref": "POS-INV-1", "kind": "pos"}}
		with patch.object(reservations, "counter_holds", return_value=holds):
			self.assertEqual(uniform_free_serials(ITEM), ["POOL-H2"])
			self.assertEqual(
				uniform_free_serials(ITEM, ignore_hold_ref="POS-INV-1"),
				["POOL-H1", "POOL-H2"])
			# Someone else's invoice stays a hold.
			self.assertEqual(
				uniform_free_serials(ITEM, ignore_hold_ref="POS-INV-2"), ["POOL-H2"])

	def test_a_gun_in_a_custody_warehouse_is_not_free(self):
		self._serial("POOL-C1")
		self._serial("POOL-C2")
		with patch.object(reservations, "custody_serials", return_value={"POOL-C1"}):
			self.assertEqual(uniform_free_serials(ITEM), ["POOL-C2"])


class CommittedUnits(_HookCase):
	def _register(self, *fns):
		self._hooks({CHANNEL_COMMITTED_UNITS_HOOK: [f"{HERE}.{f.__name__}" for f in fns]})

	def test_one_entry_per_channel(self):
		self._register(woo_owes_two, gb_owes_one)
		self.assertEqual(committed_units(ITEM),
			{CHANNEL_WOO: 2, CHANNEL_GUNBROKER: 1})

	def test_exclude_leaves_the_asking_channel_out(self):
		self._register(woo_owes_two, gb_owes_one)
		self.assertEqual(committed_units(ITEM, exclude=CHANNEL_WOO),
			{CHANNEL_GUNBROKER: 1})

	def test_an_unlabelled_registrant_still_counts_under_its_path(self):
		self._register(unlabelled_owes_three)
		self.assertEqual(committed_units(ITEM),
			{f"{HERE}.unlabelled_owes_three": 3})

	def test_a_negative_answer_counts_as_nothing(self):
		self._register(negative_owes)
		self.assertEqual(committed_units(ITEM), {"negative": 0})

	def test_no_registrant_means_nothing_is_owed(self):
		"""A POS with one channel app (or none) is a legitimate deployment."""
		self._register()
		self.assertEqual(committed_units(ITEM), {})

	def test_a_raising_registrant_propagates_instead_of_reading_as_zero(self):
		"""A missing channel reading as 'owes nothing' is an overstated count."""
		self._register(woo_owes_two, registrant_boom)
		with self.assertRaises(RuntimeError):
			committed_units(ITEM)


class SellableUnits(_HookCase):
	def test_free_guns_less_every_channels_commitments(self):
		self._hooks({CHANNEL_COMMITTED_UNITS_HOOK: [
			f"{HERE}.woo_owes_two", f"{HERE}.gb_owes_one"]})
		with patch.object(reservations, "uniform_free_serials",
				return_value=["A", "B", "C", "D", "E"]):
			self.assertEqual(uniform_sellable_units(ITEM), 2)

	def test_an_oversold_shelf_is_zero_not_negative(self):
		self._hooks({CHANNEL_COMMITTED_UNITS_HOOK: [
			f"{HERE}.woo_owes_two", f"{HERE}.gb_owes_one"]})
		with patch.object(reservations, "uniform_free_serials", return_value=["A"]):
			self.assertEqual(uniform_sellable_units(ITEM), 0)

	def test_a_raising_registrant_raises_so_a_writer_can_skip(self):
		self._hooks({CHANNEL_COMMITTED_UNITS_HOOK: [f"{HERE}.registrant_boom"]})
		with patch.object(reservations, "uniform_free_serials",
				return_value=["A", "B"]), self.assertRaises(RuntimeError):
			uniform_sellable_units(ITEM)


class ClaimBlockReason(_HookCase):
	"""The counter's refusal (scan-to-add and the POS Invoice submit gate)."""

	def setUp(self):
		super().setUp()
		self._item_row = frappe._dict(
			is_firearm=1, item_group="Firearm", uniform_sell_price=1)
		p = patch("frappe.db.get_value", side_effect=self._get_value)
		p.start()
		self.addCleanup(p.stop)

	def _get_value(self, doctype, name=None, fieldname=None, *a, **kw):
		if doctype == "Item" and isinstance(fieldname, list) \
				and "uniform_sell_price" in fieldname:
			return self._item_row
		return None

	def _reason(self, *, web=0, gb=0, free=0, **kwargs):
		def _web(item_code):
			return web

		def _gunbroker(item_code):
			return gb

		_web.channel = CHANNEL_WOO
		_gunbroker.channel = CHANNEL_GUNBROKER
		registrants = {f"{HERE}._reg_web": _web, f"{HERE}._reg_gb": _gunbroker}
		self._hooks({CHANNEL_COMMITTED_UNITS_HOOK: list(registrants)})
		with patch("frappe.get_attr", side_effect=lambda path: registrants[path]), \
				patch.object(reservations, "uniform_free_serials",
					return_value=[f"S{i}" for i in range(free)]) as pool:
			reason = channel_claim_block_reason(ITEM, **kwargs)
		self.pool_call = pool.call_args
		return reason

	def test_every_free_gun_already_owed_blocks_and_lists_each_channel(self):
		reason = self._reason(web=1, gb=1, free=2)
		self.assertIsNotNone(reason)
		self.assertIn(ITEM, reason)
		self.assertIn("web: 1", reason)
		self.assertIn("GunBroker: 1", reason)

	def test_the_old_web_only_rule_is_unchanged(self):
		self.assertIsNotNone(self._reason(web=2, free=2))
		self.assertIsNotNone(self._reason(web=3, free=2))
		self.assertIsNone(self._reason(web=2, free=3))

	def test_a_gunbroker_claim_blocks_the_same_way(self):
		self.assertIsNotNone(self._reason(gb=1, free=1))
		self.assertIsNone(self._reason(gb=1, free=2))

	def test_nothing_owed_never_blocks(self):
		self.assertIsNone(self._reason(free=0))
		self.assertIsNone(self._reason(free=5))

	def test_selling_several_guns_needs_that_many_spare(self):
		self.assertIsNone(self._reason(web=1, free=3, selling=2))
		self.assertIsNotNone(self._reason(web=1, free=2, selling=2))

	def test_the_callers_own_hold_is_passed_to_the_pool(self):
		self._reason(web=1, free=2, ignore_hold_ref="POS-INV-9")
		self.assertEqual(self.pool_call.kwargs["ignore_hold_ref"], "POS-INV-9")

	def test_an_item_that_cannot_carry_a_claim_never_blocks(self):
		for row in (None, frappe._dict(is_firearm=0, item_group="Ammo",
				uniform_sell_price=0),
				frappe._dict(is_firearm=1, item_group="Firearm", uniform_sell_price=0)):
			self._item_row = row
			self.assertIsNone(self._reason(web=5, free=0))

	def test_a_channel_that_cannot_answer_fails_open_and_is_logged(self):
		"""A counter sale cannot depend on a marketplace app being healthy."""
		self._hooks({CHANNEL_COMMITTED_UNITS_HOOK: [f"{HERE}.registrant_boom"]})
		with patch.object(frappe, "log_error") as logged:
			self.assertIsNone(channel_claim_block_reason(ITEM))
		self.assertTrue(logged.called)

	def test_the_old_name_is_the_same_function(self):
		self.assertIs(web_claim_block_reason, channel_claim_block_reason)


class Notifier(_HookCase):
	def setUp(self):
		super().setUp()
		_HEARD.clear()

	def test_every_handler_hears_it_with_the_source(self):
		self._hooks({CHANNEL_COMMITMENTS_CHANGED_HOOK: [
			f"{HERE}.handler_a", f"{HERE}.handler_c"]})
		notify_channel_commitments_changed(ITEM, source=CHANNEL_GUNBROKER)
		self.assertEqual(_HEARD, [("a", ITEM, "gunbroker"), ("c", ITEM, "gunbroker")])

	def test_a_raising_handler_never_reaches_the_caller_or_stops_the_rest(self):
		self._hooks({CHANNEL_COMMITMENTS_CHANGED_HOOK: [
			f"{HERE}.handler_boom", f"{HERE}.handler_c"]})
		with patch.object(frappe, "log_error") as logged:
			notify_channel_commitments_changed(ITEM, source=CHANNEL_WOO)
		self.assertEqual([h[0] for h in _HEARD], ["boom", "c"])
		self.assertTrue(logged.called)

	def test_a_blank_item_is_ignored(self):
		self._hooks({CHANNEL_COMMITMENTS_CHANGED_HOOK: [f"{HERE}.handler_a"]})
		notify_channel_commitments_changed("  ", source=CHANNEL_WOO)
		self.assertEqual(_HEARD, [])

	def test_a_broken_hook_table_never_raises(self):
		with patch.object(frappe, "get_hooks", side_effect=RuntimeError("no hooks")):
			notify_channel_commitments_changed(ITEM, source=CHANNEL_WOO)


class LockItemReservations(FrappeTestCase):
	def test_it_takes_the_item_row_lock_and_reads_nothing_else(self):
		with patch("frappe.db.sql") as sql:
			reservations.lock_item_reservations(ITEM)
		query, params = sql.call_args.args
		self.assertIn("FOR UPDATE", query)
		self.assertIn("`tabItem`", query)
		self.assertEqual(params, (ITEM,))

	def test_a_blank_item_locks_nothing(self):
		with patch("frappe.db.sql") as sql:
			reservations.lock_item_reservations("")
		sql.assert_not_called()


class TheInstalledChannelsAreWired(FrappeTestCase):
	"""The live hook table, not fakes: every installed channel app answers, under
	its own distinct label — the property `exclude` and the per-channel refusal
	rest on."""

	def test_each_registrant_resolves_and_carries_a_distinct_label(self):
		paths = frappe.get_hooks(CHANNEL_COMMITTED_UNITS_HOOK)
		labels = []
		for path in paths:
			fn = frappe.get_attr(path)
			self.assertTrue(callable(fn), path)
			self.assertTrue(getattr(fn, "channel", ""), f"{path} has no channel label")
			labels.append(fn.channel)
		self.assertEqual(len(labels), len(set(labels)), labels)

	def test_every_installed_channel_app_answers_for_an_item(self):
		import importlib.util

		expected = {name for name, app in (
			(CHANNEL_WOO, "ffl_woo_sync"), (CHANNEL_GUNBROKER, "ffl_integrations"))
			if importlib.util.find_spec(app)}
		if not expected:
			self.skipTest("no channel app is installed on this bench")
		# Core answers too: the units put by for its own pre-orders (ffl_core.preorder).
		self.assertEqual(set(committed_units("NO-SUCH-ITEM-POOL")), expected | {"preorder"})

	def test_every_change_handler_resolves(self):
		for path in frappe.get_hooks(CHANNEL_COMMITMENTS_CHANGED_HOOK):
			self.assertTrue(callable(frappe.get_attr(path)), path)
