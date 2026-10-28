"""Memberships: plans, sign-up, activation on payment, cancel, renewal settings.

Off until a store creates a **Membership Plan**: with no plan nothing here runs and every sale
is what it was.

* **Plan** — a billed service item (non-stock), a billing period (Monthly / Yearly), a price and
  an optional registration fee (its own item). Saving a plan points the items at the Deferred
  Membership Revenue / Membership Revenue accounts with deferred revenue on, and sets their
  selling price (:func:`sync_plan_item`). Whether a line is taxed follows the item's own Item
  Tax Template: nothing here decides taxability.
* **Sold only as Sales Invoices** — a POS Invoice is merged at the register's close, and the
  merge drops the service dates deferred revenue needs; so a POS Invoice holding a plan's item
  (or its registration item) is refused (:func:`refuse_on_pos_invoice`).
* **Sign-up** (Customer form → Memberships panel): :func:`start_membership` makes the Pending
  record; a plan's terms (a Per document Agreement Template) are signed for that record with
  the existing agreement flow; :func:`invoice_membership` builds and submits the Sales Invoice —
  the plan line from today to the period end (:func:`period_end`), and the registration line
  over the same dates on the customer's first membership of the plan only. The counter then
  takes payment the usual way (terminal charge, saving the card for recurring charges when
  auto-renew is on — see :func:`saved_card_usage` — or any other tender).
* **Active only once paid** — a Payment Entry against the invoice that leaves nothing
  outstanding activates the membership (:func:`activate_paid_memberships`) and links the card
  the charge saved. Paid without a saved card, auto-renew is switched off: a renewal with no
  card would only fail.
* **Cancel** (managers): at period end by default — no further renewal, the status turns
  Cancelled the day after the period ends (:func:`close_ended_memberships`); "end now" sets
  ``service_stop_date`` on the current invoice's deferred lines, so ERPNext moves what is left
  deferred to income that day. A membership invoice is never cancelled for a refund: refunds
  are credit notes, made by hand (:func:`refuse_invoice_cancel`).

* **Renewal** (:func:`renew_memberships`, every morning): an auto-renewing membership whose period
  has ended gets the next period's invoice — from the day after the old period ends, never
  shifted, ending the day before the next anniversary of the start day (:func:`renewal_period`) —
  and the saved card is charged for it through the card-on-file service (the
  ``ffl_membership_charge_saved_card`` hook). Approved: Paid, the period advances, Active.
  Declined: the invoice is voided (:func:`void_invoice`) and the membership goes Past Due for the
  plan's grace days, or Suspended at once when the plan has no grace. No chargeable card: no
  invoice at all. An answer the gateway never gave leaves the invoice and its charge record
  waiting; the job never charges it again and a manager resolves it from **Needs attention**
  (Memberships page). At most one open renewal invoice per membership and period: a retry
  charges that one.
* **Member or not** — :func:`is_member`: Active, or Past Due within its grace days.
* **Memberships page** (Gun Shop sidebar → Customers): :func:`page_overview` (totals, plans, Needs
  attention) and :func:`page_members` (the member list).

Member benefits are not here yet.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import (add_days, add_months, add_to_date, cint, escape_html, flt, fmt_money, getdate,
	now_datetime, nowdate, strip_html)

from ffl_core.price_approval import mark_system_priced
from ffl_core.roles import COUNTER_ROLES, CUSTOMER_MANAGER_ROLES
from ffl_core.utils import commit, single_flag

DOCTYPE = "Membership"
PLAN = "Membership Plan"
INVOICE_ROW = "Membership Invoice"
MONTHLY, YEARLY = "Monthly", "Yearly"
PENDING, ACTIVE, PAST_DUE, SUSPENDED = "Pending", "Active", "Past Due", "Suspended"
CANCELLED, EXPIRED = "Cancelled", "Expired"
# A record that still holds the customer's place in the plan (one per customer per plan).
LIVE = (PENDING, ACTIVE, PAST_DUE, SUSPENDED)
UNPAID, PAID = "Unpaid", "Paid"
RECURRING = "RECURRING"
# Read by the Memberships panel: the counter. Cancelling is a manager's decision.
READ_ROLES = COUNTER_ROLES + ("Accounts User", "Accounts Manager")
POS_REFUSAL = "Sell memberships from the customer's Memberships panel (Customer form), not the register."
SETTINGS = "Membership Settings"
# ffl_integrations answers these (payments/saved_cards.py); with no answer no card is chargeable.
CHARGE_HOOK = "ffl_membership_charge_saved_card"
CHARGEABLE_HOOK = "ffl_membership_card_chargeable"
# A sign-up still unpaid after this many days shows in Needs attention.
STALE_PENDING_DAYS = 3
# What one renewal attempt came to (the job's tally; Retry now's answer).
RENEWED, DECLINED, NO_CARD, UNKNOWN, NOT_STARTED, SUSPENDED_NOW, SKIPPED = (
	"renewed", "declined", "no_card", "unknown", "not_started", "suspended", "skipped")


# ------------------------------------------------------------------ periods and plans


def period_end(start, billing_period: str):
	"""The last day of a period starting on *start*: the day before the same day next month
	(Monthly) or next year (Yearly). A day the next month lacks clamps to that month's last day
	first, so a Monthly period from Jan 31 ends Feb 27 (Feb 28 − 1), from Jan 30 too."""
	months = 12 if billing_period == YEARLY else 1
	return getdate(add_days(add_months(getdate(start), months), -1))


def plan_items() -> set[str]:
	"""Every item a plan bills: the plan items and their registration items."""
	out = set()
	for row in frappe.get_all(PLAN, fields=["item", "registration_item"]):
		out.update(x for x in (row.item, row.registration_item) if x)
	return out


def sync_plan_item(plan) -> None:
	"""Point a plan's items at the membership accounts, deferred by day, and set their selling
	price from the plan. Item rows are written directly (an Item.save fills one company's default
	warehouse on every Item Default row and then refuses the others — setup/gunsmith)."""
	from ffl_core.setup import membership as setup

	months = 12 if plan.billing_period == YEARLY else 1
	frappe.db.set_value("Item", plan.item, {"enable_deferred_revenue": 1, "no_of_months": months})
	if plan.registration_item:
		frappe.db.set_value("Item", plan.registration_item, "enable_deferred_revenue", 1)
	for code in filter(None, (plan.item, plan.registration_item)):
		_item_defaults(code, setup)
	_item_price(plan.item, plan.price)
	if plan.registration_item and flt(plan.registration_fee) > 0:
		_item_price(plan.registration_item, plan.registration_fee)
	for code in filter(None, (plan.item, plan.registration_item)):
		frappe.clear_document_cache("Item", code)


def _item_defaults(item_code: str, setup) -> None:
	rows = {r.company: r for r in frappe.get_all("Item Default", filters={"parent": item_code,
		"parenttype": "Item"}, fields=["name", "company", "income_account", "deferred_revenue_account", "idx"])}
	last = max((r.idx for r in rows.values()), default=0)
	for company in frappe.get_all("Company", pluck="name"):
		deferred, income = setup.accounts(company)
		if not (deferred and income):
			continue
		row = rows.get(company)
		if row:
			if (row.income_account, row.deferred_revenue_account) != (income, deferred):
				frappe.db.set_value("Item Default", row.name, {"income_account": income,
					"deferred_revenue_account": deferred}, update_modified=False)
			continue
		last += 1
		frappe.get_doc({"doctype": "Item Default", "name": frappe.generate_hash(length=10), "parent": item_code,
			"parenttype": "Item", "parentfield": "item_defaults", "idx": last, "company": company,
			"income_account": income, "deferred_revenue_account": deferred}).db_insert()


def _item_price(item_code: str, rate) -> None:
	from ffl_core.item_hooks import _default_selling_price_list

	price_list = _default_selling_price_list()
	if not price_list:
		return
	existing = frappe.db.get_value("Item Price", {"item_code": item_code, "price_list": price_list,
		"selling": 1}, ["name", "price_list_rate"], as_dict=True)
	if existing:
		if flt(existing.price_list_rate) != flt(rate):
			frappe.db.set_value("Item Price", existing.name, "price_list_rate", flt(rate))
		return
	frappe.get_doc({"doctype": "Item Price", "item_code": item_code, "price_list": price_list,
		"price_list_rate": flt(rate)}).insert(ignore_permissions=True)


# ------------------------------------------------------------------ register guard


def refuse_on_pos_invoice(doc, method=None) -> None:
	"""POS Invoice validate: no membership item at the register (the merge at the register's
	close drops the service dates, and with them the whole day's merge)."""
	codes = {r.item_code for r in doc.get("items") or [] if r.item_code}
	if not codes or not frappe.db.count(PLAN):
		return
	hit = sorted(codes & plan_items())
	if hit:
		frappe.throw(_(POS_REFUSAL) + " " + _("Item: {0}").format(escape_html(", ".join(hit))),
			title=_("Membership item"))


# ------------------------------------------------------------------ sign-up


def _real_customer(customer: str) -> bool:
	from ffl_core.customer_profile import _system_customer_names

	if customer in _system_customer_names():
		return False
	return not frappe.db.exists("POS Profile", {"customer": customer})


def _default_company() -> str | None:
	return frappe.defaults.get_user_default("Company") \
		or frappe.db.get_single_value("Global Defaults", "default_company") \
		or (frappe.get_all("Company", pluck="name", limit=1) or [None])[0]


@frappe.whitelist(methods=["POST"])
def start_membership(customer: str, plan: str, auto_renew=None) -> dict:
	"""Start selling a membership: the customer's Pending membership of the plan (a Pending one
	already there is picked up again, with the new auto-renew choice).

	Args:
	    customer: Customer name (document id); not the walk-in customer.
	    plan: Membership Plan name; must be active.
	    auto_renew: 1 / 0; blank = the plan's default.

	Returns:
	    The panel row of the membership ({name, plan, status, auto_renew, terms_template,
	    terms_state, ...}).
	"""
	frappe.only_for(COUNTER_ROLES)
	frappe.has_permission("Customer", "read", doc=customer, throw=True)
	if not customer or not frappe.db.exists("Customer", customer):
		frappe.throw(_("Customer {0} not found.").format(escape_html(customer or "")))
	if not _real_customer(customer):
		frappe.throw(_("Pick the customer first: a membership cannot be sold to the walk-in customer."))
	p = frappe.db.get_value(PLAN, plan or "", ["name", "active", "auto_renew_default"], as_dict=True)
	if not p:
		frappe.throw(_("Membership plan {0} not found.").format(escape_html(plan or "")))
	if not p.active:
		frappe.throw(_("{0} is switched off and cannot be sold.").format(escape_html(p.name)))
	renew = cint(p.auto_renew_default) if auto_renew in (None, "") else cint(auto_renew)
	# Two screens selling the same plan to the same customer serialize on the customer.
	frappe.db.get_value("Customer", customer, "name", for_update=True)
	live = frappe.db.get_value(DOCTYPE, {"customer": customer, "plan": p.name, "status": ("in", LIVE)},
		["name", "status"], as_dict=True)
	if live and live.status != PENDING:
		frappe.throw(_("{0} already has a {1} membership of {2} ({3}).").format(escape_html(customer),
			_(live.status), escape_html(p.name), live.name), title=_("Already a member"))
	if live:
		doc = frappe.get_doc(DOCTYPE, live.name)
		doc.auto_renew = renew
	else:
		doc = frappe.get_doc({"doctype": DOCTYPE, "customer": customer, "plan": p.name, "status": PENDING,
			"auto_renew": renew, "company": _default_company()})
	doc.flags.via_membership = True
	doc.save(ignore_permissions=True)
	return _row(doc)


def _terms_state(doc, template: str | None) -> str | None:
	if not template:
		return None
	from ffl_core.agreements import agreement_status

	return agreement_status(doc.customer, template, reference=(DOCTYPE, doc.name))["state"]


def _first_of_plan(doc) -> bool:
	"""No other membership of this customer in this plan ever started (was paid)."""
	return not frappe.db.exists(DOCTYPE, {"customer": doc.customer, "plan": doc.plan,
		"name": ("!=", doc.name), "start_date": ("is", "set")})


@frappe.whitelist(methods=["POST"])
def invoice_membership(membership: str) -> dict:
	"""Build and submit the Pending membership's first Sales Invoice (or hand back the unpaid one
	already made), after the plan's terms are signed. The counter takes payment against it.

	Args:
	    membership: Membership name (Pending).

	Returns:
	    {sales_invoice, outstanding, customer, auto_renew}.
	"""
	frappe.only_for(COUNTER_ROLES)
	name = frappe.db.get_value(DOCTYPE, membership or "", "name", for_update=True)
	if not name:
		frappe.throw(_("Membership {0} not found.").format(escape_html(membership or "")))
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.status != PENDING:
		frappe.throw(_("{0} is {1}, not waiting for its first payment.").format(doc.name, _(doc.status)))
	plan = frappe.get_doc(PLAN, doc.plan)
	if not plan.active:
		frappe.throw(_("{0} is switched off and cannot be sold.").format(escape_html(plan.name)))
	state = _terms_state(doc, plan.terms_template)
	if state is not None and state not in ("signed", "expiring"):
		frappe.throw(_("{0} has not signed {1} for this membership yet.").format(
			escape_html(doc.customer_name or doc.customer), escape_html(plan.terms_template)),
			title=_("Terms not signed"))
	row = _unpaid_row(doc)
	if not row:
		si = _make_invoice(doc, plan, getdate(nowdate()))
		row = doc.append("invoices", {"sales_invoice": si.name, "period_start": si.items[0].service_start_date,
			"period_end": si.items[0].service_end_date, "status": UNPAID})
		doc.flags.via_membership = True
		doc.save(ignore_permissions=True)
	return {"sales_invoice": row.sales_invoice, "customer": doc.customer, "auto_renew": cint(doc.auto_renew),
		"outstanding": flt(frappe.db.get_value("Sales Invoice", row.sales_invoice, "outstanding_amount"))}


def _unpaid_row(doc):
	for row in doc.get("invoices") or []:
		if row.status == UNPAID and frappe.db.get_value("Sales Invoice", row.sales_invoice, "docstatus") == 1:
			return row
	return None


def _make_invoice(doc, plan, start, *, end=None, posting=None, renewal=False):
	"""The submitted Sales Invoice for one period from *start* (to *end*, default
	:func:`period_end`), posted on *posting* (default *start*): the plan line, plus the registration
	line on the customer's first membership of the plan — never on a renewal. Both lines carry the
	service dates and the membership accounts, so ERPNext defers them day by day. A renewal
	invoice sends no receipt on submit: the card may yet decline it (:func:`settle` sends it once
	paid)."""
	from ffl_core.api.manual_order import _apply_default_tax_template
	from ffl_core.setup import membership as setup

	company = doc.company or _default_company()
	deferred, income = setup.accounts(company)
	if not (deferred and income):
		frappe.throw(_("{0} has no chart of accounts for memberships yet. Run a migrate, then try again.").format(
			escape_html(company)))
	end = getdate(end) if end else period_end(start, plan.billing_period)
	posting = getdate(posting) if posting else getdate(start)
	lines = [(plan.item, flt(plan.price))]
	if not renewal and plan.registration_item and flt(plan.registration_fee) > 0 and _first_of_plan(doc):
		lines.append((plan.registration_item, flt(plan.registration_fee)))
	si = frappe.new_doc("Sales Invoice")
	si.update({"customer": doc.customer, "company": company, "posting_date": posting, "set_posting_time": 1,
		"due_date": posting})
	for item_code, rate in lines:
		si.append("items", {"item_code": item_code, "qty": 1, "rate": rate})
	if not cint(frappe.db.get_value("Customer", doc.customer, "is_tax_exempt")):
		_apply_default_tax_template(si, company)
	si.set_missing_values()
	# Pinned after set_missing_values, which fills item details from the Item and its price.
	for row, (_code, rate) in zip(si.items, lines):
		row.update({"qty": 1, "rate": rate, "price_list_rate": rate, "enable_deferred_revenue": 1,
			"service_start_date": start, "service_end_date": end, "deferred_revenue_account": deferred,
			"income_account": income})
	si.due_date = posting
	si.flags.ignore_permissions = True
	# The plan's own price: never a lowered one for the manager price approval to ask about.
	mark_system_priced(si)
	si.insert()
	suppressed = frappe.flags.get("ffl_suppress_sale_receipt")
	if renewal:
		frappe.flags.ffl_suppress_sale_receipt = True
	try:
		si.submit()
	finally:
		frappe.flags.ffl_suppress_sale_receipt = suppressed
	return si


# ------------------------------------------------------------------ payment → Active


def saved_card_usage(sales_invoice: str) -> str | None:
	"""``RECURRING`` when *sales_invoice* is a membership invoice and the membership renews
	automatically (the card it saves is charged every period), else None (the default)."""
	row = frappe.db.sql(f"""select m.auto_renew from `tab{INVOICE_ROW}` r join `tab{DOCTYPE}` m on m.name = r.parent
		where r.parenttype = %s and r.sales_invoice = %s limit 1""", (DOCTYPE, sales_invoice or ""), as_dict=True)
	return RECURRING if row and cint(row[0].auto_renew) else None


def activate_paid_memberships(doc, method=None) -> None:
	"""Payment Entry on_submit: a membership waiting for this invoice's payment turns Active once
	the invoice owes nothing. Never blocks the payment (a card may already be charged): a failure
	is logged and the Memberships panel activates it on its next look (:func:`get_memberships`)."""
	invoices = [r.reference_name for r in doc.get("references") or []
		if r.reference_doctype == "Sales Invoice" and r.reference_name]
	if not invoices:
		return
	for name in set(frappe.get_all(INVOICE_ROW, filters={"parenttype": DOCTYPE,
			"sales_invoice": ("in", invoices), "status": UNPAID}, pluck="parent")):
		try:
			frappe.db.savepoint("membership_activation")
			settle(name)
		except Exception:
			frappe.db.rollback(save_point="membership_activation")
			frappe.log_error(title=f"Membership {name} not activated on payment", message=frappe.get_traceback())


def settle(membership: str) -> bool:
	"""Apply the payments that came in: every unpaid invoice row whose invoice now owes nothing turns
	Paid. A Pending membership turns Active on its first invoice; a renewal invoice moves the
	period to the one it paid for (from the original due date — never shifted), turns the
	membership Active again and clears its grace, and its receipt goes out (the renewal invoice
	sent none on submit). Idempotent; True when this call changed anything."""
	if not frappe.db.get_value(DOCTYPE, membership, "name", for_update=True):
		return False
	doc = frappe.get_doc(DOCTYPE, membership)
	changed = False
	receipts = []
	for row in doc.get("invoices") or []:
		if row.status != UNPAID:
			continue
		si = frappe.db.get_value("Sales Invoice", row.sales_invoice, ["docstatus", "outstanding_amount"], as_dict=True)
		if not si or si.docstatus != 1 or flt(si.outstanding_amount) > 0.01:
			continue
		row.status = PAID
		changed = True
		if doc.status == PENDING:
			_activate(doc, row)
		elif doc.status in (ACTIVE, PAST_DUE, SUSPENDED) and (not doc.current_period_end
				or getdate(row.period_end) > getdate(doc.current_period_end)):
			if doc.current_period_end and getdate(row.period_start) > getdate(add_days(doc.current_period_end, 1)):
				doc.start_date = row.period_start      # a rejoin after suspension: renewals count from here
			doc.update({"status": ACTIVE, "current_period_start": row.period_start, "current_period_end": row.period_end,
				"grace_until": None, "last_charge_status": _("Renewed: {0} paid").format(row.sales_invoice)})
			receipts.append(row.sales_invoice)
	if not changed:
		return False
	doc.flags.via_membership = True
	doc.save(ignore_permissions=True)
	if receipts:
		from ffl_core.receipts import send_sale_receipt

		for name in receipts:
			send_sale_receipt(frappe.get_doc("Sales Invoice", name))
	return True


def _activate(doc, row) -> None:
	"""A Pending membership's first invoice is paid: Active from its period, with the card the
	charge saved (no card saved → auto-renew off: a renewal with no card would only fail)."""
	doc.update({"status": ACTIVE, "start_date": row.period_start, "current_period_start": row.period_start,
		"current_period_end": row.period_end, "last_charge_status": _("Paid {0}").format(row.sales_invoice)})
	if cint(doc.auto_renew):
		card = _card_saved_by(row.sales_invoice, doc.customer)
		if card:
			doc.saved_card = card
		else:
			doc.auto_renew = 0
			doc.last_charge_status = _("Paid {0} without saving a card for recurring charges: auto-renew is "
				"off.").format(row.sales_invoice)


def _card_saved_by(sales_invoice: str, customer: str) -> str | None:
	"""The recurring-use card the invoice's terminal charge saved, if any."""
	if not frappe.db.exists("DocType", "Card Terminal Payment"):
		return None
	for card in frappe.get_all("Card Terminal Payment", filters={"sales_invoice": sales_invoice,
			"status": "CHARGED", "save_card": 1, "saved_card": ("is", "set")}, pluck="saved_card",
			order_by="creation desc"):
		if _usable_card(card, customer):
			return card
	return None


def _usable_card(card: str | None, customer: str) -> bool:
	if not card or not frappe.db.exists("DocType", "Saved Card"):
		return False
	row = frappe.db.get_value("Saved Card", card, ["customer", "status", "usage"], as_dict=True)
	return bool(row and row.customer == customer and row.status == "Active" and row.usage == RECURRING)


# ------------------------------------------------------------------ the panel


def _card_summary(card: str | None) -> str:
	if not card or not frappe.db.exists("DocType", "Saved Card"):
		return ""
	return frappe.db.get_value("Saved Card", card, "card_summary") or card


def _row(doc) -> dict:
	plan = frappe.db.get_value(PLAN, doc.plan, ["price", "billing_period", "terms_template"], as_dict=True) \
		or frappe._dict()
	renewing = cint(doc.auto_renew) and doc.status in (ACTIVE, PAST_DUE) and not cint(doc.cancel_at_period_end)
	unpaid = _unpaid_row(doc) if doc.status in LIVE else None
	overdue = doc.status in (ACTIVE, PAST_DUE, SUSPENDED) and bool(doc.current_period_end) \
		and getdate(doc.current_period_end) < getdate(nowdate())
	return {"grace_until": doc.grace_until, "overdue": overdue, "can_retry": overdue
		and not cint(doc.cancel_at_period_end) and _chargeable(doc),
		"charge_waiting": bool(unpaid) and _charge_waiting(unpaid.sales_invoice),
		"name": doc.name, "plan": doc.plan, "status": doc.status, "price": flt(plan.price),
		"billing_period": plan.billing_period, "start_date": doc.start_date,
		"current_period_start": doc.current_period_start, "current_period_end": doc.current_period_end,
		# An overdue renewal has no "next charge" to show: its date has passed (the row says what is owed).
		"next_charge_date": add_days(doc.current_period_end, 1) if renewing and doc.current_period_end
			and not overdue else None,
		"auto_renew": cint(doc.auto_renew), "saved_card": doc.saved_card, "card_summary": _card_summary(doc.saved_card),
		"cancel_at_period_end": cint(doc.cancel_at_period_end), "last_charge_status": doc.last_charge_status,
		"terms_template": plan.terms_template, "terms_state": _terms_state(doc, plan.terms_template)
			if doc.status == PENDING else None,
		"unpaid_invoice": unpaid.sales_invoice if unpaid else None,
		"outstanding": flt(frappe.db.get_value("Sales Invoice", unpaid.sales_invoice, "outstanding_amount"))
			if unpaid else 0}


@frappe.whitelist()
def get_memberships(customer: str) -> dict:
	"""A customer's memberships and the plans on sale, for the Customer form's Memberships panel.
	A membership whose invoice has since been paid another way is settled here.

	Args:
	    customer: Customer name (document id).

	Returns:
	    {plans: [{name, price, billing_period, registration_fee, auto_renew_default,
	    terms_template}], memberships: [panel rows, live first; a row says whether its renewal is
	    overdue, can be retried on the card, or has a card charge waiting], cards: the customer's active
	    recurring-use saved cards [{name, card_summary}], can_cancel: whether the caller may
	    cancel}.
	"""
	frappe.only_for(READ_ROLES)
	frappe.has_permission("Customer", "read", doc=customer, throw=True)
	plans = frappe.get_all(PLAN, filters={"active": 1}, fields=["name", "price", "billing_period",
		"registration_fee", "auto_renew_default", "terms_template"], order_by="plan_name asc")
	names = frappe.get_all(DOCTYPE, filters={"customer": customer}, pluck="name", order_by="creation desc")
	rows = []
	for name in names:
		doc = frappe.get_doc(DOCTYPE, name)
		if doc.status in LIVE and _paid_since(doc) and _settle_quietly(name):
			doc.reload()
		rows.append(_row(doc))
	rows.sort(key=lambda r: r["status"] not in LIVE)
	cards = []
	if frappe.db.exists("DocType", "Saved Card"):
		cards = frappe.get_all("Saved Card", filters={"customer": customer, "status": "Active", "usage": RECURRING},
			fields=["name", "card_summary"], order_by="is_default desc, creation desc")
	return {"plans": plans, "memberships": rows, "cards": cards,
		"can_cancel": bool(set(frappe.get_roles()) & set(CUSTOMER_MANAGER_ROLES))}


def _paid_since(doc) -> bool:
	"""A lock-free look: does a membership's unpaid invoice owe nothing now?"""
	row = _unpaid_row(doc)
	return bool(row) and flt(frappe.db.get_value("Sales Invoice", row.sales_invoice, "outstanding_amount")) <= 0.01


def _settle_quietly(name: str) -> bool:
	"""The panel's catch-up activation. A write racing it (the same membership changed a moment
	ago) only means the panel shows it Pending until the next look; the read never fails."""
	try:
		frappe.db.savepoint("membership_panel_settle")
		return settle(name)
	except frappe.QueryDeadlockError:
		frappe.db.rollback(save_point="membership_panel_settle")
		return False


# ------------------------------------------------------------------ renewal settings


def _locked(membership: str):
	name = frappe.db.get_value(DOCTYPE, membership or "", "name", for_update=True)
	if not name:
		frappe.throw(_("Membership {0} not found.").format(escape_html(membership or "")))
	doc = frappe.get_doc(DOCTYPE, name)
	frappe.has_permission("Customer", "read", doc=doc.customer, throw=True)
	return doc


def _check_card(doc, card: str) -> None:
	if not _usable_card(card, doc.customer):
		frappe.throw(_("{0} is not one of {1}'s active cards saved for recurring charges. A card is saved "
			"for recurring charges when the customer pays for a membership with auto-renew on.").format(
			escape_html(card or ""), escape_html(doc.customer_name or doc.customer)), title=_("Card"))


@frappe.whitelist(methods=["POST"])
def set_auto_renew(membership: str, enabled) -> dict:
	"""Turn a membership's auto-renew on or off. On needs a card saved for recurring charges
	(the membership's own, else the customer's default one); a membership ending at period end
	is a manager's cancellation and stays off.

	Args:
	    membership: Membership name.
	    enabled: 1 to renew automatically, 0 not to.

	Returns:
	    The membership's panel row.
	"""
	frappe.only_for(COUNTER_ROLES)
	doc = _locked(membership)
	if doc.status in (CANCELLED, EXPIRED):
		frappe.throw(_("{0} has ended.").format(doc.name))
	on = cint(enabled)
	if on and cint(doc.cancel_at_period_end):
		frappe.throw(_("{0} ends at the end of this period (cancelled by {1}). Sell a new membership "
			"after it ends instead.").format(doc.name, escape_html(doc.cancelled_by or "")))
	if on and doc.status != PENDING:
		card = doc.saved_card if _usable_card(doc.saved_card, doc.customer) else _default_recurring_card(doc.customer)
		if not card:
			frappe.throw(_("{0} has no card saved for recurring charges. Turn auto-renew on when taking the "
				"membership payment on the terminal, which saves the card.").format(
				escape_html(doc.customer_name or doc.customer)), title=_("No saved card"))
		doc.saved_card = card
	doc.auto_renew = on
	doc.flags.via_membership = True
	doc.save(ignore_permissions=True)
	return _row(doc)


def _default_recurring_card(customer: str) -> str | None:
	if not frappe.db.exists("DocType", "Saved Card"):
		return None
	cards = frappe.get_all("Saved Card", filters={"customer": customer, "status": "Active", "usage": RECURRING},
		pluck="name", order_by="is_default desc, creation desc", limit=1)
	return cards[0] if cards else None


@frappe.whitelist(methods=["POST"])
def change_card(membership: str, saved_card: str) -> dict:
	"""Charge a membership's renewals to another of the customer's cards saved for recurring
	charges.

	Args:
	    membership: Membership name.
	    saved_card: Saved Card name: the customer's, active, saved for recurring charges.

	Returns:
	    The membership's panel row.
	"""
	frappe.only_for(COUNTER_ROLES)
	doc = _locked(membership)
	if doc.status in (CANCELLED, EXPIRED):
		frappe.throw(_("{0} has ended.").format(doc.name))
	_check_card(doc, saved_card)
	doc.saved_card = saved_card
	doc.flags.via_membership = True
	doc.save(ignore_permissions=True)
	return _row(doc)


# ------------------------------------------------------------------ cancel


@frappe.whitelist(methods=["POST"])
def cancel_membership(membership: str, reason: str, end_now=0) -> dict:
	"""Cancel a membership. By default it runs to the end of the period it is paid for and is not
	renewed. ``end_now`` ends it today: the current invoice's deferred lines get today as their
	service stop date, so what is still deferred becomes income today. The invoice itself is never
	cancelled; a refund is a credit note.

	Args:
	    membership: Membership name (Active, Past Due or Suspended).
	    reason: Why (kept on the membership).
	    end_now: 1 to end it today instead of at the period end.

	Returns:
	    The membership's panel row.
	"""
	frappe.only_for(CUSTOMER_MANAGER_ROLES)
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Say why the membership is cancelled."))
	doc = _locked(membership)
	if doc.status not in (ACTIVE, PAST_DUE, SUSPENDED):
		frappe.throw(_("{0} is {1}: only a running membership can be cancelled.").format(doc.name, _(doc.status)))
	today = getdate(nowdate())
	_void_open_renewals(doc, reason)
	doc.update({"auto_renew": 0, "cancelled_on": today, "cancelled_by": frappe.session.user,
		"cancel_reason": reason[:1000]})
	if cint(end_now):
		_stop_service(doc, today)
		doc.status = CANCELLED
		doc.cancel_at_period_end = 0
	else:
		doc.cancel_at_period_end = 1
		if doc.current_period_end and getdate(doc.current_period_end) < today:
			doc.status = CANCELLED
	doc.flags.via_membership = True
	doc.save(ignore_permissions=True)
	return _row(doc)


def _void_open_renewals(doc, reason: str) -> None:
	"""Cancelling a membership with a renewal invoice nobody paid: that invoice goes too (voided
	like a declined one). Refused while money or a card charge touched it."""
	for row in doc.get("invoices") or []:
		if row.status != UNPAID:
			continue
		si = frappe.get_doc("Sales Invoice", row.sales_invoice)
		if si.docstatus != 1:
			continue
		if _touched(si):
			frappe.throw(_("{0} has a payment or a card charge on it. Finish taking the payment, or resolve the "
				"card charge, before cancelling the membership.").format(si.name))
		row.credit_note = void_invoice(si, reason)
		row.status = CANCELLED


def _stop_service(doc, day) -> None:
	"""Set *day* as the service stop date on the deferred lines of the paid invoice whose period
	holds it (ERPNext then books what is left deferred to income on that day)."""
	for row in reversed(doc.get("invoices") or []):
		if row.status != PAID or not (getdate(row.period_start) <= day <= getdate(row.period_end)):
			continue
		si = frappe.get_doc("Sales Invoice", row.sales_invoice)
		touched = False
		for item in si.items:
			if cint(item.enable_deferred_revenue) and not item.service_stop_date \
					and getdate(item.service_start_date) <= day <= getdate(item.service_end_date):
				item.service_stop_date = day
				touched = True
		if touched:
			si.flags.ignore_permissions = True
			si.save()
		return


@frappe.whitelist(methods=["POST"])
def discard_membership(membership: str, reason: str) -> dict:
	"""Drop a Pending membership the customer did not pay for. Its unpaid invoice is cancelled —
	refused once any payment or card charge touched it.

	Args:
	    membership: Membership name (Pending).
	    reason: Why (kept on the membership).

	Returns:
	    The membership's panel row (status Cancelled).
	"""
	frappe.only_for(COUNTER_ROLES)
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Say why the membership is dropped."))
	doc = _locked(membership)
	if doc.status != PENDING:
		frappe.throw(_("{0} is {1}: only a membership waiting for its first payment can be dropped. A manager "
			"cancels a running one.").format(doc.name, _(doc.status)))
	for row in doc.get("invoices") or []:
		if row.status != UNPAID:
			continue
		si = frappe.get_doc("Sales Invoice", row.sales_invoice)
		if si.docstatus == 1:
			if _touched(si):
				frappe.throw(_("{0} has a payment or a card charge on it. Finish taking the payment, or have a "
					"manager refund it, instead of dropping the membership.").format(si.name))
			row.credit_note = void_invoice(si, reason)
		row.status = CANCELLED
	doc.update({"status": CANCELLED, "auto_renew": 0, "cancelled_on": getdate(nowdate()),
		"cancelled_by": frappe.session.user, "cancel_reason": reason[:1000]})
	doc.flags.via_membership = True
	doc.save(ignore_permissions=True)
	return _row(doc)


def _touched(si) -> bool:
	"""Whether any money reached *si* or a card charge on it is captured or still waiting: then it
	is not voided (cpa: outstanding must equal the total)."""
	if flt(si.outstanding_amount) < flt(si.rounded_total or si.grand_total) - 0.005:
		return True
	return bool(frappe.db.exists("DocType", "Card Terminal Payment") and frappe.db.exists("Card Terminal Payment",
		{"sales_invoice": si.name, "status": ("in", ("IN_PROGRESS", "CHARGED"))}))


def void_invoice(si, reason: str, remarks: str | None = None) -> str | None:
	"""Undo an unpaid membership invoice. Cancelled outright only while nothing about it has reached
	the books past its own posting: posted this month and no deferred revenue booked from it yet.
	Otherwise a cancel would rewrite a booked month, so its service stops today and a full credit
	note (not deferred, :func:`book_refunds_at_once`) takes it back. Returns the credit note's name,
	or None when the invoice was cancelled. *remarks* replaces the credit note's default remarks."""
	today = getdate(nowdate())
	if getdate(si.posting_date) >= today.replace(day=1) and not _deferred_booked(si):
		si.flags.via_membership = True
		si.flags.ignore_permissions = True
		si.cancel()
		return None
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

	for item in si.items:
		if cint(item.enable_deferred_revenue) and not item.service_stop_date \
				and getdate(item.service_start_date) <= today:
			item.service_stop_date = min(today, getdate(item.service_end_date))
	si.flags.ignore_permissions = True
	si.save()
	ret = make_sales_return(si.name)
	# update_outstanding_for_self off: the credit note settles the voided invoice instead of standing
	# as a separate credit the customer could spend.
	ret.update({"set_posting_time": 1, "posting_date": today, "update_outstanding_for_self": 0,
		"remarks": (remarks or _("Void of unpaid membership invoice {0}: {1}").format(si.name, reason))[:1000]})
	ret.flags.ignore_permissions = True
	ret.insert()
	# No "Sale Receipt" email for a void of a sale the customer never paid for (receipts.py).
	suppressed = frappe.flags.get("ffl_suppress_sale_receipt")
	frappe.flags.ffl_suppress_sale_receipt = True
	try:
		ret.submit()
	finally:
		frappe.flags.ffl_suppress_sale_receipt = suppressed
	return ret.name


def _deferred_booked(si) -> bool:
	"""Whether month-end deferred processing already booked anything from *si* (GL rows posted
	against Process Deferred Accounting, or a Journal Entry referencing the invoice)."""
	if frappe.db.exists("GL Entry", {"voucher_type": "Sales Invoice", "voucher_no": si.name, "is_cancelled": 0,
			"against_voucher_type": "Process Deferred Accounting"}):
		return True
	return bool(frappe.db.sql("""select 1 from `tabJournal Entry Account` c join `tabJournal Entry` p
		on p.name = c.parent where c.reference_type = 'Sales Invoice' and c.reference_name = %s
		and p.docstatus = 1 limit 1""", si.name))


def book_refunds_at_once(doc, method=None) -> None:
	"""Sales Invoice before_validate: a credit note against a membership invoice books straight to
	Membership Revenue, never deferred (the return copies the deferred flag but not the service
	dates, which ERPNext would refuse; and a refund is not spread over months)."""
	if not cint(doc.is_return) or not doc.return_against:
		return
	if not frappe.db.exists(INVOICE_ROW, {"parenttype": DOCTYPE, "sales_invoice": doc.return_against}):
		return
	for row in doc.items:
		row.enable_deferred_revenue = 0


def refuse_invoice_cancel(doc, method=None) -> None:
	"""Sales Invoice before_cancel: a membership invoice is not cancelled from the invoice. A
	cancel would tear out the month-end deferred entries already booked; a refund is a credit
	note, an unpaid sign-up is dropped from the Memberships panel."""
	if doc.flags.via_membership:
		return
	parent = frappe.db.get_value(INVOICE_ROW, {"parenttype": DOCTYPE, "sales_invoice": doc.name}, "parent")
	if parent:
		frappe.throw(_("{0} is the invoice of membership {1}. To end the membership, cancel it from the customer's "
			"Memberships panel; to give money back, make a credit note. The invoice itself stays.").format(
			doc.name, parent), title=_("Membership invoice"))


# ------------------------------------------------------------------ renewals


def next_period(doc, billing_period: str, today=None):
	"""The period an overdue renewal bills: the one right after the current period (from the
	original due date, never shifted — paying within grace), except for a Suspended membership,
	whose grace is over: it starts again today, so the months it was suspended are not billed."""
	if doc.status == SUSPENDED:
		for row in doc.get("invoices") or []:           # one open rejoin invoice at a time (C4)
			if row.status == UNPAID and doc.current_period_end and row.period_start \
					and getdate(row.period_start) > getdate(add_days(doc.current_period_end, 1)) \
					and frappe.db.get_value("Sales Invoice", row.sales_invoice, "docstatus") == 1:
				return getdate(row.period_start), getdate(row.period_end)
		start = getdate(today or nowdate())
		return start, period_end(start, billing_period)
	return renewal_period(doc, billing_period)


def renewal_period(doc, billing_period: str):
	"""(start, end) of the period after the membership's current one. It starts the day after the
	current period ends — never shifted, even when paid late — and ends the day before the next
	anniversary of the start day, counted from ``start_date`` every time: a member who started on
	the 31st is billed on the last day of a shorter month and on the 31st again after it (chaining
	from a clamped month end would drift to the 28th for good)."""
	start = getdate(add_days(doc.current_period_end, 1))
	anchor = getdate(doc.start_date or doc.current_period_start or start)
	months = 12 if billing_period == YEARLY else 1
	k = max(1, ((start.year - anchor.year) * 12 + start.month - anchor.month) // months - 1)
	while getdate(add_months(anchor, k * months)) <= start:
		k += 1
	return start, getdate(add_days(add_months(anchor, k * months), -1))


def renewals_enabled() -> bool:
	"""Membership Settings → Charge renewals automatically (on until a store turns it off)."""
	return bool(single_flag(SETTINGS, "auto_renew_enabled", default=1))


def _reminder_days() -> int:
	row = frappe.db.sql("select value from `tabSingles` where doctype = %s and field = 'yearly_reminder_days'",
		(SETTINGS,))
	return 7 if not row or row[0][0] in (None, "") else cint(row[0][0])


def _hook(name: str):
	"""The card service's function behind *name* (ffl_integrations registers it), or None. Core
	never imports the payments app: without it no card is chargeable and nothing is charged."""
	paths = frappe.get_hooks(name)
	return frappe.get_attr(paths[-1]) if paths else None


def _chargeable(doc) -> bool:
	"""The membership's card can be charged: the customer's, active, saved for recurring charges,
	and the card service holds a token for it."""
	if not _usable_card(doc.saved_card, doc.customer):
		return False
	check = _hook(CHARGEABLE_HOOK)
	return bool(check and check(doc.saved_card))


def _charge_waiting(sales_invoice: str | None) -> bool:
	"""A card charge on the invoice is still waiting for the gateway's answer (the card service
	locks the invoice against another charge until it is looked up)."""
	return bool(sales_invoice and frappe.db.exists("DocType", "Card Terminal Payment") and frappe.db.exists(
		"Card Terminal Payment", {"sales_invoice": sales_invoice, "status": "IN_PROGRESS"}))


def _last_charge_status(sales_invoice: str) -> str | None:
	if not frappe.db.exists("DocType", "Card Terminal Payment"):
		return None
	return frappe.db.get_value("Card Terminal Payment", {"sales_invoice": sales_invoice}, "status",
		order_by="creation desc")


def _charge_rows(sales_invoice: str) -> int:
	if not frappe.db.exists("DocType", "Card Terminal Payment"):
		return 0
	return frappe.db.count("Card Terminal Payment", {"sales_invoice": sales_invoice})


def _open_renewal(doc, start):
	"""The renewal invoice for the period from *start* that is neither paid nor voided (at most one:
	a retry charges it instead of making another)."""
	for row in doc.get("invoices") or []:
		if row.status == UNPAID and row.period_start and getdate(row.period_start) == getdate(start) \
				and frappe.db.get_value("Sales Invoice", row.sales_invoice, "docstatus") == 1:
			return row
	return None


def _waiting_renewal(doc) -> bool:
	"""Some unpaid renewal invoice of *doc* has a card charge still waiting for the gateway."""
	return any(r.status == UNPAID and _charge_waiting(r.sales_invoice) for r in doc.get("invoices") or [])


def _fmt(day) -> str:
	return frappe.format_value(getdate(day), {"fieldtype": "Date"})


def _save(doc) -> None:
	doc.flags.via_membership = True
	doc.save(ignore_permissions=True)


def renew_memberships() -> dict:
	"""Daily, store-local morning: charge the renewals that are due, retry the past-due ones once a
	day, suspend those whose grace ran out, then send the yearly reminders. Nothing runs with no plan
	or with Membership Settings → Charge renewals automatically off. Each membership is its own
	transaction (one failure is logged and leaves the others alone); running it twice the same day
	tries nobody twice. Returns a tally of the outcomes."""
	if not frappe.db.count(PLAN) or not renewals_enabled():
		return {}
	today = getdate(nowdate())
	due = frappe.get_all(DOCTYPE, filters={"auto_renew": 1, "status": ("in", (ACTIVE, PAST_DUE)),
		"current_period_end": ("<", today)}, pluck="name", order_by="current_period_end asc")
	# Past due with auto-renew since switched off: not charged, but its grace still runs out.
	lapsing = frappe.get_all(DOCTYPE, filters={"auto_renew": 0, "status": PAST_DUE}, pluck="name")
	tally: dict = {}
	for name in due + lapsing:
		out = _renew_safely(name, today)
		tally[out] = tally.get(out, 0) + 1
	tally["reminded"] = send_renewal_reminders(today)
	return tally


def _renew_safely(name: str, today) -> str:
	# Commit first: the membership's row lock must be the first read of a fresh transaction
	# (REPEATABLE READ — a lock after a plain read can see a stale snapshot, or fail with 1020).
	commit()
	frappe.db.savepoint("membership_renewal")
	try:
		out = _renew_one(name, today)
		commit()
		return out
	except Exception:
		try:
			frappe.db.rollback(save_point="membership_renewal")
		except Exception:   # noqa: BLE001 — the card service committed past the savepoint
			frappe.db.rollback()
		frappe.log_error(title=f"Membership {name}: renewal failed", message=frappe.get_traceback())
		commit()
		return "error"


def _renew_one(name: str, today) -> str:
	if not frappe.db.get_value(DOCTYPE, name, "name", for_update=True):
		return SKIPPED
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.status == PAST_DUE and _waiting_renewal(doc):
		return UNKNOWN                               # a manager resolves it first (Needs attention)
	if doc.status == PAST_DUE and (not doc.grace_until or getdate(doc.grace_until) < today):
		doc.status = SUSPENDED
		doc.last_charge_status = _("Suspended {0}: the renewal was not paid within the grace days.").format(_fmt(today))
		_save(doc)
		return SUSPENDED_NOW
	if not (cint(doc.auto_renew) and doc.status in (ACTIVE, PAST_DUE) and not cint(doc.cancel_at_period_end)
			and doc.current_period_end and getdate(doc.current_period_end) < today):
		return SKIPPED
	if doc.last_charge_attempt and getdate(doc.last_charge_attempt) >= today:
		return SKIPPED                               # tried today already
	return _attempt(doc, frappe.get_doc(PLAN, doc.plan), today)


def _attempt(doc, plan, today, *, manual: bool = False) -> str:
	"""One renewal charge for *doc* (row-locked by the caller): the open renewal invoice of the next
	period, else a new one (only when there is a card to charge — no card, no invoice), charged on
	the saved card through the card service. A charge still waiting for the gateway is left alone by
	the job; a manager's Retry now (``manual``) has the card service look it up — it never charges
	twice."""
	start, end = next_period(doc, plan.billing_period, today)
	row = _open_renewal(doc, start)
	waiting = bool(row) and _charge_waiting(row.sales_invoice)
	if waiting and not manual:
		return UNKNOWN
	if row and not waiting and flt(frappe.db.get_value("Sales Invoice", row.sales_invoice, "outstanding_amount")) <= 0.01:
		settle(doc.name)                             # paid some other way since
		return RENEWED
	charge = _hook(CHARGE_HOOK)
	if not charge or (not waiting and not _chargeable(doc)):
		doc.last_charge_attempt = today
		_fail(doc, plan, today, _("No card to charge for the renewal from {0}.").format(_fmt(start)))
		_save(doc)
		return NO_CARD
	frappe.db.savepoint("membership_renewal_charge")
	if not row:
		si = _make_invoice(doc, plan, start, end=end, posting=today, renewal=True)
		row = doc.append("invoices", {"sales_invoice": si.name, "period_start": start, "period_end": end,
			"status": UNPAID})
	invoice = row.sales_invoice
	doc.last_charge_attempt = today
	doc.last_charge_status = _("Charging the card for {0}.").format(invoice)
	_save(doc)
	before = _charge_rows(invoice)
	error = None
	try:
		out = charge(invoice, doc.saved_card, None, frappe.session.user)
	except Exception as e:   # noqa: BLE001 — sorted below by what the card service recorded
		out, error = None, strip_html(str(e))[:300]
	# The card service committed on the way (its charge record is durable before the gateway call),
	# which released the lock: take it again before reading back.
	frappe.db.get_value(DOCTYPE, doc.name, "name", for_update=True)
	doc.reload()
	if out and out.get("ok"):
		settle(doc.name)
		doc.reload()
		if not _open_renewal(doc, start):
			return RENEWED
		error = _("only part of the amount was approved")
	if _charge_waiting(invoice):
		doc.last_charge_status = _("The card charge for {0} was not confirmed by the gateway. It is not charged "
			"again: look it up with Retry now (Membership Settings → Needs attention).").format(invoice)
		_save(doc)
		return UNKNOWN
	if not out and not waiting and _charge_rows(invoice) == before and _rollback_to("membership_renewal_charge"):
		# Refused before any charge record was written — the gateway was never reached (the card
		# processor is off or not set up: the store's problem, not the card's). Nothing stays booked;
		# the job tries again tomorrow.
		doc.reload()
		doc.last_charge_attempt = today
		doc.last_charge_status = _("Renewal not charged: {0} It is tried again tomorrow.").format(error or "")
		_save(doc)
		return NOT_STARTED
	if not out and _charge_rows(invoice) > before and _last_charge_status(invoice) == "CANCELED":
		# The card service reached the processor and it refused the request itself (expired key,
		# feature off, bad setup) — recorded CANCELED, nothing charged. The store's problem, not the
		# card's: the invoice is voided and the membership keeps its status; tried again tomorrow.
		row = next((r for r in doc.invoices if r.sales_invoice == invoice), None)
		if row and row.status == UNPAID:
			_void_declined(doc, row, error or "")
		doc.last_charge_status = _("Renewal not charged: the card processor refused the request ({0}). It is "
			"tried again tomorrow.").format(error or "")
		_save(doc)
		return NOT_STARTED
	why = error or (out or {}).get("error") or _("Card declined.")
	row = next((r for r in doc.invoices if r.sales_invoice == invoice), None)
	if row and row.status == UNPAID:
		_void_declined(doc, row, why)
	_fail(doc, plan, today, _("Renewal charge for {0} declined: {1}").format(invoice, why))
	_save(doc)
	return DECLINED


def _rollback_to(savepoint: str) -> bool:
	try:
		frappe.db.rollback(save_point=savepoint)
		return True
	except Exception:   # noqa: BLE001 — committed past it: handled as a decline instead
		return False


def _void_declined(doc, row, why: str) -> None:
	"""Void the renewal invoice of a declined charge (cancelled while nothing about it reached the
	books past its posting day, else stopped today and taken back by a full credit note:
	:func:`void_invoice`). One with money on it stays open: the next retry charges what is left. A
	void that fails is logged and the invoice stays open the same way."""
	si = frappe.get_doc("Sales Invoice", row.sales_invoice)
	if si.docstatus != 1 or _touched(si):
		return
	frappe.db.savepoint("membership_void")
	try:
		row.credit_note = void_invoice(si, why, remarks=_("Void of declined renewal {0}").format(si.name))
		row.status = CANCELLED
	except Exception:
		frappe.db.rollback(save_point="membership_void")
		frappe.log_error(title=f"Membership {doc.name}: declined renewal {si.name} not voided",
			message=frappe.get_traceback())


def _fail(doc, plan, today, message: str) -> None:
	"""The renewal was not paid. An Active membership goes Past Due until its period end + the plan's
	grace days (retried daily meanwhile), or Suspended at once when the plan gives no grace (or the
	grace is already over). Past Due and Suspended stay as they are: the job suspends when the
	grace runs out."""
	if doc.status == ACTIVE:
		days = cint(plan.grace_days) if cint(plan.grace_enabled) else 0
		grace = getdate(add_days(doc.current_period_end, days)) if days > 0 else None
		if grace and grace >= today:
			doc.status, doc.grace_until = PAST_DUE, grace
			message += " " + _("Past due: tried again daily until {0}.").format(_fmt(grace))
		else:
			doc.status, doc.grace_until = SUSPENDED, None
			message += " " + _("Suspended.")
	doc.last_charge_status = message[:1000]


def _check_overdue(doc, today) -> None:
	if doc.status not in (ACTIVE, PAST_DUE, SUSPENDED) or cint(doc.cancel_at_period_end) \
			or not doc.current_period_end or getdate(doc.current_period_end) >= today:
		frappe.throw(_("{0} has no renewal due: it is {1}, paid until {2}.").format(doc.name, _(doc.status),
			_fmt(doc.current_period_end) if doc.current_period_end else "—"))


@frappe.whitelist(methods=["POST"])
def retry_renewal(membership: str) -> dict:
	"""Charge a membership's saved card now for its overdue renewal (Past Due, Suspended, or Active
	past its period end). Paid: Active again, the new period starting at the original due date. A
	card charge still waiting for the gateway's answer is looked up, never sent again.

	Args:
	    membership: Membership name.

	Returns:
	    {outcome: renewed / declined / unknown / not_started, message: what happened, row: the
	    membership's panel row}.
	"""
	frappe.only_for(CUSTOMER_MANAGER_ROLES)
	doc = _locked(membership)
	today = getdate(nowdate())
	_check_overdue(doc, today)
	plan = frappe.get_doc(PLAN, doc.plan)
	start, _end = next_period(doc, plan.billing_period, today)
	row = _open_renewal(doc, start)
	if not (row and _charge_waiting(row.sales_invoice)) and not _chargeable(doc):
		frappe.throw(_("{0} has no card that can be charged for renewals. Change the card, or take the payment "
			"at the counter (Take payment on the customer's Memberships panel).").format(
			escape_html(doc.customer_name or doc.customer)), title=_("No card"))
	outcome = _attempt(doc, plan, today, manual=True)
	doc.reload()
	return {"outcome": outcome, "message": doc.last_charge_status, "row": _row(doc)}


@frappe.whitelist(methods=["POST"])
def invoice_renewal(membership: str) -> dict:
	"""The overdue renewal's invoice (the open one, else a new one for the next period from the
	original due date), for the counter to take payment by any tender. Paid: Active again.

	Args:
	    membership: Membership name (Past Due, Suspended, or Active past its period end).

	Returns:
	    {sales_invoice, outstanding, customer, auto_renew: 0 (no card charge from this flow)}.
	"""
	frappe.only_for(COUNTER_ROLES)
	doc = _locked(membership)
	today = getdate(nowdate())
	_check_overdue(doc, today)
	plan = frappe.get_doc(PLAN, doc.plan)
	start, end = next_period(doc, plan.billing_period, today)
	row = _open_renewal(doc, start)
	if row and _charge_waiting(row.sales_invoice):
		frappe.throw(_("A card charge on {0} is waiting for the gateway's answer. A manager looks it up first "
			"(Retry now); taking another payment now could charge the customer twice.").format(row.sales_invoice),
			title=_("Charge not confirmed"))
	if not row:
		si = _make_invoice(doc, plan, start, end=end, posting=today, renewal=True)
		row = doc.append("invoices", {"sales_invoice": si.name, "period_start": start, "period_end": end,
			"status": UNPAID})
		_save(doc)
	return {"sales_invoice": row.sales_invoice, "customer": doc.customer, "auto_renew": 0,
		"outstanding": flt(frappe.db.get_value("Sales Invoice", row.sales_invoice, "outstanding_amount"))}


def is_member(customer: str, plan: str | None = None) -> bool:
	"""Whether *customer* has a membership's benefits today (of *plan*, or of any plan): Active, or
	Past Due while its grace lasts. Pending, Suspended, Cancelled and Expired do not count."""
	if not customer:
		return False
	filters = {"customer": customer, "status": ("in", (ACTIVE, PAST_DUE))}
	if plan:
		filters["plan"] = plan
	today = getdate(nowdate())
	return any(r.status == ACTIVE or (r.grace_until and getdate(r.grace_until) >= today)
		for r in frappe.get_all(DOCTYPE, filters=filters, fields=["status", "grace_until"]))


@frappe.whitelist()
def needs_attention() -> dict:
	"""Memberships a manager should look at: Past Due, Suspended, a renewal charge the gateway never
	confirmed, a renewal the job could not charge, and sign-ups still unpaid after a few days.

	Returns:
	    {renewals_on: whether the daily renewal job runs, rows: [{membership, customer,
	    customer_name, plan, status, reason, detail, current_period_end, sales_invoice, outstanding,
	    can_retry, can_cancel, can_drop}]}.
	"""
	frappe.only_for(CUSTOMER_MANAGER_ROLES)
	today = getdate(nowdate())
	fields = ["name", "status", "current_period_end", "last_charge_attempt"]
	names = [m.name for m in frappe.get_all(DOCTYPE, filters={"status": ("in", (PAST_DUE, SUSPENDED))},
		fields=fields, order_by="current_period_end asc")]
	# Active past its period end: not charged, not confirmed, or the renewal job is switched off.
	# A charge still waiting for the gateway is listed whatever auto-renew says now.
	names += [m.name for m in frappe.get_all(DOCTYPE, filters={"status": ACTIVE, "current_period_end": ("<", today)},
		fields=["name", "auto_renew", "cancel_at_period_end"], order_by="current_period_end asc")
		if (cint(m.auto_renew) and not cint(m.cancel_at_period_end)) or _waiting_renewal(frappe.get_doc(DOCTYPE, m.name))]
	stale = add_to_date(now_datetime(), days=-STALE_PENDING_DAYS)
	names += frappe.get_all(DOCTYPE, filters={"status": PENDING, "creation": ("<", stale)}, pluck="name",
		order_by="creation asc")
	rows = []
	for name in names:
		doc = frappe.get_doc(DOCTYPE, name)
		unpaid = _unpaid_row(doc)
		if doc.status == PENDING and not unpaid:
			continue
		waiting = bool(unpaid) and _charge_waiting(unpaid.sales_invoice)
		if waiting:
			reason = _("Card charge not confirmed")
		elif doc.status == PENDING:
			reason = _("Sign-up not paid since {0}").format(_fmt(doc.creation))
		elif doc.status == PAST_DUE:
			reason = _("Past due: retried daily until {0}").format(_fmt(doc.grace_until)) if doc.grace_until \
				else _("Past due")
		elif doc.status == SUSPENDED:
			reason = _("Suspended")
		elif not doc.last_charge_attempt or getdate(doc.last_charge_attempt) <= getdate(doc.current_period_end):
			reason = _("Renewal not tried yet")
		else:
			reason = _("Renewal not charged")
		rows.append({"membership": doc.name, "customer": doc.customer, "customer_name": doc.customer_name,
			"plan": doc.plan, "status": doc.status, "reason": reason, "detail": doc.last_charge_status or "",
			"current_period_end": doc.current_period_end,
			"sales_invoice": unpaid.sales_invoice if unpaid else None,
			"outstanding": flt(frappe.db.get_value("Sales Invoice", unpaid.sales_invoice, "outstanding_amount"))
				if unpaid else 0,
			"can_retry": doc.status != PENDING and not cint(doc.cancel_at_period_end) and (waiting or _chargeable(doc)),
			"can_cancel": doc.status in (ACTIVE, PAST_DUE, SUSPENDED) and not waiting,
			"can_drop": doc.status == PENDING and not waiting})
	return {"renewals_on": renewals_enabled(), "rows": rows}


def send_renewal_reminders(today=None) -> int:
	"""Email the customer of each auto-renewing Yearly membership once, Membership Settings → yearly
	reminder days before its renewal charge (0 = no reminders). Monthly renewals send none. Returns
	how many went out."""
	days = _reminder_days()
	if days <= 0:
		return 0
	today = getdate(today or nowdate())
	from ffl_core.store_identity import company_brand

	sent = 0
	for m in frappe.db.sql(f"""select m.name, m.customer, m.customer_name, m.plan, m.current_period_end,
			m.saved_card, m.company, m.renewal_reminder_for, p.plan_name, p.price
		from `tab{DOCTYPE}` m join `tab{PLAN}` p on p.name = m.plan
		where m.status = %s and m.auto_renew = 1 and m.cancel_at_period_end = 0 and p.billing_period = %s
			and m.current_period_end >= %s and m.current_period_end < %s""",
			(ACTIVE, YEARLY, today, add_days(today, days)), as_dict=True):
		if m.renewal_reminder_for and getdate(m.renewal_reminder_for) == getdate(m.current_period_end):
			continue
		email = (frappe.db.get_value("Customer", m.customer, "email_id") or "").strip()
		if not email:
			continue
		try:
			cur = frappe.get_cached_value("Company", m.company, "default_currency") if m.company else None
			store = company_brand(m.company).get("name") or ""
			when = _fmt(add_days(m.current_period_end, 1))
			plan = escape_html(m.plan_name or m.plan)
			chargeable = _chargeable(frappe._dict(saved_card=m.saved_card, customer=m.customer))
			card = escape_html(_card_summary(m.saved_card)) or _("the card on file")
			frappe.sendmail(recipients=[email], subject=_("Your {0} membership renews on {1}").format(
				m.plan_name or m.plan, when),
				message="<p>{0}</p><p>{1}</p><p>{2}</p>".format(
					_("Hello {0},").format(escape_html(m.customer_name or m.customer)),
					_("Your {0} membership at {1} renews on {2}. That day {3} is charged to {4}.").format(
						plan, escape_html(store), when, fmt_money(flt(m.price), currency=cur), card) if chargeable
					else _("Your {0} membership at {1} renews on {2} ({3}), but there is no card on file we can "
						"charge. Please visit or call the store before then to keep your membership.").format(
						plan, escape_html(store), when, fmt_money(flt(m.price), currency=cur)),
					_("To change the card or stop the renewal, contact the store before then.")),
				reference_doctype=DOCTYPE, reference_name=m.name, now=False)
			frappe.db.set_value(DOCTYPE, m.name, "renewal_reminder_for", m.current_period_end, update_modified=False)
			commit()
			sent += 1
		except Exception:
			frappe.log_error(title=f"Membership {m.name}: renewal reminder not sent", message=frappe.get_traceback())
	return sent


# ------------------------------------------------------------------ daily


def close_ended_memberships() -> int:
	"""Daily: an Active membership that does not renew and whose period has ended turns
	Cancelled (a manager cancelled it at period end) or Expired. Renewing ones are left to the
	renewal job."""
	today = getdate(nowdate())
	done = 0
	for row in frappe.get_all(DOCTYPE, filters={"status": ACTIVE, "auto_renew": 0,
			"current_period_end": ("<", today)}, fields=["name", "cancel_at_period_end"]):
		frappe.db.set_value(DOCTYPE, row.name, "status", CANCELLED if cint(row.cancel_at_period_end) else EXPIRED)
		done += 1
	if done:
		commit()
	return done


def agreement_context(doc) -> dict:
	"""Placeholders for a plan's terms (agreements.document_context, through the controller)."""
	from ffl_core.store_identity import company_brand

	plan = frappe.db.get_value(PLAN, doc.plan, ["plan_name", "price", "billing_period"], as_dict=True) \
		or frappe._dict()
	cur = frappe.get_cached_value("Company", doc.company, "default_currency") if doc.company else None
	start = getdate(doc.current_period_start or nowdate())
	end = getdate(doc.current_period_end) if doc.current_period_end else period_end(start, plan.billing_period)
	return {"plan": plan.plan_name or doc.plan, "price": fmt_money(flt(plan.price), currency=cur),
		"period": _(plan.billing_period or ""),
		"next_charge_date": frappe.format_value(add_days(end, 1), {"fieldtype": "Date"}) if cint(doc.auto_renew) else "",
		"store_name": company_brand(doc.company).get("name") or ""}


# ------------------------------------------------------------------ Memberships page


def _monthly(price, billing_period: str) -> float:
	return flt(price) / 12 if billing_period == YEARLY else flt(price)


@frappe.whitelist()
def page_overview() -> dict:
	"""Everything the Memberships page shows above the member list, in one round trip.

	Returns:
	    {plans: [{name, active, billing_period, price, registration_fee, auto_renew_default,
	    grace_enabled, grace_days, terms_template, description, members}], totals: {active,
	    monthly, renewing_soon, attention}, attention: the Needs attention answer (managers only,
	    else null), can_manage: may act on memberships, can_edit_plans: may create and change plans}.
	    ``monthly`` is what the Active and Past Due members bill per month (a yearly price counts a
	    twelfth); ``renewing_soon`` counts auto-renewing Active members charged in the next 7 days.
	"""
	frappe.only_for(READ_ROLES)
	plans = frappe.get_all(PLAN, fields=["name", "active", "billing_period", "price", "registration_fee",
		"auto_renew_default", "grace_enabled", "grace_days", "terms_template", "description"],
		order_by="active desc, plan_name asc")
	today = getdate(nowdate())
	live = frappe.get_all(DOCTYPE, filters={"status": ("in", (ACTIVE, PAST_DUE))}, fields=["plan", "status",
		"auto_renew", "cancel_at_period_end", "current_period_end"])
	by_plan = {p.name: p for p in plans}
	for p in plans:
		p.members = 0
	monthly = renewing = 0
	for m in live:
		p = by_plan.get(m.plan)
		if p:
			p.members += 1
			monthly += _monthly(p.price, p.billing_period)
		# Charged the day after the period ends: within the next 7 days.
		if m.status == ACTIVE and cint(m.auto_renew) and not cint(m.cancel_at_period_end) and m.current_period_end \
				and add_days(today, -1) <= getdate(m.current_period_end) <= add_days(today, 6):
			renewing += 1
	can_manage = bool(set(frappe.get_roles()) & set(CUSTOMER_MANAGER_ROLES))
	attention = needs_attention() if can_manage else None
	return {"plans": plans, "attention": attention, "can_manage": can_manage,
		"can_edit_plans": bool(frappe.has_permission(PLAN, "create")),
		"totals": {"active": sum(1 for m in live if m.status == ACTIVE), "monthly": flt(monthly, 2),
			"renewing_soon": renewing, "attention": len(attention["rows"]) if attention else None}}


PAGE_SIZE = 50


@frappe.whitelist()
def page_members(plan: str | None = None, status: str | None = None, search: str | None = None,
		start: int = 0) -> dict:
	"""One page of the Memberships page's member list, by customer name.

	Args:
	    plan: Membership Plan name; blank = every plan.
	    status: Membership status; blank = every status.
	    search: Part of the customer's name, id or mobile number; blank = everyone.
	    start: Rows to skip (paging, 50 a page).

	Returns:
	    {rows: [{name, customer, customer_name, plan, status, next_charge_date, ends_on, auto_renew,
	    card_summary}], more: whether another page follows}.
	"""
	frappe.only_for(READ_ROLES)
	filters = {}
	if plan:
		filters["plan"] = plan
	if status:
		filters["status"] = status
	search = (search or "").strip()
	if search:
		like = f"%{search}%"
		customers = frappe.get_all("Customer", or_filters={"name": ("like", like), "customer_name": ("like", like),
			"mobile_no": ("like", like)}, pluck="name", limit=500)  # lazy: 500 matching customers, paginate if a store outgrows it
		if not customers:
			return {"rows": [], "more": False}
		filters["customer"] = ("in", customers)
	found = frappe.get_all(DOCTYPE, filters=filters, fields=["name", "customer", "customer_name", "plan", "status",
		"auto_renew", "cancel_at_period_end", "current_period_end", "saved_card"],
		order_by="customer_name asc, name asc", limit_start=cint(start), limit_page_length=PAGE_SIZE + 1)
	more = len(found) > PAGE_SIZE
	found = found[:PAGE_SIZE]
	cards = {}
	card_names = sorted({m.saved_card for m in found if m.saved_card})
	if card_names and frappe.db.exists("DocType", "Saved Card"):
		cards = dict(frappe.get_all("Saved Card", filters={"name": ("in", card_names)},
			fields=["name", "card_summary"], as_list=True))
	today = getdate(nowdate())
	rows = []
	for m in found:
		end = getdate(m.current_period_end) if m.current_period_end else None
		renewing = cint(m.auto_renew) and m.status in (ACTIVE, PAST_DUE) and not cint(m.cancel_at_period_end)
		rows.append({"name": m.name, "customer": m.customer, "customer_name": m.customer_name, "plan": m.plan,
			"status": m.status, "auto_renew": cint(m.auto_renew),
			# An overdue renewal has no next charge to show (Needs attention lists it).
			"next_charge_date": add_days(end, 1) if renewing and end and end >= today else None,
			"ends_on": end if cint(m.cancel_at_period_end) and m.status in LIVE else None,
			"card_summary": cards.get(m.saved_card) or ""})
	return {"rows": rows, "more": more}
