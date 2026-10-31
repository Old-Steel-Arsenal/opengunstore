"""Dispose-time wiring for a C&R order — destination resolution, note, payload.

Three separate things are pinned here, and each has a specific way of going
wrong:

* **Resolution must not reach for eZ Check on an 03 number.** The ATF directory
  has no collectors in it, so the lookup fails and the operator is told
  "Destination FFL Unresolved — check the eZ Check connection" when the real
  answer is "nobody has verified this licence yet". Worse, a lookup that
  somehow returned something would manufacture a destination nobody confirmed.
* **The refusal must happen before anything is booked.** No ATF FFL Record
  created, no disposition created.
* **The dealer path must be untouched** — same eZ Check call, same errors.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import fulfillment

CR = "1-23-456-03-7C-89012"
DEALER = "5-99-888-01-9D-26128"


def _only(doctype, stub, dict_stub=None):
	"""patch side_effect that answers for ONE doctype and delegates the rest to
	the real frappe.get_doc.

	A blanket return_value also answers frappe's internal lookups — most
	importantly get_doc("System Settings"), which now_datetime() consults for
	the timezone. That makes the test's result depend on whether the timezone
	cache happens to be warm: green in a full suite run, broken when the module
	runs alone."""
	real = frappe.get_doc

	def _fn(*args, **kwargs):
		if args and args[0] == doctype:
			return stub
		# A dict literal is a doc being CONSTRUCTED (the FFL Disposition here);
		# handing that to the real get_doc would try to build a live document.
		if dict_stub is not None and args and isinstance(args[0], dict):
			return dict_stub
		return real(*args, **kwargs)
	return _fn


def _order(**over):
	o = MagicMock()
	o.name = "WOO-ORD-00042"
	o.woo_order_id = "4242"
	o.order_status = "Pending Fulfillment"
	o.woo_status = "completed"
	o.is_cr_transfer = 1
	o.ffl_license = CR
	o.atf_ffl_record = ""
	o.customer_name = "Jane Q. Collector"
	o.customer_phone = "512-555-0100"
	# Explicit, and matching VALID_DETAILS: left as MagicMock attributes these
	# resolve to None through normalize_us_state(), which switches the entire
	# state/ZIP consistency gate off — the "compliance trunk success path" would
	# then never once run on an order that HAS a shipping address.
	o.ship_to_state = "TX"
	o.ship_to_zip = "78701"
	o.cr_verified_by = "clerk@example.com"
	o.cr_verified_at = "2026-07-29 10:00:00"
	o.site = "retail"
	o.serials = []
	for k, v in over.items():
		setattr(o, k, v)
	return o


class ResolveDestinationTest(FrappeTestCase):
	def test_unverified_cr_order_is_refused_without_touching_ez_check(self):
		"""And the error must name the missing verification, not a lookup."""
		with patch("ffl_core.compliance.dealer_transfer._resolve_destination_ffl") as ez, \
			 patch("ffl_core.api.manual_order._require_valid_ffl") as gate:
			with self.assertRaises(frappe.ValidationError) as cm:
				fulfillment._resolve_and_validate_ffl(_order())
		self.assertIn("C&R", str(cm.exception))
		self.assertFalse(ez.called, "eZ Check was consulted for a Type 03 licence")
		self.assertFalse(gate.called)

	def test_verified_cr_order_uses_the_record_and_still_runs_the_shared_gate(self):
		"""The C&R branch supplies the record; it does NOT get to skip the
		unexpired + complete-address gate that every shipment passes."""
		order = _order(atf_ffl_record=CR)
		with patch("ffl_core.api.manual_order._require_valid_ffl") as gate, \
			 patch("frappe.db.set_value"):
			dest = fulfillment._resolve_and_validate_ffl(order)
		self.assertEqual(dest, CR)
		self.assertTrue(gate.called, "the shared shippable-FFL gate was bypassed")

	def test_dealer_order_still_resolves_through_ez_check(self):
		order = _order(is_cr_transfer=0, ffl_license=DEALER)
		with patch("ffl_core.compliance.dealer_transfer._resolve_destination_ffl",
				return_value=DEALER) as ez, \
			 patch("ffl_core.api.manual_order._require_valid_ffl"), \
			 patch("frappe.db.set_value"):
			dest = fulfillment._resolve_and_validate_ffl(order)
		self.assertEqual(dest, DEALER)
		self.assertTrue(ez.called)

	def test_order_with_no_licence_at_all_keeps_its_original_error(self):
		order = _order(is_cr_transfer=0, ffl_license="")
		with self.assertRaises(frappe.ValidationError) as cm:
			fulfillment._resolve_and_validate_ffl(order)
		self.assertIn("no receiving FFL", str(cm.exception))


class DispositionNoteTest(FrappeTestCase):
	def test_dealer_note_is_unchanged(self):
		note = fulfillment._disposition_note(_order(is_cr_transfer=0, ffl_license=DEALER))
		self.assertEqual(note, "Auto-booked from web order WOO-ORD-00042 (Woo #4242).")

	def test_cr_note_records_who_verified_and_when(self):
		"""There is no 4473 and no receiving dealer on this path — the store's own
		check is the entire paper trail, so it goes in the book."""
		note = fulfillment._disposition_note(_order())
		self.assertIn("C&R (Type 03) transfer", note)
		self.assertIn("clerk@example.com", note)
		self.assertIn("2026-07-29", note)

	def test_cr_note_fits_fastbound_note_limit(self):
		self.assertLess(len(fulfillment._disposition_note(_order())), 1000)

	def test_cr_note_survives_a_datetime_object(self):
		from datetime import datetime
		note = fulfillment._disposition_note(_order(cr_verified_at=datetime(2026, 7, 29, 10, 0)))
		self.assertIn("2026-07-29", note)


class DisposeGuardOrderTest(FrappeTestCase):
	"""The verification runs BEFORE resolution and BEFORE any booking."""

	def _dispose(self, order, cr_details=None):
		sr = frappe._dict({"item_code": "GUN-1", "confirmed_serial": "SN-1",
			"woo_line_id": "7", "unit_index": 0, "stock_hold_serial": "",
			"reservation_error": ""})
		order.serials = [sr]
		with patch("frappe.only_for"), \
			 patch("frappe.get_doc", side_effect=_only("Woo Online Order", order,
				dict_stub=MagicMock())), \
			 patch("ffl_woo_sync.woocommerce.fulfillment._locally_disposed_rows",
				return_value=[sr]), \
			 patch("ffl_woo_sync.woocommerce.fulfillment._resolve_and_validate_ffl") as resolve, \
			 patch("frappe.db.exists", return_value=False) as exists:
			self.resolve, self.exists = resolve, exists
			return fulfillment.dispose_web_order(order.name, cr_details)

	def test_cr_order_without_details_never_reaches_resolution_or_booking(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self._dispose(_order())
		self.assertIn("C&R", str(cm.exception))
		self.assertFalse(self.resolve.called, "resolution ran before verification")

	def test_a_stale_flag_does_not_demand_cr_details_on_a_dealer_order(self):
		"""Symmetric to the row test: a dealer number with a stale flag must
		dispose normally, not be blocked demanding a collector verification."""
		with patch("ffl_woo_sync.woocommerce.fulfillment._delist_disposed"), \
			 patch("ffl_woo_sync.woocommerce.fulfillment._sold_prices", return_value={}), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.create_sales_invoice_for_web_order",
				return_value={"sales_invoice": None, "status": "ok"}), \
			 patch("frappe.db.set_value"):
			result = self._dispose(_order(is_cr_transfer=1, ffl_license=DEALER))
		self.assertTrue(result["ok"])

	def test_cr_details_rejected_on_a_non_cr_order(self):
		"""Collector verification must not be stamped onto a dealer order."""
		details = {"license_number": CR, "license_expiry": "2028-07-31",
			"contact_name": "Jane", "premise_street": "1 St", "premise_street2": "",
			"premise_city": "Austin", "premise_state": "TX", "premise_zip": "78701"}
		with self.assertRaises(frappe.ValidationError) as cm:
			self._dispose(_order(is_cr_transfer=0, ffl_license=DEALER), details)
		self.assertIn("not a C&R transfer", str(cm.exception))


class PendingRowPayloadTest(FrappeTestCase):
	"""list_pending_web_orders feeds the dialog's prefill."""

	def _rows(self, doc, license_no=CR):
		orders = [frappe._dict({
			"name": "WOO-ORD-1", "woo_status": "completed", "woo_order_id": "101",
			"customer_name": "Jane Q. Collector", "atf_ffl_record": "", "ffl_license": license_no,
			"ffl_business_name": "", "ffl_expiration": "", "creation": "2026-07-01 10:00:00",
			"site": "retail", "currency": "USD", "order_total": 100, "total_tax": 0,
			"shipping_total": 0, "discount_total": 0})]
		with patch("frappe.only_for"), \
			 patch("frappe.get_all", return_value=orders), \
			 patch("frappe.get_doc", side_effect=_only("Woo Online Order", doc)), \
			 patch("frappe.db.get_value", return_value=None), \
			 patch("ffl_woo_sync.woocommerce.fulfillment._distributor_order_map", return_value={}), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.nowdate", return_value="2026-07-29"):
			return fulfillment.list_pending_web_orders()

	def _doc(self, **over):
		d = MagicMock()
		d.serials = [frappe._dict({"item_code": "GUN-1", "confirmed_serial": "SN-1",
			"reservation_error": ""})]
		d.is_cr_transfer = 1
		d.ffl_license = CR
		d.customer_name = "Jane Q. Collector"
		d.cr_contact_name = ""
		d.cr_license_expiry = None
		d.cr_premise_street = ""
		d.cr_premise_street2 = ""
		d.cr_premise_city = ""
		d.cr_premise_state = ""
		d.cr_premise_zip = ""
		d.ship_to_line1 = "123 Main St"
		d.ship_to_line2 = "Apt 4"
		d.ship_to_city = "Austin"
		d.ship_to_state = "TX"
		d.ship_to_zip = "78701"
		for k, v in over.items():
			setattr(d, k, v)
		return d

	def test_cr_row_prefills_from_the_shipping_address(self):
		row = self._rows(self._doc())[0]
		self.assertTrue(row["is_cr"])
		p = row["cr_prefill"]
		self.assertEqual(p["license_number"], CR)
		self.assertEqual(p["contact_name"], "Jane Q. Collector")
		self.assertEqual(p["premise_street"], "123 Main St")
		self.assertEqual(p["premise_city"], "Austin")
		self.assertEqual(p["premise_zip"], "78701")

	def test_expiry_is_never_prefilled_on_a_first_verification(self):
		"""Forces staff to actually look at the licence rather than accept a
		date the system suggested."""
		self.assertIsNone(self._rows(self._doc())[0]["cr_prefill"]["license_expiry"])

	def test_stored_verification_wins_over_the_shipping_address(self):
		"""Retry after a failed dispose must give back what was typed."""
		doc = self._doc(cr_premise_city="Dallas", cr_license_expiry="2028-07-31",
			cr_contact_name="Jane Collector")
		p = self._rows(doc)[0]["cr_prefill"]
		self.assertEqual(p["premise_city"], "Dallas")
		self.assertEqual(p["license_expiry"], "2028-07-31")
		self.assertEqual(p["contact_name"], "Jane Collector")

	def test_non_cr_row_carries_no_prefill(self):
		row = self._rows(self._doc(is_cr_transfer=0), license_no=DEALER)[0]
		self.assertFalse(row["is_cr"])
		self.assertIsNone(row["cr_prefill"])

	def test_a_stale_flag_cannot_fake_a_cr_row(self):
		"""The stored flag is derived data. A dealer order carrying a stale
		is_cr_transfer=1 (migrated, or corrected by a path that didn't recompute)
		must still render as an ordinary row — the licence number decides."""
		row = self._rows(self._doc(is_cr_transfer=1), license_no=DEALER)[0]
		self.assertFalse(row["is_cr"])


VALID_DETAILS = {
	"license_number": CR,
	"license_expiry": "2028-07-31",
	"contact_name": "Jane Q. Collector",
	"premise_street": "123 Main St",
	"premise_street2": "Apt 4",
	"premise_city": "Austin",
	"premise_state": "TX",
	"premise_zip": "78701",
}


class CrDisposeSuccessPathTest(FrappeTestCase):
	"""The compliance trunk itself — previously never executed by any test.

	Both earlier C&R dispose cases are REFUSALS, so the block that maps the
	verified values into `ensure_cr_ffl_record`, writes `atf_ffl_record` back and
	commits had zero coverage. A single swapped keyword there (street vs
	street2, city vs state) writes a wrong address into the ATF FFL Record →
	into the FastBound contact → into the A&D bound book, with the whole suite
	green. So: assert the actual arguments, not merely that it was called.
	"""

	def _run(self, disposition_raises=False):
		sr = frappe._dict({"item_code": "GUN-1", "confirmed_serial": "SN-1",
			"woo_line_id": "7", "unit_index": 0, "stock_hold_serial": "",
			"reservation_error": ""})
		order = _order()
		order.serials = [sr]
		self.order = order

		real_get_doc = frappe.get_doc

		def get_doc(*args, **kwargs):
			if args and args[0] == "Woo Online Order":
				return order
			# The disposition is built from a dict literal, not a doctype name.
			if args and isinstance(args[0], dict):
				calls.append("disposition")
				if disposition_raises:
					raise frappe.ValidationError("stock error while booking the disposition")
				disp = MagicMock()
				disp.name = "DISP-1"
				return disp
			return real_get_doc(*args, **kwargs)

		calls = self.calls = []
		order.save.side_effect = lambda *a, **k: calls.append("save")
		with patch("frappe.only_for"), \
			 patch("ffl_core.compliance.cr_transfer.commit",
				side_effect=lambda: calls.append("commit")) as commit_mock, \
			 patch("frappe.get_doc", side_effect=get_doc), \
			 patch("ffl_woo_sync.woocommerce.fulfillment._locally_disposed_rows",
				return_value=[sr]), \
			 patch("ffl_core.compliance.cr_transfer.ensure_cr_ffl_record",
				return_value=CR) as ensure, \
			 patch("ffl_woo_sync.woocommerce.fulfillment._resolve_and_validate_ffl",
				return_value=CR), \
			 patch("ffl_woo_sync.woocommerce.fulfillment._delist_disposed"), \
			 patch("ffl_woo_sync.woocommerce.fulfillment._sold_prices", return_value={}), \
			 patch("ffl_woo_sync.woocommerce.fulfillment._serial_company", return_value="FFL Test"), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.create_sales_invoice_for_web_order",
				return_value={"sales_invoice": "SI-1", "status": "ok"}), \
			 patch("frappe.db.exists", return_value=False), \
			 patch("frappe.db.set_value"):
			self.ensure, self.commit_mock = ensure, commit_mock
			try:
				self.result = fulfillment.dispose_web_order(order.name, VALID_DETAILS)
				self.raised = None
			except Exception as exc:            # noqa: BLE001 - re-inspected below
				self.result, self.raised = None, exc
		return self

	def test_verified_values_reach_ensure_cr_ffl_record_unswapped(self):
		kw = self._run().ensure.call_args.kwargs
		self.assertEqual(kw["ffl_number"], CR)
		self.assertEqual(kw["licensee_name"], "Jane Q. Collector")
		self.assertEqual(kw["street"], "123 Main St")
		self.assertEqual(kw["street2"], "Apt 4")
		self.assertEqual(kw["city"], "Austin")
		self.assertEqual(kw["state"], "TX")
		self.assertEqual(kw["zip_code"], "78701")
		self.assertEqual(kw["phone"], "512-555-0100")

	def test_expiry_is_passed_as_the_parsed_date(self):
		from datetime import date
		self.assertEqual(self._run().ensure.call_args.kwargs["expiry_date"], date(2028, 7, 31))

	def test_the_resolved_record_is_written_back_onto_the_order(self):
		run = self._run()
		self.assertEqual(run.order.atf_ffl_record, CR)
		self.assertTrue(run.order.save.called)

	def test_a_disposed_cr_order_always_carries_an_atf_record(self):
		"""This is the unwritten premise behind "C&R structurally cannot trigger a
		4473": a non-empty atf_ffl_record flows to
		revenue.py's si.ffl_destination_ffl, and compliance/firearm.py returns
		early whenever that field is set. If a C&R order could ever dispose with
		this blank, the 4473 gate would engage on a transfer that has no 4473."""
		self.assertTrue(self._run().order.atf_ffl_record)

	def test_the_verification_is_recorded_as_a_comment(self):
		run = self._run()
		comments = [c for c in run.order.add_comment.call_args_list
			if "C&R verification recorded" in str(c)]
		self.assertTrue(comments, "no audit comment was written")

	def test_dispose_completes(self):
		self.assertTrue(self._run().result["ok"])


class CrVerificationSurvivesDisposeFailureTest(FrappeTestCase):
	"""The commit boundary's whole purpose.

	If the disposition fails after the verification was captured, the operator
	must not have to re-read and re-key the licence — and the audit trail should
	still record that the check happened. So the verification must be persisted
	and committed BEFORE the booking is attempted.
	"""

	def test_verification_is_written_to_the_order_before_booking(self):
		"""Renamed: this checks PERSISTENCE only. It used to be called
		"…persisted and committed…" while asserting nothing about the commit —
		and the commit is the half that actually survives the rollback. That
		half now has its own assertions in CommitBoundaryTest below."""
		run = CrDisposeSuccessPathTest._run(
			CrDisposeSuccessPathTest(), disposition_raises=True)
		self.assertIsNotNone(run.raised, "the disposition was expected to fail")
		self.assertEqual(run.order.cr_contact_name, "Jane Q. Collector")
		self.assertEqual(run.order.cr_premise_city, "Austin")
		self.assertTrue(run.order.cr_verified_by)
		self.assertTrue(run.order.save.called)
		self.assertTrue(run.ensure.called, "the ATF record must exist for the retry")


class CommitBoundaryTest(FrappeTestCase):
	"""The verification must be COMMITTED before the booking runs.

	This is the whole mechanism behind "a failed dispose never costs the operator
	the re-typing". Frappe rolls back to the last commit when the function raises,
	so without that one line the nine cr_* fields and the audit Comment are undone
	along with the failed disposition.

	It needs its own assertion because `ffl_core.utils.commit()` is a no-op under
	frappe.flags.in_test — a deliberate repo convention, but it means "committed"
	and "about to be rolled back" are INDISTINGUISHABLE to every other test in
	this file. Delete the commit() call and they all still pass. So: assert the
	call happens, and that it happens in the only order that helps.
	"""

	def test_the_verification_is_committed(self):
		run = CrDisposeSuccessPathTest()._run()
		self.assertTrue(run.commit_mock.called,
			"no commit boundary: a failed dispose would roll the verification back")

	def test_commit_happens_after_the_write_and_before_any_booking(self):
		"""Order is the point. Committing before save() commits nothing; committing
		after the disposition loop is too late to survive its failure."""
		run = CrDisposeSuccessPathTest()._run()
		self.assertIn("save", run.calls)
		self.assertIn("commit", run.calls)
		self.assertIn("disposition", run.calls)
		self.assertLess(run.calls.index("save"), run.calls.index("commit"))
		self.assertLess(run.calls.index("commit"), run.calls.index("disposition"))

	def test_a_failing_disposition_still_leaves_the_verification_committed(self):
		"""The booking blew up, and the commit had already happened."""
		run = CrDisposeSuccessPathTest()._run(disposition_raises=True)
		self.assertIsNotNone(run.raised)
		self.assertTrue(run.commit_mock.called)
		self.assertLess(run.calls.index("commit"), run.calls.index("disposition"))
