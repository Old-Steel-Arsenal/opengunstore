"""Frappe CRM on the POS — install / migrate plumbing.

Owns: the two custom fields (``CUSTOM_FIELDS``), the Lead Mail Settings default, the same-site
ERPNext CRM Settings (on, this site, ``sync_products`` off), moving CRM's own "create lead from
incoming email" switch to ours, the Contact page's Email button, and the one-off backfill that
makes every Item a CRM Product.

Runs at ffl_core's after_install and after_migrate, and when ``crm`` is installed on a site that
already has ffl_core (after_app_install): an existing store gets Frappe CRM with the release that
brought this package (``bench install-app crm`` in deploy/prod/auto_deploy.sh). Without CRM on the
site it does nothing.

Custom fields: ``create_custom_fields`` (module None, not the fixture) — the route setup/custody.py
documents; a fixture would also fail on a site where CRM's DocTypes do not exist yet.

ERPNext CRM Settings is written field by field, not saved: its save (CRM's controller) also
grants Sales User and Sales Manager write / create / delete on Item, which the POS does not
give its counter. The three setup steps of that save we need (CRM's custom fields, the
Quotation "to" option, CRM's Create Quotation script) are called directly.
"""
from __future__ import annotations

import frappe

from ffl_core.crm import TITLE, installed
from ffl_core.crm.deal import SERIAL_FIELD
from ffl_core.crm.mail import AUTO_LEAD, SETTINGS

CRM_SETTINGS = "ERPNext CRM Settings"
SEEDED_FLAG = "ffl_crm_settings_seeded"
BACKFILL_FLAG = "ffl_crm_backfill_done"
BACKFILL_JOB = "ffl_crm_product_backfill"
# CRM's own per-page customization (a CRM Form Script record): the Email button on a Contact page.
CONTACT_SCRIPT = "OpenGunstore: Email a contact"
CONTACT_SCRIPT_JS = """function setupForm({ doc, call, router }) {
	return {
		actions: [{
			label: __("Email"),
			onClick: async () => {
				const deal = await call("ffl_core.api.crm.open_deal", { contact: doc.name })
				router.push({ name: "Deal", params: { dealId: deal }, hash: "#emails" })
			},
		}],
	}
}
"""

CUSTOM_FIELDS = {
	"CRM Products": [{
		"fieldname": SERIAL_FIELD,
		"fieldtype": "Link",
		"options": "Serial No",
		"label": "Gun (serial number)",
		"description": "For a gun priced one by one: the gun this line is for. Blank quotes the cheapest one in stock.",
		"insert_after": "net_amount",
	}],
	"Email Account": [{
		"fieldname": AUTO_LEAD,
		"fieldtype": "Check",
		"default": "0",
		"label": "Create leads from unknown senders",
		"description": "A mail from someone who is not a customer yet becomes a CRM lead; a customer's mail goes on their open deal.",
		"insert_after": "create_lead_from_incoming_email",
	}],
}


def after_install() -> None:
	ensure()


def after_app_install(app: str) -> None:
	"""hooks after_app_install: Frappe CRM installed on a site that already runs the POS. The deploy
	migrates right after (deploy/prod/auto_deploy.sh), so the product backfill waits for that
	migrate's :func:`ensure`: it must not run against a site whose migrate is still going."""
	if app == "crm":
		ensure(backfill=False)


def ensure(backfill: bool = True) -> dict:
	"""after_install / after_migrate / CRM installed: idempotent. Never raises: a thrown hook aborts
	the whole migrate."""
	if not installed():
		return {"skipped": "crm not installed"}
	try:
		_as_administrator(ensure_custom_fields)
		_as_administrator(ensure_settings)
		_as_administrator(ensure_crm_settings)
		_as_administrator(take_over_lead_flags)
		_as_administrator(ensure_contact_script)
		if backfill:
			enqueue_backfill()
	except Exception:
		frappe.log_error(title=f"[{TITLE}] setup.ensure failed", message=frappe.get_traceback())
		return {"error": 1}
	return {"ok": 1}


def ensure_custom_fields() -> None:
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)


def ensure_settings() -> bool:
	"""Once per site: our own domains from the site's Email Domain records. Remembered with a site
	global, so a manager who empties the list later keeps it empty. Waits for the migrate that
	creates the Single (CRM installed on a site still on the code before it)."""
	if frappe.db.get_global(SEEDED_FLAG) or not frappe.db.exists("DocType", SETTINGS):
		return False
	doc = frappe.get_doc(SETTINGS)
	if not (doc.own_domains or "").strip():
		doc.own_domains = "\n".join(sorted(set(frappe.get_all("Email Domain", pluck="domain_name"))))
	doc.save(ignore_permissions=True)  # our own Single, written by install/migrate as Administrator
	frappe.db.set_global(SEEDED_FLAG, "1")
	return True


def _default_company() -> str | None:
	return frappe.defaults.get_global_default("company") \
		or (frappe.get_all("Company", pluck="name", limit=1) or [None])[0]


def ensure_crm_settings() -> dict:
	"""ERPNext CRM Settings on for this site with CRM's own Product → Item push off
	(``sync_products`` 0: products come from Items; ``crm.product`` pushes the edits a
	manager may make). Returns the values it changed."""
	if not frappe.db.exists("DocType", CRM_SETTINGS):
		return {}
	settings = frappe.get_single(CRM_SETTINGS)
	want = {"enabled": 1, "is_erpnext_in_different_site": 0, "sync_products": 0}
	if not settings.erpnext_company:
		want["erpnext_company"] = _default_company()
	changed = {k: v for k, v in want.items() if v is not None and settings.get(k) != v}
	if changed:
		frappe.db.set_single_value(CRM_SETTINGS, dict(changed))   # it adds `modified` to the dict it gets
		settings = frappe.get_single(CRM_SETTINGS)
	# What CRM's save would set up for a same-site integration, minus the Item permission grant.
	settings.create_custom_fields()
	settings.add_quotation_to_option()
	settings.create_crm_form_script()
	frappe.clear_document_cache(CRM_SETTINGS, CRM_SETTINGS)
	return changed


def take_over_lead_flags() -> list[str]:
	"""Every Email Account with CRM's own lead switch on gets ours instead (CRM's would make a
	lead for a customer we already know)."""
	if not frappe.get_meta("Email Account").has_field("create_lead_from_incoming_email"):
		return []
	names = frappe.get_all("Email Account", filters={"create_lead_from_incoming_email": 1}, pluck="name")
	for name in names:
		frappe.db.set_value("Email Account", name, {AUTO_LEAD: 1, "create_lead_from_incoming_email": 0},
			update_modified=False)
	return names


def ensure_contact_script() -> None:
	"""The Contact page's Email button (CRM runs only enabled scripts)."""
	if not frappe.db.exists("DocType", "CRM Form Script"):
		return
	if not frappe.db.exists("CRM Form Script", CONTACT_SCRIPT):
		frappe.get_doc({"doctype": "CRM Form Script", "name": CONTACT_SCRIPT, "dt": "Contact", "view": "Form",
			"script": CONTACT_SCRIPT_JS, "enabled": 1}).insert(ignore_permissions=True)
		return
	current = frappe.db.get_value("CRM Form Script", CONTACT_SCRIPT, ["script", "enabled"], as_dict=True)
	if current.script != CONTACT_SCRIPT_JS or not current.enabled:
		frappe.db.set_value("CRM Form Script", CONTACT_SCRIPT, {"script": CONTACT_SCRIPT_JS, "enabled": 1})


def enqueue_backfill() -> bool:
	"""Once per site, on the long queue: every Item becomes a CRM Product."""
	if frappe.db.get_global(BACKFILL_FLAG):
		return False
	frappe.enqueue("ffl_core.crm.item.backfill_products", queue="long", job_id=BACKFILL_JOB,
		deduplicate=True, enqueue_after_commit=True)
	return True


def _as_administrator(fn):
	prev = frappe.session.user
	frappe.set_user("Administrator")
	try:
		return fn()
	finally:
		frappe.set_user(prev)
