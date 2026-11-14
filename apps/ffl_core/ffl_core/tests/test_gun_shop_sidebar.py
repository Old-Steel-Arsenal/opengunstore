"""setup/sidebar: the Gun Shop shell's navigation on Frappe 16.50's `Sidebar` store.

A module without a `Sidebar` gets one computed from its contents (doctype lists, no way back
to Gun Shop): fresh 16.50 sites showed exactly that on Gun Shop, Settings, distributor-hub and
the consignment pages. Each test runs in a savepoint its cleanup rolls back, with
frappe.db.commit turned into a failure.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.setup import sidebar

_SAVEPOINT = "gun_shop_sidebar_test"
_LINKS = ["Gun Shop", "Settings"]
_EXTRA = [{"label": "Things", "type": "Section Break"},
	{"label": "Thing Page", "type": "Link", "link_type": "Page", "link_to": "permission-manager", "icon": "package"}]


def _labels(name):
	return [row.label for row in frappe.get_doc("Sidebar", name).items]


def _shipped_gun_shop_labels():
	"""The labels in the Gun Shop sidebar file ffl_core ships (the in-force copy must equal it)."""
	import json
	with open(frappe.get_app_path("ffl_core", "ffl_core", "sidebar", "gun_shop", "gun_shop.json")) as f:
		return [i["label"] for i in json.load(f)["items"]]


class GunShopSidebar(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		cls.addClassCleanup(frappe.clear_cache, doctype="Sidebar")  # registered first: runs after the class rollback
		super().setUpClass()

	def setUp(self):
		super().setUp()
		# doctype-scoped: a whole-site flush would leave every later test module on a cold cache
		self.addCleanup(frappe.clear_cache, doctype="Sidebar")
		frappe.db.savepoint(_SAVEPOINT)
		self.addCleanup(frappe.db.rollback, save_point=_SAVEPOINT)
		no_commit = patch.object(frappe.db, "commit", side_effect=AssertionError("commit inside a sidebar test"))
		no_commit.start()
		self.addCleanup(no_commit.stop)

	def _in_migrate(self):
		previous = frappe.flags.in_migrate
		frappe.flags.in_migrate = True
		self.addCleanup(setattr, frappe.flags, "in_migrate", previous)

	def test_the_shipped_sidebars_are_in_force(self):
		for name, module in (("Gun Shop", "FFL Core"), ("Settings", "FFL Integrations")):
			doc = frappe.get_doc("Sidebar", name)
			self.assertEqual((doc.module, doc.standard), (module, 1), name)
		self.assertEqual(_labels("Gun Shop"), _shipped_gun_shop_labels())
		self.assertEqual([r.link_to for r in frappe.get_doc("Sidebar", "Settings").items], _LINKS)

	def test_a_converted_copy_is_replaced_by_the_shipped_one(self):
		frappe.db.set_value("Sidebar", "Gun Shop", "standard", 0)
		frappe.db.delete("Sidebar Item", {"parent": "Gun Shop", "parenttype": "Sidebar", "link_to": "Settings"})
		sidebar.ensure_settings_sidebar_link()
		self.assertEqual(frappe.db.get_value("Sidebar", "Gun Shop", "standard"), 1)
		self.assertEqual(_labels("Gun Shop"), _shipped_gun_shop_labels())

	def test_an_extension_module_gets_the_gun_shop_links_and_its_own_once(self):
		self._in_migrate()
		self.assertTrue(sidebar.mirror_gun_shop_sidebar("Woo Test Nav", module="FFL Woo Sync", extra_items=_EXTRA))
		doc = frappe.get_doc("Sidebar", "Woo Test Nav")
		self.assertEqual((doc.module, doc.standard, doc.app), ("FFL Woo Sync", 0, None))
		self.assertEqual(_labels("Woo Test Nav"), _shipped_gun_shop_labels() + ["Things", "Thing Page"])
		self.assertFalse(sidebar.mirror_gun_shop_sidebar("Woo Test Nav", module="FFL Woo Sync", extra_items=_EXTRA))

	def test_outside_install_and_migrate_it_writes_nothing(self):
		with patch.dict(frappe.conf, {"developer_mode": 0}):
			self.assertFalse(sidebar.mirror_gun_shop_sidebar("Woo Test Nav", module="FFL Woo Sync", extra_items=_EXTRA))
		self.assertFalse(frappe.db.exists("Sidebar", "Woo Test Nav"))

	def test_a_standard_sidebar_is_left_to_its_app(self):
		self._in_migrate()
		self.assertFalse(sidebar.mirror_gun_shop_sidebar("Gun Shop", module="FFL Core", extra_items=_EXTRA))
		self.assertEqual(_labels("Gun Shop"), _shipped_gun_shop_labels())


_STAFF = "gun-shop-sidebar-staff@example.com"
_EXT_ROWS = [{"type": "Section Break", "label": "Ext Things", "icon": "package", "indent": 1, "collapsible": 1},
	{"type": "Link", "label": "Ext Page", "link_type": "Page", "link_to": "permission-manager", "child": 1}]


class GunShopSidebarAsStaff(FrappeTestCase):
	"""The sidebar a staff login actually boots (Frappe's own resolution: permission and domain
	filter, then the site layer), not our JSON or a layer merge without the filter. Starts from a
	site with no active domain, which is what a v16 site is until ffl_core's migrate step runs."""

	def setUp(self):
		super().setUp()
		frappe.db.savepoint(_SAVEPOINT)
		self.addCleanup(self._restore)
		no_commit = patch.object(frappe.db, "commit", side_effect=AssertionError("commit inside a sidebar test"))
		no_commit.start()
		self.addCleanup(no_commit.stop)
		if not frappe.db.exists("User", _STAFF):
			u = frappe.get_doc({"doctype": "User", "email": _STAFF, "first_name": "Staff", "send_welcome_email": 0})
			u.flags.ignore_permissions = True
			u.insert()
		frappe.get_doc("User", _STAFF).add_roles("System Manager", "Sales User", "Accounts User")
		frappe.db.delete("Has Domain", {"parent": "Domain Settings"})
		self._clear_domain_cache()

	def _restore(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point=_SAVEPOINT)
		self._clear_domain_cache()
		frappe.clear_cache(doctype="Sidebar")

	@staticmethod
	def _clear_domain_cache():
		frappe.cache.delete_value(["active_domains", "domain_restricted_pages", "domain_restricted_doctypes"])
		frappe.clear_cache(user=_STAFF)

	def _booted(self) -> list[dict]:
		from frappe.boot import get_module_sidebars

		frappe.set_user(_STAFF)
		try:
			return get_module_sidebars()[sidebar.SIDEBAR_NAME]["items"]
		finally:
			frappe.set_user("Administrator")

	def _assert_every_top_level_link(self):
		from ffl_core.setup import gunshop_sidebar

		off = gunshop_sidebar.integrations_off()
		shipped = [r["link_to"] for r in sidebar.gun_shop_items()
			if not r.get("child") and r.get("link_to") and r.get("link_to") not in off]
		self.assertIn("point-of-sale", shipped)
		booted = [i.get("link_to") for i in self._booted() if not i.get("child")]
		self.assertEqual([x for x in shipped if x not in booted], [])
		return booted

	def _migrate(self):
		from ffl_core.setup import gunshop_sidebar, pos_compat

		pos_compat.ensure_register_domain()
		gunshop_sidebar.sync_site_layer()

	def test_a_staff_login_gets_every_top_level_link_of_the_shipped_sidebar(self):
		self._migrate()
		self._assert_every_top_level_link()

	def test_and_with_an_extension_section_in_the_site_layer(self):
		from ffl_core.setup import gunshop_sidebar

		with patch.object(gunshop_sidebar, "extension_rows", return_value=[dict(r) for r in _EXT_ROWS]):
			self._migrate()
		self._assert_every_top_level_link()
		labels = [i.get("label") for i in self._booted()]
		self.assertLess(labels.index("Ext Things"), labels.index(sidebar.SETTINGS_LINK))

	def test_the_migrate_step_only_adds_the_domain_and_is_idempotent(self):
		from ffl_core.setup import pos_compat

		domain = frappe.db.get_value("Page", pos_compat.REGISTER_PAGE, "restrict_to_domain")
		if not domain:
			self.skipTest("ERPNext no longer restricts the register page to a domain")
		pos_compat.ensure_register_domain()
		pos_compat.ensure_register_domain()
		self.assertEqual(frappe.get_all("Has Domain", filters={"parent": "Domain Settings"}, pluck="domain"), [domain])
