"""Signing terminals: pairing, polling, Send to tablet, signing on a tablet, and the
register's add-item prompt.

Site tests against the real doctypes and the real cache (Redis). A delivery is
dropped after the request commits; tests run ``frappe.db.after_commit`` by hand
because the test transaction never commits.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core import agreements, signing_terminal
from ffl_core.tests.test_agreements import _as, _Base
from ffl_core.tests.test_customer_profile import _customer, _uniq, _user


def _terminal(**fields):
	doc = frappe.get_doc({"doctype": "Signing Terminal", "terminal_name": _uniq("iPad"), "active": 1, **fields})
	doc.insert(ignore_permissions=True)
	return doc


def _pair(terminal) -> str:
	code = signing_terminal.start_pairing(terminal.name)["code"]
	with _as("Guest"):
		return signing_terminal.pair_terminal(code)["secret"]


def _poll(secret):
	with _as("Guest"):
		return signing_terminal.terminal_poll(secret)


class Pairing(FrappeTestCase):

	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")
		self.term = _terminal()

	def test_a_code_works_once_and_gives_a_key_stored_only_as_a_hash(self):
		code = signing_terminal.start_pairing(self.term.name)["code"]
		self.assertRegex(code, r"^\d{6}$")
		with _as("Guest"):
			out = signing_terminal.pair_terminal(code)
			with self.assertRaises(frappe.ValidationError):
				signing_terminal.pair_terminal(code)
		self.assertEqual(out["terminal_name"], self.term.name)
		row = frappe.db.get_value("Signing Terminal", self.term.name, ["device_secret_hash", "paired_on"], as_dict=True)
		self.assertEqual(row.device_secret_hash, signing_terminal._hash(out["secret"]))
		self.assertTrue(row.paired_on)
		self.assertEqual(_poll(out["secret"])["paired"], True)

	def test_bad_codes(self):
		with _as("Guest"):
			for code in ("", "12345", "abcdef", "000000x"):
				with self.assertRaises(frappe.ValidationError, msg=code):
					signing_terminal.pair_terminal(code)

	def test_wrong_codes_from_anywhere_pause_pairing(self):
		key = frappe.cache.make_key(signing_terminal._PAIR_FAILURES)
		self.addCleanup(frappe.cache.delete, key)
		frappe.cache.set(key, signing_terminal.PAIR_FAILURES_MAX - 1)
		code = signing_terminal.start_pairing(self.term.name)["code"]
		with _as("Guest"):
			with self.assertRaisesRegex(frappe.ValidationError, "not valid"):
				signing_terminal.pair_terminal("999999" if code != "999999" else "999998")
			with self.assertRaisesRegex(frappe.ValidationError, "paused"):
				signing_terminal.pair_terminal(code)   # even the right code waits
		self.assertGreater(frappe.cache.ttl(key), 0)

	def test_pairing_again_cuts_off_the_old_tablet(self):
		old = _pair(self.term)
		new = _pair(self.term)
		self.assertEqual(_poll(old), {"paired": False})
		self.assertTrue(_poll(new)["paired"])

	def test_switching_off_cuts_the_tablet_off_and_stops_new_pairings(self):
		secret = _pair(self.term)
		frappe.db.set_value("Signing Terminal", self.term.name, "active", 0)
		self.assertEqual(_poll(secret), {"paired": False})
		with self.assertRaises(frappe.ValidationError):
			signing_terminal.start_pairing(self.term.name)

	def test_only_a_manager_starts_pairing(self):
		with _as(_user("terminal-clerk@example.com", "Sales User")):
			with self.assertRaises(frappe.PermissionError):
				signing_terminal.start_pairing(self.term.name)

	def test_guest_reaches_only_pairing_and_polling(self):
		with _as("Guest"):
			for fn in (signing_terminal.start_pairing, signing_terminal.list_terminals):
				with self.assertRaises(frappe.PermissionError, msg=fn.__name__):
					frappe.is_whitelisted(fn)
			for fn in (signing_terminal.pair_terminal, signing_terminal.terminal_poll):
				frappe.is_whitelisted(fn)

	def test_idle_poll_says_nothing_about_customers(self):
		out = _poll(_pair(self.term))
		self.assertEqual(set(out), {"paired", "idle", "terminal_name", "store_name"})


class SendToTablet(_Base):

	def setUp(self):
		super().setUp()
		self.term = _terminal()
		self.secret = _pair(self.term)
		self.addCleanup(frappe.cache.delete_value, signing_terminal._delivery_key(self.term.name))

	def send(self, customer=None, terminal=None, replace=1):
		r = agreements.request_agreement((customer or self.cust).name, self.tpl.name, "tablet", replace,
			(terminal or self.term).name)
		frappe.db.after_commit.run()
		return r

	def test_the_tablet_gets_the_link_once_and_signs_on_it(self):
		r = self.send()
		self.assertEqual(r["terminal"], self.term.name)
		self.assertEqual(frappe.db.get_value("Customer Agreement", r["name"], "delivery_channel"), "Terminal")
		got = _poll(self.secret)
		self.assertTrue(got["token"])
		self.assertNotIn("token", _poll(self.secret))
		self.sign(got["token"], terminal_secret=self.secret)
		doc = frappe.get_doc("Customer Agreement", r["name"])
		self.assertEqual((doc.status, doc.signed_via, doc.signing_terminal), ("Signed", "Tablet", self.term.name))

	def test_nothing_is_dropped_before_the_commit(self):
		agreements.request_agreement(self.cust.name, self.tpl.name, "tablet", 1, self.term.name)
		self.assertNotIn("token", _poll(self.secret))
		frappe.db.after_commit.run()
		self.assertIn("token", _poll(self.secret))

	def test_another_tablet_cannot_pick_it_up(self):
		other = _terminal()
		other_secret = _pair(other)
		self.send()
		self.assertNotIn("token", _poll(other_secret))
		self.assertIn("token", _poll(self.secret))

	def test_the_newest_agreement_wins(self):
		self.send()
		later = _customer()
		self.send(customer=later)
		got = _poll(self.secret)["token"]
		with _as("Guest"):
			self.assertEqual(agreements.load_for_signing(got)["customer_name"], later.customer_name)
		self.assertNotIn("token", _poll(self.secret))

	def test_an_unpaired_or_switched_off_tablet_is_refused(self):
		unpaired = _terminal()
		with self.assertRaises(frappe.ValidationError):
			agreements.request_agreement(self.cust.name, self.tpl.name, "tablet", 1, unpaired.name)
		frappe.db.set_value("Signing Terminal", self.term.name, "active", 0)
		with self.assertRaises(frappe.ValidationError):
			agreements.request_agreement(self.cust.name, self.tpl.name, "tablet", 1, self.term.name)

	def test_a_wrong_device_key_signs_as_before_but_names_no_tablet(self):
		r, token = self.request()
		self.sign(token, terminal_secret="x" * 43)
		doc = frappe.get_doc("Customer Agreement", r["name"])
		self.assertEqual((doc.status, doc.signing_terminal), ("Signed", None))


class RegisterPrompt(_Base):

	def test_the_cart_lists_what_it_needs(self):
		item = frappe.get_doc({"doctype": "Item", "item_code": _uniq("Lane Hour"), "item_group": "All Item Groups",
			"stock_uom": "Nos", "is_stock_item": 0, "ffl_required_agreement": self.tpl.name})
		item.insert(ignore_permissions=True)
		rows = lambda: agreements.agreements_for_sale(self.cust.name, [{"item_code": item.name}])["rows"]
		mine = [r for r in rows() if r["template"] == self.tpl.name]
		self.assertEqual([(r["required"], r["state"]) for r in mine], [(True, "missing")])
		_, token = self.request()
		self.sign(token)
		mine = [r for r in rows() if r["template"] == self.tpl.name]
		self.assertEqual([(r["required"], r["state"]) for r in mine], [(True, "signed")])
