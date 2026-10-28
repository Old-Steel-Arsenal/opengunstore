"""ShipStation integration — manual push from the Pending Order page.

The Pending Order page's **Ship** button calls push_shipment on a submitted
Sales Invoice: it builds a ShipStation V2 shipment (POST /v2/shipments with
create_sales_order, so it lands on the ShipStation Orders tab) and persists
shipstation_order_id + shipstation_status='Pending' on the invoice. Staff then
rate-shop and buy the label in the ShipStation UI. Push-only: tracking/status
never sync back here (follow-up), and no carrier/service is preset.

Deliberately synchronous and deliberately NOT hooked on Sales Invoice
on_submit: shipping is an explicit staff action, independent of the bound-book
dispose step, so a ShipStation outage can never block a sale or a gun
shipment.

Ship-to for a firearm order is the destination FFL's premise address with the
CUSTOMER as recipient name and the FFL licensee (DBA first) as company — the
package is addressed to the buyer, care of their chosen dealer.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

from ffl_core.serials import serials_for_line
from ffl_core.roles import COUNTER_ROLES

from ffl_integrations.shipstation.client import (
	ShipStationClient,
	ShipStationConfig,
	ShipStationConfigError,
	ShipStationError,
)

SETTINGS_DOCTYPE = "ShipStation Settings"

# Who may push a shipment. Same set as the counter roles today (a
# ship follows a counter order); single-sourced, re-annotate here if the
# ship axis ever diverges.
_ROLES = COUNTER_ROLES

# weight_uom → ounces. ERPNext UOM names are free-ish text; match lowercased.
_UOM_TO_OUNCES = {
	"ounce": 1.0, "oz": 1.0,
	"lb": 16.0, "lbs": 16.0, "pound": 16.0,
	"g": 0.035274, "gram": 0.035274,
	"kg": 35.274, "kilogram": 35.274,
}


def _enabled() -> bool:
	return bool(frappe.db.get_single_value(SETTINGS_DOCTYPE, "enabled") or 0)


def _to_ounces(value, uom) -> float:
	"""Convert a weight to ounces by UOM name. Unknown or blank UOM means the
	value is taken as ounces already (the historical reading of
	Item.weight_per_unit here)."""
	factor = _UOM_TO_OUNCES.get((uom or "").strip().lower())
	if factor is None:
		return flt(value)
	return flt(value) * factor


def _unit_ounces(item_code: str, default_oz: float) -> float:
	rec = frappe.db.get_value(
		"Item", item_code, ["weight_per_unit", "weight_uom"], as_dict=True
	) or frappe._dict()
	return _to_ounces(rec.get("weight_per_unit") or 0, rec.get("weight_uom")) or flt(default_oz)


def ffl_ship_to(dest: str, recipient: str) -> dict:
	"""V2 ship_to for a parcel to a receiving dealer: the buyer, care of the
	licensee (DBA first), at the licence's premise address."""
	ffl = frappe.db.get_value(
		"ATF FFL Record", dest,
		["licensee_name", "licensee_dba", "street", "street2", "city",
			"state", "zip", "phone"],
		as_dict=True) or frappe._dict()
	return {
		"name": recipient,
		"company_name": ffl.licensee_dba or ffl.licensee_name or dest,
		"phone": ffl.phone,
		"address_line1": ffl.street,
		"address_line2": ffl.street2 or None,
		"city_locality": ffl.city,
		"state_province": ffl.state,
		"postal_code": ffl.zip,
		"country_code": "US",
		# An FFL premise is a business address — better rates, no
		# residential surcharge guessing.
		"address_residential_indicator": "no",
	}


def parcel_item(item_code: str, item_name: str | None, serials: list[str],
		qty, rate, default_oz: float) -> dict:
	"""One ShipStation line item (build_ship_to's ``items`` shape)."""
	name = item_name or item_code
	if serials:
		# Surface the serial(s) on the ShipStation packing slip.
		name = f"{name} (SN: {', '.join(serials)})"[:100]
	return {
		"sku": item_code,
		"name": name,
		# The live V2 API rejects floats here ("Input string '1.0' is not
		# a valid integer") — ERPNext qty is a float, ship counts are not.
		# Floor at 1: cint(0.5) is 0, and a zero-quantity item would be
		# rejected (and zero out its weight term) for a line that still
		# ships one physical parcel.
		"quantity": max(1, cint(qty)),
		"weight": {"value": _unit_ounces(item_code, default_oz), "unit": "ounce"},
		# Selling rate per unit. Without it ShipStation shows Unit Cost
		# $0.00 / Product $0.00 on every order — the
		# counter cannot tell a $595 rifle from a $20 holster on the Orders
		# tab, and there is no declared value to insure a label for.
		"unit_price": flt(rate, 2),
	}


def build_ship_to(sales_invoice: str, default_oz: float | None = None) -> dict:
	"""Assemble the shipment envelope for a Sales Invoice: V2-shaped ship_to,
	line items with per-unit weights, and the FFL number (envelope-level only —
	it is NOT a V2 ship_to key and must not enter the POST body). Pure read.

	Firearm orders ship to the destination FFL's premise address with the
	CUSTOMER as recipient and the licensee (DBA first) as company_name.
	Orders without a destination FFL fall back to the invoice's shipping
	Address and the customer's own name."""
	si = frappe.get_doc("Sales Invoice", sales_invoice)
	recipient = si.customer_name or si.customer
	ship_to: dict = {}
	dest = (getattr(si, "ffl_destination_ffl", None) or "").strip() or None
	if dest:
		ship_to = ffl_ship_to(dest, recipient)
	else:
		from ffl_core.api.manual_order import invoice_ship_address
		addr = invoice_ship_address(si)
		if addr:
			ship_to = {
				"name": recipient,
				"phone": addr.phone,
				"address_line1": addr.line1,
				"address_line2": addr.line2,
				"city_locality": addr.city,
				"state_province": addr.state,
				"postal_code": addr.zip,
				"country_code": "US",
			}

	if default_oz is None:
		default_oz = flt(frappe.db.get_single_value(
			SETTINGS_DOCTYPE, "default_parcel_weight_oz") or 0) or 16.0

	items = []
	all_serials: list[str] = []
	dropped = False
	for row in si.items:
		# A drop-ship line ships from RSR's dock, not ours: keep it out of the
		# parcel entirely (on a mixed order the local parcel must not list the RSR
		# item, and its serials/weight must not enter this shipment).
		if getattr(row, "delivered_by_supplier", 0):
			dropped = True
			continue
		serials = serials_for_line(row)
		all_serials.extend(serials)
		items.append(parcel_item(row.item_code, row.item_name, serials,
			row.qty, row.rate, default_oz))

	# Order money for ShipStation's Cost Summary / Total Paid. Freight is an
	# "Actual" charge row inside `taxes` (compliance.shipping_charge keeps
	# ffl_shipping_amount as its source of truth), so the tax term is what is
	# left once the freight is taken out — freight TAX rows are tax and stay in.
	# ffl_shipping_amount is the customer-paid shipping: on a tax-inclusive order
	# (ffl_shipping_tax_inclusive) it already holds the freight tax, so shipping
	# paid = S and tax paid = the goods tax. Income is the taxes table's Shipping
	# row, never this field.
	# Two things keep the block honest rather than merely populated:
	#   * a mixed order whose drop-ship leg was filtered out of `items` gets NO
	#     money at all — invoice totals cover the distributor's line too, so a
	#     $20 holster parcel would otherwise read "$920 paid";
	#   * nothing paid (a dealer order may ship on account) means no money
	#     block, instead of $0 paid next to $45 tax paid.
	shipping = flt(getattr(si, "ffl_shipping_amount", 0) or 0)
	paid = flt(getattr(si, "grand_total", 0) or 0) - flt(
		getattr(si, "outstanding_amount", 0) or 0)
	# A return / consolidated invoice never gets the freight row materialized,
	# so guard the subtraction instead of shipping a negative tax.
	tax = max(0.0, flt(getattr(si, "total_taxes_and_charges", 0) or 0) - shipping)
	amounts = {} if (dropped or paid <= 0) else {
		"amount_paid": flt(paid, 2),
		"tax_paid": flt(tax, 2),
		"shipping_paid": flt(shipping, 2),
	}
	return {
		"order_number": si.name,
		"ship_to": ship_to,
		"ffl_number": dest,
		"customer_email": getattr(si, "contact_email", None),
		"items": items,
		"serials": all_serials,
		"currency": (getattr(si, "currency", None) or "USD").lower(),
		"amounts": amounts,
	}


def build_shipment(sales_invoice: str, config: ShipStationConfig | None = None) -> dict:
	"""The exact POST /v2/shipments body for one invoice. No carrier/service —
	staff rate-shop in the ShipStation UI. create_sales_order makes the
	shipment appear on the Orders tab; external ids carry the invoice name for
	dedup and traceability."""
	config = config or ShipStationConfig.from_settings()
	env = build_ship_to(sales_invoice, default_oz=config.default_parcel_weight_oz)
	if not env["items"]:
		# Every line ships from the distributor (the filter above removed them all), so there
		# is nothing for the store to send. Building the shipment anyway would put a
		# phantom, item-less label on the Orders tab.
		frappe.throw(_(
			"{0} has nothing for us to ship — every line is drop-shipped from the "
			"distributor, which sends it directly."
		).format(sales_invoice), title=_("Nothing to Ship"))
	return shipment_body(env, config)


def _require_ship_from(config: ShipStationConfig) -> None:
	if not config.ship_from_warehouse_id:
		# The live API rejects a shipment carrying neither ship_from nor
		# warehouse_id — fail fast with the fix instead of a raw 400.
		raise ShipStationConfigError(
			"ShipStation Settings: Ship-From Warehouse ID is empty — run Test "
			"Connection to list your warehouses and paste the default one.")


def shipment_body(env: dict, config: ShipStationConfig) -> dict:
	"""The POST /v2/shipments body for one envelope (build_ship_to's shape).
	Shared with GunBroker's pre-dispose Ship, whose envelope comes off the
	GunBroker Order because there is no invoice yet."""
	_require_ship_from(config)
	total_oz = sum(
		flt(i["weight"]["value"]) * flt(i["quantity"]) for i in env["items"]
	) or flt(config.default_parcel_weight_oz)
	shipment = {
		"external_shipment_id": env["order_number"],
		"external_order_id": env["order_number"],
		"create_sales_order": True,
		"ship_to": env["ship_to"],
		"items": [
			{"sku": i["sku"], "name": i["name"], "quantity": i["quantity"],
			 "unit_price": i["unit_price"]}
			for i in env["items"]
		],
		"packages": [{"weight": {"value": total_oz, "unit": "ounce"}}],
	}
	# Money rides only when there IS money: a zero on an unpaid order is the
	# truth, and ShipStation renders an absent field the same way.
	currency = env.get("currency") or "usd"
	for key, value in (env.get("amounts") or {}).items():
		if flt(value):
			shipment[key] = {"currency": currency, "amount": flt(value, 2)}
	if config.store_id:
		shipment["store_id"] = config.store_id
	if config.ship_from_warehouse_id:
		shipment["warehouse_id"] = config.ship_from_warehouse_id
	if env["serials"]:
		# Staff-facing: internal notes sit in the ShipStation order sidebar,
		# so the serial(s) are visible at a glance (they also ride in the
		# item display names / packing slip). Never customer-facing.
		shipment["internal_notes"] = "Firearm serial(s): " + ", ".join(env["serials"])
	return shipment


@frappe.whitelist()
def push_shipment(sales_invoice: str) -> dict:
	"""Push a submitted order to ShipStation (the Pending Order Ship action).

	Staff buy the label in ShipStation. Synchronous; idempotent (a second
	call after success is a no-op); persists nothing on API failure, so a
	retry after fixing the cause is safe. Independent of the Dispose step.

	Args:
	    sales_invoice: Submitted Sales Invoice with a resolvable ship-to.

	Returns:
	    {ok, shipstation_order_id, shipstation_status, ...} — or {ok: True,
	    already: True, ...} when previously pushed.

	Permissions:
	    Counter shipping roles.
	"""
	frappe.only_for(_ROLES)
	from ffl_core.api.manual_order import (
		_require_valid_ffl,
		deliver_pending_stock_lines,
		invoice_ship_address,
		ship_arrival_block,
		ship_payment_block,
		shipstation_pushed,
		warn_unresolved_stock_lines,
	)

	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if si.docstatus != 1:
		frappe.throw(_("{0} is not a submitted invoice.").format(sales_invoice))
	if not _enabled():
		frappe.throw(_(
			"ShipStation is disabled — configure and enable it in ShipStation "
			"Settings first."))

	# Serialize concurrent pushes: row-lock the invoice and re-read the guard
	# fields under the lock. A second click (other tab/user) blocks here until
	# the first request commits, then sees its write and no-ops.
	locked = frappe.db.get_value(
		"Sales Invoice", si.name,
		["shipstation_order_id", "shipstation_status"],
		as_dict=True, for_update=True) or frappe._dict()
	if shipstation_pushed(locked.shipstation_order_id, locked.shipstation_status):
		return {
			"ok": True,
			"already": True,
			"shipstation_order_id": locked.shipstation_order_id,
			"shipstation_status": locked.shipstation_status,
		}
	# Same rules as dispose_order / mark_shipped_manually, from the shared predicates.
	block = ship_payment_block(si) or ship_arrival_block(si)
	if block:
		frappe.throw(block)

	dest = (getattr(si, "ffl_destination_ffl", None) or "").strip()
	if dest:
		# Fail closed: an expired/incomplete destination FFL must never enter
		# the shipping queue.
		_require_valid_ffl(dest)
	elif not invoice_ship_address(si):
		# Same completeness rule as the queue's has_ship_address and
		# build_ship_to's fallback (invoice_ship_address is the one source):
		# a link to an unusable Address must not reach the API as an empty
		# ship_to. create_order clears the auto-filled default address on
		# pickups, so what's left here really is operator-chosen.
		frappe.throw(_(
			"{0} has no destination FFL and no shipping address — add a "
			"shipping address, or use Mark Shipped for a counter pickup."
		).format(sales_invoice))

	# The stock/COGS leg (Delivery Notes for deferred non-firearm lines)
	# BEFORE the external call. Ordering trade-offs, both deliberate: a DN
	# failure aborts inside the transaction with no orphan ShipStation
	# shipment; a ShipStation failure throws, and — PROVIDED the caller lets
	# the exception escape, which this whitelisted request path does — the
	# whole transaction rolls back, DNs included, so the retry redoes them
	# (idempotency covers the lost-response case, where the DNs did commit).
	# The one exception-swallowing caller, manual_order._try_push_shipment,
	# only runs on ship-now (update_stock=1) invoices, which never classify
	# any deliverable line — keep it that way, or wrap its call in an
	# explicit savepoint. The cost here is holding the SI row lock + the new
	# DN/stock ledger locks across one outbound call, bounded by the client
	# timeout (client.py) — the inverse choice from cancel_order's
	# commit-then-void, accepted because an orphan shipment is a real-money
	# label risk while a held lock is not.
	deliver_pending_stock_lines(si)
	warn_unresolved_stock_lines(si)

	try:
		client = ShipStationClient.from_settings()
		# Lost-response healing: if an earlier push created the shipment but
		# its response never landed (nothing persisted), adopt it instead of
		# double-creating — external_shipment_id carries the invoice name.
		shipment = client.find_shipment_by_external_id(si.name)
		adopted = bool(shipment)
		if not shipment:
			shipment = client.create_shipment(
				build_shipment(sales_invoice, config=client.config))
	except ShipStationConfigError as exc:
		frappe.throw(_("ShipStation is not configured: {0}").format(exc))
	except ShipStationError as exc:
		frappe.throw(_("ShipStation API error: {0}").format(exc))

	# allow_on_submit fields, stamped in place on the submitted doc.
	frappe.db.set_value(
		"Sales Invoice", si.name,
		{"shipstation_order_id": shipment["shipment_id"], "shipstation_status": "Pending"},
		update_modified=False)
	result = {"ok": True, "shipstation_order_id": shipment["shipment_id"]}
	if adopted:
		result["already"] = True
	return result


def void_shipment(sales_invoice: str) -> dict:
	"""Void the ShipStation shipment behind a cancelled POS order so staff
	can't buy a label for a dead sale. NOT whitelisted — called server-side
	(ffl_core.api.manual_order.cancel_order) via frappe.get_attr. Raises
	ShipStationError / ShipStationConfigError upward; the caller treats any
	failure as a warning, never a blocker."""
	fields = frappe.db.get_value(
		"Sales Invoice", sales_invoice,
		["shipstation_order_id", "shipstation_status"], as_dict=True) or frappe._dict()
	shipment_id = (fields.shipstation_order_id or "").strip()
	if not shipment_id:
		return {"ok": True, "skipped": "never pushed"}
	if (fields.shipstation_status or "") == "Cancelled":
		return {"ok": True, "already": True}
	ShipStationClient.from_settings().cancel_shipment(shipment_id)
	frappe.db.set_value("Sales Invoice", sales_invoice,
		"shipstation_status", "Cancelled", update_modified=False)
	return {"ok": True, "voided": True, "shipment_id": shipment_id}


@frappe.whitelist()
def test_connection() -> dict:
	"""Probe the ShipStation connection via GET /v2/carriers.

	The Settings form's Test Connection button.

	Returns:
	    {ok, carrier_count, sandbox, warehouses: [...]}.

	Permissions:
	    System Manager (fires live outbound calls and returns the
	    account's warehouse list).
	"""
	# Settings are System-Manager-gated; so is this probe (it fires live
	# outbound calls and returns the account's warehouse list).
	frappe.only_for(("System Manager",))
	try:
		client = ShipStationClient.from_settings()
	except ShipStationConfigError as exc:
		frappe.throw(_("ShipStation is not configured: {0}").format(str(exc)))
	try:
		body = client.probe_connection()
	except ShipStationError as exc:
		frappe.throw(_("ShipStation API error: {0}").format(str(exc)))
	warehouses = []
	if not client.config.ship_from_warehouse_id:
		# Only fetched while the setting is blank (the form offers the list
		# for copy-paste), and never allowed to mask the probe: the carriers
		# call above already proved the key.
		try:
			warehouses = client.list_warehouses()
		except ShipStationError:
			pass
	return {
		"ok": True,
		"carrier_count": len(body.get("carriers") or []),
		"sandbox": client.config.sandbox_mode,
		"warehouses": [
			{"warehouse_id": w.get("warehouse_id"), "name": w.get("name"),
				"is_default": bool(w.get("is_default"))}
			for w in warehouses
		],
	}


# Public names for first-party extensions.
enabled = _enabled
unit_ounces = _unit_ounces
