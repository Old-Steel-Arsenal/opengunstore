"""Test support for guns held for a CRM deal (ffl_core.crm.holds), shared by the core, website and
GunBroker tests: a gun priced one by one in stock, a deal with it on a line, shared with the team and
linked to a customer, the staff, a manager with a counter PIN, and a Desk-shaped Sales Invoice."""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.utils.password import update_password

from ffl_core import staff_switch
from ffl_core.api import crm as api
from ffl_core.crm import holds
from ffl_core.crm.item import upsert_product
from ffl_core.tests.crm_support import CLERK, MANAGER, CRMTestCase, user
from ffl_core.tests.test_build_order import DEST_FFL, BuildOrderMixin

SETTINGS = "Lead Mail Settings"
GUN = "BO-GUN"
ACC = "_T-DH-ACC"
OWNER = "dh-deal-owner@example.com"
OTHER = "dh-other-clerk@example.com"
PIN = "1357"
PASSWORD = "Dh-Manager-Pass-1!"


def _token() -> str:
	return frappe.generate_hash(length=8).upper()


def _switches(link: int = 1, hold: int = 1) -> None:
	frappe.db.set_single_value(SETTINGS, {"crm_pos_link": link, "crm_deal_holds": hold})


def _customer(name: str | None = None) -> str:
	return frappe.get_doc({"doctype": "Customer", "customer_name": name or "DH Buyer " + _token(),
		"customer_group": "Individual", "territory": "All Territories"}).insert(ignore_permissions=True).name


def _contact() -> str:
	doc = frappe.new_doc("Contact")
	doc.first_name = "DH " + _token()
	doc.append("email_ids", {"email_id": f"dh-{_token().lower()}@example.com", "is_primary": 1})
	return doc.insert(ignore_permissions=True).name


def _comments(deal: str) -> list[str]:
	return frappe.get_all("Comment", filters={"reference_doctype": "CRM Deal", "reference_name": deal,
		"comment_type": "Comment"}, pluck="content", order_by="creation asc")


def _tasks(deal: str) -> list:
	return frappe.get_all("CRM Task", filters={"reference_doctype": "CRM Deal", "reference_docname": deal},
		fields=["name", "title", "assigned_to", "priority"])


def _active(serial: str) -> list:
	return frappe.get_all(holds.HOLD, filters={"serial_no": serial, "status": holds.ACTIVE}, pluck="name")


class DealHoldCase(BuildOrderMixin, CRMTestCase):
	"""Fixtures only (no tests): a gun priced one by one, a deal with it on a line and a customer."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		user(OWNER, "Sales User")
		user(OTHER, "Sales User")
		user(MANAGER, "Sales Manager", "Sales User")
		update_password(MANAGER, PASSWORD)
		update_password(MANAGER, PIN, fieldname=staff_switch.PIN_FIELD)
		cls._item(ACC, firearm=False)
		cls.product = upsert_product(frappe.get_doc("Item", GUN))
		cls.pos = frappe.db.get_value("POS Profile", {"company": cls.company, "disabled": 0}, "name")

	def setUp(self):
		super().setUp()
		from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError

		p = patch.object(FastBoundClient, "from_settings", side_effect=FastBoundConfigError("off in these tests"))
		p.start()
		self.addCleanup(p.stop)
		frappe.cache.delete(staff_switch._pin_fail_key(MANAGER))
		frappe.cache.delete(frappe.cache.make_key(f"ffl-unlock-fail:{MANAGER}"))
		_switches()

	def _gun(self, price: float = 700) -> str:
		serial = self._stocked_serial("DH" + _token(), item_code=GUN)
		frappe.db.set_value("Serial No", serial, "sell_price", price, update_modified=False)
		return serial

	def _deal(self, *serials, customer: str | None | bool = True, owner: str | None = OWNER):
		doc = frappe.new_doc("CRM Deal")
		doc.status = "New"
		doc.deal_owner = owner
		doc.append("contacts", {"contact": _contact(), "is_primary": 1})
		for serial in serials:
			doc.append("products", {"product_code": self.product, "qty": 1, "ffl_serial_no": serial})
		doc.insert(ignore_permissions=True)
		# Every staff user reads and works the deal, as team mail leaves it (crm.mail.share_with_team).
		from ffl_core.crm.mail import share_with_team

		share_with_team("CRM Deal", doc.name)
		if customer:
			frappe.db.set_value("CRM Deal", doc.name, "erpnext_customer",
				_customer() if customer is True else customer)
		return doc.name

	def _customer_of(self, deal: str) -> str:
		return frappe.db.get_value("CRM Deal", deal, "erpnext_customer")

	def _hold(self, deal: str, *serials, as_user: str = CLERK, **kwargs) -> dict:
		frappe.set_user(as_user)
		try:
			return api.hold_guns(deal, list(serials), **kwargs)
		finally:
			frappe.set_user("Administrator")

	def _held(self, deal: str, serial: str, as_user: str = CLERK) -> str:
		out = self._hold(deal, serial, as_user=as_user)
		self.assertEqual(out["refused"], [], out)
		return out["held"][0]["name"]

	def _invoice(self, serial: str, customer: str, *, submit: bool = False, deal: str | None = None):
		"""A Desk-shaped Sales Invoice selling *serial* to *customer* (an FFL shipment, update_stock 0)."""
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = customer
		si.ffl_destination_ffl = DEST_FFL
		si.ffl_buyer_type = "Individual"
		si.update_stock = 0
		if deal:
			si.ffl_crm_deal = deal
		si.append("items", {"item_code": GUN, "qty": 1, "rate": 700, "warehouse": self.warehouse,
			"serial_no": serial, "use_serial_batch_fields": 1})
		si.set_missing_values()
		si.flags.ignore_permissions = True
		si.insert()
		if submit:
			si.submit()
		return si
