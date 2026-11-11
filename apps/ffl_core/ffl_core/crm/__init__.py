"""Frappe CRM on the POS.

Frappe CRM (app ``crm``) is installed, unmodified, on every site next to ffl_core
(``required_apps``). This package wires it to the register:

* every Item is a CRM Product priced as the register charges it (``item``);
* a CRM edit to such a product reaches the Item field by field, a price only through
  ``ffl_core.pricing.set_regular_price`` and only by a price approver (``product``);
* CRM Deal lines are re-priced from the register, won and lost deals left alone (``deal``);
* a Quotation takes the store's default sales tax and is never submitted (``quotation``);
* incoming staff mail goes on the customer's open deal or becomes a lead (``mail``, ``deals``);
* a website inquiry becomes a note, and the gun asked about a product line, on the customer's deal or a lead
  (``inquiries``); a GunBroker member's question is one lead per conversation (``gunbroker``, ``mail``);
* a deal links to a POS customer and opens as a Create Order draft (``customer``), and the
  invoices made from it write back to the deal (``orders``), behind one switch (:func:`pos_link_on`);
* install / migrate plumbing, ERPNext CRM Settings written field by field (``setup``).

CRM's own Item → Product sync is skipped per save (``doc.flags.ignore_crm_sync``) and its
Product → Item push is off (ERPNext CRM Settings › ``sync_products`` = 0): these hooks replace both.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import single_flag

# Error Log titles start with this.
TITLE = "CRM"


def installed() -> bool:
	"""Whether Frappe CRM is installed on this site. It is on every site the deploy has run on; on
	the first deploy of the release that brought it, the new code runs for a moment before
	``bench install-app crm`` (deploy/prod/auto_deploy.sh): every hook that touches a CRM table
	or field stands down until then."""
	return "crm" in frappe.get_installed_apps()


def pos_link_on() -> bool:
	"""Lead Mail Settings › crm_pos_link. Read from the Singles table: the first deploy of a release
	runs its code for a moment before the migrate adds the field, and a hook must not throw over that."""
	return installed() and single_flag("Lead Mail Settings", "crm_pos_link", default=0)


def staff_default_app(doc, method=None):
	"""User validate (after the controller sets ``user_type``): a new staff account opens the POS.

	With Frappe CRM on the site, ``/crm`` is an app outside the desk, so Frappe's
	``apps.get_default_path`` sends a System User with no default app to the ``/apps`` picker,
	and that is where the welcome email's set-password link lands. A default app per staff user
	keeps them on the POS. Not the System Settings default: that path also serves Website Users
	(dealers), who must keep their own home page. Set when an account becomes a System User: on
	insert, or on the later save that gives desk roles to an account first saved without them (a
	Website User then). Staff who already were System Users are left alone, and a user may still
	pick another default on the apps screen."""
	if doc.user_type != "System User" or doc.get("default_app") or not doc.meta.has_field("default_app"):
		return
	if not doc.is_new():
		before = doc.get_doc_before_save()
		if not before or before.user_type == "System User":
			return
	doc.default_app = "ffl_core"
