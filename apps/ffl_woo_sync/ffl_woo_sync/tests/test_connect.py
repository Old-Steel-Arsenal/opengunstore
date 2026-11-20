"""One-click WooCommerce connection (``woocommerce/connect.py``).

The state, the callback and the Settings writes run against the real site and its Redis;
the store is a fake client that records every call (the storefront plugin's
``ffl-core/v1/pos-connection`` route does not exist in this repo's test stack).
"""
from __future__ import annotations

import json
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import connect
from ffl_woo_sync.woocommerce.client import (
	SETTINGS_DOCTYPE, WooCommerceClient, WooCommerceConfig, WooCommerceError)

STORE = "https://shop.example.test"
POS = "https://pos.example.test"
HOOK_PATH = f"/api/method/{connect.WEBHOOK_METHOD}"


def _password(field: str) -> str:
	return frappe.get_single(SETTINGS_DOCTYPE).get_password(field, raise_exception=False) or ""


def _status() -> list[str]:
	return (frappe.db.get_single_value(SETTINGS_DOCTYPE, "connect_status") or "").splitlines()


class FakeStore:
	"""Stands in for WooCommerceClient: webhooks in memory, the plugin route scripted."""

	def __init__(self, webhooks=None, plugin=True, put_answer=None, fail=None):
		self.webhooks = list(webhooks or [])
		self.plugin = plugin
		self.put_answer = {"connected": True} if put_answer is None else put_answer
		self.fail = fail or {}
		self.calls = []
		self._next_id = 1000

	def _maybe_fail(self, key):
		if key in self.fail:
			raise self.fail[key]

	def probe_connection(self):
		self.calls.append(("probe",))
		self._maybe_fail("probe")
		return {"products_reachable": True}

	def get(self, path, namespace="wc/v3", **kwargs):
		self.calls.append(("GET", namespace, path, kwargs.get("params")))
		if path == "webhooks":
			self._maybe_fail("list")
			page = (kwargs.get("params") or {}).get("page", 1)
			return self.webhooks[(page - 1) * 100:page * 100]
		if namespace == connect.PLUGIN_NAMESPACE:
			if not self.plugin:
				raise WooCommerceError('GET pos-connection → 404: {"code":"rest_no_route",'
					'"message":"No route was found matching the URL and request method."}', status_code=404)
			self._maybe_fail("plugin_get")
			return {"connected": False, "base_url": ""}
		raise AssertionError(f"unexpected GET {namespace}/{path}")

	def post(self, path, json=None, namespace="wc/v3", **kwargs):
		self.calls.append(("POST", namespace, path, json))
		self._maybe_fail("create")
		self._next_id += 1
		self.webhooks.append(dict(json, id=self._next_id))
		return {"id": self._next_id}

	def delete(self, path, namespace="wc/v3", **kwargs):
		self.calls.append(("DELETE", namespace, path, kwargs.get("params")))
		hook_id = int(path.split("/")[-1])
		self.webhooks = [w for w in self.webhooks if w["id"] != hook_id]

	def put(self, path, json=None, namespace="wc/v3", **kwargs):
		self.calls.append(("PUT", namespace, path, json))
		self._maybe_fail("plugin_put")
		return self.put_answer


class ConnectCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value(SETTINGS_DOCTYPE, "store_url", STORE)
		frappe.db.set_single_value(SETTINGS_DOCTYPE, "connect_status", "")
		for field in ("consumer_key", "consumer_secret", "webhook_secret", "outbound_webhook_secret"):
			connect._set_password(field, f"old-{field}")

	def tearDown(self):
		frappe.set_user("Administrator")

	def _start(self) -> str:
		with patch.object(connect, "get_url", return_value=POS):
			url = connect.start_connect()["url"]
		return url

	def _callback(self, state, **over):
		body = {"user_id": state, "consumer_key": "ck_new", "consumer_secret": "cs_new",
			"key_permissions": "read_write", "key_id": 7}
		body.update(over)
		frappe.local.response["http_status_code"] = None
		with patch("frappe.enqueue") as enqueue:
			result = connect.wc_auth_callback(**body)
		return result, frappe.local.response.get("http_status_code"), enqueue

	def _run(self, store: FakeStore):
		with patch.object(WooCommerceClient, "from_settings", return_value=store):
			connect.run_connect(run_as="Administrator", pos_url=POS)


class TestStartAndCallback(ConnectCase):
	def test_start_sends_the_browser_to_the_stores_approval_page(self):
		url = urlparse(self._start())
		self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", f"{STORE}/wc-auth/v1/authorize")
		q = {k: v[0] for k, v in parse_qs(url.query).items()}
		self.assertEqual(q["app_name"], "OpenGunstore")
		self.assertEqual(q["scope"], "read_write")
		self.assertEqual(q["return_url"], POS + connect.SETTINGS_ROUTE)
		self.assertEqual(q["callback_url"], f"{POS}/api/method/{connect.CALLBACK_METHOD}")
		ttl = frappe.cache.ttl(connect._state_key(q["user_id"]))
		self.assertTrue(0 < ttl <= 15 * 60, ttl)
		self.assertEqual(_status()[0], connect.WAITING, "the form polls only while Running")

	def test_start_refuses_a_store_url_without_https(self):
		frappe.db.set_single_value(SETTINGS_DOCTYPE, "store_url", "http://shop.example.test")
		with self.assertRaises(frappe.ValidationError):
			self._start()

	def test_an_approved_key_is_stored_and_the_rest_is_queued(self):
		state = parse_qs(urlparse(self._start()).query)["user_id"][0]
		result, code, enqueue = self._callback(state)
		self.assertEqual(result, {"ok": True})
		self.assertIsNone(code)
		self.assertEqual(_password("consumer_key"), "ck_new")
		self.assertEqual(_password("consumer_secret"), "cs_new")
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.args[0], "ffl_woo_sync.woocommerce.connect.run_connect")
		self.assertEqual(enqueue.call_args.kwargs["run_as"], "Administrator")
		self.assertEqual(enqueue.call_args.kwargs["pos_url"], POS)
		self.assertEqual(_status()[0], connect.RUNNING)

	def test_a_state_works_once(self):
		state = parse_qs(urlparse(self._start()).query)["user_id"][0]
		self.assertEqual(self._callback(state)[0], {"ok": True})
		result, code, enqueue = self._callback(state, consumer_key="ck_replay", consumer_secret="cs_replay")
		self.assertEqual(code, 400)
		self.assertFalse(result["ok"])
		enqueue.assert_not_called()
		self.assertEqual(_password("consumer_key"), "ck_new")

	def test_an_expired_state_is_refused(self):
		with patch.object(connect, "STATE_TTL_SECONDS", 1):
			state = parse_qs(urlparse(self._start()).query)["user_id"][0]
		time.sleep(1.2)
		result, code, enqueue = self._callback(state)
		self.assertEqual(code, 400)
		enqueue.assert_not_called()
		self.assertEqual(_password("consumer_key"), "old-consumer_key")

	def test_a_state_this_pos_never_issued_is_refused(self):
		for state in ("made-up", "", None):
			result, code, enqueue = self._callback(state)
			self.assertEqual(code, 400, state)
			enqueue.assert_not_called()
		self.assertEqual(_password("consumer_key"), "old-consumer_key")

	def test_a_key_that_is_not_read_write_is_refused_and_spends_the_state(self):
		state = parse_qs(urlparse(self._start()).query)["user_id"][0]
		for perms in ("read", "write", None):
			result, code, enqueue = self._callback(state, key_permissions=perms)
			self.assertEqual(code, 400)
			enqueue.assert_not_called()
		self.assertEqual(_password("consumer_key"), "old-consumer_key")
		# The read/write retry with the same state is refused too: it was spent.
		self.assertEqual(self._callback(state)[1], 400)
		self.assertEqual(_status()[0], connect.FAILED)
		self.assertIn("Store key failed", _status()[-1])

	def test_a_store_url_changed_mid_approval_is_refused(self):
		state = parse_qs(urlparse(self._start()).query)["user_id"][0]
		frappe.db.set_single_value(SETTINGS_DOCTYPE, "store_url", "https://other.example.test")
		result, code, enqueue = self._callback(state)
		self.assertEqual(code, 400)
		enqueue.assert_not_called()
		self.assertEqual(_password("consumer_key"), "old-consumer_key")


class TestWebhooks(ConnectCase):
	def test_only_this_pos_hooks_are_replaced(self):
		store = FakeStore(webhooks=[
			{"id": 1, "delivery_url": POS + HOOK_PATH, "topic": "order.created"},
			{"id": 2, "delivery_url": "http://POS.example.test" + HOOK_PATH + "?site=retail",
			 "topic": "order.updated"},
			{"id": 3, "delivery_url": "https://other-pos.example.test" + HOOK_PATH, "topic": "order.created"},
			{"id": 4, "delivery_url": POS + "/api/method/something.else", "topic": "order.created"},
		])
		created, removed = connect.replace_webhooks(store, "s3cret", POS)
		self.assertEqual((created, removed), (3, 2))
		self.assertEqual(sorted(w["id"] for w in store.webhooks if w["id"] < 1000), [3, 4])
		new = [w for w in store.webhooks if w["id"] > 1000]
		self.assertEqual(sorted(w["topic"] for w in new), sorted(connect.WEBHOOK_TOPICS))
		for hook in new:
			self.assertEqual(hook["delivery_url"], POS + HOOK_PATH)
			self.assertEqual(hook["secret"], "s3cret")
			self.assertEqual(hook["status"], "active")

	def test_reconnecting_leaves_three_hooks(self):
		store = FakeStore()
		connect.replace_webhooks(store, "first", POS)
		self.assertEqual(connect.replace_webhooks(store, "second", POS), (3, 3))
		ours = [w for w in store.webhooks if connect._points_here(w["delivery_url"], POS + HOOK_PATH)]
		self.assertEqual(len(ours), 3)
		self.assertEqual({w["secret"] for w in ours}, {"second"})

	def test_every_page_of_webhooks_is_read(self):
		others = [{"id": i, "delivery_url": f"https://x{i}.example.test/hook"} for i in range(1, 151)]
		store = FakeStore(webhooks=others + [{"id": 151, "delivery_url": POS + HOOK_PATH}])
		self.assertEqual(connect.replace_webhooks(store, "s", POS), (3, 1))


class TestRunConnectWithoutPlugin(ConnectCase):
	def test_without_the_plugin_only_store_and_webhooks_are_done(self):
		store = FakeStore(plugin=False)
		with patch.object(connect, "issue_api_user") as issue:
			self._run(store)
		issue.assert_not_called()
		status = _status()
		self.assertEqual(status[0], connect.CONNECTED)
		self.assertEqual(status[-1], "FFL Core plugin not installed")
		self.assertNotIn("PUT", [c[0] for c in store.calls])
		self.assertEqual(_password("outbound_webhook_secret"), "old-outbound_webhook_secret")
		new_secret = _password("webhook_secret")
		self.assertNotEqual(new_secret, "old-webhook_secret")
		self.assertEqual({w["secret"] for w in store.webhooks}, {new_secret})


class TestRunConnect(ConnectCase):
	def test_with_the_plugin_the_pos_credentials_are_pushed(self):
		store = FakeStore()
		self._run(store)
		put = [c for c in store.calls if c[0] == "PUT"]
		self.assertEqual(len(put), 1)
		_, namespace, path, body = put[0]
		self.assertEqual((namespace, path), ("ffl-core/v1", "pos-connection"))
		self.assertEqual(set(body), {"base_url", "api_key", "api_secret", "webhook_secret"})
		self.assertEqual(body["base_url"], POS)
		self.assertEqual(body["webhook_secret"], _password("outbound_webhook_secret"))
		self.assertNotEqual(body["webhook_secret"], _password("webhook_secret"))

		user = frappe.get_doc("User", connect.api_user_email())
		self.assertEqual(user.api_key, body["api_key"])
		self.assertEqual(user.get_password("api_secret"), body["api_secret"])
		self.assertTrue({"Stock Manager", "Distributor Console"} <= {r.role for r in user.roles})
		self.assertEqual(frappe.db.get_value("Role", "Distributor Console", "desk_access"), 0)
		self.assertFalse(frappe.db.sql("select 1 from `__Auth` where doctype='User' and name=%s"
			" and fieldname='password'", user.name), "the API user must have no password")

		status = _status()
		self.assertEqual(status[0], connect.CONNECTED)
		self.assertEqual(status[-1], f"FFL Core plugin connected as {user.name}")

	def test_reconnecting_reuses_the_user_and_rotates_every_secret(self):
		first = FakeStore()
		self._run(first)
		body1 = [c for c in first.calls if c[0] == "PUT"][0][3]
		hook1 = _password("webhook_secret")

		second = FakeStore()
		self._run(second)
		body2 = [c for c in second.calls if c[0] == "PUT"][0][3]
		self.assertEqual(frappe.db.count("User", {"name": connect.api_user_email()}), 1)
		self.assertEqual(body1["api_key"], body2["api_key"])
		self.assertNotEqual(body1["api_secret"], body2["api_secret"])
		self.assertNotEqual(body1["webhook_secret"], body2["webhook_secret"])
		self.assertNotEqual(hook1, _password("webhook_secret"))
		user = frappe.get_doc("User", connect.api_user_email())
		self.assertEqual(user.get_password("api_secret"), body2["api_secret"])


class TestRunConnectFailures(ConnectCase):
	def _fails_at(self, store: FakeStore, step: str) -> str:
		self._run(store)
		status = _status()
		self.assertEqual(status[0], connect.FAILED)
		self.assertTrue(status[-1].startswith(f"{step} failed: "), status[-1])
		return status[-1]

	def test_store_unreachable(self):
		store = FakeStore(fail={"probe": WooCommerceError("GET products → 401: nope", status_code=401)})
		self._fails_at(store, "Store")
		self.assertEqual(_password("webhook_secret"), "old-webhook_secret")
		self.assertEqual([c[0] for c in store.calls], ["probe"])

	def test_webhook_create_fails(self):
		store = FakeStore(fail={"create": WooCommerceError(
			'POST webhooks → 403: {"code":"woocommerce_rest_cannot_create","message":"Sorry, you cannot create."}',
			status_code=403)})
		line = self._fails_at(store, "Webhooks")
		self.assertIn("Sorry, you cannot create. (403)", line)
		self.assertNotIn("PUT", [c[0] for c in store.calls])

	def test_plugin_route_refuses_the_key(self):
		store = FakeStore(fail={"plugin_get": WooCommerceError(
			'GET pos-connection → 401: {"code":"woocommerce_rest_cannot_view","message":"Sorry."}',
			status_code=401)})
		with patch.object(connect, "issue_api_user") as issue:
			self._fails_at(store, "FFL Core plugin")
		issue.assert_not_called()
		self.assertNotIn("PUT", [c[0] for c in store.calls])

	def test_plugin_cannot_reach_the_pos(self):
		store = FakeStore(fail={"plugin_put": WooCommerceError(
			'PUT pos-connection → 422: {"code":"pos_unreachable","message":"The POS did not answer."}',
			status_code=422)})
		line = self._fails_at(store, "FFL Core plugin")
		self.assertIn("The POS did not answer. (422)", line)
		self.assertEqual(_password("outbound_webhook_secret"), "old-outbound_webhook_secret",
			"the POS keeps signing with the secret the plugin still has")

	def test_plugin_answer_without_connected_is_a_failure(self):
		store = FakeStore(put_answer={"connected": False})
		self._fails_at(store, "FFL Core plugin")
		self.assertEqual(_password("outbound_webhook_secret"), "old-outbound_webhook_secret")

	def test_missing_store_key_is_reported_not_raised(self):
		with patch.object(WooCommerceConfig, "from_settings",
				side_effect=connect.WooCommerceError("WooCommerce Settings: Consumer Key is missing.")):
			connect.run_connect(run_as="Administrator", pos_url=POS)
		self.assertEqual(_status()[-1], "Store failed: WooCommerce Settings: Consumer Key is missing.")


class TestClientNamespace(FrappeTestCase):
	def test_plugin_routes_use_the_same_session_and_auth(self):
		config = WooCommerceConfig(store_url=STORE, consumer_key="ck", consumer_secret="cs", timeout=5)
		client = WooCommerceClient(config)
		self.assertEqual(client._url("products"), f"{STORE}/wp-json/wc/v3/products")
		self.assertEqual(client._url("pos-connection", "ffl-core/v1"),
			f"{STORE}/wp-json/ffl-core/v1/pos-connection")
		with patch.object(client.session, "request") as request:
			request.return_value.status_code = 200
			request.return_value.content = b'{"connected": true}'
			request.return_value.json.return_value = json.loads(b'{"connected": true}')
			self.assertEqual(client.put("pos-connection", namespace="ffl-core/v1", json={"a": 1}),
				{"connected": True})
		args, kwargs = request.call_args
		self.assertEqual(args, ("PUT", f"{STORE}/wp-json/ffl-core/v1/pos-connection"))
		self.assertEqual(kwargs["json"], {"a": 1})
		self.assertNotIn("namespace", kwargs)
		self.assertEqual((client.session.auth.username, client.session.auth.password), ("ck", "cs"))
