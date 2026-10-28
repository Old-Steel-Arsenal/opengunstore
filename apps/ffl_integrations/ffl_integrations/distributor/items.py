"""Local Items for distributor catalog products — the counter's and the web
listing's shared path (dual-source guard → idempotent promote → the
``dropship_distributor`` stamp). Core: the counter creates Items for catalog SKUs
with or without the website drop-ship layer; ``listing`` builds on it.
"""
from __future__ import annotations

import frappe

from ffl_integrations.distributor import api
from ffl_integrations.distributor.util import legal_firearm
from ffl_integrations.notifications import notify_admins, safe_log_error


def dual_source_conflict(*, item_exists, has_serial_no=0, uniform_sell_price=0,
		active_serials=0, local_qty=0, item_code=None):
	"""Why this SKU must not be turned into a drop-ship listing, or None if it may.

	One stock source per SKU in v1. Flow A restock and Flow B listing share
	`promote_to_item` and the same item_code, so an Item the shop already stocks is a
	likely collision — converting it would flip the publishing model to uniform, hide
	the real inventory behind the drop-ship stock source (the FIRST statement of
	`stock_for_item`, so it wins outright), and let P4b treat a local firearm as a
	drop-ship line.

	The local-stock check is NOT limited to firearms (F9): ammunition and accessories
	received through Flow A carry the same item_code, and masking a full shelf behind
	the distributor's quantity is the same defect minus the compliance angle.
	"""
	if not item_exists:
		return None
	name = f" {item_code}" if item_code else ""
	if int(active_serials or 0) > 0:
		return (f"Item{name} already has {int(active_serials)} serial number(s) in stock. "
			f"Listing it as drop-ship would hide the physical inventory behind the "
			f"distributor's quantity (one stock source per SKU).")
	if float(local_qty or 0) > 0:
		return (f"Item{name} has {float(local_qty):g} unit(s) of local stock on hand. "
			f"The drop-ship stock source would mask it — sell the local stock down "
			f"first, or force the listing deliberately.")
	if has_serial_no and not uniform_sell_price:
		return (f"Item{name} is published per-serial (has_serial_no=1, "
			f"uniform_sell_price=0). Drop-ship listing would switch it to uniform "
			f"pricing and change how the store publishes it.")
	return None


def ensure_item_for_dropship(distributor: str, sku: str, *, spec=None, provider=None,
		row=None, is_firearm=None, claim: bool = True, listing: bool = False,
		managed_with_store=None) -> str:
	"""Make sure a catalog SKU has a local Item ready to be sold drop-ship:
	Dual-source guard → idempotent promote → ``dropship_distributor`` stamp.

	Shared by ``create_listing`` (Flow B web listing) and the counter Create Order
	seam (``distributor.api.ensure_dropship_item``) — the counter needs exactly this
	chain WITHOUT a Distributor Listing row, and extracting it is what keeps the two
	paths from drifting. The optional kwargs let ``create_listing`` hand over what it
	already resolved; the counter wrapper passes only ``provider``/``row``.

	``claim=False`` promotes the Item WITHOUT stamping ``dropship_distributor``: a
	ship-to-store line buys the goods into our own stock, and the stamp is the web
	channel's statement about who drop-ships the Item — an ordering decision at the
	counter must not rewrite it. ``listing``: the web listing path (`create_listing`)
	— the only one whose stock-on-hand check automatic sourcing relaxes."""
	spec = spec or {}
	if provider is None:
		provider = api.get_provider(
			frappe.get_cached_doc("Distributor", distributor).provider)
	if row is None:
		row = provider.catalog_row(sku)
		if not row:
			frappe.throw(frappe._("SKU {0} is not in {1}'s catalog.").format(sku, distributor))
	if is_firearm is None:
		# The law's answer (the service class), not the house's shipping flag: a
		# barrel RSR only ships to dealers is still not a firearm.
		is_firearm = legal_firearm(row, provider.code)
	_guard_dual_source(sku, spec, distributor, row, listing=listing,
		managed_with_store=managed_with_store)
	item_code = _promote(provider, sku, spec, is_firearm)
	if claim:
		_stamp_item(item_code, distributor, row, is_firearm)
	return item_code


def _guard_dual_source(sku, spec, distributor, row, *, relist=False, listing=False,
		managed_with_store=None) -> None:
	"""Refuse to convert an existing locally-stocked Item into a drop-ship listing.
	`force` overrides, but never silently.

	``relist``: a re-list of this house's OWN listing is not a conversion — the
	stock its Item holds (a ship-to-store receipt, a restock) is already behind
	the drop-ship source, and refusing a resume would not change that. Only the
	publishing model is checked then (a firearm whose uniform flag was cleared).

	``listing``: a web listing (`create_listing`, which hands in automatic sourcing's
	``managed_with_store``). Automatic sourcing, managing the
	product with the store's own stock a source, owns that one: the product then
	sells from whichever source is cheapest, the store's stock included, so stock
	on hand does not collide. Every other caller — the counter's drop-ship claim
	above all, which would route the store's own goods to a distributor — is
	checked as before.

	F2 (updated for UPC-keyed promotion, Task 4): `promote_to_item` no longer
	always names a new Item after the stock number — it prefers the RSR row's
	UPC when the row has one (`rsr/promote.py`'s identity chain), falling back
	to the stock number only for orphans. So the Item this listing would stamp
	is not reliably named `sku` anymore, and checking only `sku` would silently
	miss an Item an earlier Flow A receive already promoted under its UPC —
	exactly the collision this guard exists to catch. Check EVERY candidate
	identity the promoter could have used (UPC first, then the stock number)
	and block if ANY of them already names a conflicting local Item.
	"""
	requested = (spec.get("item_code") or "").strip()
	if requested and requested != sku:
		# Half-implemented rename support is worse than none: the guard would inspect
		# `requested` while promote_to_item creates/updates `sku`.
		frappe.throw(frappe._(
			"item_code {0} does not match SKU {1}. Renaming an Item during listing is "
			"not supported — the promoter names the Item after the catalog row's UPC "
			"when it has one, falling back to the distributor stock number only for "
			"orphans.").format(requested, sku))
	# UPC first, deterministically: a set literal iterates in an unspecified order,
	# and the throw below reads `conflicts[0]` — a non-deterministic candidate order
	# would make WHICH reason surfaces flaky across runs for a row that conflicts on
	# both candidates.
	candidates = []
	from ffl_integrations.rsr.promote import barcode_owner

	upc = (row.get("upc") or "").strip() or None
	for candidate in (upc, barcode_owner(upc), sku):
		if candidate and candidate not in candidates:
			candidates.append(candidate)
	conflicts = []
	for item_code in candidates:
		if not frappe.db.exists("Item", item_code):
			continue
		info = frappe.db.get_value(
			"Item", item_code, ["has_serial_no", "uniform_sell_price"], as_dict=True) or {}
		# With the store's own stock a source (automatic sourcing), stock on hand no
		# longer collides: the product sells from whichever is cheapest. Only a gun
		# priced serial by serial is still refused (its publishing model would change).
		# A web listing of a product without serial numbers publishes the store's
		# own stock with the distributors' (`stock.with_other_sources`), so stock on
		# hand no longer collides there either.
		shelf = not relist and not (listing and (not info.get("has_serial_no")
			or (managed_with_store is not None and managed_with_store(item_code))))
		reason = dual_source_conflict(
			item_exists=True,
			has_serial_no=info.get("has_serial_no"),
			uniform_sell_price=info.get("uniform_sell_price"),
			active_serials=_active_serial_count(item_code) if shelf else 0,
			local_qty=_local_stock_qty(item_code) if shelf else 0,
			item_code=item_code)
		if reason:
			conflicts.append((item_code, reason))
	if not conflicts:
		return
	if not spec.get("force"):
		frappe.throw(frappe._(
			"{0}\n\nIf this SKU really should be sold drop-ship, clear the local stock "
			"first, or re-run this listing with force enabled.").format(conflicts[0][1]))
	for item_code, reason in conflicts:
		_record_force_override(item_code, distributor, reason)


def _active_serial_count(item_code) -> int:
	"""In-stock serials. `status` alone is not enough: prod has seen a gun sitting in
	stock with a stale status (memory: serial-status-stale-inactive-relisting), which
	is a FALSE NEGATIVE for this guard. A serial parked in a warehouse is on hand
	whatever the flag says, so use the repo's stronger criterion (see
	ffl_core/patches/issue_stock_for_orphan_dispositions.py): warehouse set OR Active.
	"""
	in_warehouse = frappe.db.count(
		"Serial No", {"item_code": item_code, "warehouse": ("is", "set")})
	if in_warehouse:
		return in_warehouse
	return frappe.db.count("Serial No", {"item_code": item_code, "status": "Active"})


def _local_stock_qty(item_code) -> float:
	"""Total on-hand across warehouses via the canonical helper (F9) — not a
	hand-rolled Bin sum."""
	from ffl_core.api.item_admin import _get_item_stock as get_item_stock

	return float((get_item_stock([item_code]) or {}).get(item_code) or 0)


def _record_force_override(item_code, distributor, reason) -> None:
	"""F3: a forced override needs a PERSISTENT trail, not just an email.

	`notify_admins` returns False silently when the dedup window is hot, there are no
	recipients, or sendmail fails — and the fact being recorded is "somebody flipped
	the publishing model of an Item the shop physically stocks", which is precisely
	what gets asked about months later. The Item comment is the durable record; the
	alert is the timely one.
	"""
	note = (f"Drop-ship listing FORCED over local stock (distributor {distributor}, "
		f"by {frappe.session.user}).\n\n{reason}")
	try:
		frappe.get_doc("Item", item_code).add_comment("Comment", note)
	except Exception:  # noqa: BLE001 — the audit note must not break the listing run
		safe_log_error(f"Could not record force-override comment on {item_code}", note)
	notify_admins(
		f"Drop-ship listing FORCED over local stock: {item_code}",
		f"{note}\n\nThe Item's publishing model and stock source have changed.",
		dedup_key=f"dist_force_dual_source_{item_code}", window_minutes=1440)


def local_item(sku: str, upc: str) -> str | None:
	"""An Item the shop already has for this product: the one carrying the UPC as a
	barcode, or named by the UPC, or by the SKU."""
	from ffl_integrations.rsr.promote import barcode_owner

	for code in (barcode_owner(upc), upc, sku):
		if code and frappe.db.exists("Item", code):
			return code
	return None


def catalog_item(provider, sku: str, upc: str) -> tuple:
	"""``(item_code, reason)`` from the house's catalog: the row by SKU, else by UPC,
	promoted through the house's own promoter — idempotent, and it stamps nothing on
	the Item (an order or a receipt says nothing about who drop-ships it); a newly
	made Item gets our optimized copy. ``item_code`` None carries the reason."""
	from frappe.utils import get_datetime, now_datetime

	from ffl_integrations.distributor import offers

	promote = getattr(provider, "promote_to_item", None)
	if not callable(promote):
		return None, "the house cannot promote catalog rows to Items"
	row = offers.catalog_row(provider, sku=sku, upc=upc or None)
	if not row:
		return None, "not in the house's catalog"
	started = now_datetime()
	item_code = (promote(row.get("sku") or sku, {}) or {}).get("item_code")
	if not item_code:
		return None, "the catalog promotion returned no Item"
	if get_datetime(frappe.db.get_value("Item", item_code, "creation")) >= started:
		_apply_product_copy(item_code)
	return item_code, ""


def _promote(provider, sku, spec, is_firearm) -> str:
	"""Reuse the provider's promotion (RSR: `rsr.promote.promote_to_item`, which
	already writes MAP/MSRP/cost/brand/images — DIVERGENCE-2).

	#4: a drop-ship firearm MUST be `uniform_sell_price=1` — there is no serialised
	local unit to price individually. `promote_to_item` is idempotent and does not
	update an existing Item, so we assert and repair rather than trust the flag.
	"""
	promote = getattr(provider, "promote_to_item", None)
	if not callable(promote):
		frappe.throw(frappe._("Provider '{0}' cannot promote catalog rows to Items.")
			.format(provider.code))
	opts = {"uniform_sell_price": 1 if is_firearm else 0}
	if spec.get("item_group"):
		opts["item_group"] = spec["item_group"]
	item = promote(sku, opts) or {}
	item_code = item.get("item_code")
	if not item_code:
		frappe.throw(frappe._("Promotion of SKU {0} returned no Item.").format(sku))
	_ensure_uniform(item_code, is_firearm, current=item.get("uniform_sell_price"))
	_apply_product_copy(item_code)
	return item_code


def _apply_product_copy(item_code) -> None:
	"""Our optimized copy (the catalog service's ``content_optimized`` product) is
	the Item's name and description wherever it exists: the distributor's feed text
	(RSR's all-caps abbreviations) never stands in for it. Best effort — a catalog
	outage leaves the promoted Item as it is."""
	from ffl_integrations.distributor import catalog

	upc = frappe.db.get_value("Item", item_code, "upc") or item_code
	try:
		product = catalog.product(upc)
	except Exception:  # noqa: BLE001 — copy is cosmetic; listing goes on
		safe_log_error(f"Optimized copy unavailable: {item_code}", frappe.get_traceback())
		return
	if not product or not product.get("content_optimized"):
		return
	values = {"item_name": (product.get("description") or "")[:140],
		"description": item_html(product.get("long_description") or "")}
	values = {k: v for k, v in values.items() if v}
	current = frappe.db.get_value("Item", item_code, list(values), as_dict=True) or {}
	if any(current.get(k) != v for k, v in values.items()):
		frappe.db.set_value("Item", item_code, values)


def item_html(html: str) -> str:
	"""Copy HTML as an Item description keeps it: headings become bold paragraphs
	(a heading written into Item.description was dropped on prod, 2026-09-24)."""
	import re

	return re.sub(r"(?is)<h[1-6][^>]*>(.*?)</h[1-6]>", r"<p><strong>\1</strong></p>", html or "")


def _ensure_uniform(item_code, is_firearm, current=None) -> None:
	"""#4: a drop-ship firearm is uniform-priced — it has no local serial to price
	per copy. Asserted and repaired, never trusted."""
	if not is_firearm:
		return
	if current is None:
		current = frappe.db.get_value("Item", item_code, "uniform_sell_price")
	if not current:
		frappe.db.set_value("Item", item_code, "uniform_sell_price", 1, update_modified=False)


def _stamp_item(item_code, distributor, row, is_firearm) -> None:
	"""Stock/procurement source. NOT the state restrictions.

	This used to also write `Item.restricted_states` from the RSR inventory file's
	per-state Y-flags. Those flags are read at columns 16-66 while RSR's manual says
	"fields 16-56" — a discrepancy nobody ever reconciled, and one that cannot
	announce itself: a wrong offset mislabels which states an item is restricted in
	and everything downstream keeps working.

	Downstream was not cosmetic. `Item.restricted_states` reaches WP as
	`_osa_restricted_states` AND is what ffl_core's `_validate_items` throws on for a
	counter drop-ship line — so guessed columns were, in effect, refusing counter
	orders. A later change replaces the display half with the authoritative ship-restriction
	feed (`rsr.restrictions`) and hands the field itself to the operator.

	The key is OMITTED rather than written empty on purpose: `set_value` with an
	empty string would wipe a hand-entered list on the next `sync_listings` tick,
	which is a worse bug than the one being fixed.
	"""
	frappe.db.set_value("Item", item_code, {
		"dropship_distributor": distributor,
	}, update_modified=False)
