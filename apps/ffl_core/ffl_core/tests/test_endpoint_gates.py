"""Endpoint-gate hardening (osa/endpoint-gates adjudication, 2026-08-07).

Two layers:

1. Behavioral: a logged-in user with NO roles must be rejected at ENTRY —
   before any payload validation or DB write. create_trade_in_intake is the
   compliance-critical case (its _refresh_pending_draft branch used to
   rewrite FFL Acquisition draft seller identity/cost with no gate at all).

2. Static: every module-level ``@frappe.whitelist`` endpoint in ffl_core
   must open with a recognized gate as its FIRST statement, unless it is on
   the reviewed OPEN_BY_DESIGN allowlist. This is the repo-wide version of
   the convention test_console_endpoints pins for the distributor console,
   and it makes the adjudication table executable — an ungated endpoint
   merged later fails here, not in a security review.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.gate_audit import find_shadowed_endpoints, find_ungated_endpoints

NOROLE_USER = "gates-norole@example.com"

# Reviewed 2026-08-07 — open by design:
OPEN_BY_DESIGN = {
	# ERPNext's own check_opening_entry override keeps upstream's gateless
	# semantics — the shared-register fix must stay callable by any clerk.
	"ffl_core.api.pos_session.check_opening_entry",
	# Reviewed 2026-09-11: release notes are static files shipped
	# with the code, meant for every desk user; Guest is stopped by the
	# whitelist itself (no allow_guest). Nothing store-specific to gate.
	"ffl_core.api.changelog.get_changelog",
	# Reviewed 2026-09-30: RFC 7591 dynamic client registration, overriding
	# Frappe's own allow_guest register_client (only widens its redirect check to
	# http://localhost). A registered client still needs a user to sign in and
	# approve on the POS before it holds any token.
	"ffl_core.api.oauth_dcr.register_client",
	# Reviewed 2026-10-30: the action catalog lists only what the caller's roles and
	# DocType permissions reach; desk users only (_require_desk_user), and every
	# listed endpoint still checks the caller when it is called.
	"ffl_core.api.action_catalog.list_actions",
}


class NoRoleFixtures(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		if not frappe.db.exists("User", NOROLE_USER):
			frappe.get_doc({
				"doctype": "User", "email": NOROLE_USER,
				"first_name": "Gates NoRole", "send_welcome_email": 0,
				"user_type": "System User",
			}).insert(ignore_permissions=True)
		# Strip every role fixture-side effects may have granted.
		frappe.db.delete("Has Role", {"parent": NOROLE_USER, "parenttype": "User"})
		frappe.clear_cache(user=NOROLE_USER)

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		super().tearDown()


class TestEntryGateBehavior(NoRoleFixtures):
	def _assert_rejected_at_entry(self, fn, *args, **kwargs):
		frappe.set_user(NOROLE_USER)
		with self.assertRaises(frappe.PermissionError):
			fn(*args, **kwargs)

	def test_trade_in_refresh_branch_unreachable_without_role(self):
		"""The gate must fire BEFORE the refresh/create branch split: a
		role-less caller may not reach _refresh_pending_draft at all, with
		ANY payload — no fixture needed because entry rejection precedes
		payload validation."""
		from ffl_core.api.trade_in import create_trade_in_intake

		self._assert_rejected_at_entry(
			create_trade_in_intake,
			{"serial": "GATES-T1", "apply_credit": 1})

	def test_counter_read_endpoints_reject_roleless(self):
		from ffl_core.firearm import available_serials_with_prices
		from ffl_core.api.receive_goods import lookup_item

		self._assert_rejected_at_entry(available_serials_with_prices, ["X"])
		self._assert_rejected_at_entry(lookup_item, "X")

	def test_item_read_badge_rejects_roleless(self):
		# get_item_stock gates on Item READ permission (a role-less user has
		# none) — the badge is a read, so this is has_permission, not a role.
		from ffl_core.api.item_admin import get_item_stock

		self._assert_rejected_at_entry(get_item_stock, ["X"])

	def test_draft_order_status_rejects_roleless(self):
		# The Recent Orders chips gate on POS Invoice READ, the permission the
		# order list itself needs — not a role list (the POS page is also open to
		# Accounts roles).
		from ffl_core.api.pos_order_status import draft_order_status

		self._assert_rejected_at_entry(draft_order_status, ["X"])

	def test_gate_admits_a_holder(self):
		"""Positive path: a user WITH a counter role is not rejected at entry
		(the gate blocks the role-less, not everyone)."""
		from ffl_core.api.receive_goods import get_receive_sources

		# Grant AS ADMINISTRATOR — a role-less user has no permission to give
		# itself a role, so granting after set_user() would silently no-op and
		# the "positive" assertion would test nothing.
		frappe.set_user("Administrator")
		frappe.get_doc("User", NOROLE_USER).add_roles("Sales User")
		frappe.clear_cache(user=NOROLE_USER)
		try:
			frappe.set_user(NOROLE_USER)
			# May return anything (incl. []) — the point is it does NOT raise
			# PermissionError now that the caller holds a counter role.
			get_receive_sources()
		except frappe.PermissionError:
			self.fail("counter-role holder was wrongly rejected by the gate")
		finally:
			frappe.set_user("Administrator")
			frappe.db.delete("Has Role", {"parent": NOROLE_USER, "role": "Sales User"})
			frappe.clear_cache(user=NOROLE_USER)

	def test_wrapper_bodies_bypass_the_gate(self):
		"""The anti-lockout invariant behind every wrapper+_body split: the
		gate-free bodies MUST be callable with no role (internal callers —
		on_submit hooks, provider adapters, scheduler — reach them), so they
		never raise PermissionError. Other errors (missing data) are fine."""
		frappe.set_user(NOROLE_USER)
		from ffl_core.api.item_admin import _get_item_stock
		try:
			_get_item_stock(["_no_such_item_"])  # returns {..:0}, no perm check
		except frappe.PermissionError:
			self.fail("_get_item_stock body must not enforce a role gate")


class TestGateIsFirstStatement(FrappeTestCase):
	def test_every_whitelist_endpoint_opens_with_a_gate(self):
		offenders = find_ungated_endpoints(
			"ffl_core", frappe.get_app_path("ffl_core"),
			open_by_design=OPEN_BY_DESIGN)
		self.assertFalse(
			offenders,
			"whitelisted endpoints without a first-statement gate "
			"(add the gate, or add to OPEN_BY_DESIGN with a review note):\n"
			+ "\n".join(offenders))


class TestEndpointsStillResolveToThemselves(FrappeTestCase):
	def test_no_endpoint_is_shadowed_by_a_later_assignment(self):
		"""A module-level assignment to an endpoint's own name replaces it:
		Frappe resolves /api/method/<dotted> by attribute, so the call then
		reaches an un-whitelisted object and 403s while the decorator still
		reads fine in the source. That is how
		ffl_core.api.atf_link.create_party_from_atf was dead for a week."""
		offenders = find_shadowed_endpoints("ffl_core", frappe.get_app_path("ffl_core"))
		self.assertFalse(
			offenders,
			"whitelisted endpoints shadowed by a later assignment "
			"(rename the alias, not the endpoint):\n" + "\n".join(offenders))
