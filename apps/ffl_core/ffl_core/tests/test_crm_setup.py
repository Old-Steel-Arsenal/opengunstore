"""ffl_core.crm setup: declared in hooks.py, idempotent, ERPNext CRM Settings kept as the POS needs
them, CRM's lead switch moved to ours, the product backfill queued once."""
from __future__ import annotations

import ast
import os
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.crm import setup
from ffl_core.tests.crm_support import CRMTestCase


def _hooks_py() -> dict:
	"""Read hooks.py itself (docs/testing.md: frappe.get_hooks can be stubbed)."""
	with open(os.path.join(frappe.get_app_path("ffl_core"), "hooks.py")) as f:
		tree = ast.parse(f.read())
	return {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
		if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
		and n.targets[0].id in ("required_apps", "after_install", "after_migrate", "after_app_install",
			"doc_events", "scheduler_events", "override_whitelisted_methods")}


def _listed(value) -> list:
	return value if isinstance(value, list) else [value]


class TestDeclaration(FrappeTestCase):
	def test_crm_is_a_required_app(self):
		self.assertIn("crm", _hooks_py()["required_apps"])

	def test_lifecycle_hooks_are_declared(self):
		h = _hooks_py()
		self.assertIn("ffl_core.crm.setup.after_install", h["after_install"])
		self.assertIn("ffl_core.crm.setup.ensure", h["after_migrate"])
		self.assertIn("ffl_core.crm.setup.after_app_install", h["after_app_install"])
		self.assertIn("ffl_core.crm.item.refresh_product_prices", h["scheduler_events"]["daily"])

	def test_every_crm_handler_resolves(self):
		h = _hooks_py()
		paths = [p for events in h["doc_events"].values() for value in events.values() for p in _listed(value)
			if p.startswith("ffl_core.crm.")]
		self.assertGreaterEqual(len(paths), 12)
		for path in paths:
			self.assertTrue(callable(frappe.get_attr(path)), path)
		# The skip flag is set before CRM's own Item hooks (after_insert / on_update) read it.
		self.assertIn("ffl_core.crm.item.skip_crm_sync", _listed(h["doc_events"]["Item"]["before_insert"]))
		self.assertIn("ffl_core.crm.item.skip_crm_sync", _listed(h["doc_events"]["Item"]["before_save"]))
		# The product is priced after the Item Price sync it reads.
		on_update = _listed(h["doc_events"]["Item"]["on_update"])
		self.assertLess(on_update.index("ffl_core.item_hooks.sync_standard_selling_price"),
			on_update.index("ffl_core.crm.item.sync_product"))


class TestLifecycle(CRMTestCase):
	def test_ensure_is_idempotent(self):
		with patch("frappe.enqueue"):
			self.assertEqual(setup.ensure(), {"ok": 1})
			self.assertEqual(setup.ensure(), {"ok": 1})
		for dt, fields in setup.CUSTOM_FIELDS.items():
			self.assertEqual(frappe.db.count("Custom Field", {"dt": dt, "fieldname": fields[0]["fieldname"]}), 1)
		self.assertEqual(frappe.db.get_single_value("ERPNext CRM Settings", "sync_products"), 0)
		self.assertEqual(frappe.db.get_single_value("ERPNext CRM Settings", "enabled"), 1)
		self.assertTrue(frappe.db.exists("CRM Form Script", setup.CONTACT_SCRIPT))

	def test_installing_crm_on_an_existing_site_runs_the_setup(self):
		with patch.object(setup, "ensure") as ensure:
			setup.after_app_install("hrms")
			ensure.assert_not_called()
			setup.after_app_install("crm")
			ensure.assert_called_once_with(backfill=False)

	def test_installing_crm_queues_no_backfill_before_the_migrate(self):
		frappe.db.set_global(setup.BACKFILL_FLAG, None)
		with patch("frappe.enqueue") as enqueue:
			self.assertEqual(setup.ensure(backfill=False), {"ok": 1})
		enqueue.assert_not_called()

	def test_crm_settings_flip_back_to_ours(self):
		frappe.db.set_single_value("ERPNext CRM Settings", {"sync_products": 1, "is_erpnext_in_different_site": 1})
		self.assertEqual(set(setup.ensure_crm_settings()), {"sync_products", "is_erpnext_in_different_site"})
		self.assertEqual(setup.ensure_crm_settings(), {})

	def test_own_domains_default_once(self):
		frappe.db.set_global(setup.SEEDED_FLAG, None)
		frappe.db.set_single_value(setup.SETTINGS, "own_domains", "")
		if not frappe.db.exists("Email Domain", "store.test"):
			frappe.get_doc({"doctype": "Email Domain", "domain_name": "store.test", "email_server": "imap.store.test",
				"smtp_server": "smtp.store.test"}).db_insert()
		self.assertTrue(setup.ensure_settings())
		self.assertIn("store.test", frappe.db.get_single_value(setup.SETTINGS, "own_domains").split("\n"))
		frappe.db.set_single_value(setup.SETTINGS, "own_domains", "")
		self.assertFalse(setup.ensure_settings())
		self.assertFalse(frappe.db.get_single_value(setup.SETTINGS, "own_domains"))

	def test_crm_lead_switches_move_to_ours(self):
		name = frappe.get_doc({"doctype": "Email Account", "email_account_name": "_T CRM Old Inbox",
			"email_id": "old@store.test", "enable_incoming": 0, "enable_outgoing": 0, "awaiting_password": 1}).insert(
			ignore_permissions=True).name
		frappe.db.set_value("Email Account", name, {"create_lead_from_incoming_email": 1, "ffl_auto_lead": 0})
		self.assertIn(name, setup.take_over_lead_flags())
		self.assertEqual(frappe.db.get_value("Email Account", name, ["create_lead_from_incoming_email", "ffl_auto_lead"]),
			(0, 1))
		self.assertEqual(setup.take_over_lead_flags(), [])

	def test_the_backfill_is_enqueued_once(self):
		frappe.db.set_global(setup.BACKFILL_FLAG, None)
		with patch("frappe.enqueue") as enqueue:
			self.assertTrue(setup.enqueue_backfill())
		self.assertEqual(enqueue.call_args.args[0], "ffl_core.crm.item.backfill_products")
		self.assertEqual(enqueue.call_args.kwargs["queue"], "long")
		frappe.db.set_global(setup.BACKFILL_FLAG, "1")
		with patch("frappe.enqueue") as enqueue:
			self.assertFalse(setup.enqueue_backfill())
		enqueue.assert_not_called()

	def test_the_settings_are_on_the_settings_workspace(self):
		if not frappe.db.exists("Workspace", "Settings"):
			self.skipTest("Settings workspace not installed")
		links = frappe.get_doc("Workspace", "Settings").links
		self.assertTrue(any(l.type == "Link" and l.link_to == setup.SETTINGS for l in links))


class TestStaffDefaultApp(FrappeTestCase):
	def test_new_staff_open_the_pos_and_website_users_keep_theirs(self):
		from frappe.apps import get_default_path

		staff = frappe.get_doc({"doctype": "User", "email": "crm-new-clerk@example.com", "first_name": "New",
			"send_welcome_email": 0, "roles": [{"role": "Sales User"}]}).insert(ignore_permissions=True)
		self.assertEqual(staff.default_app, "ffl_core")
		frappe.set_user(staff.name)
		try:
			self.assertNotEqual(get_default_path(), "/apps")
		finally:
			frappe.set_user("Administrator")
		dealer = frappe.get_doc({"doctype": "User", "email": "crm-new-dealer@example.com", "first_name": "Dealer",
			"send_welcome_email": 0, "user_type": "Website User"}).insert(ignore_permissions=True)
		self.assertFalse(dealer.default_app)

	def test_an_account_given_desk_roles_later_opens_the_pos(self):
		user = frappe.get_doc({"doctype": "User", "email": "crm-later-clerk@example.com", "first_name": "Later",
			"send_welcome_email": 0}).insert(ignore_permissions=True)
		self.assertEqual(user.user_type, "Website User")
		self.assertFalse(user.default_app)
		user.append("roles", {"role": "Sales User"})
		user.save(ignore_permissions=True)
		self.assertEqual((user.user_type, user.default_app), ("System User", "ffl_core"))


class TestCurrency(CRMTestCase):
	"""A deal made in the store's currency never needs the public rate API, so mail routing does not."""

	def _store_currency(self) -> str:
		company = setup._default_company()
		return frappe.db.get_value("Company", company, "default_currency")

	def test_crm_takes_the_stores_currency(self):
		frappe.db.set_single_value(setup.FCRM_SETTINGS, "currency", "INR")
		self.assertEqual(setup.ensure_crm_currency(), self._store_currency())
		self.assertEqual(frappe.db.get_single_value(setup.FCRM_SETTINGS, "currency"), self._store_currency())
		self.assertIsNone(setup.ensure_crm_currency(), "already the store's: nothing to do")

	def test_a_new_deal_asks_no_rate_api(self):
		from ffl_core.crm.deals import open_deal_for

		frappe.db.set_single_value(setup.FCRM_SETTINGS, "currency", "INR")
		setup.ensure_crm_currency()
		contact = frappe.get_doc({"doctype": "Contact", "first_name": "No Rate Call"}).insert(ignore_permissions=True)
		with patch("crm.fcrm.doctype.crm_deal.crm_deal.get_exchange_rate",
				side_effect=AssertionError("a deal in the store's currency must not fetch a rate")) as fetch:
			deal = open_deal_for(contact.name)
		fetch.assert_not_called()
		self.assertEqual(frappe.db.get_value("CRM Deal", deal, "exchange_rate"), 1)

	def test_migrate_applies_it(self):
		frappe.db.set_single_value(setup.FCRM_SETTINGS, "currency", "INR")
		with patch("frappe.enqueue"):
			self.assertEqual(setup.ensure(), {"ok": 1})
		self.assertEqual(frappe.db.get_single_value(setup.FCRM_SETTINGS, "currency"), self._store_currency())


class TestDealStages(CRMTestCase):
	def _stock_stages(self):
		for old, new in setup.RENAMED_STAGES.items():
			frappe.rename_doc(setup.STATUS, new, old, force=True)
		for stage in setup.DROPPED_STAGES:
			frappe.get_doc({"doctype": setup.STATUS, "deal_status": stage, "type": "Ongoing", "position": 9}
				).insert(ignore_permissions=True)
		frappe.db.set_global(setup.DEAL_STAGES_FLAG, "")

	def _deal(self, status: str) -> str:
		return frappe.get_doc({"doctype": setup.DEAL, "status": status, "first_name": "_T Stage", "currency": "USD",
			"organization": None}).insert(ignore_permissions=True).name

	def _stages(self) -> list[str]:
		return frappe.get_all(setup.STATUS, pluck="name", order_by="position asc")

	def test_a_new_site_has_the_store_stages(self):
		self.assertEqual(self._stages(), list(setup.DEAL_STAGES))

	def test_stock_stages_become_the_store_stages_and_keep_their_deals(self):
		self._stock_stages()
		renamed = self._deal("Proposal/Quotation")
		dropped = self._deal("Negotiation")
		self.assertTrue(setup.ensure_deal_stages())
		self.assertEqual(self._stages(), list(setup.DEAL_STAGES))
		self.assertEqual(frappe.db.get_value(setup.STATUS, "Quoted", "deal_status"), "Quoted")
		self.assertEqual(frappe.db.get_value(setup.DEAL, renamed, "status"), "Quoted")
		self.assertEqual(frappe.db.get_value(setup.DEAL, dropped, "status"), "Talking")

	def test_a_stage_added_later_stays(self):
		frappe.get_doc({"doctype": setup.STATUS, "deal_status": "Negotiation", "type": "Ongoing", "position": 9}
			).insert(ignore_permissions=True)
		self.assertFalse(setup.ensure_deal_stages())
		self.assertTrue(frappe.db.exists(setup.STATUS, "Negotiation"))


class TestNoQuotes(FrappeTestCase):
	def test_the_deal_page_shows_no_quote_controls(self):
		from ffl_core.api.crm import deals_use_no_quotes

		base = "crm.fcrm.doctype.erpnext_crm_settings.erpnext_crm_settings."
		for method in ("can_view_quotations", "can_create_quotations"):
			self.assertEqual(_hooks_py()["override_whitelisted_methods"][base + method],
				"ffl_core.api.crm.deals_use_no_quotes")
		self.assertIs(deals_use_no_quotes(), False)
