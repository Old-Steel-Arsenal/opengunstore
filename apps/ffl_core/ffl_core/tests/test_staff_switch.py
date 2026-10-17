"""Switching staff and the idle lock: the staff list, lock / unlock, and the setting."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core import staff_switch
from ffl_core.tests.settings_sandbox import SettingsSandbox
from ffl_core.utils import single_raw
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

	def test_the_signed_in_user_is_always_listed(self):
		"""A locked screen is unlocked by picking yourself: Administrator (no counter role
		needed, never listed for others) sees itself, marked current."""
		frappe.set_user("Administrator")
		rows = {r["user"]: r for r in staff_switch.counter_users()}
		self.assertIn("Administrator", rows)
		self.assertTrue(rows["Administrator"]["current"])
		self.assertEqual(sum(1 for r in rows.values() if r["current"]), 1)
		frappe.set_user(_staff("switch-clerk@example.com", "Sales User"))
		self.assertNotIn("Administrator", {r["user"] for r in staff_switch.counter_users()})

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
		from ffl_core.patches import seed_counter_lock_minutes as patch
		self.set_single("FFL Settings", "counter_lock_minutes", 0)
		patch.execute()
		self.assertEqual(staff_switch.lock_minutes(), 5)
		self.set_single("FFL Settings", "counter_lock_minutes", 12)
		patch.execute()
		self.assertEqual(staff_switch.lock_minutes(), 12)

	def test_fresh_install_stores_five_and_keeps_a_choice(self):
		self.clear_single("FFL Settings", "counter_lock_minutes")
		staff_switch.ensure_lock_default()
		self.assertEqual(single_raw("FFL Settings", "counter_lock_minutes"), "5")
		self.set_single("FFL Settings", "counter_lock_minutes", 0)
		staff_switch.ensure_lock_default()
		self.assertEqual(staff_switch.lock_minutes(), 0)
		self.assertIn("ffl_core.staff_switch.ensure_lock_default", _hooks_list("after_install"))

	def test_wired_into_boot(self):
		self.assertIn("ffl_core.staff_switch.extend_bootinfo", _hooks_list("extend_bootinfo"))


class CounterPin(FrappeTestCase):
	"""An optional counter PIN: set with the password, used to unlock and to switch, five
	wrong ones fall back to the password, a manager clears it, and the hash never leaves
	frappe's __Auth table."""

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")
		self.clerk = _staff("switch-clerk@example.com", "Sales User")
		self.other = _staff("switch-other@example.com", "Sales User")
		self.buyer = _staff("switch-buyer@example.com", "Purchase User")
		self.manager = _staff("switch-manager@example.com", "Sales Manager")
		for u in (self.clerk, self.other):
			staff_switch.remove_encrypted_password("User", u, staff_switch.PIN_FIELD)
			frappe.cache.delete(staff_switch._pin_fail_key(u))
			self.addCleanup(frappe.cache.delete, staff_switch._pin_fail_key(u))
		self.addCleanup(lambda: (frappe.session.get("data") or {}).pop(staff_switch.LOCK_KEY, None))

	def _set(self, user, pin="4827"):
		frappe.set_user(user)
		return staff_switch.set_pin(_PASSWORD, pin, pin)

	def test_setting_a_pin_needs_the_password_and_a_real_pin(self):
		frappe.set_user(self.clerk)
		with self.assertRaises(staff_switch.WrongPassword):
			staff_switch.set_pin("wrong", "4827", "4827")
		for bad in ("1234", "1111", "987654", "12", "1234567", "12a4"):
			with self.subTest(pin=bad), self.assertRaises(frappe.ValidationError):
				staff_switch.set_pin(_PASSWORD, bad, bad)
		with self.assertRaises(frappe.ValidationError):
			staff_switch.set_pin(_PASSWORD, "4827", "4828")
		self.assertEqual(staff_switch.set_pin(_PASSWORD, "4827", "4827"), {"has_pin": True})

	def test_has_pin_is_all_that_shows(self):
		self._set(self.clerk)
		rows = {r["user"]: r for r in staff_switch.counter_users()}
		self.assertEqual((rows[self.clerk]["has_pin"], rows[self.clerk]["pin_off"]), (True, False))
		self.assertFalse(rows[self.other]["has_pin"])
		stored = frappe.db.sql("SELECT password, encrypted FROM `__Auth` WHERE doctype='User' AND name=%s AND fieldname=%s",
			(self.clerk, staff_switch.PIN_FIELD))[0]
		self.assertEqual(stored[1], 0, "a one-way hash, not an encrypted (reversible) Password field")
		self.assertNotIn("4827", stored[0])
		self.assertNotIn(stored[0], frappe.as_json(staff_switch.counter_users()))
		frappe.set_user("Administrator")
		self.assertNotIn(stored[0], frappe.as_json(frappe.get_doc("User", self.clerk).as_dict()))

	def test_unlock_with_the_pin(self):
		self._set(self.clerk)
		with _no_session_write():
			staff_switch.lock_screen()
			with self.assertRaises(staff_switch.WrongPassword):
				staff_switch.unlock_screen(pin="1357")
			self.assertTrue(staff_switch._session_locked())
			staff_switch.unlock_screen(pin="4827")
			self.assertFalse(staff_switch._session_locked())
		with self.assertRaises(staff_switch.WrongPassword):
			frappe.set_user(self.other)
			staff_switch.unlock_screen(pin="4827")   # no PIN of their own: the password

	def test_five_wrong_pins_fall_back_to_the_password(self):
		self._set(self.clerk)
		with _no_session_write():
			staff_switch.lock_screen()
			for _ in range(staff_switch.PIN_TRIES):
				with self.assertRaises(staff_switch.WrongPassword):
					staff_switch.unlock_screen(pin="1357")
			with self.assertRaisesRegex(staff_switch.WrongPassword, "PIN is off"):
				staff_switch.unlock_screen(pin="4827")   # even the right one now
			self.assertTrue({r["user"]: r for r in staff_switch.counter_users()}[self.clerk]["pin_off"])
			staff_switch.unlock_screen(_PASSWORD)   # the password turns it back on
			self.assertFalse(staff_switch._session_locked())
			staff_switch.lock_screen()
			staff_switch.unlock_screen(pin="4827")
		self.assertIn("ffl-pin-fail:*", _hooks_list("persistent_cache_keys"))
		self.assertIn("ffl_core.staff_switch.on_password_login", _hooks_list("on_login"))

	def test_a_password_sign_in_turns_the_pin_back_on(self):
		frappe.cache.set(staff_switch._pin_fail_key(self.clerk), staff_switch.PIN_TRIES)
		with patch.object(frappe.local, "request", frappe._dict(path="/api/method/login"), create=True):
			staff_switch.on_password_login(frappe._dict(user=self.clerk))
		self.assertFalse(staff_switch._pin_off(self.clerk))
		frappe.cache.set(staff_switch._pin_fail_key(self.clerk), staff_switch.PIN_TRIES)
		with patch.object(frappe.local, "request", frappe._dict(path="/api/method/ffl_core.staff_switch.switch_with_pin"),
				create=True):
			staff_switch.on_password_login(frappe._dict(user=self.clerk))   # a PIN switch is not a password
		self.assertTrue(staff_switch._pin_off(self.clerk))

	def test_switch_with_a_pin_starts_their_session(self):
		self._set(self.other, "5038")
		frappe.set_user(self.clerk)
		manager = MagicMock()
		with patch.object(frappe.local, "login_manager", manager, create=True), \
				patch("frappe.sessions.delete_session"):
			with self.assertRaises(staff_switch.WrongPassword):
				staff_switch.switch_with_pin(self.other, "1357")
			manager.login_as.assert_not_called()
			with self.assertRaises(staff_switch.WrongPassword):
				staff_switch.switch_with_pin("Administrator", "5038")
			with self.assertRaises(staff_switch.WrongPassword):
				staff_switch.switch_with_pin(self.buyer, "5038")
			self.assertEqual(staff_switch.switch_with_pin(self.other, "5038"), {"user": self.other})
		manager.login_as.assert_called_once_with(self.other)

	def test_a_manager_clears_a_pin(self):
		self._set(self.clerk)
		frappe.set_user(self.other)
		with self.assertRaises(frappe.PermissionError):
			staff_switch.clear_pin(self.clerk)
		frappe.set_user(self.manager)
		self.assertEqual(staff_switch.clear_pin(self.clerk), {"has_pin": False})
		self.assertFalse({r["user"]: r for r in staff_switch.counter_users()}[self.clerk]["has_pin"])


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
