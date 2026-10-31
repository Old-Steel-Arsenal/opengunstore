"""Firearm compliance validators wired into ERPNext's transaction pipeline.

Server-side enforcement so any client (POS, Frappe Desk, REST API, mobile) is covered.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from ffl_core.firearm import (
	ALL_FIREARM_GROUPS as FIREARM_ITEM_GROUPS,  # noqa: F401  re-exported for legacy callers
	is_firearm_item,
	is_regulated_firearm_item,
)
from ffl_core.compliance.mode import paper_4473_mode
from ffl_core.serials import serials_for_line
from ffl_core.utils import split_serials

# 4473 form must be completed before firearm leaves the store.
FORM_4473_REQUIRED = "Completed"

# NICS background check outcomes that legally allow disposition. "Delayed" is
# *not* sufficient on its own — store may proceed only after FBI 3-day window
# elapses and a separate "Proceed" determination is recorded; that workflow
# happens outside of POS submit and produces a Proceed status on retry.
NICS_PROCEED_STATUSES: frozenset[str] = frozenset({"Proceed"})


def is_distributor_dropship_row(row) -> bool:
	"""True ONLY for a line the distributor ships direct (FDS drop-ship):
	ERPNext's native ``delivered_by_supplier`` AND our ``dropship_distributor`` pin,
	**both** set.

	Deliberately the narrowest expressible predicate, because it switches off a
	compliance guard:

	* a LOCAL firearm row carries neither flag, so every guard still applies to it;
	* a **procure-to-store** row (P4c) is a LOCAL row — received into our inventory,
	  dispositioned here, serial assigned — so ``delivered_by_supplier`` is 0 by
	  design and its guards stay intact;
	* requiring BOTH means a stray core flag alone can never widen the exemption.
	"""
	return bool(getattr(row, "delivered_by_supplier", 0)) and bool(
		str(getattr(row, "dropship_distributor", "") or "").strip())


def is_awaiting_procurement_row(doc, row) -> bool:
	"""True ONLY for a counter procure-to-store FIREARM line whose gun has not
	arrived yet: a deferred (``update_stock=0``) Sales Invoice, a line carrying the
	``ffl_to_store_distributor`` pin, not a distributor drop-ship row, and no serial.

	The sale is booked before the gun exists in our inventory (an order placed
	ahead of procurement); the serial is written onto the line when the
	procure-to-store receive hangs it (``receive_bridge``). Nothing leaves stock on this invoice, and the
	disposition that eventually ships the gun still requires that serial — so the
	bound-book guard is deferred, never dropped."""
	return (getattr(doc, "doctype", "") == "Sales Invoice"
		and not int(getattr(doc, "update_stock", 0) or 0)
		and bool(str(getattr(row, "ffl_to_store_distributor", "") or "").strip())
		and not bool(getattr(row, "delivered_by_supplier", 0))
		and not (getattr(row, "serial_no", None) or "").strip()
		and is_firearm_item(row.item_code))


def canonical_serial_no(serial: str | None) -> str | None:
	"""The Serial No's name exactly as stored, or None when no such record.

	MariaDB matches Link values case-insensitively, so a cashier's ``1976ON-4786``
	resolves to the stored ``1976on-4786`` and passes validation — but ERPNext
	builds the Serial and Batch Bundle from the STORED name and later compares
	it to ``row.serial_no`` with a case-sensitive Python ``==``
	(stock_controller.validate_serial_nos_and_batches_with_bundle). The
	mismatch surfaces only at consolidation, as the misleading "Bundle has
	already created" error."""
	serial = (serial or "").strip()
	if not serial:
		return None
	return frappe.db.get_value("Serial No", serial, "name")


def canonicalize_serial_string(raw: str | None) -> str:
	"""Rewrite each serial token of an ERPNext ``serial_no`` field to its stored
	case; unknown serials pass through untouched (the Link check reports them)."""
	tokens = split_serials(raw)
	return "\n".join(canonical_serial_no(t) or t for t in tokens)


def canonicalize_row_serials(doc, method: str | None = None) -> None:
	"""validate hook (POS Invoice / Sales Invoice / Delivery Note): make every
	line's ``serial_no`` match the stored Serial No case so the bundle ERPNext
	creates at submit agrees with the line at consolidation. See
	canonical_serial_no for why."""
	for row in doc.get("items") or []:
		raw = row.get("serial_no")
		if not (raw or "").strip():
			continue
		fixed = canonicalize_serial_string(raw)
		if fixed != raw:
			row.serial_no = fixed


def require_serial_for_firearm(doc, method: str | None = None) -> None:
	"""Reject any transaction line where a firearm-class Item has no Serial No
	picked on the line.

	Every firearm is has_serial_no=1, so ERPNext's row.serial_no field is the
	only source of truth (the legacy Item.firearm_serial fallback was removed
	when the Used Firearm path was unified). Item Group membership decides
	"firearm-ness", so a misconfigured Item still cannot smuggle a firearm
	through without a serial.

	Hooked on `validate` of Sales Invoice / Sales Order / Delivery Note /
	POS Invoice.

	Sales Order is exempt: an SO is a pre-order carrier (Create Order, B2B)
	— the gun may not be in the building yet and SO Item has no serial_no
	field at all. The serial is enforced where stock actually leaves:
	Delivery Note / Sales Invoice / POS Invoice — and only at SUBMIT, like
	the 4473 gate: drafts legitimately park serial-less firearm rows (a
	pickup before the cashier assigns the physical gun), and
	no stock moves until submit.
	"""
	if getattr(doc, "doctype", "") == "Sales Order":
		return
	if int(getattr(doc, "docstatus", 0) or 0) != 1:
		return
	rows = getattr(doc, "items", None) or []
	for row in rows:
		if not is_firearm_item(row.item_code):
			continue
		if is_distributor_dropship_row(row):
			# A distributor drop-ship (FDS) gun ships from RSR's dock straight to the
			# buyer's transfer FFL and its serial is reported back on the Distributor
			# Order — there is no serial of OURS to put on the line, and no bound-book
			# entry is made here (H1). Every local firearm row is unaffected: it
			# carries neither flag (see is_distributor_dropship_row).
			continue
		if is_awaiting_procurement_row(doc, row):
			continue
		if (getattr(row, "serial_no", None) or "").strip():
			continue
		frappe.throw(
			_("Firearm item '{0}' (row #{1}) requires a serial number on the line.")
				.format(row.item_code, row.idx),
			title=_("Serial Number Required"),
		)


def require_one_firearm_per_line(doc, method: str | None = None) -> None:
	"""Reject a regulated-firearm line that rings more than one gun.

	Every regulated firearm is unique (has_serial_no=1) and the ENTIRE 4473 /
	A&D pipeline assumes ONE gun per line: the FastBound webhook matches a
	completed 4473's serials back to invoice lines, and each gun needs its own
	4473 + per-serial bound-book disposition. A line with qty>1 — or multiple
	newline-joined serials — silently breaks that: the completed 4473 can't
	match the line and the sale stalls at form_4473_status=Pending (incident
	a past invoice: 2× CZ 75 rung on one line, serial_no "J4940\\n7805C").

	Enforce the invariant at the counter, on EVERY validate (Draft saves too),
	so the malformed line never persists — the cashier splits it while building
	the cart, not after the 4473 already failed to sync. Non-firearm lines (ammo,
	accessories) stack freely; antiques are GCA-exempt and out of scope
	(is_regulated_firearm_item excludes them).

	Hooked on `validate` of POS Invoice — the retail-to-individual counter path
	where the incident occurred. The web-order path books per-unit serial rows
	elsewhere (ffl_woo_sync fulfillment), so it doesn't ring firearms on POS
	lines and is intentionally out of this gate's scope.
	"""
	for row in getattr(doc, "items", None) or []:
		if not is_regulated_firearm_item(row.item_code):
			continue
		serials = split_serials(getattr(row, "serial_no", None))
		qty = flt(getattr(row, "qty", 0))
		if qty > 1 or len(serials) > 1:
			n = max(int(qty) if qty > 0 else 0, len(serials), 2)
			frappe.throw(
				_("Row #{0} ({1}) rings {2} firearms on one line. Each firearm must "
					"be its own line with quantity 1 so it gets its own 4473 and "
					"bound-book entry. Remove this line and add {2} separate lines, "
					"one gun per line.").format(row.idx, row.item_code, n),
				title=_("One Firearm Per Line"),
			)


def _how_to_clear_4473() -> str:
	"""The 'what do I do about it' half of the gate's refusal, in this store's terms.

	The gate is the same either way — a firearm does not leave without a recorded
	4473 and NICS — but the two configurations clear it by completely different
	actions, and naming the wrong one is how a cashier ends up staring at a button
	that will never arrive.

	The FastBound wording also has to name the way OUT of a dead end: a store that
	has switched FastBound off but not yet switched Paper 4473 Mode on cannot
	satisfy this gate by any counter action at all. Telling it to "wait for the
	webhook" would be advice to wait forever, so the setting to change is in the
	message.
	"""
	if paper_4473_mode():
		return _(
			"This store is in Paper 4473 Mode: complete the paper form, run the "
			"background check, then click 'Record Paper 4473' on the payment screen.")
	return _(
		"Click FastBound 4473, complete the form in FastBound, and wait for the webhook "
		"to mark this order as Completed. If this store keeps PAPER 4473s instead, "
		"switch on FFL Settings → Paper 4473 Mode and the counter gets a "
		"'Record Paper 4473' button instead of this one.")


def require_4473_and_nics_for_firearm_sale(doc, method: str | None = None) -> None:
	"""Reject SI / POS Invoice submissions that move a firearm without 4473 + NICS Proceed.

	Hooked on `validate` of Sales Invoice / POS Invoice (the points where stock
	leaves the building to an individual). Sales Order doesn't trigger this
	check — pre-orders and FFL-to-FFL transfers (dealer portal) don't require
	4473/NICS. The stock-leaving event is what matters.

	**Gate fires only at submit time** (docstatus transitioning to 1). The
	POS hold state — Draft with form_4473_status="Pending" while FastBound
	collects the 4473 — must NOT raise on every Draft save, or the cashier
	can't even build the cart for a firearm sale.

	The doc carries custom fields `form_4473_status` and `nics_status`;
	these are populated by the FastBound integration's webhook handler
	(`ffl_integrations.fastbound.webhook`) once 4473 is signed (form4473.completed)
	and the disposition is committed in FastBound UI (disposition.committed,
	which implies NICS came back Proceed).
	"""
	if not _has_firearm_line(doc):
		return

	# Only block at actual submit — draft saves stay open so the POS hold
	# flow can advance (serial entry, FastBound disposition creation, etc.)
	# before the cashier hits Complete Order.
	if getattr(doc, "docstatus", 0) != 1:
		return

	# Consolidated Sales Invoices roll up POS Invoices that already cleared
	# the gate; the SI doesn't carry form_4473_status itself, so checking
	# it would always fail. Trust the source POS Invoices.
	if getattr(doc, "is_consolidated", 0):
		return

	# Shipped to a destination FFL: the gun is mailed to an FFL where the buyer
	# completes the 4473 + NICS — we don't run that check here. The bound-book
	# exit is recorded as an FFL/Dealer Transfer disposition by
	# dealer_transfer.create_dealer_dispositions. getattr keeps this a no-op on
	# POS Invoice (no such field → retail gate unchanged); applies to both
	# individual and dealer buyers, so it sits ahead of the is_ffl_dealer skip.
	if (getattr(doc, "ffl_destination_ffl", None) or "").strip():
		return

	# FFL-to-FFL transfer: selling a firearm to another licensed dealer is NOT
	# a transfer to a non-licensee, so no 4473 / NICS is required (the bound-book
	# entry is recorded via an FFL Disposition of type FFL/Dealer Transfer). The
	# buyer being an FFL dealer is the signal. Retail sales to individuals go
	# through POS, which carries the 4473/NICS fields and is gated normally.
	customer = getattr(doc, "customer", None)
	if customer and frappe.db.get_value("Customer", customer, "is_ffl_dealer"):
		return

	# In-store pickup: a web order whose receiving FFL is our own licence. The
	# buyer collects at the counter and completes a retail 4473 here, so there is
	# no destination FFL to skip on — and a Sales Invoice has no 4473 fields of
	# its own to satisfy the check below. Read the proof where that 4473 records
	# it instead: a committed Retail Sale disposition per serial on this order.
	if getattr(doc, "doctype", None) == "Sales Invoice" and _web_retail_4473_covers(doc):
		return

	# Counter pickup with a gun ordered to the store (api/counter_pickup): this
	# invoice moves NO stock (update_stock=0 — the gun is not even here yet);
	# it is handed over later on its own POS Invoice, which runs the 4473 and
	# is gated here in full. Both halves are required: the marker alone must
	# not let a stock-moving invoice through.
	if getattr(doc, "doctype", None) == "Sales Invoice" and _is_counter_pickup(doc) \
			and not int(getattr(doc, "update_stock", 0) or 0):
		return

	form_status = (getattr(doc, "form_4473_status", None) or "").strip()
	nics_status = (getattr(doc, "nics_status", None) or "").strip()

	if form_status != FORM_4473_REQUIRED:
		frappe.throw(
			_("Cannot submit firearm transfer: 4473 form status is '{0}', must be '{1}'. {2}")
				.format(form_status or "(missing)", FORM_4473_REQUIRED, _how_to_clear_4473()),
			title=_("4473 Required"),
		)

	if nics_status not in NICS_PROCEED_STATUSES:
		frappe.throw(
			_("Cannot submit firearm transfer: NICS status is '{0}', must be 'Proceed'. {1}")
				.format(nics_status or "(missing)", _how_to_clear_4473()),
			title=_("NICS Proceed Required"),
		)

	# The doc-level flags above can't catch a firearm added — or a serial
	# swapped — AFTER the 4473 completed (the flags stay Completed/Proceed).
	# Verify every firearm serial on a POS Invoice has its OWN committed
	# disposition for THIS order, so each gun clears its own 4473 + NICS.
	if getattr(doc, "doctype", None) == "POS Invoice":
		_require_disposition_coverage(doc)


def _is_counter_pickup(doc) -> bool:
	"""api/counter_pickup.is_pickup_order — imported late: that module imports
	from this package's neighbours, and the gate must stay importable first."""
	from ffl_core.api.counter_pickup import is_pickup_order
	return is_pickup_order(doc)


def _covered_serials(link: dict, serials) -> set[str]:
	"""The subset of `serials` proved by a committed Retail Sale FFL Disposition
	hanging off the document named in `link`.

	SINGLE SOURCE OF TRUTH for firearm-serial coverage — these criteria live here
	ONCE and are shared by every caller: the POS submit hard-gate
	(_require_disposition_coverage), the web retail gate (_web_retail_4473_covers),
	and the webhook's early mismatch alarm
	(ffl_integrations.fastbound.webhook._alert_invoice_serial_coverage). The alarm
	is just an earlier preview of the gate, and the web and counter paths must
	demand the SAME proof, so the only thing a caller may vary is WHICH document
	the disposition is linked to. Only a Retail Sale disposition (what the webhook
	stamps Completed/Proceed) proves a 4473 for THIS buyer; a non-sale disposition
	carrying those fields off-form must not satisfy any of them.
	"""
	serials = list(serials)
	if not serials:
		return set()
	return set(frappe.get_all(
		"FFL Disposition",
		filters={
			**link,
			"serial": ("in", serials),
			"disposition_type": "Retail Sale",
			"form_4473_status": FORM_4473_REQUIRED,
			"nics_status": ("in", list(NICS_PROCEED_STATUSES)),
			# Submitted only. A draft is not in the bound book yet and a cancelled
			# one has been voided — neither proves a 4473. Without this, cancelling
			# a disposition still let the invoice submit, and a manager's 4473 redo
			# (ffl_integrations.fastbound.pos_4473_recovery.manager_redo_4473) could
			# be undone by the counter's Refresh button reading the voided rows back
			# as coverage.
			"docstatus": 1,
		},
		pluck="serial",
	))


def regulated_firearm_serials(rows) -> list[str]:
	"""Every regulated-firearm serial on these invoice lines — one entry per gun,
	order kept. The one "which guns does this sale need a 4473 for" read shared by
	the submit gate, the web-pickup gate, the webhook's early mismatch alarm and
	the 4473 recovery actions, so they can't drift.

	Reads both line storages: newline-joined ``serial_no`` (qty>1, split by the
	canonical ``split_serials``) and, when that is blank, the v16 Serial and Batch
	Bundle via ``serials_for_line`` — a bundle-only line must not look gun-less.
	getattr, not row.get, so plain-object rows read too. Antiques are GCA-exempt
	and never need a 4473."""
	out: list[str] = []
	for row in rows or []:
		if not is_regulated_firearm_item(row.item_code):
			continue
		serial_no = getattr(row, "serial_no", None)
		if serial_no:
			out.extend(split_serials(serial_no))
		elif getattr(row, "serial_and_batch_bundle", None):
			out.extend(serials_for_line(row))
	return list(dict.fromkeys(out))


def covered_serials_for_invoice(invoice_name: str, serials) -> set[str]:
	"""Coverage proved against a POS Invoice — the counter path."""
	return _covered_serials({"linked_pos_invoice": invoice_name}, serials)


def covered_serials_for_web_order(woo_online_order: str, serials) -> set[str]:
	"""Coverage proved against a Woo Online Order — the in-store pickup path.

	A pickup's 4473 is an ordinary retail 4473 run at the counter, so its proof is
	the same Retail Sale disposition the counter path produces. It just hangs off
	the web order rather than a POS Invoice, because a sale already paid on
	WooCommerce never gets rung up a second time at the register.
	"""
	return _covered_serials({"woo_online_order": woo_online_order}, serials)


def _web_retail_4473_covers(doc) -> bool:
	"""True when every regulated firearm serial on this Sales Invoice is proved by
	a committed Retail Sale disposition on the invoice's own web order.

	Why the gate needs this at all: ``form_4473_status`` / ``nics_status`` are
	custom fields on **POS Invoice** — Sales Invoice carries neither. So for an SI
	the gate could only ever be satisfied by the ``ffl_destination_ffl`` escape,
	i.e. by the gun being mailed to a dealer. An in-store pickup has no destination
	FFL by definition, which left such an order permanently unsubmittable: its
	revenue and its sales-tax liability simply could not be booked (web order
	2026-08-15).

	This does not weaken the gate — it reads the proof where a web retail 4473
	actually records it. That proof is strong: FFL Disposition's
	``_reject_manual_retail_sale`` refuses a hand-made Retail Sale unless it
	carries a FastBound id (so it came from the 4473 webhook) or an audited
	manager override, and :func:`_covered_serials` still demands submitted +
	Completed + Proceed, per serial. A gun added or a serial swapped after the fact
	is uncovered and still blocks the submit.
	"""
	order = (getattr(doc, "woo_online_order", None) or "").strip()
	if not order:
		return False
	needed = set(regulated_firearm_serials(getattr(doc, "items", None)))
	if not needed:
		return False
	return not (needed - covered_serials_for_web_order(order, needed))


def _require_disposition_coverage(doc) -> None:
	"""Reject submit unless every firearm row's serial has a committed FFL
	Disposition (4473 Completed + NICS Proceed) linked to this POS Invoice.

	Closes the bypass where a second firearm is added — or a row's serial is
	changed — after the document-level 4473 status already flipped to Completed.
	"""
	# A qty>1 line carries its serials newline-joined — split so each gun is
	# checked for its OWN disposition, never the raw "J4940\n7805C" string.
	needed = set(regulated_firearm_serials(getattr(doc, "items", None)))
	if not needed:
		return

	covered = covered_serials_for_invoice(doc.name, needed)
	missing = sorted(needed - covered)
	if missing:
		frappe.throw(
			_("Cannot submit: firearm serial(s) {0} have no completed 4473 with NICS "
				"Proceed on this order. Each firearm must clear its own 4473 first.").format(
				", ".join(missing)
			),
			title=_("4473 Coverage Required"),
		)


def _has_firearm_line(doc) -> bool:
	# Regulated firearms only — antiques (GCA-exempt) need no 4473/NICS, so an
	# antique-only sale skips this gate entirely.
	for row in getattr(doc, "items", None) or []:
		if is_regulated_firearm_item(row.item_code):
			return True
	return False
