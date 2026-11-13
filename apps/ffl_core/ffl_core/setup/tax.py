"""Idempotent setup for non-taxable service items.

Services — FFL transfer fees, gunsmithing labor, etc. — are not subject to
sales tax. ERPNext models a per-line exemption with an **Item Tax Template**
that sets the relevant tax account(s) to 0% for the item; that rate overrides
the Sales Taxes and Charges template's flat rate for those lines only (goods
still tax normally). See erpnext.stock.get_item_details.get_item_tax_template,
which resolves a line's template from the Item, then walks up its Item Group
tree — so a template set as the "Service" group default exempts every service
item in the group automatically (gunsmithing, transfer, …).

This wires both belt and suspenders:
  - a per-company "Non-Taxable Service" Item Tax Template (0% on every account
    head the company's default Sales Taxes and Charges template charges),
  - set as the default tax of the "Service" Item Group (group fallback), AND
  - attached explicitly to the TRANSFER-FEE item, which is moved into "Service".

Idempotent; wired into hooks.after_install / after_migrate AFTER
setup.item_groups.ensure (so the "Service" group exists). A fresh site with no
sales tax configured just gets the group; the template is created once the
company has a default sales-tax template (e.g. on the live site, applied by the
deploy's `bench migrate`).
"""
from __future__ import annotations

import frappe

from ffl_core.utils import commit
from ffl_core.setup.item_groups import ensure as ensure_item_groups

SERVICE_GROUP = "Service"
EXEMPT_TEMPLATE_TITLE = "Non-Taxable Service"


def ensure() -> dict:
	"""Create/repair the service tax-exemption wiring. Idempotent, and resilient:
	runs in after_migrate, so it must never raise (a thrown hook aborts the whole
	`bench migrate`) — per-step failures are logged and skipped."""
	created = {"templates": 0, "templates_synced": 0, "group_links": 0,
		"transfer_fee_moved": 0, "transfer_fee_links": 0, "trade_in_credit_links": 0}
	# The "Service" group lives in the canonical taxonomy; make sure it exists
	# even if this runs before item_groups.ensure in some path.
	if not frappe.db.exists("Item Group", SERVICE_GROUP):
		ensure_item_groups()
	created["dangling_links"] = _prune_dangling_template_links()
	templates = _ensure_exempt_templates(created)  # {company: item_tax_template_name}
	try:
		for name in templates.values():
			if _attach_template_to_group(name):
				created["group_links"] += 1
		_wire_transfer_fee(templates, created)
		# The trade-in credit line must NOT be exempt: it participates in the
		# tax base so a trade-in allowance reduces the taxable amount — even
		# trade → $0 tax (TX §151.007(c)(3);
		# reversing the earlier full-price treatment). Strip the exempt
		# links the old wiring attached (prod self-heals on migrate); the
		# counter now counts REMOVED links, 0 at steady state.
		created["trade_in_credit_links"] = _unwire_trade_in_credit(templates)
	except Exception:
		frappe.log_error(
			title="tax.ensure: group / transfer-fee wiring failed",
			message=frappe.get_traceback(),
		)
	created["oos_templates"] = _ensure_out_of_state_templates()
	commit()
	return created


def _ensure_out_of_state_templates() -> int:
	"""One empty (0-tax) "Out of State Sales" Sales Taxes and Charges Template
	per company that charges sales tax. Document-level counterpart of the
	per-line service exemption: compliance/interstate_tax.py switches a
	shipped-out-of-state order onto it. NEVER is_default (that would unseat
	the 8.25% default template and break the POS fallback). Idempotent,
	never raises (runs in after_migrate)."""
	created = 0
	from ffl_core.compliance.interstate_tax import OUT_OF_STATE_TEMPLATE_TITLE
	for company in frappe.get_all("Company", pluck="name"):
		try:
			if not _company_sales_tax_accounts(company):
				continue  # company charges no sales tax → nothing to exempt
			if frappe.db.exists("Sales Taxes and Charges Template",
					{"company": company, "title": OUT_OF_STATE_TEMPLATE_TITLE}):
				continue
			frappe.get_doc({
				"doctype": "Sales Taxes and Charges Template",
				"title": OUT_OF_STATE_TEMPLATE_TITLE,
				"company": company,
				"is_default": 0,
				"taxes": [],
			}).insert(ignore_permissions=True)
			created += 1
		except Exception:
			frappe.log_error(
				title="tax.ensure: out-of-state template failed",
				message=f"company={company}\n{frappe.get_traceback()}",
			)
	return created


def _company_sales_tax_accounts(company: str) -> list[str]:
	"""Distinct account heads charged by the company's default sales-tax template."""
	tmpl = frappe.db.get_value(
		"Sales Taxes and Charges Template",
		{"company": company, "is_default": 1, "disabled": 0},
		"name",
	)
	if not tmpl:
		return []
	rows = frappe.get_all(
		"Sales Taxes and Charges",
		filters={"parent": tmpl, "parenttype": "Sales Taxes and Charges Template"},
		pluck="account_head",
	)
	# preserve order, drop blanks + dupes
	return list(dict.fromkeys(a for a in rows if a))


# The account store_setup makes for California's firearm and ammunition excise tax.
EXCISE_ACCOUNT_NAME = "CA Firearm Excise Tax Payable"


def sales_tax_and_excise_accounts(company: str) -> tuple[list[str], list[str]]:
	"""(sales-tax accounts, excise accounts) of *company*, for reporting what was collected.

	The excise accounts are FFL Settings' peace-officer exempt account (when it is this
	company's) and any account of the default template named EXCISE_ACCOUNT_NAME; the sales-tax
	accounts are the default template's accounts less those. Reporting only:
	`_company_sales_tax_accounts` stays the full list, because the 0% service template and the
	freight-tax mirror must cover the excise account too.
	# lazy: an excise account is known by store_setup's account name; one made by hand or renamed
	# is known only while FFL Settings' peace-officer account points at it (otherwise it reports as
	# sales tax); upgrade path: an "excise" check on Account, or the template rows' own flag."""
	accounts = _company_sales_tax_accounts(company)
	if not accounts:
		return [], []
	from ffl_core.compliance.le_excise_exempt import SETTING
	from ffl_core.utils import single_raw

	names = dict(frappe.get_all("Account", filters={"name": ["in", accounts]},
		fields=["name", "account_name"], as_list=True))
	excise = {a for a in accounts if names.get(a) == EXCISE_ACCOUNT_NAME}
	setting = single_raw("FFL Settings", SETTING)
	if setting and frappe.db.get_value("Account", setting, "company") == company:
		excise.add(setting)
	return [a for a in accounts if a not in excise], sorted(excise)


# lazy: 25 is a ceiling, not a tuning knob. Steady state is 0 and a retired company
# leaves exactly 2 (its Service-group link and its TRANSFER-FEE link); anything in
# the hundreds is templates renamed or dropped without their child links, which is
# not this function's to repair. Raise it only with the log in hand.
_PRUNE_CEILING = 25


def _prune_dangling_template_links() -> int:
	"""Delete Item Tax rows whose Item Tax Template no longer exists.

	The wiring below only ever ADDS links: `_attach_template_to_group` puts one row
	per company on the ONE global "Service" Item Group, and `_wire_transfer_fee`
	does the same on the one global TRANSFER-FEE item. Nothing prunes a row when
	its template goes away — a second company retired, an abbr changed (the
	template is named after it), or the way this was actually found: a company
	that only ever existed inside a test transaction, whose template rolled back
	while the link on the committed global row did not.

	A dead link is not cosmetic. ERPNext's per-line resolver reads every row with
	`frappe.get_cached_value("Item Tax Template", row.item_tax_template,
	["disabled", "company"])` and unpacks the answer without checking it
	(erpnext/stock/get_item_details.py::_get_item_tax_template), so a missing
	template raises `TypeError: cannot unpack non-iterable NoneType object` from
	inside ERPNext, naming neither the row nor the item. Every new Item in the
	group inherits the dead row on insert, so ONE orphan takes out every service
	line and transfer fee on the site — 17 test failures
	across four modules, and three investigations that never looked at the row.

	Deleting is the only repair available: a row pointing at a template that does
	not exist cannot be resolved by anything, so nothing can lose behaviour it
	still had. Site-wide rather than only our two parents — the resolver does not
	care which Item carries the orphan, and healing just the rows we happen to
	have written would leave the same crash one Item away. Scoped to the two
	doctypes that embed this child table (Item, Item Group), and refused outright
	above ``_PRUNE_CEILING``: a prod migrate that finds hundreds of orphans has hit
	a template rename/drop, and a site-wide DELETE would then take every
	operator-set per-item override with it. In that case nothing is removed, the
	Error Log names every row WITH the template it pointed at, and the resolver
	stays loudly broken until a human repairs the templates.

	Returns the number of rows removed (0 at steady state, and 0 when refused).
	"""
	try:
		orphans = frappe.db.sql("""
			SELECT it.name, it.parenttype, it.parent, it.item_tax_template
			FROM `tabItem Tax` it
			LEFT JOIN `tabItem Tax Template` itt ON itt.name = it.item_tax_template
			WHERE COALESCE(it.item_tax_template, '') != '' AND itt.name IS NULL
			  AND it.parenttype IN ('Item', 'Item Group')
		""", as_dict=True)
		if not orphans:
			return 0
		lines = "\n".join(
			f"{r.parenttype} {r.parent}: {r.name} -> {r.item_tax_template}" for r in orphans)
		if len(orphans) > _PRUNE_CEILING:
			frappe.log_error(
				title=f"tax.ensure: {len(orphans)} dangling Item Tax links — NOT pruned",
				message=(f"More than {_PRUNE_CEILING} Item Tax rows point at templates that "
					"do not exist. That is not a retired company; it is templates renamed "
					"or dropped without their child links being rewritten. Deleting "
					"site-wide would take every operator-set per-item tax override with "
					"them, so NOTHING was removed. Repair the templates (or the links) and "
					f"re-run migrate.\n{lines}"),
			)
			return 0
		frappe.db.delete("Item Tax", {"name": ("in", [r.name for r in orphans])})
		# The parents are cached as whole documents, child rows included, so a
		# direct child delete leaves the orphan visible to the very resolver this
		# exists to unblock until the cache turns over.
		for parenttype, parent in {(r.parenttype, r.parent) for r in orphans}:
			if parenttype and parent:
				frappe.clear_document_cache(parenttype, parent)
		frappe.log_error(
			title="tax.ensure: removed dangling Item Tax Template links",
			message=lines,
		)
		return len(orphans)
	except Exception:
		# after_migrate: a failed repair must not abort the migrate.
		frappe.log_error(title="tax.ensure: dangling-link prune failed",
			message=frappe.get_traceback())
		return 0


def _ensure_exempt_templates(created: dict) -> dict:
	"""One 0% Item Tax Template per company that charges sales tax, with its rows
	kept in sync with the company's CURRENT default-template accounts.

	Zeroes EVERY account head the company's default sales-tax template uses, so
	the exemption holds no matter how many tax components (state + city, …) it
	splits into — and RECONCILES on every run, so the exemption never silently
	goes stale when the store later changes its sales-tax account(s). Returns
	{company: template_name}.

	Per-company failures (e.g. a tax account ERPNext refuses on an Item Tax
	Template because it isn't typed Tax/Chargeable/Income/Expense) are logged and
	skipped, so one mis-configured company can't abort an after_migrate run."""
	out: dict[str, str] = {}
	for company in frappe.get_all("Company", pluck="name"):
		try:
			accounts = _company_sales_tax_accounts(company)
			if not accounts:
				continue  # company charges no sales tax → nothing to exempt
			abbr = frappe.get_cached_value("Company", company, "abbr")
			name = f"{EXEMPT_TEMPLATE_TITLE} - {abbr}"  # ITT autoname = "{title} - {abbr}"
			if frappe.db.exists("Item Tax Template", name):
				if _sync_template_rows(name, accounts):
					created["templates_synced"] += 1
			else:
				frappe.get_doc({
					"doctype": "Item Tax Template",
					"title": EXEMPT_TEMPLATE_TITLE,
					"company": company,
					"taxes": [{"tax_type": acct, "tax_rate": 0} for acct in accounts],
				}).insert(ignore_permissions=True)
				created["templates"] += 1
			out[company] = name
		except Exception:
			frappe.log_error(
				title="tax.ensure: exempt template failed",
				message=f"company={company}\n{frappe.get_traceback()}",
			)
	return out


def _sync_template_rows(name: str, accounts: list[str]) -> bool:
	"""Make the exempt template's rows exactly {account: 0%} for the CURRENT
	accounts, healing drift when the company's sales-tax accounts change after the
	template was first built (otherwise the 0% sits on an account the template no
	longer charges, and service lines get taxed on the new one). Returns True iff
	it rewrote the rows."""
	doc = frappe.get_doc("Item Tax Template", name)
	have = {r.tax_type: float(r.tax_rate) for r in doc.taxes}
	want = {a: 0.0 for a in accounts}
	if have == want:
		return False
	doc.set("taxes", [{"tax_type": a, "tax_rate": 0} for a in accounts])
	doc.save(ignore_permissions=True)
	return True


def _attach_template_to_group(template_name: str) -> bool:
	"""Set `template_name` as a default tax of the Service group (idempotent)."""
	group = frappe.get_doc("Item Group", SERVICE_GROUP)
	if any(t.item_tax_template == template_name for t in (group.taxes or [])):
		return False
	group.append("taxes", {"item_tax_template": template_name})
	group.save(ignore_permissions=True)
	return True


def _wire_transfer_fee(templates: dict, created: dict) -> None:
	"""Move TRANSFER-FEE into the Service group and tag it with each company's
	exempt template. No-op if the item hasn't been created yet (it's minted
	lazily on first transfer, already into the Service group — see
	setup.transfer_fee — so it inherits the group default until this re-runs)."""
	from ffl_core.setup.transfer_fee import TRANSFER_FEE_ITEM

	if not frappe.db.exists("Item", TRANSFER_FEE_ITEM):
		return
	item = frappe.get_doc("Item", TRANSFER_FEE_ITEM)
	changed = False
	if item.item_group != SERVICE_GROUP:
		item.item_group = SERVICE_GROUP
		created["transfer_fee_moved"] = 1
		changed = True
	existing = {t.item_tax_template for t in (item.taxes or [])}
	for name in templates.values():
		if name not in existing:
			item.append("taxes", {"item_tax_template": name})
			created["transfer_fee_links"] += 1
			changed = True
	if changed:
		item.save(ignore_permissions=True)


def _exempt_templates_by_company() -> dict:
	"""{company: '<Non-Taxable Service> - <abbr>'} for companies whose exempt
	Item Tax Template already exists (created by _ensure_exempt_templates). Lets
	_wire_trade_in_credit run standalone (from trade_in_credit.ensure) without
	the caller having to pass the templates dict."""
	out: dict[str, str] = {}
	for company in frappe.get_all("Company", pluck="name"):
		abbr = frappe.get_cached_value("Company", company, "abbr")
		name = f"{EXEMPT_TEMPLATE_TITLE} - {abbr}"
		if frappe.db.exists("Item Tax Template", name):
			out[company] = name
	return out


def _unwire_trade_in_credit(templates: dict | None = None) -> int:
	"""Ensure TRADE-IN-CREDIT carries NO exempt Item Tax Template: with no
	item-level override the credit line follows the invoice's own Sales Taxes
	and Charges template, so its negative amount reduces the tax base and a
	trade-in is taxed on the DIFFERENCE (even trade → $0 tax), per TX Tax Code
	§151.007(c)(3) — this reverses the earlier
	full-price determination that _wire_trade_in_credit implemented. Removing
	(not just no longer adding) matters: prod carries the old links, and this
	runs in after_migrate so the deploy self-heals. No-op until the item exists
	(minted lazily on first trade-in). Idempotent; returns the count of removed
	template links (0 at steady state). `templates` is passed from ensure() or
	looked up when called standalone."""
	from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM

	if not frappe.db.exists("Item", TRADE_IN_CREDIT_ITEM):
		return 0
	if templates is None:
		templates = _exempt_templates_by_company()
	if not templates:
		return 0
	exempt = set(templates.values())
	item = frappe.get_doc("Item", TRADE_IN_CREDIT_ITEM)
	keep = [t for t in (item.taxes or []) if t.item_tax_template not in exempt]
	n = len(item.taxes or []) - len(keep)
	if n:
		item.taxes = keep
		item.save(ignore_permissions=True)
	return n
