"""Ship on a GunBroker order before Dispose, as on a counter order.

Before Dispose there is no invoice, so ``push_gb_shipment`` builds the shipment
off the order and keeps its id there; revenue copies it onto the invoice when
Dispose raises one, which is where the queue and the tracking write-back read
it from then on.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe

from ffl_integrations.gunbroker import fulfillment, revenue
from ffl_integrations.shipstation import api as ss_api
from ffl_integrations.tests import gunbroker_fixtures as fx
from ffl_integrations.tests.test_gunbroker_fulfillment import _FulfillmentCase

DEALER = "1-99-888-01-2A-34567"


class _ShipCase(_FulfillmentCase):
	def setUp(self):
		super().setUp()
		self.locked = frappe._dict(shipstation_order_id=None, shipstation_status=None)
		self.writes: list[tuple] = []
		self._patch_object(fulfillment, "_shipstation_enabled", return_value=True)
		self.resolve = self._patch_object(fulfillment, "_resolve_and_validate_ffl",
			return_value=DEALER)
		self._patch_object(fulfillment, "_require_valid_ffl")
		self._patch("frappe.db.set_value",
			side_effect=lambda *a, **kw: self.writes.append(a))
		real_get_value = frappe.db.get_value

		def _get_value(doctype, name=None, fields=None, *a, **kw):  # noqa: ARG001
			if doctype == fulfillment.ORDER_DOCTYPE:
				return self.locked
			if doctype == "ATF FFL Record":
				return frappe._dict(licensee_name="FAR END GUNS LLC", licensee_dba=None,
					street="900 Dealer Row", street2=None, city="Austin", state="TX",
					zip="78701", phone="512-555-0100")
			if doctype == "Item":
				return None if isinstance(fields, list) else "Colt Python"
			return real_get_value(doctype, name, fields, *a, **kw)

		self._patch("frappe.db.get_value", side_effect=_get_value)
		self.client = MagicMock()
		self.client.config = MagicMock(default_parcel_weight_oz=16.0,
			ship_from_warehouse_id="se-wh-1", store_id=None)
		self.client.find_shipment_by_external_id.return_value = None
		self.client.create_shipment.return_value = {"shipment_id": "se-777"}
		self._patch("ffl_integrations.shipstation.client.ShipStationClient.from_settings",
			return_value=self.client)

	def _paid(self, **fields):
		fields = {"ffl_number": DEALER, "customer_name": "Dave Collector",
			"ship_cost": 45.0, "sales_tax_total": 100.99, **fields}
		order = self._order(**fields)
		self._patch_order_doc(order)
		return order


class TestShipBeforeDispose(_ShipCase):
	def test_a_paid_order_ships_to_the_dealer_off_the_order_itself(self):
		self._paid()
		result = fulfillment.push_gb_shipment("GB-ORD-00001")
		self.assertEqual(result, {"ok": True, "shipstation_order_id": "se-777"})
		body = self.client.create_shipment.call_args.args[0]
		self.assertEqual(body["external_shipment_id"], "GB-ORD-00001")
		self.assertEqual(body["ship_to"]["name"], "Dave Collector")
		self.assertEqual(body["ship_to"]["company_name"], "FAR END GUNS LLC")
		self.assertEqual(body["ship_to"]["address_line1"], "900 Dealer Row")
		self.assertEqual([i["sku"] for i in body["items"]], ["COLT-PYTHON"])
		self.assertIn("SN-001", body["items"][0]["name"])
		self.assertEqual(body["items"][0]["unit_price"], 1100.0)
		self.assertEqual(body["amount_paid"]["amount"], 1245.99)
		self.assertEqual(body["tax_paid"]["amount"], 100.99)
		self.assertEqual(body["shipping_paid"]["amount"], 45.0)
		self.assertEqual(body["internal_notes"], "Firearm serial(s): SN-001")
		self.assertIn((fulfillment.ORDER_DOCTYPE, "GB-ORD-00001",
			{"shipstation_order_id": "se-777", "shipstation_status": "Pending"}), self.writes)

	def test_an_unpaid_order_is_refused_before_shipstation_is_called(self):
		self._paid(payment_mode_used="")
		with self.assertRaises(frappe.ValidationError):
			fulfillment.push_gb_shipment("GB-ORD-00001")
		self.client.create_shipment.assert_not_called()

	def test_a_second_push_is_a_no_op(self):
		self._paid()
		self.locked.shipstation_order_id = "se-777"
		result = fulfillment.push_gb_shipment("GB-ORD-00001")
		self.assertTrue(result["already"])
		self.client.create_shipment.assert_not_called()

	def test_a_shipment_made_by_a_lost_response_is_adopted(self):
		self._paid()
		self.client.find_shipment_by_external_id.return_value = {"shipment_id": "se-555"}
		result = fulfillment.push_gb_shipment("GB-ORD-00001")
		self.assertEqual(result["shipstation_order_id"], "se-555")
		self.assertTrue(result["already"])
		self.client.create_shipment.assert_not_called()

	def test_an_unverified_cr_order_must_dispose_first(self):
		self._paid(is_cr_transfer=1, atf_ffl_record="")
		with self.assertRaises(frappe.ValidationError):
			fulfillment.push_gb_shipment("GB-ORD-00001")
		self.resolve.assert_not_called()
		self.client.create_shipment.assert_not_called()

	def test_a_disposed_order_pushes_its_invoice(self):
		self._paid(order_status="Transferred", sales_invoice="ACC-SINV-9")
		with patch.object(ss_api, "push_shipment", return_value={"ok": True}) as push:
			fulfillment.push_gb_shipment("GB-ORD-00001")
		push.assert_called_once_with("ACC-SINV-9")
		self.client.create_shipment.assert_not_called()


class TestTheShipmentFollowsTheOrder(_FulfillmentCase):
	def test_the_queue_reads_the_orders_shipment_until_the_invoice_has_one(self):
		order = fx.FakeDoc(shipstation_order_id="se-777", shipstation_status="Pending")
		self.assertEqual(fulfillment._shipment(order, {}), ("se-777", "Pending"))
		self.assertEqual(fulfillment._shipment(order,
			{"shipstation_order_id": "se-888", "shipstation_status": "Pending"}),
			("se-888", "Pending"))

	def test_the_invoice_takes_over_a_shipment_made_before_dispose(self):
		order = self._order(order_status="Transferred", atf_ffl_record=DEALER,
			shipstation_order_id="se-777", shipstation_status="Pending")
		si = fx.FakeDoc("Sales Invoice", shipstation_status="Not Requested")
		with patch.object(revenue, "_tax_rows", return_value=[]), \
				patch.object(revenue, "_licensee_name", return_value="FAR END GUNS LLC"):
			revenue._apply_derived(si, order, "TSA")
		self.assertEqual(si.shipstation_order_id, "se-777")
		self.assertEqual(si.shipstation_status, "Pending")
