"""Member benefits: member prices at the register, worked out on the server.

A Membership Plan lists its benefits (``Membership Plan Benefit``): what a member pays for an Item,
an Item Group (sub-groups too) or the items of a Price List — a percentage or an amount off, a fixed
price, or that price list's own price — on which days, and how many units a day.

* **One line, one answer** — :func:`price_line` prices a line for one member on one day, the day
  the document will be posted on (:func:`posting_day`: today, as ERPNext posts a sale saved or
  completed today, unless the document keeps its own date):

  1. ``list_rate`` is what the line costs anyone today (``price_approval.list_rate``: the
     document's Item Price, a sale already in it, a gun's own price). It is never raised back to
     a price before a sale.
  2. Every benefit of every membership the member holds that day (Active, or Past Due within its
     grace — ``membership.gives_benefits``, the rule of :func:`ffl_core.membership.is_member`) that
     covers the item gives a member price, worked out from the item's *regular* price (before any
     sale). A benefit is defined per stock unit: an Amount off and a Fixed price are multiplied by
     the line's conversion factor (a box of 20 at $1 off a round is $20 off the box).
  3. The lowest member price wins, and only when it is below ``list_rate``: then the line's rate
     is the member price and the line records the benefit. Otherwise the line keeps its price and
     no benefit is used (the better of the two, never both).
  4. A benefit with uses per day covers one stock unit per use; a line needs a use for each stock
     unit (qty × conversion factor). Uses are counted, in stock units, from the lines that carry the
     benefit for that member that day — register tickets and Sales Invoices other than POS
     consolidation, drafts too; a line returned in full gives its uses back to the day it was sold.
     When too few are left the line keeps its price, and the notice names the sales holding the
     uses (drafts marked not completed: an abandoned ticket holds its use until deleted).
  5. A member price of 0 is never given on an item the store taxes (:func:`taxed_here`).
  6. Never priced: trade-in credit and deposit lines, membership fee items, consignment guns, a
     gun priced one by one unless a benefit names its Item, returns (they copy the line they
     return), and documents priced elsewhere (web and GunBroker orders, counter pickup tickets,
     Create Order invoices and other invoices made from an order, our builders' system prices).

* **The line** — :func:`apply_member_prices` (POS Invoice / Sales Invoice before_validate) writes
  the answer: ``price_list_rate`` = ``list_rate``, ``rate`` = the member price, ``discount_amount``
  per unit = the difference, ``discount_percentage`` only for a percentage benefit that gives the
  rate back exactly, and the record fields (``setup/member_benefits``): the benefit, the
  membership, the member and the reason shown on the line. A rate a person lowered below the member
  price stays (the manager price approval judges it, its floor being the member price), with no
  benefit recorded. A draft that no longer qualifies goes back to its list price on save; at
  submit nothing is repriced: a line whose member price is not what the sale carries (started on
  another day, uses spent elsewhere, a line the screen never priced) is refused with what to do; the
  register asks :func:`preview` first and shows the new total instead of completing
  (``public/js/member_prices.js``). :func:`settle_member_lines` (validate)
  drops the record from a line ERPNext priced otherwise (a Pricing Rule).
* **Who** — the document's customer, or the member an in-process ticket builder names per line
  (``pos_ticket.build_ticket``'s ``benefit_customer``). A name sent by a screen or the API is
  ignored; a stored line keeps the member it was built for.
* **Concurrency** — the member's memberships are row-locked before uses are counted, and the
  count reads with a shared lock (a plain read would see the transaction's older snapshot), so two
  tickets saved at once cannot both take the last use.

Extensions call :func:`member_price`, :func:`benefit_usage` and :func:`membership_badge`
(docs/extensions/hooks.md).
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, fmt_money, formatdate, getdate, nowdate

from ffl_core.setup.member_benefits import (BENEFICIARY_FIELD, BENEFIT_FIELD, MEMBER_RATE_FIELD,
	MEMBERSHIP_FIELD, REASON_FIELD)

BENEFIT = "Membership Plan Benefit"
PERCENT, AMOUNT, FIXED, LIST_PRICE = "Percent off", "Amount off", "Fixed price", "Price-list price"
ON_ITEM, ON_GROUP, ON_PRICE_LIST = "Item", "Item Group", "Price List"
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
PRICED = ("POS Invoice", "Sales Invoice")
# doc.flags, server-only: an in-process builder named the member of some lines (pos_ticket.build_ticket).
TRUSTED_MEMBERS = "ffl_benefit_members_trusted"
# frappe.flags (request-local, never from a payload): (document, _floors_key, floors) of the save in progress.
_FLOORS = "ffl_member_floors"
_EPS = 0.0001


class MemberPriceChanged(frappe.ValidationError):
	"""A draft's member price is no longer what it was saved at."""


class MemberUsesBusy(frappe.ValidationError):
	"""Another sale took this member's uses while this one was being saved."""


# ------------------------------------------------------------------ plan configuration


def parse_days(text: str | None) -> list[int] | None:
	"""Weekday numbers (Mon = 0) of a benefit's Days, or None for every day. Raises on a word that is
	not a day."""
	words = [w.strip()[:3].title() for w in (text or "").replace(";", ",").split(",") if w.strip()]
	if not words:
		return None
	bad = [w for w in words if w not in DAYS]
	if bad:
		frappe.throw(_("Days: {0} is not a day. Use Mon, Tue, Wed, Thu, Fri, Sat, Sun.").format(
			escape_html(", ".join(bad))))
	return sorted({DAYS.index(w) for w in words})


def _default_company() -> str | None:
	from ffl_core.membership import _default_company

	return _default_company()


def _tax_templates(company: str) -> list[str]:
	"""The sales tax templates a sale here takes: the company's default one, else every enabled one
	(the stricter reading when a store has no default)."""
	default = frappe.db.get_value("Sales Taxes and Charges Template",
		{"company": company, "is_default": 1, "disabled": 0}, "name")
	if default:
		return [default]
	return frappe.get_all("Sales Taxes and Charges Template", filters={"company": company, "disabled": 0},
		pluck="name")


def taxed_here(item_code: str, company: str | None = None, on=None) -> bool:
	"""Whether this store charges any tax on *item_code*: a rate above 0 on any account of its sales
	tax template once the item's own Item Tax Template (from the item, else its group's) is applied —
	ERPNext's own resolution. The item's type does not matter: a service the store taxes is taxed.

	Args:
	    item_code: The Item.
	    company: The store; blank = the default company.
	    on: The date the template must be valid on; blank = today.

	Returns:
	    True when some tax applies; False when every rate is 0 or the store charges no tax at all.
	"""
	from erpnext.stock.get_item_details import NOT_APPLICABLE_TAX, get_item_tax_map, get_item_tax_template

	company = company or _default_company()
	if not company or not item_code:
		return False
	ctx = frappe._dict(company=company, transaction_date=str(getdate(on or nowdate())),
		posting_date=str(getdate(on or nowdate())), tax_category=None, item_code=item_code, base_net_rate=0)
	template = get_item_tax_template(ctx, frappe.get_cached_doc("Item", item_code))
	if isinstance(template, list):   # several valid ones: ERPNext lets the user pick; any may apply
		templates = template
	else:
		templates = [template]
	for sales in _tax_templates(company):
		rows = frappe.get_all("Sales Taxes and Charges", filters={"parent": sales,
			"parenttype": "Sales Taxes and Charges Template"}, fields=["account_head", "rate"])
		for item_template in templates:
			rates = get_item_tax_map(doc={"company": company, "taxes": rows}, tax_template=item_template,
				as_json=False)
			if any(r != NOT_APPLICABLE_TAX and flt(r) > 0 for r in rates.values()):
				return True
	return False


def _gives_zero(b, item_code: str | None = None) -> bool:
	"""Whether a benefit can price an item at 0 (per item when *item_code* is given)."""
	if b.benefit_type == PERCENT:
		return flt(b.value) >= 100
	if b.benefit_type == FIXED:
		return flt(b.value) <= 0
	if not item_code:
		return False
	if b.benefit_type == AMOUNT:
		from ffl_core.pricing import counter_list_price

		price = counter_list_price(item_code) or flt(frappe.db.get_value("Item", item_code, "standard_rate"))
		return price > 0 and flt(b.value) >= price
	if b.benefit_type == LIST_PRICE and b.price_list:
		price = frappe.db.get_value("Item Price", {"item_code": item_code, "price_list": b.price_list,
			"selling": 1}, "price_list_rate")
		return price is not None and flt(price) <= 0
	return False


def _scope_items(b, limit: int) -> list[str]:
	"""Some enabled sales items a group- or price-list-wide benefit covers (for the save checks)."""
	if b.applies_to == ON_GROUP and b.item_group:
		lft, rgt = frappe.db.get_value("Item Group", b.item_group, ["lft", "rgt"]) or (0, 0)
		return frappe.db.sql_list("""select i.name from `tabItem` i join `tabItem Group` g on g.name = i.item_group
			where g.lft >= %s and g.rgt <= %s and i.disabled = 0 and i.is_sales_item = 1 order by i.name limit %s""",
			(lft, rgt, limit))
	if b.applies_to == ON_PRICE_LIST and b.price_list:
		return frappe.db.sql_list("""select distinct ip.item_code from `tabItem Price` ip join `tabItem` i
			on i.name = ip.item_code where ip.price_list = %s and ip.selling = 1 and i.disabled = 0
			order by ip.item_code limit %s""", (b.price_list, limit))
	return [b.item] if b.applies_to == ON_ITEM and b.item else []


def _below_cost(b, limit: int = 10) -> list[str]:
	"""Items a fixed or price-list member price would sell under their valuation rate (a warning)."""
	if b.benefit_type == FIXED:
		if b.applies_to == ON_ITEM:
			cost = flt(frappe.db.get_value("Item", b.item, "valuation_rate"))
			return [b.item] if cost > flt(b.value) else []
		return [code for code in _scope_items(b, 500)  # lazy: first 500 items of the scope, page if a store needs more
			if flt(frappe.db.get_value("Item", code, "valuation_rate")) > flt(b.value)][:limit]
	if b.benefit_type == LIST_PRICE and b.price_list:
		return frappe.db.sql_list("""select ip.item_code from `tabItem Price` ip join `tabItem` i on i.name = ip.item_code
			where ip.price_list = %s and ip.selling = 1 and i.valuation_rate > ip.price_list_rate
			order by ip.item_code limit %s""", (b.price_list, limit))
	return []


def validate_plan_benefits(plan) -> None:
	"""Membership Plan validate: each benefit is complete, and a member price of 0 never lands on an
	item the store taxes (an Item benefit is refused; a group / price-list one names the taxed items,
	which keep their price at the register). Warns of member prices under cost and of discounts on
	taxed items (whether the membership fee stays untaxed is the store accountant's call)."""
	company = _default_company()
	own = {x for x in (plan.item, plan.registration_item) if x}
	warnings, taxed_scope = [], False
	for b in plan.get("benefits") or []:
		where = _("Benefit row {0}").format(b.idx)
		target = {ON_ITEM: b.item, ON_GROUP: b.item_group, ON_PRICE_LIST: b.price_list}.get(b.applies_to)
		if not target:
			frappe.throw(_("{0}: pick the {1} it applies to.").format(where, _(b.applies_to)))
		for field, keep in (("item", ON_ITEM), ("item_group", ON_GROUP), ("price_list", ON_PRICE_LIST)):
			if b.applies_to != keep:
				b.set(field, None)
		if b.applies_to == ON_ITEM and b.item in own:
			frappe.throw(_("{0}: {1} is this plan's own fee; a membership fee never takes a member price.").format(
				where, escape_html(b.item)))
		if b.benefit_type == LIST_PRICE:
			if b.applies_to != ON_PRICE_LIST:
				frappe.throw(_("{0}: Price-list price needs Applies To = Price List.").format(where))
			b.value = 0
		elif b.benefit_type == PERCENT and not 0 < flt(b.value) <= 100:
			frappe.throw(_("{0}: the percentage off must be more than 0 and at most 100.").format(where))
		elif b.benefit_type == AMOUNT and flt(b.value) <= 0:
			frappe.throw(_("{0}: the amount off must be more than 0.").format(where))
		elif b.benefit_type == FIXED and flt(b.value) < 0:
			frappe.throw(_("{0}: the fixed price cannot be negative.").format(where))
		if cint(b.uses_per_day) < 0:
			frappe.throw(_("{0}: uses per day cannot be negative.").format(where))
		days = parse_days(b.days)
		b.days = ", ".join(DAYS[d] for d in days) if days else None
		items = _scope_items(b, 200)  # lazy: the save checks read the first 200 items of a group / list; pricing checks every line
		if b.applies_to == ON_ITEM and _gives_zero(b, b.item) and taxed_here(b.item, company):
			frappe.throw(_("{0}: {1} is taxed here, so it cannot be given for free (a member price of 0). "
				"Free is for items the store does not tax, like range time.").format(where, escape_html(b.item)),
				title=_("Free taxed item"))
		taxed = [code for code in items if taxed_here(code, company)]
		if taxed:
			taxed_scope = True
		if b.applies_to != ON_ITEM and _gives_zero(b) and taxed:
			warnings.append(_("{0} is free for members, but these items are taxed and keep their price: {1}").format(
				escape_html(target), escape_html(", ".join(taxed[:10]))))
		cheap = _below_cost(b)
		if cheap:
			warnings.append(_("{0}: the member price is under cost for {1}.").format(where,
				escape_html(", ".join(cheap))))
	before = plan.get_doc_before_save() if not plan.is_new() else None
	changed = _benefit_rows(plan) != (_benefit_rows(before) if before else [])
	if taxed_scope and changed:
		warnings.append(_("This plan discounts items the store taxes. Ask your accountant whether the membership "
			"fee is still untaxed with these benefits."))
	if warnings:
		frappe.msgprint("<br>".join(warnings), title=_("Member benefits"), indicator="orange")


def _benefit_rows(plan) -> list[tuple]:
	return [(b.applies_to, b.item, b.item_group, b.price_list, b.benefit_type, flt(b.value), cint(b.uses_per_day),
		b.days or "") for b in plan.get("benefits") or []]


def benefit_label(b, plan_name: str) -> str:
	"""What a line priced by benefit *b* shows: the benefit's Label, else e.g. "Member 50% (Basic)"."""
	if (b.get("label") or "").strip():
		return b.label.strip()
	if b.benefit_type == PERCENT:
		what = _("Member {0}%").format(f"{flt(b.value):g}")
	elif b.benefit_type == AMOUNT:
		what = _("Member {0} off").format(fmt_money(flt(b.value), currency=frappe.defaults.get_global_default("currency")))
	else:
		what = _("Member price")
	return f"{what} ({plan_name})"


def summarize_benefits(plan_name: str) -> list[str]:
	"""One line per benefit of a plan, for the Memberships page's plan cards."""
	out = []
	for b in frappe.get_all(BENEFIT, filters={"parent": plan_name, "parenttype": "Membership Plan"},
			fields=["applies_to", "item", "item_group", "price_list", "benefit_type", "value", "uses_per_day", "days",
				"label"], order_by="idx asc"):
		target = {ON_ITEM: b.item, ON_GROUP: b.item_group, ON_PRICE_LIST: b.price_list}.get(b.applies_to) or ""
		if b.benefit_type == PERCENT:
			deal = _("{0}% off").format(f"{flt(b.value):g}")
		elif b.benefit_type == AMOUNT:
			deal = _("{0} off").format(fmt_money(flt(b.value), currency=frappe.defaults.get_global_default("currency")))
		elif b.benefit_type == FIXED:
			deal = fmt_money(flt(b.value), currency=frappe.defaults.get_global_default("currency"))
		else:
			deal = _("member prices")
		parts = [f"{target}: {deal}"]
		if cint(b.uses_per_day):
			parts.append(_("{0} a day").format(cint(b.uses_per_day)))
		if b.days:
			parts.append(b.days)
		out.append(" · ".join(parts))
	return out


# ------------------------------------------------------------------ pricing one line


def _live_memberships(customer: str, day) -> list:
	"""The customer's memberships that give benefits on *day*: Active, or Past Due within grace."""
	from ffl_core.membership import ACTIVE, PAST_DUE, gives_benefits

	if not customer:
		return []
	rows = frappe.db.sql("""select m.name, m.plan, m.status, m.grace_until, m.current_period_end,
			coalesce(p.plan_name, m.plan) as plan_name
		from `tabMembership` m left join `tabMembership Plan` p on p.name = m.plan
		where m.customer = %s and m.status in %s order by m.name""", (customer, (ACTIVE, PAST_DUE)), as_dict=True)
	return [r for r in rows if gives_benefits(r.status, r.grace_until, day)]


def _benefits(customer: str, day, cache: dict) -> list:
	"""[(benefit row, membership row)] the customer holds on *day*, in a stable order."""
	key = ("benefits", customer, str(day))
	if key not in cache:
		out = []
		live = _live_memberships(customer, day)
		plans = sorted({m.plan for m in live})
		rows = frappe.get_all(BENEFIT, filters={"parenttype": "Membership Plan", "parent": ("in", plans)},
			fields=["name", "parent", "idx", "applies_to", "item", "item_group", "price_list", "benefit_type",
				"value", "uses_per_day", "days", "label"], order_by="parent asc, idx asc") if plans else []
		for m in live:
			out.extend((b, m) for b in rows if b.parent == m.plan)
		cache[key] = out
	return cache[key]


def _item(item_code: str, cache: dict):
	key = ("item", item_code)
	if key not in cache:
		cache[key] = frappe.db.get_value("Item", item_code, ["name", "item_group", "has_serial_no",
			"uniform_sell_price", "standard_rate", "enable_deferred_revenue", "stock_uom"], as_dict=True)
	return cache[key]


def _never_priced(item, cache: dict) -> bool:
	"""Trade-in credit / deposit lines and membership fee items."""
	from ffl_core.membership import plan_items
	from ffl_core.setup.locked_lines import locked_items

	if "fees" not in cache:
		cache["fees"] = plan_items()
	return item.name in locked_items() or item.name in cache["fees"] or cint(item.enable_deferred_revenue)


def _in_group(item, group: str, cache: dict) -> bool:
	key = ("group", group)
	if key not in cache:
		cache[key] = frappe.db.get_value("Item Group", group, ["lft", "rgt"]) or (0, 0)
	lft, rgt = cache[key]
	own = ("group", item.item_group)
	if own not in cache:
		cache[own] = frappe.db.get_value("Item Group", item.item_group, ["lft", "rgt"]) or (0, 0)
	return bool(lft) and lft <= cache[own][0] and cache[own][1] <= rgt


def _price_in(price_list: str, doc, row, item, cache: dict) -> float | None:
	"""The item's price in another price list for this line's unit, or None."""
	from erpnext.stock.get_item_details import get_price_list_rate_for

	uom = row.get("uom") or item.stock_uom
	key = ("price", price_list, item.name, uom, flt(row.get("qty")) or 1, doc.get("customer"))
	if key not in cache:
		ctx = frappe._dict(price_list=price_list, customer=doc.get("customer"), uom=uom, stock_uom=item.stock_uom,
			transaction_date=_pricing_view(doc, cache).posting_date, qty=flt(row.get("qty")) or 1,
			conversion_factor=flt(row.get("conversion_factor")) or 1, batch_no=row.get("batch_no"))
		price = get_price_list_rate_for(ctx, item.name)
		cache[key] = None if price is None else flt(price)
	return cache[key]


def _covers(b, item, doc, row, day, per_gun: bool, cache: dict) -> bool:
	days = parse_days(b.days)
	if days is not None and getdate(day).weekday() not in days:
		return False
	if b.applies_to == ON_ITEM:
		return b.item == item.name
	if per_gun:   # a gun priced one by one only takes a benefit that names its Item
		return False
	if b.applies_to == ON_GROUP:
		return bool(b.item_group) and _in_group(item, b.item_group, cache)
	if b.applies_to == ON_PRICE_LIST:
		return bool(b.price_list) and _price_in(b.price_list, doc, row, item, cache) is not None
	return False


def _regular(doc, row, item, listed: float, per_gun: bool, cache: dict) -> float:
	"""The line's price before any sale: a uniform-priced Item's standard rate on the default price
	list, a gun's own price without its sale, else the list price itself (no sale is kept anywhere
	else)."""
	from ffl_core.utils import split_serials

	if per_gun:
		serials = split_serials(row.get("serial_no"))
		if len(serials) == 1:
			from ffl_core.firearm import effective_serial_price

			return flt(effective_serial_price(serials[0])) or listed
		return listed
	if cint(item.uniform_sell_price) and flt(item.standard_rate) > 0:
		from ffl_core.item_hooks import _default_selling_price_list

		if _pricing_view(doc, cache).selling_price_list == _default_selling_price_list():
			return flt(item.standard_rate) * (flt(row.get("conversion_factor")) or 1)
	return listed


def _factor(row) -> float:
	"""Stock units in one unit of the line (its conversion factor)."""
	return flt(row.get("conversion_factor")) or 1


def _units(row) -> float:
	"""The line's quantity in stock units: what its uses are counted in."""
	return flt(row.get("qty")) * _factor(row)


def _member_rate(b, regular: float, doc, row, item, p: int, cache: dict) -> float | None:
	"""The member price of one unit of the line; a benefit's amount is per stock unit."""
	if b.benefit_type == PERCENT:
		return flt(regular - flt(regular * flt(b.value) / 100, 2), p)
	if b.benefit_type == AMOUNT:
		return max(0.0, flt(regular - flt(b.value) * _factor(row), p))
	if b.benefit_type == FIXED:
		return flt(flt(b.value) * _factor(row), p)
	if b.benefit_type == LIST_PRICE and b.price_list:
		price = _price_in(b.price_list, doc, row, item, cache)
		return None if price is None else flt(price, p)
	return None


def _taxed(item_code: str, company: str | None, day, cache: dict) -> bool:
	key = ("taxed", item_code, company)
	if key not in cache:
		cache[key] = taxed_here(item_code, company, day)
	return cache[key]


def _exclusions(doctype: str, name: str | None) -> tuple[str, str]:
	return (name or "" if doctype == "POS Invoice" else ""), (name or "" if doctype == "Sales Invoice" else "")


def used_on(benefit: str, customer: str, day, *, exclude: tuple[str, str | None] = ("", None),
		lock: bool = False) -> float:
	"""Stock units of *benefit* the customer took on *day*: lines carrying it for them on register
	tickets and on Sales Invoices other than POS consolidation, drafts and submitted, less lines
	returned in full. *exclude*: (doctype, name) of the document being priced. *lock*: read with a
	shared lock — the latest committed rows, not the transaction's snapshot (a caller holding the
	membership lock)."""
	if lock:
		try:
			lines = _used_lines(benefit, customer, day, exclude, " lock in share mode")
		except frappe.QueryDeadlockError:
			# A sale committed lines of this benefit after this transaction began (MariaDB's snapshot
			# isolation answers a locking read of them with 1020): never price on the stale count.
			frappe.throw(_("This member's daily benefit was just used on another sale. Save again to see what "
				"is left today."), MemberUsesBusy, title=_("Member price"))
	else:
		lines = _used_lines(benefit, customer, day, exclude, "")
	return sum(flt(l.units) for l in lines)


def used_by(benefit: str, customer: str, day, *, exclude: tuple[str, str | None] = ("", None)) -> list:
	"""[(document, docstatus)] of the sales whose lines hold the uses :func:`used_on` counts, oldest
	first: where a member's uses for the day went (an abandoned draft holds its use until deleted)."""
	seen: dict = {}
	for line in _used_lines(benefit, customer, day, exclude, ""):
		seen.setdefault(line.parent, cint(line.docstatus))
	return list(seen.items())


def _used_lines(benefit: str, customer: str, day, exclude: tuple, share: str) -> list:
	"""The lines :func:`used_on` counts: {name, units, parent, docstatus}, not returned in full."""
	# lazy: counted from the invoice lines on every read; a per-member, per-day count table once a
	# store's volume makes this read show up.
	pos_x, si_x = _exclusions(*exclude)
	args = {"b": benefit, "c": customer, "d": getdate(day), "pos_x": pos_x, "si_x": si_x}
	units = "i.qty * coalesce(nullif(i.conversion_factor, 0), 1) as units"
	lines = frappe.db.sql(f"""select i.name, {units}, p.name as parent, p.docstatus, p.creation
		from `tabPOS Invoice Item` i join `tabPOS Invoice` p on p.name = i.parent
		where i.{BENEFIT_FIELD} = %(b)s and i.{BENEFICIARY_FIELD} = %(c)s and p.posting_date = %(d)s
			and p.docstatus < 2 and p.is_return = 0 and p.name != %(pos_x)s{share}""", args, as_dict=True)
	lines += frappe.db.sql(f"""select i.name, {units}, s.name as parent, s.docstatus, s.creation
		from `tabSales Invoice Item` i join `tabSales Invoice` s on s.name = i.parent
		where i.{BENEFIT_FIELD} = %(b)s and i.{BENEFICIARY_FIELD} = %(c)s and s.posting_date = %(d)s
			and s.docstatus < 2 and s.is_return = 0 and s.is_consolidated = 0 and s.name != %(si_x)s{share}""",
		args, as_dict=True)
	if not lines:
		return []
	returned = _returned_qty([l.name for l in lines])
	return sorted((l for l in lines if returned.get(l.name, 0) + _EPS < flt(l.units)),
		key=lambda l: (l.creation, l.parent, l.name))


def _returned_qty(names: list[str]) -> dict:
	"""{original line: stock units returned} over submitted returns: register returns, and credit
	notes (made against the original invoice, or against the consolidation invoice that carried it,
	or naming the register line itself)."""
	wanted = set(names)
	out: dict = {}
	units = "abs(r.qty * coalesce(nullif(r.conversion_factor, 0), 1)) as units"
	rows = frappe.db.sql(f"""select r.pos_invoice_item as orig, {units}
		from `tabPOS Invoice Item` r join `tabPOS Invoice` p on p.name = r.parent
		where p.is_return = 1 and p.docstatus = 1 and r.pos_invoice_item in %(n)s""", {"n": names}, as_dict=True)
	for r in frappe.db.sql(f"""select c.pos_invoice_item as merged, r.sales_invoice_item, r.pos_invoice_item, {units}
			from `tabSales Invoice Item` r join `tabSales Invoice` s on s.name = r.parent
				left join `tabSales Invoice Item` c on c.name = r.sales_invoice_item
			where s.is_return = 1 and s.docstatus = 1 and s.is_consolidated = 0
				and (r.sales_invoice_item in %(n)s or r.pos_invoice_item in %(n)s or c.pos_invoice_item in %(n)s)""",
			{"n": names}, as_dict=True):
		# The original is whichever of the three the query matched.
		orig = next((x for x in (r.merged, r.sales_invoice_item, r.pos_invoice_item) if x in wanted), None)
		rows.append(frappe._dict(orig=orig, units=r.units))
	for r in rows:
		if r.orig in wanted:
			out[r.orig] = out.get(r.orig, 0) + flt(r.units)
	return out


def posting_day(doc):
	"""The day *doc* will be posted on. ERPNext moves a POS Invoice / Sales Invoice to today on every
	save, in its own validate, after this module priced it (``SalesInvoice.validate_auto_set_posting_time``
	→ ``TransactionBase.validate_posting_time``), unless the document keeps its own date: Edit Posting
	Date and Time (``set_posting_time``), a new amendment, or an import / restore that carries a date.
	Anything else (a quote's plain dict) keeps the date it was given."""
	from frappe.model.document import Document

	if isinstance(doc, Document) and doc.doctype in PRICED:
		keeps = cint(doc.get("set_posting_time")) or (doc.is_new() and doc.get("amended_from")) \
			or ((frappe.flags.in_import or doc.flags.get("from_restore")) and doc.get("posting_date"))
		if not keeps:
			return getdate(nowdate())
	return getdate(doc.get("posting_date") or nowdate())


def _pricing_view(doc, cache: dict):
	"""What the price lookups read off the document, with the price list ERPNext will give it when it
	has none yet (a Sales Invoice built on the server is validated before its missing values are set)
	and the day it will be posted on (:func:`posting_day`)."""
	if "view" not in cache:
		price_list = doc.get("selling_price_list")
		if not price_list:
			from erpnext.accounts.party import get_default_price_list

			from ffl_core.item_hooks import _default_selling_price_list

			customer = doc.get("customer")
			price_list = (get_default_price_list(frappe.get_cached_doc("Customer", customer))
				if customer and frappe.db.exists("Customer", customer) else None) or _default_selling_price_list()
		cache["view"] = frappe._dict(selling_price_list=price_list, customer=doc.get("customer"),
			posting_date=posting_day(doc), plc_conversion_rate=doc.get("plc_conversion_rate"),
			conversion_rate=doc.get("conversion_rate"))
	return cache["view"]


def price_line(doc, row, customer: str | None, *, consumed: dict | None = None, lock: bool = False,
		cache: dict | None = None) -> frappe._dict:
	"""The member price of one line (module docstring, steps 1-6) for *customer* on the day the
	document will be posted on (:func:`posting_day`). Writes nothing.

	*consumed*: {(benefit, customer): stock units} this document's earlier lines took. *lock*: count uses
	with a shared lock (the caller holds the membership lock).

	Returns:
	    {list_rate, rate, regular, benefit, membership, plan, reason, uses_left, limit, notice,
	    percentage}: ``rate`` == ``list_rate`` and ``benefit`` None when nothing applies.
	    ``uses_left``: of the chosen benefit before this line (None = no limit). ``notice``: why a
	    better member price was not given (no uses left, and which sales took them). ``percentage``: the discount percentage the
	    line may show (0 unless a percentage benefit gives the rate back exactly).
	"""
	from ffl_core.compliance.locked_lines import rate_precision
	from ffl_core.price_approval import list_rate

	cache = {} if cache is None else cache
	consumed = consumed or {}
	p = rate_precision(doc.get("doctype") or "POS Invoice")
	listed = flt(list_rate(_pricing_view(doc, cache), row, cache.setdefault("list", {}))[0], p) \
		if row.get("item_code") else 0.0
	out = frappe._dict(list_rate=listed, rate=listed, regular=listed, benefit=None, membership=None, plan=None,
		reason=None, uses_left=None, limit=None, notice=None, percentage=0)
	if listed <= 0 or not customer or flt(row.get("qty")) <= 0:
		return out
	item = _item(row.item_code, cache)
	if not item or _never_priced(item, cache):
		return out
	day = posting_day(doc)
	held = _benefits(customer, day, cache)
	if not held:
		return out
	per_gun = bool(cint(item.has_serial_no) and not cint(item.uniform_sell_price))
	regular = _regular(doc, row, item, listed, per_gun, cache)
	out.regular = regular
	qty = _units(row)
	best = blocked = None
	for b, m in held:
		if not _covers(b, item, doc, row, day, per_gun, cache):
			continue
		rate = _member_rate(b, regular, doc, row, item, p, cache)
		if rate is None or rate >= listed - _EPS:
			continue
		if rate <= _EPS and _taxed(item.name, doc.get("company"), day, cache):
			continue
		left = None
		if cint(b.uses_per_day):
			key = ("used", b.name, customer, str(day))
			if key not in cache:
				cache[key] = used_on(b.name, customer, day, exclude=(doc.get("doctype") or "", doc.get("name")),
					lock=lock)
			left = max(0.0, cint(b.uses_per_day) - cache[key] - flt(consumed.get((b.name, customer))))
			if left + _EPS < qty:
				if blocked is None or rate < blocked[2]:
					blocked = (b, m, rate, left)
				continue
		if best is None or (rate, b.name) < (best[2], best[0].name):
			best = (b, m, rate, left)
	if best:
		b, m, rate, left = best
		out.update(rate=rate, benefit=b.name, membership=m.name, plan=m.plan, reason=benefit_label(b, m.plan_name),
			uses_left=left, limit=cint(b.uses_per_day) or None)
		if b.benefit_type == PERCENT and abs(regular - listed) < _EPS \
				and flt(listed - flt(listed * flt(b.value) / 100, 2), p) == rate:
			cap = flt(frappe.get_cached_value("Item", item.name, "max_discount"))
			out.percentage = 0 if cap and flt(b.value) > cap else flt(b.value)
	if blocked and (best is None or blocked[2] < best[2]):
		b, m, _rate, left = blocked
		label = benefit_label(b, m.plan_name)
		out.notice = (_("{0}: no uses left today").format(label) if left <= _EPS else
			_("{0}: {1} left today, this line has {2}").format(label, f"{left:g}", f"{qty:g}")) \
			+ "." + _where_used(b.name, customer, day, doc, flt(consumed.get((b.name, customer))))
		if best is None:
			out.uses_left, out.limit = left, cint(b.uses_per_day)
	return out


def _where_used(benefit: str, customer: str, day, doc, here: float) -> str:
	"""The "Used on: X, Y (not completed)." tail of a no-uses notice: the sales holding the member's
	uses of the day, so the counter can open the right one or delete a ticket left behind."""
	where = [_("earlier lines of this sale")] if here > _EPS else []
	tickets = used_by(benefit, customer, day, exclude=(doc.get("doctype") or "", doc.get("name")))
	for name, docstatus in tickets[:3]:
		where.append(name if docstatus else _("{0} (not completed)").format(name))
	if len(tickets) > 3:
		where.append(_("{0} more").format(len(tickets) - 3))
	return " " + _("Used on: {0}.").format(", ".join(where)) if where else ""


# ------------------------------------------------------------------ the invoice


def _skip(doc) -> bool:
	"""Documents never priced here (module docstring, step 6)."""
	from ffl_core.api.counter_pickup import _is_ticket
	from ffl_core.compliance.channel_invoice import is_fulfilled_elsewhere
	from ffl_core.price_approval import CHECKED, SYSTEM_PRICED

	flags = getattr(doc, "flags", None) or {}
	if cint(doc.get("is_return")) or cint(doc.get("is_consolidated")) or flags.get(SYSTEM_PRICED) \
			or is_fulfilled_elsewhere(doc):
		return True
	if doc.doctype == "POS Invoice":
		return _is_ticket(doc)
	if flags.get(CHECKED) or any(r.get("so_detail") or r.get("sales_order") for r in doc.get("items") or []):
		return True   # Create Order (later), or an invoice made from an order: priced on the order
	from ffl_core.api.manual_order import _is_counter_order

	return _is_counter_order(doc)


def _excluded_row(row) -> bool:
	"""A consignment gun (the discount would come out of the consignor's share) or a free item."""
	return flt(row.get("consignment_commission_pct")) > 0 or cint(row.get("is_free_item"))


def _beneficiary(doc, row, stored: dict, stored_customer: str | None) -> tuple[str | None, bool]:
	"""(member whose benefits price *row*, whether named apart from the customer). Another member is
	taken only from an in-process builder (doc.flags) or from the stored line it was built with."""
	named = (row.get(BENEFICIARY_FIELD) or "").strip()
	customer = doc.get("customer")
	if named and named != customer:
		if (getattr(doc, "flags", None) or {}).get(TRUSTED_MEMBERS) and frappe.db.exists("Customer", named):
			return named, True
		was = stored.get(row.get("name"))
		if was is not None and was.get(BENEFICIARY_FIELD) == named and named != stored_customer:
			return named, True
	return customer, False


def _set(row, field: str, value) -> None:
	row.set(field, value) if hasattr(row, "set") else setattr(row, field, value)


def _clear_record(row, keep_member: bool) -> None:
	for field in (BENEFIT_FIELD, MEMBERSHIP_FIELD, MEMBER_RATE_FIELD):
		_set(row, field, 0 if field == MEMBER_RATE_FIELD else None)
	if not keep_member:
		_set(row, BENEFICIARY_FIELD, None)
	_set(row, REASON_FIELD, None)


def _write_member(row, res, member: str) -> None:
	_set(row, "price_list_rate", res.list_rate)
	_set(row, "rate", res.rate)
	_set(row, "margin_type", None)
	_set(row, "margin_rate_or_amount", 0)
	_set(row, "discount_percentage", res.percentage)
	_set(row, "discount_amount", flt(res.list_rate - res.rate, 2))
	_set(row, BENEFIT_FIELD, res.benefit)
	_set(row, MEMBERSHIP_FIELD, res.membership)
	_set(row, BENEFICIARY_FIELD, member)
	_set(row, MEMBER_RATE_FIELD, res.rate)
	_set(row, REASON_FIELD, res.reason)


def _write_list(row, listed: float) -> None:
	_set(row, "price_list_rate", listed)
	_set(row, "rate", listed)
	_set(row, "discount_percentage", 0)
	_set(row, "discount_amount", 0)


def _lock_memberships(members: set[str], day) -> None:
	"""Row-lock every live membership of these members, in one statement and one order, before uses
	are counted: two tickets for the same member serialize here."""
	names = sorted({m.name for c in members for m in _live_memberships(c, day)})
	if names:
		frappe.db.sql("select name from `tabMembership` where name in %s order by name for update", (names,))


def _has_benefits() -> bool:
	return bool(frappe.db.sql(f"select 1 from `tab{BENEFIT}` where parenttype = 'Membership Plan' limit 1"))


def reprice(doc, before=None, *, submitting: bool = False, lock: bool = False) -> dict:
	"""Work out every line of *doc* and write the answer onto it (module docstring, "The line").
	*before*: the document as stored. Returns {"notices": [...], "refused": [...], "floors": {line
	name (or idx): member price}}; with *submitting*, a line whose price would move is left as it is
	and listed in "refused". "floors" is :func:`member_floors`' answer for these lines."""
	from ffl_core.compliance.locked_lines import rate_precision

	out = {"notices": [], "refused": [], "floors": {}}
	rows = [r for r in doc.get("items") or [] if r.get("item_code") and flt(r.get("qty")) > 0]
	if not rows or (not _has_benefits() and not any(r.get(BENEFIT_FIELD) for r in rows)):
		return out
	p = rate_precision(doc.doctype)
	stored = {r.name: r for r in (before.get("items") or [])} if before else {}
	stored_customer = before.get("customer") if before else None
	day = posting_day(doc)
	members = {r.name if r.get("name") else id(r): _beneficiary(doc, r, stored, stored_customer) for r in rows}
	if lock:
		_lock_memberships({m for m, _named in members.values() if m}, day)
	cache: dict = {}
	consumed: dict = {}
	for row in sorted(rows, key=lambda r: cint(r.get("idx"))):
		member, named = members[row.name if row.get("name") else id(row)]
		rate = flt(row.get("rate"), p)
		stamped = bool(row.get(BENEFIT_FIELD))
		ours = stamped and abs(rate - flt(row.get(MEMBER_RATE_FIELD), p)) < _EPS
		res = None if _excluded_row(row) else price_line(doc, row, member, consumed=consumed, lock=lock, cache=cache)
		listed = res.list_rate if res else flt(row.get("price_list_rate"), p)
		if res and res.benefit:
			out["floors"][row.get("name") or row.get("idx")] = res.rate
		charged = listed if (ours or (not rate and not stamped)) else rate
		if res and res.benefit and charged >= res.rate - _EPS:
			if submitting and abs(rate - res.rate) > _EPS:
				out["refused"].append((row, rate, res.rate, res.reason))
				continue
			_write_member(row, res, member)
			consumed[(res.benefit, member)] = flt(consumed.get((res.benefit, member))) + _units(row)
			continue
		if res and res.notice and (row.get("name") not in stored or stamped):
			out["notices"].append(_("Row {0} ({1}): {2} The line is at its regular price.").format(
				row.get("idx"), escape_html(row.item_code), escape_html(res.notice)))
		if ours:   # its member price no longer applies: back to the list price
			if submitting:
				out["refused"].append((row, rate, listed, row.get(REASON_FIELD)))
				continue
			_write_list(row, listed)
		if stamped or (row.get(BENEFICIARY_FIELD) and not named):
			_clear_record(row, keep_member=named)
		elif named:
			_set(row, BENEFICIARY_FIELD, member)
	return out


def apply_member_prices(doc, method: str | None = None) -> None:
	"""before_validate (POS Invoice / Sales Invoice): the member price of every line (module docstring).
	Refuses a submit whose member prices moved since the draft was saved."""
	frappe.flags.pop(_FLOORS, None)   # never a previous save's answer
	if doc.doctype not in PRICED or getattr(doc, "_action", "save") not in ("save", "submit") or _skip(doc):
		return
	submitting = getattr(doc, "_action", None) == "submit"
	before = None if doc.is_new() else doc.get_doc_before_save()
	found = reprice(doc, before, submitting=submitting, lock=True)
	if found["refused"]:
		lines = [_("Row {0} ({1}): {2} → {3}{4}").format(row.idx, escape_html(row.item_code),
			fmt_money(was, currency=doc.get("currency")), fmt_money(now, currency=doc.get("currency")),
			f" ({escape_html(why)})" if why else "") for row, was, now, why in found["refused"]]
		frappe.throw(_("The member price is different now: {0}. The sale was started on another day, or the "
			"member used today's benefit on another sale. On the register, press Complete Order again to update the "
			"total; on a form, save the sale. Then collect the new total and complete it.").format("; ".join(lines)), MemberPriceChanged, title=_("Member price changed"))
	frappe.flags[_FLOORS] = (doc, _floors_key(doc), found["floors"])
	if found["notices"]:
		frappe.msgprint("<br>".join(found["notices"]), title=_("Member price"), indicator="orange", alert=True)


def settle_member_lines(doc, method: str | None = None) -> None:
	"""validate, after ERPNext's own: a line ERPNext priced otherwise than its member price (a Pricing
	Rule that took it over) carries no benefit record — the benefit was not used.
	# lazy: a Pricing Rule on a member's line wins even when the member price is lower (ERPNext
	# re-applies the rule in its own validate); upgrade path: drop the rule from such a line."""
	if doc.doctype not in PRICED or cint(doc.get("is_return")) or cint(doc.get("is_consolidated")):
		return
	from ffl_core.compliance.locked_lines import rate_precision

	p = rate_precision(doc.doctype)
	for row in doc.get("items") or []:
		if row.get(BENEFIT_FIELD) and abs(flt(row.rate, p) - flt(row.get(MEMBER_RATE_FIELD), p)) > _EPS:
			_clear_record(row, keep_member=row.get(BENEFICIARY_FIELD) not in (None, "", doc.get("customer")))


def _floors_key(doc) -> tuple:
	"""What a line's member price depends on, for :func:`member_floors` to know apply_member_prices'
	answer still describes the document's current lines."""
	return (posting_day(doc), doc.get("customer"), doc.get("selling_price_list"), tuple(
		(r.get("name") or r.get("idx"), r.get("idx"), r.get("item_code"), flt(r.get("qty")), r.get("uom"), _factor(r),
			r.get("serial_no") or "", r.get("batch_no") or "", r.get(BENEFICIARY_FIELD) or "",
			flt(r.get("consignment_commission_pct")), cint(r.get("is_free_item")))
		for r in doc.get("items") or []))


def member_floors(doc, before=None) -> dict:
	"""{line name (or idx): member price} of the lines a member price covers, worked out again on the
	server for the price approval's floor (``price_approval.reductions``); a screen's claim counts
	for nothing. Within a save, the answer :func:`apply_member_prices` worked out for these very lines
	is reused (kept server-side in ``frappe.flags``, for this document object only)."""
	from frappe.model.document import Document

	if doc.get("doctype") not in PRICED or not isinstance(doc, Document) or _skip(doc):
		return {}   # a Create Order payload (a plain dict) is not priced here
	kept = frappe.flags.get(_FLOORS)
	if kept and kept[0] is doc and kept[1] == _floors_key(doc):
		return dict(kept[2])
	rows = [r for r in doc.get("items") or [] if r.get("item_code") and flt(r.get("qty")) > 0]
	if not rows or not _has_benefits():
		return {}
	stored = {r.name: r for r in (before.get("items") or [])} if before else {}
	stored_customer = before.get("customer") if before else None
	cache: dict = {}
	consumed: dict = {}
	out = {}
	for row in sorted(rows, key=lambda r: cint(r.get("idx"))):
		if _excluded_row(row):
			continue
		member, _named = _beneficiary(doc, row, stored, stored_customer)
		res = price_line(doc, row, member, consumed=consumed, cache=cache)
		if res.benefit:
			out[row.get("name") or row.get("idx")] = res.rate
			consumed[(res.benefit, member)] = flt(consumed.get((res.benefit, member))) + _units(row)
	return out


# ------------------------------------------------------------------ the stable surface


def _quote_doc(customer: str, on_date=None):
	from ffl_core.item_hooks import _default_selling_price_list

	return frappe._dict(doctype="POS Invoice", name=None, customer=customer, company=_default_company(),
		selling_price_list=_default_selling_price_list(), posting_date=getdate(on_date or nowdate()),
		currency=frappe.defaults.get_global_default("currency"), items=[], flags=frappe._dict())


def member_price(customer: str, item_code: str, qty: float = 1, on_date=None, *,
		exclude_invoice: str | None = None) -> dict:
	"""What *customer* pays for *qty* of *item_code* on *on_date*, as the register would price it.

	Args:
	    customer: The member (the person who uses the benefit, not necessarily the payer).
	    item_code: The Item.
	    qty: Stock units on the line; a benefit with uses per day needs one use per stock unit.
	    on_date: The day (store-local); blank = today.
	    exclude_invoice: A POS Invoice / Sales Invoice whose own lines must not count as used (the
	        ticket being priced).

	Returns:
	    {"list_rate", "rate", "benefit", "membership", "reason", "uses_left", "notice"}. With no
	    benefit (not a member, item not covered, no uses left, a sale already lower) ``rate`` ==
	    ``list_rate`` and ``benefit`` is None. ``uses_left``: uses of the benefit left that day before
	    this line (None = no limit). ``notice``: why a member price was not given (naming the sales
	    that took the uses), or None.
	"""
	doc = _quote_doc(customer, on_date)
	if exclude_invoice:
		doc.name = exclude_invoice
		doc.doctype = "Sales Invoice" if frappe.db.exists("Sales Invoice", exclude_invoice) else "POS Invoice"
	stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
	row = frappe._dict(item_code=item_code, qty=flt(qty) or 1, uom=stock_uom, stock_uom=stock_uom,
		conversion_factor=1)
	res = price_line(doc, row, customer) if stock_uom else frappe._dict(list_rate=0.0, rate=0.0)
	return {"list_rate": flt(res.get("list_rate")), "rate": flt(res.get("rate")), "benefit": res.get("benefit"),
		"membership": res.get("membership"), "reason": res.get("reason"), "uses_left": res.get("uses_left"),
		"notice": res.get("notice")}


def benefit_usage(customer: str, benefit: str, on_date=None) -> dict:
	"""How much of a benefit *customer* has used on *on_date*.

	Args:
	    customer: The member.
	    benefit: A Membership Plan Benefit row name (``member_price``'s ``benefit``).
	    on_date: The day; blank = today.

	Returns:
	    {"limit": uses per day (0 = no limit), "used": stock units taken that day, "left": uses left, or
	    None with no limit}.
	"""
	limit = cint(frappe.db.get_value(BENEFIT, benefit, "uses_per_day"))
	used = used_on(benefit, customer, on_date or nowdate())
	return {"limit": limit, "used": used, "left": max(0.0, limit - used) if limit else None}


def membership_badge(customer: str | None) -> dict | None:
	"""One line for a counter or a check-in board: the member's plans, their end and today's limited
	benefits, e.g. "Premium · to 2027-04 · Range 1/2 today".

	Args:
	    customer: Customer name.

	Returns:
	    None for someone with no membership giving benefits today, else {"text", "memberships":
	    [{"membership", "plan", "status", "until", "uses": [{"benefit", "label", "used", "limit"}]}]}.
	"""
	today = getdate(nowdate())
	live = _live_memberships(customer, today)
	if not live:
		return None
	cache: dict = {}
	held = _benefits(customer, today, cache)
	parts, out = [], []
	for m in live:
		uses = []
		for b, owner in held:
			days = parse_days(b.days)
			if owner.name != m.name or not cint(b.uses_per_day) or (days is not None and today.weekday() not in days):
				continue
			label = (b.label or "").strip() or b.item or b.item_group or b.price_list
			uses.append({"benefit": b.name, "label": label, "used": used_on(b.name, customer, today),
				"limit": cint(b.uses_per_day)})
		until = m.current_period_end
		bits = [m.plan_name]
		if m.status != "Active":
			bits.append(_("past due"))
		if until:
			bits.append(_("to {0}").format(str(getdate(until))[:7]))
		bits += [_("{0} {1}/{2} today").format(u["label"], f"{u['used']:g}", u["limit"]) for u in uses]
		parts.append(" · ".join(bits))
		out.append({"membership": m.name, "plan": m.plan, "status": m.status,
			"until": str(until) if until else None, "uses": uses})
	return {"text": "; ".join(parts), "memberships": out}


@frappe.whitelist()
def preview(doc) -> dict:
	"""The member prices a save would give a register sale, for the screen to show at once.

	Args:
	    doc: The POS Invoice / Sales Invoice as JSON, as on the screen.

	Returns:
	    {"rows": {line name: {"rate", "price_list_rate", "discount_percentage", "discount_amount",
	    "ffl_member_benefit", "ffl_membership", "ffl_benefit_customer", "ffl_member_rate",
	    "ffl_discount_reason"}}, "notices": [str]}. The save works it out again; nothing is stored.
	"""
	from ffl_core.roles import COUNTER_ROLES

	frappe.only_for(COUNTER_ROLES)
	d = frappe.get_doc(frappe.parse_json(doc) if isinstance(doc, str) else doc)
	if d.doctype not in PRICED:
		frappe.throw(_("Only a sale is priced."))
	before = None
	if not d.get("__islocal") and d.name and frappe.db.exists(d.doctype, d.name):
		frappe.has_permission(d.doctype, "write", doc=d.name, throw=True)
		before = frappe.get_doc(d.doctype, d.name)
	else:
		frappe.has_permission(d.doctype, "create", throw=True)
	if d.docstatus != 0 or _skip(d):
		return {"rows": {}, "notices": []}
	d.flags = frappe._dict()   # never trusted from a screen
	found = reprice(d, before)
	fields = ("rate", "price_list_rate", "discount_percentage", "discount_amount", BENEFIT_FIELD, MEMBERSHIP_FIELD,
		BENEFICIARY_FIELD, MEMBER_RATE_FIELD, REASON_FIELD)
	return {"rows": {r.name: {f: r.get(f) for f in fields} for r in d.get("items") or [] if r.get("name")},
		"notices": found["notices"]}
