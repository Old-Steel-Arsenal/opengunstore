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

A user may also set a counter PIN (4–6 digits, :func:`set_pin`, with their login
password): then picking them takes the PIN, or the password. The PIN is a passlib hash
in frappe's ``__Auth`` table beside the login password (``frappe.utils.password``,
fieldname :data:`PIN_FIELD`), never a doctype field, so no document read or endpoint can
return it. Five wrong PINs turn it off for that person until they next sign in or
unlock with their password; a Sales or System Manager can clear anyone's
(:func:`clear_pin`).

# lazy: the lock is enforced by the page (an overlay that survives a reload), not by
# refusing API calls while locked; someone with the browser's developer tools could
# still use the session. Upgrade path: a before_request guard that refuses every
# call but unlock / login while the session is locked.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint
from frappe.utils.password import check_password, remove_encrypted_password, update_password

from ffl_core.roles import COUNTER_ROLES
from ffl_core.utils import single_raw

class WrongPassword(frappe.ValidationError):
	"""The unlock password did not match (the session stays signed in, locked)."""


LOCK_KEY = "ffl_screen_locked"
UNLOCK_TRIES, UNLOCK_WINDOW = 10, 600
DEFAULT_LOCK_MINUTES = 5
PIN_FIELD = "ffl_counter_pin"
PIN_TRIES = 5
# Survives `bench clear-cache` (hooks.persistent_cache_keys), so a cache clear never hands back PIN tries.
# lazy: a Redis restart still resets the count; upgrade path: keep it on a hidden User field.
PIN_FAIL_KEY = "ffl-pin-fail:"
PIN_MANAGER_ROLES = ("Sales Manager", "System Manager")


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
	    [{"user", "full_name", "image", "current": bool, "has_pin": bool, "pin_off": bool (five
	    wrong PINs: their password until they next use it)}] for enabled desk users
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
	pins = _users_with_pin([r.name for r in rows])
	return [{"user": r.name, "full_name": r.full_name or r.name, "image": r.user_image,
		"current": r.name == frappe.session.user, "has_pin": r.name in pins,
		"pin_off": r.name in pins and _pin_off(r.name)} for r in rows]


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
def unlock_screen(password: str | None = None, pin: str | None = None) -> dict:
	"""The signed-in person unlocks their own screen with their password (10 wrong
	tries per person per 10 minutes, counted per person, not per IP — a shop's
	counters share one address) or their counter PIN (5 wrong ones turn it off).
	Signing in as someone else is frappe's login, or :func:`switch_with_pin`.

	Args:
	    password: The signed-in user's password.
	    pin: Or their counter PIN.

	Returns:
	    {"locked": 0}.
	"""
	frappe.only_for(COUNTER_ROLES)
	if pin and not password:
		_check_pin(frappe.session.user, pin)
	else:
		_check_own_password(password)
	_set_locked(0)
	return {"locked": 0}


def _check_own_password(password: str | None) -> None:
	"""The signed-in user's login password, under the unlock limit; a right one turns their PIN back on."""
	_check_password(frappe.session.user, password)


def _check_password(user: str, password: str | None) -> None:
	"""*user*'s login password, 10 wrong tries per person per 10 minutes; a right one turns their PIN
	back on. Also a manager approving a price at someone else's counter (price_approval.approve)."""
	key = frappe.cache.make_key(f"ffl-unlock-fail:{user}")
	if cint(frappe.cache.get(key)) >= UNLOCK_TRIES:
		frappe.throw(_("Too many wrong passwords. Wait a few minutes, or pick someone else to sign in."),
			WrongPassword, title=_("Locked"))
	try:
		check_password(user, password or "")
	except frappe.AuthenticationError:
		frappe.cache.set(key, 0, ex=UNLOCK_WINDOW, nx=True)
		frappe.cache.incrby(key, 1)
		# Not AuthenticationError itself: frappe answers that by clearing the session
		# cookies (app.handle_exception), which would sign the person out for a typo.
		frappe.throw(_("That password is not right."), WrongPassword, title=_("Locked"))
	frappe.cache.delete(key)
	frappe.cache.delete(_pin_fail_key(user))


# ------------------------------------------------------------------ counter PIN


def _pin_fail_key(user: str) -> str:
	return frappe.cache.make_key(PIN_FAIL_KEY + user)


def _pin_off(user: str) -> bool:
	return cint(frappe.cache.get(_pin_fail_key(user))) >= PIN_TRIES


def _users_with_pin(users: list[str]) -> set[str]:
	if not users:
		return set()
	auth = frappe.qb.Table("__Auth")
	return set((frappe.qb.from_(auth).select(auth.name)
		.where((auth.doctype == "User") & (auth.fieldname == PIN_FIELD) & (auth.name.isin(users)))).run(pluck=True))


def pin_problem(pin: str | None, confirm: str | None = None) -> str | None:
	"""Why *pin* cannot be a counter PIN, or None: 4–6 digits, not one digit repeated, not
	a run up or down (1234, 987654), and typed the same twice. Mirrored by staff_switch.js
	pinProblem (the dialog's check before it asks the server)."""
	pin = (pin or "").strip()
	if not (pin.isdigit() and pin.isascii() and 4 <= len(pin) <= 6):
		return _("A PIN is 4 to 6 digits.")
	steps = {int(b) - int(a) for a, b in zip(pin, pin[1:])}
	if steps in ({0}, {1}, {-1}):
		return _("That PIN is too easy to guess. Pick another.")
	if confirm is not None and (confirm or "").strip() != pin:
		return _("The two PINs are not the same.")
	return None


def _check_pin(user: str, pin: str | None) -> None:
	"""*user*'s counter PIN, or WrongPassword; the fifth wrong one turns it off until their password is used."""
	if _pin_off(user) or user not in _users_with_pin([user]):
		frappe.throw(_("Use the password: the PIN is off."), WrongPassword, title=_("PIN"))
	try:
		check_password(user, pin or "", fieldname=PIN_FIELD, delete_tracker_cache=False)
	except frappe.AuthenticationError:
		key = _pin_fail_key(user)
		frappe.cache.incrby(key, 1)
		left = PIN_TRIES - cint(frappe.cache.get(key))
		frappe.throw(_("That PIN is not right.") + " " + (_("Use the password now: the PIN is off.") if left <= 0
			else _("{0} tries left.").format(left)), WrongPassword, title=_("PIN"))
	frappe.cache.delete(_pin_fail_key(user))


@frappe.whitelist(methods=["POST"])
def set_pin(password: str, pin: str, confirm: str) -> dict:
	"""Set (or change) the signed-in user's counter PIN.

	Args:
	    password: Their login password.
	    pin: The new PIN, 4–6 digits, not 1111 / 1234-style.
	    confirm: The new PIN again.

	Returns:
	    {"has_pin": True}.
	"""
	frappe.only_for(COUNTER_ROLES)
	if frappe.session.user in ("Guest", "Administrator"):
		frappe.throw(_("Administrator signs in with the password only."))
	_check_own_password(password)
	problem = pin_problem(pin, confirm)
	if problem:
		frappe.throw(problem, title=_("PIN"))
	update_password(frappe.session.user, pin.strip(), fieldname=PIN_FIELD)
	frappe.cache.delete(_pin_fail_key(frappe.session.user))
	return {"has_pin": True}


@frappe.whitelist(methods=["POST"])
def clear_pin(user: str) -> dict:
	"""Clear someone's counter PIN (they sign in with the password, and may set a new one).

	Args:
	    user: The user whose PIN goes.

	Returns:
	    {"has_pin": False}.
	"""
	frappe.only_for(PIN_MANAGER_ROLES)
	remove_encrypted_password("User", user, PIN_FIELD)
	frappe.cache.delete(_pin_fail_key(user))
	return {"has_pin": False}


@frappe.whitelist(methods=["POST"])
def switch_with_pin(user: str, pin: str) -> dict:
	"""Sign someone else in at this counter with their PIN: a new session for them
	(frappe's LoginManager, so the login history records it, and its IP / hours rules
	apply) and this one ends. Only a counter user who has a PIN, without a second sign-in
	step; anyone else signs in with their password (frappe's login).

	Args:
	    user: Who is taking over the counter.
	    pin: Their counter PIN.

	Returns:
	    {"user"}.
	"""
	frappe.only_for(COUNTER_ROLES)
	if user == frappe.session.user or user not in {u["user"] for u in counter_users()} or user == "Administrator":
		frappe.throw(_("Pick someone from the list."), WrongPassword, title=_("PIN"))
	from frappe.twofactor import should_run_2fa

	if should_run_2fa(user):
		frappe.throw(_("This user signs in with a second step; sign in from the login page."), WrongPassword)
	_check_pin(user, pin)
	old_sid = frappe.session.sid
	frappe.local.login_manager.login_as(user)
	if old_sid and old_sid != frappe.session.sid:
		from frappe.sessions import delete_session
		delete_session(old_sid, reason="Switched user")
	return {"user": user}


def on_password_login(login_manager) -> None:
	"""on_login: someone signed in with their password (frappe's login, not a PIN switch),
	so a PIN turned off by wrong tries is on again."""
	request = getattr(frappe.local, "request", None)
	if getattr(request, "path", None) == "/api/method/login" or frappe.local.form_dict.get("cmd") == "login":
		frappe.cache.delete(_pin_fail_key(login_manager.user))
