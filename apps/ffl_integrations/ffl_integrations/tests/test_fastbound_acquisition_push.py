"""push_acquisition_to_fastbound RQ task tests with mocked HTTP."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.fastbound.client import FastBoundClient, FastBoundError
from ffl_integrations.fastbound.tasks import push_acquisition_to_fastbound


def _shared_sku_acquisition(company: str, sku: str, serial: str):
	"""Record one serialized unit of a catalog SKU shared by many physical
	guns (the RSR new-firearm path: N units, one Item code, distinct
	Serial Nos). Module-level so test classes can use it without inheriting a
	test-bearing TestCase."""
	from ffl_core.acquisition import record_firearm_acquisition
	if not frappe.db.exists("Item", sku):
		frappe.get_doc({
			"doctype": "Item", "item_code": sku, "item_name": sku,
			"item_group": "Rifle", "stock_uom": "Nos",
			"is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
		}).insert(ignore_permissions=True)
	return record_firearm_acquisition(
		item_code=sku, serial=serial,
		source_type="FFL Dealer", source_name="RSR Group",
		source_ffl="1-12-345-67-8K-99999",
		cost=500, company=company,
	)


class FBAcquisitionPush(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		# Configure FB Settings with placeholder creds for client init.
		s = frappe.get_single("FastBound Settings")
		if not s.account_num:
			s.account_num = "TEST-ACC"
			s.flags.ignore_permissions = True
			s.save()

	def _make_acquisition(self, suffix: str):
		from ffl_core.acquisition import record_firearm_acquisition
		# Need a real Item for the acquisition.
		code = f"FB-PUSH-{suffix}"
		if not frappe.db.exists("Item", code):
			frappe.get_doc({
				"doctype": "Item", "item_code": code, "item_name": code,
				"item_group": "Rifle", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 0,
				"firearm_serial": f"FB-MFG-{suffix}",
			}).insert(ignore_permissions=True)
		return record_firearm_acquisition(
			item_code=code, serial=f"FB-MFG-{suffix}",
			source_type="FFL Dealer", source_name="ACME",
			source_ffl="1-12-345-67-8K-99999",
			cost=200, company=self.__class__.company,
		)

	def _shared_sku_acquisition(self, sku: str, serial: str):
		return _shared_sku_acquisition(self.__class__.company, sku, serial)

	def test_shared_sku_units_get_distinct_item_external_ids(self):
		"""Two units of one catalog SKU must push distinct FastBound item
		externalIds. Regression: externalId was the Item code, so the 2nd unit
		collided — 'External ID <sku> is already used on an acquired item'."""
		sku = f"SKU-{self._testMethodName}"
		sn_a, sn_b = f"SN-A-{self._testMethodName}", f"SN-B-{self._testMethodName}"
		a1 = self._shared_sku_acquisition(sku, sn_a)
		a2 = self._shared_sku_acquisition(sku, sn_b)
		eid1 = FastBoundClient._acquisition_payload(
			frappe.get_doc("FFL Acquisition", a1))["items"][0]["externalId"]
		eid2 = FastBoundClient._acquisition_payload(
			frappe.get_doc("FFL Acquisition", a2))["items"][0]["externalId"]
		# The externalId identifies the physical gun (its serial), not the SKU.
		self.assertEqual(eid1, sn_a)
		self.assertEqual(eid2, sn_b)
		self.assertNotEqual(eid1, eid2)  # the collision that broke the push

	def test_payload_includes_sku_upc_mpn(self):
		"""The acquired-item payload carries the dealer SKU (item code), UPC and
		MPN so FastBound's item 'Other' fields (UPC/SKU/MPN) get populated."""
		sku = f"SKU-{self._testMethodName}"
		a = self._shared_sku_acquisition(sku, f"SN-{self._testMethodName}")
		frappe.db.set_value("Item", sku,
			{"upc": "012345678905", "mpn": "MPN-9000"}, update_modified=False)
		item = FastBoundClient._acquisition_payload(
			frappe.get_doc("FFL Acquisition", a))["items"][0]
		self.assertEqual(item.get("sku"), sku)
		self.assertEqual(item.get("upc"), "012345678905")
		self.assertEqual(item.get("mpn"), "MPN-9000")

	def test_payload_type_uses_acquisition_type_when_set(self):
		"""A FastBound-synced Acquisition Type on the FFL Acquisition is sent
		verbatim as the bound-book acquisition `type`."""
		a = self._shared_sku_acquisition(f"SKU-{self._testMethodName}", f"SN-{self._testMethodName}")
		frappe.db.set_value("FFL Acquisition", a, "transaction_type", "Consignment", update_modified=False)
		body = FastBoundClient._acquisition_payload(frappe.get_doc("FFL Acquisition", a))
		self.assertEqual(body["type"], "Consignment")

	def test_payload_type_falls_back_to_source_type(self):
		"""With no Acquisition Type set, the push falls back to the source-type
		mapping (FFL Dealer → FFL) — preserves pre-existing behaviour."""
		a = self._shared_sku_acquisition(f"SKU-{self._testMethodName}", f"SN-{self._testMethodName}")
		body = FastBoundClient._acquisition_payload(frappe.get_doc("FFL Acquisition", a))
		self.assertEqual(body["type"], "FFL")

	def _ensure_brand(self, name: str):
		if not frappe.db.exists("Brand", name):
			frappe.get_doc({"doctype": "Brand", "brand": name}).insert(ignore_permissions=True)
		return name

	def _ensure_serial(self, sku: str, serial: str, **overrides):
		frappe.delete_doc_if_exists("Serial No", serial)
		doc = frappe.get_doc({
			"doctype": "Serial No", "serial_no": serial, "item_code": sku, **overrides,
		})
		doc.insert(ignore_permissions=True)
		return doc

	def test_payload_prefers_per_serial_manufacturer_and_importer(self):
		"""Old / surplus / imported guns under a shared model SKU can each carry
		their own maker / importer on the Serial No. The bound-book push must
		record THAT unit's value, not the model-level Item value."""
		sku, serial = f"SKU-{self._testMethodName}", f"SN-{self._testMethodName}"
		item_brand = self._ensure_brand(f"ItemMaker-{self._testMethodName}")
		unit_brand = self._ensure_brand(f"UnitMaker-{self._testMethodName}")
		a = self._shared_sku_acquisition(sku, serial)
		frappe.db.set_value("Item", sku, {
			"manufacturer": item_brand, "importer": "Item Importer",
		}, update_modified=False)
		self._ensure_serial(sku, serial, manufacturer=unit_brand, importer="Unit Importer")
		item = FastBoundClient._acquisition_payload(
			frappe.get_doc("FFL Acquisition", a))["items"][0]
		self.assertEqual(item["manufacturer"], unit_brand)
		self.assertEqual(item["importer"], "Unit Importer")

	def test_payload_falls_back_to_item_when_serial_has_no_override(self):
		"""A serial with no per-unit maker falls back to the model SKU's
		manufacturer — the common new-firearm case is unchanged. importer does
		NOT fall back (2026-08-20): it is per-gun only, so a serial without one
		is pushed without one rather than with the Item's."""
		sku, serial = f"SKU-{self._testMethodName}", f"SN-{self._testMethodName}"
		item_brand = self._ensure_brand(f"ItemMaker-{self._testMethodName}")
		a = self._shared_sku_acquisition(sku, serial)
		frappe.db.set_value("Item", sku, {
			"manufacturer": item_brand, "importer": "Item Importer",
		}, update_modified=False)
		self._ensure_serial(sku, serial)  # no per-unit override
		item = FastBoundClient._acquisition_payload(
			frappe.get_doc("FFL Acquisition", a))["items"][0]
		self.assertEqual(item["manufacturer"], item_brand)
		self.assertNotIn("importer", item)

	def _firearm_item(self, sku: str, **fields):
		if not frappe.db.exists("Item", sku):
			frappe.get_doc({
				"doctype": "Item", "item_code": sku, "item_name": sku,
				"item_group": "Rifle", "stock_uom": "Nos", "is_stock_item": 1,
				"has_serial_no": 1, "is_firearm": 1, **fields,
			}).insert(ignore_permissions=True)

	def test_push_serial_edits_fastbound_item_with_per_serial_values(self):
		"""The Serial No form's 'sync to FastBound' calls PUT /Items/{id} with
		the per-unit manufacturer / importer for a gun already in the book."""
		from ffl_integrations.fastbound.tasks import push_serial_to_fastbound
		sku, serial = f"SKU-{self._testMethodName}", f"SN-{self._testMethodName}"
		item_brand = self._ensure_brand(f"ItemMaker-{self._testMethodName}")
		unit_brand = self._ensure_brand(f"UnitMaker-{self._testMethodName}")
		self._firearm_item(sku, manufacturer=item_brand, importer="Item Importer")
		self._ensure_serial(sku, serial, fastbound_item_id="fb-item-1",
			manufacturer=unit_brand, importer="Unit Importer")
		with patch.object(FastBoundClient, "from_settings") as factory:
			client = factory.return_value
			res = push_serial_to_fastbound(serial)
		self.assertTrue(res["ok"])
		client.update_item.assert_called_once()
		args = client.update_item.call_args.args
		self.assertEqual(args[0], "fb-item-1")  # the FastBound item id
		self.assertEqual(args[1]["manufacturer"], unit_brand)
		self.assertEqual(args[1]["importer"], "Unit Importer")

	def test_push_serial_without_fastbound_id_is_noop(self):
		"""A gun not yet in FastBound (no fastbound_item_id) has nothing to edit
		— the call short-circuits without building a client."""
		from ffl_integrations.fastbound.tasks import push_serial_to_fastbound
		sku, serial = f"SKU-{self._testMethodName}", f"SN-{self._testMethodName}"
		self._firearm_item(sku)
		self._ensure_serial(sku, serial)  # no fastbound_item_id
		with patch.object(FastBoundClient, "from_settings") as factory:
			res = push_serial_to_fastbound(serial)
		self.assertFalse(res["ok"])
		self.assertEqual(res["reason"], "not_in_fastbound")
		factory.assert_not_called()

	@staticmethod
	def _push_enqueued_for(enq_mock, acq_name: str) -> bool:
		"""True if frappe.enqueue was asked to run push_acquisition_to_fastbound
		for this acquisition (scans all calls — submit may enqueue other jobs)."""
		for call in enq_mock.call_args_list:
			args, kwargs = call
			target = str(args[0]) if args else str(kwargs.get("method", ""))
			if "push_acquisition_to_fastbound" in target and \
					kwargs.get("acquisition_name") == acq_name:
				return True
		return False

	def test_submit_without_fastbound_id_enqueues_push(self):
		"""on_submit wiring: submitting an unsynced FFL Acquisition enqueues the
		FastBound push, so the gun reaches the bound book without waiting for
		the daily reconcile."""
		with patch("frappe.enqueue") as enq:
			acq_name = self._make_acquisition(self._testMethodName)
		self.assertTrue(
			self._push_enqueued_for(enq, acq_name),
			"submitting an FFL Acquisition should enqueue push_acquisition_to_fastbound",
		)

	def test_submit_with_fastbound_id_does_not_enqueue(self):
		"""Imported acquisitions arrive already-synced (fastbound_id set by the
		bound-book importer). on_submit must NOT push them back to FastBound."""
		from ffl_core.acquisition import record_firearm_acquisition
		code = f"FB-IMPORTED-{self._testMethodName}"
		if not frappe.db.exists("Item", code):
			frappe.get_doc({
				"doctype": "Item", "item_code": code, "item_name": code,
				"item_group": "Rifle", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 0,
				"firearm_serial": f"IMP-{self._testMethodName}",
			}).insert(ignore_permissions=True)
		with patch("frappe.enqueue") as enq:
			acq_name = record_firearm_acquisition(
				item_code=code, serial=f"IMP-{self._testMethodName}",
				source_type="FFL Dealer", source_name="ACME",
				source_ffl="1-12-345-67-8K-99999",
				cost=200, company=self.__class__.company,
				fastbound_id="fb-already-synced",
			)
		self.assertFalse(
			self._push_enqueued_for(enq, acq_name),
			"already-synced (imported) acquisitions must not be re-pushed on submit",
		)

	def test_happy_path_stores_fastbound_id(self):
		acq_name = self._make_acquisition(self._testMethodName)
		with patch.object(FastBoundClient, "from_settings") as factory:
			client = factory.return_value
			client.post_acquisition.return_value = {
				"acquisitionId": "fb-id-12345", "items": [],
			}
			push_acquisition_to_fastbound(acq_name, _commit=False)
		acq = frappe.get_doc("FFL Acquisition", acq_name)
		self.assertEqual(acq.fastbound_id, "fb-id-12345")
		self.assertIsNotNone(acq.fastbound_synced_at)
		self.assertEqual(acq.fastbound_sync_error or "", "")

	def test_idempotent_when_already_synced(self):
		acq_name = self._make_acquisition(self._testMethodName)
		# Mark as already synced.
		frappe.db.set_value("FFL Acquisition", acq_name, "fastbound_id", "fb-pre-existing",
			update_modified=False)
		with patch.object(FastBoundClient, "from_settings") as factory:
			client = factory.return_value
			client.post_acquisition.return_value = {
				"acquisitionId": "fb-id-NEW", "items": [],
			}
			push_acquisition_to_fastbound(acq_name, _commit=False)
		# Did NOT call post_acquisition; id unchanged.
		client.post_acquisition.assert_not_called()
		self.assertEqual(
			frappe.db.get_value("FFL Acquisition", acq_name, "fastbound_id"),
			"fb-pre-existing",
		)

	def test_failure_persists_error(self):
		acq_name = self._make_acquisition(self._testMethodName)
		with patch.object(FastBoundClient, "from_settings") as factory:
			client = factory.return_value
			client.post_acquisition.side_effect = FastBoundError("503 Service Unavailable")
			push_acquisition_to_fastbound(acq_name, _commit=False, _retries=0)
		err = frappe.db.get_value("FFL Acquisition", acq_name, "fastbound_sync_error")
		self.assertIn("503", err)
		self.assertIsNone(frappe.db.get_value("FFL Acquisition", acq_name, "fastbound_id"))

	def test_amended_acquisition_deletes_predecessor_then_repushes(self):
		"""Amending a synced acquisition must REPLACE its FastBound record: delete
		the predecessor (frees the serial externalId) then create the corrected
		one. Otherwise the re-push 400s on 'externalId already used' or the bound
		book keeps a stale duplicate of the pre-amendment record."""
		pred = self._make_acquisition(self._testMethodName + "p")
		frappe.db.set_value("FFL Acquisition", pred, "fastbound_id", "fb-OLD", update_modified=False)
		succ = self._make_acquisition(self._testMethodName + "s")
		frappe.db.set_value("FFL Acquisition", succ, "amended_from", pred, update_modified=False)
		with patch.object(FastBoundClient, "from_settings") as factory:
			client = factory.return_value
			client.post_acquisition.return_value = {"acquisitionId": "fb-NEW", "items": []}
			push_acquisition_to_fastbound(succ, _commit=False)
		client.delete_acquisition.assert_called_once_with("fb-OLD")
		self.assertEqual(frappe.db.get_value("FFL Acquisition", succ, "fastbound_id"), "fb-NEW")
		self.assertFalse(frappe.db.get_value("FFL Acquisition", pred, "fastbound_id"),
			"predecessor's fastbound_id should be cleared after its FB record is deleted")

	def test_fastbound_id_not_copied_on_amend(self):
		"""fastbound_id + sync metadata must be no_copy so an amended acquisition
		starts UNSYNCED. Otherwise it inherits the old id and both on_submit and
		reconcile_failed_pushes skip it, so the correction never reaches the book."""
		acq_name = self._make_acquisition(self._testMethodName)
		frappe.db.set_value("FFL Acquisition", acq_name, {
			"fastbound_id": "fb-XYZ",
			"fastbound_synced_at": frappe.utils.now_datetime(),
			"fastbound_sync_error": "stale",
		}, update_modified=False)
		# ignore_no_copy=False mirrors how amend/duplicate build the new doc.
		amended = frappe.copy_doc(frappe.get_doc("FFL Acquisition", acq_name), ignore_no_copy=False)
		self.assertFalse(amended.fastbound_id, "fastbound_id must be cleared on amend (no_copy)")
		self.assertFalse(amended.fastbound_synced_at)
		self.assertFalse(amended.fastbound_sync_error)

	# ---------------------------------------------- synchronous (atomic) receive

	def test_sync_push_stamps_fastbound_id(self):
		from ffl_integrations.fastbound.tasks import push_receipt_acquisitions_sync
		a = self._make_acquisition(self._testMethodName)
		frappe.db.set_value("FFL Acquisition", a, "purchase_receipt", "PR-SYNC-OK",
			update_modified=False)
		with patch.object(FastBoundClient, "from_settings") as factory:
			client = factory.return_value
			client.post_acquisition.return_value = {"acquisitionId": "fb-sync-1", "items": []}
			push_receipt_acquisitions_sync("PR-SYNC-OK")
		self.assertEqual(frappe.db.get_value("FFL Acquisition", a, "fastbound_id"), "fb-sync-1")

	def test_sync_push_deletes_siblings_and_raises_on_failure(self):
		"""Atomic receive: when one gun's push fails, the siblings already
		committed to FastBound are deleted and the call raises, so the caller's
		rollback leaves no orphan bound-book records."""
		from ffl_integrations.fastbound.tasks import push_receipt_acquisitions_sync
		a1 = self._make_acquisition(self._testMethodName + "A")
		a2 = self._make_acquisition(self._testMethodName + "B")
		for a in (a1, a2):
			frappe.db.set_value("FFL Acquisition", a, "purchase_receipt", "PR-SYNC-FAIL",
				update_modified=False)
		with patch.object(FastBoundClient, "from_settings") as factory:
			client = factory.return_value
			# First gun is accepted; the second is rejected (hard 400 → no retry).
			client.post_acquisition.side_effect = [
				{"acquisitionId": "fb-ok-1", "items": []},
				FastBoundError("contact rejected", status_code=400),
			]
			with self.assertRaises(frappe.ValidationError):
				push_receipt_acquisitions_sync("PR-SYNC-FAIL")
			client.delete_acquisition.assert_called_once_with("fb-ok-1")

	def test_sync_push_never_deletes_adopted_acquisition(self):
		"""Atomic receive: a gun whose serial was already in the book is ADOPTED
		into a pre-existing FastBound acquisition (which can hold other guns).
		When a later gun fails, compensation must delete only what this call
		created — never the adopted acquisition."""
		from ffl_integrations.fastbound.tasks import push_receipt_acquisitions_sync
		names = [self._make_acquisition(self._testMethodName + s) for s in "ABC"]
		for a in names:
			frappe.db.set_value("FFL Acquisition", a, "purchase_receipt", "PR-SYNC-ADOPT",
				update_modified=False)
		with patch.object(FastBoundClient, "from_settings") as factory:
			client = factory.return_value
			client.post_acquisition.side_effect = [
				{"acquisitionId": "fb-new-1", "items": []},
				{"acquisitionId": "fb-shared", "adopted": True, "items": []},
				FastBoundError("contact rejected", status_code=400),
			]
			with self.assertRaises(frappe.ValidationError):
				push_receipt_acquisitions_sync("PR-SYNC-ADOPT")
			client.delete_acquisition.assert_called_once_with("fb-new-1")

	def test_payload_prefers_structured_name_with_optional_middle(self):
		"""The FastBound contact uses the structured first/middle/last captured at
		receive — a one-word customer_name no longer loses the last name."""
		a = self._make_acquisition(self._testMethodName)
		frappe.db.set_value("FFL Acquisition", a, {
			"source_type": "Individual", "source_name": "Cher",
			"source_first_name": "Marie", "source_middle_name": "Q",
			"source_last_name": "Curie",
		}, update_modified=False)
		contact = FastBoundClient._acquisition_payload(
			frappe.get_doc("FFL Acquisition", a))["contact"]
		self.assertEqual(contact["firstName"], "Marie")
		self.assertEqual(contact["middleName"], "Q")
		self.assertEqual(contact["lastName"], "Curie")

	def test_payload_falls_back_to_split_without_structured_name(self):
		a = self._make_acquisition(self._testMethodName)
		frappe.db.set_value("FFL Acquisition", a, {
			"source_type": "Individual", "source_name": "John Smith",
		}, update_modified=False)
		contact = FastBoundClient._acquisition_payload(
			frappe.get_doc("FFL Acquisition", a))["contact"]
		self.assertEqual(contact["firstName"], "John")
		self.assertEqual(contact["lastName"], "Smith")
		self.assertNotIn("middleName", contact)


class ExternalIdConflictSelfHeal(FrappeTestCase):
	"""A returned gun's re-acquisition collides with the PREVIOUS ownership
	cycle's FastBound item, which still holds externalId=serial (2026-08-21
	JV-return incident: every dealer/consignment return hit this and sat in the
	daily retry pool forever). post_acquisition must free the id — rename the
	DISPOSED holder's externalId — and retry once; an ACTIVE holder is a real
	duplicate and must fail terminal instead.

	Standalone (not inheriting FBAcquisitionPush): inheriting a test-bearing
	TestCase re-runs all of its cases under this class's name."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")

	def _shared_sku_acquisition(self, sku: str, serial: str):
		return _shared_sku_acquisition(self.company, sku, serial)

	_CONFLICT = ('POST Acquisitions/CreateAndCommit → 400: {"errors":[{"message":'
		'"External ID %s is already used on an acquired item."}]}')

	def _run_push(self, acq_name: str, holder: dict | None, calls: list):
		"""Drive client.post_acquisition with a routed _request mock: first
		CreateAndCommit 400s on externalId, GetByExternalId serves `holder`,
		SetExternalId is recorded, second CreateAndCommit succeeds."""
		from ffl_integrations.fastbound.client import FastBoundConfig
		client = FastBoundClient(FastBoundConfig(
			account_num="A", api_key="k", base_url="https://cloud.fastbound.com",
			api_version="1.0", audit_user="t@t", timeout=30))
		doc = frappe.get_doc("FFL Acquisition", acq_name)
		serial = doc.serial
		state = {"posts": 0}

		def fake_request(method, path, *, params=None, json_body=None):
			calls.append((method, path, json_body))
			if method == "POST" and path == "Acquisitions/CreateAndCommit":
				state["posts"] += 1
				if state["posts"] == 1:
					raise FastBoundError(self._CONFLICT % serial, status_code=400)
				return {"id": "new-acq-guid",
					"items": [{"id": "new-item-guid", "serial": serial}]}
			if method == "GET" and path.startswith("Items/GetByExternalId/"):
				if holder is None:
					raise FastBoundError("not found", status_code=404)
				return holder
			if method == "PUT" and path.endswith("/SetExternalId"):
				return {}
			raise AssertionError(f"unexpected request {method} {path}")

		with patch.object(client, "_request", side_effect=fake_request), \
				patch.object(client, "_resolve_existing_contact_id", return_value=None):
			return client.post_acquisition(doc)

	def test_disposed_holder_is_renamed_and_push_retried(self):
		a = self._shared_sku_acquisition(
			f"SKU-{self._testMethodName}", f"SN-{self._testMethodName}")
		serial = frappe.db.get_value("FFL Acquisition", a, "serial")
		calls: list = []
		holder = {"id": "old-item-guid", "itemNumber": "00042",
			"serial": serial, "disposed": True,
			"status": {"id": 3, "name": "Disposed"}}
		res = self._run_push(a, holder, calls)
		self.assertEqual(res["acquisitionId"], "new-acq-guid")
		renames = [c for c in calls if c[0] == "PUT" and c[1].endswith("/SetExternalId")]
		self.assertEqual(len(renames), 1)
		self.assertEqual(renames[0][1], "Items/old-item-guid/SetExternalId")
		self.assertEqual(renames[0][2], {"externalId": f"{serial}-00042"})
		posts = [c for c in calls if c[0] == "POST"]
		self.assertEqual(len(posts), 2)  # failed once, retried once

	def test_active_holder_is_terminal_and_never_renamed(self):
		a = self._shared_sku_acquisition(
			f"SKU-{self._testMethodName}", f"SN-{self._testMethodName}")
		serial = frappe.db.get_value("FFL Acquisition", a, "serial")
		calls: list = []
		holder = {"id": "old-item-guid", "itemNumber": "00042",
			"serial": serial, "disposed": False,
			"status": {"id": 1, "name": "Available"}}
		with self.assertRaises(FastBoundError) as ctx:
			self._run_push(a, holder, calls)
		self.assertTrue(ctx.exception.terminal)
		self.assertFalse([c for c in calls if c[0] == "PUT"])
		self.assertEqual(len([c for c in calls if c[0] == "POST"]), 1)

	def test_no_holder_found_reraises_original_error(self):
		# The 400 names an externalId we can't find (race: someone freed it, or
		# a different item's id) — nothing to fix here, surface the original.
		a = self._shared_sku_acquisition(
			f"SKU-{self._testMethodName}", f"SN-{self._testMethodName}")
		calls: list = []
		with self.assertRaises(FastBoundError) as ctx:
			self._run_push(a, None, calls)
		self.assertIn("already used", str(ctx.exception))
		self.assertFalse(ctx.exception.terminal)
		self.assertEqual(len([c for c in calls if c[0] == "POST"]), 1)


if __name__ == "__main__":
	unittest.main()
