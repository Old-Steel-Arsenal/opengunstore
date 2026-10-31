"""Book the revenue for a fulfilled retail web order.

The web fulfillment path (fulfillment.dispose_web_order) moves the guns out of
stock via FFL Dispositions and the store's own WooCommerce ShipStation plugin
ships the parcel — but nothing ever booked the SALE: Woo Online Orders are
docstatus-0 carriers, so web revenue never reached the GL (income-tax gross
receipts and the TX sales-tax base were understated). This module
mints the missing accounting document the moment an order is fully disposed:

- One submitted Sales Invoice per Transferred web order, mirroring the Woo
  amounts verbatim: lines at ``line_total / qty`` (discounts baked in), freight
  through the shared ``ffl_shipping_amount`` mechanism, and the tax
  WooCommerce actually collected as a single Actual row (the interstate-tax
  engine skips these SIs — there is no shipping Address to judge, and the
  destination call was made at Woo checkout).
- ``update_stock=0``: stock already left via the dispositions, so the
  ``_moves_stock`` guards skip the dealer-disposition and shippable-FFL hooks
  (no double bound-book entry). Real serials still go on the lines — the
  serial gate wants them and the invoice should be auditable per gun.
- ``ffl_destination_ffl`` carries the receiving FFL (the 4473 happens at THAT
  dealer — the 4473 gate's destination rule, applied honestly), and the SI is
  born ``shipstation_status='Shipped'`` (same convention as
  mark_shipped_manually: the label is bought outside our ShipStation push).
  list_pending_dispositions additionally excludes web-order SIs, so the
  Pending Order queue never shows them.
- Collection books through ``manual_order._book_payment_entry`` into the
  "Woo Clearing" account (Woo already holds the money). Month-end check: compare the Woo Clearing
  balance with Woo's payout / payment-gateway settlement report; the
  difference is processing fees, journaled Dr Merchant Processing Fees /
  Cr Woo Clearing.

Failure never rolls back a booked disposition: dispose_web_order wraps the
call, logs, and ``create_web_invoice_now`` retries — resumable at every stage
(draft left behind, submitted-but-unpaid) via the ``SI.woo_online_order`` key.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, nowdate

from ffl_core.customer_profile import (
	WEB_STORE_CUSTOMER,
	find_customer_by_email,
	new_retail_customer,
)
from ffl_core.utils import default_company as _default_company, guest_job_elevated

WEB_CUSTOMER = WEB_STORE_CUSTOMER   # one name, shared with the merge refusal
WOO_MODE_OF_PAYMENT = "WooCommerce"
WOO_CLEARING_ACCOUNT = "Woo Clearing"
# Mirror check: the SI must reproduce Woo's charge to the cent (one epsilon of
# float/rounding slack). Bigger drift = a draft + error log, never a silent post.
_TOTAL_TOLERANCE = 0.02


# ------------------------------------------------------------- ensure helpers


def _ensure_web_customer() -> str:
	"""The one shared cash-sale customer for web SIs. No email on purpose —
	send_sale_receipt then no-ops (the buyer already got Woo's receipt); the
	real buyer's name goes in the SI remarks."""
	existing = frappe.db.get_value("Customer", {"customer_name": WEB_CUSTOMER})
	if existing:
		return existing
	# Customer Group must be a LEAF node — the Selling Settings default when set,
	# else any non-group (a fresh site's default tree has e.g. "Individual").
	group = frappe.db.get_single_value("Selling Settings", "customer_group")
	if not group or frappe.db.get_value("Customer Group", group, "is_group"):
		group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name",
			order_by="name")
	doc = frappe.get_doc({
		"doctype": "Customer",
		"customer_name": WEB_CUSTOMER,
		"customer_type": "Individual",
		"customer_group": group,
		"territory": "All Territories",
	})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _resolve_web_customer(order) -> str:
	"""The SI billing party for a web order: the REAL buyer, deduped by billing
	email (get-or-create) — the plan-A switch off the shared aggregate so the Sales
	Report shows the real name natively (no report change). An email-less order
	(rare) falls back to the aggregate — logged, never silent.

	New buyers mirror the counter retail defaults: Individual + the neutral
	``_individual_group`` (NEVER the aggregate's mis-tagged "Non Profit") +
	non-exempt. Booking to the real buyer is tax-neutral — the web SI carries
	WooCommerce's own tax and the exemption / interstate engines already skip web
	SIs (``compliance.customer_tax_exempt`` / ``interstate_tax`` both short-circuit
	on ``woo_online_order``), so even a cross-channel email match never re-touches
	the tax."""
	email = (getattr(order, "customer_email", None) or "").strip()
	if not email:
		frappe.log_error(
			title=f"Web order {getattr(order, 'name', '?')}: no billing email",
			message="No billing email on the Woo order — booking the Sales Invoice to "
				"the shared aggregate customer (cannot dedupe / attribute to a real "
				"buyer). Review the order.")
		return _ensure_web_customer()
	# Dedup by email (case-insensitive at the DB collation): a repeat buyer merges
	# onto their existing Customer instead of sanding a new record each purchase.
	# Also finds the address of a merged-away duplicate (kept as an alias on the
	# customer it was merged into).
	existing = find_customer_by_email(email)
	# A retail buyer's address merged into a dealer must not put the sale on the
	# dealer (mirrors the GunBroker guard): treat an alias hit on a dealer as no match.
	if existing and not (existing.get("via_alias") and existing.get("is_ffl_dealer")):
		return existing.name
	doc = new_retail_customer(
		(getattr(order, "customer_name", None) or "").strip() or email,
		email=email,
		phone=(getattr(order, "customer_phone", None) or "").strip())
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _ensure_clearing_account(company: str) -> str:
	abbr = frappe.db.get_value("Company", company, "abbr")
	name = f"{WOO_CLEARING_ACCOUNT} - {abbr}"
	if frappe.db.exists("Account", name):
		return name
	# Sit next to the card clearing account when it exists (same reconciliation
	# story), else under the company's bank group.
	parent = (frappe.db.get_value("Account", f"Payroc Card Clearing - {abbr}", "parent_account")
		or f"Bank Accounts - {abbr}")
	acc = frappe.get_doc({
		"doctype": "Account",
		"account_name": WOO_CLEARING_ACCOUNT,
		"company": company,
		"parent_account": parent,
		"account_type": "Bank",
		"is_group": 0,
	})
	acc.flags.ignore_permissions = True
	acc.insert()
	return acc.name


def _ensure_mode_of_payment(company: str) -> str:
	account = _ensure_clearing_account(company)
	if frappe.db.exists("Mode of Payment", WOO_MODE_OF_PAYMENT):
		mop = frappe.get_doc("Mode of Payment", WOO_MODE_OF_PAYMENT)
		if not any(a.company == company and a.default_account for a in (mop.accounts or [])):
			mop.append("accounts", {"company": company, "default_account": account})
			mop.flags.ignore_permissions = True
			mop.save()
		return WOO_MODE_OF_PAYMENT
	mop = frappe.get_doc({
		"doctype": "Mode of Payment",
		"mode_of_payment": WOO_MODE_OF_PAYMENT,
		"type": "Bank",
		"enabled": 1,
		"accounts": [{"company": company, "default_account": account}],
	})
	mop.flags.ignore_permissions = True
	mop.insert()
	return WOO_MODE_OF_PAYMENT


def _sales_tax_account(company: str) -> str:
	"""The liability account the counter flow collects into — first row of the
	company's default Sales Taxes and Charges Template."""
	from ffl_core.compliance.interstate_tax import _default_template

	template = _default_template(company)
	# idx order, not the default `creation` (child rows tie on their parent's):
	# a CA store's template carries the CFET excise row second.
	account = template and frappe.db.get_value(
		"Sales Taxes and Charges",
		{"parent": template, "parenttype": "Sales Taxes and Charges Template"},
		"account_head", order_by="idx asc")
	if not account:
		frappe.throw(_(
			"No default Sales Taxes and Charges Template with a tax account is "
			"configured for {0} — cannot book the WooCommerce-collected tax."
		).format(company))
	return account


# ------------------------------------------------------------------- building


def _line_serials(order) -> dict[str, list[str]]:
	by_line: dict[str, list[str]] = {}
	for sr in (order.serials or []):
		serial = (sr.confirmed_serial or "").strip()
		if serial:
			by_line.setdefault(str(sr.woo_line_id), []).append(serial)
	return by_line


# Lanes whose goods came from a distributor's warehouse and never touched ours.
# `to_store` is deliberately NOT here: a procure-to-store unit IS received into
# our inventory and dispositioned locally, so its line is an ordinary stock line
# whose COGS the perpetual ledger already booked.
_SUPPLIER_LANES = frozenset({"fds", "direct"})


def _recorded_lanes(order) -> dict[str, str]:
	"""woo_line_id → the lane RECORDED on the order when it was placed.

	History, not current configuration: if an Item is re-sourced to a distributor
	after one of our own guns was sold on it, that sale was still a stock sale and
	its serial must stay pinned.
	"""
	lanes: dict[str, str] = {}
	for sr in (order.serials or []):
		lanes.setdefault(str(sr.get("woo_line_id")), sr.get("fulfillment") or "local")
	return lanes


def local_stock_lines(order) -> list:
	"""The order lines whose goods ship from OUR shelf as loose stock: a non-serialized,
	non-firearm stock item that is not on a drop-ship lane. These are the lines whose
	units come out of an in-store storage location when the order ships."""
	from ffl_core.firearm import is_firearm_item
	from ffl_woo_sync.woocommerce.webhook import _fulfillment_lane

	by_line = _line_serials(order)
	lanes = _recorded_lanes(order)
	out = []
	for line in (order.lines or []):
		code = line.item_code
		if not code or by_line.get(str(line.woo_line_id)) or is_firearm_item(code):
			continue
		item = frappe.get_cached_value("Item", code, ["is_stock_item", "has_serial_no"], as_dict=True)
		if not item or not cint(item.is_stock_item) or cint(item.has_serial_no):
			continue
		lane = line.get("lane") or lanes.get(str(line.woo_line_id))
		if lane is None:
			lane = _fulfillment_lane(code, is_firearm=False)
		if lane in _SUPPLIER_LANES:
			continue
		out.append(line)
	return out


def build_invoice_items(order) -> list[dict]:
	"""SI item rows mirroring the Woo lines: rate = post-discount line_total/qty,
	real serials joined on firearm lines (the serial gate wants them; the
	accessory lines just carry price).

	[DIVERGENCE-1] A drop-shipped line is different in three ways and each one
	matters: it is flagged ``delivered_by_supplier`` (ERPNext's own drop-ship
	semantics — no delivery, no stock movement), it carries no warehouse, and it
	carries **no serial**. The serial RSR reports for an FDS gun identifies a
	firearm that was never in our inventory or our bound book; pinning it would
	point ERPNext's serial machinery at a Serial No we do not have, and the
	``_moves_stock`` guards would try to book an exit for a gun we never held.
	``dropship_distributor`` is stamped so the Sales Report can price the line
	from the distributor order rather than from a valuation we never recorded.
	"""
	from ffl_woo_sync.woocommerce.sync import _dropship_distributor
	from ffl_woo_sync.woocommerce.webhook import _fulfillment_lane

	by_line = _line_serials(order)
	lanes = _recorded_lanes(order)
	items: list[dict] = []
	for line in (order.lines or []):
		qty = flt(line.qty) or 1
		row = {
			"item_code": line.item_code,
			"qty": qty,
			"rate": flt(line.line_total) / qty,
		}
		# The lane and source recorded on the line at checkout win; a line taken
		# before they were recorded falls back to its serial rows, then (an
		# accessory) to resolving it the way the webhook would have.
		lane = line.get("lane") or lanes.get(str(line.woo_line_id))
		if lane is None:
			lane = _fulfillment_lane(line.item_code, is_firearm=False)
		if lane in _SUPPLIER_LANES:
			row["delivered_by_supplier"] = 1
			row["warehouse"] = ""
			distributor = (line.get("source_distributor")
				or _dropship_distributor(line.item_code))
			if distributor:
				row["dropship_distributor"] = distributor
			items.append(row)
			continue
		serials = by_line.get(str(line.woo_line_id))
		if serials:
			row["serial_no"] = "\n".join(serials)
		elif line.get("storage_location"):
			# Where staff took these loose units from when the order shipped: rides the
			# invoice row, then the Delivery Note, which takes it out of that location.
			row["ffl_storage_location"] = line.storage_location
		items.append(row)
	return items


def _build_invoice(order, company: str):
	si = frappe.new_doc("Sales Invoice")
	si.company = company
	si.customer = _resolve_web_customer(order)
	si.update_stock = 0
	si.woo_online_order = order.name
	si.ffl_destination_ffl = (order.atf_ffl_record or "").strip() or None
	# The store's Woo ShipStation plugin buys web labels — the queue's retention
	# rule is satisfied at birth (mark_shipped_manually's convention).
	si.shipstation_status = "Shipped"
	# Revenue is recognised when the order left — its Transferred moment, stamped
	# on the order (WooOnlineOrder.validate / the dispose + pickup set_value sites).
	# A job that runs late (the 2-hourly backstop after a failed completion job)
	# must not move that sale into "today": the 2026-08-22 accessory orders were
	# booked on 8/26, and a 8/31 sale booked on 9/1 would leave the month. Orders
	# from before the stamp existed fall back to today, as before.
	transferred = get_datetime(order.get("transferred_at")) if order.get("transferred_at") else None
	si.posting_date = getdate(transferred) if transferred else nowdate()
	si.set_posting_time = 1
	if transferred:
		si.posting_time = transferred.time()
	si.ignore_pricing_rule = 1
	si.po_no = order.woo_order_id
	si.remarks = _("Web order {0} (Woo #{1}, {2} site). Buyer: {3}. Ships to FFL: {4}.").format(
		order.name, order.woo_order_id, order.site or "retail",
		order.customer_name or "?",
		order.ffl_business_name or order.ffl_license or "?")
	# The Error Log that explains a late booking is purged after 14 days; the
	# invoice itself must carry the trail — including the case where there is no
	# stamp to post on (an order Transferred before the field existed), which is
	# precisely when "why is the invoice date later than the shipment?" gets asked.
	if transferred and getdate(transferred) != getdate(nowdate()):
		si.remarks += _(" Booked {0}; order Transferred {1}.").format(nowdate(), transferred)
	elif not transferred:
		si.remarks += _(
			" Booked {0}; the order carries no Transferred timestamp (it left before "
			"that was recorded), so this invoice is dated the booking day."
		).format(nowdate())
	for row in build_invoice_items(order):
		si.append("items", row)
	# Customer-paid shipping as Woo books it (its freight tax rides in total_tax);
	# income is always the taxes table's Shipping row, never this field.
	si.ffl_shipping_amount = flt(order.shipping_total)
	tax = flt(order.total_tax)
	if tax:
		si.append("taxes", {
			"charge_type": "Actual",
			"account_head": _sales_tax_account(company),
			"description": _("Sales tax collected by WooCommerce (Woo #{0})").format(
				order.woo_order_id),
			"tax_amount": tax,
		})
	# WooCommerce partial refunds (D1): a recorded refund books this SI at the net
	# via a Net-Total discount — never a phantom -1 firearm line. No-op when the
	# order has no refunds (target_net == order_total). Lazy import: refunds.py
	# imports _ensure_paid from here.
	from ffl_woo_sync.woocommerce.refunds import apply_refund_discount
	apply_refund_discount(si, order)
	si.flags.ignore_permissions = True
	si.insert()
	return si


def _ensure_paid(si, order, posting_date=None) -> None:
	from ffl_core.api.manual_order import _book_payment_entry

	si.reload()
	outstanding = flt(si.outstanding_amount)
	if outstanding <= 0:
		return
	mode = _ensure_mode_of_payment(si.company)
	# posting_date: pin the collection to the SI's date (an amend-to-net re-collect
	# keeps a cross-month refund in the sale's month). None → get_payment_entry's
	# default (today), the fresh-invoice behaviour.
	_book_payment_entry(si, mode, outstanding, f"Woo #{order.woo_order_id}",
		posting_date=posting_date)


# ------------------------------------------------------------------ entry API


@guest_job_elevated()
def create_sales_invoice_for_web_order(order) -> dict:
	"""Idempotent + resumable: submitted SI → top up the payment if missing;
	dangling draft (earlier crash / total mismatch) → re-verify and submit;
	nothing yet → build, verify against Woo's total, submit, collect — then
	issue the stock of every accessory line shipped from our own shelf
	(:func:`deliver_web_stock_lines`).

	Runs elevated when reached from a Guest context: the accessory-only auto-clear
	enqueues this from the ``allow_guest`` webhook, and ERPNext's payment-entry
	helpers refuse Guest (see :func:`ffl_core.utils.guest_job_elevated` — the
	2026-08-22 incident: SI + payment rolled back, nothing
	retried). A staff session keeps its own identity. A failure here surfaces
	through the job's own Error Log and, from the second hour on, through the
	2-hourly backstop (``order_audit.scheduled_web_revenue_backstop``)."""
	if isinstance(order, str):
		order = frappe.get_doc("Woo Online Order", order)
	if (order.order_status or "") != "Transferred":
		frappe.throw(_(
			"{0} is not Transferred yet — dispose it before booking revenue."
		).format(order.name))
	# Serialise the three callers that can be in flight at once for one order —
	# the completion job, the 2-hourly backstop and a staff retry — on the
	# order row (same posture as refunds._reconcile), so two of them cannot both
	# read "no invoice yet" and each mint one.
	frappe.db.get_value("Woo Online Order", order.name, "name", for_update=True)
	# ...and read the order again UNDER the lock: a refund (or its breakdown)
	# saved between the caller's read and the lock must be in what is booked.
	order.reload()

	company = _default_company()
	existing = frappe.db.get_value(
		"Sales Invoice",
		{"woo_online_order": order.name, "docstatus": ("<", 2)},
		["name", "docstatus"], as_dict=True)

	if existing and existing.docstatus == 1:
		si = frappe.get_doc("Sales Invoice", existing.name)
		_ensure_paid(si, order, posting_date=si.posting_date)
		return {"sales_invoice": si.name, "status": "exists",
			"delivery_notes": deliver_web_stock_lines(si)}

	if existing:
		si = frappe.get_doc("Sales Invoice", existing.name)
		# A dangling draft may predate a refund's breakdown (or the refund itself):
		# re-apply the CURRENT refund booking before it is verified and submitted —
		# but only to a draft nobody has touched since _build_invoice inserted it
		# (modified == creation: every save, by staff or by this re-split, moves
		# `modified`). A draft staff have edited — typically after the
		# draft_total_mismatch alert — is theirs; it is verified as it stands.
		from ffl_woo_sync.woocommerce.refunds import apply_refund_discount, total_refunded
		if (total_refunded(order) > _TOTAL_TOLERANCE
				and get_datetime(si.modified) == get_datetime(si.creation)):
			apply_refund_discount(si, order)
			si.flags.ignore_permissions = True
			si.save()
	else:
		# A firearm in-store pickup is booked on its submitted POS Invoice; the
		# Sales Invoice only appears when that ticket consolidates (async, and it
		# can stay stuck — the */15 sweep alerts on that). Building one here
		# would book the sale twice once the merge lands.
		ticket = frappe.db.get_value(
			"POS Invoice",
			{"woo_online_order": order.name, "docstatus": 1, "is_return": 0},
			"name")
		if ticket:
			return {"sales_invoice": None, "status": "counter_sale", "pos_invoice": ticket}
		si = _build_invoice(order, company)

	# Mirror against the NET Woo owes us (gross order_total minus any recorded
	# partial refunds), not the gross — a refunded order is booked net (D1).
	from ffl_woo_sync.woocommerce.refunds import target_net
	net = target_net(order)
	diff = abs(flt(si.grand_total) - net)
	if diff > _TOTAL_TOLERANCE:
		# alert_once, not a bare log: the draft stays a backstop candidate, so a
		# bare log would repeat every two hours — and a mismatch nobody is told
		# about is an unbooked sale that goes quiet after the log's 14 days.
		from ffl_core.alerts import alert_once

		alert_once(
			"Sales Invoice", si.name,
			title=f"Web SI total mismatch for {order.name}",
			message=(
				f"Sales Invoice {si.name} grand_total {si.grand_total} != Woo net "
				f"{net} (order_total {order.order_total}, diff {diff:.2f}). Left in "
				"Draft for review — fix the lines/tax, then submit and run "
				"create_web_invoice_now again for the payment."),
			description=(
				f"Web order {order.name}: its invoice {si.name} could not be submitted "
				f"— the total differs from WooCommerce by {diff:.2f}. Fix the draft's "
				"lines/tax and submit it, then run create_web_invoice_now."),
		)
		return {"sales_invoice": si.name, "status": "draft_total_mismatch"}

	si.flags.ignore_permissions = True
	si.submit()
	# Collection pinned to the invoice's own date: a late booking (backstop) keeps
	# its Woo Clearing receipt in the sale's month, as the amend path already does.
	_ensure_paid(si, order, posting_date=si.posting_date)
	return {"sales_invoice": si.name, "status": "created",
		"delivery_notes": deliver_web_stock_lines(si)}


# ------------------------------------------------------ completion dispatch

ANTIQUE_COMPLETION_JOB = "ffl_woo_sync.woocommerce.antique.retire_antique_web_order"
REVENUE_COMPLETION_JOB = "ffl_woo_sync.woocommerce.revenue.create_sales_invoice_for_web_order"


def web_completion_job(order) -> str:
	"""Dotted path of the job that finishes a Transferred web order — the ONE
	place that decides "antique retire vs plain revenue booking". The webhook
	auto-clear enqueues it, the pickup button and the backstop run it inline
	(:func:`book_web_order_completion`); a third copy of this branch is how the
	backstop drifted from the main path in r1 of this change.

	An antique-carrying order needs its stock issued and its listing retired
	BEFORE the revenue (antique.py owns that sequence); every other order — a
	disposed firearm, an accessory-only sale — books revenue (and its accessory
	stock leg) directly.
	"""
	from ffl_core.firearm import is_antique_item

	# Attribute access: a Document, a frappe._dict and a test double all expose
	# the child table as ``.serials``; ``.get`` is not universal across them.
	if any(is_antique_item(sr.item_code) for sr in (getattr(order, "serials", None) or [])):
		return ANTIQUE_COMPLETION_JOB
	return REVENUE_COMPLETION_JOB


def book_web_order_completion(order) -> dict:
	"""Run :func:`web_completion_job` for ``order`` inline (pickup, backstop).

	``frappe.get_attr`` is the resolver, the same one ``frappe.enqueue`` uses for
	the webhook's copy of this decision — it re-reads the module attribute on
	every call, so the two routes cannot diverge.
	"""
	if isinstance(order, str):
		order = frappe.get_doc("Woo Online Order", order)
	return frappe.get_attr(web_completion_job(order))(order)


# ------------------------------------------------------ stock leg (accessories)

def _web_stock_rows(si, unplaced: list | None = None) -> list:
	"""The invoice rows whose goods left OUR shelf with no document to say so.

	A web order's firearms leave stock through the disposition's Material Issue
	(``disposition_stock``; antiques through ``antique.py``), and a drop-shipped
	line never touched our inventory (``delivered_by_supplier``, no warehouse —
	``build_invoice_items`` materialises the ``fds``/``direct`` lanes that way).
	Everything else — an optic, a bayonet, a case of ammo picked off our shelf
	and shipped by the store's ShipStation plugin — was, until this leg existed,
	sold on an ``update_stock=0`` invoice with nothing behind it: the Bin never
	moved, no COGS booked, and the item-level WooCommerce push (``mapping.
	stock_for_item`` = Bin total) kept advertising the units already gone.
	The incident (sold 1 on the web + 1 at the counter, POS and the
	storefront both still said 4) is the incident this exists for.

	Lane rule (supersedes the earlier "lane == local"
	wording): a ``to_store`` accessory IS issued — the unit was received into our
	warehouse before it shipped, exactly like ``_LOCALLY_DISPOSED_LANES``
	treats a to_store gun — and the row carries a warehouse to prove it. Only the
	supplier lanes are excluded, and they are excluded by the row itself.

	A stock row that qualifies but carries NO warehouse cannot be issued and
	must not vanish silently; it is appended to ``unplaced`` for the caller to
	alert on (``manual_order.warn_unresolved_stock_lines`` does the same for the
	counter path).
	"""
	from ffl_core.firearm import is_firearm_item

	rows = []
	for r in (si.items or []):
		if cint(r.get("delivered_by_supplier")) or flt(r.get("qty")) <= 0:
			continue
		if not cint(frappe.get_cached_value("Item", r.item_code, "is_stock_item")):
			continue
		if is_firearm_item(r.item_code):
			continue
		if not (r.get("warehouse") or "").strip():
			if unplaced is not None:
				unplaced.append(r)
			continue
		rows.append(r)
	return rows


def _stock_leg_state(row_names: list[str]) -> tuple[dict[str, float], set[str]]:
	"""What the Delivery Note table already says about these invoice rows.

	Returns ``(delivered, blocked)``: qty issued per row by SUBMITTED, non-return
	DN rows (keyed on ``si_detail``, the mapper's own back-link), and the rows a
	human is already touching — any DN row that is a draft, a cancellation, or a
	sales return. Those are never re-issued automatically: a cancelled DN was
	cancelled on purpose, a return DN means the goods came back, and re-issuing
	behind either is exactly the double-decrement the accountant review refused.
	"""
	delivered: dict[str, float] = {}
	blocked: set[str] = set()
	if not row_names:
		return delivered, blocked
	dn_rows = frappe.get_all(
		"Delivery Note Item",
		filters={"si_detail": ["in", row_names]},
		fields=["si_detail", "qty", "docstatus", "parent"],
	)
	if not dn_rows:
		return delivered, blocked
	returns = set(frappe.get_all(
		"Delivery Note",
		filters={"name": ["in", sorted({d.parent for d in dn_rows})], "is_return": 1},
		pluck="name",
	))
	for d in dn_rows:
		if cint(d.docstatus) != 1 or d.parent in returns:
			blocked.add(d.si_detail)
			continue
		delivered[d.si_detail] = delivered.get(d.si_detail, 0.0) + flt(d.qty)
	return delivered, blocked


def deliver_web_stock_lines(si) -> list[str]:
	"""Issue the stock (and book the COGS) of the invoice's undelivered accessory
	rows through one Delivery Note against the Sales Invoice — the stock leg of
	an ``update_stock=0`` web invoice, mirroring what ``manual_order.
	deliver_pending_stock_lines`` does for a deferred counter order (that helper
	is bound to Sales Order rows, which a web invoice has none of, so this one
	maps from the invoice itself: ERPNext's own ``make_delivery_note(si)``).

	Idempotent on ``si_detail``: rows already covered by a submitted DN stop
	matching; rows a human has touched (draft / cancelled / return DN) are
	alerted, never re-issued (:func:`_stock_leg_state`). Posted on the invoice's
	own date so the COGS lands in the sale's period. NEVER raises — the revenue
	must stand even when the shelf disagrees (a unit already missing from stock,
	a valuation gap, a frozen period): the failure is rolled back to its
	savepoint, Error-Logged and assigned to the managers as a ToDo (once — the
	ToDo dedupes the log), and the 2-hourly backstop retries.
	"""
	try:
		return _deliver_web_stock_lines(si)
	except Exception:
		# Belt and braces: the stock leg is subordinate to the revenue booking it
		# rides on, so nothing — a real DB deadlock that already killed the
		# savepoint, an alert that itself failed — may escape and roll back the
		# invoice above it (the very failure shape this change exists to end).
		try:
			frappe.log_error(
				title=f"Web stock delivery crashed: {si.name}",
				message=frappe.get_traceback(),
			)
		except Exception:
			pass
		return []


def _deliver_web_stock_lines(si) -> list[str]:
	if cint(si.get("update_stock")):
		# A stock-moving invoice already issued its rows at submit; a DN on top
		# would decrement twice. Web invoices are born update_stock=0
		# (_build_invoice), so this is a guard for callers, not a path.
		return []
	# Lock the invoice row for this transaction: the completion job, the backstop
	# and a staff retry can overlap, and two of them reading "nothing delivered
	# yet" would each issue a DN (refunds._reconcile's for_update, same reason).
	frappe.db.get_value("Sales Invoice", si.name, "name", for_update=True)
	unplaced: list = []
	rows = _web_stock_rows(si, unplaced)
	delivered, blocked = _stock_leg_state([r.name for r in rows])
	# One alert per run, carrying EVERY reason: alert_once dedupes on the
	# invoice's open ToDo, so a second call in the same run would be swallowed
	# and its reason lost.
	reasons = [
		f"{r.item_code} x {flt(r.qty)} carries no warehouse — the Item has no "
		"default warehouse for this company. Set one, then re-run "
		"create_web_invoice_now."
		for r in unplaced
	]
	wanted = {}
	for r in rows:
		owed = flt(r.qty) - delivered.get(r.name, 0.0)
		if owed <= 0:
			# Fully issued — a cancelled-then-amended Delivery Note leaves rows in
			# `blocked` that owe nothing, and those are not a problem to report.
			continue
		if r.name in blocked:
			reasons.append(
				f"{r.item_code}: {owed:g} of {flt(r.qty):g} still owes stock, but the "
				"row already has a draft, cancelled or return Delivery Note — not "
				"re-issued automatically. Finish or clear that document by hand, then "
				"re-run create_web_invoice_now.")
			continue
		wanted[r.name] = owed
	if reasons:
		_alert_web_stock_gap(si, "\n".join(reasons))
	if not wanted:
		return []
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_delivery_note

	savepoint = "deliver_web_stock_lines"
	frappe.db.savepoint(savepoint)
	try:
		dn = make_delivery_note(si.name)
		kept = []
		for d in dn.items:
			if d.si_detail not in wanted:
				continue
			d.qty = min(flt(d.qty), wanted[d.si_detail])
			if d.qty > 0:
				kept.append(d)
		if not kept:
			return []
		for idx, d in enumerate(kept, start=1):
			d.idx = idx
		dn.set("items", kept)
		# Freight is the invoice's business; on the DN it only pollutes ERPNext's
		# per-DN sales analytics (no GL impact either way).
		dn.set("taxes", [])
		dn.posting_date = si.posting_date
		dn.posting_time = si.posting_time
		dn.set_posting_time = 1
		dn.flags.ignore_permissions = True
		dn.insert()
		dn.submit()
		# A Delivery Note has no remarks field; the trail that the Error Log would
		# lose after 14 days lives on the document as a comment.
		dn.add_comment("Comment", _(
			"Stock leg of {0} (web order {1}, Woo #{2}): accessory rows issued "
			"against the invoice, posted on its date."
		).format(si.name, si.get("woo_online_order") or "?", si.get("po_no") or "?"))
		return [dn.name]
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		_alert_web_stock_gap(si, frappe.get_traceback())
		return []


def stock_leg_cancel_blockers(si_name: str) -> list[str]:
	"""Why this invoice's stock leg cannot be reversed automatically — empty when
	it can. **Read-only: ask before you touch anything.**

	Reversing the leg is a prerequisite for cancelling the invoice (Frappe's
	back-link check refuses an SI a submitted Delivery Note still points at), and
	the refund reconcile cancels the collection Payment Entry BEFORE it gets
	there. So a reason discovered mid-sequence would roll the refund back and
	leave the money unrecorded — the exact failure this change set exists to end.
	The reconcile therefore asks first and holds the order for a human instead.

	Three states a person owns and a job must not undo:
	  * a Delivery Note that also carries another invoice's rows (cancelling it
	    would un-ship someone else's goods);
	  * a submitted sales RETURN against our Delivery Note — the goods came back;
	    the original cannot be cancelled underneath it, and the return is itself
	    the accounting for that stock;
	  * a DRAFT Delivery Note against the invoice — invisible to Frappe's cancel
	    check, so the amend would go through and this orphan could later be
	    submitted against rows that were re-issued in the meantime (double count).
	"""
	blockers: list[str] = []
	rows = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_invoice": si_name, "docstatus": ["<", 2]},
		fields=["parent", "docstatus"],
	)
	for name in sorted({r.parent for r in rows}):
		dn = frappe.db.get_value(
			"Delivery Note", name, ["is_return", "docstatus"], as_dict=True)
		if not dn:
			continue
		if cint(dn.docstatus) == 0:
			blockers.append(
				f"Delivery Note {name} is a DRAFT against this invoice — submit or "
				"delete it first.")
			continue
		if cint(dn.is_return):
			blockers.append(
				f"Delivery Note {name} is a sales return of this invoice's stock — "
				"the goods are back on the shelf and the return already accounts for "
				"them; reconcile the refund by hand.")
			continue
		# Keep the predicate explicitly NULL/blank safe. Frappe currently wraps a
		# nullable Link ``!=`` filter in IFNULL, but this guard must not depend on
		# query-builder fallback semantics: an unlinked manual row still belongs to
		# somebody else's stock and cancelling the whole DN would un-ship it.
		linked_invoices = frappe.get_all(
			"Delivery Note Item",
			filters={"parent": name}, pluck="against_sales_invoice")
		if any((linked or "") != si_name for linked in linked_invoices):
			blockers.append(
				f"Delivery Note {name} also carries another invoice's rows — "
				"cancelling it would un-ship someone else's goods.")
	returns = frappe.get_all(
		"Delivery Note",
		filters={"return_against": ["in", sorted({r.parent for r in rows})] or [""],
			"docstatus": 1},
		pluck="name") if rows else []
	for name in returns:
		blockers.append(
			f"Delivery Note {name} is a submitted return against this invoice's stock "
			"leg — the goods came back; reconcile the refund by hand.")
	return blockers


def cancel_web_stock_lines(si) -> list[str]:
	"""Cancel the submitted, non-return Delivery Notes that issue this invoice's
	rows — the step a cancel of the invoice itself needs first.

	Never throws: :func:`stock_leg_cancel_blockers` is the gate, and the caller
	is contracted to run it before it starts mutating (see that docstring for
	why a mid-sequence refusal is worse than no refusal at all). Returns the
	cancelled Delivery Note names; the caller re-issues on the amended invoice
	via :func:`deliver_web_stock_lines`, which sees the amended rows as fresh.
	"""
	parents = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_invoice": si.name, "docstatus": 1},
		pluck="parent",
	)
	cancelled = []
	for name in sorted(set(parents)):
		dn = frappe.get_doc("Delivery Note", name)
		if cint(dn.is_return):
			continue
		dn.flags.ignore_permissions = True
		dn.cancel()
		cancelled.append(name)
	return cancelled


def _alert_web_stock_gap(si, reason: str) -> None:
	"""Error-Log the failed stock leg and assign a ToDo to the managers — once
	per invoice until the ToDo is closed (``ffl_core.alerts.alert_once``: the
	open-ToDo check gates the log too, so a shelf that stays wrong does not
	produce one identical Error Log per backstop run)."""
	from ffl_core.alerts import alert_once

	try:
		alert_once(
			"Sales Invoice", si.name,
			title=f"Web stock delivery failed: {si.name}",
			message=reason,
			description=_(
				"Web order {0}: the accessory stock for this invoice could not be "
				"issued (Delivery Note leg — see Error Log). Count the shelf, fix "
				"the stock, then re-run create_web_invoice_now."
			).format(si.get("woo_online_order") or si.name),
		)
	except Exception:
		frappe.log_error(
			title=f"Web stock delivery alert failed: {si.name}",
			message=frappe.get_traceback(),
		)


@frappe.whitelist()
def create_web_invoice_now(woo_online_order: str) -> dict:
	"""Manually retry the revenue Sales Invoice for a Transferred web order.

	For a web order whose revenue booking failed (error log / draft
	mismatch).

	Args:
	    woo_online_order: Woo Online Order name (must be Transferred).

	Returns:
	    The booking result from create_sales_invoice_for_web_order.

	Permissions:
	    Counter roles (same as the fulfillment actions).
	"""
	from ffl_core.api.manual_order import _ROLES

	frappe.only_for(_ROLES)
	return create_sales_invoice_for_web_order(woo_online_order)
