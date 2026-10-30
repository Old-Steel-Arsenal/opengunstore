"""GunBroker Statement Reconciliation — the month-end check against the
marketplace's own two bills.

GunBroker bills in two documents, and they arrive by different routes:

  Sales Tax statement   issued monthly, remitted by us about 10–14 days later.
                        Reconciles to the credit side of
                        `GunBroker MPF Sales Tax Payable`.
  Fee bill              issued around the 2nd, auto-debited from the account on
                        file around the 20th. Reconciles to
                        `Accrued GunBroker Fees` plus, possibly, the buyer
                        service fee liability — which of the two documents
                        carries that fee is still unverified (appendix #7), so
                        this report shows the accrued fee and the service fee
                        as separate figures rather than guessing.

Neither document is available through the API: there is no per-order fee
endpoint, only an account-level balance. So the operator downloads or copies
the statement and this report reads it.

**Order by order and item by item, never month against month**. An
order sold on 30 January and shipped on 2 February is on January's statement
and in February's ledger. Reconciling monthly totals reports every one of those
as a difference and buries the real ones; reconciling per order reports it as
what it is — a timing gap with both sides agreeing on the amount. That is why
`timing` is a status of its own and not a rounding tolerance.

**The export format has not been sampled.** Nobody has yet pulled a real
statement out of the GunBroker back office (appendix #9 is open), so the parser
reads headers by meaning rather than position, tolerates the byte-order mark
Excel writes, takes a pasted table as readily as a file, and refuses a file it
cannot find an order column in rather than reporting the entire month missing.
When the first real statement lands, expect to add aliases to `_FIELD_ALIASES`
and nothing else.
"""
from __future__ import annotations

import csv
import io
import re

import frappe
from frappe import _
from frappe.utils import flt, getdate

from ffl_core import report_cards
from ffl_core.marketplace_accounts import ORDER_DOCTYPE

FEE_STATEMENT = "Fee Statement"
TAX_STATEMENT = "Sales Tax Statement"
STATEMENT_TYPES = (FEE_STATEMENT, TAX_STATEMENT)

# Header spellings seen or plausible, normalised to letters and digits only.
# Read by meaning because the real export's spelling is unknown; when it turns
# up, this table is the only thing that should need to change.
_FIELD_ALIASES = {
	"order_id": ("orderid", "ordernumber", "order", "orderno", "gborderid",
		"ordernum", "salesorderid"),
	"item_id": ("itemid", "itemnumber", "item", "itemno", "listingid",
		"gbitemid", "auctionid"),
	"amount": ("amount", "fee", "feeamount", "total", "totalamount", "charge",
		"salestax", "tax", "taxamount", "netamount", "amountusd"),
	"date": ("date", "transactiondate", "postingdate", "postdate",
		"settlementdate", "invoicedate", "orderdate"),
	"description": ("description", "type", "feetype", "memo", "details",
		"transactiontype", "item description"),
}

_MATCH_TOLERANCE = 0.01


# ------------------------------------------------------------------ the page

def execute(filters=None):
	filters = frappe._dict(filters or {})
	statement_type = filters.get("statement_type") or FEE_STATEMENT
	if statement_type not in STATEMENT_TYPES:
		frappe.throw(_("Unknown statement type {0} — choose one of: {1}").format(
			statement_type, ", ".join(STATEMENT_TYPES)))
	if not filters.get("from_date") or not filters.get("to_date"):
		frappe.throw(_(
			"From Date and To Date are required — they are the statement's own "
			"period, and they are what separates a timing gap from a real "
			"difference."))
	if getdate(filters.from_date) > getdate(filters.to_date):
		frappe.throw(_("From Date must be on or before To Date"))

	statement = _parse_statement(_statement_source(filters))
	# The statement's own orders are fetched whatever their posting date: an
	# order on January's statement whose invoice posted on 2 February is a
	# timing gap, and only reaches `_classify` if the fetch returns it.
	ids = sorted({_id(r.get("order_id")) for r in statement} - {""})
	pos_rows = (_fetch_pos_fees(filters, ids) if statement_type == FEE_STATEMENT
		else _fetch_pos_tax(filters, ids))
	rows = _reconcile(statement, pos_rows,
		from_date=filters.from_date, to_date=filters.to_date)
	return _columns(), rows, _preamble(statement_type), None, _summary(rows)


def _statement_source(filters):
	"""The statement itself — from the attached file if there is one, else from
	the paste box. Both are offered because the operator may have a download or
	may only be able to select the table on the page."""
	url = filters.get("statement_file")
	if url:
		return _read_attachment(url)
	text = filters.get("statement_text")
	if text:
		return text
	frappe.throw(_(
		"Attach the GunBroker statement or paste it into Statement Text. There "
		"is no API for either document — the fee and tax statements only exist "
		"as downloads from the GunBroker back office."))


def _read_attachment(file_url):
	# lazy: whole file into memory, no size cap. A month of GunBroker
	# statements is a few hundred rows; if these ever arrive as a yearly export
	# this wants streaming, or a row cap with a message saying so.
	name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not name:
		frappe.throw(_("Attachment {0} not found").format(file_url))
	content = frappe.get_doc("File", name).get_content()
	if isinstance(content, bytes):
		# utf-8-sig: a marketplace statement will have been through Excel, and
		# Excel writes a byte-order mark. Left in place it becomes part of the
		# first header, the order column goes unrecognised, and every single
		# row reconciles as missing.
		try:
			return content.decode("utf-8-sig")
		except UnicodeDecodeError:
			return content.decode("latin-1")
	return content


def _preamble(statement_type):
	which = (_("the fee bill (issued about the 2nd, auto-debited about the 20th)")
		if statement_type == FEE_STATEMENT
		else _("the sales-tax statement (remitted 10–14 days after issue)"))
	return (
		'<div class="text-muted" style="line-height:1.6">'
		+ _("Reconciling <b>{0}</b> — {1} — order by order and item by item, "
			"never month against month: an order sold on the 30th and shipped "
			"on the 2nd sits in different months on the two sides, and that is "
			"a <b>timing</b> row, not a difference.").format(statement_type, which)
		+ "<br>"
		+ _("The export format has <b>not yet been sampled</b> (appendix #9). "
			"Columns are matched by meaning, so a statement whose headers are "
			"spelled differently should still parse — but if the row count "
			"looks wrong, check the parse before concluding the accruals are.")
		+ "</div>")


def _columns():
	return [
		{"label": _("Status"), "fieldname": "status_label", "fieldtype": "Data", "width": 165},
		{"label": _("Order #"), "fieldname": "order_id", "fieldtype": "Data", "width": 110},
		{"label": _("Item #"), "fieldname": "item_id", "fieldtype": "Data", "width": 110},
		{"label": _("Statement"), "fieldname": "statement_amount", "fieldtype": "Currency", "width": 115},
		{"label": _("POS"), "fieldname": "pos_amount", "fieldtype": "Currency", "width": 115},
		{"label": _("Difference"), "fieldname": "difference", "fieldtype": "Currency", "width": 110},
		{"label": _("POS Document"), "fieldname": "reference", "fieldtype": "Data", "width": 190},
		{"label": _("Accrual"), "fieldname": "accrual", "fieldtype": "Data", "width": 95},
		{"label": _("POS Date"), "fieldname": "posting_date", "fieldtype": "Date", "width": 100},
		{"label": _("Statement Date"), "fieldname": "statement_date", "fieldtype": "Data", "width": 110},
		{"label": _("Statement Line"), "fieldname": "description", "fieldtype": "Data", "width": 200},
	]


# ------------------------------------------------------------------- parsing

def _norm(text):
	"""A header down to its letters and digits.

	This is also what defuses the byte-order mark Excel puts at the top of a
	CSV: a pasted "\ufeffOrderID" normalises to "orderid" like any other
	spelling. Left recognisable, the BOM makes the FIRST header — usually the
	order number — unmatchable, and every row then reconciles as missing, which
	reads as a total billing failure rather than a wrong paste. Keep the
	character class a whitelist; a blacklist would let it back in."""
	return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _parse_money(text):
	"""Money out of whatever the export puts in the cell.

	Parentheses are accounting notation for a credit; read as a positive number
	a fee reversal flips sign and the difference reads as double the fee.
	Anything unparseable is zero rather than an exception — a totals row or a
	footnote at the foot of the file must not take the month down with it."""
	if text is None:
		return 0.0
	raw = str(text).strip()
	if not raw:
		return 0.0
	negative = raw.startswith("(") and raw.endswith(")")
	cleaned = re.sub(r"[^0-9.\-]", "", raw)
	if cleaned in ("", "-", ".", "-."):
		return 0.0
	try:
		value = float(cleaned)
	except ValueError:
		return 0.0
	return -abs(value) if negative else value


def _delimiter(sample):
	"""Comma or tab. Sniffed rather than assumed because a table pasted out of
	a browser or a spreadsheet arrives tab-separated, and the paste box is half
	of how this report is fed."""
	head = sample.splitlines()[0] if sample.splitlines() else ""
	return "\t" if head.count("\t") > head.count(",") else ","


def _parse_statement(text):
	"""Statement rows as {order_id, item_id, amount, date, description}.

	Refuses a file with no recognisable order column. The alternative —
	returning rows keyed on nothing — reports every order in the period as
	missing from the statement, which reads as a catastrophic billing failure
	and is really a wrong file."""
	body = (text or "").strip()
	if not body:
		frappe.throw(_("The statement is empty — attach a file or paste the table."))
	reader = csv.reader(io.StringIO(body), delimiter=_delimiter(body))
	try:
		header = next(reader)
	except StopIteration:
		frappe.throw(_("The statement is empty — attach a file or paste the table."))
	position = {}
	for index, name in enumerate(header):
		key = _norm(name)
		for field, aliases in _FIELD_ALIASES.items():
			if field in position:
				continue
			if key in {_norm(a) for a in aliases}:
				position[field] = index
	if "order_id" not in position:
		frappe.throw(_(
			"No order column found in the statement. Looked for one of: {0}. "
			"Headers read: {1}."
		).format(", ".join(_FIELD_ALIASES["order_id"]), ", ".join(header) or "—"))

	def cell(row, field):
		index = position.get(field)
		if index is None or index >= len(row):
			return ""
		return str(row[index] or "").strip()

	rows = []
	for raw in reader:
		if not any(str(c or "").strip() for c in raw):
			continue
		order_id = cell(raw, "order_id")
		if not order_id:
			continue
		rows.append({
			"order_id": order_id,
			"item_id": cell(raw, "item_id"),
			"amount": _parse_money(cell(raw, "amount")),
			"date": cell(raw, "date") or None,
			"description": cell(raw, "description"),
		})
	return rows


# ------------------------------------------------------------- reconciliation

def _status_label(status):
	# Built per call, not at import: a module-level _() binds whatever language
	# happened to be active when the worker first imported the module, and then
	# every later request gets that one.
	return {
		"matched": _("Matched"),
		"amount": _("Amount differs"),
		"timing": _("Timing — different month"),
		"missing_in_pos": _("On statement, not in POS"),
		"missing_on_statement": _("In POS, not on statement"),
	}[status]


_TRAILING_ZERO_DECIMAL = re.compile(r"^(\d+)\.0+$")


def _id(raw):
	"""One side's order or item number, in a form the other side can match.

	Trims, and undoes the one thing a spreadsheet reliably does to a column of
	numbers: renders `8888888` as `8888888.0`. When that happens every row
	lands in `missing_in_pos` AND `missing_on_statement` at once — the "whole
	month is missing" misreading that refusing a headerless file exists to
	prevent, arrived at by a different route.

	Deliberately nothing cleverer. Stripping non-digits would collapse
	`GB-1001` and `1001`; re-adding lost leading zeros would need a width
	nobody has. Only an integer with a decimal point and zeros after it."""
	text = str(raw or "").strip()
	match = _TRAILING_ZERO_DECIMAL.match(text)
	return match.group(1) if match else text


def _key(order_id, item_id):
	return (_id(order_id), _id(item_id))


def _reconcile(statement_rows, pos_rows, *, from_date, to_date):
	"""The three difference classes, per order and per item.

	Statement lines for the same item are added up first: GunBroker bills the
	value fee and the FFL document fee as separate lines while we accrue their
	sum, so comparing line against line would report two differences on every
	order that ever shipped.

	A statement line carrying no item number is matched against the order's
	total — that is the shape of the tax statement, which is per order.

	Each POS row also carries the LEDGER state of its accrual. What is being
	compared is a figure the order computed, and the thing the reconciliation
	is really asking about is the accrual on the books; the two come apart
	exactly when it matters — the journal failed to post, or was reversed on a
	cancellation and never re-raised — and both of those show as a clean match
	on the numbers alone."""
	statement = {}
	for row in statement_rows:
		key = _key(row.get("order_id"), row.get("item_id"))
		entry = statement.setdefault(key, {"amount": 0.0, "date": None,
			"description": ""})
		entry["amount"] += flt(row.get("amount"))
		entry["date"] = entry["date"] or row.get("date")
		entry["description"] = "; ".join(
			p for p in (entry["description"], row.get("description")) if p)

	pos_by_key, pos_by_order = {}, {}
	for row in pos_rows:
		key = _key(row.get("order_id"), row.get("item_id"))
		pos_by_key[key] = {"amount": flt(pos_by_key.get(key, {}).get("amount"))
			+ flt(row.get("amount")),
			"posting_date": row.get("posting_date"),
			"reference": row.get("reference"),
			"accrual": row.get("accrual") or ""}
		order = pos_by_order.setdefault(key[0], {"amount": 0.0,
			"posting_date": row.get("posting_date"), "reference": row.get("reference"),
			"accrual": row.get("accrual") or ""})
		order["amount"] += flt(row.get("amount"))

	rows, consumed_keys, consumed_orders = [], set(), set()
	for key in sorted(statement):
		order_id, item_id = key
		entry = statement[key]
		if item_id and key in pos_by_key:
			pos, matched_key = pos_by_key[key], key
		elif not item_id and order_id in pos_by_order:
			pos, matched_key = pos_by_order[order_id], None
		else:
			rows.append(_row("missing_in_pos", order_id, item_id,
				statement_amount=entry["amount"], pos_amount=0.0,
				statement_date=entry["date"], description=entry["description"]))
			continue
		if matched_key:
			consumed_keys.add(matched_key)
		else:
			consumed_orders.add(order_id)
		difference = flt(entry["amount"] - flt(pos.get("amount")), 2)
		status = _classify(difference, pos.get("posting_date"), from_date, to_date)
		rows.append(_row(status, order_id, item_id,
			statement_amount=entry["amount"], pos_amount=flt(pos.get("amount")),
			difference=difference, reference=pos.get("reference"),
			accrual=pos.get("accrual"),
			posting_date=pos.get("posting_date"), statement_date=entry["date"],
			description=entry["description"]))

	for key in sorted(pos_by_key):
		if key in consumed_keys or key[0] in consumed_orders:
			continue
		pos = pos_by_key[key]
		rows.append(_row("missing_on_statement", key[0], key[1],
			statement_amount=0.0, pos_amount=flt(pos.get("amount")),
			difference=flt(-flt(pos.get("amount")), 2),
			reference=pos.get("reference"), accrual=pos.get("accrual"),
			posting_date=pos.get("posting_date")))
	return rows


def _classify(difference, posting_date, from_date, to_date):
	"""Matched / amount / timing.

	Timing is checked only when the amounts already agree. A real difference on
	an order that also straddles the month end is still a difference — timing
	must not become a place for wrong numbers to hide."""
	if abs(difference) > _MATCH_TOLERANCE:
		return "amount"
	if posting_date and not (getdate(from_date) <= getdate(posting_date) <= getdate(to_date)):
		return "timing"
	return "matched"


def _row(status, order_id, item_id, *, statement_amount=0.0, pos_amount=0.0,
		difference=None, reference=None, posting_date=None, statement_date=None,
		description="", accrual=""):
	if difference is None:
		difference = flt(statement_amount - pos_amount, 2)
	return {"status": status, "status_label": _status_label(status),
		"order_id": order_id, "item_id": item_id,
		"statement_amount": flt(statement_amount, 2),
		"pos_amount": flt(pos_amount, 2), "difference": difference,
		"reference": reference or "", "accrual": accrual or "",
		"posting_date": posting_date,
		"statement_date": statement_date or "", "description": description or ""}


def _summary(rows):
	"""Two totals and the three counts. The totals first because the first
	thing anyone asks is whether the bill adds up to what we expected; the
	counts because a zero has to be visibly a zero, not an absent row."""
	def count(status):
		return sum(1 for r in rows if r.get("status") == status)

	def flag(status, label):
		return report_cards.flag_card(count(status), label)

	card = report_cards.card
	return [
		card(flt(sum(r.get("statement_amount") or 0 for r in rows), 2),
			_("Statement total"), "Currency", "Blue"),
		card(flt(sum(r.get("pos_amount") or 0 for r in rows), 2),
			_("POS total"), "Currency", "Blue"),
		flag("amount", _("Amount differs")),
		flag("missing_in_pos", _("Missing in POS")),
		flag("missing_on_statement", _("Missing on statement")),
		card(count("timing"), _("Timing gaps"), None, "Orange"),
	]


# ----------------------------------------------------------------- data fetch

def _gb_installed():
	return frappe.db.table_exists(ORDER_DOCTYPE)


# The per-item fee is written when the invoice posts, so an order still
# waiting for payment or an FFL carries 0.00. Including those turns every
# unshipped order into a red row — "GunBroker is billing us 57 and we accrued
# nothing" — on an order that has simply not shipped yet. The tax side is
# worse: its figure IS populated before posting, so an unpaid order shows as a
# real amount the statement has never heard of. Both sides therefore INNER JOIN
# a submitted invoice, and the month-end note explains that a statement line
# with no POS side is usually next month's.
_ACCRUAL_STATE = """
		CASE WHEN COALESCE(o.fee_accrual_je, '') = '' THEN 'none'
		     WHEN je.docstatus = 2 THEN 'cancelled'
		     WHEN EXISTS (SELECT 1 FROM `tabJournal Entry` rev
		                  WHERE rev.reversal_of = o.fee_accrual_je
		                    AND rev.docstatus = 1) THEN 'reversed'
		     ELSE 'posted' END AS accrual"""


def _period_or_listed(ids):
	"""Invoices posted in the period, plus any order the statement lists — so a
	month-straddling order comes back and is classified as timing, not as
	missing. The OR is left out when there are no ids: `IN ()` is a SQL error."""
	period = "si.posting_date BETWEEN %(from_date)s AND %(to_date)s"
	return f"({period} OR o.gb_order_id IN %(ids)s)" if ids else period


def _fetch_pos_fees(filters, ids=()):
	"""What we accrued, per item: the value fee plus the FFL document fee that
	`GunBroker Order Line` carries. Per item because that is how GunBroker
	charges — the tiers are applied to each item's own price, and an order-level
	total would reconcile a figure the bill never shows.

	The accrual's ledger state rides along. The amounts on both sides are
	derived from the same order, so they agree even when the journal behind
	them never posted or was reversed and never re-raised — which is precisely
	what this check is meant to catch."""
	if not _gb_installed():
		return []
	return [{
		"order_id": r["gb_order_id"], "item_id": r["gb_item_id"] or "",
		"amount": flt(r["fvf_amount"]) + flt(r["ffl_doc_fee_amount"]),
		"posting_date": r["posting_date"],
		"reference": r["sales_invoice"] or r["name"],
		"accrual": r["accrual"],
	} for r in frappe.db.sql(f"""
		SELECT o.name, o.gb_order_id, o.sales_invoice,
		       l.gb_item_id, l.fvf_amount, l.ffl_doc_fee_amount,
		       si.posting_date,{_ACCRUAL_STATE}
		FROM `tabGunBroker Order` o
		JOIN `tabGunBroker Order Line` l ON l.parent = o.name
		JOIN `tabSales Invoice` si ON si.name = o.sales_invoice AND si.docstatus = 1
		LEFT JOIN `tabJournal Entry` je ON je.name = o.fee_accrual_je
		WHERE {_period_or_listed(ids)}
	""", {"from_date": filters.from_date, "to_date": filters.to_date,
		"ids": tuple(ids)}, as_dict=True)]


def _fetch_pos_tax(filters, ids=()):
	"""What the marketplace collected in sales tax on our behalf, per order —
	`salesTaxTotal` less the buyer service fee, the same split the invoice's
	two liability rows carry.

	The service fee is NOT added in: which of the two statements it appears on
	is unverified (appendix #7). If it turns out to be on this one, the figure
	here is short by it and the two item-6 labels swap — both noted in the
	month-end checklist's open items."""
	if not _gb_installed():
		return []
	return [{
		"order_id": r["gb_order_id"], "item_id": "",
		"amount": flt(r["sales_tax_total"]) - flt(r["compliance_fee"]),
		"posting_date": r["posting_date"],
		"reference": r["sales_invoice"] or r["name"],
		"accrual": r["accrual"],
	} for r in frappe.db.sql(f"""
		SELECT o.name, o.gb_order_id, o.sales_invoice,
		       o.sales_tax_total, o.compliance_fee, si.posting_date,{_ACCRUAL_STATE}
		FROM `tabGunBroker Order` o
		JOIN `tabSales Invoice` si ON si.name = o.sales_invoice AND si.docstatus = 1
		LEFT JOIN `tabJournal Entry` je ON je.name = o.fee_accrual_je
		WHERE {_period_or_listed(ids)}
	""", {"from_date": filters.from_date, "to_date": filters.to_date,
		"ids": tuple(ids)}, as_dict=True)]
