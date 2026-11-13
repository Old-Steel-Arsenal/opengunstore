"""Dynamic registration accepts an http://localhost callback (Claude Code) and
still refuses other plain-http redirects."""
from __future__ import annotations

from types import SimpleNamespace

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api import oauth_dcr


def _uri(scheme, host):
	return SimpleNamespace(scheme=scheme, host=host)


class TestLocalhostRedirect(FrappeTestCase):
	def test_localhost_is_a_loopback_redirect(self):
		self.assertTrue(oauth_dcr._is_secure_redirect_uri(_uri("http", "localhost")))
		self.assertTrue(oauth_dcr._is_secure_redirect_uri(_uri("http", "LOCALHOST")))
		self.assertTrue(oauth_dcr._is_secure_redirect_uri(_uri("http", "127.0.0.1")))
		self.assertTrue(oauth_dcr._is_secure_redirect_uri(_uri("https", "claude.ai")))

	def test_other_plain_http_is_still_refused(self):
		for host in ("evil.example.com", "localhost.evil.com", "10.0.0.5"):
			self.assertFalse(oauth_dcr._is_secure_redirect_uri(_uri("http", host)), host)

	def test_frappe_validation_uses_the_widened_check(self):
		import frappe.integrations.utils as oauth_utils
		self.assertIs(oauth_utils._is_secure_redirect_uri, oauth_dcr._is_secure_redirect_uri)


class TestWebsiteRolesSignIn(FrappeTestCase):
	"""A user whose only POS role is a website connector role (no Desk access, so a
	Website User) can authorize a connector client; Desk users still can; a client made
	in Desk keeps Frappe's default (Desk users only)."""

	def setUp(self):
		super().setUp()
		from ffl_core.ffl_core.doctype.mcp_settings import mcp_settings
		mcp_settings.ensure_web_roles()
		self.addCleanup(frappe.set_user, "Administrator")

	def _client(self, as_guest):
		frappe.set_user("Guest" if as_guest else "Administrator")
		doc = frappe.get_doc({"doctype": "OAuth Client", "app_name": "Test connector",
			"scopes": "all", "redirect_uris": "http://localhost:33418/callback",
			"default_redirect_uri": "http://localhost:33418/callback"}).insert(ignore_permissions=True)
		frappe.set_user("Administrator")
		return doc

	def _user(self, *roles):
		email = f"dcr-{frappe.generate_hash(length=8)}@example.com"
		frappe.get_doc({"doctype": "User", "email": email, "first_name": "Dcr", "send_welcome_email": 0,
			"roles": [{"role": r} for r in roles]}).insert(ignore_permissions=True)
		return email

	def _allowed(self, client, user):
		frappe.set_user(user)
		try:
			return client.user_has_allowed_role()
		finally:
			frappe.set_user("Administrator")

	def test_website_only_users_can_use_a_connector_client(self):
		from ffl_core.ffl_core.doctype.mcp_settings.mcp_settings import WEB_MANAGER_ROLE, WEB_VIEWER_ROLE
		client = self._client(as_guest=True)
		for role in (WEB_VIEWER_ROLE, WEB_MANAGER_ROLE):
			user = self._user(role)
			self.assertEqual(frappe.db.get_value("User", user, "user_type"), "Website User")
			self.assertTrue(self._allowed(client, user), role)
		self.assertTrue(self._allowed(client, self._user("Sales User")), "Desk users keep access")
		self.assertFalse(self._allowed(client, self._user()), "no role, no sign-in")

	def test_a_desk_made_client_keeps_frappes_default(self):
		client = self._client(as_guest=False)
		self.assertEqual([r.role for r in client.allowed_roles], ["Desk User"])
