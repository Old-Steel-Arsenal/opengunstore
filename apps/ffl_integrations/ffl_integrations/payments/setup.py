"""Card tender post-install / post-migrate setup.

Ensures the "Credit Card" Mode of Payment exists (it ships with ERPNext
by default) and is wired into every enabled POS Profile, so cashiers
see a "Credit Card" tile in POS that routes through the active card
processor (or records plain tender when none is switched on).

Idempotent — safe to re-run on every migrate.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import commit


MODE_OF_PAYMENT_NAME = "Credit Card"
LEGACY_PAYROC_MOP = "Payroc Credit Card"


def ensure_mode_of_payment() -> None:
	# "Credit Card" exists in the default ERPNext seed; recreate only if a
	# site dropped it. Idempotent.
	if not frappe.db.exists("Mode of Payment", MODE_OF_PAYMENT_NAME):
		doc = frappe.get_doc({
			"doctype": "Mode of Payment",
			"mode_of_payment": MODE_OF_PAYMENT_NAME,
			"type": "General",
			"enabled": 1,
		})
		doc.flags.ignore_permissions = True
		doc.insert()

	# Wire it into every enabled POS Profile that doesn't already have it.
	profile_names = frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name")
	for pname in profile_names:
		profile = frappe.get_doc("POS Profile", pname)
		existing = {row.mode_of_payment for row in (profile.payments or [])}
		if MODE_OF_PAYMENT_NAME in existing:
			continue
		profile.append("payments", {
			"mode_of_payment": MODE_OF_PAYMENT_NAME,
			"default": 0,
		})
		profile.flags.ignore_permissions = True
		profile.save()

	# Earlier installs created a separate "Payroc Credit Card" MoP; the
	# integration now keys off the default "Credit Card" tile instead, so
	# delete the orphan to avoid two near-identical tiles in POS.
	if frappe.db.exists("Mode of Payment", LEGACY_PAYROC_MOP):
		# Only safe to drop if no doc is using it — otherwise leave for
		# the operator to migrate manually.
		used = frappe.db.sql(
			"""SELECT 1 FROM `tabSales Invoice Payment`
			   WHERE mode_of_payment=%s LIMIT 1""",
			(LEGACY_PAYROC_MOP,),
		)
		if not used:
			frappe.delete_doc("Mode of Payment", LEGACY_PAYROC_MOP,
				force=1, ignore_permissions=True)

	# ffl_core.utils.commit: real under bench execute / migrate, a no-op inside the
	# test transaction (docs/testing.md rule 1 — this used to be a bare
	# frappe.db.commit(), which made every test that reached it poison the site).
	commit()
