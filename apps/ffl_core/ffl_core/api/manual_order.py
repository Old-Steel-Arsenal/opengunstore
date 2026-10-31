"""Manual "build & ship" order API — the counter-to-FFL shipment loop.

The Create Order page (ffl_core/page/create_order) posts a payload here.
create_order builds a submitted Sales Order, derives a Sales Invoice from it
(make_sales_invoice), pins the chosen serials + pricing, optionally collects
full payment inline (is_pos), and submits. On submit the standard ffl_core
hooks run: the 4473 gate is skipped (ffl_destination_ffl is set — the buyer
does the 4473 at the destination FFL), the sale receipt is emailed, and — only
when the staff chose to ship now (update_stock=1) — one FFL/Dealer Transfer
disposition is booked per firearm serial.

When the staff defers shipment (dispose_now=False → update_stock=0) the invoice
is billing-only: the gun stays Active in stock and on the FastBound book, and
the order surfaces on the Pending Dispositions page. dispose_order() is the
deliberate "dispose & ship" action there — it books the transfer disposition,
whose own on_submit issues the Material Issue (stock-out) and enqueues the
FastBound push.

Invoice construction mirrors pos_4473: new doc → rows →
set_missing_values → save → re-pin economics → save. Each set_missing_values()
call also re-derives taxes from the customer/POS-Profile default, so a
tax-exempt order's blank template gets re-asserted (_reassert_tax_exemption)
right alongside every re-pin.
"""
from __future__ import annotations

import json as _json

import frappe
from frappe import _
from datetime import date

from frappe.utils import cint, flt, get_datetime, getdate, now_datetime, nowdate

from ffl_core.api.counter_pickup import (
	LINK_FIELD as PICKUP_LINK_FIELD,
	is_pickup_order,
	open_pickup_drafts_for_invoices,
	pickup_block,
	submitted_pickup_invoices,
	ticket_remarks,
)
from ffl_core.compliance import channel_invoice, cr_transfer, dealer_transfer, le_excise_exempt
from ffl_core.compliance.firearm import is_awaiting_procurement_row
from ffl_core.customer_name import refresh_customer_name_snapshots
from ffl_core.firearm import (
	available_serials_with_prices,
	is_firearm_item,
	is_regulated_firearm_item,
)
from ffl_core.reservations import (
	channel_claim_block_reason,
	channel_presale_probe,
	channel_hold_reason,
	counter_holds,
	serial_shelf_state,
	shelf_state_columns,
	si_firearm_serial_state,
	si_pending_firearm_serials,
)
from ffl_core.setup.payments import CHECK_MONEY_ORDER, ZELLE

from ffl_core import agreements, price_approval, storage
# _coerce_payload kept under this name: price_approval imports it from here.
from ffl_core.api._helpers import coerce_payload as _coerce_payload
from ffl_core.roles import COUNTER_ROLES
from ffl_core.compliance.shipping_charge import shipping_tax_inclusive_setting
from ffl_core.utils import set_missing_values_keeping_taxes

_ROLES = COUNTER_ROLES  # canonical tuple moved to ffl_core.roles (endpoint-gates)
# Zelle (a manual bank transfer with no gateway) must carry its app/bank
# confirmation number so the deposit can be reconciled; it's stored on the
# payment's reference_no and enforced server-side in compliance.payment_mode.
# ZELLE is the canonical mode name from ffl_core.setup.payments, shared with that
# gate so a rename can't silently desync them.
_MODES_OF_PAYMENT = ("Cash", "Credit Card", ZELLE, "ACH", CHECK_MONEY_ORDER)
# Buyer type drives the shipping gate (an individual must pay before its guns
# ship; a business — an FFL buying for its own license — may ship on account).
_BUYER_INDIVIDUAL = "Individual"
_BUYER_BUSINESS = "Business"
# An invoice is "paid" when its outstanding rounds to zero (a cent of tolerance).
_PAID_TOLERANCE = 0.005
# Order-line `source` values that order goods from a distributor (a blank source
# is local stock): drop-ship ships from the distributor, to_store ships to us.
_SOURCE_DROPSHIP = "dropship"
_SOURCE_TO_STORE = "to_store"
_ORDERED_SOURCES = (_SOURCE_DROPSHIP, _SOURCE_TO_STORE)


def _is_paid(outstanding) -> bool:
	return flt(outstanding) <= _PAID_TOLERANCE


def _leg_needs_full_payment(order_type) -> bool:
	"""Whether a Draft distributor leg of this order type waits for full payment
	— the queue's read of ffl_integrations' one rule (leg_needs_full_payment:
	drop-ship yes, ship-to-store no). Fails CLOSED without the integration app:
	a leg can only exist with it installed, so the answer never matters then."""
	try:
		from ffl_integrations.distributor.api import leg_needs_full_payment
	except ImportError:
		return True
	return leg_needs_full_payment(order_type)


def _has_ship_role() -> bool:
	"""True if the current user holds a role that may see firearm-shipment / order
	data. The non-raising counterpart to frappe.only_for(_ROLES): use it to gate a
	passive background read that should degrade to empty (no error toast) rather
	than raise — e.g. the reserved-serial badge fired on every Item-form open."""
	return bool(set(_ROLES).intersection(frappe.get_roles()))


def _normalize_buyer_type(payload) -> str:
	is_business = (str(payload.get("buyer_type") or "")).strip().lower() == "business"
	return _BUYER_BUSINESS if is_business else _BUYER_INDIVIDUAL


def _is_business(si) -> bool:
	return getattr(si, "ffl_buyer_type", None) == _BUYER_BUSINESS


def _shipstation_enabled() -> bool:
	"""Is the ShipStation push (the Pending Order Ship button) available?
	Soft-guarded read of the ffl_integrations single (try/except, not a raw
	tabSingles read — the docstring said otherwise for a while) so the queue keeps
	working when ffl_integrations isn't installed."""
	try:
		return bool(int(frappe.db.get_single_value("ShipStation Settings", "enabled") or 0))
	except Exception:
		return False


def _try_push_shipment(sales_invoice: str) -> tuple[bool, str]:
	"""Best-effort ShipStation push for Dispose & Ship Now. The order and its
	disposition are committed facts by the time this runs, so a push failure
	must NEVER bubble — the order simply stays on the Pending Order queue for
	a manual Ship. Lazy get_attr keeps ffl_core import-independent of
	ffl_integrations (absent app behaves like disabled)."""
	if not _shipstation_enabled():
		return False, ""
	try:
		frappe.get_attr("ffl_integrations.shipstation.api.push_shipment")(sales_invoice)
		return True, ""
	except Exception as exc:
		frappe.log_error(
			title=f"Auto-push to ShipStation failed: {sales_invoice}",
			message=frappe.get_traceback())
		return False, str(exc)


# The status stamps that mean "never pushed NOR stamp-cleared" — the ONE
# word-list shipstation_pushed and the queue's arm3 SQL prefilter both read,
# so adding a new unpushed-looking status can never make arm3 silently drop
# pending rows while the helper keeps returning False. NULL joins the club in
# each consumer's own NULL-safe way (SQL needs a separate "is not set" arm).
_UNPUSHED_STATUSES = ("", "Not Requested")


def shipstation_pushed(shipstation_order_id, shipstation_status) -> bool:
	"""THE definition of "already pushed to ShipStation" — shared by the queue
	retention rule, push_shipment's idempotency guard, and the legacy stamp
	patch so the three can never drift. NULL-safe: SIs predating the
	ShipStation custom fields carry NULL, which reads as not-pushed exactly
	like 'Not Requested'."""
	return bool((shipstation_order_id or "").strip()) \
		or (shipstation_status or "") not in _UNPUSHED_STATUSES


def _requires_full_payment_to_ship(is_business: bool) -> bool:
	"""THE buyer-type shipping policy, stated once: an individual's firearms
	leave the store only fully paid; a business — an FFL buying on its own
	license — may ship on account. ship_payment_block applies it to invoice
	state (dispose_order / push_shipment); create_order's dispose_now gate
	applies it to the pre-submit payment intent (a deposit's Payment Entry
	only books after submit, so outstanding isn't final there)."""
	return not is_business


def ship_arrival_block(si) -> str | None:
	"""The ship-to-store arrival rule, shared by mark_shipped_manually AND
	push_shipment (the two clear-the-shipping-half actions). The order ships as
	one parcel to one destination, so while a ship-to-store Distributor Order is
	unreceived, shipping would send it out short — and the Delivery Note leg would
	issue someone else's shelf stock in place of the goods still on their way.
	Returns the throwable message, or None."""
	waiting = _unreceived_to_store_orders(si.name)
	if not waiting:
		return None
	return _(
		"{0} is still waiting for ship-to-store goods — receive {1} first."
	).format(si.name, ", ".join(waiting))


def ship_payment_block(si) -> str | None:
	"""The pre-ship payment rule, shared by dispose_order AND push_shipment
	(the two independent Pending Order actions) so the compliance gate can
	never desync. Returns the throwable message, or None when shipping may
	proceed."""
	if _requires_full_payment_to_ship(_is_business(si)) and not _is_paid(si.outstanding_amount):
		return _(
			"{0} is an individual order with an outstanding balance — record the "
			"payment before shipping."
		).format(si.name)
	return None


def ffl_premise(ffl_number: str) -> frappe._dict:
	"""Canonical ATF FFL Record premise read + the DBA-first company name —
	the ONE source feeding the queue's ship-to preview, the ShipStation
	payload builder, and the ship-Address creation, so the three can never
	drift. Empty dict when the record doesn't exist."""
	ffl = frappe.db.get_value(
		"ATF FFL Record", ffl_number,
		["licensee_name", "licensee_dba", "street", "street2", "city",
			"state", "zip", "phone", "expiry_date"],
		as_dict=True)
	if not ffl:
		return frappe._dict()
	ffl.company = ffl.licensee_dba or ffl.licensee_name or ffl_number
	return ffl


def invoice_ship_address(si) -> frappe._dict | None:
	"""Canonical read of a counter order's OWN ship-to Address — the no-FFL
	sibling of ffl_premise, and the ONE completeness rule ("resolves to an
	Address with a first street line") shared by the queue's ship_to_preview,
	push_shipment's precheck, and build_ship_to's fallback, so the three can
	never drift. Deliberately NOT shared with distributor drop-ship's
	_recover_ship_to (ffl_integrations/distributor/counter.py) — a drop-ship
	destination must be COMPLETE (line1+city+state+zip), a label fallback
	only needs a street line; folding them would loosen the stricter rule.
	Accepts a doc or a get_all row. Returns the OrderPlan-shaped block
	(line1/line2/city/state/zip/phone), or None when the invoice has no
	shipping Address or it is unusable for a label."""
	name = (getattr(si, "shipping_address_name", None) or "").strip()
	if not name:
		return None
	addr = frappe.db.get_value(
		"Address", name,
		["address_line1", "address_line2", "city", "state", "pincode", "phone"],
		as_dict=True) or frappe._dict()
	if not (addr.address_line1 or "").strip():
		return None
	return frappe._dict(
		line1=addr.address_line1, line2=addr.address_line2 or None,
		city=addr.city, state=addr.state, zip=addr.pincode, phone=addr.phone)


@frappe.whitelist()
def create_order(payload) -> dict:
	"""Build a counter order: submitted Sales Order → Sales Invoice.

	The Create Order page's single entry point. Handles firearm orders
	(destination FFL required, optional immediate Dispose & Ship),
	accessory/direct orders, distributor drop-ship legs, tax exemption,
	and full or partial payment. Everything runs in one transaction — a
	failure anywhere rolls the whole order back.

	Args:
	    payload: {
	        buyer_type: "individual" | "business",
	        customer, destination_ffl?, mode_of_payment,
	        payment_received: bool, paid_amount?, shipping_amount?,
	        dispose_now: bool,
	        items: [{item_code, qty, rate, serials: [...], storage_location?}] —
	            storage_location: the in-store location a loose-stock line is
	            taken from (an open location; the units leave it when the stock
	            does, and nothing blocks if it holds less),
	        recipient_name?, recipient_phone?, contact_email?,
	        pickup_in_store?: bool — the customer collects here: no ship-to is
	            stored; drop-ship lines and a destination FFL are refused, and a
	            regulated firearm is allowed only as a ship-to-store line (it is
	            handed over on a POS Invoice, with its 4473, once it arrives),
	        ship_to?: {address_line1, address_line2, city, state, pincode},
	        notes?, pos_profile?, tax_exempt?, tax_certificate?, draft?,
	        le_excise_exempt?, le_exempt_buyer?, le_exempt_certificate? —
	            a peace officer's (or their agency's) sale: the excise tax comes
	            off, sales tax stays; buyer and CDTFA-230-FET certificate required
	            (compliance.le_excise_exempt),
	        price_approval? — a manager's approval token for prices below list
	            (price_approval.approve; needed while FFL Settings asks for one)
	        } — dict or JSON string.

	Agreements the buyer has not signed yet (FFL Settings › Used Item Agreement,
	an Item's required agreement) do not stop the order: each is emailed to the
	order's email (else the customer's) and Dispose waits for it; a dispose_now
	order is then held back on Pending Order (``ship_held_for_agreements``). A
	failed or impossible email never undoes the order — it is reported.

	Returns:
	    {ok, sales_order, sales_invoice, paid_amount, outstanding,
	    disposed, ship_pushed, ship_error, email_to, agreements: [{template,
	    serial_no, state: sent / link_open / failed, email, token_expires,
	    error}], agreement_email_error, ship_held_for_agreements}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	return _create_order(payload)


def _create_order(payload, *, shipping_tax_inclusive: int | None = None,
		prior_rates: dict | None = None) -> dict:
	"""create_order's body. ``shipping_tax_inclusive`` pins the freight tax
	basis instead of reading FFL Settings — update_order passes the edited
	order's own. Never a payload key, so a
	client cannot re-price the tax. ``prior_rates`` (update_order): the edited
	order's line prices, which the rebuild keeps without a manager's approval."""
	payload = _coerce_payload(payload)

	customer = (str(payload.get("customer") or "")).strip()
	if not customer or not frappe.db.exists("Customer", customer):
		frappe.throw(_("Pick the buyer (Customer) for this order."))
	# Sales-tax exemption. The page's checkbox is authoritative for THIS order;
	# other callers fall back to the customer's standing flag. The standing
	# Customer.is_tax_exempt (+ optional certificate) is persisted only here, at
	# order creation — never on an exploratory page toggle. Checking the box is
	# enough to exempt; the certificate is optional (kept on file when supplied).
	if "tax_exempt" in payload:
		tax_exempt = bool(int(payload.get("tax_exempt") or 0))
		set_customer_tax_exempt(customer, 1 if tax_exempt else 0,
			(str(payload.get("tax_certificate") or "")).strip() or None)
	else:
		tax_exempt = bool(frappe.db.get_value("Customer", customer, "is_tax_exempt"))
	le_exemption = _le_exemption(payload)

	rows, _serials, has_regulated = _validate_items(payload.get("items") or [])
	# Counter pickup: the customer collects here, so the order HAS no destination —
	# no address is asked for and none is stored (which is what makes the invoice a
	# pickup downstream). Anything that needs a destination is refused here rather
	# than booked against a blank one.
	pickup = bool(int(payload.get("pickup_in_store") or 0))
	if pickup:
		_assert_pickup_order(rows, payload, has_regulated)
		payload["ship_to"] = {}
		# Nothing is shipped, so nothing is charged for shipping. The page clears the
		# box when Pickup goes on, but a figure typed AFTER that would otherwise ride
		# the payload through — the customer is standing at the counter.
		payload["shipping_amount"] = 0
	has_dropship = _order_has_dropship(rows)
	# Any distributor-supplied line (drop-ship OR ship to store) defers the whole
	# order and builds its Distributor Order(s) at submit.
	has_ordered_leg = _order_has_ordered_leg(rows)

	# ffl_shipping_amount = what the customer pays for shipping (sales tax inside it
	# when the order is tax-inclusive); income is always the taxes table's Shipping row.
	shipping_amount = flt(payload.get("shipping_amount") or 0)
	if shipping_amount < 0:
		frappe.throw(_("Shipping amount can't be negative."))
	if shipping_tax_inclusive is None:
		shipping_tax_inclusive = shipping_tax_inclusive_setting()
		# The page priced the shipping on the basis it loaded; if FFL Settings
		# changed since, the total it showed the customer is no longer the one
		# this order would book — refuse rather than charge a different amount.
		if ("shipping_tax_inclusive" in payload and shipping_amount > 0
				and cint(payload.get("shipping_tax_inclusive")) != shipping_tax_inclusive):
			frappe.throw(_(
				"The shipping sales-tax setting changed since this page was loaded — "
				"reload the page to see the correct total, then create the order."),
				title=_("Shipping Tax Setting Changed"))

	destination_ffl = (str(payload.get("destination_ffl") or "")).strip() or None
	# A pickup's gun is ordered to the store and handed over here with a 4473
	# (api/counter_pickup) — the one firearm order with no destination FFL.
	if has_regulated and not pickup:
		if not destination_ffl:
			frappe.throw(_("Select a destination FFL — this order ships firearms."))
		_require_valid_ffl(destination_ffl)

	# Drop-ship restricted-state block: a drop-ship ACCESSORY is checked against
	# the buyer's ship-to state, a drop-ship FIREARM against the FFL premise state.
	# Uses the ORIGINAL payload ship-to, not the resolved Address.
	if has_dropship:
		# Order matters: a complete destination must exist BEFORE the restriction is
		# evaluated against it, or a blank state passes the check silently.
		_assert_dropship_ship_to(rows, payload)
		# Mixed FDS + direct legs are allowed, but only into ONE state —
		# the invoice taxes a single destination.
		_assert_single_destination_state(rows, payload, destination_ffl)
	if has_ordered_leg:
		_validate_dropship_restrictions(rows, payload.get("ship_to"), destination_ffl)

	# Buyer type drives the pre-ship payment rule; the authoritative
	# full-payment-before-dispose check runs after the invoice totals exist
	# (a deposit can only be judged against the real taxed grand total).
	is_business = _normalize_buyer_type(payload) == _BUYER_BUSINESS
	# Distributor drop-ship is individual-retail only: Business/dealer excluded.
	_assert_dropship_buyer(rows, is_business)

	# The order books under the company that actually stocks the firearms —
	# derived from the serials' warehouses — so the per-line warehouses never
	# cross companies (ERPNext rejects a warehouse owned by another company).
	# Non-firearm orders fall back to the default company.
	order_company = _company_from_warehouses(rows)

	# Resolve a POS Profile for company/warehouse + is_pos payment wiring. Prefer
	# an explicit payload profile, then the user's open shift, then an enabled
	# profile for the order's company — building an order does NOT require manually
	# opening a POS shift (the invoice isn't is_created_using_pos, so ERPNext never
	# asks for one).
	pos_profile = (
		_resolve_pos_profile((str(payload.get("pos_profile") or "")).strip() or None)
		or _default_pos_profile(order_company))
	if not pos_profile:
		frappe.throw(_(
			"No POS Profile is configured — set one up before creating orders."))
	profile = frappe.db.get_value(
		"POS Profile", pos_profile, ["company", "warehouse"], as_dict=True)
	if not profile:
		# An explicit payload profile is returned by _resolve_pos_profile without
		# an existence check; a bogus/deleted name lands here (was an AttributeError).
		frappe.throw(_(
			"POS Profile {0} no longer exists — pick another."
		).format(pos_profile))
	company = profile.company
	if order_company and company != order_company:
		# A profile for a different company than the guns are stocked in would
		# deduct from the wrong company's ledger and trip ERPNext's warehouse-
		# company check — fail with an actionable message instead.
		frappe.throw(_(
			"These firearms are in {0}'s inventory, but POS Profile {1} books to "
			"{2}. Use a {0} POS Profile (or open a {0} POS shift)."
		).format(order_company, pos_profile, company),
			title=_("Wrong company for this inventory"))
	# A location picked on a line must be one of THIS order's company.
	for r in rows:
		if r.get("storage_location"):
			storage.validate_open_location(r["item_code"], r["storage_location"], company)
	warehouse = profile.warehouse or _store_warehouse(company)
	if not warehouse:
		frappe.throw(_(
			"POS Profile {0} has no warehouse and company {1} has none configured "
			"— set a warehouse before creating orders."
		).format(pos_profile, company))

	# Structured ship-to → an Address linked to the customer. Firearm orders ship
	# to the destination FFL — derive that address from the ATF FFL Record so the
	# ship-to (and the destination-based sales-tax decision) is authoritative
	# regardless of what the page echoed; otherwise the buyer's payload ship-to.
	ship_to, ship_recipient = _resolve_ship_to(payload, destination_ffl)
	ship_address = _ensure_ship_address(customer, ship_recipient, ship_to)
	if not destination_ffl and not has_dropship and not ship_address \
			and (str(ship_to.get("address_line1") or ship_to.get("line1") or "")).strip():
		# _ensure_ship_address is best-effort (logs and returns None) and the
		# FFL/drop-ship throws below don't cover a plain direct order — the
		# operator typed an address, the order would silently book as a
		# counter pickup, and only the error log would know. Say so.
		frappe.msgprint(_(
			"The shipping address could not be saved — this order books as a "
			"counter pickup (no Ship button). Check the address and use Edit, "
			"or see the error log."), alert=True, indicator="orange")
	if destination_ffl and not ship_address:
		# A firearm ships to the FFL; without a shipping Address the destination-
		# based tax engine can't read the state and would wrongly tax the sale.
		frappe.throw(_(
			"Could not build the shipping address from destination FFL {0} — "
			"complete its street/city/state/ZIP and retry."
		).format(destination_ffl), title=_("Destination FFL Address Incomplete"))
	if has_dropship and not ship_address:
		# _ensure_ship_address is best-effort (it logs and returns None on
		# failure). For a drop-ship order that silence is a TAX hole — with no Address
		# on the invoice, interstate_tax has no destination to read and falls back to
		# the in-state default, over-collecting on an out-of-state sale. Fail loudly.
		frappe.throw(_(
			"The shipping address for this drop-ship order could not be saved — without "
			"it sales tax would fall back to the in-state rate. Check the address and "
			"try again."), title=_("Shipping Address Not Saved"))

	# A lowered price needs a manager (price_approval): the page asked one before sending and
	# passes the approval token, taken once here for both the order and its invoice.
	token = (str(payload.get("price_approval") or "")).strip()
	approval = None
	if token:
		try:
			approval = price_approval.take_token(token)
		except price_approval.PriceApprovalRequired:
			# Expired, used, or someone else's: worth nothing, but no reason to refuse an order that needs
			# no approval at all — the validate gate still refuses one that does.
			frappe.clear_last_message()

	# --- Sales Order -------------------------------------------------------
	so = frappe.new_doc("Sales Order")
	price_approval.attach(so, approval, prior_rates)
	so.company = company
	so.customer = customer
	so.delivery_date = frappe.utils.add_days(nowdate(), 7)
	so.set_warehouse = warehouse
	if destination_ffl:
		so.ffl_destination_ffl = destination_ffl
	if ship_address:
		so.shipping_address_name = ship_address
	for r in rows:
		line = {"item_code": r["item_code"], "qty": r["qty"], "rate": r["rate"]}
		if r.get("warehouse"):
			# Ship each firearm from the warehouse its serial is actually in, not
			# the POS profile default — otherwise update_stock 404s on the gun.
			line["warehouse"] = r["warehouse"]
		if r.get("storage_location"):
			# Carried to the invoice and, for a deferred order, to its Delivery Note.
			line["ffl_storage_location"] = r["storage_location"]
		row = so.append("items", line)
		if r.get("source") in _ORDERED_SOURCES:
			price_approval.mark_quoted(row)   # priced from the distributor's quote, not a list price
	# Destination-based sales tax — unless the customer is tax exempt, in which
	# case no template is applied (0%). Otherwise apply the company default so an
	# in-state shipment is taxed; compliance.interstate_tax.apply_interstate_tax
	# (on validate) swaps it for the 0% Out-of-State template for out-of-state.
	if not tax_exempt:
		_apply_default_tax_template(so, company)
	so.update(le_exemption)
	so.ffl_shipping_amount = shipping_amount
	so.ffl_shipping_tax_inclusive = shipping_tax_inclusive
	so.flags.ffl_shipping_tax_basis_set = True  # stamp_shipping_tax_basis keeps it
	so.flags.ignore_permissions = True
	so.insert()
	so.submit()
	notes = (str(payload.get("notes") or "")).strip()

	# --- Sales Invoice (derived from the SO) -------------------------------
	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

	dispose_now = bool(payload.get("dispose_now"))
	# A drop-ship line forces the whole order to DEFER — ERPNext
	# hard-throws on update_stock=1 + delivered_by_supplier. `ship_now` is the single
	# source the update_stock write, the pre-ship payment gate, and the `disposed`
	# flag all read, so they can never disagree.
	ship_now = bool(_counter_update_stock(dispose_now=dispose_now, has_dropship=has_ordered_leg))
	if dispose_now and not ship_now:
		# Any distributor line defers the WHOLE order (ERPNext refuses update_stock=1
		# with a delivered_by_supplier row; a ship-to-store line has nothing in stock
		# yet) — local and arrived lines ship from the Pending Order queue, drop-ship
		# lines from the distributor once their order is placed.
		frappe.msgprint(_(
			"This order contains distributor lines, so the WHOLE order defers: local "
			"and ship-to-store lines ship from the Pending Order queue, drop-ship "
			"lines ship from the distributor once their order is placed."), alert=True)
	si = make_sales_invoice(so.name)
	price_approval.attach(si, approval, prior_rates)
	si.pos_profile = pos_profile
	if destination_ffl:
		si.ffl_destination_ffl = destination_ffl
	# The positive pickup marker: "no FFL and no address" also describes a
	# broken invoice, and a pickup with a gun on it must never read as one.
	si.ffl_pickup_in_store = 1 if pickup else 0
	si.ffl_buyer_type = _BUYER_BUSINESS if is_business else _BUYER_INDIVIDUAL
	si.update(le_exemption)
	si.ffl_shipping_amount = shipping_amount
	si.ffl_shipping_tax_inclusive = shipping_tax_inclusive
	si.flags.ffl_shipping_tax_basis_set = True
	if ship_address:
		si.shipping_address_name = ship_address
	if not tax_exempt:
		_apply_default_tax_template(si, company)
	email = (str(payload.get("contact_email") or "")).strip()
	if email:
		si.contact_email = email  # ensures the on-submit receipt has a recipient
		_remember_customer_email(customer, email)
	if notes:
		# Dedicated ffl_staff_note is the note's ONE home (queue row reads it).
		# Deliberately NOT ERPNext's shared `remarks`, which standard print
		# formats put on the customer-facing invoice — this note is internal.
		si.ffl_staff_note = notes
	_apply_serials(si, rows)
	si.set_missing_values()
	# set_missing_values() (via SellingController.set_missing_lead_customer_details
	# → _get_party_details, and — once is_pos is set below — set_pos_fields' POS
	# Profile fallback) independently re-derives taxes_and_charges/taxes from the
	# customer/company/profile default, silently overriding the insert-time skip
	# above. Re-assert the exemption right before every insert/save that follows
	# a set_missing_values() call — see _reassert_tax_exemption's docstring.
	_reassert_tax_exemption(si, tax_exempt)
	si.flags.ignore_permissions = True
	si.insert()

	# Agreements the buyer has not signed (a used gun's acknowledgment, say) do not
	# stop the order: a phone / ship-to-FFL buyer is not here to sign, so the links
	# are emailed below. The gun waits for them instead — Dispose on Pending Order
	# checks again — so a Dispose & Ship Now is held back to a deferred order.
	unsigned = agreements.missing_requirements(si)
	si.flags.ffl_defer_agreements = True
	# Only a gun waits: Pending Order's Dispose is the step that checks again.
	ship_held_for_agreements = bool(unsigned and ship_now and has_regulated)
	if ship_held_for_agreements:
		ship_now = False
		frappe.msgprint(_(
			"{0} has not signed {1} yet, so this order is not disposed now. The signing "
			"link is emailed; the gun ships from Pending Order once it is signed."
		).format(frappe.bold(frappe.utils.escape_html(si.customer_name or customer)),
			_agreement_names(unsigned)), alert=True, indicator="orange")

	# Re-pin staff economics + serials (set_missing_values reset them), and set
	# update_stock — ship now → 1, defer → 0 — AFTER insert so it wins over the
	# dealer-shipment before_insert auto-enable (which forces it on for FFL buyers).
	# A drop-ship line forces defer (see _counter_update_stock).
	_repin_economics(si, rows)
	_reassert_tax_exemption(si, tax_exempt)
	si.update_stock = 1 if ship_now else 0
	si.save()

	# --- Payment (manually flagged) ---------------------------------------
	payment_received = bool(payload.get("payment_received"))
	mode_of_payment = (str(payload.get("mode_of_payment") or "")).strip()
	paid_amount = 0.0
	partial_deposit = False
	transaction_number = (str(payload.get("transaction_number") or "")).strip()
	if payment_received:
		if mode_of_payment not in _MODES_OF_PAYMENT:
			frappe.throw(_("Pick a valid payment method to mark this order paid."))
		if mode_of_payment in _REFERENCED_BANK_MODES and not transaction_number:
			frappe.throw(_("Enter the {0} to mark this order paid by {1}.").format(
				_reference_name(mode_of_payment), mode_of_payment))
		# The deposit leg books a Payment Entry, not an invoice payment row, so
		# the Sales Invoice submit gate never sees it — this early check is the
		# ONLY gate a card deposit meets (the deposit leg had none).
		# Card marked paid here never ran the A80 (the terminal path books on
		# account and charges after), so with the terminal live it was taken on
		# the Virtual Terminal — same rule as record_payment.
		if not transaction_number and _card_reference_required_for(mode_of_payment, off_terminal=True):
			frappe.throw(_(
				"Enter the approval code from the terminal or Virtual Terminal receipt "
				"to take this {0} payment — it is the only thing tying the payment to "
				"the processor's batch."
			).format(mode_of_payment))
		_reject_pan_reference(transaction_number)
		# Honour the amount the operator collected (the page defaults it to the
		# taxed grand total; an empty amount means "the full total").
		grand_total = flt(si.grand_total)
		paid_amount = flt(payload.get("paid_amount")) or grand_total
		if paid_amount < 0:
			frappe.throw(_("Payment amount can't be negative."))
		if paid_amount + 0.01 < grand_total:
			# Partial payment (deposit): the is_pos inline tender can't submit
			# underpaid, so the invoice books on account and the deposit lands
			# as a Payment Entry right after submit — the same mechanism the
			# Pending Order page's Record Payment uses for the balance.
			partial_deposit = True
		else:
			# Full payment stays on the is_pos inline tender ON PURPOSE: that
			# is what books the counter's cash-drawer/POS-reconciliation way.
			# Only deposits/balances go the Payment Entry route — a deliberate
			# accounting split, not an omission.
			si.is_pos = 1
			si.pos_profile = pos_profile
			# set_missing_values rebuilds the POS payment table from the profile and
			# zeroes the amounts, so pin the tender AFTER it (the same re-pin lesson
			# _repin_economics applies to rates) — otherwise the staff amount is wiped
			# and the invoice submits Unpaid with full outstanding.
			#
			# ...keeping this invoice's OWN tax decision — the template AND its
			# rows. set_pos_fields copies the register's taxes_and_charges over
			# ours unconditionally on this pass, and the empty taxes table is
			# then refilled from THAT template. Both halves have to come back
			# together: restoring only the name leaves an out-of-state order
			# NAMING the 0% template while CARRYING 8.25% rows, and
			# apply_interstate_tax repairs by comparing the name, so it sees the
			# name it wanted and leaves the wrong rows alone. On an unconfigured
			# register (setup/dev_site's, and every store's before somebody sets
			# one) the profile's blank wipes the default template instead.
			# _reassert_tax_exemption below covers the exempt case.
			set_missing_values_keeping_taxes(si)
			si.set("payments", [])
			# reference_no carries the Zelle Transaction Number; `or None` leaves it
			# unset for non-Zelle tenders (which pass an empty string) rather than "".
			si.append("payments", {"mode_of_payment": mode_of_payment, "amount": paid_amount,
				"reference_no": transaction_number or None})
			# set_missing_values above reran set_pos_fields — THE prod-confirmed
			# culprit: is_pos is now 1, so set_pos_fields (unlike its earlier
			# no-op call above, before is_pos was set) unconditionally overwrites
			# taxes_and_charges from the POS Profile default, wiping the exemption
			# and re-taxing the invoice. It also reset each line's warehouse +
			# update_stock to the POS-profile default — re-assert everything as
			# the final writes (validate()/submit() never rerun set_pos_fields, so
			# they win). Only the is_pos path resets them, so this lives here.
			_repin_economics(si, rows)
			_reassert_tax_exemption(si, tax_exempt)
			si.update_stock = 1 if ship_now else 0
			si.save()

	# Same policy dispose_order/push_shipment enforce later (see
	# _requires_full_payment_to_ship), applied to the pre-submit payment
	# intent so dispose_now can't sneak a partially-paid gun out the door.
	if (ship_now and has_regulated
			and _requires_full_payment_to_ship(is_business)
			and (not payment_received or partial_deposit)):
		frappe.throw(_(
			"Individual orders must be paid IN FULL before shipping. Collect the "
			"full amount now, or create the order (a deposit is fine) and ship it "
			"from Pending Order once the balance is recorded."))

	if has_ordered_leg:
		# Tell ffl_integrations' on_submit guard that THIS path will fire the counter
		# hook itself moments from now (below), so the guard's rebuild-or-refuse
		# branch stays reserved for invoices that bypass create_order entirely
		# (an amend, a hand-built desk invoice).
		si.flags.ffl_counter_distributor_hook_pending = True
	si.submit()
	if not destination_ffl and not ship_address:
		# A direct order with no operator-chosen ship-to is a counter pickup.
		# validate/set_missing_values re-fill shipping_address_name from the
		# customer's DEFAULT Shipping address on EVERY save, which would make
		# the pickup look shippable — the queue's has_ship_address,
		# push_shipment's precheck and build_ship_to all read this field (via
		# invoice_ship_address). Only an operator-chosen destination may
		# ship, so drop the auto-fill AFTER the final write, where nothing
		# can resurrect it. Taxes were computed at submit exactly as before
		# this feature — the clear only affects shippability.
		si.db_set("shipping_address_name", None, update_modified=False)

	if partial_deposit:
		_book_payment_entry(si, mode_of_payment, paid_amount, transaction_number)
		si.reload()

	# Counter drop-ship: build the Draft Distributor Order(s) in THIS
	# transaction, after submit + reload — a raise rolls the whole counter sale back,
	# and the DO is built with complete context and no webhook race. Fires only for a
	# drop-ship order; the hook list is empty on a POS-only install, so this no-ops.
	if has_ordered_leg:
		si.reload()
		_fire_counter_distributor_hook(
			si.name,
			_counter_distributor_context(payload=payload, rows=rows,
				destination_ffl=destination_ffl, is_business=is_business))

	# A consumed draft is done — the order it parked now exists.
	draft = (str(payload.get("draft") or "")).strip()
	if draft:
		delete_order_draft(draft)

	# Last, so a failure above rolls the emails (queued in this transaction) back too.
	# Never raises: a bad address or a mail problem is reported, the order stands.
	sent = agreements.email_sale_agreements(si, _customer_email_hint(si), unsigned) if unsigned else []
	failed = [a for a in sent if a["state"] == "failed"]

	disposed = bool(ship_now and has_regulated)
	# Dispose & Ship Now also pushes the shipment to ShipStation (owner
	# decision 2026-07-03) — best-effort: on failure the order stays queued
	# for a manual Ship, exactly like before.
	ship_pushed, ship_error = False, ""
	if disposed and destination_ffl:
		ship_pushed, ship_error = _try_push_shipment(si.name)
	return {
		"ok": True,
		"sales_order": so.name,
		"sales_invoice": si.name,
		# grand − outstanding covers both tender paths: the is_pos inline
		# payment (si.paid_amount) AND a partial deposit booked as a Payment
		# Entry (which reduces outstanding but leaves si.paid_amount at 0).
		"paid_amount": flt(si.grand_total) - flt(si.outstanding_amount),
		"outstanding": flt(si.outstanding_amount),
		"disposed": disposed,
		"ship_pushed": ship_pushed,
		"ship_error": ship_error,
		"email_to": _customer_email_hint(si),
		# Unsigned agreements: what was emailed (agreements.email_sale_agreements) and
		# whether a Dispose & Ship Now was held back for them.
		"agreements": sent,
		"agreement_email_error": "; ".join(dict.fromkeys(a["error"] for a in failed)) or None,
		"ship_held_for_agreements": ship_held_for_agreements,
	}


def _agreement_names(needs: list[dict]) -> str:
	""""Used Item Agreement, Bill of Sale (SN123)" — escaped, for a message."""
	return ", ".join(frappe.utils.escape_html(n["template"] + (f" ({n['serial_no']})" if n.get("serial_no") else ""))
		for n in needs)


# Pending Order page sections: the ONE thing a row waits
# for. Same vocabulary on every source (counter here, web in ffl_woo_sync,
# GunBroker in ffl_integrations) so the page can section the three together.
STAGE_BLOCKED, STAGE_TO_PLACE, STAGE_WAITING = "blocked", "to_place", "waiting"
STAGE_READY_SHIP, STAGE_READY_PICKUP, STAGE_DISPOSING = "ready_ship", "ready_pickup", "disposing"
STAGE_ORDER = (STAGE_BLOCKED, STAGE_TO_PLACE, STAGE_WAITING, STAGE_READY_SHIP,
	STAGE_READY_PICKUP, STAGE_DISPOSING)


def _ships(row: dict) -> bool:
	"""Does this order leave by parcel (an address or a destination FFL) rather
	than over the counter?"""
	return bool(row.get("has_ship_address")) or bool(row.get("destination_ffl"))


def blocked_reason(row: dict) -> str:
	"""Why a queue row cannot move right now — '' when nothing blocks it. ONE
	rulebook for every source (counter, web, GunBroker rows all carry these keys;
	a key a source never sets simply never fires). The first rule wins; the page
	shows it as the row's hint.

	Payment blocks a shipment, a distributor placement and a shipped-on-account
	order that only owes money; a pickup need not be paid to be ready.
	"""
	legs = row.get("legs") or {}
	trouble = legs.get("trouble") or []
	if trouble:
		t = trouble[0]
		label = {"killed": "{0} killed {1}", "short": "{0} short-shipped {1}",
			"hold": "{0} hold {1}", "error": "{0} placement failed {1}"}.get(
			t.get("kind"), "{0} {1}")
		return _(label).format(t.get("distributor", ""), t.get("detail", ""))
	if row.get("serial_errors"):
		return _("Serial rejected")
	# Only a KNOWN-expired licence blocks. A web order has no resolved ATF FFL
	# Record at listing time (Woo sends the licence number + an ATF date code
	# like "9B"; the dispose step resolves and gates it for real), so an
	# "unverified" state must not block — an earlier flag for it parked the
	# whole web queue under a false "expired" hint and was dropped. Matches the
	# card badge and the Dispose gate, which both read ffl_expired alone.
	if row.get("destination_ffl") and row.get("ffl_expired"):
		return _("FFL expired")
	if row.get("awaiting_payment"):
		return _("Awaiting payment")
	# The payment rules are the counter's: a web order is paid before it reaches
	# us and a GunBroker sale's payment gate is its own awaiting_payment flag.
	counter = (row.get("source") or "sales_invoice") == "sales_invoice"
	paid = bool(row.get("paid"))
	dos = legs.get("distributor_orders") or []
	if counter and not paid and any(d.get("status") == "Draft"
			and _leg_needs_full_payment(d.get("order_type")) for d in dos):
		return _("Unpaid — settle the balance before placing the distributor order")
	if counter and _ships(row) and not row.get("is_business") and not paid:
		return _("Unpaid — record payment before shipping")
	if counter and not paid and row.get("ship_pushed") and not row.get("needs_disposition"):
		return _("Shipped on account — record payment")
	return ""


def queue_stage(row: dict) -> str:
	"""Which section of the Pending Order page a row belongs to.
	Reads only what the row builders stamped — never raw order rows — so the
	three sources cannot drift from each other."""
	if blocked_reason(row):
		return STAGE_BLOCKED
	legs = row.get("legs") or {}
	if legs.get("releasable"):
		return STAGE_TO_PLACE
	if legs.get("awaiting_goods") or legs.get("awaiting_arrival") or legs.get("awaiting_dropship") \
			or row.get("awaiting_serial") or (row.get("to_store") and not row.get("arrived")):
		return STAGE_WAITING
	if row.get("ship_pushed"):
		return STAGE_DISPOSING
	if row.get("store_pickup"):
		return STAGE_READY_PICKUP
	return STAGE_READY_SHIP if _ships(row) else STAGE_READY_PICKUP


def overdue_limit_days() -> int:
	"""FFL Settings → pending_order_age_alert_days: the page turns a row red and
	the digest lists it past this many days. Read straight from ``tabSingles``
	(``single_raw``) for the reason on that helper: an ORM read loads the DocType
	through ``frappe.get_all`` / ``frappe.get_doc``, which the queue's unit tests
	patch. Never saved → the docfield default, 7."""
	from ffl_core.utils import single_raw
	raw = single_raw("FFL Settings", "pending_order_age_alert_days")
	return int(raw) if raw not in (None, "") else 7


def stamp_stage(row: dict, posting_date, limit_days: int, today=None) -> dict:
	"""Stamp section, reason and age onto a queue row (all three sources).
	``today`` is the site date the listing resolved once (see ``age_days``)."""
	row["stage"] = queue_stage(row)
	row["blocked_reason"] = blocked_reason(row)
	row["age_days"] = age_days(posting_date, today)
	row["overdue"] = row["age_days"] >= int(limit_days)
	return row


def digest_lines(rows: list, limit_days: int) -> list[str]:
	"""One line per order that needs a person: blocked rows (with why) and rows
	older than the limit whatever their stage. Blocked first, oldest first."""
	out = []
	ordered = sorted(rows or [], key=lambda r: (r.get("stage") != STAGE_BLOCKED,
		-int(r.get("age_days") or 0)))
	for r in ordered:
		ref = r.get("sales_invoice") or r.get("woo_order_id") or r.get("gb_order_id") or "?"
		who = r.get("customer_name") or r.get("customer") or ""
		days = int(r.get("age_days") or 0)
		if r.get("stage") == STAGE_BLOCKED:
			out.append(f"{ref} · {who} · {r.get('blocked_reason') or 'blocked'} · {days}d")
		elif days >= int(limit_days):
			out.append(f"{ref} · {who} · {r.get('stage')} for {days}d")
	return out


_DIGEST_CACHE_PREFIX = "ffl_core:pending_digest:"


# Module-level name kept: tests patch it.
from ffl_core.alerts import alert_recipients as _digest_recipients  # noqa: E402


@frappe.whitelist()
def pending_orders_digest() -> dict:
	"""Morning summary of stuck counter orders for the managers — every blocked
	row and every row older than FFL Settings → pending_order_age_alert_days.

	Scheduled daily; sent once per day (a manual re-run the same day is a no-op
	before the queue is even read); sends nothing when nothing is stuck. Counter
	rows only (web and GunBroker rows are listed on the page but paid / handled
	elsewhere).

	Returns:
	    {lines: <count>, sent: <bool>}.

	Permissions:
	    Inventory roles (the scheduler runs as Administrator, whom the gate exempts).
	"""
	from ffl_core.roles import STOCK_ROLES
	frappe.only_for(STOCK_ROLES)
	key = _DIGEST_CACHE_PREFIX + nowdate()
	if frappe.cache().get_value(key):
		return {"lines": 0, "sent": False}
	limit = overdue_limit_days()
	lines = digest_lines(list_pending_dispositions(), limit)
	if not lines:
		return {"lines": 0, "sent": False}
	recipients = _digest_recipients()
	if not recipients:
		return {"lines": len(lines), "sent": False}
	frappe.sendmail(
		recipients=recipients,
		subject=f"[POS Alert] Pending orders needing attention — {nowdate()}",
		message="These counter orders are blocked or have waited longer than "
			f"{limit} days:\n\n" + "\n".join(lines) + "\n\nOpen Pending Order to act on them.",
		now=False)
	frappe.cache().set_value(key, "1", expires_in_sec=20 * 3600)
	return {"lines": len(lines), "sent": True}


def age_days(posting_date, today=None) -> int:
	"""Whole days a row has waited since its order date (0 when unknown).

	Pure date arithmetic on the ISO date part: a value that is not a date at
	all reads as 0 (fresh), never as a crash on the queue. ``today`` is the
	site's date (``nowdate()``), which each listing resolves ONCE and passes in —
	never the host clock."""
	if not posting_date:
		return 0
	try:
		started = date.fromisoformat(str(posting_date)[:10])
	except ValueError:
		return 0
	current = date.fromisoformat(str(today or nowdate())[:10])
	return max(0, (current - started).days)


@frappe.whitelist()
def list_pending_dispositions() -> list[dict]:
	"""The Pending Order queue — every submitted counter order that still needs
	work: undisposed firearm serials (needs disposing), an outstanding balance
	(needs payment), or deferred physical stock that hasn't shipped (needs its
	ShipStation push / Delivery Note leg). FFL shipments and direct orders (no
	destination FFL — accessories shipping to the buyer, or a counter pickup)
	both live here; a fully-handled order leaves the queue. A business order
	shipped on account therefore stays until paid, and a dispose-now order
	appears (already disposed) until its shipment is pushed.

	Returns:
	    One card per order: invoice identity/customer/totals, paid state,
	    pending vs disposed serials per item, fulfillment leg state
	    (local/dropship, releasable), ship_to_preview, has_ship_address,
	    shipstation state, and ship_items (when ShipStation is enabled).

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	ss_enabled = _shipstation_enabled()
	limit_days = overdue_limit_days()
	today = nowdate()
	# Web-order revenue SIs (ffl_woo_sync revenue.py) are accounting-only:
	# their fulfillment lives on the Woo Online Order row (source="woo"), so
	# they must never surface here as bogus counter rows. An extension's
	# settlement invoices (an outbound consignment's, say) are the same shape
	# of thing and declare their Link field through
	# gunshop_pending_queue_exclusion_fields (channel_invoice).
	# GunBroker's dispositions link the GunBroker Order, not the invoice, so
	# without the exclusion every shipped GunBroker gun would sit here for ever.
	# Shared with reservations.counter_holds — see channel_invoice.
	base = channel_invoice.pending_si_base_filter()
	fields = ["name", "customer", "customer_name", "ffl_destination_ffl",
		"ffl_buyer_type", "posting_date", "creation", "grand_total",
		"outstanding_amount", "shipstation_status", "shipstation_order_id",
		"ffl_staff_note", "shipping_address_name", "update_stock"]
	# SQL prefilter — three arms, ORed across separate queries because
	# get_all can't OR an AND-group; the arms only bound the get_doc loop,
	# the retention rule below decides precisely.
	#   arm1: still owes money AND carries the create_order fingerprint (a
	#         POS Profile) — a desk-made receivable is not a counter order
	#         and must not gain this page's Cancel/Edit surface;
	#   arm2: is (or was) an FFL shipment — the queue's historical whole;
	#   arm3: a deferred order never pushed NOR stamp-cleared. A pushed or
	#         marked-shipped order always carries an order id or a status
	#         stamp, so this arm can't lose a pending direct order — and
	#         excluding the stamps keeps completed direct orders from
	#         re-entering the loop forever. Two queries because SQL's
	#         three-valued NULL makes a single `not in` drop NULL rows.
	invoices = frappe.get_all("Sales Invoice",
		filters={**base, "outstanding_amount": (">", _PAID_TOLERANCE),
			"pos_profile": ("is", "set")},
		fields=fields)
	seen = {inv.name for inv in invoices}
	arm_filters = [
		{**base, "ffl_destination_ffl": ("is", "set")},
		{**base, "update_stock": 0,
			"shipstation_order_id": ("is", "not set"),
			"shipstation_status": ("is", "not set")},
		{**base, "update_stock": 0,
			"shipstation_order_id": ("is", "not set"),
			"shipstation_status": ("in", _UNPUSHED_STATUSES)},
	]
	for arm in arm_filters:
		for inv in frappe.get_all("Sales Invoice", filters=arm, fields=fields):
			if inv.name not in seen:
				seen.add(inv.name)
				invoices.append(inv)
	invoices.sort(key=lambda r: (r.posting_date, r.creation))
	# Counter pickups' open 4473 tickets (api/counter_pickup), one query.
	pickup_map = open_pickup_drafts_for_invoices([inv.name for inv in invoices])
	out = []
	for inv in invoices:
		si = frappe.get_doc("Sales Invoice", inv.name)
		# The membership rule, not just a bound: arm3's shape (deferred +
		# unstamped) is also the default shape of a desk-made invoice, so the
		# counter-order fingerprint decides here — the same predicate the
		# Cancel/Edit/Mark surfaces enforce.
		if not _is_counter_order(si):
			continue
		# One pass resolves pending, the disposed set AND per-row serials —
		# the v16 Bundle path costs a get_doc per serial resolution, so it
		# must not run twice per row (pending + ship_items).
		pending, disposed_serials, row_serials = si_firearm_serial_state(si)
		outstanding = flt(inv.outstanding_amount)
		# A counter pickup with a regulated gun: no Dispose (the buyer collects
		# here with a 4473 on the pickup ticket), a Pickup button instead — the
		# same shape as a web order whose FFL is our own licence.
		store_pickup = is_pickup_order(si) and any(
			is_regulated_firearm_item(row.item_code) for row in si.items)
		needs_disposition = bool(pending) and not store_pickup
		paid = _is_paid(outstanding)
		# Disposed = at least one booked transfer and no serial left to book —
		# distinct from "has no firearm serials at all".
		disposed = not pending and bool(disposed_serials)
		ship_pushed = shipstation_pushed(
			inv.shipstation_order_id, inv.shipstation_status)
		undelivered = _undelivered_stock_lines(si)
		# P1: the order's distributor legs. An order with
		# a live (non-terminal) DO must NOT leave the queue — pre-P1, a settled
		# pure drop-ship order vanished here while its DO sat in Draft unconfirmed.
		dropship_legs = frappe.get_all("Distributor Order",
			filters={"source_doctype": "Sales Invoice", "source_name": inv.name},
			fields=["name", "distributor", "status", "order_type", "receive_status", "web_ref"])
		# A ship-to-store leg is not done when the distributor ships it — only when
		# it no longer owes this order goods: every unit received here or covered
		# from shelf stock (Fulfill from stock / Re-source raise substituted_qty, and
		# that parcel then books as plain restock, never against the DO).
		# Read once: the owing set, the substitutable rows and awaiting_goods all
		# derive from the same ship-to-store lines.
		to_store = _to_store_line_rows(inv.name) if any(
			d.order_type == "Procure To Store" for d in dropship_legs) else {}
		owing = set(_unreceived_to_store_orders(inv.name, to_store))
		open_legs = [d for d in dropship_legs if d.status != "Cancelled" and (
			d.name in owing if d.order_type == "Procure To Store"
			else d.status != "Complete")]
		awaiting_arrival = [row for row in si.items if is_awaiting_procurement_row(si, row)]
		# An antique's stock never auto-issues (no disposition, no DN) — it
		# still holds the row until the Mark step, where the operator gets
		# the manual-issue warning. Without this, a paid antique order
		# vanishes silently and the warning never fires.
		undelivered_antique = [row for row in _classify_deferred_lines(si)[2]
			if _so_line_undelivered(row)]
		# Two ship arms: firearms wait for dispose, then their ShipStation
		# push (only while the integration is on — its stock leg is the
		# disposition's Material Issue either way); plain stock lines wait
		# for their Delivery Note leg (push or Mark Shipped), integration
		# or not, because without it stock and COGS never book.
		needs_ship = (ss_enabled and disposed and not ship_pushed) \
			or (bool(undelivered or undelivered_antique) and not ship_pushed)
		# A pickup gun that has arrived but not been handed over is work too.
		pickup_open = store_pickup and bool(pending)
		# Drop only when there's nothing left to do — a live distributor leg
		# counts as something to do (release, or watch it to Complete).
		if not needs_disposition and paid and not needs_ship and not open_legs \
				and not awaiting_arrival and not pickup_open:
			continue
		dest = (inv.ffl_destination_ffl or "").strip()
		ffl = ffl_premise(dest) if dest else frappe._dict()
		# A direct order has no license to expire — only a real destination
		# FFL can gate the buttons on expiry.
		expired = bool(dest) and (not ffl.get("expiry_date")
			or getdate(ffl.expiry_date) < getdate(nowdate()))
		row = {
			"source": "sales_invoice",
			"sales_invoice": inv.name,
			"customer": inv.customer,
			"customer_name": inv.customer_name,
			"is_business": inv.ffl_buyer_type == _BUYER_BUSINESS,
			"destination_ffl": dest or None,
			"destination_ffl_name": ffl.get("licensee_name"),
			"ffl_expiry_date": ffl.get("expiry_date"),
			"ffl_expired": expired,
			"posting_date": inv.posting_date,
			"grand_total": inv.grand_total,
			"outstanding_amount": outstanding,
			"note": (inv.ffl_staff_note or "").strip(),
			"paid": paid,
			"needs_disposition": needs_disposition,
			# The agreements the gun waits for (never a blocked stage: Dispose may
			# override them). Only while there is a gun to dispose.
			"agreements": _agreement_rows(si) if needs_disposition else [],
			"pending_serials": pending,
			"store_pickup": store_pickup,
			"pickup_invoice": pickup_map.get(inv.name) if store_pickup else None,
			# Why Pickup cannot be pressed yet (None when it can, or when it is
			# not offered) — the card's hint under the button.
			"pickup_block": (pickup_block(si, pending)
				if store_pickup and not pickup_map.get(inv.name) else None),
			# The disposed serials (already on the bound book) so the card can
			# still show a disposed order's guns — greyed — once pending empties.
			"disposed_serials": [{"serial": s} for s in sorted(disposed_serials)],
			"disposed": disposed,
			# ANY booked disposition (even a partial one) makes the order
			# non-editable server-side — the Edit button gates on this.
			"has_disposition": bool(disposed_serials),
			# THE server refusal rule (mark_shipped_block), not a mirror of
			# it — payment excluded, because ship_payment_block is the rule
			# for that and both ship actions ask it; the page adds it on top,
			# so an unpaid individual's row offers no ship action until the
			# balance is recorded. True on an undisposed row: Mark Shipped
			# clears the shipping half and leaves the row here for its
			# Dispose, so a paid row always has a clear-the-queue action even
			# with ShipStation off.
			"can_mark_shipped": mark_shipped_block(
				si, pending, disposed_serials) is None,
			"ship_pushed": ship_pushed,
			"shipstation_enabled": ss_enabled,
			"shipstation_status": inv.shipstation_status or "Not Requested",
			"shipstation_order_id": inv.shipstation_order_id or "",
			# Cancel-dialog payload.
			"paid_total": flt(inv.grand_total) - outstanding,
			"last_payment_mode": _last_payment_mode(inv.name),
			"sales_orders": _linked_sales_orders(si),
			# P1: per-leg status for the queue card.
			"legs": {
				"local": {"undelivered": len(undelivered),
					"pending_serials": len(pending)},
				# Ship-to-store firearm lines whose gun has not arrived yet (no
				# serial to Dispose until the Distributor Order is received).
				"awaiting_arrival": len(awaiting_arrival),
				# Any unreceived ship-to-store order — guns or accessories. The Ship
				# button reads this (push_shipment refuses the same way).
				# Same answer as ship_arrival_block(si), from the lines read above.
				"awaiting_goods": bool(owing),
				"distributor_orders": dropship_legs,
				# A Draft leg the counter may place now: paid, or a ship-to-store
				# leg (goods come to us — placeable on a deposit; see
				# _leg_needs_full_payment).
				"releasable": any(
					d.status == "Draft" and (paid or not _leg_needs_full_payment(d.order_type))
					for d in dropship_legs),
				# A drop-ship leg the house is still working on (placed / acked /
				# held / partly shipped): nothing to do here but watch it.
				"awaiting_dropship": any(
					d.status not in ("Complete", "Cancelled", "Draft", "Queued", "Error")
					and d.order_type != "Procure To Store" for d in dropship_legs),
				# Ship-to-store lines not arrived yet that the shelf could serve
				# instead (Fulfill from stock); the parcel then lands as restock.
				"substitutable": substitutable_rows(si, to_store),
				# Legs a person must act on: killed / short-shipped / held too long.
				"trouble": trouble_legs(dropship_legs),
			},
		}
		# Ship-to preview: the FFL premise, else the invoice's shipping
		# Address — the same fallback order as shipstation.api.build_ship_to,
		# so the card previews exactly what the label would say. None when
		# neither exists (a counter pickup).
		preview = None
		if dest:
			preview = {
				"company": ffl.get("company"),
				"line1": ffl.get("street"),
				"line2": ffl.get("street2"),
				"city": ffl.get("city"),
				"state": ffl.get("state"),
				"zip": ffl.get("zip"),
				"phone": ffl.get("phone"),
			}
		else:
			addr = invoice_ship_address(inv)
			if addr:
				preview = {"company": None, **addr}
		row["ship_to_preview"] = preview
		row["has_ship_address"] = bool(preview)
		row["agreements_missing"] = [{"template": a["template"], "serial_no": a["serial_no"]}
			for a in row["agreements"] if not a["signed"]]
		# Page section + why it is stuck + how long it has waited.
		stamp_stage(row, inv.posting_date, limit_days, today)
		if ss_enabled:
			# Ship-dialog payload — only built when the Ship button can
			# render. ship_items carries ALL serials (pending_serials empties
			# once disposed, the dialog must not).
			row["ship_items"] = [
				{
					"item_code": item_row.item_code,
					"item_name": item_row.item_name,
					"qty": item_row.qty,
					"serials": serials,
				}
				for item_row, serials in row_serials
			]
		out.append(row)
	# One sweep for the whole queue (see _attach_shelf_stock) rather than one per
	# card: ten cards waiting on the same model share a single serial count.
	_attach_shelf_stock([row["legs"]["substitutable"] for row in out])
	return out


def _book_payment_entry(si, mode_of_payment: str, amount: float,
		transaction_number: str | None = None, posting_date: str | None = None,
		*, remarks: str | None = None) -> str:
	"""Book a submitted Payment Entry of `amount` against a submitted Sales
	Invoice — THE mechanism for every non-is_pos collection: the Pending Order
	page's Record Payment AND create_order's partial deposit, so the two can
	never drift.

	posting_date pins the entry's date (None → get_payment_entry's default, today);
	callers re-collecting against an amended invoice pass the SI's own date so the
	receipt stays in the sale's accounting period, and Record Payment passes the
	day the money actually arrived (record_payment's received_on).

	remarks, when given, replaces the sentence ERPNext writes onto the entry —
	and takes `custom_remarks` with it, because `PaymentEntry.set_remarks()`
	overwrites the field on every validate unless that flag is set. A marketplace
	receipt needs this: its posting date is the INVOICE's date by design, so the
	day the money actually reached the bank has nowhere else to live and the
	deposit becomes untraceable on a bank statement. Every existing caller passes
	nothing and keeps ERPNext's own wording, byte for byte."""
	account = _mode_of_payment_account(mode_of_payment, si.company)
	if not account:
		frappe.throw(_(
			"No {0} account is configured for company {1}. Add one on the Mode of "
			"Payment before recording this payment."
		).format(mode_of_payment, si.company))

	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	# Pass the mode's account as bank_account so get_payment_entry sets paid_to,
	# its currency AND its account_type consistently from the start (and never
	# crashes hunting a company default that may not exist). party_amount carries
	# partial collection — validate refreshes the reference row to the real total.
	pe = get_payment_entry("Sales Invoice", si.name, party_amount=amount,
		bank_account=account)
	pe.mode_of_payment = mode_of_payment
	if posting_date:
		pe.posting_date = posting_date
		pe.set_posting_time = 1
	# A Zelle transfer carries its own app/bank confirmation number — record it so
	# the deposit reconciles against this sale. Other Bank-type tenders fall back
	# to the invoice name; a cash/drawer account needs no reference at all.
	# The reference is dated with the ENTRY, not with the moment it was typed:
	# ERPNext's set_remarks stamps "Transaction reference no X dated
	# <reference_date>" into the entry, so a pinned posting_date left with
	# today's reference_date would state two different dates for one deposit.
	reference_date = posting_date or nowdate()
	if transaction_number:
		# ANY manually-settled tender that carried a reference keeps it — not
		# just Zelle. A card approval code the operator was REQUIRED to type
		# used to fall into the branch below and be overwritten with the
		# invoice name, so the control existed and produced nothing: every card
		# Payment Entry in production carries an invoice number where its
		# approval code should be.
		pe.reference_no = transaction_number
		pe.reference_date = reference_date
	elif pe.paid_to_account_type == "Bank":
		# Nothing was given (cash-like Bank tenders) — the invoice name still
		# beats an empty field for tracing the deposit.
		pe.reference_no = si.name
		pe.reference_date = reference_date
	if remarks:
		pe.remarks = remarks
		# Without this, set_remarks() rewrites the field during validate and the
		# caller's sentence never reaches the ledger.
		pe.custom_remarks = 1
	pe.flags.ignore_permissions = True
	pe.insert()
	pe.submit()
	return pe.name


def _mode_of_payment_account(mode_of_payment: str, company: str) -> str | None:
	"""The GL account a Mode of Payment posts to for a company (asset Bank/Cash),
	from its Mode of Payment Account child rows."""
	rows = frappe.get_all(
		"Mode of Payment Account",
		filters={"parent": mode_of_payment, "company": company},
		fields=["default_account"], limit=1)
	return rows[0].default_account if rows else None


def _received_on_posting_date(received_on, si) -> str | None:
	"""The "Payment received on" date, normalised for _book_payment_entry.

	Blank → None: the entry books today, byte-for-byte the behaviour every
	same-day collection had before this parameter existed. A date is honoured
	only inside the window the money could actually have arrived in, and both
	bounds REFUSE rather than clamp — silently moving an operator's date is how
	a deposit ends up in a month nobody meant:
	  * later than today — money cannot arrive in the future;
	  * earlier than the invoice — the receipt would post outside the period
	    of the sale it settles (and could land in a closed month).
	"""
	raw = received_on.strip() if isinstance(received_on, str) else received_on
	if raw in (None, ""):
		return None
	try:
		received = getdate(raw)
	except Exception:  # noqa: BLE001 — getdate throws its own wording; ours is the operator's
		received = None
	if not received:
		frappe.throw(_(
			"Could not read the payment date {0}. Enter it as YYYY-MM-DD, or pick "
			"it from the calendar."
		).format(frappe.utils.escape_html(str(raw))))
	today = getdate(nowdate())
	invoice_date = getdate(si.posting_date)
	window = _("Enter a date between {0} (the invoice date) and {1} (today).").format(
		frappe.format_value(invoice_date, {"fieldtype": "Date"}),
		frappe.format_value(today, {"fieldtype": "Date"}))
	if received > today:
		frappe.throw(_("A payment cannot be received in the future. {0}").format(window))
	if received < invoice_date:
		frappe.throw(_(
			"{0} is before invoice {1} was dated. {2}"
		).format(frappe.format_value(received, {"fieldtype": "Date"}),
			si.name, window))
	return str(received)


def _card_reference_required_for(mode_of_payment: str, *, off_terminal: bool = False) -> bool:
	"""True when this mode is the card tender AND its approval code must be typed:
	the store is on a standalone terminal with references required, or — for a
	payment booked off the terminal (off_terminal) — the card processor's terminal is live,
	so this card was run on the Virtual Terminal and its code is the only link to
	that batch (the terminal's own booking always passes the gateway reference).

	Imported lazily and failing OPEN: ffl_core must run without ffl_integrations
	installed, and a missing integration app is not a reason to block a cashier
	from recording money that was genuinely collected.
	"""
	try:
		from ffl_integrations.integration_gate import card_processor_ready, card_reference_required
		from ffl_integrations.payments.setup import MODE_OF_PAYMENT_NAME
	except ImportError:
		return False
	if mode_of_payment != MODE_OF_PAYMENT_NAME:
		return False
	if card_reference_required():
		return True
	if not off_terminal:
		return False
	try:
		return card_processor_ready(require_device=True)
	except Exception:
		# An unreadable processor config (e.g. a password that no longer decrypts)
		# must not stop a cashier recording money — fail open, like the import.
		return False


# Zelle and ACH are the same shape of tender — a manual bank transfer whose only
# machine record lives in the OTHER system — so they carry the same reference
# requirement. A check or money order joins them: its number is the only thing
# tying the deposit slip back to this sale. Listing them together keeps "we
# added ACH to the mode list but not to the gate" from happening again.
_REFERENCED_BANK_MODES = (ZELLE, "ACH", CHECK_MONEY_ORDER)


def _reference_name(mode_of_payment: str) -> str:
	"""What a referenced bank mode asks the operator to type."""
	if mode_of_payment == CHECK_MONEY_ORDER:
		return _("check or money order number")
	return _("{0} Transaction Number").format(mode_of_payment)


def _reject_pan_reference(reference: str) -> None:
	"""No reference field in this module may hold a card NUMBER — that would put
	cardholder data into accounting vouchers. Same test the invoice-row gate
	runs (ffl_core.compliance.payment_mode.looks_like_pan)."""
	from ffl_core.compliance.payment_mode import looks_like_pan
	if looks_like_pan(reference):
		frappe.throw(_(
			"That reference looks like a full card number. Record the approval / "
			"confirmation code — never the card number."))


def _validated_refund(refund_mode, refund_reference) -> tuple[str, str]:
	"""The refund tender gate shared by cancel_order and update_order: a valid
	mode, a reference for the bank tenders, a terminal approval code for a card
	refund when one is required, and never a card number. Returns the stripped
	(refund_mode, refund_reference)."""
	refund_mode = (str(refund_mode or "")).strip()
	if refund_mode not in _MODES_OF_PAYMENT:
		frappe.throw(_("Pick a valid refund method."))
	refund_reference = (str(refund_reference or "")).strip()
	if refund_mode in _REFERENCED_BANK_MODES and not refund_reference:
		frappe.throw(_("Enter the {0} for the refund.").format(
			_reference_name(refund_mode)))
	# Cards refunded on the standalone terminal need their approval code
	# here too. THIS is the path the store actually refunds through — it
	# has never raised a POS return document — so a control that lived only
	# on returns was a control over nothing.
	if not refund_reference and _card_reference_required_for(refund_mode):
		frappe.throw(_(
			"Enter the approval code from the terminal for the {0} refund. The "
			"card is refunded on a standalone terminal, so this is the only "
			"record that the money actually went back."
		).format(refund_mode))
	_reject_pan_reference(refund_reference)
	return refund_mode, refund_reference


def _cancel_submitted_sales_orders(sales_orders) -> list[str]:
	"""Cancel each still-submitted linked Sales Order; return the names actually
	cancelled (one already cancelled or in draft is skipped)."""
	cancelled = []
	for so_name in sales_orders:
		if frappe.db.get_value("Sales Order", so_name, "docstatus") == 1:
			so = frappe.get_doc("Sales Order", so_name)
			so.flags.ignore_permissions = True
			so.cancel()
			cancelled.append(so_name)
	return cancelled


@frappe.whitelist()
def record_payment(sales_invoice: str, mode_of_payment: str, amount=None,
		transaction_number=None, received_on=None) -> dict:
	"""Record a later payment against a submitted, not-fully-paid Sales Invoice —
	the Pending Order "Record Payment" action for when a customer pays after the
	order was created on account. Builds and submits a Payment Entry for the full
	outstanding (or a smaller partial `amount`), booked to the Mode of Payment's
	account, and returns the updated balance.

	Args:
	    sales_invoice: Submitted, not-fully-paid Sales Invoice name.
	    mode_of_payment: Cash, Credit Card, Zelle, ACH or Check/Money
	        Order; Zelle, ACH and Check/Money Order also require
	        transaction_number.
	    amount: Optional partial amount (> 0, at most the outstanding
	        balance); blank pays the full outstanding.
	    transaction_number: Processor reference — the check or money order
	        number for Check/Money Order. Required for Zelle, ACH and
	        Check/Money Order, and for the card tender on a store running the
	        standalone terminal with approval codes required — or with the
	        card processor's terminal live (a card typed in here was run on the
	        Virtual Terminal).
	    received_on: The day the money arrived (blank → today): it dates
	        the Payment Entry, so a collection typed in late still books in
	        the period the deposit really landed in. See
	        _received_on_posting_date for the window.

	Returns:
	    {ok, payment_entry, paid_amount, outstanding, paid}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	mode_of_payment = (str(mode_of_payment or "")).strip()
	if mode_of_payment not in _MODES_OF_PAYMENT:
		frappe.throw(_("Pick a valid payment method."))
	transaction_number = (str(transaction_number or "")).strip()
	if mode_of_payment in _REFERENCED_BANK_MODES and not transaction_number:
		frappe.throw(_("Enter the {0} to record a {1} payment.").format(
			_reference_name(mode_of_payment), mode_of_payment))
	# The THIRD way card money enters the system: deferred collection on an
	# on-account order, booked as a Payment Entry rather than an invoice payment
	# row — so neither the POS gate nor the Sales Invoice one sees it. On a store
	# swiping cards on a standalone terminal that left the approval code optional
	# here while it was mandatory everywhere else, which is how "every card row
	# carries a reference" could be asserted and still be false (a review found two
	# such payments in production carrying invoice numbers instead).
	if not transaction_number and _card_reference_required_for(mode_of_payment, off_terminal=True):
		frappe.throw(_(
			"Enter the approval code from the terminal or Virtual Terminal receipt "
			"to record a {0} payment — it is the only record tying the payment to "
			"the processor's batch."
		).format(mode_of_payment))
	_reject_pan_reference(transaction_number)
	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if si.docstatus != 1:
		frappe.throw(_("{0} is not a submitted invoice.").format(sales_invoice))
	outstanding = flt(si.outstanding_amount)
	if _is_paid(outstanding):
		frappe.throw(_("{0} is already paid.").format(sales_invoice))
	pay = flt(amount) if amount not in (None, "") else outstanding
	if pay <= 0:
		frappe.throw(_("Payment amount must be greater than zero."))
	if pay > outstanding + 0.01:
		frappe.throw(_(
			"Payment {0} exceeds the outstanding balance {1}."
		).format(frappe.format_value(pay, {"fieldtype": "Currency"}),
			frappe.format_value(outstanding, {"fieldtype": "Currency"})))
	# Validated LAST, after every pre-existing gate, so this feature cannot
	# reorder the errors an operator already knows ("already paid" and "exceeds
	# the balance" still answer first). It only needs `si` for the lower bound.
	posting_date = _received_on_posting_date(received_on, si)

	pe_name = _book_payment_entry(si, mode_of_payment, pay, transaction_number,
		posting_date=posting_date)

	si.reload()
	new_outstanding = flt(si.outstanding_amount)
	return {
		"ok": True,
		"payment_entry": pe_name,
		"paid_amount": pay,
		"outstanding": new_outstanding,
		"paid": _is_paid(new_outstanding),
	}


@frappe.whitelist()
def release_order_legs(sales_invoice: str) -> dict:
	"""Release the Draft distributor legs of a counter order.

	P1 — one-click release: confirms every Draft
	Distributor Order sourced from this invoice. The payment rule is the
	leg's (ffl_integrations.distributor.api.placement_payment_block, applied
	by confirm_order): a drop-ship leg needs the invoice settled, a
	ship-to-store leg may go out on a deposit or unpaid — a leg the rule
	refuses lands in ``failed`` with its reason and stays Draft. Each leg is
	savepoint-isolated (a failing leg rolls back only its own half-writes,
	never the siblings or the caller's transaction); confirm/place are
	idempotent, so a failed leg can be retried. Local/firearm legs keep
	their own queue buttons.

	Args:
	    sales_invoice: Counter-order Sales Invoice name.

	Returns:
	    {"released": [DO names], "failed": [{name, error}]}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	from ffl_integrations.distributor import api as dist_api

	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if not _is_counter_order(si):
		frappe.throw(_("{0} is not a counter order.").format(sales_invoice))
	released, failed = [], []
	draft_dos = frappe.get_all("Distributor Order",
		filters={"source_doctype": "Sales Invoice",
			"source_name": sales_invoice, "status": "Draft"},
		pluck="name")
	for idx, do_name in enumerate(draft_dos):
		savepoint = f"release_leg_{idx}"
		frappe.db.savepoint(savepoint)
		try:
			dist_api._confirm_order(do_name)
			released.append(do_name)
		except Exception as exc:  # noqa: BLE001 — isolate each leg; a failed leg stays in Draft
			frappe.db.rollback(save_point=savepoint)
			failed.append({"name": do_name, "error": str(exc)})
	return {"released": released, "failed": failed}


def assign_procured_serials(sales_invoice: str, procured_by_item: dict,
		distributor: str | None = None) -> dict:
	"""Hang firearm serials that just arrived on a ship-to-store Distributor Order
	onto the counter order that is waiting for them.

	Writing the serial onto the awaiting line IS the hold: an undisposed serial on
	a submitted counter invoice is a counter hold (``reservations.counter_holds``),
	so the gun can't be sold again, and it shows up on the Pending Order card as a
	serial to Dispose. Only lines still awaiting procurement are filled, one serial
	each, matched by item code and (when given) the ordering distributor — so a
	re-receive writes nothing (idempotent) and a serial is never moved off a line
	that already has one. A serial that is not an in-stock Serial No of that item is
	never hung: holding a number that isn't the gun on the shelf blocks nothing and
	fails at Dispose.

	Called by ``ffl_integrations.distributor.receive_bridge`` after the Purchase
	Receipt committed; it never raises for a dead order (a cancelled invoice gets
	nothing hung — the receiver is told instead).

	Returns ``{"order", "assigned": [serial, ...], "skipped"?}``."""
	if not frappe.db.exists("Sales Invoice", sales_invoice):
		return {"order": sales_invoice, "assigned": [], "skipped": "missing"}
	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if si.docstatus != 1:
		return {"order": sales_invoice, "assigned": [], "skipped": "not_submitted"}
	pools = {code: list(serials or []) for code, serials in (procured_by_item or {}).items()}
	assigned = []
	for code in pools:
		pools[code] = [sn for sn in pools[code] if frappe.db.get_value(
			"Serial No", {"name": sn, "item_code": code, "status": "Active"}, "name")]
	for row in si.items:
		pool = pools.get(row.item_code)
		if not pool or not is_awaiting_procurement_row(si, row):
			continue
		if distributor and row.ffl_to_store_distributor != distributor:
			continue
		serial = pool.pop(0)
		warehouse = frappe.db.get_value("Serial No", serial, "warehouse") or row.warehouse
		frappe.db.set_value("Sales Invoice Item", row.name, {
			"serial_no": serial, "use_serial_batch_fields": 1, "warehouse": warehouse,
		}, update_modified=False)
		assigned.append(serial)
	return {"order": sales_invoice, "assigned": assigned}


@frappe.whitelist()
def dispose_order(sales_invoice: str, override_agreement: int = 0, reason: str | None = None) -> dict:
	"""Dispose (ship) the firearms on a submitted order — the queue action.

	Books one transfer disposition per not-yet-disposed serial; the
	disposition's own on_submit issues the stock-out Material Issue and
	enqueues the FastBound push. Idempotent. Gated on the pre-ship payment
	rule (ship_payment_block), a valid destination FFL and the agreements the
	sale needs: an unsigned one refuses (``AgreementRequired``) unless
	``override_agreement`` is 1, which disposes anyway and records who, what
	was unsigned and why as a comment on the invoice. The signed Each firearm
	sold copies then belong to this invoice.

	Args:
	    sales_invoice: Submitted Sales Invoice with a destination FFL.
	    override_agreement: 1 to dispose although agreements are unsigned.
	    reason: Why, for the override's comment (optional).

	Returns:
	    {"ok": True, "dispositions": [names], "disposed_count": n,
	    "agreement_override": whether unsigned agreements were overridden}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if si.docstatus != 1:
		frappe.throw(_("{0} is not a submitted invoice.").format(sales_invoice))

	# Shipping gate: shared with push_shipment via ship_payment_block.
	block = ship_payment_block(si)
	if block:
		frappe.throw(block)

	if is_pickup_order(si):
		# Booking a transfer to ourselves would put a disposition that never
		# happened into the bound book; the gun leaves on its pickup ticket.
		frappe.throw(_(
			"{0} is a counter pickup — the buyer collects here with a 4473. Use "
			"Pickup on the Pending Order page.").format(sales_invoice))
	dest = (getattr(si, "ffl_destination_ffl", None) or "").strip()
	if not dest:
		frappe.throw(_("{0} has no destination FFL — nothing to dispose.").format(sales_invoice))
	_require_valid_ffl(dest)

	target = dealer_transfer._resolve_transfer_target(si)
	if not target:
		frappe.throw(_("Could not resolve the destination FFL for {0}.").format(sales_invoice))
	destination, disposition_type = target
	# Only while a gun is still to go (a re-run on a disposed order stays a no-op).
	unsigned = agreements.missing_requirements(si) if si_pending_firearm_serials(si) else []
	if unsigned and not cint(override_agreement):
		frappe.throw(_(
			"{0} has not signed {1}. Send the link again from the order's Agreements, or "
			"dispose without it (recorded on the order).").format(
			frappe.bold(frappe.utils.escape_html(si.customer_name or si.customer)), _agreement_names(unsigned)),
			agreements.AgreementRequired, title=_("Agreement not signed"))
	names = dealer_transfer._register_dispositions(
		si, destination=destination, disposition_type=disposition_type,
		link_field="sales_invoice")
	if unsigned and names:
		reason = (str(reason or "")).strip()
		si.add_comment("Comment", _("Disposed by {0} without the signed agreement: {1}. Reason: {2}").format(
			frappe.utils.escape_html(frappe.session.user), _agreement_names(unsigned),
			frappe.utils.escape_html(reason) if reason else _("none given")))
	# At submit these were unsigned, so nothing was bound then.
	agreements.mark_firearm_agreements_used(si)
	return {"ok": True, "dispositions": names, "disposed_count": len(names),
		"agreement_override": bool(unsigned and names)}


def _agreement_rows(si) -> list[dict]:
	"""Each agreement *si*'s sale needs and where the customer stands with it."""
	out = []
	for n in agreements.sale_requirements(si):
		st = agreements.agreement_status(si.customer, n["template"], serial_no=n["serial_no"], sale=si.name)
		link = frappe.db.get_value("Customer Agreement", st["agreement"], ["token_expires", "delivery_channel"],
			as_dict=True) if st["state"] == "pending" else None
		out.append({"template": n["template"], "serial_no": n["serial_no"], "item_code": n["item_code"],
			"state": st["state"], "signed": st["state"] in ("signed", "expiring"),
			"token_expires": str(link.token_expires) if link and link.token_expires else None,
			"link_open": bool(link and link.token_expires and get_datetime(link.token_expires) > now_datetime()),
			"sent_by": link.delivery_channel if link else None})
	return out


@frappe.whitelist()
def order_agreements(sales_invoice: str) -> dict:
	"""The agreements a counter order's sale needs and whether the buyer signed —
	the Pending Order card's Agreements strip.

	Args:
	    sales_invoice: Submitted counter-order Sales Invoice.

	Returns:
	    {"email": where a link goes, "rows": [{template, serial_no, item_code,
	    state (signed / expiring / expired / pending / missing), signed,
	    token_expires, link_open, sent_by}]}.
	"""
	frappe.only_for(_ROLES)
	si = frappe.get_doc("Sales Invoice", sales_invoice)
	return {"email": _customer_email_hint(si), "rows": _agreement_rows(si)}


@frappe.whitelist(methods=["POST"])
def resend_order_agreement(sales_invoice: str, template: str, serial_no: str | None = None,
		email: str | None = None) -> dict:
	"""Email a counter order's buyer a fresh signing link (the old one stops working).

	Args:
	    sales_invoice: Submitted counter-order Sales Invoice.
	    template: An agreement this order's sale needs.
	    serial_no: The gun, for an Each firearm sold agreement (the link covers every
	        gun of the order still unsigned for it, unless it is signed once per gun).
	    email: Send here instead of the order's email (else the customer's).

	Returns:
	    {"email", "token_expires"}.
	"""
	frappe.only_for(_ROLES)
	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if si.docstatus != 1:
		frappe.throw(_("{0} is not a submitted invoice.").format(sales_invoice))
	serial_no = (serial_no or "").strip() or None
	need = next((n for n in agreements.sale_requirements(si)
		if n["template"] == template and n["serial_no"] == serial_no), None)
	if not need:
		frappe.throw(_("{0} does not need {1}.").format(sales_invoice, frappe.utils.escape_html(template)))
	to = (str(email or "")).strip() or _customer_email_hint(si)
	if not to:
		frappe.throw(_("This order has no email address. Type one in."), title=_("No email address"))
	if email:
		frappe.utils.validate_email_address(to, throw=True)
	# An Each firearm sold one: one copy for every gun of this order still unsigned.
	needs = [n for n in agreements.missing_requirements(si) if n["template"] == template] \
		if serial_no and not frappe.db.get_value("Agreement Template", template, "one_copy_per_gun") else []
	res = agreements.email_sale_agreements(si, to, needs if need in needs else [need], replace=1)[0]
	if res["state"] == "failed":
		frappe.throw(res["error"], title=_("Not sent"))
	return {"email": res["email"], "token_expires": res["token_expires"]}


def _classify_deferred_lines(si) -> tuple[list, list, list]:
	"""One pass over a deferred invoice's lines, ONE eligibility rule shared
	by the Delivery Note leg and the operator warnings — they must stay
	exactly complementary, or a line could neither deliver nor warn and
	become a silent stock hole:
	  deliverable — non-firearm stock line billed off a Sales Order line;
	  legacy      — same shape but no Sales Order link (pre-SO invoices):
	                only a hand-made Delivery Note can issue it;
	  antique     — a firearm that isn't regulated: no FFL Disposition will
	                ever issue its stock, and its serial keeps it out of the
	                DN leg, so the operator has to move it by hand.
	A ship-now (update_stock=1) invoice classifies nothing — its own submit
	moved the stock."""
	deliverable, legacy, antique = [], [], []
	if int(getattr(si, "update_stock", 0) or 0):
		return deliverable, legacy, antique
	for row in si.items:
		# Bare delivered_by_supplier on purpose: this is parcel/stock
		# semantics — the same flag ERPNext's own SO→DN mapping keys on —
		# NOT the stricter compliance pairing is_distributor_dropship_row
		# (compliance/firearm.py) enforces for FDS routing. A drop-ship
		# line is never our parcel regardless of which distributor.
		if int(getattr(row, "delivered_by_supplier", 0) or 0):
			continue
		if not int(frappe.get_cached_value(
				"Item", row.item_code, "is_stock_item") or 0):
			continue
		if is_firearm_item(row.item_code):
			if not is_regulated_firearm_item(row.item_code):
				antique.append(row)
			continue
		if getattr(row, "so_detail", None) and getattr(row, "sales_order", None):
			deliverable.append(row)
		else:
			legacy.append(row)
	return deliverable, legacy, antique


def _so_line_undelivered(row) -> bool:
	"""Whether this invoice line's Sales Order line still owes delivery —
	the ONE remaining-qty rule for the DN leg and the queue's ship arms."""
	if not getattr(row, "so_detail", None):
		return False
	so_line = frappe.db.get_value(
		"Sales Order Item", row.so_detail,
		["qty", "delivered_qty", "docstatus"], as_dict=True)
	return bool(so_line) and so_line.docstatus == 1 \
		and flt(so_line.delivered_qty) + 1e-6 < flt(so_line.qty)


def _undelivered_stock_lines(si) -> list:
	"""The deliverable lines (see _classify_deferred_lines) whose Sales Order
	line isn't fully delivered yet — what the Delivery Note leg still owes."""
	return [row for row in _classify_deferred_lines(si)[0]
		if _so_line_undelivered(row)]


def deliver_pending_stock_lines(si) -> list[str]:
	"""Create + submit Delivery Notes for the invoice's undelivered non-firearm
	stock lines — the stock/COGS leg of shipping a deferred (update_stock=0)
	counter order, shared by push_shipment AND mark_shipped_manually so the
	two clear-the-queue paths can't diverge. Firearms are excluded (their
	stock-out is the disposition's Material Issue — a Delivery Note for them
	would double-decrement). One Delivery Note per linked Sales Order; each
	DN quantity is clamped to the invoice line's own qty, so a partially
	billed Sales Order can never over-deliver off this invoice. Idempotent:
	a delivered line stops matching. A stock shortfall raises — shipping
	goods the system doesn't have must fail loudly, not book a phantom
	delivery. The savepoint keeps a multi-SO failure all-or-nothing even if
	a caller swallows the exception."""
	lines = _undelivered_stock_lines(si)
	if not lines:
		return []
	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
	si_qty = {row.so_detail: flt(row.qty) for row in lines}
	created = []
	savepoint = "deliver_pending_stock_lines"
	frappe.db.savepoint(savepoint)
	try:
		for so_name in sorted({row.sales_order for row in lines}):
			dn = make_delivery_note(so_name)
			kept = []
			for d in dn.items:
				if d.so_detail not in si_qty:
					continue
				# make_delivery_note maps the SO line's REMAINING qty; cap
				# it at what this invoice billed.
				d.qty = min(flt(d.qty), si_qty[d.so_detail])
				if d.qty > 0:
					kept.append(d)
			if not kept:
				continue
			dn.set("items", kept)
			dn.flags.ignore_permissions = True
			dn.insert()
			dn.submit()
			created.append(dn.name)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	return created


def warn_unresolved_stock_lines(si) -> None:
	"""msgprint (never throw) when a deferred invoice carries stock the
	Delivery Note leg cannot issue — a non-firearm line with no Sales Order
	link (pre-SO legacy shapes), or an antique firearm (no disposition, no
	DN). Shared by both clear-the-queue paths so neither can go silent."""
	deliverable, legacy, antique = _classify_deferred_lines(si)
	del deliverable
	if legacy:
		frappe.msgprint(_(
			"{0} ({1}) has no linked Sales Order — its stock was NOT issued. "
			"Create the Delivery Note manually so inventory and COGS stay "
			"correct.").format(legacy[0].item_code, si.name),
			alert=True, indicator="orange")
	if antique:
		frappe.msgprint(_(
			"{0} ({1}) is an antique firearm — no disposition or Delivery "
			"Note issues its stock. Move it out of inventory manually so "
			"stock and COGS stay correct.").format(antique[0].item_code, si.name),
			alert=True, indicator="orange")


def mark_shipped_block(si, pending, disposed_serials) -> str | None:
	"""THE Mark Shipped refusal rule, shared by mark_shipped_manually AND the
	queue's can_mark_shipped so the page button can never drift from what the
	server would refuse. Payment is NOT part of it — ship_payment_block is the
	shared pre-ship rule, asked by this endpoint and by push_shipment alike, and
	the page applies it on top (an unpaid row offers neither ship action; the
	card says why underneath). Returns the throwable message, or None when
	marking may proceed.

	Undisposed firearms do NOT refuse (user decision, 2026-09-11). Marking
	shipped stamps the ShipStation leg and issues the deferred NON-firearm
	stock lines; a regulated firearm's stock leaves through its disposition's
	Material Issue and nothing here touches it, so an early stamp cannot
	double-book it. The order keeps needs_disposition, so the row STAYS on
	the Pending Order queue with its Dispose button — the shipping half
	clears, the bound-book half does not. warn_undisposed_firearms says so
	at the moment it happens; the A&D disposition is still owed (ATF: by the
	close of the next business day)."""
	if not _is_counter_order(si):
		# This endpoint really moves goods now (Delivery Notes) — a desk-made
		# invoice must not gain a stock-issuing surface just by being
		# submitted and unstamped.
		return _("{0} is not a counter order.").format(si.name)
	waiting = ship_arrival_block(si)
	if waiting:
		return waiting
	# A counter pickup's gun leaves on its 4473 ticket (api/counter_pickup);
	# marking the order picked up with the gun still here would stamp it
	# cleared and drop the ticket out of the queue's sight.
	if pending and is_pickup_order(si):
		return _("{0} still has a firearm to hand over — complete its pickup ticket "
			"(4473) first.").format(si.name)
	if not pending and not disposed_serials and any(
			is_regulated_firearm_item(r.item_code) for r in si.items):
		# Regulated lines but zero resolvable serial state — neither pending
		# nor disposed. A data anomaly (unreadable serials), not an order
		# waiting on a dispose: fail closed rather than clear a gun order
		# whose serials nobody can account for. `not pending` is load-
		# bearing since undisposed rows became allowed — without it every
		# never-disposed order would fall into this arm instead.
		return _(
			"{0} has firearm lines but no serial/disposition state — fix the "
			"serials and Dispose before marking the order shipped."
		).format(si.name)
	return None


def warn_undisposed_firearms(si, pending) -> None:
	"""msgprint (never throw) when an order is marked shipped with firearms
	still on the bound book. Mark Shipped clears the SHIPPING half only: no
	disposition is booked, no FastBound push happens, and the gun's stock
	does not move — its Material Issue rides on the disposition that is
	still owed. The row deliberately stays on the Pending Order queue for
	it, and this is the reminder at the moment the clerk clears the other
	half."""
	if not pending:
		return
	frappe.msgprint(_(
		"{0}: {1} firearm(s) still have NO disposition — {2}. Marking the "
		"order shipped does not touch the bound book. The order stays on the "
		"Pending Order queue until you Dispose, and the A&D entry is still "
		"owed."
	).format(si.name, len(pending), ", ".join(p["serial"] for p in pending)),
		alert=True, indicator="orange")


@frappe.whitelist()
def mark_shipped_manually(sales_invoice: str) -> dict:
	"""Clear an order from the Pending Order queue without a ShipStation push
	— the escape hatch for a destination FFL that expired AFTER dispose, a
	misconfigured integration, a label bought outside ShipStation, or a
	direct order handed over at the counter. Books the Delivery Note leg for
	deferred non-firearm stock lines (stock + COGS), then stamps
	shipstation_status='Shipped' with no order id (the same convention as the
	legacy migration stamp), which satisfies the queue's retention rule.

	Undisposed firearms no longer refuse — the same as the ShipStation push,
	which has always been independent of Dispose. This clears the SHIPPING
	half only: no disposition, no FastBound push, and the gun's stock still
	leaves through its disposition's Material Issue, so the row stays on the
	queue with its Dispose button and the caller is warned
	(warn_undisposed_firearms). The pre-ship payment rule (ship_payment_block
	— the same gate dispose_order and push_shipment share) still applies:
	this path really moves the non-firearm goods.

	Args:
	    sales_invoice: Submitted counter-order Sales Invoice name.

	Returns:
	    {"ok": True, "delivery_notes": [...], "undisposed_serials": [...]} —
	    undisposed_serials names the firearms whose bound-book entry is still
	    owed. Or {"ok": True, "already": True, "shipstation_status": ...}
	    when already pushed/marked.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if si.docstatus != 1:
		frappe.throw(_("{0} is not a submitted invoice.").format(sales_invoice))
	res = clear_shipping_leg(si)
	if not res["ok"]:
		frappe.throw(res["block"])
	return res


def clear_shipping_leg(si) -> dict:
	"""The locked core of Mark Shipped / Picked Up — shared with the counter
	pickup ticket's submit (api/counter_pickup._clear_if_done), which used to
	carry its own copy of this sequence minus the lock and minus the
	unresolved-line warning, so an antique or a legacy stock line on a pickup
	went silently un-issued and a concurrent Mark Picked Up could book the
	Delivery Note leg twice.

	Serializes with push_shipment (and a double-click of the button) on the SI
	row lock, then re-reads the stamp under the lock — without this two
	concurrent calls both see "not shipped" and book stock and COGS twice.

	Returns:
	    {"ok": True, "already": True, "shipstation_status": …} when the order
	    was already pushed or marked; {"ok": False, "block": reason} when a gate
	    refuses; otherwise {"ok": True, "delivery_notes": [...],
	    "undisposed_serials": [...]}. The caller decides whether a block throws.
	"""
	locked = frappe.db.get_value(
		"Sales Invoice", si.name,
		["shipstation_order_id", "shipstation_status"],
		as_dict=True, for_update=True) or frappe._dict()
	if shipstation_pushed(locked.shipstation_order_id, locked.shipstation_status):
		return {"ok": True, "already": True,
			"shipstation_status": locked.shipstation_status}
	pending, disposed_serials, _rows = si_firearm_serial_state(si)
	block = mark_shipped_block(si, pending, disposed_serials) \
		or ship_payment_block(si)
	if block:
		return {"ok": False, "block": block}
	delivery_notes = deliver_pending_stock_lines(si)
	warn_unresolved_stock_lines(si)
	warn_undisposed_firearms(si, pending)
	frappe.db.set_value("Sales Invoice", si.name,
		"shipstation_status", "Shipped", update_modified=False)
	return {"ok": True, "delivery_notes": delivery_notes,
		"undisposed_serials": [p["serial"] for p in pending]}


def _committed_distributor_orders(sales_invoice: str) -> list[str]:
	"""This counter order's Distributor Orders that are past Draft/Queued — placed,
	on hold, shipped or complete — and still owe it goods. Those cannot be
	withdrawn by a cancel (see ffl_integrations.distributor.cancellation.classify).
	An order every line of which is dead (killed by the house) or already covered
	elsewhere (fulfilled from stock / re-sourced, ``substituted_qty``) no longer
	binds the invoice: editing it re-orders nothing that is still coming. Empty
	without the app."""
	if not frappe.db.table_exists("Distributor Order"):
		return []
	names = frappe.get_all("Distributor Order",
		filters={"source_doctype": "Sales Invoice", "source_name": sales_invoice,
			"status": ("not in", ("Draft", "Queued", "Cancelled"))},
		pluck="name")
	live = []
	for name in names:
		lines = frappe.get_all("Distributor Order Line", filters={"parent": name},
			fields=["wish_qty", "substituted_qty", "line_status"])
		if any(l.line_status != "Killed" and int(l.wish_qty or 0) > int(l.substituted_qty or 0)
				for l in lines):
			live.append(name)
	return live


def _ordered_line_locks(si) -> dict:
	"""``{(distributor, item_code): {do_name, lane, qty}}`` — what this order has
	ALREADY bought from a house, by house and item, with the quantity summed over
	the invoice rows it covers.

	An edit re-creates the invoice, so this much must come back: the purchase
	cannot be unmade (no house has a cancel API) and it is carried onto the new
	invoice rather than re-ordered. A Distributor Order carries no link to an
	invoice ROW — house + item is how :func:`_ordered_lines` matches too — so the
	unit here is the (house, item) TOTAL, not a row: two rows of one item share
	one lock, and quantity beyond it is simply new goods to order.
	"""
	committed = _committed_distributor_orders(si.name)
	if not committed:
		return {}
	ordered = {}
	for line in frappe.get_all("Distributor Order Line",
			filters={"parent": ("in", committed)},
			fields=["parent", "item_code", "fulfillment"]):
		house = frappe.db.get_value("Distributor Order", line.parent, "distributor")
		ordered[(house, line.item_code)] = (line.parent, line.fulfillment or "")
	locks = {}
	for row in si.items:
		house = ((getattr(row, "dropship_distributor", "") or "").strip()
			or (getattr(row, "ffl_to_store_distributor", "") or "").strip())
		hit = ordered.get((house, row.item_code)) if house else None
		if not hit:
			continue
		do_name, lane = hit
		# The order line's lane vocabulary is the ROUTER's (local / to_store / fds /
		# direct); a payload line's is the counter's (dropship / to_store). Translate
		# once, here, or every comparison downstream is false. A line that names no
		# lane at all falls back to the invoice row's own stamp.
		to_store = (lane == _SOURCE_TO_STORE) if lane else bool(
			(getattr(row, "ffl_to_store_distributor", "") or "").strip())
		lock = locks.setdefault((house, row.item_code), {
			"do_name": do_name,
			"lane": _SOURCE_TO_STORE if to_store else _SOURCE_DROPSHIP,
			"qty": 0.0,
		})
		lock["qty"] += flt(row.qty)
	return locks


def _assert_ordered_lines_intact(si, payload: dict, locks: dict,
		customer_changed: bool) -> None:
	"""Refuse an edit that changes what a house is already sending.

	The goods on a placed order cannot be recalled through any API, so the edit's
	freedom stops where the purchase starts: an already-ordered line comes back on
	the rebuilt invoice with the same house, lane and quantity (its price may
	change — that is what we charge, not what we paid), and while a drop-ship
	order is in flight to the buyer's FFL the buyer and that FFL are fixed too.
	Everything else — notes, other lines, brand-new lines — edits freely.
	"""
	if not locks:
		return
	# A drop-ship order is already flying to the buyer's FFL: that buyer and that
	# destination are facts of a parcel in transit. Goods ordered to the STORE
	# don't care who buys them — the shop takes delivery either way.
	dropship = [l for l in locks.values() if l["lane"] != _SOURCE_TO_STORE]
	if dropship and (customer_changed
			or str(payload.get("destination_ffl") or "").strip()
			!= str(si.ffl_destination_ffl or "").strip()):
		frappe.throw(_(
			"Distributor order(s) {0} are already shipping to this buyer's dealer — "
			"the buyer and the destination can't be changed here. Use Cancel Order "
			"and coordinate the return.").format(
				", ".join(sorted({l["do_name"] for l in dropship}))))
	wanted: dict = {}
	for item in (payload.get("items") or []):
		key = ((str(item.get("dropship_distributor") or item.get("distributor") or "")).strip(),
			item.get("item_code"))
		lock = locks.get(key)
		if not lock:
			continue
		if (item.get("source") or "") != lock["lane"]:
			frappe.throw(_(
				"{0} was already ordered from {1} on {2} — how it is fulfilled can't "
				"be changed here. Use Cancel Order and coordinate the distributor "
				"order.").format(item.get("item_code"), key[0], lock["do_name"]))
		wanted[key] = wanted.get(key, 0.0) + flt(item.get("qty") or 0)
	# Short of what the house is sending = something was removed or reduced, and
	# nobody can un-order it. MORE is fine: the extra is new goods, ordered on its
	# own Distributor Order by the rebuild.
	short = {k: v for k, v in locks.items() if wanted.get(k, 0.0) < flt(v["qty"])}
	if short:
		frappe.throw(_(
			"{0} was already ordered from {1} on {2} — it can't be removed or reduced "
			"here (the price can still change). Use Cancel Order and coordinate the "
			"distributor order.").format(
				", ".join(sorted(k[1] for k in short)),
				", ".join(sorted({k[0] for k in short})),
				", ".join(sorted({l["do_name"] for l in short.values()}))))


def _edit_ship_to(si) -> dict:
	"""The edit payload's ``ship_to`` — the invoice's own shipping Address in the
	page's field names, or ``{}`` for a pickup or an order that ships to a dealer
	(there the Address on the invoice is the FFL's premise, not the buyer's)."""
	block = None if si.ffl_destination_ffl else invoice_ship_address(si)
	if not block:
		return {}
	return {
		"address_line1": block.get("line1") or "",
		"address_line2": block.get("line2") or "",
		"city": block.get("city") or "",
		"state": block.get("state") or "",
		"pincode": block.get("zip") or "",
	}


def _fire_counter_order_replaced(old_invoice: str, new_invoice: str) -> None:
	"""Fire ``ffl_counter_order_replaced`` — an edited counter order's documents
	(its Distributor Orders) belong to the invoice that replaced it. Mirrors
	``_fire_counter_distributor_hook``: one positional pair, a raise rolls the
	whole edit back, empty hook list (no ffl_integrations) → no-op."""
	for fn in frappe.get_hooks("ffl_counter_order_replaced"):
		frappe.get_attr(fn)(old_invoice, new_invoice)


def _fire_counter_hold_changed(sales_invoice: str) -> None:
	"""Fire ``ffl_counter_hold_changed`` with the reloaded invoice — a submitted
	counter order started holding a gun without a submit/cancel event (Fulfill
	from local stock), so each channel reconciles it as its own on_submit would.
	Empty hook list (no channel app) → no-op."""
	fns = frappe.get_hooks("ffl_counter_hold_changed")
	if not fns:
		return
	doc = frappe.get_doc("Sales Invoice", sales_invoice)
	for fn in fns:
		frappe.get_attr(fn)(doc)


def _arrived_to_store_serials(si) -> dict:
	"""``{house: {item_code: [serial, ...]}}`` — the guns a ship-to-store order has
	already received and hung on this invoice (assign_procured_serials). The edit
	rebuilds those lines as ship-to-store again, serial-less (get_order_for_edit),
	and _carry_arrived_goods hangs the serials back from THIS record — never from
	the client's payload."""
	out: dict = {}
	for row in si.items:
		house = (getattr(row, "ffl_to_store_distributor", "") or "").strip()
		if not house or int(getattr(row, "delivered_by_supplier", 0) or 0):
			continue
		for serial in (getattr(row, "serial_no", "") or "").split("\n"):
			if serial.strip():
				out.setdefault(house, {}).setdefault(row.item_code, []).append(serial.strip())
	return out


def _carry_arrived_goods(old_invoice: str, new_invoice: str, arrived: dict,
		draft_ticket: str | None) -> None:
	"""Goods already in hand follow an edited order: every arrived serial is hung
	on the rebuilt invoice's matching ship-to-store line (that IS the counter
	hold), and an open pickup ticket is re-pointed at the new invoice so its
	parked 4473 stays where it is. A serial that fails to re-hang throws, rolling
	the whole edit back — the original order survives untouched."""
	for house, by_item in arrived.items():
		res = assign_procured_serials(new_invoice, by_item, distributor=house)
		wanted = {s for serials in by_item.values() for s in serials}
		missing = wanted - set(res.get("assigned") or [])
		if missing:
			frappe.throw(_(
				"Could not carry serial(s) {0} onto the edited order — reload and try "
				"again.").format(", ".join(sorted(missing))))
	if draft_ticket:
		# `modified` moves on purpose: a copy of the ticket already open on a
		# register must fail its next save (Frappe's timestamp check) instead of
		# writing the cancelled invoice's name back over this link.
		frappe.db.set_value("POS Invoice", draft_ticket, {
			PICKUP_LINK_FIELD: new_invoice,
			"remarks": ticket_remarks(new_invoice),
		})
		frappe.get_doc("POS Invoice", draft_ticket).add_comment("Comment", _(
			"Counter order {0} was edited — this ticket now hands over {1}.").format(
			old_invoice, new_invoice))


def _hold_days_limit(distributor: str) -> int:
	"""Days a house may hold an order before the queue flags it (that house's
	Settings → hold_escalate_days; 3 when the house has no Settings)."""
	from ffl_integrations.distributor.api import house_setting
	return int(house_setting(distributor, "hold_escalate_days", 3))


def trouble_legs(dropship_legs: list) -> list[dict]:
	"""Distributor legs that need a person: a killed line,
	a short ship, or a hold older than the house's limit. Each names the order
	and its web reference so the call to the distributor can quote them."""
	out = []
	for d in dropship_legs or []:
		if d.status in ("Cancelled", "Draft", "Queued"):
			continue
		lines = frappe.get_all("Distributor Order Line", filters={"parent": d.name},
			fields=["sku", "wish_qty", "invoiced_qty", "line_status", "substituted_qty"])
		killed = [l.sku for l in lines if l.line_status == "Killed"
			and int(l.wish_qty or 0) > int(l.substituted_qty or 0)]
		short = [l.sku for l in lines if l.line_status in ("Shipped", "Received")
			and int(l.invoiced_qty or 0) < int(l.wish_qty or 0) - int(l.substituted_qty or 0)]
		head = frappe.db.get_value("Distributor Order", d.name,
			["web_ref", "placed_at", "hold_status"], as_dict=True) or frappe._dict()
		base = {"do_name": d.name, "distributor": d.distributor, "web_ref": head.get("web_ref") or ""}
		if killed:
			out.append({**base, "kind": "killed", "detail": ", ".join(killed)})
		if short:
			out.append({**base, "kind": "short", "detail": ", ".join(short)})
		if d.status == "Error":
			err = (frappe.db.get_value("Distributor Order", d.name, "error") or "").strip()
			out.append({**base, "kind": "error", "detail": err[:80] or _("placement error")})
		if d.status == "Hold" and head.get("placed_at"):
			days = (frappe.utils.now_datetime() - head.placed_at).days
			if days >= _hold_days_limit(d.distributor):
				out.append({**base, "kind": "hold", "detail": f"{head.get('hold_status') or 'Hold'} · {days}d"})
	return out


@frappe.whitelist()
def resource_line(sales_invoice: str, row_name: str, distributor: str) -> dict:
	"""Order an unarrived ship-to-store line again from another house.

	A Draft Distributor Order is created for the line at the new house (placed
	from the queue's Place order as usual); the old order's line is retired with
	``substituted_qty`` so the queue stops waiting on it and its parcel, if it
	still comes, is restock. The old order is NOT cancelled at the distributor —
	neither house has a cancel API; the page tells the operator to phone them
	and quotes the web reference.

	Args:
	    sales_invoice: The counter order.
	    row_name: The Sales Invoice Item row.
	    distributor: The house to order from now.

	Returns:
	    {order, row_name, do_name: <new Draft DO>, retired: <old DO>}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	from ffl_integrations.distributor.counter import order_line_to_store

	si = frappe.get_doc("Sales Invoice", sales_invoice)
	row = next((r for r in si.items if r.name == row_name), None)
	target = next((s for s in substitutable_rows(si) if s["row_name"] == row_name), None) if row else None
	if not target:
		frappe.throw(_("Row {0} is not an unarrived ship-to-store line of {1}.").format(
			row_name, sales_invoice))
	if distributor == target["distributor"]:
		frappe.throw(_("{0} is the house this line is already ordered from.").format(distributor))
	# Only what the old house still owes: units it already delivered (a short
	# ship) are on the shelf for this order and must not be bought again.
	qty = min(int(row.qty), int(target["owed"]))
	new_name = order_line_to_store(row.item_code, distributor, qty, target["is_firearm"])
	if not new_name:
		frappe.throw(_("{0} could not plan an order for {1}.").format(distributor, row.item_code))
	frappe.db.set_value("Distributor Order", new_name,
		{"source_doctype": "Sales Invoice", "source_name": sales_invoice}, update_modified=False)
	old_line = frappe.get_doc("Distributor Order Line", target["do_line"])
	old_line.db_set("substituted_qty", int(old_line.substituted_qty or 0) + qty)
	frappe.db.set_value("Sales Invoice Item", row.name, "ffl_to_store_distributor", distributor,
		update_modified=False)
	frappe.get_doc("Distributor Order", target["do_name"]).add_comment("Comment", _(
		"{0} × {1} re-sourced to {2} ({3}) for {4}. Not cancelled at the distributor — phone "
		"them; if it still ships it is restock.").format(qty, row.item_code, distributor, new_name,
		sales_invoice))
	si.add_comment("Comment", _("{0} re-sourced from {1} {2} to {3} {4}.").format(
		row.item_code, target["distributor"], target["do_name"], distributor, new_name))
	return {"order": sales_invoice, "row_name": row.name, "do_name": new_name,
		"retired": target["do_name"]}


def _to_store_line_rows(sales_invoice: str) -> dict:
	"""``{(distributor, item_code): [DO line dict…]}`` of this counter order's live
	ship-to-store lines, each with ``owed`` = wish − received − substituted (the
	units the order still needs from that parcel) — 0 on a killed line."""
	if not frappe.db.table_exists("Distributor Order"):
		return {}
	dos = {d.name: d.distributor for d in frappe.get_all("Distributor Order",
		filters={"source_doctype": "Sales Invoice", "source_name": sales_invoice,
			"order_type": "Procure To Store", "status": ("!=", "Cancelled")},
		fields=["name", "distributor"])}
	out: dict = {}
	if not dos:
		return out
	for l in frappe.get_all("Distributor Order Line", filters={"parent": ("in", list(dos))},
			fields=["name", "parent", "item_code", "wish_qty", "received_qty",
				"substituted_qty", "line_status"]):
		l["do_name"] = l.parent
		l["owed"] = 0 if l.line_status == "Killed" else max(0,
			int(l.wish_qty or 0) - int(l.received_qty or 0) - int(l.substituted_qty or 0))
		out.setdefault((dos[l.parent], l.item_code), []).append(l)
	return out


def _unreceived_to_store_orders(sales_invoice: str, line_rows: dict | None = None) -> list[str]:
	"""Live ship-to-store Distributor Orders of this counter order that still owe
	it goods: a line with units neither received nor covered from shelf stock
	(``substituted_qty``). Empty on a site without the distributor app.
	``line_rows`` is a :func:`_to_store_line_rows` result already read for it."""
	if line_rows is None:
		line_rows = _to_store_line_rows(sales_invoice)
	names = {l.do_name for rows in line_rows.values() for l in rows if l.owed > 0}
	return sorted(names)


def substitutable_rows(si, line_rows: dict | None = None) -> list[dict]:
	"""The order's ship-to-store lines that have not arrived and could be served
	from the shelf instead — what the queue's Fulfill from stock button offers.
	``line_rows`` is a :func:`_to_store_line_rows` result already read for it."""
	rows = []
	by_key = _to_store_line_rows(si.name) if line_rows is None else line_rows
	for row in si.items:
		house = (row.ffl_to_store_distributor or "").strip()
		if not house or (row.serial_no or "").strip() or int(row.delivered_by_supplier or 0):
			continue
		lines = [l for l in by_key.get((house, row.item_code), []) if l.owed > 0]
		if not lines:
			continue
		rows.append({"row_name": row.name, "item_code": row.item_code, "item_name": row.item_name,
			"qty": int(row.qty), "is_firearm": is_firearm_item(row.item_code),
			"distributor": house, "do_name": lines[0].do_name, "do_line": lines[0].name,
			"owed": int(lines[0].owed)})
	return rows


def _attach_shelf_stock(orders: list[list[dict]]) -> None:
	"""Write ``available`` — the units of each line's item the shelf could serve
	right now — onto every order's substitutable rows. Counted exactly the way
	fulfill_from_stock checks it (unheld serials for a gun, on-hand qty for an
	accessory), so a Fulfill from local stock button the queue offers is one the
	server honours instead of a click that ends in "nothing on the shelf".

	Takes the WHOLE queue at once: counting the unheld serials of one gun walks
	every submitted invoice that ever carried it (reservations.counter_holds), so
	a per-card count would re-walk that backlog once per card waiting on the same
	popular model. Within one order the units are allotted in row order — two
	lines of the same accessory cannot both be served by the shelf's last one.
	"""
	from ffl_core.api.item_admin import _get_item_stock
	from ffl_core.firearm import available_serials_for_items

	rows = [r for order in orders for r in order]
	if not rows:
		return
	guns = sorted({r["item_code"] for r in rows if r["is_firearm"]})
	others = sorted({r["item_code"] for r in rows if not r["is_firearm"]})
	free = available_serials_for_items(guns, exclude_unavailable=True) if guns else {}
	on_hand = _get_item_stock(others) if others else {}
	for order in orders:
		left: dict[str, int] = {}
		for r in order:
			code = r["item_code"]
			if code not in left:
				left[code] = (len(free.get(code) or []) if r["is_firearm"]
					else int(flt(on_hand.get(code) or 0)))
			need = 1 if r["is_firearm"] else int(r["qty"])
			r["available"] = left[code]
			if left[code] >= need:
				left[code] -= need


@frappe.whitelist()
def fulfill_from_stock(sales_invoice: str, row_name: str, serial_no: str | None = None) -> dict:
	"""Serve a ship-to-store line from shelf stock instead of waiting for the
	distributor's parcel.

	The line becomes an ordinary local leg: its ship-to-store pin is cleared, a
	gun gets the chosen in-stock serial written on (the counter hold), an
	accessory keeps its quantity for the Delivery Note leg. The Distributor Order
	is NOT cancelled (no cancel API at either house): its line's
	``substituted_qty`` goes up, so the queue stops waiting for it and, when the
	parcel lands, the Receive Goods scan books it as plain restock instead of
	holding it for this order. Refused once the goods have arrived (the line
	already carries its serial), for a serial that is not an unheld, in-stock
	unit of the item, or for an accessory the shelf cannot cover.

	Args:
	    sales_invoice: The counter order.
	    row_name: The Sales Invoice Item row.
	    serial_no: For a firearm line, the in-stock serial to hold.

	Returns:
	    {order, row_name, serial_no, do_name, substituted_qty}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	from ffl_core.api.item_admin import _get_item_stock
	from ffl_core.firearm import available_serials_for_items

	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if si.docstatus != 1:
		frappe.throw(_("{0} is not a submitted order.").format(sales_invoice))
	row = next((r for r in si.items if r.name == row_name), None)
	if not row:
		frappe.throw(_("Row {0} is not on {1}.").format(row_name, sales_invoice))
	target = next((s for s in substitutable_rows(si) if s["row_name"] == row_name), None)
	if not target:
		frappe.throw(_("{0} is not waiting for ship-to-store goods (already arrived, "
			"or not a ship-to-store line).").format(row.item_code))
	qty = int(row.qty)
	serial_no = (serial_no or "").strip()
	if target["is_firearm"]:
		free = available_serials_for_items([row.item_code], exclude_unavailable=True)
		if serial_no not in (free.get(row.item_code) or []):
			frappe.throw(_("{0} is not an unheld in-stock serial of {1}.").format(
				serial_no or "(none)", row.item_code))
		warehouse = frappe.db.get_value("Serial No", serial_no, "warehouse") or row.warehouse
		frappe.db.set_value("Sales Invoice Item", row.name, {
			"serial_no": serial_no, "use_serial_batch_fields": 1, "warehouse": warehouse,
			"ffl_to_store_distributor": ""}, update_modified=False)
		# The raw write fires no doc event, and the invoice submitted long ago, so
		# the channels never hear that this shelf gun is now promised. Tell them
		# the way a submit would — the channel apps register their own
		# on_invoice_change; none installed → no-op.
		_fire_counter_hold_changed(sales_invoice)
	else:
		serial_no = ""
		on_hand = float((_get_item_stock([row.item_code]) or {}).get(row.item_code) or 0)
		if on_hand < qty:
			frappe.throw(_("Only {0} of {1} on the shelf — {2} needed.").format(
				on_hand, row.item_code, qty))
		frappe.db.set_value("Sales Invoice Item", row.name, "ffl_to_store_distributor", "",
			update_modified=False)
	# The order line keeps its record; its units are simply no longer owed here.
	do_line = frappe.get_doc("Distributor Order Line", target["do_line"])
	do_line.db_set("substituted_qty", int(do_line.substituted_qty or 0) + qty)
	frappe.get_doc("Distributor Order", target["do_name"]).add_comment("Comment", _(
		"{0} × {1} fulfilled from shelf stock for {2}{3}; when this parcel arrives it is "
		"plain restock.").format(qty, row.item_code, sales_invoice,
		f" (serial {serial_no})" if serial_no else ""))
	si.add_comment("Comment", _("{0} fulfilled from shelf stock instead of {1} {2}{3}.").format(
		row.item_code, target["distributor"], target["do_name"],
		f" — serial {serial_no}" if serial_no else ""))
	return {"order": sales_invoice, "row_name": row.name, "serial_no": serial_no,
		"do_name": target["do_name"], "substituted_qty": int(do_line.substituted_qty)}


def _ordered_lines(sales_invoice: str) -> dict:
	"""``{(distributor, item_code): {sku, unit_cost}}`` as this counter order's live
	Distributor Orders were written — the SKU each house was actually asked for, at
	the unit cost on the order. Empty on a site without the distributor app."""
	if not frappe.db.table_exists("Distributor Order"):
		return {}
	houses = dict(frappe.get_all("Distributor Order",
		filters={"source_doctype": "Sales Invoice", "source_name": sales_invoice,
			"status": ("!=", "Cancelled")},
		fields=["name", "distributor"], as_list=True))
	if not houses:
		return {}
	return {(houses[line.parent], line.item_code): line for line in frappe.get_all(
		"Distributor Order Line", filters={"parent": ("in", list(houses))},
		fields=["parent", "item_code", "sku", "unit_cost"])}


_DELIVERY_OPTIONS = ("signature", "adult_signature", "insurance")


def _delivery_options_key(distributor: str, order_type: str, firearm: bool) -> str:
	"""The key a set of delivery options is filed under — must spell exactly like
	``ffl_integrations.distributor.api.delivery_options_key`` (the planner's freight
	answer and the counter hook use that one; a test pins the two together)."""
	return f"{distributor}|{order_type}|{1 if firearm else 0}"


def _clean_delivery_options(raw) -> dict:
	"""Create Order's ``distributor_options`` → ``{key: {signature, adult_signature,
	insurance}}`` of booleans; anything else is dropped (a bounded, typed map)."""
	if isinstance(raw, str):
		raw = frappe.parse_json(raw)
	if not isinstance(raw, dict):
		return {}
	out = {}
	for key, opts in list(raw.items())[:50]:
		if isinstance(key, str) and 0 < len(key) <= 200 and isinstance(opts, dict):
			out[key] = {k: bool(opts.get(k)) for k in _DELIVERY_OPTIONS}
	return out


def _ordered_delivery_options(sales_invoice: str) -> dict:
	"""The delivery options this counter order's live Distributor Orders were written
	with, keyed like Create Order sends them — so an edit reopens with the same picks.
	Only orders whose options were chosen at the counter; empty without the app."""
	if not frappe.db.table_exists("Distributor Order"):
		return {}
	out = {}
	for do in frappe.get_all("Distributor Order",
			filters={"source_doctype": "Sales Invoice", "source_name": sales_invoice,
				"status": ("!=", "Cancelled"), "delivery_options_set": 1},
			fields=["name", "distributor", "order_type", "delivery_signature",
				"delivery_adult_signature", "delivery_insurance"]):
		codes = frappe.get_all("Distributor Order Line", filters={"parent": do.name},
			pluck="item_code")
		firearm = any(is_firearm_item(code) for code in codes if code)
		out[_delivery_options_key(do.distributor, do.order_type, firearm)] = {
			"signature": bool(do.delivery_signature),
			"adult_signature": bool(do.delivery_adult_signature),
			"insurance": bool(do.delivery_insurance),
		}
	return out


def _is_counter_order(si) -> bool:
	"""THE counter-order marker, shared by cancel_order and
	_editable_or_throw so the two destructive surfaces can't drift: not a
	web-order revenue SI or consignment settlement, and built by
	create_order — a destination FFL (legacy rows predate the fingerprint)
	or the SO-derived + POS Profile pair every create_order invoice
	carries. A desk-made SO→SI chain or a bare desk invoice has no POS
	Profile and stays refused, exactly like before the direct-order
	expansion."""
	if channel_invoice.is_fulfilled_elsewhere(si):
		return False
	if (getattr(si, "ffl_destination_ffl", None) or "").strip():
		return True
	return bool(_linked_sales_orders(si)) \
		and bool((getattr(si, "pos_profile", None) or "").strip())


def _submitted_dn_names(sales_orders: list) -> list[str]:
	"""Submitted Delivery Notes carrying lines against these Sales Orders —
	the ship leg's paper trail, shared by cancel_order (cancel them) and
	_editable_or_throw (refuse to edit past them)."""
	if not sales_orders:
		return []
	return frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": ("in", sales_orders), "docstatus": 1},
		pluck="parent", distinct=True)


def _refuse_with_pickup_ticket(si, drafts: bool = True) -> None:
	"""A counter pickup's 4473 ticket (api/counter_pickup) is a POS Invoice of
	its own: a submitted one has handed the gun over (the sale cannot be
	unwound here), and a draft one carries the order's serial — cancelling
	underneath it would orphan a parked 4473. An EDIT keeps the draft
	(``drafts=False``): update_order re-points it at the rebuilt invoice."""
	done = submitted_pickup_invoices(si.name)
	if done:
		frappe.throw(_(
			"{0} handed its firearm over on {1} — a completed counter pickup "
			"cannot be cancelled or edited from the Pending Order page.").format(
			si.name, ", ".join(done)))
	if not drafts:
		return
	draft = open_pickup_drafts_for_invoices([si.name]).get(si.name)
	if draft:
		frappe.throw(_(
			"{0} has an open pickup ticket {1} — delete that POS Invoice first.").format(
			si.name, draft))


def _editable_or_throw(si) -> None:
	"""Gate shared by get_order_for_edit AND update_order: only an undisposed,
	never-pushed counter transfer order may be edited. Undisposed means the
	edit touches no FastBound/ShipStation/stock state — which is exactly what
	makes the cancel-and-rebuild a pure, fully-rollbackable DB transaction.

	Goods already ORDERED from a house do not block the edit: those lines ride it
	locked and their orders move to the rebuilt invoice (update_order). Goods
	already RECEIVED ride it too — a gun waiting at the counter keeps its serial
	(the hold) and its open pickup ticket (_carry_arrived_goods), so a ready-for-
	pickup order can still be repriced before the customer pays."""
	if si.docstatus == 2:
		frappe.throw(_("{0} is already cancelled.").format(si.name))
	if si.docstatus != 1:
		frappe.throw(_("{0} is not a submitted invoice.").format(si.name))
	if not _is_counter_order(si):
		frappe.throw(_("{0} is not a counter order.").format(si.name))
	if frappe.db.exists("FFL Disposition",
			{"sales_invoice": si.name, "docstatus": 1}):
		frappe.throw(_(
			"{0} has booked disposition(s) — editing would touch the bound "
			"book. Use Cancel Order, then re-create the order.").format(si.name))
	_refuse_with_pickup_ticket(si, drafts=False)
	if shipstation_pushed(getattr(si, "shipstation_order_id", None),
			getattr(si, "shipstation_status", None)):
		frappe.throw(_(
			"{0} was already pushed to ShipStation or marked shipped — use "
			"Cancel Order instead.").format(si.name))
	if _submitted_dn_names(_linked_sales_orders(si)):
		frappe.throw(_(
			"{0} already has delivered stock (a submitted Delivery Note) — "
			"use Cancel Order, then re-create the order.").format(si.name))


@frappe.whitelist()
def get_order_for_edit(sales_invoice: str) -> dict:
	"""Reconstruct the create_order payload from an editable pending order.

	The Edit flow's prefill. Same shape _collectPayload/save_order_draft use,
	so the page hydrates it through the existing draft path. tax_exempt is
	deliberately OMITTED: create_order then falls back to the customer's
	standing flag, so an edit can never silently re-toggle the exemption.

	Args:
	    sales_invoice: Submitted counter-order Sales Invoice (must pass the
	        editable gate: undisposed, never pushed, no completed pickup).

	Returns:
	    {payload: <create_order-shaped dict>, edit_ctx: {sales_invoice,
	    customer, customer_name, contact_email, grand_total, paid_total,
	    last_payment_mode, shipping_tax_inclusive}}. A payload item whose goods are already on order
	    carries ``ordered_do`` (that order's name): the page locks the row, and
	    update_order enforces the lock against the orders themselves.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	si = frappe.get_doc("Sales Invoice", sales_invoice)
	_editable_or_throw(si)

	_pending, _disposed, row_serials = si_firearm_serial_state(si)
	serials_by_row = {item_row.name: serials for item_row, serials in row_serials}
	ordered = _ordered_lines(si.name)
	locks = _ordered_line_locks(si)
	items = []
	for row in si.items:
		serials = serials_by_row.get(row.name) or []
		item = {
			"item_code": row.item_code, "item_name": row.item_name,
			"is_firearm": bool(serials), "qty": row.qty, "rate": row.rate,
			"serials": serials,
		}
		if not serials and (getattr(row, "ffl_storage_location", "") or "").strip():
			item["storage_location"] = row.ffl_storage_location
		if int(getattr(row, "delivered_by_supplier", 0) or 0):
			# Counter drop-ship line: rebuild it WITH its source, or an edit
			# round-trip silently converts it into a local stock line. No serial
			# exists (RSR owns it), so is_firearm comes from the Item, not serials.
			# The house and SKU it was ordered under ride along, so the rebuild and a
			# reopened Fulfillment plan keep them (distributor-settings-layout E5).
			house = getattr(row, "dropship_distributor", "") or ""
			item.update({
				"source": "dropship",
				"dropship_distributor": house, "distributor": house,
				"sku": (ordered.get((house, row.item_code)) or {}).get("sku"),
				"is_firearm": bool(is_firearm_item(row.item_code)),
			})
		elif (getattr(row, "ffl_to_store_distributor", "") or "").strip():
			# Ship-to-store line, on its way OR arrived: rebuild it as one, or the
			# edit turns it into a local line — which a pickup order refuses for a
			# gun (_assert_pickup_order). An arrived gun's serial is deliberately
			# NOT sent: the page would re-validate it against its own pickup
			# ticket's hold and drop it; update_order re-hangs it from the invoice
			# itself (_carry_arrived_goods).
			item.update({
				"serials": [],
				"source": _SOURCE_TO_STORE,
				"channel": _SOURCE_TO_STORE, "channel_confirmed": True,
				"distributor": row.ffl_to_store_distributor,
				"sku": (ordered.get((row.ffl_to_store_distributor, row.item_code)) or {}).get("sku"),
				"is_firearm": bool(is_firearm_item(row.item_code)),
			})
		# Already bought from the house: the page locks the row (quantity, house and
		# removal), and update_order re-checks it against the orders themselves —
		# this flag is the page's label, never the server's proof.
		lock = locks.get((item.get("distributor") or "", row.item_code))
		if lock:
			item["ordered_do"] = lock["do_name"]
		items.append(item)

	is_business = _is_business(si)
	payload = {
		"buyer_type": "business" if is_business else "individual",
		"customer": si.customer,
		"destination_ffl": si.ffl_destination_ffl,
		# No FFL and no shipping Address IS the counter-pickup shape (create_order
		# clears an auto-filled address on one), so the edit resumes as a pickup —
		# otherwise the rebuilt order would ask for an address the sale never had.
		"pickup_in_store": 1 if (is_pickup_order(si)
			or not (si.ffl_destination_ffl or invoice_ship_address(si))) else 0,
		"items": items,
		# Customer-paid shipping (tax inside it on an inclusive order), not income.
		"shipping_amount": flt(getattr(si, "ffl_shipping_amount", 0)),
		"contact_email": si.contact_email or "",
		"notes": (getattr(si, "ffl_staff_note", "") or ""),
		"pos_profile": si.pos_profile or "",
		# Unlike tax_exempt (the customer's standing flag), this one is the sale's own.
		"le_excise_exempt": cint(si.get(le_excise_exempt.FLAG)),
		"le_exempt_buyer": si.get(le_excise_exempt.BUYER) or "",
		"le_exempt_certificate": si.get(le_excise_exempt.CERT) or "",
		"payment_received": 0,
		"dispose_now": 0,
		# The buyer's own door, when that is where the order ships — a rebuild
		# without it asks the operator to retype the address, and a drop-ship
		# accessory line is refused outright for want of one. Read ONLY on an order
		# with no destination FFL: on a firearm order that Address is the dealer's
		# premise, never the buyer's (_counter_ship_to_block).
		"ship_to": _edit_ship_to(si),
		"distributor_options": _ordered_delivery_options(si.name),
	}
	if is_business:
		# Business buyers ship to their own license — the page re-resolves the
		# dealer Customer from this via create_party_from_atf on hydration.
		payload["biz_ffl"] = si.ffl_destination_ffl

	return {
		"payload": payload,
		"edit_ctx": {
			"sales_invoice": si.name,
			"customer": si.customer,
			"customer_name": si.customer_name,
			# So the page can drop a stale carried email on a buyer change.
			"contact_email": si.contact_email or "",
			"grand_total": flt(si.grand_total),
			"paid_total": flt(si.grand_total) - flt(si.outstanding_amount),
			"last_payment_mode": _last_payment_mode(si.name),
			# The preview must price the edit the way update_order will rebuild it.
			"shipping_tax_inclusive": cint(si.get("ffl_shipping_tax_inclusive")),
		},
	}


def _last_payment_mode(si_name: str) -> str | None:
	"""Mode of the most recent submitted Receive Payment Entry referencing this
	invoice — the Cancel dialog's refund-mode default."""
	rows = frappe.get_all(
		"Payment Entry",
		filters=[
			["Payment Entry Reference", "reference_name", "=", si_name],
			["Payment Entry", "docstatus", "=", 1],
			["Payment Entry", "payment_type", "=", "Receive"],
		],
		fields=["mode_of_payment"],
		order_by="posting_date desc, creation desc",
		limit=1)
	return rows[0].mode_of_payment if rows else None


def _linked_sales_orders(si) -> list[str]:
	"""Distinct submitted-or-otherwise Sales Orders the invoice rows point at."""
	return sorted({row.get("sales_order") for row in si.items if row.get("sales_order")})


def _refund_payment_entry(si, amount: float, mode_of_payment: str,
		transaction_number: str | None, reason: str, account: str | None = None) -> str:
	"""Book the refund for a cancelled order: a submitted Pay-type Payment
	Entry to the customer for what they actually paid. The original Receive
	PE(s) stay submitted — the money really arrived — so Receive + Pay net
	the customer's balance to zero. Reuses _mode_of_payment_account so this
	and _book_payment_entry can never drift on account resolution; `account`
	overrides it only to restore a posted row's own account."""
	account = account or _mode_of_payment_account(mode_of_payment, si.company)
	if not account:
		frappe.throw(_(
			"No {0} account is configured for company {1}. Add one on the Mode "
			"of Payment before recording this refund."
		).format(mode_of_payment, si.company))
	pe = frappe.get_doc({
		"doctype": "Payment Entry",
		"payment_type": "Pay",
		"company": si.company,
		"posting_date": nowdate(),
		"mode_of_payment": mode_of_payment,
		"party_type": "Customer",
		"party": si.customer,
		"paid_from": account,
		"paid_to": si.debit_to,
		"paid_amount": amount,
		"received_amount": amount,
		"source_exchange_rate": 1,
		"target_exchange_rate": 1,
		"reference_no": (transaction_number or si.name),
		"reference_date": nowdate(),
		"custom_remarks": 1,
		"remarks": _("Refund for cancelled {0}. Reason: {1}").format(si.name, reason),
	})
	pe.flags.ignore_permissions = True
	pe.insert()
	pe.submit()
	return pe.name


def _receive_payment_entries(si_name: str) -> list[frappe._dict]:
	"""Submitted Receive PEs referencing this invoice, oldest first, each with
	the amount it allocated to THIS invoice. Snapshot BEFORE cancelling the
	invoice — the cancel unlinks the reference rows this reads."""
	refs = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice",
			"reference_name": si_name, "docstatus": 1},
		fields=["parent", "allocated_amount"])
	if not refs:
		return []
	alloc: dict[str, float] = {}
	for r in refs:
		alloc[r.parent] = alloc.get(r.parent, 0.0) + flt(r.allocated_amount)
	pes = frappe.get_all(
		"Payment Entry",
		filters={"name": ("in", list(alloc)), "docstatus": 1,
			"payment_type": "Receive"},
		fields=["name", "mode_of_payment", "reference_no"],
		order_by="posting_date asc, creation asc")
	for pe in pes:
		pe.allocated = alloc[pe.name]
	return pes


def _inline_tender_gl(si) -> list[frappe._dict]:
	"""What an is_pos invoice posted to its tender accounts (payment rows +
	change account), net per account — exactly what its cancel will reverse.
	Read from GL, not the payment rows: change can post to an account no row
	carries, and a row's account may differ from its mode's current default.
	Each entry: {account, net, mode_of_payment, reference_no, amount}."""
	if not si.get("is_pos"):
		return []
	rows = [p for p in (si.get("payments") or []) if p.account]
	accounts = {p.account for p in rows}
	if si.get("account_for_change_amount"):
		accounts.add(si.account_for_change_amount)
	if not accounts:
		return []
	out = []
	for account, net in frappe.db.sql("""select account, sum(debit - credit)
			from `tabGL Entry` where voucher_type='Sales Invoice' and voucher_no=%s
			and is_cancelled=0 and account in %s group by account""",
			(si.name, tuple(accounts))):
		row = next((p for p in rows if p.account == account and flt(p.amount) > 0),
			None) or next((p for p in rows if p.account == account), None)
		mode = row.mode_of_payment if row else "Cash"
		out.append(frappe._dict(account=account, net=flt(net), amount=flt(net),
			mode_of_payment=mode,
			reference_no=((row.reference_no if row else "") or "").strip()))
	return out


def _allocate_advances(new_si, sources) -> float:
	"""Re-point the order's surviving Receive PEs — unallocated customer
	advances since the old invoice's cancel unlinked them — at the rebuilt
	invoice, oldest first, capped at its grand total. Goes through ERPNext's
	own reconciliation engine (the Payment Reconciliation tool's path), which
	also refreshes the invoice outstanding. Never cancels a PE: the money
	really arrived. Returns the total allocated; any excess stays on the PE
	as an advance for the over-payment refund to net out."""
	from erpnext.accounts.utils import reconcile_against_document

	remaining = flt(new_si.grand_total)
	args, allocated = [], 0.0
	for src in sources:
		take = min(flt(src.allocated), remaining)
		if take <= _PAID_TOLERANCE:
			continue
		# erpnext's check_if_advance_entry_modified compares against the PE's
		# LIVE unallocated_amount, not our pre-cancel snapshot — a PE that
		# still carries a lingering unallocated remainder from an earlier
		# edit's overpay refund would otherwise fail "Payment Entry has been
		# modified after you pulled it" on a second edit. allocated_amount
		# (the carry ceiling) stays the snapshot: that's this order's real
		# net credit, not whatever else the PE happens to carry.
		unalloc = flt(frappe.db.get_value(
			"Payment Entry", src.name, "unallocated_amount"))
		args.append(frappe._dict({
			"voucher_type": "Payment Entry",
			"voucher_no": src.name,
			"voucher_detail_no": None,
			"against_voucher_type": "Sales Invoice",
			"against_voucher": new_si.name,
			"account": new_si.debit_to,
			"party_type": "Customer",
			"party": new_si.customer,
			"is_advance": "Yes",
			"dr_or_cr": "credit_in_account_currency",
			"unadjusted_amount": unalloc,
			"unreconciled_amount": unalloc,
			"allocated_amount": take,
			"exchange_rate": 1,
			"difference_amount": 0,
			"grand_total": flt(new_si.grand_total),
			"outstanding_amount": flt(new_si.outstanding_amount),
		}))
		allocated += take
		remaining -= take
	if args:
		reconcile_against_document(args)
	return allocated


def _rebook_inline_tender(inline, old_si, against_si=None, account=None) -> str:
	"""The old invoice's cancel reversed its inline POS tender in GL, but the
	cash never left the drawer — re-book the FULL original amount as a Receive
	Payment Entry. Against the rebuilt invoice when the buyer is unchanged
	(allocation caps at outstanding; any remainder rides as an advance), or as
	a pure advance on the ORIGINAL customer when the buyer changed (the
	full-amount refund then nets it — skipping this re-book would push the
	cash GL below what is physically in the drawer)."""
	mode = inline.mode_of_payment
	company = (against_si or old_si).company
	# `account`: the tender row's own posted account — the cancel reversed THAT
	# one, which may differ from the Mode of Payment's current default.
	account = account or _mode_of_payment_account(mode, company)
	if not account:
		frappe.throw(_(
			"No {0} account is configured for company {1}. Add one on the Mode "
			"of Payment before editing this order.").format(mode, company))
	if against_si is not None:
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
		pe = get_payment_entry("Sales Invoice", against_si.name,
			party_amount=min(flt(inline.amount), flt(against_si.outstanding_amount)),
			bank_account=account)
		pe.paid_amount = flt(inline.amount)
		pe.received_amount = flt(inline.amount)
	else:
		pe = frappe.get_doc({
			"doctype": "Payment Entry", "payment_type": "Receive",
			"company": old_si.company, "posting_date": nowdate(),
			"party_type": "Customer", "party": old_si.customer,
			"paid_from": old_si.debit_to, "paid_to": account,
			"paid_amount": flt(inline.amount),
			"received_amount": flt(inline.amount),
			"source_exchange_rate": 1, "target_exchange_rate": 1,
		})
	pe.mode_of_payment = mode
	pe.reference_no = inline.reference_no or old_si.name
	pe.reference_date = nowdate()
	pe.set_amounts()
	pe.flags.ignore_permissions = True
	pe.insert()
	pe.submit()
	pe.add_comment("Comment", _(
		"Re-booked inline tender of edited {0}.").format(old_si.name))
	return pe.name


def _try_void_shipment(sales_invoice: str) -> tuple[bool, str]:
	"""Best-effort ShipStation void for cancel_order. The POS cancel is a
	committed fact by the time this runs — a void failure must NEVER bubble;
	it degrades to a warning the operator acts on. Lazy get_attr keeps
	ffl_core import-independent of ffl_integrations (absent app → warning)."""
	try:
		out = frappe.get_attr(
			"ffl_integrations.shipstation.api.void_shipment")(sales_invoice) or {}
		return bool(out.get("voided") or out.get("already")), ""
	except Exception as exc:
		frappe.log_error(
			title=f"ShipStation void failed: {sales_invoice}",
			message=frappe.get_traceback())
		return False, str(exc)


@frappe.whitelist()
def cancel_order(sales_invoice: str, reason: str, refund_mode: str | None = None,
		refund_reference: str | None = None) -> dict:
	"""Cancel a counter order: cascade documents, refund what was paid.

	The Pending Order page's Cancel Order action. The POS document work
	commits first — cancel the invoice (its own on_cancel hook cascades the
	FFL/Dealer Transfer dispositions → Material Issue reversal → queued
	FastBound delete), cancel the linked Sales Order(s), and book a refund
	Payment Entry for whatever was actually paid (an at-counter inline payment
	is first restored to its own accounts, since the invoice cancel reversed
	it) — then a best-effort
	ShipStation void runs after that commit. Never deletes documents —
	cancelled records stay as the audit trail. Orders carrying a 4473
	disposition are refused.

	Args:
	    sales_invoice: Submitted counter-order Sales Invoice name.
	    reason: Required free-text cancellation reason.
	    refund_mode: Payment mode for the refund entry (required when
	        anything was paid).
	    refund_reference: Processor reference (required for Zelle, ACH and
	        Check/Money Order refunds).

	Returns:
	    {ok, dispositions_cancelled, delivery_notes_cancelled,
	    sales_orders_cancelled, refund_entry, refunded_amount,
	    shipstation_voided, warning, ...}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	reason = (str(reason or "")).strip()
	if not reason:
		frappe.throw(_("Enter a cancellation reason."))
	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if si.docstatus == 2:
		frappe.throw(_("{0} is already cancelled.").format(sales_invoice))
	if si.docstatus != 1:
		frappe.throw(_("{0} is not a submitted invoice.").format(sales_invoice))
	# Counter orders only — with or without a destination FFL (a direct
	# accessory order cancels here too); see _is_counter_order for the
	# marker. Web-order revenue SIs, consignment settlements and desk-made
	# invoices are refused exactly like before.
	if not _is_counter_order(si):
		frappe.throw(_("{0} is not a counter order.").format(sales_invoice))
	if frappe.db.exists("FFL Disposition", {
			"sales_invoice": si.name, "docstatus": 1,
			"form_4473_status": ("!=", "N/A")}):
		frappe.throw(_(
			"{0} has a 4473 disposition — counter-pickup sales cannot be "
			"cancelled from the Pending Order page.").format(sales_invoice))
	_refuse_with_pickup_ticket(si)

	# Snapshots BEFORE cancel mutates them.
	paid_total = flt(si.grand_total) - flt(si.outstanding_amount)
	if paid_total > _PAID_TOLERANCE:
		refund_mode, refund_reference = _validated_refund(refund_mode, refund_reference)
	# The is_pos inline tender (payment rows + change) is reversed by
	# si.cancel() itself, account by account. Snapshot exactly what the
	# invoice posted to those accounts so it can be restored after the cancel;
	# the Pay PE then takes the refund out of the refund's own account. Booking
	# the Pay PE WITHOUT restoring reversed that cash twice
	# — the same re-book-then-refund shape update_order uses.
	inline_gl = _inline_tender_gl(si)
	dispositions = frappe.get_all(
		"FFL Disposition",
		filters={"sales_invoice": si.name, "docstatus": 1},
		pluck="name")
	sales_orders = _linked_sales_orders(si)

	# Fail fast, BEFORE any mutation: a Delivery Note that also carries
	# another order's lines must refuse the whole cancel — cancelling it
	# would silently un-ship someone else's goods. Checking here (not in
	# the cancel loop below) means a refusal leaves every document exactly
	# as it was, not relying on the request rollback.
	ship_dns = _submitted_dn_names(sales_orders)
	for dn_name in ship_dns:
		# Python-side check on purpose: an unlinked line (NULL
		# against_sales_order) must also refuse, and SQL's `not in` silently
		# skips NULL rows.
		linked = frappe.get_all("Delivery Note Item",
			filters={"parent": dn_name, "docstatus": 1},
			pluck="against_sales_order")
		if any((so or "") not in sales_orders for so in linked):
			frappe.throw(_(
				"Delivery Note {0} also carries lines from another order — "
				"cancel it manually first.").format(dn_name))

	# 1) The invoice. Payments auto-unlink (Accounts Settings), and the
	# on_cancel hook cancels the transfer dispositions, each of which cancels
	# its Material Issue (stock back) and enqueues its FastBound delete.
	si.flags.ignore_permissions = True
	si.cancel()
	leftovers = frappe.get_all(
		"FFL Disposition",
		filters={"sales_invoice": si.name, "docstatus": 1}, pluck="name")
	if leftovers:
		frappe.throw(_(
			"Could not cancel disposition(s) {0} — the order cancel was rolled "
			"back.").format(", ".join(leftovers)))

	# 1.5) Delivery Notes booked by the ship leg (deferred non-firearm stock)
	# reference the Sales Orders, not the invoice — cancel them here so the
	# stock returns and the SO cancel below isn't blocked by an active DN. A
	# DN that mixes another order's lines is refused (fail closed, the whole
	# cancel rolls back) rather than silently un-shipping someone else's goods.
	cancelled_dns = []
	for dn_name in ship_dns:
		dn = frappe.get_doc("Delivery Note", dn_name)
		dn.flags.ignore_permissions = True
		dn.cancel()
		cancelled_dns.append(dn_name)

	# 2) Linked Sales Orders — AFTER the invoice so the billed link is gone.
	# Collect only the ones actually cancelled here (a linked SO may already be
	# cancelled/draft and get skipped) — the report below must reflect that,
	# not the pre-collected `sales_orders` snapshot.
	cancelled_sos = _cancel_submitted_sales_orders(sales_orders)

	# 3) Refund what was actually paid. First restore the inline tender the
	# cancel reversed, in the accounts it was posted to (money in → Receive
	# advance; change paid out of another account → Pay back out of it), so
	# the customer carries the net paid as an advance and every account again
	# holds what it physically holds. Then one Pay PE refunds it all.
	for g in inline_gl:
		if g.net > _PAID_TOLERANCE:
			_rebook_inline_tender(g, si, account=g.account)
		elif g.net < -_PAID_TOLERANCE:
			_refund_payment_entry(si, -g.net, g.mode_of_payment, None,
				_("change given at the counter"), account=g.account)
	refund_entry = None
	if paid_total > _PAID_TOLERANCE:
		refund_entry = _refund_payment_entry(
			si, paid_total, refund_mode, refund_reference, reason)

	si.add_comment("Comment", _(
		"Order cancelled by {0}. Reason: {1}").format(frappe.session.user, reason))

	# The POS unit of work is complete — commit it so the external void can
	# never be voided-then-rolled-back (the inverse divergence of the warning
	# path) and no row locks are held across an outbound HTTP call.
	frappe.db.commit()

	# 4) Best-effort ShipStation void — LAST: nothing DB-critical follows, so
	# a void can neither poison the transaction nor fire before the cancel is
	# a sure thing.
	shipstation_voided, warning = False, ""
	if (si.shipstation_order_id or "").strip():
		shipstation_voided, void_error = _try_void_shipment(si.name)
		if not shipstation_voided:
			warning = _(
				"Could not void ShipStation shipment {0} — void it manually in "
				"ShipStation so the order isn't shipped. ({1})"
			).format(si.shipstation_order_id, void_error or _("see error log"))

	return {
		"ok": True,
		"dispositions_cancelled": dispositions,
		"delivery_notes_cancelled": cancelled_dns,
		"sales_orders_cancelled": cancelled_sos,
		"refund_entry": refund_entry,
		"refunded_amount": paid_total if paid_total > _PAID_TOLERANCE else 0,
		"shipstation_voided": shipstation_voided,
		"warning": warning,
	}


@frappe.whitelist()
def update_order(sales_invoice: str, payload, refund_mode: str | None = None,
		refund_reference: str | None = None) -> dict:
	"""Edit a pending counter order — the Pending Order page's Edit action.
	Cancels the submitted invoice (+ its Sales Orders) and rebuilds it through
	create_order with the edited payload, in ONE transaction: editing is gated
	to undisposed, never-pushed orders, so there are no FastBound/ShipStation/
	stock side effects, and any failure rolls the whole edit back — the
	original order survives untouched. Deliberately NO frappe.db.commit()
	anywhere in this flow.

	Money follows the order (_allocate_advances / _rebook_inline_tender);
	overpay refunds the difference (refund_mode required, Zelle also needs
	refund_reference — same rules as cancel_order). A changed buyer refunds
	the FULL paid amount to the ORIGINAL customer; the new invoice starts
	unpaid.

	Goods a house is already sending follow the order too: those lines must come
	back unchanged (same house, lane and quantity — the price may change) and
	their Distributor Orders are re-pointed at the rebuilt invoice instead of
	being ordered again. New lines are planned and ordered as usual, so adding an
	item to an order already placed buys just that item.

	Args:
	    sales_invoice: Submitted counter-order Sales Invoice (editable gate
	        applies).
	    payload: Edited create_order-shaped payload (dict or JSON string).
	    refund_mode: Refund payment mode when the edit produces an overpay.
	    refund_reference: Processor reference (required for Zelle, ACH and
	        Check/Money Order refunds).

	Returns:
	    {ok, sales_invoice: <new SI>, replaced: <old SI>,
	    sales_orders_cancelled, grand_total, carried_amount, refund_entry,
	    refunded_amount, outstanding, agreements, agreement_email_error} —
	    the last two as create_order's (a link still open is not sent again).

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	payload = _coerce_payload(payload)
	si = frappe.get_doc("Sales Invoice", sales_invoice)
	_editable_or_throw(si)

	paid_total = flt(si.grand_total) - flt(si.outstanding_amount)
	pes = _receive_payment_entries(si.name)
	# What the is_pos tender NET posted per account (change included) — the
	# cancel below reverses exactly this, so it is what gets re-booked. The gross
	# payment rows would carry the change handed back as a phantom advance.
	inline_gl = _inline_tender_gl(si)
	new_customer = (str(payload.get("customer") or "")).strip()
	customer_changed = bool(new_customer) and new_customer != si.customer
	old_contact_email = (str(si.contact_email or "")).strip()

	if customer_changed and paid_total > _PAID_TOLERANCE:
		# fail BEFORE any mutation
		refund_mode, refund_reference = _validated_refund(refund_mode, refund_reference)
	# Goods already in hand ride the edit (_carry_arrived_goods); read once here.
	arrived = _arrived_to_store_serials(si)
	draft_ticket = open_pickup_drafts_for_invoices([si.name]).get(si.name)
	if customer_changed and draft_ticket:
		# The open pickup ticket is the OLD buyer's 4473 workspace — it cannot be
		# re-pointed at a different person.
		frappe.throw(_(
			"{0} has an open pickup ticket {1} for the current buyer — delete that "
			"POS Invoice before changing the buyer.").format(si.name, draft_ticket))

	locks = _ordered_line_locks(si)
	_assert_ordered_lines_intact(si, payload, locks, customer_changed)

	# 1) Cancel the old order. Payments auto-unlink into customer advances
	# (Accounts Settings unlink_payment_on_cancellation_of_invoice); the
	# disposition guard guarantees no cascade beyond the invoice itself.
	sales_orders = _linked_sales_orders(si)
	si.flags.ignore_permissions = True
	# An order already placed with a house survives this cancel and moves to the
	# rebuilt invoice (step 4), so the distributor guard must not refuse the
	# cancel over it. Draft/Queued orders are still withdrawn there, exactly as
	# on any other cancel — the rebuild re-creates them.
	si.flags.ffl_edit_carry_forward = True
	# Mute ERPNext's "Payment Entries are un-linked" msgprint the cancel emits —
	# left on, it surfaces on the page and buries our success dialog (seen in the
	# live e2e). Scoped to just this cancel; restored right after.
	_prev_mute = frappe.flags.mute_messages
	frappe.flags.mute_messages = True
	try:
		si.cancel()
	finally:
		frappe.flags.mute_messages = _prev_mute
	# TOCTOU guard: a disposition could have been booked between
	# _editable_or_throw's check and this cancel (a concurrent Dispose Now) —
	# refuse and roll back the whole edit rather than rebuild over a race.
	if frappe.db.exists("FFL Disposition", {"sales_invoice": si.name, "docstatus": 1}):
		frappe.throw(_(
			"{0} was disposed while this edit was in progress — reload and "
			"try again.").format(si.name))
	cancelled_sos = _cancel_submitted_sales_orders(sales_orders)

	# 2) Rebuild through create_order — its own validation (serials, FFL,
	# POS profile, taxes) IS the edit's validation; a throw rolls back 1).
	# The rebuilt invoice always books on account: payment arrives via the
	# carry below, never via the payload.
	payload["payment_received"] = 0
	payload["dispose_now"] = 0
	payload.pop("draft", None)
	# Stale-email belt: on a buyer swap, get_order_for_edit carried the ORIGINAL
	# buyer's contact_email into the payload — left as-is, the rebuilt invoice
	# (and any later deliberate re-send) would address the WRONG person. Blank it
	# only when it's the unchanged carry; a freshly-typed new address survives.
	if customer_changed and str(payload.get("contact_email") or "").strip().lower() \
			== old_contact_email.lower():
		payload["contact_email"] = ""
	# Editing must NEVER auto re-email a sale receipt: a swap would mail buyer B's
	# firearm receipt to buyer A, and any edit would duplicate-mail. Staff re-send
	# the corrected invoice deliberately from the Sales Invoice. send_sale_receipt
	# ENQUEUES in-process during this submit, so this in-process flag reliably
	# suppresses it (the background send never gets queued).
	_prev_suppress = frappe.flags.get("ffl_suppress_sale_receipt")
	frappe.flags.ffl_suppress_sale_receipt = True
	# Already-bought lines must NOT be ordered a second time by the rebuild. They
	# are excluded from the hook's context here (a request-scoped flag, never a
	# payload key — a client must not be able to book a distributor line that
	# nobody orders) and the orders behind them move to the new invoice in 4).
	_prev_carried = frappe.flags.get("ffl_carried_distributor_lines")
	frappe.flags.ffl_carried_distributor_lines = {k: v["qty"] for k, v in locks.items()}
	try:
		# The rebuild keeps the order's own freight tax basis, whatever FFL
		# Settings says now.
		created = _create_order(payload,
			shipping_tax_inclusive=cint(si.get("ffl_shipping_tax_inclusive")),
			# The order's own prices carry over; only a price lowered in this edit asks.
			prior_rates=price_approval.prior_rates(si))
	finally:
		frappe.flags.ffl_suppress_sale_receipt = _prev_suppress
		frappe.flags.ffl_carried_distributor_lines = _prev_carried
	new_si = frappe.get_doc("Sales Invoice", created["sales_invoice"])

	# 3) The purchases follow the order: every distributor order that survived the
	# cancel now belongs to the rebuilt invoice (the same re-point an amend does).
	# Unconditional — the cancel above let EVERY live order through, so anything
	# `locks` didn't see (an order whose lines were all re-sourced, or one a worker
	# placed mid-edit) would otherwise be stranded on the cancelled invoice.
	_fire_counter_order_replaced(si.name, new_si.name)
	# ...and so do the goods already in hand: the arrived gun's serial (the hold)
	# and its open pickup ticket.
	_carry_arrived_goods(si.name, new_si.name, arrived, draft_ticket)

	# 4) Money follows the order.
	carried, refund_entry, refunded = 0.0, None, 0.0
	if paid_total > _PAID_TOLERANCE:
		if customer_changed:
			# Same restore cancel_order does before its full refund.
			for g in inline_gl:
				if g.net > _PAID_TOLERANCE:
					_rebook_inline_tender(g, si, account=g.account)
				elif g.net < -_PAID_TOLERANCE:
					_refund_payment_entry(si, -g.net, g.mode_of_payment, None,
						_("change given at the counter"), account=g.account)
			refund_entry = _refund_payment_entry(
				si, paid_total, refund_mode, refund_reference,
				_("order edited — buyer changed to {0}").format(new_customer))
			refunded = paid_total
		else:
			carried += _allocate_advances(new_si, pes)
			for g in inline_gl:
				if g.net > _PAID_TOLERANCE:
					new_si.reload()
					alloc_cap = flt(new_si.outstanding_amount)
					_rebook_inline_tender(g, si, against_si=new_si, account=g.account)
					carried += min(flt(g.net), alloc_cap)
				elif g.net < -_PAID_TOLERANCE:
					# Change paid out of an account no tender row used: put it
					# back out of that account, as cancel_order does.
					_refund_payment_entry(si, -g.net, g.mode_of_payment, None,
						_("change given at the counter"), account=g.account)
			over = flt(paid_total) - flt(carried)
			if over > 0.01:
				refund_mode, refund_reference = _validated_refund(
					refund_mode, refund_reference)
				new_si.reload()
				refund_entry = _refund_payment_entry(
					new_si, over, refund_mode, refund_reference,
					_("order edited — total decreased"))
				refunded = over

	si.add_comment("Comment", _(
		"Order edited by {0} — replaced by {1}.").format(
		frappe.session.user, new_si.name))
	new_si.add_comment("Comment", _("Created by editing {0}.").format(si.name))

	new_si.reload()
	return {
		"ok": True,
		"sales_invoice": new_si.name,
		"replaced": si.name,
		"sales_orders_cancelled": cancelled_sos,
		"grand_total": flt(new_si.grand_total),
		"carried_amount": flt(carried),
		"refund_entry": refund_entry,
		"refunded_amount": flt(refunded),
		"outstanding": flt(new_si.outstanding_amount),
		"agreements": created.get("agreements") or [],
		"agreement_email_error": created.get("agreement_email_error"),
	}


# --------------------------------------------------------------- read helpers


def _serial_ctx_for_item(ctx: dict, item_code: str) -> tuple[dict, list, frappe._dict]:
	"""Per-item lookups behind _serial_status — reservation map, price rows,
	item meta — cached in `ctx` so a bulk check (draft resume) pays the heavy
	counter_holds/pricing scans ONCE per distinct item, not once
	per serial."""
	if item_code not in ctx:
		ctx[item_code] = (
			counter_holds(item_code),
			# exclude_unavailable=False: this is a PRICE lookup, not the POS
			# pick-list — it must return every on-hand serial (incl. one held
			# only by the invoice being edited) so _serial_status can resolve its
			# price. The held/consigned exclusion belongs to the pickers.
			available_serials_with_prices(
				[item_code], exclude_unavailable=False).get(item_code) or [],
			frappe.db.get_value(
				"Item", item_code, ["item_name", "is_antique"], as_dict=True)
				or frappe._dict(),
		)
	return ctx[item_code]


# ctx key for the custody-warehouse set. Deliberately not an item_code: unlike
# every other entry in `ctx` this value is per-REQUEST, not per-item, and the
# double-underscore name cannot collide with a real Item.
_CUSTODY_CTX_KEY = "__custody_warehouses__"


def _custody_warehouses(ctx: dict) -> set:
	"""The custody-warehouse set, resolved once per request.

	`custody_warehouse_names()` is deliberately uncached globally — a stale
	answer there means a customer's gun looks sellable, which is the failure
	this whole feature exists to prevent. But `check_serials_available` (draft
	resume) calls _serial_status once per gun, so an un-memoised call would cost
	2N reads for N guns. `ctx` is exactly the request-scoped cache this function
	already keeps for the other heavy per-item lookups (review a)."""
	if _CUSTODY_CTX_KEY not in ctx:
		from ffl_core.warehouses import custody_warehouse_names
		ctx[_CUSTODY_CTX_KEY] = custody_warehouse_names()
	return ctx[_CUSTODY_CTX_KEY]


def _presale_warning(serial_no: str) -> str | None:
	"""The live "somebody may already have bought this" advisory, or None.

	Wrapped rather than called inline so the scan path has one place that
	decides the probe is advisory: any failure here returns None and the sale
	continues. See `reservations.channel_presale_probe` for why it fails open.
	"""
	try:
		answer = channel_presale_probe(serial_no)
	except Exception:  # noqa: BLE001 — advice must never break scan-to-add
		return None
	return (answer or {}).get("message") or None


def _serial_status(term, ctx: dict | None = None,
		ignore_invoice: str | None = None, *,
		probe: bool = True) -> tuple[dict | None, str | None]:
	"""(line data, None) when the serial is sellable, (None, reason) when not —
	the single rulebook behind the throwing find_serial_for_order (scan-to-add),
	the non-throwing bulk check_serials_available (draft resume) and the
	create_order / update_order submit gate (_validate_serial).

	probe: run the live, advisory channel pre-sale probe. The submit gate passes
	False — the cashier was already told at scan time, and a submit must not
	wait on a marketplace round trip per gun.

	ignore_invoice: the Edit flow re-validates an order's OWN serial lines, so a
	serial held only by the invoice being edited must not read as unavailable —
	otherwise the prefill (and a delete-then-rescan of the same gun) would reject
	the order's own firearm. Pass the SI being edited to exempt its own hold."""
	term = (str(term or "")).strip()
	if not term:
		return None, _("Enter a serial number.")
	# Normalised up front so the custody lookup below and the per-item lookups
	# further down share ONE cache across a bulk check.
	ctx = ctx if ctx is not None else {}
	# shelf_state_columns: the same row reservations.serial_shelf_state judges,
	# so the optional consigned_out / gb_sold_unknown flags come along.
	sn = frappe.db.get_value(
		"Serial No", term, ["name", *shelf_state_columns()], as_dict=True)
	if not sn:
		return None, _(
			"No serial {0} is on file — check the number, or intake the gun "
			"first."
		).format(term)
	if not is_firearm_item(sn.item_code):
		return None, _(
			"Serial {0} belongs to {1}, which is not a firearm — add it as a "
			"regular item."
		).format(sn.name, sn.item_code)
	# The shared sellability rulebook (reservations.serial_shelf_state), judged
	# off the row already in hand. holds={} on purpose: the counter-hold rule
	# stays below, because it must exempt the edited invoice's own hold
	# (ignore_invoice) and run after the claim block.
	state = serial_shelf_state(sn.name, row=sn, holds={})
	reason = state["reason"]
	if reason in ("not_active", "no_warehouse"):
		return None, _("Serial {0} is not in stock (status {1}).").format(
			sn.name, sn.status or "—")
	# A customer's transfer gun is Active, in a warehouse and never woo_reserved,
	# and it is sitting in the store's own safe alongside sellable guns. Scanning
	# the wrong one is an ordinary mistake with an extraordinary result: the
	# customer's property disposed to a third party, on a 4473 made out for
	# someone else's gun.
	#
	# Both keys, because this is a SALE gate and the row is already in hand — the
	# same density firearm._pos_picker_excluded_serials rule 4 uses. The shared
	# rulebook checks only the flag; the custody-warehouse half is added here.
	# That docstring names this function its sibling and asks that they be kept
	# aligned; this is that alignment.
	if reason == "custody" or sn.warehouse in _custody_warehouses(ctx):
		return None, _(
			"Serial {0} is a customer's firearm held for an FFL transfer — it is not "
			"the store's to sell. To hand it back to its owner, use the POS customer "
			"transfer flow instead."
		).format(sn.name)
	# A consigned-out gun is already disposed to a dealer; a GunBroker "sold
	# unknown" gun very probably has a buyer there — both fail closed.
	if reason == "consigned_out":
		return None, _("Serial {0} is out on consignment — it is not in the store's "
			"stock to sell.").format(sn.name)
	if reason == "sold_unknown":
		return None, _("Serial {0} may have sold on GunBroker — confirm on GunBroker "
			"and clear Sold Unknown before selling it.").format(sn.name)
	# Every sales channel, not just Woo: a gun the counter sells out from under a
	# marketplace buyer cannot be unwound on GunBroker.
	if reason == "channel_reserved":
		return None, channel_hold_reason(sn.name, state["detail"])
	# Advisory, live, and deliberately AFTER the local flags: a channel may have
	# a buyer we have not polled in yet. Never blocks — the hard refusal is each
	# channel's own `before_submit` hook — but the cashier gets told now rather
	# than at submit time. Costs nothing when no channel app is installed.
	presale = _presale_warning(sn.name) if probe else None
	if presale:
		frappe.msgprint(presale, indicator="orange", alert=True)
	# Unit-level, because an unassigned claim (a web or GunBroker sale not yet
	# matched to a rifle) names no gun and so is invisible to the flag above. The
	# edited order's own hold is not a rival: it holds the very gun being
	# re-validated.
	claim_block = channel_claim_block_reason(sn.item_code, ignore_hold_ref=ignore_invoice)
	if claim_block:
		return None, claim_block
	held_map, prices, item = _serial_ctx_for_item(ctx, sn.item_code)
	held = held_map.get(sn.name)
	# A serial held ONLY by the invoice being edited stays sellable to that same
	# edit (ignore_invoice) — the held_map ref carries the holding SI's name.
	# Sibling of firearm._pos_picker_excluded_serials' current_invoice exemption
	# (same "exempt the editing doc's own hold" concept in two rulebooks): this
	# path matches on ref alone (any hold kind for Create Order / Edit); the POS
	# picker gates its own-hold exemption to a kind='pos' hold. Keep them aligned.
	if held and held.get("ref") != ignore_invoice:
		return None, _("Serial {0} is already held by {1}.").format(
			sn.name, held.get("ref") or held.get("source"))
	sell_price = next(
		(r.get("sell_price") for r in prices if r.get("serial") == sn.name), None)
	return {
		"serial": sn.name,
		"item_code": sn.item_code,
		"item_name": item.item_name or sn.item_code,
		"is_firearm": True,
		"is_antique": bool(item.is_antique),
		"sell_price": sell_price,
	}, None


@frappe.whitelist()
def find_serial_for_order(serial: str, ignore_invoice: str | None = None) -> dict:
	"""Scan-to-add for the Create Order page: resolve a firearm serial number
	straight to an order line (canonical serial + item + tag price). Throws
	with the specific reason when the gun can't be sold — unknown serial, not
	in stock, held by a web order, or already on an open counter order. The
	price comes from the same available_serials_with_prices pass the serial
	dropdown uses, so both entry paths always quote identically.

	Args:
	    serial: Scanned/typed serial number.
	    ignore_invoice: Exempts the edited order's own hold, so staff can
	        delete then re-scan the same gun mid-edit.

	Returns:
	    Order-line data: canonical serial, item_code/item_name, and the tag
	    price.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	data, reason = _serial_status(serial, ignore_invoice=(ignore_invoice or None))
	if reason:
		frappe.throw(reason)
	return data


@frappe.whitelist()
def check_serials_available(serials, ignore_invoice: str | None = None) -> dict:
	"""Bulk, non-throwing serial validation for draft resume / edit prefill.

	Resuming a draft re-checks every parked firearm (drafts don't reserve
	stock, so a gun may have sold in the meantime).

	Args:
	    serials: List (or JSON-encoded list) of typed serial terms.
	    ignore_invoice: Exempts the edited order's own hold — the Edit
	        prefill re-checks the order's own serials, which must not read
	        as unavailable.

	Returns:
	    {typed_term: {ok: True, ...line data} | {ok: False, reason}}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	ignore_invoice = ignore_invoice or None  # '' (client's absent-value) → None
	if isinstance(serials, str):
		serials = _json.loads(serials or "[]")
	out = {}
	ctx: dict = {}  # shared per-item cache — see _serial_ctx_for_item
	for term in serials or []:
		data, reason = _serial_status(term, ctx, ignore_invoice)
		out[term] = {"ok": True, **data} if data else {"ok": False, "reason": reason}
	return out


# ------------------------------------------------------------------ drafts


@frappe.whitelist()
def save_order_draft(payload, name: str | None = None, title: str | None = None) -> dict:
	"""Park the Create Order page's form state as a shared draft (upsert).

	The create_order payload shape plus display extras, stored as a Create
	Order Draft; upserts when `name` is passed. The list columns
	(customer/total/title) are derived here from the same items shape
	create_order itself consumes. Drafts reserve NOTHING; serials are
	re-validated on resume.

	Args:
	    payload: create_order-shaped payload (dict or JSON string).
	    name: Existing draft name to update; blank creates a new one.
	    title: Optional display title; defaults to "<buyer> · N item(s)".

	Returns:
	    {"name": ..., "title": ...}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	raw = payload if isinstance(payload, str) else _json.dumps(payload)
	data = _json.loads(raw or "{}")
	items = data.get("items") or []
	customer = (str(data.get("customer") or "")).strip()
	total = sum(flt(i.get("rate")) * (flt(i.get("qty")) or 1) for i in items)
	title = (str(title or "")).strip() or "{0} · {1} item(s)".format(
		customer or _("No buyer yet"), len(items))
	doc = None
	if name:
		try:
			doc = frappe.get_doc("Create Order Draft", name)
		except frappe.DoesNotExistError:
			frappe.clear_last_message()
	if doc is None:
		doc = frappe.new_doc("Create Order Draft")
	doc.update({"title": title, "customer": customer, "total": total, "payload": raw})
	doc.flags.ignore_permissions = True
	doc.save()
	return {"name": doc.name, "title": doc.title}


@frappe.whitelist()
def list_order_drafts() -> list[dict]:
	"""Shared draft list, newest first — shown in the page's Drafts dialog.

	Returns:
	    List of {name, title, customer, total, modified, modified_by}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	return frappe.get_all(
		"Create Order Draft",
		fields=["name", "title", "customer", "total", "modified", "modified_by"],
		order_by="modified desc")


@frappe.whitelist()
def get_order_draft(name: str) -> dict:
	"""Load one Create Order Draft for resume.

	Args:
	    name: Create Order Draft name.

	Returns:
	    {"name": ..., "title": ..., "payload": <parsed payload dict>}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	doc = frappe.get_doc("Create Order Draft", name)
	return {"name": doc.name, "title": doc.title,
		"payload": _json.loads(doc.payload or "{}")}


@frappe.whitelist()
def delete_order_draft(name: str) -> dict:
	"""Delete a Create Order Draft (missing names are ignored).

	Also the consumed-draft cleanup create_order calls after success.

	Args:
	    name: Create Order Draft name.

	Returns:
	    {"ok": True}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	frappe.delete_doc("Create Order Draft", name,
		ignore_permissions=True, ignore_missing=True)
	return {"ok": True}


@frappe.whitelist()
def available_serials(item_codes) -> dict:
	"""In-stock serials (+ sell/cost price) for the Create Order serial picker.

	Thin pass-through to the canonical firearm helper.

	exclude_unavailable=False: this Create Order picker keeps its existing
	behaviour (the held/consigned exclusion applies to the THREE POS
	firearm.py pickers, not this alias). The counter's real gate is
	_serial_status at scan-to-add / submit, which already rejects a held gun and
	honours ignore_invoice for the order being edited.

	Args:
	    item_codes: Item code(s) — list, JSON list, or comma-separated.

	Returns:
	    {item_code: [{serial, sell_price, cost, ...}]} (see
	    firearm.available_serials_with_prices).

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	return available_serials_with_prices(item_codes, exclude_unavailable=False)


@frappe.whitelist()
def order_line_costs(items, sales_invoice: str | None = None) -> list:
	"""Unit cost of each Create Order line, for the page's read-only Cost column.

	Display only — nothing here feeds create_order or its checks. Each line is
	costed through the figure the store already keeps for it: a serial →
	Serial No.purchase_rate (the Sales Report's gun cost); a drop-ship / ship
	to store line → the Distributor Order line's unit_cost on the order being
	edited, else that house's catalog quote (the Fulfillment plan's cost); any
	other store line → its stock valuation, else last purchase rate (the
	fulfilment-options local cost). A line with no house chosen yet, or a gun
	without a serial, has no cost to show.

	Args:
	    items: Create Order lines — list or JSON list of {item_code, serials,
	        channel or source, distributor or dropship_distributor, sku}.
	    sales_invoice: The order being edited, whose Distributor Orders carry
	        its distributor lines' costs.

	Returns:
	    One unit cost per line, in input order — a float, or None when it is
	    not known (never 0).
	"""
	frappe.only_for(_ROLES)
	if isinstance(items, str):
		items = _json.loads(items or "[]")
	items = [i if isinstance(i, dict) else {} for i in (items or [])]
	ordered = _ordered_lines(sales_invoice) if sales_invoice else {}
	serials = [next(iter(i.get("serials") or []), None) for i in items]
	purchase = dict(frappe.get_all("Serial No",
		filters={"name": ("in", [s for s in serials if s])},
		fields=["name", "purchase_rate"], as_list=True)) if any(serials) else {}

	def cost_of(line, serial):
		item_code = (str(line.get("item_code") or "")).strip() or None
		if serial:
			return purchase.get(serial)
		if (line.get("channel") or line.get("source")) in _ORDERED_SOURCES:
			house = line.get("distributor") or line.get("dropship_distributor")
			if not house:
				return None
			on_order = (ordered.get((house, item_code)) or {}).get("unit_cost")
			if flt(on_order) > 0:
				return on_order
			return (_dropship_quote(item_code, house, line.get("sku") or None) or {}).get("cost")
		if not item_code or is_firearm_item(item_code):
			return None
		from ffl_core.api.item_admin import local_unit_cost
		return local_unit_cost(item_code)

	return [flt(c) if flt(c) > 0 else None
		for c in (cost_of(line, serial) for line, serial in zip(items, serials))]


@frappe.whitelist()
def verify_ffl(ffl_number: str) -> dict:
	"""Live-verify an FFL number and upsert the ATF FFL Record.

	Delegates to the eZ Check integration (online verify + address).

	Args:
	    ffl_number: Full FFL number to verify.

	Returns:
	    The eZ Check lookup result (see
	    ffl_integrations.atf.ez_check.lookup_and_create).

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	from ffl_integrations.atf.ez_check import lookup_and_create
	return lookup_and_create(ffl_number)


@frappe.whitelist()
def save_cr_ffl(cr_details) -> dict:
	"""Record a C&R (Type 03) collector's license and return its ATF FFL Record.

	verify_ffl cannot serve a collector: the ATF's public FFL list contains no
	Type 03 licensees at all (that is the whole reason this path exists), so
	the destination's name, premises and expiry come from a human reading the
	license copy at the counter. This is that capture for the Create Order
	page — the same validation the web-order dispose runs
	(compliance.cr_transfer), so the counter cannot accept what the queue
	would reject.

	The record it upserts is an ordinary destination FFL from here on: the
	ship-to, the destination-based sales tax and the FFL Transfer disposition
	all read it the way they read any other.

	Args:
	    cr_details: {license_number, license_expiry, contact_name,
	        premise_street, premise_street2?, premise_city, premise_state,
	        premise_zip} — dict or JSON string.

	Returns:
	    {name}: the ATF FFL Record to post back as destination_ffl.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	# No order exists yet — the destination is being chosen. The ship-to
	# comparison inside the validator is therefore vacuous BY CONSTRUCTION and
	# not skipped: _resolve_ship_to derives this order's address FROM the
	# record below, so there is no second address it could disagree with.
	details = cr_transfer._validate_cr_details(cr_details, frappe._dict(name=""))
	name = cr_transfer.ensure_cr_ffl_record(
		ffl_number=details["license_number"],
		licensee_name=details["contact_name"],
		street=details["premise_street"],
		street2=details["premise_street2"],
		city=details["premise_city"],
		state=details["premise_state"],
		zip_code=details["premise_zip"],
		expiry_date=details["license_expiry"],
		loaded_from="cr_counter_order",
	)
	frappe.get_doc("ATF FFL Record", name).add_comment("Comment", _(
		"C&R license read off the copy by {0} for a counter order: {1} / {2} / {3} {4}."
	).format(frappe.session.user, details["license_number"], details["contact_name"],
		details["premise_city"], details["premise_state"]))
	return {"name": name}


@frappe.whitelist()
def quick_create_individual(payload) -> dict:
	"""Create an individual (non-dealer) Customer with a primary Contact.

	The Contact carries the receipt email.

	Args:
	    payload: {customer_name (required), email_id (required — receipt
	        destination), phone?} — dict or JSON string.

	Returns:
	    {name, customer_name, email_id, phone}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	payload = _coerce_payload(payload)
	name = (str(payload.get("customer_name") or "")).strip()
	email = (str(payload.get("email_id") or "")).strip()
	phone = (str(payload.get("phone") or "")).strip()
	if not name:
		frappe.throw(_("Enter the buyer's name."))
	if not email:
		frappe.throw(_("Enter the buyer's email — it's where the receipt is sent."))

	cust = frappe.new_doc("Customer")
	cust.customer_name = name
	cust.customer_type = "Individual"
	cust.is_ffl_dealer = 0
	cust.customer_group = _individual_group()
	cust.territory = frappe.db.get_single_value("Selling Settings", "territory") \
		or _first_leaf("Territory")
	cust.flags.ignore_permissions = True
	cust.insert()

	contact = frappe.new_doc("Contact")
	contact.first_name = name
	contact.append("email_ids", {"email_id": email, "is_primary": 1})
	if phone:
		contact.append("phone_nos", {"phone": phone, "is_primary_phone": 1})
	contact.append("links", {"link_doctype": "Customer", "link_name": cust.name})
	contact.flags.ignore_permissions = True
	contact.insert()
	cust.customer_primary_contact = contact.name
	cust.email_id = email
	cust.mobile_no = phone or None
	cust.save()

	return {"name": cust.name, "customer_name": cust.customer_name,
		"email_id": email, "phone": phone}


@frappe.whitelist()
def rename_customer(customer: str, new_name: str) -> dict:
	"""Fix a misspelled customer name SYSTEM-WIDE (the Edit form's pencil):
	update customer_name, rename the doc when its id mirrors the name
	(ERPNext's default customer naming — rename_doc migrates every link), and
	refresh the display-name snapshot on the customer's open sales documents.
	Cancelled documents keep their historical spelling on purpose. The
	primary Contact is NOT renamed (out of scope).

	Args:
	    customer: Customer name (document id).
	    new_name: Corrected display name.

	Returns:
	    {"name": <possibly renamed id>, "customer_name": new_name}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	new_name = (str(new_name or "")).strip()
	if not new_name:
		frappe.throw(_("Enter the corrected name."))
	cust = frappe.get_doc("Customer", customer)
	name = cust.name
	if cust.customer_name == new_name and name == new_name:
		return {"name": name, "customer_name": new_name}
	if name == cust.customer_name:
		# Call the model-level rename, not the frappe.rename_doc *wrapper*: the
		# wrapper can't forward ignore_permissions, and _ROLES admits roles with
		# no Customer write perm (Sales Manager, Stock Manager) that would die in
		# validate_rename's write check — ignore_permissions clears it and matches
		# this module's every-write convention. show_alert=False stops rename_doc
		# from pushing a stray desk alert out of a whitelisted API call.
		from frappe.model.rename_doc import rename_doc as _rename_doc
		name = _rename_doc("Customer", name, new_name,
			ignore_permissions=True, show_alert=False)
	frappe.db.set_value("Customer", name, "customer_name", new_name)
	refresh_customer_name_snapshots(name, new_name)
	return {"name": name, "customer_name": new_name}


# ------------------------------------------------------------------ internals


def _validate_items(items: list) -> tuple[list[dict], set[str], bool]:
	"""Normalise + validate order lines. Firearms are one-per-row with exactly
	one in-stock serial, each serial used only once across the order. Returns
	(rows, serials_seen, has_regulated_firearm)."""
	rows: list[dict] = []
	serials_seen: set[str] = set()
	serial_ctx: dict = {}  # shared per-item cache for _serial_status
	has_regulated = False
	for line in items:
		item_code = (str(line.get("item_code") or "")).strip()
		qty = flt(line.get("qty") or 0)
		rate = flt(line.get("rate") or 0)
		if not item_code:
			# A blank filler row is skippable; an ORDERED line (drop-ship or ship to
			# store) without its Item is not — silently dropping it (the pre-ensure
			# behavior) shrank the invoice while the operator collected the
			# on-screen total.
			if (str(line.get("source") or "")).strip().lower() in _ORDERED_SOURCES:
				frappe.throw(_(
					"A distributor line has no Item reference — remove it and re-add "
					"it from the item search."))
			continue
		if not frappe.db.exists("Item", item_code):
			frappe.throw(_("Item '{0}' not found.").format(item_code))
		source = (str(line.get("source") or "")).strip().lower()
		if source == _SOURCE_TO_STORE:
			# P2: ordered from the distributor INTO the shop,
			# received through the A&D book, then shipped from here like a deferred
			# local line. No serial yet — it is hung on the line when the gun arrives.
			distributor = (str(line.get("distributor") or "")).strip()
			if not distributor:
				frappe.throw(_(
					"'{0}' is marked ship to store but names no distributor — reopen the "
					"fulfillment plan.").format(item_code))
			is_gun = is_firearm_item(item_code)
			if is_gun and qty != 1:
				frappe.throw(_(
					"Ship-to-store firearm '{0}' must be one unit per line.").format(item_code))
			if qty <= 0:
				frappe.throw(_("Quantity for '{0}' must be greater than zero.").format(item_code))
			# A line an edit carries over is bought (maybe received) already — the
			# house's live stock says nothing about it, and a sold-out SKU must not
			# refuse a reprice of a gun sitting at the counter.
			carried = frappe.flags.get("ffl_carried_distributor_lines") or {}
			block = None if (distributor, item_code) in carried else \
				_to_store_availability_block(item_code, distributor,
					(str(line.get("sku") or "")).strip() or None)
			if block:
				frappe.throw(block)
			rows.append({"item_code": item_code, "qty": qty, "rate": rate,
				"serials": [], "source": _SOURCE_TO_STORE,
				"to_store_distributor": distributor,
				"sku": (str(line.get("sku") or "")).strip() or None,
				"restricted_states": frappe.db.get_value(
					"Item", item_code, "restricted_states") or ""})
			if is_regulated_firearm_item(item_code):
				has_regulated = True
			continue
		# Drop-ship line: RSR fulfils it, so there is NO local serial and NO
		# warehouse — the one-serial firearm guard below must not fire on it. A
		# drop-ship firearm still sets has_regulated (FDS ships to the buyer's
		# transfer FFL: destination_ffl + the canonical FFL validation stay required,
		# enforced by create_order exactly as for a local gun).
		if source == "dropship":
			# The house the Fulfillment plan picked rides the line (the planner sends
			# both keys; a resumed legacy line may carry only one).
			is_gun = is_firearm_item(item_code)
			elig = _dropship_eligibility(item_code,
				distributor=(str(line.get("dropship_distributor")
					or line.get("distributor") or "")).strip() or None,
				sku=(str(line.get("sku") or "")).strip() or None,
				is_firearm=is_gun)
			if is_gun and qty != 1:
				frappe.throw(_(
					"Drop-ship firearm '{0}' must be one unit per line.").format(item_code))
			if not is_gun and qty <= 0:
				frappe.throw(_(
					"Quantity for '{0}' must be greater than zero.").format(item_code))
			rows.append({"item_code": item_code, "qty": qty, "rate": rate,
				"serials": [], "source": "dropship", "warehouse": "",
				"dropship_distributor": elig["distributor"],
				"sku": elig.get("sku"),
				"restricted_states": elig.get("restricted_states") or ""})
			if is_regulated_firearm_item(item_code):
				has_regulated = True
			continue
		picked = [s.strip() for s in (line.get("serials") or []) if (s or "").strip()]
		if is_firearm_item(item_code):
			if qty != 1 or len(picked) != 1:
				frappe.throw(_(
					"Firearm '{0}' must be one unit per line with exactly one serial."
				).format(item_code))
			serial = picked[0]
			serial_wh = _validate_serial(serial, item_code, serials_seen, serial_ctx)
			serials_seen.add(serial)
			rows.append({"item_code": item_code, "qty": 1, "rate": rate,
				"serials": [serial], "warehouse": serial_wh})
			if is_regulated_firearm_item(item_code):
				has_regulated = True
		else:
			if qty <= 0:
				frappe.throw(_("Quantity for '{0}' must be greater than zero.").format(item_code))
			rows.append({"item_code": item_code, "qty": qty, "rate": rate, "serials": [],
				"storage_location": storage.validate_open_location(item_code, line.get("storage_location"))})
	if not rows:
		frappe.throw(_("Add at least one item to the order."))
	return rows, serials_seen, has_regulated


def _dropship_eligibility(item_code: str, distributor: str | None = None,
		sku: str | None = None, *, is_firearm: bool = False) -> dict:
	"""Server-side drop-ship eligibility for a counter line.

	The house is the one the line names — the Fulfillment plan's pick, ordered from
	whatever house the Item's web listing is sourced from (distributor-settings-layout
	E; the Item's ``dropship_distributor`` stamp is never changed here). A named
	house must exist, be enabled and allow drop-ship. A line naming none (a
	hand-built payload) falls back to the Item's stamp, which must be set.

	Returns ``{distributor, sku, restricted_states}`` — the row carries them so the
	counter hook orders from that house with that house's SKU, and create_order
	enforces the per-line state restriction against the right destination (
	accessory ↔ ship-to, firearm ↔ FFL premise). ``restricted_states`` is the
	operator's ``Item.restricted_states`` PLUS the house's own ``blocked_states`` for
	the SKU (strictest wins; a provider without a restriction feed adds
	nothing). The distributor's live block / qty checks come from the same quote."""
	if distributor:
		block = _lane_block(distributor, "direct", is_firearm=is_firearm)
		if block:
			frappe.throw(_("{0} can't drop-ship {1} — {2}. Reopen the fulfillment plan.")
				.format(distributor, item_code, block))
	else:
		distributor = frappe.db.get_value("Item", item_code, "dropship_distributor")
		if not distributor:
			frappe.throw(_(
				"{0} is not set up for distributor drop-ship — pick a stocked item or "
				"configure its distributor first.").format(item_code))
	q = _dropship_quote(item_code, distributor, sku)
	block = _dropship_block_reason(item_code, q)
	if block:
		frappe.throw(block)
	q = q or {}
	restricted = ",".join(s for s in [
		frappe.db.get_value("Item", item_code, "restricted_states") or "",
		*(q.get("restricted_states") or [])] if str(s).strip())
	return {"distributor": distributor, "sku": q.get("sku") or sku,
		"restricted_states": restricted}


def _dropship_quote(item_code: str, distributor: str, sku: str | None) -> dict | None:
	"""The distributor quote for one drop-ship line — the house and SKU it names —
	via ``ffl_integrations.distributor.api.quote`` (a LOCAL catalog read; the
	real-time re-confirm stays at placement). None when the integration is absent or
	the provider errors: a provider hiccup must NOT block a counter sale
	(FillOrKill=Y kills a truly-unavailable line at the distributor), so the error
	degrades to "no quote" and is logged."""
	try:
		from ffl_integrations.distributor import api as _dist_api
	except ImportError:
		return None  # no distributor integration installed → nothing to judge
	try:
		return _dist_api.quote(item_code=item_code, sku=sku, distributor=distributor)
	except Exception as exc:  # noqa: BLE001 — provider trouble must not sink the counter
		if _capability_off(exc):
			return None
		frappe.log_error(title="counter dropship: quote failed",
			message=frappe.get_traceback())
		return None


def _capability_off(exc) -> bool:
	"""The site has no distributor catalog (ffl_integrations' CapabilityOff, read by
	its ``capability`` so ffl_core imports nothing): no quote, not an error — and
	the quote's "not set up" message is not shown on a sale that goes ahead."""
	if not getattr(exc, "capability", None):
		return False
	log, exc_id = frappe.local.message_log, getattr(exc, "__frappe_exc_id", None)
	if exc_id and log and isinstance(log[-1], dict) and log[-1].get("__frappe_exc_id") == exc_id:
		frappe.clear_last_message()
	frappe.local.response.pop("capability_off", None)
	return True


def _dropship_block_reason(item_code: str, q: dict | None) -> str | None:
	"""A human reason the quoted SKU can't be drop-shipped right now (distributor
	block or zero catalog qty), or None — including for no quote at all (see
	:func:`_dropship_quote`)."""
	if q is None:
		return None
	# Judge on the RESOLVED mode, not the raw distributor flag — a brand
	# with a `Direct OK` authorization (e.g. Sig) legitimately drop-ships even
	# though the feed still says blocked. A quote missing the key (an older
	# integrations build) falls back to the raw flag so this can never be
	# LAXER than the data available.
	mode = str(q.get("fulfillment_mode") or "").strip()
	blocked = (mode and mode != "Direct") or (not mode and q.get("dropship_blocked"))
	if blocked:
		return _(
			"{0} is procure-to-store — the distributor won't drop-ship it. "
			"Sell it as a local line and order it via Restock.").format(item_code)
	if flt(q.get("qty")) <= 0:
		return _("{0} is out of stock at the distributor.").format(item_code)
	return None


def _lane_block(distributor: str, mode: str, *, is_firearm: bool = False) -> str | None:
	"""The distributor layer's answer to "can this house ship a line this way" —
	its switches and its integration's capabilities, in one place (distributor
	api.lane_block). Returns the reason, or None when it can."""
	try:
		from ffl_integrations.distributor import api as _dist_api
	except ImportError:
		return _("the distributor integration is not installed")
	return _dist_api.lane_block(distributor, mode, is_firearm=is_firearm)


def _to_store_availability_block(item_code: str, distributor: str, sku: str | None) -> str | None:
	"""A human reason this line can't be ordered to the store from ``distributor``,
	or None. Unlike drop-ship the fulfilment mode does not matter — a brand the
	distributor won't drop-ship is exactly what this channel is for — so only
	"does the house exist and carry it in stock" is judged. Same degrade rule as
	:func:`_dropship_quote`: a provider hiccup never sinks the sale
	(the order is placed by a human from the queue, after a live re-check)."""
	try:
		from ffl_integrations.distributor import api as _dist_api
	except ImportError:
		return _("Ship to store needs the distributor integration, which is not installed.")
	block = _lane_block(distributor, "to_store")
	if block:
		return _("{0} can't order {1} to the store — {2}. Reopen the fulfillment plan.").format(
			distributor, item_code, block)
	try:
		q = _dist_api.quote(item_code, sku=sku, distributor=distributor)
	except Exception as exc:  # noqa: BLE001 — provider trouble must not sink the counter
		if _capability_off(exc):
			return None
		frappe.log_error(title="counter to_store: quote failed", message=frappe.get_traceback())
		return None
	if not q.get("found"):
		return _("{0} is not in {1}'s catalog.").format(item_code, distributor)
	if flt(q.get("qty")) <= 0:
		return _("{0} is out of stock at {1}.").format(item_code, distributor)
	return None


def _state_restricted(restricted, state) -> bool:
	"""True iff ``state`` (a 2-letter code) is in the item's restricted list —
	accepts a CSV string or a list, case-insensitive.

	Both sides are run through ``normalize_us_state`` first. Without it an
	operator typing "Texas" — or an ATF record whose premise state is spelled out —
	would MISS a "TX" restriction and the restricted goods would ship. A value the
	normaliser does not recognise falls back to an exact upper-cased compare, so
	nothing that matched before stops matching.

	A blank ``state`` returns False, which is only safe because a blank destination
	state can no longer reach here: an accessory's buyer address is
	required complete by :func:`_assert_dropship_ship_to`, and a firearm's FFL premise
	state by :func:`_require_valid_ffl` (``atf_record_block(require_address=True)``
	rejects a missing/garbled state). Those two guards are what make "unknown state ⇒
	not restricted" a non-hole; do not relax either without revisiting this."""
	if not state:
		return False
	raw = restricted.split(",") if isinstance(restricted, str) else (restricted or [])
	codes = {_normalized_state(c) for c in raw if str(c or "").strip()}
	return _normalized_state(state) in codes


def _normalized_state(value) -> str:
	"""COMPARISON form: a US state as its 2-letter code, falling back to an
	upper-cased exact compare for anything the normaliser doesn't know."""
	from ffl_core.compliance.interstate_tax import normalize_us_state

	return normalize_us_state(value) or str(value or "").strip().upper()


def _storage_state(value) -> str:
	"""STORAGE form: the 2-letter code when we recognise it, else the operator's
	value verbatim — an address we cannot parse should be written down as given,
	not upper-cased into something they never typed."""
	from ffl_core.compliance.interstate_tax import normalize_us_state

	if not value:
		return ""
	return normalize_us_state(value) or value


def _line_destination_state(row: dict, destination_ffl: str | None,
		ship_to: dict | None, cache: dict) -> str | None:
	"""The state a shipped line lands in: a FIREARM goes to the destination FFL's
	premise state (looked up once per `cache`), anything else to the ship-to
	state."""
	if is_firearm_item(row["item_code"]):
		if "ffl" not in cache:
			prem = ffl_premise(destination_ffl) if destination_ffl else None
			cache["ffl"] = prem.state if prem else None
		return cache["ffl"]
	return (ship_to or {}).get("state")


def _validate_dropship_restrictions(rows: list, ship_to: dict | None,
		destination_ffl: str | None) -> None:
	"""Block a drop-ship line whose destination state is restricted for that SKU.

	A drop-ship ACCESSORY ships to the buyer's own address → checked against
	``ship_to.state``. A drop-ship FIREARM ships to the transfer FFL (FDS) → checked
	against the FFL PREMISE state, NOT the buyer's door. The premise state is
	resolved at most once.

	A ship-to-store line is re-shipped from the shop to the same destinations
	(re-shipping follows the Item.restricted_states rule), so it is checked the same way;
	a blank ship-to on it means a counter pickup, which ships nowhere."""
	cache: dict = {}
	for r in rows:
		if r.get("source") not in _ORDERED_SOURCES:
			continue
		restricted = r.get("restricted_states") or ""
		if not restricted:
			continue
		state = _line_destination_state(r, destination_ffl, ship_to, cache)
		if _state_restricted(restricted, state):
			frappe.throw(_(
				"{0} can't be shipped to {1} (state restriction) — remove it or "
				"choose a different destination.").format(r["item_code"], state))


def _order_has_dropship(rows: list) -> bool:
	return any(r.get("source") == _SOURCE_DROPSHIP for r in rows)


def _assert_pickup_order(rows: list, payload: dict, has_regulated: bool) -> None:
	"""What a counter pickup may not be (the page mirrors all three, but the
	server is the gate):

	* a FIREARM from stock — a gun the customer takes home today is a retail
	  sale with a 4473, which the POS register books. A gun ordered to the
	  store is the exception: it is handed over on its own POS Invoice, with
	  the 4473, when it arrives (api/counter_pickup);
	* a DROP-SHIP order — the distributor ships to the buyer's door, and a pickup
	  has no door on file. Ship it to the store instead (the same goods, the same
	  order, collected here);
	* a shipment to a destination FFL — that is not a pickup at all.

	Its freight is zeroed by the caller for the same reason: there is no shipment.
	"""
	if has_regulated:
		# A gun ORDERED TO THE STORE may be picked up: it is handed over on a POS
		# Invoice with its 4473 once it arrives (api/counter_pickup). A gun that
		# is on the shelf right now is a retail sale today — the register.
		stock_guns = [r["item_code"] for r in rows
			if r.get("source") != _SOURCE_TO_STORE and is_regulated_firearm_item(r["item_code"])]
		if stock_guns:
			frappe.throw(_(
				"A firearm collected in the shop is a retail sale (4473 + background "
				"check) — ring it on the POS register, or give this order a destination "
				"FFL to ship to. Only a gun ordered to the store can be a pickup: {0}"
			).format(", ".join(stock_guns)), title=_("Not a Counter Pickup"))
	if (str(payload.get("destination_ffl") or "")).strip():
		frappe.throw(_(
			"This order has a destination FFL, so it ships there — clear Pickup in "
			"store, or clear the FFL."), title=_("Not a Counter Pickup"))
	dropship = [r["item_code"] for r in rows if r.get("source") == _SOURCE_DROPSHIP]
	if dropship:
		frappe.throw(_(
			"A drop-ship line ships from the distributor to the customer's address, "
			"which a pickup order doesn't have — order {0} to the store instead."
		).format(", ".join(dropship)), title=_("Not a Counter Pickup"))


def _order_has_ordered_leg(rows: list) -> bool:
	"""Any line the distributor supplies — drop-ship OR ship to store. Either one
	defers the whole order and needs its Distributor Order built at submit."""
	return any(r.get("source") in _ORDERED_SOURCES for r in rows)


def _assert_single_destination_state(rows: list, payload: dict,
		destination_ffl: str | None) -> None:
	"""Every drop-ship leg on one counter order must land in the SAME state.

	The invoice's sales tax is evaluated against a single destination, so an order
	whose FDS gun ships to a transfer FFL in one state while its direct accessory
	ships to a buyer address in another must tax exactly one leg wrong. Same-state
	legs are always equivalent — and a buyer's FFL is usually in their own state — so
	this only forces a split when it genuinely must."""
	cache: dict = {}
	states = set()
	for r in rows:
		if r.get("source") != "dropship":
			continue
		raw = _line_destination_state(r, destination_ffl, payload.get("ship_to"), cache)
		# Same normaliser as the restriction check, so "Texas" and "TX" are one state
		# here too — and an unrecognised value still participates (exact compare)
		# rather than being dropped, which would hide a genuine cross-state split.
		if raw:
			states.add(_normalized_state(raw))
	if len(states) > 1:
		frappe.throw(_(
			"This order's drop-ship items ship to different states ({0}) — sales tax "
			"is computed for one destination, so ring them as separate orders."
		).format(", ".join(sorted(states))), title=_("One Destination Per Order"))


def _assert_dropship_ship_to(rows: list, payload: dict) -> None:
	"""A drop-ship ACCESSORY ships from the distributor straight to the buyer's door,
	so the order needs a COMPLETE destination up front — street, city, state and ZIP
.

	Two things break without it: the Distributor Order is built against a half address
	(the router's standing rule is that a partial destination is worse than no order),
	and :func:`_state_restricted` passes silently on a blank state — an item
	restricted in the buyer's state would ship because nobody knew the state. A
	firearm-only drop-ship order is exempt: an FDS gun ships to the transfer FFL,
	whose premise address `_require_valid_ffl` already forces complete."""
	if not any(r.get("source") == "dropship" and not is_firearm_item(r["item_code"])
			for r in rows):
		return
	block = _counter_ship_to_block(payload) or {}
	missing = [label for key, label in (
		("line1", "street"), ("city", "city"), ("state", "state"), ("zip", "ZIP"))
		if not (str(block.get(key) or "")).strip()]
	if missing:
		frappe.throw(_(
			"A drop-ship accessory ships to the customer, so this order needs a "
			"complete shipping address — missing: {0}."
		).format(", ".join(missing)), title=_("Shipping Address Incomplete"))


def _counter_update_stock(*, dispose_now: bool, has_dropship: bool) -> int:
	"""``update_stock`` for a counter order — what the bench spike concluded.

	Bench-verified (ERPNext 16.28 ``Sales Invoice.validate_dropship_item``): an
	``update_stock=1`` invoice that carries ANY ``delivered_by_supplier`` line is
	hard-rejected, so a drop-ship order can NEVER dispose-now. v1 rule:
	any drop-ship line forces the whole order to defer — a local gun
	settles from the Pending Order queue, a local accessory just doesn't move stock
	(the existing deferred behaviour). Pure, so the T0 rule is unit-pinned in ONE
	place; if the mixed-order policy is ever changed to reject-instead-of-defer, this
	is the only line to change."""
	return 1 if (dispose_now and not has_dropship) else 0


def _assert_dropship_buyer(rows: list, is_business: bool) -> None:
	"""Distributor drop-ship is individual-retail only: a Business
	or dealer buyer can't drop-ship — the whole path is unavailable to them. The
	same holds for ship-to-store lines."""
	if is_business and _order_has_ordered_leg(rows):
		frappe.throw(_(
			"Distributor ordering (drop-ship or ship to store) is for individual retail "
			"customers only — a Business/dealer order can't include it. Remove the "
			"distributor line or stock the item."))


def _counter_ship_to_block(payload: dict) -> dict | None:
	"""The ORIGINAL payload ship-to in the OrderPlan ``{line1,line2,city,state,zip}``
	shape. NEVER re-derived from ``si.shipping_address_name`` — for a firearm
	order that Address is the FFL premise, and a drop-ship ACCESSORY must ship to the
	buyer's own door."""
	st = dict(payload.get("ship_to") or {})
	if not st:
		return None
	state = st.get("state") or ""
	return {
		"line1": st.get("address_line1") or st.get("line1") or "",
		"line2": st.get("address_line2") or st.get("line2") or "",
		"city": st.get("city") or "",
		# Normalised at the entry point so the Distributor Order's
		# ship_address_state lands as a 2-letter code — its field's format — rather than
		# whatever the operator typed. NB this is the STORAGE form, so an
		# unrecognised value passes through VERBATIM (don't mangle an address we
		# can't parse); `_normalized_state` upper-cases its fallback because that is
		# a COMPARISON concern, not a storage one.
		"state": _storage_state(state),
		"zip": st.get("pincode") or st.get("zip") or "",
	}


def _counter_ffl_block(destination_ffl: str | None) -> dict | None:
	"""The transfer FFL as the block ``web_router._order_snapshot`` produces for the web
	path, so the DO builder treats a counter FDS order exactly like a web one."""
	if not destination_ffl:
		return None
	rec = frappe.db.get_value(
		"ATF FFL Record", destination_ffl,
		["ffl_number", "street", "city", "state", "zip"], as_dict=True) or frappe._dict()
	return {
		# NOTE the asymmetry: the ATF doctype's licence column is `ffl_number` (and is
		# its autoname, so name == ffl_number), while the OrderPlan / transfer_dealer
		# contract key is `license`. Reading "license" off the doctype is a 1054.
		"license": rec.get("ffl_number") or destination_ffl,
		"premise_street": rec.get("street") or "",
		"premise_city": rec.get("city") or "",
		"premise_state": rec.get("state") or "",
		"premise_zip": rec.get("zip") or "",
		"ffl_record": destination_ffl,
	}


def _counter_distributor_context(*, payload: dict, rows: list,
		destination_ffl: str | None, is_business: bool) -> dict:
	"""Build the ``ffl_counter_order_distributor`` context: only the
	distributor lines, the ORIGINAL ship-to, the resolved transfer-FFL block,
	buyer type.

	Every line names its own lane — ``dropship`` (the distributor ships to the buyer)
	or ``to_store`` (it ships to us) — plus the house and SKU the operator picked in
	the plan. The hook orders from those facts, never from the Item's drop-ship
	stamp.

	What an edit carries over from the invoice it replaces is left OUT: those goods
	are bought already, and their order moves to this invoice instead
	(update_order). The carry is a QUANTITY per (house, item), not a row — order
	two more of something already on order and only the two are ordered."""
	carried = dict(frappe.flags.get("ffl_carried_distributor_lines") or {})
	lines = []
	for r in rows:
		house = r.get("dropship_distributor") or r.get("to_store_distributor") or ""
		key = (house, r.get("item_code"))
		if carried.get(key):
			bought = min(flt(carried[key]), flt(r.get("qty") or 0))
			carried[key] = flt(carried[key]) - bought
			r = {**r, "qty": flt(r.get("qty") or 0) - bought}
			if not r["qty"]:
				continue
		if r.get("source") == _SOURCE_DROPSHIP:
			lines.append({
				"lane": _SOURCE_DROPSHIP,
				"item_code": r["item_code"],
				"qty": r["qty"],
				"rate": r["rate"],
				"distributor": r.get("dropship_distributor"),
				"sku": r.get("sku"),
				"is_firearm": is_firearm_item(r["item_code"]),
				"restricted_states": r.get("restricted_states") or "",
			})
		elif r.get("source") == _SOURCE_TO_STORE:
			lines.append({
				"lane": _SOURCE_TO_STORE,
				"item_code": r["item_code"],
				"qty": r["qty"],
				"rate": r["rate"],
				"distributor": r.get("to_store_distributor"),
				"sku": r.get("sku"),
				"is_firearm": is_firearm_item(r["item_code"]),
			})
	return {
		"lines": lines,
		"ship_to": _counter_ship_to_block(payload),
		"destination_ffl": _counter_ffl_block(destination_ffl),
		"buyer_type": _BUYER_BUSINESS if is_business else _BUYER_INDIVIDUAL,
		# Signature / adult signature / insurance picked per distributor order in the
		# Fulfillment plan; an order without an entry goes out as its house's settings say.
		"delivery_options": _clean_delivery_options(payload.get("distributor_options")),
	}


def _fire_counter_distributor_hook(sales_invoice_name: str, context: dict) -> None:
	"""Fire the ``ffl_counter_order_distributor`` hook — mirrors
	``ffl_acquisition_sync_push``: one positional pair, no return consumed, a raise
	rolls the whole counter order back. Empty hook list (no ffl_integrations) → no-op."""
	for fn in frappe.get_hooks("ffl_counter_order_distributor"):
		frappe.get_attr(fn)(sales_invoice_name, context)


def _validate_serial(serial: str, item_code: str, serials_seen: set[str],
		ctx: dict | None = None) -> str:
	"""Validate the serial is sellable for item_code; return its warehouse so the
	order ships each firearm from where it actually is (not the POS default).

	The sellable verdict is _serial_status — the same rulebook scan-to-add uses —
	because the serial dropdown and an API/MCP caller reach here without a scan.
	update_order needs no own-hold exemption: it cancels the old invoice first."""
	if serial in serials_seen:
		frappe.throw(_("Serial {0} was selected on more than one line.").format(serial))
	sn = frappe.db.get_value(
		"Serial No", serial, ["item_code", "warehouse"], as_dict=True)
	if not sn:
		frappe.throw(_("Serial {0} does not exist.").format(serial))
	if sn.item_code != item_code:
		frappe.throw(_("Serial {0} belongs to {1}, not {2}.").format(
			serial, sn.item_code, item_code))
	_data, reason = _serial_status(serial, ctx, probe=False)
	if reason:
		frappe.throw(reason)
	return sn.warehouse


def _require_valid_ffl(ffl: str) -> None:
	"""Destination FFL must exist, be unexpired, AND carry a complete shippable
	address (a street + a normalizable US state) before we ship. The address
	drives the destination-based sales-tax decision, so a blank/garbled state
	would otherwise silently tax an out-of-state shipment at the in-state rate.

	The CONDITIONS live in exactly one place —
	dealer_transfer.atf_record_block. This is a throwing wrapper over it, and
	all that lives here is the operator-facing wording and the three dialog
	titles the POS pages key off, selected by reason code. Previously this was
	a hand-copied third statement of the same condition set, free to drift
	away from the badge (_dealer_shippable) and the submit hook
	(require_shippable_ffl_dealer) that are supposed to agree with it."""
	block = dealer_transfer.atf_record_block(ffl, require_address=True)
	if not block:
		return
	code, _reason = block
	if code == dealer_transfer.FFL_BLOCK_EXPIRED:
		frappe.throw(_(
			"Destination FFL {0} has no valid (unexpired) license on file — "
			"run Verify FFL before shipping."
		).format(ffl), title=_("Destination FFL Expired"))
	if code == dealer_transfer.FFL_BLOCK_INCOMPLETE_ADDRESS:
		frappe.throw(_(
			"Destination FFL {0} is missing a complete address (street + a valid "
			"state) — open the FFL and complete it (or run Verify FFL) before "
			"shipping, so sales tax is computed correctly."
		).format(ffl), title=_("Destination FFL Address Incomplete"))
	# FFL_BLOCK_NOT_LINKED (blank) and FFL_BLOCK_NOT_ON_FILE (no such record)
	# are one message here, as they always were: the old code passed a blank
	# ffl straight to get_value, got None back, and said "not on file".
	frappe.throw(_("Destination FFL {0} is not on file — verify it first.").format(ffl))


def _pin_serial(row, src: dict) -> None:
	"""Pin a firearm row's chosen serial + the warehouse it ships from — the
	serial's own (_validate_serial guarantees it's set). Re-applied after every
	set_missing_values, which re-derives item details and drops these."""
	row.use_serial_batch_fields = 1
	row.serial_no = "\n".join(src["serials"])
	row.warehouse = src["warehouse"]


def _apply_serials(si, rows: list[dict]) -> None:
	"""Pin the chosen serials onto the SI firearm rows. SI items mirror the SO
	(and thus payload) item order 1:1, so match by index + item_code."""
	for row, src in zip(si.items, rows, strict=False):
		if src.get("source") == "dropship":
			continue  # RSR ships it — no local serial to pin
		if src.get("serials"):
			if row.item_code != src["item_code"]:
				frappe.throw(_("Invoice line {0} did not line up with the order — "
					"reload and try again.").format(row.idx))
			_pin_serial(row, src)


def _repin_economics(si, rows: list[dict]) -> None:
	"""After the first pricing pass, re-pin each line to the rate the staff set
	(price-list refetch could otherwise zero or override it) and re-assert the
	serials."""
	for row, src in zip(si.items, rows, strict=False):
		row.rate = row.price_list_rate = flt(src["rate"])
		row.discount_amount = 0
		row.discount_percentage = 0
		# set_missing_values derives a margin when the staff rate differs from
		# the Item Price (rate 700 on a 600 price list → margin 100). With the
		# price_list_rate re-pinned to the staff rate above, a surviving margin
		# would be re-ADDED on the next validate (rate_with_margin = 700 + 100
		# = 800) — silently over-billing the SO by exactly the old difference.
		row.margin_type = ""
		row.margin_rate_or_amount = 0
		if src.get("source") == "dropship":
			# RSR fulfils this line: ERPNext's native drop-ship row — no stock
			# issue, no local serial, no warehouse. set_missing_values / set_pos_fields
			# reset these on every save, so they must be re-pinned here (the whole
			# reason this function exists). NEVER _pin_serial — there is no local one.
			row.delivered_by_supplier = 1
			price_approval.mark_quoted(row)
			row.warehouse = None
			row.dropship_distributor = src.get("dropship_distributor") or ""
			# Same FIFO trap as ship to store below: while the invoice still reads
			# update_stock=1, item-details hands a stock gun row an in-stock serial —
			# another customer's gun on our shelf. The distributor ships this one.
			row.serial_no = ""
			continue
		if src.get("source") == _SOURCE_TO_STORE:
			# Ordered into the shop: a plain deferred line (warehouse kept, shipped
			# from here later) that is NOT a drop-ship row — dropship_distributor
			# would send the Sales Report to a drop-ship cost that never exists. The
			# pin is what lets the serial-less firearm line submit, and where the
			# arrival hangs its serial (compliance.firearm.is_awaiting_procurement_row).
			row.delivered_by_supplier = 0
			row.ffl_to_store_distributor = src.get("to_store_distributor") or ""
			price_approval.mark_quoted(row)
			# ERPNext's item-details pass auto-picks a FIFO serial for a stock
			# firearm row while the invoice still reads update_stock=1 (the
			# dealer-shipment before_insert default) — some OTHER customer's gun
			# already on the shelf. This line's gun hasn't been bought yet.
			row.serial_no = ""
			continue
		if src.get("serials"):
			_pin_serial(row, src)
		else:
			# set_missing_values never fills this, but a re-save must not drop it.
			row.ffl_storage_location = src.get("storage_location") or None


def _le_exemption(payload) -> dict:
	"""The peace-officer excise exemption fields for this order's Sales Order and
	Sales Invoice, or {} — compliance.le_excise_exempt checks the paperwork and
	drops the excise rows on their validate."""
	if not cint(payload.get("le_excise_exempt") or 0):
		return {}
	return {
		le_excise_exempt.FLAG: 1,
		le_excise_exempt.BUYER: (str(payload.get("le_exempt_buyer") or "")).strip() or None,
		le_excise_exempt.CERT: (str(payload.get("le_exempt_certificate") or "")).strip() or None,
	}


def _reassert_tax_exemption(si, tax_exempt: bool) -> None:
	"""Force the invoice back to zero tax for a tax-exempt order — the
	counterpart to create_order's insert-time skip of _apply_default_tax_template.
	ERPNext's set_missing_values() independently re-derives taxes_and_charges
	(SellingController.set_missing_lead_customer_details → _get_party_details'
	Tax Rule lookup, and — once is_pos is set — set_pos_fields' unconditional
	POS Profile default) on EVERY call, silently overriding the exemption with
	the company's taxed default. Prod-confirmed: a tax-exempt order paid in
	full (the is_pos inline-tender path) came back 8.25% taxed because of
	exactly this. Must be re-applied after every set_missing_values() call,
	right before the next insert/save — mirrors _repin_economics re-pinning
	rate/discount/margin for the same reason.

	This imperative call is belt-and-suspenders: the real, load-bearing
	enforcement is compliance.customer_tax_exempt.enforce_customer_tax_exemption,
	a validate-time doc_events hook that survives ERPNext's automatic refill
	(a pre-save clear like this one does NOT — see that module's docstring for
	why). Both read the SAME persisted Customer.is_tax_exempt (this function
	is only ever called with the value create_order already resolved from it
	or the payload), so they can't diverge on WHETHER to clear — and both
	delegate to clear_tax_keep_freight so they can't diverge on WHAT survives
	the clear either (the shipping charge freight row does).
	"""
	if not tax_exempt:
		return
	from ffl_core.compliance.customer_tax_exempt import clear_tax_keep_freight
	clear_tax_keep_freight(si)


def _resolve_ship_to(payload: dict, destination_ffl: str | None) -> tuple[dict, str | None]:
	"""(ship_to dict, recipient name) for the order's Address. Firearm orders ship
	to the destination FFL — derive the address from the shared ffl_premise
	helper so the Address, the queue preview, and the ShipStation payload all
	read the same record the same way. Non-firearm orders use the payload
	ship_to / recipient (+ the recipient's phone, so the created Address —
	and any label built from it — carries a contact number)."""
	if destination_ffl:
		ffl = ffl_premise(destination_ffl)
		if ffl:
			return ({"address_line1": ffl.street, "address_line2": ffl.street2 or None,
				"city": ffl.city, "state": ffl.state, "pincode": ffl.zip,
				"phone": ffl.phone},
				ffl.licensee_name or destination_ffl)
	ship_to = dict(payload.get("ship_to") or {})
	if payload.get("recipient_phone"):
		ship_to.setdefault("phone", payload.get("recipient_phone"))
	return (ship_to, payload.get("recipient_name"))


def _ensure_ship_address(customer: str, recipient_name: str | None, ship_to: dict) -> str | None:
	"""Create a Shipping Address from the structured ship-to fields and link it
	to the customer; returns the Address name (or None when no street is given —
	non-firearm orders may leave it blank). Best-effort: a bad address must not
	sink the sale, so failures are logged and skipped."""
	line1 = (str((ship_to or {}).get("address_line1") or "")).strip()
	if not line1:
		return None
	try:
		addr = frappe.new_doc("Address")
		addr.address_title = (str(recipient_name or customer or "Ship To")).strip()[:140] or "Ship To"
		addr.address_type = "Shipping"
		addr.address_line1 = line1
		addr.address_line2 = (str(ship_to.get("address_line2") or "")).strip() or None
		addr.city = (str(ship_to.get("city") or "")).strip() or "Not specified"
		addr.state = (str(ship_to.get("state") or "")).strip() or None
		addr.pincode = (str(ship_to.get("pincode") or "")).strip() or None
		addr.phone = (str(ship_to.get("phone") or "")).strip() or None
		addr.country = "United States"
		addr.append("links", {"link_doctype": "Customer", "link_name": customer})
		addr.flags.ignore_permissions = True
		addr.insert()
		return addr.name
	except Exception:
		frappe.log_error(title="manual_order: ship address create failed",
			message=frappe.get_traceback())
		return None


def _apply_default_tax_template(doc, company: str) -> None:
	"""Pin the company's default sales-tax template (and its rows) so an in-state
	order is taxed. compliance.interstate_tax.apply_interstate_tax runs on validate
	and swaps this for the 0% Out-of-State template for an out-of-state shipping
	address — this only establishes the taxed baseline. The engine's in-state
	branch does NOT (re)populate rows, so we reuse its _set_template to load them."""
	from ffl_core.compliance.interstate_tax import _default_template, _set_template
	tmpl = _default_template(company)
	if tmpl:
		_set_template(doc, tmpl)


def _default_company() -> str | None:
	return (frappe.defaults.get_user_default("company")
		or frappe.db.get_default("company")
		or (frappe.get_all("Company", limit=1, pluck="name") or [None])[0])


def _company_from_warehouses(rows) -> str | None:
	"""The company that owns this order's firearms, derived from the serials'
	warehouses. A single order can't span companies (update_stock can't cross
	them); non-firearm orders return None so the caller falls back to the default."""
	warehouses = {r["warehouse"] for r in rows if r.get("warehouse")}
	if not warehouses:
		return None
	companies = set(frappe.get_all(
		"Warehouse", filters={"name": ["in", list(warehouses)]}, pluck="company"))
	companies.discard(None)
	if len(companies) > 1:
		frappe.throw(_(
			"All firearms on one order must be in the same company's inventory; "
			"found: {0}."
		).format(", ".join(sorted(companies))))
	return next(iter(companies), None)


def _resolve_pos_profile(pos_profile: str | None) -> str | None:
	"""The explicit profile if given (returned unchecked — the caller verifies
	it exists), else the POS Profile of the user's open shift."""
	if pos_profile:
		return pos_profile
	return frappe.db.get_value(
		"POS Opening Entry",
		{"user": frappe.session.user, "status": "Open", "docstatus": 1},
		"pos_profile",
	)


def _store_warehouse(company: str) -> str | None:
	# Delegates to ffl_core.warehouses — the last fallback after the POS
	# Profile's own warehouse, so it must never hand back a per-dealer
	# outbound-consignment warehouse (ffl_core/warehouses.py module docstring).
	from ffl_core.warehouses import default_stock_warehouse
	return default_stock_warehouse(company)


def _default_pos_profile(company: str | None = None) -> str | None:
	"""An enabled POS Profile to source company/warehouse + is_pos payment wiring
	from when no POS shift is open — so building an order never requires manually
	opening a POS shift. Prefers the given (or default) company's profile, else any
	enabled one. (An explicit payload profile / the user's open shift take
	precedence.)"""
	company = company or _default_company()
	name = None
	if company:
		name = frappe.db.get_value(
			"POS Profile", {"disabled": 0, "company": company},
			"name", order_by="creation asc")
	return name or frappe.db.get_value(
		"POS Profile", {"disabled": 0}, "name", order_by="creation asc")


@frappe.whitelist()
def quote_config(company: str | None = None) -> dict:
	"""Tax preview config for the Create Order page.

	The home state and the default On-Net-Total sales-tax rate, so the page
	can show the running tax without a round-trip per keystroke. The
	authoritative tax is still computed server-side at create time from the
	shipping address.

	Args:
	    company: Optional company; defaults to the configured default.

	Returns:
	    {"home_state": ..., "rate": <summed On Net Total %>, "freight_taxable": bool,
	    "shipping_tax_inclusive": bool (a new order's shipping includes its
	    sales tax — FFL Settings), "us_states": {<state name>: <USPS code>},
	    "le_exempt_offered": bool (a California store that names the excise
	    account a peace officer is exempt from), "le_excise_rate": <that
	    account's On Net Total % in the default template>}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	from ffl_core.compliance.interstate_tax import (
		US_STATES, _default_template, freight_taxable, home_state)
	company = (str(company or "")).strip() or _default_company()
	tmpl = _default_template(company) if company else None
	rows = [r for r in frappe.get_doc("Sales Taxes and Charges Template", tmpl).taxes
		if r.charge_type == "On Net Total"] if tmpl else []
	rate = sum(flt(r.rate) for r in rows)
	le_account = le_excise_exempt.exempt_account(company)
	return {"home_state": home_state(company), "rate": rate,
		"le_exempt_offered": bool(le_account),
		"le_excise_rate": sum(flt(r.rate) for r in rows if r.account_head == le_account),
		"freight_taxable": freight_taxable(company),
		"shipping_tax_inclusive": bool(shipping_tax_inclusive_setting()),
		# The server's own name → code table, so the preview reads "Texas" as TX.
		"us_states": US_STATES}


@frappe.whitelist()
def item_tax_rates(item_codes, company: str | None = None, excise_exempt: int = 0) -> dict:
	"""Per-item sales-tax rate for the Create Order preview.

	The summed "On Net Total" rate of the company's default sales-tax template
	as it applies to each item once ERPNext's Item Tax Template resolution
	(the item's own templates, then its item groups') has overridden the
	template rows — the same rate compliance.shipping_charge._effective_rate
	reads off the saved line. A 0% (or Not Applicable) template gives 0, so
	the preview shows no goods tax on a service-only order and, like the
	server, no tax on or inside its shipping.

	Args:
	    item_codes: List (or JSON list) of Item codes.
	    company: Optional company; defaults to the configured default.
	    excise_exempt: 1 for a peace officer's sale — the excise account's
	        rows (compliance.le_excise_exempt) are left out of the rate.

	Returns:
	    {<item_code>: <rate %>} for every known item.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	from erpnext.stock.get_item_details import (
		NOT_APPLICABLE_TAX, get_item_tax_map, get_item_tax_template)
	from ffl_core.compliance.interstate_tax import _default_template
	company = (str(company or "")).strip() or _default_company()
	tmpl = _default_template(company) if company else None
	rows = [r for r in frappe.get_doc("Sales Taxes and Charges Template", tmpl).taxes
		if r.charge_type == "On Net Total"] if tmpl else []
	if cint(excise_exempt):
		le_account = le_excise_exempt.exempt_account(company)
		rows = [r for r in rows if r.account_head != le_account]
	base = {"company": company, "taxes": [
		{"account_head": r.account_head, "rate": r.rate} for r in rows]}
	codes = frappe.parse_json(item_codes) if isinstance(item_codes, str) else item_codes
	out = {}
	for code in set(codes or []):
		if not frappe.db.exists("Item", code):
			continue
		itt = get_item_tax_template(
			{"company": company, "transaction_date": nowdate(), "tax_category": None},
			frappe.get_cached_doc("Item", code), frappe._dict())
		rates = get_item_tax_map(doc=base, tax_template=itt, as_json=False)
		out[code] = sum(0.0 if rates.get(r.account_head) == NOT_APPLICABLE_TAX
			else flt(rates.get(r.account_head)) for r in rows)
	return out


@frappe.whitelist()
def get_customer_tax_status(customer: str) -> dict:
	"""Tax-exempt flag + certificate URL for a customer.

	Lets the Create Order page pre-fill a Business buyer's existing
	exemption.

	Args:
	    customer: Customer name.

	Returns:
	    {"is_tax_exempt": 0|1, "certificate": <file URL or null>}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	v = frappe.db.get_value(
		"Customer", customer, ["is_tax_exempt", "tax_exemption_certificate"],
		as_dict=True) or {}
	return {"is_tax_exempt": int(v.get("is_tax_exempt") or 0),
		"certificate": v.get("tax_exemption_certificate") or None}


@frappe.whitelist()
def set_customer_tax_exempt(customer: str, exempt, certificate: str | None = None) -> dict:
	"""Persist a customer's sales-tax-exempt status (+ optional certificate).

	Checking the exemption is enough to exempt; the certificate is optional
	and, once on file, is attached to the Customer (retrievable any time)
	and reused.

	Args:
	    customer: Customer name.
	    exempt: Truthy ("1"/"true"/"yes") to exempt, else cleared.
	    certificate: Optional certificate file URL to keep on file.

	Returns:
	    {"ok": True, "is_tax_exempt": 0|1, "certificate": <URL or the one
	    already on file>}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(_ROLES)
	if not frappe.db.exists("Customer", customer):
		frappe.throw(_("Customer not found."))
	exempt = 1 if str(exempt) in ("1", "true", "True", "yes") else 0
	on_file = frappe.db.get_value("Customer", customer, "tax_exemption_certificate")
	vals = {"is_tax_exempt": exempt}
	if (certificate or "").strip():
		vals["tax_exemption_certificate"] = certificate
	frappe.db.set_value("Customer", customer, vals)
	return {"ok": True, "is_tax_exempt": exempt,
		"certificate": (certificate or "").strip() or on_file}


def _remember_customer_email(customer: str, email: str) -> None:
	"""Save the receipt email typed on an order onto a buyer who has none on
	file, so the next order fills it in. Never overwrites an email the buyer
	already has (a one-off receipt address stays on that order), and never
	saves a placeholder: one of the store's own inboxes, or an address already
	used for another buyer (staff type a filler when the buyer has no email).
	Best effort: a failure here is logged and never blocks the order."""
	from frappe.utils import validate_email_address

	email = validate_email_address(email)
	if not email or "," in email:   # one address only; a list stays on the order
		return
	cust = frappe.db.get_value("Customer", customer,
		["email_id", "customer_primary_contact", "customer_name"], as_dict=True)
	if not cust or cust.email_id:
		return
	# lazy: contact_email is unindexed, so this scans every invoice; add an index if orders reach ~100k.
	if frappe.db.exists("Email Account", {"email_id": email}) \
			or frappe.db.exists("Customer", {"email_id": email, "name": ["!=", customer]}) \
			or frappe.db.exists("Sales Invoice", {"contact_email": email,
				"customer": ["!=", customer], "docstatus": ["!=", 2]}):
		return
	savepoint = "remember_customer_email"
	frappe.db.savepoint(savepoint)
	try:
		if cust.customer_primary_contact:
			contact = frappe.get_doc("Contact", cust.customer_primary_contact)
			if not any((r.email_id or "").lower() == email.lower() for r in contact.email_ids):
				contact.append("email_ids", {"email_id": email})
			# Contact allows one primary; the buyer has no email on file, so this one is it.
			for r in contact.email_ids:
				r.is_primary = 1 if (r.email_id or "").lower() == email.lower() else 0
		else:
			contact = frappe.new_doc("Contact")
			contact.first_name = cust.customer_name or customer
			contact.append("email_ids", {"email_id": email, "is_primary": 1})
			contact.append("links", {"link_doctype": "Customer", "link_name": customer})
		contact.flags.ignore_permissions = True
		contact.save()
		frappe.db.set_value("Customer", customer, {
			"email_id": email, "customer_primary_contact": contact.name})
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(title=f"Could not save the receipt email onto Customer {customer}")


def _customer_email_hint(si) -> str | None:
	from ffl_core.receipts import _customer_email
	return _customer_email(si) or None


def _individual_group() -> str:
	for g in ("Individual", "All Customer Groups"):
		if frappe.db.exists("Customer Group", g):
			return g
	return _first_leaf("Customer Group")


def _first_leaf(doctype: str) -> str:
	return frappe.get_all(doctype, filters={"is_group": 0}, pluck="name", limit=1)[0]


# Public names for first-party extensions: the
# underscored originals stay for core callers, these are the stable surface.
default_company = _default_company
shipstation_enabled = _shipstation_enabled
require_valid_ffl = _require_valid_ffl
is_paid = _is_paid
serial_status = _serial_status
