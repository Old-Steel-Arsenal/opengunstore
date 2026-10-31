"""Shared helpers for ffl_core API endpoints."""
from __future__ import annotations

import json

import frappe
from frappe import _


def coerce_payload(payload) -> dict:
	"""A whitelisted endpoint's payload: a dict, or the JSON string the browser
	sends. Throws on anything that is not a JSON object."""
	if isinstance(payload, str):
		try:
			payload = json.loads(payload)
		except json.JSONDecodeError:
			frappe.throw(_("Invalid payload (not JSON)"))
	if not isinstance(payload, dict):
		frappe.throw(_("Invalid payload"))
	return payload


def default_warehouse(*, throw_if_missing: bool = False) -> str | None:
	"""The user's default warehouse, via the canonical resolver.

	Delegates to ``ffl_core.warehouses.default_stock_warehouse``.
	This function used to carry its own three-tier fallback — POS Profile, then
	ANY non-group warehouse on the company, then any warehouse anywhere — with
	no ordering and no exclusions. That was survivable while every warehouse was
	sellable. It stopped being survivable the moment a customer-custody
	warehouse existed: an ordinary purchase could be routed into the one
	location every sellable-stock query deliberately ignores, and the goods
	would simply vanish from the storefront and the POS pickers.

	``warehouses.py`` describes itself as "the single, safe replacement all
	three call sites now delegate to" — this was the fourth call site, and the
	one Receive Goods actually uses. It excludes custody, consignment, Transit
	and disabled warehouses, prefers an explicitly configured answer, and orders
	deterministically instead of taking whatever row the table returns first.

	Returns None when nothing resolves (or raises with `throw_if_missing=True` —
	Receive Goods cannot proceed without a warehouse)."""
	from ffl_core.warehouses import default_stock_warehouse

	company = frappe.defaults.get_user_default("Company") \
		or frappe.db.get_single_value("Global Defaults", "default_company")
	warehouse = default_stock_warehouse(company) if company else None
	if not warehouse and throw_if_missing:
		frappe.throw(_("No warehouse configured. Set up a Warehouse first."))
	return warehouse
