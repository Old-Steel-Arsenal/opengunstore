"""WooCommerce → POS inbound webhook receiver.

Configure in WooCommerce:
  WooCommerce → Settings → Advanced → Webhooks → Add webhook:
    Topic:        Order created   (and a second for Order updated)
    Delivery URL: https://<your-pos-host>/api/method/ffl_woo_sync.woocommerce.webhook.woo_event
    Secret:       <WooCommerce Settings.webhook_secret>

WooCommerce sends:
  Header X-WC-Webhook-Signature: base64(HMAC-SHA256(raw_body, secret))
  Header X-WC-Webhook-Topic:     "order.created" | "order.updated" | …
  Body: WooCommerce Order object (JSON)

WC retries non-2xx up to ~5 times over 5 days and disables webhooks after
repeated failures — we ALWAYS return 200 after HMAC passes (even on handler
errors, after logging) to prevent retry storms / auto-disable.

Loop guard: frappe.flags.woo_sync_inbound is set to True for the entire
handler body so T10's doc_events don't echo stock changes back to WooCommerce.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import traceback as _tb
from collections import defaultdict
from contextlib import contextmanager
from typing import Any

import frappe
from frappe.utils import flt

from ffl_core.alerts import safe_log_error
from ffl_core.compliance.ffl_number import FFL_NUMBER_RE, is_cr_license
from ffl_core.firearm import is_antique_item, is_firearm_item, is_regulated_firearm_item
from ffl_core.reservations import (
	CHANNEL_WOO,
	channel_reservation,
	channel_reservations,
	channel_reserve_columns,
	channel_reserved_serials,
	lock_item_reservations,
	notify_channel_commitments_changed,
)
from ffl_core.warehouses import custody_warehouse_names

from ffl_woo_sync.woocommerce.cr_transfer import _clear_cr_verification

from ffl_woo_sync.woocommerce.client import resolve_site, store_entry
from ffl_woo_sync.woocommerce.sync import _f


# The ?site= query param names the store a webhook comes from. Only the stores in
# woocommerce/client.py's registry are accepted (retail, the only one); a webhook
# naming any other is refused with a 400 before anything is read or written (see
# woo_event).

# Public WooCommerce order-item meta key carrying dealer-entered firearm serials
# (was the protected "_ffl_core_serials"; renamed to a public key so the REST API,
# the storefront, and other plugins can read it).
_SERIAL_META_KEY = "firearm_serials"

# Reason prefix the create path writes when a line has NO free serial to hold.
# Named because the retry sweep must exclude these rows: nothing on Woo can fix
# them (there is no dealer-entered serial to re-apply — the shop is simply out of
# stock), so re-reading the order every two hours would burn REST calls forever.
# Sharing the constant keeps the writer and the filter from drifting apart.
_NO_STOCK_REASON = "No Active unreserved serial available"


# ---------------------------------------------------------------------------
# Inbound flags
# ---------------------------------------------------------------------------

@contextmanager
def woo_site(site: str):
	"""Set the two flags every Woo-inbound path depends on, then restore them.

	``woo_sync_inbound`` is the doc_events loop guard (``doc_events._skip_push``
	reads it); ``woo_sync_site`` threads the originating store into the REST
	client and the outbound pushes. They belong together — three helpers on this
	path state the inbound flag as a precondition in their docstrings
	(:func:`_reserve_serial`, ``_skip_push``) — so
	setting one without the other is precisely how a Woo→POS→Woo echo gets
	introduced later, by someone who never reads those docstrings.

	Restores the PREVIOUS values rather than blanking them, so a nested use can
	never clear a flag its caller still needs.
	"""
	prior_inbound = frappe.flags.get("woo_sync_inbound")
	prior_site = frappe.flags.get("woo_sync_site")
	frappe.flags.woo_sync_inbound = True
	frappe.flags.woo_sync_site = site
	try:
		yield
	finally:
		frappe.flags.woo_sync_inbound = prior_inbound
		frappe.flags.woo_sync_site = prior_site


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Webhook-safe error logging: never blocks the webhook ack.
_log_error = safe_log_error


# ---------------------------------------------------------------------------
# HMAC verification
# ---------------------------------------------------------------------------

class WebhookAuthError(Exception):
	"""HMAC verification failed — caller should return 401."""


def _read_raw_body() -> bytes:
	"""Return raw POST body bytes.  HMAC MUST run over exactly these bytes —
	re-serialising the parsed JSON would break the signature."""
	return frappe.request.get_data() if getattr(frappe, "request", None) else b""


def _verify_hmac(raw_body: bytes, sig_b64: str, secret: str) -> None:
	"""Verify WooCommerce HMAC-SHA256 signature.

	WC header: ``X-WC-Webhook-Signature: base64(HMAC-SHA256(raw_body, secret))``
	"""
	if not sig_b64:
		raise WebhookAuthError("Missing X-WC-Webhook-Signature header")
	expected = base64.b64encode(
		hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
	).decode("utf-8")
	if not hmac.compare_digest(expected.strip(), sig_b64.strip()):
		raise WebhookAuthError("HMAC signature mismatch")


def _get_webhook_secret(site: str = "retail") -> str:
	settings_doctype = store_entry(site)["settings"]  # woo_event refused any other site
	s = frappe.get_single(settings_doctype)
	secret = s.get_password("webhook_secret", raise_exception=False) or ""
	if not secret:
		raise WebhookAuthError(f"{settings_doctype}.webhook_secret is empty")
	return secret


def _origin_site() -> str | None:
	"""The store an inbound webhook names in its ``?site=`` query param: the retail store when
	there is none (no param, or a blank one); None when it names a store this POS does not run
	(:func:`woo_event` refuses it).

	The retail store posts with no param. WooCommerce delivers the order as a JSON **body**, so
	Frappe populates ``frappe.form_dict`` from that body and DROPS query-string args: the param
	is read from ``frappe.request.args`` and nowhere else. A ``site`` field in the body or the
	form is order data, never a store name. Outside a request there is no query string: retail.

	Not folded to "retail" when unknown: a webhook naming a store this POS does not run must
	not be processed as a retail order — that silent downgrade is a double-sell.
	"""
	request = getattr(frappe, "request", None)
	return resolve_site(request.args.get("site") if request else None)


# ---------------------------------------------------------------------------
# FFL extraction from order meta_data
# ---------------------------------------------------------------------------

# Exact meta_data keys from the storefront plugin's includes/class-order-meta.php (OrderMeta).
# Both classic checkout and block/Store-API persist to the same keys.
# Real storefront-plugin keys come FIRST; legacy / guessed variants follow as fallbacks.
_FFL_LICENSE_KEYS = (
	"_shipping_fflno",             # storefront-plugin canonical
	"ffl_license_number", "_ffl_license_number", "receiving_ffl",
	"ffl_number", "ffl_license",
)
_FFL_BUSINESS_KEYS = (
	"_shipping_ffl_name",          # storefront-plugin canonical
	"ffl_business_name", "_ffl_business_name", "ffl_name", "licensee_name",
)
_FFL_PHONE_KEYS = (
	"_shipping_ffl_phone",         # storefront-plugin canonical
	"ffl_phone", "_ffl_phone",
)
_FFL_STREET_KEYS = (
	"_shipping_ffl_premise_street",  # storefront-plugin canonical
	"ffl_premise_street", "ffl_street", "_ffl_street", "ffl_address",
)
_FFL_CITY_KEYS = (
	"_shipping_ffl_premise_city",   # storefront-plugin canonical
	"ffl_premise_city", "ffl_city", "_ffl_city",
)
_FFL_STATE_KEYS = (
	"_shipping_ffl_premise_state",  # storefront-plugin canonical
	"ffl_premise_state", "ffl_state", "_ffl_state",
)
_FFL_ZIP_KEYS = (
	"_shipping_ffl_premise_zip",    # storefront-plugin canonical
	"ffl_premise_zip", "ffl_zip", "_ffl_zip", "ffl_postal",
)
_FFL_EXPIRATION_KEYS = (
	"_shipping_fflexp",            # storefront-plugin canonical
	"ffl_expiration", "_ffl_expiration",
)

# Canonical ATF FFL number shape: region-district-county-type-expiration-sequence.
# Was a second, byte-identical copy of the same pattern; now the one in
# ffl_core.compliance.ffl_number, which is also what the order's validate() and
# the C&R gate judge against — one shape, one place. That copy anchors with \Z
# rather than $, so a number with a trailing newline is no longer spliced into a
# "canonical" form; splicing junk was never the intent, and the value now sent
# downstream is the same value that was validated.
_FFL_NUMBER_RE = FFL_NUMBER_RE


def _canonical_ffl_number(license_no: str, expiration: str) -> str:
	"""Splice the real ATF expiration code into a license number whose 5th group is
	a placeholder.

	The storefront plugin persists the receiving FFL number with a zeroed expiration group
	(``5-99-888-01-00-11762``) and the real 2-char code (``9C``) in a separate
	``_shipping_fflexp`` meta field. ATF eZ Check only validates region+sequence
	(first-3 / last-5), so the placeholder still "verifies", but FastBound validates
	the whole number and rejects it (``400 Contact.FFLNumber Invalid FFL Number``).
	Rebuild the canonical number so every downstream consumer — the ATF FFL Record,
	the disposition's ``destination_ffl``, and the FastBound push — uses the real
	code.

	Only a placeholder (``00``) group is rewritten; an already-correct embedded code
	is never clobbered, and non-canonical inputs are returned unchanged.
	"""
	license_no = (license_no or "").strip()
	exp = (expiration or "").strip().upper()
	m = _FFL_NUMBER_RE.match(license_no)
	if not m or len(exp) != 2:
		return license_no
	region, district, county, lic_type, exp_group, seq = m.groups()
	if exp_group == "00" and exp != "00":
		return f"{region}-{district}-{county}-{lic_type}-{exp}-{seq}"
	return license_no


def _extract_ffl(meta_data: list) -> dict:
	"""Extract receiving-FFL fields from the WC order's ``meta_data`` list.

	``meta_data`` is a list of ``{"key": str, "value": str}`` dicts.
	All FFL-related meta entries are stored in ``_raw`` as a fallback so
	nothing is lost if the plugin uses different key names.

	Returns::

		{
		    "license": str, "business_name": str, "street": str,
		    "city": str, "state": str, "zip": str,
		    "_raw": [...],   # all entries whose key contains "ffl"
		}
	"""
	flat: dict[str, str] = {}
	for m in meta_data:
		if isinstance(m, dict) and m.get("key"):
			flat[m["key"]] = str(m.get("value") or "")

	ffl_raw = [m for m in meta_data
		if isinstance(m, dict) and "ffl" in (m.get("key") or "").lower()]

	def _pick(keys: tuple) -> str:
		for k in keys:
			v = flat.get(k, "").strip()
			if v:
				return v
		return ""

	expiration = _pick(_FFL_EXPIRATION_KEYS)
	return {
		# The storefront plugin zeroes the number's expiration group and ships the real code
		# separately — splice it back so the canonical license is FastBound-valid.
		"license": _canonical_ffl_number(_pick(_FFL_LICENSE_KEYS), expiration),
		"business_name": _pick(_FFL_BUSINESS_KEYS),
		"phone": _pick(_FFL_PHONE_KEYS),
		"street": _pick(_FFL_STREET_KEYS),
		"city": _pick(_FFL_CITY_KEYS),
		"state": _pick(_FFL_STATE_KEYS),
		"zip": _pick(_FFL_ZIP_KEYS),
		"expiration": expiration,
		"_raw": ffl_raw,
	}


# ---------------------------------------------------------------------------
# FFL REST API fallback
# ---------------------------------------------------------------------------

def _ffl_is_empty(ffl: dict) -> bool:
	"""True when no identifying FFL field was resolved (license + name + street all blank)."""
	return not any([ffl.get("license"), ffl.get("business_name"), ffl.get("street")])


def _fetch_order_ffl(woo_order_id: str) -> dict:
	"""Fetch the WC order via REST API and re-extract FFL from its meta_data.

	WooCommerce versions vary in whether underscore-prefixed ("protected") meta
	keys are included in webhook payloads.  Orders differ from products — the
	WC REST API DOES return protected order meta — so a GET /orders/{id} is a
	reliable fallback.

	Logs a clear warning when the fallback is invoked (so the operator knows to
	verify their WC version includes protected meta in webhooks).  Logs an error
	and returns an empty FFL dict when the API call itself fails.
	"""
	from ffl_woo_sync.woocommerce.client import (
		WooCommerceClient,
		WooCommerceConfigError,
		WooCommerceError,
	)
	try:
		client = WooCommerceClient.from_settings(frappe.flags.get("woo_sync_site") or "retail")
		order = client.get_order(woo_order_id)
	except (WooCommerceConfigError, WooCommerceError) as exc:
		_log_error(
			title=f"Woo webhook: FFL REST fallback failed for order {woo_order_id}",
			message=str(exc),
		)
		return _extract_ffl([])

	meta_data = order.get("meta_data") or []
	ffl = _extract_ffl(meta_data)
	if not _ffl_is_empty(ffl):
		# Informational notice — NOT an error; use logger.warning so it does
		# not pollute the Frappe Error Log with operational-level diagnostics.
		try:
			frappe.logger("woo.webhook", allow_site=True).warning(
				f"Woo webhook: FFL resolved via REST fallback for order {woo_order_id} — "
				"webhook payload omitted underscore-prefixed meta_data keys. "
				"Fetched via GET /wc/v3/orders/{woo_order_id}. "
				"Verify whether your WC version includes protected meta in webhooks "
				"(staging test recommended)."
			)
		except Exception:
			pass  # logger failure is non-fatal
	return ffl


def _fetch_order_serials_and_status(woo_order_id: str) -> tuple[dict[str, list[str]], str]:
	"""REST fetch → ``(line id → dealer serials, live WC order status)``.

	WooCommerce may omit underscore-prefixed (protected) meta from webhook
	payloads, so a ``GET /orders/{id}`` is the reliable way to read the
	dealer-entered serial. Mirrors :func:`_fetch_order_ffl`.

	The status rides along free on that same response, and the retry sweep needs
	it: POS only ever learns an order died *through a webhook*, and the sweep
	exists precisely because the webhook may never arrive. Without reading it, a
	sweep would cheerfully re-pin a gun to an order cancelled on Woo days ago.

	On failure returns ``({}, "")`` — an empty status means **unknown**, never
	"alive", so callers must not read it as permission to act.
	"""
	from ffl_woo_sync.woocommerce.client import (
		WooCommerceClient,
		WooCommerceConfigError,
		WooCommerceError,
	)
	try:
		client = WooCommerceClient.from_settings(frappe.flags.get("woo_sync_site") or "retail")
		order = client.get_order(woo_order_id)
	except (WooCommerceConfigError, WooCommerceError) as exc:
		_log_error(
			title=f"Woo webhook: serial REST fallback failed for order {woo_order_id}",
			message=str(exc),
		)
		return {}, ""
	return (
		_extract_line_serials(order.get("line_items") or []),
		(order.get("status") or "").strip().lower(),
	)




# ---------------------------------------------------------------------------
# SKU → POS mapping
# ---------------------------------------------------------------------------

def _map_sku_to_pos(sku: str) -> tuple[str, str | None]:
	"""Split a WooCommerce SKU into (item_code, serial_no | None).

	Per-gun products use SKU ``"item_code::serial"``; item-level products
	use plain ``"item_code"``.
	"""
	if "::" in sku:
		parts = sku.split("::", 1)
		return parts[0].strip(), parts[1].strip() or None
	return sku.strip(), None


def _extract_line_serials(line_items: list) -> dict[str, list[str]]:
	"""Map WC line id → dealer-entered serials from each line item's
	``firearm_serials`` meta (set by the storefront plugin on the Edit Order screen).

	The meta value arrives either as a list (WC REST), a JSON-encoded string
	(some webhook payloads), or a bare string (single serial). Lines without the
	key — or whose serials are all blank — are omitted.
	"""
	out: dict[str, list[str]] = {}
	for line in line_items or []:
		if not isinstance(line, dict):
			continue
		line_id = str(line.get("id") or "").strip()
		if not line_id:
			continue
		raw = None
		for m in (line.get("meta_data") or []):
			if isinstance(m, dict) and m.get("key") == _SERIAL_META_KEY:
				raw = m.get("value")
				break
		if raw is None:
			continue
		if isinstance(raw, str):
			text = raw.strip()
			if text[:1] in ("[", "{"):
				try:
					raw = json.loads(text)
				except ValueError:
					raw = [text]
			else:
				raw = [text] if text else []
		if not isinstance(raw, (list, tuple)):
			raw = [raw]
		serials = [str(s).strip() for s in raw if str(s).strip()]
		if serials:
			out[line_id] = serials
	return out


def _extract_pricing(body: dict) -> dict:
	"""Order-level amounts from the WC order payload (tax, shipping, discount,
	total, currency) so the POS shows WooCommerce's full pricing."""
	return {
		"currency": (body.get("currency") or "").strip(),
		"order_total": flt(body.get("total")),
		"total_tax": flt(body.get("total_tax")),
		"shipping_total": flt(body.get("shipping_total")),
		"discount_total": flt(body.get("discount_total")),
	}


def _sync_woo_status_and_pricing(order_doc, body: dict) -> bool:
	"""Mirror the WC order status + amounts onto an existing Woo Online Order.
	Returns True if any field changed (so the caller persists). Keeps the POS
	fulfillment `order_status` separate — this only tracks the WooCommerce side."""
	changed = False
	status = (body.get("status") or "").strip()
	if status and (getattr(order_doc, "woo_status", None) or "") != status:
		order_doc.woo_status = status
		changed = True
	pricing = _extract_pricing(body)
	# Only overwrite currency when the payload actually carries one (don't blank a
	# known currency on a reduced payload).
	if pricing["currency"] and (getattr(order_doc, "currency", None) or "") != pricing["currency"]:
		order_doc.currency = pricing["currency"]
		changed = True
	for field in ("order_total", "total_tax", "shipping_total", "discount_total"):
		value = pricing[field]
		if flt(getattr(order_doc, field, 0)) != value:
			setattr(order_doc, field, value)
			changed = True
	return changed


def _sync_line_amounts(order_doc, line_items: list) -> bool:
	"""Update per-line WC amounts (unit price + subtotal/total/tax) on existing
	order lines, matched by woo_line_id — so re-syncs and WC line edits propagate
	(the create path captures these, but updates must too). Returns True if any
	field changed. These are standard WC line fields (not protected meta), so the
	webhook payload carries them — no REST refetch needed."""
	by_id = {str(li.get("id") or ""): li
		for li in (line_items or []) if isinstance(li, dict)}
	changed = False
	for line in order_doc.lines:
		li = by_id.get(str(line.woo_line_id))
		if not li:
			continue
		for field, key in (
			("unit_price", "price"), ("subtotal", "subtotal"),
			("line_total", "total"), ("line_tax", "total_tax"),
		):
			value = flt(li.get(key))
			if flt(getattr(line, field, 0)) != value:
				setattr(line, field, value)
				changed = True
	return changed


# ---------------------------------------------------------------------------
# Soft-reserve helpers
# ---------------------------------------------------------------------------

def _pick_oldest_unreserved_serial(item_code: str,
		exclude: set[str] | None = None) -> str | None:
	"""Return the oldest Active, non-reserved Serial No for ``item_code``, or None.

	``exclude`` names guns this caller must not be given back. Eviction needs it:
	it frees the disputed rifle in the same transaction, and this query — reading
	its own uncommitted write, ordered oldest-first — would otherwise return that
	very gun, since being the oldest free unit is how it became a placeholder in
	the first place.

	Uses a two-phase lock within the current database transaction to prevent
	concurrent webhooks from double-booking the same physical firearm:

	1. SELECT up to 5 candidates without a lock (cheap read).
	2. For each candidate, re-read with ``for_update=True`` to acquire a row
	   lock.  If the row was reserved between the SELECT and the lock (a lost
	   race), skip to the next candidate.

	Callers MUST be operating inside an active database transaction — the row
	lock is held until the transaction commits or rolls back.
	"""
	# is_transfer_gun=0: a customer's transfer gun hangs off the very
	# SKU a web order names — that shared-SKU Item shape is this feature's design,
	# not an accident — and it is Active and never woo_reserved, so nothing else
	# here would rule it out. R4b keeps it out of the pushed stock COUNT, which
	# makes the race narrow but not closed: sell the store's own one at the
	# counter, and before the stock push lands a web order arrives, skips the
	# now-Delivered gun on status, and reserves the customer's. Picking is
	# followed by shipping, so the failure mode is mailing away someone's
	# property.
	from ffl_core.reservations import counter_holds

	custody_whs = custody_warehouse_names()
	# One rulebook with `mapping.stock_for_item`, whose count this picker names out
	# of. `consigned_out` keeps out a gun sitting at a partner FFL (Active, not
	# woo_reserved, never counted by the storefront); `counter_holds` keeps out one
	# already promised on an open counter order — a submitted Sales Invoice awaiting
	# its FFL, a parked POS sale, a Draft Consignment Out. Without the second, this
	# picker could hand a web order a rifle the cashier is mid-way through selling
	# It must be `counter_holds`, NOT the whitelisted
	# `reserved_counter_serials`: this runs in the Guest webhook request, and the
	# gated one answers a Guest with a silent {} — which reads as "nothing is held".
	held = counter_holds(item_code)
	filters = {"item_code": item_code, "status": "Active", "woo_reserved": 0,
		"is_transfer_gun": 0, "consigned_out": 0}
	# `gb_sold_unknown`: GunBroker very probably sold it already. Fail closed, the
	# same key `reservations.uniform_free_serials` (the pool the claim gate counts)
	# applies. Column-guarded: the field belongs to ffl_integrations.
	if frappe.db.has_column("Serial No", "gb_sold_unknown"):
		filters["gb_sold_unknown"] = 0
	# Every OTHER sales channel's reservations, added to the same exclusion set
	# as the counter holds below. `woo_reserved: 0` stays an explicit filter — it
	# is this app's own flag and belongs in SQL — while a second channel's flag
	# may not even exist as a column on this site, so it is answered through the
	# shared predicate instead of being spelled into the filter dict.
	channel_held = {s for s in channel_reserved_serials(item_code)}
	# Both exclusion sets go into SQL, not a Python skip in the loop below: the
	# `limit` keeps the lock phase cheap, and a row filtered after the fetch eats a
	# candidate slot — five unusable guns at the front of the queue would make this
	# answer "no stock" with free rifles sitting behind them. For `exclude` it is
	# worse than wasteful: the excluded gun is the oldest free one by construction,
	# so dropping it after the fact could return None while stock still existed.
	unavailable = set(held or ()) | set(exclude or ()) | channel_held
	if unavailable:
		filters["name"] = ("not in", sorted(unavailable))
	candidates = frappe.get_all(
		"Serial No",
		filters=filters,
		fields=["name", "warehouse"],
		order_by="creation asc",
		limit=5,
	)
	for row in candidates:
		sn_name = row["name"]
		# Location is the second key, checked here in Python rather than as a SQL
		# `not in` because `warehouse` is a nullable Link — see the trap documented
		# at length in ffl_core/warehouses.py.
		if row.get("warehouse") and row["warehouse"] in custody_whs:
			continue
		locked = frappe.db.get_value(
			"Serial No",
			sn_name,
			["status", "is_transfer_gun", *channel_reserve_columns()],
			as_dict=True,
			for_update=True,
		) or {}
		# The locked re-check answers for every channel too: the SQL filter above
		# ran before the lock, so a GunBroker order poll landing in between would
		# otherwise be overwritten by a web order taking the same gun.
		if channel_reservation(sn_name, row=locked) or locked.get("status") != "Active":
			continue  # lost the race — another concurrent transaction claimed this serial
		# Re-checked under the row lock, like the other two: a gun can be flagged
		# into custody between the SELECT and the lock.
		if locked.get("is_transfer_gun"):
			continue
		return sn_name
	return None


def _is_uniform_price_item(item_code: str) -> bool:
	"""True when ONE WooCommerce product stands for several interchangeable guns.

	The dividing line for whether POS may name a rifle at order time. A per-serial
	listing is one product per gun and carries the serial in its own SKU, so there
	is nothing to guess. A uniform-price listing is a shelf of equivalent rifles
	behind a single product — such an order owes a UNIT, and which unit it is only
	becomes true when staff pull one off the rack.

	``cache=True`` like its siblings :func:`is_firearm_item` /
	:func:`is_regulated_firearm_item`, and named rather than inlined so tests can
	patch it the way they already patch those.
	"""
	return bool(frappe.db.get_value("Item", item_code, "uniform_sell_price", cache=True))


def _sells_through(item_code: str) -> str:
	"""The listing Item whose web orders may be fulfilled out of ``item_code``'s
	serials, or ``''`` when the Item declares no such pairing.

	The store keeps some models twice: one uniform-price Item that IS the
	WooCommerce product, and one per-gun-priced Item holding the same model for
	hand-picked sales. Staff fulfil an order for the first out of the second all
	day long — 19 hand re-bucketings in three days — and every one of those picks
	arrived as a refused serial. ``Item.sells_through_item`` is that pairing said
	out loud, on the pool.

	Declared on the SOURCE (the pool names the listing, not the other way round)
	so the lookup is one field read on a value we already hold, and so adding a
	pool cannot silently widen what an existing listing already accepts.

	``cache=True`` and named rather than inlined, like
	:func:`_is_uniform_price_item` — tests patch it by name.
	"""
	if not item_code:
		return ""
	return (frappe.db.get_value(
		"Item", item_code, "sells_through_item", cache=True) or "").strip()


def _free_units_for_claim(item_code: str) -> int:
	"""Units of *item_code* WooCommerce may still promise — the same count the
	storefront itself is given.

	Deliberately :func:`mapping.stock_for_item` rather than a second query: the
	question here ("can this order be filled?") is the one the shopper's Add to
	Cart already answered, so a private rulebook could only ever disagree with the
	shelf the customer saw. It nets off counter holds, custody, consigned-out units
	and other orders' claims for free.
	"""
	from ffl_woo_sync.woocommerce.mapping import stock_for_item

	try:
		return int(stock_for_item(item_code) or 0)
	except Exception:  # noqa: BLE001 — see below
		# Another channel could not say what it has committed (stock_for_item lets
		# that raise so a stock PUSH keeps its last number). Here the caller is
		# order intake, and `woo_event` answers 200 even when it rolls back, so
		# raising would lose a paid order. Answer "nothing free" instead: the order
		# is recorded with a no-stock reservation error that staff can see and fix.
		frappe.log_error(f"Woo claim: could not count free units of {item_code}",
			frappe.get_traceback())
		return 0


def unassigned_web_claims(item_code: str) -> int:
	"""Claims other pending web orders already hold on *item_code*.

	Re-exported from :mod:`mapping` so the naming gate can ask whether the pool it
	is about to name out of is unclaimed, and so a test patches one name instead of
	reaching through two modules."""
	from ffl_woo_sync.woocommerce.mapping import unassigned_web_claims as _claims

	return int(_claims(item_code) or 0)


def _claimed_items(order_doc) -> set[str]:
	"""Item codes on *order_doc* that took a unit without naming a rifle.

	The outbound stock push and the cancel-time release are both driven by
	ITERATING RESERVED SERIALS (:func:`_handle_order_created`,
	:func:`_handle_order_cancelled`). A claim reserves nothing, so neither edge
	fires on its own and WooCommerce would go on advertising a rifle that is
	already sold until the next reconcile sweep — a worse oversell than the
	deadlock claims exist to remove. This is what those two edges push instead.
	"""
	return {
		(sr.get("item_code") or "").strip()
		for sr in (order_doc.serials or [])
		if (sr.get("fulfillment") or LANE_LOCAL) == LANE_LOCAL
		and not (sr.get("confirmed_serial") or "").strip()
		and not (sr.get("stock_hold_serial") or "").strip()
		and (sr.get("item_code") or "").strip()
	}


def _committed_items(order_doc) -> set[str]:
	"""Item codes whose COMMITTED units this order moved: every line the store
	fills itself (a drop-shipped line never takes a unit off this shelf — the
	same lane rule as :func:`mapping.pending_web_line_qty`) plus the units it
	claimed without naming a rifle.

	Lines, not just claims: a claim is a uniform-price gun's commitment, but an
	accessory's (ammunition) is its open line, and GunBroker's quantity for that
	listing is derived from it.
	"""
	items = {
		(line.get("item_code") or "").strip()
		for line in (order_doc.lines or [])
		if (line.get("lane") or "") not in (LANE_DIRECT, LANE_FDS)
	}
	return {i for i in items if i} | _claimed_items(order_doc)


def _announce_commitments(item_codes) -> None:
	"""Tell the other channels this order moved these Items' committed units.

	A checkout and a cancellation post no stock ledger entry, so nothing else
	would: GunBroker's quantity for an item-level listing is the shelf less what
	the web store has sold and not yet shipped. Never raises (see
	:func:`reservations.notify_channel_commitments_changed`); queues after commit.
	"""
	for item_code in sorted({i for i in item_codes if i}):
		notify_channel_commitments_changed(item_code, source=CHANNEL_WOO)


def _reserve_serial(serial_no: str, order_name: str) -> None:
	"""Mark a Serial No as reserved for a Woo Online Order.

	Uses frappe.db.set_value (direct SQL) so the write bypasses doc_events —
	the woo_sync_inbound flag is still set at the request level for safety.
	"""
	frappe.db.set_value(
		"Serial No",
		serial_no,
		{"woo_reserved": 1, "woo_order": order_name},
		update_modified=False,
	)


def _enqueue_serial_or_item_push(
	serial_no: str, site: str = "retail", relist: bool = False
) -> None:
	"""Enqueue the outbound stock push for a serial whose reservation changed —
	a per-serial product re-pushes via ``push_serial_task``; an item-level serial
	re-pushes its parent Item via ``push_stock_task``.

	``relist`` defaults to **False** and must be passed explicitly by the one
	caller that means "this gun is for sale again". This helper is shared by
	three paths and only one of them is a release:

	    :func:`_release_serial`        reserve RELEASED → relist=True
	    (:func:`_handle_order_cancelled` releases inline, also relist=True)
	    :func:`_handle_order_created`  reserve TAKEN    → default (no relist)
	    :func:`_apply_dealer_serial`   reserve TAKEN    → default (no relist)

	Hardcoding the override here would hand it to the two reserve-TAKING paths,
	so an order whose serial picker landed on an owner-hidden gun would clear
	that gun's ``{site}_delisted`` sentinel — reopening the self-destruct that
	an earlier fix exists to close, through the reservation door.

	Site-aware: reads the serial's / parent Item's product_id for *site* (via
	:func:`sync._f`) and threads *site* into the task so the push targets that
	store.  Mirrors the enqueue logic in :func:`_handle_order_created` /
	:func:`_handle_order_cancelled`."""
	pid_field = _f(site, "product_id")
	sn_row = frappe.db.get_value(
		"Serial No", serial_no, [pid_field, "item_code"], as_dict=True
	) or {}
	if sn_row.get(pid_field):
		frappe.enqueue(
			"ffl_woo_sync.woocommerce.tasks.push_serial_task",
			queue="long", enqueue_after_commit=True, serial_no=serial_no, site=site,
			relist=relist,
		)
	elif sn_row.get("item_code"):
		parent_woo_id = frappe.db.get_value("Item", sn_row["item_code"], pid_field) or ""
		if parent_woo_id:
			frappe.enqueue(
				"ffl_woo_sync.woocommerce.tasks.push_stock_task",
				queue="long", enqueue_after_commit=True,
				item_code=sn_row["item_code"], site=site,
			)


def _push_item_stock_everywhere(item_code: str) -> None:
	"""Re-push an item-level stock count to the store the Item is listed on.

	The claim counterpart of :func:`_enqueue_serial_or_item_push`. That one starts
	from a Serial No; a claimed unit names none, so this starts from the Item.

	Not "the order's own store": there is one physical shelf, so a claim taken by
	any order changes the number the storefront must show
	(:func:`mapping.unassigned_web_claims` is site-blind for exactly this reason).
	"""
	item_row = frappe.db.get_value(
		"Item", item_code, ["woo_product_id"], as_dict=True,
	) or {}
	if item_row.get("woo_product_id"):
		frappe.enqueue(
			"ffl_woo_sync.woocommerce.tasks.push_stock_task",
			queue="short", enqueue_after_commit=True, item_code=item_code,
		)


def _release_serial(serial_no: str, site: str = "retail") -> None:
	"""Clear a serial's soft-reserve (``woo_reserved=0``, ``woo_order=""``) and
	re-push its stock so WooCommerce reflects the freed availability.

	Site-aware: the re-push targets the originating store (*site*).

	The clearing half lives in :func:`_clear_reservation` so eviction — which
	must free a gun WITHOUT announcing it as for sale — reuses it rather than
	hand-rolling the same two-column write."""
	_clear_reservation(serial_no)
	# Freeing a reservation means the gun is for sale again — it was
	# machine-delisted when the order took it, so this ONE re-push is allowed
	# past the hidden-serial guard.  The two reserve-TAKING callers
	# of this helper deliberately do not opt in.
	_enqueue_serial_or_item_push(serial_no, site=site, relist=True)


# ---------------------------------------------------------------------------
# Placeholder eviction — a staff-typed serial outranks POS's own guess
# ---------------------------------------------------------------------------

def _lock_item_reservations(item_code: str) -> None:
	"""Take the per-Item mutex that every reservation shuffle must hold first —
	``ffl_core.reservations.lock_item_reservations``, which carries the reasoning
	(eviction made this path lock two guns at once; one Item row, always taken
	before any Serial No, keeps the order fixed). It lives in ffl_core since a
	second channel moves the same reservation set and must take the SAME lock;
	the name stays here for the callers below and the tests that patch it."""
	lock_item_reservations(item_code)


def _clear_reservation(serial_no: str) -> None:
	"""Drop a soft-reserve, sending NO relist push.

	The quiet half of :func:`_release_serial`, split out because eviction needs
	exactly this and nothing more: the gun changes hands inside one transaction,
	so announcing it as for-sale again would put a just-sold rifle back on the
	shelf for as long as the queue takes to drain. `_release_serial` is this plus
	the relist, so the two can never drift apart.
	"""
	frappe.db.set_value(
		"Serial No", serial_no, {"woo_reserved": 0, "woo_order": ""},
		update_modified=False,
	)


def _evictable_hold(serial_no: str, item_code: str,
		holder_order: str) -> dict | None:
	"""Describe *holder_order*'s claim on *serial_no* when that claim is a POS
	PLACEHOLDER and may therefore be moved; None when it must stand.

	``Serial No.woo_reserved`` / ``woo_order`` record THAT a web order holds a
	gun, never HOW, and the two hows could not be less alike:

	* ``confirmed_serial`` — staff named this exact rifle for that order. A real
	  sale. Nothing may take it away.
	* ``stock_hold_serial`` — POS picked the oldest free gun at order time, with
	  no idea which one staff would actually pull off the rack. A guess, and
	  swappable for any other unit of the same Item.

	Reading only the Serial No, the cross-order guard could not tell them apart,
	so a staff pick lost to a neighbouring order's guess — and since a
	placeholder is released only when its OWN order gets a serial, the refusals
	chained: three prod orders (2026-08-13) deadlocked three
	rifles that were all sitting on the shelf. The distinction was never missing
	from the domain; ``fulfillment.reserved_web_serials`` has called these
	``woo_assigned`` and ``woo_held`` all along. This is the guard finally asking.

	Evictable requires ALL of:

	* the holder is still ``Pending Fulfillment`` — a disposed or cancelled
	  order's reservation is stale, not swappable, and rewriting a gun onto one
	  already booked is how the bound book and the shelf stop agreeing;
	* the row holds this serial as ``stock_hold_serial``…
	* …and carries no ``confirmed_serial`` of its own;
	* the row is the SAME Item as the caller's line. Mirrors the ``item_code``
	  predicate the sibling query :func:`fulfillment.reserved_web_serials` leads
	  with. Without it a mistyped serial naming another model's rifle still
	  matched, and the evicted order — an order for a different gun entirely —
	  was handed a replacement of OUR model;
	* the row is a local-fulfilment lane. A drop-shipped unit holds no local
	  stock, so there is nothing here to move.

	A reservation with no row behind it (dangling ``woo_order``) is a data fault,
	not an invitation — it stays put so the anomaly keeps showing.

	Row-locked (``FOR UPDATE``): the caller writes this row immediately after,
	and the holder's ``order_status`` is re-read here rather than trusted from a
	snapshot — an order cancelled in between would otherwise be handed a
	replacement gun it can never release.

	Returns:
	    ``{"row_name", "item_code", "reservation_error", "site", "woo_order_id"}``
	    for the evictable row, else None. The site is the HOLDER's, since the
	    notice and the stock re-push have to reach the store that order came from.
	"""
	rows = frappe.db.sql(
		"""
		SELECT s.name AS row_name, s.item_code AS item_code,
		       COALESCE(s.reservation_error, '') AS reservation_error,
		       o.site AS site, o.woo_order_id AS woo_order_id
		FROM `tabWoo Online Order Serial` s
		INNER JOIN `tabWoo Online Order` o ON o.name = s.parent
		WHERE s.parenttype = 'Woo Online Order'
		  AND s.parent = %(order)s
		  AND o.order_status = 'Pending Fulfillment'
		  AND s.stock_hold_serial = %(serial)s
		  AND COALESCE(s.confirmed_serial, '') = ''
		  AND s.item_code = %(item_code)s
		  AND COALESCE(NULLIF(s.fulfillment, ''), %(local)s) = %(local)s
		ORDER BY s.idx
		LIMIT 1
		FOR UPDATE
		""",
		{"order": holder_order, "serial": serial_no, "item_code": item_code,
			"local": LANE_LOCAL},
		as_dict=True,
	)
	return rows[0] if rows else None


def _evict_placeholder_hold(serial_no: str, item_code: str, holder_order: str, *,
		new_owner: str = "", preferred_replacement: str = "") -> str | None:
	"""Move *holder_order* off its placeholder on *serial_no* so the order whose
	staff actually named that rifle can have it.

	**Settle the disputed rifle before looking for a replacement.** The first cut
	cleared the reservation and then asked
	:func:`_pick_oldest_unreserved_serial` — which filters on ``woo_reserved=0``
	and reads this transaction's own uncommitted write, and returns the OLDEST
	such gun. A gun becomes someone's placeholder precisely by being the oldest
	free one, so the helper handed it straight back: the evicted order kept it as
	a hold while the naming order confirmed it, and two paid orders named one
	rifle. Reserving to *new_owner* first (or clearing plus an explicit
	``exclude``) makes that impossible rather than unlikely.

	*preferred_replacement* is the caller's own placeholder, offered before the
	pool is searched. Two orders swapping guns is the ordinary shape of this bug —
	three orders and three rifles with no spare was the prod case — and without
	the offer the evicted order got ``_NO_STOCK_REASON`` while the gun this order
	was about to release went back on sale. The swap has to close 1:1.

	The evicted order's existing ``reservation_error`` is NEVER overwritten: the
	retry sweep selects on ``reservation_error != ''`` (tasks.py), so blanking one
	drops that order out of its own auto-recovery for good — and a Woo order that
	has gone ``completed`` sends no webhook to put it back. Only a stale no-stock
	reason, the one thing this call just resolved, is cleared.

	Returns:
	    None when the claim must stand and nothing changed; ``""`` when the
	    eviction happened but no unit was free for the evicted order; otherwise
	    the serial the evicted order now holds instead.
	"""
	hold = _evictable_hold(serial_no, item_code, holder_order)
	if hold is None:
		return None
	# The HOLDER's store, not ours — its order note and the replacement's push go there.
	site = hold.get("site") or "retail"
	held_item = (hold.get("item_code") or item_code or "").strip()

	if new_owner:
		_reserve_serial(serial_no, new_owner)
	else:
		# Create path: the order has no name yet, so it cannot be named as owner.
		# The caller reserves it after insert, inside this same transaction.
		_clear_reservation(serial_no)

	if preferred_replacement and preferred_replacement != serial_no:
		replacement = preferred_replacement
	else:
		replacement = _pick_oldest_unreserved_serial(
			held_item, exclude={serial_no}) or ""

	fields: dict[str, str] = {"stock_hold_serial": replacement}
	if replacement:
		_reserve_serial(replacement, holder_order)
		_enqueue_serial_or_item_push(replacement, site=site)
		# A no-stock reason from an EARLIER eviction is exactly what this call
		# just fixed, so it goes; anything else is about this order's own serial
		# and must survive (see the sweep note above).
		if hold.get("reservation_error", "").startswith(_NO_STOCK_REASON):
			fields["reservation_error"] = ""
		note = (
			f"Serial {serial_no} was re-assigned to the order whose serial was "
			f"entered in WooCommerce; this order now holds {replacement} instead. "
			"No action needed unless that is not the gun you are shipping."
		)
		title = (f"Woo webhook: {holder_order} moved off held serial {serial_no} "
			f"onto {replacement}")
	else:
		# LEADS with _NO_STOCK_REASON: the sweep excludes those rows by prefix
		# (tasks.py: NOT LIKE '<reason>%%'), and must, because no dealer serial
		# exists on Woo for this order to re-apply. Worded any other way the row
		# becomes a REST call every two hours, forever.
		note = (
			f"{_NO_STOCK_REASON} for {held_item!r} — serial {serial_no} went to the "
			"order whose serial was entered in WooCommerce, and no other unit is "
			"free to hold for this one. Staff action required."
		)
		if not hold.get("reservation_error"):
			fields["reservation_error"] = note
		title = (f"Woo webhook: {holder_order} lost its held serial {serial_no} "
			f"with no replacement in stock")
	frappe.db.set_value(
		"Woo Online Order Serial", hold["row_name"], fields,
		update_modified=False,
	)
	# Logged on BOTH branches. The WooCommerce note is best-effort — its client
	# swallows failures — so without this a re-assignment could leave no trace
	# anywhere in POS, which is the "the gun changed quietly" outcome a review
	# calls out. `update_modified=False` above also means the row keeps its old
	# timestamp, so there is no implicit trail either.
	_log_error(title=title, message=(
		f"{note}\n\nevicted_order={holder_order} serial={serial_no} "
		f"replacement={replacement or '(none)'} new_owner={new_owner or '(pending insert)'}"
	))
	_queue_wc_order_note(str(hold.get("woo_order_id") or ""), f"POS: {note}", site)
	return replacement


def _evict_blocking_hold(serial_no: str, item_code: str, *,
		new_owner: str = "", preferred_replacement: str = "") -> str | None:
	"""Evict whoever is squatting on *serial_no* with a placeholder, if anyone is.

	The order-CREATE path's entry point: an order can arrive already carrying a
	staff-typed serial (the gun was picked before the webhook landed), and that
	serial deserves the same precedence as one typed later — otherwise the order
	is born stuck. Reads the reservation itself because the create path has no
	Serial No row in hand yet; ``for_update`` like every other read here, and the
	lock is held to the end of the transaction that then reserves the gun.

	Callers must already hold this Item's reservation mutex
	(:func:`_lock_item_reservations`).
	"""
	sn = frappe.db.get_value(
		"Serial No", serial_no, channel_reserve_columns(),
		as_dict=True, for_update=True,
	) or {}
	# EVERY holder, not the first one reported. Eviction is a WITHIN-Woo move: it
	# takes a gun off one web order's placeholder and hands it to another, and it
	# can do that because both sides are ours to reassign. Another channel's hold
	# is not — a GunBroker buyer has paid and the marketplace gives the seller no
	# way to undo it, so the answer to "may I take this gun?" there is no,
	# permanently. Evicting it would put a gun that is already sold back on the
	# shelf for a web order to claim.
	#
	# Asked as "is ANY non-Woo channel holding it" because the flags are ordered
	# and Woo comes first: a gun somehow carrying both flags would report as
	# held-by-woo, sail past a first-hit test, and be handed to another web order
	# while GunBroker still had it. The double-set state should not arise (both
	# writers take the row lock), but this is the guard that would have to catch
	# it if it did.
	holders = channel_reservations(serial_no, row=sn)
	if not holders:
		return None
	held = next((h for h in holders if h["channel"] != CHANNEL_WOO), None)
	if held:
		reason = (
			f"Serial {serial_no!r} is held by {held['channel']} order "
			f"{held['ref'] or '(unknown)'} and cannot be reassigned — that sale "
			f"is already made. Pick another gun for this order."
		)
		# _log_error, not an alert: the caller writes the row's
		# `reservation_error` from the refusal `_serial_availability` already
		# returned, and that is what staff see on the order. Alerting from here
		# too would be the same fact twice. (Nothing reaches this branch today —
		# `_serial_availability` sets `blocking_order` only for a Woo hold, so
		# eviction is not even attempted for another channel's. It is here so a
		# future caller that skips that check still cannot evict a paid sale.)
		_log_error(
			title=f"Woo webhook: {held['channel']} hold on {serial_no!r} blocks reassignment",
			message=f"{reason}\n\nnew_owner={new_owner or '(pending insert)'} item={item_code}")
		return None
	holder = (sn.get("woo_order") or "").strip()
	if not holder or holder == new_owner:
		return None
	return _evict_placeholder_hold(
		serial_no, item_code, holder,
		new_owner=new_owner, preferred_replacement=preferred_replacement,
	)


# ---------------------------------------------------------------------------
# Order line processing
# ---------------------------------------------------------------------------

def _serial_identity_problem(serial_no: str, sn: dict | None, item_code: str, *,
		label: str = "Serial") -> tuple[str, str]:
	"""``('', '')`` when *sn* is an Active Serial No of *item_code*; otherwise
	``(kind, reason)`` naming WHICH of three unrelated things went wrong.

	These used to share one sentence — "is not a valid in-stock serial for X" —
	produced by a single ``or`` chain. Three failures, three different actions,
	and the message described none of them:

	``unknown``     no Serial No by that name. Re-type it, or receive the gun.
	``inactive``    the gun is in POS but has left stock. Pick another.
	``wrong_item``  the gun is on the shelf, filed under a DIFFERENT Item. One
	                Reassign Item away from correct — and the sentence above
	                reads as "this gun does not exist", which is how a staff
	                member spends sixteen minutes on it.

	Status is tested before the Item on purpose: for a gun that is both sold and
	elsewhere, "reassign it here" is not merely unhelpful, it is wrong.

	The *kind* is the caller's branch, not decoration — only ``wrong_item`` may
	be auto-re-bucketed, and only the other two want a typo suggestion appended.
	"""
	if not sn:
		return "unknown", (
			f"{label} {serial_no!r} is not in POS — no Serial No by that name. "
			f"Check the stamp, or receive the gun into stock first."
		)
	status = (sn.get("status") or "").strip()
	if status != "Active":
		return "inactive", (
			f"{label} {serial_no!r} is no longer in stock (status "
			f"{status or 'unknown'}) — staff must pick another."
		)
	found = (sn.get("item_code") or "").strip()
	if found != item_code:
		return "wrong_item", (
			f"{label} {serial_no!r} is in stock but filed under Item {found!r}, "
			f"not {item_code!r}. If it is the right gun, open the Serial No and "
			f"use Reassign Item to move it to {item_code!r} — POS then applies "
			f"the serial on its own."
		)
	return "", ""


def _serial_unavailable_reason(serial_no: str, item_code: str) -> str:
	"""Return '' when ``serial_no`` is an Active, item-matching Serial No that is
	free to reserve, else a human-readable reason. Thin wrapper over
	:func:`_serial_availability` for the callers that only want the reason."""
	return _serial_availability(serial_no, item_code)[0]


def _not_ours_reason(serial_no: str, row: dict) -> str:
	"""'' unless the (locked) Serial No row is a gun the store does not own or
	does not have: a customer's transfer gun / custody-held firearm, or one
	consigned out to a partner dealer. Shared by the create path and the
	dealer-entered serial path so neither can pin such a gun to a web order."""
	if row.get("is_transfer_gun") or (
			row.get("warehouse") and row["warehouse"] in custody_warehouse_names()):
		return (
			f"Serial {serial_no!r} is a customer's firearm held for an FFL transfer "
			"— it is not the store's to sell; staff must pick another"
		)
	# The flag alone, as _pick_oldest_unreserved_serial reads it: it is the
	# first write ship_consignment_out makes.
	if row.get("consigned_out"):
		return (
			f"Serial {serial_no!r} is consigned out to a partner dealer "
			"— it is not on the store's shelf; staff must pick another"
		)
	return ""


def _serial_availability(serial_no: str, item_code: str) -> tuple[str, str]:
	"""``(reason, blocking_order)`` for ``serial_no`` against ``item_code``.

	``reason`` is '' when the gun is free to reserve, else the staff-facing text.
	``blocking_order`` is set ONLY when the gun is a perfectly good unit of this
	Item whose single problem is that another web order holds it — never when the
	serial is unknown, the wrong model, out of stock, or a customer's transfer
	gun.

	That distinction is the whole point of returning a tuple. Eviction may run
	only for a reservation conflict: gating it on "any reason at all" meant a
	mistyped serial naming ANOTHER model's rifle still triggered one, and an
	unrelated paid order was quietly moved onto the wrong gun. The reasons are
	tested in order — custody, then identity/stock, then reservation — so the
	reservation branch is reached only once everything else has passed, which is
	exactly the precondition eviction needs.

	Row-locks the Serial No ``for_update`` so a concurrent webhook cannot grab the
	same gun between this check and the post-insert reservation (the lock is held
	until the request transaction commits). Mirrors the lock-and-verify the old
	per-gun-SKU path used.
	"""
	locked = frappe.db.get_value(
		"Serial No",
		serial_no,
		["item_code", "status", "warehouse", "is_transfer_gun", "consigned_out",
		 *channel_reserve_columns()],
		as_dict=True,
		for_update=True,
	) or {}
	# A customer's transfer gun is not orderable at any price, so an order line
	# naming one is not a stock problem to reconcile — it is a gun that was never
	# ours to list. Reported with its own reason so the staff-facing
	# reservation_error says what actually happened.
	not_ours = _not_ours_reason(serial_no, locked)
	if not_ours:
		return not_ours, ""
	# Diagnosis only on this path: the serial here came off the line's own SKU, so
	# a cross-Item hit means the SKU→gun mapping is broken, not that staff picked
	# from a sister pool. Nothing to auto-re-bucket — say precisely what is wrong.
	_, identity = _serial_identity_problem(serial_no, locked, item_code)
	if identity:
		return identity, ""
	# ANY sales channel, read off the row this function just locked. This is the
	# named-serial path — the SKU on the order line points at one specific gun —
	# and before GunBroker it tested `woo_reserved` alone, so a gun already sold
	# on the marketplace would sail through and be reserved for a web buyer too.
	# One gun, two paid orders, and the GunBroker half cannot be cancelled from
	# the seller's side.
	held = channel_reservation(serial_no, row=locked)
	if held:
		held_by = f"{held['channel']} {held['ref']}".strip() if held["ref"] else held["channel"]
		return (
			f"Serial {serial_no!r} is already reserved ({held_by}) "
			"— staff must pick another"
		), (locked.get("woo_order") or "")
	return "", ""


# --------------------------------------------------------------------------
# Drop-ship lane (RSR Direct Connect)
# --------------------------------------------------------------------------

_SHIP_TO_FIELDS = (
	("ship_to_line1", "address_1"),
	("ship_to_line2", "address_2"),
	("ship_to_city", "city"),
	("ship_to_state", "state"),
	("ship_to_zip", "postcode"),
)


def _apply_ship_to(order, body: dict) -> None:
	"""Persist the buyer's ORIGINAL shipping address onto the order.

	Nothing needed this until now — the store shipped every parcel itself, so the
	address lived only in WooCommerce. A direct drop-ship accessory ships from the
	distributor's dock, and the address has to travel with the order.

	Deliberately the raw payload, not a re-derivation from an ERPNext Address:
	This exists because re-deriving a destination is how a parcel ends up at the
	FFL's premise instead of the buyer's home.

	WooCommerce leaves ``shipping`` empty for a cart that needs no shipping, in
	which case billing is the only address the buyer supplied.

	A cart with a firearm has its ``shipping`` rewritten to the FFL's premise, so
	by default the direct lines go to the dealer with the gun. When the buyer
	chose "ship non-firearm items to my address", the storefront plugin carries their own
	address in the ``ffl_core_ship_home`` meta, and that wins.
	"""
	body = body or {}
	shipping = body.get("shipping") or {}
	billing = body.get("billing") or {}
	src = _ship_home(body) or (
		shipping if (shipping.get("address_1") or "").strip() else billing)
	first = (src.get("first_name") or "").strip()
	last = (src.get("last_name") or "").strip()
	order.ship_to_name = (f"{first} {last}").strip() or (src.get("company") or "").strip()
	for field, key in _SHIP_TO_FIELDS:
		setattr(order, field, (src.get(key) or "").strip())


def _ship_home(body: dict) -> dict | None:
	"""The buyer's own address from the storefront plugin's ``ffl_core_ship_home`` meta, or None."""
	for m in body.get("meta_data") or []:
		if m.get("key") == "ffl_core_ship_home":
			value = m.get("value")
			if isinstance(value, dict) and (value.get("address_1") or "").strip():
				return value
	return None


def _refresh_ship_to_if_unrouted(order, body: dict) -> bool:
	"""Re-capture the buyer's shipping address on an order.updated.

	Only while the order is still ``Pending Route``: the direct drop-ship leg ships
	from this address, and the buyer/CS may correct it on Woo after checkout. Once
	the order has been placed with the distributor (any other dropship_status) the
	address is committed — refreshing it then would diverge from what was ordered.
	A non-drop-ship order (blank dropship_status) has nothing to refresh.

	Returns True when it changed the address."""
	if (order.get("dropship_status") or "") != "Pending Route":
		return False
	before = tuple(order.get(f) for f, _ in _SHIP_TO_FIELDS)
	_apply_ship_to(order, body)
	return tuple(order.get(f) for f, _ in _SHIP_TO_FIELDS) != before


def _book_revenue_after_auto_clear(order) -> None:
	"""#8 — book the Sales Invoice for an order auto-cleared to Transferred.

	The only route to a web Sales Invoice ran through ``Transferred``, and only a
	firearm disposition ever set that — so a purely-accessory web order booked NO
	revenue at all: its gross receipts and its TX tax base were both simply
	missing. Auto-clear is that order's completion event, so it books here.

	Deferred until after commit for the same reasons the refund reconcile is: the
	insert/submit/collect chain is too heavy for a webhook response, and the job
	must read back a persisted ``Transferred`` order — which also makes a failure
	retryable rather than a lost invoice.
	"""
	if (order.get("order_status") or "") != "Transferred":
		return
	# Which job finishes the order — antique retire (stock out -> retire listing
	# -> revenue, see antique.py) or the plain revenue booking (which now also
	# issues an accessory order's own shelf stock) — is decided in ONE place,
	# revenue.web_completion_job; the pickup button and the backstop run the
	# same answer inline.
	from ffl_woo_sync.woocommerce.revenue import web_completion_job

	frappe.enqueue(
		web_completion_job(order),
		queue="long",
		enqueue_after_commit=True,
		order=order.name,
	)

LANE_LOCAL = "local"
LANE_TO_STORE = "to_store"
LANE_FDS = "fds"
LANE_DIRECT = "direct"

# Woo Online Order.dropship_status values that mean "a distributor still owes us
# something on this order" (the #3 auto-clear guard keys off this). SECOND copy of
# ffl_integrations.distributor.router.UNSETTLED_DROPSHIP_STATUSES — this app may
# not import that one (woo_sync must not import integrations). The
# two copies, and the doctype's dropship_status Select options, are pinned
# together by ffl_integrations/tests/test_distributor_layering.py::
# TestDropshipStatusAgreement. **Edit all three or none** — if router grows a new
# unsettled state and this set does not, the auto-clear guard silently lets a
# still-open order mark itself Transferred and book revenue.
_UNSETTLED_DROPSHIP_STATUSES: frozenset[str] = frozenset({
	"Pending Route", "Ordered", "Error",
})

_MODE_TO_STORE = "To Store"


def _line_source(item_code: str, *, is_firearm: bool) -> dict:
	"""Who fills this order line: ``{lane, distributor, sku}`` with lane one of
	``local`` / ``to_store`` / ``fds`` / ``direct``. Recorded on the line at
	checkout, so routing and invoicing follow what the order was sold under, not
	the product's source at the time they run.

	A drop-shipped FIREARM can never ship to the buyer's door — it goes to their
	transfer FFL, which is RSR's FDS lane. An accessory ships direct. Under C2 a
	blocked brand flips MODE, and both kinds come to the shop first.

	The source comes from the drop-ship layer's ``dropship_source_for_item`` hook
	(:func:`mapping.dropship_source`); None means the store ships it.
	"""
	from ffl_woo_sync.woocommerce.mapping import dropship_source

	src = dropship_source(item_code, lead=False)
	if not src:
		return {"lane": LANE_LOCAL, "distributor": "", "sku": ""}
	if str(src.get("fulfillment_mode") or "").strip() == _MODE_TO_STORE:
		lane = LANE_TO_STORE
	else:
		lane = LANE_FDS if is_firearm else LANE_DIRECT
	return {"lane": lane, "distributor": src.get("distributor") or "",
		"sku": src.get("sku") or ""}


def _fulfillment_lane(item_code: str, *, is_firearm: bool) -> str:
	"""The lane alone — for a line taken before the lane was recorded."""
	return _line_source(item_code, is_firearm=is_firearm)["lane"]


def _process_lines(order_doc, line_items: list) -> list[str]:
	"""Append commerce ``lines`` rows and, for firearms, one ``serials`` row per
	physical unit (``unit_index`` 0..qty-1).  Returns the list of ``Serial No``
	names to reserve after the order is inserted (confirmed serials + placeholders).

	Serial source per unit = ``firearm_serials[unit_index]`` (pre-filled for per-gun
	SKUs, dealer-entered for uniform firearms).  When a unit has no field serial, an
	oldest-unreserved placeholder is held so WooCommerce stock drops while the real
	serial is awaited; ``confirmed_serial`` stays blank.  No serial is written to
	``line.serial`` (vestigial).
	"""
	to_reserve: list[str] = []
	line_serials = _extract_line_serials(line_items)
	has_dropship = False
	skipped_line_ids: list[str] = []
	# Units this order has already spent, per Item — claimed OR named. The order is
	# not inserted yet, so neither its claims nor its just-named rifles are visible
	# to the availability query; without this every unit of a two-unit order would
	# be told the same shelf is free.
	spent_units: dict[str, int] = defaultdict(int)

	# Every Item this order touches, locked up front and in NAME ORDER. Both
	# halves matter: up front, because a serial must never be locked before its
	# Item's mutex; sorted, because an order with two firearm models could
	# otherwise take them in WooCommerce's line order and deadlock against a
	# concurrent order listing the same two the other way round.
	#
	# Deliberately NOT narrowed to firearms: `is_firearm_item` is itself a
	# database read, and doing it here would double the reads on the hot path to
	# save a lock nobody contends. Locking an accessory row costs nothing.
	items = sorted({_map_sku_to_pos(line.get("sku").strip())[0]
		for line in line_items if (line.get("sku") or "").strip()})
	# Who fills each Item, resolved BEFORE the locks below: an unlisted
	# accessory's brand-policy read can wait on the catalog service, and must
	# not do it while holding other checkouts' reservation locks.
	regulated = {ic: is_regulated_firearm_item(ic) for ic in items}
	sources = {ic: _line_source(ic, is_firearm=regulated[ic]) for ic in items}
	for locked_item in items:
		_lock_item_reservations(locked_item)

	for line in line_items:
		sku = (line.get("sku") or "").strip()
		if not sku:
			# A line we cannot map to POS at all. It is dropped WITHOUT a `lines`
			# row, so nothing downstream can see that it existed — including the
			# Pending Order queue, whose actionless test reasons over `lines`
			# (fulfillment._cannot_prove_actionless). If this was a firearm, the
			# order can therefore look "nothing to do" and leave the queue while a
			# gun still owes a bound-book entry. We cannot recover the line here,
			# but it must not vanish SILENTLY — record it below.
			skipped_line_ids.append(str(line.get("id") or "?"))
			continue

		item_code, serial_from_sku = _map_sku_to_pos(sku)
		woo_line_id = str(line.get("id") or "")
		qty = max(1, int(line.get("quantity") or 1))
		unit_price = float(line.get("price") or 0)

		# The LANE stays on the ATF test: a drop-ship lane decides who ships a
		# REGULATED gun and to whose FFL, which an antique has no part in.
		source = sources[item_code]
		lane = source["lane"]

		order_doc.append("lines", {
			"item_code": item_code,
			"serial": "",  # vestigial — firearm serials live in the `serials` table
			"woo_line_id": woo_line_id,
			"qty": qty,
			"unit_price": unit_price,
			"subtotal": flt(line.get("subtotal")),
			"line_total": flt(line.get("total")),
			"line_tax": flt(line.get("total_tax")),
			"reservation_error": "",
			"lane": lane,
			"source_distributor": source["distributor"],
			"source_sku": source["sku"],
		})

		if lane != LANE_LOCAL:
			has_dropship = True

		# ...but the serial ROW is an INVENTORY device, not an ATF one. It carries
		# three things that have nothing to do with the bound book: the reservation
		# that stops the same gun being sold twice, the serial the Sales Invoice
		# needs on its line, and the target the listing archive/delist acts on.
		#
		# An antique (pre-1899 receiver) is GCA-exempt — no bound book, no 4473, no
		# FFL — but it is still one serialized rifle on our shelf, so it needs all
		# three. Gating this on `is_regulated_firearm_item` is what made an antique
		# sale (a) reserve nothing, so two shoppers could buy the same gun, (b) book
		# NO revenue at all, because the Sales Invoice needs a serial on any
		# is_firearm line and threw at submit without one, (c) never relieve stock,
		# and (d) stay listed for sale after it shipped. The incident order was all four.
		# `is_firearm_item` is the exact complement of the old test: it adds back
		# antiques and changes nothing else.
		if not is_firearm_item(item_code):
			continue  # accessory: no serial rows

		provided = line_serials.get(woo_line_id) or []
		for unit_index in range(qty):
			field_serial = (provided[unit_index].strip()
				if unit_index < len(provided) and provided[unit_index] else "")
			# Per-gun SKU fallback applies only to a single-gun line (qty 1): a
			# per-gun SKU with qty>1 is a WC data error — extra units fall back to
			# placeholders, and firearm_serials remains the authoritative source.
			if not field_serial and serial_from_sku and qty == 1:
				field_serial = serial_from_sku

			row = {
				"item_code": item_code,
				"woo_line_id": woo_line_id,
				"unit_index": unit_index,
				"confirmed_serial": "",
				"stock_hold_serial": "",
				"reservation_error": "",
				"fulfillment": lane,
			}

			# A drop-shipped unit is filled from the distributor's
			# warehouse, so there is NOTHING here to hold: no placeholder, no
			# reservation, and a dealer-typed serial is ignored because it names a
			# gun that is not ours. The row exists only to carry the unit and its
			# lane; RSR's serial is recorded on it later (Distributor Order line →
			# confirmed_serial) purely as a record — it never becomes a local
			# Serial No, a disposition, or a stock movement.
			if lane != LANE_LOCAL:
				order_doc.append("serials", row)
				continue

			if field_serial:
				reason, blocked_by = _serial_availability(field_serial, item_code)
				# Staff typed this serial before the webhook landed, so it is the
				# same physical fact the order.updated path honours: if the ONLY
				# thing in the way is another order's PLACEHOLDER, move it —
				# otherwise the order is born stuck.
				#
				# Asked only once a refusal is in hand, and asked SEPARATELY: the
				# blocking order is what gates the eviction, and it is set only for
				# a reservation conflict on an otherwise perfect unit, so a
				# mistyped serial naming another model — or a customer's transfer
				# gun — can never reach one. Gating on "any reason at all" moved an
				# unrelated paid order onto the wrong rifle.
				#
				# No new_owner: this order has no name until it is inserted, and
				# the reservation loop after the insert takes the gun then.
				if blocked_by and _evict_blocking_hold(
						field_serial, item_code) is not None:
					reason = ""
				if reason:
					row["reservation_error"] = reason
					# Blank/blank, so `mapping.unassigned_web_claims` counts this row
					# as a claim once the order is inserted — the in-request budget has
					# to agree, or the next unit is told the shelf is one deeper than
					# it is.
					spent_units[item_code] += 1
					_log_error(
						title=f"Woo webhook: serial {field_serial!r} unavailable for {item_code}",
						message=f"sku={sku!r}, woo_line_id={woo_line_id}, unit={unit_index}: {reason}",
					)
				else:
					row["confirmed_serial"] = field_serial
					to_reserve.append(field_serial)
					# Spent, like any other unit: it is not reserved until after the
					# insert, so the next unit's availability read cannot see it.
					# (develop had the same blind spot; this accumulator is where it
					# belongs.)
					spent_units[item_code] += 1
			elif _is_uniform_price_item(item_code):
				# One product, many equivalent rifles: this order owes a UNIT, and
				# which rifle it is is not knowable yet. POS used to answer anyway
				# by parking the oldest free gun here — a guess staff's own pick
				# then contradicted, and with a full queue the refusals chained
				# into a deadlock nothing could break (seven Mosin orders, 2026-08-13).
				#
				# So the row claims a unit and names nothing. It is NOT an error
				# state: `awaiting_serial` is derived from a blank confirmed_serial
				# alone, so the queue already reads it as "waiting for the serial
				# from WooCommerce" with no error badge, and staff's serial lands
				# on a rifle nobody is holding.
				#
				# The claim is spent through `mapping.unassigned_web_claims`, which
				# is why nothing is appended to `to_reserve` and why the count still
				# drops. `spent_units` covers what the database cannot see yet: this
				# order is not inserted, so neither its own claims nor the rifles it
				# just named are visible to that query. Keyed per ORDER, not per
				# line — two WooCommerce lines can name the same Item.
				free = _free_units_for_claim(item_code) - spent_units[item_code]
				# `free` is a PROMISE budget, not a rack count: `stock_for_item`
				# already nets off other orders' claims. Naming is therefore gated on
				# BOTH — one unit left to promise AND nobody else holding a claim —
				# because only then is the rifle it would name unambiguous. Seven
				# rifles with six claims outstanding also reads `free == 1`, and
				# picking "the oldest" out of seven is the guess this change exists
				# to stop making.
				#
				# The read is NOT locked. At the naming gate that is settled for us:
				# `_pick_oldest_unreserved_serial` re-reads each candidate
				# `for_update`, so two webhooks racing for the last rifle serialise
				# there and the loser is told below that nothing is free. The CLAIM
				# branch stays racy — three simultaneous orders against two free units
				# each see a budget of 2 and all three claim, and the third gets no
				# `_NO_STOCK_REASON` alarm at order time. That is a lost early warning,
				# not a double-sell: the storefront still clamps to 0
				# (`mapping.stock_for_item`) and the rifles are still picked by hand.
				# Deliberately NOT fixed with an Item-row lock here. An earlier change did take one
				# and measured it closing its own AB-BA (2/2 -> 0/1); its review then
				# found the ordering runs OPPOSITE to ERPNext's Purchase Receipt path,
				# which writes the Item after its serial rows (`buying_controller` ->
				# `update_last_purchase_rate`) — i.e. a fresh deadlock surface against
				# receiving, a compliance path. That was never measured either way, so
				# the honest statement is: not attempted here, residual recorded.
				if free <= 0:
					# Nothing left to promise — say so before anything else, or an
					# oversold shelf reads as an ordinary claim.
					row["reservation_error"] = (
						f"{_NO_STOCK_REASON} for {item_code!r} to "
						"hold stock — staff action required"
					)
					spent_units[item_code] += 1
					_log_error(
						title=f"Woo webhook: no available unit for {item_code}",
						message=f"woo_line_id={woo_line_id}, unit={unit_index}",
					)
				elif (
					free == 1
					and spent_units[item_code] == 0
					and unassigned_web_claims(item_code) == 0
				):
					# One rifle left, nobody else holding a claim, and this order has
					# spent nothing yet — the one shape where the pool offers a single
					# candidate, so naming it is not a guess. Worth doing,
					# because a claim reserves no Serial No and the counter's own gate
					# reads `woo_reserved` — so without this the shop could sell the
					# last rifle out from under a paid web order and strand it. Naming
					# it also puts this unit back on the ordinary reserved-serial path,
					# which carries its own lock, its own stock push and its own
					# release on cancel.
					placeholder = _pick_oldest_unreserved_serial(item_code)
					if placeholder:
						row["stock_hold_serial"] = placeholder
						to_reserve.append(placeholder)
						spent_units[item_code] += 1
					else:
						# Nothing to name. The count nets off MORE than the picker
						# does (counter holds, consigned-out), so it is the stricter
						# of the two — an empty picker while the count says one means
						# the unit is gone, most likely to the webhook that just won
						# the row lock. Falling back to a claim here would promise a
						# rifle that does not exist; say so instead.
						row["reservation_error"] = (
							f"{_NO_STOCK_REASON} for {item_code!r} to "
							"hold stock — staff action required"
						)
						spent_units[item_code] += 1
						_log_error(
							title=f"Woo webhook: last unit of {item_code} was taken first",
							message=f"woo_line_id={woo_line_id}, unit={unit_index}",
						)
				else:
					# Units remain, but which rifle is not knowable — either because
					# several are free, or because other orders already hold claims on
					# the pool. Take a unit and name nothing.
					spent_units[item_code] += 1
			else:
				# Per-serial listing reaching here means a per-gun SKU carried
				# qty>1 (a WC data error the comment above calls out). Unchanged.
				placeholder = _pick_oldest_unreserved_serial(item_code)
				if placeholder:
					row["stock_hold_serial"] = placeholder
					to_reserve.append(placeholder)
				else:
					row["reservation_error"] = (
						f"{_NO_STOCK_REASON} for {item_code!r} to "
						"hold stock — staff action required"
					)
					_log_error(
						title=f"Woo webhook: no available serial for {item_code}",
						message=f"woo_line_id={woo_line_id}, unit={unit_index}",
					)

			order_doc.append("serials", row)

	# ONE Error Log per order, not one per line: a webhook that spams the log gets
	# muted by humans, which defeats the point. Error Log (not just frappe.logger)
	# because logs/*.log on the box is not a surface anyone watches — this has to be
	# searchable and alertable in Desk.
	if skipped_line_ids:
		_log_error(
			title=(f"Woo webhook: unmapped line(s) skipped on order "
				f"{order_doc.woo_order_id}"),
			message=(
				f"WooCommerce line id(s) {', '.join(skipped_line_ids)} carried no SKU, "
				f"so no order line was recorded for them on Woo order "
				f"{order_doc.woo_order_id}.\n\n"
				"Consequence: POS holds NO record of these lines. If one of them was a "
				"firearm, the order can read as 'nothing for POS to do' and drop out of "
				"the Pending Order queue while the gun still owes a bound-book "
				"disposition. Check the order in WooCommerce and, if a firearm was "
				"sold, book it by hand.\n\n"
				"Fix at source: every sellable WooCommerce product needs a SKU."
			),
		)

	# Mark the order for routing HERE, inside the same insert — not from the
	# async router. A `completed` webhook can otherwise arrive before the routing
	# job runs, and _auto_clear_non_firearm_if_completed would mark an
	# accessory order Transferred (and book its revenue) before RSR has shipped.
	if has_dropship:
		order_doc.dropship_status = "Pending Route"

	return to_reserve


# ---------------------------------------------------------------------------
# order.created / order.updated handler
# ---------------------------------------------------------------------------

def _handle_order_created(body: dict) -> None:
	"""Create a Woo Online Order and soft-reserve serials.

	Idempotent on ``woo_order_id`` — if a Woo Online Order already exists for
	this WC order ID, this is a no-op (ack without re-processing).

	Soft-reserve:
	- Per-gun SKU (``item::serial``) → reserve that Serial No.
	- Uniform/per-serial firearm (plain SKU) → pick oldest Active unreserved.
	- Non-firearm → record line only, no reservation.
	- No-stock case → record ``reservation_error`` on line; flag for staff.

	Stock push (outbound, not an echo):
	Enqueues ``push_serial_task`` / ``push_stock_task`` for each affected
	item so WooCommerce reflects the reservation immediately.
	"""
	# Resolve originating site from frappe.flags.woo_sync_site set by woo_event.
	# woo_event stores the validated ?site= value in the flag for the entire handler
	# body; defaults to "retail" when not set (direct unit-test calls or retail path).
	_site = frappe.flags.get("woo_sync_site") or "retail"

	woo_order_id = str(body.get("id") or "").strip()
	if not woo_order_id:
		_log_error(
			title="Woo webhook: order.created missing id",
			message=json.dumps(body)[:3000],
		)
		return

	# Already on file: this order.updated is a follow-up edit (typically the
	# dealer entering the firearm's serial on the WC Edit Order screen). Apply
	# any newly-supplied per-line serials instead of the old no-op, then ack.
	if frappe.db.exists("Woo Online Order", {"woo_order_id": woo_order_id, "site": _site}):
		_handle_order_updated(body)
		return

	# Customer fields from billing object
	billing = body.get("billing") or {}
	first = (billing.get("first_name") or "").strip()
	last = (billing.get("last_name") or "").strip()
	customer_name = (f"{first} {last}").strip() or billing.get("company") or ""

	# FFL fields from meta_data — with REST API fallback for WC versions that
	# omit underscore-prefixed ("protected") meta from the webhook payload.
	meta_data = body.get("meta_data") or []
	ffl = _extract_ffl(meta_data)
	ffl_unresolved = False
	if _ffl_is_empty(ffl):
		ffl = _fetch_order_ffl(woo_order_id)
		# Logged below, once the lines are known: an accessory-only order has no
		# FFL to resolve, and the alert was firing on every bayonet / optic sale.
		ffl_unresolved = _ffl_is_empty(ffl)

	# Build order document (don't insert yet — need to process lines first)
	order = frappe.new_doc("Woo Online Order")
	order.woo_order_id = woo_order_id
	order.order_status = "Pending Fulfillment"
	order.site = _site
	# WC status + order-level amounts (same path the update flow uses).
	_sync_woo_status_and_pricing(order, body)
	order.customer_name = customer_name
	order.customer_email = (billing.get("email") or "").strip()
	order.customer_phone = (billing.get("phone") or "").strip()
	# The buyer's own destination — needed by a direct drop-ship accessory, which
	# ships from the distributor rather than from the shop.
	_apply_ship_to(order, body)
	order.ffl_license = ffl["license"]
	_set_ffl_details(order, ffl)
	order.ffl_raw = json.dumps(ffl["_raw"]) if ffl["_raw"] else ""
	# Seed the authority signal at creation, so the first staff edit on Woo
	# (rev 2+) is recognised as newer instead of tying with a default 0 and
	# being dropped as a replay. The C&R flag is derived from the number itself.
	order.woo_ffl_rev = _extract_ffl_signal(meta_data)["rev"] or 0
	order.is_cr_transfer = 1 if is_cr_license(ffl["license"]) else 0

	# Process lines (appends child rows; returns list of serials to reserve)
	to_reserve = _process_lines(order, body.get("line_items") or [])
	if ffl_unresolved and (order.serials or []):
		_log_error(
			title=f"Woo webhook: FFL not resolved for order {woo_order_id}",
			message=(
				"Could not extract FFL from webhook meta_data or GET /orders fallback. "
				"Staff must fill FFL fields on the Woo Online Order manually."
			),
		)

	# An order whose FIRST webhook arrives already completed (the original one was
	# lost and staff re-fired it from WooCommerce, as order_audit tells them to)
	# gets no later order.updated: clear it now, or an accessory-only order sits
	# in the queue for ever with no revenue booked and its stock never issued.
	# Inserted straight into Transferred, so validate stamps transferred_at.
	auto_cleared = _auto_clear_non_firearm_if_completed(order)

	order.flags.ignore_permissions = True
	order.insert()

	# Tag the order 'web' (Desk-filterable) so it's distinguishable from counter
	# orders. Write _user_tags directly: Document.add_tag() runs a permission check
	# that fails for the Guest webhook user (the insert above uses ignore_permissions,
	# but add_tag does not honor it).
	frappe.db.set_value("Woo Online Order", order.name, "_user_tags", ",web",
		update_modified=False)

	# Reserve serials (confirmed + placeholders) and enqueue the outbound stock
	# push on the originating store.
	for serial_no in to_reserve:
		try:
			_reserve_serial(serial_no, order.name)
		except Exception as exc:
			_log_error(
				title=f"Woo webhook: reserve {serial_no} failed",
				message=str(exc),
			)
		_enqueue_serial_or_item_push(serial_no, site=_site)

	# A claimed unit names no Serial No, so the loop above — the ONLY outbound push
	# on this path — never sees it. Nothing else picks up the slack either: no
	# serial is written, so no Serial No doc_event fires; no stock moves, so no
	# Stock Ledger Entry does; and there is no doc_event on Woo Online Order at all. Without this the count would be right
	# in the database and wrong on the storefront until the next reconcile sweep.
	for item_code in sorted(_claimed_items(order)):
		_push_item_stock_everywhere(item_code)

	# The other channels hear it too: the claim (and an accessory's open line) is
	# what they subtract from their own quantity.
	_announce_commitments(_committed_items(order))

	if auto_cleared:
		_book_revenue_after_auto_clear(order)


# ---------------------------------------------------------------------------
# order.updated — apply the dealer-entered serial (WC Edit Order screen)
# ---------------------------------------------------------------------------

def _suggest_instock_serial(entered: str, item_code: str, order_name: str) -> str | None:
	"""The in-stock Serial No the dealer probably MEANT when ``entered`` was
	rejected — the U283116-vs-B283116 class of typo (a misread stamp letter,
	stray dash/space, or wrong case). Two tiers, each accepted only on a UNIQUE
	match so we never steer staff toward the wrong gun:

	1. normalized (uppercase alphanumerics) equality — case/punctuation slips;
	2. digits-only equality — a misread letter prefix.

	Candidates are the item's Active serials that are free or already held by
	THIS order (its own stock-hold placeholder is a fine suggestion); a serial
	reserved by another web order is never suggested. Suggestion only — the
	correction stays a human decision (A&D bound-book compliance)."""
	def _norm(s: str) -> str:
		return re.sub(r"[^A-Z0-9]", "", (s or "").upper())

	def _digits(s: str) -> str:
		return re.sub(r"[^0-9]", "", s or "")

	# Never a customer's transfer gun or a gun out on consignment: the dealer
	# path refuses those, so suggesting one only steers a typo onto a refusal.
	custody_whs = custody_warehouse_names()
	rows = [
		r for r in frappe.get_all(
			"Serial No",
			filters={"item_code": item_code, "status": "Active",
				"is_transfer_gun": 0, "consigned_out": 0},
			fields=["name", "warehouse"],
		)
		if not (r.get("warehouse") and r["warehouse"] in custody_whs)
	]
	# A gun is suggestible when NO channel holds it, or the only hold is this
	# order's own Woo one (its placeholder is a fine thing to suggest). The
	# "release it there" half is unchanged; what is new is that a gun sold on
	# another channel is never offered as a "did you mean…?" — a suggestion is
	# how a mistyped serial gets corrected onto a real gun, and pointing that at
	# a gun somebody has already bought elsewhere is the double sale arriving by
	# way of a helpful hint.
	channel_held = channel_reserved_serials(item_code)

	def _suggestible(name: str) -> bool:
		hold = channel_held.get(name)
		if not hold:
			return True
		return hold["channel"] == "woo" and (hold["ref"] or "") == order_name

	free = [r["name"] for r in rows if _suggestible(r["name"])]
	target_norm = _norm(entered)
	if target_norm:
		exact = [n for n in free if _norm(n) == target_norm]
		if len(exact) == 1:
			return exact[0]
	target_digits = _digits(entered)
	if target_digits:
		by_digits = [n for n in free if _digits(n) == target_digits]
		if len(by_digits) == 1:
			return by_digits[0]
	return None


def _post_wc_order_note(woo_order_id: str, note: str, site: str) -> None:
	"""Best-effort: attach a private order note on the WooCommerce order so the
	person who typed the serial sees the rejection where they typed it. A WC API
	failure is logged and swallowed — feedback must never break webhook
	processing."""
	from ffl_woo_sync.woocommerce.client import WooCommerceClient
	try:
		client = WooCommerceClient.from_settings(site)
		client.create_order_note(woo_order_id, note)
	except Exception as exc:
		_log_error(
			title=f"Woo webhook: order note failed for order {woo_order_id}",
			message=str(exc),
		)


def _queue_wc_order_note(woo_order_id: str, note: str, site: str) -> None:
	"""Deliver a private WC order note AFTER the current transaction commits.

	Posting inline meant a rollback past that point — a concurrent save losing
	the timestamp race, say — left the note sitting on the WooCommerce order
	while the reason it describes never landed in the database. The next pass
	then read the OLD stored reason, judged it different, and posted the very
	same note again. An externally visible side effect must never precede its own
	commit, so this rides ``enqueue_after_commit`` like every other outbound push
	on this path.
	"""
	frappe.enqueue(
		"ffl_woo_sync.woocommerce.webhook._post_wc_order_note",
		queue="short", enqueue_after_commit=True,
		woo_order_id=woo_order_id, note=note, site=site,
	)


def _reject_serial_row(order_doc, serial_row, reason: str, title: str) -> bool:
	"""Record a serial rejection on the row and close the feedback loop: Error
	Log + a WooCommerce order note. Idempotent per reason — the next
	order.updated re-delivering the same bad serial must not re-post the note
	nor dirty the order (returns False so the caller skips the save)."""
	if (serial_row.get("reservation_error") or "").strip() == reason:
		return False
	serial_row.reservation_error = reason
	_log_error(title=title, message=reason)
	site = frappe.flags.get("woo_sync_site") or "retail"
	_queue_wc_order_note(str(order_doc.woo_order_id or ""), f"POS: {reason}", site)
	return True


def _queue_rebucket(order_doc, serial_row, serial_no: str, from_item: str,
		to_item: str) -> bool:
	"""Hand a declared cross-Item pick to a background job: move the gun into the
	listing's Item, then re-apply the serial to this row. True when the row moved.

	Deliberately NOT inline. ``reassign_serial_to_item`` submits two Stock Entries
	and then commits (``ffl_core.utils.commit``); this module never commits — every
	outbound effect rides ``enqueue_after_commit`` — and a commit here would drop
	the ``for_update`` locks its caller holds over a half-processed order. Two
	ERPNext submits inside the webhook would also take Item-level locks against the
	receive path, which is the shape of the deadlock this whole incident came from.

	The row carries a plain in-flight note, NOT an error. ``reservation_error`` is
	what the Pending Order page shows staff (as a `serial_errors` entry, the same
	channel the antique "awaiting its serial" notices use — informational there
	too), and telling them to reconcile a gun POS is already moving is how a
	second person starts hand-moving it as well.

	The enqueue is UNCONDITIONAL; only the row write is guarded. A row in this
	state matches ``_STUCK_FROM_WHERE``, so the retry sweep re-enters here every
	couple of hours — which is the ONLY thing that recovers a job lost to a
	restarted worker, and skipping the enqueue on a repeat would forfeit it. The
	job is idempotent about the move itself, so a duplicate costs a no-op, while
	the guarded row write keeps order.updated's many redeliveries from dirtying
	the order or re-posting the WooCommerce note.
	"""
	note = (
		f"Serial {serial_no!r} is filed under Item {from_item!r}, which sells "
		f"through {to_item!r} — POS is moving the gun into {to_item!r} and will "
		f"apply the serial automatically. No action needed."
	)
	frappe.enqueue(
		"ffl_woo_sync.woocommerce.tasks.rebucket_and_apply_serial_task",
		queue="short", enqueue_after_commit=True,
		serial_no=serial_no, target_item=to_item,
		woo_online_order=order_doc.name,
		serial_row_name=serial_row.get("name"),
		site=frappe.flags.get("woo_sync_site") or "retail",
	)
	if (serial_row.get("reservation_error") or "").strip() == note:
		return False
	serial_row.reservation_error = note
	return True


def _named_by_sibling_row(order_doc, this_row, serial: str) -> bool:
	"""True when ANOTHER serial row of this same order still names ``serial``.

	Guards the release in :func:`_apply_dealer_serial`. Both columns count: a
	sibling holding it as a placeholder (`stock_hold_serial`) is still holding it,
	and one that has confirmed it (`confirmed_serial`) is a sold gun.

	Rows are compared by their child-row `name`, not by identity or index — the
	caller may hand us a row read back from the database rather than the very
	object in `order_doc.serials`.
	"""
	mine = this_row.get("name")
	for row in (order_doc.get("serials") or []):
		if row.get("name") and row.get("name") == mine:
			continue
		if serial in {(row.get("stock_hold_serial") or "").strip(),
				(row.get("confirmed_serial") or "").strip()}:
			return True
	return False


def _held_elsewhere_reason(serial_no: str, held: dict) -> str:
	"""The staff-facing refusal when a serial belongs to another channel's order.

	Two different actions, so two different sentences. Another WEB order can let
	the gun go and POS re-applies the serial by itself, so staff are told to wait
	or pick another. A marketplace sale cannot be released at all — GunBroker
	gives the seller no cancel — so there the only action is to pick a different
	gun, and saying "release it there" would send somebody looking for a button
	that does not exist.
	"""
	ref = held.get("ref") or "(unknown)"
	if held.get("channel") == "woo":
		return (
			f"Serial {serial_no!r} is already reserved for another web order "
			f"({ref}) — release it there or pick another. "
			f"Once that order lets the gun go, POS re-applies this serial on its "
			f"own — no need to re-enter it here."
		)
	return (
		f"Serial {serial_no!r} has already been sold on {held.get('channel')} "
		f"(order {ref}) and is no longer available — pick a different gun for "
		f"this order. That sale cannot be cancelled from our side."
	)


def _apply_dealer_serial(order_doc, serial_row, dealer_serial: str) -> bool:
	"""Pin a dealer-entered serial onto a Woo Online Order Serial row, overriding
	any placeholder held for stock or any prior confirmed serial. Validates the
	serial is a real, in-stock ``Serial No`` for the row's item; on failure it
	records a ``reservation_error`` on the row.

	One mismatch is not a failure: when the serial's Item declares it sells through
	the row's Item (:func:`_sells_through`), staff picked out of a sister pool the
	store has said feeds this listing, and the move is queued instead of refused.

	Releases the row's ``stock_hold_serial`` and any prior ``confirmed_serial``
	ONLY when they differ from ``dealer_serial`` — the placeholder may already be
	the gun the dealer names, and releasing it would drop the reservation we just
	confirmed. Returns True when the row was mutated.
	"""
	dealer_serial = (dealer_serial or "").strip()
	if not dealer_serial:
		return False
	# Originating store for this order.updated (set by woo_event; defaults retail).
	site = frappe.flags.get("woo_sync_site") or "retail"
	item_code = (serial_row.get("item_code") or "").strip()
	current = (serial_row.get("confirmed_serial") or "").strip()
	hold = (serial_row.get("stock_hold_serial") or "").strip()

	# BEFORE any Serial No lock: eviction can make this transaction hold two guns
	# at once, and two webhooks doing that in opposite order deadlock (measured
	# 2/2). One mutex per Item, always first, gives every such transaction the
	# same lock order.
	_lock_item_reservations(item_code)

	# Row-locked, mirroring _pick_oldest_unreserved_serial and
	# _serial_unavailable_reason: read-then-reserve without the lock lets a
	# concurrent writer take the same gun in between, and both orders then believe
	# they hold it. That race used to need two simultaneous webhooks; the retry
	# sweep made it an unattended writer that runs 12 times a day, so the lock is
	# no longer optional. Held until the caller's transaction commits.
	sn = frappe.db.get_value(
		"Serial No", dealer_serial,
		["item_code", "status", "warehouse", "is_transfer_gun", "consigned_out",
		 *channel_reserve_columns()], as_dict=True,
		for_update=True,
	)
	# A customer's transfer gun shares the SKU a web order names by design, and
	# it is Active and never woo_reserved — nothing below would rule it out (nor
	# a gun out on consignment). Refused before the sister-pool re-bucket too, so
	# such a gun is never re-filed or reserved.
	not_ours = _not_ours_reason(dealer_serial, sn) if sn else ""
	if not_ours:
		return _reject_serial_row(
			order_doc, serial_row, not_ours,
			title=f"Woo webhook: dealer serial {dealer_serial!r} is not the store's to sell",
		)
	# Answered once, off the locked row, and reused by all three "is someone else
	# holding this gun?" tests below. `held` is None when nothing holds it.
	held = channel_reservation(dealer_serial, row=sn) if sn else None
	# The Woo half keeps its ORIGINAL condition, `woo_reserved AND woo_order`,
	# rather than the shorter "the flag is set". A gun with the flag but no order
	# name used to be let through here, and while `_reserve_serial` always writes
	# both together — so the state should not exist — this function is on the web
	# store's hot path and the Woo regression suite has not been run against this
	# branch. Tightening it would be a behaviour change nobody asked for, smuggled
	# in beside one that was.
	held_elsewhere = bool(held) and not (
		held["channel"] == "woo"
		and ((held["ref"] or "") == order_doc.name or not held["ref"]))
	kind, identity = _serial_identity_problem(
		dealer_serial, sn, item_code, label="Dealer-entered serial")
	# Declared cross-Item pick: this per-gun-priced pool feeds the listing the
	# order was placed against, so the mismatch is the store's ordinary picking
	# flow. Anything else that lands under `wrong_item` is a typo that happened to
	# name a real gun somewhere else, and moving THAT gun would be a fresh bug.
	if kind == "wrong_item" and _sells_through(sn.get("item_code") or "") == item_code:
		# But not a sister-pool gun ANOTHER order holds: re-bucketing physically
		# re-files the rifle under a new Item while someone else's paid order has it
		# reserved. Same answer as the pinned case below — theirs until they let it
		# go. Eviction is out of scope on purpose: a placeholder lives on the SKU
		# the order itself named, and this gun sits in a sister pool.
		if held_elsewhere:
			return _reject_serial_row(
				order_doc, serial_row, _held_elsewhere_reason(dealer_serial, held),
				title=(f"Woo webhook: dealer serial {dealer_serial!r} reserved by "
					f"another {held['channel']} order"),
			)
		return _queue_rebucket(
			order_doc, serial_row, dealer_serial,
			(sn.get("item_code") or "").strip(), item_code)
	if identity:
		reason = identity
		# A "did you mean…?" only helps when POS could not find the gun at all.
		# For wrong_item the message already names the gun and the single action
		# that fixes it; a competing suggestion would just blur that.
		if kind != "wrong_item":
			suggestion = _suggest_instock_serial(dealer_serial, item_code, order_doc.name)
			if suggestion:
				reason += (
					f" An in-stock serial with matching digits exists: {suggestion!r} — "
					f"if that is the gun, correct the serial on the WooCommerce order "
					f"and it will re-sync automatically."
				)
		return _reject_serial_row(
			order_doc, serial_row, reason,
			title=(f"Woo webhook: dealer serial {dealer_serial!r} rejected "
				f"({kind}) for {item_code}"),
		)

	gave_hold_away = False
	if held_elsewhere and held["channel"] != "woo":
		# Another channel's buyer, not another web order's placeholder. There is
		# nothing to evict: a marketplace sale is final from the seller's side, so
		# the gun staff typed is not available to this order at any priority.
		return _reject_serial_row(
			order_doc, serial_row, _held_elsewhere_reason(dealer_serial, held),
			title=(f"Woo webhook: dealer serial {dealer_serial!r} is sold on "
				f"{held['channel']}"),
		)
	if held_elsewhere:
		# Whose claim is it? A placeholder is POS's own guess and yields to the
		# rifle staff physically picked; a dealer-CONFIRMED pin is another order's
		# real sale and is the one thing this guard should ever stop. Reading only
		# woo_reserved could not tell them apart, which is what let three shelved
		# rifles deadlock three paid orders (2026-08-13).
		#
		# Our own placeholder is offered as the replacement: this is a SWAP, and
		# without the offer the evicted order was told there was no stock while
		# the gun we were about to let go went back on sale. `new_owner` hands the
		# disputed rifle over in the same breath, so it is never briefly unheld —
		# and so the replacement search cannot return it to the order we just
		# moved off it.
		moved_to = _evict_placeholder_hold(
			dealer_serial, item_code, sn["woo_order"],
			new_owner=order_doc.name, preferred_replacement=hold,
		)
		if moved_to is None:
			return _reject_serial_row(
				order_doc, serial_row, _held_elsewhere_reason(dealer_serial, held),
				title=f"Woo webhook: dealer serial {dealer_serial!r} reserved by another order",
			)
		gave_hold_away = bool(hold) and moved_to == hold

	# Idempotent: already pinned + reserved for this very order.
	if current == dealer_serial and sn.get("woo_reserved") and sn.get("woo_order") == order_doc.name:
		return False

	_reserve_serial(dealer_serial, order_doc.name)
	# Release the placeholder / prior confirmed only if different from the new one
	# — never the placeholder an eviction just handed to the other order (that
	# would strip the reservation we made on its behalf and relist a gun that is
	# now spoken for), AND never one a sibling row of this same order still
	# names.
	#
	# `_release_serial` clears woo_reserved/woo_order and pushes with relist=True —
	# it is explicitly the "this gun is for sale again" path. On a two-unit order
	# where staff type the rifles in physical order, row 1's serial differs from
	# row 0's, so the old rule released row 0's gun: a rifle already confirmed on a
	# paid order went back into the free pool and past the counter's woo_reserved
	# gate, and stayed there until dispose issued it — which can be days while the
	# FFL is chased. Sibling check: one predicate, no new query — the order doc is
	# already in hand.
	if (hold and hold != dealer_serial and not gave_hold_away
			and not _named_by_sibling_row(order_doc, serial_row, hold)):
		_release_serial(hold, site=site)
	if current and current != dealer_serial and not _named_by_sibling_row(
			order_doc, serial_row, current):
		_release_serial(current, site=site)
	serial_row.confirmed_serial = dealer_serial
	serial_row.stock_hold_serial = ""
	serial_row.reservation_error = ""
	_enqueue_serial_or_item_push(dealer_serial, site=site)
	return True


def _apply_serials_from_woo(order, line_items: list | None = None, *,
		fetched: tuple[dict[str, list[str]], str] | None = None) -> bool:
	"""Apply the dealer-entered serials WooCommerce holds for *order* onto its
	serial rows, via the idempotent :func:`_apply_dealer_serial` path.

	``line_items`` is the webhook body's line items when a webhook is in hand.
	Pass ``None`` to read the serials straight from the WC REST API instead — that
	is what lets :func:`ffl_woo_sync.woocommerce.tasks.retry_stuck_serial_reservations`
	re-run this with no webhook at all. That retry matters because a rejection is
	only ever a snapshot: the gun a row was refused is often free hours later, and
	a Woo order that has gone ``completed`` sends no further ``order.updated`` to
	drive the retry.

	Three guards, all of them there to stop a gun being pinned to an order that
	should not hold it:

	* **Still pending in POS** — a disposed/cancelled order's serials are already
	  out of stock.
	* **Local lanes only** — a drop-shipped unit ships from the distributor's
	  warehouse, so a dealer-typed serial on one "names a gun that is not ours"
	  (the create path says exactly this and skips those rows). Re-applying one
	  would reserve OUR stock against a unit we never handle. Uses the
	  fulfillment queue's own canonical lane filter rather than a second opinion.
	* **Still alive on Woo** — whenever we pay for the REST read we also learn the
	  live status. A terminal one means the order died and the cancel webhook that
	  should have released its serials never arrived; skip and report loudly.
	  POS's own ``order_status`` cannot answer this: the only thing that ever
	  writes ``Cancelled`` is that same missing webhook.

	Returns True when a row changed.
	"""
	if order.order_status != "Pending Fulfillment" or not order.serials:
		return False
	# Canonical definition of "rows this store disposes itself" lives with the
	# fulfillment queue that acts on them; imported in-function to avoid a cycle.
	from ffl_woo_sync.woocommerce.fulfillment import _locally_disposed_rows
	rows = _locally_disposed_rows(order.serials)
	if not rows:
		return False
	line_serials = _extract_line_serials(line_items or [])
	if not line_serials:
		# Protected/empty payload — or no webhook at all → REST.
		# ``fetched`` lets a caller do that read BEFORE taking any row lock: the
		# sweep must not hold an order lock across a call that can run ~90s, or a
		# concurrent webhook stalls behind it and its event is lost outright.
		if fetched is not None:
			line_serials, woo_status = fetched
		else:
			line_serials, woo_status = _fetch_order_serials_and_status(
				str(order.woo_order_id or "").strip())
		if woo_status in _RELEASE_STATUSES:
			_log_error(
				title=f"Woo order {order.name} is {woo_status!r} on Woo but pending in POS",
				message=(
					f"Refused to re-apply serials: WooCommerce reports "
					f"{woo_status!r} while POS still holds {order.name} in "
					f"{order.order_status!r}. The cancel/refund webhook was almost "
					f"certainly missed, so this order's soft-reserved serials may "
					f"still be held against a dead order — verify and release them."
				),
			)
			return False
	changed = False
	for row in rows:
		provided = line_serials.get(str(row.woo_line_id)) or []
		idx = int(row.unit_index or 0)
		if idx < len(provided) and provided[idx] and _apply_dealer_serial(order, row, provided[idx]):
			changed = True
	return changed


def _recapture_ffl_if_empty(order, body: dict) -> bool:
	"""Fill in an EMPTY receiving FFL from a later order webhook.

	Store-API (block) checkout fires order.created/updated before the storefront plugin commits
	the FFL meta, so the create path can land an empty FFL; the data arrives on a
	later order.updated. Populate it then — but ONLY when the stored FFL is still
	empty, never clobbering a staff-entered value. Routes through ``_extract_ffl``
	so the ``00`` expiration placeholder is spliced to the real code, and falls
	back to a REST refetch when the webhook body carried no FFL meta. Returns True
	when the order was filled in.
	"""
	if (order.ffl_license or "").strip():
		return False
	ffl = _extract_ffl(body.get("meta_data") or [])
	if _ffl_is_empty(ffl):
		ffl = _fetch_order_ffl(str(body.get("id") or "").strip())
	if _ffl_is_empty(ffl):
		return False
	order.ffl_license = ffl["license"]
	_set_ffl_details(order, ffl)
	if ffl["_raw"]:
		order.ffl_raw = json.dumps(ffl["_raw"])
	return True


_FFL_SIGNAL_REV_KEY = "ffl_core_ffl_rev"
_FFL_SIGNAL_STATE_KEY = "ffl_core_ffl_state"
_FFL_SIGNAL_STATES = frozenset({"ffl", "cr", "none"})

# The seven flat FFL columns a Woo-side change owns outright, plus the two
# derived/raw ones. Listed once so "remove" and "overwrite" cannot drift apart.
_FFL_VALUE_FIELDS: tuple[str, ...] = (
	"ffl_license", "ffl_business_name", "ffl_phone", "ffl_premise_street",
	"ffl_premise_city", "ffl_premise_state", "ffl_premise_zip", "ffl_expiration",
	"ffl_raw",
)


def _set_ffl_details(order, ffl: dict) -> None:
	"""Copy an ``_extract_ffl`` result onto the order's FFL columns. The license
	and ``ffl_raw`` are left to the caller: the authoritative path writes a
	validated license, and recapture keeps the stored raw when the new one is empty."""
	order.ffl_business_name = ffl["business_name"]
	order.ffl_phone = ffl["phone"]
	order.ffl_premise_street = ffl["street"]
	order.ffl_premise_city = ffl["city"]
	order.ffl_premise_state = ffl["state"]
	order.ffl_premise_zip = ffl["zip"]
	order.ffl_expiration = ffl["expiration"]


def _extract_ffl_signal(meta_data: list | None) -> dict:
	"""Read the Woo-side authority signal: ``{"rev": int|None, "state": str|None}``.

	Either being None means "no signal", and no signal means every FFL field is
	left exactly as it is. Malformed values deliberately read as no-signal
	rather than as ``rev=0``: a 0 would compare as stale forever after and
	silently suppress the real update that follows.
	"""
	flat: dict[str, str] = {}
	for m in (meta_data or []):
		if isinstance(m, dict) and m.get("key"):
			flat[m["key"]] = str(m.get("value") or "").strip()

	rev = None
	raw_rev = flat.get(_FFL_SIGNAL_REV_KEY, "")
	if raw_rev:
		try:
			rev = int(raw_rev)
		except (TypeError, ValueError):
			rev = None
	# Contract C1 numbers revisions from 1, so anything below that is not a
	# revision. Treating it as one would be worse than ignoring it: rev 0 would
	# compare `0 <= 0` against the default and be dropped as a replay anyway, but
	# only after the int/str path had already decided that WooCommerce's `0` and
	# `"0"` mean different things. One rule, stated once.
	if rev is not None and rev < 1:
		rev = None

	state = flat.get(_FFL_SIGNAL_STATE_KEY, "").lower() or None
	if state not in _FFL_SIGNAL_STATES:
		state = None
	return {"rev": rev, "state": state}


def _apply_authoritative_ffl_change(order, body: dict) -> bool:
	"""Apply a staff-made FFL change from WooCommerce — including a removal.

	This is the ONLY path allowed to overwrite an FFL that is already on the
	order, and it earns that right from a monotonic revision: a webhook may
	overwrite only when it carries a strictly higher `ffl_core_ffl_rev` than the
	one stored. Replays and out-of-order deliveries compare as stale and are
	dropped, which makes the whole thing idempotent without a dedupe table.

	"Removed" is read from the explicit `state == 'none'`, never inferred from
	the FFL fields being blank — blank is also what an ordinary status webhook
	looks like, and guessing between them is how you either ignore a real
	removal or wipe a good FFL on a refund notification.

	Returns True when it handled the payload (so the caller skips the
	fill-only-when-empty fallback); False means "no signal, not mine".
	"""
	signal = _extract_ffl_signal(body.get("meta_data"))
	if signal["rev"] is None or signal["state"] is None:
		return False
	if signal["rev"] <= int(order.woo_ffl_rev or 0):
		return False

	woo_id = str(body.get("id") or order.woo_order_id or "").strip()

	# Past the point of no return: the firearm has already left (or the order is
	# cancelled). The bound book — not this webhook — is the record of where it
	# went, so refuse the rewrite and make it visible. The revision is still
	# absorbed, or every later webhook re-raises the same alarm.
	if order.order_status != "Pending Fulfillment":
		msg = (f"WooCommerce changed the receiving FFL on order {woo_id} after "
			f"fulfilment (order status: {order.order_status}). The POS record and "
			f"the bound book were NOT changed — reconcile in FastBound by hand if "
			f"the firearm really went elsewhere.")
		order.add_comment("Comment", msg)
		frappe.log_error(title=f"Woo order {woo_id}: FFL changed after fulfilment", message=msg)
		order.woo_ffl_rev = signal["rev"]
		return True

	if signal["state"] == "none":
		for field in _FFL_VALUE_FIELDS:
			setattr(order, field, "")
		order.is_cr_transfer = 0
		order.atf_ffl_record = ""
		_clear_cr_verification(order)
	else:
		ffl = _extract_ffl(body.get("meta_data") or [])
		if _ffl_is_empty(ffl) and woo_id:
			# The state asserts an FFL exists; protected meta may simply have
			# been withheld from the webhook body.
			ffl = _fetch_order_ffl(woo_id)
		if _ffl_is_empty(ffl):
			frappe.log_error(
				title=f"Woo order {woo_id}: FFL state {signal['state']!r} but no FFL data",
				message=("The order claims a receiving FFL but none could be read from the "
					"webhook body or the REST fallback. Keeping the previously stored FFL: "
					"clearing it here would be indistinguishable from a removal nobody asked "
					"for."))
			return False

		new_license = (ffl["license"] or "").strip()
		old_license = (order.ffl_license or "").strip()

		# Refuse a malformed number rather than store it. Storing it would make
		# the order's own validate() throw on the very next save — and that save
		# is the one that also applies serial backfill, status and refunds, so a
		# single bad licence from Woo would stall the whole order instead of just
		# its FFL. The revision is deliberately NOT consumed, so a corrected
		# resend at the same rev still lands.
		if not FFL_NUMBER_RE.match(new_license):
			frappe.log_error(
				title=f"Woo order {woo_id}: malformed FFL number rejected",
				message=(f"POS order {order.name} (Woo #{woo_id}): WooCommerce sent "
					f"{new_license!r} as the receiving FFL, which is not a valid ATF licence "
					f"number. The previously stored value ({old_license!r}) was kept and the "
					f"change was NOT applied — fix the number on WooCommerce and save the "
					f"order again."))
			return False

		order.ffl_license = new_license
		_set_ffl_details(order, ffl)
		order.ffl_raw = json.dumps(ffl["_raw"]) if ffl["_raw"] else ""

		# The licence NUMBER is the single source of truth for C&R, not the Woo
		# state — that eliminates a whole class of flag/number drift. The state
		# is kept only as a cross-check worth logging.
		order.is_cr_transfer = 1 if is_cr_license(new_license) else 0
		if (signal["state"] == "cr") != bool(order.is_cr_transfer):
			frappe.log_error(
				title=f"Woo order {woo_id}: C&R state disagrees with the licence number",
				message=(f"WooCommerce reported state={signal['state']!r} but licence "
					f"{new_license!r} is {'' if order.is_cr_transfer else 'not '}Type 03. "
					f"Following the licence number."))

		if new_license != old_license:
			# Both of these describe the PREVIOUS licence. `atf_ffl_record`
			# short-circuits destination resolution, so a stale one ships the
			# firearm to the old FFL; the cr_* block is a verification of the old
			# number, so a stale one pushes the previous holder's name and address
			# to FastBound under the new number.
			order.atf_ffl_record = ""
			_clear_cr_verification(order)

	# A drop-ship leg that is already placed went to the PREVIOUS destination.
	# Record the truth here, but the distributor order needs a human.
	if (order.get("dropship_status") or "").strip() in _UNSETTLED_DROPSHIP_STATUSES:
		msg = (f"The receiving FFL changed on WooCommerce order {woo_id} after the "
			f"distributor leg was already placed to the previous FFL. Check the "
			f"distributor order and redirect or cancel it.")
		order.add_comment("Comment", msg)
		frappe.log_error(
			title=f"Woo order {woo_id}: FFL changed after the distributor leg was placed",
			message=msg)

	order.woo_ffl_rev = signal["rev"]
	return True


def _auto_clear_non_firearm_if_completed(order_doc) -> bool:
	"""A web order that owes no FFL disposition needs no Dispose click — WooCommerce
	handles its shipping. Once WC marks it completed, drop it out of the POS Pending
	Fulfillment queue (-> Transferred) so such orders don't pile up there forever:
	the only other exit, dispose_web_order, requires a disposable firearm serial row
	and refuses an order without one. No-op for orders that DO owe a disposition
	(they must go through Dispose), and for orders not yet completed or already
	cleared. Returns True when it changed order_status."""
	if order_doc.order_status != "Pending Fulfillment":
		return False
	if (order_doc.woo_status or "").strip().lower() != "completed":
		return False
	# Antique units are the one kind of serial row that owes no bound-book exit: a
	# pre-1899 receiver is not a GCA firearm, so there is no disposition to book and
	# no FFL to ship to. Every other row — local, to_store, drop-ship — still has to
	# leave through its own path, so a SINGLE non-antique row keeps the order out of
	# auto-clear exactly as before this test learned about antiques (an empty
	# `serials` table still falls straight through, unchanged).
	if any(not is_antique_item(sr.item_code) for sr in (order_doc.serials or [])):
		return False
	# …and a LOCALLY HELD antique that never got a serial owes one before it may
	# leave. Its stock-out (`antique._issue_antique_units`) skips a row that names
	# no gun, so clearing here would book the revenue while the rifle stayed on the
	# shelf. Under the old placeholder the orphaned reservation hid that by keeping
	# the gun off the storefront for ever; a claimed unit is released the moment
	# this order stops being Pending Fulfillment, so a SOLD antique would go back
	# on sale. Hold it in the queue as `awaiting_serial` instead.
	#
	# Scoped to the lanes that hold local stock, and keyed on `confirmed_serial`
	# ALONE — both for the same reason: this guard must test exactly what
	# `_issue_antique_units` tests (antique.py: local lanes, `confirmed_serial`),
	# or it refuses orders that exit cleanly and passes ones that do not. A
	# drop-ship row is blank/blank BY DESIGN (nothing here is ours to hold), so
	# without the lane filter an all-antique drop-ship order would fail this on
	# every pass and hang in the queue for ever, unbadged — `unassigned_web_claims`
	# documents the same trap for the sibling query. A hold-only row is skipped by
	# the stock-out too, so letting it through here would produce the very outcome
	# this guard exists to prevent.
	from ffl_woo_sync.woocommerce.fulfillment import _LOCALLY_DISPOSED_LANES

	if any(
		(sr.get("fulfillment") or LANE_LOCAL) in _LOCALLY_DISPOSED_LANES
		and not (sr.get("confirmed_serial") or "").strip()
		for sr in (order_doc.serials or [])
	):
		return False
	# #3 — a drop-ship order is "completed" on WooCommerce the moment the shopper
	# pays; the goods are still at the distributor. Clearing it here would mark it
	# Transferred and (since P4) book the revenue before anything shipped. Stand
	# down while a distributor order is outstanding; the completion path clears it
	# once every leg is done. Complete / blank (the ordinary local order) do not
	# hold it up.
	if (order_doc.get("dropship_status") or "").strip() in _UNSETTLED_DROPSHIP_STATUSES:
		return False
	order_doc.order_status = "Transferred"
	return True


_PLACED_STATUSES: frozenset[str] = frozenset({
	"pending", "processing", "on-hold", "completed",
})


def _reopen_if_revived(order, body: dict) -> bool:
	"""Reopen an order a 'failed' / 'trash' status cancelled here once WooCommerce
	moves it back to a placed status (the shopper retried payment on the same
	order, or staff restored it from the trash).

	_handle_order_cancelled released its guns and wrote Cancelled; nothing else
	ever writes the status back, so the paid order never reached the queue and its
	gun stayed for sale. Each local row gets its own gun back when that gun is
	still free; a placeholder that is gone becomes a claim, a confirmed gun that
	is gone is reported on the row. Distributor routing is NOT redone: the cancel
	hook may already have withdrawn the distributor order, and placing one again
	unattended could double-order — staff are told to check it.

	Returns True when the order was reopened (the caller saves it)."""
	if (order.order_status or "") != "Cancelled":
		return False
	if _order_status(body) not in _PLACED_STATUSES:
		return False
	if frappe.db.exists("Sales Invoice", {"woo_online_order": order.name, "docstatus": 1}):
		return False

	site = frappe.flags.get("woo_sync_site") or "retail"
	local_rows = [sr for sr in (order.serials or [])
		if (sr.get("fulfillment") or LANE_LOCAL) == LANE_LOCAL]
	# Per-Item mutex first, always, before any Serial No lock (see
	# _lock_item_reservations).
	for item_code in sorted({(sr.get("item_code") or "").strip() for sr in local_rows} - {""}):
		_lock_item_reservations(item_code)

	order.order_status = "Pending Fulfillment"
	for sr in local_rows:
		item_code = (sr.get("item_code") or "").strip()
		for field in ("confirmed_serial", "stock_hold_serial"):
			serial = (sr.get(field) or "").strip()
			if not serial:
				continue
			reason, _detail = _serial_availability(serial, item_code)
			if not reason:
				_reserve_serial(serial, order.name)
				_enqueue_serial_or_item_push(serial, site=site)
			elif field == "stock_hold_serial":
				sr.stock_hold_serial = ""  # POS's own guess: the row becomes a claim
			else:
				sr.reservation_error = reason
	for item_code in sorted(_claimed_items(order)):
		_push_item_stock_everywhere(item_code)
	_announce_commitments(_committed_items(order))

	if (order.get("dropship_status") or "").strip():
		_log_error(
			title=f"Woo order {order.woo_order_id} revived after cancel: check distributor routing",
			message=(
				f"{order.name} was cancelled on a '{order.woo_status}' status and "
				f"WooCommerce has moved it back to '{_order_status(body)}'. Its "
				f"distributor order may have been withdrawn on cancel — re-check and "
				f"re-route it by hand."),
		)
	return True


def _handle_order_updated(body: dict) -> None:
	"""Apply dealer-entered serials to an EXISTING Woo Online Order — the
	order.updated path after a dealer saves a serial on the WC Edit Order screen.

	Reads ``firearm_serials`` from the webhook line items; because that key is
	protected (underscore-prefixed) and may be omitted from the payload, it
	refetches via REST when the body carried line items but no serials. No-op
	when nothing is supplied or the order isn't on file.
	"""
	woo_order_id = str(body.get("id") or "").strip()
	if not woo_order_id:
		return

	# Site-scope the lookup: (woo_order_id, site) is the order's unique key.
	site = frappe.flags.get("woo_sync_site") or "retail"
	order_name = frappe.db.get_value(
		"Woo Online Order", {"woo_order_id": woo_order_id, "site": site}, "name"
	)
	if not order_name:
		return

	order = frappe.get_doc("Woo Online Order", order_name)

	# Before the status mirror below: the revive message names the old status.
	revived = _reopen_if_revived(order, body)

	# Always mirror the live WC status + amounts onto the order (status changes,
	# edits), regardless of fulfillment state — order-level and per-line.
	changed = _sync_woo_status_and_pricing(order, body) or revived
	if _sync_line_amounts(order, body.get("line_items") or []):
		changed = True

	# An authoritative FFL change from Woo (staff used Change FFL / Add C&R /
	# Remove FFL) — the only path allowed to OVERWRITE, including with nothing.
	# Runs first: when it handles the payload the fill-only-when-empty fallback
	# below must not run, or a removal would immediately be re-filled from the
	# same body's meta.
	if _apply_authoritative_ffl_change(order, body):
		changed = True
	# Store-API draft race: the create path may have landed an empty FFL; a later
	# update carries it. Fill it in (non-destructive — only when still empty).
	elif _recapture_ffl_if_empty(order, body):
		changed = True

	# m6: the buyer/CS may correct the shipping address on Woo after checkout; the
	# direct drop-ship leg ships from it, so re-capture it while the order still
	# awaits routing (frozen once ordered).
	if _refresh_ship_to_if_unrouted(order, body):
		changed = True

	# A non-firearm order completed on WooCommerce has no FFL disposition step to
	# exit through — clear it from the Pending Fulfillment queue (firearm orders
	# are untouched; they still go through Dispose).
	auto_cleared = _auto_clear_non_firearm_if_completed(order)
	if auto_cleared:
		changed = True

	# Apply whatever serial Woo holds for this order. The pending-only guard and
	# the REST fallback live in the helper — which the stuck-row retry cron reuses
	# to run this same path when no webhook is coming.
	if _apply_serials_from_woo(order, body.get("line_items")):
		changed = True

	# WooCommerce partial refunds (D1): record any NEW refund on the order (deduped
	# by Woo refund id). When one landed — or a refund's tax/shipping breakdown is
	# still unread — enqueue the SI reconcile AFTER commit: the breakdown read (a
	# REST call) and the cancel+amend are too heavy for the webhook response, and
	# enqueue-after-commit means the refund row is already persisted when the job
	# runs (so a job failure is retryable, never a lost refund). No-op on orders
	# with no refunds.
	from ffl_woo_sync.woocommerce.refunds import has_pending_breakdown, record_order_refunds
	new_refund = record_order_refunds(order, body)
	if new_refund:
		changed = True
	if new_refund or has_pending_breakdown(order):
		# One job per order: redeliveries while it waits add nothing. A refund
		# that lands while the job is already RUNNING is dropped here (Frappe
		# dedupes started jobs too) — but the job re-reads the order under its
		# lock, and anything it still missed is a Pending row the 2-hourly
		# backstop sweeps (order_audit._sweep_pending_refund_breakdowns).
		frappe.enqueue(
			"ffl_woo_sync.woocommerce.refunds._reconcile",
			queue="long",
			enqueue_after_commit=True,
			job_id=f"refund-reconcile-{order.name}",
			deduplicate=True,
			order=order.name,
		)

	if changed:
		order.flags.ignore_permissions = True
		order.save()

	# #8 — an accessory order's completion event is the auto-clear above; book its
	# revenue. Enqueued after the save so the job reads back a persisted order.
	if auto_cleared:
		_book_revenue_after_auto_clear(order)


# ---------------------------------------------------------------------------
# order cancel / delete / refund handler (release soft-reserve)
# ---------------------------------------------------------------------------

# WooCommerce order statuses that must NOT hold a soft-reserve → release any
# reserved serials (or skip reserving for a new order).  The WC REST/webhook
# order body reports `status` WITHOUT the internal ``wc-`` prefix (e.g.
# "cancelled", "refunded", "checkout-draft").  Two groups:
#   • Terminal — the order is dead: cancelled / canceled / refunded / trash /
#     failed (payment failed; if the shopper retries, the later pending/
#     processing event re-reserves).
#   • Pre-placement — checkout-draft / draft / auto-draft: WooCommerce's block /
#     Store-API checkout creates a DRAFT order (and fires order.created/updated)
#     the moment a shopper reaches the checkout page, BEFORE they place it.
#     Reserving here would hold a firearm just for someone browsing checkout
#     (the "stock shows 2 not 3" phantom). A serial is reserved only once the
#     order is actually placed: pending / processing / on-hold / completed.
# Both English/American spellings of cancel are accepted defensively.
_RELEASE_STATUSES: frozenset[str] = frozenset({
	"cancelled", "canceled", "refunded", "trash", "failed",
	"checkout-draft", "draft", "auto-draft",
})


def _order_status(body: dict) -> str:
	return (body.get("status") or "").strip().lower()


def _handle_order_cancelled(body: dict) -> None:
	"""Release soft-reserved serials when a Woo order is cancelled / deleted / refunded.

	Looks up the ``Woo Online Order`` by ``woo_order_id``, marks it
	``order_status="Cancelled"``, releases every ``Serial No`` reserved for it
	(``woo_reserved=0``, ``woo_order=""``), and enqueues an outbound stock
	re-push for each affected product so WooCommerce reflects the freed
	availability.

	Without this, a cancelled/deleted Woo order leaves the POS soft-reserve
	stuck — the physical firearm shows as unavailable forever (the phantom
	reservation D1 root cause).

	Idempotent: if no matching ``Woo Online Order`` exists (already cleaned up,
	or never recorded), this is a no-op ack.

	Mirrors :func:`_handle_order_created`'s outbound enqueue logic: a serial that
	has its own ``woo_product_id`` re-pushes via ``push_serial_task``; otherwise
	the parent item (uniform / non-firearm) re-pushes via ``push_stock_task``.
	"""
	woo_order_id = str(body.get("id") or "").strip()
	if not woo_order_id:
		_log_error(
			title="Woo webhook: order cancel/delete missing id",
			message=json.dumps(body)[:3000],
		)
		return

	# Site-scope the lookup ((woo_order_id, site) is the order's unique key); read
	# back the order's own site to thread the re-push to the store the cancelled
	# order belongs to.
	site = frappe.flags.get("woo_sync_site") or "retail"
	order = frappe.db.get_value(
		"Woo Online Order",
		{"woo_order_id": woo_order_id, "site": site},
		["name", "site"],
		as_dict=True,
	)
	if not order:
		return  # nothing recorded for this order — ack, no-op
	order_name = order["name"]
	order_site = order.get("site") or "retail"

	# D1: a terminal WC status (cancelled / trash / fully refunded) landing on an
	# order whose sale was already booked must never be silent. Alert loudly —
	# never auto-reverse: reversing a shipped/transferred firearm's sale (its
	# bound-book disposition still stands) is a human decision.
	from ffl_woo_sync.woocommerce.refunds import alert_if_invoiced
	alert_if_invoiced(frappe._dict(
		name=order_name,
		woo_order_id=woo_order_id,
		woo_status=_order_status(body) or "cancelled",
	))

	pid_field = _f(order_site, "product_id")

	# Items this order claimed WITHOUT naming a rifle. Read before the status write
	# below, because that write is what drops the claim: it takes the row out of
	# `Pending Fulfillment`, which is the whole scope of
	# `mapping.unassigned_web_claims`. So the claim frees itself correctly — only
	# the storefront push is missing, since the release loop below has no serial to
	# iterate. Not an edge case: `_RELEASE_STATUSES` includes `checkout-draft`,
	# which fires when a shopper merely opens checkout.
	#
	# Kept as SQL rather than `_claimed_items(frappe.get_doc(...))`. Review asked for
	# the reuse — three hand-written copies of "what a claim is" is how they stop
	# agreeing — but this handler runs on `checkout-draft`, which WooCommerce fires
	# when a shopper merely OPENS checkout, and loading a whole order doc there to
	# read one column of one child table is the wrong trade. The drift the reuse was
	# meant to prevent is covered instead by the test that pins this predicate
	# against the same fixtures as `mapping.unassigned_web_claims`.
	claimed_items = frappe.db.sql_list(
		"""
		SELECT DISTINCT s.item_code
		FROM `tabWoo Online Order Serial` s
		WHERE s.parenttype = 'Woo Online Order'
		  AND s.parent = %(order)s
		  AND COALESCE(NULLIF(s.fulfillment, ''), 'local') = 'local'
		  AND COALESCE(TRIM(s.confirmed_serial), '') = ''
		  AND COALESCE(TRIM(s.stock_hold_serial), '') = ''
		  AND COALESCE(s.item_code, '') != ''
		""",
		{"order": order_name},
	)

	# What the other channels must hear about, read now for the same reason: the
	# status write below is what drops these lines out of the open count. Lane
	# column guarded like `mapping.pending_web_line_qty` (code can run before
	# migrate adds it).
	lane_filter = ("AND COALESCE(l.lane, '') NOT IN ('direct', 'fds')"
		if frappe.db.has_column("Woo Online Order Line", "lane") else "")
	line_items = frappe.db.sql_list(
		f"""
		SELECT DISTINCT l.item_code
		FROM `tabWoo Online Order Line` l
		WHERE l.parenttype = 'Woo Online Order'
		  AND l.parent = %(order)s
		  AND COALESCE(l.item_code, '') != ''
		  {lane_filter}
		""",
		{"order": order_name},
	)

	# Release every serial reserved against this order.
	reserved = frappe.get_all(
		"Serial No",
		filters={"woo_order": order_name},
		fields=["name", "item_code", pid_field],
	)
	for sn in reserved:
		_clear_reservation(sn["name"])

	# Mark the order cancelled (db.set_value bypasses doc_events; the
	# woo_sync_inbound flag is set at the request level for safety). Mirror the
	# raw WC status too (cancelled / refunded / failed / trash).
	frappe.db.set_value("Woo Online Order", order_name, {
		"order_status": "Cancelled",
		"woo_status": _order_status(body) or "cancelled",
	})

	# #9 — the write above is db.set_value, which fires NO doc_events, so anything
	# that must react to a cancellation has to be called explicitly. The
	# `woo_order_cancelled` hook lets ffl_integrations withdraw a
	# not-yet-placed Distributor Order (and merely alert on a placed one: RSR has
	# no cancel API). Called AFTER the status write so the consumer reads back a
	# cancelled order.
	#
	# DELIBERATE divergence from the ffl_acquisition_sync_push convention, where a
	# raising registrant rolls the caller back: releasing the soft-reserve above is
	# the safety-critical half of this handler (a stuck reservation is the phantom
	# "gun unavailable forever" bug), and the webhook must still ack. A distributor
	# bookkeeping failure is logged for a human instead of stranding a firearm.
	for fn in frappe.get_hooks("woo_order_cancelled"):
		try:
			frappe.get_attr(fn)(order_name)
		except Exception as exc:  # noqa: BLE001
			_log_error(
				title=f"woo_order_cancelled hook failed for {order_name}",
				message=f"{fn}: {exc}",
			)

	# Enqueue outbound stock re-pushes on the cancelled order's store so
	# WooCommerce reflects the freed serial(s). A serial that has its own
	# product_id on that store re-pushes via push_serial_task; otherwise the
	# parent Item re-pushes via push_stock_task (deduped on item_code).
	pushed_items: set[str] = set()
	for sn in reserved:
		if sn.get(pid_field):
			frappe.enqueue(
				"ffl_woo_sync.woocommerce.tasks.push_serial_task",
				queue="long",
				enqueue_after_commit=True,
				serial_no=sn["name"],
				site=order_site,
				# A release: the gun is for sale again, same as _release_serial.
				relist=True,
			)
		else:
			item_code = sn.get("item_code")
			if not item_code or item_code in pushed_items:
				continue
			pushed_items.add(item_code)
			parent_woo_id = frappe.db.get_value("Item", item_code, pid_field) or ""
			if parent_woo_id:
				frappe.enqueue(
					"ffl_woo_sync.woocommerce.tasks.push_stock_task",
					queue="long",
					enqueue_after_commit=True,
					item_code=item_code,
					site=order_site,
				)

	# The claimed units freed themselves when the status write above took this
	# order out of `Pending Fulfillment`, but no serial exists for the loop above
	# to have pushed. Tell the storefront(s) the unit is back.
	for item_code in sorted(set(claimed_items) - pushed_items):
		_push_item_stock_everywhere(item_code)

	# And tell the other channels their share of the pool just grew back.
	_announce_commitments(set(claimed_items) | set(line_items))


def _handle_order_upsert(body: dict) -> None:
	"""Route ``order.created`` / ``order.updated`` by the order's status.

	Reserve serials ONLY for actually-placed orders (pending / processing /
	on-hold / completed).  Any status in :data:`_RELEASE_STATUSES` — terminal
	(cancelled / refunded / trash / failed) OR pre-placement (checkout-draft /
	draft / auto-draft, which WC fires merely when a shopper opens checkout) —
	releases the soft-reserve instead of creating / reserving.  This is what
	keeps a draft checkout from holding a firearm out of stock.
	"""
	if _order_status(body) in _RELEASE_STATUSES:
		_handle_order_cancelled(body)
	else:
		_handle_order_created(body)


_HANDLERS: dict[str, Any] = {
	# order.created / order.updated both route through _handle_order_upsert,
	# which checks the order status: a cancelled/refunded/trashed body releases
	# the soft-reserve; anything else creates + reserves.  _handle_order_created
	# early-returns when a Woo Online Order already exists for this woo_order_id
	# (idempotent).
	# Consequence: FFL/customer data that arrives only on a later order.updated
	# (e.g. customer fills in FFL after payment) is NOT re-captured.  This is
	# acceptable — staff can update the Woo Online Order manually via Desk.
	# POS is the source of truth once the order is created.
	"order.created": _handle_order_upsert,
	"order.updated": _handle_order_upsert,
	# A hard delete in WooCommerce → release any soft-reserve for the order.
	"order.deleted": _handle_order_cancelled,
}


# ---------------------------------------------------------------------------
# Webhook entry point
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["POST"])
def woo_event() -> dict:
	"""Single WooCommerce webhook entry point (guest, HMAC-verified).

	Payload:
	    WooCommerce's webhook JSON body; the X-WC-Webhook-Topic header
	    selects the handler (order.created/updated, product.*, ...). The
	    X-WC-Webhook-Signature header must carry a valid HMAC over the raw
	    body (secret configured in Woo Sync Settings).

	Returns:
	    200 with a minimal JSON body ({ok, topic/handled/error}) on success.
	    400 when ``?site=`` names a store this POS does not run (only the
	    retail store is connected) and 401 when HMAC verification fails — the
	    two cases that mean "stop delivering here", which is what a 4xx gets
	    WooCommerce to do; the order is not read or processed for either. Every other
	    outcome logs (where there is anything worth logging) and returns 200,
	    so WooCommerce does not retry or auto-disable the webhook over
	    something the caller cannot fix.
	"""
	raw_body = _read_raw_body()
	sig = (
		frappe.get_request_header("X-WC-Webhook-Signature")
		or frappe.get_request_header("x-wc-webhook-signature")
		or ""
	)

	# Resolve the originating store for this webhook. A store this POS does not
	# run gets a 400 before any secret is read or the order touched: processed as
	# retail it would be a double-sell, and WooCommerce disabling a webhook after
	# repeated 4xx is the right end for one.
	site = _origin_site()
	if site is None:
		frappe.local.response["http_status_code"] = 400
		frappe.logger("woo.webhook", allow_site=True).info(
			"woo_event: refused a webhook for a store this POS does not run")
		return {"ok": False, "error": "unknown store"}

	try:
		secret = _get_webhook_secret(site)
		_verify_hmac(raw_body, sig, secret)
	except WebhookAuthError as exc:
		frappe.local.response["http_status_code"] = 401
		return {"ok": False, "error": str(exc)}
	except Exception as exc:
		# Config error (missing Settings, DB blip, etc.) — log but return 200 so
		# WooCommerce does not count this toward webhook retry / auto-disable.
		_log_error(
			title="Woo webhook: auth configuration error",
			message=f"{exc}\n\n{_tb.format_exc()}",
		)
		return {"ok": False, "error": "configuration error"}

	try:
		body = json.loads(raw_body or b"{}")
	except ValueError:
		_log_error(
			title="Woo webhook: non-JSON body",
			message=(raw_body[:5000] if raw_body else b"").decode("utf-8", errors="replace"),
		)
		return {"ok": False, "error": "non-JSON body"}

	topic = (
		frappe.get_request_header("X-WC-Webhook-Topic")
		or frappe.get_request_header("x-wc-webhook-topic")
		or (body or {}).get("topic")
		or ""
	).strip().lower()

	handler = _HANDLERS.get(topic)
	if handler is None:
		_log_error(
			title=f"Woo webhook: unhandled topic {topic!r}",
			message=json.dumps(body)[:3000],
		)
		return {"ok": True, "handled": False, "topic": topic}

	# Inbound loop guard + originating site for the entire handler body, set and
	# restored by the shared context manager (see :func:`woo_site`).
	try:
		with woo_site(site):
			handler(body)
			frappe.db.commit()
	except Exception as exc:
		frappe.db.rollback()
		_log_error(
			title=f"Woo webhook handler failed for topic {topic!r}",
			message=f"{exc}\n\n{_tb.format_exc()}\n\nBody: {json.dumps(body)[:3000]}",
		)
		return {"ok": False, "error": "internal", "topic": topic}

	return {"ok": True, "topic": topic}
