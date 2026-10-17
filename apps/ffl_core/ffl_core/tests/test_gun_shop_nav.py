"""The Gun Shop shell and home page: sidebar content, per-store shape, extension sections, the
"Needs attention" cards and the patch that brings an existing site's workspace to the new layout.

Each test runs inside the test transaction (``ffl_core.utils.commit`` is a no-op there); the
sync functions under test never call ``frappe.db.commit`` themselves.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import frappe
from frappe.desk.doctype.custom_sidebar.custom_sidebar import SITE_LAYER, add_site_sidebar_item, merged_arrangement
from frappe.tests.utils import FrappeTestCase

from ffl_core import extensions as sdk
from ffl_core.api import gun_shop_cards
from ffl_core.setup import gunshop_sidebar as gs
from ffl_core.setup.sidebar import SETTINGS_LINK, gun_shop_items
from ffl_core.tests.test_extensions import FAKE, ExtensionFixture

MODULE = "FFL Core"
ONLINE = {"Online", "Web orders", "GunBroker orders"}
FAKE_ROWS = [
	{"type": "Section Break", "label": "Fake Things", "icon": "package", "indent": 1, "collapsible": 1},
	{"type": "Link", "label": "Fake Page", "link_type": "Page", "link_to": "permission-manager", "child": 1},
]


def _app_json(*parts) -> dict:
	with open(frappe.get_app_path("ffl_core", *parts)) as f:
		return json.load(f)


def _drop_site_layer():
	for name in frappe.get_all("Custom Sidebar", filters={"module": MODULE, "user": ["in", ["", None]]}, pluck="name"):
		frappe.delete_doc("Custom Sidebar", name, ignore_permissions=True, force=True)


def _layer_names(module: str = MODULE) -> list[str]:
	return frappe.get_all("Custom Sidebar", filters={"module": module, "user": ["in", ["", None]]}, pluck="name")


def _rendered(base: list[dict] | None = None, module: str = MODULE) -> list[str]:
	"""Labels of the Gun Shop sidebar as Frappe merges the site layer over ``base``."""
	layers = [frappe.get_doc("Custom Sidebar", n) for n in _layer_names(module)]
	items, _ = merged_arrangement([dict(r) for r in (base or gun_shop_items())], layers)
	return [i["label"] for i in items]


def _user(email, *roles):
	if not frappe.db.exists("User", email):
		u = frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0})
		u.flags.ignore_permissions = True
		u.insert()
	frappe.db.delete("Has Role", {"parent": email, "parenttype": "User"})
	frappe.get_doc("User", email).add_roles(*roles)
	frappe.clear_cache(user=email)
	return email


@contextmanager
def _switches(off=()):
	with patch.object(gs, "integrations_off", return_value=set(off)):
		yield


@contextmanager
def _sources(build):
	"""Replace what the queue builders do: ``build(path)`` is called in place of the builder at ``path``
	(any other attribute lookup is the real one)."""
	real = frappe.get_attr
	paths = (gun_shop_cards.COUNTER_SOURCE, gun_shop_cards.WOO_SOURCE, gun_shop_cards.GUNBROKER_SOURCE)
	with patch.object(frappe, "get_attr", side_effect=lambda path, *a, **k: (lambda: build(path)) if path in paths else real(path, *a, **k)):
		yield


@contextmanager
def _fake_sidebar_hook(rows):
	real = frappe.get_hooks

	def fake(hook=None, default="_KEEP_", app_name=None):
		if hook == "gunshop_sidebar_items":
			return [dict(r) for r in rows] if app_name in (None, FAKE) else []
		return real(hook, app_name=app_name) if default == "_KEEP_" else real(hook, default, app_name)

	with patch.object(frappe, "get_hooks", side_effect=fake):
		yield


class TestShippedSidebar(FrappeTestCase):
	def test_overview_first_then_the_sections_in_order(self):
		items = _app_json("ffl_core", "sidebar", "gun_shop", "gun_shop.json")["items"]
		sections = [i["label"] for i in items if i["type"] == "Section Break"]
		self.assertEqual(sections, ["Sales", "Customers", "Compliance", "Inventory", "Purchasing", "Online"])
		# No Notifications row: Frappe's own sidebar entry opens the same Notification Log.
		self.assertEqual([(i["label"], i["link_type"], i["link_to"]) for i in items[:2]], [
			("Dashboard", "Page", "store-overview"), ("Point of sale", "Page", "point-of-sale")])
		self.assertEqual((items[-1]["link_type"], items[-1]["link_to"]), ("Workspace", "Settings"))
		self.assertFalse(any(i["keep_closed"] for i in items))
		self.assertTrue(all(i["collapsible"] for i in items if i["type"] == "Section Break"))

	def test_each_section_holds_the_links_of_the_design(self):
		items = _app_json("ffl_core", "sidebar", "gun_shop", "gun_shop.json")["items"]
		sections, current = {}, None
		for i in items[2:-1]:
			if i["type"] == "Section Break":
				current = sections.setdefault(i["label"], [])
			else:
				self.assertTrue(i["child"], i["label"])
				current.append(i["label"])
		self.assertEqual(sections, {
			"Sales": ["Orders", "POS invoices", "Create order", "Gunsmith work orders", "Cash drawer", "Card payments", "Sales report"],
			"Customers": ["Customers", "Agreements", "Agreement templates", "Signing tablets"],
			"Compliance": ["4473 records", "Acquisitions", "Dispositions", "Transfer pickups", "Firearms in stock"],
			"Inventory": ["Items", "Serial numbers", "Stock on hand", "Inventory count", "Storage locations"],
			"Purchasing": ["Distributor orders", "Receive goods", "Suppliers"],
			"Online": ["Web orders", "GunBroker orders"],
		})

	def test_ownership_claims_leave_erpnexts_four_alone(self):
		"""A claim needs its row on screen (hidden rows claim nothing). POS Invoice and Serial No keep
		a row for that: the Overview opens them, and the counter must stay in the Gun Shop shell."""
		items = _app_json("ffl_core", "sidebar", "gun_shop", "gun_shop.json")["items"]
		claimed = {i["link_to"] for i in items if i.get("is_default_module")}
		self.assertTrue({"Customer", "Item", "store-overview", "POS Invoice", "Serial No", "Payroc Terminal Payment"} <= claimed)
		self.assertFalse(claimed & {"Stock Balance", "Supplier", "point-of-sale"})
		self.assertFalse(claimed & {"Customer Group", "Territory", "Sales Person", "Item Group"})

	def test_every_link_points_at_something_that_exists(self):
		items = _app_json("ffl_core", "sidebar", "gun_shop", "gun_shop.json")["items"]
		for i in items:
			if i["type"] == "Link":
				self.assertTrue(frappe.db.exists(i["link_type"], i["link_to"]), f"{i['label']}: {i['link_type']} {i['link_to']}")

	def test_the_sidebar_in_force_is_the_shipped_one(self):
		shipped = [i["label"] for i in _app_json("ffl_core", "sidebar", "gun_shop", "gun_shop.json")["items"]]
		self.assertEqual([r.label for r in frappe.get_doc("Sidebar", "Gun Shop").items], shipped)


class TestStoreSwitches(FrappeTestCase):
	def setUp(self):
		super().setUp()
		_drop_site_layer()
		self.addCleanup(frappe.clear_cache, doctype="Custom Sidebar")

	def test_switched_off_integrations_hide_their_links_and_the_empty_section(self):
		with _switches({"Woo Online Order", "GunBroker Order"}):
			self.assertTrue(gs.sync_site_layer())
		shown = _rendered()
		self.assertFalse(ONLINE & set(shown))
		self.assertIn("Purchasing", shown)
		self.assertEqual(shown[0], "Dashboard")
		self.assertEqual(shown[-1], "Settings")

	def test_one_integration_off_hides_only_its_link(self):
		with _switches({"GunBroker Order"}):
			gs.sync_site_layer()
		shown = _rendered()
		self.assertIn("Online", shown)
		self.assertIn("Web orders", shown)
		self.assertNotIn("GunBroker orders", shown)

	def test_card_payments_follow_payroc(self):
		"""Card payments sits in Sales after Cash drawer; a store with Payroc off hides only that row."""
		full = _rendered()
		self.assertEqual(full[full.index("Cash drawer") + 1], "Card payments")
		with _switches({"Payroc Terminal Payment"}):
			self.assertTrue(gs.sync_site_layer())
			self.assertFalse(gs.sync_site_layer())
		shown = _rendered()
		self.assertNotIn("Card payments", shown)
		self.assertEqual(shown, [label for label in full if label != "Card payments"])

	def test_off_then_on_leaves_no_layer_and_the_full_sidebar(self):
		full = _rendered()
		with _switches({"Woo Online Order", "GunBroker Order", "Payroc Terminal Payment"}):
			gs.sync_site_layer()
			self.assertFalse(gs.sync_site_layer())          # idempotent
		self.assertEqual(len(_layer_names()), 1)
		with _switches():
			self.assertTrue(gs.sync_site_layer())
			self.assertFalse(gs.sync_site_layer())
		self.assertEqual(_layer_names(), [])
		self.assertEqual(_rendered(), full)

	def test_an_item_the_app_adds_later_still_shows_on_a_hidden_store(self):
		with _switches({"Woo Online Order", "GunBroker Order"}):
			gs.sync_site_layer()
		later = {"type": "Link", "label": "Brand New", "link_type": "DocType", "link_to": "Note", "child": 1}
		base = gun_shop_items()
		at = next(i for i, r in enumerate(base) if r["label"] == "Cash drawer")
		shown = _rendered(base[:at + 1] + [later] + base[at + 1:])
		self.assertIn("Brand New", shown)
		self.assertLess(shown.index("Cash drawer"), shown.index("Brand New"))
		self.assertFalse(ONLINE & set(shown))

	def test_a_row_frappe_added_for_a_new_workspace_survives_the_sync(self):
		add_site_sidebar_item(MODULE, {"type": "Link", "label": "My Page", "link_type": "DocType", "link_to": "Note", "icon": "star"})
		with _switches({"Woo Online Order", "GunBroker Order"}):
			gs.sync_site_layer()
		shown = _rendered()
		self.assertIn("My Page", shown)
		self.assertFalse(ONLINE & set(shown))
		self.assertEqual(shown[-2:], ["Settings", "My Page"])

	def test_the_settings_update_hook_is_wired_for_all_three_integrations(self):
		events = frappe.get_hooks("doc_events")
		for dt in ("WooCommerce Settings", "GunBroker Settings", "Payroc Settings"):
			self.assertIn("ffl_core.setup.gunshop_sidebar.on_settings_update", events[dt]["on_update"])

	def test_a_failing_sync_never_fails_the_settings_save(self):
		def half_then_boom():
			frappe.get_doc({"doctype": "Custom Sidebar", "module": MODULE, "user": SITE_LAYER, "sidebar_items": [
				{"type": "Link", "label": "Half", "link_type": "DocType", "link_to": "Note", "added": 1}]}).insert(ignore_permissions=True)
			raise RuntimeError("sync broke")

		errors = frappe.db.count("Error Log")
		with patch.object(gs, "sync", side_effect=half_then_boom):
			doc = frappe.get_doc("GunBroker Settings")
			doc.save()                                                   # on_update -> the guarded hook
		self.assertEqual(frappe.db.count("Error Log"), errors + 1)
		self.assertEqual(_layer_names(), [])                             # no half-written layer
		gs.on_settings_update()                                          # and the next call works
		frappe.db.set_single_value("GunBroker Settings", "enabled", 0)
		gs.on_settings_update()
		self.assertNotIn("GunBroker orders", _rendered())

	def test_gunbroker_settings_save_re_shapes_the_sidebar(self):
		"""The real switch: GunBroker Settings off -> its link goes, on -> it is back."""
		for enabled, expect in ((0, False), (1, True)):
			frappe.db.set_single_value("GunBroker Settings", "enabled", enabled)
			gs.on_settings_update()
			self.assertEqual("GunBroker orders" in _rendered(), expect)


class TestExtensionSections(ExtensionFixture):
	def setUp(self):
		super().setUp()
		_drop_site_layer()
		self.addCleanup(frappe.clear_cache, doctype="Custom Sidebar")

	def _flip(self, enabled):
		doc = frappe.get_doc("POS Extension", FAKE)
		doc.enabled = enabled
		doc.save()

	def test_an_effective_extension_adds_its_section_before_settings_and_off_removes_it(self):
		with _fake_sidebar_hook(FAKE_ROWS), _switches():
			gs.sync_site_layer()
			shown = _rendered()
			self.assertEqual(shown[-3:], ["Fake Things", "Fake Page", SETTINGS_LINK])
			self.assertEqual(shown[:-3], [r["label"] for r in gun_shop_items()][:-1])
			self._flip(0)                                    # the real toggled hook rebuilds the layer
			self.assertNotIn("Fake Things", _rendered())
			self.assertEqual(_layer_names(), [])
			self._flip(1)
			self.assertEqual(_rendered()[-3:], ["Fake Things", "Fake Page", SETTINGS_LINK])

	def test_repeated_syncs_do_not_duplicate_the_section(self):
		with _fake_sidebar_hook(FAKE_ROWS), _switches():
			self.assertTrue(gs.sync_site_layer())
			self.assertFalse(gs.sync_site_layer())
			self.assertFalse(gs.sync_site_layer())
		self.assertEqual(_rendered().count("Fake Things"), 1)
		self.assertEqual(frappe.db.count("Sidebar Item", {"parenttype": "Custom Sidebar", "label": "Fake Things"}), 1)

	def test_hiding_and_an_extension_section_work_together(self):
		with _fake_sidebar_hook(FAKE_ROWS), _switches({"Woo Online Order", "GunBroker Order"}):
			gs.sync_site_layer()
		shown = _rendered()
		self.assertFalse(ONLINE & set(shown))
		self.assertEqual(shown[-3:], ["Fake Things", "Fake Page", SETTINGS_LINK])

	def test_rows_of_an_uninstalled_extension_go_with_it_and_a_frappe_row_stays(self):
		"""The hook that declared the rows is gone once the app is uninstalled; the layer must still
		recognise the rows as ours (it remembers what it wrote) and not carry them as foreign."""
		add_site_sidebar_item(MODULE, {"type": "Link", "label": "My Page", "link_type": "DocType", "link_to": "Note", "icon": "star"})
		with _switches():
			with _fake_sidebar_hook(FAKE_ROWS):
				gs.sync_site_layer()
				self.assertIn("Fake Things", _rendered())
			with _fake_sidebar_hook([]):
				gs.sync_site_layer()                          # same extension, hook no longer declared
		shown = _rendered()
		self.assertNotIn("Fake Things", shown)
		self.assertNotIn("Fake Page", shown)
		self.assertEqual(shown[-2:], ["Settings", "My Page"])
		with _switches(), _fake_sidebar_hook([]):
			self.assertFalse(gs.sync_site_layer())

	def test_the_app_install_and_uninstall_hooks_rebuild_the_layer(self):
		for hook in ("after_app_install", "after_app_uninstall"):
			self.assertIn("ffl_core.setup.gunshop_sidebar.on_app_change", frappe.get_hooks(hook))
		with _fake_sidebar_hook(FAKE_ROWS), _switches():
			gs.on_app_change(FAKE)
			self.assertIn("Fake Things", _rendered())
		with _fake_sidebar_hook(FAKE_ROWS), _switches({"Woo Online Order"}):   # ffl_woo_sync uninstalled: its card goes too
			gs.on_app_change("ffl_woo_sync")
		self.assertNotIn("Web Orders", [c.number_card_name for c in frappe.get_doc("Workspace", "Gun Shop").number_cards])

	def test_a_changed_section_flag_is_written(self):
		"""Change detection compares the whole row, not a few columns of it."""
		closed = [{**FAKE_ROWS[0], "keep_closed": 1}, FAKE_ROWS[1]]
		with _switches():
			with _fake_sidebar_hook(FAKE_ROWS):
				self.assertTrue(gs.sync_site_layer())
			with _fake_sidebar_hook(closed):
				self.assertTrue(gs.sync_site_layer())
				self.assertFalse(gs.sync_site_layer())
		section = next(r for r in frappe.get_doc("Custom Sidebar", _layer_names()[0]).sidebar_items if r.label == "Fake Things")
		self.assertEqual(section.keep_closed, 1)

	def test_an_extension_module_mirror_follows_the_store_like_the_shell(self):
		"""An extension's own pages show the Gun Shop sidebar (mirror_gun_shop_sidebar): it must hide
		what the shell hides and carry the extension's section once, before Settings."""
		from ffl_core.setup.sidebar import item_dicts, mirror_gun_shop_sidebar
		mirror_module = "FFL Woo Sync"
		frappe.db.savepoint("gs_mirror_test")
		self.addCleanup(frappe.db.rollback, save_point="gs_mirror_test")
		self.addCleanup(frappe.clear_cache, doctype="Sidebar")
		previous = frappe.flags.in_migrate
		frappe.flags.in_migrate = True
		self.addCleanup(setattr, frappe.flags, "in_migrate", previous)
		with _fake_sidebar_hook([]):
			self.assertTrue(mirror_gun_shop_sidebar("Mirror Nav Test", module=mirror_module, extra_items=FAKE_ROWS))
		mirror_base = item_dicts(frappe.get_doc("Sidebar", "Mirror Nav Test"))

		with _fake_sidebar_hook(FAKE_ROWS), _switches({"GunBroker Order", "Payroc Terminal Payment"}):
			self.assertTrue(gs.sync_site_layer())
			self.assertFalse(gs.sync_site_layer())
		shell = _rendered()
		mirror = _rendered(mirror_base, module=mirror_module)
		self.assertNotIn("GunBroker orders", mirror)
		self.assertNotIn("Card payments", mirror)
		self.assertEqual(mirror.count("Fake Things"), 1)
		self.assertEqual(mirror.count("Fake Page"), 1)
		self.assertEqual(mirror[-3:], ["Fake Things", "Fake Page", SETTINGS_LINK])
		self.assertEqual(mirror, shell)

		with _switches(), _fake_sidebar_hook([]):            # everything back on, extension gone
			self.assertTrue(gs.sync_site_layer())
		self.assertEqual(_layer_names(mirror_module), [])
		self.assertEqual(_rendered(mirror_base, module=mirror_module).count("Fake Things"), 1)    # its own extra_items

	def test_an_extension_that_is_not_effective_adds_nothing(self):
		self.installed_here = False
		from ffl_core.extensions import registry
		registry.sync()
		with _fake_sidebar_hook(FAKE_ROWS), _switches():
			gs.sync_site_layer()
		self.assertNotIn("Fake Things", _rendered())
		self.assertFalse(sdk.is_enabled(FAKE))


class TestNeedsAttentionCards(FrappeTestCase):
	CARDS = ("pending_orders", "pending_4473", "transfer_pickups", "to_receive", "web_orders")

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		gun_shop_cards.clear_cache()           # counts are cached for a minute; each test starts cold
		self.addCleanup(gun_shop_cards.clear_cache)

	def test_shipped_cards_match_the_endpoint_and_the_page_layout(self):
		self.assertEqual(set(self.CARDS), set(gun_shop_cards.CARDS))
		ws = _app_json("ffl_core", "workspace", "gun_shop", "gun_shop.json")
		blocks = json.loads(ws["content"])
		self.assertEqual(blocks[0]["type"], "header")                  # Needs attention is the top of the page
		on_page = [b["data"]["number_card_name"] for b in blocks if b["type"] == "number_card"]
		self.assertEqual(on_page, ["Pending Orders", "Pending 4473", "Transfer Pickups", "To Receive", "Web Orders"])
		for key in self.CARDS:
			card = _app_json("ffl_core", "number_card", key, f"{key}.json")
			self.assertEqual((card["type"], card["method"]), ("Custom", "ffl_core.api.gun_shop_cards.get_count"))
			self.assertEqual(json.loads(card["filters_json"]), {"card": key})
			self.assertIn(card["name"], on_page)
			self.assertTrue(frappe.db.exists("Number Card", card["name"]), card["name"])

	def test_report_cards_count_the_rows_of_their_report(self):
		from ffl_core.ffl_core.report.pending_4473_orders.pending_4473_orders import execute as p4473
		from ffl_core.ffl_core.report.pending_transfer_pickups.pending_transfer_pickups import execute as pickups
		for key, report in (("pending_4473", p4473), ("transfer_pickups", pickups)):
			got = gun_shop_cards.get_count({"card": key})
			self.assertEqual(got["value"], len(report({})[1]), key)
			self.assertEqual(got["route"][0], "query-report")

	def test_pending_orders_is_the_pending_order_page_sources(self):
		with patch.object(gun_shop_cards, "_queue", side_effect=lambda path: {
				gun_shop_cards.COUNTER_SOURCE: 3, gun_shop_cards.WOO_SOURCE: 2, gun_shop_cards.GUNBROKER_SOURCE: 1}[path]):
			self.assertEqual(gun_shop_cards.get_count(json.dumps({"card": "pending_orders"}))["value"], 6)

	def test_a_source_is_counted_like_the_page_counts_it(self):
		"""Not installed or failing: that source is 0 and the others still count."""
		woo = gun_shop_cards.WOO_SOURCE
		real = frappe.get_installed_apps()
		with patch("frappe.get_installed_apps", return_value=[a for a in real if a != "ffl_woo_sync"]), \
				_sources(lambda path: self.fail("built a source whose app is gone")):
			self.assertEqual(gun_shop_cards._queue(woo), 0)
		errors = frappe.db.count("Error Log")
		with _sources(lambda path: 1 / 0):
			self.assertEqual(gun_shop_cards._queue(woo), 0)
		self.assertEqual(frappe.db.count("Error Log"), errors + 1)
		with _sources(lambda path: [1, 2]):                                          # the failure was not cached
			self.assertEqual(gun_shop_cards._queue(woo), 2)

	def test_each_queue_is_built_once_a_minute_and_shared_by_the_cards(self):
		builds = []
		sizes = {gun_shop_cards.COUNTER_SOURCE: 3, gun_shop_cards.WOO_SOURCE: 2, gun_shop_cards.GUNBROKER_SOURCE: 1}

		def build(path):
			builds.append(path)
			return [1] * sizes[path]

		with _sources(build):
			values = [gun_shop_cards.get_count({"card": c})["value"] for c in ("pending_orders", "web_orders", "pending_orders")]
		self.assertEqual(values, [6, 2, 6])
		self.assertEqual(sorted(builds), sorted(sizes))                              # each queue once, Woo shared by two cards
		gun_shop_cards.clear_cache()
		with _sources(build):
			gun_shop_cards.get_count({"card": "web_orders"})
		self.assertEqual(builds.count(gun_shop_cards.WOO_SOURCE), 2)

	def test_the_card_count_is_the_row_count_of_the_list_it_opens(self):
		from ffl_core.api.manual_order import list_pending_dispositions
		self.assertEqual(gun_shop_cards.get_count({"card": "pending_orders"})["value"],
			len(list_pending_dispositions()) + gun_shop_cards._queue(gun_shop_cards.WOO_SOURCE)
			+ gun_shop_cards._queue(gun_shop_cards.GUNBROKER_SOURCE))

	def _distributor_order(self, lines=({"ack_qty": 1},), **f):
		doc = frappe.get_doc({"doctype": "Distributor Order", "distributor": "X", "order_type": "Stocking", **f,
			"lines": [dict(line) for line in lines]})
		doc.name = frappe.generate_hash(length=10)
		doc.db_insert()
		doc.set_parent_in_children()
		for i, line in enumerate(doc.lines, 1):
			line.name, line.idx = frappe.generate_hash(length=10), i
			line.db_insert()

	def test_to_receive_counts_orders_with_units_still_to_arrive(self):
		"""The Receive bridge's rule: store-bound, not a dead state, a line the house is sending that is
		neither booked in nor killed."""
		before = gun_shop_cards.get_count({"card": "to_receive"})["value"]
		self._distributor_order(status="Placed")                                                          # counts
		self._distributor_order(status="Partially Shipped", lines=({"ack_qty": 2, "received_qty": 1},))  # counts
		self._distributor_order(status="Complete", order_type="Procure To Store", lines=({"invoiced_qty": 1},))  # counts
		self._distributor_order(status="Complete")                                                        # stocking, shipped, not in: counts
		self._distributor_order(status="Complete", lines=({"ack_qty": 1, "received_qty": 1},))            # in
		self._distributor_order(status="Complete", lines=({"ack_qty": 1, "line_status": "Killed"},))      # every line killed
		self._distributor_order(status="Placed", lines=({"ack_qty": 1, "substituted_qty": 1},))           # covered from the shelf
		self._distributor_order(status="Placed", order_type="Dropship Firearm")                           # goes to the customer
		for dead in ("Draft", "Queued", "Cancelled", "Error"):
			self._distributor_order(status=dead)
		gun_shop_cards.clear_cache()
		self.assertEqual(gun_shop_cards.get_count({"card": "to_receive"})["value"] - before, 4)

	def test_to_receive_is_what_receive_goods_lists_as_incoming(self):
		from ffl_integrations.distributor.receive_bridge import list_incoming
		self._distributor_order(status="Complete")
		self._distributor_order(status="Placed", lines=({"ack_qty": 2, "received_qty": 2},))
		listed = {o["do_name"] for g in list_incoming()["groups"] for o in g["orders"] if o.get("do_name")}
		self.assertEqual(gun_shop_cards.get_count({"card": "to_receive"})["value"], len(listed))

	def test_report_cards_are_cached_too(self):
		execute = MagicMock(return_value=([], [1, 2]))
		self.assertEqual([gun_shop_cards._report("transfer_pickups", execute) for _ in range(2)], [2, 2])
		self.assertEqual(execute.call_count, 1)

	def test_counter_roles_only_and_unknown_cards_refused(self):
		with self.assertRaises(frappe.ValidationError):
			gun_shop_cards.get_count({"card": "nope"})
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			gun_shop_cards.get_count({"card": "pending_4473"})

	def test_a_card_counts_only_for_who_may_open_what_it_links_to(self):
		"""A Number Card is shown to anyone who can read its document type, so the endpoint answers 0
		(not an error) for a user the page or report behind the card turns away."""
		self._distributor_order(status="Placed")
		purchaser = _user("gs-purchase@example.com", "Purchase User")
		stocker = _user("gs-stock@example.com", "Stock User")
		self.addCleanup(frappe.set_user, "Administrator")

		frappe.set_user(purchaser)               # Distributor Orders page: yes; the 4473 report and counter queue: no
		self.assertGreaterEqual(gun_shop_cards.get_count({"card": "to_receive"})["value"], 1)
		for card in ("pending_4473", "transfer_pickups", "pending_orders", "web_orders"):
			self.assertEqual(gun_shop_cards.get_count({"card": card})["value"], 0, card)

		frappe.set_user(stocker)                 # the two reports: yes; Distributor Orders and the counter queue: no
		self.assertEqual(gun_shop_cards.get_count({"card": "to_receive"})["value"], 0)
		self.assertEqual(gun_shop_cards.get_count({"card": "pending_orders"})["value"], 0)
		self.assertEqual(gun_shop_cards.get_count({"card": "pending_4473"})["route"][0], "query-report")

	def test_web_orders_card_follows_the_woo_switch(self):
		ws = frappe.get_doc("Workspace", "Gun Shop")

		def on_page():
			return [c.number_card_name for c in frappe.get_doc("Workspace", ws.name).number_cards], [
				b["data"]["number_card_name"] for b in json.loads(frappe.get_doc("Workspace", ws.name).content)
				if b["type"] == "number_card"]

		with _switches({"Woo Online Order"}):
			gs.sync_attention_cards()
			self.assertFalse(gs.sync_attention_cards())
			self.assertNotIn("Web Orders", on_page()[0] + on_page()[1])
		with _switches():
			self.assertTrue(gs.sync_attention_cards())
			self.assertFalse(gs.sync_attention_cards())
			rows, blocks = on_page()
			self.assertEqual(blocks.count("Web Orders"), 1)
			self.assertEqual(blocks[blocks.index("To Receive") + 1], "Web Orders")
			self.assertEqual(rows.count("Web Orders"), 1)


class TestWorkspacePatch(FrappeTestCase):
	"""rewrite_gun_shop_workspace brings the old 18-button page to the new layout."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.addCleanup(frappe.db.rollback)
		self.shipped = _app_json("ffl_core", "workspace", "gun_shop", "gun_shop.json")
		self._seed_old_layout()

	def _seed_old_layout(self):
		old = ["Open POS", "Create Order", "Pending Order", "Receive Goods", "Distributor Orders", "New Listing",
			"Add Customer", "Firearms In Stock", "Import from FastBound", "Cash Drawer"]
		ext = ["Consignment Out", "Dealer Orders"]
		ws = frappe.get_doc("Workspace", "Gun Shop")
		ws.set("shortcuts", [{"label": lbl, "type": "URL", "url": f"/app/{frappe.scrub(lbl)}", "color": "Blue"} for lbl in old + ext])
		ws.set("links", [{"type": "Card Break", "label": "Sales"}, {"type": "Link", "label": "POS Invoice", "link_type": "DocType", "link_to": "POS Invoice"}])
		ws.set("number_cards", [])
		blocks = [{"id": f"sc_{frappe.scrub(l)}", "type": "shortcut", "data": {"shortcut_name": l, "col": 4}} for l in old + ext]
		blocks.append({"id": "header_lists", "type": "header", "data": {"text": "Lists & Reports", "col": 12}})
		ws.content = json.dumps(blocks)
		ws.flags.ignore_permissions = True
		ws.save(ignore_permissions=True)

	def _state(self):
		ws = frappe.get_doc("Workspace", "Gun Shop")
		return ([s.label for s in ws.shortcuts], ws.content, [l.label for l in ws.links], [c.number_card_name for c in ws.number_cards], ws.modified)

	def test_rewrites_keeps_extension_tiles_at_the_end_and_is_idempotent(self):
		from ffl_core.patches.rewrite_gun_shop_workspace import execute
		execute()
		shortcuts, content, links, cards, modified = self._state()
		self.assertEqual(shortcuts, ["Open POS", "Create Order", "Receive Goods", "Add Customer", "Consignment Out", "Dealer Orders"])
		blocks = json.loads(content)
		self.assertIn("Needs attention", blocks[0]["data"]["text"])
		self.assertNotIn("Lists & Reports", content)
		self.assertNotIn("Import from FastBound", content)
		self.assertEqual([b["data"]["shortcut_name"] for b in blocks if b["type"] == "shortcut"][-2:], ["Consignment Out", "Dealer Orders"])
		self.assertEqual(links, [l["label"] for l in self.shipped["links"]])
		self.assertEqual(cards, [c["number_card_name"] for c in self.shipped["number_cards"]])

		execute()
		self.assertEqual(self._state(), (shortcuts, content, links, cards, modified))     # nothing written the second time

	def test_charts_quick_lists_and_custom_cards_survive_the_rewrite_at_the_end(self):
		"""What the shop or an extension put on the page by hand is not the core's to drop."""
		from ffl_core.patches.rewrite_gun_shop_workspace import execute
		frappe.get_doc({"doctype": "Dashboard Chart", "chart_name": "GS Test Chart", "chart_type": "Count",
			"document_type": "Note", "based_on": "creation", "timeseries": 1, "time_interval": "Daily",
			"timespan": "Last Month", "type": "Line", "is_public": 1, "filters_json": "[]"}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Number Card", "label": "GS Test Card", "type": "Document Type", "document_type": "Note",
			"function": "Count", "is_public": 1}).insert(ignore_permissions=True)
		ws = frappe.get_doc("Workspace", "Gun Shop")
		ws.append("charts", {"chart_name": "GS Test Chart", "label": "GS Test Chart"})
		ws.append("quick_lists", {"document_type": "Note", "label": "GS Notes"})
		ws.append("number_cards", {"number_card_name": "GS Test Card", "label": "GS Test Card"})
		blocks = json.loads(ws.content) + [
			{"id": "gs_chart", "type": "chart", "data": {"chart_name": "GS Test Chart", "col": 6}},
			{"id": "gs_ql", "type": "quick_list", "data": {"quick_list_name": "GS Notes", "col": 4}},
			{"id": "gs_nc", "type": "number_card", "data": {"number_card_name": "GS Test Card", "col": 4}}]
		ws.content = json.dumps(blocks)
		ws.save(ignore_permissions=True)

		execute()
		after = self._state()
		ws = frappe.get_doc("Workspace", "Gun Shop")
		self.assertEqual([c.label for c in ws.charts], ["GS Test Chart"])
		self.assertEqual([q.label for q in ws.quick_lists], ["GS Notes"])
		self.assertEqual([c.number_card_name for c in ws.number_cards],
			[c["number_card_name"] for c in self.shipped["number_cards"]] + ["GS Test Card"])
		content = json.loads(ws.content)
		self.assertEqual([b["id"] for b in content][-3:], ["gs_chart", "gs_ql", "gs_nc"])      # after the extension tiles
		self.assertEqual([b["data"]["shortcut_name"] for b in content if b["type"] == "shortcut"][-2:], ["Consignment Out", "Dealer Orders"])

		execute()
		self.assertEqual(self._state(), after)                                                  # second run writes nothing
		self.assertEqual([c.label for c in frappe.get_doc("Workspace", "Gun Shop").charts], ["GS Test Chart"])

	def test_shipstation_settings_lands_on_the_settings_workspace_once(self):
		from ffl_core.patches.add_shipstation_settings_to_settings_workspace import execute
		ws = frappe.get_doc("Workspace", "Settings")
		ws.set("links", [l for l in ws.links if l.link_to != "ShipStation Settings"])
		ws.save(ignore_permissions=True)
		execute()
		execute()
		links = frappe.get_doc("Workspace", "Settings").links
		self.assertEqual([l.link_to for l in links].count("ShipStation Settings"), 1)
		at = next(i for i, l in enumerate(links) if l.link_to == "ShipStation Settings")
		self.assertEqual((links[at - 1].type, links[at - 1].label), ("Card Break", "POS & Sales"))

	def test_the_shipped_workspace_json_drops_the_old_page(self):
		self.assertNotIn("Lists & Reports", self.shipped["content"])
		self.assertNotIn("Import from FastBound", [s["label"] for s in self.shipped["shortcuts"]])
		self.assertEqual([s["label"] for s in self.shipped["shortcuts"]], ["Open POS", "Create Order", "Receive Goods", "Add Customer"])
		self.assertEqual([l["label"] for l in self.shipped["links"] if l["type"] == "Card Break"], ["Buying", "Inventory", "FFL", "Money"])
		self.assertEqual(self.shipped["modified"], "2026-07-02 21:30:00.000000")     # never bumped: import_doc would re-insert
		self.assertIn("ShipStation Settings", [l["label"] for l in _app_json("ffl_core", "workspace", "settings", "settings.json")["links"]])

	def test_each_group_card_is_title_then_its_own_links(self):
		"""Narrow screens: a Card block is one unit, so a group's title can never be separated from its buttons."""
		blocks = json.loads(self.shipped["content"])
		cards = [b["data"]["card_name"] for b in blocks if b["type"] == "card"]
		self.assertEqual(cards, ["Buying", "Inventory", "FFL", "Money"])
		self.assertEqual({l["label"] for l in self.shipped["links"] if l["type"] == "Card Break"}, set(cards))
		self.assertTrue(all(b["data"]["col"] == 3 for b in blocks if b["type"] == "card"))
