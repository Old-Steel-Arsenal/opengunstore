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

Renewal charging (Run 2's daily job) and member benefits are not here yet; the fields they need
(``auto_renew``, ``saved_card``, ``grace_until``, the invoices table) are.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, add_months, cint, escape_html, flt, fmt_money, getdate, nowdate

from ffl_core.roles import COUNTER_ROLES, CUSTOMER_MANAGER_ROLES
from ffl_core.utils import commit

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


def _make_invoice(doc, plan, start):
	"""The submitted Sales Invoice for one period from *start*: the plan line, plus the
	registration line on the customer's first membership of the plan. Both lines carry the
	service dates and the membership accounts, so ERPNext defers them day by day."""
	from ffl_core.api.manual_order import _apply_default_tax_template
	from ffl_core.setup import membership as setup

	company = doc.company or _default_company()
	deferred, income = setup.accounts(company)
	if not (deferred and income):
		frappe.throw(_("{0} has no chart of accounts for memberships yet. Run a migrate, then try again.").format(
			escape_html(company)))
	end = period_end(start, plan.billing_period)
	lines = [(plan.item, flt(plan.price))]
	if plan.registration_item and flt(plan.registration_fee) > 0 and _first_of_plan(doc):
		lines.append((plan.registration_item, flt(plan.registration_fee)))
	si = frappe.new_doc("Sales Invoice")
	si.update({"customer": doc.customer, "company": company, "posting_date": start, "set_posting_time": 1,
		"due_date": start})
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
	si.due_date = start
	si.flags.ignore_permissions = True
	si.insert()
	si.submit()
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
	the invoice owes nothing."""
	invoices = [r.reference_name for r in doc.get("references") or []
		if r.reference_doctype == "Sales Invoice" and r.reference_name]
	if not invoices:
		return
	for name in set(frappe.get_all(INVOICE_ROW, filters={"parenttype": DOCTYPE,
			"sales_invoice": ("in", invoices), "status": UNPAID}, pluck="parent")):
		settle(name)


def settle(membership: str) -> bool:
	"""Activate a Pending membership whose unpaid invoice is now paid. Idempotent; True when
	this call activated it."""
	if not frappe.db.get_value(DOCTYPE, membership, "name", for_update=True):
		return False
	doc = frappe.get_doc(DOCTYPE, membership)
	if doc.status != PENDING:
		return False
	for row in doc.get("invoices") or []:
		if row.status != UNPAID:
			continue
		si = frappe.db.get_value("Sales Invoice", row.sales_invoice, ["docstatus", "outstanding_amount"], as_dict=True)
		if not si or si.docstatus != 1 or flt(si.outstanding_amount) > 0.01:
			continue
		row.status = PAID
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
		doc.flags.via_membership = True
		doc.save(ignore_permissions=True)
		return True
	return False


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
	unpaid = _unpaid_row(doc) if doc.status == PENDING else None
	return {"name": doc.name, "plan": doc.plan, "status": doc.status, "price": flt(plan.price),
		"billing_period": plan.billing_period, "start_date": doc.start_date,
		"current_period_start": doc.current_period_start, "current_period_end": doc.current_period_end,
		"next_charge_date": add_days(doc.current_period_end, 1) if renewing and doc.current_period_end else None,
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
	A Pending membership whose invoice has since been paid another way is activated here.

	Args:
	    customer: Customer name (document id).

	Returns:
	    {plans: [{name, price, billing_period, registration_fee, auto_renew_default,
	    terms_template}], memberships: [panel rows, live first], cards: the customer's active
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
		if doc.status == PENDING and settle(name):
			doc.reload()
		rows.append(_row(doc))
	rows.sort(key=lambda r: r["status"] not in LIVE)
	cards = []
	if frappe.db.exists("DocType", "Saved Card"):
		cards = frappe.get_all("Saved Card", filters={"customer": customer, "status": "Active", "usage": RECURRING},
			fields=["name", "card_summary"], order_by="is_default desc, creation desc")
	return {"plans": plans, "memberships": rows, "cards": cards,
		"can_cancel": bool(set(frappe.get_roles()) & set(CUSTOMER_MANAGER_ROLES))}


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
			touched = flt(si.outstanding_amount) < flt(si.rounded_total or si.grand_total) - 0.005
			if not touched and frappe.db.exists("DocType", "Card Terminal Payment"):
				touched = bool(frappe.db.exists("Card Terminal Payment", {"sales_invoice": si.name,
					"status": ("in", ("IN_PROGRESS", "CHARGED"))}))
			if touched:
				frappe.throw(_("{0} has a payment or a card charge on it. Finish taking the payment, or have a "
					"manager refund it, instead of dropping the membership.").format(si.name))
			si.flags.via_membership = True
			si.flags.ignore_permissions = True
			si.cancel()
		row.status = CANCELLED
	doc.update({"status": CANCELLED, "auto_renew": 0, "cancelled_on": getdate(nowdate()),
		"cancelled_by": frappe.session.user, "cancel_reason": reason[:1000]})
	doc.flags.via_membership = True
	doc.save(ignore_permissions=True)
	return _row(doc)


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
