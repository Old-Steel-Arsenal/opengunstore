"""Non-4473 disposition push to FastBound — payload building + enqueue guards.

FFL transfers / returns / destruction push to FastBound's bound book;
4473 / to-individual dispositions (Retail Sale, Sale, Donation, …) never do.
HTTP + frappe.enqueue are mocked — no live calls.

CreateAndCommit returns 204 (no body); the committed disposition's id lands on
the disposed item, so post_disposition re-fetches the item — the mock below
mirrors that (POST → None, GET Items/{id} → {"dispositionId": …}).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfig, FastBoundError
from ffl_integrations.fastbound import tasks


def _client() -> FastBoundClient:
	cfg = FastBoundConfig(account_num="TEST", api_key="k", base_url="https://example.invalid",
		api_version="1.0", audit_user="u", timeout=5)
	return FastBoundClient(cfg, session=MagicMock())


def _req_side_effect(disp_id="fbdisp-1"):
	"""Mimic FastBound: CreateAndCommit → 204 (None); item GET → dispositionId."""
	def fn(method, path, **kw):
		if method == "POST" and path == "Dispositions/CreateAndCommit":
			return None
		if method == "GET" and path.startswith("Items/"):
			return {"dispositionId": disp_id}
		return None
	return fn


def _disp(**over):
	base = dict(name="DISP-T-1", serial="DSN-1", disposition_date="2026-05-27T10:00:00",
		notes="", disposition_type="Dealer Transfer", destination_ffl="1-12-345-67-8K-99999",
		fastbound_id=None)
	base.update(over)
	return SimpleNamespace(**base)


def _post_body(req_mock):
	calls = [ca for ca in req_mock.call_args_list if ca.args[:2] == ("POST", "Dispositions/CreateAndCommit")]
	return calls[0].kwargs["json_body"]


class PostDisposition(FrappeTestCase):
	def test_transfer_references_existing_ffl_contact_by_id(self):
		client = _client()
		with patch.object(client, "get_item_id_by_serial", return_value="fbitem-1"), \
		     patch.object(client, "find_contact_by_ffl_number", return_value={"id": "contact-xyz"}), \
		     patch.object(client, "_request", side_effect=_req_side_effect("fbdisp-1")) as req:
			fb_id = client.post_disposition(_disp())
		self.assertEqual(fb_id, "fbdisp-1")
		body = _post_body(req)
		self.assertEqual(body["requestType"], "Regular")
		self.assertEqual(body["type"], "Dealer Transfer")
		self.assertEqual(body["items"], [{"id": "fbitem-1"}])
		self.assertEqual(body["contactId"], "contact-xyz")
		self.assertNotIn("contact", body)  # referenced existing, did not create

	def test_destruction_uses_destroyed_with_witnesses(self):
		client = _client()
		with patch.object(client, "get_item_id_by_serial", return_value="fbitem-9"), \
		     patch.object(client, "_request", side_effect=_req_side_effect("fbdisp-9")) as req:
			fb_id = client.post_disposition(_disp(disposition_type="Scrap", destination_ffl="",
				destroyed_witness1="WA", destroyed_witness2="WB"))
		self.assertEqual(fb_id, "fbdisp-9")
		body = _post_body(req)
		self.assertEqual(body["requestType"], "Destroyed")
		self.assertEqual(body["destroyed_Witness1"], "WA")
		self.assertEqual(body["destroyed_Witness2"], "WB")
		self.assertNotIn("type", body)
		self.assertNotIn("contact", body)

	def test_missing_item_in_fastbound_raises(self):
		client = _client()
		with patch.object(client, "get_item_id_by_serial", return_value=None):
			with self.assertRaises(FastBoundError):
				client.post_disposition(_disp(serial="NOPE-NOT-IN-FB"))

	def test_already_disposed_item_raises_actionable_error_without_adopting(self):
		"""An item FastBound has already disposed (e.g. a mistaken Return booked
		directly in FastBound) must surface a clear, human-actionable error naming
		the EXISTING disposition's date / type / recipient — never silently adopt
		its id, which would mis-record this transfer as that prior disposition."""
		client = _client()
		disposed_item = {
			"id": "fbitem-1", "itemNumber": "00633", "serial": "DSN-1",
			"status": {"id": 3, "name": "Disposed"},
			"dispositionType": "Return", "dispose_Date": "2026-06-15T16:03:12",
			"dispose_LicenseName": "TEST VENDOR INC",
			"dispositionId": "prior-disp-1",
		}

		def side_effect(method, path, **kw):
			if method == "POST" and path == "Dispositions/CreateAndCommit":
				raise FastBoundError(
					'POST Dispositions/CreateAndCommit → 400: '
					'{"errors":[{"message":"Item 00633 was disposed on 6/15/2026."}]}',
					status_code=400)
			if method == "GET" and path.startswith("Items/"):
				return disposed_item
			return None

		with patch.object(client, "get_item_id_by_serial", return_value="fbitem-1"), \
		     patch.object(client, "find_contact_by_ffl_number", return_value={"id": "c-1"}), \
		     patch.object(client, "_request", side_effect=side_effect):
			with self.assertRaises(FastBoundError) as ctx:
				client.post_disposition(_disp())
		msg = str(ctx.exception)
		self.assertIn("00633", msg)            # which physical item
		self.assertIn("Return", msg)           # the conflicting disposition type
		self.assertIn("TEST VENDOR INC", msg)  # who it already went to
		self.assertIn("2026-06-15", msg)       # when
		self.assertIn("DISP-T-1", msg)         # our disposition name, for the operator
		self.assertNotIn("CreateAndCommit", msg)     # friendly message, not the raw 400

	def test_other_400_is_not_masked_as_already_disposed(self):
		"""A 400 on an item that is NOT actually disposed (some other rejection,
		e.g. a bad contact) must re-raise the ORIGINAL error — the friendly
		'already disposed' path only triggers when the item really is disposed."""
		client = _client()
		active_item = {"id": "fbitem-1", "itemNumber": "00700", "serial": "DSN-1",
			"status": {"id": 1, "name": "Active"}}

		def side_effect(method, path, **kw):
			if method == "POST" and path == "Dispositions/CreateAndCommit":
				raise FastBoundError(
					'POST Dispositions/CreateAndCommit → 400: '
					'{"errors":[{"message":"Contact is invalid."}]}',
					status_code=400)
			if method == "GET" and path.startswith("Items/"):
				return active_item
			return None

		with patch.object(client, "get_item_id_by_serial", return_value="fbitem-1"), \
		     patch.object(client, "find_contact_by_ffl_number", return_value={"id": "c-1"}), \
		     patch.object(client, "_request", side_effect=side_effect):
			with self.assertRaises(FastBoundError) as ctx:
				client.post_disposition(_disp())
		msg = str(ctx.exception)
		self.assertIn("Contact is invalid", msg)   # original rejection preserved
		self.assertNotIn("already disposed", msg)


class ReacquiredInFastBound(FrappeTestCase):
	"""A gun that comes back (a returned transfer, a trip to the gunsmith) and is
	re-booked by hand in FastBound becomes a NEW item with the same serial and no
	externalId. The Serial No still points at the old, disposed item, so the next
	sale's push 400s "already disposed". When the new item is unambiguously the
	same gun coming back, dispose it and repoint the Serial No."""

	OLD = {"id": "fbitem-1", "itemNumber": "00327", "serial": "DSN-1",
		"manufacturer": "Walther", "status": {"id": 3, "name": "Disposed"},
		"dispositionType": "FFL Transfer", "dispose_Date": "2026-09-16T17:10:20",
		"dispose_LicenseName": "FIRST BUYER INC", "dispositionId": "prior-1"}
	NEW = {"id": "fbitem-2", "itemNumber": "01412", "serial": "DSN-1",
		"manufacturer": "Walther", "disposed": False, "deleted": False,
		"acquire_Date": "2026-10-02T00:00:00"}

	def _push(self, new_item):
		client = _client()
		posted: list[str] = []

		def side_effect(method, path, **kw):
			if method == "POST" and path == "Dispositions/CreateAndCommit":
				item_id = kw["json_body"]["items"][0]["id"]
				posted.append(item_id)
				if item_id == "fbitem-1":
					raise FastBoundError(
						'POST Dispositions/CreateAndCommit → 400: '
						'{"errors":[{"message":"Item 00327 was disposed on 9/16/2026."}]}',
						status_code=400)
				return None
			if method == "GET" and path == "Items":
				return {"items": [self.OLD | {"disposed": True}] + ([new_item] if new_item else [])}
			if method == "GET" and path == "Items/fbitem-1":
				return self.OLD
			if method == "GET" and path == "Items/fbitem-2":
				return {"dispositionId": "fbdisp-new"}
			return None

		with patch("ffl_integrations.fastbound.client.frappe.db", new_callable=MagicMock) as db, \
		     patch.object(client, "find_contact_by_ffl_number", return_value={"id": "c-1"}), \
		     patch.object(client, "_request", side_effect=side_effect):
			db.exists.return_value = True
			db.get_value.return_value = "fbitem-1"  # the stale pointer
			try:
				result = client.post_disposition(_disp())
			except FastBoundError as exc:
				result = exc
		return result, posted, db.set_value

	def test_the_reacquired_item_is_disposed_and_the_serial_repointed(self):
		result, posted, set_value = self._push(self.NEW)
		self.assertEqual(result, "fbdisp-new")
		self.assertEqual(posted, ["fbitem-1", "fbitem-2"])
		set_value.assert_called_once_with(
			"Serial No", "DSN-1", "fastbound_item_id", "fbitem-2", update_modified=False)

	def test_a_different_manufacturer_is_another_gun_and_is_left_alone(self):
		result, posted, set_value = self._push(self.NEW | {"manufacturer": "Ruger"})
		self.assertIsInstance(result, FastBoundError)
		self.assertTrue(result.terminal)
		self.assertIn("already disposed", str(result))
		self.assertEqual(posted, ["fbitem-1"])
		set_value.assert_not_called()

	def test_an_item_acquired_before_the_disposition_is_not_a_return(self):
		result, posted, set_value = self._push(self.NEW | {"acquire_Date": "2026-09-01T00:00:00"})
		self.assertIsInstance(result, FastBoundError)
		self.assertTrue(result.terminal)
		self.assertEqual(posted, ["fbitem-1"])
		set_value.assert_not_called()

	def test_no_in_stock_item_keeps_the_terminal_error(self):
		result, posted, set_value = self._push(None)
		self.assertIsInstance(result, FastBoundError)
		self.assertTrue(result.terminal)
		self.assertIn("FIRST BUYER INC", str(result))
		self.assertEqual(posted, ["fbitem-1"])
		set_value.assert_not_called()


class PendingDispositionConflict(FrappeTestCase):
	"""DISP-2026-02000: an abandoned FastBound 4473 wizard left an UNCOMMITTED
	disposition holding item 00843. Every later push for that gun 400'd with
	"Item 00843 has already been added to another disposition" — a message that
	names the item and not its owner, so finding the culprit took a manual hunt
	through FastBound's UI.
	"""

	HELD_ITEM = {
		"id": "fbitem-1", "itemNumber": "00843", "serial": "DSN-1",
		"status": {"id": 1, "name": "Available"},
		"dispositionId": "pending-disp-9",
	}

	def _push(self, item=None, disposition=None, *, error=None, get_raises=None,
			body=""):
		client = _client()
		item = self.HELD_ITEM if item is None else item

		posted: list[bool] = []

		def side_effect(method, path, **kw):
			if method == "POST" and path == "Dispositions/CreateAndCommit":
				posted.append(True)
				raise FastBoundError(
					error or
					'POST Dispositions/CreateAndCommit → 400: '
					'{"errors":[{"message":"Item 00843 has already been added to '
					'another disposition."}]}',
					status_code=400, body=body)
			# Only the lookups the 400 handler makes — anything the push needs on
			# the way IN keeps working, so this models a network that dies exactly
			# where the best-effort reads happen.
			if get_raises is not None and posted and method == "GET":
				raise get_raises
			if method == "GET" and path.startswith("Items/"):
				return item
			if method == "GET" and path.startswith("Dispositions/"):
				return disposition
			return None

		with patch.object(client, "get_item_id_by_serial", return_value="fbitem-1"), \
		     patch.object(client, "find_contact_by_ffl_number", return_value={"id": "c-1"}), \
		     patch.object(client, "_request", side_effect=side_effect):
			with self.assertRaises(FastBoundError) as ctx:
				client.post_disposition(_disp())
		return ctx.exception

	def test_the_error_names_the_disposition_that_is_holding_the_item(self):
		exc = self._push()
		msg = str(exc)
		self.assertIn("pending-disp-9", msg)   # WHO has it — the whole point
		self.assertIn("00843", msg)            # which physical item
		self.assertIn("DISP-T-1", msg)         # our record, for the operator

	def test_it_is_not_terminal(self):
		"""The hold is temporary by nature: delete or finish that disposition and
		this same push succeeds — which is how 00843 actually healed. Marking it
		terminal takes it out of reconcile_failed_pushes and leaves the A&D gap
		open until a human notices."""
		self.assertFalse(self._push().terminal)

	def test_a_disposed_item_still_takes_the_terminal_path(self):
		"""The neighbouring branch must keep its own verdict: a gun that has
		genuinely left the book cannot be disposed again, ever."""
		exc = self._push(item={
			"id": "fbitem-1", "itemNumber": "00633", "serial": "DSN-1",
			"status": {"id": 3, "name": "Disposed"}, "dispositionType": "Return",
			"dispose_Date": "2026-06-15T16:03:12", "dispositionId": "prior-1",
		})
		self.assertTrue(exc.terminal)
		self.assertIn("already disposed", str(exc))

	def test_the_disposition_status_is_reported_when_fastbound_gives_one(self):
		exc = self._push(disposition={"status": {"name": "Pending"}, "type": "Retail Sale"})
		self.assertIn("Pending", str(exc))

	def test_an_unreadable_disposition_still_names_its_id(self):
		"""GET /Dispositions/{id} is decoration; losing it must not cost the
		operator the one fact they need."""
		self.assertIn("pending-disp-9", str(self._push(disposition=None)))

	def test_a_DIFFERENT_400_is_not_rewritten_as_an_abandoned_4473(self):
		"""The rewrite is gated on what FastBound SAID, not on the item's state.
		A dispositionId can outlive its disposition (a dangling hold), so "the item
		carries one" is also true of guns whose 400 was about something else —
		a rejected contact, a missing required field. Rewriting those hands the
		operator advice to go delete a disposition that was never the problem, with
		the real rejection gone from the message."""
		exc = self._push(error='POST Dispositions/CreateAndCommit → 400: '
			'{"errors":[{"message":"Acquire_Date is required."}]}')
		msg = str(exc)
		self.assertIn("Acquire_Date is required", msg)
		self.assertNotIn("uncommitted disposition", msg)
		self.assertNotIn("Clear Abandoned Order", msg)

	def test_the_hold_is_recognised_when_the_sentence_fell_past_the_truncation(self):
		"""`_request` puts only the first 500 characters of the body in the message
		so an Error Log row stays readable. Matching the message therefore made the
		advice depend on where FastBound happens to put this sentence: behind a long
		validation preamble it would fall through to the raw 400, and the operator
		would lose the one pointer that names the disposition holding the gun. The
		match reads the untruncated body instead."""
		body = ('{"errors":[{"message":"' + "padding. " * 80 + '"},'
			'{"message":"Item 00843 has already been added to another '
			'disposition."}]}')
		self.assertNotIn("another disposition", body[:500])  # the trap, pinned
		exc = self._push(
			error=f"POST Dispositions/CreateAndCommit → 400: {body[:500]}", body=body)
		msg = str(exc)
		self.assertIn("uncommitted disposition", msg)  # rewritten, not raw
		self.assertIn("pending-disp-9", msg)           # ...and it names the holder

	def test_the_stored_body_is_capped(self):
		"""A FastBound Item or Contact response body carries 4473 PII — buyer name,
		date of birth, address — and this object sits one `log_error(exc.body)` away
		from an Error Log row holding it for a year, readable by anyone with the desk
		role. The cap bounds what that slip can spill while leaving room for the hold
		sentence behind any validation preamble. It is a bound, not permission: the
		rule in the docstring is that the body stays in memory."""
		from ffl_integrations.fastbound.client import _ERROR_BODY_KEPT

		exc = FastBoundError("boom", status_code=400,
			body="x" * (_ERROR_BODY_KEPT + 500))
		self.assertEqual(len(exc.body), _ERROR_BODY_KEPT)
		self.assertEqual(FastBoundError("boom").body, "")

	def test_an_error_raised_without_a_body_still_matches_on_its_message(self):
		"""Not every FastBoundError comes straight off the wire — a re-raise or a
		test double carries only a message. The fallback keeps those working."""
		exc = self._push(body="")
		self.assertIn("uncommitted disposition", str(exc))

	def test_a_transport_failure_in_the_lookup_keeps_the_original_400(self):
		"""The lookups this branch adds are best effort, and they used to catch
		only FastBoundError: a requests Timeout / SSLError raised inside them
		escaped the caller's `except FastBoundError` and surfaced INSTEAD of the
		400 — losing the one message that tells the operator what FastBound
		refused, and turning an actionable rejection into "connection reset"."""
		import requests

		exc = self._push(get_raises=requests.exceptions.Timeout("read timed out"))
		self.assertEqual(exc.status_code, 400)
		self.assertIn("another disposition", str(exc))
		self.assertFalse(exc.terminal)


class DispositionEnqueueGuard(FrappeTestCase):
	def test_skips_when_already_synced(self):
		with patch("frappe.enqueue") as enq:
			tasks.enqueue_disposition_push_on_submit(_disp(fastbound_id="fb-existing"))
		enq.assert_not_called()

	def test_skips_non_pushable_type(self):
		with patch("frappe.enqueue") as enq:
			tasks.enqueue_disposition_push_on_submit(_disp(disposition_type="Donation"))
		enq.assert_not_called()

	def test_enqueues_pushable_type(self):
		with patch("frappe.enqueue") as enq:
			tasks.enqueue_disposition_push_on_submit(_disp(disposition_type="Dealer Transfer"))
		self.assertTrue(enq.called)

	def test_cancel_deletes_pushed_non_4473_only(self):
		# A non-4473 transfer we pushed → enqueue the FastBound delete.
		with patch("frappe.enqueue") as enq:
			tasks.cancel_disposition_on_cancel(_disp(disposition_type="Dealer Transfer", fastbound_id="fb-1"))
		self.assertTrue(enq.called)
		# A 4473 Retail Sale mirror → never delete FastBound's committed record.
		with patch("frappe.enqueue") as enq:
			tasks.cancel_disposition_on_cancel(_disp(disposition_type="Retail Sale", fastbound_id="fb-2"))
		enq.assert_not_called()


if __name__ == "__main__":
	unittest.main()
