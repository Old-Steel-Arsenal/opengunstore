"""Woo Online Order — tracks inbound WooCommerce orders and soft-reserved serials."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from ffl_core.compliance.ffl_number import FFL_NUMBER_RE, is_cr_license

TRANSFERRED = "Transferred"


def mark_transferred(order_name: str, *, at=None) -> str:
	"""Flip an order to Transferred and stamp WHEN — the single front door.

	``transferred_at`` is the date revenue belongs to (``revenue._build_invoice``
	posts on it, so a booking that runs late — the 2-hourly backstop, a manual
	retry — cannot drag an August sale into September). That invariant only
	holds if every path that sets the status also sets the stamp, and the flip
	happens in five places across three apps: the web disposition, three pickup
	transitions, and the drop-ship completion in ``ffl_integrations`` (which
	reaches this by dotted path, since it must not import this app). Each of
	them writes raw — loading and saving the document would drag order
	validation into a POS submit or a distributor job that has nothing to do
	with it — so the controller's own guard cannot cover them. One function,
	so the sixth caller cannot forget.

	Returns the stamp, for callers holding a stale in-memory copy.
	"""
	stamp = at or now_datetime()
	frappe.db.set_value("Woo Online Order", order_name,
		{"order_status": TRANSFERRED, "transferred_at": stamp},
		update_modified=False)
	from ffl_core.crm.web_orders import TRANSFERRED as NOTE_TRANSFERRED, note_web_order

	note_web_order(order_name, NOTE_TRANSFERRED)
	return stamp


def unmark_transferred(order_name: str, *, status: str) -> None:
	"""Move an order back out of Transferred (a voided pickup), clearing the
	stamp with it — the twin of :func:`mark_transferred`."""
	frappe.db.set_value("Woo Online Order", order_name,
		{"order_status": status, "transferred_at": None},
		update_modified=False)


class WooOnlineOrder(Document):
	def validate(self):
		self._validate_ffl_license()
		self._validate_unique_woo_order()
		self._stamp_transferred_at()

	def _stamp_transferred_at(self):
		"""Record the moment the order became Transferred — the revenue date.

		Keyed on the TRANSITION, not on "the field is empty". Every order that
		was already Transferred before this field existed carries NULL, and an
		ordinary later save of such an order — the ``order.updated`` webhook
		backfilling a serial, recording a refund, refreshing ship-to — must not
		invent "now" as the moment it shipped: that fake stamp would become the
		posting date (and the ``remarks`` provenance line) of any invoice booked
		for it later. A NULL stamp is honest; ``_build_invoice`` falls back to
		today and says so.

		``has_value_changed`` reports True on insert (no previous doc), which is
		the wanted semantics for an order created straight into Transferred.
		The paths that flip the status with a raw ``db.set_value`` bypass
		validate entirely and stamp through :func:`mark_transferred`.
		"""
		if not self.has_value_changed("order_status"):
			return
		if (self.order_status or "") == TRANSFERRED:
			self.transferred_at = now_datetime()
		elif self.transferred_at:
			# Un-Transferred (a voided pickup): the stamp goes with the status,
			# so a second Transferred re-stamps honestly.
			self.transferred_at = None

	def _validate_ffl_license(self):
		"""Check the FFL number's shape and derive the C&R flag from it.

		Only a CHANGED value is judged. Live orders carry historical junk in this
		field (imported records, a dealer's name typed in by hand); judging those
		unconditionally would make them permanently unsaveable — and they are
		saved by the serial-backfill webhook, so those orders would wedge whole.

		Whitespace is normalized away only when doing so YIELDS a valid number.
		That ordering is load-bearing in both directions: it keeps a stray
		trailing newline out of the database (where it would make the stored
		value differ from the one we validated, and put a newline inside the ATF
		FFL Record's name), while never turning a benign legacy value into a
		"changed" one that then fails the format check and blocks the save.

		The C&R flag is recomputed on every save, changed or not: it is derived
		data and the licence number is its single source of truth, so a hand-set
		or migrated value must not be allowed to survive.
		"""
		raw = self.ffl_license or ""
		stripped = raw.strip()
		if stripped != raw and FFL_NUMBER_RE.match(stripped):
			self.ffl_license = stripped

		if (self.ffl_license and self.has_value_changed("ffl_license")
				and not FFL_NUMBER_RE.match(self.ffl_license)):
			# Throw only for a HUMAN edit. The rule asks for format validation
			# on what staff type; WooCommerce is not staff and its payload is not
			# ours to reject at save time. Throwing on an inbound sync would take
			# the whole order down — that same save also carries serial backfill,
			# status and refunds — and on the create path it would abort insert()
			# entirely: the order would never exist, its serial would never be
			# soft-reserved, and the firearm would stay sellable at the counter
			# while the webhook still answered 200 so Woo never retried.
			#
			# woo_sync_inbound is the repo's existing "this write came from the
			# Woo webhook" flag (set for the whole inbound request, see
			# webhook.py's loop guard). Record the bad value and let the sync land;
			# it still fails loudly at dispose, where _resolve_and_validate_ffl
			# cannot resolve it.
			if frappe.flags.get("woo_sync_inbound"):
				frappe.log_error(
					title=f"Woo Online Order {self.name}: malformed FFL number from WooCommerce",
					message=(f"WooCommerce sent {self.ffl_license!r}, which is not a valid ATF "
						f"licence number. It was stored as-is so the rest of the sync (serials, "
						f"status, refunds) still lands — fix it on WooCommerce. The order cannot "
						f"be disposed until the number resolves."))
			else:
				frappe.throw(_(
					"FFL license {0} is not a valid ATF license number "
					"(expected 1-99-999-99-9X-99999)."
				).format(self.ffl_license))

		self.is_cr_transfer = 1 if is_cr_license(self.ffl_license or "") else 0

	def _validate_unique_woo_order(self):
		"""Enforce composite uniqueness on (woo_order_id, site).

		``woo_order_id`` is a WooCommerce store's own auto-increment, so it is only
		unique within that store: the key is (woo_order_id, site). One store runs
		today (retail); the key stays per store so two orders for the same
		(woo_order_id, site) can never coexist.
		"""
		if not self.woo_order_id:
			return
		dupe = frappe.db.exists(
			"Woo Online Order",
			{
				"woo_order_id": self.woo_order_id,
				"site": self.site or "retail",
				"name": ("!=", self.name),
			},
		)
		if dupe:
			frappe.throw(
				_("A Woo Online Order already exists for order {0} on site {1} ({2}).").format(
					self.woo_order_id, self.site or "retail", dupe
				)
			)
