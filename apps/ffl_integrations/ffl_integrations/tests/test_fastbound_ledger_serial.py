"""Same-serial and no-serial guns against FastBound.

The bound book may hold several in-stock items with no serial ("NONE MARKED", "NSN")
or with the same serial (two models from one maker; a gun and a suppressor marked
alike). Each must arrive in the POS as its own gun with its own key, re-imports must
neither rename, merge nor duplicate them, and everything sent back to FastBound must
carry the bound-book serial, never the POS key.

All serials, item numbers and ids are made up.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.fastbound import importer, reconcile, tasks, webhook
from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfig, FastBoundError
# The module, not its TestCase class: a TestCase imported into this namespace would be
# collected and run a second time as part of this module.
from ffl_integrations.tests import test_fastbound_importer as _importer_tests
from ffl_integrations.tests.test_fastbound_importer import _drop_leftovers_from_earlier_runs


def _client() -> FastBoundClient:
	return FastBoundClient(FastBoundConfig(account_num="A", api_key="k",
		base_url="https://example.invalid", api_version="1.0", audit_user="u", timeout=5),
		session=MagicMock())


def _gun(fb_id: str, number: str, serial: str, *, model="Example 10", maker="Example Arms",
		caliber="9MM", ftype="Pistol") -> dict:
	return {"id": fb_id, "itemNumber": number, "serial": serial, "manufacturer": maker,
		"model": model, "caliber": caliber, "type": ftype, "acquisitionType": "Individual",
		"acquire_Date": "2026-04-01T10:00:00Z", "cost": 300.0}


class _ImportCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		_importer_tests.FastBoundImporter._ensure_masters()

	def setUp(self):
		_drop_leftovers_from_earlier_runs(self._testMethodName)
		# Short per-method tag: keys are built from item numbers, and the class
		# rollback only runs at tearDownClass, so numbers must not repeat across methods.
		self.t = "".join(c for c in self._testMethodName.upper() if c.isalnum())[-10:]
		self.sfx = self._testMethodName

	def _import(self, items, **kw):
		fake = _importer_tests.FastBoundImporter._FakeClient([items])
		with patch.object(importer.FastBoundClient, "from_settings", return_value=fake):
			return importer.import_bound_book(commit_each=0, aggregate_by="model", **kw)

	def _key_of(self, fb_id: str) -> str:
		return frappe.db.get_value("Serial No", {"fastbound_item_id": fb_id}, "name")

	def _assert_one_gun_each(self, guns):
		keys = set()
		for g in guns:
			key = self._key_of(g["id"])
			self.assertTrue(key, g)
			keys.add(key)
			self.assertEqual(frappe.db.get_value("Serial No", key, "status"), "Active")
			self.assertEqual(frappe.db.get_value("Serial No", key, "ledger_serial"), g["serial"].strip())
			self.assertEqual(frappe.db.get_value("Serial No", key, "fastbound_item_number"), g["itemNumber"])
			acqs = frappe.get_all("FFL Acquisition", filters={"fastbound_id": g["id"]},
				fields=["serial", "docstatus", "ledger_serial"])
			self.assertEqual(len(acqs), 1, g)
			self.assertEqual((acqs[0].serial, acqs[0].docstatus, acqs[0].ledger_serial),
				(key, 1, g["serial"].strip()))
			receipts = frappe.db.sql("""select count(distinct b.voucher_no) from
				`tabSerial and Batch Bundle` b join `tabSerial and Batch Entry` e on e.parent = b.name
				where e.serial_no = %s and b.type_of_transaction = 'Inward' and b.docstatus = 1
				and b.is_cancelled = 0""", key)[0][0]
			self.assertEqual(receipts, 1, f"{key}: exactly one stock receipt")
		self.assertEqual(len(keys), len(guns), "every bound-book item is its own gun")
		return keys


class ImportSameAndNoSerial(_ImportCase):
	def test_T1_two_none_marked_on_one_item_both_arrive_and_reruns_change_nothing(self):
		a = _gun(f"fb-a-{self.sfx}", f"{self.t}A", "NONE MARKED")
		b = _gun(f"fb-b-{self.sfx}", f"{self.t}B", "NONE MARKED")
		res = self._import([a, b])
		self.assertEqual((res["created"], res["error_count"]), (2, 0), res["errors"])
		keys = self._assert_one_gun_each([a, b])
		self.assertEqual(keys, {f"NONE MARKED (FB {self.t}A)", f"NONE MARKED (FB {self.t}B)"})
		for key in keys:
			self.assertEqual(frappe.db.get_value("Serial No", key, "no_serial"), 1)
		for _ in range(3):
			again = self._import([a, b])
			self.assertEqual((again["created"], again["updated"], again["error_count"]), (0, 2, 0))
			self.assertEqual(again["corrected"], [])
		self.assertEqual(self._assert_one_gun_each([a, b]), keys)

	def test_T2_same_serial_different_models_both_tagged_and_previewed(self):
		number = f"QQ-{self.t}"
		a = _gun(f"fb-c-{self.sfx}", f"{self.t}C", number, model="Example 10")
		b = _gun(f"fb-d-{self.sfx}", f"{self.t}D", number.lower(), model="Example 20")
		dry = self._import([a, b], dry_run=1)
		groups = {g["ledger_serial"].casefold(): g for g in dry["duplicate_groups"]}
		self.assertIn(number.casefold(), groups)
		self.assertEqual(sorted(i["itemNumber"] for i in groups[number.casefold()]["items"]),
			[f"{self.t}C", f"{self.t}D"])
		self.assertFalse(frappe.db.exists("FFL Acquisition", {"fastbound_id": a["id"]}))
		res = self._import([a, b])
		self.assertEqual((res["created"], res["error_count"]), (2, 0), res["errors"])
		keys = self._assert_one_gun_each([a, b])
		self.assertEqual(keys, {f"{number} (FB {self.t}C)", f"{number.lower()} (FB {self.t}D)"})

	def test_four_shapes_preview_import_and_three_reruns(self):
		t = self.t
		guns = [
			_gun(f"fb-n1-{self.sfx}", f"{t}1", "NONE MARKED", model="Example Rifle", ftype="Rifle"),
			_gun(f"fb-n2-{self.sfx}", f"{t}2", "NONE MARKED", model="Example Rifle", ftype="Rifle"),
			_gun(f"fb-n3-{self.sfx}", f"{t}3", "NSN", model="Example Musket", ftype="Rifle"),
			_gun(f"fb-m1-{self.sfx}", f"{t}4", f"SM-{t}", model="Example 10"),
			_gun(f"fb-m2-{self.sfx}", f"{t}5", f"SM-{t}", model="Example 20"),
			_gun(f"fb-s1-{self.sfx}", f"{t}6", f"SA-{t}", model="Example 30"),
			_gun(f"fb-s2-{self.sfx}", f"{t}7", f"SA-{t}", model="Example Can", ftype="Silencer",
				caliber="30 CAL"),
		]
		dry = self._import(guns, dry_run=1)
		previewed = {i["fastbound_id"] for g in dry["duplicate_groups"] for i in g["items"]}
		self.assertEqual(previewed, {g["id"] for g in guns})
		res = self._import(guns)
		self.assertEqual((res["created"], res["error_count"]), (len(guns), 0), res["errors"])
		keys = self._assert_one_gun_each(guns)
		self.assertTrue(all(k.endswith(")") and " (FB " in k for k in keys), keys)
		for _ in range(3):
			again = self._import(guns)
			self.assertEqual((again["created"], again["updated"], again["error_count"]),
				(0, len(guns), 0))
			self.assertEqual(again["corrected"], [])
		self.assertEqual(self._assert_one_gun_each(guns), keys)

	def test_T3_fastbound_serial_edit_moves_only_that_gun(self):
		a = _gun(f"fb-e-{self.sfx}", f"{self.t}E", "NONE MARKED")
		b = _gun(f"fb-f-{self.sfx}", f"{self.t}F", "NONE MARKED")
		self._import([a, b])
		key_b = self._key_of(b["id"])
		fixed = f"ZR-{self.t}"
		res = self._import([{**a, "serial": fixed}, b])
		self.assertEqual(res["error_count"], 0, res["errors"])
		self.assertEqual(self._key_of(a["id"]), fixed, "a real serial now: the gun is named by it")
		self.assertEqual(frappe.db.get_value("Serial No", fixed, "ledger_serial"), fixed)
		self.assertEqual(frappe.db.get_value("Serial No", fixed, "no_serial"), 0)
		self.assertEqual(frappe.db.get_value("FFL Acquisition", {"fastbound_id": a["id"]},
			["serial", "ledger_serial"]), (fixed, fixed))
		self.assertEqual(self._key_of(b["id"]), key_b, "the other gun is untouched")
		self.assertEqual(frappe.db.get_value("Serial No", key_b, "ledger_serial"), "NONE MARKED")

	def test_T3_a_corrected_serial_already_in_use_tags_instead_of_merging(self):
		taken = _gun(f"fb-g-{self.sfx}", f"{self.t}G", f"ZT-{self.t}")
		other = _gun(f"fb-h-{self.sfx}", f"{self.t}H", f"ZU-{self.t}")
		self._import([taken, other])
		res = self._import([taken, {**other, "serial": taken["serial"]}])
		self.assertEqual(res["error_count"], 0, res["errors"])
		self.assertEqual(self._key_of(taken["id"]), taken["serial"])
		moved = self._key_of(other["id"])
		self.assertEqual(moved, f"{taken['serial']} (FB {self.t}H)")
		self.assertEqual(frappe.db.get_value("Serial No", moved, "ledger_serial"), taken["serial"])

	def test_resync_never_falls_back_to_a_bare_serial_name(self):
		"""An in-stock POS gun the importer never linked must not be adopted (and its
		id overwritten) just because a bound-book item carries the same serial."""
		first = _gun(f"fb-i-{self.sfx}", f"{self.t}I", f"ZB-{self.t}")
		self._import([first])
		# A second bound-book item with the same serial, imported in a LATER run on
		# its own: no duplicate group in this run, and the name is taken in stock.
		second = _gun(f"fb-j-{self.sfx}", f"{self.t}J", first["serial"], model="Example 20")
		res = self._import([second])
		self.assertEqual(res["created"], 0)
		self.assertEqual(res["error_count"], 1)
		self.assertIn("already in stock", res["errors"][0]["error"])
		self.assertEqual(frappe.db.get_value("Serial No", first["serial"], "fastbound_item_id"),
			first["id"])

	def test_a_gun_pushed_from_the_pos_is_resynced_not_duplicated(self):
		pushed = _gun(f"fb-k-{self.sfx}", f"{self.t}K", f"ZP-{self.t}")
		self._import([pushed])
		# What a POS push leaves: the acquisition carries FastBound's ACQUISITION id,
		# only the Serial No carries the item id.
		frappe.db.set_value("FFL Acquisition", {"fastbound_id": pushed["id"]}, "fastbound_id",
			f"fb-acq-{self.sfx}", update_modified=False)
		res = self._import([pushed])
		self.assertEqual((res["created"], res["updated"], res["error_count"]), (0, 1, 0))
		self.assertEqual(frappe.db.count("Serial No", {"fastbound_item_id": pushed["id"]}), 1)


class PushAndMatch(FrappeTestCase):
	"""Everything sent to FastBound carries the bound-book serial; the POS key only
	rides as externalId, where it is unique."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		_importer_tests.FastBoundImporter._ensure_masters()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		cls.item = "LSF-TEST-ITEM"
		if not frappe.db.exists("Item", cls.item):
			frappe.get_doc({"doctype": "Item", "item_code": cls.item, "item_name": cls.item,
				"item_group": "Used Firearm", "stock_uom": "Nos", "is_stock_item": 1,
				"has_serial_no": 1, "is_firearm": 1, "firearm_type": "Pistol"}).insert(ignore_permissions=True)

	def _serial(self, key, ledger, **kw):
		frappe.get_doc({"doctype": "Serial No", "serial_no": key, "item_code": self.item,
			"ledger_serial": ledger, **kw}).insert(ignore_permissions=True)
		return key

	def _acq(self, key):
		from ffl_core.acquisition import record_firearm_acquisition
		return frappe.get_doc("FFL Acquisition", record_firearm_acquisition(
			item_code=self.item, serial=key, source_type="FFL Dealer", source_name="EXAMPLE SUPPLY",
			source_ffl="9-99-999-99-9X-99998", cost=100, company=self.company))

	def test_T5_tagged_gun_payload_carries_the_ledger_serial(self):
		key = self._serial("NONE MARKED (POS 9801)", "NONE MARKED")
		body = FastBoundClient._acquisition_payload(self._acq(key))
		item = body["items"][0]
		self.assertEqual(item["serial"], "NONE MARKED")
		self.assertEqual(item["externalId"], key)
		fields = FastBoundClient._firearm_item_fields(self.item, key)
		self.assertEqual(fields["serial"], "NONE MARKED")

	def test_T5_push_result_is_stamped_on_the_pushed_gun(self):
		key = self._serial("NSN (POS 9802)", "NSN")
		twin = self._serial("NSN (POS 9803)", "NSN")
		acq = self._acq(key)
		tasks._record_acquisition_push(acq.name, {"acquisitionId": "fb-acq-9802",
			"items": [{"fbItemId": "fb-item-9802", "serial": "NSN", "itemNumber": "9802"}]})
		self.assertEqual(frappe.db.get_value("Serial No", key,
			["fastbound_item_id", "fastbound_item_number"]), ("fb-item-9802", "9802"))
		self.assertFalse(frappe.db.get_value("Serial No", twin, "fastbound_item_id"))

	def test_update_item_serial_moves_a_tagged_external_id(self):
		sent = {}
		current = {"itemNumber": "9804", "serial": "NONE MARKED", "externalId": "NONE MARKED (FB 9804)"}

		def fake(method, path, *, params=None, json_body=None):
			if method == "GET":
				return dict(current)
			sent["body"] = json_body
			return {}

		client = _client()
		with patch.object(client, "_request", side_effect=fake):
			client.update_item_serial("fb-item-9804", "ZK-9804", new_key="ZK-9804",
				old_key="NONE MARKED (FB 9804)")
		self.assertEqual((sent["body"]["serial"], sent["body"]["externalId"]), ("ZK-9804", "ZK-9804"))
		with patch.object(client, "_request", side_effect=fake):
			client.update_item_serial("fb-item-9804", "NSN", new_key="NSN (FB 9804)",
				old_key="NONE MARKED (FB 9804)")
		self.assertEqual((sent["body"]["serial"], sent["body"]["externalId"]), ("NSN", "NSN (FB 9804)"))

	def test_find_item_by_serial_names_the_candidates_when_ambiguous(self):
		client = _client()
		page = {"items": [{"id": "x1", "itemNumber": "9805", "serial": "NONE MARKED"},
			{"id": "x2", "itemNumber": "9806", "serial": "none marked"}]}
		with patch.object(client, "_request", return_value=page):
			with self.assertRaises(FastBoundError) as ctx:
				client.find_item_by_serial("NONE MARKED")
		self.assertIn("9805", str(ctx.exception))
		self.assertIn("9806", str(ctx.exception))
		with patch.object(client, "_request", return_value=page):
			got = client.find_item_by_serial("NONE MARKED", skip=lambda iid: iid == "x1")
		self.assertEqual(got["id"], "x2")

	def test_adopt_skips_items_another_gun_already_holds(self):
		self._serial("ZA-9807", "ZA-9807", fastbound_item_id="y1")
		key = self._serial("ZA-9807 (POS 9808)", "ZA-9807")
		acq = self._acq(key)
		client = _client()
		page = {"items": [{"id": "y1", "itemNumber": "9807", "serial": "ZA-9807"},
			{"id": "y2", "itemNumber": "9808", "serial": "ZA-9807"}]}

		def fake(method, path, *, params=None, json_body=None):
			return page if path == "Items" else {}

		with patch.object(client, "_request", side_effect=fake):
			res = client._adopt_existing_item(acq)
		self.assertEqual(res["items"][0]["fbItemId"], "y2")
		self.assertEqual(res["items"][0]["serial"], "ZA-9807")

	def test_reconcile_link_compares_the_ledger_serial(self):
		key = self._serial("NONE MARKED (POS 9809)", "NONE MARKED")
		acq = self._acq(key)
		fs = MagicMock()
		fs.return_value.get_item.return_value = {"id": "z1", "itemNumber": "9809",
			"serial": "NONE MARKED", "deleted": False}
		fs.return_value.find_acquisition_id_by_item.return_value = "fb-acq-z1"
		with patch.object(reconcile.FastBoundClient, "from_settings", fs):
			res = reconcile.reconcile_record_to_fastbound("FFL Acquisition", acq.name, "z1")
		self.assertTrue(res["ok"])
		self.assertEqual(frappe.db.get_value("Serial No", key, "fastbound_item_id"), "z1")

	def test_diff_reports_no_false_serial_drift_for_tagged_guns(self):
		key = self._serial("NSN (POS 9810)", "NSN", fastbound_item_id="w1", manufacturer=None)
		fb = {"id": "w1", "itemNumber": "9810", "serial": "nsn", "manufacturer": "",
			"importer": "", "model": "", "caliber": "", "type": "Pistol"}
		fs = MagicMock()
		fs.return_value.get_item.return_value = fb
		with patch.object(reconcile.FastBoundClient, "from_settings", fs):
			res = reconcile.compute_serial_fb_diff(key)
		self.assertEqual(res["status"], "ok")
		self.assertIsNone(res.get("serial_diff"))
		fb["serial"] = "NSN-1"
		with patch.object(reconcile.FastBoundClient, "from_settings", fs):
			res = reconcile.compute_serial_fb_diff(key)
		self.assertEqual(res["serial_diff"], {"pos_value": "NSN", "fb_value": "NSN-1"})

	def test_webhook_bare_serial_is_adopted_only_when_it_names_one_gun(self):
		one = self._serial("ZW-9811", "ZW-9811")
		self._serial("NONE MARKED (POS 9812)", "NONE MARKED")
		self._serial("NONE MARKED (POS 9813)", "NONE MARKED")
		got = webhook._our_serials_from_fb_items([
			{"id": "unknown-1", "serial": one.lower()},
			{"id": "unknown-2", "serial": "NONE MARKED"},
		])
		self.assertEqual(got, [one])
