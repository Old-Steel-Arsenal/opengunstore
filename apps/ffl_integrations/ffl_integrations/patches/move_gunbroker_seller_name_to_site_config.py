"""One-shot — move the GunBroker Seller name off the Settings form into site config.

The name is the one the DevKey was registered under, so it belongs with the key
(see move_gunbroker_dev_keys_to_site_config): the client now reads
``gunbroker_seller_name`` from ``frappe.conf``. A store's operators had nothing
to choose in that field, only a way to break the live connection.

Same rules as the DevKey move: the form's value is copied into site_config.json
unless site config already carries one (the explicit setting wins), and the
form's row is deleted either way. Runs after the doctype sync, so the field is
gone from the meta and the value is read straight from ``tabSingles``.
"""
from __future__ import annotations

import frappe
from frappe.installer import update_site_config

SETTINGS = "GunBroker Settings"
FIELD = "seller_name"
CONF_KEY = "gunbroker_seller_name"


def execute(site_config_path: str | None = None) -> str:
	"""Returns "moved" | "kept" | "absent"."""
	row = frappe.db.sql(
		"select value from `tabSingles` where doctype = %s and field = %s", (SETTINGS, FIELD))
	stored = ((row[0][0] if row else "") or "").strip()
	if not stored:
		outcome = "absent"
	elif frappe.conf.get(CONF_KEY):
		outcome = "kept"
	else:
		update_site_config(CONF_KEY, stored, site_config_path=site_config_path)
		outcome = "moved"
	frappe.db.delete("Singles", {"doctype": SETTINGS, "field": FIELD})
	return outcome
