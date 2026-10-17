"""GunBroker Order — the POS-side record of a sale made on GunBroker.

The mirror of a document that lives somewhere else. GunBroker owns the order's
lifecycle; this record follows it, exactly as Woo Online Order follows a
WooCommerce order, and for the same reason it is deliberately NOT submittable:
there is no state here we are entitled to declare final.

Two things it does own, and they are the two that matter:

``order_status`` is OUR fulfillment state — Pending Fulfillment until the
bound-book exit is booked, then Transferred. GunBroker's own numeric status
rides alongside in ``gb_status`` and never drives a decision on its own.

``payment_mode_used`` is the record that WE took the money. GunBroker's
``payment_received`` flag is a click on their website; it is mirrored here for
reporting and it is not evidence of anything on our ledger. The Dispose gate
reads ``payment_mode_used``, so a gun cannot leave the building on the strength
of somebody else's checkbox.

Field notes (kept here, not in the form, which is for staff):

- ``gb_order_id`` is the poll's idempotency key, unique at the database level.
- Every Datetime is site local time, converted from GunBroker's ``…UTC`` twin
  (or its Eastern reading where there is none); the verbatim values stay in
  ``raw_payload``. The poll watermark in GunBroker Settings stays UTC.
- Correcting ``ffl_number`` in the C&R dialog also clears ``atf_ffl_record``,
  so the two never describe different licences. ``is_cr_transfer`` is derived
  from ``ffl_number`` and never stored independently.
- ``sales_tax_total`` is GunBroker's combined figure (marketplace tax plus the
  1% ``compliance_fee``); the invoice splits it by ``compliance_fee`` and stays
  in Draft when that field is missing rather than guessing.
- ``payment_received_on`` drives the cash-basis schedule and bank
  reconciliation; a recorded ``payment_mode_used`` always carries it.
- ``payment_entry`` and ``fee_accrual_je`` are idempotency keys: non-empty
  means already booked. An empty ``gb_overpayment_pe`` on an overpaid order is
  retried by the next booking run.
- Ship write-back is three steps (OrderShipped flag, tracking, read-back);
  ``gb_ship_writeback_pending`` marks a failed step 2 or 3 for the tracking
  poll to retry.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, nowdate

from ffl_core.compliance.ffl_number import is_cr_license


class GunBrokerOrder(Document):
	def validate(self):
		self._require_order_id()
		self._derive_cr_flag()
		self._check_payment_invariants()

	# ---------------------------------------------------------------- identity

	def _require_order_id(self) -> None:
		self.gb_order_id = (self.gb_order_id or "").strip()
		if not self.gb_order_id:
			frappe.throw(_("GunBroker Order ID is required."))
		# Belt to the unique index's braces. The index is what actually prevents a
		# double order under a race — but it surfaces as a raw IntegrityError, and
		# order polling wants to recognise "already have this one" without parsing
		# database error text.
		duplicate = frappe.db.get_value(
			"GunBroker Order",
			{"gb_order_id": self.gb_order_id, "name": ("!=", self.name or "")},
			"name")
		if duplicate:
			frappe.throw(
				_("GunBroker order {0} is already recorded as {1}.").format(
					self.gb_order_id, duplicate))

	# --------------------------------------------------------------------- C&R

	def _derive_cr_flag(self) -> None:
		"""``is_cr_transfer`` is computed from the licence number, every save.

		Never stored as an independent fact: staff can correct the number in the
		dispose dialog, and a stale flag would send a Type 03 collector's order
		down the eZ Check path (which cannot resolve a collector) or, worse, an
		ordinary dealer transfer through the collector dialog.
		"""
		self.is_cr_transfer = 1 if is_cr_license(self.ffl_number or "") else 0

	# ----------------------------------------------------------------- payment

	def _check_payment_invariants(self) -> None:
		"""A recorded payment always carries the day it was deposited.

		The pair is an invariant rather than two independent fields because a
		blank date is invisible in exactly the place it matters most. The
		cash-basis schedule selects on ``payment_received_on <= period_end``, and
		SQL says NULL <= anything is not true — so an order with the money in the
		bank, the gun already shipped (``payment_mode_used`` is what opens that
		gate) and no deposit date would silently never appear on the schedule the
		accountant works from. mark_payment_received defaults the date to today
		rather than leaving it blank; this is the backstop for a row written any
		other way.
		"""
		mode = (self.payment_mode_used or "").strip()
		if mode and not self.payment_received_on:
			frappe.throw(_(
				"{0} records a payment by {1} but no deposit date. The date is "
				"what puts this order on the cash-basis schedule — without it the "
				"money is invisible to the year-end close. Set 'Deposited On'."
			).format(self.name or _("This order"), mode),
				title=_("Deposit Date Required"))
		if self.payment_received_on and getdate(self.payment_received_on) > getdate(nowdate()):
			frappe.throw(_(
				"A payment cannot be deposited in the future ({0})."
			).format(self.payment_received_on), title=_("Deposit Date"))
