"""GunBroker HTTP client — driven by real responses through the real parser.

Every response below is a genuine `requests.Response` carrying a body captured
from the sandbox (see `gunbroker_fixtures`). That is deliberate and it is the
only way most of these assertions mean anything: a mock returning a dict skips
the content-type check, the JSON parse and the enum unwrapping, which between
them are where this client's real decisions are made.
"""
from __future__ import annotations

from datetime import timezone
from unittest.mock import patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import client as client_module
from ffl_integrations.gunbroker.client import (
	PROD_BASE,
	SANDBOX_BASE,
	GunBrokerClient,
	GunBrokerConfig,
	GunBrokerConfigError,
	GunBrokerError,
	dev_key,
	gb_datetime,
	get_field,
	kv_key,
	kv_value,
	parse_gb_datetime,
	parse_item_id,
)
from ffl_integrations.tests import gunbroker_fixtures as fx


def _config(**over) -> GunBrokerConfig:
	values = {
		"username": "seller", "password": "secret", "dev_key": "sandbox-key",
		"base_url": SANDBOX_BASE, "sandbox": True, "timeout": 5,
	}
	values.update(over)
	return GunBrokerConfig(**values)


def _client(script, **over) -> tuple[GunBrokerClient, fx.RecordingSession]:
	session = fx.RecordingSession(script)
	client = GunBrokerClient(_config(**over), session=session)
	# A token in hand, so tests exercise the call they are about rather than a
	# login first. The login path has its own tests below.
	patcher = patch.object(GunBrokerClient, "cached_token", return_value="tok")
	patcher.start()
	client._stop_token_patch = patcher.stop
	return client, session


class ResponseShapeTriage(FrappeTestCase):
	"""404 means two different things, and the difference decides whether a gun
	goes back on the shelf."""

	def test_json_404_is_a_missing_resource(self):
		client, _s = _client([(("GET", "/Items/1"), fx.json_404())])
		self.addCleanup(client._stop_token_patch)
		with self.assertRaises(GunBrokerError) as caught:
			client.get_item("1")
		self.assertEqual(caught.exception.status_code, 404)
		self.assertTrue(caught.exception.json_body)

	def test_html_404_is_a_programming_error_not_a_missing_listing(self):
		client, _s = _client([(("GET", "/Items/1"), fx.html_404())])
		self.addCleanup(client._stop_token_patch)
		with self.assertRaises(GunBrokerError) as caught:
			client.get_item("1")
		exc = caught.exception
		self.assertEqual(exc.status_code, 404)
		self.assertFalse(exc.json_body,
			"an HTML 404 is a bad URL, and must never read as 'GunBroker says this "
			"listing is gone'")
		self.assertTrue(exc.terminal, "a route that does not exist will not start to")

	def test_item_probe_calls_only_a_json_404_gone(self):
		client, _s = _client([(("GET", "/Items/1"), fx.json_404())])
		self.addCleanup(client._stop_token_patch)
		self.assertEqual(client.item_probe("1")["state"], "gone")

	def test_item_probe_calls_an_html_404_an_error(self):
		"""The assertion that stops a double sale.

		A listing GunBroker still holds, reached through a mistyped route, must
		come back `error` — the caller acts on `gone` by releasing the gun.
		"""
		client, _s = _client([(("GET", "/Items/1"), fx.html_404())])
		self.addCleanup(client._stop_token_patch)
		self.assertEqual(client.item_probe("1")["state"], "error")

	def test_item_probe_ok_carries_the_item(self):
		client, _s = _client([
			(("GET", "/Items/16087230"), fx.make_response(fx.item_detail())),
		])
		self.addCleanup(client._stop_token_patch)
		probe = client.item_probe("16087230")
		self.assertEqual(probe["state"], "ok")
		self.assertEqual(probe["item"]["sku"], "PROBE::TEST0001")

	def test_a_rate_limit_page_is_never_gone(self):
		"""429 is retried, and once the retries are spent it is still `error`.

		The scripted answer repeats because the client tries three times — which
		is itself the assertion that a rate limit is treated as a transient fault
		rather than as news about the listing.
		"""
		client, session = _client([
			(("GET", "/Items/1"), fx.make_response(
				"<html>rate limited</html>", status=429, content_type="text/html"), True),
		])
		self.addCleanup(client._stop_token_patch)
		with patch("time.sleep"):
			self.assertEqual(client.item_probe("1")["state"], "error")
		self.assertEqual(session.count("GET", "/Items/1"), 3)


class RetryPolicy(FrappeTestCase):
	def test_a_5xx_is_retried_and_stays_retryable(self):
		client, session = _client([
			(("GET", "/Items/1"), fx.make_response(fx.SERVER_ERROR_JSON, status=500), True),
		])
		self.addCleanup(client._stop_token_patch)
		with patch("time.sleep"):
			with self.assertRaises(GunBrokerError) as caught:
				client.get_item("1")
		self.assertFalse(caught.exception.terminal,
			"a 500 must stay retryable so a sweep recovers once the cause clears")
		self.assertEqual(session.count("GET", "/Items/1"), 3)

	def test_a_validation_400_is_terminal_and_not_retried(self):
		client, session = _client([
			(("PUT", "/Items/1"), fx.make_response(fx.QUANTITY_ZERO_400, status=400)),
		])
		self.addCleanup(client._stop_token_patch)
		with self.assertRaises(GunBrokerError) as caught:
			client.update_listing("1", {"Quantity": 0})
		self.assertTrue(caught.exception.terminal)
		self.assertEqual(session.count("PUT"), 1,
			"GunBroker considered this request and refused it; sending it again "
			"gets the same refusal")

	def test_creating_a_listing_is_never_retried(self):
		"""The most important line in the retry policy.

		GunBroker enforces no uniqueness on SKU or serial number, so a retried
		create after an ambiguous failure makes a SECOND real listing for a gun
		that exists once — which then has to be deleted by hand there, and which
		sells twice if nobody notices.
		"""
		client, session = _client([
			(("POST", "/Items"), fx.make_response(fx.SERVER_ERROR_JSON, status=500), True),
		])
		self.addCleanup(client._stop_token_patch)
		with patch("time.sleep"):
			with self.assertRaises(GunBrokerError):
				client.create_listing({"Title": "x"}, [])
		self.assertEqual(session.count("POST", "/Items"), 1)

	def test_appending_pictures_is_never_retried(self):
		"""Same rule as create_listing, same reason.

		`POST /Pictures` APPENDS. A 5xx returned after the photos were already
		stored would, on a retry, store them a second time — the listing then
		shows every photo twice, and nothing on this side can tell that happened.
		"""
		client, session = _client([
			(("POST", "/Pictures/1"),
				fx.make_response(fx.SERVER_ERROR_JSON, status=500), True),
		])
		self.addCleanup(client._stop_token_patch)
		with patch("time.sleep"):
			with self.assertRaises(GunBrokerError):
				client.add_pictures("1", [("a.jpg", b"x", "image/jpeg")])
		self.assertEqual(session.count("POST", "/Pictures/1"), 1)

	def test_a_transport_failure_carries_no_status(self):
		def _boom(method, url, kwargs):
			raise requests.ConnectionError("reset")

		client, _s = _client([(("GET", "/Items/1"), _boom, True)])
		self.addCleanup(client._stop_token_patch)
		with patch("time.sleep"):
			with self.assertRaises(GunBrokerError) as caught:
				client.get_item("1")
		self.assertIsNone(caught.exception.status_code,
			"a dropped connection is not a 404 — a caller reading a stale status "
			"here would act on the previous call's result")


class TokenHandling(FrappeTestCase):
	def test_a_401_relogins_once_and_replays(self):
		calls = {"n": 0}

		def _expire_then_succeed(method, url, kwargs):
			calls["n"] += 1
			if calls["n"] == 1:
				return fx.make_response(fx.EXPIRED_TOKEN_401, status=401)
			return fx.make_response(fx.item_detail())

		session = fx.RecordingSession([
			(("GET", "/Items/16087230"), _expire_then_succeed, True),
			(("POST", "/Users/AccessToken"), fx.make_response(fx.LOGIN_OK)),
		])
		client = GunBrokerClient(_config(), session=session)
		with patch.object(GunBrokerClient, "cached_token", return_value="stale"), \
				patch.object(GunBrokerClient, "_store_token"), \
				patch.object(GunBrokerClient, "_clear_token"):
			item = client.get_item("16087230")
		self.assertEqual(item["itemID"], 16087230)
		self.assertEqual(session.count("POST", "/Users/AccessToken"), 1)
		self.assertEqual(session.count("GET", "/Items/16087230"), 2)

	def test_multipart_survives_the_replay(self):
		"""Photos are held as bytes, not streams.

		A file handle is consumed by the first attempt, so a replayed multipart
		would upload nothing — and GunBroker would answer 200 to a listing with no
		photos. Nothing about that failure is visible from this side.
		"""
		seen: list[int] = []
		calls = {"n": 0}

		def _expire_then_succeed(method, url, kwargs):
			calls["n"] += 1
			files = kwargs.get("files") or []
			for name, part in files:
				if name == "picture":
					seen.append(len(part[1]))
			if calls["n"] == 1:
				return fx.make_response(fx.EXPIRED_TOKEN_401, status=401)
			return fx.make_response(fx.PICTURE_UPLOAD_OK)

		session = fx.RecordingSession([
			(("POST", "/Pictures/1"), _expire_then_succeed, True),
			(("POST", "/Users/AccessToken"), fx.make_response(fx.LOGIN_OK)),
		])
		client = GunBrokerClient(_config(), session=session)
		with patch.object(GunBrokerClient, "cached_token", return_value="stale"), \
				patch.object(GunBrokerClient, "_store_token"), \
				patch.object(GunBrokerClient, "_clear_token"):
			client.add_pictures("1", [("a.jpg", b"0123456789", "image/jpeg")])
		self.assertEqual(seen, [10, 10],
			"the replayed attempt must carry the same bytes, not an exhausted stream")

	def test_a_failed_login_never_quotes_the_request(self):
		"""The password travels in that body."""
		session = fx.RecordingSession([
			(("POST", "/Users/AccessToken"),
				fx.make_response({"userMessage": "Invalid credentials."}, status=400)),
		])
		client = GunBrokerClient(_config(password="hunter2-in-the-body"), session=session)
		with self.assertRaises(GunBrokerError) as caught:
			client.login()
		self.assertNotIn("hunter2", str(caught.exception))
		self.assertNotIn("hunter2", caught.exception.body or "")

	def test_a_successful_call_renews_the_token(self):
		"""Renewal on use, not only at login.

		The counter's pre-sale check refuses to make an HTTP call unless a token
		is already cached — that is what keeps it inside its time budget. If the
		cache entry were only written at login it would lapse during any quiet
		spell and that check would degrade to silently doing nothing.
		"""
		client, _s = _client([
			(("GET", "/Items/16087230"), fx.make_response(fx.item_detail())),
		])
		self.addCleanup(client._stop_token_patch)
		with patch.object(GunBrokerClient, "_store_token") as store:
			client.get_item("16087230")
		store.assert_called_once_with("tok")


class EnvironmentSelection(FrappeTestCase):
	def test_sandbox_and_production_have_separate_bases(self):
		self.assertEqual(_config(sandbox=True, base_url=SANDBOX_BASE).base_url, SANDBOX_BASE)
		self.assertEqual(_config(sandbox=False, base_url=PROD_BASE).base_url, PROD_BASE)
		self.assertNotEqual(SANDBOX_BASE, PROD_BASE)

	def test_the_url_is_built_against_the_configured_base(self):
		client, session = _client([
			(("GET", "/GunBrokerTime"), fx.make_response(fx.GUNBROKER_TIME_OK)),
		])
		self.addCleanup(client._stop_token_patch)
		client.server_time()
		self.assertTrue(session.calls[0]["url"].startswith(SANDBOX_BASE))

	def test_the_devkey_rides_on_every_request(self):
		client, session = _client([
			(("GET", "/GunBrokerTime"), fx.make_response(fx.GUNBROKER_TIME_OK)),
		])
		self.addCleanup(client._stop_token_patch)
		client.server_time()
		self.assertEqual(session.calls[0]["headers"]["X-DevKey"], "sandbox-key")

	def test_the_user_agent_is_the_one_the_devkey_was_registered_under(self):
		"""Production matches this header against the DevKey's registered name
		— a case-sensitive substring — and refuses everything else with a 403
		that blames the key. The sandbox does not look, which makes this the
		one header a green sandbox run cannot vouch for. The shape is the one
		GunBroker asked for — Software/Seller/Version/Application — and the
		Seller segment is the registered name, which is what is matched.
		"""
		client, session = _client([
			(("GET", "/GunBrokerTime"), fx.make_response(fx.GUNBROKER_TIME_OK)),
		], seller_name="Registered Seller LLC")
		self.addCleanup(client._stop_token_patch)
		client.server_time()
		sent = session.calls[0]["headers"]["User-Agent"]
		self.assertEqual(sent, client.config.user_agent)
		software, seller, version, application = sent.split("/")
		self.assertEqual(software, "GunStorePOS")
		self.assertEqual(seller, "Registered Seller LLC")
		self.assertTrue(version and application)


class ResponseFieldHelpers(FrappeTestCase):
	def test_get_field_keeps_false_and_zero(self):
		"""`src.get(a) or src.get(b)` looks equivalent and is not.

		`quantity` 0 and `isActive` False are the two values whose entire purpose
		is being false; an `or` chain steps over both to the default and reads
		"sold out" as "not stated".
		"""
		item = fx.item_detail(is_active=False, quantity=0)
		self.assertIs(get_field(item, "isActive"), False)
		self.assertEqual(get_field(item, "quantity"), 0)
		self.assertIsNone(get_field(item, "noSuchKey"))

	def test_get_field_is_case_insensitive_across_the_round_trip(self):
		"""GunBroker takes `FixedPrice` and answers `fixedPrice`."""
		item = fx.item_detail()
		self.assertEqual(get_field(item, "FixedPrice", "fixedPrice"), 9999.99)
		self.assertEqual(get_field(item, "fixedprice"), 9999.99)

	def test_enums_read_back_as_one_entry_objects(self):
		item = fx.item_detail()
		self.assertEqual(kv_key(item["condition"]), "3")
		self.assertEqual(kv_value(item["condition"]), "Used")
		self.assertEqual(kv_key(item["shippingClassesSupported"]), "8")
		self.assertEqual(kv_value(item["inspectionPeriod"]),
			"AS IS - No refund or exchange")

	def test_enum_helpers_accept_the_plain_value_too(self):
		"""So a caller never has to know which end of the round trip it holds."""
		self.assertEqual(kv_key(3), 3)
		self.assertEqual(kv_value("Used"), "Used")
		self.assertIsNone(kv_key(None))


class DateParsing(FrappeTestCase):
	def test_a_z_suffix_is_utc(self):
		parsed = parse_gb_datetime("2026-09-24T23:30:42Z")
		self.assertEqual(parsed.tzinfo, timezone.utc)
		self.assertEqual(parsed.hour, 23)

	def test_a_bare_timestamp_is_eastern_not_utc(self):
		"""Measured: the two spellings of one moment differ by four hours in
		August. Reading the bare one as UTC puts every deadline four hours out."""
		bare = parse_gb_datetime("2026-09-24T19:30:42")
		explicit = parse_gb_datetime("2026-09-24T23:30:42Z")
		self.assertEqual(bare, explicit)

	def test_gb_datetime_prefers_the_explicit_utc_twin(self):
		row = fx.selling_row()
		self.assertEqual(
			gb_datetime(row, "endingDateTime"),
			parse_gb_datetime(row["endingDateTimeUTC"]))

	def test_unparseable_and_blank_are_none(self):
		self.assertIsNone(parse_gb_datetime(""))
		self.assertIsNone(parse_gb_datetime(None))
		self.assertIsNone(parse_gb_datetime("not a date"))


class ItemIdParsing(FrappeTestCase):
	"""There is no itemID key. The number is in prose, and in a link."""

	def test_read_from_the_user_message(self):
		self.assertEqual(parse_item_id(fx.CREATE_LISTING_OK), "16087230")

	def test_falls_back_to_the_self_link(self):
		self.assertEqual(parse_item_id(fx.CREATE_LISTING_OK_LINKS_ONLY), "16087231")

	def test_returns_blank_rather_than_guessing(self):
		"""A wrong guess here does not fail loudly — it leaves a real listing on
		GunBroker that this POS knows nothing about, and the next click makes a
		second one. Blank sends the caller down the recovery path instead."""
		self.assertEqual(parse_item_id(fx.CREATE_LISTING_OK_NO_ID), "")
		self.assertEqual(parse_item_id({}), "")
		self.assertEqual(parse_item_id("a string"), "")


class ListingScans(FrappeTestCase):
	def test_paging_stops_on_a_short_page(self):
		rows = [fx.selling_row(item_id=i) for i in range(3)]
		client, session = _client([
			(("GET", "/ItemsSelling"), fx.make_response(fx.page(rows, page_size=300))),
		])
		self.addCleanup(client._stop_token_patch)
		self.assertEqual(len(list(client.iter_items_selling())), 3)
		self.assertEqual(session.count("GET", "/ItemsSelling"), 1)

	def test_paging_walks_while_pages_are_full(self):
		full = [fx.selling_row(item_id=i) for i in range(2)]
		client, session = _client([
			(("GET", "/ItemsSelling"), fx.make_response(fx.page(full, page_size=2))),
			(("GET", "/ItemsSelling"), fx.make_response(fx.page([], page_size=2))),
		])
		self.addCleanup(client._stop_token_patch)
		self.assertEqual(len(list(client.iter_items_selling(page_size=2))), 2)
		self.assertEqual(session.count("GET", "/ItemsSelling"), 2)

	def test_no_sku_parameter_is_ever_sent_to_items_selling(self):
		"""`?SKU=` is rejected outright by this endpoint, so matching happens
		here rather than there."""
		client, session = _client([
			(("GET", "/ItemsSelling"), fx.make_response(fx.page([]))),
		])
		self.addCleanup(client._stop_token_patch)
		list(client.iter_items_selling())
		self.assertNotIn("SKU", session.calls[0]["params"] or {})


class Pictures(FrappeTestCase):
	def test_the_upload_response_is_a_bare_string(self):
		"""Returned as-is rather than coerced: a caller that then calls .get() on
		it should fail here, not silently see every key as missing."""
		client, _s = _client([
			(("POST", "/Pictures/1"), fx.make_response(fx.PICTURE_UPLOAD_OK)),
		])
		self.addCleanup(client._stop_token_patch)
		self.assertEqual(
			client.add_pictures("1", [("a.jpg", b"x", "image/jpeg")]),
			fx.PICTURE_UPLOAD_OK)

	def test_create_listing_sends_at_most_eight_pictures(self):
		"""The ninth onward are dropped by GunBroker without a word."""
		client, session = _client([
			(("POST", "/Items"), fx.make_response(fx.CREATE_LISTING_OK)),
		])
		self.addCleanup(client._stop_token_patch)
		client.create_listing(
			{"Title": "x"},
			[(f"{i}.jpg", b"x", "image/jpeg") for i in range(13)])
		parts = session.calls[0]["files"]
		self.assertEqual(len([p for p in parts if p[0] == "picture"]), 8)
		self.assertEqual(len([p for p in parts if p[0] == "data"]), 1)

	def test_deleting_an_already_deleted_picture_is_not_an_error(self):
		client, _s = _client([
			(("DELETE", "/Pictures/9"), fx.make_response(
				{"userMessage": "Picture 9 not found"}, status=400)),
		])
		self.addCleanup(client._stop_token_patch)
		self.assertFalse(client.delete_picture("9"))


class DeleteListing(FrappeTestCase):
	def test_a_successful_delete_returns_the_message_response(self):
		client, _s = _client([
			(("DELETE", "/Items/16087239"), fx.make_response(fx.delete_ok())),
		])
		self.addCleanup(client._stop_token_patch)
		body = client.delete_listing("16087239")
		self.assertIn("is ended", body["userMessage"])
		self.assertNotIn("item", body,
			"the delete response is a message, with no item payload to read state "
			"from — confirmation has to come from re-reading the item")

	def test_a_403_is_surfaced_as_such(self):
		client, _s = _client([
			(("DELETE", "/Items/1"), fx.make_response(
				fx.DELETE_FORBIDDEN_403, status=403)),
		])
		self.addCleanup(client._stop_token_patch)
		with self.assertRaises(GunBrokerError) as caught:
			client.delete_listing("1")
		self.assertEqual(caught.exception.status_code, 403)

	def test_a_batch_delete_reports_only_what_succeeded(self):
		client, session = _client([
			(("DELETE", "/Items/"), fx.make_response(fx.BATCH_DELETE_OK)),
		])
		self.addCleanup(client._stop_token_patch)
		body = client.delete_listings(["16087238", "16087237"])
		self.assertEqual(
			[row["itemID"] for row in body["successfullyEndedItems"]],
			[16087238, 16087237])
		self.assertEqual(session.calls[0]["params"]["itemIds"], "16087238,16087237")


class TheDevKeyLivesInSiteConfig(FrappeTestCase):
	"""The DevKey identifies the software, not the store, so it is read from
	``frappe.conf`` (``bench set-config``) and never from the Settings form —
	the form no longer has a field for it."""

	def test_each_environment_reads_its_own_key(self):
		with fx.dev_keys(production="prod-key", sandbox="sandbox-key"):
			self.assertEqual(dev_key(sandbox=False), "prod-key")
			self.assertEqual(dev_key(sandbox=True), "sandbox-key")

	def test_a_missing_key_is_never_covered_by_the_other_environment(self):
		with fx.dev_keys(production="prod-key", sandbox=""):
			self.assertEqual(dev_key(sandbox=True), "")

	def _from_settings(self, **conf):
		settings = fx.settings_stub()
		with fx.dev_keys(**conf), \
				patch("ffl_integrations.integration_gate.gunbroker_enabled",
					return_value=True), \
				patch("frappe.get_single", return_value=settings), \
				patch.object(client_module, "sandbox_mode", return_value=True):
			return GunBrokerConfig.from_settings()

	def test_from_settings_combines_the_form_and_the_site_config(self):
		cfg = self._from_settings(production="prod-key", sandbox="sandbox-key")
		self.assertEqual(cfg.dev_key, "sandbox-key")
		self.assertEqual(cfg.username, "seller")
		self.assertEqual(cfg.password, "secret")
		self.assertEqual(cfg.base_url, SANDBOX_BASE)

	def _config_for(self, *, sandbox, seller_name="", **settings_over):
		settings = fx.settings_stub(**settings_over)
		with fx.dev_keys(production="p", sandbox="s"), \
				patch.dict(frappe.local.conf, {"gunbroker_seller_name": seller_name}), \
				patch("ffl_integrations.integration_gate.gunbroker_enabled", return_value=True), \
				patch("frappe.get_single", return_value=settings), \
				patch.object(client_module, "sandbox_mode", return_value=sandbox):
			return GunBrokerConfig.from_settings()

	def test_the_seller_segment_comes_from_site_config(self):
		for sandbox in (True, False):
			cfg = self._config_for(sandbox=sandbox, seller_name="  Registered Seller LLC ")
			self.assertEqual(cfg.seller_name, "Registered Seller LLC")
			self.assertEqual(cfg.user_agent, "GunStorePOS/Registered Seller LLC/1.0/POS")

	def test_a_blank_seller_name_is_a_config_error_on_production(self):
		"""No guess from the Company name: production answers a wrong segment with
		the misleading DevKey 403, so the blank site config key is named instead."""
		with self.assertRaises(GunBrokerConfigError) as caught:
			self._config_for(sandbox=False, seller_name="  ")
		self.assertIn("gunbroker_seller_name", str(caught.exception))
		self.assertIn("DevKey is registered under", str(caught.exception))

	def test_the_sandbox_does_not_check_the_segment_so_blank_gets_a_placeholder(self):
		cfg = self._config_for(sandbox=True, seller_name="")
		self.assertEqual(cfg.user_agent, "GunStorePOS/Sandbox/1.0/POS")

	def test_a_base_url_override_makes_it_production_for_the_header(self):
		with self.assertRaises(GunBrokerConfigError):
			self._config_for(sandbox=True, seller_name="",
				base_url_override="https://api.gunbroker.example/v1")

	def test_test_connection_shows_the_seller_name_message(self):
		from ffl_integrations.gunbroker import listings

		with patch.object(GunBrokerClient, "from_settings", side_effect=GunBrokerConfigError(
				"Set site config `gunbroker_seller_name` to the name your DevKey is registered under.")):
			result = listings.test_connection()
		self.assertFalse(result["ok"])
		self.assertIn("gunbroker_seller_name", result["error"])

	def test_the_form_no_longer_carries_the_seller_name(self):
		"""It belongs to the DevKey, not the store; the form must not offer it."""
		import json
		from pathlib import Path

		# Read the shipped doctype file: the form metadata is only as new as the last migrate.
		path = Path(client_module.__file__).parents[1] / "ffl_integrations" / "doctype" \
			/ "gunbroker_settings" / "gunbroker_settings.json"
		fields = {f["fieldname"] for f in json.loads(path.read_text(encoding="utf-8"))["fields"]}
		self.assertNotIn("seller_name", fields)

	def test_a_slash_in_the_seller_name_cannot_shift_the_segments(self):
		self.assertEqual(client_module.build_user_agent("A/B Arms"),
			"GunStorePOS/A B Arms/1.0/POS")

	def test_from_settings_refuses_without_the_key_and_names_where_it_goes(self):
		with self.assertRaises(GunBrokerConfigError) as caught:
			self._from_settings(production="prod-key", sandbox="")
		self.assertIn("gunbroker_sandbox_dev_key", str(caught.exception))
		self.assertIn("sandbox", str(caught.exception))
