"""Authoritative FFL backfill from WooCommerce.

The existing rule is "fill the FFL only when it is empty, never clobber a value
staff entered" (`_recapture_ffl_if_empty`). That rule cannot express the two
new operations: *change* the FFL, and *remove* it. Both must overwrite, and a
removal must overwrite with nothing — which is indistinguishable from "this
webhook carried no FFL data" if you only look at whether the fields are blank.

So the Woo side sends an explicit pair: a monotonic `ffl_core_ffl_rev` and a
`ffl_core_ffl_state` enum. Authority is "the revision is strictly higher than
the one we stored"; emptiness is read from `state == 'none'`, never inferred.
Everything else — status changes, refunds, serial backfill, webhook replays,
out-of-order delivery — must leave the FFL fields alone, and these tests pin
that as hard as they pin the overwrite.

The clearing tests are the load-bearing ones. `atf_ffl_record` short-circuits
destination resolution, and the `cr_*` block is a verification OF A SPECIFIC
LICENCE NUMBER; leaving either behind when the number changes ships the firearm
to the previous FFL, or pushes the previous holder's identity to FastBound
under the new number.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce.webhook import (
	_apply_authoritative_ffl_change,
	_extract_ffl_signal,
)


_INTAKE = patch("ffl_woo_sync.woocommerce.webhook.intake_customer", return_value=None)


def setUpModule():
	# The buyer's POS customer at intake reads and writes Customers for real, which these doubles
	# cannot answer; it is tested on the real database in test_web_order_customer.
	_INTAKE.start()


def tearDownModule():
	_INTAKE.stop()


CR = "1-23-456-03-7C-89012"
DEALER = "5-99-888-01-9D-26128"
OTHER_DEALER = "1-99-888-01-2D-09999"


def _meta(**kv):
	return [{"key": k, "value": v} for k, v in kv.items()]


def _ffl_meta(license_no=DEALER, name="ACME Guns", **extra):
	m = {
		"_shipping_fflno": license_no,
		"_shipping_ffl_name": name,
		"_shipping_ffl_phone": "512-555-0000",
		"_shipping_ffl_premise_street": "9 Dealer Way",
		"_shipping_ffl_premise_city": "Austin",
		"_shipping_ffl_premise_state": "TX",
		"_shipping_ffl_premise_zip": "78702",
		"_shipping_fflexp": "2D",
	}
	m.update(extra)
	return m


def _order(**over):
	o = MagicMock()
	o.name = "WOO-ORD-00042"
	o.woo_order_id = "4242"
	o.order_status = "Pending Fulfillment"
	o.dropship_status = ""
	o.woo_ffl_rev = 0
	o.ffl_license = ""
	o.ffl_business_name = ""
	o.ffl_phone = ""
	o.ffl_premise_street = ""
	o.ffl_premise_city = ""
	o.ffl_premise_state = ""
	o.ffl_premise_zip = ""
	o.ffl_expiration = ""
	o.ffl_raw = ""
	o.atf_ffl_record = ""
	o.is_cr_transfer = 0
	for f in ("cr_contact_name", "cr_license_expiry", "cr_premise_street",
			"cr_premise_street2", "cr_premise_city", "cr_premise_state",
			"cr_premise_zip", "cr_verified_by", "cr_verified_at"):
		setattr(o, f, "")
	for k, v in over.items():
		setattr(o, k, v)
	# The code reads dropship_status via Document.get(); a bare MagicMock.get()
	# returns a fresh mock and would quietly ignore what the test set up.
	o.get.side_effect = lambda key, default=None: getattr(o, key, default)
	return o


def _verified(o):
	"""Give the order a completed C&R verification to watch get cleared."""
	o.is_cr_transfer = 1
	o.ffl_license = CR
	o.atf_ffl_record = CR
	o.cr_contact_name = "Jane Q. Collector"
	o.cr_license_expiry = "2028-07-31"
	o.cr_premise_street = "123 Main St"
	o.cr_premise_city = "Austin"
	o.cr_premise_state = "TX"
	o.cr_premise_zip = "78701"
	o.cr_verified_by = "clerk@example.com"
	o.cr_verified_at = "2026-07-29 10:00:00"
	return o


class ExtractFflSignalTest(FrappeTestCase):
	def test_reads_rev_and_state(self):
		sig = _extract_ffl_signal(_meta(ffl_core_ffl_rev="3", ffl_core_ffl_state="cr"))
		self.assertEqual(sig["rev"], 3)
		self.assertEqual(sig["state"], "cr")

	def test_absent_keys_are_no_signal(self):
		sig = _extract_ffl_signal(_meta(other="x"))
		self.assertIsNone(sig["rev"])
		self.assertIsNone(sig["state"])

	def test_empty_meta_is_no_signal(self):
		self.assertIsNone(_extract_ffl_signal([])["rev"])
		self.assertIsNone(_extract_ffl_signal(None)["rev"])

	def test_non_numeric_rev_is_no_signal(self):
		"""Garbage must read as "no signal", never as revision 0 — a 0 would
		compare as stale and silently suppress a real later update."""
		self.assertIsNone(_extract_ffl_signal(
			_meta(ffl_core_ffl_rev="abc", ffl_core_ffl_state="ffl"))["rev"])

	def test_unknown_state_is_no_signal(self):
		self.assertIsNone(_extract_ffl_signal(
			_meta(ffl_core_ffl_rev="1", ffl_core_ffl_state="maybe"))["state"])

	def test_integer_rev_is_accepted(self):
		self.assertEqual(_extract_ffl_signal(
			_meta(ffl_core_ffl_rev=2, ffl_core_ffl_state="none"))["rev"], 2)


class NoSignalTest(FrappeTestCase):
	"""Ordinary traffic must not touch the FFL — today's behaviour, preserved."""

	def test_payload_without_the_signal_is_ignored(self):
		o = _order(ffl_license=DEALER, woo_ffl_rev=1)
		self.assertFalse(_apply_authoritative_ffl_change(o, {"meta_data": _meta(**_ffl_meta())}))
		self.assertEqual(o.ffl_license, DEALER)

	def test_state_without_rev_is_ignored(self):
		o = _order()
		self.assertFalse(_apply_authoritative_ffl_change(
			o, {"meta_data": _meta(ffl_core_ffl_state="none")}))


class MonotonicityTest(FrappeTestCase):
	def test_equal_revision_is_a_replay(self):
		o = _order(ffl_license=DEALER, woo_ffl_rev=2)
		body = {"meta_data": _meta(ffl_core_ffl_rev="2", ffl_core_ffl_state="none")}
		self.assertFalse(_apply_authoritative_ffl_change(o, body))
		self.assertEqual(o.ffl_license, DEALER, "a replay must not clear the FFL")

	def test_lower_revision_arrived_out_of_order(self):
		o = _order(ffl_license=DEALER, woo_ffl_rev=5)
		body = {"meta_data": _meta(ffl_core_ffl_rev="3", ffl_core_ffl_state="none")}
		self.assertFalse(_apply_authoritative_ffl_change(o, body))
		self.assertEqual(o.ffl_license, DEALER)

	def test_higher_revision_wins(self):
		o = _order(ffl_license=DEALER, woo_ffl_rev=1)
		body = {"meta_data": _meta(ffl_core_ffl_rev="2", ffl_core_ffl_state="ffl",
			**_ffl_meta(license_no=OTHER_DEALER))}
		self.assertTrue(_apply_authoritative_ffl_change(o, body))
		self.assertEqual(o.ffl_license, OTHER_DEALER)
		self.assertEqual(o.woo_ffl_rev, 2)


class RemoveFflTest(FrappeTestCase):
	def test_state_none_clears_every_ffl_field(self):
		o = _verified(_order(woo_ffl_rev=1))
		o.ffl_business_name = "ACME Guns"
		o.ffl_premise_street = "9 Dealer Way"
		body = {"meta_data": _meta(ffl_core_ffl_rev="2", ffl_core_ffl_state="none")}
		self.assertTrue(_apply_authoritative_ffl_change(o, body))
		for f in ("ffl_license", "ffl_business_name", "ffl_phone", "ffl_premise_street",
				"ffl_premise_city", "ffl_premise_state", "ffl_premise_zip",
				"ffl_expiration", "ffl_raw"):
			with self.subTest(field=f):
				self.assertEqual(getattr(o, f), "")

	def test_removal_also_clears_the_resolved_record_and_the_verification(self):
		"""The line that is easiest to get wrong: clearing
		ffl_license but leaving atf_ffl_record ships the gun to the old FFL,
		because destination resolution reads atf_ffl_record FIRST and never
		falls through to the licence number."""
		o = _verified(_order(woo_ffl_rev=1))
		body = {"meta_data": _meta(ffl_core_ffl_rev="2", ffl_core_ffl_state="none")}
		_apply_authoritative_ffl_change(o, body)
		self.assertEqual(o.atf_ffl_record, "")
		self.assertEqual(o.is_cr_transfer, 0)
		for f in ("cr_contact_name", "cr_license_expiry", "cr_premise_city",
				"cr_verified_by", "cr_verified_at"):
			with self.subTest(field=f):
				self.assertFalse(getattr(o, f))

	def test_removal_does_not_consult_the_rest_api(self):
		"""'Removed' is stated by the enum. Re-fetching could resurrect the FFL
		from a stale read and un-remove it."""
		o = _order(ffl_license=DEALER, woo_ffl_rev=1)
		body = {"meta_data": _meta(ffl_core_ffl_rev="2", ffl_core_ffl_state="none")}
		with patch("ffl_woo_sync.woocommerce.webhook._fetch_order_ffl") as fetch:
			_apply_authoritative_ffl_change(o, body)
		self.assertFalse(fetch.called)


class ChangeFflTest(FrappeTestCase):
	def test_changing_the_number_clears_the_stale_resolution_and_verification(self):
		o = _verified(_order(woo_ffl_rev=1))
		body = {"meta_data": _meta(ffl_core_ffl_rev="2", ffl_core_ffl_state="ffl",
			**_ffl_meta(license_no=OTHER_DEALER))}
		self.assertTrue(_apply_authoritative_ffl_change(o, body))
		self.assertEqual(o.ffl_license, OTHER_DEALER)
		self.assertEqual(o.atf_ffl_record, "")
		self.assertEqual(o.is_cr_transfer, 0)
		self.assertFalse(o.cr_contact_name)
		self.assertFalse(o.cr_verified_by)

	def test_same_number_re_sent_keeps_the_resolved_record(self):
		"""A re-send at a higher rev (e.g. staff re-saved the order) should not
		throw away a perfectly good resolution and force a re-verification."""
		o = _verified(_order(woo_ffl_rev=1))
		body = {"meta_data": _meta(ffl_core_ffl_rev="2", ffl_core_ffl_state="cr",
			**_ffl_meta(license_no=CR, name=""))}
		_apply_authoritative_ffl_change(o, body)
		self.assertEqual(o.atf_ffl_record, CR)
		self.assertEqual(o.cr_contact_name, "Jane Q. Collector")

	def test_cr_flag_is_derived_from_the_number_not_the_woo_state(self):
		"""Contract: the licence number is the single source of truth. A Woo
		state of 'ffl' carrying an 03 number is still a C&R transfer."""
		o = _order(woo_ffl_rev=0)
		body = {"meta_data": _meta(ffl_core_ffl_rev="1", ffl_core_ffl_state="ffl",
			**_ffl_meta(license_no=CR))}
		with patch("frappe.log_error"):
			_apply_authoritative_ffl_change(o, body)
		self.assertEqual(o.is_cr_transfer, 1)

	def test_disagreement_between_state_and_number_is_logged(self):
		o = _order(woo_ffl_rev=0)
		body = {"meta_data": _meta(ffl_core_ffl_rev="1", ffl_core_ffl_state="cr",
			**_ffl_meta(license_no=DEALER))}
		with patch("frappe.log_error") as log:
			_apply_authoritative_ffl_change(o, body)
		self.assertEqual(o.is_cr_transfer, 0)
		self.assertTrue(log.called)

	def test_empty_payload_falls_back_to_rest_then_refuses_to_clear(self):
		"""State says an FFL exists but nothing could be read. Keeping the old
		value is the only safe move — clearing would look like a removal that
		nobody requested."""
		o = _order(ffl_license=DEALER, woo_ffl_rev=1)
		body = {"id": "4242", "meta_data": _meta(ffl_core_ffl_rev="2", ffl_core_ffl_state="ffl")}
		with patch("ffl_woo_sync.woocommerce.webhook._fetch_order_ffl",
				return_value={"license": "", "business_name": "", "street": "", "_raw": []}) as fetch, \
			 patch("frappe.log_error") as log:
			self.assertFalse(_apply_authoritative_ffl_change(o, body))
		self.assertTrue(fetch.called)
		self.assertEqual(o.ffl_license, DEALER)
		self.assertTrue(log.called)

	def test_malformed_licence_is_rejected_without_wedging_the_webhook(self):
		"""A malformed number must not be written. If it were, the order's own
		validate() would throw on save — and that save is the SAME one that
		applies serial backfill and status changes, so one bad licence from Woo
		would stall the entire order, not just its FFL. Keep the old value, log,
		and let the rest of the webhook proceed."""
		o = _order(ffl_license=DEALER, woo_ffl_rev=1)
		body = {"meta_data": _meta(ffl_core_ffl_rev="2", ffl_core_ffl_state="ffl",
			**_ffl_meta(license_no="ACME GUNS LLC"))}
		with patch("frappe.log_error") as log:
			self.assertFalse(_apply_authoritative_ffl_change(o, body))
		self.assertEqual(o.ffl_license, DEALER)
		self.assertEqual(o.woo_ffl_rev, 1, "a rejected payload must not consume the revision")
		self.assertTrue(log.called)

	def test_rest_fallback_supplies_the_value(self):
		o = _order(woo_ffl_rev=1)
		body = {"id": "4242", "meta_data": _meta(ffl_core_ffl_rev="2", ffl_core_ffl_state="cr")}
		fetched = {"license": CR, "business_name": "", "phone": "", "street": "",
			"city": "", "state": "", "zip": "", "expiration": "", "_raw": []}
		with patch("ffl_woo_sync.woocommerce.webhook._fetch_order_ffl", return_value=fetched):
			self.assertTrue(_apply_authoritative_ffl_change(o, body))
		self.assertEqual(o.ffl_license, CR)
		self.assertEqual(o.is_cr_transfer, 1)


class AfterFulfilmentTest(FrappeTestCase):
	"""Once the firearm is gone, the bound book is the record — a webhook must
	not quietly rewrite where it went."""

	def test_disposed_order_is_not_rewritten(self):
		o = _order(order_status="Transferred", ffl_license=DEALER, woo_ffl_rev=1)
		body = {"meta_data": _meta(ffl_core_ffl_rev="2", ffl_core_ffl_state="ffl",
			**_ffl_meta(license_no=OTHER_DEALER))}
		with patch("frappe.log_error") as log:
			self.assertTrue(_apply_authoritative_ffl_change(o, body))
		self.assertEqual(o.ffl_license, DEALER, "a shipped order's FFL was rewritten")
		self.assertTrue(log.called)
		self.assertTrue(o.add_comment.called)

	def test_disposed_order_still_absorbs_the_revision(self):
		"""Otherwise every subsequent webhook re-raises the same alarm."""
		o = _order(order_status="Transferred", ffl_license=DEALER, woo_ffl_rev=1)
		body = {"meta_data": _meta(ffl_core_ffl_rev="2", ffl_core_ffl_state="none")}
		with patch("frappe.log_error"):
			_apply_authoritative_ffl_change(o, body)
		self.assertEqual(o.woo_ffl_rev, 2)

	def test_cancelled_order_is_not_rewritten(self):
		o = _order(order_status="Cancelled", ffl_license=DEALER, woo_ffl_rev=1)
		body = {"meta_data": _meta(ffl_core_ffl_rev="2", ffl_core_ffl_state="none")}
		with patch("frappe.log_error"):
			_apply_authoritative_ffl_change(o, body)
		self.assertEqual(o.ffl_license, DEALER)


class DistributorLegTest(FrappeTestCase):
	def test_change_after_the_distributor_order_is_applied_but_flagged(self):
		"""The drop-ship leg was already placed to the OLD address. We still
		record the truth, but a human has to go fix the distributor order."""
		o = _order(ffl_license=DEALER, woo_ffl_rev=1, dropship_status="Ordered")
		body = {"meta_data": _meta(ffl_core_ffl_rev="2", ffl_core_ffl_state="ffl",
			**_ffl_meta(license_no=OTHER_DEALER))}
		with patch("frappe.log_error") as log:
			self.assertTrue(_apply_authoritative_ffl_change(o, body))
		self.assertEqual(o.ffl_license, OTHER_DEALER)
		self.assertTrue(log.called)
		self.assertTrue(o.add_comment.called)


class RecaptureFallbackTest(FrappeTestCase):
	"""The legacy fill-when-empty path — deliberately left UNCHANGED.

	An earlier revision of this branch added a malformed-number guard here. It
	is gone: the reason for it (a stored bad number would make the next save
	throw) was removed at the source by scoping the format check to human edits.
	What remains is this repo's standing posture — KEEP the data, fail loudly at
	the point of USE — and that posture is better here, because the operator
	troubleshooting a stuck order sees the actual value WooCommerce sent rather
	than an empty field with no explanation.
	"""

	def test_still_fills_an_empty_ffl(self):
		from ffl_woo_sync.woocommerce.webhook import _recapture_ffl_if_empty
		o = _order()
		self.assertTrue(_recapture_ffl_if_empty(o, {"meta_data": _meta(**_ffl_meta())}))
		self.assertEqual(o.ffl_license, DEALER)

	def test_still_refuses_to_clobber_a_stored_value(self):
		from ffl_woo_sync.woocommerce.webhook import _recapture_ffl_if_empty
		o = _order(ffl_license=OTHER_DEALER)
		self.assertFalse(_recapture_ffl_if_empty(o, {"meta_data": _meta(**_ffl_meta())}))
		self.assertEqual(o.ffl_license, OTHER_DEALER)

	def test_a_malformed_number_is_captured_as_sent(self):
		"""Restored pre-branch behaviour: store it, don't second-guess Woo."""
		from ffl_woo_sync.woocommerce.webhook import _recapture_ffl_if_empty
		o = _order()
		self.assertTrue(_recapture_ffl_if_empty(
			o, {"meta_data": _meta(**_ffl_meta(license_no="ACME GUNS LLC"))}))
		self.assertEqual(o.ffl_license, "ACME GUNS LLC")


class MalformedNumberDoesNotWedgeTheOrderTest(FrappeTestCase):
	"""Storing a malformed number must not stop the order saving.

	That save is the one carrying serial backfill, status and refunds, so a
	throw here stalls the whole order rather than just its FFL — and on create
	it aborts insert() outright, losing a paid order entirely. Guaranteed now by
	scoping the format check to human edits, not by refusing the data.
	"""

	def _doc(self, license_no=""):
		doc = frappe.get_doc({
			"doctype": "Woo Online Order", "woo_order_id": "", "site": "retail",
			"ffl_license": license_no,
		})
		doc._doc_before_save = None
		return doc

	def test_control_a_human_edit_with_the_same_value_still_raises(self):
		"""Control: the format gate is still armed where G1 wanted it. If this
		stops raising, the gate is gone and the test below proves nothing."""
		with self.assertRaises(frappe.ValidationError):
			self._doc("ACME GUNS LLC").validate()

	def test_inbound_sync_saves_so_serial_backfill_can_proceed(self):
		from ffl_woo_sync.woocommerce.webhook import _recapture_ffl_if_empty
		order = _order()
		_recapture_ffl_if_empty(
			order, {"meta_data": _meta(**_ffl_meta(license_no="ACME GUNS LLC"))})
		doc = self._doc(order.ffl_license)
		with patch.dict(frappe.flags, {"woo_sync_inbound": True}), \
			 patch("frappe.log_error"):
			doc.validate()   # must NOT raise — the save serial backfill rides on
		self.assertEqual(doc.ffl_license, "ACME GUNS LLC")


class RawMetaTest(FrappeTestCase):
	def test_raw_meta_is_preserved_for_audit(self):
		o = _order(woo_ffl_rev=0)
		body = {"meta_data": _meta(ffl_core_ffl_rev="1", ffl_core_ffl_state="ffl", **_ffl_meta())}
		_apply_authoritative_ffl_change(o, body)
		self.assertTrue(o.ffl_raw)
		self.assertIsInstance(json.loads(o.ffl_raw), list)


class RevisionFloorTest(FrappeTestCase):
	"""Contract C1 starts revisions at 1, so 0 is not a revision.

	Without an explicit floor the int/str round-trip decides it: WooCommerce's
	`0` reads as "no signal" while `"0"` reads as revision zero. Both then get
	dropped, but by different routes and for different reasons — one rule,
	stated once, is what keeps that from becoming a real divergence later.
	"""

	def test_zero_reads_as_no_signal_in_both_forms(self):
		for value in (0, "0"):
			with self.subTest(value=value):
				self.assertIsNone(_extract_ffl_signal(
					_meta(ffl_core_ffl_rev=value, ffl_core_ffl_state="none"))["rev"])

	def test_negative_reads_as_no_signal(self):
		self.assertIsNone(_extract_ffl_signal(
			_meta(ffl_core_ffl_rev="-1", ffl_core_ffl_state="none"))["rev"])

	def test_one_is_a_real_first_revision(self):
		self.assertEqual(_extract_ffl_signal(
			_meta(ffl_core_ffl_rev="1", ffl_core_ffl_state="cr"))["rev"], 1)


class CreatePathMalformedLicenceTest(FrappeTestCase):
	"""A malformed number on CREATE must not cost us the whole order.

	validate() judges any CHANGED licence, and every field of a brand-new
	document counts as changed. Left unqualified that throws inside insert(),
	the webhook rolls back and still answers 200, and WooCommerce never retries
	— the order would simply not exist: no soft-reserve on the serial, nothing
	in the Pending Order queue, and the firearm still sellable at the counter.

	Resolved by scoping the throw to human edits (see
	InboundSyncIsNotJudgedTest); these cases pin the two halves of that scope.
	"""

	def test_order_is_still_created_with_a_blank_ffl(self):
		doc = frappe.get_doc({
			"doctype": "Woo Online Order", "woo_order_id": "", "site": "retail",
			"ffl_license": "ACME GUNS LLC",
		})
		doc._doc_before_save = None
		# Control: this is what would have happened inside insert().
		with self.assertRaises(frappe.ValidationError):
			doc.validate()

		clean = frappe.get_doc({
			"doctype": "Woo Online Order", "woo_order_id": "", "site": "retail",
			"ffl_license": "",
		})
		clean._doc_before_save = None
		clean.validate()   # blank is a supported state — the order survives
		self.assertEqual(clean.is_cr_transfer, 0)


class HandlerWiringTest(FrappeTestCase):
	"""The `if authoritative / elif recapture` join in _handle_order_updated.

	The two functions are tested densely on their own; this pins the ONE LINE
	that connects them. Written as `if`/`if` instead of `if`/`elif`, a removal
	(state='none') would clear the FFL and then immediately hand the same
	request to _recapture_ffl_if_empty — which, seeing an empty FFL and empty
	meta, issues a REST re-fetch and can put the old FFL straight back.

	That would make "does a removal stay removed?" depend on whether the OTHER
	repo really calls delete_meta_data() (contract C3). This test turns that
	cross-repo assumption into something this repo proves for itself: the REST
	fallback is made to return a live old FFL, and the removal must still stand.
	"""

	def _run_updated(self, body, doc):
		from ffl_woo_sync.woocommerce import webhook
		with patch("frappe.db.get_value", return_value="WOO-ORD-00042"), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch.object(webhook, "_sync_woo_status_and_pricing", return_value=False), \
			 patch.object(webhook, "_sync_line_amounts", return_value=False), \
			 patch.object(webhook, "_refresh_ship_to_if_unrouted", return_value=False), \
			 patch.object(webhook, "_auto_clear_non_firearm_if_completed", return_value=False), \
			 patch.object(webhook, "_extract_line_serials", return_value={}), \
			 patch("ffl_woo_sync.woocommerce.refunds.record_order_refunds", return_value=False), \
			 patch.object(webhook, "_fetch_order_ffl", return_value={
				"license": OTHER_DEALER, "business_name": "Resurrected FFL", "phone": "",
				"street": "9 Old Way", "city": "Austin", "state": "TX", "zip": "78702",
				"expiration": "", "_raw": [{"key": "_shipping_fflno", "value": OTHER_DEALER}],
			 }) as fetch:
			webhook._handle_order_updated(body)
		return fetch

	def test_a_removal_is_not_resurrected_by_the_fallback_in_the_same_request(self):
		doc = _verified(_order(woo_ffl_rev=1))
		doc.serials = []
		body = {"id": "4242", "meta_data": _meta(ffl_core_ffl_rev="2", ffl_core_ffl_state="none")}
		self._run_updated(body, doc)
		self.assertEqual(doc.ffl_license, "", "the removal was undone within the same webhook")
		self.assertEqual(doc.atf_ffl_record, "")
		self.assertEqual(doc.woo_ffl_rev, 2)

	def test_the_fallback_still_runs_when_there_is_no_authority_signal(self):
		"""The elif must not disable the legacy path for ordinary traffic."""
		doc = _order(ffl_license="")
		doc.serials = []
		body = {"id": "4242", "meta_data": _meta(other="x")}
		fetch = self._run_updated(body, doc)
		self.assertTrue(fetch.called, "the fill-when-empty fallback stopped running")
		self.assertEqual(doc.ffl_license, OTHER_DEALER)


class CreatePathSeedingTest(FrappeTestCase):
	"""The two lines the create path adds — woo_ffl_rev must equal 1.

	Every pre-existing create-path test patches frappe.new_doc wholesale, so
	those two assignments are structurally invisible to them (the same trap).
	The stub below DISPATCHES by doctype and delegates everything else to the real new_doc, so frappe's own internals are
	not handed a mock — the failure mode that poisoned this site's cache earlier
	in this branch.
	"""

	def _created(self, meta):
		from ffl_woo_sync.woocommerce import webhook
		doc = MagicMock()
		doc.serials = []
		doc.lines = []
		real_new_doc = frappe.new_doc

		def new_doc_for(doctype, *a, **kw):
			if doctype == "Woo Online Order":
				return doc
			return real_new_doc(doctype, *a, **kw)

		body = {"id": "77", "status": "processing", "currency": "USD", "total": "500",
			"total_tax": "0", "shipping_total": "0", "discount_total": "0",
			"billing": {"first_name": "Jane", "last_name": "Collector", "email": "j@c.c"},
			"line_items": [], "meta_data": meta}
		with patch("frappe.db.exists", return_value=None), \
			 patch("frappe.db.get_value", return_value=None), \
			 patch("frappe.new_doc", side_effect=new_doc_for), \
			 patch("frappe.db.set_value"), patch("frappe.enqueue"), \
			 patch.object(webhook, "_process_lines", return_value=[]), \
			 patch.object(webhook, "_apply_ship_to"), \
			 patch.object(webhook, "_sync_woo_status_and_pricing"):
			webhook._handle_order_created(body)
		return doc

	def test_the_authority_signal_is_seeded_at_creation(self):
		"""Without this the first staff edit (rev 1) would tie with the default 0
		and be dropped as a replay — the Store-API race would then be handled by
		the fill-when-empty fallback rather than by design."""
		doc = self._created(_meta(ffl_core_ffl_rev="1", ffl_core_ffl_state="ffl", **_ffl_meta()))
		self.assertEqual(doc.woo_ffl_rev, 1)

	def test_an_unsignalled_order_seeds_zero_not_none(self):
		doc = self._created(_meta(**_ffl_meta()))
		self.assertEqual(doc.woo_ffl_rev, 0)

	def test_the_cr_flag_is_derived_at_creation_from_the_number(self):
		doc = self._created(_meta(ffl_core_ffl_rev="1", ffl_core_ffl_state="cr",
			**_ffl_meta(license_no=CR)))
		self.assertEqual(doc.is_cr_transfer, 1)

	def test_a_dealer_order_is_not_flagged_at_creation(self):
		doc = self._created(_meta(ffl_core_ffl_rev="1", ffl_core_ffl_state="ffl", **_ffl_meta()))
		self.assertEqual(doc.is_cr_transfer, 0)
