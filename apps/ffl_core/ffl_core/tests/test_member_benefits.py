"""Member benefits (ffl_core/membership_benefits.py): a plan's member prices at the register.

Site tests on the bootstrap company ("FFL Test") and the gunsmith fixtures' register profile (6%
sales tax), against the real doctypes; nothing is stubbed. The CA tax case has its own stand-in
company (7.75% sales tax and the 11% excise on goods). The two-session concurrency case commits on
purpose and deletes what it made.
"""
from __future__ import annotations

import threading
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, getdate, nowdate

from ffl_core import membership_benefits as mb
from ffl_core import price_approval
from ffl_core.api import pos_ticket
from ffl_core.setup.member_benefits import (BENEFICIARY_FIELD, BENEFIT_FIELD, MEMBER_RATE_FIELD,
	MEMBERSHIP_FIELD, REASON_FIELD)
from ffl_core.tests import test_gunsmith_work_order as gw
from ffl_core.tests import test_price_approval as tpa
from ffl_core.tests.test_customer_profile import _uniq
from ffl_core.tests.settings_sandbox import SettingsSandbox

COMPANY = gw.COMPANY
PRICE_LIST = "Standard Selling"
MEMBER_LIST = "_Test MB Member Prices"
GROUP, SUB_GROUP = "_Test MB Range", "_Test MB Range Lanes"


def _tax_accounts(company: str) -> list[str]:
	return sorted({r.account_head for r in frappe.get_all("Sales Taxes and Charges",
		filters={"parenttype": "Sales Taxes and Charges Template", "parent": ("in", frappe.get_all(
			"Sales Taxes and Charges Template", filters={"company": company}, pluck="name"))},
		fields=["account_head"]) if r.account_head})


def _exempt_template(company: str, title: str) -> str:
	"""A 0% Item Tax Template on every account the company's sales tax templates charge."""
	name = frappe.db.get_value("Item Tax Template", {"company": company, "title": title}, "name")
	if name:
		return name
	return frappe.get_doc({"doctype": "Item Tax Template", "title": title, "company": company,
		"taxes": [{"tax_type": a, "tax_rate": 0} for a in _tax_accounts(company)]}).insert(ignore_permissions=True).name


def _group(name: str, parent: str = "All Item Groups") -> str:
	if not frappe.db.exists("Item Group", name):
		frappe.get_doc({"doctype": "Item Group", "item_group_name": name, "parent_item_group": parent,
			"is_group": 1}).insert(ignore_permissions=True)
	return name


def _price(item_code: str, rate: float, price_list: str = PRICE_LIST) -> None:
	name = frappe.db.get_value("Item Price", {"item_code": item_code, "price_list": price_list, "selling": 1})
	if name:
		frappe.db.set_value("Item Price", name, "price_list_rate", rate)
	else:
		frappe.get_doc({"doctype": "Item Price", "item_code": item_code, "price_list": price_list, "selling": 1,
			"price_list_rate": rate}).insert(ignore_permissions=True)


def _item(prefix: str, price: float | None = None, group: str = "Parts", exempt: str | None = None, **fields) -> str:
	code = f"_MB-{prefix}-{frappe.generate_hash(length=5)}".upper()
	frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": group, "stock_uom": "Nos",
		"is_stock_item": 0, "is_sales_item": 1, "include_item_in_manufacturing": 0,
		"taxes": [{"item_tax_template": exempt}] if exempt else [], **fields}).insert(ignore_permissions=True)
	if price is not None and not frappe.db.exists("Item Price", {"item_code": code, "price_list": PRICE_LIST}):
		_price(code, price)
	return code


def _plan(*benefits, name: str | None = None) -> frappe._dict:
	from ffl_core.tests.test_membership import _item as dues_item

	doc = frappe.get_doc({"doctype": "Membership Plan", "plan_name": name or _uniq("Plan"), "active": 1,
		"billing_period": "Yearly", "price": 650, "item": dues_item("DUES"), "registration_fee": 0,
		"auto_renew_default": 0, "benefits": [dict(b) for b in benefits]})
	doc.insert(ignore_permissions=True)
	return doc


def _benefit(on, target, kind=mb.PERCENT, value=0, uses=0, days=None, label=None) -> dict:
	field = {mb.ON_ITEM: "item", mb.ON_GROUP: "item_group", mb.ON_PRICE_LIST: "price_list"}[on]
	return {"applies_to": on, field: target, "benefit_type": kind, "value": value, "uses_per_day": uses,
		"days": days, "label": label}


def _member(customer: str, plan, status: str = "Active", grace_until=None) -> str:
	today = getdate(nowdate())
	doc = frappe.get_doc({"doctype": "Membership", "customer": customer, "plan": plan.name, "status": status,
		"company": COMPANY, "start_date": add_days(today, -30), "current_period_start": add_days(today, -30),
		"current_period_end": add_days(today, 334), "grace_until": grace_until, "auto_renew": 0})
	doc.flags.via_membership = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _ticket(customer: str, *lines, submit: bool = False):
	"""A register ticket built on the server the way the register's own save builds it."""
	inv = frappe.get_doc({"doctype": "POS Invoice", "company": COMPANY, "pos_profile": gw.PROFILE, "customer": customer,
		"items": [{"item_code": code, "qty": qty, **({"rate": rate} if rate is not None else {})}
			for code, qty, rate in (l if len(l) == 3 else (*l, None) for l in lines)]})
	inv.set_missing_values()
	inv.flags.ignore_permissions = True
	inv.insert()
	if submit:
		return gw._pay(inv.name)
	return frappe.get_doc("POS Invoice", inv.name)


def _line(row) -> tuple:
	return (flt(row.price_list_rate), flt(row.rate), flt(row.discount_amount), flt(row.discount_percentage),
		row.get(REASON_FIELD))


class _Base(gw._Base):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import member_benefits

		member_benefits.ensure()
		cls.exempt = _exempt_template(COMPANY, "_Test MB No Tax")
		_group(GROUP)
		_group(SUB_GROUP, GROUP)
		if not frappe.db.exists("Price List", MEMBER_LIST):
			frappe.get_doc({"doctype": "Price List", "price_list_name": MEMBER_LIST, "selling": 1, "currency": "USD",
				"enabled": 1}).insert(ignore_permissions=True)

	def setUp(self):
		super().setUp()
		frappe.message_log = []


class Wiring(FrappeTestCase):

	def test_hooks(self):
		from ffl_core.tests.test_customer_profile import _hooks

		hooks = _hooks()
		for dt in ("POS Invoice", "Sales Invoice"):
			before = hooks["doc_events"][dt]["before_validate"]
			at = before.index("ffl_core.membership_benefits.apply_member_prices")
			self.assertGreater(at, before.index("ffl_core.compliance.consignment.freeze_consignment_commission"), dt)
			self.assertGreater(at, before.index("ffl_core.compliance.locked_lines.pin_locked_lines"), dt)
			validate = hooks["doc_events"][dt]["validate"]
			self.assertLess(validate.index("ffl_core.membership_benefits.settle_member_lines"),
				validate.index("ffl_core.compliance.locked_lines.assert_locked_lines"), dt)
		for when in ("after_install", "after_migrate"):
			self.assertIn("ffl_core.setup.member_benefits.ensure", hooks[when])


class PlanConfig(_Base):

	def test_a_free_taxed_item_is_refused_at_save(self):
		gun_part = _item("TAXED", 30)
		self.assertTrue(mb.taxed_here(gun_part, COMPANY))
		for b in (_benefit(mb.ON_ITEM, gun_part, mb.PERCENT, 100), _benefit(mb.ON_ITEM, gun_part, mb.FIXED, 0),
				_benefit(mb.ON_ITEM, gun_part, mb.AMOUNT, 30)):
			with self.subTest(b=b), self.assertRaisesRegex(frappe.ValidationError, "cannot be given for free"):
				_plan(b)

	def test_free_untaxed_service_saves(self):
		lane = _item("LANE", 20, group=SUB_GROUP, exempt=self.exempt)
		self.assertFalse(mb.taxed_here(lane, COMPANY))
		plan = _plan(_benefit(mb.ON_ITEM, lane, mb.PERCENT, 100, days="mon,TUESDAY, fri"))
		self.assertEqual(plan.benefits[0].days, "Mon, Tue, Fri")

	def test_group_wide_free_saves_and_names_the_taxed_items(self):
		group = _group(_uniq("_Test MB Mixed"))
		taxed = _item("MIXT", 10, group=group)
		_item("MIXF", 10, group=group, exempt=self.exempt)
		_plan(_benefit(mb.ON_GROUP, group, mb.PERCENT, 100))
		self.assertTrue(any(taxed in str(m) for m in frappe.message_log), frappe.message_log)

	def test_bad_rows(self):
		item = _item("BAD", 10)
		for b in (_benefit(mb.ON_ITEM, item, mb.LIST_PRICE), _benefit(mb.ON_ITEM, item, mb.PERCENT, 0),
				_benefit(mb.ON_ITEM, item, mb.PERCENT, 120), _benefit(mb.ON_ITEM, item, mb.AMOUNT, 0),
				_benefit(mb.ON_ITEM, item, mb.PERCENT, 10, days="Funday")):
			with self.subTest(b=b), self.assertRaises(frappe.ValidationError):
				_plan(b)

	def test_under_cost_warns_only(self):
		item = _item("COST", 30)
		frappe.db.set_value("Item", item, "valuation_rate", 20)
		_plan(_benefit(mb.ON_ITEM, item, mb.FIXED, 15))
		self.assertTrue(any("under cost" in str(m) for m in frappe.message_log), frappe.message_log)

	def test_taxed_discount_names_the_accountant(self):
		item = _item("TXD", 30)
		_plan(_benefit(mb.ON_ITEM, item, mb.PERCENT, 10))
		self.assertTrue(any("accountant" in str(m) for m in frappe.message_log), frappe.message_log)

	def test_summary_lines(self):
		item = _item("SUM", 30)
		plan = _plan(_benefit(mb.ON_ITEM, item, mb.PERCENT, 50, uses=1, days="Mon,Tue"),
			_benefit(mb.ON_PRICE_LIST, MEMBER_LIST, mb.LIST_PRICE))
		self.assertEqual(mb.summarize_benefits(plan.name),
			[f"{item}: 50% off · 1 a day · Mon, Tue", f"{MEMBER_LIST}: member prices"])


class LinePrices(_Base):
	"""Each benefit type on a register ticket: list price, rate, discount per unit, reason."""

	def setUp(self):
		super().setUp()
		self.cust = gw._customer().name

	def test_percent_amount_fixed(self):
		a, b, c = _item("PCT", 30), _item("AMT", 30), _item("FIX", 30)
		plan = _plan(_benefit(mb.ON_ITEM, a, mb.PERCENT, 20), _benefit(mb.ON_ITEM, b, mb.AMOUNT, 5),
			_benefit(mb.ON_ITEM, c, mb.FIXED, 18))
		m = _member(self.cust, plan)
		inv = _ticket(self.cust, (a, 1), (b, 1), (c, 1))
		name = plan.plan_name
		self.assertEqual([_line(r) for r in inv.items], [
			(30, 24, 6, 20, f"Member 20% ({name})"),
			(30, 25, 5, 0, f"Member $ 5.00 off ({name})"),
			(30, 18, 12, 0, f"Member price ({name})"),
		])
		row = inv.items[0]
		self.assertEqual((row.get(BENEFIT_FIELD), row.get(MEMBERSHIP_FIELD), row.get(BENEFICIARY_FIELD),
			flt(row.get(MEMBER_RATE_FIELD))), (plan.benefits[0].name, m, self.cust, 24))
		self.assertEqual(flt(inv.net_total), 67)
		self.assertEqual(flt(inv.total_taxes_and_charges), 4.02, "6% on the member prices")
		# Saved again (the register's next save) it stays.
		inv.save()
		self.assertEqual(_line(frappe.get_doc("POS Invoice", inv.name).items[0]), (30, 24, 6, 20, f"Member 20% ({name})"))

	def test_price_list_price_to_the_cent(self):
		"""Ammo at 0.45, member list 0.36, 500 rounds: 0.09 a round, 180.00 the line, 45.00 saved."""
		ammo = _item("AMMO", 0.45)
		_price(ammo, 0.36, MEMBER_LIST)
		plan = _plan(_benefit(mb.ON_PRICE_LIST, MEMBER_LIST, mb.LIST_PRICE))
		_member(self.cust, plan)
		inv = _ticket(self.cust, (ammo, 500))
		row = inv.items[0]
		self.assertEqual(_line(row), (0.45, 0.36, 0.09, 0, f"Member price ({plan.plan_name})"))
		self.assertEqual(flt(row.amount), 180.0)
		self.assertEqual(flt((row.price_list_rate - row.rate) * row.qty, 2), 45.0)

	def test_a_non_member_pays_the_list_price(self):
		item = _item("NM", 30)
		_plan(_benefit(mb.ON_ITEM, item, mb.PERCENT, 20))
		inv = _ticket(self.cust, (item, 1))
		self.assertEqual(_line(inv.items[0]), (30, 30, 0, 0, None))
		self.assertFalse(inv.items[0].get(BENEFIT_FIELD))

	def test_sale_or_member_whichever_is_lower(self):
		"""Regular 30 on sale at 25, member 20% = 24: list 25, rate 24, 1.00 off. On sale at 22: the sale
		price, no benefit recorded and no use taken."""
		group = _group(_uniq("_Test MB Promo"))
		on25 = _item("S25", None, group=group, uniform_sell_price=1, standard_rate=30, sale_price=25)
		on22 = _item("S22", None, group=group, uniform_sell_price=1, standard_rate=30, sale_price=22)
		self.assertEqual(flt(frappe.db.get_value("Item Price", {"item_code": on25, "price_list": PRICE_LIST},
			"price_list_rate")), 25, "the sale is in the Item Price")
		plan = _plan(_benefit(mb.ON_GROUP, group, mb.PERCENT, 20, uses=1))
		_member(self.cust, plan)
		inv = _ticket(self.cust, (on22, 1), (on25, 1))
		self.assertEqual(_line(inv.items[0]), (22, 22, 0, 0, None))
		self.assertFalse(inv.items[0].get(BENEFIT_FIELD))
		self.assertEqual(_line(inv.items[1]), (25, 24, 1, 0, f"Member 20% ({plan.plan_name})"),
			"the 22 line took no use, so the 25 line has it")

	def test_lowest_of_several_plans_and_overlapping_rows(self):
		item = _item("MULTI", 30)
		basic = _plan(_benefit(mb.ON_GROUP, "Parts", mb.PERCENT, 10))
		club = _plan(_benefit(mb.ON_ITEM, item, mb.FIXED, 20))
		_member(self.cust, basic)
		m = _member(self.cust, club)
		row = _ticket(self.cust, (item, 1)).items[0]
		self.assertEqual((flt(row.rate), row.get(MEMBERSHIP_FIELD)), (20, m))
		other = gw._customer().name
		both = _plan(_benefit(mb.ON_ITEM, item, mb.PERCENT, 10), _benefit(mb.ON_ITEM, item, mb.AMOUNT, 5))
		_member(other, both)
		row = _ticket(other, (item, 1)).items[0]
		self.assertEqual((flt(row.rate), row.get(BENEFIT_FIELD)), (25, both.benefits[1].name))

	def test_free_untaxed_lane_and_taxed_item_in_the_same_group(self):
		lane = _item("FREE", 20, group=SUB_GROUP, exempt=self.exempt)
		taxed = _item("TAXG", 20, group=SUB_GROUP)
		plan = _plan(_benefit(mb.ON_GROUP, GROUP, mb.PERCENT, 100))
		_member(self.cust, plan)
		inv = _ticket(self.cust, (lane, 1), (taxed, 1))
		self.assertEqual(_line(inv.items[0]), (20, 0, 20, 100, f"Member 100% ({plan.plan_name})"))
		self.assertEqual(_line(inv.items[1]), (20, 20, 0, 0, None), "a taxed item is never free")
		self.assertEqual(flt(inv.total_taxes_and_charges), 1.2, "6% on the taxed line only")

	def test_week_days(self):
		item = _item("DAYS", 30)
		today = mb.DAYS[getdate(nowdate()).weekday()]
		other = mb.DAYS[(getdate(nowdate()).weekday() + 1) % 7]
		plan = _plan(_benefit(mb.ON_ITEM, item, mb.PERCENT, 50, days=other))
		_member(self.cust, plan)
		self.assertEqual(flt(_ticket(self.cust, (item, 1)).items[0].rate), 30)
		cust2 = gw._customer().name
		_member(cust2, _plan(_benefit(mb.ON_ITEM, item, mb.PERCENT, 50, days=today)))
		self.assertEqual(flt(_ticket(cust2, (item, 1)).items[0].rate), 15)

	def test_grace_period(self):
		item = _item("GRACE", 30)
		plan = _plan(_benefit(mb.ON_ITEM, item, mb.PERCENT, 50))
		_member(self.cust, plan, status="Past Due", grace_until=nowdate())
		self.assertEqual(flt(_ticket(self.cust, (item, 1)).items[0].rate), 15, "Past Due within grace")
		lapsed = gw._customer().name
		_member(lapsed, plan, status="Past Due", grace_until=add_days(nowdate(), -1))
		self.assertEqual(flt(_ticket(lapsed, (item, 1)).items[0].rate), 30, "grace over yesterday: not today")
		for status in ("Pending", "Suspended", "Expired", "Cancelled"):
			c = gw._customer().name
			_member(c, plan, status=status)
			self.assertEqual(flt(_ticket(c, (item, 1)).items[0].rate), 30, status)

	def test_a_rate_a_person_lowered_further_stays_without_the_benefit(self):
		item = _item("LOW", 30)
		_member(self.cust, _plan(_benefit(mb.ON_ITEM, item, mb.PERCENT, 20, uses=1)))
		inv = _ticket(self.cust, (item, 1, 20))
		self.assertEqual((flt(inv.items[0].rate), inv.items[0].get(BENEFIT_FIELD)), (20, None))
		self.assertEqual(flt(_ticket(self.cust, (item, 1)).items[0].rate), 24, "no use was taken")


class DailyUses(_Base):

	def setUp(self):
		super().setUp()
		self.cust = gw._customer().name
		self.item = _item("USE", 30)
		self.plan = _plan(_benefit(mb.ON_ITEM, self.item, mb.PERCENT, 50, uses=1))
		self.benefit = self.plan.benefits[0].name
		_member(self.cust, self.plan)

	def test_one_a_day_then_list_price_with_a_notice(self):
		first = _ticket(self.cust, (self.item, 1), submit=True)
		self.assertEqual(flt(first.items[0].rate), 15)
		frappe.message_log = []
		second = _ticket(self.cust, (self.item, 1))
		self.assertEqual(_line(second.items[0]), (30, 30, 0, 0, None))
		self.assertTrue(any("no uses left today" in str(m) for m in frappe.message_log), frappe.message_log)
		self.assertEqual(mb.benefit_usage(self.cust, self.benefit), {"limit": 1, "used": 1, "left": 0})

	def test_two_lines_in_one_ticket(self):
		inv = _ticket(self.cust, (self.item, 1), (self.item, 1))
		self.assertEqual([flt(r.rate) for r in inv.items], [15, 30])

	def test_qty_over_the_uses_left_is_not_given(self):
		plan = _plan(_benefit(mb.ON_ITEM, self.item, mb.PERCENT, 50, uses=2))
		cust = gw._customer().name
		_member(cust, plan)
		_ticket(cust, (self.item, 1))
		frappe.message_log = []
		inv = _ticket(cust, (self.item, 2))
		self.assertEqual(flt(inv.items[0].rate), 30)
		self.assertTrue(any("1 left today, this line has 2" in str(m) for m in frappe.message_log), frappe.message_log)

	def test_a_draft_holds_its_use(self):
		_ticket(self.cust, (self.item, 1))
		self.assertEqual(flt(_ticket(self.cust, (self.item, 1)).items[0].rate), 30)

	def test_a_full_return_gives_the_use_back_a_partial_one_does_not(self):
		from erpnext.accounts.doctype.pos_invoice.pos_invoice import make_sales_return

		plan = _plan(_benefit(mb.ON_ITEM, self.item, mb.PERCENT, 50, uses=2))
		cust = gw._customer().name
		_member(cust, plan)
		sale = _ticket(cust, (self.item, 2), submit=True)
		self.assertEqual(flt(sale.items[0].rate), 15)
		ret = self._return(make_sales_return(sale.name), qty=-1)
		self.assertEqual(_line(ret.items[0])[:2], (30, 15), "the return copies the line, not repriced")
		self.assertEqual(ret.items[0].get(BENEFIT_FIELD), plan.benefits[0].name)
		self.assertEqual(mb.benefit_usage(cust, plan.benefits[0].name)["used"], 2, "half returned: still used")
		self._return(make_sales_return(sale.name))
		self.assertEqual(mb.benefit_usage(cust, plan.benefits[0].name)["used"], 0, "all returned: given back")

	@staticmethod
	def _return(ret, qty=None):
		if qty is not None:
			ret.items[0].qty = qty
		ret.calculate_taxes_and_totals()
		ret.set("payments", [])
		ret.append("payments", {"mode_of_payment": "Cash", "amount": flt(ret.grand_total), "default": 1})
		ret.paid_amount = ret.base_paid_amount = flt(ret.grand_total)   # a POS Invoice's own header sum
		ret.flags.ignore_permissions = True
		ret.insert()
		ret.submit()
		return ret

	def test_consolidation_does_not_count_twice(self):
		from ffl_core.api.pos_consolidate import consolidate_pos_invoice_now

		sale = _ticket(self.cust, (self.item, 1), submit=True)
		out = consolidate_pos_invoice_now(sale.name)
		self.assertTrue(out["consolidated"], out)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.items[0].get(BENEFIT_FIELD), self.benefit, "the merge copies the record")
		self.assertEqual(mb.benefit_usage(self.cust, self.benefit)["used"], 1)
		# The register line keeps its record; the merged invoice books the net amount.
		pos = frappe.get_doc("POS Invoice", sale.name)
		self.assertEqual(_line(pos.items[0]), (30, 15, 15, 50, f"Member 50% ({self.plan.plan_name})"))
		self.assertEqual(flt(si.net_total), 15)
		income = frappe.db.sql("""select sum(credit) - sum(debit) from `tabGL Entry` where voucher_no = %s
			and account = %s and is_cancelled = 0""", (si.name, si.items[0].income_account))[0][0]
		self.assertEqual(flt(income), 15)

	def test_a_deleted_draft_gives_the_use_back(self):
		sale = _ticket(self.cust, (self.item, 1))
		sale.delete()
		self.assertEqual(flt(_ticket(self.cust, (self.item, 1)).items[0].rate), 15)


class SubmitAndApproval(_Base):

	def setUp(self):
		super().setUp()
		self.cust = gw._customer().name
		self.item = _item("SUB", 30)
		self.plan = _plan(_benefit(mb.ON_ITEM, self.item, mb.PERCENT, 20, uses=1))
		self.membership = _member(self.cust, self.plan)

	def test_submit_never_reprices_quietly(self):
		inv = _ticket(self.cust, (self.item, 1))
		self.assertEqual(flt(inv.items[0].rate), 24)
		frappe.db.set_value("Membership", self.membership, "status", "Expired")
		# The register's Complete Order: payment for the total it showed, submitted in one call.
		inv = frappe.get_doc("POS Invoice", inv.name)
		inv.set("payments", [])
		inv.append("payments", {"mode_of_payment": "Cash", "amount": flt(inv.grand_total), "default": 1})
		inv.flags.ignore_permissions = True
		with self.assertRaises(mb.MemberPriceChanged):
			inv.submit()
		self.assertEqual(flt(frappe.db.get_value("POS Invoice Item", inv.items[0].name, "rate")), 24)
		# Saved again as a draft it goes back to the list price, record gone.
		inv = frappe.get_doc("POS Invoice", inv.name)
		inv.save()
		self.assertEqual(_line(inv.items[0]), (30, 30, 0, 0, None))
		self.assertFalse(inv.items[0].get(BENEFIT_FIELD))

	def test_submit_of_a_line_the_screen_never_priced_is_refused(self):
		inv = frappe.get_doc({"doctype": "POS Invoice", "company": COMPANY, "pos_profile": gw.PROFILE,
			"customer": self.cust, "items": [{"item_code": self.item, "qty": 1, "rate": 30}]})
		inv.set_missing_values()
		inv.append("payments", {"mode_of_payment": "Cash", "amount": 31.8})
		inv.flags.ignore_permissions = True
		with self.assertRaisesRegex(mb.MemberPriceChanged, "member price changed"):
			inv.submit()

	def test_price_approval_floor(self):
		inv = _ticket(self.cust, (self.item, 1))
		stored = frappe.get_doc("POS Invoice", inv.name)
		self.assertEqual(price_approval.reductions(inv)["lines"], [], "the member price asks nothing")
		self.assertEqual(price_approval.reductions(inv, against_list=True)["lines"], [])
		inv.items[0].rate = 23
		self.assertEqual([(l["item_code"], l["rate"]) for l in price_approval.reductions(inv, stored)["lines"]],
			[(self.item, 23)], "under the member price asks")
		walk_in = gw._customer().name
		other = _ticket(walk_in, (self.item, 1, 24))
		self.assertEqual([l["rate"] for l in price_approval.reductions(other)["lines"]], [24],
			"a non-member at the member price asks")

	def test_a_screen_naming_another_member_is_ignored(self):
		guest = gw._customer().name
		_member(guest, _plan(_benefit(mb.ON_ITEM, self.item, mb.PERCENT, 50)))
		payer = gw._customer().name
		inv = frappe.get_doc({"doctype": "POS Invoice", "company": COMPANY, "pos_profile": gw.PROFILE,
			"customer": payer, "items": [{"item_code": self.item, "qty": 1, BENEFICIARY_FIELD: guest}]})
		inv.set_missing_values()
		inv.flags.ignore_permissions = True
		inv.insert()
		self.assertEqual((flt(inv.items[0].rate), inv.items[0].get(BENEFICIARY_FIELD)), (30, None))


class Exclusions(_Base):

	def setUp(self):
		super().setUp()
		self.cust = gw._customer().name

	def test_lines_never_priced(self):
		from ffl_core.setup.locked_lines import locked_items

		plan = _plan(_benefit(mb.ON_GROUP, "All Item Groups", mb.PERCENT, 10))
		_member(self.cust, plan)
		doc = frappe._dict(doctype="POS Invoice", name=None, customer=self.cust, company=COMPANY,
			selling_price_list=PRICE_LIST, posting_date=nowdate(), items=[])
		from ffl_core.setup import trade_in_credit

		trade_in_credit.ensure()
		locked = [c for c in locked_items() if frappe.db.exists("Item", c)]
		self.assertEqual(len(locked), 2, "the trade-in credit and deposit items exist")
		for code in locked + [plan.item]:
			res = mb.price_line(doc, frappe._dict(item_code=code, qty=1, uom="Nos", stock_uom="Nos",
				conversion_factor=1), self.cust)
			self.assertIsNone(res.benefit, code)
		deferred = _item("DEF", 10)
		frappe.db.set_value("Item", deferred, "enable_deferred_revenue", 1)
		self.assertIsNone(mb.price_line(doc, frappe._dict(item_code=deferred, qty=1), self.cust).benefit)
		plain = _item("PLAIN", 10)
		self.assertEqual(mb.price_line(doc, frappe._dict(item_code=plain, qty=1), self.cust).rate, 9)

	def test_consignment_line_is_not_priced(self):
		item = _item("CONS", 30)
		_member(self.cust, _plan(_benefit(mb.ON_ITEM, item, mb.PERCENT, 20)))
		inv = frappe.get_doc({"doctype": "POS Invoice", "company": COMPANY, "pos_profile": gw.PROFILE,
			"customer": self.cust, "items": [{"item_code": item, "qty": 1, "rate": 30,
				"consignment_commission_pct": 20}]})
		inv.set_missing_values()
		mb.reprice(inv)
		self.assertEqual((flt(inv.items[0].rate), inv.items[0].get(BENEFIT_FIELD)), (30, None))

	def test_documents_priced_elsewhere(self):
		from ffl_core.api.counter_pickup import LINK_FIELD
		from ffl_core.compliance.channel_invoice import CHANNEL_INVOICE_FIELDS

		pickup = frappe.get_doc({"doctype": "POS Invoice", LINK_FIELD: "SINV-X", "items": []})
		self.assertTrue(mb._skip(pickup))
		ret = frappe.get_doc({"doctype": "POS Invoice", "is_return": 1, "items": []})
		self.assertTrue(mb._skip(ret))
		merged = frappe.get_doc({"doctype": "Sales Invoice", "is_consolidated": 1, "items": []})
		self.assertTrue(mb._skip(merged))
		ordered = frappe.get_doc({"doctype": "Sales Invoice", "items": [{"item_code": "x", "so_detail": "abc"}]})
		self.assertTrue(mb._skip(ordered))
		for field in CHANNEL_INVOICE_FIELDS:
			if frappe.get_meta("Sales Invoice").has_field(field):
				web = frappe.get_doc({"doctype": "Sales Invoice", field: "1001", "items": []})
				self.assertTrue(mb._skip(web), field)
		self.assertFalse(mb._skip(frappe.get_doc({"doctype": "POS Invoice", "items": []})))
		self.assertFalse(mb._skip(frappe.get_doc({"doctype": "Sales Invoice", "items": []})))


class ExtensionSurface(_Base):

	def setUp(self):
		super().setUp()
		self.cust = gw._customer().name
		self.lane = _item("LANE", 40, group=SUB_GROUP, exempt=self.exempt)
		self.plan = _plan(_benefit(mb.ON_GROUP, GROUP, mb.PERCENT, 50, uses=2, label="Range"))
		self.benefit = self.plan.benefits[0].name
		self.membership = _member(self.cust, self.plan)

	def test_member_price(self):
		out = mb.member_price(self.cust, self.lane)
		self.assertEqual(out, {"list_rate": 40, "rate": 20, "benefit": self.benefit, "membership": self.membership,
			"reason": "Range", "uses_left": 2, "notice": None})
		self.assertEqual(mb.member_price(self.cust, self.lane, qty=3)["rate"], 40)
		self.assertIn("2 left today", mb.member_price(self.cust, self.lane, qty=3)["notice"])
		walk_in = gw._customer().name
		self.assertEqual(mb.member_price(walk_in, self.lane), {"list_rate": 40, "rate": 40, "benefit": None,
			"membership": None, "reason": None, "uses_left": None, "notice": None})

	def test_usage_and_badge(self):
		inv = _ticket(self.cust, (self.lane, 1))
		self.assertEqual(mb.benefit_usage(self.cust, self.benefit), {"limit": 2, "used": 1, "left": 1})
		self.assertEqual(mb.member_price(self.cust, self.lane, exclude_invoice=inv.name)["uses_left"], 2)
		badge = mb.membership_badge(self.cust)
		until = str(add_days(getdate(nowdate()), 334))[:7]
		self.assertEqual(badge["text"], f"{self.plan.plan_name} · to {until} · Range 1/2 today")
		self.assertEqual(badge["memberships"][0]["uses"][0]["used"], 1)
		self.assertIsNone(mb.membership_badge(gw._customer().name))
		from ffl_core.customer_flags import get_customer_flags

		self.assertEqual(get_customer_flags(self.cust)["membership"]["text"], badge["text"])

	def test_build_ticket_names_the_guest_open_ticket_cannot(self):
		payer = gw._customer().name
		lines = [{"item_code": self.lane, "qty": 1, "benefit_customer": self.cust}]
		out = pos_ticket.open_ticket(payer, frappe.as_json(lines), company=COMPANY)
		row = frappe.get_doc("POS Invoice", out["pos_invoice"]).items[0]
		self.assertEqual((flt(row.rate), row.get(BENEFICIARY_FIELD)), (40, None), "a request's member is ignored")
		out = pos_ticket.build_ticket(payer, lines, company=COMPANY)
		inv = frappe.get_doc("POS Invoice", out["pos_invoice"])
		row = inv.items[0]
		self.assertEqual((flt(row.rate), row.get(BENEFICIARY_FIELD), row.get(BENEFIT_FIELD)),
			(20, self.cust, self.benefit))
		self.assertEqual(mb.benefit_usage(self.cust, self.benefit)["used"], 1, "the guest's use, not the payer's")
		inv.save()   # the register's next save keeps the guest
		self.assertEqual(frappe.get_doc("POS Invoice", inv.name).items[0].get(BENEFICIARY_FIELD), self.cust)

	def test_preview_shows_what_the_save_will(self):
		doc = frappe.get_doc({"doctype": "POS Invoice", "company": COMPANY, "pos_profile": gw.PROFILE,
			"customer": self.cust, "items": [{"item_code": self.lane, "qty": 1, "rate": 40},
				{"item_code": self.lane, "qty": 2, "rate": 40}]})
		doc.set_missing_values()
		for i, row in enumerate(doc.items):
			row.name = f"new-row-{i}"
		out = mb.preview(frappe.as_json(doc.as_dict()))
		self.assertEqual(flt(out["rows"]["new-row-0"]["rate"]), 20)
		self.assertEqual(out["rows"]["new-row-0"][REASON_FIELD], "Range")
		self.assertEqual(flt(out["rows"]["new-row-1"]["rate"]), 40, "one use left, two units")
		self.assertTrue(any("1 left today, this line has 2" in n for n in out["notices"]), out)
		self.assertFalse(frappe.db.exists("POS Invoice Item", {"name": "new-row-0"}), "nothing stored")

	def test_caller_rate_under_the_member_price_wins_without_the_benefit(self):
		out = pos_ticket.build_ticket(self.cust, [{"item_code": self.lane, "rate": 18}], company=COMPANY)
		row = frappe.get_doc("POS Invoice", out["pos_invoice"]).items[0]
		self.assertEqual((flt(row.rate), row.get(BENEFIT_FIELD)), (18, None))
		out = pos_ticket.build_ticket(self.cust, [{"item_code": self.lane, "rate": 30}], company=COMPANY)
		self.assertEqual(flt(frappe.get_doc("POS Invoice", out["pos_invoice"]).items[0].rate), 20,
			"a member price under the caller's rate replaces it")


class OpenTicketApproval(SettingsSandbox, _Base):
	"""open_ticket's typed rates under price approval, by a clerk: the customer's member price, worked
	out on the server, asks nothing; a typed rate under it, or another person's member price, asks.
	build_ticket's member prices are our own pricing."""

	def setUp(self):
		super().setUp()
		self.set_single("FFL Settings", price_approval.SETTING, 1)
		tpa._staff()
		self.cust = gw._customer().name
		self.payer = gw._customer().name
		self.lane = _item("LANE", 40, group=SUB_GROUP, exempt=self.exempt)
		plan = _plan(_benefit(mb.ON_GROUP, GROUP, mb.PERCENT, 50, label="Range"))
		self.benefit = plan.benefits[0].name
		_member(self.cust, plan)

	def _clerk(self, customer, lines, fn=None):
		frappe.set_user(tpa.CLERK)
		self.addCleanup(frappe.set_user, "Administrator")
		fn = fn or (lambda c, l, **kw: pos_ticket.open_ticket(c, frappe.as_json(l), **kw))
		out = fn(customer, lines, company=COMPANY)
		return frappe.get_doc("POS Invoice", out["pos_invoice"])

	def _priced(self, inv) -> tuple:
		row = inv.items[0]
		return flt(row.rate), row.get(BENEFIT_FIELD), row.get(BENEFICIARY_FIELD), inv.get(price_approval.BY)

	def test_a_rate_under_the_member_price_is_asked(self):
		with self.assertRaisesRegex(price_approval.PriceApprovalRequired, "Ask a manager"):
			self._clerk(self.cust, [{"item_code": self.lane, "rate": 19}])

	def test_the_member_price_the_server_gives_is_not_asked(self):
		self.assertEqual(self._priced(self._clerk(self.cust, [{"item_code": self.lane}])),
			(20, self.benefit, self.cust, None))
		self.assertEqual(self._priced(self._clerk(self.cust, [{"item_code": self.lane, "rate": 30}])),
			(20, self.benefit, self.cust, None), "a member price under the typed rate replaces it")
		self.assertEqual(self._priced(self._clerk(self.cust, [{"item_code": self.lane, "rate": 20}]))[0::3],
			(20, None), "typed at the member price this customer gets: not a lowered price")

	def test_another_persons_member_price_is_asked(self):
		with self.assertRaisesRegex(price_approval.PriceApprovalRequired, "Ask a manager"):
			self._clerk(self.payer, [{"item_code": self.lane, "rate": 20, "benefit_customer": self.cust}])

	def test_build_ticket_prices_the_guest_unasked_and_the_register_save_keeps_it(self):
		inv = self._clerk(self.payer, [{"item_code": self.lane, "benefit_customer": self.cust}],
			fn=pos_ticket.build_ticket)
		self.assertEqual(self._priced(inv), (20, self.benefit, self.cust, None))
		tpa._request(self)   # the register's own save, by the clerk
		inv = frappe.get_doc("POS Invoice", inv.name)
		inv.save()
		self.assertEqual(self._priced(frappe.get_doc("POS Invoice", inv.name)), (20, self.benefit, self.cust, None))


SALES_RATE, EXCISE_RATE = 7.75, 11
TAX_CO, TAX_ABBR = "_Test MB Excise Co", "TMBX"


class CaliforniaTax(FrappeTestCase):
	"""Ammo at 30 to a member at 24 (CA 7.75% sales tax + 11% excise): both taxes on 24 — 1.86 and 2.64."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		from ffl_core.setup import member_benefits, test_bootstrap

		test_bootstrap.run()
		member_benefits.ensure()
		if not frappe.db.exists("Company", TAX_CO):
			frappe.get_doc({"doctype": "Company", "company_name": TAX_CO, "abbr": TAX_ABBR, "default_currency": "USD",
				"country": "United States"}).insert(ignore_permissions=True)

		def account(name):
			full = f"{name} - {TAX_ABBR}"
			if not frappe.db.exists("Account", full):
				frappe.get_doc({"doctype": "Account", "account_name": name, "company": TAX_CO, "is_group": 0,
					"parent_account": f"Duties and Taxes - {TAX_ABBR}", "account_type": "Tax"}).insert(ignore_permissions=True)
			return full

		cls.sales_tax, cls.excise = account("Sales Tax Payable"), account("CA Firearm Excise Tax Payable")
		cls.template = frappe.db.get_value("Sales Taxes and Charges Template", {"company": TAX_CO, "title": "_Test MB CA"},
			"name") \
			or frappe.get_doc({"doctype": "Sales Taxes and Charges Template", "title": "_Test MB CA", "company": TAX_CO,
				"is_default": 1, "taxes": [
					{"charge_type": "On Net Total", "account_head": cls.sales_tax, "rate": SALES_RATE, "description": "Tax"},
					{"charge_type": "On Net Total", "account_head": cls.excise, "rate": 0, "description": "Excise"}]}
			).insert(ignore_permissions=True).name
		goods = frappe.db.get_value("Item Tax Template", {"company": TAX_CO, "title": "_Test MB CA Goods"}, "name") \
			or frappe.get_doc({"doctype": "Item Tax Template", "title": "_Test MB CA Goods", "company": TAX_CO,
				"taxes": [{"tax_type": cls.sales_tax, "tax_rate": SALES_RATE},
					{"tax_type": cls.excise, "tax_rate": EXCISE_RATE}]}).insert(ignore_permissions=True).name
		cls.ammo = _item("CAAMMO", 30, group="Parts", exempt=goods)   # the goods template: 7.75% + 11%
		cls.cust = frappe.get_doc({"doctype": "Customer", "customer_name": _uniq("CA Member"), "customer_group": "Individual",
			"territory": "All Territories"}).insert(ignore_permissions=True).name
		_member(cls.cust, _plan(_benefit(mb.ON_ITEM, cls.ammo, mb.PERCENT, 20)))

	def test_taxes_on_the_member_price(self):
		from erpnext.controllers.accounts_controller import get_taxes_and_charges

		si = frappe.get_doc({"doctype": "Sales Invoice", "company": TAX_CO, "customer": self.cust,
			"posting_date": nowdate(), "set_posting_time": 1, "taxes_and_charges": self.template,
			"items": [{"item_code": self.ammo, "qty": 1, "rate": 30}]})
		si.set("taxes", get_taxes_and_charges("Sales Taxes and Charges Template", self.template))
		si.insert(ignore_permissions=True)
		self.assertEqual(_line(si.items[0])[:3], (30, 24, 6))
		by_account = {t.account_head: flt(t.tax_amount, 2) for t in si.taxes}
		self.assertEqual((by_account[self.sales_tax], by_account[self.excise]), (1.86, 2.64))
		self.assertTrue(mb.taxed_here(self.ammo, TAX_CO))


class TwoTicketsAtOnce(FrappeTestCase):
	"""Two sessions save a sale for the same member at the same moment, one use a day left: one gets
	it. Commits on purpose (a second connection sees only committed rows) and deletes what it made."""

	def test_the_last_use_goes_to_one_ticket(self):
		frappe.set_user("Administrator")
		from ffl_core.setup import member_benefits

		member_benefits.ensure()
		made = []
		cust = frappe.get_doc({"doctype": "Customer", "customer_name": _uniq("Race Member"), "customer_group": "Individual",
			"territory": "All Territories"}).insert(ignore_permissions=True).name
		made.append(("Customer", cust))
		item = _item("RACE", 30, group="Parts")
		made.append(("Item", item))
		plan = _plan(_benefit(mb.ON_ITEM, item, mb.PERCENT, 50, uses=1))
		made += [("Item", plan.item), ("Membership Plan", plan.name)]
		made.append(("Membership", _member(cust, plan)))
		frappe.db.commit()   # the other sessions must see the member (deleted in the finally below)
		site, results = frappe.local.site, []
		barrier = threading.Barrier(2)
		real_used = mb.used_on

		def slow_used(*args, **kwargs):
			n = real_used(*args, **kwargs)
			import time
			time.sleep(1.5)   # both sessions inside the count at once, unless the lock serializes them
			return n

		def sell():
			frappe.init(site=site, sites_path=frappe.local.sites_path if hasattr(frappe.local, "sites_path") else ".")
			frappe.connect()
			try:
				frappe.set_user("Administrator")
				barrier.wait(timeout=30)
				si = frappe.get_doc({"doctype": "Sales Invoice", "company": COMPANY, "customer": cust,
					"items": [{"item_code": item, "qty": 1, "rate": 30}]})
				# Named here: two sessions drawing from one naming series at once collide on its row
				# (MariaDB 1020) before they ever reach the member price.
				si.name, si.flags.name_set = f"_TEST-RACE-{frappe.generate_hash(length=8)}", True
				si.set_missing_values()
				si.insert(ignore_permissions=True)
				frappe.db.commit()
				results.append((si.name, flt(si.items[0].rate)))
			except Exception as e:
				results.append(("error", type(e).__name__ + ": " + str(e)))
				frappe.db.rollback()
			finally:
				frappe.destroy()

		try:
			with patch.object(mb, "used_on", side_effect=slow_used):
				threads = [threading.Thread(target=sell) for _ in range(2)]
				for t in threads:
					t.start()
				for t in threads:
					t.join(timeout=120)
			self.assertEqual(len(results), 2, results)
			# One session has the use. The other waited on the member's lock and then either saw the use
			# taken (list price) or, its snapshot older than that sale, was refused to save again.
			self.assertEqual([r[1] for r in results].count(15), 1, results)
			other = next(r for r in results if r[1] != 15)
			self.assertTrue(other[1] == 30 or "MemberUsesBusy" in str(other[1]), results)
			made += [("Sales Invoice", r[0]) for r in results if r[0] != "error"]
		finally:
			frappe.db.rollback()
			for doctype, name in reversed(made):
				if frappe.db.exists(doctype, name):
					frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
			frappe.db.commit()
