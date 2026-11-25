"""Frappe CRM endpoints: the CRM Contact page's Email button (``ffl_core.crm.setup.CONTACT_SCRIPT``)
opens the contact's deal to write to them, since CRM shows and sends mail only on a lead or a deal;
**Connect my mailbox** on a staff member's own My Settings (``public/js/user_form.js``) starts the
sign-in that connects their own mailbox; **Make lead** on a Communication (``public/js/communication_form.js``)
files a mail the AI screening left without a lead; the Deal page's customer card, **Open order** and **Add want**
(``ffl_core.crm.setup.DEAL_SCRIPT``), behind Lead Mail Settings' switch; a sell request's Gun offered card,
**Make offer**, **Seller accepted** and **Receive gun** (``ffl_core.crm.sell_requests``), behind the same switch; guns held for a deal (the Deal page's
**Hold gun**, and the counter's check and manager override, ``ffl_core.crm.holds``); the stand-ins that take quotes off the Deal page and other mail off the Leads list; **Draft reply** and
**Insert price** on the Lead and Deal pages (``ffl_core.crm.reply_draft``, ``ffl_core.crm.price_insert``); the Lead and
Deal pages' Memory card, **Edit memory**, **Update memory** and **Clear memory** (``ffl_core.crm.memory``)."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from ffl_core.crm import customer as deal_customer
from ffl_core.crm import deals, holds, mail, pos_link_on, sell_requests
from ffl_core.crm import setup as crm_setup
from ffl_core.roles import CRM_ROLES


@frappe.whitelist(methods=["POST"])
def open_deal(contact: str) -> str:
	"""The contact's open deal, made when they have none, to write to them from CRM.

	Args:
	    contact: The Contact's name.

	Returns:
	    str: The CRM Deal's name (open it at /crm/deals/<name>#emails). Refused for a contact
	    without an email address.
	"""
	frappe.only_for(CRM_ROLES)
	if not isinstance(contact, str) or not frappe.db.exists("Contact", contact):
		frappe.throw(_("This contact no longer exists."), frappe.DoesNotExistError)
	frappe.has_permission("Contact", "read", doc=contact, throw=True)
	if not (frappe.db.get_value("Contact", contact, "email_id")
			or frappe.db.exists("Contact Email", {"parenttype": "Contact", "parent": contact})):
		frappe.throw(_("This contact has no email address. Add one to the contact first."),
			title=_("No email address"))
	return deals.open_deal_for(contact, owner=frappe.session.user)


@frappe.whitelist(methods=["POST"])
def connect_my_mailbox(email_account: str | None = None, success_uri: str | None = None) -> str:
	"""Start connecting your own mailbox to the POS: the address of the provider's sign-in page.

	For a staff member whose mailbox an administrator set up as an OAuth Email Account with them as
	its connected user; they sign in to their own mail account, not the administrator. Frappe's own
	Authorize button needs read access to the Connected App, which staff do not have.

	Args:
	    email_account: The Email Account to connect. Optional when you have only one.
	    success_uri: Where to land after signing in (a path on this site). Default: CRM.

	Returns:
	    str: The sign-in address to open. Refused for an Email Account whose connected user is
	    someone else, or that does not sign in with OAuth.
	"""
	frappe.only_for(CRM_ROLES)
	user = frappe.session.user
	filters = {"auth_method": "OAuth", "connected_user": user}
	if email_account:
		filters["name"] = email_account
	# Staff have no read right on Email Account: looked up for the caller's own mailbox only.
	accounts = frappe.get_all("Email Account", filters=filters, fields=["name", "connected_app"], limit=2)
	if not accounts:
		frappe.throw(_("No mailbox is set up for you to connect. Ask your administrator to add it."),
			frappe.PermissionError)
	if len(accounts) > 1:
		frappe.throw(_("You have more than one mailbox. Pick the one to connect."), title=_("Pick a mailbox"))
	if not accounts[0].connected_app:
		frappe.throw(_("Your mailbox has no sign-in app yet. Ask your administrator to finish setting it up."))
	app = frappe.get_doc("Connected App", accounts[0].connected_app)
	# As the caller themselves: Frappe checks no permission for a user's own flow.
	return app.initiate_web_application_flow(user=user, success_uri=success_uri or "/crm")


@frappe.whitelist(methods=["POST"])
def make_lead_from_mail(communication: str) -> dict:
	"""Make the lead for a mail the AI screening judged not to be from a customer.

	The mail goes by the normal mail rules with the screening skipped: a new lead shared with the team,
	or the sender's deal if they have become a customer.

	Args:
	    communication: The Communication's name.

	Returns:
	    dict: {"doctype": "CRM Lead" or "CRM Deal", "name": its name} (open it at /crm/leads/<name> or
	    /crm/deals/<name>). Refused for a mail that is already linked, was not screened out, or is not
	    received mail.
	"""
	frappe.only_for(CRM_ROLES)
	if not isinstance(communication, str) or not frappe.db.exists("Communication", communication):
		frappe.throw(_("This mail no longer exists."), frappe.DoesNotExistError)
	doctype, name = mail.make_lead(communication)
	return {"doctype": doctype, "name": name}


def _deal_for_pos_link(deal: str, ptype: str):
	if not pos_link_on():
		frappe.throw(_("Linking deals to POS orders is off. A manager can turn it on in Lead Mail Settings."),
			title=_("Turned off"))
	if not isinstance(deal, str) or not frappe.db.exists(deals.DEAL, deal):
		frappe.throw(_("This deal no longer exists."), frappe.DoesNotExistError)
	frappe.has_permission(deals.DEAL, ptype, doc=deal, throw=True)
	return frappe.get_doc(deals.DEAL, deal)


@frappe.whitelist()
def deal_customer_summary(deal: str) -> dict:
	"""What the Deal page's customer card shows. Never carries an ID number, date of birth, 4473 or
	NICS.

	Args:
	    deal: The CRM Deal's name.

	Returns:
	    dict: {"customer": None, "matches": [...], "html"} before the deal has a customer, else
	    {"customer", "customer_name", "flags": {"status", "status_reason", "banned",
	    "override_until", "alerts", "tags"}, "ffl": {"is_dealer", "on_file", "expiry_date",
	    "expired"}, "orders": last orders, "open_orders": orders on Pending Order, "lifetime_spend":
	    Net Sales as the Sales Report reads it, "holds": guns held for the deal [{"serial",
	    "expires_label"}], "marketing": {"status", "tags"} of the customer's marketing contact or None,
	    "html"}. Refused while the switch is off.
	"""
	frappe.only_for(CRM_ROLES)
	return deal_customer.customer_summary(_deal_for_pos_link(deal, "read"))


@frappe.whitelist(methods=["POST"])
def ensure_deal_customer(deal: str, customer: str | None = None, create: int = 0) -> dict:
	"""The deal's POS customer: its link, the matches to confirm, or a new customer on confirm.

	Args:
	    deal: The CRM Deal's name.
	    customer: The Customer the employee confirmed: it goes on the deal and on its contact.
	    create: 1 to make a new Customer from the deal's contact.

	Returns:
	    dict: {"customer": name} once there is one; else {"customer": None, "matches":
	    [{"name", "customer_name", "customer_status", "matched", "banned", "status_reason",
	    "alerts"}], "new": the values a new customer would take, "html": the dialog's text}.
	"""
	frappe.only_for(CRM_ROLES)
	return deal_customer.ensure_customer(_deal_for_pos_link(deal, "write"), customer=customer or None,
		create=bool(cint(create)))


@frappe.whitelist(methods=["POST"])
def open_deal_order(deal: str) -> dict:
	"""Save a deal with a customer as a Create Order draft, to finish the sale on that page.

	Args:
	    deal: The CRM Deal's name.

	Returns:
	    dict: {"draft": the draft's name, "route": the page to open, "skipped": deal lines that are
	    not POS items}. The page sets the prices, as for a line picked there.
	"""
	frappe.only_for(CRM_ROLES)
	return deal_customer.open_order(_deal_for_pos_link(deal, "write"))


@frappe.whitelist()
def sell_request_card(deal: str) -> dict:
	"""What a sell request's Gun offered card shows: the gun, its photos and where the buy stands.

	Args:
	    deal: The CRM Deal's name, a sell request.

	Returns:
	    dict: {"html", "offer": the store's offer or 0, "accepted_on": the day the seller accepted or None,
	    "received": the gun is received}. Refused while the switch is off.
	"""
	frappe.only_for(CRM_ROLES)
	doc = _deal_for_pos_link(deal, "read")
	if not sell_requests.is_sell_request(doc):
		frappe.throw(_("This deal is not a sell request."), title=_("Not a sell request"))
	return sell_requests.card(doc)


@frappe.whitelist(methods=["POST"])
def make_sell_offer(deal: str, amount: float) -> dict:
	"""Store what the store offers for a gun someone sells it. No email is sent, and the amount never
	counts as a sale (the deal's value).

	Args:
	    deal: The CRM Deal's name, an open sell request whose gun is not received yet.
	    amount: The offer, more than 0.

	Returns:
	    dict: The Gun offered card, as sell_request_card.
	"""
	frappe.only_for(CRM_ROLES)
	return sell_requests.make_offer(_deal_for_pos_link(deal, "write"), amount)


@frappe.whitelist(methods=["POST"])
def sell_offer_accepted(deal: str) -> dict:
	"""The seller accepted the offer: the deal waits for the gun from today. A second call keeps the
	first day.

	Args:
	    deal: The CRM Deal's name, an open sell request with an offer.

	Returns:
	    dict: The Gun offered card, as sell_request_card.
	"""
	frappe.only_for(CRM_ROLES)
	return sell_requests.accepted(_deal_for_pos_link(deal, "write"))


@frappe.whitelist()
def sell_gun_intake(deal: str) -> dict:
	"""What the register's Trade-in dialog is filled with to receive a sell request's gun. Writes
	nothing: staff check the gun and submit the dialog, and that books it.

	Args:
	    deal: The CRM Deal's name, an open sell request with a customer and no gun received yet.

	Returns:
	    dict: {"deal", "source_name": the seller (the deal's customer), "manufacturer", "model_name",
	    "caliber", "firearm_type", "serial_no", "acquisition_cost": the offer or None}. No condition:
	    staff grade the gun after inspecting it.
	"""
	frappe.only_for(CRM_ROLES)
	return sell_requests.intake(_deal_for_pos_link(deal, "read"))


@frappe.whitelist(methods=["POST"])
def add_want(deal: str, keywords: str | None = None, item: str | None = None, budget: float | None = None,
		notes: str | None = None) -> str:
	"""Add a want to a deal's primary contact: when stock that fits arrives, you get a task.

	Args:
	    deal: The CRM Deal's name.
	    keywords: What they are after, for example "P228" or "Mosin 91/30". Every word must appear in
	        the arriving gun's name, brand, model, caliber or description.
	    item: An Item to alert on, instead of or as well as keywords.
	    budget: What they want to spend. Shown on the task; arrivals are not filtered by price.
	    notes: Anything else worth knowing.

	Returns:
	    str: The CRM Want's name. The owner is the caller. Refused for a deal with no contact, and
	    when neither keywords nor an item is given.
	"""
	frappe.only_for(CRM_ROLES)
	if not isinstance(deal, str) or not frappe.db.exists(deals.DEAL, deal):
		frappe.throw(_("This deal no longer exists."), frappe.DoesNotExistError)
	frappe.has_permission(deals.DEAL, "read", doc=deal, throw=True)
	doc = frappe.get_doc(deals.DEAL, deal)
	contact = deal_customer._primary_contact(doc)
	if not contact:
		frappe.throw(_("This deal has no contact. Add one first."), title=_("No contact"))
	# As the caller: a Sales User may create wants (the DocType's own permission).
	return frappe.get_doc({"doctype": "CRM Want", "contact": contact, "deal": deal,
		"customer": doc.get("erpnext_customer") or None, "keywords": keywords, "item": item or None,
		"budget": budget or None, "notes": notes, "want_owner": frappe.session.user}).insert().name


@frappe.whitelist()
def crm_list_data(**kwargs) -> dict:
	"""CRM's list data for its Leads and Deals pages (hooks.py overrides ``crm.api.doc.get_data``), with
	three changes: a Leads list leaves out other mail unless it filters on the status or category,
	``@me`` inside a filter's list of values is the caller (CRM's page replaces only a plain ``@me``, and
	**My follow-ups** asks for "mine or nobody's"), and a Deals list on CRM's own columns shows the
	person's name first.

	Args:
	    kwargs: CRM's own arguments, unchanged, chiefly doctype and filters.

	Returns:
	    dict: CRM's answer: the rows and columns of the list.
	"""
	from crm.api.doc import get_data

	if kwargs.get("doctype") in ("CRM Lead", "CRM Deal"):
		filters = frappe.parse_json(kwargs.get("filters") or {}) or {}
		filters = {key: _with_caller(value) for key, value in filters.items()}
		fixed = frappe.parse_json(kwargs.get("default_filters") or {}) or {}
		if kwargs["doctype"] == "CRM Lead" and not ({"status", mail.CATEGORY_FIELD} & (set(filters) | set(fixed))):
			filters[mail.CATEGORY_FIELD] = ["is", "not set"]
		kwargs["filters"] = filters
		kwargs["default_filters"] = fixed or None
	view = frappe.parse_json(kwargs.get("view") or {}) or {}
	named = kwargs.get("doctype") == "CRM Deal" and view.get("view_type", "list") != "kanban" \
		and not kwargs.get("columns") and not kwargs.get("rows") \
		and not frappe.db.exists("CRM View Settings", {"dt": "CRM Deal", "type": view.get("view_type") or "list",
			"is_standard": 1, "user": frappe.session.user})
	if named:
		# CRM's own default (it would read CRMDeal.default_list_data): the same, the name first.
		kwargs.update(crm_setup.deal_list_data())
	data = frappe.call(get_data, **kwargs)
	if named and not view.get("custom_view_name"):
		data["is_default"] = True   # still the default list: no "reset" offered
	return data


def _with_caller(value):
	if isinstance(value, list):
		return [_with_caller(entry) for entry in value]
	return frappe.session.user if value == "@me" else value


@frappe.whitelist()
def deals_use_no_quotes() -> bool:
	"""Stands in for CRM's quotation gates (hooks.py): the Deal page shows no Quotations tab and no
	**Create Quotation** button, since a deal closes through **Open order**.

	Returns:
	    bool: Always False.
	"""
	return False


# ---------------------------------------------------------------- guns held for a deal (crm/holds.py)


def _serial_list(serials) -> list[str]:
	value = frappe.parse_json(serials) if isinstance(serials, str) and serials.strip().startswith("[") else serials
	if isinstance(value, str):
		value = [value]
	return [str(s) for s in (value or []) if isinstance(s, (str, int)) and str(s).strip()][:50]


def _with_guns(deal, serials: list[str]):
	"""*deal* with a product line for each of *serials* it had none for (``deal.add_product_row``: only
	a gun in stock gets one). What still has no line, ``holds.hold_guns`` refuses by name."""
	from ffl_core.crm.deal import SERIAL_FIELD, add_product_row

	on_deal = {r.get(SERIAL_FIELD) for r in deal.get("products") or []}
	added = False
	for serial in serials:
		item = serial not in on_deal and frappe.db.get_value("Serial No", serial, "item_code")
		if item:
			added = add_product_row(deal.doctype, deal.name, item, serial) or added
	return frappe.get_doc(deal.doctype, deal.name) if added else deal


def _hold_deal(hold: str, ptype: str):
	deal = isinstance(hold, str) and frappe.db.get_value(holds.HOLD, hold, "deal")
	if not deal:
		frappe.throw(_("This hold no longer exists."), frappe.DoesNotExistError)
	return _deal_for_pos_link(deal, ptype)


@frappe.whitelist()
def deal_holds(deal: str) -> dict:
	"""The Deal page's Hold guns dialog: each gun on the deal's lines, its hold and what can be done.
	Answers while Hold guns for deals is off too, so holds still running stay in view.

	Args:
	    deal: The CRM Deal's name.

	Returns:
	    dict: {"guns": [{"serial", "item_code", "item_name", "hold": None or {"name", "expires_at",
	    "expires_label", "held_by", "held_by_name", "extensions", "extend_needs_manager",
	    "release_needs_manager"}, "blocked": why it cannot be held or None, "needs_manager": holding it
	    takes a manager}], "enabled": whether new holds are on, "self_approves": the caller is a
	    manager, "approvers": [{"user", "full_name", "has_pin", "pin_off"}] otherwise}.
	"""
	frappe.only_for(CRM_ROLES)
	return holds.deal_guns(_deal_for_pos_link(deal, "read"))


@frappe.whitelist(methods=["POST"])
def hold_guns(deal: str, serials, approver: str | None = None, pin: str | None = None,
		password: str | None = None, hours: int | None = None) -> dict:
	"""Hold guns on a deal (for 48 hours unless told otherwise): only the deal's customer can buy them meanwhile, at the
	counter and on the website. Each gun is judged on its own.

	Args:
	    deal: The CRM Deal's name. It needs its POS customer.
	    serials: The guns' serial numbers (a list, or JSON). One that is not on the deal's lines yet
	        and is in stock goes on a line of its own first.
	    approver: The manager approving, when a gun needs one (held for this deal before).
	    pin: The approver's counter PIN.
	    password: Or their password.
	    hours: How long: 24, 48, 72 or 168. 48 when not given.

	Returns:
	    dict: {"held": [{"name", "serial", "deal", "status", "expires_at", "expires_label", "held_by",
	    "extensions", "end_reason"}], "refused": [{"serial", "reason", "needs_manager"?}]}. Refused
	    while Hold guns for deals is off.
	"""
	frappe.only_for(CRM_ROLES)
	return holds.hold_guns(_with_guns(_deal_for_pos_link(deal, "write"), _serial_list(serials)), _serial_list(serials),
		approver=approver or None, pin=pin or None, password=password or None, hours=hours)


@frappe.whitelist(methods=["POST"])
def extend_hold(hold: str, approver: str | None = None, pin: str | None = None,
		password: str | None = None) -> dict:
	"""Another 48 hours on a hold. The person who placed it extends it once; after that, or for anyone
	else, a manager does (signed in, or picked with their PIN).

	Args:
	    hold: The CRM Deal Hold's name.
	    approver: The manager approving.
	    pin: The approver's counter PIN.
	    password: Or their password.

	Returns:
	    dict: The hold: {"name", "serial", "deal", "status", "expires_at", "expires_label", "held_by",
	    "extensions", "end_reason"}.
	"""
	frappe.only_for(CRM_ROLES)
	_hold_deal(hold, "write")
	return holds.extend(hold, approver=approver or None, pin=pin or None, password=password or None)


@frappe.whitelist(methods=["POST"])
def release_hold(hold: str, reason: str | None = None, approver: str | None = None, pin: str | None = None,
		password: str | None = None) -> dict:
	"""End a hold: the gun is for sale to anyone again. Whoever placed it, the deal owner or a manager;
	anyone else with a manager's PIN.

	Args:
	    hold: The CRM Deal Hold's name.
	    reason: Why (optional).
	    approver: The manager approving.
	    pin: The approver's counter PIN.
	    password: Or their password.

	Returns:
	    dict: The hold, now Released: {"name", "serial", "deal", "status", "expires_at",
	    "expires_label", "held_by", "extensions", "end_reason"}.
	"""
	frappe.only_for(CRM_ROLES)
	_hold_deal(hold, "read")
	return holds.release(hold, reason=reason, approver=approver or None, pin=pin or None,
		password=password or None)


@frappe.whitelist()
def serial_hold(serial: str) -> dict:
	"""The live deal hold on one gun, for the counter. Carries no ID, 4473 or NICS data.

	Args:
	    serial: The Serial No.

	Returns:
	    dict: {"hold": None or {"serial", "hold", "deal", "customer", "customer_name", "expires_at",
	    "expires_label", "held_by", "held_by_name", "message"}, "self_approves": the caller is a
	    manager, "approvers": [{"user", "full_name", "has_pin", "pin_off"}] otherwise}.
	"""
	from ffl_core.price_approval import is_approver
	from ffl_core.roles import COUNTER_ROLES

	frappe.only_for(COUNTER_ROLES)
	entry = holds.serial_entry(str(serial or "").strip()) if serial else None
	manager = is_approver()
	return {"hold": entry, "self_approves": manager, "approvers": [] if manager or not entry else holds.approvers()}


@frappe.whitelist(methods=["POST"])
def check_sale(doc) -> dict:
	"""Before a POS / Sales Invoice is saved: the guns on it held for a deal whose customer is not this
	sale's.

	Args:
	    doc: The form's document (POS Invoice or Sales Invoice) as JSON.

	Returns:
	    dict: {"blocked": [{"serial", "hold", "deal", "customer", "customer_name", "expires_at",
	    "expires_label", "held_by", "held_by_name", "message"}], "self_approves": the caller is a
	    manager, "approvers": [{"user", "full_name", "has_pin", "pin_off"}] when something is blocked}.
	"""
	from ffl_core.price_approval import _target, is_approver
	from ffl_core.roles import PRICE_CHECK_ROLES

	frappe.only_for(PRICE_CHECK_ROLES)
	d = _target(doc)[0]
	if d.doctype not in ("POS Invoice", "Sales Invoice"):
		frappe.throw(_("Only a sale is checked."))
	blocked = holds.blocked_on_sale(d)
	manager = is_approver()
	return {"blocked": blocked, "self_approves": manager,
		"approvers": holds.approvers() if blocked and not manager else []}


@frappe.whitelist(methods=["POST"])
def override_hold(serial: str, reason: str, approver: str | None = None, pin: str | None = None,
		password: str | None = None) -> dict:
	"""A manager ends a deal's hold at the counter so the gun can be sold to someone else; the deal's
	owner gets a task. A manager signed in approves their own; anyone else names one, who types their
	counter PIN (or password).

	Args:
	    serial: The held gun's Serial No.
	    reason: Why (required).
	    approver: The manager approving.
	    pin: The approver's counter PIN.
	    password: Or their password.

	Returns:
	    dict: {"released": the hold, now Released, or None when nothing held the gun, "task": the CRM
	    Task given to the deal owner, or None}.
	"""
	from ffl_core.roles import COUNTER_ROLES

	frappe.only_for(COUNTER_ROLES)
	return holds.override(str(serial or "").strip(), reason, approver=approver or None, pin=pin or None,
		password=password or None)


@frappe.whitelist()
def reply_draft_ready(doctype: str, name: str) -> dict:
	"""Which of a Lead or Deal page's Draft reply and Insert price buttons show. No network call.

	Args:
	    doctype: "CRM Lead" or "CRM Deal".
	    name: The record's name.

	Returns:
	    dict: {"ready": an extension drafts replies, "insert_price": linking deals to POS orders is on}.
	    Both false on a sell request.
	"""
	from ffl_core.crm import reply_draft

	frappe.only_for(CRM_ROLES)
	return reply_draft.buttons(doctype, name)


@frappe.whitelist(methods=["POST"])
def draft_reply(doctype: str, name: str, quote_text: str = "", composer: int = 0) -> dict:
	"""A draft reply to the customer's message, written by an extension. Nothing is sent or saved.

	Args:
	    doctype: "CRM Lead" or "CRM Deal".
	    name: The record's name.
	    quote_text: The text of the quote in CRM's open composer (the mail Reply was clicked on), if any.
	    composer: 1 when CRM's composer is open.

	Returns:
	    dict: {"native_ok": 1 when the draft goes into that composer, "to", "subject", "sender",
	    "in_reply_to": the Communication answered, "channel": "email" or "gunbroker", "content_html",
	    "signature_html", "handoff": what needs a person, or None}. The envelope fields are for the
	    page's own dialog when the composer is not used. Refused when the record has nothing from the
	    customer to answer, or with the extension's own reason.
	"""
	from ffl_core.crm import reply_draft

	frappe.only_for(CRM_ROLES)
	return reply_draft.draft(doctype, name, quote_text=quote_text or "")


@frappe.whitelist()
def price_options(doctype: str, name: str) -> dict:
	"""The products Insert price offers on a Lead or Deal, most relevant first, at the register's price.

	Args:
	    doctype: "CRM Lead" or "CRM Deal".
	    name: The record's name.

	Returns:
	    dict: {"options": [{"item_code", "serial_no", "label"}] (15 at most), "default": 0}.
	"""
	from ffl_core.crm import price_insert

	frappe.only_for(CRM_ROLES)
	return price_insert.options(doctype, name)


@frappe.whitelist()
def price_text(doctype: str, name: str, item_code: str, serial_no: str | None = None) -> dict:
	"""The line Insert price puts in the reply, priced now.

	Args:
	    doctype: "CRM Lead" or "CRM Deal".
	    name: The record's name.
	    item_code: The Item.
	    serial_no: The gun, for a model priced gun by gun; it must be one of the Item's in stock.

	Returns:
	    dict: {"text": "{product} — ${price}"}. Refused when the Item has no price.
	"""
	from ffl_core.crm import price_insert

	frappe.only_for(CRM_ROLES)
	return price_insert.text(doctype, name, item_code, serial_no=serial_no or None)


def _memory_record(doctype: str, name: str, ptype: str):
	from ffl_core.crm import memory

	return memory.record_for(doctype, name, ptype)


@frappe.whitelist()
def memory_card(doctype: str, name: str) -> dict:
	"""The Memory card of a Lead or Deal page: what the customer told the store, and the record's summary.

	Args:
	    doctype: "CRM Lead" or "CRM Deal".
	    name: The record's name.

	Returns:
	    dict: {"html", "shown": the page shows the card (memory is on, or something is kept), "ready":
	    an extension updates memory (Update memory shows), "can_edit": you may change this record,
	    "has_content", "values": {"want", "trade_in", "preference", "question" (one row a line),
	    "budget", "state", "auto_update"} for Edit memory}.
	"""
	frappe.only_for(CRM_ROLES)
	from ffl_core.crm import memory

	return memory.card(_memory_record(doctype, name, "read"))


@frappe.whitelist()
def memory_source(doctype: str, name: str, row: str) -> dict:
	"""The customer message a row of the Memory card came from (its "said" link).

	Args:
	    doctype: "CRM Lead" or "CRM Deal".
	    name: The record's name.
	    row: The memory row's name, from the card.

	Returns:
	    dict: {"subject", "at", "text": plain text, 2000 characters at most}. Refused for a row that is
	    not this customer's, or a message on a record you may not read.
	"""
	frappe.only_for(CRM_ROLES)
	from ffl_core.crm import memory

	return memory.source(_memory_record(doctype, name, "read"), str(row or ""))


@frappe.whitelist(methods=["POST"])
def memory_save(doctype: str, name: str, values) -> dict:
	"""Edit memory: save what the customer's memory says. A new or changed row is yours and the AI leaves
	it alone.

	Args:
	    doctype: "CRM Lead" or "CRM Deal".
	    name: The record's name.
	    values: {"want", "trade_in", "preference", "question" (one row a line), "budget", "state",
	        "auto_update"}, as a dict or JSON.

	Returns:
	    dict: The new card, as memory_card returns it. Refused with ID numbers, eligibility details,
	    prices or stock in a row, a state that is not a US state, or more lines than a kind keeps.
	"""
	frappe.only_for(CRM_ROLES)
	from ffl_core.crm import memory

	return memory.save(_memory_record(doctype, name, "write"), values)


@frappe.whitelist(methods=["POST"])
def memory_clear(doctype: str, name: str) -> dict:
	"""Clear memory: every row of the customer's memory you can see goes, and the summary of each of their
	leads and deals you may change. On a lead that became a deal, the deal's rights apply.

	Args:
	    doctype: "CRM Lead" or "CRM Deal".
	    name: The record's name.

	Returns:
	    dict: The new card, as memory_card returns it, with "summaries_cleared" and "summaries_kept"
	    (the person's other leads and deals you may not change keep theirs).
	"""
	frappe.only_for(CRM_ROLES)
	from ffl_core.crm import memory

	return memory.clear(_memory_record(doctype, name, "write"))


@frappe.whitelist(methods=["POST"])
def memory_update_now(doctype: str, name: str) -> dict:
	"""Update memory: have the extension read the record's latest customer messages now.

	Args:
	    doctype: "CRM Lead" or "CRM Deal".
	    name: The record's name.

	Returns:
	    dict: The new card, as memory_card returns it. Refused while memory is off, with the
	    extension's own reason (for example a spending limit), or when nothing could be updated.
	"""
	frappe.only_for(CRM_ROLES)
	from ffl_core.crm import memory

	return memory.update_now(_memory_record(doctype, name, "write"))
