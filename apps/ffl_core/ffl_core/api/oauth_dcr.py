"""Dynamic client registration that also accepts a `localhost` loopback redirect.

Frappe's register_client accepts a plain-http redirect only when its host is a
loopback IP (127.0.0.1 / ::1). Native MCP clients — Claude Code among them —
register http://localhost:<port>/callback (RFC 8252 §7.3), which Frappe's check
cannot parse as an IP and refuses ("redirect_uris must be https"), so they can
never sign in. This override (hooks.override_whitelisted_methods) widens exactly
that one check to the name `localhost`; everything else — validation, client
creation, the response — is Frappe's own register_client.
"""
from __future__ import annotations

import frappe
import frappe.integrations.utils as oauth_utils
from frappe.integrations.oauth2 import register_client as frappe_register_client
from frappe.permissions import SYSTEM_USER_ROLE

from ffl_core.ffl_core.doctype.mcp_settings.mcp_settings import WEB_ROLES

_frappe_is_secure = oauth_utils._is_secure_redirect_uri


def _is_secure_redirect_uri(uri) -> bool:
	if _frappe_is_secure(uri):
		return True
	return uri.scheme == "http" and (uri.host or "").lower() == "localhost"


# Module-level, once per process: validate_dynamic_client_metadata looks the
# helper up in its own module, and a per-request swap would race other threads.
oauth_utils._is_secure_redirect_uri = _is_secure_redirect_uri


@frappe.whitelist(allow_guest=True, methods=["POST"])
def register_client():
	"""RFC 7591 dynamic client registration (Frappe's), plus localhost loopback
	redirects. Open by design like Frappe's: the client still needs a user to
	sign in and approve before it gets any token.

	Returns:
		Frappe's registration response (201 with the client metadata, or 400).
	"""
	return frappe_register_client()


def allow_website_roles(doc, method=None):
	"""doc_events OAuth Client before_insert: a connector client (dynamic registration,
	owner Guest) may also be used by a user whose only POS roles are the website
	connector ones. Those roles have no Desk access, and Frappe lets a client with no
	allowed roles serve Desk users only ("Desk User"), so such a user could never
	sign in to the website connector. Desk users keep exactly the access they had;
	what each surface lets a user do is still decided per call (api/connector.py)."""
	if doc.owner != "Guest" or doc.get("allowed_roles"):
		return
	for role in (SYSTEM_USER_ROLE, *(r for r in WEB_ROLES if r != "System Manager")):
		# A role not made yet (new code before its migrate) is left out, never a failed registration.
		if frappe.db.exists("Role", role):
			doc.append("allowed_roles", {"role": role})
