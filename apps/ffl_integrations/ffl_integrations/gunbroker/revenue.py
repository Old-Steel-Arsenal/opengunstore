"""Booking the money for a GunBroker sale.

The guns leave under dispositions, which is a bound-book event and not an
accounting one. This module mints the document that puts the sale on the
ledger: one submitted Sales Invoice per transferred order, mirroring what
GunBroker charged the buyer, plus the fee we owe GunBroker for it and the
receipt for what was banked.

Four things about it are not obvious.

**Two of the three tax rows are somebody else's money.** GunBroker computes the
sales tax as marketplace facilitator, adds it to the buyer's total, and files it
with the state — but the money reaches our bank on its way there. So it posts to
a liability, never to ``Sales Tax Payable``, which is what the Texas return is
built from: a marketplace sale belongs in Total Sales and NOT in Taxable Sales.
The buyer's 1% service fee rides inside the same ``salesTaxTotal`` figure and is
owed to a different party, so the two are split apart rather than netted. When
GunBroker does not tell us the service fee, the invoice stops in Draft — the
split cannot be guessed at, and inferring it from a percentage would put real
money in the wrong liability every time the rate changed.

**The final value fee is worked out per gun, not per order.** The tiers are
progressive, so running one order total through them charges the cheap upper
bands once instead of once per gun: two $1,000 rifles are $96 of fee billed
per-gun and $88 billed per-order. GunBroker bills the first, so we accrue the
first, and the per-line amounts are stored so the monthly statement can be
checked line by line.

**The receipt posts on the invoice's date, never on the day the money arrived.**
GunBroker orders are paid before they are invoiced — always — and POS refuses,
correctly, to post a receipt earlier than the invoice it settles. Pinning the
receipt to the arrival date would therefore fail on every single order and leave
the sale sitting in receivables with the cash already in the bank. The deposit
date is not lost: it goes in the entry's remarks, which is what a bank
reconciliation is done against.

**A receipt is never invented.** If nobody recorded the payment in POS, no
Payment Entry is created — not from the order total, and not from GunBroker's
own "payment received" flag, which is a click on their website. The invoice
stays outstanding and the daily audit keeps saying so until a human records what
was actually banked.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

from ffl_core.api.manual_order import _ensure_ship_address
from ffl_core.customer_profile import find_customer_by_email, find_customer_by_gb_id
from ffl_integrations.gunbroker import setup as gb_setup
from ffl_integrations.notifications import notify_admins, safe_log_error

ORDER_DOCTYPE = "GunBroker Order"
SETTINGS_DOCTYPE = "GunBroker Settings"

# One cent of float slack. Anything wider is a mirroring failure, not rounding,
# and stops in Draft.
TOTAL_TOLERANCE = 0.02

# Amounts GunBroker can put on an order that this phase has never seen and has
# no accounting treatment for. Any of them non-zero and the invoice stops:
# posting a total that silently ignores a coupon is worse than not posting.
UNHANDLED_AMOUNT_FIELDS = (
	("coupon_value", "couponValue"),
	("lost_cash_discount", "lostCashDiscount"),
	("buyers_premium", "buyersPremium"),
	("misc_charge", "miscCharge"),
	("misc_fee", "miscFee"),
)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def create_sales_invoice_for_gb_order(order) -> dict:
	"""Idempotent and resumable: submitted invoice → top up the receipt if it is
	missing; a stranded draft → re-check and submit; nothing yet → build it."""
	if isinstance(order, str):
		order = frappe.get_doc(ORDER_DOCTYPE, order)
	if (order.order_status or "") != "Transferred":
		frappe.throw(_("{0} has not been disposed yet — book the bound-book exit "
			"before the revenue.").format(order.name))

	company = _default_company()
	existing = frappe.db.get_value(
		"Sales Invoice",
		{"gunbroker_order": order.name, "docstatus": ("<", 2)},
		["name", "docstatus", "amended_from"], as_dict=True)

	if existing and existing.docstatus == 1:
		si = frappe.get_doc("Sales Invoice", existing.name)
		_link_invoice(order, si)
		_accrue_fees(order, si)
		_ensure_paid(si, order)
		return {"sales_invoice": si.name, "status": "exists"}

	_refuse_to_rewrite_an_amendment(order, existing)
	_refuse_to_duplicate_an_unlinked_amendment(order, existing)

	# The two refusals are judged at different moments, and they have to be.
	#
	# The first is arithmetic on the ORDER: a missing service fee, a fee bigger
	# than the tax it is supposed to be inside, per-line tax that does not add
	# up, an amount this phase has never seen. None of it needs an invoice to
	# decide, and deciding after one was built put it behind a document that
	# could not be built from those same numbers.
	#
	# The second is about the DOCUMENT, and can only be judged afterwards:
	# ERPNext is what puts the unexpected tax rows there, during the insert.
	blocked = unhandled_amounts(order)
	si = _refresh_draft(order, existing, company) if existing \
		else _build_invoice(order, company)
	blocked = blocked or _unexpected_tax_rows(si, company)
	if blocked:
		return _leave_in_draft(order, si, blocked)

	drift = abs(flt(si.grand_total) - flt(order.order_total))
	if drift > TOTAL_TOLERANCE:
		return _leave_in_draft(order, si, _(
			"This invoice totals {0} and GunBroker charged the buyer {1} — off by "
			"{2}. A posted invoice that does not agree with what the buyer was "
			"charged is worse than one nobody has posted yet. Fix the lines or "
			"the tax rows, submit, then run Create Invoice for the receipt."
		).format(si.grand_total, order.order_total, f"{drift:.2f}"),
			status="draft_total_mismatch")

	_refuse_to_post_a_negative_liability(si, order)
	si.flags.ignore_permissions = True
	si.submit()
	_link_invoice(order, si)
	_accrue_fees(order, si)
	_ensure_paid(si, order)
	return {"sales_invoice": si.name, "status": "created"}


def _refuse_to_rewrite_an_amendment(order, existing) -> None:
	"""A Draft that is an amendment is not a Draft to bring up to date.

	`_refresh_draft` re-derives everything from the GunBroker order, which is
	exactly right for a Draft this module built and stopped, and exactly wrong
	for one a human made by amending a cancelled invoice. That Draft usually
	exists BECAUSE somebody changed a figure — a partial refund agreed with the
	buyer, a line taken off — and re-deriving it puts the original amounts
	back. GunBroker's own `totalPrice` does not move on a partial refund, so
	the drift guard agrees with the restored figure and the invoice submits at
	the price nobody agreed to.

	It is refused rather than handled: submitting an amendment is a decision
	made on the form, with the numbers in front of you.
	"""
	if not existing or existing.docstatus != 0:
		return
	if not (existing.get("amended_from") or "").strip():
		return
	frappe.throw(_(
		"{0} is an amendment of a cancelled invoice, not a draft this can "
		"finish. Re-deriving it from GunBroker would undo whatever was changed "
		"on it — a part refund, a line removed — and post the original "
		"amount.\n\nSubmit it in the form, then run this again to re-accrue "
		"the fee and re-apply the receipt."
	).format(existing.name), title=_("Amendment In Progress"))


def _refuse_to_duplicate_an_unlinked_amendment(order, existing) -> None:
	"""An amendment that lost its GunBroker link is not an invoice to replace.

	`existing` is looked up by `gunbroker_order`, so an amendment that does not
	carry the field is invisible to it — and the order still points at the
	cancelled original, which is what the daily audit reports, telling whoever
	reads it to run Create Invoice. That builds a SECOND live invoice for one
	sale: gross receipts counted twice, a second fee accrual (the first link
	was cleared by the cancel), and both dated today.

	Desk copies the field on an amend, so the standard path never gets here.
	One built through the REST API or the MCP tools — which is how amendments
	have actually been made in this shop — can miss it. Refused rather than
	adopted: setting somebody else's document's link from here is a guess about
	what they meant.

	An order whose invoice was cancelled with NO amendment behind it is left
	alone; rebuilding is the legitimate way back from a cancellation made in
	error.
	"""
	if existing:
		return
	cancelled = (order.sales_invoice or "").strip()
	if not cancelled:
		return
	if frappe.db.get_value("Sales Invoice", cancelled, "docstatus") != 2:
		return
	amendment = frappe.db.get_value(
		"Sales Invoice",
		{"amended_from": cancelled, "docstatus": ("<", 2)}, "name")
	if not amendment:
		return
	frappe.throw(_(
		"{0} was cancelled and amended as {1}, but {1} does not carry the "
		"GunBroker order — so this cannot see it, and would raise a SECOND "
		"invoice for the same sale.\n\nSet GunBroker Order (and the two fee "
		"fields) on {1}, then run this again."
	).format(cancelled, amendment), title=_("Amendment Not Linked"))


def _refuse_to_post_a_negative_liability(si, order) -> None:
	"""The last door: nothing with a negative tax row may be submitted.

	`unhandled_amounts` refuses these before the invoice is built, and that is
	the path that matters — it produces a Draft somebody can see and an alert
	somebody reads. This is here for a caller that reached submit without going
	through it, and it throws rather than returning a reason because by this
	point there is nowhere left to put one.
	"""
	for row in (si.get("taxes") or []):
		if flt(row.get("tax_amount")) < 0:
			frappe.throw(_(
				"{0} carries a negative tax row ({1} {2}) and cannot be posted. "
				"A marketplace liability is money we are holding for somebody "
				"else; there is no such thing as a negative amount of it."
			).format(order.name, row.get("account_head"),
				flt(row.get("tax_amount"))),
				title=_("Negative Marketplace Liability"))


@frappe.whitelist()
def create_gb_invoice_now(gunbroker_order: str) -> dict:
	"""Retry the revenue booking for a disposed GunBroker order.

	For an order whose invoice failed, stopped in Draft, or was posted without
	its receipt (the payment was recorded after the guns had already shipped).

	Args:
	    gunbroker_order: GunBroker Order name (must be Transferred).

	Returns:
	    The booking result from create_sales_invoice_for_gb_order.

	Permissions:
	    Counter roles (the same gate as the fulfillment actions).
	"""
	from ffl_core.api.manual_order import _ROLES

	frappe.only_for(_ROLES)
	return create_sales_invoice_for_gb_order(gunbroker_order)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def unhandled_amounts(order) -> str:
	"""Why this order cannot be posted, or "".

	Two separate refusals, and both are fail-closed by choice. A missing service
	fee means the tax figure cannot be split, and the alternative — assuming the
	documented 1% — would misfile real money into the wrong liability the first
	time GunBroker changed the rate, silently and for every order. The unhandled
	amounts are things this phase has never seen: posting an invoice that
	quietly ignores a coupon is not better than posting nothing.
	"""
	# Zero is treated exactly like absent, deliberately. A Currency column cannot
	# hold "GunBroker did not send this key" — it reads back 0.00 either way. And
	# it is refused whatever salesTaxTotal says: the buyer's service
	# fee is charged on EVERY purchase and rides inside salesTaxTotal, so a zero
	# fee never means "waived", it means the figure did not arrive. A no-tax
	# state is salesTaxTotal == complianceFee, not both zero.
	if not flt(order.compliance_fee):
		return _(
			"GunBroker sent no complianceFee (salesTaxTotal {0}), so the figure "
			"cannot be split between the marketplace sales tax and the buyer's "
			"service fee. Those are owed to two different parties, and the split "
			"is not something to estimate — inferring the documented 1% would "
			"misfile real money the first time GunBroker changed the rate."
		).format(flt(order.sales_tax_total))
	# The subtraction below must not go negative. If GunBroker's
	# salesTaxTotal turns out NOT to include the service fee
	# (never verified against a real order), a no-tax buyer produces
	# {MPF −11.00, MSF +11.00}. The drift guard would hold that in Draft, but
	# the obvious repair — delete the negative row and submit — books the fee
	# with no tax against it and reconciles to nothing.
	if flt(order.sales_tax_total) < flt(order.compliance_fee):
		return _(
			"salesTaxTotal {0} is below complianceFee {1}: the marketplace tax "
			"cannot be negative. Verify what GunBroker means by salesTaxTotal on "
			"this order before booking it — if the fee is not inside that figure, "
			"the split in this module is wrong for every order.\n\nDo NOT submit "
			"the Draft as it stands. The row it is missing is the marketplace "
			"sales tax itself, so submitting it books the buyer's service fee "
			"with no tax behind it and reconciles to nothing."
		).format(flt(order.sales_tax_total), flt(order.compliance_fee))
	# the free self-check on the same question, from the other direction.
	# Per-line salesTax sums to the tax GunBroker charged on the goods; freight
	# tax only makes the order-level figure bigger, so the line sum can never
	# exceed the marketplace tax. If it does, salesTaxTotal did not contain the
	# service fee.
	line_tax = flt(sum(flt(line.line_tax) for line in (order.lines or [])), 2)
	marketplace_tax = flt(flt(order.sales_tax_total) - flt(order.compliance_fee), 2)
	if line_tax and line_tax > marketplace_tax + 0.01:
		return _(
			"The per-line sales tax adds up to {0}, more than the {1} left after "
			"taking the buyer's service fee out of salesTaxTotal. Freight tax can "
			"only make the order figure larger, never smaller, so salesTaxTotal "
			"does not contain the service fee on this order."
		).format(line_tax, marketplace_tax)
	found = [
		f"{api_name}={flt(order.get(field)):.2f}"
		for field, api_name in UNHANDLED_AMOUNT_FIELDS
		if flt(order.get(field))
	]
	if found:
		return _(
			"This order carries amounts this phase has no treatment for: {0}. "
			"Book it by hand rather than posting a total that ignores them."
		).format(", ".join(found))
	return ""


def _unexpected_tax_rows(si, company: str) -> str:
	"""Any taxes row this integration did not put there, or "".

	ERPNext fills an empty taxes table in for us. ``set_taxes_and_charges()``
	runs inside the controller's validate, finds no rows and — with
	``add_taxes_from_item_tax_template`` on, which it is in production —
	appends one per Item Tax Template found on the lines. The freight row this
	invoice does get is appended by ffl_core's hook AFTER the controller, so it
	is not there to stop it, and the three ffl_core hooks that step aside for a
	channel-mirrored invoice only skip: they never delete a row.

	What that produces on a GunBroker sale is a ``Sales Tax Payable`` row — the
	one account the Texas return is built from, and the one account a
	marketplace sale must not touch, because the marketplace filed that tax.
	Three known account heads, everything else stops in Draft rather than
	being guessed at.
	"""
	from ffl_core.setup.shipping_charge import shipping_account_name

	allowed = {
		gb_setup.account_name(gb_setup.MPF_TAX_ACCOUNT, company),
		gb_setup.account_name(gb_setup.MSF_ACCOUNT, company),
		shipping_account_name(company),
	}
	found = [
		f"{row.get('account_head')} {flt(row.get('tax_amount')):.2f}"
		for row in (si.get("taxes") or [])
		if (row.get("account_head") or "") not in allowed
	]
	if not found:
		return ""
	return _(
		"This invoice carries tax rows nothing in this integration put there: "
		"{0}. ERPNext fills an empty taxes table from the items' own tax "
		"templates, and a marketplace sale must not carry the store's sales tax "
		"— GunBroker already filed it. Remove the rows and submit, or take the "
		"Item Tax Template off the item."
	).format(", ".join(found))


def _leave_in_draft(order, si, reason: str,
		status: str = "draft_unhandled_amounts") -> dict:
	"""One exit for every invoice this module declines to post.

	Both reasons an invoice stops here — the order's figures cannot be booked,
	or the document does not agree with what the buyer was charged — leave the
	same thing behind: a Draft nobody has posted and a sale that is not on the
	ledger. So they leave it the same way, through here, rather than through
	two copies of the same log/link/alert that drift apart the first time
	somebody edits one window or one dedup key.

	The reason also goes onto the document. The Error Log and the email both
	name it, but the person who eventually opens this invoice is looking at the
	invoice — and a Draft held for a missing service fee looks exactly like an
	ordinary no-tax sale from there.
	"""
	frappe.log_error(
		title=f"GunBroker invoice held in Draft: {order.name}",
		message=f"{order.name} ({order.gb_order_id}): {reason}\n\nSales Invoice "
			f"{si.name} was built but NOT submitted.")
	_link_invoice(order, si)
	_note_on_the_invoice(si, reason)
	notify_admins(
		_("GunBroker order {0} needs a human to book it").format(order.gb_order_id),
		_("{0} ({1}) could not be posted automatically: {2}\n\nDraft invoice: {3}"
		).format(order.name, order.gb_order_id, reason, si.name),
		dedup_key=f"gb_invoice_draft_{order.gb_order_id}", window_minutes=1440)
	return {"sales_invoice": si.name, "status": status}


def _note_on_the_invoice(si, reason: str) -> None:
	"""Why this Draft stopped, on its own timeline. Never raises."""
	try:
		si.add_comment("Comment", _("Held in Draft by the GunBroker booking: {0}"
			).format(reason))
	except Exception:  # noqa: BLE001 — a note must not cost us the Draft
		safe_log_error(f"GunBroker Draft note failed for {si.name}",
			frappe.get_traceback())


# ---------------------------------------------------------------------------
# Building the invoice
# ---------------------------------------------------------------------------

def _build_invoice(order, company: str):
	si = frappe.new_doc("Sales Invoice")
	si.company = company
	si.customer = resolve_customer(order)
	# The guns already left under the dispositions. Moving stock again here
	# would book a second exit for the same firearm.
	si.update_stock = 0
	si.gunbroker_order = order.name
	si.posting_date = nowdate()
	si.set_posting_time = 1
	si.ignore_pricing_rule = 1
	_apply_derived(si, order, company)
	si.flags.ignore_permissions = True
	si.insert()
	return si


def _refresh_draft(order, existing, company: str):
	"""A stranded Draft, brought back up to date from the order as it stands now.

	A Draft exists only because something about the order could not be posted,
	and the commonest of those reasons fixes itself: GunBroker had not sent
	``complianceFee`` yet, and a later poll fills it in. What must NOT survive
	that fix is the tax split the Draft was built with. With no service fee the
	whole of ``salesTaxTotal`` went onto the MPF sales-tax row, so re-submitting
	the Draft untouched files the buyer's service fee with the state — for ever,
	on a document that reconciles perfectly, because the grand total is
	identical either way and the drift guard below therefore cannot see it.

	What survives is the posting date and the customer, and only those: the
	items, the tax rows, the freight, the fee fields and the remarks are all
	re-derived. Re-dating a Draft raised last month would move the revenue into
	a period nobody asked it to move to, which is why the date is left alone —
	but nothing else on the document is preserved, and an amendment (whose
	whole point is a figure somebody changed by hand) is refused upstream
	rather than handed to this.
	"""
	si = frappe.get_doc("Sales Invoice", existing.name)
	_apply_derived(si, order, company)
	si.flags.ignore_permissions = True
	si.save()
	return si


def _apply_derived(si, order, company: str) -> None:
	"""Everything on the invoice that comes off the order, (re)computed.

	Shared by the build and the Draft recovery on purpose: two ways of working
	out the tax split is one way too many, and the one that would rot is the
	recovery path, which by definition only runs when something already went
	wrong.
	"""
	# The 4473 gate reads this to decide the transfer happens at the receiving
	# dealer. Without it the invoice cannot be submitted at all.
	si.ffl_destination_ffl = (order.atf_ffl_record or "").strip() or None
	si.po_no = order.gb_order_id
	si.gb_ship_to_state = order.ship_to_state or ""
	if cint(order.ships_direct) and not (si.shipping_address_name or "").strip():
		# No dealer at the far end, so the label goes to the buyer: give the
		# invoice the shipping Address ShipStation's build_ship_to falls back
		# to when there is no destination FFL. Here and not in _build_invoice
		# so the Draft recovery gets it too — the helper is best-effort, and
		# one failure at build time must not leave an invoice nothing can ship.
		si.shipping_address_name = _buyer_ship_address(si.customer, order)
	# The item numbers are the reconciliation key, not decoration: the freight
	# income row's description is a fixed constant ffl_core's hook owns, so the
	# GunBroker order and item are matched from here, from po_no and from the
	# disposition notes. GL Entry takes its remarks from this field, so this is
	# what a month-end reconciliation actually reads.
	items = ", ".join(f"Item #{line.gb_item_id}" for line in (order.lines or [])
		if (line.gb_item_id or "").strip())
	if cint(order.ships_direct):
		ships_to = _("Ships direct to the buyer (antique, no FFL).")
	else:
		ships_to = _("Ships to FFL: {0} {1}.").format(
			order.ffl_number or "?", _licensee_name(order) or "")
	si.remarks = _("GunBroker Order #{0}{1}. Buyer: {2}. {3}").format(
		order.gb_order_id, f" ({items})" if items else "",
		order.customer_name or order.buyer_username or "?", ships_to)

	si.set("items", [])
	for row in _invoice_items(order):
		si.append("items", row)

	# The freight income ROW is generated from this by ffl_core's shipping hook,
	# and its description is a fixed constant the hook's own freight-tax
	# identification then matches on — so it cannot carry the GunBroker order
	# number. Overwriting it would break that identification
	# for every channel. The number is on the same invoice twice already, in
	# po_no and in remarks, which is what the month-end reconciliation matches.
	# ffl_shipping_amount = customer-paid shipping; income is always the taxes
	# table's Shipping row, never this field.
	si.ffl_shipping_amount = flt(order.ship_cost) + flt(order.ship_handling_cost) \
		+ flt(order.ship_insurance_cost)
	# Pinned empty, and pinned before the rows go on. A GunBroker invoice never
	# takes a Sales Taxes and Charges Template: its two rows are Actual amounts
	# GunBroker already computed, and a template would add the store's own
	# sales-tax row on top — the one account a marketplace sale must not touch.
	# ERPNext's set_missing_values fills this in from the customer or the
	# company when it is blank, and the tax-free variant (no-tax state, seller
	# pays shipping) is the one that leaves the table empty for it to fill.
	si.taxes_and_charges = ""
	si.set("taxes", [])
	for tax in _tax_rows(order, company):
		si.append("taxes", tax)

	fvf, doc_fee = _line_fees(order)
	si.gb_final_value_fee = fvf
	si.gb_ffl_doc_fee = doc_fee
	# Ship pressed before Dispose: the shipment was made against the order, and
	# from here on everything (the queue, the tracking write-back) reads it off
	# the invoice.
	if (order.get("shipstation_order_id") or "").strip() and not (si.get("shipstation_order_id") or "").strip():
		si.shipstation_order_id = order.shipstation_order_id
		si.shipstation_status = order.shipstation_status or "Pending"


def _buyer_ship_address(customer: str, order) -> str | None:
	"""A Shipping Address for the buyer, off the order's own ship-to block."""
	return _ensure_ship_address(customer, order.ship_to_name or order.customer_name, {
		"address_line1": order.ship_to_line1,
		"address_line2": order.ship_to_line2,
		"city": order.ship_to_city,
		"state": order.ship_to_state,
		"pincode": order.ship_to_zip,
		"phone": order.customer_phone,
	})


def _invoice_items(order) -> list[dict]:
	"""One row per gun, carrying its serial.

	The serial is not decoration. The Sales Report prices a firearm's cost from
	the serial's own purchase rate, and both the consignment settlement and the
	pay-on-sale supplier settlement resolve which gun was sold by reading it off
	the line — so an invoice without it reports inflated profit and silently
	fails to pay a supplier.
	"""
	serial_by_item = {}
	refunded_by_item: dict[str, int] = {}
	for row in (order.serials or []):
		serial = (row.confirmed_serial or "").strip()
		if serial:
			serial_by_item.setdefault(row.gb_item_id or "", []).append(serial)
		if cint(row.refunded_on_gb):
			refunded_by_item[row.gb_item_id or ""] = refunded_by_item.get(row.gb_item_id or "", 0) + 1
	items = []
	for line in (order.lines or []):
		# A unit refunded on GunBroker (an oversold model-level order) was never
		# delivered: it is not on the invoice, has no serial and no disposition.
		# The seller fee is NOT reduced — _line_fees reads the whole line.
		qty = max(1, int(line.qty or 1)) - refunded_by_item.get(line.gb_item_id or "", 0)
		if qty <= 0:
			continue
		row = {
			"item_code": line.item_code,
			"qty": qty,
			"rate": flt(line.unit_price),
		}
		serials = serial_by_item.get(line.gb_item_id or "")
		if serials:
			row["serial_no"] = "\n".join(serials)
		items.append(row)
	return items


def _tax_rows(order, company: str) -> list[dict]:
	"""Freight income, the marketplace tax, and the marketplace fee.

	A zero row is left out entirely rather than posted as 0.00 — a state with no
	sales tax, a tax-exempt buyer, or a seller-pays-shipping listing each drop
	one of the three, and an empty row on a ledger is a question somebody has to
	answer later.
	"""
	rows = []
	cost_center = _cost_center(company)
	tax_total = flt(order.sales_tax_total)
	service_fee = flt(order.compliance_fee)
	marketplace_tax = flt(tax_total - service_fee, 2)
	if marketplace_tax < 0:
		# Left off entirely rather than posted or thrown on. `unhandled_amounts`
		# has already refused this order — it is judged before anything is
		# built — and what happens next is that a Draft is raised for a human
		# to look at. Throwing HERE would take that Draft away with it: the
		# only automatic caller is the dispose step, whose blanket handler turns
		# an exception into one line in the Error Log, on an order whose guns
		# have already gone. The hard stop lives where it can still be a hard
		# stop, immediately before submit.
		marketplace_tax = 0.0
	if marketplace_tax:
		rows.append({
			"charge_type": "Actual",
			"account_head": gb_setup.account_name(gb_setup.MPF_TAX_ACCOUNT, company),
			"description": _("GunBroker MPF sales tax (GB Order #{0})").format(
				order.gb_order_id),
			"tax_amount": marketplace_tax,
			"cost_center": cost_center,
		})
	if service_fee:
		rows.append({
			"charge_type": "Actual",
			"account_head": gb_setup.account_name(gb_setup.MSF_ACCOUNT, company),
			"description": _("GunBroker marketplace service fee (GB Order #{0})"
				).format(order.gb_order_id),
			"tax_amount": service_fee,
			"cost_center": cost_center,
		})
	return rows


def _cost_center(company: str) -> str | None:
	"""The company's cost centre, or its first real one.

	A charge row without one saves happily and then fails at submit with a
	cryptic GL error — the same trap ffl_core's shipping hook documents, and the
	same resolution, so the two rows on one invoice cannot disagree.
	"""
	return (frappe.db.get_value("Company", company, "cost_center")
		or frappe.db.get_value(
			"Cost Center", {"company": company, "is_group": 0, "disabled": 0},
			"name", order_by="lft"))


def _licensee_name(order) -> str:
	if not order.atf_ffl_record:
		return ""
	return frappe.db.get_value("ATF FFL Record", order.atf_ffl_record,
		"licensee_name") or ""


def _default_company() -> str:
	import erpnext

	company = erpnext.get_default_company()
	if company:
		return company
	names = frappe.get_all("Company", pluck="name", limit=1)
	if not names:
		frappe.throw(_("No Company exists — cannot book GunBroker revenue."))
	return names[0]


def _link_invoice(order, si) -> None:
	if (order.sales_invoice or "") == si.name:
		return
	frappe.db.set_value(ORDER_DOCTYPE, order.name, "sales_invoice", si.name,
		update_modified=False)
	order.sales_invoice = si.name


# ---------------------------------------------------------------------------
# The customer
# ---------------------------------------------------------------------------

def resolve_customer(order) -> str:
	"""The buyer, deduplicated on their GunBroker account.

	The numeric user id first, because a username can be changed and an email
	can be shared; then a GunBroker id a merge recorded as an alias; then the
	email (``Customer.email_id``, then an email alias). A hit that is an FFL
	dealer is skipped at the alias and email steps alike. An existing customer
	flagged as an FFL dealer is deliberately NOT reused even on an exact email
	match: a dealer record carries pricing and tax treatment that belongs to a
	wholesale relationship, and a retail marketplace purchase must not inherit
	it. No existing customer's exemption is ever touched.

	A plain email match stamps the order's user id onto that customer; a match
	through an alias does not: the customer was merged from another record and
	keeps the account id it has.
	"""
	from ffl_core.api.manual_order import _individual_group

	user_id = (order.buyer_user_id or "").strip()
	has_gb_id = bool(user_id) and frappe.db.has_column("Customer", "gb_user_id")
	if has_gb_id:
		existing = frappe.db.get_value("Customer", {"gb_user_id": user_id}, "name")
		if existing:
			return existing
	if user_id:
		aliased = find_customer_by_gb_id(user_id)
		# Same dealer rule as the email step: a retail buyer merged into a dealer
		# record must not be booked to the dealer (falls through to email, then new).
		if aliased and not frappe.db.get_value("Customer", aliased, "is_ffl_dealer"):
			return aliased
	email = (order.customer_email or "").strip()
	if email:
		match = find_customer_by_email(email)
		if match and not match.is_ffl_dealer:
			if has_gb_id and not match.get("via_alias"):
				frappe.db.set_value("Customer", match.name, "gb_user_id", user_id,
					update_modified=False)
			return match.name

	doc = frappe.get_doc({
		"doctype": "Customer",
		"customer_name": (order.customer_name or "").strip()
			or (order.buyer_username or "").strip() or email or user_id,
		"customer_type": "Individual",
		"is_ffl_dealer": 0,
		"customer_group": _individual_group(),
		"territory": frappe.db.get_single_value("Selling Settings", "territory")
			or "All Territories",
		"email_id": email or None,
		"mobile_no": (order.customer_phone or "").strip() or None,
		"is_tax_exempt": 0,
	})
	if user_id and frappe.db.has_column("Customer", "gb_user_id"):
		doc.gb_user_id = user_id
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


# ---------------------------------------------------------------------------
# Seller fees
# ---------------------------------------------------------------------------

def final_value_fee(price: float, tiers) -> float:
	"""GunBroker's progressive final value fee on ONE gun's price.

	Progressive, so each band is charged at its own rate: at the shipped tiers a
	$1,000 rifle is 6% of the first $400 plus 4% of the next $600. Rounded here,
	per gun, because that is how the bill arrives — rounding a summed order once
	drifts from the statement by a cent or two per line.
	"""
	price = flt(price)
	# A tier with no upper bound is the open-ended top band; it sorts last.
	ordered = sorted(
		[t for t in (tiers or []) if flt(t.rate)],
		key=lambda t: (flt(t.upper_bound) <= 0, flt(t.upper_bound)))
	fee = 0.0
	lower = 0.0
	for tier in ordered:
		if price <= lower:
			break
		upper = flt(tier.upper_bound)
		ceiling = price if upper <= 0 else min(price, upper)
		fee += (ceiling - lower) * flt(tier.rate) / 100.0
		lower = ceiling
	return flt(fee, 2)


def _line_fees(order) -> tuple[float, float]:
	"""Accrue per line, store per line, return the two totals.

	Already-accrued lines keep their amounts. The fee was computed from the
	price at the time of sale and GunBroker does not reduce it when a seller
	later discounts an order, so recomputing would quietly re-price a liability
	that has already been journalled.
	"""
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	tiers = settings.fee_tiers or []
	doc_fee_rate = flt(settings.ffl_doc_fee)
	fvf_total = 0.0
	doc_total = 0.0
	for line in (order.lines or []):
		if flt(line.fvf_amount) or flt(line.ffl_doc_fee_amount):
			fvf_total += flt(line.fvf_amount)
			doc_total += flt(line.ffl_doc_fee_amount)
			continue
		qty = max(1, int(line.qty or 1))
		fvf = final_value_fee(line.unit_price, tiers) * qty
		# Per unit, like the FVF. GunBroker bills the FFL document fee per
		# firearm, and a two-of-a-kind line is two firearms — it cannot happen
		# on a serialised listing today, which is exactly why it would go
		# unnoticed the first time a non-serialised one is sold.
		doc_fee = (doc_fee_rate * qty) if int(line.is_ffl_required or 0) else 0.0
		line.db_set("fvf_amount", flt(fvf, 2), update_modified=False)
		line.db_set("ffl_doc_fee_amount", flt(doc_fee, 2), update_modified=False)
		fvf_total += fvf
		doc_total += doc_fee
	return flt(fvf_total, 2), flt(doc_total, 2)


def _accrue_fees(order, si) -> None:
	"""One journal entry per order: what GunBroker will bill us for this sale.

	Idempotent on the order's own link to it — GunBroker bills once, so a retry
	of the invoice booking must not accrue the fee twice. A failure here is
	logged and left: the sale is on the ledger and the daily audit reports an
	invoice with no accrual against it.
	"""
	if order.fee_accrual_je:
		return
	amount = flt(si.gb_final_value_fee) + flt(si.gb_ffl_doc_fee)
	if amount <= 0:
		return
	company = si.company
	# The cost centre is passed rather than left to Frappe's `:Company` default
	# on the child docfield: every other journal this repo writes passes it
	# (consignment settlement, pay-on-sale), and a row that resolves it from a
	# default is one company-default edit away from failing at submit with a
	# GL error nobody will connect to this.
	cost_center = _cost_center(company)
	# And the same frozen-period escape hatch the receipt has. Without it
	# a retry after the month closes cannot build the journal at all, and
	# si_no_fee_je then reports the order every day for ever.
	#
	# An amendment is the exception, and it is dated today. Its re-accrual is
	# the other half of the reversal that went out with the cancel, and that
	# reversal is dated today: leaving this one on the original invoice's date
	# puts the fee back into the month the reversal just took it out of. The
	# running balance stays right and every monthly figure goes wrong — the
	# sale's month shows the fee twice, this month shows it once negative, and
	# neither agrees with GunBroker's statement. Desk copies posting_date on an
	# amend, so this is not a rare shape.
	posting_date = nowdate() if (si.amended_from or "") \
		else _receipt_posting_date(si)
	try:
		je = frappe.get_doc({
			"doctype": "Journal Entry",
			"voucher_type": "Journal Entry",
			"company": company,
			"posting_date": posting_date,
			"user_remark": _("GunBroker seller fees for order #{0} ({1})").format(
				order.gb_order_id, si.name),
			"accounts": [
				{
					"account": gb_setup.account_name(
						gb_setup.FEES_EXPENSE_ACCOUNT, company),
					"debit_in_account_currency": amount,
					"cost_center": cost_center,
				},
				{
					"account": gb_setup.account_name(
						gb_setup.ACCRUED_FEES_ACCOUNT, company),
					"credit_in_account_currency": amount,
					"cost_center": cost_center,
				},
			],
		})
		je.flags.ignore_permissions = True
		je.insert()
		je.submit()
	except Exception:  # noqa: BLE001 — the sale is booked; the accrual can be retried
		safe_log_error(f"GunBroker fee accrual failed for {order.name}",
			frappe.get_traceback())
		return
	frappe.db.set_value(ORDER_DOCTYPE, order.name, "fee_accrual_je", je.name,
		update_modified=False)
	order.fee_accrual_je = je.name


def refuse_a_negative_liability_on_validate(doc, method=None) -> None:
	"""Sales Invoice validate: no GunBroker invoice carries a negative liability.

	The booking path has its own check immediately before `si.submit()`, and
	that one is unreachable from the Desk. A Draft held for exactly this reason
	— GunBroker's salesTaxTotal came in below its complianceFee — is handed to
	an operator with the marketplace sales-tax row missing and the service-fee
	row present, which looks like an ordinary no-tax sale with one odd line.
	The obvious thing to do with it is press Submit, and that books the buyer's
	service fee with no tax behind it.

	Only invoices that carry a GunBroker order, and only negative amounts: this
	runs on the validate of every Sales Invoice in the system.
	"""
	if not (getattr(doc, "gunbroker_order", None) or "").strip():
		return
	for row in (doc.get("taxes") or []):
		if flt(row.get("tax_amount")) < 0:
			frappe.throw(_(
				"This GunBroker invoice carries a negative tax row ({0} {1}). A "
				"marketplace liability is money held for somebody else and "
				"cannot be negative — GunBroker reported a service fee larger "
				"than the tax figure it is supposed to sit inside, and what "
				"that figure really means has to be established before any of "
				"it is posted."
			).format(row.get("account_head"), flt(row.get("tax_amount"))),
				title=_("Negative Marketplace Liability"))


def keep_gunbroker_remarks(doc, method=None) -> None:
	"""Payment Entry before_validate: stop ERPNext overwriting the order number.

	A hand-keyed GunBroker refund has to carry "GunBroker Order #<id>" in the
	entry's remarks, after ticking Custom Remarks (`marketplace_accounts.
	refund_remarks_rule` is what the alerts tell operators), because that
	marker is the only thing tying it back to its order.
	The cash-basis schedule matches on it, and a refund it cannot match leaves
	the order in that year's gross receipts with the money already returned.

	ERPNext throws the text away. `PaymentEntry.set_remarks()` returns early
	only when `custom_remarks` is set; otherwise it rebuilds the field from the
	amount and the party, on every save — no error, and a schedule that reads
	as though no refund ever happened.

	**What this actually covers.** On the Desk form the field cannot be typed
	into first: `remarks` carries `read_only_depends_on: eval:doc.custom_remarks
	== 0` (ERPNext 16.29), so ticking the box is what unlocks it and a form user
	is already safe. What is left is every path that does not go through that
	form — REST, `bench console`, Data Import, a client script — plus the one
	form sequence that defeats it: tick, type, untick. Those are the cases this
	handler is for. It is a backstop, not the mechanism: operators are told to
	tick Custom Remarks first and then write the order number, not to rely on it.

	Set rather than refused: the entry is right, it is the field that needs
	protecting, and a stop-work in front of somebody mid-refund helps nobody.

	**before_validate, not validate, and that is the whole point.**
	`set_remarks()` runs inside the controller's `validate()`, and a hooked
	`validate` runs AFTER the controller's — the same ordering `hooks.py`
	already leans on for the drop-ship re-pin. Registered there this would set
	the flag one step too late: right on the next save, useless on the one that
	wiped the text. `run_before_save_methods` calls `before_validate` first, on
	both save and submit.

	Narrow on purpose. A receipt is left alone (the receipts this system writes
	set the flag themselves, and turning it on elsewhere would freeze ERPNext's
	description on entries nobody typed a marker into), and so is a payment to
	a supplier — settling GunBroker's own fee bill may well mention an order
	number, and nothing reads that one.
	"""
	# The one marker, read from where the schedule defines it — this is the
	# same function-local import the rest of this module uses to reach ffl_core.
	from ffl_core.marketplace_accounts import ORDER_REMARK_PREFIX

	if (getattr(doc, "payment_type", None) != "Pay"
			or getattr(doc, "party_type", None) != "Customer"
			or cint(getattr(doc, "custom_remarks", 0))):
		return
	if ORDER_REMARK_PREFIX not in (getattr(doc, "remarks", "") or ""):
		return
	doc.custom_remarks = 1
	frappe.msgprint(
		_("Custom Remarks has been ticked so the GunBroker order number in "
			"Remarks survives saving. Without it ERPNext rewrites that field "
			"from the amount and party, and the cash-basis schedule can no "
			"longer match this refund to its order."),
		indicator="blue", alert=True)


def reverse_fee_accrual_on_cancel(doc, method=None) -> None:
	"""Sales Invoice on_cancel: unwind the seller-fee accrual behind it.

	Cancelling the invoice is how a GunBroker sale comes off the books —
	whole-order cancellation, a return, or the cancel half of an amend. The
	accrual is a separate journal and nothing cancels it on its own, so without
	this ``Dr Marketplace Fees / Cr Accrued GunBroker Fees`` stays on the ledger
	against a sale that no longer exists: channel profit reads low for ever and
	the monthly statement cannot be reconciled.

	The order's link is cleared with it, which is what makes an amend come out
	right: the amended invoice re-accrues from the SAME per-line amounts
	(``_line_fees`` keeps them — GunBroker does not re-price a fee because we
	discounted something), so the figure is unchanged and exactly one journal is
	live at the end of it.

	Never raises. A cancel that fails half way through because a journal would
	not reverse leaves the store unable to cancel an invoice at all, which is
	worse than an accrual somebody has to unwind by hand — and the daily audit
	reports the mismatch either way.
	"""
	order_name = (getattr(doc, "gunbroker_order", None) or "").strip()
	if not order_name:
		return
	je = frappe.db.get_value(ORDER_DOCTYPE, order_name, "fee_accrual_je")
	if not je:
		return
	try:
		_reverse_journal(je)
	except Exception:  # noqa: BLE001 — never block a cancel
		safe_log_error(
			f"GunBroker fee accrual not reversed for {order_name}",
			frappe.get_traceback())
		notify_admins(
			_("GunBroker fee accrual left standing on a cancelled invoice"),
			_("Sales Invoice {0} was cancelled and its seller-fee accrual {1} "
				"could not be reversed automatically. Until somebody reverses it "
				"by hand, channel profit reads low and the GunBroker monthly "
				"statement will not reconcile.\n\nAfter reversing it, ask a "
				"System Manager to clear Fee Accrual Journal Entry on {2}. The "
				"field is what stops a second accrual being raised, so while it "
				"still points at the reversed journal an amendment of this "
				"invoice will not re-accrue the fee at all — and nothing "
				"reports that."
			).format(doc.name, je, order_name),
			dedup_key=f"gb_accrual_unreversed_{order_name}", window_minutes=1440)
		return
	frappe.db.set_value(ORDER_DOCTYPE, order_name, "fee_accrual_je", None,
		update_modified=False)


def rebook_on_amended_submit(doc, method=None) -> None:
	"""Sales Invoice on_submit: an amendment picks its fee and receipt back up.

	Cancelling a GunBroker invoice reverses the accrual and unlinks the
	receipt; submitting the amendment has to put both back, and nothing did.
	The order was left pointing at the cancelled original, the daily audit
	reported it, and the only way through was for somebody to know to press
	Create Invoice.

	Only on an amendment. A first submission is booked by whatever raised it —
	`create_sales_invoice_for_gb_order` does the accrual and the receipt itself,
	in order, and doing them again from a hook would be a second attempt at
	work that has not finished yet. Both are idempotent regardless: the accrual
	skips an order that already has a live journal, and the receipt skips an
	order that already has a live entry.

	Never raises. Failing here would fail the submit, and an invoice that
	cannot be submitted is worse than a fee accrual the audit reports tomorrow.
	"""
	order_name = (getattr(doc, "gunbroker_order", None) or "").strip()
	if not order_name:
		return
	if not (getattr(doc, "amended_from", None) or "").strip():
		return
	# A savepoint, because this runs INSIDE somebody else's submit. Swallowing
	# the exception is right — an invoice nobody can submit is worse than an
	# accrual the audit reports tomorrow — but swallowing it without unwinding
	# is not: a journal that inserted and then failed to submit would ride out
	# on the enclosing commit as a Draft nobody asked for.
	savepoint = "gb_rebook_amendment"
	frappe.db.savepoint(savepoint)
	try:
		order = frappe.get_doc(ORDER_DOCTYPE, order_name)
		# The order still points at the cancelled original. Moving the link is
		# what takes it back out of disposed_no_si.
		_link_invoice(order, doc)
		_accrue_fees(order, doc)
		_ensure_paid(doc, order)
	except Exception:  # noqa: BLE001 — never block a submit
		frappe.db.rollback(save_point=savepoint)
		safe_log_error(
			f"GunBroker amendment {doc.name} was not re-booked",
			frappe.get_traceback())
		notify_admins(
			_("GunBroker order {0}: the amendment was not re-booked").format(
				order_name),
			_("Sales Invoice {0} was submitted as an amendment and its seller-fee "
				"accrual and receipt could not be laid back down. The invoice "
				"itself is posted and correct; what is missing is the accrual "
				"behind it, the receipt against it, or both — the person who "
				"pressed Submit saw nothing, because failing here would have "
				"failed the submit.\n\nRun Create Invoice on {1} to try again. "
				"Tomorrow's audit reports it either way."
			).format(doc.name, order_name),
			dedup_key=f"gb_rebook_failed_{order_name}", window_minutes=1440)


def _reverse_journal(je: str) -> None:
	"""A reversing entry, submitted. Falls back to cancelling the original.

	A reversal is preferred over a cancellation because it leaves both halves
	visible in the period they belong to, which is what an accrual reversal
	looks like to an accountant. Some journals cannot be reversed (already
	cancelled, or amended) and for those the cancellation is the honest
	equivalent.
	"""
	from erpnext.accounts.doctype.journal_entry.journal_entry import (
		make_reverse_journal_entry)

	docstatus = frappe.db.get_value("Journal Entry", je, "docstatus")
	if docstatus != 1:
		return  # already cancelled or never submitted — nothing to unwind
	reverse = make_reverse_journal_entry(je)
	reverse.posting_date = nowdate()
	reverse.flags.ignore_permissions = True
	reverse.insert()
	reverse.submit()


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------

def _ensure_paid(si, order) -> None:
	"""Replay what was banked onto a Payment Entry against this invoice.

	Never invents one. An order with no recorded payment gets no entry at all —
	not from the order total, and not from GunBroker's own flag — because a
	receipt nobody took is a lie that balances. The invoice stays outstanding
	and the daily audit keeps reporting it.
	"""
	mode = (order.payment_mode_used or "").strip()
	if not mode:
		return
	if _live_entry(order.payment_entry):
		# The receipt stands. The overpayment half may not — its own booking is
		# best-effort and logs rather than raises, and once payment_entry was
		# set nothing came back for it, so the bank reconciliation stayed out by
		# the difference for ever.
		_top_up_overpayment(si, order)
		return
	si.reload()
	outstanding = flt(si.outstanding_amount)
	if outstanding <= 0:
		return
	banked = flt(order.payment_amount)
	if banked <= 0:
		return

	from ffl_core.api.manual_order import _book_payment_entry

	# The mode's account is re-pointed from Settings before it is used.
	# ensure runs on install and migrate; a store that fills the deposit account
	# in afterwards — the normal go-live order — otherwise reaches here with a
	# Mode of Payment that has no account row, and _book_payment_entry throws
	# with the gun already shipped. Idempotent and cheap; the belt to the
	# braces on GunBroker Settings.on_update.
	_ensure_modes(si.company)
	posting_date = _receipt_posting_date(si)
	allocated = min(banked, outstanding)
	remarks = _(
		"GunBroker Order #{0} — received on {1} via {2} ref {3}"
	).format(order.gb_order_id, order.payment_received_on or "?", mode,
		order.payment_reference_no or "—")
	name = _book_payment_entry(si, mode, allocated,
		transaction_number=order.payment_reference_no or None,
		posting_date=posting_date, remarks=remarks)
	frappe.db.set_value(ORDER_DOCTYPE, order.name, "payment_entry", name,
		update_modified=False)
	order.payment_entry = name

	overpaid = flt(banked - allocated, 2)
	if overpaid >= 0.01:
		_book_overpayment(si, order, overpaid, posting_date)


def _live_entry(name: str | None) -> bool:
	"""Is this Payment Entry link pointing at something that still counts?

	A cancelled entry is not a receipt. A bounced cheque — the entry is
	cancelled, the money is chased and arrives — needs the booking to run
	again, and a link left pointing at the cancelled document made
	create_gb_invoice_now return without doing anything at all.
	"""
	if not (name or "").strip():
		return False
	return frappe.db.get_value("Payment Entry", name, "docstatus") == 1


def _ensure_modes(company: str) -> None:
	try:
		gb_setup.ensure_modes_of_payment(company)
	except Exception:  # noqa: BLE001 — a repair attempt must not break the booking
		safe_log_error("GunBroker modes of payment could not be refreshed",
			frappe.get_traceback())


def _top_up_overpayment(si, order) -> None:
	"""Book the overpayment half if the receipt landed and it did not.

	Only reachable once the allocated receipt exists, so the arithmetic is
	against what the bank actually took: everything banked, less what the live
	receipt allocated. Under a cent is rounding, not a deposit.

	An invoice that is still owed money is not an overpayment, whatever the
	order says was banked. A short payment somebody later "corrected" by
	editing payment_amount would otherwise book the difference as unallocated
	customer credit while the invoice went on showing it outstanding: the bank
	reconciles and the receivable is nonsense. That case is the daily audit's
	(si_outstanding) and a human's.
	"""
	si.reload()
	if flt(si.outstanding_amount) > 0.01:
		return
	banked = flt(order.payment_amount)
	if banked <= 0:
		return
	allocated = flt(frappe.db.get_value("Payment Entry", order.payment_entry,
		"paid_amount"))
	overpaid = flt(banked - allocated, 2)
	if overpaid < 0.01:
		return
	_book_overpayment(si, order, overpaid, _receipt_posting_date(si))


def _receipt_posting_date(si) -> str:
	"""The invoice's own date — with one escape hatch.

	A marketplace order is paid before it is invoiced, always, and POS refuses
	to post a receipt earlier than the invoice it settles (it would land outside
	the period of the sale, possibly in a closed month). So the receipt takes
	the invoice's date, which also keeps the collection in the month of the
	sale when somebody retries the booking days later.

	The escape hatch is for the one case where that date is unusable: the month
	has since been frozen. Refusing to post at all would leave the cash off the
	ledger entirely, so it lands today instead and says so.
	"""
	frozen = frappe.db.get_value("Company", si.company, "accounts_frozen_till_date")
	if frozen and getdate(si.posting_date) <= getdate(frozen):
		frappe.msgprint(_(
			"{0} is dated {1}, which is inside the frozen period (closed through "
			"{2}), so its receipt is posted today instead. The deposit date is on "
			"the entry's remarks either way."
		).format(si.name, si.posting_date, frozen), indicator="orange")
		return nowdate()
	return str(si.posting_date)


def _book_overpayment(si, order, amount: float, posting_date: str) -> None:
	"""The part of a cheque the invoice could not absorb, as customer credit.

	The bank took the whole cheque. Recording only the part that fitted the
	invoice would leave the bank reconciliation permanently out by the
	difference, so the remainder is booked as an unallocated receipt against the
	same customer, in the same account, under the same cheque number — two
	entries that sum to what was actually deposited. Refunding it later is a
	Pay entry against the credit.

	Same posting date as the entry it accompanies: it is one deposit, and
	letting this half default to today would put the two halves of one cheque in
	two different months.
	"""
	from erpnext.accounts.party import get_party_account
	from ffl_core.api.manual_order import _mode_of_payment_account

	if _live_entry(order.gb_overpayment_pe):
		# The one door both callers come through. The re-booking path (
		# the cheque bounced, the allocated entry was cancelled, the money was
		# chased and arrived) reaches here with the OVERPAYMENT entry possibly
		# still live — an operator who cancelled one of the two would otherwise
		# get a second unallocated receipt for the same fifty dollars, and the
		# customer's credit and the bank would disagree by it for good.
		return
	account = _mode_of_payment_account(order.payment_mode_used, si.company)
	if not account:
		safe_log_error(
			f"GunBroker overpayment not booked for {order.name}",
			f"No account configured for {order.payment_mode_used}; {amount} of "
			f"cheque {order.payment_reference_no} is in the bank and not on the "
			"ledger.")
		return
	try:
		pe = frappe.get_doc({
			"doctype": "Payment Entry",
			"payment_type": "Receive",
			"company": si.company,
			"posting_date": posting_date,
			"set_posting_time": 1,
			"party_type": "Customer",
			"party": si.customer,
			"paid_from": get_party_account("Customer", si.customer, si.company),
			"paid_to": account,
			"paid_amount": amount,
			"received_amount": amount,
			"source_exchange_rate": 1,
			"target_exchange_rate": 1,
			"mode_of_payment": order.payment_mode_used,
			"reference_no": order.payment_reference_no or si.name,
			"reference_date": posting_date,
			"custom_remarks": 1,
			"remarks": _(
				"Overpayment on GunBroker Order #{0} — {1} banked above the "
				"invoice total, held as customer credit. Cheque {2}."
			).format(order.gb_order_id, amount, order.payment_reference_no or "—"),
		})
		pe.flags.ignore_permissions = True
		pe.insert()
		pe.submit()
	except Exception:  # noqa: BLE001 — the allocated receipt already posted
		safe_log_error(f"GunBroker overpayment entry failed for {order.name}",
			frappe.get_traceback())
		return
	# Recorded, so the next booking run knows this half is done. Without the
	# link there was no way to tell "never needed one" from "tried and failed",
	# and the failed case never came back.
	frappe.db.set_value(ORDER_DOCTYPE, order.name, "gb_overpayment_pe", pe.name,
		update_modified=False)
	order.gb_overpayment_pe = pe.name
	# No commit here. This function became reachable from a Sales Invoice
	# on_submit handler (the amendment re-booking), and committing part way
	# through somebody else's submit breaks its atomicity: a later handler on
	# the same document — ffl_woo_sync's on_invoice_change is one — that throws
	# would leave the invoice, the journal and both receipts persisted while
	# the operator is shown an error. The request boundary commits anyway, and
	# notify_admins cannot fail the transaction.
	notify_admins(
		_("GunBroker order {0} was overpaid").format(order.gb_order_id),
		_("{0} banked {1} more than the invoice total. It is held as customer "
			"credit on Payment Entry {2}; refund it with a Pay entry against the "
			"same customer when the buyer asks."
		).format(order.name, amount, pe.name),
		dedup_key=f"gb_overpaid_{order.gb_order_id}", window_minutes=1440)
