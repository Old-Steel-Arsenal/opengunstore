"""Switching staff and the idle lock: the staff list, lock / unlock, and the setting."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core import staff_switch
from ffl_core.tests.settings_sandbox import SettingsSandbox
from ffl_core.tests.test_customer_profile import _user

_PASSWORD = "Counter-Pass-91!"


def _staff(email, *roles, enabled=1):
	user = _user(email, *roles)
	from frappe.utils.password import update_password
	update_password(user, _PASSWORD)
	frappe.db.set_value("User", user, "enabled", enabled)
	return user


class StaffList(FrappeTestCase):

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")

	def test_everyone_with_a_counter_role_and_no_one_else(self):
		clerk = _staff("switch-clerk@example.com", "Sales User")
		manager = _staff("switch-manager@example.com", "Sales Manager")
		buyer = _staff("switch-buyer@example.com", "Purchase User")
		gone = _staff("switch-gone@example.com", "Sales User", enabled=0)
		frappe.set_user(clerk)
		rows = {r["user"]: r for r in staff_switch.counter_users()}
		self.assertIn(clerk, rows)
		self.assertIn(manager, rows)
		self.assertNotIn(buyer, rows)
		self.assertNotIn(gone, rows)
		self.assertNotIn("Administrator", rows)
		self.assertTrue(rows[clerk]["current"])
		self.assertFalse(rows[manager]["current"])

	def test_only_counter_staff_may_list(self):
		frappe.set_user(_staff("switch-buyer@example.com", "Purchase User"))
		with self.assertRaises(frappe.PermissionError):
			staff_switch.counter_users()


class Locking(SettingsSandbox, FrappeTestCase):

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user(_staff("switch-clerk@example.com", "Sales User"))
		self.addCleanup(lambda: (frappe.session.get("data") or {}).pop(staff_switch.LOCK_KEY, None))

	def test_lock_then_unlock_with_your_own_password(self):
		with _no_session_write():
			staff_switch.lock_screen()
			self.assertTrue(staff_switch._session_locked())
			with self.assertRaises(staff_switch.WrongPassword) as cm:
				staff_switch.unlock_screen("wrong")
			# A bare AuthenticationError would make frappe drop the session cookies.
			self.assertIsNot(type(cm.exception), frappe.AuthenticationError)
			self.assertTrue(staff_switch._session_locked())
			staff_switch.unlock_screen(_PASSWORD)
			self.assertFalse(staff_switch._session_locked())

	def test_ten_wrong_passwords_pause_unlocking_for_that_person(self):
		key = frappe.cache.make_key(f"ffl-unlock-fail:{frappe.session.user}")
		self.addCleanup(frappe.cache.delete, key)
		with _no_session_write():
			staff_switch.lock_screen()
			for _ in range(staff_switch.UNLOCK_TRIES):
				with self.assertRaises(staff_switch.WrongPassword):
					staff_switch.unlock_screen("wrong")
			with self.assertRaisesRegex(staff_switch.WrongPassword, "Too many"):
				staff_switch.unlock_screen(_PASSWORD)   # even the right one waits
			self.assertGreater(frappe.cache.ttl(key), 0)
			frappe.cache.delete(key)
			staff_switch.unlock_screen(_PASSWORD)
			self.assertFalse(staff_switch._session_locked())

	def test_boot_tells_the_page(self):
		with _no_session_write():
			staff_switch.lock_screen()
			boot = frappe._dict()
			staff_switch.extend_bootinfo(boot)
		self.assertEqual(boot.ffl_screen_locked, 1)

	def test_lock_minutes_default_five_and_zero_means_never(self):
		self.clear_single("FFL Settings", "counter_lock_minutes")
		self.assertEqual(staff_switch.lock_minutes(), 5)
		self.set_single("FFL Settings", "counter_lock_minutes", 0)
		self.assertEqual(staff_switch.lock_minutes(), 0)
		self.set_single("FFL Settings", "counter_lock_minutes", 12)
		self.assertEqual(staff_switch.lock_minutes(), 12)

	def test_patch_turns_a_stored_zero_into_five_and_keeps_a_choice(self):
		from ffl_core.patches import v0_1_3_counter_lock_minutes_five as patch
		self.set_single("FFL Settings", "counter_lock_minutes", 0)
		patch.execute()
		self.assertEqual(staff_switch.lock_minutes(), 5)
		self.set_single("FFL Settings", "counter_lock_minutes", 12)
		patch.execute()
		self.assertEqual(staff_switch.lock_minutes(), 12)

	def test_wired_into_boot(self):
		self.assertIn("ffl_core.staff_switch.extend_bootinfo", _hooks_list("extend_bootinfo"))


class _no_session_write:
	"""A test has no real session object to persist: keep the flag in memory only."""

	def __enter__(self):
		self._had = getattr(frappe.local, "session_obj", None)

		class _Obj:
			def update(self, force=False):
				return None
		frappe.local.session_obj = _Obj()
		if frappe.session.get("data") is None:
			frappe.session.data = frappe._dict()

	def __exit__(self, *exc):
		frappe.local.session_obj = self._had


def _hooks_list(name):
	import ast
	import os
	path = os.path.join(frappe.get_app_path("ffl_core"), "hooks.py")
	tree = ast.parse(open(path).read())
	for node in tree.body:
		if isinstance(node, ast.Assign) and node.targets[0].id == name:
			return ast.literal_eval(node.value)
	return []
