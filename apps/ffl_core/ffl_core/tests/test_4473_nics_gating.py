"""Spike 2: SI / POS Invoice with firearm lines is blocked unless 4473 = Completed
AND NICS = Proceed.

Verifies the gating rule itself; integration with real FastBound webhook payloads
is part of Phase 3.

Run inside the bench container:
    bench --site dev.localhost run-tests --module ffl_core.tests.test_4473_nics_gating
"""
from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.compliance.firearm import require_4473_and_nics_for_firearm_sale
from ffl_core.firearm import is_antique_item, is_firearm_item, is_regulated_firearm_item


def refuse(doc):
	"""A gunshop_firearm_transfer_refusal provider with an objection."""
	if getattr(refuse, "seen", None) is not None:
		refuse.seen.append(doc)
	return "The birth date differs between the 4473 and the ID."


def no_objection(doc):
	return None


def broken(doc):
	raise RuntimeError("provider bug")


class CoveredSerialsForInvoice(FrappeTestCase):
	"""The shared coverage predicate — single source of truth for the submit gate
	(_require_disposition_coverage) AND the webhook's early mismatch alarm
	(_alert_invoice_serial_coverage). They must never drift."""

	def test_returns_only_the_covered_subset(self):
		from ffl_core.compliance.firearm import covered_serials_for_invoice
		with patch("frappe.get_all", return_value=["S1"]) as ga:
			out = covered_serials_for_invoice("INV-1", ["S1", "S2"])
		self.assertEqual(out, {"S1"})
		# Queries FFL Disposition with the gate's exact Retail Sale / Completed /
		# Proceed criteria (so alarm and gate can't diverge).
		f = ga.call_args.kwargs["filters"]
		self.assertEqual(f["linked_pos_invoice"], "INV-1")
		self.assertEqual(f["disposition_type"], "Retail Sale")
		self.assertEqual(f["form_4473_status"], "Completed")
		self.assertIn("Proceed", list(f["nics_status"][1]))
		self.assertEqual(set(f["serial"][1]), {"S1", "S2"})

	def test_empty_serials_short_circuits_without_a_query(self):
		from ffl_core.compliance.firearm import covered_serials_for_invoice
		with patch("frappe.get_all") as ga:
			out = covered_serials_for_invoice("INV-1", [])
		self.assertEqual(out, set())
		ga.assert_not_called()

	def test_only_counts_submitted_dispositions(self):
		"""A cancelled (docstatus=2) or draft (0) Retail Sale disposition must NOT
		prove a 4473: the gate would otherwise pass on a voided bound-book record,
		and manager_redo_4473's cancellation could be silently undone by the
		counter's Refresh button re-reading those rows as coverage."""
		from ffl_core.compliance.firearm import covered_serials_for_invoice
		with patch("frappe.get_all", return_value=["S1"]) as ga:
			covered_serials_for_invoice("INV-1", ["S1"])
		self.assertEqual(ga.call_args.kwargs["filters"]["docstatus"], 1)


class FourFiveSevenThreeAndNicsGating(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls._ensure_item_group("All Item Groups", is_group=1, parent=None)
		cls._ensure_item_group("Firearm", is_group=0, parent="All Item Groups")
		cls._ensure_item_group("Accessory", is_group=0, parent="All Item Groups")
		cls._ensure_uom("Nos")
		cls._ensure_item("FFL-TEST-GUN", item_group="Firearm", has_serial_no=1)
		cls._ensure_item("FFL-TEST-ACC", item_group="Accessory", has_serial_no=0)
		# Antique firearm: a firearm Item flagged is_antique=1 — GCA-exempt, so the
		# 4473 gate must treat it like a non-firearm while inventory still sees a gun.
		cls._ensure_item("FFL-TEST-ANTIQUE", item_group="Firearm", has_serial_no=1)
		frappe.db.set_value("Item", "FFL-TEST-ANTIQUE", "is_antique", 1, update_modified=False)
		cls._ensure_ffl_customer("FFL-DEALER-CUST")

	@staticmethod
	def _ensure_ffl_customer(name: str) -> None:
		if not frappe.db.exists("Customer Group", "All Customer Groups"):
			frappe.get_doc({"doctype": "Customer Group",
				"customer_group_name": "All Customer Groups", "is_group": 1}).insert(ignore_permissions=True)
		if not frappe.db.exists("Customer Group", "FFL Dealers"):
			frappe.get_doc({"doctype": "Customer Group", "customer_group_name": "FFL Dealers",
				"is_group": 0, "parent_customer_group": "All Customer Groups"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Territory", "All Territories"):
			frappe.get_doc({"doctype": "Territory",
				"territory_name": "All Territories", "is_group": 1}).insert(ignore_permissions=True)
		if not frappe.db.exists("Territory", "FFL Territory"):
			frappe.get_doc({"doctype": "Territory", "territory_name": "FFL Territory",
				"is_group": 0, "parent_territory": "All Territories"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Customer", name):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": name,
				"customer_group": "FFL Dealers", "territory": "FFL Territory",
				"is_ffl_dealer": 1, "ffl_number": "1-77-777-77-7X-77777",
			}).insert(ignore_permissions=True)

	@staticmethod
	def _ensure_item_group(name: str, is_group: int, parent: str | None) -> None:
		if frappe.db.exists("Item Group", name):
			return
		doc = frappe.get_doc({
			"doctype": "Item Group",
			"item_group_name": name,
			"is_group": is_group,
		})
		if parent:
			doc.parent_item_group = parent
		doc.insert(ignore_permissions=True)

	@staticmethod
	def _ensure_uom(name: str) -> None:
		if not frappe.db.exists("UOM", name):
			frappe.get_doc({"doctype": "UOM", "uom_name": name, "must_be_whole_number": 1}).insert(ignore_permissions=True)

	@staticmethod
	def _ensure_item(code: str, item_group: str, has_serial_no: int) -> None:
		if frappe.db.exists("Item", code):
			return
		frappe.get_doc({
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": item_group,
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": has_serial_no,
			"serial_no_series": f"{code}-.####" if has_serial_no else "",
		}).insert(ignore_permissions=True)

	@staticmethod
	def _make_doc(*lines: tuple[str, str], form_status: str = "", nics_status: str = "",
		docstatus: int = 1, customer: str | None = None) -> SimpleNamespace:
		"""Build a minimal doc-like for compliance hook tests.

		`docstatus` defaults to 1 (submit) because the hook only runs at
		submit time. Set docstatus=0 to verify Draft saves are NOT blocked.
		`customer` lets a test exercise the FFL-to-FFL transfer skip.
		"""
		return SimpleNamespace(
			items=[
				SimpleNamespace(idx=i + 1, item_code=code, serial_no=serial)
				for i, (code, serial) in enumerate(lines)
			],
			form_4473_status=form_status,
			nics_status=nics_status,
			docstatus=docstatus,
			customer=customer,
		)

	# ------------------------------------------------------------------ tests

	def test_firearm_sale_blocked_without_4473(self):
		doc = self._make_doc(("FFL-TEST-GUN", "G1"), form_status="", nics_status="Proceed")
		with self.assertRaises(frappe.ValidationError) as ctx:
			require_4473_and_nics_for_firearm_sale(doc)
		self.assertIn("4473", str(ctx.exception))

	def test_firearm_sale_blocked_with_pending_4473(self):
		doc = self._make_doc(("FFL-TEST-GUN", "G1"), form_status="Pending", nics_status="Proceed")
		with self.assertRaises(frappe.ValidationError):
			require_4473_and_nics_for_firearm_sale(doc)

	def test_firearm_sale_blocked_without_nics(self):
		doc = self._make_doc(("FFL-TEST-GUN", "G1"), form_status="Completed", nics_status="")
		with self.assertRaises(frappe.ValidationError) as ctx:
			require_4473_and_nics_for_firearm_sale(doc)
		self.assertIn("NICS", str(ctx.exception))

	def test_firearm_sale_blocked_with_nics_delayed(self):
		# "Delayed" alone is not sufficient — store cannot proceed.
		doc = self._make_doc(("FFL-TEST-GUN", "G1"), form_status="Completed", nics_status="Delayed")
		with self.assertRaises(frappe.ValidationError):
			require_4473_and_nics_for_firearm_sale(doc)

	def test_firearm_sale_blocked_with_nics_denied(self):
		doc = self._make_doc(("FFL-TEST-GUN", "G1"), form_status="Completed", nics_status="Denied")
		with self.assertRaises(frappe.ValidationError):
			require_4473_and_nics_for_firearm_sale(doc)

	def test_firearm_sale_passes_when_4473_completed_and_nics_proceed(self):
		doc = self._make_doc(("FFL-TEST-GUN", "G1"), form_status="Completed", nics_status="Proceed")
		require_4473_and_nics_for_firearm_sale(doc)  # must not raise

	# --- gunshop_firearm_transfer_refusal: an extension's own reason to stop the sale -------

	@staticmethod
	def _providers(*paths):
		real = frappe.get_hooks

		def hooks(name=None, *a, **k):
			return list(paths) if name == "gunshop_firearm_transfer_refusal" else real(name, *a, **k)
		return patch("frappe.get_hooks", side_effect=hooks)

	def test_an_extension_can_refuse_a_sale_that_cleared_4473_and_nics(self):
		doc = self._make_doc(("FFL-TEST-GUN", "G1"), form_status="Completed", nics_status="Proceed")
		with self._providers("ffl_core.tests.test_4473_nics_gating.refuse"), \
				self.assertRaises(frappe.ValidationError) as ctx:
			require_4473_and_nics_for_firearm_sale(doc)
		self.assertIn("birth date differs", str(ctx.exception))

	def test_an_extension_with_no_objection_lets_the_sale_through(self):
		doc = self._make_doc(("FFL-TEST-GUN", "G1"), form_status="Completed", nics_status="Proceed")
		with self._providers("ffl_core.tests.test_4473_nics_gating.no_objection"):
			require_4473_and_nics_for_firearm_sale(doc)  # must not raise

	def test_a_failing_extension_is_logged_and_does_not_stop_the_sale(self):
		doc = self._make_doc(("FFL-TEST-GUN", "G1"), form_status="Completed", nics_status="Proceed")
		with self._providers("ffl_core.tests.test_4473_nics_gating.broken"), patch("frappe.log_error") as log:
			require_4473_and_nics_for_firearm_sale(doc)  # must not raise
		self.assertIn("gunshop_firearm_transfer_refusal", log.call_args.kwargs["title"])

	def test_an_extension_is_never_asked_before_4473_and_nics_have_cleared(self):
		asked = []
		refuse.seen = asked
		try:
			for kw in ({"form_status": "Pending", "nics_status": ""}, {"form_status": "Completed", "nics_status": "Delayed"}):
				with self._providers("ffl_core.tests.test_4473_nics_gating.refuse"), self.assertRaises(frappe.ValidationError):
					require_4473_and_nics_for_firearm_sale(self._make_doc(("FFL-TEST-GUN", "G1"), **kw))
			with self._providers("ffl_core.tests.test_4473_nics_gating.refuse"):
				require_4473_and_nics_for_firearm_sale(self._make_doc(("FFL-TEST-ACC", None)))      # no firearm
				require_4473_and_nics_for_firearm_sale(self._make_doc(("FFL-TEST-GUN", "G1"), docstatus=0))  # a draft
		finally:
			refuse.seen = None
		self.assertEqual(asked, [])

	def test_ffl_dealer_transfer_skips_4473(self):
		# Selling a firearm to another FFL dealer (is_ffl_dealer customer) is a
		# dealer-to-dealer transfer — no 4473 / NICS required.
		doc = self._make_doc(("FFL-TEST-GUN", "G1"), form_status="", nics_status="",
			customer="FFL-DEALER-CUST")
		require_4473_and_nics_for_firearm_sale(doc)  # must not raise

	def test_non_firearm_sale_does_not_require_4473(self):
		doc = self._make_doc(("FFL-TEST-ACC", ""), form_status="", nics_status="")
		require_4473_and_nics_for_firearm_sale(doc)  # must not raise

	def test_mixed_cart_with_firearm_still_requires_compliance(self):
		doc = self._make_doc(
			("FFL-TEST-ACC", ""),
			("FFL-TEST-GUN", "G1"),
			form_status="",
			nics_status="",
		)
		with self.assertRaises(frappe.ValidationError):
			require_4473_and_nics_for_firearm_sale(doc)

	def test_draft_with_firearm_does_not_block(self):
		"""POS hold flow: cashier can save Draft with firearm but no 4473 yet.

		Validate hook should only fire on submit (docstatus=1). On Draft
		(docstatus=0), saving a cart with a firearm and empty 4473 fields
		must not raise — otherwise the 4473 button (which saves the doc as
		Draft) can't even run.
		"""
		doc = self._make_doc(
			("FFL-TEST-GUN", "G1"),
			form_status="", nics_status="", docstatus=0,
		)
		# Must not raise.
		require_4473_and_nics_for_firearm_sale(doc)

	def test_draft_with_firearm_pending_4473_does_not_block(self):
		doc = self._make_doc(
			("FFL-TEST-GUN", "G1"),
			form_status="Pending", nics_status="N/A", docstatus=0,
		)
		require_4473_and_nics_for_firearm_sale(doc)

	# ------------------------------------------------------ antique firearms

	def test_antique_only_sale_does_not_require_4473(self):
		# A GCA-exempt antique is a firearm for inventory but NOT for the 4473
		# gate — an antique-only sale must submit with no 4473 / NICS.
		doc = self._make_doc(("FFL-TEST-ANTIQUE", "ANTIQUE-1"), form_status="", nics_status="")
		require_4473_and_nics_for_firearm_sale(doc)  # must not raise

	def test_mixed_antique_and_regulated_still_requires_4473(self):
		# The antique is exempt, but the regulated gun on the same cart still gates.
		doc = self._make_doc(
			("FFL-TEST-ANTIQUE", "ANTIQUE-1"),
			("FFL-TEST-GUN", "G1"),
			form_status="", nics_status="",
		)
		with self.assertRaises(frappe.ValidationError):
			require_4473_and_nics_for_firearm_sale(doc)

	def test_counter_pickup_invoice_that_moves_no_stock_skips_the_gate(self):
		"""A counter pickup of a gun ordered to the store (api/counter_pickup): the
		Sales Invoice books the sale with update_stock=0 and no 4473 fields of its
		own; the gun is handed over later on a POS Invoice that IS gated."""
		doc = self._make_doc(("FFL-TEST-GUN", ""))
		doc.doctype = "Sales Invoice"
		doc.ffl_pickup_in_store = 1
		doc.update_stock = 0
		require_4473_and_nics_for_firearm_sale(doc)  # no raise

	def test_counter_pickup_marker_alone_never_lets_stock_leave(self):
		"""The invariant the bypass states: the marker on a STOCK-MOVING invoice
		is not a pass. Dropping the update_stock half would let a firearm leave
		stock with no 4473 at all."""
		doc = self._make_doc(("FFL-TEST-GUN", "S-PU-1"))
		doc.doctype = "Sales Invoice"
		doc.ffl_pickup_in_store = 1
		doc.update_stock = 1
		with self.assertRaises(frappe.ValidationError):
			require_4473_and_nics_for_firearm_sale(doc)

	def test_regulated_firearm_predicate_excludes_antique(self):
		# The compliance predicate excludes antiques; the inventory predicate does not.
		self.assertFalse(is_regulated_firearm_item("FFL-TEST-ANTIQUE"))
		self.assertTrue(is_regulated_firearm_item("FFL-TEST-GUN"))
		self.assertFalse(is_regulated_firearm_item("FFL-TEST-ACC"))
		# Antique is still a firearm for inventory / serial tracking.
		self.assertTrue(is_firearm_item("FFL-TEST-ANTIQUE"))
		self.assertTrue(is_antique_item("FFL-TEST-ANTIQUE"))
		self.assertFalse(is_antique_item("FFL-TEST-GUN"))


class RequireDispositionCoverageSplitsMultiSerial(FrappeTestCase):
	"""_require_disposition_coverage must treat a newline-joined serial_no as
	MULTIPLE serials (a past incident): build `needed` per split
	serial so each gun's own per-serial disposition satisfies the gate. Reading
	the whole "J4940\\n7805C" string as one serial made the gate query for a
	serial that never exists — it would block a fully-covered two-gun sale."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		g = FourFiveSevenThreeAndNicsGating
		g._ensure_item_group("All Item Groups", is_group=1, parent=None)
		g._ensure_item_group("Firearm", is_group=0, parent="All Item Groups")
		g._ensure_uom("Nos")
		g._ensure_item("FFL-TEST-GUN", item_group="Firearm", has_serial_no=1)

	@staticmethod
	def _pos_doc(serial_no):
		return SimpleNamespace(
			name="POSINV-MS", doctype="POS Invoice",
			items=[SimpleNamespace(idx=1, item_code="FFL-TEST-GUN",
				serial_no=serial_no, qty=2)],
		)

	def test_coverage_queried_with_split_serials(self):
		from ffl_core.compliance import firearm as compliance
		doc = self._pos_doc("J4940\n7805C")
		with patch.object(compliance, "covered_serials_for_invoice",
				return_value={"J4940", "7805C"}) as cov:
			compliance._require_disposition_coverage(doc)  # both covered → must not raise
		# The gate must ask about BOTH split serials, never the raw joined string.
		called = cov.call_args.args[1]
		self.assertEqual(set(called), {"J4940", "7805C"})

	def test_missing_one_split_serial_blocks_and_names_it(self):
		from ffl_core.compliance import firearm as compliance
		doc = self._pos_doc("J4940\n7805C")
		# Only J4940 covered → 7805C uncovered → must raise, naming the real serial.
		with patch.object(compliance, "covered_serials_for_invoice",
				return_value={"J4940"}):
			with self.assertRaises(frappe.ValidationError) as ctx:
				compliance._require_disposition_coverage(doc)
		msg = str(ctx.exception)
		self.assertIn("7805C", msg)
		self.assertNotIn("J4940\n7805C", msg)  # the raw joined string is never a "serial"


class OneFirearmPerLine(FrappeTestCase):
	"""Safeguard: a regulated-firearm POS line ringing >1 gun (qty>1 OR multiple
	newline-joined serials) is rejected at validate, so the 4473 pipeline's
	one-gun-per-line invariant holds at the source (a past incident:
	2× CZ 75 on one line, serial_no "J4940\\n7805C")."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		g = FourFiveSevenThreeAndNicsGating
		g._ensure_item_group("All Item Groups", is_group=1, parent=None)
		g._ensure_item_group("Firearm", is_group=0, parent="All Item Groups")
		g._ensure_item_group("Accessory", is_group=0, parent="All Item Groups")
		g._ensure_uom("Nos")
		g._ensure_item("FFL-TEST-GUN", item_group="Firearm", has_serial_no=1)
		g._ensure_item("FFL-TEST-ACC", item_group="Accessory", has_serial_no=0)

	@staticmethod
	def _doc(*rows):
		"""rows: (item_code, serial_no, qty)."""
		return SimpleNamespace(
			doctype="POS Invoice",
			items=[SimpleNamespace(idx=i + 1, item_code=c, serial_no=s, qty=q)
				for i, (c, s, q) in enumerate(rows)],
		)

	def test_blocks_two_firearms_on_one_line_by_qty(self):
		from ffl_core.compliance.firearm import require_one_firearm_per_line
		with self.assertRaises(frappe.ValidationError) as ctx:
			require_one_firearm_per_line(self._doc(("FFL-TEST-GUN", "", 2)))
		self.assertIn("line", str(ctx.exception).lower())

	def test_blocks_two_serials_on_one_line(self):
		from ffl_core.compliance.firearm import require_one_firearm_per_line
		with self.assertRaises(frappe.ValidationError):
			require_one_firearm_per_line(self._doc(("FFL-TEST-GUN", "J4940\n7805C", 1)))

	def test_allows_single_firearm_per_line(self):
		from ffl_core.compliance.firearm import require_one_firearm_per_line
		require_one_firearm_per_line(self._doc(("FFL-TEST-GUN", "J4940", 1)))  # must not raise

	def test_allows_two_single_qty_firearm_lines(self):
		from ffl_core.compliance.firearm import require_one_firearm_per_line
		require_one_firearm_per_line(
			self._doc(("FFL-TEST-GUN", "J4940", 1), ("FFL-TEST-GUN", "7805C", 1)))  # must not raise

	def test_allows_qty_two_non_firearm_line(self):
		from ffl_core.compliance.firearm import require_one_firearm_per_line
		# Accessories / ammo stack freely — the invariant is firearm-only.
		require_one_firearm_per_line(self._doc(("FFL-TEST-ACC", "", 2)))  # must not raise

	def test_empty_cart_is_fine(self):
		from ffl_core.compliance.firearm import require_one_firearm_per_line
		require_one_firearm_per_line(SimpleNamespace(doctype="POS Invoice", items=[]))  # must not raise


if __name__ == "__main__":
	unittest.main()
