"""Test support for ffl_core.marketing: a test case that runs each test in a savepoint rolled back after
it, with Marketing Settings › Enabled switched on through the settings sandbox, and fresh addresses."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.marketing import CONTACT, EVENT, REQUEST, SETTINGS
from ffl_core.tests.settings_sandbox import SettingsSandbox

SAVEPOINT = "ffl_marketing_test"
_n = [0]


def address(prefix: str = "mkt") -> str:
	"""A fresh, valid address no other test uses."""
	_n[0] += 1
	return f"{prefix}-{_n[0]}-{frappe.generate_hash(length=6)}@example.org"


def events_of(email: str, kind: str | None = None) -> list[dict]:
	filters = {"contact": email, **({"kind": kind} if kind else {})}
	return frappe.get_all(EVENT, filters=filters, fields=["kind", "detail", "dedupe_key", "campaign", "occurred_at"],
		order_by="creation asc, name asc")


def requests_of(email: str) -> list[dict]:
	return frappe.get_all(REQUEST, filters={"contact": email},
		fields=["kind", "status", "item", "sku", "web_product_id", "model_key", "label", "url", "requested_at", "notified_at"],
		order_by="creation asc, name asc")


def make_customer(email: str | None = None, name: str | None = None, **fields):
	"""A Customer (individual) listed under *email*, inserted through the real hooks."""
	doc = frappe.get_doc({"doctype": "Customer", "customer_name": name or f"_Test Mkt {frappe.generate_hash(length=6)}",
		"customer_type": "Individual", "customer_group": "Individual", "territory": "All Territories",
		**({"ffl_email": email} if email else {}), **fields})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc


def status_of(email: str) -> str | None:
	return frappe.db.get_value(CONTACT, email, "status")


class MarketingTestCase(SettingsSandbox, FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.savepoint(SAVEPOINT)
		self.set_single(SETTINGS, "enabled", 1)

	def tearDown(self):
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.set_user("Administrator")
		super().tearDown()
