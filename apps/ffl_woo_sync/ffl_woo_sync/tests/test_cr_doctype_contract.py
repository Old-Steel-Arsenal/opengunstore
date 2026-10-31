"""Woo Online Order schema contract for the C&R (Type 03) fields.

Pure test — reads the doctype JSON off disk, no frappe, no site. It guards two
things that a site test cannot cheaply catch:

1. **fields[] and field_order[] must agree.** Frappe renders from
   `field_order`; a field present in `fields` but missing from `field_order`
   simply never appears in the form. The C&R verification block is entered by
   staff at dispose time — an invisible field there means the operator cannot
   see what they are attesting to.
2. **The derived flags stay read-only.** `is_cr_transfer` is derived from the
   licence number (segment 4 == 03) and `woo_ffl_rev` is the Woo-side
   monotonic signal. If either becomes hand-editable, the "single source of
   truth is the licence number" property in ffl_number.py is silently void and
   a firearm can be routed down the collector path by a typo in a form.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

_DOCTYPE_JSON = (Path(__file__).resolve().parents[1]
	/ "ffl_woo_sync" / "doctype" / "woo_online_order" / "woo_online_order.json")

# The 9 staff-entered verification fields, frozen as contract C5.
CR_VERIFICATION_FIELDS = [
	"cr_contact_name", "cr_license_expiry", "cr_premise_street",
	"cr_premise_street2", "cr_premise_city", "cr_premise_state",
	"cr_premise_zip", "cr_verified_by", "cr_verified_at",
]


def _load():
	return json.loads(_DOCTYPE_JSON.read_text())


class FieldOrderIntegrityTest(unittest.TestCase):
	"""Pre-existing invariant — asserted so the C&R additions can't break it."""

	def setUp(self):
		self.data = _load()
		self.fields = [f["fieldname"] for f in self.data["fields"]]
		self.order = self.data["field_order"]

	def test_every_field_is_rendered(self):
		missing = [f for f in self.fields if f not in self.order]
		self.assertEqual(missing, [], f"fields[] entries absent from field_order[]: {missing}")

	def test_field_order_has_no_phantoms(self):
		phantom = [f for f in self.order if f not in self.fields]
		self.assertEqual(phantom, [], f"field_order[] names with no field definition: {phantom}")

	def test_no_duplicate_fieldnames(self):
		dupes = {f for f in self.fields if self.fields.count(f) > 1}
		self.assertEqual(dupes, set())


class CrFieldsPresentTest(unittest.TestCase):
	def setUp(self):
		self.data = _load()
		self.by_name = {f["fieldname"]: f for f in self.data["fields"]}
		self.order = self.data["field_order"]

	def test_is_cr_transfer_is_a_readonly_check(self):
		f = self.by_name.get("is_cr_transfer")
		self.assertIsNotNone(f, "is_cr_transfer missing")
		self.assertEqual(f["fieldtype"], "Check")
		self.assertEqual(f.get("read_only"), 1, "derived from the licence number — not hand-set")
		self.assertEqual(f.get("default"), "0")

	def test_woo_ffl_rev_is_a_readonly_int(self):
		f = self.by_name.get("woo_ffl_rev")
		self.assertIsNotNone(f, "woo_ffl_rev missing")
		self.assertEqual(f["fieldtype"], "Int")
		self.assertEqual(f.get("read_only"), 1)
		self.assertEqual(f.get("no_copy"), 1)
		self.assertEqual(f.get("default"), "0")

	def test_cr_flags_sit_with_the_ffl_block(self):
		"""Placed right after ffl_license, so the flag is read next to the number
		it is derived from rather than in an unrelated part of the form."""
		self.assertEqual(
			self.order[self.order.index("ffl_license") + 1:self.order.index("ffl_license") + 3],
			["is_cr_transfer", "woo_ffl_rev"],
		)

	def test_verification_section_exists_and_is_conditional(self):
		sec = self.by_name.get("section_cr")
		self.assertIsNotNone(sec, "section_cr missing")
		self.assertEqual(sec["fieldtype"], "Section Break")
		self.assertEqual(sec.get("depends_on"), "eval:doc.is_cr_transfer")

	def test_all_nine_verification_fields_exist_and_render(self):
		for name in CR_VERIFICATION_FIELDS:
			with self.subTest(field=name):
				self.assertIn(name, self.by_name, f"{name} missing from fields[]")
				self.assertIn(name, self.order, f"{name} missing from field_order[]")

	def test_verification_fields_follow_the_section(self):
		# A Column Break inside the section is layout only; the nine fields still
		# follow the section in order, before the next Section Break.
		start = self.order.index("section_cr")
		section = []
		for name in self.order[start + 1:]:
			fieldtype = self.by_name[name]["fieldtype"]
			if fieldtype in ("Section Break", "Tab Break"):
				break
			if fieldtype != "Column Break":
				section.append(name)
		self.assertEqual(section, CR_VERIFICATION_FIELDS)

	def test_expiry_is_a_real_date_not_free_text(self):
		"""The expiry drives the expired-licence gate; a Data field would let
		"next year" through and make the comparison meaningless."""
		self.assertEqual(self.by_name["cr_license_expiry"]["fieldtype"], "Date")

	def test_audit_stamps_are_readonly_and_not_copied(self):
		for name in ("cr_verified_by", "cr_verified_at"):
			with self.subTest(field=name):
				f = self.by_name[name]
				self.assertEqual(f.get("read_only"), 1)
				self.assertEqual(f.get("no_copy"), 1, "an audit stamp must not ride along on a copy")
		self.assertEqual(self.by_name["cr_verified_by"]["fieldtype"], "Link")
		self.assertEqual(self.by_name["cr_verified_by"]["options"], "User")
		self.assertEqual(self.by_name["cr_verified_at"]["fieldtype"], "Datetime")

	def test_state_and_zip_are_length_bounded(self):
		self.assertEqual(self.by_name["cr_premise_state"].get("length"), 2)
		self.assertEqual(self.by_name["cr_premise_zip"].get("length"), 10)


if __name__ == "__main__":
	unittest.main()
