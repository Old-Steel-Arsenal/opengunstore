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

from ffl_core.utils import single_flag

SETTINGS = "Marketing Settings"
CONTACT = "Marketing Contact"
EVENT = "Marketing Event"
REQUEST = "Marketing Request"


def enabled() -> bool:
	"""Marketing Settings › Enabled (off on a new site)."""
	return single_flag(SETTINGS, "enabled")
