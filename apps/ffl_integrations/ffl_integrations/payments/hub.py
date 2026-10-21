"""Payments hub — the Settings-page entry for EVERY card processor.

One custom page (``/app/payment-hub``) replaces the per-processor Settings
shortcut, the same shape ``distributor-hub`` gave the wholesale houses: one
card per processor with its enable switch, charge-side health and a
connection probe. Credentials stay in each processor's own Single doctype
(``settings_doctype``) — the hub only links to it, so the next processor is
one ``gunshop_card_processors`` entry plus its own Single, and the workspace
card never changes again.

The cards come from the card processor registry (``payments.registry``, hook
``gunshop_card_processors``): Payroc from this app, and any processor an
extension adds. At most one is switched on — ``toggle`` refuses a second.

Permission model mirrors Payroc Settings: System Manager only — the enable
switch arms live money movement, so it answers to the same audience as the
credentials it sits next to.
"""

from __future__ import annotations

import frappe
from frappe import _

from ffl_integrations.payments import registry
from ffl_integrations.payments.setup import MODE_OF_PAYMENT_NAME

ROLES = ("System Manager",)


SIDEBAR_SYNC = "ffl_core.setup.gunshop_sidebar.on_settings_update"


def _hooked_on_update(doctype: str) -> list:
	handlers = (frappe.get_hooks("doc_events") or {}).get(doctype, {}).get("on_update") or []
	return [handlers] if isinstance(handlers, str) else list(handlers)


def _entry(code: str):
	p = registry.get_processor(code)
	if p is None:
		frappe.throw(_("Unknown payment processor '{0}'.").format(code))
	return p


@frappe.whitelist()
def overview() -> dict:
	"""Everything the payment-hub page renders, in one round trip.

	``enabled`` is each processor's own ``enabled()`` (Payroc: ``single_flag``,
	raw ``tabSingles`` row, docfield default when absent) — the same defence the
	integration gate uses, so a site that never saved the settings form reads ON,
	not OFF.

	Returns:
	    {processors: [{code, label, kind, settings_doctype, mode_of_payment,
	    enabled, ready, can_write, health: [{label, at}]}], can_write}.
	    ``ready`` is enabled AND credentials present — the switch can be on
	    while the key is missing, and the card has to show that as its own
	    state, not as "working".

	Permissions:
	    System Manager (mirrors Payroc Settings).
	"""
	frappe.only_for(ROLES)
	rows = []
	for p in registry.processors():
		can_write = bool(frappe.has_permission(p.settings_doctype, ptype="write"))
		rows.append({
			"code": p.code,
			"label": p.label,
			"kind": p.kind,
			"settings_doctype": p.settings_doctype,
			"mode_of_payment": MODE_OF_PAYMENT_NAME,
			"enabled": 1 if p.enabled() else 0,
			"ready": bool(p.ready()),
			"can_write": can_write,
			"health": [{
				"label": label,
				"at": frappe.db.get_single_value(p.settings_doctype, fieldname),
			} for label, fieldname in p.health_fields],
		})
	return {"processors": rows, "can_write": any(r["can_write"] for r in rows)}


@frappe.whitelist(methods=["POST"])
def toggle(processor: str, value) -> dict:
	"""Flip one processor's enable switch, through the real settings save.

	Deliberately ``doc.save()``, not ``db_set``: the settings form makes the
	credential fields mandatory while the switch is on (a store cannot arm a
	processor it holds no key for), and a hub that wrote the flag straight to
	the table would be a side door around exactly that rule. Disabling never
	trips it — credentials are only demanded while ``enabled`` stays 1. Only one
	processor takes cards at a time: switching one on while another is on is
	refused (switch the other off first).

	Args:
	    processor: Registry code (e.g. ``"payroc"``).
	    value: Truthy/falsy.

	Returns:
	    {code, enabled: 0|1}.

	Permissions:
	    System Manager, plus the doc-level write check ``doc.save()`` runs.
	"""
	frappe.only_for(ROLES)
	p = _entry(processor)
	on = 1 if frappe.utils.cint(value) else 0
	if on:
		registry.refuse_second_active(p.code)
	doc = frappe.get_doc(p.settings_doctype)
	doc.enabled = on
	doc.save()
	# The Gun Shop sidebar's Card payments link follows whether a card processor is on.
	# A settings Single hooked to the re-shape (Payroc Settings, ffl_core hooks) did it in
	# its own on_update; an extension's Single is not, so the switch asks for it here.
	if SIDEBAR_SYNC not in _hooked_on_update(p.settings_doctype):
		frappe.get_attr(SIDEBAR_SYNC)(doc)
	return {"code": p.code, "enabled": int(doc.enabled)}


@frappe.whitelist()
def test_connection(processor: str) -> dict:
	"""Run the processor's read-only connection probe.

	Args:
	    processor: Registry code (e.g. ``"payroc"``).

	Returns:
	    {checks: [{label, ok, detail}]} — the shape the distributor hub
	    renders, so the two pages share one mental model. ``ok`` is true,
	    false, or null for "not applicable" (e.g. the switch is off, so
	    nothing was probed — that is a state, not a failure).

	Permissions:
	    System Manager.
	"""
	frappe.only_for(ROLES)
	p = _entry(processor)
	return {"checks": p.probe()}
