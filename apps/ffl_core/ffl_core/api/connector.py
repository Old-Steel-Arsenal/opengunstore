"""Remote MCP connectors (Claude, ChatGPT, any MCP client): who is calling, and
the audit trail of what they did.

The connector (the MCP server in http mode) holds no key: every request
carries the signed-in user's own OAuth bearer token, which Frappe has already
resolved to the session user before these run. These two endpoints add what the
token alone does not say:

* whether the token was issued to a CONNECTOR — a client created through dynamic
  client registration (owner "Guest", since registration is unauthenticated). A
  token issued to any OAuth app created in Desk is refused, so another
  integration's token can never drive the MCP;
* a durable, per-call record in Connector Audit Log — who, through which
  connector, which tool, with what arguments, and whether it succeeded. Reads
  included. Never purged or deleted (Activity Log is cleared after 90 days).
"""
from __future__ import annotations

import json
import re
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import now_datetime
from frappe.utils.data import get_system_timezone

from ffl_core.ffl_core.doctype.mcp_settings.mcp_settings import (
	SURFACES,
	WEB_SURFACES,
	surface_enabled,
	web_access,
)

# Argument values under these keys never reach the log — the same bounded rule
# the connector masks with before sending (gunstore_mcp.safety._CREDENTIAL_NAME),
# so ordinary fields (shipping_*, author, …) stay readable in the trail.
SECRET_KEY = re.compile(
	r"(passwd|password|pwd|secret|api[_-]?key|token|webhook|private[_-]?key"
	r"|dev[_-]?key|[_-]key\b|passphrase|credential|\bpin\b)", re.I)
ARGS_LIMIT = 4000
STATUS = {"started": "Started", "ok": "Success", "failed": "Failed"}


def _connector_gate() -> frappe._dict:
	"""The caller's OAuth client, from this request's own bearer token."""
	scheme, _sep, token = (frappe.get_request_header("Authorization") or "").partition(" ")
	if scheme.lower() != "bearer" or not token.strip():
		frappe.throw(_("This endpoint is for the remote connector (OAuth bearer token)."),
			frappe.PermissionError)
	bearer = frappe.db.get_value("OAuth Bearer Token", token.strip(),
		["user", "client", "status"], as_dict=True)
	if not bearer or bearer.status != "Active" or bearer.user != frappe.session.user:
		frappe.throw(_("Unknown or inactive token."), frappe.PermissionError)
	client = frappe.db.get_value("OAuth Client", bearer.client,
		["name", "app_name", "owner"], as_dict=True) or frappe._dict()
	return frappe._dict(user=bearer.user, client_id=client.get("name"),
		client_name=client.get("app_name") or "", connector=client.get("owner") == "Guest",
		expires_at=frappe.db.get_value("OAuth Bearer Token", token.strip(), "expiration_time"))


def _require_surface(surface, user: str) -> None:
	"""The surface must be switched on in MCP Settings (a connector that names
	none — an older build — counts as off), and a website surface also needs a
	website role (mcp_settings.WEB_ROLES)."""
	if not surface_enabled(surface):
		frappe.throw(_("The {0} MCP server is switched off in MCP Settings.")
			.format(surface if isinstance(surface, str) else "requested"), frappe.PermissionError)
	if surface in WEB_SURFACES and not web_access(user, surface):
		frappe.throw(_("{0} has no website access: a Website Connector Viewer or Website "
			"Connector Manager role is needed.").format(user), frappe.PermissionError)


def _redact(value):
	"""Mask secrets by key, in [field, op, value] filter triples, and inside
	JSON-string arguments."""
	if isinstance(value, dict):
		return {k: ("***" if SECRET_KEY.search(str(k)) else _redact(v)) for k, v in value.items()}
	if isinstance(value, list):
		if len(value) >= 3 and isinstance(value[0], str) and SECRET_KEY.search(value[0]):
			return [value[0], value[1], "***", *value[3:]]
		return [_redact(v) for v in value]
	if isinstance(value, str) and value[:1] in "{[":
		try:
			return json.dumps(_redact(json.loads(value)))
		except ValueError:
			return value
	return value


def _args_text(args) -> str:
	text = json.dumps(args, default=str, separators=(",", ":"))
	if len(text) > ARGS_LIMIT:
		# Still valid JSON for the Desk viewer, and says it was cut.
		text = json.dumps({"truncated": True, "head": text[:ARGS_LIMIT - 40]})
	return text


@frappe.whitelist(methods=["GET"])
def connector_identity(surface: str | None = None) -> dict:
	"""Who this bearer token belongs to, and whether it was issued to a connector.

	Args:
		surface: "cpa", "full" or "web_ro" — the connector asking.

	Returns:
		{user, full_name, client_id, client_name, connector, enabled, web_access,
		expires_at} — connector is true only for a client created by dynamic
		registration; enabled says whether that surface is switched on (answered,
		not refused: a switched-off server must not look like a bad token, or
		clients would loop on sign-in); web_access is the user's level on a
		website surface ("manager", "viewer" or "", by role alone; always "" for
		cpa and full); expires_at is the token's expiry as a Unix timestamp.
	"""
	c = _connector_gate()
	expires = None
	if c.expires_at:
		# Stored naive in the SITE time zone; .timestamp() on a naive datetime would
		# use the server's zone instead (UTC in the container — hours off).
		expires = frappe.utils.get_datetime(c.expires_at).replace(
			tzinfo=ZoneInfo(get_system_timezone())).timestamp()
	return {"user": c.user, "full_name": frappe.utils.get_fullname(c.user),
		"client_id": c.client_id, "client_name": c.client_name, "connector": c.connector,
		"enabled": surface_enabled(surface), "web_access": web_access(c.user, surface),
		"expires_at": int(expires) if expires else None}


@frappe.whitelist(methods=["POST"])
def log_connector_call(tool: str, surface: str, phase: str = "started",
		arguments: str | None = None, log: str | None = None,
		error: str | None = None) -> dict:
	"""Record one connector tool call in Connector Audit Log.

	Called before the tool runs (phase "started" — the connector refuses to run
	the tool if this fails, so nothing happens unrecorded) and once after it
	(phase "ok" / "failed", with the log name returned by the first call). A row
	moves from Started to its outcome exactly once; it cannot be rewritten.

	Args:
		tool: Tool name.
		surface: "cpa", "full" or "web_ro".
		phase: "started", "ok" or "failed".
		arguments: The tool arguments as JSON; secret-looking keys are masked.
		log: The Connector Audit Log returned by the "started" call (for "ok"/"failed").
		error: The error text, for "failed".

	Returns:
		{log} — the Connector Audit Log name.
	"""
	c = _connector_gate()
	if not c.connector:
		frappe.throw(_("Only a connector token can record connector calls."), frappe.PermissionError)
	# Annotations are strings here (__future__), so Frappe does not type-check:
	# a dict in `log` would become a filter matching other users' rows.
	for v in (tool, surface, phase, arguments, log, error):
		if v is not None and not isinstance(v, str):
			frappe.throw(_("Invalid argument type."))
	if phase not in STATUS:
		frappe.throw(_("phase must be started, ok or failed."))
	if surface not in SURFACES:
		frappe.throw(_("surface must be one of {0}.").format(", ".join(SURFACES)))
	# A call already running when the switch goes off may still close its record.
	if phase == "started":
		_require_surface(surface, c.user)
	if phase != "started":
		# Row lock: two concurrent closes cannot both see Started.
		rows = frappe.db.sql("select user, status from `tabConnector Audit Log` "
			"where name=%s for update", (log or "",), as_dict=True)
		row = rows[0] if rows else None
		if not row or row.user != c.user:
			frappe.throw(_("Not a connector call of this user."), frappe.PermissionError)
		if row.status != "Started":
			frappe.throw(_("This call's outcome is already recorded."), frappe.PermissionError)
		frappe.db.set_value("Connector Audit Log", log, {
			"status": STATUS[phase], "finished_at": now_datetime(),
			"error": _redact(str(error))[:1000] if error else None,
		})
		return {"log": log}
	try:
		args = _redact(json.loads(arguments)) if arguments else {}
	except ValueError:
		args = {"raw": "(unparseable)"}
	doc = frappe.get_doc({
		"doctype": "Connector Audit Log",
		"started_at": now_datetime(),
		"user": c.user,
		"full_name": frappe.utils.get_fullname(c.user),
		"tool": str(tool)[:140],
		"surface": surface,
		"status": STATUS["started"],
		"client_name": c.client_name,
		"client_id": c.client_id,
		"arguments": _args_text(args),
	})
	doc.insert(ignore_permissions=True)
	return {"log": doc.name}
