"""Tests for the customer sale-receipt email on invoice submit."""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.receipts import _customer_email, send_sale_receipt, send_sale_receipt_task


class TestSaleReceipt(FrappeTestCase):
	def test_customer_email_prefers_contact_email(self):
		doc = frappe._dict(contact_email="buyer@example.com", customer="C1")
		self.assertEqual(_customer_email(doc), "buyer@example.com")

	@patch("frappe.db.get_value", return_value="fromcust@example.com")
	def test_customer_email_falls_back_to_customer_master(self, _gv):
		doc = frappe._dict(contact_email="", customer="C1")
		self.assertEqual(_customer_email(doc), "fromcust@example.com")

	@patch("frappe.enqueue")
	def test_hook_enqueues_after_commit_when_email_present(self, mock_enqueue):
		doc = frappe._dict(doctype="Sales Invoice", name="SI-1",
			contact_email="buyer@example.com", customer="C1")
		send_sale_receipt(doc)
		mock_enqueue.assert_called_once()
		self.assertEqual(mock_enqueue.call_args.args[0], "ffl_core.receipts.send_sale_receipt_task")
		self.assertTrue(mock_enqueue.call_args.kwargs["enqueue_after_commit"])
		self.assertEqual(mock_enqueue.call_args.kwargs["name"], "SI-1")

	@patch("frappe.enqueue")
	@patch("frappe.db.get_value", return_value="")
	def test_hook_no_enqueue_when_no_email(self, _gv, mock_enqueue):
		doc = frappe._dict(doctype="Sales Invoice", name="SI-2", contact_email="", customer="C1")
		send_sale_receipt(doc)
		mock_enqueue.assert_not_called()

	@patch("frappe.enqueue")
	def test_hook_skips_a_counter_pickup_ticket(self, mock_enqueue):
		"""api/counter_pickup's $0 POS Invoice: the buyer got the order's receipt
		on order day; a second one listing the gun at $0.00 must not go out."""
		doc = frappe._dict(doctype="POS Invoice", name="ACC-PSINV-T1",
			contact_email="buyer@example.com", customer="C1",
			ffl_counter_order="ACC-SINV-2026-00001")
		send_sale_receipt(doc)
		mock_enqueue.assert_not_called()

	@patch("frappe.enqueue")
	def test_hook_skips_consolidated_pos_sales_invoice(self, mock_enqueue):
		# POS closing consolidates the day's POS Invoices into a Sales Invoice
		# (is_consolidated=1); the POS Invoice already emailed the receipt at
		# sale, so the consolidated SI must NOT re-send (double-mail regression).
		doc = frappe._dict(doctype="Sales Invoice", name="SI-CONS",
			contact_email="buyer@example.com", customer="C1", is_consolidated=1)
		send_sale_receipt(doc)
		mock_enqueue.assert_not_called()

	@patch("frappe.enqueue")
	def test_hook_skips_web_order_sales_invoice(self, mock_enqueue):
		# Web-order revenue SIs: WooCommerce already emailed the buyer their order
		# confirmation at checkout, and the per-buyer web customer now
		# carries an email — this SI must NOT auto-send a second receipt (mirrors
		# interstate_tax / customer_tax_exempt skipping web SIs on woo_online_order).
		doc = frappe._dict(doctype="Sales Invoice", name="SI-WEB",
			contact_email="buyer@example.com", customer="C1",
			woo_online_order="WOO-ORD-00007")
		send_sale_receipt(doc)
		mock_enqueue.assert_not_called()

	@patch("frappe.enqueue")
	def test_hook_still_sends_for_pos_invoice(self, mock_enqueue):
		# The POS Invoice (at sale time) is the one receipt that SHOULD send.
		doc = frappe._dict(doctype="POS Invoice", name="PINV-1",
			contact_email="buyer@example.com", customer="C1")
		send_sale_receipt(doc)
		mock_enqueue.assert_called_once()

	@patch("frappe.enqueue")
	def test_hook_skips_a_web_orders_pickup_ticket(self, mock_enqueue):
		# The buyer already got the web order's receipt at checkout.
		doc = frappe._dict(doctype="POS Invoice", name="PINV-2",
			contact_email="buyer@example.com", customer="C1", woo_online_order="WOO-ORD-1")
		send_sale_receipt(doc)
		mock_enqueue.assert_not_called()

	@patch("frappe.attach_print", return_value={"fname": "r.pdf", "fcontent": b"%PDF"})
	@patch("frappe.sendmail")
	def test_task_sends_receipt(self, mock_sendmail, _ap):
		invoice = frappe._dict(doctype="Sales Invoice", name="SI-1",
			contact_email="buyer@example.com", customer="C1", customer_name="Buyer",
			grand_total=100, meta=frappe._dict(default_print_format=None))
		template = frappe._dict(subject="Receipt {{ doc.name }}", use_html=1,
			response_html="<p>Hi {{ customer_name }}</p>", response="Hi")
		_real_get_doc = frappe.get_doc
		_calls = []
		def _side_effect(doctype, name=None, *args, **kwargs):
			if doctype == "Sales Invoice":
				return invoice
			if doctype == "Email Template":
				return template
			return _real_get_doc(doctype, name, *args, **kwargs)
		with patch("frappe.get_doc", side_effect=_side_effect):
			send_sale_receipt_task("Sales Invoice", "SI-1")
		mock_sendmail.assert_called_once()
		self.assertEqual(mock_sendmail.call_args.kwargs["recipients"], ["buyer@example.com"])
		self.assertEqual(mock_sendmail.call_args.kwargs["subject"], "Receipt SI-1")


# ---------------------------------------------------------------------------
# Negative trade-in ticket: the receipt prints the
# allowance, the tax (even at 0.00) and what the store paid the customer.
# Rendered from the SHIPPED fixtures (what a migrate installs), against an
# in-memory invoice — no submit gate, no GL.
# ---------------------------------------------------------------------------
import json
import os
import re

_FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def _fixture_html(print_format):
	with open(os.path.join(_FIXTURES, "print_format.json")) as f:
		return next(p["html"] for p in json.load(f) if p["name"] == print_format)


def _invoice(doctype, credits=(450, 1200, 300, 2750, 750), gun=565.0, tax=0.0, is_return=0, taxes=None, **extra):
	company = frappe.db.get_value("Company", {}, "name")
	items = [{"item_code": "GLOCK", "item_name": "Glock 19", "qty": 1, "rate": gun, "amount": gun}]
	items += [{"item_code": "TRADE-IN-CREDIT", "item_name": "Trade-in credit", "qty": 1, "rate": -c, "amount": -c}
		for c in credits]
	net = gun - sum(credits)
	if taxes is None:
		taxes = [{"description": "Texas Sales Tax 8.25% (Springfield)", "tax_amount": tax, "charge_type": "On Net Total"}] if tax or credits else []
	tax = sum(t["tax_amount"] for t in taxes)
	return frappe.get_doc({
		"doctype": doctype, "name": "ACC-TEST-NEG", "company": company, "currency": "USD",
		"customer_name": "Buyer", "posting_date": "2026-10-06", "is_return": is_return,
		"items": items, "taxes": taxes, "net_total": net, "total_taxes_and_charges": tax,
		"grand_total": net + tax, **extra})


def _text(html):
	"""Collapse the receipt markup to one line per table row: 'label | amount'."""
	rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S)
	return [" | ".join(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
		for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, flags=re.S)) for r in rows]


class TestNegativeTicketReceipt(FrappeTestCase):
	def _render(self, doctype, print_format, **kw):
		doc = _invoice(doctype, **kw)
		return frappe.render_template(_fixture_html(print_format), {"doc": doc})

	def test_pos_receipt_prints_allowance_zero_tax_and_the_payout(self):
		html = self._render("POS Invoice", "POS Receipt",
			ffl_trade_in_payout_method="Zelle", ffl_trade_in_payout_reference="CONF-7781")
		rows = _text(html)
		self.assertIn("Items sold | $ 565.00", rows)
		self.assertIn("Trade-in allowance | $ -5,450.00", rows)
		self.assertIn("Texas Sales Tax 8.25% (Springfield) | $ 0.00", rows)   # the tax row itself, at 0.00
		self.assertIn("Total | $ -4,885.00", rows)
		self.assertIn("Paid to customer (Zelle, CONF-7781) | $ 4,885.00", rows)
		# the footing the customer can check on the page
		self.assertEqual(rows.index("Items sold | $ 565.00") + 1, rows.index("Trade-in allowance | $ -5,450.00"))

	def test_cash_payout_names_the_method_and_no_reference(self):
		rows = _text(self._render("POS Invoice", "POS Receipt", ffl_trade_in_payout_method="Cash"))
		self.assertIn("Paid to customer (Cash) | $ 4,885.00", rows)

	def test_a_typed_reference_is_escaped(self):
		html = self._render("POS Invoice", "POS Receipt", ffl_trade_in_payout_method="Check",
			ffl_trade_in_payout_reference="<script>x</script>")
		self.assertNotIn("<script>x</script>", html)
		self.assertIn("&lt;script&gt;", html)

	def test_sales_receipt_format_prints_the_same_lines(self):
		rows = _text(self._render("Sales Invoice", "Sales Receipt", ffl_trade_in_payout_method="Cash"))
		self.assertIn("Trade-in allowance | $ -5,450.00", rows)
		self.assertIn("Texas Sales Tax 8.25% (Springfield) | $ 0.00", rows)
		self.assertIn("Paid to customer (Cash) | $ 4,885.00", rows)

	def test_a_ticket_with_no_tax_rows_still_prints_the_zero_tax_line(self):
		for doctype, fmt in (("POS Invoice", "POS Receipt"), ("Sales Invoice", "Sales Receipt")):
			rows = _text(self._render(doctype, fmt, taxes=[], ffl_trade_in_payout_method="Cash"))
			self.assertIn("Sales tax | $ 0.00", rows, fmt)

	def test_shipping_prints_as_its_own_row_not_inside_a_lump_sales_tax_line(self):
		# net -4,885 + 0.00 tax + 25.00 shipping = -4,860: the payout follows the total
		taxes = [{"description": "Texas Sales Tax 8.25% (Springfield)", "tax_amount": 0.0, "charge_type": "On Net Total"},
			{"description": "Shipping", "tax_amount": 25.0, "charge_type": "Actual"}]
		for doctype, fmt in (("POS Invoice", "POS Receipt"), ("Sales Invoice", "Sales Receipt")):
			rows = _text(self._render(doctype, fmt, taxes=taxes, ffl_trade_in_payout_method="Cash"))
			self.assertIn("Texas Sales Tax 8.25% (Springfield) | $ 0.00", rows, fmt)
			self.assertIn("Shipping | $ 25.00", rows, fmt)
			self.assertIn("Total | $ -4,860.00", rows, fmt)
			self.assertIn("Paid to customer (Cash) | $ 4,860.00", rows, fmt)

	def test_the_payout_is_the_rounded_total_when_there_is_one(self):
		# rounded_total -4,885 beside grand_total -4,885.40: the server pays the rounded figure
		rows = _text(self._render("POS Invoice", "POS Receipt", ffl_trade_in_payout_method="Cash",
			rounded_total=-4885.0, grand_total=-4885.4))
		self.assertIn("Paid to customer (Cash) | $ 4,885.00", rows)

	def test_an_ordinary_trade_in_ticket_prints_as_before(self):
		# 565 - 300 + 21.86 tax = 286.86: customer pays, nothing is paid out.
		html = self._render("POS Invoice", "POS Receipt", credits=(300,), tax=21.86)
		rows = _text(html)
		self.assertFalse([r for r in rows if r.startswith(("Paid to customer", "Items sold", "Trade-in allowance"))])
		self.assertIn("Subtotal | $ 265.00", rows)
		self.assertIn("Texas Sales Tax 8.25% (Springfield) | $ 21.86", rows)

	def test_a_return_prints_as_before(self):
		rows = _text(self._render("POS Invoice", "POS Receipt", credits=(), gun=-565.0, is_return=1))
		self.assertFalse([r for r in rows if r.startswith("Paid to customer")])

	@patch("frappe.attach_print", return_value={"fname": "r.pdf", "fcontent": b"%PDF"})
	@patch("frappe.sendmail")
	def test_email_says_what_we_paid_not_a_negative_total(self, mock_sendmail, _ap):
		with open(os.path.join(_FIXTURES, "email_template.json")) as f:
			tpl = json.load(f)[0]
		doc = _invoice("POS Invoice", ffl_trade_in_payout_method="Zelle", contact_email="buyer@example.com",
			customer="C1")
		doc.meta.default_print_format = None
		template = frappe._dict(subject="Receipt", use_html=1, response_html=tpl["response_html"], response=tpl["response"])
		_real = frappe.get_doc
		def _get_doc(doctype, name=None, *a, **k):
			if doctype == "POS Invoice" and name == doc.name:
				return doc
			if doctype == "Email Template":
				return template
			return _real(doctype, name, *a, **k)
		with patch("frappe.get_doc", side_effect=_get_doc):
			send_sale_receipt_task("POS Invoice", doc.name)
		body = mock_sendmail.call_args.kwargs["message"]
		self.assertIn("we paid you $ 4,885.00 by Zelle", body)
		self.assertNotIn("-4885", body)
		self.assertNotIn("(total", body)

	@patch("frappe.attach_print", return_value={"fname": "r.pdf", "fcontent": b"%PDF"})
	@patch("frappe.sendmail")
	def test_email_for_an_ordinary_sale_is_unchanged(self, mock_sendmail, _ap):
		with open(os.path.join(_FIXTURES, "email_template.json")) as f:
			tpl = json.load(f)[0]
		doc = _invoice("POS Invoice", credits=(300,), tax=21.86, contact_email="buyer@example.com", customer="C1")
		doc.meta.default_print_format = None
		template = frappe._dict(subject="Receipt", use_html=1, response_html=tpl["response_html"], response=tpl["response"])
		_real = frappe.get_doc
		def _get_doc(doctype, name=None, *a, **k):
			if doctype == "POS Invoice" and name == doc.name:
				return doc
			if doctype == "Email Template":
				return template
			return _real(doctype, name, *a, **k)
		with patch("frappe.get_doc", side_effect=_get_doc):
			send_sale_receipt_task("POS Invoice", doc.name)
		body = mock_sendmail.call_args.kwargs["message"]
		self.assertIn("Thank you for your purchase", body)
		self.assertIn("(total 286.86)", body)

	def test_the_plain_text_email_names_the_method_like_the_html_one(self):
		with open(os.path.join(_FIXTURES, "email_template.json")) as f:
			tpl = json.load(f)[0]
		doc = _invoice("POS Invoice", ffl_trade_in_payout_method="Zelle", rounded_total=-4885.0, grand_total=-4885.4)
		ctx = {"doc": doc, "customer_name": "Buyer"}
		for field in ("response", "response_html"):
			text = frappe.render_template(tpl[field], ctx)
			self.assertIn("we paid you $ 4,885.00 by Zelle", text, field)
		ordinary = frappe.render_template(tpl["response"], {"doc": _invoice("POS Invoice", credits=(300,), tax=21.86), "customer_name": "Buyer"})
		self.assertIn("(total 286.86)", ordinary)
