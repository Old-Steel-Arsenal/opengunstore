"""The counter's view of a customer: flags panel, ban gate, ID scan lookup, duplicates.

Built on the profile fields from ``setup/customer_profile.py`` (status, alerts,
ID documents, family; rules in ``customer_profile.py``):

* **Flags** — :func:`get_customer_flags` is what the shared counter panel
  (``public/js/customer_flags.js``) shows when a customer is picked on the POS,
  Create Order, a trade-in or Receive Goods: status, active alerts, tags, family,
  IDs that expire within :data:`ID_EXPIRY_WARN_DAYS`, and where the customer
  stands with the agreements shown on the panel (``agreements.counter_panel_rows``).
  Never an ID number.
* **Ban gate** — a Banned customer cannot be sold to at the counter.
  :func:`refuse_banned_customer` runs on POS Invoice, Sales Invoice and Sales
  Order ``validate`` and refuses a NEW document for a Banned customer (or an
  existing draft switched to one) unless a manager has opened a window with
  :func:`override_customer_ban` (:data:`OVERRIDE_MINUTES`). A document created
  under the window keeps going after it closes — a card charged on a held draft
  must still be able to complete. Left alone on purpose (user decision): returns,
  POS consolidation invoices and channel mirrors (web / GunBroker sales booked
  after the goods left — :mod:`ffl_core.compliance.channel_invoice`), and every
  FFL Disposition (the bound book records what happened; the sale is decided
  upstream). Web orders from a Banned buyer are marked on the Pending Order page
  instead (:func:`banned_order_rows`).
* **Who may ban** — only a manager (``CUSTOMER_MANAGER_ROLES``) may set or lift
  Banned or edit a Banned customer's reason; the override fields are written by
  :func:`override_customer_ban` alone (:func:`guard_ban_fields`, called from
  ``customer_profile.validate_customer``).
* **ID scan** — :func:`find_customers_by_id_number`: a licence scanned at the
  register finds the customer holding that ID number (only while the store keeps
  ID numbers, FFL Settings → Store Customer ID Numbers).
* **Duplicates** — :func:`find_possible_duplicates`: before a new customer is
  saved, the customers already holding the same email, phone or ID number.
"""
from __future__ import annotations

import re
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import add_days, escape_html, get_datetime, getdate, now_datetime, nowdate

from ffl_core.agreements import counter_panel_rows
from ffl_core.compliance.channel_invoice import is_channel_mirrored_invoice
from ffl_core.customer_profile import ACTIVE, BANNED, find_customer_by_email, find_customer_by_gb_id
from ffl_core.feature_gate import customer_id_numbers_enabled
from ffl_core.roles import CUSTOMER_MANAGER_ROLES

OVERRIDE_MINUTES = 30
ID_EXPIRY_WARN_DAYS = 30
_MAX_MATCHES = 5


def is_manager() -> bool:
	return bool(set(frappe.get_roles()) & set(CUSTOMER_MANAGER_ROLES))


def ban_window_open(row) -> bool:
	"""Whether a manager's override is still open for this customer row/doc."""
	until = row.get("ban_override_until")
	return bool(until) and get_datetime(until) > now_datetime()


def is_blocked(customer: str | None) -> bool:
	"""True when *customer* is Banned and no manager override is open."""
	if not customer:
		return False
	row = frappe.db.get_value("Customer", customer,
		["customer_status", "ban_override_until"], as_dict=True)
	return bool(row) and row.customer_status == BANNED and not ban_window_open(row)


# ---------------------------------------------------------------- doc events


def guard_ban_fields(doc) -> None:
	"""Customer.validate: Banned is a manager's call; the override fields are not editable.

	A clerk who can edit a Customer must not be able to lift a ban (or ban someone)
	or open an override window by saving the form or calling REST. The override
	fields always keep their stored value on a save (only
	:func:`override_customer_ban` writes them); changing to or from Banned, or a
	Banned customer's reason, needs a manager (Administrator holds every role).
	"""
	before = None if doc.is_new() else doc.get_doc_before_save()
	# Put back whatever is stored: a form loaded before an override was opened
	# would otherwise erase it on save (the override does not bump `modified`).
	for field in ("ban_override_until", "ban_override_by"):
		doc.set(field, before.get(field) if before else None)
	if is_manager():
		return
	was = before.get("customer_status") if before else None
	now = doc.get("customer_status")
	if BANNED in (was, now) and was != now:
		frappe.throw(_("Only a manager (Sales Manager) can mark a customer as Banned or lift a "
			"ban."), frappe.PermissionError, title=_("Manager required"))
	if was == BANNED and now == BANNED and \
			(doc.get("status_reason") or "") != (before.get("status_reason") or ""):
		frappe.throw(_("Only a manager (Sales Manager) can change a Banned customer's reason."),
			frappe.PermissionError, title=_("Manager required"))


def refuse_banned_customer(doc, method=None) -> None:
	"""POS Invoice / Sales Invoice / Sales Order validate: no counter sale to a Banned customer.

	Checked when the document is new or its customer changed; see the module
	docstring for what is left alone and why.
	"""
	customer = doc.get("customer")
	if not customer or doc.get("is_return") or doc.get("is_consolidated"):
		return
	if frappe.flags.in_import or frappe.flags.in_patch or frappe.flags.in_migrate:
		return
	if is_channel_mirrored_invoice(doc):
		# Booked after the goods left, so never refused; leave a note on the buyer.
		if doc.is_new() and frappe.db.get_value("Customer", customer, "customer_status") == BANNED:
			ref = doc.get("woo_online_order") or doc.get("gunbroker_order")
			frappe.get_doc("Customer", customer).add_comment("Comment", _(
				"Online order {0} was booked to this customer while Banned.").format(escape_html(ref)))
		return
	if not doc.is_new():
		before = doc.get_doc_before_save()
		if before and before.get("customer") == customer:
			return
	if is_blocked(customer):
		name = frappe.db.get_value("Customer", customer, "customer_name") or customer
		frappe.throw(_("{0} is Banned. A manager must open an override first: pick the "
			"customer on a counter screen (register or Create Order) and choose Override ban "
			"(30 min), then save this {1} again.").format(
			frappe.bold(escape_html(name)), _(doc.doctype)), title=_("Banned customer"))


# ------------------------------------------------------------------ endpoints


@frappe.whitelist()
def get_customer_flags(customer: str) -> dict:
	"""What the counter panel shows when this customer is picked.

	Args:
	    customer: Customer name (document id).

	Returns:
	    {"name", "customer_name", "status", "status_reason" (only when not Active),
	    "banned": Banned with no open override, "override_until": end of an open
	    manager override or None, "can_override": whether the caller may override,
	    "alerts": [{"message", "created_by", "created_on"}] (active ones, oldest
	    first), "tags": [str], "family": {"primary": {"name", "customer_name"} or
	    None, "relationship", "members": int}, "ids": [{"id_type", "expiry_date",
	    "expired": bool}] for ID documents expired or expiring within 30 days,
	    "agreements": [{"template", "state", "expires_on", "agreement"}] for each
	    active Agreement Template shown on the counter panel — state is signed,
	    expiring (within 30 days), expired, pending or missing}.
	    Never carries an ID number or date of birth.
	"""
	frappe.has_permission("Customer", "read", doc=customer, throw=True)
	doc = frappe.get_doc("Customer", customer)
	status = doc.get("customer_status") or ACTIVE
	window = status == BANNED and ban_window_open(doc)
	primary = None
	if doc.get("primary_customer"):
		primary = {"name": doc.primary_customer, "customer_name": frappe.db.get_value(
			"Customer", doc.primary_customer, "customer_name")}
	return {
		"name": doc.name,
		"customer_name": doc.customer_name,
		"status": status,
		"status_reason": (doc.get("status_reason") or "") if status != ACTIVE else "",
		"banned": status == BANNED and not window,
		"override_until": str(doc.ban_override_until) if window else None,
		"can_override": is_manager(),
		"alerts": [{"message": a.message, "created_by": a.created_by,
			"created_on": str(a.created_on or "")}
			for a in doc.get("customer_alerts") or [] if a.active and (a.message or "").strip()],
		"tags": [t for t in (doc.get("_user_tags") or "").split(",") if t.strip()],
		"family": {"primary": primary,
			"relationship": doc.get("relationship") if primary else None,
			"members": frappe.db.count("Customer", {"primary_customer": doc.name})},
		"ids": expiring_ids(doc.get("customer_ids") or []),
		"agreements": counter_panel_rows(doc.name),
	}


def expiring_ids(rows, today=None) -> list[dict]:
	"""ID documents already expired or expiring within ``ID_EXPIRY_WARN_DAYS``."""
	today = getdate(today or nowdate())
	horizon = getdate(add_days(today, ID_EXPIRY_WARN_DAYS))
	out = []
	for row in rows:
		if not row.get("expiry_date"):
			continue
		expiry = getdate(row.expiry_date)
		if expiry <= horizon:
			out.append({"id_type": row.id_type, "expiry_date": str(expiry), "expired": expiry < today})
	return out


@frappe.whitelist(methods=["POST"])
def override_customer_ban(customer: str, reason: str) -> dict:
	"""Manager lets a Banned customer be served for the next 30 minutes.

	Opens the window every counter screen and the server-side sale gate read
	(``ban_override_until`` / ``ban_override_by``) and records who and why on the
	customer's timeline. The customer stays Banned.

	Args:
	    customer: Customer name (document id); must be Banned.
	    reason: Why the sale is allowed (required, kept on the timeline).

	Returns:
	    {"customer": name, "override_until": the window's end, "by": the manager}.
	"""
	frappe.only_for(CUSTOMER_MANAGER_ROLES)
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Enter a reason for serving this Banned customer."), title=_("Reason required"))
	status = frappe.db.get_value("Customer", customer, "customer_status")
	if status is None:
		frappe.throw(_("Customer {0} not found.").format(escape_html(customer)), frappe.DoesNotExistError)
	if status != BANNED:
		frappe.throw(_("{0} is not Banned.").format(escape_html(customer)))
	until = now_datetime() + timedelta(minutes=OVERRIDE_MINUTES)
	# db.set_value, not save: no validate (guard_ban_fields refuses these fields
	# on a save), no modified bump on the customer.
	frappe.db.set_value("Customer", customer, {"ban_override_until": until,
		"ban_override_by": frappe.session.user}, update_modified=False)
	frappe.get_doc("Customer", customer).add_comment("Comment", _(
		"Ban overridden by {0} until {1}: {2}").format(escape_html(frappe.session.user),
		until.strftime("%Y-%m-%d %H:%M"), escape_html(reason)))
	return {"customer": customer, "override_until": str(until), "by": frappe.session.user}


def _id_key(value: str | None) -> str:
	"""An ID number compared without case, spaces or dashes."""
	return re.sub(r"[\s\-]", "", (value or "")).upper()


@frappe.whitelist()
def find_customers_by_id_number(id_number: str) -> list[dict]:
	"""Customers holding this ID number (a licence scanned at the register).

	Only while FFL Settings → Store Customer ID Numbers is on (otherwise none are
	stored, and the answer is an empty list). Compared without case, spaces or dashes.

	Args:
	    id_number: The number read from the ID.

	Returns:
	    [{"name", "customer_name", "customer_status"}], at most 5.
	"""
	frappe.has_permission("Customer", "read", throw=True)
	key = _id_key(id_number)
	if not key or not customer_id_numbers_enabled():
		return []
	# lazy: REPLACE() in the WHERE scans every ID row (fine at 140k customers, a
	# few ms); normalise on save + index id_number if it ever shows up in a profile.
	names = frappe.db.sql("""select distinct parent from `tabCustomer ID Document`
		where parenttype = 'Customer' and parentfield = 'customer_ids'
		and upper(replace(replace(id_number, ' ', ''), '-', '')) = %s
		limit %s""", (key, _MAX_MATCHES), pluck=True)
	return _customer_rows(names)


def _phone_key(value: str | None) -> str:
	"""The last 10 digits of a phone number (drops +1, punctuation)."""
	digits = re.sub(r"\D", "", value or "")
	return digits[-10:] if len(digits) >= 7 else ""


@frappe.whitelist()
def find_possible_duplicates(email: str | None = None, phone: str | None = None,
		id_number: str | None = None) -> list[dict]:
	"""Existing customers a new one may duplicate, for the New Customer dialog.

	Matches the customer's email (or an email merged into it), mobile number
	(last 10 digits) and — while the store keeps ID numbers — an ID number.

	Args:
	    email: Email typed in the dialog.
	    phone: Mobile number typed in the dialog.
	    id_number: Driver licence number from a scan (ignored when ID numbers are off).

	Returns:
	    [{"name", "customer_name", "customer_status", "matched": ["email" | "phone" |
	    "id_number"]}], at most 5.
	"""
	frappe.has_permission("Customer", "read", throw=True)
	matched: dict[str, list[str]] = {}

	def add(names, why):
		for n in names:
			matched.setdefault(n, [])
			if why not in matched[n]:
				matched[n].append(why)

	email = (email or "").strip()
	if email:
		hit = find_customer_by_email(email)
		if hit:
			add([hit.name], "email")
	key = _phone_key(phone)
	if key:
		# lazy: REGEXP_REPLACE scans every customer's mobile_no (~100 ms at 140k);
		# a normalised, indexed phone column is the upgrade path.
		add(frappe.db.sql("""select name from `tabCustomer`
			where regexp_replace(ifnull(mobile_no, ''), '[^0-9]', '') like %s limit %s""",
			("%" + key, _MAX_MATCHES), pluck=True), "phone")
	if id_number:
		add([r["name"] for r in find_customers_by_id_number(id_number)], "id_number")
	rows = _customer_rows(list(matched)[:_MAX_MATCHES])
	for r in rows:
		r["matched"] = matched[r["name"]]
	return rows


def _customer_rows(names: list[str]) -> list[dict]:
	if not names:
		return []
	rows = frappe.get_all("Customer", filters={"name": ("in", names)},
		fields=["name", "customer_name", "customer_status"])
	order = {n: i for i, n in enumerate(names)}
	return sorted(rows, key=lambda r: order.get(r.name, 0))


@frappe.whitelist()
def banned_order_rows(woo_orders: list | str | None = None, gb_orders: list | str | None = None,
		customers: list | str | None = None) -> dict:
	"""Which Pending Order rows belong to a Banned customer, for the page's red badge.

	A web or GunBroker order is booked to a customer only once it has shipped, so
	its buyer is looked up the way that booking will: a Woo order by its billing
	email (customer email, then merged-email alias); a GunBroker order by its linked
	customer, then the buyer's GunBroker id (field, then alias), then email.

	Args:
	    woo_orders: Woo Online Order names on the page.
	    gb_orders: GunBroker Order names on the page.
	    customers: Customer names of the counter rows.

	Returns:
	    {"woo": [names], "gb": [names], "customers": [names]} — only the Banned ones.
	"""
	frappe.has_permission("Customer", "read", throw=True)
	woo_orders, gb_orders, customers = (frappe.parse_json(v) if isinstance(v, str) else (v or [])
		for v in (woo_orders, gb_orders, customers))
	banned = set(frappe.get_all("Customer", filters={"customer_status": BANNED}, pluck="name"))
	if not banned:
		return {"woo": [], "gb": [], "customers": []}

	def email_owner(email):
		hit = find_customer_by_email(email)
		return hit.name if hit else None

	woo = []
	if woo_orders and frappe.db.exists("DocType", "Woo Online Order"):
		for o in frappe.get_all("Woo Online Order", filters={"name": ("in", woo_orders)},
				fields=["name", "customer_email"]):
			if email_owner(o.customer_email) in banned:
				woo.append(o.name)
	gb = []
	if gb_orders and frappe.db.exists("DocType", "GunBroker Order"):
		has_gb_id = frappe.db.has_column("Customer", "gb_user_id")
		for o in frappe.get_all("GunBroker Order", filters={"name": ("in", gb_orders)},
				fields=["name", "customer", "buyer_user_id", "customer_email"]):
			uid = (o.buyer_user_id or "").strip()
			owner = o.customer \
				or (uid and has_gb_id and frappe.db.get_value("Customer", {"gb_user_id": uid}, "name")) \
				or (uid and find_customer_by_gb_id(uid)) \
				or email_owner(o.customer_email)
			if owner in banned:
				gb.append(o.name)
	return {"woo": woo, "gb": gb, "customers": [c for c in customers or [] if c in banned]}
