"""The one way mail goes to a Marketing Contact: :func:`send`.

Nothing else may call ``frappe.sendmail`` with ``reference_doctype="Marketing Contact"`` (a test scans
the code for it). :func:`send`, in order:

1. reads the contact's status and refuses what its *category* may not reach: ``marketing`` mail
   (digests, campaigns, welcome series) only ``Subscribed``; ``service`` mail (what the customer asked
   for: a back-in-stock notice, a review invitation) anyone not suppressed. A refusal is a ``Skipped``
   event with the reason;
2. claims the send: a ``Sent`` event keyed ``sent:<campaign>:<email>``, before anything is queued. A
   second call for the same campaign and address finds the key and sends nothing, so a retry or a
   concurrent run never mails anyone twice;
3. renders the mail: ``templates/marketing/<template>.html`` inside the store's shell
   (``base.html``: name, logo, address, phone, unsubscribe link), autoescaped;
4. adds tracking when Marketing Settings asks for it (``ffl_core.marketing.track``);
5. queues it through ``frappe.sendmail`` from the marketing Email Account, with ``List-Unsubscribe``
   headers (see :func:`restore_list_unsubscribe`).

The claim and the Email Queue row are written in the same transaction: if queueing fails, the claim
is rolled back with it (and the error raised), so "Sent" on the log always means a queued mail, and a
mail that was not queued can be sent again.

Frappe's queue is the second gate: it drops a recipient with an ``Email Unsubscribe`` for the same
reference, which :func:`ffl_core.marketing.contacts.set_status` files for every suppressed contact.
"""
from __future__ import annotations

import re
from email.utils import formataddr
from pathlib import Path

import frappe
from frappe import _
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ffl_core.marketing import CONTACT, EVENT, SETTINGS, enabled, has_site_url
from ffl_core.marketing.contacts import (
	NOT_SUBSCRIBED, SENT, SKIPPED, SUBSCRIBED, event_detail, log_event, normalize_email, plain_text,
)
from ffl_core.marketing.track import CAMPAIGN_CAP, open_url, switches, tracker
from ffl_core.marketing.unsubscribe import ONE_CLICK, store_name
from ffl_core.marketing.unsubscribe import url as unsubscribe_url
from ffl_core.store_identity import company_brand
from ffl_core.utils import default_company, single_raw

MARKETING = "marketing"
SERVICE = "service"
# Who each category of mail may reach.
REACHES = {MARKETING: (SUBSCRIBED,), SERVICE: (SUBSCRIBED, NOT_SUBSCRIBED)}

TEMPLATES = Path(__file__).parent / "templates" / "marketing"
# Frappe wraps a queued mail's HTML in a template of its own; this one adds nothing to ours.
WRAPPER = "ffl_core/marketing/templates/marketing/passthrough.html"
_TEMPLATE_NAME = re.compile(r"^[a-z0-9_]{1,60}$")
_env: Environment | None = None

# lazy: no send-rate limit of our own; Frappe's queue sends in batches, enough for a few thousand
# contacts. Add a per-minute cap here when a list reaches tens of thousands.


class MarketingMailError(frappe.ValidationError):
	"""Marketing mail cannot be sent as configured (switched off, no sending account, no store address, no
	site address for its links)."""


def send(contact, mail: str, subject: str, template: str, context: dict | None = None, *,
		category: str, campaign: str) -> bool:
	"""Queue one marketing mail to *contact*; True when it was queued.

	Args:
	    contact: A Marketing Contact or its email.
	    mail: Which mail this is (a stable key, e.g. ``new_arrivals``); kept on the events.
	    subject: The subject line (plain text).
	    template: The body template's name, ``templates/marketing/<template>.html``.
	    context: Values for the template; every one is HTML-escaped where it is printed.
	    category: ``marketing`` or ``service`` (who it may reach, see the module docstring).
	    campaign: This send's key; one mail per campaign and address, ever.

	Returns:
	    True when queued; False when the contact's status does not allow it (a ``Skipped`` event),
	    when this campaign was already sent to them, or when Frappe's queue dropped the recipient.

	Raises ``MarketingMailError`` while marketing is off, no sending account is set up or the store has
	no address (:func:`sending_address`).
	"""
	if category not in REACHES:
		frappe.throw(_("Unknown mail category: {0}").format(frappe.utils.escape_html(str(category))))
	if not _TEMPLATE_NAME.match(template or "") or not (TEMPLATES / f"{template}.html").is_file():
		frappe.throw(_("No marketing mail template named {0}").format(frappe.utils.escape_html(str(template))))
	if not campaign or plain_text(campaign, CAMPAIGN_CAP) != campaign:
		frappe.throw(_("A campaign key is one line of at most {0} characters.").format(CAMPAIGN_CAP))
	sender = sending_address()
	email = normalize_email(contact if isinstance(contact, str) else contact.name)
	status = frappe.db.get_value(CONTACT, email, "status") if email else None
	if status is None:
		frappe.throw(_("No marketing contact {0}").format(frappe.utils.escape_html(str(contact))), frappe.DoesNotExistError)

	if status not in REACHES[category]:
		log_event(email, SKIPPED, mail=mail, campaign=campaign, dedupe_key=f"skipped:{campaign}:{email}",
			detail=event_detail(reason="status", status=status, category=category))
		return False

	frappe.db.savepoint("mkt_send")
	claim = log_event(email, SENT, mail=mail, campaign=campaign, dedupe_key=f"sent:{campaign}:{email}")
	if not claim:   # already sent in this campaign
		return False
	try:
		queued = frappe.sendmail(
			recipients=[email], sender=sender, subject=subject,
			message=render(template, context, email=email, campaign=campaign, subject=subject),
			reference_doctype=CONTACT, reference_name=email,
			add_unsubscribe_link=0, delayed=True, wrapper=WRAPPER, add_css=False,
			reply_to=single_raw(SETTINGS, "reply_to") or None,
			email_headers={"List-Unsubscribe": f"<{unsubscribe_url(email)}>",
				"List-Unsubscribe-Post": f"List-Unsubscribe={ONE_CLICK}"},
		)
	except Exception:
		frappe.db.rollback(save_point="mkt_send")   # the claim goes with the mail that was not queued
		raise
	if not queued:   # Frappe's queue dropped the recipient (an Email Unsubscribe for this contact)
		frappe.db.rollback(save_point="mkt_send")
		log_event(email, SKIPPED, mail=mail, campaign=campaign, dedupe_key=f"skipped:{campaign}:{email}",
			detail=event_detail(reason="dropped_by_queue", category=category))
		return False
	frappe.db.set_value(EVENT, claim, "detail", event_detail(queue=queued.name, category=category),
		update_modified=False)
	return True


def sending_address() -> str:
	"""``Name <address>`` of the marketing Email Account. Raises ``MarketingMailError`` while marketing is
	off; when the account is not set, not found or not enabled for outgoing mail (Frappe would quietly
	fall back to the default account, the store's transactional identity); or when the store's Company
	has no address (every marketing mail carries the store's postal address); or when there is neither a
	web request nor a configured ``host_name`` to build the mail's links from (a background send would
	mail an unsubscribe link nobody can open)."""
	if not enabled():
		frappe.throw(_("Marketing is switched off (Marketing Settings › Enabled)."), MarketingMailError)
	name = single_raw(SETTINGS, "email_account")
	account = frappe.db.get_value("Email Account", name, ["email_id", "enable_outgoing"], as_dict=True) if name else None
	if not account or not account.email_id:
		frappe.throw(_("Choose the marketing Email Account in Marketing Settings."), MarketingMailError)
	if not account.enable_outgoing:
		frappe.throw(_("Email Account {0} does not send mail: enable Outgoing on it.").format(
			frappe.utils.escape_html(name)), MarketingMailError)
	if not company_brand(default_company()).get("address_line"):
		frappe.throw(_("Add the store's address to Company {0}: every marketing email carries it.").format(
			frappe.utils.escape_html(default_company())), MarketingMailError)
	if not has_site_url():
		frappe.throw(_("This site's web address is not set, so marketing mail cannot carry working links. "
			"Set host_name in the site configuration."), MarketingMailError)
	return formataddr((store_name(), account.email_id))


def render(template: str, context: dict | None, *, email: str, campaign: str, subject: str = "") -> str:
	"""The full HTML of *template* for *email* in *campaign*: the shell, the body, the links. Every
	value is HTML-escaped where the template prints it (Jinja autoescape)."""
	tracking = switches()
	contact = frappe.db.get_value(CONTACT, email, ["email", "first_name", "last_name"], as_dict=True) \
		or frappe._dict(email=email, first_name="", last_name="")
	brand = company_brand(default_company())
	values = {
		**(context or {}),
		"subject": subject,
		"contact": contact,
		"brand": brand,
		"track": tracker(email, campaign, clicks=tracking["track_clicks"]),
		"unsubscribe_url": unsubscribe_url(email),
		"open_pixel_url": open_url(email, campaign) if tracking["track_opens"] else "",
	}
	return _jenv().get_template(f"{template}.html").render(values)


def _jenv() -> Environment:
	global _env
	if _env is None:
		_env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True, undefined=StrictUndefined)
	return _env


def restore_list_unsubscribe(doc, method=None) -> None:
	"""Email Queue before_insert: give a marketing mail its ``List-Unsubscribe`` headers back.

	Frappe prefixes every custom header with ``X-`` (``EMail.add_headers``), and mailbox providers only
	read ``List-Unsubscribe`` / ``List-Unsubscribe-Post``. Only rows sent on a Marketing Contact; only
	those two header names, only in the message's own header block (before the first blank line). The
	values and the body are left exactly as they are."""
	if doc.get("reference_doctype") != CONTACT or not doc.get("message"):
		return
	message = doc.message
	end = re.search(r"\r?\n\r?\n", message)
	head, rest = (message[:end.start()], message[end.start():]) if end else (message, "")
	doc.message = _X_LIST_UNSUBSCRIBE.sub(r"\1:", head) + rest


_X_LIST_UNSUBSCRIBE = re.compile(r"^X-(List-Unsubscribe(?:-Post)?):", re.MULTILINE)
