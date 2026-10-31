"""A customer's transferred-in gun is held in CUSTODY, never as owned stock.

INCIDENT (2026-07-19).  A customer transfer was received the same way
a purchase is: into the sellable warehouse, at a made-up cost.  Staff had to put
*something* in the cost box to get the receive through, so they typed $10.  That
produced three separate wrongs at once:

  1. the gun sat in ``Stores`` — the shared, sellable warehouse — so every
     count that asks "how many of this SKU can we sell?" counted a gun the
     store does not own;
  2. $10 of a customer's property entered the books as inventory asset and,
     at delivery, as COGS — phantom profit on a gun we never bought;
  3. $10 hung in Stock Received But Not Billed against a bill that will never
     arrive (reversed by hand).

An earlier fix stopped the placeholder from repricing the shared SKU.  That was
the bleeding-edge fix; this is the structural one.  A transfer gun now:

  * lands in a dedicated custody warehouse (never the sellable one),
  * is received at rate 0 with zero-valuation allowed — so it posts NO GL at
    all: no asset, no SRBNB, and later no COGS,
  * carries ``Serial No.is_transfer_gun = 1`` — the flag every downstream
    "is this ours to sell?" check consults.

The three are deliberately independent.  Any one of them alone would have
stopped the incident; all three mean a single mistake cannot resurrect it.
"""
from __future__ import annotations

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch

from ffl_core.api.receive_goods import create_receive
from ffl_core.firearm import is_transfer_intake
from ffl_core.setup import custody
from ffl_core.warehouses import (
	custody_warehouse_names,
	default_stock_warehouse,
	ensure_custody_warehouse,
	is_custody_warehouse,
)
from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError


def _company() -> str:
	return (frappe.new_doc("Purchase Receipt").company
			or frappe.db.get_single_value("Global Defaults", "default_company"))


# ===========================================================================
# The custody warehouse
# ===========================================================================

class TestCustodyWarehouse(FrappeTestCase):
	"""A place to put things that are not ours.  Deliberately generic:
	repair drop-offs and layaway hold the same legal shape as a transfer — the
	customer owns it, we possess it — so the WAREHOUSE is "customer custody"
	while the transfer-specific meaning lives on the serial's own flag."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		custody.ensure()
		cls.company = _company()

	def test_the_custody_warehouse_is_created_on_demand(self):
		name = ensure_custody_warehouse(self.company)
		self.assertTrue(frappe.db.exists("Warehouse", name))

	def test_creating_it_twice_returns_the_same_warehouse(self):
		"""Idempotent: it is called on every transfer receive, and a second
		warehouse would silently split custody stock across two locations."""
		self.assertEqual(
			ensure_custody_warehouse(self.company),
			ensure_custody_warehouse(self.company))

	def test_it_is_a_leaf_warehouse_stock_can_actually_land_in(self):
		row = frappe.db.get_value(
			"Warehouse", ensure_custody_warehouse(self.company),
			["is_group", "parent_warehouse", "company"], as_dict=True)
		self.assertEqual(row.is_group, 0)
		self.assertTrue(row.parent_warehouse)
		self.assertEqual(row.company, self.company)

	def test_it_is_flagged_so_queries_can_find_it_without_matching_on_its_name(self):
		"""Every exclusion downstream keys off the FLAG, never off the label —
		a site that renames the warehouse must not silently start selling
		customer property."""
		name = ensure_custody_warehouse(self.company)
		self.assertTrue(is_custody_warehouse(name))
		self.assertIn(name, custody_warehouse_names())

	def test_an_ordinary_warehouse_is_not_custody(self):
		self.assertFalse(is_custody_warehouse(default_stock_warehouse(self.company)))
		self.assertNotIn(
			default_stock_warehouse(self.company), custody_warehouse_names())

	def test_the_lookup_survives_a_mocked_query_layer(self):
		"""This set is read from inside code paths
		that several long-standing suites cover while mocking Frappe's query
		layer wholesale. Routed through ``frappe.get_all`` it dragged in the
		permission/meta machinery, which on a COLD meta cache calls
		``frappe.db.get_value`` — so the lookup came back holding those suites'
		fixture rows and corrupted Frappe's own Meta object ("'Meta' object has
		no attribute 'istable'"). Reading one indexed column directly keeps the
		answer true no matter what the caller has stubbed."""
		name = ensure_custody_warehouse(self.company)
		with patch("frappe.db.get_value", return_value=frappe._dict(bogus=1)):
			self.assertIn(name, custody_warehouse_names())

	def test_the_default_stock_warehouse_is_never_the_custody_one(self):
		"""The counter's fallback warehouse resolver must not be able to hand
		back custody — that would route ordinary purchases, consignment returns
		and every other intake into a warehouse holding customer property
		(exactly the trap ``consignment_dealer`` warehouses are excluded for)."""
		ensure_custody_warehouse(self.company)
		self.assertFalse(is_custody_warehouse(default_stock_warehouse(self.company)))


# ===========================================================================
# The receive path locks warehouse + rate + flag
# ===========================================================================

class TransferReceiveIsHeldInCustody(FrappeTestCase):
	ITEM = "TCU-UNIFORM-GLOCK"
	REAL_PRICE = 589.68
	REAL_COST = 420.00

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		custody.ensure()

		cls.company = _company()
		cls.warehouse = default_stock_warehouse(cls.company)
		cls.custody_warehouse = ensure_custody_warehouse(cls.company)

		if not frappe.db.exists("Supplier Group", "Distributor"):
			frappe.get_doc({"doctype": "Supplier Group", "supplier_group_name": "Distributor",
							"parent_supplier_group": "All Supplier Groups",
							"is_group": 0}).insert(ignore_permissions=True)
		if not frappe.db.exists("Supplier", "TCU-FFL"):
			frappe.get_doc({"doctype": "Supplier", "supplier_name": "TCU-FFL",
							"supplier_group": "Distributor", "is_ffl_dealer": 1,
							"ffl_number": "1-55-555-55-5X-55555",
							"ez_check_status": "Verified"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Customer", "TCU-Transfer-Customer"):
			frappe.get_doc({"doctype": "Customer",
							"customer_name": "TCU-Transfer-Customer"}).insert(ignore_permissions=True)

	def setUp(self) -> None:
		super().setUp()
		p = patch.object(FastBoundClient, "from_settings",
						 side_effect=FastBoundConfigError("disabled in custody tests"))
		p.start()
		self.addCleanup(p.stop)
		if not frappe.db.exists("Item", self.ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": self.ITEM, "item_name": self.ITEM,
				"item_group": "Pistol", "stock_uom": "Nos", "is_stock_item": 1,
				"has_serial_no": 1, "serial_no_series": f"{self.ITEM}-.####",
			}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", self.ITEM, {
			"uniform_sell_price": 1,
			"standard_rate": self.REAL_PRICE,
			"dealer_price": self.REAL_COST,
		}, update_modified=False)

	def _receive(self, serial, *, transfer=False, rate=10.0, sell_price=35.0):
		"""The incident's own numbers by default: a $10 cost / $35 price
		placeholder pair typed to get a customer's gun through the form."""
		payload = {
			"supplier": "TCU-FFL",
			"source": "Distributor",
			"warehouse": self.warehouse,
			"items": [{
				"item_code": self.ITEM, "qty": 1, "rate": rate,
				"serials": [serial], "sell_price": sell_price,
				"manufacturer": "Glock", "model_name": "19",
				"firearm_type": "Pistol", "caliber": "9mm",
			}],
		}
		if transfer:
			payload["is_ffl_transfer"] = 1
			payload["transfer_for_customer"] = "TCU-Transfer-Customer"
			payload["acquisition_type"] = "Transfer"
		return create_receive(json.dumps(payload))

	def _pr_row(self, res):
		return frappe.db.get_value(
			"Purchase Receipt Item", {"parent": res["name"]},
			["warehouse", "rate", "amount", "allow_zero_valuation_rate"], as_dict=True)

	# -- warehouse lock ------------------------------------------------------

	def test_a_transfer_gun_lands_in_custody_not_the_sellable_warehouse(self):
		res = self._receive("TCU-XFER-1", transfer=True)
		self.assertEqual(self._pr_row(res).warehouse, self.custody_warehouse)

	def test_the_requested_warehouse_is_overridden_not_merely_defaulted(self):
		"""The payload explicitly asks for the sellable warehouse (the page
		always sends one).  A transfer must ignore it — otherwise the lock is
		only as good as the caller's manners, which is how the incident happened."""
		res = self._receive("TCU-XFER-2", transfer=True)
		self.assertNotEqual(self._pr_row(res).warehouse, self.warehouse)

	def test_an_ordinary_purchase_still_uses_the_requested_warehouse(self):
		res = self._receive("TCU-BUY-1", transfer=False, rate=400.0, sell_price=650.0)
		self.assertEqual(self._pr_row(res).warehouse, self.warehouse)

	# -- rate lock -----------------------------------------------------------

	def test_a_transfer_placeholder_cost_is_forced_to_zero(self):
		"""$10 of a customer's property must never become inventory value."""
		res = self._receive("TCU-XFER-3", transfer=True, rate=10.0)
		row = self._pr_row(res)
		self.assertEqual(float(row.rate), 0.0)
		self.assertEqual(float(row.amount), 0.0)

	def test_a_zero_rate_transfer_line_allows_zero_valuation(self):
		"""Without this flag ERPNext refuses the $0 stock movement outright —
		the receive would fail instead of booking nothing."""
		self.assertEqual(
			self._pr_row(self._receive("TCU-XFER-4", transfer=True)).allow_zero_valuation_rate, 1)

	def test_an_ordinary_purchase_keeps_its_real_cost(self):
		res = self._receive("TCU-BUY-2", transfer=False, rate=400.0, sell_price=650.0)
		self.assertEqual(float(self._pr_row(res).rate), 400.0)

	# -- the serial flag -----------------------------------------------------

	def test_a_transfer_gun_is_flagged_on_its_serial(self):
		self._receive("TCU-XFER-5", transfer=True)
		self.assertEqual(
			frappe.db.get_value("Serial No", "TCU-XFER-5", "is_transfer_gun"), 1)

	def test_a_purchased_gun_is_not_flagged(self):
		"""Must stay 0: the flag suppresses the gun everywhere online, so a
		false positive silently un-sells real inventory."""
		self._receive("TCU-BUY-3", transfer=False, rate=400.0, sell_price=650.0)
		self.assertFalse(
			frappe.db.get_value("Serial No", "TCU-BUY-3", "is_transfer_gun"))

	def test_a_transfer_gun_carries_no_cost_on_its_serial(self):
		"""Supersedes the earlier behaviour that PRESERVED the typed
		placeholder on the serial.  There is no longer a placeholder to
		preserve: a custody gun's cost to this store is zero, and the sales
		report reads purchase_rate as cost."""
		self._receive("TCU-XFER-6", transfer=True, rate=10.0)
		self.assertFalse(
			float(frappe.db.get_value("Serial No", "TCU-XFER-6", "purchase_rate") or 0))

	def test_a_transfer_gun_carries_no_sell_price(self):
		"""It is not for sale at any price — an un-priced serial (0 = unpriced
		by this system's convention) is also refused by the Woo listing guard."""
		self._receive("TCU-XFER-7", transfer=True, sell_price=35.0)
		self.assertFalse(
			float(frappe.db.get_value("Serial No", "TCU-XFER-7", "sell_price") or 0))

	# -- the earlier guards must survive ---------------------------------------

	def test_the_shared_sku_price_is_still_protected(self):
		"""The earlier shared-SKU price guard, re-asserted here: this run rewrites the same
		code path, and the incident it fixed must not come back."""
		self._receive("TCU-XFER-8", transfer=True)
		self.assertEqual(
			float(frappe.db.get_value("Item", self.ITEM, "standard_rate")), self.REAL_PRICE)

	def test_the_shared_sku_cost_is_still_protected(self):
		self._receive("TCU-XFER-9", transfer=True)
		self.assertEqual(
			float(frappe.db.get_value("Item", self.ITEM, "dealer_price")), self.REAL_COST)


# ===========================================================================
# The checkbox is the declaration; a lone pickup contact is not
# ===========================================================================

class TestTransferDeclaration(FrappeTestCase):
	"""``is_transfer_intake`` gained an explicit third marker: the Receive
	Goods "FFL Transfer" checkbox.  The other two markers stay — they
	are what an API/MCP caller sends — but the checkbox is what the page
	writes, and what the whole custody lock now hangs off."""

	def test_the_checkbox_alone_marks_a_transfer(self):
		self.assertTrue(is_transfer_intake(None, None, True))

	def test_the_legacy_markers_still_mark_a_transfer(self):
		"""Backward compatibility: an MCP/API caller predating the checkbox
		sends acquisition_type='Transfer' and nothing else.  Reading that as
		an ordinary purchase would put a customer's gun back into stock."""
		self.assertTrue(is_transfer_intake("Transfer", None))
		self.assertTrue(is_transfer_intake(None, "CUST-0001"))

	def test_a_plain_receive_is_still_not_a_transfer(self):
		self.assertFalse(is_transfer_intake(None, None))
		self.assertFalse(is_transfer_intake("Purchase", None, False))

	def test_the_literal_prod_actually_uses_is_recognised(self):
		"""Prod's A&D vocabulary spells this **"FFL Transfer"**, not "Transfer"
		(confirmed against the live bound book, 2026-07-20). The original
		equality check — `== "transfer"` — therefore never once matched in
		production: every transfer this system has correctly identified was
		caught by the transfer_for_customer marker instead, and the
		acquisition_type arm of the earlier guard was dead code on the only site
		that matters.

		Nothing was lost to it (the two historical transfers both named a pickup
		customer), but it is exactly the silent half-guard this run exists to
		remove."""
		self.assertTrue(is_transfer_intake("FFL Transfer", None))
		self.assertTrue(is_transfer_intake("  ffl transfer  ", None))

	def test_a_purchase_is_not_caught_by_the_widened_match(self):
		"""The widening is a fixed vocabulary, not a substring search: matching
		anything merely CONTAINING "transfer" would hand zero-cost custody
		routing to acquisition types nobody has audited."""
		self.assertFalse(is_transfer_intake("Transfer to Dealer", None))
		self.assertFalse(is_transfer_intake("Purchase", None))


class TestAmbiguousTransferDeclarationIsRefused(FrappeTestCase):
	"""A pickup contact with no transfer declaration is ambiguous, and the
	custody lock makes ambiguity expensive: guessing "transfer" reroutes the
	warehouse and zeroes the cost of what might be a real purchase; guessing
	"purchase" is the incident again.  So it is refused, with an instruction.
	"""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		custody.ensure()
		if not frappe.db.exists("Customer", "TCU-Ambiguous-Customer"):
			frappe.get_doc({"doctype": "Customer",
							"customer_name": "TCU-Ambiguous-Customer"}).insert(ignore_permissions=True)

	def test_a_pickup_contact_without_the_checkbox_is_refused(self):
		from ffl_core.firearm import assert_transfer_declaration_unambiguous
		with self.assertRaises(frappe.ValidationError):
			assert_transfer_declaration_unambiguous(None, "TCU-Ambiguous-Customer", False)

	def test_the_refusal_names_the_checkbox_so_staff_know_what_to_do(self):
		from ffl_core.firearm import assert_transfer_declaration_unambiguous
		with self.assertRaises(frappe.ValidationError) as ctx:
			assert_transfer_declaration_unambiguous(None, "TCU-Ambiguous-Customer", False)
		self.assertIn("FFL Transfer", str(ctx.exception))

	def test_an_explicit_acquisition_type_resolves_the_ambiguity(self):
		"""acquisition_type='Transfer' IS a declaration — it is the marker the
		MCP receive tool sends.  Only a bare pickup contact is ambiguous."""
		from ffl_core.firearm import assert_transfer_declaration_unambiguous
		assert_transfer_declaration_unambiguous("Transfer", "TCU-Ambiguous-Customer", False)

	def test_the_checkbox_resolves_the_ambiguity(self):
		from ffl_core.firearm import assert_transfer_declaration_unambiguous
		assert_transfer_declaration_unambiguous(None, "TCU-Ambiguous-Customer", True)

	def test_an_ordinary_receive_is_unaffected(self):
		from ffl_core.firearm import assert_transfer_declaration_unambiguous
		assert_transfer_declaration_unambiguous("Purchase", None, False)


# ===========================================================================
# Zero-cost accounting
# ===========================================================================

class TransferReceivePostsNoGeneralLedger(FrappeTestCase):
	"""The books must not learn about a gun the store does not own.

	The incident's $10 produced an inventory asset debit and an SRBNB credit that
	had to be reversed by hand (reversed by hand).  At rate 0 there is no
	amount to post, so ERPNext writes no GL rows at all — asserted here rather
	than assumed, because it is the whole point of the rate lock."""

	ITEM = "TCU-GL-GLOCK"

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		custody.ensure()
		cls.company = _company()
		cls.warehouse = default_stock_warehouse(cls.company)
		if not frappe.db.exists("Supplier Group", "Distributor"):
			frappe.get_doc({"doctype": "Supplier Group", "supplier_group_name": "Distributor",
							"parent_supplier_group": "All Supplier Groups",
							"is_group": 0}).insert(ignore_permissions=True)
		if not frappe.db.exists("Supplier", "TCU-GL-FFL"):
			frappe.get_doc({"doctype": "Supplier", "supplier_name": "TCU-GL-FFL",
							"supplier_group": "Distributor", "is_ffl_dealer": 1,
							"ffl_number": "1-55-555-55-5X-55556",
							"ez_check_status": "Verified"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Customer", "TCU-GL-Customer"):
			frappe.get_doc({"doctype": "Customer",
							"customer_name": "TCU-GL-Customer"}).insert(ignore_permissions=True)

	def setUp(self) -> None:
		super().setUp()
		p = patch.object(FastBoundClient, "from_settings",
						 side_effect=FastBoundConfigError("disabled in custody tests"))
		p.start()
		self.addCleanup(p.stop)
		if not frappe.db.exists("Item", self.ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": self.ITEM, "item_name": self.ITEM,
				"item_group": "Pistol", "stock_uom": "Nos", "is_stock_item": 1,
				"has_serial_no": 1, "serial_no_series": f"{self.ITEM}-.####",
			}).insert(ignore_permissions=True)

	def _receive(self, serial, *, transfer, rate):
		payload = {
			"supplier": "TCU-GL-FFL", "source": "Distributor", "warehouse": self.warehouse,
			"items": [{
				"item_code": self.ITEM, "qty": 1, "rate": rate, "serials": [serial],
				"sell_price": 0, "manufacturer": "Glock", "model_name": "19",
				"firearm_type": "Pistol", "caliber": "9mm",
			}],
		}
		if transfer:
			payload["is_ffl_transfer"] = 1
			payload["transfer_for_customer"] = "TCU-GL-Customer"
			payload["acquisition_type"] = "Transfer"
		return create_receive(json.dumps(payload))

	def _gl(self, pr_name):
		return frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Purchase Receipt", "voucher_no": pr_name,
					 "is_cancelled": 0},
			fields=["account", "debit", "credit"])

	def test_a_transfer_receive_posts_no_gl_entries_at_all(self):
		res = self._receive("TCU-GL-XFER-1", transfer=True, rate=10.0)
		self.assertEqual(self._gl(res["name"]), [])

	def test_a_transfer_receive_parks_nothing_in_srbnb(self):
		"""The half that had to be reversed by hand.  Asserted separately from
		the blanket "no GL" test so a future change that reintroduces only the
		SRBNB leg still fails loudly."""
		res = self._receive("TCU-GL-XFER-2", transfer=True, rate=10.0)
		srbnb = frappe.db.get_value("Company", self.company, "stock_received_but_not_billed")
		self.assertEqual(
			[r for r in self._gl(res["name"]) if r["account"] == srbnb], [])

	def test_no_missing_account_popup_for_a_custody_receive(self):
		"""ERPNext tells the clerk "No accounting entries for the following warehouses: Customer
		Custody - …" for every zero-valued row; for custody that is the design, not a missing
		account, so the notice is dropped (gunsmith and transfer custody share this path)."""
		text = "No accounting entries for the following warehouses"
		frappe.clear_messages()
		self._receive("TCU-GL-XFER-4", transfer=True, rate=10.0)
		self.assertFalse([m for m in frappe.local.message_log if text in str(m)])
		# Only custody: the same notice on any other receipt is ERPNext's and stays.
		from ffl_core.api.receive_goods import _drop_no_accounting_notice
		frappe.local.message_log = [{"message": "keep"}, {"message": text + ": X"}]
		_drop_no_accounting_notice(1)
		self.assertEqual(frappe.local.message_log, [{"message": "keep"}])

	def test_a_transfer_gun_is_never_marked_pay_on_sale(self):
		"""pay_on_sale parks a cost in SRBNB to settle at sale.  A custody gun
		has no cost and will never be sold by this store."""
		self._receive("TCU-GL-XFER-3", transfer=True, rate=10.0)
		self.assertFalse(
			frappe.db.get_value("Serial No", "TCU-GL-XFER-3", "pay_on_sale"))

	def test_an_ordinary_purchase_still_posts_its_gl(self):
		"""The control.  A blanket "no GL" bug would otherwise look like a pass
		on every test above."""
		res = self._receive("TCU-GL-BUY-1", transfer=False, rate=400.0)
		self.assertTrue(self._gl(res["name"]))


class OnlyTheCheckboxRoutesAGunIntoCustody(FrappeTestCase):
	"""The narrowing forced by prod reality .

	This branch first treated a transfer-shaped ``acquisition_type`` as enough
	to route a receive into custody at zero cost. A prod read-only audit then
	found an acquisition: acquisition_type "FFL Transfer", NO pickup
	customer, a real cost, source an FFL dealer — an ordinary PURCHASE
	from another licensee. The store uses that AcquireType for buying guns.

	Had this shipped, every such purchase would have been silently rerouted
	into the custody warehouse, had its cost erased, and — with the mixed-line
	rule — refused outright if an accessory rode along. A day-one operational
	incident, caused by the fix for the previous incident.

	So the two halves are split by blast radius:

	  * **destructive** semantics — warehouse override, cost erasure, the
	    firearms-only rule, the unsellable serial flag — answer ONLY to the
	    explicit checkbox, which a human ticks deliberately;
	  * **protective** semantics — the suppression of SKU price/cost
	    write-back — keep reading every marker, because over-triggering there
	    costs nothing (a price simply isn't propagated) while under-triggering
	    is the original incident.

	Fail-closed is preserved where it matters and abandoned only where "closed"
	would itself be the damage."""

	ITEM = "TCU-AT-GLOCK"
	REAL_COST = 549.99      # a figure from a real purchase
	REAL_PRICE = 799.00

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		custody.ensure()
		cls.company = _company()
		cls.warehouse = default_stock_warehouse(cls.company)
		cls.custody_warehouse = ensure_custody_warehouse(cls.company)
		if not frappe.db.exists("Supplier Group", "Distributor"):
			frappe.get_doc({"doctype": "Supplier Group", "supplier_group_name": "Distributor",
							"parent_supplier_group": "All Supplier Groups",
							"is_group": 0}).insert(ignore_permissions=True)
		if not frappe.db.exists("Supplier", "TCU-AT-FFL"):
			frappe.get_doc({"doctype": "Supplier", "supplier_name": "TCU-AT-FFL",
							"supplier_group": "Distributor", "is_ffl_dealer": 1,
							"ffl_number": "1-55-555-55-5X-55558",
							"ez_check_status": "Verified"}).insert(ignore_permissions=True)

	def setUp(self) -> None:
		super().setUp()
		p = patch.object(FastBoundClient, "from_settings",
						 side_effect=FastBoundConfigError("disabled in custody tests"))
		p.start()
		self.addCleanup(p.stop)
		if not frappe.db.exists("Item", self.ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": self.ITEM, "item_name": self.ITEM,
				"item_group": "Pistol", "stock_uom": "Nos", "is_stock_item": 1,
				"has_serial_no": 1, "serial_no_series": f"{self.ITEM}-.####",
			}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", self.ITEM, {
			"uniform_sell_price": 1, "standard_rate": self.REAL_PRICE,
			"dealer_price": self.REAL_COST,
		}, update_modified=False)

	def _buy_with_transfer_acquisition_type(self, serial):
		"""That acquisition's exact shape: the transfer AcquireType on a real
		purchase — no checkbox, no pickup customer, a real price paid."""
		return create_receive(json.dumps({
			"supplier": "TCU-AT-FFL", "source": "Distributor",
			"warehouse": self.warehouse,
			"acquisition_type": "FFL Transfer",
			"items": [{
				"item_code": self.ITEM, "qty": 1, "rate": self.REAL_COST,
				"serials": [serial], "sell_price": 0,
				"manufacturer": "Glock", "model_name": "19",
				"firearm_type": "Pistol", "caliber": "9mm",
			}],
		}))

	def _pr_row(self, res):
		return frappe.db.get_value(
			"Purchase Receipt Item", {"parent": res["name"]},
			["warehouse", "rate"], as_dict=True)

	def test_a_purchase_using_the_transfer_type_stays_in_sellable_stock(self):
		res = self._buy_with_transfer_acquisition_type("TCU-AT-1")
		self.assertEqual(self._pr_row(res).warehouse, self.warehouse)

	def test_a_purchase_using_the_transfer_type_keeps_the_price_paid(self):
		"""The cost is what the store actually paid. Erasing it would delete a
		real inventory asset and invent margin on the eventual sale."""
		res = self._buy_with_transfer_acquisition_type("TCU-AT-2")
		self.assertEqual(float(self._pr_row(res).rate), self.REAL_COST)

	def test_a_purchase_using_the_transfer_type_stays_sellable(self):
		self._buy_with_transfer_acquisition_type("TCU-AT-3")
		self.assertFalse(
			frappe.db.get_value("Serial No", "TCU-AT-3", "is_transfer_gun"))

	def test_the_242_price_guard_still_fires_on_the_soft_marker(self):
		"""The protective half is deliberately NOT narrowed: a transfer-shaped
		marker still suppresses SKU price write-back, because guessing wrong
		there costs nothing."""
		from ffl_core.firearm import is_transfer_intake
		self.assertTrue(is_transfer_intake("FFL Transfer", None))

	def test_a_ticked_checkbox_still_routes_into_custody(self):
		"""The control: narrowing must not disarm the feature itself."""
		res = create_receive(json.dumps({
			"supplier": "TCU-AT-FFL", "source": "Distributor",
			"warehouse": self.warehouse, "is_ffl_transfer": 1,
			"acquisition_type": "FFL Transfer",
			"items": [{
				"item_code": self.ITEM, "qty": 1, "rate": 10.0,
				"serials": ["TCU-AT-CUSTODY-1"], "sell_price": 0,
				"manufacturer": "Glock", "model_name": "19",
				"firearm_type": "Pistol", "caliber": "9mm",
			}],
		}))
		row = self._pr_row(res)
		self.assertEqual(row.warehouse, self.custody_warehouse)
		self.assertEqual(float(row.rate), 0.0)
		self.assertEqual(
			frappe.db.get_value("Serial No", "TCU-AT-CUSTODY-1", "is_transfer_gun"), 1)

	def test_a_purchase_using_the_transfer_type_may_still_carry_accessories(self):
		"""The firearms-only rule is destructive too (it REFUSES a document),
		so it must not fire on a purchase either."""
		if not frappe.db.exists("Item", "TCU-AT-CASE"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "TCU-AT-CASE", "item_name": "TCU-AT-CASE",
				"item_group": "Accessory", "stock_uom": "Nos", "is_stock_item": 1,
				"has_serial_no": 0,
			}).insert(ignore_permissions=True)
		res = create_receive(json.dumps({
			"supplier": "TCU-AT-FFL", "source": "Distributor",
			"warehouse": self.warehouse, "acquisition_type": "FFL Transfer",
			"items": [
				{"item_code": self.ITEM, "qty": 1, "rate": self.REAL_COST,
				 "serials": ["TCU-AT-4"], "sell_price": 0, "manufacturer": "Glock",
				 "model_name": "19", "firearm_type": "Pistol", "caliber": "9mm"},
				{"item_code": "TCU-AT-CASE", "qty": 1, "rate": 25.0, "serials": []},
			],
		}))
		self.assertTrue(res["ok"])


class TheNearMissAlarmActuallyFires(FrappeTestCase):
	"""An alarm that never sounds is the defect it was built to prevent.

	``log_transfer_type_near_miss`` exists because the ``== "transfer"`` check
	sat in production for months, matching nothing, while reading like cover.
	Its entire value is firing at the right moment — so its three branches (the
	"transfer" gate, the exact-match exemption, the throttle) are asserted
	rather than assumed (review follow-up 3)."""

	def _clear(self, value):
		frappe.cache().delete_value(
			f"ffl_core:transfer_type_near_miss:{value.lower()}")

	def test_a_transfer_shaped_spelling_outside_the_vocabulary_is_reported(self):
		from ffl_core.firearm import log_transfer_type_near_miss
		self._clear("Transfer In (Consignment)")
		with patch("frappe.log_error") as log:
			log_transfer_type_near_miss("Transfer In (Consignment)")
		log.assert_called_once()

	def test_a_recognised_spelling_is_not_reported(self):
		""""FFL Transfer" IS in the vocabulary — reporting it would train
		operators to ignore the alarm."""
		from ffl_core.firearm import log_transfer_type_near_miss
		self._clear("FFL Transfer")
		with patch("frappe.log_error") as log:
			log_transfer_type_near_miss("FFL Transfer")
		log.assert_not_called()

	def test_an_unrelated_acquisition_type_is_not_reported(self):
		from ffl_core.firearm import log_transfer_type_near_miss
		self._clear("Purchase")
		with patch("frappe.log_error") as log:
			log_transfer_type_near_miss("Purchase")
		log.assert_not_called()

	def test_it_is_reported_once_an_hour_not_once_a_receive(self):
		"""A mis-configured AcquireType is a standing fact, not an event. Without
		the throttle one bad dropdown value buries every other Error Log."""
		from ffl_core.firearm import log_transfer_type_near_miss
		self._clear("Dealer Transfer Out")
		with patch("frappe.log_error") as log:
			log_transfer_type_near_miss("Dealer Transfer Out")
			log_transfer_type_near_miss("Dealer Transfer Out")
			log_transfer_type_near_miss("Dealer Transfer Out")
		self.assertEqual(log.call_count, 1)

	def test_a_broken_logger_never_blocks_a_receive(self):
		"""It sits on the goods-inwards path. A diagnostic must not be able to
		stop the store taking delivery."""
		from ffl_core.firearm import log_transfer_type_near_miss
		self._clear("Transfer Something")
		with patch("frappe.log_error", side_effect=RuntimeError("log is down")):
			log_transfer_type_near_miss("Transfer Something")  # must not raise


class ATransferReceiveHoldsOnlyTheCustomersFirearms(FrappeTestCase):
	"""``is_transfer`` is a property of the SHIPMENT, not of a line (the model
	this system has always used). Custody makes that consequential: ticking the
	box sends EVERY line to the custody warehouse at zero cost. A mixed
	document would therefore turn a purchased accessory into $0 stock the store
	owns but cannot see.

	The rule is to fail closed, because no legitimate mixed
	document exists. A non-firearm on a transfer receive is either the
	customer's own accessory — which is their property riding along with their
	gun, and has no business entering our inventory at all — or goods the store
	bought, which belong on their own receive. Both cases are answered by
	splitting the document, so the error says so."""

	ITEM_GUN = "TCU-MIX-GLOCK"
	ITEM_ACC = "TCU-MIX-HOLSTER"

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		custody.ensure()
		cls.company = _company()
		cls.warehouse = default_stock_warehouse(cls.company)
		ensure_custody_warehouse(cls.company)
		if not frappe.db.exists("Supplier Group", "Distributor"):
			frappe.get_doc({"doctype": "Supplier Group", "supplier_group_name": "Distributor",
							"parent_supplier_group": "All Supplier Groups",
							"is_group": 0}).insert(ignore_permissions=True)
		if not frappe.db.exists("Supplier", "TCU-MIX-FFL"):
			frappe.get_doc({"doctype": "Supplier", "supplier_name": "TCU-MIX-FFL",
							"supplier_group": "Distributor", "is_ffl_dealer": 1,
							"ffl_number": "1-55-555-55-5X-55557",
							"ez_check_status": "Verified"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Customer", "TCU-MIX-Customer"):
			frappe.get_doc({"doctype": "Customer",
							"customer_name": "TCU-MIX-Customer"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", cls.ITEM_GUN):
			frappe.get_doc({
				"doctype": "Item", "item_code": cls.ITEM_GUN, "item_name": cls.ITEM_GUN,
				"item_group": "Pistol", "stock_uom": "Nos", "is_stock_item": 1,
				"has_serial_no": 1, "serial_no_series": f"{cls.ITEM_GUN}-.####",
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", cls.ITEM_ACC):
			frappe.get_doc({
				"doctype": "Item", "item_code": cls.ITEM_ACC, "item_name": cls.ITEM_ACC,
				"item_group": "Accessory", "stock_uom": "Nos", "is_stock_item": 1,
				"has_serial_no": 0,
			}).insert(ignore_permissions=True)

	def setUp(self) -> None:
		super().setUp()
		p = patch.object(FastBoundClient, "from_settings",
						 side_effect=FastBoundConfigError("disabled in custody tests"))
		p.start()
		self.addCleanup(p.stop)

	def _payload(self, items, *, transfer):
		payload = {"supplier": "TCU-MIX-FFL", "source": "Distributor",
				   "warehouse": self.warehouse, "items": items}
		if transfer:
			payload["is_ffl_transfer"] = 1
			payload["transfer_for_customer"] = "TCU-MIX-Customer"
			payload["acquisition_type"] = "FFL Transfer"
		return json.dumps(payload)

	def _gun_line(self, serial, rate=0):
		# rate defaults to 0 because most callers here are TRANSFERS, where the
		# server forces 0 anyway and the number is never read. An ordinary
		# purchase must pass a real cost — receive_goods refuses an unconfirmed
		# $0 purchase (zero-cost guard).
		return {"item_code": self.ITEM_GUN, "qty": 1, "rate": rate, "serials": [serial],
				"sell_price": 0, "manufacturer": "Glock", "model_name": "19",
				"firearm_type": "Pistol", "caliber": "9mm"}

	def _acc_line(self, qty=1, rate=30.0):
		return {"item_code": self.ITEM_ACC, "qty": qty, "rate": rate, "serials": [],
				"sell_price": 60.0}

	def test_a_non_firearm_line_on_a_transfer_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			create_receive(self._payload(
				[self._gun_line("TCU-MIX-1"), self._acc_line()], transfer=True))

	def test_the_refusal_tells_staff_to_split_the_document(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(self._payload(
				[self._gun_line("TCU-MIX-2"), self._acc_line()], transfer=True))
		self.assertIn("separate receive document", str(ctx.exception))

	def test_the_refusal_names_the_offending_item(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(self._payload(
				[self._gun_line("TCU-MIX-3"), self._acc_line()], transfer=True))
		self.assertIn(self.ITEM_ACC, str(ctx.exception))

	def test_nothing_is_received_when_the_document_is_refused(self):
		"""Refused BEFORE the Purchase Receipt is built, so a rejected mixed
		document leaves no half-receive and no $0 accessory behind."""
		try:
			create_receive(self._payload(
				[self._gun_line("TCU-MIX-4"), self._acc_line()], transfer=True))
		except frappe.ValidationError:
			pass
		self.assertFalse(frappe.db.exists("Serial No", "TCU-MIX-4"))

	def test_a_firearms_only_transfer_is_accepted(self):
		res = create_receive(self._payload(
			[self._gun_line("TCU-MIX-OK-1")], transfer=True))
		self.assertTrue(res["ok"])

	def test_an_ordinary_purchase_may_still_mix_freely(self):
		"""The control. Ordinary receives are how the store buys guns and
		accessories on one invoice — the rule is transfer-only."""
		res = create_receive(self._payload(
			[self._gun_line("TCU-MIX-BUY-1", rate=400.0), self._acc_line()], transfer=False))
		self.assertTrue(res["ok"])


# ===========================================================================
# "Is this ours to sell?", the predicate every channel consults
# ===========================================================================

class TestIsCustodySerial(FrappeTestCase):
	"""Two independent keys, OR'd. A flag and a location can drift apart — a
	hand-run stock entry moves a gun without touching flags, a backfill sets
	flags without moving stock — and the two failure modes are not symmetric:
	wrongly hiding one of our guns costs a listing until someone notices;
	wrongly exposing a customer's gun sells property we do not own."""

	ITEM = "TCU-PRED-GLOCK"

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		custody.ensure()
		cls.company = _company()
		cls.custody_warehouse = ensure_custody_warehouse(cls.company)
		cls.warehouse = default_stock_warehouse(cls.company)
		if not frappe.db.exists("Item", cls.ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": cls.ITEM, "item_name": cls.ITEM,
				"item_group": "Pistol", "stock_uom": "Nos", "is_stock_item": 1,
				"has_serial_no": 1, "serial_no_series": f"{cls.ITEM}-.####",
			}).insert(ignore_permissions=True)

	def _serial(self, name, *, flag=0, warehouse=None):
		if not frappe.db.exists("Serial No", name):
			frappe.get_doc({
				"doctype": "Serial No", "serial_no": name, "item_code": self.ITEM,
				"company": self.company,
			}).insert(ignore_permissions=True)
		frappe.db.set_value("Serial No", name, {
			"is_transfer_gun": flag,
			"warehouse": warehouse if warehouse is not None else self.warehouse,
			"status": "Active",
		}, update_modified=False)
		return name

	def test_the_flag_alone_makes_a_gun_custody(self):
		from ffl_core.custody import is_custody_serial
		self.assertTrue(is_custody_serial(
			self._serial("TCU-PRED-1", flag=1, warehouse=self.warehouse)))

	def test_the_location_alone_makes_a_gun_custody(self):
		"""The drift case: a gun physically in the custody warehouse whose flag
		was never stamped (a receive that predates this feature, or a manual
		stock entry) is still not ours."""
		from ffl_core.custody import is_custody_serial
		self.assertTrue(is_custody_serial(
			self._serial("TCU-PRED-2", flag=0, warehouse=self.custody_warehouse)))

	def test_an_ordinary_gun_is_not_custody(self):
		from ffl_core.custody import is_custody_serial
		self.assertFalse(is_custody_serial(
			self._serial("TCU-PRED-3", flag=0, warehouse=self.warehouse)))

	def test_an_unknown_serial_is_not_custody(self):
		from ffl_core.custody import is_custody_serial
		self.assertFalse(is_custody_serial("TCU-PRED-DOES-NOT-EXIST"))

	def test_custody_serials_finds_guns_by_location(self):
		from ffl_core.custody import custody_serials
		self._serial("TCU-PRED-4", flag=0, warehouse=self.custody_warehouse)
		self._serial("TCU-PRED-5", flag=0, warehouse=self.warehouse)
		found = custody_serials(self.ITEM)
		self.assertIn("TCU-PRED-4", found)
		self.assertNotIn("TCU-PRED-5", found)


class TestPickersAndCustody(FrappeTestCase):
	"""The boundary that is easy to get backwards, and expensive both ways.

	A custody gun must vanish from every SALE picker — it cannot be sold. But
	it must stay VISIBLE in the customer-transfer picker, because that dialog is
	how the gun goes home. A well-meaning "exclude custody everywhere" would
	strand every gun this feature receives, with no way to hand any of them
	back."""

	ITEM = "TCU-PICK-GLOCK"

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		custody.ensure()
		cls.company = _company()
		cls.custody_warehouse = ensure_custody_warehouse(cls.company)
		cls.warehouse = default_stock_warehouse(cls.company)
		if not frappe.db.exists("Item", cls.ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": cls.ITEM, "item_name": cls.ITEM,
				"item_group": "Pistol", "stock_uom": "Nos", "is_stock_item": 1,
				"has_serial_no": 1, "serial_no_series": f"{cls.ITEM}-.####",
				"uniform_sell_price": 1, "standard_rate": 500,
			}).insert(ignore_permissions=True)

	def setUp(self) -> None:
		super().setUp()
		for name, flag, wh in (
				("TCU-PICK-CUSTODY", 1, self.custody_warehouse),
				("TCU-PICK-OWNED", 0, self.warehouse)):
			if not frappe.db.exists("Serial No", name):
				frappe.get_doc({
					"doctype": "Serial No", "serial_no": name, "item_code": self.ITEM,
					"company": self.company,
				}).insert(ignore_permissions=True)
			frappe.db.set_value("Serial No", name, {
				"is_transfer_gun": flag, "warehouse": wh, "status": "Active",
			}, update_modified=False)

	def test_the_sale_picker_does_not_offer_a_custody_gun(self):
		from ffl_core.firearm import available_serials_for_items
		offered = available_serials_for_items([self.ITEM]).get(self.ITEM, [])
		self.assertNotIn("TCU-PICK-CUSTODY", offered)

	def test_the_sale_picker_still_offers_the_stores_own_gun(self):
		from ffl_core.firearm import available_serials_for_items
		offered = available_serials_for_items([self.ITEM]).get(self.ITEM, [])
		self.assertIn("TCU-PICK-OWNED", offered)

	def test_the_priced_sale_picker_does_not_offer_a_custody_gun(self):
		from ffl_core.firearm import available_serials_with_prices
		offered = [r["serial"] for r in
				   available_serials_with_prices([self.ITEM]).get(self.ITEM, [])]
		self.assertNotIn("TCU-PICK-CUSTODY", offered)

	def test_the_transfer_picker_STILL_offers_a_custody_gun(self):
		"""The one place custody guns must remain visible. If this test ever
		'fails safe' by excluding them, every transfer gun in the safe becomes
		unreturnable through the POS."""
		from ffl_core.firearm import in_stock_firearm_serials
		offered = [r["serial"] for r in in_stock_firearm_serials()]
		self.assertIn("TCU-PICK-CUSTODY", offered)

	# -- Create Order scan gate --------------------------------

	def test_scanning_a_custody_gun_into_create_order_is_refused(self):
		"""The sibling rulebook. `_pos_picker_excluded_serials` gained rule 4 and
		its docstring already named this function its sibling with a "Keep them
		aligned" note — this is that alignment.

		Unlike a consigned-out gun (physically at a dealer, unscannable), a
		custody gun sits in the store's own safe next to sellable guns, so
		scanning the wrong one is an ordinary mistake. The result would not be
		ordinary: a customer's property disposed to a third party on a 4473 made
		out for someone else's gun."""
		from ffl_core.api.manual_order import _serial_status
		row, reason = _serial_status("TCU-PICK-CUSTODY")
		self.assertIsNone(row)
		self.assertTrue(reason)

	def test_the_create_order_refusal_points_at_the_transfer_flow(self):
		from ffl_core.api.manual_order import _serial_status
		_row, reason = _serial_status("TCU-PICK-CUSTODY")
		self.assertIn("transfer", reason.lower())

	def test_create_order_still_accepts_the_stores_own_gun(self):
		from ffl_core.api.manual_order import _serial_status
		row, reason = _serial_status("TCU-PICK-OWNED")
		self.assertIsNone(reason)
		self.assertEqual(row["serial"], "TCU-PICK-OWNED")

	def test_a_gun_in_the_custody_warehouse_is_refused_even_without_the_flag(self):
		"""Location is the second key here too — this is a SALE gate, and the
		row is already in hand, so both are checked."""
		from ffl_core.api.manual_order import _serial_status
		frappe.db.set_value("Serial No", "TCU-PICK-CUSTODY",
							"is_transfer_gun", 0, update_modified=False)
		row, reason = _serial_status("TCU-PICK-CUSTODY")
		self.assertIsNone(row)
		self.assertTrue(reason)

	# -- Create Order submit gate --------------------------------

	def _line(self, serial: str) -> list[dict]:
		return [{"item_code": self.ITEM, "qty": 1, "rate": 500, "serials": [serial]}]

	def test_create_order_submit_refuses_a_custody_gun(self):
		"""The serial dropdown and an API/MCP caller reach create_order without a
		scan, so the submit path (_validate_items) must run the same rulebook."""
		from ffl_core.api.manual_order import _validate_items
		with self.assertRaises(frappe.ValidationError):
			_validate_items(self._line("TCU-PICK-CUSTODY"))

	def test_create_order_submit_refuses_a_consigned_out_gun(self):
		from ffl_core.api.manual_order import _validate_items
		frappe.db.set_value("Serial No", "TCU-PICK-OWNED", "consigned_out", 1,
							update_modified=False)
		self.addCleanup(frappe.db.set_value, "Serial No", "TCU-PICK-OWNED",
						"consigned_out", 0, update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			_validate_items(self._line("TCU-PICK-OWNED"))

	def test_create_order_submit_accepts_the_stores_own_gun(self):
		from ffl_core.api.manual_order import _validate_items
		rows, _seen, _reg = _validate_items(self._line("TCU-PICK-OWNED"))
		self.assertEqual(rows[0]["serials"], ["TCU-PICK-OWNED"])
		self.assertEqual(rows[0]["warehouse"], self.warehouse)


# ===========================================================================
# The sales report classifies transfers by the flag, not by inference
# ===========================================================================

class TransferRowsCostNothingInTheSalesReport(FrappeTestCase):
	"""Before this run the report inferred "transfer" from an invoice header
	flag AND a net of exactly 0, which cleared the missing-cost warning but
	never zeroed the cost. With a $10 placeholder still on the serial that read:

	    gun line   net $0 − cost $10  = profit −$10
	    fee line   net $25            = the fee, $10 light against the GL

	Both numbers were wrong, and the negative row was the visible symptom (real
	repros: a past invoice). The gun's own is_transfer_gun flag now
	answers the question directly."""

	NET_ZERO = 0.0
	PLACEHOLDER_COST = 10.0   # the incident's figure

	def _cost(self, *, serial, net, serial_cost, transfer_serials,
			is_ffl_transfer=True):
		from ffl_core.ffl_core.report.sales_report.sales_report import _line_cost
		line = {"item_code": "TCU-RPT-GUN", "qty": 1, "serial_no": serial,
				"base_net_amount": net}
		item_meta = {"TCU-RPT-GUN": {"is_firearm": 1}}
		return _line_cost(line, item_meta, serial_cost, None,
						  is_ffl_transfer=is_ffl_transfer,
						  transfer_serials=transfer_serials)

	def test_a_flagged_transfer_gun_costs_zero_despite_a_legacy_placeholder(self):
		"""The exact incident shape: the serial still carries $10 from a receive
		that predates the rate lock, and the report must ignore it."""
		cost, missing = self._cost(
			serial="SN-XFER", net=self.NET_ZERO,
			serial_cost={"SN-XFER": self.PLACEHOLDER_COST},
			transfer_serials={"SN-XFER"})
		self.assertEqual(cost, 0.0)
		self.assertFalse(missing)

	def test_a_flagged_transfer_gun_produces_no_negative_profit_row(self):
		cost, _ = self._cost(
			serial="SN-XFER", net=self.NET_ZERO,
			serial_cost={"SN-XFER": self.PLACEHOLDER_COST},
			transfer_serials={"SN-XFER"})
		self.assertGreaterEqual(self.NET_ZERO - cost, 0.0)

	def test_the_flag_wins_even_when_the_line_did_not_net_zero(self):
		"""The heuristic keyed on net == 0 and silently gave up otherwise. The
		flag does not care what the line netted — which is what makes it a real
		classification rather than a coincidence detector."""
		cost, missing = self._cost(
			serial="SN-XFER", net=25.0,
			serial_cost={"SN-XFER": self.PLACEHOLDER_COST},
			transfer_serials={"SN-XFER"})
		self.assertEqual(cost, 0.0)
		self.assertFalse(missing)

	def test_an_unflagged_gun_with_no_cost_is_still_reported_missing(self):
		"""The control that keeps the flag honest: zeroing cost for everything
		would hide every genuine missing-cost data gap the report exists to
		surface."""
		_, missing = self._cost(
			serial="SN-REAL", net=650.0, serial_cost={}, transfer_serials=set(),
			is_ffl_transfer=False)
		self.assertTrue(missing)

	def test_an_ordinary_gun_still_reports_its_real_cost(self):
		cost, missing = self._cost(
			serial="SN-REAL", net=650.0, serial_cost={"SN-REAL": 420.0},
			transfer_serials=set(), is_ffl_transfer=False)
		self.assertEqual(cost, 420.0)
		self.assertFalse(missing)

	def test_a_mixed_invoice_charges_only_the_real_gun(self):
		"""A transfer and a real sale on one ticket: the customer's gun costs
		nothing, the store's gun costs what it cost."""
		cost, missing = self._cost(
			serial="SN-XFER\nSN-REAL", net=650.0,
			serial_cost={"SN-XFER": self.PLACEHOLDER_COST, "SN-REAL": 420.0},
			transfer_serials={"SN-XFER"})
		self.assertEqual(cost, 420.0)
		self.assertFalse(missing)
