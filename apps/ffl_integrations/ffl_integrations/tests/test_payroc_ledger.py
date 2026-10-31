"""Payroc transaction ledger — gateway rows matched to the POS, with a flag
wherever the two disagree (the accountant's read-only view)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.payroc import ledger
from ffl_integrations.payroc.client import PayrocClient


def _txn(ref, order_id, amount, ttype="SALE", status="COMPLETE"):
	return {"uniqueReference": ref, "orderId": order_id, "amount": amount, "type": ttype,
		"status": status, "batchStatus": "COMPLETE", "dateTime": "2026-09-23T10:00:00-05:00",
		"cardType": "VISA", "maskedPan": "411111******1234", "customerName": "A BUYER",
		"terminal": "380572001"}


def _pos(doctype, name, amount, status="CHARGED", docstatus=1):
	return frappe._dict(doctype=doctype, name=name, amount=amount, status=status,
		docstatus=docstatus)


POS_BY_REF = {
	"CNTROK0001": _pos("POS Invoice", "ACC-PSINV-T-1", 100.0),
	"CNTRDIFF01": _pos("POS Invoice", "ACC-PSINV-T-2", 90.0),
	"CNTRVOID01": _pos("POS Invoice", "ACC-PSINV-T-3", 50.0),
	"POSVOIDED1": _pos("POS Invoice", "ACC-PSINV-T-4", 60.0, status="VOIDED"),
	"REFUNDVOID": _pos("Sales Invoice", "ACC-SINV-T-5", 70.0),
	"CANCELLED1": _pos("POS Invoice", "ACC-PSINV-T-6", 80.0, docstatus=2),
}
POS_BY_ORDER = {
	"5175": _pos("Woo Online Order", "WOO-ORD-T1", 1345.0, status=None, docstatus=0),
	"6000": _pos("Woo Online Order", "WOO-ORD-T2", 500.0, status=None, docstatus=0),
	"ACC-PSINV-T-1": _pos("POS Invoice", "ACC-PSINV-T-1", None, status=None),
	"ACC-PSINV-T-9-A2": _pos("POS Invoice", "ACC-PSINV-T-9", None, status=None),
}


class TestPayrocLedger(FrappeTestCase):
	def _run(self, gateway, *, refunded=None, pos_charges=(), payments=None, truncated=False,
			order_history=None):
		client = MagicMock()
		client.search_transactions.return_value = (gateway, truncated)
		client.retrieve_payment.side_effect = lambda ref: (payments or {})[ref]
		client.find_transaction.side_effect = \
			lambda order_id: {"data": (order_history or {}).get(order_id, [])}
		with patch.object(ledger, "payroc_ready", return_value=True), \
				patch.object(ledger, "PayrocClient") as pc, \
				patch.object(ledger, "_pos_by_ref", side_effect=POS_BY_REF.get), \
				patch.object(ledger, "_pos_by_order", side_effect=POS_BY_ORDER.get), \
				patch.object(ledger, "_pos_refunded",
					side_effect=lambda dt, name: (refunded or {}).get(name, 0.0)), \
				patch.object(ledger.reconcile, "_captured_charges",
					return_value=list(pos_charges)) as cc:
			pc.from_settings.return_value = client
			out = ledger.payroc_transactions("2026-09-01", "2026-09-30")
		return out, client, cc

	def _row(self, out, ref):
		return next(r for r in out["transactions"] if r["reference"] == ref)

	def test_matches_counter_and_web_and_flags_disagreements(self):
		out, client, cc = self._run([
			_txn("CNTROK0001", "ACC-PSINV-T-1", 100.0),
			_txn("CNTRDIFF01", "ACC-PSINV-T-2", 95.0),
			_txn("CNTRVOID01", "ACC-PSINV-T-3", 50.0, status="VOID"),
			_txn("WEBSALE001", "5175", 1345.0),
			_txn("WEBREFUND1", "5175", 1117.73, ttype="REFUND"),
			_txn("STRANGER01", "9999999", 20.0),
			_txn("DECLINED01", "9999998", 30.0, status="DECLINED"),
		])
		ok = self._row(out, "CNTROK0001")
		self.assertEqual((ok["source"], ok["pos_name"], ok["flags"]), ("counter", "ACC-PSINV-T-1", []))
		self.assertEqual(ok["card"], "VISA *1234", "only the last 4 digits leave the POS")
		self.assertIn("amount differs", self._row(out, "CNTRDIFF01")["flags"][0])
		self.assertEqual(self._row(out, "CNTRVOID01")["flags"], ["POS shows charged, gateway VOID"])
		web = self._row(out, "WEBSALE001")
		self.assertEqual((web["source"], web["pos_name"], web["flags"]), ("web", "WOO-ORD-T1", []))
		# The Tyler case: refunded in Woo / the portal, never recorded on the POS side.
		refund = self._row(out, "WEBREFUND1")
		self.assertIn("refund not recorded in POS", refund["flags"][0])
		self.assertEqual(self._row(out, "STRANGER01")["flags"], ["no POS record for this sale"])
		self.assertEqual(self._row(out, "DECLINED01")["flags"], [], "a decline took no money")

		s = out["summary"]
		self.assertEqual((s["sales"], s["sales_total"]), (4, 1560.0))
		self.assertEqual((s["refunds_total"], s["net"]), (1117.73, 442.27))
		self.assertEqual((s["counter_sales_total"], s["web_sales_total"]), (195.0, 1345.0))
		self.assertEqual(s["flagged"], 4)
		# Store-time-zone window with an offset: the gateway 400s on a bare date.
		kw = client.search_transactions.call_args.kwargs
		self.assertRegex(kw["after"], r"^2026-09-01T00:00:00[+-]\d\d:\d\d$")
		self.assertRegex(kw["before"], r"^2026-10-01T00:00:00[+-]\d\d:\d\d$")
		self.assertIsNone(cc.call_args.args[0].tzinfo)

	def test_gateway_negative_refund_amounts(self):
		"""Prod 2026-09-30: the gateway returns refunds as NEGATIVE amounts. Net
		must subtract them, and an unrecorded one must still be flagged."""
		out, _, _ = self._run([_txn("WEBSALE001", "5175", 1345.0),
			_txn("WEBREFUND1", "5175", -1117.73, ttype="REFUND")])
		self.assertEqual(self._row(out, "WEBREFUND1")["amount"], 1117.73)
		self.assertIn("refund not recorded in POS", self._row(out, "WEBREFUND1")["flags"][0])
		s = out["summary"]
		self.assertEqual((s["refunds_total"], s["net"]), (1117.73, 227.27))

	def test_virtual_terminal_charge_is_named(self):
		out, _, _ = self._run([_txn("VTCHARGE01", "VT0000000041", 944.0)])
		row = self._row(out, "VTCHARGE01")
		self.assertEqual(row["source"], "virtual terminal")
		self.assertIn("virtual-terminal charge", row["flags"][0])
		self.assertEqual(out["summary"]["virtual_terminal_sales_total"], 944.0)

	def test_recorded_refund_is_not_flagged(self):
		out, _, _ = self._run([_txn("WEBREFUND1", "5175", 1117.73, ttype="REFUND")],
			refunded={"WOO-ORD-T1": 1117.73})
		self.assertEqual(self._row(out, "WEBREFUND1")["flags"], [])

	def test_pos_capture_missing_from_the_search_is_read_back(self):
		charges = [frappe._dict(ref="INWINDOW01", amount=10.0, refunded_amount=0,
				doctype="POS Invoice", name="P-1"),
			frappe._dict(ref="DOWN000001", amount=50.0, refunded_amount=0,
				doctype="Sales Invoice", name="S-3"),
			frappe._dict(ref="EDGEOK0001", amount=20.0, refunded_amount=0,
				doctype="POS Invoice", name="P-2"),
			frappe._dict(ref="VOIDED0001", amount=30.0, refunded_amount=0,
				doctype="Sales Invoice", name="S-1"),
			frappe._dict(ref="GONE000001", amount=40.0, refunded_amount=0,
				doctype="Sales Invoice", name="S-2")]
		payments = {"EDGEOK0001": {"transactionResult": {"status": "COMPLETE"}},
			"VOIDED0001": {"transactionResult": {"status": "VOID"}}}

		def retrieve(ref):
			if ref == "DOWN000001":
				raise RuntimeError("Payroc GET /api/v1/transaction/payments/DOWN000001 503: down")
			if ref not in payments:
				raise RuntimeError(f"Payroc GET /api/v1/transaction/payments/{ref} 404: none")
			return payments[ref]

		gateway = [_txn("INWINDOW01", "P-1", 10.0)]
		client = MagicMock()
		client.search_transactions.return_value = (gateway, False)
		client.retrieve_payment.side_effect = retrieve
		with patch.object(ledger, "payroc_ready", return_value=True), \
				patch.object(ledger, "PayrocClient") as pc, \
				patch.object(ledger, "_pos_by_ref", return_value=None), \
				patch.object(ledger, "_pos_by_order", return_value=None), \
				patch.object(ledger.reconcile, "_captured_charges", return_value=charges), \
				patch.object(ledger, "safe_log_error") as log:
			pc.from_settings.return_value = client
			out = ledger.payroc_transactions("2026-09-01", "2026-09-30")
		only = {r["reference"]: r["flags"] for r in out["pos_only"]}
		self.assertEqual(only, {"EDGEOK0001": [],
			"VOIDED0001": ["POS shows charged, gateway status VOID"],
			"GONE000001": ["not found at the gateway"],
			# An outage is not evidence the capture is missing.
			"DOWN000001": ["gateway read failed — status unknown"]})
		log.assert_called_once()

	def test_capture_the_pos_never_recorded_is_flagged(self):
		"""A held capture whose reference no POS charge carries — a timed-out
		attempt the gateway kept, or a terminal double charge — must not pass
		just because its orderId names a real invoice."""
		out, _, _ = self._run([_txn("ORPHAN0001", "ACC-PSINV-T-9-A2", 40.0)])
		row = self._row(out, "ORPHAN0001")
		self.assertEqual((row["source"], row["pos_name"]), ("counter", "ACC-PSINV-T-9"))
		self.assertEqual(row["flags"],
			["capture not recorded on the POS invoice (reference unknown to POS)"])

	def test_gateway_holds_what_the_pos_says_was_voided(self):
		out, _, _ = self._run([_txn("POSVOIDED1", "ACC-PSINV-T-4", 60.0)])
		self.assertEqual(self._row(out, "POSVOIDED1")["flags"],
			["gateway holds this capture, POS status VOIDED"])

	def test_void_after_a_recorded_full_refund_is_normal(self):
		out, _, _ = self._run([_txn("REFUNDVOID", "ACC-SINV-T-5", 70.0, status="VOID")],
			refunded={"ACC-SINV-T-5": 70.0})
		self.assertEqual(self._row(out, "REFUNDVOID")["flags"], [])

	def test_held_capture_on_a_cancelled_invoice_is_flagged(self):
		out, _, _ = self._run([_txn("CANCELLED1", "ACC-PSINV-T-6", 80.0)])
		self.assertEqual(self._row(out, "CANCELLED1")["flags"],
			["POS POS Invoice ACC-PSINV-T-6 is cancelled"])

	def test_an_earlier_recorded_refund_does_not_hide_a_later_portal_one(self):
		"""August refund 500 recorded in Woo; September portal refund 300 never
		recorded. The September window sees 300 vs POS 500 — the all-time
		gateway history (800) is what the POS side must be compared with."""
		history = {"5175": [_txn("AUGREFUND1", "5175", 500.0, ttype="REFUND"),
			_txn("SEPREFUND1", "5175", 300.0, ttype="REFUND"),
			_txn("WEBSALE001", "5175", 1345.0)]}
		out, _, _ = self._run([_txn("SEPREFUND1", "5175", 300.0, ttype="REFUND")],
			refunded={"WOO-ORD-T1": 500.0}, order_history=history)
		self.assertIn("gateway 800.00", self._row(out, "SEPREFUND1")["flags"][0])

	def test_two_captures_on_one_web_order(self):
		out, _, _ = self._run([_txn("WEBDUP0001", "6000", 500.0),
			_txn("WEBDUP0002", "6000", 500.0)])
		for ref in ("WEBDUP0001", "WEBDUP0002"):
			self.assertEqual(self._row(out, ref)["flags"], ["2 captures for one web order"])

	def test_unknown_gateway_type_is_flagged_not_dropped(self):
		out, _, _ = self._run([_txn("PREAUTH001", "ACC-PSINV-T-1", 25.0, ttype="PREAUTHORIZATION")])
		self.assertIn("not handled here", self._row(out, "PREAUTH001")["flags"][0])
		self.assertEqual(out["summary"]["other_types"], 1)

	def test_truncated_search_skips_the_pos_only_read_back(self):
		charges = [frappe._dict(ref="OLDDAY0001", amount=10.0, refunded_amount=0,
			doctype="POS Invoice", name="P-1")]
		out, client, _ = self._run([], pos_charges=charges, truncated=True)
		self.assertTrue(out["truncated"])
		self.assertEqual(out["pos_only"], [])
		client.retrieve_payment.assert_not_called()

	def test_range_and_role_checks(self):
		with self.assertRaises(frappe.ValidationError):
			ledger.payroc_transactions("2026-09-30", "2026-09-01")
		with self.assertRaises(frappe.ValidationError):
			ledger.payroc_transactions("2026-08-01", "2026-09-30")
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				ledger.payroc_transactions("2026-09-01", "2026-09-30")
		finally:
			frappe.set_user("Administrator")

	def test_sales_invoice_card_refund_counts_as_recorded(self):
		# si_reversal records a counter SI refund on the Card Terminal Payment row;
		# a voided row (no gateway REFUND) and another processor's row do not count.
		si = "ACC-SINV-LEDGER-T-1"
		for status, amount, refunded, processor in (("REFUNDED", 100, 100, "payroc"),
				("CHARGED", 50, 20, ""), ("VOIDED", 30, 30, "payroc"),
				("CHARGED", 40, 40, "other")):
			doc = frappe.get_doc({"doctype": "Card Terminal Payment", "sales_invoice": si,
				"amount": amount, "refunded_amount": refunded, "status": status,
				"processor": processor})
			doc.flags.ignore_links = True
			doc.insert(ignore_permissions=True)
		self.assertEqual(ledger._pos_refunded("Sales Invoice", si), 120.0)

	def test_order_id_attempt_suffix_names_the_invoice(self):
		with patch.object(ledger.frappe.db, "get_value",
				side_effect=lambda dt, name, field: 1 if (dt, name) == ("POS Invoice",
					"ACC-PSINV-2026-99001") else None):
			pos = ledger._pos_by_order("ACC-PSINV-2026-99001-A2")
		self.assertEqual((pos.doctype, pos.name), ("POS Invoice", "ACC-PSINV-2026-99001"))


class TestSearchTransactions(FrappeTestCase):
	def _client(self, pages):
		client = PayrocClient.__new__(PayrocClient)
		calls = []

		def fake_request(method, path, **kw):
			calls.append(path)
			return pages[len(calls) - 1]

		client._request = fake_request
		return client, calls

	def test_follows_the_next_cursor(self):
		client, calls = self._client([{"data": [{"uniqueReference": "A"}], "next": "CUR1"},
			{"data": [{"uniqueReference": "B"}]}])
		rows, truncated = client.search_transactions(
			after="2026-09-22T00:00:00-05:00", before="2026-09-24T00:00:00-05:00")
		self.assertEqual([r["uniqueReference"] for r in rows], ["A", "B"])
		self.assertFalse(truncated)
		self.assertIn("afterDate=2026-09-22T00%3A00%3A00-05%3A00", calls[0])
		self.assertTrue(calls[1].endswith("&next=CUR1"))

	def test_time_budget_stops_paging(self):
		client, calls = self._client([{"data": [{}], "next": "X"}] * 5)
		with patch("ffl_integrations.payroc.client.time.monotonic", side_effect=[0, 10, 100]):
			rows, truncated = client.search_transactions(after="a", before="b", time_budget=50)
		self.assertEqual((len(calls), truncated), (2, True))

	def test_reports_truncation(self):
		client, _ = self._client([{"data": [{}], "next": "X"}] * 3)
		rows, truncated = client.search_transactions(after="a", before="b", max_pages=2)
		self.assertEqual((len(rows), truncated), (2, True))
