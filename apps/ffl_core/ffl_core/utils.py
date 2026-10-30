"""Shared low-level helpers for the FFL apps."""
from __future__ import annotations

import random
import string
from contextlib import contextmanager

import frappe
from frappe.utils import cint


def split_serials(raw: str | None) -> list[str]:
	"""Split an ERPNext ``serial_no`` field into individual serials.

	A serialized line with qty>1 carries its serials NEWLINE-joined (ERPNext
	core convention, e.g. ``"J4940\\n7805C"``). The 4473 / A&D pipeline assumes
	one gun per line and elsewhere read the whole field as a single serial —
	that is the past invoice bug (a completed 4473 could not match a
	two-gun line, and a manager override booked a disposition whose ``serial``
	held the newline-joined string).

	The single home for that split so every call site behaves identically:
	split on newline, strip each token (handles a trailing ``\\r`` from CRLF),
	drop blanks. Mirrors ERPNext's ``get_serial_nos`` convention WITHOUT
	uppercasing — our serials are matched case-as-stored everywhere else, so
	folding case here would silently break lookups.
	"""
	return [s.strip() for s in (raw or "").split("\n") if s.strip()]


def default_company() -> str:
	"""The Company channel revenue / drop-ship cost books to: ERPNext's default,
	else the first Company. Throws when the site has none."""
	import erpnext

	company = erpnext.get_default_company() or (
		frappe.get_all("Company", pluck="name", limit=1) or [None])[0]
	if not company:
		frappe.throw(frappe._("No Company exists."))
	return company


def commit() -> None:
	"""`frappe.db.commit()` that is a no-op inside the test transaction.

	Frappe's ``IntegrationTestCase`` (what ``FrappeTestCase`` resolves to) rolls
	back the whole test class ONCE, at ``tearDownClass``, and ``frappe.db.rollback``
	rewinds only to the LAST commit. So any real commit executed while a test class
	runs moves that rollback floor forward — permanently persisting every row
	created before it and poisoning the next run on the same site
	(``SerialNoDuplicate`` / "already on file" / serial-without-stock →
	``NegativeStock``). Frappe flags this itself via ``_commit_watcher``
	("Transaction committed during tests").

	Setup ensures (``dev_site``/``*.ensure``/``test_bootstrap``) and the background
	persists they gate legitimately commit in prod and during ``bench migrate`` /
	``bench execute`` — where ``frappe.flags.in_test`` is False — but must NOT commit
	when reached inside the test runner (from ``setUpClass``, or lazily from
	code-under-test). Route those commits through here.

	Do NOT convert call sites that already thread an explicit transaction-boundary
	flag (e.g. the importer's ``commit_each``) — those hand the caller control and
	are correct as-is.

	When the call site has a local ``commit`` in scope (e.g. the importer's
	``commit = bool(int(commit_each))``), import this aliased —
	``from ffl_core.utils import commit as safe_commit`` — so the helper isn't
	shadowed by the local name (see ffl_integrations/fastbound/importer.py).
	"""
	if frappe.flags.in_test:
		return
	frappe.db.commit()


def single_flag(doctype: str, fieldname: str, *, default: int = 0) -> bool:
	"""Read a Check field off a Single doctype, honouring the docfield default on
	a site that has never saved that field.

	``frappe.db.get_single_value`` casts a MISSING ``tabSingles`` row to 0 for a
	Check field. So shipping a new "Enable X" switch whose docfield default is 1
	reads back OFF on every existing site until somebody opens the form and saves
	it — the migrate that adds the switch would silently DISABLE X in production.
	Telling "never set" apart from "explicitly unchecked" needs the raw row, which
	is the whole reason this helper exists. (``patches/set_integration_switch_
	defaults`` also writes the value so the FORM shows the true state; this keeps
	the READ correct whether or not that patch has run yet.)

	Reads ``tabSingles`` directly for the same reason ``firearm.is_own_license``
	does: ``get_single_value`` resolves the fieldtype through ``frappe.get_meta``,
	and on a cold meta cache that loads the DocType through the ORM — which blows
	up any caller whose tests patch ``frappe.get_all`` / ``frappe.get_doc``.
	"""
	return _flag_from_raw(single_raw(doctype, fieldname), default)


def _flag_from_raw(raw: str | None, default: int) -> bool:
	"""The shared core of :func:`single_flag` and :func:`single_flags`.

	"Never stored" and "stored empty" both fall back to the docfield default;
	anything else is the stored value.
	"""
	if raw is None or str(raw).strip() == "":
		return bool(default)
	return bool(cint(raw))


def single_raw_map(doctype: str) -> dict[str, str]:
	"""Every stored ``tabSingles`` value for one Single, in ONE query.

	The batched form of :func:`single_raw`, for a caller that needs several
	switches at once — a listing payload reads eleven of them, and eleven
	round trips to answer eleven questions about the same row set is silly.

	Only fields that have ever been saved appear, which is the whole point: a
	key missing from this dict is a field nobody has stated a value for.
	"""
	# NEVER order this query. `tabSingles` is (doctype, field, value) with one
	# non-unique index on (doctype, field) and no key of any kind — no `name`, no
	# `creation`, no primary key. An `ORDER BY name` here shipped once and raised
	# `1054 Unknown column 'name'` on EVERY settings read in the codebase: migrate
	# dead (the after_migrate hook reads this), the FastBound gate throwing on
	# every gun received. Frappe's own two readers order it no more than we do.
	#
	# Which row wins is therefore settled in Python, and it is FIRST-row-wins —
	# here (`setdefault`) and in `single_raw` (`row[0][0]`), matching
	# `frappe.db.get_single_value`, the framework reader this family shadows.
	# Frappe is not self-consistent about this and we are deliberately not
	# copying the other half: `get_singles_dict` / `get_values_from_single` fold
	# through `_dict(rows)`, i.e. LAST row wins. We align with `get_single_value`
	# because that is the per-field reader `single_raw` stands in for, so the one
	# comparison anybody will ever make — ours against the framework's, on the
	# same field — comes out equal. That
	# is what makes `single_flags` "exactly single_flag for each key" true by
	# construction. It is belt-and-braces regardless: both framework writers
	# (`Document.update_single`, `frappe.db.set_single_value`) DELETE the rows they
	# are about to write before inserting, so a second row for one field cannot
	# arise. `ffl_core/tests/test_single_reads.py` pins the schema and the
	# one-row invariant, on the bench, where a stubbed `frappe.db` cannot.
	rows = frappe.db.sql(
		"SELECT `field`, `value` FROM `tabSingles` WHERE `doctype` = %s", (doctype,))
	out: dict[str, str] = {}
	for field, value in (rows or []):
		out.setdefault(field, value)
	return out


def single_flags(doctype: str, defaults: dict[str, int]) -> dict[str, bool]:
	"""Read several Check fields off one Single, honouring their docfield defaults.

	Exactly :func:`single_flag` for each key, in one query instead of N.

	Use this — not ``frappe.get_single(...).get(field)`` — for any Check whose
	default matters. A Single applies its docfield defaults only while it has no
	stored rows at all, and `BaseDocument._fix_numeric_types` then casts every
	Check through `cint()` UNCONDITIONALLY, so `None` arrives as `0`: a document
	read cannot tell "unchecked" from "never saved" for a Check field, at all.
	The raw row can, which is the reason this family of helpers exists.

	Args:
	    doctype: The Single to read.
	    defaults: ``{fieldname: docfield default}`` — pass the same defaults the
	        DocType JSON declares, or this quietly reintroduces the bug it exists
	        to prevent.

	Returns:
	    ``{fieldname: bool}`` for every key in ``defaults``.
	"""
	raw = single_raw_map(doctype)
	return {field: _flag_from_raw(raw.get(field), default)
		for field, default in defaults.items()}


def single_raw(doctype: str, fieldname: str) -> str | None:
	"""The stored ``tabSingles`` value for a Single's field, or None if no row exists.

	The whole point is that distinction: a Single stores one row per field that has
	ever been SAVED, so "never written" and "explicitly empty/zero" are different
	states — and every switch in this codebase depends on telling them apart
	(``single_flag`` above, the patch that seeds switch defaults, the test helper
	that restores them). Reading them three different ways would give three chances
	to get it wrong.

	Direct SQL rather than an ORM read for the reason spelled out on
	``firearm.is_own_license``: ``get_single_value`` resolves the fieldtype through
	``frappe.get_meta``, and on a cold meta cache that loads the DocType through the
	ORM, which blows up any caller whose tests patch ``frappe.get_all`` /
	``frappe.get_doc``.
	"""
	# First row wins, unordered — the same fold and the same query shape as
	# `frappe.db.get_single_value`, and the same one `single_raw_map` folds to.
	# See its comment for why this table has nothing to order by and why one row
	# per field is an invariant the writers keep rather than a hope.
	row = frappe.db.sql(
		"SELECT `value` FROM `tabSingles` WHERE `doctype` = %s AND `field` = %s",
		(doctype, fieldname))
	return row[0][0] if row else None


def set_file_privacy(
	file_url: str | None, *, private: bool, verify_attached_field: bool = False
) -> str | None:
	"""Flip the File behind ``file_url`` private/public; return its current ``file_url``.

	The ONE home for File visibility changes — ``receive_goods._ensure_private_file``
	forces Gov-ID scans private, ``ffl_woo_sync.woocommerce.mapping._publicize_file``
	makes product photos public — so an audit hook or permission rule added here
	covers both directions.

	``File.save`` does the work: Frappe's ``handle_is_private_changed`` moves the
	bytes between ``/private/files`` and ``/files``, rewrites ``file_url`` on this
	row and on every duplicate row sharing its content, and — when the row carries
	``attached_to_field`` — rewrites that field on the parent document too (with
	``modified`` bumped).  Child-table rows are never touched.

	``verify_attached_field=True`` makes that parent rewrite conditional on the
	field actually holding this URL.  A File row's ``attached_to_field`` is
	whatever fieldname the uploader passed — for a child-table (gallery) upload
	that is the CHILD fieldname, which may collide with a parent field of the
	same name (``image``), and Frappe then rewrites the parent field with this
	file's URL whether or not it held it (``File.handle_is_private_changed`` →
	``fetch_attached_to_field`` trusts the field name, file.py:379-382).  Blanking
	the field for the save makes Frappe scan the parent's top-level values for
	the old URL instead, so only a field that really holds it is rewritten; the
	metadata is restored afterwards.  Leave it off for single-field attachments
	(ID scans), where the trust is warranted.

	Returns ``None`` when no File row carries ``file_url``; callers decide whether
	that is tolerable (an ID scan: carry on) or fatal (a photo about to be
	published: fail loudly).  Disk / permission failures propagate as Frappe raises
	them (``FileNotFoundError``, ``FileExistsError``, ``frappe.ValidationError``).
	"""
	url = (file_url or "").strip()
	if not url:
		return None
	name = frappe.db.get_value("File", {"file_url": url}, "name")
	if not name:
		return None
	file_doc = frappe.get_doc("File", name)
	if bool(file_doc.is_private) == bool(private):
		return file_doc.file_url
	file_doc.is_private = 1 if private else 0
	if not verify_attached_field:
		file_doc.save(ignore_permissions=True)
		return file_doc.file_url
	original_field = file_doc.attached_to_field
	file_doc.attached_to_field = None
	file_doc.save(ignore_permissions=True)
	if original_field and file_doc.attached_to_field != original_field:
		file_doc.db_set("attached_to_field", original_field, update_modified=False)
	return file_doc.file_url


@contextmanager
def guest_job_elevated():
	"""Run the block as Administrator when the session is ``Guest`` — i.e. a
	background job enqueued from an ``allow_guest`` webhook.

	``frappe.enqueue`` captures ``frappe.session.user`` and the worker
	``set_user``s it back, so a job queued by the WooCommerce webhook runs as
	Guest. Our own inserts carry ``ignore_permissions``, but ERPNext's helpers
	do their own checks — ``get_payment_entry`` → ``get_reference_details`` →
	``frappe.has_permission("Sales Invoice", throw=True)`` — and refuse Guest.
	That refusal rolled back the whole revenue job for every accessory-only web
	order (2026-08-22): no Sales Invoice, no payment, no
	retry, one Error Log nobody saw.

	A real user's session is left alone — their own roles decide (the
	``create_web_invoice_now`` style entry points already role-gate). The
	previous user is restored on exit, exception included. Same shape as the
	inline prev_user/set_user/finally blocks in ``tasks.rebucket_and_apply_
	serial_task`` and ``osa_consignment.hooks_impl.settlement``; this is the shared home
	for job entry points that may be reached from a Guest context.
	"""
	prev = frappe.session.user
	if prev != "Guest":
		yield
		return
	frappe.set_user("Administrator")
	try:
		yield
	finally:
		frappe.set_user(prev)


@contextmanager
def atomic(label: str):
	"""Undo every database write inside the block when it raises, then re-raise.

	A SQL savepoint, so the undo does not depend on WHO called the code. Frappe
	rolls the transaction back for you when a request handler throws, and when a
	background job throws — but not for a `bench console` session, a
	`bench execute`, a `bench --site … run-tests` body, or any script that
	catches the exception itself. Multi-step writes that assumed the request
	rollback therefore left half-finished state behind for exactly the callers
	an operator reaches for when something has already gone wrong.

	Wrap the steps that must land together::

	    with atomic("consignment ship line"):
	        move_the_stock()
	        book_the_bound_book_entry()   # throws -> the move is undone

	NOT ``frappe.database.database.savepoint``, which looks like this helper and
	is not: it SWALLOWS the exception it catches (`except catch: rollback` with
	no re-raise), so the caller sees a silent no-op instead of a failure. That is
	the right shape for "insert these rows, skipping duplicates" and the wrong
	shape for anything a human has to be told about. Here the exception always
	propagates; only the writes are taken back.

	Nesting is fine (savepoints stack). The block still participates in the
	surrounding transaction: this rolls back to the savepoint, it never commits,
	and a caller that commits mid-block has published those writes and put them
	beyond any undo — so do not commit inside one.

	Args:
	    label: What the block does, for the log line if the undo itself fails.
	"""
	save_point = "ffl_" + "".join(random.sample(string.ascii_lowercase, 10))
	frappe.db.savepoint(save_point)
	try:
		yield
	except Exception:
		try:
			frappe.db.rollback(save_point=save_point)
		except Exception:
			# The savepoint is gone because the WHOLE transaction was already
			# unwound (a deadlock or lock-wait timeout does that). Nothing is
			# left to undo, which is the outcome we wanted anyway — but say so,
			# because it means the failure below is not the whole story.
			frappe.log_error(
				title=f"atomic({label}): savepoint rollback failed",
				message=frappe.get_traceback())
		raise
	else:
		frappe.db.release_savepoint(save_point)


def set_missing_values_keeping_taxes(doc) -> None:
	"""``doc.set_missing_values()``, with the caller's tax decision left alone.

	"Tax decision" means BOTH ``taxes_and_charges`` and the ``taxes`` rows,
	because ``set_pos_fields`` rewrites them in that order on the same call:

	* it copies a fixed field list off the POS Profile, and on the
	  ``for_validate=False`` pass — the one ``set_missing_values()`` makes — the
	  profile wins unconditionally, an EMPTY profile value included::

	      for fieldname in ("currency", …, "taxes_and_charges", …):
	          if (not for_validate) or (for_validate and not self.get(fieldname)):
	              self.set(fieldname, pos.get(fieldname))

	* then, at the end of the same method, it fills an empty taxes table from
	  the template it has just written over ours (sales_invoice.py)::

	      # fetch charges
	      if self.taxes_and_charges and not len(self.get("taxes")):
	          self.set_taxes()

	Restoring only the field is worse than restoring neither: the document then
	names one template while carrying another's rows, and
	``compliance.interstate_tax.apply_interstate_tax`` — which decides whether to
	rebuild the rows by looking at the NAME — sees the name it wanted and leaves
	the wrong rows in place. That is a real out-of-state order billed Texas sales
	tax, so the two are restored together or not at all.

	Both directions have a caller here. ``api/manual_order.create_order`` pins the
	tax decision (default template, out-of-state template, or nothing at all for
	an exempt buyer) and then reruns ``set_missing_values()`` on the paid-in-full
	(is_pos) path. ``ffl_woo_sync/woocommerce/pickup`` pins the template EMPTY and
	the rows to WooCommerce's own figures, so that neither this call nor a later
	POS-UI touch can recompute tax over what the buyer was actually charged.

	Restores whatever was there BEFORE the call, blank included — a caller that
	pinned it empty made a decision, and "put it back only if it was blanked"
	would silently ignore that half. Use it wherever the caller owns the tax
	decision; a caller that genuinely wants the profile's or the customer's
	default should keep calling ``set_missing_values()`` directly.

	ERPNext core is never patched (upgrade-safe plugin rule), so the caller
	re-asserts what it owns — the same shape as
	``manual_order._repin_economics`` re-pinning rate/discount after the same call
	for the same reason.
	"""
	template = doc.get("taxes_and_charges")
	# A copy of the LIST, holding the original child rows: restoring these keeps
	# each row's `name`, so a saved document updates its rows instead of deleting
	# and re-inserting them.
	rows = list(doc.get("taxes") or [])
	doc.set_missing_values()
	doc.taxes_and_charges = template
	doc.set("taxes", rows)
