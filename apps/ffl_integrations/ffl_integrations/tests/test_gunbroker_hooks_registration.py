"""The handlers this integration registers, asserted where they are declared.

A hooks dict is the one contract in the app that nothing else checks. Every
handler here has its own tests, and all of them pass whether or not the handler
is ever called — so a registration that disappears is invisible: no syntax
error, no failing test, one behaviour simply gone.

That is not hypothetical on this branch. `Sales Invoice` is a key two pull
requests are both adding to, and if the values are bare strings the natural way
to resolve the merge is to keep both lines — which leaves a dict literal with
the same key twice, and Python takes the last one. Whichever handler loses,
nothing says so: either a sold gun stays listed on GunBroker (a second buyer
can still pay for it), or a cancelled invoice keeps its fee accrual on the
ledger for ever.

So the values are lists, and these tests say so. A list has exactly one
sensible merge resolution, and it is the right one.
"""
from __future__ import annotations

import unittest

from ffl_integrations import hooks

CANCEL = "ffl_integrations.gunbroker.revenue.reverse_fee_accrual_on_cancel"
SUBMIT = "ffl_integrations.gunbroker.revenue.rebook_on_amended_submit"
VALIDATE = "ffl_integrations.gunbroker.revenue.refuse_a_negative_liability_on_validate"


class SalesInvoiceHandlers(unittest.TestCase):
	def _events(self, key: str):
		return hooks.doc_events["Sales Invoice"][key]

	def test_on_cancel_reverses_the_fee_accrual(self):
		self.assertIn(CANCEL, self._events("on_cancel"))

	def test_on_submit_rebooks_an_amendment(self):
		self.assertIn(SUBMIT, self._events("on_submit"))

	def test_validate_refuses_a_negative_marketplace_liability(self):
		# The booking path's own check sits immediately before its submit, and
		# the Desk does not go through it.
		self.assertIn(VALIDATE, self._events("validate"))

	def test_both_keys_are_lists_so_a_second_handler_cannot_replace_the_first(self):
		# The whole point. A string value invites "write the key again", and
		# Python keeps the last one written.
		for key in ("validate", "on_submit", "on_cancel"):
			self.assertIsInstance(self._events(key), list, key)

	def test_the_counter_guards_that_were_already_there_still_are(self):
		# The other side of the same risk: this change edited both keys, and a
		# list built by hand is a list something can be dropped out of.
		self.assertIn(
			"ffl_integrations.distributor.counter.guard_sales_invoice_submit",
			self._events("on_submit"))
		for path in ("ffl_integrations.distributor.counter.repin_dropship_lines",
				"ffl_integrations.payments.api.require_card_reference_on_manual_tender"):
			self.assertIn(path, self._events("validate"))
		# Containment, not equality: this key is still a bare string, and the
		# lesson of this very file is that somebody will eventually make it a
		# list. That should not turn this test red.
		self.assertIn(
			"ffl_integrations.distributor.counter.guard_sales_invoice_cancel",
			hooks.doc_events["Sales Invoice"]["before_cancel"])

	def test_every_registered_path_resolves_to_something_callable(self):
		# A renamed function leaves the dotted path behind, and Frappe only
		# finds out at the moment the document is saved.
		import importlib

		for key in ("validate", "on_submit", "on_cancel"):
			for path in self._events(key):
				module_name, _sep, attr = path.rpartition(".")
				module = importlib.import_module(module_name)
				self.assertTrue(callable(getattr(module, attr, None)), path)


if __name__ == "__main__":
	unittest.main()
