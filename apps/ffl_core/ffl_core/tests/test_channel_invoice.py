"""One predicate for "this invoice mirrors a sale made on another channel".

Eight rules in this codebase assume an invoice is a sale being rung up at the
counter. Eight is the number that matters: before this module the rule was
spelled ``woo_online_order`` in each of them, so adding a second sales channel
meant finding all eight by reading, and each one missed is its own quiet
failure — the marketplace's tax charged twice, a second receipt emailed to a
buyer who already has one, or Cancel Order offered on a sale that cannot be
unwound.

Five of them live in ffl_core and are the subject here. The other three are the
Sales Report's channel split, FastBound's error wording and the Item form's
Pending badge.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.compliance import channel_invoice


class _Doc(dict):
	"""A stand-in with attribute access — the predicate reads with getattr, so a
	plain row must work exactly like a Document."""

	def __init__(self, **fields):
		super().__init__()
		self.update({"doctype": "Sales Invoice"})
		self.update(fields)

	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError:
			raise AttributeError(key)

	def get(self, key, default=None):
		return dict.get(self, key, default)


class TestThePredicate(FrappeTestCase):
	def test_a_web_order_invoice_is_mirrored(self):
		self.assertTrue(channel_invoice.is_channel_mirrored_invoice(
			_Doc(woo_online_order="WOO-ORD-1")))

	def test_a_gunbroker_invoice_is_mirrored(self):
		self.assertTrue(channel_invoice.is_channel_mirrored_invoice(
			_Doc(gunbroker_order="GB-ORD-1")))

	def test_a_counter_invoice_is_not(self):
		self.assertFalse(channel_invoice.is_channel_mirrored_invoice(
			_Doc(woo_online_order=None, gunbroker_order=None)))

	def test_a_document_without_the_fields_at_all_answers_no(self):
		# ffl_core has to run with neither integration app installed, and this
		# sits on the hot path of every invoice submit.
		self.assertFalse(channel_invoice.is_channel_mirrored_invoice(_Doc()))

	def test_an_empty_string_is_not_a_channel(self):
		self.assertFalse(channel_invoice.is_channel_mirrored_invoice(
			_Doc(gunbroker_order="")))


class TestQueueExclusions(FrappeTestCase):
	def test_only_columns_that_exist_are_filtered_on(self):
		# Not defensive habit: both call sites build raw get_all filters, and a
		# filter naming a column that has not been migrated yet RAISES — taking
		# down the Pending Order page and the counter-hold registry with it.
		with patch("frappe.db.has_column",
				side_effect=lambda _dt, field: field == "woo_online_order"):
			out = channel_invoice.pending_queue_exclusions()
		self.assertEqual(out, {"woo_online_order": ("is", "not set")})

	def test_every_channel_is_excluded_once_its_column_is_there(self):
		# Channels plus whatever extensions declared (gunshop_pending_queue_exclusion_fields).
		with patch("frappe.db.has_column", return_value=True):
			out = channel_invoice.pending_queue_exclusions()
		self.assertEqual(set(out), set(channel_invoice.CHANNEL_INVOICE_FIELDS) | set(channel_invoice.extension_exclusion_fields()))
		self.assertTrue(all(v == ("is", "not set") for v in out.values()))
		self.assertIn("gunbroker_order", out)

	def test_the_shared_base_filter_adds_the_exclusions_to_the_counter_rule(self):
		# One filter for the Pending Order queue and the counter-hold registry:
		# submitted, not consolidated, not a return, plus the column-guarded
		# channel exclusions — no hard-coded woo key on a site without ffl_woo_sync.
		with patch("frappe.db.has_column", return_value=False):
			out = channel_invoice.pending_si_base_filter()
		self.assertEqual(out, {"docstatus": 1, "is_consolidated": 0, "is_return": 0})
		with patch("frappe.db.has_column", return_value=True):
			out = channel_invoice.pending_si_base_filter()
			expected = channel_invoice.pending_queue_exclusions()
		self.assertIn("woo_online_order", expected)
		self.assertEqual(out, {"docstatus": 1, "is_consolidated": 0, "is_return": 0,
			**expected})


class TestTheQueueCallSites(FrappeTestCase):
	"""A shipped GunBroker gun must not read as still on an open counter order.

	Its disposition links the GunBroker Order, not the invoice, so the "has this
	gun been disposed?" test can never see it — and without the exclusion every
	one of them would be flagged as held for ever and could never be sold again.
	"""

	def test_the_counter_hold_registry_excludes_channel_invoices(self):
		from ffl_core import reservations

		seen = []

		def _get_all(doctype, **kw):
			seen.append((doctype, kw.get("filters") or {}))
			# One invoice carries this gun, so the registry goes on to ask which
			# of those invoices count — which is the query under test.
			return ["ACC-SINV-1"] if doctype == "Sales Invoice Item" else []

		with patch("frappe.db.has_column", return_value=True), \
				patch("frappe.get_all", side_effect=_get_all), \
				patch("frappe.db.sql", return_value=[]):
			reservations.counter_holds("COLT-PYTHON")
		invoice_queries = [f for dt, f in seen
			if dt == "Sales Invoice" and "docstatus" in f]
		self.assertTrue(invoice_queries, "no Sales Invoice query was made")
		for filters in invoice_queries:
			self.assertEqual(filters.get("gunbroker_order"), ("is", "not set"))
			self.assertEqual(filters.get("woo_online_order"), ("is", "not set"))

	def test_the_pending_order_queue_excludes_channel_invoices(self):
		# The other half of the invariant, and the visible one: without it every
		# shipped GunBroker gun sits on the counter's Pending Order page for
		# ever, offering Dispose (already done, on the GunBroker Order) and
		# Cancel Order (reverses the GL of a sale GunBroker will not let us
		# unwind). Every arm of the query, because it is built from `base` four
		# times and dropping the merge from one of them is the same bug.
		from ffl_core.api import manual_order

		seen = []

		def _get_all(doctype, **kw):
			seen.append((doctype, kw.get("filters") or {}))
			return []

		with patch("frappe.db.has_column", return_value=True), \
				patch("frappe.get_all", side_effect=_get_all), \
				patch.object(manual_order, "_shipstation_enabled", return_value=False):
			manual_order.list_pending_dispositions()
		invoice_queries = [f for dt, f in seen if dt == "Sales Invoice"]
		self.assertEqual(len(invoice_queries), 4,
			"the queue no longer builds its four arms from one base")
		for filters in invoice_queries:
			self.assertEqual(filters.get("gunbroker_order"), ("is", "not set"))
			self.assertEqual(filters.get("woo_online_order"), ("is", "not set"))


class TestTheFiveReadPoints(FrappeTestCase):
	"""Each of these is a rule that must step aside for a mirrored invoice."""

	def test_the_interstate_tax_engine_leaves_a_gunbroker_invoice_alone(self):
		# There is no shipping Address here to judge in or out of state, and the
		# destination call was made at the marketplace's checkout.
		from ffl_core.compliance import interstate_tax

		doc = _Doc(gunbroker_order="GB-ORD-1", is_return=0, is_consolidated=0,
			company="TSA")
		with patch.object(interstate_tax, "_oos_template") as oos:
			interstate_tax.apply_interstate_tax(doc)
		oos.assert_not_called()

	def test_the_exemption_engine_leaves_a_gunbroker_invoice_alone(self):
		from ffl_core.compliance import customer_tax_exempt

		doc = _Doc(gunbroker_order="GB-ORD-1", is_return=0, is_consolidated=0,
			customer="CUST-1")
		with patch("frappe.db.get_value") as get_value:
			customer_tax_exempt.enforce_customer_tax_exemption(doc)
		get_value.assert_not_called()

	def test_no_freight_tax_row_is_added_to_a_gunbroker_invoice(self):
		# The marketplace taxed the freight itself; adding our own row would
		# charge the buyer's tax twice on the same invoice.
		from ffl_core.compliance import shipping_charge

		doc = _Doc(gunbroker_order="GB-ORD-1", company="TSA",
			ffl_shipping_amount=0, taxes=[])
		with patch.object(shipping_charge, "_freight_tax_rows") as freight, \
				patch.object(shipping_charge, "_mirrorable_tax_rows") as mirrorable, \
				patch.object(shipping_charge, "shipping_account_name",
					return_value="Shipping Income - TSA"), \
				patch.object(shipping_charge, "_company_of", create=True,
					return_value="TSA"):
			try:
				shipping_charge.apply_shipping_charge(doc)
			except Exception:
				# The zero-amount branch runs on past the guard and touches doc
				# machinery a plain dict does not have; the guard itself is what
				# this test is about, and it runs first.
				pass
		freight.assert_not_called()
		mirrorable.assert_not_called()

	def test_no_pos_receipt_is_emailed_for_a_gunbroker_sale(self):
		# Both channels book the REAL buyer, with their email, onto the invoice
		# — so without this guard every marketplace sale sends a second,
		# POS-branded receipt to somebody who already has one.
		from ffl_core import receipts

		doc = _Doc(gunbroker_order="GB-ORD-1", is_consolidated=0)
		with patch("frappe.enqueue") as enqueue, \
				patch.object(receipts, "_customer_email", return_value="x@y.z"):
			receipts.send_sale_receipt(doc)
		enqueue.assert_not_called()

	def test_a_gunbroker_invoice_is_not_a_counter_order(self):
		# _is_counter_order is what Edit and Cancel Order gate on. Both reverse
		# GL entries without touching the bound book, which for a gun that has
		# already shipped under a booked disposition is a hole in a federal
		# record.
		from ffl_core.api import manual_order

		doc = _Doc(gunbroker_order="GB-ORD-1", consignment_out=None,
			ffl_destination_ffl="FFL-1")
		self.assertFalse(manual_order._is_counter_order(doc))

	def test_a_counter_invoice_is_still_a_counter_order(self):
		from ffl_core.api import manual_order

		doc = _Doc(woo_online_order=None, gunbroker_order=None,
			consignment_out=None, ffl_destination_ffl="FFL-1")
		self.assertTrue(manual_order._is_counter_order(doc))
