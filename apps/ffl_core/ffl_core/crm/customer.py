"""A CRM Deal's POS customer: finding or making it, what staff see about it, and opening its order.

:func:`ensure_customer` answers with the customer once the deal has one. Otherwise it returns the
customers the deal's email, phone or full name match for the employee to confirm, or makes a new one when asked
to. A deal whose Contact is linked to exactly one Customer takes it; a Contact linked to several
leaves the choice to the employee, with those customers among the matches. A Contact's email counts here (``customer_flags.find_possible_duplicates`` reads the Customer's
own): the Woo and GunBroker resolvers must keep ignoring Contact emails, because they book an order
with no one to confirm the match, so this lookup stays local.

:func:`customer_summary` is the Deal page's customer card: flags and alerts, recent and open orders,
lifetime spend on the Sales Report's Net Sales basis, FFL status. It carries no ID number, date of
birth, 4473 or NICS. The HTML is built here with every value escaped: CRM's dialog renders HTML as it
gets it.

:func:`open_order` saves a Create Order draft. The page prices it the way it prices a line picked
there, and re-checks every gun and the buyer when it loads the draft. A Banned customer is shown,
never hidden: the page's own gate and ``customer_flags.refuse_banned_customer`` decide the sale.
"""
from __future__ import annotations

from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, fmt_money, formatdate, getdate, nowdate

from ffl_core import customer_flags, pricing
from ffl_core.crm import TITLE, pos_link_on
from ffl_core.crm.deal import SERIAL_FIELD
from ffl_core.crm.holds import card_lines
from ffl_core.crm.deals import customer_of
from ffl_core.crm.mail import contacts_with_email
from ffl_core.marketing import customers as marketing_customers
from ffl_core.crm.item import PRODUCT
from ffl_core.customer_profile import ACTIVE, BANNED, find_customer_by_email

CARD_FIELD = "ffl_customer_card"
RECENT_ORDERS = 5
_FLAG_KEYS = ("status", "status_reason", "banned", "override_until", "alerts", "tags")


# ----------------------------------------------------------------- the deal's customer


def linked_customer(deal) -> str | None:
	"""The deal's Customer. A name that no longer exists (a merge or a rename does not reach this
	Data field) reads as none, and the deal is matched again."""
	customer = deal.get("erpnext_customer")
	return customer if customer and frappe.db.exists("Customer", customer) else None


def _primary_contact(deal) -> str | None:
	rows = deal.get("contacts") or []
	row = next((r for r in rows if r.is_primary), rows[0] if rows else None)
	return row.contact if row else None


def _customers_of_contacts(contacts) -> list[str]:
	if not contacts:
		return []
	return frappe.get_all("Dynamic Link", filters={"parenttype": "Contact", "parent": ["in", list(contacts)],
		"link_doctype": "Customer"}, pluck="link_name", distinct=True, order_by="link_name")


def _customers_by_contact_email(email: str | None) -> list[str]:
	email = (email or "").strip()
	return _customers_of_contacts(contacts_with_email(email)) if email else []


def _customers_by_name(full_name: str) -> list[str]:
	"""Customers with this full name (first and last word). One word is no name."""
	words = full_name.split()
	return customer_flags.find_customers_by_name(words[0], words[-1], full_name=True) if len(words) > 1 else []


def _safe_flags(customer: str) -> dict:
	flags = customer_flags.get_customer_flags(customer)
	return {key: flags[key] for key in _FLAG_KEYS}


def find_matches(deal, person: str | None = None) -> list[dict]:
	"""Customers the deal's email, phone or full name match, each with its flags: [{"name",
	"customer_name", "customer_status", "matched", "banned", "status_reason", "alerts"}]. The
	customers the deal's primary Contact is linked to are in it too, whatever their email and phone
	say. A customer found only by name is marked "name only"; ``person`` is the buyer's name when
	the caller already has it (:func:`_person_name`)."""
	rows = customer_flags.find_possible_duplicates(email=deal.get("email"),
		phone=deal.get("mobile_no") or deal.get("phone"))
	by_name = {row["name"]: row for row in rows}
	contact = _primary_contact(deal)
	for why, customers in (("contact email", _customers_by_contact_email(deal.get("email"))),
			("contact link", _customers_of_contacts([contact] if contact else [])),
			("name only", _customers_by_name(_person_name(deal, contact) if person is None else person))):
		for customer in customers:
			if customer not in by_name and frappe.db.exists("Customer", customer):
				row = frappe.db.get_value("Customer", customer, ["name", "customer_name", "customer_status"], as_dict=True)
				row["matched"] = [why]
				by_name[customer] = row
	out = []
	for row in by_name.values():
		flags = _safe_flags(row["name"])
		out.append({"name": row["name"], "customer_name": row["customer_name"],
			"customer_status": row["customer_status"] or ACTIVE, "matched": row["matched"],
			"banned": flags["banned"], "status_reason": flags["status_reason"], "alerts": flags["alerts"]})
	return out


def _person_name(deal, contact: str | None) -> str:
	"""The buyer's own name: the primary Contact's, else the deal's first + last name."""
	return (contact and frappe.db.get_value("Contact", contact, "full_name")) \
		or " ".join(p for p in (deal.get("first_name"), deal.get("last_name")) if p)


def _new_customer_values(deal, person: str) -> dict:
	name = person or deal.get("lead_name") or deal.get("organization")
	return {"customer_name": name or "", "email_id": deal.get("email") or "",
		"phone": deal.get("mobile_no") or deal.get("phone") or ""}


def ensure_customer(deal, customer: str | None = None, create: bool = False) -> dict:
	"""The deal's customer, or what the employee has to decide.

	Args:
	    deal: The CRM Deal document.
	    customer: The Customer the employee confirmed.
	    create: The employee confirmed making a new Customer from the deal's contact.

	Returns:
	    {"customer": name} once there is one; else {"customer": None, "matches": find_matches,
	    "new": the values a new Customer would take, "html": the dialog's text}.
	"""
	if customer:
		if not frappe.db.exists("Customer", customer):
			frappe.throw(_("This customer no longer exists."), frappe.DoesNotExistError)
		frappe.has_permission("Customer", "read", doc=customer, throw=True)
		return _link(deal, customer)
	existing = linked_customer(deal)
	if existing:
		return {"customer": existing}
	contact = _primary_contact(deal)
	linked = contact and customer_of(contact)
	if linked:
		return _link(deal, linked)   # several: none here, the employee chooses among find_matches
	person = _person_name(deal, contact)
	values = _new_customer_values(deal, person)
	if create:
		return _link(deal, _create(values, contact))
	matches = find_matches(deal, person)
	return {"customer": None, "matches": matches, "new": values, "html": _matches_html(matches, values)}


def _create(values: dict, contact: str | None) -> str:
	from ffl_core.api.manual_order import quick_create_individual

	name = quick_create_individual(values)["name"]
	if contact:
		# quick_create_individual made a Contact: the deal's own, with the same person's mail on its
		# timeline, takes its place.
		made = frappe.db.get_value("Customer", name, "customer_primary_contact")
		frappe.db.set_value("Customer", name, "customer_primary_contact", contact)
		frappe.delete_doc("Contact", made, ignore_permissions=True)
		_keep_phone(contact, values["phone"])
	return name


def _keep_phone(contact: str, phone: str) -> None:
	"""The deal's Contact takes the phone the new Customer was made with, so a lookup by phone through
	the Contact still finds the customer. A phone it holds is left as it is, primary mobile too."""
	if not phone:
		return
	doc = frappe.get_doc("Contact", contact)
	if any(row.phone == phone for row in doc.phone_nos):
		return
	doc.add_phone(phone, is_primary_mobile_no=0 if any(row.is_primary_mobile_no for row in doc.phone_nos) else 1)
	doc.flags.ignore_permissions = True   # the caller holds the deal; ERPNext's Contact rights are not CRM's
	doc.save()


def _link(deal, customer: str) -> dict:
	# CRM writes this field the same way (erpnext_crm_settings.create_customer_from_deal).
	frappe.db.set_value("CRM Deal", deal.name, "erpnext_customer", customer)
	deal.erpnext_customer = customer
	contact = _primary_contact(deal)
	if contact and not frappe.db.exists("Dynamic Link", {"parenttype": "Contact", "parent": contact,
			"link_doctype": "Customer", "link_name": customer}):
		doc = frappe.get_doc("Contact", contact)
		doc.append("links", {"link_doctype": "Customer", "link_name": customer})
		doc.flags.ignore_permissions = True   # the caller holds the deal; ERPNext's Contact rights are not CRM's
		doc.save()
	if contact and not frappe.db.get_value("Customer", customer, "customer_primary_contact"):
		# A customer with no primary Contact takes the deal's: its email and mobile are what the
		# Customer shows and what an order's lookup by email reads.
		email, mobile = frappe.db.get_value("Contact", contact, ["email_id", "mobile_no"])
		frappe.db.set_value("Customer", customer, {"customer_primary_contact": contact,
			"email_id": email or "", "mobile_no": mobile or ""})
		frappe.db.set_value("Contact", contact, "is_primary_contact", 1)
	return {"customer": customer}


# ----------------------------------------------------------------- linking on its own

AUTO_LINK_JOB = "ffl_core.crm.customer.auto_link_deals"


def sole_customer_by_email(email: str | None) -> str | None:
	"""The one Customer whose own email is *email* (or, with none, the one a merge alias names; an
	alias on a dealer does not count, as in the web order resolver). None when there is no such
	customer or there are several: then the employee chooses."""
	email = (email or "").strip()
	if not email:
		return None
	own = frappe.get_all("Customer", filters={"email_id": email}, pluck="name", limit=2)
	if own:
		return own[0] if len(own) == 1 else None
	hit = find_customer_by_email(email)
	return hit.name if hit and not (hit.get("via_alias") and hit.get("is_ffl_dealer")) else None


def link_unless_taken(deal, customer: str) -> bool:
	"""Link *deal* to *customer* (:func:`_link`) unless it has a customer already or its primary Contact
	is linked to other customers only: those are the employee's to sort out. True when linked."""
	contact = _primary_contact(deal)
	others = _customers_of_contacts([contact]) if contact else []
	if deal.get("erpnext_customer") or (others and customer not in others):
		return False
	_link(deal, customer)
	return True


def auto_link_after_commit(*, deal: str | None = None, email: str | None = None) -> None:
	"""Queue :func:`auto_link_deals` for once the caller's transaction commits (a web order's or a
	deal's: nothing is written into it). Off with the deal to POS switch. Never raises."""
	try:
		if pos_link_on():
			frappe.enqueue(AUTO_LINK_JOB, queue="short", enqueue_after_commit=True, deal=deal, email=email)
	except Exception:  # noqa: BLE001 — the employee can still link the deal by hand
		frappe.log_error(title=f"[{TITLE}] Deal link not queued: {deal or email}", message=frappe.get_traceback())


def deal_inserted(doc, method=None) -> None:
	"""CRM Deal after_insert: a new deal with an email and no customer is linked after it commits (the
	customer's web order may have come first)."""
	if doc.get("email") and not doc.get("erpnext_customer"):
		auto_link_after_commit(deal=doc.name)


def auto_link_deals(deal: str | None = None, email: str | None = None) -> list[str]:
	"""Link deals with no customer to the one Customer their email belongs to
	(:func:`sole_customer_by_email`): *deal* alone, the deals with *email*, or with neither every such
	deal (the upgrade patch). A deal whose contact is already linked to another customer is left to
	the employee. Each deal in its own savepoint; a failure is logged. Returns the deals linked."""
	from ffl_core.utils import guest_job_elevated

	if not pos_link_on():
		return []
	filters = {"erpnext_customer": ["is", "not set"], "email": ["is", "set"]}
	if deal:
		filters["name"] = deal
	if email:
		filters["email"] = email
	linked = []
	# lazy: every unlinked deal with an email when called with neither (the patch, once); a handful otherwise.
	with guest_job_elevated():   # queued from the Guest webhook
		for row in frappe.get_all("CRM Deal", filters=filters, fields=["name", "email"], order_by="creation asc"):
			frappe.db.savepoint("ffl_deal_auto_link")
			try:
				customer = sole_customer_by_email(row.email)
				if not customer:
					continue
				doc = frappe.get_doc("CRM Deal", row.name)
				if link_unless_taken(doc, customer):
					linked.append(row.name)
			except Exception:
				frappe.db.rollback(save_point="ffl_deal_auto_link")
				frappe.log_error(title=f"[{TITLE}] Deal not linked: {row.name}", message=frappe.get_traceback())
	return linked


# ----------------------------------------------------------------- the card


def _recent_orders(customer: str) -> list[dict]:
	rows = []
	for doctype, extra in (("Sales Invoice", {"is_consolidated": 0}), ("POS Invoice", _pos_sale_filter())):
		rows += [{**row, "doctype": doctype} for row in frappe.get_all(doctype,
			filters={"customer": customer, "docstatus": 1, **extra},
			fields=["name", "posting_date", "grand_total", "is_return"],
			order_by="posting_date desc, creation desc", limit=RECENT_ORDERS)]
	rows.sort(key=lambda r: str(r["posting_date"]), reverse=True)
	return [{"doctype": r["doctype"], "name": r["name"], "posting_date": str(r["posting_date"]),
		"grand_total": flt(r["grand_total"]), "is_return": cint(r["is_return"])} for r in rows[:RECENT_ORDERS]]


def _pos_sale_filter() -> dict:
	"""A counter pickup's 4473 ticket is not a sale (the Sales Report's own rule)."""
	return {"ffl_counter_order": ("is", "not set")} if frappe.db.has_column("POS Invoice", "ffl_counter_order") else {}


def _lifetime_spend(customer: str) -> float:
	from ffl_core.ffl_core.report.sales_report.sales_report import net_sales_of

	base = {"customer": customer, "docstatus": 1}
	nets = net_sales_of("Sales Invoice", {**base, "is_consolidated": 0})
	nets.update(net_sales_of("POS Invoice", {**base, **_pos_sale_filter()}))
	return flt(sum(nets.values()), 2)


def _open_orders(customer: str) -> list[dict]:
	from ffl_core.api.manual_order import list_pending_dispositions

	return [{"sales_invoice": row["sales_invoice"], "posting_date": str(row["posting_date"]),
		"grand_total": flt(row["grand_total"]), "outstanding_amount": flt(row["outstanding_amount"])}
		for row in list_pending_dispositions(customer=customer)]


WEB_ORDER = "Woo Online Order"


def _web_orders(customer: str) -> list[dict]:
	"""The customer's web orders still awaiting fulfilment. One already on a submitted invoice or
	register ticket is left out: it shows there."""
	if not frappe.db.has_column(WEB_ORDER, "customer"):
		return []   # the first deploy of this release, before the migrate
	rows = frappe.get_all(WEB_ORDER, filters={"customer": customer, "order_status": "Pending Fulfillment"},
		fields=["name", "woo_status", "creation", "order_total"], order_by="creation desc")
	names = [r.name for r in rows]
	invoiced = set()
	for doctype in ("Sales Invoice", "POS Invoice"):
		if names:
			invoiced.update(frappe.get_all(doctype, filters={"woo_online_order": ["in", names], "docstatus": 1},
				pluck="woo_online_order"))
	return [{"name": r.name, "status": (r.woo_status or "").replace("-", " ").capitalize(),
		"date": str(getdate(r.creation)), "grand_total": flt(r.order_total)}
		for r in rows if r.name not in invoiced][:RECENT_ORDERS]


def _ffl_status(customer: str) -> dict:
	row = frappe.db.get_value("Customer", customer, ["is_ffl_dealer", "ffl_number"], as_dict=True)
	if not row.is_ffl_dealer:
		return {"is_dealer": False, "on_file": False, "expiry_date": None, "expired": False}
	expiry = frappe.db.get_value("ATF FFL Record", row.ffl_number, "expiry_date") if row.ffl_number else None
	return {"is_dealer": True, "on_file": bool(expiry), "expiry_date": str(expiry) if expiry else None,
		"expired": not expiry or getdate(expiry) < getdate(nowdate())}


def customer_summary(deal) -> dict:
	"""The Deal page's customer card.

	Returns:
	    {"customer": None, "matches": find_matches, "html"} before the deal has a customer, else
	    {"customer", "customer_name", "flags", "ffl", "orders", "open_orders", "web_orders": web orders not
	    fulfilled yet [{"name", "status", "date", "grand_total"}], "lifetime_spend",
	    "holds": the deal's live gun holds [{"serial", "expires_label"}], "marketing": {"status", "tags"} of
	    the customer's marketing contact (None when marketing is off or there is none), "html"}.
	"""
	customer = linked_customer(deal)
	if not customer:
		from ffl_core.crm.sell_requests import is_sell_request

		matches = find_matches(deal)
		return {"customer": None, "matches": matches, "html": _no_customer_html(matches, is_sell_request(deal))}
	summary = {
		"customer": customer,
		"customer_name": frappe.db.get_value("Customer", customer, "customer_name"),
		"flags": _safe_flags(customer),
		"ffl": _ffl_status(customer),
		"orders": _recent_orders(customer),
		"open_orders": _open_orders(customer),
		"web_orders": _web_orders(customer),
		"lifetime_spend": _lifetime_spend(customer),
		"holds": card_lines(deal.name),
		"marketing": marketing_customers.card(customer),
	}
	summary["html"] = _card_html(summary)
	return summary


_RED = "color:#b91c1c;font-weight:600"
_AMBER = "color:#b45309"
_ROW = "margin-bottom:6px"
_MUTED = "color:#6b7280"


def _money_text(value) -> str:
	return fmt_money(value, currency=frappe.db.get_default("currency"))


def _money(value) -> str:
	return escape_html(_money_text(value))


def _link_html(path: str, name: str, label: str | None = None) -> str:
	return f'<a href="/app/{path}/{quote(str(name), safe="")}" style="color:inherit;text-decoration:underline">{escape_html(label or name)}</a>'


def _status_html(status: str, reason: str | None, banned: bool, override_until: str | None = None) -> str:
	if status != BANNED:
		return ""
	style = _RED
	text = escape_html(status)
	if not banned:
		text = escape_html(_("Banned, override open until {0}").format(override_until or ""))
	if reason:
		text += f": {escape_html(reason)}"
	return f'<div style="{style};{_ROW}">{text}</div>'


def _alerts_html(alerts: list[dict]) -> str:
	return "".join(f'<div style="{_AMBER};{_ROW}">{escape_html(a["message"])}</div>' for a in alerts)


def _match_html(match: dict) -> str:
	why = ", ".join(escape_html(_(m)) for m in match["matched"])
	return (f'<div style="{_ROW}"><div style="font-weight:600">{_link_html("customer", match["name"], match["customer_name"])}'
		f' <span style="{_MUTED}">({why})</span></div>'
		f'{_status_html(match["customer_status"], match["status_reason"], match["banned"])}'
		f'{_alerts_html(match["alerts"])}</div>')


def _no_customer_html(matches: list[dict], sell: bool = False) -> str:
	if not matches:
		text = _("No POS customer yet. Receive gun makes one.") if sell else _("No POS customer yet. Open order makes one.")
		return f'<div style="{_MUTED}">{escape_html(text)}</div>'
	head = escape_html(_("Not linked yet. Receive gun asks which one this is.") if sell
		else _("Not linked yet. Open order asks which one this is."))
	return f'<div style="{_MUTED};{_ROW}">{head}</div>' + "".join(_match_html(m) for m in matches)


def _matches_html(matches: list[dict], values: dict) -> str:
	new = escape_html(" · ".join(v for v in (values["customer_name"], values["email_id"], values["phone"]) if v))
	if not matches:
		return f'<div style="{_ROW}">{escape_html(_("No POS customer matches this deal. A new one will be:"))}</div><div>{new}</div>'
	return "".join(_match_html(m) for m in matches) \
		+ f'<div style="{_MUTED};margin-top:12px">{escape_html(_("Or a new customer:"))} {new}</div>'


def _ffl_html(ffl: dict) -> str:
	if not ffl["is_dealer"]:
		return ""
	if not ffl["on_file"]:
		return f'<div style="{_RED};{_ROW}">{escape_html(_("Dealer: no FFL on file"))}</div>'
	text = _("Dealer: FFL expires {0}").format(formatdate(ffl["expiry_date"]))
	return f'<div style="{_RED if ffl["expired"] else ""};{_ROW}">{escape_html(text)}</div>'


def _marketing_html(marketing: dict) -> str:
	suppressed = marketing["status"] in ("Unsubscribed", "Bounced", "Complained")
	return f'<div style="{_RED if suppressed else _MUTED};{_ROW}">{escape_html(marketing_customers.card_text(marketing))}</div>'


def _order_line(doctype: str, name: str, posting_date: str, amount: float, note: str = "") -> str:
	path = doctype.lower().replace(" ", "-")
	return (f'<div style="display:flex;justify-content:space-between;gap:8px">'
		f'<span>{_link_html(path, name)}{note}</span>'
		f'<span style="{_MUTED};white-space:nowrap">{escape_html(formatdate(posting_date))} · {_money(amount)}</span></div>')


def _card_html(summary: dict) -> str:
	flags = summary["flags"]
	parts = [
		f'<div style="font-weight:600;{_ROW}">{_link_html("customer", summary["customer"], summary["customer_name"])}</div>',
		_status_html(flags["status"], flags["status_reason"], flags["banned"], flags["override_until"]),
		_alerts_html(flags["alerts"]),
		_ffl_html(summary["ffl"]),
		f'<div style="{_ROW}">{escape_html(_("Lifetime spend"))}: <b>{_money(summary["lifetime_spend"])}</b></div>',
	]
	for hold in summary.get("holds") or []:
		parts.append(f'<div style="{_AMBER};{_ROW}">{escape_html(_("Held: {0} until {1}").format(hold["serial"], hold["expires_label"]))}</div>')
	if summary.get("marketing"):
		parts.append(_marketing_html(summary["marketing"]))
	web_orders = summary.get("web_orders") or []
	if summary["open_orders"] or web_orders:
		parts.append(f'<div style="font-weight:600;margin-top:8px">{escape_html(_("Open orders"))}</div>')
		for row in summary["open_orders"]:
			due = row["outstanding_amount"]
			note = f' <span style="{_AMBER}">{escape_html(_("{0} due").format(_money_text(due)))}</span>' if due > 0 else ""
			parts.append(_order_line("Sales Invoice", row["sales_invoice"], row["posting_date"], row["grand_total"], note))
		for row in web_orders:
			status = _("Web order, {0}").format(_(row["status"])) if row["status"] else _("Web order")
			note = f' <span style="{_MUTED}">{escape_html(status)}</span>'
			parts.append(_order_line(WEB_ORDER, row["name"], row["date"], row["grand_total"], note))
	if summary["orders"]:
		parts.append(f'<div style="font-weight:600;margin-top:8px">{escape_html(_("Last orders"))}</div>')
		for row in summary["orders"]:
			note = f' <span style="{_MUTED}">{escape_html(_("Return"))}</span>' if row["is_return"] else ""
			parts.append(_order_line(row["doctype"], row["name"], row["posting_date"], row["grand_total"], note))
	elif not web_orders:
		parts.append(f'<div style="{_MUTED}">{escape_html(_("No orders yet."))}</div>')
	return "".join(parts)


# ----------------------------------------------------------------- the order


def _order_lines(deal) -> tuple[list[dict], list[dict], list[str]]:
	"""(draft lines, guns still to pick, lines that are not POS items). Prices are the Create Order
	page's for a line picked there, and the deal's own (``crm.deal``): a gun's own price, any other
	line ``pricing.counter_price_for`` (the Item's selling price, not ``Item.standard_rate``); a line
	with no price opens at 0."""
	from ffl_core.api.manual_order import _serial_status
	from ffl_core.firearm import is_firearm_item

	customer = linked_customer(deal)

	rows = deal.get("products") or []
	items = {p.name: p.erpnext_item_code for p in frappe.get_all(PRODUCT,
		filters={"name": ["in", [r.product_code for r in rows if r.product_code]]},
		fields=["name", "erpnext_item_code"])}
	lines, unpicked, skipped = [], [], []
	for row in rows:
		item_code = items.get(row.product_code)
		if not item_code:
			skipped.append(row.product_name or row.product_code)
			continue
		item = frappe.db.get_value("Item", item_code, ["item_name", "is_antique"], as_dict=True)
		serial = row.get(SERIAL_FIELD)
		if serial:
			# A gun that cannot be sold any more stays on the draft: the page drops it and says why.
			data, _reason = _serial_status(serial, probe=False, customer=customer)
			lines.append({"item_code": item_code, "item_name": item.item_name, "is_firearm": True,
				"is_antique": bool(item.is_antique), "qty": 1, "rate": flt((data or {}).get("sell_price")),
				"serials": [serial]})
		elif is_firearm_item(item_code):
			unpicked.append({"item_code": item_code, "item_name": item.item_name, "is_firearm": True,
				"is_antique": bool(item.is_antique)})
		else:
			lines.append({"item_code": item_code, "item_name": item.item_name, "is_firearm": False,
				"qty": flt(row.qty) or 1, "rate": pricing.counter_price_for(item_code), "serials": []})
	return lines, unpicked, skipped


def open_order(deal) -> dict:
	"""Save the deal as a Create Order draft.

	One clean draft per deal: a second click refreshes the draft it left, by its title. A draft
	someone saved from the page has taken the page's own title, so it is left alone.

	Returns:
	    {"draft", "route", "skipped": deal lines that are not POS items}.
	"""
	from ffl_core.api.manual_order import save_order_draft
	from ffl_core.crm.sell_requests import refuse_selling

	refuse_selling(deal)
	customer = linked_customer(deal)
	if not customer:
		frappe.throw(_("Link this deal to a customer first."), title=_("No customer"))
	row = frappe.db.get_value("Customer", customer, ["customer_name", "is_ffl_dealer", "ffl_number"], as_dict=True)
	lines, unpicked, skipped = _order_lines(deal)
	payload = {
		"buyer_type": "business" if row.is_ffl_dealer and row.ffl_number else "individual",
		"customer": customer,
		"items": lines,
		"unpicked_firearms": unpicked,
		"crm_deal": deal.name,
		"pay_later": 0,
		"charge_terminal": 1,
		"dispose_now": 0,
		"ship_to": {},
	}
	if payload["buyer_type"] == "business":
		payload["biz_ffl"] = row.ffl_number
	title = _("Deal {0} · {1}").format(deal.name, row.customer_name)
	draft = save_order_draft(payload, name=frappe.db.get_value("Create Order Draft", {"title": title}), title=title)
	return {"draft": draft["name"], "route": f"/app/create-order/draft/{quote(draft['name'], safe='')}",
		"skipped": skipped}
