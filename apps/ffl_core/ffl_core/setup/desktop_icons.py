"""Desktop-grid (Desktop Icon) wiring for the Gun Shop app.

Frappe v16 auto-builds the /desk landing grid from the "Desktop Icon"
doctype. For an app registered via ``add_to_apps_screen`` it creates one
"App" tile (labelled with ``app_title``) and nests a tile for every public
workspace under it. That turns the app tile into a *folder*: clicking it
opens a popup of child tiles instead of going to the workspace.

We want the opposite — a single "Gun Shop" tile that opens
``/desk/gun-shop`` directly. The app tile only behaves as a folder while it
has visible children, so we hide the child-workspace tiles (Settings, …).
They stay reachable from the Gun Shop sidebar (see ``setup.sidebar``).

This reconciler converges to that single tile from ANY prior state, not
just a fresh install — older/production sites carry leftovers that a plain
fresh-install path never sees:
  - the app tile labelled "FFL Core" (created before the app_title rename),
  - a redundant standard Link icon from the now-deleted
    desktop_icon/gun_shop.json fixture (Frappe doesn't drop standard docs
    when their JSON is removed),
  - an auto-generated workspace Link icon nested under the old app folder,
  - the App tile's logo_url still naming a logo file that has since been renamed.
Leaving those produces a stale "FFL Core" folder and/or a duplicate
"Gun Shop" tile after deploy. So we treat the add_to_apps_screen App tile
as the one canonical entry, delete the other home-workspace tiles, and
relabel the App tile to app_title.

Everything here is idempotent and defensive — a failure must never break
``bench migrate`` — and runs from ``after_install`` / ``after_migrate``.
"""
from __future__ import annotations

import frappe
from ffl_core.utils import commit

APP = "ffl_core"
WORKSPACE_MODULE = "FFL Core"
HOME_WORKSPACE = "Gun Shop"


def ensure_gun_shop_desktop_tile() -> None:
	try:
		app_title = (frappe.get_hooks("app_title", app_name=APP) or [APP])[0]

		# The add_to_apps_screen App tile is the canonical /desk entry. (App
		# icons route via `link` = /desk/gun-shop, not `link_to`, so the
		# link_to-based queries below never touch the App tile itself.)
		app_icons = frappe.get_all(
			"Desktop Icon", filters={"app": APP, "icon_type": "App"}, pluck="name"
		)

		if app_icons:
			# 1. Drop every OTHER tile pointing at the home workspace — the old
			#    fixture's standard Link icon and any auto-generated workspace
			#    Link icon — so we don't ship a duplicate or a stale folder.
			for name in frappe.get_all(
				"Desktop Icon",
				filters={"link_to": HOME_WORKSPACE, "icon_type": ["!=", "App"]},
				pluck="name",
			):
				frappe.db.delete("Desktop Icon", {"name": name})

			# 2. Relabel the App tile to the current app_title (older sites have
			#    it as "FFL Core"). Display uses `label`, so a field update is
			#    enough — no doc rename / name collision to worry about.
			# 3. Re-point the tile at the hook's current logo. Frappe copies `logo` into the
			#    tile once, at creation, so renaming the asset left a broken image on sites
			#    whose tile predates the rename (/assets/ffl_core/images/gun_shop.svg).
			screens = frappe.get_hooks("add_to_apps_screen", app_name=APP) or [{}]
			logo = screens[0].get("logo")
			for name in app_icons:
				label, logo_url = frappe.db.get_value("Desktop Icon", name, ["label", "logo_url"])
				if label != app_title:
					frappe.db.set_value("Desktop Icon", name, "label", app_title)
				if logo and logo_url != logo:
					frappe.db.set_value("Desktop Icon", name, "logo_url", logo)
		# else: no App tile (e.g. test-bootstrap fallback created a Link icon as
		# the sole entry) — leave that lone tile in place; just hide children below.

		# 4. Hide child-workspace tiles so the app tile stays a direct link
		#    (no folder popup). They remain reachable from the Gun Shop sidebar.
		child_workspaces = frappe.get_all(
			"Workspace",
			filters={"module": WORKSPACE_MODULE, "name": ["!=", HOME_WORKSPACE]},
			pluck="name",
		)
		for ws in child_workspaces:
			for icon in frappe.get_all(
				"Desktop Icon", filters={"link_to": ws}, pluck="name"
			):
				frappe.db.set_value(
					"Desktop Icon", icon, {"hidden": 1, "parent_icon": None}
				)

		from frappe.desk.doctype.desktop_icon.desktop_icon import clear_desktop_icons_cache

		clear_desktop_icons_cache()
		commit()
	except Exception:
		frappe.log_error(title="ffl_core: ensure_gun_shop_desktop_tile failed")
