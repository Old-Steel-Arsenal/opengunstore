from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from ffl_integrations.distributor import remote_catalog
from ffl_integrations.tests import catalog_stub


class TestRemoteCatalogStub(FrappeTestCase):
	def setUp(self):
		self.stub = catalog_stub.install()

	def tearDown(self):
		catalog_stub.uninstall()

	def test_seed_aliases_normalize(self):
		catalog_stub.seed_catalog([{
			"rsr_stock_number": "GLK19", "upc": "764503037082",
			"dealer_price": 445.5, "map_price": 499.0, "msrp": 599.0,
			"quantity": 12, "department_number": "01", "ffl_required": 1,
			"short_description": "GLOCK 19 GEN5",
		}])
		row = remote_catalog.cached_row("rsr", "GLK19")
		self.assertEqual(row["cost"], 445.5)
		self.assertEqual(row["map"], 499.0)
		self.assertEqual(row["department"], "01")
		self.assertEqual(row["qty"], 12)
		self.assertTrue(row["ffl_required"])
		self.assertEqual(row["long_description"], "")  # default filled

	def test_unknown_sku_is_none_and_memoized(self):
		self.assertIsNone(remote_catalog.cached_row("rsr", "NOPE"))
		catalog_stub.seed_catalog([
			{"rsr_stock_number": "NOPE", "short_description": "NOW EXISTS"},
			{"rsr_stock_number": "OTHER", "short_description": "FRESH ROW"},
		])
		# the miss was memoized in-request; freshly-seeded data for the SAME
		# key must not be re-fetched within this test
		self.assertIsNone(remote_catalog.cached_row("rsr", "NOPE"))
		# a different sku was never memoized, so it sees the just-seeded row
		other = remote_catalog.cached_row("rsr", "OTHER")
		self.assertIsNotNone(other)
		self.assertEqual(other["short_description"], "FRESH ROW")

	def test_missing_stub_raises_loudly(self):
		catalog_stub.uninstall()
		with catalog_stub.data_service_configured(), \
				self.assertRaises(remote_catalog.CatalogStubMissing):
			remote_catalog.cached_row("rsr", "GLK19")

	def test_search_finds_by_substring_and_excludes_deleted(self):
		catalog_stub.seed_catalog([
			{"rsr_stock_number": "GLK19", "manufacturer": "GLOCK",
				"short_description": "GLOCK 19 GEN5"},
			{"rsr_stock_number": "GLKX", "manufacturer": "GLOCK",
				"short_description": "GLOCK OLD", "status_flag": "Deleted"},
		])
		hits = remote_catalog.search("rsr", "glock", 10)
		skus = sorted(h["best_offer"]["sku"] for h in hits)
		self.assertEqual(skus, ["GLK19"])

	def test_house_search_skips_a_hit_with_no_best_offer(self):
		"""The service sends best_offer=null when every offer on a hit belongs to a
		stale house. That hit has nothing to buy; the other hits must still come back."""
		from ffl_integrations.distributor.rsr_provider import RSRProvider

		catalog_stub.seed_catalog([{"rsr_stock_number": "GLK19", "short_description": "GLOCK 19"}])
		live = remote_catalog.search("rsr", "glock", 10)
		stale = {"upc": "1", "offers": [dict(live[0]["best_offer"], sku="STALE")], "best_offer": None}
		with patch.object(remote_catalog, "search", return_value=[stale, *live]):
			rows = RSRProvider().search("glock", 10)
		self.assertEqual([r["sku"] for r in rows], ["GLK19"])

	def test_health_reports_totals_and_honors_override(self):
		catalog_stub.seed_catalog([{"rsr_stock_number": "A"}, {"rsr_stock_number": "B"}])
		health = remote_catalog.health("rsr")
		self.assertEqual(health["offers_total"], 2)
		self.stub.set_health("rsr", offers_total=999)
		health2 = remote_catalog.health("rsr")
		self.assertEqual(health2["offers_total"], 999)

	def test_item_by_upc_returns_product_and_offers_or_none(self):
		catalog_stub.seed_catalog([
			{"rsr_stock_number": "GLK19", "upc": "764503037082",
				"manufacturer": "GLOCK", "short_description": "GLOCK 19 GEN5"},
			{"rsr_stock_number": "ORPHAN1"},  # no upc -> normalizes to None
		])
		found = remote_catalog.item_by_upc("764503037082")
		self.assertEqual(found["product"]["manufacturer"], "GLOCK")
		self.assertEqual(len(found["offers"]), 1)
		self.assertIsNone(remote_catalog.item_by_upc("000000000000"))
		# orphan-aggregation guard: neither the transport nor the stub may
		# treat None/"" as "match every row with no upc"
		self.assertIsNone(remote_catalog.item_by_upc(None))
		self.assertIsNone(self.stub.item_by_upc(None))

	def test_restrictions_entries_seeded_or_empty(self):
		self.stub.seed_restrictions("GLK19", [{"state": "NY", "note": "blocked"}])
		self.assertEqual(
			remote_catalog.restrictions_entries("rsr", "GLK19"),
			[{"state": "NY", "note": "blocked"}])
		self.assertEqual(remote_catalog.restrictions_entries("rsr", "NOPE"), [])
