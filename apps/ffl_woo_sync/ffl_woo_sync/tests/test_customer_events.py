"""POS → shop customer sync (``woocommerce/customer_events.py``).

Pure helpers are tested directly. The job runs against the real test database
(real Customers, Contacts and Singles); only ``requests.post`` is replaced.
A test class rolls back once, so rows persist from test to test: each test
stamps its customers in a year of its own, far past every real record, and
starts its watermark at that year's first day.
"""
from __future__ import annotations

import itertools
import json
import uuid
from unittest.mock import patch

import frappe
import requests
from frappe.cache_manager import clear_defaults_cache
from frappe.tests.utils import FrappeTestCase
from frappe.utils.password import set_encrypted_password

from ffl_woo_sync.woocommerce import customer_events as ce
from ffl_woo_sync.woocommerce import status_events as se

SETTINGS = "WooCommerce Settings"
SECRET = "test-secret"
_years = itertools.count(2100)


class TestHelpers(FrappeTestCase):
	def test_store_host_drops_www_and_path(self):
		self.assertEqual(ce.store_host("https://www.Shop.test/"), "shop.test")
		self.assertEqual(ce.store_host("https://shop.test/shop"), "shop.test")
		self.assertEqual(ce.store_host("shop.test"), "shop.test")

	def test_clean_email(self):
		self.assertEqual(ce.clean_email("  Ann@Example.COM "), "ann@example.com")
		for bad in (None, "", "   ", "not-an-email"):
			self.assertEqual(ce.clean_email(bad), "", bad)

	def test_the_shops_own_domain_is_skipped(self):
		self.assertEqual(ce.clean_email("staff@shop.test", "shop.test"), "")
		self.assertEqual(ce.clean_email("ann@mail.shop.test", "shop.test"), "ann@mail.shop.test")
		self.assertEqual(ce.clean_email("ann@example.com", "shop.test"), "ann@example.com")

	def test_split_name(self):
		self.assertEqual(ce.split_name("Ann", "Lee", "Ignored Name"), ("Ann", "Lee"))
		self.assertEqual(ce.split_name("", "", "Ann Marie Lee"), ("Ann", "Lee"))
		self.assertEqual(ce.split_name(None, None, "Cher"), ("Cher", ""))
		self.assertEqual(ce.split_name("", "", "Ann Lee - 2"), ("Ann", "Lee"))
		self.assertEqual(ce.split_name("", "", "Ann Lee-3"), ("Ann", "Lee-3"))
		self.assertEqual(ce.split_name("Ann Lee", "", "X"), ("Ann", "Lee"))
		self.assertEqual(ce.split_name("", "", ""), ("", ""))

	def test_customer_payload_prefers_the_contact_email(self):
		row = {"customer": "C1", "customer_name": "Ann Lee", "customer_email": "old@example.com",
			"contact_email": "New@Example.com", "first_name": "", "last_name": "",
			"changed": "2090-01-02 00:00:00"}
		self.assertEqual(ce.customer_payload(row), {
			"customer": "C1", "email": "new@example.com", "first_name": "Ann", "last_name": "Lee",
			"modified": "2090-01-02 00:00:00"})
		row["contact_email"] = None
		self.assertEqual(ce.customer_payload(row)["email"], "old@example.com")
		row["customer_email"] = ""
		self.assertIsNone(ce.customer_payload(row))

	def test_trim_tied_tail(self):
		rows = [{"changed": 1}, {"changed": 2}, {"changed": 2}]
		self.assertEqual(ce.trim_tied_tail(rows, 4), rows)  # not full: nothing was cut
		self.assertEqual(ce.trim_tied_tail(rows, 3), [{"changed": 1}])
		tied = [{"changed": 5}] * 3
		self.assertEqual(ce.trim_tied_tail(tied, 3), tied)


def _response(code):
	r = requests.Response()
	r.status_code = code
	return r


class TestPushCustomerChanges(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.tag = uuid.uuid4().hex[:8]
		self.year = next(_years)
		self.base = self.ts(1)
		frappe.db.set_single_value(SETTINGS, "store_url", "https://shop.test")
		frappe.db.set_single_value(SETTINGS, "send_customer_events", 1)
		set_encrypted_password(SETTINGS, SETTINGS, SECRET, "outbound_webhook_secret")
		frappe.db.set_global(ce.WATERMARK_KEY, self.base)
		frappe.db.set_global(ce.LAST_FAILURE_KEY, None)
		self.addCleanup(clear_defaults_cache)
		# The settle window keeps real, just-saved customers out of a batch; the
		# test records are stamped in the far future, so open it past them.
		p = patch.object(ce, "_settled_cutoff", return_value="9999-12-31 00:00:00")
		p.start()
		self.addCleanup(p.stop)

	def ts(self, day):
		return f"{self.year}-01-{day:02d} 00:00:00"

	def _customer(self, name, email="", modified=None, contact=None):
		"""A Customer (and optionally a primary Contact) stamped at ``modified``."""
		customer = frappe.get_doc({
			"doctype": "Customer", "customer_name": f"{name} {self.tag}",
			"customer_group": "Individual", "territory": "All Territories"}).insert(ignore_permissions=True)
		modified = modified or self.ts(2)
		if email:
			frappe.db.set_value("Customer", customer.name, "email_id", email, update_modified=False)
		frappe.db.set_value("Customer", customer.name, "modified", modified, update_modified=False)
		if contact:
			doc = frappe.get_doc({
				"doctype": "Contact", "first_name": contact.get("first", ""), "last_name": contact.get("last", ""),
				"email_ids": [{"email_id": contact["email"], "is_primary_email": 1}],
				"links": [{"link_doctype": "Customer", "link_name": customer.name}]}).insert(ignore_permissions=True)
			frappe.db.set_value("Contact", doc.name, "modified", contact.get("modified", modified), update_modified=False)
			frappe.db.set_value("Customer", customer.name, "customer_primary_contact", doc.name, update_modified=False)
		return customer.name

	def _run(self, code=200):
		with patch.object(ce.requests, "post", return_value=_response(code)) as post:
			out = ce.push_customer_changes()
		return out, post

	def _sent(self, post):
		"""``[(url, headers, body, event)]`` for each POST made."""
		return [(c.args[0], c.kwargs["headers"], c.kwargs["data"], json.loads(c.kwargs["data"]))
			for c in post.call_args_list]

	def test_box_off_makes_no_call(self):
		self._customer("Off", "off@example.com")
		frappe.db.set_single_value(SETTINGS, "send_customer_events", 0)
		out, post = self._run()
		post.assert_not_called()
		self.assertEqual(frappe.db.get_global(ce.WATERMARK_KEY), self.base)

	def test_no_secret_makes_no_call(self):
		self._customer("NoSecret", "nosecret@example.com")
		set_encrypted_password(SETTINGS, SETTINGS, "", "outbound_webhook_secret")
		out, post = self._run()
		post.assert_not_called()

	def test_first_run_sets_the_watermark_and_sends_nothing(self):
		self._customer("First", "first@example.com")
		frappe.db.set_global(ce.WATERMARK_KEY, None)
		clear_defaults_cache()
		out, post = self._run()
		post.assert_not_called()
		self.assertTrue(frappe.db.get_global(ce.WATERMARK_KEY))

	def test_delivers_a_signed_batch_and_advances_the_watermark(self):
		name = self._customer("Ann Lee", "Ann@Example.com", self.ts(2))
		self._customer("Staff", "staff@shop.test", self.ts(3))
		self._customer("Blank", "", self.ts(4))
		out, post = self._run(200)
		(url, headers, body, event), = self._sent(post)
		self.assertEqual(url, "https://shop.test/wp-json/osa-growth/v1/pos-customers")
		self.assertEqual(headers["x-osa-signature"], se.sign(SECRET, headers["x-osa-timestamp"], body))
		self.assertEqual(event["site"], "retail")
		self.assertEqual(event["event_id"], f"customers:{self.base}:{self.ts(4)}")
		self.assertEqual([c["email"] for c in event["customers"]], ["ann@example.com"])
		self.assertEqual(event["customers"][0], {
			"customer": name, "email": "ann@example.com", "first_name": "Ann", "last_name": self.tag,
			"modified": self.ts(2)})
		# Skipped customers (the shop's own domain, no email) still move the watermark.
		self.assertTrue(str(frappe.db.get_global(ce.WATERMARK_KEY)).startswith(self.ts(4)[:10]))
		# Nothing new: the next run sends nothing.
		out, post = self._run(200)
		post.assert_not_called()

	def test_a_contact_change_alone_re_sends_the_customer(self):
		name = self._customer("Contact", "old@example.com", "2000-01-01 00:00:00",
			contact={"email": "fresh@example.com", "first": "Cy", "last": "Doe", "modified": self.ts(5)})
		out, post = self._run(200)
		(_, _, _, event), = self._sent(post)
		self.assertEqual(event["customers"], [{
			"customer": name, "email": "fresh@example.com", "first_name": "Cy", "last_name": "Doe",
			"modified": self.ts(5)}])

	def test_a_disabled_customer_is_not_sent(self):
		name = self._customer("Gone", "gone@example.com")
		frappe.db.set_value("Customer", name, "disabled", 1, update_modified=False)
		out, post = self._run(200)
		post.assert_not_called()

	def test_a_server_error_keeps_the_watermark_and_logs_once(self):
		self._customer("Retry", "retry@example.com", self.ts(2))
		with patch("frappe.log_error") as log:
			out, post = self._run(500)
			self.assertEqual(out["reason"], "delivery failed")
			self.assertEqual(frappe.db.get_global(ce.WATERMARK_KEY), self.base)
			log.assert_called_once()
			# The same batch failing again on the next hourly run stays quiet...
			self._run(500)
			log.assert_called_once()
			self.assertEqual(frappe.db.get_global(ce.WATERMARK_KEY), self.base)
		# ...and once it goes through, the customer is delivered and the marker cleared.
		out, post = self._run(200)
		self.assertEqual(out["sent"], 1)
		self.assertFalse(frappe.db.get_global(ce.LAST_FAILURE_KEY))

	def test_a_transport_error_does_not_raise(self):
		self._customer("Down", "down@example.com")
		with patch.object(ce.requests, "post", side_effect=requests.ConnectionError("down")), \
			 patch("frappe.log_error") as log:
			out = ce.push_customer_changes()
		self.assertEqual(out["reason"], "delivery failed")
		log.assert_called_once()
		self.assertEqual(frappe.db.get_global(ce.WATERMARK_KEY), self.base)

	def test_batches_are_chained_and_a_tie_is_never_split(self):
		with patch.object(ce, "BATCH_SIZE", 2):
			self._customer("A", "a@example.com", self.ts(2))
			self._customer("B", "b@example.com", self.ts(3))
			self._customer("C", "c@example.com", self.ts(3))
			self._customer("D", "d@example.com", self.ts(4))
			out, post = self._run(200)
		batches = [[c["email"] for c in e["customers"]] for *_, e in self._sent(post)]
		# Batch 1 would end on the B/C tie, so it holds only A; the tie goes together next.
		self.assertEqual(batches, [["a@example.com"], ["b@example.com", "c@example.com"], ["d@example.com"]])
		self.assertTrue(str(frappe.db.get_global(ce.WATERMARK_KEY)).startswith(self.ts(4)[:10]))
