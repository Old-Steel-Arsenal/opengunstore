"""FastBound importer integration test.

Mocks the FastBoundClient with a canned page of items and verifies the full
upsert chain: Item created, FFL Acquisition submitted with fastbound_id,
Stock Entry receipt posted, idempotency on re-run.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.fastbound import importer


def _sample_items(suffix: str) -> list[dict]:
	"""Each test method asks for a unique suffix so concurrent or repeated
	runs don't see leftover data from prior runs (Stock Entry / Stock Ledger
	commits escape FrappeTestCase rollback)."""
	return [
		{
			"id": f"fb-A-{suffix}",
			"serial": f"SW19-A-{suffix}",
			"manufacturer": "Smith & Wesson",
			"model": "Model 19",
			"caliber": ".357 Magnum",
			"type": "Pistol",
			"acquireType": "Individual",
			"acquireDate": "2026-04-01T10:00:00Z",
			"acquisitionCost": 350.00,
			"acquiredFrom": {"name": "Jane Doe"},
		},
		{
			"id": f"fb-B-{suffix}",
			"serial": f"GLK17-B-{suffix}",
			"manufacturer": "Glock",
			"model": "G17",
			"caliber": "9mm Luger",
			"type": "Pistol",
			"acquireType": "FFL Dealer",
			"acquireDate": "2026-04-15T12:00:00Z",
			"acquisitionCost": 480.00,
			"acquiredFrom": {"name": "Acme Distributors", "ffl": "1-12-345-67-8K-99999"},
		},
	]


def _serial_is_in_the_ledger(serial: str) -> bool:
	"""Does stock history still reference this serial?

	A serial the ledger knows must be left alone: deleting it would leave the
	Stock Ledger Entry and the Bin pointing at nothing, and the next run on this
	site pays for that with NegativeStock.

	BOTH storage models count. v16 reaches a serial through the bundle, but this
	site runs ``Stock Settings.use_serial_batch_fields = 1``, so plenty of entries
	write the legacy ``Stock Ledger Entry.serial_no`` string instead — asking only
	the bundle join calls those serials unknown to the ledger and deletes them,
	which is precisely the orphan-SLE-then-NegativeStock outcome this guard is
	here to prevent. Hence the LEFT join plus the legacy column.

	The legacy column holds one serial per line, so it is compared element-wise
	(``FIND_IN_SET`` over the newlines) rather than with a ``LIKE`` that would also
	answer yes for any serial that merely contains this one.

	A bundle ENTRY on its own is not stock: an abandoned draft bundle carries
	entries and moves nothing, and earlier runs of this suite left dozens of
	those behind.
	"""
	return bool(frappe.db.sql(
		"""SELECT sle.name
		FROM `tabStock Ledger Entry` sle
		LEFT JOIN `tabSerial and Batch Entry` sbe
			ON sbe.parent = sle.serial_and_batch_bundle
		WHERE sbe.serial_no = %(s)s
			OR FIND_IN_SET(%(s)s, REPLACE(COALESCE(sle.serial_no, ''), '\n', ','))
		LIMIT 1""", {"s": serial}))


def _drop_leftovers_from_earlier_runs(suffix: str) -> None:
	"""Delete this test method's own committed leftovers.

	The ids and serials here are METHOD-scoped but STABLE across runs, and a
	commit anywhere in the suite moves FrappeTestCase's rollback floor forward
	(see ffl_core.utils.commit) — so a submitted FFL Acquisition from an earlier
	run survives and `_upsert_one` takes its "already imported" branch on the
	next one. `created` then reads 0 where the test says 2, which is how
	test_model_mode_folds_caliber_spelling became a standing red that every QA
	round had to explain by hand.

	Deleted as a PAIR (acquisition + serial) or not at all: dropping the
	acquisition alone would send the import down the create path and straight
	into a duplicate Serial No. Raw deletes on purpose — these are orphans to
	clear, not documents to cancel, and the on_trash guards over an A&D record
	exist to stop exactly that being done to a live one. Not committed either:
	the rows are wanted gone for the duration of this test, and leaving the
	delete inside the transaction keeps the rollback floor where it is.

	An EMPTY suffix is refused outright: ``like "%"`` matches every row on the
	site, and these are raw deletes chosen specifically to go around the A&D
	``on_trash`` guards — the guards whose whole job is to stop an acquisition
	being erased. A test-method name is never empty today; that is one refactor
	away from being the difference between a fixture and a wiped bound book.
	"""
	if not suffix:
		return
	for acq in frappe.get_all(
			"FFL Acquisition",
			filters={"fastbound_id": ("like", f"%{suffix}")},
			fields=["name", "serial"]):
		if acq.serial and _serial_is_in_the_ledger(acq.serial):
			continue
		frappe.db.delete("FFL Acquisition", {"name": acq.name})
		if acq.serial:
			frappe.db.delete("Serial No", {"name": acq.serial})


class TheFixtureCleanupItself(FrappeTestCase):
	"""`_drop_leftovers_from_earlier_runs` raw-deletes FFL Acquisition and Serial
	No rows, deliberately around the A&D ``on_trash`` guards. The blast radius of
	a query like that is the whole bound book, so the two ways it can go wrong get
	a test each — nobody reads a setUp helper again after the day it was written.
	"""

	def test_an_empty_suffix_deletes_nothing(self):
		"""``like "%"`` matches every row on the site. ``_testMethodName`` is
		non-empty today; this guard is what keeps one refactor from turning a
		fixture reset into an erased bound book."""
		with patch("frappe.get_all") as get_all, patch("frappe.db.delete") as delete:
			_drop_leftovers_from_earlier_runs("")
		get_all.assert_not_called()
		delete.assert_not_called()

	def test_a_serial_the_ledger_still_references_is_not_deleted(self):
		"""Deleting one leaves the Stock Ledger Entry and the Bin pointing at
		nothing, and the next run on this site pays for it with NegativeStock."""
		with patch(f"{__name__}._serial_is_in_the_ledger", return_value=True), \
			 patch("frappe.get_all", return_value=[
				 frappe._dict({"name": "ACQ-FIXTURE-1", "serial": "SN-FIXTURE-1"})]), \
			 patch("frappe.db.delete") as delete:
			_drop_leftovers_from_earlier_runs("a-suffix")
		delete.assert_not_called()

	def test_the_LEGACY_ledger_column_counts_as_a_reference(self):
		"""The reason that skip fires at all on this site.

		``Stock Settings.use_serial_batch_fields = 1`` here, so a serial's only
		ledger reference is routinely the legacy ``Stock Ledger Entry.serial_no``
		string with no bundle anywhere. Asking the bundle join alone answered "not
		in the ledger" for those and deleted them.

		Also pins the comparison as element-wise: the column holds one serial per
		line, and a ``LIKE`` would answer yes for any serial that merely CONTAINS
		this one — which reads as extra caution and is really a different bug,
		skipping deletes it should make.
		"""
		serial = "LEDGER-LEGACY-ONLY-1"
		sle = frappe.get_doc({
			"doctype": "Stock Ledger Entry",
			"item_code": "_Test Fixture Guard Item",
			"warehouse": "_Test Fixture Guard WH",
			"posting_date": frappe.utils.nowdate(),
			"actual_qty": 1,
			"voucher_type": "Stock Entry",
			"voucher_no": "SE-FIXTURE-GUARD",
			"serial_no": f"{serial}\nLEDGER-LEGACY-ONLY-2",
			"is_cancelled": 0,
		})
		sle.name = "SLE-FIXTURE-GUARD-1"
		# db_insert on purpose: a real SLE wants a real voucher and real stock, and
		# nothing here reads either — the helper under test looks at two columns.
		sle.db_insert()
		try:
			self.assertTrue(_serial_is_in_the_ledger(serial))
			self.assertTrue(_serial_is_in_the_ledger("LEDGER-LEGACY-ONLY-2"))
			self.assertFalse(_serial_is_in_the_ledger("EDGER-LEGACY-ONLY-1"))
			self.assertFalse(_serial_is_in_the_ledger("LEDGER-LEGACY-ONLY"))
		finally:
			frappe.db.delete("Stock Ledger Entry", {"name": sle.name})


class FastBoundImporter(FrappeTestCase):
	def setUp(self):
		_drop_leftovers_from_earlier_runs(self._testMethodName)

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		# Reuse the dev-site bootstrap helpers for masters.
		cls._ensure_masters()

	@staticmethod
	def _ensure_masters():
		from ffl_core.setup import dev_site
		dev_site.run()
		# Ensure Used Firearm group exists
		if not frappe.db.exists("Item Group", "Used Firearm"):
			frappe.get_doc({
				"doctype": "Item Group",
				"item_group_name": "Used Firearm",
				"parent_item_group": "All Item Groups",
				"is_group": 0,
			}).insert(ignore_permissions=True)
		# Configure FastBound Settings with placeholder creds so the import path
		# is happy when our mock kicks in (FastBoundClient is mocked, so creds
		# never actually leave the box).
		s = frappe.get_single("FastBound Settings")
		if not s.account_num:
			s.account_num = "TEST-ACCOUNT"
			s.default_audit_user = "test@gunshop.local"
			s.flags.ignore_permissions = True
			s.save(ignore_permissions=True)
		# Set User Default Company so importer doesn't have to guess.
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", company)
	# -------------------------------------------------- mocked client

	class _FakeClient:
		def __init__(self, pages):
			self.pages = pages

		def iter_items(self, page_size: int = 100, **_):
			yield from self.pages

	# -------------------------------------------------- tests

	def _patch_client(self, pages):
		fake = self._FakeClient(pages)
		return patch.object(importer.FastBoundClient, "from_settings", return_value=fake)

	def test_import_creates_items_acquisitions_and_stock(self):
		with self._patch_client([_sample_items(self._testMethodName)]):
			result = importer.import_bound_book(commit_each=0)
		self.assertEqual(result["fetched"], 2)
		self.assertEqual(result["created"], 2)
		self.assertEqual(result["skipped"], 0)
		self.assertEqual(result["error_count"], 0)

		for fb in _sample_items(self._testMethodName):
			item_code = importer._item_code_for(fb)
			self.assertTrue(frappe.db.exists("Item", item_code), f"Item {item_code} not created")

			acq_name = frappe.db.get_value("FFL Acquisition", {"fastbound_id": fb["id"]}, "name")
			self.assertTrue(acq_name)
			acq = frappe.get_doc("FFL Acquisition", acq_name)
			self.assertEqual(acq.docstatus, 1)
			self.assertEqual(acq.serial, fb["serial"])
			self.assertEqual(acq.item, item_code)

	def test_import_backfills_fastbound_item_id_on_serial(self):
		# inventory_sync reconciles in-stock guns against the bound book via
		# Serial No.fastbound_item_id — imported guns must carry it.
		with self._patch_client([_sample_items(self._testMethodName)]):
			importer.import_bound_book(commit_each=0)
		for fb in _sample_items(self._testMethodName):
			serial = fb["serial"]
			self.assertTrue(frappe.db.exists("Serial No", serial), f"Serial {serial} missing")
			self.assertEqual(
				frappe.db.get_value("Serial No", serial, "fastbound_item_id"), fb["id"],
				"imported gun's Serial No must carry the FastBound item id",
			)

	def test_aggregate_by_model_ignores_sku(self):
		# When aggregate_by="model", SKU/UPC/MPN (and manufacturer) are ignored —
		# two FB items with different SKUs but the same model+caliber roll into the
		# same Item, coded by the model+caliber slug alone.
		sfx = self._testMethodName
		gun_a = {
			"id": f"fb-mode-a-{sfx}",
			"serial": f"GA-{sfx}", "manufacturer": "Glock", "model": "19",
			"caliber": "9MM", "type": "Pistol", "sku": "SKU-FROM-DISTRIBUTOR-A",
			"acquireType": "Individual", "acquireDate": "2026-04-01T10:00:00Z",
			"acquisitionCost": 500.0,
		}
		gun_b = {**gun_a, "id": f"fb-mode-b-{sfx}", "serial": f"GB-{sfx}",
			"sku": "SKU-FROM-DISTRIBUTOR-B"}

		with self._patch_client([[gun_a, gun_b]]):
			result = importer.import_bound_book(commit_each=0, aggregate_by="model")
		self.assertEqual(result["created"], 2)

		expected_code = importer._item_code_for(gun_a, aggregate_by="model")
		self.assertEqual(expected_code, "19-9MM")  # model+caliber, manufacturer dropped
		# Both guns land on the same Item.
		for gun in (gun_a, gun_b):
			acq = frappe.db.get_value("FFL Acquisition",
				{"fastbound_id": gun["id"]}, "item")
			self.assertEqual(acq, expected_code)

	def test_model_mode_folds_makers_and_keeps_per_serial_maker(self):
		# The "same model, different maker/importer" case: two M1 Garands from
		# different manufacturers + importers must land on ONE maker-agnostic Item,
		# with each gun's own manufacturer + importer kept on its Serial No.
		sfx = self._testMethodName
		spr = {
			"id": f"fb-spr-{sfx}", "serial": f"M1-SPR-{sfx}",
			"manufacturer": "Springfield Armory", "importer": "",
			"model": "M1", "caliber": ".30", "type": "Rifle",
			"acquireType": "Individual", "acquireDate": "2026-04-01T10:00:00Z",
			"acquisitionCost": 1200.0,
		}
		win = {**spr, "id": f"fb-win-{sfx}", "serial": f"M1-WIN-{sfx}",
			"manufacturer": "Winchester", "importer": "Acme Importers Inc"}

		with self._patch_client([[spr, win]]):
			result = importer.import_bound_book(commit_each=0, aggregate_by="model")
		self.assertEqual(result["created"], 2)

		# model+caliber slug (.30 normalises to 30), manufacturer dropped → one Item.
		code = importer._item_code_for(spr, aggregate_by="model")
		self.assertEqual(code, "M1-30")
		for gun in (spr, win):
			self.assertEqual(
				frappe.db.get_value("FFL Acquisition", {"fastbound_id": gun["id"]}, "item"), code)
		# The Item itself is maker-agnostic …
		self.assertFalse(frappe.db.get_value("Item", code, "manufacturer"))
		# … and each Serial No keeps its own manufacturer + importer.
		self.assertEqual(
			frappe.db.get_value("Serial No", spr["serial"], "manufacturer"), "Springfield Armory")
		self.assertEqual(
			frappe.db.get_value("Serial No", win["serial"], "manufacturer"), "Winchester")
		self.assertEqual(
			frappe.db.get_value("Serial No", win["serial"], "importer"), "Acme Importers Inc")

	def test_reimport_resyncs_not_duplicates(self):
		# Re-running never duplicates; existing guns are re-synced (updated), not skipped.
		with self._patch_client([_sample_items(self._testMethodName)]):
			first = importer.import_bound_book(commit_each=0)
		with self._patch_client([_sample_items(self._testMethodName)]):
			second = importer.import_bound_book(commit_each=0)
		self.assertEqual(first["created"], 2)
		self.assertEqual(second["created"], 0)
		self.assertEqual(second["updated"], 2)

	def test_reimport_resyncs_corrected_maker_and_importer(self):
		# FastBound is authoritative: a corrected maker/importer upstream must reach
		# POS on the next sync — the gun's Serial No is updated, not skipped.
		sfx = self._testMethodName
		gun = {
			"id": f"fb-rs-{sfx}", "serial": f"RS-{sfx}", "manufacturer": "Colt",
			"importer": "Old Importer", "model": "1911", "caliber": "45ACP",
			"type": "Pistol", "acquireType": "Individual",
			"acquireDate": "2026-04-01T10:00:00Z", "acquisitionCost": 700.0,
		}
		with self._patch_client([[gun]]):
			importer.import_bound_book(commit_each=0, aggregate_by="model")
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "manufacturer"), "Colt")
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "importer"), "Old Importer")
		# FastBound corrects both → re-sync must propagate.
		corrected = {**gun, "manufacturer": "Colt's Manufacturing", "importer": "New Importer"}
		with self._patch_client([[corrected]]):
			result = importer.import_bound_book(commit_each=0, aggregate_by="model")
		self.assertEqual(result["created"], 0)
		self.assertEqual(result["updated"], 1)
		self.assertEqual(
			frappe.db.get_value("Serial No", gun["serial"], "manufacturer"), "Colt's Manufacturing")
		self.assertEqual(
			frappe.db.get_value("Serial No", gun["serial"], "importer"), "New Importer")

	def test_import_stamps_model_caliber_on_serial(self):
		# model + caliber are per-unit facts stamped onto the Serial No (the Item is
		# just the bucket), so the sync can keep them aligned without moving the gun.
		sfx = self._testMethodName
		gun = {
			"id": f"fb-mc-{sfx}", "serial": f"MC-{sfx}", "manufacturer": "Colt",
			"model": "1911", "caliber": "45ACP", "type": "Pistol",
			"acquireType": "Individual", "acquireDate": "2026-04-01T10:00:00Z",
			"acquisitionCost": 700.0,
		}
		with self._patch_client([[gun]]):
			importer.import_bound_book(commit_each=0, aggregate_by="model")
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "model_name"), "1911")
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "caliber"), "45ACP")

	def test_reimport_model_change_realigns_serial_not_item(self):
		# A corrected model is an ATTRIBUTE change: the sync aligns the Serial No's
		# model to FastBound IN PLACE — it does NOT move the gun between Items (no
		# reverse+recreate), so per-unit data (e.g. a tagged price) survives. The
		# mis-bucket is only reported for a human to reassign.
		sfx = self._testMethodName
		gun = {
			"id": f"fb-mv-{sfx}", "serial": f"MV-{sfx}", "manufacturer": "Glock",
			"model": "19", "caliber": "9MM", "type": "Pistol",
			"acquireType": "Individual", "acquireDate": "2026-04-01T10:00:00Z",
			"acquisitionCost": 500.0,
		}
		with self._patch_client([[gun]]):
			importer.import_bound_book(commit_each=0, aggregate_by="model")
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "item_code"), "19-9MM")
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "model_name"), "19")
		# A per-unit value an operator set — must survive the sync (no record churn).
		frappe.db.set_value("Serial No", gun["serial"], "sell_price", 599, update_modified=False)

		# FastBound corrects the model 19 → 17.
		with self._patch_client([[{**gun, "model": "17"}]]):
			result = importer.import_bound_book(commit_each=0, aggregate_by="model")
		self.assertEqual(result["updated"], 1)
		# Serial's model aligned to FB; gun NOT moved; price preserved.
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "model_name"), "17")
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "item_code"), "19-9MM")
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "status"), "Active")
		self.assertEqual(float(frappe.db.get_value("Serial No", gun["serial"], "sell_price")), 599.0)
		# Mis-bucket reported, never auto-moved.
		self.assertFalse(any(c.get("change") == "moved" for c in result["corrected"]))
		self.assertTrue(any(r["to"] == "17-9MM" for r in result["regroup_needed"]))

	def test_reassign_serial_preserves_all_per_unit_data(self):
		# Reassign moves the gun between Items via a two-leg stock move WITHOUT deleting
		# the Serial No or the A&D record — so price, condition, service flag,
		# maker/importer, the acquisition's identity and its fastbound_id all survive.
		# (The old reverse+recreate silently dropped price / photos / condition.)
		sfx = self._testMethodName
		gun = {
			"id": f"fb-ra-{sfx}", "serial": f"RA-{sfx}", "manufacturer": "Colt",
			"importer": "Imp Co", "model": "19", "caliber": "9MM", "type": "Pistol",
			"acquireType": "Individual", "acquireDate": "2026-04-01T10:00:00Z",
			"acquisitionCost": 500.0,
		}
		with self._patch_client([[gun]]):
			importer.import_bound_book(commit_each=0, aggregate_by="model")
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "item_code"), "19-9MM")
		# Per-unit data an operator added that MUST survive a reassign.
		frappe.db.set_value("Serial No", gun["serial"],
			{"sell_price": 649, "condition_grade": "Excellent", "needs_service": 1},
			update_modified=False)
		acq_before = frappe.db.get_value(
			"FFL Acquisition", {"fastbound_id": gun["id"], "docstatus": 1}, "name")
		fbid_before = frappe.db.get_value("Serial No", gun["serial"], "fastbound_item_id")

		target = f"RA-TGT-{sfx}"
		if not frappe.db.exists("Item", target):
			frappe.get_doc({
				"doctype": "Item", "item_code": target, "item_name": target,
				"item_group": "Used Firearm", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
			}).insert(ignore_permissions=True)

		result = importer.reassign_serial_to_item(gun["serial"], target)
		self.assertTrue(result["ok"])
		self.assertEqual((result["from"], result["to"]), ("19-9MM", target))
		# Stock moved; serial still in stock under the target.
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "item_code"), target)
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "status"), "Active")
		# Per-unit data preserved — the whole point of the safe move.
		self.assertEqual(float(frappe.db.get_value("Serial No", gun["serial"], "sell_price")), 649.0)
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "condition_grade"), "Excellent")
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "needs_service"), 1)
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "manufacturer"), "Colt")
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "importer"), "Imp Co")
		# SAME A&D record (not a new one), re-pointed to the target, fastbound_id intact.
		self.assertEqual(
			frappe.db.get_value("FFL Acquisition", {"fastbound_id": gun["id"], "docstatus": 1}, "name"),
			acq_before)
		self.assertEqual(frappe.db.get_value("FFL Acquisition", acq_before, "item"), target)
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "fastbound_item_id"), fbid_before)

	def test_reassign_to_antique_item_skips_off_book_acquisitions(self):
		"""A serial whose only A&D record is marked [NOT A&D] (an antique booked by
		mistake, its FastBound record deleted) can move to an antique Item. A normal
		submitted acquisition, or a FastBound id on the serial, still blocks it."""
		from ffl_core.firearm import NOT_IN_BOOK_PREFIX
		sfx = self._testMethodName
		gun = {
			"id": f"fb-ob-{sfx}", "serial": f"OB-{sfx}", "manufacturer": "Loewe",
			"model": "Gew88", "caliber": "8MM", "type": "Rifle",
			"acquireType": "Individual", "acquireDate": "2026-04-01T10:00:00Z",
			"acquisitionCost": 300.0,
		}
		with self._patch_client([[gun]]):
			importer.import_bound_book(commit_each=0, aggregate_by="model")
		target = f"OB-ANT-{sfx}"
		if not frappe.db.exists("Item", target):
			frappe.get_doc({
				"doctype": "Item", "item_code": target, "item_name": target,
				"item_group": "Used Firearm", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1, "is_antique": 1,
			}).insert(ignore_permissions=True)
		acq = frappe.db.get_value("FFL Acquisition", {"fastbound_id": gun["id"], "docstatus": 1}, "name")

		# FastBound id on the serial: on the books, blocked.
		with self.assertRaisesRegex(frappe.ValidationError, "bound book"):
			importer.reassign_serial_to_item(gun["serial"], target)
		# FastBound record deleted, acquisition still live: blocked.
		frappe.db.set_value("Serial No", gun["serial"], "fastbound_item_id", "", update_modified=False)
		with self.assertRaisesRegex(frappe.ValidationError, "bound book"):
			importer.reassign_serial_to_item(gun["serial"], target)
		# Acquisition marked off the bound book: allowed.
		frappe.db.set_value("FFL Acquisition", acq, "fastbound_sync_error",
			NOT_IN_BOOK_PREFIX + "antique booked by mistake", update_modified=False)
		result = importer.reassign_serial_to_item(gun["serial"], target)
		self.assertEqual(result["to"], target)
		self.assertEqual(frappe.db.get_value("Serial No", gun["serial"], "item_code"), target)

	def test_reassign_receives_the_gun_at_the_value_it_left_at(self):
		"""A cost typed on the Serial No form after the receipt leaves purchase_rate off the
		ledger. Reassign must carry the gun across at its ledger value, so the two legs net to
		zero, and keep the serial's own purchase_rate (229580, 2026-10-08: out at 1512, in at
		1050.63, $461.37 into Stock Adjustment)."""
		sfx = self._testMethodName
		gun = {
			"id": f"fb-rv-{sfx}", "serial": f"RV-{sfx}", "manufacturer": "Colt",
			"model": "19", "caliber": "9MM", "type": "Pistol",
			"acquireType": "Individual", "acquireDate": "2026-04-01T10:00:00Z",
			"acquisitionCost": 500.0,
		}
		with self._patch_client([[gun]]):
			importer.import_bound_book(commit_each=0, aggregate_by="model")
		frappe.db.set_value("Serial No", gun["serial"], "purchase_rate", 300, update_modified=False)
		target = f"RV-TGT-{sfx}"
		if not frappe.db.exists("Item", target):
			frappe.get_doc({
				"doctype": "Item", "item_code": target, "item_name": target,
				"item_group": "Used Firearm", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
			}).insert(ignore_permissions=True)
		already = set(frappe.get_all("Stock Entry", pluck="name"))

		importer.reassign_serial_to_item(gun["serial"], target)

		legs = [n for n in frappe.get_all("Stock Entry", pluck="name") if n not in already]
		self.assertEqual(len(legs), 2)
		moved = frappe.get_all("Stock Ledger Entry", filters={"voucher_type": "Stock Entry",
			"voucher_no": ("in", legs), "is_cancelled": 0}, fields=["actual_qty", "stock_value_difference"])
		self.assertEqual(sorted(r.actual_qty for r in moved), [-1, 1])
		self.assertAlmostEqual(sum(r.stock_value_difference for r in moved), 0, places=2)
		self.assertEqual(float(frappe.db.get_value("Serial No", gun["serial"], "purchase_rate")), 300.0)

	def _reassign_with_enqueue_spy(self, *, serial_fields):
		"""Import one gun, stamp `serial_fields` on its Serial No, reassign it to a
		fresh Item, and return every `frappe.enqueue` dotted path fired on the way."""
		sfx = self._testMethodName
		gun = {
			"id": f"fb-dl-{sfx}", "serial": f"DL-{sfx}", "manufacturer": "Colt",
			"model": "19", "caliber": "9MM", "type": "Pistol",
			"acquireType": "Individual", "acquireDate": "2026-04-01T10:00:00Z",
			"acquisitionCost": 500.0,
		}
		with self._patch_client([[gun]]):
			importer.import_bound_book(commit_each=0, aggregate_by="model")
		if serial_fields:
			frappe.db.set_value("Serial No", gun["serial"], serial_fields,
				update_modified=False)
		target = f"DL-TGT-{sfx}"
		if not frappe.db.exists("Item", target):
			frappe.get_doc({
				"doctype": "Item", "item_code": target, "item_name": target,
				"item_group": "Used Firearm", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
			}).insert(ignore_permissions=True)
		with patch("frappe.enqueue") as enqueued:
			importer.reassign_serial_to_item(gun["serial"], target)
		# Submitting the two Stock Entries fires other pushes through doc_events,
		# so filter by dotted path rather than asserting a call count.
		return [
			c for c in enqueued.call_args_list
			if c.args and c.args[0] == "ffl_woo_sync.woocommerce.tasks.delist_serial_task"
		], gun["serial"]

	def test_reassign_delists_the_guns_own_woo_listing(self):
		"""A per-gun WooCommerce product belongs to the gun's OLD Item. Left live,
		the rifle is sellable twice: on its own product page, and through the
		target Item's listing, which now counts it in stock."""
		calls, serial = self._reassign_with_enqueue_spy(
			serial_fields={"woo_product_id": "4846"})
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0].kwargs["serial_no"], serial)
		self.assertEqual(calls[0].kwargs["site"], "retail")

	def test_reassign_runs_with_the_dealer_store_fields_gone(self):
		"""1.11.0: the retired dealer store's fields are off the doctypes. The
		reassign reads none of them and delists on the retail store only."""
		from ffl_core.tests.test_remove_dealer_store import refuse_removed_field_reads

		with refuse_removed_field_reads():
			calls, serial = self._reassign_with_enqueue_spy(
				serial_fields={"woo_product_id": "4846"})
		self.assertEqual([c.kwargs["site"] for c in calls], ["retail"])
		self.assertEqual(calls[0].kwargs["serial_no"], serial)

	def test_reassign_delists_nothing_when_the_gun_has_no_own_listing(self):
		"""The ordinary case — an item-level listing is not the gun's to delist,
		and drafting one would pull the whole model off the storefront."""
		calls, _ = self._reassign_with_enqueue_spy(serial_fields=None)
		self.assertEqual(calls, [])

	def test_reimport_renames_serial_on_correction(self):
		# A corrected serial renames the Serial No in place; stock + A&D follow.
		sfx = self._testMethodName
		gun = {
			"id": f"fb-rn-{sfx}", "serial": f"RN-OLD-{sfx}", "manufacturer": "Colt",
			"model": "1911", "caliber": "45ACP", "type": "Pistol",
			"acquireType": "Individual", "acquireDate": "2026-04-01T10:00:00Z",
			"acquisitionCost": 700.0,
		}
		with self._patch_client([[gun]]):
			importer.import_bound_book(commit_each=0, aggregate_by="model")
		new_serial = f"RN-NEW-{sfx}"
		with self._patch_client([[{**gun, "serial": new_serial}]]):
			result = importer.import_bound_book(commit_each=0, aggregate_by="model")
		self.assertEqual(result["updated"], 1)
		self.assertFalse(frappe.db.exists("Serial No", gun["serial"]))   # old serial gone
		self.assertEqual(frappe.db.get_value("Serial No", new_serial, "status"), "Active")
		self.assertEqual(
			frappe.db.get_value("FFL Acquisition", {"fastbound_id": gun["id"]}, "serial"), new_serial)
		self.assertTrue(any(c["change"] == "renamed" for c in result["corrected"]))

	def test_import_partial_failure_does_not_poison_batch(self):
		bad_then_good = [
			{**_sample_items(self._testMethodName)[0], "id": ""},  # missing id → error
			_sample_items(self._testMethodName)[1],
		]
		with self._patch_client([bad_then_good]):
			result = importer.import_bound_book(commit_each=0)
		self.assertEqual(result["fetched"], 2)
		self.assertEqual(result["created"], 1)
		self.assertEqual(result["error_count"], 1)
		good_id = _sample_items(self._testMethodName)[1]["id"]
		self.assertTrue(frappe.db.exists("FFL Acquisition", {"fastbound_id": good_id}))

	def test_pagination_consumes_all_pages(self):
		page1 = _sample_items(self._testMethodName)[:1]
		page2 = _sample_items(self._testMethodName)[1:]
		with self._patch_client([page1, page2]):
			result = importer.import_bound_book(commit_each=0)
		self.assertEqual(result["fetched"], 2)
		self.assertEqual(result["created"], 2)

	def test_skip_disposed_imports_only_in_stock(self):
		# Default skip_disposed=1 → only current inventory; a disposed (sold) gun
		# is skipped, not re-stocked.
		sfx = self._testMethodName
		live, sold = _sample_items(sfx)
		live = {**live, "id": f"fb-live-{sfx}", "serial": f"LIVE-{sfx}"}
		sold = {**sold, "id": f"fb-sold-{sfx}", "serial": f"SOLD-{sfx}", "disposed": True}
		with self._patch_client([[live, sold]]):
			result = importer.import_bound_book(commit_each=0)
		self.assertEqual(result["created"], 1)
		self.assertEqual(result["disposed_skipped"], 1)
		self.assertTrue(frappe.db.exists("FFL Acquisition", {"fastbound_id": live["id"]}))
		self.assertFalse(frappe.db.exists("FFL Acquisition", {"fastbound_id": sold["id"]}))

	def test_dry_run_projects_grouping_without_writing(self):
		sfx = self._testMethodName
		items = _sample_items(sfx)
		with self._patch_client([items]):
			result = importer.import_bound_book(commit_each=0, dry_run=1, aggregate_by="model")
		self.assertEqual(result["created"], 0)
		for fb in items:  # nothing written
			self.assertFalse(frappe.db.exists("FFL Acquisition", {"fastbound_id": fb["id"]}))
		# the projection lists the two distinct model items + their serials
		self.assertEqual(result["projected_items"], 2)
		self.assertEqual(
			sorted(s for d in result["projected"].values() for s in d["serials"]),
			sorted(fb["serial"] for fb in items))

	def test_model_mode_folds_caliber_spelling(self):
		# "45 AUTO" and ".45ACP" are the same cartridge — model mode must fold them
		# into one Item (canonicalised caliber), across different makers.
		sfx = self._testMethodName
		a = {
			"id": f"fb-ca-{sfx}", "serial": f"CA-{sfx}", "manufacturer": "Colt",
			"model": "1911", "caliber": "45 AUTO", "type": "Pistol",
			"acquireType": "Individual", "acquireDate": "2026-04-01T10:00:00Z",
			"acquisitionCost": 700.0,
		}
		b = {**a, "id": f"fb-cb-{sfx}", "serial": f"CB-{sfx}",
			"manufacturer": "Remington Rand", "caliber": ".45ACP"}
		with self._patch_client([[a, b]]):
			result = importer.import_bound_book(commit_each=0, aggregate_by="model")
		self.assertEqual(result["created"], 2)
		code = importer._item_code_for(a, aggregate_by="model")
		self.assertEqual(code, "1911-45ACP")
		for gun in (a, b):
			self.assertEqual(
				frappe.db.get_value("FFL Acquisition", {"fastbound_id": gun["id"]}, "item"), code)


class MapFirearmType(unittest.TestCase):
	"""_map_firearm_type is the single FB→POS type mapper — import stamping,
	reconcile's _pos_type/_fb_type canonicalizers, and FB-wins resolution all
	route through it. Pin the NFA vocabulary and the precedence trap: FB
	"Short Barreled Rifle" contains "rifle" and must NOT fold into "Rifle"."""

	def test_nfa_types_map_to_themselves(self):
		for fb in (
			"Silencer", "Machine Gun", "Short Barreled Rifle",
			"Short Barreled Shotgun", "Destructive Device", "Any Other Weapon",
		):
			self.assertEqual(importer._map_firearm_type(fb), fb)

	def test_nfa_synonyms_and_spellings(self):
		cases = {
			"Suppressor": "Silencer",
			"SBR": "Short Barreled Rifle",
			"SBS": "Short Barreled Shotgun",
			"Short-Barreled Rifle": "Short Barreled Rifle",
			"Short-Barreled Shotgun": "Short Barreled Shotgun",
			"AOW": "Any Other Weapon",
			"MachineGun": "Machine Gun",
			"Destructive Device (DD)": "Destructive Device",
		}
		for fb, ours in cases.items():
			self.assertEqual(importer._map_firearm_type(fb), ours, fb)

	def test_generic_types_unchanged(self):
		self.assertEqual(importer._map_firearm_type("Rifle"), "Rifle")
		self.assertEqual(importer._map_firearm_type("Shotgun"), "Shotgun")
		self.assertEqual(importer._map_firearm_type("Frame"), "Receiver")
		self.assertEqual(importer._map_firearm_type("Semi-Auto Pistol"), "Pistol")
		self.assertIsNone(importer._map_firearm_type(""))
		self.assertIsNone(importer._map_firearm_type(None))
		self.assertEqual(importer._map_firearm_type("Curio"), "Other Firearm")

	def test_nfa_tokens_are_word_bounded(self):
		# "SBS"/"SBR"/"AOW" match only as whole tokens — letters buried inside
		# another word must not reclassify the item.
		self.assertEqual(importer._map_firearm_type("Rossbsomething"), "Other Firearm")
		self.assertEqual(importer._map_firearm_type("Straow Mk2"), "Other Firearm")

	def test_suppressor_ready_host_guns_keep_their_host_type(self):
		# "Suppressor ready/capable/compatible" is host-gun marketing copy —
		# an industry-standard phrasing on FB types (partner FFLs type freely
		# into the shared book) — and must not reclassify the gun.
		cases = {
			"Suppressor Ready Pistol": "Pistol",
			"Pistol - Suppressor Ready": "Pistol",
			"Suppressor-Ready Rifle": "Rifle",
			"Silencer Capable Shotgun": "Shotgun",
			"Suppressor Compatible Rifle": "Rifle",
			"Suppressor Host Rifle": "Rifle",
			"Sound Suppressor": "Silencer",
			# Accessory noun-heads: not a silencer, and no host type either —
			# they fall to the neutral bucket instead of minting NFA records.
			"Suppressor Cover": "Other Firearm",
			"Suppressor Covers": "Other Firearm",
			"Silencer Sleeve": "Other Firearm",
			"Suppressor/Ready Pistol": "Pistol",
		}
		for fb, ours in cases.items():
			self.assertEqual(importer._map_firearm_type(fb), ours, fb)


class FbItemTypeRoundTrip(unittest.TestCase):
	"""POS→FB→POS identity: the outbound allowlist (client._fb_item_type)
	must pass every canonical vocabulary value through unchanged, and the
	FB→POS mapper must return it to the same value. This fixed point is the
	condition that keeps reconcile diff-free for correctly classified guns —
	a value the allowlist folds (the pre-NFA bug) shows up as a permanent
	type diff, and an operator's "FB wins" click then erases the POS
	classification."""

	def test_outbound_allowlist_passes_the_canonical_vocabulary(self):
		from ffl_core.firearm import ATF_FIREARM_TYPES
		from ffl_integrations.fastbound.client import _fb_item_type
		for t in ATF_FIREARM_TYPES:
			self.assertEqual(_fb_item_type(t), t, t)

	def test_pos_fb_pos_round_trip_is_identity(self):
		from ffl_core.firearm import ATF_FIREARM_TYPES
		from ffl_integrations.fastbound.client import _fb_item_type
		for t in ATF_FIREARM_TYPES:
			self.assertEqual(importer._map_firearm_type(_fb_item_type(t)), t, t)

	def test_reconcile_sees_no_diff_for_canonical_types(self):
		from ffl_core.firearm import ATF_FIREARM_TYPES
		from ffl_integrations.fastbound.client import _fb_item_type
		from ffl_integrations.fastbound.reconcile import _fb_type, _pos_type
		for t in ATF_FIREARM_TYPES:
			self.assertEqual(_pos_type(t), _fb_type(_fb_item_type(t)), t)

	def test_legacy_aliases_and_junk_still_fold(self):
		from ffl_integrations.fastbound.client import _fb_item_type
		self.assertEqual(_fb_item_type("Handgun"), "Pistol")
		self.assertEqual(_fb_item_type("Long Gun"), "Rifle")
		self.assertEqual(_fb_item_type("Other"), "Other Firearm")
		self.assertEqual(_fb_item_type(""), "Other Firearm")
		self.assertEqual(_fb_item_type(None), "Other Firearm")
		self.assertEqual(_fb_item_type("Garbage"), "Other Firearm")


if __name__ == "__main__":
	unittest.main()
