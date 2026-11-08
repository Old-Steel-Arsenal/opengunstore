"""Unit tests for the ShipStation V2 REST client.

All tests run WITHOUT a live ShipStation account: HTTP calls are intercepted at
the session level (injecting a MagicMock session) or at the `_request` level
(patch.object). No real network traffic is made.
"""
from __future__ import annotations

import ast
import inspect
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from ffl_integrations.shipstation import client as client_module
from ffl_integrations.shipstation.client import (
	ShipStationClient,
	ShipStationConfig,
	ShipStationConfigError,
	ShipStationError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(
	api_key: str = "TEST_key",
	base_url: str = "https://api.shipstation.com/v2",
	store_id: str = "",
	ship_from_warehouse_id: str = "",
	default_parcel_weight_oz: float = 16.0,
	timeout: int = 5,
	sandbox_mode: bool = True,
) -> ShipStationConfig:
	return ShipStationConfig(
		api_key=api_key,
		base_url=base_url,
		store_id=store_id,
		ship_from_warehouse_id=ship_from_warehouse_id,
		default_parcel_weight_oz=default_parcel_weight_oz,
		timeout=timeout,
		sandbox_mode=sandbox_mode,
	)


def _client(cfg: ShipStationConfig | None = None) -> tuple[ShipStationClient, MagicMock]:
	"""Return (client, mock_session). HTTP calls go to mock_session.request."""
	cfg = cfg or _cfg()
	session = MagicMock()
	return ShipStationClient(cfg, session=session), session


def _ok_response(body=None, status: int = 200) -> MagicMock:
	resp = MagicMock()
	resp.status_code = status
	resp.content = b"{}" if body is None else b"ok"
	resp.json.return_value = body if body is not None else {}
	resp.text = ""
	return resp


def _err_response(status: int, text: str = "Error") -> MagicMock:
	resp = MagicMock()
	resp.status_code = status
	resp.content = text.encode()
	resp.text = text
	return resp


def _mock_settings(api_key="TEST_key", base_url_override="", store_id="",
		ship_from_warehouse_id="", default_parcel_weight_oz=16,
		timeout=30, sandbox=1):
	m = MagicMock()
	m.base_url_override = base_url_override
	m.store_id = store_id
	m.ship_from_warehouse_id = ship_from_warehouse_id
	m.default_parcel_weight_oz = default_parcel_weight_oz
	m.request_timeout_seconds = timeout
	m.sandbox_mode = sandbox
	m.get_password.side_effect = lambda field, **_: {"api_key": api_key}.get(field, "")
	return m


# ---------------------------------------------------------------------------
# ShipStationConfig.from_settings
# ---------------------------------------------------------------------------

class TestShipStationConfigFromSettings(FrappeTestCase):
	def test_raises_when_api_key_missing(self):
		s = _mock_settings(api_key="")
		with patch("frappe.get_single", return_value=s):
			with self.assertRaises(ShipStationConfigError):
				ShipStationConfig.from_settings()

	def test_base_url_defaults_to_v2(self):
		s = _mock_settings(base_url_override="")
		with patch("frappe.get_single", return_value=s):
			cfg = ShipStationConfig.from_settings()
		self.assertEqual(cfg.base_url, "https://api.shipstation.com/v2")

	def test_base_url_override_respected_and_trailing_slash_stripped(self):
		s = _mock_settings(base_url_override="https://sandbox.example.com/v2/")
		with patch("frappe.get_single", return_value=s):
			cfg = ShipStationConfig.from_settings()
		self.assertEqual(cfg.base_url, "https://sandbox.example.com/v2")

	def test_timeout_defaults_to_30_when_unset(self):
		s = _mock_settings(timeout=0)
		with patch("frappe.get_single", return_value=s):
			cfg = ShipStationConfig.from_settings()
		self.assertEqual(cfg.timeout, 30)

	def test_http_public_override_raises(self):
		s = _mock_settings(base_url_override="http://api.example.com/v2")
		with patch("frappe.get_single", return_value=s):
			with self.assertRaises(ShipStationConfigError) as cm:
				ShipStationConfig.from_settings()
		self.assertIn("HTTPS", str(cm.exception))

	def test_http_localhost_override_allowed(self):
		s = _mock_settings(base_url_override="http://localhost:8080/v2")
		with patch("frappe.get_single", return_value=s):
			cfg = ShipStationConfig.from_settings()
		self.assertTrue(cfg.base_url.startswith("http://localhost"))

	def test_fields_propagated(self):
		s = _mock_settings(api_key="TEST_abc", store_id="1582173",
			ship_from_warehouse_id="se-wh-1", default_parcel_weight_oz=24,
			timeout=15, sandbox=1)
		with patch("frappe.get_single", return_value=s):
			cfg = ShipStationConfig.from_settings()
		self.assertEqual(cfg.api_key, "TEST_abc")
		self.assertEqual(cfg.store_id, "1582173")
		self.assertEqual(cfg.ship_from_warehouse_id, "se-wh-1")
		self.assertEqual(cfg.default_parcel_weight_oz, 24)
		self.assertEqual(cfg.timeout, 15)
		self.assertTrue(cfg.sandbox_mode)


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------

class TestUrlConstruction(FrappeTestCase):
	def test_url_no_leading_slash(self):
		client, _ = _client()
		self.assertEqual(client._url("shipments"),
			"https://api.shipstation.com/v2/shipments")

	def test_url_with_leading_slash(self):
		client, _ = _client()
		self.assertEqual(client._url("/carriers"),
			"https://api.shipstation.com/v2/carriers")


# ---------------------------------------------------------------------------
# _request — error handling & timeout
# ---------------------------------------------------------------------------

class TestRequest(FrappeTestCase):
	def test_raises_shipstation_error_on_4xx_with_status_code(self):
		client, session = _client()
		session.request.return_value = _err_response(400, "bad postal_code")
		with self.assertRaises(ShipStationError) as cm:
			client._request("POST", "shipments")
		self.assertEqual(cm.exception.status_code, 400)
		self.assertIn("bad postal_code", str(cm.exception))

	def test_raises_shipstation_error_on_401(self):
		client, session = _client()
		session.request.return_value = _err_response(401, "Unauthorized")
		with self.assertRaises(ShipStationError) as cm:
			client._request("GET", "carriers")
		self.assertEqual(cm.exception.status_code, 401)

	def test_timeout_passed_through_to_session(self):
		client, session = _client(_cfg(timeout=7))
		session.request.return_value = _ok_response()
		client._request("GET", "carriers")
		_, kwargs = session.request.call_args
		self.assertEqual(kwargs.get("timeout"), 7)

	def test_returns_json_body_on_success(self):
		client, session = _client()
		session.request.return_value = _ok_response({"carriers": []})
		self.assertEqual(client._request("GET", "carriers"), {"carriers": []})

	def test_returns_none_on_empty_body(self):
		client, session = _client()
		resp = MagicMock()
		resp.status_code = 204
		resp.content = b""
		session.request.return_value = resp
		self.assertIsNone(client._request("GET", "carriers"))

	def test_raises_on_non_json_200_body(self):
		client, session = _client()
		resp = MagicMock()
		resp.status_code = 200
		resp.content = b"<html>Not JSON</html>"
		resp.json.side_effect = ValueError("no json")
		resp.text = "<html>Not JSON</html>"
		session.request.return_value = resp
		with self.assertRaises(ShipStationError):
			client._request("GET", "carriers")

	def test_network_errors_wrapped_as_shipstation_error(self):
		"""Timeout/ConnectionError must surface as ShipStationError (status None)
		so the whitelisted layer's except clauses catch an outage cleanly."""
		import requests as _requests
		client, session = _client()
		for exc in (_requests.exceptions.ConnectTimeout("t"),
				_requests.exceptions.ConnectionError("down"),
				_requests.exceptions.ReadTimeout("slow")):
			session.request.side_effect = exc
			with self.assertRaises(ShipStationError) as cm:
				client._request("POST", "shipments")
			self.assertIsNone(cm.exception.status_code)


# ---------------------------------------------------------------------------
# Session — API-Key header + retry policy
# ---------------------------------------------------------------------------

class TestSession(FrappeTestCase):
	def test_build_session_sets_api_key_header(self):
		client = ShipStationClient(_cfg(api_key="TEST_xyz"))
		session = client._build_session()
		self.assertEqual(session.headers.get("API-Key"), "TEST_xyz")

	def test_retry_policy_excludes_post(self):
		"""POST /shipments creates a resource — a blind urllib3 retry could
		double-create, so only safe methods may auto-retry."""
		client = ShipStationClient(_cfg())
		session = client._build_session()
		retry = session.get_adapter("https://api.shipstation.com").max_retries
		self.assertNotIn("POST", retry.allowed_methods)
		self.assertIn("GET", retry.allowed_methods)


# ---------------------------------------------------------------------------
# create_shipment
# ---------------------------------------------------------------------------

class TestCreateShipment(FrappeTestCase):
	def test_wraps_envelope_and_returns_shipment(self):
		client, _ = _client()
		shipment = {"external_shipment_id": "SI-0001", "ship_to": {}}
		body = {"has_errors": False,
			"shipments": [{"shipment_id": "se-123", "shipment_status": "pending"}]}
		with patch.object(client, "_request", return_value=body) as req:
			got = client.create_shipment(shipment)
		req.assert_called_once_with("POST", "shipments",
			json_body={"shipments": [shipment]})
		self.assertEqual(got["shipment_id"], "se-123")

	def test_http_200_errors_without_id_raises(self):
		client, _ = _client()
		body = {"has_errors": True, "shipments": [
			{"errors": [{"message": "postal_code is invalid"}]}]}
		with patch.object(client, "_request", return_value=body):
			with self.assertRaises(ShipStationError) as cm:
				client.create_shipment({"ship_to": {}})
		self.assertIn("postal_code is invalid", str(cm.exception))

	def test_id_with_warning_errors_returns_shipment(self):
		"""A created shipment (id present) must never be reported as failure —
		raising here would orphan a live ShipStation shipment with nothing
		persisted on the invoice."""
		client, _ = _client()
		body = {"has_errors": True, "shipments": [
			{"shipment_id": "se-77", "shipment_status": "pending",
				"errors": [{"message": "address warning"}]}]}
		with patch.object(client, "_request", return_value=body):
			got = client.create_shipment({"ship_to": {}})
		self.assertEqual(got["shipment_id"], "se-77")

	def test_missing_shipment_id_raises(self):
		client, _ = _client()
		body = {"shipments": [{"shipment_status": "pending"}]}
		with patch.object(client, "_request", return_value=body):
			with self.assertRaises(ShipStationError):
				client.create_shipment({"ship_to": {}})

	def test_empty_shipments_raises(self):
		client, _ = _client()
		with patch.object(client, "_request", return_value={"shipments": []}):
			with self.assertRaises(ShipStationError):
				client.create_shipment({"ship_to": {}})


# ---------------------------------------------------------------------------
# probe_connection
# ---------------------------------------------------------------------------

class TestFindShipmentByExternalId(FrappeTestCase):
	def test_returns_first_non_cancelled_match(self):
		client, _ = _client()
		body = {"shipments": [
			{"shipment_id": "se-1", "shipment_status": "cancelled"},
			{"shipment_id": "se-2", "shipment_status": "pending"}]}
		with patch.object(client, "_request", return_value=body) as req:
			got = client.find_shipment_by_external_id("SI-0001")
		req.assert_called_once_with("GET", "shipments",
			params={"external_shipment_id": "SI-0001"})
		self.assertEqual(got["shipment_id"], "se-2")

	def test_returns_none_when_only_cancelled_or_empty(self):
		client, _ = _client()
		with patch.object(client, "_request", return_value={"shipments": [
				{"shipment_id": "se-1", "shipment_status": "cancelled"}]}):
			self.assertIsNone(client.find_shipment_by_external_id("SI-0001"))
		with patch.object(client, "_request", return_value={"shipments": []}):
			self.assertIsNone(client.find_shipment_by_external_id("SI-0001"))


class TestProbeConnection(FrappeTestCase):
	def test_gets_carriers(self):
		client, _ = _client()
		with patch.object(client, "_request",
				return_value={"carriers": [{"carrier_id": "se-1"}]}) as req:
			body = client.probe_connection()
		req.assert_called_once_with("GET", "carriers")
		self.assertEqual(len(body["carriers"]), 1)

	def test_list_warehouses(self):
		client, _ = _client()
		with patch.object(client, "_request",
				return_value={"warehouses": [{"warehouse_id": "se-9", "name": "Main"}]}) as req:
			whs = client.list_warehouses()
		req.assert_called_once_with("GET", "warehouses")
		self.assertEqual(whs[0]["warehouse_id"], "se-9")


# ---------------------------------------------------------------------------
# The wire contract
# ---------------------------------------------------------------------------
# Every call this client makes, in ONE table, because a wrong VERB is the one
# mistake the tests above structurally cannot catch. Patch `_request`, assert
# what the method passed it, and the assertion only mirrors whatever the code
# already does — which is exactly how `POST shipments/{id}/cancel` reached
# prod. ShipStation answers that with 405, so the void leg of Cancel Order had
# never once succeeded; it surfaced on 2026-09-11, when an operator cancelled
# an already-pushed order (a past invoice) and the invoice cancelled
# correctly while the ShipStation shipment stayed live.
#
# So the table is not another mirror: each row carries the operationId it
# implements, from the published spec
# (https://github.com/ShipEngine/shipengine-openapi — documented there under
# /v1/*; ShipStation V2 serves the same operations under /v2/* with the same
# verbs, and every GET/POST in this client already agrees with it). Editing a
# row means contradicting a cited contract in the same diff, where a reviewer
# can see it, instead of quietly flipping an assertion three files away.
_WIRE_CONTRACT = (
	dict(method="create_shipment", op="create_shipments",
		verb="POST", path="shipments",
		returns={"shipments": [{"shipment_id": "se-1"}]},
		call=lambda c: c.create_shipment({"service_code": "usps_priority_mail"})),
	dict(method="find_shipment_by_external_id", op="list_shipments",
		verb="GET", path="shipments",
		returns={"shipments": []},
		call=lambda c: c.find_shipment_by_external_id("ACC-SINV-1")),
	dict(method="cancel_shipment", op="cancel_shipments",
		verb="PUT", path="shipments/se-1/cancel",
		returns=None,
		call=lambda c: c.cancel_shipment("se-1")),
	dict(method="labels_for_shipment", op="list_labels",
		verb="GET", path="labels",
		returns={"labels": []},
		call=lambda c: c.labels_for_shipment("se-1")),
	dict(method="labels_for_tracking", op="list_labels",
		verb="GET", path="labels",
		returns={"labels": []},
		call=lambda c: c.labels_for_tracking("879")),
	dict(method="probe_connection", op="list_carriers",
		verb="GET", path="carriers",
		returns={"carriers": []},
		call=lambda c: c.probe_connection()),
	dict(method="list_warehouses", op="list_warehouses",
		verb="GET", path="warehouses",
		returns={"warehouses": []},
		call=lambda c: c.list_warehouses()),
)


def _methods_that_call_request() -> set[str]:
	"""Names of ShipStationClient methods whose body reaches `self._request`.

	Parsed from the shipped source rather than listed by hand, so the coverage
	test below cannot be satisfied by forgetting to update it."""
	tree = ast.parse(inspect.getsource(client_module))
	cls = next(node for node in ast.walk(tree)
		if isinstance(node, ast.ClassDef) and node.name == "ShipStationClient")
	found = set()
	for node in cls.body:
		if not isinstance(node, ast.FunctionDef) or node.name == "_request":
			continue
		if any(isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
				and sub.func.attr == "_request" for sub in ast.walk(node)):
			found.add(node.name)
	return found


class TestWireContract(FrappeTestCase):
	def test_every_call_uses_the_documented_verb_and_path(self):
		for row in _WIRE_CONTRACT:
			with self.subTest(method=row["method"]):
				client, _ = _client()
				with patch.object(client, "_request", return_value=row["returns"]) as req:
					row["call"](client)
				verb, path = req.call_args.args[:2]
				self.assertEqual(verb, row["verb"],
					msg=(f"{row['method']} sent {verb}; ShipEngine "
						f"{row['op']} is {row['verb']}"))
				self.assertEqual(path, row["path"],
					msg=f"{row['method']} sent path {path!r}")

	def test_the_table_covers_every_method_that_talks_to_shipstation(self):
		"""A new client method that reaches `_request` must land in the table.
		Without this, the contract above degrades into a snapshot of whichever
		calls happened to exist when it was written — and the next endpoint
		goes out unpinned, which is the state cancel_shipment was in."""
		self.assertEqual(
			_methods_that_call_request(),
			{row["method"] for row in _WIRE_CONTRACT},
			msg=("add the new ShipStationClient method to _WIRE_CONTRACT with "
				"the ShipEngine operationId it implements"))
