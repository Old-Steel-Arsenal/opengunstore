"""Contract tests for the core hooks extensions plug into (docs/extensions/hooks.md).

Each test registers a fake provider (a function in this module) under the hook
and checks the core consumer honours it — and what core assumes when the
provider raises. The real providers (Consignment Out) are exercised by their
own tests; this file pins the contract.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

HERE = "ffl_core.tests.test_extension_hooks"


@contextmanager
def hooks(name: str, values: list):
	"""frappe.get_hooks(name) → values; every other key untouched."""
	real = frappe.get_hooks

	def fake(hook=None, default="_KEEP_", app_name=None):
		if hook == name:
			return list(values)
		return real(hook, app_name=app_name) if default == "_KEEP_" else real(hook, default, app_name)

	with patch.object(frappe, "get_hooks", side_effect=fake):
		yield


def _error_logged(title_fragment: str) -> bool:
	return bool(frappe.db.exists("Error Log", {"method": ["like", f"%{title_fragment}%"]}))


# --- fake providers -----------------------------------------------------------
def holds_a(item_code):
	return [{"serial": "HOLD-A", "source": "fake", "ref": "A-1", "kind": "fake",
		"tooltip": "held by A", "url": "/app/fake/A-1"}]


def holds_b(item_code):
	return [{"serial": "HOLD-A", "source": "fake-b", "ref": "B-1", "kind": "fake", "tooltip": "held by B", "url": ""},
		{"serial": "HOLD-B", "source": "fake-b", "ref": "B-2", "kind": "fake", "tooltip": "held by B", "url": ""}]


def holds_boom(item_code):
	raise RuntimeError("provider down")


def stock_handled_yes(doc):
	return True


def stock_handled_no(doc):
	return False


def stock_handled_boom(doc):
	raise RuntimeError("provider down")


def home_portal(user):
	return "fake-portal"


def home_none(user):
	return None


def home_boom(user):
	raise RuntimeError("provider down")


SCREENED: list[dict] = []


def screen_not_customer(mail):
	SCREENED.append(mail)
	return {"customer": False, "category": "notification", "reason": "Carrier delivery notice."}


def screen_customer(mail):
	SCREENED.append(mail)
	return {"customer": True, "category": "customer", "reason": "Asks about a rifle."}


def screen_no_opinion(mail):
	SCREENED.append(mail)
	return None


def screen_boom(mail):
	raise RuntimeError("provider down")


TOGGLES: list[tuple[str, bool]] = []


def on_toggled(app, enabled):
	TOGGLES.append((app, enabled))


def toggled_boom(app, enabled):
	raise RuntimeError("provider down")


# --- gunshop_counter_holds ------------------------------------------------------
class TestCounterHolds(FrappeTestCase):
	ITEM = "ZZ-NO-SUCH-ITEM-FOR-HOOK-TEST"

	def test_provider_rows_are_merged_first_provider_wins(self):
		from ffl_core.reservations import counter_holds
		with hooks("gunshop_counter_holds", [f"{HERE}.holds_a", f"{HERE}.holds_b"]):
			out = counter_holds(self.ITEM)
		self.assertEqual(out["HOLD-A"]["ref"], "A-1")                # setdefault: A registered first
		self.assertEqual(out["HOLD-A"]["source"], "fake")
		self.assertEqual(out["HOLD-B"]["ref"], "B-2")
		self.assertEqual(set(out["HOLD-A"]), {"source", "ref", "kind", "tooltip", "url"})

	def test_failing_provider_is_logged_and_contributes_nothing(self):
		from ffl_core.reservations import counter_holds
		with hooks("gunshop_counter_holds", [f"{HERE}.holds_boom", f"{HERE}.holds_a"]):
			out = counter_holds(self.ITEM)
		self.assertIn("HOLD-A", out)                                  # the healthy provider still counts
		self.assertTrue(_error_logged("gunshop_counter_holds provider failed"))

	def test_the_counters_own_hold_beats_a_provider_on_the_same_serial(self):
		"""Providers are merged AFTER the counter sources (setdefault)."""
		from ffl_core import reservations
		with hooks("gunshop_counter_holds", [f"{HERE}.holds_a"]), \
				patch.object(reservations, "_draft_pos_serials", return_value=[("HOLD-A", "POS-INV-1")]):
			out = reservations.counter_holds(self.ITEM)
		self.assertEqual((out["HOLD-A"]["source"], out["HOLD-A"]["ref"]), ("pos", "POS-INV-1"))

	def test_no_providers_no_change(self):
		from ffl_core.reservations import counter_holds
		with hooks("gunshop_counter_holds", []):
			self.assertEqual(counter_holds(self.ITEM), {})


# --- gunshop_pending_queue_exclusion_fields ---------------------------------------
class TestPendingQueueExclusionFields(FrappeTestCase):
	def test_declared_column_joins_the_exclusions_when_it_exists(self):
		from ffl_core.compliance import channel_invoice
		with hooks("gunshop_pending_queue_exclusion_fields", ["remarks", "no_such_column_xyz"]):
			out = channel_invoice.pending_queue_exclusions()
			self.assertEqual(out.get("remarks"), ("is", "not set"))    # a real Sales Invoice column
			self.assertNotIn("no_such_column_xyz", out)               # column-guarded, never raises
			self.assertTrue(channel_invoice.is_fulfilled_elsewhere(frappe._dict(remarks="settled elsewhere")))
			self.assertFalse(channel_invoice.is_fulfilled_elsewhere(frappe._dict(remarks="")))
		# a channel mirror is fulfilled elsewhere regardless of the hook
		self.assertTrue(channel_invoice.is_fulfilled_elsewhere(frappe._dict(woo_online_order="WOO-1")))

	def test_dealer_dispositions_are_not_auto_booked_for_a_claimed_invoice(self):
		"""create_dealer_dispositions defers to whoever fulfilled the invoice —
		a second booking would double-push the gun to FastBound."""
		from ffl_core.compliance import dealer_transfer
		doc = frappe._dict(doctype="Sales Invoice", remarks="ext", update_stock=1)
		with hooks("gunshop_pending_queue_exclusion_fields", ["remarks"]), \
				patch.object(dealer_transfer, "_moves_stock", side_effect=AssertionError("looked past the claim")):
			dealer_transfer.create_dealer_dispositions(doc)

	def test_counter_order_marker_honours_the_hook(self):
		from ffl_core.api import manual_order
		doc = frappe._dict(remarks="ext", ffl_destination_ffl="1-23-456-07-8X-99999", pos_profile="")
		with hooks("gunshop_pending_queue_exclusion_fields", ["remarks"]):
			self.assertFalse(manual_order._is_counter_order(doc))
		with hooks("gunshop_pending_queue_exclusion_fields", []):
			self.assertTrue(manual_order._is_counter_order(doc))


# --- gunshop_disposition_stock_handled --------------------------------------------
class TestDispositionStockHandled(FrappeTestCase):
	def _doc(self):
		return frappe._dict(name="D-TEST", disposition_type="Dealer Transfer", stock_entry="")

	def test_true_stops_the_auto_issue(self):
		from ffl_core.compliance.disposition_stock import _should_issue_stock
		with hooks("gunshop_disposition_stock_handled", [f"{HERE}.stock_handled_yes"]):
			self.assertFalse(_should_issue_stock(self._doc()))

	def test_false_falls_through_to_the_type_rule(self):
		from ffl_core.compliance.disposition_stock import _should_issue_stock
		with hooks("gunshop_disposition_stock_handled", [f"{HERE}.stock_handled_no"]):
			self.assertTrue(_should_issue_stock(self._doc()))
			self.assertFalse(_should_issue_stock(frappe._dict(disposition_type="Retail Sale", stock_entry="")))

	def test_failing_provider_fails_closed(self):
		from ffl_core.compliance.disposition_stock import _should_issue_stock
		with hooks("gunshop_disposition_stock_handled", [f"{HERE}.stock_handled_boom"]):
			self.assertFalse(_should_issue_stock(self._doc()))
		self.assertTrue(_error_logged("gunshop_disposition_stock_handled failed"))


# --- gunshop_website_user_home_page ------------------------------------------------
class TestWebsiteUserHomePage(FrappeTestCase):
	WEB_USER = "hook-test-website-user@example.com"

	def setUp(self):
		super().setUp()
		if not frappe.db.exists("User", self.WEB_USER):
			frappe.get_doc({"doctype": "User", "email": self.WEB_USER, "first_name": "Hook",
				"user_type": "Website User", "send_welcome_email": 0}).insert(ignore_permissions=True)

	def test_first_answer_wins_for_a_website_user(self):
		from ffl_core import get_website_user_home_page
		with hooks("gunshop_website_user_home_page", [f"{HERE}.home_none", f"{HERE}.home_portal"]):
			self.assertEqual(get_website_user_home_page(self.WEB_USER), "fake-portal")

	def test_desk_user_never_reaches_providers(self):
		from ffl_core import get_website_user_home_page
		with hooks("gunshop_website_user_home_page", [f"{HERE}.home_portal"]):
			self.assertIsNone(get_website_user_home_page("Administrator"))

	def test_failing_provider_is_skipped(self):
		from ffl_core import get_website_user_home_page
		with hooks("gunshop_website_user_home_page", [f"{HERE}.home_boom", f"{HERE}.home_portal"]):
			self.assertEqual(get_website_user_home_page(self.WEB_USER), "fake-portal")
		self.assertTrue(_error_logged("gunshop_website_user_home_page failed"))


# --- gunshop_extension_toggled ------------------------------------------------------
from ffl_core.extensions import registry  # noqa: E402
from ffl_core.tests.test_extensions import FAKE, MANIFEST, ExtensionFixture  # noqa: E402


class TestExtensionToggled(ExtensionFixture):
	def setUp(self):
		super().setUp()
		TOGGLES.clear()

	def _flip(self, enabled: int):
		doc = frappe.get_doc("POS Extension", FAKE)
		doc.enabled = enabled
		doc.save()

	def test_providers_hear_both_directions_but_not_a_no_op_save(self):
		with hooks("gunshop_extension_toggled", [f"{HERE}.on_toggled"]):
			self._flip(0)
			self._flip(0)                                             # unchanged → no call
			self._flip(1)
		self.assertEqual(TOGGLES, [(FAKE, False), (FAKE, True)])

	def test_failing_provider_does_not_undo_the_switch(self):
		with hooks("gunshop_extension_toggled", [f"{HERE}.toggled_boom", f"{HERE}.on_toggled"]):
			self._flip(0)
		self.assertEqual(frappe.db.get_value("POS Extension", FAKE, "enabled"), 0)
		self.assertEqual(TOGGLES, [(FAKE, False)])                    # the next provider still ran
		self.assertTrue(_error_logged("gunshop_extension_toggled failed"))

	def test_a_compatibility_flip_is_heard_once_each_way(self):
		"""The effective switch changed without anyone touching ``enabled``."""
		with hooks("gunshop_extension_toggled", [f"{HERE}.on_toggled"]):
			self.manifest = {**MANIFEST, "requires_core": ">=9.0"}
			registry.sync()
			registry.sync()                                           # nothing changed → no second call
			self.manifest = MANIFEST
			registry.sync()
		self.assertEqual(TOGGLES, [(FAKE, False), (FAKE, True)])
		self.assertEqual(frappe.db.get_value("POS Extension", FAKE, "enabled"), 1)

	def test_install_and_uninstall_are_not_toggles(self):
		with hooks("gunshop_extension_toggled", [f"{HERE}.on_toggled"]):
			self.installed_here = False
			registry.sync()
			self.installed_here = True
			registry.sync()
		self.assertEqual(TOGGLES, [])

	def test_switching_an_extension_that_is_not_effective_is_not_a_toggle(self):
		self.manifest = {**MANIFEST, "requires_core": ">=9.0"}
		registry.sync()
		with hooks("gunshop_extension_toggled", [f"{HERE}.on_toggled"]):
			self._flip(0)
		self.assertEqual(TOGGLES, [])


# --- gunshop_crm_mail_screen ----------------------------------------------------------
from ffl_core.crm import mail as crm_mail  # noqa: E402
from ffl_core.tests.crm_support import CRMTestCase  # noqa: E402
from ffl_core.tests.test_crm_mail import TEAM, _inbox, _leads, _receive, _settings  # noqa: E402


class TestCrmMailScreen(CRMTestCase):
	"""Team mail from someone new waits for a background job that asks the providers. The mail comes in
	through Communication insert (the pull's own hooks); the queue is captured and the job run by hand."""

	def setUp(self):
		super().setUp()
		_inbox()
		_settings(blocked_sender_domains="", own_domains="store.test",
			team_addresses=[{"address": TEAM, "address_type": "Group"}], primary_team_address="")
		SCREENED.clear()
		self.queued: list[dict] = []
		self._enqueue_patch = patch.object(frappe, "enqueue",
			side_effect=lambda method, **kw: self.queued.append({"method": method, **kw}))
		self._enqueue_patch.start()
		self.addCleanup(self._enqueue_patch.stop)

	def tearDown(self):
		super().tearDown()
		frappe.clear_document_cache(crm_mail.SETTINGS, crm_mail.SETTINGS)

	def _comm(self, sender="jane@buyer.test", **fields):
		fields.setdefault("content", "<p>Do you have <b>it</b>?</p>")
		return _receive(sender, "Jane Public", recipients=TEAM, **fields)

	def _run_job(self, comm):
		crm_mail.screen_new_sender(comm.name)

	def _reference(self, comm):
		return frappe.db.get_value("Communication", comm.name, ["reference_doctype", "reference_name"])

	def _screen_comments(self, comm):
		return frappe.get_all("Comment", filters={"reference_doctype": "Communication", "reference_name": comm.name,
			"comment_type": "Info"}, pluck="content")

	def test_without_a_provider_the_lead_is_made_at_once(self):
		with hooks("gunshop_crm_mail_screen", []):
			comm = self._comm()
		self.assertEqual(len(_leads("jane@buyer.test")), 1)
		self.assertEqual(self._reference(comm)[0], "CRM Lead")
		self.assertFalse(self.queued)

	def test_with_a_provider_the_lead_waits_for_the_job(self):
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_customer"]):
			comm = self._comm()
		self.assertFalse(_leads("jane@buyer.test"))
		self.assertEqual(self._reference(comm), (None, None))
		[job] = self.queued
		self.assertEqual((job["method"], job["queue"], job["communication"]), ("ffl_core.crm.mail.screen_new_sender", "short", comm.name))
		self.assertTrue(job["enqueue_after_commit"])
		self.assertTrue(job["deduplicate"])
		self.assertIn(comm.name, job["job_id"])

	def test_a_customer_answer_makes_the_lead_and_shares_it(self):
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_customer"]):
			comm = self._comm()
			self._run_job(comm)
		[lead] = _leads("jane@buyer.test")
		self.assertEqual(self._reference(comm), ("CRM Lead", lead))
		self.assertTrue(frappe.db.exists("ToDo", {"reference_type": "CRM Lead", "reference_name": lead, "status": "Closed"}))
		self.assertFalse(self._screen_comments(comm))
		[mail] = SCREENED
		body = mail.pop("body")
		self.assertEqual(" ".join(body.split()), "Do you have it ?", "HTML stripped")
		self.assertEqual(mail, {"communication": comm.name, "sender": "jane@buyer.test", "sender_name": "Jane Public",
			"to": [TEAM], "subject": "Do you have it?", "team_address": TEAM})

	def test_the_body_is_cut_at_4000_characters(self):
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_customer"]):
			comm = self._comm(content="<p>" + "x" * 9000 + "</p>")
			self._run_job(comm)
		self.assertEqual(len(SCREENED[0]["body"]), 4000)

	def test_a_not_a_customer_answer_makes_other_mail_with_the_reason(self):
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_not_customer", f"{HERE}.screen_customer"]):
			comm = self._comm("pkginfo@carrier.test")
			self._run_job(comm)
		[lead] = _leads("pkginfo@carrier.test")
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, ["status", crm_mail.CATEGORY_FIELD]),
			(crm_mail.OTHER_MAIL, "Notification"))
		self.assertEqual(self._reference(comm), ("CRM Lead", lead))
		self.assertEqual(self._screen_comments(comm), ["Not a customer inquiry (notification): Carrier delivery notice."])
		self.assertEqual(len(SCREENED), 1, "the first no settles it")
		# The same sender writing again goes on that lead, unasked.
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_not_customer"]):
			again = self._comm("pkginfo@carrier.test")
		self.assertEqual(self._reference(again), ("CRM Lead", lead))
		self.assertEqual(len(self.queued), 1)

	def test_no_opinion_makes_the_lead(self):
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_no_opinion"]):
			comm = self._comm()
			self._run_job(comm)
		self.assertEqual(len(_leads("jane@buyer.test")), 1)
		self.assertEqual(len(SCREENED), 1)
		self.assertFalse(self._screen_comments(comm))

	def test_a_failing_provider_is_logged_and_the_next_one_still_counts(self):
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_boom", f"{HERE}.screen_not_customer"]):
			comm = self._comm()
			self._run_job(comm)
		self.assertTrue(_error_logged("gunshop_crm_mail_screen provider failed"))
		self.assertEqual(frappe.db.get_value("CRM Lead", _leads("jane@buyer.test")[0], "status"), crm_mail.OTHER_MAIL,
			"the healthy provider's no stands")
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_boom"]):
			comm = self._comm("other@buyer.test")
			self._run_job(comm)
		self.assertEqual(len(_leads("other@buyer.test")), 1, "every provider failed: the lead is made")

	def test_a_sender_who_became_a_lead_meanwhile_is_linked_without_asking(self):
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_not_customer"]):
			comm = self._comm()
			lead = crm_mail._new_lead("jane@buyer.test", "Jane Public")
			self._run_job(comm)
		self.assertEqual(self._reference(comm), ("CRM Lead", lead))
		self.assertFalse(SCREENED)
		self.assertEqual(_leads("jane@buyer.test"), [lead])

	def test_a_sender_who_became_a_customer_meanwhile_goes_on_their_deal(self):
		from ffl_core.tests.test_crm_mail import _customer_with_contact

		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_not_customer"]):
			comm = self._comm()
			_customer_with_contact("jane@buyer.test")
			self._run_job(comm)
		self.assertEqual(self._reference(comm)[0], "CRM Deal")
		self.assertFalse(SCREENED)

	def test_a_known_lead_or_customer_is_never_screened(self):
		lead = crm_mail._new_lead("known@buyer.test", "Known Lead")
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_not_customer"]):
			comm = self._comm("known@buyer.test")
		self.assertEqual(self._reference(comm), ("CRM Lead", lead))
		self.assertFalse(self.queued)

	def test_the_job_is_harmless_when_the_mail_is_gone_or_already_linked(self):
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_customer"]):
			comm = self._comm()
			self._run_job(comm)
			self._run_job(comm)   # a second run (a retry) adds nothing
			crm_mail.screen_new_sender("no-such-communication")
		self.assertEqual(len(_leads("jane@buyer.test")), 1)
		self.assertEqual(len(SCREENED), 1)

	def test_a_queue_that_is_down_falls_back_to_the_lead(self):
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_not_customer"]), \
				patch.object(frappe, "enqueue", side_effect=ConnectionError("redis down")):
			comm = self._comm()
		self.assertEqual(len(_leads("jane@buyer.test")), 1)
		self.assertEqual(self._reference(comm)[0], "CRM Lead")

	def test_a_lock_that_times_out_still_makes_the_lead(self):
		from frappe.utils.file_lock import LockTimeoutError

		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_not_customer"]):
			comm = self._comm()
			with patch.object(crm_mail, "filelock", side_effect=LockTimeoutError("held")):
				self._run_job(comm)
		self.assertEqual(len(_leads("jane@buyer.test")), 1, "the lead, not nothing")
		self.assertEqual(self._reference(comm)[0], "CRM Lead")
		self.assertFalse(SCREENED, "no provider was asked")
		self.assertTrue(_error_logged("Mail screening failed"))

	def test_any_other_job_failure_is_logged_and_still_makes_the_lead(self):
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_not_customer"]):
			comm = self._comm()
			with patch.object(crm_mail, "_screen", side_effect=RuntimeError("database gone")):
				self._run_job(comm)
		self.assertEqual(len(_leads("jane@buyer.test")), 1)
		self.assertTrue(_error_logged("Mail screening failed"))

	def _flush_real_queue(self, queue):
		"""What a commit and a worker do: run the after-commit callbacks (the real frappe.enqueue puts the
		job on the queue), then run each job it queued by its method string and kwargs."""
		self.assertFalse(queue.enqueue_call.called, "nothing is queued before the mail commits")
		frappe.db.after_commit.run()
		for call in queue.enqueue_call.call_args_list:
			job = call.kwargs["kwargs"]
			frappe.call(job["method"], **job["kwargs"])
		return queue.enqueue_call.call_args_list

	def _real_queue(self):
		self._enqueue_patch.stop()   # frappe.enqueue itself runs; only the Redis queue is replaced
		queue = MagicMock(count=0)
		frappe.db.after_commit.reset()
		patcher = patch("frappe.utils.background_jobs.get_queue", return_value=queue)
		patcher.start()
		self.addCleanup(patcher.stop)
		return queue

	def test_through_the_real_enqueue_a_customer_mail_becomes_a_lead(self):
		queue = self._real_queue()
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_customer"]):
			comm = self._comm()
			self.assertFalse(_leads("jane@buyer.test"))
			calls = self._flush_real_queue(queue)
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0].kwargs["kwargs"]["method"], "ffl_core.crm.mail.screen_new_sender")
		self.assertEqual(len(_leads("jane@buyer.test")), 1)
		self.assertEqual(self._reference(comm)[0], "CRM Lead")

	def test_through_the_real_enqueue_a_not_a_customer_mail_gets_the_comment(self):
		queue = self._real_queue()
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_not_customer"]):
			comm = self._comm("pkginfo@carrier.test")
			self._flush_real_queue(queue)
		self.assertEqual(frappe.db.get_value("CRM Lead", _leads("pkginfo@carrier.test")[0], "status"), crm_mail.OTHER_MAIL)
		self.assertEqual(self._screen_comments(comm), ["Not a customer inquiry (notification): Carrier delivery notice."])

	# --- Make lead on a mail screened before other mail: stored, unlinked, with the note ----
	def _screened_out(self, sender="pkginfo@carrier.test"):
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_not_customer"]):
			comm = self._comm(sender)
		comm.add_comment("Info", crm_mail.NOT_A_CUSTOMER + " (notification): Carrier delivery notice.")
		return comm

	def _make_lead(self, comm, as_user="Administrator"):
		from ffl_core.api.crm import make_lead_from_mail

		frappe.set_user(as_user)
		try:
			return make_lead_from_mail(comm.name)
		finally:
			frappe.set_user("Administrator")

	def test_make_lead_files_a_screened_out_mail_and_shares_it(self):
		from ffl_core.tests.crm_support import CLERK

		comm = self._screened_out()
		self.assertTrue(crm_mail.screened_out(comm))
		doc = frappe.get_doc("Communication", comm.name)
		doc.run_method("onload")
		self.assertTrue(doc.get_onload("ffl_screened_out"), "the form shows the button")
		result = self._make_lead(comm, CLERK)
		[lead] = _leads("pkginfo@carrier.test")
		self.assertEqual(result, {"doctype": "CRM Lead", "name": lead})
		self.assertEqual(self._reference(comm), ("CRM Lead", lead))
		self.assertTrue(frappe.db.exists("ToDo", {"reference_type": "CRM Lead", "reference_name": lead, "status": "Closed"}))
		self.assertFalse(crm_mail.screened_out(frappe.get_doc("Communication", comm.name)), "linked: the button goes")
		self.assertFalse(SCREENED[1:], "the provider is not asked again")

	def test_make_lead_refuses_a_linked_mail_and_a_mail_without_the_note(self):
		linked = self._screened_out()
		self._make_lead(linked)
		with self.assertRaisesRegex(frappe.ValidationError, "judged not to be from a customer"):
			self._make_lead(linked)   # linked now
		with hooks("gunshop_crm_mail_screen", []):
			plain = self._comm("someone@buyer.test")   # got its lead at once: never screened out
		with self.assertRaisesRegex(frappe.ValidationError, "judged not to be from a customer"):
			self._make_lead(plain)
		with hooks("gunshop_crm_mail_screen", [f"{HERE}.screen_customer"]):
			waiting = self._comm("waiting@buyer.test")   # queued, no verdict yet
		with self.assertRaisesRegex(frappe.ValidationError, "judged not to be from a customer"):
			self._make_lead(waiting)
		self.assertFalse(_leads("waiting@buyer.test"))
		with self.assertRaises(frappe.DoesNotExistError):
			from ffl_core.api.crm import make_lead_from_mail
			make_lead_from_mail("no-such-communication")

	def test_make_lead_is_for_crm_staff_only(self):
		from ffl_core.tests.crm_support import user

		comm = self._screened_out()
		with self.assertRaises(frappe.PermissionError):
			self._make_lead(comm, user("crm-stock-user@example.com", "Stock User"))
		self.assertFalse(_leads("pkginfo@carrier.test"))

	def test_make_lead_goes_on_the_deal_of_a_sender_who_became_a_customer(self):
		from ffl_core.tests.test_crm_mail import _customer_with_contact

		comm = self._screened_out("later.buyer@buyer.test")
		_customer_with_contact("later.buyer@buyer.test")
		result = self._make_lead(comm)
		self.assertEqual(result["doctype"], "CRM Deal")
		self.assertFalse(_leads("later.buyer@buyer.test"))
