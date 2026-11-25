"""The Matches card of the Lead and Deal pages (``crm.matches``).

Real documents on the test site, no stand-ins. Every test runs inside the savepoint CRMTestCase rolls back."""
from __future__ import annotations

import json

import frappe

from ffl_core.api import crm as api
from ffl_core.crm import matches
from ffl_core.crm.deal import add_product_row
from ffl_core.tests import test_crm_memory as M
from ffl_core.tests.crm_support import CLERK, make_item, set_item_price, user
from ffl_core.tests.test_crm_deal_pos import _token
from ffl_core.tests.test_crm_reply_draft import _gun

SETTINGS = "Lead Mail Settings"
WORD = "zqm4"   # a word no other item on the test site has


def _link(on: int = 1) -> None:
	frappe.db.set_single_value(SETTINGS, "crm_pos_link", on)


def _item(name: str, *prices: float) -> tuple[str, list[str]]:
	"""A serialized item named *name* with one gun in stock per price."""
	code = f"_T-MT-{_token()}"
	make_item(code, item_name=name, is_stock_item=1, has_serial_no=1, is_firearm=1)
	set_item_price(code, max(prices or (500,)))
	return code, [_gun(code, f"MT-{_token()}", price) for price in prices]


def _wants(doc, keywords: str, budget: float | None = None) -> None:
	rows = [{"kind": "want", "text": keywords, "keywords": keywords}]
	if budget:
		rows.append({"kind": "budget", "text": str(budget), "amount": budget})
	M._staff_memory(doc, *rows)


class TestMatches(M.MemoryCase):
	def setUp(self):
		super().setUp()
		_link()

	def _card(self, doc) -> dict:
		return matches.card(frappe.get_doc(doc.doctype, doc.name))

	def test_the_gun_asked_about_shows_with_its_price_and_stock(self):
		code, (gun,) = _item(f"{WORD} carbine", 640)
		lead = M._lead()
		add_product_row(lead.doctype, lead.name, code, gun)
		card = self._card(lead)
		self.assertTrue(card["shown"])
		self.assertIn(f"{WORD} carbine", card["html"])
		self.assertIn("640", card["html"])
		self.assertIn("1 in stock", card["html"])
		self.assertEqual(card["guns"], [])   # a lead holds nothing

	def test_what_the_customer_said_they_want_is_matched_in_stock(self):
		code, guns = _item(f"{WORD} rifle", 700, 550)
		_item("something else", 300)
		deal = M._deal()
		_wants(deal, f"{WORD} rifle")
		found = matches.rows(frappe.get_doc(deal.doctype, deal.name))
		self.assertEqual([r["item"] for r in found], [code])
		self.assertEqual(found[0]["price"], 550)   # the cheapest gun
		self.assertEqual(found[0]["available"], 2)
		card = self._card(deal)
		self.assertEqual([g["serial"] for g in card["guns"]], [guns[1], guns[0]])   # cheapest first
		self.assertIn(guns[1], card["guns"][0]["label"])

	def test_a_gun_already_on_the_deal_is_not_offered_again(self):
		code, guns = _item(f"{WORD} pistol", 400, 450)
		deal = M._deal()
		add_product_row(deal.doctype, deal.name, code, guns[0])
		self.assertEqual([g["serial"] for g in self._card(deal)["guns"]], [guns[1]])

	def test_a_counted_item_with_none_left_says_so(self):
		code = f"_T-MT-{_token()}"
		make_item(code, item_name=f"{WORD} sling", is_stock_item=1)
		set_item_price(code, 25)
		deal = M._deal()
		add_product_row(deal.doctype, deal.name, code)
		self.assertIn("Out of stock", self._card(deal)["html"])

	def test_a_gun_priced_one_by_one_that_sold_is_not_listed(self):
		# No gun left means no price to show (``reply_draft.fact``).
		code, (gun,) = _item(f"{WORD} shotgun", 900)
		deal = M._deal()
		add_product_row(deal.doctype, deal.name, code)
		frappe.db.set_value("Serial No", gun, "status", "Delivered", update_modified=False)
		self.assertFalse(self._card(deal)["shown"])

	def test_a_line_that_names_a_gun_shows_that_gun(self):
		code, (asked, cheaper) = _item(f"{WORD} mauser", 900, 550)
		deal = M._deal()
		add_product_row(deal.doctype, deal.name, code, asked)
		found = matches.rows(frappe.get_doc(deal.doctype, deal.name))
		self.assertEqual([(r["name"], r["price"], r["available"]) for r in found],
			[(f"{WORD} mauser · {asked}", 900, 1), (f"{WORD} mauser", 550, 1)])
		self.assertEqual([g["serial"] for g in self._card(deal)["guns"]], [cheaper])
		# Sold: the gun asked about says so, whatever else the item has.
		frappe.db.set_value("Serial No", asked, "status", "Delivered", update_modified=False)
		found = matches.rows(frappe.get_doc(deal.doctype, deal.name))
		self.assertEqual((found[0]["available"], found[0]["price"]), (0, 0))
		self.assertIn("Out of stock", self._card(deal)["html"])
		# The last one gone too: the gun asked about still shows.
		frappe.db.set_value("Serial No", cheaper, "status", "Delivered", update_modified=False)
		self.assertEqual([r["name"] for r in matches.rows(frappe.get_doc(deal.doctype, deal.name))],
			[f"{WORD} mauser · {asked}"])

	def test_a_model_sold_at_one_price_offers_no_gun_to_hold(self):
		code, _guns = _item(f"{WORD} glock", 500, 500)
		frappe.db.set_value("Item", code, "uniform_sell_price", 1)
		frappe.clear_document_cache("Item", code)
		deal = M._deal()
		_wants(deal, f"{WORD} glock")
		card = self._card(deal)
		self.assertTrue(card["shown"])
		self.assertEqual(card["guns"], [])

	def test_not_shown_on_a_closed_deal_or_a_sell_request(self):
		from ffl_core.crm.sell_requests import SELL

		code, _guns = _item(f"{WORD} rifle", 700)
		deal = M._deal()
		add_product_row(deal.doctype, deal.name, code)
		self.assertTrue(self._card(deal)["shown"])
		frappe.db.set_value(deal.doctype, deal.name, SELL, 1, update_modified=False)
		self.assertFalse(self._card(deal)["shown"])
		frappe.db.set_value(deal.doctype, deal.name, {SELL: 0, "status": "Lost"}, update_modified=False)
		self.assertFalse(self._card(deal)["shown"])

	def test_nothing_to_show_hides_the_card(self):
		_item(f"{WORD} rifle", 700)
		self.assertEqual(self._card(M._deal()), {"shown": False, "html": "", "guns": []})

	def test_not_shown_with_the_pos_link_off_or_on_other_mail(self):
		code, _guns = _item(f"{WORD} rifle", 700)
		lead = M._lead()
		add_product_row(lead.doctype, lead.name, code)
		self.assertTrue(self._card(lead)["shown"])
		frappe.db.set_value(lead.doctype, lead.name, "status", "Other mail", update_modified=False)
		self.assertFalse(self._card(lead)["shown"])
		deal = M._deal()
		add_product_row(deal.doctype, deal.name, code)
		_link(0)
		self.assertFalse(self._card(deal)["shown"])

	def test_the_name_is_escaped(self):
		code, _guns = _item(f"{WORD} <b>x</b>", 700)
		lead = M._lead()
		add_product_row(lead.doctype, lead.name, code)
		self.assertNotIn("<b>", self._card(lead)["html"])

	def test_the_endpoint_checks_the_record(self):
		code, _guns = _item(f"{WORD} rifle", 700)
		lead = M._lead(lead_owner="Administrator")
		add_product_row(lead.doctype, lead.name, code)
		user(CLERK, "Sales User")
		frappe.set_user(CLERK)
		try:
			self.assertRaises(frappe.PermissionError, api.matches_card, lead.doctype, lead.name)
		finally:
			frappe.set_user("Administrator")
		self.assertTrue(api.matches_card(lead.doctype, lead.name)["shown"])

	def test_the_section_is_placed_in_both_side_panels_and_removed(self):
		def has() -> list[bool]:
			return [any(s.get("name") == matches.SECTION for s in json.loads(
				frappe.db.get_value("CRM Fields Layout", f"{dt}-Side Panel", "layout") or "[]")) for dt in matches.DOCTYPES]
		matches.place(False)
		self.assertEqual(has(), [False, False])
		self.assertTrue(matches.place(True))
		self.assertEqual(has(), [True, True])
		self.assertFalse(matches.place(True))
		# A section a manager took out stays out until the link is switched off and on.
		layout = frappe.get_doc("CRM Fields Layout", f"{matches.DOCTYPES[0]}-Side Panel")
		layout.layout = json.dumps([s for s in json.loads(layout.layout) if s.get("name") != matches.SECTION])
		layout.save(ignore_permissions=True)
		self.assertFalse(matches.place(True))
		self.assertEqual(has(), [False, True])
		matches.place(False)
		self.assertEqual(has(), [False, False])
		matches.place(True)
		self.assertEqual(has(), [True, True])
