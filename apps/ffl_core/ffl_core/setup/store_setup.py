"""Store setup — converge ONE site to ONE store's facts, idempotently.

This used to live as a separate per-store script (410 lines, no
tests, one store's own facts baked in as defaults). It moved here so it ships with the
image, runs under ``make test`` on the shared dev site, and serves every store
the same way: the store repo keeps only ``store.env`` and one call.

    bench --site <site> execute ffl_core.setup.store_setup.run
        # facts come from the environment (docker exec -e KEY=VALUE ...)
    store_setup.run(config_dict)          # tests / callers with a dict

What it converges (all from ``config``; nothing about any store is hard-coded):
  accounts     sales-tax payable, optional excise (CFET) payable, bank, card clearing,
               and the cash drawer's accounts (Cash Over/Short, Meals). Parents are found
               by what they are, so both of ERPNext's standard charts work ("Standard"
               and "Standard with Numbers"); a site that already had the account is untouched.
  stock        Stock Settings -> "Activate Serial / Batch No for Item" (a fresh ERPNext
               site has it off, and a serialized firearm cannot be stocked without it)
  compliance   optional FORM_4473_MODE: "paper" (FFL Settings Paper 4473 Mode on, FastBound
               off) or "fastbound" (the reverse). Blank leaves both switches as they are,
               which on a new site means FastBound is required until it is configured.
  taxes        default Sales Taxes and Charges Template; Item Tax Template on the
               Firearm + Ammunition groups; ffl_core's service exemption re-synced;
               ERPNext's generic "US ST" seed removed
  tenders      Mode of Payment -> default account per company
  masters      walk-in customer, Selling Settings defaults
  branding     Letter Head (name/address/phone/email/licences/logo/website) as the
               site default; Company logo/email/phone/website; the company Address
               (is_your_company_address) the receipts print; FFL Settings receipt
               footer + consignment contact; System Settings email footer; Website
               Settings app name / logo / splash; Navbar logo
  mail         the outgoing Email Account (display name = store name), sender flags
  POS          POS Profile named after the abbr: tax template, letter head, print
               format "POS Receipt", company address, tenders, users

Idempotency — read this before re-running on a live store:
  re-asserted EVERY run (store.env is the source of truth; Desk edits to these are
  overwritten): tax template rows, Item Tax Template rows, Letter Head content +
  default, Company email/phone/website/logo, FFL Settings receipt fields, System /
  Website / Navbar branding fields, Email Account sender flags, POS Profile tax
  template / letter head / print format / company address.
  created ONCE, then left alone: company Address (unless every one is disabled),
  Email Account (credential rotation happens in Desk), walk-in Customer, POS
  Profile tenders and users, accounts.
  a blank optional value means "omit" (no CFD line, no logo, no footer) — it never
  falls back to another store's value. Required keys missing, a wrong abbreviation or an
  unknown mode -> throw before anything is written. A later failure may leave part of the
  setup written (tax.ensure and the payment-method steps commit on their own); every step
  is idempotent, so fix the cause and run it again.
"""
from __future__ import annotations

import json
import os

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt

from ffl_core.utils import commit, single_flag

MARKER = "STORE_SETUP COMMITTED"

REQUIRED = (
	"COMPANY_NAME", "COMPANY_ABBR", "OWNER_EMAIL",
	"ADDRESS_LINE1", "CITY", "STATE", "PINCODE", "FFL_NUMBER",
	"SALES_TAX_RATE", "SALES_TAX_TITLE", "ITT_TITLE",
)
OPTIONAL = {
	"STORE_PHONE": "", "STORE_WEBSITE": "", "ADDRESS_LINE2": "", "COUNTRY": "United States",
	"CFD_NUMBER": "", "LOGO_URL": "", "RECEIPT_FOOTER_NOTE": "",
	"CFET_RATE": "0", "FOREIGN_LETTER_HEADS": "",
	"BANK_ACCOUNT_NAME": "Bank Account", "CARD_CLEARING_ACCOUNT_NAME": "Card Clearing",
	"WALK_IN_CUSTOMER": "Walk-in Customer",
	"SMTP_SERVER": "", "SMTP_PORT": "587", "SMTP_LOGIN": "", "SMTP_SENDER": "",
	"SMTP_ACCOUNT_NAME": "", "SMTP_PASSWORD": "",
	"FORM_4473_MODE": "",
}
KEYS = REQUIRED + tuple(OPTIONAL)

# ERPNext's generic US sales-tax seed — unused on a store with its own template.
_US_SEED = ("US ST 6%", "US ST 4%", "US ST 6.25%")
_US_SEED_ACCOUNTS = ("ST 6%", "ST 4%", "ST 6.25%")


def log(*parts) -> None:
	print("STORE_SETUP", *parts)


def config_from_env() -> dict:
	"""Every known key from the environment (missing -> "")."""
	return {k: os.environ.get(k, "") for k in KEYS}


def _normalize(config: dict | None) -> dict:
	raw = config_from_env() if config is None else dict(config)
	c = {}
	for k in KEYS:
		v = raw.get(k)
		v = "" if v is None else str(v).strip()
		c[k] = v if v else OPTIONAL.get(k, "")
	missing = [k for k in REQUIRED if not c.get(k)]
	if missing:
		frappe.throw(
			_("store.env is missing {0} — refusing to fall back to another store's facts.").format(
				", ".join(missing)),
			frappe.ValidationError,
		)
	c["SALES_TAX_RATE"] = flt(c["SALES_TAX_RATE"])
	c["CFET_RATE"] = flt(c["CFET_RATE"])
	c["SMTP_PORT"] = cint(c["SMTP_PORT"]) or 587
	c["SMTP_SENDER"] = c["SMTP_SENDER"] or c["OWNER_EMAIL"]
	c["SMTP_ACCOUNT_NAME"] = c["SMTP_ACCOUNT_NAME"] or c["COMPANY_NAME"]
	c["FORM_4473_MODE"] = c["FORM_4473_MODE"].lower()
	if c["FORM_4473_MODE"] not in ("", "fastbound", "paper"):
		frappe.throw(_("FORM_4473_MODE must be blank, fastbound or paper, not {0}.").format(c["FORM_4473_MODE"]),
			frappe.ValidationError)
	c["FOREIGN_LETTER_HEADS"] = [s.strip() for s in c["FOREIGN_LETTER_HEADS"].split(",") if s.strip()]
	c["ADDRESS_ONE_LINE"] = ", ".join(p for p in [
		c["ADDRESS_LINE1"], c["ADDRESS_LINE2"],
		" ".join(p for p in [f"{c['CITY']},", c["STATE"], c["PINCODE"]] if p),
	] if p)
	return c


def _preflight(c: dict) -> None:
	"""Refuse early, before anything is written."""
	company = c["COMPANY_NAME"]
	if not frappe.db.exists("Company", company):
		frappe.throw(_("Company {0} does not exist — run the setup wizard first.").format(company))
	abbr = frappe.db.get_value("Company", company, "abbr")
	if abbr != c["COMPANY_ABBR"]:
		frappe.throw(_("COMPANY_ABBR is {0} but Company {1} has abbr {2}.").format(
			c["COMPANY_ABBR"], company, abbr))
	# The accounts the run reads but does not create: refuse here, before anything is written.
	from ffl_core.cash_drawer import cash_account

	cash_account(company)
	if not frappe.db.exists("POS Profile", abbr):  # only a new POS Profile needs the other three
		for name, field in (("Write Off", "write_off_account"), ("Sales", "default_income_account"),
				("Cost of Goods Sold", "default_expense_account")):
			_account_or_default(c, name, field)
	meta = frappe.get_meta("FFL Settings")
	for f in ("receipt_footer_note",):
		if not meta.has_field(f):
			# Older image: the receipt reads these; silently skipping them
			# is how a store ends up printing without a footer and nobody notices.
			frappe.throw(_("FFL Settings has no field {0}: this image predates the multi-store "
				"branding change — deploy a newer tag.").format(f))


# --- 1. accounts -------------------------------------------------------------
# Parents whose kind is unambiguous in ERPNext's charts: (account_type, root_type) of the group.
_PARENT_KIND = {"Duties and Taxes": ("Tax", "Liability"), "Bank Accounts": ("Bank", "Asset")}


def _parent_account(c: dict, parent: str) -> str:
	"""The company's real group account for ``parent``. "Standard with Numbers" names it
	"2300 - Duties and Taxes - XX", not "Duties and Taxes - XX", so the name is looked up by
	``account_name`` and, for the tax and bank groups, by what they are."""
	company = c["COMPANY_NAME"]
	plain = f"{parent} - {c['COMPANY_ABBR']}"
	if frappe.db.exists("Account", plain):
		return plain
	group = {"company": company, "is_group": 1, "disabled": 0}
	found = frappe.db.get_value("Account", {**group, "account_name": parent}, "name")
	if not found and parent in _PARENT_KIND:
		account_type, root_type = _PARENT_KIND[parent]
		found = frappe.db.get_value(
			"Account", {**group, "account_type": account_type, "root_type": root_type}, "name", order_by="lft")
	if not found:
		frappe.throw(_("{0} has no group account \"{1}\" to file a new account under. Use the "
			"\"Standard\" or \"Standard with Numbers\" chart of accounts, or create that group first.").format(
			company, parent))
	return found


def _account_or_default(c: dict, name: str, company_field: str) -> str:
	"""``<name> - <abbr>`` when the company has it (every chart without account numbers), else the
	account the company itself names as its default for that role (numbered charts). Neither exists:
	refuse with both names, rather than hand back one that is not there."""
	plain = f"{name} - {c['COMPANY_ABBR']}"
	if frappe.db.exists("Account", plain):
		return plain
	default = frappe.db.get_value("Company", c["COMPANY_NAME"], company_field)
	if default and frappe.db.exists("Account", default):
		return default
	frappe.throw(_("{0} has no account \"{1}\" and no {2} set on the Company. Create the account or set "
		"that default (Company > Accounts), then run the setup again.").format(
		c["COMPANY_NAME"], plain, company_field))


def _ensure_account(c: dict, name: str, parent: str, account_type: str) -> str:
	abbr = c["COMPANY_ABBR"]
	full = f"{name} - {abbr}"
	if frappe.db.exists("Account", full):
		return full
	frappe.get_doc({
		"doctype": "Account", "account_name": name, "company": c["COMPANY_NAME"], "is_group": 0,
		"parent_account": _parent_account(c, parent), "account_type": account_type,
		"account_currency": "USD",
	}).insert(ignore_permissions=True)
	log("account created", full)
	return full


def _accounts(c: dict) -> dict:
	a = {
		"sales_tax": _ensure_account(c, "Sales Tax Payable", "Duties and Taxes", "Tax"),
		"bank": _ensure_account(c, c["BANK_ACCOUNT_NAME"], "Bank Accounts", "Bank"),
		"clearing": _ensure_account(c, c["CARD_CLEARING_ACCOUNT_NAME"], "Bank Accounts", "Bank"),
		"cfet": None,
	}
	if c["CFET_RATE"] > 0:
		a["cfet"] = _ensure_account(c, "CA Firearm Excise Tax Payable", "Duties and Taxes", "Tax")
	# Only when the company has none: the cash drawer (and the accountant) may already have chosen one.
	if not frappe.db.get_value("Company", c["COMPANY_NAME"], "default_bank_account"):
		frappe.db.set_value("Company", c["COMPANY_NAME"], "default_bank_account", a["bank"])
		log("company default_bank_account ->", a["bank"])
	return a


def _cash_drawer_accounts(c: dict) -> None:
	"""Cash Over/Short and Meals, which the cash drawer posts to. Migrate makes them too, but only on a
	migrate that runs after the company exists; a new site must not have to wait for one. No bank account
	is made here: ``_accounts`` has just made the store's own."""
	from ffl_core.cash_drawer import ensure_accounts
	ensure_accounts(c["COMPANY_NAME"], create_bank=False)


def _stock_settings() -> None:
	"""A fresh ERPNext site has serial numbers off for items, so a firearm Item (has_serial_no) cannot be
	created or stocked. Switch it on (see ``serial_tracking``); on a site that has it, nothing is written."""
	from ffl_core.setup import serial_tracking

	if serial_tracking.ensure():
		log("stock settings enable_serial_and_batch_no_for_item -> 1")


def _form_4473_mode(c: dict) -> None:
	"""Only when store.env says so. Blank touches nothing: a new site then needs FastBound (its Settings
	default to on) before a firearm can be sold, never a silent bypass. Paper mode changes where the 4473
	is kept, not what is required: the cashier still attests the paper form is done, by name."""
	mode = c["FORM_4473_MODE"]
	if not mode:
		return
	paper = mode == "paper"
	# single_flag, not get_single_value: FastBound's "enabled" is on by default but has no row until saved.
	for doctype, field, value, default in (("FFL Settings", "paper_4473_mode", paper, 0),
			("FastBound Settings", "enabled", not paper, 1)):
		if single_flag(doctype, field, default=default) != value:
			frappe.db.set_single_value(doctype, field, int(value))
			log(doctype, field, "->", int(value))


# --- 2. default sales-tax template --------------------------------------------
def _sales_tax_template(c: dict, a: dict) -> str:
	cc = f"Main - {c['COMPANY_ABBR']}"
	rows = [{
		"charge_type": "On Net Total", "account_head": a["sales_tax"], "rate": c["SALES_TAX_RATE"],
		"description": c["SALES_TAX_TITLE"], "cost_center": cc,
	}]
	if a["cfet"]:
		rows.append({
			"charge_type": "On Net Total", "account_head": a["cfet"], "rate": 0,
			"description": f"CA Firearm & Ammo Excise Tax (CFET) {c['CFET_RATE']:g}% - "
				"firearm/precursor/ammunition lines",
			"cost_center": cc,
		})
	name = frappe.db.get_value("Sales Taxes and Charges Template",
		{"company": c["COMPANY_NAME"], "title": c["SALES_TAX_TITLE"]}, "name")
	if name:
		d = frappe.get_doc("Sales Taxes and Charges Template", name)
		have = [(t.charge_type, t.account_head, flt(t.rate), t.description, t.cost_center) for t in d.taxes]
		want = [(r["charge_type"], r["account_head"], flt(r["rate"]), r["description"], r["cost_center"]) for r in rows]
		if have != want or not d.is_default or d.disabled:
			d.taxes = []
			for r in rows:
				d.append("taxes", r)
			d.is_default, d.disabled = 1, 0
			d.save(ignore_permissions=True)
			log("sales tax template updated", name)
		return name
	d = frappe.get_doc({
		"doctype": "Sales Taxes and Charges Template", "title": c["SALES_TAX_TITLE"],
		"company": c["COMPANY_NAME"], "is_default": 1, "disabled": 0, "taxes": rows,
	}).insert(ignore_permissions=True)
	log("sales tax template created", d.name)
	return d.name


# --- 3. item tax templates ------------------------------------------------------
def _item_tax_templates(c: dict, a: dict) -> str:
	rows = [{"tax_type": a["sales_tax"], "tax_rate": c["SALES_TAX_RATE"]}]
	if a["cfet"]:
		rows.append({"tax_type": a["cfet"], "tax_rate": c["CFET_RATE"]})
	itt = frappe.db.get_value("Item Tax Template",
		{"company": c["COMPANY_NAME"], "title": c["ITT_TITLE"]}, "name")
	if itt:
		d = frappe.get_doc("Item Tax Template", itt)
		have = [(t.tax_type, flt(t.tax_rate)) for t in d.taxes]
		want = [(r["tax_type"], flt(r["tax_rate"])) for r in rows]
		if have != want or d.disabled:
			d.taxes = []
			for r in rows:
				d.append("taxes", r)
			d.disabled = 0
			d.save(ignore_permissions=True)
			log("item tax template updated", itt)
	else:
		d = frappe.get_doc({
			"doctype": "Item Tax Template", "title": c["ITT_TITLE"],
			"company": c["COMPANY_NAME"], "taxes": rows,
		}).insert(ignore_permissions=True)
		itt = d.name
		log("item tax template created", itt)
	for g in ("Firearm", "Ammunition"):
		if not frappe.db.exists("Item Group", g):
			continue
		ig = frappe.get_doc("Item Group", g)
		if not any(t.item_tax_template == itt for t in (ig.taxes or [])):
			ig.append("taxes", {"item_tax_template": itt})
			ig.save(ignore_permissions=True)
			log("item group tax attached", g, "->", itt)
	# ffl_core's service exemption: re-point to this store's accounts (0% on each)
	nts = frappe.db.get_value("Item Tax Template",
		{"company": c["COMPANY_NAME"], "title": "Non-Taxable Service"}, "name")
	if nts:
		d = frappe.get_doc("Item Tax Template", nts)
		want = [(acct, 0.0) for acct in [a["sales_tax"]] + ([a["cfet"]] if a["cfet"] else [])]
		if [(t.tax_type, flt(t.tax_rate)) for t in d.taxes] != want:
			d.taxes = []
			for acct, _rate in want:
				d.append("taxes", {"tax_type": acct, "tax_rate": 0})
			d.save(ignore_permissions=True)
			log("non-taxable service template re-synced", nts)
	from ffl_core.setup.tax import ensure as tax_ensure
	log("ffl_core tax.ensure ->", json.dumps(tax_ensure(), default=str))
	return itt


# --- 4. drop ERPNext's generic US seed ---------------------------------------------
def _drop_generic_us_seed(c: dict) -> None:
	abbr = c["COMPANY_ABBR"]
	for t in _US_SEED:
		for dt in ("Item Tax Template", "Sales Taxes and Charges Template",
				"Purchase Taxes and Charges Template"):
			n = f"{t} - {abbr}"
			if not frappe.db.exists(dt, n):
				continue
			try:
				frappe.delete_doc(dt, n, ignore_permissions=True, force=1)
				log("deleted", dt, n)
			except Exception as e:  # linked somewhere -> just disable
				if frappe.get_meta(dt).has_field("disabled"):
					frappe.db.set_value(dt, n, "disabled", 1)
				log("could not delete, disabled instead", dt, n, repr(e)[:120])
	for acct in _US_SEED_ACCOUNTS:
		n = f"{acct} - {abbr}"
		if frappe.db.exists("Account", n) and not frappe.db.exists("GL Entry", {"account": n}):
			try:
				frappe.delete_doc("Account", n, ignore_permissions=True)
				log("deleted account", n)
			except Exception as e:
				frappe.db.set_value("Account", n, "disabled", 1)
				log("could not delete account, disabled instead", n, repr(e)[:120])


# --- 5. tenders -> accounts ----------------------------------------------------------
def _tenders(c: dict, a: dict) -> None:
	def set_mop(mop: str, account: str) -> None:
		if not frappe.db.exists("Mode of Payment", mop):
			return
		d = frappe.get_doc("Mode of Payment", mop)
		row = next((r for r in (d.accounts or []) if r.company == c["COMPANY_NAME"]), None)
		if row is None:
			d.append("accounts", {"company": c["COMPANY_NAME"], "default_account": account})
		elif row.default_account == account:
			return
		else:
			row.default_account = account
		d.save(ignore_permissions=True)
		log("mode of payment", mop, "->", account)

	from ffl_core.cash_drawer import cash_account

	set_mop("Cash", cash_account(c["COMPANY_NAME"]))  # the cash drawer posts here, so the tender must agree (checked in _preflight)
	set_mop("Credit Card", a["clearing"])
	for m in ("Zelle", "ACH", "Check/Money Order", "Check", "Wire Transfer", "Bank Draft"):
		set_mop(m, a["bank"])


# --- 6. masters ------------------------------------------------------------------------
def _masters(c: dict) -> None:
	walk_in = c["WALK_IN_CUSTOMER"]
	if not frappe.db.exists("Customer", walk_in):
		frappe.get_doc({
			"doctype": "Customer", "customer_name": walk_in, "customer_type": "Individual",
			"customer_group": "Individual", "territory": c["COUNTRY"],
		}).insert(ignore_permissions=True)
		log("customer created", walk_in)
	for k, v in (("customer_group", "Individual"), ("territory", c["COUNTRY"])):
		if frappe.db.get_single_value("Selling Settings", k) != v:
			frappe.db.set_single_value("Selling Settings", k, v)
			log("selling settings", k, "->", v)


def letter_head_html(c: dict) -> str:
	"""The store's letter head: logo (when there is one), name, contact line,
	licence line, website. Every interpolated value is escaped."""
	e = escape_html
	contact = " &middot; ".join(e(p) for p in [c["ADDRESS_ONE_LINE"], c["STORE_PHONE"], c["OWNER_EMAIL"]] if p)
	licence = " &middot; ".join(e(p) for p in [
		f"FFL {c['FFL_NUMBER']}" if c["FFL_NUMBER"] else "",
		f"CA DOJ CFD# {c['CFD_NUMBER']}" if c["CFD_NUMBER"] else "",  # blank -> omitted
	] if p)
	website = c["STORE_WEBSITE"].replace("https://", "").replace("http://", "").rstrip("/")
	return (
		'<div style="text-align:center;font-family:Helvetica,Arial,sans-serif;line-height:1.35">'
		+ (f'<img src="{e(c["LOGO_URL"])}" alt="" style="max-height:64px;margin-bottom:4px">' if c["LOGO_URL"] else "")
		+ f'<div style="font-size:18px;font-weight:700;letter-spacing:.02em">{e(c["COMPANY_NAME"])}</div>'
		+ f'<div style="font-size:12px">{contact}</div>'
		+ (f'<div style="font-size:11px;color:#555">{licence}</div>' if licence else "")
		+ (f'<div style="font-size:11px;color:#555">{e(website)}</div>' if website else "")
		+ "</div>"
	)


def _letter_head(c: dict) -> str:
	name = c["COMPANY_ABBR"]
	html = letter_head_html(c)
	if not frappe.db.exists("Letter Head", name):
		frappe.get_doc({
			"doctype": "Letter Head", "letter_head_name": name, "source": "HTML",
			"content": html, "is_default": 1, "disabled": 0,
		}).insert(ignore_permissions=True)  # on_update -> set_as_default (DefaultValue key too)
		log("letter head created", name)
	else:
		lh = frappe.get_doc("Letter Head", name)
		if lh.content != html or not lh.is_default or lh.disabled:
			lh.content, lh.is_default, lh.disabled = html, 1, 0
			lh.save(ignore_permissions=True)  # save, not set_value: on_update owns the default key
			log("letter head refreshed", name)
		elif frappe.defaults.get_global_default("letter_head") != name:
			# Flag says default but the key new documents read disagrees (a fixture
			# import or a raw set_value got there in between): re-assert both.
			lh.set_as_default()
			log("letter head default key re-asserted", name)
	# Another store's brand left behind (e.g. a Letter Head that used to ship as a
	# fixture). Only names listed explicitly; delete unless a POS Profile still
	# points at it, in which case disable.
	for foreign in c["FOREIGN_LETTER_HEADS"]:
		if foreign == name or not frappe.db.exists("Letter Head", foreign):
			continue
		used_by = frappe.get_all("POS Profile", filters={"letter_head": foreign}, pluck="name")
		if used_by:
			frappe.db.set_value("Letter Head", foreign, {"disabled": 1, "is_default": 0})
			log("foreign letter head still referenced -> disabled", foreign, used_by)
		else:
			frappe.delete_doc("Letter Head", foreign, ignore_permissions=True, force=True)
			log("foreign letter head deleted", foreign)
	return name


# --- 6b. store identity ------------------------------------------------------------------
def _company_identity(c: dict) -> None:
	fields = {"email": c["OWNER_EMAIL"], "phone_no": c["STORE_PHONE"], "website": c["STORE_WEBSITE"]}
	if c["LOGO_URL"]:
		fields["company_logo"] = c["LOGO_URL"]
	for k, v in fields.items():
		if v and frappe.db.get_value("Company", c["COMPANY_NAME"], k) != v:
			frappe.db.set_value("Company", c["COMPANY_NAME"], k, v)
			log("company", k, "->", v)


def _company_address(c: dict) -> str:
	"""The address the receipts print (is_your_company_address, enabled).
	Created once; an operator edit is respected — but a disabled one no longer
	counts as "exists", because the reader filters disabled=0."""
	linked = frappe.get_all(
		"Dynamic Link",
		filters={"parenttype": "Address", "link_doctype": "Company", "link_name": c["COMPANY_NAME"]},
		pluck="parent",
	)
	mine = frappe.get_all(
		"Address",
		filters={"name": ("in", linked or [""]), "is_your_company_address": 1, "disabled": 0},
		order_by="is_primary_address desc, creation asc, name asc",  # same pick as company_address()
		pluck="name",
	)
	if mine:
		log("company address exists, left alone", mine)
		return mine[0]
	d = frappe.get_doc({
		"doctype": "Address", "address_title": c["COMPANY_NAME"], "address_type": "Billing",
		"address_line1": c["ADDRESS_LINE1"], "address_line2": c["ADDRESS_LINE2"] or None,
		"city": c["CITY"], "state": c["STATE"], "pincode": c["PINCODE"], "country": c["COUNTRY"],
		"is_your_company_address": 1, "email_id": c["OWNER_EMAIL"] or None,
		"phone": c["STORE_PHONE"] or None,
		"links": [{"link_doctype": "Company", "link_name": c["COMPANY_NAME"]}],
	}).insert(ignore_permissions=True)
	log("company address created", c["ADDRESS_ONE_LINE"])
	return d.name


def _ffl_settings(c: dict) -> None:
	for k, v in (("receipt_footer_note", c["RECEIPT_FOOTER_NOTE"]),):
		if v and frappe.db.get_single_value("FFL Settings", k) != v:
			frappe.db.set_single_value("FFL Settings", k, v)
			log("ffl settings", k, "->", v)


# --- 6c. outgoing mail ---------------------------------------------------------------------
def _email_account(c: dict) -> None:
	name = c["SMTP_ACCOUNT_NAME"]
	flags = {"always_use_account_email_id_as_sender": 1, "always_use_account_name_as_sender_name": 1,
		"default_outgoing": 1}
	if frappe.db.exists("Email Account", name):
		# Credentials rotate in Desk; the sender flags are ours to keep right.
		cur = frappe.db.get_value("Email Account", name, list(flags), as_dict=True)
		todo = {k: v for k, v in flags.items() if cint(cur.get(k)) != v}
		if todo:
			frappe.db.set_value("Email Account", name, todo)
			log("email account sender flags ->", todo)
		else:
			log("email account exists, left alone", name)
		return
	if not (c["SMTP_SERVER"] and c["SMTP_LOGIN"] and c["SMTP_PASSWORD"]):
		log("SMTP not configured — outgoing mail stays OFF "
			"(need SMTP_SERVER + SMTP_LOGIN in store.env and SMTP_PASSWORD in the environment)")
		return
	frappe.get_doc({
		"doctype": "Email Account", "email_account_name": name, "email_id": c["SMTP_SENDER"],
		"enable_outgoing": 1, "enable_incoming": 0,
		"smtp_server": c["SMTP_SERVER"], "smtp_port": c["SMTP_PORT"], "use_tls": 1,
		"login_id_is_different": 1, "login_id": c["SMTP_LOGIN"], "password": c["SMTP_PASSWORD"],
		**flags,
	}).insert(ignore_permissions=True)
	log("SMTP email account created", name, "via", f"{c['SMTP_SERVER']}:{c['SMTP_PORT']}")


# --- 6d. site branding (login page, Desk, e-mail footer) -----------------------------------
def _site_branding(c: dict) -> None:
	def single(doctype: str, field: str, value) -> None:
		if not frappe.get_meta(doctype).has_field(field):
			return
		if frappe.db.get_single_value(doctype, field) != value:
			frappe.db.set_single_value(doctype, field, value)
			log(doctype, field, "->", value)

	footer = " · ".join(p for p in [c["COMPANY_NAME"], c["ADDRESS_ONE_LINE"], c["STORE_PHONE"]] if p)
	single("System Settings", "email_footer_address", footer)
	single("System Settings", "disable_standard_email_footer", 1)   # no "Sent via ERPNext"
	single("Website Settings", "app_name", c["COMPANY_NAME"])
	if c["LOGO_URL"]:
		single("Website Settings", "app_logo", c["LOGO_URL"])
		single("Website Settings", "splash_image", c["LOGO_URL"])
		single("Navbar Settings", "app_logo", c["LOGO_URL"])


# --- 7. POS Profile ------------------------------------------------------------------------
def _pos_profile(c: dict, stc: str, letter_head: str, address: str) -> str:
	name = c["COMPANY_ABBR"]
	abbr = c["COMPANY_ABBR"]
	cc = f"Main - {abbr}"
	print_format = "POS Receipt" if frappe.db.exists("Print Format", "POS Receipt") else None
	want = {"taxes_and_charges": stc, "letter_head": letter_head,
		"print_format": print_format, "company_address": address}
	if not frappe.db.exists("POS Profile", name):
		payments = [m for m in ("Credit Card", "Cash", "Zelle", "ACH", "Check/Money Order")
			if frappe.db.exists("Mode of Payment", m)]
		# The owner login is created by the setup wizard; on a site where it is
		# not (yet) there, do not fail the whole setup over a profile-user row.
		users = ["Administrator"]
		if frappe.db.exists("User", c["OWNER_EMAIL"]):
			users.insert(0, c["OWNER_EMAIL"])
		else:
			log("owner user not on site yet, POS Profile users = Administrator only", c["OWNER_EMAIL"])
		frappe.get_doc({
			"doctype": "POS Profile", "name": name, "company": c["COMPANY_NAME"],
			"country": c["COUNTRY"], "warehouse": f"Stores - {abbr}", "currency": "USD",
			"selling_price_list": "Standard Selling",
			"write_off_account": _account_or_default(c, "Write Off", "write_off_account"), "write_off_cost_center": cc, "write_off_limit": 1,
			"apply_discount_on": "Grand Total", "allow_partial_payment": 1,
			"action_on_new_invoice": "Save Changes and Load New Invoice", "update_stock": 1,
			"validate_stock_on_save": 0, "allow_rate_change": 1, "allow_discount_change": 1,
			"set_grand_total_to_default_mop": 1, "customer": c["WALK_IN_CUSTOMER"],
			"cost_center": cc, "income_account": _account_or_default(c, "Sales", "default_income_account"),
			"expense_account": _account_or_default(c, "Cost of Goods Sold", "default_expense_account"),
			**{k: v for k, v in want.items() if v},
			"payments": [{"mode_of_payment": m, "default": 1 if i == 0 else 0, "allow_in_returns": 1}
				for i, m in enumerate(payments)],
			"applicable_for_users": [{"user": u, "default": 1} for u in users],
		}).insert(ignore_permissions=True)
		log("POS Profile created", name, "users:", users)
		return name
	d = frappe.get_doc("POS Profile", name)
	changed = {k: v for k, v in want.items() if v and d.get(k) != v}
	if changed:
		for k, v in changed.items():
			d.set(k, v)
		d.save(ignore_permissions=True)
		log("POS Profile updated", name, changed)
	return name


# --- 8. verification (reads only) -----------------------------------------------------------
def _verify(c: dict, a: dict) -> dict:
	from erpnext.stock.get_item_details import get_item_tax_template

	def rates_for_group(group):
		out = frappe._dict()
		fake_item = frappe._dict(name="X", item_group=group, taxes=[])
		get_item_tax_template(frappe._dict(company=c["COMPANY_NAME"], tax_category=""), fake_item, out)
		tmpl = out.get("item_tax_template")
		rate_map = {r.tax_type: r.tax_rate for r in frappe.get_doc("Item Tax Template", tmpl).taxes} if tmpl else {}
		return tmpl, rate_map

	checks = {}
	for g, net in (("Pistol", 1000.0), ("Rifle Ammo", 100.0), ("Optic", 500.0), ("Service", 70.0), ("Accessory", 50.0)):
		if not frappe.db.exists("Item Group", g):
			continue
		tmpl, rate_map = rates_for_group(g)
		st = net * flt(rate_map.get(a["sales_tax"], c["SALES_TAX_RATE"])) / 100
		cf = net * flt(rate_map.get(a["cfet"], 0)) / 100 if a["cfet"] else 0
		checks[g] = {"template": tmpl, "net": net, "sales_tax": round(st, 2), "cfet": round(cf, 2),
			"total": round(net + st + cf, 2)}
	print("VERIFY", json.dumps(checks, default=str))
	state = {
		"default_template": frappe.db.get_value("Sales Taxes and Charges Template",
			{"company": c["COMPANY_NAME"], "is_default": 1}, "name"),
		"letter_head_default": frappe.db.get_value("Letter Head", {"is_default": 1}, "name"),
		"letter_head_default_key": frappe.defaults.get_global_default("letter_head"),
		"pos_profile": frappe.db.get_value("POS Profile", c["COMPANY_ABBR"],
			["letter_head", "print_format", "company_address", "taxes_and_charges"], as_dict=True),
		"company_bank": frappe.db.get_value("Company", c["COMPANY_NAME"], "default_bank_account"),
	}
	print("STATE", json.dumps(state, default=str))
	return {"verify": checks, "state": state}


def run(config: dict | None = None) -> dict:
	"""Converge the site to the store described by ``config`` (or the environment).

	Validation (``_normalize``, ``_preflight``) throws before anything is written. After that a
	failure can leave part of the setup in place (some steps commit on their own); every step is
	idempotent, so the answer is to fix the cause and run it again. Prints ``STORE_SETUP COMMITTED``
	last, once everything is written — the scripts require that line and fail without it.
	"""
	c = _normalize(config)
	_preflight(c)
	a = _accounts(c)
	_cash_drawer_accounts(c)
	_stock_settings()
	stc = _sales_tax_template(c, a)
	_item_tax_templates(c, a)
	_drop_generic_us_seed(c)
	from ffl_core.setup.payments import ensure_manual_modes_of_payment
	ensure_manual_modes_of_payment()
	_tenders(c, a)
	_masters(c)
	letter_head = _letter_head(c)
	_company_identity(c)
	address = _company_address(c)
	_ffl_settings(c)
	_form_4473_mode(c)
	_email_account(c)
	_site_branding(c)
	_pos_profile(c, stc, letter_head, address)
	from ffl_integrations.payments.setup import ensure_mode_of_payment
	ensure_mode_of_payment()
	result = _verify(c, a)
	commit()
	frappe.clear_cache()
	print(MARKER)
	return result
