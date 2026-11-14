"""Marketing on the POS: who may be emailed, what they asked for, and what happened.

* ``contacts``: the Marketing Contact (one per lowercase email), its consent status and the one
  function that changes it (:func:`contacts.set_status`), and the event log every change writes;
* ``tags``: the slugs the code relies on, and adding / removing / reading Frappe's own tags;
* ``events``: the website's events (``ffl_woo_sync.woocommerce.client_api.web_events``), one
  handler per event type;
* ``migrate``: the one-off import from the shop's previous email plugin (FluentCRM);
* ``setup``: install / migrate plumbing.

Off until Marketing Settings › Enabled is checked (:func:`enabled`).
"""
from __future__ import annotations

import frappe
from frappe.utils import get_url
from frappe.utils.data import get_host_name_from_request

from ffl_core.utils import single_flag

SETTINGS = "Marketing Settings"
CONTACT = "Marketing Contact"
EVENT = "Marketing Event"
REQUEST = "Marketing Request"


def enabled() -> bool:
	"""Marketing Settings › Enabled (off on a new site)."""
	return single_flag(SETTINGS, "enabled")


def has_site_url() -> bool:
	"""True when a link to this site can be built from what is known: the site config's ``host_name``,
	or the host of the web request being answered. Without either Frappe falls back to
	``http://<site name>``, an address nobody can open."""
	return bool(frappe.conf.host_name or frappe.conf.hostname or get_host_name_from_request())


def site_url(path: str = "") -> str:
	"""The address of *path* on this site, for a link in mail (unsubscribe, open pixel, tracked click).
	Mail built outside a web request must check :func:`has_site_url` first (``mailer.sending_address``
	does)."""
	return get_url(path)
