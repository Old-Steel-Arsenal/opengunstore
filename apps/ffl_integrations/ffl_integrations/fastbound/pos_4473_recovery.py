"""Counter-side recovery for a 4473 whose status drifted out of sync.

Born from a past incident: the 4473 completed in FastBound, the
webhook synced it back, and a stale POS page then reset the invoice to Pending
by re-clicking the 4473 button.

* ``refresh_4473_status`` — what the cashier's "Refresh 4473" button calls.
  Three layers, cheapest first, and only the middle one writes.
* ``manager_redo_4473`` — the sanctioned way to re-open a completed 4473.

NICS lives ONLY in the form4473.completed webhook payload
(``Form4473CompletedEvent.response``); no REST endpoint exposes it — not
``GET /Items``, not ``GET /Dispositions/Only4473s``, not the 4473 PDF download.
So a FastBound probe may only *display* what it finds. The sole evidence we
accept for writing Completed/Proceed is our own committed Retail Sale ledger,
which is the same evidence the submit gate uses
(``ffl_core.compliance.firearm.covered_serials_for_invoice``).
"""
from __future__ import annotations

import frappe
from frappe import _

from ffl_core.compliance.firearm import (
	FORM_4473_REQUIRED,
	covered_serials_for_invoice,
	regulated_firearm_serials,
)
from ffl_core.firearm import is_regulated_firearm_item
from ffl_core.utils import commit

from ffl_integrations.fastbound.client import (
	FastBoundClient,
	FastBoundConfigError,
	FastBoundError,
)
from ffl_integrations.fastbound.pos_4473 import _check_manager_perm, _check_perm

# refresh_4473_status outcomes — the POS button switches its message on these.
STATE_NO_FIREARM = "no_firearm"
STATE_NO_SERIALS = "no_serials"
STATE_COMPLETED = "completed"
STATE_REPAIRED = "repaired_from_ledger"
STATE_FASTBOUND_ONLY = "fastbound_only"
STATE_NOT_READY = "not_ready"
STATE_FB_UNREACHABLE = "fb_unreachable"

NICS_PROCEED = "Proceed"
NICS_NONE = "N/A"
FORM_4473_PENDING = "Pending"

# Per-request ceiling for the interactive FastBound probe. Bounded on purpose:
# a cashier is waiting on the button (see _probe_client).
PROBE_TIMEOUT_SECONDS = 5

# Written as one group by the webhook; read and reset as one group here.
WEBHOOK_OWNED_FIELDS = (
	"form_4473_status",
	"nics_status",
	"fastbound_disposition_id",
	"fastbound_4473_pdf_url",
)


def _firearm_serials(doc) -> list[str]:
	"""Every regulated-firearm serial on the cart — one entry per gun, order kept.

	Goes through the canonical ``ffl_core.serials.serials_for_line`` because a
	line stores its serials in EITHER of two ways: newline-joined in
	``row.serial_no`` when qty>1, or in a Serial
	and Batch Bundle, which is the v16 default. Reading ``row.serial_no`` alone
	made a bundle cart look gun-less — and a gun-less cart is exactly what let
	``manager_abandon_4473`` delete the draft, freeing the gun onto the shelf,
	while FastBound's pending disposition went on holding it and the call still
	reported success.

	Antiques are GCA-exempt and never need a 4473. Shared with the submit gate
	via ``regulated_firearm_serials``."""
	return regulated_firearm_serials(doc.items)


def _flags(invoice_name: str) -> dict:
	return frappe.db.get_value(
		"POS Invoice", invoice_name, list(WEBHOOK_OWNED_FIELDS), as_dict=True) or {}


def _result(state: str, invoice_name: str, *, covered=(), missing=(),
		fastbound=None, dispositions=()) -> dict:
	flags = _flags(invoice_name)
	return {
		"ok": True,
		"state": state,
		"invoice": invoice_name,
		"form_4473_status": flags.get("form_4473_status") or "",
		"nics_status": flags.get("nics_status") or "",
		"fastbound_disposition_id": flags.get("fastbound_disposition_id") or "",
		"fastbound_4473_pdf_url": flags.get("fastbound_4473_pdf_url") or "",
		"covered": sorted(covered),
		"missing": sorted(missing),
		"fastbound": list(fastbound or []),
		"dispositions": list(dispositions),
	}


def _add_comment(invoice_name: str, content: str) -> None:
	"""Audit trail on the invoice. Best-effort: a comment must never be the
	reason a recovery fails."""
	try:
		frappe.get_doc({
			"doctype": "Comment",
			"comment_type": "Comment",
			"reference_doctype": "POS Invoice",
			"reference_name": invoice_name,
			"content": content,
		}).insert(ignore_permissions=True)
	except Exception:  # noqa: BLE001 — logging must never block a recovery
		frappe.log_error(
			title=f"POS Invoice {invoice_name}: 4473 recovery comment failed",
			message=frappe.get_traceback())


def _notify_pos(invoice_name: str) -> None:
	commit()
	frappe.publish_realtime(
		"doc_update", {"doctype": "POS Invoice", "name": invoice_name},
		after_commit=True,
	)


def _ledger_rows(invoice_name: str, serials: list[str]) -> list:
	"""The committed Retail Sale dispositions backing these serials — the same
	criteria covered_serials_for_invoice applies, plus the fields we report."""
	if not serials:
		return []
	return frappe.get_all(
		"FFL Disposition",
		filters={
			"linked_pos_invoice": invoice_name,
			"serial": ("in", serials),
			"disposition_type": "Retail Sale",
			"form_4473_status": FORM_4473_REQUIRED,
			"nics_status": NICS_PROCEED,
			"docstatus": 1,
		},
		fields=["name", "serial", "fastbound_id", "manual_override"],
	)


def _probe_client() -> FastBoundClient:
	"""A FastBound client tuned for the counter: short timeout, no retry storm.

	The shared client (``FastBoundClient.from_settings``) retries 5x with
	exponential back-off — roughly 18 seconds against an unreachable host. That
	is right for background sync and wrong for a button a cashier is standing in
	front of, so the probe fails fast and the caller degrades to
	``fb_unreachable``.

	The construction itself now lives on the client (``from_settings_fast``) — the
	4473 webhook lookup needs the same fail-fast posture for the same reason.
	"""
	return FastBoundClient.from_settings_fast(timeout=PROBE_TIMEOUT_SECONDS)


def _probe_fastbound(serials: list[str]) -> tuple[list[dict], str | None]:
	"""What FastBound knows about these serials — READ ONLY.

	Returns (rows, error). ``error`` is a human string when FastBound is
	unconfigured or unreachable; the caller degrades instead of raising, because
	this runs behind a button at the counter and must never blow up in a
	cashier's face.

	The bound-book item tells us whether the gun was disposed and under which
	disposition — but NOT the NICS outcome (that only ever arrives on the
	form4473.completed webhook), which is why nothing here writes.
	"""
	if not serials:
		return [], None
	try:
		client = _probe_client()
	except FastBoundConfigError as exc:
		return [], str(exc)

	rows: list[dict] = []
	for serial in serials:
		try:
			item = client.find_item_by_serial(serial) or {}
		except (FastBoundError, FastBoundConfigError) as exc:
			return rows, str(exc)
		except Exception as exc:  # noqa: BLE001 — network / library surprises
			frappe.log_error(
				title=f"4473 refresh: FastBound probe failed for {serial}",
				message=frappe.get_traceback())
			return rows, str(exc)
		rows.append({
			"serial": serial,
			"found": bool(item),
			"disposed": bool(item) and FastBoundClient._item_is_disposed(item),
			"disposition_id": (item.get("dispositionId") or "") if item else "",
			# The number FastBound's own 400 names ("Item 00843 has already been
			# added…") and the one an operator types into its search box, so it is
			# what every message built off this probe must show. ttsn is the legacy
			# fallback for an item that carries no itemNumber.
			"item_number": (item.get("itemNumber") or item.get("ttsn") or "") if item else "",
			"ttsn": (item.get("ttsn") or "") if item else "",
			"disposition_type": (item.get("dispositionType") or "") if item else "",
		})
	return rows, None


@frappe.whitelist()
def refresh_4473_status(invoice_name: str) -> dict:
	"""Re-read the authoritative 4473 state for a POS Invoice.

	Layer 1 — the DB already says Completed/Proceed: the caller's page was just
	stale. Report and stop.
	Layer 2 — every firearm on the order has a committed Retail Sale disposition
	but the doc-level flags say otherwise: the flags are stale, restore them from
	the ledger. This is the past invoice shape.
	Layer 3 — coverage is incomplete: ask FastBound what it knows and REPORT it.

	Never writes NICS from a FastBound probe: the API does not carry the outcome,
	so inferring Proceed from "the gun left the bound book" would fabricate a
	compliance fact.

	Args:
	    invoice_name: POS Invoice name.

	Returns:
	    {state: completed|repaired|no_firearm|no_serials|<incomplete>,
	    invoice, covered, missing, [dispositions], [fastbound]}.

	Permissions:
	    POS 4473 roles (_check_perm).
	"""
	_check_perm()

	invoice_name = (invoice_name or "").strip()
	if not invoice_name or not frappe.db.exists("POS Invoice", invoice_name):
		frappe.throw(_("POS Invoice {0} not found.").format(invoice_name or "?"))

	doc = frappe.get_doc("POS Invoice", invoice_name)
	if not any(is_regulated_firearm_item(row.item_code) for row in (doc.items or [])):
		return _result(STATE_NO_FIREARM, invoice_name)
	serials = _firearm_serials(doc)
	if not serials:
		# Firearms are in the cart but no serial has been picked yet. Distinct
		# from "no firearm": there is nothing to look up anywhere, and telling
		# the cashier "still waiting on FastBound" would be a lie — they haven't
		# started the 4473 yet.
		return _result(STATE_NO_SERIALS, invoice_name)

	flags = _flags(invoice_name)
	if ((flags.get("form_4473_status") or "") == FORM_4473_REQUIRED
			and (flags.get("nics_status") or "") == NICS_PROCEED):
		return _result(STATE_COMPLETED, invoice_name, covered=serials)

	covered = covered_serials_for_invoice(invoice_name, serials)
	missing = [s for s in serials if s not in covered]

	if not missing and int(doc.docstatus or 0) == 0:
		rows = _ledger_rows(invoice_name, serials)
		updates = {"form_4473_status": FORM_4473_REQUIRED, "nics_status": NICS_PROCEED}
		if not (flags.get("fastbound_disposition_id") or "").strip():
			fb_id = next((r.fastbound_id for r in rows if (r.fastbound_id or "").strip()), "")
			if fb_id:
				updates["fastbound_disposition_id"] = fb_id
		frappe.db.set_value("POS Invoice", invoice_name, updates, update_modified=False)
		_add_comment(invoice_name, _(
			"<b>4473 status refreshed from the disposition ledger</b><br>"
			"Every firearm on this order has a committed Retail Sale disposition "
			"(4473 Completed / NICS Proceed), but the order still read {0} / {1} — "
			"restored to Completed / Proceed.<br>Evidence: {2}"
		).format(
			frappe.utils.escape_html(flags.get("form_4473_status") or "(empty)"),
			frappe.utils.escape_html(flags.get("nics_status") or "(empty)"),
			frappe.utils.escape_html(", ".join(sorted(r.name for r in rows))),
		))
		_notify_pos(invoice_name)
		return _result(STATE_REPAIRED, invoice_name, covered=covered, missing=[],
			dispositions=sorted(r.name for r in rows))

	fb_rows, fb_error = _probe_fastbound(missing)
	if fb_error:
		state = STATE_FB_UNREACHABLE
	elif any(r.get("disposed") for r in fb_rows):
		state = STATE_FASTBOUND_ONLY
	else:
		state = STATE_NOT_READY
	return _result(state, invoice_name, covered=covered, missing=missing,
		fastbound=fb_rows)


@frappe.whitelist()
def manager_redo_4473(invoice_name: str, reason: str) -> dict:
	"""Re-open a COMPLETED 4473 — the only sanctioned way past the reset guard.

	The counter path (``pos_4473.start_4473_for_pos_invoice``) refuses to reset a
	completed order, because a stale page clicking that button is exactly what
	broke a past invoice. A genuine redo — wrong serial on the 4473, buyer
	denied after the fact, the disposition voided in FastBound — comes through
	here instead:

	* manager-gated (Sales Manager / System Manager) and reason-required;
	* cancels this order's committed Retail Sale dispositions — the bound-book
	  mirror of the 4473 being voided — so coverage drops to zero and
	  ``refresh_4473_status`` has nothing left to restore Completed from;
	* clears the webhook-owned field group as a WHOLE, keeping "Pending while
	  still carrying a fastbound_disposition_id" an impossible state. That shape
	  is the fingerprint of the bug this change closes, so it must not be
	  reachable through a legitimate path either. ``fastbound_url`` goes too: it
	  is the "electronic 4473 in flight" marker that makes ``attest_paper_4473``
	  refuse, and whose error message sends the cashier here;
	* marks this order's Paper / override Form 4473 Log rows Cancelled (the
	  status the webhook gives a voided electronic 4473), so the row the redone
	  form left behind can be told apart from the corrected one logged next.

	Does NOT touch FastBound — void the disposition there first.

	Args:
	    invoice_name: Draft POS Invoice whose 4473 is to be re-opened.
	    reason: Required audit reason.

	Returns:
	    {ok, invoice, cancelled: [disposition names], n_cancelled}.

	Permissions:
	    Sales Manager / System Manager (manager gate) + required reason.
	"""
	_check_manager_perm()

	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("A reason is required to redo a completed 4473."))

	invoice_name = (invoice_name or "").strip()
	if not invoice_name or not frappe.db.exists("POS Invoice", invoice_name):
		frappe.throw(_("POS Invoice {0} not found.").format(invoice_name or "?"))
	if int(frappe.db.get_value("POS Invoice", invoice_name, "docstatus") or 0) != 0:
		frappe.throw(_("Cannot redo the 4473 on a submitted invoice."))

	flags = _flags(invoice_name)
	cancelled: list[str] = []
	for name in frappe.get_all(
		"FFL Disposition",
		filters={
			"linked_pos_invoice": invoice_name,
			"disposition_type": "Retail Sale",
			"docstatus": 1,
		},
		pluck="name",
	):
		disp = frappe.get_doc("FFL Disposition", name)
		disp.flags.ignore_permissions = True
		disp.cancel()
		cancelled.append(name)

	frappe.db.set_value("POS Invoice", invoice_name, {
		"form_4473_status": FORM_4473_PENDING,
		"nics_status": NICS_NONE,
		"fastbound_disposition_id": "",
		"fastbound_4473_pdf_url": "",
		"fastbound_url": "",
	}, update_modified=False)
	frappe.db.set_value("Form 4473 Log",
		{"source": "Paper / override", "pos_invoice": invoice_name},
		"nics_status", "Cancelled")

	_add_comment(invoice_name, _(
		"<b>4473 redo by {0}</b><br>Reason: {1}<br>"
		"Cancelled Retail Sale disposition(s): {2}<br>"
		"Cleared FastBound disposition id {3} and the 4473 PDF link; the order is "
		"back to Pending and needs a fresh 4473."
	).format(
		frappe.utils.escape_html(frappe.session.user),
		frappe.utils.escape_html(reason),
		frappe.utils.escape_html(", ".join(cancelled) or "(none)"),
		frappe.utils.escape_html(flags.get("fastbound_disposition_id") or "(empty)"),
	))
	_notify_pos(invoice_name)

	return {
		"ok": True,
		"invoice": invoice_name,
		"cancelled": cancelled,
		"n_cancelled": len(cancelled),
	}


# ===========================================================================
# Abandoned 4473 — the pending disposition it leaves in the bound book
# ===========================================================================

def pending_disposition_probe(serials) -> dict:
	"""Which of these guns FastBound has parked on an UNCOMMITTED disposition.

	Registered as ``ffl_pending_disposition_probe`` so the Pending 4473 Orders
	report (ffl_core, which must run with no integration app at all) can show
	the fact without importing this one.

	The state it looks for is what an abandoned 4473 wizard leaves behind: the
	item is NOT disposed — the bound book still shows it available, so nothing
	on the POS side objects — yet it carries a ``dispositionId``, and the next
	real disposition for that gun is refused with ``has already been added to
	another disposition``. That is DISP-2026-02000 exactly.

	READ ONLY, and never raises: it decorates a report, and FastBound being
	unreachable must cost the operator a blank column rather than the queue.

	Args:
	    serials: Serial numbers to look up.

	Returns:
	    ``{serial: {"disposition_id": str, "item_number": str}}`` — only guns
	    actually held by a pending disposition appear. Both keys are CONTRACT:
	    the Pending 4473 Orders report renders exactly these (it used to read a
	    ``status`` key nothing ever sent, and dropped the item number, which is
	    the one thing an operator needs to find the hold in FastBound).
	"""
	out: dict[str, dict] = {}
	rows, error = _probe_fastbound([s for s in (serials or []) if s])
	if error:
		frappe.logger("ffl_integrations.fastbound", allow_site=True).warning(
			f"pending_disposition_probe: {error}")
	for row in rows:
		if row.get("disposed") or not row.get("disposition_id"):
			continue
		out[row["serial"]] = {
			"disposition_id": row["disposition_id"],
			"item_number": row.get("item_number") or row.get("ttsn") or "",
		}
	return out


def _log_abandon_progress(invoice_name: str, reason: str, deleted: list[str],
		skipped: list[str], outcome: str) -> None:
	"""Persist WHICH FastBound pending dispositions are already gone, then commit.

	Every failure below this point leaves FastBound changed and the POS untouched,
	and ``frappe.throw`` rolls the request back — taking the Error Log row with it
	if it was written inside the transaction. The operator re-running the button
	would then be blind to what the first run deleted. So the trail is written and
	COMMITTED before the throw; the commit is safe because the only POS write in
	this call (``delete_doc``) happens after every throw, so there is nothing of
	ours to keep uncommitted.

	``safe_log_error`` falls back to the site's file logger when ``frappe.log_error``
	itself cannot write, so the trail is not guaranteed to be an Error Log ROW. The
	messages that point the operator at it say "or the site log" for that reason —
	promising a row that is not there is how someone concludes nothing was deleted.
	"""
	from ffl_integrations.notifications import safe_log_error

	safe_log_error(
		f"Abandoned 4473 partially cleared: {invoice_name}",
		f"Abandoned 4473 clear by {frappe.session.user} did NOT finish\n"
		f"POS Invoice: {invoice_name} (NOT deleted)\nReason: {reason}\n"
		f"FastBound pending dispositions deleted: {', '.join(deleted) or '(none)'}\n"
		f"Serials with nothing to delete: {', '.join(skipped) or '(none)'}\n"
		f"Stopped because: {outcome}\n"
		"Re-running deletes only what is still held; the ids above are already gone.",
	)
	commit()


@frappe.whitelist()
def manager_abandon_4473(invoice_name: str, reason: str) -> dict:
	"""Clear an abandoned counter order: drop FastBound's pending disposition
	for every gun on it, then delete the draft that has been holding them.

	The counter half of the DISP-2026-02000 fix, and deliberately a BUTTON. The
	daily watch (``ffl_core.api.pending_4473.alert_overdue_pending_4473``) only
	ever raises a ToDo: deleting a bound-book record is an ATF-visible act and
	an unattended job may not decide to perform one.

	Same shape as :func:`manager_redo_4473` — manager gate, reason required,
	audit trail — with one difference that matters: the invoice does not
	survive, so the trail is an Error Log entry rather than a comment on a
	document that is about to disappear. The draft itself is *deleted*, not
	cancelled (a draft cannot be cancelled), which is how the POS closes an
	abandoned order anyway — ``ffl_core.api.pos_recycle_bin`` keeps it
	restorable for as long as its Deleted Document row lives.

	FastBound goes FIRST, and is then RE-READ. A failure leaves everything as it
	was; doing it the other way round would release the gun while the bound book
	still held it, which is the state this whole change exists to end. The
	re-read is there because DELETE is not always the end of the hold — it can
	leave the item carrying a dangling ``dispositionId`` (observed 2026-07-07;
	see :meth:`FastBoundClient.undispose_item`). The draft is the only POS trace
	of the order, so it is given up only once FastBound itself confirms every gun
	is free.

	Args:
	    invoice_name: The Draft POS Invoice to clear.
	    reason: Required audit reason.

	Returns:
	    ``{ok, invoice, serials, deleted, skipped}`` — ``deleted`` lists the
	    FastBound disposition ids removed, ``skipped`` the serials that had no
	    pending disposition to remove.

	Permissions:
	    Sales Manager / System Manager (manager gate) + required reason.
	"""
	from ffl_integrations.notifications import safe_log_error

	_check_manager_perm()

	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("A reason is required to clear an abandoned 4473 order."))

	invoice_name = (invoice_name or "").strip()
	if not invoice_name or not frappe.db.exists("POS Invoice", invoice_name):
		frappe.throw(_("POS Invoice {0} not found.").format(invoice_name or "?"))
	if int(frappe.db.get_value("POS Invoice", invoice_name, "docstatus") or 0) != 0:
		frappe.throw(_("Only a DRAFT order can be cleared — {0} is submitted or "
			"cancelled. Nothing was changed.").format(invoice_name))

	doc = frappe.get_doc("POS Invoice", invoice_name)
	serials = _firearm_serials(doc)

	deleted: list[str] = []
	skipped: list[str] = []
	if serials:
		rows, error = _probe_fastbound(serials)
		if error:
			frappe.throw(_("FastBound could not be reached ({0}), so this order's "
				"pending disposition cannot be cleared. Nothing was changed.").format(error))
		client = _probe_client()
		found = {r["serial"]: r for r in rows}
		for serial in serials:
			row = found.get(serial) or {}
			disp_id = "" if row.get("disposed") else (row.get("disposition_id") or "")
			if not disp_id:
				skipped.append(serial)
				continue
			try:
				client.delete_disposition(disp_id)
			except (FastBoundError, FastBoundConfigError) as exc:
				_log_abandon_progress(invoice_name, reason, deleted, skipped,
					f"FastBound refused to delete {disp_id} for serial {serial}: {exc}")
				frappe.throw(_("FastBound refused to delete pending disposition {0} "
					"for serial {1}: {2}. {3} disposition(s) were already removed "
					"(listed in the Error Log, or the site log if that write "
					"failed); the order was NOT deleted.").format(
					disp_id, serial, exc, len(deleted)))
			deleted.append(disp_id)

		# Verify rather than assume: a DELETE that returned 200 can still
		# leave the item held by a dangling dispositionId, in which case the next
		# real disposition for this gun goes on 400ing — and deleting the draft
		# would destroy the only POS record of the order while telling the
		# operator it was cleared. Re-probing also costs nothing when there is
		# nothing to verify, so it is skipped when no delete happened.
		if deleted:
			rows, error = _probe_fastbound(serials)
			if error:
				_log_abandon_progress(invoice_name, reason, deleted, skipped,
					f"FastBound unreachable on the verify re-probe: {error}")
				frappe.throw(_("{0} pending disposition(s) were deleted, but FastBound "
					"then became unreachable ({1}) so the guns could not be confirmed "
					"free. The order was NOT deleted — try again. A gun whose hold is "
					"really gone is skipped on the next run; one still carrying a dangling "
					"disposition id is NOT — it is deleted again (harmless: FastBound "
					"answers 404 and the client treats that as done) and then reported as "
					"still held. The ids deleted so far are in the Error Log, or the "
					"site log if that write failed.").format(
					len(deleted), error))
			still_held = [
				f"{r['serial']} (disposition {r.get('disposition_id')})"
				for r in rows
				if not r.get("disposed") and r.get("disposition_id")
			]
			if still_held:
				_log_abandon_progress(invoice_name, reason, deleted, skipped,
					"FastBound still reports these guns held: " + "; ".join(still_held))
				frappe.throw(_("FastBound deleted {0} pending disposition(s) but still "
					"reports these guns held: {1}. A disposition id can survive its "
					"disposition (a dangling hold), and the next disposition for such a "
					"gun is still refused — so the order was NOT deleted. Clear the hold "
					"in FastBound (Undispose / delete the disposition there), then run "
					"this again.").format(len(deleted), "; ".join(still_held)))

	trail = (
		f"Abandoned 4473 cleared by {frappe.session.user}\n"
		f"POS Invoice: {invoice_name}\nReason: {reason}\n"
		f"Serials: {', '.join(serials) or '(none)'}\n"
		f"FastBound pending dispositions deleted: {', '.join(deleted) or '(none)'}\n"
		f"Serials with nothing to delete: {', '.join(skipped) or '(none)'}\n"
		"The draft was deleted; restore it from the POS recycle bin if this was wrong."
	)
	safe_log_error(f"Abandoned 4473 cleared: {invoice_name}", trail)

	frappe.delete_doc("POS Invoice", invoice_name, ignore_permissions=True)
	commit()

	return {
		"ok": True,
		"invoice": invoice_name,
		"serials": serials,
		"deleted": deleted,
		"skipped": skipped,
	}
