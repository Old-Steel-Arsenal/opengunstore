"""Tests for woocommerce/webhook.py.

Covers:
- HMAC verification (_verify_hmac)
- FFL extraction (_extract_ffl)
- SKU mapping (_map_sku_to_pos)
- Soft-reserve: per-gun, uniform-oldest, no-stock, non-firearm
- Idempotency: duplicate woo_order_id → no second record
- Loop guard: woo_sync_inbound set during handler, cleared after
- No FFL Disposition created
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.reservations import CHANNEL_RESERVE_FIELDS
from ffl_core.tests.channel_query_doubles import warm_channel_meta

from ffl_woo_sync.woocommerce.webhook import (
	WebhookAuthError,
	_canonical_ffl_number,
	_extract_ffl,
	_ffl_is_empty,
	_handle_order_cancelled,
	_handle_order_created,
	_handle_order_upsert,
	_map_sku_to_pos,
	_origin_site,
	_pick_oldest_unreserved_serial,
	_recapture_ffl_if_empty,
	_verify_hmac,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECRET = "test-secret-123"
_RAW_BODY = b'{"id": 42, "status": "processing"}'


# Reads the drop-ship layer makes on a site that has it (its listing, and the
# extension switch every osa_dropship entry point checks): answered "not drop-ship".
_DROPSHIP_READS = ("Distributor Listing", "OSA Dropship Listing", "POS Extension")

def _make_sig(raw_body: bytes = _RAW_BODY, secret: str = _SECRET) -> str:
	return base64.b64encode(
		hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
	).decode("utf-8")


# Minimal FFL meta so tests that don't exercise FFL extraction don't
# trigger the REST API fallback (which requires a live WC connection).
_DEFAULT_FFL_META = [{"key": "_shipping_fflno", "value": "1-99-888-01-2D-09999"}]


def _order_body(woo_id: int = 42, line_items=None, meta_data=None) -> dict:
	"""Build a minimal WC order payload.

	``meta_data`` defaults to a single FFL license entry so tests that don't
	care about FFL extraction don't accidentally trigger the REST API fallback.
	Pass ``meta_data=[]`` explicitly to test the empty/fallback path.
	"""
	return {
		"id": woo_id,
		"status": "processing",
		"billing": {
			"first_name": "John",
			"last_name": "Doe",
			"email": "john@example.com",
			"phone": "555-1234",
		},
		"line_items": line_items or [],
		"meta_data": _DEFAULT_FFL_META if meta_data is None else meta_data,
	}


# ---------------------------------------------------------------------------
# HMAC verification
# ---------------------------------------------------------------------------

class TestVerifyHmac(FrappeTestCase):
	def test_valid_signature_accepted(self):
		sig = _make_sig()
		# Should not raise
		_verify_hmac(_RAW_BODY, sig, _SECRET)

	def test_invalid_signature_raises(self):
		with self.assertRaises(WebhookAuthError):
			_verify_hmac(_RAW_BODY, "AAAAAAAAAAAAAAAA==", _SECRET)

	def test_missing_signature_raises(self):
		with self.assertRaises(WebhookAuthError):
			_verify_hmac(_RAW_BODY, "", _SECRET)

	def test_wrong_secret_raises(self):
		sig = _make_sig(secret="other-secret")
		with self.assertRaises(WebhookAuthError):
			_verify_hmac(_RAW_BODY, sig, _SECRET)

	def test_uses_sha256(self):
		"""Signature must be HMAC-SHA256, not a weaker algorithm."""
		# Compute using MD5 (wrong) — should be rejected
		wrong_sig = base64.b64encode(
			hmac.new(_SECRET.encode(), _RAW_BODY, hashlib.md5).digest()
		).decode()
		with self.assertRaises(WebhookAuthError):
			_verify_hmac(_RAW_BODY, wrong_sig, _SECRET)


# ---------------------------------------------------------------------------
# SKU mapping
# ---------------------------------------------------------------------------

class TestMapSkuToPos(FrappeTestCase):
	def test_per_gun_sku_returns_item_and_serial(self):
		item, serial = _map_sku_to_pos("ITEM-001::SN-ABC")
		self.assertEqual(item, "ITEM-001")
		self.assertEqual(serial, "SN-ABC")

	def test_plain_sku_returns_item_no_serial(self):
		item, serial = _map_sku_to_pos("ITEM-001")
		self.assertEqual(item, "ITEM-001")
		self.assertIsNone(serial)

	def test_empty_serial_part_becomes_none(self):
		item, serial = _map_sku_to_pos("ITEM-001::")
		self.assertEqual(item, "ITEM-001")
		self.assertIsNone(serial)

	def test_whitespace_stripped(self):
		item, serial = _map_sku_to_pos("  ITEM-001  ::  SN-ABC  ")
		self.assertEqual(item, "ITEM-001")
		self.assertEqual(serial, "SN-ABC")


# ---------------------------------------------------------------------------
# FFL extraction
# ---------------------------------------------------------------------------

class TestExtractFfl(FrappeTestCase):
	def test_common_ffl_keys_extracted(self):
		meta = [
			{"key": "ffl_license_number", "value": "1-99-888-01-2D-09999"},
			{"key": "ffl_business_name", "value": "Test Gun Store"},
			{"key": "ffl_premise_street", "value": "100 Main St"},
			{"key": "ffl_premise_city", "value": "Dallas"},
			{"key": "ffl_premise_state", "value": "TX"},
			{"key": "ffl_premise_zip", "value": "75201"},
		]
		ffl = _extract_ffl(meta)
		self.assertEqual(ffl["license"], "1-99-888-01-2D-09999")
		self.assertEqual(ffl["business_name"], "Test Gun Store")
		self.assertEqual(ffl["street"], "100 Main St")
		self.assertEqual(ffl["city"], "Dallas")
		self.assertEqual(ffl["state"], "TX")
		self.assertEqual(ffl["zip"], "75201")

	def test_ffl_raw_stored_for_all_ffl_keys(self):
		meta = [
			{"key": "ffl_license_number", "value": "1-57-..."},
			{"key": "other_field", "value": "irrelevant"},
		]
		ffl = _extract_ffl(meta)
		# _raw should contain only ffl-related entries
		self.assertEqual(len(ffl["_raw"]), 1)
		self.assertEqual(ffl["_raw"][0]["key"], "ffl_license_number")

	def test_empty_meta_data_returns_empty_strings(self):
		ffl = _extract_ffl([])
		self.assertEqual(ffl["license"], "")
		self.assertEqual(ffl["_raw"], [])

	def test_alternate_key_names_picked_up(self):
		meta = [{"key": "receiving_ffl", "value": "9-99-..."}]
		ffl = _extract_ffl(meta)
		self.assertEqual(ffl["license"], "9-99-...")

	def test_non_dict_entries_skipped(self):
		meta = [None, "garbage", {"key": "ffl_license_number", "value": "1-57-..."}]
		ffl = _extract_ffl(meta)
		self.assertEqual(ffl["license"], "1-57-...")

	# --- Real storefront-plugin keys ---

	def test_real_ffl_core_license_key(self):
		"""_shipping_fflno is the canonical storefront-plugin key and wins over fallbacks."""
		meta = [
			{"key": "_shipping_fflno", "value": "1-99-888-01-2D-09999"},
			{"key": "ffl_license_number", "value": "WRONG"},  # fallback — must not win
		]
		ffl = _extract_ffl(meta)
		self.assertEqual(ffl["license"], "1-99-888-01-2D-09999")

	def test_real_ffl_core_all_premise_keys(self):
		"""All canonical _shipping_ffl_premise_* keys are resolved correctly."""
		meta = [
			{"key": "_shipping_ffl_name", "value": "Example Gun Store"},
			{"key": "_shipping_ffl_phone", "value": "555-9999"},
			{"key": "_shipping_ffl_premise_street", "value": "100 Main St"},
			{"key": "_shipping_ffl_premise_city", "value": "Springfield"},
			{"key": "_shipping_ffl_premise_state", "value": "TX"},
			{"key": "_shipping_ffl_premise_zip", "value": "75024"},
		]
		ffl = _extract_ffl(meta)
		self.assertEqual(ffl["business_name"], "Example Gun Store")
		self.assertEqual(ffl["phone"], "555-9999")
		self.assertEqual(ffl["street"], "100 Main St")
		self.assertEqual(ffl["city"], "Springfield")
		self.assertEqual(ffl["state"], "TX")
		self.assertEqual(ffl["zip"], "75024")

	def test_ffl_is_empty_when_all_blank(self):
		ffl = _extract_ffl([])
		self.assertTrue(_ffl_is_empty(ffl))

	def test_ffl_is_not_empty_when_license_present(self):
		meta = [{"key": "_shipping_fflno", "value": "1-57-..."}]
		ffl = _extract_ffl(meta)
		self.assertFalse(_ffl_is_empty(ffl))

	def test_placeholder_expiration_spliced_from_fflexp(self):
		"""The storefront plugin delivers fflno with a '00' expiration group and the real ATF
		code in _shipping_fflexp; _extract_ffl returns the spliced canonical
		license so FastBound (which validates the whole number) accepts it."""
		meta = [
			{"key": "_shipping_fflno", "value": "5-99-888-01-00-11762"},
			{"key": "_shipping_fflexp", "value": "9C"},
		]
		ffl = _extract_ffl(meta)
		self.assertEqual(ffl["license"], "5-99-888-01-9C-11762")
		self.assertEqual(ffl["expiration"], "9C")


class TestCanonicalFflNumber(FrappeTestCase):
	"""The storefront plugin stores the FFL number with a placeholder '00' expiration group and
	the real ATF expiration code in a separate field. ATF eZ Check only validates
	region+sequence so '...-00-...' "verifies", but FastBound checks the whole
	number and 400s 'Invalid FFL Number'. _canonical_ffl_number splices the real
	code back into the 5th group."""

	def test_splices_real_expiration_into_placeholder_group(self):
		self.assertEqual(
			_canonical_ffl_number("5-99-888-01-00-11762", "9C"),
			"5-99-888-01-9C-11762")

	def test_uppercases_lowercase_expiration_code(self):
		self.assertEqual(
			_canonical_ffl_number("3-99-888-07-00-07832", "8j"),
			"3-99-888-07-8J-07832")

	def test_leaves_already_correct_number_unchanged(self):
		self.assertEqual(
			_canonical_ffl_number("1-99-888-01-2D-09999", "2D"),
			"1-99-888-01-2D-09999")

	def test_never_clobbers_a_real_embedded_code(self):
		# Embedded group is a real code, not the '00' placeholder — keep it even if
		# the looser separate expiration field disagrees.
		self.assertEqual(
			_canonical_ffl_number("1-99-888-01-2D-09999", "9C"),
			"1-99-888-01-2D-09999")

	def test_missing_expiration_leaves_number_unchanged(self):
		self.assertEqual(
			_canonical_ffl_number("5-99-888-01-00-11762", ""),
			"5-99-888-01-00-11762")

	def test_non_canonical_format_returned_as_is(self):
		self.assertEqual(_canonical_ffl_number("not-an-ffl", "9C"), "not-an-ffl")

	def test_blank_license_returns_blank(self):
		self.assertEqual(_canonical_ffl_number("", "9C"), "")


class TestRecaptureFflIfEmpty(FrappeTestCase):
	"""Store-API (block) checkout fires order.created/updated before the FFL meta
	is committed, so the create path can land an empty FFL; a later order.updated
	carries it. _recapture_ffl_if_empty fills it in — but only when empty, never
	clobbering a staff-entered value — and routes through _extract_ffl so the
	'00' expiration placeholder is spliced to the real code."""

	def test_fills_empty_ffl_from_update_body_and_splices(self):
		order = frappe._dict({"ffl_license": "", "ffl_business_name": "",
			"ffl_phone": "", "ffl_premise_street": "", "ffl_premise_city": "",
			"ffl_premise_state": "", "ffl_premise_zip": "", "ffl_expiration": "",
			"ffl_raw": ""})
		body = {"id": 1350, "meta_data": [
			{"key": "_shipping_fflno", "value": "1-99-888-01-00-23777"},
			{"key": "_shipping_fflexp", "value": "7H"},
			{"key": "_shipping_ffl_name", "value": "CHUCK'S FIREARMS, INC"},
		]}
		changed = _recapture_ffl_if_empty(order, body)
		self.assertTrue(changed)
		self.assertEqual(order.ffl_license, "1-99-888-01-7H-23777")  # spliced
		self.assertEqual(order.ffl_business_name, "CHUCK'S FIREARMS, INC")
		self.assertEqual(order.ffl_expiration, "7H")

	def test_does_not_clobber_an_existing_ffl(self):
		order = frappe._dict({"ffl_license": "9-99-999-01-2D-12345",
			"ffl_business_name": "ALREADY SET"})
		body = {"id": 1, "meta_data": [
			{"key": "_shipping_fflno", "value": "1-99-888-01-00-23777"},
			{"key": "_shipping_fflexp", "value": "7H"},
		]}
		changed = _recapture_ffl_if_empty(order, body)
		self.assertFalse(changed)
		self.assertEqual(order.ffl_license, "9-99-999-01-2D-12345")
		self.assertEqual(order.ffl_business_name, "ALREADY SET")


# ---------------------------------------------------------------------------
# Soft-reserve + order creation (_handle_order_created)
# ---------------------------------------------------------------------------

def _channel_columns() -> list[str]:
	"""The channel flag/ref column names, WITHOUT asking frappe.

	`reservations.channel_reserve_columns()` filters the same list through
	`frappe.get_meta("Serial No")`, and these helpers are built INSIDE a
	`patch("frappe.db.get_value", ...)` block — which is exactly where frappe
	cannot build a Meta, because it builds one by reading the doctype through
	that call. A test double naming its own columns needs no database at all:
	`CHANNEL_RESERVE_FIELDS` is a plain tuple.

	Naming every declared channel rather than only the installed ones is right
	here too: a double should absorb a read it might not have to, not miss one.
	"""
	return [f for spec in CHANNEL_RESERVE_FIELDS for f in spec[:2]]


def _locked_row(**over):
	"""One row as the picker's LOCKING select actually returns it.

	That select carries the channel flag/ref columns
	(`["status", "is_transfer_gun", *channel_reserve_columns()]`), and
	`channel_reservation` re-reads any column its caller did not hand it —
	deliberately, because "I did not select that" must never be answered as "no
	channel holds it". A fixture row missing them therefore triggers an extra
	`get_value` per gun, which against a POSITIONAL script runs it dry:
	`StopIteration`, in a test about a lock.

	Naming the columns from `_channel_columns()` rather than by hand also means
	a third channel does not quietly reintroduce the same failure.
	"""
	row = {"status": "Active", "is_transfer_gun": 0}
	row.update({c: (0 if c.endswith("_reserved") else "")
		for c in _channel_columns()})
	row.update(over)
	return row


def _seq_except_dropship(answers):
	"""Wrap a POSITIONAL `frappe.db.get_value` script so the drop-ship probes do
	not consume its entries.

	`_process_lines` now resolves each line's fulfilment lane before the serial
	logic, which reads `Item.dropship_distributor` and
	`Distributor Listing.fulfillment_mode`. A scripted side_effect LIST answers by
	POSITION, so those two extra reads would silently shift every later answer by
	one and the reservation assertions below would fail for a reason that has
	nothing to do with what they test.

	This answers the drop-ship questions with None ("not a drop-ship item" — which
	is what these fixtures mean) and hands every OTHER read the next scripted
	answer, so no assertion is weakened. Same remedy as the P3 `_gv_except_dropship`
	pattern, applied to a positional script rather than a truthy stub.

	The cross-channel reservation read (GunBroker, 2026-08) is absorbed the same
	way and for the same reason. `_evict_blocking_hold` and friends ask for the
	channel flag/ref columns on their own, so the script shifted by one and
	`_enqueue_serial_or_item_push` was handed the item code where it expected a
	row — `AttributeError: 'str' object has no attribute 'get'`, three tests
	away from anything to do with channels. These fixtures mean "no other channel
	holds this gun", so that is what it answers; the channel dimension is covered
	in `test_channel_exclusion`.
	"""
	it = iter(answers)
	channel_columns = set(_channel_columns())

	def _gv(doctype=None, name=None, field=None, *a, **k):
		if doctype == "DocType":  # frappe loading a controller/meta: real, never a fixture
			return type(frappe.db).get_value(frappe.db, doctype, name, field, *a, **k)
		if field == "dropship_distributor" or doctype in _DROPSHIP_READS:
			return None
		if (channel_columns and isinstance(field, (list, tuple))
				and set(field) <= channel_columns):
			return frappe._dict({c: None for c in field})
		return next(it)

	return _gv


def _patch_db_exists(return_value=False):
	return patch("frappe.db.exists", return_value=return_value)


def _patch_new_doc():
	doc = MagicMock()
	doc.name = "WOO-ORD-00001"
	doc.lines = []
	doc.serials = []

	def append_row(table, row):
		getattr(doc, table).append(row)
	doc.append.side_effect = append_row

	return patch("frappe.new_doc", return_value=doc), doc


class TheTestHelpersDoNotNeedADatabase(FrappeTestCase):
	"""`_channel_columns` / `_locked_row` are built inside a `get_value` patch.

	Anything in there that asks frappe for a Meta dies, because frappe builds a
	Meta by reading the doctype through the very call being stubbed — and it
	dies three different ways depending on timing (`istable`, `RecursionError`,
	`DocType None not found`). So the property is pinned rather than remembered.
	"""

	def test_they_work_with_get_value_stubbed_to_a_fixture_row(self):
		fixture = {"item_code": "ITEM-001", "status": "Active",
			"woo_reserved": 1, "woo_order": "WOO-OTHER"}
		with patch("frappe.db.get_value", return_value=fixture), \
				patch("frappe.get_meta", side_effect=AssertionError(
					"a test double must not build a Meta while get_value is stubbed")):
			columns = _channel_columns()
			row = _locked_row(woo_reserved=1)
		self.assertIn("woo_reserved", columns)
		self.assertIn("gb_reserved", columns)
		self.assertEqual(row["woo_reserved"], 1)
		self.assertEqual(row["status"], "Active")


class TestSoftReserve(FrappeTestCase):
	def setUp(self):
		super().setUp()
		# Build Serial No's Meta while `frappe.db.get_value` is still the real
		# one. Every test below stubs it, and `_serial_availability` asks
		# `channel_reserve_columns()` -> `frappe.get_meta("Serial No")`, which
		# frappe builds by READING THE DOCTYPE through that same call. Handed a
		# fixture row instead, it builds a Meta out of it and then dies three
		# different ways depending on how far it gets:
		#     AttributeError: 'Meta' object has no attribute 'istable'
		#     RecursionError: maximum recursion depth exceeded  (get_meta ->
		#         Meta.__init__ -> load_from_db -> base_document.meta -> ...)
		#     DoesNotExistError: DocType None not found
		# One cause, three signatures. Warming it here is deterministic; relying
		# on an earlier test in the same process to have cached it is not, which
		# is why these passed in one full-suite run and failed in the next.

		warm_channel_meta()

	def test_idempotent_when_woo_order_id_exists(self):
		"""Duplicate order → no new doc created."""
		body = _order_body()
		with _patch_db_exists(return_value="WOO-ORD-00001"), \
			 patch("frappe.new_doc") as mock_new_doc:
			_handle_order_created(body)
		mock_new_doc.assert_not_called()

	def test_per_gun_sku_reserves_specific_serial(self):
		"""Per-gun SKU (item::serial) → that exact serial is reserved."""
		body = _order_body(line_items=[{
			"id": 1, "sku": "ITEM-001::SN-ABC", "quantity": 1, "price": "500.00"
		}])
		patcher_new, doc = _patch_new_doc()
		with _patch_db_exists(return_value=None), \
			 patcher_new, \
			 patch("ffl_woo_sync.woocommerce.webhook.is_regulated_firearm_item", return_value=True), \
			 patch("ffl_woo_sync.woocommerce.webhook.is_firearm_item", return_value=True), \
			 patch("frappe.db.get_value") as mock_gv, \
			 patch("frappe.db.set_value") as mock_sv, \
			 patch("frappe.enqueue"):
			mock_gv.side_effect = _seq_except_dropship([
				{"item_code": "ITEM-001", "status": "Active", "woo_reserved": 0, "woo_order": ""},  # _serial_unavailable_reason → free
				{"woo_product_id": "WC-SN", "item_code": "ITEM-001"},  # _enqueue_serial_or_item_push (retail)
			])
			_handle_order_created(body)
		# Serial SN-ABC should be reserved
		sv_calls = [c for c in mock_sv.call_args_list
			if c[0][0] == "Serial No" and c[0][1] == "SN-ABC"]
		self.assertEqual(len(sv_calls), 1)
		reserved = sv_calls[0][0][2]
		self.assertEqual(reserved.get("woo_reserved"), 1)
		self.assertEqual(reserved.get("woo_order"), "WOO-ORD-00001")

	def test_per_gun_sku_enqueues_push_serial_task(self):
		"""Per-gun SKU with woo_product_id → push_serial_task enqueued."""
		body = _order_body(line_items=[{
			"id": 1, "sku": "ITEM-001::SN-ABC", "quantity": 1, "price": "500.00"
		}])
		patcher_new, doc = _patch_new_doc()
		with _patch_db_exists(return_value=None), \
			 patcher_new, \
			 patch("ffl_woo_sync.woocommerce.webhook.is_regulated_firearm_item", return_value=True), \
			 patch("ffl_woo_sync.woocommerce.webhook.is_firearm_item", return_value=True), \
			 patch("frappe.db.get_value") as mock_gv, \
			 patch("frappe.db.set_value"), \
			 patch("frappe.enqueue") as mock_enqueue:
			mock_gv.side_effect = _seq_except_dropship([
				{"item_code": "ITEM-001", "status": "Active", "woo_reserved": 0, "woo_order": ""},  # _serial_unavailable_reason → free
				{"woo_product_id": "WC-SN", "item_code": "ITEM-001"},  # _enqueue_serial_or_item_push (retail)
			])
			_handle_order_created(body)
		enqueue_calls = [c for c in mock_enqueue.call_args_list
			if "push_serial_task" in c[0][0]]
		self.assertEqual(len(enqueue_calls), 1)
		self.assertEqual(enqueue_calls[0][1]["serial_no"], "SN-ABC")

	def test_uniform_sku_claims_a_unit_without_naming_a_rifle(self):
		"""Plain item SKU for a uniform-price firearm → the order claims a UNIT and
		reserves no Serial No.

		This test used to assert the opposite: that the oldest free rifle was parked
		on the row. That guess is what staff's own pick then contradicted, deadlocking
		seven paid orders in production on 2026-08-13 — so POS stopped making it. One
		product standing for many interchangeable rifles owes a unit; which rifle it is
		only becomes true when staff pull one off the rack.

		The unit is still spent: `mapping.unassigned_web_claims` subtracts it from what
		WooCommerce is told, and the item-level push below is what tells the store."""
		body = _order_body(line_items=[{
			"id": 1, "sku": "ITEM-001", "quantity": 1, "price": "500.00"
		}])
		patcher_new, doc = _patch_new_doc()
		with _patch_db_exists(return_value=None), \
			 patcher_new, \
			 patch("ffl_woo_sync.woocommerce.webhook.is_firearm_item",
				   return_value=True), \
			 patch("ffl_woo_sync.woocommerce.webhook.is_regulated_firearm_item",
				   return_value=True), \
			 patch("ffl_woo_sync.woocommerce.webhook._is_uniform_price_item",
				   return_value=True), \
			 patch("ffl_woo_sync.woocommerce.webhook._free_units_for_claim",
				   return_value=3), \
			 patch("ffl_woo_sync.woocommerce.webhook._pick_oldest_unreserved_serial") as mock_pick, \
			 patch("frappe.db.get_value", return_value={
				 "woo_product_id": "WC-1"}), \
			 patch("frappe.db.set_value") as mock_sv, \
			 patch("frappe.enqueue") as mock_enqueue:
			_handle_order_created(body)
		row = doc.serials[0]
		self.assertEqual(row.get("stock_hold_serial") or "", "")
		self.assertEqual(row.get("confirmed_serial") or "", "")
		self.assertEqual(row.get("reservation_error") or "", "")
		mock_pick.assert_not_called()
		self.assertEqual(
			[c for c in mock_sv.call_args_list if c[0][0] == "Serial No"], [])
		# …but the storefront still hears the count moved. Without this the database
		# would be right and the shop wrong until the next reconcile sweep.
		pushes = [c for c in mock_enqueue.call_args_list
			if "push_stock_task" in c[0][0] and c[1].get("item_code") == "ITEM-001"]
		self.assertEqual(len(pushes), 1)

	def test_no_stock_records_reservation_error(self):
		"""No available serials → reservation_error set on serial row, no crash."""
		body = _order_body(line_items=[{
			"id": 1, "sku": "ITEM-001", "quantity": 1, "price": "500.00"
		}])
		patcher_new, doc = _patch_new_doc()
		with _patch_db_exists(return_value=None), \
			 patcher_new, \
			 patch("frappe.db.get_value", return_value={"is_firearm": 1, "uniform_sell_price": 1}), \
			 patch("frappe.get_all", return_value=[]), \
			 patch("frappe.db.set_value"), \
			 patch("frappe.enqueue"), \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			_handle_order_created(body)
		# Serial row should have reservation_error set
		row = doc.serials[0]
		self.assertTrue((row.get("reservation_error") or "") != "")

	def test_non_firearm_sku_no_serial_reservation(self):
		"""Non-firearm item → line recorded, no serial set_value."""
		body = _order_body(line_items=[{
			"id": 1, "sku": "AMMO-001", "quantity": 2, "price": "25.00"
		}])
		patcher_new, doc = _patch_new_doc()
		with _patch_db_exists(return_value=None), \
			 patcher_new, \
			 patch("frappe.db.get_value", return_value={"is_firearm": 0, "uniform_sell_price": 0}), \
			 patch("frappe.db.set_value") as mock_sv, \
			 patch("frappe.enqueue"):
			_handle_order_created(body)
		sn_sv = [c for c in mock_sv.call_args_list if c[0][0] == "Serial No"]
		self.assertEqual(len(sn_sv), 0)

	def test_ffl_fields_captured_on_order(self):
		"""FFL meta_data → captured on Woo Online Order doc."""
		meta = [
			{"key": "ffl_license_number", "value": "1-99-888-01-2D-09999"},
			{"key": "ffl_business_name", "value": "Test FFL Store"},
		]
		body = _order_body(meta_data=meta)
		patcher_new, doc = _patch_new_doc()
		with _patch_db_exists(return_value=None), \
			 patcher_new, \
			 patch("frappe.db.get_value", return_value={}), \
			 patch("frappe.db.set_value"), \
			 patch("frappe.enqueue"):
			_handle_order_created(body)
		self.assertEqual(doc.ffl_license, "1-99-888-01-2D-09999")
		self.assertEqual(doc.ffl_business_name, "Test FFL Store")

	def test_no_ffl_disposition_created(self):
		"""Webhook handler NEVER creates an FFL Disposition."""
		body = _order_body(line_items=[{
			"id": 1, "sku": "ITEM-001::SN-ABC", "quantity": 1, "price": "500.00"
		}])
		patcher_new, doc = _patch_new_doc()
		ffl_disp_created = []

		def _track_new_doc(doctype):
			if doctype == "FFL Disposition":
				ffl_disp_created.append(doctype)
			return doc

		with _patch_db_exists(return_value=None), \
			 patch("frappe.new_doc", side_effect=_track_new_doc), \
			 patch("frappe.db.get_value") as mock_gv, \
			 patch("frappe.db.set_value"), \
			 patch("frappe.enqueue"):
			# C1: FOR UPDATE check first, then sn_row lookup
			mock_gv.side_effect = _seq_except_dropship([
				{"woo_reserved": 0, "status": "Active"},   # FOR UPDATE re-check
				{"woo_product_id": "WC-SN", "item_code": "ITEM-001"},  # sn_row
			])
			_handle_order_created(body)
		self.assertEqual(ffl_disp_created, [])

	def test_missing_order_id_is_logged_not_raised(self):
		"""body without 'id' → logs error, no exception."""
		body = {"status": "processing", "billing": {}, "line_items": []}
		with patch("ffl_woo_sync.woocommerce.webhook._log_error") as mock_log:
			_handle_order_created(body)
		mock_log.assert_called_once()

	def test_ffl_fallback_via_rest_api_when_meta_empty(self):
		"""When webhook meta_data has no FFL, GET /orders fallback is called."""
		body = _order_body(meta_data=[])  # empty meta → _ffl_is_empty True → REST fallback
		patcher_new, doc = _patch_new_doc()
		rest_ffl = {
			"license": "1-99-888", "business_name": "Test FFL", "phone": "555-0000",
			"street": "1 Main", "city": "Dallas", "state": "TX", "zip": "75001",
			"expiration": "",
			"_raw": [{"key": "_shipping_fflno", "value": "1-99-888"}],
		}
		with _patch_db_exists(return_value=None), \
			 patcher_new, \
			 patch("ffl_woo_sync.woocommerce.webhook._fetch_order_ffl", return_value=rest_ffl) as mock_fetch, \
			 patch("frappe.db.get_value", return_value={}), \
			 patch("frappe.db.set_value"), \
			 patch("frappe.enqueue"):
			_handle_order_created(body)
		mock_fetch.assert_called_once_with("42")
		self.assertEqual(doc.ffl_license, "1-99-888")
		self.assertEqual(doc.ffl_phone, "555-0000")

	_EMPTY_FFL = {"license": "", "business_name": "", "phone": "", "street": "",
		"city": "", "state": "", "zip": "", "expiration": "", "_raw": []}

	def _created_with_unresolved_ffl(self, *, firearm_line: bool):
		"""Run order.created with FFL missing from BOTH the payload and the REST
		fallback; ``firearm_line`` decides whether line processing yields a serial
		row (a gun on the order) or nothing (an accessory-only order)."""
		body = _order_body(meta_data=[])  # empty meta → fallback called → empty
		patcher_new, doc = _patch_new_doc()

		def _lines(order, items):
			if firearm_line:
				order.serials.append({"item_code": "GLOCK-19", "woo_line_id": "1"})
			return []

		with _patch_db_exists(return_value=None), \
			 patcher_new, \
			 patch("ffl_woo_sync.woocommerce.webhook._fetch_order_ffl", return_value=self._EMPTY_FFL), \
			 patch("ffl_woo_sync.woocommerce.webhook._process_lines", side_effect=_lines), \
			 patch("frappe.db.get_value", return_value={}), \
			 patch("frappe.db.set_value"), \
			 patch("frappe.enqueue"), \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error") as mock_log:
			_handle_order_created(body)
		return [c[1]["title"] for c in mock_log.call_args_list]

	def test_ffl_warning_logged_when_rest_fallback_also_empty(self):
		"""When both webhook meta AND REST API yield no FFL, a warning is logged —
		for an order that actually carries a firearm."""
		titles = self._created_with_unresolved_ffl(firearm_line=True)
		self.assertTrue(any("FFL not resolved" in t for t in titles))

	def test_no_ffl_warning_for_an_accessory_only_order(self):
		"""A bayonet / optic order has no FFL to resolve; the alert fired on every
		one of them and buried the real ones."""
		titles = self._created_with_unresolved_ffl(firearm_line=False)
		self.assertFalse(any("FFL not resolved" in t for t in titles))


# ---------------------------------------------------------------------------
# D1-prevention: order cancel / delete / refund → release soft-reserve
# ---------------------------------------------------------------------------

class TestOrderCancelRelease(FrappeTestCase):
	"""_handle_order_cancelled releases reserved serials + cancels the order."""

	def test_releases_reserved_serial_and_cancels_order(self):
		"""Per-serial product: serial released, order Cancelled, push_serial_task enqueued."""
		body = {"id": 42, "status": "cancelled"}
		reserved = [{"name": "SN-1", "item_code": "GLOCK-19", "woo_product_id": "WC-SN-1"}]

		def _gv(doctype, name, field, *a, **k):
			if doctype == "Woo Online Order":
				return {"name": "WOO-ORD-00042", "site": "retail"}
			return None

		with patch("frappe.db.get_value", side_effect=_gv), \
			 patch("frappe.get_all",
				 side_effect=lambda dt, *a, **k: reserved if dt == "Serial No" else []), \
			 patch("frappe.db.set_value") as mock_sv, \
			 patch("frappe.enqueue") as mock_enq:
			_handle_order_cancelled(body)

		# Serial released: woo_reserved=0, woo_order=""
		sn_calls = [c for c in mock_sv.call_args_list
			if c[0][0] == "Serial No" and c[0][1] == "SN-1"]
		self.assertEqual(len(sn_calls), 1)
		released = sn_calls[0][0][2]
		self.assertEqual(released["woo_reserved"], 0)
		self.assertEqual(released["woo_order"], "")

		# Order marked Cancelled (+ raw WC status mirrored to woo_status).
		# Scoped to the CANCELLATION write: the `woo_order_cancelled` hook also writes
		# this doctype (it clears `dropship_status`), and that write has
		# always happened in production. It only became visible here once the hook
		# stopped dying on the blanket get_all stub above, so count the write we mean
		# rather than every write to the doctype.
		ord_calls = [c for c in mock_sv.call_args_list
			if c[0][0] == "Woo Online Order"
			and isinstance(c[0][2], dict) and "order_status" in c[0][2]]
		self.assertEqual(len(ord_calls), 1)
		vals = ord_calls[0][0][2]
		self.assertEqual(vals["order_status"], "Cancelled")
		self.assertEqual(vals["woo_status"], "cancelled")

		# Outbound re-push enqueued for the freed per-serial product
		ps = [c for c in mock_enq.call_args_list if "push_serial_task" in c[0][0]]
		self.assertEqual(len(ps), 1)
		self.assertEqual(ps[0][1]["serial_no"], "SN-1")
		# A cancel frees the gun for sale: the re-push must pass the hidden-serial
		# guard, as _release_serial's does, or a machine-delisted gun stays hidden.
		self.assertIs(ps[0][1]["relist"], True)

	def test_item_level_serial_enqueues_push_stock(self):
		"""Serial without its own product → parent item push_stock_task enqueued."""
		body = {"id": 42, "status": "refunded"}
		reserved = [{"name": "SN-2", "item_code": "REVOLVER-1", "woo_product_id": ""}]

		def _gv(doctype, name, field, *a, **k):
			if doctype == "Woo Online Order":
				return {"name": "WOO-ORD-00042", "site": "retail"}
			if doctype == "Item":
				return "WC-ITEM-9"  # parent item has a WC product
			return None

		with patch("frappe.db.get_value", side_effect=_gv), \
			 patch("frappe.get_all",
				 side_effect=lambda dt, *a, **k: reserved if dt == "Serial No" else []), \
			 patch("frappe.db.set_value"), \
			 patch("frappe.enqueue") as mock_enq:
			_handle_order_cancelled(body)

		pst = [c for c in mock_enq.call_args_list if "push_stock_task" in c[0][0]]
		self.assertEqual(len(pst), 1)
		self.assertEqual(pst[0][1]["item_code"], "REVOLVER-1")
		# No per-serial push for an item-level serial
		ps = [c for c in mock_enq.call_args_list if "push_serial_task" in c[0][0]]
		self.assertEqual(len(ps), 0)

	def test_no_matching_order_is_noop(self):
		"""Unknown woo_order_id → no serial/order writes, no enqueue."""
		body = {"id": 999, "status": "cancelled"}
		with patch("frappe.db.get_value", return_value=None), \
			 patch("frappe.get_all") as mock_ga, \
			 patch("frappe.db.set_value") as mock_sv, \
			 patch("frappe.enqueue") as mock_enq:
			_handle_order_cancelled(body)
		mock_ga.assert_not_called()
		mock_sv.assert_not_called()
		mock_enq.assert_not_called()

	def test_missing_id_logged_not_raised(self):
		"""Cancel body without 'id' → logs error, no exception."""
		with patch("ffl_woo_sync.woocommerce.webhook._log_error") as mock_log:
			_handle_order_cancelled({"status": "cancelled"})
		mock_log.assert_called_once()

	def test_multiple_serials_same_item_dedups_push_stock(self):
		"""Two item-level serials sharing an item → a single push_stock_task."""
		body = {"id": 42, "status": "cancelled"}
		reserved = [
			{"name": "SN-A", "item_code": "REVOLVER-1", "woo_product_id": ""},
			{"name": "SN-B", "item_code": "REVOLVER-1", "woo_product_id": ""},
		]

		def _gv(doctype, name, field, *a, **k):
			if doctype == "Woo Online Order":
				return {"name": "WOO-ORD-00042", "site": "retail"}
			if doctype == "Item":
				return "WC-ITEM-9"
			return None

		with patch("frappe.db.get_value", side_effect=_gv), \
			 patch("frappe.get_all",
				 side_effect=lambda dt, *a, **k: reserved if dt == "Serial No" else []), \
			 patch("frappe.db.set_value") as mock_sv, \
			 patch("frappe.enqueue") as mock_enq:
			_handle_order_cancelled(body)

		# Both serials released
		sn_released = [c for c in mock_sv.call_args_list if c[0][0] == "Serial No"]
		self.assertEqual(len(sn_released), 2)
		# But only one push_stock_task (deduped on item_code)
		pst = [c for c in mock_enq.call_args_list if "push_stock_task" in c[0][0]]
		self.assertEqual(len(pst), 1)


class TestOrderUpsertRouting(FrappeTestCase):
	"""_handle_order_upsert routes by order status; _HANDLERS wires the topics."""

	def test_cancelled_status_routes_to_release(self):
		for status in ("cancelled", "canceled", "refunded", "trash"):
			with self.subTest(status=status):
				body = {"id": 42, "status": status}
				with patch("ffl_woo_sync.woocommerce.webhook._handle_order_cancelled") as mock_cancel, \
					 patch("ffl_woo_sync.woocommerce.webhook._handle_order_created") as mock_create:
					_handle_order_upsert(body)
				mock_cancel.assert_called_once_with(body)
				mock_create.assert_not_called()

	def test_live_status_routes_to_create(self):
		for status in ("processing", "completed", "on-hold", "pending"):
			with self.subTest(status=status):
				body = {"id": 42, "status": status}
				with patch("ffl_woo_sync.woocommerce.webhook._handle_order_cancelled") as mock_cancel, \
					 patch("ffl_woo_sync.woocommerce.webhook._handle_order_created") as mock_create:
					_handle_order_upsert(body)
				mock_create.assert_called_once_with(body)
				mock_cancel.assert_not_called()

	def test_draft_and_failed_statuses_route_to_release(self):
		"""Pre-placement drafts + failed orders must NOT reserve — route to release.

		A WooCommerce block/Store-API checkout fires order.created/updated with
		status 'checkout-draft' the moment a shopper opens checkout, before placing
		the order; reserving then would hold a firearm out of stock for a mere
		browser (the stock-shows-2-not-3 phantom)."""
		for status in ("checkout-draft", "draft", "auto-draft", "failed"):
			with self.subTest(status=status):
				body = {"id": 42, "status": status}
				with patch("ffl_woo_sync.woocommerce.webhook._handle_order_cancelled") as mock_cancel, \
					 patch("ffl_woo_sync.woocommerce.webhook._handle_order_created") as mock_create:
					_handle_order_upsert(body)
				mock_cancel.assert_called_once_with(body)
				mock_create.assert_not_called()

	def test_handlers_wire_created_updated_to_upsert(self):
		from ffl_woo_sync.woocommerce.webhook import _HANDLERS
		self.assertIs(_HANDLERS["order.created"], _handle_order_upsert)
		self.assertIs(_HANDLERS["order.updated"], _handle_order_upsert)

	def test_order_deleted_topic_wired_to_cancel(self):
		from ffl_woo_sync.woocommerce.webhook import _HANDLERS
		self.assertIs(_HANDLERS["order.deleted"], _handle_order_cancelled)


# ---------------------------------------------------------------------------
# Loop guard (tested at the woo_event level)
# ---------------------------------------------------------------------------

class TestLoopGuard(FrappeTestCase):
	def _make_request_mocks(self, raw_body: bytes, sig: str, topic: str):
		"""Context manager stack for woo_event call."""
		from unittest.mock import patch
		return [
			patch("ffl_woo_sync.woocommerce.webhook._read_raw_body", return_value=raw_body),
			patch("ffl_woo_sync.woocommerce.webhook._get_webhook_secret", return_value=_SECRET),
			patch("frappe.get_request_header", side_effect=lambda h: {
				"X-WC-Webhook-Signature": sig,
				"x-wc-webhook-signature": sig,
				"X-WC-Webhook-Topic": topic,
				"x-wc-webhook-topic": topic,
			}.get(h, "")),
		]

	def test_inbound_flag_set_during_handler(self):
		"""frappe.flags.woo_sync_inbound is True inside the handler."""
		from ffl_woo_sync.woocommerce.webhook import woo_event
		raw_body = json.dumps({"id": 99}).encode()
		sig = _make_sig(raw_body)
		flag_captured = []

		def capturing_handler(body):
			flag_captured.append(frappe.flags.get("woo_sync_inbound"))

		_sig_headers = {"X-WC-Webhook-Signature", "x-wc-webhook-signature"}

		def _header_side_effect(name):
			return sig if name in _sig_headers else ""

		with patch("ffl_woo_sync.woocommerce.webhook._read_raw_body", return_value=raw_body), \
			 patch("ffl_woo_sync.woocommerce.webhook._get_webhook_secret", return_value=_SECRET), \
			 patch("frappe.get_request_header", side_effect=_header_side_effect), \
			 patch("ffl_woo_sync.woocommerce.webhook._HANDLERS", {"": capturing_handler}), \
			 patch("frappe.db.commit"):
			woo_event()

		self.assertTrue(flag_captured[0])

	def test_inbound_flag_cleared_after_handler(self):
		"""frappe.flags.woo_sync_inbound is False after woo_event returns."""
		from ffl_woo_sync.woocommerce.webhook import woo_event
		raw_body = json.dumps({"id": 99}).encode()
		sig = _make_sig(raw_body)

		_sig_headers = {"X-WC-Webhook-Signature", "x-wc-webhook-signature"}

		def _header_side_effect(name):
			return sig if name in _sig_headers else ""

		frappe.flags.woo_sync_inbound = False
		with patch("ffl_woo_sync.woocommerce.webhook._read_raw_body", return_value=raw_body), \
			 patch("ffl_woo_sync.woocommerce.webhook._get_webhook_secret", return_value=_SECRET), \
			 patch("frappe.get_request_header", side_effect=_header_side_effect), \
			 patch("ffl_woo_sync.woocommerce.webhook._HANDLERS", {"": lambda b: None}), \
			 patch("frappe.db.commit"):
			woo_event()
		self.assertFalse(frappe.flags.get("woo_sync_inbound"))

	def test_inbound_flag_cleared_after_handler_exception(self):
		"""frappe.flags.woo_sync_inbound is reset even when handler raises."""
		from ffl_woo_sync.woocommerce.webhook import woo_event
		raw_body = json.dumps({"id": 99}).encode()
		sig = _make_sig(raw_body)

		def failing_handler(body):
			raise RuntimeError("boom")

		_sig_headers = {"X-WC-Webhook-Signature", "x-wc-webhook-signature"}

		def _header_side_effect(name):
			return sig if name in _sig_headers else ""

		frappe.flags.woo_sync_inbound = False
		with patch("ffl_woo_sync.woocommerce.webhook._read_raw_body", return_value=raw_body), \
			 patch("ffl_woo_sync.woocommerce.webhook._get_webhook_secret", return_value=_SECRET), \
			 patch("frappe.get_request_header", side_effect=_header_side_effect), \
			 patch("ffl_woo_sync.woocommerce.webhook._HANDLERS", {"": failing_handler}), \
			 patch("frappe.db.commit"), \
			 patch("frappe.db.rollback"), \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			result = woo_event()
		self.assertFalse(frappe.flags.get("woo_sync_inbound"))
		self.assertFalse(result["ok"])


# ---------------------------------------------------------------------------
# C1: Serial reservation race — two-phase FOR UPDATE lock
# ---------------------------------------------------------------------------

class TestC1SerialRaceLock(FrappeTestCase):
	"""C1: Two-phase lock prevents double-booking of the same physical firearm."""

	def setUp(self):
		super().setUp()
		# Build Serial No's Meta while `frappe.db.get_value` is still the real
		# one. Every test below stubs it, and `_serial_availability` asks
		# `channel_reserve_columns()` -> `frappe.get_meta("Serial No")`, which
		# frappe builds by READING THE DOCTYPE through that same call. Handed a
		# fixture row instead, it builds a Meta out of it and then dies three
		# different ways depending on how far it gets:
		#     AttributeError: 'Meta' object has no attribute 'istable'
		#     RecursionError: maximum recursion depth exceeded  (get_meta ->
		#         Meta.__init__ -> load_from_db -> base_document.meta -> ...)
		#     DoesNotExistError: DocType None not found
		# One cause, three signatures. Warming it here is deterministic; relying
		# on an earlier test in the same process to have cached it is not, which
		# is why these passed in one full-suite run and failed in the next.

		warm_channel_meta()

	def test_per_gun_race_logs_error_and_sets_reservation_error(self):
		"""Per-gun SKU where serial already reserved → reservation_error on serial row."""
		body = _order_body(line_items=[{
			"id": 1, "sku": "ITEM-001::SN-RACE", "quantity": 1, "price": "500.00"
		}])
		patcher_new, doc = _patch_new_doc()
		with _patch_db_exists(return_value=None), \
			 patcher_new, \
			 patch("ffl_woo_sync.woocommerce.webhook.is_regulated_firearm_item", return_value=True), \
			 patch("ffl_woo_sync.woocommerce.webhook.is_firearm_item", return_value=True), \
			 patch("frappe.db.get_value") as mock_gv, \
			 patch("frappe.db.set_value"), \
			 patch("frappe.enqueue"), \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error") as mock_log:
			mock_gv.return_value = {"item_code": "ITEM-001", "status": "Active", "woo_reserved": 1, "woo_order": "WOO-OTHER"}
			_handle_order_created(body)

		self.assertEqual(len(doc.lines), 1)
		self.assertIn("SN-RACE", doc.serials[0].get("reservation_error", ""))
		race_calls = [c for c in mock_log.call_args_list
			if "SN-RACE" in (c[1].get("title") or "")]
		self.assertTrue(len(race_calls) >= 1)

	def test_per_gun_race_does_not_reserve_taken_serial(self):
		"""Per-gun race lost → Serial No woo_reserved is NOT set via set_value."""
		body = _order_body(line_items=[{
			"id": 1, "sku": "ITEM-001::SN-RACE", "quantity": 1, "price": "500.00"
		}])
		patcher_new, doc = _patch_new_doc()
		with _patch_db_exists(return_value=None), \
			 patcher_new, \
			 patch("ffl_woo_sync.woocommerce.webhook.is_regulated_firearm_item", return_value=True), \
			 patch("ffl_woo_sync.woocommerce.webhook.is_firearm_item", return_value=True), \
			 patch("frappe.db.get_value") as mock_gv, \
			 patch("frappe.db.set_value") as mock_sv, \
			 patch("frappe.enqueue"), \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			mock_gv.return_value = {"item_code": "ITEM-001", "status": "Active", "woo_reserved": 1, "woo_order": "WOO-OTHER"}
			_handle_order_created(body)

		# No reservation write for this serial
		sn_sv = [c for c in mock_sv.call_args_list if c[0][0] == "Serial No"]
		self.assertEqual(len(sn_sv), 0)

	def test_picker_skips_a_candidate_that_lost_the_race(self):
		"""First candidate reserved between the SELECT and the lock → take the next.

		Exercised directly on `_pick_oldest_unreserved_serial` rather than through a
		uniform-price order, because a uniform-price order no longer picks at all (it
		claims a unit and names nothing). The picker itself still runs — a per-gun SKU
		carrying qty>1 falls back to it — and its two-phase lock is what stops two
		concurrent webhooks booking one rifle, so it keeps its own test."""
		from ffl_woo_sync.woocommerce.webhook import _pick_oldest_unreserved_serial
		with patch("frappe.get_all", return_value=[
				 {"name": "SN-TAKEN"}, {"name": "SN-FREE"}]), \
			 patch("ffl_core.reservations.counter_holds", return_value={}), \
			 patch("ffl_woo_sync.woocommerce.webhook.custody_warehouse_names",
				   return_value=set()), \
			 patch("frappe.db.get_value", side_effect=[
				 _locked_row(woo_reserved=1),   # SN-TAKEN → lost the race
				 _locked_row(),                 # SN-FREE  → still free
			 ]) as mock_gv:
			self.assertEqual(_pick_oldest_unreserved_serial("ITEM-001"), "SN-FREE")
		# Both re-reads must be locking — dropping for_update is how two orders end
		# up believing they hold the same gun.
		self.assertTrue(all(c.kwargs.get("for_update") for c in mock_gv.call_args_list))


# ---------------------------------------------------------------------------
# C2: per-(woo_order_id, site) scoping + composite uniqueness
# ---------------------------------------------------------------------------

class TestC2SiteScopedOrders(FrappeTestCase):
	"""C2: woo_order_id is a store's own auto-increment, so it is unique only per
	site — every Woo Online Order lookup is site-scoped, and validate() enforces
	composite (woo_order_id, site) uniqueness."""

	def tearDown(self):
		super().tearDown()
		frappe.flags.woo_sync_site = None

	def test_validate_rejects_duplicate_same_site(self):
		"""validate() throws when another order shares (woo_order_id, site)."""
		doc = frappe.get_doc({
			"doctype": "Woo Online Order", "woo_order_id": "55", "site": "retail",
		})
		with patch("frappe.db.exists", return_value="WOO-ORD-DUP"), \
			 self.assertRaises(frappe.ValidationError):
			doc.validate()

	def test_validate_looks_up_the_composite_key_blank_site_as_retail(self):
		"""validate() checks (woo_order_id, site) — a blank site is the retail store."""
		doc = frappe.get_doc({
			"doctype": "Woo Online Order", "woo_order_id": "55", "site": "",
		})
		captured = {}

		def _exists(doctype, filters):
			captured["filters"] = filters
			return None  # no #55 on file for that store

		with patch("frappe.db.exists", side_effect=_exists):
			doc.validate()  # must not raise
		self.assertEqual(captured["filters"].get("woo_order_id"), "55")
		self.assertEqual(captured["filters"].get("site"), "retail")

# ---------------------------------------------------------------------------
# I1: woo_event — config error returns 200, not 401
# ---------------------------------------------------------------------------

class TestWooEventEdgeCases(FrappeTestCase):
	"""Edge cases at the woo_event entry point level."""

	def test_config_error_returns_200_not_401(self):
		"""Auth block config error (RuntimeError) → 200 to prevent WC webhook disable."""
		from ffl_woo_sync.woocommerce.webhook import woo_event
		raw_body = json.dumps({"id": 1}).encode()
		with patch("ffl_woo_sync.woocommerce.webhook._read_raw_body", return_value=raw_body), \
			 patch("ffl_woo_sync.woocommerce.webhook._get_webhook_secret",
				   side_effect=RuntimeError("DB connection lost")), \
			 patch("frappe.get_request_header", return_value=""), \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			result = woo_event()

		self.assertFalse(result["ok"])
		self.assertIn("configuration error", result.get("error", ""))
		# Must NOT be 401 — WC must not count this as a failure
		self.assertNotEqual(frappe.local.response.get("http_status_code"), 401)

	def test_non_json_body_returns_200(self):
		"""Non-JSON POST body → 200 with error=non-JSON body (after valid HMAC)."""
		from ffl_woo_sync.woocommerce.webhook import woo_event
		raw_body = b"not json at all"
		sig = _make_sig(raw_body)
		with patch("ffl_woo_sync.woocommerce.webhook._read_raw_body", return_value=raw_body), \
			 patch("ffl_woo_sync.woocommerce.webhook._get_webhook_secret", return_value=_SECRET), \
			 patch("frappe.get_request_header", side_effect=lambda h: {
				 "X-WC-Webhook-Signature": sig,
				 "x-wc-webhook-signature": sig,
			 }.get(h, "")), \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			result = woo_event()

		self.assertFalse(result["ok"])
		self.assertIn("non-JSON", result.get("error", ""))

	def test_unhandled_topic_returns_200_handled_false(self):
		"""Unknown webhook topic → 200 with ok=True, handled=False."""
		from ffl_woo_sync.woocommerce.webhook import woo_event
		raw_body = json.dumps({"id": 1}).encode()
		sig = _make_sig(raw_body)
		with patch("ffl_woo_sync.woocommerce.webhook._read_raw_body", return_value=raw_body), \
			 patch("ffl_woo_sync.woocommerce.webhook._get_webhook_secret", return_value=_SECRET), \
			 patch("frappe.get_request_header", side_effect=lambda h: {
				 "X-WC-Webhook-Signature": sig,
				 "x-wc-webhook-signature": sig,
				 "X-WC-Webhook-Topic": "product.updated",
				 "x-wc-webhook-topic": "product.updated",
			 }.get(h, "")), \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			result = woo_event()

		self.assertTrue(result["ok"])
		self.assertFalse(result.get("handled"))
		self.assertEqual(result.get("topic"), "product.updated")

	def test_order_updated_idempotent_when_order_exists(self):
		"""order.updated for an already-processed order → no second doc created."""
		from ffl_woo_sync.woocommerce.webhook import woo_event
		raw_body = json.dumps({
			"id": 99, "status": "processing",
			"meta_data": _DEFAULT_FFL_META,
		}).encode()
		sig = _make_sig(raw_body)
		with patch("ffl_woo_sync.woocommerce.webhook._read_raw_body", return_value=raw_body), \
			 patch("ffl_woo_sync.woocommerce.webhook._get_webhook_secret", return_value=_SECRET), \
			 patch("frappe.get_request_header", side_effect=lambda h: {
				 "X-WC-Webhook-Signature": sig,
				 "x-wc-webhook-signature": sig,
				 "X-WC-Webhook-Topic": "order.updated",
				 "x-wc-webhook-topic": "order.updated",
			 }.get(h, "")), \
			 _patch_db_exists(return_value="WOO-ORD-00099"), \
			 patch("frappe.new_doc") as mock_new_doc, \
			 patch("frappe.db.commit"):
			result = woo_event()

		mock_new_doc.assert_not_called()
		self.assertTrue(result["ok"])


# ---------------------------------------------------------------------------
# REST fallback failure (WooCommerceError)
# ---------------------------------------------------------------------------

class TestFetchOrderFflFailure(FrappeTestCase):
	"""_fetch_order_ffl: WooCommerceError from REST API → log + empty FFL."""

	def test_woo_error_logs_error_and_returns_empty_ffl(self):
		"""WooCommerceError during REST fetch → _log_error, empty FFL dict returned."""
		from ffl_woo_sync.woocommerce.webhook import _fetch_order_ffl
		from ffl_woo_sync.woocommerce.client import WooCommerceError
		client_mock = MagicMock()
		client_mock.get_order.side_effect = WooCommerceError("HTTP 503")
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient") as MockCls, \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error") as mock_log:
			MockCls.from_settings.return_value = client_mock
			result = _fetch_order_ffl("42")

		log_titles = [c[1].get("title", "") for c in mock_log.call_args_list]
		self.assertTrue(any("FFL REST fallback failed" in t for t in log_titles))
		self.assertEqual(result["license"], "")
		self.assertEqual(result["_raw"], [])

	def test_woo_error_does_not_raise(self):
		"""WooCommerceError during REST fetch → swallowed, no exception propagated."""
		from ffl_woo_sync.woocommerce.webhook import _fetch_order_ffl
		from ffl_woo_sync.woocommerce.client import WooCommerceError
		client_mock = MagicMock()
		client_mock.get_order.side_effect = WooCommerceError("timeout")
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient") as MockCls, \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			MockCls.from_settings.return_value = client_mock
			# Should not raise
			result = _fetch_order_ffl("42")
		self.assertIsNotNone(result)


# ---------------------------------------------------------------------------
# I2: FFL expiration field extracted and stored
# ---------------------------------------------------------------------------

class TestFflExpirationField(FrappeTestCase):
	"""I2: _FFL_EXPIRATION_KEYS are extracted and stored on the order doc."""

	def test_expiration_key_extracted(self):
		"""ffl_expiration key → expiration field in returned dict."""
		meta = [{"key": "ffl_expiration", "value": "2027-10-01"}]
		ffl = _extract_ffl(meta)
		self.assertEqual(ffl["expiration"], "2027-10-01")

	def test_underscore_expiration_key_extracted(self):
		"""_ffl_expiration (underscore prefix) is also recognized."""
		meta = [{"key": "_ffl_expiration", "value": "2028-03-15"}]
		ffl = _extract_ffl(meta)
		self.assertEqual(ffl["expiration"], "2028-03-15")

	def test_expiration_captured_on_order_doc(self):
		"""FFL meta with expiration → ffl_expiration set on Woo Online Order."""
		meta = [
			{"key": "ffl_license_number", "value": "1-99-888-01-2D-09999"},
			{"key": "ffl_expiration", "value": "2027-10-01"},
		]
		body = _order_body(meta_data=meta)
		patcher_new, doc = _patch_new_doc()
		with _patch_db_exists(return_value=None), \
			 patcher_new, \
			 patch("frappe.db.get_value", return_value={}), \
			 patch("frappe.db.set_value"), \
			 patch("frappe.enqueue"):
			_handle_order_created(body)
		self.assertEqual(doc.ffl_expiration, "2027-10-01")

	def test_empty_expiration_when_key_absent(self):
		"""No expiration key in meta → expiration is empty string."""
		meta = [{"key": "ffl_license_number", "value": "1-57-..."}]
		ffl = _extract_ffl(meta)
		self.assertEqual(ffl["expiration"], "")


# ---------------------------------------------------------------------------
# Task 11: site routing — HMAC per-site secret + Woo Online Order.site stamp
# ---------------------------------------------------------------------------

_OTHER_SECRET = "another-store-secret-xyz"


class TestSiteRouting(FrappeTestCase):
	"""Task 11: webhook routing by site — retail is the only store accepted."""

	def tearDown(self):
		"""Prevent response status and woo_sync_site flag from leaking across tests."""
		super().tearDown()
		frappe.local.response["http_status_code"] = None
		frappe.flags.woo_sync_site = None

	# ---- _handle_order_created unit tests (flag-based site read) -----------

	def test_retail_site_stamps_retail_on_order(self):
		"""frappe.flags.woo_sync_site='retail' → _handle_order_created stamps doc.site='retail'."""
		frappe.flags.woo_sync_site = "retail"
		body = _order_body()
		patcher_new, doc = _patch_new_doc()
		with _patch_db_exists(return_value=None), \
			 patcher_new, \
			 patch("frappe.db.get_value", return_value={}), \
			 patch("frappe.db.set_value"), \
			 patch("frappe.enqueue"):
			_handle_order_created(body)
		self.assertEqual(doc.site, "retail")

	def test_default_site_is_retail(self):
		"""No flag set → _handle_order_created defaults doc.site to 'retail'."""
		# frappe.flags.woo_sync_site is None (cleared in tearDown from prior test)
		body = _order_body()
		patcher_new, doc = _patch_new_doc()
		with _patch_db_exists(return_value=None), \
			 patcher_new, \
			 patch("frappe.db.get_value", return_value={}), \
			 patch("frappe.db.set_value"), \
			 patch("frappe.enqueue"):
			_handle_order_created(body)
		self.assertEqual(doc.site, "retail")

	# ---- woo_event integration tests (site query param → secret selection) --

	def test_another_stores_secret_fails_on_retail(self):
		"""Body signed with some other secret, no site param → HMAC fails (retail secret used) → 401."""
		from ffl_woo_sync.woocommerce.webhook import woo_event
		raw_body = json.dumps({"id": 78, "status": "processing"}).encode()
		# Signed with another secret, but form_dict has no site param → retail used → mismatch
		sig = _make_sig(raw_body, secret=_OTHER_SECRET)

		# _get_webhook_secret always returns the RETAIL secret (no site param)
		with patch("ffl_woo_sync.woocommerce.webhook._read_raw_body", return_value=raw_body), \
			 patch("ffl_woo_sync.woocommerce.webhook._get_webhook_secret",
				   return_value=_SECRET), \
			 patch.object(frappe, "form_dict", frappe._dict({})), \
			 patch("frappe.get_request_header", side_effect=lambda h: {
				 "X-WC-Webhook-Signature": sig,
				 "x-wc-webhook-signature": sig,
			 }.get(h, "")):
			result = woo_event()

		self.assertEqual(frappe.local.response.get("http_status_code"), 401)
		self.assertFalse(result["ok"])

	def test_no_site_param_uses_retail_secret(self):
		"""woo_event with no site param → _get_webhook_secret called with site='retail'."""
		from ffl_woo_sync.woocommerce.webhook import woo_event
		raw_body = json.dumps({"id": 79}).encode()
		sig = _make_sig(raw_body, secret=_SECRET)

		secrets_called_with = []

		def _mock_get_secret(site="retail"):
			secrets_called_with.append(site)
			return _SECRET

		with patch("ffl_woo_sync.woocommerce.webhook._read_raw_body", return_value=raw_body), \
			 patch("ffl_woo_sync.woocommerce.webhook._get_webhook_secret",
				   side_effect=_mock_get_secret), \
			 patch.object(frappe, "form_dict", frappe._dict({})), \
			 patch("frappe.get_request_header", side_effect=lambda h: {
				 "X-WC-Webhook-Signature": sig,
				 "x-wc-webhook-signature": sig,
			 }.get(h, "")), \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			woo_event()

		self.assertEqual(secrets_called_with, ["retail"])

	def test_an_unknown_site_param_is_refused_with_400(self):
		"""woo_event with ?site=bogus → 400, before any secret is read; never
		processed as retail (that silent downgrade would be a double-sell)."""
		from ffl_woo_sync.woocommerce.webhook import woo_event
		raw_body = json.dumps({"id": 80}).encode()
		sig = _make_sig(raw_body, secret=_SECRET)  # retail secret

		secrets_called_with = []

		def _mock_get_secret(site="retail"):
			secrets_called_with.append(site)
			return _SECRET

		with patch("ffl_woo_sync.woocommerce.webhook._read_raw_body", return_value=raw_body), \
			 patch("ffl_woo_sync.woocommerce.webhook._get_webhook_secret",
				   side_effect=_mock_get_secret), \
			 patch.object(frappe.local, "request", MagicMock(args={"site": "bogus"}), create=True), \
			 patch("frappe.get_request_header", side_effect=lambda h: {
				 "X-WC-Webhook-Signature": sig,
				 "x-wc-webhook-signature": sig,
			 }.get(h, "")), \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			result = woo_event()

		self.assertEqual(frappe.local.response.get("http_status_code"), 400)
		self.assertFalse(result["ok"])
		self.assertEqual(secrets_called_with, [])


# ---------------------------------------------------------------------------
# E2E site routing — real _get_webhook_secret path (no mock on that function)
# ---------------------------------------------------------------------------

class TestSiteRoutingE2E(FrappeTestCase):
	"""E2E tests that exercise the real _get_webhook_secret code path.

	Unlike TestSiteRouting (which patches _get_webhook_secret), these tests
	let _get_webhook_secret run fully so they cover:
	  • client.STORES selects the correct Settings doctype per site
	  • get_password() is actually called on that doctype object
	  • woo_event sets frappe.flags.woo_sync_site before dispatching the handler
	  • _handle_order_created reads the flag and stamps order.site correctly

	frappe.get_single is mocked (provides a controlled settings object) so tests
	don't require a live database with configured secrets.  _get_webhook_secret
	itself is NOT mocked.
	"""

	def tearDown(self):
		super().tearDown()
		frappe.local.response["http_status_code"] = None
		frappe.flags.woo_sync_site = None

	@staticmethod
	def _settings_get_single(retail_secret=_SECRET):
		"""Return a frappe.get_single side-effect that serves controlled secrets."""
		def _side_effect(doctype):
			mock_s = MagicMock()
			if doctype == "WooCommerce Settings":
				mock_s.get_password.return_value = retail_secret or None
			else:
				mock_s.get_password.return_value = None
			return mock_s
		return _side_effect

	def test_retail_e2e_order_site_stamped_retail(self):
		"""E2E: no ?site= param + retail-signed body → order.site == 'retail'.

		Proves the default (no site param) routes to WooCommerce Settings and
		stamps 'retail'.
		"""
		from ffl_woo_sync.woocommerce.webhook import woo_event
		raw_body = json.dumps(_order_body(woo_id=201)).encode()
		sig = _make_sig(raw_body, secret=_SECRET)  # retail secret
		patcher_new, doc = _patch_new_doc()

		with patcher_new, \
			 _patch_db_exists(return_value=None), \
			 patch("ffl_woo_sync.woocommerce.webhook._read_raw_body", return_value=raw_body), \
			 patch("frappe.get_single",
				   side_effect=self._settings_get_single(retail_secret=_SECRET)), \
			 patch.object(frappe, "form_dict", frappe._dict({})), \
			 patch("frappe.get_request_header", side_effect=lambda h: {
				 "X-WC-Webhook-Signature": sig,
				 "x-wc-webhook-signature": sig,
				 "X-WC-Webhook-Topic": "order.created",
				 "x-wc-webhook-topic": "order.created",
			 }.get(h, "")), \
			 patch("frappe.db.get_value", return_value={}), \
			 patch("frappe.db.set_value"), \
			 patch("frappe.enqueue"), \
			 patch("frappe.db.commit"):
			result = woo_event()

		self.assertNotEqual(frappe.local.response.get("http_status_code"), 401,
			"HMAC must pass when body is signed with the retail secret")
		self.assertEqual(result.get("ok"), True)
		self.assertEqual(doc.site, "retail",
			"order.site must be stamped 'retail' when no ?site= param is given")

# ---------------------------------------------------------------------------
# Finding 1: _fetch_order_ffl uses the originating site's WC client
# ---------------------------------------------------------------------------

class TestFetchOrderFflSite(FrappeTestCase):
	"""_fetch_order_ffl reads frappe.flags.woo_sync_site to select the store
	client; a retail order (or no flag) queries the RETAIL store."""

	def tearDown(self):
		super().tearDown()
		frappe.flags.woo_sync_site = None

	def test_retail_flag_selects_retail_client(self):
		"""RETAIL order: _fetch_order_ffl calls WooCommerceClient.from_settings('retail')."""
		from ffl_woo_sync.woocommerce.webhook import _fetch_order_ffl
		client_mock = MagicMock()
		client_mock.get_order.return_value = {"meta_data": []}
		frappe.flags.woo_sync_site = "retail"
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient") as MockCls:
			MockCls.from_settings.return_value = client_mock
			_fetch_order_ffl("78")
		MockCls.from_settings.assert_called_once_with("retail")

	def test_no_flag_defaults_to_retail(self):
		"""No woo_sync_site flag → _fetch_order_ffl defaults to 'retail' (safe fallback)."""
		from ffl_woo_sync.woocommerce.webhook import _fetch_order_ffl
		client_mock = MagicMock()
		client_mock.get_order.return_value = {"meta_data": []}
		# Flag is None (cleared in tearDown from previous test)
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient") as MockCls:
			MockCls.from_settings.return_value = client_mock
			_fetch_order_ffl("79")
		MockCls.from_settings.assert_called_once_with("retail")


class TestOriginSite(FrappeTestCase):
	"""WooCommerce delivers the order as a JSON body and puts the store in the
	``?site=`` query string. Frappe fills ``form_dict`` from the body and DROPS
	query args, so ``_origin_site`` reads ``frappe.request.args`` and nothing else.
	It returns the store (blank = retail) or None for a store this POS does not
	run — woo_event refuses that rather than processing it as retail.
	"""

	def setUp(self):
		self._saved_form_dict = frappe.local.form_dict
		self._saved_request = getattr(frappe.local, "request", None)

	def tearDown(self):
		frappe.local.form_dict = self._saved_form_dict
		frappe.local.request = self._saved_request

	def _simulate_http(self, query, body):
		# form_dict is parsed from the JSON body (query args are NOT merged in);
		# request.args carries the raw query string.
		frappe.local.form_dict = frappe._dict(body)
		req = MagicMock()
		req.args = query  # plain dict supports .get()
		frappe.local.request = req

	def test_site_resolved_from_query_with_json_body(self):
		# Order JSON in the body (no "site"), ?site= in the query string.
		self._simulate_http(query={"site": " Retail "}, body={"id": 123, "status": "processing"})
		self.assertEqual(_origin_site(), "retail")

	def test_retail_default_when_no_site_anywhere(self):
		self._simulate_http(query={}, body={"id": 123, "status": "processing"})
		self.assertEqual(_origin_site(), "retail")

	def test_an_unknown_site_is_none_not_folded_to_retail(self):
		self._simulate_http(query={"site": "Bogus "}, body={"id": 1})
		self.assertIsNone(_origin_site())

	def test_a_blank_site_param_is_the_retail_store(self):
		"""``?site=`` / ``?site=%20`` name no store: retail, not an unknown store ''."""
		for blank in ("", "   "):
			with self.subTest(param=repr(blank)):
				self._simulate_http(query={"site": blank}, body={"id": 1})
				self.assertEqual(_origin_site(), "retail")

	def test_a_site_in_the_body_or_form_is_never_read(self):
		self._simulate_http(query={}, body={"id": 1, "site": "dealer"})
		self.assertEqual(_origin_site(), "retail")

	def test_no_request_means_no_query_string_means_retail(self):
		frappe.local.form_dict = frappe._dict({"site": "dealer"})
		frappe.local.request = None
		self.assertEqual(_origin_site(), "retail")


class AutoClearNonFirearmOnCompleted(FrappeTestCase):
	"""Non-firearm web orders (no firearm serial rows) have no FFL disposition, so
	dispose_web_order refuses them and they'd sit in Pending Fulfillment forever.
	Once WooCommerce marks them completed, the webhook auto-clears them
	(-> Transferred). Firearm orders (serial rows) must still go through Dispose &
	Ship and are never auto-cleared."""

	def _order(self, **kw):
		base = {"order_status": "Pending Fulfillment", "woo_status": "completed", "serials": []}
		base.update(kw)
		return frappe._dict(base)

	def test_clears_non_firearm_completed_pending_order(self):
		from ffl_woo_sync.woocommerce.webhook import _auto_clear_non_firearm_if_completed
		o = self._order()
		self.assertTrue(_auto_clear_non_firearm_if_completed(o))
		self.assertEqual(o.order_status, "Transferred")

	def test_skips_firearm_order_with_serial_rows(self):
		from ffl_woo_sync.woocommerce.webhook import _auto_clear_non_firearm_if_completed
		o = self._order(serials=[frappe._dict(item_code="GLOCK-19")])
		self.assertFalse(_auto_clear_non_firearm_if_completed(o))
		self.assertEqual(o.order_status, "Pending Fulfillment")  # firearm -> Dispose only

	def test_skips_when_not_completed(self):
		from ffl_woo_sync.woocommerce.webhook import _auto_clear_non_firearm_if_completed
		o = self._order(woo_status="processing")
		self.assertFalse(_auto_clear_non_firearm_if_completed(o))
		self.assertEqual(o.order_status, "Pending Fulfillment")

	def test_skips_when_already_cleared(self):
		from ffl_woo_sync.woocommerce.webhook import _auto_clear_non_firearm_if_completed
		o = self._order(order_status="Transferred")
		self.assertFalse(_auto_clear_non_firearm_if_completed(o))


class AutoClearOnFirstWebhook(FrappeTestCase):
	"""An order whose FIRST webhook is already 'completed' (the original was lost
	and staff re-fired it from WooCommerce, as order_audit tells them to) gets no
	later order.updated — the create path itself must clear it, or an
	accessory-only order never books revenue nor issues its stock."""

	def setUp(self):
		super().setUp()
		warm_channel_meta()

	def _create(self, *, firearm_row: bool):
		body = _order_body(line_items=[])
		body["status"] = "completed"
		patcher_new, doc = _patch_new_doc()

		def _lines(order, items):
			if firearm_row:
				order.serials.append(frappe._dict(item_code="GLOCK-19", woo_line_id="1"))
			return []

		with _patch_db_exists(return_value=None), \
			 patcher_new, \
			 patch("ffl_woo_sync.woocommerce.webhook._process_lines", side_effect=_lines), \
			 patch("ffl_woo_sync.woocommerce.webhook.is_antique_item", return_value=False), \
			 patch("ffl_woo_sync.woocommerce.webhook._book_revenue_after_auto_clear") as book, \
			 patch("frappe.db.get_value", return_value={}), \
			 patch("frappe.db.set_value"), \
			 patch("frappe.enqueue"), \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			_handle_order_created(body)
		return doc, book

	def test_an_accessory_only_completed_order_is_cleared_and_booked(self):
		doc, book = self._create(firearm_row=False)
		self.assertEqual(doc.order_status, "Transferred")
		doc.insert.assert_called_once()
		book.assert_called_once_with(doc)

	def test_a_firearm_order_still_waits_for_dispose(self):
		doc, book = self._create(firearm_row=True)
		self.assertEqual(doc.order_status, "Pending Fulfillment")
		book.assert_not_called()
