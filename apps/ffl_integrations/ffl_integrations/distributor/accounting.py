"""Drop-ship cost accounting — the Purchase Invoice leg.

Two documents, two jobs, and they must never overlap:

* the **Sales Invoice** (``ffl_woo_sync.woocommerce.revenue``) books the revenue,
  the freight income and the tax WooCommerce collected — and books **no COGS**,
  because ``update_stock=0`` on goods that never entered our inventory produces
  no perpetual entry;
* the **Purchase Invoice** built here books the COGS leg and the real payable to
  the distributor.

So the ledger reads::

    Dr  Cost of Goods Sold - <ABBR>              <Σ ack qty × ack cost>
    Dr  Freight and Forwarding Charges - <ABBR>  <RSR freight, when any>
        Cr  Creditors - <ABBR> [Supplier = RSR GROUP INC]

**Three clauses, all mandatory.** ``update_stock=0`` (drop-ship goods must
raise no receipt and no stock ledger entry); each item row's ``expense_account`` set
EXPLICITLY to the COGS account (an RSR-promoted Item usually has no expense or
valuation account of its own, so relying on the Item master would let the cost
drift into a valuation account); supplier = the ``Distributor.supplier``, so the
payable ages in the ordinary AP sub-ledger and reconciles against RSR's own
statement. The amount is RSR's **acknowledged** cost — never our sell price, and
never the catalog price, which moves between order and invoice.

**No automatic reversal exists in this module, by design.** When a customer's
4473 is denied after an FDS gun has already shipped, the goods are at their
transfer dealer and RSR may or may not accept them back. Reversing the PI on our
own initiative would erase a debt we still owe. The credit is booked by hand once
RSR issues one (SOP: ``docs/rsr-fds-after-sale-sop.md``). The absence of a
reversal path is asserted by
``tests/test_web_dropship_complete.py::TestNoAutomaticReversal``.

Everything the PI's shape depends on lives in :func:`build_pi_payload`, which is
pure — the ledger contract is therefore testable without a site.

**Draft only.** :func:`ensure_cost_invoice` inserts and never submits. The accountant's
sign-off gates promotion to submitted; until then a human reviews each
one against RSR's invoice.
"""
from __future__ import annotations

from ffl_core.utils import default_company  # noqa: F401 — public name; ss_provider / api import it from here
from ffl_integrations.distributor import lanes

COGS_ACCOUNT = "Cost of Goods Sold"
FREIGHT_ACCOUNT = "Freight and Forwarding Charges"



def account_name(base: str, abbr: str) -> str:
	"""ERPNext account naming: ``"<name> - <company abbr>"``. The accounts are all
	pre-existing (this design creates none)."""
	return f"{base} - {abbr}"


def billable_lines(do) -> list:
	"""The DO lines RSR has actually acknowledged.

	A killed or never-acknowledged line costs nothing; billing it would invent a
	payable. ``ack_qty`` (not ``wish_qty``) is the quantity RSR committed to.
	"""
	out = []
	for line in (do.lines or []):
		qty = int(line.get("ack_qty") or 0)
		if qty <= 0:
			continue
		out.append(line)
	return out


def build_pi_payload(do, *, supplier, company, abbr, bill_no=None,
		posting_date=None) -> dict | None:
	"""The draft Purchase Invoice for one drop-ship distributor order, or None.

	Returns None — meaning "nothing to bill" — when the order's goods enter our
	own stock (C2), or when RSR acknowledged nothing.
	"""
	# An order whose goods enter our own inventory (stocking / procure-to-store) is an
	# ordinary purchase — receipt → Purchase Receipt → bill — and a COGS invoice here
	# would count the cost twice.
	if lanes.stocks_goods(do.order_type):
		return None
	lines = billable_lines(do)
	if not lines:
		return None

	cogs = account_name(COGS_ACCOUNT, abbr)
	items = [{
		"item_code": line.get("item_code") or line.get("sku"),
		"item_name": (line.get("description") or line.get("sku") or "")[:140],
		"qty": int(line.get("ack_qty") or 0),
		"rate": float(line.get("unit_cost") or 0),
		# Clause 2 — explicit, never inherited from the Item master.
		"expense_account": cogs,
	} for line in lines]

	freight = float(do.get("freight_cost") or 0)
	if freight:
		items.append({
			"item_name": "Distributor freight",
			"qty": 1,
			"rate": freight,
			"amount": freight,
			# Kept OFF cogs so per-SKU item cost stays comparable to the DO line the
			# Sales Report reads; freight is order-level and cannot be split per SKU.
			"expense_account": account_name(FREIGHT_ACCOUNT, abbr),
		})

	remark = (f"Drop-ship cost for distributor order {do.name}"
		+ (f" (distributor invoice {bill_no})" if bill_no else ""))
	if not freight:
		# B1: a silent zero freight is indistinguishable from "no shipping was
		# charged". Say which, so whoever reconciles against RSR's invoice knows the
		# freight line's absence is a captured fact, not a data gap. Add the charge
		# by hand if the invoice shows one.
		remark += (" — note: distributor freight not captured on the order response; "
			"add a freight line manually if the invoice shows one.")

	payload = {
		"doctype": "Purchase Invoice",
		"company": company,
		"supplier": supplier,
		# Clause 1 — no Purchase Receipt, no stock ledger entry.
		"update_stock": 0,
		"set_posting_time": 1,
		"distributor_order": do.name,
		"remarks": remark,
		"items": items,
	}
	if bill_no:
		payload["bill_no"] = bill_no
	if posting_date:
		payload["posting_date"] = posting_date
	return payload


# ------------------------------------------------------------------- frappe


def ensure_cost_invoice(do_name: str, *, bill_no=None) -> dict:
	"""Idempotently create the DRAFT cost PI for a completed drop-ship order.

	Idempotency key = ``(distributor_order, bill_no)``: RSR bills a split shipment
	on several invoices, so one DO legitimately yields several PIs — but the same
	invoice number must never be booked twice.

    Never submits (see the module docstring): the accountant's sign-off gates that.
	"""
	import frappe

	do = frappe.get_doc("Distributor Order", do_name)
	supplier = frappe.db.get_value("Distributor", do.distributor, "supplier")
	if not supplier:
		frappe.log_error(
			title=f"Drop-ship cost invoice skipped: {do_name}",
			message=(f"Distributor {do.distributor!r} has no Supplier set, so the RSR "
				f"payable cannot be booked. Set Distributor.supplier and re-run."))
		return {"purchase_invoice": None, "status": "no_supplier"}

	existing = frappe.db.get_value(
		"Purchase Invoice",
		# "is not set" matches NULL too: build_pi_payload leaves bill_no unset
		# (NULL) when none is given, and a plain "" filter never matches NULL.
		{"distributor_order": do_name, "bill_no": bill_no or ("is", "not set"),
			"docstatus": ("<", 2)},
		"name")
	if existing:
		return {"purchase_invoice": existing, "status": "exists"}

	company = frappe.db.get_value("Distributor Order", do_name, "company") \
		or default_company()
	abbr = frappe.db.get_value("Company", company, "abbr")
	payload = build_pi_payload(do, supplier=supplier, company=company, abbr=abbr,
		bill_no=bill_no)
	if payload is None:
		return {"purchase_invoice": None, "status": "nothing_to_bill"}

	pi = frappe.get_doc(payload)
	pi.flags.ignore_permissions = True
	pi.insert()
	return {"purchase_invoice": pi.name, "status": "draft"}


# ------------------------------------------------------------- SRBNB re-pin


def repin_dropship_expense_accounts(doc, method=None) -> None:
	"""``Purchase Invoice: validate`` doc_event — keep the drop-ship COGS account
	Upgrade-safe: a hook, never a core edit.

	ERPNext's ``set_expense_account`` (Purchase Invoice ``validate``) forces a
	stock item's PI line to *Stock Received But Not Billed* whenever perpetual
	inventory is on — **even** with ``update_stock=0`` and an explicitly-set
	``expense_account``, and it re-derives on EVERY save including submit. A
	drop-ship PI has no Purchase Receipt behind that SRBNB balance, so the cost
	would hang there forever instead of landing on COGS.

	frappe runs a doctype's own ``validate`` before the app ``doc_events['validate']``
	handlers, so this runs AFTER the override and rewrites the goods lines back to
	COGS. Because it is in ``validate`` (not a submit-only hook) it survives the
	draft insert AND the later accountant-triggered submit — ``make_gl_entries`` reads
	``item.expense_account`` at ``on_submit``, so the ledger debit lands on COGS.

	Scoped to drop-ship PIs (``distributor_order`` set); ordinary purchasing keeps
	ERPNext's SRBNB behaviour untouched. Only lines currently on SRBNB are moved —
	the freight line (a non-stock service on the Freight account, which ERPNext
	never overrode) is left alone. If the COGS account is absent (a bare site), the
	line is left on SRBNB rather than pinned to a non-existent account.
	"""
	import frappe

	if not doc.get("distributor_order"):
		return
	abbr = frappe.db.get_value("Company", doc.company, "abbr")
	if not abbr:
		return
	srbnb = f"Stock Received But Not Billed - {abbr}"
	cogs = account_name(COGS_ACCOUNT, abbr)
	if not frappe.db.exists("Account", cogs):
		return
	for item in (doc.get("items") or []):
		if (item.get("expense_account") or "") == srbnb:
			item.expense_account = cogs
