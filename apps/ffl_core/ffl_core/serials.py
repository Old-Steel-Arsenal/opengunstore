"""Shared serial-number extraction for transaction lines.

ERPNext stores a line's serials as either a newline-separated string in
row.serial_no (use_serial_batch_fields=1) or a Serial and Batch Bundle
reference (v16 default). Every consumer of per-line serials — acquisitions on
Purchase Receipt, dealer-transfer dispositions on SI/DN — must handle both,
so the dual-path lives here once.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

import frappe
from frappe import _


# ------------------------------------------------------------ ledger serial vs key
#
# A Serial No's NAME is the POS's internal key for one physical gun. For almost
# every gun it IS the bound-book serial, and nothing below changes that. Two kinds
# of gun can't be named by their serial, because ERPNext names Serial Nos uniquely
# (case-insensitively): guns the bound book records with no serial ("NONE MARKED",
# "NSN", ...) and two different guns that carry the same serial. Those get a TAGGED
# key, "<serial> (FB 01074)" or "<serial> (POS 0003)", and the serial exactly as the
# bound book writes it lives in Serial No.ledger_serial.
#
# Rules every caller relies on:
#   * read the bound-book serial only through ledger_serial()/ledger_serials()/
#     ledger_serial_text() — it falls back to the name, so an untagged gun reads
#     exactly what it always did;
#   * mint a key only through allocate_serial_key();
#   * an untagged gun's name == its ledger serial; a tagged gun's name starts
#     with its ledger serial (cleaned of separator characters) followed by " (".

DEFAULT_PLACEHOLDERS = ("NONE", "NONE MARKED", "NSN", "NO SERIAL", "N/A", "UNKNOWN")
NO_SERIAL_TEXT = "NO SERIAL"
# Characters a KEY may not carry: ERPNext splits serial text on commas and line
# breaks, "::" separates the Woo/GB SKU, "|" the price-approval key, and the rest
# break desk URLs or Frappe's name validation. An untagged gun keeps its serial as
# its name even when it holds one of the URL-unfriendly ones (it always has); only a
# comma or line break forces a tag, since such a name cannot work at all.
_KEY_UNSAFE = re.compile(r"[,\r\n|#/?%<>]|::")
_NAME_UNSAFE = re.compile(r"[,\r\n<>]")
# lazy: a ledger longer than this is cut in a tagged key (the name column is 140);
# the tag survives, the invariant "key starts with the ledger" then holds for the cut text only.
_KEY_BASE_MAX = 100
_POS_SERIES = "SNTAG-"
# The shape allocate_serial_key gives a tagged key: "<ledger> (FB <number>)" or
# "<ledger> (POS <number>)", the tag last. Only the FB / POS prefix inside the final
# parentheses counts, so a real serial that merely ends in parentheses ("A123 (2)")
# does not look like a key. Case-blind, as typed text may be.
_TAG_SHAPE = re.compile(r"^.*\S \((?:FB|POS) [^()\s][^()]*\)$", re.IGNORECASE)


def _norm(text: str | None) -> str:
	"""Comparison form of a serial: case-folded, dots dropped, runs of space collapsed."""
	return " ".join(str(text or "").replace(".", "").casefold().split())


def same_serial(a: str | None, b: str | None) -> bool:
	"""Two ledger serials name the same number (case and spacing aside)."""
	return " ".join(str(a or "").casefold().split()) == " ".join(str(b or "").casefold().split())


def placeholder_serials() -> set[str]:
	"""Normalised "no serial" spellings: the defaults plus FFL Settings › Serial placeholders."""
	# Meta-guarded: this code is live before `bench migrate` adds the field.
	extra = frappe.db.get_single_value("FFL Settings", "serial_placeholders") \
		if frappe.get_meta("FFL Settings").has_field("serial_placeholders") else None
	words = list(DEFAULT_PLACEHOLDERS) + (extra or "").splitlines()
	return {_norm(w) for w in words if _norm(w)}


def is_placeholder(ledger: str | None) -> bool:
	"""True for a blank serial or one of the bound book's "no serial" spellings."""
	norm = _norm(ledger)
	return not norm or norm in placeholder_serials()


def key_base(ledger: str | None) -> str:
	"""The ledger serial as it may appear at the front of a tagged key."""
	base = " ".join(_KEY_UNSAFE.sub(" ", str(ledger or "")).split())
	return (base or NO_SERIAL_TEXT)[:_KEY_BASE_MAX].strip()


def allocate_serial_key(ledger: str | None, *, fb_item_number: str | None = None,
		force_tag: bool = False) -> str:
	"""The Serial No name for a NEW gun whose bound-book serial is `ledger`.

	The serial itself when it can be one: not a placeholder, no comma or line
	break, `force_tag` not set, and no Serial No already has that name. Otherwise a
	tagged key: "<ledger> (FB <item number>)" when the bound-book item number is
	known (stable across re-imports), else "<ledger> (POS 0001)" from a naming
	series. A tagged key that is somehow taken gets "-2", "-3", ... inside the tag.

	Callers that must NOT tag a collision (the importer meeting an in-stock gun it
	does not know) check existence themselves before calling."""
	raw = str(ledger or "").strip()
	if (raw and not force_tag and not is_placeholder(raw) and not _NAME_UNSAFE.search(raw)
			and not frappe.db.exists("Serial No", raw)):
		return raw
	from frappe.model.naming import getseries

	number = str(fb_item_number or "").strip()
	tag = f"FB {number}" if number else "POS " + getseries(_POS_SERIES, 4)
	base = key_base(raw)
	key, n = f"{base} ({tag})", 1
	while frappe.db.exists("Serial No", key):
		n += 1
		key = f"{base} ({tag}-{n})"
	return key


def _fields_ready() -> bool:
	"""The Serial No columns exist. This code is live before `bench migrate` adds them
	during a deploy; until then every gun reads as untagged (name = ledger serial)."""
	return frappe.db.has_column("Serial No", "ledger_serial")


def ledger_serial(name: str | None) -> str:
	"""The bound-book serial of one gun — what anything facing a customer, the ATF,
	FastBound or a sales channel must print. Falls back to the name (rows from
	before the field, non-firearm serials, a name with no Serial No)."""
	name = str(name or "").strip()
	if not name or not _fields_ready():
		return name
	return (frappe.db.get_value("Serial No", name, "ledger_serial") or "").strip() or name


def ledger_serials(names: Iterable[str]) -> dict[str, str]:
	"""Bulk ledger_serial: {name: ledger serial} in one query; unknown names map to themselves."""
	wanted = [n for n in dict.fromkeys(str(n or "").strip() for n in names) if n]
	if not wanted or not _fields_ready():
		return {n: n for n in wanted}
	found = dict(frappe.get_all("Serial No", filters={"name": ("in", wanted)},
		fields=["name", "ledger_serial"], as_list=True))
	# The DB matches names case-insensitively; key the answer by the caller's spelling.
	by_fold = {k.casefold(): (v or "").strip() or k for k, v in found.items()}
	return {n: by_fold.get(n.casefold()) or n for n in wanted}


def ledger_serial_text(serial_no_text: str | None, sep: str = ", ") -> str:
	"""A line's serial_no text (one key per line) as bound-book serials joined by `sep`."""
	from ffl_core.utils import split_serials

	keys = split_serials(serial_no_text)
	mapped = ledger_serials(keys)
	return sep.join(mapped.get(k, k) for k in keys)


def looks_like_tagged_key(text: str | None) -> bool:
	"""`text` has the shape of a key allocate_serial_key mints ("NSN (POS 0001)")."""
	return bool(_TAG_SHAPE.match(str(text or "").strip()))


def is_tagged(name: str | None) -> bool:
	"""The gun's name is not its bound-book serial (a no-serial or same-serial gun)."""
	name = str(name or "").strip()
	return bool(name) and not same_serial(name, ledger_serial(name))


def serial_label(name: str | None) -> str:
	"""Staff-facing description that tells same-serial guns apart: the ledger serial
	alone for an ordinary gun, else "<ledger> · <maker> <model> · FB#<number>"."""
	name = str(name or "").strip()
	if not _fields_ready():
		return name
	row = frappe.db.get_value("Serial No", name,
		["ledger_serial", "manufacturer", "model_name", "fastbound_item_number"], as_dict=True) if name else None
	if not row:
		return name
	ledger = (row.ledger_serial or "").strip() or name
	if same_serial(ledger, name):
		return ledger
	parts = [ledger, " ".join(p for p in (row.manufacturer, row.model_name) if p)]
	if row.fastbound_item_number:
		parts.append(f"FB#{row.fastbound_item_number}")
	return " · ".join(p for p in parts if p)


_RESOLVE_FIELDS = ["name", "ledger_serial", "item_code", "status", "warehouse",
	"fastbound_item_id", "fastbound_item_number", "no_serial"]


def resolve_serial(text: str | None, *, item_code: str | None = None,
		status: str | None = "Active") -> list[dict]:
	"""A serial a person or another system typed → every gun it may mean.

	A tagged key names one gun outright (fast path). Otherwise the answer is every
	Serial No whose ledger serial matches (case and spacing aside), plus the gun
	NAMED by the text when its ledger serial is not stamped yet. Callers decide:
	one → that gun, several → ask, none → not found. Never pick the first of many.

	Returns:
	    [{name, ledger_serial, label, item_code, status, warehouse,
	      fastbound_item_id, fastbound_item_number, no_serial}]
	"""
	text = str(text or "").strip()
	if not text or not _fields_ready():
		return []
	filters: dict = {}
	if item_code:
		filters["item_code"] = item_code
	if status:
		filters["status"] = status

	exact = frappe.db.get_value("Serial No", text, _RESOLVE_FIELDS, as_dict=True)
	if exact and not same_serial(exact.name, (exact.ledger_serial or "").strip() or exact.name):
		rows = [exact]
	else:
		spaced = " ".join(text.split())
		rows = frappe.get_all("Serial No", fields=_RESOLVE_FIELDS,
			filters={**filters, "ledger_serial": ("in", list({text, spaced}))}, order_by="creation asc")
		if exact and exact.name not in {r.name for r in rows}:
			rows.append(exact)
	out = []
	for r in rows:
		if item_code and r.item_code != item_code:
			continue
		if status and r.status != status:
			continue
		r = dict(r)
		r["ledger_serial"] = (r.get("ledger_serial") or "").strip() or r["name"]
		r["label"] = serial_label(r["name"])
		out.append(r)
	return out


# ------------------------------------------------------------ temporary gates
# Until the receipts, agreements, 4473 records and the sales channels learn to
# print the ledger serial (instead of a tagged key), a tagged gun may not be
# listed online or sold. Each gate is ONE function so lifting it is one change.

def listing_block_reason(serial_no: str | None) -> str | None:
	"""Why a gun cannot be listed on a sales channel yet, or None. Tagged guns only."""
	if not is_tagged(serial_no):
		return None
	return _("{0} shares its serial with another gun or has no serial number, so it "
		"can't be listed online yet. Sell it at the counter once that is supported."
	).format(serial_no)


def refuse_tagged_serial_sale(doc, method: str | None = None) -> None:
	"""validate hook (POS Invoice / Sales Invoice / Delivery Note): refuse selling or
	shipping out a tagged gun. Returns pass, and so do stock moves that are not a sale
	or shipment (Stock Entry). Sales Order lines carry no serial, so it is not hooked there."""
	if doc.get("is_return"):
		return
	keys = [k for line in serials_for_lines(doc.get("items") or []) for k in line]
	if not keys:
		return
	tagged = [k for k, ledger in ledger_serials(keys).items() if not same_serial(k, ledger)]
	if tagged:
		frappe.throw(_("{0} can't be sold yet: it shares its serial with another gun or "
			"has no serial number, and receipts and 4473 records can't show it correctly "
			"yet. Take it off this sale; it stays in stock.").format(", ".join(tagged)),
			title=_("Gun not sellable yet"))


def only_active_serial(item_code: str | None) -> str | None:
	"""The Item's Serial No when it has exactly ONE Active serial, else None.

	The A&D auto-fill rule shared by FFL Acquisition and FFL Disposition: a
	blank serial on a single-gun Item is stamped server-side too (defence in
	depth for REST/script callers that bypass the form's auto-pick)."""
	if not item_code:
		return None
	active = frappe.get_all(
		"Serial No",
		filters={"item_code": item_code, "status": "Active"},
		pluck="name", limit=2,
	)
	return active[0] if len(active) == 1 else None


def serials_for_line(row) -> list[str]:
	# Legacy path: newline-separated string in row.serial_no
	if row.get("serial_no"):
		return [s.strip() for s in (row.serial_no or "").splitlines() if s.strip()]

	# v16 Bundle path
	if row.get("serial_and_batch_bundle"):
		bundle = frappe.get_doc("Serial and Batch Bundle", row.serial_and_batch_bundle)
		return [e.serial_no for e in bundle.entries if e.serial_no]

	return []


def serials_for_lines(rows) -> list[list[str]]:
	"""``serials_for_line`` for many rows, with ONE query for all their bundles —
	a report over years of v16 lines must not get_doc each bundle. Same answer
	per row, in input order; a bundle that no longer exists reads as no serials."""
	bundles = {r.get("serial_and_batch_bundle") for r in rows
		if not r.get("serial_no") and r.get("serial_and_batch_bundle")}
	by_bundle: dict[str, list[str]] = {}
	if bundles:
		for parent, serial in frappe.db.sql("""
				select parent, serial_no from `tabSerial and Batch Entry`
				where parent in %(bundles)s and ifnull(serial_no, '') != ''
				order by parent, idx""", {"bundles": tuple(bundles)}):
			by_bundle.setdefault(parent, []).append(serial)
	return [serials_for_line(r) if r.get("serial_no")
		else by_bundle.get(r.get("serial_and_batch_bundle") or "", []) for r in rows]


def receipts_of(serial: str) -> list:
	"""The gun's standing receipts — submitted Purchase Receipts that took it IN,
	not purchase returns nor a receipt's rejected guns — latest first, each with
	the row (voucher_detail_no) and its posting_datetime. Read off the Serial and
	Batch Bundles, which every submitted receipt row carries whatever its serial
	storage."""
	return frappe.db.sql("""
		select b.voucher_no, b.voucher_detail_no, b.posting_datetime
		from `tabSerial and Batch Bundle` b
		join `tabSerial and Batch Entry` e on e.parent = b.name
		where e.serial_no = %s and b.voucher_type = 'Purchase Receipt'
			and b.type_of_transaction = 'Inward' and b.is_rejected = 0
			and b.docstatus = 1 and b.is_cancelled = 0
		order by b.posting_datetime desc, b.creation desc""", serial, as_dict=True)


def receipt_serials(receipt: str) -> dict[str, str]:
	"""Every serial a receipt took in (its rejected guns aside) → its item code,
	the receipt standing or cancelled: a cancel marks the row's bundle cancelled
	and unlinks it from the row (StockController.delete_auto_created_batches),
	but the bundle keeps its voucher and its entries."""
	return dict(frappe.db.sql("""
		select e.serial_no, b.item_code
		from `tabSerial and Batch Bundle` b
		join `tabSerial and Batch Entry` e on e.parent = b.name
		where b.voucher_type = 'Purchase Receipt' and b.voucher_no = %s
			and b.type_of_transaction = 'Inward' and b.is_rejected = 0
			and ifnull(e.serial_no, '') != ''""", receipt))
