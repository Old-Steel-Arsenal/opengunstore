"""Destination-based out-of-state tax exemption (server-enforced).

Texas sales tax follows possession: a sale delivered in Texas (incl. in-store
pickup) is taxed; goods SHIPPED to another state are exempt interstate
commerce (Tax Code §151.005 — no in-state transfer of possession). The engine
therefore keys on the document's SHIPPING address, never the customer's home
address, and a document without a shipping address is always taxed.

Deliberately NOT built on ERPNext Tax Rule: Address.state is free text and
production data already mixes "TX"/"Texas" (Tax Rule matches exact strings),
rule evaluation is client-trigger-driven rather than submit-enforced, and an
address-only rule silently matches address-less walk-in POS customers. This
hook normalizes state spellings and decides on every validate.

Wired on validate of Sales Order / Sales Invoice / Delivery Note
(apply_interstate_tax) and POS Invoice (guard_pos_exemption — POS never
auto-exempts; the guard only blocks a manually mis-picked 0% template).
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils.caching import request_cache

from ffl_core.compliance.channel_invoice import is_channel_mirrored_invoice

# The store's home state is the state of the Company's own address: deliveries
# inside it are taxed, shipments out of it are exempt. Each store runs its own
# site, one Company each. There is no default: a Company with no readable
# address makes ``home_state`` raise rather than tax as some other state.
HOME_COUNTRY = "United States"

# Home states whose separately-stated freight is NOT part of the taxable sales
# price: California — R&TC §6012(c)(7), Reg. 1628(a),
# for common-carrier shipping charged at no more than its cost (the counter asks
# for the actual carrier cost; a mark-up or handling goes on a taxable line).
# Texas taxes it with the goods (Tax Code §151.007(a)); any state not listed is
# treated as taxing it too — an over-collection can be refunded, an
# under-collection is the store's own money. Add a state here only on the
# advice of the store's accountant.
# lazy: a code table and the Company's own (receipt) address decide both, for two
# single-company stores; a third store, or one whose primary address is not its
# premises, gets an explicit Company-level home state / freight flag instead.
FREIGHT_EXEMPT_HOME_STATES = frozenset({"CA"})

OUT_OF_STATE_TEMPLATE_TITLE = "Out of State Sales"

US_STATES = {
	"ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
	"CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
	"DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA",
	"HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN",
	"IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA",
	"MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI",
	"MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
	"NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ",
	"NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC",
	"NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
	"PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
	"SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
	"VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
	"WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
	"PUERTO RICO": "PR", "GUAM": "GU", "U.S. VIRGIN ISLANDS": "VI",
}
US_CODES = frozenset(US_STATES.values())


def extend_bootinfo(bootinfo) -> None:
	"""``frappe.boot.ffl_us_states``: the state codes every address form's State dropdown
	offers (work order, Receive Goods, the register's trade-in), in name order."""
	bootinfo.ffl_us_states = list(US_STATES.values())


def normalize_us_state(raw) -> str | None:
	"""Canonical USPS code for a free-text state value, else None.
	Handles full names, codes, case and stray whitespace — production
	addresses already mix "TX" and "Texas"."""
	if not raw:
		return None
	value = " ".join(str(raw).split()).upper().rstrip(".")
	if value in US_CODES:
		return value
	return US_STATES.get(value)


def apply_interstate_tax(doc, method: str | None = None) -> None:
	"""Pick the right Sales Taxes and Charges Template from the shipping
	address: out-of-state US shipment → the 0% "Out of State Sales" template;
	anything else → never leave OUR 0% template on the doc (manual mis-picks
	revert to the company default). Templates other than ours are left alone —
	this engine only owns the exemption decision."""
	if int(getattr(doc, "is_return", 0) or 0) or int(getattr(doc, "is_consolidated", 0) or 0):
		return
	# A channel-mirrored invoice (a web order, a GunBroker order) carries the tax
	# that channel actually collected at checkout, as an Actual row: there is no
	# shipping Address here to judge in/out-of-state, and the destination call
	# was already made there — the exemption engine keeps its hands off.
	if is_channel_mirrored_invoice(doc):
		return
	company = getattr(doc, "company", None)
	if not company:
		return
	oos = _oos_template(company)
	if not oos:
		return  # setup hasn't created the template — engine degrades to no-op

	if _ships_out_of_state(doc):
		if doc.taxes_and_charges != oos:
			_set_template(doc, oos)
	elif doc.taxes_and_charges == oos:
		_set_template(doc, _default_template(company))


def guard_pos_exemption(doc, method: str | None = None) -> None:
	"""POS Invoice defence: in-store sales never qualify for the out-of-state
	exemption, so the 0% template on a POS Invoice without an out-of-state
	shipping address is always a mistake (or worse) — block it."""
	if int(getattr(doc, "is_return", 0) or 0):
		return
	company = getattr(doc, "company", None)
	if not company:
		return
	oos = _oos_template(company)
	if not oos or getattr(doc, "taxes_and_charges", None) != oos:
		return
	if _ships_out_of_state(doc):
		return
	frappe.throw(
		_("The '{0}' tax template only applies to orders shipped to an "
			"out-of-state address. In-store pickups are taxed — switch the "
			"template back, or set the out-of-state shipping address on the "
			"order.").format(OUT_OF_STATE_TEMPLATE_TITLE),
		title=_("Out-of-State Exemption Not Applicable"),
	)


# ------------------------------------------------------------------ internals


def _ships_out_of_state(doc) -> bool:
	addr_name = getattr(doc, "shipping_address_name", None)
	if not addr_name:
		return False
	addr = frappe.db.get_value("Address", addr_name, ["state", "country"], as_dict=True)
	if not addr or (addr.country or "") != HOME_COUNTRY:
		# Exports are out of scope for the TX exemption template; leave the
		# default tax in place and let the operator handle exports manually.
		return False
	code = normalize_us_state(addr.state)
	return bool(code) and code != home_state(getattr(doc, "company", None))


def address_fix() -> str:
	"""How to fix a Company with no usable address (a literal ``_()`` string, so the
	translation extractor finds it)."""
	return _("Set the company's address (with a US state) in Company › Addresses.")


def _read_home_state(company) -> tuple[str | None, str]:
	"""(USPS code, "") of the Company's own address, or (None, why not)."""
	from ffl_core.store_identity import company_address

	name = company_address(company) if company else None
	state = frappe.db.get_value("Address", name, "state") if name else None
	code = normalize_us_state(state)
	if code:
		return code, ""
	if name:
		return None, _("The company's address {0} has state {1!r}, which is not a US state.").format(
			name, state)
	return None, _("The company has no address of its own on file.")


def check_company_address(company) -> str | None:
	"""A pure read for settings checks: what is wrong with this Company's home
	state, as a message that says how to fix it, or None when it is fine.

	``home_state`` raises the same text at the moment a sale needs it; this lets an
	admin see it first (after_migrate -> ffl_core.setup.company_address)."""
	code, why = _read_home_state(company)
	return None if code else f"{address_fix()} {why}"


@request_cache
def home_state(company) -> str:
	"""USPS code of the state the Company's own address is in (the address its
	receipts print).

	Raises when there is none, or when its state is not a US state: guessing a
	home state would tax every shipment as if the store were somewhere it is not.
	Callers: the in/out-of-state decision on Sales Order / Sales Invoice /
	Delivery Note (``_ships_out_of_state``), ``freight_taxable``, the counter's
	tax preview (``manual_order.quote_config``) and the Sales Report's
	marketplace rows.
	"""
	code, why = _read_home_state(company)
	if code:
		return code
	frappe.throw(f"{address_fix()} {why}", frappe.ValidationError,
		title=_("Company address missing"))


def freight_taxable(company) -> bool:
	"""Whether a separately-stated shipping charge is taxed along with taxable goods
	in this Company's home state (see FREIGHT_EXEMPT_HOME_STATES)."""
	return home_state(company) not in FREIGHT_EXEMPT_HOME_STATES


def _oos_template(company: str) -> str | None:
	return frappe.db.get_value(
		"Sales Taxes and Charges Template",
		{"company": company, "title": OUT_OF_STATE_TEMPLATE_TITLE, "disabled": 0},
		"name",
	)


def _default_template(company: str) -> str | None:
	return frappe.db.get_value(
		"Sales Taxes and Charges Template",
		{"company": company, "is_default": 1, "disabled": 0},
		"name",
	)


def _set_template(doc, template: str | None) -> None:
	from erpnext.controllers.accounts_controller import get_taxes_and_charges

	doc.taxes_and_charges = template
	doc.set("taxes", get_taxes_and_charges(
		"Sales Taxes and Charges Template", template) or [])
	# validate hooks run after the controller already totalled the doc —
	# recalculate so the switched template is reflected in this save.
	doc.calculate_taxes_and_totals()
