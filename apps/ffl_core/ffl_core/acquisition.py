"""Single chokepoint for creating FFL Acquisition records.

Three callers route through here:
  - ffl_core.api.firearm_intake (consumer trade-ins)
  - ffl_core.compliance.purchase (Purchase Receipt firearm lines)
  - ffl_integrations.fastbound.importer (one-time bound-book import)

Idempotent at (item_code, serial) WITHIN an ownership cycle: a second call
with the same pair returns the existing acquisition's name without inserting
a duplicate — until a submitted disposition closes the cycle, after which the
same pair books a fresh record (a returned gun is a reacquisition; see
_existing).
"""
from __future__ import annotations

from datetime import datetime

import frappe
from frappe import _
from frappe.utils import now_datetime


INDIVIDUAL_SOURCES: frozenset[str] = frozenset({"Individual", "Trade-in", "Estate", "Pawn"})
# The trade-in dialog's pay-now buy is a Receive Goods receipt with this Source (api/trade_in._buy_now); its
# acquisitions keep source_type "Trade-in" (compliance/purchase.py), and Inventory Receipts files it as a trade-in.
TRADE_IN_SOURCE = "Trade-in"


def is_trade_in_source(receive_source: str | None) -> bool:
	"""Whether a Receive Goods Source (free text from FFL Settings) is TRADE_IN_SOURCE, however it is spelled."""
	return (receive_source or "").strip().lower() == TRADE_IN_SOURCE.lower()


def record_firearm_acquisition(
	*,
	item_code: str,
	serial: str,
	source_type: str,
	source_name: str,
	cost: float,
	company: str,
	source_ffl: str | None = None,
	source_id_type: str | None = None,
	source_id_number: str | None = None,
	source_customer: str | None = None,
	source_id_document: str | None = None,
	source_first_name: str | None = None,
	source_middle_name: str | None = None,
	source_last_name: str | None = None,
	source_suffix: str | None = None,
	purchase_receipt: str | None = None,
	purchase_order: str | None = None,
	source_address: str | None = None,
	receive_source: str | None = None,
	transaction_type: str | None = None,
	transfer_for_customer: str | None = None,
	notes: str | None = None,
	acquisition_date: datetime | None = None,
	fastbound_id: str | None = None,
	submit_now: bool = True,
	pending_intake_payload: str | None = None,
	ledger_serial: str | None = None,
) -> str:
	"""Validate, insert, and (by default) submit one FFL Acquisition. Returns its name.

	Raises ValidationError on the source-type matrix:
	  - source_type='FFL Dealer'   → source_ffl required
	  - source_type ∈ INDIVIDUAL   → source_id_type + source_id_number required

	acquisition_date defaults to now_datetime() so historical sources (FastBound
	bound book) can back-date the record to the original acquisition date.

	submit_now=False parks the record as a DRAFT — the deferred-booking trade-in
	path: the gun must not hit the bound book (submit triggers the FastBound
	push) until the POS checkout actually completes. `pending_intake_payload`
	carries the per-unit stamp data (JSON) the booking step needs then.

	`serial` is the gun's KEY (its Serial No name); `ledger_serial` is the serial
	as the bound book writes it, defaulting to the key's own ledger serial. A bare
	"no serial" spelling (NONE, NSN, ...) is refused as a key: two such guns would
	collapse into one record here. Keys come from ffl_core.serials.allocate_serial_key.
	"""
	from ffl_core import serials as _serials

	_validate_source_matrix(source_type, source_ffl, source_id_type, source_id_number)
	key = (serial or "").strip()
	if key and _serials.is_placeholder(key):
		frappe.throw(_("{0} is a \"no serial number\" entry, not a serial. Each such gun "
			"needs its own key (for example \"{0} (POS 0001)\") so two of them are never "
			"booked as one gun.").format(key), title=_("Serial Key Needed"))
	ledger = (ledger_serial or "").strip() or (_serials.ledger_serial(key) if key else "")

	existing = _existing(item_code, serial)
	if existing:
		other_fb = (frappe.db.get_value("FFL Acquisition", existing, "fastbound_id") or "").strip()
		if (fastbound_id or "").strip() and other_fb and other_fb != fastbound_id.strip():
			# Two different bound-book entries arriving on one key is a key-allocation
			# bug upstream; collapsing them would lose a gun from the book.
			frappe.throw(_("{0} {1} already records serial {2} for FastBound entry {3}; a second "
				"entry ({4}) can't be booked on the same gun. Nothing was recorded.").format(
				_("FFL Acquisition"), existing, serial, other_fb, fastbound_id),
				title=_("Two Bound-Book Entries, One Gun"))
		return existing

	doc = frappe.get_doc({
		"doctype": "FFL Acquisition",
		"acquisition_date": acquisition_date or now_datetime(),
		"source_type": source_type,
		"receive_source": receive_source,
		"transaction_type": transaction_type,
		"transfer_for_customer": transfer_for_customer,
		"source_name": source_name,
		"source_address": source_address,
		"source_ffl": source_ffl if source_type == "FFL Dealer" else None,
		"source_id_type": source_id_type if source_type in INDIVIDUAL_SOURCES else None,
		"source_id_number": source_id_number if source_type in INDIVIDUAL_SOURCES else None,
		"source_customer": source_customer if source_type in INDIVIDUAL_SOURCES else None,
		"source_id_document": source_id_document if source_type in INDIVIDUAL_SOURCES else None,
		"source_first_name": source_first_name if source_type in INDIVIDUAL_SOURCES else None,
		"source_middle_name": source_middle_name if source_type in INDIVIDUAL_SOURCES else None,
		"source_last_name": source_last_name if source_type in INDIVIDUAL_SOURCES else None,
		"source_suffix": source_suffix if source_type in INDIVIDUAL_SOURCES else None,
		"company": company,
		"item": item_code,
		"serial": serial,
		"ledger_serial": ledger or None,
		"cost": cost,
		"purchase_receipt": purchase_receipt,
		"purchase_order": purchase_order,
		"notes": notes,
		"fastbound_id": fastbound_id,
		"fastbound_synced_at": now_datetime() if fastbound_id else None,
		"pending_intake_payload": pending_intake_payload,
	})
	doc.flags.ignore_permissions = True
	doc.insert()
	if submit_now:
		doc.submit()
	if key and ledger and frappe.db.exists("Serial No", key) and not (
			frappe.db.get_value("Serial No", key, "ledger_serial") or "").strip():
		# Receipts create Serial Nos by bulk insert (no hooks), so the first record
		# of a new gun stamps its bound-book serial.
		frappe.db.set_value("Serial No", key, "ledger_serial", ledger, update_modified=False)
	return doc.name


def _validate_source_matrix(
	source_type: str,
	source_ffl: str | None,
	source_id_type: str | None,
	source_id_number: str | None,
) -> None:
	if source_type == "FFL Dealer" and not (source_ffl or "").strip():
		frappe.throw(_("Source FFL number is required for source type 'FFL Dealer'."))
	if source_type in INDIVIDUAL_SOURCES and not (
		(source_id_type or "").strip() and (source_id_number or "").strip()
	):
		frappe.throw(_("ID type and ID number are required for source type '{0}'.").format(source_type))


def _existing(item_code: str, serial: str) -> str | None:
	"""The CURRENT ownership cycle's acquisition for this unit, if any.

	Dedupe guard: the PR on_submit hook and the receive API can both call
	record_firearm_acquisition for the same unit, so an OPEN-cycle duplicate
	must collapse to the existing record. But an acquisition whose gun has
	since been DISPOSED belongs to a closed cycle — a new receive of that
	serial is a REacquisition (dealer return, consignment return) and must
	book fresh. Matching it here silently swallowed the JV-return rifles'
	re-acquisitions (2026-08-21 incident: three rifles re-entered stock with
	no acquisition record and nothing said a word).

	The closing disposition is matched by SERIAL alone, not item: a Serial No
	is unique by name, so a same-serial disposition under a different item is
	the same physical gun after an item reassignment (the Mosin two-Items
	pattern), and its dispose still closes this cycle. A cancelled disposition
	(docstatus 2) means the gun never left — the cycle stays open. A BLANK
	serial can't be cycle-judged at all (it would match other guns' blank-
	serial dispositions), so it keeps the plain pre-cycle dedupe.
	"""
	from ffl_core.firearm import on_book_acquisitions

	# An acquisition marked [NOT A&D] (an antique booked by mistake) is no stay at all:
	# an antique sale writes no disposition, so counting it would refuse the gun's
	# return as a trade-in forever.
	row = next(iter(on_book_acquisitions(
		{"item": item_code, "serial": serial},
		["name", "acquisition_date"],
		# creation tiebreaks equal acquisition_dates (date-only backfills,
		# same-second double-writes) — without it MariaDB picks a row at
		# random and could hand back the CLOSED cycle's record.
		order_by="acquisition_date desc, creation desc",
	)), None)
	if not row:
		return None
	if not (serial or "").strip():
		return row.name
	closed = frappe.db.exists(
		"FFL Disposition",
		{"serial": serial, "docstatus": 1,
			"disposition_date": (">=", row.acquisition_date)},
	)
	return None if closed else row.name
