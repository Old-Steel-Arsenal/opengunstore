"""ffl_core.mailing_address: what the storefront customer push and the marketing list share."""
from __future__ import annotations

from frappe.tests.utils import FrappeTestCase

from ffl_core import mailing_address as ma
from ffl_core.tests.settings_sandbox import SettingsSandbox


class TestMailingAddress(SettingsSandbox, FrappeTestCase):
	def test_store_host_drops_www_and_path(self):
		self.assertEqual(ma.store_host("https://www.Shop.test/shop"), "shop.test")
		self.assertEqual(ma.store_host("shop.test"), "shop.test")
		self.assertEqual(ma.store_host(""), "")

	def test_clean_email_skips_blank_no_at_and_the_shops_own_domain(self):
		self.assertEqual(ma.clean_email("  Ann@Example.COM "), "ann@example.com")
		for bad in ("", None, "no-at-sign"):
			self.assertEqual(ma.clean_email(bad), "")
		self.assertEqual(ma.clean_email("staff@shop.test", "shop.test"), "")
		self.assertEqual(ma.clean_email("ann@mail.shop.test", "shop.test"), "ann@mail.shop.test")

	def test_split_name(self):
		self.assertEqual(ma.split_name("Ann", "Lee", "Ignored"), ("Ann", "Lee"))
		self.assertEqual(ma.split_name(None, None, "Ann Marie Lee - 2"), ("Ann", "Lee"))
		self.assertEqual(ma.split_name("Ann Lee", None, None), ("Ann", "Lee"))
		self.assertEqual(ma.split_name(None, None, None), ("", ""))

	def test_the_shops_host_is_the_store_url_setting(self):
		self.set_single("WooCommerce Settings", "store_url", "https://www.Shop.test/")
		self.assertEqual(ma.shop_host(), "shop.test")
		self.set_single("WooCommerce Settings", "store_url", "")
		self.assertEqual(ma.shop_host(), "")

	def test_the_storefront_push_still_exposes_the_names_it_always_had(self):
		from ffl_woo_sync.woocommerce import customer_events as ce

		self.assertIs(ce.clean_email, ma.clean_email)
		self.assertIs(ce.store_host, ma.store_host)
		self.assertIs(ce.split_name, ma.split_name)
