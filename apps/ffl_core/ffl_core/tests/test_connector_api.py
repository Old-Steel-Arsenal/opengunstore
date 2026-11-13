"""Remote MCP connector endpoints: identity (only dynamically registered
connector clients count) and the per-call Connector Audit Log trail."""
from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api import connector
from ffl_core.ffl_core.doctype.mcp_settings import mcp_settings


class ConnectorFixtures(FrappeTestCase):
	"""Switches, OAuth clients and tokens shared by the test classes below (no tests here)."""

	def setUp(self):
		super().setUp()
		self.user = "Administrator"
		self._switch(cpa=1, full=1)
		self.connector_token = self._token(self._client("Claude", owner="Guest"))
		self.desk_token = self._token(self._client("Some Desk App", owner="Administrator"))

	def _switch(self, cpa=0, full=0, web_ro=0):
		doc = frappe.get_single("MCP Settings")
		doc.enable_cpa, doc.enable_full, doc.enable_web_ro = cpa, full, web_ro
		doc.save(ignore_permissions=True)

	def _client(self, name, owner):
		doc = frappe.get_doc({"doctype": "OAuth Client", "app_name": name, "scopes": "all",
			"default_redirect_uri": "https://claude.ai/api/mcp/auth_callback",
			"redirect_uris": "https://claude.ai/api/mcp/auth_callback"})
		doc.insert(ignore_permissions=True)
		# Dynamic registration runs unauthenticated, so its clients are owned by Guest.
		frappe.db.set_value("OAuth Client", doc.name, "owner", owner)
		return doc.name

	def _token(self, client, status="Active"):
		tok = frappe.generate_hash(length=30)
		frappe.get_doc({"doctype": "OAuth Bearer Token", "access_token": tok, "client": client,
			"user": self.user, "scopes": "all", "status": status, "expires_in": 3600,
			"expiration_time": frappe.utils.add_to_date(None, hours=1)}).insert(ignore_permissions=True)
		return tok

	def _as(self, token):
		return patch.object(connector.frappe, "get_request_header",
			side_effect=lambda h, *a: f"Bearer {token}" if h == "Authorization" else None)


class TestConnectorApi(ConnectorFixtures):
	def test_identity_marks_only_registered_clients_as_connectors(self):
		with self._as(self.connector_token):
			me = connector.connector_identity("cpa")
		self.assertEqual((me["user"], me["client_name"], me["connector"]),
			(self.user, "Claude", True))
		with self._as(self.desk_token):
			self.assertFalse(connector.connector_identity("cpa")["connector"])

	def test_no_or_inactive_token_is_refused(self):
		with patch.object(connector.frappe, "get_request_header", return_value=None):
			with self.assertRaises(frappe.PermissionError):
				connector.connector_identity("cpa")
		with self._as(self._token(frappe.db.get_value("OAuth Bearer Token",
				self.connector_token, "client"), status="Revoked")):
			with self.assertRaises(frappe.PermissionError):
				connector.connector_identity("cpa")

	def test_call_is_logged_before_and_after_with_secrets_masked(self):
		args = json.dumps({"doctype": "GunBroker Settings", "dev_key": "abc",
			"values": {"consumer_key": "ck", "password": "x", "qty": 3}})
		with self._as(self.connector_token):
			log = connector.log_connector_call("frappe_update_document", "full",
				arguments=args)["log"]
			row = frappe.get_doc("Connector Audit Log", log)
			self.assertEqual((row.user, row.status, row.tool, row.surface, row.client_name),
				(self.user, "Started", "frappe_update_document", "full", "Claude"))
			for secret in ("abc", "ck", '"x"'):
				self.assertNotIn(secret, row.arguments)
			self.assertIn('"qty":3', row.arguments)
			connector.log_connector_call("frappe_update_document", "full", phase="failed",
				log=log, error="boom")
		row.reload()
		self.assertEqual((row.status, row.error), ("Failed", "boom"))
		self.assertTrue(row.finished_at)

	def test_outcome_is_recorded_once(self):
		with self._as(self.connector_token):
			log = connector.log_connector_call("x", "cpa")["log"]
			connector.log_connector_call("x", "cpa", phase="failed", log=log, error="e")
			with self.assertRaises(frappe.PermissionError):
				connector.log_connector_call("x", "cpa", phase="ok", log=log)

	def test_a_dict_log_cannot_reach_other_rows(self):
		"""r4: a JSON object in `log` must not become a filter over everyone's rows."""
		with self._as(self.connector_token):
			mine = connector.log_connector_call("x", "cpa")["log"]
			other = connector.log_connector_call("y", "cpa")["log"]
			with self.assertRaises(frappe.ValidationError):
				connector.log_connector_call("x", "cpa", phase="ok", log={"status": "Started"})
		self.assertEqual({frappe.db.get_value("Connector Audit Log", n, "status") for n in (mine, other)},
			{"Started"})

	def test_secrets_in_values_filters_and_json_strings_are_masked(self):
		args = json.dumps({"filters": [["api_key", "=", "sk_live_abc"]],
			"kwargs": {"args": json.dumps({"password": "hunter2"})},
			"values": {"shipping_rule": "Ground", "author": "Tom"}})
		with self._as(self.connector_token):
			row = frappe.get_doc("Connector Audit Log",
				connector.log_connector_call("frappe_run_method", "full", arguments=args)["log"])
		for secret in ("sk_live_abc", "hunter2"):
			self.assertNotIn(secret, row.arguments)
		# Ordinary fields stay readable.
		self.assertIn("Ground", row.arguments)
		self.assertIn("Tom", row.arguments)
		json.loads(row.arguments)

	def test_long_arguments_stay_valid_json(self):
		with self._as(self.connector_token):
			row = frappe.get_doc("Connector Audit Log", connector.log_connector_call(
				"x", "full", arguments=json.dumps({"description": "y" * 20000}))["log"])
		self.assertTrue(json.loads(row.arguments)["truncated"])

	def test_identity_reports_the_token_expiry(self):
		with self._as(self.connector_token):
			me = connector.connector_identity("cpa")
		# Against real time, not a naive-datetime conversion (r4 E2E: 5 h off).
		import time
		self.assertAlmostEqual(me["expires_at"], time.time() + 3600, delta=120)

	def test_switched_off_surfaces_answer_disabled_and_cannot_start_calls(self):
		self._switch(cpa=1, full=0)
		with self._as(self.connector_token):
			self.assertTrue(connector.connector_identity("cpa")["enabled"])
			# Answered, not refused: a refusal would read as a bad token (sign-in loop).
			self.assertFalse(connector.connector_identity("full")["enabled"])
			self.assertFalse(connector.connector_identity()["enabled"], "no surface = off")
			with self.assertRaises(frappe.PermissionError):
				connector.log_connector_call("x", "full")

	def test_a_running_call_can_close_after_the_switch_goes_off(self):
		with self._as(self.connector_token):
			log = connector.log_connector_call("x", "full")["log"]
			self._switch(cpa=1, full=0)
			connector.log_connector_call("x", "full", phase="ok", log=log)
		self.assertEqual(frappe.db.get_value("Connector Audit Log", log, "status"), "Success")

	def test_switches_own_the_oauth_settings(self):
		o = frappe.get_single("OAuth Settings")
		self._switch(cpa=0, full=0)
		o.reload()
		self.assertEqual((o.show_auth_server_metadata, o.enable_dynamic_client_registration), (0, 0))
		# Drift: someone turns consent-skipping on by hand, everywhere it can be.
		frappe.db.set_single_value("OAuth Settings", "skip_authorization", 1)
		frappe.db.set_single_value("OAuth Provider Settings", "skip_authorization", "Auto")
		guest_client = frappe.db.get_value("OAuth Bearer Token", self.connector_token, "client")
		frappe.db.set_value("OAuth Client", guest_client, "skip_authorization", 1)
		self._switch(cpa=1, full=0)
		o.reload()
		self.assertEqual((o.show_auth_server_metadata, o.enable_dynamic_client_registration,
			o.skip_authorization), (1, 1, 0))
		self.assertEqual(frappe.db.get_single_value("OAuth Provider Settings", "skip_authorization"),
			"Force")
		self.assertEqual(frappe.db.get_value("OAuth Client", guest_client, "skip_authorization"), 0)

	def test_migrate_resyncs_oauth(self):
		from ffl_core.ffl_core.doctype.mcp_settings.mcp_settings import sync_oauth
		self._switch(cpa=0, full=0)
		frappe.db.set_single_value("OAuth Settings", "enable_dynamic_client_registration", 1)
		sync_oauth()
		self.assertEqual(frappe.db.get_single_value("OAuth Settings",
			"enable_dynamic_client_registration"), 0)

	def test_urls_show_without_a_save(self):
		doc = frappe.get_doc("MCP Settings")
		doc.run_method("onload")
		self.assertTrue(doc.cpa_url.endswith("/connector/cpa/mcp"))
		self.assertTrue(doc.full_url.endswith("/connector/full/mcp"))

	def test_claude_code_commands_use_the_server_names(self):
		from ffl_core.ffl_core.doctype.mcp_settings import mcp_settings
		with patch.object(mcp_settings.frappe.utils, "get_url",
				return_value="https://pos.example.com"):
			urls = mcp_settings.connector_urls()
		self.assertEqual(urls["cpa_claude_code"], "claude mcp add -s user --transport http "
			"gunstore-pos-cpa https://pos.example.com/connector/cpa/mcp")
		self.assertEqual(urls["full_claude_code"], "claude mcp add -s user --transport http "
			"gunstore-pos https://pos.example.com/connector/full/mcp")
		self.assertEqual(urls["web_ro_url"], "https://pos.example.com/connector/web_ro/mcp")
		self.assertEqual(urls["web_ro_claude_code"], "claude mcp add -s user --transport http "
			"gunstore-web-ro https://pos.example.com/connector/web_ro/mcp")

	def test_rows_cannot_be_deleted(self):
		with self._as(self.connector_token):
			log = connector.log_connector_call("x", "cpa")["log"]
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Connector Audit Log", log, ignore_permissions=True)

	def test_desk_app_tokens_cannot_write_the_trail(self):
		with self._as(self.desk_token):
			with self.assertRaises(frappe.PermissionError):
				connector.log_connector_call("x", "full")

	def test_cannot_touch_someone_elses_log(self):
		with self._as(self.connector_token):
			log = connector.log_connector_call("x", "cpa")["log"]
		frappe.db.set_value("Connector Audit Log", log, "user", "Guest")
		with self._as(self.connector_token):
			with self.assertRaises(frappe.PermissionError):
				connector.log_connector_call("x", "cpa", phase="ok", log=log)


class TestWebConnector(ConnectorFixtures):
	"""The website read-only surface (web_ro): its switch AND a website role gate a call;
	connector_identity reports the role level as web_access. Uses the token helpers in ConnectorFixtures."""

	VIEWER, MANAGER = mcp_settings.WEB_VIEWER_ROLE, mcp_settings.WEB_MANAGER_ROLE

	def setUp(self):
		super().setUp()
		mcp_settings.ensure_web_roles()
		self.addCleanup(frappe.set_user, "Administrator")

	def _user(self, *roles):
		email = f"web-{frappe.generate_hash(length=8)}@example.com"
		user = frappe.get_doc({"doctype": "User", "email": email, "first_name": "Web",
			"send_welcome_email": 0, "roles": [{"role": r} for r in roles]})
		user.insert(ignore_permissions=True)
		return email

	def _as_user(self, *roles):
		"""Signed in as a new user with these roles, through a connector token of theirs."""
		self.user = self._user(*roles)
		token = self._token(frappe.db.get_value("OAuth Bearer Token", self.connector_token, "client"))
		frappe.set_user(self.user)
		return self._as(token)

	def _access(self, *roles, surface="web_ro"):
		with self._as_user(*roles):
			return connector.connector_identity(surface)

	def test_web_access_follows_the_role(self):
		self._switch(web_ro=1)
		for roles, want in (((), ""), ((self.VIEWER,), "viewer"), ((self.MANAGER,), "manager"),
				(("System Manager",), "manager"), ((self.VIEWER, self.MANAGER), "manager"),
				(("Sales Manager", "Accounts Manager", "Stock Manager"), "")):
			with self.subTest(roles=roles):
				me = self._access(*roles)
				self.assertEqual((me["web_access"], me["enabled"]), (want, True))

	def test_frappes_own_website_manager_role_gives_no_website_access(self):
		"""Frappe ships "Website Manager" (it edits the POS's web pages and scripts): it is not ours."""
		self.assertNotIn("Website Manager", mcp_settings.WEB_ROLES)
		self._switch(web_ro=1)
		with self._as_user("Website Manager"):
			self.assertEqual(connector.connector_identity("web_ro")["web_access"], "")
			with self.assertRaises(frappe.PermissionError):
				connector.log_connector_call("woocommerce_orders_query", "web_ro")

	def test_web_access_is_reported_with_the_switch_off(self):
		self._switch(cpa=1, full=1, web_ro=0)
		me = self._access(self.VIEWER)
		self.assertEqual((me["enabled"], me["web_access"]), (False, "viewer"))

	def test_web_access_is_empty_on_other_surfaces(self):
		self._switch(cpa=1, full=1, web_ro=1)
		for surface in ("cpa", "full", None, "web_full", "nope"):
			with self.subTest(surface=surface):
				self.assertEqual(self._access(self.MANAGER, surface=surface)["web_access"], "")
		frappe.set_user("Administrator")  # every role, System Manager included
		with self._as(self.connector_token):
			self.assertEqual(connector.connector_identity("cpa")["web_access"], "")

	def test_start_needs_switch_and_role_with_distinct_reasons(self):
		cases = (
			(0, (self.MANAGER,), "switched off in MCP Settings"),
			(0, (), "switched off in MCP Settings"),
			(1, (), "has no website access"),
			(1, ("Sales Manager",), "has no website access"),
		)
		for switch, roles, reason in cases:
			with self.subTest(switch=switch, roles=roles):
				self._switch(cpa=1, full=1, web_ro=switch)
				with self._as_user(*roles):
					with self.assertRaises(frappe.PermissionError) as e:
						connector.log_connector_call("woocommerce_orders_query", "web_ro")
				self.assertIn(reason, str(e.exception))
				self.assertFalse(frappe.db.exists("Connector Audit Log", {"user": self.user}))

	def test_viewer_and_manager_calls_are_logged_under_web_ro(self):
		self._switch(web_ro=1)
		for roles in ((self.VIEWER,), (self.MANAGER,), ("System Manager",)):
			with self.subTest(roles=roles), self._as_user(*roles):
				log = connector.log_connector_call("woocommerce_orders_query", "web_ro",
					arguments=json.dumps({"per_page": 5, "api_key": "k"}))["log"]
				row = frappe.get_doc("Connector Audit Log", log)
				self.assertEqual((row.user, row.surface, row.status), (self.user, "web_ro", "Started"))
				self.assertNotIn('"k"', row.arguments)
				connector.log_connector_call("woocommerce_orders_query", "web_ro", phase="ok", log=log)
				self.assertEqual(frappe.db.get_value("Connector Audit Log", log, "status"), "Success")

	def test_a_running_web_call_closes_after_switch_or_role_goes(self):
		self._switch(web_ro=1)
		with self._as_user(self.VIEWER):
			log = connector.log_connector_call("x", "web_ro")["log"]
			frappe.db.delete("Has Role", {"parent": self.user, "role": self.VIEWER})
			frappe.clear_cache(user=self.user)
			self.assertEqual(connector.connector_identity("web_ro")["web_access"], "")
			self._switch(web_ro=0)
			connector.log_connector_call("x", "web_ro", phase="failed", log=log, error="e")
		self.assertEqual(frappe.db.get_value("Connector Audit Log", log, "status"), "Failed")

	def test_cpa_and_full_need_no_website_role(self):
		self._switch(cpa=1, full=1, web_ro=0)
		with self._as_user("Accounts User"):
			for surface in ("cpa", "full"):
				log = connector.log_connector_call("x", surface)["log"]
				self.assertEqual(frappe.db.get_value("Connector Audit Log", log, "surface"), surface)

	def test_the_web_switch_alone_turns_oauth_on(self):
		self._switch(cpa=0, full=0, web_ro=1)
		o = frappe.get_single("OAuth Settings")
		self.assertEqual((o.show_auth_server_metadata, o.enable_dynamic_client_registration,
			o.skip_authorization), (1, 1, 0))
		self._switch(cpa=0, full=0, web_ro=0)
		o.reload()
		self.assertEqual((o.show_auth_server_metadata, o.enable_dynamic_client_registration), (0, 0))

	def test_web_roles_are_made_once_without_desk(self):
		frappe.db.delete("Has Role", {"role": self.VIEWER})
		frappe.delete_doc("Role", self.VIEWER, ignore_permissions=True, force=True)
		mcp_settings.ensure_web_roles()
		mcp_settings.ensure_web_roles()
		for role in (self.VIEWER, self.MANAGER):
			self.assertEqual(frappe.db.count("Role", {"name": role}), 1)
			self.assertEqual(frappe.db.get_value("Role", role, "desk_access"), 0)
		for hook in ("after_install", "after_migrate"):
			self.assertIn("ffl_core.ffl_core.doctype.mcp_settings.mcp_settings.ensure_web_roles",
				frappe.get_hooks(hook))

	def test_urls_show_the_web_address_without_a_save(self):
		doc = frappe.get_doc("MCP Settings")
		doc.run_method("onload")
		self.assertTrue(doc.web_ro_url.endswith("/connector/web_ro/mcp"))
		self.assertIn("gunstore-web-ro", doc.web_ro_claude_code)
