"""Unit tests for the WooCommerce REST client.

All tests run WITHOUT a live WooCommerce store: HTTP calls are intercepted at
the session level (injecting a MagicMock session) or at the `_request` level
(patch.object). No real network traffic is made.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce.client import (
	WooCommerceClient,
	WooCommerceConfig,
	WooCommerceConfigError,
	WooCommerceError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(
	store_url: str = "https://shop.example.com",
	consumer_key: str = "ck_testkey",
	consumer_secret: str = "cs_testsecret",
	timeout: int = 5,
) -> WooCommerceConfig:
	return WooCommerceConfig(
		store_url=store_url,
		consumer_key=consumer_key,
		consumer_secret=consumer_secret,
		timeout=timeout,
	)


def _client(cfg: WooCommerceConfig | None = None) -> tuple[WooCommerceClient, MagicMock]:
	"""Return (client, mock_session). HTTP calls go to mock_session.request."""
	cfg = cfg or _cfg()
	session = MagicMock()
	return WooCommerceClient(cfg, session=session), session


def _ok_response(body=None, status: int = 200) -> MagicMock:
	"""Fake requests.Response for a successful call."""
	resp = MagicMock()
	resp.status_code = status
	resp.content = b"[]" if body is None else b"ok"
	resp.json.return_value = body if body is not None else []
	resp.text = ""
	return resp


def _err_response(status: int, text: str = "Error") -> MagicMock:
	resp = MagicMock()
	resp.status_code = status
	resp.content = text.encode()
	resp.text = text
	return resp


# ---------------------------------------------------------------------------
# WooCommerceConfig.from_settings
# ---------------------------------------------------------------------------

class TestWooCommerceConfigFromSettings(FrappeTestCase):
	"""WooCommerceConfig.from_settings() with a mocked Frappe single doc."""

	def _mock_settings(self, store_url="", consumer_key="", consumer_secret="",
	                    timeout=30):
		m = MagicMock()
		m.store_url = store_url
		m.request_timeout_seconds = timeout
		m.get_password.side_effect = lambda field, **_: {
			"consumer_key": consumer_key,
			"consumer_secret": consumer_secret,
		}.get(field, "")
		return m

	def test_raises_when_store_url_empty(self):
		s = self._mock_settings(store_url="", consumer_key="ck_x", consumer_secret="cs_x")
		with patch("frappe.get_single", return_value=s):
			with self.assertRaises(WooCommerceConfigError):
				WooCommerceConfig.from_settings()

	def test_raises_when_consumer_key_missing(self):
		s = self._mock_settings(store_url="https://shop.example.com", consumer_key="",
		                        consumer_secret="cs_x")
		with patch("frappe.get_single", return_value=s):
			with self.assertRaises(WooCommerceConfigError):
				WooCommerceConfig.from_settings()

	def test_raises_when_consumer_secret_missing(self):
		s = self._mock_settings(store_url="https://shop.example.com", consumer_key="ck_x",
		                        consumer_secret="")
		with patch("frappe.get_single", return_value=s):
			with self.assertRaises(WooCommerceConfigError):
				WooCommerceConfig.from_settings()

	def test_trailing_slash_stripped_from_store_url(self):
		s = self._mock_settings(store_url="https://shop.example.com/",
		                        consumer_key="ck_x", consumer_secret="cs_y")
		with patch("frappe.get_single", return_value=s):
			cfg = WooCommerceConfig.from_settings()
		self.assertEqual(cfg.store_url, "https://shop.example.com")

	def test_timeout_defaults_to_30_when_unset(self):
		s = self._mock_settings(store_url="https://shop.example.com",
		                        consumer_key="ck_x", consumer_secret="cs_y",
		                        timeout=0)
		with patch("frappe.get_single", return_value=s):
			cfg = WooCommerceConfig.from_settings()
		self.assertEqual(cfg.timeout, 30)

	def test_credentials_propagated(self):
		s = self._mock_settings(store_url="https://shop.example.com",
		                        consumer_key="ck_abc", consumer_secret="cs_xyz",
		                        timeout=15)
		with patch("frappe.get_single", return_value=s):
			cfg = WooCommerceConfig.from_settings()
		self.assertEqual(cfg.consumer_key, "ck_abc")
		self.assertEqual(cfg.consumer_secret, "cs_xyz")
		self.assertEqual(cfg.timeout, 15)

	def test_https_url_accepted(self):
		s = self._mock_settings(store_url="https://shop.example.com",
		                        consumer_key="ck_x", consumer_secret="cs_y")
		with patch("frappe.get_single", return_value=s):
			cfg = WooCommerceConfig.from_settings()
		self.assertEqual(cfg.store_url, "https://shop.example.com")

	def test_http_public_url_raises(self):
		"""Plain http:// to a public host must raise WooCommerceConfigError."""
		s = self._mock_settings(store_url="http://shop.example.com",
		                        consumer_key="ck_x", consumer_secret="cs_y")
		with patch("frappe.get_single", return_value=s):
			with self.assertRaises(WooCommerceConfigError) as cm:
				WooCommerceConfig.from_settings()
		self.assertIn("HTTPS", str(cm.exception))

	def test_http_localhost_accepted(self):
		"""http://localhost is allowed for local development."""
		s = self._mock_settings(store_url="http://localhost:8080",
		                        consumer_key="ck_x", consumer_secret="cs_y")
		with patch("frappe.get_single", return_value=s):
			cfg = WooCommerceConfig.from_settings()
		self.assertTrue(cfg.store_url.startswith("http://localhost"))

	def test_http_127_accepted(self):
		"""http://127.x.x.x is allowed for local development."""
		s = self._mock_settings(store_url="http://127.0.0.1",
		                        consumer_key="ck_x", consumer_secret="cs_y")
		with patch("frappe.get_single", return_value=s):
			cfg = WooCommerceConfig.from_settings()
		self.assertTrue(cfg.store_url.startswith("http://127."))

	def test_unknown_site_raises_config_error_not_key_error(self):
		"""An unrecognised site arg must raise WooCommerceConfigError, not KeyError."""
		with self.assertRaises(WooCommerceConfigError) as cm:
			WooCommerceConfig.from_settings(site="wholesale")
		self.assertIn("wholesale", str(cm.exception))

	def test_wrong_case_site_raises_config_error(self):
		"""'Dealer' (wrong case) must raise WooCommerceConfigError, not KeyError."""
		with self.assertRaises(WooCommerceConfigError) as cm:
			WooCommerceConfig.from_settings(site="Dealer")
		self.assertIn("Dealer", str(cm.exception))


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------

class TestUrlConstruction(FrappeTestCase):
	def test_url_no_leading_slash(self):
		client, _ = _client()
		self.assertEqual(
			client._url("products"),
			"https://shop.example.com/wp-json/wc/v3/products",
		)

	def test_url_with_leading_slash(self):
		client, _ = _client()
		self.assertEqual(
			client._url("/products"),
			"https://shop.example.com/wp-json/wc/v3/products",
		)

	def test_url_with_sub_path(self):
		client, _ = _client()
		self.assertEqual(
			client._url("products/42"),
			"https://shop.example.com/wp-json/wc/v3/products/42",
		)


# ---------------------------------------------------------------------------
# _request — error handling & timeout
# ---------------------------------------------------------------------------

class TestRequest(FrappeTestCase):
	def test_raises_woocommerce_error_on_4xx(self):
		client, session = _client()
		session.request.return_value = _err_response(401, "Unauthorized")
		with self.assertRaises(WooCommerceError) as cm:
			client._request("GET", "products")
		self.assertEqual(cm.exception.status_code, 401)

	def test_raises_woocommerce_error_on_403(self):
		client, session = _client()
		session.request.return_value = _err_response(403, "Forbidden")
		with self.assertRaises(WooCommerceError) as cm:
			client._request("GET", "products")
		self.assertEqual(cm.exception.status_code, 403)

	def test_raises_woocommerce_error_on_5xx(self):
		client, session = _client()
		session.request.return_value = _err_response(503, "Service Unavailable")
		with self.assertRaises(WooCommerceError) as cm:
			client._request("GET", "products")
		self.assertEqual(cm.exception.status_code, 503)

	def test_timeout_passed_through_to_session(self):
		client, session = _client(_cfg(timeout=7))
		session.request.return_value = _ok_response()
		client._request("GET", "products")
		_, kwargs = session.request.call_args
		self.assertEqual(kwargs.get("timeout"), 7)

	def test_returns_json_body_on_success(self):
		client, session = _client()
		session.request.return_value = _ok_response([{"id": 1}])
		result = client._request("GET", "products")
		self.assertEqual(result, [{"id": 1}])

	def test_returns_none_on_empty_body(self):
		client, session = _client()
		resp = MagicMock()
		resp.status_code = 204
		resp.content = b""
		session.request.return_value = resp
		self.assertIsNone(client._request("DELETE", "products/1"))

	def test_raises_on_non_json_200_body(self):
		"""A 200 with non-empty body that fails json() decoding raises WooCommerceError."""
		client, session = _client()
		resp = MagicMock()
		resp.status_code = 200
		resp.content = b"<html>Not JSON</html>"
		resp.json.side_effect = ValueError("No JSON object could be decoded")
		resp.text = "<html>Not JSON</html>"
		session.request.return_value = resp
		with self.assertRaises(WooCommerceError):
			client._request("GET", "products")


# ---------------------------------------------------------------------------
# Session — Basic auth credential wiring
# ---------------------------------------------------------------------------

class TestSessionAuth(FrappeTestCase):
	def test_build_session_sets_basic_auth(self):
		"""The real session built by _build_session must carry Basic auth credentials."""
		cfg = _cfg(consumer_key="ck_abc", consumer_secret="cs_xyz")
		client = WooCommerceClient(cfg)  # no injected session → calls _build_session
		# Re-build so we get a fresh one we can inspect (the __init__ session is real)
		session = client._build_session()
		# An auth OBJECT, not the (key, secret) tuple: the tuple printed both
		# halves raw in every requests frame the Error Log records
		# (ffl_core.credentials.RedactedBasicAuth).
		self.assertEqual((session.auth.username, session.auth.password), ("ck_abc", "cs_xyz"))
		self.assertNotIn("cs_xyz", repr(session.auth))

	def test_tls_verify_is_always_on(self):
		"""There is no switch for this any more: Basic auth credentials travel
		on every request, and a store that needs a self-signed certificate
		trusted gets its CA added to the trust store, not verification off."""
		client = WooCommerceClient(_cfg())
		session = client._build_session()
		self.assertTrue(session.verify)


# ---------------------------------------------------------------------------
# list_categories — the category mirror reads a store's whole tree once a tick
# ---------------------------------------------------------------------------

class TestListCategories(FrappeTestCase):
	def test_pages_through_and_unescapes_names(self):
		client, _ = _client()
		page1 = [{"id": i, "name": f"C{i}", "slug": f"c{i}", "parent": 0} for i in range(1, 101)]
		page2 = [{"id": 101, "name": "Slings &amp; Mounts", "slug": "slings-mounts", "parent": 589,
			"menu_order": 3, "count": 12}]
		with patch.object(client, "get", side_effect=[page1, page2]) as mock_get:
			terms = client.list_categories()
		self.assertEqual(len(terms), 101)
		self.assertEqual(terms[-1], {"id": 101, "name": "Slings & Mounts", "slug": "slings-mounts",
			"parent": 589, "menu_order": 3, "count": 12})
		self.assertEqual([c.kwargs["params"]["page"] for c in mock_get.call_args_list], [1, 2])
		self.assertEqual(mock_get.call_args_list[0].kwargs["params"]["per_page"], 100)


# ---------------------------------------------------------------------------
# get_or_create_brand (product_brand term; the MAP plugin matches on it)
# ---------------------------------------------------------------------------

class TestGetOrCreateBrand(FrappeTestCase):
	def test_returns_id_of_exact_match(self):
		client, _ = _client()
		with patch.object(client, "get",
		                  return_value=[{"id": 12, "name": "Holosun"}]), \
		     patch.object(client, "post") as mock_post:
			brand_id = client.get_or_create_brand("Holosun")
		self.assertEqual(brand_id, 12)
		mock_post.assert_not_called()

	def test_match_is_case_insensitive(self):
		client, _ = _client()
		with patch.object(client, "get",
		                  return_value=[{"id": 9, "name": "holosun"}]), \
		     patch.object(client, "post") as mock_post:
			self.assertEqual(client.get_or_create_brand("Holosun"), 9)
		mock_post.assert_not_called()

	def test_creates_when_no_exact_match(self):
		client, _ = _client()
		# `search` is fuzzy — "Holosun Optics" must not be taken for "Holosun".
		with patch.object(client, "get",
		                  return_value=[{"id": 3, "name": "Holosun Optics"}]), \
		     patch.object(client, "post",
		                  return_value={"id": 42, "name": "Holosun"}) as mock_post:
			self.assertEqual(client.get_or_create_brand("Holosun"), 42)
		mock_post.assert_called_once_with("products/brands", json={"name": "Holosun"})

	def test_search_hits_the_brands_endpoint(self):
		client, _ = _client()
		with patch.object(client, "get",
		                  return_value=[{"id": 1, "name": "Glock"}]) as mock_get, \
		     patch.object(client, "post"):
			client.get_or_create_brand("Glock")
		args, kwargs = mock_get.call_args
		self.assertEqual(args[0], "products/brands")
		self.assertEqual(kwargs["params"], {"search": "Glock", "per_page": 100})

	def test_memoized_per_name(self):
		client, _ = _client()
		with patch.object(client, "get",
		                  return_value=[{"id": 5, "name": "Smith & Wesson"}]) as mock_get, \
		     patch.object(client, "post"):
			first = client.get_or_create_brand("Smith & Wesson")
			second = client.get_or_create_brand("smith & wesson")
		self.assertEqual((first, second), (5, 5))
		mock_get.assert_called_once()

	def test_empty_name_raises(self):
		client, _ = _client()
		with self.assertRaises(WooCommerceError):
			client.get_or_create_brand("  ")


# ---------------------------------------------------------------------------
# get_or_create_shipping_class (dropship flat-rate classes)
# ---------------------------------------------------------------------------

class TestGetOrCreateShippingClass(FrappeTestCase):
	def test_matches_on_slug_not_name(self):
		"""WooCommerce assigns a product's shipping class by SLUG, so the slug is
		the identity here — a same-named class with another slug is a miss."""
		client, _ = _client()
		with patch.object(client, "get", return_value=[
			{"id": 8, "name": "Dropship Handgun", "slug": "dropship-handgun-2"},
			{"id": 3, "name": "Handgun (dropship)", "slug": "dropship-handgun"},
		]), patch.object(client, "post") as mock_post:
			self.assertEqual(client.get_or_create_shipping_class("dropship-handgun"), 3)
		mock_post.assert_not_called()

	def test_creates_with_name_and_slug(self):
		client, _ = _client()
		with patch.object(client, "get", return_value=[]), \
		     patch.object(client, "post", return_value={"id": 42}) as mock_post:
			created = client.get_or_create_shipping_class(
				"dropship-handgun", name="Dropship Handgun")
		self.assertEqual(created, 42)
		mock_post.assert_called_once_with(
			"products/shipping_classes",
			json={"name": "Dropship Handgun", "slug": "dropship-handgun"},
		)

	def test_name_defaults_to_the_slug(self):
		client, _ = _client()
		with patch.object(client, "get", return_value=[]), \
		     patch.object(client, "post", return_value={"id": 7}) as mock_post:
			client.get_or_create_shipping_class("dropship-firearm")
		self.assertEqual(mock_post.call_args.kwargs["json"]["name"], "dropship-firearm")

	def test_lookup_filters_by_slug_exactly(self):
		"""The terms controller supports `slug=` — an exact filter beats a fuzzy
		`search` plus client-side matching (fewer rows, no near-miss risk)."""
		client, _ = _client()
		with patch.object(client, "get", return_value=[
			{"id": 1, "slug": "dropship-accessory"}]) as mock_get, \
		     patch.object(client, "post"):
			client.get_or_create_shipping_class("dropship-accessory")
		args, kwargs = mock_get.call_args
		self.assertEqual(args[0], "products/shipping_classes")
		self.assertEqual(kwargs["params"],
		                 {"slug": "dropship-accessory", "per_page": 100})

	def test_list_shipping_classes_returns_a_slug_index(self):
		"""One GET for the whole table — what the per-batch ensure runs on."""
		client, _ = _client()
		with patch.object(client, "get", return_value=[
			{"id": 3, "slug": "dropship-handgun"},
			{"id": 4, "slug": "dropship-firearm"},
			{"id": 9, "slug": None},
		]) as mock_get:
			index = client.list_shipping_classes()
		self.assertEqual(index, {"dropship-handgun": 3, "dropship-firearm": 4})
		self.assertEqual(mock_get.call_args.kwargs["params"], {"per_page": 100})

	def test_memoized_per_slug(self):
		client, _ = _client()
		with patch.object(client, "get", return_value=[
			{"id": 5, "slug": "dropship-accessory"}]) as mock_get, \
		     patch.object(client, "post"):
			first = client.get_or_create_shipping_class("dropship-accessory")
			second = client.get_or_create_shipping_class("dropship-accessory")
		self.assertEqual((first, second), (5, 5))
		mock_get.assert_called_once()

	def test_empty_slug_raises(self):
		client, _ = _client()
		with self.assertRaises(WooCommerceError):
			client.get_or_create_shipping_class("   ")


# ---------------------------------------------------------------------------
# _get_or_create_term — the one resolver behind category / brand / shipping class
# ---------------------------------------------------------------------------

class TestGetOrCreateTermConflict(FrappeTestCase):
	"""A create that loses a race must resolve, not give up.

	Two workers (a listing batch and a reconcile sweep) can POST the same
	not-yet-existing term at the same moment; WooCommerce answers the loser with
	400 `term_exists`. Treating that as failure is what dropped a product's
	shipping class — and shipping-class loss is silent money.
	"""

	def _conflict(self, message="term_exists: A term with the name provided already exists"):
		return WooCommerceError(message, status_code=400)

	def test_shipping_class_conflict_resolves_by_re_reading(self):
		client, _ = _client()
		with patch.object(client, "get", side_effect=[[], [{"id": 3, "slug": "dropship-handgun"}]]), \
		     patch.object(client, "post", side_effect=self._conflict()):
			self.assertEqual(client.get_or_create_shipping_class("dropship-handgun"), 3)

	def test_brand_conflict_resolves_by_re_reading(self):
		client, _ = _client()
		with patch.object(client, "get", side_effect=[[], [{"id": 12, "name": "Holosun"}]]), \
		     patch.object(client, "post", side_effect=self._conflict()):
			self.assertEqual(client.get_or_create_brand("Holosun"), 12)

	def test_conflict_with_a_resource_id_uses_it_directly(self):
		"""WooCommerce hands back the winner's id in `data.resource_id`."""
		client, _ = _client()
		exc = WooCommerceError(
			'{"code":"term_exists","data":{"status":400,"resource_id":42}}',
			status_code=400)
		with patch.object(client, "get", return_value=[]) as mock_get, \
		     patch.object(client, "post", side_effect=exc):
			self.assertEqual(client.get_or_create_shipping_class("dropship-firearm"), 42)
		self.assertEqual(mock_get.call_count, 1)  # no second lookup needed

	def test_a_conflict_that_still_cannot_be_resolved_raises(self):
		"""Never invent an id: a caller that cannot get a real term must hear
		about it (the shipping-class path turns that into an Error Log)."""
		client, _ = _client()
		with patch.object(client, "get", side_effect=[[], []]), \
		     patch.object(client, "post", side_effect=self._conflict()):
			with self.assertRaises(WooCommerceError):
				client.get_or_create_shipping_class("dropship-handgun")

	def test_other_errors_are_not_swallowed(self):
		client, _ = _client()
		with patch.object(client, "get", return_value=[]), \
		     patch.object(client, "post",
		                  side_effect=WooCommerceError("500 boom", status_code=500)):
			with self.assertRaises(WooCommerceError):
				client.get_or_create_brand("Holosun")

	def test_the_resolvers_share_one_implementation(self):
		"""Same skeleton, one copy. If another taxonomy shows up it must
		reuse this too, so the conflict/caching fixes stay in one place. (Product
		categories are no longer resolved by name — the catalog master's tree links
		them by id, woocommerce.categories.)"""
		import inspect

		from ffl_woo_sync.woocommerce.client import WooCommerceClient

		for name in ("get_or_create_brand", "get_or_create_shipping_class"):
			with self.subTest(method=name):
				src = inspect.getsource(getattr(WooCommerceClient, name))
				self.assertIn("_get_or_create_term", src)


# ---------------------------------------------------------------------------
# probe_connection
# ---------------------------------------------------------------------------

class TestProbeConnection(FrappeTestCase):
	def test_calls_products_endpoint(self):
		client, session = _client()
		session.request.return_value = _ok_response([])
		client.probe_connection()
		call_args, call_kwargs = session.request.call_args
		url = call_args[1]
		self.assertIn("wp-json/wc/v3/products", url)

	def test_passes_per_page_1(self):
		client, session = _client()
		session.request.return_value = _ok_response([])
		client.probe_connection()
		_, call_kwargs = session.request.call_args
		self.assertEqual(call_kwargs.get("params", {}).get("per_page"), 1)

	def test_returns_store_url_on_success(self):
		client, session = _client(_cfg(store_url="https://store.example.com"))
		session.request.return_value = _ok_response([])
		info = client.probe_connection()
		self.assertEqual(info["store_url"], "https://store.example.com")
		self.assertTrue(info["products_reachable"])

	def test_propagates_woocommerce_error(self):
		client, session = _client()
		session.request.return_value = _err_response(401, "Invalid credentials")
		with self.assertRaises(WooCommerceError):
			client.probe_connection()


# ---------------------------------------------------------------------------
# Site selection (retail is the only store)
# ---------------------------------------------------------------------------

class TestClientSiteSelection(FrappeTestCase):
	def _stub(self):
		s = MagicMock()
		s.store_url = "https://shop.example.com"
		s.get_password.return_value = "k"
		s.request_timeout_seconds = 30
		return s

	@patch("ffl_woo_sync.woocommerce.client.frappe.get_single")
	def test_the_retired_dealer_store_is_an_unknown_site(self, gs):
		"""1.11.0: no settings are read for it; a config error, which every caller
		already treats as "no usable store"."""
		with self.assertRaises(WooCommerceConfigError):
			WooCommerceConfig.from_settings(site="dealer")
		gs.assert_not_called()

	@patch("ffl_woo_sync.woocommerce.client.frappe.get_single")
	def test_retail_default_reads_retail_doctype(self, gs):
		gs.return_value = self._stub()
		WooCommerceConfig.from_settings()
		gs.assert_called_once_with("WooCommerce Settings")


class TestListOrders(FrappeTestCase):
	"""list_orders — the order-integrity audit's read of recent WC orders."""

	def test_passes_params_and_returns_the_list(self):
		client, session = _client()
		session.request.return_value = _ok_response([{"id": 1059}, {"id": 1060}])
		out = client.list_orders(
			after="2026-07-05T00:00:00", status="any", per_page=50, page=2)
		self.assertEqual([o["id"] for o in out], [1059, 1060])
		_, kw = session.request.call_args
		self.assertEqual(kw["params"]["after"], "2026-07-05T00:00:00")
		self.assertEqual(kw["params"]["status"], "any")
		self.assertEqual(kw["params"]["per_page"], 50)
		self.assertEqual(kw["params"]["page"], 2)

	def test_non_list_response_returns_empty(self):
		client, session = _client()
		session.request.return_value = _ok_response({"code": "err"})  # dict, not a list
		self.assertEqual(client.list_orders(), [])


class FakeImageResponse:
	"""The slice of a streamed ``requests`` Response that ``probe_image_url`` reads."""

	def __init__(self, status=200, body=b"\xff\xd8\xff\xe0jpeg", ctype="image/jpeg", length=None):
		self.status_code = status
		self.headers = {"Content-Type": ctype}
		if length is not None:
			self.headers["Content-Length"] = str(length)
		self._body = body
		self.closed = False

	def iter_content(self, chunk_size=1):
		if self._body:
			yield self._body[:chunk_size]

	def close(self):
		self.closed = True


def fake_cdn(table: dict):
	"""A ``requests.get`` stand-in: URL → FakeImageResponse, or an exception to raise."""
	calls = []

	def get(url, **kwargs):
		calls.append(url)
		out = table[url]
		if isinstance(out, Exception):
			raise out
		return out
	get.calls = calls
	return get


class TestProbeImageUrl(FrappeTestCase):
	"""``ffl_core.utils.probe_image_url``: True good, False known bad, None unknown."""

	def _probe(self, resp):
		from ffl_core.utils import probe_image_url
		with patch("requests.get", fake_cdn({"https://cdn.example.com/a.jpg": resp})):
			return probe_image_url("https://cdn.example.com/a.jpg")

	def test_answers(self):
		import requests
		self.assertIs(self._probe(FakeImageResponse()), True)
		self.assertIs(self._probe(FakeImageResponse(length=0)), False)
		self.assertIs(self._probe(FakeImageResponse(body=b"")), False)  # chunked, no bytes
		self.assertIs(self._probe(FakeImageResponse(ctype="text/html; charset=utf-8")), False)
		self.assertIs(self._probe(FakeImageResponse(status=404)), False)
		self.assertIs(self._probe(FakeImageResponse(ctype="application/octet-stream")), True)
		self.assertIsNone(self._probe(FakeImageResponse(status=503)))
		self.assertIsNone(self._probe(FakeImageResponse(status=403)))
		self.assertIsNone(self._probe(requests.ConnectTimeout("slow")))

	def test_non_http_url_is_not_fetched(self):
		from ffl_core.utils import probe_image_url
		with patch("requests.get") as get:
			self.assertIsNone(probe_image_url("/files/a.jpg"))
		get.assert_not_called()

	def test_response_is_closed_and_timeout_bounded(self):
		from ffl_core.utils import probe_image_url
		resp = FakeImageResponse()
		with patch("requests.get", return_value=resp) as get:
			probe_image_url("https://cdn.example.com/a.jpg")
		self.assertTrue(resp.closed)
		self.assertTrue(get.call_args.kwargs["stream"])
		self.assertIsNotNone(get.call_args.kwargs["timeout"])


ITEM = "TEST-WOO-BAD-IMAGE"
GOOD = "https://cdn.example.com/x/good.jpg"
EMPTY = "https://cdn.example.com/x/empty_hires.png"
EMPTY_FILE_ERR = ('POST products → 400: {"code":"woocommerce_product_image_upload_error","message":'
	'"Invalid image: File is empty. Please upload something more substantial."}')


class TestDeadImagesDropped(FrappeTestCase):
	"""A product refused over an image is re-sent without the bad image(s), and
	the Item stops carrying them. Only the HTTP layers (the Woo send, the CDN
	GET) are stubbed; the gallery and Item rows are real."""

	def setUp(self):
		frappe.set_user("Administrator")
		if frappe.db.exists("Item", ITEM):
			frappe.delete_doc("Item", ITEM, ignore_permissions=True, force=True)
		self.addCleanup(frappe.db.rollback)

	def _item(self, image, gallery):
		with patch("frappe.enqueue"):
			frappe.get_doc({"doctype": "Item", "item_code": ITEM, "item_name": ITEM,
				"item_group": "Misc Accessory", "stock_uom": "Nos", "is_stock_item": 0,
				"image": image,
				"image_gallery": [{"image": u, "is_primary": int(i == 0)} for i, u in enumerate(gallery)],
			}).insert(ignore_permissions=True)

	def _gallery(self):
		return frappe.get_all("Item Image Gallery", filters={"parent": ITEM, "parenttype": "Item"},
			pluck="image", order_by="idx asc")

	@staticmethod
	def _sender(payload, errors):
		"""send(): raises the queued errors in order, then succeeds."""
		calls = []

		def send():
			calls.append([i["src"] for i in payload.get("images") or []])
			if len(calls) <= len(errors):
				raise WooCommerceError(errors[len(calls) - 1], status_code=400)
			return {"id": 9}
		send.calls = calls
		return send

	def test_named_dead_image_dropped_from_payload_and_gallery(self):
		from ffl_woo_sync.woocommerce import sync

		dead = "https://img.rsrgroup.com/pimages/X_3.jpg"
		live = "https://img.rsrgroup.com/pimages/X_1.jpg"
		self._item(live, [live, dead])
		payload = {"images": [{"src": live}, {"src": dead}]}
		body = ('POST products → 400: {"code":"woocommerce_product_image_upload_error","message":'
			'"Error getting remote image ' + dead.replace("/", "\\/") + '. Error: Not Found"}')
		send = self._sender(payload, [body])
		with patch("requests.get") as get:
			self.assertEqual(sync._without_dead_images(ITEM, payload, send), {"id": 9})
		get.assert_not_called()  # the error named it: nothing to probe
		self.assertEqual(send.calls[1], [live])
		self.assertEqual(self._gallery(), [live])

	def test_unnamed_empty_file_is_found_by_probing(self):
		"""The reported case: "Invalid image: File is empty" names no URL."""
		from ffl_woo_sync.woocommerce import sync

		self._item(EMPTY, [EMPTY, GOOD])
		payload = {"images": [{"src": EMPTY}, {"src": GOOD}]}
		send = self._sender(payload, [EMPTY_FILE_ERR])
		cdn = fake_cdn({EMPTY: FakeImageResponse(ctype="image/png", length=0), GOOD: FakeImageResponse()})
		with patch("requests.get", cdn):
			self.assertEqual(sync._without_dead_images(ITEM, payload, send), {"id": 9})
		self.assertEqual(send.calls, [[EMPTY, GOOD], [GOOD]])
		self.assertEqual(sorted(cdn.calls), sorted([EMPTY, GOOD]))  # at most the payload's images
		self.assertEqual(self._gallery(), [GOOD])
		self.assertEqual(frappe.db.get_value("Item", ITEM, "image"), GOOD)

	def test_unnamed_error_nothing_identified_raises_as_before(self):
		"""Probe finds every image fine or cannot tell: the error surfaces, nothing dropped."""
		import requests
		from ffl_woo_sync.woocommerce import sync

		self._item(GOOD, [GOOD, EMPTY])
		payload = {"images": [{"src": GOOD}, {"src": EMPTY}]}
		send = self._sender(payload, [EMPTY_FILE_ERR])
		cdn = fake_cdn({GOOD: FakeImageResponse(), EMPTY: requests.ReadTimeout("slow")})
		with patch("requests.get", cdn), self.assertRaises(WooCommerceError) as ctx:
			sync._without_dead_images(ITEM, payload, send)
		self.assertIn("File is empty", str(ctx.exception))
		self.assertEqual(len(send.calls), 1)
		self.assertEqual(len(payload["images"]), 2)
		self.assertEqual(self._gallery(), [GOOD, EMPTY])

	def test_unnamed_error_is_retried_once_only(self):
		from ffl_woo_sync.woocommerce import sync

		self._item(GOOD, [GOOD, EMPTY])
		payload = {"images": [{"src": GOOD}, {"src": EMPTY}]}
		send = self._sender(payload, [EMPTY_FILE_ERR, EMPTY_FILE_ERR])
		cdn = fake_cdn({GOOD: FakeImageResponse(), EMPTY: FakeImageResponse(length=0)})
		with patch("requests.get", cdn), self.assertRaises(WooCommerceError):
			sync._without_dead_images(ITEM, payload, send)
		self.assertEqual(send.calls, [[GOOD, EMPTY], [GOOD]])
		self.assertEqual(len(cdn.calls), 2)  # probed once, not again on the second refusal

	def test_bad_highres_upgrade_is_withdrawn_not_the_stored_row(self):
		"""The payload carries RSR's _HR upgrade of a stored standard URL: the
		standard row stays, and the next payload build uses it."""
		from ffl_woo_sync.woocommerce import mapping, sync

		std = "https://img.rsrgroup.com/pimages/X_1.jpg"
		hr = "https://img.rsrgroup.com/highres-pimages/X_1_HR.jpg"
		self._item(std, [std])
		self.addCleanup(mapping._HIGHRES_CACHE.pop, std, None)
		mapping._HIGHRES_CACHE[std] = hr
		payload = {"images": [{"src": hr}]}
		send = self._sender(payload, [EMPTY_FILE_ERR])
		with patch("requests.get", fake_cdn({hr: FakeImageResponse(length=0)})):
			sync._without_dead_images(ITEM, payload, send)
		# Nothing left to send: no key, so an update keeps the store's images.
		self.assertNotIn("images", payload)
		self.assertEqual(self._gallery(), [std])
		self.assertEqual(frappe.db.get_value("Item", ITEM, "image"), std)
		self.assertEqual(mapping._to_highres(std), std)

	def test_other_errors_raise_without_probing(self):
		from ffl_woo_sync.woocommerce import sync

		def send():
			raise WooCommerceError("POST products → 400: bad sku", status_code=400)
		with patch("requests.get") as get, self.assertRaises(WooCommerceError):
			sync._without_dead_images("ITEM-X", {"images": [{"src": GOOD}]}, send)
		get.assert_not_called()
