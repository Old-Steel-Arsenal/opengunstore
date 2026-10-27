"""One-shot — write Require Agreements at Sale (on) onto existing FFL Settings.

A Single applies docfield defaults only while it has no stored rows, so on every
existing site the new Check would show unticked on the form (the reads use
``single_flag`` and are already on). Only written when no value is stored, so a
store's choice stays. Fresh installs take the docfield default. Idempotent.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import single_raw


def execute() -> None:
	if single_raw("FFL Settings", "enforce_sale_agreements") in (None, ""):
		frappe.db.set_single_value("FFL Settings", "enforce_sale_agreements", 1)
