"""Fulfilling a GunBroker order — the gates, and why each one is fail-closed.

Two of these are federal-record gates and the rest protect money.

``_refuse_what_must_never_be_booked`` runs before anything else looks at
anything. What it lets through gets written into the A&D bound book — except an
antique, which never reaches the book at all: ``dispose_gb_order`` splits the
order's guns into regulated (disposition) and antique (plain stock issue), and
an order that is all antiques needs no receiving dealer.

The payment gate reads what POS recorded, never GunBroker's own flag. Their flag
is a click on their website: a store employee pressing it before the cheque
clears would otherwise open the door on a gun that has not been paid for.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import fulfillment
from ffl_integrations.tests import gunbroker_fixtures as fx


class _FulfillmentCase(FrappeTestCase):
	def setUp(self):
		self.alerts: list[str] = []
		self.messages: list[str] = []
		# Warm the two cached documents these paths reach through
		# frappe.client_cache BEFORE anything is patched: System Settings (for
		# nowdate()) and the Language record (for a formatted amount). Their
		# miss path loads through frappe.get_doc and frappe.db.get_value, and a
		# test's db.get_value=1 patch turns that load into "'int' object has no
		# attribute 'custom'". Only visible on a cold cache — a module run on
		# its own, or a fresh redis; the full suite warmed both in ffl_core.
		from frappe.locale import get_number_format

		frappe.get_system_settings("time_zone")
		get_number_format()
		self._patch_object(fulfillment, "notify_admins",
			side_effect=lambda s, m, **kw: self.alerts.append(kw["dedup_key"]))
		self._patch("frappe.msgprint",
			side_effect=lambda msg, **kw: self.messages.append(str(msg)))
		self._patch("frappe.only_for")
		# A correctly set-up site: the Mode of Payment exists and points at the
		# account GunBroker Settings names. TestThePaymentMethodIsSetUp takes
		# these apart deliberately; every other case here is about something
		# else and should not have to know this gate exists.
		self._patch("ffl_integrations.gunbroker.revenue._default_company",
			return_value="TSA")
		self._patch("ffl_core.api.manual_order._mode_of_payment_account",
			return_value="Operating Bank - TSA")
		self._patch("frappe.db.get_single_value",
			return_value="Operating Bank - TSA")

	def _patch(self, target, *args, **kw):
		patcher = patch(target, *args, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _patch_object(self, target, attr, *args, **kw):
		patcher = patch.object(target, attr, *args, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _patch_order_doc(self, order):
		"""frappe.get_doc returns `order` for the GunBroker Order lookup and is
		left alone for everything else.

		A bare return_value=order used to stand here. The first test to need a
		cached document (System Settings for nowdate(), the locale for a
		formatted amount) while that patch was live handed the FakeDoc to
		frappe.client_cache's miss path, which pickled it into redis under the
		real key — and every later load, in every later test, died unpickling
		it. Only visible on a cold cache, i.e. a module run on its own."""
		real_get_doc = frappe.get_doc

		def _get_doc(*args, **kwargs):
			if args and args[0] == fulfillment.ORDER_DOCTYPE:
				return order
			return real_get_doc(*args, **kwargs)

		return self._patch("frappe.get_doc", side_effect=_get_doc)

	def _order(self, detail=None, **fields):
		from ffl_integrations.gunbroker import orders as gb_orders

		defaults = {"name": "GB-ORD-00001", "gb_order_id": "998877",
			"order_status": "Pending Fulfillment", "payment_mode_used": "GunBroker Check",
			"payment_received_on": "2026-08-26", "payment_amount": 1245.99}
		defaults.update(fields)
		order = fx.FakeDoc(**defaults)
		detail = detail or fx.order_detail()
		with patch("frappe.db.exists", return_value=True):
			gb_orders.apply_order_lines(order, detail)
		order.ffl_received = 1 if detail.get("fflReceived") else 0
		for line in order.lines:
			order.append("serials", {"item_code": line.item_code,
				"gb_item_id": line.gb_item_id,
				"confirmed_serial": line.serial_number})
		for key, value in defaults.items():
			order[key] = value
		return order


# ---------------------------------------------------------------------------
# What must never reach the bound book
# ---------------------------------------------------------------------------

class TestBoundBookRefusals(_FulfillmentCase):
	def test_our_own_licence_as_the_receiving_dealer_is_refused(self):
		# The buyer chose this store. Nothing ships, there is no far-end dealer,
		# and booking it would write "we transferred this gun to ourselves" into
		# the bound book while the sale that really happens — a 4473 at the
		# counter — went unrecorded.
		order = self._order()
		with patch.object(fulfillment, "is_own_license", return_value=True), \
				self.assertRaises(Exception) as caught:
			fulfillment._refuse_what_must_never_be_booked(order)
		self.assertIn("4473", str(caught.exception))

	def test_an_ordinary_order_passes(self):
		order = self._order()
		with patch.object(fulfillment, "is_own_license", return_value=False):
			fulfillment._refuse_what_must_never_be_booked(order)  # no raise


# ---------------------------------------------------------------------------
# Antiques leave without a disposition
# ---------------------------------------------------------------------------

ANTIQUE = "M39-ANTIQUE"


class TestDisposeRetiresAntiquesWithoutADisposition(_FulfillmentCase):
	"""An antique is a firearm for inventory and not for ATF: it is issued out
	of stock, and everything the disposition would have done for the bound book
	and FastBound simply does not happen."""

	def _antique_detail(self, **kw):
		kw.setdefault("ffl_number", "")
		detail = fx.order_detail(sku=f"{ANTIQUE}::SN-A1", serial="SN-A1",
			ffl_received=False, **kw)
		detail["orderItemsCollection"][0]["isFFLRequired"] = False
		return detail

	def _dispose(self, order, cr_details=None, *, issue_result="MAT-STE-0001",
			serial_rows=None):
		set_values: list[tuple] = []
		self._patch_order_doc(order)
		with patch("frappe.db.set_value",
					side_effect=lambda *a, **k: set_values.append(a)), \
				patch.object(fulfillment.gb_listings, "serial_state",
					side_effect=lambda s: frappe._dict((serial_rows or {}).get(s, {}))), \
				patch.object(fulfillment, "is_own_license", return_value=False), \
				patch.object(fulfillment, "is_antique_item",
					side_effect=lambda code: code == ANTIQUE), \
				patch.object(fulfillment, "is_regulated_firearm_item",
					side_effect=lambda code: code == "COLT-PYTHON"), \
				patch.object(fulfillment, "safe_log_error"), \
				patch.object(fulfillment, "issue_antique_serial",
					return_value=issue_result) as issue, \
				patch.object(fulfillment, "_resolve_and_validate_ffl",
					return_value="FFL-REC-1") as resolve, \
				patch.object(fulfillment, "_book_dispositions",
					side_effect=lambda o, d, rows=None: (
						["FD-1"], [r.confirmed_serial for r in rows])) as book, \
				patch("ffl_integrations.gunbroker.revenue.create_sales_invoice_for_gb_order",
					return_value={"sales_invoice": "ACC-SINV-1", "status": "created"}) as invoice:
			result = fulfillment.dispose_gb_order("GB-ORD-00001", cr_details=cr_details)
		return result, issue, resolve, book, invoice, set_values

	def test_an_all_antique_order_needs_no_dealer_and_books_no_disposition(self):
		order = self._order(self._antique_detail())
		order.ffl_received = 0
		result, issue, resolve, book, invoice, set_values = self._dispose(order)
		resolve.assert_not_called()
		book.assert_not_called()
		issue.assert_called_once()
		self.assertEqual(issue.call_args.args, ("SN-A1",))
		self.assertIn("998877", issue.call_args.kwargs["remarks"],
			"the stock issue must name the order that took the gun")
		self.assertEqual(result["retired"], ["SN-A1"])
		self.assertEqual(result["disposed_count"], 0)
		self.assertEqual(order.order_status, "Transferred")
		self.assertEqual(order.ships_direct, 1, "the invoice reads this, not the Items")
		self.assertIsNone(order.atf_ffl_record)
		invoice.assert_called_once()
		self.assertIn(("Serial No", "SN-A1", {"gb_reserved": 0, "gb_order": ""}),
			set_values, "the GunBroker hold must be released like a disposed gun's")

	def _released(self, set_values, serial):
		rows = [a[2] for a in set_values if a[:2] == ("Serial No", serial)]
		self.assertEqual(len(rows), 1, serial)
		return rows[0]

	def test_the_listing_the_order_bought_leaves_with_the_gun(self):
		"""Left on the serial, the sweep finds the listing sold with no order
		holding the gun and flags it "may have sold on GunBroker" — for the
		regulated gun and the antique alike."""
		detail = fx.order_detail(extra_items=[fx.order_item(16087231,
			sku=f"{ANTIQUE}::SN-A1", serial="SN-A1", price=900, ffl_required=False)])
		order = self._order(detail)
		set_values = self._dispose(order, serial_rows={
			"SN-001": {"gb_item_id": "16087230", "gb_item_history": "16000001",
				"gb_end_pending": 1, "gb_gone_strikes": 2},
			"SN-A1": {"gb_item_id": "16087231"},
		})[-1]
		regulated = self._released(set_values, "SN-001")
		self.assertEqual(regulated["gb_item_id"], "")
		self.assertEqual(regulated["gb_item_history"], "16000001,16087230")
		self.assertEqual(regulated["gb_end_pending"], 0,
			"an end request for the listing that sold has nothing left to end")
		self.assertEqual(regulated["gb_gone_strikes"], 0)
		self.assertEqual((regulated["gb_reserved"], regulated["gb_order"]), (0, ""))
		antique = self._released(set_values, "SN-A1")
		self.assertEqual(antique["gb_item_id"], "")
		self.assertEqual(antique["gb_item_history"], "16087231")

	def test_a_listing_the_order_did_not_buy_is_left_for_the_end_listing_jobs(self):
		order = self._order(self._antique_detail())
		order.ffl_received = 0
		set_values = self._dispose(order, serial_rows={
			"SN-A1": {"gb_item_id": "99999999", "gb_end_pending": 1}})[-1]
		self.assertEqual(self._released(set_values, "SN-A1"),
			{"gb_reserved": 0, "gb_order": ""},
			"a still-live listing must stay visible so it gets ended")

	def test_a_mixed_order_books_the_regulated_gun_and_issues_the_antique(self):
		detail = fx.order_detail(extra_items=[fx.order_item(16087231,
			sku=f"{ANTIQUE}::SN-A1", serial="SN-A1", price=900, ffl_required=False)])
		order = self._order(detail)
		result, issue, resolve, book, invoice, _ = self._dispose(order)
		resolve.assert_called_once()
		rows = book.call_args.kwargs["rows"]
		self.assertEqual([r.confirmed_serial for r in rows], ["SN-001"],
			"only the regulated gun goes to the bound book")
		self.assertEqual(issue.call_args.args, ("SN-A1",))
		self.assertEqual(result["disposed_count"], 1)
		self.assertEqual(result["retired"], ["SN-A1"])
		self.assertEqual(order.atf_ffl_record, "FFL-REC-1",
			"the parcel still goes to the dealer, antique included")
		self.assertEqual(order.ships_direct, 0)

	def test_an_antique_already_out_of_stock_is_not_issued_again(self):
		# issue_antique_serial returns None for a gun that already left — a
		# retry after a failed invoice — and the order still completes.
		order = self._order(self._antique_detail())
		order.ffl_received = 0
		with patch("frappe.db.get_value", return_value="Consumed"):
			result, _issue, _r, _b, invoice, _ = self._dispose(order, issue_result=None)
		self.assertEqual(result["retired"], ["SN-A1"])
		self.assertEqual(order.order_status, "Transferred")
		invoice.assert_called_once()

	def test_a_hand_made_listing_is_still_an_antique_order(self):
		# The NORMAL shape of a listing made on the GunBroker website: the SKU
		# named no Item we hold, so the line carries none — but the gun matched
		# on its serial and the serial row carries the antique's own Item.
		# GunBroker may even flag the line FFL-required. Neither may turn this
		# into a dealer order, or the Dispose waits for a licence for ever.
		order = self._order(self._antique_detail())
		order.ffl_received = 0
		order.lines[0].item_code = ""
		order.lines[0].is_ffl_required = 1
		result, issue, resolve, book, _invoice, _ = self._dispose(order)
		resolve.assert_not_called()
		book.assert_not_called()
		issue.assert_called_once()
		self.assertEqual(order.ships_direct, 1)
		self.assertEqual(result["retired"], ["SN-A1"])

	def test_an_antique_order_that_names_a_dealer_ships_there(self):
		# The buyer supplied a licence anyway. It is honoured — verified and
		# used as the destination — rather than silently ignored; the antique
		# still books no disposition.
		order = self._order(self._antique_detail(ffl_number="1-99-888-01-2A-34567"))
		order.ffl_number = "1-99-888-01-2A-34567"
		order.ffl_received = 1
		result, issue, resolve, book, _invoice, _ = self._dispose(order)
		resolve.assert_called_once()
		book.assert_not_called()
		issue.assert_called_once()
		self.assertEqual(order.ships_direct, 0)
		self.assertEqual(order.atf_ffl_record, "FFL-REC-1")
		self.assertEqual(result["disposed_count"], 0)

	def test_a_serialized_non_firearm_is_refused_rather_than_booked(self):
		# Neither predicate claims it: not a regulated firearm (no disposition),
		# not an antique (no stock issue). Booking it into the bound book —
		# what the old loop did for every serial row — is exactly wrong.
		detail = fx.order_detail(sku="OPTIC-1::OPT-9", serial="OPT-9")
		order = self._order(detail)
		with self.assertRaises(Exception) as caught:
			self._dispose(order)
		self.assertIn("neither a regulated firearm nor an antique", str(caught.exception))
		self.assertIn("OPT-9", str(caught.exception))

	def test_an_antique_still_on_the_books_that_could_not_be_issued_is_reported(self):
		# issue_antique_serial found nothing to issue, yet the serial is still
		# Active: no warehouse to issue from. Not silently retired.
		order = self._order(self._antique_detail())
		order.ffl_received = 0
		with patch("frappe.db.get_value", return_value="Active"):
			self._dispose(order, issue_result=None)
		self.assertIn("gb_antique_not_issued_SN-A1", self.alerts)

	def test_an_antique_already_out_raises_no_alarm(self):
		order = self._order(self._antique_detail())
		order.ffl_received = 0
		with patch("frappe.db.get_value", return_value="Consumed"):
			self._dispose(order, issue_result=None)
		self.assertEqual(self.alerts, [])

	def test_collector_details_are_refused_on_an_antique_order(self):
		order = self._order(self._antique_detail())
		order.ffl_received = 0
		with self.assertRaises(Exception) as caught:
			self._dispose(order, cr_details={"license_number": "3-57-999-03-1A-00001"})
		self.assertIn("not a C&R transfer", str(caught.exception))


class TestShipsDirect(_FulfillmentCase):
	def _classify(self, *, antique, regulated):
		return patch.multiple(fulfillment,
			is_antique_item=lambda code: code in antique,
			is_regulated_firearm_item=lambda code: code in regulated)

	def test_all_antiques_ship_to_the_buyer(self):
		order = self._order()
		with self._classify(antique={"COLT-PYTHON"}, regulated=set()):
			self.assertTrue(fulfillment.ships_direct(order))

	def test_one_regulated_gun_sends_the_whole_parcel_to_the_dealer(self):
		detail = fx.order_detail(extra_items=[fx.order_item(16087231,
			sku=f"{ANTIQUE}::SN-A1", serial="SN-A1", price=900, ffl_required=False)])
		order = self._order(detail)
		with self._classify(antique={ANTIQUE}, regulated={"COLT-PYTHON"}):
			self.assertFalse(fulfillment.ships_direct(order))

	def test_a_named_dealer_licence_is_honoured_even_for_antiques(self):
		order = self._order()
		order.ffl_number = "1-99-888-01-2A-34567"
		with self._classify(antique={"COLT-PYTHON"}, regulated=set()):
			self.assertFalse(fulfillment.ships_direct(order))

	def test_a_hand_made_listing_reads_the_item_off_the_serial_row(self):
		order = self._order()
		order.lines[0].item_code = ""  # the serial row still names the Item
		with self._classify(antique={"COLT-PYTHON"}, regulated=set()):
			self.assertTrue(fulfillment.ships_direct(order))

	def test_an_order_with_no_matched_gun_fails_closed(self):
		order = self._order()
		order.serials = []
		with self._classify(antique={"COLT-PYTHON"}, regulated=set()):
			self.assertFalse(fulfillment.ships_direct(order))

	def test_a_line_nothing_can_classify_fails_closed(self):
		order = self._order()
		order.lines[0].item_code = ""
		order.serials[0].item_code = ""
		with self._classify(antique={"COLT-PYTHON"}, regulated=set()):
			self.assertFalse(fulfillment.ships_direct(order))

	def test_a_transferred_order_reads_the_decision_dispose_wrote(self):
		# The invoice was built from the persisted flag; the card must agree
		# with it even if the Items would classify differently today.
		order = self._order(order_status="Transferred", ships_direct=1)
		with self._classify(antique=set(), regulated={"COLT-PYTHON"}):
			self.assertTrue(fulfillment.ships_direct(order))
		order = self._order(order_status="Transferred", ships_direct=0)
		with self._classify(antique={"COLT-PYTHON"}, regulated=set()):
			self.assertFalse(fulfillment.ships_direct(order))


# ---------------------------------------------------------------------------
# The dispose preconditions
# ---------------------------------------------------------------------------

class TestDisposeGate(_FulfillmentCase):
	def test_an_order_that_is_not_pending_is_refused(self):
		order = self._order(order_status="Transferred")
		with self.assertRaises(Exception):
			fulfillment._require_ready_to_dispose(order)

	def test_a_layaway_order_is_refused(self):
		# Instalments are a customer deposit, not a sale.
		order = self._order(gb_status=10)
		with self.assertRaises(Exception) as caught:
			fulfillment._require_ready_to_dispose(order)
		self.assertIn("layaway", str(caught.exception).lower())

	def test_gunbrokers_own_payment_flag_does_not_open_the_gate(self):
		# THE gate. A store employee pressing "Payment Received" on GunBroker's
		# website gets mirrored here within fifteen minutes; on its own that is
		# not evidence the money reached us, and the gun must not leave on it.
		order = self._order(payment_mode_used="", payment_received=1)
		with self.assertRaises(Exception) as caught:
			fulfillment._require_ready_to_dispose(order)
		self.assertIn("Mark Payment Received", str(caught.exception))

	def test_recording_the_payment_in_pos_opens_it(self):
		order = self._order(payment_mode_used="GunBroker Check")
		fulfillment._require_ready_to_dispose(order)  # no raise

	def test_an_order_still_waiting_for_the_licence_is_refused(self):
		order = self._order(fx.order_detail(ffl_received=False))
		order.ffl_received = 0
		with self.assertRaises(Exception) as caught:
			fulfillment._require_ready_to_dispose(order)
		self.assertIn("licence", str(caught.exception).lower())

	def test_an_order_with_no_ffl_required_line_skips_the_licence_gate(self):
		detail = fx.order_detail(ffl_received=False)
		detail["orderItemsCollection"][0]["isFFLRequired"] = False
		order = self._order(detail)
		order.ffl_received = 0
		fulfillment._require_ready_to_dispose(order)  # no raise

	def test_an_antique_line_never_waits_for_a_licence(self):
		# GunBroker may still flag the line isFFLRequired (a listing made by
		# hand). The Item is what the gun is: a pre-1899 gun needs no licence,
		# and waiting for one would wait for ever.
		order = self._order(fx.order_detail(ffl_received=False))
		order.ffl_received = 0
		with patch.object(fulfillment, "is_antique_item", return_value=True):
			fulfillment._require_ready_to_dispose(order)  # no raise

	def test_an_antique_matched_only_by_serial_number_never_waits_either(self):
		# The line names no Item (hand-made listing); the serial row does.
		order = self._order(fx.order_detail(ffl_received=False))
		order.ffl_received = 0
		order.lines[0].item_code = ""
		with patch.object(fulfillment, "is_antique_item",
				side_effect=lambda code: code == "COLT-PYTHON"):
			fulfillment._require_ready_to_dispose(order)  # no raise

	def test_an_order_with_nothing_matched_is_refused(self):
		order = self._order()
		order.set("serials", [])
		with self.assertRaises(Exception) as caught:
			fulfillment._require_ready_to_dispose(order)
		self.assertIn("no gun matched", str(caught.exception).lower())

	def test_an_unresolved_line_blocks_the_whole_order(self):
		# Disposing now would ship part of the order and mark the whole thing
		# transferred, which is a gap in the bound book.
		order = self._order()
		order.lines[0].reservation_error = "Held by woo WOO-ORD-7"
		with self.assertRaises(Exception) as caught:
			fulfillment._require_ready_to_dispose(order)
		self.assertIn("WOO-ORD-7", str(caught.exception))


class TestDispositionNote(_FulfillmentCase):
	def test_the_note_names_the_order_and_the_item(self):
		# The month-end reconciliation against GunBroker's statement matches on
		# them.
		order = self._order()
		note = fulfillment._disposition_note(order, order.serials[0])
		self.assertIn("998877", note)
		self.assertIn("16087230", note)

	def test_a_collector_transfer_records_who_verified_it_and_when(self):
		# There is no dealer at the far end and no 4473 — the store's own check
		# is the entire paper trail, so it belongs in the book.
		order = self._order(is_cr_transfer=1, cr_verified_by="clerk@example.com",
			cr_verified_at="2026-08-26 11:00:00")
		note = fulfillment._disposition_note(order, order.serials[0])
		self.assertIn("clerk@example.com", note)
		self.assertIn("2026-08-26", note)


# ---------------------------------------------------------------------------
# Recording the money
# ---------------------------------------------------------------------------

class TestARecordedPaymentIsNotEditedTwice(_FulfillmentCase):
	"""revenue._ensure_paid books exactly one receipt, on purpose.

	It returns immediately when payment_entry is set, so a retry cannot post a
	second one. Recording again therefore rewrites the amount, the date and the
	cheque number on the ORDER while the entry on the ledger keeps the old
	ones — permanently, invisibly, and on the two fields the year-end journal
	and the cash-basis reconciliation read.
	"""

	def test_recording_again_over_a_posted_receipt_is_refused(self):
		order = self._order(payment_entry="ACC-PAY-0001", payment_amount=1245.99)
		self._patch_order_doc(order)
		self._patch("frappe.db.get_value", return_value=1)
		with self.assertRaises(Exception) as caught:
			fulfillment.mark_payment_received("GB-ORD-00001", "GunBroker Check",
				999.00, reference_no="4471")
		self.assertIn("ACC-PAY-0001", str(caught.exception))
		self.assertEqual(order.payment_amount, 1245.99)

	def test_the_override_records_it_and_says_so(self):
		order = self._order(payment_entry="ACC-PAY-0001", payment_amount=1245.99)
		self._patch_order_doc(order)
		self._patch("frappe.db.get_value", return_value=1)
		self._patch_object(fulfillment, "_flag_payment_received")
		out = fulfillment.mark_payment_received("GB-ORD-00001", "GunBroker Check",
			999.00, reference_no="4471", replace_posted_receipt=1)
		self.assertTrue(out["ok"])
		self.assertEqual(order.payment_amount, 999.00)
		self.assertIn("gb_receipt_overwritten_998877", self.alerts)

	def test_a_cancelled_receipt_does_not_block_recording_the_replacement(self):
		# The cheque bounced, the entry was
		# cancelled, the money was chased and it arrived. Recording the
		# replacement is the right thing to do — refusing it, and then mailing
		# "the ledger was not changed" when the operator overrode the refusal,
		# was wrong twice over.
		order = self._order(payment_entry="ACC-PAY-0001", order_total=1245.99)
		self._patch_order_doc(order)
		self._patch("frappe.db.get_value", return_value=2)
		self._patch_object(fulfillment, "_flag_payment_received")
		out = fulfillment.mark_payment_received("GB-ORD-00001", "GunBroker Check",
			1245.99, reference_no="4472")
		self.assertTrue(out["ok"])
		self.assertEqual(self.alerts, [])

	def test_an_order_with_no_receipt_yet_is_untouched_by_the_guard(self):
		order = self._order(payment_entry="", order_total=1245.99)
		self._patch_order_doc(order)
		self._patch_object(fulfillment, "_flag_payment_received")
		out = fulfillment.mark_payment_received("GB-ORD-00001", "GunBroker Check",
			1245.99, reference_no="4471")
		self.assertTrue(out["ok"])
		self.assertEqual(self.alerts, [])


class TestThePaymentMethodIsSetUp(_FulfillmentCase):
	"""Fail here, where nothing has happened, not after the gun has gone.

	Recording the payment is what opens the Dispose gate, and the receipt it
	promises is not written until the invoice is raised — which is after the
	dispositions are booked and the parcel is on its way. A Mode of Payment with
	no account behind it therefore surfaced as a logged error on an order that
	was already shipped, invoiced, and showing the full amount outstanding.

	The seed that builds those modes runs on install and on migrate and nowhere
	else, and the ordinary go-live order is: migrate an empty site, then fill
	the deposit account in.
	"""

	def _record(self):
		return fulfillment.mark_payment_received("GB-ORD-00001", "GunBroker Check",
			1245.99, reference_no="4471")

	def test_a_mode_with_no_account_is_refused_before_anything_happens(self):
		order = self._order(payment_entry="", order_total=1245.99)
		self._patch_order_doc(order)
		self._patch("ffl_core.api.manual_order._mode_of_payment_account",
			return_value=None)
		with self.assertRaises(Exception) as caught:
			self._record()
		self.assertIn("GunBroker Settings", str(caught.exception))
		self.assertEqual(order.saves, 0, "the order was written anyway")

	def test_a_mode_pointing_at_the_old_account_is_refused(self):
		# The other half: change the deposit account in Settings and,
		# until somebody happens to migrate, every receipt keeps landing in the
		# account that was configured the day the mode was created.
		order = self._order(payment_entry="", order_total=1245.99)
		self._patch_order_doc(order)
		self._patch("ffl_core.api.manual_order._mode_of_payment_account",
			return_value="Old Bank - TSA")
		self._patch("frappe.db.get_single_value", return_value="New Bank - TSA")
		with self.assertRaises(Exception) as caught:
			self._record()
		self.assertIn("Old Bank - TSA", str(caught.exception))
		self.assertIn("New Bank - TSA", str(caught.exception))

	def test_a_correctly_pointed_mode_records_normally(self):
		order = self._order(payment_entry="", order_total=1245.99)
		self._patch_order_doc(order)
		self._patch_object(fulfillment, "_flag_payment_received")
		self.assertTrue(self._record()["ok"])


class TestCorrectingTheDepositDetails(_FulfillmentCase):
	"""Correcting the deposit details needs a way back out.

	The four payment fields are read-only and the document refuses to save when
	a mode is recorded with no deposit date, so a row that reached that state is
	reported by the daily audit and then cannot be repaired through the Desk at
	all — the field will not accept typing, and opening the form and saving
	throws.
	"""

	def test_the_date_is_corrected_and_the_change_is_recorded(self):
		order = self._order(payment_mode_used="GunBroker Check",
			payment_received_on=None, payment_reference_no="4471")
		self._patch_order_doc(order)
		out = fulfillment.correct_payment_details("GB-ORD-00001", "2026-08-20")
		self.assertTrue(out["ok"])
		self.assertEqual(order.payment_received_on, "2026-08-20")
		self.assertTrue(order.comments)
		self.assertIn("2026-08-20", order.comments[0])

	def test_a_future_deposit_date_is_refused_here_too(self):
		order = self._order(payment_received_on=None)
		self._patch_order_doc(order)
		with self.assertRaises(Exception):
			fulfillment.correct_payment_details("GB-ORD-00001", "2027-01-01")

	def test_the_amount_and_the_mode_are_not_reachable_from_here(self):
		# Changing what was banked is mark_payment_received's job, and once a
		# receipt is posted it is an accounting correction rather than an edit.
		import inspect

		params = inspect.signature(fulfillment.correct_payment_details).parameters
		self.assertEqual(list(params),
			["gunbroker_order", "received_on", "reference_no"])


class TestMarkPaymentReceived(_FulfillmentCase):
	def setUp(self):
		super().setUp()
		self.order = self._order(payment_mode_used="", payment_received=0,
			order_total=1245.99)
		self._patch_order_doc(self.order)
		self._patch_object(fulfillment, "_flag_payment_received",
			side_effect=lambda order: None)
		self._patch("frappe.format_value", side_effect=lambda v, *a, **k: str(v))

	def _call(self, **kw):
		args = {"gunbroker_order": "GB-ORD-00001",
			"mode_of_payment": fulfillment.MODE_CHECK, "amount": 1245.99,
			"reference_no": "9001", "received_on": "2026-08-26"}
		args.update(kw)
		return fulfillment.mark_payment_received(**args)

	def test_a_cheque_is_recorded_with_everything_needed_to_trace_it(self):
		with patch.object(fulfillment, "_card_checkout_enabled", return_value=False):
			out = self._call()
		self.assertTrue(out["ok"])
		self.assertEqual(self.order.payment_mode_used, "GunBroker Check")
		self.assertEqual(self.order.payment_reference_no, "9001")
		self.assertEqual(self.order.payment_amount, 1245.99)
		self.assertEqual(str(self.order.payment_received_on), "2026-08-26")
		self.assertEqual(self.order.payment_received, 1)

	def test_a_cheque_with_no_number_is_refused(self):
		# It is the only thing that ties this order to a line on the bank
		# statement.
		with patch.object(fulfillment, "_card_checkout_enabled", return_value=False), \
				self.assertRaises(Exception) as caught:
			self._call(reference_no="")
		self.assertIn("cheque", str(caught.exception).lower())

	def test_a_card_payment_is_refused_while_no_gateway_is_connected(self):
		# There is no card money to record. If one really did arrive, that is
		# worth investigating before it is booked.
		with patch.object(fulfillment, "_card_checkout_enabled", return_value=False), \
				self.assertRaises(Exception) as caught:
			self._call(mode_of_payment=fulfillment.MODE_CARD, reference_no="txn-1")
		self.assertIn("gateway", str(caught.exception).lower())

	def test_a_card_payment_records_once_the_gateway_switch_is_on(self):
		# The path is kept alive and covered so turning the switch on later is a
		# settings change and not a code change.
		with patch.object(fulfillment, "_card_checkout_enabled", return_value=True):
			self._call(mode_of_payment=fulfillment.MODE_CARD, reference_no="txn-1")
		self.assertEqual(self.order.payment_mode_used, "GunBroker Card")

	def test_a_future_deposit_date_is_refused(self):
		with patch.object(fulfillment, "_card_checkout_enabled", return_value=False), \
				self.assertRaises(Exception):
			self._call(received_on="2099-01-01")

	def test_a_missing_deposit_date_defaults_to_today_and_is_never_left_blank(self):
		# The cash-basis schedule selects on "deposited on or before period
		# end", and NULL is not <= anything — an order with the gun shipped, the
		# money in the bank and no date would be invisible on the schedule the
		# accountant works from.
		with patch.object(fulfillment, "_card_checkout_enabled", return_value=False):
			self._call(received_on=None)
		self.assertTrue(self.order.payment_received_on)

	def test_an_unrecognised_payment_method_is_refused(self):
		with self.assertRaises(Exception):
			self._call(mode_of_payment="Cash")

	def test_a_zero_amount_is_refused(self):
		with patch.object(fulfillment, "_card_checkout_enabled", return_value=False), \
				self.assertRaises(Exception):
			self._call(amount=0)

	def test_a_short_payment_is_recorded_and_flagged(self):
		# The invoice is still raised for the full amount; the balance shows up
		# on the daily audit until it is settled.
		with patch.object(fulfillment, "_card_checkout_enabled", return_value=False):
			out = self._call(amount=1000.00)
		self.assertAlmostEqual(out["difference"], -245.99, places=2)
		self.assertIn("gb_payment_diff_998877", self.alerts)
		self.assertTrue(self.messages)

	def test_an_exact_payment_raises_no_alarm(self):
		with patch.object(fulfillment, "_card_checkout_enabled", return_value=False):
			out = self._call()
		self.assertEqual(out["difference"], 0)
		self.assertEqual(self.alerts, [])


# ---------------------------------------------------------------------------
# The queue card
# ---------------------------------------------------------------------------

class TestOrderStage(_FulfillmentCase):
	def test_no_recorded_payment_is_awaiting_payment(self):
		order = self._order(payment_mode_used="", payment_received=1)
		self.assertEqual(fulfillment.order_stage(order),
			fulfillment.STAGE_AWAITING_PAYMENT)

	def test_the_boundary_is_the_same_fact_the_server_gate_reads(self):
		# Reading GunBroker's own flag here instead would put a Dispose button
		# on a card the server then refuses.
		order = self._order(payment_mode_used="GunBroker Check")
		self.assertEqual(fulfillment.order_stage(order),
			fulfillment.STAGE_READY_TO_DISPOSE)

	def test_a_disposed_order_with_no_shipment_is_awaiting_ship(self):
		order = self._order(payment_mode_used="GunBroker Check",
			order_status="Transferred", sales_invoice="ACC-SINV-1")
		with patch("frappe.db.get_value", return_value=frappe._dict(
				{"name": "ACC-SINV-1", "shipstation_order_id": "",
					"shipstation_status": "Not Requested"})):
			self.assertEqual(fulfillment.order_stage(order),
				fulfillment.STAGE_AWAITING_SHIP)

	def test_a_pushed_shipment_is_shipped(self):
		order = self._order(payment_mode_used="GunBroker Check",
			order_status="Transferred", sales_invoice="ACC-SINV-1")
		with patch("frappe.db.get_value", return_value=frappe._dict(
				{"name": "ACC-SINV-1", "shipstation_order_id": "se-99",
					"shipstation_status": "Pushed"})):
			self.assertEqual(fulfillment.order_stage(order),
				fulfillment.STAGE_SHIPPED)


class TestPendingRow(_FulfillmentCase):
	# The page reads every one of these unconditionally; a missing key renders
	# as "undefined" on a card a cashier is working from, or worse renders a
	# counter-only button.
	REQUIRED_KEYS = (
		"source", "gunbroker_order", "gb_order_id", "gb_status_label", "stage", "channel_stage",
		"blocked_reason", "age_days", "overdue", "awaiting_payment",
		"posting_date", "customer", "customer_name", "is_business", "paid",
		"paid_total", "outstanding_amount", "grand_total", "order_total",
		"currency", "shipping_total", "discount_total", "total_tax",
		"needs_disposition", "has_disposition", "disposed", "disposed_serials",
		"awaiting_serial", "serial_errors", "pending_serials", "store_pickup",
		"pickup_invoice", "destination_ffl", "destination_ffl_name",
		"ffl_expiry_date", "ffl_expired", "is_cr", "cr_prefill", "sales_invoice",
		"sales_invoice_posted",
		"sales_orders", "legs", "note", "last_payment_mode", "ship_pushed",
		"can_mark_shipped", "shipstation_enabled", "shipstation_order_id",
		"ship_items", "has_ship_address", "ship_to_preview", "site",
		"woo_order_id", "woo_status", "woo_online_order", "ships_direct",
		"sync_error", "sync_fail_count",
	)

	def _row(self, order):
		with patch("frappe.get_all", return_value=[]), \
				patch("frappe.db.get_value", return_value=None):
			return fulfillment._pending_row(order, ss_enabled=True)

	def test_the_row_carries_every_key_the_page_reads(self):
		row = self._row(self._order(payment_mode_used="GunBroker Check"))
		missing = [k for k in self.REQUIRED_KEYS if k not in row]
		self.assertEqual(missing, [], f"row contract is missing: {missing}")

	def test_only_a_submitted_invoice_counts_as_posted(self):
		"""A blocked submit leaves the order linked to a Draft, which Ship refuses."""
		order = self._order(payment_mode_used="GunBroker Check",
			order_status="Transferred", sales_invoice="ACC-SINV-1")
		for docstatus, posted in ((0, False), (1, True)):
			with patch("frappe.get_all", return_value=[]), \
					patch.object(fulfillment, "_invoice_row",
						return_value={"name": "ACC-SINV-1", "docstatus": docstatus}), \
					patch.object(fulfillment, "_ship_to_preview", return_value=None), \
					patch.object(fulfillment, "_ship_items", return_value=[]):
				row = fulfillment._pending_row(order, ss_enabled=True)
			self.assertIs(row["sales_invoice_posted"], posted)

	def test_a_gunbroker_row_never_claims_to_be_a_pickup_or_a_web_order(self):
		row = self._row(self._order(payment_mode_used="GunBroker Check"))
		self.assertEqual(row["source"], "gunbroker")
		self.assertFalse(row["store_pickup"])
		self.assertIsNone(row["woo_online_order"])
		self.assertFalse(row["can_mark_shipped"])

	def test_a_finished_order_leaves_the_queue(self):
		order = self._order(payment_mode_used="GunBroker Check",
			order_status="Transferred", sales_invoice="ACC-SINV-1",
			tracking_pushed_at="2026-08-27 09:00:00")
		with patch("frappe.get_all", return_value=[]), \
				patch("frappe.db.get_value", return_value=frappe._dict(
					{"name": "ACC-SINV-1", "shipstation_order_id": "se-99",
						"shipstation_status": "Pushed"})):
			self.assertIsNone(fulfillment._pending_row(order, ss_enabled=True))

	def _antique_order(self):
		detail = fx.order_detail(sku=f"{ANTIQUE}::SN-A1", serial="SN-A1",
			ffl_number="", ffl_received=False)
		detail["orderItemsCollection"][0]["isFFLRequired"] = False
		order = self._order(detail, payment_mode_used="GunBroker Check")
		order.ffl_received = 0
		# apply_order_fields is what copies these off the detail; _order only
		# applies the lines, so the buyer's address is set here by hand.
		order.ship_to_name = "Dave Collector"
		order.ship_to_line1 = "12 Elm St"
		order.ship_to_city = "Dallas"
		order.ship_to_state = "TX"
		order.ship_to_zip = "75201"
		order.customer_phone = "214-555-0100"
		return order

	def test_an_all_antique_order_ships_to_the_buyer_and_carries_no_licence(self):
		with patch.object(fulfillment, "is_antique_item", return_value=True), \
				patch.object(fulfillment, "is_regulated_firearm_item", return_value=False):
			row = self._row(self._antique_order())
		self.assertTrue(row["ships_direct"])
		self.assertNotIn("ffl_unverified", row)
		self.assertFalse(row["ffl_expired"])
		self.assertEqual(row["destination_ffl"], "")
		self.assertEqual(row["destination_ffl_name"], "")
		self.assertTrue(row["has_ship_address"])
		self.assertEqual(row["ship_to_preview"]["city"], "Dallas")
		self.assertIsNone(row["ship_to_preview"]["company"])
		self.assertTrue(row["needs_disposition"], "still Active: nothing has happened yet")

	def test_an_issued_antique_counts_as_handled(self):
		def _status(doctype, *args, **kwargs):
			return "Consumed" if doctype == "Serial No" else None

		order = self._antique_order()
		order.order_status = "Transferred"
		order.ships_direct = 1
		with patch("frappe.get_all", return_value=[]), \
				patch("frappe.db.get_value", side_effect=_status), \
				patch.object(fulfillment, "is_antique_item", return_value=True):
			row = fulfillment._pending_row(order, ss_enabled=True)
		self.assertFalse(row["needs_disposition"])
		self.assertIn("SN-A1", row["disposed_serials"])
		self.assertEqual(row["channel_stage"], fulfillment.STAGE_AWAITING_SHIP)
		self.assertEqual(row["stage"], "ready_ship")
		self.assertTrue(row["ships_direct"], "read off the order once it is Transferred")
		self.assertEqual(row["ship_to_preview"]["city"], "Dallas")

	def test_a_regulated_order_still_previews_the_dealer_not_the_buyer(self):
		order = self._order(payment_mode_used="GunBroker Check")
		order.ship_to_line1 = "12 Elm St"
		with patch.object(fulfillment, "is_antique_item", return_value=False), \
				patch.object(fulfillment, "is_regulated_firearm_item", return_value=True):
			row = self._row(order)
		self.assertFalse(row["ships_direct"])
		self.assertIsNone(row["ship_to_preview"],
			"no FFL record and no invoice yet: nothing honest to preview")

	def test_an_unmatched_line_surfaces_as_a_serial_error(self):
		order = self._order(payment_mode_used="GunBroker Check")
		order.lines[0].reservation_error = "Unmatched GunBroker item 16087230"
		row = self._row(order)
		self.assertEqual(len(row["serial_errors"]), 1)
		self.assertIn("Unmatched", row["serial_errors"][0]["error"])


# ---------------------------------------------------------------------------
# Item-level lines: a quantity of stock, no serial, no bound book
# ---------------------------------------------------------------------------

AMMO = "AMMO-9MM-115"


class TestDisposeIssuesItemLevelStock(_FulfillmentCase):
	"""An item-level line sold N interchangeable units — ammunition, optics,
	parts. It owes no serial, no disposition and nothing to FastBound, but the
	stock still has to leave: the invoice is raised with update_stock=0 because
	the GUNS on it left under their dispositions, so something has to move what
	did not."""

	def _ammo_order(self, *, qty=2, ffl_number=""):
		order = self._order(fx.order_detail(sku=AMMO, serial="", ffl_received=False,
			ffl_number=ffl_number))
		order.set("serials", [])          # an item-level line has no serial row
		order.lines[0].item_code = AMMO
		order.lines[0].qty = qty
		order.lines[0].is_ffl_required = 0
		order.ffl_received = 0
		order.ffl_number = ffl_number
		return order

	def _dispose(self, order, *, issue_result="MAT-STE-9001"):
		set_values: list[tuple] = []
		self._patch_order_doc(order)
		with patch("frappe.db.set_value",
					side_effect=lambda *a, **k: set_values.append(a)), \
				patch.object(fulfillment, "is_own_license", return_value=False), \
				patch.object(fulfillment, "is_antique_item", return_value=False), \
				patch.object(fulfillment, "is_regulated_firearm_item",
					side_effect=lambda code: code == "COLT-PYTHON"), \
				patch.object(fulfillment.gb_orders, "is_item_level_line",
					side_effect=lambda line: (line.item_code or "") == AMMO), \
				patch.object(fulfillment, "safe_log_error"), \
				patch("ffl_core.firearm.issue_stock_qty",
					return_value=issue_result) as issue, \
				patch("ffl_core.warehouses.default_stock_warehouse",
					return_value="Stores - TSA"), \
				patch.object(fulfillment, "_order_company", return_value="TSA"), \
				patch.object(fulfillment, "_resolve_and_validate_ffl",
					return_value="FFL-REC-1") as resolve, \
				patch.object(fulfillment, "_book_dispositions",
					side_effect=lambda o, d, rows=None: (
						["FD-1"], [r.confirmed_serial for r in rows])) as book, \
				patch("ffl_integrations.gunbroker.revenue.create_sales_invoice_for_gb_order",
					return_value={"sales_invoice": "ACC-SINV-9", "status": "created"}):
			result = fulfillment.dispose_gb_order("GB-ORD-00001")
		return result, issue, resolve, book, set_values

	def test_an_all_ammunition_order_issues_stock_and_books_nothing(self):
		order = self._ammo_order(qty=2)
		result, issue, resolve, book, _sets = self._dispose(order)
		book.assert_not_called()
		resolve.assert_not_called()
		issue.assert_called_once()
		self.assertEqual(issue.call_args.args[0], AMMO)
		self.assertEqual(issue.call_args.args[1], 2)
		self.assertEqual(result["issued"][0]["qty"], 2)
		self.assertEqual(result["disposed_count"], 0)

	def test_it_ships_to_the_buyer_rather_than_a_dealer(self):
		"""Nothing on the order needs a licence, so the parcel goes to the buyer
		— and the decision is written down for the invoice and the card."""
		order = self._ammo_order()
		_result, _issue, _resolve, _book, set_values = self._dispose(order)
		flip = [a for a in set_values if a[0] == fulfillment.ORDER_DOCTYPE][-1]
		self.assertEqual(flip[2]["ships_direct"], 1)
		self.assertEqual(flip[2]["order_status"], "Transferred")

	def test_a_buyer_supplied_licence_still_routes_the_parcel_to_the_dealer(self):
		"""A licence on the order is not ignored just because nothing on it
		needed one — same rule the antique path follows."""
		order = self._ammo_order(ffl_number="1-23-456-78-9A-01234")
		_result, _issue, _resolve, _book, set_values = self._dispose(order)
		flip = [a for a in set_values if a[0] == fulfillment.ORDER_DOCTYPE][-1]
		self.assertEqual(flip[2]["ships_direct"], 0)

	def test_the_cost_is_debited_to_cost_of_goods_sold(self):
		"""Not to stock_adjustment_account, where a Material Issue lands by
		default — that is the shrinkage account, and the same box of ammunition
		sold at the counter debits COGS. Two accounts for one product is a
		permanent hand-written carve-out at month end."""
		order = self._ammo_order()
		with patch("frappe.get_cached_value", return_value="Cost of Goods Sold - TSA"):
			_result, issue, _resolve, _book, _sets = self._dispose(order)
		self.assertEqual(
			issue.call_args.kwargs["expense_account"], "Cost of Goods Sold - TSA")

	def test_the_stock_entry_names_the_order(self):
		"""A quantity carries no state to re-read, so the entry's remark is the
		only thing that makes a duplicate traceable if one is ever forced."""
		order = self._ammo_order()
		_result, issue, _resolve, _book, _sets = self._dispose(order)
		self.assertIn("998877", issue.call_args.kwargs["remarks"])


class TestTheCardShowsItemLevelUnits(_FulfillmentCase):
	def _row(self, order):
		with patch.object(fulfillment, "_invoice_row", return_value={}), \
				patch.object(fulfillment, "_ffl_record", return_value={}), \
				patch.object(fulfillment, "_serial_lists", return_value=([], set(), [])), \
				patch.object(fulfillment.gb_orders, "is_item_level_line",
					side_effect=lambda line: (line.item_code or "") == AMMO), \
				patch.object(fulfillment, "needs_dealer", return_value=False), \
				patch.object(fulfillment, "_ship_to_preview", return_value="Dallas, TX"), \
				patch.object(fulfillment, "_ship_items", return_value=[]):
			return fulfillment._pending_row(order, ss_enabled=True)

	def _ammo_order(self, **fields):
		order = self._order(fx.order_detail(sku=AMMO, serial="", ffl_received=False),
			**fields)
		order.set("serials", [])
		order.lines[0].item_code = AMMO
		order.lines[0].qty = 3
		return order

	def test_the_ship_dialog_lists_the_quantity_it_will_ship(self):
		"""The Ship dialog and the ShipStation parcel come off the same rows, so
		an ammunition-only order no longer reads as "(none)"."""
		items = fulfillment._ship_items(self._ammo_order())
		self.assertEqual([(i["item_code"], i["qty"], i["serials"]) for i in items],
			[(AMMO, 3, [])])

	def test_an_open_order_lists_the_quantity_instead_of_a_serial(self):
		row = self._row(self._ammo_order())
		self.assertEqual(row["stock_units"], [{"item_code": AMMO, "qty": 3}])
		self.assertTrue(row["needs_disposition"])
		self.assertEqual(row["pending_serials"], [])

	def test_a_transferred_order_no_longer_shows_them_as_outstanding(self):
		"""The stock has gone; leaving them on the card would read as work still
		to do on an order that is finished with."""
		row = self._row(self._ammo_order(order_status="Transferred", ships_direct=1))
		self.assertEqual(row["stock_units"], [])
		self.assertFalse(row["needs_disposition"])

	def test_an_all_stock_order_reads_as_shipping_direct(self):
		"""Without the item-level branch this order has no serial rows at all and
		would read as undecided — the card would offer a dealer nothing needs."""
		order = self._ammo_order()
		with patch.object(fulfillment.gb_orders, "is_item_level_line",
					return_value=True), \
				patch.object(fulfillment, "needs_dealer", return_value=False):
			self.assertTrue(fulfillment.ships_direct(order))
