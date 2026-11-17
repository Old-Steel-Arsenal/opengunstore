"""Pending Order stages: one row → the one thing it is
waiting for. Pure: rows are the dicts the page receives."""
from __future__ import annotations

import unittest

from ffl_core.api.manual_order import (STAGE_ORDER, age_days, blocked_reason, digest_lines,
	queue_stage, stamp_stage)


def _legs(**kw):
	base = {"local": {"undelivered": 0, "pending_serials": 0}, "awaiting_arrival": 0,
		"awaiting_goods": False, "awaiting_dropship": False, "distributor_orders": [],
		"releasable": False, "trouble": [], "substitutable": []}
	base.update(kw)
	return base


def _row(**kw):
	base = {"paid": True, "is_business": False, "ffl_expired": False, "destination_ffl": None,
		"has_ship_address": True, "needs_disposition": False, "disposed": False,
		"ship_pushed": False, "pending_serials": [], "can_mark_shipped": True,
		"serial_errors": [], "legs": _legs()}
	base.update(kw)
	return base


class Stages(unittest.TestCase):
	def test_the_six_sections_in_page_order(self):
		self.assertEqual(STAGE_ORDER, ("blocked", "to_place", "waiting", "ready_ship",
			"ready_pickup", "disposing"))

	def test_trouble_leg_is_blocked_first(self):
		row = _row(legs=_legs(trouble=[{"kind": "killed", "distributor": "RSR", "detail": "GLK"}],
			releasable=True))
		self.assertEqual(queue_stage(row), "blocked")
		self.assertEqual(blocked_reason(row), "RSR killed GLK")

	def test_expired_ffl_is_blocked(self):
		self.assertEqual(queue_stage(_row(destination_ffl="1-23", ffl_expired=True)), "blocked")

	def test_unpaid_individual_shipment_is_blocked(self):
		row = _row(paid=False)
		self.assertEqual(queue_stage(row), "blocked")
		self.assertIn("Unpaid", blocked_reason(row))

	def test_unpaid_business_shipment_is_not_blocked(self):
		self.assertEqual(queue_stage(_row(paid=False, is_business=True)), "ready_ship")

	def test_unpaid_pickup_is_not_blocked(self):
		"""A pickup does not need to be paid to be ready."""
		self.assertEqual(queue_stage(_row(paid=False, has_ship_address=False)), "ready_pickup")

	def test_draft_legs_are_to_place(self):
		row = _row(legs=_legs(releasable=True,
			distributor_orders=[{"status": "Draft", "order_type": "Procure To Store"}]))
		self.assertEqual(queue_stage(row), "to_place")

	def test_a_web_row_with_an_unplaced_leg_is_to_place(self):
		"""A web row carries no ``legs``; its builder stamps ``to_place`` instead."""
		row = _row(source="woo", legs=None, to_place=True, needs_disposition=True)
		self.assertEqual(queue_stage(row), "to_place")

	def test_unpaid_dropship_legs_are_blocked_not_to_place(self):
		"""Place order refuses an unpaid DROP-SHIP leg (business or pickup alike),
		so the row must say why instead of sitting in To place with no button."""
		for extra in ({"is_business": True}, {"has_ship_address": False}):
			row = _row(paid=False, legs=_legs(releasable=False,
				distributor_orders=[{"status": "Draft", "order_type": "Dropship Accessory"}]), **extra)
			self.assertEqual(queue_stage(row), "blocked")
			self.assertIn("settle the balance", blocked_reason(row))

	def test_unpaid_ship_to_store_leg_is_to_place(self):
		"""Goods ordered to the store may be placed on a
		deposit or unpaid — the balance is collected at pickup, not before ordering."""
		row = _row(paid=False, has_ship_address=False, legs=_legs(releasable=True,
			distributor_orders=[{"status": "Draft", "order_type": "Procure To Store"}]))
		self.assertEqual(blocked_reason(row), "")
		self.assertEqual(queue_stage(row), "to_place")

	def test_goods_on_the_way_are_waiting(self):
		self.assertEqual(queue_stage(_row(legs=_legs(awaiting_goods=True))), "waiting")
		self.assertEqual(queue_stage(_row(legs=_legs(awaiting_arrival=1))), "waiting")
		self.assertEqual(queue_stage(_row(legs=_legs(awaiting_dropship=True))), "waiting")

	def test_a_failed_placement_is_trouble_not_waiting(self):
		row = _row(legs=_legs(trouble=[{"kind": "error", "distributor": "RSR", "detail": "401"}]))
		self.assertEqual(queue_stage(row), "blocked")
		self.assertEqual(blocked_reason(row), "RSR placement failed 401")

	def test_shipped_on_account_with_nothing_to_dispose_is_blocked_on_payment(self):
		row = _row(paid=False, is_business=True, ship_pushed=True, needs_disposition=False)
		self.assertEqual(queue_stage(row), "blocked")
		self.assertIn("record payment", blocked_reason(row))

	def test_all_here_with_address_is_ready_ship(self):
		self.assertEqual(queue_stage(_row(needs_disposition=True)), "ready_ship")
		self.assertEqual(queue_stage(_row(has_ship_address=False, destination_ffl="1-23")), "ready_ship")

	def test_all_here_without_address_is_ready_pickup(self):
		self.assertEqual(queue_stage(_row(has_ship_address=False)), "ready_pickup")

	def test_shipped_but_undisposed_is_disposing(self):
		self.assertEqual(queue_stage(_row(ship_pushed=True, needs_disposition=True)), "disposing")

	def test_nothing_blocked_reads_empty(self):
		self.assertEqual(blocked_reason(_row()), "")


class AgeDays(unittest.TestCase):
	def test_unknown_date_is_zero(self):
		self.assertEqual(age_days(None), 0)
		self.assertEqual(age_days(""), 0)

	def test_a_past_date_counts_whole_days(self):
		from frappe.utils import add_days, nowdate
		self.assertEqual(age_days(add_days(nowdate(), -9)), 9)
		self.assertEqual(age_days(nowdate()), 0)


class Digest(unittest.TestCase):
	def test_blocked_and_overdue_rows_are_listed_once_each(self):
		rows = [_row(stage="blocked", blocked_reason="RSR killed GLK", customer_name="A",
				sales_invoice="SI-1", age_days=1),
			_row(stage="waiting", customer_name="B", sales_invoice="SI-2", age_days=9),
			_row(stage="ready_ship", customer_name="C", sales_invoice="SI-3", age_days=2)]
		lines = digest_lines(rows, 7)
		self.assertEqual(len(lines), 2)
		self.assertIn("SI-1", lines[0])
		self.assertIn("RSR killed GLK", lines[0])
		self.assertIn("SI-2", lines[1])
		self.assertIn("9", lines[1])

	def test_nothing_stuck_means_no_lines(self):
		self.assertEqual(digest_lines([_row(stage="ready_ship", age_days=1)], 7), [])


class WebAndGunBrokerStages(unittest.TestCase):
	"""The same rulebook over the other two sources' row shapes."""

	def test_web_rows(self):
		base = {"source": "woo", "paid": True, "is_business": False, "destination_ffl": "1-23",
			"has_ship_address": True, "serial_errors": [], "ffl_expired": False,
			"awaiting_serial": False, "to_store": False,
			"arrived": False, "store_pickup": False, "ship_pushed": False, "legs": None}
		self.assertEqual(queue_stage({**base, "serial_errors": [{"error": "x"}]}), "blocked")
		self.assertEqual(queue_stage({**base, "ffl_expired": True}), "blocked")
		self.assertEqual(blocked_reason({**base, "ffl_expired": True}), "FFL expired")
		self.assertEqual(queue_stage({**base, "awaiting_serial": True}), "waiting")
		self.assertEqual(queue_stage({**base, "to_store": True}), "waiting")
		self.assertEqual(queue_stage({**base, "to_store": True, "arrived": True}), "ready_ship")
		self.assertEqual(queue_stage({**base, "store_pickup": True}), "ready_pickup")
		self.assertEqual(queue_stage(base), "ready_ship")

	def test_gunbroker_rows(self):
		base = {"source": "gunbroker", "paid": True, "is_business": False,
			"destination_ffl": "1-23", "has_ship_address": True, "serial_errors": [],
			"ffl_expired": False, "awaiting_payment": False,
			"needs_disposition": True, "ship_pushed": False, "store_pickup": False, "legs": None}
		self.assertEqual(queue_stage({**base, "awaiting_payment": True, "paid": False}), "blocked")
		self.assertEqual(blocked_reason({**base, "awaiting_payment": True, "paid": False}),
			"Awaiting payment")
		self.assertEqual(queue_stage({**base, "ffl_expired": True}), "blocked")
		self.assertEqual(queue_stage({**base, "serial_errors": [{"error": "x"}]}), "blocked")
		self.assertEqual(queue_stage(base), "ready_ship")
		self.assertEqual(queue_stage({**base, "ship_pushed": True}), "disposing")

	def test_stamp_stage_marks_overdue_against_the_limit(self):
		from frappe.utils import add_days, nowdate
		row = stamp_stage(_row(), add_days(nowdate(), -8), 7)
		self.assertEqual((row["stage"], row["age_days"], row["overdue"]), ("ready_ship", 8, True))
		self.assertFalse(stamp_stage(_row(), nowdate(), 7)["overdue"])
