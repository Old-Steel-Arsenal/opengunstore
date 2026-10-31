"""A past invoice family — the 4473 completed, the invoice said Awaiting.

Covers the server-side reset guard (pos_4473.start_4473_for_pos_invoice), the
counter's Refresh 4473 path (pos_4473_recovery.refresh_4473_status) and the
manager redo channel (pos_4473_recovery.manager_redo_4473).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

RECOVERY_ITEM = "RCV4473-GUN"
RECOVERY_CUSTOMER = "_Test RCV4473 Buyer"


def _ensure_recovery_item() -> str:
	"""Serialized firearm item for the recovery tests (own namespace so these
	never share fixtures — or residue — with the multi-gun webhook tests)."""
	if frappe.db.exists("Item", RECOVERY_ITEM):
		return RECOVERY_ITEM
	frappe.get_doc({
		"doctype": "Item", "item_code": RECOVERY_ITEM, "item_name": RECOVERY_ITEM,
		"item_group": "Firearm", "stock_uom": "Nos",
		"is_stock_item": 1, "has_serial_no": 1,
	}).insert(ignore_permissions=True)
	return RECOVERY_ITEM


def _ensure_serial(serial: str, company: str) -> None:
	"""Register the serial WITHOUT moving stock (idempotent).

	These tests never submit a POS Invoice — they exercise the 4473 status
	machinery — and the only thing that reads the serial is
	FFL Disposition._require_serial_belongs_to_item (a Serial No lookup). Doing a
	Material Receipt instead would leave stock ledger entries behind that survive
	the test rollback and collide with the next same-site run (docs/testing.md)."""
	if frappe.db.exists("Serial No", serial):
		return
	sn = frappe.get_doc({
		"doctype": "Serial No",
		"serial_no": serial,
		"item_code": RECOVERY_ITEM,
		"company": company,
	})
	sn.flags.ignore_permissions = True
	sn.insert()


class Pos4473Recovery(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		if not frappe.db.exists("Customer", RECOVERY_CUSTOMER):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": RECOVERY_CUSTOMER,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		from ffl_core.tests.pos_opening import todays_pos_opening
		todays_pos_opening(cls.company)
		_ensure_recovery_item()
		# _probe_client builds a real config; give it credentials to build from.
		# Outbound is blackholed on the dev site and every probe test stubs the
		# client anyway — this only keeps FastBoundConfig.from_settings happy.
		settings = frappe.get_single("FastBound Settings")
		if not settings.account_num:
			settings.account_num = "TEST-ACCOUNT"
		if not settings.get_password("api_key", raise_exception=False):
			settings.api_key = "test-api-key"
		if not settings.base_url:
			# Base URL has no doctype default any more, and from_settings() now
			# fails closed on a blank one — a default would silently point a fresh
			# site at the live bound book. So a fresh site (CI) has nothing here
			# and must say what it wants: an unroutable host, since every probe
			# test stubs the client and nothing should leave the box.
			settings.base_url = "https://fastbound.invalid"
			# Committed below, so it outlives this class and the NEXT `make test` on
			# the same site would find FastBound "configured" and try to push every
			# trade-in receive in ffl_core to a host that does not resolve. Put the
			# blank back when the class is done.
			cls.addClassCleanup(cls._blank_fastbound_base_url)
		settings.flags.ignore_permissions = True
		settings.save()
		frappe.db.commit()

	@staticmethod
	def _blank_fastbound_base_url():
		frappe.db.set_value("FastBound Settings", None, "base_url", "")
		frappe.db.commit()  # the value being undone was committed, so the undo must be too

	def setUp(self):
		super().setUp()
		if getattr(frappe.flags, "currently_saving", None) is None:
			frappe.flags.currently_saving = []
		frappe.local.response = frappe._dict()

	# ------------------------------------------------------------- fixtures

	def _draft_invoice(self, *serials: str, form_4473: str = "", nics: str = "",
			disposition_id: str = "", pdf_url: str = "") -> str:
		"""Draft POS Invoice, one firearm row per serial (one gun per line is
		enforced by ffl_core.compliance.firearm.require_one_firearm_per_line)."""
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = "FFL Test"
		inv.customer = RECOVERY_CUSTOMER
		for ser in serials:
			inv.append("items", {
				"item_code": RECOVERY_ITEM, "qty": 1, "rate": 600,
				"price_list_rate": 600, "serial_no": ser,
				"use_serial_batch_fields": 1, "allow_zero_valuation_rate": 1,
			})
		inv.set_missing_values()
		if form_4473:
			inv.form_4473_status = form_4473
		if nics:
			inv.nics_status = nics
		if disposition_id:
			inv.fastbound_disposition_id = disposition_id
		if pdf_url:
			inv.fastbound_4473_pdf_url = pdf_url
		inv.flags.ignore_permissions = True
		inv.save()
		return inv.name

	def _draft_invoice_without_serial(self) -> str:
		"""The state a firearm cart is in before the 4473 dialog picks serials."""
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = "FFL Test"
		inv.customer = RECOVERY_CUSTOMER
		inv.append("items", {
			"item_code": RECOVERY_ITEM, "qty": 1, "rate": 600,
			"price_list_rate": 600, "use_serial_batch_fields": 1,
			"allow_zero_valuation_rate": 1,
		})
		inv.set_missing_values()
		inv.flags.ignore_permissions = True
		inv.save()
		return inv.name

	def _bundle(self, serial: str) -> str:
		"""A Serial and Batch Bundle holding one gun.

		This is the v16 DEFAULT storage for a serialized line, and the shape the
		whole 4473 suite never had: every fixture above writes
		`use_serial_batch_fields=1` + `serial_no`, so a reader that only looked at
		`row.serial_no` passed every test while seeing NOTHING on a real v16 cart.

		Inserted with ignore_validate on purpose: the bundle's own validate() wants
		a posting voucher and live stock, and nothing under test reads either — the
		code path resolves `entries[].serial_no` through
		ffl_core.serials.serials_for_line.
		"""
		bundle = frappe.get_doc({
			"doctype": "Serial and Batch Bundle",
			"company": self.company,
			"item_code": RECOVERY_ITEM,
			"has_serial_no": 1,
			"type_of_transaction": "Outward",
			"voucher_type": "POS Invoice",
			"entries": [{"serial_no": serial, "qty": -1}],
		})
		bundle.flags.ignore_validate = True
		bundle.flags.ignore_permissions = True
		bundle.insert(ignore_permissions=True, ignore_mandatory=True, ignore_links=True)
		return bundle.name

	def _draft_invoice_with_bundles(self, *serials: str) -> tuple[str, list[str]]:
		"""A draft whose firearm lines carry a Serial and Batch Bundle instead of
		a `serial_no` string.

		Built the ordinary way and then REWRITTEN with db.set_value: saving it in
		the bundle shape would drag ERPNext's own bundle validation (live stock, a
		posting voucher) into a test about how the recovery code READS a row.
		"""
		invoice = self._draft_invoice(*serials)
		bundles: list[str] = []
		for row, serial in zip(frappe.get_doc("POS Invoice", invoice).items, serials):
			bundle = self._bundle(serial)
			bundles.append(bundle)
			frappe.db.set_value("POS Invoice Item", row.name, {
				"serial_no": None,
				"use_serial_batch_fields": 0,
				"serial_and_batch_bundle": bundle,
			}, update_modified=False)
		return invoice, bundles

	def _drop_bundles(self, bundles: list[str]) -> None:
		for bundle in bundles:
			frappe.db.delete("Serial and Batch Entry", {"parent": bundle})
			frappe.db.delete("Serial and Batch Bundle", {"name": bundle})
		frappe.db.commit()

	def _seed_retail_disposition(self, invoice_name: str, serial: str, *,
			fastbound_id: str = "FB-RCV-DISP", submit: bool = True) -> str:
		"""What the form4473.completed webhook books: a Retail Sale disposition
		carrying a fastbound_id (a Retail Sale without one is rejected as a 4473
		bypass by FFL Disposition._reject_manual_retail_sale)."""
		disp = frappe.get_doc({
			"doctype": "FFL Disposition",
			"disposition_date": frappe.utils.now_datetime(),
			"disposition_type": "Retail Sale",
			"company": self.company,
			"customer": RECOVERY_CUSTOMER,
			"item": RECOVERY_ITEM,
			"serial": serial,
			"linked_pos_invoice": invoice_name,
			"form_4473_status": "Completed",
			"nics_status": "Proceed",
			"fastbound_id": fastbound_id,
			"notes": f"POS Invoice {invoice_name}",
		})
		disp.flags.ignore_permissions = True
		disp.insert()
		if submit:
			disp.submit()
		return disp.name

	def _flags(self, invoice_name: str):
		return frappe.db.get_value("POS Invoice", invoice_name, [
			"form_4473_status", "nics_status", "fastbound_disposition_id",
			"fastbound_4473_pdf_url"], as_dict=True)

	def _cleanup(self, invoice_name: str, *serials: str) -> None:
		"""Committed delete — the code under test calls commit(), so a plain
		delete would be swallowed by the test rollback and leave residue that
		collides with the next run (docs/testing.md)."""
		frappe.db.delete("FFL Disposition", {"linked_pos_invoice": invoice_name})
		frappe.db.delete("Comment", {"reference_doctype": "POS Invoice",
			"reference_name": invoice_name})
		frappe.db.delete("POS Invoice Item", {"parent": invoice_name})
		frappe.db.delete("POS Invoice", {"name": invoice_name})
		for ser in serials:
			frappe.db.delete("Serial No", {"name": ser})
		frappe.db.commit()

	# ------------------------------------------------- Task 2: the reset guard

	def test_start_4473_blocks_when_db_already_completed(self):
		"""A past invoice: a stale POS page re-clicking FastBound 4473 must
		NOT reset a completed order back to Pending."""
		from ffl_integrations.fastbound import pos_4473
		ser = "RCV-GUARD-A"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Completed", nics="Proceed",
			disposition_id="FB-DISP-GUARD", pdf_url="https://fb.test/4473.pdf")
		try:
			row = frappe.get_doc("POS Invoice", inv).items[0].name
			with self.assertRaises(frappe.ValidationError):
				pos_4473.start_4473_for_pos_invoice(inv, {row: ser})
			flags = self._flags(inv)
			self.assertEqual(flags.form_4473_status, "Completed")
			self.assertEqual(flags.nics_status, "Proceed")
			self.assertEqual(flags.fastbound_disposition_id, "FB-DISP-GUARD")
			self.assertEqual(flags.fastbound_4473_pdf_url, "https://fb.test/4473.pdf")
		finally:
			self._cleanup(inv, ser)

	def test_start_4473_reads_db_not_the_incoming_doc(self):
		"""The guard must consult the DB: the caller's doc is exactly the stale
		party (its in-memory status still says Pending)."""
		from ffl_integrations.fastbound import pos_4473
		ser = "RCV-GUARD-B"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending")
		try:
			# Webhook-style stamp: db.set_value never touches the loaded doc.
			frappe.db.set_value("POS Invoice", inv, {
				"form_4473_status": "Completed", "nics_status": "Proceed",
			}, update_modified=False)
			row = frappe.get_doc("POS Invoice", inv).items[0].name
			with self.assertRaises(frappe.ValidationError):
				pos_4473.start_4473_for_pos_invoice(inv, {row: ser})
			self.assertEqual(self._flags(inv).form_4473_status, "Completed")
		finally:
			self._cleanup(inv, ser)

	def test_start_4473_still_parks_a_pending_order(self):
		"""The normal path is untouched: a Pending order still parks for 4473."""
		from ffl_integrations.fastbound import pos_4473
		ser = "RCV-GUARD-C"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser)
		try:
			row = frappe.get_doc("POS Invoice", inv).items[0].name
			res = pos_4473.start_4473_for_pos_invoice(inv, {row: ser})
			self.assertTrue(res["ok"])
			self.assertEqual(res["n_firearms"], 1)
			self.assertEqual(self._flags(inv).form_4473_status, "Pending")
		finally:
			self._cleanup(inv, ser)

	# --------------------------------------- Task 3: refresh — read + self-heal

	def test_refresh_reports_completed_without_writing(self):
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-RF-DONE"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Completed", nics="Proceed")
		try:
			with patch.object(recovery, "_probe_fastbound") as probe:
				res = recovery.refresh_4473_status(inv)
			self.assertEqual(res["state"], recovery.STATE_COMPLETED)
			probe.assert_not_called()  # layer 1 short-circuits before any network
		finally:
			self._cleanup(inv, ser)

	def test_refresh_reports_no_serials_before_a_4473_is_started(self):
		"""A firearm sits in the cart but no serial has been picked yet: there is
		nothing to look up, and "still waiting on FastBound" would be a lie."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		inv = self._draft_invoice_without_serial()
		try:
			with patch.object(recovery, "_probe_fastbound") as probe:
				res = recovery.refresh_4473_status(inv)
			self.assertEqual(res["state"], recovery.STATE_NO_SERIALS)
			probe.assert_not_called()
		finally:
			self._cleanup(inv)

	def test_refresh_repairs_doc_flags_when_ledger_covers_every_serial(self):
		"""The incident's exact shape: the ledger says the 4473 cleared, the
		doc-level flags were reset. The ledger wins — it is the submit gate's own
		evidence."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser_a, ser_b = "RCV-RF-A", "RCV-RF-B"
		for s in (ser_a, ser_b):
			_ensure_serial(s, self.company)
		inv = self._draft_invoice(ser_a, ser_b, form_4473="Pending", nics="N/A")
		try:
			for s in (ser_a, ser_b):
				self._seed_retail_disposition(inv, s, fastbound_id="FB-RF-DISP")
			with patch.object(recovery, "_probe_fastbound") as probe:
				res = recovery.refresh_4473_status(inv)
			self.assertEqual(res["state"], recovery.STATE_REPAIRED)
			self.assertEqual(res["missing"], [])
			probe.assert_not_called()
			flags = self._flags(inv)
			self.assertEqual(flags.form_4473_status, "Completed")
			self.assertEqual(flags.nics_status, "Proceed")
			# The disposition id is backfilled from the ledger when it was lost.
			self.assertEqual(flags.fastbound_disposition_id, "FB-RF-DISP")
			self.assertTrue(frappe.db.exists("Comment", {
				"reference_doctype": "POS Invoice", "reference_name": inv,
				"comment_type": "Comment"}))
			# The submit gate now passes.
			from ffl_core.compliance.firearm import require_4473_and_nics_for_firearm_sale
			gate_doc = frappe.get_doc("POS Invoice", inv)
			gate_doc.docstatus = 1
			require_4473_and_nics_for_firearm_sale(gate_doc)
		finally:
			self._cleanup(inv, ser_a, ser_b)

	def test_refresh_does_not_repair_on_partial_coverage(self):
		"""Two guns, one disposition — a half-covered order must stay blocked."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser_a, ser_b = "RCV-RF-PART-A", "RCV-RF-PART-B"
		for s in (ser_a, ser_b):
			_ensure_serial(s, self.company)
		inv = self._draft_invoice(ser_a, ser_b, form_4473="Pending", nics="N/A")
		try:
			self._seed_retail_disposition(inv, ser_a, fastbound_id="FB-RF-PART")
			with patch.object(recovery, "_probe_fastbound", return_value=([], None)):
				res = recovery.refresh_4473_status(inv)
			self.assertNotEqual(res["state"], recovery.STATE_REPAIRED)
			self.assertEqual(res["missing"], [ser_b])
			self.assertEqual(res["covered"], [ser_a])
			self.assertEqual(self._flags(inv).form_4473_status, "Pending")
		finally:
			self._cleanup(inv, ser_a, ser_b)

	def test_refresh_ignores_cancelled_dispositions(self):
		"""A voided bound-book record proves nothing."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-RF-CANC"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending", nics="N/A")
		try:
			name = self._seed_retail_disposition(inv, ser, fastbound_id="FB-RF-CANC")
			disp = frappe.get_doc("FFL Disposition", name)
			disp.flags.ignore_permissions = True
			disp.cancel()
			with patch.object(recovery, "_probe_fastbound", return_value=([], None)):
				res = recovery.refresh_4473_status(inv)
			self.assertNotEqual(res["state"], recovery.STATE_REPAIRED)
			self.assertEqual(self._flags(inv).form_4473_status, "Pending")
		finally:
			self._cleanup(inv, ser)

	def test_refresh_is_idempotent(self):
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-RF-IDEM"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending", nics="N/A")
		try:
			self._seed_retail_disposition(inv, ser, fastbound_id="FB-RF-IDEM")
			with patch.object(recovery, "_probe_fastbound", return_value=([], None)):
				first = recovery.refresh_4473_status(inv)
				second = recovery.refresh_4473_status(inv)
			self.assertEqual(first["state"], recovery.STATE_REPAIRED)
			self.assertEqual(second["state"], recovery.STATE_COMPLETED)
			comments = frappe.get_all("Comment", filters={
				"reference_doctype": "POS Invoice", "reference_name": inv,
				"comment_type": "Comment"})
			self.assertEqual(len(comments), 1, "the repair is logged once, not per click")
		finally:
			self._cleanup(inv, ser)

	# ------------------------------------- Task 4: FastBound probe (read-only)

	def _fake_fb(self, item):
		"""A FastBoundClient stand-in whose find_item_by_serial returns `item`."""
		return SimpleNamespace(find_item_by_serial=lambda serial: item)

	def test_probe_client_fails_fast_instead_of_retrying(self):
		"""The shared client retries 5x with exponential back-off — ~18s against an
		unreachable host, which froze the POS behind the Refresh button in local
		verification. The probe client must fail fast instead.

		This one owns the counter's side: it goes through the real _probe_client and
		pins PROBE_TIMEOUT_SECONDS. Its sibling
		test_fastbound_pos_4473.test_fail_fast_client_really_has_no_retry_budget
		covers the same FastBoundClient.from_settings_fast for the 4473 webhook
		lookup and carries the control assertion (the shared client really does
		retry). Change one, look at the other.
		"""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		self.assertLessEqual(recovery.PROBE_TIMEOUT_SECONDS, 5)
		client = recovery._probe_client()
		self.assertEqual(client.config.timeout, recovery.PROBE_TIMEOUT_SECONDS)
		for adapter in client.session.adapters.values():
			total = getattr(getattr(adapter, "max_retries", None), "total", 0)
			self.assertIn(total, (0, None, False), "the probe session must not retry")

	def test_refresh_never_writes_nics_from_a_fastbound_probe(self):
		"""FastBound says the gun left the bound book, but no local disposition
		proves the NICS outcome — the REST API does not carry it. Report, do not
		write: guessing Proceed here would fabricate a compliance fact."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-FB-DISPOSED"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending", nics="N/A")
		try:
			item = {"serial": ser, "disposed": True, "dispositionId": "FB-PROBE-1",
				"ttsn": "2026-00044", "dispositionType": "Sale"}
			with patch.object(recovery, "_probe_client", return_value=self._fake_fb(item)):
				res = recovery.refresh_4473_status(inv)
			self.assertEqual(res["state"], recovery.STATE_FASTBOUND_ONLY)
			self.assertEqual(res["fastbound"][0]["disposition_id"], "FB-PROBE-1")
			self.assertEqual(res["fastbound"][0]["ttsn"], "2026-00044")
			self.assertTrue(res["fastbound"][0]["disposed"])
			flags = self._flags(inv)
			self.assertEqual(flags.form_4473_status, "Pending")
			self.assertEqual(flags.nics_status, "N/A")
		finally:
			self._cleanup(inv, ser)

	def test_refresh_reports_not_ready_when_fastbound_has_no_disposal(self):
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-FB-OPEN"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending", nics="N/A")
		try:
			item = {"serial": ser, "disposed": False, "dispositionId": None}
			with patch.object(recovery, "_probe_client", return_value=self._fake_fb(item)):
				res = recovery.refresh_4473_status(inv)
			self.assertEqual(res["state"], recovery.STATE_NOT_READY)
			self.assertEqual(self._flags(inv).form_4473_status, "Pending")
		finally:
			self._cleanup(inv, ser)

	def test_refresh_degrades_when_fastbound_is_unreachable(self):
		"""The button must never throw at the counter."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		from ffl_integrations.fastbound.client import FastBoundError
		ser = "RCV-FB-DOWN"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending", nics="N/A")
		try:
			def _boom(serial):
				raise FastBoundError("connection refused")
			with patch.object(recovery, "_probe_client",
					return_value=SimpleNamespace(find_item_by_serial=_boom)):
				res = recovery.refresh_4473_status(inv)
			self.assertEqual(res["state"], recovery.STATE_FB_UNREACHABLE)
			self.assertEqual(self._flags(inv).form_4473_status, "Pending")
		finally:
			self._cleanup(inv, ser)

	def test_refresh_degrades_when_fastbound_is_not_configured(self):
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		from ffl_integrations.fastbound.client import FastBoundConfigError
		ser = "RCV-FB-UNCONF"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending", nics="N/A")
		try:
			with patch.object(recovery, "_probe_client",
					side_effect=FastBoundConfigError("no api key")):
				res = recovery.refresh_4473_status(inv)
			self.assertEqual(res["state"], recovery.STATE_FB_UNREACHABLE)
		finally:
			self._cleanup(inv, ser)

	# ------------------------------------------------ Task 5: the redo channel

	def test_manager_redo_requires_manager_role_and_reason(self):
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-REDO-PERM"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Completed", nics="Proceed")
		try:
			name = self._seed_retail_disposition(inv, ser, fastbound_id="FB-REDO-PERM")
			with patch.object(recovery.frappe, "get_roles", return_value=["Sales User"]):
				with self.assertRaises(frappe.PermissionError):
					recovery.manager_redo_4473(inv, "no manager role")
			with self.assertRaises(frappe.ValidationError):
				recovery.manager_redo_4473(inv, "   ")
			# Nothing moved on either failed attempt.
			self.assertEqual(frappe.db.get_value("FFL Disposition", name, "docstatus"), 1)
			self.assertEqual(self._flags(inv).form_4473_status, "Completed")
		finally:
			self._cleanup(inv, ser)

	def test_manager_redo_cancels_the_ledger_and_clears_the_field_group(self):
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-REDO-OK"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Completed", nics="Proceed",
			disposition_id="FB-REDO-OK", pdf_url="https://fb.test/redo.pdf")
		try:
			name = self._seed_retail_disposition(inv, ser, fastbound_id="FB-REDO-OK")
			frappe.db.set_value("POS Invoice", inv, "fastbound_url",
				"https://cloud.fastbound.com/TEST-ACC/Form4473s", update_modified=False)
			res = recovery.manager_redo_4473(inv, "buyer denied; 4473 voided in FastBound")
			self.assertTrue(res["ok"])
			self.assertEqual(res["cancelled"], [name])
			self.assertEqual(frappe.db.get_value("FFL Disposition", name, "docstatus"), 2)
			flags = self._flags(inv)
			self.assertEqual(flags.form_4473_status, "Pending")
			self.assertEqual(flags.nics_status, "N/A")
			self.assertEqual(flags.fastbound_disposition_id or "", "")
			self.assertEqual(flags.fastbound_4473_pdf_url or "", "")
			self.assertEqual(frappe.db.get_value("POS Invoice", inv, "fastbound_url") or "", "")
			self.assertTrue(frappe.db.exists("Comment", {
				"reference_doctype": "POS Invoice", "reference_name": inv,
				"comment_type": "Comment"}))
		finally:
			self._cleanup(inv, ser)

	def test_refresh_after_redo_does_not_resurrect_the_old_status(self):
		"""The two recovery paths must not fight: a redo cancels the ledger, so
		the next Refresh has no evidence to restore Completed from."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-REDO-REFRESH"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Completed", nics="Proceed")
		try:
			self._seed_retail_disposition(inv, ser, fastbound_id="FB-REDO-RF")
			recovery.manager_redo_4473(inv, "redo: wrong serial on the 4473")
			with patch.object(recovery, "_probe_fastbound", return_value=([], None)):
				res = recovery.refresh_4473_status(inv)
			self.assertNotEqual(res["state"], recovery.STATE_REPAIRED)
			self.assertEqual(self._flags(inv).form_4473_status, "Pending")
		finally:
			self._cleanup(inv, ser)

	def test_manager_redo_rejects_a_submitted_invoice(self):
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-REDO-SUBMITTED"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Completed", nics="Proceed")
		try:
			self._seed_retail_disposition(inv, ser, fastbound_id="FB-REDO-SUB")
			frappe.db.set_value("POS Invoice", inv, "docstatus", 1, update_modified=False)
			with self.assertRaises(frappe.ValidationError):
				recovery.manager_redo_4473(inv, "too late — already submitted")
		finally:
			frappe.db.set_value("POS Invoice", inv, "docstatus", 0, update_modified=False)
			self._cleanup(inv, ser)

	# --------------------------------------- DISP-2026-02000: abandoned orders

	def _fb(self, rows, *, error=None, delete_raises=None, after=None):
		"""Stand in for FastBound: `_probe_fastbound` answers `rows`, and the
		client records (or refuses) each delete.

		The probe is asked TWICE on the happy path — once to find the holds, then
		again after the deletes to confirm the guns really came free, because a
		FastBound DELETE can leave a dangling dispositionId behind. So the
		second answer is its own: by default `rows` with the disposition ids
		cleared (FastBound honoured the delete); pass `after=(rows, error)` to
		model a hold that survived it, or a FastBound that went away.
		"""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		from unittest.mock import MagicMock

		client = MagicMock()
		if delete_raises is not None:
			client.delete_disposition.side_effect = delete_raises
		answers = [
			(rows, error),
			after if after is not None
				else ([dict(r, disposition_id="") for r in rows], None),
		]

		def probe(_serials):
			return answers.pop(0) if len(answers) > 1 else answers[0]

		return (patch.object(recovery, "_probe_fastbound", side_effect=probe),
			patch.object(recovery, "_probe_client", return_value=client), client)

	@staticmethod
	def _fb_row(serial, *, disposition_id="", disposed=False):
		return {"serial": serial, "found": True, "disposed": disposed,
			"disposition_id": disposition_id, "item_number": "00843",
			"ttsn": "00843", "disposition_type": ""}

	def test_the_probe_reports_only_guns_held_by_an_UNCOMMITTED_disposition(self):
		"""A disposed gun has left the book legitimately; a gun with no
		disposition id is simply free. Neither is the abandoned-4473 shape."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		rows = [
			self._fb_row("HELD", disposition_id="pending-9"),
			self._fb_row("SOLD", disposition_id="done-1", disposed=True),
			self._fb_row("FREE"),
		]
		with patch.object(recovery, "_probe_fastbound", return_value=(rows, None)):
			out = recovery.pending_disposition_probe(["HELD", "SOLD", "FREE"])
		self.assertEqual(list(out), ["HELD"])
		self.assertEqual(out["HELD"]["disposition_id"], "pending-9")

	def test_the_probe_never_raises_when_fastbound_is_unreachable(self):
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		with patch.object(recovery, "_probe_fastbound",
				return_value=([], "connection refused")):
			self.assertEqual(recovery.pending_disposition_probe(["X"]), {})

	def test_abandon_requires_manager_role_and_reason(self):
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-ABAN-PERM"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending")
		try:
			with patch.object(recovery.frappe, "get_roles", return_value=["Sales User"]):
				with self.assertRaises(frappe.PermissionError):
					recovery.manager_abandon_4473(inv, "no manager role")
			with self.assertRaises(frappe.ValidationError):
				recovery.manager_abandon_4473(inv, "   ")
			self.assertTrue(frappe.db.exists("POS Invoice", inv))
		finally:
			self._cleanup(inv, ser)

	def test_abandon_drops_the_pending_disposition_then_deletes_the_draft(self):
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-ABAN-OK"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending")
		try:
			probe, cli, client = self._fb([self._fb_row(ser, disposition_id="pending-9")])
			with probe, cli:
				res = recovery.manager_abandon_4473(inv, "customer never came back")
			self.assertTrue(res["ok"])
			self.assertEqual(res["deleted"], ["pending-9"])
			client.delete_disposition.assert_called_once_with("pending-9")
			self.assertFalse(frappe.db.exists("POS Invoice", inv))
		finally:
			self._cleanup(inv, ser)

	def test_the_deleted_draft_is_restorable_from_the_pos_recycle_bin(self):
		"""Deleted, not cancelled — a draft cannot be cancelled — so the safety
		net is the Deleted Document row the POS recycle bin reads."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-ABAN-BIN"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending")
		try:
			probe, cli, _ = self._fb([self._fb_row(ser, disposition_id="p-1")])
			with probe, cli:
				recovery.manager_abandon_4473(inv, "abandoned")
			self.assertTrue(frappe.db.exists("Deleted Document", {
				"deleted_doctype": "POS Invoice", "deleted_name": inv}))
		finally:
			frappe.db.delete("Deleted Document",
				{"deleted_doctype": "POS Invoice", "deleted_name": inv})
			self._cleanup(inv, ser)

	def test_a_fastbound_refusal_leaves_the_order_exactly_as_it_was(self):
		"""FastBound goes first on purpose: the other order would release the gun
		while the bound book still held it — the state this whole change ends."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		from ffl_integrations.fastbound.client import FastBoundError
		ser = "RCV-ABAN-FAIL"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending")
		try:
			probe, cli, _ = self._fb(
				[self._fb_row(ser, disposition_id="pending-9")],
				delete_raises=FastBoundError("500 boom", status_code=500))
			with probe, cli:
				with self.assertRaises(frappe.ValidationError):
					recovery.manager_abandon_4473(inv, "abandoned")
			self.assertTrue(frappe.db.exists("POS Invoice", inv))
		finally:
			self._cleanup(inv, ser)

	def test_an_unreachable_fastbound_changes_nothing(self):
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-ABAN-DOWN"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending")
		try:
			probe, cli, client = self._fb([], error="connection refused")
			with probe, cli:
				with self.assertRaises(frappe.ValidationError):
					recovery.manager_abandon_4473(inv, "abandoned")
			client.delete_disposition.assert_not_called()
			self.assertTrue(frappe.db.exists("POS Invoice", inv))
		finally:
			self._cleanup(inv, ser)

	def test_a_gun_fastbound_has_genuinely_disposed_is_never_deleted(self):
		"""That is a committed bound-book entry, not an abandoned wizard."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-ABAN-DISPOSED"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending")
		try:
			probe, cli, client = self._fb(
				[self._fb_row(ser, disposition_id="done-1", disposed=True)])
			with probe, cli:
				res = recovery.manager_abandon_4473(inv, "abandoned")
			client.delete_disposition.assert_not_called()
			self.assertEqual(res["skipped"], [ser])
		finally:
			self._cleanup(inv, ser)

	# --------------------------------------- B1: the v16 Bundle storage shape

	def test_the_serials_of_a_BUNDLE_line_are_seen_at_all(self):
		"""`_firearm_serials` read only `row.serial_no`. On a v16 cart the serials
		live in a Serial and Batch Bundle, so it returned [] — and an empty list is
		not a harmless blank: `manager_abandon_4473` skips FastBound entirely,
		deletes the draft, and answers `ok: True, deleted: []`. The gun goes back
		on the shelf while the bound book still holds it, and the operator is told
		the order was cleared."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-BUNDLE-READ"
		_ensure_serial(ser, self.company)
		inv, bundles = self._draft_invoice_with_bundles(ser)
		try:
			doc = frappe.get_doc("POS Invoice", inv)
			self.assertFalse(doc.items[0].serial_no, "fixture is not in bundle shape")
			self.assertEqual(recovery._firearm_serials(doc), [ser])
		finally:
			self._cleanup(inv, ser)
			self._drop_bundles(bundles)

	def test_submit_gate_demands_coverage_for_a_BUNDLE_line(self):
		"""The submit gate shares the recovery read (regulated_firearm_serials), so
		a bundle-only firearm line is not gun-less to it either: with no Retail
		Sale disposition on the invoice, _require_disposition_coverage blocks and
		names the bundle's serial."""
		from ffl_core.compliance.firearm import _require_disposition_coverage
		ser = "RCV-BUNDLE-GATE"
		_ensure_serial(ser, self.company)
		inv, bundles = self._draft_invoice_with_bundles(ser)
		try:
			doc = frappe.get_doc("POS Invoice", inv)
			self.assertFalse(doc.items[0].serial_no, "fixture is not in bundle shape")
			with self.assertRaises(frappe.ValidationError) as ctx:
				_require_disposition_coverage(doc)
			self.assertIn(ser, str(ctx.exception))
		finally:
			self._cleanup(inv, ser)
			self._drop_bundles(bundles)

	def test_abandon_clears_the_bound_book_for_a_BUNDLE_line_order(self):
		"""The whole point of B1, end to end: the same clear-up on the storage
		model a real v16 register produces."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-BUNDLE-ABAN"
		_ensure_serial(ser, self.company)
		inv, bundles = self._draft_invoice_with_bundles(ser)
		try:
			probe, cli, client = self._fb([self._fb_row(ser, disposition_id="pending-b")])
			with probe, cli:
				res = recovery.manager_abandon_4473(inv, "bundle cart, customer gone")
			self.assertEqual(res["serials"], [ser])
			self.assertEqual(res["deleted"], ["pending-b"])
			client.delete_disposition.assert_called_once_with("pending-b")
			self.assertFalse(frappe.db.exists("POS Invoice", inv))
		finally:
			self._cleanup(inv, ser)
			self._drop_bundles(bundles)

	# --------------------------------------- M1: the delete is verified, not assumed

	def test_a_hold_that_survives_the_delete_keeps_the_order(self):
		"""A FastBound DELETE can return 200 and still leave the item carrying a
		dangling dispositionId (observed 2026-07-07 — the reason undispose_item
		exists). The next disposition for that gun is still refused, so giving up
		the draft here would destroy the only POS record of the order while
		reporting it cleared."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-ABAN-DANGLING"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending")
		try:
			held = [self._fb_row(ser, disposition_id="pending-9")]
			probe, cli, client = self._fb(held, after=(held, None))
			with probe, cli:
				with self.assertRaises(frappe.ValidationError) as ctx:
					recovery.manager_abandon_4473(inv, "abandoned")
			client.delete_disposition.assert_called_once_with("pending-9")
			self.assertIn("pending-9", str(ctx.exception))
			self.assertTrue(frappe.db.exists("POS Invoice", inv))
		finally:
			self._cleanup(inv, ser)

	def test_a_fastbound_blackout_after_the_delete_also_keeps_the_order(self):
		"""Unverified is not verified, so the order stays. Re-running is safe: a
		gun whose hold really went is skipped, and one left carrying a dangling
		disposition id is deleted again — FastBound answers 404 and
		`delete_disposition` treats that as done."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-ABAN-BLACKOUT"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending")
		try:
			probe, cli, _ = self._fb(
				[self._fb_row(ser, disposition_id="pending-9")],
				after=([], "connection refused"))
			with probe, cli:
				with self.assertRaises(frappe.ValidationError):
					recovery.manager_abandon_4473(inv, "abandoned")
			self.assertTrue(frappe.db.exists("POS Invoice", inv))
		finally:
			self._cleanup(inv, ser)

	def test_nothing_to_delete_means_nothing_to_verify(self):
		"""No delete happened, so no second round trip is spent confirming it."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-ABAN-FREE"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending")
		try:
			probe, cli, client = self._fb([self._fb_row(ser)])
			with probe as probe_mock, cli:
				res = recovery.manager_abandon_4473(inv, "nothing held")
			self.assertEqual(res["skipped"], [ser])
			self.assertEqual(probe_mock.call_count, 1)
			client.delete_disposition.assert_not_called()
			self.assertFalse(frappe.db.exists("POS Invoice", inv))
		finally:
			self._cleanup(inv, ser)

	# --------------------------------------- n1: multi-gun partial failure

	def test_a_second_gun_refused_keeps_the_order_and_reports_the_first(self):
		"""Two guns, the first delete accepted and the second refused. The order
		must survive — half a bound book cleared and no POS record of which half
		is the worst outcome available — and the message has to say how far it
		got, because the operator's retry will skip gun one."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		from ffl_integrations.fastbound.client import FastBoundError
		first, second = "RCV-ABAN-MULTI-A", "RCV-ABAN-MULTI-B"
		_ensure_serial(first, self.company)
		_ensure_serial(second, self.company)
		inv = self._draft_invoice(first, second, form_4473="Pending")
		try:
			probe, cli, client = self._fb([
				self._fb_row(first, disposition_id="pending-a"),
				self._fb_row(second, disposition_id="pending-b"),
			], delete_raises=[None, FastBoundError("500 boom", status_code=500)])
			with probe, cli:
				with self.assertRaises(frappe.ValidationError) as ctx:
					recovery.manager_abandon_4473(inv, "abandoned")
			msg = str(ctx.exception)
			self.assertIn("pending-b", msg)   # the one that refused
			self.assertIn(second, msg)
			self.assertIn("1 disposition", msg)  # how far it got, for the retry
			self.assertEqual(
				[c[0][0] for c in client.delete_disposition.call_args_list],
				["pending-a", "pending-b"])
			self.assertTrue(frappe.db.exists("POS Invoice", inv))
		finally:
			self._cleanup(inv, first, second)

	# ------------------- the partial-failure trail survives the rolled-back throw

	def _abandon_expecting_failure(self, inv, probe, cli):
		"""Run the button expecting a throw, capturing the audit trail's two
		halves — the Error Log write and the commit — in ORDER.

		Both have to land BEFORE the throw: ``frappe.throw`` rolls the request
		back, and an Error Log row written inside that transaction goes with it.
		The commit is the whole point, so a test that only checked the log text
		would pass on a trail nobody can read afterwards.
		"""
		from unittest.mock import MagicMock

		from ffl_integrations.fastbound import pos_4473_recovery as recovery

		trail = MagicMock()
		with probe, cli, \
				patch("ffl_integrations.notifications.safe_log_error", trail.log), \
				patch.object(recovery, "commit", trail.commit):
			with self.assertRaises(frappe.ValidationError) as ctx:
				recovery.manager_abandon_4473(inv, "abandoned")
		return trail, ctx

	@staticmethod
	def _trail_text(trail) -> str:
		return " | ".join(str(a) for c in trail.log.call_args_list for a in c[0])

	def test_a_refused_delete_leaves_a_committed_record_of_what_WAS_deleted(self):
		"""Two guns, the second refused: the operator's only way to know that
		pending-a is already gone is a record that outlives the rollback. Without
		it a re-run is blind — and `deleted` in the thrown message dies with the
		response the moment the page is closed."""
		from ffl_integrations.fastbound.client import FastBoundError
		first, second = "RCV-ABAN-TRAIL-A", "RCV-ABAN-TRAIL-B"
		_ensure_serial(first, self.company)
		_ensure_serial(second, self.company)
		inv = self._draft_invoice(first, second, form_4473="Pending")
		try:
			probe, cli, _ = self._fb([
				self._fb_row(first, disposition_id="pending-a"),
				self._fb_row(second, disposition_id="pending-b"),
			], delete_raises=[None, FastBoundError("500 boom", status_code=500)])
			trail, _ctx = self._abandon_expecting_failure(inv, probe, cli)
			self.assertEqual([c[0] for c in trail.mock_calls], ["log", "commit"],
				msg="log the progress, then COMMIT it, then throw")
			text = self._trail_text(trail)
			self.assertIn("pending-a", text)   # already gone from FastBound
			self.assertIn(inv, text)
			self.assertIn("NOT deleted", text)  # ...and the order still exists
			self.assertTrue(frappe.db.exists("POS Invoice", inv))
		finally:
			self._cleanup(inv, first, second)

	def test_a_blackout_after_the_delete_leaves_the_same_record(self):
		"""The delete went through and the verify re-probe could not answer. The
		guns whose holds are gone are exactly what the next run must not guess."""
		ser = "RCV-ABAN-TRAIL-DOWN"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending")
		try:
			probe, cli, _ = self._fb(
				[self._fb_row(ser, disposition_id="pending-9")],
				after=([], "connection refused"))
			trail, _ctx = self._abandon_expecting_failure(inv, probe, cli)
			self.assertEqual([c[0] for c in trail.mock_calls], ["log", "commit"])
			self.assertIn("pending-9", self._trail_text(trail))
		finally:
			self._cleanup(inv, ser)

	def test_a_surviving_hold_leaves_the_same_record(self):
		"""The dangling-dispositionId path: the delete was accepted, the hold
		stayed. pending-9 is still gone as a disposition, so the record matters
		here too."""
		ser = "RCV-ABAN-TRAIL-DANGLE"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending")
		try:
			held = [self._fb_row(ser, disposition_id="pending-9")]
			probe, cli, _ = self._fb(held, after=(held, None))
			trail, _ctx = self._abandon_expecting_failure(inv, probe, cli)
			self.assertEqual([c[0] for c in trail.mock_calls], ["log", "commit"])
			self.assertIn("pending-9", self._trail_text(trail))
		finally:
			self._cleanup(inv, ser)

	def test_a_failure_that_changed_NOTHING_writes_no_trail(self):
		"""FastBound unreachable on the FIRST probe: nothing was deleted, so
		there is nothing to remember — and a partial-clear entry in the Error Log
		for a run that touched nothing would be a false lead."""
		ser = "RCV-ABAN-TRAIL-NONE"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending")
		try:
			probe, cli, _ = self._fb([], error="connection refused")
			trail, _ctx = self._abandon_expecting_failure(inv, probe, cli)
			self.assertEqual(trail.mock_calls, [])
		finally:
			self._cleanup(inv, ser)

	# --------------------------------------- m5: the report's half of the contract

	def test_the_probe_hands_the_report_the_item_number(self):
		"""The report renders `disposition_id (item #n)`: the item number is what
		an operator types into FastBound's search box to reach the hold. It used to
		be dropped, while the report read a `status` key nothing ever sent."""
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		rows = [self._fb_row("HELD", disposition_id="pending-9")]
		with patch.object(recovery, "_probe_fastbound", return_value=(rows, None)):
			out = recovery.pending_disposition_probe(["HELD"])
		self.assertEqual(out["HELD"]["item_number"], "00843")

	def test_abandon_rejects_a_submitted_invoice(self):
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser = "RCV-ABAN-SUBMITTED"
		_ensure_serial(ser, self.company)
		inv = self._draft_invoice(ser, form_4473="Pending")
		try:
			frappe.db.set_value("POS Invoice", inv, "docstatus", 1, update_modified=False)
			with self.assertRaises(frappe.ValidationError):
				recovery.manager_abandon_4473(inv, "too late")
		finally:
			frappe.db.set_value("POS Invoice", inv, "docstatus", 0, update_modified=False)
			self._cleanup(inv, ser)
