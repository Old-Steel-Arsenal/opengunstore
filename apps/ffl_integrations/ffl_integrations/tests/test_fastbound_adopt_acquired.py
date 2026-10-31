"""Adopt-on-'already acquired' for the FastBound acquisition push.

When a firearm's serial is already in the FastBound book (entered directly in
FastBound, or a prior push that committed but lost the id), CreateAndCommit 400s
`<gun> has already been acquired` and the record stays stuck forever. The push
must instead ADOPT the existing FastBound item — resolve it live by serial,
take its id (+ parent acquisition id) and mark our record synced — so the bound
book and our mirror agree without a duplicate.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.fastbound.client import (
	FastBoundClient,
	FastBoundConfig,
	FastBoundError,
)


class FBAdoptAcquired(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		s = frappe.get_single("FastBound Settings")
		if not s.account_num:
			s.account_num = "TEST-ACC"
			s.flags.ignore_permissions = True
			s.save()

	def _client(self) -> FastBoundClient:
		cfg = FastBoundConfig(
			account_num="T", api_key="k", base_url="https://fb.test",
			api_version="1.0", audit_user="a@test", timeout=5,
		)
		return FastBoundClient(cfg, session=object())

	def test_find_item_by_serial_returns_live_match(self):
		"""Resolve the in-stock FastBound item for a serial (client-side verify, in
		case FastBound ignores the ?serial= filter like it does ?fflNumber=)."""
		client = self._client()
		pages = [{"items": [
			{"id": "it-1", "serial": "865829", "disposed": False, "deleted": False},
			{"id": "it-2", "serial": "999999", "disposed": False, "deleted": False},
		]}]
		with patch.object(client, "_request", side_effect=pages):
			found = client.find_item_by_serial("865829")
		self.assertEqual(found["id"], "it-1")

	def test_find_item_by_serial_prefers_in_stock_over_disposed(self):
		"""A re-acquired serial can have an old disposed item AND a current one;
		adopt the in-stock (non-disposed) one, unambiguously."""
		client = self._client()
		pages = [{"items": [
			{"id": "old", "serial": "865829", "disposed": True, "deleted": False},
			{"id": "new", "serial": "865829", "disposed": False, "deleted": False},
		]}]
		with patch.object(client, "_request", side_effect=pages):
			found = client.find_item_by_serial("865829")
		self.assertEqual(found["id"], "new")

	def test_find_item_by_serial_ambiguous_raises(self):
		"""Two in-stock items with the same serial is ambiguous — don't guess."""
		client = self._client()
		pages = [{"items": [
			{"id": "a", "serial": "865829", "disposed": False},
			{"id": "b", "serial": "865829", "disposed": False},
		]}]
		with patch.object(client, "_request", side_effect=pages):
			with self.assertRaises(FastBoundError):
				client.find_item_by_serial("865829")

	def _ffl_dealer_acquisition(self, serial: str) -> str:
		from ffl_core.acquisition import record_firearm_acquisition
		code = "ADOPT-ITEM"
		if not frappe.db.exists("Item", code):
			frappe.get_doc({
				"doctype": "Item", "item_code": code, "item_name": code,
				"item_group": "Rifle", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
			}).insert(ignore_permissions=True)
		return record_firearm_acquisition(
			item_code=code, serial=serial,
			source_type="FFL Dealer", source_name="CIVILIAN MARKSMANSHIP PROGRAM",
			source_ffl="1-99-888-07-7M-10282",
			cost=1263.6, company=self.__class__.company,
		)

	def test_push_adopts_existing_item_when_already_acquired(self):
		"""CreateAndCommit 400s 'already been acquired' (serial entered directly in
		FastBound). The push adopts the live item + its acquisition id instead of
		failing — returning the same shape a fresh push would, so the task stamps
		fastbound_id + Serial No.fastbound_item_id and clears the error."""
		a = self._ffl_dealer_acquisition("865829")
		doc = frappe.get_doc("FFL Acquisition", a)
		client = self._client()
		err = FastBoundError(
			'POST Acquisitions/CreateAndCommit → 400: {"errors":[{"message":'
			'"ITHACA GUN CO, INC. M1911A1 U.S.ARMY Pistol .45 ACP/Auto 865829 '
			'has already been acquired."}]}',
			status_code=400,
		)
		with patch.object(client, "_resolve_existing_contact_id", return_value=None), \
				patch.object(client, "find_item_by_serial",
					return_value={"id": "it-865829", "serial": "865829", "disposed": False, "deleted": False}), \
				patch.object(client, "find_acquisition_id_by_item", return_value="acq-xyz"), \
				patch.object(client, "_request", side_effect=err):
			result = client.post_acquisition(doc)
		self.assertEqual(result["acquisitionId"], "acq-xyz")
		self.assertEqual(result["items"], [{"fbItemId": "it-865829", "serial": "865829"}])

	def test_record_acquisition_push_stamps_acquisition_and_serials(self):
		"""The one write-back both push paths (async task + Receive Goods sync push)
		share: fastbound_id/synced_at/cleared error on the acquisition, and the FB
		item id on each returned Serial No that exists — unknown serials and items
		without an fbItemId are skipped, never invented."""
		from ffl_integrations.fastbound.tasks import _record_acquisition_push
		serial = "REC-PUSH-1"
		a = self._ffl_dealer_acquisition(serial)
		if not frappe.db.exists("Serial No", serial):
			frappe.get_doc({"doctype": "Serial No", "serial_no": serial,
				"item_code": "ADOPT-ITEM"}).insert(ignore_permissions=True)
		frappe.db.set_value("FFL Acquisition", a, "fastbound_sync_error", "old error")
		_record_acquisition_push(a, {"acquisitionId": "acq-rec-1", "items": [
			{"serial": serial, "fbItemId": "it-rec-1"},
			{"serial": "REC-PUSH-NOT-A-SERIAL", "fbItemId": "it-ghost"},
			{"serial": serial, "fbItemId": None},
		]})
		row = frappe.db.get_value("FFL Acquisition", a,
			["fastbound_id", "fastbound_synced_at", "fastbound_sync_error"], as_dict=True)
		self.assertEqual(row.fastbound_id, "acq-rec-1")
		self.assertTrue(row.fastbound_synced_at)
		self.assertFalse(row.fastbound_sync_error)
		self.assertEqual(frappe.db.get_value("Serial No", serial, "fastbound_item_id"), "it-rec-1")
		self.assertFalse(frappe.db.exists("Serial No", "REC-PUSH-NOT-A-SERIAL"))

	def test_adopt_raises_when_no_item_found(self):
		"""'Already acquired' but no item resolves — fail loudly (don't silently
		mark synced with nothing behind it)."""
		a = self._ffl_dealer_acquisition("NOITEM1")
		doc = frappe.get_doc("FFL Acquisition", a)
		client = self._client()
		err = FastBoundError('400: {"errors":[{"message":"x NOITEM1 has already been acquired."}]}',
			status_code=400)
		with patch.object(client, "_resolve_existing_contact_id", return_value=None), \
				patch.object(client, "find_item_by_serial", return_value=None), \
				patch.object(client, "_request", side_effect=err):
			with self.assertRaises(FastBoundError):
				client.post_acquisition(doc)

	def test_adopt_raises_when_existing_item_is_disposed(self):
		"""'Already acquired' but the matching item is DISPOSED — don't re-stock a
		sold gun; needs a human."""
		a = self._ffl_dealer_acquisition("DISPSN1")
		doc = frappe.get_doc("FFL Acquisition", a)
		client = self._client()
		err = FastBoundError('400: {"errors":[{"message":"x DISPSN1 has already been acquired."}]}',
			status_code=400)
		with patch.object(client, "_resolve_existing_contact_id", return_value=None), \
				patch.object(client, "find_item_by_serial",
					return_value={"id": "it-disp", "serial": "DISPSN1", "disposed": True}), \
				patch.object(client, "_request", side_effect=err):
			with self.assertRaises(FastBoundError):
				client.post_acquisition(doc)


if __name__ == "__main__":
	unittest.main()
