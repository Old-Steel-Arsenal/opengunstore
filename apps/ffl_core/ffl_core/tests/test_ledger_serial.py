"""Guns the bound book records with no serial, or with the same serial as another gun.

ERPNext names a Serial No uniquely, so such a gun gets a TAGGED key
("NONE MARKED (FB 0901)") and its bound-book serial lives in Serial No.ledger_serial.
These tests pin the helpers in ffl_core.serials, the two acquisition guards that keep
two guns from collapsing into one record, the backfill patch, and the two temporary
gates (no online listing, no sale) that hold a tagged gun back until receipts and
4473 records print its ledger serial.

All serials here are made up. Stock setup runs under a patched commit so the class
rollback removes it (docs/testing.md).
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from ffl_core import serials
from ffl_core.acquisition import record_firearm_acquisition
from ffl_core.tests.settings_sandbox import SettingsSandbox


class _Guns(SettingsSandbox, FrappeTestCase):
	ITEM = "LS-TEST-ITEM"

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		if not frappe.db.exists("Item Group", "Used Firearm"):
			frappe.get_doc({"doctype": "Item Group", "item_group_name": "Used Firearm",
				"parent_item_group": "All Item Groups", "is_group": 0}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", cls.ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": cls.ITEM, "item_name": cls.ITEM,
				"item_group": "Used Firearm", "stock_uom": "Nos", "is_stock_item": 1,
				"has_serial_no": 1, "is_firearm": 1, "firearm_type": "Pistol",
			}).insert(ignore_permissions=True)
		cls.warehouse = frappe.get_all(
			"Warehouse", filters={"company": cls.company, "is_group": 0, "disabled": 0},
			pluck="name", limit=1)[0]

	def _gun(self, key: str, ledger: str | None = None, **fields) -> str:
		"""An in-stock gun named `key` whose bound-book serial is `ledger`."""
		with patch("frappe.db.commit"):
			se = frappe.get_doc({
				"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [{"item_code": self.ITEM, "qty": 1, "t_warehouse": self.warehouse,
					"basic_rate": 100, "use_serial_batch_fields": 1, "serial_no": key}],
			})
			se.flags.ignore_permissions = True
			se.insert()
			se.submit()
		frappe.db.set_value("Serial No", key, {"ledger_serial": ledger or "", **fields},
			update_modified=False)
		return key

	def _acquire(self, key: str, **kw) -> str:
		return record_firearm_acquisition(
			item_code=self.ITEM, serial=key, source_type="Individual",
			source_name="Pat Example", source_id_type="Driver License",
			source_id_number="DL-0000", cost=100, company=self.company, **kw)


class Placeholders(_Guns):
	def test_default_spellings_are_placeholders(self):
		for text in ("NONE", "None Marked", "n.s.n.", "  NO   SERIAL ", "n/a", "Unknown", "", None):
			self.assertTrue(serials.is_placeholder(text), text)
		for text in ("AB123", "NONE-1", "NSN7"):
			self.assertFalse(serials.is_placeholder(text), text)

	def test_settings_add_spellings(self):
		self.assertFalse(serials.is_placeholder("NOT LEGIBLE"))
		self.set_single("FFL Settings", "serial_placeholders", "not legible\nSCRATCHED")
		self.assertTrue(serials.is_placeholder("Not Legible"))
		self.assertTrue(serials.is_placeholder("scratched"))


class AllocateKey(_Guns):
	def test_an_ordinary_serial_is_its_own_key(self):
		self.assertEqual(serials.allocate_serial_key(f"ZQ-{self._testMethodName}"),
			f"ZQ-{self._testMethodName}")

	def test_a_placeholder_is_always_tagged(self):
		self.assertEqual(serials.allocate_serial_key("NONE MARKED", fb_item_number="0901"),
			"NONE MARKED (FB 0901)")
		self.assertRegex(serials.allocate_serial_key("NSN"), r"^NSN \(POS \d{4}\)$")
		self.assertRegex(serials.allocate_serial_key(""), r"^NO SERIAL \(POS \d{4}\)$")

	def test_force_tag_and_taken_names(self):
		plain = self._gun(f"ZT-{self._testMethodName}")
		self.assertEqual(serials.allocate_serial_key(plain, fb_item_number="0902"),
			f"{plain} (FB 0902)", "a name already on file is not reused for a new gun")
		self.assertEqual(serials.allocate_serial_key("ZX-77", fb_item_number="0903", force_tag=True),
			"ZX-77 (FB 0903)")
		taken = self._gun("ZX-78 (FB 0904)", "ZX-78")
		self.assertEqual(serials.allocate_serial_key("ZX-78", fb_item_number="0904", force_tag=True),
			"ZX-78 (FB 0904-2)")
		self.assertTrue(taken)

	def test_key_never_carries_a_separator(self):
		key = serials.allocate_serial_key("AB,12\n3", fb_item_number="0905")
		self.assertEqual(key, "AB 12 3 (FB 0905)")
		self.assertNotRegex(serials.allocate_serial_key("CD|9::1", force_tag=True), r"[,\n|]|::")


class Reading(_Guns):
	def test_ledger_serial_falls_back_to_the_name(self):
		self.assertEqual(serials.ledger_serial("NO-SUCH-GUN-LS"), "NO-SUCH-GUN-LS")
		plain = self._gun(f"RL-{self._testMethodName}")  # ledger left blank: pre-backfill row
		self.assertEqual(serials.ledger_serial(plain), plain)
		tagged = self._gun(f"NONE MARKED (FB R{self._testMethodName[-6:]})", "NONE MARKED")
		self.assertEqual(serials.ledger_serial(tagged), "NONE MARKED")
		self.assertEqual(serials.ledger_serials([plain, tagged, "NO-SUCH-GUN-LS"]),
			{plain: plain, tagged: "NONE MARKED", "NO-SUCH-GUN-LS": "NO-SUCH-GUN-LS"})
		self.assertEqual(serials.ledger_serial_text(f"{plain}\n{tagged}"), f"{plain}, NONE MARKED")
		self.assertTrue(serials.is_tagged(tagged))
		self.assertFalse(serials.is_tagged(plain))

	def test_label_tells_same_serial_guns_apart(self):
		tagged = self._gun(f"NSN (FB L{self._testMethodName[-6:]})", "NSN",
			fastbound_item_number="0911", model_name="Example Carbine")
		label = serials.serial_label(tagged)
		self.assertIn("NSN", label)
		self.assertIn("Example Carbine", label)
		self.assertIn("FB#0911", label)


class Resolve(_Guns):
	def test_a_tagged_key_is_one_gun(self):
		key = self._gun(f"NONE MARKED (FB V{self._testMethodName[-6:]})", "NONE MARKED")
		rows = serials.resolve_serial(key)
		self.assertEqual([r["name"] for r in rows], [key])
		self.assertEqual(rows[0]["ledger_serial"], "NONE MARKED")

	def test_a_ledger_serial_returns_every_gun_carrying_it(self):
		number = f"RS-{self._testMethodName[-8:]}"
		old = self._gun(number, number)
		new = self._gun(f"{number} (POS 9901)", number)
		for text in (number, number.lower(), f" {number} "):
			got = sorted(r["name"] for r in serials.resolve_serial(text))
			self.assertEqual(got, sorted([old, new]), text)
		self.assertEqual(serials.resolve_serial(""), [])

	def test_an_unstamped_gun_is_still_found_by_its_name(self):
		plain = self._gun(f"RU-{self._testMethodName[-8:]}")  # no ledger_serial yet
		self.assertEqual([r["name"] for r in serials.resolve_serial(plain)], [plain])


class AcquisitionGuards(_Guns):
	"""record_firearm_acquisition's dedupe returns an existing record for the same key
	in an open cycle. It must never be the place two different guns become one."""

	def test_a_bare_placeholder_is_refused(self):
		"""No receiving screen can mint a tagged key yet: the refusal says so and names
		no example key a person could copy into the serial field."""
		for text in ("NONE MARKED", "nsn"):
			with self.assertRaisesRegex(frappe.ValidationError, "can't be received in the POS yet") as cm:
				self._acquire(text)
			self.assertNotIn("(POS", str(cm.exception))
			self.assertNotIn("(FB", str(cm.exception))

	def test_tag_shape(self):
		for text in ("NSN (POS 0001)", "NONE MARKED (FB 0901)", "nsn (pos 7)", "A1 (2) (FB 3-2)"):
			self.assertTrue(serials.looks_like_tagged_key(text), text)
		for text in ("A123 (2)", "A123(FB 1)", "(FB 1)", "AB (FBI 1)", "A123", "X (POS)"):
			self.assertFalse(serials.looks_like_tagged_key(text), text)
		# What the allocator mints always has the shape.
		self.assertTrue(serials.looks_like_tagged_key(serials.allocate_serial_key("NSN", fb_item_number="77")))

	def test_a_hand_typed_tag_is_refused(self):
		"""A person copying a key's shape into the serial field: no ledger serial passed,
		none stamped, so the bound book would get the label as the serial."""
		key = self._gun(f"NSN (POS H{self._testMethodName[-6:]})")
		with self.assertRaisesRegex(frappe.ValidationError, "not the serial marked on the gun"):
			self._acquire(key)
		with self.assertRaisesRegex(frappe.ValidationError, "not the serial marked on the gun"):
			self._acquire(key, ledger_serial=key)
		with self.assertRaisesRegex(frappe.ValidationError, "not the serial marked on the gun"):
			self._acquire(f"NONE MARKED (FB H{self._testMethodName[-6:]})")
		self.assertFalse(frappe.db.exists("FFL Acquisition", {"serial": key}))

	def test_an_allocated_tagged_key_is_accepted(self):
		"""The importer's way in: a minted key with its ledger serial passed (or already
		stamped on the gun)."""
		key = serials.allocate_serial_key("NONE MARKED", fb_item_number=f"I{self._testMethodName[-6:]}")
		self._gun(key)
		acq = self._acquire(key, ledger_serial="NONE MARKED")
		self.assertEqual(frappe.db.get_value("FFL Acquisition", acq, "ledger_serial"), "NONE MARKED")
		stamped = self._gun(f"NSN (FB J{self._testMethodName[-6:]})", "NSN")
		acq = self._acquire(stamped)
		self.assertEqual(frappe.db.get_value("FFL Acquisition", acq, "ledger_serial"), "NSN")

	def test_a_real_serial_with_parentheses_is_accepted(self):
		plain = self._gun(f"A{self._testMethodName[-6:]} (2)")
		acq = self._acquire(plain)
		self.assertEqual(frappe.db.get_value("FFL Acquisition", acq, "ledger_serial"), plain)
		self.assertFalse(serials.is_tagged(plain))

	def test_same_key_with_a_different_fastbound_entry_is_refused(self):
		key = self._gun(f"NONE MARKED (FB G{self._testMethodName[-6:]})", "NONE MARKED")
		first = self._acquire(key, fastbound_id=f"fb-g1-{self._testMethodName}")
		with self.assertRaisesRegex(frappe.ValidationError, "can't be booked on the same gun"):
			self._acquire(key, fastbound_id=f"fb-g2-{self._testMethodName}")
		self.assertEqual(frappe.db.count("FFL Acquisition", {"serial": key}), 1)
		self.assertTrue(first)

	def test_same_key_double_write_is_still_idempotent(self):
		key = self._gun(f"AG-{self._testMethodName[-8:]}")
		first = self._acquire(key, fastbound_id=f"fb-g3-{self._testMethodName}")
		self.assertEqual(self._acquire(key), first)
		self.assertEqual(self._acquire(key, fastbound_id=f"fb-g3-{self._testMethodName}"), first)

	def test_the_record_and_the_gun_carry_the_ledger_serial(self):
		plain = self._gun(f"AL-{self._testMethodName[-8:]}")
		acq = self._acquire(plain)
		self.assertEqual(frappe.db.get_value("FFL Acquisition", acq, "ledger_serial"), plain)
		self.assertEqual(frappe.db.get_value("Serial No", plain, "ledger_serial"), plain)
		key = self._gun(f"NSN (FB A{self._testMethodName[-6:]})", "NSN")
		acq = self._acquire(key, ledger_serial="NSN")
		self.assertEqual(frappe.db.get_value("FFL Acquisition", acq, "ledger_serial"), "NSN")


class Backfill(_Guns):
	def test_backfill_fills_blanks_only_and_reruns_clean(self):
		from ffl_core.patches import backfill_ledger_serial

		plain = self._gun(f"BF-{self._testMethodName[-8:]}")
		acq = self._acquire(plain)
		frappe.db.set_value("FFL Acquisition", acq, "ledger_serial", "", update_modified=False)
		frappe.db.set_value("Serial No", plain, "ledger_serial", "", update_modified=False)
		tagged = self._gun(f"NONE MARKED (FB B{self._testMethodName[-6:]})", "NONE MARKED")
		for _ in range(2):
			backfill_ledger_serial.execute()
			self.assertEqual(frappe.db.get_value("Serial No", plain, "ledger_serial"), plain)
			self.assertEqual(frappe.db.get_value("FFL Acquisition", acq, "ledger_serial"), plain)
			self.assertEqual(frappe.db.get_value("Serial No", tagged, "ledger_serial"), "NONE MARKED")

	def test_an_ordinary_guns_fastbound_payload_is_unchanged_by_the_backfill(self):
		from ffl_core.patches import backfill_ledger_serial
		from ffl_integrations.fastbound.client import FastBoundClient

		plain = self._gun(f"BP-{self._testMethodName[-8:]}", manufacturer=None)
		acq = frappe.get_doc("FFL Acquisition", self._acquire(plain))
		frappe.db.set_value("Serial No", plain, "ledger_serial", "", update_modified=False)
		before = FastBoundClient._acquisition_payload(acq)
		backfill_ledger_serial.execute()
		after = FastBoundClient._acquisition_payload(acq)
		self.assertEqual(before, after)
		self.assertEqual(after["items"][0]["serial"], plain)
		self.assertEqual(after["items"][0]["externalId"], plain)


class Gates(_Guns):
	"""Temporary: a tagged gun is neither listed online nor sold until R2/R4."""

	def test_listing_gate(self):
		tagged = self._gun(f"NONE MARKED (FB W{self._testMethodName[-6:]})", "NONE MARKED")
		plain = self._gun(f"GW-{self._testMethodName[-8:]}", None)
		self.assertTrue(serials.listing_block_reason(tagged))
		self.assertIsNone(serials.listing_block_reason(plain))

	def test_woo_and_gunbroker_refuse_a_tagged_gun(self):
		from ffl_integrations.gunbroker import listings as gb
		from ffl_woo_sync.woocommerce import sync as woo

		tagged = self._gun(f"NSN (FB X{self._testMethodName[-6:]})", "NSN")
		res = woo.push_serial(tagged)
		self.assertEqual(res.get("skipped"), woo.TAGGED_SERIAL)
		refusal = gb.check_push_guards(tagged)
		self.assertEqual((refusal or {}).get("skipped"), gb.TAGGED_SERIAL)

	def test_sale_gate(self):
		tagged = self._gun(f"NONE MARKED (FB S{self._testMethodName[-6:]})", "NONE MARKED")
		plain = self._gun(f"GS-{self._testMethodName[-8:]}", None)

		def doc(serial_no, is_return=0):
			return frappe._dict(is_return=is_return, items=[frappe._dict(
				item_code=self.ITEM, serial_no=serial_no, serial_and_batch_bundle=None)])

		with self.assertRaisesRegex(frappe.ValidationError, "can't be sold yet"):
			serials.refuse_tagged_serial_sale(doc(tagged))
		serials.refuse_tagged_serial_sale(doc(plain))
		serials.refuse_tagged_serial_sale(doc(tagged, is_return=1))

	def test_delivery_note_gate(self):
		"""A shipment's paperwork goes to the receiving dealer: a tagged gun's key must
		not leave on it. A return DN and a DN of ordinary guns pass."""
		tagged = self._gun(f"NSN (FB D{self._testMethodName[-6:]})", "NSN")
		plain = self._gun(f"GD-{self._testMethodName[-8:]}", None)

		def dn(*serial_nos, is_return=0):
			return frappe._dict(doctype="Delivery Note", is_return=is_return, items=[
				frappe._dict(item_code=self.ITEM, serial_no=s, serial_and_batch_bundle=None)
				for s in serial_nos])

		with self.assertRaisesRegex(frappe.ValidationError, "can't be sold yet"):
			serials.refuse_tagged_serial_sale(dn(plain, tagged))
		serials.refuse_tagged_serial_sale(dn(plain))
		serials.refuse_tagged_serial_sale(dn(tagged, is_return=1))

	def test_sale_gate_is_wired_on_both_sales_doctypes(self):
		hooks = Path(frappe.get_app_path("ffl_core", "hooks.py")).read_text(encoding="utf-8")
		for doctype in ("Sales Invoice", "POS Invoice", "Delivery Note"):
			block = re.search(rf'\n\t"{doctype}": \{{(.*?)\n\t\}},', hooks, re.S)
			self.assertIsNotNone(block, doctype)
			self.assertIn("ffl_core.serials.refuse_tagged_serial_sale", block.group(1), doctype)


class LegacyPlaceholderNamedGun(_Guns):
	"""A store may already hold a gun received before this change whose Serial No is
	named exactly by a "no serial" spelling. After the backfill its name IS its ledger
	serial, so it is an ordinary, untagged gun: it sells, lists, corrects and disposes
	as before. Only receiving another such gun is refused."""

	CUSTOMER = "LS-TEST-CUSTOMER"

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		if not frappe.db.exists("Customer", cls.CUSTOMER):
			frappe.get_doc({"doctype": "Customer", "customer_name": cls.CUSTOMER,
				"customer_type": "Individual", "customer_group": "Individual",
				"territory": "All Territories"}).insert(ignore_permissions=True)

	def _legacy_gun(self, name: str) -> tuple[str, str]:
		"""Received the old way (no acquisition guard), then the upgrade's backfill."""
		from ffl_core.patches import backfill_ledger_serial

		self._gun(name)
		with patch("frappe.db.commit"):
			acq = frappe.get_doc({"doctype": "FFL Acquisition", "acquisition_date": now_datetime(),
				"source_type": "Individual", "source_name": "Pat Example",
				"source_id_type": "Driver License", "source_id_number": "DL-0000",
				"company": self.company, "item": self.ITEM, "serial": name, "cost": 100})
			acq.insert(ignore_permissions=True)
			acq.submit()
		frappe.db.set_value("FFL Acquisition", acq.name, "ledger_serial", "", update_modified=False)
		frappe.db.set_value("Serial No", name, {"ledger_serial": "", "fastbound_item_id": f"fb-{name}"},
			update_modified=False)
		backfill_ledger_serial.execute()
		self.assertEqual(frappe.db.get_value("Serial No", name, "ledger_serial"), name)
		self.assertEqual(frappe.db.get_value("FFL Acquisition", acq.name, "ledger_serial"), name)
		self.assertFalse(serials.is_tagged(name))
		return name, acq.name

	def test_it_sells_and_lists(self):
		from ffl_integrations.gunbroker import listings as gb

		gun, _acq = self._legacy_gun("NONE")
		serials.refuse_tagged_serial_sale(frappe._dict(is_return=0, items=[frappe._dict(
			item_code=self.ITEM, serial_no=gun, serial_and_batch_bundle=None)]))
		self.assertIsNone(serials.listing_block_reason(gun))
		self.assertNotEqual((gb.check_push_guards(gun) or {}).get("skipped"), gb.TAGGED_SERIAL)

	def test_it_corrects_like_an_ordinary_gun(self):
		from ffl_core.api.serial_correction import correct_serial

		gun, acq = self._legacy_gun("UNKNOWN")
		self.set_single("FastBound Settings", "enabled", 1)
		new = f"LG-{self._testMethodName[-8:]}"
		client = MagicMock()
		with patch("ffl_integrations.fastbound.client.FastBoundClient.from_settings",
				return_value=client), patch("frappe.enqueue"):
			res = correct_serial(gun, new, "serial found under the grip")
		self.assertEqual((res["new_serial"], res["ledger_serial"]), (new, new))
		self.assertFalse(frappe.db.exists("Serial No", gun))
		self.assertEqual(frappe.db.get_value("Serial No", new, "ledger_serial"), new)
		self.assertEqual(frappe.db.get_value("FFL Acquisition", acq, ["serial", "ledger_serial"]), (new, new))
		self.assertEqual(client.update_item_serial.call_args.args[:2], (f"fb-{gun}", new))

	def test_it_disposes_like_an_ordinary_gun(self):
		gun, _acq = self._legacy_gun("NO SERIAL")
		with patch("frappe.db.commit"):
			disp = frappe.get_doc({"doctype": "FFL Disposition", "disposition_date": now_datetime(),
				"disposition_type": "Retail Sale", "customer": self.CUSTOMER,
				"company": self.company, "item": self.ITEM, "serial": gun,
				"form_4473_status": "Completed", "nics_status": "Proceed",
				"fastbound_id": f"FB-DISP-{self._testMethodName[-8:]}"})
			disp.insert(ignore_permissions=True)
			disp.submit()
		self.assertEqual(disp.docstatus, 1)
		self.assertEqual(frappe.db.get_value("FFL Disposition", disp.name, "ledger_serial"), gun)

	def test_receiving_another_one_is_refused(self):
		gun, _acq = self._legacy_gun("NONE MARKED")
		with self.assertRaisesRegex(frappe.ValidationError, "can't be received in the POS yet"):
			self._acquire(gun, fastbound_id=f"fb-other-{self._testMethodName}")
