"""Async FastBound push tasks. Run on Frappe RQ."""
from __future__ import annotations

import time

import frappe
from frappe import _
from frappe.utils import now_datetime

from ffl_integrations.fastbound.client import (
	FastBoundClient,
	FastBoundConfigError,
	FastBoundError,
	PUSHABLE_DISPOSITION_TYPES,
)
from ffl_integrations.integration_gate import fastbound_enabled
from ffl_integrations.notifications import notify_admins

# Marker the push tasks prepend to fastbound_sync_error when a push hit a
# PER-RECORD terminal failure (FastBoundError.terminal). See _fb_error_is_terminal.
_TERMINAL_PREFIX = "[TERMINAL] "

# A person checked the record and it does not belong in the bound book (an antique booked
# by mistake, DISP-2026-02131). Stamped by reconcile.mark_not_in_bound_book: nothing pushes
# or retries it, and neither the stuck digest nor the Dashboard lists it as waiting.
NOT_IN_BOOK_PREFIX = "[NOT A&D] "


def _off_book(doctype: str, name: str) -> bool:
	return (frappe.db.get_value(doctype, name, "fastbound_sync_error") or "").startswith(NOT_IN_BOOK_PREFIX)


def _record_push_error(doctype: str, name: str, sync_error: str) -> bool:
	"""Write a failed push's error unless the record was marked off the bound book
	meanwhile. One conditional UPDATE, because an UPDATE reads the latest committed
	row: a plain re-read inside this job's REPEATABLE READ snapshot would miss a marker
	another session committed while the push slept in its back-off (and on a server
	with snapshot isolation the UPDATE raises 1020 instead — the marker survives
	either way). Returns whether the row changed: False when the marker won, and also
	when the very same error was already stored (no new bell is due for that either)."""
	frappe.db.sql(f"""UPDATE `tab{doctype}` SET fastbound_sync_error = %s
		WHERE name = %s AND COALESCE(fastbound_sync_error, '') NOT LIKE %s""",
		(sync_error, name, NOT_IN_BOOK_PREFIX + "%"))
	return bool(frappe.db._cursor.rowcount)

# Every entry point below opens with `if not fastbound_enabled(): return`.
#
# A store running without FastBound keeps its A&D book HERE: ffl_core still
# creates the FFL Acquisition / FFL Disposition records, they just never leave the
# building. Without these guards that store would push to a vendor it has no
# account with, fail, stamp fastbound_sync_error on every record it ever books,
# and be nagged about the backlog daily and forever.
#
# Guarded on BOTH sides — the enqueue hooks (no pointless jobs) and the task
# bodies (a job already sitting in the queue when the switch was flipped must not
# fire). The check is one indexed row read, so paying for it twice is free.
#
# Deliberately NOT guarded:
#   * alert_stale_trade_in_drafts — a §478.125(e) next-business-day deadline on
#     OUR OWN records. Nothing to do with FastBound, and it matters MORE to a
#     paper-book store, not less.
#   * an ENABLED-but-misconfigured FastBound, which must keep failing loudly (see
#     integration_gate's docstring). "Off" is an operator statement of intent;
#     "broken" is an incident, and a bound book cannot afford to confuse them.


def _fb_error_is_terminal(err: str | None) -> bool:
	"""True when a stored ``fastbound_sync_error`` was flagged terminal AT THE
	SOURCE — a failure blind retry can never fix that needs a human (already
	disposed, can't auto-adopt). The push tasks stamp _TERMINAL_PREFIX when
	FastBoundError.terminal is set, so terminality is an explicit, semantic
	decision — NOT inferred from the HTTP status. That distinction matters: a
	global/config fault (Invalid Audit User, expired key → 4xx) and a transient
	4xx (408, gateway-rewritten) are recoverable and MUST stay retryable so the
	daily reconcile recovers them once the cause clears."""
	return bool(err) and err.startswith(_TERMINAL_PREFIX)


@frappe.task(queue="long", timeout=120)
def push_acquisition_to_fastbound(
	acquisition_name: str,
	*,
	_retries: int = 3,
	_commit: bool = True,
) -> None:
	"""Push one FFL Acquisition to FastBound. Idempotent (no-op when already synced).
	Retries `_retries` times with exponential backoff on FastBoundError."""
	if not fastbound_enabled():
		return  # switched off — the record stays local (see module header)
	doc = frappe.get_doc("FFL Acquisition", acquisition_name)
	# Submit-then-cancel race guard — see push_disposition_to_fastbound for the
	# full rationale: pushing a cancelled doc orphans a FastBound record that
	# the docstatus:1-filtered reconciler can never see.
	if doc.docstatus != 1:
		return
	if doc.fastbound_id:
		return  # already synced
	if _off_book("FFL Acquisition", acquisition_name):
		return  # a person marked it off the bound book

	_delete_amended_predecessor(doc, _commit=_commit)

	last_error: str | None = None
	last_terminal = False
	for attempt in range(max(1, _retries) + 1):
		try:
			client = FastBoundClient.from_settings()
			result = client.post_acquisition(doc)
			_record_acquisition_push(acquisition_name, result)
			if _commit:
				frappe.db.commit()
			return
		except FastBoundError as exc:
			last_error = str(exc)
			last_terminal = bool(getattr(exc, "terminal", False))
			# Terminal failures (already disposed / can't auto-adopt) won't change
			# on retry — fail fast; everything else backs off and retries.
			if last_terminal or attempt >= _retries:
				break
			time.sleep(2 ** attempt)

	sync_error = (_TERMINAL_PREFIX if last_terminal else "") + (last_error or "unknown error")
	if _record_push_error("FFL Acquisition", acquisition_name, sync_error) and last_terminal:
		_notify_terminal("FFL Acquisition", acquisition_name, last_error or "")
	if _commit:
		frappe.db.commit()


def _record_acquisition_push(acquisition_name: str, result: dict) -> None:
	"""Record a successful CreateAndCommit (no commit — the caller owns it): the
	FastBound id on the FFL Acquisition, and each returned item's FastBound id on
	its Serial No so the bound book stays linkable per gun (4473 dispositions
	reference items by FB id; the Item form's Serial Numbers tab surfaces them)."""
	frappe.db.set_value("FFL Acquisition", acquisition_name, {
		"fastbound_id": result["acquisitionId"],
		"fastbound_synced_at": now_datetime(),
		"fastbound_sync_error": "",
	}, update_modified=False)
	for fb_item in result.get("items", []):
		if (fb_item.get("serial") and fb_item.get("fbItemId")
				and frappe.db.exists("Serial No", fb_item["serial"])):
			frappe.db.set_value("Serial No", fb_item["serial"],
				"fastbound_item_id", fb_item["fbItemId"], update_modified=False)


def _notify_terminal(doctype: str, name: str, error: str) -> None:
	"""A push FastBound refused for good (a [TERMINAL] sync error) waits on a person:
	ring the managers' bell. The reason is in the subject, which is also the dedupe
	key, so a record fixed and refused again for a new reason rings again. The daily
	digest still lists it."""
	from ffl_core.alerts import ALERT_ROLES, notify_staff

	reason = error if len(error) <= 120 else error[:117] + "..."
	notify_staff(doctype, name, f"A&D book: FastBound refused {doctype} {name} — {reason}", ALERT_ROLES)


def _delete_amended_predecessor(doc, *, _commit: bool) -> None:
	"""When `doc` is an amendment, delete the predecessor's FastBound record
	first so the re-push doesn't collide on the serial externalId and the bound
	book doesn't keep the pre-amendment duplicate. Best-effort: if the delete
	fails, the create below may 400 and reconcile_failed_pushes retries."""
	if not doc.amended_from:
		return
	old_fb_id = frappe.db.get_value("FFL Acquisition", doc.amended_from, "fastbound_id")
	if not old_fb_id:
		return
	try:
		FastBoundClient.from_settings().delete_acquisition(old_fb_id)
	except FastBoundError:
		return
	frappe.db.set_value("FFL Acquisition", doc.amended_from, "fastbound_id", "",
		update_modified=False)
	if _commit:
		frappe.db.commit()


def _post_acquisition_with_retry(client, doc, *, _retries: int = 3):
	"""post_acquisition with bounded backoff, retrying only TRANSIENT failures
	(network / 5xx). A 4xx rejection (e.g. a bad contact) fails fast — retrying a
	deterministic error would just keep the operator waiting."""
	for attempt in range(max(1, _retries) + 1):
		try:
			return client.post_acquisition(doc)
		except FastBoundError as exc:
			transient = exc.status_code is None or exc.status_code >= 500
			if transient and attempt < _retries:
				time.sleep(2 ** attempt)
				continue
			raise


def push_receipt_acquisitions_sync(purchase_receipt: str) -> None:
	"""Push every not-yet-synced FFL Acquisition for a Purchase Receipt to
	FastBound SYNCHRONOUSLY, inside the caller's (Receive Goods) transaction.

	This is the atomic intake path: a firearm ends up in FastBound AND our
	system, or in neither. On any rejection it deletes the sibling acquisitions
	already committed to FastBound this call (so the caller's rollback leaves no
	orphan bound-book records) and raises — which rolls back the Purchase
	Receipt + Serial Nos + stock + Acquisitions together. Wired as the
	`ffl_acquisition_sync_push` hook so ffl_core triggers it without importing
	ffl_integrations.

	No commits here (writes ride the caller's transaction). Returns quietly when
	FastBound isn't configured, so receiving still works without the integration.
	"""
	if not fastbound_enabled():
		return  # switched off — the record stays local (see module header)
	acqs = frappe.get_all(
		"FFL Acquisition",
		filters={"purchase_receipt": purchase_receipt, "docstatus": 1},
		fields=["name", "fastbound_id"])
	to_push = [a.name for a in acqs if not a.fastbound_id]
	if not to_push:
		return
	try:
		client = FastBoundClient.from_settings()
	except FastBoundConfigError:
		return  # FastBound not set up — don't block receiving.

	# FastBound acquisition ids CREATED this call. Adopted ones (serial already
	# in the book) are pre-existing records that may hold other guns, so they
	# are never compensated; worst case an adopt after our own retried POST is
	# left as an orphan the next receive re-adopts.
	pushed: list[str] = []
	for name in to_push:
		doc = frappe.get_doc("FFL Acquisition", name)
		try:
			result = _post_acquisition_with_retry(client, doc)
		except FastBoundError as exc:
			for fb_id in pushed:
				try:
					client.delete_acquisition(fb_id)
				except FastBoundError:
					frappe.log_error(
						title="push_receipt_acquisitions_sync: orphan cleanup failed",
						message=f"{fb_id}\n\n{frappe.get_traceback()}")
			frappe.throw(
				_("FastBound rejected this firearm, so nothing was received — fix "
					"the issue and receive again.\n\n{0}").format(exc),
				title=_("FastBound Acquisition Failed"))
		_record_acquisition_push(name, result)
		if not result.get("adopted"):
			pushed.append(result["acquisitionId"])


def enqueue_push_on_submit(doc, method: str | None = None) -> None:
	"""FFL Acquisition on_submit hook: enqueue the FastBound push after the
	submit commits, so a slow or failing API call never blocks the operator.
	The push task is idempotent; reconcile_failed_pushes retries failures daily."""
	if not fastbound_enabled():
		return  # switched off — the record stays local (see module header)
	if frappe.flags.get("ffl_receive_sync_push"):
		return  # Receive Goods pushes synchronously in-transaction — skip the async dup.
	if doc.fastbound_id:
		return  # imported from FastBound — already in the bound book, don't push back
	frappe.enqueue(
		"ffl_integrations.fastbound.tasks.push_acquisition_to_fastbound",
		queue="long",
		acquisition_name=doc.name,
		enqueue_after_commit=True,
	)


@frappe.task(queue="long", timeout=600)
def reconcile_failed_pushes() -> None:
	"""Re-enqueue push for every Acquisition / Disposition that's submitted but
	unsynced — EXCEPT those whose last failure was a deterministic client error
	(see _fb_error_is_terminal). Those need a human to fix the cause and manually
	re-push; re-enqueuing them daily just burns API calls and floods the stuck
	digest with permanent noise."""
	if not fastbound_enabled():
		return  # switched off — the record stays local (see module header)
	for row in unsynced_records():
		if row["terminal"]:
			continue
		method, arg = _PUSH_TASKS[row["doctype"]]
		frappe.enqueue(f"ffl_integrations.fastbound.tasks.{method}", queue="long", **{arg: row["name"]})


# doctype -> (push task, its record argument)
_PUSH_TASKS = {
	"FFL Acquisition": ("push_acquisition_to_fastbound", "acquisition_name"),
	"FFL Disposition": ("push_disposition_to_fastbound", "disposition_name"),
}


def unsynced_records() -> list[dict]:
	"""Every submitted FFL Acquisition / FFL Disposition the bound book does not have yet: no
	``fastbound_id`` (a Disposition only of a type FastBound books, PUSHABLE_DISPOSITION_TYPES).
	The set reconcile_failed_pushes works on, and the one the Overview page counts as "Not logged",
	so the two cannot disagree. ``terminal`` is a last failure that needs a person
	(_fb_error_is_terminal); the others are retried daily. Acquisitions first."""
	out: list[dict] = []
	for doctype, extra in (("FFL Acquisition", {}),
			("FFL Disposition", {"disposition_type": ("in", list(PUSHABLE_DISPOSITION_TYPES))})):
		for row in frappe.get_all(doctype, filters={"docstatus": 1, "fastbound_id": ("is", "not set"), **extra},
				fields=["name", "fastbound_sync_error", "modified"]):
			if (row.fastbound_sync_error or "").startswith(NOT_IN_BOOK_PREFIX):
				continue
			out.append({"doctype": doctype, "name": row.name, "error": row.fastbound_sync_error or "",
				"terminal": _fb_error_is_terminal(row.fastbound_sync_error), "modified": row.modified})
	return out


@frappe.task(queue="long", timeout=600)
def reconcile_failed_deletes() -> None:
	"""Retry every cancelled Acquisition / Disposition whose DELETE to FastBound
	failed (transient 5xx, network blip, etc.). Symmetric with reconcile_failed_pushes
	— without this, a delete failure during a brief outage leaves a permanent
	orphan in the FastBound bound book that we never re-attempt."""
	if not fastbound_enabled():
		return  # switched off — the record stays local (see module header)
	stale_acq = frappe.get_all(
		"FFL Acquisition",
		filters={
			"docstatus": 2,
			"fastbound_id": ("is", "set"),
			"fastbound_sync_error": ("like", "DELETE failed%"),
		},
		fields=["name", "fastbound_id"],
	)
	for row in stale_acq:
		frappe.enqueue(
			"ffl_integrations.fastbound.tasks.delete_acquisition_from_fastbound",
			queue="long", acquisition_name=row.name, fastbound_id=row.fastbound_id,
		)
	stale_disp = frappe.get_all(
		"FFL Disposition",
		filters={
			"docstatus": 2,
			"fastbound_id": ("is", "set"),
			"fastbound_sync_error": ("like", "DELETE failed%"),
		},
		fields=["name", "fastbound_id"],
	)
	for row in stale_disp:
		frappe.enqueue(
			"ffl_integrations.fastbound.tasks.delete_disposition_from_fastbound",
			queue="long", disposition_name=row.name, fastbound_id=row.fastbound_id,
		)


@frappe.task(queue="long", timeout=300)
def enqueue_delete_on_cancel(doc, method: str | None = None) -> None:
	"""FFL Acquisition on_cancel: close the bound-book loop for NON-PR
	acquisitions (trade-in / intake / import) — a cancelled entry must leave
	FastBound too, or the two books diverge (the PR-level
	delete filters by purchase_receipt, which these rows never have; Purchase
	Receipt cancellation keeps handling its own rows in
	compliance/purchase.cancel_acquisitions_for_firearm_lines, so skip those
	here to avoid a double DELETE).

	The DELETE is enqueued after commit (idempotent task; a failure lands in
	fastbound_sync_error and the daily stuck digest — including FastBound's
	refusal to delete a committed acquisition, which then needs a human in
	FastBound's UI). Amend flows stay safe: the cancel's DELETE is enqueued
	before the amendment's push, and _delete_amended_predecessor remains as
	belt-and-braces; if the record still survives, the push's adopt path
	links the existing item rather than failing.

	Stock is deliberately NOT auto-reversed — cancelling the A&D entry can
	mean "full unwind" (also cancel the Material Receipt) or "amending the
	record" (stock stays). Nudge the operator instead of guessing."""
	if not fastbound_enabled():
		return  # switched off — the record stays local (see module header)
	if doc.purchase_receipt:
		return
	if doc.fastbound_id:
		frappe.enqueue(
			"ffl_integrations.fastbound.tasks.delete_acquisition_from_fastbound",
			queue="long",
			acquisition_name=doc.name,
			fastbound_id=doc.fastbound_id,
			enqueue_after_commit=True,
		)
	if doc.serial and frappe.db.get_value("Serial No", doc.serial, "status") == "Active":
		frappe.msgprint(
			frappe._(
				"Serial {0} is still in stock. If this gun is leaving the books "
				"entirely, cancel its Material Receipt too; if you are amending "
				"the record, submit the amendment now."
			).format(doc.serial),
			indicator="orange", alert=True,
		)


def alert_stale_trade_in_drafts() -> None:
	"""Hourly: warn staff about parked draft trade-in acquisitions that missed
	same-day booking. The POS trade-in dialog parks a DRAFT FFL Acquisition
	and the checkout submit books it — but §478.125(e)'s bound-book clock runs
	from PHYSICAL POSSESSION ("not later than the close of the next business
	day"), independent of the buyer-side 4473/NICS. A delayed-NICS checkout
	must not silently carry an unbooked gun past that line. Fires STRICTER
	than the legal deadline — any draft whose possession date
	(acquisition_date) is before today — so staff still have the whole next
	business day to act: complete the checkout, submit the acquisition
	directly (the store keeps the gun regardless of the sale), or delete the
	draft (the customer took the gun back). Lives here rather than ffl_core
	because the admin-digest plumbing (notify_admins dedup) does."""
	today = frappe.utils.getdate()
	rows = frappe.get_all(
		"FFL Acquisition",
		filters={"docstatus": 0,
			"source_type": ("in", ("Trade-in", "Individual", "Estate", "Pawn"))},
		fields=["name", "serial", "acquisition_date", "source_name"],
	)
	stale = [r for r in rows
		if r.acquisition_date and frappe.utils.getdate(r.acquisition_date) < today]
	if not stale:
		return
	lines = [
		f"{r.name}: serial {r.serial or '?'} from {r.source_name or '?'} — "
		f"possession {frappe.utils.getdate(r.acquisition_date)}, still unbooked"
		for r in stale
	]
	notify_admins(
		f"{len(stale)} trade-in draft(s) not yet in the bound book",
		"Bound-book entries are due by the close of the NEXT business day "
		"after taking possession (27 CFR 478.125(e)). For each draft below: "
		"complete the checkout, or submit the acquisition directly if the "
		"store keeps the gun, or delete the draft if the customer took it "
		"back.\n\n" + "\n".join(lines),
		dedup_key="trade_in_drafts_stale",
		window_minutes=720,  # at most twice a day
	)


def alert_stuck_fastbound() -> None:
	"""Daily: warn staff about A&D records stuck unsynced to FastBound. A push
	that keeps failing never clears fastbound_sync_error and has no dead-letter
	state (reconcile re-enqueues forever), so a non-empty fastbound_sync_error is
	the real 'stuck' signal."""
	if not fastbound_enabled():
		return  # switched off — the record stays local (see module header)
	stuck: list[str] = []
	for dt in ("FFL Acquisition", "FFL Disposition"):
		rows = frappe.get_all(
			dt, filters={"fastbound_sync_error": ("!=", "")},
			fields=["name", "fastbound_sync_error", "docstatus", "fastbound_id"])
		for r in rows:
			# A cancelled record that never synced (no fastbound_id) has no bound-book
			# obligation — its stale PUSH error shouldn't haunt the digest forever.
			# (A cancelled record WITH a fastbound_id has a pending DELETE and stays.)
			if r.docstatus == 2 and not r.fastbound_id:
				continue
			if r.fastbound_sync_error.startswith(NOT_IN_BOOK_PREFIX):
				continue  # checked by a person: not a bound-book record
			# Distinguish "won't auto-retry, fix me" from "transient, retrying" so
			# a genuinely-actionable divergence stands out in the digest.
			tag = "[needs human] " if _fb_error_is_terminal(r.fastbound_sync_error) else ""
			stuck.append(f"{tag}{dt} {r.name}: {(r.fastbound_sync_error or '')[:200]}")
	if not stuck:
		return
	notify_admins(
		f"{len(stuck)} FastBound record(s) stuck unsynced",
		"\n".join(stuck),
		dedup_key="fastbound_stuck",
		window_minutes=1440,  # at most one digest per day
	)


@frappe.task(queue="long", timeout=60)
def delete_acquisition_from_fastbound(
	acquisition_name: str,
	fastbound_id: str,
	*,
	_retries: int = 3,
	_commit: bool = True,
) -> None:
	"""DELETE the cancelled acquisition on FastBound. Retries on FastBoundError
	(transient 5xx / network blip); persisting failures land in
	fastbound_sync_error AND are retried daily by reconcile_failed_deletes()."""
	if not fastbound_enabled():
		return  # switched off — the record stays local (see module header)
	last_error: str | None = None
	for attempt in range(max(1, _retries) + 1):
		try:
			FastBoundClient.from_settings().delete_acquisition(fastbound_id)
			# Clear the id so the row no longer claims a remote counterpart.
			frappe.db.set_value("FFL Acquisition", acquisition_name, {
				"fastbound_id": "",
				"fastbound_sync_error": "",
			}, update_modified=False)
			if _commit:
				frappe.db.commit()
			return
		except FastBoundError as exc:
			last_error = str(exc)
			if attempt < _retries:
				time.sleep(2 ** attempt)
				continue
			break
	frappe.db.set_value("FFL Acquisition", acquisition_name,
		"fastbound_sync_error", f"DELETE failed: {last_error or 'unknown error'}",
		update_modified=False)
	if _commit:
		frappe.db.commit()


# ==================================================== Disposition push (non-4473)

@frappe.task(queue="long", timeout=120)
def push_disposition_to_fastbound(
	disposition_name: str,
	*,
	_retries: int = 3,
	_commit: bool = True,
) -> None:
	"""Push one non-4473 FFL Disposition to FastBound. Idempotent (no-op when
	already synced or not a pushable type). Retries on FastBoundError."""
	if not fastbound_enabled():
		return  # switched off — the record stays local (see module header)
	doc = frappe.get_doc("FFL Disposition", disposition_name)
	# Submit-then-cancel race: enqueue_after_commit means the push runs AFTER
	# the on_submit commit. A fast Cancel in between leaves the doc at
	# docstatus=2 by the time the worker dequeues — pushing then would stamp
	# fastbound_id onto a cancelled row, leaving an orphan FastBound record
	# that the reconciler (docstatus:1 filter) never touches.
	if doc.docstatus != 1:
		return
	if doc.fastbound_id:
		return  # already in the bound book (e.g. a 4473 disposition from the webhook)
	if doc.disposition_type not in PUSHABLE_DISPOSITION_TYPES:
		return  # 4473 / to-individual / unsupported — not auto-pushed
	if _off_book("FFL Disposition", disposition_name):
		return  # a person marked it off the bound book

	last_error: str | None = None
	last_terminal = False
	for attempt in range(max(1, _retries) + 1):
		try:
			client = FastBoundClient.from_settings()
			fb_id = client.post_disposition(doc)
			frappe.db.set_value("FFL Disposition", disposition_name, {
				"fastbound_id": fb_id,
				"fastbound_synced_at": now_datetime(),
				"fastbound_sync_error": "",
			}, update_modified=False)
			if _commit:
				frappe.db.commit()
			return
		except FastBoundError as exc:
			last_error = str(exc)
			last_terminal = bool(getattr(exc, "terminal", False))
			# Terminal failures (already disposed / can't auto-adopt) won't change
			# on retry — fail fast; everything else backs off and retries.
			if last_terminal or attempt >= _retries:
				break
			time.sleep(2 ** attempt)

	sync_error = (_TERMINAL_PREFIX if last_terminal else "") + (last_error or "unknown error")
	if _record_push_error("FFL Disposition", disposition_name, sync_error) and last_terminal:
		_notify_terminal("FFL Disposition", disposition_name, last_error or "")
	if _commit:
		frappe.db.commit()


def enqueue_disposition_push_on_submit(doc, method: str | None = None) -> None:
	"""FFL Disposition on_submit hook: enqueue the FastBound push (after commit)
	for the non-4473 dispositions we own. 4473 dispositions arrive from the
	webhook with fastbound_id already set and are skipped; Retail Sale / Sale /
	to-individual types aren't pushable and are skipped too."""
	if not fastbound_enabled():
		return  # switched off — the record stays local (see module header)
	if doc.fastbound_id:
		return
	if doc.disposition_type not in PUSHABLE_DISPOSITION_TYPES:
		return
	frappe.enqueue(
		"ffl_integrations.fastbound.tasks.push_disposition_to_fastbound",
		queue="long",
		disposition_name=doc.name,
		enqueue_after_commit=True,
	)


@frappe.task(queue="long", timeout=60)
def delete_disposition_from_fastbound(
	disposition_name: str,
	fastbound_id: str,
	*,
	_retries: int = 3,
	_commit: bool = True,
) -> None:
	"""DELETE the cancelled disposition on FastBound. Retries on FastBoundError
	(transient 5xx / network blip); persisting failures land in
	fastbound_sync_error AND are retried daily by reconcile_failed_deletes()."""
	if not fastbound_enabled():
		return  # switched off — the record stays local (see module header)
	last_error: str | None = None
	for attempt in range(max(1, _retries) + 1):
		try:
			client = FastBoundClient.from_settings()
			client.delete_disposition(fastbound_id)
			_ensure_undisposed(client, disposition_name)
			frappe.db.set_value("FFL Disposition", disposition_name, {
				"fastbound_id": "",
				"fastbound_sync_error": "",
			}, update_modified=False)
			if _commit:
				frappe.db.commit()
			return
		except FastBoundError as exc:
			last_error = str(exc)
			if attempt < _retries:
				time.sleep(2 ** attempt)
				continue
			break
	frappe.db.set_value("FFL Disposition", disposition_name,
		"fastbound_sync_error", f"DELETE failed: {last_error or 'unknown error'}",
		update_modified=False)
	if _commit:
		frappe.db.commit()


def _ensure_undisposed(client, disposition_name: str) -> None:
	"""FastBound's DELETE /Dispositions/{id} removes the disposition record but
	can leave its item status=Disposed with a dangling dispositionId (observed
	2026-07-07: DISP-2026-00986). Verify the item landed back in inventory and
	undispose it if not. Raises FastBoundError on API failure so the caller's
	retry loop + 'DELETE failed:' marker (reconcile_failed_deletes) cover this
	leg exactly like the DELETE itself."""
	serial = frappe.db.get_value("FFL Disposition", disposition_name, "serial")
	if not serial:
		return
	item_id = frappe.db.get_value("Serial No", serial, "fastbound_item_id") \
		or client.get_item_id_by_serial(serial)
	if not item_id:
		return
	item = client.get_item(item_id)
	if item and client._item_is_disposed(item):
		client.undispose_item(item_id, note=(
			f"POS disposition {disposition_name} cancelled — "
			"returning item to inventory."))


def cancel_disposition_on_cancel(doc, method: str | None = None) -> None:
	"""FFL Disposition on_cancel: if WE pushed it (non-4473, has fastbound_id),
	delete the FastBound record. Never touches 4473 dispositions — those are
	committed bound-book entries owned by FastBound's 4473 flow."""
	if not fastbound_enabled():
		return  # switched off — the record stays local (see module header)
	if doc.fastbound_id and doc.disposition_type in PUSHABLE_DISPOSITION_TYPES:
		frappe.enqueue(
			"ffl_integrations.fastbound.tasks.delete_disposition_from_fastbound",
			queue="long",
			disposition_name=doc.name,
			fastbound_id=doc.fastbound_id,
			enqueue_after_commit=True,
		)


# ============================================ Post-acquisition item correction

@frappe.whitelist()
def push_serial_to_fastbound(serial: str) -> dict:
	"""Push a post-acquisition manufacturer / importer correction for ONE
	firearm to FastBound, editing the bound-book item in place (PUT /Items/{id})
	— no delete/recreate, FastBound audit-logs the edit.

	Called synchronously from the Serial No form's "sync to FastBound?" confirm
	after an operator edits a per-unit manufacturer / importer.

	Args:
	    serial: Serial No name (must carry a fastbound_item_id).

	Returns:
	    {"ok": True, "item_id": ...} on success; {"ok": False, "reason":
	    "not_in_fastbound" | "not_configured"} for the expected not-pushed
	    cases (rather than raising) so the form can show a tidy message.
	    A store running with FastBound switched off reports "not_configured"
	    too — from the operator's side it is the same fact, and the edit they
	    just made is saved locally either way.

	Raises:
	    FastBoundError: Genuine API failures.
	"""
	frappe.has_permission("Serial No", "write", throw=True)
	serial = (serial or "").strip()
	if not serial:
		frappe.throw(_("No serial number provided."))
	# The form already enforces write to save; re-check here — this is a
	# whitelisted endpoint that mutates the bound book.
	frappe.has_permission("Serial No", "write", doc=serial, throw=True)

	sn = frappe.db.get_value(
		"Serial No", serial, ["item_code", "fastbound_item_id"], as_dict=True,
	)
	if not sn:
		frappe.throw(_("Serial No {0} not found.").format(serial))
	if not sn.fastbound_item_id:
		# Never pushed to FastBound yet (acquisition unsynced) — nothing to edit.
		# The eventual acquisition push will carry the current per-serial values.
		return {"ok": False, "reason": "not_in_fastbound"}

	if not fastbound_enabled():
		return {"ok": False, "reason": "not_configured"}
	try:
		client = FastBoundClient.from_settings()
	except FastBoundConfigError:
		return {"ok": False, "reason": "not_configured"}

	client.update_item(
		sn.fastbound_item_id,
		FastBoundClient._firearm_item_fields(sn.item_code, serial),
	)
	return {"ok": True, "item_id": sn.fastbound_item_id}
