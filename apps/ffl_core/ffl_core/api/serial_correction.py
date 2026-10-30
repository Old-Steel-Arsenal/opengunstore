"""Serial-number correction API.

Productizes the manual bench-console fix (rename an in-stock firearm's Serial No,
repoint its already-submitted A&D, and edit the matching FastBound bound-book
item) into a single manager-gated, atomic whitelisted action — the desk button
behind `public/js/serial_no.js` and the Item form's Serial Numbers tab.

Scope: IN-STOCK, unsold firearms only —
  - Serial No.status == "Active", AND
  - no committed (docstatus=1) FFL Disposition references the serial.
A sold / disposed gun's 4473 is archived under the OLD serial, and
FFL Disposition.serial is an Autocomplete *string* (not a Link) that
`rename_doc` would NOT cascade — so both are hard-rejected here.

Atomicity: every check runs BEFORE any write; the writes run inside ONE DB
savepoint in this order — rename the Serial No (cascades all Link references:
Serial and Batch Entry / bundle / SLE → stock stays consistent), repoint the
submitted A&D.serial (Data field, no cascade; set_value bypasses the submit lock
and does NOT re-trigger the FastBound push, which would double-book the A&D),
write the audit trail, then edit the FastBound item. A FastBound failure rolls
the savepoint back and re-raises, so ERP and the bound book never diverge. Only
the narrow FastBound-ok-then-commit-fail window remains. The Woo
re-push is enqueued after commit, never for a rolled-back correction.

Precedent for the rename+repoint half: ffl_integrations.fastbound.importer
._resync_existing (the FastBound→POS serial-correction path).
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime


@frappe.whitelist()
def correct_serial(serial: str, new_serial: str, reason: str) -> dict:
	"""Rename an in-stock firearm's serial and sync FastBound, atomically.

	All validation runs before any write; a FastBound failure rolls
	everything back.

	Args:
	    serial: Current Serial No name (must be an in-stock firearm).
	    new_serial: Corrected serial number.
	    reason: Required free-text audit reason.

	Returns:
	    {ok, old_serial, new_serial, fastbound_item_id} — fastbound_item_id is
	        "" on a store running with the FastBound integration switched off,
	        where the local rename is the whole correction.

	Raises:
	    ValidationError / PermissionError / FastBoundError: With no partial
	        write.

	Permissions:
	    Stock Manager / System Manager (aligned with intake).
	"""
	# Permission gate FIRST — a whitelisted method that mutates the bound book must
	# never be reachable by an ordinary logged-in user. Throws before any read.
	frappe.only_for(("Stock Manager", "System Manager"))

	old = str(serial or "").strip()
	new = str(new_serial or "").strip()
	reason = (reason or "").strip()

	# -------------------------------------------------- validation (pure DB first)
	if not old:
		frappe.throw(_("The current serial number is required."))
	if not frappe.db.exists("Serial No", old):
		frappe.throw(_("Serial No {0} not found.").format(old))
	if not new:
		frappe.throw(_("The corrected (new) serial number is required."))
	if not reason:
		frappe.throw(_("A correction reason is required — it is written to the audit trail."))
	if new == old:
		frappe.throw(_("The corrected serial is identical to the current one — nothing to change."))
	if frappe.db.exists("Serial No", new):
		frappe.throw(_(
			"Serial No {0} already exists — it names a different firearm. Resolve the "
			"conflict manually; this tool will not merge two guns."
		).format(new))

	sn = frappe.db.get_value(
		"Serial No", old,
		["status", "item_code", "fastbound_item_id", "woo_product_id"],
		as_dict=True,
	)
	if (sn.status or "") != "Active":
		frappe.throw(_(
			"Serial No {0} is '{1}', not Active (in stock). Serial correction is only "
			"supported for in-stock, unsold firearms."
		).format(old, sn.status or "—"))

	# A committed disposition means the gun has left the book under the OLD serial
	# (its 4473 is archived to it) and FFL Disposition.serial is a plain string
	# rename would not cascade — out of scope, hard reject.
	if frappe.db.exists("FFL Disposition", {"serial": old, "docstatus": 1}):
		frappe.throw(_(
			"Serial No {0} has a committed FFL Disposition — it has been sold or "
			"transferred. Correcting a disposed firearm's serial is out of scope."
		).format(old))

	# A gun on an open counter order / parked POS sale or reserved by a sales channel is
	# held by its serial TEXT on that order (rename_doc cascades Link fields only): a
	# rename would drop the hold, relist the gun and leave the order pointing at nothing.
	from ffl_core.reservations import channel_reservation, counter_holds
	hold = (counter_holds(sn.item_code).get(old) if sn.item_code else None) or channel_reservation(old)
	if hold:
		frappe.throw(_(
			"Serial No {0} is held on {1}. Finish it or take the gun off that order first, "
			"then correct the serial."
		).format(old, hold.get("ref") or hold.get("tooltip") or _("an open order")))

	# -------------------------------------------------- validation (FastBound last)
	# Resolve the bound-book item BEFORE touching ERP: if FastBound is unreachable
	# or the item can't be located, we must not rename locally and then fail to
	# sync (that is the exact ERP/FastBound divergence this feature exists to avoid).
	from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError
	from ffl_integrations.integration_gate import fastbound_enabled

	# A store running WITHOUT FastBound has no bound-book item to keep in step, so
	# there is nothing to desync and the local rename IS the whole correction. The
	# refusals below exist to stop ERP and the bound book drifting apart; applying
	# them where no bound book exists would only leave a mistyped serial permanently
	# uncorrectable — and the local A&D would then be the record carrying the wrong
	# number.
	client = None
	fb_item_id = ""
	if fastbound_enabled():
		try:
			client = FastBoundClient.from_settings()
		except FastBoundConfigError:
			frappe.throw(_(
				"FastBound is not configured, so the correction cannot be synced to the "
				"bound book. Configure FastBound Settings first, or switch the "
				"integration off if this store does not use it."
			))
		fb_item_id = (sn.fastbound_item_id or "").strip() or client.get_item_id_by_serial(old)
		if not fb_item_id:
			frappe.throw(_(
				"This firearm has no FastBound bound-book item (serial {0} is not in "
				"FastBound). Correcting the serial here would desync ERP from the bound "
				"book — resolve the FastBound link first."
			).format(old))

	# Every submitted A&D carrying the old serial (normally one). Data field, so the
	# rename does not cascade — repoint each explicitly (mirrors the importer path).
	acqs = frappe.get_all(
		"FFL Acquisition", filters={"serial": old, "docstatus": 1}, pluck="name",
	)

	user = frappe.session.user
	stamp = now_datetime().strftime("%Y-%m-%d %H:%M")
	audit_line = _("Serial corrected {0} → {1}: {2} ({3}, {4})").format(
		old, new, reason, user, stamp)
	fb_note = f"Serial corrected {old}→{new}: {reason} ({user}, {stamp})"

	# -------------------------------------------------- atomic write (one savepoint)
	savepoint = "serial_correction"
	frappe.db.savepoint(savepoint)
	try:
		# 1) Rename the Serial No — cascades every Link reference (Serial and Batch
		#    Entry / bundle / SLE) so stock stays consistent. force=True because
		#    Serial No sets allow_rename=0. show_alert=False: the desk shows its own.
		frappe.rename_doc("Serial No", old, new, force=True, show_alert=False)

		# 2) Repoint the submitted A&D (Data field) + append the audit line. Direct
		#    set_value bypasses the submit lock and never re-pushes FastBound.
		for acq in acqs:
			frappe.db.set_value("FFL Acquisition", acq, "serial", new, update_modified=False)
			_append_acq_note(acq, audit_line)

		# 3) Timeline comment on the renamed Serial No.
		note = audit_line
		if client is None and (sn.fastbound_item_id or "").strip():
			# The "nothing to desync" reasoning holds for a store that never used
			# FastBound. This gun HAS a bound-book item — the integration is merely
			# switched off — so the two records diverge from here, and the only thing
			# that stops that being invisible is saying so on the record itself.
			note = _("{0} — NOTE: FastBound is switched off; bound-book item {1} still "
				"carries the OLD serial and must be corrected there before the "
				"integration is re-enabled.").format(audit_line, sn.fastbound_item_id)
		frappe.get_doc("Serial No", new).add_comment("Comment", note)

		# 4) FastBound LAST: edit the bound-book item's serial in place with an audit
		#    note. Any failure here rolls back every ERP write above. Skipped
		#    entirely when the integration is switched off — steps 1-3 are then the
		#    complete correction, and there is no second book to keep in step.
		if client:
			client.update_item_serial(fb_item_id, new, note=fb_note)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise

	# -------------------------------------------------- post-commit: Woo re-push
	# The Woo listing's title / SKU embed the old serial, so re-push the corrected
	# serial. enqueue_after_commit so a rolled-back request never pushes; gated on
	# the listing id, mirroring ffl_woo_sync.woocommerce.doc_events.on_serial_no_update.
	# pos_fields_only=False: this is a DELIBERATE full push, not a machine drift
	# update — the whole point is refreshing the content fields (title / SKU /
	# _osa_serial) that embed the old serial, and the task's machine default
	# (lean) filters exactly those out. Lean here would leave the store selling
	# under a serial the POS no longer has (webhook._map_sku_to_pos would then
	# resolve web orders onto a ghost).
	if (sn.woo_product_id or ""):
		frappe.enqueue(
			"ffl_woo_sync.woocommerce.tasks.push_serial_task",
			queue="long", enqueue_after_commit=True, serial_no=new,
			pos_fields_only=False,
		)

	# -------------------------------------------------- post-commit: GunBroker
	# GunBroker cannot be corrected the way WooCommerce can. Its `SerialNumber`
	# is accepted by an update and then silently ignored, so a listing goes on
	# advertising the old number for as long as it stays up — and a firearm
	# listed under a serial that is not the one on the receiver is not a listing
	# to leave running. So the listing is ENDED and a person re-lists it, with
	# both numbers recorded on the serial and in the alert.
	#
	# Gated on the column existing, not on the app: during a deploy this code is
	# live before `bench migrate` has added the field, and a correction must not
	# 500 in that window. Same posture firearm._pos_picker_excluded_serials takes.
	if frappe.db.has_column("Serial No", "gb_item_id"):
		frappe.enqueue(
			"ffl_integrations.gunbroker.doc_events.on_serial_renamed",
			queue="long", enqueue_after_commit=True,
			old_name=old, new_name=new,
		)

	return {"ok": True, "old_serial": old, "new_serial": new, "fastbound_item_id": fb_item_id}


def _append_acq_note(acq_name: str, line: str) -> None:
	"""Append an audit line to FFL Acquisition.notes (Small Text), preserving any
	existing text. update_modified=False so the correction doesn't look like a
	fresh edit of the submitted A&D."""
	existing = (frappe.db.get_value("FFL Acquisition", acq_name, "notes") or "").rstrip()
	combined = f"{existing}\n{line}" if existing else line
	frappe.db.set_value("FFL Acquisition", acq_name, "notes", combined, update_modified=False)
