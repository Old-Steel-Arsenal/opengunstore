"""WooCommerce doc-event hooks — auto-push on Item / Serial No / SLE changes.

Public surface
--------------
``on_item_update(doc, method)``          — ``Item.on_update``
``on_serial_no_update(doc, method)``     — ``Serial No.on_update``
``on_sle_submit(doc, method)``           — ``Stock Ledger Entry.on_submit``
``on_sle_cancel(doc, method)``           — ``Stock Ledger Entry.on_cancel``

All handlers enforce three guards before enqueuing:

1. **Inbound-webhook loop guard** (``frappe.flags.woo_sync_inbound``): when an
   inbound WooCommerce webhook is being processed the flag is set to prevent the
   resulting Frappe doc save from echoing back to WooCommerce.

2. **System-state guard**: skip during Frappe install / migrate / import.

3. **Existence guard**: never auto-create a WooCommerce product from a doc event.
   Only push when ``woo_product_id`` is already set — i.e. the record was
   previously pushed manually.

Item and Serial No handlers additionally use ``doc.has_value_changed()`` to
skip enqueuing when only WooCommerce-irrelevant fields changed, avoiding
spurious API round-trips on unrelated saves.

Pushes are enqueued *after* the current DB transaction commits
(``enqueue_after_commit=True``, ``queue="long"``) so the background worker
always sees the committed data.
"""
from __future__ import annotations

import frappe

from ffl_core.firearm import _is_firearm_row, is_firearm_item
from ffl_woo_sync.woocommerce.mapping import MAP_ITEM_FIELDS

# ---------------------------------------------------------------------------
# WooCommerce-relevant fields per doctype
# ---------------------------------------------------------------------------

# Item fields whose change should trigger a WC product update.
#
# NOTE (pos_fields_only era): the push these lists trigger is a MACHINE update,
# which sends only the POS-owned fields (sync._POS_OWNED_KEYS) on an existing
# product. The content entries below (item_name / description / image /
# gallery / caliber / condition) therefore no longer deliver their change to
# the store — a POS content edit reaches WooCommerce only through the manual
# push buttons. The fields stay watched on purpose: the enqueued lean push
# still opportunistically heals price/stock/status drift, and trimming the
# lists would silently change which saves sync at all (kept as documented
# behaviour rather than re-derived).
_ITEM_WOO_FIELDS = (
	"standard_rate",
	# Promotional price + window — POS-owned keys, so the lean push a change
	# here enqueues DOES deliver them to the store (unlike the content fields
	# noted above).
	"sale_price",
	"sale_from",
	"sale_to",
	"item_name",
	"description",
	"image",
	"disabled",
	"uniform_sell_price",
	# pa_condition / pa_caliber taxonomy feeds — dropped in 619da28, restored here.
	"condition_grade",
	"caliber",
	"is_used",
)

# Item fields behind the retail-only MAP meta (mapping.map_meta): the MAP, the
# brand (manufacturer when blank) whose mode applies, and the used flag that drops
# it. A per-serial firearm's MAP rides each gun's own product.
_ITEM_MAP_FIELDS = MAP_ITEM_FIELDS

# Serial No fields whose change should trigger a WC per-serial product update.
_SERIAL_WOO_FIELDS = (
	"sell_price",
	# Promotional price + window — POS-owned keys; the lean push delivers them.
	"sale_sell_price",
	"sale_from",
	"sale_to",
	"condition_grade",
	"status",
	"woo_reserved",
	"image",
	"caliber",
)

# Serial No fields that, when changed, affect the PARENT ITEM's stock count
# (relevant only for uniform-price firearms).
_SERIAL_STOCK_FIELDS = ("status", "woo_reserved")


def _extension_push_fields(doctype: str) -> tuple[str, ...]:
	"""Fields an extension asked core to watch: ``gunshop_woo_push_fields``.

	Shape ``{"Item": [fieldname, ...], "Serial No": [fieldname, ...]}`` — the
	custom fields whose value a ``gunshop_woo_product_meta`` provider reads.
	Without this the provider only runs when a *core* field happens to change in
	the same save, so a flag edited on its own never reaches the store (the CA
	Legal gap, 2026-09-19). The push it triggers is the lean one; the provider's
	keys are on that allowlist via ``gunshop_woo_product_meta_keys``.
	"""
	declared = frappe.get_hooks("gunshop_woo_push_fields") or {}
	if not isinstance(declared, dict):
		return ()
	return tuple(str(f) for f in (declared.get(doctype) or []))


# ---------------------------------------------------------------------------
# Common guards / helpers
# ---------------------------------------------------------------------------

def _gallery_changed(doc) -> bool:
	"""Return True if the ``image_gallery`` child table changed since last save.

	``doc.has_value_changed('image_gallery')`` is NOT viable for child tables:
	Frappe compares freshly-loaded Document instances by identity on every save,
	so it returns True unconditionally — causing a false-positive push storm.

	Instead, compare against ``doc._doc_before_save`` (the pre-save snapshot
	Frappe attaches automatically): check row count, each row's ``image`` URL,
	and its ``is_primary`` flag.  A missing ``_doc_before_save`` (new document)
	is treated as changed so the first save of a listed record always syncs.
	"""
	before = getattr(doc, "_doc_before_save", None)
	if before is None:
		return True

	curr_rows = doc.image_gallery or []
	prev_rows = before.image_gallery or []

	if len(curr_rows) != len(prev_rows):
		return True

	for cur, prv in zip(curr_rows, prev_rows):
		cur_img = (getattr(cur, "image", None) or "").strip()
		prv_img = (getattr(prv, "image", None) or "").strip()
		cur_primary = bool(getattr(cur, "is_primary", 0))
		prv_primary = bool(getattr(prv, "is_primary", 0))
		if cur_img != prv_img or cur_primary != prv_primary:
			return True

	return False


def _skip_push() -> bool:
	"""Return True when the current context means we should NOT push to WooCommerce.

	- ``frappe.flags.woo_sync_inbound`` is set (change originated from a Woo
	  inbound webhook — skip to avoid an echo loop back to Woo).
	- Frappe system flags: install, migrate, or import in progress.
	"""
	if frappe.flags.get("woo_sync_inbound"):
		return True
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_import:
		return True
	return False


# ---------------------------------------------------------------------------
# Item.on_update
# ---------------------------------------------------------------------------

def on_item_update(doc, method=None) -> None:
	"""Push the WooCommerce product when a WC-relevant Item field changes.

	Only fires when the Item already has a ``woo_product_id`` — doc events
	never auto-create a WooCommerce product.  Uses ``doc.has_value_changed()``
	to detect whether any WooCommerce-relevant field actually changed, preventing
	spurious round-trips on unrelated saves (e.g. reorder-level adjustments).
	"""
	if _skip_push():
		return

	gallery_chg = _gallery_changed(doc)
	watched = _ITEM_WOO_FIELDS + _extension_push_fields("Item")
	map_chg = (doc.get_doc_before_save() is not None
		and any(doc.has_value_changed(f) for f in _ITEM_MAP_FIELDS))

	if (doc.woo_product_id or "") and (
		any(doc.has_value_changed(f) for f in watched) or gallery_chg or map_chg
	):
		frappe.enqueue(
			"ffl_woo_sync.woocommerce.tasks.push_item_task",
			queue="long",
			enqueue_after_commit=True,
			item_code=doc.name,
		)
	elif (map_chg and _is_firearm_row(doc) and not doc.get("uniform_sell_price")
			and frappe.db.exists("Serial No", {"item_code": doc.name, "status": "Active",
				"woo_product_id": ["is", "set"]})):
		frappe.enqueue(
			"ffl_woo_sync.woocommerce.tasks.push_map_task",
			queue="long",
			enqueue_after_commit=True,
			item_codes=[doc.name],
		)


# ---------------------------------------------------------------------------
# Serial No.on_update
# ---------------------------------------------------------------------------

def on_serial_no_update(doc, method=None) -> None:
	"""Push WooCommerce product(s) when a Serial No changes.

	Two independent actions, determined by the parent Item type:

	**Per-serial firearm** (``woo_product_id`` set on the Serial No):
		If a WC-relevant field changed, enqueue a push of this serial's
		individual WC product via ``push_serial_task``.

	**Uniform-price firearm** (parent Item: ``is_firearm=1``,
	``uniform_sell_price=1``, ``woo_product_id`` set):
		A ``status`` or ``woo_reserved`` change on the serial affects the
		parent item's WooCommerce stock count.  Enqueue ``push_stock_task``
		for the parent item.
	"""
	if _skip_push():
		return

	item_code = doc.item_code
	if not item_code:
		return

	# Push this serial's own WC product when relevant fields change.
	watched = _SERIAL_WOO_FIELDS + _extension_push_fields("Serial No")
	if (doc.woo_product_id or "") and (
		any(doc.has_value_changed(f) for f in watched)
		or _gallery_changed(doc)
	):
		frappe.enqueue(
			"ffl_woo_sync.woocommerce.tasks.push_serial_task",
			queue="long",
			enqueue_after_commit=True,
			serial_no=doc.name,
		)

	# Fetch parent Item data (used for the uniform-stock push).
	item_row = frappe.db.get_value(
		"Item",
		item_code,
		["is_firearm", "item_group", "uniform_sell_price", "woo_product_id"],
		as_dict=True,
	) or {}

	# Push stock for the parent Item when it is a listed uniform-price firearm.
	if (
		_is_firearm_row(item_row)
		and bool(item_row.get("uniform_sell_price"))
		and (item_row.get("woo_product_id") or "")
		and any(doc.has_value_changed(f) for f in _SERIAL_STOCK_FIELDS)
	):
		frappe.enqueue(
			"ffl_woo_sync.woocommerce.tasks.push_stock_task",
			queue="long",
			enqueue_after_commit=True,
			item_code=item_code,
		)


# ---------------------------------------------------------------------------
# Stock Ledger Entry.on_submit / on_cancel
# ---------------------------------------------------------------------------

def on_sle_submit(doc, method=None) -> None:
	"""Push updated stock to WooCommerce when a Stock Ledger Entry is submitted."""
	if _skip_push():
		return
	_maybe_push_stock_from_sle(doc)


def on_sle_cancel(doc, method=None) -> None:
	"""Push updated stock to WooCommerce when a Stock Ledger Entry is cancelled.

	A cancelled SLE reverses a stock movement, changing on-hand qty just as a
	new SLE does — push the updated stock count.
	"""
	if _skip_push():
		return
	_maybe_push_stock_from_sle(doc)


def _enqueue_item_stock(item_code: str, item_row: dict) -> None:
	"""Enqueue the stock-count push for a UNIFORM-PRICE firearm Item.

	The item-level twin of :func:`_enqueue_serial_listing`: these products have
	no per-serial listing, so a hold reaches the storefront as a changed count
	(``mapping.stock_for_item`` subtracts the same holds).

	``_maybe_push_stock_from_sle`` keeps its own copy on purpose: a stock
	movement is a bulk path and rides the ``long`` queue, while a counter hold is
	a cashier waiting on a storefront that still says "in stock".
	"""
	if item_row.get("woo_product_id"):
		frappe.enqueue(
			"ffl_woo_sync.woocommerce.tasks.push_stock_task",
			queue="short",
			enqueue_after_commit=True,
			item_code=item_code,
		)


def _sync_row_serial_listings(row_like) -> None:
	"""Enqueue a listing reconcile for every LISTED serial a document row (SLE
	or invoice item) carries. The post-commit task decides push-vs-delist from
	the serial's final state."""
	from ffl_core.serials import serials_for_line

	for serial_no in serials_for_line(row_like):
		_enqueue_serial_listing(serial_no)


def _enqueue_serial_listing(serial_no: str) -> None:
	"""Enqueue the post-commit listing reconcile for ONE serial.

	Split out of :func:`_sync_row_serial_listings` so a caller holding a bare
	serial NAME — a gun that has just been taken OFF a document row, which is
	the moment its hold is released — reconciles through the same rulebook as
	one still on a row, instead of growing a second copy of it.
	"""
	ids = frappe.db.get_value("Serial No", serial_no, ["woo_product_id"], as_dict=True) or {}
	if ids.get("woo_product_id"):
		frappe.enqueue(
			"ffl_woo_sync.woocommerce.tasks.sync_serial_listing_task",
			queue="short",
			enqueue_after_commit=True,
			serial_no=serial_no,
			site="retail",
		)


def on_serial_hold_changed(serial_no: str) -> None:
	"""``gunshop_serial_hold_changed`` (ffl_core.crm.holds): a gun was held for a CRM deal, or its
	hold moved or ended. Its listing reconciles after the commit: stock 0 with the page kept while
	held, back on sale when the hold ends."""
	_enqueue_serial_listing(serial_no)


# ---------------------------------------------------------------------------
# Sales Invoice / POS Invoice.on_submit + on_cancel
# ---------------------------------------------------------------------------

def on_invoice_change(doc, method=None) -> None:
	"""A counter order parking firearms (submit) or releasing them (cancel)
	changes their ONLINE sellability long before any stock moves — the gun is
	still Active on the shelf while it waits for payment + 4473, but it is
	promised. Reconcile every firearm row: per-serial listings via the sync
	task (which consults the counter-hold rulebook), uniform-price listings
	via a stock-count re-push (stock_for_item subtracts the same holds)."""
	if _skip_push():
		return
	if getattr(doc, "is_consolidated", 0):
		return
	seen_uniform: set = set()
	for row in (doc.get("items") or []):
		item_code = row.item_code
		if not item_code:
			continue
		item_row = frappe.db.get_value(
			"Item",
			item_code,
			["is_firearm", "item_group", "uniform_sell_price", "woo_product_id"],
			as_dict=True,
		) or {}
		if not _is_firearm_row(item_row):
			continue
		if not bool(item_row.get("uniform_sell_price")):
			_sync_row_serial_listings(row)
			continue
		if item_code in seen_uniform:
			continue
		seen_uniform.add(item_code)
		_enqueue_item_stock(item_code, item_row)


def _line_serials(row, bundle_serials: dict | None) -> list[str]:
	"""``serials_for_line`` for a row, reading any one BUNDLE at most once per save.

	:func:`on_pos_draft_change` keys the pre-save doc and the post-save doc, and
	every row the cashier did not touch points at the SAME bundle name in both —
	so the second ``get_doc`` of that bundle loads a whole document for an answer
	already in hand (a three-gun cart paid six loads on every keystroke-ish save).

	The memo lives for one hook call and dies with it. ``serials_for_line`` itself
	stays UNCACHED on purpose: everywhere else it has to see what the row says
	right now, and a cached reader is how a swap becomes invisible.
	"""
	from ffl_core.serials import serials_for_line

	bundle = row.get("serial_and_batch_bundle") or ""
	if bundle_serials is None or not bundle or row.get("serial_no"):
		return serials_for_line(row)
	if bundle not in bundle_serials:
		bundle_serials[bundle] = serials_for_line(row)
	return bundle_serials[bundle]


def _hold_key(doc, bundle_serials: dict | None = None) -> frozenset:
	"""The part of an invoice that decides which guns it is holding.

	``(item_code, serial)`` for every gun on the FIREARM rows plus the return
	flag. A Draft POS Invoice is saved on every keystroke-ish interaction at the
	register (payments, discounts, customer, a box of ammo added to the cart), and
	all but a handful of those saves change nothing about which guns are spoken
	for. Comparing this against the pre-save doc is what keeps the ordinary save
	free instead of firing a WooCommerce round-trip per gun on the cart.
	Non-firearm rows are therefore left OUT of the key — adding the ammo must not
	re-push the rifle — which costs one CACHED Item read per row.

	Serials are read the canonical way (``serials_for_line``), so the v16 DEFAULT
	storage — a Serial and Batch Bundle — is keyed on the guns IN the bundle
	rather than on the bundle's name. Keying on the name alone meant every
	bundle row read as "cannot tell, assume changed", which is how a per-save
	dedupe ends up switched off in production.

	A row holding no serial yet contributes nothing: a hold is a gun
	(``reservations._draft_pos_serials`` keys on the serial), so there is nothing
	to reconcile until one is picked.

	``is_return`` belongs in the key because it flips what the same rows MEAN: a
	return holds nothing (``reservations._draft_pos_serials`` excludes returns),
	so toggling it has to reconcile.

	What this key CANNOT see — by construction, not by omission — is entries
	rewritten in place inside a bundle the row already pointed at: ERPNext's
	serial dialog does that in its own committed request
	(``serial_and_batch_bundle.update_serial_batch_no_ledgers``), so by the time
	this save runs, the pre-save snapshot reads the new gun too. That swap is
	reconciled by :func:`on_serial_batch_bundle_update`, which compares the
	BUNDLE's own before/after entries and is the only place the gun that left is
	still visible.

	``bundle_serials`` is the per-save memo described in :func:`_line_serials`:
	pass the SAME dict to both keyings of one save so a bundle is loaded once.
	"""
	key: set = {("__is_return__", int(getattr(doc, "is_return", 0) or 0))}
	for row in (doc.get("items") or []):
		item_code = getattr(row, "item_code", "") or ""
		if not is_firearm_item(item_code):
			continue
		for serial_no in _line_serials(row, bundle_serials):
			key.add((item_code, serial_no))
	return frozenset(key)


def _sync_released_serials(before, doc) -> None:
	"""Reconcile the listings of guns that were on ``before`` and are not on
	``doc`` any more — a line deleted or its serial swapped.

	:func:`on_invoice_change` walks the CURRENT rows, so without this the
	released gun is the one thing the change never reaches: its hold is gone
	and its listing stays down for good.
	"""
	if before is None:
		return
	from ffl_core.serials import serials_for_line

	still_on = set()
	for row in (doc.get("items") or []):
		still_on.update(serials_for_line(row))
	for row in (before.get("items") or []):
		item_code = getattr(row, "item_code", None)
		if not item_code:
			continue
		gone = [s for s in serials_for_line(row) if s not in still_on]
		if not gone:
			continue
		item_row = frappe.db.get_value(
			"Item", item_code, ["is_firearm", "item_group"],
			as_dict=True) or {}
		if not _is_firearm_row(item_row):
			continue
		for serial_no in gone:
			_enqueue_serial_listing(serial_no)


def on_pos_draft_change(doc, method=None) -> None:
	"""A DRAFT POS Invoice was saved — a counter hold just appeared or moved.

	A parked POS sale holds its gun from the moment the draft is saved
	(``ffl_core.reservations._draft_pos_serials``), which is hours or days
	before the invoice is submitted. Until this hook existed, nothing told
	WooCommerce: the only POS Invoice events wired here were submit and cancel,
	and a draft fires neither. That is the eleven-day half of DISP-2026-02000 —
	a gun parked behind a pending 4473 on 8/29, still buyable online, sold on
	the web on 9/9.

	Delegates to :func:`on_invoice_change`, which already knows how to reconcile
	both listing shapes off an invoice's rows; the only thing added here is
	*when*, plus the guns that LEFT the cart (:func:`_sync_released_serials`).
	"""
	if _skip_push():
		return
	if int(getattr(doc, "docstatus", 0) or 0) != 0:
		# Submit / cancel run on_update too. They have their own hook, and doing
		# it twice would double every enqueue.
		return
	before = doc.get_doc_before_save()
	if before is not None:
		# One memo across both keyings: the untouched rows point at the same
		# bundle in the before and the after snapshot (see _line_serials).
		bundle_serials: dict = {}
		if _hold_key(before, bundle_serials) == _hold_key(doc, bundle_serials):
			return
	on_invoice_change(doc)
	_sync_released_serials(before, doc)


def on_pos_invoice_trash(doc, method=None) -> None:
	"""A POS Invoice is being deleted — every gun it held is free again.

	Deleting the draft is how an abandoned counter order is closed out (the POS
	recycle bin keeps it restorable), and it is what the manager cleanup button
	does. The enqueue is post-commit, so by the time the job reads
	``counter_holds`` the row is gone and the gun relists.
	"""
	if _skip_push():
		return
	on_invoice_change(doc)


def on_serial_batch_bundle_update(doc, method=None) -> None:
	"""A Serial and Batch Bundle was rewritten — the gun on a counter order may
	have been swapped for another one.

	ERPNext's serial picker edits an EXISTING bundle IN PLACE (
	``serial_and_batch_bundle.update_serial_batch_no_ledgers``: ``set("entries",
	[])`` then re-append, same bundle name) in its own committed request, before
	the invoice is saved. From the invoice's save nothing changed — the row still
	points at the same bundle name, and a pre-save read of that bundle already
	returns the NEW gun — so this is the only place the gun that LEFT is still
	visible: the bundle's own before-save snapshot. Without it the swapped-out gun
	keeps a hold nothing will ever release and stays at stock 0 for good.

	Scoped to a DRAFT POS Invoice, the one voucher whose draft already holds its
	guns: ``reservations.counter_holds`` takes a draft hold from
	``_draft_pos_serials`` (POS Invoice) and from a draft Consignment Out, whose
	lines carry a plain ``serial`` column and no bundle — its Sales Invoice leg
	requires a SUBMITTED invoice. A draft Sales Invoice holds nothing, so
	reconciling one would spend a WooCommerce round-trip per gun to publish the
	answer already on the page. Once any invoice is submitted the Stock Ledger
	Entry hooks carry every change, and doing it here too would double the work.
	"""
	if _skip_push():
		return
	if (getattr(doc, "voucher_type", "") or "") != "POS Invoice":
		return
	voucher = (getattr(doc, "voucher_no", "") or "").strip()
	if not voucher:
		# A bundle built before its invoice has a name: that invoice's own save
		# sees a new bundle on the row (a changed _hold_key) and reconciles there.
		return
	docstatus = frappe.db.get_value("POS Invoice", voucher, "docstatus")
	if docstatus is None or int(docstatus) != 0:
		# Draft only. A voucher_no naming an invoice that is not there (deleted,
		# or never saved) is NOT a draft: `int(None or 0) == 0` read it as one and
		# went on to push holds for an order that does not exist.
		return

	before = doc.get_doc_before_save()
	if before is None:
		# An INSERT is not a hold yet. `counter_holds` reaches a gun through
		# `POS Invoice Item.serial_and_batch_bundle`, and no row can point at a
		# name that did not exist a moment ago — so pushing here would publish the
		# pre-hold answer, and then the save that attaches the bundle pushes the
		# same gun again (a new bundle on the row changes `_hold_key`). The in-place
		# SWAP below is the only thing no other hook can see.
		return
	was = {e.serial_no for e in (before.get("entries") or []) if e.serial_no}
	now = {e.serial_no for e in (doc.get("entries") or []) if e.serial_no}
	changed = was ^ now
	if not changed:
		return

	item_code = getattr(doc, "item_code", "") or ""
	item_row = frappe.db.get_value(
		"Item", item_code,
		["is_firearm", "item_group", "uniform_sell_price", "woo_product_id"],
		as_dict=True,
	) or {}
	if not _is_firearm_row(item_row):
		return
	if not bool(item_row.get("uniform_sell_price")):
		for serial_no in sorted(changed):
			_enqueue_serial_listing(serial_no)
		return
	# Uniform-price: no per-serial listing to reconcile, only a count. A swap
	# (one gun out, one in) leaves the number of held guns alone, so the
	# storefront only needs telling when that number moved.
	if len(was) != len(now):
		_enqueue_item_stock(item_code, item_row)


def _maybe_push_stock_from_sle(sle) -> None:
	"""Enqueue the WooCommerce sync a stock movement implies, if listed.

	Item-level (uniform-price) products get a stock-count push. PER-SERIAL
	firearms get a per-serial listing reconcile instead — ERPNext v16 flips
	Serial No status via a direct QB UPDATE (serial_batch_bundle.py) that
	never fires Serial No.on_update, so the SLE is the only reliable signal
	that a listed gun left stock (Create Order dispose, POS sale) or came
	back (cancellation)."""
	item_code = sle.item_code
	if not item_code:
		return

	item_row = frappe.db.get_value(
		"Item",
		item_code,
		["is_firearm", "item_group", "uniform_sell_price", "woo_product_id"],
		as_dict=True,
	) or {}

	# Per-serial firearm: reconcile each LISTED serial's own product; the
	# enqueued task decides push-vs-delist from the post-commit state.
	if _is_firearm_row(item_row) and not bool(item_row.get("uniform_sell_price")):
		_sync_row_serial_listings(sle)
		return

	if item_row.get("woo_product_id"):
		frappe.enqueue(
			"ffl_woo_sync.woocommerce.tasks.push_stock_task",
			queue="long",
			enqueue_after_commit=True,
			item_code=item_code,
		)
