"""Sales Report — revenue + profit for a date range, across ALL sales channels.

In ERPNext, sales fan out across documents: counter POS sales are POS Invoices
(consolidated into a Sales Invoice at close), while Create Order produces
direct Sales Invoices. To count each sale exactly once — and still see
today's not-yet-consolidated POS sales — the source is the union of:

  - POS Invoice (docstatus=1)                         → channel "POS"
  - Sales Invoice (docstatus=1, is_consolidated=0)    → channel "Order"

The consolidated Sales Invoices (is_consolidated=1) are excluded: they are
roll-ups of POS Invoices already counted. So is a counter pickup's 4473 ticket
(a POS Invoice with ffl_counter_order set, ffl_core.api.counter_pickup): the
sale — price, serial and therefore cost — is the counter order's own line; the
ticket only hands the gun over at $0 and would report as a second, free gun.

No COGS is stored on invoice lines, so cost is computed per line:
  - trade-in credit → 0: the allowance is the acquisition cost of a gun taken
                    INTO inventory (an asset swap), not a cost of this sale —
                    and revenue is reported gross of the credit (below), so
                    the allowance touches neither side; the traded-in gun's
                    own cost is realized when IT later sells,
  - consignment gun → net × (1 − effective commission %), so profit on the
                    line equals the commission the store actually earns
                    (checked before the firearm branch below — a consigned
                    gun IS a firearm-with-serial, but its purchase_rate is 0),
  - firearm line  → Σ Serial No.purchase_rate of the line's serials (exact
                    per-gun cost; row flagged when a serial has no rate),
  - everything else → signed qty × Bin moving-average cost (the item's real
                    per-warehouse valuation; signed so returns reverse the
                    cost). The Item-master valuation_rate is only a fallback —
                    it is 0 for RSR-synced accessories (stock is received into
                    the Bin at real cost, but the master field is never synced),
                    which used to show a phantom 100% margin on accessory sales.

Revenue and profit are pre-tax and GROSS of trade-in credits:
Net Sales = base_net_total − Σ trade-in credit lines (a $1300 gun
sold against an $800 trade-in reports Net Sales 1300 / Cost = the gun's own
COGS — never 500 / COGS−800), and profit = gross net − cost, numerically equal
to the old net-method profit. The invoice itself still nets the credit (that
is what the customer owes and what the GL income account carries), so report
Net Sales exceeds GL income by exactly the period's allowances while profit
ties to the GL. The tax base (net of trade-in, TX §151.007(c)(3)) and the
Total column (grand_total actually charged) are untouched.

A ticket whose trade-ins exceed the goods goes NEGATIVE (compliance/negative_ticket):
the store pays the customer the difference on the same
ticket. Net Sales stays the goods (565), Tax is floored at 0 (never a refund),
and Total is the negative grand_total actually settled (-4,885) — the one place
Total may be below zero outside a return. Two figures sit beside it, both
positive: trade_in_applied = min(Σ credit, Σ taxable goods), the allowance that
actually came off the tax base, and trade_in_payout = max(0, -grand_total), the
money paid out. 565 - 5,450 + 0 = -4,885 ties Net Sales, trade_in_net, Tax and
Total on the row.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

from ffl_core import report_cards
from ffl_core.compliance.shipping_charge import SHIPPING_TAX_DESCRIPTION
from ffl_core.marketplace_accounts import (
	MARKETPLACE_FEE_ACCOUNTS, MARKETPLACE_TAX_ACCOUNTS, ORDER_DOCTYPE)
from ffl_core.setup.shipping_charge import shipping_account_name
from ffl_core.setup.tax import _company_sales_tax_accounts
from ffl_core.setup.gunsmith import DEPOSIT_ITEM
from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
from ffl_core.utils import split_serials

_POS = "POS Invoice"
_SI = "Sales Invoice"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	_validate(filters)
	# The Order view is the redesigned default: Order Ref / Serial columns
	# + 3-way channel + a period summary-card strip. The product-level views (CPA
	# tax reporting) are a separate code path (_execute_product_view). Both inner
	# builders return (columns, data); execute() wraps them in Frappe's native
	# 5-tuple below.
	view = filters.get("view") or "Order"
	if view in ("Order Detail", "Product"):
		columns, data = _execute_product_view(filters, detail=(view == "Order Detail"))
	else:
		columns, data = _execute_order_view(filters)
	# Frappe's native report return: (columns, data, message, chart, report_summary)
	# message = the above-datatable view helper / friendly empty
	# state; chart = None (not built yet); report_summary = the period
	# cards, assembled from the Total row + one flag scan — NO new accounting basis.
	message = _report_message(view, data, filters)
	report_summary = _report_summary(view, data, filters)
	return columns, data, message, None, report_summary


def _execute_order_view(filters):
	headers = _fetch_headers(filters)
	headers = _apply_channel_filter(headers, filters.get("channel"))
	lines = _fetch_lines(headers)
	item_meta = _fetch_item_meta(lines)
	serial_cost, consignment, transfer_serials = _fetch_serial_facts(lines)
	dropship_cost = _fetch_dropship_costs(headers, lines)
	# Tax rows only for the tickets that carry a trade-in credit: that is all trade_in_applied reads them for.
	credit_headers = [h for h in headers if any(
		l.get("item_code") == TRADE_IN_CREDIT_ITEM for l in lines.get(h["name"], []))]
	rows = _build_rows(
		headers, lines, item_meta, serial_cost, filters.get("product_type") or "", consignment,
		transfer_serials=transfer_serials, dropship_cost=dropship_cost,
		taxes_by_invoice=_fetch_taxes(credit_headers))
	return _columns(), rows


# The ONLY values the row builders branch on. Anything else used to fall through
# both branches and return EVERYTHING unfiltered: for a CPA, "the filter did
# not run" is indistinguishable from "there are no rows of that type", and the
# number gets used. So an unrecognised value is refused rather than silently ignored.
PRODUCT_TYPES = ("Firearms", "Non-Firearms")


def _validate(filters) -> None:
	if not filters.get("from_date") or not filters.get("to_date"):
		frappe.throw(_("From Date and To Date are required"))
	if getdate(filters.from_date) > getdate(filters.to_date):
		frappe.throw(_("From Date must be on or before To Date"))
	product_type = (str(filters.get("product_type") or "")).strip()
	# Write the normalised value back: the row builders compare it verbatim, so a
	# padded string would otherwise pass validation and then match no branch —
	# re-opening the very hole this check closes.
	filters["product_type"] = product_type
	if product_type and product_type not in PRODUCT_TYPES:
		frappe.throw(_(
			"Unknown product_type {0} — use one of: {1} (or leave it empty for all "
			"products). It is refused rather than ignored, so an unfiltered total is "
			"never mistaken for a filtered one."
		).format(product_type, ", ".join(PRODUCT_TYPES)))


def _columns():
	# Order view column CONTRACT: 13 columns in this order. Order Ref +
	# Serial added for look-alike disambiguation (two same-price same-item real orders);
	# Total moved to the END (the FE default-hides it
	# via the column picker — kept in the set so it can be un-hidden / CSV-exported,
	# per the always-full-CSV contract). The invoice column stays a Dynamic Link
	# keyed on voucher_type. FE owns the .js formatter + default-hidden seeding;
	# this is the server-side column set (order = render order).
	return [
		{"label": _("Date"), "fieldname": "posting_date", "fieldtype": "Date", "width": 95},
		{"label": _("Invoice"), "fieldname": "invoice", "fieldtype": "Dynamic Link", "options": "voucher_type", "width": 150},
		{"label": _("Order Ref"), "fieldname": "order_ref", "fieldtype": "Data", "width": 110},
		{"label": _("Channel"), "fieldname": "channel", "fieldtype": "Data", "width": 80},
		{"label": _("Customer"), "fieldname": "customer", "fieldtype": "Link", "options": "Customer", "width": 170},
		{"label": _("Serial"), "fieldname": "serials", "fieldtype": "Data", "width": 150},
		{"label": _("Category"), "fieldname": "category", "fieldtype": "Data", "width": 110},
		{"label": _("Qty"), "fieldname": "qty", "fieldtype": "Float", "width": 60, "precision": 0},
		{"label": _("Net Sales"), "fieldname": "net_sales", "fieldtype": "Currency", "width": 110},
		{"label": _("Cost"), "fieldname": "cost", "fieldtype": "Currency", "width": 110},
		{"label": _("Profit"), "fieldname": "profit", "fieldtype": "Currency", "width": 110},
		{"label": _("Margin %"), "fieldname": "margin", "fieldtype": "Percent", "width": 90},
		# Default-hidden on screen (sales_report.js SR_DEFAULT_HIDDEN)
		# and always in the CSV: the marketplace's cut and the profit left after
		# it. Placed after Margin so the goods figures stay adjacent.
		{"label": _("Channel Fees"), "fieldname": "channel_fees", "fieldtype": "Currency", "width": 105},
		{"label": _("Profit after Fees"), "fieldname": "profit_after_fees", "fieldtype": "Currency", "width": 125},
		# Trade-in settlement (negative-ticket settlement): default-hidden on screen,
		# always in the CSV. Both positive, 0 on an ordinary sale.
		{"label": _("Trade-in Applied"), "fieldname": "trade_in_applied", "fieldtype": "Currency", "width": 120},
		{"label": _("Trade-in Paid Out"), "fieldname": "trade_in_payout", "fieldtype": "Currency", "width": 125},
		{"label": _("Total"), "fieldname": "total", "fieldtype": "Currency", "width": 110},
	]


# ---------------------------------------------------------------- data fetch

# po_no is ERPNext's standard "Customer's Purchase Order"; ffl_woo_sync stamps it
# with the Woo storefront order number (revenue.py: si.po_no = order.woo_order_id),
# e.g. "2134". is_ffl_transfer is a POS-Invoice-ONLY custom marker (a counter 4473
# transfer of a customer's own gun — no merchandise sold). Both are fetched per
# doctype ONLY when the field actually exists there, so a field present on one
# document type but not the other (is_ffl_transfer isn't on Sales Invoice) is a
# safe no-op — every header ends up with the key defaulted, never a get_all crash.
_DROPSHIP_FIELD = "dropship_distributor"
# A ship-to-store line: the house that was asked to
# send the goods HERE. Deliberately a different field from the drop-ship one — the
# goods do enter our stock — but until they arrive the line has neither a Serial
# No nor a Bin figure to cost from, so it is priced from the same Distributor
# Order line the drop-ship branch reads (2026-09-16: two prod orders read cost 0).
_TO_STORE_FIELD = "ffl_to_store_distributor"
_HOUSE_FIELDS = (_DROPSHIP_FIELD, _TO_STORE_FIELD)
_OPTIONAL_HEADER_FIELDS = ("po_no", "is_ffl_transfer")

# GunBroker's three invoice-header stamps, all ffl_integrations custom fields.
#   gb_ship_to_state   the state the gun was actually shipped to — the Texas
#                      worksheet's only source. NOT derivable from the
#                      customer address: a GB gun ships to the buyer's FFL.
#   gb_final_value_fee / gb_ffl_doc_fee
#                      what the marketplace kept. Expense, never COGS.
# Every one is column-guarded for the same reason gunbroker_order is: they
# arrive with a migration, and this report is the store's daily read.
_GB_HEADER_FIELDS = ("gb_ship_to_state", "gb_final_value_fee", "gb_ffl_doc_fee")
_GB_ORDER_DOCTYPE = ORDER_DOCTYPE


def _has_column(doctype, fieldname):
	# DB-column truth (matches the codebase's patch convention, e.g.
	# frappe.db.has_column("POS Invoice", ...)): frappe.get_all reads real
	# columns, so this is the exact guarantee that adding `fieldname` to the
	# fetch can't raise on a doctype that lacks it.
	return frappe.db.has_column(doctype, fieldname)


def _fetch_headers(filters, extended=False):
	base = {
		"docstatus": 1,
		"posting_date": ["between", [filters.from_date, filters.to_date]],
	}
	fields = ["name", "posting_date", "customer", "company", "base_net_total", "grand_total",
		"rounded_total", "is_return"]
	pos_opt = [f for f in _OPTIONAL_HEADER_FIELDS if _has_column(_POS, f)]
	# A counter pickup's 4473 ticket is not a sale (see the module docstring).
	pos_base = ({**base, "ffl_counter_order": ("is", "not set")}
		if _has_column(_POS, "ffl_counter_order") else base)
	pos = frappe.get_all(_POS, filters=pos_base, fields=fields + pos_opt)
	for p in pos:
		p["voucher_type"], p["channel"] = _POS, "POS"
		# Neither channel marker exists on POS Invoice; both read falsy, so every
		# POS row lands on the POS channel.
		p["woo_online_order"] = None
		p["gunbroker_order"] = None
		if extended:
			p["exempt_from_sales_tax"] = 0
	# woo_online_order / exempt_from_sales_tax are custom fields on Sales Invoice
	# only. woo_online_order is ALWAYS fetched — it drives the 3-way channel filter
	# (POS/Web/Manual) in every view, including Order; exempt is only fetched
	# when a product view needs to display it.
	si_opt = [f for f in _OPTIONAL_HEADER_FIELDS if _has_column(_SI, f)]
	# gunbroker_order is column-guarded where woo_online_order is not, because
	# ffl_woo_sync has always been installed here while ffl_integrations' GunBroker
	# field arrives with a migration: fetching a column that does not exist yet
	# raises, and this report is the store's daily read.
	channel_fields = ["woo_online_order"] + (
		["gunbroker_order"] if _has_column(_SI, "gunbroker_order") else [])
	# The fee stamps are needed in EVERY view (the Order view is where profit
	# lives), the ship-to state only where the tax worksheet is built.
	gb_fields = [f for f in _GB_HEADER_FIELDS
		if (extended or f != "gb_ship_to_state") and _has_column(_SI, f)]
	si_fields = fields + channel_fields + gb_fields + si_opt + (
		["exempt_from_sales_tax"] if extended else [])
	si = frappe.get_all(_SI, filters={**base, "is_consolidated": 0}, fields=si_fields)
	for s in si:
		s["voucher_type"], s["channel"] = _SI, "Order"
	# Uniform read downstream: every header carries po_no / is_ffl_transfer (None
	# where the field is absent on that doctype, so .get() reads a falsy default).
	headers = pos + si
	for h in headers:
		for f in _OPTIONAL_HEADER_FIELDS + _GB_HEADER_FIELDS:
			h.setdefault(f, None)
	return headers


def _fetch_marketplace_exempt(headers):
	"""{GunBroker Order name: tax_exempt} for the period's marketplace orders.

	A GunBroker order shows $0 sales tax for two entirely different reasons —
	the BUYER holds an exemption, or the ship-to state has no sales tax — and
	the Texas worksheet has to be able to tell them apart. The flag
	only ever existed on the GunBroker Order; the invoice's own
	`exempt_from_sales_tax` is the store's certificate on file for a counter
	customer, a different fact that must not be overwritten with this one.

	Read by doctype NAME with a table guard, never by import: ffl_core runs on
	sites where ffl_integrations is absent (same contract as
	ffl_woo_sync.woocommerce.fulfillment's Distributor Order read)."""
	names = {h.get("gunbroker_order") for h in headers if h.get("gunbroker_order")}
	if not names or not frappe.db.table_exists(_GB_ORDER_DOCTYPE):
		return {}
	return {r["name"]: bool(r.get("tax_exempt")) for r in frappe.get_all(
		_GB_ORDER_DOCTYPE, filters={"name": ["in", sorted(names)]},
		fields=["name", "tax_exempt"])}


def _ship_to_state(header):
	"""The ship-to state as a USPS code, or the raw value when it is not one.

	GunBroker's `shipToState` arrives with no normalisation anywhere on the way
	in — `str(...).strip()` onto the order, copied verbatim onto the invoice —
	and no sample of what GunBroker actually sends exists. The repo's own
	normaliser (whose docstring notes that production addresses already mix
	"TX" and "Texas") is already used to read THIS field in
	`compliance/cr_transfer.py`, so comparing the raw string here would be the
	one place that does not.

	Getting it wrong is silent and one-directional: an unrecognised spelling
	drops the sale out of Texas Item 1, while Item 2 never had it either — a
	GunBroker order is $0 sales tax by design — so the sale appears in neither
	column of the return and nothing goes red.

	An unrecognisable value is returned AS IS rather than blanked: the CSV is
	the evidence the state wants kept for four years, and dirty data has to be
	visible in it. It simply will not match "TX".

	Imported inside the function, like every other caller of this helper, to
	keep the compliance package out of the daily report's import graph."""
	from ffl_core.compliance.interstate_tax import normalize_us_state

	raw = header.get("gb_ship_to_state") or ""
	return normalize_us_state(raw) or raw


def _site_home_state() -> str:
	"""The site company's home state (each store runs its own site)."""
	from ffl_core.compliance.interstate_tax import home_state

	return home_state(frappe.defaults.get_global_default("company"))


def _channel_fees(header):
	"""What the marketplace kept out of this sale.

	Deliberately NOT part of Cost: Cost is COGS, and the same gun has to cost
	the same whether it sold at the counter or on GunBroker — otherwise every
	channel comparison is measuring the fee twice. Zero on every other channel,
	including on a site where the fields do not exist at all."""
	return flt(header.get("gb_final_value_fee")) + flt(header.get("gb_ffl_doc_fee"))


def _fetch_lines(headers, extended=False):
	out: dict[str, list] = {}
	# Each line carries its invoice's date: whether a sold gun was consigned is a
	# fact of that sale, read differently before and after the commission freeze
	# (ffl_core.consignment.sold_on_consignment).
	posted = {h["name"]: h.get("posting_date") for h in headers}
	fields = ["parent", "item_code", "qty", "base_net_amount", "serial_no",
		"consignment_commission_pct", "item_tax_template"]
	# The product views also need the item row's own `name` (IWTD.item_row keys
	# off it), the display name, and the price columns. Order view asks for none.
	if extended:
		fields += ["name", "item_name", "base_net_rate", "price_list_rate", "discount_amount"]

	def collect(child_dt, names):
		if not names:
			return
		# `dropship_distributor` / `ffl_to_store_distributor` are custom fields that
		# exist on Sales Invoice Item ONLY — neither lane reaches a POS Invoice, and
		# asking for a column a doctype does not have raises. Same guard as
		# _OPTIONAL_HEADER_FIELDS, applied per child doctype.
		child_fields = fields + [f for f in _HOUSE_FIELDS if _has_column(child_dt, f)]
		for r in frappe.get_all(
			child_dt,
			filters={"parent": ["in", names]},
			fields=child_fields,
		):
			r["posting_date"] = posted.get(r["parent"])
			out.setdefault(r["parent"], []).append(r)

	collect("POS Invoice Item", [h["name"] for h in headers if h["voucher_type"] == _POS])
	collect("Sales Invoice Item", [h["name"] for h in headers if h["voucher_type"] == _SI])
	return out


def _resolve_valuation(bin_rows, master_rate):
	"""Per-item cost rate for the report's non-firearm lines. RSR-synced items
	carry valuation_rate 0 on the Item master (stock is received into the Bin at
	real cost, but the master field is never synced), so prefer the Bin's
	moving-average cost: the qty-weighted average when in stock, else the
	persisted Bin rate (Bin.valuation_rate holds the last moving average even at
	0 qty). Fall back to the Item-master rate only when there's no usable Bin
	figure (item never stocked)."""
	qty = sum(flt(b.get("actual_qty")) for b in bin_rows)
	val = sum(flt(b.get("stock_value")) for b in bin_rows)
	if qty > 0 and val > 0:
		return val / qty
	rate = max((flt(b.get("valuation_rate")) for b in bin_rows), default=0.0)
	return rate or flt(master_rate)


def _fetch_item_meta(lines_by_invoice):
	codes = {l["item_code"] for ls in lines_by_invoice.values() for l in ls if l.get("item_code")}
	if not codes:
		return {}
	bins: dict[str, list] = {}
	for b in frappe.get_all(
		"Bin", filters={"item_code": ["in", list(codes)]},
		fields=["item_code", "actual_qty", "stock_value", "valuation_rate"],
	):
		bins.setdefault(b["item_code"], []).append(b)
	return {
		it["name"]: {
			"is_firearm": it.get("is_firearm"),
			"valuation_rate": _resolve_valuation(bins.get(it["name"], []), it.get("valuation_rate")),
			"item_group": it.get("item_group"),
		}
		for it in frappe.get_all(
			"Item", filters={"name": ["in", list(codes)]},
			fields=["name", "is_firearm", "valuation_rate", "item_group"],
		)
	}


def _fetch_serial_facts(lines_by_invoice) -> tuple[dict, dict, set]:
	"""One Serial No read for the invoice lines' serials, returning
	``(serial_cost, consignment, transfer_serials)``:

	- serial_cost: ``{serial_no: purchase_rate}`` for every serial;
	- consignment: ``{serial_no: {"is_consignment": 1, "commission_pct": <serial default>}}``
	  for consigned serials ONLY (``_consigned`` tests membership);
	- transfer_serials: the serials that are CUSTOMER property held on an FFL
	  transfer (Serial No.is_transfer_gun). This is the real classification that
	  replaced the net-0 heuristic: the store never bought these guns, so their
	  cost is 0 by fact rather than by inference, on every view and regardless
	  of what the line's net came to.
	"""
	serials = {
		s
		for ls in lines_by_invoice.values()
		for l in ls
		for s in split_serials(l.get("serial_no"))
	}
	if not serials:
		return {}, {}, set()
	serial_cost, consignment, transfer_serials = {}, {}, set()
	for s in frappe.get_all(
		"Serial No", filters={"name": ["in", list(serials)]},
		fields=["name", "purchase_rate", "is_consignment", "consignment_commission_pct", "is_transfer_gun"],
	):
		serial_cost[s["name"]] = flt(s.get("purchase_rate"))
		if cint(s.get("is_consignment")):
			consignment[s["name"]] = {
				"is_consignment": 1, "commission_pct": flt(s.get("consignment_commission_pct"))}
		if cint(s.get("is_transfer_gun")):
			transfer_serials.add(s["name"])
	return serial_cost, consignment, transfer_serials


def _dropship_source_keys(headers):
	"""``{(source_doctype, source_name): invoice_name}`` a Distributor Order may
	point at for these invoices.

	A DO records its origin as a Dynamic Link in two shapes: the web flow links the
	**Woo Online Order** (which the SI also names through ``woo_online_order``),
	while the counter flow links the **Sales Invoice** itself. Both are offered so
	one Distributor Order lookup serves either, and the value carries the invoice
	each key resolves back to. This is the ONE builder — ``_fetch_dropship_costs``
	consumes it rather than re-deriving the same mapping (so the two cannot drift,
	and the tests exercise the code production runs).
	"""
	mapping = {}
	for h in headers or []:
		if h.get("voucher_type") != _SI:
			continue  # POS invoices carry no drop-ship line
		mapping.setdefault((_SI, h["name"]), h["name"])
		woo = h.get("woo_online_order")
		if woo:
			mapping.setdefault(("Woo Online Order", woo), h["name"])
	return mapping


def _line_house(line) -> str | None:
	"""The distributor a line is costed from: its drop-ship house, else its
	ship-to-store house. None for an ordinary stock line."""
	return line.get(_DROPSHIP_FIELD) or line.get(_TO_STORE_FIELD) or None


def _fetch_dropship_costs(headers, lines_by_invoice):
	"""{(invoice, item_code, distributor): acknowledged distributor cost},
	for drop-ship AND ship-to-store lines.

	Keyed by the house as well: one invoice can order the same SKU from two houses
	(a drop-ship leg and a ship-to-store leg), and a two-part key would hand each
	line the SUM of both orders.

	Two bulk queries and only when a distributor line actually appears, matching
	the other prefetches: never a per-row lookup inside the costing loop.
	"""
	invoices = {
		inv for inv, ls in (lines_by_invoice or {}).items()
		if any(_line_house(l) for l in ls)
	}
	if not invoices:
		return {}
	heads = [h for h in (headers or []) if h["name"] in invoices]
	invoice_by_key = _dropship_source_keys(heads)  # the one builder
	if not invoice_by_key:
		return {}

	orders = frappe.get_all(
		"Distributor Order",
		filters={
			"source_doctype": ["in", sorted({k[0] for k in invoice_by_key})],
			"source_name": ["in", sorted({k[1] for k in invoice_by_key})],
		},
		fields=["name", "source_doctype", "source_name", "distributor"],
	)
	invoice_by_order = {}
	for o in orders:
		inv = invoice_by_key.get((o.get("source_doctype"), o.get("source_name")))
		if inv:
			invoice_by_order[o["name"]] = (inv, o.get("distributor"))
	if not invoice_by_order:
		return {}

	out = {}
	for l in frappe.get_all(
		"Distributor Order Line",
		filters={"parent": ["in", list(invoice_by_order)]},
		fields=["parent", "item_code", "sku", "unit_cost", "ack_qty"],
	):
		invoice, house = invoice_by_order.get(l["parent"], (None, None))
		item_code = l.get("item_code") or l.get("sku")
		ack = int(l.get("ack_qty") or 0)
		rate = flt(l.get("unit_cost"))
		# ACKNOWLEDGED total (ack_qty × unit_cost) — the same figure the PI books, so
		# the report ties to COGS even on a short-ship. ack 0 ⇒ no key ⇒ the
		# line reports missing_cost (a real gap), never a phantom 0-cost margin.
		if invoice and item_code and ack > 0 and rate > 0:
			key = (invoice, item_code, house)
			out[key] = out.get(key, 0.0) + ack * rate
	return out


def _consigned(line, serials, consignment):
	"""The consignment facts for one sold line, or None when THAT sale was not a
	consignment sale (ffl_core.consignment.sold_on_consignment: the line's own
	frozen %, the Serial No stamp only for a sale older than the freeze — a later
	receipt of the same gun rewrites the stamp, and an old period run again must
	not re-cost its sales)."""
	from ffl_core.consignment import sold_on_consignment

	cmap = consignment or {}
	stamped = next((cmap[s] for s in serials if s in cmap), None)
	if not sold_on_consignment(line.get("consignment_commission_pct"), line.get("posting_date"), stamped):
		return None
	return stamped or {"is_consignment": 1, "commission_pct": 0.0}


# ----------------------------------------------------------- row construction

def _line_cost(line, item_meta, serial_cost, consignment=None, is_ffl_transfer=False,
		transfer_serials=None, dropship_cost=None):
	"""(cost, missing) for one invoice line. Trade-in credit → 0 (not a cost
	of this sale; the row builders report revenue gross of the credit, so the
	allowance stays off both sides); consignment gun → net × (1 − effective
	commission %); firearm → per-serial purchase_rate (sign follows qty so
	returns reverse it); else signed qty × valuation_rate. On an FFL-transfer
	invoice (is_ffl_transfer, header flag) a net-0 firearm line is NOT flagged
	missing_cost: it's the customer's OWN gun the store never bought, so a 0
	purchase_rate is expected, not a data gap (B)."""
	qty = flt(line.get("qty"))
	# A trade-in credit is non-cash consideration — the acquisition cost of a
	# gun taken INTO inventory (compliance locks rate == -acquisition.cost),
	# NOT a cost of THIS sale. It contributes nothing to cost, and the row
	# builders add the (negative) credit back out of net revenue, so neither
	# Net Sales nor Cost carries the allowance and profit is untouched by the
	# swap; the traded-in gun's own margin is realized when IT later sells
	# (its Serial No.purchase_rate == the allowance).
	if line.get("item_code") == TRADE_IN_CREDIT_ITEM:
		return 0.0, False
	meta = item_meta.get(line.get("item_code"), {})
	serials = split_serials(line.get("serial_no"))
	# A consignment gun IS a firearm-with-serial (Serial No.purchase_rate == 0
	# by design — the store never bought it), so this must be checked BEFORE
	# the firearm branch below or that branch would win with cost 0 and show a
	# phantom 100% margin. cost = net * (1 - commission%) makes profit on the
	# line equal exactly the commission the store earns.
	consigned = _consigned(line, serials, consignment)
	if consigned:
		# Effective commission %: the frozen line value when it's a REAL rate
		# (> 0), else the Serial No's rate. A line % of 0 is UNSET, not a real 0%
		# override — on a sale that predates this feature, or a retro-flagged
		# consignment gun whose old invoice line was never frozen, fall back to
		# the serial's rate (otherwise the report would show cost = net / profit 0
		# while the GL carries the real commission). Mirrors
		# ffl_core.consignment.effective_commission_pct exactly. A genuine 0%
		# consignment is NOT expressible: validate_consignment_serial backfills a
		# 0/blank Serial No rate to the store default, and the freeze hook throws
		# on a resolved <= 0 — a 0%-commission sale does not exist.
		line_pct = flt(line.get("consignment_commission_pct"))
		pct = line_pct if line_pct > 0 else flt(consigned.get("commission_pct"))
		return flt(line.get("base_net_amount")) * (1 - pct / 100.0), False
	# Drop-ship. The goods never entered our inventory, so
	# BOTH branches below are wrong for this line: an FDS gun has no local Serial
	# No to read a purchase_rate from (the serial RSR reports is a record,
	# not a stock document), and a SKU that never sat in our warehouse has no Bin
	# moving average. Each would return 0 and paint a phantom 100% margin; on a
	# SKU we ALSO stock locally, the Bin branch is worse still — it would price a
	# drop-shipped unit at the cost of different goods.
	#
	# Checked BEFORE the firearm branch for exactly the reason the consignment
	# branch is: whichever runs first wins, and that one would win with cost 0.
	#
	# The cost is the distributor order line's acknowledged unit cost — the same
	# number the cost Purchase Invoice books, so margin here and COGS in the GL
	# can never tell two stories. No ack cost yet is a REAL gap worth flagging;
	# it is not the false "missing cost" a failed local-serial lookup produces.
	#
	# A ship-to-store line (P2) reads the same order line while its goods are on
	# the way: the SKU has no Serial No yet and no Bin history to average, so both
	# branches below would price it at 0 — the two prod orders of 2026-09-16. Once
	# a gun has ARRIVED it carries a serial and the firearm branch prices it from
	# the Purchase Receipt, which booked the same order-line cost; an
	# accessory keeps the order-line figure for good, since its Bin average mixes
	# in every other lot of that SKU.
	house = _line_house(line)
	is_landed_gun = bool(meta.get("is_firearm") and serials)
	if house and (line.get(_DROPSHIP_FIELD) or not is_landed_gun):
		# M5: the value is the distributor order's ACKNOWLEDGED total (ack_qty ×
		# unit_cost), NOT a unit rate scaled by the SI's sold quantity — RSR can
		# short-ship (ack < ordered), and the PI books the ack. Multiplying the sold
		# qty would over-count cost the moment ack ≠ sold and break the tie-out.
		# Only the sign follows qty, so a return reverses cleanly.
		total = (dropship_cost or {}).get((line.get("parent"), line.get("item_code"), house))
		if total is None:
			return 0.0, True  # no ack yet — a REAL data gap, not a fake one
		return (1 if qty >= 0 else -1) * flt(total), False
	if meta.get("is_firearm") and serials:
		missing = False
		total = 0.0
		xfer = transfer_serials or set()
		for sn in serials:
			# A customer's transfer gun contributes NOTHING and is not a data
			# gap. This is the real classification — the gun's own flag —
			# and it replaces the inference below: cost is 0 because the store
			# never bought this gun, not because the line happened to net 0.
			#
			# It also fixes the arithmetic the heuristic could not. Before the
			# custody rework, a transfer received at a $10 placeholder left
			# purchase_rate=10 on the serial, so a net-0 gun line reported
			# cost 10 → profit −$10, and the invoice's transfer fee showed
			# $10 short of what it earned.
			# Skipping the serial outright makes both correct.
			if sn in xfer:
				continue
			rate = flt(serial_cost.get(sn))
			if rate <= 0:
				missing = True
			total += rate
		# Legacy fallback, deliberately KEPT for history: guns transferred out
		# before is_transfer_gun existed carry no flag and cannot be
		# retroactively classified (we do not backfill already-delivered
		# guns). For those rows the old net-0 inference is still the only signal
		# there is, so removing it would re-flag every historical transfer as a
		# missing-cost error. New rows never reach it — their serials are caught
		# above.
		if is_ffl_transfer and flt(line.get("base_net_amount")) == 0:
			missing = False
		return (1 if qty >= 0 else -1) * total, missing
	return qty * flt(meta.get("valuation_rate")), False


def _line_taxable(line, company, sales_tax_accounts) -> bool:
	"""Does this line carry sales tax on THIS ticket? Asked the way the ticket's tax floor asks it
	(compliance/negative_trade_in._tax_at): ERPNext's get_item_tax_map for the line's Item Tax Template against the
	ticket's own sales-tax account(s) — taxable when any of them is charged on the line (not 0%, not marked not
	applicable). NOT the row's stored ``item_tax_rate``: the floor sets the ticket's tax rate to 0 and ERPNext then
	rewrites every row's ``item_tax_rate`` to 0, so on exactly the ticket this figure is for, the gun would read as
	exempt. No template means the tax rows' own rate applies: taxable. A template that never mentions the ticket's
	account leaves that account at the row rate (taxable); one that marks it not applicable or 0% exempts the line."""
	template = line.get("item_tax_template")
	if not template or not sales_tax_accounts:
		return True
	from erpnext.stock.get_item_details import NOT_APPLICABLE_TAX, get_item_tax_map

	# rate 1 stands in for "the ticket's rate": only what the template does to it matters (0 / not applicable).
	tax_map = get_item_tax_map(doc={"company": company, "taxes": [{"account_head": a, "rate": 1} for a in sales_tax_accounts]},
		tax_template=template, as_json=False)
	return any(tax_map.get(a) != NOT_APPLICABLE_TAX and flt(tax_map.get(a)) != 0 for a in sales_tax_accounts)


def _trade_in_settlement(header, lines, trade_in_net, tax_rows) -> tuple[float, float]:
	"""(trade_in_applied, trade_in_payout) of one invoice — both positive, (0, 0) when
	there is no trade-in credit or the invoice is a return.

	``trade_in_net`` is the credit lines' net (negative), already summed by the caller.
	applied = min(Σ credit, Σ taxable goods): the part of the allowance that came off
	the sales-tax base (TX §151.007(c)(3)); the rest has no tax effect. Freight is not
	in the base here (the rule names goods). payout = max(0, -total), the total
	being rounded_total, else grand_total — what the server pays out
	(compliance/negative_trade_in.invoice_total): what the store paid the customer
	on a ticket that went negative."""
	credit = -trade_in_net
	if credit <= 0 or header.get("is_return"):
		return 0.0, 0.0
	accounts = [t.get("account_head") for t in tax_rows or [] if t.get("charge_type") == "On Net Total"]
	goods = sum(flt(l.get("base_net_amount")) for l in lines
		if l.get("item_code") not in (TRADE_IN_CREDIT_ITEM, DEPOSIT_ITEM)
		and flt(l.get("base_net_amount")) > 0 and _line_taxable(l, header.get("company"), accounts))
	return min(credit, goods), max(0.0, -(flt(header.get("rounded_total")) or flt(header.get("grand_total"))))


def _order_facts(h, lines, item_meta) -> dict:
	"""The order-level facts the Order view (_build_rows) and the Order Detail
	parent row (_build_product_rows) must agree on: one definition of each.

	Firearm flag drives the Firearms/Non-Firearms filter; the Category column
	shows the line's real Item Group ("Mixed" for the rare invoice spanning more
	than one group).

	trade_in_net is the (negative) trade-in credit lines' net: an inventory
	purchase, not negative revenue, so Net Sales is reported gross of it.

	A gunsmith deposit row is customer money held, never revenue: the collecting
	invoice is all deposit (+100 → Net 0) and the pick-up invoice releases it
	(−100 → the work's full amount). deposit_net comes out of the net, the same
	way a trade-in allowance does.

	order_ref: Woo storefront order # ("2134"), "" for a POS/counter sale; falls
	back to the WOO-ORD doc name, then the GunBroker order, when po_no is blank.
	"""
	groups = sorted({
		item_meta.get(l.get("item_code"), {}).get("item_group")
		for l in lines if item_meta.get(l.get("item_code"), {}).get("item_group")
	})
	return {
		"is_firearm": any(item_meta.get(l.get("item_code"), {}).get("is_firearm") for l in lines),
		"groups": groups,
		"category": groups[0] if len(groups) == 1 else ("Mixed" if groups else ""),
		"trade_in_net": sum(
			flt(l.get("base_net_amount")) for l in lines
			if l.get("item_code") == TRADE_IN_CREDIT_ITEM),
		"deposit_net": sum(
			flt(l.get("base_net_amount")) for l in lines
			if l.get("item_code") == DEPOSIT_ITEM),
		"order_ref": h.get("po_no") or h.get("woo_online_order") or h.get("gunbroker_order") or "",
	}


def _build_rows(headers, lines_by_invoice, item_meta, serial_cost, product_type, consignment=None,
		transfer_serials=None, dropship_cost=None, taxes_by_invoice=None):
	rows = []
	tot_net = tot_total = tot_cost = tot_profit = tot_fees = 0.0
	tot_applied = tot_payout = 0.0
	for h in sorted(headers, key=lambda x: (str(x.get("posting_date") or ""), x.get("name") or "")):
		lines = lines_by_invoice.get(h["name"], [])
		fees = _channel_fees(h)
		f = _order_facts(h, lines, item_meta)
		if product_type == "Firearms" and not f["is_firearm"]:
			continue
		if product_type == "Non-Firearms" and f["is_firearm"]:
			continue

		qty = cost = 0.0
		missing = False
		serials = []
		for line in lines:
			qty += flt(line.get("qty"))
			line_cost, line_missing = _line_cost(line, item_meta, serial_cost, consignment,
				is_ffl_transfer=h.get("is_ffl_transfer"),
				transfer_serials=transfer_serials, dropship_cost=dropship_cost)
			cost += line_cost
			missing = missing or line_missing
			serials.extend(split_serials(line.get("serial_no")))

		# Net Sales / Cost are GROSS of trade-in: the
		# (negative) credit line is an inventory purchase, not negative revenue,
		# so it is added back out of the invoice net — a $1300 gun sold against
		# an $800 trade-in shows Net Sales 1300 / Cost = the gun's own COGS
		# (_line_cost prices the credit line at 0), never 500 / COGS−800.
		# Profit is numerically unchanged by the gross-up; margin keeps its
		# gross base (a 15% gun would read ~44% on the netted figure). The
		# Total column stays grand_total — what was actually charged after the
		# credit — and the GL income account still nets the credit, so report
		# Net Sales exceeds GL income by exactly the allowance. A gunsmith
		# deposit comes out of the net the same way (_order_facts).
		trade_in_net = f["trade_in_net"]
		net = flt(h.get("base_net_total")) - trade_in_net - f["deposit_net"]
		total = flt(h.get("grand_total"))
		profit = net - cost
		applied, payout = _trade_in_settlement(h, lines, trade_in_net, (taxes_by_invoice or {}).get(h["name"]))
		rows.append({
			"posting_date": h.get("posting_date"),
			"invoice": h.get("name"),
			"voucher_type": h.get("voucher_type"),
			# Order view channel is the 3-way POS/Web/Manual (was the
			# literal "Order" for every direct SI), matching the Product view — the
			# same _channel() the channel filter already uses. Value only, no accounting-basis change.
			"channel": _channel(h.get("voucher_type"), h.get("woo_online_order"),
				h.get("gunbroker_order")),
			"customer": h.get("customer"),
			"category": f["category"],
			"qty": qty,
			"net_sales": net,
			"total": total,
			"cost": cost,
			"profit": profit,
			"margin": (profit / net * 100) if net else 0.0,
			"missing_cost": missing,
			# C: disambiguation / redesign data for the Order view's new
			# columns (final presentation is the FE's call; BE only
			# guarantees the data is on the row).
			#   order_ref  Woo storefront order # ("2134"), "" for a POS/counter sale
			#              (FE renders "#2134" / "—"); falls back to the WOO-ORD doc
			#              name when po_no is blank.
			#   serials    the invoice's serials aggregated across its lines, "; "-joined.
			#   category_detail  sorted item groups ", "-joined — the Mixed tooltip
			#              source; a single-group invoice repeats its one group.
			#   is_consignment   any consigned line → the ▲ profit marker.
			"order_ref": f["order_ref"],
			"serials": "; ".join(serials),
			"category_detail": ", ".join(f["groups"]),
			"is_consignment": any(_consigned(l, split_serials(l.get("serial_no")), consignment) for l in lines),
			# Data-only trade-in keys (no column): net_sales is gross while Total
			# stays the charged amount, so a trade-in row is the one place
			# net_sales > total — the FE marks its Net Sales cell (◐) and names
			# the allowance, instead of leaving an unexplained gap.
			"is_trade_in": bool(trade_in_net),
			"trade_in_net": trade_in_net,
			# Real columns (CSV-visible), default-hidden: what the allowance took off
			# the tax base, and what a negative ticket paid the customer.
			"trade_in_applied": applied,
			"trade_in_payout": payout,
			# Data-only (no column): a return / credit note. The Overview counts sales without them.
			"is_return": bool(h.get("is_return")),
			# The marketplace's cut, alongside — never inside — Cost.
			# profit_after_fees is what the channel actually earned; profit is
			# what the goods earned, and the two agree everywhere else.
			"channel_fees": fees,
			"profit_after_fees": profit - fees,
		})
		tot_net += net
		tot_total += total
		tot_cost += cost
		tot_profit += profit
		tot_fees += fees
		tot_applied += applied
		tot_payout += payout

	if rows:
		rows.append({
			"posting_date": None,
			"invoice": _("Total"),
			"channel": "",
			"customer": "",
			"category": "",
			"qty": None,
			"net_sales": tot_net,
			"total": tot_total,
			"trade_in_applied": tot_applied,
			"trade_in_payout": tot_payout,
			"cost": tot_cost,
			"profit": tot_profit,
			# tot_net is already gross of trade-in (each row's net is), so the
			# period margin needs no add-back.
			"margin": (tot_profit / tot_net * 100) if tot_net else 0.0,
			"channel_fees": tot_fees,
			"profit_after_fees": tot_profit - tot_fees,
			"is_total": True,
		})
	return rows


# ------------------------------------------- period summary + message

def _report_message(view, data, filters):
	"""Above-datatable hint (Frappe `message`, HTML): the view-purpose helper
 when there are rows, or a friendly empty-state when the
	period has no sales — never a bare "Nothing to show". Wrapped in a muted span
	so it reads as a low-key hint. The flag legend is the FE's job (.js);
	this carries only the one-line purpose / empty text."""
	if not data:
		body = _(
			"No sales in {0}–{1} for these filters. Try a wider date range or "
			"clear Channel / Product Type."
		).format(filters.get("from_date") or "", filters.get("to_date") or "")
	else:
		helps = {
			"Order": _("One row per sale across all channels. Flags understated cost."),
			"Order Detail": _("Open ▸ Lines for the product, shipping and tax lines."),
			"Product": _("Flat one-row-per-line table for the tax CSV. Exports all columns."),
		}
		body = helps.get(view, helps["Order"])
	return '<span class="text-muted">{0}</span>'.format(body)


# The card builders live in ffl_core.report_cards, shared with the month-end
# reports. What matters is not the dict shape — that is Frappe's — but
# the conventions: "✓ Clean" in green for a zero flag, red for anything else.
# Two copies of that are two conventions the first time one is edited.
_card = report_cards.card
_flag_card = report_cards.flag_card


def _report_summary(view, data, filters=None):
	"""Frappe report_summary cards: ONLY the Total row's existing figures
	+ a flag scan — introduces NO new accounting basis. Order view = 5 cards; Order
	Detail / Product = 7 (Sales Tax + Shipping inserted after Margin), plus the
	Texas marketplace card when the period has been narrowed to one marketplace.
	An empty period → zero-value cards + "✓ Clean", so no-sales reads as
	"$0", not a broken report.

	`filters` is optional so every existing 2-argument caller keeps working."""
	total = next((r for r in data if r.get("is_total")), {})
	body = [r for r in data if not r.get("is_total")]
	channel = (filters or {}).get("channel")

	if view in ("Order Detail", "Product"):
		# The Net Sales card is GROSS of trade-in — the same figure the Order
		# view's card shows. net_amount (net of the allowance) stays on the
		# Totals row / CSV for the tax worksheet; it is just not the headline number.
		net = flt(total.get("gross_merch"))
		cost = flt(total.get("cost"))
		profit = flt(total.get("profit"))
		gross = flt(total.get("gross_merch"))
		# Margin uses the SAME formula as the Order view (profit / gross-of-trade-in).
		# The product Total row stores gross_merch but not the ratio, so recompute it
		# from those very numbers — not a new accounting basis.
		margin = (profit / gross * 100) if gross else 0.0
		# Data Flags = missing_cost product rows + tax_mismatch ORDERS (distinct
		# invoices, so two flagged lines of one order count once) + unclassified rows.
		n_missing = sum(1 for r in body if r.get("row_kind") == "product" and r.get("missing_cost"))
		n_mismatch = len({r.get("invoice") for r in body if r.get("tax_mismatch")})
		n_uncl = sum(1 for r in body if r.get("row_kind") == "unclassified")
		cards = [
			_card(net, _("Net Sales"), "Currency", "Blue"),
			_card(cost, _("Cost"), "Currency", "Grey"),
			_card(profit, _("Goods Profit"), "Currency", "Green" if profit >= 0 else "Red"),
			_card(margin, _("Margin %"), "Percent", "Green" if margin >= 0 else "Red"),
			_card(flt(total.get("sales_tax")), _("Sales Tax"), "Currency", "Grey"),
			_card(flt(total.get("shipping")), _("Shipping"), "Currency", "Grey"),
		]
		# Shown ONLY on a run narrowed to the marketplace: on an
		# all-channel run the same card would sit next to the store's own Texas
		# sales and read as "our Texas total", which is the opposite of what
		# Item 1 wants — and the figure the return needs is the one line the
		# sales-tax column can never show, because a GB order is $0 there.
		if channel == "GunBroker":
			cards.append(_card(flt(total.get("marketplace_tx")),
				_("Marketplace sales ({0})").format(total.get("marketplace_state")
					or _site_home_state()),
				"Currency", "Blue"))
		cards.append(_flag_card(n_missing + n_mismatch + n_uncl, _("Data Flags")))
		return cards

	# Order view (5 cards) — reads the Order Total row's own net_sales / margin.
	profit = flt(total.get("profit"))
	margin = flt(total.get("margin"))
	n_flags = sum(1 for r in body if r.get("missing_cost"))
	return [
		_card(flt(total.get("net_sales")), _("Net Sales"), "Currency", "Blue"),
		_card(flt(total.get("cost")), _("Cost"), "Currency", "Grey"),
		_card(profit, _("Goods Profit"), "Currency", "Green" if profit >= 0 else "Red"),
		_card(margin, _("Margin %"), "Percent", "Green" if margin >= 0 else "Red"),
		_flag_card(n_flags, _("Cost Flags")),
	]


# =============================================================== product views
# CPA product-level tax reporting. Fully independent of the Order view above:
# adds per-line tax (read from the item_wise_tax_details child table ERPNext
# already fills, NEVER re-prorated by net share), an order-level Shipping row,
# and a fail-closed "unclassified charge" row for any tax account that is
# neither sales tax nor shipping. Cost reuses _line_cost unchanged.

_SALES_TAX, _SHIPPING, _UNCLASSIFIED = "sales_tax", "shipping", "unclassified"
_MARKETPLACE_TAX, _MARKETPLACE_FEE = "marketplace_tax", "marketplace_fee"

# Liabilities a marketplace collects THROUGH us — matched on the BARE account
# name, without the company suffix. The names live in ffl_core.marketplace_accounts
# (shared with the month-end schedules) rather than being looked up in the
# integration app: ffl_core has to run with ffl_integrations absent, and
# importing it here would put the daily sales report behind an optional
# dependency. Both directions are pinned from the integration side by
# ffl_integrations/tests/test_gunbroker_report_contract.py.
_MARKETPLACE_TAX_ACCOUNTS = MARKETPLACE_TAX_ACCOUNTS
_MARKETPLACE_FEE_ACCOUNTS = MARKETPLACE_FEE_ACCOUNTS


def _classify_account(account_head, shipping_account, sales_tax_accounts,
		account_name=None):
	"""Positively classify a taxes-row account_head against the company's OWN
	canonical accounts: the shipping-income account
	(ffl_core.setup.shipping_charge.shipping_account_name — the very account the
	shipping row is booked to) and the sales-tax template's account_heads
	(ffl_core.setup.tax._company_sales_tax_accounts). Anything else fails closed
	→ unclassified. This is deliberately NOT type-based (account_type=="Tax" /
	root_type=="Income"), which fails OPEN: a Tax-type federal excise tax would be
	swept into collected sales tax, and an Income-type handling/restocking fee
	into shipping. Only the resolved account_heads are trusted."""
	if account_head == shipping_account:
		return _SHIPPING
	if account_head in sales_tax_accounts:
		return _SALES_TAX
	if account_name in _MARKETPLACE_TAX_ACCOUNTS:
		return _MARKETPLACE_TAX
	if account_name in _MARKETPLACE_FEE_ACCOUNTS:
		return _MARKETPLACE_FEE
	return _UNCLASSIFIED


def _is_freight_tax_row(tax_row) -> bool:
	"""The POS freight-tax rows compliance.shipping_charge appends — the
	same identification triple the hook itself owns: charge_type "Actual" + the
	fixed "Shipping Tax" description prefix (the account-set membership is the
	caller's _SALES_TAX classification). They carry no item_wise_tax_details
	(dont_recompute_tax), so the report sums them order-level like shipping and
	exempts exactly them from the IWTD-vs-charged mismatch probe. Web-order
	Woo mirror rows are Actual on the same account but never carry this prefix
	(and the woo guard means the two can't co-exist on one document)."""
	return (tax_row.get("charge_type") == "Actual"
		and (tax_row.get("description") or "").startswith(SHIPPING_TAX_DESCRIPTION))


def _channel(voucher_type, woo_online_order, gunbroker_order=None):
	"""POS (counter) / Web / GunBroker / Manual (a direct SI, no marker).

	`gunbroker_order` defaults to None so a caller on a site whose Sales Invoice
	has no such column — this report runs with ffl_integrations absent — reads
	exactly as it did before."""
	if voucher_type == _POS:
		return "POS"
	if gunbroker_order:
		return "GunBroker"
	return "Web" if woo_online_order else "Manual"


def _apply_channel_filter(headers, channel):
	"""Filter headers by the 3-way channel (POS / Web / Manual), uniformly for
	every view. Empty = all. Two retired values stay as aliases so old saved
	filters / deep links don't silently return nothing: "Order" (the 2-way
	filter's value for all direct Sales Invoices, Web + Manual) and "B2B-Manual"
	(Manual's name before 2026-08-30)."""
	if not channel:
		return headers
	if channel == "Order":
		return [h for h in headers if h.get("voucher_type") == _SI]
	if channel == "B2B-Manual":
		channel = "Manual"  # the channel's name until 2026-08-30; saved filters keep it
	return [h for h in headers
		if _channel(h.get("voucher_type"), h.get("woo_online_order"),
			h.get("gunbroker_order")) == channel]


def _tax_breakdown(taxes_rows, iwt_rows, acct_class):
	"""Split one invoice's taxes into per-line sales tax, order-level shipping and
	freight-tax totals, and any unclassified charges — keyed on each taxes row's
	account class.

	  line_tax          {item_row_name: Σ sales tax allocated to that line}
	  freight_tax_total Σ freight-tax rows (freight-tax hook: Actual on the sales-tax
	                    account, "Shipping Tax" description prefix). Order-level
	                    like shipping — dont_recompute_tax rows carry NO IWTD.
	  sales_tax_total   the order's sales tax = Σ line_tax (Σ IWTD) + freight tax,
	                    post-discount — ties to the GL sales-tax account
	  shipping_total    Σ shipping rows (order-level, NOT split per item)
	  marketplace_tax_total / marketplace_fee_total
	                    liabilities a marketplace collected through us — the
	                    facilitator's sales tax and the buyer's service fee.
	                    Reported separately: neither is our revenue, neither is
	                    tax we owe a state, and neither is unclassified.
	  unclassified      [{account_head, amount}] surfaced explicitly, never as tax
	  tax_mismatch      True when Σ IWTD ≠ the charged post-discount sales tax
	                    EXCLUDING freight-tax rows (audit): freight rows have no
	                    IWTD by design, so exempting exactly them keeps every
	                    freight-taxed order clean while a tampered goods-tax row
	                    or a manually keyed Actual tax still flags

	Currency & discount: all amounts are COMPANY (base) currency.
	item_wise_tax_details.amount is company-currency (options
	Company:company:default_currency), matching base_net_amount, so the per-order
	reconciliation (Σ net + shipping + tax == grand_total) holds (this store is single-
	currency USD: base == transaction). Sales tax is taken from Σ IWTD, NOT the
	taxes row's base_tax_amount — that field is pre-discount and a grand-total
	discount leaves it stale (e.g. base 57.67 vs true 51.90).
	Shipping / unclassified (which have no per-line IWTD to sum) read
	base_tax_amount_after_discount_amount, the post-discount figure.
	"""
	row_class = {t.get("name"): acct_class.get(t.get("account_head"), _UNCLASSIFIED)
		for t in taxes_rows}
	line_tax: dict[str, float] = {}
	for w in iwt_rows:
		# Only sales-tax allocations become a line's tax; a shipping row's
		# per-item allocation is deliberately ignored (shipping is order-level).
		if row_class.get(w.get("tax_row")) == _SALES_TAX:
			key = w.get("item_row")
			line_tax[key] = flt(line_tax.get(key)) + flt(w.get("amount"))
	# Goods sales tax = Σ IWTD (per-line, post-discount, GL-consistent).
	goods_tax_total = sum(line_tax.values())
	shipping_total = freight_tax_total = sales_tax_charged = 0.0
	marketplace_tax_total = marketplace_fee_total = 0.0
	unclassified = []
	for t in taxes_rows:
		cls = row_class.get(t.get("name"))
		# post-discount amount (== base_tax_amount when there is no discount).
		amt = flt(t.get("base_tax_amount_after_discount_amount"))
		if cls == _SHIPPING:
			shipping_total += amt
		elif cls == _SALES_TAX and _is_freight_tax_row(t):
			freight_tax_total += amt
		elif cls == _SALES_TAX:
			sales_tax_charged += amt
		# Kept OUT of sales_tax_total and out of unclassified both. Out of the
		# first because it is not tax this store owes a state — counting it there
		# would overstate the return by every marketplace sale. Out of the second
		# because unclassified is a fail-closed alarm, and an account we do
		# recognise ringing that alarm daily is how a real one stops being read.
		elif cls == _MARKETPLACE_TAX:
			marketplace_tax_total += amt
		elif cls == _MARKETPLACE_FEE:
			marketplace_fee_total += amt
		else:
			unclassified.append({"account_head": t.get("account_head"), "amount": amt})
	# Σ IWTD should equal the charged post-discount GOODS sales tax; a divergence
	# is a genuine IWTD/GL anomaly (e.g. a manually keyed tax with no item-wise
	# detail), NOT a discount artifact — comparing against the post-discount charge
	# (not the stale base_tax_amount) keeps every discounted order from being
	# false-flagged, and excluding the freight-tax rows (which never produce IWTD)
	# keeps every freight-taxed order from being false-flagged.
	tax_mismatch = abs(goods_tax_total - sales_tax_charged) > 0.01
	return {"line_tax": line_tax,
		"sales_tax_total": goods_tax_total + freight_tax_total,
		"freight_tax_total": freight_tax_total,
		"shipping_total": shipping_total, "unclassified": unclassified,
		"marketplace_tax_total": marketplace_tax_total,
		"marketplace_fee_total": marketplace_fee_total,
		"tax_mismatch": tax_mismatch}


def _execute_product_view(filters, detail):
	headers = _fetch_headers(filters, extended=True)
	headers = _apply_channel_filter(headers, filters.get("channel"))
	lines = _fetch_lines(headers, extended=True)
	item_meta = _fetch_item_meta(lines)
	serial_cost, consignment, transfer_serials = _fetch_serial_facts(lines)
	iwt = _fetch_item_wise_tax(headers)
	taxes = _fetch_taxes(headers)
	acct_class = _fetch_account_classes(taxes)
	rows = _build_product_rows(headers, lines, item_meta, serial_cost, consignment,
		iwt, taxes, acct_class, filters.get("product_type") or "", detail,
		transfer_serials=transfer_serials,
		dropship_cost=_fetch_dropship_costs(headers, lines),
		marketplace_exempt=_fetch_marketplace_exempt(headers))
	return _product_columns(detail), rows


def _fetch_item_wise_tax(headers):
	"""{invoice: [item_wise_tax_details rows]} — the per-item tax allocation
	ERPNext already stores on the invoice header (item_row→items row `name`,
	tax_row→taxes row `name`)."""
	out: dict[str, list] = {}

	def collect(parenttype, names):
		if not names:
			return
		for r in frappe.get_all(
			"Item Wise Tax Detail",
			filters={"parent": ["in", names], "parenttype": parenttype},
			fields=["parent", "item_row", "tax_row", "amount", "taxable_amount"],
		):
			out.setdefault(r["parent"], []).append(r)

	collect(_POS, [h["name"] for h in headers if h["voucher_type"] == _POS])
	collect(_SI, [h["name"] for h in headers if h["voucher_type"] == _SI])
	return out


def _fetch_taxes(headers):
	"""{invoice: [Sales Taxes and Charges rows]} — base_tax_amount_after_discount_amount
	is the post-discount, company-currency tax charged (matches base_net_amount so
	the reconciliation ties to grand_total; base_tax_amount is pre-discount stale)."""
	out: dict[str, list] = {}

	def collect(parenttype, names):
		if not names:
			return
		for r in frappe.get_all(
			"Sales Taxes and Charges",
			filters={"parent": ["in", names], "parenttype": parenttype},
			# charge_type + description feed the freight-tax subclassification
			# (_is_freight_tax_row) — same triple the freight-tax hook stamps.
			fields=["parent", "name", "account_head",
				"base_tax_amount_after_discount_amount", "charge_type", "description"],
		):
			out.setdefault(r["parent"], []).append(r)

	collect(_POS, [h["name"] for h in headers if h["voucher_type"] == _POS])
	collect(_SI, [h["name"] for h in headers if h["voucher_type"] == _SI])
	return out


def _fetch_account_classes(taxes_by_invoice):
	"""{account_head: 'sales_tax'|'shipping'|'unclassified'} for every account
	referenced by the period's taxes rows. Classified per the account's OWN
	company via the canonical resolvers (each company's shipping-income account +
	sales-tax template accounts), so it is correct across companies (dev FT /
	prod) without hard-coded names, and fails closed on anything else."""
	heads = {t["account_head"] for ts in taxes_by_invoice.values() for t in ts
		if t.get("account_head")}
	if not heads:
		return {}
	accounts = {
		a["name"]: a
		for a in frappe.get_all(
			"Account", filters={"name": ["in", list(heads)]},
			fields=["name", "company", "account_name"])
	}
	company_of = {name: a.get("company") for name, a in accounts.items()}
	canon: dict[str, tuple] = {}  # company -> (shipping_account, sales_tax_accounts set)
	out = {}
	for head in heads:
		company = company_of.get(head)
		if not company:
			out[head] = _UNCLASSIFIED
			continue
		if company not in canon:
			canon[company] = (
				shipping_account_name(company), set(_company_sales_tax_accounts(company)))
		ship, taxset = canon[company]
		out[head] = _classify_account(head, ship, taxset,
			account_name=(accounts.get(head) or {}).get("account_name"))
	return out


def _build_product_rows(headers, lines_by_invoice, item_meta, serial_cost, consignment,
                        iwt_by_invoice, taxes_by_invoice, acct_class, product_type="",
                        detail=False, transfer_serials=None, dropship_cost=None,
                        marketplace_exempt=None):
	"""Product-level rows. Product flat (detail=False): one row per order×line +
	an order-level Shipping row + fail-closed Unclassified rows. Order Detail
	(detail=True): the same lines indented under an order parent row that mirrors
	the Order view's net_sales/total. A Totals row carries each reconcilable
	component separately (gross net, trade-in, shipping, sales tax, cost)."""
	rows = []
	child_indent = 1 if detail else 0
	tot_net = tot_trade = tot_ship = tot_tax = tot_cost = tot_uncl = 0.0
	from ffl_core.compliance.interstate_tax import home_state

	tot_fees = tot_mkt_tx = 0.0
	tot_applied = tot_payout = 0.0
	# The store's own state, not Texas: the same line on the California return.
	# Set per GunBroker row below, so a Company with no address only stops a
	# marketplace run (which needs it), not every report.
	mkt_state = ""
	tot_mismatch = False
	for h in sorted(headers, key=lambda x: (str(x.get("posting_date") or ""), x.get("name") or "")):
		name = h["name"]
		lines = lines_by_invoice.get(name, [])
		f = _order_facts(h, lines, item_meta)
		if product_type == "Firearms" and not f["is_firearm"]:
			continue
		if product_type == "Non-Firearms" and f["is_firearm"]:
			continue
		bd = _tax_breakdown(taxes_by_invoice.get(name, []), iwt_by_invoice.get(name, []), acct_class)
		channel = _channel(h.get("voucher_type"), h.get("woo_online_order"),
			h.get("gunbroker_order"))
		fees = _channel_fees(h)
		ship_to_state = _ship_to_state(h)
		carry = {
			"posting_date": h.get("posting_date"),
			"invoice": name,
			# carried onto every product/shipping/order row so the invoice column's
			# Dynamic Link resolves to the right doctype (the Totals row, built
			# separately, has none → rendered as plain text).
			"voucher_type": h.get("voucher_type"),
			"channel": channel,
			"customer": h.get("customer"),
			"exempt_from_sales_tax": h.get("exempt_from_sales_tax") or 0,
			# The state the gun was shipped to — for a marketplace sale
			# that is the buyer's FFL, never the customer's own address, so it
			# cannot be derived and has to ride every row of the CSV.
			# Normalised, so the column sums the same way the card counts.
			"ship_to_state": ship_to_state,
			# The BUYER's marketplace exemption, read back through the
			# order link. Distinct from exempt_from_sales_tax above (the store's
			# own certificate); both can be false while the tax is still $0
			# because the ship-to state has no sales tax.
			"gb_tax_exempt": bool((marketplace_exempt or {}).get(h.get("gunbroker_order"))),
		}

		order_qty = order_cost = 0.0
		order_missing = False
		product_rows = []
		for line in lines:
			if line.get("item_code") == DEPOSIT_ITEM:
				# Money held for the customer, not a product line: it would report a
				# negative-price "item" with 100% margin. It comes off order_net below.
				continue
			meta = item_meta.get(line.get("item_code"), {})
			line_cost, line_missing = _line_cost(line, item_meta, serial_cost, consignment,
				is_ffl_transfer=h.get("is_ffl_transfer"),
				transfer_serials=transfer_serials, dropship_cost=dropship_cost)
			order_qty += flt(line.get("qty"))
			order_cost += line_cost
			order_missing = order_missing or line_missing
			net = flt(line.get("base_net_amount"))
			serials = split_serials(line.get("serial_no"))
			is_trade_in = line.get("item_code") == TRADE_IN_CREDIT_ITEM
			# Line profit is measured on the line's MERCHANDISE revenue
			# (gross_merch) — for every ordinary line that IS its net. A trade-in
			# credit line is 0 merch / 0 cost (_line_cost), so it reports profit
			# 0: the allowance is an inventory purchase, neither a loss on this
			# sale nor the old cost-wash entry. Column sums stay internally
			# consistent: Σprofit == Σgross_merch − Σcost == the Totals row.
			gross = 0.0 if is_trade_in else net
			product_rows.append({
				**carry,
				"row_kind": "product",
				"indent": child_indent,
				"item_code": line.get("item_code"),
				"item_name": line.get("item_name"),
				"category": meta.get("item_group") or "",
				"serial_no": line.get("serial_no"),
				"qty": flt(line.get("qty")),
				"unit_price": flt(line.get("base_net_rate")),
				"list_price": flt(line.get("price_list_rate")),
				"discount": flt(line.get("discount_amount")),
				"net_amount": net,
				# Gross merchandise (full price) vs the trade-in allowance, split so
				# the CSV needn't subtract the TRADE-IN-CREDIT row to reach gross
				# receipts. A trade-in row is 0 merch / all allowance.
				"gross_merch": gross,
				"trade_in_net": net if is_trade_in else 0.0,
				"line_tax": flt(bd["line_tax"].get(line.get("name"))),
				"cost": line_cost,
				"profit": gross - line_cost,
				"is_firearm": bool(meta.get("is_firearm")),
				"is_trade_in": is_trade_in,
				"is_consignment": bool(_consigned(line, serials, consignment)),
				"missing_cost": line_missing,
				"tax_mismatch": bd["tax_mismatch"],
			})

		# A gunsmith deposit is customer money held, never a sale (Order view, same rule).
		order_net = flt(h.get("base_net_total")) - f["deposit_net"]
		trade_in_net = f["trade_in_net"]
		applied, payout = _trade_in_settlement(h, lines, trade_in_net, taxes_by_invoice.get(name))
		if detail:
			# Parent mirrors the Order view (net_sales / total), so the tree is
			# "the Order view, expandable"; children carry the line detail.
			# net_sales is gross of trade-in exactly like the Order view (==
			# gross_merch; the credit is an inventory purchase, not negative
			# revenue), cost is goods COGS (credit lines cost 0), and profit is
			# measured on the gross figure — numerically identical to the old
			# net-method profit.
			rows.append({
				**carry,
				"row_kind": "order",
				"indent": 0,
				# order_ref on the PARENT order row ONLY (FE pass-through so the
				# tree's parent shows the same Woo #), NOT on the flat product/CSV
				# rows (19.5-A rejected) and NOT in carry. FE renders "#…".
				"order_ref": f["order_ref"],
				"category": f["category"],
				"qty": order_qty,
				"net_sales": order_net - trade_in_net,
				"gross_merch": order_net - trade_in_net,
				"trade_in_net": trade_in_net,
				# Mirrors the Order view's data-only marker so the FE can flag
				# the gross Net Sales cell on this parent row too.
				"is_trade_in": bool(trade_in_net),
				"total": flt(h.get("grand_total")),
				# Order-level, so only on this parent row (spread over the lines it
				# would invent a per-line payout) — the flat view has no such column.
				"trade_in_applied": applied,
				"trade_in_payout": payout,
				"cost": order_cost,
				"profit": (order_net - trade_in_net) - order_cost,
				"sales_tax": bd["sales_tax_total"],
				# Data only in this slice — the marketplace columns and the
				# month-end reconciliation that reads them are the next one.
				"marketplace_tax": bd["marketplace_tax_total"],
				"marketplace_fee": bd["marketplace_fee_total"],
				"shipping": bd["shipping_total"],
				# Order-level because the fee is charged per order —
				# spreading it over lines would invent a per-line number GB
				# never billed. The flat Product view therefore omits it.
				"channel_fees": fees,
				"profit_after_fees": ((order_net - trade_in_net) - order_cost) - fees,
				"missing_cost": order_missing,
				"tax_mismatch": bd["tax_mismatch"],
			})
		rows.extend(product_rows)
		if bd["shipping_total"] or bd["freight_tax_total"]:
			rows.append({
				**carry,
				"row_kind": "shipping",
				"indent": child_indent,
				"item_name": _("Shipping"),
				"category": _("Shipping"),
				"shipping": bd["shipping_total"],
				# The freight tax rides the Shipping row's tax column (it has no
				# per-line IWTD), so the flat view's Line Tax column sums to the
				# Total row / Sales Tax card figure and the freight tax stays
				# separable per order. Omitted when zero so pre-freight-tax
				# shipping rows render exactly as before.
				**({"line_tax": bd["freight_tax_total"]} if bd["freight_tax_total"] else {}),
			})
		for u in bd["unclassified"]:
			rows.append({
				**carry,
				"row_kind": "unclassified",
				"indent": child_indent,
				# the account is surfaced in the Category column (a rendered column,
				# so it survives CSV export) — the Item Name column was removed and
				# item_code stays blank so the Item link doesn't dangle.
				"category": _("Unclassified: {0}").format(u["account_head"]),
				"item_name": _("Unclassified charge: {0}").format(u["account_head"]),
				"unclassified": u["amount"],
			})

		tot_net += order_net
		tot_trade += trade_in_net
		tot_applied += applied
		tot_payout += payout
		tot_ship += bd["shipping_total"]
		tot_tax += bd["sales_tax_total"]
		tot_cost += order_cost
		tot_uncl += sum(u["amount"] for u in bd["unclassified"])
		tot_fees += fees
		tot_mismatch = tot_mismatch or bd["tax_mismatch"]
		# Texas Item 1: goods + freight of every TEXAS sale made
		# THROUGH a marketplace provider, and nothing the provider collected on
		# top (its sales tax and its buyer fee are somebody else's money). The
		# channel test is what makes it "marketplace": a Texan buying at the
		# counter or on the web is ordinary taxable business and stays in Item 2.
		if channel == "GunBroker":
			mkt_state = home_state(h.get("company"))
			if ship_to_state == mkt_state:
				tot_mkt_tx += (order_net - trade_in_net) + bd["shipping_total"]

	if rows:
		rows.append({
			"row_kind": "total",
			"is_total": True,
			"indent": 0,
			"invoice": _("Total"),
			# gross_merch (full-price merchandise) is the first-class gross-receipts
			# figure; net_amount is net-OF-trade-in and must NOT be read as
			# gross receipts. trade_in_net is the separable allowance.
			"gross_merch": tot_net - tot_trade,
			# net_sales: what the Order Detail grid's visible Net Sales column
			# shows on the Total row — gross of trade-in, so it sums the parent
			# rows and agrees with the Net Sales card. Without it the FE used to
			# backfill from net_amount and resurrect the netted figure.
			"net_sales": tot_net - tot_trade,
			"net_amount": tot_net,
			"trade_in_net": tot_trade,
			"trade_in_applied": tot_applied,
			"trade_in_payout": tot_payout,
			"shipping": tot_ship,
			# period sales tax rendered under BOTH the flat view's per-line tax
			# column (line_tax) and the detail view's order-level column (sales_tax),
			# so the total is visible in either view.
			"line_tax": tot_tax,
			"sales_tax": tot_tax,
			"unclassified": tot_uncl,
			# cost is goods COGS (credit lines cost 0) and profit is measured on
			# gross merchandise — same figures the Order view totals to, and Σ of
			# the flat view's cost / profit columns.
			"cost": tot_cost,
			"profit": (tot_net - tot_trade) - tot_cost,
			"channel_fees": tot_fees,
			"profit_after_fees": ((tot_net - tot_trade) - tot_cost) - tot_fees,
			# Data only — the "Marketplace sales (TX)" card reads it, and only
			# when the channel filter says the period IS one marketplace.
			"marketplace_tx": tot_mkt_tx,
			"marketplace_state": mkt_state,
			"tax_mismatch": tot_mismatch,
		})
	return rows


def _product_columns(detail):
	head = [
		{"label": _("Date"), "fieldname": "posting_date", "fieldtype": "Date", "width": 90},
		{"label": _("Invoice"), "fieldname": "invoice", "fieldtype": "Dynamic Link", "options": "voucher_type", "width": 160},
		{"label": _("Channel"), "fieldname": "channel", "fieldtype": "Data", "width": 95},
		{"label": _("Customer"), "fieldname": "customer", "fieldtype": "Link", "options": "Customer", "width": 150},
	]
	if detail:
		return head + [
			{"label": _("Category"), "fieldname": "category", "fieldtype": "Data", "width": 130},
			{"label": _("Item"), "fieldname": "item_code", "fieldtype": "Link", "options": "Item", "width": 160},
			{"label": _("Serial No"), "fieldname": "serial_no", "fieldtype": "Data", "width": 120},
			{"label": _("Qty"), "fieldname": "qty", "fieldtype": "Float", "width": 60, "precision": 0},
			{"label": _("Unit Price"), "fieldname": "unit_price", "fieldtype": "Currency", "width": 100},
			{"label": _("Net Sales"), "fieldname": "net_sales", "fieldtype": "Currency", "width": 110},
			{"label": _("Gross Goods"), "fieldname": "gross_merch", "fieldtype": "Currency", "width": 110},
			{"label": _("Trade-in"), "fieldname": "trade_in_net", "fieldtype": "Currency", "width": 100},
			{"label": _("Net Amount"), "fieldname": "net_amount", "fieldtype": "Currency", "width": 110},
			{"label": _("Line Tax"), "fieldname": "line_tax", "fieldtype": "Currency", "width": 95},
			{"label": _("Shipping"), "fieldname": "shipping", "fieldtype": "Currency", "width": 95},
			{"label": _("Unclassified"), "fieldname": "unclassified", "fieldtype": "Currency", "width": 105},
			{"label": _("Sales Tax"), "fieldname": "sales_tax", "fieldtype": "Currency", "width": 95},
			{"label": _("Cost"), "fieldname": "cost", "fieldtype": "Currency", "width": 100},
			{"label": _("Profit"), "fieldname": "profit", "fieldtype": "Currency", "width": 100},
			{"label": _("Channel Fees"), "fieldname": "channel_fees", "fieldtype": "Currency", "width": 105},
			{"label": _("Profit after Fees"), "fieldname": "profit_after_fees", "fieldtype": "Currency", "width": 125},
			{"label": _("Ship-to State"), "fieldname": "ship_to_state", "fieldtype": "Data", "width": 90},
			{"label": _("Tax Mismatch"), "fieldname": "tax_mismatch", "fieldtype": "Check", "width": 95},
			{"label": _("Trade-in Applied"), "fieldname": "trade_in_applied", "fieldtype": "Currency", "width": 120},
			{"label": _("Trade-in Paid Out"), "fieldname": "trade_in_payout", "fieldtype": "Currency", "width": 125},
			{"label": _("Total"), "fieldname": "total", "fieldtype": "Currency", "width": 110},
		]
	return head + [
		{"label": _("Kind"), "fieldname": "row_kind", "fieldtype": "Data", "width": 90},
		{"label": _("Item"), "fieldname": "item_code", "fieldtype": "Link", "options": "Item", "width": 160},
		{"label": _("Category"), "fieldname": "category", "fieldtype": "Data", "width": 130},
		{"label": _("Serial No"), "fieldname": "serial_no", "fieldtype": "Data", "width": 120},
		{"label": _("Qty"), "fieldname": "qty", "fieldtype": "Float", "width": 60, "precision": 0},
		{"label": _("Unit Price"), "fieldname": "unit_price", "fieldtype": "Currency", "width": 100},
		{"label": _("List Price"), "fieldname": "list_price", "fieldtype": "Currency", "width": 100},
		{"label": _("Discount"), "fieldname": "discount", "fieldtype": "Currency", "width": 90},
		{"label": _("Net Amount"), "fieldname": "net_amount", "fieldtype": "Currency", "width": 110},
		{"label": _("Gross Goods"), "fieldname": "gross_merch", "fieldtype": "Currency", "width": 110},
		{"label": _("Trade-in"), "fieldname": "trade_in_net", "fieldtype": "Currency", "width": 100},
		{"label": _("Line Tax"), "fieldname": "line_tax", "fieldtype": "Currency", "width": 95},
		{"label": _("Shipping"), "fieldname": "shipping", "fieldtype": "Currency", "width": 95},
		{"label": _("Unclassified"), "fieldname": "unclassified", "fieldtype": "Currency", "width": 105},
		{"label": _("Cost"), "fieldname": "cost", "fieldtype": "Currency", "width": 100},
		{"label": _("Profit"), "fieldname": "profit", "fieldtype": "Currency", "width": 100},
		{"label": _("Missing Cost"), "fieldname": "missing_cost", "fieldtype": "Check", "width": 90},
		{"label": _("Tax Mismatch"), "fieldname": "tax_mismatch", "fieldtype": "Check", "width": 95},
		{"label": _("Trade-in?"), "fieldname": "is_trade_in", "fieldtype": "Check", "width": 75},
		{"label": _("Consignment?"), "fieldname": "is_consignment", "fieldtype": "Check", "width": 95},
		{"label": _("Exempt"), "fieldname": "exempt_from_sales_tax", "fieldtype": "Check", "width": 70},
		# Worksheet pair. Ship-to State is the sum key; Marketplace Exempt
		# is why a $0-tax row is $0 — the buyer's exemption rather than a state
		# with no sales tax. Both stay VISIBLE: the CSV is the evidence Texas
		# wants kept for four years, and a hidden column reads as a missing one.
		{"label": _("Ship-to State"), "fieldname": "ship_to_state", "fieldtype": "Data", "width": 90},
		{"label": _("Marketplace Exempt"), "fieldname": "gb_tax_exempt", "fieldtype": "Check", "width": 90},
	]
