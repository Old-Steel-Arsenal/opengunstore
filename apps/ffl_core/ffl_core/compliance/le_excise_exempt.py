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
an excise row on freight alike) and the sales tax rows stay. A store that names
no account (a state with no such tax) has nothing to exempt, so the flag is
refused there rather than saved as a promise the invoice does not keep.

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


def exempt_account() -> str | None:
	"""The excise account a peace-officer sale is exempt from, or None (not offered)."""
	from ffl_core.utils import single_raw

	return single_raw("FFL Settings", SETTING) or None


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


def _strip(doc) -> bool:
	"""Check the paperwork and drop the excise rows; True when a row went."""
	if not cint(doc.get(FLAG)):
		return False
	if cint(doc.get("is_return")) or cint(doc.get("is_consolidated")) or is_channel_mirrored_invoice(doc):
		return False
	account = exempt_account()
	if not account:
		frappe.throw(_(
			"This store has no excise tax a peace officer is exempt from (FFL Settings › "
			"Peace-Officer Exempt Tax Account is blank). Untick the exemption."),
			title=_("Exemption Not Set Up"))
	if not doc.get(BUYER):
		frappe.throw(_("Say who the exempt buyer is: an active or retired peace officer, "
			"or the law enforcement agency employing one."), title=_("Peace-Officer Exemption"))
	if not doc.get(CERT):
		frappe.throw(_("Attach the buyer's CDTFA-230-FET exemption certificate for this sale. "
			"Without it the excise tax is charged."), title=_("Peace-Officer Exemption"))
	taxes = doc.get("taxes") or []
	keep = [t for t in taxes if t.account_head != account]
	if len(keep) == len(taxes):
		return False
	for i, row in enumerate(keep, 1):
		row.idx = i
	doc.set("taxes", keep)
	return True
