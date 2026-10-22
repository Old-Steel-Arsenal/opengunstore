"""Which card processors this site has, and which one takes new money.

Processors arrive through the ``gunshop_card_processors`` hook: each entry is a dotted path to
a zero-arg factory returning a :class:`~ffl_integrations.payments.base.CardProcessor`
(contract: docs/extensions/hooks.md). ffl_integrations registers Payroc through it, so the core
special-cases no processor. A factory that raises is logged and its processor counts as not
installed; so does one from a POS Extension the store has switched off.

At most one processor is ON — switched on AND configured (``CardProcessor.ready``): the single
"Credit Card" Mode of Payment is what they share, and nothing could say which gateway a swipe
should go to. A switch that is on without credentials does not count: Payroc's switch reads ON on
a site that never saved Payroc Settings, and a store running another processor must not have to
find and untick it first. The Payments page refuses to switch a second one on (and Payroc
Settings refuses to arm Payroc beside another); should a site end up with two anyway,
``active_processor`` refuses to pick one.
"""
from __future__ import annotations

import frappe
from frappe import _

from ffl_integrations.payments.base import CardProcessor

HOOK = "gunshop_card_processors"
# Every charge recorded before charges named their processor — POS Invoices with only the
# aggregate stamps, rows from before the `processor` field — was taken by Payroc, the only
# processor there was. The backfill patch writes it on every old row; this covers the stamps.
LEGACY_PROCESSOR = "payroc"


def processors() -> list[CardProcessor]:
	"""Every installed processor, in hook order (one per code: the first registration wins)."""
	out: list[CardProcessor] = []
	for path in frappe.get_hooks(HOOK):
		app = path.split(".", 1)[0]
		if not _app_running(app):
			continue
		try:
			p = frappe.get_attr(path)()
		except Exception:
			frappe.log_error(title=f"{HOOK} failed: {path}", message=frappe.get_traceback())
			continue
		if not isinstance(p, CardProcessor) or not p.code:
			frappe.log_error(title=f"{HOOK}: {path} returned no CardProcessor",
				message=f"{path}() returned {p!r}")
			continue
		if any(q.code == p.code for q in out):
			continue
		out.append(p)
	return out


def _app_running(app: str) -> bool:
	"""A POS Extension (an app declaring ``gunshop_extension``) counts only while it is
	effectively on; the core apps always run."""
	if not frappe.get_hooks("gunshop_extension", app_name=app):
		return True
	from ffl_core.extensions import is_enabled

	return is_enabled(app)


def _is_on(p: CardProcessor) -> bool:
	"""Switched on and configured. A configuration that cannot be read (a password that no
	longer decrypts) is not configured: it breaks that processor's terminal, never the
	counter's other work — but the Error Log says why the terminal went quiet."""
	try:
		return bool(p.ready())
	except Exception:  # noqa: BLE001 — unreadable config reads as not configured
		_warn_unreadable(p.code)
		return False


def _warn_unreadable(code: str) -> None:
	"""Logged at most once an hour per processor: the gates ask on every invoice save."""
	try:
		key = f"ffl_card_processor_unreadable:{code}"
		if frappe.cache().get_value(key):
			return
		frappe.cache().set_value(key, 1, expires_in_sec=3600)
		from ffl_integrations.notifications import safe_log_error

		safe_log_error(f"Card processor {code}: settings cannot be read, treated as off",
			frappe.get_traceback())
	except Exception:  # noqa: BLE001
		pass


def get_processor(code: str | None) -> CardProcessor | None:
	"""The installed processor with this code, or None."""
	return next((p for p in processors() if p.code == code), None) if code else None


def active_processor() -> CardProcessor | None:
	"""The processor that is ON (switched on and configured) for new money, or None (cards are
	then plain tender on a standalone terminal). Two ON raises: guessing which gateway a swipe
	belongs to is not something to do quietly."""
	on = [p for p in processors() if _is_on(p)]
	if len(on) > 1:
		frappe.throw(_("More than one card processor is switched on ({0}). Switch all but one off "
			"in Settings → Payments.").format(", ".join(p.label for p in on)))
	return on[0] if on else None


def any_on() -> bool:
	"""Whether any card processor is ON (switched on and configured) — the question the
	validate-time gates ask, which must never throw."""
	return any(_is_on(p) for p in processors())


def label_for(code: str | None) -> str:
	"""A processor's display name for operator messages: the named one, else the active one,
	else plain "card processor". Never raises (messages are built on error paths)."""
	try:
		p = get_processor(code) if code else active_processor()
	except Exception:
		p = None
	return p.label if p else _("card processor")


def refuse_second_active(code: str) -> None:
	"""Throw when switching ``code`` on would make two processors ON. A processor's settings
	form calls it from validate while it is switched on and configured; the Payments page calls
	it before it flips one. Only processors that are ON count against it."""
	others = [p.label for p in processors() if p.code != code and _is_on(p)]
	if others:
		frappe.throw(_("{0} is switched on. Only one card processor can take cards at a time — "
			"switch it off first.").format(", ".join(others)))
