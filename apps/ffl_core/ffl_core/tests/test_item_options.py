"""Items that come in options (ffl_core.item_options): made in one Save, kept from the template.

Site tests on the bootstrap company: real Item Attributes, template and variant Items saved
through ERPNext's own controllers and our Item hooks. Nothing is mocked except one fault
injected to prove the create is all or nothing.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core import item_options as io
from ffl_core.item_hooks import _default_selling_price_list
from ffl_core.setup.preorder import AVAILABLE_FIELD, ETA_FIELD


def _token() -> str:
	return frappe.generate_hash(length=6).upper()


def _price_list_rate(item_code: str) -> float | None:
	return frappe.db.get_value("Item Price", {"item_code": item_code, "selling": 1,
		"price_list": _default_selling_price_list()}, "price_list_rate")


class _Options(FrappeTestCase):

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import preorder, test_bootstrap
		test_bootstrap.run()
		preorder.ensure()
		frappe.set_user("Administrator")

	def setUp(self):
		self.t = _token()
		self.color, self.size = f"_T Color {self.t}", f"_T Size {self.t}"
		self.code = f"_T-OPT-{self.t}"
		self.brand = f"_T Brand {self.t}"
		frappe.get_doc({"doctype": "Brand", "brand": self.brand}).insert(ignore_permissions=True)

	def _item(self, **extra) -> dict:
		return {"doctype": "Item", "item_code": self.code, "item_name": f"Test Grip {self.t}",
			"item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 1, "brand": self.brand,
			"description": "A grip.", "standard_rate": 55, "dealer_price": 20, "upc": "",
			"image_gallery": [{"image": "/files/_t_grip.jpg", "is_primary": 1}], **extra}

	def _options(self, colors=("Black", "Flat Dark Earth"), sizes=("S", "L")):
		return [{"name": self.color, "values": list(colors)}, {"name": self.size, "values": list(sizes)}]

	def _row(self, color, size, *, sku=None, price=60, cost=25, preorder=0, eta=None, include=1):
		return {"values": {self.color: color, self.size: size}, "include": include,
			"sku": sku or f"{self.code}-{color[:3].upper()}-{size}", "price": price, "cost": cost,
			"preorder": preorder, "eta": eta}

	def _create(self, rows=None, options=None, **item):
		rows = rows if rows is not None else [
			self._row("Black", "S", price=60, cost=25),
			self._row("Black", "L", price=65, cost=26, preorder=5, eta="2026-12-15"),
			self._row("Flat Dark Earth", "S", price=62),
			self._row("Flat Dark Earth", "L", include=0),
		]
		return io.create_item_with_options(self._item(**item), options or self._options(), rows)


class Create(_Options):

	def test_one_save_makes_the_options_the_template_and_each_ticked_row(self):
		out = self._create()
		self.assertEqual(out["template"], self.code)
		self.assertEqual(len(out["variants"]), 3)

		# Options: new Item Attributes, values in order, short codes unique per option.
		values = frappe.get_all("Item Attribute Value", filters={"parent": self.color},
			fields=["attribute_value", "abbr"], order_by="idx asc")
		self.assertEqual([(v.attribute_value, v.abbr) for v in values], [("Black", "BLACK"), ("Flat Dark Earth", "FDE")])
		self.assertEqual(frappe.get_all("Item Attribute Value", filters={"parent": self.size},
			order_by="idx asc", pluck="abbr"), ["S", "L"])

		template = frappe.get_doc("Item", self.code)
		self.assertEqual((template.has_variants, template.variant_based_on), (1, "Item Attribute"))
		self.assertEqual([a.attribute for a in template.attributes], [self.color, self.size])
		# The template is never sold itself: the form's price and cost are not its own.
		self.assertEqual((template.standard_rate, template.dealer_price), (0, 0))
		self.assertIsNone(_price_list_rate(self.code))

		black_l = frappe.get_doc("Item", f"{self.code}-BLA-L")
		self.assertEqual(black_l.variant_of, self.code)
		self.assertEqual(black_l.item_name, f"Test Grip {self.t} – Black / L")
		self.assertEqual({(a.attribute, a.attribute_value) for a in black_l.attributes},
			{(self.color, "Black"), (self.size, "L")})
		self.assertEqual((black_l.standard_rate, black_l.dealer_price), (65, 26))
		self.assertEqual(_price_list_rate(black_l.name), 65)
		self.assertEqual((black_l.get(AVAILABLE_FIELD), str(black_l.get(ETA_FIELD))), (5, "2026-12-15"))
		# Shared fields, copied once.
		self.assertEqual((black_l.item_group, black_l.brand, black_l.description, black_l.is_stock_item),
			("All Item Groups", self.brand, "A grip.", 1))
		self.assertEqual([r.image for r in black_l.image_gallery], ["/files/_t_grip.jpg"])
		self.assertEqual(black_l.image, "/files/_t_grip.jpg")
		black_s = frappe.get_doc("Item", f"{self.code}-BLA-S")
		self.assertEqual((black_s.get(AVAILABLE_FIELD), black_s.get(ETA_FIELD)), (0, None))
		# The unticked combination is not made.
		self.assertFalse(frappe.db.exists("Item", f"{self.code}-FLA-L"))

	def test_existing_options_and_values_are_reused_ignoring_case(self):
		frappe.get_doc({"doctype": "Item Attribute", "attribute_name": self.color, "item_attribute_values": [
			{"attribute_value": "Black", "abbr": "BLACK"}, {"attribute_value": "Blue", "abbr": "BLUE"}]},
		).insert(ignore_permissions=True)
		options = [{"name": self.color.lower(), "values": ["black", "Blackout"]}, {"name": self.size, "values": ["S"]}]
		rows = [{"values": {self.color.lower(): "black", self.size: "S"}, "sku": f"{self.code}-1", "price": 5},
			{"values": {self.color.lower(): "Blackout", self.size: "S"}, "sku": f"{self.code}-2", "price": 5}]
		io.create_item_with_options(self._item(), options, rows)
		values = frappe.get_all("Item Attribute Value", filters={"parent": self.color},
			fields=["attribute_value", "abbr"], order_by="idx asc")
		# "black" is the existing Black; Blackout is new, its code clear of BLACK.
		self.assertEqual([(v.attribute_value, v.abbr) for v in values],
			[("Black", "BLACK"), ("Blue", "BLUE"), ("Blackout", "BLAC2")])
		self.assertEqual(frappe.db.get_value("Item Variant Attribute",
			{"parent": f"{self.code}-1", "attribute": self.color}, "attribute_value"), "Black")
		self.assertEqual(frappe.get_all("Item Variant Attribute", filters={"parent": self.code}, order_by="idx asc", pluck="attribute"),
			[self.color, self.size])

	def test_the_dragged_order_is_the_storefront_order_and_other_values_keep_their_places(self):
		frappe.get_doc({"doctype": "Item Attribute", "attribute_name": self.color, "item_attribute_values": [
			{"attribute_value": v, "abbr": v.upper()} for v in ("Aa", "Bb", "Cc", "Dd")]}).insert(ignore_permissions=True)
		options = [{"name": self.color, "values": ["Dd", "Bb"]}]
		rows = [{"values": {self.color: c}, "sku": f"{self.code}-{c}", "price": 1} for c in ("Dd", "Bb")]
		io.create_item_with_options(self._item(), options, rows)
		self.assertEqual(frappe.get_all("Item Attribute Value", filters={"parent": self.color},
			order_by="idx asc", pluck="attribute_value"), ["Aa", "Dd", "Cc", "Bb"])

	def test_abbreviations(self):
		self.assertEqual(io.abbreviation("Flat Dark Earth", []), "FDE")
		self.assertEqual(io.abbreviation("17 Round", []), "17R")
		self.assertEqual(io.abbreviation("Large", []), "LARGE")
		self.assertEqual(io.abbreviation("Chocolate", []), "CHOCO")
		self.assertEqual(io.abbreviation("Black", ["black"]), "BLAC2")
		self.assertEqual(io.abbreviation("Black", ["BLACK", "BLAC2"]), "BLAC3")
		self.assertEqual(io.abbreviation("½", []), "V")

	def test_a_failure_part_way_leaves_nothing(self):
		real = io._insert_variant
		calls = []

		def fail_second(*args, **kwargs):
			calls.append(1)
			if len(calls) == 2:
				raise frappe.ValidationError("injected")
			return real(*args, **kwargs)

		with patch.object(io, "_insert_variant", side_effect=fail_second), \
				self.assertRaisesRegex(frappe.ValidationError, "injected"):
			self._create()
		self.assertFalse(frappe.db.exists("Item", self.code))
		self.assertFalse(frappe.db.exists("Item", f"{self.code}-BLA-S"))
		self.assertFalse(frappe.db.exists("Item Attribute", self.color))
		self.assertFalse(frappe.db.exists("Item Attribute", self.size))

	def test_every_bad_row_is_listed_and_nothing_is_written(self):
		taken = f"_T-TAKEN-{self.t}"
		frappe.get_doc({"doctype": "Item", "item_code": taken, "item_name": taken,
			"item_group": "All Item Groups", "stock_uom": "Nos"}).insert(ignore_permissions=True)
		rows = [
			self._row("Black", "S", sku="DUP-SKU-" + self.t),
			self._row("Black", "L", sku="dup-sku-" + self.t),          # same SKU ignoring case
			self._row("Flat Dark Earth", "S", sku=taken),             # an existing item
			self._row("Flat Dark Earth", "L", price=0),               # no price
			self._row("Black", "S", sku=f"{self.code}-X"),            # the same combination twice
		]
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._create(rows)
		message = str(ctx.exception)
		self.assertIn(f"SKU DUP-SKU-{self.t} is used by Black / S and Black / L", message.replace("dup-sku", "DUP-SKU"))
		self.assertIn(f"Flat Dark Earth / S: SKU {taken} is already an item", message)
		self.assertIn("Flat Dark Earth / L: enter a price", message)
		self.assertIn("Black / S is listed twice", message)
		self.assertFalse(frappe.db.exists("Item", self.code))
		self.assertFalse(frappe.db.exists("Item Attribute", self.color))

	def test_trust_boundary(self):
		with self.assertRaisesRegex(frappe.ValidationError, "may only use"):
			self._create([self._row("Black", "S", sku="BAD SKU")])
		with self.assertRaisesRegex(frappe.ValidationError, "can't contain"):
			self._create([self._row("Black", "S")], options=[{"name": "<b>Color", "values": ["Black"]},
				{"name": self.size, "values": ["S"]}])
		with self.assertRaisesRegex(frappe.ValidationError, "listed twice"):
			self._create([self._row("Black", "S")], options=self._options(colors=("Black", "BLACK")))
		with self.assertRaisesRegex(frappe.ValidationError, "can't be negative"):
			self._create([self._row("Black", "S", cost=-1)])
		with self.assertRaisesRegex(frappe.ValidationError, "whole number"):
			self._create([self._row("Black", "S", preorder=1.5)])
		with self.assertRaisesRegex(frappe.ValidationError, "not one of its values"):
			self._create([self._row("Red", "S")])
		with self.assertRaisesRegex(frappe.ValidationError, "at most 3 options"):
			self._create([self._row("Black", "S")], options=self._options() + [
				{"name": f"_T A {self.t}", "values": ["x"]}, {"name": f"_T B {self.t}", "values": ["y"]}])
		self.assertFalse(frappe.db.exists("Item", self.code))

	def test_a_gun_priced_one_by_one_cannot_come_in_options(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Uniform Pricing"):
			self._create(item_group="Pistol")
		self.assertFalse(frappe.db.exists("Item", self.code))
		self.assertFalse(frappe.db.exists("Item Attribute", self.color))


class Panel(_Options):

	def setUp(self):
		super().setUp()
		self._create()
		self.bs, self.bl, self.fs = (f"{self.code}-BLA-S", f"{self.code}-BLA-L", f"{self.code}-FLA-S")

	def test_the_state_lists_options_and_rows(self):
		state = io.item_options_state(self.code)
		self.assertEqual([(o["name"], o["values"]) for o in state["options"]],
			[(self.color, ["Black", "Flat Dark Earth"]), (self.size, ["S", "L"])])
		self.assertEqual(state["options"][0]["abbr"]["Flat Dark Earth"], "FDE")
		rows = {r["item_code"]: r for r in state["rows"]}
		self.assertEqual(set(rows), {self.bs, self.bl, self.fs})
		self.assertEqual((rows[self.bl]["price"], rows[self.bl]["cost"], rows[self.bl]["preorder"], rows[self.bl]["eta"]),
			(65, 26, 5, "2026-12-15"))

	def test_a_row_edit_is_an_item_save_and_moves_the_register_price(self):
		io.update_option_rows(self.code, [{"item_code": self.bs, "price": 70, "cost": 30, "preorder": 2,
			"eta": "2027-01-10"}])
		doc = frappe.get_doc("Item", self.bs)
		self.assertEqual((doc.standard_rate, doc.dealer_price, doc.get(AVAILABLE_FIELD), str(doc.get(ETA_FIELD))),
			(70, 30, 2, "2027-01-10"))
		self.assertEqual(_price_list_rate(self.bs), 70)
		# The other rows are untouched.
		self.assertEqual(frappe.db.get_value("Item", self.bl, "standard_rate"), 65)

	def test_a_new_value_adds_only_its_combinations(self):
		before = set(io._all_variants(self.code))
		options = self._options(colors=("Black", "Flat Dark Earth", "OD Green"))
		out = io.update_option_rows(self.code, [
			{"values": {self.color: "OD Green", self.size: "S"}, "sku": f"{self.code}-ODG-S", "price": 61},
			{"values": {self.color: "OD Green", self.size: "L"}, "sku": f"{self.code}-ODG-L", "price": 0, "include": 0},
		], options)
		self.assertEqual(out["created"], [f"{self.code}-ODG-S"])
		self.assertEqual(set(io._all_variants(self.code)) - before, {f"{self.code}-ODG-S"})
		self.assertEqual(frappe.db.get_value("Item Attribute Value", {"parent": self.color, "attribute_value": "OD Green"},
			"abbr"), "OG")
		self.assertIn("OD Green", out["options"][0]["values"])
		# An existing combination (enabled or not) can't be made again.
		with self.assertRaisesRegex(frappe.ValidationError, f"Black / S is already {self.bs}"):
			io.update_option_rows(self.code, [{"values": {self.color: "Black", self.size: "S"},
				"sku": f"{self.code}-NEW", "price": 5}], options)
		with self.assertRaisesRegex(frappe.ValidationError, "can't be renamed"):
			io.update_option_rows(self.code, [{"values": {self.color: "Black"}, "sku": "X1", "price": 5}],
				[{"name": self.color, "values": ["Black"]}])

	def test_unticking_disables_never_deletes(self):
		io.update_option_rows(self.code, [{"item_code": self.fs, "include": 0}])
		self.assertEqual(frappe.db.get_value("Item", self.fs, "disabled"), 1)
		self.assertNotIn(self.fs, [r["item_code"] for r in io.item_options_state(self.code)["rows"] if not r["disabled"]])
		io.update_option_rows(self.code, [{"item_code": self.fs, "include": 1}])
		self.assertEqual(frappe.db.get_value("Item", self.fs, "disabled"), 0)
		# A row on sale needs a price; a row switched off does not.
		with self.assertRaisesRegex(frappe.ValidationError, "enter a price"):
			io.update_option_rows(self.code, [{"item_code": self.fs, "price": 0}])
		io.update_option_rows(self.code, [{"item_code": self.fs, "include": 0, "price": 0}])
		self.assertEqual(frappe.db.get_value("Item", self.fs, ["disabled", "standard_rate"]), (1, 0))

	def test_a_row_of_another_item_is_refused(self):
		other = f"_T-OTHER-{self.t}"
		frappe.get_doc({"doctype": "Item", "item_code": other, "item_name": other,
			"item_group": "All Item Groups", "stock_uom": "Nos", "standard_rate": 9}).insert(ignore_permissions=True)
		with self.assertRaisesRegex(frappe.ValidationError, "not one of this item's options"):
			io.update_option_rows(self.code, [{"item_code": other, "price": 1}])

	def test_saving_the_template_never_rewrites_variant_prices(self):
		# A store whose Item Variant Settings copy prices to variants on every template save.
		settings = frappe.get_single("Item Variant Settings")
		settings.do_not_update_variants = 0
		for field in ("standard_rate", "dealer_price", "map_price", "description"):
			if field not in [f.field_name for f in settings.fields]:
				settings.append("fields", {"field_name": field})
		settings.save()
		template = frappe.get_doc("Item", self.code)
		template.description = "A better grip."
		template.map_price = 1
		template.save()
		self.assertEqual(frappe.db.get_value("Item", self.bl, ["standard_rate", "dealer_price", "description"]),
			(65, 26, "A grip."))


class Group(_Options):

	def setUp(self):
		super().setUp()
		self._create(rows=[self._row("Black", "S")])
		self.plain = [f"_T-PLAIN-{self.t}-{i}" for i in range(2)]
		for code in self.plain:
			frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": "All Item Groups",
				"stock_uom": "Nos", "is_stock_item": 1, "standard_rate": 40}).insert(ignore_permissions=True)

	def test_existing_items_join_with_their_codes(self):
		out = io.group_items(self.code, {self.plain[0]: {self.color: "Black", self.size: "L"},
			self.plain[1]: {self.color: "Flat Dark Earth", self.size: "S"}})
		self.assertEqual((out["ok"], out["variants"]), (True, sorted(self.plain)))
		doc = frappe.get_doc("Item", self.plain[1])
		self.assertEqual(doc.variant_of, self.code)
		self.assertEqual({(a.attribute, a.attribute_value) for a in doc.attributes},
			{(self.color, "Flat Dark Earth"), (self.size, "S")})
		doc.save()  # ERPNext's own variant validation accepts it
		self.assertEqual(len(io.item_options_state(self.code)["rows"]), 3)

	def test_the_shared_checks_refuse_and_write_nothing(self):
		out = io.group_items(self.code, {self.plain[0]: {self.color: "Black", self.size: "S"},
			self.plain[1]: {self.color: "Purple", self.size: "S"}})
		self.assertFalse(out["ok"])
		self.assertIn(f"{self.plain[0]}: same attributes as {self.code}-BLA-S", out["errors"])
		self.assertTrue(any("'Purple'" in e for e in out["errors"]))
		self.assertFalse(frappe.db.get_value("Item", self.plain[0], "variant_of"))
		# A row switched off frees its values for grouping; it can't come back while they are taken.
		io.update_option_rows(self.code, [{"item_code": f"{self.code}-BLA-S", "include": 0}])
		self.assertTrue(io.group_items(self.code, {self.plain[0]: {self.color: "Black", self.size: "S"}})["ok"])
		with self.assertRaisesRegex(frappe.ValidationError, f"same options as {self.plain[0]}"):
			io.update_option_rows(self.code, [{"item_code": f"{self.code}-BLA-S", "include": 1}])
		self.assertIn(f"{self.code}: is the template itself",
			io.group_items(self.code, {self.code: {self.color: "Black", self.size: "L"}})["errors"])


class Suggestions(_Options):

	def test_names_then_values(self):
		self._create(rows=[self._row("Black", "S")])
		self.assertIn({"value": self.color}, io.option_suggestions(txt=f"Color {self.t}"))
		self.assertEqual(io.option_suggestions(self.color, "dark"), [{"value": "Flat Dark Earth", "abbr": "FDE"}])
		self.assertEqual(io.option_suggestions(f"_T Nope {self.t}", ""), [])

	def test_codes_the_form_builds_skus_from(self):
		self._create(rows=[self._row("Black", "S")])
		# Existing values keep their codes; new ones get the codes a save would give them.
		self.assertEqual(io.option_codes(self.color, ["Flat Dark Earth", "Blackish", "Blackberry"]),
			{"Flat Dark Earth": "FDE", "Blackish": "BLAC2", "Blackberry": "BLAC3"})
		self.assertEqual(io.option_codes(f"_T New {self.t}", ["Red", "red"]), {"Red": "RED", "red": "RED"})
		self.assertEqual(io.taken_item_codes([f"{self.code}-bla-s", "_T-FREE-" + self.t]), [f"{self.code}-bla-s"])

	def test_a_reorder_alone_is_saved(self):
		self._create(rows=[self._row("Black", "S"), self._row("Flat Dark Earth", "S")])
		io.update_option_rows(self.code, [], [{"name": self.color, "values": ["Flat Dark Earth", "Black"]},
			{"name": self.size, "values": ["S", "L"]}])
		self.assertEqual(io.item_options_state(self.code)["options"][0]["values"], ["Flat Dark Earth", "Black"])
