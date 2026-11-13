"""MCP Settings — the on/off switches for the remote MCP servers that AI
assistants (Claude, ChatGPT, any MCP client with OAuth) connect to.

Three surfaces, switched separately: the read-only one (accounting), the full
one (writes, within each user's own role) and the website read-only one (the
store's website, through a bridge that reads it as one site user; who may use it
is decided here by role, WEB_ROLES). The switches are enforced by the POS
itself (ffl_core.api.connector), and they own the OAuth settings the servers sign
in through: with any on, the authorization-server metadata and dynamic client
registration are on; with all off, they are off. Consent is never skipped —
every user approves on the POS. Re-applied on every migrate (sync_oauth), so a
deploy or a hand edit of OAuth Settings cannot drift from the switches.
"""
from __future__ import annotations

import frappe
from frappe.model.document import Document

SURFACES = {"cpa": "enable_cpa", "full": "enable_full", "web_ro": "enable_web_ro"}
DOCTYPE = "MCP Settings"

# Who may use a website surface, and at which level: "manager" may also call the
# bridge's restricted abilities. Any other role has no website access — Frappe's own
# "Website Manager" (it edits the POS's web pages and scripts) included.
WEB_SURFACES = ("web_ro",)
WEB_VIEWER_ROLE = "Website Connector Viewer"
WEB_MANAGER_ROLE = "Website Connector Manager"
WEB_ROLES = {WEB_MANAGER_ROLE: "manager", "System Manager": "manager", WEB_VIEWER_ROLE: "viewer"}


def surface_enabled(surface) -> bool:
	field = SURFACES.get(surface) if isinstance(surface, str) else None
	if not field:
		return False
	try:
		return bool(frappe.db.get_single_value(DOCTYPE, field))
	except Exception:
		# Mid-deploy (new code, migrate not run yet): the switch reads off.
		return False


# The names the servers report (the MCP server's serverInfo) and that the skills
# and agents address tools by (mcp__gunstore-pos__*), so every install matches.
SERVER_NAMES = {"cpa": "gunstore-pos-cpa", "full": "gunstore-pos", "web_ro": "gunstore-web-ro"}


def web_access(user: str, surface) -> str:
	"""The user's level on a website surface: "manager", "viewer" or "" (none).

	Roles only; the switch is reported separately (connector_identity's enabled).
	Always "" for a surface that is not a website one."""
	if surface not in WEB_SURFACES:
		return ""
	levels = {WEB_ROLES[r] for r in frappe.get_roles(user) if r in WEB_ROLES}
	return "manager" if "manager" in levels else "viewer" if levels else ""


def ensure_web_roles() -> None:
	"""after_install / after_migrate: the website connector roles exist, so a manager can give them."""
	for role in (WEB_VIEWER_ROLE, WEB_MANAGER_ROLE):
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 0}).insert(
				ignore_permissions=True)


def connector_urls() -> dict:
	"""Each surface's server URL and the Claude Code command for it."""
	base = frappe.utils.get_url().rstrip("/")
	out = {}
	for surface in SURFACES:
		url = f"{base}/connector/{surface}/mcp"
		out[f"{surface}_url"] = url
		out[f"{surface}_claude_code"] = (
			f"claude mcp add -s user --transport http {SERVER_NAMES[surface]} {url}")
	return out


def sync_oauth() -> None:
	"""OAuth Settings follow the switches; consent is never skipped (after_migrate)."""
	on = int(any(surface_enabled(s) for s in SURFACES))
	oauth = frappe.get_single("OAuth Settings")
	want = {"show_auth_server_metadata": on, "enable_dynamic_client_registration": on,
		"skip_authorization": 0}
	if any((oauth.get(k) or 0) != v for k, v in want.items()):
		oauth.update(want)
		oauth.flags.ignore_permissions = True
		oauth.save()
	# The older provider-level switch can also skip consent ("Auto").
	if frappe.db.get_single_value("OAuth Provider Settings", "skip_authorization") != "Force":
		frappe.db.set_single_value("OAuth Provider Settings", "skip_authorization", "Force")
	# A connector client (dynamically registered, owner Guest) never skips consent.
	for name in frappe.get_all("OAuth Client", filters={"owner": "Guest", "skip_authorization": 1},
			pluck="name"):
		frappe.db.set_value("OAuth Client", name, "skip_authorization", 0)


class MCPSettings(Document):
	def onload(self):
		# Shown the moment a box is ticked, from the site's own URL.
		for k, v in connector_urls().items():
			self.set(k, v)

	def validate(self):
		self.update(connector_urls())

	def on_update(self):
		sync_oauth()
