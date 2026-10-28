"""Manager approval for a lowered price: the gate, the token, the record, and every path left alone.

Site tests on the real doctypes. The gate's documents are really inserted as a clerk (Sales User)
or a manager (Sales Manager) with FFL Settings → Require Manager Approval for Price Reductions on.
The builder paths (Create Order, a gunsmith pick-up, membership billing) run end to end with the
signed-in user's own approval taken away (``price_approval.is_approver`` → False), so nothing but
the path's own exemption can let them through. The transfer ticket's is in ffl_integrations
(tests/test_transfer_price_approval.py).
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, nowdate
from frappe.utils.password import update_password

from ffl_core import price_approval as pa
from ffl_core import staff_switch
from ffl_core.tests.settings_sandbox import SettingsSandbox
from ffl_core.tests.test_customer_profile import _customer, _hooks, _user

ITEM = "_Test PA Item"
PRICE = 100
PRICE_LIST = "Standard Selling"
PIN = "4826"
PASSWORD = "Approve-Pass-73!"
CLERK = "pa-clerk@example.com"
MANAGER = "pa-manager@example.com"
OTHER_CLERK = "pa-clerk2@example.com"
REASON = "Price match"


def _company() -> str:
	return frappe.defaults.get_global_default("company") or frappe.get_all("Company", limit=1, pluck="name")[0]


def _priced_item(code: str = ITEM, price: float = PRICE, **fields) -> str:
	if not frappe.db.exists("Item", code):
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": "All Item Groups",
			"stock_uom": "Nos", "is_stock_item": 0, **fields}).insert(ignore_permissions=True)
	_price(code, price)
	return code


def _price(code: str, price: float) -> None:
	name = frappe.db.get_value("Item Price", {"item_code": code, "price_list": PRICE_LIST})
	if name:
		frappe.db.set_value("Item Price", name, "price_list_rate", price)
	else:
		frappe.get_doc({"doctype": "Item Price", "item_code": code, "price_list": PRICE_LIST,
			"price_list_rate": price}).insert(ignore_permissions=True)


def _staff() -> None:
	_user(CLERK, "Sales User")
	_user(OTHER_CLERK, "Sales User")
	_user(MANAGER, "Sales Manager", "Sales User")
	update_password(MANAGER, PASSWORD)
	update_password(MANAGER, PIN, fieldname=staff_switch.PIN_FIELD)
	for user in (MANAGER,):
		frappe.cache.delete(staff_switch._pin_fail_key(user))
		frappe.cache.delete(frappe.cache.make_key(f"ffl-unlock-fail:{user}"))


def _so(rate=PRICE, customer=None, **fields):
	so = frappe.get_doc({"doctype": "Sales Order", "company": _company(), "customer": customer or _customer().name,
		"transaction_date": nowdate(), "delivery_date": add_days(nowdate(), 7), "selling_price_list": PRICE_LIST,
		"items": [{"item_code": _priced_item(), "qty": 1, "rate": rate, "delivery_date": add_days(nowdate(), 7)}],
		**fields})
	so.flags.ignore_permissions = True
	return so


def _si(rate=PRICE, **fields):
	si = frappe.get_doc({"doctype": "Sales Invoice", "company": _company(), "customer": _customer().name,
		"selling_price_list": PRICE_LIST, "items": [{"item_code": _priced_item(), "qty": 1, "rate": rate}], **fields})
	si.flags.ignore_permissions = True
	si.set("__islocal", 1)
	return si


def _approve(doc, **kw):
	args = {"approver": MANAGER, "reason": REASON, "pin": PIN, "doc": doc.as_dict()}
	args.update(kw)
	return pa.approve(**args)


def _comments(doc) -> list[str]:
	return frappe.get_all("Comment", filters={"reference_doctype": doc.doctype, "reference_name": doc.name,
		"comment_type": "Comment"}, pluck="content")


class _On(SettingsSandbox, FrappeTestCase):
	"""The switch on, the staff in place, signed back in as Administrator afterwards."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")
		self.set_single("FFL Settings", pa.SETTING, 1)
		_staff()
		_priced_item()


class Wiring(FrappeTestCase):

	def test_the_gate_and_its_comment_run_on_every_counter_sale_doctype(self):
		events = _hooks()["doc_events"]
		for dt in pa.GATED:
			self.assertIn("ffl_core.price_approval.enforce_price_approval", events[dt]["validate"], dt)
			self.assertEqual(events[dt]["on_update"], "ffl_core.price_approval.post_approval_comment", dt)

	def test_fields_on_install_and_migrate_and_the_switch_seeded(self):
		hooks = _hooks()
		self.assertIn("ffl_core.setup.price_approval.ensure", hooks["after_install"])
		self.assertIn("ffl_core.setup.price_approval.ensure", hooks["after_migrate"])
		self.assertIn("ffl_core.price_approval.ensure_default", hooks["after_install"])
		with open(frappe.get_app_path("ffl_core", "patches.txt")) as f:
			self.assertIn("ffl_core.patches.seed_price_approval_off", f.read())
		for dt in pa.GATED:
			meta = frappe.get_meta(dt)
			for field in (*pa.RECORD_FIELDS, pa.TOKEN_FIELD):
				self.assertTrue(meta.get_field(field), f"{dt}.{field}")
				self.assertTrue(meta.get_field(field).no_copy, f"{dt}.{field} no_copy")


class Setting(SettingsSandbox, FrappeTestCase):

	def test_a_new_store_has_it_on_an_existing_store_off_and_a_choice_stays(self):
		from ffl_core.patches import seed_price_approval_off as patch_off

		self.clear_single("FFL Settings", pa.SETTING)
		self.assertTrue(pa.enabled(), "never saved: the docfield default, on")
		pa.ensure_default()
		self.assertTrue(pa.enabled())
		self.clear_single("FFL Settings", pa.SETTING)
		patch_off.execute()   # a store migrating onto this release
		self.assertFalse(pa.enabled())
		self.set_single("FFL Settings", pa.SETTING, 1)
		patch_off.execute()
		pa.ensure_default()
		self.assertTrue(pa.enabled(), "a stated choice is kept by both")


class Gate(_On):

	def test_a_clerk_lowering_a_price_is_refused(self):
		frappe.set_user(CLERK)
		with self.assertRaisesRegex(pa.PriceApprovalRequired, "Ask a manager"):
			_so(rate=80).insert()

	def test_list_price_or_more_asks_nothing(self):
		frappe.set_user(CLERK)
		for rate in (PRICE, 120):
			so = _so(rate=rate)
			so.insert()
			self.assertFalse(so.get(pa.BY))

	def test_switch_off_is_the_old_behaviour(self):
		self.set_single("FFL Settings", pa.SETTING, 0)
		frappe.set_user(CLERK)
		so = _so(rate=50)
		so.insert()
		self.assertFalse(so.get(pa.BY))
		self.assertEqual(flt(frappe.db.get_value("Sales Order Item", so.items[0].name, "rate")), 50)

	def test_a_manager_signed_in_approves_their_own_and_it_is_recorded(self):
		frappe.set_user(MANAGER)
		so = _so(rate=80)
		so.insert()
		row = frappe.db.get_value("Sales Order", so.name, list(pa.RECORD_FIELDS), as_dict=True)
		self.assertEqual(row[pa.BY], MANAGER)
		self.assertEqual(row[pa.REASON], "Manager's own price")
		self.assertTrue(row[pa.ON])
		self.assertEqual(len(_comments(so)), 1)
		self.assertIn("→", _comments(so)[0])

	def test_an_approval_token_lets_the_clerk_save_and_is_recorded_never_stored(self):
		frappe.set_user(CLERK)
		so = _so(rate=80)
		out = _approve(so)
		self.assertEqual([(l["item_code"], l["list_rate"], l["rate"]) for l in out["lines"]], [(ITEM, PRICE, 80)])
		so.set(pa.TOKEN_FIELD, out["token"])
		so.insert()
		row = frappe.db.get_value("Sales Order", so.name, [*pa.RECORD_FIELDS, pa.TOKEN_FIELD], as_dict=True)
		self.assertEqual((row[pa.BY], row[pa.REASON]), (MANAGER, REASON))
		self.assertFalse(row[pa.TOKEN_FIELD], "the token is carried in, never stored")
		[comment] = _comments(so)
		self.assertIn(REASON, comment)
		self.assertIn(ITEM, comment)

	def test_a_further_reduction_needs_a_new_approval_raising_does_not(self):
		frappe.set_user(CLERK)
		so = _so(rate=80)
		so.set(pa.TOKEN_FIELD, _approve(so)["token"])
		so.insert()
		def fresh():   # a new request's copy
			doc = frappe.get_doc("Sales Order", so.name)
			doc.flags.ignore_permissions = True
			return doc

		fresh().save()   # kept: nothing asks
		doc = fresh()
		doc.items[0].rate = 70
		with self.assertRaisesRegex(pa.PriceApprovalRequired, "Ask a manager"):
			doc.save()
		doc = fresh()
		doc.items[0].rate = 95
		doc.save()   # raised: nothing asks
		doc = fresh()
		doc.items[0].rate = 70
		doc.set(pa.TOKEN_FIELD, _approve(doc)["token"])
		doc.save()
		self.assertEqual(len(_comments(so)), 2)

	def test_a_rate_below_what_was_approved_is_refused(self):
		frappe.set_user(CLERK)
		so = _so(rate=80)
		token = _approve(so)["token"]
		so.items[0].rate = 60
		so.set(pa.TOKEN_FIELD, token)
		with self.assertRaisesRegex(pa.PriceApprovalRequired, "Lower than the manager approved"):
			so.insert()

	def test_the_token_is_single_use_and_only_for_who_asked(self):
		frappe.set_user(CLERK)
		so = _so(rate=80)
		token = _approve(so)["token"]
		frappe.set_user(OTHER_CLERK)
		other = _so(rate=80)
		other.set(pa.TOKEN_FIELD, token)
		with self.assertRaisesRegex(pa.PriceApprovalRequired, "another sale"):
			other.insert()
		frappe.set_user(CLERK)
		so.set(pa.TOKEN_FIELD, token)
		so.insert()
		again = _so(rate=80)
		again.set(pa.TOKEN_FIELD, token)
		with self.assertRaisesRegex(pa.PriceApprovalRequired, "expired or was used"):
			again.insert()

	def test_a_token_for_one_doctype_is_not_good_for_another(self):
		frappe.set_user(CLERK)
		token = _approve(_so(rate=80))["token"]
		si = _si(rate=80)
		si.set(pa.TOKEN_FIELD, token)
		with self.assertRaisesRegex(pa.PriceApprovalRequired, "another sale"):
			si.insert()

	def test_a_save_that_fails_after_taking_the_token_gets_it_back(self):
		frappe.set_user(CLERK)
		so = _so(rate=80)
		token = _approve(so)["token"]
		key = pa._TOKEN_KEY + token
		pa.take_token(token, so)
		self.assertIsNone(frappe.cache.get_value(key, expires=True))
		frappe.db.after_rollback.run()   # what a rollback runs
		self.assertTrue(frappe.cache.get_value(key, expires=True))

	def test_wrong_pins_count_down_five_turn_it_off_and_the_password_still_works(self):
		frappe.set_user(CLERK)
		so = _so(rate=80)
		with self.assertRaisesRegex(staff_switch.WrongPassword, "4 tries left"):
			_approve(so, pin="1357")
		for _ in range(staff_switch.PIN_TRIES - 1):
			with self.assertRaises(staff_switch.WrongPassword):
				_approve(so, pin="1357")
		with self.assertRaisesRegex(staff_switch.WrongPassword, "PIN is off"):
			_approve(so)   # even the right PIN, now
		self.assertTrue(_approve(so, pin=None, password=PASSWORD)["token"])
		self.assertTrue(_approve(so)["token"], "the password turned the PIN back on")
		with self.assertRaises(staff_switch.WrongPassword):
			_approve(so, pin=None, password="wrong")

	def test_only_a_manager_from_the_list_approves_with_a_reason(self):
		frappe.set_user(CLERK)
		so = _so(rate=80)
		for who in (CLERK, "Administrator", "nobody@example.com"):
			with self.assertRaisesRegex(staff_switch.WrongPassword, "Pick a manager"):
				_approve(so, approver=who)
		with self.assertRaisesRegex(frappe.ValidationError, "reason"):
			_approve(so, reason="  ")
		with self.assertRaisesRegex(frappe.ValidationError, "Nothing on this sale"):
			_approve(_so(rate=PRICE))

	def test_an_invoice_discount_needs_approval(self):
		frappe.set_user(CLERK)
		so = _so(additional_discount_percentage=10)
		with self.assertRaisesRegex(pa.PriceApprovalRequired, "invoice discount"):
			so.insert()
		so = _so(additional_discount_percentage=10)
		out = _approve(so)
		self.assertEqual(out["discount"]["percentage"], 10)
		so.set(pa.TOKEN_FIELD, out["token"])
		so.insert()
		so = frappe.get_doc("Sales Order", so.name)
		so.flags.ignore_permissions = True
		so.additional_discount_percentage = 15
		with self.assertRaisesRegex(pa.PriceApprovalRequired, "invoice discount"):
			so.save()

	def test_the_record_is_server_only(self):
		frappe.set_user(CLERK)
		so = _so(**{pa.BY: MANAGER, pa.REASON: "forged"})
		so.insert()
		self.assertFalse(frappe.db.get_value("Sales Order", so.name, pa.BY), "a save cannot forge one")
		frappe.set_user(MANAGER)
		so = _so(rate=80)
		so.insert()
		frappe.set_user(CLERK)
		so.set(pa.BY, None)
		so.set(pa.REASON, "edited")
		so.save()
		self.assertEqual(frappe.db.get_value("Sales Order", so.name, [pa.BY, pa.REASON]),
			(MANAGER, "Manager's own price"), "nor wipe one")

	def test_a_pricing_rule_discount_asks_nothing(self):
		rule = frappe.get_doc({"doctype": "Pricing Rule", "title": "_Test PA rule " + frappe.generate_hash(length=6),
			"apply_on": "Item Code", "items": [{"item_code": ITEM}], "selling": 1, "price_or_product_discount": "Price",
			"rate_or_discount": "Discount Percentage", "discount_percentage": 10, "company": _company(),
			"priority": 1})
		rule.insert(ignore_permissions=True)
		self.addCleanup(frappe.db.set_value, "Pricing Rule", rule.name, "disable", 1)
		frappe.set_user(CLERK)
		so = _so(rate=PRICE)
		so.insert()
		self.assertEqual(flt(so.items[0].rate), 90, "ERPNext applied the rule")
		self.assertFalse(so.get(pa.BY))

	def test_an_invoice_made_from_an_approved_order_asks_nothing_and_carries_the_record(self):
		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

		frappe.set_user(CLERK)
		so = _so(rate=80)
		so.set(pa.TOKEN_FIELD, _approve(so)["token"])
		so.insert()
		so.submit()
		frappe.set_user("Administrator")   # a clerk has no Sales Invoice rights; no own approval either
		with patch("ffl_core.price_approval.is_approver", return_value=False):
			si = make_sales_invoice(so.name)
			si.flags.ignore_permissions = True
			si.insert()
		self.assertEqual(flt(si.items[0].rate), 80)
		self.assertEqual(si.get(pa.BY), MANAGER)


class Check(_On):

	def test_the_screen_check_mirrors_the_gate(self):
		frappe.set_user(CLERK)
		res = pa.check(doc=_so(rate=80).as_dict())
		self.assertTrue(res["needs_approval"])
		self.assertFalse(res["approver"])
		self.assertEqual([l["rate"] for l in res["lines"]], [80])
		self.assertIn(MANAGER, [a["user"] for a in res["approvers"]])
		self.assertTrue(next(a for a in res["approvers"] if a["user"] == MANAGER)["has_pin"])
		self.assertNotIn(CLERK, [a["user"] for a in res["approvers"]])
		self.assertFalse(pa.check(doc=_so(rate=PRICE).as_dict())["needs_approval"])
		frappe.set_user("Administrator")   # a clerk has no Sales Invoice rights
		with patch("ffl_core.price_approval.is_approver", return_value=False):
			self.assertFalse(pa.check(doc=_si(rate=80, is_return=1).as_dict())["needs_approval"])
			self.assertTrue(pa.check(doc=_si(rate=80).as_dict())["needs_approval"])

	def test_a_saved_draft_is_judged_against_what_it_saved(self):
		frappe.set_user(CLERK)
		so = _so(rate=80)
		so.set(pa.TOKEN_FIELD, _approve(so)["token"])
		so.insert()
		self.assertFalse(pa.check(doc=so.as_dict())["needs_approval"])
		so.items[0].rate = 75
		self.assertTrue(pa.check(doc=so.as_dict())["needs_approval"])

	def test_someone_who_saves_no_sale_is_refused_at_the_door(self):
		frappe.set_user(_user("pa-buyer@example.com", "Purchase User"))
		for call in (lambda: pa.check(doc=_so(rate=80).as_dict()),
				lambda: pa.approve(MANAGER, REASON, pin=PIN, doc=_so(rate=80).as_dict())):
			with self.assertRaises(frappe.PermissionError):
				call()

	def test_a_manager_needs_no_dialog(self):
		frappe.set_user(MANAGER)
		res = pa.check(doc=_so(rate=80).as_dict())
		self.assertTrue(res["approver"])
		self.assertEqual(res["approvers"], [])
		boot = frappe._dict()
		pa.extend_bootinfo(boot)
		self.assertEqual(boot.ffl_price_approval, 0)
		frappe.set_user(CLERK)
		pa.extend_bootinfo(boot)
		self.assertEqual(boot.ffl_price_approval, 1)


class ListPrice(_On):

	def test_a_gun_is_priced_from_its_own_serial_with_its_sale(self):
		code = "_Test PA Gun"
		if not frappe.db.exists("Item", code):
			frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": "All Item Groups",
				"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
				"uniform_sell_price": 0}).insert(ignore_permissions=True)
		serial = "PA-" + frappe.generate_hash(length=8).upper()
		frappe.get_doc({"doctype": "Serial No", "serial_no": serial, "item_code": code, "company": _company()}
			).insert(ignore_permissions=True)
		frappe.db.set_value("Serial No", serial, "sell_price", 900)
		doc = frappe._dict(doctype="Sales Invoice", selling_price_list=PRICE_LIST, items=[])
		row = frappe._dict(item_code=code, serial_no=serial, qty=1, uom="Nos", conversion_factor=1)
		self.assertEqual(pa.list_rate(doc, row), (900, True))
		frappe.db.set_value("Serial No", serial, {"sale_sell_price": 800, "sale_from": nowdate()})
		self.assertEqual(pa.list_rate(doc, row), (800, True), "a sale running is the price")
		frappe.db.set_value("Serial No", serial, "sell_price", 0)
		self.assertEqual(pa.list_rate(doc, row), (0, False), "an un-priced gun has nothing to lower")


class LeftAlone(_On):
	"""Each path the owner listed as never asked: a clerk's save with a price under list goes through."""

	def setUp(self):
		super().setUp()
		frappe.set_user(CLERK)

	def _gate(self, doc):
		pa.enforce_price_approval(doc)
		self.assertFalse(doc.get(pa.BY))

	def test_control_the_same_invoice_is_refused(self):
		with self.assertRaises(pa.PriceApprovalRequired):
			pa.enforce_price_approval(_si(rate=10))

	def test_returns_and_pos_consolidation(self):
		self._gate(_si(rate=10, is_return=1))
		self._gate(_si(rate=10, is_consolidated=1))

	def test_web_and_gunbroker_mirrors(self):
		for field in ("woo_online_order", "gunbroker_order"):
			if not frappe.get_meta("Sales Invoice").get_field(field):
				continue
			si = _si(rate=10)
			si.set(field, "PA-MIRROR")
			pa.enforce_price_approval(si)

	def test_an_invoice_an_extension_books_a_consignment_settlement_at_cost(self):
		si = _si(rate=10, po_no="CONO-TEST")
		with patch("ffl_core.compliance.channel_invoice.extension_exclusion_fields", return_value=("po_no",)):
			self._gate(si)

	def test_a_counter_pickup_ticket(self):
		inv = frappe.get_doc({"doctype": "POS Invoice", "customer": _customer().name, "ffl_counter_order": "SINV-PA",
			"items": [{"item_code": ITEM, "qty": 1, "rate": 0}]})
		inv.set("__islocal", 1)
		self._gate(inv)

	def test_trade_in_credit_and_gunsmith_deposit_lines(self):
		from ffl_core.setup.locked_lines import locked_items

		si = _si(rate=PRICE)
		for code in locked_items():
			si.append("items", {"item_code": code, "qty": 1, "rate": -50})
		self._gate(si)

	def test_a_line_a_distributor_fills(self):
		si = _si(rate=10)
		si.items[0].delivered_by_supplier = 1
		self._gate(si)
		si = _si(rate=10)
		si.items[0].ffl_to_store_distributor = "RSR"
		self._gate(si)

	def test_background_jobs_migrate_import_and_our_own_builders(self):
		with patch.object(frappe.local, "job", frappe._dict(method="x"), create=True):
			self._gate(_si(rate=10))
		for flag in ("in_import", "in_patch", "in_migrate", "in_install"):
			with patch.dict(frappe.flags, {flag: True}):
				self._gate(_si(rate=10))
		self._gate(pa.mark_system_priced(_si(rate=10)))


# ------------------------------------------------------------------ builders, end to end

def _no_own_approval(test):
	p = patch("ffl_core.price_approval.is_approver", return_value=False)
	p.start()
	test.addCleanup(p.stop)


from ffl_core.tests.test_gunsmith_work_order import _Base as _GunsmithBase  # noqa: E402
from ffl_core.tests.test_gunsmith_work_order import _job, _pay, _ready  # noqa: E402


class GunsmithPickup(SettingsSandbox, _GunsmithBase):

	def test_the_jobs_charges_are_the_bill_and_a_lower_one_at_the_register_asks(self):
		from ffl_core import gunsmith

		self.set_single("FFL Settings", pa.SETTING, 1)
		_price("_Test GW Part", 60)   # the job charges 40 for the part
		_no_own_approval(self)
		job = _ready(_job(self.cust), [("_Test GW Part", 40)])
		pick = frappe.get_doc("POS Invoice", gunsmith.open_pickup(job.name)["pos_invoice"])
		self.assertEqual([flt(r.rate) for r in pick.items], [40])
		pick.items[0].rate = 30
		pick.flags.ignore_permissions = True
		with self.assertRaises(pa.PriceApprovalRequired):
			pick.save()
		pick.reload()
		pick = _pay(pick.name)
		self.assertEqual(pick.docstatus, 1)
		self.assertFalse(pick.get(pa.BY))


from ffl_core.tests.test_membership import _Base as _MembershipBase  # noqa: E402
from ffl_core.tests.test_membership import _sell  # noqa: E402


class MembershipBilling(_MembershipBase):

	def test_the_plan_price_is_billed_whatever_the_item_price_says(self):
		self.set_single("FFL Settings", pa.SETTING, 1)
		for item in (self.plan.item, self.plan.registration_item):
			_price(item, 999)
		_no_own_approval(self)
		_m, si = _sell(self.customer, self.plan.name)
		self.assertEqual(frappe.db.get_value("Sales Invoice", si, "docstatus"), 1)
		self.assertFalse(frappe.db.get_value("Sales Invoice", si, pa.BY))


from ffl_core.tests.test_build_order import DEST_FFL, BuildOrderMixin  # noqa: E402


class CreateOrder(SettingsSandbox, BuildOrderMixin, FrappeTestCase):

	def setUp(self):
		frappe.set_user("Administrator")
		self.set_single("FFL Settings", pa.SETTING, 1)
		_staff()
		self.serial = self._stocked_serial("PA-CO-" + frappe.generate_hash(length=6).upper(),
			warehouse=self._make_warehouse("PA Order WH"))
		frappe.db.set_value("Serial No", self.serial, "sell_price", 1000)
		_no_own_approval(self)

	def _payload(self, rate):
		return {"buyer_type": "individual", "customer": self._indiv("PA Order Buyer " + frappe.generate_hash(length=5)),
			"destination_ffl": DEST_FFL, "dispose_now": 0, "payment_received": 0,
			"pos_profile": frappe.db.get_value("POS Profile", {"company": self.company, "disabled": 0}, "name"),
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": rate, "serials": [self.serial]}]}

	def test_a_gun_below_its_price_needs_a_manager_and_the_invoice_records_it(self):
		from ffl_core.api import manual_order

		payload = self._payload(700)
		res = pa.check(payload=payload)
		self.assertTrue(res["needs_approval"])
		self.assertEqual([(l["list_rate"], l["rate"]) for l in res["lines"]], [(1000, 700)])
		with self.assertRaises(pa.PriceApprovalRequired):
			manual_order.create_order(dict(payload))
		payload["price_approval"] = pa.approve(MANAGER, REASON, pin=PIN, payload=payload)["token"]
		out = manual_order.create_order(payload)
		# The order line has no gun yet (no serial on a Sales Order line), so nothing on it is below
		# a list price; the invoice line carries the gun and its price, and the approval.
		self.assertEqual(frappe.db.get_value("Sales Invoice", out["sales_invoice"], [pa.BY, pa.REASON]),
			(MANAGER, REASON))
		self.assertTrue(frappe.db.exists("Comment", {"reference_doctype": "Sales Invoice",
			"reference_name": out["sales_invoice"], "content": ("like", "%1,000.00 → %700.00%")}))
		self.assertIsNone(frappe.cache.get_value(pa._TOKEN_KEY + payload["price_approval"], expires=True))
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice Item", {"parent": out["sales_invoice"]}, "rate")), 700)
		# Editing the order keeps its price; lowering it again asks.
		edit = self._payload(700)
		edit["customer"] = payload["customer"]
		self.assertFalse(pa.check(payload=edit, sales_invoice=out["sales_invoice"])["needs_approval"])
		edit["items"][0]["rate"] = 650
		self.assertTrue(pa.check(payload=edit, sales_invoice=out["sales_invoice"])["needs_approval"])
		frappe.db.savepoint("pa_edit")   # a refused request rolls back: the cancel inside it too
		with self.assertRaises(pa.PriceApprovalRequired):
			manual_order.update_order(out["sales_invoice"], dict(edit))
		frappe.db.rollback(save_point="pa_edit")
		edit["items"][0]["rate"] = 700
		edited = manual_order.update_order(out["sales_invoice"], edit)   # same price: no token, no manager
		self.assertTrue(edited["ok"])
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice Item", {"parent": edited["sales_invoice"]}, "rate")), 700)

	def test_at_its_price_nothing_asks(self):
		from ffl_core.api import manual_order

		payload = self._payload(1000)
		self.assertFalse(pa.check(payload=payload)["needs_approval"])
		self.assertTrue(manual_order.create_order(payload)["ok"])

	def test_an_order_token_is_not_good_on_a_form(self):
		payload = self._payload(700)
		token = pa.approve(MANAGER, REASON, pin=PIN, payload=payload)["token"]
		so = _so(rate=80)
		so.set(pa.TOKEN_FIELD, token)
		with self.assertRaisesRegex(pa.PriceApprovalRequired, "another sale"):
			so.insert()
