"""P3 — a 4473-class disposition is NOT
retrievable via GET /Dispositions/{id}; that endpoint 404s for is4473=true
dispositions (Error Log qvfjg7sjuc). So every 4473 lookup must be marked
is_4473=True to keep it off that endpoint.

What "is_4473=True" then DOES has since changed. It used to mean "resolve from the
event payload, there is no other source" — an assumption a past invoice
disproved: a form4473.completed payload can carry only the guns added in an
earlier pass. The 4473 items are now resolved from the other side of the relation
(GET /Items?dispositionId=), so is_4473=True means "use the bound-book lookup, not
the 404-ing disposition endpoint". The tests here still pin the routing flag; the
lookup's own behaviour is covered by FB4473AuthoritativeItemLookup in
test_fastbound_pos_4473.py.

These tests drive the _fetch_disposition_items short-circuit + the is4473 event
detector directly (no DB needed).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase


class Fetch4473Endpoint(FrappeTestCase):
	def test_fetch_disposition_items_skips_get_for_4473(self):
		"""is_4473=True must NOT construct/call the FastBound client — a 4473
		disposition 404s on /Dispositions/{id}, so we never issue that GET."""
		from ffl_integrations.fastbound import webhook as fb
		with patch.object(fb, "FastBoundClient") as mock_client:
			out = fb._fetch_disposition_items("FB-DISP-4473", is_4473=True)
		self.assertEqual(out, [])
		mock_client.from_settings.assert_not_called()

	def test_fetch_disposition_items_fetches_for_non_4473(self):
		"""Non-4473 dispositions keep the GET fallback (regression guard)."""
		from ffl_integrations.fastbound import webhook as fb
		client = MagicMock()
		client.get_disposition.return_value = {"items": [{"id": "IT-1", "serial": "S-1"}]}
		with patch.object(fb, "FastBoundClient") as mock_client:
			mock_client.from_settings.return_value = client
			out = fb._fetch_disposition_items("FB-DISP-XFER", is_4473=False)
		mock_client.from_settings.assert_called_once()
		client.get_disposition.assert_called_once_with("FB-DISP-XFER")
		self.assertEqual(out, [{"id": "IT-1", "serial": "S-1"}])

	def test_fetch_disposition_items_defaults_to_fetch(self):
		"""Default (is_4473 omitted) preserves the historical fetch behavior."""
		from ffl_integrations.fastbound import webhook as fb
		client = MagicMock()
		client.get_disposition.return_value = {"items": []}
		with patch.object(fb, "FastBoundClient") as mock_client:
			mock_client.from_settings.return_value = client
			fb._fetch_disposition_items("FB-DISP-D")
		mock_client.from_settings.assert_called_once()


class EventIs4473(FrappeTestCase):
	def test_reads_top_level_flag(self):
		from ffl_integrations.fastbound.webhook import _event_is_4473
		self.assertTrue(_event_is_4473({"is4473": True}))
		self.assertFalse(_event_is_4473({"is4473": False}))

	def test_reads_nested_disposition_flag(self):
		from ffl_integrations.fastbound.webhook import _event_is_4473
		self.assertTrue(_event_is_4473({"disposition": {"is4473": True}}))

	def test_coerces_string_true(self):
		from ffl_integrations.fastbound.webhook import _event_is_4473
		self.assertTrue(_event_is_4473({"is4473": "true"}))
		self.assertTrue(_event_is_4473({"is4473": "True"}))

	def test_absent_flag_is_false(self):
		from ffl_integrations.fastbound.webhook import _event_is_4473
		self.assertFalse(_event_is_4473({}))
		self.assertFalse(_event_is_4473(None))
		self.assertFalse(_event_is_4473({"disposition": {"id": "X"}}))


class Form4473CompletedUsesPayload(FrappeTestCase):
	"""The always-4473 form4473.completed handler must never route its lookup at
	GET /Dispositions/{id} (which would 404) — every call carries is_4473=True."""

	def test_coverage_fetch_is_marked_4473(self):
		from ffl_integrations.fastbound import webhook as fb
		data = {
			"disposition": {"id": "DISP-P3"},
			"items": [{"serial": "P3-A"}],
			"response": 2,  # Proceed
		}
		# The stub answers the fake invoice's reads only (docs/testing.md rule 4): a blanket
		# return_value also answered frappe's own Workflow lookup for Error Log (the handler
		# logs here), and the "N/A" it cached in redis broke every later log_error.
		real_get_value = fb.frappe.db.get_value

		def _get_value(doctype, *args, **kwargs):
			if doctype == "POS Invoice":
				return "N/A"
			return real_get_value(doctype, *args, **kwargs)

		with patch.object(fb, "_find_draft_invoices_for_serials",
				return_value={"INV-1": ["P3-A"]}), \
		     patch.object(fb, "_ensure_disposition_coverage"), \
		     patch.object(fb, "_fetch_disposition_serials", return_value=[]) as fetch, \
		     patch.object(fb.frappe.db, "get_value", side_effect=_get_value), \
		     patch.object(fb.frappe.db, "set_value"), \
		     patch.object(fb.frappe, "publish_realtime"):
			fb._handle_form4473_completed(data)
		# Any lookup the handler makes must pass is_4473=True so it is routed to the
		# bound-book reverse lookup instead of the 404-ing disposition endpoint.
		for call in fetch.call_args_list:
			self.assertTrue(call.kwargs.get("is_4473"),
				f"_fetch_disposition_serials must be called with is_4473=True; got {call}")
