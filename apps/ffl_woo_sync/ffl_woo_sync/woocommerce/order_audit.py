"""Daily WooCommerce order-integrity audit (D2) — the swallowed-order backstop.

A webhook handler exception rolls back and STILL returns 200 (webhook.py's own
note: "we ALWAYS return 200 after HMAC passes"), so WooCommerce never retries and
the order simply never books — only an Error Log marks it. This scheduled audit
is the safety net: it pulls the last N days of WooCommerce orders and Error-Logs
any that SHOULD have a Woo Online Order in ERP but don't. Alert only — it never
auto-creates (a missing order may need serials / FFL resolution a human drives).

Gated + throttled exactly like ``tasks.scheduled_reconcile``: one cron entry, a
per-site ``auto_sync_enabled`` check, and a daily ``_due()`` throttle on
``last_order_audit_at``.
"""
from __future__ import annotations

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from ffl_core.utils import commit as safe_commit
from ffl_woo_sync.woocommerce.tasks import (
	_SHIPPED_UNDISPOSED_GRACE_HOURS,
	_SHIPPED_UNDISPOSED_SQL,
	_due,
)
from ffl_woo_sync.woocommerce.webhook import _RELEASE_STATUSES

_AUDIT_LOOKBACK_DAYS = 10
# Daily-ish, mirroring tasks._RECONCILE_MIN_INTERVAL["Daily"] (allows daily drift).
_AUDIT_MIN_INTERVAL = 20 * 3600
_MAX_PAGES = 20  # safety cap: 20 × 100 = 2000 orders / site / run


def _should_exist(order: dict) -> bool:
	"""True when ERP should carry this Woo order — the SAME predicate the webhook
	uses to CREATE (vs release): any status not in the release set (a placed order
	— pending / processing / on-hold / completed — gets a Woo Online Order)."""
	status = (str(order.get("status") or "")).strip().lower()
	return status not in _RELEASE_STATUSES


def _missing_orders(woo_orders: list[dict], existing_ids) -> list[dict]:
	"""Live Woo orders whose id has no Woo Online Order in ERP. Ids compared as
	strings (ERP stores woo_order_id as Data; Woo sends int)."""
	existing = {str(i) for i in (existing_ids or [])}
	return [
		o for o in woo_orders
		if _should_exist(o) and str(o.get("id") or "") not in existing
	]


def _audit_site(site: str) -> bool:
	"""Pull recent WC orders for one site and Error-Log every live order missing
	from ERP. Returns True when the audit ran to completion (caller may stamp the
	throttle); False when it could not fetch (config or API error) so the caller
	does NOT advance the throttle and next cron retries. Never writes to Woo or ERP
	beyond the log."""
	from ffl_woo_sync.woocommerce.client import (
		WooCommerceClient, WooCommerceConfigError, WooCommerceError)

	try:
		client = WooCommerceClient.from_settings(site)
	except WooCommerceConfigError:
		return False  # misconfigured — nothing to audit; don't stamp, retry later

	after = get_datetime(
		add_to_date(now_datetime(), days=-_AUDIT_LOOKBACK_DAYS)).isoformat()
	woo_orders: list[dict] = []
	try:
		for page in range(1, _MAX_PAGES + 1):
			batch = client.list_orders(after=after, status="any", per_page=100, page=page)
			if not batch:
				break
			woo_orders.extend(batch)
			if len(batch) < 100:
				break
	except WooCommerceError as exc:
		# The swallowed-order backstop must not itself swallow: a failed fetch is
		# NOT "zero missing orders". Log and DON'T stamp — retry next cron.
		frappe.log_error(
			title=f"Woo order audit fetch failed ({site})",
			message=(
				f"WooCommerce order pull failed: {exc}. Audit skipped this run; the "
				"throttle was NOT advanced so the next cron retries."))
		return False

	existing = frappe.get_all(
		"Woo Online Order", filters={"site": site}, pluck="woo_order_id")
	for o in _missing_orders(woo_orders, existing):
		frappe.log_error(
			title=f"Woo order {o.get('id')} ({site}) missing from ERP",
			message=(
				f"WooCommerce order #{o.get('number') or o.get('id')} "
				f"(status='{o.get('status')}', total={o.get('total')}) has NO Woo "
				"Online Order in ERP — the webhook likely swallowed it. NOT auto-"
				"created (it may need serials / FFL resolution). Recover manually: "
				"re-fire the order from WooCommerce (Edit Order → Update), or build "
				"+ dispose it in the POS."),
		)
	return True


def _gate_and_audit(site: str) -> None:
	"""Per-site gate: audit only when the site's WooCommerce is auto-sync enabled
	and the daily throttle has elapsed; stamp ``last_order_audit_at`` after."""
	from ffl_woo_sync.woocommerce.client import store_entry

	settings_dt = store_entry(site)["settings"]
	s = frappe.get_single(settings_dt)
	if not int(getattr(s, "auto_sync_enabled", 0) or 0):
		return
	if not _due(getattr(s, "last_order_audit_at", None), _AUDIT_MIN_INTERVAL):
		return
	# Stamp the throttle ONLY when the audit actually completed a fetch — a config
	# or API failure (returns False) must not advance the daily clock, so the next
	# cron retries instead of waiting a day. db.set_value (not .save()) bypasses the
	# track_changes audit log — mirrors tasks._run_reconcile's status writes.
	if _audit_site(site):
		frappe.db.set_value(
			settings_dt, settings_dt, {"last_order_audit_at": now_datetime()})


def scheduled_order_audit() -> None:
	"""Cron entry (shares the 2-hourly reconcile cron; self-throttles to daily)."""
	_gate_and_audit("retail")


# ---------------------------------------------------------------------------
# Web revenue / stock backstop (2026-08-26)
# ---------------------------------------------------------------------------
# The accessory-only auto-clear books its revenue from a job enqueued by the
# Guest webhook; for four days that job failed on a permission check and NOTHING
# retried it — the order sat Transferred with no Sales Invoice, no payment, and
# (the stock leg not existing yet) four units on the shelf that were two. The
# audit above only asks "is the Woo order in ERP?", so it saw nothing. These two
# sweeps close that gap: a Transferred order that never got an invoice is booked
# again through its own completion job (idempotent, posted on the order's
# Transferred date), and a web invoice whose accessory rows still have no
# Delivery Note gets its stock leg. Anything a human has already touched — an
# invoice that exists but was cancelled, a DN row that is a draft / cancelled /
# a return — is alerted, never re-booked: "cancelled on purpose" and "never
# booked" are indistinguishable to a query, and re-booking behind a person is
# how revenue or stock gets counted twice. Bounded, never touches WooCommerce,
# and every hit is Error-Logged so a miss of the primary path stays visible.

_BACKSTOP_SETTLE_HOURS = 2
_BACKSTOP_LOOKBACK_DAYS = 30
_BACKSTOP_BATCH = 25
_BACKSTOP_BATCH_MAX = 500  # ceiling for the read-only month-end listing


def _transferred_without_invoice(limit: int = _BACKSTOP_BATCH) -> list[str]:
	"""Transferred web orders that owe a submitted Sales Invoice.

	Both bounds sit on ``COALESCE(transferred_at, creation)`` — the moment the
	order LEFT, not the moment it was placed. Placement is the wrong clock in
	both directions: an order that waited weeks for an FFL or a distributor
	restock would fall out of the 30-day floor before it was ever fulfilled (a
	permanent blind spot, which is the one thing this sweep exists to prevent),
	and the settle window is meant to give the primary completion job its two
	hours after the transfer, not after checkout. Orders from before the stamp
	existed fall back to ``creation``, i.e. the previous behaviour.

	A DRAFT invoice does not count as "has an invoice": the draft-total-mismatch
	path leaves exactly that, and the booking entry point re-verifies and submits
	a dangling draft, so leaving those in the candidate set is what retries them.
	Orders whose only invoice was CANCELLED come back too — the caller tells the
	two apart and alerts instead of re-booking a human's decision."""
	now = now_datetime()
	return frappe.db.sql_list(
		"""
		SELECT o.name
		FROM `tabWoo Online Order` o
		WHERE o.order_status = 'Transferred'
		  AND COALESCE(o.transferred_at, o.creation) < %(cutoff)s
		  AND COALESCE(o.transferred_at, o.creation) >= %(floor)s
		  AND NOT EXISTS (
		    SELECT 1 FROM `tabSales Invoice` si
		    WHERE si.woo_online_order = o.name AND si.docstatus = 1
		  )
		ORDER BY COALESCE(o.transferred_at, o.creation)
		LIMIT %(limit)s
		""",
		{
			"cutoff": add_to_date(now, hours=-_BACKSTOP_SETTLE_HOURS),
			"floor": add_to_date(now, days=-_BACKSTOP_LOOKBACK_DAYS),
			"limit": int(limit),
		},
	)


def _invoiced_without_delivery(limit: int = _BACKSTOP_BATCH, *,
		from_date=None, to_date=None) -> list[str]:
	"""Submitted web Sales Invoices (``update_stock=0``) with at least one
	accessory row — not drop-shipped, in our warehouse, a stock item, not a
	firearm — whose submitted, non-return Delivery Note rows add up to less than
	the invoiced qty, i.e. stock that left the shelf with nothing to say so.

	The same owed-quantity rule as ``revenue._web_stock_rows`` +
	``_stock_leg_state``, in SQL (the firearm test is literally the shared
	``firearm_item_sql`` fragment), and pinned to it by
	``TestWebStockRuleAgreement``. Rows a person is already holding — any DRAFT,
	CANCELLED or RETURN Delivery Note row against them — MUST remain in this
	control: Python will alert instead of re-issuing them, and month-end must not
	declare COGS complete while they still owe stock. To keep those persistent
	rows from starving the capped backstop, invoices with at least one actionable
	owed row sort first; among the rest, rows without an open alert sort first.

	``from_date`` / ``to_date`` bound ``posting_date`` for the month-end control
	(month-end check: call it for the month and expect zero rows); the
	default is the rolling ``_BACKSTOP_LOOKBACK_DAYS`` window the cron uses."""
	from ffl_core.firearm import ALL_FIREARM_GROUPS, firearm_item_sql

	return frappe.db.sql_list(
		"""
		SELECT si.name
		FROM `tabSales Invoice` si
		INNER JOIN `tabSales Invoice Item` r ON r.parent = si.name
		INNER JOIN `tabItem` it ON it.name = r.item_code
		WHERE si.docstatus = 1
		  AND COALESCE(si.woo_online_order, '') <> ''
		  AND si.update_stock = 0
		  AND si.posting_date >= %(floor)s
		  AND (%(ceiling)s IS NULL OR si.posting_date <= %(ceiling)s)
		  AND COALESCE(r.delivered_by_supplier, 0) = 0
		  AND COALESCE(r.warehouse, '') <> ''
		  AND r.qty > 0
		  AND it.is_stock_item = 1
		  AND NOT {firearm}
		  AND r.qty > COALESCE((
		    SELECT SUM(d.qty)
		    FROM `tabDelivery Note Item` d
		    INNER JOIN `tabDelivery Note` dn ON dn.name = d.parent
		    WHERE d.si_detail = r.name AND d.docstatus = 1 AND dn.is_return = 0
		  ), 0)
		GROUP BY si.name, si.posting_date
		ORDER BY
		  MIN(CASE WHEN EXISTS (
		    -- 0 when at least one owed row can be repaired automatically; 1 when
		    -- every owed row is held by a person's draft/cancellation/return.
		    SELECT 1
		    FROM `tabDelivery Note Item` d2
		    INNER JOIN `tabDelivery Note` dn2 ON dn2.name = d2.parent
		    WHERE d2.si_detail = r.name
		      AND (d2.docstatus <> 1 OR dn2.is_return = 1)
		  ) THEN 1 ELSE 0 END),
		  CASE WHEN EXISTS (
		    SELECT 1 FROM `tabToDo` td
		    WHERE td.reference_type = 'Sales Invoice'
		      AND td.reference_name = si.name AND td.status = 'Open'
		  ) THEN 1 ELSE 0 END,
		  si.posting_date, si.name
		LIMIT %(limit)s
		""".format(firearm=firearm_item_sql("it")),
		{
			"floor": from_date or add_to_date(
				now_datetime(), days=-_BACKSTOP_LOOKBACK_DAYS),
			"ceiling": to_date,
			"firearm_groups": tuple(ALL_FIREARM_GROUPS),
			"limit": int(limit),
		},
	)


def _alert_cancelled_invoice(order_name: str, cancelled: str) -> None:
	from ffl_core.alerts import alert_once

	alert_once(
		"Woo Online Order", order_name,
		title=f"Web order {order_name} has only a cancelled invoice",
		message=(
			f"{order_name} is Transferred and its Sales Invoice {cancelled} is "
			"cancelled with no live amendment. Not re-booked automatically — a "
			"cancellation is a decision. Amend or rebuild it by hand "
			"(create_web_invoice_now once the cancelled one is amended/deleted)."),
		description=(
			f"Web order {order_name}: invoice {cancelled} is cancelled and nothing "
			"replaced it. Decide: amend it, or run create_web_invoice_now."),
	)


def scheduled_web_revenue_backstop() -> dict:
	"""Cron entry (rides the 2-hourly tick; no daily throttle — both queries are
	cheap, every hit is an incident, and the alerts dedupe themselves).
	Returns counts for the log / a manual run."""
	from ffl_core.alerts import alert_once
	from ffl_woo_sync.woocommerce.revenue import (
		book_web_order_completion, deliver_web_stock_lines)

	booked, delivered, failed, held = [], [], [], []
	for name in _transferred_without_invoice():
		# A dangling DRAFT is retried (the booking re-verifies and submits it); a
		# CANCELLED invoice with nothing live behind it is a decision, and gets a
		# person, not a second posting.
		draft = frappe.db.get_value(
			"Sales Invoice", {"woo_online_order": name, "docstatus": 0}, "name")
		cancelled = None if draft else frappe.db.get_value(
			"Sales Invoice", {"woo_online_order": name, "docstatus": 2}, "name")
		if cancelled:
			_alert_cancelled_invoice(name, cancelled)
			safe_commit()
			held.append(name)
			continue
		try:
			result = book_web_order_completion(name)
			safe_commit()
			status = (result.get("revenue") or result).get("status") if isinstance(result, dict) else None
			if status == "created":
				booked.append(name)
				frappe.log_error(
					title=f"Web revenue backstop booked {name}",
					message=(
						f"{name} was Transferred with no Sales Invoice — its completion "
						f"job never landed (see the Error Log around the order's "
						f"Transferred time). Booked now: {result}"),
				)
		except Exception:
			frappe.db.rollback()
			failed.append(name)
			alert_once(
				"Woo Online Order", name,
				title=f"Web revenue backstop failed: {name}",
				message=frappe.get_traceback(),
				description=(
					f"Web order {name} is Transferred but its revenue booking keeps "
					"failing (see Error Log). Fix the cause, then run "
					"create_web_invoice_now."),
			)
			safe_commit()
	for si_name in _invoiced_without_delivery():
		si = frappe.get_doc("Sales Invoice", si_name)
		# deliver_web_stock_lines never raises: a shelf that disagrees, or a row a
		# human already touched, is alerted (once) and the row stays a candidate.
		created = deliver_web_stock_lines(si)
		safe_commit()
		if created:
			delivered.append(si_name)
			frappe.log_error(
				title=f"Web stock backstop delivered {si_name}",
				message=(
					f"{si_name} (web order {si.woo_online_order}) carried accessory rows "
					f"with no Delivery Note — stock and COGS were never booked at "
					f"invoicing. Issued now: {created}"),
			)
	refunds_read, refunds_pending = _sweep_pending_refund_breakdowns()
	return {"booked": booked, "delivered": delivered, "failed": failed, "held": held,
		"refund_breakdowns": refunds_read, "refund_breakdowns_pending": refunds_pending}


# A Pending refund younger than this is still the webhook's own reconcile job's
# to read; older means that job failed or WooCommerce was down — retry here.
PENDING_REFUND_MINUTES = 30


def _sweep_pending_refund_breakdowns() -> tuple[list[str], list[str]]:
	"""Re-run the refund reconcile for every web order whose refund breakdown is
	still unread after PENDING_REFUND_MINUTES — the retry for an order WooCommerce
	sends no further webhook for, or whose webhook job was deduplicated away. The
	reconcile reads the breakdown and re-amends the invoice. Returns (orders now
	fully read, orders still Pending); a failure is its own alert, never a stop."""
	from ffl_core.alerts import alert_once
	from ffl_woo_sync.woocommerce.refunds import _reconcile, has_pending_breakdown

	cutoff = add_to_date(now_datetime(), minutes=-PENDING_REFUND_MINUTES)
	names = sorted(set(frappe.get_all("Woo Online Order Refund",
		filters={"parenttype": "Woo Online Order", "breakdown_status": "Pending",
			"recorded_on": ("<", cutoff)},
		pluck="parent")))
	read, pending = [], []
	for name in names:
		try:
			_reconcile(name)
			safe_commit()
		except Exception:
			frappe.db.rollback()
			alert_once(
				"Woo Online Order", name, dedupe_on_title=True,
				title=f"Web refund reconcile failed: {name}",
				message=frappe.get_traceback(),
				description=(
					f"Web order {name}: its refund reconcile keeps failing (see Error "
					"Log). Fix the cause, then run reconcile_web_order_refund."))
			safe_commit()
		if has_pending_breakdown(frappe.get_doc("Woo Online Order", name)):
			pending.append(name)
		else:
			read.append(name)
	return read, pending


@frappe.whitelist()
def web_stock_legs_outstanding(from_date=None, to_date=None) -> dict:
	"""List web invoices whose accessory stock never left — the month-end control.

	Read-only. Before closing a month, call this for that month (first to last
	day) and expect an empty list: a web invoice whose accessory Delivery Note
	never posted still shows a cost in the Sales Report (its cost column is
	qty x Bin valuation rate, not the GL), so the report cannot reveal the gap
	and its cost can only be trusted against GL COGS once this is empty. A
	non-zero count lists the invoices: run ``run_web_revenue_backstop_now`` to
	issue the ones that can be issued, handle the rest by hand. Also before
	closing: no open ToDo on a Sales Invoice or Woo Online Order from this
	machinery (a stock leg that could not be issued, a revenue booking that
	keeps failing, an order whose only invoice is cancelled, a refund held
	because a person is holding the stock leg, an invoice left in Draft on a
	total mismatch) — each is an unbooked sale or an overstated shelf. A
	backfilled Delivery Note is posted on the invoice's own date while the
	period is open (no Period Closing Voucher, no accounts/stock freeze, month
	not yet handed to an outside accountant), otherwise on the correction date
	with a comment saying so. After a submitted sales return against a web
	Delivery Note, expect report cost to exceed GL COGS by the returned amount.

	Args:
	    from_date: First posting date to consider (default: 30 days ago).
	    to_date: Last posting date to consider (default: no upper bound).

	Returns:
	    ``{"invoices": [...], "count": n}`` — submitted web Sales Invoices still
	    owing a Delivery Note in the window.

	Permissions:
	    Counter roles (same as the other web-order accounting actions).
	"""
	from ffl_core.api.manual_order import _ROLES

	frappe.only_for(_ROLES)
	names = _invoiced_without_delivery(
		limit=_BACKSTOP_BATCH_MAX, from_date=from_date, to_date=to_date)
	return {"invoices": names, "count": len(names)}


@frappe.whitelist()
def shipped_undisposed_orders() -> list[dict]:
	"""List web orders WooCommerce says shipped while POS still has them open.

	Read-only: the same query (and grace period) as the daily
	``tasks.alert_shipped_undisposed_orders`` alarm, without its Error Log, so
	the list and the alarm cannot disagree. Each one is a firearm that may have
	left with no disposition booked: confirm the serial against ShipStation and
	FastBound, then dispose the order in POS.

	Args:
	    (none)

	Returns:
	    ``[{"name", "woo_order_id", "customer_name", "modified"}, ...]``, oldest
	    first; empty when nothing is outstanding.
	"""
	from ffl_core.api.manual_order import _ROLES

	frappe.only_for(_ROLES)
	return frappe.db.sql(_SHIPPED_UNDISPOSED_SQL,
		{"grace": _SHIPPED_UNDISPOSED_GRACE_HOURS}, as_dict=True)


@frappe.whitelist()
def run_web_revenue_backstop_now() -> dict:
	"""Run the web revenue / stock backstop sweep by hand.

	Same sweep the 2-hourly cron runs (``scheduled_web_revenue_backstop``):
	books the revenue of Transferred web orders that never got an invoice and
	issues the missing accessory stock legs, alerting instead of acting on
	anything a person already touched.

	Args:
	    (none)

	Returns:
	    ``{"booked": [...], "delivered": [...], "failed": [...], "held": [...]}``
	    — order / invoice names per outcome.

	Permissions:
	    Counter roles (same as the other web-order accounting actions).
	"""
	from ffl_core.api.manual_order import _ROLES

	frappe.only_for(_ROLES)
	return scheduled_web_revenue_backstop()
