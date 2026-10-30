"""The counter gate: what it refuses, and — mostly — what it must NOT refuse.

The refusal is one line of code and the rest of the file is about the seven ways
it could refuse something it shouldn't. That ratio is the point. A gate that
blocks a sale when GunBroker is slow, or when a background job consolidates the
day's invoices, is a gate somebody switches off within a week, and then nothing
stops the double sale it was written for.

So: it blocks on GunBroker saying, in a reply we parsed, that the listing has a
buyer. It also blocks on our own `gb_sold_unknown` flag, without sending
anything — that one is local and an outage cannot argue with it. Everything
else warns.

Every GunBroker answer here is a real `requests.Response` built from a body the
sandbox actually returned, so the client's own content-type check, JSON parse
and enum normalisation all run. A mock returning a dict would skip exactly the
branches that decide whether a 404 means "no such listing" or "bad URL".
"""
from __future__ import annotations

import time
from unittest.mock import patch

import frappe
from frappe import _
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import doc_events
from ffl_integrations.gunbroker.client import GunBrokerClient, GunBrokerConfig
from ffl_integrations.tests import gunbroker_fixtures as fx

ITEM_A = "16087230"
ITEM_B = "16087231"
ITEM_C = "16087232"


def _config(timeout) -> GunBrokerConfig:
	return GunBrokerConfig(
		username="seller", password="secret", dev_key="sandbox-key",
		base_url="https://api.sandbox.gunbroker.com/v1", sandbox=True,
		timeout=timeout)


class _FakeCache:
	"""A store that lives for one test, for THIS gate's probe keys only.

	The real cache is shared across the bench, and a 3-second entry surviving
	into the next test would make a probe count depend on how fast the suite
	runs. See :meth:`_GateCase._isolate_probe_cache` for why only the probe keys
	come here and everything else goes through to the real cache.
	"""

	def __init__(self):
		self.store: dict = {}

	def get_value(self, key):
		return self.store.get(key)

	def set_value(self, key, value, expires_in_sec=None):
		self.store[key] = value

	def delete_value(self, key):
		self.store.pop(key, None)


class TheGateCanStillSpeak(FrappeTestCase):
	"""A guard on the test scaffolding itself, not on the gate.

	Three assertions in this file check that a refusal names the gun or the
	listing. They were green for the whole of PR-2 while asserting against
	`<MagicMock name='cache.hget().get().format().decode()'>`, because the
	scaffolding had replaced `frappe.cache` wholesale and frappe reads its
	translations out of the cache. A stub that silently disables translation
	makes every message-content assertion in the file meaningless, so the
	property is pinned here rather than trusted.
	"""

	def test_translation_still_returns_a_real_string(self):
		case = _GateCase("run")
		case.setUp()
		try:
			rendered = _("GunBroker item {0}").format(ITEM_A)
			self.assertIsInstance(rendered, str,
				"_() went through a mock — every message assertion here is void")
			self.assertIn(ITEM_A, rendered)
		finally:
			case.doCleanups()

	def test_the_whole_cache_is_not_replaced(self):
		"""The narrower statement of the same thing: `frappe.cache` is left
		alone, so anything else reading it (translations included) is unaffected
		by this file's isolation of the probe keys."""
		before = frappe.cache
		case = _GateCase("run")
		case.setUp()
		try:
			self.assertIs(frappe.cache, before)
		finally:
			case.doCleanups()


class _GateCase(FrappeTestCase):
	def setUp(self):
		self.rows: dict[str, dict] = {}
		self.msgprints: list[str] = []
		self.timeouts: list = []
		self.session = None

		frappe.local._gb_probe_cache = {}
		self.cache = _FakeCache()
		self._isolate_probe_cache()
		self._patch_object(doc_events, "_skip", return_value=False)
		self._patch_object(doc_events, "_settings",
			side_effect=lambda: fx.settings_stub(probe_timeout_seconds=3))
		self._patch("frappe.msgprint",
			side_effect=lambda msg, **kw: self.msgprints.append(str(msg)))
		self._patch_object(doc_events, "safe_log_error")
		self._patch("frappe.db.get_value", side_effect=self._get_value)
		self._patch_object(doc_events, "serials_for_line",
			side_effect=lambda row: [row["serial_no"]] if row.get("serial_no") else [])

	def _patch(self, target, **kw):
		patcher = patch(target, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _patch_object(self, target, attr, **kw):
		patcher = patch.object(target, attr, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _isolate_probe_cache(self):
		"""Answer THIS gate's probe keys from a per-test store, and let every
		other key through to the real cache.

		The obvious stub is patching `frappe.cache` wholesale with a fake, and
		that is what stood here until QA ran this file on a bench. It replaces
		the entire cache, and frappe's own translation layer reads its table out
		of that same cache -- so every `_("... {0} ...").format(item_id)` in the
		code under test came back a MagicMock, and the three assertions that
		check the refusal NAMES THE GUN were passing against a mock's repr:

		    AssertionError: '16087230' not found in
		    "<MagicMock name='cache.hget().get().format().decode()' ...>"

		Telling the person at the counter which gun was stopped is the whole
		usability half, so those were the assertions that mattered.
		docs/testing.md rule 4: answer only what is under test, let the rest
		through.

		Patched on the CLASS rather than on one instance, so it holds however
		`frappe.cache()` hands the wrapper out; `frappe.cache` itself is left
		alone and stays whatever this Frappe makes it.
		"""
		cache = frappe.cache() if callable(frappe.cache) else frappe.cache
		klass = type(cache)
		mine, prefix = self.cache, doc_events.PROBE_CACHE_PREFIX
		originals = {name: getattr(klass, name)
			for name in ("get_value", "set_value", "delete_value")}

		def _ours(key) -> bool:
			return str(key).startswith(prefix)

		def get_value(_self, key, *a, **kw):
			if _ours(key):
				return mine.get_value(key)
			return originals["get_value"](_self, key, *a, **kw)

		def set_value(_self, key, value=None, *a, **kw):
			if _ours(key):
				return mine.set_value(key, value)
			return originals["set_value"](_self, key, value, *a, **kw)

		def delete_value(_self, key, *a, **kw):
			if _ours(key):
				return mine.delete_value(key)
			return originals["delete_value"](_self, key, *a, **kw)

		for name, fn in (("get_value", get_value), ("set_value", set_value),
				("delete_value", delete_value)):
			self._patch_object(klass, name, new=fn)

	def _get_value(self, doctype, name, field=None, **_kw):
		row = self.rows.get(name) or {}
		if isinstance(field, list):
			return frappe._dict({f: row.get(f) for f in field}) if row else None
		return row.get(field)

	def _serial(self, serial_no: str, **row):
		base = {"item_code": "COLT", "gb_item_id": "", "gb_sold_unknown": 0}
		base.update(row)
		self.rows[serial_no] = base

	def _invoice(self, *serials, **flags):
		doc = frappe._dict({
			"doctype": "Sales Invoice", "is_consolidated": 0, "is_return": 0,
			"items": [frappe._dict({"serial_no": s}) for s in serials],
		})
		doc.update(flags)
		return doc

	def _gunbroker(self, script, *, token: str = "tok"):
		"""Answer GunBroker from `script`, recording the timeout each call got."""
		self.session = fx.RecordingSession(script)

		def _fast(*, timeout):
			self.timeouts.append(timeout)
			return GunBrokerClient(_config(timeout), session=self.session)

		self._patch_object(GunBrokerClient, "from_settings_fast", side_effect=_fast)
		self._patch_object(GunBrokerClient, "cached_token", return_value=token)
		self._patch_object(GunBrokerClient, "_store_token")
		return self.session


class ItRefusesASaleGunBrokerHasAlreadyMade(_GateCase):
	def test_each_of_the_four_sold_signals_blocks_on_its_own(self):
		"""The union, one candidate at a time.

		The union is wider than it needs to be because nobody has read back a
		fixed-price listing AFTER a Buy Now — the sandbox has no second buyer. Any
		one of the four counting is deliberate: a false positive costs one refused
		sale and a look at GunBroker; a false negative sells a gun twice.
		"""
		variants = (
			("hasWinningBid", {"has_winning_bid": True}),
			("buyer", {"buyer": {"userID": 42}}),
			("bids", {"bids": 1}),
			("inactive+zero-qty", {"is_active": False, "quantity": 0}),
		)
		for label, kwargs in variants:
			with self.subTest(signal=label):
				frappe.local._gb_probe_cache = {}
				self.cache.store.clear()
				self._serial("SN-1", gb_item_id=ITEM_A)
				self._gunbroker([
					(("GET", f"/Items/{ITEM_A}"),
						fx.make_response(fx.item_detail(ITEM_A, **kwargs))),
				])
				with self.assertRaises(frappe.ValidationError) as caught:
					doc_events.block_sale_of_sold_gb_listing(self._invoice("SN-1"))
				self.assertIn(ITEM_A, str(caught.exception))

	def test_a_bid_on_a_live_auction_blocks_as_promised(self):
		"""Not "already sold" — no order exists — but the gun is spoken for."""
		self._serial("SN-1", gb_item_id=ITEM_A)
		self._gunbroker([
			(("GET", f"/Items/{ITEM_A}"), fx.make_response(
				fx.item_detail(ITEM_A, is_fixed_price=False, bids=1))),
		])
		with self.assertRaises(frappe.ValidationError) as caught:
			doc_events.block_sale_of_sold_gb_listing(self._invoice("SN-1"))
		self.assertIn("promised", str(caught.exception))
		self.assertIn(ITEM_A, str(caught.exception))

	def test_the_local_bid_lock_blocks_without_sending_anything(self):
		self._serial("SN-1", gb_item_id=ITEM_A, gb_bid_locked=1)
		session = self._gunbroker([])
		with self.assertRaises(frappe.ValidationError) as caught:
			doc_events.block_sale_of_sold_gb_listing(self._invoice("SN-1"))
		self.assertIn("promised", str(caught.exception))
		self.assertEqual(session.calls, [], "a local flag must cost no HTTP call")

	def test_the_local_doubt_flag_blocks_without_sending_anything(self):
		"""`gb_sold_unknown` is fail-closed and offline, on purpose.

		It is set when GunBroker stopped serving a listing and the same SKU turned
		up in its sold list. The order has not arrived, so nothing can be asked —
		which is exactly why this refusal must not depend on asking.
		"""
		self._serial("SN-1", gb_item_id=ITEM_A, gb_sold_unknown=1)
		session = self._gunbroker([])
		with self.assertRaises(frappe.ValidationError):
			doc_events.block_sale_of_sold_gb_listing(self._invoice("SN-1"))
		self.assertEqual(session.calls, [], "a local flag must cost no HTTP call")


class ItLetsThroughEverythingItCannotProve(_GateCase):
	def test_a_missing_listing_warns_and_does_not_block(self):
		"""A JSON 404 says the listing is gone, not that it was bought.

		GunBroker stops serving old items on its own schedule. Blocking on that
		would hand it the power to close the register.
		"""
		self._serial("SN-1", gb_item_id=ITEM_A)
		self._gunbroker([(("GET", f"/Items/{ITEM_A}"), fx.json_404())])
		doc_events.block_sale_of_sold_gb_listing(self._invoice("SN-1"))
		self.assertTrue(self.msgprints)
		self.assertIn(ITEM_A, self.msgprints[0])

	def test_an_inactive_unsold_listing_warns_rather_than_passing_silently(self):
		"""The relist window. `isActive` false with no sale is ambiguous, and the
		dangerous reading is the reassuring one.

		With auto-relist on a thirty-day listing, every unsold gun passes through
		this shape once a month: the old item is inactive and its replacement is
		not visible yet. Reading that as "confirmed not sold" would let the
		counter sell a gun that is live on GunBroker under a number POS has not
		seen. It does not block — the sale is probably fine — but it says so.
		"""
		self._serial("SN-1", gb_item_id=ITEM_A)
		self._gunbroker([
			(("GET", f"/Items/{ITEM_A}"),
				fx.make_response(fx.item_detail(ITEM_A, is_active=False, quantity=1))),
		])
		doc_events.block_sale_of_sold_gb_listing(self._invoice("SN-1"))
		self.assertTrue(self.msgprints, "an ambiguous answer must be visible")

	def test_a_timeout_warns_and_does_not_block(self):
		import requests

		self._serial("SN-1", gb_item_id=ITEM_A)
		self._gunbroker([
			(("GET", f"/Items/{ITEM_A}"),
				lambda *_a: (_ for _ in ()).throw(requests.Timeout("slow"))),
		])
		doc_events.block_sale_of_sold_gb_listing(self._invoice("SN-1"))
		self.assertTrue(self.msgprints)

	def test_no_cached_session_answers_unknown_instead_of_logging_in(self):
		"""A login plus a read is most of the budget. It warms one for next time."""
		self._serial("SN-1", gb_item_id=ITEM_A)
		session = self._gunbroker([], token="")
		warm = self._patch_object(doc_events, "_warm_token")
		doc_events.block_sale_of_sold_gb_listing(self._invoice("SN-1"))
		self.assertEqual(session.calls, [])
		self.assertTrue(warm.called)
		self.assertTrue(self.msgprints)


class ItStaysOutOfTheWayOfBackgroundWork(_GateCase):
	def test_a_consolidated_invoice_is_exempt_before_anything_else(self):
		"""Consolidation runs as a background job long after the customer left.

		A throw here fails the job, not a sale — the day goes unconsolidated and
		somebody finds out much later. Every compliance hook in ffl_core carries
		this same first line for the same reason.
		"""
		self._serial("SN-1", gb_item_id=ITEM_A, gb_sold_unknown=1)
		session = self._gunbroker([])
		doc_events.block_sale_of_sold_gb_listing(
			self._invoice("SN-1", is_consolidated=1))
		self.assertEqual(session.calls, [])
		self.assertEqual(self.msgprints, [])

	def test_a_pos_invoices_consolidated_link_is_exempt_too(self):
		"""POS Invoice records the same fact under a different field name."""
		self._serial("SN-1", gb_item_id=ITEM_A, gb_sold_unknown=1)
		self._gunbroker([])
		doc_events.block_sale_of_sold_gb_listing(
			self._invoice("SN-1", consolidated_invoice="ACC-SINV-0001"))
		self.assertEqual(self.msgprints, [])

	def test_a_return_is_exempt(self):
		"""A return puts the gun BACK. Refusing it protects nobody."""
		self._serial("SN-1", gb_item_id=ITEM_A, gb_sold_unknown=1)
		session = self._gunbroker([])
		doc_events.block_sale_of_sold_gb_listing(self._invoice("SN-1", is_return=1))
		self.assertEqual(session.calls, [])

	def test_a_gunbroker_orders_own_invoice_is_exempt(self):
		"""The revenue invoice raised at Dispose sells the gun to the buyer GunBroker
		reports. Probing it finds its own listing sold and would strand the order
		with no invoice."""
		self._serial("SN-1", gb_item_id=ITEM_A)
		session = self._gunbroker([])
		doc_events.block_sale_of_sold_gb_listing(
			self._invoice("SN-1", gunbroker_order="GB-ORD-0001"))
		self.assertEqual(session.calls, [])
		self.assertEqual(self.msgprints, [])


class ItKeepsToItsTimeBudget(_GateCase):
	def test_one_request_per_distinct_listing_however_many_lines(self):
		"""Three guns, four lines — one of them twice. Three requests.

		The cache de-duplicates the same LISTING, not "the guns on this ticket":
		three different guns are three different questions and each one is worth
		asking.
		"""
		for serial, item in (("SN-1", ITEM_A), ("SN-2", ITEM_B), ("SN-3", ITEM_C)):
			self._serial(serial, gb_item_id=item)
		session = self._gunbroker([
			(("GET", f"/Items/{item}"), fx.make_response(fx.item_detail(item)), True)
			for item in (ITEM_A, ITEM_B, ITEM_C)
		])
		doc_events.block_sale_of_sold_gb_listing(
			self._invoice("SN-1", "SN-2", "SN-1", "SN-3"))
		self.assertEqual(session.count("GET", "/Items/"), 3)

	def test_the_budget_belongs_to_the_ticket_not_to_each_gun(self):
		"""Three guns, a three-second budget, two seconds a gun: two go out.

		The mock sleeps for whatever timeout the client actually hands it, so what
		is measured is the implementation's budget arithmetic rather than the
		mock's own idea of slow. The second gun must get a read timeout of one
		second or less — that is the budget shrinking, which is the whole
		mechanism.

		The third gun is not asked about and the sale is NOT blocked: running out
		of time is an absence of evidence.
		"""
		for serial, item in (("SN-1", ITEM_A), ("SN-2", ITEM_B), ("SN-3", ITEM_C)):
			self._serial(serial, gb_item_id=item)

		def _slow(_method, url, kwargs):
			timeout = kwargs.get("timeout")
			read = timeout[1] if isinstance(timeout, tuple) else timeout
			time.sleep(min(2, read))
			item = url.rsplit("/", 1)[-1]
			return fx.make_response(fx.item_detail(item))

		session = self._gunbroker([(("GET", "/Items/"), _slow, True)])
		started = time.monotonic()
		doc_events.block_sale_of_sold_gb_listing(
			self._invoice("SN-1", "SN-2", "SN-3"))
		elapsed = time.monotonic() - started

		self.assertEqual(session.count("GET", "/Items/"), 2)
		self.assertLessEqual(elapsed, 5, "the ticket budget was not enforced")
		second = self.timeouts[1]
		read = second[1] if isinstance(second, tuple) else second
		self.assertLessEqual(read, 1.0,
			"the second probe must inherit what is LEFT of the budget")
		self.assertTrue(self.msgprints)
		self.assertIn("SN-3", self.msgprints[0])

	def test_the_timeout_is_a_pair_so_the_budget_is_the_real_ceiling(self):
		"""`requests` applies a single timeout to EACH phase, so `timeout=3` can
		cost close to six seconds. A ticket budget written that way is not a
		budget. The connect half is capped at a second; the read half gets what
		is left."""
		self._serial("SN-1", gb_item_id=ITEM_A)
		self._gunbroker([
			(("GET", f"/Items/{ITEM_A}"), fx.make_response(fx.item_detail(ITEM_A))),
		])
		doc_events.block_sale_of_sold_gb_listing(self._invoice("SN-1"))
		self.assertIsInstance(self.timeouts[0], tuple)
		connect, read = self.timeouts[0]
		self.assertLessEqual(connect, doc_events.PROBE_CONNECT_TIMEOUT)
		self.assertGreater(read, 0)


class TheSoldPredicate(FrappeTestCase):
	"""The union read directly, so the union's shape is checkable without a ticket."""

	def test_an_ordinary_live_listing_is_not_sold(self):
		self.assertFalse(doc_events.gb_item_sold(fx.item_detail()))

	def test_each_candidate_alone_counts(self):
		for kwargs in ({"has_winning_bid": True}, {"buyer": {"userID": 1}},
				{"bids": 2}, {"is_active": False, "quantity": 0}):
			with self.subTest(**kwargs):
				self.assertTrue(doc_events.gb_item_sold(fx.item_detail(**kwargs)))

	def test_inactive_with_stock_left_is_not_a_sale(self):
		"""The relist window, told apart from a sale by the quantity."""
		self.assertFalse(
			doc_events.gb_item_sold(fx.item_detail(is_active=False, quantity=1)))

	def test_an_empty_body_is_not_a_sale(self):
		self.assertFalse(doc_events.gb_item_sold({}))
