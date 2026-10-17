"""Switching staff at a shared counter screen, and locking it when nobody is there.

The desk's bottom-left corner gets a switch-user button (``public/js/staff_switch.js``):
it lists everyone with a counter role (:func:`counter_users`); picking someone else
and typing *their* password signs them in through frappe's own ``login`` (a new
session, the old one ends), so every document from then on carries their name and
frappe's login history records the change.

After FFL Settings → Lock Idle Screen After (minutes) with no touch or key, the
screen locks (:func:`lock_screen`): the same staff list, with no way back except a
password — the person who was signed in unlocks with theirs (:func:`unlock_screen`),
anyone else signs in as themselves. The lock is kept on the server session, so
reloading the page comes back locked (``frappe.boot.ffl_screen_locked``).

# lazy: the lock is enforced by the page (an overlay that survives a reload), not by
# refusing API calls while locked; someone with the browser's developer tools could
# still use the session. Upgrade path: a before_request guard that refuses every
# call but unlock / login while the session is locked.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint
from frappe.utils.password import check_password

from ffl_core.roles import COUNTER_ROLES
from ffl_core.utils import single_raw

class WrongPassword(frappe.ValidationError):
	"""The unlock password did not match (the session stays signed in, locked)."""


LOCK_KEY = "ffl_screen_locked"
UNLOCK_TRIES, UNLOCK_WINDOW = 10, 600
DEFAULT_LOCK_MINUTES = 5


def lock_minutes() -> int:
	"""FFL Settings → Lock Idle Screen After (minutes); 5 on a site that never saved it, 0 = never."""
	raw = single_raw("FFL Settings", "counter_lock_minutes")
	return DEFAULT_LOCK_MINUTES if raw in (None, "") else max(0, cint(raw))


def ensure_lock_default() -> None:
	"""Store the 5-minute default when the site has no row for it yet (fresh install: patches
	don't run there), so a first Save of FFL Settings can't silently write 0. A stated value is kept."""
	if single_raw("FFL Settings", "counter_lock_minutes") is None:
		frappe.db.set_single_value("FFL Settings", "counter_lock_minutes", DEFAULT_LOCK_MINUTES)


def _session_locked() -> bool:
	return bool(cint((frappe.session.data or {}).get(LOCK_KEY)))


def _set_locked(value: int) -> None:
	frappe.session.data[LOCK_KEY] = value
	frappe.local.session_obj.update(force=True)


def extend_bootinfo(bootinfo) -> None:
	"""The page's settings: lock after N idle minutes, and whether this session is locked now."""
	if frappe.session.user == "Guest":
		return
	bootinfo.ffl_counter_lock_minutes = lock_minutes()
	bootinfo.ffl_screen_locked = 1 if _session_locked() else 0


@frappe.whitelist()
def counter_users() -> list[dict]:
	"""Everyone who can sign in at the counter, for the switch-user list.

	Returns:
	    [{"user", "full_name", "image", "current": bool}] for enabled desk users
	    holding a counter role, by name (never Administrator, the site's superuser),
	    and always the signed-in one, marked current: a locked screen is unlocked
	    by picking yourself, whatever your roles.
	"""
	frappe.only_for(COUNTER_ROLES)
	holders = set(frappe.get_all("Has Role", filters={"role": ("in", COUNTER_ROLES), "parenttype": "User"},
		pluck="parent"))
	holders -= {"Guest", "Administrator"}
	if frappe.session.user != "Guest":
		holders.add(frappe.session.user)
	if not holders:
		return []
	rows = frappe.get_all("User", filters={"name": ("in", list(holders)), "enabled": 1, "user_type": "System User"},
		fields=["name", "full_name", "user_image"], order_by="full_name asc")
	return [{"user": r.name, "full_name": r.full_name or r.name, "image": r.user_image,
		"current": r.name == frappe.session.user} for r in rows]


@frappe.whitelist(methods=["POST"])
def lock_screen() -> dict:
	"""Lock this session's screen (idle, or the clerk walked away).

	Returns:
	    {"locked": 1}.
	"""
	frappe.only_for(COUNTER_ROLES)
	_set_locked(1)
	return {"locked": 1}


@frappe.whitelist(methods=["POST"])
def unlock_screen(password: str) -> dict:
	"""The signed-in person unlocks their own screen with their password: 10 wrong
	tries per person per 10 minutes (counted per person, not per IP — a shop's
	counters share one address). Signing in as someone else is frappe's login.

	Args:
	    password: The signed-in user's password.

	Returns:
	    {"locked": 0}.
	"""
	frappe.only_for(COUNTER_ROLES)
	key = frappe.cache.make_key(f"ffl-unlock-fail:{frappe.session.user}")
	if cint(frappe.cache.get(key)) >= UNLOCK_TRIES:
		frappe.throw(_("Too many wrong passwords. Wait a few minutes, or pick someone else to sign in."),
			WrongPassword, title=_("Locked"))
	try:
		check_password(frappe.session.user, password or "")
	except frappe.AuthenticationError:
		frappe.cache.set(key, 0, ex=UNLOCK_WINDOW, nx=True)
		frappe.cache.incrby(key, 1)
		# Not AuthenticationError itself: frappe answers that by clearing the session
		# cookies (app.handle_exception), which would sign the person out for a typo.
		frappe.throw(_("That password is not right."), WrongPassword, title=_("Locked"))
	frappe.cache.delete(key)
	_set_locked(0)
	return {"locked": 0}
