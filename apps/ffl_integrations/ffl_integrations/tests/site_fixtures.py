"""Site-state fixtures this suite must own outright.

Historically these records were left behind by the ffl_core suite running
FIRST on the same CI site, so the modules here that touch them passed by
accident of ordering. The CI split (parallel per-app test jobs) runs this
app's suite on a site where ffl_core's tests have never run — and
``_ensure()``-style helpers skip records that already exist with the WRONG
fields (another module may have created the bare record first). Every helper
here therefore enforces FIELD STATE, not mere existence, and is idempotent
under any module ordering.
"""
from __future__ import annotations

import frappe

# Mirrors ffl_core.tests.test_build_order.DEST_FFL — the licence this suite's
# dealer-transfer / dropship paths point documents at.
DEST_FFL = "1-99-888-01-2D-09999"


def snapshot_single_values(doctype: str, fields, passwords=()):
	"""Snapshot Single-doctype values NOW; returns a restore() for addCleanup /
	addClassCleanup.

	For fixtures that write CONNECTION-level settings (endpoint / client id /
	secret / provider switch): the shared dev site is also a hand-wired live
	environment, and a fixture that saves fake wiring without restoring it
	disconnects the site from the real service on every test run — the
	2026-08-01 incident: `make test` left FFL Settings pointing at
	`api.example.test`, silently emptying the counter's distributor search and
	the ATF remote typeahead until someone re-entered the credentials.

	``passwords`` names Password fields — those read via get_password (the
	__Auth table) and MUST round-trip through doc.save, not tabSingles.

	For plain switches/flags prefer ``ffl_core.tests.settings_sandbox`` instead: it
	writes tabSingles directly, so it can restore a field to "no row at all" (which
	doc.save cannot express and every default-ON switch depends on) and it does not
	commit. This one is for the Password/connection case that needs both."""
	doc = frappe.get_single(doctype)
	plain = {f: doc.get(f) for f in fields}
	secret = {f: (doc.get_password(f, raise_exception=False) or "") for f in passwords}

	def restore():
		d = frappe.get_single(doctype)
		for f, v in plain.items():
			d.set(f, v)
		for f, v in secret.items():
			d.set(f, v)
		d.save(ignore_permissions=True)
		frappe.db.commit()

	return restore


def ensure_atf_ffl_record(ffl_number: str = DEST_FFL) -> str:
	"""The record exists, carries a complete shippable address, and is not
	expired — regardless of which module created it first (same recipe as
	ffl_core.tests.test_build_order's fixture)."""
	if not frappe.db.exists("ATF FFL Record", ffl_number):
		frappe.get_doc({
			"doctype": "ATF FFL Record", "ffl_number": ffl_number,
			"licensee_name": f"Test FFL {ffl_number[-5:]}",
			"expiry_date": frappe.utils.add_days(frappe.utils.nowdate(), 365),
		}).insert(ignore_permissions=True)
	frappe.db.set_value("ATF FFL Record", ffl_number, {
		"street": "100 Test Pkwy", "city": "Springfield", "state": "TX", "zip": "75024",
		"expiry_date": frappe.utils.add_days(frappe.utils.nowdate(), 365),
	}, update_modified=False)
	return ffl_number


def ensure_ffl_supplier(name: str, ffl_number: str = "9-99-999-99-9X-99961",
		supplier_group: str | None = None) -> str:
	"""Supplier exists AND passes ffl_core's firearm-receive gate
	(is_ffl_dealer + ffl_number + acceptable EZ Check). The FFL fields go
	through set_value, not the insert payload, so a bare same-name Supplier
	created earlier by another module gets completed instead of skipped."""
	if not frappe.db.exists("Supplier", name):
		payload = {"doctype": "Supplier", "supplier_name": name}
		if supplier_group and frappe.db.exists("Supplier Group", supplier_group):
			payload["supplier_group"] = supplier_group
		frappe.get_doc(payload).insert(ignore_permissions=True)
	frappe.db.set_value("Supplier", name, {
		"is_ffl_dealer": 1, "ffl_number": ffl_number,
		"ez_check_status": "Verified",
	}, update_modified=False)
	return name


def ensure_credit_card_mop_account() -> None:
	"""The "Credit Card" Mode of Payment carries a default account for every
	company — ERPNext refuses to save any POS Profile whose payment row's MoP
	has none, which is exactly what payments.setup.ensure_mode_of_payment() does
	to every enabled profile. On prod the account predates us;
	a CI site must seed it. Deliberately NOT calling
	ensure_mode_of_payment() here: it wires profiles as a side effect and
	would hit the very validation this fixture exists to satisfy."""
	from ffl_integrations.payments.setup import MODE_OF_PAYMENT_NAME

	if not frappe.db.exists("Mode of Payment", MODE_OF_PAYMENT_NAME):
		frappe.get_doc({
			"doctype": "Mode of Payment",
			"mode_of_payment": MODE_OF_PAYMENT_NAME,
			"type": "General",
			"enabled": 1,
		}).insert(ignore_permissions=True)

	mop = frappe.get_doc("Mode of Payment", MODE_OF_PAYMENT_NAME)
	covered = {row.company for row in (mop.accounts or []) if row.default_account}
	changed = False
	for company in frappe.get_all("Company", pluck="name"):
		if company in covered:
			continue
		account = (
			frappe.db.get_value("Company", company, "default_bank_account")
			or frappe.db.get_value("Company", company, "default_cash_account")
			or frappe.db.get_value("Account",
				{"company": company, "account_type": "Bank", "is_group": 0}, "name")
			or frappe.db.get_value("Account",
				{"company": company, "account_type": "Cash", "is_group": 0}, "name"))
		if account:
			mop.append("accounts", {"company": company, "default_account": account})
			changed = True
	if changed:
		mop.flags.ignore_permissions = True
		mop.save()


def payroc_configured():
	"""A patch under which Payroc counts as configured whenever it is switched on.

	A card processor is ON only when switched on AND configured (payments.registry); the test
	site holds no Payroc key, so tests that stand in for a store running Payroc start this.
	"""
	from unittest.mock import patch

	from ffl_integrations import integration_gate

	return patch("ffl_integrations.integration_gate.payroc_ready",
		side_effect=lambda require_device=False: integration_gate.payroc_enabled())
