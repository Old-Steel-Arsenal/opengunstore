"""Pickup: hand an in-store-pickup web order to the counter, already paid.

`is_own_license` recognises the order; this is what staff DO with it. One button
on the Pending Order page, and the order joins the flow the counter already has
— which is the point. A retail 4473 lives on a POS Invoice: that is where
`start_4473_for_pos_invoice` starts, what `_ensure_disposition_coverage` looks
for when the webhook comes back, and what stops
`_sync_fb_direct_disposal_stock` alarming "4473 DISPOSAL WITH NO POS INVOICE"
and holding the gun in stock. A real web order proved what happens without it.

Building a POS Invoice server-side is NOT the same as building one in the POS
UI, and the first cut of this feature got several of those differences wrong.
Review caught them after it shipped; the tests below are mostly the shape of
those misses, because each is invisible until the exact shift, the exact order,
or the exact cancel happens:

* the profile has to come from the OPEN SHIFT, not from `sorted(profiles)[0]` —
  ERPNext throws without an open POS Opening Entry, so the wrong pick is either
  a hard error on the other register or someone else's till;
* `update_stock` / `ignore_pricing_rule` are copied from the profile only when
  `not for_validate`, and a server-side insert never is — so the gun would
  never leave stock on Complete Order;
* the payment has to be the INVOICE's total, mirrored against WooCommerce's,
  because any gap posts as change out of the drawer;
* shipping has to be a real charge, or it is that gap;
* submit and cancel both have to move the web order, and submit has to release
  the soft-reserve the way both sibling exits do.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

PICKUP = "ffl_woo_sync.woocommerce.pickup"
# The Transferred flip + its revenue-date stamp live behind the doctype's own
# front door (mark_transferred), so that is where the clock is.
ORDER_DOC = "ffl_woo_sync.ffl_woo_sync.doctype.woo_online_order.woo_online_order"
ORDER = "WOO-ORD-00008"


def _line(item_code="Sample Pistol Grade A", qty=1, line_total=649.50, woo_line_id="632"):
	return frappe._dict({"item_code": item_code, "qty": qty,
		"line_total": line_total, "woo_line_id": woo_line_id})


def _serial_row(item_code="Sample Pistol Grade A", confirmed_serial="",
		woo_line_id="632", fulfillment="local"):
	return frappe._dict({"item_code": item_code, "confirmed_serial": confirmed_serial,
		"stock_hold_serial": "", "woo_line_id": woo_line_id,
		"fulfillment": fulfillment, "unit_index": 0})


def _order(*, lines=None, serials=None, order_status="Pending Fulfillment",
		woo_status="processing", shipping=0.0, tax=54.37, total=713.36,
		refunds=None, dropship_status=""):
	doc = MagicMock()
	doc.name = ORDER
	doc.woo_order_id = "9001"
	doc.order_status = order_status
	doc.woo_status = woo_status
	doc.ffl_license = "5-99-888-01-9B-42763"
	doc.ffl_business_name = "TEST STORE A INC"
	doc.customer_name = "Sam Sample"
	doc.site = "retail"
	doc.currency = "USD"
	doc.order_total = total
	doc.total_tax = tax
	doc.shipping_total = shipping
	doc.refunds = refunds or []
	doc.lines = lines if lines is not None else [_line()]
	doc.serials = serials if serials is not None else [_serial_row()]
	doc.get = lambda key, default=None: {"dropship_status": dropship_status}.get(
		key, default)
	return doc


class _Captured:
	"""The doc `frappe.get_doc({...})` was asked to build, with the write stubbed
	and a grand_total that behaves like ERPNext's own calculation."""

	def __init__(self, grand_total=713.36, profile_template=None):
		self.payload = None
		self.doc = None
		self.grand_total = grand_total
		# What ERPNext's set_pos_fields would copy off the POS Profile on the
		# for_validate=False pass. Verified against
		# erpnext/accounts/doctype/pos_invoice/pos_invoice.py: the loop that
		# includes "taxes_and_charges" writes the profile's value with no regard
		# for what the document already carries, empty profile value included.
		self.profile_template = profile_template

	def get_doc(self, arg, *a, **kw):
		if isinstance(arg, dict) and arg.get("doctype") == "POS Invoice":
			self.payload = arg
			doc = MagicMock()
			doc.name = "ACC-PSINV-2026-09999"
			doc.company = arg["company"]
			doc.grand_total = self.grand_total
			doc.payments = []
			doc.taxes_and_charges = arg.get("taxes_and_charges")
			doc.set = lambda field, value: setattr(doc, field, value)
			doc.get = lambda field, default=None: getattr(doc, field, default)
			doc.set_missing_values = lambda *a, **k: setattr(
				doc, "taxes_and_charges", self.profile_template)
			self.doc = doc
			return doc
		return MagicMock()


def _run(order, *, is_own=True, regulated=True, existing_draft=None,
		shift=("POS1", "cashier@example.com"), grand_total=713.36, profile_defaults=None,
		profile_template=None):
	from ffl_woo_sync.woocommerce import pickup
	cap = _Captured(grand_total, profile_template)
	defaults = profile_defaults if profile_defaults is not None else {
		"update_stock": 1, "ignore_pricing_rule": 0}
	with patch("frappe.only_for"), \
		 patch(f"{PICKUP}.is_own_license", return_value=is_own), \
		 patch(f"{PICKUP}.is_regulated_firearm_item", return_value=regulated), \
		 patch(f"{PICKUP}._open_draft_for_order", return_value=existing_draft), \
		 patch(f"{PICKUP}._refuse_unless_ready"), \
		 patch(f"{PICKUP}._open_shift",
			 return_value=frappe._dict(pos_profile=shift[0], user=shift[1])), \
		 patch(f"{PICKUP}._profile_defaults", return_value=defaults), \
		 patch(f"{PICKUP}._default_company", return_value="_Test Store A"), \
		 patch(f"{PICKUP}._ensure_mode_of_payment", return_value="WooCommerce"), \
		 patch(f"{PICKUP}._resolve_web_customer", return_value="Sam Sample"), \
		 patch(f"{PICKUP}._sales_tax_account", return_value="Sales Tax - TSA"), \
		 patch(f"{PICKUP}.build_invoice_items",
			 return_value=[{"item_code": "Sample Pistol Grade A", "qty": 1,
				 "rate": 649.50, "serial_no": "X100001"}]), \
		 patch("ffl_core.setup.shipping_charge.ensure_shipping_account",
			 return_value="Shipping Income - TSA"), \
		 patch(f"{PICKUP}._finish_non_firearm_pickup",
			 return_value={"ok": True, "mode": "closed"}) as fin, \
		 patch("frappe.get_doc", side_effect=cap.get_doc), \
		 patch("frappe.db.set_value"):
		result = pickup._pickup(order)
	return result, cap, fin


# ---------------------------------------------------------------------------
# the counter draft
# ---------------------------------------------------------------------------

class TestFirearmPickupOpensPaidDraft(FrappeTestCase):
	def test_the_profile_comes_from_the_open_shift(self):
		"""Not `sorted(profiles)[0]`. ERPNext's validate_pos_opening_entry throws
		without an Open entry for THIS profile — on the draft insert too — so a
		guess is a hard error on the other register, or someone else's till."""
		_, cap, _ = _run(_order(), shift=("POS2", "cashier@example.com"))
		self.assertEqual(cap.payload["pos_profile"], "POS2")

	def test_profile_defaults_are_copied_explicitly(self):
		"""`set_pos_fields` copies these only when `not for_validate`, and a
		server-side insert() never is. Left unset, update_stock stays 0 and the
		gun never leaves stock on Complete Order."""
		_, cap, _ = _run(_order())
		self.assertEqual(cap.payload["update_stock"], 1)
		self.assertIn("ignore_pricing_rule", cap.payload)

	def test_the_tax_template_is_pinned_empty(self):
		"""Blank, not absent: a later POS-UI touch runs set_missing_values with
		for_validate=False, which would pull the profile's template in and
		recompute over the carried WooCommerce figure."""
		_, cap, _ = _run(_order())
		self.assertEqual(cap.payload["taxes_and_charges"], "")

	def test_the_pinned_blank_template_survives_the_build(self):
		"""Pinning it in the payload was not enough. `set_missing_values()` runs
		on the way in, and its `set_pos_fields` copies the POS Profile's
		`taxes_and_charges` over whatever the document carries — so on any
		register that has a template, the deliberate blank was overwritten on
		every pickup. That is precisely
		the door the pin exists to shut: the invoice then names a percentage
		template while carrying WooCommerce's Actual tax rows, and the next
		POS-UI touch recomputes tax over the figure the buyer was charged."""
		_, cap, _ = _run(_order(), profile_template="Texas Sales Tax - TSA")
		self.assertEqual(cap.doc.taxes_and_charges, "")

	def test_it_is_already_paid_for_exactly_what_it_totals(self):
		"""The amount is the INVOICE's grand total, never order_total: any gap
		between them posts as change out of the drawer."""
		_, cap, _ = _run(_order())
		self.assertEqual(len(cap.doc.payments), 1)
		self.assertEqual(cap.doc.payments[0]["mode_of_payment"], "WooCommerce")
		self.assertAlmostEqual(cap.doc.payments[0]["amount"], 713.36, places=2)

	def test_a_total_that_does_not_mirror_woocommerce_is_refused(self):
		"""Rates are line_total/qty and round on a multi-qty line. An unmirrored
		cent either shorts the till or trips 'Partial Payment in POS Transactions
		are not allowed' AT SUBMIT — buyer at the counter, 4473 already run."""
		with self.assertRaises(frappe.ValidationError):
			_run(_order(), grand_total=700.00)

	def test_a_cent_of_rounding_is_tolerated(self):
		_, cap, _ = _run(_order(), grand_total=713.37)
		self.assertAlmostEqual(cap.doc.payments[0]["amount"], 713.37, places=2)

	def test_shipping_becomes_a_real_charge(self):
		"""Not just an addend on the payment. Paying order_total against an
		invoice of lines+tax hands the shipping to change_amount, which posts out
		of the cash account — till short, shipping revenue never booked."""
		_, cap, _ = _run(_order(shipping=45.0, tax=0.0, total=444.0),
			grand_total=444.0)
		heads = [t["account_head"] for t in cap.payload["taxes"]]
		self.assertIn("Shipping Income - TSA", heads)

	def test_no_shipping_adds_no_row(self):
		_, cap, _ = _run(_order())
		self.assertEqual([t["account_head"] for t in cap.payload["taxes"]],
			["Sales Tax - TSA"])

	def test_woocommerce_tax_is_carried_not_recomputed(self):
		_, cap, _ = _run(_order())
		tax_row = cap.payload["taxes"][-1]
		self.assertEqual(tax_row["charge_type"], "Actual")
		self.assertAlmostEqual(tax_row["tax_amount"], 54.37, places=2)

	def test_items_come_from_the_shared_builder(self):
		"""revenue.build_invoice_items, not a local copy — the copy dropped the
		supplier-lane branch, so a drop-ship line tried to issue stock we never
		received and lost its dropship compliance exemption."""
		_, cap, _ = _run(_order())
		self.assertEqual(cap.payload["items"][0]["serial_no"], "X100001")

	def test_the_invoice_points_back_at_its_web_order(self):
		_, cap, _ = _run(_order())
		self.assertEqual(cap.payload["woo_online_order"], ORDER)

	def test_it_stays_a_draft_and_the_order_stays_queued(self):
		result, cap, _ = _run(_order())
		cap.doc.insert.assert_called_once()
		cap.doc.submit.assert_not_called()
		self.assertEqual(result["order_status"], "Pending Fulfillment")


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------

class TestPickupGuards(FrappeTestCase):
	def test_a_shipping_order_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			_run(_order(), is_own=False)

	def test_an_order_already_off_the_queue_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			_run(_order(order_status="Transferred"))

	def test_a_second_click_returns_the_same_draft(self):
		result, cap, _ = _run(_order(), existing_draft="ACC-PSINV-2026-99001")
		self.assertEqual(result["pos_invoice"], "ACC-PSINV-2026-99001")
		self.assertIsNone(cap.payload)

	def test_the_endpoint_does_not_accept_a_document(self):
		"""Frappe splats form_dict into whitelisted methods, so a doc-shaped
		parameter on `pickup_web_order` would let a caller drive invoice creation
		off request-supplied lines, serials and totals. The doc-taking half is
		unwhitelisted, exactly as revenue/antique split theirs."""
		import inspect

		from ffl_woo_sync.woocommerce import pickup
		params = set(inspect.signature(pickup.pickup_web_order).parameters)
		self.assertEqual(params, {"woo_online_order"})
		# frappe.whitelist() registers the function object in frappe.whitelisted;
		# it sets no attribute on the function, so that is what has to be checked.
		self.assertIn(pickup.pickup_web_order, frappe.whitelisted)
		self.assertNotIn(pickup._pickup, frappe.whitelisted)


class TestRefuseUnlessReady(FrappeTestCase):
	def _check(self, order, *, legs=None, ready=True):
		from ffl_woo_sync.woocommerce import pickup
		with patch("ffl_woo_sync.woocommerce.fulfillment._to_store_receive_map",
				return_value={ORDER: legs} if legs else {}), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.to_store_ready",
				return_value=ready):
			pickup._refuse_unless_ready(order)

	def test_an_unpaid_order_is_refused(self):
		"""The payment row books the full total against the Woo clearing account
		— on an unpaid order that is money never collected. dispose_web_order
		guards the same thing on the same endpoint surface."""
		with self.assertRaises(frappe.ValidationError):
			self._check(_order(woo_status="pending"))

	def test_an_outstanding_distributor_leg_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._check(_order(dropship_status="Pending Route"))

	def test_a_refunded_order_is_refused(self):
		"""The invoice mirrors the ORIGINAL total, so ringing it collects gross."""
		with self.assertRaises(frappe.ValidationError):
			self._check(_order(refunds=[frappe._dict({"amount": 50})]))

	def test_a_procure_to_store_gun_that_has_not_arrived_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._check(_order(), legs=["Pending"], ready=False)

	def test_a_ready_order_passes(self):
		self._check(_order(), legs=["Received"], ready=True)


# ---------------------------------------------------------------------------
# no firearms → no draft at all
# ---------------------------------------------------------------------------

class TestNonFirearmPickup(FrappeTestCase):
	def test_an_accessory_only_pickup_opens_no_pos_invoice(self):
		result, cap, fin = _run(_order(), regulated=False)
		self.assertIsNone(cap.payload)
		fin.assert_called_once()
		self.assertEqual(result["mode"], "closed")


class TestFinishNonFirearmPickup(FrappeTestCase):
	"""Exercised for real — the branch above stubs it. An earlier cut called
	`_auto_clear_non_firearm_if_completed` AFTER setting Transferred, which that
	function's own first-line guard rejects, so the order left the queue with
	stock, listing and revenue all untouched."""

	def _finish(self, order, *, antique=False):
		from ffl_woo_sync.woocommerce import pickup
		with patch(f"{PICKUP}.is_antique_item", return_value=antique), \
			 patch("ffl_core.firearm.is_antique_item", return_value=antique), \
			 patch("ffl_woo_sync.woocommerce.antique.retire_antique_web_order") as retire, \
			 patch("ffl_woo_sync.woocommerce.revenue.create_sales_invoice_for_web_order") as invoice, \
			 patch("frappe.db.set_value") as set_value:
			result = pickup._finish_non_firearm_pickup(order)
		return result, retire, invoice, set_value

	def test_an_accessory_order_books_its_revenue(self):
		result, retire, invoice, set_value = self._finish(_order(serials=[]))
		invoice.assert_called_once()
		retire.assert_not_called()
		self.assertEqual(result["order_status"], "Transferred")
		set_value.assert_called_once()

	def test_an_antique_order_goes_through_the_retire_path(self):
		order = _order(serials=[_serial_row(confirmed_serial="ANT-1")])
		_, retire, invoice, _ = self._finish(order, antique=True)
		retire.assert_called_once()
		invoice.assert_not_called()

	def test_transferred_is_set_before_the_downstream_step(self):
		order = _order(serials=[])
		_, _, invoice, set_value = self._finish(order)
		self.assertEqual(set_value.call_args[0][2]["order_status"], "Transferred")
		self.assertEqual(order.order_status, "Transferred")
		self.assertTrue(order.transferred_at)   # the revenue booking reads it
		invoice.assert_called_once()

	def test_an_antique_with_no_serial_is_refused(self):
		order = _order(serials=[_serial_row(confirmed_serial="")])
		with self.assertRaises(frappe.ValidationError):
			self._finish(order, antique=True)

	def test_a_serialised_non_antique_row_is_refused_by_its_own_name(self):
		"""It owes its own bound-book exit. The earlier cut ran only the
		blank-serial half, unconditionally, and told staff to go find an antique
		that was not there."""
		order = _order(serials=[_serial_row(confirmed_serial="")])
		from ffl_woo_sync.woocommerce import pickup
		with patch(f"{PICKUP}.is_antique_item", return_value=False), \
			 patch("frappe.db.set_value"), \
			 self.assertRaises(frappe.ValidationError) as caught:
			pickup._finish_non_firearm_pickup(order)
		self.assertNotIn("antique unit with no serial", str(caught.exception))

	def test_a_dropship_row_with_no_serial_does_not_block(self):
		"""Blank/blank is a drop-ship row's designed state."""
		order = _order(serials=[_serial_row(confirmed_serial="", fulfillment="direct")])
		_, _, invoice, _ = self._finish(order, antique=True)
		invoice.assert_not_called()


# ---------------------------------------------------------------------------
# submit retires the order; cancel puts it back
# ---------------------------------------------------------------------------

class TestCloseAndReopen(FrappeTestCase):
	def _doc(self, woo_order=ORDER):
		doc = MagicMock()
		doc.doctype = "POS Invoice"
		doc.name = "ACC-PSINV-2026-09999"
		doc.woo_online_order = woo_order
		return doc

	def _submit(self, *, woo_order=ORDER, status="Pending Fulfillment", held=()):
		from ffl_woo_sync.woocommerce import pickup
		# get_value is blanket-patched, which also swallows the clock's timezone
		# lookup — pin the stamp so the test stays about the status write.
		with patch("frappe.db.get_value", return_value=status), \
			 patch("frappe.get_all", return_value=list(held)), \
			 patch(f"{ORDER_DOC}.now_datetime", return_value="2026-08-26 15:11:43"), \
			 patch("frappe.db.set_value") as set_value:
			pickup.close_web_order_on_pos_submit(self._doc(woo_order))
		return set_value

	def test_submitting_the_counter_invoice_retires_the_web_order(self):
		set_value = self._submit()
		first = set_value.call_args_list[0][0]
		self.assertEqual(first[:2], ("Woo Online Order", ORDER))
		self.assertEqual(first[2]["order_status"], "Transferred")
		self.assertTrue(first[2]["transferred_at"])   # the revenue date, stamped here

	def test_submitting_releases_the_soft_reserve(self):
		"""Both sibling exits clear {woo_reserved, woo_order}; this one did not,
		so a sold gun kept a phantom hold — the counter would refuse to re-sell
		it and the web auto-assign query would never offer it again."""
		set_value = self._submit(held=["X100001"])
		released = [c for c in set_value.call_args_list
			if c[0][0] == "Serial No" and c[0][1] == "X100001"]
		self.assertEqual(len(released), 1)
		self.assertEqual(released[0][0][2], {"woo_reserved": 0, "woo_order": ""})

	def test_an_unrelated_counter_sale_is_left_alone(self):
		set_value = self._submit(woo_order=None)
		set_value.assert_not_called()

	def test_an_order_already_retired_is_not_rewritten(self):
		set_value = self._submit(status="Transferred")
		set_value.assert_not_called()

	def _cancel(self, *, woo_order=ORDER, status="Transferred"):
		from ffl_woo_sync.woocommerce import pickup
		with patch("frappe.db.get_value", return_value=status), \
			 patch(f"{ORDER_DOC}.now_datetime", return_value="2026-08-26 15:11:43"), \
			 patch("frappe.db.set_value") as set_value:
			pickup.reopen_web_order_on_pos_cancel(self._doc(woo_order))
		return set_value

	def test_cancelling_puts_the_order_back_in_the_queue(self):
		"""ERPNext returns the gun to stock; without this the order stayed
		Transferred for ever — invisible, rifle on the shelf, sale unbooked, and
		Pickup refusing to reopen it."""
		set_value = self._cancel()
		args = set_value.call_args[0]
		self.assertEqual(args[:2], ("Woo Online Order", ORDER))
		self.assertEqual(args[2]["order_status"], "Pending Fulfillment")
		self.assertIsNone(args[2]["transferred_at"])   # reopened: the stamp goes too

	def test_cancelling_an_unrelated_counter_sale_is_a_no_op(self):
		set_value = self._cancel(woo_order=None)
		set_value.assert_not_called()

	def test_cancelling_does_not_reopen_an_order_that_never_left(self):
		set_value = self._cancel(status="Pending Fulfillment")
		set_value.assert_not_called()


class TestWebTenderOnSubmit(FrappeTestCase):
	"""The ticket's WooCommerce row used to vanish in the register and the card
	terminal was offered the full total — to a buyer who had already paid. Submit
	is the last line: WooCommerce, for exactly what WooCommerce charged, and
	nothing else — on an order that is still paid and still waiting."""

	def _doc(self, payments, *, grand_total=812.40, woo_order=ORDER, is_return=0):
		return frappe._dict(doctype="POS Invoice", name="ACC-PSINV-2026-09999",
			woo_online_order=woo_order, is_return=is_return, grand_total=grand_total,
			payments=[frappe._dict(mode_of_payment=m, amount=a) for m, a in payments])

	def _order(self, *, order_total=812.40, order_status="Pending Fulfillment"):
		return frappe._dict(name=ORDER, order_total=order_total, order_status=order_status)

	def _submit(self, doc, order=None, ready=None):
		from ffl_woo_sync.woocommerce import pickup
		order = order or self._order()
		with patch("frappe.get_doc", return_value=order) as get_doc, \
			 patch(f"{PICKUP}._refuse_unless_ready", side_effect=ready) as refuse:
			pickup.require_web_tender_on_submit(doc)
		return get_doc, refuse

	def test_the_woocommerce_tender_for_the_order_total_passes(self):
		self._submit(self._doc([("WooCommerce", 812.40), ("Credit Card", 0), ("Cash", 0)]))

	def test_a_card_tender_in_its_place_is_refused(self):
		"""The register's reset: WooCommerce zeroed, card pre-filled."""
		with self.assertRaises(frappe.ValidationError):
			self._submit(self._doc([("WooCommerce", 0), ("Credit Card", 812.40)]))

	def test_the_profile_rows_alone_are_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._submit(self._doc([("Credit Card", 812.40), ("Cash", 0)]))

	def test_any_counter_tender_beside_it_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._submit(self._doc([("WooCommerce", 812.40), ("Cash", 20)], grand_total=832.40))

	def test_a_woocommerce_amount_off_the_order_total_is_refused(self):
		"""Money nobody collected, booked as collected."""
		with self.assertRaises(frappe.ValidationError):
			self._submit(self._doc([("WooCommerce", 870.00)], grand_total=870.00))

	def test_a_ticket_that_no_longer_totals_the_order_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._submit(self._doc([("WooCommerce", 812.40)], grand_total=870.00))

	def test_a_cent_of_rounding_is_tolerated(self):
		self._submit(self._doc([("WooCommerce", 812.41)], grand_total=812.41))

	def test_an_order_cancelled_since_pickup_is_refused(self):
		"""The 4473 can take days; the buyer may cancel online meanwhile."""
		with self.assertRaises(frappe.ValidationError):
			self._submit(self._doc([("WooCommerce", 812.40)]),
				order=self._order(order_status="Cancelled"))

	def test_the_pickup_refusals_are_re_run_on_the_order(self):
		"""Refunded / no longer paid / distributor leg open: the same checks
		Pickup made when it opened the draft."""
		order = self._order()
		_get_doc, refuse = self._submit(self._doc([("WooCommerce", 812.40)]), order=order)
		refuse.assert_called_once_with(order)
		with self.assertRaises(frappe.ValidationError):
			self._submit(self._doc([("WooCommerce", 812.40)]),
				ready=frappe.ValidationError("refunded"))

	def test_an_ordinary_counter_sale_is_left_alone(self):
		get_doc, _refuse = self._submit(self._doc([("Credit Card", 500)], woo_order=None))
		get_doc.assert_not_called()

	def test_a_return_is_left_alone(self):
		get_doc, _refuse = self._submit(self._doc([("Credit Card", -812.40)], is_return=1))
		get_doc.assert_not_called()

	def test_it_runs_before_submit(self):
		hooks = frappe.get_hooks("doc_events")["POS Invoice"]["before_submit"]
		self.assertIn("ffl_woo_sync.woocommerce.pickup.require_web_tender_on_submit", hooks)


class TestOpenDraftsForOrders(FrappeTestCase):
	def test_many_orders_are_answered_in_one_query(self):
		"""The Pending Order page renders every pending order and reloads after
		every action; asking per row made it an N+1."""
		from ffl_woo_sync.woocommerce.pickup import open_drafts_for_orders
		rows = [frappe._dict({"name": "PSINV-1", "woo_online_order": "WOO-1"}),
			frappe._dict({"name": "PSINV-2", "woo_online_order": "WOO-2"})]
		with patch("frappe.get_all", return_value=rows) as get_all:
			out = open_drafts_for_orders(["WOO-1", "WOO-2"])
		self.assertEqual(get_all.call_count, 1)
		self.assertEqual(out, {"WOO-1": "PSINV-1", "WOO-2": "PSINV-2"})

	def test_only_drafts_count_as_open(self):
		"""A submitted invoice is not an open draft — treating it as one would
		make Pickup hand back a finished sale instead of opening a new order."""
		from ffl_woo_sync.woocommerce.pickup import open_drafts_for_orders
		with patch("frappe.get_all", return_value=[]) as get_all:
			open_drafts_for_orders(["WOO-1"])
		self.assertEqual(get_all.call_args[1]["filters"]["docstatus"], 0)

	def test_no_orders_asks_the_database_nothing(self):
		from ffl_woo_sync.woocommerce.pickup import open_drafts_for_orders
		with patch("frappe.get_all") as get_all:
			self.assertEqual(open_drafts_for_orders([]), {})
		get_all.assert_not_called()


class TestOpenShift(FrappeTestCase):
	def _shift(self, shifts, user="me@example.com"):
		from ffl_woo_sync.woocommerce import pickup
		with patch("frappe.get_all", return_value=shifts), \
			 patch("frappe.session") as session:
			session.user = user
			return pickup._open_shift("_Test Store A")

	def test_the_callers_own_shift_wins(self):
		"""So the sale lands on the till the person clicking is working."""
		shift = self._shift([
			frappe._dict({"name": "OPE-1", "pos_profile": "POS1", "user": "other@example.com"}),
			frappe._dict({"name": "OPE-2", "pos_profile": "POS2", "user": "me@example.com"}),
		])
		self.assertEqual(shift.pos_profile, "POS2")

	def test_someone_elses_shift_is_used_when_it_is_the_only_one(self):
		shift = self._shift([
			frappe._dict({"name": "OPE-1", "pos_profile": "POS1", "user": "other@example.com"}),
		])
		self.assertEqual(shift.pos_profile, "POS1")

	def test_no_open_shift_says_to_open_one(self):
		"""ERPNext's own message names a POS Profile nobody chose; this one names
		the action."""
		with self.assertRaises(frappe.ValidationError) as caught:
			self._shift([])
		self.assertIn("Open the register", str(caught.exception))
