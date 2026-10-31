"""Dealer-entered serials outrank POS's own placeholder holds.

When a web order for a shared-SKU firearm lands, POS picks the oldest free gun
as a `stock_hold_serial` PLACEHOLDER — an arbitrary guess made with no
information about which rifle staff will actually pull off the rack. When staff
later type the serial of the gun in their hands into WooCommerce, that is the
physical fact and it has to win.

It did not. The cross-order guard read only `Serial No.woo_reserved` /
`woo_order`, which cannot tell a placeholder apart from another order's
dealer-CONFIRMED pin. So a staff pick lost to a neighbouring order's guess, and
because a placeholder is only released when its own order gets a serial, the
refusals could chain into a deadlock no retry could break:

    order A wants MOR0002985  → held (placeholder) by order B
    order B wants 9130104914  → held (placeholder) by order C
    order C wants nothing     → never releases

Seen in production. Three rifles on the shelf, three paid orders, zero actual
double-sell, and the queue stuck until a human noticed.

The distinction was never missing from the domain — `fulfillment.reserved_web_serials`
has named these two holds `woo_assigned` vs `woo_held` all along. The guard just
never asked. So:

- placeholder blocker  → EVICT it (move that order to another gun) and hand the
  rifle to the order whose staff named it;
- confirmed blocker    → keep refusing. Two orders naming the same physical gun
  is a real data error and the only thing this guard should ever stop.

`TestEvictionAgainstTheDatabase` is the load-bearing class here. The first cut
of this feature mocked `_pick_oldest_unreserved_serial` and `frappe.db.set_value`
in every eviction test, which hid the defect that mattered: freeing the disputed
gun before searching for a replacement let the search hand it straight back, so
both orders ended up naming one rifle. Mocks cannot see that. Real rows can.
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

WEBHOOK = "ffl_woo_sync.woocommerce.webhook"
ITEM = "QA-PRECEDENCE-MOSIN"
OTHER_ITEM = "QA-PRECEDENCE-OTHER"


def _srow(item_code=ITEM, confirmed_serial="", stock_hold_serial="",
		woo_line_id="7", unit_index=0, reservation_error=""):
	return frappe._dict({
		"item_code": item_code, "confirmed_serial": confirmed_serial,
		"stock_hold_serial": stock_hold_serial, "woo_line_id": woo_line_id,
		"unit_index": unit_index, "reservation_error": reservation_error,
	})


def _order(name="WOO-ORD-MINE"):
	o = MagicMock()
	o.name = name
	o.woo_order_id = "9001"
	return o


def _sn(item_code=ITEM, status="Active", woo_reserved=1,
		woo_order="WOO-ORD-OTHER"):
	return {"item_code": item_code, "status": status,
			"woo_reserved": woo_reserved, "woo_order": woo_order}


def _make_order(*, serials, order_status="Pending Fulfillment", site="retail",
		woo_order_id=None, item_code=ITEM):
	"""A real Woo Online Order so the hold lookup runs against the database."""
	doc = frappe.get_doc({
		"doctype": "Woo Online Order",
		"woo_order_id": woo_order_id or f"DP-{frappe.generate_hash(length=8)}",
		"order_status": order_status, "site": site,
		"customer_name": "QA Precedence",
		"serials": [{
			"item_code": s.get("item_code", item_code),
			"woo_line_id": s.get("woo_line_id", "1"),
			"unit_index": 0,
			"confirmed_serial": s.get("confirmed_serial", ""),
			"stock_hold_serial": s.get("stock_hold_serial", ""),
			"reservation_error": s.get("reservation_error", ""),
			"fulfillment": s.get("fulfillment", "local"),
		} for s in serials],
	})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc


def _ensure_items():
	"""The serial rows carry `item_code` as a Link, so the masters must exist
	before any fixture is inserted. Committed once and left behind: a rolled-back
	Item would take the next class's fixtures down with it."""
	group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
	made = False
	for code in (ITEM, OTHER_ITEM):
		if frappe.db.exists("Item", code):
			continue
		doc = frappe.get_doc({
			"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": group, "stock_uom": "Nos", "has_serial_no": 1,
			"is_stock_item": 1,
		})
		doc.flags.ignore_permissions = True
		doc.insert()
		made = True
	if made:
		frappe.db.commit()


def _row_of(order_name):
	return frappe.db.get_value(
		"Woo Online Order Serial", {"parent": order_name},
		["stock_hold_serial", "confirmed_serial", "reservation_error"],
		as_dict=True,
	)


def _reservation_of(serial_no):
	return frappe.db.get_value(
		"Serial No", serial_no, ["woo_reserved", "woo_order"], as_dict=True)


def setUpModule():
	# These tests stub frappe.db.get_value for the whole test. A Meta built under that stub is built out of
	# the fixture, so build the real one first. Alone in a fresh process nothing else has (docs/testing.md).
	from ffl_core.tests.channel_query_doubles import warm_channel_meta

	warm_channel_meta()


# ---------------------------------------------------------------------------
# _evictable_hold — which holds may be moved, against a REAL database
# ---------------------------------------------------------------------------

class TestEvictableHoldLookup(FrappeTestCase):
	"""This lookup decides whether a gun can be taken away from another paid
	order. Asserting on its SQL text would pass just as happily for `WHERE 1=1`,
	so it goes against the real database."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_items()

	def test_a_pos_placeholder_is_evictable(self):
		from ffl_woo_sync.woocommerce.webhook import _evictable_hold
		holder = _make_order(serials=[{"stock_hold_serial": "MOR0002985"}])
		hold = _evictable_hold("MOR0002985", ITEM, holder.name)
		self.assertIsNotNone(hold)
		self.assertEqual(hold["site"], "retail")
		self.assertEqual(hold["item_code"], ITEM)
		self.assertEqual(str(hold["woo_order_id"]), str(holder.woo_order_id))

	def test_a_dealer_confirmed_pin_is_never_evictable(self):
		"""The one case this guard exists for: two orders naming the same gun."""
		from ffl_woo_sync.woocommerce.webhook import _evictable_hold
		holder = _make_order(serials=[{"confirmed_serial": "MOR0002985"}])
		self.assertIsNone(_evictable_hold("MOR0002985", ITEM, holder.name))

	def test_a_hold_on_an_order_that_left_the_queue_is_not_evictable(self):
		"""A disposed/cancelled order's reservation is stale, not swappable —
		moving it would quietly rewrite a gun onto an order already booked."""
		from ffl_woo_sync.woocommerce.webhook import _evictable_hold
		holder = _make_order(serials=[{"stock_hold_serial": "MOR0002985"}],
			order_status="Transferred")
		self.assertIsNone(_evictable_hold("MOR0002985", ITEM, holder.name))

	def test_a_reservation_with_no_matching_row_is_not_evictable(self):
		"""Dangling `Serial No.woo_order` with nothing behind it is a data fault.
		Refusing keeps the anomaly visible instead of silently reassigning."""
		from ffl_woo_sync.woocommerce.webhook import _evictable_hold
		holder = _make_order(serials=[{"stock_hold_serial": "SOMETHING-ELSE"}])
		self.assertIsNone(_evictable_hold("MOR0002985", ITEM, holder.name))

	def test_a_hold_belonging_to_a_different_item_is_not_evictable(self):
		"""A mistyped serial can name another model's rifle. Without this
		predicate the holder — an order for a different gun entirely — was
		matched, then handed a replacement of OUR model."""
		from ffl_woo_sync.woocommerce.webhook import _evictable_hold
		holder = _make_order(
			serials=[{"stock_hold_serial": "MOR0002985", "item_code": OTHER_ITEM}],
			item_code=OTHER_ITEM)
		self.assertIsNone(_evictable_hold("MOR0002985", ITEM, holder.name))

	def test_a_dropship_row_is_not_evictable(self):
		"""A drop-shipped unit holds no local stock — there is nothing to move."""
		from ffl_woo_sync.woocommerce.webhook import _evictable_hold
		holder = _make_order(serials=[
			{"stock_hold_serial": "MOR0002985", "fulfillment": "direct"}])
		self.assertIsNone(_evictable_hold("MOR0002985", ITEM, holder.name))

	def test_the_holders_own_site_is_reported(self):
		"""The eviction notice and the replacement's push go to the store the
		evicted order came from: the hold reports that order's own site."""
		from ffl_woo_sync.woocommerce.webhook import _evictable_hold
		holder = _make_order(serials=[{"stock_hold_serial": "MOR0002985"}],
			site="retail")
		self.assertEqual(_evictable_hold("MOR0002985", ITEM, holder.name)["site"],
			"retail")


# ---------------------------------------------------------------------------
# Eviction against a REAL database — real Item, real Serial Nos, real writes
# ---------------------------------------------------------------------------

class TestEvictionAgainstTheDatabase(FrappeTestCase):
	"""Nothing on the write path is mocked here: the replacement really is
	chosen by `_pick_oldest_unreserved_serial`, and every reservation really
	lands in the database. That is deliberate — the defect this class exists to
	catch (the disputed gun being handed back as its own replacement) is
	invisible to any test that stubs the picker."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_items()

	def _serial(self, name, *, reserved_for=""):
		"""A real in-stock Serial No. `creation` ordering is what the picker uses,
		so they are made in call order and asserted on by name, never by luck."""
		doc = frappe.get_doc({
			"doctype": "Serial No", "serial_no": name, "item_code": ITEM})
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.set_value("Serial No", name, {
			"status": "Active",
			"woo_reserved": 1 if reserved_for else 0,
			"woo_order": reserved_for,
		}, update_modified=False)
		return name

	def _quiet(self):
		"""Silence only the OUTBOUND effects — queue pushes, the WooCommerce
		note, the Error Log. Nothing that decides which order
		gets which rifle is touched, which is the whole point of this class."""
		stack = ExitStack()
		for target in ("_enqueue_serial_or_item_push",
				"_queue_wc_order_note", "_log_error"):
			stack.enter_context(patch(f"{WEBHOOK}.{target}"))
		self.addCleanup(stack.close)
		return stack

	def test_the_disputed_gun_is_never_handed_back_as_its_own_replacement(self):
		"""The bug that shipped in the first cut. Freeing the rifle and THEN
		asking for a replacement returned that same rifle — it reads this
		transaction's own uncommitted write, oldest-first, and a gun becomes a
		placeholder precisely by being the oldest free one. Both orders then
		named it."""
		from ffl_woo_sync.woocommerce.webhook import _evict_placeholder_hold
		disputed = self._serial("QAP-OLD-0001")          # oldest → the trap
		spare = self._serial("QAP-NEW-0002")
		holder = _make_order(serials=[{"stock_hold_serial": disputed}])
		frappe.db.set_value("Serial No", disputed,
			{"woo_reserved": 1, "woo_order": holder.name}, update_modified=False)

		with self._quiet():
			moved_to = _evict_placeholder_hold(
				disputed, ITEM, holder.name, new_owner="WOO-ORD-MINE")

		self.assertEqual(moved_to, spare)
		self.assertEqual(_row_of(holder.name).stock_hold_serial, spare)
		self.assertEqual(_reservation_of(disputed).woo_order, "WOO-ORD-MINE")
		self.assertEqual(_reservation_of(spare).woo_order, holder.name)

	def test_no_two_pending_rows_end_up_naming_the_same_rifle(self):
		"""The invariant the whole guard exists to protect, asserted on final
		state rather than on which branch ran."""
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		disputed = self._serial("QAP-DIS-0001")
		mine = self._serial("QAP-MINE-0002")
		holder = _make_order(serials=[{"stock_hold_serial": disputed}])
		frappe.db.set_value("Serial No", disputed,
			{"woo_reserved": 1, "woo_order": holder.name}, update_modified=False)
		taker = _make_order(serials=[{"stock_hold_serial": mine}])
		frappe.db.set_value("Serial No", mine,
			{"woo_reserved": 1, "woo_order": taker.name}, update_modified=False)
		row = _srow(stock_hold_serial=mine)

		with self._quiet():
			_apply_dealer_serial(taker, row, disputed)

		# For the EVICTED order the stored row is authoritative — eviction writes
		# it directly. For the taker the in-memory row is: `_apply_dealer_serial`
		# mutates it and the caller saves afterwards, so its stored copy is still
		# the pre-call state and counting that too would double-count by
		# construction.
		evicted = _row_of(holder.name)
		named = [s for s in (evicted.stock_hold_serial, evicted.confirmed_serial) if s]
		named += [s for s in (row.confirmed_serial, row.stock_hold_serial) if s]
		self.assertEqual(len(named), len(set(named)), f"a rifle is named twice: {named}")
		# …and the guns really did change hands, so the assertion above is not
		# passing on an unchanged board.
		self.assertEqual(row.confirmed_serial, disputed)
		self.assertEqual(evicted.stock_hold_serial, mine)

	def test_the_picker_skips_a_gun_gunbroker_probably_sold(self):
		"""`gb_sold_unknown` keeps a gun out of `uniform_free_serials`, the pool the
		claim gate counts. The picker must apply the same key, or it hands a paid
		web order the oldest gun — one GunBroker very probably already sold."""
		if not frappe.db.has_column("Serial No", "gb_sold_unknown"):
			self.skipTest("gb_sold_unknown is not migrated on this bench")
		from ffl_woo_sync.woocommerce.webhook import _pick_oldest_unreserved_serial
		# Its own Item: the other tests here leave free guns of ITEM behind.
		item = f"QA-GBSOLD-{frappe.generate_hash(length=6)}"
		doc = frappe.get_doc({
			"doctype": "Item", "item_code": item, "item_name": item,
			"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
			"stock_uom": "Nos", "has_serial_no": 1, "is_stock_item": 1})
		doc.flags.ignore_permissions = True
		doc.insert()
		serials = []
		for suffix in ("0001", "0002"):              # 0001 is oldest → the trap
			sn = frappe.get_doc({"doctype": "Serial No",
				"serial_no": f"{item}-{suffix}", "item_code": item})
			sn.flags.ignore_permissions = True
			sn.insert()
			frappe.db.set_value("Serial No", sn.name, "status", "Active",
				update_modified=False)
			serials.append(sn.name)
		sold, free = serials
		frappe.db.set_value("Serial No", sold, "gb_sold_unknown", 1,
			update_modified=False)
		self.assertEqual(_pick_oldest_unreserved_serial(item), free)

	def test_a_two_order_swap_closes_one_for_one_with_no_spare_stock(self):
		"""Two orders, two rifles, nothing else on the shelf — the prod shape.
		Without offering our own placeholder as the replacement the evicted order
		was told there was no stock, while the gun we released went back on sale."""
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		gun_a = self._serial("QAP-SWAP-0001")
		gun_b = self._serial("QAP-SWAP-0002")
		holder = _make_order(serials=[{"stock_hold_serial": gun_b}])
		frappe.db.set_value("Serial No", gun_b,
			{"woo_reserved": 1, "woo_order": holder.name}, update_modified=False)
		taker = _make_order(serials=[{"stock_hold_serial": gun_a}])
		frappe.db.set_value("Serial No", gun_a,
			{"woo_reserved": 1, "woo_order": taker.name}, update_modified=False)
		row = _srow(stock_hold_serial=gun_a)

		with self._quiet():
			_apply_dealer_serial(taker, row, gun_b)

		self.assertEqual(row.confirmed_serial, gun_b)
		self.assertEqual(row.reservation_error, "")
		self.assertEqual(_reservation_of(gun_b).woo_order, taker.name)
		# The evicted order kept a gun — ours — instead of being told "no stock".
		self.assertEqual(_row_of(holder.name).stock_hold_serial, gun_a)
		self.assertEqual(_reservation_of(gun_a).woo_order, holder.name)
		self.assertEqual(_row_of(holder.name).reservation_error, "")

	def test_an_evicted_orders_existing_error_survives_so_the_sweep_keeps_it(self):
		"""`reservation_error != ''` is the retry sweep's own selector. Blanking
		one drops that order out of auto-recovery for good — and a Woo order that
		has gone `completed` sends no webhook to put it back."""
		from ffl_woo_sync.woocommerce.webhook import _evict_placeholder_hold
		disputed = self._serial("QAP-ERR-0001")
		self._serial("QAP-ERR-0002")
		own_error = "Serial 'ZZZ' is already reserved for another web order (WOO-ORD-X)"
		holder = _make_order(serials=[
			{"stock_hold_serial": disputed, "reservation_error": own_error}])
		frappe.db.set_value("Serial No", disputed,
			{"woo_reserved": 1, "woo_order": holder.name}, update_modified=False)

		with self._quiet():
			_evict_placeholder_hold(disputed, ITEM, holder.name,
				new_owner="WOO-ORD-MINE")

		self.assertEqual(_row_of(holder.name).reservation_error, own_error)

	def test_with_nothing_left_to_hold_the_evicted_order_says_so(self):
		from ffl_woo_sync.woocommerce.webhook import (
			_NO_STOCK_REASON, _evict_placeholder_hold)
		only_gun = self._serial("QAP-LAST-0001")
		holder = _make_order(serials=[{"stock_hold_serial": only_gun}])
		frappe.db.set_value("Serial No", only_gun,
			{"woo_reserved": 1, "woo_order": holder.name}, update_modified=False)

		with self._quiet():
			moved_to = _evict_placeholder_hold(only_gun, ITEM, holder.name,
				new_owner="WOO-ORD-MINE")

		self.assertEqual(moved_to, "")
		row = _row_of(holder.name)
		self.assertEqual(row.stock_hold_serial, "")
		self.assertTrue(row.reservation_error.startswith(_NO_STOCK_REASON),
			row.reservation_error)
		# The rifle still went to the order whose staff named it.
		self.assertEqual(_reservation_of(only_gun).woo_order, "WOO-ORD-MINE")

	def test_the_prod_chain_unwinds_and_every_order_keeps_a_rifle(self):
		"""The production deadlock chain (orders A / B / C), rebuilt end to end: three orders,
		three rifles, no spare. Before the fix this needed a human to put a serial
		on the tail order and then two sweep cycles."""
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		g1 = self._serial("QAP-CHAIN-0001")   # order A's placeholder
		g2 = self._serial("QAP-CHAIN-0002")   # order C's placeholder
		g3 = self._serial("QAP-CHAIN-0003")   # order B's placeholder
		tail = _make_order(serials=[{"stock_hold_serial": g2}], woo_order_id="4875")
		middle = _make_order(serials=[{"stock_hold_serial": g3}], woo_order_id="4879")
		head = _make_order(serials=[{"stock_hold_serial": g1}], woo_order_id="4786")
		for serial, owner in ((g1, head.name), (g2, tail.name), (g3, middle.name)):
			frappe.db.set_value("Serial No", serial,
				{"woo_reserved": 1, "woo_order": owner}, update_modified=False)

		mid_row = _srow(stock_hold_serial=g3)
		head_row = _srow(stock_hold_serial=g1)
		with self._quiet():
			_apply_dealer_serial(middle, mid_row, g2)   # order B named order C's gun
			_apply_dealer_serial(head, head_row, g3)    # order A named order B's gun

		self.assertEqual(mid_row.confirmed_serial, g2)
		self.assertEqual(mid_row.reservation_error, "")
		self.assertEqual(head_row.confirmed_serial, g3)
		self.assertEqual(head_row.reservation_error, "")
		self.assertEqual(_reservation_of(g2).woo_order, middle.name)
		self.assertEqual(_reservation_of(g3).woo_order, head.name)
		# …and the tail order, which never named anything, still holds a rifle.
		self.assertEqual(_row_of(tail.name).stock_hold_serial, g1)
		self.assertEqual(_reservation_of(g1).woo_order, tail.name)
		self.assertEqual(_row_of(tail.name).reservation_error, "")


# ---------------------------------------------------------------------------
# _apply_dealer_serial — the guard that used to refuse
# ---------------------------------------------------------------------------

class TestApplyDealerSerialPrecedence(FrappeTestCase):
	def test_a_confirmed_blocker_is_still_refused_with_the_same_wording(self):
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow()
		with patch("frappe.db.get_value", return_value=_sn()), \
			 patch(f"{WEBHOOK}._evict_placeholder_hold", return_value=None), \
			 patch(f"{WEBHOOK}._reserve_serial") as mock_res, \
			 patch(f"{WEBHOOK}._queue_wc_order_note"), \
			 patch(f"{WEBHOOK}._log_error"):
			changed = _apply_dealer_serial(_order(), row, "MOR0002985")
		self.assertTrue(changed)
		self.assertIn("already reserved for another web order", row.reservation_error)
		self.assertIn("WOO-ORD-OTHER", row.reservation_error)
		self.assertEqual(row.confirmed_serial, "")
		mock_res.assert_not_called()

	def test_our_own_placeholder_is_offered_to_the_order_we_evict(self):
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow(stock_hold_serial="MINE-1")
		with patch("frappe.db.get_value", return_value=_sn()), \
			 patch(f"{WEBHOOK}._evict_placeholder_hold",
				 return_value="MINE-1") as mock_ev, \
			 patch(f"{WEBHOOK}._reserve_serial"), \
			 patch(f"{WEBHOOK}._release_serial") as mock_rel, \
			 patch(f"{WEBHOOK}._enqueue_serial_or_item_push"):
			_apply_dealer_serial(_order(), row, "MOR0002985")
		self.assertEqual(mock_ev.call_args.kwargs["preferred_replacement"], "MINE-1")
		self.assertEqual(mock_ev.call_args.kwargs["new_owner"], "WOO-ORD-MINE")
		# Handed over — releasing it would strip the reservation made on the
		# evicted order's behalf and relist a gun that is now spoken for.
		mock_rel.assert_not_called()

	def test_a_placeholder_we_did_not_give_away_is_still_released(self):
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow(stock_hold_serial="MINE-1")
		with patch("frappe.db.get_value", return_value=_sn()), \
			 patch(f"{WEBHOOK}._evict_placeholder_hold", return_value="SOMETHING-ELSE"), \
			 patch(f"{WEBHOOK}._reserve_serial"), \
			 patch(f"{WEBHOOK}._release_serial") as mock_rel, \
			 patch(f"{WEBHOOK}._enqueue_serial_or_item_push"):
			_apply_dealer_serial(_order(), row, "MOR0002985")
		mock_rel.assert_called_once_with("MINE-1", site="retail")

	def test_eviction_is_not_attempted_against_our_own_hold(self):
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow(stock_hold_serial="MINE-1")
		with patch("frappe.db.get_value",
				return_value=_sn(woo_order="WOO-ORD-MINE")), \
			 patch(f"{WEBHOOK}._evict_placeholder_hold") as mock_ev, \
			 patch(f"{WEBHOOK}._reserve_serial"), \
			 patch(f"{WEBHOOK}._release_serial") as mock_rel, \
			 patch(f"{WEBHOOK}._enqueue_serial_or_item_push"):
			_apply_dealer_serial(_order(), row, "MOR0002985")
		mock_ev.assert_not_called()
		mock_rel.assert_called_once_with("MINE-1", site="retail")

	def test_an_unreserved_gun_costs_no_eviction_lookup(self):
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow()
		with patch("frappe.db.get_value",
				return_value=_sn(woo_reserved=0, woo_order="")), \
			 patch(f"{WEBHOOK}._evict_placeholder_hold") as mock_ev, \
			 patch(f"{WEBHOOK}._reserve_serial"), \
			 patch(f"{WEBHOOK}._enqueue_serial_or_item_push"):
			_apply_dealer_serial(_order(), row, "MOR0002985")
		mock_ev.assert_not_called()

	def test_the_item_mutex_is_taken_before_the_serial_is_locked(self):
		"""Order matters, not merely presence: a serial locked first, mutex
		second, is the deadlock this exists to prevent (measured 2/2)."""
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		seen: list[str] = []
		with patch(f"{WEBHOOK}._lock_item_reservations",
				side_effect=lambda ic: seen.append(f"item:{ic}")), \
			 patch("frappe.db.get_value",
				side_effect=lambda dt, *a, **kw: seen.append(f"lock:{dt}") or _sn()), \
			 patch(f"{WEBHOOK}._evict_placeholder_hold", return_value=None), \
			 patch(f"{WEBHOOK}._queue_wc_order_note"), \
			 patch(f"{WEBHOOK}._log_error"):
			_apply_dealer_serial(_order(), _srow(), "MOR0002985")
		self.assertEqual(seen[0], f"item:{ITEM}")
		self.assertEqual(seen[1], "lock:Serial No")

	def test_the_serial_read_is_still_row_locked(self):
		"""Eviction widens what this path writes; losing the lock here would let
		two orders evict each other onto the same rifle."""
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow()
		with patch("frappe.db.get_value", return_value=_sn()) as mock_get, \
			 patch(f"{WEBHOOK}._lock_item_reservations"), \
			 patch(f"{WEBHOOK}._evict_placeholder_hold", return_value=None), \
			 patch(f"{WEBHOOK}._queue_wc_order_note"), \
			 patch(f"{WEBHOOK}._log_error"):
			_apply_dealer_serial(_order(), row, "MOR0002985")
		self.assertTrue(mock_get.call_args.kwargs.get("for_update"))


# ---------------------------------------------------------------------------
# _serial_availability / _evict_blocking_hold — the order-CREATE path
# ---------------------------------------------------------------------------

class TestCreatePathPrecedence(FrappeTestCase):
	"""A brand-new order can arrive already carrying a staff-typed serial (the
	gun was picked before the webhook landed). That serial deserves the same
	precedence as one typed later — otherwise the order is born stuck."""

	def test_only_a_reservation_conflict_reports_a_blocking_order(self):
		"""Eviction is gated on this. Every other reason must report no blocker,
		or a mistyped serial takes a gun off an unrelated paid order."""
		from ffl_woo_sync.woocommerce.webhook import _serial_availability
		cases = [
			({"item_code": ITEM, "status": "Active", "woo_reserved": 1,
			  "woo_order": "WOO-ORD-H"}, "WOO-ORD-H"),
			({"item_code": "OTHER", "status": "Active", "woo_reserved": 1,
			  "woo_order": "WOO-ORD-H"}, ""),          # wrong model
			({"item_code": ITEM, "status": "Delivered", "woo_reserved": 1,
			  "woo_order": "WOO-ORD-H"}, ""),          # not in stock
			({"item_code": ITEM, "status": "Active", "woo_reserved": 1,
			  "woo_order": "WOO-ORD-H", "is_transfer_gun": 1}, ""),  # customer's gun
		]
		for locked, expected in cases:
			with self.subTest(locked=locked):
				with patch("frappe.db.get_value", return_value=locked), \
					 patch(f"{WEBHOOK}.custody_warehouse_names", return_value=set()):
					_reason, blocked_by = _serial_availability("SN-1", ITEM)
				self.assertEqual(blocked_by, expected)

	def test_a_free_serial_reports_neither_reason_nor_blocker(self):
		from ffl_woo_sync.woocommerce.webhook import _serial_availability
		with patch("frappe.db.get_value", return_value={
				"item_code": ITEM, "status": "Active", "woo_reserved": 0,
				"woo_order": ""}), \
			 patch(f"{WEBHOOK}.custody_warehouse_names", return_value=set()):
			self.assertEqual(_serial_availability("SN-1", ITEM), ("", ""))

	def test_the_thin_wrapper_still_answers_the_old_callers(self):
		from ffl_woo_sync.woocommerce.webhook import _serial_unavailable_reason
		with patch("frappe.db.get_value", return_value={
				"item_code": ITEM, "status": "Active", "woo_reserved": 0,
				"woo_order": ""}), \
			 patch(f"{WEBHOOK}.custody_warehouse_names", return_value=set()):
			self.assertEqual(_serial_unavailable_reason("SN-1", ITEM), "")

	def test_a_blocking_placeholder_is_evicted_for_a_new_order(self):
		from ffl_woo_sync.woocommerce.webhook import _evict_blocking_hold
		with patch("frappe.db.get_value", return_value={
				"woo_reserved": 1, "woo_order": "WOO-ORD-OTHER"}), \
			 patch(f"{WEBHOOK}._evict_placeholder_hold", return_value="X1") as mock_ev:
			self.assertEqual(_evict_blocking_hold("MOR0002985", ITEM), "X1")
		self.assertEqual(mock_ev.call_args[0][:3],
			("MOR0002985", ITEM, "WOO-ORD-OTHER"))
		# No new_owner: the order has no name until it is inserted.
		self.assertEqual(mock_ev.call_args.kwargs["new_owner"], "")

	def test_an_unreserved_gun_is_left_alone(self):
		from ffl_woo_sync.woocommerce.webhook import _evict_blocking_hold
		with patch("frappe.db.get_value", return_value={
				"woo_reserved": 0, "woo_order": ""}), \
			 patch(f"{WEBHOOK}._evict_placeholder_hold") as mock_ev:
			self.assertIsNone(_evict_blocking_hold("MOR0002985", ITEM))
		mock_ev.assert_not_called()

	def test_an_unknown_serial_is_left_to_the_validator(self):
		from ffl_woo_sync.woocommerce.webhook import _evict_blocking_hold
		with patch("frappe.db.get_value", return_value=None), \
			 patch(f"{WEBHOOK}._evict_placeholder_hold") as mock_ev:
			self.assertIsNone(_evict_blocking_hold("SN-GHOST", ITEM))
		mock_ev.assert_not_called()


# ---------------------------------------------------------------------------
# _pick_oldest_unreserved_serial — the exclusion that closes the loop
# ---------------------------------------------------------------------------

class TestPickerExclusion(FrappeTestCase):
	def test_an_excluded_serial_is_filtered_in_sql_not_skipped_after(self):
		"""Skipping it in the loop would eat one of the five candidate slots, so
		a shop with six free rifles could be told it had none."""
		from ffl_woo_sync.woocommerce.webhook import _pick_oldest_unreserved_serial
		with patch("frappe.get_all", return_value=[]) as mock_all, \
			 patch(f"{WEBHOOK}.custody_warehouse_names", return_value=set()):
			_pick_oldest_unreserved_serial(ITEM, exclude={"MOR0002985"})
		self.assertEqual(mock_all.call_args.kwargs["filters"]["name"],
			("not in", ["MOR0002985"]))

	def test_without_an_exclusion_the_filter_is_unchanged(self):
		from ffl_woo_sync.woocommerce.webhook import _pick_oldest_unreserved_serial
		with patch("frappe.get_all", return_value=[]) as mock_all, \
			 patch(f"{WEBHOOK}.custody_warehouse_names", return_value=set()):
			_pick_oldest_unreserved_serial(ITEM)
		self.assertNotIn("name", mock_all.call_args.kwargs["filters"])
