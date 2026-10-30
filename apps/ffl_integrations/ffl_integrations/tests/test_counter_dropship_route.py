"""Counter drop-ship routing (the ffl_counter_order_distributor hook).

The hook fires INSIDE ``create_order``'s transaction (after ``si.submit()+reload()``),
builds the Draft Distributor Order(s) the counter sale needs, and a raise rolls the
whole sale back. It reuses the web router's planner: a Sales-Invoice snapshot →
``router.plan_route`` → ``api.create_do_from_plan(source=("Sales Invoice", …))``.

Harness-safe here (pure / db-patchable): the snapshot builder, the ``is_paid``
override that lets a counter order route regardless of a Woo status it doesn't have,
and the paid-gate predicate. The actual DO creation through the state machine is
site-only (``test_counter_dropship_integration``), like the web router's shells.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ffl_integrations.distributor import api, counter, router

_dict = api.frappe._dict


class RouteVerdictIsPaidOverrideTest(unittest.TestCase):
	"""A counter order has no Woo status, so route_verdict must accept an explicit
	``is_paid`` (the DO is created regardless; the paid GATE is at Queue). The web
	callers keep falling back to woo_status."""

	def test_explicit_is_paid_true_routes_without_a_woo_status(self):
		v = router.route_verdict(woo_status=None, lanes=["direct"],
			ffl_captured=True, is_paid=True)
		self.assertTrue(v["route"])

	def test_explicit_is_paid_false_blocks(self):
		v = router.route_verdict(woo_status="processing", lanes=["direct"],
			ffl_captured=True, is_paid=False)
		self.assertEqual(v["reason"], router.REASON_UNPAID)

	def test_web_path_unchanged_when_is_paid_omitted(self):
		self.assertFalse(router.route_verdict(
			woo_status="pending", lanes=["fds"], ffl_captured=True)["route"])
		self.assertTrue(router.route_verdict(
			woo_status="completed", lanes=["direct"], ffl_captured=True)["route"])


class CounterSnapshotTest(unittest.TestCase):
	def _patch_reads(self, *, firearm):
		def _get_value(dt, name=None, field=None, *a, **k):
			if dt == "Item" and field == "dropship_distributor":
				return "RSR"
			if dt == "Item" and field == "is_firearm":
				return 1 if firearm else 0
			if dt == "Distributor Listing":
				return None  # no listing → sku falls back / mode default
			return None
		for p in (mock.patch.object(counter.frappe.db, "get_value", _get_value),
				mock.patch.object(counter.offers, "listing_for", lambda *a, **k: None)):
			p.start()
			self.addCleanup(p.stop)

	def _ctx(self, item_code, **over):
		ctx = {
			"lines": [{"item_code": item_code, "qty": 1, "rate": 500,
				"distributor": "RSR", "sku": "GLK19"}],
			"ship_to": {"line1": "1 Main", "city": "Austin", "state": "TX", "zip": "78701"},
			"destination_ffl": {"license": "1-23", "premise_state": "TX",
				"ffl_record": "ATF-1"},
			"buyer_type": "individual",
		}
		ctx.update(over)
		return ctx

	def test_accessory_line_is_a_direct_lane(self):
		self._patch_reads(firearm=False)
		snap = counter._counter_snapshot("SINV-1", self._ctx("RSR-ACC"))
		self.assertEqual(snap["doctype"], "Sales Invoice")
		self.assertEqual(snap["name"], "SINV-1")
		self.assertTrue(snap["is_paid"])                    # always routes; gate at Queue
		self.assertEqual(snap["lines"][0]["lane"], router.LANE_DIRECT)
		self.assertEqual(snap["lines"][0]["distributor"], "RSR")
		self.assertEqual(snap["ship_to"]["state"], "TX")    # the original ship-to

	def test_firearm_line_is_an_fds_lane(self):
		self._patch_reads(firearm=True)
		snap = counter._counter_snapshot("SINV-1", self._ctx("RSR-GUN"))
		self.assertEqual(snap["lines"][0]["lane"], router.LANE_FDS)
		self.assertEqual(snap["destination_ffl"]["license"], "1-23")

	def test_sku_uses_the_line_value_when_present(self):
		self._patch_reads(firearm=False)
		snap = counter._counter_snapshot("SINV-1", self._ctx("RSR-ACC"))
		self.assertEqual(snap["lines"][0]["sku"], "GLK19")


class CounterSnapshotLineHouseTest(unittest.TestCase):
	"""distributor-settings-layout E4 — a drop-ship line's SKU and fulfilment mode belong
	to the house the line names. Distributor Listing is autonamed by item, so the one
	row an Item has is the CLAIMING house's: an unscoped read hands another house's SKU
	(and mode) to this line. Reads are answered by their filters, never by position."""

	CLAIM = "Sports South"

	def setUp(self):
		self.resolved = []
		listing = {"sku": "SS-SKU", "fulfillment_mode": "To Store"}

		def _get_value(dt, name=None, field=None, *a, **k):
			if dt == "Item" and field == "dropship_distributor":
				return self.CLAIM
			if dt == "Item" and field == "is_firearm":
				return 0
			if dt == "Distributor Listing":
				scope = (name or {}).get("distributor")
				return listing.get(field) if scope in (None, self.CLAIM) else None
			return None

		def _resolve(item_code, distributor):
			self.resolved.append((item_code, distributor))
			return f"{distributor}-SKU"

		def _listing_for(item_code, distributor, fields=("sku",)):
			return {f: listing.get(f) for f in fields} if distributor == self.CLAIM else None

		for p in [
			mock.patch.object(counter.frappe.db, "get_value", _get_value),
			mock.patch.object(counter.offers, "listing_for", _listing_for),
			mock.patch.object(counter, "_planned_sku", _resolve, create=True),
		]:
			p.start()
			self.addCleanup(p.stop)

	def _line(self, **ln):
		snap = counter._counter_snapshot("SINV-1", {"lines": [
			{"item_code": "GEI-SPR", "qty": 1, "is_firearm": False, **ln}],
			"ship_to": {"line1": "1 Main", "city": "Austin", "state": "TX", "zip": "78701"}})
		return snap["lines"][0]

	def test_a_line_without_sku_resolves_its_own_house_sku(self):
		line = self._line(distributor="RSR")
		self.assertEqual(line["sku"], "RSR-SKU")
		self.assertEqual(self.resolved, [("GEI-SPR", "RSR")])
		self.assertEqual(line["distributor"], "RSR")

	def test_the_mode_is_read_for_the_line_house_only(self):
		# The claiming house lists it To Store; RSR has no listing → Direct.
		self.assertEqual(self._line(distributor="RSR", sku="R-1")["lane"],
			router.LANE_DIRECT)

	def test_the_claiming_house_keeps_its_own_listing_mode(self):
		self.assertEqual(self._line(distributor=self.CLAIM, sku="S-1")["lane"],
			router.LANE_TO_STORE)

	def test_a_line_naming_no_house_follows_the_claim(self):
		line = self._line()
		self.assertEqual(line["distributor"], self.CLAIM)
		self.assertEqual(self.resolved, [("GEI-SPR", self.CLAIM)])

	def test_the_line_sku_is_never_re_resolved(self):
		self.assertEqual(self._line(distributor="RSR", sku="R-9")["sku"], "R-9")
		self.assertEqual(self.resolved, [])

	def test_a_line_with_no_house_at_all_resolves_nothing(self):
		# A hand-built drop-ship row on an unstamped Item: no house to ask, and the
		# route refuses it as a local lane with its own message — not "None's catalog".
		self.CLAIM = None
		line = self._line()
		self.assertEqual((line["lane"], line["sku"]), (router.LANE_LOCAL, "GEI-SPR"))
		self.assertEqual(self.resolved, [])


class CounterHookIdempotencyTest(unittest.TestCase):
	"""Idempotency premise: re-firing the hook for a source that already has
	a live Distributor Order must create nothing. Closed by reusing the router's
	already-tested, split-safe `groups_to_create` — a naive per-plan dedup inside
	`create_do_from_plan` would collapse a legitimate multi-plan split (>400 SKU)
	into one order."""

	class _Provider:
		code = "rsr"

		def catalog_row(self, sku):
			return {"sku": sku, "cost": 10, "ffl_required": False, "department": "10"}

		def plan_fulfillment(self, lines, ctx):
			return [{"order_type": "Dropship Accessory", "account": "fulfillment",
				"fill_or_kill": True, "lines": [
					{"sku": ln["sku"], "qty": ln["qty"], "ship_method": "Grnd"}
					for ln in lines]}]

	def _fire(self, existing_orders):
		created = []

		def _get_value(dt, name=None, field=None, *a, **k):
			if dt == "Item" and field == "dropship_distributor":
				return "RSR"
			return None

		ps = [
			mock.patch.object(counter.frappe.db, "get_value", _get_value),
			mock.patch.object(counter.offers, "listing_for", lambda *a, **k: None),
			mock.patch.object(counter.frappe, "get_doc",
				lambda dt, n=None, *a, **k: _dict(name="RSR", provider="rsr")),
			mock.patch.object(api, "get_provider", lambda code: self._Provider()),
			mock.patch.object(router, "existing_orders",
				lambda dt, name: existing_orders),
			mock.patch.object(api, "create_do_from_plan",
				lambda *a, **k: created.append(k.get("source")) or _dict(name="DO-N")),
		]
		for p in ps:
			p.start()
			# Register each stop separately: unittest runs cleanups LIFO, which
			# unwinds overlapping patches correctly. Stopping them FIFO in one
			# cleanup restores a STUB as the 'original' when two patchers target
			# the same attribute — that leaked frappe.get_doc process-wide.
			self.addCleanup(p.stop)
		counter.ffl_counter_order_distributor("SINV-1", {
			"lines": [{"item_code": "RSR-ACC", "qty": 1, "rate": 40,
				"sku": "RSR-ACC", "distributor": "RSR", "is_firearm": False}],
			"ship_to": {"line1": "1 Main", "city": "Austin", "state": "TX", "zip": "78701"},
			"destination_ffl": None, "buyer_type": "Individual"})
		return created

	def test_first_fire_creates_the_order(self):
		self.assertEqual(len(self._fire([])), 1)

	def test_refire_with_a_live_order_creates_nothing(self):
		existing = [{"lane": router.LANE_DIRECT, "distributor": "RSR",
			"status": "Draft", "name": "DO-1"}]
		self.assertEqual(self._fire(existing), [])

	def test_a_cancelled_order_does_not_block_a_retry(self):
		existing = [{"lane": router.LANE_DIRECT, "distributor": "RSR",
			"status": "Cancelled", "name": "DO-1"}]
		self.assertEqual(len(self._fire(existing)), 1)


class CounterHookDeliveryOptionsTest(unittest.TestCase):
	"""Options picked per order in the plan reach the Distributor Order built for that
	order (matched by house | order type | firearm); a house without options gets none."""

	class _Provider(CounterHookIdempotencyTest._Provider):
		def __init__(self, supports):
			self.supports = supports

		def delivery_options(self, firearm_order):
			return {"signature": False, "adult_signature": False, "insurance": False} \
				if self.supports else None

	def _fire(self, *, supports, options):
		created = []

		def _get_value(dt, name=None, field=None, *a, **k):
			if dt == "Item" and field == "dropship_distributor":
				return "RSR"
			return None

		for p in [
			mock.patch.object(counter.frappe.db, "get_value", _get_value),
			mock.patch.object(counter.offers, "listing_for", lambda *a, **k: None),
			mock.patch.object(counter.frappe, "get_doc",
				lambda dt, n=None, *a, **k: _dict(name="RSR", provider="rsr")),
			mock.patch.object(api, "get_provider", lambda code: self._Provider(supports)),
			mock.patch.object(router, "existing_orders", lambda dt, name: []),
			mock.patch.object(api, "create_do_from_plan",
				lambda *a, **k: created.append(k.get("delivery_options")) or _dict(name="DO-N")),
		]:
			p.start()
			self.addCleanup(p.stop)
		counter.ffl_counter_order_distributor("SINV-1", {
			"lines": [{"item_code": "RSR-ACC", "qty": 1, "rate": 40,
				"sku": "RSR-ACC", "distributor": "RSR", "is_firearm": False}],
			"ship_to": {"line1": "1 Main", "city": "Austin", "state": "TX", "zip": "78701"},
			"destination_ffl": None, "buyer_type": "Individual",
			"delivery_options": options})
		return created

	def test_picked_options_reach_their_order(self):
		picked = {"signature": True, "adult_signature": False, "insurance": True}
		self.assertEqual(self._fire(supports=True,
			options={"RSR|Dropship Accessory|0": picked}), [picked])

	def test_an_order_without_an_entry_is_left_to_settings(self):
		self.assertEqual(self._fire(supports=True,
			options={"RSR|Dropship Firearm|1": {"signature": True}}), [None])

	def test_a_house_without_options_ignores_them(self):
		self.assertEqual(self._fire(supports=False,
			options={"RSR|Dropship Accessory|0": {"signature": True}}), [None])


class FillOrKillTest(unittest.TestCase):
	"""The counter default is FillOrKill=**Y** (the
	customer is standing there, so a short-ship is re-quoted on the spot rather
	than back-ordered). The counter snapshot must carry it all the way into the plan;
	the web default (N — short-ship alerts + partial refund) is unchanged."""

	def test_counter_snapshot_asks_for_fill_or_kill(self):
		with mock.patch.object(counter.frappe.db, "get_value", lambda *a, **k: None), \
				mock.patch.object(counter.offers, "listing_for", lambda *a, **k: None):
			snap = counter._counter_snapshot("SINV-1", {
				"lines": [{"item_code": "A", "qty": 1,
					"distributor": "RSR", "is_firearm": False, "sku": "A"}]})
		self.assertTrue(snap["fill_or_kill"])

	def test_ship_context_carries_it(self):
		ctx = router.build_ship_context(router.LANE_DIRECT, fill_or_kill=True)
		self.assertTrue(ctx["fill_or_kill"])

	def test_web_default_stays_false(self):
		self.assertFalse(router.build_ship_context(router.LANE_DIRECT)["fill_or_kill"])

	def test_plan_route_propagates_it_into_each_group(self):
		snap = {"woo_status": None, "is_paid": True, "fill_or_kill": True,
			"ship_to": {"line1": "1", "city": "c", "state": "TX", "zip": "1"},
			"destination_ffl": None,
			"lines": [{"sku": "A", "item_code": "A", "qty": 1,
				"lane": router.LANE_DIRECT, "distributor": "RSR"}]}
		plan = router.plan_route(snap)
		self.assertTrue(plan["groups"][0]["ship_context"]["fill_or_kill"])


class DropshipWarehouseRepinHookTest(unittest.TestCase):
	"""The contract was broken on a real bench: `set_pos_fields`
	runs inside the controller's own validate, i.e. AFTER create_order's manual
	re-pin, and refilled `warehouse` on the drop-ship rows. Fixed the same way the PI's
	SRBNB overwrite was fixed: a hooked `validate` doc_event, which frappe runs
	AFTER the controller's — so this is the durable last word."""

	def _doc(self, rows):
		return types.SimpleNamespace(items=[_dict(**r) for r in rows])

	def test_dropship_row_warehouse_is_cleared(self):
		doc = self._doc([{"item_code": "RSR-ACC", "delivered_by_supplier": 1,
			"dropship_distributor": "RSR", "warehouse": "Stores - FT"}])
		counter.repin_dropship_lines(doc)
		self.assertFalse(doc.items[0].warehouse)

	def test_local_row_keeps_its_warehouse(self):
		doc = self._doc([{"item_code": "LOCAL", "delivered_by_supplier": 0,
			"warehouse": "Stores - FT"}])
		counter.repin_dropship_lines(doc)
		self.assertEqual(doc.items[0].warehouse, "Stores - FT")

	def test_procure_to_store_row_keeps_its_warehouse(self):
		# A distributor-sourced row that IS received into our inventory.
		doc = self._doc([{"item_code": "TS", "delivered_by_supplier": 0,
			"dropship_distributor": "RSR", "warehouse": "Stores - FT"}])
		counter.repin_dropship_lines(doc)
		self.assertEqual(doc.items[0].warehouse, "Stores - FT")


class CancelOrderAlertTest(unittest.TestCase):
	"""Cancelling never reaches the house: a LIVE order alerts ops to phone it in —
	unless the operator records that the distributor already confirmed."""

	def _do(self, status):
		doc = types.SimpleNamespace(name="DO-1", status=status, hold_reason=None, distributor="RSR")
		doc.save = lambda: None
		return doc

	def _cancel(self, status, **kw):
		import ffl_integrations.notifications as notifications
		doc = self._do(status)
		with mock.patch.object(api.frappe, "only_for"), \
				mock.patch.object(api.frappe, "get_doc", return_value=doc), \
				mock.patch.object(notifications, "notify_admins") as alert:
			out = api.cancel_order("DO-1", **kw)
		return doc, out, alert

	def test_live_order_cancelled_unconfirmed_alerts_ops(self):
		doc, out, alert = self._cancel("Placed", reason="customer backed out")
		self.assertEqual(out["status"], "Cancelled")
		self.assertTrue(alert.called)
		self.assertEqual(doc.hold_reason, "customer backed out")

	def test_live_order_cancelled_with_distributor_confirmation_is_quiet(self):
		doc, _out, alert = self._cancel("Acknowledged", confirmed=1)
		self.assertFalse(alert.called)
		self.assertIn("confirmed the cancellation", doc.hold_reason)

	def test_error_order_cancelled_unconfirmed_alerts_ops(self):
		# An Error order may have reached the house before it failed (LIVE_AT_DISTRIBUTOR).
		_doc, _out, alert = self._cancel("Error")
		self.assertTrue(alert.called)

	def test_draft_cancel_never_alerts(self):
		_doc, _out, alert = self._cancel("Draft")
		self.assertFalse(alert.called)


class PaidGateTest(unittest.TestCase):
	"""The counter paid-gate is the LEG's: an SI-sourced drop-ship DO can only
	leave Draft when its Sales Invoice is fully paid (a deposit counter order sits
	in the queue until the balance is recorded); a ship-to-store DO may be placed
	on a deposit or unpaid — its goods come to our own shelf."""

	def test_unpaid_dropship_source_is_not_placeable(self):
		with mock.patch.object(api.frappe.db, "get_value", lambda *a, **k: 250.0):
			self.assertIn("not paid in full", api.placement_payment_block(_dict(
				source_doctype="Sales Invoice", source_name="SINV-1",
				order_type="Dropship Firearm")))

	def test_fully_paid_dropship_source_is_placeable(self):
		with mock.patch.object(api.frappe.db, "get_value", lambda *a, **k: 0.0):
			self.assertIsNone(api.placement_payment_block(_dict(
				source_doctype="Sales Invoice", source_name="SINV-1",
				order_type="Dropship Accessory")))

	def test_unpaid_ship_to_store_source_is_placeable(self):
		# Never even reads the invoice: the rule is decided by the leg type.
		with mock.patch.object(api.frappe.db, "get_value",
				side_effect=AssertionError("must not read the invoice")):
			self.assertIsNone(api.placement_payment_block(_dict(
				source_doctype="Sales Invoice", source_name="SINV-1",
				order_type="Procure To Store")))

	def test_leg_rule_by_order_type(self):
		self.assertTrue(api.leg_needs_full_payment("Dropship Firearm"))
		self.assertTrue(api.leg_needs_full_payment("Dropship Accessory"))
		self.assertFalse(api.leg_needs_full_payment("Procure To Store"))
		self.assertTrue(api.leg_needs_full_payment(None))   # unknown → fail closed

	def test_non_counter_source_is_always_placeable(self):
		# A web (Woo) DO has its own paid gate at route time; this gate is a no-op.
		self.assertIsNone(api.placement_payment_block(
			_dict(source_doctype="Woo Online Order", source_name="WOO-1",
				order_type="Dropship Firearm")))


if __name__ == "__main__":
	unittest.main()
