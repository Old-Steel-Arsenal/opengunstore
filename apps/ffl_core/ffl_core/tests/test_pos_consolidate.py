"""Immediate firearm POS-Invoice consolidation tests.

Covers OUR logic — firearm-line detection, the on_submit enqueue dispatch + its
idempotency/return guards, and the one-invoice POS Invoice Merge Log we build
(with `company` set — the field is mandatory, so this is the regression that
makes immediate consolidation work on a real sale). The merge log's on_submit
(consolidated Sales Invoice + stock posting) is ERPNext's, exercised end-to-end
on dev with a real firearm sale."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime, nowdate

from ffl_core.api.pos_consolidate import (
	_find_stuck_pos_invoices,
	_has_firearm_line,
	consolidate_firearm_invoice_on_submit,
	consolidate_pos_invoice_now,
)
from ffl_core.tests.test_cash_drawer_pos import ShiftFixtures


def _item(code: str, group: str, is_firearm: int) -> None:
	if frappe.db.exists("Item", code):
		return
	doc = {
		"doctype": "Item", "item_code": code, "item_name": code,
		"item_group": group, "stock_uom": "Nos", "is_stock_item": 1,
		"has_serial_no": 1 if is_firearm else 0, "is_firearm": is_firearm,
	}
	if is_firearm:
		doc["serial_no_series"] = f"{code}-.####"
	frappe.get_doc(doc).insert(ignore_permissions=True)


class PosConsolidate(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import item_groups, test_bootstrap
		test_bootstrap.run()
		item_groups.ensure()
		_item("PC-FIREARM", "Pistol", 1)
		_item("PC-AMMO", "Ammunition", 0)

	# ---------------------------------------------------------- firearm detection

	def test_has_firearm_line_true_when_a_line_is_a_firearm(self):
		doc = frappe._dict(items=[frappe._dict(item_code="PC-AMMO"),
			frappe._dict(item_code="PC-FIREARM")])
		self.assertTrue(_has_firearm_line(doc))

	def test_has_firearm_line_false_for_non_firearm_only(self):
		doc = frappe._dict(items=[frappe._dict(item_code="PC-AMMO")])
		self.assertFalse(_has_firearm_line(doc))

	# ------------------------------------------------------ on_submit dispatch

	@patch("ffl_core.api.pos_consolidate.frappe.enqueue")
	def test_on_submit_enqueues_immediate_consolidation_for_firearm(self, mock_enqueue):
		doc = frappe._dict(name="PINV-F", doctype="POS Invoice", is_consolidated=0,
			consolidated_invoice=None, items=[frappe._dict(item_code="PC-FIREARM")])
		consolidate_firearm_invoice_on_submit(doc)
		mock_enqueue.assert_called_once()
		self.assertEqual(mock_enqueue.call_args.args[0],
			"ffl_core.api.pos_consolidate.consolidate_pos_invoice_now")
		self.assertEqual(mock_enqueue.call_args.kwargs["pos_invoice"], "PINV-F")
		self.assertTrue(mock_enqueue.call_args.kwargs["enqueue_after_commit"])

	@patch("ffl_core.api.pos_consolidate.frappe.enqueue")
	def test_on_submit_skips_non_firearm_invoice(self, mock_enqueue):
		doc = frappe._dict(name="PINV-A", doctype="POS Invoice", is_consolidated=0,
			consolidated_invoice=None, items=[frappe._dict(item_code="PC-AMMO")])
		consolidate_firearm_invoice_on_submit(doc)
		mock_enqueue.assert_not_called()

	@patch("ffl_core.api.pos_consolidate.frappe.enqueue")
	def test_on_submit_enqueues_immediate_consolidation_for_negative_trade_in_ticket(self, mock_enqueue):
		doc = frappe._dict(name="PINV-N", doctype="POS Invoice", is_consolidated=0, grand_total=-4885,
			consolidated_invoice=None, items=[frappe._dict(item_code="PC-AMMO")])
		consolidate_firearm_invoice_on_submit(doc)
		mock_enqueue.assert_called_once()
		self.assertEqual(mock_enqueue.call_args.kwargs["pos_invoice"], "PINV-N")

	@patch("ffl_core.api.pos_consolidate.frappe.enqueue")
	def test_on_submit_skips_already_consolidated_invoice(self, mock_enqueue):
		doc = frappe._dict(name="PINV-C", doctype="POS Invoice", is_consolidated=1,
			consolidated_invoice="ACC-SINV-X", items=[frappe._dict(item_code="PC-FIREARM")])
		consolidate_firearm_invoice_on_submit(doc)
		mock_enqueue.assert_not_called()

	# --------------------------------------------- consolidate_pos_invoice_now

	def _run_with_fake_invoice(self, inv: dict, *, final_si: str = "ACC-SINV-NEW",
			originals: dict | None = None):
		"""Run consolidate_pos_invoice_now against a faked POS Invoice (no real
		invoice row needed), capturing the POS Invoice Merge Log it builds without
		touching the DB or creating a Sales Invoice.

		get_value is patched with a PASSTHROUGH: only OUR two POS-Invoice reads are
		faked (the initial fetch + the post-merge consolidated_invoice read); every
		other lookup — crucially the DocType controller resolution inside
		new_doc("POS Invoice Merge Log") — hits the real DB. `originals` fakes the
		consolidated_invoice of a return's ORIGINAL ({name: si_or_None}). Returns
		(out, captured)."""
		from erpnext.accounts.doctype.pos_invoice_merge_log import (
			pos_invoice_merge_log as mlmod,
		)
		captured = {}

		def fake_save(ml, *a, **k):
			captured["company"] = ml.company
			captured["customer"] = ml.customer
			captured["pos_invoices"] = [d.pos_invoice for d in ml.pos_invoices]
			captured["rows"] = [(d.pos_invoice, d.is_return, d.return_against) for d in ml.pos_invoices]

		def fake_submit(ml, *a, **k):
			captured["submitted"] = True

		real_gv = frappe.db.get_value

		def smart_gv(*args, **kwargs):
			# Forward EVERYTHING faithfully except our two POS-Invoice reads, which
			# we always make positionally: get_value("POS Invoice", name, fieldname).
			doctype = args[0] if args else kwargs.get("doctype")
			name = args[1] if len(args) > 1 else kwargs.get("filters")
			fieldname = args[2] if len(args) > 2 else kwargs.get("fieldname")
			if doctype == "POS Invoice" and name == inv["name"]:
				return final_si if fieldname == "consolidated_invoice" else frappe._dict(inv)
			if doctype == "POS Invoice" and originals is not None and name in originals:
				return originals[name]  # the return's original: its consolidated SI, or None
			return real_gv(*args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=smart_gv), \
			patch.object(frappe.db, "commit"), \
			patch.object(mlmod.POSInvoiceMergeLog, "save", fake_save), \
			patch.object(mlmod.POSInvoiceMergeLog, "submit", fake_submit):
			out = consolidate_pos_invoice_now(inv["name"])
		return out, captured

	def test_consolidate_now_builds_merge_log_with_company(self):
		# Regression for the SEV-1: the merge log's `company` is mandatory, so a
		# None company (the old no-closing-entry consolidate_pos_invoices path)
		# threw on every real sale. We must set company from the POS Invoice.
		out, captured = self._run_with_fake_invoice(dict(
			name="PINV-1", company="FFL Test Co", customer="C1",
			posting_date="2026-06-09", grand_total=500, is_return=0,
			return_against=None, consolidated_invoice=None, docstatus=1))
		self.assertEqual(captured["company"], "FFL Test Co")   # not None → no MandatoryError
		self.assertEqual(captured["customer"], "C1")
		self.assertEqual(captured["pos_invoices"], ["PINV-1"])  # exactly this one invoice
		self.assertTrue(captured["submitted"])
		self.assertTrue(out["consolidated"])
		self.assertEqual(out["sales_invoice"], "ACC-SINV-NEW")

	def test_consolidate_now_is_idempotent_when_already_consolidated(self):
		out, captured = self._run_with_fake_invoice(dict(
			name="PINV-1", company="FFL Test Co", customer="C1", docstatus=1,
			is_return=0, return_against=None, posting_date="2026-06-09",
			grand_total=0, consolidated_invoice="ACC-SINV-EXISTING"))
		self.assertEqual(captured, {})            # no merge log built/submitted
		self.assertFalse(out["consolidated"])

	def test_consolidate_now_holds_return_until_original_consolidated(self):
		# A return becomes a credit note against its original's consolidated Sales
		# Invoice (ERPNext's merge log insists the original is Consolidated first),
		# so with the original still unconsolidated there is nothing to build.
		out, captured = self._run_with_fake_invoice(dict(
			name="PINV-R", company="FFL Test Co", customer="C1", docstatus=1,
			is_return=1, return_against="PINV-1", posting_date="2026-06-09",
			grand_total=-50, consolidated_invoice=None), originals={"PINV-1": None})
		self.assertEqual(captured, {})
		self.assertFalse(out["consolidated"])
		self.assertEqual(out["waiting_on"], "PINV-1")

	def test_consolidate_now_merges_return_once_original_consolidated(self):
		out, captured = self._run_with_fake_invoice(dict(
			name="PINV-R", company="FFL Test Co", customer="C1", docstatus=1,
			is_return=1, return_against="PINV-1", posting_date="2026-06-09",
			grand_total=-50, consolidated_invoice=None), originals={"PINV-1": "ACC-SINV-ORIG"})
		self.assertEqual(captured["rows"], [("PINV-R", 1, "PINV-1")])  # credit note vs the original
		self.assertTrue(captured["submitted"])
		self.assertTrue(out["consolidated"])


class ReconsolidateStuck(unittest.TestCase):
	"""The nightly-safety-net sweep: retry submitted-but-unconsolidated POS
	Invoices and raise a visible alert when one keeps failing, so a sold firearm
	can't sit silently on the books (the failure mode that stranded a real sale
	when consolidation threw on a negative trade-in line)."""

	def test_find_stuck_scopes_to_firearm_unconsolidated_submitted_non_return(self):
		from ffl_core.api import pos_consolidate as m
		captured = {}
		real_sql = m.frappe.db.sql

		def fake_sql(query, params=None, **kw):
			if "`tabPOS Invoice`" not in query:
				# Framework lookups stay real: run first in a fresh process, now_datetime()
				# loads System Settings through db.sql on a cold client cache.
				return real_sql(query, params, **kw)
			captured.setdefault("queries", []).append(query)
			captured.setdefault("params", {}).update(params or {})
			return [["INV-1"], ["INV-2"]]

		with patch.object(m.frappe.db, "sql", side_effect=fake_sql):
			out = m._find_stuck_pos_invoices(10)
		self.assertEqual(out, ["INV-1", "INV-2"])   # both queries' rows, deduped, firearm first
		self.assertEqual(len(captured["queries"]), 2)  # firearm branch + missed-close branch
		q = captured["queries"][0].lower()             # the firearm branch:
		self.assertIn("tabpos invoice item", q)       # joins line items...
		self.assertIn("is_firearm", q)                # ...to scope to firearm sales only
		self.assertIn("docstatus = 1", q)
		self.assertIn("is_return = 0", q)
		self.assertIn("consolidated_invoice", q)
		self.assertIn("creation <", q)                # settle on creation, not modified
		self.assertNotIn("modified <", q)
		self.assertIn("cutoff", captured["params"])

	def test_sweep_retries_each_stuck_and_reports(self):
		from ffl_core.api import pos_consolidate as m
		with patch.object(m, "_find_stuck_pos_invoices", return_value=["A", "B"]), \
			patch.object(m, "consolidate_pos_invoice_now",
				side_effect=lambda name: {"consolidated": True, "pos_invoice": name}) as cons:
			out = m.reconsolidate_stuck_pos_invoices()
		self.assertEqual(cons.call_count, 2)
		self.assertEqual(set(out["consolidated"]), {"A", "B"})
		self.assertEqual(out["failed"], [])
		self.assertEqual(out["checked"], 2)

	def test_sweep_rolls_back_and_alerts_on_failure(self):
		from ffl_core.api import pos_consolidate as m

		def cons(name):
			if name == "BAD":
				raise frappe.ValidationError("boom")
			return {"consolidated": True, "pos_invoice": name}

		with patch.object(m, "_find_stuck_pos_invoices", return_value=["BAD", "OK"]), \
			patch.object(m, "consolidate_pos_invoice_now", side_effect=cons), \
			patch.object(m, "_alert_stuck") as alert, \
			patch.object(m.frappe.db, "rollback") as rollback, \
			patch.object(m.frappe.db, "commit"):
			out = m.reconsolidate_stuck_pos_invoices()
		self.assertEqual(out["failed"], ["BAD"])
		self.assertEqual(out["consolidated"], ["OK"])  # one failure doesn't abort the rest
		alert.assert_called_once_with("BAD")
		rollback.assert_called()  # the failed attempt's orphan partial write is discarded

	def test_sweep_caps_new_alerts_per_run_and_summarises_the_rest(self):
		from ffl_core.api import pos_consolidate as m
		names = [f"BAD-{i}" for i in range(8)]
		with patch.object(m, "_find_stuck_pos_invoices", return_value=names), \
			patch.object(m, "consolidate_pos_invoice_now",
				side_effect=frappe.ValidationError("closed period")), \
			patch.object(m, "_alert_stuck", return_value=True) as alert, \
			patch.object(m.frappe, "log_error") as log_error, \
			patch.object(m.frappe.db, "rollback"), \
			patch.object(m.frappe.db, "commit"):
			out = m.reconsolidate_stuck_pos_invoices()
		self.assertEqual(out["failed"], names)                          # every failure recorded
		self.assertEqual(alert.call_count, m._SWEEP_MAX_ALERTS_PER_RUN)   # fan-out capped
		log_error.assert_called_once()                                    # one summary, not eight

	def test_sweep_cap_counts_only_new_alerts(self):
		# An invoice that already has an open ToDo (alert returns False) must not
		# use up the cap — otherwise a long-stuck backlog would starve fresh ones.
		from ffl_core.api import pos_consolidate as m
		names = [f"BAD-{i}" for i in range(7)]
		with patch.object(m, "_find_stuck_pos_invoices", return_value=names), \
			patch.object(m, "consolidate_pos_invoice_now",
				side_effect=frappe.ValidationError("boom")), \
			patch.object(m, "_alert_stuck", return_value=False) as alert, \
			patch.object(m.frappe, "log_error"), \
			patch.object(m.frappe.db, "rollback"), \
			patch.object(m.frappe.db, "commit"):
			m.reconsolidate_stuck_pos_invoices()
		self.assertEqual(alert.call_count, 7)  # offered to all; none counted against the cap

	def test_sweep_reports_held_return_and_alerts_when_original_out_of_reach(self):
		from ffl_core.api import pos_consolidate as m
		with patch.object(m, "_find_stuck_pos_invoices", return_value=["R1"]), \
			patch.object(m, "consolidate_pos_invoice_now",
				return_value={"consolidated": False, "pos_invoice": "R1", "waiting_on": "O-OLD"}), \
			patch.object(m, "_original_out_of_reach", return_value=True), \
			patch.object(m, "_alert_stuck", return_value=True) as alert, \
			patch.object(m.frappe.db, "commit"):
			out = m.reconsolidate_stuck_pos_invoices()
		self.assertEqual(out["held"], ["R1"])   # the third state is reported, not swallowed
		self.assertEqual(out["failed"], [])
		alert.assert_called_once()
		self.assertEqual(alert.call_args.args[0], "R1")
		self.assertIn("O-OLD", alert.call_args.kwargs["reason"])

	def test_sweep_held_return_within_reach_is_silent(self):
		# The original is simply later in this run / tonight's close: no alert.
		from ffl_core.api import pos_consolidate as m
		with patch.object(m, "_find_stuck_pos_invoices", return_value=["R1"]), \
			patch.object(m, "consolidate_pos_invoice_now",
				return_value={"consolidated": False, "pos_invoice": "R1", "waiting_on": "O1"}), \
			patch.object(m, "_original_out_of_reach", return_value=False), \
			patch.object(m, "_alert_stuck") as alert:
			out = m.reconsolidate_stuck_pos_invoices()
		self.assertEqual(out["held"], ["R1"])
		alert.assert_not_called()

	def test_manual_batch_is_clamped_to_a_sane_range(self):
		from ffl_core.api import pos_consolidate as m
		with patch.object(m, "_find_stuck_pos_invoices", return_value=[]) as find, \
			patch.object(m, "consolidate_pos_invoice_now"):
			m.reconsolidate_stuck_pos_invoices(batch=-5)       # would be `LIMIT -5` → SQL error
			m.reconsolidate_stuck_pos_invoices(batch=10_000)
			m.reconsolidate_stuck_pos_invoices()
		self.assertEqual([c.kwargs["batch"] for c in find.call_args_list],
			[1, m._SWEEP_BATCH_MAX, None])

	def test_held_return_whose_alert_throws_is_failed_not_both(self):
		from ffl_core.api import pos_consolidate as m
		with patch.object(m, "_find_stuck_pos_invoices", return_value=["R1"]), \
			patch.object(m, "consolidate_pos_invoice_now",
				return_value={"consolidated": False, "pos_invoice": "R1", "waiting_on": None}), \
			patch.object(m, "_original_out_of_reach", return_value=True), \
			patch.object(m, "_alert_stuck", side_effect=[Exception("mail down"), True]), \
			patch.object(m.frappe.db, "rollback"), \
			patch.object(m.frappe.db, "commit"):
			out = m.reconsolidate_stuck_pos_invoices()
		self.assertEqual(out["failed"], ["R1"])
		self.assertEqual(out["held"], [])   # not reported twice

	def test_sweep_noop_when_none_stuck(self):
		from ffl_core.api import pos_consolidate as m
		with patch.object(m, "_find_stuck_pos_invoices", return_value=[]), \
			patch.object(m, "consolidate_pos_invoice_now") as cons, \
			patch.object(m, "_close_healed_alerts") as close_healed:
			out = m.reconsolidate_stuck_pos_invoices()
		cons.assert_not_called()
		self.assertEqual(out, {"checked": 0, "consolidated": [], "failed": [], "held": []})
		close_healed.assert_called_once()  # the nightly close may have healed an alerted invoice

	def test_alert_skips_and_does_not_log_when_open_todo_exists(self):
		from ffl_core.api import pos_consolidate as m
		with patch.object(m.frappe, "log_error") as log_error, \
			patch.object(m.frappe.db, "exists", return_value=True), \
			patch("frappe.desk.form.assign_to.add") as assign_add:
			out = m._alert_stuck("INV-1")
		log_error.assert_not_called()   # logged once on transition, not every run
		assign_add.assert_not_called()  # deduped: one open ToDo per invoice
		self.assertFalse(out)           # "nothing new" — must not consume the sweep's alert cap

	def test_alert_logs_once_and_assigns_to_managers_when_new(self):
		from ffl_core.api import pos_consolidate as m
		with patch.object(m.frappe, "log_error") as log_error, \
			patch.object(m.frappe.db, "exists", return_value=False), \
			patch("ffl_core.alerts.alert_assignees", return_value=["mgr@x"]), \
			patch("frappe.desk.form.assign_to.add") as assign_add:
			out = m._alert_stuck("INV-1")
		self.assertTrue(out)            # a new alert — counts against the cap
		log_error.assert_called_once()
		assign_add.assert_called_once()
		payload = assign_add.call_args.args[0]
		self.assertEqual(payload["assign_to"], ["mgr@x"])  # a real assignment → notifies
		self.assertEqual(payload["doctype"], "POS Invoice")
		self.assertEqual(payload["name"], "INV-1")

	def test_alert_skips_assignment_when_no_managers(self):
		from ffl_core.api import pos_consolidate as m
		with patch.object(m.frappe, "log_error") as log_error, \
			patch.object(m.frappe.db, "exists", return_value=False), \
			patch("ffl_core.alerts.alert_assignees", return_value=[]), \
			patch("frappe.desk.form.assign_to.add") as assign_add:
			out = m._alert_stuck("INV-1")
		self.assertTrue(out)            # logged = alerted, even with nobody to assign
		log_error.assert_called_once()  # still logged
		assign_add.assert_not_called()  # nobody to assign


class ConsolidateRowRace(unittest.TestCase):
	"""A merge that loses a MariaDB row race (1020 "Record has changed since last
	read" / 1213, raised as QueryDeadlockError) or a lock wait (1205, raised as
	QueryTimeoutError) is rolled back and run again. execute_job only retries
	db.InternalError, which neither is, so before this the sold gun stayed in
	stock — and buyable on the website — until the 15-min sweep
	(a past invoice: the Woo serial push stamped the same Serial No row two
	seconds before the merge flipped it to Delivered)."""

	def _run(self, *outcomes, pending_writes: int = 0):
		from ffl_core.api import pos_consolidate as m
		results = iter(outcomes)

		def once(name):
			r = next(results)
			if isinstance(r, Exception):
				raise r
			return r

		mocks = {}
		with patch.object(m, "_consolidate_once", side_effect=once) as mocks["once"], \
			patch.object(m.frappe.db, "transaction_writes", pending_writes), \
			patch.object(m.frappe.db, "rollback") as mocks["rollback"], \
			patch.object(m.time, "sleep") as mocks["sleep"]:
			try:
				mocks["out"] = m.consolidate_pos_invoice_now("PINV-1")
			except Exception as e:
				mocks["raised"] = e
		return mocks

	def test_lost_row_race_is_rolled_back_and_retried(self):
		ok = {"consolidated": True, "pos_invoice": "PINV-1", "sales_invoice": "ACC-SINV-1"}
		r = self._run(frappe.QueryDeadlockError("1020"), ok)
		self.assertEqual(r["out"], ok)
		self.assertEqual(r["once"].call_count, 2)
		r["rollback"].assert_called_once()   # the stale snapshot ends before the retry
		r["sleep"].assert_called_once_with(1)

	def test_lock_wait_timeout_is_retried_too(self):
		ok = {"consolidated": True, "pos_invoice": "PINV-1", "sales_invoice": "ACC-SINV-1"}
		r = self._run(frappe.QueryTimeoutError("1205"), ok)
		self.assertEqual(r["out"], ok)
		self.assertEqual(r["once"].call_count, 2)

	def test_gives_up_after_the_last_attempt(self):
		from ffl_core.api import pos_consolidate as m
		r = self._run(*[frappe.QueryDeadlockError("1020")] * m._ROW_RACE_ATTEMPTS)
		self.assertIsInstance(r["raised"], frappe.QueryDeadlockError)  # the sweep/Error Log takes it
		self.assertEqual(r["once"].call_count, m._ROW_RACE_ATTEMPTS)
		self.assertEqual(r["rollback"].call_count, m._ROW_RACE_ATTEMPTS - 1)  # the caller rolls back the last
		self.assertEqual([c.args[0] for c in r["sleep"].call_args_list], [1, 2])

	def test_no_retry_while_the_caller_has_writes_pending(self):
		# A retry's rollback would silently discard them: hand the error back instead.
		r = self._run(frappe.QueryDeadlockError("1020"), pending_writes=1)
		self.assertIsInstance(r["raised"], frappe.QueryDeadlockError)
		self.assertEqual(r["once"].call_count, 1)
		r["rollback"].assert_not_called()

	def test_other_errors_are_not_retried(self):
		r = self._run(frappe.ValidationError("closed period"))
		self.assertIsInstance(r["raised"], frappe.ValidationError)
		self.assertEqual(r["once"].call_count, 1)
		r["rollback"].assert_not_called()    # the caller's failure path owns that


class ConsolidateRetryReadsAfresh(ShiftFixtures):
	"""A retry after a lost row race merges the invoice as it is in the database. ERPNext's merge had already moved
	the cached invoice's tax rows onto the rolled-back Sales Invoice (renamed), so the retry failed with
	"KeyError: <tax row>" and the sale waited for the next sweep."""

	def _taxed_sale(self) -> str:
		frappe.set_user(self.cashier)
		try:
			inv = frappe.new_doc("POS Invoice")
			inv.pos_profile = self.profile
			inv.customer = "_Test CD Customer"
			inv.append("items", {"item_code": "CD-SERVICE", "qty": 1, "rate": 100, "price_list_rate": 100})
			inv.set_missing_values()
			inv.items[0].rate = inv.items[0].price_list_rate = 100
			inv.append("taxes", {"charge_type": "On Net Total", "account_head": self.stp, "description": "Sales tax",
				"rate": 8.25, "cost_center": frappe.db.get_value("Company", self.company, "cost_center")})
			inv.set("payments", [])
			inv.append("payments", {"mode_of_payment": "Cash", "amount": 108.25})
			inv.flags.ignore_permissions = True
			inv.save()
			inv.submit()
			return inv.name
		finally:
			frappe.set_user("Administrator")

	def test_a_retry_after_a_lost_row_race_merges_the_invoice(self):
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import SalesInvoice

		from ffl_core.api import pos_consolidate as m

		self._open_shift()
		sale = self._taxed_sale()
		self.assertTrue(frappe.get_all("Item Wise Tax Detail", filters={"parent": sale}))
		frappe.get_cached_doc("POS Invoice", sale)  # the register screen or a report read it already
		frappe.db.savepoint("before_merge")
		real_rollback, real_submit = frappe.db.rollback, SalesInvoice.submit
		tries = []

		def submit(si, *a, **k):
			tries.append(si.name)
			if len(tries) == 1:  # the Sales Invoice is in, its rows renamed: lose the race now
				raise frappe.QueryDeadlockError("1020")
			return real_submit(si, *a, **k)

		with patch.object(SalesInvoice, "submit", submit), \
			patch.object(m.frappe.db, "transaction_writes", 0), \
			patch.object(m.frappe.db, "rollback", side_effect=lambda *a, **k: real_rollback(save_point="before_merge")), \
			patch.object(m.time, "sleep"):
			out = consolidate_pos_invoice_now(sale)
		self.assertEqual(len(tries), 2)
		self.assertTrue(out["consolidated"])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual((si.docstatus, si.total_taxes_and_charges), (1, 8.25))


class FindStuckSweepQuery(FrappeTestCase):
	"""_find_stuck_pos_invoices against the REAL tables. Rows go in with
	db_insert — no validate/submit chain, because a genuinely submitted POS
	Invoice needs a live POS session, customer, payments and stock, none of which
	the sweep's predicate reads — so the SQL itself is what's under test. Rows roll
	back with the CLASS transaction (FrappeTestCase has no per-test rollback), so
	every test names its rows AND its register after itself — otherwise one test's
	Open shift would hold the next test's invoice — and re-inserting a name wipes
	any leftover first. The Customer is real (not a dangling Link): under
	frappe.in_test ERPNext's consolidate_pos_invoices falls back to ALL
	unconsolidated invoices when handed none, so any future closing-entry test in
	this class would otherwise pull these rows into a merge log and choke."""

	CUSTOMER = "_Test Sweep Customer"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import item_groups, test_bootstrap
		test_bootstrap.run()
		item_groups.ensure()
		_item("PC-FIREARM", "Pistol", 1)
		_item("PC-AMMO", "Ammunition", 0)
		if not frappe.db.exists("Customer", cls.CUSTOMER):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": cls.CUSTOMER,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)

	# ------------------------------------------------------------ row builders

	def _register(self, suffix: str = "") -> str:
		return f"REG-{self._testMethodName}{suffix}"[:140]

	def _invoice(self, tag: str, *, item: str, days_ago: int, created_minutes_ago: int | None = None,
			consolidated: str | None = None, register: str | None = None, is_return: int = 0,
			return_against: str | None = None, posting_time_null: bool = False,
			pos_profile_null: bool = False, owner: str = "Administrator", grand_total: float = 0) -> str:
		name = f"{self._testMethodName}-{tag}"[:140]
		when = add_to_date(now_datetime(), days=-days_ago)
		created = when if created_minutes_ago is None else add_to_date(now_datetime(), minutes=-created_minutes_ago)
		frappe.db.delete("POS Invoice Item", {"parent": name})
		frappe.db.delete("POS Invoice", {"name": name})
		frappe.get_doc({
			"doctype": "POS Invoice", "name": name, "docstatus": 1, "is_pos": 1,
			"is_return": is_return, "return_against": return_against,
			"pos_profile": None if pos_profile_null else (register or self._register()),
			"customer": self.CUSTOMER,
			"posting_date": when.date(), "posting_time": None if posting_time_null else when.time(),
			"consolidated_invoice": consolidated, "status": "Consolidated" if consolidated else "Paid",
			"grand_total": grand_total,
			"creation": created, "modified": created, "owner": owner, "modified_by": owner,
		}).db_insert()
		frappe.get_doc({
			"doctype": "POS Invoice Item", "name": f"{name}-row", "parent": name, "parenttype": "POS Invoice",
			"parentfield": "items", "idx": 1, "docstatus": 1, "item_code": item, "qty": 1,
			"creation": created, "modified": created, "owner": "Administrator", "modified_by": "Administrator",
		}).db_insert()
		return name

	def _open_shift(self, tag: str, *, started_days_ago: int, hours_offset: float = -1, register: str | None = None,
			user: str = "Administrator") -> str:
		name = f"{self._testMethodName}-{tag}"[:140]
		start = add_to_date(add_to_date(now_datetime(), days=-started_days_ago), hours=hours_offset)
		frappe.db.delete("POS Opening Entry", {"name": name})
		frappe.get_doc({
			"doctype": "POS Opening Entry", "name": name, "docstatus": 1, "status": "Open",
			"pos_profile": register or self._register(), "user": user,
			"period_start_date": start, "posting_date": start.date(),
			"creation": start, "modified": start, "owner": "Administrator", "modified_by": "Administrator",
		}).db_insert()
		return name

	def _open_todo(self, invoice: str) -> None:
		"""An open 'stuck' ToDo on `invoice`, as _alert_stuck would leave behind."""
		name = f"{invoice}-todo"[:140]
		now = now_datetime()
		frappe.db.delete("ToDo", {"name": name})
		frappe.get_doc({
			"doctype": "ToDo", "name": name, "status": "Open", "priority": "High",
			"reference_type": "POS Invoice", "reference_name": invoice, "description": "stuck",
			"creation": now, "modified": now, "owner": "Administrator", "modified_by": "Administrator",
		}).db_insert()

	# ------------------------------------------------------------------ cases

	def test_past_day_non_firearm_left_by_close_is_swept(self):
		inv = self._invoice("ammo-yday", item="PC-AMMO", days_ago=1)
		self.assertIn(inv, _find_stuck_pos_invoices(10))

	def test_same_day_non_firearm_waits_for_tonights_close(self):
		inv = self._invoice("ammo-today", item="PC-AMMO", days_ago=0, created_minutes_ago=60)
		self.assertNotIn(inv, _find_stuck_pos_invoices(10))

	def test_past_day_non_firearm_held_while_its_shift_is_still_open(self):
		# The 23:00 close missed a night: the shift that covers this sale is still
		# Open, so its close can still run — leave the invoice to it.
		self._open_shift("shift", started_days_ago=1)          # started 1 h before the sale
		inv = self._invoice("ammo-yday", item="PC-AMMO", days_ago=1)
		self.assertNotIn(inv, _find_stuck_pos_invoices(10))

	def test_open_shift_on_another_register_does_not_hold_it(self):
		self._open_shift("other-shift", started_days_ago=1, register=self._register("-other"))
		inv = self._invoice("ammo-yday", item="PC-AMMO", days_ago=1)
		self.assertIn(inv, _find_stuck_pos_invoices(10))

	def test_open_shift_started_after_the_sale_does_not_hold_it(self):
		# A shift opened AFTER the sale can't be the one that owes it a close.
		self._open_shift("later-shift", started_days_ago=1, hours_offset=+1)
		inv = self._invoice("ammo-yday", item="PC-AMMO", days_ago=1)
		self.assertIn(inv, _find_stuck_pos_invoices(10))

	# --- owner-scoped close (8/25, 8/26 incidents): a shift opened by ANOTHER
	# clerk can never take the invoice, so it holds nothing.

	def test_same_day_sale_under_another_clerks_open_shift_is_swept(self):
		self._open_shift("shift", started_days_ago=0, hours_offset=-3, user="opener@example.com")
		inv = self._invoice("ammo-today", item="PC-AMMO", days_ago=0, created_minutes_ago=60,
			owner="clerk@example.com")
		self.assertIn(inv, _find_stuck_pos_invoices(10))

	def test_same_day_sale_under_another_clerks_shift_still_waits_the_settle_window(self):
		self._open_shift("shift", started_days_ago=0, hours_offset=-3, user="opener@example.com")
		inv = self._invoice("ammo-now", item="PC-AMMO", days_ago=0, created_minutes_ago=1,
			owner="clerk@example.com")
		self.assertNotIn(inv, _find_stuck_pos_invoices(10))

	def test_same_day_sale_under_the_clerks_own_open_shift_waits_for_tonights_close(self):
		self._open_shift("shift", started_days_ago=0, hours_offset=-3, user="clerk@example.com")
		inv = self._invoice("ammo-today", item="PC-AMMO", days_ago=0, created_minutes_ago=60,
			owner="clerk@example.com")
		self.assertNotIn(inv, _find_stuck_pos_invoices(10))

	def test_past_day_sale_under_another_clerks_still_open_shift_is_swept(self):
		# Before the owner test this was "held while its shift is still open" — for a
		# close that, being owner-scoped, was never going to take it.
		self._open_shift("shift", started_days_ago=1, user="opener@example.com")
		inv = self._invoice("ammo-yday", item="PC-AMMO", days_ago=1, owner="clerk@example.com")
		self.assertIn(inv, _find_stuck_pos_invoices(10))

	def test_same_day_sale_under_another_clerks_abandoned_shift_is_not_swept_today(self):
		# An opening left Open past the dead-shift window holds nothing — and the
		# same-day branch keys on a LIVE other-clerk shift, so this waits for tomorrow.
		self._open_shift("dead", started_days_ago=5, user="opener@example.com")
		inv = self._invoice("ammo-today", item="PC-AMMO", days_ago=0, created_minutes_ago=60,
			owner="clerk@example.com")
		self.assertNotIn(inv, _find_stuck_pos_invoices(10))

	def test_own_open_shift_wins_over_another_clerks_on_the_same_register(self):
		# ERPNext allows one open shift per register; if two ever coexist (a raw
		# insert, a race), the owner's own shift must still hold the sale.
		self._open_shift("other", started_days_ago=0, hours_offset=-4, user="opener@example.com")
		self._open_shift("own", started_days_ago=0, hours_offset=-3, user="clerk@example.com")
		inv = self._invoice("ammo-today", item="PC-AMMO", days_ago=0, created_minutes_ago=60,
			owner="clerk@example.com")
		self.assertNotIn(inv, _find_stuck_pos_invoices(10))

	def test_same_day_sale_with_no_open_shift_still_waits_for_tonight(self):
		inv = self._invoice("ammo-today", item="PC-AMMO", days_ago=0, created_minutes_ago=60,
			owner="clerk@example.com")
		self.assertNotIn(inv, _find_stuck_pos_invoices(10))

	def test_already_consolidated_past_day_invoice_is_not_swept(self):
		inv = self._invoice("done", item="PC-AMMO", days_ago=1, consolidated="ACC-SINV-DONE")
		self.assertNotIn(inv, _find_stuck_pos_invoices(10))

	def test_past_day_return_is_swept_after_its_original(self):
		original = self._invoice("sale", item="PC-AMMO", days_ago=1)
		ret = self._invoice("return", item="PC-AMMO", days_ago=1, is_return=1, return_against=original)
		self._open_todo(original)  # even an already-alerted original still precedes its return
		found = _find_stuck_pos_invoices(10)
		self.assertIn(original, found)
		self.assertIn(ret, found)
		self.assertLess(found.index(original), found.index(ret))  # originals first within a run

	def test_older_than_lookback_is_left_to_a_human(self):
		old = self._invoice("old", item="PC-AMMO", days_ago=40)
		recent = self._invoice("recent", item="PC-AMMO", days_ago=20)
		found = _find_stuck_pos_invoices(10)
		self.assertNotIn(old, found)
		self.assertIn(recent, found)

	def test_abandoned_open_shift_no_longer_holds_its_invoices(self):
		# A shift left Open for days is broken, not "about to close": it must not
		# bury its sales silently.
		self._open_shift("dead-shift", started_days_ago=5)
		inv = self._invoice("ammo", item="PC-AMMO", days_ago=4)
		self.assertIn(inv, _find_stuck_pos_invoices(10))

	def test_null_posting_time_matches_no_shift_and_is_swept(self):
		# Mirrors ERPNext's closing query: Timestamp(date, NULL) is NULL there too,
		# so such an invoice can enter no closing entry — the sweep is its only path.
		self._open_shift("shift", started_days_ago=1)
		inv = self._invoice("no-time", item="PC-AMMO", days_ago=1, posting_time_null=True)
		self.assertIn(inv, _find_stuck_pos_invoices(10))

	# The class shares one transaction, so other tests' rows are candidates too.
	# The batch tests therefore date their rows at the oldest end of the lookback
	# window (oldest-first ordering hands them the single slot).

	def test_batch_bounds_the_missed_close_part_of_a_run(self):
		a = self._invoice("a", item="PC-AMMO", days_ago=29)
		b = self._invoice("b", item="PC-AMMO", days_ago=28)
		self.assertEqual({a, b} & set(_find_stuck_pos_invoices(10, batch=1)), {a})

	def test_firearm_branch_is_never_capped_and_comes_first(self):
		# The compliance path must not be starved by a backlog of past-day
		# leftovers: it has no cap of its own and rides outside the batch.
		gun = self._invoice("gun", item="PC-FIREARM", days_ago=0, created_minutes_ago=20)
		a = self._invoice("a", item="PC-AMMO", days_ago=30)   # = the lookback floor, inclusive
		b = self._invoice("b", item="PC-AMMO", days_ago=29)
		found = _find_stuck_pos_invoices(10, batch=1)
		self.assertIn(gun, found)
		self.assertEqual({a, b} & set(found), {a})            # (b) still bounded to one
		self.assertLess(found.index(gun), found.index(a))     # (a) rows first

	def test_invoices_with_an_open_stuck_todo_go_to_the_back_of_the_batch(self):
		# Tried, alerted, in someone's hands — must not keep taking the slots of
		# invoices nobody has looked at yet, even though it is the older one.
		older = self._invoice("older-alerted", item="PC-AMMO", days_ago=2)
		self._open_todo(older)
		newer = self._invoice("newer", item="PC-AMMO", days_ago=1)
		found = _find_stuck_pos_invoices(10)
		self.assertIn(older, found)
		self.assertLess(found.index(newer), found.index(older))

	def test_null_pos_profile_matches_no_shift_and_is_swept(self):
		# Same mirror as posting_time: ERPNext's closing query needs pos_profile
		# equality, so a NULL-profile invoice can enter no closing entry either.
		self._open_shift("shift", started_days_ago=1)
		inv = self._invoice("no-profile", item="PC-AMMO", days_ago=1, pos_profile_null=True)
		self.assertIn(inv, _find_stuck_pos_invoices(10))

	def test_original_out_of_reach_rules(self):
		from ffl_core.api.pos_consolidate import _original_out_of_reach
		self.assertTrue(_original_out_of_reach(None))                        # no original at all
		self.assertTrue(_original_out_of_reach("no-such-invoice"))           # missing
		old = self._invoice("old-original", item="PC-AMMO", days_ago=40)
		self.assertTrue(_original_out_of_reach(old))                         # beyond the lookback
		recent = self._invoice("recent-original", item="PC-AMMO", days_ago=1)
		self.assertFalse(_original_out_of_reach(recent))                     # the sweep will get to it

	def test_firearm_same_day_still_swept_after_settle_window_only(self):
		# Unchanged firearm behaviour: same-day is fine once past the settle
		# window (its immediate consolidation must have failed), not before.
		settled = self._invoice("gun-settled", item="PC-FIREARM", days_ago=0, created_minutes_ago=20)
		fresh = self._invoice("gun-fresh", item="PC-FIREARM", days_ago=0, created_minutes_ago=1)
		found = _find_stuck_pos_invoices(10)
		self.assertIn(settled, found)
		self.assertNotIn(fresh, found)


	def test_negative_trade_in_ticket_swept_like_a_firearm_sale(self):
		# A negative trade-in ticket merges on submit too, so a same-day one past the
		# settle window is a failed immediate merge — swept without waiting for tonight.
		settled = self._invoice("neg-settled", item="PC-AMMO", days_ago=0, created_minutes_ago=20, grand_total=-100)
		fresh = self._invoice("neg-fresh", item="PC-AMMO", days_ago=0, created_minutes_ago=1, grand_total=-100)
		ordinary = self._invoice("ammo-same-day", item="PC-AMMO", days_ago=0, created_minutes_ago=20, grand_total=100)
		found = _find_stuck_pos_invoices(10)
		self.assertIn(settled, found)
		self.assertNotIn(fresh, found)
		self.assertNotIn(ordinary, found)


	def _cancelled_merge(self, invoice: str) -> None:
		"""A cancelled POS Invoice Merge Log over `invoice`: what a manager leaves when un-merging it to cancel it."""
		name = f"{invoice}-ml"[:140]
		frappe.db.delete("POS Invoice Reference", {"parent": name})
		frappe.db.delete("POS Invoice Merge Log", {"name": name})
		frappe.get_doc({"doctype": "POS Invoice Merge Log", "name": name, "docstatus": 2,
			"merge_invoices_based_on": "Customer", "customer": self.CUSTOMER, "posting_date": nowdate()}).db_insert()
		frappe.get_doc({"doctype": "POS Invoice Reference", "name": f"{name}-row", "parent": name,
			"parenttype": "POS Invoice Merge Log", "parentfield": "pos_invoices", "idx": 1, "docstatus": 2,
			"pos_invoice": invoice}).db_insert()

	def test_negative_ticket_unmerged_on_purpose_is_left_by_both_branches(self):
		# Same day (firearm/negative branch) and a past day (missed-close branch): a negative
		# ticket whose merge log was cancelled is being cancelled by a manager — never re-merged.
		today = self._invoice("neg-unmerged-today", item="PC-AMMO", days_ago=0, created_minutes_ago=20, grand_total=-100)
		past = self._invoice("neg-unmerged-past", item="PC-AMMO", days_ago=1, grand_total=-100)
		ordinary = self._invoice("ammo-unmerged-past", item="PC-AMMO", days_ago=1, grand_total=100)
		for name in (today, past, ordinary):
			self._cancelled_merge(name)
		found = _find_stuck_pos_invoices(10)
		self.assertNotIn(today, found)
		self.assertNotIn(past, found)
		self.assertIn(ordinary, found)  # only negative tickets: an ordinary sale un-merged is still swept, as before

	# ------------------------------------------------- healed alerts close

	def _alert_todo(self, invoice: str, tag: str, description: str, user: str) -> str:
		name = f"{invoice}-{tag}"[:140]
		now = now_datetime()
		frappe.db.delete("ToDo", {"name": name})
		frappe.get_doc({
			"doctype": "ToDo", "name": name, "status": "Open", "priority": "High",
			"reference_type": "POS Invoice", "reference_name": invoice, "allocated_to": user,
			"description": description, "assigned_by": "Administrator",
			"creation": now, "modified": now, "owner": "Administrator", "modified_by": "Administrator",
		}).db_insert()
		return name

	def test_stuck_todos_close_once_the_invoice_has_merged_however_it_merged(self):
		# alert_once never closed them: a past invoice healed on a later sweep and
		# kept one open High ToDo per manager. Closed here whoever merged it.
		from ffl_core.api import pos_consolidate as m
		healed = self._invoice("healed", item="PC-FIREARM", days_ago=0, consolidated="ACC-SINV-HEALED")
		stuck = self._invoice("stuck", item="PC-FIREARM", days_ago=0)
		mark = f"POS Invoice {{}} {m._STUCK_TODO_MARK} — its sale has not posted to the books."
		healed_todo = self._alert_todo(healed, "stuck", mark.format(healed), "Administrator")
		# _alert_stuck's text arriving HTML-wrapped (the field is a Text Editor) still matches
		wrapped_todo = self._alert_todo(healed, "stuck-p", f"<p>{mark.format(healed)}</p>", "Guest")
		other_alert = self._alert_todo(healed, "4473", f"Abandoned 4473 — {healed} has held X", "Guest")
		still_stuck = self._alert_todo(stuck, "stuck", mark.format(stuck), "Administrator")
		frappe.db.set_value("POS Invoice", healed, "_assign", '["Administrator", "Guest"]',
			update_modified=False)
		with patch.object(m.frappe.db, "commit"):
			m._close_healed_alerts()
		status = lambda n: frappe.db.get_value("ToDo", n, "status")  # noqa: E731
		self.assertEqual(status(healed_todo), "Closed")
		self.assertEqual(status(wrapped_todo), "Closed")
		self.assertEqual(status(other_alert), "Open")   # a different alert on the healed invoice
		self.assertEqual(status(still_stuck), "Open")   # not merged yet: still a real alert
		# saved, not db.set_value: the invoice's assignee cache drops the closed ones
		self.assertEqual(frappe.db.get_value("POS Invoice", healed, "_assign"), '["Guest"]')

	def test_closing_healed_alerts_never_raises(self):
		from ffl_core.api import pos_consolidate as m
		with patch.object(m.frappe.db, "sql", side_effect=frappe.ValidationError("boom")), \
			patch.object(m.frappe.db, "rollback") as rollback:
			m._close_healed_alerts()   # end-of-sweep housekeeping: must not fail the sweep
		rollback.assert_called_once()


class WarnOwnerShiftMismatch(unittest.TestCase):
	"""POS Invoice validate: an alert (never a throw) when the register's open
	shift belongs to another account."""

	def _run(self, doc, shift):
		from ffl_core.api.pos_consolidate import warn_owner_shift_mismatch
		# The covering-shift lookup is pos_session.open_shift_for_profile, called
		# with the sweep's own bounds (started at or before now, not abandoned).
		with patch("ffl_core.api.pos_session.open_shift_for_profile", return_value=shift) as lookup, \
			 patch("frappe.msgprint") as msgprint:
			warn_owner_shift_mismatch(doc)
		return lookup, msgprint

	def test_alerts_when_the_open_shift_was_opened_by_someone_else(self):
		doc = frappe._dict(docstatus=1, is_pos=1, pos_profile="POS1", owner="clerk@example.com")
		lookup, msgprint = self._run(doc, frappe._dict(name="POS-OPE-1", user="opener@example.com"))
		msgprint.assert_called_once()
		self.assertEqual(lookup.call_args.args[0], "POS1")
		self.assertEqual(lookup.call_args.kwargs["dead_after_days"], 3)
		self.assertEqual(msgprint.call_args.kwargs.get("indicator"), "orange")
		self.assertTrue(msgprint.call_args.kwargs.get("alert"))
		self.assertIn("opener@example.com", msgprint.call_args[0][0])

	def test_silent_when_the_clerk_opened_the_shift(self):
		doc = frappe._dict(docstatus=1, is_pos=1, pos_profile="POS1", owner="clerk@example.com")
		_lookup, msgprint = self._run(doc, frappe._dict(name="POS-OPE-1", user="clerk@example.com"))
		msgprint.assert_not_called()

	def test_silent_when_no_shift_is_open(self):
		doc = frappe._dict(docstatus=1, is_pos=1, pos_profile="POS1", owner="clerk@example.com")
		_lookup, msgprint = self._run(doc, None)
		msgprint.assert_not_called()

	def test_only_fires_on_submit_of_a_pos_invoice(self):
		draft = frappe._dict(docstatus=0, is_pos=1, pos_profile="POS1", owner="clerk@example.com")
		desk = frappe._dict(docstatus=1, is_pos=0, pos_profile="POS1", owner="clerk@example.com")
		for doc in (draft, desk):
			lookup, msgprint = self._run(doc, frappe._dict(name="x", user="opener@example.com"))
			lookup.assert_not_called()   # no shift lookup at all
			msgprint.assert_not_called()


class SerialCaseCanonical(FrappeTestCase):
	"""A past incident: line serial '1976ON-4786' vs stored
	'1976on-4786' — MariaDB let the Link through, ERPNext's case-sensitive
	bundle check killed consolidation on every retry."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import item_groups, test_bootstrap
		test_bootstrap.run()
		item_groups.ensure()
		_item("PC-FIREARM", "Pistol", 1)
		if not frappe.db.exists("Serial No", "pc-case-0001"):
			frappe.get_doc({"doctype": "Serial No", "serial_no": "pc-case-0001",
				"item_code": "PC-FIREARM"}).insert(ignore_permissions=True)

	def test_validate_rewrites_line_serial_to_stored_case(self):
		from ffl_core.compliance.firearm import canonicalize_row_serials
		doc = frappe._dict(items=[
			frappe._dict(item_code="PC-FIREARM", serial_no="PC-CASE-0001"),
			frappe._dict(item_code="PC-FIREARM", serial_no="NO-SUCH-SERIAL"),
			frappe._dict(item_code="PC-AMMO", serial_no=None),
		])
		canonicalize_row_serials(doc)
		self.assertEqual(doc["items"][0].serial_no, "pc-case-0001")
		self.assertEqual(doc["items"][1].serial_no, "NO-SUCH-SERIAL")  # Link check's job
		self.assertIsNone(doc["items"][2].serial_no)

	def test_multi_serial_line_keeps_newline_join(self):
		from ffl_core.compliance.firearm import canonicalize_serial_string
		self.assertEqual(canonicalize_serial_string("PC-CASE-0001\nOTHER"),
			"pc-case-0001\nOTHER")

	def test_repair_rewrites_only_mismatched_submitted_rows(self):
		from ffl_core.api.pos_consolidate import _repair_serial_case
		rows = [frappe._dict(name="r1", serial_no="PC-CASE-0001"),
			frappe._dict(name="r2", serial_no="pc-case-0001")]
		with patch.object(frappe, "get_all", return_value=rows), \
			patch.object(frappe.db, "set_value") as sv:
			_repair_serial_case("ACC-PSINV-TEST")
		sv.assert_called_once_with("POS Invoice Item", "r1", "serial_no", "pc-case-0001",
			update_modified=False)

if __name__ == "__main__":
	unittest.main()
