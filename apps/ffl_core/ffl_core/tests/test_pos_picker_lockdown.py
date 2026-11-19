"""POS 4473 picker lockdown.

The three POS firearm pickers offered no exclusion at all — a gun held on a
counter order, a Draft consignment, already consigned out, or sitting in a
dealer consignment warehouse would still show up to be sold. All three now
route through ONE rulebook (_pos_picker_excluded_serials) so they can't
diverge. This is the POS money path, so the exclusion is opt-out
(exclude_unavailable=False) exactly where a caller needs the raw list (the
scan-to-add price lookup, the checkout-gate summary, the Create Order picker)
— those behaviours must be preserved.

Written RED-first: the pickers take no exclude flag yet and offer every Active
serial.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from ffl_core.tests.settings_sandbox import SettingsSandbox
from ffl_core.tests.test_extension_hooks import hooks

DEALER = "_Test POS Lockdown Dealer"
DEALER_FFL = "1-99-888-01-2D-00194"
ITEM = "POS-LOCK-GUN"
COST = 480.0


HELD: dict[str, set[str]] = {}       # item_code -> serials a test is holding


def fake_holds(item_code):
	return [{"serial": s, "source": "test", "ref": f"HOLD-{s}", "kind": "consignment",
		"tooltip": "held by a test", "url": ""} for s in sorted(HELD.get(item_code, ()))]


class PickerLockdownFixtures(SettingsSandbox, FrappeTestCase):
	fake_holds = True                      # an extension's test sets False to use its real provider

	def setUp(self) -> None:
		super().setUp()
		# Counter holds come from the registry (reservations.counter_holds); the
		# extension hold an outbound consignment adds is one provider of many, so
		# these tests hold a serial through a fake provider (HELD below) — the
		# picker's exclusion rules are the subject, not who holds the gun.
		HELD.clear()
		if self.fake_holds:
			self._hooks = hooks("gunshop_counter_holds", [f"{__name__}.fake_holds"])
			self._hooks.__enter__()
			self.addCleanup(self._hooks.__exit__, None, None, None)

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.stores = frappe.db.get_value(
			"Warehouse", {"company": cls.company, "warehouse_name": "Stores"}, "name")

		if not frappe.db.exists("Item", ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": ITEM, "item_name": ITEM,
				"item_group": "Firearm", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1,
			}).insert(ignore_permissions=True)

		if not frappe.db.exists("Customer", DEALER):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": DEALER,
				"customer_group": "Commercial", "territory": "All Territories",
				"is_ffl_dealer": 1, "ffl_number": DEALER_FFL,
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("ATF FFL Record", DEALER_FFL):
			frappe.get_doc({
				"doctype": "ATF FFL Record", "ffl_number": DEALER_FFL,
				"licensee_name": "POS Lock Dealer LLC",
				"street": "7 Test Way", "city": "Austin", "state": "TX", "zip": "78701",
				"expiry_date": add_days(nowdate(), 365),
			}).insert(ignore_permissions=True)

	def _receive(self, serial: str, warehouse: str | None = None) -> None:
		from ffl_core.firearm import serialized_receipt_row
		warehouse = warehouse or self.stores
		if frappe.db.exists("Serial No", serial) and frappe.db.get_value(
				"Serial No", serial, "status") == "Active":
			return
		se = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
			"company": self.company,
			"items": [serialized_receipt_row(
				item_code=ITEM, qty=1, warehouse=warehouse, basic_rate=COST, serial=serial)],
		})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()

	def _draft_cono(self, serial: str) -> str:
		"""Hold `serial` the way a draft outbound consignment does — through the
		counter-hold registry — without the extension."""
		HELD.setdefault(ITEM, set()).add(serial)
		return f"HOLD-{serial}"


class SharedHelperExcludes(PickerLockdownFixtures):
	"""The one rulebook: _pos_picker_excluded_serials."""

	def test_excludes_held_consigned_and_consignment_warehouse(self):
		from ffl_core.firearm import _pos_picker_excluded_serials
		from ffl_core.warehouses import ensure_consignment_warehouse
		free = "POS-LOCK-FREE-1"
		held = "POS-LOCK-HELD-1"
		consigned = "POS-LOCK-CONSIGNED-1"
		in_cwh = "POS-LOCK-INCWH-1"
		for s in (free, held, consigned, in_cwh):
			self._receive(s)
		self._draft_cono(held)  # counter/consignment hold registry
		frappe.db.set_value("Serial No", consigned, "consigned_out", 1)
		dealer_wh = ensure_consignment_warehouse(DEALER, self.company)
		from ffl_core.firearm import transfer_serial
		transfer_serial(ITEM, in_cwh, self.stores, dealer_wh, remarks="lockdown fixture")

		excluded = _pos_picker_excluded_serials([ITEM])
		self.assertIn(held, excluded)
		self.assertIn(consigned, excluded)
		self.assertIn(in_cwh, excluded)
		self.assertNotIn(free, excluded)


class ThreePickersExcludeUnavailable(PickerLockdownFixtures):
	"""All three POS pickers drop the held/consigned/consignment-wh
	guns and keep the free one."""

	def _setup_four(self, tag: str):
		from ffl_core.warehouses import ensure_consignment_warehouse
		from ffl_core.firearm import transfer_serial
		free, held, consigned, in_cwh = (
			f"POS-LOCK-{tag}-FREE", f"POS-LOCK-{tag}-HELD",
			f"POS-LOCK-{tag}-CONS", f"POS-LOCK-{tag}-CWH")
		for s in (free, held, consigned, in_cwh):
			self._receive(s)
		self._draft_cono(held)
		frappe.db.set_value("Serial No", consigned, "consigned_out", 1)
		dealer_wh = ensure_consignment_warehouse(DEALER, self.company)
		transfer_serial(ITEM, in_cwh, self.stores, dealer_wh, remarks="lockdown fixture")
		return free, held, consigned, in_cwh

	def test_available_serials_for_items_excludes(self):
		from ffl_core.firearm import available_serials_for_items
		free, held, consigned, in_cwh = self._setup_four("A")
		got = available_serials_for_items([ITEM]).get(ITEM, [])
		self.assertIn(free, got)
		for s in (held, consigned, in_cwh):
			self.assertNotIn(s, got)

	def test_available_serials_with_prices_excludes(self):
		from ffl_core.firearm import available_serials_with_prices
		free, held, consigned, in_cwh = self._setup_four("B")
		got = [r["serial"] for r in available_serials_with_prices([ITEM]).get(ITEM, [])]
		self.assertIn(free, got)
		for s in (held, consigned, in_cwh):
			self.assertNotIn(s, got)

	def test_in_stock_firearm_serials_excludes(self):
		from ffl_core.firearm import in_stock_firearm_serials
		free, held, consigned, in_cwh = self._setup_four("C")
		got = [r["serial"] for r in in_stock_firearm_serials()]
		self.assertIn(free, got)
		for s in (held, consigned, in_cwh):
			self.assertNotIn(s, got)


class ExcludeFlagOptOutPreservesRawList(PickerLockdownFixtures):
	"""The opt-out that keeps the price lookup / checkout gate / Create Order
	picker seeing the raw list — exclude_unavailable=False returns held guns."""

	def test_with_prices_opt_out_returns_held_serial_and_its_price(self):
		from ffl_core.firearm import available_serials_with_prices
		serial = "POS-LOCK-D-HELD"
		self._receive(serial)
		self._draft_cono(serial)
		# Default (exclude): dropped.
		self.assertNotIn(serial,
			[r["serial"] for r in available_serials_with_prices([ITEM]).get(ITEM, [])])
		# Opt out (the _serial_ctx_for_item / pos_pricing_summary path): present.
		rows = available_serials_with_prices([ITEM], exclude_unavailable=False).get(ITEM, [])
		self.assertIn(serial, [r["serial"] for r in rows])

	def test_for_items_opt_out_returns_held_serial(self):
		from ffl_core.firearm import available_serials_for_items
		serial = "POS-LOCK-E-HELD"
		self._receive(serial)
		self._draft_cono(serial)
		got = available_serials_for_items([ITEM], exclude_unavailable=False).get(ITEM, [])
		self.assertIn(serial, got)


class InternalCallersPreserved(PickerLockdownFixtures):
	"""The shared callers that must NOT lose held guns still
	see them, so the counter scan price + checkout gate don't regress."""

	def test_serial_status_price_still_resolves_for_an_ignored_invoices_own_serial(self):
		"""_serial_ctx_for_item feeds _serial_status the price via the opt-out
		path. Pin it: a normally-sellable serial resolves its price (the edit
		flow relies on this same path for a held-by-own-invoice serial)."""
		from ffl_core.api.manual_order import _serial_status
		serial = "POS-LOCK-F-FREE"
		self._receive(serial)
		frappe.db.set_value("Serial No", serial, "sell_price", 725)
		data, reason = _serial_status(serial)
		self.assertIsNone(reason)
		self.assertEqual(data["serial"], serial)
		self.assertEqual(data["sell_price"], 725)

	def test_pos_pricing_summary_counts_a_held_gun_gate_unchanged(self):
		"""pos_pricing_summary drives the checkout gate; it uses the opt-out
		path, so a held gun still counts toward n (gate behaviour preserved —
		B6 scopes the change to the three pickers, not the gate)."""
		from ffl_core.firearm import pos_pricing_summary
		serial = "POS-LOCK-G-HELD"
		self._receive(serial)
		frappe.db.set_value("Serial No", serial, "sell_price", 640)
		self._draft_cono(serial)
		summary = pos_pricing_summary([ITEM])[ITEM]
		self.assertGreaterEqual(summary["n"], 1, "the gate still sees the held gun's serial")


POS_BUYER = "_Test POS Lock Buyer"


class CurrentInvoiceSelfExemption(PickerLockdownFixtures):
	"""A past incident (self-lockout): a serial parked on the
	CURRENT invoice's OWN Draft POS line was excluded from that invoice's own
	4473 / Item-Details picker (it looked like someone else's hold), so the 4473
	serial dropdown lost the serial already on the line and the cashier couldn't
	proceed. Passing current_invoice must lift ONLY this invoice's own POS hold —
	other invoices' holds, consignment, and consigned_out stay excluded."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		if not frappe.db.exists("Customer", POS_BUYER):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": POS_BUYER,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		from ffl_core.tests.pos_opening import todays_pos_opening
		todays_pos_opening(cls.company)

	def _draft_pos(self, serial: str) -> str:
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = "FFL Test"
		inv.customer = POS_BUYER
		inv.append("items", {
			"item_code": ITEM, "qty": 1, "rate": 600, "price_list_rate": 600,
			"serial_no": serial, "use_serial_batch_fields": 1,
			"allow_zero_valuation_rate": 1,
		})
		inv.set_missing_values()
		inv.flags.ignore_permissions = True
		inv.save()
		return inv.name

	def test_current_invoice_serial_is_offered_to_its_own_picker(self):
		from ffl_core.firearm import available_serials_for_items
		serial = "POS-LOCK-SELF-1"
		self._receive(serial)
		inv = self._draft_pos(serial)
		# Without current_invoice: excluded (the parked-POS hold) — the lockout.
		self.assertNotIn(serial, available_serials_for_items([ITEM]).get(ITEM, []))
		# With current_invoice: offered again (self-hold exemption).
		self.assertIn(serial,
			available_serials_for_items([ITEM], current_invoice=inv).get(ITEM, []))

	def test_other_invoices_serial_stays_excluded(self):
		from ffl_core.firearm import available_serials_for_items
		mine = "POS-LOCK-SELF-MINE"
		theirs = "POS-LOCK-SELF-THEIRS"
		self._receive(mine)
		self._receive(theirs)
		my_inv = self._draft_pos(mine)
		self._draft_pos(theirs)  # a DIFFERENT invoice holds `theirs`
		got = available_serials_for_items([ITEM], current_invoice=my_inv).get(ITEM, [])
		self.assertIn(mine, got, "own line serial must be offered")
		self.assertNotIn(theirs, got, "another open invoice's hold must stay excluded")

	def test_with_prices_current_invoice_exemption(self):
		from ffl_core.firearm import available_serials_with_prices
		serial = "POS-LOCK-SELF-PRICED"
		self._receive(serial)
		inv = self._draft_pos(serial)
		self.assertNotIn(serial,
			[r["serial"] for r in available_serials_with_prices([ITEM]).get(ITEM, [])])
		self.assertIn(serial, [r["serial"] for r in
			available_serials_with_prices([ITEM], current_invoice=inv).get(ITEM, [])])

	def test_bare_string_item_code_does_not_500(self):
		from ffl_core.firearm import available_serials_for_items, available_serials_with_prices
		serial = "POS-LOCK-BARESTR-1"
		self._receive(serial)
		# A bare code string (not JSON) must be treated as a single code, not
		# json.loads-crashed into a 500 (today's MCP available_serials 500).
		got = available_serials_for_items(ITEM).get(ITEM, [])
		self.assertIn(serial, got)
		got2 = [r["serial"] for r in available_serials_with_prices(ITEM).get(ITEM, [])]
		self.assertIn(serial, got2)

	# --- follow-up hardening (existence/attribution, anomalies) ----

	def test_nonexistent_current_invoice_grants_no_exemption(self):
		"""D: only a live Draft POS Invoice grants the self-exemption. A bogus /
		unknown name must NOT unlock a held serial (server-side attribution guard
		— you can't self-report an arbitrary invoice name to open the picker)."""
		from ffl_core.firearm import available_serials_for_items
		serial = "POS-LOCK-D-BOGUS"
		self._receive(serial)
		self._draft_pos(serial)  # a real hold on a real invoice
		got = available_serials_for_items(
			[ITEM], current_invoice="POS-INV-DOES-NOT-EXIST-9999").get(ITEM, [])
		self.assertNotIn(serial, got, "a bogus current_invoice must not lift the hold")

	def test_exemption_does_not_lift_consignment_or_consigned(self):
		"""F(a): the self-exemption lifts ONLY this invoice's own POS hold — a
		consigned_out gun or a Draft-consignment hold stays excluded even when
		current_invoice is passed."""
		from ffl_core.firearm import available_serials_for_items
		own = "POS-LOCK-FA-OWN"
		consigned = "POS-LOCK-FA-CONSIGNED"
		cono = "POS-LOCK-FA-CONO"
		for s in (own, consigned, cono):
			self._receive(s)
		frappe.db.set_value("Serial No", consigned, "consigned_out", 1)
		self._draft_cono(cono)
		inv = self._draft_pos(own)
		got = available_serials_for_items([ITEM], current_invoice=inv).get(ITEM, [])
		self.assertIn(own, got, "own POS-line serial is offered")
		self.assertNotIn(consigned, got, "consigned_out stays excluded with current_invoice")
		self.assertNotIn(cono, got, "Draft-consignment hold stays excluded with current_invoice")

	def test_double_hold_pos_plus_si_stays_fail_closed(self):
		"""F(b): a serial on THIS invoice's own POS line whose WINNING reservation
		is a submitted SI (kind != 'pos') stays excluded — fail-closed. The SI hold
		is simulated (a full destination-FFL SI fixture is disproportionate); the
		decision under test is the kind/ref gate in _pos_picker_excluded_serials."""
		from ffl_core.firearm import _pos_picker_excluded_serials
		serial = "POS-LOCK-FB-DOUBLE"
		self._receive(serial)
		inv = self._draft_pos(serial)  # serial IS on inv's own line (own_serials)
		si_hold = {serial: {"kind": "sales_invoice", "ref": "SI-DOUBLE-X",
			"source": "sales_invoice"}}
		with patch("ffl_core.reservations.counter_holds", return_value=si_hold):
			excluded = _pos_picker_excluded_serials([ITEM], current_invoice=inv)
		self.assertIn(serial, excluded,
			"an SI-won double-hold stays excluded even on this invoice's own line")

	# --- a web order's pickup ticket and the gun that order reserved ----

	def _web_reserved(self, serial: str, order: str) -> str:
		"""Exactly what a paid web order writes on the gun it claims."""
		if not (frappe.db.has_column("Serial No", "woo_reserved")
				and frappe.db.has_column("POS Invoice", "woo_online_order")):
			self.skipTest("the web channel's custom fields are not migrated on this bench")
		self._receive(serial)
		frappe.db.set_value("Serial No", serial,
			{"woo_reserved": 1, "woo_order": order}, update_modified=False)
		return serial

	def _pickup_ticket(self, serial: str, order: str) -> str:
		"""A Draft POS Invoice that is `order`'s in-store pickup. The link is
		written after the save: the ticket is real, the web order need not be."""
		inv = self._draft_pos(serial)
		frappe.db.set_value("POS Invoice", inv, "woo_online_order", order,
			update_modified=False)
		return inv

	def test_a_pickup_ticket_is_offered_the_gun_its_own_web_order_reserved(self):
		from ffl_core.firearm import available_serials_for_items, available_serials_with_prices
		order = "WOO-ORD-LOCK-PICKUP-1"
		serial = "POS-LOCK-PICKUP-1"
		self._receive(serial)
		inv = self._pickup_ticket(serial, order)
		self._web_reserved(serial, order)
		# Any other ticket: the web order owns it.
		self.assertNotIn(serial, available_serials_for_items([ITEM]).get(ITEM, []))
		# Its own pickup ticket: this IS that order's sale.
		self.assertIn(serial,
			available_serials_for_items([ITEM], current_invoice=inv).get(ITEM, []))
		self.assertIn(serial, [r["serial"] for r in
			available_serials_with_prices([ITEM], current_invoice=inv).get(ITEM, [])])

	def test_a_pickup_ticket_is_not_offered_another_web_orders_gun(self):
		from ffl_core.firearm import available_serials_for_items
		mine = "POS-LOCK-PICKUP-MINE"
		theirs = "POS-LOCK-PICKUP-THEIRS"
		self._receive(mine)
		inv = self._pickup_ticket(mine, "WOO-ORD-LOCK-PICKUP-2")
		self._web_reserved(mine, "WOO-ORD-LOCK-PICKUP-2")
		self._web_reserved(theirs, "WOO-ORD-LOCK-PICKUP-3")
		got = available_serials_for_items([ITEM], current_invoice=inv).get(ITEM, [])
		self.assertIn(mine, got)
		self.assertNotIn(theirs, got, "another web order's gun stays excluded")

	def test_a_counter_ticket_is_not_offered_a_web_reserved_gun_on_its_own_line(self):
		"""An ordinary counter ticket naming a web-reserved gun is the double sale
		the channel rule exists for: being on the line is not a claim to it."""
		from ffl_core.firearm import available_serials_for_items
		serial = "POS-LOCK-PICKUP-COUNTER"
		self._receive(serial)
		inv = self._draft_pos(serial)  # no woo_online_order
		self._web_reserved(serial, "WOO-ORD-LOCK-PICKUP-4")
		self.assertNotIn(serial,
			available_serials_for_items([ITEM], current_invoice=inv).get(ITEM, []))

	def test_a_pickup_ticket_does_not_lift_another_channels_hold_of_the_same_ref(self):
		"""The order link lifts ITS channel's reservation: a marketplace hold whose
		ref merely reads the same is somebody else's sale."""
		from ffl_core.firearm import _pos_picker_excluded_serials
		if not frappe.db.has_column("Serial No", "gb_reserved"):
			self.skipTest("the marketplace channel's custom fields are not migrated on this bench")
		order = "WOO-ORD-LOCK-PICKUP-6"
		serial = "POS-LOCK-PICKUP-OTHERCHANNEL"
		self._receive(serial)
		inv = self._pickup_ticket(serial, order)
		self._web_reserved(serial, "")  # columns present; no web hold
		frappe.db.set_value("Serial No", serial,
			{"woo_reserved": 0, "gb_reserved": 1, "gb_order": order}, update_modified=False)
		self.assertIn(serial, _pos_picker_excluded_serials([ITEM], current_invoice=inv))

	def test_every_channel_link_names_a_reserving_channel(self):
		from ffl_core.compliance.channel_invoice import CHANNEL_INVOICE_LINKS
		from ffl_core.reservations import CHANNEL_RESERVE_FIELDS
		self.assertLessEqual(set(CHANNEL_INVOICE_LINKS), {spec[2] for spec in CHANNEL_RESERVE_FIELDS})

	def test_a_submitted_pickup_ticket_grants_no_exemption(self):
		"""Only a live Draft lifts its order's reservation, as for a POS hold."""
		from ffl_core.firearm import _pos_picker_excluded_serials
		order = "WOO-ORD-LOCK-PICKUP-5"
		serial = "POS-LOCK-PICKUP-SUBMITTED"
		self._receive(serial)
		inv = self._pickup_ticket(serial, order)
		self._web_reserved(serial, order)
		self.assertNotIn(serial, _pos_picker_excluded_serials([ITEM], current_invoice=inv))
		frappe.db.set_value("POS Invoice", inv, "docstatus", 1, update_modified=False)
		self.assertIn(serial, _pos_picker_excluded_serials([ITEM], current_invoice=inv))


class CoerceItemCodes(FrappeTestCase):
	"""F(c): _coerce_item_codes list / JSON-string / bare-string branches."""

	def test_list_passthrough(self):
		from ffl_core.firearm import _coerce_item_codes
		self.assertEqual(_coerce_item_codes(["A", "B"]), ["A", "B"])

	def test_json_string_list(self):
		from ffl_core.firearm import _coerce_item_codes
		self.assertEqual(_coerce_item_codes('["A","B"]'), ["A", "B"])

	def test_bare_string_is_single_code(self):
		from ffl_core.firearm import _coerce_item_codes
		self.assertEqual(_coerce_item_codes("TYPE56-762X39"), ["TYPE56-762X39"])

	def test_empty_and_none(self):
		from ffl_core.firearm import _coerce_item_codes
		self.assertEqual(_coerce_item_codes(""), [])
		self.assertEqual(_coerce_item_codes(None), [])
