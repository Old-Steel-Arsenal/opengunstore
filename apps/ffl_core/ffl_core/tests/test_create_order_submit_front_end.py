"""Create Order's submit button, read off the shipped JS.

With price approval on, _submit / _submitEdit await a network check before
calling the server. The button must be disabled BEFORE that await: a second
click inside the window would book a second Sales Order, Sales Invoice and
payment (create_order has no idempotency key). This repo ships no JS test runner
for the page class, so the order of the two statements is asserted on the source.
"""
from __future__ import annotations

import unittest

from ffl_core.tests.front_end_invariants import block, code_only, read


def _page() -> str:
	return read("ffl_core", "ffl_core", "page", "create_order", "create_order.js")


class SubmitDisablesBeforeApprovalCheck(unittest.TestCase):
	def test_the_button_is_disabled_before_the_price_approval_await(self):
		for opener in ("\tasync _submit() {", "\tasync _submitEdit() {"):
			with self.subTest(method=opener.strip()):
				body = code_only(block(_page(), opener, "\t"))
				disable = body.index("$btn.prop('disabled', true)")
				check = body.index("await ffl.priceApproval.forOrder(")
				self.assertLess(disable, check)

	def test_a_cancelled_approval_gives_the_button_back(self):
		for opener in ("\tasync _submit() {", "\tasync _submitEdit() {"):
			with self.subTest(method=opener.strip()):
				body = code_only(block(_page(), opener, "\t"))
				line = next(l for l in body.splitlines() if "await ffl.priceApproval.forOrder(" in l)
				self.assertIn("$btn.prop('disabled', false); return;", line)


class RemovedFirearmDropsDestination(unittest.TestCase):
	def test_a_hidden_destination_section_clears_its_ffl(self):
		"""Removing the last firearm hides the destination section; the FFL it
		held must go too, or the remaining goods ship to that dealer."""
		body = code_only(block(_page(), "\t_syncDestSection() {", "\t"))
		self.assertIn("this.buyerType === 'individual' && !show && !this._isPickup()", body)
		self.assertIn("this.fields.dest_ffl.set_value('')", body)
		self.assertIn("this.destinationFfl = staleDest ? null :", body)


if __name__ == "__main__":
	unittest.main()
