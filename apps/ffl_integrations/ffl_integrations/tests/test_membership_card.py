"""Memberships and card on file: an auto-renewing membership's terminal charge saves the card for
RECURRING charges and links it; without auto-renew no card is saved (ffl_core/membership.py,
payments/saved_cards.py). Real schema; only the Payroc client and the register's gates mocked."""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.utils import flt

from ffl_core import membership as ms
from ffl_core.setup import membership as membership_setup
from ffl_integrations.payments import api, saved_cards
from ffl_integrations.tests.site_fixtures import payroc_configured
from ffl_integrations.tests.test_card_on_file import _CardOnFileBase, _payment, _uniq

_PAYROC_CONFIGURED = payroc_configured()


def setUpModule():
	_PAYROC_CONFIGURED.start()


def tearDownModule():
	_PAYROC_CONFIGURED.stop()


def _plan() -> str:
	item = f"MEMC-{frappe.generate_hash(length=8)}".upper()
	frappe.get_doc({"doctype": "Item", "item_code": item, "item_name": item, "item_group": "Service",
		"stock_uom": "Nos", "is_stock_item": 0, "is_sales_item": 1}).insert(ignore_permissions=True)
	return frappe.get_doc({"doctype": ms.PLAN, "plan_name": _uniq("Card plan"), "active": 1,
		"billing_period": ms.MONTHLY, "price": 30, "item": item}).insert(ignore_permissions=True).name


@patch("ffl_integrations.payments.api._integration_ready", return_value=True)
@patch("ffl_integrations.payments.api._check_perm")
@patch("ffl_integrations.payroc.adapter.PayrocClient")
class MembershipCardOnFile(_CardOnFileBase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		membership_setup.ensure()
		cls.plan = _plan()

	def _sell(self, customer, auto_renew):
		m = ms.start_membership(customer, self.plan, auto_renew=auto_renew)["name"]
		inv = ms.invoice_membership(m)
		return m, inv["sales_invoice"], flt(inv["outstanding"])

	def test_auto_renew_saves_the_card_as_recurring_and_links_it(self, cc, _p, _r):
		client = cc.from_settings.return_value
		cust = self._customer()
		m, si, owed = self._sell(cust, 1)
		self.assertEqual(ms.saved_card_usage(si), "RECURRING")
		client.create_payment_instruction.return_value = {"paymentInstructionId": "PI-M1", "status": "IN_PROGRESS"}
		api.charge_sales_invoice(si, save_card=1)
		cof = client.create_payment_instruction.call_args.kwargs["credential_on_file"]
		self.assertEqual((cof["tokenize"], cof["usageAgreement"]), (True, "RECURRING"))
		self._approve(client, _payment("REF-M1", owed, reference=cof["merchantReference"]))
		res = api.poll_sales_invoice_charge(si, "PI-M1")
		self.assertTrue(res["ok"] and res["paid"] and res["card_saved"])
		card = frappe.get_doc(saved_cards.SAVED_CARD, res["saved_card"])
		self.assertEqual((card.usage, card.status, card.customer), ("RECURRING", "Active", cust))
		doc = frappe.get_doc(ms.DOCTYPE, m)
		self.assertEqual((doc.status, doc.auto_renew, doc.saved_card), (ms.ACTIVE, 1, card.name))
		row = next(r for r in ms.get_memberships(cust)["memberships"] if r["name"] == m)
		self.assertTrue(row["next_charge_date"])
		self.assertEqual(row["card_summary"], "Visa •1111")
		self._assert_no_token_anywhere(res, row)

	def test_without_auto_renew_no_card_is_saved(self, cc, _p, _r):
		client = cc.from_settings.return_value
		cust = self._customer()
		m, si, owed = self._sell(cust, 0)
		self.assertIsNone(ms.saved_card_usage(si))
		client.create_payment_instruction.return_value = {"paymentInstructionId": "PI-M2", "status": "IN_PROGRESS"}
		api.charge_sales_invoice(si)
		self.assertIsNone(client.create_payment_instruction.call_args.kwargs["credential_on_file"])
		self._approve(client, _payment("REF-M2", owed, token=None))
		self.assertTrue(api.poll_sales_invoice_charge(si, "PI-M2")["paid"])
		doc = frappe.get_doc(ms.DOCTYPE, m)
		self.assertEqual((doc.status, doc.auto_renew, doc.saved_card), (ms.ACTIVE, 0, None))
		self.assertEqual(self._cards(cust), [])

	def test_a_counter_order_still_saves_unscheduled(self, cc, _p, _r):
		client = cc.from_settings.return_value
		cust = self._customer()
		si, owed = self._unpaid_order(cust)
		client.create_payment_instruction.return_value = {"paymentInstructionId": "PI-M3", "status": "IN_PROGRESS"}
		api.charge_sales_invoice(si, save_card=1)
		cof = client.create_payment_instruction.call_args.kwargs["credential_on_file"]
		self.assertEqual(cof["usageAgreement"], "UNSCHEDULED")
		self._approve(client, _payment("REF-M3", owed, reference=cof["merchantReference"]))
		res = api.poll_sales_invoice_charge(si, "PI-M3")
		self.assertEqual(frappe.db.get_value(saved_cards.SAVED_CARD, res["saved_card"], "usage"), "UNSCHEDULED")

	def test_change_card_takes_only_a_recurring_card(self, cc, _p, _r):
		cust = self._customer()
		m, si, owed = self._sell(cust, 0)
		frappe.db.set_value("Sales Invoice", si, "outstanding_amount", 0)
		ms.settle(m)
		agreement = saved_cards.card_on_file_offer(cust)["agreement"]

		def card(usage):
			return frappe.get_doc({"doctype": saved_cards.SAVED_CARD, "customer": cust, "processor": "payroc",
				"token": "2967530000009999", "merchant_reference": f"t-{frappe.generate_hash(length=10)}",
				"card_summary": f"Visa {usage}", "usage": usage, "status": "Active", "authorization": agreement,
			}).insert(ignore_permissions=True).name

		unscheduled, recurring = card("UNSCHEDULED"), card("RECURRING")
		with self.assertRaises(frappe.ValidationError):
			ms.change_card(m, unscheduled)
		self.assertEqual(ms.change_card(m, recurring)["saved_card"], recurring)
		self.assertEqual(ms.set_auto_renew(m, 1)["auto_renew"], 1)

	def test_save_request_refuses_an_unknown_usage(self, cc, _p, _r):
		with self.assertRaises(frappe.ValidationError):
			saved_cards.save_request(self._customer(), None, usage="WHENEVER")
