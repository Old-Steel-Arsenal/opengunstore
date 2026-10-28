"""setup/pos_compat: the counter roles' select on POS Profile, and the drift check.

ERPNext 16.50 checks select-or-read on POS Profile in every register endpoint and,
in-process, in POS Invoice.validate_stock_availablility; Sales User and Stock Manager
have neither in stock ERPNext, so a counter sale stopped submitting
(test_cash_drawer_pos, 12 red).

Each test runs inside a savepoint that its cleanup rolls back, with frappe.db.commit
turned into a failure, so the site's own POS Profile rules always come back. Each
test starts from the stock rules (no Custom DocPerm on POS Profile).
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.roles import COUNTER_ROLES
from ffl_core.setup import dev_site, pos_compat
from ffl_core.tests.test_cash_drawer import _user

_FLAGS = pos_compat._PERM_FLAGS
_SAVEPOINT = "pos_profile_perm_test"
_DRIFT = "POS Profile permissions differ from ERPNext (%"
_ANY_DRIFT = "% permissions differ from ERPNext (%"  # every doctype check_perm_drift covers


def _rows(doctype: str) -> dict:
	"""pos_compat.perm_rows keyed as {(role, permlevel, if_owner): {flag: value}} (tests only
	build tables without duplicate keys; the drift check reads the rows themselves)."""
	return {row[:3]: dict(zip(_FLAGS, row[3])) for row in pos_compat.perm_rows(doctype)}


def _drop_global_defaults(parent=None) -> None:
	"""Forget the cached "__global" defaults only. frappe.db.set_global flushes the whole site cache
	(frappe.defaults._clear_cache), which would leave every later test module on a cold cache."""
	frappe.client_cache.delete_value(f"defaults::{parent or '__global'}")


def _grants(flags) -> bool:
	return bool(flags and (flags["select"] or flags["read"]))


def _isolate(test, doctypes, keys) -> None:
	"""Run ``test`` inside a savepoint its cleanup rolls back, with frappe.db.commit turned into a failure, from
	the stock rules of ``doctypes`` (no Custom DocPerm) and with the DefaultValue ``keys`` cleared, as on a site
	that never ran the grants. Error Log rows the test leaves (MyISAM, past every rollback) are deleted."""
	frappe.set_user("Administrator")
	test.addCleanup(frappe.set_user, "Administrator")
	for doctype in doctypes:
		test.addCleanup(frappe.clear_cache, doctype=doctype)
	test.addCleanup(_drop_global_defaults)  # the grant/drift records live in DefaultValue
	frappe.db.savepoint(_SAVEPOINT)
	test.addCleanup(frappe.db.rollback, save_point=_SAVEPOINT)
	# A commit would release the savepoint and keep this test's deletes: fail instead.
	no_commit = patch.object(frappe.db, "commit", side_effect=AssertionError("commit inside a permission test"))
	no_commit.start()
	test.addCleanup(no_commit.stop)
	narrow = patch.object(frappe.defaults, "_clear_cache", side_effect=_drop_global_defaults)
	narrow.start()
	test.addCleanup(narrow.stop)
	for doctype in doctypes:
		frappe.db.delete("Custom DocPerm", {"parent": doctype})  # back to the stock rules, until the rollback
		frappe.clear_cache(doctype=doctype)
	for key in keys:
		frappe.db.set_global(key, "")
	known = set(frappe.get_all("Error Log", filters={"method": ("like", _ANY_DRIFT)}, pluck="name"))
	test.addCleanup(lambda: [frappe.db.delete("Error Log", {"name": n}) for n in
		frappe.get_all("Error Log", filters={"method": ("like", _ANY_DRIFT)}, pluck="name") if n not in known])


class CounterRolesPOSProfileSelect(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		# Registered BEFORE super(): class cleanups run LIFO, so this runs after its rollback.
		cls.addClassCleanup(frappe.clear_cache, doctype="POS Profile")
		super().setUpClass()
		dev_site.run()  # the shared test company and its POS Profile (a fresh site has neither yet)
		cls.sysmgr = _user("posperm-sysmgr@example.com", "System Manager")  # who a drift alert is assigned to
		cls.cashiers = {
			"Sales User": _user("posperm-cashier@example.com", "Sales User"),
			"Stock Manager": _user("posperm-stockmgr@example.com", "Stock Manager"),
		}
		cls.profile = dev_site.COMPANY
		cls.warehouse = frappe.db.get_value("POS Profile", cls.profile, "warehouse")

	def setUp(self):
		super().setUp()
		_isolate(self, ("POS Profile",), (pos_compat._GRANTED_KEY, pos_compat._DRIFT_SEEN_KEY))

	def _rule(self, doctype="Custom DocPerm", role="Sales User", **flags):
		"""A rule with exactly ``flags`` (the doctype defaults read/export to 1)."""
		from frappe.permissions import setup_custom_perms

		if doctype == "Custom DocPerm":
			setup_custom_perms("POS Profile")
		frappe.get_doc({"doctype": doctype, "parent": "POS Profile", "parenttype": "DocType",
			"parentfield": "permissions", "role": role, "permlevel": 0,
			"read": 0, "export": 0, **flags}).db_insert()
		frappe.clear_cache(doctype="POS Profile")

	def _drift_logs(self) -> int:
		return frappe.db.count("Error Log", {"method": ("like", _DRIFT)})

	def _new_shipped_rule(self) -> str:
		"""ERPNext ships a rule after the rows were frozen into Custom DocPerm: a role unique to this run,
		so its digest is too (Error Log rows of earlier runs survive)."""
		role = f"posperm-drift-{frappe.generate_hash(length=8)}"
		self._rule("DocPerm", role=role, select=1)
		return role

	def _open_drift_todos(self) -> list:
		"""This class's System Manager's open drift ToDos (the site may hold other System Managers)."""
		return frappe.get_all("ToDo", fields=["description", "allocated_to"], filters={"status": "Open",
			"allocated_to": self.sysmgr, "reference_type": "DocType", "reference_name": "POS Profile",
			"description": ("like", "%" + _DRIFT)})

	def test_grants_select_only_to_each_counter_role_and_keeps_every_stock_row(self):
		stock = _rows("DocPerm")
		lacking = [r for r in COUNTER_ROLES if not _grants(stock.get((r, 0, 0)))]
		self.assertIn("Sales User", lacking)
		self.assertIn("Stock Manager", lacking)
		pos_compat.ensure_cashier_pos_profile_select()
		site = _rows("Custom DocPerm")
		for role in lacking:
			self.assertEqual({f for f, v in site.pop((role, 0, 0)).items() if v}, {"select"}, role)
		self.assertEqual(site, stock)  # the stock rows came across unchanged

	def test_each_counter_cashier_can_ring_and_still_cannot_open_the_profile(self):
		from erpnext.accounts.doctype.pos_invoice.pos_invoice import get_stock_availability
		from erpnext.selling.page.point_of_sale.point_of_sale import get_pos_profile_data

		pos_compat.ensure_cashier_pos_profile_select()
		for role, user in self.cashiers.items():
			with self.subTest(role):
				frappe.set_user(user)
				# the in-process call every stock line of a submitted POS Invoice makes; no such item -> non-stock answer
				self.assertEqual(get_stock_availability("POSPERM-NO-SUCH-ITEM", self.warehouse), (0, False, False))
				self.assertEqual(get_pos_profile_data(self.profile)["name"], self.profile)
				self.assertTrue(frappe.has_permission("POS Profile", "select", doc=self.profile))
				self.assertFalse(frappe.has_permission("POS Profile", "read"))
				self.assertFalse(frappe.has_permission("POS Profile", "write"))
				frappe.set_user("Administrator")

	def test_idempotent(self):
		pos_compat.ensure_cashier_pos_profile_select()
		before = _rows("Custom DocPerm")
		pos_compat.ensure_cashier_pos_profile_select()
		self.assertEqual(_rows("Custom DocPerm"), before)
		self.assertEqual(frappe.db.count("Custom DocPerm", {"parent": "POS Profile", "role": "Sales User"}), 1)

	def test_an_existing_rule_gains_select_and_keeps_its_flags(self):
		self._rule(print=1)
		pos_compat.ensure_cashier_pos_profile_select()
		row = _rows("Custom DocPerm")[("Sales User", 0, 0)]
		self.assertEqual((row["select"], row["print"], row["read"], row["export"]), (1, 1, 0, 0))
		self.assertEqual(frappe.db.count("Custom DocPerm", {"parent": "POS Profile", "role": "Sales User"}), 1)

	def test_never_narrows_a_read_grant(self):
		self._rule(read=1)
		before = _rows("Custom DocPerm")[("Sales User", 0, 0)]
		pos_compat.ensure_cashier_pos_profile_select()
		self.assertEqual(_rows("Custom DocPerm")[("Sales User", 0, 0)], before)

	def test_never_widens_an_owner_only_rule(self):
		self._rule(if_owner=1, select=1)
		self._rule(role="Stock Manager", if_owner=1, print=1)  # owner-only, granting neither
		before = _rows("Custom DocPerm")
		pos_compat.ensure_cashier_pos_profile_select()
		site = _rows("Custom DocPerm")
		for role in ("Sales User", "Stock Manager"):
			self.assertNotIn((role, 0, 0), site, role)
			self.assertEqual(site[(role, 0, 1)], before[(role, 0, 1)], role)

	def test_writes_nothing_while_the_stock_rows_already_grant_it(self):
		# A later ERPNext shipping the grant itself: no Custom DocPerm, so its rows keep reaching the site.
		stock = _rows("DocPerm")
		for role in COUNTER_ROLES:
			if not _grants(stock.get((role, 0, 0))):
				self._rule("DocPerm", role=role, select=1)
		pos_compat.ensure_cashier_pos_profile_select()
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": "POS Profile"}))

	def test_the_grant_alone_is_no_drift(self):
		pos_compat.ensure_cashier_pos_profile_select()
		before = self._drift_logs()
		pos_compat.check_perm_drift()
		self.assertEqual(self._drift_logs(), before)

	def test_a_changed_shipped_rule_is_reported_once(self):
		pos_compat.ensure_cashier_pos_profile_select()
		before = self._drift_logs()
		role = self._new_shipped_rule()
		pos_compat.check_perm_drift()
		pos_compat.check_perm_drift()  # the same difference: no second alert
		self.assertEqual(self._drift_logs(), before + 1)
		log = frappe.get_all("Error Log", filters={"method": ("like", _DRIFT)}, fields=["error"],
			order_by="creation desc", limit=1)[0]
		self.assertIn(f"ERPNext ships {role}, level 0: select; this site lacks it", log.error)
		self.assertNotIn("Sales User", log.error)
		self.assertEqual(len(self._open_drift_todos()), 1)

	def test_a_pos_profile_digest_is_its_detail_alone(self):
		"""The digest keying predates the other doctypes: a POS Profile difference already reviewed on a site
		hashes the same and is not reported again."""
		import hashlib

		pos_compat.ensure_cashier_pos_profile_select()
		role = self._new_shipped_rule()
		pos_compat.check_perm_drift()
		digest = hashlib.sha1(f"ERPNext ships {role}, level 0: select; this site lacks it".encode()).hexdigest()[:12]
		self.assertIn(digest, pos_compat._get_list(pos_compat._DRIFT_SEEN_KEY))
		self.assertTrue(frappe.db.exists("Error Log", {"method": f"POS Profile permissions differ from ERPNext ({digest})"}))

	def test_a_reported_difference_stays_quiet_after_its_error_log_is_purged(self):
		pos_compat.ensure_cashier_pos_profile_select()
		self._new_shipped_rule()
		pos_compat.check_perm_drift()
		frappe.db.delete("Error Log", {"method": ("like", _DRIFT)})  # retention, or someone clearing the log
		pos_compat.check_perm_drift()
		self.assertEqual(self._drift_logs(), 0)

	def test_a_newer_difference_replaces_the_open_alert_and_notifies_again(self):
		pos_compat.ensure_cashier_pos_profile_select()
		self._new_shipped_rule()
		pos_compat.check_perm_drift()
		[first] = self._open_drift_todos()
		self._new_shipped_rule()
		pos_compat.check_perm_drift()
		[second] = self._open_drift_todos()  # the first one closed, so this is a new assignment
		self.assertNotEqual(first.description, second.description)

	def test_a_duplicated_rule_is_a_difference(self):
		pos_compat.ensure_cashier_pos_profile_select()
		self._rule(role="Accounts User", read=1, print=1, email=1, report=1)  # the stock rule, a second time
		pos_compat.check_perm_drift()
		log = frappe.get_all("Error Log", filters={"method": ("like", _DRIFT)}, fields=["error"],
			order_by="creation desc", limit=1)[0]
		self.assertIn("this site has Accounts User, level 0:", log.error)

	def test_a_role_an_admin_removed_is_not_granted_again(self):
		pos_compat.ensure_cashier_pos_profile_select()
		frappe.db.delete("Custom DocPerm", {"parent": "POS Profile", "role": "Stock Manager"})
		pos_compat.ensure_cashier_pos_profile_select()  # the next migrate
		self.assertNotIn(("Stock Manager", 0, 0), _rows("Custom DocPerm"))
		self.assertIn(("Sales User", 0, 0), _rows("Custom DocPerm"))

	def test_runs_on_install_and_on_migrate(self):
		# Both: a fresh `install-app` never migrates (docs/testing.md).
		for hook in ("after_install", "after_migrate"):
			self.assertIn("ffl_core.setup.pos_compat.ensure_cashier_pos_profile_select", frappe.get_hooks(hook), hook)
		self.assertIn("ffl_core.setup.pos_compat.check_perm_drift", frappe.get_hooks("after_migrate"))
		hooks = frappe.get_hooks("after_migrate")
		self.assertLess(hooks.index("ffl_core.setup.pos_compat.ensure_cashier_pos_profile_select"),
			hooks.index("ffl_core.setup.pos_compat.check_perm_drift"))


class RoleGrantDoctypesDrift(FrappeTestCase):
	"""check_perm_drift beyond POS Profile: the trade-in's Serial and Batch Bundle, and nothing else ROLE_GRANTS
	writes."""

	DOCTYPES = tuple(dict.fromkeys(doctype for doctype, *_ in pos_compat.ROLE_GRANTS))

	@classmethod
	def setUpClass(cls):
		for doctype in cls.DOCTYPES:
			cls.addClassCleanup(frappe.clear_cache, doctype=doctype)
		super().setUpClass()
		cls.sysmgr = _user("posperm-sysmgr@example.com", "System Manager")

	def setUp(self):
		super().setUp()
		_isolate(self, self.DOCTYPES, (pos_compat._ROLE_GRANTS_KEY, pos_compat._DRIFT_SEEN_KEY))
		pos_compat.ensure_role_grants()

	def _logs(self, doctype: str) -> list:
		return frappe.get_all("Error Log", filters={"method": ("like", f"{doctype} permissions differ from ERPNext (%")},
			fields=["error"], order_by="creation desc")

	def _ship(self, doctype: str) -> str:
		"""ERPNext ships a new rule on ``doctype`` after its rows were frozen: a role unique to this run."""
		role = f"posperm-drift-{frappe.generate_hash(length=8)}"
		frappe.get_doc({"doctype": "DocPerm", "parent": doctype, "parenttype": "DocType",
			"parentfield": "permissions", "role": role, "permlevel": 0, "select": 1}).db_insert()
		frappe.clear_cache(doctype=doctype)
		return role

	def test_covers_pos_profile_and_the_bundle_and_our_grants_come_from_the_data(self):
		self.assertEqual(pos_compat._DRIFT_DOCTYPES, ("POS Profile", "Serial and Batch Bundle", "POS Invoice",
			"POS Opening Entry", "POS Closing Entry", "Sales Invoice"))
		counter = {"write", "create", "delete", "submit"}
		entry = {"read", "write", "create", "submit"}
		self.assertEqual(pos_compat._our_grants(), {
			**{("POS Profile", role): {"select"} for role in COUNTER_ROLES},
			("POS Profile", "Sales User"): {"select", "read"},
			("POS Profile", "Sales Manager"): {"select", "read"},
			("POS Invoice", "Sales User"): {"report"} | counter,
			("POS Invoice", "Sales Manager"): counter,
			("FFL Acquisition", "Sales User"): {"read", "report"},
			("FFL Acquisition", "Sales Manager"): {"read", "report"},
			("Serial and Batch Bundle", "Sales User"): {"read", "write", "create", "submit"},
			("POS Opening Entry", "Sales User"): entry,
			("POS Closing Entry", "Sales User"): entry,
			("Sales Invoice", "Sales User"): entry,
			("Sales Invoice", "Sales Manager"): entry,
		})

	def test_our_own_grant_rows_are_no_drift(self):
		self.assertTrue(frappe.db.exists("Custom DocPerm", {"parent": "Serial and Batch Bundle"}))
		before = len(self._logs("Serial and Batch Bundle"))
		pos_compat.check_perm_drift()
		self.assertEqual(len(self._logs("Serial and Batch Bundle")), before)

	def test_a_changed_shipped_bundle_rule_is_reported_once(self):
		before = len(self._logs("Serial and Batch Bundle"))
		role = self._ship("Serial and Batch Bundle")
		pos_compat.check_perm_drift()
		pos_compat.check_perm_drift()  # the same difference: no second alert
		logs = self._logs("Serial and Batch Bundle")
		self.assertEqual(len(logs), before + 1)
		self.assertIn(f"ERPNext ships {role}, level 0: select; this site lacks it", logs[0].error)
		self.assertNotIn("Sales User", logs[0].error)  # our grant row is not reported
		todos = frappe.get_all("ToDo", pluck="description", filters={"status": "Open",
			"allocated_to": self.sysmgr, "reference_type": "DocType", "reference_name": "Serial and Batch Bundle",
			"description": ("like", "%Serial and Batch Bundle permissions differ from ERPNext (%")})
		self.assertEqual(len(todos), 1)

	def test_a_doctype_outside_the_list_is_not_compared(self):
		"""FFL Acquisition's grants predate the drift check: frozen into Custom DocPerm, it is still not compared."""
		from frappe.permissions import setup_custom_perms

		setup_custom_perms("FFL Acquisition")
		before = len(self._logs("FFL Acquisition"))
		self._ship("FFL Acquisition")
		pos_compat.check_perm_drift()
		self.assertEqual(len(self._logs("FFL Acquisition")), before)

	def test_a_rule_an_admin_widened_is_reported(self):
		"""Our row with a flag we never grant (delete on the bundle) is a difference, not our grant."""
		frappe.db.set_value("Custom DocPerm", {"parent": "Serial and Batch Bundle", "role": "Sales User"}, "delete", 1)
		pos_compat.check_perm_drift()
		[log] = self._logs("Serial and Batch Bundle")[:1]
		self.assertIn("this site has Sales User, level 0: select, read, write, create, delete, submit; "
			"ERPNext ships no such rule", log.error)
