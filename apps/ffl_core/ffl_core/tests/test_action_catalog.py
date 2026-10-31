"""The action catalog (ffl_core.api.action_catalog.list_actions): guest rows never listed,
rows filtered by the caller's roles and DocType permissions, ranking and lookup.

Runs on the real framework with real users; only the catalog rows are a fixture,
swapped in for the shipped files so the cases do not move with the API."""
from __future__ import annotations

import json
import os
import tempfile

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api import action_catalog

CLERK = "catalog-clerk@example.com"
NOBODY = "catalog-norole@example.com"
WEB = "catalog-web@example.com"

FIXTURE = [
	{"method": "acme.hooks.webhook", "module": "acme.hooks", "summary": "Receive a webhook.",
		"args": [], "http": ["POST"], "guest": True, "auth": None},
	{"method": "acme.cash.refund_charge", "module": "acme.cash", "summary": "Refund a card charge.",
		"description": "Long text.", "returns": "{ok}", "source": "acme/cash.py:1",
		"args": [{"name": "charge", "type": "string", "required": True, "desc": "The charge."}],
		"http": ["POST"], "guest": False, "auth": {"roles": ["Sales Manager", "System Manager"]}},
	{"method": "acme.cash.charge_card", "module": "acme.cash", "summary": "Charge a card.",
		"args": [{"name": "amount", "type": "number", "required": True, "desc": "Dollars."}],
		"http": ["POST"], "guest": False, "auth": {"roles": ["Sales User", "Sales Manager"]}},
	{"method": "acme.items.rename_item", "module": "acme.items", "summary": "Rename an item.",
		"args": [], "http": ["POST"], "guest": False,
		"auth": {"perm": "write permission on Item", "perm_doctype": "Item", "perm_ptype": "write"}},
	{"method": "acme.items.item_overview", "module": "acme.items", "summary": "Item overview.",
		"args": [], "http": ["GET"], "guest": False, "auth": None},
	{"method": "acme.items.soft_list", "module": "acme.items", "summary": "Filtered list.",
		"args": [], "http": ["POST"], "guest": False, "auth": {"roles": ["Sales User"], "soft": True}},
]


def _user(email: str, roles: list[str], user_type: str = "System User"):
	if not frappe.db.exists("User", email):
		frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0],
			"send_welcome_email": 0, "user_type": user_type}).insert(ignore_permissions=True)
	frappe.db.delete("Has Role", {"parent": email, "parenttype": "User"})
	user = frappe.get_doc("User", email)
	if roles:
		user.add_roles(*roles)
	frappe.db.set_value("User", email, "user_type", user_type)
	frappe.clear_cache(user=email)


class TestActionCatalog(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_user(CLERK, ["Sales User"])
		_user(NOBODY, [])
		_user(WEB, [], user_type="Website User")
		cls._dir = tempfile.TemporaryDirectory()
		cls.path = os.path.join(cls._dir.name, "api_catalog.json")
		with open(cls.path, "w", encoding="utf-8") as fh:
			json.dump(FIXTURE, fh)

	@classmethod
	def tearDownClass(cls):
		cls._dir.cleanup()
		super().tearDownClass()

	def setUp(self):
		self._files = action_catalog._catalog_files
		action_catalog._catalog_files = lambda: [("acme", "1.0.0", self.path)]
		action_catalog._cache["key"] = None

	def tearDown(self):
		action_catalog._catalog_files = self._files
		action_catalog._cache["key"] = None
		frappe.set_user("Administrator")

	def methods(self, **kw):
		return [r["method"] for r in action_catalog.list_actions(**kw)]

	def test_guest_rows_are_never_listed(self):
		self.assertNotIn("acme.hooks.webhook", self.methods())
		self.assertEqual(action_catalog.list_actions(method="acme.hooks.webhook"), [])

	def test_rows_follow_the_callers_roles(self):
		frappe.set_user(CLERK)
		got = {r["method"]: r["runnable"] for r in action_catalog.list_actions()}
		self.assertNotIn("acme.cash.refund_charge", got)           # manager only
		self.assertEqual(got["acme.cash.charge_card"], True)
		self.assertEqual(got["acme.items.soft_list"], action_catalog.AT_CALL_TIME)
		self.assertEqual(got["acme.items.item_overview"], action_catalog.AT_CALL_TIME)
		self.assertEqual(got.get("acme.items.rename_item"),
			True if frappe.has_permission("Item", "write") else None)

		frappe.set_user(NOBODY)
		got = self.methods()
		self.assertEqual(sorted(got), ["acme.items.item_overview"])  # no role, no Item write

	def test_administrator_sees_every_non_guest_row(self):
		self.assertEqual(len(self.methods()), len(FIXTURE) - 1)

	def test_website_users_are_refused(self):
		frappe.set_user(WEB)
		with self.assertRaises(frappe.PermissionError):
			action_catalog.list_actions()

	def test_query_ranks_and_module_filters(self):
		self.assertEqual(self.methods(query="refund")[0], "acme.cash.refund_charge")
		self.assertEqual(self.methods(query="card charge")[:2],
			["acme.cash.charge_card", "acme.cash.refund_charge"])
		self.assertEqual(self.methods(query="zzzz"), [])
		self.assertEqual(sorted(self.methods(module="acme.items.")),
			["acme.items.item_overview", "acme.items.rename_item", "acme.items.soft_list"])
		self.assertEqual(len(self.methods(limit=2)), 2)
		self.assertEqual(len(self.methods(limit=999)), len(FIXTURE) - 1)

	def test_search_rows_are_compact_and_method_returns_the_full_row(self):
		(row,) = [r for r in action_catalog.list_actions(query="refund") if r["method"] == "acme.cash.refund_charge"]
		self.assertEqual(row["args"], [{"name": "charge", "type": "string", "required": True}])
		self.assertEqual(row["roles"], ["Sales Manager", "System Manager"])
		self.assertNotIn("description", row)
		(full,) = action_catalog.list_actions(method="acme.cash.refund_charge")
		self.assertEqual((full["description"], full["returns"], full["source"], full["args"][0]["desc"]),
			("Long text.", "{ok}", "acme/cash.py:1", "The charge."))
		frappe.set_user(CLERK)
		self.assertEqual(action_catalog.list_actions(method="acme.cash.refund_charge"), [])

	def test_the_shipped_catalogs_load(self):
		action_catalog._catalog_files = self._files
		rows = action_catalog._load_rows()
		methods = {r["method"] for r in rows}
		self.assertIn("ffl_core.api.action_catalog.list_actions", methods)
		self.assertTrue(any(m.startswith("ffl_integrations.") for m in methods))
		frappe.set_user(CLERK)
		self.assertEqual(action_catalog.list_actions(method="ffl_core.api.action_catalog.list_actions")[0]["runnable"],
			action_catalog.AT_CALL_TIME)
