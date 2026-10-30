"""Materialize ffl_shipping_amount into a Sales Taxes and Charges "Actual" row.

Shipping is a service charge, not a stock item. The amount lives on the
ffl_shipping_amount custom field (the source of truth); this validate hook turns
it into exactly one "Actual" charge row on every validate.

It MUST run AFTER compliance.interstate_tax.apply_interstate_tax: that engine
does doc.set("taxes", ...) — a wholesale replacement of the taxes table — when it
swaps to the 0% Out-of-State template, which would otherwise drop the freight row
exactly on out-of-state shipments (a gun shipped to an out-of-state FFL — the
most common shipping case). Rebuilding from the field after the swap makes the
charge robust against that, make_sales_invoice copying, set_missing_values, and
the POS payment repin.

Freight tax (per home state:
California does not tax it, see
interstate_tax.FREIGHT_EXEMPT_HOME_STATES): Texas taxes
separately-stated delivery charges on taxable sales (Tax Code §151.007(a),
34 TAC §3.303, Pub 94-113). Right after the freight row this hook adds one
freight tax row per sales-tax template row that computed a POSITIVE goods tax
(freight follows the taxability of what it ships):
charge_type "Actual", amount hook-computed = that row's rate% × the FULL
freight, posted to the SAME liability account (freight tax lands
in the existing filing workpapers), description "Shipping Tax (<rate>%)".

Why "Actual" and not "On Previous Row Amount": the kernel computes
OPRA per item — the freight row's amount is first prorated across items by
net_amount, then each share passes the item's Item Tax Template rate override
on this same account. A 0% ITT line therefore silently prorates the freight
tax (3.54 instead of 3.71 on a mixed order), and a NEGATIVE 0% line
(TRADE-IN-CREDIT) blows the proration up entirely (6.19 — the gun line's
share exceeds the whole freight and the negative share's offset is zeroed
away). The hook-computed Actual amount is invariant to line structure and, as
a bonus, structurally immune to the tax-on-tax trap ("On Previous Row Total").
The kernel nulls `rate` and forbids row_id on Actual rows, so the rate lives
in the description text (traceability).

Zero goods tax — out-of-state empty template, exempt customer's cleared table,
or every line 0% via Item Tax Template — adds no freight tax row, keeping
"no goods tax → no freight tax" uniform across all four paths. Channel-mirrored
invoices (a web order, a GunBroker order) NEVER get freight tax rows: the
marketplace already taxed goods+freight at checkout and the channel's revenue
module mirrors that collected tax as an Actual row on the same account — the
guard runs BEFORE any identification or removal logic, so those docs are
zero-touch beyond the freight row itself (the
description prefix separates the two same-shaped Actual rows as second
insurance).

Tax-inclusive freight: a document with
ffl_shipping_tax_inclusive set treats ffl_shipping_amount as the customer's
TOTAL S with the sales tax already inside it. Each freight tax row is computed
from S directly — t_i = round_half_up(S × r_i / (100 + R), 2), R the summed
rate of the mirrored rows — and the freight (income) row takes S − Σt, so the
customer pays exactly S. Never derive the tax from the net (16.99 → 1.29; the
net route gives 1.30 and a 17.00 total). The flag is stamped on the document
at creation (manual_order, from FFL Settings) and copies with it, so an old
document keeps its old basis through amend/return; the same rule is unchanged
(no goods tax → no freight tax → the freight row is the whole S).
"""
from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe.utils import cint, flt

from ffl_core.compliance.channel_invoice import is_channel_mirrored_invoice
from ffl_core.compliance.interstate_tax import freight_taxable
from ffl_core.setup.shipping_charge import ensure_shipping_account, shipping_account_name

SHIPPING_DESCRIPTION = "Shipping"

# Freight tax rows are identified by all three features TOGETHER:
# description starts with this fixed prefix + the
# rate suffix rendered by _shipping_tax_description() + charge_type "Actual"
# + account_head within the sales-tax account set. Never by account_head
# alone — the row deliberately shares the sales-tax liability account with
# the template rows (and, on woo docs, with the Woo-collected tax row).
SHIPPING_TAX_DESCRIPTION = "Shipping Tax"

# erpnext.stock.get_item_details.NOT_APPLICABLE_TAX — kept as a literal so a
# core relocation of the constant cannot break this hook.
_NOT_APPLICABLE_TAX = "N/A"


def apply_shipping_charge(doc, method: str | None = None) -> bool:
	"""Returns True when it rebuilt the rows and recalculated the totals."""
	if int(getattr(doc, "is_return", 0) or 0) or int(getattr(doc, "is_consolidated", 0) or 0):
		return False
	company = getattr(doc, "company", None)
	if not company:
		return False
	# The freight row is identified by its dedicated account head (shipping-only).
	# Derive the name without a DB hit — this hook runs on EVERY SO/SI validate, so
	# the common no-shipping order must short-circuit before any query
	# (_freight_tax_rows below only queries when a candidate row exists).
	account = shipping_account_name(company)
	amount = flt(getattr(doc, "ffl_shipping_amount", 0))
	taxes = doc.get("taxes") or []
	existing = [t for t in taxes if t.account_head == account]

	# Channel guard FIRST, before any freight-tax identification or removal
	# a channel-mirrored invoice (web, GunBroker) is zero-touch beyond
	# the freight row — the marketplace taxed the freight itself.
	if is_channel_mirrored_invoice(doc):
		mirrors, desired = [], []
	else:
		mirrors = _freight_tax_rows(company, taxes)
		# A home state that doesn't tax freight (CA) gets no freight tax rows —
		# and a doc saved before that was known sheds the ones it carries.
		desired = (_mirrorable_tax_rows(doc, account)
			if amount > 0 and freight_taxable(company) else [])
	inclusive = cint(getattr(doc, "ffl_shipping_tax_inclusive", 0))
	freight_taxes = _freight_tax_amounts(desired, amount, inclusive)
	# The freight (income) row: the whole charge, or S − Σt when S includes the tax.
	freight = flt(amount - sum(freight_taxes), 2) if inclusive else amount

	if amount <= 0:
		if existing or mirrors:
			keep = [t for t in taxes if t.account_head != account and t not in mirrors]
			doc.set("taxes", keep)
			doc.calculate_taxes_and_totals()
			return True
		return False

	# Already exactly right → avoid a needless recompute (validate runs
	# repeatedly). "Exactly right" includes the freight TAX rows:
	# the freight row alone being right must not strand a pre-freight-tax document
	# with no freight tax row, nor block a rate-drift resync — the
	# short-circuit heals both on the next touch.
	if (len(existing) == 1 and existing[0].charge_type == "Actual"
			and flt(existing[0].tax_amount, 2) == flt(freight, 2)
			and _freight_taxes_exactly_right(mirrors, desired, freight_taxes)):
		return False

	# Posting a row → make sure the account actually exists (creates it once).
	account = ensure_shipping_account(company)
	cost_center = company_cost_center(company)

	doc.set("taxes", [t for t in taxes if t.account_head != account and t not in mirrors])
	doc.append("taxes", {
		"charge_type": "Actual",
		"account_head": account,
		"description": SHIPPING_DESCRIPTION,
		"tax_amount": freight,
		"cost_center": cost_center,
	})
	for src, tax_amount in zip(desired, freight_taxes):
		doc.append("taxes", {
			"charge_type": "Actual",
			"account_head": src.account_head,
			"description": _shipping_tax_description(src.rate),
			"tax_amount": tax_amount,
			"cost_center": src.cost_center,
			# CRITICAL, not cosmetic: v16's update_item_tax_map() rebuilds every
			# item's item_tax_rate map from the taxes table on each calculate —
			# LAST row per account_head wins, and the kernel nulls `rate` on
			# Actual rows. Without this flag the freight tax row (same account,
			# later position) overwrites the template row's rate with null →
			# flt(None)=0 → the GOODS tax computes 0.00. get_item_tax_map()
			# skips rows carrying this flag (get_item_details.py), which is
			# exactly its meaning: "not one of the sales-tax template's own
			# rows". Core only ever sets the flag, never prunes by it.
			"set_by_item_tax_template": 1,
			# Equally CRITICAL: keep this row out of the item-wise tax details
			# subsystem. The kernel distributes an Actual row per item by
			# net_amount, but adjust_rounding_in_item_wise_tax_details() DROPS
			# detail rows whose per-item rate resolves to 0 (an ITT-exempt
			# line) from the breakup sum while the row's expected amount stays
			# whole — on a trade-in/mixed doc the difference (e.g. -2.47 of
			# 3.71) exceeds the 0.5 tolerance and validation THROWS. With
			# dont_recompute_tax the row produces no item-wise details at all
			# (same treatment core gives tax-withholding and POS-merge rows —
			# an externally-computed amount), totals still include it.
			"dont_recompute_tax": 1,
		})
	# Renumber so idx always equals list position: kept rows may carry stale
	# idx values after the filter above, and a duplicate idx would scramble
	# the row order on the next load.
	for i, t in enumerate(doc.get("taxes"), 1):
		t.idx = i
	doc.calculate_taxes_and_totals()
	return True


def company_cost_center(company: str) -> str | None:
	"""The company's default cost center, else its first enabled leaf one.

	A company without a default cost center would save a charge row fine but
	FAIL at submit with a cryptic GL error (the income charge row needs a cost
	center). GunBroker's own charge rows use this too, so the rows on one
	invoice cannot disagree.
	"""
	return (frappe.db.get_value("Company", company, "cost_center")
		or frappe.db.get_value(
			"Cost Center", {"company": company, "is_group": 0, "disabled": 0},
			"name", order_by="lft"))


def shipping_tax_inclusive_setting() -> int:
	"""FFL Settings' "Shipping Price Includes Sales Tax" — ON when never saved."""
	from ffl_core.utils import single_flag
	return cint(single_flag("FFL Settings", "shipping_price_includes_tax", default=1))


def stamp_shipping_tax_basis(doc, method: str | None = None) -> None:
	"""before_insert (SO/SI): a NEW order takes the current setting as its
	freight tax basis — desk-built, API/MCP-built,
	and a desk Duplicate (which copied its source's value) alike. A document that
	continues another keeps the copied value: an amendment, a return, an invoice
	mapped from a Sales Order, a consolidated POS invoice. Channel mirrors keep
	0 — the channel taxed its own freight. create_order stamps the basis itself
	(update_order must keep the edited order's) and flags the doc."""
	if doc.flags.get("ffl_shipping_tax_basis_set"):
		return
	if (doc.get("amended_from") or cint(doc.get("is_return"))
			or cint(doc.get("is_consolidated"))
			or any(row.get("sales_order") for row in (doc.get("items") or []))):
		return
	doc.ffl_shipping_tax_inclusive = 0 if is_channel_mirrored_invoice(doc) \
		else shipping_tax_inclusive_setting()


def inclusive_shipping_lines(doc):
	"""Jinja (receipt): on a tax-inclusive document that carries freight tax,
	{"freight": idx of the freight row, "taxes": [idx of each freight tax row],
	"gross": the signed customer-paid shipping (freight + its tax — negative on a
	credit note)}; otherwise None and the receipt prints every row as-is. Rows
	are identified exactly as apply_shipping_charge identifies them: the freight
	row by its shipping account, the tax rows by the three-feature test."""
	if not cint(doc.get("ffl_shipping_tax_inclusive")) or not doc.get("company"):
		return None
	taxes = doc.get("taxes") or []
	account = shipping_account_name(doc.company)
	freight = [t for t in taxes if t.account_head == account]
	mirrors = _freight_tax_rows(doc.company, taxes)
	if len(freight) != 1 or not mirrors:
		return None
	return {
		"freight": freight[0].idx,
		"taxes": [m.idx for m in mirrors],
		"gross": flt(flt(freight[0].tax_amount) + sum(flt(m.tax_amount) for m in mirrors), 2),
	}


# ------------------------------------------------------------------ internals


def _shipping_tax_description(rate) -> str:
	""""Shipping Tax (8.25%)" — the identifying prefix plus the rate text:
	the kernel nulls `rate` on Actual rows, so the rate lives here for
	per-invoice traceability, and a rate drift shows up as a
	description mismatch in _freight_taxes_exactly_right."""
	return f"{SHIPPING_TAX_DESCRIPTION} ({flt(rate):g}%)"


def _freight_tax_amount(rate, amount) -> float:
	"""rate% × the FULL freight, rounded once — invariant to line structure
	(mixed orders tax the whole delivery charge, no proration)."""
	return flt(flt(rate) * amount / 100.0, 2)


def _freight_tax_amounts(desired, amount, inclusive) -> list[float]:
	"""The freight tax per desired row, in order. Inclusive: t_i =
	round_half_up(S × r_i / (100 + R), 2) straight from S
	— Decimal, because flt() rounds half-to-even here."""
	if not inclusive:
		return [_freight_tax_amount(d.rate, amount) for d in desired]
	gross = Decimal(str(flt(amount)))
	total = sum(Decimal(str(flt(d.rate))) for d in desired)
	return [float((gross * Decimal(str(flt(d.rate))) / (100 + total))
		.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) for d in desired]


def _freight_tax_rows(company, taxes) -> list:
	"""The freight tax rows already on the doc — all three features must
	match (prefix + Actual + a sales-tax account), so neither template rows
	nor the freight row nor a Woo-collected tax row can ever be mistaken for
	one. The account set is only resolved when a candidate exists, keeping
	the common no-freight-tax validate free of queries."""
	candidates = [t for t in taxes
		if t.charge_type == "Actual"
		and (t.description or "").startswith(SHIPPING_TAX_DESCRIPTION)]
	if not candidates:
		return []
	allowed = _sales_tax_accounts(company, taxes)
	return [t for t in candidates if t.account_head in allowed]


def _sales_tax_accounts(company, taxes) -> set:
	"""Third feature: the company default template's accounts (the single
	source setup/tax.py keys everything on), PLUS the accounts of this doc's
	own "On Net Total" rows — a non-default template (state+city split) may
	charge accounts the default one doesn't, and its freight tax rows must
	still be identifiable or a rebuild would duplicate them."""
	from ffl_core.setup.tax import _company_sales_tax_accounts
	accounts = set(_company_sales_tax_accounts(company))
	accounts.update(t.account_head for t in taxes
		if t.charge_type == "On Net Total" and t.account_head)
	return accounts


def _mirrorable_tax_rows(doc, shipping_account) -> list:
	"""The sales-tax rows the freight must follow:
	every "On Net Total" row (the "taxes the sale amount" charge type; Actual
	rows — the freight row itself, Woo-collected tax — are structurally out)
	whose goods tax is positive. Anchored on charge type + computed amount,
	never on a hard-coded rate or template name."""
	out = []
	for t in (doc.get("taxes") or []):
		if t.charge_type != "On Net Total":
			continue
		if t.account_head == shipping_account:
			continue  # hardening: never mirror anything posted to freight income
		if _unrounded_goods_tax(doc, t) > 0 or flt(t.tax_amount) > 0:
			out.append(t)
	return out


def _unrounded_goods_tax(doc, tax) -> float:
	"""The row's tax on the goods lines, UNROUNDED — recomputed from the same
	inputs core uses (item.net_amount × the Item-Tax-Template-resolved rate,
	see erpnext taxes_and_totals._get_tax_rate/_load_item_tax_rate; the
	item_tax_rate JSON is populated by validate_item_tax_template before this
	hook runs). The row's stored tax_amount is rounded to currency precision,
	which would drop a sub-cent goods tax to 0.00 and un-tax a large freight
	(ANY positive goods tax makes the freight taxable in full)."""
	total = 0.0
	for item in (doc.get("items") or []):
		total += _effective_rate(tax, item) / 100.0 * flt(item.net_amount)
	return total


def _effective_rate(tax, item) -> float:
	try:
		item_tax_map = json.loads(item.item_tax_rate) if item.get("item_tax_rate") else {}
	except Exception:
		item_tax_map = {}
	if tax.account_head in item_tax_map:
		rate = item_tax_map[tax.account_head]
		return 0.0 if rate == _NOT_APPLICABLE_TAX else flt(rate)
	return flt(tax.rate)


def _freight_taxes_exactly_right(mirrors, desired, amounts) -> bool:
	"""True iff the freight tax rows on the doc are exactly the desired set:
	same multiset of (account, description-with-rate-text, amount). A missing
	row (pre-freight-tax doc), a stale amount (freight changed), or a stale rate text
	(source row's rate changed, T12) all fail the check and trigger a rebuild
	— which only ever touches the freight row and the freight tax rows."""
	if len(mirrors) != len(desired):
		return False
	if not mirrors:
		return True
	want = sorted(
		(d.account_head, _shipping_tax_description(d.rate), amt, 1, 1)
		for d, amt in zip(desired, amounts))
	have = sorted(
		(m.account_head, m.description or "", flt(m.tax_amount, 2),
			# both kernel-exclusion flags must hold too (see the append above)
			# — a copy path that dropped them would silently zero the goods
			# tax / re-trip item-wise validation; failing here makes the
			# rebuild restore them
			int(bool(m.get("set_by_item_tax_template"))),
			int(bool(m.get("dont_recompute_tax"))))
		for m in mirrors)
	return want == have
