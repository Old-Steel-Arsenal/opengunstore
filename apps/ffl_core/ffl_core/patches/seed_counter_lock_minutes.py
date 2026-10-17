"""One-shot — turn the idle counter lock on (5 minutes) on stores that already had FFL Settings.

``counter_lock_minutes`` came in with the staff switch (PR #32). A store whose FFL Settings was
saved after the field arrived stored it as 0 ("never"), so the lock stayed off instead of the
intended default. The field is new, so no store has chosen 0 yet: a blank or 0 becomes 5. Any
other value is left alone. Fresh installs get the default from ``staff_switch.ensure_lock_default``
(patches don't run there). Idempotent.
"""
import frappe

from ffl_core.staff_switch import DEFAULT_LOCK_MINUTES
from ffl_core.utils import single_raw


def execute():
	if single_raw("FFL Settings", "counter_lock_minutes") in (None, "", "0"):
		frappe.db.set_single_value("FFL Settings", "counter_lock_minutes", DEFAULT_LOCK_MINUTES)
