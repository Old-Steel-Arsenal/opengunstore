"""Site integration — counter drop-ship end to end.

SITE-ONLY (needs a real bench: documents, the state machine, the registered
ffl_counter_order_distributor hook, the interstate tax engine). Runs under `make test`,
NOT the stubbed harness — this is where "the pure payload looked fine but the real
Document rejects it" is caught.

Reuses ``BuildOrderMixin`` (POS Profile, tax templates, DEST_FFL in TX, customer
helpers) and adds the drop-ship masters. Proves, on real docs:

* a drop-ship line comes back ``delivered_by_supplier=1`` + no warehouse + the
  distributor pinned, and the invoice does NOT update stock (update_stock is 0);
* the ffl_counter_order_distributor hook builds a Draft Distributor Order with the
  SI as its Dynamic-Link source, the right order_type/lane, and the right
  destination (accessory → buyer address; firearm → transfer FFL);
* the SI is taxed by DESTINATION through the interstate engine — an
  in-state FFL is taxed, an out-of-state one is zeroed, NEVER the counter TX default;
* Business buyers are refused; a restricted state is refused;
* a placed DO blocks the SI cancel; the ShipStation payload skips the
  drop-ship line; no local Serial No / Disposition is ever created.
"""
from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, nowdate

from ffl_core.api import manual_order
from ffl_core.tests.test_build_order import DEST_FFL, BuildOrderMixin
# Cross-app test import — same precedent as ffl_woo_sync's tests importing this
# module (test_distributor_layering.py): the catalog stub is test-only plumbing,
# not a production dependency, so it does not violate the ffl_core/ffl_integrations
# layering (that rule is about ffl_integrations <-> ffl_woo_sync production code).
from ffl_integrations.tests import catalog_stub

DISTRIBUTOR = "RSR-P4B"
SUPPLIER = "RSR GROUP INC"
ACC_ITEM = "P4B-RSR-HOLO"       # drop-ship accessory
GUN_ITEM = "P4B-RSR-GLK19"      # drop-ship firearm
RESTRICTED_ITEM = "P4B-RSR-CABAN"   # restricted in CA
OOS_FFL = "9-99-999-99-9X-00001"    # an out-of-state (CA) transfer FFL


def _ensure(doctype, name, payload):
	if frappe.db.exists(doctype, name):
		return frappe.get_doc(doctype, name)
	doc = frappe.get_doc({"doctype": doctype, **payload})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc


class CounterDropshipFixtures(BuildOrderMixin, FrappeTestCase):
	"""Drop-ship masters + helpers, WITHOUT test methods — what other modules
	subclass (inheriting CounterDropshipIntegration would re-run its claims)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# BuildOrderMixin supplies _indiv / _item / _stocked_serial — but NOT the POS
		# Profile helper nor the tax templates (test_manual_order_tax_exempt defines
		# those for itself; copying its usage without its fixtures is what broke the
		# first bench run). The destination-tax rule needs BOTH: a company-default template so an in-state
		# sale is taxed, and the 0% "Out of State Sales" template the interstate engine
		# swaps to. Order matters — the default template creates the sales-tax ACCOUNT
		# that setup.tax's out-of-state provisioner keys off (it skips a company with
		# no sales-tax account, which is why a bare/CI site can lack the OOS template).
		cls._ensure_default_tax_template()
		cls._ensure_pos_profile_default_tax()
		cls._ensure_oos_template()
		cls.stub = catalog_stub.install()
		cls._dropship_fixture()

	@classmethod
	def tearDownClass(cls):
		catalog_stub.uninstall()
		super().tearDownClass()

	# ---- fixtures BuildOrderMixin does NOT provide (see setUpClass comment) ----

	def _pos_profile(self) -> str:
		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		self.assertTrue(pos, "dev_site should seed a POS Profile for the test company")
		return pos

	@classmethod
	def _ensure_default_tax_template(cls) -> None:
		"""Company-default 8.25% template + its tax account. Same title/company
		test_interstate_tax and test_manual_order_tax_exempt use, created idempotently
		so any of them may run first in one `bench run-tests --app ffl_core` session."""
		abbr = frappe.get_cached_value("Company", cls.company, "abbr")
		cls.default_template = f"Texas Sales Tax - {abbr}"
		if frappe.db.exists("Sales Taxes and Charges Template", cls.default_template):
			return
		acct_name = frappe.db.get_value(
			"Account", {"company": cls.company, "account_name": "ITX Sales Tax"}, "name")
		if not acct_name:
			parent = frappe.db.get_value(
				"Account", {"company": cls.company, "root_type": "Liability", "is_group": 1},
				"name", order_by="lft")
			acct = frappe.get_doc({
				"doctype": "Account", "account_name": "ITX Sales Tax",
				"company": cls.company, "parent_account": parent,
				"account_type": "Tax"})
			acct.insert(ignore_permissions=True)
			acct_name = acct.name
		frappe.get_doc({
			"doctype": "Sales Taxes and Charges Template",
			"title": "Texas Sales Tax", "company": cls.company, "is_default": 1,
			"taxes": [{"charge_type": "On Net Total", "account_head": acct_name,
				"rate": 8.25, "description": "TX Sales Tax"}],
		}).insert(ignore_permissions=True)

	@classmethod
	def _ensure_pos_profile_default_tax(cls) -> None:
		"""A real counter POS Profile carries its own default `taxes_and_charges`.
		These orders are paid in full (is_pos), and set_pos_fields refills the template
		from the profile — without this the in-state case would come back untaxed
		for a fixture reason, not a product one."""
		pos = frappe.db.get_value(
			"POS Profile", {"company": cls.company, "disabled": 0}, "name")
		if pos and frappe.db.get_value(
				"POS Profile", pos, "taxes_and_charges") != cls.default_template:
			frappe.db.set_value("POS Profile", pos, "taxes_and_charges",
				cls.default_template, update_modified=False)

	@classmethod
	def _ensure_oos_template(cls) -> None:
		"""Provision the 0% "Out of State Sales" template by REUSING the app's own
		after_migrate routine (never a hand-rolled copy). It no-ops unless the company
		has a sales-tax account — which the default template above just created."""
		from ffl_core.setup.tax import _ensure_out_of_state_templates
		_ensure_out_of_state_templates()

	@classmethod
	def _dropship_fixture(cls):
		_ensure("Supplier", SUPPLIER, {"supplier_name": SUPPLIER, "supplier_type": "Company"})
		_ensure("Distributor", DISTRIBUTOR, {
			"code": DISTRIBUTOR, "distributor_name": "P4b Test Distributor",
			"provider": "rsr", "enabled": 1, "dropship_enabled": 1,
			"supplier": SUPPLIER, "safety_buffer": 0, "display_cap": 0,
			"default_markup_pct": 20})
		specs = (
			(ACC_ITEM, 0, "10", ""),
			(GUN_ITEM, 1, "01", ""),
			(RESTRICTED_ITEM, 0, "10", "CA"),
		)
		for code, is_firearm, dept, restricted in specs:
			if not frappe.db.exists("Item", code):
				doc = frappe.get_doc({
					"doctype": "Item", "item_code": code, "item_name": code,
					"item_group": ("Pistol" if is_firearm else "Accessory")
						if frappe.db.exists("Item Group",
							"Pistol" if is_firearm else "Accessory") else "All Item Groups",
					"is_firearm": is_firearm, "is_stock_item": 1,
					"has_serial_no": 1 if is_firearm else 0,
					"uniform_sell_price": 1 if is_firearm else 0,
					"dropship_distributor": DISTRIBUTOR,
					"restricted_states": restricted,
					"stock_uom": "Nos"})
				doc.flags.ignore_permissions = True
				doc.insert()
			catalog_stub.seed_catalog([{
				"rsr_stock_number": code, "dealer_price": 300.0, "quantity": 8,
				"ffl_required": is_firearm, "department_number": dept, "model": code}])
		# An out-of-state transfer FFL for the destination-tax test.
		if not frappe.db.exists("ATF FFL Record", OOS_FFL):
			rec = frappe.get_doc({
				# `ffl_number` IS the licence column (and the autoname) — there is no
				# `license` field on this doctype; see test_counter_doctype_contracts.
				"doctype": "ATF FFL Record", "ffl_number": OOS_FFL,
				"licensee_name": "CA Dealer",
				"street": "1 Pacific Ave", "city": "Los Angeles", "state": "CA",
				"zip": "90001", "expiry_date": add_days(nowdate(), 365)})
			rec.flags.ignore_permissions = True
			rec.insert()

	# ------------------------------------------------------------- helpers

	def _orders_for(self, si_name):
		return frappe.get_all(
			"Distributor Order",
			filters={"source_doctype": "Sales Invoice", "source_name": si_name},
			fields=["name", "order_type", "account", "status", "transfer_ffl_license",
				"ship_address_line1", "ship_address_state", "fill_or_kill"])

	def _counter(self, *, items, buyer="Ann Individual", ffl=None, ship_to=None,
			dispose_now=1, paid=1):
		payload = {
			"buyer_type": "individual", "customer": self._indiv(buyer),
			"pos_profile": self._pos_profile(),
			"dispose_now": dispose_now,
			"payment_received": paid, "mode_of_payment": "Cash",
			"items": items,
		}
		if ffl:
			payload["destination_ffl"] = ffl
		if ship_to:
			payload["ship_to"] = ship_to
		return manual_order.create_order(payload)

	_ACC_SHIP_TO = {"address_line1": "5 Elm St", "city": "Springfield",
		"state": "TX", "pincode": "75024"}


class CounterDropshipIntegration(CounterDropshipFixtures):

	# ------------------------------------------------------------- the claims

	def test_dropship_accessory_line_shape_and_no_stock_update(self):
		out = self._counter(
			items=[{"item_code": ACC_ITEM, "qty": 1, "rate": 40, "source": "dropship"}],
			ship_to=self._ACC_SHIP_TO)
		self.assertTrue(out["ok"])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.update_stock, 0)                 # T0: forced defer
		row = si.items[0]
		self.assertEqual(row.delivered_by_supplier, 1)
		self.assertFalse(row.warehouse)
		self.assertEqual(row.dropship_distributor, DISTRIBUTOR)

	def test_accessory_hook_builds_a_direct_draft_do_to_the_buyer_address(self):
		out = self._counter(
			items=[{"item_code": ACC_ITEM, "qty": 1, "rate": 40, "source": "dropship"}],
			ship_to=self._ACC_SHIP_TO)
		orders = self._orders_for(out["sales_invoice"])
		self.assertEqual(len(orders), 1)
		do = orders[0]
		self.assertEqual(do["order_type"], "Dropship Accessory")
		self.assertEqual(do["status"], "Draft")
		self.assertEqual(do["ship_address_state"], "TX")     # the buyer address, not the FFL
		self.assertEqual(do["fill_or_kill"], 1)              # counter FillOrKill=Y

	def test_dropship_firearm_ships_to_the_transfer_ffl_and_pins_no_local_serial(self):
		out = self._counter(
			items=[{"item_code": GUN_ITEM, "qty": 1, "rate": 500, "source": "dropship"}],
			ffl=DEST_FFL)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.items[0].delivered_by_supplier, 1)
		self.assertFalse(si.items[0].get("serial_no"))
		orders = self._orders_for(out["sales_invoice"])
		self.assertEqual(orders[0]["order_type"], "Dropship Firearm")
		self.assertTrue(orders[0]["transfer_ffl_license"])
		# No local bound-book document for a drop-ship gun.
		self.assertEqual(frappe.db.count("Serial No",
			{"item_code": GUN_ITEM}), 0)

	def test_mixed_dropship_and_in_store_order_is_refused(self):
		"""update_stock=0 leaves an in-store line's cost with no home,
		so v1 makes the operator ring two orders."""
		serial = self._stocked_serial("P4B-MIX-1")
		with self.assertRaises(frappe.ValidationError):
			self._counter(items=[
				{"item_code": ACC_ITEM, "qty": 1, "rate": 40, "source": "dropship"},
				{"item_code": "BO-GUN", "qty": 1, "rate": 900, "serials": [serial]},
			], ship_to=self._ACC_SHIP_TO)

	def test_business_buyer_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			manual_order.create_order({
				"buyer_type": "business", "customer": self._indiv("Biz Buyer"),
				"pos_profile": self._pos_profile(), "payment_received": 1,
				"mode_of_payment": "Cash", "ship_to": self._ACC_SHIP_TO,
				"items": [{"item_code": ACC_ITEM, "qty": 1, "rate": 40, "source": "dropship"}]})

	def test_accessory_without_a_street_is_refused(self):
		"""An incomplete ship-to never reaches the tax engine."""
		with self.assertRaises(frappe.ValidationError):
			self._counter(
				items=[{"item_code": ACC_ITEM, "qty": 1, "rate": 40, "source": "dropship"}],
				ship_to={"address_line1": "", "city": "Springfield", "state": "TX",
					"pincode": "75024"})

	def test_address_materialisation_failure_is_refused(self):
		"""The ship-to LOOKS complete but the Address doc cannot
		be written (`_ensure_ship_address` is best-effort and returns None on failure).
		Left silent, interstate_tax would find no destination on the invoice and fall
		back to the in-state default — over-collecting on an out-of-state sale."""
		with mock.patch.object(manual_order, "_ensure_ship_address",
				lambda *a, **k: None):
			with self.assertRaises(frappe.ValidationError):
				self._counter(
					items=[{"item_code": ACC_ITEM, "qty": 1, "rate": 40,
						"source": "dropship"}],
					ship_to=self._ACC_SHIP_TO)

	def test_cross_state_dropship_legs_are_refused(self):
		"""An FDS gun to a TX transfer FFL plus an accessory to a
		CA address cannot both be taxed right on one single-destination invoice."""
		with self.assertRaises(frappe.ValidationError):
			self._counter(items=[
				{"item_code": GUN_ITEM, "qty": 1, "rate": 500, "source": "dropship"},
				{"item_code": ACC_ITEM, "qty": 1, "rate": 40, "source": "dropship"},
			], ffl=DEST_FFL, ship_to={"address_line1": "1 Sunset Blvd",
				"city": "Los Angeles", "state": "CA", "pincode": "90001"})

	def test_same_state_dropship_legs_are_allowed(self):
		"""…while the ordinary case (buyer's FFL in the buyer's own state) still rings
		as one order."""
		out = self._counter(items=[
			{"item_code": GUN_ITEM, "qty": 1, "rate": 500, "source": "dropship"},
			{"item_code": ACC_ITEM, "qty": 1, "rate": 40, "source": "dropship"},
		], ffl=DEST_FFL, ship_to=self._ACC_SHIP_TO)
		self.assertTrue(out["ok"])
		# Two legs ⇒ two orders (FDS gun + direct accessory), one per destination kind.
		self.assertEqual(len(self._orders_for(out["sales_invoice"])), 2)

	def test_restricted_state_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._counter(
				items=[{"item_code": RESTRICTED_ITEM, "qty": 1, "rate": 40,
					"source": "dropship"}],
				ship_to={"address_line1": "1 Sunset", "city": "LA", "state": "CA",
					"pincode": "90001"})

	# ------------------------------------------------------------- destination tax

	def _sales_tax(self, si) -> float:
		"""Tax booked to the SALES-TAX account(s) only.

		`total_taxes_and_charges` also carries the freight row, so it is not a
		sales-tax signal — the earlier assertion was only accidentally right because this
		fixture's shipping is 0. Judge the ACCOUNT, not the total.

		The accounts are derived from the company's DEFAULT template rather than
		hard-coded, so this reads correctly on whatever chart the site actually runs
		(this fixture's is `ITX Sales Tax - <abbr>`; a stock CoA names it
		`Sales Tax Payable - <abbr>`). An out-of-state invoice swaps to the 0%
		Out-of-State template, which has no rows at all — hence 0."""
		tmpl = frappe.db.get_value(
			"Sales Taxes and Charges Template",
			{"company": self.company, "is_default": 1, "disabled": 0}, "name")
		accounts = set(frappe.get_all(
			"Sales Taxes and Charges", filters={"parent": tmpl},
			pluck="account_head")) if tmpl else set()
		return sum(flt(t.tax_amount) for t in (si.taxes or [])
			if (t.account_head or "") in accounts)

	def test_c3_in_state_ffl_is_taxed(self):
		out = self._counter(
			items=[{"item_code": GUN_ITEM, "qty": 1, "rate": 500, "source": "dropship"}],
			ffl=DEST_FFL)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertGreater(self._sales_tax(si), 0,
			"an in-state drop-ship SI must be taxed by destination")

	def test_c3_out_of_state_ffl_is_not_taxed(self):
		out = self._counter(
			items=[{"item_code": GUN_ITEM, "qty": 1, "rate": 500, "source": "dropship"}],
			ffl=OOS_FFL)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertAlmostEqual(self._sales_tax(si), 0, places=2,
			msg="an out-of-state drop-ship SI must NOT fall back to the counter TX default")

	def test_c3_pure_accessory_direct_ship_out_of_state_is_not_taxed(self):
		"""The path counter drop-ship actually opened (accessory straight to the buyer's
		own out-of-state address, no FFL anywhere) had zero coverage. Here the
		destination comes from the ship-to rather than an FFL premise, so it exercises
		a different branch of the interstate engine than the two FDS cases above."""
		out = self._counter(
			items=[{"item_code": ACC_ITEM, "qty": 1, "rate": 60, "source": "dropship"}],
			ship_to={"address_line1": "1 Sunset Blvd", "city": "Los Angeles",
				"state": "CA", "pincode": "90001"})
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertAlmostEqual(self._sales_tax(si), 0, places=2,
			msg="an out-of-state accessory drop-ship must not be taxed at the TX rate")
		# And the parcel really is addressed out of state (the ship-to
		# carried through to the Distributor Order, not re-derived).
		self.assertEqual(self._orders_for(out["sales_invoice"])[0]["ship_address_state"],
			"CA")

	def test_c3_in_state_accessory_direct_ship_is_taxed(self):
		out = self._counter(
			items=[{"item_code": ACC_ITEM, "qty": 1, "rate": 60, "source": "dropship"}],
			ship_to=self._ACC_SHIP_TO)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertGreater(self._sales_tax(si), 0,
			"an in-state accessory drop-ship must still be taxed")

	# ------------------------------------------------------------- guards / wiring

	def test_placed_distributor_order_blocks_the_sale_cancel(self):
		out = self._counter(
			items=[{"item_code": ACC_ITEM, "qty": 1, "rate": 40, "source": "dropship"}],
			ship_to=self._ACC_SHIP_TO)
		do = frappe.get_doc("Distributor Order", self._orders_for(out["sales_invoice"])[0]["name"])
		do.db_set("status", "Placed")   # simulate a placed order (no cancel API)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		with self.assertRaises(frappe.ValidationError):
			si.cancel()

	def test_shipstation_payload_excludes_the_dropship_line(self):
		from ffl_integrations.shipstation import api as ss
		out = self._counter(
			items=[{"item_code": ACC_ITEM, "qty": 1, "rate": 40, "source": "dropship"}],
			ship_to=self._ACC_SHIP_TO)
		env = ss.build_ship_to(out["sales_invoice"])
		self.assertNotIn(ACC_ITEM, [i["sku"] for i in env["items"]])

	def test_deposit_counter_order_cannot_be_placed_until_paid(self):
		from ffl_integrations.distributor import api
		out = self._counter(
			items=[{"item_code": ACC_ITEM, "qty": 1, "rate": 40, "source": "dropship"}],
			ship_to=self._ACC_SHIP_TO, paid=0)
		do_name = self._orders_for(out["sales_invoice"])[0]["name"]
		with self.assertRaises(frappe.ValidationError):
			api.confirm_order(do_name)   # paid-gate: only a fully settled order can be Queued

	def test_amended_invoice_rebuilds_its_distributor_order(self):
		"""The amend door: cancelling withdraws the Draft order, and the
		amended invoice submits WITHOUT create_order — the on_submit guard must
		rebuild a Draft order rather than leave paid-for goods unordered."""
		out = self._counter(
			items=[{"item_code": ACC_ITEM, "qty": 1, "rate": 40, "source": "dropship"}],
			ship_to=self._ACC_SHIP_TO)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		si.cancel()   # Draft DO is withdrawn by the before_cancel guard
		amended = frappe.copy_doc(si)
		amended.amended_from = si.name
		amended.docstatus = 0
		amended.flags.ignore_permissions = True
		amended.insert()
		amended.submit()
		rebuilt = self._orders_for(amended.name)
		self.assertTrue(rebuilt, "an amended drop-ship invoice must rebuild its order")
		self.assertEqual(rebuilt[0]["status"], "Draft")   # never auto-placed

	def test_g1_amend_keeps_the_dropship_cost_chain(self):
		"""After cancel + amend the drop-ship line must
		still resolve its ORIGINAL ack cost — `cost > 0`, `missing_cost = False`, and
		the report's cost equal to the linked PI's COGS leg.

		Partial refunds are done as cancel + amend, so without the carry-forward the
		tie-out breaks on every real refund."""
		from ffl_integrations.distributor import accounting
		from ffl_core.ffl_core.report.sales_report import sales_report

		out = self._counter(
			items=[{"item_code": ACC_ITEM, "qty": 1, "rate": 90, "source": "dropship"}],
			ship_to=self._ACC_SHIP_TO)
		si1 = out["sales_invoice"]
		do = frappe.get_doc("Distributor Order", self._orders_for(si1)[0]["name"])

		# The distributor acknowledged and shipped: real ack cost, then its cost PI.
		for line in do.lines:
			line.db_set("ack_qty", 1)
			line.db_set("unit_cost", 62.5)
		# Walk the real state machine — Draft→Complete is an illegal jump, and
		# a fixture that bypasses the machine can hide a transition bug.
		for step in ("Queued", "Placed", "Complete"):
			do.db_set("status", step)
		pi_name = accounting.ensure_cost_invoice(do.name)["purchase_invoice"]
		self.assertTrue(pi_name, "the cost PI must exist for the tie-out to mean anything")

		# Partial refund SOP: cancel + amend to net.
		si_doc = frappe.get_doc("Sales Invoice", si1)
		si_doc.cancel()
		amended = frappe.copy_doc(si_doc)
		amended.amended_from = si1
		amended.docstatus = 0
		amended.flags.ignore_permissions = True
		amended.insert()
		amended.submit()

		# The live order followed the live invoice (and no phantom was rebuilt).
		self.assertEqual(
			frappe.db.get_value("Distributor Order", do.name, "source_name"),
			amended.name)
		self.assertEqual(len(self._orders_for(amended.name)), 1)

		# The accountant's assertion, on the real report — the **Product** view. `view` is not
		# optional here: the default Order view emits one row PER INVOICE with no
		# `row_kind` and no `item_code`, so a per-line cost assertion silently matches
		# nothing there. The tie-out is defined on the per-line CPA view.
		rows = sales_report.execute({
			"from_date": frappe.utils.nowdate(), "to_date": frappe.utils.nowdate(),
			"company": self.company, "view": "Product"})[1]
		line = next((r for r in rows
			if r.get("row_kind") == "product" and r.get("item_code") == ACC_ITEM
			and r.get("invoice") == amended.name), None)
		self.assertIsNotNone(line, "the amended invoice's drop-ship line must appear")
		self.assertGreater(flt(line["cost"]), 0, "cost must survive the amend")
		self.assertFalse(line.get("missing_cost"), "no missing-cost flag after amend")

		pi = frappe.get_doc("Purchase Invoice", pi_name)
		cogs = sum(flt(i.amount) for i in pi.items
			if "Cost of Goods Sold" in (i.expense_account or ""))
		self.assertAlmostEqual(flt(line["cost"]), cogs, places=2,
			msg="report cost must equal the PI's COGS leg after an amend")

	def test_cost_invoice_is_booked_once_per_order(self):
		"""A completed DO's cost PI job runs more than once (the poll's save fires
		on_update AND the poll calls maybe_complete_source itself). With no bill_no
		the PI stores bill_no NULL, so the dedup lookup must match NULL — else every
		run drafts another PI and COGS + the payable double-book."""
		from ffl_integrations.distributor import accounting

		out = self._counter(
			items=[{"item_code": ACC_ITEM, "qty": 1, "rate": 90, "source": "dropship"}],
			ship_to=self._ACC_SHIP_TO)
		do = frappe.get_doc("Distributor Order", self._orders_for(out["sales_invoice"])[0]["name"])
		for line in do.lines:
			line.db_set("ack_qty", 1)
			line.db_set("unit_cost", 62.5)
		for step in ("Queued", "Placed", "Complete"):
			do.db_set("status", step)
		first = accounting.ensure_cost_invoice(do.name)
		self.assertEqual(first["status"], "draft")
		again = accounting.ensure_cost_invoice(do.name)
		self.assertEqual(again, {"purchase_invoice": first["purchase_invoice"], "status": "exists"})
		self.assertEqual(frappe.db.count("Purchase Invoice",
			{"distributor_order": do.name, "docstatus": ("<", 2)}), 1)

	def test_a_sales_user_can_ring_and_place_a_dropship_order(self):
		"""The counter roles hold no Distributor Order DocPerm: the hook's insert and
		Pending Order's Place order run under the counter gate, not the DocPerm."""
		from ffl_core.tests.test_trade_in_counter_roles import SALES_USER, _ensure_user

		_ensure_user(SALES_USER, "Sales User")
		frappe.set_user(SALES_USER)
		try:
			out = self._counter(
				items=[{"item_code": ACC_ITEM, "qty": 1, "rate": 40, "source": "dropship"}],
				ship_to=self._ACC_SHIP_TO)
			self.assertEqual(len(self._orders_for(out["sales_invoice"])), 1)
			result = manual_order.release_order_legs(out["sales_invoice"])
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(result["failed"], [])
		self.assertEqual(frappe.db.get_value(
			"Distributor Order", result["released"][0], "status"), "Queued")

	def test_the_counter_hook_is_registered(self):
		self.assertIn(
			"ffl_integrations.distributor.counter.ffl_counter_order_distributor",
			frappe.get_hooks("ffl_counter_order_distributor"))


if __name__ == "__main__":
	import unittest
	unittest.main()
