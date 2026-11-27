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
                    gun IS a firearm-with-serial, but its purchase_rate is 0).
                    A sale since the commission freeze is consigned when its
                    line froze a %; an older one when the stay it took was
                    acquired on a consignment Purchase Receipt or, while that
                    stay is the gun's latest, the Serial No is stamped consigned,
  - firearm line  → Σ over the line's serials of the gun's cost AT THIS SALE'S
                    OWN DELIVERY, read from the stock ledger's Serial and Batch
                    Entries — what the GL books as COGS when the gun goes out.
                    The delivery is the sale's own outward entry, found by
                    document link only (the invoice's own stock out, the Sales
                    Invoice a POS Invoice was consolidated into, a Delivery
                    Note of it, the Material Issue of an FFL Disposition of
                    it), even when it was posted a moment before the invoice.
                    An acquisition (Purchase Receipt, a Purchase Invoice that
                    updates stock, Material Receipt, Repack, Manufacture,
                    Opening Stock, a count that finds a gun not in stock)
                    opens a stay; a move (any Stock Entry that takes the
                    serial out and back in, a cost correction or count with a
                    leg each way) carries or restates its value. The stay is
                    the last acquisition before the delivery and the cost the
                    stay's last inward rate before it (_resolve_sale). A sale
                    with no linked delivery takes the first outward after it
                    (after the gun's arrival if it was invoiced first) that is
                    not a move or a count — never one that delivers another
                    sale. Not delivered yet and in stock: the gun's value today,
                    cost_pending_delivery. Gone, with no stay left that another
                    sale did not take: cost 0, missing_cost — never an old
                    stay's cost. An outward entry before the gun's first
                    acquisition means its receipt was never recorded: costed
                    from purchase_rate and flagged missing_cost (a customer's
                    gun handed over at $0 on a transfer ticket: 0, no flag). An
                    invoice with no posting_time is placed at its creation. A
                    return is costed as of the sale it returns. A later
                    acquisition of the same serial (bought back, traded in)
                    never re-costs an earlier sale. A gun re-invoiced at $0 to
                    the customer of its last priced sale, after coming back
                    only on a receipt that reverses that sale's hand-over (sent
                    back in transit), costs 0, no flag, and so does a credit
                    note of that $0 invoice: the sale carries the cost. The stay's acquisition in
                    a custody warehouse is a customer's gun: cost 0, not a gap.
                    The Serial No's own marks (purchase_rate, is_transfer_gun,
                    zero_cost_confirmed) describe the gun's LATEST stay, so
                    they are read only while no acquisition follows the sale's
                    stay: is_transfer_gun → 0; a ledger value at or under the
                    placeholder ceiling ($1) → 0 when confirmed free, else
                    purchase_rate with the row marked placeholder_cost. After
                    a later acquisition, a placeholder ledger value stands and
                    the row is flagged missing_cost. A serial with no
                    acquisition in the ledger is costed from purchase_rate
                    (row flagged when it has none); one with an inward voucher
                    the report does not classify is too, and marked
                    unledgered_cost. A serial on two priced sales that took the
                    same stay (or the later took none), the earlier not
                    reversed by a credit note, marks both double_sale: a review
                    flag, costs unchanged,
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

import re

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

from ffl_core import report_cards
from ffl_core.api.cost_correction import _HINT_OLD_MAX as _PLACEHOLDER_COST_MAX
from ffl_core.compliance.shipping_charge import SHIPPING_TAX_DESCRIPTION
from ffl_core.marketplace_accounts import (
	MARKETPLACE_FEE_ACCOUNTS, MARKETPLACE_TAX_ACCOUNTS, ORDER_DOCTYPE)
from ffl_core.setup.shipping_charge import shipping_account_name
from ffl_core.setup.tax import sales_tax_and_excise_accounts
from ffl_core.deposits import ITEMS as DEPOSIT_ITEMS
from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
from ffl_core.utils import split_serials
from ffl_core.warehouses import custody_warehouse_names

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
		taxes_by_invoice=_fetch_taxes(credit_headers),
		as_of=_fetch_cost_as_of_sale(headers, lines, item_meta))
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


def _fetch_lines(headers, extended=False, more=()):
	"""{invoice: [line rows]}. *more*: further line columns a caller needs (the Sales Summary's
	pricing facts), each fetched where the child doctype has it."""
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
		child_fields = fields + [f for f in (*_HOUSE_FIELDS, *more) if f not in fields and _has_column(child_dt, f)]
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


# What a Serial and Batch Entry is, by its voucher. An ACQUISITION starts a
# stay of the gun in the store: a Purchase Receipt or a Purchase Invoice that
# updates stock (bought, traded in, taken in on transfer or consignment), a
# Material Receipt, a Repack or Manufacture that produced it, an Opening Stock
# entry, or a count that found it when it was not in stock. A MOVE only carries
# or restates that stay's value and has a leg each way: a warehouse transfer,
# any Stock Entry that takes the serial out and back in, a cost correction or a
# count that found it where it was. A RETURN is the inward bundle of a sales
# voucher (a credit note with the gun): the same stay coming home at the cost it
# left with, never a receipt. Any other inward voucher is unclassified: none of
# the POS's own paths make one for a serialized firearm.
_ACQUISITION, _MOVE, _RETURN = "acquisition", "move", "return"
_SALES_VOUCHERS = ("Sales Invoice", "POS Invoice", "Delivery Note")
_ACQUIRING_ENTRY_PURPOSES = ("Material Receipt", "Repack", "Manufacture")


def _entry_kind(entry, both_legs) -> str | None:
	"""_ACQUISITION, _MOVE, _RETURN, or None: an inward voucher this report does
	not classify, or on the outward side the gun leaving stock. *both_legs*: the
	same voucher carries the serial out and in. A Stock Reconciliation with one
	inward leg took in a gun that was not in stock; one outward leg took it out."""
	inward = entry.type_of_transaction == "Inward"
	if entry.voucher_type == "Stock Entry":
		if both_legs or entry.stock_entry_purpose == "Material Transfer":
			return _MOVE
		return _ACQUISITION if inward and entry.stock_entry_purpose in _ACQUIRING_ENTRY_PURPOSES else None
	if entry.voucher_type == "Stock Reconciliation" and entry.reconciliation_purpose in (
			"Stock Reconciliation", "Opening Stock"):
		return _MOVE if both_legs else _ACQUISITION if inward else None
	if not inward:
		return None
	if entry.voucher_type in ("Purchase Receipt", "Purchase Invoice"):
		return _ACQUISITION
	return _RETURN if entry.voucher_type in _SALES_VOUCHERS else None


def _serial_ledger(serials) -> dict:
	"""{SERIAL: [entries]} — every standing Serial and Batch Entry of *serials*,
	both ways, in posting order, each with .at (posting datetime, creation) and
	.kind (_entry_kind). Upper-cased keys: the database matches serials without
	regard to case, and a sale line may spell one differently from its Serial No."""
	if not serials:
		return {}
	out: dict[str, list] = {}
	for e in frappe.db.sql("""
			select e.serial_no, e.incoming_rate, coalesce(nullif(e.warehouse, ''), b.warehouse) as warehouse,
				b.posting_datetime, b.creation, b.voucher_type, b.voucher_no, b.voucher_detail_no,
				se.purpose as stock_entry_purpose, se.remarks, sr.purpose as reconciliation_purpose, b.type_of_transaction
			from `tabSerial and Batch Bundle` b
			join `tabSerial and Batch Entry` e on e.parent = b.name
			left join `tabStock Entry` se on b.voucher_type = 'Stock Entry' and se.name = b.voucher_no
			left join `tabStock Reconciliation` sr on b.voucher_type = 'Stock Reconciliation' and sr.name = b.voucher_no
			where e.serial_no in %(serials)s
				and b.type_of_transaction in ('Inward', 'Outward') and b.is_rejected = 0
				and b.docstatus = 1 and b.is_cancelled = 0 and b.posting_datetime is not null
			order by b.posting_datetime, b.creation""", {"serials": tuple(sorted(serials))}, as_dict=True):
		e.at = (e.posting_datetime, e.creation)
		out.setdefault(e.serial_no.upper(), []).append(e)
	legs = {}
	for sn, seq in out.items():
		for e in seq:
			legs.setdefault((sn, e.voucher_type, e.voucher_no), set()).add(e.type_of_transaction)
	for sn, seq in out.items():
		for e in seq:
			e.kind = _entry_kind(e, len(legs[(sn, e.voucher_type, e.voucher_no)]) == 2)
	return out


def _sale_moments(headers) -> dict:
	"""{invoice: (posting datetime, creation)} — when each sale happened, for
	ordering it against the stock ledger. A return answers with the sale it
	returns (return_against), so it reverses exactly what that sale cost even
	when the gun was bought back in between; a credit note with no original
	keeps its own moment. creation breaks a tie inside one second; an invoice
	with no posting_time is read as posted when it was created."""
	names: dict[str, set] = {}
	for h in headers:
		names.setdefault(h["voucher_type"], set()).add(h["name"])
	moments, against = {}, {}

	def read(doctype, wanted):
		# doctype is one of the two module constants, never caller input.
		return frappe.db.sql(f"""
			select name, coalesce(timestamp(posting_date, posting_time), creation) as at, creation, return_against
			from `tab{doctype}` where name in %(names)s""", {"names": tuple(wanted)}, as_dict=True)

	for doctype, wanted in names.items():
		for r in read(doctype, wanted):
			moments[r.name] = (r.at, r.creation)
			if r.return_against:
				against[r.name] = (doctype, r.return_against)
		originals = {o for d, o in against.values() if d == doctype and o not in moments}
		if originals:
			for r in read(doctype, originals):
				moments[r.name] = (r.at, r.creation)
	for name, (_doctype, original) in against.items():
		if original in moments:
			moments[name] = moments[original]
	return moments


def _fetch_cost_as_of_sale(headers, lines_by_invoice, item_meta) -> dict:
	"""{(invoice, SERIAL): fact} — each sold gun's cost as of THAT sale's own
	delivery (_ledger_cost_as_of_sale),
	except a gun re-invoiced at no charge after its sale, and a credit note of
	that re-invoice (_reissued_at_no_charge): the sale already carries its cost,
	so these cost 0 and are no gap."""
	serials = {sn.upper() for ls in (lines_by_invoice or {}).values() for l in ls
		if (item_meta.get(l.get("item_code")) or {}).get("is_firearm") for sn in split_serials(l.get("serial_no"))}
	priced = _priced_sales(serials)   # one read, both callers
	out = _ledger_cost_as_of_sale(headers, lines_by_invoice, item_meta, priced)
	for key in _reissued_at_no_charge(headers, lines_by_invoice, item_meta, priced):
		out[key] = {**out.get(key, {}), "rate": 0.0, "missing": False, "placeholder": False, "pending": False,
			"no_stay": False}
	return out


def _reissued_at_no_charge(headers, lines_by_invoice, item_meta, priced=None) -> set:
	"""{(invoice, SERIAL)} of this report's lines that cost 0 because the gun's
	sale already carries its cost: a $0 re-invoice of a gun sent back in
	transit and sent out once more (_no_charge_reissues), and a credit note of
	such a re-invoice, wherever the re-invoice itself falls."""
	free = {}      # (doctype, invoice, SERIAL) -> customer: the $0 firearm lines to judge
	notes = {}     # doctype -> {credit note: {SERIAL}}
	for h in headers:
		serials = {sn.upper() for l in (lines_by_invoice or {}).get(h["name"], [])
			if (item_meta.get(l.get("item_code")) or {}).get("is_firearm")
			and (cint(h.get("is_return")) or flt(l.get("base_net_amount")) == 0)
			for sn in split_serials(l.get("serial_no"))}
		if not serials:
			continue
		if cint(h.get("is_return")):
			notes.setdefault(h["voucher_type"], {})[h["name"]] = serials
		elif h.get("customer"):
			free.update({(h["voucher_type"], h["name"], sn): h["customer"] for sn in serials})
	credited = {}  # (doctype, re-invoice, SERIAL) -> [credit notes]
	for doctype, by_note in notes.items():
		against = {r.name: r.return_against for r in frappe.get_all(doctype,
			filters={"name": ["in", list(by_note)]}, fields=["name", "return_against"]) if r.return_against}
		if not against:
			continue
		for note, original in against.items():
			for sn in by_note[note]:
				credited.setdefault((doctype, original, sn), []).append(note)
		# doctype is one of the two module constants, never caller input.
		for r in frappe.db.sql(f"""
				select p.name, p.customer, i.serial_no
				from `tab{doctype} Item` i join `tab{doctype}` p on p.name = i.parent
				where p.name in %(names)s and p.docstatus = 1 and p.is_return = 0 and i.base_net_amount = 0""",
				{"names": tuple(set(against.values()))}, as_dict=True):
			for sn in split_serials(r.serial_no):
				if r.customer and (doctype, r.name, sn.upper()) in credited:
					free.setdefault((doctype, r.name, sn.upper()), r.customer)
	zeroed = _no_charge_reissues(free, priced) if free else set()
	ours = {h["name"] for h in headers}
	return {(inv, sn) for _doctype, inv, sn in zeroed if inv in ours} | {
		(note, key[2]) for key in zeroed for note in credited.get(key, ())}


def _no_charge_reissues(free, priced=None) -> set:
	"""The keys of *free* ({(doctype, invoice, SERIAL): customer}, $0 firearm
	lines of non-return invoices) that re-invoice a gun its sale already costs.

	The sale is the serial's MOST RECENT priced sale before the $0 invoice
	(_sale_moments): a line with base_net_amount > 0 on a submitted, non-return
	invoice this report counts — a Sales Invoice that is not a consolidation, a
	POS Invoice that is not a counter pickup's ticket — in any period. It must
	be to the same customer and not taken back by a credit note of that sale;
	otherwise nothing further back is looked at.

	Between the two, the serial's inward ledger entries are either none, or
	every one is a Material Receipt reversing the sale's hand-over: its remarks
	name the outward voucher that issued the serial for the sale (the invoice
	itself, a Delivery Note of it, the Material Issue that took the gun out of
	that stay) or an FFL Disposition of the sale — linked to the invoice or its
	web order — whose Material Issue issued it. Moves (_entry_kind) are read
	past, both legs. A buy-back, a trade-in, any other receipt or a count that
	takes the gun in between leaves the $0 line to the ledger. Serials compare
	upper-cased, like the ledger lookup's. *priced*: _priced_sales of at least
	these serials, when the caller has read them already."""
	serials = {sn for _doctype, _inv, sn in free}
	sales = [s for s in (_priced_sales(serials) if priced is None else priced) if s[2] in serials]
	if not sales:
		return set()
	moments = _sale_moments([{"voucher_type": d, "name": n} for d, n, *_rest in sales]
		+ [{"voucher_type": d, "name": n} for d, n, _sn in free])
	returned = _returned_sales(sales)
	ledger: dict[str, list] = {}
	for e in frappe.db.sql("""
			select e.serial_no, b.type_of_transaction, b.voucher_type, b.voucher_no, b.posting_datetime, b.creation,
				se.purpose as stock_entry_purpose, sr.purpose as reconciliation_purpose, se.remarks
			from `tabSerial and Batch Bundle` b
			join `tabSerial and Batch Entry` e on e.parent = b.name
			left join `tabStock Entry` se on b.voucher_type = 'Stock Entry' and se.name = b.voucher_no
			left join `tabStock Reconciliation` sr on b.voucher_type = 'Stock Reconciliation' and sr.name = b.voucher_no
			where e.serial_no in %(serials)s and b.type_of_transaction in ('Inward', 'Outward')
				and b.docstatus = 1 and b.is_cancelled = 0 and b.posting_datetime is not null
			order by b.posting_datetime, b.creation""", {"serials": tuple(sorted(serials))}, as_dict=True):
		e.at = (e.posting_datetime, e.creation)
		ledger.setdefault(e.serial_no.upper(), []).append(e)
	# A move (a warehouse transfer, a cost correction or count with a leg each way) only carries the
	# stay: neither of its legs is a receipt or a hand-over, so the window below reads past both.
	legs = {}
	for sn, seq in ledger.items():
		for e in seq:
			legs.setdefault((sn, e.voucher_type, e.voucher_no), set()).add(e.type_of_transaction)
	ledger = {sn: [e for e in seq if _entry_kind(e, len(legs[(sn, e.voucher_type, e.voucher_no)]) == 2) != _MOVE]
		for sn, seq in ledger.items()}

	last = {}  # each $0 line's key -> its serial's last priced sale, when that sale is the one to judge
	for key, customer in free.items():
		at, sn = moments.get(key[1]), key[2]
		prior = [s for s in sales if s[2] == sn and at and moments.get(s[1]) and moments[s[1]] < at]
		if prior:
			sale = max(prior, key=lambda s: moments[s[1]])
			if sale[3] == customer and (sale[0], sale[1], sn) not in returned:
				last[key] = sale
	if not last:
		return set()
	delivered = _sale_hand_overs(set(last.values()), ledger)
	out = set()
	for key, (d0, i0, sn, _customer, _web) in last.items():
		since, until = moments[i0], moments[key[1]]
		seq = ledger.get(sn, [])
		back = [e for e in seq if e.type_of_transaction == "Inward" and since < e.at < until]
		if back:
			# The Material Issue that ended the sale's stay: the last outward before
			# the gun came back, after the stay's last receipt at or before the sale.
			opened = [e.at for e in seq if e.type_of_transaction == "Inward" and e.at <= since]
			issue = [e for e in seq if e.type_of_transaction == "Outward" and e.at < back[0].at
				and (not opened or e.at > opened[-1])]
			tied = set(delivered.get((d0, i0, sn), ()))
			if issue and issue[-1].voucher_type == "Stock Entry" and issue[-1].stock_entry_purpose == "Material Issue":
				tied.add(issue[-1].voucher_no)
			if not all(e.voucher_type == "Stock Entry" and e.stock_entry_purpose == "Material Receipt"
					and _names_any(e.remarks, tied) for e in back):
				continue
		out.add(key)
	return out


def _priced_sales(serials) -> list:
	"""[(doctype, invoice, SERIAL, customer, web order)] — every priced sale of
	*serials* in any period: a line with base_net_amount > 0 on a submitted,
	non-return invoice this report counts (a Sales Invoice that is not a
	consolidation, a POS Invoice that is not a counter pickup's ticket)."""
	wanted = {sn.upper() for sn in serials}
	if not wanted:
		return []
	out = []
	for doctype, counted, web in (
			(_SI, "p.is_consolidated = 0", "p.woo_online_order" if _has_column(_SI, "woo_online_order") else "null"),
			(_POS, "coalesce(p.ffl_counter_order, '') = ''" if _has_column(_POS, "ffl_counter_order") else "1 = 1", "null")):
		# doctype / counted / web are module constants, never caller input.
		# lazy: reads every priced serialized line ever and matches the serials here — a store's few
		# thousand gun sales; an indexed serial→sale-line map if that grows.
		for r in frappe.db.sql(f"""
				select p.name, p.customer, {web} as web_order, i.serial_no
				from `tab{doctype} Item` i join `tab{doctype}` p on p.name = i.parent
				where p.docstatus = 1 and p.is_return = 0 and i.base_net_amount > 0 and {counted}
					and coalesce(i.serial_no, '') != ''""", as_dict=True):
			out += [(doctype, r.name, sn.upper(), r.customer, r.web_order)
				for sn in split_serials(r.serial_no) if sn.upper() in wanted]
	return list(dict.fromkeys(out))


def _returned_sales(sales) -> set:
	"""{(doctype, invoice, SERIAL)} of *sales* taken back by a submitted credit note that names the serial."""
	returned = set()
	for doctype in {d for d, *_rest in sales}:
		# doctype is one of the two module constants, never caller input.
		for r in frappe.db.sql(f"""
				select p.return_against, i.serial_no
				from `tab{doctype} Item` i join `tab{doctype}` p on p.name = i.parent
				where p.docstatus = 1 and p.is_return = 1 and p.return_against in %(against)s""",
				{"against": tuple({n for d, n, *_rest in sales if d == doctype})}, as_dict=True):
			returned.update((doctype, r.return_against, sn.upper()) for sn in split_serials(r.serial_no))
	return returned


def _sale_hand_overs(sales, ledger) -> dict:
	"""{(doctype, invoice, SERIAL): {names}} — the vouchers that issued each
	sale's serial out of stock and are tied to that sale by a document link,
	never by timing: the invoice itself, the Sales Invoice a POS Invoice was
	consolidated into, a Delivery Note of it or of the Sales Order it was
	billed from (the serial picks the note's line), the Material Issue of an FFL
	Disposition of it (linked to the invoice, the POS invoice or its
	consolidation, the web order, or the marketplace order), plus those
	dispositions' names. A disposition that kept no stock_entry ties the serial's
	Material Issue whose remarks name it (the issue it posted itself). *sales*:
	(doctype, invoice, SERIAL, customer, web order); *ledger*: {SERIAL: [entries]}."""
	issued = {sn: {e.voucher_no: e for e in seq if e.type_of_transaction == "Outward"} for sn, seq in ledger.items()}
	invoices = {n for d, n, *_rest in sales if d == _SI}
	counter = {n for d, n, *_rest in sales if d == _POS}
	merged = {r.name: r.consolidated_invoice for r in frappe.get_all(_POS, filters={
		"name": ["in", list(counter)], "consolidated_invoice": ["is", "set"]},
		fields=["name", "consolidated_invoice"])} if counter else {}
	marketplace = {r.name: r.gunbroker_order for r in frappe.get_all(_SI, filters={
		"name": ["in", list(invoices)], "gunbroker_order": ["is", "set"]},
		fields=["name", "gunbroker_order"])} if invoices and _has_column(_SI, "gunbroker_order") else {}
	deliveries: dict[str, set] = {}
	if invoices:
		if _has_column("Delivery Note Item", "against_sales_invoice"):
			for r in frappe.get_all("Delivery Note Item", filters={"against_sales_invoice": ["in", list(invoices)],
					"docstatus": 1}, fields=["parent", "against_sales_invoice"]):
				deliveries.setdefault(r.against_sales_invoice, set()).add(r.parent)
		if _has_column("Sales Invoice Item", "delivery_note"):
			for r in frappe.get_all("Sales Invoice Item", filters={"parent": ["in", list(invoices)],
					"delivery_note": ["is", "set"]}, fields=["parent", "delivery_note"]):
				deliveries.setdefault(r.parent, set()).add(r.delivery_note)
		orders: dict[str, set] = {}
		for r in frappe.get_all("Sales Invoice Item", filters={"parent": ["in", list(invoices)],
				"sales_order": ["is", "set"]}, fields=["parent", "sales_order"]):
			orders.setdefault(r.sales_order, set()).add(r.parent)
		if orders:
			for r in frappe.get_all("Delivery Note Item", filters={"against_sales_order": ["in", list(orders)],
					"docstatus": 1}, fields=["parent", "against_sales_order"]):
				for inv in orders[r.against_sales_order]:
					deliveries.setdefault(inv, set()).add(r.parent)
	links = [(field, values) for field, values in (
		("sales_invoice", invoices | set(merged.values())),
		("linked_pos_invoice", counter),
		("woo_online_order", {w for d, _n, _sn, _c, w in sales if w}),
		("gunbroker_order", set(marketplace.values())))
		if values and _has_column("FFL Disposition", field)]
	dispositions = frappe.get_all("FFL Disposition", filters={"docstatus": 1},
		or_filters=[[field, "in", list(values)] for field, values in links],
		fields=["name", "serial", "stock_entry"] + [field for field, _values in links]) if (
		links and _has_column("FFL Disposition", "stock_entry")) else []
	out = {}
	for key in sales:
		doctype, inv, sn, _customer, web = key
		names = {inv} | deliveries.get(inv, set()) | ({merged[inv]} if inv in merged else set())
		mine = issued.get(sn, {})
		tied = {v for v in names if v in mine}
		for d in dispositions:
			if (d.serial or "").strip().upper() != sn or not (
					(doctype == _SI and d.get("sales_invoice") == inv)
					or (doctype == _POS and (d.get("linked_pos_invoice") == inv
						or (inv in merged and d.get("sales_invoice") == merged[inv])))
					or (web and d.get("woo_online_order") == web)
					or (inv in marketplace and d.get("gunbroker_order") == marketplace[inv])):
				continue
			if d.stock_entry:
				issues = [mine[d.stock_entry]] if d.stock_entry in mine else []
			else:
				issues = [e for e in mine.values() if e.voucher_type == "Stock Entry" and _names_any(e.get("remarks"), {d.name})]
			for entry in issues:
				if entry.voucher_type == "Stock Entry" and entry.stock_entry_purpose == "Material Issue":
					tied |= {entry.voucher_no, d.name}
		out[(doctype, inv, sn)] = tied
	return out


_ABSENT, _UNLEDGERED, _GAP, _NO_STAY, _COSTED = "absent", "unledgered", "gap", "no stay", "costed"


def _resolve_sale(seq, at, tied, taken) -> frappe._dict:
	"""Which stay of the gun one sale took, and at what value: the ONE answer the
	cost, the consignment decision and the double-sale check all read.

	*seq*: the serial's ledger (_serial_ledger); *at*: the sale's moment
	(_sale_moments; a credit note answers with the sale it returns); *tied*: the
	vouchers that deliver this sale (_sale_hand_overs); *taken*: the vouchers
	_sale_hand_overs tied to any sale of the serial — one of them that is not in
	*tied* delivers another sale.

	The sale's delivery D is its own outward entry, found by link (*tied*) —
	wherever it falls, even a moment before the invoice. With none:
	  - invoiced before the gun arrived (no acquisition at or before the sale):
	    D is its first outward after that arrival that is not a move or a count;
	    an outward before the arrival means its receipt was never recorded (_GAP);
	  - invoiced while a stay was open (its last acquisition at or before the
	    sale, no outward but moves between): D is the first outward after the
	    sale that is not a move or a count;
	  - either way, an outward tied to another sale is never this one's D:
	    that stay is not this sale's to take.
	With D: the stay is the last acquisition before D and the value the stay's
	last inward rate before D (kind _COSTED). Without D: in stock, the current
	stay at its latest value, pending delivery; gone, _NO_STAY, with .wanted the
	stay another sale took from under it (open when this sale was invoiced, or
	the arrival it was invoiced ahead of), else None.

	A serial with an inward voucher _entry_kind does not classify is _UNLEDGERED,
	one with no acquisition at all _ABSENT: the Serial No is all there is."""
	if not seq or not at:
		return frappe._dict(kind=_ABSENT)
	if any(e.type_of_transaction == "Inward" and e.kind is None for e in seq):
		return frappe._dict(kind=_UNLEDGERED)
	acqs = [e for e in seq if e.kind == _ACQUISITION]
	if not acqs:
		return frappe._dict(kind=_ABSENT)
	outs = [e for e in seq if e.type_of_transaction == "Outward" and e.kind != _MOVE]
	stocked = sum(1 if e.type_of_transaction == "Inward" else -1 for e in seq) > 0
	handed = next((o for o in outs if o.voucher_no in tied), None)
	if handed is None:
		prior = [a for a in acqs if a.at <= at]
		if prior:
			start, open_ = at, not any(prior[-1].at < o.at <= at for o in outs)
		elif any(o.at < acqs[0].at for o in outs):
			return frappe._dict(kind=_GAP)
		else:
			start, open_ = acqs[0].at, True
		wanted = None
		if open_:
			handed = next((o for o in outs if o.at > start and o.voucher_type != "Stock Reconciliation"), None)
			if handed is not None and handed.voucher_no in taken:
				wanted = prior[-1] if prior else acqs[0]
				handed, open_ = None, False
		if handed is None:
			if not (open_ or stocked):
				return frappe._dict(kind=_NO_STAY, wanted=wanted)
			value = next(e for e in reversed(seq) if e.type_of_transaction == "Inward" and e.kind in (_ACQUISITION, _MOVE))
			return frappe._dict(kind=_COSTED, stay=acqs[-1], entry=value, pending=stocked)
	before = [e for e in seq if e.at < handed.at]
	stay = next((e for e in reversed(before) if e.kind == _ACQUISITION), None)
	if stay is None:
		return frappe._dict(kind=_GAP)
	value = next(e for e in reversed(before) if e.type_of_transaction == "Inward" and e.kind in (_ACQUISITION, _MOVE))
	return frappe._dict(kind=_COSTED, stay=stay, entry=value, pending=False)


def _double_sales(sales, resolved, moments, returned) -> set:
	"""{(doctype, invoice, SERIAL)} — both sales of a serial on two priced sales
	(*sales*, _priced_sales) where the earlier is not taken back by a credit note
	(*returned*) and the two took the same stay, or the later took none
	(_NO_STAY), or the earlier took none because the later took the stay it was
	invoiced against (_NO_STAY .wanted). A buy-back or trade-in in between is
	another stay: no flag. A review flag only — the cost of neither sale changes."""
	by_serial: dict[str, list] = {}
	for d, n, sn, *_rest in sales:
		if moments.get(n) and (d, n, sn) in resolved:
			by_serial.setdefault(sn, []).append((d, n, sn))
	out = set()
	for keys in by_serial.values():
		keys.sort(key=lambda k: moments[k[1]])
		for i, earlier in enumerate(keys):
			if earlier in returned:
				continue
			stay = resolved[earlier].get("stay") or resolved[earlier].get("wanted")
			for later in keys[i + 1:]:
				took = resolved[later]
				if took.kind == _NO_STAY or (stay is not None and took.get("stay") is stay):
					out |= {earlier, later}
	return out


def _names_any(text, names) -> bool:
	"""Does *text* name any of *names* as a whole word (not a longer name containing it)?"""
	return bool(text) and any(
		re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text, re.IGNORECASE) for n in names if n)


def _ledger_cost_as_of_sale(headers, lines_by_invoice, item_meta, priced=None) -> dict:
	"""{(invoice, SERIAL): fact} for every firearm serial on this report's lines,
	from the stay the sale took (_resolve_sale). A fact:

	  rate         the gun's cost, or None: read the Serial No (_line_cost);
	  missing      a gap the ledger found (with rate None: a receipt the ledger
	               never recorded, or no stay left for this sale — no_stay, then
	               cost 0, and no gap once a credit note takes that sale back);
	  placeholder  costed from purchase_rate over a placeholder ledger value;
	  pending      not delivered yet: costed at the gun's value today
	               (cost_pending_delivery);
	  unledgered   the Serial No's current marks decided it, because the ledger
	               could not (an unclassified inward voucher; for a sale before
	               the commission freeze, also no stay to read consignment from);
	  double       the serial is on another priced sale that took the same stay
	               (_double_sales);
	  consigned    the stay's consignment: {"is_consignment", "commission_pct"},
	               False (the store's own), or None (no stay: read the stamp).

	A credit note is costed as of the sale it returns. Custody is the stay's
	acquisition's warehouse: a customer's gun taken in on transfer costs 0. The
	Serial No marks are this stay's only while no later acquisition exists
	(moves do not count): then is_transfer_gun reads 0, and a ledger value at
	or under _PLACEHOLDER_COST_MAX reads 0 when confirmed free, else
	purchase_rate with placeholder set. After a later acquisition a placeholder
	ledger value stands and is missing.

	A stay acquired on a consignment Purchase Receipt is a consignment sale's
	(ffl_core.consignment.sold_on_consignment reads it for a sale older than the
	freeze): at the Serial No's commission while that stay is the gun's latest,
	else at the receipt line's own %. While the stay is the gun's latest, a
	Serial No stamped consigned makes it one too, at the stamp's % (a gun stamped
	after it came in on a receipt of another type)."""
	sold = {}  # (invoice, SERIAL) -> one of its lines
	for inv, ls in (lines_by_invoice or {}).items():
		for l in ls:
			if (item_meta.get(l.get("item_code")) or {}).get("is_firearm"):
				for sn in split_serials(l.get("serial_no")):
					sold.setdefault((inv, sn.upper()), l)
	if not sold:
		return {}
	serials = sorted({sn for _inv, sn in sold})
	ledger = _serial_ledger(serials)
	doctype_of = {h["name"]: h["voucher_type"] for h in headers}
	invoice = _invoice_facts({(doctype_of[inv], inv) for inv, _sn in sold if inv in doctype_of})
	originals = {(d, f.return_against) for (d, _n), f in invoice.items() if f.return_against} - set(invoice)
	invoice.update(_invoice_facts(originals))

	def sale_of(inv, sn):
		"""The sale a line is costed as: a credit note's original, else the invoice itself."""
		d = doctype_of.get(inv)
		f = invoice.get((d, inv)) or frappe._dict()
		n = f.return_against if f.return_against and (d, f.return_against) in invoice else inv
		g = invoice.get((d, n)) or frappe._dict()
		return (d, n, sn, g.customer, g.web_order)

	ours = {key: sale_of(*key) for key in sold if key[0] in doctype_of}
	priced = _priced_sales(serials) if priced is None else [s for s in priced if s[2] in set(serials)]
	sales = set(ours.values()) | set(priced)
	moments = _sale_moments([{"voucher_type": d, "name": n} for d, n, *_rest in sales])
	tied = _sale_hand_overs(sales, ledger)
	taken = set().union(*tied.values()) if tied else set()
	resolved = {(d, n, sn): _resolve_sale(ledger.get(sn), moments.get(n), tied.get((d, n, sn), ()), taken)
		for d, n, sn, *_rest in sales}
	returned = _returned_sales(priced)
	doubles = _double_sales(priced, resolved, moments, returned)

	stays = [r for r in resolved.values() if r.kind == _COSTED]
	mark_fields = ["name", "purchase_rate", "is_transfer_gun", "is_consignment", "consignment_commission_pct"] + (
		["zero_cost_confirmed"] if _has_column("Serial No", "zero_cost_confirmed") else [])
	marks = {s["name"].upper(): s for s in frappe.get_all(
		"Serial No", filters={"name": ["in", serials]}, fields=mark_fields)}
	custody = custody_warehouse_names()
	consigned_receipts = _consignment_receipts({r.stay.voucher_no for r in stays
		if r.stay.voucher_type == "Purchase Receipt"})

	out = {}
	for (inv, sn), line in sold.items():
		sale = ours.get((inv, sn))
		took = resolved.get(sale[:3]) if sale else None
		fact = frappe._dict(rate=None, missing=False, placeholder=False, pending=False, unledgered=False,
			no_stay=False, double=(doctype_of.get(inv), inv, sn) in doubles, consigned=None)
		if took is None or took.kind == _ABSENT:
			pass
		elif took.kind == _UNLEDGERED:
			fact.unledgered = True
		elif took.kind == _GAP:
			fact.missing = True
		elif took.kind == _NO_STAY:
			# 0 either way; a gap only while no credit note takes the sale back (then sale and note net to 0).
			fact.no_stay, fact.missing = True, sale[:3] not in returned
		else:
			fact.update(_stay_cost(took, ledger[sn], marks.get(sn) or {}, custody, consigned_receipts))
		if fact.consigned is None and _consignment_read_from_stay(line):
			fact.unledgered = True   # an old sale's consignment, read from the gun's current stamp
		out[(inv, sn)] = fact
	return out


def _invoice_facts(keys) -> dict:
	"""{(doctype, invoice): {customer, return_against, web_order}} for *keys*, one read per doctype."""
	out = {}
	for doctype in (_SI, _POS):
		names = [n for d, n in keys if d == doctype and n]
		if not names:
			continue
		web = ["woo_online_order as web_order"] if doctype == _SI and _has_column(_SI, "woo_online_order") else []
		for r in frappe.get_all(doctype, filters={"name": ["in", names]},
				fields=["name", "customer", "return_against", *web]):
			r.setdefault("web_order", None)
			out[(doctype, r.name)] = r
	return out


def _consignment_receipts(receipts) -> dict:
	"""{Purchase Receipt: {"rows": {row name: commission %}}} for those of *receipts* that took guns in on consignment."""
	if not receipts or not _has_column("Purchase Receipt", "ffl_acquisition_type"):
		return {}
	from ffl_core.firearm import is_consignment_acquisition_type
	out = {r.name: {"rows": {}} for r in frappe.get_all("Purchase Receipt", filters={"name": ["in", list(receipts)]},
		fields=["name", "ffl_acquisition_type"]) if is_consignment_acquisition_type(r.ffl_acquisition_type)}
	if out and _has_column("Purchase Receipt Item", "ffl_consignment_commission_pct"):
		for r in frappe.get_all("Purchase Receipt Item", filters={"parent": ["in", list(out)]},
				fields=["name", "parent", "ffl_consignment_commission_pct"]):
			out[r.parent]["rows"][r.name] = flt(r.ffl_consignment_commission_pct)
	return out


def _stay_cost(took, seq, mark, custody, consigned_receipts) -> dict:
	"""The cost fields of a sale that took a stay (_resolve_sale kind _COSTED)."""
	stay, rate = took.stay, flt(took.entry.incoming_rate)
	later = any(e.kind == _ACQUISITION for e in seq[seq.index(stay) + 1:])
	receipt = consigned_receipts.get(stay.voucher_no) if stay.voucher_type == "Purchase Receipt" else None
	if not later and (receipt is not None or cint(mark.get("is_consignment"))):
		# The gun's latest stay: its Serial No stamp is this stay's, whatever the receipt was typed
		# (guns stamped consigned after they came in on a receipt of another type).
		consigned = {"is_consignment": 1, "commission_pct": flt(mark.get("consignment_commission_pct"))}
	elif receipt is None:
		consigned = False
	else:
		# The gun came back after this stay: its stamp is the later stay's. The receipt line is this one's.
		from ffl_core.setup.consignment import default_commission_pct
		# lazy: a line with no % takes today's store default, which may differ from the default when the gun
		# was received; upgrade path: freeze the resolved % onto the receipt line at intake.
		pct = receipt["rows"].get(stay.voucher_detail_no) or default_commission_pct()
		consigned = {"is_consignment": 1, "commission_pct": flt(pct)}
	fact = {"pending": bool(took.pending), "consigned": consigned}
	if stay.warehouse in custody:
		return {**fact, "rate": 0.0, "missing": False, "placeholder": False}
	if later:
		# The gun was acquired again after this stay: its marks belong to that
		# later stay. The ledger is the only witness to this one.
		return {**fact, "rate": rate, "missing": rate <= _PLACEHOLDER_COST_MAX, "placeholder": False}
	if cint(mark.get("is_transfer_gun")):
		return {**fact, "rate": 0.0, "missing": False, "placeholder": False}
	if rate > _PLACEHOLDER_COST_MAX:
		return {**fact, "rate": rate, "missing": False, "placeholder": False}
	if cint(mark.get("zero_cost_confirmed")):
		return {**fact, "rate": 0.0, "missing": False, "placeholder": False}
	# A placeholder in the ledger ($0.01 migration gun, $0 receipt whose
	# cost was filled in on the serial): the serial's cost is the real one.
	pr = flt(mark.get("purchase_rate"))
	return {**fact, "rate": pr, "missing": pr <= 0, "placeholder": True}


def _consignment_read_from_stay(line) -> bool:
	"""Does this line's consignment decision come from the gun's stay rather than
	its own frozen %? Only for a sale older than the commission freeze whose
	line kept no % (ffl_core.consignment.sold_on_consignment)."""
	from ffl_core.consignment import COMMISSION_FROZEN_SINCE
	posted = line.get("posting_date")
	return flt(line.get("consignment_commission_pct")) <= 0 and not (
		posted and getdate(posted) >= getdate(COMMISSION_FROZEN_SINCE))


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


def _consigned(line, serials, consignment, as_of=None):
	"""The consignment facts for one sold line, or None when THAT sale was not a
	consignment sale (ffl_core.consignment.sold_on_consignment: the line's own
	frozen %; for a sale older than the freeze, the stay the sale took —
	_fetch_cost_as_of_sale's "consigned" — and the Serial No stamp only where the
	ledger has no stay to read: a later receipt of the same gun rewrites the
	stamp, and an old period run again must not re-cost its sales)."""
	from ffl_core.consignment import sold_on_consignment

	cmap, facts = consignment or {}, as_of or {}

	def held(sn):
		fact = facts.get((line.get("parent"), sn.upper()))
		if fact is not None and fact.get("consigned") is not None:
			return fact["consigned"] or None
		return cmap.get(sn)

	stamped = next((c for c in map(held, serials) if c), None)
	if not sold_on_consignment(line.get("consignment_commission_pct"), line.get("posting_date"), stamped):
		return None
	return stamped or {"is_consignment": 1, "commission_pct": 0.0}


def _line_marks(line, item_meta, consignment, as_of) -> dict:
	"""The data-only review flags of one line, from its serials' facts (_fetch_cost_as_of_sale):
	cost_pending_delivery (not delivered yet, costed at today's value; never on a consignment
	line, whose cost is the consignor's share), unledgered_cost, double_sale."""
	if not (item_meta.get(line.get("item_code")) or {}).get("is_firearm"):
		return dict.fromkeys(_LINE_MARKS, False)
	serials = split_serials(line.get("serial_no"))
	facts = [f for f in ((as_of or {}).get((line.get("parent"), sn.upper())) for sn in serials) if f]
	return {
		"cost_pending_delivery": any(f.get("pending") for f in facts)
			and not _consigned(line, serials, consignment, as_of),
		"unledgered_cost": any(f.get("unledgered") for f in facts),
		"double_sale": any(f.get("double") for f in facts),
	}


_LINE_MARKS = ("cost_pending_delivery", "unledgered_cost", "double_sale")


# ----------------------------------------------------------- row construction

def _line_cost(line, item_meta, serial_cost, consignment=None, is_ffl_transfer=False,
		transfer_serials=None, dropship_cost=None, as_of=None):
	"""(cost, missing, placeholder) for one invoice line. Trade-in credit → 0
	(not a cost of this sale; the row builders report revenue gross of the
	credit, so the allowance stays off both sides); consignment gun → net ×
	(1 − effective commission %); firearm → per-serial cost as of this sale
	(*as_of*, _fetch_cost_as_of_sale), else the serial's purchase_rate (sign
	follows qty so returns reverse it); else signed qty × valuation_rate.
	placeholder: a serial's cost came from purchase_rate because the ledger
	only holds a placeholder for it. On an FFL-transfer invoice (is_ffl_transfer,
	header flag) a net-0 firearm line is NOT flagged missing_cost: it's the
	customer's OWN gun the store never bought, so a 0 purchase_rate is
	expected, not a data gap (B)."""
	qty = flt(line.get("qty"))
	# A trade-in credit is non-cash consideration — the acquisition cost of a
	# gun taken INTO inventory (compliance locks rate == -acquisition.cost),
	# NOT a cost of THIS sale. It contributes nothing to cost, and the row
	# builders add the (negative) credit back out of net revenue, so neither
	# Net Sales nor Cost carries the allowance and profit is untouched by the
	# swap; the traded-in gun's own margin is realized when IT later sells
	# (its Serial No.purchase_rate == the allowance).
	if line.get("item_code") == TRADE_IN_CREDIT_ITEM:
		return 0.0, False, False
	meta = item_meta.get(line.get("item_code"), {})
	serials = split_serials(line.get("serial_no"))
	# A consignment gun IS a firearm-with-serial (Serial No.purchase_rate == 0
	# by design — the store never bought it), so this must be checked BEFORE
	# the firearm branch below or that branch would win with cost 0 and show a
	# phantom 100% margin. cost = net * (1 - commission%) makes profit on the
	# line equal exactly the commission the store earns.
	consigned = _consigned(line, serials, consignment, as_of)
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
		return flt(line.get("base_net_amount")) * (1 - pct / 100.0), False, False
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
			return 0.0, True, False  # no ack yet — a REAL data gap, not a fake one
		return (1 if qty >= 0 else -1) * flt(total), False, False
	if meta.get("is_firearm") and serials:
		# missing: what the ledger concluded (as_of); serial_missing: a gap on
		# the Serial No path only, the one the legacy net-0 inference below may clear.
		missing = serial_missing = placeholder = False
		total = 0.0
		xfer = transfer_serials or set()
		transfer_handover = bool(is_ffl_transfer) and flt(line.get("base_net_amount")) == 0
		for sn in serials:
			fact = (as_of or {}).get((line.get("parent"), sn.upper()))
			if fact and fact["rate"] is not None:
				total += fact["rate"]
				missing = missing or fact["missing"]
				placeholder = placeholder or fact["placeholder"]
				continue
			if fact and (fact.get("missing", True) or fact.get("no_stay")):
				# A receipt the ledger never recorded, or no stay left for this
				# sale. Handed over at $0 on a transfer ticket, it is the
				# customer's gun: nothing, no gap.
				if transfer_handover:
					continue
				missing = missing or bool(fact.get("missing", True))
				if fact.get("no_stay"):
					continue   # every stay it could take is another sale's: 0, never their cost
			# No acquisition in the ledger (a gun older than the bundles), an
			# inward voucher the report does not classify, or that unrecorded
			# receipt: the Serial No is all there is.
			#
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
				serial_missing = True
			total += rate
		# Legacy fallback, deliberately KEPT for history: guns transferred out
		# before is_transfer_gun existed carry no flag and cannot be
		# retroactively classified (we do not backfill already-delivered
		# guns). For those rows the old net-0 inference is still the only signal
		# there is, so removing it would re-flag every historical transfer as a
		# missing-cost error. New rows never reach it — their serials are caught
		# above, and a gap the ledger found is never cleared by it.
		if transfer_handover:
			serial_missing = False
		return (1 if qty >= 0 else -1) * total, missing or serial_missing, placeholder
	return qty * flt(meta.get("valuation_rate")), False, False


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
	from erpnext.stock.get_item_details import NOT_APPLICABLE_TAX, _get_item_tax_map

	# rate 1 stands in for "the ticket's rate": only what the template does to it matters (0 / not applicable).
	tax_map = _get_item_tax_map(doc={"company": company, "taxes": [{"account_head": a, "rate": 1} for a in sales_tax_accounts]},
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
		if l.get("item_code") not in (TRADE_IN_CREDIT_ITEM, *DEPOSIT_ITEMS)
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

	A deposit row (gunsmith work order, pre-order: deposits.ITEMS) is customer money
	held, never revenue: the collecting invoice is all deposit (+100 → Net 0) and the
	pick-up invoice releases it (−100 → the work's full amount). deposit_net comes out
	of the net, the same way a trade-in allowance does.

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
			if l.get("item_code") in DEPOSIT_ITEMS),
		"order_ref": h.get("po_no") or h.get("woo_online_order") or h.get("gunbroker_order") or "",
	}


def _net_sales(h, facts) -> float:
	"""The one Net Sales expression: the invoice's net, gross of trade-in, without deposits."""
	return flt(h.get("base_net_total")) - facts["trade_in_net"] - facts["deposit_net"]


def net_sales_of(doctype: str, filters: dict) -> dict[str, float]:
	"""Net Sales per invoice, as the report reads it, for the *doctype* (Sales Invoice or POS
	Invoice) invoices *filters* picks. The caller says which invoices count: the report's own
	rule is docstatus 1, no consolidated invoice, no counter pickup ticket. A credit note reads
	negative."""
	headers = frappe.get_all(doctype, filters=filters, fields=["name", "base_net_total"])
	lines: dict[str, list] = {}
	if headers:
		for row in frappe.get_all(f"{doctype} Item", fields=["parent", "item_code", "base_net_amount"],
				filters={"parent": ["in", [h.name for h in headers]],
					"item_code": ["in", [TRADE_IN_CREDIT_ITEM, *DEPOSIT_ITEMS]]}):
			lines.setdefault(row.parent, []).append(row)
	return {h.name: _net_sales(h, _order_facts(h, lines.get(h.name, []), {})) for h in headers}


def _build_rows(headers, lines_by_invoice, item_meta, serial_cost, product_type, consignment=None,
		transfer_serials=None, dropship_cost=None, taxes_by_invoice=None, as_of=None):
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
		missing = placeholder = False
		marks = dict.fromkeys(_LINE_MARKS, False)
		serials = []
		for line in lines:
			qty += flt(line.get("qty"))
			line_cost, line_missing, line_placeholder = _line_cost(line, item_meta, serial_cost, consignment,
				is_ffl_transfer=h.get("is_ffl_transfer"),
				transfer_serials=transfer_serials, dropship_cost=dropship_cost, as_of=as_of)
			cost += line_cost
			missing = missing or line_missing
			placeholder = placeholder or line_placeholder
			marks = {k: v or marks[k] for k, v in _line_marks(line, item_meta, consignment, as_of).items()}
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
		net = _net_sales(h, f)
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
			# Data-only (no column): a gun on the row is costed from its Serial No
			# over a placeholder ledger value (_fetch_cost_as_of_sale).
			"placeholder_cost": placeholder,
			# Data-only (no column), any line of the sale (_line_marks):
			# cost_pending_delivery — a gun not delivered yet, costed at today's value;
			# unledgered_cost — costed from the Serial No's current marks (Cost Flags);
			# double_sale — the gun is on another live sale of the same stay (review only).
			**marks,
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
			"is_consignment": any(_consigned(l, split_serials(l.get("serial_no")), consignment, as_of) for l in lines),
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
		# invoices, so two flagged lines of one order count once) + unclassified rows
		# + double_sale product rows + unledgered_cost product rows.
		products = [r for r in body if r.get("row_kind") == "product"]
		n_missing = sum(1 for r in products if r.get("missing_cost"))
		n_mismatch = len({r.get("invoice") for r in body if r.get("tax_mismatch")})
		n_uncl = sum(1 for r in body if r.get("row_kind") == "unclassified")
		n_review = sum(1 for r in products if r.get("double_sale")) + sum(1 for r in products if r.get("unledgered_cost"))
		cards = [
			_card(net, _("Net Sales"), "Currency", "Blue"),
			_card(cost, _("Cost"), "Currency", "Grey"),
			_card(profit, _("Goods Profit"), "Currency", "Green" if profit >= 0 else "Red"),
			_card(margin, _("Margin %"), "Percent", "Green" if margin >= 0 else "Red"),
			_card(flt(total.get("sales_tax")), _("Sales Tax"), "Currency", "Grey"),
			_card(flt(total.get("shipping")), _("Shipping"), "Currency", "Grey"),
		]
		# Only where an excise tax was collected, so a store without one sees no new card.
		if flt(total.get("excise_tax")):
			cards.insert(5, _card(flt(total.get("excise_tax")), _("Excise Tax"), "Currency", "Grey"))
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
		cards.append(_flag_card(n_missing + n_mismatch + n_uncl + n_review, _("Data Flags")))
		return cards

	# Order view (5 cards) — reads the Order Total row's own net_sales / margin.
	profit = flt(total.get("profit"))
	margin = flt(total.get("margin"))
	# Cost Flags = rows with missing_cost + rows costed from the Serial No's current marks (unledgered_cost).
	n_flags = sum(1 for r in body if r.get("missing_cost")) + sum(1 for r in body if r.get("unledgered_cost"))
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
# A state excise tax collected beside sales tax (California's CFET): its own liability, never
# summed into sales tax (setup.tax.sales_tax_and_excise_accounts).
_EXCISE = "excise"
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
		account_name=None, excise_accounts=()):
	"""Positively classify a taxes-row account_head against the company's OWN
	canonical accounts: the shipping-income account
	(ffl_core.setup.shipping_charge.shipping_account_name — the very account the
	shipping row is booked to) and the sales-tax template's account_heads
	(ffl_core.setup.tax._company_sales_tax_accounts). Anything else fails closed
	→ unclassified. This is deliberately NOT type-based (account_type=="Tax" /
	root_type=="Income"), which fails OPEN: a Tax-type federal excise tax would be
	swept into collected sales tax, and an Income-type handling/restocking fee
	into shipping. Only the resolved account_heads are trusted. An excise account
	(setup.tax.sales_tax_and_excise_accounts) is checked first: it may sit in the
	sales-tax template too."""
	if account_head == shipping_account:
		return _SHIPPING
	if account_head in excise_accounts:
		return _EXCISE
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
	line_excise: dict[str, float] = {}
	for w in iwt_rows:
		# Only sales-tax allocations become a line's tax; a shipping row's
		# per-item allocation is deliberately ignored (shipping is order-level).
		cls = row_class.get(w.get("tax_row"))
		if cls in (_SALES_TAX, _EXCISE):
			bucket = line_tax if cls == _SALES_TAX else line_excise
			key = w.get("item_row")
			bucket[key] = flt(bucket.get(key)) + flt(w.get("amount"))
	# Goods sales tax = Σ IWTD (per-line, post-discount, GL-consistent).
	goods_tax_total = sum(line_tax.values())
	shipping_total = freight_tax_total = sales_tax_charged = 0.0
	marketplace_tax_total = marketplace_fee_total = excise_total = 0.0
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
		# Excise (every row on the account, freight included) is its own total.
		elif cls == _EXCISE:
			excise_total += amt
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
		# The charged figures beside the IWTD-based ones above.
		"sales_tax_charged": sales_tax_charged,
		"line_excise": line_excise, "excise_total": excise_total,
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
		marketplace_exempt=_fetch_marketplace_exempt(headers),
		as_of=_fetch_cost_as_of_sale(headers, lines, item_meta))
	columns = _product_columns(detail)
	# The Excise Tax column only where the period collected some: a store without one sees none.
	if not any(flt(r.get("excise_tax")) for r in rows):
		columns = [c for c in columns if c["fieldname"] != "excise_tax"]
	return columns, rows


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
	canon: dict[str, tuple] = {}  # company -> (shipping_account, sales-tax set, excise set)
	out = {}
	for head in heads:
		company = company_of.get(head)
		if not company:
			out[head] = _UNCLASSIFIED
			continue
		if company not in canon:
			sales, excise = sales_tax_and_excise_accounts(company)
			canon[company] = (shipping_account_name(company), set(sales), set(excise))
		ship, taxset, exciseset = canon[company]
		out[head] = _classify_account(head, ship, taxset,
			account_name=(accounts.get(head) or {}).get("account_name"), excise_accounts=exciseset)
	return out


def _build_product_rows(headers, lines_by_invoice, item_meta, serial_cost, consignment,
                        iwt_by_invoice, taxes_by_invoice, acct_class, product_type="",
                        detail=False, transfer_serials=None, dropship_cost=None,
                        marketplace_exempt=None, as_of=None):
	"""Product-level rows. Product flat (detail=False): one row per order×line +
	an order-level Shipping row + fail-closed Unclassified rows. Order Detail
	(detail=True): the same lines indented under an order parent row that mirrors
	the Order view's net_sales/total. A Totals row carries each reconcilable
	component separately (gross net, trade-in, shipping, sales tax, cost)."""
	rows = []
	child_indent = 1 if detail else 0
	tot_net = tot_trade = tot_ship = tot_tax = tot_cost = tot_uncl = tot_excise = 0.0
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
		order_missing = order_placeholder = False
		order_marks = dict.fromkeys(_LINE_MARKS, False)
		product_rows = []
		for line in lines:
			if line.get("item_code") in DEPOSIT_ITEMS:
				# Money held for the customer, not a product line: it would report a
				# negative-price "item" with 100% margin. It comes off order_net below.
				continue
			meta = item_meta.get(line.get("item_code"), {})
			line_cost, line_missing, line_placeholder = _line_cost(line, item_meta, serial_cost, consignment,
				is_ffl_transfer=h.get("is_ffl_transfer"),
				transfer_serials=transfer_serials, dropship_cost=dropship_cost, as_of=as_of)
			order_qty += flt(line.get("qty"))
			order_cost += line_cost
			order_missing = order_missing or line_missing
			order_placeholder = order_placeholder or line_placeholder
			line_marks = _line_marks(line, item_meta, consignment, as_of)
			order_marks = {k: v or order_marks[k] for k, v in line_marks.items()}
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
				"excise_tax": flt(bd["line_excise"].get(line.get("name"))),
				"cost": line_cost,
				"profit": gross - line_cost,
				"is_firearm": bool(meta.get("is_firearm")),
				"is_trade_in": is_trade_in,
				"is_consignment": bool(_consigned(line, serials, consignment, as_of)),
				"missing_cost": line_missing,
				"placeholder_cost": line_placeholder,
				**line_marks,
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
				"excise_tax": bd["excise_total"],
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
				"placeholder_cost": order_placeholder,
				**order_marks,
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
		tot_excise += bd["excise_total"]
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
			"excise_tax": tot_excise,
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
			{"label": _("Excise Tax"), "fieldname": "excise_tax", "fieldtype": "Currency", "width": 95},
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
		{"label": _("Excise Tax"), "fieldname": "excise_tax", "fieldtype": "Currency", "width": 95},
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
