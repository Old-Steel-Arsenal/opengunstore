"""Sales Summary — one period's sales, discounts, returns, tax and payments for a manager.

A management summary, not a tax worksheet: returns and month-end close read the GL.

The invoices are exactly the Sales Report's (`sales_report._fetch_headers`: submitted POS Invoices
without counter pickup tickets, plus Sales Invoices that are not POS consolidations), narrowed to
one company, and Net Sales is the Sales Report's own expression (`_order_facts` / `_net_sales`):
gross of trade-in credit, without gunsmith deposits. Nothing here re-derives it.

Views:

* **Summary** — one top-to-bottom table: the sales lines, the other amounts on the tickets, the
  payments on these tickets, and two checks that must come to 0.00:
  E1  Σ ticket totals = Net sales − Trade-in allowance + (Deposits taken − applied) + Shipping
      + every tax row + Rounding
  E2  Σ ticket totals = the payment rows (payouts negative) + Write-off + Unpaid
* **Category** — the top-level item group of each line's Item (as the Sales Report reads it),
  plus "Memberships (billed)" for deferred-revenue lines.
* **Register** — the ticket's POS Profile; **Employee** — who opened the ticket. Grouping only:
  each view's Total row equals the Summary.

Gross sales is built, not read: Net sales + Recorded discounts + Returns. A line's price-list
rate is not a reliable list price everywhere (a typed Create Order price and a per-gun price are
the rate itself), so only discounts the documents record are counted:
  Recorded discounts  max(0, price-list rate − rate) × qty on lines with a price-list rate, plus
                      each line's share of a whole-ticket discount (`distributed_discount_amount`);
                      on a web order that share is a refund (the Woo refund flow books it as a
                      discount) and goes to Returns. Return documents and locked lines are left out.
  Returns             minus the Net Sales of return documents and credit notes, plus web refunds.
"""
from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt, get_fullname

from ffl_core.compliance.negative_trade_in import invoice_total
from ffl_core.ffl_core.report.sales_report import sales_report as sr
from ffl_core.setup.gunsmith import DEPOSIT_ITEM
from ffl_core.setup.locked_lines import locked_items

VIEWS = ("Summary", "Category", "Register", "Employee")
UNATTRIBUTED = "Unattributed"
MEMBERSHIPS = "Memberships (billed)"
UNCATEGORIZED = "Uncategorized"
# Channels whose whole-ticket discount is a refund rather than a price cut. The Woo refund flow
# (ffl_woo_sync refunds.apply_refund_discount) books what Woo refunded as a Net Total discount on
# the amended invoice. A GunBroker partial refund is a person's cancel + amend with a lower price,
# so it stays a recorded discount until the store says otherwise.
# lazy: by channel, not by document; add "GunBroker" here if the store books its partial refunds as
# refunds.
REFUND_DISCOUNT_CHANNELS = frozenset({"Web"})
# Legacy root-level leaves that are firearms (setup.item_groups.LEGACY_LEAVES).
_LEGACY_TOP = {"New Firearm": "Firearm", "Used Firearm": "Firearm", "Long Gun": "Firearm"}
# Users that are not a person at the counter.
# lazy: Administrator / Guest and non-desk users only; an integration that runs as its own desk
# user shows under that user's name. Upgrade: a per-site list of service users.
_SYSTEM_USERS = frozenset({"Administrator", "Guest"})
CREDIT_NOTES_APPLIED = "Credit notes applied"
TRADE_IN_PAYOUTS = "Trade-in payouts"
WRITE_OFF = "Write-off"
UNPAID = "Unpaid / on account"


def execute(filters=None):
	filters = _validate(filters)
	ctx = _collect(filters)
	view = filters.view
	if view == "Category":
		columns, data = _category_view(ctx)
	elif view in ("Register", "Employee"):
		columns, data = _group_view(ctx, "register" if view == "Register" else "employee")
	else:
		columns, data = _summary_view(ctx)
	return columns, data, _message(ctx, view)


def _validate(filters) -> frappe._dict:
	"""Every filter is checked here, server-side: dates (the Sales Report's own check), the view,
	and read permission on the company."""
	filters = frappe._dict(filters or {})
	sr._validate(filters)
	filters.view = (str(filters.get("view") or "Summary")).strip()
	if filters.view not in VIEWS:
		frappe.throw(_("Unknown view {0}. Use one of: {1}.").format(filters.view, ", ".join(VIEWS)))
	company = filters.get("company") or frappe.defaults.get_user_default("Company") \
		or frappe.db.get_single_value("Global Defaults", "default_company")
	if not company or not frappe.db.exists("Company", company):
		frappe.throw(_("Choose a company."))
	if not frappe.has_permission("Company", "read", doc=company):
		frappe.throw(_("You cannot read the sales of {0}.").format(company), frappe.PermissionError)
	filters.company = company
	return filters


# ------------------------------------------------------------------ data

def _collect(filters) -> frappe._dict:
	"""Every invoice of the period with its figures, one dict each, plus the period totals."""
	headers = [h for h in sr._fetch_headers(filters, extended=True) if h.get("company") == filters.company]
	lines = sr._fetch_lines(headers, extended=True)
	_add_line_facts(lines)
	item_meta = sr._fetch_item_meta(lines)
	taxes = sr._fetch_taxes(headers)
	acct_class = sr._fetch_account_classes(taxes)
	extra = _header_facts(headers)
	payments = _pos_payments(headers, extra)
	settlements = _ledger_settlements(filters.company, [h["name"] for h in headers if h["voucher_type"] == sr._SI])
	exempt_customers = _exempt_customers(headers)
	oos_templates = _out_of_state_templates(filters.company)
	from ffl_core.setup.tax import sales_tax_and_excise_accounts
	excise_accounts = sales_tax_and_excise_accounts(filters.company)[1]
	top_group = _top_groups()
	locked = set(locked_items())

	docs = []
	for h in sorted(headers, key=lambda x: (str(x.get("posting_date") or ""), x.get("name") or "")):
		name = h["name"]
		ls = lines.get(name, [])
		x = extra.get(name, {})
		f = sr._order_facts(h, ls, item_meta)
		net = sr._net_sales(h, f)
		applied, payout = sr._trade_in_settlement(h, ls, f["trade_in_net"], taxes.get(name))
		bd = sr._tax_breakdown(taxes.get(name, []), [], acct_class)
		channel = sr._channel(h.get("voucher_type"), h.get("woo_online_order"), h.get("gunbroker_order"))
		is_return = bool(cint(h.get("is_return")))
		rate = flt(x.get("conversion_rate")) or 1.0
		d = frappe._dict(
			name=name, voucher_type=h["voucher_type"], channel=channel, is_return=is_return,
			net=net, trade_in_allowance=-f["trade_in_net"], payout=payout,
			total=invoice_total(h), grand_total=flt(h.get("grand_total")),
			shipping=bd["shipping_total"], sales_tax=bd["sales_tax_charged"] + bd["freight_tax_total"],
			freight_tax=bd["freight_tax_total"], excise=bd["excise_total"],
			marketplace_tax=bd["marketplace_tax_total"], marketplace_fee=bd["marketplace_fee_total"],
			unclassified=sum(u["amount"] for u in bd["unclassified"]),
			deposit_taken=0.0, deposit_applied=0.0, discounts=0.0, web_refund=0.0,
			register=x.get("pos_profile") or UNATTRIBUTED, owner=x.get("owner"),
			categories=defaultdict(lambda: [0.0, 0.0, 0.0, 0.0]),  # qty, discounts, returns, net
			membership=0.0, payments=defaultdict(float),
		)
		for line in ls:
			code, line_net = line.get("item_code"), flt(line.get("base_net_amount"))
			if code == DEPOSIT_ITEM:
				if line_net > 0:
					d.deposit_taken += line_net
				else:
					d.deposit_applied -= line_net
			if code in locked:
				continue
			cat = MEMBERSHIPS if cint(line.get("enable_deferred_revenue")) else \
				top_group(item_meta.get(code, {}).get("item_group"))
			row = d.categories[cat]
			row[0] += flt(line.get("qty"))
			row[3] += line_net
			if cat == MEMBERSHIPS:
				d.membership += line_net
			if is_return:
				row[2] -= line_net
				continue
			plr, sold = flt(line.get("base_price_list_rate")), flt(line.get("base_rate"))
			line_disc = max(0.0, plr - sold) * flt(line.get("qty")) if plr > 0 else 0.0
			shared = flt(line.get("distributed_discount_amount")) * rate
			if channel in REFUND_DISCOUNT_CHANNELS:
				d.web_refund += shared
				row[2] += shared
				shared = 0.0
			d.discounts += line_disc + shared
			row[1] += line_disc + shared
		d.returns = -net if is_return else d.web_refund
		d.gross = net + d.discounts + d.returns
		_non_taxable(d, h, ls, x, bd, taxes.get(name, []), acct_class, exempt_customers, oos_templates,
			excise_accounts, locked)
		_settle(d, x, payments.get(name, []), settlements.get(name, []))
		docs.append(d)
	return frappe._dict(filters=filters, docs=docs, totals=_totals(docs, filters))


def _add_line_facts(lines_by_invoice) -> None:
	"""The pricing columns this report needs beyond the Sales Report's line fetch, merged by row name."""
	rows = {l["name"]: l for ls in lines_by_invoice.values() for l in ls}
	if not rows:
		return
	fields = ["name", "base_rate", "base_price_list_rate", "distributed_discount_amount", "enable_deferred_revenue"]
	for child in ("POS Invoice Item", "Sales Invoice Item"):
		for r in frappe.get_all(child, filters={"name": ["in", list(rows)]}, fields=fields):
			rows[r["name"]].update(r)


def _header_facts(headers) -> dict:
	"""{invoice: facts} the Sales Report's header fetch does not carry."""
	out = {}
	base = ["name", "owner", "pos_profile", "conversion_rate", "base_change_amount", "base_write_off_amount",
		"outstanding_amount", "taxes_and_charges"]
	for dt, extra in ((sr._POS, []), (sr._SI, ["is_pos"])):
		names = [h["name"] for h in headers if h["voucher_type"] == dt]
		if not names:
			continue
		fields = base + extra + [f for f in ("ffl_le_excise_exempt",) if frappe.db.has_column(dt, f)]
		for r in frappe.get_all(dt, filters={"name": ["in", names]}, fields=fields):
			r["is_pos"] = 1 if dt == sr._POS else cint(r.get("is_pos"))
			out[r["name"]] = r
	return out


def _pos_payments(headers, extra) -> dict:
	"""{invoice: [payment rows]} of register tickets and POS-mode Sales Invoices."""
	out = defaultdict(list)
	for dt in (sr._POS, sr._SI):
		names = [h["name"] for h in headers if h["voucher_type"] == dt and extra.get(h["name"], {}).get("is_pos")]
		if not names:
			continue
		for r in frappe.get_all("Sales Invoice Payment", filters={"parenttype": dt, "parent": ["in", names]},
				fields=["parent", "mode_of_payment", "base_amount", "type", "idx"], order_by="idx asc"):
			out[r["parent"]].append(r)
	return out


def _ledger_settlements(company, invoices) -> dict:
	"""{invoice: [(label, amount)]}: what settled each Sales Invoice in the payment ledger, by source.

	Another voucher booked against the invoice (a Payment Entry, a Journal Entry, a credit note
	applied to it) settles −amount; the invoice itself booked against another one (a credit note
	applied to its original) settles +amount. Its own postings (the total, POS payments, write-off)
	are read from the invoice instead."""
	out = defaultdict(list)
	if not invoices:
		return out
	fields = ["voucher_type", "voucher_no", "against_voucher_type", "against_voucher_no", "amount"]
	base = {"company": company, "delinked": 0}
	against = frappe.get_all("Payment Ledger Entry", fields=fields, filters={**base,
		"against_voucher_type": sr._SI, "against_voucher_no": ["in", invoices]})
	own = frappe.get_all("Payment Ledger Entry", fields=fields, filters={**base,
		"voucher_type": sr._SI, "voucher_no": ["in", invoices]})
	pe = {r.voucher_no for r in against if r.voucher_type == "Payment Entry"}
	je = {r.voucher_no for r in against if r.voucher_type == "Journal Entry"}
	modes = {}
	for dt, names in (("Payment Entry", pe), ("Journal Entry", je)):
		if names:
			modes.update({(dt, r.name): r.mode_of_payment for r in frappe.get_all(
				dt, filters={"name": ["in", list(names)]}, fields=["name", "mode_of_payment"])})
	for r in against:
		if r.voucher_type == r.against_voucher_type and r.voucher_no == r.against_voucher_no:
			continue
		if r.voucher_type == sr._SI:
			label = CREDIT_NOTES_APPLIED
		else:
			label = modes.get((r.voucher_type, r.voucher_no)) or _(r.voucher_type)
		out[r.against_voucher_no].append((label, -flt(r.amount)))
	for r in own:
		if r.against_voucher_type == sr._SI and r.against_voucher_no == r.voucher_no:
			continue
		out[r.voucher_no].append((CREDIT_NOTES_APPLIED, flt(r.amount)))
	return out


def _settle(d, x, pos_rows, ledger) -> None:
	"""The ticket's payments by method (cash net of change), payout, write-off and what is unpaid."""
	change = flt(x.get("base_change_amount"))
	if pos_rows and change:
		cash = next((r for r in pos_rows if r.get("type") == "Cash"), pos_rows[0])
		cash["base_amount"] = flt(cash["base_amount"]) - change
	for r in pos_rows:
		if flt(r["base_amount"]):
			d.payments[r["mode_of_payment"]] += flt(r["base_amount"])
	for label, amount in ledger:
		d.payments[label] += amount
	if d.payout:
		d.payments[TRADE_IN_PAYOUTS] -= d.payout
	d.payments[WRITE_OFF] += flt(x.get("base_write_off_amount"))
	d.payments[UNPAID] += flt(x.get("outstanding_amount"))


def _exempt_customers(headers) -> set:
	customers = {h.get("customer") for h in headers if h.get("customer")}
	if not customers or not frappe.db.has_column("Customer", "is_tax_exempt"):
		return set()
	return set(frappe.get_all("Customer", filters={"name": ["in", list(customers)], "is_tax_exempt": 1},
		pluck="name"))


def _out_of_state_templates(company) -> set:
	from ffl_core.compliance.interstate_tax import OUT_OF_STATE_TEMPLATE_TITLE

	return set(frappe.get_all("Sales Taxes and Charges Template",
		filters={"company": company, "title": OUT_OF_STATE_TEMPLATE_TITLE}, pluck="name"))


def _non_taxable(d, h, lines, x, bd, tax_rows, acct_class, exempt_customers, oos, excise_accounts, locked) -> None:
	"""Net sales that carried no sales tax, by the first reason that applies to the whole ticket, else
	the ticket's lines whose Item Tax Template exempts them; and the peace-officer excise exemption."""
	d.non_taxable = {}
	reason = None
	if d.channel == "GunBroker":
		reason = _("Marketplace (GunBroker)")
	elif x.get("taxes_and_charges") in oos:
		reason = _("Out of state")
	elif cint(h.get("exempt_from_sales_tax")) or h.get("customer") in exempt_customers:
		reason = _("Tax-exempt customer")
	accounts = [t.get("account_head") for t in tax_rows
		if acct_class.get(t.get("account_head")) == sr._SALES_TAX and t.get("charge_type") == "On Net Total"]
	if not reason and not accounts and d.net:
		reason = _("No sales tax on the ticket")
	if reason:
		d.non_taxable[reason] = d.net
	else:
		exempt = sum(flt(l.get("base_net_amount")) for l in lines if l.get("item_code") not in locked
			and not sr._line_taxable(l, h.get("company"), accounts))
		if exempt:
			d.non_taxable[_("Non-taxable items")] = exempt
	d.le_exempt = 0.0
	if cint(x.get("ffl_le_excise_exempt")) and excise_accounts:
		d.le_exempt = sum(flt(l.get("base_net_amount")) for l in lines if l.get("item_code") not in locked
			and sr._line_taxable(l, h.get("company"), excise_accounts))


def _top_groups():
	"""item_group → its ancestor directly under the root (legacy root-level firearm leaves → Firearm)."""
	parent = dict(frappe.get_all("Item Group", fields=["name", "parent_item_group"], as_list=True))
	cache = {}

	def top(group):
		if not group or group not in parent or not parent.get(group):
			return UNCATEGORIZED
		if group not in cache:
			node = group
			while parent.get(parent.get(node)):
				node = parent[node]
			cache[group] = _LEGACY_TOP.get(node, node)
		return cache[group]

	return top


def _membership_recognized(filters) -> float:
	"""Membership Revenue booked in the period (credit − debit): the deferred income actually recognized."""
	from ffl_core.setup.membership import INCOME_ACCOUNT_TITLE, account

	acct = account(filters.company, INCOME_ACCOUNT_TITLE)
	if not acct:
		return 0.0
	return flt(frappe.db.sql(
		"""SELECT IFNULL(SUM(credit - debit), 0) FROM `tabGL Entry` WHERE company = %s AND account = %s
		AND is_cancelled = 0 AND posting_date BETWEEN %s AND %s""",
		(filters.company, acct, filters.from_date, filters.to_date))[0][0], 2)


def _totals(docs, filters) -> frappe._dict:
	t = frappe._dict()
	for key in ("net", "gross", "discounts", "returns", "web_refund", "trade_in_allowance", "deposit_taken",
			"deposit_applied", "shipping", "sales_tax", "freight_tax", "excise", "marketplace_tax",
			"marketplace_fee", "unclassified", "membership", "total", "le_exempt"):
		t[key] = flt(sum(flt(d[key]) for d in docs), 2)
	t.rounding = flt(sum(d.total - d.grand_total for d in docs), 2)
	t.all_tax = flt(t.sales_tax + t.excise + t.marketplace_tax + t.marketplace_fee + t.unclassified, 2)
	t.payments = defaultdict(float)
	t.payment_counts = defaultdict(int)
	t.non_taxable = defaultdict(float)
	for d in docs:
		for k, v in d.payments.items():
			if flt(v, 2):
				t.payments[k] += v
				t.payment_counts[k] += 1
		for k, v in d.non_taxable.items():
			t.non_taxable[k] += v
	t.e1 = flt(t.total - (t.net - t.trade_in_allowance + t.deposit_taken - t.deposit_applied + t.shipping
		+ t.all_tax + t.rounding), 2)
	t.e2 = flt(t.total - sum(t.payments.values()), 2)
	t.membership_recognized = _membership_recognized(filters)
	return t


# ------------------------------------------------------------------ views

def _summary_columns():
	return [
		{"label": _("Line"), "fieldname": "line", "fieldtype": "Data", "width": 460},
		{"label": _("Amount"), "fieldname": "amount", "fieldtype": "Currency", "width": 140},
		{"label": _("Count"), "fieldname": "count", "fieldtype": "Int", "width": 80},
	]


def _summary_view(ctx):
	t, docs = ctx.totals, ctx.docs
	rows = []

	def head(label):
		rows.append({"line": label, "amount": None, "is_section": 1, "bold": 1})

	def line(label, amount, count=None, indent=0, bold=0, alert=0):
		rows.append({"line": label, "amount": flt(amount, 2), "count": count, "sub": indent,
			"bold": bold, "is_alert": alert})

	def n(pred):
		return sum(1 for d in docs if pred(d))

	head(_("Sales"))
	line(_("Gross sales"), t.gross, n(lambda d: not d.is_return))
	line(_("Recorded discounts"), t.discounts, n(lambda d: flt(d.discounts, 2)))
	line(_("Returns"), t.returns, n(lambda d: flt(d.returns, 2)))
	line(_("Net sales"), t.net, len(docs), bold=1)
	line(_("of which Memberships (billed)"), t.membership, n(lambda d: flt(d.membership, 2)), indent=1)
	line(_("Memberships recognized (GL)"), t.membership_recognized, indent=1)

	head(_("Other amounts on tickets"))
	line(_("Trade-in allowance"), t.trade_in_allowance, n(lambda d: flt(d.trade_in_allowance, 2)))
	line(_("Deposits taken"), t.deposit_taken, n(lambda d: flt(d.deposit_taken, 2)))
	line(_("Deposits applied"), t.deposit_applied, n(lambda d: flt(d.deposit_applied, 2)))
	line(_("Shipping"), t.shipping, n(lambda d: flt(d.shipping, 2)))
	line(_("Sales tax"), t.sales_tax, n(lambda d: flt(d.sales_tax, 2)))
	line(_("of which on shipping"), t.freight_tax, n(lambda d: flt(d.freight_tax, 2)), indent=1)
	line(_("Excise (CFET)"), t.excise, n(lambda d: flt(d.excise, 2)))
	line(_("Tax owed"), t.sales_tax + t.excise, bold=1)
	line(_("Marketplace-collected tax (memo)"), t.marketplace_tax, n(lambda d: flt(d.marketplace_tax, 2)))
	line(_("Marketplace fee collected (memo)"), t.marketplace_fee, n(lambda d: flt(d.marketplace_fee, 2)))
	line(_("Unclassified charges"), t.unclassified, n(lambda d: flt(d.unclassified, 2)), alert=bool(t.unclassified))
	head(_("Non-taxable sales"))
	for reason, amount in sorted(t.non_taxable.items()):
		line(reason, amount, n(lambda d, r=reason: flt(d.non_taxable.get(r), 2)), indent=1)
	line(_("CFET-exempt (LE)"), t.le_exempt, n(lambda d: flt(d.le_exempt, 2)), indent=1)
	line(_("Rounding"), t.rounding)
	line(_("Total of tickets"), t.total, len(docs), bold=1)

	head(_("Payments on these sales"))
	for label, amount in _payment_order(t):
		line(label, amount, t.payment_counts[label], indent=1)
	line(_("Total"), sum(t.payments.values()), bold=1)

	head(_("Checks"))
	line(_("E1: tickets less sales and charges"), t.e1, bold=1, alert=bool(t.e1))
	line(_("E2: tickets less payments"), t.e2, bold=1, alert=bool(t.e2))

	head(_("Notes"))
	for note in _notes():
		rows.append({"line": note, "amount": None, "is_note": 1})
	return _summary_columns(), rows


def _notes():
	return [
		_("Web refunds are recorded on the original sale's date."),
		_("Sale prices, per-gun prices, prices typed on Create Order and Woo coupons are already in the selling price and are not in Recorded discounts."),
		_("Sales tax on web-order shipping is not split out."),
		_("Totals for a period already viewed can change when an invoice is amended or cancelled."),
	]


_FIXED_TAIL = (CREDIT_NOTES_APPLIED, TRADE_IN_PAYOUTS, WRITE_OFF, UNPAID)


def _payment_order(t):
	"""Payment methods (largest first), then the fixed rows, leaving out the ones that are zero."""
	methods = sorted(((k, v) for k, v in t.payments.items() if k not in _FIXED_TAIL and flt(v, 2)),
		key=lambda kv: (-kv[1], kv[0]))
	return methods + [(k, t.payments[k]) for k in _FIXED_TAIL if flt(t.payments.get(k), 2)]


def _category_view(ctx):
	cats = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
	for d in ctx.docs:
		for cat, (qty, disc, ret, net) in d.categories.items():
			c = cats[cat]
			c[0] += qty
			c[1] += disc
			c[2] += ret
			c[3] += net
	rows = []
	tot = [0.0] * 4
	order = sorted((c for c in cats if c != MEMBERSHIPS), key=lambda c: (c == UNCATEGORIZED, c))
	for cat in order + ([MEMBERSHIPS] if MEMBERSHIPS in cats else []):
		qty, disc, ret, net = cats[cat]
		rows.append(_cat_row(cat, qty, disc, ret, net))
		tot = [a + b for a, b in zip(tot, (qty, disc, ret, net))]
	if rows:
		rows.append({**_cat_row(_("Total"), *tot), "bold": 1, "is_total": 1})
	return [
		{"label": _("Category"), "fieldname": "category", "fieldtype": "Data", "width": 220},
		{"label": _("Qty"), "fieldname": "qty", "fieldtype": "Float", "precision": 0, "width": 70},
		{"label": _("Gross"), "fieldname": "gross", "fieldtype": "Currency", "width": 120},
		{"label": _("Recorded discounts"), "fieldname": "discounts", "fieldtype": "Currency", "width": 140},
		{"label": _("Returns"), "fieldname": "returns", "fieldtype": "Currency", "width": 120},
		{"label": _("Net sales"), "fieldname": "net_sales", "fieldtype": "Currency", "width": 120},
	], rows


def _cat_row(cat, qty, disc, ret, net):
	return {"category": cat, "qty": qty, "gross": flt(net + disc + ret, 2), "discounts": flt(disc, 2),
		"returns": flt(ret, 2), "net_sales": flt(net, 2)}


def _employee_label(owner, cache):
	if not owner or owner in _SYSTEM_USERS:
		return UNATTRIBUTED
	if owner not in cache:
		kind = frappe.db.get_value("User", owner, "user_type")
		cache[owner] = get_fullname(owner) if kind == "System User" else UNATTRIBUTED
	return cache[owner]


def _group_view(ctx, by):
	groups = defaultdict(lambda: frappe._dict(orders=0, gross=0.0, discounts=0.0, returns=0.0, net_sales=0.0,
		tax=0.0, total=0.0, payments=defaultdict(float)))
	names = {}
	for d in ctx.docs:
		key = d.register if by == "register" else _employee_label(d.owner, names)
		g = groups[key]
		g.orders += 1
		g.gross += d.gross
		g.discounts += d.discounts
		g.returns += d.returns
		g.net_sales += d.net
		g.tax += d.sales_tax + d.excise
		g.total += d.total
		for k, v in d.payments.items():
			g.payments[k] += v
	pay = [k for k, _v in _payment_order(ctx.totals)]
	fields = {k: "pay_" + frappe.scrub(k) for k in pay}
	rows = []
	tot = frappe._dict(orders=0, gross=0.0, discounts=0.0, returns=0.0, net_sales=0.0, tax=0.0, total=0.0,
		payments=defaultdict(float))
	for key in sorted(groups, key=lambda k: (k == UNATTRIBUTED, k)):
		g = groups[key]
		rows.append(_group_row(key, g, fields))
		for f in ("orders", "gross", "discounts", "returns", "net_sales", "tax", "total"):
			tot[f] += g[f]
		for k, v in g.payments.items():
			tot.payments[k] += v
	if rows:
		rows.append({**_group_row(_("Total"), tot, fields), "bold": 1, "is_total": 1})
	label = _("Register") if by == "register" else _("Employee")
	cols = [
		{"label": label, "fieldname": "group", "fieldtype": "Data", "width": 200},
		{"label": _("Orders"), "fieldname": "orders", "fieldtype": "Int", "width": 75},
		{"label": _("Gross"), "fieldname": "gross", "fieldtype": "Currency", "width": 115},
		{"label": _("Discounts"), "fieldname": "discounts", "fieldtype": "Currency", "width": 110},
		{"label": _("Returns"), "fieldname": "returns", "fieldtype": "Currency", "width": 110},
		{"label": _("Net sales"), "fieldname": "net_sales", "fieldtype": "Currency", "width": 115},
		{"label": _("Tax"), "fieldname": "tax", "fieldtype": "Currency", "width": 100},
		{"label": _("Total"), "fieldname": "total", "fieldtype": "Currency", "width": 115},
	] + [{"label": _(k), "fieldname": fields[k], "fieldtype": "Currency", "width": 120} for k in pay]
	return cols, rows


def _group_row(key, g, fields):
	row = {"group": key, "orders": g.orders}
	for f in ("gross", "discounts", "returns", "net_sales", "tax", "total"):
		row[f] = flt(g[f], 2)
	for k, field in fields.items():
		row[field] = flt(g.payments.get(k), 2)
	return row


def _message(ctx, view) -> str:
	t = ctx.totals
	alerts = []
	if t.e1:
		alerts.append(_("E1 is off by {0}: the ticket totals do not equal sales plus charges.").format(
			frappe.format_value(t.e1, {"fieldtype": "Currency"})))
	if t.e2:
		alerts.append(_("E2 is off by {0}: the ticket totals do not equal the payments.").format(
			frappe.format_value(t.e2, {"fieldtype": "Currency"})))
	if t.unclassified:
		alerts.append(_("{0} of charges are on an account that is neither tax nor shipping.").format(
			frappe.format_value(t.unclassified, {"fieldtype": "Currency"})))
	parts = [f'<div class="text-danger" style="font-weight:600">{frappe.utils.escape_html(a)}</div>' for a in alerts]
	if view in ("Register", "Employee"):
		parts.append('<span class="text-muted">{0}</span>'.format(
			_("Register = the ticket's POS profile. Employee = who opened the ticket.")))
	return "".join(parts)


# ------------------------------------------------------------------ pre-launch check (not on screen)

def gl_sales_tax_memo(filters) -> dict:
	"""E3, for checking a period before launch: the GL's net credit on the company's sales-tax
	accounts against the report's Sales tax. Expected differences: tax on register tickets not yet
	consolidated, consolidated invoices dated differently from their tickets, and journal entries on
	the tax accounts."""
	filters = _validate(filters)
	from ffl_core.setup.tax import sales_tax_and_excise_accounts

	accounts = sales_tax_and_excise_accounts(filters.company)[0]
	gl = 0.0
	if accounts:
		gl = flt(frappe.db.sql(
			"""SELECT IFNULL(SUM(credit - debit), 0) FROM `tabGL Entry` WHERE company = %s AND is_cancelled = 0
			AND account IN %s AND posting_date BETWEEN %s AND %s""",
			(filters.company, tuple(accounts), filters.from_date, filters.to_date))[0][0], 2)
	report = _collect(filters).totals.sales_tax
	return {"gl": gl, "report": report, "difference": flt(gl - report, 2)}
