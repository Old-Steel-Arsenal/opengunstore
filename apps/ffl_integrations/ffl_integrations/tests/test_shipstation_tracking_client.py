"""ShipStationClient.labels_for_shipment — the read-only GET the consignment
tracking poll uses to discover a bought label's tracking number.

No live account: _request is patched, so no network traffic. Written RED-first —
labels_for_shipment does not exist yet.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from ffl_integrations.shipstation.client import ShipStationClient, ShipStationConfig


def _cfg() -> ShipStationConfig:
	return ShipStationConfig(
		api_key="TEST_key", base_url="https://api.shipstation.com/v2",
		store_id="", ship_from_warehouse_id="", default_parcel_weight_oz=16.0,
		timeout=5, sandbox_mode=True)


def _client() -> ShipStationClient:
	return ShipStationClient(_cfg(), session=MagicMock())


class TestLabelsForShipment(FrappeTestCase):
	def test_queries_labels_by_shipment_id_and_returns_them(self):
		client = _client()
		body = {"labels": [{
			"label_id": "se-label-1", "tracking_number": "1Z999AA10123456784",
			"carrier_code": "ups", "tracking_url": "https://track/1Z999",
			"status": "completed", "tracking_status": "in_transit"}]}
		with patch.object(client, "_request", return_value=body) as req:
			labels = client.labels_for_shipment("se-77")
		req.assert_called_once_with("GET", "labels", params={"shipment_id": "se-77"})
		self.assertEqual(len(labels), 1)
		self.assertEqual(labels[0]["tracking_number"], "1Z999AA10123456784")
		self.assertEqual(labels[0]["carrier_code"], "ups")

	def test_voided_labels_are_filtered_out(self):
		"""A voided label is a dead reprint — its tracking must never win."""
		client = _client()
		body = {"labels": [
			{"label_id": "a", "tracking_number": "VOIDED-OLD", "status": "voided"},
			{"label_id": "b", "tracking_number": "LIVE-NEW", "status": "completed"}]}
		with patch.object(client, "_request", return_value=body):
			labels = client.labels_for_shipment("se-1")
		self.assertEqual([l["tracking_number"] for l in labels], ["LIVE-NEW"])

	def test_voided_boolean_flag_is_also_filtered(self):
		client = _client()
		body = {"labels": [
			{"label_id": "a", "tracking_number": "OLD", "voided": True},
			{"label_id": "b", "tracking_number": "NEW", "status": "completed"}]}
		with patch.object(client, "_request", return_value=body):
			labels = client.labels_for_shipment("se-1")
		self.assertEqual([l["tracking_number"] for l in labels], ["NEW"])

	def test_empty_missing_or_none_body_returns_empty_list(self):
		client = _client()
		for body in ({"labels": []}, {}, None):
			with patch.object(client, "_request", return_value=body):
				self.assertEqual(client.labels_for_shipment("se-1"), [])


class TestLabelsForTracking(FrappeTestCase):
	def test_queries_labels_by_tracking_number_and_drops_voided(self):
		client = _client()
		body = {"labels": [
			{"label_id": "a", "tracking_number": "879", "status": "voided"},
			{"label_id": "b", "tracking_number": "879", "status": "completed"}]}
		with patch.object(client, "_request", return_value=body) as req:
			labels = client.labels_for_tracking("879")
		req.assert_called_once_with("GET", "labels", params={"tracking_number": "879"})
		self.assertEqual([l["label_id"] for l in labels], ["b"])
