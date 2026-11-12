"""Card readers — which card device a terminal charge goes to.

A store with one card device keeps it as the processor's single configured device (Payroc:
``Payroc Settings.device_serial_number``) and nothing here changes anything. A store with several
lists them in the processor's settings Single, table ``card_readers`` (child doctype Card Reader:
name, device serial number, enabled); every computer then picks its own once and remembers it
(the browser's localStorage), and every terminal charge names that reader. The core resolves the
name to the device serial here, on the server: the browser never sends a serial number.

Rules:
  * no rows                       → the processor's own device, exactly as before (``None``)
  * rows, a terminal charge with no reader named → refused (:class:`CardReaderRequired`): never
    quietly fall back to some device — it may be on another counter
  * rows, an unknown or disabled reader → refused (:class:`CardReaderUnavailable`); the browser
    forgets its choice and asks again
  * a keyed charge (the processor's payment page) uses no device and needs no reader

A charge already started stays with its own device: poll / cancel / resume go by the handle the
processor gave it, and the record keeps the reader's name (``card_reader``) for the books.
"""
from __future__ import annotations

import frappe
from frappe import _

DOCTYPE = "Card Reader"
TABLE_FIELD = "card_readers"


class CardReaderRequired(frappe.ValidationError):
	"""Several card readers are set up and the request named none."""


class CardReaderUnavailable(frappe.ValidationError):
	"""The named card reader is not set up (or switched off) for the processor taking cards."""


def _has_table(settings_doctype: str | None) -> bool:
	if not settings_doctype:
		return False
	try:
		return bool(frappe.get_meta(settings_doctype).get_field(TABLE_FIELD))
	except Exception:  # noqa: BLE001 — a processor without a settings doctype has no readers
		return False


def readers(settings_doctype: str | None) -> list:
	"""Every card reader row of a processor's settings Single, in table order ([] when the
	processor keeps no table, or it is empty)."""
	if not _has_table(settings_doctype):
		return []
	return frappe.get_all(DOCTYPE, filters={"parenttype": settings_doctype, "parent": settings_doctype,
		"parentfield": TABLE_FIELD}, fields=["reader_name", "serial_number", "enabled"],
		order_by="idx asc")


def enabled_readers(settings_doctype: str | None) -> list:
	return [r for r in readers(settings_doctype) if r.enabled]


def resolve(processor, reader: str | None, *, keyed: bool = False) -> dict | None:
	"""The reader a new charge on ``processor`` goes to: {"name", "serial"}, or None when the
	processor keeps no readers (its own configured device) or the charge is keyed. Throws when
	readers are set up and ``reader`` is missing, unknown or switched off."""
	if keyed:
		return None
	rows = readers(processor.settings_doctype)
	reader = (reader or "").strip()
	if not rows:
		if reader:
			# A page loaded while readers were set up; the table has since been emptied.
			frappe.throw(_("Card reader {0} is no longer set up. Reload the page and charge again."
				).format(reader), CardReaderUnavailable, title=_("Card reader"))
		return None
	if not reader:
		frappe.throw(_("Choose this computer's card reader first. Reload the page if you are not "
			"asked which one."), CardReaderRequired,
			title=_("Card reader"))
	row = next((r for r in rows if (r.reader_name or "").strip().lower() == reader.lower()), None)
	if not row or not row.enabled:
		frappe.throw(_("Card reader {0} is not available. Choose this computer's card reader again."
			).format(reader), CardReaderUnavailable, title=_("Card reader"))
	return {"name": row.reader_name, "serial": (row.serial_number or "").strip()}


def start_kwargs(chosen: dict | None) -> dict:
	"""The extra argument a processor's ``start_charge`` takes for a chosen reader — passed only
	when there is one, so a processor without readers never sees it."""
	return {"device": chosen["serial"]} if chosen else {}


def boot_names(processor) -> list[str]:
	"""The enabled readers' names for the counter screens ([] = no reader choice at all)."""
	if not processor:
		return []
	return [r.reader_name for r in enabled_readers(processor.settings_doctype)]


def validate_table(doc) -> None:
	"""A settings Single's ``card_readers`` rows: names and serial numbers each used once."""
	names, serials = set(), set()
	for row in doc.get(TABLE_FIELD) or []:
		row.reader_name = (row.reader_name or "").strip()
		row.serial_number = (row.serial_number or "").strip()
		key = row.reader_name.lower()
		if key in names:
			frappe.throw(_("Card reader name {0} is used twice.").format(row.reader_name))
		if row.serial_number in serials:
			frappe.throw(_("Device serial number {0} is used twice.").format(row.serial_number))
		names.add(key)
		serials.add(row.serial_number)
