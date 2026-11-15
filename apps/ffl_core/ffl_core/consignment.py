"""Runtime helpers for consignment intake.

ensure_consignor_supplier resolves — or creates — the Supplier a consigned
firearm's payable will eventually book against. Consignors are private
parties (the Customer who dropped off the gun), but ERPNext payables run
through Supplier, so intake mints a matching Supplier the first time a given
consignor's name is seen, then reuses it on every later consignment from the
same person (matched by supplier_name — mirrors _ensure_individual_supplier
in api/receive_goods.py, which does the same for the PR's own placeholder
Supplier).

See setup/consignment.py for the schema (Serial No fields, default
commission %, Consignment Payable account) this helper's caller
(compliance/purchase.py) stamps at receive time.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate

# From this date every consigned invoice line carries its commission % frozen at
# sale, and a line that was not consigned carries none. The freeze
# (compliance.consignment.freeze_consignment_commission) shipped with the intake
# stamp and the payable JE on 2026-07-01 but froze a 0 onto the first consigned
# lines; the last one in production is a sale of 2026-07-15.
COMMISSION_FROZEN_SINCE = "2026-07-16"


def sold_on_consignment(line_pct, posting_date, stamped) -> bool:
	"""Was THIS sale of the gun a consignment sale? The line's frozen % says so
	for any sale since COMMISSION_FROZEN_SINCE. A Serial No's stamp only says who
	holds the gun NOW — a later receipt of the same gun (a trade-in, a buy-back, a
	new consignment) rewrites it — so it answers only for an older sale (the
	consignment backfill stamped those guns)."""
	if flt(line_pct) > 0:
		return True
	# lazy: a sale older than the freeze reads the gun's CURRENT stamp, so it
	# changes its answer if the gun later comes back under another owner;
	# upgrade path: freeze a flag onto those old lines once (a patch).
	if posting_date and getdate(posting_date) >= getdate(COMMISSION_FROZEN_SINCE):
		return False
	return bool(stamped)


def ensure_consignor_supplier(display_name: str, customer: str | None = None) -> str:
	"""Return the Supplier name for a consignor, creating one if needed.

	display_name is normally the seller's Customer.customer_name (already
	resolved by the caller); customer is the Customer id, used only as a
	fallback lookup when display_name is blank. Idempotent — matched by
	Supplier.supplier_name rather than assuming any particular autoname
	scheme. Raises frappe.ValidationError if no usable name can be resolved
	from either argument.
	"""
	name = (display_name or "").strip()
	if not name and customer:
		name = (frappe.db.get_value("Customer", customer, "customer_name") or "").strip()
	if not name:
		frappe.throw(_("Consignor name required to book a consignment payable."))

	existing = frappe.db.get_value("Supplier", {"supplier_name": name})
	if existing:
		return existing

	# A leaf group, or none: ERPNext 16.55 refuses a Supplier in a group-type Supplier Group.
	group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
	return frappe.get_doc({
		"doctype": "Supplier",
		"supplier_name": name,
		"supplier_group": group,
		"supplier_type": "Individual",
	}).insert(ignore_permissions=True).name


def consignor_share(net, pct: float | None) -> float:
	"""The consignor's payout on one sale line: P·(1 − pct/100).

	`net` is the line's pre-tax net amount (P); `pct` is the EFFECTIVE
	commission % (already resolved by effective_commission_pct — the store's
	cut). Pure function, no DB access: shared by the GL posting
	(compliance/consignment.post_consignment_payable) and the Sales Report
	cost formula (report/sales_report.py), which computes this exact
	number so GL COGS and reported cost reconcile.
	"""
	return flt(net) * (1 - flt(pct) / 100.0)


def effective_commission_pct(line, serial_pct: float | None) -> float:
	"""The commission % that actually applies to a sale line.

	Priority: the line's OWN consignment_commission_pct (POS Invoice Item /
	Sales Invoice Item custom field) when it's a REAL rate, else the consigned
	Serial No's consignment_commission_pct.

	A line value of 0 is treated as UNSET — identical to None/"" — NOT as an
	explicit "0% commission this sale" override. The policy is
	that a 0%-commission consignment sale does not exist (the store always keeps
	a cut), so an explicit 0% is no longer expressible: it falls back to the
	serial's rate. This also removes a latent ambiguity — the POS front end
	sends an untouched field as 0.0, and ERPNext's own round_floats_in coerces a
	truly-blank Percent field to 0.0 before any doc_event can read it, so an
	on-the-wire 0 could never be told apart from "operator left it blank";
	collapsing both to the serial rate is what an accountant audit called
	for. The freeze hook (compliance/consignment.freeze_consignment_commission)
	throws when even the serial rate resolves to <= 0, so a genuine 0 never
	reaches GL/reporting.

	Pure function: no DB access, so it's cheap to call from both the validate
	hook that freezes the value onto the line (compliance/consignment.py) and
	any later reporting/GL logic (A4/B1) that recomputes it for display.
	"""
	pct = flt(getattr(line, "consignment_commission_pct", None))
	return pct if pct else flt(serial_pct)
