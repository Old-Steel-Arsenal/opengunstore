"""One-shot — leave Require Manager Approval for Price Reductions OFF on stores that already ran.

The switch is on for a new store (``price_approval.ensure_default`` at install; the docfield
default). A store that predates it keeps selling as it did until a manager turns it on
deliberately: off is written only where no value is stored, so a stated choice stays.
Patches don't run on a fresh install. Idempotent.
"""
from __future__ import annotations

import frappe

from ffl_core.price_approval import SETTING
from ffl_core.utils import single_raw


def execute() -> None:
	if single_raw("FFL Settings", SETTING) in (None, ""):
		frappe.db.set_single_value("FFL Settings", SETTING, 0)
