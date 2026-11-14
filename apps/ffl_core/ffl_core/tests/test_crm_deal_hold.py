"""A gun held for a CRM deal (ffl_core.crm.holds): placing, extending and releasing a hold, who may,
the counter's gate and manager override, Create Order, the pickers, the deal's own events, the
sweep, and the hold as a counter hold every channel already reads. Real documents through the real
endpoints and hooks; FastBound is off. ``ffl_core.utils.commit`` is a no-op under tests, so the
"commit before the locks" step leaves the test's savepoint alone. The two-session race at the end
commits on purpose and deletes what it made."""
from __future__ import annotations

import threading
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, now_datetime

from ffl_core import staff_switch
from ffl_core.api import crm as api
from ffl_core.api import manual_order
from ffl_core.crm import holds
from ffl_core.crm.item import upsert_product
from ffl_core.reservations import counter_holds
from ffl_core.tests.crm_support import CLERK, MANAGER, CRMTestCase, user
from ffl_core.tests.deal_hold_support import (
	GUN, OTHER, OWNER, PIN, DealHoldCase, _active, _comments, _contact, _customer, _switches, _tasks, _token,
)
from ffl_core.tests.test_build_order import DEST_FFL
from ffl_core.tests.test_extension_hooks import hooks

HERE = __name__
CHANGED: list[str] = []


def record_change(serial_no):
	CHANGED.append(serial_no)


def refuse_everything(serial_no):
	return f"{serial_no} is busy elsewhere."


def broken_refusal(serial_no):
	raise RuntimeError("channel down")


def broken_change(serial_no):
	raise RuntimeError("push failed")


class _Queued:
	"""Inside: the hooks queue a sweep for after the commit and write nothing (``_rollback`` is never
	called, the holds stay Active). ``run()`` then does what the queued job does after the commit."""

	def __init__(self, case):
		self.case = case

	def __enter__(self):
		self._enqueue = patch("frappe.enqueue")
		self._rollback = patch.object(holds, "_rollback", side_effect=AssertionError("rolled back in a hook"))
		self.enqueue = self._enqueue.start()
		self._rollback.start()
		return self

	def __exit__(self, *exc):
		self._rollback.stop()
		self._enqueue.stop()

	def names(self) -> list[str]:
		calls = [c for c in self.enqueue.call_args_list if c.args and c.args[0] == holds.SWEEP_JOB]
		for c in calls:
			self.case.assertTrue(c.kwargs.get("enqueue_after_commit"))
		return sorted({n for c in calls for n in c.kwargs["holds"]})

	def run(self) -> dict:
		return holds.sweep(holds=self.names())


class TestPlacing(DealHoldCase):
	def test_a_hold_is_a_live_counter_hold_for_48_hours(self):
		serial = self._gun()
		deal = self._deal(serial)
		before = now_datetime()
		name = self._held(deal, serial)
		row = frappe.db.get_value(holds.HOLD, name, ["status", "deal", "held_by", "expires_at", "item_code",
			"active_serial", "extensions"], as_dict=True)
		self.assertEqual((row.status, row.deal, row.held_by, row.item_code, row.active_serial, row.extensions),
			(holds.ACTIVE, deal, CLERK, GUN, serial, 0))
		hours = (row.expires_at - before).total_seconds() / 3600
		self.assertTrue(47.99 < hours <= 48.01, hours)
		entry = counter_holds(GUN)[serial]
		self.assertEqual((entry["kind"], entry["ref"], entry["customer"]), ("crm_deal", deal, self._customer_of(deal)))
		self.assertEqual(entry["url"], f"/crm/deals/{deal}")
		self.assertTrue(any(f"Held {serial} until" in c for c in _comments(deal)), _comments(deal))

	def test_one_gun_one_deal_and_holding_again_is_the_same_hold(self):
		serial = self._gun()
		first, second = self._deal(serial), self._deal(serial)
		name = self._held(first, serial)
		again = self._hold(first, serial)
		self.assertEqual(again["held"][0]["name"], name, "same deal: the hold it already has")
		out = self._hold(second, serial)
		self.assertEqual(out["held"], [])
		self.assertIn(f"{serial} is already held for deal {first} until", out["refused"][0]["reason"])
		self.assertEqual(_active(serial), [name])

	def test_the_unique_index_stops_a_second_active_hold(self):
		serial = self._gun()
		deal = self._deal(serial)
		self._held(deal, serial)
		with self.assertRaises((frappe.UniqueValidationError, frappe.DuplicateEntryError)):
			frappe.get_doc({"doctype": holds.HOLD, "serial_no": serial, "item_code": GUN, "deal": self._deal(serial),
				"status": holds.ACTIVE, "expires_at": now_datetime() + timedelta(hours=1),
				"active_serial": serial}).insert(ignore_permissions=True)

	def test_each_gun_on_its_own(self):
		good, taken = self._gun(), self._gun()
		frappe.db.set_value("Serial No", taken, "woo_reserved", 1, update_modified=False)
		deal = self._deal(good, taken)
		out = self._hold(deal, good, taken)
		self.assertEqual([h["serial"] for h in out["held"]], [good])
		self.assertEqual([r["serial"] for r in out["refused"]], [taken])

	def test_what_cannot_be_held(self):
		deal_customer = _customer()

		def refused(serial, deal=None) -> str:
			out = self._hold(deal or self._deal(serial, customer=deal_customer), serial)
			self.assertEqual(out["held"], [], serial)
			return out["refused"][0]["reason"]

		web = self._gun()
		frappe.db.set_value("Serial No", web, {"woo_reserved": 1, "woo_order": "WEB-1"}, update_modified=False)
		self.assertIn("web order WEB-1", refused(web))
		custody = self._gun()
		frappe.db.set_value("Serial No", custody, "is_transfer_gun", 1, update_modified=False)
		self.assertIn("Customer's gun", refused(custody))
		consigned = self._gun()
		frappe.db.set_value("Serial No", consigned, "consigned_out", 1, update_modified=False)
		self.assertIn("Out on consignment", refused(consigned))
		parked = self._gun()
		with hooks("gunshop_counter_holds", [f"{HERE}.fake_parked"]):
			PARKED.add(parked)
			self.assertIn("In a parked POS sale", refused(parked))
		tagged = self._gun()
		frappe.db.set_value("Serial No", tagged, "ledger_serial", "NSN", update_modified=False)
		self.assertIn("can't be sold yet", refused(tagged))
		sold = self._gun()
		self._invoice(sold, _customer(), submit=True)
		self.assertIn("On Sales Invoice", refused(sold))

		loose = self._gun()
		self.assertEqual(refused(loose, deal=self._deal(self._gun(), customer=deal_customer)),
			f"{loose} is not on this deal.")

		frappe.db.set_value("Item", GUN, "uniform_sell_price", 1, update_modified=False)
		frappe.clear_document_cache("Item", GUN)
		frappe.cache.delete_value("item_cache")
		try:
			gun = self._gun()
			with patch("ffl_core.reservations.is_uniform_firearm_item", side_effect=lambda code: code == GUN):
				self.assertIn("sold as one product for all its guns", refused(gun))
		finally:
			frappe.db.set_value("Item", GUN, "uniform_sell_price", 0, update_modified=False)

		nobody = self._gun()
		with self.assertRaisesRegex(frappe.ValidationError, "Pick the customer first"):
			self._hold(self._deal(nobody, customer=False), nobody)

		off = self._gun()
		_switches(hold=0)
		with self.assertRaisesRegex(frappe.ValidationError, "Holding guns for deals is off"):
			self._hold(self._deal(off), off)

	def test_a_gunbroker_style_refusal_and_a_failing_channel_both_refuse(self):
		serial = self._gun()
		deal = self._deal(serial)
		with hooks(holds.HOOK_REFUSAL, [f"{HERE}.refuse_everything"]):
			self.assertEqual(self._hold(deal, serial)["refused"][0]["reason"], f"{serial} is busy elsewhere.")
		with hooks(holds.HOOK_REFUSAL, [f"{HERE}.broken_refusal"]):
			self.assertIn("Couldn't check", self._hold(deal, serial)["refused"][0]["reason"])
		self.assertEqual(_active(serial), [])

	def test_a_failing_change_listener_does_not_undo_the_hold(self):
		serial = self._gun()
		deal = self._deal(serial)
		with hooks(holds.HOOK_CHANGED, [f"{HERE}.broken_change"]):
			name = self._held(deal, serial)
		self.assertEqual(_active(serial), [name])

	def test_the_channels_hear_of_each_change(self):
		serial = self._gun()
		deal = self._deal(serial)
		CHANGED.clear()
		with hooks(holds.HOOK_CHANGED, [f"{HERE}.record_change"]):
			name = self._held(deal, serial)
			frappe.set_user(CLERK)
			api.release_hold(name)
			frappe.set_user("Administrator")
		self.assertEqual(CHANGED, [serial, serial])

	def test_crm_roles_only(self):
		serial = self._gun()
		deal = self._deal(serial)
		user("dh-stock@example.com", "Stock Manager")
		with self.assertRaises(frappe.PermissionError):
			self._hold(deal, serial, as_user="dh-stock@example.com")


PARKED: set[str] = set()


def fake_parked(item_code):
	return [{"serial": s, "source": "pos", "ref": "POS-DRAFT-1", "kind": "pos",
		"tooltip": "In a parked POS sale (POS-DRAFT-1)", "url": ""} for s in sorted(PARKED)]


class TestTime(DealHoldCase):
	def test_expiry_is_read_at_the_time(self):
		serial = self._gun()
		name = self._held(self._deal(serial), serial)
		at = now_datetime()
		with patch("ffl_core.crm.holds.now_datetime", return_value=at):
			for delta, live in ((-1, False), (0, False), (1, True)):
				frappe.db.set_value(holds.HOLD, name, "expires_at", at + timedelta(seconds=delta), update_modified=False)
				self.assertEqual(serial in counter_holds(GUN), live, delta)

	def test_the_sweep_writes_an_expired_hold(self):
		serial = self._gun()
		deal = self._deal(serial)
		name = self._held(deal, serial)
		frappe.db.set_value(holds.HOLD, name, "expires_at", now_datetime() - timedelta(minutes=1), update_modified=False)
		CHANGED.clear()
		with hooks(holds.HOOK_CHANGED, [f"{HERE}.record_change"]):
			self.assertEqual(holds.sweep().get(holds.END_EXPIRED), 1)
		row = frappe.db.get_value(holds.HOLD, name, ["status", "end_reason", "active_serial", "ended_by"], as_dict=True)
		self.assertEqual((row.status, row.end_reason, row.active_serial, row.ended_by),
			(holds.EXPIRED, holds.END_EXPIRED, f"ended:{name}", "Administrator"))
		self.assertIn(f"Hold on {serial} expired.", _comments(deal))
		self.assertEqual(CHANGED, [serial])
		self.assertEqual(_tasks(deal), [], "Q3: an expiry makes no task")
		self.assertEqual(holds.sweep(), {}, "nothing left to do")

	def test_the_sweep_leaves_a_live_hold(self):
		serial = self._gun()
		name = self._held(self._deal(serial), serial)
		holds.sweep()
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, "status"), holds.ACTIVE)

	def test_holding_again_after_expiry_takes_a_manager(self):
		serial = self._gun()
		deal = self._deal(serial)
		name = self._held(deal, serial)
		frappe.db.set_value(holds.HOLD, name, "expires_at", now_datetime() - timedelta(minutes=1), update_modified=False)
		out = self._hold(deal, serial)
		self.assertTrue(out["refused"][0].get("needs_manager"), out)
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, "status"), holds.EXPIRED, "ended as it was found")
		again = self._hold(deal, serial, approver=MANAGER, pin=PIN)
		self.assertEqual(len(again["held"]), 1, again)
		self.assertEqual(frappe.db.get_value(holds.HOLD, again["held"][0]["name"], "approved_by"), MANAGER)

	def test_another_deal_may_hold_a_gun_whose_hold_ran_out(self):
		serial = self._gun()
		name = self._held(self._deal(serial), serial)
		frappe.db.set_value(holds.HOLD, name, "expires_at", now_datetime() - timedelta(minutes=1), update_modified=False)
		other = self._deal(serial)
		self.assertEqual(len(self._hold(other, serial)["held"]), 1)


class TestExtendRelease(DealHoldCase):
	def test_the_limit_is_checked_again_under_the_lock(self):
		"""Two Extend clicks at once both read "not extended yet" before the lock. The second, in
		turn under the lock, finds the first's extension and needs a manager."""
		serial = self._gun()
		name = self._held(self._deal(serial), serial)
		stale = holds._brief(name)
		self._extend(name)
		with patch.object(holds, "_brief", return_value=stale):
			with self.assertRaisesRegex(frappe.ValidationError, "A manager has to extend it again"):
				self._extend(name)
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, "extensions"), 1)

	def _extend(self, name: str, as_user: str = CLERK, **kwargs) -> dict:
		frappe.set_user(as_user)
		try:
			return api.extend_hold(name, **kwargs)
		finally:
			frappe.set_user("Administrator")

	def _release(self, name: str, as_user: str = CLERK, **kwargs) -> dict:
		frappe.set_user(as_user)
		try:
			return api.release_hold(name, **kwargs)
		finally:
			frappe.set_user("Administrator")

	def test_the_holder_extends_once_then_a_manager(self):
		serial = self._gun()
		deal = self._deal(serial)
		name = self._held(deal, serial)
		first = frappe.db.get_value(holds.HOLD, name, "expires_at")
		out = self._extend(name)
		self.assertEqual(out["extensions"], 1)
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, "expires_at"), first + timedelta(hours=48))
		self.assertTrue(any(f"Hold on {serial} extended to" in c for c in _comments(deal)))
		with self.assertRaisesRegex(frappe.ValidationError, "A manager has to extend it again"):
			self._extend(name)
		self.assertEqual(self._extend(name, approver=MANAGER, pin=PIN)["extensions"], 2)
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, "approved_by"), MANAGER)
		self.assertEqual(self._extend(name, as_user=MANAGER)["extensions"], 3, "a manager signed in approves their own")

	def test_someone_else_needs_a_manager_even_the_first_time(self):
		serial = self._gun()
		name = self._held(self._deal(serial), serial)
		with self.assertRaisesRegex(frappe.ValidationError, "placed this hold"):
			self._extend(name, as_user=OTHER)
		self.assertEqual(self._extend(name, as_user=OTHER, approver=MANAGER, pin=PIN)["extensions"], 1)

	def test_who_may_release(self):
		for who in (CLERK, OWNER, MANAGER):
			serial = self._gun()
			name = self._held(self._deal(serial), serial)
			self.assertEqual(self._release(name, as_user=who)["end_reason"], holds.END_RELEASED, who)
			self.assertEqual(_active(serial), [])
		serial = self._gun()
		deal = self._deal(serial)
		name = self._held(deal, serial)
		with self.assertRaisesRegex(frappe.ValidationError, "the deal owner or a manager can release"):
			self._release(name, as_user=OTHER)
		with self.assertRaises(staff_switch.WrongPassword):
			self._release(name, as_user=OTHER, approver=MANAGER, pin="9999")
		self.assertEqual(frappe.cache.get(staff_switch._pin_fail_key(MANAGER)), b"1", "the wrong PIN counts")
		out = self._release(name, as_user=OTHER, approver=MANAGER, pin=PIN, reason="Customer called")
		self.assertEqual((out["status"], out["end_reason"]), (holds.RELEASED, holds.END_RELEASED))
		row = frappe.db.get_value(holds.HOLD, name, ["approved_by", "note", "ended_by"], as_dict=True)
		self.assertEqual((row.approved_by, row.note, row.ended_by), (MANAGER, "Customer called", OTHER))
		self.assertTrue(any(f"Hold on {serial} released by" in c for c in _comments(deal)))
		with self.assertRaisesRegex(frappe.ValidationError, "This hold has ended"):
			self._release(name)

	def test_a_released_gun_can_be_held_again_by_anyone(self):
		serial = self._gun()
		name = self._held(self._deal(serial), serial)
		self._release(name)
		self.assertEqual(len(self._hold(self._deal(serial), serial)["held"]), 1)


class TestCounter(DealHoldCase):
	def test_the_sale_gate_lets_the_deal_customer_through(self):
		serial = self._gun()
		deal = self._deal(serial)
		self._held(deal, serial)
		mine = self._invoice(serial, self._customer_of(deal))
		self.assertTrue(mine.name)
		with self.assertRaises(holds.DealHoldError) as caught:
			self._invoice(serial, _customer())
		self.assertIn("is held for", str(caught.exception))
		self.assertIn(f"(deal {deal})", str(caught.exception))
		self.assertIn("A manager can release it", str(caught.exception))

	def test_the_gate_on_a_pos_invoice_and_what_passes(self):
		serial = self._gun()
		deal = self._deal(serial)
		self._held(deal, serial)
		for event in ("POS Invoice", "Sales Invoice"):
			validate = frappe.get_hooks("doc_events")[event]["validate"]
			self.assertIn("ffl_core.crm.holds.refuse_deal_held_serials", validate, event)
		pos = frappe.new_doc("POS Invoice")
		pos.customer = _customer()
		pos.append("items", {"item_code": GUN, "qty": 1, "rate": 700, "serial_no": serial})
		with self.assertRaises(holds.DealHoldError):
			holds.refuse_deal_held_serials(pos)
		pos.customer = self._customer_of(deal)
		holds.refuse_deal_held_serials(pos)
		pos.customer = _customer()
		for flag in ({"is_return": 1}, {"is_consolidated": 1}, {"woo_online_order": "WOO-1"}, {"gunbroker_order": "GB-1"}):
			probe = frappe.copy_doc(pos)
			probe.update(flag)
			holds.refuse_deal_held_serials(probe)

	def test_the_counter_check_names_the_deal(self):
		serial = self._gun()
		deal = self._deal(serial)
		self._held(deal, serial)
		doc = {"doctype": "Sales Invoice", "company": self.company, "customer": _customer(),
			"items": [{"item_code": GUN, "qty": 1, "rate": 700, "serial_no": serial}]}
		frappe.set_user(CLERK)
		try:
			out = api.check_sale(frappe.as_json(doc))
		finally:
			frappe.set_user("Administrator")
		self.assertEqual([b["serial"] for b in out["blocked"]], [serial])
		self.assertEqual(out["blocked"][0]["deal"], deal)
		self.assertFalse(out["self_approves"])
		self.assertIn(MANAGER, [a["user"] for a in out["approvers"]])
		doc["customer"] = self._customer_of(deal)
		self.assertEqual(api.check_sale(frappe.as_json(doc))["blocked"], [])

	def test_a_manager_override_ends_the_hold_and_tasks_the_owner(self):
		serial = self._gun()
		deal = self._deal(serial)
		name = self._held(deal, serial)
		frappe.set_user(OTHER)
		try:
			with self.assertRaisesRegex(frappe.ValidationError, "Pick the manager"):
				api.override_hold(serial, "Customer did not come back")
			out = api.override_hold(serial, "Customer did not come back", approver=MANAGER, pin=PIN)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(out["released"]["name"], name)
		row = frappe.db.get_value(holds.HOLD, name, ["status", "end_reason", "approved_by", "note"], as_dict=True)
		self.assertEqual((row.status, row.end_reason, row.approved_by, row.note),
			(holds.RELEASED, holds.END_OVERRIDE, MANAGER, "Customer did not come back"))
		tasks = _tasks(deal)
		self.assertEqual(len(tasks), 1)
		self.assertEqual((tasks[0].assigned_to, tasks[0].priority, tasks[0].title),
			(OWNER, "High", f"Hold released at the counter: {serial}"))
		self.assertTrue(self._invoice(serial, _customer()).name, "the sale saves now")

	def test_an_override_on_a_deal_without_owner_tasks_the_holder(self):
		serial = self._gun()
		deal = self._deal(serial, owner=None)
		self._held(deal, serial)
		frappe.set_user(MANAGER)
		try:
			api.override_hold(serial, "Deal owner agreed")
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(_tasks(deal)[0].assigned_to, CLERK)

	def test_the_pickers_offer_it_to_the_deal_customer_only(self):
		from ffl_core.firearm import available_serials_for_items, available_serials_with_prices

		serial = self._gun()
		deal = self._deal(serial)
		self._held(deal, serial)
		buyer = self._customer_of(deal)
		rows = available_serials_with_prices([GUN], customer=buyer)[GUN]
		mine = [r for r in rows if r["serial"] == serial]
		self.assertEqual(len(mine), 1)
		self.assertEqual(mine[0].get("held_for_customer"), 1)
		self.assertNotIn(serial, [r["serial"] for r in available_serials_with_prices([GUN], customer=_customer())[GUN]])
		self.assertNotIn(serial, [r["serial"] for r in available_serials_with_prices([GUN])[GUN]])
		self.assertIn(serial, available_serials_for_items([GUN], customer=buyer)[GUN])
		self.assertNotIn(serial, available_serials_for_items([GUN])[GUN])
		frappe.set_user(CLERK)
		try:
			self.assertEqual(api.serial_hold(serial)["hold"]["deal"], deal)
		finally:
			frappe.set_user("Administrator")


class TestCreateOrder(DealHoldCase):
	def test_serial_status_by_buyer(self):
		serial = self._gun()
		deal = self._deal(serial)
		self._held(deal, serial)
		data, reason = manual_order._serial_status(serial, probe=False, customer=self._customer_of(deal))
		self.assertIsNone(reason)
		self.assertEqual(data["serial"], serial)
		data, reason = manual_order._serial_status(serial, probe=False, customer=_customer())
		self.assertIsNone(data)
		self.assertIsInstance(reason, holds.HeldText)
		self.assertIn(f"(deal {deal})", reason)
		with self.assertRaises(holds.DealHoldError):
			manual_order.find_serial_for_order(serial)
		self.assertEqual(manual_order.find_serial_for_order(serial, customer=self._customer_of(deal))["serial"], serial)
		checked = manual_order.check_serials_available([serial], customer=_customer())
		self.assertFalse(checked[serial]["ok"])

	def test_an_order_for_the_customer_ends_the_hold(self):
		serial = self._gun()
		deal = self._deal(serial)
		name = self._held(deal, serial)
		payload = {"buyer_type": "individual", "customer": self._customer_of(deal), "pos_profile": self.pos,
			"destination_ffl": DEST_FFL, "dispose_now": 0, "payment_received": 0,
			"items": [{"item_code": GUN, "qty": 1, "rate": 700, "serials": [serial]}]}
		with self.assertRaises(holds.DealHoldError):
			manual_order.create_order({**payload, "customer": _customer()})
		with _Queued(self) as queued:
			out = manual_order.create_order(payload)
		self.assertTrue(out["ok"])
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, "status"), holds.ACTIVE, "nothing written in the sale")
		self.assertEqual(queued.names(), [name])
		self.assertEqual(queued.run(), {holds.END_SOLD: 1})
		row = frappe.db.get_value(holds.HOLD, name, ["status", "end_reason"], as_dict=True)
		self.assertEqual((row.status, row.end_reason), (holds.RELEASED, holds.END_SOLD))
		self.assertIn(f"Sold to the customer on {out['sales_invoice']}.", _comments(deal))


class TestDealEvents(DealHoldCase):
	def test_won_by_an_invoice_ends_the_other_holds(self):
		sold, other = self._gun(), self._gun()
		deal = self._deal(sold, other)
		self.assertEqual(len(self._hold(deal, sold, other)["held"]), 2)
		with _Queued(self) as queued:
			self._invoice(sold, self._customer_of(deal), submit=True, deal=deal)
		self.assertEqual(frappe.db.get_value("CRM Deal", deal, "status"), "Won")
		self.assertEqual(len(_active(sold) + _active(other)), 2, "nothing written in the sale or the deal's save")
		self.assertEqual(len(queued.names()), 2)
		queued.run()
		ends = dict(frappe.get_all(holds.HOLD, filters={"deal": deal}, fields=["serial_no", "end_reason"], as_list=True))
		self.assertEqual(ends, {sold: holds.END_SOLD, other: holds.END_WON})

	def test_lost_deleted_and_off_the_deal(self):
		serial = self._gun()
		deal = self._deal(serial)
		name = self._held(deal, serial)
		doc = frappe.get_doc("CRM Deal", deal)
		doc.status = "Lost"
		doc.lost_reason = "Other"
		doc.lost_notes = "test"
		doc.flags.ignore_mandatory = True
		with _Queued(self) as queued:
			doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, "status"), holds.ACTIVE)
		queued.run()
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, "end_reason"), holds.END_LOST)

		serial = self._gun()
		deal = self._deal(serial)
		name = self._held(deal, serial)
		with _Queued(self) as queued:
			frappe.delete_doc("CRM Deal", deal, ignore_permissions=True, force=True)
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, "status"), holds.ACTIVE)
		queued.run()
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, ["status", "end_reason"]), (holds.RELEASED, holds.END_DELETED))

		keep, drop = self._gun(), self._gun()
		deal = self._deal(keep, drop)
		self._hold(deal, keep, drop)
		doc = frappe.get_doc("CRM Deal", deal)
		doc.products = [r for r in doc.products if r.ffl_serial_no == keep]
		with _Queued(self) as queued:
			doc.save(ignore_permissions=True)
		self.assertEqual(len(queued.names()), 1)
		queued.run()
		self.assertEqual(_active(keep) and frappe.db.get_value(holds.HOLD, _active(keep)[0], "deal"), deal)
		self.assertEqual(frappe.db.get_value(holds.HOLD, {"serial_no": drop}, "end_reason"), holds.END_OFF_DEAL)

	def test_won_by_hand_ends_the_holds_after_the_save(self):
		serial = self._gun()
		deal = self._deal(serial)
		name = self._held(deal, serial)
		doc = frappe.get_doc("CRM Deal", deal)
		doc.status = "Won"
		doc.flags.ignore_mandatory = True
		with _Queued(self) as queued:
			doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, "status"), holds.ACTIVE)
		self.assertEqual(queued.run(), {holds.END_WON: 1})

	def test_a_pos_sale_to_the_customer_ends_it_as_sold(self):
		"""A POS ticket holds no gun once submitted: the sweep finds the sale itself."""
		serial = self._gun()
		deal = self._deal(serial)
		name = self._held(deal, serial)
		pos = frappe.new_doc("POS Invoice")
		pos.customer = self._customer_of(deal)
		pos.append("items", {"item_code": GUN, "qty": 1, "rate": 700, "serial_no": serial})
		pos.name, pos.docstatus, pos.is_return = "DH-POS-" + _token(), 1, 0
		with _Queued(self) as queued:
			holds.release_sold(pos)
		self.assertEqual(queued.names(), [name])
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, "status"), holds.ACTIVE)
		with patch.object(holds, "_sold_to", return_value=pos.name) as sold_to:
			self.assertEqual(queued.run(), {holds.END_SOLD: 1})
		self.assertEqual(sold_to.call_args.args[:2], (serial, self._customer_of(deal)))

	def test_a_renamed_deal_keeps_its_holds(self):
		serial = self._gun()
		deal = self._deal(serial)
		name = self._held(deal, serial)
		new = deal + "-R"
		frappe.rename_doc("CRM Deal", deal, new, force=True)
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, ["deal", "status"]), (new, holds.ACTIVE))

	def test_the_customer_card_lists_the_holds(self):
		serial = self._gun()
		deal = self._deal(serial)
		self._held(deal, serial)
		summary = api.deal_customer_summary(deal)
		self.assertEqual([h["serial"] for h in summary["holds"]], [serial])
		self.assertIn(f"Held: {serial} until", summary["html"])

	def test_the_dialog_reads_each_gun(self):
		mine, other, free = self._gun(), self._gun(), self._gun()
		deal = self._deal(mine, other, free)
		self._held(deal, mine)
		self._held(self._deal(other), other)
		frappe.set_user(CLERK)
		try:
			out = api.deal_holds(deal)
		finally:
			frappe.set_user("Administrator")
		guns = {g["serial"]: g for g in out["guns"]}
		self.assertEqual(guns[mine]["hold"]["held_by"], CLERK)
		self.assertFalse(guns[mine]["hold"]["extend_needs_manager"])
		self.assertTrue(guns[other]["blocked"].startswith("Held for another deal until"))
		self.assertIsNone(guns[free]["blocked"])
		self.assertTrue(out["enabled"])
		self.assertFalse(out["self_approves"])

	def test_the_deal_script_follows_the_switch(self):
		from ffl_core.crm import setup

		menu = 'label: __("Hold gun")'
		_switches(hold=0)
		setup.apply_pos_link()
		self.assertNotIn(menu, frappe.db.get_value("CRM Form Script", setup.DEAL_SCRIPT, "script"))
		_switches(hold=1)
		setup.apply_pos_link()
		script = frappe.db.get_value("CRM Form Script", setup.DEAL_SCRIPT, "script")
		for needle in (menu, "ffl_core.api.crm.deal_holds", "ffl_core.api.crm.hold_guns",
				"ffl_core.api.crm.extend_hold", "ffl_core.api.crm.release_hold", "Hold for 48 hours"):
			self.assertIn(needle, script)


class TestSweepCatches(DealHoldCase):
	def test_a_web_order_that_got_there_first(self):
		serial = self._gun()
		deal = self._deal(serial)
		name = self._held(deal, serial)
		frappe.db.set_value("Serial No", serial, {"woo_reserved": 1, "woo_order": "WEB-9"}, update_modified=False)
		self.assertEqual(holds.sweep().get(holds.END_TAKEN), 1)
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, "end_reason"), holds.END_TAKEN)
		tasks = _tasks(deal)
		self.assertEqual(len(tasks), 1)
		self.assertEqual(tasks[0].assigned_to, OWNER)

	def test_a_gun_that_left_stock(self):
		serial = self._gun()
		deal = self._deal(serial)
		name = self._held(deal, serial)
		frappe.db.set_value("Serial No", serial, "status", "Delivered", update_modified=False)
		holds.sweep()
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, "end_reason"), holds.END_LEFT_STOCK)
		self.assertEqual(len(_tasks(deal)), 1)

	def test_a_deal_closed_behind_the_hooks_back(self):
		serial = self._gun()
		deal = self._deal(serial)
		name = self._held(deal, serial)
		frappe.db.set_value("CRM Deal", deal, "status", "Won", update_modified=False)
		holds.sweep()
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, "end_reason"), holds.END_WON)

	def test_the_customers_own_counter_order_ends_it_as_sold(self):
		serial = self._gun()
		deal = self._deal(serial)
		name = self._held(deal, serial)
		with patch("ffl_core.crm.holds.release_sold"):
			si = self._invoice(serial, self._customer_of(deal), submit=True)
		holds.sweep()   # the five-minute sweep alone, as if the queued one never ran
		self.assertEqual(frappe.db.get_value(holds.HOLD, name, "end_reason"), holds.END_SOLD)
		self.assertIn(f"Sold to the customer on {si.name}.", _comments(deal))


class TestDeployWindow(DealHoldCase):
	def test_no_table_yet_reads_as_no_holds(self):
		serial = self._gun()
		self._held(self._deal(serial), serial)
		real = frappe.db.table_exists

		def missing(doctype, *args, **kwargs):
			return False if doctype == holds.HOLD else real(doctype, *args, **kwargs)

		with patch.object(frappe.db, "table_exists", side_effect=missing):
			self.assertNotIn(serial, counter_holds(GUN))
			self.assertEqual(holds.sweep(), {})
			holds.refuse_deal_held_serials(self._invoice_doc(serial))

	def _invoice_doc(self, serial):
		doc = frappe.new_doc("Sales Invoice")
		doc.customer = _customer()
		doc.append("items", {"item_code": GUN, "qty": 1, "rate": 1, "serial_no": serial})
		return doc


class TwoClerksAtOnce(FrappeTestCase):
	"""Two sessions hold the same gun for two deals at the same moment: one hold. Commits on purpose
	(a second connection sees only committed rows) and deletes what it made."""

	def test_one_gun_one_hold(self):
		frappe.set_user("Administrator")
		from ffl_core.crm.setup import ensure_custom_fields

		ensure_custom_fields()
		made = []
		token = _token()
		item = f"_T-DH-RACE-{token}"
		frappe.get_doc({"doctype": "Item", "item_code": item, "item_name": item, "item_group": "Firearm",
			"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1}).insert(ignore_permissions=True)
		made.append(("Item", item))
		product = upsert_product(frappe.get_doc("Item", item))   # goes with the Item (CRM's own cascade)
		warehouse = frappe.db.get_value("Warehouse", {"is_group": 0}, "name")
		serial = f"DHRACE{token}"
		company = frappe.db.get_value("Warehouse", warehouse, "company")
		frappe.get_doc({"doctype": "Serial No", "serial_no": serial, "item_code": item, "company": company}).insert(
			ignore_permissions=True)
		frappe.db.set_value("Serial No", serial, {"status": "Active", "warehouse": warehouse, "sell_price": 500},
			update_modified=False)
		made.append(("Serial No", serial))
		deals = []
		for _i in range(2):
			customer = _customer()
			contact = _contact()
			made += [("Customer", customer), ("Contact", contact)]
			deal = frappe.new_doc("CRM Deal")
			deal.status = "New"
			deal.append("contacts", {"contact": contact, "is_primary": 1})
			deal.append("products", {"product_code": product, "qty": 1, "ffl_serial_no": serial})
			deal.insert(ignore_permissions=True)
			frappe.db.set_value("CRM Deal", deal.name, "erpnext_customer", customer)
			deals.append(deal.name)
			made.append(("CRM Deal", deal.name))
		frappe.db.commit()   # the other sessions must see the gun and the deals (deleted in the finally below)
		site, results = frappe.local.site, []
		barrier = threading.Barrier(2)
		real_refusal = holds._shelf_refusal

		def slow_refusal(*args, **kwargs):
			import time

			time.sleep(1.5)   # both sessions inside the check at once, unless the locks serialize them
			return real_refusal(*args, **kwargs)

		def hold(deal):
			frappe.init(site=site, sites_path=frappe.local.sites_path if hasattr(frappe.local, "sites_path") else ".")
			frappe.connect()
			try:
				frappe.set_user("Administrator")
				barrier.wait(timeout=30)
				out = holds.hold_guns(frappe.get_doc("CRM Deal", deal), [serial])
				frappe.db.commit()
				results.append(out)
			except Exception as e:
				results.append({"error": type(e).__name__ + ": " + str(e)})
				frappe.db.rollback()
			finally:
				frappe.destroy()

		def real_commit():
			# The sessions are not the test's transaction: their commit before the locks is the real one
			# (ffl_core.utils.commit skips it while the runner's in_test flag is set, here too).
			frappe.db.commit()

		try:
			with patch.object(holds, "holds_on", return_value=True), \
					patch.object(holds, "commit", side_effect=real_commit), \
					patch.object(holds, "_shelf_refusal", side_effect=slow_refusal):
				threads = [threading.Thread(target=hold, args=(d,)) for d in deals]
				for t in threads:
					t.start()
				for t in threads:
					t.join(timeout=120)
			self.assertEqual(len(results), 2, results)
			self.assertEqual(sum(len(r.get("held") or []) for r in results), 1, results)
			refused = [r for r in results if r.get("refused")]
			self.assertEqual(len(refused), 1, results)
			self.assertIn("already held", refused[0]["refused"][0]["reason"])
			frappe.db.rollback()
			self.assertEqual(len(frappe.get_all(holds.HOLD, filters={"serial_no": serial, "status": "Active"})), 1)
		finally:
			frappe.db.rollback()
			for name in frappe.get_all(holds.HOLD, filters={"serial_no": serial}, pluck="name"):
				frappe.delete_doc(holds.HOLD, name, force=True, ignore_permissions=True)
			frappe.db.delete("Comment", {"reference_doctype": "CRM Deal", "reference_name": ["in", deals]})
			for doctype, name in reversed(made):
				if frappe.db.exists(doctype, name):
					frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
			frappe.db.commit()


class TestContract(CRMTestCase):
	def test_crm_names_the_holds_rely_on(self):
		fields = lambda doctype: {f.fieldname for f in frappe.get_meta(doctype).fields}  # noqa: E731
		self.assertTrue({"type"} <= fields("CRM Deal Status"))
		self.assertTrue({"erpnext_customer", "deal_owner", "products", "status"} <= fields("CRM Deal"))
		self.assertIn("ffl_serial_no", fields("CRM Products"))
		self.assertTrue(frappe.get_meta("CRM Deal").allow_rename, "renaming a deal: on_deal_rename follows it")
		self.assertTrue(frappe.get_meta(holds.HOLD).get_field("active_serial").unique)
		cron = frappe.get_hooks("scheduler_events")["cron"]["*/5 * * * *"]
		self.assertIn("ffl_core.crm.holds.sweep", cron)
		self.assertEqual(flt(holds.HOLD_HOURS), 48)
