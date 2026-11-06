"""Frappe CRM on the POS — install / migrate plumbing.

Owns: the two custom fields (``CUSTOM_FIELDS``), the Lead Mail Settings default, the team mail
setup (``crm.team.apply``: staff Reply-To, team mailboxes in User Emails), the same-site
ERPNext CRM Settings (on, this site, ``sync_products`` off), CRM's currency, the deal stages, moving CRM's own
"create lead from incoming email" switch to ours, the Contact page's Email button, the Deal page's customer card and
Open order button (:func:`apply_pos_link`, following Lead Mail Settings › crm_pos_link), and the
one-off backfill that makes every Item a CRM Product.

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

import json

import frappe

from ffl_core.crm import TITLE, installed, pos_link_on, team
from ffl_core.crm.customer import CARD_FIELD
from ffl_core.crm.deal import SERIAL_FIELD
from ffl_core.crm.deals import DEAL, STATUS
from ffl_core.crm.mail import AUTO_LEAD, CATEGORY, CATEGORY_FIELD, LEAD, OTHER_MAIL, SETTINGS
from ffl_core.crm.orders import DEAL_FIELD

CRM_SETTINGS = "ERPNext CRM Settings"
FCRM_SETTINGS = "FCRM Settings"
SEEDED_FLAG = "ffl_crm_settings_seeded"
BACKFILL_FLAG = "ffl_crm_backfill_done"
DEAL_STAGES_FLAG = "ffl_crm_deal_stages_done"
# CRM's stock deal stages and what they become. A stage dropped hands its deals to the one named.
RENAMED_STAGES = {"Qualification": "New", "Demo/Making": "Talking", "Proposal/Quotation": "Quoted"}
DROPPED_STAGES = {"Negotiation": "Talking", "Ready to Close": "Quoted"}
DEAL_STAGES = ("New", "Talking", "Quoted", "Won", "Lost")
MAIL_CATEGORIES = ("Automated", "Blocked", "Distributor", "Dealer", "GunBroker", "Marketing", "Notification",
	"Our store", "Vendor", "Other")
CATEGORY_RULES = {"gunbroker.com": "GunBroker", "rsrgroup.com": "Distributor"}
MAIL_SETUP_FLAG = "ffl_crm_other_mail_seeded"
FOLLOW_UPS = "My follow-ups"
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
# The Deal page's customer card and Open order button: a CRM Form Script, on while the switch is.
DEAL_SCRIPT = "OpenGunstore: Deal to POS"
DEAL_SCRIPT_JS = """class CRMDeal {
	onLoad() {
		if (this.doc.__newDocument) return
		this.actions.push({ label: __("Open order"), onClick: () => this.fflOpenOrder() })
		this.fflLoadCard()
	}
	fflError(e) {
		toast.error(e.messages?.[0] || e.message || __("Something went wrong."))
	}
	fflLoadCard() {
		call("ffl_core.api.crm.deal_customer_summary", { deal: this.doc.name })
			.then((summary) => this.setFieldHtml("%(card)s", summary.html))
			.catch((e) => this.fflError(e))
	}
	async fflOpenOrder() {
		try {
			const linked = await call("ffl_core.api.crm.ensure_deal_customer", { deal: this.doc.name })
			if (linked.customer) return await this.fflGoToOrder()
			const choose = (args) => async (close) => {
				close()
				try {
					await call("ffl_core.api.crm.ensure_deal_customer", { deal: this.doc.name, ...args })
					await this.fflGoToOrder()
				} catch (e) {
					this.fflError(e)
				}
			}
			createDialog({
				title: __("Who is the buyer?"),
				html: linked.html,
				actions: [
					...linked.matches.map((m) => ({
						label: m.banned ? __("Use {0} (Banned)", [m.customer_name]) : __("Use {0}", [m.customer_name]),
						variant: "solid",
						theme: m.banned ? "red" : undefined,
						onClick: choose({ customer: m.name }),
					})),
					{
						label: __("Create customer"),
						variant: linked.matches.length ? "outline" : "solid",
						onClick: choose({ create: 1 }),
					},
				],
			})
		} catch (e) {
			this.fflError(e)
		}
	}
	async fflGoToOrder() {
		const order = await call("ffl_core.api.crm.open_deal_order", { deal: this.doc.name })
		window.location.href = order.route
	}
}
""" % {"card": CARD_FIELD}
# Where the card sits on the Deal page: after Contacts, as CRM places its own Lost Reason section.
SIDE_PANEL = f"{DEAL}-Side Panel"
CARD_SECTION = "ffl_customer_section"

CUSTOM_FIELDS = {
	"CRM Deal": [{
		"fieldname": CARD_FIELD,
		"fieldtype": "HTML",
		"label": "Customer card",
		"insert_after": "lead_name",
	}],
	# Data, as CRM's own Quotation.crm_deal: a deal deleted later must never stop a credit note.
	# Not no_copy: a credit note and an amended invoice stay on the deal.
	"Sales Invoice": [{
		"fieldname": DEAL_FIELD,
		"fieldtype": "Data",
		"label": "CRM Deal",
		"read_only": 1,
		"print_hide": 1,
		"search_index": 1,
		"insert_after": "ffl_staff_note",
	}],
	"CRM Products": [{
		"fieldname": SERIAL_FIELD,
		"fieldtype": "Link",
		"options": "Serial No",
		"label": "Gun (serial number)",
		"description": "For a gun priced one by one: the gun this line is for. Blank quotes the cheapest one in stock.",
		"insert_after": "net_amount",
	}],
	"CRM Lead": [{
		"fieldname": CATEGORY_FIELD,
		"fieldtype": "Link",
		"options": CATEGORY,
		"label": "Mail category",
		"insert_after": "status",
		"in_standard_filter": 1,
	}],
	"Email Account": [{
		"fieldname": AUTO_LEAD,
		"fieldtype": "Check",
		"default": "0",
		"label": "Sync business mail to CRM",
		"description": "A staff mailbox: a customer's mail goes on their open deal, a known lead's on the lead, and mail to a team address from someone new becomes a lead. Other mail is not brought in. Team addresses: Lead Mail Settings.",
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
		_as_administrator(ensure_crm_currency)
		_as_administrator(ensure_deal_stages)
		_as_administrator(ensure_other_mail)
		_as_administrator(ensure_crm_views)
		_as_administrator(take_over_lead_flags)
		_as_administrator(ensure_contact_script)
		_as_administrator(apply_pos_link)
		_as_administrator(team.apply)
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


def ensure_crm_currency() -> str | None:
	"""CRM's own currency is the store's. CRM saves its settings at install, so a site that gets CRM
	before its setup wizard keeps whatever Frappe's default was then. A deal in the store's currency
	then asks a public rate API on every insert (CRM Deal ``update_exchange_rate``); with that API
	unreachable CRM refuses the deal, and a customer's mail stays unrouted. Returns the currency set."""
	company = _default_company()
	currency = company and frappe.db.get_value("Company", company, "default_currency")
	if not currency or frappe.db.get_single_value(FCRM_SETTINGS, "currency") == currency:
		return None
	# Not a save: CRM makes the field read-only once set, and its save re-runs its own setup.
	frappe.db.set_single_value(FCRM_SETTINGS, "currency", currency)
	frappe.clear_document_cache(FCRM_SETTINGS, FCRM_SETTINGS)
	return currency


def ensure_deal_stages() -> bool:
	"""Once per site: CRM's stock deal stages become the ones a gun store uses. Remembered with a site
	global, so a stage a manager adds or renames later stays. A stage a site has already replaced is
	left alone."""
	if frappe.db.get_global(DEAL_STAGES_FLAG):
		return False
	moved = {}
	for old, new in RENAMED_STAGES.items():
		if frappe.db.exists(STATUS, old) and not frappe.db.exists(STATUS, new):
			frappe.rename_doc(STATUS, old, new, force=True)
			moved[old] = new
	for old, new in DROPPED_STAGES.items():
		if frappe.db.exists(STATUS, old) and frappe.db.exists(STATUS, new):
			frappe.db.set_value(DEAL, {"status": old}, "status", new)
			frappe.delete_doc(STATUS, old, ignore_permissions=True)
			moved[old] = new
	for position, stage in enumerate(DEAL_STAGES, 1):
		if frappe.db.exists(STATUS, stage):
			frappe.db.set_value(STATUS, stage, "position", position)
	_move_stages_in_saved_views(moved)
	frappe.db.set_global(DEAL_STAGES_FLAG, "1")
	return True


def _move_stages_in_saved_views(moved: dict) -> None:
	"""A saved Deal view keeps stage names as JSON, in its filter and its kanban columns, where a
	rename does not reach them."""
	def swap(value):
		return [swap(v) for v in value] if isinstance(value, list) else moved.get(value, value)

	views = frappe.get_all("CRM View Settings", filters={"dt": DEAL}, fields=["name", "filters", "kanban_columns"])
	for view in views:
		filters = json.loads(view.filters or "{}")
		if "status" in filters:
			filters["status"] = swap(filters["status"])
		columns = {}   # two old stages can land on one new stage: its column appears once
		for column in json.loads(view.kanban_columns or "[]"):
			column["name"] = swap(column.get("name"))
			columns.setdefault(column["name"], column)
		frappe.db.set_value("CRM View Settings", view.name, {"filters": json.dumps(filters),
			"kanban_columns": json.dumps(list(columns.values()))}, update_modified=False)


def ensure_other_mail() -> None:
	"""The Other mail status always (mail depends on it); the categories and first rules once per site,
	so a manager who removes one keeps it removed."""
	if not frappe.db.exists("CRM Lead Status", OTHER_MAIL):
		frappe.get_doc({"doctype": "CRM Lead Status", "lead_status": OTHER_MAIL, "type": "On Hold", "color": "gray",
			"position": 99}).insert(ignore_permissions=True)
	if frappe.db.get_global(MAIL_SETUP_FLAG) or not frappe.db.exists("DocType", CATEGORY):
		return
	for name in MAIL_CATEGORIES:
		if not frappe.db.exists(CATEGORY, name):
			frappe.get_doc({"doctype": CATEGORY, "category_name": name}).insert(ignore_permissions=True)
	settings = frappe.get_doc(SETTINGS)
	have = {rule.domain for rule in settings.get("category_rules") or []}
	for domain, category in CATEGORY_RULES.items():
		if domain not in have:
			settings.append("category_rules", {"domain": domain, "category": category})
	settings.save(ignore_permissions=True)   # our own Single, written by install/migrate as Administrator
	frappe.db.set_global(MAIL_SETUP_FLAG, "1")


def ensure_crm_views() -> None:
	"""The shared views staff work from (CRM's sidebar lists public views). Made once: a manager may change them."""
	from crm.fcrm.doctype.crm_lead.crm_lead import CRMLead

	open_statuses = lambda doctype: frappe.get_all(doctype, filters={"type": ["not in", ["Won", "Lost"]],  # noqa: E731
		"name": ["!=", OTHER_MAIL]}, pluck="name", order_by="position asc")
	columns = CRMLead.default_list_data()
	_ensure_view(LEAD, "Leads", OTHER_MAIL, {"status": OTHER_MAIL},
		columns=[{"label": "Mail category", "type": "Link", "key": CATEGORY_FIELD, "options": CATEGORY, "width": "10rem"},
			*columns["columns"]], rows=[*columns["rows"], CATEGORY_FIELD])
	_ensure_view(LEAD, "Leads", FOLLOW_UPS, {"lead_owner": ["in", ["@me", ""]], "communication_status": "Open",
		"status": ["in", open_statuses("CRM Lead Status")]}, order_by="modified asc")
	_ensure_view(DEAL, "Deals", FOLLOW_UPS, {"deal_owner": ["in", ["@me", ""]], "communication_status": "Open",
		"status": ["in", open_statuses(STATUS)]}, order_by="modified asc")


def _ensure_view(doctype: str, route: str, label: str, filters: dict, order_by: str = "modified desc",
		columns: list | None = None, rows: list | None = None) -> None:
	found = {"dt": doctype, "label": label, "public": 1}
	if frappe.db.exists("CRM View Settings", found):
		return
	view = frappe.get_doc({"doctype": "CRM View Settings", "dt": doctype, "label": label, "type": "list", "public": 1,
		"route_name": route, "filters": json.dumps(filters), "order_by": order_by,
		"load_default_columns": 0 if columns else 1, "columns": json.dumps(columns or []), "rows": json.dumps(rows or [])})
	view.insert(ignore_permissions=True)
	frappe.db.set_value("CRM View Settings", view.name, "user", "")   # shared views have this, not NULL (CRM's get_views)


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


def apply_pos_link() -> None:
	"""The Deal page follows Lead Mail Settings › crm_pos_link: the script runs and the card's section
	shows only while it is on. Migrate and that page's save, by whoever may change the settings."""
	if not frappe.db.exists("DocType", "CRM Form Script"):
		return
	on = pos_link_on()
	want = {"script": DEAL_SCRIPT_JS, "enabled": 1 if on else 0}
	if not frappe.db.exists("CRM Form Script", DEAL_SCRIPT):
		frappe.get_doc({"doctype": "CRM Form Script", "name": DEAL_SCRIPT, "dt": DEAL, "view": "Form", **want}
			).insert(ignore_permissions=True)
	elif frappe.db.get_value("CRM Form Script", DEAL_SCRIPT, list(want), as_dict=True) != want:
		frappe.db.set_value("CRM Form Script", DEAL_SCRIPT, want)
	_place_card_section(on)


def _place_card_section(on: bool) -> None:
	if not frappe.db.exists("CRM Fields Layout", SIDE_PANEL):
		return
	layout = frappe.get_doc("CRM Fields Layout", SIDE_PANEL)
	sections = json.loads(layout.layout or "[]")
	present = any(section.get("name") == CARD_SECTION for section in sections)
	if on and not present:
		at = 1 if sections and sections[0].get("name") == "contacts_section" else 0
		sections.insert(at, {"name": CARD_SECTION, "label": "Customer", "opened": True,
			"columns": [{"name": "ffl_customer_column", "fields": [CARD_FIELD]}]})
	elif not on and present:
		sections = [section for section in sections if section.get("name") != CARD_SECTION]
	else:
		return
	layout.layout = json.dumps(sections)
	layout.save(ignore_permissions=True)


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
