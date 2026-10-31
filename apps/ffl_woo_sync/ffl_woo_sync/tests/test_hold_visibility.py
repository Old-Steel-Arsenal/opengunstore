"""Holds that one side of the system takes and the other cannot see.

Four issues with one shape between them: something is holding a rifle, and the
code that decides whether to hand that rifle to somebody else is looking at a
different register.

* **Picker** — `_pick_oldest_unreserved_serial` had no counter-hold rule at all, so
  it could park a web order on a rifle a cashier was mid-way through selling.
* **Web claim** — a uniform-price web order holds an unassigned CLAIM: it owes a unit
  and reserves no `Serial No`. Every counter gate tests `woo_reserved`, which a
  claim never sets, so the counter could drain a pool the storefront had already
  promised away.
* **Sibling row** — `_apply_dealer_serial` released the row's previous hold whenever it
  differed from the serial being applied, without asking whether a SIBLING row of
  the same order still named it. A sold rifle went back on sale.
* **Shipped, undisposed** — WooCommerce says shipped, POS says Pending Fulfillment: a firearm
  out of the building with no A&D entry, and nothing said so.

The through-line for the first two is `reservations.counter_holds` vs the
whitelisted `reserved_counter_serials`. The gated one answers `{}` to a caller
without the shipping role — the webhook runs as Guest — and `{}` reads as
"nothing is held". A display function may fail closed like that; a gate may not.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.channel_query_doubles import get_all_double, warm_channel_meta

from ffl_woo_sync.tests.test_serial_exit_delist import ITEM, SerialExitFixtures
from ffl_woo_sync.woocommerce import mapping

WEBHOOK = "ffl_woo_sync.woocommerce.webhook"
MAPPING = "ffl_woo_sync.woocommerce.mapping"
TASKS = "ffl_woo_sync.woocommerce.tasks"


def _srow(name="row1", confirmed_serial="", stock_hold_serial="",
		item_code="ITEM-1"):
	return frappe._dict({"name": name, "item_code": item_code,
		"confirmed_serial": confirmed_serial,
		"stock_hold_serial": stock_hold_serial,
		"woo_line_id": "1", "unit_index": 0, "reservation_error": ""})


def _order(rows, name="WOO-ORD-1"):
	doc = MagicMock()
	doc.name = name
	doc.woo_order_id = "9001"
	doc.get = lambda key, default=None: {"serials": rows}.get(key, default)
	return doc


# ---------------------------------------------------------------------------
# The picker must see counter holds
# ---------------------------------------------------------------------------

class TestPickerHonoursCounterHolds(FrappeTestCase):
	def _pick(self, *, held, candidates, channel_held=None):
		"""`channel_held` defaults to nothing, and that is the point of using a
		double here rather than a blanket answer.

		The picker excludes counter holds AND channel reservations (UD-3). With
		`patch("frappe.get_all", return_value=candidates)` the candidate list was
		handed to the channel-reservation SELECT as well, so every candidate came
		back looking like a gun GunBroker had sold and the exclusion list grew by
		one. That reads exactly like the intended new behaviour and is not it —
		nothing here sets `gb_reserved`. Channel exclusion has its own coverage
		in `test_channel_exclusion`.
		"""
		from ffl_woo_sync.woocommerce.webhook import _pick_oldest_unreserved_serial
		warm_channel_meta()
		with patch("ffl_core.reservations.counter_holds", return_value=held), \
			 patch(f"{WEBHOOK}.custody_warehouse_names", return_value=set()), \
			 patch("frappe.get_all",
				 side_effect=get_all_double(candidates, held=channel_held)) as get_all, \
			 patch("frappe.db.get_value", return_value={
				 "woo_reserved": 0, "status": "Active", "is_transfer_gun": 0}):
			picked = _pick_oldest_unreserved_serial("ITEM-1")
		return picked, get_all

	def test_a_gun_on_an_open_counter_order_is_never_picked(self):
		"""It could park a web order on a rifle the cashier is ringing up."""
		_, get_all = self._pick(
			held={"A": {"kind": "pos"}},
			candidates=[{"name": "B", "warehouse": "Stores - TSA"}])
		self.assertEqual(get_all.call_args[1]["filters"]["name"], ("not in", ["A"]))

	def test_holds_are_excluded_in_sql_not_after_the_fetch(self):
		"""The row limit exists to keep the lock phase cheap. Filtering after it
		spends the budget on rows already known unusable — five held guns at the
		front would answer 'no stock' with free rifles behind them."""
		_, get_all = self._pick(
			held={"A": {}, "B": {}},
			candidates=[{"name": "C", "warehouse": None}])
		self.assertEqual(
			sorted(get_all.call_args[1]["filters"]["name"][1]), ["A", "B"])

	def test_no_holds_adds_no_filter(self):
		"""An empty `not in` is a query trap; the ordinary case must stay clean."""
		_, get_all = self._pick(held={}, candidates=[{"name": "A", "warehouse": None}])
		self.assertNotIn("name", get_all.call_args[1]["filters"])

	def test_it_reads_the_ungated_helper(self):
		"""`reserved_counter_serials` answers a Guest with a silent {} — and this
		runs in the Guest webhook request, where {} reads as 'nothing is held'.

		Matches the CALL, not the mention: the function's comment names the gated
		one to explain why it is not used, and a substring test reads that as
		usage."""
		import inspect
		import re

		from ffl_woo_sync.woocommerce import webhook
		src = inspect.getsource(webhook._pick_oldest_unreserved_serial)
		self.assertRegex(src, r"\bcounter_holds\s*\(")
		self.assertIsNone(re.search(r"\breserved_counter_serials\s*\(", src))


# ---------------------------------------------------------------------------
# A sibling row still naming the serial
# ---------------------------------------------------------------------------

class TestSiblingRowKeepsItsHold(FrappeTestCase):
	def _apply(self, order, row, serial):
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		with patch("frappe.db.get_value", return_value={
				"item_code": "ITEM-1", "status": "Active",
				"woo_reserved": 0, "woo_order": ""}), \
			 patch(f"{WEBHOOK}._sells_through", return_value=""), \
			 patch(f"{WEBHOOK}._reserve_serial"), \
			 patch(f"{WEBHOOK}._release_serial") as release, \
			 patch(f"{WEBHOOK}._enqueue_serial_or_item_push"):
			_apply_dealer_serial(order, row, serial)
		return release

	def test_a_gun_another_row_confirmed_is_not_released(self):
		"""Two-unit order, staff type the rifles in physical order. Row 1's serial
		differs from row 0's hold, so the old rule released row 0's gun — a rifle
		already confirmed on a PAID order went back into the free pool and past
		the counter's woo_reserved gate, for as long as the FFL took to chase."""
		row0 = _srow(name="r0", confirmed_serial="A")
		row1 = _srow(name="r1", stock_hold_serial="A")
		release = self._apply(_order([row0, row1]), row1, "B")
		release.assert_not_called()

	def test_a_gun_another_row_merely_holds_is_not_released_either(self):
		"""A sibling's placeholder is still that sibling holding the rifle."""
		row0 = _srow(name="r0", stock_hold_serial="A")
		row1 = _srow(name="r1", stock_hold_serial="A")
		release = self._apply(_order([row0, row1]), row1, "B")
		release.assert_not_called()

	def test_a_gun_no_other_row_names_is_still_released(self):
		"""The ordinary correction: staff fix a typo, the old rifle goes back on
		sale. Nothing about the sibling-row rule may weaken that."""
		row0 = _srow(name="r0", confirmed_serial="C")
		row1 = _srow(name="r1", stock_hold_serial="A")
		release = self._apply(_order([row0, row1]), row1, "B")
		release.assert_called_once()
		self.assertEqual(release.call_args[0][0], "A")

	def test_a_single_row_order_still_releases_its_own_old_hold(self):
		row = _srow(name="r0", stock_hold_serial="A")
		release = self._apply(_order([row]), row, "B")
		release.assert_called_once()

	def test_the_row_does_not_count_as_its_own_sibling(self):
		from ffl_woo_sync.woocommerce.webhook import _named_by_sibling_row
		row = _srow(name="r0", stock_hold_serial="A")
		self.assertFalse(_named_by_sibling_row(_order([row]), row, "A"))


# ---------------------------------------------------------------------------
# Shipped on WooCommerce, never disposed here
# ---------------------------------------------------------------------------

class TestShippedUndisposedAlert(FrappeTestCase):
	def _run(self, rows, *, throttled=False):
		from ffl_woo_sync.woocommerce import tasks
		cache = MagicMock()
		cache.get_value.return_value = 1 if throttled else None
		with patch("frappe.only_for"), \
			 patch("frappe.db.sql", return_value=rows) as sql, \
			 patch("frappe.cache", return_value=cache), \
			 patch("frappe.log_error") as log_error, \
			 patch("frappe.db.set_value") as set_value, \
			 patch("frappe.get_doc") as get_doc, \
			 patch(f"{TASKS}.safe_commit"):
			out = tasks.alert_shipped_undisposed_orders()
		return out, log_error, sql, cache, set_value, get_doc

	def _row(self, name="WOO-ORD-1"):
		return {"name": name, "woo_order_id": "4201",
			"customer_name": "Web Buyer", "modified": "2026-08-14 10:00:00"}

	def test_a_shipped_undisposed_order_is_reported(self):
		out, log_error, _, _, _, _ = self._run([self._row()])
		self.assertEqual(out, {"checked": 1, "reported": 1})
		log_error.assert_called_once()
		self.assertIn("WOO-ORD-1", log_error.call_args[1]["message"])

	def test_a_staff_caller_is_checked_against_the_counter_roles(self):
		"""Only the Administrator path ever ran: `_ROLES` was never imported,
		so the documented manual check raised NameError for counter staff."""
		from ffl_core.api.manual_order import _ROLES
		from ffl_woo_sync.woocommerce import tasks
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		with patch("frappe.only_for") as only_for, \
			 patch("frappe.db.sql", return_value=[]):
			out = tasks.alert_shipped_undisposed_orders()
		only_for.assert_called_once_with(_ROLES)
		self.assertEqual(out, {"checked": 0, "reported": 0})

	def test_a_clean_queue_reports_nothing(self):
		out, log_error, _, _, _, _ = self._run([])
		self.assertEqual(out, {"checked": 0, "reported": 0})
		log_error.assert_not_called()

	def test_the_report_is_throttled_to_once_a_day(self):
		"""The condition is permanent by construction until somebody acts, so an
		unthrottled report is twelve identical Error Logs a day — the exact
		'permanent failure becomes permanent noise' the sibling reporter's
		cut-off exists to prevent."""
		out, log_error, _, _, _, _ = self._run([self._row()], throttled=True)
		log_error.assert_not_called()
		self.assertEqual(out, {"checked": 1, "reported": 0})

	def test_the_gate_is_shut_only_after_the_report_lands(self):
		"""Redis does not roll back and the Error Log insert does. Shutting first
		can lose the report while keeping the gate closed for 24h."""
		_, _, _, cache, _, _ = self._run([self._row()])
		cache.set_value.assert_called_once()

	def test_a_grace_period_keeps_healthy_orders_quiet(self):
		"""Every normal order passes through 'completed but not yet disposed' for
		the moments between the shipment and the Dispose click."""
		_, _, sql, _, _, _ = self._run([])
		self.assertGreater(sql.call_args[0][1]["grace"], 0)

	def test_it_never_writes_anything(self):
		"""Reports only. Which rifle went in the box is precisely what POS does
		not know in this state, and guessing it is the mistake the whole
		serial-handling rework exists to stop — so this asserts the behaviour (no
		writes) rather than grepping the source for the word 'dispose'."""
		_, _, _, _, set_value, get_doc = self._run([self._row()])
		set_value.assert_not_called()
		get_doc.assert_not_called()


# ---------------------------------------------------------------------------
# DISP-2026-02000 — the PER-SERIAL half of the same registry
# ---------------------------------------------------------------------------

class TestSerialPayloadHonoursCounterHolds(FrappeTestCase):
	"""`stock_for_item` has subtracted counter holds since the picker fix. Its sibling
	`serial_to_product_payload` — the OTHER listing path out of the same
	registry — never asked, so a gun parked on a Draft POS Invoice behind a
	pending 4473 went on publishing stock 1. On 2026-08-29 one did; on 2026-09-09
	it sold online, eleven days later, and the disposition push then hit a
	FastBound 400 because the abandoned 4473 still owned the item.
	"""

	ITEM = "HOLD-ITEM"
	SERIAL = "HOLD-SN-1"

	def _payload(self, *, held):
		warm_channel_meta()
		serial = frappe._dict({
			"name": self.SERIAL, "item_code": self.ITEM, "status": "Active",
			"sell_price": 900.0, "item_name": "Colt Python", "description": "",
			"image_gallery": [], "woo_reserved": 0, "woo_sold_archived": 0,
		})
		item = frappe._dict({
			"name": self.ITEM, "item_name": "Colt Python", "description": "",
			"image_gallery": [], "standard_rate": 900.0, "uniform_sell_price": 0,
		})
		docs = {"Serial No": serial, "Item": item}
		with patch("frappe.get_doc", side_effect=lambda dt, _n: docs[dt]), \
			 patch("ffl_core.reservations.counter_holds", return_value=held):
			return mapping.serial_to_product_payload(self.SERIAL)

	def test_a_gun_on_an_open_counter_order_is_not_buyable_online(self):
		payload = self._payload(held={self.SERIAL: {"kind": "pos", "ref": "PSINV-1"}})
		self.assertEqual(payload["stock_quantity"], 0)

	def test_it_stays_published_rather_than_drafting(self):
		"""Our OWN store is holding it, so the page is not a lie and the gun
		comes back when the counter order is cancelled — the same lane a pending
		web order rides (`woo_reserved`), not the marketplace draft."""
		payload = self._payload(held={self.SERIAL: {"kind": "pos"}})
		self.assertEqual(payload["status"], "publish")

	def test_a_hold_on_a_DIFFERENT_serial_leaves_this_one_buyable(self):
		"""`counter_holds` answers per ITEM: a sibling gun being rung up must not
		take this one's listing down."""
		payload = self._payload(held={"SOME-OTHER-GUN": {"kind": "pos"}})
		self.assertEqual(payload["stock_quantity"], 1)

	def test_releasing_the_hold_makes_it_sellable_again(self):
		payload = self._payload(held={})
		self.assertEqual(payload["stock_quantity"], 1)
		self.assertEqual(payload["status"], "publish")

	def test_it_reads_the_ungated_helper(self):
		"""Same rule as the picker above: `reserved_counter_serials` answers a
		caller without the shipping role with a silent {}, and this function runs
		in the Guest webhook request. Matches the CALL, not the mention."""
		import inspect
		import re

		src = inspect.getsource(mapping.serial_to_product_payload)
		self.assertRegex(src, r"\bcounter_holds\s*\(")
		self.assertIsNone(re.search(r"\breserved_counter_serials\s*\(", src))


class TestDraftPosInvoiceReachesTheStorefront(FrappeTestCase):
	"""The hold exists from the first DRAFT save; the only POS Invoice events
	wired to WooCommerce were submit and cancel, which a parked sale fires
	neither of. Eleven days of a buyable gun is what that gap costs."""

	def _doc(self, rows, docstatus=0, before=None, is_return=0):
		doc = MagicMock()
		doc.docstatus = docstatus
		doc.is_consolidated = 0
		doc.is_return = is_return
		doc.get = lambda key, default=None: {"items": rows}.get(key, default)
		doc.get_doc_before_save.return_value = before
		return doc

	def _row(self, item_code="HOLD-ITEM", serial="HOLD-SN-1", bundle=None):
		row = MagicMock()
		row.item_code = item_code
		row.serial_no = serial
		row.serial_and_batch_bundle = bundle
		row.get = lambda key, default=None: {
			"item_code": item_code, "serial_no": serial,
			"serial_and_batch_bundle": bundle}.get(key, default)
		return row

	def _fire(self, doc, handler="on_pos_draft_change", bundles=None):
		"""`bundles` maps a row's Serial and Batch Bundle name to the guns in it,
		standing in for the get_doc that `serials_for_line` does on the v16 path."""
		from ffl_woo_sync.woocommerce import doc_events

		def line_serials(row):
			if getattr(row, "serial_no", ""):
				return [row.serial_no]
			return list((bundles or {}).get(
				getattr(row, "serial_and_batch_bundle", "") or "", []))

		with patch.object(doc_events, "_skip_push", return_value=False), \
			 patch("ffl_core.serials.serials_for_line", side_effect=line_serials), \
			 patch("frappe.db.get_value", return_value=frappe._dict({
				 "is_firearm": 1, "item_group": "Rifle", "uniform_sell_price": 0,
				 "woo_product_id": "77"})), \
			 patch("frappe.enqueue") as enqueue:
			getattr(doc_events, handler)(doc)
		return enqueue

	def test_saving_a_draft_with_a_firearm_reconciles_its_listing(self):
		enqueue = self._fire(self._doc([self._row()]))
		self.assertEqual(
			[c[1]["serial_no"] for c in enqueue.call_args_list], ["HOLD-SN-1"])

	def test_an_unrelated_resave_costs_nothing(self):
		"""A register saves the draft on every payment / discount / customer
		edit. Firing a WooCommerce round-trip per gun on each of those is how a
		fix becomes an outage."""
		rows = [self._row()]
		enqueue = self._fire(self._doc(rows, before=self._doc(rows)))
		enqueue.assert_not_called()

	def test_taking_the_gun_OFF_the_draft_relists_it(self):
		"""The released gun is the one the current-rows walk can never see."""
		before = self._doc([self._row()])
		enqueue = self._fire(self._doc([], before=before))
		self.assertEqual(
			[c[1]["serial_no"] for c in enqueue.call_args_list], ["HOLD-SN-1"])

	def test_a_submitted_invoice_is_left_to_the_submit_hook(self):
		"""`on_update` fires on submit too; doing it twice doubles every enqueue."""
		enqueue = self._fire(self._doc([self._row()], docstatus=1))
		enqueue.assert_not_called()

	def test_deleting_the_draft_frees_the_gun(self):
		enqueue = self._fire(self._doc([self._row()]), handler="on_pos_invoice_trash")
		self.assertEqual(
			[c[1]["serial_no"] for c in enqueue.call_args_list], ["HOLD-SN-1"])

	def test_a_box_of_ammo_is_not_a_change_of_holds(self):
		"""The key decides whether a save costs a WooCommerce round trip per gun on
		the cart. Keying on EVERY row made adding ammo — or a cleaning kit, or a
		box of targets — look like the guns had changed."""
		from ffl_woo_sync.woocommerce import doc_events

		gun = self._row()
		ammo = self._row(item_code="HOLD-AMMO", serial="")

		def firearm_only(doctype=None, name=None, field=None, *a, **k):
			if doctype == "Item" and name == "HOLD-AMMO":
				return frappe._dict({"is_firearm": 0, "item_group": "Ammunition"})
			return frappe._dict({
				"is_firearm": 1, "item_group": "Rifle", "uniform_sell_price": 0,
				"woo_product_id": "77"})

		with patch.object(doc_events, "_skip_push", return_value=False), \
			 patch("frappe.db.get_value", side_effect=firearm_only), \
			 patch("frappe.enqueue") as enqueue:
			doc_events.on_pos_draft_change(
				self._doc([gun, ammo], before=self._doc([gun])))
		enqueue.assert_not_called()

	def test_a_BUNDLE_line_resaved_unchanged_costs_nothing(self):
		"""A v16 register stores serials in a Serial and Batch Bundle BY DEFAULT.
		Keying on the bundle's NAME could only ever answer "cannot tell, assume
		changed", which switched this dedupe off for every firearm row in
		production: one WooCommerce round trip per gun on the cart, on every
		payment / discount / customer keystroke. The key reads the guns IN the
		bundle instead."""
		rows = [self._row(serial="", bundle="SABB-0001")]
		enqueue = self._fire(self._doc(rows, before=self._doc(rows)),
			bundles={"SABB-0001": ["HOLD-SN-1"]})
		enqueue.assert_not_called()

	def test_a_BUNDLE_line_arriving_on_the_draft_parks_its_gun(self):
		"""The hold has to reach the storefront on the save that created it —
		whichever way the register stored the serial."""
		enqueue = self._fire(
			self._doc([self._row(serial="", bundle="SABB-0001")]),
			bundles={"SABB-0001": ["HOLD-SN-1"]})
		self.assertEqual(
			[c[1]["serial_no"] for c in enqueue.call_args_list], ["HOLD-SN-1"])

	def test_taking_a_BUNDLE_line_OFF_the_draft_relists_its_gun(self):
		"""The release half of the same symmetry: no path may leave a gun holding
		a hold nothing will release."""
		before = self._doc([self._row(serial="", bundle="SABB-0001")])
		enqueue = self._fire(self._doc([], before=before),
			bundles={"SABB-0001": ["HOLD-SN-1"]})
		self.assertEqual(
			[c[1]["serial_no"] for c in enqueue.call_args_list], ["HOLD-SN-1"])

	def test_pointing_the_row_at_ANOTHER_bundle_moves_the_hold_both_ways(self):
		"""Same row, a different bundle: the gun now on the cart is parked AND the
		one that left goes back on sale."""
		before = self._doc([self._row(serial="", bundle="SABB-0001")])
		enqueue = self._fire(
			self._doc([self._row(serial="", bundle="SABB-0002")], before=before),
			bundles={"SABB-0001": ["HOLD-SN-1"], "SABB-0002": ["HOLD-SN-2"]})
		self.assertEqual(
			sorted({c[1]["serial_no"] for c in enqueue.call_args_list}),
			["HOLD-SN-1", "HOLD-SN-2"])

	def test_a_firearm_row_with_no_serial_yet_is_not_a_hold(self):
		"""A hold is a gun — `reservations._draft_pos_serials` keys on the serial.
		A firearm line added before one is picked holds nothing, so it must not
		cost a round trip, and picking the serial must."""
		empty = self._row(serial="", bundle="")
		self.assertEqual(
			self._fire(self._doc([empty], before=self._doc([]))).call_args_list, [])
		picked = self._fire(self._doc([self._row()], before=self._doc([empty])))
		self.assertEqual(
			[c[1]["serial_no"] for c in picked.call_args_list], ["HOLD-SN-1"])

	def test_the_same_bundle_is_loaded_once_per_save(self):
		"""before and after point at the SAME bundle for every row the cashier did
		not touch, so the second full document load answers a question already
		answered. A three-gun cart paid six of those on every keystroke-ish save.

		Not patching `serials_for_line` here: the read being counted is the one it
		does, and a double is the one thing that cannot show it happening twice."""
		from ffl_woo_sync.woocommerce import doc_events

		rows = [self._row(serial="", bundle="SBB-ONCE")]
		bundle_doc = frappe._dict(
			{"entries": [frappe._dict({"serial_no": "BND-SN-1"})]})
		with patch.object(doc_events, "_skip_push", return_value=False), \
			 patch("frappe.get_doc", return_value=bundle_doc) as get_doc, \
			 patch("frappe.db.get_value", return_value=frappe._dict({
				 "is_firearm": 1, "item_group": "Rifle", "uniform_sell_price": 0,
				 "woo_product_id": "77"})), \
			 patch("frappe.enqueue") as enqueue:
			doc_events.on_pos_draft_change(self._doc(rows, before=self._doc(rows)))
		reads = [c for c in get_doc.call_args_list
			if c.args and c.args[0] == "Serial and Batch Bundle"]
		self.assertEqual(len(reads), 1, "the same bundle must be read once per save")
		enqueue.assert_not_called()  # ...and the dedupe still concludes "unchanged"

	def test_turning_the_order_into_a_RETURN_reconciles(self):
		"""Same rows, opposite meaning: a return holds nothing
		(`reservations._draft_pos_serials` excludes returns), so the guns on it go
		back on sale — and something has to tell the storefront."""
		rows = [self._row()]
		enqueue = self._fire(
			self._doc(rows, before=self._doc(rows, is_return=0), is_return=1))
		self.assertEqual(
			[c[1]["serial_no"] for c in enqueue.call_args_list], ["HOLD-SN-1"])

	def test_the_hooks_are_actually_wired(self):
		"""A handler nothing calls is the bug, not the fix."""
		from ffl_woo_sync import hooks

		events = hooks.doc_events["POS Invoice"]
		self.assertIn("on_pos_draft_change", str(events.get("on_update")))
		self.assertIn("on_pos_invoice_trash", str(events.get("on_trash")))


# ---------------------------------------------------------------------------
# The gun swapped INSIDE a bundle — the only place the one that left is visible
# ---------------------------------------------------------------------------

class TestASwappedBundleReleasesTheGunThatLeft(FrappeTestCase):
	"""ERPNext's serial picker rewrites an EXISTING bundle in place
	(``serial_and_batch_bundle.update_serial_batch_no_ledgers``: ``set("entries",
	[])`` then re-append, same bundle name) and does it in its own committed
	request, before the invoice is saved. So from the invoice's save nothing
	changed — the row still names the same bundle, and even the pre-save snapshot
	of that bundle already reads the NEW gun. The bundle's own before/after
	entries are the only record of the swap, which makes this hook the only thing
	that can put the gun that left back on sale."""

	def _bundle(self, entries, *, before_entries=None, voucher_type="POS Invoice",
			voucher="ACC-PSINV-2026-00001", item_code="HOLD-ITEM"):
		def as_doc(serials):
			doc = MagicMock()
			doc.get = lambda key, default=None: (
				{"entries": [frappe._dict({"serial_no": s}) for s in serials]}
				.get(key, default))
			return doc

		doc = as_doc(entries)
		doc.voucher_type = voucher_type
		doc.voucher_no = voucher
		doc.item_code = item_code
		doc.get_doc_before_save.return_value = (
			None if before_entries is None else as_doc(before_entries))
		return doc

	def _fire(self, doc, *, docstatus=0, item=None):
		from ffl_woo_sync.woocommerce import doc_events

		item_row = item if item is not None else {
			"is_firearm": 1, "item_group": "Rifle", "uniform_sell_price": 0,
			"woo_product_id": "77"}

		def db_get_value(doctype=None, name=None, fieldname=None, *a, **k):
			if doctype in ("POS Invoice", "Sales Invoice"):
				return docstatus
			if doctype == "Serial No":
				return frappe._dict({"woo_product_id": "77"})
			return frappe._dict(item_row)

		with patch.object(doc_events, "_skip_push", return_value=False), \
			 patch("frappe.db.get_value", side_effect=db_get_value), \
			 patch("frappe.enqueue") as enqueue:
			doc_events.on_serial_batch_bundle_update(doc)
		return enqueue

	def test_the_gun_that_left_and_the_gun_that_arrived_both_reconcile(self):
		"""Both halves, or the swap is half-done: the gun taken off would sit at
		stock 0 for ever and the one put on would stay buyable online."""
		enqueue = self._fire(self._bundle(["SWAP-NEW"], before_entries=["SWAP-OLD"]))
		self.assertEqual(
			sorted(c[1]["serial_no"] for c in enqueue.call_args_list),
			["SWAP-NEW", "SWAP-OLD"])

	def test_a_bundle_saved_with_the_same_guns_costs_nothing(self):
		"""Every invoice save touches its bundles; only a changed gun is news."""
		enqueue = self._fire(
			self._bundle(["SWAP-SAME"], before_entries=["SWAP-SAME"]))
		enqueue.assert_not_called()

	def test_a_brand_new_bundle_is_left_to_the_save_that_attaches_it(self):
		"""No before-save snapshot means an INSERT, and an insert is not a hold yet:
		`counter_holds` reaches a gun through `POS Invoice Item.
		serial_and_batch_bundle`, and no row can point at a name that did not exist
		a moment ago. Reconciling here would publish the PRE-hold answer and then
		the save that attaches the bundle — a changed `_hold_key` — would push the
		same gun again. Two WooCommerce round-trips per gun, the first one wrong."""
		enqueue = self._fire(self._bundle(["SWAP-FRESH"]))
		enqueue.assert_not_called()

	def test_a_draft_SALES_invoice_is_not_a_counter_hold(self):
		"""A DRAFT hold comes from a draft POS Invoice or a draft Consignment Out
		(`reservations.counter_holds`, whose lines carry a plain serial column and
		no bundle); its Sales Invoice leg requires a SUBMITTED invoice. So changing
		the guns on a draft SI spends a round-trip per gun to publish the answer
		already on the page — and leaves the next reader thinking draft-SI holds
		are handled."""
		enqueue = self._fire(self._bundle(
			["SWAP-NEW"], before_entries=["SWAP-OLD"], voucher_type="Sales Invoice"))
		enqueue.assert_not_called()

	def test_a_voucher_that_is_not_there_is_not_a_draft(self):
		"""`int(None or 0) == 0` read a missing invoice — deleted, or a voucher_no
		pointing at nothing — as a draft, and went on pushing holds for an order
		that does not exist."""
		enqueue = self._fire(
			self._bundle(["SWAP-NEW"], before_entries=["SWAP-OLD"]), docstatus=None)
		enqueue.assert_not_called()

	def test_a_submitted_invoice_is_left_to_the_stock_ledger(self):
		"""Past submit the SLE hooks carry every change; doing it here too would
		double the work."""
		enqueue = self._fire(
			self._bundle(["SWAP-NEW"], before_entries=["SWAP-OLD"]), docstatus=1)
		enqueue.assert_not_called()

	def test_a_bundle_that_belongs_to_no_counter_order_is_ignored(self):
		"""A Stock Entry's or Purchase Receipt's bundle is not a counter hold."""
		enqueue = self._fire(self._bundle(
			["SWAP-NEW"], before_entries=["SWAP-OLD"], voucher_type="Stock Entry"))
		enqueue.assert_not_called()

	def test_a_bundle_with_no_invoice_name_yet_is_left_to_the_invoice_save(self):
		"""Built before its invoice had a name — that invoice's own save sees a new
		bundle on the row and reconciles from there."""
		enqueue = self._fire(self._bundle(
			["SWAP-NEW"], before_entries=["SWAP-OLD"], voucher=""))
		enqueue.assert_not_called()

	def test_a_non_firearm_bundle_is_ignored(self):
		"""Serialised non-firearms exist (optics, NVGs) and hold nothing."""
		enqueue = self._fire(
			self._bundle(["SWAP-NEW"], before_entries=["SWAP-OLD"]),
			item={"is_firearm": 0, "item_group": "Optics"})
		enqueue.assert_not_called()

	def test_a_uniform_price_item_is_told_only_when_the_NUMBER_of_holds_moves(self):
		"""Uniform-price firearms have no per-serial listing — the hold reaches the
		storefront as a count (`mapping.stock_for_item` subtracts it). A 1-for-1
		swap leaves that count alone; losing a gun off the bundle does not."""
		uniform = {"is_firearm": 1, "item_group": "Rifle", "uniform_sell_price": 1,
			"woo_product_id": "77"}
		swapped = self._fire(
			self._bundle(["U-NEW"], before_entries=["U-OLD"]), item=uniform)
		swapped.assert_not_called()
		dropped = self._fire(
			self._bundle([], before_entries=["U-OLD"]), item=uniform)
		self.assertEqual(
			[c[1]["item_code"] for c in dropped.call_args_list], ["HOLD-ITEM"])

	def test_the_hook_is_actually_wired(self):
		"""A handler nothing calls is the bug, not the fix."""
		from ffl_woo_sync import hooks

		self.assertIn(
			"on_serial_batch_bundle_update",
			str(hooks.doc_events["Serial and Batch Bundle"].get("on_update")))


# ---------------------------------------------------------------------------
# The same swap, on real documents
# ---------------------------------------------------------------------------

class TestARealBundleSwapOnRealDocuments(SerialExitFixtures):
	"""Everything above builds the bundle and the pre-save snapshot out of mocks,
	and the load-bearing step is exactly what a mock cannot get wrong: whether a
	rewritten bundle is still visible from the invoice's own save. An earlier version shipped on
	that assumption and was wrong about it, with nine green tests.

	So this one uses real documents — a real draft POS Invoice whose firearm line
	carries a real Serial and Batch Bundle, two real serials in stock, and the
	bundle's entries rewritten the way ERPNext's serial picker rewrites them
	(`serial_and_batch_bundle.update_serial_batch_no_ledgers`: clear `entries`,
	re-append, save the SAME bundle). Nothing is patched but `frappe.enqueue`:
	`get_doc_before_save`, `serials_for_line` and the docstatus read are the real
	thing, and the assertion is which GUNS came out, not how often a mock was
	called.
	"""

	GUN_A = "WOO-SWAP-A"
	GUN_B = "WOO-SWAP-B"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.customer = "Woo Swap Buyer"
		if not frappe.db.exists("Customer", cls.customer):
			frappe.get_doc({"doctype": "Customer", "customer_name": cls.customer,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		# ERPNext refuses a POS Invoice with no open shift.
		from ffl_core.tests.pos_opening import todays_pos_opening
		todays_pos_opening(cls.company)

	def setUp(self):
		for serial in (self.GUN_A, self.GUN_B):
			self._stock_in(serial)
			# Listed online — an unlisted gun has nothing to reconcile.
			frappe.db.set_value("Serial No", serial, "woo_product_id", "90901",
				update_modified=False)

	def _draft_holding(self, serial: str) -> tuple[str, str]:
		"""A DRAFT POS Invoice whose firearm line stores its gun in a bundle.

		Saved in the legacy shape and then rewritten into the bundle shape, the way
		the sibling fixtures in ffl_core and ffl_integrations do it: saving it as a
		bundle line would drag ERPNext's own bundle validation — live stock, a
		posting voucher — into a test about what our hook can see.
		"""
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = "FFL Test"
		inv.customer = self.customer
		inv.append("items", {"item_code": ITEM, "qty": 1, "rate": 600,
			"price_list_rate": 600, "serial_no": serial,
			"use_serial_batch_fields": 1, "allow_zero_valuation_rate": 1})
		inv.set_missing_values()
		inv.flags.ignore_permissions = True
		inv.save()
		row = frappe.get_doc("POS Invoice", inv.name).items[0]
		bundle = frappe.get_doc({
			"doctype": "Serial and Batch Bundle", "company": self.company,
			"item_code": ITEM, "has_serial_no": 1, "type_of_transaction": "Outward",
			"voucher_type": "POS Invoice", "voucher_no": inv.name,
			# The LINE's warehouse, not any warehouse: ERPNext cross-checks the two
			# when the invoice is re-saved over this bundle, and the register's
			# profile does not necessarily sell out of the one stock came in to.
			"warehouse": row.warehouse,
			"entries": [{"serial_no": serial, "qty": -1, "warehouse": row.warehouse}],
		})
		bundle.flags.ignore_validate = True
		bundle.flags.ignore_permissions = True
		bundle.insert(ignore_permissions=True, ignore_mandatory=True, ignore_links=True)
		frappe.db.set_value("POS Invoice Item", row.name, {
			"serial_no": None, "use_serial_batch_fields": 0,
			"serial_and_batch_bundle": bundle.name}, update_modified=False)
		return inv.name, bundle.name

	def _swap(self, bundle_name: str, serial: str):
		"""What the serial picker does: clear `entries`, re-append, save the SAME
		bundle. Returns the patched `frappe.enqueue`."""
		bundle = frappe.get_doc("Serial and Batch Bundle", bundle_name)
		bundle.set("entries", [])
		bundle.append("entries", {"serial_no": serial, "qty": -1})
		bundle.flags.ignore_validate = True
		bundle.flags.ignore_permissions = True
		with patch("frappe.enqueue") as enqueue:
			bundle.save(ignore_permissions=True)
		return enqueue

	@staticmethod
	def _reconciled(enqueue) -> list[str]:
		return sorted(c.kwargs["serial_no"] for c in enqueue.call_args_list
			if c.args and "sync_serial_listing_task" in str(c.args[0]))

	def _cleanup(self, invoice: str, bundle: str) -> None:
		"""Invoice line FIRST, then the bundle: FrappeTestCase rolls back once per
		CLASS, and a line left pointing at a deleted bundle makes
		`serials_for_line` raise for every later test in it."""
		frappe.db.delete("POS Invoice Item", {"parent": invoice})
		frappe.db.delete("POS Invoice", {"name": invoice})
		frappe.db.delete("Serial and Batch Entry", {"parent": bundle})
		frappe.db.delete("Serial and Batch Bundle", {"name": bundle})

	def test_a_real_in_place_swap_reconciles_BOTH_guns(self):
		"""Both halves, or the swap is half-done: the gun taken off would sit at
		stock 0 for ever, and the one put on would stay buyable online."""
		invoice, bundle = self._draft_holding(self.GUN_A)
		try:
			enqueue = self._swap(bundle, self.GUN_B)
			self.assertEqual(
				self._reconciled(enqueue), [self.GUN_A, self.GUN_B],
				"the gun that left and the gun that arrived must both reconcile")
		finally:
			self._cleanup(invoice, bundle)

	def test_the_same_gun_rewritten_in_place_costs_nothing(self):
		"""The picker rewrites the bundle even when the cashier re-picks the gun it
		already held. Only a CHANGED gun is news."""
		invoice, bundle = self._draft_holding(self.GUN_A)
		try:
			enqueue = self._swap(bundle, self.GUN_A)
			self.assertEqual(self._reconciled(enqueue), [])
		finally:
			self._cleanup(invoice, bundle)

	def test_the_invoices_own_save_can_no_longer_see_the_swap(self):
		"""Why the hook has to exist at all, pinned on the real thing — and an alarm
		if v16 ever starts showing the old entries to the invoice's save again.

		The invoice is saved AFTER the rewrite and reconciles nothing: the row still
		names the same bundle, and a pre-save read of that bundle already returns the
		NEW gun. That is where the earlier shipped version stopped, and why the gun that
		left stayed at stock 0."""
		invoice, bundle = self._draft_holding(self.GUN_A)
		try:
			self._swap(bundle, self.GUN_B)
			inv = frappe.get_doc("POS Invoice", invoice)
			inv.flags.ignore_permissions = True
			with patch("frappe.enqueue") as enqueue:
				inv.save()
			self.assertEqual(self._reconciled(enqueue), [])
		finally:
			self._cleanup(invoice, bundle)
