"""A gun held for a CRM deal (CRM Deal Hold): 48 hours where only the deal's customer can buy it.

One record per hold. A live hold (Active, ``expires_at`` still ahead) is a counter hold of kind
``crm_deal`` (:func:`counter_hold_rows`, read by ``reservations.counter_holds``), so every place that
already asks "can this gun be sold?" (the POS pickers, Create Order, the website push, GunBroker,
an outbound consignment) refuses it without knowing about deals. The deal's own customer is let
through where a sale names its buyer: the POS / Sales Invoice gate (:func:`refuse_deal_held_serials`),
Create Order (``manual_order._serial_status``) and the POS pickers.

Every write that decides who holds a gun (hold, extend, release, override, the sweep) commits what
the request has read so far, then locks in one order: the Item (``reservations.lock_item_reservations``,
the mutex the web order and GunBroker take), the Serial No row, the gun's Active hold rows. The
first statements after the commit are locking reads, so the decision is made on current rows, never
on a snapshot from earlier in the request (MariaDB snapshot isolation would refuse a lock on a row
changed since, error 1020). ``active_serial`` is unique: a second Active hold on one gun cannot be
written even if two requests got past the checks at once.

Expiry is read at the time (a live hold has ``expires_at > now``), and :func:`sweep` (every five
minutes) writes the record Expired and tells the website, so the page comes back within minutes.
The sweep also ends a hold whose gun left by a path no hold could refuse (a web order placed before
the website heard of the hold, a stock entry in Desk) and gives the deal owner a task.

The owner's rules live in the constants and :func:`is_own_hold` below, one place each.
"""
from __future__ import annotations

from datetime import timedelta

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, escape_html, get_datetime, get_fullname, now_datetime

from ffl_core.crm import TITLE, installed, pos_link_on
from ffl_core.utils import commit, single_flag

HOLD = "CRM Deal Hold"
DEAL = "CRM Deal"
KIND = "crm_deal"
ACTIVE, RELEASED, EXPIRED = "Active", "Released", "Expired"
SETTINGS = "Lead Mail Settings"
SWITCH = "crm_deal_holds"
# Hooks (docs/extensions/hooks.md): told after a hold starts, moves or ends; asked before a gun is held.
HOOK_CHANGED = "gunshop_serial_hold_changed"
HOOK_REFUSAL = "gunshop_deal_hold_refusal"

# Why a hold ended (CRM Deal Hold.end_reason).
END_RELEASED = "Released"
END_EXPIRED = "Expired"
END_WON = "Deal won"
END_LOST = "Deal lost"
END_DELETED = "Deal deleted"
END_OFF_DEAL = "Off the deal"
END_SOLD = "Sold to the customer"
END_OVERRIDE = "Manager override"
END_LEFT_STOCK = "Gun left stock"
END_TAKEN = "Taken by another order"

# ---------------------------------------------------------------- the owner's rules, one place each
HOLD_HOURS = 48
# The person who placed a hold may extend it this many times; after that a manager does.
EXTENSIONS_WITHOUT_MANAGER = 1
# Q1: a uniform-price gun (one product for all its guns) cannot be held.
# lazy: per-serial guns only; upgrade path: hold a unit of the shared pool (reservations.uniform_free_serials
# and the GunBroker model quantity subtract it).
HOLD_UNIFORM_PRICE_GUNS = False
# Q2: holding a gun again for the deal its hold expired on takes a manager, like a second extension.
REHOLD_AFTER_EXPIRY_NEEDS_MANAGER = True
# Q3: an expired hold only leaves a comment on the deal; True would also give the owner a task.
TASK_ON_EXPIRY = False
# Q4: a web or GunBroker order that took the gun before the hold reached that channel keeps it (the
# order paths are unchanged); the sweep ends the hold and, with this on, tasks the deal owner.
TASK_WHEN_TAKEN = True


def is_own_hold(hold, user: str | None = None) -> bool:
	"""Q5: whose hold it is, for extending without a manager: the person who placed it."""
	return (user or frappe.session.user) == hold.get("held_by")


class DealHoldError(frappe.ValidationError):
	"""A sale names a gun held for another customer's deal."""


class HeldText(str):
	"""A refusal that is a deal hold: still a plain string to every caller, and the scan endpoint
	raises it as :class:`DealHoldError` so the counter can offer the manager override."""


def throw_reason(reason: str) -> None:
	"""``frappe.throw`` a sale refusal; a deal hold's as :class:`DealHoldError`."""
	if isinstance(reason, HeldText):
		frappe.throw(escape_html(reason), DealHoldError, title=_("Gun held for a deal"))
	frappe.throw(reason)


# ---------------------------------------------------------------- reading


def holds_on() -> bool:
	"""Lead Mail Settings › Hold guns for deals, under the deal-to-POS switch. Off stops new holds and
	extensions only; holds already placed run to their end."""
	return pos_link_on() and single_flag(SETTINGS, SWITCH, default=0)


def table_ready() -> bool:
	"""The code runs for a moment before the migrate that makes the table (deploy window)."""
	return frappe.db.table_exists(HOLD)


def expires_label(value) -> str:
	"""``Fri Nov 14, 3:15 PM`` in the site's time zone (how the database keeps it)."""
	d = get_datetime(value)
	return f"{d:%a %b} {d.day}, {d.hour % 12 or 12}:{d:%M} {d:%p}"


def _live_rows(filters_sql: str, values: dict) -> list:
	if not table_ready():
		return []
	return frappe.db.sql(f"""
		SELECT name, serial_no, item_code, deal, held_by, expires_at, extensions
		FROM `tabCRM Deal Hold`
		WHERE status = 'Active' AND expires_at > %(now)s AND {filters_sql}
		ORDER BY expires_at ASC""", {"now": now_datetime(), **values}, as_dict=True)


def _deal_customers(deals) -> dict:
	deals = [d for d in set(deals or ()) if d]
	if not deals or not installed():
		return {}
	return {r.name: r.erpnext_customer for r in frappe.get_all(DEAL, filters={"name": ["in", deals]},
		fields=["name", "erpnext_customer"])}


def _entry(row, customer: str | None) -> dict:
	label = expires_label(row.expires_at)
	return {"source": KIND, "ref": row.deal, "kind": KIND,
		"tooltip": _("Held for deal {0} until {1}").format(row.deal, label),
		"url": f"/crm/deals/{row.deal}", "hold": row.name, "deal": row.deal, "customer": customer,
		"expires_at": str(row.expires_at), "expires_label": label, "held_by": row.held_by}


def counter_hold_rows(item_code: str) -> dict:
	"""This Item's live holds, as ``reservations.counter_holds`` entries: ``{serial: {source, ref (the
	deal), kind "crm_deal", tooltip, url, hold, deal, customer (the deal's), expires_at, expires_label,
	held_by}}``."""
	if not item_code:
		return {}
	rows = _live_rows("item_code = %(item)s", {"item": item_code})
	customers = _deal_customers(r.deal for r in rows)
	return {r.serial_no: _entry(r, customers.get(r.deal)) for r in rows}


def live_holds(serials) -> dict:
	"""``{serial: entry}`` for the live holds on these guns (entries as :func:`counter_hold_rows`)."""
	serials = [s for s in dict.fromkeys(serials or ()) if s]
	if not serials:
		return {}
	rows = _live_rows("serial_no IN %(serials)s", {"serials": serials})
	customers = _deal_customers(r.deal for r in rows)
	return {r.serial_no: _entry(r, customers.get(r.deal)) for r in rows}


def held_message(serial: str, entry: dict, *, manager_hint: bool = True) -> str:
	"""The counter's refusal for a gun held for someone else."""
	customer = entry.get("customer")
	name = (customer and frappe.db.get_value("Customer", customer, "customer_name")) or customer or _("a customer")
	text = _("{0} is held for {1} (deal {2}) until {3}.").format(serial, name, entry.get("deal"),
		entry.get("expires_label") or expires_label(entry.get("expires_at")))
	return f"{text} {_('A manager can release it.')}" if manager_hint else text


def counter_info(serial: str, entry: dict) -> dict:
	"""What the counter's "Gun held for a deal" dialog shows. No ID, 4473 or NICS data."""
	customer = entry.get("customer")
	return {"serial": serial, "hold": entry.get("hold"), "deal": entry.get("deal"), "customer": customer,
		"customer_name": (customer and frappe.db.get_value("Customer", customer, "customer_name")) or customer,
		"expires_at": entry.get("expires_at"), "expires_label": entry.get("expires_label"),
		"held_by": entry.get("held_by"), "held_by_name": get_fullname(entry.get("held_by")) if entry.get("held_by") else "",
		"message": held_message(serial, entry, manager_hint=False)}


# ---------------------------------------------------------------- the sale gate


def refuse_deal_held_serials(doc, method=None) -> None:
	"""POS Invoice / Sales Invoice validate: no gun held for a deal goes to anyone but the deal's
	customer. A manager releases the hold first (:func:`override`), then the save passes. Returns,
	consolidated invoices and web / GunBroker mirror invoices pass (their sale was already made)."""
	from ffl_core.compliance.channel_invoice import is_channel_mirrored_invoice
	from ffl_core.serials import serials_for_lines

	if doc.get("is_return") or doc.get("is_consolidated") or is_channel_mirrored_invoice(doc):
		return
	if frappe.flags.in_import or frappe.flags.in_patch or frappe.flags.in_migrate:
		return
	if not table_ready() or not frappe.db.exists(HOLD, {"status": ACTIVE}):
		return
	serials = [s for line in serials_for_lines(doc.get("items") or []) for s in line]
	# lazy: a hold committed between this read and the sale's commit is not seen (the gate takes no
	# lock); the sweep then ends that hold when it finds the gun gone. Upgrade path: lock the held
	# guns' Items (reservations.lock_item_reservations) here.
	for serial, entry in live_holds(serials).items():
		if entry.get("customer") and entry["customer"] == doc.get("customer"):
			continue
		frappe.throw(escape_html(held_message(serial, entry)), DealHoldError, title=_("Gun held for a deal"))


def release_sold(doc, method=None) -> None:
	"""POS Invoice / Sales Invoice on_submit: a held gun sold to the deal's own customer ends its hold,
	right after the sale commits (:func:`sweep_after_commit`). Nothing is written in the sale's own
	transaction, so nothing here can roll the sale back."""
	from ffl_core.serials import serials_for_lines

	if doc.get("is_return") or doc.get("is_consolidated") or not table_ready():
		return
	if not frappe.db.exists(HOLD, {"status": ACTIVE}):
		return
	serials = [s for line in serials_for_lines(doc.get("items") or []) for s in line]
	if serials:
		sweep_after_commit(frappe.get_all(HOLD, filters={"serial_no": ["in", serials], "status": ACTIVE}, pluck="name"))


SWEEP_JOB = "ffl_core.crm.holds.sweep"


def sweep_after_commit(names) -> None:
	"""Sweep these holds in a short job queued once the caller's transaction commits (an invoice's or a
	deal's, which this must never write into or roll back). The sweep ends whatever has ended: sold to
	the customer, deal won, lost, deleted, gun off its lines. A queue that is down costs only the
	wait for the five-minute sweep. Never raises."""
	names = sorted(set(names or ()))
	if not names:
		return
	try:
		frappe.enqueue(SWEEP_JOB, queue="short", enqueue_after_commit=True, holds=names)
	except Exception:  # noqa: BLE001 — the five-minute sweep catches up
		frappe.log_error(title=f"[{TITLE}] Hold sweep not queued: {', '.join(names)}", message=frappe.get_traceback())


# ---------------------------------------------------------------- locking and writing


def _rollback(savepoint: str) -> None:
	try:
		frappe.db.rollback(save_point=savepoint)
	except Exception:  # noqa: BLE001 — a deadlock already rolled the transaction back, savepoint and all
		frappe.db.rollback()


def _lock(item_code: str, serial: str, columns: list[str]):
	"""Commit, then lock Item → Serial No → the gun's Active holds. Returns (the Serial No row with
	``columns``, the Active hold rows). The caller passes ``columns`` (``shelf_state_columns()``)
	worked out before, so nothing reads between the commit and the locks."""
	from ffl_core.reservations import lock_item_reservations

	commit()
	lock_item_reservations(item_code)
	fields = ", ".join(f"`{c}`" for c in ["name", *columns])
	row = frappe.db.sql(f"SELECT {fields} FROM `tabSerial No` WHERE name = %s FOR UPDATE", (serial,), as_dict=True)
	active = frappe.db.sql("""SELECT name, serial_no, item_code, deal, held_by, expires_at, extensions
		FROM `tabCRM Deal Hold` WHERE serial_no = %s AND status = 'Active' FOR UPDATE""", (serial,), as_dict=True)
	return (row[0] if row else None), active


_PENDING = "ffl_deal_hold_comments"


def _comment(deal: str, text: str) -> None:
	"""The deal's timeline note. Inside :func:`_after_commit` it waits for the hold's own commit."""
	pending = getattr(frappe.local, _PENDING, None)
	if pending is not None:
		pending.append((deal, text))
		return
	_insert_comment(deal, text)


def _after_commit(fn):
	"""Run *fn* (a write under the gun's locks, which commits), then post its deal notes in a
	transaction of their own. A note updates the deal's row (``_comments``): written under the
	locks, a deal saved meanwhile by someone else (mail, CRM's page) would fail the whole hold with
	MariaDB's 1020. A note that still fails is logged; the hold stands."""
	setattr(frappe.local, _PENDING, [])
	try:
		result = fn()
		commit()
	finally:
		pending = getattr(frappe.local, _PENDING, None) or []
		setattr(frappe.local, _PENDING, None)
	for deal, text in pending:
		frappe.db.savepoint("ffl_hold_note")
		try:
			_insert_comment(deal, text)
			commit()
		except Exception:
			_rollback("ffl_hold_note")
			frappe.log_error(title=f"[{TITLE}] Hold note not posted: {deal}", message=frappe.get_traceback())
	return result


def _insert_comment(deal: str, text: str) -> None:
	if deal and installed() and frappe.db.exists(DEAL, deal):
		frappe.get_doc({"doctype": "Comment", "comment_type": "Comment", "reference_doctype": DEAL,
			"reference_name": deal, "content": escape_html(text), "comment_email": frappe.session.user,
			"comment_by": get_fullname(frappe.session.user)}).insert(ignore_permissions=True)


def fire_changed(serial: str) -> None:
	"""Tell the channels (``gunshop_serial_hold_changed``) that this gun's hold moved. Never raises."""
	for path in frappe.get_hooks(HOOK_CHANGED):
		try:
			frappe.get_attr(path)(serial)
		except Exception:  # noqa: BLE001 — a missed push is caught by the channel's reconcile
			frappe.log_error(title=f"[{TITLE}] {HOOK_CHANGED} failed: {path} ({serial})", message=frappe.get_traceback())


def _end(hold, status: str, reason: str, *, comment: str | None = None, note: str | None = None,
		approved_by: str | None = None, task: tuple[str, str] | None = None, by: str | None = None) -> None:
	"""End *hold* (a CRM Deal Hold document or a row with ``name``): status, reason, who, the deal's
	comment, a task for the deal owner when *task* = (title, description), and the channels told."""
	doc = hold if isinstance(hold, Document) else frappe.get_doc(HOLD, hold["name"])
	doc.update({"status": status, "end_reason": reason, "active_serial": f"ended:{doc.name}",
		"ended_at": now_datetime(), "ended_by": by or frappe.session.user})
	if note:
		doc.note = note[:500]
	if approved_by:
		doc.approved_by = approved_by
	doc.flags.ignore_permissions = True
	doc.save()
	_comment(doc.deal, comment or _("Hold on {0} ended: {1}.").format(doc.serial_no, _(reason).lower()))
	if task:
		_task(doc, *task)
	fire_changed(doc.serial_no)


def _task(hold, title: str, description: str) -> str | None:
	from ffl_core.crm.deals import new_task

	if not installed() or not frappe.db.exists(DEAL, hold.deal):
		return None
	owner = frappe.db.get_value(DEAL, hold.deal, "deal_owner") or hold.held_by
	return new_task(title, description, owner, hold.deal, priority="High")


def _approval(approver: str | None, pin: str | None, password: str | None) -> str | None:
	"""The manager approving this: the caller when they are one, else *approver* once their PIN (or
	password) checks out (price approval's list and lockouts). None when nobody was named."""
	from ffl_core.price_approval import _check_approver, is_approver

	if is_approver():
		return frappe.session.user
	if not approver:
		return None
	_check_approver(approver, pin, password)
	return approver


def approvers() -> list[dict]:
	from ffl_core.price_approval import approvers as price_approvers

	return price_approvers()


def _shelf_refusal(serial: str, row, item_code: str) -> str | None:
	"""Why this gun cannot be held now, or None: off the shelf, another channel's order, another counter
	hold (a parked POS sale, a counter order, a consignment), or a channel that refuses (GunBroker)."""
	from ffl_core.api.inventory_count import _shelf_label
	from ffl_core.reservations import channel_hold_reason, counter_holds, serial_shelf_state

	from ffl_core.serials import ledger_serials, same_serial

	if not row:
		return _("{0} is no longer on file.").format(serial)
	if not same_serial(serial, ledger_serials([serial]).get(serial)):
		# The same rule as serials.refuse_tagged_serial_sale: not for sale yet, so not to hold either.
		return _("{0} can't be sold yet: it shares its serial with another gun or has no serial number.").format(serial)
	state = serial_shelf_state(serial, row=row, holds={})
	if not state["sellable"]:
		if state["reason"] == "channel_reserved":
			return channel_hold_reason(serial, state["detail"])
		return f"{serial}: {_shelf_label(state, show_refs=True)}."
	other = counter_holds(item_code).get(serial)
	if other and other.get("kind") != KIND:
		return f"{serial}: {other.get('tooltip') or _('On an open counter order')}."
	return channel_refusal(serial)


def channel_refusal(serial: str) -> str | None:
	"""Ask ``gunshop_deal_hold_refusal`` providers (GunBroker: a live listing). A provider that fails
	refuses: holding a gun a marketplace may sell is the mistake this check exists to stop."""
	for path in frappe.get_hooks(HOOK_REFUSAL):
		try:
			answer = frappe.get_attr(path)(serial)
		except Exception:  # noqa: BLE001
			frappe.log_error(title=f"[{TITLE}] {HOOK_REFUSAL} failed: {path} ({serial})", message=frappe.get_traceback())
			return _("Couldn't check the other sales channels for {0}. Try again.").format(serial)
		if answer:
			return answer
	return None


def _uniform_refusal(serial: str, item_code: str) -> str | None:
	from ffl_core.reservations import is_uniform_firearm_item

	if HOLD_UNIFORM_PRICE_GUNS or not is_uniform_firearm_item(item_code):
		return None
	name = frappe.db.get_value("Item", item_code, "item_name") or item_code
	return _("{0} is sold as one product for all its guns, so a single gun can't be held.").format(name)


def _held_before(deal: str, serial: str) -> bool:
	"""Q2: this deal held this gun before and the hold expired."""
	return REHOLD_AFTER_EXPIRY_NEEDS_MANAGER and bool(frappe.db.exists(HOLD,
		{"deal": deal, "serial_no": serial, "status": EXPIRED}))


def _public(name: str) -> dict:
	doc = frappe.get_doc(HOLD, name)
	return {"name": doc.name, "serial": doc.serial_no, "deal": doc.deal, "status": doc.status,
		"expires_at": str(doc.expires_at), "expires_label": expires_label(doc.expires_at),
		"held_by": doc.held_by, "extensions": cint(doc.extensions), "end_reason": doc.end_reason or None}


CONCURRENT = "Someone else just changed this gun. Try again."


def _serial_items(serials) -> dict:
	return {r.name: r.item_code for r in frappe.get_all("Serial No", filters={"name": ["in", list(serials)]},
		fields=["name", "item_code"])} if serials else {}


def hold_guns(deal, serials, approver: str | None = None, pin: str | None = None,
		password: str | None = None) -> dict:
	"""Hold each gun for *deal* (a CRM Deal document, read and permission-checked by the caller) for
	:data:`HOLD_HOURS`. Each gun on its own: ``{"held": [hold], "refused": [{"serial", "reason"}]}``."""
	from ffl_core.crm.customer import linked_customer
	from ffl_core.crm.deal import SERIAL_FIELD
	from ffl_core.reservations import shelf_state_columns

	if not holds_on():
		frappe.throw(_("Holding guns for deals is off. A manager can turn it on in Lead Mail Settings."),
			title=_("Turned off"))
	if not linked_customer(deal):
		frappe.throw(_("Pick the customer first."), title=_("No customer"))
	serials = [str(s).strip() for s in dict.fromkeys(serials or ()) if str(s or "").strip()]
	if not serials:
		frappe.throw(_("Pick a gun to hold."))
	on_deal = {r.get(SERIAL_FIELD) for r in deal.get("products") or [] if r.get(SERIAL_FIELD)}
	items = _serial_items(serials)
	held, refused, todo = [], [], []
	for serial in serials:
		reason = None
		if serial not in on_deal:
			reason = _("{0} is not on this deal.").format(serial)
		elif serial not in items:
			reason = _("{0} is no longer on file.").format(serial)
		else:
			reason = _uniform_refusal(serial, items[serial])
		if reason:
			refused.append({"serial": serial, "reason": reason})
		else:
			todo.append(serial)
	# A manager is asked for up front (never under the locks), and only when a gun needs one.
	needs = {s for s in todo if _held_before(deal.name, s) or _expired_here(deal.name, s)}
	manager = _approval(approver, pin, password) if needs else None
	columns = shelf_state_columns()
	for serial in sorted(todo, key=lambda s: (items[s], s)):
		out = _after_commit(lambda serial=serial: _hold_one(deal.name, serial, items[serial], columns, manager))
		(held if "name" in out else refused).append(out)
	return {"held": held, "refused": refused}


def _expired_here(deal: str, serial: str) -> bool:
	return bool(table_ready() and frappe.db.exists(HOLD, {"deal": deal, "serial_no": serial, "status": ACTIVE,
		"expires_at": ["<=", now_datetime()]}))


def _hold_one(deal: str, serial: str, item_code: str, columns: list[str], manager: str | None) -> dict:
	savepoint = "ffl_deal_hold"
	try:
		row, active = _lock(item_code, serial, columns)
		frappe.db.savepoint(savepoint)
		now = now_datetime()
		was_ours = False
		for h in active:
			if get_datetime(h.expires_at) > now:
				if h.deal == deal:
					return _public(h.name)   # already held for this deal
				return {"serial": serial, "reason": _("{0} is already held for deal {1} until {2}.").format(
					serial, h.deal, expires_label(h.expires_at))}
			# Ran out and the sweep has not been by yet: it ends now.
			was_ours = was_ours or h.deal == deal
			_end(h, EXPIRED, END_EXPIRED, comment=_("Hold on {0} expired.").format(serial), by="Administrator")
		if (was_ours or _held_before(deal, serial)) and not manager:
			return {"serial": serial, "reason": _("{0} was held for this deal before. A manager has to hold it again.").format(serial),
				"needs_manager": True}
		reason = _shelf_refusal(serial, row, item_code)
		if reason:
			return {"serial": serial, "reason": reason}
		user = frappe.session.user
		doc = frappe.get_doc({"doctype": HOLD, "serial_no": serial, "item_code": item_code, "deal": deal,
			"status": ACTIVE, "held_by": user, "held_at": now, "expires_at": now + timedelta(hours=HOLD_HOURS),
			"extensions": 0, "active_serial": serial, "approved_by": manager if (was_ours or _held_before(deal, serial)) else None})
		doc.insert(ignore_permissions=True)
		_comment(deal, _("Held {0} until {1}.").format(serial, expires_label(doc.expires_at)))
		fire_changed(serial)
		commit()
		return _public(doc.name)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		_rollback(savepoint)
		return {"serial": serial, "reason": _("{0} is already held for another deal.").format(serial)}
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		_rollback(savepoint)
		return {"serial": serial, "reason": _(CONCURRENT)}


def _locked_hold(name: str):
	"""Lock the hold's gun (Item → Serial No → holds) and return its locked Active row, or throw
	"This hold has ended."."""
	from ffl_core.reservations import shelf_state_columns

	base = frappe.db.get_value(HOLD, name, ["serial_no", "item_code"], as_dict=True)
	if not base:
		frappe.throw(_("This hold no longer exists."), frappe.DoesNotExistError)
	_row, active = _lock(base.item_code, base.serial_no, shelf_state_columns())
	hold = next((h for h in active if h.name == name), None)
	if not hold or get_datetime(hold.expires_at) <= now_datetime():
		frappe.throw(_("This hold has ended."), title=_("Hold ended"))
	return hold


def _concurrent(fn):
	"""Run *fn*; a 1020 / deadlock on the gun's rows becomes "try again"."""
	try:
		return fn()
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		frappe.db.rollback()
		frappe.throw(_(CONCURRENT), title=_("Try again"))


def _brief(name: str):
	return frappe.db.get_value(HOLD, name, ["name", "held_by", "extensions"], as_dict=True)


def _extend_needs_manager(hold) -> bool:
	return not (is_own_hold(hold) and cint(hold.extensions) < EXTENSIONS_WITHOUT_MANAGER)


def _refuse_extension(hold) -> None:
	if cint(hold.extensions) >= EXTENSIONS_WITHOUT_MANAGER:
		frappe.throw(_("This hold was extended once. A manager has to extend it again."), title=_("Manager needed"))
	frappe.throw(_("{0} placed this hold. A manager has to extend it.").format(get_fullname(hold.held_by)),
		title=_("Manager needed"))


def extend(name: str, approver: str | None = None, pin: str | None = None, password: str | None = None) -> dict:
	"""Another :data:`HOLD_HOURS` on hold *name* (permission on its deal checked by the caller)."""
	if not holds_on():
		frappe.throw(_("Holding guns for deals is off. A manager can turn it on in Lead Mail Settings."),
			title=_("Turned off"))
	hold = _brief(name)
	if not hold:
		frappe.throw(_("This hold no longer exists."), frappe.DoesNotExistError)
	# Read before the lock only to know whether to check a PIN first; the rule is applied again under
	# the lock (run), where two extensions at once cannot both read "not extended yet".
	manager = _approval(approver, pin, password) if _extend_needs_manager(hold) else None
	if _extend_needs_manager(hold) and not manager:
		_refuse_extension(hold)

	def run():
		locked = _locked_hold(name)
		if _extend_needs_manager(locked) and not manager:
			_refuse_extension(locked)
		doc = frappe.get_doc(HOLD, locked.name)
		doc.expires_at = get_datetime(doc.expires_at) + timedelta(hours=HOLD_HOURS)
		doc.extensions = cint(doc.extensions) + 1
		if manager:
			doc.approved_by = manager
		doc.flags.ignore_permissions = True
		doc.save()
		text = _("Hold on {0} extended to {1}.").format(doc.serial_no, expires_label(doc.expires_at))
		if manager and manager != frappe.session.user:
			text += " " + _("Approved by {0}.").format(get_fullname(manager))
		_comment(doc.deal, text)
		fire_changed(doc.serial_no)
		return _public(doc.name)

	return _after_commit(lambda: _concurrent(run))


def release(name: str, reason: str | None = None, approver: str | None = None, pin: str | None = None,
		password: str | None = None) -> dict:
	"""End hold *name* by hand. Its holder, the deal owner or a manager; anyone else with a manager's PIN."""
	hold = frappe.db.get_value(HOLD, name, ["held_by", "deal"], as_dict=True)
	if not hold:
		frappe.throw(_("This hold no longer exists."), frappe.DoesNotExistError)
	user = frappe.session.user
	owner = installed() and frappe.db.get_value(DEAL, hold.deal, "deal_owner")
	manager = None
	if user not in (hold.held_by, owner):
		manager = _approval(approver, pin, password)
		if not manager:
			frappe.throw(_("Only {0}, the deal owner or a manager can release this hold.").format(
				get_fullname(hold.held_by)), title=_("Manager needed"))

	def run():
		locked = _locked_hold(name)
		_end(locked, RELEASED, END_RELEASED, note=(reason or "").strip() or None,
			approved_by=manager if manager != user else None,
			comment=_("Hold on {0} released by {1}.").format(locked.serial_no, get_fullname(user)))
		return _public(name)

	return _after_commit(lambda: _concurrent(run))


def override(serial: str, reason: str, approver: str | None = None, pin: str | None = None,
		password: str | None = None) -> dict:
	"""A manager at the counter ends the hold on *serial* so it can be sold to someone else; the deal
	owner gets a task. ``{"released": hold or None (nothing was held), "task": name or None}``."""
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Give a reason."), title=_("Reason required"))
	manager = _approval(approver, pin, password)
	if not manager:
		frappe.throw(_("Pick the manager releasing the hold."), title=_("Manager needed"))
	from ffl_core.reservations import shelf_state_columns

	item_code = frappe.db.get_value("Serial No", serial, "item_code")
	if not item_code:
		frappe.throw(_("{0} is no longer on file.").format(serial), frappe.DoesNotExistError)
	columns = shelf_state_columns()

	def run():
		_row, active = _lock(item_code, serial, columns)
		live = next((h for h in active if get_datetime(h.expires_at) > now_datetime()), None)
		if not live:
			return {"released": None, "task": None}
		customer = _deal_customers([live.deal]).get(live.deal)
		name = (customer and frappe.db.get_value("Customer", customer, "customer_name")) or customer or _("the customer")
		doc = frappe.get_doc(HOLD, live.name)
		_end(doc, RELEASED, END_OVERRIDE, note=reason, approved_by=manager,
			comment=_("Hold on {0} released at the counter by {1}. Reason: {2}").format(serial, get_fullname(manager), reason))
		task = _task(doc, _("Hold released at the counter: {0}").format(serial),
			_("{0} released the hold for {1} so the gun could be sold to someone else. Reason: {2}.").format(
				get_fullname(manager), name, reason.rstrip(".")))
		commit()
		return {"released": _public(doc.name), "task": task}

	return _after_commit(lambda: _concurrent(run))


# ---------------------------------------------------------------- the deal's own events


def _deal_holds(deal: str) -> list[str]:
	if not table_ready():
		return []
	return frappe.get_all(HOLD, filters={"deal": deal, "status": ACTIVE}, pluck="name")


def on_deal_update(doc, method=None) -> None:
	"""CRM Deal on_update: a deal Won or Lost, or a gun taken off its lines, ends the holds concerned —
	in the sweep queued after the deal's save commits (:func:`sweep_after_commit`), never in it."""
	from ffl_core.crm.deal import SERIAL_FIELD

	names = _deal_holds(doc.name)
	if not names:
		return
	if frappe.db.get_value("CRM Deal Status", doc.status, "type") in ("Won", "Lost"):
		sweep_after_commit(names)
		return
	on = {r.get(SERIAL_FIELD) for r in doc.get("products") or [] if r.get(SERIAL_FIELD)}
	sweep_after_commit([n for n in names if frappe.db.get_value(HOLD, n, "serial_no") not in on])


def on_deal_trash(doc, method=None) -> None:
	"""CRM Deal on_trash: its holds end once the delete commits (the records stay)."""
	sweep_after_commit(_deal_holds(doc.name))


def on_deal_rename(doc, method=None, old: str | None = None, new: str | None = None, merge: bool = False) -> None:
	"""CRM Deal after_rename (Desk allows it): the holds follow the new name. A merge leaves the merged
	deal's holds on its old name, gone now, and the sweep ends them as for a deleted deal."""
	if not table_ready() or not old or not new:
		return
	if merge:
		sweep_after_commit(frappe.get_all(HOLD, filters={"deal": old, "status": ACTIVE}, pluck="name"))
		return
	frappe.db.sql("UPDATE `tabCRM Deal Hold` SET deal = %s WHERE deal = %s", (new, old))


# ---------------------------------------------------------------- the sweep


def sweep(holds: list[str] | None = None) -> dict:
	"""Every five minutes, and right after a sale or a deal event (*holds*: just those): write every
	hold that has ended in fact. Each in its own transaction; a failure is logged and tried again next
	time. Returns ``{reason: count}``."""
	if not installed() or not table_ready():
		return {}
	counts: dict[str, int] = {}
	filters = {"status": ACTIVE}
	if holds is not None:
		filters["name"] = ["in", list(holds) or [""]]
	for name in frappe.get_all(HOLD, filters=filters, pluck="name", order_by="expires_at asc"):
		frappe.db.savepoint("ffl_hold_sweep")
		try:
			ended = _after_commit(lambda name=name: _sweep_one(name))
		except Exception:
			_rollback("ffl_hold_sweep")
			frappe.log_error(title=f"[{TITLE}] Hold sweep failed: {name}", message=frappe.get_traceback())
			continue
		if ended:
			counts[ended] = counts.get(ended, 0) + 1
	return counts


def _doc_customer(entry: dict) -> str | None:
	"""The buyer on the document behind another counter hold, when it is one of ours."""
	doctype = {"sales_invoice": "Sales Invoice", "pos": "POS Invoice"}.get(entry.get("kind"))
	return doctype and entry.get("ref") and frappe.db.get_value(doctype, entry["ref"], "customer")


def _sold_to(serial: str, customer: str, since) -> str | None:
	"""The submitted sale (Sales Invoice or POS Invoice, no return) of *serial* to *customer* since the
	hold began, or None. lazy: reads each of the customer's sales since then; a handful in 48 hours.
	Upgrade path: a serial-to-voucher lookup through the Serial and Batch ledger."""
	from ffl_core.serials import serials_for_lines

	for doctype in ("Sales Invoice", "POS Invoice"):
		filters = {"customer": customer, "docstatus": 1, "is_return": 0, "creation": [">=", since]}
		if doctype == "Sales Invoice":
			filters["is_consolidated"] = 0
		for name in frappe.get_all(doctype, filters=filters, pluck="name", order_by="creation asc"):
			rows = frappe.get_all(f"{doctype} Item", filters={"parent": name, "parenttype": doctype},
				fields=["serial_no", "serial_and_batch_bundle"])
			if any(serial in line for line in serials_for_lines(rows)):
				return name
	return None


def _sweep_one(name: str) -> str | None:
	from ffl_core.crm.deal import SERIAL_FIELD
	from ffl_core.reservations import counter_holds, serial_shelf_state, shelf_state_columns

	base = frappe.db.get_value(HOLD, name, ["serial_no", "item_code"], as_dict=True)
	if not base:
		return None
	columns = shelf_state_columns()
	row, active = _lock(base.item_code, base.serial_no, columns)
	hold = next((h for h in active if h.name == name), None)
	if not hold:
		return None
	doc = frappe.get_doc(HOLD, name)
	serial = doc.serial_no
	sys = "Administrator"
	deal = frappe.db.get_value(DEAL, doc.deal, ["status", "erpnext_customer"], as_dict=True)
	if not deal:
		_end(doc, RELEASED, END_DELETED, by=sys)
		return END_DELETED
	# First: a sale to the deal's own customer (it also makes the deal Won and takes the gun off the shelf).
	invoice = deal.erpnext_customer and _sold_to(serial, deal.erpnext_customer, doc.held_at or doc.creation)
	if invoice:
		_end(doc, RELEASED, END_SOLD, by=sys, comment=_("Sold to the customer on {0}.").format(invoice))
		return END_SOLD
	closed = frappe.db.get_value("CRM Deal Status", deal.status, "type")
	if closed in ("Won", "Lost"):
		reason = END_WON if closed == "Won" else END_LOST
		_end(doc, RELEASED, reason, by=sys)
		return reason
	if not frappe.db.exists("CRM Products", {"parenttype": DEAL, "parent": doc.deal, SERIAL_FIELD: serial}):
		_end(doc, RELEASED, END_OFF_DEAL, by=sys)
		return END_OFF_DEAL
	state = serial_shelf_state(serial, row=row, holds={}) if row else {"sellable": False, "reason": "not_found", "detail": {}}
	if not state["sellable"]:
		if state["reason"] == "channel_reserved":
			detail = state["detail"] or {}
			channel = {"woo": _("Web"), "gunbroker": _("GunBroker")}.get(detail.get("channel"), detail.get("channel") or "")
			_end(doc, RELEASED, END_TAKEN, by=sys, task=(_("Hold ended: {0} was sold elsewhere").format(serial),
				_("{0} order {1} bought {2} before the hold reached that channel.").format(channel, detail.get("ref") or "", serial))
				if TASK_WHEN_TAKEN else None)
			return END_TAKEN
		_end(doc, RELEASED, END_LEFT_STOCK, by=sys, task=(_("Hold ended: {0} left stock").format(serial),
			_("{0} is no longer on the shelf to sell, so its hold for this deal has ended.").format(serial)))
		return END_LEFT_STOCK
	other = counter_holds(doc.item_code).get(serial)
	if other and other.get("kind") != KIND:
		if deal.erpnext_customer and _doc_customer(other) == deal.erpnext_customer:
			if other.get("kind") == "sales_invoice":
				_end(doc, RELEASED, END_SOLD, by=sys,
					comment=_("Sold to the customer on {0}.").format(other.get("ref")))
				return END_SOLD
			# The customer's own parked sale: the hold stays until it ends or the sale goes through.
		else:
			_end(doc, RELEASED, END_TAKEN, by=sys, task=(_("Hold ended: {0} is on another order").format(serial),
				_("{0} went on {1} while it was held for this deal.").format(serial, other.get("ref") or other.get("source"))))
			return END_TAKEN
	if get_datetime(doc.expires_at) <= now_datetime():
		_end(doc, EXPIRED, END_EXPIRED, by=sys, comment=_("Hold on {0} expired.").format(serial),
			task=(_("Hold expired: {0}").format(serial), _("The hold on {0} for this deal ran out.").format(serial))
			if TASK_ON_EXPIRY else None)
		return END_EXPIRED
	return None


# ---------------------------------------------------------------- what the Deal page shows


def deal_guns(deal) -> dict:
	"""The Deal page's Hold guns dialog: each gun on *deal*'s lines, its hold and what can be done."""
	from ffl_core.crm.deal import SERIAL_FIELD
	from ffl_core.price_approval import is_approver
	from ffl_core.reservations import shelf_state_columns

	user = frappe.session.user
	manager = is_approver()
	owner = deal.get("deal_owner")
	serials = [r.get(SERIAL_FIELD) for r in deal.get("products") or [] if r.get(SERIAL_FIELD)]
	serials = list(dict.fromkeys(serials))
	items = _serial_items(serials)
	live = live_holds(serials)
	columns = shelf_state_columns()
	guns = []
	for serial in serials:
		item_code = items.get(serial)
		gun = {"serial": serial, "item_code": item_code,
			"item_name": (item_code and frappe.db.get_value("Item", item_code, "item_name")) or item_code or serial,
			"hold": None, "blocked": None, "needs_manager": False}
		entry = live.get(serial)
		if entry and entry["deal"] == deal.name:
			row = frappe.db.get_value(HOLD, entry["hold"], ["held_by", "extensions"], as_dict=True)
			own_extend = is_own_hold(row, user) and cint(row.extensions) < EXTENSIONS_WITHOUT_MANAGER
			gun["hold"] = {"name": entry["hold"], "expires_at": entry["expires_at"], "expires_label": entry["expires_label"],
				"held_by": row.held_by, "held_by_name": get_fullname(row.held_by), "extensions": cint(row.extensions),
				"extend_needs_manager": not (own_extend or manager),
				"release_needs_manager": not (manager or user in (row.held_by, owner))}
		elif entry:
			gun["blocked"] = _("Held for another deal until {0}").format(entry["expires_label"])
		elif not item_code:
			gun["blocked"] = _("No longer on file")
		else:
			row = frappe.db.get_value("Serial No", serial, ["name", *columns], as_dict=True)
			gun["blocked"] = _uniform_refusal(serial, item_code) or _shelf_refusal(serial, row, item_code)
			gun["needs_manager"] = not manager and (_held_before(deal.name, serial) or _expired_here(deal.name, serial))
		guns.append(gun)
	return {"guns": guns, "enabled": holds_on(), "self_approves": manager,
		"approvers": [] if manager else approvers()}


def card_lines(deal: str) -> list[dict]:
	"""The customer card's ``Held: {serial} until {time}`` lines."""
	if not table_ready():
		return []
	rows = _live_rows("deal = %(deal)s", {"deal": deal})
	return [{"serial": r.serial_no, "expires_label": expires_label(r.expires_at)} for r in rows]


def serial_entry(serial: str) -> dict | None:
	"""The live hold on one gun, as :func:`counter_info`, or None."""
	entry = live_holds([serial]).get(serial)
	return counter_info(serial, entry) if entry else None


def blocked_on_sale(doc) -> list[dict]:
	"""The guns on a sale document held for someone other than its customer (:func:`counter_info` each)."""
	from ffl_core.serials import serials_for_lines

	if doc.get("is_return") or not table_ready():
		return []
	serials = [s for line in serials_for_lines(doc.get("items") or []) for s in line]
	return [counter_info(serial, entry) for serial, entry in live_holds(serials).items()
		if not (entry.get("customer") and entry["customer"] == doc.get("customer"))]
