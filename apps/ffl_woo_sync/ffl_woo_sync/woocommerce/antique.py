"""Retirement of antique (GCA-exempt) firearm units sold on a web order.

An antique — a pre-1899 receiver — is a firearm for INVENTORY purposes and not
for ATF purposes. That split is the whole difficulty:

  * ATF side: no bound-book entry, no 4473, no FFL to ship to. So an antique
    order never passes through ``dispose_web_order``, and must not: booking an
    FFL Disposition for one would write a gun into the A&D book that does not
    exist in ATF's eyes.
  * Inventory side: it is still one serialized rifle on our shelf, with a
    valuation, a WooCommerce listing and a reservation.

Everything that retires a normal web firearm hangs off the disposition:
``FFL Disposition.on_submit`` issues the Material Issue that takes the gun out
of stock (``ffl_core.compliance.disposition_stock``), and ``_delist_disposed``
retires the listing from the list of serials that disposition produced. An
antique reaches ``Transferred`` through ``_auto_clear_non_firearm_if_completed``
instead, which does neither — so before this module existed an antique sale left
the gun in stock, the listing on sale, and (because a Sales Invoice refuses an
``is_firearm`` line with no serial) booked no revenue at all.

This module is that missing leg. It deliberately reuses the disposition path's
own helpers rather than reimplementing them, so an antique retires through the
same code — and lands in the same accounts — as every other web firearm sale.

**The three steps are ORDER-DEPENDENT.** Stock must leave before the listing is
retired: ``sync.route_serial_exit`` sends a serial to the SOLD archive only when
its status is in ``SOLD_STATUS`` (``Delivered``/``Consumed``), and the Material
Issue is what makes it ``Consumed``. Retire the listing first and the gun is
still ``Active``, so it routes to a plain delist (a 404 page instead of the SOLD
page that keeps its rankings) — and the 2-hourly reconcile sweep, seeing a
still-active serial against a stock-0 listing, reads it as drift and pushes the
sold gun back on sale.
"""

from __future__ import annotations

import frappe
from frappe import _

from ffl_core.firearm import is_antique_item, issue_antique_serial
from ffl_core.utils import guest_job_elevated


def _issue_antique_units(order) -> list[tuple[str, str]]:
	"""Material-Issue every locally-held antique unit on ``order`` out of stock.

	Returns the ``(item_code, serial)`` pairs actually taken out, in the shape
	``_delist_disposed`` expects.

	Guards mirror ``disposition_stock.issue_stock_on_submit`` one for one, which is
	also what makes this idempotent: a serial that is no longer ``Active`` with a
	warehouse has already left, so a retried job is a no-op rather than a second
	issue. A distributor-held unit is skipped — there is no gun of ours to issue.
	"""
	from ffl_woo_sync.woocommerce.fulfillment import _LOCALLY_DISPOSED_LANES

	issued: list[tuple[str, str]] = []
	for sr in (order.serials or []):
		if not is_antique_item(sr.item_code):
			continue
		if (sr.get("fulfillment") or "local") not in _LOCALLY_DISPOSED_LANES:
			continue
		serial = (sr.confirmed_serial or "").strip()
		if not serial:
			# No serial means the reservation never resolved. Nothing to issue, and
			# nothing to invent: leave it for a human. The order stays visible via
			# the awaiting-serial path rather than being silently retired.
			continue
		# The canonical antique exit: it re-checks the SERIAL's own Item and throws
		# on a regulated gun (that one owes an FFL Disposition), and returns None
		# for a gun already out of stock — a retry, or staff issued it by hand.
		issue_antique_serial(
			serial,
			remarks=(
				f"Antique unit sold on web order {order.name} "
				f"(Woo #{order.woo_order_id}). GCA-exempt: no FFL Disposition exists, "
				"so stock is issued here instead of by disposition_stock."),
		)
		sn = frappe.db.get_value("Serial No", serial, ["item_code"], as_dict=True)
		if not sn:
			continue
		# Issued now or already out: either way hand it to the listing step, whose
		# page may not have been retired yet.
		issued.append((sn.item_code, serial))
	return issued


@guest_job_elevated()
def retire_antique_web_order(order) -> dict:
	"""Finish an antique-carrying web order that auto-cleared to ``Transferred``:
	issue the stock, retire the listing, then book the revenue — in that order.

	Deliberately NOT whitelisted. It is reached two ways, neither of which needs it:
	``frappe.enqueue`` resolves the dotted path directly, and a human uses the
	role-gated ``retire_antique_web_order_now`` below. Exposing this one would let any
	authenticated session — a dealer-portal login included — move stock and cut an
	invoice. Same split ``revenue.py`` uses for
	``create_sales_invoice_for_web_order`` / ``create_web_invoice_now``.

	Idempotent and resumable, because each step already is: the stock issue is
	guarded on the serial still being on hand, listing retirement is a WooCommerce
	upsert, and ``create_sales_invoice_for_web_order`` tops up or reuses an existing
	invoice. A failure part-way is safe to re-run.

	Runs elevated when reached from the Guest webhook context — it moves stock
	and cuts an invoice, both of which ERPNext permission-checks (see
	:func:`ffl_core.utils.guest_job_elevated`).

	Revenue goes LAST on purpose. The Sales Invoice is the step most likely to
	throw (totals mirroring, tax accounts, an unpriced item), and if it ran first a
	throw would roll back the stock issue with the gun already shipped — the state
	that once made a gun double-sellable. Ordered this way, the worst case is an
	un-invoiced order whose gun is correctly out of stock and off the shelf, which
	is recoverable by re-running and cannot oversell.
	"""
	if isinstance(order, str):
		order = frappe.get_doc("Woo Online Order", order)
	if (order.order_status or "") != "Transferred":
		# Same refusal shape as create_sales_invoice_for_web_order: this runs off the
		# auto-clear event, so a non-Transferred order means the caller is confused.
		frappe.throw(_(
			"{0} is not Transferred — nothing to retire."
		).format(order.name))

	issued = _issue_antique_units(order)
	# The gun has left; the soft reservation that held its WooCommerce stock at 0 is
	# moot. Clear it for the same reason dispose_web_order does: a sold serial still
	# flagged woo_reserved shows up as a live hold in the reserved-serials view.
	for _item_code, serial in issued:
		frappe.db.set_value("Serial No", serial,
			{"woo_reserved": 0, "woo_order": ""}, update_modified=False)
	if issued:
		# Reuse the disposition path's own retirement so an antique's page gets the
		# SAME treatment as any other sold gun (SOLD archive at the realized price,
		# or a delist where that is the routed answer) instead of a second, subtly
		# different implementation drifting alongside it.
		from ffl_woo_sync.woocommerce.fulfillment import _delist_disposed, _sold_prices

		_delist_disposed(issued, order.site, _sold_prices(order))

	from ffl_woo_sync.woocommerce.revenue import create_sales_invoice_for_web_order

	revenue = create_sales_invoice_for_web_order(order)
	return {"issued": issued, "revenue": revenue}


@frappe.whitelist()
def retire_antique_web_order_now(woo_online_order: str) -> dict:
	"""Manually retry retiring an antique web order (no bound-book entry).

	For an antique web order whose retirement failed (Error Log).

	Args:
	    woo_online_order: Woo Online Order name (antique lines only).

	Returns:
	    The retirement result from retire_antique_web_order.

	Permissions:
	    Counter roles (same as the other fulfillment actions).
	"""
	from ffl_core.api.manual_order import _ROLES

	frappe.only_for(_ROLES)
	return retire_antique_web_order(woo_online_order)
