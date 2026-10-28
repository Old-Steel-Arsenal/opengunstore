"""Waivers and agreements: request, sign, paper copies, status, and the sale gate.

An **Agreement Template** holds the text (a range waiver, a rental agreement); a
**Customer Agreement** is one customer's copy of it. The copy is written only here.

* **Request** — :func:`request_agreement` (counter) creates the customer's Pending
  copy, or re-issues the one already pending (the old link stops working), with the
  text rendered for this customer and frozen on the copy (``body_snapshot``). It
  hands out a one-time signing link: sent to a paired signing tablet, which opens
  it by itself (``ffl_core.signing_terminal``), shown as a QR code at the counter
  (a tablet or the customer's phone scans it) or emailed to the customer. The link's
  token is 256 random bits; only its sha256 is stored, and it works for
  :data:`TOKEN_HOURS`.
* **Sign** — the page ``/agreement/sign#t=<token>`` (``www/agreement/sign.*``, the
  token in the fragment so it never reaches a log or a Referer) calls the two Guest
  endpoints :func:`load_for_signing` and :func:`submit_signature`. They answer only
  for a valid token, return only the customer's name, the title and the text, and
  give one and the same error for every bad token. The signature is a PNG checked
  for size, type and ink; it and the signed PDF are private files. After the
  signature commits a background job builds the PDF and, when the customer has an
  email address, sends it to them.
* **Minors** — on a template with ``minors_need_guardian``, a participant under 18
  is signed for by a parent or guardian. The age comes from the customer's date of
  birth when the store keeps one (FFL Settings → Store Customer ID Numbers);
  otherwise the signer declares it on the page.
* **Paper** — a manager can record a copy signed on paper
  (:func:`record_paper_agreement`): the scan, attached to the copy, is made private
  and the copy becomes Signed via Paper. No one can waive a missing signature.
* **Status** — :func:`agreement_status` (signed / expiring / expired / pending /
  missing) feeds the counter panel (:func:`counter_panel_rows`, the ``agreements``
  key of ``customer_flags.get_customer_flags``); :func:`require_agreement` raises
  :class:`AgreementRequired` for an extension that needs a signature first.
  :func:`expire_agreements` (daily) turns copies past ``expires_on`` to Expired.
* **Sale gate** — an Item can name the agreement its buyer must have signed
  or its Item Group does, else a used Item takes FFL Settings › Used Item Agreement
  (:func:`required_for_item`); a template's ``requirement`` says
  whether that matters, or every sale needs it, or it is only offered; its ``scope``
  says whether one signature covers the customer or each firearm sold needs a signed
  copy naming the gun (:func:`sale_requirements`; one copy may name every gun of a
  sale, in its ``firearms`` table). The register's Agreements button
  lists them all for the cart (:func:`agreements_for_sale`). A **Per document** template is
  signed for one record (``reference_doctype`` / ``reference_name``: a gunsmith work order's
  intake authorization), from that record, and is never asked for at a sale.
  (``Item.ffl_required_agreement``, ``setup/agreements.py``), and extensions can add
  more through the ``gunshop_required_agreements`` hook. FFL Settings › Require
  Agreements at Sale (on by default) switches the whole sale gate off. :func:`refuse_missing_required`
  refuses to submit a counter sale (POS Invoice / Sales Invoice) until the customer
  has signed them. Returns, POS consolidation invoices and web / GunBroker sales
  booked after the fact are left alone. A counter order (``manual_order.create_order``)
  is booked unsigned instead: the links are emailed (:func:`email_sale_agreements`)
  and its Dispose on Pending Order waits for the signatures.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
import secrets
from datetime import timedelta

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_days, cint, escape_html, flt, fmt_money, formatdate, get_datetime, get_fullname, get_url, \
	getdate, now_datetime, nowdate, split_emails, strip_html
from frappe.utils.html_utils import sanitize_html

from ffl_core.compliance.channel_invoice import is_channel_mirrored_invoice
from ffl_core.roles import COUNTER_ROLES, CUSTOMER_MANAGER_ROLES
from ffl_core.store_identity import company_brand
from ffl_core.utils import atomic, commit, guest_job_elevated, set_file_privacy, single_flag

DOCTYPE = "Customer Agreement"
# The guns an Each firearm sold copy covers (one signature for every used gun in a sale).
FIREARM_ROW = "Customer Agreement Firearm"
TEMPLATE = "Agreement Template"
TOKEN_HOURS = 72
EXPIRING_DAYS = 30
ADULT_AGE = 18
# A data-URL PNG of a 1200x400 signature is ~20-60 KB; these are the hard ceilings.
MAX_SIGNATURE_CHARS = 700_000
MAX_SIGNATURE_BYTES = 500 * 1024
MAX_SIGNATURE_PX = 2000
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_LINK_EMAIL = "Agreement Signing Link"
_COPY_EMAIL = "Agreement Signed Copy"
_PDF_TEMPLATE = "templates/agreement/agreement_pdf.html"
# Every invalid token gets this, whatever the reason (the reason goes to the log).
INVALID_LINK = "This signing link is not valid any more. Ask the store for a new one."
DELIVERIES = ("tablet", "qr", "email", "none")
RELATIONSHIPS = ("Self", "Parent", "Guardian")
# Agreement Template.requirement / .scope (see the doctype's field descriptions).
OPTIONAL, BY_ITEM, EVERY_SALE = "Optional", "Required by item", "Required for every sale"
PER_CUSTOMER, PER_FIREARM, PER_DOCUMENT = "Per customer", "Each firearm sold", "Per document"


class AgreementRequired(frappe.ValidationError):
	"""The customer has not signed an agreement this action needs."""


def _log():
	return frappe.logger("agreements")


def _hash(token: str) -> str:
	return hashlib.sha256(token.encode()).hexdigest()


def _is_manager() -> bool:
	return bool(set(frappe.get_roles()) & set(CUSTOMER_MANAGER_ROLES))


def _default_company() -> str | None:
	return frappe.defaults.get_user_default("Company") or frappe.defaults.get_global_default("company")


def _age_on(dob, on) -> int:
	dob, on = getdate(dob), getdate(on)
	return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def _expires_on(template, signed_day):
	row = frappe.db.get_value(TEMPLATE, template, ["valid_days", "scope"], as_dict=True) or frappe._dict()
	if row.scope in (PER_FIREARM, PER_DOCUMENT):   # belongs to one sale / one record; never runs out by days
		return None
	days = cint(row.valid_days)
	return add_days(getdate(signed_day), days) if days > 0 else None


def render_body(template_doc, customer_doc, company: str | None, firearm: dict | list | None = None) -> str:
	"""The template's text for this customer, placeholders filled, sanitized.

	Only the documented placeholders are in the context, each value HTML-escaped;
	*firearm* adds the guns' for an Each firearm sold copy (a list of
	:func:`firearm_context`: one gun fills its placeholders, several list each
	placeholder's values with " / ", and ``{{ firearms }}`` is a table of them all),
	or the record's (:func:`document_context`) for a Per document one.
	"""
	brand = company_brand(company)
	name = customer_doc.customer_name or customer_doc.name
	first = (customer_doc.get("ffl_first_name") or "").strip() or name.split(" ")[0]
	guns = firearm if isinstance(firearm, list) else None
	if guns:
		firearm = {k: " / ".join(str(g[k]) for g in guns) for k in guns[0]}
	ctx = {k: escape_html(str(v)) for k, v in {
		"customer_name": name, "first_name": first, "date": formatdate(nowdate()),
		"store_name": brand.get("name") or "", "store_address": brand.get("address_line") or "",
		"valid_days": template_doc.valid_days or "", **(firearm or {}),
	}.items()}
	if guns:
		ctx["firearms"] = _firearms_table(guns)
	return sanitize_html(frappe.render_template(template_doc.body or "", ctx))


def _firearms_table(guns: list[dict]) -> str:
	"""``{{ firearms }}``: one row per gun a copy covers, every value escaped."""
	e = lambda v: escape_html(str(v or ""))  # noqa: E731
	rows = "".join(f"<tr><td>{e(' '.join(filter(None, (g['manufacturer'], g['model']))) or g['item_name'])}"
		f"{', ' + e(g['caliber']) if g['caliber'] else ''}</td><td>{e(g['serial_no'])}</td><td>{e(g['price'])}</td></tr>"
		for g in guns)
	return f"<table><tr><th>Firearm</th><th>Serial</th><th>Price</th></tr>{rows}</table>"


def firearm_context(serial_no: str, price=None) -> dict:
	"""The gun an Each firearm sold copy names: its Serial No's own make / model /
	caliber, else its Item's."""
	sn = frappe.db.get_value("Serial No", serial_no, ["item_code", "manufacturer", "model_name", "caliber"],
		as_dict=True) or frappe._dict()
	item = frappe.db.get_value("Item", sn.item_code, ["item_name", "manufacturer", "model_name", "caliber"],
		as_dict=True) if sn.get("item_code") else None
	item = item or frappe._dict()
	return {"serial_no": serial_no, "item_name": item.item_name or sn.item_code or "",
		"manufacturer": sn.manufacturer or item.manufacturer or "", "model": sn.model_name or item.model_name or "",
		"caliber": sn.caliber or item.caliber or "",
		"price": fmt_money(flt(price), currency=frappe.defaults.get_global_default("currency")) if price else ""}


def document_context(reference_doctype: str, reference_name: str) -> dict:
	"""The placeholders a Per document copy adds: the record's own ``agreement_context()``
	(its controller decides what the text may name), else nothing."""
	doc = frappe.get_doc(reference_doctype, reference_name)
	fn = getattr(doc, "agreement_context", None)
	return {k: "" if v is None else v for k, v in (fn() if callable(fn) else {}).items()}


def _template_rows(names=None) -> dict:
	filters = {"active": 1}
	if names is not None:
		filters["name"] = ("in", list(names) or [""])
	rows = frappe.get_all(TEMPLATE, filters=filters,
		fields=["name", "requirement", "scope", "show_on_counter_panel"], order_by="title asc")
	for r in rows:   # a template saved before the two settings existed
		r.requirement = r.requirement or BY_ITEM
		r.scope = r.scope or PER_CUSTOMER
	return {r.name: r for r in rows}


def is_per_firearm(template: str) -> bool:
	return frappe.db.get_value(TEMPLATE, template, "scope") == PER_FIREARM


# ------------------------------------------------------------------ status


def agreement_status(customer: str, template: str, today=None, serial_no: str | None = None,
		sale: str | None = None, reference: tuple[str, str] | None = None) -> dict:
	"""Where *customer* stands with *template* today.

	For an Each firearm sold template pass the gun (*serial_no*) and, when there is
	one, the sale it is for (*sale*): a signed copy covering that gun (its
	``firearms`` rows; one copy may cover every gun of a sale) counts until a
	submitted sale has used it (``reference_name``), and never expires. For a Per
	document template pass the record (*reference*: ``(doctype, name)``): only that
	record's copy counts, and it never expires.

	Returns:
	    {"template", "serial_no", "state", "expires_on", "agreement"} where state is
	    ``signed`` (valid, not expiring within 30 days), ``expiring``, ``expired``
	    (the last signed copy ran out), ``pending`` (sent, not signed) or
	    ``missing``. ``agreement`` is the copy the state comes from.
	"""
	today = getdate(today or nowdate())
	filters = {"customer": customer, "template": template, "status": ("in", ("Signed", "Pending", "Expired"))}
	if serial_no:
		filters["name"] = ("in", _covering([serial_no]) or [""])
	else:
		filters["serial_no"] = ("is", "not set")
	if reference:
		filters.update(reference_doctype=reference[0], reference_name=reference[1])
	elif not serial_no:
		filters["reference_name"] = ("is", "not set")
	rows = frappe.get_all(DOCTYPE, filters=filters, fields=["name", "status", "expires_on", "reference_name"],
		order_by="creation desc")
	out = {"template": template, "serial_no": serial_no, "state": "missing", "expires_on": None, "agreement": None}
	if serial_no or reference:
		# A gun's copy is free until a sale uses it; a record's copy is that record's.
		usable = [r for r in rows if r.status == "Signed" and (reference or not r.reference_name
			or r.reference_name == sale)]
		if usable:
			out.update(state="signed", agreement=usable[0].name)
			return out
		pending = next((r for r in rows if r.status == "Pending"), None)
		if pending:
			out.update(state="pending", agreement=pending.name)
		return out
	valid = [r for r in rows if r.status == "Signed" and (not r.expires_on or getdate(r.expires_on) >= today)]
	if valid:
		# Never-expiring first, then the one that lasts longest.
		best = max(valid, key=lambda r: (r.expires_on is None, getdate(r.expires_on) if r.expires_on else today))
		soon = best.expires_on and getdate(best.expires_on) <= getdate(add_days(today, EXPIRING_DAYS))
		out.update(state="expiring" if soon else "signed", agreement=best.name,
			expires_on=str(best.expires_on) if best.expires_on else None)
		return out
	pending = next((r for r in rows if r.status == "Pending"), None)
	if pending:
		out.update(state="pending", agreement=pending.name)
		return out
	lapsed = next((r for r in rows if r.status in ("Signed", "Expired")), None)
	if lapsed:
		out.update(state="expired", agreement=lapsed.name,
			expires_on=str(lapsed.expires_on) if lapsed.expires_on else None)
	return out


def _covering(serials: list[str]) -> list[str]:
	"""The copies whose ``firearms`` name any of *serials*."""
	return list(dict.fromkeys(frappe.get_all(FIREARM_ROW, filters={"parenttype": DOCTYPE,
		"serial_no": ("in", serials)}, pluck="parent"))) if serials else []


def _satisfied(status: dict) -> bool:
	return status["state"] in ("signed", "expiring")


def require_agreement(customer: str, template: str) -> None:
	"""Raise :class:`AgreementRequired` unless *customer* holds a valid signed *template*."""
	if not _satisfied(agreement_status(customer, template)):
		name = frappe.db.get_value("Customer", customer, "customer_name") or customer
		frappe.throw(_("{0} has not signed {1}.").format(frappe.bold(escape_html(name)),
			frappe.bold(escape_html(template))), AgreementRequired, title=_("Agreement needed"))


def counter_panel_rows(customer: str) -> list[dict]:
	"""Status of every active Per customer template marked "Show on Counter Panel"
	(an Each firearm sold one depends on the cart: the Agreements button shows it)."""
	return [{**agreement_status(customer, t.name), "requirement": t.requirement} for t in _template_rows().values()
		if t.show_on_counter_panel and t.scope == PER_CUSTOMER]


def required_for_item(item_code: str) -> str | None:
	"""The agreement an Item needs: its own ``ffl_required_agreement``, else the
	nearest Item Group (the Item's group or a parent) that names one, else — for
	an Item marked Used — FFL Settings › Used Item Agreement."""
	row = frappe.db.get_value("Item", item_code, ["ffl_required_agreement", "item_group", "is_used"], as_dict=True) \
		if frappe.db.has_column("Item", "ffl_required_agreement") else None
	if not row:
		return None
	if row.ffl_required_agreement:
		return row.ffl_required_agreement
	return _group_agreement(row.item_group) or (_used_item_agreement() if row.is_used else None)


def _group_agreement(item_group: str | None) -> str | None:
	if not item_group or not frappe.db.has_column("Item Group", "ffl_required_agreement"):
		return None
	lft, rgt = frappe.db.get_value("Item Group", item_group, ["lft", "rgt"]) or (None, None)
	if lft is None:
		return None
	hit = frappe.get_all("Item Group", filters={"lft": ("<=", lft), "rgt": (">=", rgt),
		"ffl_required_agreement": ("is", "set")}, pluck="ffl_required_agreement", order_by="lft desc", limit=1)
	return hit[0] if hit else None


def _used_item_agreement() -> str | None:
	return frappe.db.get_single_value("FFL Settings", "used_item_agreement_template") or None


def _row_serials(row) -> list[str]:
	return [s.strip() for s in (row.get("serial_no") or "").replace(",", "\n").splitlines() if s.strip()]


def sale_agreements_enforced() -> bool:
	"""FFL Settings › Require Agreements at Sale (on unless a store turned it off).
	Only the sale gate: requesting, signing, paper copies, Per document agreements and
	:func:`require_agreement` work either way."""
	return single_flag("FFL Settings", "enforce_sale_agreements", default=1)


def sale_requirements(doc) -> list[dict]:
	"""What a counter sale's customer must have signed: [{"template", "serial_no",
	"item_code"}] (serial_no only for an Each firearm sold template, one entry per gun).

	* Required for every sale — every sale (an Each firearm sold one: every gun in it),
	  except an FFL dealer's;
	* Required by item — the Items (or their Item Groups) that name it, and whatever
	  ``gunshop_required_agreements`` providers name (a failing provider is logged and
	  adds nothing);
	* Optional — never; it is offered from the Agreements button only.

	An Each firearm sold template applies to the sale's serial-numbered lines only; a
	Per document one never applies to a sale. With FFL Settings › Require Agreements
	at Sale off, nothing does (providers are not even asked): the sale gate, a counter
	order's emails, Dispose's check and the register's "required" marks all read this.
	"""
	if not sale_agreements_enforced():
		return []
	rows = [r for r in doc.get("items") or [] if r.get("item_code")]
	named: dict[str, list] = {}
	for r in rows:
		t = required_for_item(r.item_code)
		if t:
			named.setdefault(t, []).append(r)
	for hook in frappe.get_hooks("gunshop_required_agreements"):
		try:
			for t in frappe.get_attr(hook)(doc) or []:
				if isinstance(t, str):
					named.setdefault(t, []).extend(rows)
		except Exception:
			frappe.log_error(title=f"gunshop_required_agreements provider failed: {hook}")
	templates = _template_rows()
	# An FFL dealer buying wholesale is not who a store-wide waiver is for.
	dealer = bool(doc.get("customer") and frappe.db.get_value("Customer", doc.get("customer"), "is_ffl_dealer"))
	out, seen = [], set()

	def add(t, serial, item):
		key = (t, serial)
		if key not in seen:
			seen.add(key)
			out.append({"template": t, "serial_no": serial, "item_code": item})

	for t, row in templates.items():
		if row.scope == PER_DOCUMENT:
			continue
		if row.requirement == EVERY_SALE:
			if dealer:
				continue
			lines = rows
		elif row.requirement == BY_ITEM and t in named:
			lines = named[t]
		else:
			continue
		if row.scope == PER_FIREARM:
			for r in lines:
				for serial in _row_serials(r):
					add(t, serial, r.item_code)
		else:
			add(t, None, None)
	return out


def required_templates(doc) -> list[str]:
	"""The templates :func:`sale_requirements` names (each once)."""
	return list(dict.fromkeys(n["template"] for n in sale_requirements(doc)))


def _sale_name(doc) -> str | None:
	return None if doc.is_new() else doc.name


def missing_requirements(doc) -> list[dict]:
	"""The :func:`sale_requirements` the customer has not signed yet."""
	customer = doc.get("customer")
	return [n for n in sale_requirements(doc) if not _satisfied(
		agreement_status(customer, n["template"], serial_no=n["serial_no"], sale=_sale_name(doc)))]


@frappe.whitelist()
def agreements_for_sale(customer: str, items: list | str | None = None, sale: str | None = None) -> dict:
	"""Every active agreement and where this customer stands, for the register's Agreements button.

	Args:
	    customer: Customer name (document id).
	    items: The cart: [{"item_code", "serial_no", "rate"}] (serial_no may hold
	        several, one per line).
	    sale: The POS Invoice, once saved.

	Returns:
	    {"rows": [{"template", "scope", "requirement", "required": whether this
	    sale needs it, "state", "expires_on", "agreement", "firearms"}]} — every
	    template once. An Each firearm sold one lists the guns one copy is signed
	    for in "firearms": [{"serial_no", "item_code", "item_name", "rate",
	    "state"}] (the guns the sale needs it for, else every gun in the cart; empty
	    when the cart has none); its "state" is signed once every one of them is,
	    pending when the unsigned ones all have a copy out, else missing.
	"""
	frappe.has_permission("Customer", "read", doc=customer, throw=True)
	cart = frappe.parse_json(items) if isinstance(items, str) else (items or [])
	cart = [frappe._dict(r) for r in cart if isinstance(r, dict) and isinstance(r.get("item_code"), str)][:200]
	needed = {(n["template"], n["serial_no"]) for n in sale_requirements(frappe._dict(items=cart, customer=customer))}
	guns = [(serial, r) for r in cart for serial in _row_serials(r)]
	names = {r.item_code: r for r in frappe.get_all("Item", filters={"name": ("in", [g[1].item_code for g in guns] or [""])},
		fields=["name as item_code", "item_name"])}
	out = []
	for t, row in _template_rows().items():
		if row.scope == PER_DOCUMENT:
			continue
		base = {"template": t, "scope": row.scope, "requirement": row.requirement, "firearms": []}
		if row.scope != PER_FIREARM:
			st = agreement_status(customer, t)
			out.append({**base, "required": (t, None) in needed, "state": st["state"],
				"expires_on": st["expires_on"], "agreement": st["agreement"]})
			continue
		mine = [(serial, r) for serial, r in guns if (t, serial) in needed] or guns
		firearms = [{"serial_no": serial, "item_code": r.item_code,
			"item_name": (names.get(r.item_code) or {}).get("item_name"), "rate": flt(r.get("rate")),
			"state": agreement_status(customer, t, serial_no=serial, sale=sale)["state"]} for serial, r in mine]
		unsigned = [g for g in firearms if g["state"] != "signed"]
		state = "signed" if firearms and not unsigned else \
			"pending" if unsigned and all(g["state"] == "pending" for g in unsigned) else "missing"
		out.append({**base, "required": any((t, g["serial_no"]) in needed for g in firearms), "state": state,
			"expires_on": None, "agreement": None, "firearms": firearms})
	out.sort(key=lambda x: (not x["required"], x["template"]))
	return {"rows": out}


def refuse_missing_required(doc, method=None) -> None:
	"""POS Invoice / Sales Invoice before_submit: the customer must have signed what the sale needs.

	There is no override: the customer signs (the Agreements button at the register,
	or Request agreement on the customer), or a manager records a copy signed on paper.
	The one exception is a counter order built by ``manual_order.create_order``
	(``doc.flags.ffl_defer_agreements``): a phone or ship-to-FFL sale where the customer
	is not here to sign. It is booked, the links are emailed (:func:`email_sale_agreements`)
	and the gun waits on Pending Order, whose Dispose checks again.
	"""
	customer = doc.get("customer")
	if not customer or doc.get("is_return") or doc.get("is_consolidated"):
		return
	if doc.flags.get("ffl_defer_agreements"):
		return
	if frappe.flags.in_import or frappe.flags.in_patch or frappe.flags.in_migrate:
		return
	if is_channel_mirrored_invoice(doc):
		return
	missing = missing_requirements(doc)
	if not missing:
		return
	name = frappe.db.get_value("Customer", customer, "customer_name") or customer
	what = ", ".join(frappe.bold(escape_html(n["template"] + (f" ({n['serial_no']})" if n["serial_no"] else "")))
		for n in missing)
	frappe.throw(_("{0} must sign {1} before this sale. At the register, click Agreements under the "
		"customer's name to send it to the tablet, show a QR code or email a link; or a manager records a "
		"copy signed on paper.").format(frappe.bold(escape_html(name)), what),
		AgreementRequired, title=_("Agreement needed"))


def mark_firearm_agreements_used(doc, method=None) -> None:
	"""POS Invoice / Sales Invoice on_submit: the Each firearm sold copies this sale
	relied on now belong to it (``reference_*``), so selling the same gun again —
	after a return, say — needs a new one."""
	if doc.get("is_return") or doc.get("is_consolidated") or is_channel_mirrored_invoice(doc):
		return
	for n in sale_requirements(doc):
		if not n["serial_no"]:
			continue
		st = agreement_status(doc.customer, n["template"], serial_no=n["serial_no"], sale=doc.name)
		# Already this sale's (a counter order signed before it was disposed): nothing to redo.
		if st["state"] == "signed" and st["agreement"] \
				and frappe.db.get_value(DOCTYPE, st["agreement"], "reference_name") != doc.name:
			frappe.db.set_value(DOCTYPE, st["agreement"], {"reference_doctype": doc.doctype,
				"reference_name": doc.name}, update_modified=False)
			# The signed copy now names its invoice / receipt number: build it again.
			if frappe.db.get_value(DOCTYPE, st["agreement"], "signature"):
				frappe.enqueue("ffl_core.agreements.rebuild_signed_pdf", queue="short",
					enqueue_after_commit=True, name=st["agreement"])


def _line_rate(doc, serial_no: str | None):
	"""The price of the line that sells *serial_no* (for an Each firearm sold copy's text)."""
	return next((flt(r.get("rate")) for r in doc.get("items") or [] if serial_no and serial_no in _row_serials(r)),
		None)


def email_sale_agreements(doc, email: str | None, needs: list[dict] | None = None, replace: int = 0) -> list[dict]:
	"""Email *doc*'s customer a signing link for each agreement the sale still needs.

	For a sale booked before the customer signed (a counter order taken on the phone;
	see :func:`refuse_missing_required`). *needs* defaults to :func:`missing_requirements`;
	an Each firearm sold one gets one copy for all its guns, naming each line's price.
	A copy whose link is still open is left alone unless *replace*. Never raises: each
	agreement is tried on its own and a failure is reported, not thrown, so it cannot
	undo the sale.

	Returns:
	    [{"template", "serial_no", "state": "sent" / "link_open" / "failed", "email",
	    "token_expires", "error"}] — one per need (the guns of one copy share its result).
	"""
	# An order may carry a list of addresses; the link goes to the first.
	email = next(iter(split_emails(email or "")), "").strip()
	groups: dict[str, list[dict]] = {}
	for n in missing_requirements(doc) if needs is None else needs:
		groups.setdefault(n["template"], []).append(n)
	out = []
	for template, group in groups.items():
		rows = [{"template": template, "serial_no": n.get("serial_no"), "state": "failed", "email": email or None,
			"token_expires": None, "error": None} for n in group]
		out.extend(rows)
		if not email:
			for row in rows:
				row["error"] = _("No email address on this order.")
			continue
		guns = [{"serial_no": n["serial_no"], "price": _line_rate(doc, n["serial_no"])} for n in group if n.get("serial_no")]
		mark = len(frappe.local.message_log or [])
		try:
			with atomic("email sale agreement"):
				r = _request(doc.customer, template, "email", replace, firearms=guns, email=email)
			for row in rows:
				row.update(state="link_open" if r.get("link_open") else "sent", token_expires=r.get("token_expires"))
		except Exception as e:
			# A handled failure: its throw must not sit as a red error on a successful order.
			if frappe.local.message_log is not None:
				del frappe.local.message_log[mark:]
			for row in rows:
				row["error"] = strip_html(str(e)) or _("The email could not be sent.")
			frappe.log_error(title=f"Agreement email for {doc.name} failed")
	return out


def release_firearm_agreements(doc, method=None) -> None:
	"""POS Invoice / Sales Invoice on_cancel: a cancelled sale gives its Each firearm
	sold copies back (the gun did not leave)."""
	for name in frappe.get_all(DOCTYPE, filters={"reference_doctype": doc.doctype, "reference_name": doc.name},
			pluck="name"):
		frappe.db.set_value(DOCTYPE, name, {"reference_doctype": None, "reference_name": None},
			update_modified=False)


# ------------------------------------------------------------------ staff side


def _qr_data_uri(url: str) -> str:
	import pyqrcode

	buf = io.BytesIO()
	pyqrcode.create(url, error="M").svg(buf, scale=6, background="#ffffff", xmldecl=False, svgns=True)
	return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


@frappe.whitelist(methods=["POST"])
def request_agreement(customer: str, template: str, deliver: str = "qr", replace: int = 0,
		terminal: str | None = None, serial_no: str | None = None, price: float | None = None,
		reference_doctype: str | None = None, reference_name: str | None = None,
		firearms: list | str | None = None) -> dict:
	"""Send a customer an agreement to sign.

	Creates the customer's Pending copy of the template, or re-issues the one
	already pending (its old link stops working and its text is rendered again).
	While that copy still has a working link — the customer may be signing on the
	tablet right now — nothing changes unless ``replace`` is 1: the answer is
	``{"name", "link_open": True, "token_expires"}`` so the counter can ask first.

	Args:
	    customer: Customer name (document id).
	    template: Agreement Template name; must be active.
	    deliver: ``tablet`` — send the link to a paired signing tablet
	        (``terminal``; ``ffl_core.signing_terminal``), which opens it by itself
	        once the request commits (a caller that never commits sends nothing);
	        ``qr`` — return the signing link and a QR code for the counter;
	        ``email`` — email the link to the customer (refused when the customer
	        has no email address); ``none`` — only create the Pending copy (a
	        manager then records a paper copy on it).
	    replace: 1 to re-issue even though the pending copy's link still works.
	    terminal: Signing Terminal name, for ``tablet``; must be active and paired.
	    serial_no: The gun, for an Each firearm sold template (it or *firearms* is required there).
	    price: The gun's price in this sale, for the text's ``{{ price }}``.
	    reference_doctype: For a Per document template (required there): the record's DocType.
	    reference_name: That record's name; the caller must be able to read it, and the
	        record's ``agreement_context()`` fills the text's placeholders.
	    firearms: For an Each firearm sold template, several guns signed for at once:
	        [{"serial_no", "price"}]. One copy covers them all; a pending copy covering
	        any of them is the one re-issued.

	Returns:
	    {"name": the Customer Agreement, "deliver", "token_expires"} plus, for
	    ``qr``, "sign_url" and "qr_svg" (an SVG data URI); for ``email``, "email".
	    The link itself is never stored.
	"""
	frappe.only_for(COUNTER_ROLES)
	frappe.has_permission("Customer", "read", doc=customer, throw=True)
	return _request(customer, template, deliver, replace, terminal, serial_no, price, reference_doctype,
		reference_name, firearms=firearms)


def _gun_list(serial_no, price, firearms) -> list[tuple[str, float | None]]:
	"""[(serial, price)] from *firearms* ([{"serial_no", "price"}]) or the one *serial_no*, each gun once."""
	rows = frappe.parse_json(firearms) if isinstance(firearms, str) else firearms
	if not rows:
		rows = [{"serial_no": serial_no, "price": price}]
	if not isinstance(rows, list) or len(rows) > 50:
		frappe.throw(_("firearms must be a list of up to 50 guns."))
	out = {}
	for r in rows:
		s = (r.get("serial_no") if isinstance(r, dict) else None) or ""
		if isinstance(s, str) and s.strip():
			out.setdefault(s.strip(), flt(r.get("price")) or None)
	return list(out.items())


def _request(customer, template, deliver="qr", replace=0, terminal=None, serial_no=None, price=None,
		reference_doctype=None, reference_name=None, email: str | None = None, firearms=None) -> dict:
	""":func:`request_agreement` past its role / Customer checks (the caller made its own).
	*email*, for ``email``, is where the link goes instead of the customer's address."""
	if deliver not in DELIVERIES:
		frappe.throw(_("Unknown delivery: {0}").format(escape_html(deliver)))
	tpl = frappe.get_doc(TEMPLATE, template)
	if not tpl.active:
		frappe.throw(_("{0} is switched off.").format(escape_html(template)))
	cust = frappe.get_doc("Customer", customer)
	guns = []
	if tpl.scope == PER_FIREARM:
		guns = _gun_list(serial_no, price, firearms)
		if not guns or any(not frappe.db.exists("Serial No", s) for s, _p in guns):
			frappe.throw(_("{0} is signed for each firearm sold: pick the gun it is for.").format(
				escape_html(template)), title=_("Which firearm?"))
	serial_no = guns[0][0] if guns else None
	reference = None
	if tpl.scope == PER_DOCUMENT:
		if not (reference_doctype and reference_name and frappe.db.exists(reference_doctype, reference_name)):
			frappe.throw(_("{0} is signed for one record: send it from that record.").format(escape_html(template)),
				title=_("Which record?"))
		frappe.has_permission(reference_doctype, "read", doc=reference_name, throw=True)
		if frappe.db.get_value(reference_doctype, reference_name, "customer") != customer:
			frappe.throw(_("{0} {1} is not {2}'s.").format(escape_html(reference_doctype),
				escape_html(reference_name), escape_html(customer)))
		reference = (reference_doctype, reference_name)
	firearm = [firearm_context(s, p) for s, p in guns] if guns else \
		document_context(*reference) if reference else None
	email = (email or cust.get("email_id") or "").strip()
	if deliver == "tablet":
		row = frappe.db.get_value("Signing Terminal", terminal or "", ["active", "device_secret_hash"], as_dict=True)
		if not row or not row.active or not row.device_secret_hash:
			frappe.throw(_("{0} is not a paired, active signing tablet. Pair it on its Signing Terminal record, or "
				"show the QR code instead.").format(escape_html(terminal or "")), title=_("Signing tablet"))
	if deliver == "email" and not email:
		frappe.throw(_("{0} has no email address. Show the QR code instead, or add an email to the customer.")
			.format(escape_html(cust.customer_name)), title=_("No email address"))
	token = secrets.token_urlsafe(32) if deliver != "none" else None
	expires = now_datetime() + timedelta(hours=TOKEN_HOURS) if token else None
	company = _default_company()
	with atomic("request agreement"):
		existing = frappe.db.get_value(DOCTYPE, {"customer": customer, "template": template, "status": "Pending",
			**({"name": ("in", _covering([s for s, _p in guns]) or [""])} if guns
				else {"serial_no": ("is", "not set")}),
			**({"reference_doctype": reference[0], "reference_name": reference[1]} if reference
				else {"reference_name": ("is", "not set")})}, ["name", "sign_token_hash", "token_expires"],
			as_dict=True, for_update=True, order_by="creation desc")
		if existing and existing.sign_token_hash and existing.token_expires \
				and get_datetime(existing.token_expires) > now_datetime() and not cint(replace):
			return {"name": existing.name, "link_open": True, "token_expires": str(existing.token_expires)}
		doc = frappe.get_doc(DOCTYPE, existing.name) if existing else frappe.new_doc(DOCTYPE)
		doc.update({
			"customer": customer, "template": template, "template_version": tpl.version or 1,
			"company": company, "status": "Pending",
			"body_snapshot": render_body(tpl, cust, company, firearm),
			"serial_no": serial_no, "item_code": serial_no and frappe.db.get_value("Serial No", serial_no, "item_code"),
			"firearms": [{"serial_no": s, "item_code": frappe.db.get_value("Serial No", s, "item_code"), "price": p}
				for s, p in guns],
			"reference_doctype": reference and reference[0], "reference_name": reference and reference[1],
			"sign_token_hash": _hash(token) if token else None, "token_expires": expires,
			"delivery_channel": {"tablet": "Terminal", "qr": "QR", "email": "Email"}.get(deliver),
			"requested_by": frappe.session.user,
		})
		doc.flags.via_agreements = True
		doc.save(ignore_permissions=True)
	out = {"name": doc.name, "deliver": deliver, "token_expires": str(expires) if expires else None}
	if not token:
		return out
	url = get_url("/agreement/sign#t=" + token)
	if deliver == "tablet":
		from ffl_core import signing_terminal
		# After the commit: a tablet polling every 2 s must not fetch a link whose copy
		# is not in the database yet.
		frappe.db.after_commit.add(lambda: signing_terminal.deliver(terminal, token))
		out["terminal"] = terminal
		return out
	if deliver == "qr":
		out.update(sign_url=url, qr_svg=_qr_data_uri(url))
		return out
	_send_link_email(doc, cust, email, url)
	out["email"] = email
	return out


def _send_link_email(doc, cust, email: str, url: str) -> None:
	tpl = frappe.get_doc("Email Template", _LINK_EMAIL)
	ctx = {"customer_name": cust.customer_name, "title": doc.template, "sign_url": url,
		"store_name": company_brand(doc.company).get("name") or "", "hours": TOKEN_HOURS}
	frappe.sendmail(recipients=[email], subject=frappe.render_template(tpl.subject, ctx),
		message=frappe.render_template(tpl.response_html if tpl.use_html else tpl.response, ctx),
		reference_doctype=DOCTYPE, reference_name=doc.name, now=False)


@frappe.whitelist(methods=["POST"])
def void_agreement(name: str, reason: str) -> dict:
	"""Manager voids a copy (sent by mistake, signed by the wrong person).

	A Pending copy's link stops working. The copy is kept, marked Void, and the
	reason goes on its timeline.

	Args:
	    name: Customer Agreement name.
	    reason: Why (required).

	Returns:
	    {"name", "status": "Void"}.
	"""
	frappe.only_for(CUSTOMER_MANAGER_ROLES)
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Enter a reason."), title=_("Reason required"))
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.status == "Void":
		frappe.throw(_("{0} is already void.").format(name))
	doc.update({"status": "Void", "void_reason": reason, "sign_token_hash": None, "token_expires": None})
	doc.flags.via_agreements = True
	doc.save(ignore_permissions=True)
	doc.add_comment("Comment", _("Voided by {0}: {1}").format(escape_html(frappe.session.user), escape_html(reason)))
	return {"name": doc.name, "status": doc.status}


@frappe.whitelist(methods=["POST"])
def record_paper_agreement(name: str, file_url: str, signer_name: str, paper_signed_on: str,
		participant_is_minor: int = 0, signer_relationship: str = "Self") -> dict:
	"""Manager records a copy the customer signed on paper.

	The scan must already be attached to this Customer Agreement; it is made
	private and becomes the signed copy. The copy turns Signed via Paper, its
	expiry counts from the date on the paper, and who recorded it, when, and the
	file go on the timeline. A missing signature can never be waived: without a
	signed scan there is nothing to record.

	Args:
	    name: The Pending Customer Agreement.
	    file_url: The attached scan (PDF or image).
	    signer_name: Name as signed on the paper.
	    paper_signed_on: Date written on the paper (YYYY-MM-DD), not in the future.
	    participant_is_minor: 1 when the participant is under 18 (then a Parent or
	        Guardian signed).
	    signer_relationship: Self, Parent or Guardian.

	Returns:
	    {"name", "status", "expires_on", "signed_pdf"}.
	"""
	frappe.only_for(CUSTOMER_MANAGER_ROLES)
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.status != "Pending":
		frappe.throw(_("Only a Pending agreement can take a paper copy ({0} is {1}).").format(name, doc.status))
	signer_name = (signer_name or "").strip()
	if not signer_name or len(signer_name) > 140:
		frappe.throw(_("Enter the name signed on the paper."))
	day = getdate(paper_signed_on) if paper_signed_on else None
	if not day or day > getdate(nowdate()):
		frappe.throw(_("Enter the date written on the paper (not in the future)."))
	minor = cint(participant_is_minor)
	if signer_relationship not in RELATIONSHIPS or (minor and signer_relationship == "Self"):
		frappe.throw(_("A participant under 18 is signed for by a Parent or Guardian."))
	url = (file_url or "").strip()
	attached = url and frappe.db.exists("File", {"file_url": url, "attached_to_doctype": DOCTYPE,
		"attached_to_name": name})
	if not attached:
		frappe.throw(_("Attach the signed paper to this agreement first."), title=_("Scan needed"))
	private_url = set_file_privacy(url, private=True)
	if not private_url or not private_url.startswith("/private/"):
		frappe.throw(_("The scan could not be made private."))
	with atomic("record paper agreement"):
		doc.update({
			"status": "Signed", "signed_via": "Paper", "signed_on": get_datetime(day),
			"paper_signed_on": day, "signer_name": signer_name, "signer_relationship": signer_relationship,
			"participant_is_minor": minor, "minor_determined_by": "Paper" if minor else None,
			"signed_pdf": private_url, "recorded_by": frappe.session.user,
			"expires_on": _expires_on(doc.template, day),
			"sign_token_hash": None, "token_expires": None,
		})
		doc.flags.via_agreements = True
		doc.save(ignore_permissions=True)
		doc.add_comment("Comment", _("Paper copy recorded by {0} on {1}: {2}, signed {3} by {4}.").format(
			escape_html(frappe.session.user), now_datetime().strftime("%Y-%m-%d %H:%M"),
			escape_html(url.rsplit("/", 1)[-1]), day, escape_html(signer_name)))
	_announce_signed(doc)
	return {"name": doc.name, "status": doc.status, "expires_on": str(doc.expires_on or ""),
		"signed_pdf": doc.signed_pdf}


@frappe.whitelist()
def get_customer_agreements(customer: str) -> dict:
	"""The Agreements section of the Customer form.

	Args:
	    customer: Customer name (document id).

	Returns:
	    {"rows": [{"name", "template", "serial_no": every gun it covers, comma-separated,
	    "status", "signed_on", "signed_via", "expires_on", "signed_pdf"}] newest first,
	    "can_manage": whether the caller may void or record paper copies}.
	"""
	frappe.has_permission("Customer", "read", doc=customer, throw=True)
	rows = frappe.get_all(DOCTYPE, filters={"customer": customer},
		fields=["name", "template", "serial_no", "status", "signed_on", "signed_via", "expires_on", "signed_pdf"],
		order_by="creation desc", limit=50) if frappe.has_permission(DOCTYPE, "read") else []
	guns: dict[str, list] = {}
	for g in frappe.get_all(FIREARM_ROW, filters={"parenttype": DOCTYPE, "parent": ("in", [r.name for r in rows] or [""])},
			fields=["parent", "serial_no"], order_by="idx"):
		guns.setdefault(g.parent, []).append(g.serial_no)
	return {"rows": [{**r, "serial_no": ", ".join(guns.get(r.name) or []) or r.serial_no,
		"signed_on": str(r.signed_on or ""), "expires_on": str(r.expires_on or "")} for r in rows],
		"can_manage": _is_manager()}


@frappe.whitelist(methods=["POST"])
def regenerate_signed_pdf(name: str) -> dict:
	"""Build a signed copy's PDF again (when the background job failed).

	Args:
	    name: A Customer Agreement signed on a tablet or by link.

	Returns:
	    {"signed_pdf": file URL}.
	"""
	frappe.only_for(COUNTER_ROLES)
	frappe.has_permission(DOCTYPE, "read", doc=name, throw=True)
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.status not in ("Signed", "Expired") or doc.signed_via == "Paper" or not doc.signature:
		frappe.throw(_("Only an agreement signed on a tablet or by link has a generated PDF."))
	_attach_pdf(doc)
	return {"signed_pdf": doc.signed_pdf}


# ------------------------------------------------------------------ signing (Guest)


def _by_token(token, for_update: bool = False):
	"""The Pending copy this token opens, or throw :data:`INVALID_LINK`."""
	reason = None
	doc = None
	if not isinstance(token, str) or not _TOKEN_RE.match(token):
		reason = "malformed"
	else:
		name = frappe.db.get_value(DOCTYPE, {"sign_token_hash": _hash(token)}, "name", for_update=for_update)
		if not name:
			reason = "unknown"
		else:
			doc = frappe.get_doc(DOCTYPE, name)
			if doc.status != "Pending":
				reason = f"{name} is {doc.status}"
			elif not doc.token_expires or get_datetime(doc.token_expires) < now_datetime():
				reason = f"{name} link expired"
			elif not frappe.db.get_value(TEMPLATE, doc.template, "active"):
				reason = f"{name} template off"
	if reason:
		_log().info(f"refused signing link from {_request_ip()}: {reason}")
		frappe.throw(_(INVALID_LINK), title=_("Link not valid"))
	return doc


def _request_ip() -> str | None:
	return getattr(frappe.local, "request_ip", None)


def _user_agent() -> str:
	request = getattr(frappe.local, "request", None)
	return ((request.headers.get("User-Agent") if request else "") or "")[:500]


def _minor_by_dob(doc) -> bool | None:
	"""True/False from the customer's date of birth, None when none is kept."""
	dob = frappe.db.get_value("Customer", doc.customer, "date_of_birth") \
		if frappe.db.has_column("Customer", "date_of_birth") else None
	return _age_on(dob, nowdate()) < ADULT_AGE if dob else None


# Rate limits: per link (IP + token), and a wide per-IP cap. A store's tablets and
# walk-ins' phones share one public IP on a busy range day, so the per-IP cap is
# sized for the whole shop; the per-link limit is what stops one link being hammered.
# (Guessing a token is not the threat: it is 256 random bits.)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=600, seconds=3600)
@rate_limit(key="token", limit=30, seconds=3600)
def load_for_signing(token: str) -> dict:
	"""What the signing page shows for a one-time link (Guest).

	Only the customer's name, the agreement's title and text, and the store's
	name — nothing else about the customer. Every invalid, used, void or
	expired link gets the same error. 30 calls an hour per link and 600 per IP
	address. A copy sent before its template's text changed is shown (and from
	then on carries) the current text.

	Args:
	    token: The token from the link's ``#t=`` fragment.

	Returns:
	    {"customer_name", "title", "body_html", "store_name",
	    "minors_need_guardian", "minor_by_dob": true / false from the customer's
	    date of birth, or null when the store keeps none (the page then asks)}.
	"""
	doc = _by_token(token)
	_refresh_text(doc)
	needs_guardian = bool(frappe.db.get_value(TEMPLATE, doc.template, "minors_need_guardian"))
	return {
		"customer_name": frappe.db.get_value("Customer", doc.customer, "customer_name") or doc.customer_name,
		"title": doc.template,
		"body_html": sanitize_html(doc.body_snapshot or ""),
		"store_name": company_brand(doc.company).get("name") or "",
		"minors_need_guardian": needs_guardian,
		"minor_by_dob": _minor_by_dob(doc) if needs_guardian else None,
	}


def _refresh_text(doc) -> bool:
	"""Re-render a Pending copy whose template text changed since it was sent.

	Returns True when the text changed (the copy now carries the current version)."""
	tpl = frappe.get_doc(TEMPLATE, doc.template)
	if cint(doc.template_version) == cint(tpl.version or 1):
		return False
	cust = frappe.get_doc("Customer", doc.customer)
	firearm = [firearm_context(r.serial_no, r.price) for r in doc.get("firearms") or []] or None
	doc.db_set({"body_snapshot": render_body(tpl, cust, doc.company, firearm), "template_version": tpl.version or 1},
		update_modified=False)
	return True


def _decode_signature(signature) -> bytes:
	"""The PNG bytes of a data-URL signature, or throw. Cheap checks first."""
	if not isinstance(signature, str) or len(signature) > MAX_SIGNATURE_CHARS:
		frappe.throw(_("The signature is too large."))
	raw = signature.split(",", 1)[1] if signature.startswith("data:") else signature
	try:
		data = base64.b64decode(raw, validate=True)
	except (binascii.Error, ValueError):
		frappe.throw(_("The signature could not be read."))
	if len(data) > MAX_SIGNATURE_BYTES:
		frappe.throw(_("The signature is too large."))
	if not data.startswith(_PNG_MAGIC):
		frappe.throw(_("The signature could not be read."))
	from PIL import Image, ImageOps

	try:
		with Image.open(io.BytesIO(data)) as img:
			if img.format != "PNG" or max(img.size) > MAX_SIGNATURE_PX:
				raise ValueError("format/size")
			img.verify()
		with Image.open(io.BytesIO(data)) as img:
			img.load()
			flat = Image.new("RGBA", img.size, "white")
			flat.alpha_composite(img.convert("RGBA"))
			ink = ImageOps.invert(flat.convert("L")).point(lambda v: 255 if v > 48 else 0).getbbox()
	except Exception:
		frappe.throw(_("The signature could not be read."))
	if not ink:
		frappe.throw(_("Please sign in the box."))
	return data


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=300, seconds=3600)
@rate_limit(key="token", limit=10, seconds=3600)
def submit_signature(token: str, signature: str, agreed: int, signer_name: str,
		participant_is_minor: int = 0, signer_relationship: str = "Self",
		terminal_secret: str | None = None) -> dict:
	"""Sign an agreement through a one-time link (Guest).

	Checked before anything is written: the "I agree" tick, the typed name, the
	token (same error for every invalid one), the signature (a PNG of at most
	500 KB and 2000 px with ink on it) and the age rule. A link opened as a QR
	code at the counter, or on a paired signing tablet (its device key sent as
	``terminal_secret``; the copy records which tablet), is recorded as signed on
	a Tablet, an emailed one as Remote. The link is used up; the signed PDF is built and emailed afterwards.
	When the template's text changed after the page was opened, nothing is signed
	and the signer is asked to reload and read the new text. 10 calls an hour per
	link and 300 per IP address.

	Args:
	    token: The token from the link's ``#t=`` fragment.
	    signature: The drawn signature as a PNG data URL.
	    agreed: 1 — the signer ticked "I agree".
	    signer_name: Name typed by the signer.
	    participant_is_minor: 1 when the signer declares the participant is under
	        18 (ignored when the store keeps the customer's date of birth).
	    signer_relationship: Self, or Parent / Guardian when signing for a minor.
	    terminal_secret: The signing tablet's device key, when signed on one.

	Returns:
	    {"signed": true, "store_name"}.
	"""
	if not cint(agreed):
		frappe.throw(_("Tick \"I agree\" to sign."))
	signer_name = (signer_name or "").strip() if isinstance(signer_name, str) else ""
	if not signer_name or len(signer_name) > 140:
		frappe.throw(_("Type your full name."))
	if not isinstance(signature, str) or len(signature) > MAX_SIGNATURE_CHARS:
		frappe.throw(_("The signature is too large."))
	doc = _by_token(token, for_update=True)
	if _refresh_text(doc):
		commit()   # keep the new text past the error below: the reload shows it
		frappe.throw(_("This agreement's text was updated while the page was open. Reload the page and "
			"read it again before signing."), title=_("Text updated"))
	png = _decode_signature(signature)
	needs_guardian = bool(frappe.db.get_value(TEMPLATE, doc.template, "minors_need_guardian"))
	minor, determined = 0, None
	if needs_guardian:
		by_dob = _minor_by_dob(doc)
		if by_dob is None:
			minor, determined = cint(participant_is_minor), "Self-declared"
		else:
			minor, determined = int(by_dob), "Date of Birth"
	if signer_relationship not in RELATIONSHIPS:
		frappe.throw(_("Choose who is signing."))
	if minor and signer_relationship == "Self":
		frappe.throw(_("The participant is under 18: a parent or guardian must sign, with their own name."))
	from ffl_core.signing_terminal import terminal_for_secret
	terminal = terminal_for_secret(terminal_secret)
	via = "Tablet" if terminal else \
		"Remote" if doc.delivery_channel == "Email" and frappe.session.user == "Guest" else "Tablet"
	today = nowdate()
	with guest_job_elevated(), atomic("sign agreement"):
		f = frappe.get_doc({"doctype": "File", "file_name": f"{doc.name}-signature.png", "is_private": 1,
			"attached_to_doctype": DOCTYPE, "attached_to_name": doc.name, "attached_to_field": "signature",
			"content": png})
		f.flags.ignore_permissions = True
		f.insert()
		doc.update({
			"status": "Signed", "signed_on": now_datetime(), "signed_via": via,
			"signer_name": signer_name, "signer_relationship": signer_relationship,
			"participant_is_minor": minor, "minor_determined_by": determined if minor else None,
			"signature": f.file_url, "expires_on": _expires_on(doc.template, today),
			"signer_ip": _request_ip(),
			"signer_user_agent": _user_agent(),
			"signing_terminal": terminal,
			"sign_token_hash": None, "token_expires": None,
		})
		doc.flags.via_agreements = True
		doc.save(ignore_permissions=True)
	frappe.enqueue("ffl_core.agreements.finish_signed_agreement", queue="short",
		enqueue_after_commit=True, name=doc.name)
	_announce_signed(doc)
	return {"signed": True, "store_name": company_brand(doc.company).get("name") or ""}


def _announce_signed(doc) -> None:
	"""Tell open staff pages (Pending Order) once the signature commits. Only the
	Customer, template and serial; the room is for users who may read agreements."""
	from frappe.realtime import get_doctype_room

	frappe.publish_realtime("customer_agreement_signed", {"customer": doc.customer, "agreement": doc.template,
		"serial_no": doc.get("serial_no") or None}, room=get_doctype_room(DOCTYPE), after_commit=True)


# ------------------------------------------------------------------ after signing


def _pdf_bytes(doc) -> bytes:
	from frappe.utils.pdf import get_pdf

	sig = frappe.get_doc("File", {"file_url": doc.signature}).get_content() if doc.signature else b""
	html = frappe.render_template(_PDF_TEMPLATE, {
		"doc": doc, "brand": company_brand(doc.company), "body_html": sanitize_html(doc.body_snapshot or ""),
		"signature_uri": "data:image/png;base64," + base64.b64encode(sig).decode() if sig else "",
		"signed_on": str(get_datetime(doc.signed_on).strftime("%Y-%m-%d %H:%M")) if doc.signed_on else "",
		# The store's witness: whoever sent it to be signed (or recorded the paper copy).
		"witness": get_fullname(doc.get("recorded_by") or doc.get("requested_by")) if (doc.get("recorded_by")
			or doc.get("requested_by")) else "",
	})
	return get_pdf(html)


def rebuild_signed_pdf(name: str) -> None:
	"""Background: build a signed copy's PDF again in place (after a sale took it)."""
	with guest_job_elevated():
		try:
			_attach_pdf(frappe.get_doc(DOCTYPE, name), replace=True)
		except Exception:
			frappe.log_error(title=f"Agreement PDF rebuild failed: {name}")


def _attach_pdf(doc, replace: bool = False) -> bytes:
	pdf = _pdf_bytes(doc)
	previous = doc.get("signed_pdf") if replace else None
	f = frappe.get_doc({"doctype": "File", "file_name": f"{doc.name}.pdf", "is_private": 1,
		"attached_to_doctype": DOCTYPE, "attached_to_name": doc.name, "attached_to_field": "signed_pdf",
		"content": pdf})
	f.flags.ignore_permissions = True
	f.insert()
	doc.db_set("signed_pdf", f.file_url, update_modified=False)
	# Only once the new copy is saved: deleting a File removes it from disk at once,
	# which no rollback brings back.
	if previous and previous != f.file_url:
		for old in frappe.get_all("File", filters={"file_url": previous, "attached_to_doctype": DOCTYPE,
				"attached_to_name": doc.name}, pluck="name"):
			frappe.delete_doc("File", old, ignore_permissions=True, force=True)
	return pdf


def finish_signed_agreement(name: str) -> None:
	"""Background, after a signature commits: attach the PDF, email it when the
	customer has an address. A failure is logged; the signature stands and the
	form's Regenerate PDF builds it again."""
	with guest_job_elevated():
		doc = frappe.get_doc(DOCTYPE, name)
		try:
			pdf = _attach_pdf(doc)
		except Exception:
			frappe.log_error(title=f"Agreement PDF failed: {name}")
			return
		# The PDF stands even when the email cannot go (no outgoing account, bad address).
		commit()
		email = (frappe.db.get_value("Customer", doc.customer, "email_id") or "").strip()
		if not email:
			return
		try:
			tpl = frappe.get_doc("Email Template", _COPY_EMAIL)
			ctx = {"customer_name": doc.customer_name, "title": doc.template,
				"store_name": company_brand(doc.company).get("name") or ""}
			frappe.sendmail(recipients=[email], subject=frappe.render_template(tpl.subject, ctx),
				message=frappe.render_template(tpl.response_html if tpl.use_html else tpl.response, ctx),
				reference_doctype=DOCTYPE, reference_name=name,
				attachments=[{"fname": f"{name}.pdf", "fcontent": pdf}], now=False)
		except Exception:
			frappe.log_error(title=f"Agreement signed copy email failed: {name}")


def expire_agreements() -> None:
	"""Daily: signed copies past ``expires_on`` become Expired."""
	# "is set" too: frappe compares a blank date as the earliest one (ifnull), which
	# would expire every never-expiring copy.
	for name in frappe.get_all(DOCTYPE, filters=[["status", "=", "Signed"], ["expires_on", "is", "set"],
			["expires_on", "<", nowdate()]], pluck="name"):
		frappe.db.set_value(DOCTYPE, name, "status", "Expired")
