"""RSR provider catalog reads via the remote catalog (Task 3): search,
catalog_row, feed_heartbeat/catalog_content_heartbeat, prefetch_rows and
blocked_states all go through ffl_integrations.distributor.remote_catalog
instead of the local RSR Catalog Item / RSR Ship Restriction tables (Task 2's
transport + in-memory test stub)."""
from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.distributor import remote_catalog
from ffl_integrations.distributor.rsr_provider import RSRProvider
from ffl_integrations.tests import catalog_stub


class TestRemoteCatalogProvider(FrappeTestCase):
	def setUp(self):
		self.stub = catalog_stub.install()
		catalog_stub.seed_catalog([
			{"rsr_stock_number": "GLK19", "upc": "764503037082", "dealer_price": 445.5,
			 "map_price": 499.0, "msrp": 599.0, "quantity": 12,
			 "department_number": "01", "ffl_required": 1,
			 "short_description": "GLOCK 19 GEN5", "manufacturer": "GLOCK"},
			{"rsr_stock_number": "DEADSKU", "status_flag": "Deleted",
			 "short_description": "GLOCK OLD"},
		])
		self.provider = RSRProvider()

	def tearDown(self):
		catalog_stub.uninstall()

	# ---------------------------------------------------------------- catalog_row

	def test_catalog_row_normalises(self):
		row = self.provider.catalog_row("GLK19")
		self.assertEqual(row["sku"], "GLK19")
		self.assertEqual(row["cost"], 445.5)
		self.assertEqual(row["department"], "01")
		self.assertFalse(row["discontinued"])

	def test_deleted_status_folds_into_discontinued(self):
		self.assertTrue(self.provider.catalog_row("DEADSKU")["discontinued"])

	def test_unknown_sku_is_none(self):
		self.assertIsNone(self.provider.catalog_row("NOPE"))

	# -------------------------------------------------------------------- search

	def test_search_flattens_hits_to_rows(self):
		rows = self.provider.search("GLOCK", limit=10)
		self.assertEqual(rows[0]["sku"], "GLK19")
		self.assertEqual(rows[0]["item_name"], "GLOCK 19 GEN5")
		self.assertEqual(rows[0]["cost"], 445.5)
		self.assertNotIn("DEADSKU", [r["sku"] for r in rows])

	def test_search_degrades_to_empty_list_when_data_service_unavailable(self):
		"""Binding constraint: typeahead must never raise into the caller because
		the Data Service is down — log it and hand back []."""
		with mock.patch.object(remote_catalog, "search",
				side_effect=remote_catalog.DataServiceUnavailable("down")), \
				mock.patch.object(frappe, "log_error") as log_error:
			rows = self.provider.search("GLOCK", limit=10)
		self.assertEqual(rows, [])
		log_error.assert_called_once()

	# ------------------------------------------------------------- heartbeats

	def test_feed_heartbeat_parses_to_naive_local(self):
		hb = self.provider.feed_heartbeat()
		self.assertIsNotNone(hb)
		self.assertIsNone(hb.tzinfo)

	def test_catalog_content_heartbeat_parses_to_naive_local(self):
		hb = self.provider.catalog_content_heartbeat()
		self.assertIsNotNone(hb)
		self.assertIsNone(hb.tzinfo)

	# ------------------------------------------------------------- prefetch_rows

	def test_prefetch_rows_populates_the_request_memo(self):
		"""Cheap contract check only — remote_catalog's own memo semantics are
		already covered by test_remote_catalog.py (Task 2)."""
		self.provider.prefetch_rows(["GLK19"])
		self.assertIsNotNone(self.provider.catalog_row("GLK19"))

	# ------------------------------------------------------------- blocked_states

	def test_blocked_states_reads_through_to_the_remote_restrictions_feed(self):
		"""Migrated from the old TestBlockedStatesDelegation mock: pins that the
		permit/blocking derivation stayed in rsr/restrictions.py even though the
		raw entries now come from the remote catalog stub — H is a
		recipient-permit code and must NOT block."""
		self.stub.seed_restrictions("GLK19", [
			["ALL", "NY", "S", "F"], ["ALL", "MA", "S", "H"]])
		self.assertEqual(self.provider.blocked_states("GLK19"), ["NY"])
