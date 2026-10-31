"""after_migrate seed: the Lipsey's Distributor row, disabled until the operator
enables it in the hub. Create-once: operator edits are never overwritten.

Polling is on: a Lipsey's order status is worked out on this site from the
placement answer and the mirrored shipping rows (no call to Lipsey's), so the
30-minute poll costs nothing at the house."""
from __future__ import annotations

import frappe

from ffl_integrations.distributor.policy import ensure_distributor_row

DISTRIBUTOR_NAME = "Lipsey's"
PROVIDER = "lipseys"


def ensure_distributor() -> bool:
	return ensure_distributor_row(DISTRIBUTOR_NAME, "Lipsey's, LLC", PROVIDER, poll=1, priority=3)


def run() -> dict:
	out = {"distributor_seeded": ensure_distributor()}
	frappe.db.commit()
	return out
