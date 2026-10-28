"""Price approval on the paths that need ffl_integrations: the customer-transfer ticket is never asked
for a manager's approval (a lower fee later is), nor is a Create Order ship-to-store line at its quote price.

The transfer builder (fastbound.pos_4473.start_transfer_for_pos_invoice) puts the customer's own gun
on at $0 and the per-gun fee at what the dialog sent. With FFL Settings → Require Manager Approval
for Price Reductions on, a gun that has a price of its own and a fee item listed above the fee would
both read as lowered prices. The builder's save inside the clerk's request is not a save the clerk
sent (price_approval._client_save), so the ticket saves with nobody's approval; the register saving
it afterwards is judged. The 4473 kickoff it hands off to is stubbed.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.utils import flt

from ffl_core import price_approval as pa
from ffl_core.firearm import serialized_receipt_row
from ffl_core.setup.transfer_fee import TRANSFER_FEE_ITEM
from ffl_core.tests.settings_sandbox import SettingsSandbox
from ffl_core.tests.test_ffl_transfer import _bootstrap, _ensure_firearm_item
from ffl_core.tests.test_gunsmith_work_order import COMPANY, PROFILE
from ffl_core.tests.test_gunsmith_work_order import _Base as _CounterBase
from ffl_core.tests.test_price_approval import _price, _request


class TransferTicket(SettingsSandbox, _CounterBase):

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_bootstrap()

	def _gun(self) -> str:
		item = _ensure_firearm_item("PA-XFER-GUN")
		serial = "PA-XF-" + frappe.generate_hash(length=8).upper()
		se = frappe.get_doc({"doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "company": COMPANY,
			"items": [serialized_receipt_row(item_code=item, qty=1, warehouse="Stores - FT", basic_rate=300,
				serial=serial)]})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
		frappe.db.set_value("Serial No", serial, "sell_price", 900)   # a price of its own: $0 reads as lowered
		return serial

	def test_the_ticket_saves_unapproved_and_a_lower_fee_later_asks(self):
		from ffl_integrations.fastbound import pos_4473

		self.set_single("FFL Settings", pa.SETTING, 1)
		_price(TRANSFER_FEE_ITEM, 50)   # the fee item lists at 50; the dialog sends 25
		serial = self._gun()
		own = patch("ffl_core.price_approval.is_approver", return_value=False)
		own.start()
		self.addCleanup(own.stop)
		_request(self, "ffl_integrations.fastbound.pos_4473.start_transfer_for_pos_invoice")
		with patch.object(pos_4473, "start_4473_for_pos_invoice", side_effect=lambda name, _m: {"pos_invoice": name}):
			out = pos_4473.start_transfer_for_pos_invoice(serials=[serial], fee_item=TRANSFER_FEE_ITEM, fee_amount=25,
				customer=self.cust.name, pos_profile=PROFILE)
		inv = frappe.get_doc("POS Invoice", out["pos_invoice"])
		self.assertEqual(sorted(flt(r.rate) for r in inv.items), [0, 25])
		self.assertFalse(inv.get(pa.BY))
		_request(self)   # the register's save from here on
		inv.flags.ignore_permissions = True
		inv.save()   # the register saving it again, unchanged: nothing asks
		fee = next(r for r in inv.items if r.item_code == TRANSFER_FEE_ITEM)
		fee.rate = 10
		with self.assertRaises(pa.PriceApprovalRequired):
			inv.save()


from ffl_core.tests.test_counter_to_store import ACC, CounterToStoreFixtures  # noqa: E402


class ShipToStoreLine(SettingsSandbox, CounterToStoreFixtures):
	"""A Create Order line ordered in from a distributor carries the counter price from the quote, which
	may sit under the Item Price by design: create_order marks those lines (price_approval.mark_quoted),
	so they are not judged against a list price."""

	def test_a_distributor_line_at_its_quote_price_saves_with_no_approval(self):
		self.set_single("FFL Settings", pa.SETTING, 1)
		_price(ACC, 90)   # the website's price; the counter quote for the line is 40
		own = patch("ffl_core.price_approval.is_approver", return_value=False)
		own.start()
		self.addCleanup(own.stop)
		ship_to = {"address_line1": "5 Elm St", "city": "Springfield", "state": "TX", "pincode": "75024"}
		out = self._counter([self._line(ACC, qty=2, rate=40)], ship_to=ship_to)
		self.assertTrue(out["ok"])
		self.assertFalse(frappe.db.get_value("Sales Invoice", out["sales_invoice"], pa.BY))
