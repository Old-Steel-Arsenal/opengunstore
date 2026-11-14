"""A CRM Deal carries the person's name as a converted deal does (crm.deals.person_name / name_deal:
the Contact, else its Customer, else the organization, else the email), that name is the deal's title,
first column and quick filter (crm.setup), and the one-shot patch names the deals made before. Real
documents, real setup, real patch."""
from __future__ import annotations

import json

import frappe

from ffl_core.api import crm as api
from ffl_core.crm import setup
from ffl_core.crm.deals import open_deal_for
from ffl_core.patches import name_crm_deals
from ffl_core.tests.crm_support import CRMTestCase

NAMES = ["first_name", "last_name", "lead_name"]


def _contact(email: str, first: str, last: str | None = None, customer: str | None = None) -> str:
	return frappe.get_doc({"doctype": "Contact", "first_name": first, "last_name": last,
		"email_ids": [{"email_id": email, "is_primary": 1}],
		"links": [{"link_doctype": "Customer", "link_name": customer}] if customer else []}).insert(ignore_permissions=True).name


def _customer(name: str) -> str:
	return frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_type": "Individual"}
		).insert(ignore_permissions=True).name


def _names(deal: str) -> tuple:
	return frappe.db.get_value("CRM Deal", deal, NAMES)


def _organization(name: str) -> str:
	return frappe.get_doc({"doctype": "CRM Organization", "organization_name": name}).insert(ignore_permissions=True).name


def _view(**fields) -> str:
	doc = frappe.get_doc({"doctype": "CRM View Settings", "dt": "CRM Deal", "type": "list", "route_name": "Deals",
		"filters": "{}", **fields}).insert(ignore_permissions=True)
	return doc.name


def _keys(view: str) -> list[str]:
	return [c["key"] for c in json.loads(frappe.db.get_value("CRM View Settings", view, "columns") or "[]")]


def _blank(deal: str, **keep) -> None:
	frappe.db.set_value("CRM Deal", deal, {**dict.fromkeys(NAMES, ""), **keep}, update_modified=False)


class TestDealNames(CRMTestCase):
	def test_the_contacts_name(self):
		deal = open_deal_for(_contact("names-a@buyer.test", "Dana", "Reyes"))
		self.assertEqual(_names(deal), ("Dana", "Reyes", "Dana Reyes"))

	def test_an_address_for_a_first_name_falls_back_to_the_customer(self):
		customer = _customer("_T Lee Morgan Smith")
		deal = open_deal_for(_contact("names-b@buyer.test", "names-b@buyer.test", customer=customer))
		self.assertEqual(_names(deal), ("_T", "Lee Morgan Smith", "_T Lee Morgan Smith"))

	def test_no_name_and_no_customer_takes_the_email(self):
		deal = open_deal_for(_contact("names.c@buyer.test", "names.c@buyer.test"))
		self.assertEqual(_names(deal), ("names.c", "", "names.c"))

	def test_the_deals_list_finds_it_by_the_persons_name(self):
		deal = open_deal_for(_contact("names-d@buyer.test", "Quinn", "Halvorsen"))
		# What a CRM list filter on the Full Name column asks (Data: like %text%).
		self.assertIn(deal, frappe.get_list("CRM Deal", filters={"lead_name": ["like", "%halvorsen%"]}, pluck="name"))

	def test_a_caller_may_title_the_deal(self):
		deal = open_deal_for(_contact("names-t@buyer.test", "Tess", "Ward"), title="Sell: Make Model")
		self.assertEqual(_names(deal), ("Tess", "Ward", "Sell: Make Model"))

	def test_a_deal_made_in_crm_is_named_on_save(self):
		contact = _contact("names-u@buyer.test", "Uma", "Stone")
		deal = frappe.get_doc({"doctype": "CRM Deal", "contacts": [{"contact": contact, "is_primary": 1}]}
			).insert(ignore_permissions=True)
		self.assertEqual(_names(deal.name), ("Uma", "Stone", "Uma Stone"))
		org = frappe.get_doc({"doctype": "CRM Deal", "organization": _organization("_T Names Range Co")}
			).insert(ignore_permissions=True)
		self.assertEqual(_names(org.name), ("", "", "_T Names Range Co"), "an organization keeps its title")
		org.lead_name = "Picked By Hand"
		org.save(ignore_permissions=True)
		self.assertEqual(_names(org.name)[2], "Picked By Hand")

	def test_the_title_and_quick_filter_are_set_on_install_and_migrate(self):
		self.addCleanup(frappe.clear_cache, doctype="CRM Deal")
		frappe.db.delete("Property Setter", {"doc_type": "CRM Deal"})
		frappe.clear_cache(doctype="CRM Deal")
		self.assertEqual(frappe.get_meta("CRM Deal").title_field, "organization", "CRM's own title")
		setup.ensure(backfill=False)   # after_install and after_migrate
		meta = frappe.get_meta("CRM Deal")
		self.assertEqual(meta.title_field, "lead_name")
		self.assertEqual(meta.get_field("lead_name").label, "Full Name")
		self.assertEqual(setup.ensure_deal_properties(), [], "a second run writes nothing")

	def test_the_name_is_the_first_quick_filter_once(self):
		from crm.api.doc import get_quick_filters

		self.addCleanup(frappe.clear_cache, doctype="CRM Deal")
		quick = lambda: [f["fieldname"] for f in get_quick_filters("CRM Deal", cached=False)]  # noqa: E731
		settings = frappe.db.exists("CRM Global Settings", {"dt": "CRM Deal", "type": "Quick Filters"})
		self.assertTrue(settings, "CRM saves the quick filters on its install")
		frappe.db.set_value("CRM Global Settings", settings, "json", json.dumps(["organization", "status"]))
		frappe.db.set_global(setup.DEAL_FILTER_FLAG, "")
		self.assertTrue(setup.ensure_deal_name_filter())
		self.assertEqual(quick(), ["lead_name", "organization", "status"])
		frappe.clear_cache(doctype="CRM Deal")
		self.assertEqual(frappe.get_meta("CRM Deal").get_field("lead_name").in_standard_filter, 1)
		frappe.db.set_value("CRM Global Settings", settings, "json", json.dumps(["status"]))
		self.assertFalse(setup.ensure_deal_name_filter(), "once per site: a manager may take it off")
		self.assertEqual(quick(), ["status"])

	def test_the_deals_list_shows_the_name_first_and_filters_on_it(self):
		deal = open_deal_for(_contact("names-l@buyer.test", "Lane", "Okafor"))
		frappe.db.delete("CRM View Settings", {"dt": "CRM Deal", "is_standard": 1, "user": "Administrator"})
		# What the Deals page asks first (no columns yet), with the quick filter typed (Data: like).
		data = api.crm_list_data(doctype="CRM Deal", filters={"lead_name": ["LIKE", "%okafor%"]},
			order_by="modified desc", view={"custom_view_name": "", "view_type": "list"}, columns="", rows="")
		keys = [c["key"] for c in data["columns"]]
		self.assertEqual(keys[0], "lead_name")
		self.assertIn("organization", keys[1:])
		self.assertEqual([(r["name"], r["lead_name"]) for r in data["data"]], [(deal, "Lane Okafor")])
		self.assertTrue(data["is_default"])

	def test_saved_deal_views_on_crm_columns_get_the_name_once(self):
		from crm.fcrm.doctype.crm_deal.crm_deal import CRMDeal

		stock = CRMDeal.default_list_data()
		mine = _view(is_standard=1, user="Administrator", columns=json.dumps(stock["columns"]), rows=json.dumps(stock["rows"]))
		chosen = _view(is_standard=1, user="Guest", columns=json.dumps(stock["columns"][:2]), rows="[]")
		made = _view(label="_T My deals", public=1, columns=json.dumps(stock["columns"]), rows=json.dumps(stock["rows"]))
		shared = _view(label=setup.FOLLOW_UPS, public=1, columns="[]", rows="[]", load_default_columns=1)
		frappe.db.set_global(setup.DEAL_COLUMNS_FLAG, "")
		changed = setup.ensure_deal_name_columns()
		self.assertEqual(set(changed) & {mine, chosen, made, shared}, {mine, shared})
		for view in (mine, shared):
			self.assertEqual(_keys(view)[0], "lead_name")
			self.assertIn("lead_name", json.loads(frappe.db.get_value("CRM View Settings", view, "rows")))
		self.assertEqual(_keys(chosen), [c["key"] for c in stock["columns"][:2]])
		self.assertEqual(_keys(made), [c["key"] for c in stock["columns"]], "a view a user made is theirs")
		frappe.db.set_value("CRM View Settings", mine, "columns", json.dumps(stock["columns"]))
		self.assertEqual(setup.ensure_deal_name_columns(), [], "once per site")

	def test_the_patch_names_blank_deals_keeps_named_ones_and_is_idempotent(self):
		customer = _customer("_T Robin Vale")
		by_contact = open_deal_for(_contact("names-e@buyer.test", "Ellis", "Park"))
		by_customer = open_deal_for(_contact("names-f@buyer.test", "names-f@buyer.test", customer=customer))
		by_email = open_deal_for(_contact("names.g@buyer.test", "names.g@buyer.test"))
		half = open_deal_for(_contact("names-h@buyer.test", "Ignored", "Name"))
		named = open_deal_for(_contact("names-i@buyer.test", "Contact", "Name"))
		by_org = frappe.get_doc({"doctype": "CRM Deal", "organization": _organization("_T Names Club")}
			).insert(ignore_permissions=True).name
		for deal in (by_contact, by_customer, by_email, by_org):
			_blank(deal)
		_blank(half, first_name="Kept", last_name="Surname")
		frappe.db.set_value("CRM Deal", named, dict(zip(NAMES, ("Set", "By Hand", "Set By Hand"))), update_modified=False)
		modified = {d: frappe.db.get_value("CRM Deal", d, "modified") for d in (by_contact, by_customer, by_email, half)}

		name_crm_deals.execute()
		self.assertEqual(_names(by_contact), ("Ellis", "Park", "Ellis Park"))
		self.assertEqual(_names(by_customer), ("_T", "Robin Vale", "_T Robin Vale"))
		self.assertEqual(_names(by_email), ("names.g", "", "names.g"))
		self.assertEqual(_names(half), ("Kept", "Surname", "Kept Surname"))
		self.assertEqual(_names(named), ("Set", "By Hand", "Set By Hand"))
		self.assertEqual(_names(by_org), ("", "", "_T Names Club"))
		for deal, when in modified.items():
			self.assertEqual(frappe.db.get_value("CRM Deal", deal, "modified"), when)

		frappe.db.set_value("Contact", frappe.db.get_value("CRM Contacts", {"parent": by_contact}, "contact"),
			"first_name", "Changed")
		name_crm_deals.execute()
		self.assertEqual(_names(by_contact), ("Ellis", "Park", "Ellis Park"), "a named deal is left as it is")
