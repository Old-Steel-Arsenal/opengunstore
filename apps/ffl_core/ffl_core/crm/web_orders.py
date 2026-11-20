"""A customer's web orders on their open CRM deal: a note when an order is placed, shipped or picked up,
cancelled, or refunded, so whoever talks to them sees it. Behind Lead Mail Settings' deal to POS switch.

A customer with no open deal gets no note and no deal. Woo redelivers webhooks, and an order passes some
of these points more than once, so a note's title is its key. Never fails the order: a failure is rolled
back and logged. An order's notes go on the deal it won, once it won one.

A paid web order wins the sale it closes (:func:`web_order_paid`, a job queued after the order commits),
matched by the order's email, among the records made before the order was recorded:

* the customer's open deal, unless an order was opened from it (an invoice, or a Create Order draft
  from its Open order button) or it holds a gun: that deal is set Won;
* with no such open deal, their newest lead not converted yet, not lost and not other mail: CRM's own
  conversion makes it a deal (owner and mail thread kept), set Won.

The won deal stores the order (``WON_BY``) and takes the order's total as its value (``crm.orders.deal_sales``
keeps it there on later saves), and is linked to the order's POS customer. An order wins one record,
once. Cancelled or fully refunded later, the deal it won goes back to the open status it had and its
value is cleared (:func:`web_order_undone`). Nobody is told: no email, no task.

A web order whose payment failed is the one exception (:func:`payment_failed`): the sale is still
there to win back, so it gets a deal when the customer has none (their newest unconverted lead is
converted into it, as a paid order's would be, else a new one), a note with the order and the link
where they can pay it, and one follow-up task per deal (more failures add to it). The task closes when
a later web order of the customer is paid, or an invoice is made from the deal (:func:`payment_recovered`,
:func:`close_payment_failed`). No email is sent to the customer.

lazy: a failure webhook that arrives after the customer's paid order leaves its task open (webhooks
are delivered in order per order, not across orders); upgrade path: compare the orders' paid times.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.desk.form.assign_to import close_all_assignments
from frappe.utils import escape_html, flt, fmt_money, now_datetime

from ffl_core.crm import TITLE, installed, pos_link_on
from ffl_core.crm.deals import DEAL, STATUS, _statuses, is_closed, new_task, open_deal_for
from ffl_core.crm.mail import _customer_contact, _split_name, contacts_with_email, share_with_team

NOTE = "FCRM Note"
TASK = "CRM Task"
ORDER = "Woo Online Order"
PLACED, TRANSFERRED, CANCELLED, REFUNDED = "placed", "transferred", "cancelled", "refunded"
_TITLES = {
	PLACED: "Web order {0} placed",
	TRANSFERRED: "Web order {0} shipped or picked up",
	CANCELLED: "Web order {0} cancelled",
	REFUNDED: "Web order {0} refunded",
}
_FAILED_NOTE = "Web order {0} payment failed"
_TASK_TITLE = "Payment failed on web order {0} — contact customer"
_TASK_LIKE = "Payment failed on web order %"   # one open task per deal is told apart by its title
_TASK_OPEN = ["Backlog", "Todo", "In Progress"]
# CRM Deal: the paid web order that won it (crm.setup.CUSTOM_FIELDS).
WON_BY = "ffl_web_order"
LEAD = "CRM Lead"
WIN_JOB = "ffl_core.crm.web_orders.web_order_paid"
_UNDONE = ("cancelled", "refunded")   # WooCommerce statuses that take an order back
_CENT = 0.005
UNDO_JOB = "ffl_core.crm.web_orders.web_order_undone"


def won_deal(order: str) -> str | None:
	"""The deal this web order won, if any. None before the migrate adds the field."""
	if not frappe.db.has_column(DEAL, WON_BY):
		return None
	return frappe.db.get_value(DEAL, {WON_BY: order}, "name")


def _open_deal(email: str) -> str | None:
	"""The newest open deal this address's contact is on, a sell request left out (``crm.sell_requests``: the
	store buying is no sale to win or note). Never makes one."""
	from ffl_core.crm.sell_requests import SELL, ready

	contacts = contacts_with_email(email)
	deals = contacts and frappe.get_all("CRM Contacts", filters={"parenttype": DEAL, "contact": ["in", list(contacts)]},
		pluck="parent")
	if not deals:
		return None
	filters = {"name": ["in", deals], "status": ["not in", _statuses(closed=True) or [""]]}
	if ready():
		filters[SELL] = 0
	found = frappe.get_all(DEAL, filters=filters, pluck="name", order_by="modified desc", limit=1)
	return found[0] if found else None


def note_web_order(order: str, event: str, key: str = "", amount: float | None = None) -> None:
	"""Add the note for *event* on the order's customer's open deal. *key* tells repeats of an event apart
	(a second refund); *amount* is what the note reports, the order's total if not given."""
	if not installed() or not pos_link_on():
		return
	frappe.db.savepoint("ffl_crm_web_order")
	try:
		row = frappe.db.get_value(ORDER, order, ["customer_email", "woo_order_id", "order_total", "currency"], as_dict=True)
		email = ((row and row.customer_email) or "").strip().lower()
		deal = row and (won_deal(order) or (email and _open_deal(email)))
		if deal:
			_note(deal, row, event, key, amount)
	except Exception:
		frappe.db.rollback(save_point="ffl_crm_web_order")
		frappe.log_error(title=f"[{TITLE}] Web order note not added: {order}", message=frappe.get_traceback())


def _note(deal: str, row, event: str, key: str = "", amount: float | None = None) -> None:
	"""The note for *event* of the order *row* on *deal*, once."""
	title = _(_TITLES[event]).format(row.woo_order_id)
	if key:
		title = f"{title} ({key})"
	if frappe.db.exists(NOTE, {"reference_doctype": DEAL, "reference_docname": deal, "title": title}):
		return
	total = fmt_money(row.order_total if amount is None else amount, currency=row.currency)
	frappe.get_doc({"doctype": NOTE, "title": title, "content": f"<p>{frappe.utils.escape_html(total)}</p>",
		"reference_doctype": DEAL, "reference_docname": deal}).insert(ignore_permissions=True)


# A paid web order wins its sale -----------------------------------------------------------------------

def after_commit(order: str, paid: bool) -> None:
	"""Queue :func:`web_order_paid` (*paid*) or :func:`web_order_undone` for *order*, once the webhook's
	transaction commits: nothing is written into it. Off with the deal to POS switch. Never raises."""
	try:
		if order and installed() and pos_link_on():
			job = WIN_JOB if paid else UNDO_JOB
			frappe.enqueue(job, queue="short", enqueue_after_commit=True, job_id=f"{job}:{order}",
				deduplicate=True, order=order)
	except Exception:  # noqa: BLE001 — the deal stays as it was; staff can still close it by hand
		frappe.log_error(title=f"[{TITLE}] Web order not passed to the CRM: {order}", message=frappe.get_traceback())


def web_order_paid(order: str, hand_closed: bool = False) -> str | None:
	"""A paid web order wins the open deal or the lead it closes (see the module). *hand_closed* (the
	upgrade patch only) also takes a lead staff set to a Won status by hand that was never converted.
	Safe to run any number of times: an order wins once, a won deal and a converted lead are not open.
	Never raises: a failure is rolled back and logged. Returns the deal won."""
	from ffl_core.utils import guest_job_elevated

	if not installed() or not pos_link_on() or not frappe.db.has_column(DEAL, WON_BY):
		return None
	with guest_job_elevated():   # queued from the Guest webhook
		frappe.db.savepoint("ffl_crm_web_win")
		try:
			return _win_for(order, hand_closed)
		except Exception:
			frappe.db.rollback(save_point="ffl_crm_web_win")
			frappe.log_error(title=f"[{TITLE}] Web order did not win its deal: {order}", message=frappe.get_traceback())
	return None


def standing_total(order: str | None) -> float | None:
	"""The web order's total while it stands; None once it is cancelled or fully refunded, whatever its
	fulfilment status: the cancel webhook writes Cancelled, and refunds that add up to the total while
	WooCommerce still says completed count as a full refund too."""
	row = order and frappe.db.get_value(ORDER, order, ["order_status", "woo_status", "order_total"], as_dict=True)
	if not row or row.order_status == "Cancelled" or (row.woo_status or "").strip().lower() in _UNDONE:
		return None
	refunded = flt(frappe.db.sql("""SELECT SUM(refund_amount) FROM `tabWoo Online Order Refund`
		WHERE parenttype = %s AND parent = %s""", (ORDER, order))[0][0])
	if refunded and refunded >= flt(row.order_total) - _CENT:
		return None
	return flt(row.order_total)


def _paid_order(order: str):
	from ffl_woo_sync.woocommerce.fulfillment import _is_paid_woo_status

	row = frappe.db.get_value(ORDER, order, ["name", "customer_email", "customer", "woo_order_id", "order_total",
		"currency", "order_status", "woo_status", "creation"], as_dict=True)
	if not row or not _is_paid_woo_status(row.woo_status) or standing_total(order) is None:
		return None
	return row


def _win_for(order: str, hand_closed: bool) -> str | None:
	row = _paid_order(order)
	email = ((row and row.customer_email) or "").strip().lower()
	if not email or won_deal(order):
		return None
	deal = _open_deal(email)
	if deal and frappe.db.get_value(DEAL, deal, "creation") <= row.creation:
		if _order_opened(deal) or _holds_gun(deal):
			return None   # the order made at the counter or the held gun decides this deal
		return _win(frappe.get_doc(DEAL, deal, for_update=True), row)
	deal = _deal_from_lead(email, row.creation, hand_closed)
	return _win(frappe.get_doc(DEAL, deal), row) if deal else None


def _order_opened(deal: str) -> bool:
	"""An invoice made from the deal, or the Create Order draft its Open order button saved."""
	from ffl_core.crm.orders import DEAL_FIELD

	if frappe.db.exists("Sales Invoice", {DEAL_FIELD: deal, "docstatus": ["<", 2]}):
		return True
	# The draft's payload is JSON text: as the server writes it, and as the page does.
	return bool(frappe.db.sql("""SELECT name FROM `tabCreate Order Draft`
		WHERE payload LIKE %(spaced)s OR payload LIKE %(tight)s LIMIT 1""",
		{"spaced": f'%"crm_deal": {frappe.as_json(deal)}%', "tight": f'%"crm_deal":{frappe.as_json(deal)}%'}))


def _holds_gun(deal: str) -> bool:
	from ffl_core.crm.holds import card_lines

	return bool(card_lines(deal))


def _lead_statuses(type_: str) -> list[str]:
	return frappe.get_all("CRM Lead Status", filters={"type": type_}, pluck="name")


def _lead_to_convert(email: str, before, hand_closed: bool) -> str | None:
	"""The newest lead with this email, made by *before*, not converted, with no deal, not lost, not
	other mail, and not won (with *hand_closed*: a won lead counts)."""
	from ffl_core.crm.mail import OTHER_MAIL

	skip = _lead_statuses("Lost") + [OTHER_MAIL] + ([] if hand_closed else _lead_statuses("Won"))
	for lead in frappe.get_all(LEAD, filters={"email": email, "converted": 0, "status": ["not in", skip],
			"creation": ["<=", before]}, pluck="name", order_by="creation desc"):
		if not frappe.db.exists(DEAL, {"lead": lead}):
			return lead
	return None


def _deal_from_lead(email: str, before, hand_closed: bool = False) -> str | None:
	"""The deal CRM's conversion makes of the customer's newest lead (:func:`_lead_to_convert`), None when
	they have none. Run as Administrator from the Guest webhook, as the paid-order job is."""
	from ffl_core.utils import guest_job_elevated

	lead = _lead_to_convert(email, before, hand_closed)
	if not lead:
		return None
	with guest_job_elevated():
		return _convert(lead)


def _convert(lead: str) -> str:
	"""CRM's own conversion (what its Convert to deal button calls): the lead's owner owns the deal and
	the lead's mail shows on it. Run as the owner, so CRM's assignment of the deal to them is their own
	and tells nobody; anyone else on the lead is assigned without the acknowledgement popup."""
	from crm.fcrm.doctype.crm_lead.crm_lead import convert_to_deal

	from ffl_core.assignment_notice import SYSTEM_ALERT, marked

	doc = frappe.get_doc(LEAD, lead)
	doc.flags.ignore_permissions = True   # the system acts on a paid order, for nobody's session
	owner = doc.get("lead_owner")
	actor = owner if owner and frappe.db.get_value("User", owner, "enabled") else frappe.session.user
	prev = frappe.session.user
	frappe.set_user(actor)
	try:
		with marked(SYSTEM_ALERT):
			return convert_to_deal(lead=lead, doc=doc, if_converted="Return Existing")
	finally:
		frappe.set_user(prev)


def _win(deal, row) -> str | None:
	"""Set *deal* Won by the order *row*: its value the order's total, the order's customer linked."""
	from ffl_core.crm.customer import link_unless_taken, linked_customer
	from ffl_core.crm.orders import _mark_won, _save, deal_sales

	if is_closed(deal.status):
		return None   # a conversion that returned a deal closed meanwhile
	_mark_won(deal)
	deal.set(WON_BY, row.name)
	deal.deal_value = deal_sales(deal.name, row.name)[0]
	_save(deal)
	if row.customer and not linked_customer(deal) and frappe.db.exists("Customer", row.customer):
		link_unless_taken(deal, row.customer)
	_note(deal.name, row, PLACED)
	return deal.name


def web_order_undone(order: str) -> list[str]:
	"""A web order cancelled or fully refunded: the deal it won goes back to the open status it had
	before (its status change log; else the first open status) unless an invoice made from it stands,
	and its value is what its invoices make, nothing without. Never raises. Returns the deals reopened."""
	from ffl_core.utils import guest_job_elevated

	if not installed() or not pos_link_on() or not frappe.db.has_column(DEAL, WON_BY) \
			or not frappe.db.exists(ORDER, order) or standing_total(order) is not None:
		return []
	done = []
	with guest_job_elevated():
		for name in frappe.get_all(DEAL, filters={WON_BY: order}, pluck="name"):
			frappe.db.savepoint("ffl_crm_web_undo")
			try:
				_reopen(frappe.get_doc(DEAL, name, for_update=True))
				done.append(name)
			except Exception:
				frappe.db.rollback(save_point="ffl_crm_web_undo")
				frappe.log_error(title=f"[{TITLE}] Deal not reopened: {name}", message=frappe.get_traceback())
	return done


def _reopen(deal) -> None:
	from ffl_core.crm.orders import DEAL_FIELD, _save, deal_sales

	deal.set(WON_BY, None)
	deal.deal_value = deal_sales(deal.name)[0]
	sold = frappe.db.exists("Sales Invoice", {DEAL_FIELD: deal.name, "docstatus": 1, "is_return": 0, "is_consolidated": 0})
	if frappe.db.get_value(STATUS, deal.status, "type") == "Won" and not sold:
		deal.status = _status_before(deal)
		deal.closed_date = None
	_save(deal)


def _status_before(deal) -> str:
	"""The open status the deal left for the one it has now (its status change log), else the site's
	first open status."""
	for row in reversed(deal.get("status_change_log") or []):
		if row.get("to") == deal.status:
			before = row.get("from")
			if before and frappe.db.exists(STATUS, before) and not is_closed(before):
				return before
			break
	return (frappe.get_all(STATUS, filters={"type": "Open"}, pluck="name", order_by="position asc", limit=1)
		or _statuses(closed=False))[0]


# Payment failed -------------------------------------------------------------------------------------

def _contact_of(email: str, full_name: str, phone: str) -> str:
	"""The Contact to put a new deal on: the one of a POS customer, else any that holds the address,
	else a new one (a buyer who never became a customer)."""
	contact = _customer_contact(email)
	if contact:
		return contact
	found = contacts_with_email(email)
	if found:
		return sorted(found)[0]
	first, last = _split_name((full_name or "").replace("<", " ").replace(">", " "))   # a name Frappe refuses in a doc name
	doc = {"doctype": "Contact", "first_name": first or email.split("@", 1)[0], "last_name": last,
		"email_ids": [{"email_id": email, "is_primary": 1}]}
	if phone:
		doc["phone_nos"] = [{"phone": phone, "is_primary_phone": 1}]
	# The webhook is a guest request: the system makes the contact, as CRM's own mail intake does.
	return frappe.get_doc(doc).insert(ignore_permissions=True).name


def _failure_html(order_id: str, total: str, items: list[str], pay_link: str, method: str, phone: str) -> str:
	lines = [f"<b>{escape_html(_('Web order {0}').format(order_id))}</b>: {escape_html(total)}"]
	if items:
		lines.append(escape_html(", ".join(items)))
	if method:
		lines.append(escape_html(_("Paid with: {0}").format(method)))
	if phone:
		lines.append(escape_html(_("Phone: {0}").format(phone)))
	if pay_link.startswith(("http://", "https://")):
		lines.append(f'<a href="{escape_html(pay_link)}">{escape_html(_("Link for the customer to pay this order"))}</a>')
	return "".join(f"<p>{line}</p>" for line in lines)


def _add_to_task(deal: str, order_id: str, html: str) -> None:
	"""The deal's open payment-failed task gets this order added; a deal with none gets one."""
	task = frappe.db.get_value(TASK, {"reference_doctype": DEAL, "reference_docname": deal,
		"status": ["in", _TASK_OPEN], "title": ["like", _TASK_LIKE]}, ["name", "description"], as_dict=True)
	if task:
		frappe.db.set_value(TASK, task.name, "description", (task.description or "") + html)
		return
	# A deal nobody owns is the team's (shared on creation): its task is readable by every Sales User.
	new_task(_(_TASK_TITLE).format(order_id), html, frappe.db.get_value(DEAL, deal, "deal_owner"), deal,
		priority="High", due_date=now_datetime())


def payment_failed(order_id: str, email: str, full_name: str = "", phone: str = "", total: float = 0,
		currency: str | None = None, items: list[str] | None = None, pay_link: str = "", method: str = "") -> None:
	"""A web order's payment failed (the webhook decides what counts): note it on the customer's open deal,
	else on the deal their lead becomes (CRM's conversion, as a paid order's would), else on a new one,
	and leave the team one task to contact them. *items* are lines of text, *pay_link* where the customer
	can pay this order. Once per order: Woo redelivers."""
	email = (email or "").strip().lower()
	if not email or not installed() or not pos_link_on():
		return
	title = _(_FAILED_NOTE).format(order_id)
	if frappe.db.exists(NOTE, {"reference_doctype": DEAL, "title": title}):
		return
	frappe.db.savepoint("ffl_crm_payment_failed")
	try:
		deal = _open_deal(email) or _deal_from_lead(email, now_datetime())
		if not deal:
			# new: their only open deals, if any, are sell requests (_open_deal)
			deal = open_deal_for(_contact_of(email, full_name, phone), new=True)
			share_with_team(DEAL, deal)
		html = _failure_html(str(order_id), fmt_money(total, currency=currency), items or [], pay_link, method, phone)
		frappe.get_doc({"doctype": NOTE, "title": title, "content": html,
			"reference_doctype": DEAL, "reference_docname": deal}).insert(ignore_permissions=True)
		_add_to_task(deal, str(order_id), html)
	except Exception:
		frappe.db.rollback(save_point="ffl_crm_payment_failed")
		frappe.log_error(title=f"[{TITLE}] Payment failure not followed up: web order {order_id}", message=frappe.get_traceback())


def close_payment_failed(deals: list[str], recovered_by: str) -> None:
	"""The open payment-failed tasks on these deals are done: the customer paid. Each deal gets a note
	saying *recovered_by*. Never fails the caller: a failure is rolled back and logged."""
	if not deals or not installed() or not pos_link_on():
		return
	frappe.db.savepoint("ffl_crm_payment_recovered")
	try:
		for task in frappe.get_all(TASK, filters={"reference_doctype": DEAL, "reference_docname": ["in", deals],
				"status": ["in", _TASK_OPEN], "title": ["like", _TASK_LIKE]}, fields=["name", "reference_docname"]):
			doc = frappe.get_doc(TASK, task.name)
			doc.status = "Done"
			doc.save(ignore_permissions=True)
			close_all_assignments(TASK, task.name, ignore_permissions=True)
			frappe.get_doc({"doctype": NOTE, "title": recovered_by, "content": f"<p>{escape_html(_('The payment-failed task is done.'))}</p>",
				"reference_doctype": DEAL, "reference_docname": task.reference_docname}).insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point="ffl_crm_payment_recovered")
		frappe.log_error(title=f"[{TITLE}] Payment-failed task not closed: {recovered_by}", message=frappe.get_traceback())


def payment_recovered(order_id: str, email: str) -> None:
	"""A web order of this customer is paid: close their payment-failed tasks, on any of their deals
	(a deal may have been closed since). Never fails the order: a failure is logged."""
	email = (email or "").strip().lower()
	if not email or not installed() or not pos_link_on():
		return
	try:
		if not frappe.db.exists(TASK, {"status": ["in", _TASK_OPEN], "title": ["like", _TASK_LIKE]}):
			return   # every paid order comes by here: most days there is nothing to close
		contacts = contacts_with_email(email)
		if contacts:
			deals = frappe.get_all("CRM Contacts", filters={"parenttype": DEAL, "contact": ["in", list(contacts)]}, pluck="parent")
			close_payment_failed(deals, _("Recovered by web order {0}").format(order_id))
	except Exception:
		frappe.log_error(title=f"[{TITLE}] Payment-failed task not closed: web order {order_id}", message=frappe.get_traceback())


# One-off repair of deals made before a failed payment converted the customer's lead -----------------

def link_failed_payment_leads(dry_run: int | str = 1) -> list[dict]:
	"""Deals a failed payment made while the customer's lead sat unconverted: the guest webhook's deals
	(owner Guest, no lead, with a payment-failed note) whose customer had a lead made before the deal,
	by :func:`_lead_to_convert`'s rules. Run by hand, never by a patch::

		bench --site <site> execute ffl_core.crm.web_orders.link_failed_payment_leads
		bench --site <site> execute ffl_core.crm.web_orders.link_failed_payment_leads --kwargs '{"dry_run": 0}'

	Only an explicit 0 applies: the deal's ``lead`` is set (CRM then shows the lead's mail and notes on its
	timeline) and the lead is marked converted exactly as CRM's own conversion does. The deal's owner, status
	and value are left as they are. Returns one row per deal: ``deal``, ``lead``, ``applied``."""
	apply = dry_run in (0, "0", False)
	notes = frappe.get_all(NOTE, filters={"reference_doctype": DEAL, "title": ["like", "Web order % payment failed"]},
		pluck="reference_docname")
	deals = notes and frappe.get_all(DEAL, filters={"name": ["in", notes], "owner": "Guest"},
		fields=["name", "creation", "lead"], order_by="creation asc")
	rows, taken = [], set()
	for deal in deals or []:
		if deal.lead:
			continue
		contacts = frappe.get_all("CRM Contacts", filters={"parenttype": DEAL, "parent": deal.name}, pluck="contact")
		emails = contacts and frappe.get_all("Contact Email", filters={"parenttype": "Contact", "parent": ["in", contacts]},
			pluck="email_id")
		for email in sorted({e.strip().lower() for e in emails or [] if e}):
			lead = _lead_to_convert(email, deal.creation, False)
			if lead and lead not in taken:
				break
		else:
			continue
		taken.add(lead)
		if apply:
			_link(deal.name, lead)
		rows.append({"deal": deal.name, "lead": lead, "applied": apply})
	return rows


def _link(deal: str, lead: str) -> None:
	from crm.fcrm.doctype.crm_lead.crm_lead import mark_converted

	from ffl_core.utils import commit

	frappe.db.set_value(DEAL, deal, "lead", lead, update_modified=False)
	mark_converted(frappe.get_doc(LEAD, lead))
	commit()
