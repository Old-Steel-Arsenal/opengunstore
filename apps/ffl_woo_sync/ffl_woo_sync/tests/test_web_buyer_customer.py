"""Tests for per-buyer web customer resolution.

New web orders book their Sales Invoice to the REAL buyer — deduped by billing
email, get-or-create — instead of the shared "Web Store Customer" aggregate. The
report is unchanged (SI.customer shows the real name natively). Email-less orders
(and the existing 45) fall back to the aggregate. New buyers mirror the counter
retail defaults: Individual + neutral group (NEVER the aggregate's mis-tagged
"Non Profit") + non-exempt.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import revenue


def _order(name="WOO-ORD-00007", customer_name="Alex Example",
		customer_email="alex@example.com", customer_phone="555-1212"):
	o = MagicMock()
	o.name = name
	o.customer_name = customer_name
	o.customer_email = customer_email
	o.customer_phone = customer_phone
	o.customer = None   # no customer stored at intake
	return o


def setUpModule():
	# The email lookup also reads the Customer Alias doctype. These tests stub frappe.db.get_value and frappe.get_doc
	# for the whole test, and a Meta built under those stubs is not the doctype. Alone in a fresh process nothing
	# has built it yet, so build it first (docs/testing.md).
	from ffl_core.tests.channel_query_doubles import warm_channel_meta

	warm_channel_meta("Customer Alias")
	# The resolved customer is written back to the order: its doctype and column, read the same way.
	warm_channel_meta("Woo Online Order")
	frappe.db.has_column("Woo Online Order", "customer")


class _CaptureCustomer:
	"""Capture the Customer dict passed to frappe.get_doc + return an insertable stub."""

	def __init__(self, new_name="CUST-NEW"):
		self.spec = None
		self._name = new_name

	def __call__(self, spec):
		self.spec = dict(spec)
		stub = MagicMock()
		stub.name = self._name
		stub.flags = frappe._dict()
		return stub


class TestResolveWebCustomer(FrappeTestCase):

	def test_creates_real_customer_from_billing_email(self):
		order = _order(customer_name="Alex Example", customer_email="alex@example.com",
			customer_phone="555")
		cap = _CaptureCustomer()
		with patch("frappe.db.get_value", return_value=None), \
			 patch("ffl_core.api.manual_order._individual_group", return_value="Individual"), \
			 patch("frappe.db.get_single_value", return_value=None), \
			 patch("frappe.get_doc", side_effect=cap):
			name = revenue._resolve_web_customer(order)
		self.assertEqual(name, "CUST-NEW")
		self.assertEqual(cap.spec["doctype"], "Customer")
		self.assertEqual(cap.spec["customer_name"], "Alex Example")
		self.assertEqual(cap.spec["email_id"], "alex@example.com")   # dedup key stored
		self.assertEqual(cap.spec["customer_type"], "Individual")
		self.assertEqual(cap.spec["customer_group"], "Individual")    # NOT "Non Profit"
		self.assertEqual(cap.spec["is_tax_exempt"], 0)                # non-exempt retail
		self.assertEqual(cap.spec["is_ffl_dealer"], 0)

	def test_repeat_buyer_email_reuses_existing_customer(self):
		# Second order from the same email → the existing Customer, no duplicate.
		order = _order(customer_email="alex@example.com")
		hit = frappe._dict(name="CUST-ALEX", is_ffl_dealer=0)   # find_customer_by_email's row
		with patch("frappe.db.get_value", return_value=hit) as gv, \
			 patch("frappe.get_doc") as get_doc:
			name = revenue._resolve_web_customer(order)
		self.assertEqual(name, "CUST-ALEX")
		get_doc.assert_not_called()                                  # no second customer
		self.assertEqual(gv.call_args.args[0], "Customer")
		self.assertEqual(gv.call_args.args[1], {"email_id": "alex@example.com"})  # by email

	def test_email_less_order_falls_back_to_aggregate_and_logs(self):
		order = _order(customer_email="")
		with patch.object(revenue, "_ensure_web_customer", return_value="Web Store Customer") as agg, \
			 patch("frappe.log_error") as log, \
			 patch("frappe.get_doc") as get_doc:
			name = revenue._resolve_web_customer(order)
		self.assertEqual(name, "Web Store Customer")
		agg.assert_called_once()                                     # aggregate fallback
		log.assert_called_once()                                     # never silent
		get_doc.assert_not_called()                                  # no real customer created

	def test_whitespace_only_email_also_falls_back(self):
		order = _order(customer_email="   ")
		with patch.object(revenue, "_ensure_web_customer", return_value="Web Store Customer") as agg, \
			 patch("frappe.log_error"):
			name = revenue._resolve_web_customer(order)
		self.assertEqual(name, "Web Store Customer")
		agg.assert_called_once()
