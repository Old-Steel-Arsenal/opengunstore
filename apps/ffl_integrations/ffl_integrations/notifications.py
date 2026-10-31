"""Operational alert emails (sync failures, stuck records).

First place in the codebase that sends mail to staff. Recipients come from
FFL Settings.alert_recipients (comma/newline separated), falling back to all
enabled System Managers so an alert is never silently dropped. Sends are
throttled per dedup_key so a sync that fails every 15 min can't flood inboxes.

Also home to notify_new_order, the counter's in-app bell for a new web / GunBroker order.
"""
from __future__ import annotations

import frappe

# One recipients rule and one never-raising logger for all three apps; re-exported
# here because other modules import them from this module and tests patch these names.
from ffl_core.alerts import alert_recipients as _recipients
from ffl_core.alerts import safe_log_error

_THROTTLE_PREFIX = "ffl_alert_sent:"

# Back-compat alias: this helper predates its promotion to a shared utility and
# is imported under the old name by tests.
_safe_log_error = safe_log_error


def notify_admins(subject: str, message: str, *, dedup_key: str,
		window_minutes: int = 60) -> bool:
	"""Queue an operational-alert email. Throttled per dedup_key. Returns True if
	queued, False if suppressed (throttled / no recipients). Never raises —
	alerting must not crash a scheduler tick or webhook."""
	try:
		cache = frappe.cache()
		ckey = _THROTTLE_PREFIX + dedup_key
		if cache.get_value(ckey):
			return False
		recipients = _recipients()
		if not recipients:
			return False
		frappe.sendmail(
			recipients=recipients,
			subject=f"[POS Alert] {subject}",
			message=message,
			now=False,
		)
		cache.set_value(ckey, "1", expires_in_sec=window_minutes * 60)
		return True
	except Exception:
		safe_log_error("notify_admins failed", frappe.get_traceback())
		return False


# A back-fill (GunBroker Settings > Orders Since Override) re-reads orders the shop
# handled long ago; those are not news.
_NEW_ORDER_MAX_AGE_DAYS = 7


# A web order awaiting payment is not a sale yet; it rings when WooCommerce moves it on.
_PAID_WOO_STATUSES = ("processing", "on-hold", "completed")


def notify_new_order(doc, method=None) -> None:
	"""``after_insert`` of Woo Online Order / GunBroker Order: ring the counter's bell.
	Also ``on_update`` of Woo Online Order, for an order created while still awaiting
	payment ("pending") that is now paid. Never raises — it runs inside the webhook /
	poll that writes the order."""
	from frappe.utils import add_days, flt, get_datetime, now_datetime

	from ffl_core.alerts import notify_staff
	from ffl_core.roles import COUNTER_ROLES

	try:
		if doc.doctype == "GunBroker Order":
			if doc.cancel_date or (doc.order_date and get_datetime(doc.order_date)
					< add_days(now_datetime(), -_NEW_ORDER_MAX_AGE_DAYS)):
				return
			channel, ref, buyer = "GunBroker", doc.gb_order_id, doc.customer_name or doc.buyer_username
			match = {"gb_order_id": ref}
		else:
			status = (doc.woo_status or "").lower()
			if method == "on_update":
				before = doc.get_doc_before_save()
				if not before or (before.woo_status or "").lower() != "pending" \
						or status not in _PAID_WOO_STATUSES:
					return
			# An order.updated for an order POS never had creates it too — a WC edit
			# to an old, finished order is not a new sale.
			elif status in ("pending", "completed"):
				return
			channel = "web"
			ref, buyer = doc.woo_order_id, doc.customer_name
			match = {"woo_order_id": ref, "site": doc.site}
		notify_staff(doc.doctype, doc.name,
			f"New {channel} order #{ref} — {buyer or 'no name'} · ${flt(doc.order_total):,.2f}",
			COUNTER_ROLES, match=match)
	except Exception:
		safe_log_error(f"New-order notification failed: {doc.name}", frappe.get_traceback())
