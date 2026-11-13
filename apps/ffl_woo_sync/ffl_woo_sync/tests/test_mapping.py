"""Unit tests for woocommerce/mapping.py.

Focus: the **pure builders** ``build_simple_product_payload`` and
``build_serial_product_payload``.  These functions accept plain Python values
and have zero DB / HTTP calls, so all tests run without a live Frappe site
or WooCommerce store.

Thin loaders (``item_to_product_payload``, ``serial_to_product_payload``,
``stock_for_item``) are covered structurally via the pure-builder tests; a
small suite of loader tests is included using lightweight mocking of the
Frappe DB layer.

Shapes under test
-----------------
1. Non-firearm item          — build_simple_product_payload
2. Uniform-price firearm     — build_simple_product_payload
3. Per-serial firearm        — build_serial_product_payload
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce.mapping import (
    _CALIBER_CANONICAL,
    _MODEL_CANONICAL,
    _canonical_caliber,
    _canonical_model,
    _looks_like_a_carbine,
    _norm_model_key,
    _pa_condition,
    _publicize_file,
    _publicize_listing_images,
    _resolve_image,
    _resolve_image_list,
    _to_highres,
    _wc_attributes,
    build_serial_product_payload,
    build_simple_product_payload,
    item_to_product_payload,
    serial_to_product_payload,
    stock_for_item,
)


# ===========================================================================
# _pa_condition helper
# ===========================================================================

class TestPaCondition(FrappeTestCase):
    """``is_used or is_antique`` decides whether a grade is believed at all."""

    TIERS = ("New", "Excellent", "Very Good", "Good", "Fair")

    def test_not_used_is_new_whatever_the_grade(self):
        # The production shape: the Select used to default to "Very Good", so
        # brand-new is_used=0 guns carry a grade nobody chose. It is ignored.
        for grade in (*self.TIERS, "", None, "   ", "Grade A"):
            with self.subTest(grade=grade):
                self.assertEqual(_pa_condition(grade, is_used=False), "New")

    def test_used_with_valid_grade_is_that_tier(self):
        for grade in self.TIERS:
            with self.subTest(grade=grade):
                self.assertEqual(_pa_condition(grade, is_used=True), grade)

    def test_used_grade_is_case_and_space_tolerant(self):
        for raw, want in (
            ("very good", "Very Good"), ("  VERY   GOOD ", "Very Good"),
            ("good", "Good"), ("FAIR", "Fair"), (" excellent\t", "Excellent"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(_pa_condition(raw, is_used=True), want)

    def test_used_ungraded_publishes_no_condition(self):
        for grade in ("", None, "   "):
            with self.subTest(grade=grade):
                self.assertEqual(_pa_condition(grade, is_used=True), "")

    def test_used_dirty_grade_publishes_no_condition(self):
        for bad in ("Grade A", "Like New", "very-good", "99%"):
            with self.subTest(bad=bad):
                self.assertEqual(_pa_condition(bad, is_used=True), "")

    def test_antique_is_never_new(self):
        # A pre-1899 gun with is_used=0 (the data gap) is treated as used.
        self.assertEqual(_pa_condition("Good", is_used=False, is_antique=True), "Good")
        self.assertEqual(_pa_condition("very good", is_used=False, is_antique=True), "Very Good")
        self.assertEqual(_pa_condition("", is_used=False, is_antique=True), "")
        self.assertEqual(_pa_condition(None, is_used=False, is_antique=True), "")
        self.assertEqual(_pa_condition("Grade A", is_used=False, is_antique=True), "")

    def test_is_antique_defaults_false(self):
        self.assertEqual(_pa_condition("Good", is_used=False), "New")


# ===========================================================================
# build_simple_product_payload — non-firearm
# ===========================================================================

class TestBuildSimpleNonFirearm(FrappeTestCase):
    def _payload(self, **kwargs):
        defaults = dict(
            item_code="ACME-001",
            item_name="Acme Widget",
            description="A widget",
            standard_rate=49.99,
            stock_qty=10,
            is_firearm=False,
            is_used=False,
            condition_grade="",
            caliber="",
            item_group="Accessories",
            image_srcs=[],
        )
        defaults.update(kwargs)
        return build_simple_product_payload(**defaults)

    def test_sku_equals_item_code(self):
        self.assertEqual(self._payload()["sku"], "ACME-001")

    def test_type_is_simple(self):
        self.assertEqual(self._payload()["type"], "simple")

    def test_status_is_publish(self):
        self.assertEqual(self._payload()["status"], "publish")

    def test_price_formatted_two_decimals(self):
        self.assertEqual(self._payload()["regular_price"], "49.99")

    def test_stock_quantity_passed_through(self):
        self.assertEqual(self._payload(stock_qty=7)["stock_quantity"], 7)

    def test_negative_stock_clamped_to_zero(self):
        self.assertEqual(self._payload(stock_qty=-5)["stock_quantity"], 0)

    def test_manage_stock_true(self):
        self.assertTrue(self._payload()["manage_stock"])

    def test_firearm_product_meta_is_no(self):
        meta = {m["key"]: m["value"] for m in self._payload()["meta_data"]}
        self.assertEqual(meta["_firearm_product"], "no")

    def test_ammunition_product_meta_is_no(self):
        meta = {m["key"]: m["value"] for m in self._payload()["meta_data"]}
        self.assertEqual(meta["_ammunition_product"], "no")

    def test_no_condition_or_caliber_attributes(self):
        self.assertNotIn("attributes", self._payload())

    def test_image_included_when_provided(self):
        p = self._payload(image_srcs=["https://example.com/img.jpg"])
        self.assertIn("images", p)
        self.assertEqual(p["images"][0]["src"], "https://example.com/img.jpg")

    def test_no_images_key_when_empty(self):
        self.assertNotIn("images", self._payload(image_srcs=[]))

    def test_multiple_images_in_order(self):
        urls = ["https://example.com/a.jpg", "https://example.com/b.jpg"]
        p = self._payload(image_srcs=urls)
        self.assertEqual([i["src"] for i in p["images"]], urls)


# ===========================================================================
# condition_grade has no field default any more
# ===========================================================================

class TestNoConditionGradeDefault(FrappeTestCase):
    """The Select used to default to "Very Good" on every new Item / Serial No,
    which is why 39 of 40 recent is_used=0 firearm Items carried a grade nobody
    chose. Dropping ``default`` is NOT enough on its own: Frappe fills a Select
    that has no default with its FIRST option (``create_new.get_default_value``,
    reached by every insert via ``_set_defaults``), which would have stamped
    "New" on every used gun. So the options also start with a blank line, making
    "no grade" the first option and the default."""

    def test_fixture_declares_no_default_and_a_blank_first_option(self):
        import json
        from pathlib import Path
        path = Path(frappe.get_app_path("ffl_core")) / "fixtures" / "custom_field.json"
        fields = {f["name"]: f for f in json.loads(path.read_text())
                  if f.get("fieldname") == "condition_grade"}
        self.assertEqual(set(fields), {"Item-condition_grade", "Serial No-condition_grade"})
        for name, f in fields.items():
            with self.subTest(field=name):
                self.assertFalse(f.get("default"))
                self.assertEqual(
                    f["options"], "\nNew\nExcellent\nVery Good\nGood\nFair")

    def test_new_docs_carry_no_grade(self):
        self.assertFalse(frappe.new_doc("Item").get("condition_grade"))
        self.assertFalse(frappe.new_doc("Serial No").get("condition_grade"))

    def _serial_payload(self, *, is_used):
        from ffl_woo_sync.woocommerce.mapping import _serial_attribute_inputs
        item = frappe.new_doc("Item")
        item.update({"is_used": is_used, "caliber": "9mm"})
        serial = frappe.new_doc("Serial No")
        return build_serial_product_payload(
            item_code="X", serial_no="Y", item_name="X",
            **_serial_attribute_inputs(serial, item))

    def test_new_gun_created_without_a_grade_publishes_new(self):
        slugs = {a["slug"]: a["options"][0] for a in self._serial_payload(is_used=0)["attributes"]}
        self.assertEqual(slugs["pa_condition"], "New")

    def test_used_gun_created_without_a_grade_publishes_no_condition(self):
        slugs = {a["slug"] for a in self._serial_payload(is_used=1)["attributes"]}
        self.assertNotIn("pa_condition", slugs)


# ===========================================================================
# build_simple_product_payload — uniform-price firearm
# ===========================================================================

class TestBuildSimpleUniformFirearm(FrappeTestCase):
    def _payload(self, **kwargs):
        defaults = dict(
            item_code="GLOCK-19",
            item_name="Glock 19 Gen5",
            description="Polymer pistol",
            standard_rate=599.0,
            stock_qty=3,
            is_firearm=True,
            is_used=False,
            condition_grade="",
            caliber="9mm Luger",
            item_group="Handgun",
            image_srcs=[],
        )
        defaults.update(kwargs)
        return build_simple_product_payload(**defaults)

    def test_firearm_product_meta_is_yes(self):
        meta = {m["key"]: m["value"] for m in self._payload()["meta_data"]}
        self.assertEqual(meta["_firearm_product"], "yes")

    def test_ammunition_product_meta_is_no_for_firearm(self):
        meta = {m["key"]: m["value"] for m in self._payload()["meta_data"]}
        self.assertEqual(meta["_ammunition_product"], "no")

    def test_condition_attribute_is_new(self):
        slugs = {a["slug"]: a["options"][0] for a in self._payload()["attributes"]}
        self.assertEqual(slugs["pa_condition"], "New")

    def test_condition_attribute_from_grade_for_used_firearm(self):
        slugs = {a["slug"]: a["options"][0] for a in
                 self._payload(is_used=True, condition_grade="Good")["attributes"]}
        self.assertEqual(slugs["pa_condition"], "Good")

    def test_not_used_ignores_a_stale_default_grade(self):
        # is_used=0 + the old "Very Good" default (prod shape) must stay New.
        slugs = {a["slug"]: a["options"][0] for a in
                 self._payload(is_used=False, condition_grade="Very Good")["attributes"]}
        self.assertEqual(slugs["pa_condition"], "New")

    def test_used_blank_grade_sends_no_condition_attribute(self):
        # Omitted from the authoritative list => WooCommerce unsets pa_condition.
        p = self._payload(is_used=True, condition_grade="")
        slugs = {a["slug"] for a in p["attributes"]}
        self.assertNotIn("pa_condition", slugs)
        self.assertIn("pa_caliber", slugs)

    def test_used_blank_grade_and_no_other_attrs_still_sends_empty_list(self):
        # `attributes` must be PRESENT (empty) or WooCommerce keeps the old term.
        p = self._payload(is_used=True, condition_grade="", caliber="")
        self.assertEqual(p["attributes"], [])

    def test_antique_not_flagged_used_is_never_new(self):
        graded = {a["slug"]: a["options"][0] for a in self._payload(
            is_used=False, is_antique=True, condition_grade="Good")["attributes"]}
        self.assertEqual(graded["pa_condition"], "Good")
        blank = self._payload(is_used=False, is_antique=True, condition_grade="")
        self.assertNotIn("pa_condition", {a["slug"] for a in blank["attributes"]})

    def test_caliber_attribute(self):
        slugs = {a["slug"]: a["options"][0] for a in self._payload()["attributes"]}
        self.assertEqual(slugs["pa_caliber"], "9mm Luger")

    def test_no_caliber_attribute_when_blank(self):
        p = self._payload(caliber="")
        slugs = {a["slug"] for a in p.get("attributes", [])}
        self.assertNotIn("pa_caliber", slugs)

    def test_price_zero_formatted(self):
        p = self._payload(standard_rate=0.0)
        self.assertEqual(p["regular_price"], "0.00")


# ===========================================================================
# build_simple_product_payload — ammunition
# ===========================================================================

class TestBuildSimpleAmmunition(FrappeTestCase):
    def _payload(self, **kwargs):
        defaults = dict(
            item_code="9MM-50",
            item_name="9mm 115gr FMJ 50-round",
            standard_rate=24.99,
            stock_qty=500,
            is_firearm=False,
            is_used=False,
            caliber="9mm Luger",
            item_group="Pistol Ammo",  # an AMMO_LEAVES group
            image_srcs=[],
        )
        defaults.update(kwargs)
        return build_simple_product_payload(**defaults)

    def test_ammunition_product_meta_is_yes(self):
        meta = {m["key"]: m["value"] for m in self._payload()["meta_data"]}
        self.assertEqual(meta["_ammunition_product"], "yes")

    def test_firearm_product_meta_is_no_for_ammo(self):
        meta = {m["key"]: m["value"] for m in self._payload()["meta_data"]}
        self.assertEqual(meta["_firearm_product"], "no")

    def test_caliber_attribute(self):
        slugs = {a["slug"]: a["options"][0] for a in self._payload().get("attributes", [])}
        self.assertEqual(slugs["pa_caliber"], "9mm Luger")

    def test_no_caliber_attribute_when_blank(self):
        p = self._payload(caliber="")
        self.assertNotIn("attributes", p)

    def test_ammo_parent_group_also_detected(self):
        """item_group='Ammunition' (parent) also counts as ammo."""
        p = self._payload(item_group="Ammunition", caliber="9mm Luger")
        meta = {m["key"]: m["value"] for m in p["meta_data"]}
        self.assertEqual(meta["_ammunition_product"], "yes")


# ===========================================================================
# build_serial_product_payload
# ===========================================================================

class TestBuildSerialProduct(FrappeTestCase):
    def _payload(self, **kwargs):
        defaults = dict(
            item_code="GLOCK-19",
            serial_no="GXK12345",
            item_name="Glock 19 Gen5",
            description="",
            sell_price=699.99,
            is_used=True,
            condition_grade="Excellent",
            caliber="9mm Luger",
            woo_reserved=False,
            is_active=True,
            image_srcs=[],
        )
        defaults.update(kwargs)
        return build_serial_product_payload(**defaults)

    def test_sku_uses_double_colon_separator(self):
        self.assertEqual(self._payload()["sku"], "GLOCK-19::GXK12345")

    def test_stock_1_when_active_and_not_reserved(self):
        self.assertEqual(self._payload(woo_reserved=False, is_active=True)["stock_quantity"], 1)

    def test_stock_0_when_reserved(self):
        self.assertEqual(self._payload(woo_reserved=True, is_active=True)["stock_quantity"], 0)

    def test_stock_0_when_not_active(self):
        self.assertEqual(self._payload(woo_reserved=False, is_active=False)["stock_quantity"], 0)

    def test_price_from_sell_price(self):
        self.assertEqual(self._payload(sell_price=749.99)["regular_price"], "749.99")

    def test_price_zero_when_none(self):
        self.assertEqual(self._payload(sell_price=None)["regular_price"], "0.00")

    def test_type_is_simple(self):
        self.assertEqual(self._payload()["type"], "simple")

    def test_firearm_product_meta_is_yes(self):
        meta = {m["key"]: m["value"] for m in self._payload()["meta_data"]}
        self.assertEqual(meta["_firearm_product"], "yes")

    def test_ammunition_product_meta_is_no(self):
        meta = {m["key"]: m["value"] for m in self._payload()["meta_data"]}
        self.assertEqual(meta["_ammunition_product"], "no")

    def test_serial_meta_key_in_meta(self):
        meta = {m["key"]: m["value"] for m in self._payload()["meta_data"]}
        self.assertEqual(meta["_osa_serial"], "GXK12345")

    def test_condition_attribute_from_grade_for_used(self):
        slugs = {a["slug"]: a["options"][0] for a in
                 self._payload(is_used=True, condition_grade="Fair")["attributes"]}
        self.assertEqual(slugs["pa_condition"], "Fair")

    def test_non_used_is_new_whatever_the_serial_grade(self):
        slugs = {a["slug"]: a["options"][0]
                 for a in self._payload(is_used=False, condition_grade="Excellent")["attributes"]}
        self.assertEqual(slugs["pa_condition"], "New")

    def test_no_condition_attribute_when_used_blank_grade(self):
        slugs = {a["slug"] for a in
                 self._payload(is_used=True, condition_grade="")["attributes"]}
        self.assertNotIn("pa_condition", slugs)
        self.assertIn("pa_caliber", slugs)

    def test_no_condition_attribute_when_used_dirty_grade(self):
        slugs = {a["slug"] for a in
                 self._payload(is_used=True, condition_grade="Grade A")["attributes"]}
        self.assertNotIn("pa_condition", slugs)

    def test_ungraded_new_stock_is_new(self):
        slugs = {a["slug"]: a["options"][0] for a in
                 self._payload(is_used=False, condition_grade="")["attributes"]}
        self.assertEqual(slugs["pa_condition"], "New")

    def test_antique_not_flagged_used_is_never_new(self):
        graded = {a["slug"]: a["options"][0] for a in self._payload(
            is_used=False, is_antique=True, condition_grade="Fair")["attributes"]}
        self.assertEqual(graded["pa_condition"], "Fair")
        blank = self._payload(is_used=False, is_antique=True, condition_grade="")
        self.assertNotIn("pa_condition", {a["slug"] for a in blank["attributes"]})

    def test_empty_condition_still_sends_attributes_list_to_clear(self):
        p = self._payload(is_used=True, condition_grade="", caliber="")
        self.assertEqual(p["attributes"], [])

    def test_caliber_attribute_present(self):
        slugs = {a["slug"]: a["options"][0] for a in self._payload()["attributes"]}
        self.assertEqual(slugs["pa_caliber"], "9mm Luger")

    def test_no_caliber_attribute_when_blank(self):
        slugs = {a["slug"] for a in self._payload(caliber="").get("attributes", [])}
        self.assertNotIn("pa_caliber", slugs)

    def test_condition_attribute_present_when_graded(self):
        slugs = {a["slug"] for a in self._payload(caliber="")["attributes"]}
        self.assertIn("pa_condition", slugs)

    def test_image_included_when_provided(self):
        p = self._payload(image_srcs=["https://example.com/gun.jpg"])
        self.assertIn("images", p)
        self.assertEqual(p["images"][0]["src"], "https://example.com/gun.jpg")

    def test_no_images_key_when_empty(self):
        self.assertNotIn("images", self._payload(image_srcs=[]))

    def test_multiple_images_in_order(self):
        urls = ["https://example.com/front.jpg", "https://example.com/side.jpg"]
        p = self._payload(image_srcs=urls)
        self.assertEqual([i["src"] for i in p["images"]], urls)


# ===========================================================================
# No dealer-store meta: the builders take no dealer price (store retired, 1.11.0)
# ===========================================================================

class TestNoDealerStoreMeta(FrappeTestCase):
    """Neither pure builder emits the old dealer-store keys, and neither accepts
    the dealer price any more (it is the Cost field, never sent to the store)."""

    _GONE = ("_osa_publish_to_dealer", "_osa_dealer_price_override")

    def _meta(self, payload):
        return {m["key"]: m["value"] for m in payload["meta_data"]}

    def test_simple_payload(self):
        p = build_simple_product_payload(
            item_code="X1", item_name="X", standard_rate=100, stock_qty=1)
        for key in self._GONE:
            self.assertNotIn(key, self._meta(p))
        with self.assertRaises(TypeError):
            build_simple_product_payload(
                item_code="X1", item_name="X", standard_rate=100, stock_qty=1, dealer_price=70.0)

    def test_serial_payload(self):
        p = build_serial_product_payload(
            item_code="X1", serial_no="S1", item_name="X", sell_price=100)
        for key in self._GONE:
            self.assertNotIn(key, self._meta(p))
        with self.assertRaises(TypeError):
            build_serial_product_payload(
                item_code="X1", serial_no="S1", item_name="X", sell_price=100, dealer_price=0.0)


# ===========================================================================
# Manufacturer / Model → WC attributes  +  UPC → global_unique_id (GTIN)
# ===========================================================================

class TestIdentityAttributesAndGtin(FrappeTestCase):
    """Manufacturer / Model are emitted as pa_manufacturer / pa_model WC
    attributes, and UPC populates WooCommerce's native ``global_unique_id``
    (the "GTIN, UPC, EAN, or ISBN" field) — on both builders."""

    def _simple(self, **kwargs):
        defaults = dict(
            item_code="GLOCK-17", item_name="Glock 17 Gen6", standard_rate=620.0,
            stock_qty=3, is_firearm=True, caliber="9x19", item_group="Pistol",
        )
        defaults.update(kwargs)
        return build_simple_product_payload(**defaults)

    def _serial(self, **kwargs):
        defaults = dict(
            item_code="GLOCK-17", serial_no="ABC123", item_name="Glock 17 Gen6",
            sell_price=620.0, caliber="9x19",
        )
        defaults.update(kwargs)
        return build_serial_product_payload(**defaults)

    @staticmethod
    def _attrs(payload):
        return {a["slug"]: a["options"][0] for a in payload.get("attributes", [])}

    # --- simple builder ---
    def test_simple_manufacturer_attribute(self):
        self.assertEqual(self._attrs(self._simple(manufacturer="GLOCK"))["pa_manufacturer"], "GLOCK")

    def test_simple_model_attribute(self):
        self.assertEqual(self._attrs(self._simple(model_name="17 Gen 6"))["pa_model"], "17 Gen 6")

    def test_simple_no_identity_attrs_when_blank(self):
        slugs = self._attrs(self._simple())
        self.assertNotIn("pa_manufacturer", slugs)
        self.assertNotIn("pa_model", slugs)

    def test_simple_upc_sets_global_unique_id(self):
        self.assertEqual(self._simple(upc="764503068232")["global_unique_id"], "764503068232")

    def test_simple_gtin_stripped(self):
        self.assertEqual(self._simple(upc="  764503068232 ")["global_unique_id"], "764503068232")

    def test_simple_no_gtin_when_upc_blank(self):
        self.assertNotIn("global_unique_id", self._simple())

    # --- serial builder ---
    def test_serial_manufacturer_attribute(self):
        self.assertEqual(self._attrs(self._serial(manufacturer="GLOCK"))["pa_manufacturer"], "GLOCK")

    def test_serial_model_attribute(self):
        self.assertEqual(self._attrs(self._serial(model_name="17 Gen 6"))["pa_model"], "17 Gen 6")

    def test_serial_no_identity_attrs_when_blank(self):
        slugs = self._attrs(self._serial())
        self.assertNotIn("pa_manufacturer", slugs)
        self.assertNotIn("pa_model", slugs)

    def test_serial_upc_sets_global_unique_id(self):
        self.assertEqual(self._serial(upc="764503068232")["global_unique_id"], "764503068232")

    def test_serial_no_gtin_when_upc_blank(self):
        self.assertNotIn("global_unique_id", self._serial())


# ===========================================================================
# Thin loader: stock_for_item
# ===========================================================================

from ffl_core.tests.channel_query_doubles import (  # noqa: E402
    get_all_double,
    warm_channel_meta,
)


class TestStockForItem(FrappeTestCase):
    def _item_row(self, is_firearm=0, item_group="Products", uniform_sell_price=0):
        return {"is_firearm": is_firearm, "item_group": item_group,
                "uniform_sell_price": uniform_sell_price}

    def test_non_firearm_uses_bin_qty(self):
        row = self._item_row(is_firearm=0, item_group="Accessories")
        with patch("frappe.db.get_value", return_value=row), \
             patch("ffl_woo_sync.woocommerce.mapping.get_item_stock",
                   return_value={"ACC-001": 12.0}):
            result = stock_for_item("ACC-001")
        self.assertEqual(result, 12)

    def test_uniform_firearm_counts_active_non_reserved_serials(self):
        row = self._item_row(is_firearm=1, uniform_sell_price=1)
        # `custody_serials` is stubbed here (and below) because stock_for_item
        # gained a second data dependency on 2026-07-20 (customer custody).
        # These tests mock frappe.get_all wholesale, so without the stub it
        # would answer the custody lookup with this fixture list too and
        # exclude every serial. Custody exclusion has its own suite:
        # test_transfer_custody_isolation.py.
        #
        # The same hazard, third time: `free_units` also asks
        # `channel_reserved_serials`, whose SELECT wants ROWS, not names, and
        # `_channel_fields_present` builds `get_meta("Serial No")` through the
        # very `get_value` stubbed below. `get_all_double` answers the first by
        # query shape; `warm_channel_meta` takes the second out of the stub's
        # reach. Both leave this fixture doing exactly what it always did.
        warm_channel_meta()
        with patch("frappe.db.get_value", return_value=row), \
             patch("frappe.get_all",
                   side_effect=get_all_double(["S1", "S2", "S3"])), \
             patch("ffl_core.reservations.custody_serials",
                   return_value=set()), \
             patch("ffl_core.reservations.counter_holds",
                   return_value={}):
            result = stock_for_item("GLOCK-19")
        self.assertEqual(result, 3)

    def test_uniform_firearm_subtracts_counter_held_serials(self):
        row = self._item_row(is_firearm=1, uniform_sell_price=1)
        warm_channel_meta()
        with patch("frappe.db.get_value", return_value=row), \
             patch("frappe.get_all",
                   side_effect=get_all_double(["S1", "S2", "S3"])), \
             patch("ffl_core.reservations.custody_serials",
                   return_value=set()), \
             patch("ffl_core.reservations.counter_holds",
                   return_value={"S2": {"ref": "ACC-SINV-2026-99001"}}):
            result = stock_for_item("GLOCK-19")
        self.assertEqual(result, 2)

    def test_per_serial_firearm_raises(self):
        row = self._item_row(is_firearm=1, uniform_sell_price=0)
        with patch("frappe.db.get_value", return_value=row):
            with self.assertRaises(ValueError):
                stock_for_item("AR15")

    def test_non_firearm_negative_bin_clamped_to_zero(self):
        row = self._item_row(is_firearm=0)
        with patch("frappe.db.get_value", return_value=row), \
             patch("ffl_woo_sync.woocommerce.mapping.get_item_stock",
                   return_value={"ITEM": -3.0}):
            self.assertEqual(stock_for_item("ITEM"), 0)


# ===========================================================================
# Thin loader: item_to_product_payload (structural smoke test)
# ===========================================================================

class TestItemToProductPayload(FrappeTestCase):
    def _mock_item(self, **kwargs):
        item = MagicMock()
        item.name = kwargs.get("item_code", "TEST-001")
        item.item_code = kwargs.get("item_code", "TEST-001")
        item.item_name = kwargs.get("item_name", "Test Item")
        item.description = kwargs.get("description", "")
        item.standard_rate = kwargs.get("standard_rate", 99.0)
        item.item_group = kwargs.get("item_group", "Accessories")
        item.image_gallery = kwargs.get("image_gallery", [])
        item.get = lambda f, d=None: kwargs.get(f, d)
        return item

    def test_non_firearm_delegates_to_builder(self):
        item = self._mock_item(is_firearm=0, is_used=0, uniform_sell_price=0)
        with patch("frappe.get_doc", return_value=item), \
             patch("ffl_woo_sync.woocommerce.mapping.stock_for_item", return_value=5):
            payload = item_to_product_payload("TEST-001")
        self.assertEqual(payload["sku"], "TEST-001")
        self.assertEqual(payload["stock_quantity"], 5)

    def test_per_serial_firearm_raises(self):
        item = self._mock_item(is_firearm=1, uniform_sell_price=0)
        with patch("frappe.get_doc", return_value=item):
            with self.assertRaises(ValueError):
                item_to_product_payload("AR15-001")

    def test_attr_ids_threaded_into_attributes(self):
        """attr_ids are passed to _wc_attributes so the correct taxonomy IDs are used."""
        item = self._mock_item(
            is_firearm=1, is_used=0, uniform_sell_price=1,
            caliber="9mm Luger", item_group="Handgun",
        )
        attr_ids = {"pa_caliber": 5, "pa_condition": 7}
        with patch("frappe.get_doc", return_value=item), \
             patch("ffl_woo_sync.woocommerce.mapping.stock_for_item", return_value=2):
            payload = item_to_product_payload("GLOCK-19", attr_ids=attr_ids)
        by_slug = {a["slug"]: a["id"] for a in payload.get("attributes", [])}
        self.assertEqual(by_slug.get("pa_caliber"), 5)
        self.assertEqual(by_slug.get("pa_condition"), 7)

    def test_identity_threaded_from_item(self):
        """upc → GTIN; manufacturer/model → attributes. Item → payload. The Cost field
        (dealer_price) never reaches the store."""
        item = self._mock_item(
            is_firearm=0, is_used=0, uniform_sell_price=0,
            upc="012345678905", manufacturer="Magpul", model_name="PMAG 30",
            dealer_price=30.0,
        )
        with patch("frappe.get_doc", return_value=item), \
             patch("ffl_woo_sync.woocommerce.mapping.stock_for_item", return_value=5):
            payload = item_to_product_payload("TEST-001")
        attrs = {a["slug"]: a["options"][0] for a in payload.get("attributes", [])}
        meta = {m["key"]: m["value"] for m in payload["meta_data"]}
        self.assertEqual(payload["global_unique_id"], "012345678905")
        self.assertEqual(attrs["pa_manufacturer"], "Magpul")
        self.assertEqual(attrs["pa_model"], "PMAG 30")
        self.assertNotIn("_osa_dealer_price_override", meta)

    def test_image_gallery_rows_resolved(self):
        """image_gallery rows are resolved to absolute URLs and passed as image_srcs."""
        row = MagicMock()
        row.image = "/files/gun.jpg"
        row.is_primary = 0
        item = self._mock_item(is_firearm=0, image_gallery=[row])
        with patch("frappe.get_doc", return_value=item), \
             patch("ffl_woo_sync.woocommerce.mapping.stock_for_item", return_value=1), \
             patch("frappe.utils.get_url", return_value="https://pos.example.com"):
            payload = item_to_product_payload("TEST-001")
        self.assertIn("images", payload)
        self.assertEqual(payload["images"][0]["src"], "https://pos.example.com/files/gun.jpg")

    def test_item_primary_image_placed_first_in_payload(self):
        """Item is_primary=1 gallery row is always images[0] regardless of gallery order."""
        row1 = MagicMock(); row1.image = "/files/other.jpg"; row1.is_primary = 0
        row2 = MagicMock(); row2.image = "/files/primary.jpg"; row2.is_primary = 1
        item = self._mock_item(is_firearm=0, image_gallery=[row1, row2])
        with patch("frappe.get_doc", return_value=item), \
             patch("ffl_woo_sync.woocommerce.mapping.stock_for_item", return_value=1), \
             patch("frappe.utils.get_url", return_value="https://pos.example.com"):
            payload = item_to_product_payload("TEST-001")
        self.assertEqual(payload["images"][0]["src"], "https://pos.example.com/files/primary.jpg")
        self.assertEqual(payload["images"][1]["src"], "https://pos.example.com/files/other.jpg")


    def test_private_gallery_image_flipped_public_before_payload(self):
        """A /private/files/ gallery row is made public, the row rewritten, the
        cover re-asserted, the parent cache cleared, and the payload carries /files/."""
        row = MagicMock()
        row.image = "/private/files/gun.jpg"
        row.is_primary = 0
        row.name = "ROW-1"
        row.idx = 1
        item = self._mock_item(is_firearm=0, image_gallery=[row])
        with patch("frappe.get_doc", return_value=item), \
             patch("ffl_woo_sync.woocommerce.mapping.stock_for_item", return_value=1), \
             patch("ffl_woo_sync.woocommerce.mapping._publicize_file",
                   return_value="/files/gun.jpg") as flip, \
             patch("frappe.db.set_value") as set_value, \
             patch("frappe.utils.get_url", return_value="https://pos.example.com"):
            payload = item_to_product_payload("TEST-001")
        flip.assert_called_once_with("/private/files/gun.jpg",
                                     where="Item TEST-001 gallery row 1",
                                     owner=("Item", "TEST-001"))
        self.assertEqual(set_value.call_args_list, [
            call("Item Image Gallery", "ROW-1", "image", "/files/gun.jpg", update_modified=False),
            call("Item", "TEST-001", "image", "", update_modified=False),
        ])
        self.assertEqual(payload["images"][0]["src"], "https://pos.example.com/files/gun.jpg")
        self.assertEqual(row.image, "/files/gun.jpg")


# ===========================================================================
# Thin loader: serial_to_product_payload (structural smoke test)
# ===========================================================================


class TestSerialToProductPayload(FrappeTestCase):
    def _mock_serial(self, **kwargs):
        serial = MagicMock()
        serial.name = kwargs.get("name", "SN-001")
        serial.item_code = kwargs.get("item_code", "GLOCK-19")
        serial.status = kwargs.get("status", "Active")
        serial.image_gallery = kwargs.get("image_gallery", [])
        serial.get = lambda f, d=None: kwargs.get(f, d)
        return serial

    def _mock_item(self, **kwargs):
        item = MagicMock()
        item.item_code = kwargs.get("item_code", "GLOCK-19")
        item.item_name = kwargs.get("item_name", "Glock 19")
        item.description = kwargs.get("description", "")
        item.image_gallery = kwargs.get("image_gallery", [])
        item.get = lambda f, d=None: kwargs.get(f, d)
        return item

    def test_sku_uses_double_colon(self):
        serial = self._mock_serial(name="GXK99", item_code="GLOCK-19",
                                   sell_price=649.0, condition_grade="Good",
                                   woo_reserved=0)
        item = self._mock_item(item_code="GLOCK-19", is_used=1,
                                uniform_sell_price=0, standard_rate=0.0)
        with patch("frappe.get_doc", side_effect=[serial, item]):
            payload = serial_to_product_payload("GXK99")
        self.assertEqual(payload["sku"], "GLOCK-19::GXK99")

    def test_title_per_serial_uses_serial_item_name(self):
        """Per-serial firearm: product title comes from Serial No.item_name."""
        serial = self._mock_serial(name="SN-NAME", item_code="GLOCK-19",
                                   sell_price=700.0, woo_reserved=0,
                                   item_name="Remington M1903 rifle")
        item = self._mock_item(item_code="GLOCK-19", item_name="Model 1903 .30-06",
                               uniform_sell_price=0, standard_rate=0.0)
        with patch("frappe.get_doc", side_effect=[serial, item]):
            payload = serial_to_product_payload("SN-NAME")
        self.assertEqual(payload["name"], "Remington M1903 rifle")

    def test_title_per_serial_falls_back_to_item_name(self):
        """Per-serial firearm with no per-gun name falls back to the Item name."""
        serial = self._mock_serial(name="SN-NONAME", item_code="GLOCK-19",
                                   sell_price=700.0, woo_reserved=0)
        item = self._mock_item(item_code="GLOCK-19", item_name="Model 1903 .30-06",
                               uniform_sell_price=0, standard_rate=0.0)
        with patch("frappe.get_doc", side_effect=[serial, item]):
            payload = serial_to_product_payload("SN-NONAME")
        self.assertEqual(payload["name"], "Model 1903 .30-06")

    def test_title_uniform_price_uses_item_name_not_serial(self):
        """Uniform-price firearm: title comes from the parent Item even when the
        serial carries its own item_name."""
        serial = self._mock_serial(name="SN-UNI", item_code="GLOCK-19",
                                   sell_price=0.0, woo_reserved=0,
                                   item_name="Per-Gun Title")
        item = self._mock_item(item_code="GLOCK-19", item_name="Glock 19 Gen5",
                               uniform_sell_price=1, standard_rate=549.0)
        with patch("frappe.get_doc", side_effect=[serial, item]):
            payload = serial_to_product_payload("SN-UNI")
        self.assertEqual(payload["name"], "Glock 19 Gen5")

    def test_reserved_serial_stock_is_zero(self):
        serial = self._mock_serial(name="SN-RSV", item_code="SIG-P320",
                                   sell_price=500.0, woo_reserved=1,
                                   condition_grade="Good")
        item = self._mock_item(item_code="SIG-P320", is_used=1,
                                uniform_sell_price=0, standard_rate=0.0)
        with patch("frappe.get_doc", side_effect=[serial, item]):
            payload = serial_to_product_payload("SN-RSV")
        self.assertEqual(payload["stock_quantity"], 0)

    def test_identity_serial_level_wins_with_item_fallback(self):
        """manufacturer/model: serial-level wins → attributes; upc → GTIN."""
        serial = self._mock_serial(name="SN-META", item_code="GLOCK-19",
                                   sell_price=700.0, woo_reserved=0,
                                   manufacturer="SerialMfr", model_name="SerialModel")
        item = self._mock_item(item_code="GLOCK-19", is_used=1,
                               uniform_sell_price=0, standard_rate=0.0,
                               upc="UPC-123", manufacturer="ItemMfr",
                               model_name="ItemModel")
        with patch("frappe.get_doc", side_effect=[serial, item]):
            payload = serial_to_product_payload("SN-META")
        attrs = {a["slug"]: a["options"][0] for a in payload.get("attributes", [])}
        meta = {m["key"]: m["value"] for m in payload["meta_data"]}
        self.assertEqual(attrs["pa_manufacturer"], "SerialMfr")   # serial wins
        self.assertEqual(attrs["pa_model"], "SerialModel")        # serial wins
        self.assertEqual(payload["global_unique_id"], "UPC-123")  # from Item
        self.assertNotIn("_osa_publish_to_dealer", meta)

    def test_identity_falls_back_to_item_when_serial_blank(self):
        """manufacturer/model fall back to Item (→ attributes) when the serial has none."""
        serial = self._mock_serial(name="SN-FB", item_code="GLOCK-19",
                                   sell_price=700.0, woo_reserved=0)
        item = self._mock_item(item_code="GLOCK-19", is_used=1,
                               uniform_sell_price=0, standard_rate=0.0,
                               manufacturer="ItemMfr", model_name="ItemModel")
        with patch("frappe.get_doc", side_effect=[serial, item]):
            payload = serial_to_product_payload("SN-FB")
        attrs = {a["slug"]: a["options"][0] for a in payload.get("attributes", [])}
        self.assertEqual(attrs["pa_manufacturer"], "ItemMfr")
        self.assertEqual(attrs["pa_model"], "ItemModel")

    def test_description_uses_serial_over_item(self):
        """Per-serial description wins over the parent item's description, and
        the serial's plain-text (Text field) is rendered to HTML for the store."""
        serial = self._mock_serial(name="SN-DESC", item_code="GLOCK-19",
                                   sell_price=700.0, woo_reserved=0,
                                   description="Bore: bright\nNote: Curio & Relic eligible.")
        item = self._mock_item(item_code="GLOCK-19", is_used=1,
                               uniform_sell_price=0, standard_rate=0.0,
                               description="ITEM ONE-LINER")
        with patch("frappe.get_doc", side_effect=[serial, item]):
            payload = serial_to_product_payload("SN-DESC")
        desc = payload["description"]
        self.assertNotIn("ITEM ONE-LINER", desc)        # item description not used
        self.assertIn("Bore: bright", desc)
        self.assertIn("Note:", desc)
        self.assertIn("<br>", desc)                     # newline → HTML line break
        self.assertIn("&amp;", desc)                    # '&' HTML-escaped, not raw

    def test_description_falls_back_to_item_when_serial_blank(self):
        """When the serial has no description, fall back to the item's."""
        serial = self._mock_serial(name="SN-NODESC", item_code="GLOCK-19",
                                   sell_price=700.0, woo_reserved=0)
        item = self._mock_item(item_code="GLOCK-19", is_used=1,
                               uniform_sell_price=0, standard_rate=0.0,
                               description="ITEM ONE-LINER")
        with patch("frappe.get_doc", side_effect=[serial, item]):
            payload = serial_to_product_payload("SN-NODESC")
        self.assertEqual(payload["description"], "ITEM ONE-LINER")

    def test_serial_gallery_images_used_over_item_gallery(self):
        """Serial image_gallery takes priority over item image_gallery."""
        sn_row = MagicMock()
        sn_row.image = "/files/serial.jpg"
        sn_row.is_primary = 0
        item_row = MagicMock()
        item_row.image = "/files/item.jpg"
        item_row.is_primary = 0
        serial = self._mock_serial(name="SN-IMG", item_code="GLOCK-19",
                                   sell_price=700.0, image_gallery=[sn_row])
        item = self._mock_item(item_code="GLOCK-19", image_gallery=[item_row])
        with patch("frappe.get_doc", side_effect=[serial, item]), \
             patch("frappe.utils.get_url", return_value="https://pos.example.com"):
            payload = serial_to_product_payload("SN-IMG")
        self.assertIn("images", payload)
        self.assertEqual(payload["images"][0]["src"], "https://pos.example.com/files/serial.jpg")

    def test_falls_back_to_item_gallery_when_serial_gallery_empty(self):
        """Falls back to item gallery when serial has no images."""
        item_row = MagicMock()
        item_row.image = "/files/item.jpg"
        item_row.is_primary = 0
        serial = self._mock_serial(name="SN-FB", item_code="GLOCK-19",
                                   sell_price=700.0, image_gallery=[])
        item = self._mock_item(item_code="GLOCK-19", image_gallery=[item_row])
        with patch("frappe.get_doc", side_effect=[serial, item]), \
             patch("frappe.utils.get_url", return_value="https://pos.example.com"):
            payload = serial_to_product_payload("SN-FB")
        self.assertIn("images", payload)
        self.assertEqual(payload["images"][0]["src"], "https://pos.example.com/files/item.jpg")

    def test_serial_primary_image_placed_first_in_payload(self):
        """Serial is_primary=1 row is always images[0] regardless of gallery order."""
        row1 = MagicMock(); row1.image = "/files/side.jpg"; row1.is_primary = 0
        row2 = MagicMock(); row2.image = "/files/primary.jpg"; row2.is_primary = 1
        serial = self._mock_serial(name="SN-PRI", item_code="GLOCK-19",
                                   sell_price=700.0, image_gallery=[row1, row2])
        item = self._mock_item(item_code="GLOCK-19", image_gallery=[])
        with patch("frappe.get_doc", side_effect=[serial, item]), \
             patch("frappe.utils.get_url", return_value="https://pos.example.com"):
            payload = serial_to_product_payload("SN-PRI")
        self.assertEqual(payload["images"][0]["src"], "https://pos.example.com/files/primary.jpg")
        self.assertEqual(payload["images"][1]["src"], "https://pos.example.com/files/side.jpg")


    def test_private_serial_image_flipped_public_before_payload(self):
        """Serial No.image under /private/files/ is flipped, rewritten via db.set_value, and used."""
        serial = self._mock_serial(name="SN-PRIV", item_code="GLOCK-19", sell_price=649.0,
                                   condition_grade="Good", woo_reserved=0,
                                   image="/private/files/sn.jpg")
        item = self._mock_item(item_code="GLOCK-19", is_used=1,
                               uniform_sell_price=0, standard_rate=0.0)
        with patch("frappe.get_doc", side_effect=[serial, item]), \
             patch("ffl_woo_sync.woocommerce.mapping._publicize_file",
                   return_value="/files/sn.jpg") as flip, \
             patch("frappe.db.set_value") as set_value, \
             patch("frappe.utils.get_url", return_value="https://pos.example.com"):
            payload = serial_to_product_payload("SN-PRIV")
        flip.assert_called_once_with("/private/files/sn.jpg", where="Serial No SN-PRIV image",
                                     owner=("Serial No", "SN-PRIV"))
        set_value.assert_called_once_with("Serial No", "SN-PRIV", "image", "/files/sn.jpg",
                                          update_modified=False)
        self.assertEqual(payload["images"][0]["src"], "https://pos.example.com/files/sn.jpg")

    def test_public_images_cost_no_file_lookup(self):
        """Regression guard: docs with only /files/ photos never touch File.

        The guard is a blanket ``frappe.db.get_value`` assertion, so it also sees
        any read a `gunshop_woo_product_meta` provider makes on a bench where an
        extension is installed. Providers are silenced here — this test is about
        core's own File lookups, not about what an extension costs — and the
        extension seam has its own tests in test_extension_product_meta.py.
        """
        serial = self._mock_serial(name="SN-PUB", item_code="GLOCK-19", sell_price=649.0,
                                   condition_grade="Good", woo_reserved=0,
                                   image="/files/sn.jpg")
        item = self._mock_item(item_code="GLOCK-19", is_used=1,
                               uniform_sell_price=0, standard_rate=0.0)
        with patch("frappe.get_doc", side_effect=[serial, item]), \
             patch("frappe.get_hooks", return_value=[]), \
             patch("frappe.db.get_value") as get_value, \
             patch("frappe.db.set_value") as set_value, \
             patch("frappe.utils.get_url", return_value="https://pos.example.com"):
            payload = serial_to_product_payload("SN-PUB")
        get_value.assert_not_called()
        set_value.assert_not_called()
        self.assertEqual(payload["images"][0]["src"], "https://pos.example.com/files/sn.jpg")


# ===========================================================================
# URL helper: _resolve_image
# ===========================================================================

class TestResolveImage(FrappeTestCase):
    def test_relative_path_made_absolute(self):
        """/files/... paths are prefixed with the site URL."""
        with patch("frappe.utils.get_url", return_value="https://pos.example.com"):
            result = _resolve_image("/files/gun.jpg")
        self.assertEqual(result, "https://pos.example.com/files/gun.jpg")

    def test_absolute_https_url_passes_through_unchanged(self):
        """Already-absolute URLs are returned as-is (no double-prefix)."""
        url = "https://cdn.example.com/images/glock.jpg"
        result = _resolve_image(url)
        self.assertEqual(result, url)


# ===========================================================================
# URL helper: _resolve_image_list
# ===========================================================================

class TestResolveImageList(FrappeTestCase):
    def _row(self, path, is_primary=0):
        r = MagicMock()
        r.image = path
        r.is_primary = is_primary
        return r

    def test_gallery_rows_resolved_in_order(self):
        rows = [self._row("/files/a.jpg"), self._row("/files/b.jpg")]
        with patch("frappe.utils.get_url", return_value="https://pos.example.com"):
            result = _resolve_image_list(rows)
        self.assertEqual(result, [
            "https://pos.example.com/files/a.jpg",
            "https://pos.example.com/files/b.jpg",
        ])

    def test_empty_gallery_falls_back_to_single_image(self):
        with patch("frappe.utils.get_url", return_value="https://pos.example.com"):
            result = _resolve_image_list([], fallback_image="/files/fallback.jpg")
        self.assertEqual(result, ["https://pos.example.com/files/fallback.jpg"])

    def test_empty_gallery_no_fallback_returns_empty(self):
        self.assertEqual(_resolve_image_list([], fallback_image=None), [])

    def test_none_gallery_treated_as_empty(self):
        self.assertEqual(_resolve_image_list(None, fallback_image=None), [])

    def test_gallery_rows_skip_blank_images(self):
        rows = [self._row(""), self._row("/files/good.jpg"), self._row(None)]
        with patch("frappe.utils.get_url", return_value="https://pos.example.com"):
            result = _resolve_image_list(rows)
        self.assertEqual(result, ["https://pos.example.com/files/good.jpg"])

    def test_primary_row_placed_first_when_not_first_in_gallery(self):
        """is_primary=1 row becomes images[0] even when it is not first in the list."""
        rows = [
            self._row("/files/a.jpg", is_primary=0),
            self._row("/files/b.jpg", is_primary=1),
        ]
        with patch("frappe.utils.get_url", return_value="https://pos.example.com"):
            result = _resolve_image_list(rows)
        self.assertEqual(result[0], "https://pos.example.com/files/b.jpg")
        self.assertEqual(result[1], "https://pos.example.com/files/a.jpg")

    def test_non_primary_order_preserved_after_primary(self):
        """Non-primary rows maintain their original order after the primary row."""
        rows = [
            self._row("/files/primary.jpg", is_primary=1),
            self._row("/files/c.jpg", is_primary=0),
            self._row("/files/d.jpg", is_primary=0),
        ]
        with patch("frappe.utils.get_url", return_value="https://pos.example.com"):
            result = _resolve_image_list(rows)
        self.assertEqual(result[0], "https://pos.example.com/files/primary.jpg")
        self.assertEqual(result[1], "https://pos.example.com/files/c.jpg")
        self.assertEqual(result[2], "https://pos.example.com/files/d.jpg")

    def test_no_primary_flag_preserves_original_order(self):
        """When no row has is_primary=1, original order is unchanged."""
        rows = [self._row("/files/x.jpg", is_primary=0), self._row("/files/y.jpg", is_primary=0)]
        with patch("frappe.utils.get_url", return_value="https://pos.example.com"):
            result = _resolve_image_list(rows)
        self.assertEqual(result[0], "https://pos.example.com/files/x.jpg")
        self.assertEqual(result[1], "https://pos.example.com/files/y.jpg")

    def test_multiple_is_primary_rows_safe(self):
        """Two rows both marked is_primary=1 → no crash; both placed before non-primary."""
        rows = [
            self._row("/files/p1.jpg", is_primary=1),
            self._row("/files/p2.jpg", is_primary=1),
            self._row("/files/other.jpg", is_primary=0),
        ]
        with patch("frappe.utils.get_url", return_value="https://pos.example.com"):
            result = _resolve_image_list(rows)
        self.assertEqual(len(result), 3)
        # Both primaries must come before the non-primary
        other_idx = result.index("https://pos.example.com/files/other.jpg")
        p1_idx = result.index("https://pos.example.com/files/p1.jpg")
        p2_idx = result.index("https://pos.example.com/files/p2.jpg")
        self.assertLess(p1_idx, other_idx)
        self.assertLess(p2_idx, other_idx)


# ===========================================================================
# RSR high-res image upgrade: _to_highres
# ===========================================================================

class TestToHighres(FrappeTestCase):
    """RSR standard image URLs upgrade to the 300dpi _HR version when it exists."""

    _STD = "https://img.rsrgroup.com/pimages/GLP61750203_1.jpg"
    _HR = "https://img.rsrgroup.com/highres-pimages/GLP61750203_1_HR.jpg"

    def setUp(self):
        from ffl_woo_sync.woocommerce import mapping
        mapping._HIGHRES_CACHE.clear()

    def test_upgrades_when_hr_exists(self):
        with patch("requests.head", return_value=MagicMock(status_code=200)):
            self.assertEqual(_to_highres(self._STD), self._HR)

    def test_falls_back_to_standard_when_hr_missing(self):
        with patch("requests.head", return_value=MagicMock(status_code=404)):
            self.assertEqual(_to_highres(self._STD), self._STD)

    def test_non_rsr_url_unchanged_without_probing(self):
        url = "https://example.com/wp-content/uploads/2026/06/x.jpg"
        with patch("requests.head") as mock_head:
            self.assertEqual(_to_highres(url), url)
        mock_head.assert_not_called()

    def test_probe_failure_falls_back_to_standard(self):
        with patch("requests.head", side_effect=Exception("network down")):
            self.assertEqual(_to_highres(self._STD), self._STD)

    def test_result_is_memoised_per_url(self):
        with patch("requests.head", return_value=MagicMock(status_code=200)) as mock_head:
            _to_highres(self._STD)
            _to_highres(self._STD)
        self.assertEqual(mock_head.call_count, 1)  # second call served from cache


# ===========================================================================
# Pure helper: _wc_attributes with attr_ids
# ===========================================================================

class TestWcAttributes(FrappeTestCase):
    def test_uses_global_ids_when_provided(self):
        attrs = [("Caliber", "pa_caliber", "9mm Luger"), ("Condition", "pa_condition", "New")]
        attr_ids = {"pa_caliber": 5, "pa_condition": 7}
        result = _wc_attributes(attrs, attr_ids=attr_ids)
        by_slug = {a["slug"]: a for a in result}
        self.assertEqual(by_slug["pa_caliber"]["id"], 5)
        self.assertEqual(by_slug["pa_condition"]["id"], 7)

    def test_falls_back_to_zero_when_no_attr_ids(self):
        attrs = [("Caliber", "pa_caliber", "9mm Luger")]
        result = _wc_attributes(attrs, attr_ids=None)
        self.assertEqual(result[0]["id"], 0)

    def test_falls_back_to_zero_for_unknown_slug(self):
        attrs = [("Caliber", "pa_caliber", "9mm Luger")]
        result = _wc_attributes(attrs, attr_ids={"pa_condition": 7})
        self.assertEqual(result[0]["id"], 0)

    def test_options_and_visible_set_correctly(self):
        attrs = [("Condition", "pa_condition", "Excellent")]
        result = _wc_attributes(attrs, attr_ids={"pa_condition": 7})
        self.assertEqual(result[0]["options"], ["Excellent"])
        self.assertTrue(result[0]["visible"])


# ===========================================================================
# _canonical_caliber — display-layer caliber normalization (layer B)
#
# The caliber display map is frozen.  This is the DISPLAY table
# only; it must never touch the FastBound as-marked push path — that isolation
# is guarded by test_caliber_fb_isolation.py.
# ===========================================================================

class TestCanonicalCaliber(FrappeTestCase):
    def test_9mm_family_all_eight_variants_collapse(self):
        for raw in ("9x19", "9MM", "9MM PARA", "9 Para", "9 Luger",
                    "9x19mm", "9mmx19", "9"):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_caliber(raw), "9mm Luger")

    def test_45acp_variants(self):
        for raw in ("45ACP", ".45ACP", ".45 ACP/Auto"):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_caliber(raw), ".45 ACP")

    def test_762x39_variants(self):
        for raw in ("7.62X39", "7.62X39mm"):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_caliber(raw), "7.62x39mm")

    def test_556_family_including_dual_marked(self):
        # 5.56 kept distinct from .223; dual-marked 5.56mm/223 → 5.56 NATO
        for raw in ("5.56 NATO", "5.56mm", "5.56x45", "5.56mmx45", "5.56mm/223"):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_caliber(raw), "5.56 NATO")

    def test_223_kept_separate_from_556(self):
        for raw in ("223", ".223", ".223 Remington"):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_caliber(raw), ".223 Remington")

    def test_762_nato_folds_into_308(self):
        # 7.62 NATO (incl. 7.62.N) → .308 Win (display only, as-marked untouched)
        for raw in ("7.62 NATO", "7.62.N", ".308",
                    ".308/7.62x51mm (.308 Winchester)"):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_caliber(raw), ".308 Win")

    def test_distributor_spellings_fold_onto_existing_terms(self):
        # Feed spellings that minted duplicate store terms next to the canonical one.
        cases = {
            "556NATO": "5.56 NATO", "5.56": "5.56 NATO",
            "223 Remington": ".223 Remington", ".223 REM": ".223 Remington",
            "10MM": "10mm Auto", "300 Blackout": ".300 AAC Blackout",
            "16 GA": "16 Gauge", "20 GA": "20 Gauge", "12 GA": "12 Gauge",
        }
        for raw, canon in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_caliber(raw), canon)

    def test_765mm_stays_browning_not_32acp(self):
        # 7.65 mm → 7.65mm Browning (NOT folded into .32 ACP)
        self.assertEqual(_canonical_caliber("7.65 mm"), "7.65mm Browning")
        self.assertEqual(_canonical_caliber("32 ACP"), ".32 ACP")

    def test_38_special_vs_38_sw_stay_distinct(self):
        # Two different cartridges must NOT collapse together.
        self.assertEqual(_canonical_caliber("38 S&W SPECIAL"), ".38 Special")
        self.assertEqual(_canonical_caliber(".38 S&W"), ".38 S&W")

    def test_8mm_mauser_vs_8x56r_stay_distinct(self):
        self.assertEqual(_canonical_caliber("8MM"), "8mm Mauser")
        self.assertEqual(_canonical_caliber("8x56mm"), "8x56R")

    def test_case_and_whitespace_folding(self):
        # normkey = casefold + strip + internal-whitespace collapse
        self.assertEqual(_canonical_caliber(" 9MM  PARA "), "9mm Luger")
        self.assertEqual(_canonical_caliber("9mm para"), "9mm Luger")

    def test_canonical_names_are_idempotent(self):
        for canon in ("9mm Luger", ".45 ACP", ".357 Magnum", "12 Gauge",
                      "7.62x39mm", ".308 Win", ".30 Carbine", ".30 Luger",
                      ".38 Special", ".38 S&W", "9x18 Makarov", ".22 LR"):
            with self.subTest(canon=canon):
                self.assertEqual(_canonical_caliber(canon), canon)

    def test_unknown_string_passes_through_unchanged(self):
        # Never invent a mapping — a miss returns the raw string verbatim.
        for raw in ("Frobnicator 9000", ".700 Nitro Express", ""):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_caliber(raw), raw)

    def test_ambiguous_bare_tokens_are_not_dict_keys(self):
        # The map's key strategy: .30 / 30 / 7.62 are item-dependent, so they are
        # NOT in the dict and pass through unchanged (resolved item-side, not
        # by string normalization).  Their normkey equals themselves.
        for raw in (".30", "30", "7.62", "7.62MM", "6.5mm", ".22"):
            with self.subTest(raw=raw):
                self.assertNotIn(raw.casefold(), _CALIBER_CANONICAL)
                self.assertEqual(_canonical_caliber(raw), raw)

    def test_safely_mappable_short_tokens_are_dict_keys(self):
        # The map explicitly includes these unambiguous short tokens.  (This
        # diverges from an earlier note that listed 38/9x18 as excluded; the
        # frozen authority map wins.)
        self.assertIn("38", _CALIBER_CANONICAL)
        self.assertEqual(_canonical_caliber("38"), ".38 S&W")
        self.assertIn("9x18", _CALIBER_CANONICAL)
        self.assertEqual(_canonical_caliber("9x18"), "9x18 Makarov")


# ===========================================================================
# Builder wiring — both Woo builder paths run caliber through _canonical_caliber
# as-marked storage is untouched; only the pushed term is
# normalized.
# ===========================================================================

class TestBuilderCaliberNormalization(FrappeTestCase):
    def test_uniform_firearm_path_normalizes(self):
        p = build_simple_product_payload(
            item_code="G", item_name="G", description="", standard_rate=100.0,
            stock_qty=1, is_firearm=True, is_used=False, condition_grade="",
            caliber="9MM PARA", item_group="Pistol", image_srcs=[],
        )
        slugs = {a["slug"]: a["options"][0] for a in p["attributes"]}
        self.assertEqual(slugs["pa_caliber"], "9mm Luger")

    def test_ammo_path_normalizes(self):
        p = build_simple_product_payload(
            item_code="A", item_name="A", description="", standard_rate=20.0,
            stock_qty=5, is_firearm=False, is_used=False, condition_grade="",
            caliber="9x19", item_group="Ammunition", image_srcs=[],
        )
        slugs = {a["slug"]: a["options"][0] for a in p["attributes"]}
        self.assertEqual(slugs["pa_caliber"], "9mm Luger")

    def test_per_serial_path_normalizes(self):
        p = build_serial_product_payload(
            item_code="G", serial_no="SN1", item_name="G", description="",
            sell_price=699.0, is_used=True, condition_grade="Excellent",
            caliber="9MM PARA", image_srcs=[],
        )
        slugs = {a["slug"]: a["options"][0] for a in p["attributes"]}
        self.assertEqual(slugs["pa_caliber"], "9mm Luger")

    def test_unknown_caliber_pushed_verbatim_per_serial(self):
        p = build_serial_product_payload(
            item_code="G", serial_no="SN2", item_name="G", description="",
            sell_price=1.0, is_used=True, condition_grade="Good",
            caliber=".700 Nitro Express", image_srcs=[],
        )
        slugs = {a["slug"]: a["options"][0] for a in p["attributes"]}
        self.assertEqual(slugs["pa_caliber"], ".700 Nitro Express")


# ===========================================================================
# _canonical_model — display-layer model normalization (layer B)
#
# One gun, several operator spellings ⇒ one WooCommerce ``pa_model`` term per
# spelling ⇒ duplicate checkboxes in the shop's Model filter.  This table folds
# the known spellings.  DISPLAY table only — the FastBound as-marked path is
# guarded by test_caliber_fb_isolation.py.
#
# The M1 Garand / M1 Carbine separation is the load-bearing invariant here:
# they are different firearms and must never share a term.
# ===========================================================================

class TestCanonicalModel(FrappeTestCase):
    def test_m1_garand_spellings_collapse(self):
        for raw in ("M1", "U.S. RIFLE M1", "U.S. Rifle M1", "M1 Garand"):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_model(raw), "M1 Garand")

    def test_punctuation_and_spacing_variants_collapse(self):
        # normkey drops every non-alphanumeric, so a new dot/space variant of a
        # mapped model cannot grow a second shop term.  (The POS already holds
        # both "U.S. CARBINE M1" and "U.S.CARBINE M1" for the carbine, which is
        # what proves this class of variance is real here.)
        for raw in ("U.S.RIFLE M1", "US RIFLE M1", "us rifle m1",
                    " U.S. Rifle  M1 ", "m1", "M-1", "m1 garand"):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_model(raw), "M1 Garand")

    def test_m1_carbine_never_folds_into_the_garand(self):
        # Different firearm.  All three spellings live in the POS today.
        for raw in ("U.S. CARBINE M1", "U.S.CARBINE M1", "U.S. Carbine",
                    "M1 Carbine", "M1CARBINE"):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_model(raw), raw)
                self.assertNotEqual(_canonical_model(raw), "M1 Garand")

    def test_models_merely_containing_m1_are_untouched(self):
        # Whole-value lookup, never a substring match.  Every one of these is a
        # real pa_model term on the shop today.
        for raw in ("M1911A1", "M1911A1 U.S.ARMY", 'M1911 A2 6" - MATCH TACT.',
                    "M16 A2", "M11-A1", "AUG A3/M1", "M1903", "Model 1903",
                    "M1D", "M14"):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_model(raw), raw)

    def test_bare_m1_in_30_carbine_is_left_alone(self):
        # The one real exception in the book: an IBM M1 Carbine whose as-marked
        # model IS "M1" (serial 3898544).  The A&D record is correct and must
        # not be edited to suit the shop, so the display layer corroborates
        # against the rest of the record instead of folding blind.
        for cal in (".30 Carbine", "30 CARBINE", " .30 carbine ", "7.62x33"):
            with self.subTest(caliber=cal):
                self.assertEqual(_canonical_model("M1", cal), "M1")

    def test_bare_m1_is_left_alone_when_only_the_item_code_says_carbine(self):
        # The caliber signal can go quiet: seven of the eight serials on that
        # carbine's Item record the bare ".30" that ~40 Garands also use, so a
        # routine caliber tidy-up would erase it.  The item code survives that.
        self.assertEqual(
            _canonical_model("M1", ".30", item_code="M1CARBINE-30"), "M1")
        self.assertEqual(_canonical_model("M1", "", "M1CARBINE-30"), "M1")

    def test_a_garand_titled_carbine_length_still_folds(self):
        # Item NAME must not be a signal: on the per-serial path it is the
        # per-gun marketing title, so a Tanker Garand described as
        # "carbine-length" would otherwise stop folding on a copy edit.
        p = build_serial_product_payload(
            item_code="M1-30", serial_no="SN9",
            item_name="Springfield M1 Garand .30-06 — Tanker, carbine-length",
            description="", sell_price=2495.0, is_used=True,
            condition_grade="Good", caliber=".30", image_srcs=[],
            model_name="M1",
        )
        slugs = {a["slug"]: a["options"][0] for a in p["attributes"]}
        self.assertEqual(slugs["pa_model"], "M1 Garand")

    def test_carbine_detector_signals(self):
        self.assertTrue(_looks_like_a_carbine(".30 Carbine", ""))
        self.assertTrue(_looks_like_a_carbine("", "m1carbine-30"))
        self.assertFalse(_looks_like_a_carbine(".30", "M1-30"))
        self.assertFalse(_looks_like_a_carbine("", ""))

    def test_bare_m1_on_a_garand_record_still_folds(self):
        # ⚠ The real fleet records the Garand's caliber as the bare ".30" — the
        # ambiguous token _CALIBER_CANONICAL deliberately does not map.  Folding
        # MUST survive it, which is why a positive "Garand calibers" allowlist
        # would be wrong here: it would drop ~40 rifles out of the merged term.
        for cal in (".30", "30", ".30-06", "30-06", ".30-06 Springfield",
                    "7.62 NATO", ".308"):
            with self.subTest(caliber=cal):
                self.assertEqual(
                    _canonical_model("M1", cal, "M1-30"), "M1 Garand")

    def test_bare_m1_without_a_caliber_folds_to_the_garand(self):
        # Default stays the overwhelming majority.  Only a caliber that says
        # "carbine" overrides it.
        self.assertEqual(_canonical_model("M1"), "M1 Garand")
        self.assertEqual(_canonical_model("M1", ""), "M1 Garand")

    def test_unambiguous_spellings_ignore_the_corroboration(self):
        # Only keys in _MODEL_NEEDS_CORROBORATION consult it.  "U.S. Rifle M1"
        # names the Garand outright, so nothing else can unfold it.
        self.assertEqual(
            _canonical_model("U.S. RIFLE M1", ".30 Carbine", "M1CARBINE-30"),
            "M1 Garand")

    def test_bare_us_rifle_is_not_mapped(self):
        # "U.S. Rifle" is equally the M1903's service name — ambiguous by
        # string alone, so it is resolved POS-side and passes through here.
        self.assertNotIn(_norm_model_key("U.S. Rifle"), _MODEL_CANONICAL)
        self.assertEqual(_canonical_model("U.S. Rifle"), "U.S. Rifle")

    def test_the_display_name_is_a_fixed_point_including_its_variants(self):
        # Not via an identity table row (that would oblige the FastBound alias
        # table to agree with a retail-display convenience) but derived from
        # the table's own values.  WooCommerce matches attribute options by
        # exact term NAME, so a case/spacing variant that failed to fold here
        # would mint a second term and re-open the split.
        for raw in ("M1 Garand", "m1 garand", "M1 GARAND", "M1-Garand",
                    " M1  Garand "):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_model(raw), "M1 Garand")
        self.assertEqual(_canonical_model(_canonical_model("M1")), "M1 Garand")

    def test_unknown_string_passes_through_unchanged(self):
        for raw in ("Frobnicator 9000", "K98k", "P226", ""):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_model(raw), raw)


# ===========================================================================
# Builder wiring — both Woo builder paths run model_name through
# _canonical_model, so a push can never recreate the merged duplicate term.
# ===========================================================================

class TestBuilderModelNormalization(FrappeTestCase):
    def test_uniform_firearm_path_normalizes(self):
        p = build_simple_product_payload(
            item_code="G", item_name="G", description="", standard_rate=100.0,
            stock_qty=1, is_firearm=True, is_used=False, condition_grade="",
            caliber=".30-06", item_group="Rifle", image_srcs=[],
            model_name="U.S. RIFLE M1",
        )
        slugs = {a["slug"]: a["options"][0] for a in p["attributes"]}
        self.assertEqual(slugs["pa_model"], "M1 Garand")

    def test_per_serial_path_normalizes(self):
        p = build_serial_product_payload(
            item_code="M1-30", serial_no="SN1", item_name="M1 Garand 30.06",
            description="", sell_price=1895.0, is_used=True,
            condition_grade="Very Good", caliber=".30", image_srcs=[],
            model_name="M1",
        )
        slugs = {a["slug"]: a["options"][0] for a in p["attributes"]}
        self.assertEqual(slugs["pa_model"], "M1 Garand")

    def test_per_serial_carbine_stays_verbatim(self):
        p = build_serial_product_payload(
            item_code="M1CARBINE-30", serial_no="SN2", item_name="M1 Carbine",
            description="", sell_price=1185.0, is_used=True,
            condition_grade="Good", caliber=".30 Carbine", image_srcs=[],
            model_name="U.S.CARBINE M1",
        )
        slugs = {a["slug"]: a["options"][0] for a in p["attributes"]}
        self.assertEqual(slugs["pa_model"], "U.S.CARBINE M1")

    def test_per_serial_bare_m1_carbine_is_not_pushed_as_a_garand(self):
        # Regression lock for serial 3898544 (IBM M1 Carbine, bound-book model
        # "M1", .30 Carbine).  Without the guard this lands on the shop as
        # "M1 Garand" — a carbine inside the Garand filter and landing page.
        p = build_serial_product_payload(
            item_code="M1CARBINE-30", serial_no="3898544",
            item_name="M1 .30 Carbine", description="", sell_price=1195.0,
            is_used=True, condition_grade="Good", caliber=".30 Carbine",
            image_srcs=[], model_name="M1",
        )
        slugs = {a["slug"]: a["options"][0] for a in p["attributes"]}
        self.assertEqual(slugs["pa_model"], "M1")

    def test_per_serial_carbine_survives_a_caliber_tidy_up(self):
        # Same gun after someone normalises its caliber to the bare ".30" its
        # seven Item siblings use.  The caliber signal is gone; item code and
        # name still say carbine, so it must still not become a Garand.
        p = build_serial_product_payload(
            item_code="M1CARBINE-30", serial_no="3898544",
            item_name="M1 .30 Carbine", description="", sell_price=1195.0,
            is_used=True, condition_grade="Good", caliber=".30",
            image_srcs=[], model_name="M1",
        )
        slugs = {a["slug"]: a["options"][0] for a in p["attributes"]}
        self.assertEqual(slugs["pa_model"], "M1")

    def test_uniform_path_bare_m1_carbine_is_not_pushed_as_a_garand(self):
        p = build_simple_product_payload(
            item_code="M1CARBINE-30", item_name="M1 .30 Carbine", description="",
            standard_rate=1195.0, stock_qty=1, is_firearm=True, is_used=True,
            condition_grade="Good", caliber=".30 Carbine", item_group="Rifle",
            image_srcs=[], model_name="M1",
        )
        slugs = {a["slug"]: a["options"][0] for a in p["attributes"]}
        self.assertEqual(slugs["pa_model"], "M1")

    def test_blank_model_still_omits_the_attribute(self):
        p = build_serial_product_payload(
            item_code="G", serial_no="SN3", item_name="G", description="",
            sell_price=1.0, is_used=True, condition_grade="Good",
            caliber="9MM", image_srcs=[], model_name="   ",
        )
        self.assertNotIn("pa_model", {a["slug"] for a in p["attributes"]})


# ===========================================================================
# Private-upload guard: _publicize_file / _publicize_listing_images
# ===========================================================================

OWNER = ("Serial No", "S1")


class TestPublicizeFile(FrappeTestCase):
    def _file_doc(self, url="/private/files/gun.jpg", is_private=1):
        """File mock whose save() performs Frappe's private→public URL rewrite."""
        file_doc = MagicMock()
        file_doc.is_private = is_private
        file_doc.file_url = url
        file_doc.attached_to_field = "image"

        def _save(**kwargs):
            file_doc.file_url = "/files/" + url.rsplit("/", 1)[-1]
        file_doc.save.side_effect = _save
        return file_doc

    def test_public_and_absolute_paths_untouched_without_db(self):
        with patch("frappe.db.get_value") as get_value, patch("frappe.get_doc") as get_doc:
            self.assertEqual(_publicize_file("/files/gun.jpg", where="w", owner=OWNER),
                             "/files/gun.jpg")
            self.assertEqual(_publicize_file("https://cdn.example/x.jpg", where="w", owner=OWNER),
                             "https://cdn.example/x.jpg")
            self.assertEqual(_publicize_file("", where="w", owner=OWNER), "")
            self.assertEqual(_publicize_file(None, where="w", owner=OWNER), "")
        get_value.assert_not_called()
        get_doc.assert_not_called()

    def test_private_file_flipped_and_new_url_returned(self):
        file_doc = self._file_doc()
        with patch("frappe.db.get_value", return_value="FILE-1") as get_value, \
             patch("frappe.get_doc", return_value=file_doc):
            out = _publicize_file("/private/files/gun.jpg", where="Serial No S1 image", owner=OWNER)
        get_value.assert_called_once_with("File", {"file_url": "/private/files/gun.jpg"}, "name")
        self.assertEqual(out, "/files/gun.jpg")
        self.assertEqual(file_doc.is_private, 0)
        file_doc.save.assert_called_once_with(ignore_permissions=True)

    def test_file_row_already_public_is_not_saved_again(self):
        """Stale /private/ reference whose File row is already public (Frappe rewrites
        duplicate rows itself): just return the current file_url, no save."""
        file_doc = self._file_doc(url="/files/gun.jpg", is_private=0)
        with patch("frappe.db.get_value", return_value="FILE-1"), \
             patch("frappe.get_doc", return_value=file_doc):
            out = _publicize_file("/private/files/gun.jpg", where="w", owner=OWNER)
        self.assertEqual(out, "/files/gun.jpg")
        file_doc.save.assert_not_called()

    def test_missing_file_row_raises_actionable_error(self):
        # No private row, no public twin either.
        with patch("frappe.db.get_value", return_value=None) as get_value:
            with self.assertRaises(frappe.ValidationError) as cm:
                _publicize_file("/private/files/gone.jpg", where="Serial No S1 image", owner=OWNER)
        self.assertEqual(get_value.call_args_list[2], call(
            "File", {"file_url": "/files/gone.jpg"},
            ["name", "attached_to_doctype", "attached_to_name"], as_dict=True))
        msg = str(cm.exception)
        self.assertIn("Serial No S1 image", msg)
        self.assertIn("/private/files/gone.jpg", msg)
        self.assertNotIn("same name", msg)

    def test_stale_private_reference_resolves_to_public_twin_of_same_owner(self):
        """The same photo referenced twice: Frappe rewrote every File row on the
        first flip, so the second reference finds no private row — the public
        twin attached to THIS document is the answer, asserted via the one home."""
        twin = self._file_doc(url="/files/gun.jpg", is_private=0)
        with patch("frappe.db.get_value", side_effect=[None, "FILE-2", "FILE-2"]) as get_value, \
             patch("frappe.get_doc", return_value=twin) as get_doc:
            out = _publicize_file("/private/files/gun.jpg", where="Serial No S1 gallery row 1",
                                  owner=OWNER)
        self.assertEqual(out, "/files/gun.jpg")
        # 1: private row lookup (None) · 2: THIS document's twin row · 3: the home's own lookup
        self.assertEqual(get_value.call_args_list[1], call(
            "File",
            {"file_url": "/files/gun.jpg", "attached_to_doctype": "Serial No", "attached_to_name": "S1"},
            "name"))
        self.assertEqual(get_value.call_count, 3)
        get_doc.assert_called_once_with("File", "FILE-2")
        twin.save.assert_not_called()

    def test_twin_shared_by_two_documents_resolves_to_own_row(self):
        """Byte-identical photos on two products share one file_url; Frappe flipped
        both rows when the other product was pushed.  The owner-filtered lookup
        finds this document's row even though another row comes first."""
        twin = self._file_doc(url="/files/same.jpg", is_private=0)
        with patch("frappe.db.get_value", side_effect=[None, "FILE-MINE", "FILE-MINE"]), \
             patch("frappe.get_doc", return_value=twin):
            out = _publicize_file("/private/files/same.jpg", where="Item B image", owner=("Item", "B"))
        self.assertEqual(out, "/files/same.jpg")

    def test_public_twin_owned_by_another_document_is_refused(self):
        """IMG_2685.jpg in both folders can be two unrelated photos: never publish
        another document's picture — fail with a message that names the owner."""
        twin_row = frappe._dict(name="FILE-9", attached_to_doctype="Serial No", attached_to_name="OTHER")
        # 1: private row (None) · 2: owner-filtered twin (None) · 3: any twin, for the message
        with patch("frappe.db.get_value", side_effect=[None, None, twin_row]), \
             patch("frappe.get_doc") as get_doc:
            with self.assertRaises(frappe.ValidationError) as cm:
                _publicize_file("/private/files/gun.jpg", where="Serial No S1 gallery row 1",
                                owner=OWNER)
        get_doc.assert_not_called()
        msg = str(cm.exception)
        self.assertIn("Serial No S1 gallery row 1", msg)
        self.assertIn("belongs to Serial No OTHER", msg)

    def test_inconsistent_file_row_raises_instead_of_returning_private_url(self):
        """is_private=0 but file_url still /private/…: never hand Woo a 403 URL."""
        file_doc = self._file_doc(url="/private/files/gun.jpg", is_private=0)
        with patch("frappe.db.get_value", return_value="FILE-1"), \
             patch("frappe.get_doc", return_value=file_doc):
            with self.assertRaises(frappe.ValidationError) as cm:
                _publicize_file("/private/files/gun.jpg", where="Serial No S1 image", owner=OWNER)
        file_doc.save.assert_not_called()
        msg = str(cm.exception)
        self.assertIn("Serial No S1 image", msg)
        self.assertIn("disagree", msg)

    def test_site_policy_error_wrapped_with_where(self):
        """only_allow_system_managers_to_upload_public_files=1 → Frappe's bare
        ValidationError is re-thrown with the field named."""
        file_doc = self._file_doc()
        file_doc.save.side_effect = frappe.ValidationError(
            "Only System Managers can make this file public.")
        with patch("frappe.db.get_value", return_value="FILE-1"), \
             patch("frappe.get_doc", return_value=file_doc):
            with self.assertRaises(frappe.ValidationError) as cm:
                _publicize_file("/private/files/gun.jpg", where="Item X image", owner=("Item", "X"))
        msg = str(cm.exception)
        self.assertIn("Item X image", msg)
        self.assertIn("Only System Managers", msg)

    def test_disk_failure_raises_actionable_error(self):
        file_doc = self._file_doc()
        file_doc.save.side_effect = FileNotFoundError("Cannot find file on disk")
        with patch("frappe.db.get_value", return_value="FILE-1"), \
             patch("frappe.get_doc", return_value=file_doc):
            with self.assertRaises(frappe.ValidationError) as cm:
                _publicize_file("/private/files/gun.jpg", where="Item X gallery row 2",
                                owner=("Item", "X"))
        msg = str(cm.exception)
        self.assertIn("Item X gallery row 2", msg)
        self.assertIn("Cannot find file on disk", msg)


class TestPublicizeListingImages(FrappeTestCase):
    def _row(self, path, name="ROW-1", idx=1):
        row = MagicMock()
        row.image = path
        row.is_primary = 0
        row.name = name
        row.idx = idx
        return row

    def _doc(self, name="SN-1", image=None, gallery=None):
        doc = MagicMock()
        doc.name = name
        store = {"image": image, "image_gallery": gallery or []}
        doc.get = lambda f, d=None: store.get(f, d)
        return doc

    def test_all_public_returns_inputs_without_db(self):
        row = self._row("/files/a.jpg")
        doc = self._doc(image="/files/b.jpg", gallery=[row])
        with patch("frappe.db.get_value") as get_value, \
             patch("frappe.db.set_value") as set_value:
            rows, image = _publicize_listing_images(doc, doctype="Serial No")
        self.assertEqual(rows, [row])
        self.assertEqual(image, "/files/b.jpg")
        get_value.assert_not_called()
        set_value.assert_not_called()

    def test_private_gallery_row_rewritten_in_db_and_memory(self):
        row = self._row("/private/files/a.jpg", name="ROW-A", idx=2)
        doc = self._doc(name="SN-1", gallery=[row])
        with patch("ffl_woo_sync.woocommerce.mapping._publicize_file",
                   return_value="/files/a.jpg") as flip, \
             patch("frappe.db.set_value") as set_value:
            rows, image = _publicize_listing_images(doc, doctype="Serial No")
        flip.assert_called_once_with("/private/files/a.jpg", where="Serial No SN-1 gallery row 2",
                                     owner=("Serial No", "SN-1"))
        # Row rewritten, then the (empty) cover re-asserted.
        self.assertEqual(set_value.call_args_list, [
            call("Item Image Gallery", "ROW-A", "image", "/files/a.jpg", update_modified=False),
            call("Serial No", "SN-1", "image", "", update_modified=False),
        ])
        self.assertEqual(rows[0].image, "/files/a.jpg")
        self.assertEqual(image, "")

    def test_private_top_level_image_rewritten(self):
        doc = self._doc(name="ITEM-1", image="/private/files/top.jpg")
        with patch("ffl_woo_sync.woocommerce.mapping._publicize_file",
                   return_value="/files/top.jpg"), \
             patch("frappe.db.set_value") as set_value:
            rows, image = _publicize_listing_images(doc, doctype="Item")
        set_value.assert_called_once_with("Item", "ITEM-1", "image", "/files/top.jpg",
                                          update_modified=False)
        self.assertEqual(image, "/files/top.jpg")
        self.assertEqual(rows, [])

    def test_mixed_gallery_only_private_rows_touched(self):
        pub = self._row("/files/pub.jpg", name="ROW-P", idx=1)
        priv = self._row("/private/files/priv.jpg", name="ROW-Q", idx=2)
        doc = self._doc(name="SN-2", gallery=[pub, priv])
        with patch("ffl_woo_sync.woocommerce.mapping._publicize_file",
                   return_value="/files/priv.jpg") as flip, \
             patch("frappe.db.set_value") as set_value:
            rows, _ = _publicize_listing_images(doc, doctype="Serial No")
        flip.assert_called_once()
        self.assertEqual(set_value.call_args_list, [
            call("Item Image Gallery", "ROW-Q", "image", "/files/priv.jpg", update_modified=False),
            call("Serial No", "SN-2", "image", "", update_modified=False),
        ])
        self.assertEqual([r.image for r in rows], ["/files/pub.jpg", "/files/priv.jpg"])

    def test_same_photo_as_cover_and_gallery_row_is_flipped_once(self):
        """Item.image is mirrored into the gallery (sync_item_image_gallery): the
        second reference must hit the per-call memo, not a second File lookup."""
        row = self._row("/private/files/cover.jpg", name="ROW-C", idx=1)
        doc = self._doc(name="ITEM-1", image="/private/files/cover.jpg", gallery=[row])
        with patch("ffl_woo_sync.woocommerce.mapping._publicize_file",
                   return_value="/files/cover.jpg") as flip, \
             patch("frappe.db.set_value") as set_value:
            rows, image = _publicize_listing_images(doc, doctype="Item")
        flip.assert_called_once_with("/private/files/cover.jpg", where="Item ITEM-1 image",
                                     owner=("Item", "ITEM-1"))
        self.assertEqual(set_value.call_args_list, [
            call("Item", "ITEM-1", "image", "/files/cover.jpg", update_modified=False),
            call("Item Image Gallery", "ROW-C", "image", "/files/cover.jpg", update_modified=False),
            call("Item", "ITEM-1", "image", "/files/cover.jpg", update_modified=False),
        ])
        self.assertEqual(image, "/files/cover.jpg")
        self.assertEqual(rows[0].image, "/files/cover.jpg")

    def test_gallery_only_private_file_keeps_existing_public_cover(self):
        """Cover public, one gallery row private: after the flip the cover is
        re-asserted to its own URL — a gallery photo must never become the cover."""
        row = self._row("/private/files/extra.jpg", name="ROW-E", idx=2)
        doc = self._doc(name="SN-3", image="/files/cover.jpg", gallery=[row])
        with patch("ffl_woo_sync.woocommerce.mapping._publicize_file",
                   return_value="/files/extra.jpg"), \
             patch("frappe.db.set_value") as set_value:
            rows, image = _publicize_listing_images(doc, doctype="Serial No")
        self.assertEqual(set_value.call_args_list, [
            call("Item Image Gallery", "ROW-E", "image", "/files/extra.jpg", update_modified=False),
            call("Serial No", "SN-3", "image", "/files/cover.jpg", update_modified=False),
        ])
        self.assertEqual(image, "/files/cover.jpg")


class TestPublicizeFileOnDisk(FrappeTestCase):
    """Real File rows: the flip must move the bytes into /files and rewrite file_url.

    Mocks above pin the contract; this pins Frappe's actual move mechanics,
    which are the whole point of the guard.

    Disk cleanup is left to Frappe's own ``after_rollback`` handlers (the test
    transaction rolls back): the insert handler deletes at the File's original
    private path (``delete_file`` is exists-guarded), the flip handler moves
    the public file back — so the public file must still be there at rollback,
    i.e. NO manual deletion inside the test.  Callbacks run in insertion order,
    so the order-agnostic sweep below is registered AFTER the flip (in a
    ``finally``, so a failing flip still gets swept) and removes what the two
    handlers leave behind.
    """

    def _register_sweep(self, file_name: str) -> None:
        import os

        private_path = frappe.get_site_path("private", "files", file_name)
        public_path = frappe.get_site_path("public", "files", file_name)

        def _sweep():
            if not os.path.exists(public_path) and os.path.exists(private_path):
                os.remove(private_path)
        frappe.db.after_rollback.add(_sweep)

    def test_private_file_becomes_public_on_disk(self):
        import os

        token = frappe.generate_hash(length=10)
        file_doc = frappe.new_doc("File")
        file_doc.file_name = f"woo-guard-{token}.txt"
        file_doc.is_private = 1
        file_doc.content = f"woo private-image guard {token}".encode()
        file_doc.insert(ignore_permissions=True)
        private_path = frappe.get_site_path("private", "files", file_doc.file_name)
        public_path = frappe.get_site_path("public", "files", file_doc.file_name)

        self.assertEqual(file_doc.file_url, f"/private/files/{file_doc.file_name}")
        self.assertTrue(os.path.exists(private_path))
        try:
            new_url = _publicize_file(file_doc.file_url, where="test", owner=("Item", "none"))
        finally:
            self._register_sweep(file_doc.file_name)

        self.assertEqual(new_url, f"/files/{file_doc.file_name}")
        self.assertTrue(os.path.exists(public_path))
        self.assertFalse(os.path.exists(private_path))
        self.assertEqual(
            frappe.db.get_value("File", file_doc.name, ["is_private", "file_url"], as_dict=True),
            {"is_private": 0, "file_url": new_url},
        )
        # Idempotent: a second call on the (now public) URL is a no-op.
        self.assertEqual(_publicize_file(new_url, where="test", owner=("Item", "none")), new_url)

    # -- real Item: attached File rows, the cover referenced twice, a gallery-only file --

    def _private_file(self, file_name: str, *, attached_to: tuple[str, str, str]):
        """Private File row shaped like a desk/MCP upload: attached_to_* set —
        the gallery grid passes the CHILD fieldname ``image`` as attached_to_field."""
        doctype, name, field = attached_to
        file_doc = frappe.new_doc("File")
        file_doc.file_name = file_name
        file_doc.is_private = 1
        file_doc.content = f"woo guard {file_name}".encode()
        file_doc.attached_to_doctype = doctype
        file_doc.attached_to_name = name
        file_doc.attached_to_field = field
        file_doc.insert(ignore_permissions=True)
        return file_doc

    def test_real_item_double_reference_and_gallery_only_file(self):
        """The two Frappe side effects, on a real Item with real attached Files:

        * cover photo referenced twice (Item.image + its mirrored gallery row) →
          flipped once, both references rewritten, no "no File record" error;
        * a second, gallery-only private File whose attached_to_field says
          ``image`` → must not become Item.image; its attached_to_field survives.
        """
        import os

        token = frappe.generate_hash(length=8)
        item = frappe.get_doc({
            "doctype": "Item",
            "item_code": f"WOO-GUARD-{token}",
            "item_name": f"Woo guard {token}",
            "item_group": "Products",
            "stock_uom": "Nos",
            "is_stock_item": 1,
        })
        item.flags.ignore_permissions = True
        item.insert()
        cover = self._private_file(f"cover-{token}.txt", attached_to=("Item", item.name, "image"))
        extra = self._private_file(f"extra-{token}.txt", attached_to=("Item", item.name, "image"))
        item.image = cover.file_url
        item.append("image_gallery", {"image": cover.file_url, "is_primary": 1})
        item.append("image_gallery", {"image": extra.file_url, "is_primary": 0})
        item.save(ignore_permissions=True)
        item.reload()
        self.assertEqual(item.image, cover.file_url)
        self.assertEqual([r.image for r in item.image_gallery], [cover.file_url, extra.file_url])

        try:
            rows, image = _publicize_listing_images(item, doctype="Item")
        finally:
            self._register_sweep(cover.file_name)
            self._register_sweep(extra.file_name)

        pub_cover = f"/files/{cover.file_name}"
        pub_extra = f"/files/{extra.file_name}"
        self.assertEqual(image, pub_cover)
        self.assertEqual([r.image for r in rows], [pub_cover, pub_extra])
        for file_doc, pub in ((cover, pub_cover), (extra, pub_extra)):
            self.assertEqual(
                frappe.db.get_value(
                    "File", file_doc.name,
                    ["is_private", "file_url", "attached_to_field"], as_dict=True),
                {"is_private": 0, "file_url": pub, "attached_to_field": "image"},
            )
            self.assertTrue(os.path.exists(frappe.get_site_path("public", "files", file_doc.file_name)))
        fresh = frappe.get_doc("Item", item.name)
        self.assertEqual(fresh.image, pub_cover, "gallery-only file must not become the cover")
        self.assertEqual([r.image for r in fresh.image_gallery], [pub_cover, pub_extra])
