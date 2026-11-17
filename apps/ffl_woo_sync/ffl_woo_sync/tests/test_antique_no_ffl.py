"""Antique firearms are not FFL-transfer products (GCA-exempt) — Woo push + POS queue.

Repro (a Woo order for ``Finnish M39 Antique``, $1840): the Item is
``is_firearm=1`` AND ``is_antique=1``.  Two defects fell out of that:

A. The Woo product was pushed with ``_firearm_product='yes'`` — the storefront plugin's "Requires
   FFL Shipment" flag — so the storefront demanded an FFL for a gun the GCA does not
   regulate.  ``build_serial_product_payload`` hard-coded ``'yes'``; the simple builder
   derived it from ``is_firearm``, which INCLUDES antiques.  The meta's correct
   predicate is "a *regulated* firearm" (``is_firearm and not is_antique``) — the same
   rule ``ffl_core.firearm.is_regulated_firearm_item`` gates the 4473/A&D layer on.

B. The order landed in the POS Pending Order queue with ZERO action buttons: the
   webhook writes no serial row for a non-regulated line, so ``needs_disposition`` and
   ``awaiting_serial`` are both false, and Ship/Pay/Edit/Cancel render for counter rows
   only.  POS has nothing to do with such an order — WooCommerce/ShipStation ships it —
   so it must not sit in the queue at all.

Boundaries these tests pin down (all four are live behaviours, not hypotheticals):

* ``is_firearm`` keeps its meaning — an antique still gets its ``pa_caliber`` /
  ``pa_condition`` attributes.  ONLY the ``_firearm_product`` meta narrows.
* A MIXED order (regulated gun + antique) still has a serial row and must still be
  listed and disposable.
* An awaiting-serial order has a serial row and is unaffected.
* A zero-serial *procure-to-store* order (an accessory routed through the shop) must
  STILL be listed — ``to_store_ready`` exists precisely so staff "ship the parcel (an
  accessory)".  Excluding purely on "no serial rows" would silently kill that queue.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce.mapping import (
	build_serial_product_payload,
	build_simple_product_payload,
	item_to_product_payload,
	serial_to_product_payload,
)


def _no_counter_holds():
	return patch("ffl_core.reservations.counter_holds", return_value={})


def _meta(payload: dict) -> dict:
	return {m["key"]: m["value"] for m in payload["meta_data"]}


def _attr_slugs(payload: dict) -> dict:
	return {a["slug"]: a["options"] for a in payload.get("attributes", [])}


# ===========================================================================
# A1. Pure builders — _firearm_product means "GCA-regulated", not "is a gun"
# ===========================================================================

class TestSimpleBuilderAntiqueMeta(FrappeTestCase):
	"""build_simple_product_payload — uniform-price / non-serial push."""

	def _payload(self, **kwargs):
		defaults = dict(
			item_code="ANTIQUE-001",
			item_name="Finnish M39 Antique",
			description="Pre-1899 rifle",
			standard_rate=1840.0,
			stock_qty=1,
			is_firearm=True,
			is_used=True,
			condition_grade="Good",
			caliber="7.62x54R",
			item_group="Rifle",
			image_srcs=[],
		)
		defaults.update(kwargs)
		return build_simple_product_payload(**defaults)

	def test_antique_firearm_is_not_an_ffl_product(self):
		"""The fix: an antique gun must NOT be flagged Requires-FFL on the store."""
		self.assertEqual(_meta(self._payload(is_antique=True))["_firearm_product"], "no")

	def test_regulated_firearm_is_still_an_ffl_product(self):
		"""Regression guard — an ordinary gun keeps requiring an FFL transfer."""
		self.assertEqual(_meta(self._payload(is_antique=False))["_firearm_product"], "yes")

	def test_antique_default_preserves_today_behaviour(self):
		"""Callers that don't pass is_antique keep the pre-fix result."""
		self.assertEqual(_meta(self._payload())["_firearm_product"], "yes")

	def test_antique_keeps_caliber_and_condition_attributes(self):
		"""``is_firearm`` still drives the pa_* attributes — ONLY the meta narrowed.
		An antique is still a gun in the catalogue and must stay filterable."""
		attrs = _attr_slugs(self._payload(is_antique=True))
		self.assertEqual(attrs.get("pa_caliber"), ["7.62x54R"])
		self.assertEqual(attrs.get("pa_condition"), ["Good"])

	def test_antique_is_not_ammunition(self):
		"""_ammunition_product keys off item_group and is untouched by this change."""
		self.assertEqual(_meta(self._payload(is_antique=True))["_ammunition_product"], "no")

	def test_non_firearm_unaffected(self):
		"""An accessory was already 'no' and stays 'no' whatever is_antique says."""
		payload = self._payload(is_firearm=False, is_antique=False, item_group="Accessories")
		self.assertEqual(_meta(payload)["_firearm_product"], "no")


class TestSerialBuilderAntiqueMeta(FrappeTestCase):
	"""build_serial_product_payload — the per-gun push the original order actually took."""

	def _payload(self, **kwargs):
		defaults = dict(
			item_code="Finnish M39 Antique",
			serial_no="205745",
			item_name="Finnish M39 Antique",
			description="Pre-1899 rifle",
			sell_price=1840.0,
			is_used=True,
			condition_grade="Good",
			caliber="7.62x54R",
		)
		defaults.update(kwargs)
		return build_serial_product_payload(**defaults)

	def test_antique_serial_is_not_an_ffl_product(self):
		"""The original order itself: SKU 'Finnish M39 Antique::205745' pushed Requires-FFL."""
		self.assertEqual(_meta(self._payload(is_antique=True))["_firearm_product"], "no")

	def test_regulated_serial_is_still_an_ffl_product(self):
		self.assertEqual(_meta(self._payload(is_antique=False))["_firearm_product"], "yes")

	def test_default_preserves_today_behaviour(self):
		"""The new kwarg defaults to 'regulated' — no caller changes behaviour by
		omission (the builder hard-coded 'yes' before this fix)."""
		self.assertEqual(_meta(self._payload())["_firearm_product"], "yes")

	def test_antique_keeps_condition_and_caliber_attributes(self):
		attrs = _attr_slugs(self._payload(is_antique=True))
		self.assertEqual(attrs.get("pa_caliber"), ["7.62x54R"])
		self.assertEqual(attrs.get("pa_condition"), ["Good"])

	def test_antique_keeps_its_serial_meta(self):
		"""The per-gun _osa_serial is orthogonal to the FFL flag."""
		self.assertEqual(_meta(self._payload(is_antique=True))["_osa_serial"], "205745")


# ===========================================================================
# A2. Thin loaders — the antique fact reaches the builders from the Item
# ===========================================================================

class TestLoadersThreadAntique(FrappeTestCase):

	def _mock_item(self, **kwargs):
		item = MagicMock()
		item.name = kwargs.get("item_code", "ANTIQUE-001")
		item.item_code = kwargs.get("item_code", "ANTIQUE-001")
		item.item_name = kwargs.get("item_name", "Finnish M39 Antique")
		item.description = kwargs.get("description", "")
		item.standard_rate = kwargs.get("standard_rate", 1840.0)
		item.item_group = kwargs.get("item_group", "Rifle")
		item.image_gallery = kwargs.get("image_gallery", [])
		item.get = lambda f, d=None: kwargs.get(f, d)
		return item

	def _mock_serial(self, **kwargs):
		serial = MagicMock()
		serial.name = kwargs.get("name", "205745")
		serial.item_code = kwargs.get("item_code", "Finnish M39 Antique")
		serial.status = kwargs.get("status", "Active")
		serial.image_gallery = kwargs.get("image_gallery", [])
		serial.get = lambda f, d=None: kwargs.get(f, d)
		return serial

	def test_item_loader_reads_antique_flag_from_the_item(self):
		item = self._mock_item(is_firearm=1, is_antique=1, uniform_sell_price=1,
			item_group="Rifle", caliber="7.62x54R")
		with patch("frappe.get_doc", return_value=item), \
			 patch("ffl_woo_sync.woocommerce.mapping.stock_for_item", return_value=1):
			payload = item_to_product_payload("ANTIQUE-001")
		self.assertEqual(_meta(payload)["_firearm_product"], "no")

	def test_item_loader_regulated_firearm_unchanged(self):
		item = self._mock_item(is_firearm=1, is_antique=0, uniform_sell_price=1,
			item_group="Rifle", caliber="5.56")
		with patch("frappe.get_doc", return_value=item), \
			 patch("ffl_woo_sync.woocommerce.mapping.stock_for_item", return_value=3):
			payload = item_to_product_payload("AR15-001")
		self.assertEqual(_meta(payload)["_firearm_product"], "yes")

	# The serial loader asks the counter-hold registry whether the gun is held; its real
	# readers reach now_datetime(), whose System Settings read would be the stubbed
	# get_doc's third call (StopIteration) whenever the settings are not cached yet.
	# Holds are not what these tests are about: none.
	def test_serial_loader_reads_antique_flag_from_the_parent_item(self):
		"""The exact original path: per-serial antique → one WC product per gun."""
		serial = self._mock_serial(name="205745", item_code="Finnish M39 Antique",
			sell_price=1840.0, condition_grade="Good", woo_reserved=0)
		item = self._mock_item(item_code="Finnish M39 Antique", is_firearm=1,
			is_antique=1, is_used=1, uniform_sell_price=0, standard_rate=0.0)
		with patch("frappe.get_doc", side_effect=[serial, item]), _no_counter_holds():
			payload = serial_to_product_payload("205745")
		self.assertEqual(payload["sku"], "Finnish M39 Antique::205745")
		self.assertEqual(_meta(payload)["_firearm_product"], "no")

	def test_serial_loader_regulated_firearm_unchanged(self):
		serial = self._mock_serial(name="GXK99", item_code="GLOCK-19",
			sell_price=649.0, condition_grade="Good", woo_reserved=0)
		item = self._mock_item(item_code="GLOCK-19", is_firearm=1, is_antique=0,
			is_used=1, uniform_sell_price=0, standard_rate=0.0)
		with patch("frappe.get_doc", side_effect=[serial, item]), _no_counter_holds():
			payload = serial_to_product_payload("GXK99")
		self.assertEqual(_meta(payload)["_firearm_product"], "yes")


# ===========================================================================
# B. The POS Pending Order queue — an order POS cannot act on must not be listed
# ===========================================================================

def _srow(item_code="GLOCK-19", confirmed_serial="", woo_line_id="7", unit_index=0):
	return frappe._dict({
		"item_code": item_code, "confirmed_serial": confirmed_serial,
		"woo_line_id": woo_line_id, "unit_index": unit_index,
		"stock_hold_serial": "", "reservation_error": "",
	})


def _order(name="WOO-ORD-01379", woo_order_id="4201"):
	return frappe._dict({
		"name": name, "site": "retail", "woo_status": "processing",
		"woo_order_id": woo_order_id, "customer_name": "Web Buyer",
		"atf_ffl_record": "", "ffl_license": "", "ffl_business_name": "",
		"ffl_expiration": "", "creation": "2026-07-28 10:00:00",
		"currency": "USD", "order_total": 1840.0, "total_tax": 0.0,
		"shipping_total": 0.0, "discount_total": 0.0,
	})


class TestQueueExcludesActionlessOrders(FrappeTestCase):

	def _rows(self, serials, *, lines=None, table_exists=False, get_all=None):
		doc = MagicMock()
		doc.serials = serials
		# Give ``lines`` a REAL shape. Left as an auto-MagicMock it is
		# truthy yet iterates empty — a production-impossible object, so the two
		# exclusion cases below would have passed without ever exercising the line
		# probe. Default = one resolvable accessory line, i.e. the ordinary "POS
		# genuinely has nothing to do" order.
		doc.lines = [_line("ACC-SLING")] if lines is None else lines
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders
		real_get_value = frappe.db.get_value

		def _get_value(doctype, name=None, *args, **kwargs):
			if doctype == "Item":
				return _item_row() if name == "ACC-SLING" else None
			return real_get_value(doctype, name, *args, **kwargs)

		from ffl_woo_sync.woocommerce import webhook
		get_all_patch = ({"side_effect": get_all} if get_all
			else {"return_value": [_order()]})
		with patch("frappe.only_for"), \
			 patch("frappe.get_all", **get_all_patch), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch("frappe.db.get_value", side_effect=_get_value), \
			 patch("frappe.db.table_exists", return_value=table_exists), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.nowdate", return_value="2026-07-29"), \
			 patch.object(webhook, "_log_error"):
			# Belt-and-braces isolation: no case in this class reaches the trace today
			# (each is either provably actionless or short-circuits before it), but
			# that is a property of the FIXTURES, not of the test — pinning it means a
			# future fixture tweak cannot silently start invoking the real
			# frappe.log_error from here. See the sibling helper in
			# TestQueueDropsOnlyProvenActionlessOrders for why the "cache poisoning"
			# rationale originally given for this stub was wrong.
			return list_pending_web_orders()

	def test_order_with_no_serial_rows_is_not_queued(self):
		"""POS has no action, so the order must not appear with zero buttons.

		⚠ SUPERSEDED AS THE ANTIQUE SHAPE, still live as a shape. When this was written
		an antique line wrote no serial row — that turned out to be the defect behind
		the original report (no reservation, no revenue, no stock relief, no delist), and antiques now
		get a serial row again (test_antique_inventory). This exact shape remains
		production-real for (a) a pure-accessory web order and (b) antique orders created
		BEFORE that fix, so it stays pinned here.
		"""
		self.assertEqual(self._rows([]), [])

	def test_mixed_regulated_plus_antique_order_is_still_queued(self):
		"""BOUNDARY: a regulated gun's serial row keeps the order listed AND
		dispose-ready. (Antiques now contribute a serial row of their own too — it is
		simply not an ACTION item; see test_antique_inventory.)"""
		rows = self._rows([_srow(item_code="GLOCK-19", confirmed_serial="SN-MIX")])
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["woo_online_order"], "WOO-ORD-01379")
		self.assertTrue(rows[0]["needs_disposition"])
		self.assertEqual(rows[0]["pending_serials"],
			[{"item_code": "GLOCK-19", "serial": "SN-MIX"}])

	def test_awaiting_serial_order_is_still_queued(self):
		"""BOUNDARY: a firearm unit whose serial hasn't arrived yet HAS a serial row
		(blank) — it must keep its awaiting-serial row, not vanish."""
		rows = self._rows([_srow(confirmed_serial="")])
		self.assertEqual(len(rows), 1)
		self.assertTrue(rows[0]["awaiting_serial"])
		self.assertFalse(rows[0]["needs_disposition"])

	def test_zero_serial_procure_to_store_order_is_still_queued(self):
		"""BOUNDARY: an accessory routed THROUGH the shop has no serial row either,
		but the store physically ships it — to_store_ready's 'arrived, awaiting shipment' signal exists
		for exactly this row. Excluding on 'no serial rows' alone would kill it."""
		rows = self._rows(
			[],
			table_exists=True,
			get_all=[
				[_order(name="WOO-ORD-TS", woo_order_id="4300")],
				[frappe._dict({"source_name": "WOO-ORD-TS", "order_type": "Procure To Store",
					"receive_status": "Received"})],
			],
		)
		self.assertEqual(len(rows), 1)
		self.assertTrue(rows[0]["to_store"])
		self.assertTrue(rows[0]["arrived"])

	def test_zero_serial_order_excluded_even_when_another_order_has_a_leg(self):
		"""The sibling test above short-circuits inside
		``_distributor_order_map`` (table_exists=False returns {} on its FIRST line), so
		the real production shape — ffl_integrations INSTALLED, Distributor Order table
		present, but no leg for THIS order — was never exercised. Here the table exists
		and the query returns a leg belonging to a DIFFERENT order, so the map is
		non-empty yet ``receive_map.get(o.name)`` still misses."""
		rows = self._rows(
			[],
			table_exists=True,
			get_all=[
				[_order()],  # the order under test: WOO-ORD-01379
				[frappe._dict({"source_name": "WOO-ORD-SOMEONE-ELSE", "order_type": "Procure To Store",
					"receive_status": "Received"})],
			],
		)
		self.assertEqual(rows, [])


# ===========================================================================
# B2. Dropping an order needs POSITIVE PROOF, not the "no serial rows" inference
# ===========================================================================

def _line(item_code="ACC-SLING", woo_line_id="7"):
	return frappe._dict({"item_code": item_code, "woo_line_id": woo_line_id, "qty": 1})


def _item_row(*, is_firearm=0, item_group="Accessories", is_antique=0):
	return frappe._dict({"is_firearm": is_firearm, "item_group": item_group,
		"is_antique": is_antique})


class TestQueueDropsOnlyProvenActionlessOrders(FrappeTestCase):
	"""An empty ``serials`` table is NOT by itself evidence that POS has nothing to
	do. The webhook also writes no serial row when it never RECOGNISED the firearm
	line: ``webhook._process_lines`` skips a blank-SKU WooCommerce line outright
	(no ``lines`` row either), and ``is_regulated_firearm_item`` reads False for an
	item_code that resolves to no Item at all (``get_value`` → None →
	``_is_regulated_firearm_row(None)`` → False).

	Dropping on the inference would make such an order vanish from the queue with no
	log and no counter, while ``_auto_clear_non_firearm_if_completed`` still retires
	it to Transferred and books a serial-less Sales Invoice — a real gun out the door,
	zero dispositions in the bound book, and no human ever having seen the row. That
	order was only ever REPORTED because it sat in the queue as an ugly zero-button
	row; fail-loud outranks a tidy UI on the compliance surface.

	So the queue drops an order only on positive proof: it HAS commerce lines, every
	line's item_code resolves to a real Item, and none of them is GCA-regulated.
	"""

	def _rows(self, serials, lines, items, *, table_exists=False, get_all=None):
		doc = MagicMock()
		doc.serials = serials
		doc.lines = lines
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders
		real_get_value = frappe.db.get_value

		def _get_value(doctype, name=None, *args, **kwargs):
			# Answer ONLY the Item probe under test and let every other read reach
			# the real primitive (docs/testing.md rule 4 — a blanket return_value
			# also answers core's own reads underneath the code being tested).
			if doctype == "Item":
				return items.get(name)
			return real_get_value(doctype, name, *args, **kwargs)

		from ffl_woo_sync.woocommerce import webhook
		get_all_patch = ({"side_effect": get_all} if get_all
			else {"return_value": [_order()]})
		with patch("frappe.only_for"), \
			 patch("frappe.get_all", **get_all_patch), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch("frappe.db.get_value", side_effect=_get_value), \
			 patch("frappe.db.table_exists", return_value=table_exists), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.nowdate", return_value="2026-07-29"), \
			 patch.object(webhook, "_log_error"):
			# _log_error is stubbed for ISOLATION, not convenience (the two tests that
			# assert on it build their own context). Without it the unprovable path
			# runs the REAL frappe.log_error, which builds an Error Log doc and calls
			# capture_exception()/get_error_metadata() — work these tests neither need
			# nor control, executed against an already-stubbed frappe.get_doc.
			#
			# CORRECTION (measured, not reasoned). An earlier version of this comment
			# claimed the real log_error would POISON the doc cache: that a stubbed
			# frappe.get_doc would land a MagicMock in get_cached_doc("System
			# Settings") and kill every later module. That was tested at the pre-fix
			# commit, in both a cross-process and a same-process/whole-app shape, and
			# did NOT reproduce — both stayed green. Reading frappe: log_error calls
			# frappe.get_doc directly but never goes through get_cached_doc, and only
			# get_cached_doc writes to the cache; a MagicMock is unpicklable besides,
			# so it cannot reach redis, and frappe.local.cache dies with the process.
			# The poisoning shape IS real in general — it bit a sibling repo whose stub
			# was a picklable module-level class — but it is not what this stub
			# prevents here. Kept for determinism and isolation, not for that reason.
			return list_pending_web_orders()

	# ---- proven actionless → dropped (the orders this feature is FOR) ----

	def test_antique_only_order_is_dropped(self):
		"""The line resolves to a real Item that is a firearm but ANTIQUE — GCA-exempt,
		nothing for POS to book. Proven → dropped.

		This is the LEGACY antique shape (no serial row), which orders placed before the
		inventory fix still have. The current shape — antique WITH a serial row, also
		dropped — is pinned in test_antique_inventory.
		"""
		rows = self._rows(
			[], [_line("Finnish M39 Antique")],
			{"Finnish M39 Antique": _item_row(is_firearm=1, item_group="Rifle", is_antique=1)},
		)
		self.assertEqual(rows, [])

	def test_pure_accessory_order_is_dropped(self):
		rows = self._rows([], [_line("ACC-SLING")], {"ACC-SLING": _item_row()})
		self.assertEqual(rows, [])

	# ---- NOT proven → stays visible for a human ----

	def test_an_unresolvable_item_code_keeps_the_order_visible(self):
		"""THE regression this class exists for: the SKU mapped to an item_code that
		matches no Item, so is_regulated_firearm_item() answered False for a line that
		may well be a gun. POS cannot prove it is actionless → it must stay on screen."""
		rows = self._rows([], [_line("GHOST-SKU-404")], {})
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["woo_online_order"], "WOO-ORD-01379")

	def test_one_bad_line_among_good_ones_keeps_the_order_visible(self):
		"""A mixed basket must not be dropped because the RESOLVABLE lines looked
		harmless — the unresolvable one is the whole risk."""
		rows = self._rows(
			[], [_line("ACC-SLING"), _line("GHOST-SKU-404", woo_line_id="8")],
			{"ACC-SLING": _item_row()},
		)
		self.assertEqual(len(rows), 1)

	def test_a_regulated_line_with_no_serial_row_keeps_the_order_visible(self):
		"""A REGULATED firearm line that produced no serial row is a webhook defect,
		not an actionless order. Dropping it would hide a gun that still owes a
		bound-book disposition."""
		rows = self._rows(
			[], [_line("GLOCK-19")],
			{"GLOCK-19": _item_row(is_firearm=1, item_group="Handgun")},
		)
		self.assertEqual(len(rows), 1)

	def test_an_order_with_no_lines_at_all_keeps_the_order_visible(self):
		"""Zero evidence is not proof. A WooCommerce line with a blank SKU is skipped
		by _process_lines BEFORE the ``lines`` row is appended, so a dropped firearm
		leaves nothing behind at all — exactly the shape that must never be silently
		retired."""
		rows = self._rows([], [], {})
		self.assertEqual(len(rows), 1)

	def test_a_blank_item_code_line_keeps_the_order_visible(self):
		rows = self._rows([], [_line("")], {})
		self.assertEqual(len(rows), 1)

	# ---- the proof only gates the DROP; it never resurrects a real row ----

	def test_a_serial_row_still_wins_without_any_item_probe(self):
		"""An order with a serial row is listed on its own merits — the lines probe
		must not be able to drop it (nor is it even consulted)."""
		rows = self._rows(
			[_srow(item_code="GLOCK-19", confirmed_serial="SN-MIX")],
			[_line("GHOST-SKU-404")], {},
		)
		self.assertEqual(len(rows), 1)
		self.assertTrue(rows[0]["needs_disposition"])


# ===========================================================================
# C. Fail-loud at the two points where an order can go quiet
# ===========================================================================

class TestUnrecognisedLineIsAudited(FrappeTestCase):
	"""Fail-loud AT THE POINT OF LOSS.

	``_process_lines`` skips a WooCommerce line with a blank SKU *before* it appends
	the ``lines`` row, so the line leaves no trace on the order at all. That is why
	``_cannot_prove_actionless`` cannot see it: a "good line + blank-SKU line" order
	looks, to the queue, exactly like a plain accessory order and gets dropped.

	Making ``lines`` a complete record was the structurally better fix,
	but the consumer census found it also reaches ``revenue._invoice_items``,
	``distributor.options`` and ``distributor.router`` — a phantom empty-item_code row
	would enter the ROUTING plan. Too wide for this change, so it is
	booked as a follow-up. What ships here is the other half of the original
	objection: the loss may stay invisible in the queue, but it must not stay UNKNOWN.
	One Error Log per affected order, naming the WC line ids.
	"""

	def _order_doc(self, woo_order_id="4201"):
		doc = MagicMock()
		doc.name = None  # _process_lines runs BEFORE insert — there is no name yet
		doc.woo_order_id = woo_order_id
		doc.lines = []
		doc.serials = []
		def _append(table, row):
			getattr(doc, table).append(frappe._dict(row))
			return frappe._dict(row)
		doc.append.side_effect = _append
		return doc

	def test_a_blank_sku_line_beside_a_good_one_is_reported_to_error_log(self):
		"""THE case the queue cannot see: the good line is recorded and would prove
		the order actionless, while the blank-SKU line vanishes. It must be audited."""
		from ffl_woo_sync.woocommerce import webhook
		order = self._order_doc()
		body_lines = [
			{"id": 7, "sku": "ACC-SLING", "quantity": 1, "price": "20",
				"subtotal": "20", "total": "20", "total_tax": "0", "meta_data": []},
			{"id": 8, "sku": "", "quantity": 1, "price": "1840",
				"subtotal": "1840", "total": "1840", "total_tax": "0", "meta_data": []},
		]
		# is_firearm_item is patched alongside is_regulated_firearm_item because the
		# serial-row gate now reads the INVENTORY test (antiques included). Left
		# unpatched it would reach the real Item table for these fixture SKUs, so the
		# assertions would depend on what happens to exist on the test site.
		with patch.object(webhook, "is_regulated_firearm_item", return_value=False), \
			 patch.object(webhook, "is_firearm_item", return_value=False), \
			 patch.object(webhook, "_log_error") as mock_err:
			webhook._process_lines(order, body_lines)

		# Control flow is UNCHANGED: the good line still becomes exactly one row.
		self.assertEqual(len(order.lines), 1)
		self.assertEqual(order.lines[0]["item_code"], "ACC-SLING")
		# ...and the skipped line is now auditable in Desk.
		self.assertTrue(mock_err.called, "a skipped blank-SKU line must reach Error Log")
		blob = " ".join(str(v) for v in mock_err.call_args.kwargs.values())
		self.assertIn("4201", blob)   # the WooCommerce order id
		self.assertIn("8", blob)      # the WooCommerce line id that was dropped

	def test_one_error_log_per_order_not_one_per_line(self):
		"""Two blank lines must not produce two Error Log entries — a webhook that
		spams the log gets muted by humans, which defeats the purpose."""
		from ffl_woo_sync.woocommerce import webhook
		order = self._order_doc()
		body_lines = [
			{"id": 8, "sku": "", "quantity": 1, "meta_data": []},
			{"id": 9, "sku": "   ", "quantity": 1, "meta_data": []},
		]
		# is_firearm_item is patched alongside is_regulated_firearm_item because the
		# serial-row gate now reads the INVENTORY test (antiques included). Left
		# unpatched it would reach the real Item table for these fixture SKUs, so the
		# assertions would depend on what happens to exist on the test site.
		with patch.object(webhook, "is_regulated_firearm_item", return_value=False), \
			 patch.object(webhook, "is_firearm_item", return_value=False), \
			 patch.object(webhook, "_log_error") as mock_err:
			webhook._process_lines(order, body_lines)
		self.assertEqual(mock_err.call_count, 1)
		blob = " ".join(str(v) for v in mock_err.call_args.kwargs.values())
		self.assertIn("8", blob)
		self.assertIn("9", blob)

	def test_an_ordinary_order_logs_nothing(self):
		"""No false alarms — a clean order must not write to Error Log at all,
		or the signal stops meaning anything."""
		from ffl_woo_sync.woocommerce import webhook
		order = self._order_doc()
		body_lines = [{"id": 7, "sku": "ACC-SLING", "quantity": 1, "price": "20",
			"subtotal": "20", "total": "20", "total_tax": "0", "meta_data": []}]
		# is_firearm_item is patched alongside is_regulated_firearm_item because the
		# serial-row gate now reads the INVENTORY test (antiques included). Left
		# unpatched it would reach the real Item table for these fixture SKUs, so the
		# assertions would depend on what happens to exist on the test site.
		with patch.object(webhook, "is_regulated_firearm_item", return_value=False), \
			 patch.object(webhook, "is_firearm_item", return_value=False), \
			 patch.object(webhook, "_log_error") as mock_err:
			webhook._process_lines(order, body_lines)
		self.assertFalse(mock_err.called)


class TestQueueTraceIsDiscoverable(FrappeTestCase):
	"""An audit trace nobody can find is decoration.

	``frappe.logger`` only reaches ``logs/*.log`` on the production box, which no one
	tails. The whole point of keeping an unprovable order visible is fail-loud, so the
	discoverable copy must land in **Error Log** (Desk-searchable, alertable) via the
	app's existing ``webhook._log_error`` helper.
	"""

	def test_an_unprovable_order_reaches_error_log(self):
		from ffl_woo_sync.woocommerce import webhook
		doc = MagicMock()
		doc.serials = []
		doc.lines = [_line("GHOST-SKU-404")]
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders
		real_get_value = frappe.db.get_value

		def _get_value(doctype, name=None, *args, **kwargs):
			if doctype == "Item":
				return None  # resolves to no Item — the unprovable case
			return real_get_value(doctype, name, *args, **kwargs)

		with patch("frappe.only_for"), \
			 patch("frappe.get_all", return_value=[_order()]), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch("frappe.db.get_value", side_effect=_get_value), \
			 patch("frappe.db.table_exists", return_value=False), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.nowdate", return_value="2026-07-29"), \
			 patch.object(webhook, "_log_error") as mock_err:
			rows = list_pending_web_orders()

		self.assertEqual(len(rows), 1)  # still visible in the queue
		self.assertTrue(mock_err.called, "the trace must reach Error Log, not only a logger")
		blob = " ".join(str(v) for v in mock_err.call_args.kwargs.values())
		self.assertIn("WOO-ORD-01379", blob)   # the POS order
		self.assertIn("4201", blob)            # the WooCommerce order id
		self.assertIn("GHOST-SKU-404", blob)   # the reason

	def test_a_provably_actionless_order_writes_no_error_log(self):
		"""The ordinary antique/accessory drop is not an incident — no log noise."""
		from ffl_woo_sync.woocommerce import webhook
		doc = MagicMock()
		doc.serials = []
		doc.lines = [_line("ACC-SLING")]
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders
		real_get_value = frappe.db.get_value

		def _get_value(doctype, name=None, *args, **kwargs):
			if doctype == "Item":
				return _item_row()
			return real_get_value(doctype, name, *args, **kwargs)

		with patch("frappe.only_for"), \
			 patch("frappe.get_all", return_value=[_order()]), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch("frappe.db.get_value", side_effect=_get_value), \
			 patch("frappe.db.table_exists", return_value=False), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.nowdate", return_value="2026-07-29"), \
			 patch.object(webhook, "_log_error") as mock_err:
			rows = list_pending_web_orders()

		self.assertEqual(rows, [])
		self.assertFalse(mock_err.called)
