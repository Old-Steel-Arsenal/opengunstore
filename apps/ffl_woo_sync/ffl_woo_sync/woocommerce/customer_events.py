"""Customer sync: POS → storefront marketing list.

Customers who only ever shop at the counter never reach the shop's mailing
list. When an operator turns on *Send customer emails to the shop* (WooCommerce
Settings), an hourly job posts every Customer whose record, or whose primary
Contact, changed since the last delivery to the storefront plugin:

    POST {store_url}/wp-json/opengunstore/v1/customers
    x-osa-timestamp: <unix seconds>
    x-osa-signature: hex(HMAC-SHA256(secret, "<timestamp>.<raw body>"))

    {
      "event_id": "customers:2026-11-01 08:00:00:2026-11-01 09:30:12.123456",
      "site": "retail",
      "customers": [{"customer": "CUST-0001", "email": "a@example.com",
                     "first_name": "Ann", "last_name": "Lee",
                     "modified": "2026-11-01 09:30:12.123456"}]
    }

The signature scheme, header names, timeout and secret are the drop-ship
status webhook's (:mod:`ffl_woo_sync.woocommerce.status_events`): the same
``outbound_webhook_secret`` on the store's WooCommerce Settings. The shop
upserts by email, so a batch delivered twice (the job died between the 2xx and
the watermark write) is harmless.

The watermark is the change time of the last delivered batch, kept as a
global default (``frappe.db.set_global``): database state, so it survives the
cache flush every deployment runs. **The start is the moment the box is ticked**
(:func:`on_settings_update` records it), so customers that existed before the
feature was switched on are not replayed — they are backfilled by hand — while
those added after it are not lost. (Not the settings' ``modified``: the
reconcile job's status writes bump that every two hours.)

The watermark moves only after a 2xx. A failed run stops, keeps it, and logs
the failure once: while the same batch (same ``event_id``) keeps failing the
hourly retries stay quiet, so an outage or a shop without the route yet does
not fill the Error Log. The job never raises.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import frappe
from frappe.utils import add_to_date, now_datetime
from pypika.terms import CustomFunction

from ffl_core.utils import commit
from ffl_woo_sync.woocommerce.client import RETAIL_SITE, STORES
from ffl_woo_sync.woocommerce.status_events import deliver, outbound_config

WEBHOOK_PATH = "/wp-json/opengunstore/v1/customers"
WATERMARK_KEY = "ffl_customer_events_watermark"
LAST_FAILURE_KEY = "ffl_customer_events_last_failure"
BATCH_SIZE = 200
# The most rows one request may carry: the storefront refuses a bigger batch.
MAX_BATCH_ROWS = 500
# lazy: 2,000 customers per hourly run; a bigger backlog drains over the next
# runs. Raise the cap (or the cadence) if a store ever imports customers in bulk.
MAX_BATCHES_PER_RUN = 10
# A save stamps ``modified`` when it starts, not when it commits. A change newer
# than this is left for the next run so a transaction still open at the
# watermark cannot slip under it.
SETTLE_SECONDS = 120

_GREATEST = CustomFunction("GREATEST", ["a", "b"])
_IFNULL = CustomFunction("IFNULL", ["a", "b"])
_DEDUPE_SUFFIX = re.compile(r"\s+-\s*\d+$")


# -- pure helpers ----------------------------------------------------------------


def store_host(store_url: str) -> str:
	"""The shop's host, lower-cased and without a leading ``www.``."""
	host = (urlparse(store_url if "//" in store_url else "//" + store_url).hostname or "").lower()
	return host[4:] if host.startswith("www.") else host


def clean_email(raw, shop_host: str = "") -> str:
	"""The address to send, or "" to skip the customer: blank, no ``@``, or an
	address at the shop's own domain (staff and test mail, not shoppers)."""
	email = str(raw or "").strip().lower()
	if "@" not in email:
		return ""
	if shop_host and email.rsplit("@", 1)[1] == shop_host:
		return ""
	return email


def split_name(contact_first, contact_last, customer_name) -> tuple[str, str]:
	"""``(first, last)``. A Contact with both names wins. Otherwise the whole
	name is split into its first and last words — the Contact's lone first name
	when it has one (ERPNext often stores the full name there), else the
	Customer's, minus a trailing `` - <digits>`` duplicate-name suffix."""
	first, last = str(contact_first or "").strip(), str(contact_last or "").strip()
	if first and last:
		return first, last
	full = _DEDUPE_SUFFIX.sub("", " ".join((first or str(customer_name or "")).split()))
	words = full.split()
	if not words:
		return "", last
	return words[0], last or (words[-1] if len(words) > 1 else "")


def customer_payload(row, shop_host: str = "") -> dict | None:
	"""One query row → the shop's customer entry, or None to skip it."""
	email = clean_email(row.get("contact_email") or row.get("customer_email"), shop_host)
	if not email:
		return None
	first, last = split_name(row.get("first_name"), row.get("last_name"), row.get("customer_name"))
	return {
		"customer": str(row.get("customer") or ""),
		"email": email,
		"first_name": first,
		"last_name": last,
		"modified": str(row.get("changed") or ""),
	}


def trim_tied_tail(rows: list, limit: int) -> list:
	"""A full batch may cut a run of customers sharing one change time in half;
	the watermark would then move past the half that was not sent. Drop the tail
	sharing the last time so the next run starts with it. (Never empties the
	batch: :func:`_fetch` grows the limit until a full batch is not all one time.)"""
	if len(rows) < limit:
		return rows
	last = rows[-1]["changed"]
	kept = [r for r in rows if r["changed"] != last]
	return kept or rows


# -- query -----------------------------------------------------------------------


def _settled_cutoff():
	return add_to_date(now_datetime(), seconds=-SETTLE_SECONDS)


def _fetch(watermark: str) -> list:
	"""Changed, enabled customers after ``watermark``, oldest change first.

	Change time is the later of the Customer's and its primary Contact's
	``modified``. If a full batch is all one instant (a bulk update), the limit
	doubles (up to :data:`MAX_BATCH_ROWS`) so no row is left behind the watermark."""
	customer = frappe.qb.DocType("Customer")
	contact = frappe.qb.DocType("Contact")
	changed = _GREATEST(customer.modified, _IFNULL(contact.modified, customer.modified))
	cutoff = _settled_cutoff()
	limit = BATCH_SIZE
	while True:
		rows = (
			frappe.qb.from_(customer)
			.left_join(contact).on(contact.name == customer.customer_primary_contact)
			.select(
				customer.name.as_("customer"), customer.customer_name,
				customer.email_id.as_("customer_email"), contact.email_id.as_("contact_email"),
				contact.first_name, contact.last_name, changed.as_("changed"))
			.where(customer.disabled == 0)
			.where((customer.modified > watermark) | (contact.modified > watermark))
			.where(changed <= cutoff)
			.orderby(changed).orderby(customer.name)
			.limit(limit)
		).run(as_dict=True)
		if len(rows) < limit or rows[0]["changed"] != rows[-1]["changed"]:
			return rows
		if limit >= MAX_BATCH_ROWS:
			# lazy: more than MAX_BATCH_ROWS customers stamped in one instant (a bulk
			# SQL update): send these and move on; the rest of that instant is skipped.
			# Upgrade path: page within the instant by customer name.
			return rows
		limit = min(limit * 2, MAX_BATCH_ROWS)


# -- delivery --------------------------------------------------------------------


def _report_failure(event_id: str) -> None:
	"""Log a delivery failure once per batch: the hourly retry of the same
	``event_id`` stays quiet."""
	if frappe.db.get_global(LAST_FAILURE_KEY) == event_id:
		return
	frappe.log_error(title=f"Customer sync to the shop failed: {event_id}", message=frappe.get_traceback())
	frappe.db.set_global(LAST_FAILURE_KEY, event_id)
	commit()


def _enabled_config() -> tuple[str, str] | None:
	"""``(store_url, secret)`` when the retail store opted in and has the
	outbound webhook secret set, else None."""
	try:
		if not frappe.get_single(STORES[RETAIL_SITE]["settings"]).get("send_customer_events"):
			return None
	except Exception:  # noqa: BLE001 — settings not migrated on this site
		return None
	return outbound_config(RETAIL_SITE)


def on_settings_update(doc) -> None:
	"""WooCommerce Settings saved: when *Send customer emails to the shop* goes
	from off to on and no watermark exists yet, start the sync from now."""
	before = doc.get_doc_before_save()
	if doc.get("send_customer_events") and not (before and before.get("send_customer_events")) \
			and not frappe.db.get_global(WATERMARK_KEY):
		frappe.db.set_global(WATERMARK_KEY, str(now_datetime()))


def push_customer_changes() -> dict:
	"""Scheduled (hourly): deliver customers changed since the watermark, up to
	:data:`MAX_BATCHES_PER_RUN` batches of :data:`BATCH_SIZE`. See the module
	docstring for the contract, the first-run rule and the failure handling.
	Never raises."""
	try:
		return _push()
	except Exception:  # noqa: BLE001 — a shop outage must not take the scheduler tick down
		_report_failure("customers:job")
		return {"sent": 0, "reason": "job failed"}


def _push() -> dict:
	config = _enabled_config()
	if not config:
		return {"sent": 0, "reason": "customer events not enabled"}
	store_url, secret = config
	watermark = frappe.db.get_global(WATERMARK_KEY)
	if not watermark:
		# Box set without a form save (e.g. a direct DB write): start from now.
		watermark = str(now_datetime())
		frappe.db.set_global(WATERMARK_KEY, watermark)
		commit()
	shop_host = store_host(store_url)
	sent = batches = 0
	event_id = ""
	try:
		while batches < MAX_BATCHES_PER_RUN:
			event_id = ""
			fetched = _fetch(watermark)
			rows = trim_tied_tail(fetched, BATCH_SIZE)
			if not rows:
				break
			batches += 1
			newest = str(rows[-1]["changed"])
			customers = [c for c in (customer_payload(r, shop_host) for r in rows) if c]
			if customers:
				event_id = f"customers:{watermark}:{newest}"
				deliver(store_url, secret, {"event_id": event_id, "site": RETAIL_SITE, "customers": customers},
					WEBHOOK_PATH)
				sent += len(customers)
			watermark = newest
			frappe.db.set_global(WATERMARK_KEY, watermark)
			if frappe.db.get_global(LAST_FAILURE_KEY):
				frappe.db.set_global(LAST_FAILURE_KEY, None)
			commit()
			if len(fetched) < BATCH_SIZE:
				break
	except Exception:  # noqa: BLE001 — a shop outage must not take the scheduler tick down
		_report_failure(event_id or f"customers:{watermark}")
		return {"sent": sent, "batches": batches, "reason": "delivery failed"}
	return {"sent": sent, "batches": batches}
