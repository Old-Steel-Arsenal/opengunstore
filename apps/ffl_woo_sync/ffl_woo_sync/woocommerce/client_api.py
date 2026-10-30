"""Whitelisted helpers used by the WooCommerce Settings client script and Item form."""
from __future__ import annotations

import frappe
from frappe import _

from ffl_core.roles import STOCK_ROLES, SYSTEM_ROLES

from ffl_core.firearm import _is_firearm_row
from ffl_woo_sync.woocommerce.client import (
	WooCommerceClient, WooCommerceConfigError, WooCommerceError, assert_store_site)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _skipped_response(result: dict) -> dict:
	"""Turn a guard's skip dict into an operator-facing answer.

	A refusal must never come back as ``ok: True`` with an empty product id —
	the form's callback would announce a successful push that never happened,
	which is precisely the kind of silent no-op that let mispriced guns sit on
	the storefront unnoticed (the "make the error visible" half).

	Returned rather than thrown ON PURPOSE: ``frappe.throw`` rolls the request
	back, which would erase the ``{site}_sync_error`` the guard just wrote — so
	the operator would see the reason once and the Serial No form would forget
	it. The client scripts surface ``ok: False`` with this message instead.
	"""
	reason = result.get("skipped") or ""
	return {
		"ok": False,
		"skipped": reason,
		"message": _skip_message(reason),
		"woo_product_id": "",
	}


def _skip_message(reason: str) -> str:
	"""Operator-facing text for a guard refusal.

	Built per call rather than in a module-level dict: `_()` needs a request's
	language context, which does not exist at import time.

	The ``delisted`` text names the per-gun route on purpose. A per-serial
	firearm cannot be relisted from the Item button — ``push_item_now``'s
	``_relist`` applies to the uniform/non-firearm branch only, by design
	(#5: an Item-level bulk relist would also revive guns hidden individually
	for a hold, a repair, or a customer callback). So the message has to point
	at the route that does work, or the operator is left with a refusal and no
	next step.
	"""
	return {
		"unpriced": _(
			"Not sent to WooCommerce: no sell price is set, and the store would "
			"list this at $0. Set a price first, then send it again."
		),
		"delisted": _(
			"Not sent to WooCommerce: this product was taken off the store by "
			"hand. Open the Serial No and use 'Send to WooCommerce' there to put "
			"this specific gun back on sale."
		),
		# No "…then send it again" here, deliberately: unlike the other two, this
		# refusal has no operator remedy and must not read as though it does. The
		# gun belongs to a customer; the only thing that clears it is the gun
		# ceasing to be a transfer.
		"transfer_custody": _(
			"Not sent to WooCommerce: this is a customer's firearm held for an "
			"FFL transfer, not store inventory. It cannot be listed for sale."
		),
		# Remedy is the consignment's, not this button's: the gun lists again
		# once it is back from the partner store.
		"consigned_out": _(
			"Not sent to WooCommerce: this firearm is out on consignment at a "
			"partner dealer. It can be listed again once it is returned to the store."
		),
		# No operator remedy on THIS record, deliberately: the fix is not to
		# "send it again" but to list the whole Item once. A per-serial product
		# for a uniform-priced firearm double-counts stock against the item-level
		# product (the 2026-07-21 flood) — so the message points at the item-level
		# route, not back at this button.
		"uniform_item": _(
			"Not sent to WooCommerce: this Item uses Uniform Pricing, so it is "
			"listed as a single product with a quantity in stock — individual "
			"serial numbers are not listed separately. List it from the Item form "
			"instead."
		),
	}.get(reason) or _("Not sent to WooCommerce ({0}).").format(reason)


def _push_serial_list(
	serials: list[str],
	*,
	sync,
	client: WooCommerceClient,
	attr_ids: dict,
	site: str = "retail",
) -> tuple[list[str], list[dict], list[dict]]:
	"""Push each serial in *serials*, aggregating results instead of re-raising.

	Returns ``(pushed, errors, skipped)``:

	* *pushed*  — serial names the store actually accepted;
	* *errors*  — ``{"serial_no", "error"}`` for serials that raised;
	* *skipped* — ``{"serial_no", "reason", "message"}`` for serials a GUARD
	  refused.

	The third bucket exists because both guards **return** a skip dict instead of
	raising (``sync.push_serial``), so a refusal used to land in *pushed* and the
	Item form announced a green "Pushed 2/2 serial(s)" over guns that were still
	draft on the store. That is the same silent no-op this branch exists to
	remove — and it hit exactly the shape of the incident, since a per-serial
	firearm (both SG 551s) reaches WooCommerce through this function.

	This is the **aggregating** variant — every serial is attempted regardless
	of earlier failures.  Compare with
	:func:`ffl_woo_sync.woocommerce.sync.push_all_serials` which is
	**fail-fast** (re-raises the first per-serial exception immediately).
	"""
	pushed: list[str] = []
	errors: list[dict] = []
	skipped: list[dict] = []
	for sn in serials:
		try:
			result = sync.push_serial(sn, client=client, attr_ids=attr_ids, site=site) or {}
			reason = result.get("skipped")
			if reason:
				skipped.append({
					"serial_no": sn, "reason": reason, "message": _skip_message(reason),
				})
			else:
				pushed.append(sn)
		except Exception as exc:
			errors.append({"serial_no": sn, "error": str(exc)})
	return pushed, errors, skipped


@frappe.whitelist()
def test_connection(site: str = "retail") -> dict:
	"""Probe the WooCommerce store via GET /products?per_page=1 (read auth only).

	Args:
	    site: "retail" (default; the only store).

	Returns:
	    {"ok": True, "store_url": ..., "raw": <probe info>}.

	Raises:
	    ValidationError: Settings incomplete or the API call fails — a
	        clean user-facing message in both cases.
	"""
	frappe.only_for(SYSTEM_ROLES)
	assert_store_site(site)
	try:
		client = WooCommerceClient.from_settings(site)
	except WooCommerceConfigError as exc:
		frappe.throw(_("WooCommerce is not configured: {0}").format(str(exc)))
	try:
		info = client.probe_connection()
	except WooCommerceError as exc:
		frappe.throw(_("WooCommerce API error: {0}").format(str(exc)))
	return {
		"ok": True,
		"store_url": info.get("store_url") or "",
		"raw": info,
	}


@frappe.whitelist()
def push_item_now(item_code: str, site: str = "retail") -> dict:
	"""Push (create or update) the WooCommerce product(s) for an Item.

	Routing is determined server-side so all callers (Item form button, MCP,
	future automation) behave correctly without any client-side detection:

	- **Per-serial firearm** (``is_firearm=1``, ``uniform_sell_price=0``):
	  pushes every Active Serial No via :func:`ffl_woo_sync.woocommerce.sync.push_serial`.
	  Returns ``{"ok": bool, "pushed": [...], "errors": [...], "skipped": [...],
	  "total": int}``.  This branch does **not** pass ``_relist``: a hidden gun
	  stays hidden and lands in ``skipped`` (an Item-level bulk
	  relist would also revive guns hidden one-by-one for holds or repairs).
	  The per-gun route is the Serial No form's own button.
	- **Non-firearm / uniform-price firearm**:
	  single item-level push via :func:`ffl_woo_sync.woocommerce.sync.push_item`.
	  Returns ``{"ok": True, "woo_product_id": "<id>", "raw": <WC response>}``.

	Args:
	    item_code: The Item to push.
	    site: "retail" (default; the only store).

	Returns:
	    Per-serial branch: {ok, pushed, errors, skipped, total};
	    item-level branch: {ok, woo_product_id, raw}.

	Raises:
	    ValidationError: User-facing message on WC / config errors.
	"""
	frappe.only_for(STOCK_ROLES)
	assert_store_site(site)
	from ffl_woo_sync.woocommerce import sync

	# Detect per-serial firearm routing.
	item_row = frappe.db.get_value(
		"Item", item_code, ["is_firearm", "item_group", "uniform_sell_price"], as_dict=True
	) or {}
	is_per_serial = _is_firearm_row(item_row) and not bool(item_row.get("uniform_sell_price"))

	if is_per_serial:
		# Per-serial firearm: push each Active Serial No with error aggregation.
		return push_all_serials(item_code, site)

	# Non-firearm or uniform-price firearm: single item-level product.
	try:
		# An operator pressing "Push to WooCommerce" is the clearest relist there
		# is, so this overrides the hidden-product guard — a human may
		# always undo a human's hide.  Mirrors push_serial_now.  The PRICE guard
		# is deliberately not overridable and still applies.
		result = sync.push_item(item_code, site=site, _relist=True)
	except WooCommerceConfigError as exc:
		frappe.throw(_("WooCommerce is not configured: {0}").format(str(exc)))
	except (WooCommerceError, ValueError) as exc:
		frappe.throw(_("WooCommerce sync failed: {0}").format(str(exc)))
	if result.get("skipped"):
		return _skipped_response(result)
	return {
		"ok": True,
		"woo_product_id": str(result.get("id", "")),
		"raw": result,
	}


@frappe.whitelist()
def push_all_serials(item_code: str, site: str = "retail") -> dict:
	"""Push (create or update) WC products for all Active Serial Nos of an Item.

	Uses a single pre-built client and pre-fetched attribute IDs to minimise
	HTTP round-trips.  Returns per-serial results and any errors encountered.

	.. note:: **Aggregating semantics** — every serial is attempted regardless
	   of earlier failures; errors are collected and returned rather than
	   re-raised.  This differs from
	   :func:`ffl_woo_sync.woocommerce.sync.push_all_serials` which is
	   fail-fast (re-raises the first per-serial exception immediately).

	Args:
	    item_code: The Item whose Active serials to push.
	    site: "retail" (default; the only store).

	Returns:
	    {ok, pushed, errors, skipped, total}.
	"""
	frappe.only_for(STOCK_ROLES)
	assert_store_site(site)
	from ffl_woo_sync.woocommerce import sync
	try:
		client = WooCommerceClient.from_settings(site)
		attr_ids = client.get_attribute_ids()
	except WooCommerceConfigError as exc:
		frappe.throw(_("WooCommerce is not configured: {0}").format(str(exc)))

	serials = frappe.get_all(
		"Serial No",
		filters={"item_code": item_code, "status": "Active"},
		pluck="name",
	)
	pushed, errors, skipped = _push_serial_list(
		serials, sync=sync, client=client, attr_ids=attr_ids, site=site)
	return {
		# A guard refusal is NOT a success: `ok` must go false or the Item
		# form reports a green "Pushed N/N" over guns still draft on the shop.
		"ok": not errors and not skipped,
		"pushed": pushed,
		"errors": errors,
		"skipped": skipped,
		"total": len(serials),
	}


@frappe.whitelist()
def delist_item_now(item_code: str, site: str = "retail") -> dict:
	"""Set the WooCommerce product for an Item to ``draft`` + stock 0.

	The operator's deliberate delist — the only delist_item caller that
	raises the republish sentinel.

	Args:
	    item_code: The Item to delist.
	    site: "retail" (default; the only store).

	Returns:
	    {"ok": True, "raw": <WC response>}.
	"""
	frappe.only_for(STOCK_ROLES)
	assert_store_site(site)
	from ffl_woo_sync.woocommerce import sync
	try:
		# The operator's own "Delist from WooCommerce" button — a deliberate human
		# delist, and the only delist_item caller that raises the republish
		# sentinel.
		result = sync.delist_item(item_code, site=site, _deliberate=True)
	except WooCommerceConfigError as exc:
		frappe.throw(_("WooCommerce is not configured: {0}").format(str(exc)))
	except (WooCommerceError, ValueError) as exc:
		frappe.throw(_("WooCommerce delist failed: {0}").format(str(exc)))
	return {"ok": True, "raw": result}


@frappe.whitelist()
def push_serial_now(serial_no: str, site: str = "retail") -> dict:
	"""Push (create or update) the WooCommerce product for a single Serial No.

	An operator's explicit push is the clearest relist there is, so this
	overrides the hidden-serial guard (a human may always undo a human's
	hide).

	Args:
	    serial_no: The Serial No to push.
	    site: "retail" (default; the only store).

	Returns:
	    {"ok": True, "woo_product_id": "<id>", "raw": <WC response>} — or
	    a skipped response when the sync guard refuses.
	"""
	frappe.only_for(STOCK_ROLES)
	assert_store_site(site)
	from ffl_woo_sync.woocommerce import sync

	try:
		# An operator pressing "Push to WooCommerce" is the clearest relist there
		# is, so this explicitly overrides the hidden-serial guard —
		# a human may always undo a human's hide.
		result = sync.push_serial(serial_no, site=site, _relist=True)
	except WooCommerceConfigError as exc:
		frappe.throw(_("WooCommerce is not configured: {0}").format(str(exc)))
	except (WooCommerceError, ValueError) as exc:
		frappe.throw(_("WooCommerce sync failed: {0}").format(str(exc)))
	if result.get("skipped"):
		return _skipped_response(result)
	return {
		"ok": True,
		"woo_product_id": str(result.get("id", "")),
		"raw": result,
	}


@frappe.whitelist()
def delist_serial_now(serial_no: str, site: str = "retail") -> dict:
	"""Set the WooCommerce product for a Serial No to ``draft`` + stock 0.

	Args:
	    serial_no: The Serial No to delist.
	    site: "retail" (default; the only store).

	Returns:
	    {"ok": True, "raw": <WC response>}.
	"""
	frappe.only_for(STOCK_ROLES)
	assert_store_site(site)
	from ffl_woo_sync.woocommerce import sync
	try:
		result = sync.delist_serial(serial_no, site=site)
	except WooCommerceConfigError as exc:
		frappe.throw(_("WooCommerce is not configured: {0}").format(str(exc)))
	except (WooCommerceError, ValueError) as exc:
		frappe.throw(_("WooCommerce delist failed: {0}").format(str(exc)))
	return {"ok": True, "raw": result}


@frappe.whitelist()
def reconcile_now(item_code: str, site: str = "retail") -> dict:
	"""Push the item product + all Active serials in a single call.

	Uses a shared client and pre-fetched attribute IDs.  Returns combined
	results: the item push result plus per-serial results and any errors.

	Suitable for "initial listing" of a per-serial firearm item where you want
	to bring everything live in one operation.

	FULL-PAYLOAD by design: as a deliberate,
	confirmed bulk action it keeps the manual-push semantics — which means it
	WILL overwrite wp-admin content edits (title / description / images) with
	the POS versions on every product it touches. Routine drift healing belongs
	to the scheduled reconcile, which is pos_fields_only and content-safe.

	Args:
	    item_code: The Item to reconcile.
	    site: "retail" (default; the only store).

	Returns:
	    {ok, item_result, item_error, dangling, pushed_serials,
	    serial_errors, skipped_serials, ...}.
	"""
	frappe.only_for(STOCK_ROLES)
	assert_store_site(site)
	from ffl_woo_sync.woocommerce import sync

	try:
		client = WooCommerceClient.from_settings(site)
		attr_ids = client.get_attribute_ids()
	except WooCommerceConfigError as exc:
		frappe.throw(_("WooCommerce is not configured: {0}").format(str(exc)))

	item_result: dict | None = None
	item_error: str | None = None
	dangling: dict | None = None
	try:
		item_result = sync.push_item(item_code, client=client, attr_ids=attr_ids, site=site)
	except ValueError:
		# Per-serial firearm — no item-level product; push serials only.
		pass
	except WooCommerceError as exc:
		if exc.status_code == 404:
			# Dangling product_id: the WC product was deleted on the store. Not a
			# sync error to retry — a stale id for woo_audit_dangling_ids to clear.
			# Own bucket, never auto-recreate (deletion on Woo may be deliberate),
			# never a failure.
			dangling = {"item_code": item_code, "field": sync._f(site, "product_id")}
		else:
			item_error = str(exc)
	except Exception as exc:
		item_error = str(exc)

	serials = frappe.get_all(
		"Serial No",
		filters={"item_code": item_code, "status": "Active"},
		pluck="name",
	)
	pushed_serials, serial_errors, skipped_serials = _push_serial_list(
		serials, sync=sync, client=client, attr_ids=attr_ids, site=site
	)

	# Skips are reported but do NOT fail the run: reconcile_now is a bulk
	# "bring everything live" action, and a gun deliberately held back is an
	# expected outcome rather than a failure. They must still be visible —
	# silently folding them into success is the bug this branch removes.
	all_ok = (item_error is None) and (not serial_errors)
	return {
		"ok": all_ok,
		"item_result": item_result,
		"item_error": item_error,
		"dangling": dangling,
		"pushed_serials": pushed_serials,
		"serial_errors": serial_errors,
		"skipped_serials": skipped_serials,
	}


@frappe.whitelist(methods=["POST"])
def pull_product_from_woo(sku: str, site: str = "retail") -> dict:
	"""Overwrite a POS record's listing content from its WooCommerce product.

	The reverse of push_item_now / push_serial_now, for the storefront plugin's product
	page's "overwrite POS from WooCommerce" button: pulls title, description,
	price and gallery images from the live WC product and writes them onto
	the Item (or, for an ``item_code::serial_no`` SKU, the Serial No).
	Stock and attributes are never pulled — the POS stays their source of
	truth. The save runs under the inbound loop guard, so doc_events does not
	push the change straight back at WooCommerce.

	Args:
	    sku: WooCommerce product SKU — ``item_code`` for an item-level
	        product, ``item_code::serial_no`` for a per-serial firearm.
	    site: "retail" (default; the only store).

	Returns:
	    {"ok": True, "doctype": ..., "name": ..., "updated": {field: summary},
	    "images_imported": n, "image_errors": [...]} — plus "price_note"
	    (optional) when the price was deliberately not written (uniform-priced
	    parent). The Item branch reports the applied price as
	    updated["selling_price"] (it lands via the canonical selling-price
	    helper, not a bare standard_rate write). Refusals (unknown record,
	    never pushed, missing write permission, product deleted on the store)
	    raise a ValidationError with the reason.
	"""
	frappe.only_for(STOCK_ROLES)
	assert_store_site(site)
	from ffl_woo_sync.woocommerce.pull import pull_product_from_woo as _pull

	try:
		result = _pull(sku, site=site)
	except WooCommerceConfigError as exc:
		frappe.throw(_("WooCommerce is not configured: {0}").format(str(exc)))
	except WooCommerceError as exc:
		frappe.throw(_("WooCommerce API error: {0}").format(str(exc)))
	return {"ok": True, **result}
