"""The action catalog: every server endpoint the caller can run, searchable.

Backs the MCP tool list_pos_actions. Each installed app may ship an
``api_catalog.json`` inside its package (``<app>/<app>/api_catalog.json``),
generated from source by ``scripts/generate_api_docs.py`` (``make api-docs``;
``--app`` for an extension): one row per whitelisted endpoint with its summary,
arguments, HTTP verbs and the permission gate derived from its code. This module
reads those files, keeps the rows the caller can run, and ranks them against a
plain-word query.

The listing is advice, not the gate: every endpoint still checks the caller
itself when it is called. Rows whose outcome only the call can tell (a soft
filter, a runtime-resolved role list, an extension switch, no gate at entry) are
listed with ``runnable: "checked at call time"``.
"""
from __future__ import annotations

import json
import os
import re

import frappe
from frappe import _
from frappe.utils import cint

CATALOG_NAME = "api_catalog.json"
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
AT_CALL_TIME = "checked at call time"

# (app, version, mtime) per installed app with a catalog -> its rows. Per worker process.
_cache: dict[str, object] = {"key": None, "rows": []}


def _catalog_files() -> list[tuple[str, str, str]]:
	"""(app, version, path) for every installed app that ships a catalog."""
	found = []
	for app in frappe.get_installed_apps():
		try:
			path = frappe.get_app_path(app, CATALOG_NAME)
		except Exception:  # an app listed on the site but missing from the bench
			continue
		if os.path.isfile(path):
			version = getattr(frappe.get_module(app), "__version__", "")
			found.append((app, version, path))
	return found


def _load_rows() -> list[dict]:
	files = _catalog_files()
	key = tuple((app, version, os.path.getmtime(path)) for app, version, path in files)
	if _cache["key"] != key:
		rows = []
		for _app, _version, path in files:
			try:
				with open(path, encoding="utf-8") as fh:
					data = json.load(fh)
			except (OSError, ValueError):
				frappe.log_error(title="Action catalog unreadable", message=path)
				continue
			rows.extend(r for r in data if isinstance(r, dict) and r.get("method"))
		_cache["key"], _cache["rows"] = key, rows
	return _cache["rows"]


def _runnable(auth: dict | None, roles: set[str], is_admin: bool):
	"""True, AT_CALL_TIME, or None (the caller cannot run it: not listed)."""
	auth = auth or {}
	deferred = bool(auth.get("soft") or auth.get("switch"))
	if auth.get("roles"):
		if not (is_admin or roles & set(auth["roles"])):
			return None
		return AT_CALL_TIME if deferred else True
	if auth.get("perm_doctype"):
		try:
			allowed = is_admin or frappe.has_permission(
				auth["perm_doctype"], auth.get("perm_ptype") or "read")
		except Exception:  # a DocType this site does not have
			allowed = False
		if not allowed:
			return None
		return AT_CALL_TIME if deferred else True
	# a runtime-resolved label, a perm without its structured half (an older catalog),
	# the extension switch alone, or no gate at entry: only the call can tell
	return True if is_admin and not auth.get("switch") else AT_CALL_TIME


def _public_row(row: dict, runnable, full: bool) -> dict:
	auth = row.get("auth") or {}
	out = {
		"method": row["method"],
		"module": row.get("module", ""),
		"summary": row.get("summary", ""),
		"args": [
			{k: a.get(k) for k in (("name", "type", "required", "desc") if full else ("name", "type", "required"))}
			for a in row.get("args") or []
		],
		"http": row.get("http") or ["POST"],
	}
	if auth.get("roles"):
		out["roles"] = auth["roles"]
	if auth.get("perm"):
		out["perm"] = auth["perm"]
	if auth.get("label"):
		out["gate"] = auth["label"]
	if auth.get("switch"):
		out["switch"] = True
	out["runnable"] = runnable
	if full:
		out["description"] = row.get("description", "")
		out["returns"] = row.get("returns", "")
		out["source"] = row.get("source", "")
	return out


_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list[str]:
	return _WORD.findall((text or "").lower())


def _score(row: dict, tokens: list[str]) -> int:
	"""Simple word matching: the dotted path weighs most, then argument names, then the summary."""
	method_words = _words(row["method"])
	arg_words = [w for a in row.get("args") or [] for w in _words(a.get("name", ""))]
	summary_words = _words(row.get("summary", ""))
	score = 0
	for token in tokens:
		if token in method_words:
			score += 4
		elif any(token in w for w in method_words):
			score += 2
		if token in arg_words:
			score += 2
		if any(w.startswith(token) or (len(w) > 3 and token.startswith(w)) for w in summary_words):
			score += 1
	return score


@frappe.whitelist()
def list_actions(query: str | None = None, module: str | None = None, method: str | None = None,
		limit: int = DEFAULT_LIMIT):
	"""Search the server endpoints the signed-in user can run, across every installed app.

	Reads the api_catalog.json each app ships (core apps and installed extensions),
	drops guest endpoints (webhooks), and keeps the rows whose derived gate the caller
	passes: a role list the caller holds some role of, or a DocType permission the
	caller has. Rows only the call itself can decide (soft filters, runtime role lists,
	an extension switch, no gate at entry) are kept with runnable "checked at call time".
	The endpoint still checks the caller when it is called.

	Args:
	    query (str): Words matched against the dotted path, argument names and summary; best matches first.
	    module (str): A dotted prefix, e.g. "ffl_integrations.gunbroker"; only endpoints under it.
	    method (str): One exact dotted path; returns that endpoint's full row (description, Returns text and every argument's description) or an empty list.
	    limit (int): Rows returned, 1 to 100 (default 25).

	Returns:
	    list: Rows of {method, module, summary, args [{name, type, required}], http, roles or perm or gate,
	    switch (when an extension switch applies), runnable (true or "checked at call time")};
	    with method, at most one row that also carries description, returns, source and each argument's desc.

	Permissions:
	    Any signed-in desk user (System User); each user sees only the endpoints their roles and
	    DocType permissions reach.
	"""
	_require_desk_user()
	roles = set(frappe.get_roles())
	is_admin = frappe.session.user == "Administrator"

	method = (method or "").strip()
	if method:
		for row in _load_rows():
			if row["method"] == method and not row.get("guest"):
				runnable = _runnable(row.get("auth"), roles, is_admin)
				return [_public_row(row, runnable, full=True)] if runnable is not None else []
		return []

	limit = min(max(cint(limit) or DEFAULT_LIMIT, 1), MAX_LIMIT)
	module = (module or "").strip().rstrip(".")
	tokens = _words(query or "")
	hits = []
	for row in _load_rows():
		if row.get("guest"):
			continue
		if module and not (row["method"] == module or row["method"].startswith(module + ".")):
			continue
		score = _score(row, tokens) if tokens else 0
		if tokens and not score:
			continue
		runnable = _runnable(row.get("auth"), roles, is_admin)
		if runnable is None:
			continue
		hits.append((-score, row["method"], row, runnable))
	hits.sort(key=lambda hit: hit[:2])
	return [_public_row(row, runnable, full=False) for _s, _m, row, runnable in hits[:limit]]


def _require_desk_user():
	"""Desk (System) users only: a website user has no endpoints to discover here."""
	if frappe.session.user == "Guest" or frappe.get_cached_value(
			"User", frappe.session.user, "user_type") != "System User":
		frappe.throw(_("Only desk users can list POS actions."), frappe.PermissionError)
