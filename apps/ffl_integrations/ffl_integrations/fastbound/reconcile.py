"""Per-gun FastBound reconcile: compare a Serial No's firearm identity fields to
the authoritative FastBound item, and apply an operator's per-field resolution.

Read side (compute_serial_fb_diff) is pure. Write side (apply_serial_fb_resolution,
Task 7) reuses push_serial_to_fastbound for POS→FB and the importer's brand/caliber
ensure helpers for FB→POS.
"""
from __future__ import annotations

import frappe
from frappe import _

from ffl_core import serials as _serials

from ffl_integrations.fastbound import normalize as _normalize
from ffl_integrations.fastbound.client import (
	FastBoundClient,
	FastBoundConfigError,
	_held_by_another_serial,
)
from ffl_integrations.fastbound.importer import (
	_ensure_brand,
	_ensure_caliber,
	_map_firearm_type,
)
from ffl_integrations.fastbound.tasks import push_serial_to_fastbound


def _blank_unknown(v: str | None) -> str:
	"""Fold the literal "Unknown" placeholder to blank (case-insensitive).

	client._firearm_item_fields injects "Unknown" for a blank required field
	(manufacturer / model / caliber) when pushing to FastBound, so FB carries
	"Unknown" where POS is effectively blank. That is NOT a real value: it must
	not read as a diff (nor be stamped back). Mirrors importer._stamp_serial_attrs,
	which already skips a case-insensitive "unknown" on the FB→POS path."""
	v = str(v or "").strip()
	return "" if v.lower() == "unknown" else v


def _canon_mfg(v):
	return _normalize.squash(_normalize.canon_manufacturer(_blank_unknown(v)))


def _canon_model(v):
	return _normalize.canon_model(_blank_unknown(v))


def _canon_caliber(v):
	return _normalize.canon_caliber(_blank_unknown(v))


def _pos_type(t):
	"""POS firearm_type for comparison: blank folds to "Other Firearm" (the
	acquisition push stores a blank firearm_type as "Other Firearm")."""
	return (t or "").strip() or "Other Firearm"


def _fb_type(t):
	"""FB item type mapped into OUR space via the authoritative importer mapper
	(_map_firearm_type — the same fn import uses to set Serial No.firearm_type),
	blank → "Other Firearm". So an imported "Frame" gun (POS "Receiver") canons
	equal to FB "Frame" — no perpetual type diff — while blank-POS ⇄ FB
	"Other Firearm" stays a no-diff."""
	return _map_firearm_type((t or "").strip()) or "Other Firearm"


# logical field, label, POS-side canonicalizer, FB-side canonicalizer.
# The four text fields use the SAME fn on both sides; `type` alone needs different
# canonicalizers — POS firearm_type is already in our space, while the FB type
# string is mapped FB→ours (_map_firearm_type) before comparing. "Unknown" folds
# to blank on manufacturer / model / caliber (see _blank_unknown). The FB item dict
# is keyed by these same logical names, so the field name doubles as the FB key.
_COMPARED = [
	("manufacturer", "Manufacturer", _canon_mfg, _canon_mfg),
	("importer", "Importer", _normalize.squash, _normalize.squash),
	("model", "Model", _canon_model, _canon_model),
	("caliber", "Caliber", _canon_caliber, _canon_caliber),
	("type", "Firearm Type", _pos_type, _fb_type),
]


def _pos_effective(serial: str) -> dict:
	"""POS effective firearm identity: Serial No value, falling back to the Item
	(the precedence the FastBound push uses). importer never falls back — the
	push sends it from the Serial No only (2026-08-20), so the diff must judge
	the same value or it would report an importer the push will never send."""
	sn = frappe.db.get_value(
		"Serial No", serial,
		["item_code", "manufacturer", "importer", "model_name", "caliber", "firearm_type"],
		as_dict=True,
	) or {}
	item = (frappe.db.get_value(
		"Item", sn.get("item_code"),
		["manufacturer", "model_name", "caliber", "firearm_type"],
		as_dict=True,
	) or {}) if sn.get("item_code") else {}
	return {
		"manufacturer": sn.get("manufacturer") or item.get("manufacturer") or "",
		"importer": sn.get("importer") or "",
		"model": sn.get("model_name") or item.get("model_name") or "",
		"caliber": sn.get("caliber") or item.get("caliber") or "",
		"type": sn.get("firearm_type") or item.get("firearm_type") or "",
	}


@frappe.whitelist()
def compute_serial_fb_diff(serial: str) -> dict:
	"""Diff a Serial No's firearm identity against its FastBound item (read-only).

	Args:
	    serial: Serial No name (needs Serial No read permission).

	Returns:
	    {status: "ok" | "not_in_fastbound" | "not_configured" |
	    "fb_missing" | "fb_disposed", serial, [fb_item_number], [in_sync],
	    [diffs: [{field, pos_value, fb_value}]], [serial_diff: {pos_value,
	    fb_value} or None]}. serial_diff compares the gun's ledger serial with
	    FastBound's (case and spacing aside); it is outside `diffs` because a
	    serial is changed with Correct Serial, not by picking a side here.
	"""
	frappe.has_permission("Serial No", "read", throw=True)
	serial = (serial or "").strip()
	if not serial:
		frappe.throw(_("No serial number provided."))
	frappe.has_permission("Serial No", "read", doc=serial, throw=True)
	fb_item_id = frappe.db.get_value("Serial No", serial, "fastbound_item_id")
	if not fb_item_id:
		return {"status": "not_in_fastbound", "serial": serial}
	try:
		client = FastBoundClient.from_settings()
	except FastBoundConfigError:
		return {"status": "not_configured", "serial": serial}
	item = client.get_item(fb_item_id)
	if not item:
		return {"status": "fb_missing", "serial": serial}
	if item.get("deleted") or FastBoundClient._item_is_disposed(item):
		return {"status": "fb_disposed", "serial": serial,
			"fb_item_number": item.get("itemNumber")}
	pos = _pos_effective(serial)
	diffs = []
	for field, label, pos_canon, fb_canon in _COMPARED:
		pos_val = (pos.get(field) or "").strip()
		fb_val = str(item.get(field) or "").strip()
		if pos_canon(pos_val) != fb_canon(fb_val):
			diffs.append({"field": field, "label": label,
				"pos_value": pos_val, "fb_value": fb_val})
	ledger = _serials.ledger_serial(serial)
	fb_serial = str(item.get("serial") or "").strip()
	serial_diff = None if _serials.same_serial(ledger, fb_serial) \
		else {"pos_value": ledger, "fb_value": fb_serial}
	return {"status": "ok", "serial": serial,
		"fb_item_number": item.get("itemNumber"),
		"in_sync": not diffs, "diffs": diffs, "serial_diff": serial_diff}


def _stage_fb_to_pos(field: str, fb_value: str, sn_updates: dict, skipped: list) -> bool:
	"""Translate a FastBound-wins value into a Serial No write, creating the
	Brand / Caliber link record as needed. Returns True iff a write was staged
	into sn_updates; False when the value was skipped (FB placeholder / blank type)."""
	fb_value = (fb_value or "").strip()
	# The "Unknown" placeholder (client._firearm_item_fields fills it in for a
	# blank required field) is not a real value — never stamp it back, which would
	# also mint a junk Brand "Unknown" / Caliber "Unknown". Mirrors the FB→POS
	# guard in importer._stamp_serial_attrs.
	if field in ("manufacturer", "model", "caliber") and fb_value.lower() == "unknown":
		skipped.append({"field": field, "reason": "fb_placeholder"})
		return False
	if field == "manufacturer":
		sn_updates["manufacturer"] = _ensure_brand(_normalize.canon_manufacturer(fb_value))
	elif field == "caliber":
		sn_updates["caliber"] = _ensure_caliber(fb_value)
	elif field == "model":
		sn_updates["model_name"] = fb_value
	elif field == "importer":
		sn_updates["importer"] = fb_value
	elif field == "type":
		# Map FB→ours with the authoritative importer mapper so e.g. FB "Frame"
		# becomes "Receiver" (matching how import set firearm_type). Only a truly
		# blank FB type maps to None — nothing to stamp, so skip it.
		ours = _map_firearm_type(fb_value)
		if not ours:
			skipped.append({"field": "type", "reason": "fb_type_blank"})
			return False
		sn_updates["firearm_type"] = ours
	return True


@frappe.whitelist()
def apply_serial_fb_resolution(serial: str, resolutions) -> dict:
	"""Apply the operator's per-field POS-vs-FastBound resolution choices.

	Re-computes the diff first: a field no longer differing is skipped
	(already_resolved); a field whose FB value changed since shown is
	skipped (changed); the rest apply against fresh FB values. FB-wins
	write the Serial No; any POS-win triggers exactly one
	push_serial_to_fastbound.

	Args:
	    serial: Serial No name (needs Serial No write permission).
	    resolutions: {field: {"choice": "pos"|"fb", "fb_value": <value
	        shown in the dialog>}} — dict or JSON string.

	Returns:
	    {ok, applied: {fb: [fields], pos: [fields]}, skipped: [{field,
	    reason}], fastbound_updated} — ok False with a reason when the
	    fresh diff isn't in "ok" state.
	"""
	frappe.has_permission("Serial No", "write", throw=True)
	serial = (serial or "").strip()
	if not serial:
		frappe.throw(_("No serial number provided."))
	frappe.has_permission("Serial No", "write", doc=serial, throw=True)
	if isinstance(resolutions, str):
		resolutions = frappe.parse_json(resolutions)
	resolutions = resolutions or {}

	fresh = compute_serial_fb_diff(serial)
	if fresh.get("status") != "ok":
		return {"ok": False, "reason": fresh.get("status"),
			"applied": {"fb": [], "pos": []}, "skipped": [], "fastbound_updated": False}
	fresh_by_field = {d["field"]: d for d in fresh["diffs"]}

	sn_updates: dict = {}
	applied_fb: list = []
	applied_pos: list = []
	skipped: list = []
	any_pos = False
	for field, res in resolutions.items():
		res = res or {}
		choice = res.get("choice")
		shown_fb = res.get("fb_value")
		cur = fresh_by_field.get(field)
		if not cur:
			skipped.append({"field": field, "reason": "already_resolved"})
			continue
		if shown_fb is None:
			skipped.append({"field": field, "reason": "no_shown_value"})
			continue
		if str(shown_fb) != str(cur["fb_value"]):
			skipped.append({"field": field, "reason": "changed"})
			continue
		if choice == "fb":
			if _stage_fb_to_pos(field, cur["fb_value"], sn_updates, skipped):
				applied_fb.append(field)
		elif choice == "pos":
			# A blank POS importer cannot "win": the push sends importer only
			# when the serial carries one (never a clearing empty — that would
			# wipe legitimate importers on guns booked directly in FastBound),
			# so the read-merge-write PUT keeps FastBound's value and the same
			# diff resurfaces on the next scan. Clearing a bound-book importer
			# is FastBound-side work; say so instead of reporting an apply
			# that didn't happen.
			if field == "importer" and not (cur.get("pos_value") or "").strip():
				skipped.append({"field": field, "reason": "pos_blank_importer"})
				continue
			any_pos = True
			applied_pos.append(field)
		else:
			skipped.append({"field": field, "reason": "no_choice"})

	if sn_updates:
		frappe.db.set_value("Serial No", serial, sn_updates, update_modified=True)
	fastbound_updated = False
	if any_pos:
		push_res = push_serial_to_fastbound(serial)
		fastbound_updated = bool(push_res.get("ok"))
	return {"ok": True, "applied": {"fb": applied_fb, "pos": applied_pos},
		"skipped": skipped, "fastbound_updated": fastbound_updated}


@frappe.whitelist()
def match_serial_to_fastbound(serial: str) -> dict:
	"""Find and stamp the FastBound item id for an unlinked serial.

	For a gun with no fastbound_item_id, finds its FastBound item by
	serial and stamps the id so it can be reconciled. Uses
	find_item_by_serial (raises on >1 in-stock match — surfaced to the
	operator).

	Args:
	    serial: Serial No name (needs Serial No write permission).

	Returns:
	    {ok: True, item_id} | {ok: True, already: True} | {ok: False,
	    reason: "not_found" | "not_configured"}.
	"""
	frappe.has_permission("Serial No", "write", throw=True)
	serial = (serial or "").strip()
	frappe.has_permission("Serial No", "write", doc=serial, throw=True)
	if frappe.db.get_value("Serial No", serial, "fastbound_item_id"):
		return {"ok": True, "already": True}
	try:
		client = FastBoundClient.from_settings()
	except FastBoundConfigError:
		return {"ok": False, "reason": "not_configured"}
	# FastBound knows the gun by its ledger serial; an item another gun already
	# holds is never this gun's.
	item = client.find_item_by_serial(_serials.ledger_serial(serial),
		skip=_held_by_another_serial(serial))
	if not item:
		return {"ok": False, "reason": "not_found"}
	item_id = item.get("id") or item.get("itemId")
	frappe.db.set_value("Serial No", serial, "fastbound_item_id", item_id, update_modified=False)
	return {"ok": True, "item_id": item_id}


@frappe.whitelist()
def scan_in_stock_fb_diffs() -> dict:
	"""Kick off a background POS-vs-FastBound diff scan of in-stock firearms.

	Scans every in-stock firearm with a fastbound_item_id. The result
	(which guns differ) is pushed via realtime (event 'fb_diff_scan_done')
	+ a Notification Log when the job finishes.

	Returns:
	    {"ok": True, "started": True} — the scan itself is asynchronous.
	"""
	frappe.has_permission("Serial No", "read", throw=True)
	frappe.enqueue(
		"ffl_integrations.fastbound.reconcile._run_scan_in_stock_fb_diffs",
		queue="long", timeout=1800, user=frappe.session.user,
	)
	return {"ok": True, "started": True}


def _run_scan_in_stock_fb_diffs(user: str) -> None:
	serials = frappe.get_all(
		"Serial No",
		filters={"status": "Active", "fastbound_item_id": ["is", "set"]},
		pluck="name",
	)
	divergent = []
	for s in serials:
		try:
			res = compute_serial_fb_diff(s)
		except Exception:
			frappe.log_error(title=f"FB diff scan failed for {s}", message=frappe.get_traceback())
			continue
		if res.get("status") == "ok" and not res.get("in_sync"):
			divergent.append({"serial": s, "fields": [d["field"] for d in res["diffs"]]})
	frappe.publish_realtime(
		"fb_diff_scan_done", {"divergent": divergent}, user=user,
	)
	if divergent:
		frappe.get_doc({
			"doctype": "Notification Log", "for_user": user, "type": "Alert",
			"subject": _("FastBound scan: {0} in-stock gun(s) differ").format(len(divergent)),
			"email_content": "<br>".join(
				f"{d['serial']}: {', '.join(d['fields'])}" for d in divergent),
		}).insert(ignore_permissions=True)


@frappe.whitelist()
def mark_not_in_bound_book(doctype: str, name: str, reason: str) -> dict:
	"""Record that a person checked this submitted record and it does not belong in the
	bound book — e.g. an antique (pre-1899 receiver) booked by mistake, which FastBound
	refuses because it never acquired the gun. The daily retry, the stuck digest and the
	Dashboard's "Not in the bound book yet" list leave it alone from then on. The record
	itself is not cancelled (its stock leg is right); the reason, who marked it and the
	previous sync error are kept as a comment.

	Args:
	    doctype: "FFL Acquisition" or "FFL Disposition".
	    name: The submitted record FastBound does not have.
	    reason: Why it is not a bound-book record (required).

	Returns:
	    {ok: True, doctype, name}.
	"""
	from ffl_core.roles import STOCK_ROLES

	from ffl_integrations.fastbound.tasks import NOT_IN_BOOK_PREFIX
	frappe.only_for(STOCK_ROLES)
	if doctype not in ("FFL Acquisition", "FFL Disposition"):
		frappe.throw(_("Only FFL Acquisition / FFL Disposition records can be marked (got {0}).").format(doctype))
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Say why this record is not in the bound book."))
	doc = frappe.get_doc(doctype, name)
	if doc.docstatus != 1 or doc.fastbound_id:
		frappe.throw(_("{0} {1} is not a submitted record missing from the bound book — nothing to mark.")
			.format(doctype, name))
	previous = doc.fastbound_sync_error or ""
	frappe.db.set_value(doctype, name, "fastbound_sync_error", NOT_IN_BOOK_PREFIX + reason,
		update_modified=False)
	doc.add_comment("Info", _("Marked not in the bound book by {0}: {1}. Previous sync error: {2}").format(
		frappe.session.user, reason, previous or _("(none)")))
	return {"ok": True, "doctype": doctype, "name": name}


def _refuse_if_antique(doctype: str, name: str, action: str) -> None:
	"""Refuse putting an ACQUISITION back on the bound book once its gun is antique — its
	Item flagged, or its serial moved to an antique Item. An antique leaves without a 4473
	or a disposition, so a bound-book entry opened for it could never be closed.

	Dispositions are never refused: a disposition CLOSES an entry, and refusing it would
	leave a sold gun open in the book (its per-gun Item may be flagged antique once the
	serial is sold, since a sold serial no longer locks the flag)."""
	if doctype != "FFL Acquisition":
		return
	from ffl_core.firearm import is_antique_item

	row = frappe.db.get_value(doctype, name, ["item", "serial"], as_dict=True) or {}
	current = frappe.db.get_value("Serial No", row.get("serial"), "item_code") if row.get("serial") else None
	antique = next((i for i in (row.get("item"), current) if i and is_antique_item(i)), None)
	if antique:
		frappe.throw(_("Cannot {0} {1} {2}: its gun is under Antique Item {3}, and an antique stays "
			"out of the bound book. Move the serial back to a regulated Item first (Serial No > "
			"Reassign Item).").format(action, doctype, name, antique))


@frappe.whitelist()
def clear_not_in_bound_book(doctype: str, name: str) -> dict:
	"""Undo :func:`mark_not_in_bound_book` — the record turned out to belong in the
	bound book after all. Its sync error is emptied, so the daily retry pushes it again
	and the Dashboard lists it until it lands; a comment records who cleared it.

	Args:
	    doctype: "FFL Acquisition" or "FFL Disposition".
	    name: A record marked off the bound book.

	Returns:
	    {ok: True, doctype, name}.
	"""
	from ffl_core.roles import STOCK_ROLES

	from ffl_integrations.fastbound.tasks import NOT_IN_BOOK_PREFIX
	frappe.only_for(STOCK_ROLES)
	if doctype not in ("FFL Acquisition", "FFL Disposition"):
		frappe.throw(_("Only FFL Acquisition / FFL Disposition records can be marked (got {0}).").format(doctype))
	marked = frappe.db.get_value(doctype, name, "fastbound_sync_error") or ""
	if not marked.startswith(NOT_IN_BOOK_PREFIX):
		frappe.throw(_("{0} {1} is not marked as off the bound book.").format(doctype, name))
	_refuse_if_antique(doctype, name, _("clear the mark on"))
	frappe.db.set_value(doctype, name, "fastbound_sync_error", "", update_modified=False)
	frappe.get_doc(doctype, name).add_comment("Info", _("Bound-book mark cleared by {0} (was: {1})").format(
		frappe.session.user, marked[len(NOT_IN_BOOK_PREFIX):]))
	return {"ok": True, "doctype": doctype, "name": name}


@frappe.whitelist()
def reconcile_record_to_fastbound(doctype: str, name: str, fastbound_ref: str) -> dict:
	"""Link an already-booked POS record to an EXISTING FastBound record.

	For the divergence class where the bound book already carries the event
	(entered directly in FastBound, or a push that landed but lost its id) and
	the POS record is stuck with a sync error: re-pushing would double-book, so
	the fix is to adopt the FastBound record's id. fastbound_id is locked after
	submit on both doctypes — this endpoint is the sanctioned entrance (the
	2026-08-21 JV-return incident had to do all of this through a bench
	console).

	Everything is verified against FastBound LIVE before anything is written
	(fail-closed): the ref must resolve to a real item whose serial matches the
	record's, and a Disposition additionally requires the item to actually be
	disposed. An acquisition reconcile also repoints Serial No.fastbound_item_id
	the way a successful push would.

	Args:
	    doctype: "FFL Acquisition" or "FFL Disposition".
	    name: The submitted, not-yet-linked record to link.
	    fastbound_ref: FastBound item GUID, or the bound-book item number shown
	        in FastBound's UI (digits, e.g. "01171").

	Returns:
	    {ok: True, fastbound_id, fb_item_id, fb_item_number, serial_repointed}.
	"""
	from ffl_core.roles import STOCK_ROLES
	frappe.only_for(STOCK_ROLES)
	if doctype not in ("FFL Acquisition", "FFL Disposition"):
		frappe.throw(_("Only FFL Acquisition / FFL Disposition records can be "
			"reconciled to FastBound (got {0}).").format(doctype))
	doc = frappe.get_doc(doctype, name)
	if doc.docstatus != 1:
		frappe.throw(_("{0} {1} is not submitted — only a submitted record can "
			"be linked to the bound book.").format(doctype, name))
	if doc.fastbound_id:
		frappe.throw(_("{0} {1} is already linked to FastBound record {2}. "
			"Re-linking is deliberate surgery — clear the link manually first "
			"if that record is truly wrong.").format(doctype, name, doc.fastbound_id))
	_refuse_if_antique(doctype, name, _("link"))

	ref = (fastbound_ref or "").strip()
	if not ref:
		frappe.throw(_("A FastBound item GUID or item number is required."))
	try:
		client = FastBoundClient.from_settings()
	except FastBoundConfigError as exc:
		frappe.throw(str(exc))
	item = client.find_item_by_number(ref) if ref.isdigit() else client.get_item(ref)
	if not item or item.get("deleted"):
		frappe.throw(_("FastBound has no item for {0!r} — check the item number "
			"/ GUID in FastBound and try again (item numbers are zero-padded, "
			"e.g. 01171 not 1171). Nothing was changed.").format(ref))
	fb_serial = str(item.get("serial") or "").strip()
	# The bound book records the ledger serial; doc.serial is the gun's POS key.
	doc_serial = str(doc.get("ledger_serial") or "").strip() or _serials.ledger_serial(doc.serial)
	if not fb_serial or not doc_serial:
		frappe.throw(_("A record and a bound-book item can only be linked "
			"through a matching, NON-EMPTY serial — blank on {0}. Nothing was "
			"changed.").format(
			"both sides" if not (fb_serial or doc_serial)
			else (f"FastBound item #{item.get('itemNumber')}" if not fb_serial
				else f"{doctype} {name}")))
	if fb_serial.casefold() != doc_serial.casefold():
		frappe.throw(_("FastBound item #{0} is serial {1!r} but {2} {3} is serial "
			"{4!r} — refusing to link records for two different guns.").format(
			item.get("itemNumber"), fb_serial, doctype, name, doc_serial))

	item_id = item.get("id") or item.get("itemId")
	if doctype == "FFL Disposition":
		if not (item.get("disposed") or FastBoundClient._item_is_disposed(item)) \
				or not item.get("dispositionId"):
			frappe.throw(_("FastBound item #{0} is not disposed — linking this "
				"disposition to it would assert a transfer the bound book "
				"denies. Nothing was changed.").format(item.get("itemNumber")))
		fastbound_id = item["dispositionId"]
		serial_repointed = False
	else:
		# The push stores the FastBound ACQUISITION id; fall back to the item
		# id when it can't be resolved (the _adopt_existing_item convention).
		fastbound_id = client.find_acquisition_id_by_item(item_id) or item_id
		serial_repointed = bool(frappe.db.exists("Serial No", doc.serial))
		if serial_repointed:
			frappe.db.set_value("Serial No", doc.serial,
				"fastbound_item_id", item_id, update_modified=False)

	frappe.db.set_value(doctype, name, {
		"fastbound_id": fastbound_id,
		"fastbound_synced_at": frappe.utils.now_datetime(),
		"fastbound_sync_error": "",
	}, update_modified=False)
	doc.add_comment("Comment", _(
		"Reconciled to existing FastBound record {0} (item #{1}, {2}) by {3} — "
		"linked in place of a re-push, which would have double-booked."
	).format(fastbound_id, item.get("itemNumber"), item_id, frappe.session.user))
	return {"ok": True, "fastbound_id": fastbound_id, "fb_item_id": item_id,
		"fb_item_number": item.get("itemNumber"), "serial_repointed": serial_repointed}


@frappe.whitelist()
def retry_disposition_push(name: str) -> dict:
	"""Re-run the FastBound push for a submitted FFL Disposition that failed.

	``push_disposition_to_fastbound`` is a ``@frappe.task`` and nothing else —
	there was no way to re-run one. A push that failed sat in
	``fastbound_sync_error`` until the next daily ``reconcile_failed_pushes``,
	which is up to 24 hours with a firearm out of the building and no bound-book
	entry for it. DISP-2026-02000 spent a day in exactly that state while the
	obstacle that caused the 400 had already been cleared by hand.

	A thin wrapper on purpose: it re-checks who may press it and what may be
	pressed, then calls the task unchanged. Run INLINE rather than enqueued so
	the operator sees the outcome — the task is already idempotent (it no-ops on
	an already-linked record) and carries its own retry/back-off.

	The ``PUSHABLE_DISPOSITION_TYPES`` check is not decoration. A Retail Sale is
	a 4473 disposition that FastBound's own wizard owns; pushing one from here
	would book a to-individual transfer as a plain bound-book entry — a
	compliance defect, not a sync error. The task would refuse it silently, so
	this refuses it loudly instead of pretending to have done something. So does
	FastBound being switched off, for the same reason: the task returns silently
	on a disabled integration, which behind a button reads as "it failed again,
	reason unknown".

	The task's retry ladder is turned OFF here (``_retries=0``): an operator is
	waiting, and the default four attempts with back-off can hold the desk for
	~80 seconds before showing anything.

	Args:
	    name: The submitted FFL Disposition to push.

	Returns:
	    ``{ok, name, fastbound_id, fastbound_sync_error}`` — ``ok`` is True only
	    when the push landed an id; a still-failing push returns ok False with
	    the error FastBound gave, so the button can show it.

	Permissions:
	    Inventory roles (same gate as :func:`reconcile_record_to_fastbound`).
	"""
	from ffl_core.roles import STOCK_ROLES

	from ffl_integrations.fastbound.client import PUSHABLE_DISPOSITION_TYPES
	from ffl_integrations.fastbound.tasks import push_disposition_to_fastbound
	from ffl_integrations.integration_gate import fastbound_enabled

	frappe.only_for(STOCK_ROLES)

	# The task answers a disabled integration with a silent return — correct for a
	# scheduled job, useless behind a button: the wrapper would report ok:False
	# with whatever stale fastbound_sync_error was already on the row, and the
	# operator would read "FastBound refused it again: ?". Say what is true.
	if not fastbound_enabled():
		frappe.throw(_("FastBound is switched off (FastBound Settings → Enable "
			"FastBound), so nothing can be pushed to the bound book. Nothing "
			"was sent."))

	name = (name or "").strip()
	if not name or not frappe.db.exists("FFL Disposition", name):
		frappe.throw(_("FFL Disposition {0} not found.").format(name or "?"))
	row = frappe.db.get_value(
		"FFL Disposition", name,
		["docstatus", "fastbound_id", "disposition_type", "fastbound_sync_error"], as_dict=True)
	from ffl_integrations.fastbound.tasks import NOT_IN_BOOK_PREFIX
	if (row.fastbound_sync_error or "").startswith(NOT_IN_BOOK_PREFIX):
		frappe.throw(_("{0} is marked as not belonging in the bound book ({1}), so nothing was "
			"sent. If it does belong there, clear the mark first.").format(
			name, row.fastbound_sync_error[len(NOT_IN_BOOK_PREFIX):]))
	if int(row.docstatus or 0) != 1:
		frappe.throw(_("{0} is not submitted — only a submitted disposition can be "
			"pushed to the bound book.").format(name))
	if (row.fastbound_id or "").strip():
		frappe.throw(_("{0} is already in the bound book as {1}. Re-pushing would "
			"double-book it.").format(name, row.fastbound_id))
	if row.disposition_type not in PUSHABLE_DISPOSITION_TYPES:
		frappe.throw(_("{0} is a {1} disposition, which FastBound's 4473 flow owns "
			"— it is never pushed from here. Pushing it would record a transfer to "
			"an individual as a plain bound-book entry.").format(
			name, row.disposition_type or "?"))

	# _retries=0: ONE attempt. The task's default ladder is four attempts with
	# exponential back-off on top of a 30s-per-request timeout — up to ~80
	# seconds of a frozen desk while an operator watches a spinner. This path is
	# interactive and cheap to repeat (the button is right there, and the daily
	# reconcile_failed_pushes retries on its own), so it fails fast and shows the
	# error instead.
	push_disposition_to_fastbound(name, _retries=0)

	after = frappe.db.get_value(
		"FFL Disposition", name, ["fastbound_id", "fastbound_sync_error"],
		as_dict=True) or {}
	fb_id = (after.get("fastbound_id") or "").strip()
	return {
		"ok": bool(fb_id),
		"name": name,
		"fastbound_id": fb_id,
		"fastbound_sync_error": after.get("fastbound_sync_error") or "",
	}
