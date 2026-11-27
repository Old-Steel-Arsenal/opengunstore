"""Return in transit: a shipped firearm the carrier brought back.

The action undoes the shipment as if the gun never left: the FFL Disposition is
cancelled (bound book; FastBound deletes + undisposes it through the
disposition's own on_cancel hook) and the gun comes back on a Material Receipt
that mirrors the original Material Issue: same warehouse, same value, same
offset (difference) account, remarks naming the disposition. The issue itself is
NOT cancelled, so the shipping month's ledger and GL stay as filed; the receipt
lands today and carries the "FFL Disposition" text the COGS summary keys on.

Real DB, real controllers. The only stub is frappe.enqueue around the cancel, to
see the FastBound delete job without running it against a vendor.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_core.api import manual_order
from ffl_core.api import return_in_transit as rit
from ffl_core.tests.test_build_order import DEST_FFL, BuildOrderMixin

SALES_USER = "rit-sales-user@example.test"


def _uid() -> str:
	return frappe.generate_hash(length=6).upper()


class ReturnInTransitFixtures(BuildOrderMixin, FrappeTestCase):

	def _manual_transfer(self, serial: str, disposition_type: str = "FFL Transfer") -> "frappe.Document":
		"""A Desk-made transfer disposition: its own on_submit Material-Issues the gun."""
		disp = frappe.get_doc({
			"doctype": "FFL Disposition",
			"disposition_date": frappe.utils.now_datetime(),
			"disposition_type": disposition_type,
			"company": self.company,
			"destination_ffl": DEST_FFL,
			"item": "BO-GUN",
			"serial": serial,
		})
		disp.flags.ignore_permissions = True
		disp.insert()
		disp.submit()
		disp.reload()
		return disp

	def _issue_row(self, se_name: str):
		return frappe.db.get_value("Stock Entry Detail", {"parent": se_name},
			["s_warehouse", "expense_account", "cost_center"], as_dict=True)

	def _stock_value(self, se_name: str) -> float:
		return flt(frappe.db.get_value("Stock Ledger Entry",
			{"voucher_no": se_name, "is_cancelled": 0}, "stock_value_difference"))

	def _gl(self, se_name: str, account: str) -> tuple[float, float]:
		row = frappe.db.sql("""SELECT COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0)
			FROM `tabGL Entry` WHERE voucher_no = %s AND account = %s AND is_cancelled = 0""",
			(se_name, account))[0]
		return flt(row[0]), flt(row[1])

	def _run(self, name: str) -> dict:
		with patch("frappe.enqueue") as enq:
			out = rit.return_in_transit(name)
		self.enqueued = enq
		return out


class TestManualTransferComesBack(ReturnInTransitFixtures):

	def test_undoes_disposition_and_receives_gun_at_issue_value_and_account(self):
		serial = self._stocked_serial(f"RIT-M-{_uid()}")
		disp = self._manual_transfer(serial)
		issue = disp.stock_entry
		self.assertTrue(issue)
		self.assertNotEqual(frappe.db.get_value("Serial No", serial, "status"), "Active")
		issued = self._issue_row(issue)

		out = self._run(disp.name)

		self.assertTrue(out["ok"])
		# Bound book: the disposition is cancelled.
		self.assertEqual(frappe.db.get_value("FFL Disposition", disp.name, "docstatus"), 2)
		# The shipping month's issue stands; the gun comes back on its own receipt.
		self.assertEqual(frappe.db.get_value("Stock Entry", issue, "docstatus"), 1)
		receipt = out["stock_entry"]
		self.assertEqual(frappe.db.get_value("FFL Disposition", disp.name,
			["stock_entry", "return_stock_entry"]), (issue, receipt))
		se = frappe.get_doc("Stock Entry", receipt)
		self.assertEqual((se.docstatus, se.stock_entry_type), (1, "Material Receipt"))
		self.assertIn(f"FFL Disposition {disp.name}", se.remarks)
		self.assertIn(issue, se.remarks)
		row = se.items[0]
		self.assertEqual((row.t_warehouse, row.expense_account, row.cost_center),
			(issued.s_warehouse, issued.expense_account, issued.cost_center))
		# Same value back in as went out.
		self.assertAlmostEqual(self._stock_value(receipt), -self._stock_value(issue), places=2)
		# Same offset account, the opposite side.
		issue_dr, _ = self._gl(issue, issued.expense_account)
		_, receipt_cr = self._gl(receipt, issued.expense_account)
		self.assertGreater(issue_dr, 0)
		self.assertAlmostEqual(receipt_cr, issue_dr, places=2)
		# The gun is on the shelf it left from.
		self.assertEqual(frappe.db.get_value("Serial No", serial, ["status", "warehouse"]),
			("Active", issued.s_warehouse))
		# An audit line on the disposition: who, and which receipt.
		self.assertTrue(frappe.db.exists("Comment", {"reference_doctype": "FFL Disposition",
			"reference_name": disp.name, "content": ("like", f"%{receipt}%")}))

	def test_return_type_comes_back_too(self):
		serial = self._stocked_serial(f"RIT-R-{_uid()}")
		disp = self._manual_transfer(serial, disposition_type="Return")
		out = self._run(disp.name)
		self.assertEqual(frappe.db.get_value("Serial No", serial, "status"), "Active")
		self.assertIsNone(out["sale"])

	def test_fastbound_record_is_deleted_and_undisposed(self):
		"""A pushed disposition: its cancel queues FastBound's delete (which also
		undisposes the item — test_fastbound_disposition_delete pins that half)."""
		serial = self._stocked_serial(f"RIT-F-{_uid()}")
		disp = self._manual_transfer(serial)
		frappe.db.set_value("FFL Disposition", disp.name, "fastbound_id", "fb-rit-1",
			update_modified=False)
		out = self._run(disp.name)
		jobs = [c for c in self.enqueued.call_args_list
			if c.args and c.args[0].endswith("delete_disposition_from_fastbound")]
		self.assertEqual(len(jobs), 1)
		self.assertEqual(jobs[0].kwargs["fastbound_id"], "fb-rit-1")
		self.assertEqual(jobs[0].kwargs["disposition_name"], disp.name)
		self.assertEqual(out["fastbound"], "undispose_queued")

	def test_fastbound_switched_off_says_so(self):
		serial = self._stocked_serial(f"RIT-D-{_uid()}")
		disp = self._manual_transfer(serial)
		frappe.db.set_value("FFL Disposition", disp.name, "fastbound_id", "fb-rit-2",
			update_modified=False)
		frappe.db.set_single_value("FastBound Settings", "enabled", 0)
		try:
			out = self._run(disp.name)
		finally:
			frappe.db.set_single_value("FastBound Settings", "enabled", 1)
		self.assertEqual(out["fastbound"], "disabled")
		self.assertFalse([c for c in self.enqueued.call_args_list
			if c.args and c.args[0].endswith("delete_disposition_from_fastbound")])

	def test_comment_escapes_what_it_quotes(self):
		serial = self._stocked_serial(f"RIT-<b>{_uid()}")
		disp = self._manual_transfer(serial)
		self._run(disp.name)
		content = frappe.db.get_value("Comment", {"reference_doctype": "FFL Disposition",
			"reference_name": disp.name, "comment_type": "Comment"}, "content")
		self.assertNotIn("<b>", content)
		self.assertIn("&lt;b&gt;", content)

	def test_plain_cancel_still_cancels_the_issue(self):
		"""The ordinary Desk cancel is unchanged: no return receipt, so the
		disposition's own on_cancel cancels its Material Issue."""
		serial = self._stocked_serial(f"RIT-C-{_uid()}")
		disp = self._manual_transfer(serial)
		issue = disp.stock_entry
		with patch("frappe.enqueue"):
			disp.cancel()
		self.assertEqual(frappe.db.get_value("Stock Entry", issue, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Serial No", serial, "status"), "Active")


class TestCounterOrderComesBack(ReturnInTransitFixtures):

	def test_order_goes_back_on_the_queue_holding_the_gun(self):
		serial = self._stocked_serial(f"RIT-O-{_uid()}")
		si = self._ship(serial, update_stock=0, buyer_type="Business")
		manual_order.dispose_order(si.name)
		disp = frappe.get_all("FFL Disposition",
			filters={"sales_invoice": si.name, "docstatus": 1}, pluck="name")[0]
		# It really shipped: a ShipStation order, then marked shipped.
		frappe.db.set_value("Sales Invoice", si.name, "shipstation_order_id", "se-rit-1",
			update_modified=False)
		self.assertTrue(manual_order.mark_shipped_manually(si.name).get("already"))

		out = self._run(disp)

		self.assertEqual(frappe.db.get_value("Serial No", serial, "status"), "Active")
		# The sale is untouched and says it still owes this gun's shipment.
		self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "docstatus"), 1)
		from ffl_core.reservations import counter_holds, si_pending_firearm_serials
		self.assertEqual([p["serial"] for p in si_pending_firearm_serials(
			frappe.get_doc("Sales Invoice", si.name))], [serial])
		self.assertIn(serial, counter_holds("BO-GUN"))
		self.assertEqual(out["sale"], {"doctype": "Sales Invoice", "name": si.name})
		self.assertIn(si.name, out["next_step"])
		# Not shipped any more: the stamp is cleared, the old shipment named on the invoice.
		self.assertEqual(frappe.db.get_value("Sales Invoice", si.name,
			["shipstation_order_id", "shipstation_status"]), ("", "Not Requested"))
		self.assertTrue(frappe.db.exists("Comment", {"reference_doctype": "Sales Invoice",
			"reference_name": si.name, "content": ("like", "%se-rit-1%")}))
		# ...and Dispose + Mark Shipped can ship it again.
		with patch("frappe.enqueue"):
			again = manual_order.dispose_order(si.name)
		self.assertEqual(again["disposed_count"], 1)
		shipped = manual_order.mark_shipped_manually(si.name)
		self.assertTrue(shipped["ok"])
		self.assertFalse(shipped.get("already"))
		self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "shipstation_status"),
			"Shipped")

	def test_unshipped_order_keeps_its_stamp(self):
		"""Disposed but never pushed or marked: nothing to reset, no comment."""
		serial = self._stocked_serial(f"RIT-U-{_uid()}")
		si = self._ship(serial, update_stock=0, buyer_type="Business")
		manual_order.dispose_order(si.name)
		disp = frappe.get_all("FFL Disposition",
			filters={"sales_invoice": si.name, "docstatus": 1}, pluck="name")[0]
		self._run(disp)
		self.assertFalse(frappe.db.exists("Comment", {"reference_doctype": "Sales Invoice",
			"reference_name": si.name, "content": ("like", "%returned in transit%")}))


class TestRefusals(ReturnInTransitFixtures):

	def _refused(self, name: str, *fragments: str, exc=frappe.ValidationError) -> None:
		with self.assertRaises(exc) as ctx:
			self._run(name)
		for fragment in fragments:
			self.assertIn(fragment, str(ctx.exception))

	def test_counter_role_is_refused(self):
		serial = self._stocked_serial(f"RIT-P-{_uid()}")
		disp = self._manual_transfer(serial)
		if not frappe.db.exists("User", SALES_USER):
			user = frappe.get_doc({"doctype": "User", "email": SALES_USER,
				"first_name": "RIT", "send_welcome_email": 0,
				"roles": [{"role": "Sales User"}]})
			user.flags.ignore_permissions = True
			user.insert()
		frappe.set_user(SALES_USER)
		try:
			self._refused(disp.name, exc=frappe.PermissionError)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("FFL Disposition", disp.name, "docstatus"), 1)

	def test_cancelled_disposition_is_refused(self):
		serial = self._stocked_serial(f"RIT-X-{_uid()}")
		disp = self._manual_transfer(serial)
		self._run(disp.name)
		self._refused(disp.name, "not a submitted")

	def test_destruction_is_refused(self):
		serial = self._stocked_serial(f"RIT-S-{_uid()}")
		disp = frappe.get_doc({
			"doctype": "FFL Disposition", "disposition_date": frappe.utils.now_datetime(),
			"disposition_type": "Scrap", "company": self.company,
			"item": "BO-GUN", "serial": serial,
			"destroyed_witness1": "A", "destroyed_witness2": "B",
		})
		disp.flags.ignore_permissions = True
		disp.insert()
		with patch("frappe.enqueue"):
			disp.submit()
		self._refused(disp.name, "Scrap")

	def test_stock_that_left_on_the_invoice_needs_a_credit_note(self):
		"""Ship-now order: the invoice moved the gun, the disposition issued
		nothing — bringing it back is a return of the sale, not ours to book."""
		serial = self._stocked_serial(f"RIT-I-{_uid()}")
		si = self._ship(serial, update_stock=1)
		disp = frappe.get_all("FFL Disposition",
			filters={"sales_invoice": si.name, "docstatus": 1}, pluck="name")[0]
		self._refused(disp, si.name, "credit note")
		self.assertEqual(frappe.db.get_value("FFL Disposition", disp, "docstatus"), 1)

	def test_gun_already_back_in_stock_is_refused(self):
		"""A manual receipt (the old workaround) already brought it back."""
		serial = self._stocked_serial(f"RIT-B-{_uid()}")
		disp = self._manual_transfer(serial)
		self._stocked_serial(serial)  # Material Receipt by hand
		self._refused(disp.name, serial, "already back in stock")
		self.assertEqual(frappe.db.get_value("FFL Disposition", disp.name, "docstatus"), 1)

	def test_later_bound_book_entry_is_refused(self):
		"""The gun was booked again after this disposition (received back from the
		dealer as an acquisition, or sold on): undoing this one would rewrite a
		history the book has moved past."""
		serial = self._stocked_serial(f"RIT-L-{_uid()}")
		disp = self._manual_transfer(serial)
		later = frappe.get_doc({
			"doctype": "FFL Acquisition", "acquisition_date": frappe.utils.now_datetime(),
			"source_type": "FFL Dealer", "company": self.company, "source_name": "Test Dealer",
			"source_ffl": DEST_FFL, "item": "BO-GUN", "serial": serial,
		})
		later.db_insert()
		frappe.db.set_value("FFL Acquisition", later.name, {"docstatus": 1,
			"creation": frappe.utils.add_to_date(disp.creation, seconds=5)}, update_modified=False)
		self._refused(disp.name, later.name)

	def test_web_order_is_refused(self):
		serial = self._stocked_serial(f"RIT-W-{_uid()}")
		disp = self._manual_transfer(serial)
		if not frappe.db.has_column("FFL Disposition", "woo_online_order"):
			self.skipTest("ffl_woo_sync not installed")
		frappe.db.set_value("FFL Disposition", disp.name, "woo_online_order", "WOO-RIT-1",
			update_modified=False)
		self._refused(disp.name, "WOO-RIT-1")
		self.assertEqual(frappe.db.get_value("FFL Disposition", disp.name, "docstatus"), 1)


class TestFormButtonMatchesServer(FrappeTestCase):

	def test_button_types_and_roles_match_the_endpoint(self):
		"""The JS only decides whether the button is drawn; a drifted copy draws it
		for people the server refuses, or hides it from people it accepts."""
		import re
		from pathlib import Path

		from ffl_core.roles import RETURN_IN_TRANSIT_ROLES

		js = Path(frappe.get_app_path("ffl_core", "ffl_core", "doctype",
			"ffl_disposition", "ffl_disposition.js")).read_text()
		types = re.search(r"FFL_DISPOSITION_DESTINATION_TYPES = new Set\(\[(.*?)\]\)", js, re.S)
		roles = re.search(r"FFL_DISPOSITION_RETURN_ROLES = \[(.*?)\]", js, re.S)
		self.assertIsNotNone(types)
		self.assertIsNotNone(roles)
		self.assertEqual(set(re.findall(r"'([^']+)'", types.group(1))), set(rit.SHIPPED_TYPES))
		self.assertEqual(set(re.findall(r"'([^']+)'", roles.group(1))), set(RETURN_IN_TRANSIT_ROLES))
		self.assertIn("ffl_core.api.return_in_transit.return_in_transit", js)
