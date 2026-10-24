"""Peace-officer excise exemption: the excise tax comes off a sale, sales tax stays.

California's firearm and ammunition excise tax (11% of gross receipts, R&TC
§36011) does not apply to a retail sale to an active or retired peace officer, or
to the law enforcement agency employing one (§36021(a)). Sales tax still applies.
The seller keeps a CDTFA-230-FET certificate for the sale (one per sale for an
officer; an agency may give a blanket one) — without it the exemption is the
store's own money.

One per-sale flag, ``ffl_le_excise_exempt``, on POS Invoice / Sales Order / Sales
Invoice, with who the buyer is and the certificate beside it. FFL Settings'
"Peace-Officer Exempt Tax Account" names the excise account; with the flag on,
every taxes row booked to that account is dropped (the template's excise row and
an excise row on freight alike) and the sales tax rows stay. It is California
law, so it is offered only to a store whose own address (the Company's, as for
the sales-tax home state) is in California: anywhere else the setting is
refused on save and ignored if present, and the flag is refused rather than saved
as a promise the invoice does not keep.

Not a customer-level switch: an officer's certificate covers one sale, and a
standing flag would exempt their next purchase without one. A resale or
government buyer exempt from ALL tax is Customer.is_tax_exempt
(compliance.customer_tax_exempt), which clears every row anyway.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from ffl_core.compliance.channel_invoice import is_channel_mirrored_invoice

FLAG = "ffl_le_excise_exempt"
BUYER = "ffl_le_exempt_buyer"
CERT = "ffl_le_exempt_certificate"
SETTING = "peace_officer_exempt_tax_account"

# R&TC §36021(a) is California's; a state is added here only on the advice of
# the store's accountant.
EXEMPT_HOME_STATES = frozenset({"CA"})


def exempt_account(company: str | None = None) -> str | None:
	"""The excise account a peace-officer sale is exempt from, or None (not offered):
	FFL Settings' account, for a store in California only."""
	from ffl_core.utils import single_raw

	account = single_raw("FFL Settings", SETTING) or None
	return account if account and in_exempt_state(company) else None


def in_exempt_state(company: str | None = None) -> bool:
	"""Whether the Company's own address is in a state with this exemption. Never
	raises (boot calls it): a store with no readable address is not offered it."""
	from ffl_core.compliance.interstate_tax import _read_home_state

	company = company or frappe.defaults.get_user_default("company") or frappe.db.get_default("company")
	code, _why = _read_home_state(company)
	return code in EXEMPT_HOME_STATES


def extend_bootinfo(bootinfo) -> None:
	"""``frappe.boot.ffl_le_exempt_account``: the register shows its LE Exempt button
	when set and drops that account's rows on the live cart (ffl_core.bundle.js)."""
	bootinfo.ffl_le_exempt_account = exempt_account() or ""


def apply_le_excise_exemption(doc, method: str | None = None) -> None:
	"""Sales Order / Sales Invoice validate, after apply_shipping_charge (which
	rebuilds the freight row this may have to drop) and before the customer's
	full exemption: drop the excise rows and total again."""
	if not _strip(doc):
		return
	# A tax-inclusive freight row holds S − Σ(tax on freight): rebuild it from the
	# rows left, as clear_tax_keep_freight does. The rebuild recalculates.
	if doc.get("ffl_shipping_tax_inclusive"):
		from ffl_core.compliance.shipping_charge import apply_shipping_charge

		if apply_shipping_charge(doc):
			return
	doc.calculate_taxes_and_totals()


def apply_le_excise_exemption_before(doc, method: str | None = None) -> None:
	"""POS Invoice before_validate: drop the excise rows before ERPNext totals the
	ticket, so its change and full-payment checks see the exempt total."""
	_strip(doc)


def _certificate_on_file(doc) -> bool:
	"""The certificate is a file kept on THIS buyer's Customer record: not a typed
	value, and not another buyer's certificate left on the sale after the customer
	was changed."""
	cert, customer = doc.get(CERT), doc.get("customer")
	return bool(cert and customer and frappe.db.exists("File", {"file_url": cert,
		"attached_to_doctype": "Customer", "attached_to_name": customer}))


def _strip(doc) -> bool:
	"""Check the paperwork and drop the excise rows; True when a row went."""
	if not cint(doc.get(FLAG)):
		return False
	if cint(doc.get("is_return")) or cint(doc.get("is_consolidated")) or is_channel_mirrored_invoice(doc):
		return False
	account = exempt_account(doc.get("company"))
	if not account:
		frappe.throw(_(
			"The peace-officer excise exemption is for California stores only, with FFL "
			"Settings › Peace-Officer Exempt Tax Account set. Untick the exemption."),
			title=_("Exemption Not Available"))
	if not doc.get(BUYER):
		frappe.throw(_("Say who the exempt buyer is: an active or retired peace officer, "
			"or the law enforcement agency employing one."), title=_("Peace-Officer Exemption"))
	if not _certificate_on_file(doc):
		frappe.throw(_("Attach the buyer's CDTFA-230-FET exemption certificate for this sale, "
			"uploaded on this buyer's own customer record. Without it the excise tax is charged."),
			title=_("Peace-Officer Exemption"))
	taxes = doc.get("taxes") or []
	keep = [t for t in taxes if t.account_head != account]
	if len(keep) == len(taxes):
		return False
	for i, row in enumerate(keep, 1):
		row.idx = i
	doc.set("taxes", keep)
	return True
