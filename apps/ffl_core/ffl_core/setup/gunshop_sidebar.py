"""Per-store shape of the Gun Shop shell: what the shipped sidebar and home page leave out or add.

The Gun Shop `Sidebar` (setup/sidebar.py) is app content, the same on every store. Three things
differ per store, and none of them may touch that content or the next migrate undoes it:

* an integration the store has switched off (Woo, GunBroker, card processing) hides its sidebar link
  (and its section when nothing is left in it) and, for Woo, the Web Orders card on the home page;
* an extension the store has switched on contributes its sidebar section, declared as data in the
  extension's hooks.py (``gunshop_sidebar_items``, docs/extensions/hooks.md).

Frappe's own place for a site's arrangement is the *site layer* of ``Custom Sidebar``
(module FFL Core, no user). :func:`sync_site_layer` writes exactly that layer from the current
switches, so it is idempotent and a toggle back leaves it clean. Layer semantics
(frappe/desk/layers.py) that shape the writer:

* a layer moves only the rows it names; app rows it never names still show, so an item the app
  adds later reaches a store whose layer hides something;
* a layer naming base rows is an *arrangement*: its rows come first, the unnamed ones after. Hiding
  alone needs no position, so it stores only the hidden rows. Extension sections must sit before
  Settings, which a lone append cannot say, so then the layer names the whole sidebar in order
  (a row the app adds later trails Settings until the next migrate rebuilds the layer).

The layer is owned by this module: every migrate and every switch rebuilds it. Rows it does not
recognise (added by Frappe for a new workspace, say) are carried along at the end.
"""
from __future__ import annotations

import json

import frappe
from frappe.desk.doctype.custom_sidebar.custom_sidebar import SITE_LAYER, get_customization, shape_row
from frappe.desk.doctype.sidebar.sidebar import is_linked, item_key

from ffl_core.extensions import effective
from ffl_core.setup.sidebar import SIDEBAR_NAME, SETTINGS_LINK, gun_shop_items, item_dicts
from ffl_core.utils import commit

WORKSPACE = "Gun Shop"
WEB_ORDERS_CARD = "Web Orders"
SAVEPOINT = "gunshop_sidebar_sync"
WRITTEN_DEFAULT = "gunshop_sidebar_extension_rows"      # see _written()
ATTENTION_ANCHOR = "To Receive"          # the card Web Orders follows when it is put back
_SWITCH_DOCTYPES = ("WooCommerce Settings", "GunBroker Settings", "Payroc Settings")


def integrations_off() -> set[str]:
	"""The sidebar ``link_to`` of every integration this store has switched off, each by the
	integration's own enabled check. An app that is not installed counts as off."""
	apps = frappe.get_installed_apps()
	off: set[str] = set()
	if "ffl_integrations" not in apps:
		off |= {"GunBroker Order", "Card Terminal Payment"}
	else:
		from ffl_integrations.integration_gate import card_processor_enabled, gunbroker_enabled

		if not gunbroker_enabled():
			off.add("GunBroker Order")
		if not card_processor_enabled():
			off.add("Card Terminal Payment")
	if "ffl_woo_sync" not in apps:
		off.add("Woo Online Order")
	else:
		from ffl_woo_sync.woocommerce.tasks import _site_sync_enabled

		if not _site_sync_enabled("retail"):
			off.add("Woo Online Order")
	return off


def extension_rows() -> list[dict]:
	"""The sidebar rows of every extension that is effectively on, in registry order."""
	rows: list[dict] = []
	for ext in frappe.get_all("POS Extension", fields=["name", "enabled", "installed_on_site", "compatible"], order_by="name"):
		if effective(ext):
			rows += [dict(r) for r in frappe.get_hooks("gunshop_sidebar_items", app_name=ext.name) or []]
	return rows


def _hidden_flags(base: list[dict], off: set[str]) -> list[bool]:
	"""One flag per base row: the link is off, or it is a section whose links are all off."""
	flags = [r.get("link_to") in off for r in base]
	for i, row in enumerate(base):
		if row.get("type") != "Section Break":
			continue
		kids = []
		for j in range(i + 1, len(base)):
			if not base[j].get("child"):
				break
			kids.append(j)
		if kids and all(flags[j] for j in kids):
			flags[i] = True
	return flags


def mirror_shells(base: list[dict], own_module: str) -> dict[str, list[dict]]:
	"""module -> rows of every sidebar that mirrors the Gun Shop's (``mirror_gun_shop_sidebar``): an
	extension module's own sidebar, which starts with the Gun Shop's links. Found by content (its
	first row is the Gun Shop's first row), so nothing has to register. A shipped (standard) sidebar
	is its app's to arrange and is skipped, and so is ``own_module`` (the Gun Shop's own layer is
	built on the shipped sidebar, never on a copy in the same module)."""
	first = {k: base[0].get(k) for k in ("type", "link_type", "link_to")}
	shells: dict[str, list[dict]] = {}
	for name in frappe.get_all("Sidebar Item", filters={"parenttype": "Sidebar", "idx": 1, **first}, pluck="parent"):
		module, standard = frappe.db.get_value("Sidebar", name, ["module", "standard"]) or (None, 1)
		if module and module != own_module and not standard and name != SIDEBAR_NAME:
			shells[module] = item_dicts(frappe.get_doc("Sidebar", name))
	return shells


def _written() -> dict[str, list[str]]:
	"""module -> keys of the extension rows the last sync wrote there, kept as a global default so
	that a row is still recognised as ours after its extension is gone and its hook with it. Read
	from the table, not through frappe.defaults: its cached copy outlives a write that was rolled
	back (``_guarded``, a test), and this must say what the layers in the database hold."""
	raw = frappe.db.get_value("DefaultValue", {"parent": "__default", "defkey": WRITTEN_DEFAULT}, "defvalue")
	try:
		return json.loads(raw) if raw else {}
	except ValueError:
		return {}


def sync_site_layer() -> bool:
	"""Rebuild the site layer of the Gun Shop sidebar -- and of every extension module that
	mirrors it (``mirror_gun_shop_sidebar``) -- from the integration switches and the extensions
	that are on. Idempotent. True when a layer changed."""
	module = frappe.db.get_value("Sidebar", SIDEBAR_NAME, "module")
	base = gun_shop_items()
	if not module or not base:
		return False

	off = integrations_off()
	ext = extension_rows()
	previous = _written()
	shells = {module: base, **mirror_shells(base, module)}
	changed = any([_sync_layer(mod, rows, off, ext, set(previous.get(mod, []))) for mod, rows in shells.items()])
	written = {mod: [item_key(r) for r in ext] for mod in shells}
	if written != previous:
		frappe.defaults.set_global_default(WRITTEN_DEFAULT, json.dumps(written, sort_keys=True))
	return changed


def _sync_layer(module: str, base: list[dict], off: set[str], ext_rows: list[dict], written: set[str]) -> bool:
	"""One module's site layer. ``base`` is the module's sidebar rows, ``written`` the extension row
	keys the previous sync put in this layer."""
	def ref(row: dict, hidden: bool) -> dict:
		# A reference names the base row and states whether it is hidden; it carries no label or
		# icon, so a later rename in the shipped JSON is not frozen out by this layer.
		named = {k: v for k, v in row.items() if k not in ("label", "icon")}
		return shape_row({**named, "key": None if is_linked(row) else item_key(row), "hidden": int(hidden), "added": 0})

	def added(row: dict) -> dict:
		return shape_row({**row, "key": item_key(row), "hidden": 0, "added": 1})

	base_keys = {item_key(r) for r in base}
	hidden = _hidden_flags(base, off)
	# A mirror module also lists the extension's own rows (extra_items) after Settings: an extension
	# row the base already holds is moved into the section, not added a second time.
	ext = [ref(r, False) if item_key(r) in base_keys else added(r) for r in ext_rows]
	moved = {item_key(r) for r in ext_rows} & base_keys
	ours = written | {item_key(r) for r in ext_rows}

	existing = get_customization(module, SITE_LAYER)
	doc = frappe.get_doc("Custom Sidebar", existing.name) if existing else None
	# Added rows the layer holds that are not an extension row of ours (Frappe adds one for a new
	# workspace, an admin adds others) are carried along at the end rather than dropped. Ours are
	# the rows this sync names or the last one wrote -- the hook that declared them is gone once
	# the extension is uninstalled. A reference to a row the base no longer has names nothing and goes.
	foreign = [shape_row(r.as_dict()) for r in doc.sidebar_items if r.added and item_key(r) not in ours] if doc else []

	if ext or foreign:
		# Positions matter: name the whole sidebar in order, extension sections before Settings.
		rows: list[dict] = []
		for row, h in zip(base, hidden):
			if item_key(row) in moved:
				continue
			if row.get("link_type") == "Workspace" and row.get("link_to") == SETTINGS_LINK:
				rows += ext
			rows.append(ref(row, h))
		if not any(r.get("link_to") == SETTINGS_LINK for r in base):
			rows += ext
	else:
		rows = [ref(row, True) for row, h in zip(base, hidden) if h]
	rows += foreign

	if not rows:
		if doc:
			frappe.delete_doc("Custom Sidebar", doc.name, ignore_permissions=True, force=True)
			return True
		return False

	doc = doc or frappe.new_doc("Custom Sidebar").update({"module": module, "user": SITE_LAYER})
	old = [_ident(r) for r in doc.sidebar_items]
	doc.set("sidebar_items", [])
	for row in rows:
		doc.append("sidebar_items", row)
	doc._set_defaults()      # what save would fill in (e.g. open_in_new_tab), so rows compare like for like
	if old == [_ident(r) for r in doc.sidebar_items] and not doc.is_new():
		return False
	doc.save(ignore_permissions=True)
	return True


def _ident(row) -> tuple:
	"""Everything a layer row says, comparable: all that ``shape_row`` keeps of it, a blank in any
	spelling (None, "", 0) counting as the same, a linked row being identified by its columns (a
	row that links nowhere has no ``link_type``, whatever default the column fills in)."""
	shaped = shape_row(row.as_dict())
	shaped["key"] = item_key(row)
	if not is_linked(row):
		shaped["link_type"] = None
	return tuple(sorted((k, v or None) for k, v in shaped.items()))


def sync_attention_cards() -> bool:
	"""The Web Orders card on the home page follows the Woo switch. True when the page changed."""
	from ffl_core.workspace_shortcuts import ensure_number_card, remove_number_card

	if "Woo Online Order" in integrations_off():
		return remove_number_card(WEB_ORDERS_CARD, workspace=WORKSPACE)
	return ensure_number_card(WEB_ORDERS_CARD, anchor=ATTENTION_ANCHOR, block_id="nc_web_orders", workspace=WORKSPACE)


def sync() -> None:
	"""Everything this module derives from the switches. Safe to call from any hook."""
	sync_site_layer()
	sync_attention_cards()


def ensure() -> None:
	"""after_install / after_migrate."""
	sync()
	commit()


def _guarded(what: str, fn) -> None:
	"""Run ``fn`` so that a failure costs the navigation, never the action that triggered it: its
	writes are undone back to a savepoint (no half-written sidebar or home page) and the failure
	goes to the Error Log. The next migrate rebuilds whatever this left out."""
	frappe.db.savepoint(SAVEPOINT)
	try:
		fn()
	except Exception:
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.log_error(title=f"Gun Shop sidebar: {what} failed")
	else:
		frappe.db.release_savepoint(SAVEPOINT)


def on_settings_update(doc=None, method=None) -> None:
	"""doc_events on_update of the three integration Settings: the store flipped a switch. Saving the
	switch must not depend on the sidebar following it."""
	_guarded("sync after a Settings save", sync)


def on_extension_toggled(app: str, enabled: bool) -> None:
	"""hooks ``gunshop_extension_toggled``: an extension came on or went off."""
	_guarded(f"sync after {app} was switched", sync_site_layer)


def on_app_change(app_name: str | None = None) -> None:
	"""hooks after_app_install / after_app_uninstall (after ``extensions.registry.on_app_change`` has
	brought the registry in step): an extension came or went with its hook rows."""
	_guarded(f"sync after app change ({app_name})", sync)        # ffl_woo_sync coming or going moves the Web Orders card
