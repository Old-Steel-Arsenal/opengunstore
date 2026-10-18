"""The OpenGunstore App tile follows the add_to_apps_screen logo, and the Dark Mode Logo reaches boot.

Frappe copies the hook's `logo` into the Desktop Icon once, at creation; renaming the asset left
production's tile pointing at a file that no longer exists (a broken image on /desk).
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.setup.desktop_icons import APP, ensure_gun_shop_desktop_tile
from ffl_core.store_identity import extend_bootinfo


class TestDesktopIconLogo(FrappeTestCase):
	def test_stale_logo_url_is_repointed_at_the_hook_logo(self):
		name = frappe.db.get_value("Desktop Icon", {"app": APP, "icon_type": "App"}, "name")
		if not name:
			# A fresh test site has no tile (frappe seeds it only from the desktop page);
			# a production one does, named after the app_title it was created under.
			name = "FFL Core"
			frappe.get_doc({
				"doctype": "Desktop Icon", "name": name, "label": name, "app": APP,
				"icon_type": "App", "link_type": "External", "link": "/desk/gun-shop",
			}).db_insert()
			self.addCleanup(frappe.db.delete, "Desktop Icon", {"name": name})
		frappe.db.set_value("Desktop Icon", name, "logo_url", "/assets/ffl_core/images/gun_shop.svg")

		ensure_gun_shop_desktop_tile()

		logo = frappe.get_hooks("add_to_apps_screen", app_name=APP)[0]["logo"]
		self.assertEqual(frappe.db.get_value("Desktop Icon", name, "logo_url"), logo)

	def test_dark_logo_reaches_boot(self):
		frappe.db.set_single_value("Navbar Settings", "app_logo_dark", "/files/dark.png")
		boot = frappe._dict()
		extend_bootinfo(boot)
		self.assertEqual(boot.app_logo_dark, "/files/dark.png")

		frappe.db.set_single_value("Navbar Settings", "app_logo_dark", None)
		boot = frappe._dict()
		extend_bootinfo(boot)
		self.assertEqual(boot.app_logo_dark, "")
