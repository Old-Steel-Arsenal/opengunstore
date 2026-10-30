"""The hourly sweep — the only thing here that asks GunBroker what is true.

Almost every test in this file is about NOT concluding something. That is the
shape of the risk: the sweep is the one component allowed to forget a listing,
and every way of forgetting one wrongly ends with a gun that is live on
GunBroker and sellable at the counter.

Two failure modes drive the design, and both have a test that fails if the
implementation gets simpler:

**The relist window.** With auto-relist on a thirty-day listing, EVERY unsold
gun spends a few minutes each month looking exactly like an ended one: the old
item reads back inactive, its `relistedAsItemID` still reads 0, and the
replacement has not surfaced in `/ItemsSelling` yet.
An implementation that acts on one look marks a live listing ENDED, sets the
sentinel that stops any machine re-listing it, and frees the gun for the
counter. `test_an_inactive_listing_nobody_ended_needs_two_rounds` is that case.

**404 is not "never existed".** GunBroker stops serving old item numbers on its
own schedule, including for guns it has just sold. Reading that as "no listing"
puts a sold gun back on the shelf. So the 404 arm re-reads, then looks the SKU
up in both scans, and only ages out after a day of agreement.

Responses are real `requests.Response` objects built from bodies the sandbox
returned, so the client's content-type/JSON/enum handling runs for real — the
JSON-404-versus-HTML-404 split in particular, which is what separates "this
listing is gone" from "that URL was wrong".
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from ffl_integrations.gunbroker import listings, tasks
from ffl_integrations.gunbroker.client import GunBrokerClient, GunBrokerConfig
from ffl_integrations.tests import gunbroker_fixtures as fx

OLD_ID = "16087230"
NEW_ID = "16087260"
SKU = "COLT-PYTHON::SN-001"
SERIAL = "SN-001"


def _config() -> GunBrokerConfig:
	return GunBrokerConfig(
		username="seller", password="secret", dev_key="sandbox-key",
		base_url="https://api.sandbox.gunbroker.com/v1", sandbox=True, timeout=5)


class _SweepCase(FrappeTestCase):
	"""Serial No writes land in one in-memory row that `serial_state` reads back,
	so "the flag was still set after the function returned" is a real assertion
	rather than a look at an object the code is still holding."""

	def setUp(self):
		self.row = {
			"name": SERIAL, "item_code": "COLT-PYTHON", "status": "Active",
			"gb_item_id": OLD_ID, "gb_item_history": "", "gb_delisted": 0,
			"gb_end_pending": 0, "gb_push_pending": 0, "gb_push_attempted_at": None,
			"gb_price_dirty": 0, "gb_sold_unknown": 0, "gb_gone_strikes": 0,
			"gb_gone_first_at": None, "gb_reserved": 0, "gb_order": "",
			"gb_sync_error": "",
		}
		self.alerts: list[str] = []
		self.exits: list[dict] = []
		self.enqueued: list[str] = []

		def _set_value(_doctype, _name, field, value=None, **_kw):
			self.row.update(field if isinstance(field, dict) else {field: value})

		self._patch("frappe.db.set_value", side_effect=_set_value)
		self._patch("frappe.db.get_value",
			side_effect=lambda _dt, _n, f=None, **_kw: (
				frappe._dict({k: self.row.get(k) for k in f})
				if isinstance(f, list) else self.row.get(f)))
		self._patch("frappe.enqueue",
			side_effect=lambda path, **kw: self.enqueued.append(path))
		self._patch_object(listings, "serial_state",
			side_effect=lambda _sn, **_kw: frappe._dict(dict(self.row)))
		self._patch_object(tasks, "notify_admins",
			side_effect=lambda subject, message, **kw: self.alerts.append(kw["dedup_key"]))
		self._patch_object(tasks, "fire_serial_channel_exit",
			side_effect=lambda sn, **kw: self.exits.append({"serial": sn, **kw}))
		self._patch_object(tasks, "safe_log_error")
		self._patch_object(tasks, "sweep_candidates",
			side_effect=lambda *_a, **_kw: [frappe._dict(dict(self.row))])
		self._patch_object(tasks, "_sku", return_value=SKU)
		# The reverse arm is exercised in its own test; everywhere else the gun is
		# an ordinary sellable one so it cannot interfere with the assertion.
		self.shelf = {"sellable": True, "reason": None, "detail": {}}
		self._patch_object(tasks, "serial_shelf_state",
			side_effect=lambda *_a, **_kw: dict(self.shelf))
		self._patch_object(tasks, "GONE_RECHECK_SECONDS", new=0)

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

	def _run(self, script):
		session = fx.RecordingSession(script)
		client = GunBrokerClient(_config(), session=session)
		self._patch_object(GunBrokerClient, "from_settings", return_value=client)
		self._patch_object(GunBrokerClient, "cached_token", return_value="tok")
		self._patch_object(GunBrokerClient, "_store_token")
		tasks._run_sweep()
		return session

	def _page(self, rows):
		return fx.make_response(fx.page(rows))


# ---------------------------------------------------------------------------
# The 404 arm
# ---------------------------------------------------------------------------

class WhenGunBrokerAnswers404(_SweepCase):
	def _gone_twice(self, *rest):
		"""Two 404s — the arm re-reads once before believing the first one."""
		return [
			(("GET", f"/Items/{OLD_ID}"), fx.json_404()),
			(("GET", f"/Items/{OLD_ID}"), fx.json_404()),
			*rest,
		]

	def test_a_second_read_that_answers_is_treated_as_an_ordinary_answer(self):
		"""One 404 is not a fact. An ended listing reads back 200 with `isActive`
		false for a good while, so a 404 this soon is more often one bad edge node
		than a listing that has gone."""
		session = self._run([
			(("GET", f"/Items/{OLD_ID}"), fx.json_404()),
			(("GET", f"/Items/{OLD_ID}"),
				fx.make_response(fx.item_detail(OLD_ID, sku=SKU))),
		])
		self.assertEqual(self.row["gb_item_id"], OLD_ID)
		self.assertEqual(self.row["gb_gone_strikes"], 0)
		self.assertEqual(session.count("GET", f"/Items/{OLD_ID}"), 2)

	def test_a_live_listing_under_a_new_number_is_adopted_not_forgotten(self):
		"""The relist. Matched on SKU, because the number is exactly what changed."""
		self.row["gb_gone_strikes"] = 1
		self.row["gb_gone_first_at"] = add_to_date(now_datetime(), hours=-30)
		self._run(self._gone_twice(
			(("GET", "/ItemsSelling"),
				self._page([fx.selling_row(item_id=int(NEW_ID), sku=SKU)])),
		))
		self.assertEqual(self.row["gb_item_id"], NEW_ID)
		self.assertIn(OLD_ID, self.row["gb_item_history"])
		self.assertEqual(self.row["gb_gone_strikes"], 0)
		self.assertIsNone(self.row["gb_gone_first_at"],
			"adopting a live listing must clear the age as well as the count — "
			"a stale first_at is already older than the threshold")
		self.assertFalse(self.row["gb_sold_unknown"])

	def test_the_match_is_on_sku_even_when_the_row_has_no_item_number(self):
		"""A row that matches the SKU proves the listing exists, whatever else it
		is missing. Reading "no comparable item number" as "not found" would take
		the one fact the row does carry and conclude its opposite."""
		self._run(self._gone_twice(
			(("GET", "/ItemsSelling"), self._page([{"sku": SKU, "title": "Colt"}])),
		))
		self.assertEqual(self.row["gb_item_id"], OLD_ID)
		self.assertEqual(self.row["gb_gone_strikes"], 0)
		self.assertFalse(self.row["gb_delisted"])

	def test_a_row_carrying_a_zero_item_number_is_not_claimed(self):
		"""Zero is what an item number reads as before GunBroker has processed a
		relist. Writing it back would clear the pending flag and leave the gun
		looking unlisted — after which the next click posts a second listing."""
		self._run(self._gone_twice(
			(("GET", "/ItemsSelling"),
				self._page([{"sku": SKU, "itemID": 0, "title": "Colt"}])),
		))
		self.assertEqual(self.row["gb_item_id"], OLD_ID)
		self.assertFalse(self.row["gb_delisted"])

	def test_the_sold_list_flags_the_gun_and_tells_the_other_channels(self):
		"""Probably sold, order not polled in yet. Everything fails closed at once,
		and the storefronts are told NOW — the gap between "probably sold" and
		"definitely sold" is exactly where a second sale happens."""
		self._run(self._gone_twice(
			(("GET", "/ItemsSelling"), self._page([])),
			(("GET", "/ItemsSold"),
				self._page([fx.selling_row(item_id=int(OLD_ID), sku=SKU)])),
		))
		self.assertEqual(self.row["gb_sold_unknown"], 1)
		self.assertEqual(self.row["gb_item_id"], "")
		self.assertIn(OLD_ID, self.row["gb_item_history"])
		self.assertIn(f"gb_sold_unmatched_{SERIAL}", self.alerts)
		self.assertEqual(self.exits[0]["reason"], tasks.REASON_RESERVED)

	def test_neither_list_has_it_so_the_first_round_changes_nothing(self):
		"""One observation. The item number stays exactly where it is."""
		self._run(self._gone_twice(
			(("GET", "/ItemsSelling"), self._page([])),
			(("GET", "/ItemsSold"), self._page([])),
		))
		self.assertEqual(self.row["gb_item_id"], OLD_ID)
		self.assertEqual(self.row["gb_gone_strikes"], 1)
		self.assertIsNotNone(self.row["gb_gone_first_at"])
		self.assertFalse(self.row["gb_delisted"])

	def test_a_day_of_agreement_ends_it_as_ENDED_never_as_UNLISTED(self):
		self.row["gb_gone_strikes"] = 1
		self.row["gb_gone_first_at"] = add_to_date(now_datetime(), hours=-25)
		self._run(self._gone_twice(
			(("GET", "/ItemsSelling"), self._page([])),
			(("GET", "/ItemsSold"), self._page([])),
		))
		self.assertEqual(self.row["gb_item_id"], "")
		self.assertEqual(self.row["gb_delisted"], 1,
			"ENDED, not UNLISTED — UNLISTED would invite a machine to list it again")
		self.assertIn(OLD_ID, self.row["gb_item_history"])
		self.assertEqual(self.alerts.count(f"gb_ended_externally_{SERIAL}"), 1,
			"a listing marked ENDED that no machine will ever put back is not "
			"something to do silently")

	def test_the_count_and_the_age_are_both_required(self):
		"""An old first_at with only one strike is not enough, and this is the
		example that catches an implementation using OR.

		The count alone is not a clock: a gun with an end pending is probed by the
		fifteen-minute order poll too, so its strikes accrue four times faster
		than an hourly gun's.
		"""
		self.row["gb_gone_strikes"] = 0
		self.row["gb_gone_first_at"] = add_to_date(now_datetime(), hours=-25)
		self._run(self._gone_twice(
			(("GET", "/ItemsSelling"), self._page([])),
			(("GET", "/ItemsSold"), self._page([])),
		))
		self.assertEqual(self.row["gb_gone_strikes"], 1)
		self.assertEqual(self.row["gb_item_id"], OLD_ID)
		self.assertFalse(self.row["gb_delisted"])

	def test_rows_that_answer_nothing_move_nothing(self):
		"""Rows with neither key are not a "no". They alert once and touch no field."""
		self._run(self._gone_twice(
			(("GET", "/ItemsSelling"), self._page([{"title": "something"}])),
			(("GET", "/ItemsSold"), self._page([{"title": "something"}])),
		))
		self.assertEqual(self.row["gb_item_id"], OLD_ID)
		self.assertEqual(self.row["gb_gone_strikes"], 0)
		self.assertIsNone(self.row["gb_gone_first_at"])
		self.assertIn(f"gb_scan_unusable_{SERIAL}", self.alerts)

	def test_an_html_404_is_a_bad_url_not_a_missing_listing(self):
		"""Same status code, opposite meaning. An IIS page says nothing about any
		listing, so it counts as an error — and the error arm changes nothing."""
		self._run([(("GET", f"/Items/{OLD_ID}"), fx.html_404())])
		self.assertEqual(self.row["gb_item_id"], OLD_ID)
		self.assertEqual(self.row["gb_gone_strikes"], 0)


# ---------------------------------------------------------------------------
# The 200 arm
# ---------------------------------------------------------------------------

class WhenGunBrokerAnswersWithTheItem(_SweepCase):
	def test_a_live_listing_clears_the_counters(self):
		self.row["gb_gone_strikes"] = 1
		self.row["gb_gone_first_at"] = add_to_date(now_datetime(), hours=-30)
		self._run([
			(("GET", f"/Items/{OLD_ID}"),
				fx.make_response(fx.item_detail(OLD_ID, sku=SKU))),
		])
		self.assertEqual(self.row["gb_gone_strikes"], 0)
		self.assertIsNone(self.row["gb_gone_first_at"])

	def test_a_sold_listing_with_no_order_yet_is_flagged_fail_closed(self):
		self._run([
			(("GET", f"/Items/{OLD_ID}"),
				fx.make_response(fx.item_detail(OLD_ID, sku=SKU, has_winning_bid=True))),
		])
		self.assertEqual(self.row["gb_sold_unknown"], 1)
		self.assertIn(f"gb_sold_unmatched_{SERIAL}", self.alerts)

	def test_a_sold_listing_whose_order_is_already_recorded_is_left_alone(self):
		self.row["gb_reserved"] = 1
		self.row["gb_order"] = "GB-ORD-0001"
		self._run([
			(("GET", f"/Items/{OLD_ID}"),
				fx.make_response(fx.item_detail(OLD_ID, sku=SKU, has_winning_bid=True))),
		])
		self.assertEqual(self.row["gb_sold_unknown"], 0)
		self.assertEqual(self.alerts, [])

	def test_a_relisted_listing_is_adopted_before_anything_calls_it_ended(self):
		"""The relist window, from the 200 side. `isActive` false and no sale is
		what EVERY unsold gun looks like once a month — the replacement listing
		exists, and calling this ended would strand a live listing."""
		self._run([
			(("GET", f"/Items/{OLD_ID}"), fx.make_response(
				fx.item_detail(OLD_ID, sku=SKU, is_active=False, quantity=1))),
			(("GET", "/ItemsSelling"),
				self._page([fx.selling_row(item_id=int(NEW_ID), sku=SKU)])),
		])
		self.assertEqual(self.row["gb_item_id"], NEW_ID)
		self.assertIn(OLD_ID, self.row["gb_item_history"])
		self.assertEqual(self.row["gb_delisted"], 0)
		self.assertEqual(self.alerts, [])

	def test_the_relist_pointer_is_used_when_gunbroker_has_filled_it_in(self):
		"""`relistedAsItemID` is a shortcut when present and never something to
		wait for: a 0 is not evidence that nothing was relisted."""
		self._run([
			(("GET", f"/Items/{OLD_ID}"), fx.make_response(fx.item_detail(
				OLD_ID, sku=SKU, is_active=False, quantity=1,
				relisted_as=int(NEW_ID)))),
		])
		self.assertEqual(self.row["gb_item_id"], NEW_ID)
		self.assertEqual(self.row["gb_delisted"], 0)

	def test_an_inactive_listing_nobody_ended_needs_two_rounds(self):
		"""THE regression test for this arm.

		Round one records an observation and changes nothing. An implementation
		that ends the listing on one look passes every other test in this file and
		then, on the thirtieth day, marks a gun that is live on GunBroker as ENDED
		— sentinel set so no machine re-lists it, item number thrown away so the
		sweep stops watching it, and the counter free to sell it.
		"""
		script = [
			(("GET", f"/Items/{OLD_ID}"), fx.make_response(
				fx.item_detail(OLD_ID, sku=SKU, is_active=False, quantity=1))),
			(("GET", "/ItemsSelling"), self._page([])),
		]
		self._run(script)
		self.assertEqual(self.row["gb_item_id"], OLD_ID)
		self.assertEqual(self.row["gb_delisted"], 0)
		self.assertEqual(self.row["gb_gone_strikes"], 1)
		self.assertIsNotNone(self.row["gb_gone_first_at"])
		self.assertEqual(self.alerts, [], "one observation is not news")

		# Two hours later, still nothing under that SKU.
		self.row["gb_gone_first_at"] = add_to_date(now_datetime(), hours=-3)
		self._run(script)
		self.assertEqual(self.row["gb_item_id"], "")
		self.assertEqual(self.row["gb_delisted"], 1)
		self.assertEqual(self.alerts.count(f"gb_ended_externally_{SERIAL}"), 1,
			"exactly one alert — a person has to be told, once")

	def test_a_listing_we_asked_to_end_needs_only_one_round(self):
		"""We sent the delete, so inactive is the expected answer and there is
		nothing ambiguous to wait out. No second alert either — T4 already sent
		one when the end was requested."""
		self.row["gb_end_pending"] = 1
		self._run([
			(("GET", f"/Items/{OLD_ID}"), fx.make_response(
				fx.item_detail(OLD_ID, sku=SKU, is_active=False, quantity=1))),
			(("GET", "/ItemsSelling"), self._page([])),
		])
		self.assertEqual(self.row["gb_item_id"], "")
		self.assertEqual(self.row["gb_delisted"], 1)
		self.assertEqual(self.row["gb_end_pending"], 0)
		self.assertNotIn(f"gb_ended_externally_{SERIAL}", self.alerts)

	def test_a_listing_that_reappears_after_we_ended_it_is_ended_again_properly(self):
		"""Somebody re-listed the same gun by hand on GunBroker. Costs no request
		to notice — it reads the index this round already pulled.

		The second end goes through the whole contract: delete, then read the item
		back and check `isActive`. "We sent a delete" is not evidence one happened;
		this API answers 200 to things it does not do.
		"""
		self.row["gb_end_pending"] = 1
		self._patch_object(tasks.listings, "_settings",
			side_effect=lambda: fx.settings_stub())
		session = self._run([
			(("GET", f"/Items/{OLD_ID}"), fx.make_response(
				fx.item_detail(OLD_ID, sku=SKU, is_active=False, quantity=1))),
			(("GET", "/ItemsSelling"),
				self._page([fx.selling_row(item_id=int(NEW_ID), sku=SKU)])),
			(("DELETE", f"/Items/{NEW_ID}"), fx.make_response(fx.delete_ok(NEW_ID))),
			(("GET", f"/Items/{NEW_ID}"), fx.make_response(
				fx.item_detail(NEW_ID, sku=SKU, is_active=False))),
		])
		self.assertEqual(session.count("DELETE", f"/Items/{NEW_ID}"), 1)
		self.assertEqual(session.count("GET", f"/Items/{NEW_ID}"), 1,
			"a delete is only done when the item reads back inactive")
		self.assertEqual(self.row["gb_item_id"], "")
		self.assertEqual(self.row["gb_delisted"], 1)

	def test_a_price_that_drifted_is_marked_AND_sent(self):
		"""Flagging is not sending.

		The dirty bit's only other writer is a human editing `sell_price`, which
		is also the only other thing that enqueues the reprice — so a drift the
		sweep found and merely flagged would sit on a live listing at the wrong
		price until somebody happened to edit that gun again. The sweep is the
		one thing that noticed; it has to be the one that acts.
		"""
		self._patch("ffl_core.firearm.effective_serial_price", return_value=1234.0)
		self._run([
			(("GET", f"/Items/{OLD_ID}"), fx.make_response(
				fx.item_detail(OLD_ID, sku=SKU, fixed_price=999.0))),
		])
		self.assertEqual(self.row["gb_price_dirty"], 1)
		self.assertIn("ffl_integrations.gunbroker.tasks.gb_reprice_serial_task",
			self.enqueued, "the reprice was flagged and never sent")

	def test_a_price_that_agrees_enqueues_nothing(self):
		self._patch("ffl_core.firearm.effective_serial_price", return_value=9999.99)
		self._run([
			(("GET", f"/Items/{OLD_ID}"), fx.make_response(
				fx.item_detail(OLD_ID, sku=SKU, fixed_price=9999.99))),
		])
		self.assertEqual(self.row["gb_price_dirty"], 0)
		self.assertEqual(self.enqueued, [])


# ---------------------------------------------------------------------------
# Round-level behaviour
# ---------------------------------------------------------------------------

class TheRoundItself(_SweepCase):
	def test_the_reverse_direction_ends_a_listing_pos_says_is_not_sellable(self):
		"""POS knows the gun is spoken for and GunBroker still has it up."""
		self.shelf = {"sellable": False, "reason": "counter_hold",
			"detail": {"ref": "ACC-SINV-0001"}}
		self._patch_object(tasks.listings, "_settings",
			side_effect=lambda: fx.settings_stub())
		session = self._run([
			(("GET", f"/Items/{OLD_ID}"),
				fx.make_response(fx.item_detail(OLD_ID, sku=SKU))),
			(("DELETE", f"/Items/{OLD_ID}"), fx.make_response(fx.delete_ok(OLD_ID))),
			(("GET", f"/Items/{OLD_ID}"), fx.make_response(
				fx.item_detail(OLD_ID, sku=SKU, is_active=False))),
		])
		self.assertEqual(session.count("DELETE", "/Items/"), 1)
		self.assertEqual(self.row["gb_delisted"], 1)

	def test_it_does_not_end_a_listing_because_gunbroker_itself_has_the_gun(self):
		"""The mirror-image mistake, and an easy one to make.

		A gun with a GunBroker order against it IS unsellable in POS — that is the
		reservation working. Reading that as "POS says unsellable, so take it off
		GunBroker" answers the marketplace with its own news, and GunBroker will
		not withdraw a listing somebody has bought: the call fails, an end-pending
		flag goes up and somebody is alerted about a listing behaving correctly.
		"""
		self.row["gb_reserved"] = 1
		self.row["gb_order"] = "GB-ORD-0001"
		self.shelf = {"sellable": False, "reason": "channel_reserved",
			"detail": {"channel": "gunbroker", "ref": "GB-ORD-0001"}}
		session = self._run([
			(("GET", f"/Items/{OLD_ID}"),
				fx.make_response(fx.item_detail(OLD_ID, sku=SKU))),
		])
		self.assertEqual(session.count("DELETE", "/Items/"), 0)
		self.assertEqual(self.row["gb_item_id"], OLD_ID)

	def test_the_doubt_flag_is_treated_the_same_way(self):
		"""`gb_sold_unknown` means the same thing with less certainty, and less
		certainty is not a reason to be more aggressive."""
		self.shelf = {"sellable": False, "reason": "sold_unknown", "detail": {}}
		session = self._run([
			(("GET", f"/Items/{OLD_ID}"),
				fx.make_response(fx.item_detail(OLD_ID, sku=SKU))),
		])
		self.assertEqual(session.count("DELETE", "/Items/"), 0)

	def test_five_unreadable_answers_stop_the_round(self):
		"""GunBroker being down is one fact. Rediscovering it three hundred times
		is not a better answer than stopping and saying so once.

		Counted in PROBES, not requests: the client's own retry ladder turns each
		500 into three calls, and the circuit breaker is about how many questions
		the round keeps asking, not how many packets that costs.
		"""
		rows = [frappe._dict(dict(self.row, name=f"SN-{i}")) for i in range(8)]
		self._patch_object(tasks, "sweep_candidates", side_effect=lambda *_a, **_k: rows)
		probes = []
		self._patch_object(GunBrokerClient, "item_probe",
			side_effect=lambda item_id: probes.append(item_id) or {
				"state": "error", "item": None})
		self._run([])
		self.assertEqual(len(probes), tasks.ERROR_CIRCUIT_BREAK,
			"the round must stop asking, not work through all eight guns")
		self.assertIn("gb_sweep_circuit_break", self.alerts)

	def test_an_unconfirmed_attempt_with_no_timestamp_is_chased_immediately(self):
		"""The timestamp arrived after the flag, so historical rows carry the flag
		alone. Reading a missing timestamp as "not old enough" would strand those
		guns for good — the opposite of what the grace period is for."""
		self.assertTrue(tasks._push_grace_expired({"gb_push_attempted_at": None}))

	def test_an_unconfirmed_attempt_inside_the_grace_period_is_left_alone(self):
		self.assertFalse(tasks._push_grace_expired(
			{"gb_push_attempted_at": now_datetime()}))

class TheCandidateSet(FrappeTestCase):
	"""Its own class because `_SweepCase` replaces this very function."""

	def test_it_names_only_the_three_live_states(self):
		"""A gun that has LEFT the channel is not swept: GunBroker does not relist
		after a sale or a delete (owner confirmed), so there is nothing left to
		watch for and re-selecting them would grow this set without bound."""
		captured = {}
		with patch("frappe.db.sql",
				side_effect=lambda q, *a, **k: captured.setdefault("q", q) and []):
			tasks.sweep_candidates()
		sql = captured["q"]
		self.assertIn("gb_item_id", sql)
		self.assertIn("gb_push_pending", sql)
		self.assertIn("gb_end_pending", sql)
		self.assertNotIn("gb_sku_watch_until", sql)

	def test_it_selects_the_auction_columns_the_sweep_branches_on(self):
		"""_handle_ok reads gb_listing_type (unsold auction vs ended externally)
		and gb_bid_locked (lock once, not every hour) straight off this row."""
		captured = {}
		with patch("frappe.db.sql",
				side_effect=lambda q, *a, **k: captured.setdefault("q", q) and []):
			tasks.sweep_candidates()
		self.assertIn("gb_listing_type", captured["q"])
		self.assertIn("gb_bid_locked", captured["q"])


class TheRoundAnswersFromItsSharedScans(_SweepCase):
	"""One pull of each scan per round, not one per gun.

	The unconfirmed-attempt arm used to re-page `/ItemsSelling` and `/ItemsSold`
	for every gun it looked at. On an account with a real catalogue that is fifty
	pages each, per gun — and the round already has both scans indexed by SKU.
	"""

	def test_an_unconfirmed_attempt_is_settled_from_the_index(self):
		self.row["gb_push_pending"] = 1
		self.row["gb_item_id"] = ""
		session = self._run([
			(("GET", "/ItemsSelling"),
				self._page([fx.selling_row(item_id=int(NEW_ID), sku=SKU)])),
		])
		self.assertEqual(self.row["gb_item_id"], NEW_ID)
		self.assertEqual(self.row["gb_push_pending"], 0)
		self.assertEqual(session.count("GET", "/ItemsSelling"), 1,
			"the round pulls each scan once and every arm reads that copy")

	def test_both_scans_missing_it_releases_the_attempt(self):
		self.row["gb_push_pending"] = 1
		self.row["gb_item_id"] = ""
		self._run([
			(("GET", "/ItemsSelling"), self._page([])),
			(("GET", "/ItemsSold"), self._page([])),
		])
		self.assertEqual(self.row["gb_push_pending"], 0,
			"nothing was created, so the button has to work again")

	def test_two_live_listings_under_one_SKU_are_never_auto_claimed(self):
		"""The state the fast path must not flatten.

		GunBroker permits duplicates, and a lost response followed by a retry is
		precisely how a gun ends up with two. Picking one automatically leaves
		the other live and unwatched — and a GunBroker sale cannot be unwound by
		the seller, so the second buyer gets a refund and an apology. This is the
		situation the whole PR exists to prevent, so the shortcut past
		`recover_item_id` must keep its `duplicate` verdict.
		"""
		self.row["gb_push_pending"] = 1
		self.row["gb_item_id"] = ""
		self._run([
			(("GET", "/ItemsSelling"), self._page([
				fx.selling_row(item_id=int(OLD_ID), sku=SKU),
				fx.selling_row(item_id=int(NEW_ID), sku=SKU),
			])),
		])
		self.assertEqual(self.row["gb_item_id"], "",
			"neither listing may be adopted — POS does not choose between them")
		self.assertEqual(self.row["gb_push_pending"], 1,
			"the attempt stays pending; it is unresolved, not resolved")
		self.assertEqual(self.alerts.count(f"gb_dup_listing_Serial No:{SERIAL}"), 1,
			"and somebody is told, exactly once")
		self.assertIn(OLD_ID, self.row["gb_sync_error"])
		self.assertIn(NEW_ID, self.row["gb_sync_error"])

	def test_the_sold_scan_flags_it_rather_than_releasing_it(self):
		self.row["gb_push_pending"] = 1
		self.row["gb_item_id"] = ""
		self._run([
			(("GET", "/ItemsSelling"), self._page([])),
			(("GET", "/ItemsSold"),
				self._page([fx.selling_row(item_id=int(OLD_ID), sku=SKU)])),
		])
		self.assertEqual(self.row["gb_sold_unknown"], 1)


class TheFastPathAgreesWithTheLongOne(FrappeTestCase):
	"""`_recover_from_round` is a second implementation of a decision that
	already had one, kept only because it can answer without paging GunBroker
	again. Its whole licence to exist is that it reaches the SAME verdict, so
	that is asserted directly rather than left to two sets of tests that happen
	to have been written the same week.

	The state that went missing first time was `duplicate`, and it is the one
	that matters most: it is the difference between telling somebody two live
	listings exist and quietly adopting one of them.
	"""

	def _fast(self, rows):
		rnd = SimpleNamespace(
			selling=tasks._Scan(list(rows)), sold=tasks._Scan([]))
		found = tasks._recover_from_round(rnd, SERIAL, SKU)
		return None if found is None else found["state"]

	def _slow(self, rows):
		"""The verdict `recover_item_id` builds from the same rows, by its own
		rules (listings.py) rather than by restating them here."""
		matches, usable = listings._scan_for_sku(list(rows), SKU, "")
		if usable is False:
			return "unknown"
		if len(matches) == 1:
			return "claimed"
		if len(matches) > 1:
			return "duplicate"
		return "miss"

	def _rows(self, *item_ids, sku=None):
		return [fx.selling_row(item_id=int(i), sku=sku or SKU) for i in item_ids]

	def test_one_live_listing_is_claimed_by_both(self):
		rows = self._rows(NEW_ID)
		self.assertEqual(self._fast(rows), "claimed")
		self.assertEqual(self._slow(rows), "claimed")

	def test_two_live_listings_are_a_duplicate_to_both(self):
		rows = self._rows(OLD_ID, NEW_ID)
		self.assertEqual(self._fast(rows), "duplicate")
		self.assertEqual(self._slow(rows), "duplicate")

	def test_a_scan_without_the_sku_is_a_miss_to_both(self):
		rows = self._rows(NEW_ID, sku="SOMEBODY-ELSE")
		self.assertEqual(self._fast(rows), "miss")
		self.assertEqual(self._slow(rows), "miss")

	def test_a_matching_row_with_no_readable_number_is_handed_over(self):
		"""The one case where the two legitimately differ, and the difference is
		one-directional: the long way has a local cached item number to fall
		back on, which an index built from responses does not. So the fast path
		declines and the long way decides — never the reverse."""
		rows = self._rows(NEW_ID)
		rows[0]["itemID"] = 0
		self.assertIsNone(self._fast(rows), "declines, rather than guessing")
		self.assertEqual(self._slow(rows), "unknown")


class WhenTheListingIsAnAuction(_SweepCase):
	def setUp(self):
		super().setUp()
		self.row["gb_listing_type"] = "Auction"
		self.row["gb_bid_locked"] = 0
		self.row["gb_bid_count"] = 0

	def test_a_bid_locks_the_gun_and_tells_the_web_store(self):
		self._run([
			(("GET", f"/Items/{OLD_ID}"), fx.make_response(fx.item_detail(
				OLD_ID, is_fixed_price=False, bids=2, current_bid=950.0))),
		])
		self.assertEqual(self.row["gb_bid_locked"], 1)
		self.assertEqual(self.row["gb_bid_count"], 2)
		self.assertEqual(self.row["gb_current_bid"], 950.0)
		self.assertEqual([e["reason"] for e in self.exits], ["bid"])
		self.assertEqual(self.row["gb_sold_unknown"], 0, "promised is not sold")
		self.assertEqual(self.alerts, [])

	def test_the_price_of_a_live_auction_is_never_called_drift(self):
		self._run([
			(("GET", f"/Items/{OLD_ID}"), fx.make_response(fx.item_detail(
				OLD_ID, is_fixed_price=False, fixed_price=0.0))),
		])
		self.assertEqual(self.row["gb_price_dirty"], 0)
		self.assertNotIn(
			"ffl_integrations.gunbroker.tasks.gb_reprice_serial_task", self.enqueued)

	def test_withdrawn_bids_release_the_gun(self):
		self.row.update(gb_bid_locked=1, gb_bid_count=1)
		self._run([
			(("GET", f"/Items/{OLD_ID}"), fx.make_response(fx.item_detail(
				OLD_ID, is_fixed_price=False, bids=0))),
		])
		self.assertEqual(self.row["gb_bid_locked"], 0)
		self.assertEqual(self.row["gb_bid_count"], 0)

	def test_a_winner_is_a_sale_and_lifts_the_lock(self):
		self.row.update(gb_bid_locked=1, gb_bid_count=3)
		self._run([
			(("GET", f"/Items/{OLD_ID}"), fx.make_response(fx.item_detail(
				OLD_ID, is_fixed_price=False, is_active=False, bids=3,
				has_winning_bid=True))),
		])
		self.assertEqual(self.row["gb_sold_unknown"], 1)
		self.assertEqual(self.row["gb_bid_locked"], 0)

	def test_running_out_unsold_ends_quietly_after_the_same_patience(self):
		"""No winner, nothing relisted: the ordinary end of an auction. Marked
		ENDED with a note, no alert — but only after the two rounds a fixed-price
		listing gets, because relist-until-sold shows up in that window."""
		self.row["gb_gone_strikes"] = 1
		self.row["gb_gone_first_at"] = add_to_date(now_datetime(), hours=-25)
		self._run([
			(("GET", f"/Items/{OLD_ID}"), fx.make_response(fx.item_detail(
				OLD_ID, is_fixed_price=False, is_active=False, bids=0))),
			(("GET", "/ItemsSelling"), self._page([])),
		])
		self.assertEqual(self.row["gb_delisted"], 1)
		self.assertEqual(self.row["gb_item_id"], "")
		self.assertEqual(self.row["gb_sync_error"], tasks.AUCTION_UNSOLD_NOTE)
		self.assertEqual(self.alerts, [], "not a fault, so nobody is paged")

	def test_the_first_look_at_an_unsold_auction_only_counts(self):
		self._run([
			(("GET", f"/Items/{OLD_ID}"), fx.make_response(fx.item_detail(
				OLD_ID, is_fixed_price=False, is_active=False, bids=0))),
			(("GET", "/ItemsSelling"), self._page([])),
		])
		self.assertEqual(self.row["gb_delisted"], 0)
		self.assertEqual(self.row["gb_gone_strikes"], 1)


class TheRoundIsSchedulerSafe(FrappeTestCase):
	"""`_run_sweep` is a cron target, so nothing may escape it — including the
	candidate query, which reads `gb_*` columns directly and therefore raises on
	a site where the app is live but migrate has not run."""

	def test_a_failure_selecting_candidates_is_reported_not_raised(self):
		alerts = []
		with patch.object(tasks, "sweep_candidates",
					side_effect=Exception("Unknown column 'gb_item_id'")), 				patch.object(tasks, "safe_log_error") as logged, 				patch.object(tasks, "notify_admins",
					side_effect=lambda s, m, **kw: alerts.append(kw["dedup_key"])), 				patch.object(GunBrokerClient, "from_settings",
					return_value=MagicMock()):
			tasks._run_sweep()  # must not raise
		self.assertTrue(logged.called)
		self.assertIn("gb_listing_sweep", alerts,
			"the task-level fallback key registered in the spec's alert table")


class TheGateIsSelfThrottling(FrappeTestCase):
	def test_switched_off_means_no_job_at_all(self):
		with patch("ffl_integrations.integration_gate.gunbroker_enabled",
				return_value=False), patch("frappe.enqueue") as enqueue:
			tasks.gb_listing_sweep()
		self.assertFalse(enqueue.called)

	def test_switched_on_enqueues_one_deduplicated_round(self):
		with patch("ffl_integrations.integration_gate.gunbroker_enabled",
				return_value=True), \
				patch.object(tasks, "scheduled_sync_enabled", return_value=True), \
				patch("frappe.enqueue") as enqueue:
			tasks.gb_listing_sweep()
		_args, kwargs = enqueue.call_args
		self.assertEqual(kwargs["job_id"], tasks.SWEEP_JOB)
		self.assertTrue(kwargs["deduplicate"],
			"a round that overruns the hour must not have a second stacked on it")

	def test_run_scheduled_sync_off_stops_the_sweep(self):
		"""The form promises "uncheck to stop the scheduled jobs while leaving
		manual buttons available". A checkbox that promises that and does nothing
		is worse than no checkbox."""
		with patch("ffl_integrations.integration_gate.gunbroker_enabled",
				return_value=True), \
				patch.object(tasks, "scheduled_sync_enabled", return_value=False), \
				patch("frappe.enqueue") as enqueue:
			tasks.gb_listing_sweep()
		self.assertFalse(enqueue.called)


class TheScheduledSyncSwitch(FrappeTestCase):
	"""It is read off the stored row, not off the settings document.

	A Single casts every Check through `cint()` as it loads, so a field nobody
	has saved arrives as 0 — indistinguishable from one deliberately unchecked.
	Read that way, the docfield default of 1 never applies on a site that has
	stored ANY row for this Single, and the sweep would silently never run on
	exactly the sites that already have GunBroker configured. The RSR and
	WooCommerce gates read their own copy of this field the unsafe way; this one
	deliberately does not copy them.
	"""

	def test_a_site_that_never_saved_the_field_still_runs_the_sweep(self):
		with patch("ffl_core.utils.single_raw", return_value=None):
			self.assertTrue(tasks.scheduled_sync_enabled())

	def test_an_explicit_zero_is_honoured(self):
		with patch("ffl_core.utils.single_raw", return_value="0"):
			self.assertFalse(tasks.scheduled_sync_enabled())

	def test_an_explicit_one_is_honoured(self):
		with patch("ffl_core.utils.single_raw", return_value="1"):
			self.assertTrue(tasks.scheduled_sync_enabled())

	def test_it_is_not_a_listing_payload_flag(self):
		"""`LISTING_FLAG_DEFAULTS` is scoped to Checks that shape the payload, and
		a derived test asserts that table matches its fixture exactly. A scheduler
		gate belongs outside it, beside `enabled` and `sandbox_mode`."""
		from ffl_integrations.gunbroker import mapping

		self.assertNotIn("auto_sync_enabled", mapping.LISTING_FLAG_DEFAULTS)
