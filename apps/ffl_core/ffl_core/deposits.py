"""Money a customer pays at the register towards something not handed over yet — a gunsmith
work order (``ffl_core.gunsmith``) or a pre-order (``ffl_core.preorder``).

One mechanism for both, told apart by a :class:`Kind`: a locked line of the kind's own item
(``setup/locked_lines``) on a register sale, linked to its source by the kind's link field (on the
line, and on the invoice header once a guard has checked it), booked to the kind's own liability
account and never taxed. What is held for a source is read from the submitted POS Invoices'
lines (:func:`held`): a payment +, a return −, a release on the bill that settles it −. There is
no balance document to keep in step.

The sources decide their own rules (which statuses take a payment, what the cap is); this module
holds what they share: the held sum, the open drafts, the line, the draft invoice, the locked
amount, and the returns that pay it back.
"""
from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import cint, flt

from ffl_core.setup import gunsmith as gunsmith_setup
from ffl_core.setup import preorder as preorder_setup
from ffl_core.setup.customer_deposits import DEPOSIT_ACCOUNT_TITLE, deposit_account
from ffl_core.setup.locked_lines import LOCKED_RATE_FIELD

TOL = 0.005


@dataclass(frozen=True)
class Kind:
	item: str            # the locked line's item
	link_field: str      # on POS Invoice, POS Invoice Item and Sales Invoice Item
	doctype: str         # what the money is held for
	account_title: str   # the company's liability account it books to


GUNSMITH = Kind(gunsmith_setup.DEPOSIT_ITEM, gunsmith_setup.LINK_FIELD, "Gunsmith Work Order", DEPOSIT_ACCOUNT_TITLE)
PREORDER = Kind(preorder_setup.PREORDER_ITEM, preorder_setup.LINK_FIELD, "Sales Order", preorder_setup.ACCOUNT_TITLE)
KINDS = (GUNSMITH, PREORDER)
# Every deposit item: customer money held, never a sale (the sales reports leave them out of Net Sales).
ITEMS = tuple(k.item for k in KINDS)


def kind_of(item_code: str | None) -> Kind | None:
	return next((k for k in KINDS if k.item == item_code), None)


def account(kind: Kind, company: str) -> str | None:
	"""The company's liability account for *kind*, if created."""
	return deposit_account(company, kind.account_title)


def held(kind: Kind, name: str, exclude: str | None = None, *, for_update: bool = False) -> float:
	"""What is held for *name*: the sum of its *kind* lines on submitted POS Invoices (a return's
	line is negative). *exclude* leaves one invoice out (the one being validated). *for_update*
	makes it a locking read, which sees rows committed after this transaction's snapshot — the
	plain read under REPEATABLE READ does not."""
	rows = frappe.db.sql("""
		SELECT COALESCE(SUM(i.amount), 0) FROM `tabPOS Invoice Item` i
		INNER JOIN `tabPOS Invoice` p ON p.name = i.parent
		WHERE p.docstatus = 1 AND i.item_code = %s AND i.{link} = %s AND p.name != %s{lock}
	""".format(link=kind.link_field, lock=" FOR UPDATE" if for_update else ""),
		(kind.item, name, exclude or ""))
	return flt(rows[0][0] if rows else 0, 2)


def open_draft(kind: Kind, name: str, which: str) -> str | None:
	"""The open draft payment (``deposit``), bill (``pickup``) or refund (``refund``) invoice."""
	found = open_drafts(kind, name, which)
	return found[0] if found else None


def open_drafts(kind: Kind, name: str, which: str) -> list[str]:
	"""Every open draft of one sort (see :func:`open_draft`), oldest first."""
	if not frappe.db.has_column("POS Invoice", kind.link_field):
		return []
	out = []
	for row in frappe.get_all("POS Invoice", filters={kind.link_field: name, "docstatus": 0},
			fields=["name", "is_return"], order_by="creation"):
		amounts = frappe.get_all("POS Invoice Item", filters={"parent": row.name, "item_code": kind.item},
			pluck="amount")
		if which == "refund" and row.is_return and any(flt(a) < 0 for a in amounts):
			# A return of other goods on a payment ticket is not this source's refund.
			out.append(row.name)
		elif which == "deposit" and not row.is_return and any(flt(a) > 0 for a in amounts):
			out.append(row.name)
		elif which == "pickup" and not row.is_return and not any(flt(a) > 0 for a in amounts):
			out.append(row.name)
	return out


def draft_amount(kind: Kind, invoice: str) -> float:
	"""The *kind* lines' total on one invoice."""
	return flt(sum(flt(a) for a in frappe.get_all("POS Invoice Item",
		filters={"parent": invoice, "item_code": kind.item}, pluck="amount")), 2)


def line(kind: Kind, doc, amount: float, description: str) -> dict:
	"""The locked line paying *amount* towards *doc* (negative: releasing it on the bill)."""
	acct = account(kind, doc.company)
	if not acct:
		frappe.throw(_("{0} has no {1} account yet. Run bench migrate, or ask a manager.").format(
			doc.company, kind.account_title))
	return {"item_code": kind.item, "qty": 1, "rate": amount, LOCKED_RATE_FIELD: amount, "price_list_rate": 0,
		"income_account": acct, kind.link_field: doc.name, "description": description}


def new_invoice(kind: Kind, doc, items: list[dict]):
	"""A draft register invoice for *doc*'s customer on the open shift, linked to *doc*."""
	from ffl_core.pos_draft import open_shift, profile_defaults

	shift = open_shift(doc.company)
	inv = frappe.get_doc({
		"doctype": "POS Invoice", "company": doc.company, "pos_profile": shift.pos_profile,
		"customer": doc.customer, kind.link_field: doc.name, "items": items,
		**profile_defaults(shift.pos_profile),
	})
	inv.flags.ignore_permissions = True
	inv.set_missing_values()
	return inv


def expected_rate(kind: Kind, doc, row) -> float | None:
	"""``compliance/locked_lines``: the amount a *kind* line is locked to, or None to leave it to
	the source's guard. A payment (+) is what was stamped on it when it was made; a release (−) is
	what was locked when the bill was built, never more than is held. A return is the return's."""
	name = (row.get(kind.link_field) if hasattr(row, "get") else getattr(row, kind.link_field, None)) or ""
	if not name or cint(getattr(doc, "is_return", 0) or 0):
		return None
	locked = flt(getattr(row, LOCKED_RATE_FIELD, 0))
	if locked > 0:
		return flt(locked, 2)
	if locked < 0:
		have = held(kind, name, exclude=getattr(doc, "name", None))
		return -min(-locked, have) if have > TOL else None
	return None


def sources_on(kind: Kind, doc) -> list[str]:
	"""The sources an invoice touches: its header link, then its rows' (a register sale may carry a
	payment next to other goods, and the header is set only by a guard or a bill builder)."""
	out = []
	for name in [doc.get(kind.link_field)] + [r.get(kind.link_field) for r in doc.get("items") or []]:
		if name and name not in out and frappe.db.exists(kind.doctype, name):
			out.append(name)
	return out


def refundable_payments(kind: Kind, name: str) -> list[tuple[str, float]]:
	"""The submitted payments towards *name* whose *kind* line can still be returned, newest first,
	with each one's amount. Only a return of that line counts (draft or submitted): goods returned
	from the same ticket leave the payment refundable.

	A payment line already returned once is left out even when part of it is still held: the line
	is one unit and ERPNext returns a line's quantity once (``validate_returned_items`` counts the
	returned qty per line), so a second return of it is refused. Our own refunds return what is held
	in one go; a partial return of the line is made only by hand at the register."""
	rows = frappe.db.sql("""
		SELECT p.name, SUM(i.amount),
			(SELECT COUNT(*) FROM `tabPOS Invoice` r INNER JOIN `tabPOS Invoice Item` ri ON ri.parent = r.name
				WHERE r.return_against = p.name AND r.docstatus < 2 AND ri.item_code = %s)
		FROM `tabPOS Invoice` p INNER JOIN `tabPOS Invoice Item` i ON i.parent = p.name
		WHERE p.docstatus = 1 AND p.is_return = 0 AND i.item_code = %s AND i.{link} = %s AND i.amount > 0
		GROUP BY p.name ORDER BY p.creation DESC""".format(link=kind.link_field), (kind.item, kind.item, name))
	return [(r[0], flt(r[1], 2)) for r in rows if not r[2] and flt(r[1]) > TOL]


def refund_return(kind: Kind, name: str, source: str, amount: float):
	"""The draft register return paying *amount* of *source* (a payment towards *name*) back:
	only the *kind* line, the way the payment came in. Inserted; the register checks it out (card
	refunds run there, never on the Desk)."""
	from erpnext.accounts.doctype.pos_invoice.pos_invoice import make_sales_return

	ret = make_sales_return(source)
	ret.items = [r for r in ret.items if r.item_code == kind.item]
	for r in ret.items:
		r.qty = -1
		r.rate = amount
		r.set(LOCKED_RATE_FIELD, amount)
		r.set(kind.link_field, name)
	ret.set(kind.link_field, name)
	ret.flags.ignore_permissions = True
	ret.set_missing_values()
	ret.calculate_taxes_and_totals()
	# The refund goes back the way the payment came in, split across the modes in the same
	# shares as the source sale ($10 cash + $20 card → a third cash, two thirds card), so a
	# card is never asked for more than it was charged.
	paid = {}
	for mode, amt in frappe.get_all("Sales Invoice Payment", filters={"parent": source,
			"parenttype": "POS Invoice"}, fields=["mode_of_payment", "amount"], as_list=True):
		paid[mode] = paid.get(mode, 0) + max(flt(amt), 0)
	total_paid = sum(paid.values())
	refund = flt(ret.rounded_total) or flt(ret.grand_total)   # negative
	rows = ret.get("payments") or []
	for p in rows:
		p.amount = p.base_amount = 0
	shares = [(p, paid.get(p.mode_of_payment, 0)) for p in rows if paid.get(p.mode_of_payment, 0) > 0]
	if not shares and rows:
		shares = [(rows[0], 1)]
		total_paid = 1
	left = refund
	for i, (p, share) in enumerate(shares):
		part = left if i == len(shares) - 1 else flt(refund * share / total_paid, 2)
		p.amount = p.base_amount = part
		left = flt(left - part, 2)
	# Totals do not re-sum a POS return's tender; the header must agree with the rows.
	ret.paid_amount = ret.base_paid_amount = sum(flt(p.amount) for p in ret.get("payments") or [])
	ret.insert()
	return ret


def refuse_taxed_line(doc, row, label: str) -> None:
	"""A deposit line is never taxed. Its item carries the company's 0% template, which zeroes
	every account of the company's default sales-tax template; an invoice taxed on any other
	account (a register set to a template the 0% one does not cover) would tax the deposit, since
	ERPNext charges a line the template's own rate for any account its item map leaves out.
	Refuse, naming the fix, rather than book tax on money held."""
	import json

	try:
		item_map = json.loads(row.get("item_tax_rate") or "{}") or {}
	except ValueError:
		item_map = {}
	for tax in doc.get("taxes") or []:
		if tax.get("charge_type") not in ("On Net Total", "On Item Quantity"):
			continue
		if not flt(tax.get("rate")):
			continue
		if tax.get("account_head") in item_map and not flt(item_map[tax.get("account_head")]):
			continue
		frappe.throw(_("The {0} line would be taxed on {1}. Its item's 0% template must cover that account: "
			"run bench migrate, or set the register's tax template to the company default.").format(
				label, tax.get("account_head")), title=_("Tax on a deposit"))
