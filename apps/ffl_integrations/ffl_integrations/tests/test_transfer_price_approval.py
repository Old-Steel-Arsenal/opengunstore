"""The customer-transfer ticket is never asked for a manager's price approval; a lower fee later is.

The transfer builder (fastbound.pos_4473.start_transfer_for_pos_invoice) puts the customer's own gun
on at $0 and the per-gun fee at what the dialog sent. With FFL Settings → Require Manager Approval
for Price Reductions on, a gun that has a price of its own and a fee item listed above the fee would
both read as lowered prices — the builder marks its document (price_approval.mark_system_priced), so
the ticket saves with nobody's approval. The 4473 kickoff it hands off to is stubbed.
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
from ffl_core.tests.test_price_approval import _price


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
		with patch.object(pos_4473, "start_4473_for_pos_invoice", side_effect=lambda name, _m: {"pos_invoice": name}):
			out = pos_4473.start_transfer_for_pos_invoice(serials=[serial], fee_item=TRANSFER_FEE_ITEM, fee_amount=25,
				customer=self.cust.name, pos_profile=PROFILE)
		inv = frappe.get_doc("POS Invoice", out["pos_invoice"])
		self.assertEqual(sorted(flt(r.rate) for r in inv.items), [0, 25])
		self.assertFalse(inv.get(pa.BY))
		inv.flags.ignore_permissions = True
		inv.save()   # the register saving it again, unchanged: nothing asks
		fee = next(r for r in inv.items if r.item_code == TRANSFER_FEE_ITEM)
		fee.rate = 10
		with self.assertRaises(pa.PriceApprovalRequired):
			inv.save()
