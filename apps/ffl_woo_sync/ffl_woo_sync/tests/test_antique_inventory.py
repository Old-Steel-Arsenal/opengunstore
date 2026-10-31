"""An antique is GCA-exempt but still a serialized gun on our shelf.

The previous change (``test_antique_no_ffl``) correctly took antiques OUT of the ATF
machinery: no ``_firearm_product`` flag on the storefront, no bound-book entry, no
Dispose click.  It did that by gating the webhook's serial-row writer on
``is_regulated_firearm_item``, and that is where it went one step too far — the
serial row is not an ATF device.  It carries three things the bound book has no
part in:

1. the **reservation** that stops two shoppers buying the same gun,
2. the **serial the Sales Invoice needs** on its line, and
3. the **target** the listing archive/delist acts on.

So an antique sale reserved nothing, booked NO revenue at all (``Sales Invoice``
refuses an ``is_firearm`` line with no serial — and antiques are ``is_firearm=1``,
``is_antique=1``, so nothing exempted them), never relieved stock, and stayed on
sale after it shipped.  The original order was all four at once: $1,840 collected, no
invoice, the rifle still ``Active`` in ``Stores - <ABBR>``, and its listing still
buyable at $1,795.

The fix restores the serial row for antiques (``is_firearm_item`` — the exact
complement of the old test) and routes their retirement through
``antique.retire_antique_web_order`` instead of a disposition.

**Legacy shapes still exist.** Antique orders created BEFORE the fix have no serial
row at all, so the "no serial rows + antique line" cases in ``test_antique_no_ffl``
remain live production shapes and are not superseded by anything here.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

ANTIQUE = "Finnish M39 Antique"
MODERN = "GLOCK-19"


def _order_doc(woo_order_id="4201"):
	"""A pre-insert order doc, the shape ``_process_lines`` mutates."""
	doc = MagicMock()
	doc.name = None  # _process_lines runs BEFORE insert
	doc.woo_order_id = woo_order_id
	doc.lines = []
	doc.serials = []

	def _append(table, row):
		getattr(doc, table).append(frappe._dict(row))
		return frappe._dict(row)

	doc.append.side_effect = _append
	return doc


def _wc_line(sku, line_id=7, qty=1, total="1795"):
	return {"id": line_id, "sku": sku, "quantity": qty, "price": total,
		"subtotal": total, "total": total, "total_tax": "0", "meta_data": []}


def _srow(item_code=ANTIQUE, confirmed_serial="205745", lane="local", woo_line_id="7"):
	return frappe._dict({
		"item_code": item_code, "confirmed_serial": confirmed_serial,
		"woo_line_id": woo_line_id, "unit_index": 0, "stock_hold_serial": "",
		"reservation_error": "", "fulfillment": lane,
	})


# ===========================================================================
# 1. The root fix — an antique gets a serial row again
# ===========================================================================

class TestAntiqueGetsASerialRow(FrappeTestCase):

	def _run(self, sku, *, is_regulated, is_firearm):
		from ffl_woo_sync.woocommerce import webhook
		order = _order_doc()
		with patch.object(webhook, "is_regulated_firearm_item", return_value=is_regulated), \
			 patch.object(webhook, "is_firearm_item", return_value=is_firearm), \
			 patch.object(webhook, "_line_source", return_value={
				"lane": webhook.LANE_LOCAL, "distributor": "", "sku": ""}), \
			 patch.object(webhook, "_serial_availability", return_value=("", "")), \
			 patch.object(webhook, "_log_error"):
			to_reserve = webhook._process_lines(order, [_wc_line(sku)])
		return order, to_reserve

	def test_an_antique_line_writes_a_serial_row_and_reserves_the_gun(self):
		"""THE fix. Antique = not regulated, but IS a firearm → serial row + reservation.

		The reservation is what stops the double-sell: without it two shoppers could
		both buy Woo product 1671 because nothing ever dropped its stock to 0.
		"""
		order, to_reserve = self._run(
			f"{ANTIQUE}::205745", is_regulated=False, is_firearm=True)
		self.assertEqual(len(order.serials), 1, "an antique unit must have a serial row")
		row = order.serials[0]
		self.assertEqual(row["item_code"], ANTIQUE)
		self.assertEqual(row["confirmed_serial"], "205745",
			"the per-gun SKU's serial must land on the row — the Sales Invoice needs it")
		self.assertEqual(row["fulfillment"], "local")
		self.assertEqual(to_reserve, ["205745"], "the gun must be soft-reserved")

	def test_an_accessory_still_writes_no_serial_row(self):
		"""Regression: the gate narrowed by exactly one case (antiques) and no more."""
		order, to_reserve = self._run(
			"ACC-SLING", is_regulated=False, is_firearm=False)
		self.assertEqual(order.serials, [])
		self.assertEqual(to_reserve, [])
		self.assertEqual(len(order.lines), 1, "the commerce line is still recorded")

	def test_a_regulated_firearm_is_unchanged(self):
		"""Regression: an ordinary gun behaved correctly before and must still."""
		order, to_reserve = self._run(
			f"{MODERN}::SN-9", is_regulated=True, is_firearm=True)
		self.assertEqual(len(order.serials), 1)
		self.assertEqual(to_reserve, ["SN-9"])

	def test_the_lane_still_uses_the_ATF_test_not_the_inventory_one(self):
		"""The drop-ship lane decides who ships a REGULATED gun and to whose FFL — an
		antique has no part in that, so the lane keeps reading is_regulated. Pinning
		this stops a later 'tidy-up' from collapsing the two tests into one."""
		from ffl_woo_sync.woocommerce import webhook
		order = _order_doc()
		with patch.object(webhook, "is_regulated_firearm_item", return_value=False), \
			 patch.object(webhook, "is_firearm_item", return_value=True), \
			 patch.object(webhook, "_line_source") as lane, \
			 patch.object(webhook, "_serial_availability", return_value=("", "")), \
			 patch.object(webhook, "_log_error"):
			lane.return_value = {"lane": webhook.LANE_LOCAL, "distributor": "", "sku": ""}
			webhook._process_lines(order, [_wc_line(f"{ANTIQUE}::205745")])
		self.assertIs(lane.call_args.kwargs["is_firearm"], False,
			"_line_source must be told the ANTIQUE is not a regulated firearm")


# ===========================================================================
# 2. Auto-clear — an antique owes no disposition, everything else still does
# ===========================================================================

class TestAutoClearSeesAntiquesAsActionless(FrappeTestCase):

	def _clear(self, serials, *, woo_status="completed", antiques=(ANTIQUE,)):
		from ffl_woo_sync.woocommerce import webhook
		doc = frappe._dict({
			"order_status": "Pending Fulfillment", "woo_status": woo_status,
			"serials": list(serials), "dropship_status": "",
		})
		with patch.object(webhook, "is_antique_item",
				side_effect=lambda ic: ic in antiques):
			changed = webhook._auto_clear_non_firearm_if_completed(doc)
		return changed, doc

	def test_an_antique_only_order_auto_clears(self):
		"""Now that antiques HAVE serial rows, the old `if order_doc.serials` gate would
		have blocked this forever — no auto-clear, so no revenue, ever."""
		changed, doc = self._clear([_srow()])
		self.assertTrue(changed)
		self.assertEqual(doc.order_status, "Transferred")

	def test_a_regulated_unit_still_blocks_auto_clear(self):
		"""Regression, and the most important one here: a real gun must NEVER be
		retired without a disposition."""
		changed, doc = self._clear([_srow(item_code=MODERN, confirmed_serial="SN-9")])
		self.assertFalse(changed)
		self.assertEqual(doc.order_status, "Pending Fulfillment")

	def test_a_mixed_order_still_blocks_auto_clear(self):
		"""One regulated unit alongside an antique keeps the whole order on Dispose."""
		changed, _ = self._clear([_srow(), _srow(item_code=MODERN, confirmed_serial="SN-9")])
		self.assertFalse(changed)

	def test_an_accessory_only_order_is_unchanged(self):
		"""The original #8 behaviour: no serial rows at all still auto-clears."""
		changed, doc = self._clear([])
		self.assertTrue(changed)
		self.assertEqual(doc.order_status, "Transferred")

	def test_an_uncompleted_antique_order_does_not_clear(self):
		changed, _ = self._clear([_srow()], woo_status="processing")
		self.assertFalse(changed)


# ===========================================================================
# 3. Routing — an antique order needs stock+listing, not just an invoice
# ===========================================================================

class TestAutoClearRoutesAntiquesToRetirement(FrappeTestCase):

	def _enqueued(self, serials, antiques=(ANTIQUE,)):
		from ffl_woo_sync.woocommerce import webhook
		order = frappe._dict({
			"name": "WOO-ORD-01379", "order_status": "Transferred",
			"serials": list(serials),
		})
		# The antique-vs-revenue branch lives in revenue.web_completion_job now
		# (webhook enqueues, pickup + backstop run it inline) and reads the
		# firearm predicate from its home module.
		with patch("ffl_core.firearm.is_antique_item",
				side_effect=lambda ic: ic in antiques), \
			 patch("frappe.enqueue") as enq:
			webhook._book_revenue_after_auto_clear(order)
		return enq

	def test_an_antique_order_is_routed_through_retirement(self):
		"""Booking revenue ALONE would leave the gun in stock and the page on sale —
		exactly the half-fix that made the original order double-sellable."""
		enq = self._enqueued([_srow()])
		self.assertEqual(enq.call_count, 1)
		self.assertEqual(enq.call_args.args[0],
			"ffl_woo_sync.woocommerce.antique.retire_antique_web_order")

	def test_an_accessory_order_still_goes_straight_to_revenue(self):
		"""Regression: an accessory has no stock of ours to move."""
		enq = self._enqueued([])
		self.assertEqual(enq.call_args.args[0],
			"ffl_woo_sync.woocommerce.revenue.create_sales_invoice_for_web_order")

	def test_a_non_transferred_order_enqueues_nothing(self):
		from ffl_woo_sync.woocommerce import webhook
		order = frappe._dict({"name": "X", "order_status": "Pending Fulfillment",
			"serials": [_srow()]})
		with patch("frappe.enqueue") as enq:
			webhook._book_revenue_after_auto_clear(order)
		self.assertFalse(enq.called)


# ===========================================================================
# 4. Retirement itself — the three steps, and their ORDER
# ===========================================================================

class TestRetireAntiqueWebOrder(FrappeTestCase):

	def _order(self, serials=None):
		return frappe._dict({
			"name": "WOO-ORD-01379", "woo_order_id": "4201", "site": "retail",
			"order_status": "Transferred",
			"serials": list(serials) if serials is not None else [_srow()],
			"lines": [frappe._dict({"item_code": ANTIQUE, "woo_line_id": "7", "qty": 1,
				"line_total": 1795.0, "unit_price": 1795.0})],
		})

	def _run(self, order, *, sn_status="Active", sn_warehouse="Stores - TSA",
			sn_item_code=ANTIQUE):
		"""Run retirement with every sink recorded in call order."""
		from ffl_woo_sync.woocommerce import antique, fulfillment, revenue
		calls: list[str] = []

		def _issue(item_code, serial, warehouse, remarks=None):
			calls.append("issue")
			return "MAT-STE-0001"

		def _delist(disposed, site, sold_prices=None):
			calls.append("delist")

		def _invoice(o):
			calls.append("invoice")
			return {"sales_invoice": "ACC-SINV-1", "status": "created"}

		row = (frappe._dict({"item_code": sn_item_code, "warehouse": sn_warehouse,
			"status": sn_status}) if sn_status else None)
		real_get_value = frappe.db.get_value

		def _get_value(doctype, *args, **kwargs):
			if doctype == "Serial No":
				return row
			# Forward verbatim: Frappe's own set_value path calls get_value with
			# `filters=` as a KEYWORD, and a shim that re-sent `name` positionally
			# collided with it ("got multiple values for argument 'filters'").
			return real_get_value(doctype, *args, **kwargs)

		with patch.object(antique, "is_antique_item",
				side_effect=lambda ic: ic == ANTIQUE), \
			 patch("ffl_core.firearm.is_antique_item",
				side_effect=lambda ic: ic == ANTIQUE), \
			 patch("ffl_core.firearm.issue_serial", side_effect=_issue) as issue, \
			 patch.object(fulfillment, "_delist_disposed", side_effect=_delist) as delist, \
			 patch.object(revenue, "create_sales_invoice_for_web_order",
				side_effect=_invoice) as invoice, \
			 patch("frappe.db.get_value", side_effect=_get_value):
			result = antique.retire_antique_web_order(order)
		return result, calls, issue, delist, invoice

	def test_stock_leaves_before_the_listing_is_retired_and_revenue_is_last(self):
		"""THE ordering invariant, and it is not cosmetic.

		``route_serial_exit`` sends a serial to the SOLD archive only when its status is
		in SOLD_STATUS (Delivered/Consumed), and the Material Issue is what makes it
		Consumed. Retire the listing first and the gun is still Active, so it routes to
		a plain delist (404 instead of the SOLD page that keeps its rankings) — and the
		2-hourly reconcile sweep, seeing an active serial against a stock-0 listing,
		reads it as drift and pushes the sold gun BACK ON SALE.

		Revenue is last because it is the step most likely to throw; running it first
		would roll back the stock issue with the gun already shipped.
		"""
		_, calls, _, _, _ = self._run(self._order())
		self.assertEqual(calls, ["issue", "delist", "invoice"])

	def test_the_gun_is_issued_out_of_its_own_warehouse(self):
		_, _, issue, _, _ = self._run(self._order())
		self.assertEqual(issue.call_args.args, (ANTIQUE, "205745", "Stores - TSA"))
		self.assertIn("4201", issue.call_args.kwargs["remarks"],
			"the audit note must name the order that took the gun")

	def test_the_listing_is_retired_with_the_realized_price(self):
		"""The SOLD page shows 'Sold for $X' — it must be the real figure, so the
		realized price is threaded through exactly as the disposition path does."""
		_, _, _, delist, _ = self._run(self._order())
		self.assertEqual(delist.call_args.args[0], [(ANTIQUE, "205745")])
		self.assertEqual(delist.call_args.args[1], "retail")
		self.assertEqual(delist.call_args.args[2], {"205745": 1795.0})

	def test_an_already_issued_gun_is_not_issued_twice_but_is_still_retired(self):
		"""Idempotency on retry. The stock guard mirrors
		disposition_stock.issue_stock_on_submit: a serial that is no longer Active has
		already left. Its LISTING may still be live though, so it must keep flowing to
		the retirement step — otherwise a retry after a mid-way failure would leave a
		sold gun on sale forever."""
		_, calls, issue, delist, _ = self._run(self._order(), sn_status="Consumed")
		self.assertFalse(issue.called, "must not double-issue")
		self.assertTrue(delist.called, "the listing still has to be retired")
		self.assertEqual(delist.call_args.args[0], [(ANTIQUE, "205745")])
		self.assertEqual(calls, ["delist", "invoice"])

	def test_revenue_is_booked_through_the_production_entry_point(self):
		result, _, _, _, invoice = self._run(self._order())
		self.assertTrue(invoice.called)
		self.assertEqual(result["revenue"]["sales_invoice"], "ACC-SINV-1")

	def test_a_unit_with_no_serial_is_skipped_rather_than_invented(self):
		"""A blank serial means the reservation never resolved. There is nothing to
		issue and nothing to guess — and crucially no listing action either, or we would
		retire a page for a gun we cannot name."""
		_, calls, issue, delist, _ = self._run(
			self._order([_srow(confirmed_serial="")]))
		self.assertFalse(issue.called)
		self.assertFalse(delist.called)
		self.assertEqual(calls, ["invoice"])

	def test_a_distributor_held_antique_is_not_issued_from_our_stock(self):
		"""A drop-shipped unit was never in our warehouse — issuing it would book a
		stock exit for a gun we never held."""
		_, _, issue, delist, _ = self._run(self._order([_srow(lane="fds")]))
		self.assertFalse(issue.called)
		self.assertFalse(delist.called)

	def test_a_regulated_unit_is_never_touched_by_this_path(self):
		"""Belt and braces: retirement must not silently retire a real gun that owes a
		disposition, even if it somehow reaches here on a mixed order."""
		_, _, issue, _, _ = self._run(
			self._order([_srow(item_code=MODERN, confirmed_serial="SN-9")]))
		self.assertFalse(issue.called)

	def test_a_regulated_serial_on_an_antique_row_is_refused_not_issued(self):
		"""The row says antique but the serial belongs to a regulated gun (a
		mismatched reservation or staff pick). Issuing it here would take a real
		gun out of stock with no FFL Disposition — a hole in the A&D book. The
		canonical antique exit refuses it before anything is issued or delisted."""
		with self.assertRaises(frappe.ValidationError):
			self._run(self._order(), sn_item_code=MODERN)

	def test_a_non_transferred_order_is_refused(self):
		from ffl_woo_sync.woocommerce import antique
		order = self._order()
		order.order_status = "Pending Fulfillment"
		with self.assertRaises(frappe.ValidationError):
			antique.retire_antique_web_order(order)

	def test_only_the_role_gated_entry_point_is_whitelisted(self):
		"""Retirement moves stock and cuts an invoice. The worker reaches it by dotted
		path and a human reaches it through the role-gated wrapper, so the unguarded
		function must NOT be callable over HTTP — otherwise any authenticated session,
		a dealer-portal login included, could issue guns out of our inventory."""
		from ffl_woo_sync.woocommerce import antique

		# frappe.whitelisted is a SET of function objects — @frappe.whitelist() adds to
		# it and sets no attribute on the function. A getattr(fn, "whitelisted", False)
		# probe therefore reads False for EVERYTHING, so the negative assertion below
		# would pass no matter what and could never catch a regression. Assert against
		# the real registry.
		self.assertNotIn(
			antique.retire_antique_web_order, frappe.whitelisted,
			"retire_antique_web_order must not be whitelisted — it has no role check")
		self.assertIn(
			antique.retire_antique_web_order_now, frappe.whitelisted,
			"the role-gated retry wrapper is the intended HTTP entry point")


# ===========================================================================
# 5. Dispose must never write an antique into the bound book
# ===========================================================================

class TestDisposeRefusesAntiques(FrappeTestCase):

	def _dispose(self, serials):
		from ffl_woo_sync.woocommerce import fulfillment
		order = frappe._dict({
			"name": "WOO-ORD-01379", "woo_order_id": "4201", "site": "retail",
			"order_status": "Pending Fulfillment", "woo_status": "completed",
			"serials": list(serials), "lines": [],
		})
		with patch("frappe.only_for"), \
			 patch("frappe.get_doc", return_value=order), \
			 patch.object(fulfillment, "is_antique_item",
				side_effect=lambda ic: ic == ANTIQUE):
			return fulfillment.dispose_web_order("WOO-ORD-01379")

	def test_an_antique_only_order_is_refused_with_an_accurate_reason(self):
		"""Booking an FFL Disposition for an antique would write into the A&D book a
		gun that does not exist in ATF's eyes — the one thing this must never do.

		The message matters: the pre-existing 'every unit ships from the distributor'
		branch would be a plainly false explanation for staff looking at the rifle on
		the shelf, which is why the antique check runs BEFORE it.
		"""
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._dispose([_srow()])
		self.assertIn("antique", str(ctx.exception).lower())

	def test_a_mixed_order_is_refused_while_its_antique_names_no_gun(self):
		"""The rule the auto-clear exit already carries, written at THIS exit too.

		Without it a mixed order disposes the regulated gun, books the revenue and
		goes Transferred while `antique._issue_antique_units` silently skips the
		serial-less antique — no Material Issue, no delist. Since POS stopped
		reserving a placeholder for such a row, nothing else is holding that rifle
		either, so it goes straight back on sale having been sold and paid for.

		Mutation-checked: deleting the guard used to leave the whole suite green.
		"""
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._dispose([
				_srow(confirmed_serial=""),
				_srow(item_code=MODERN, confirmed_serial="SN-9", woo_line_id="8"),
			])
		self.assertIn("antique", str(ctx.exception).lower())

	def test_a_mixed_order_whose_antique_names_its_gun_gets_past_the_guard(self):
		"""The companion — the guard must not swallow an order that is ready."""
		try:
			self._dispose([
				_srow(confirmed_serial="205745"),
				_srow(item_code=MODERN, confirmed_serial="SN-9", woo_line_id="8"),
			])
		except Exception as exc:  # noqa: BLE001 — any later failure is fine here
			self.assertNotIn("antique unit", str(exc).lower())

	def test_a_regulated_order_is_not_refused_by_the_antique_guard(self):
		"""Regression: the guard must not swallow ordinary orders. This one gets past
		it and fails later (on the real disposition machinery these mocks don't
		provide) — what matters is that it is NOT the antique refusal."""
		try:
			self._dispose([_srow(item_code=MODERN, confirmed_serial="SN-9")])
		except Exception as exc:  # noqa: BLE001 — any later failure is fine here
			self.assertNotIn("antique", str(exc).lower())


# ===========================================================================
# 6. The queue — an antique's serial row must not drag the order back in
# ===========================================================================

def _line(item_code, woo_line_id="7"):
	return frappe._dict({"item_code": item_code, "woo_line_id": woo_line_id, "qty": 1})


def _item_row(*, is_firearm=0, item_group="Accessories", is_antique=0):
	return frappe._dict({"is_firearm": is_firearm, "item_group": item_group,
		"is_antique": is_antique})


def _queue_order():
	return frappe._dict({
		"name": "WOO-ORD-01379", "site": "retail", "woo_status": "completed",
		"woo_order_id": "4201", "customer_name": "Web Buyer", "atf_ffl_record": "",
		"ffl_license": "", "ffl_business_name": "", "ffl_expiration": "",
		"creation": "2026-07-29 10:00:00", "currency": "USD", "order_total": 1795.0,
		"total_tax": 0.0, "shipping_total": 0.0, "discount_total": 0.0,
	})


class TestQueueIgnoresAntiqueSerialRows(FrappeTestCase):
	"""Restoring the serial row must NOT undo the previous change's fix.

	The original complaint was that an antique order sat in the Pending Order page
	rendering zero action buttons. Antiques now have serial rows again, so the old
	``if not doc.serials`` test would put every antique order straight back into that
	queue with a Dispose button it must never be given.
	"""

	def _rows(self, serials, lines, items):
		from ffl_woo_sync.woocommerce import fulfillment, webhook
		doc = MagicMock()
		doc.serials = serials
		doc.lines = lines
		real_get_value = frappe.db.get_value

		def _get_value(doctype, name=None, *args, **kwargs):
			if doctype == "Item":
				return items.get(name)
			return real_get_value(doctype, name, *args, **kwargs)

		with patch("frappe.only_for"), \
			 patch("frappe.get_all", return_value=[_queue_order()]), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch("frappe.db.get_value", side_effect=_get_value), \
			 patch("frappe.db.table_exists", return_value=False), \
			 patch.object(fulfillment, "is_antique_item",
				side_effect=lambda ic: ic == ANTIQUE), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.nowdate", return_value="2026-07-29"), \
			 patch.object(webhook, "_log_error"):
			return fulfillment.list_pending_web_orders()

	def test_an_antique_with_a_serial_row_is_still_dropped(self):
		"""THE regression guard for this change: a row exists now, but it is not an
		action item — the order must stay out of the queue."""
		rows = self._rows(
			[_srow()], [_line(ANTIQUE)],
			{ANTIQUE: _item_row(is_firearm=1, item_group="Rifle", is_antique=1)},
		)
		self.assertEqual(rows, [])

	def test_a_mixed_order_is_still_queued_and_disposable(self):
		"""The antique row is ignored, the regulated one still counts."""
		rows = self._rows(
			[_srow(), _srow(item_code=MODERN, confirmed_serial="SN-9", woo_line_id="8")],
			[_line(ANTIQUE), _line(MODERN, woo_line_id="8")],
			{ANTIQUE: _item_row(is_firearm=1, item_group="Rifle", is_antique=1),
			 MODERN: _item_row(is_firearm=1, item_group="Handgun")},
		)
		self.assertEqual(len(rows), 1)
		self.assertTrue(rows[0]["needs_disposition"])
		self.assertEqual(rows[0]["pending_serials"],
			[{"item_code": MODERN, "serial": "SN-9"}],
			"the dispose dialog must not list the antique as something it will book "
			"into the bound book — dispose_web_order filters it out, so showing it "
			"would simply be a lie to the person clicking Dispose")

	def test_an_unresolved_antique_now_holds_the_order(self):
		"""An antique naming NO gun sets `awaiting` — reversing what this test used
		to assert, and deliberately.

		The old rule ("it owes no bound-book entry, so it has no standing to hold up
		a gun that does") rested on a fact that no longer holds: the antique row used
		to carry a `stock_hold_serial`, so the rifle was reserved and off the
		storefront even while the row waited. Since POS stopped guessing which unit a
		uniform-price order gets, a serial-less antique reserves NOTHING — and
		`antique._issue_antique_units` skips such a row, so disposing would mark the
		order Transferred, book its revenue, and put a sold rifle back on sale.

		Nor is anything really being delayed: without the serial nobody knows WHICH
		antique to put in the box, so the parcel cannot ship either way.

		It still stays out of `pending_serials` — the dispose dialog must not claim
		it will book the antique into the bound book."""
		rows = self._rows(
			[_srow(confirmed_serial=""),
			 _srow(item_code=MODERN, confirmed_serial="SN-9", woo_line_id="8")],
			[_line(ANTIQUE), _line(MODERN, woo_line_id="8")],
			{ANTIQUE: _item_row(is_firearm=1, item_group="Rifle", is_antique=1),
			 MODERN: _item_row(is_firearm=1, item_group="Handgun")},
		)
		self.assertEqual(len(rows), 1)
		self.assertTrue(rows[0]["awaiting_serial"])
		self.assertTrue(rows[0]["needs_disposition"])
		self.assertEqual(rows[0]["pending_serials"],
			[{"item_code": MODERN, "serial": "SN-9"}])
		self.assertTrue(any(ANTIQUE in e["item_code"] for e in rows[0]["serial_errors"]),
			"staff must be told WHAT is holding the order")

	def test_a_resolved_antique_still_does_not_block(self):
		"""The companion: once the antique names its rifle it is out of the way
		again, exactly as before."""
		rows = self._rows(
			[_srow(confirmed_serial="205745"),
			 _srow(item_code=MODERN, confirmed_serial="SN-9", woo_line_id="8")],
			[_line(ANTIQUE), _line(MODERN, woo_line_id="8")],
			{ANTIQUE: _item_row(is_firearm=1, item_group="Rifle", is_antique=1),
			 MODERN: _item_row(is_firearm=1, item_group="Handgun")},
		)
		self.assertEqual(len(rows), 1)
		self.assertFalse(rows[0]["awaiting_serial"])
		self.assertTrue(rows[0]["needs_disposition"])

	def test_a_regulated_only_order_is_still_queued(self):
		rows = self._rows(
			[_srow(item_code=MODERN, confirmed_serial="SN-9")], [_line(MODERN)],
			{MODERN: _item_row(is_firearm=1, item_group="Handgun")},
		)
		self.assertEqual(len(rows), 1)

	def test_an_all_antique_order_with_no_serial_stays_in_the_queue(self):
		"""The retention half of the fix, which mutation showed was unpinned: with
		no regulated row beside it, `actionable_rows` is empty and the order used to
		be dropped out of the queue entirely — no badge, no error, no sweep. Both
		exits now refuse such an order, so the operator has to be able to see it."""
		rows = self._rows(
			[_srow(confirmed_serial="")], [_line(ANTIQUE)],
			{ANTIQUE: _item_row(is_firearm=1, item_group="Rifle", is_antique=1)},
		)
		self.assertEqual(len(rows), 1, "the order must not drop out of the queue")
		self.assertTrue(rows[0]["awaiting_serial"])
		self.assertEqual(rows[0]["pending_serials"], [],
			"an antique books no disposition — it must not be offered as one")
		self.assertTrue(rows[0]["serial_errors"], "staff must be told what holds it")

	def test_an_all_antique_order_that_names_its_gun_still_leaves_the_queue(self):
		"""The companion: a resolved antique owes nothing, so it must keep falling
		straight through as before."""
		rows = self._rows(
			[_srow(confirmed_serial="205745")], [_line(ANTIQUE)],
			{ANTIQUE: _item_row(is_firearm=1, item_group="Rifle", is_antique=1)},
		)
		self.assertEqual(rows, [])
