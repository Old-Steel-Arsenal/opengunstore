"""The doc_events entries GunBroker will have to share must survive being shared.

Three keys in this app's `doc_events` hold a BARE STRING today:

    POS Invoice.before_submit    -> payments.api.block_partial_submit
    Sales Invoice.on_submit      -> distributor.counter.guard_sales_invoice_submit
    Woo Online Order.after_insert/on_update -> distributor.web_router.on_woo_order_change
      (since 1.5 the osa_dropship extension's; the key stays a list here)

GunBroker needs a handler on each of them, and Frappe's hooks are a plain dict:
adding one by writing `"POS Invoice": {"before_submit": "…gunbroker…"}` REPLACES
the incumbent rather than joining it. On the first of those keys that would
delete the server-side guard against submitting a partially-captured card sale
— three rounds of hardening, removed by an edit that looks like an addition and
breaks no test, because nothing else asserts those handlers are still there.

This file is that assertion. It runs now, while the strings are still strings,
so the protection is in place BEFORE the edit that endangers it — and it is
written to keep working after the conversion to lists, checking the ordering
from that point on.

Two different questions, deliberately asked of two different objects:

* what THIS app declares — read off the module attribute, because that is what
  a careless edit changes; and
* what the site ends up running — read off `frappe.get_hooks`, which merges
  every app in `apps.txt` order. Only a RELATIVE order can be asserted there:
  ffl_core registers its own handlers on two of these keys and, being earlier
  in apps.txt, is always first in the merged list.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations import hooks

# (doctype, event, the handler that must not be displaced)
INCUMBENTS = (
	("POS Invoice", "before_submit",
		"ffl_integrations.payments.api.block_partial_submit"),
	# The delete guard stays first when GunBroker shares the key.
	("POS Invoice", "on_trash",
		"ffl_integrations.payments.api.block_locked_return_trash"),
	("Sales Invoice", "on_submit",
		"ffl_integrations.distributor.counter.guard_sales_invoice_submit"),
)

# The other direction: OUR handlers on keys another branch is also editing.
#
# INCUMBENTS above protects what was here before GunBroker. This protects
# GunBroker from the same mistake made by the next author — and specifically
# from a MERGE, where both sides edited the same key and only one side's value
# survives the resolution. `Sales Invoice.on_cancel` is that key today: PR-3
# turns it into a list to append `revenue.reverse_fee_accrual_on_cancel`, and a
# resolution that simply keeps PR-3's list drops the delist with nothing red to
# say so — a gun stays for sale on GunBroker after its invoice is cancelled.
#
# PRESENCE only, deliberately: unlike a submit guard, nothing here has to run
# first, and pinning an order nobody needs would turn an ordinary merge into a
# false failure.
OURS_THAT_A_MERGE_MUST_NOT_DROP = (
	("Sales Invoice", "on_cancel",
		"ffl_integrations.gunbroker.doc_events.on_invoice_change"),
	("Sales Invoice", "on_submit",
		"ffl_integrations.gunbroker.doc_events.on_invoice_change"),
	# A model listing's quantity follows the guns a parked counter ticket holds, and
	# its price and standing follow its Item.
	("POS Invoice", "on_update",
		"ffl_integrations.gunbroker.doc_events.on_invoice_draft_change"),
	("POS Invoice", "on_trash",
		"ffl_integrations.gunbroker.doc_events.on_invoice_draft_change"),
	("Item", "on_update",
		"ffl_integrations.gunbroker.doc_events.on_item_update"),
	# Shared with automatic sourcing.
	("Stock Ledger Entry", "on_submit",
		"ffl_integrations.gunbroker.doc_events.on_sle_submit"),
	("Stock Ledger Entry", "on_cancel",
		"ffl_integrations.gunbroker.doc_events.on_sle_cancel"),
)

# Every Payroc guard that hangs off POS Invoice, plus the two distributor
# handlers on the keys GunBroker touches. Named individually so a rename or a
# module move is caught here rather than at the till.
MUST_RESOLVE = (
	"ffl_integrations.payments.api.restore_charge_rows",
	"ffl_integrations.payments.api.protect_money_scalars",
	"ffl_integrations.payments.api.require_card_reference_on_manual_tender",
	"ffl_integrations.payments.api.block_unconfirmed_gateway_refund",
	"ffl_integrations.payments.api.block_partial_submit",
	"ffl_integrations.payments.api.block_locked_return_trash",
	"ffl_integrations.distributor.counter.guard_sales_invoice_submit",
	# The Payment Entry remarks guard: a dotted path that only fires on a save
	# nobody makes in a test, so a rename lands in production silently.
	"ffl_integrations.gunbroker.revenue.keep_gunbroker_remarks",
)


def _as_list(value) -> list[str]:
	"""Frappe accepts a bare string or a list for a doc_event. Normalise, so
	this file keeps working across the conversion it exists to protect."""
	if value is None:
		return []
	return [value] if isinstance(value, str) else list(value)


class GunBrokerHookRegistrySurvives(FrappeTestCase):
	def test_a_merge_cannot_quietly_drop_our_own_handler(self):
		"""The failure this is here for leaves NOTHING else red.

		Two branches edit `Sales Invoice.on_cancel`; the resolution keeps one
		side's value; the GunBroker delist is gone. Every GunBroker test still
		passes, because they all call the handler directly — the only thing that
		knew it was wired up was hooks.py.
		"""
		for doctype, event, handler in OURS_THAT_A_MERGE_MUST_NOT_DROP:
			declared = _as_list((hooks.doc_events.get(doctype) or {}).get(event))
			self.assertIn(handler, declared,
				f"{doctype}.{event} no longer runs {handler}. If another branch "
				f"added a handler to this key, the resolution must keep BOTH — "
				f"append to the list, never replace it.")

	def test_every_shared_key_is_declared_as_a_list(self):
		"""A bare string is the shape that gets replaced.

		`"on_cancel": "a"` invites the next author to write `"on_cancel": "b"`,
		and both a merge tool and a reviewer read that as an edit rather than a
		deletion. A list makes appending the obvious move.
		"""
		for doctype, event, _handler in OURS_THAT_A_MERGE_MUST_NOT_DROP:
			value = (hooks.doc_events.get(doctype) or {}).get(event)
			self.assertIsInstance(value, list,
				f"{doctype}.{event} is declared as a bare {type(value).__name__}; "
				f"make it a list, even with one entry.")

	def test_this_app_still_declares_every_incumbent_handler_first(self):
		"""Read the module attribute — that is what an edit here changes.

		`frappe.get_hooks` would answer with every app's contribution merged
		together, which is a different question and would hide this app
		overwriting its own entry.
		"""
		for doctype, event, handler in INCUMBENTS:
			declared = _as_list((hooks.doc_events.get(doctype) or {}).get(event))
			self.assertIn(handler, declared,
				f"{doctype}.{event} no longer runs {handler} — adding a handler to a "
				f"doc_event REPLACES it unless the value is made a list with the "
				f"original kept.")
			self.assertEqual(declared[0], handler,
				f"{doctype}.{event} must keep {handler} first: a guard that refuses "
				f"a submit has to run before anything that might throw for another "
				f"reason.")

	def test_a_gunbroker_handler_never_displaces_an_incumbent(self):
		"""Relative order in the MERGED list, once GunBroker registers.

		Skipped while GunBroker has no handler on the key — which is the state
		this slice ships in. The assertion is written now so that the slice which
		adds those handlers inherits a test that already knows what it must not
		break, instead of one written afterwards by whoever broke it.

		Relative, never absolute: `frappe.get_hooks` extends per app in apps.txt
		order and ffl_core is ahead of this app, so index 0 of the merged list is
		ffl_core's handler on two of these keys.
		"""
		merged = frappe.get_hooks("doc_events") or {}
		checked = 0
		for doctype, event, handler in INCUMBENTS:
			handlers = _as_list((merged.get(doctype) or {}).get(event))
			gunbroker = [h for h in handlers if ".gunbroker." in h]
			if not gunbroker:
				continue
			checked += 1
			self.assertIn(handler, handlers,
				f"{doctype}.{event}: {handler} vanished when GunBroker was added.")
			self.assertLess(
				handlers.index(handler), handlers.index(gunbroker[0]),
				f"{doctype}.{event}: {handler} must run before {gunbroker[0]}.")
		if not checked:
			self.skipTest("no GunBroker doc_events handlers registered yet")

	def test_every_protected_handler_still_resolves(self):
		"""A dotted path in hooks.py is only checked when it fires.

		A renamed or moved function stays in the hooks dict looking perfectly
		fine, and the first sign of trouble is an AttributeError in the middle of
		a sale.
		"""
		for path in MUST_RESOLVE:
			try:
				resolved = frappe.get_attr(path)
			except Exception as exc:  # noqa: BLE001 — the failure IS the finding
				self.fail(f"{path} no longer resolves: {exc}")
			self.assertTrue(callable(resolved), f"{path} is not callable")
