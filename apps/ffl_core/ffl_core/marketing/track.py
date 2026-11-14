"""Open and click tracking for marketing mail (Guest endpoints).

A mail template asks for tracking explicitly: ``{{ track(url) }}`` for a link (the mailer passes
``track``), and the shell adds the open pixel. Nothing rewrites a mail's HTML. Both follow Marketing
Settings › Track opens / Track clicks when the mail is built; the endpoints keep answering for mail
already sent, and record only while their switch is on.

* ``open?c=<campaign>&t=<contact token>``: a 1x1 GIF. The first open of a campaign by a contact is a
  ``Opened`` event (``opened:<campaign>:<email>``); later ones are not recorded.
* ``click?u=<url>&c=&t=&s=<signature of u>``: a redirect to ``u``, only when ``s`` is this site's
  signature of ``u`` and ``u`` is an http(s) address. Anything else is refused: no open redirect.
  The first click on a link per campaign and contact is a ``Clicked`` event.

Only a contact the campaign was sent to (its ``Sent`` event) is recorded; anything else is answered the
same way and recorded nowhere, so a response never says whether an address is on file.
"""
from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable
from urllib.parse import urlencode, urlsplit

import frappe
from frappe.rate_limiter import rate_limit
from werkzeug.wrappers import Response

from ffl_core.marketing import EVENT, SETTINGS, site_url
from ffl_core.marketing.contacts import CLICKED, OPENED, event_detail, log_event, plain_text
from ffl_core.marketing.signing import CLICK, contact_token, email_of, sign, signed
from ffl_core.utils import single_flags

OPEN_PATH = "/api/method/ffl_core.marketing.track.open"
CLICK_PATH = "/api/method/ffl_core.marketing.track.click"
CAMPAIGN_CAP = 80
URL_CAP = 2000
PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
_UNSAFE_URL = re.compile(r"[\s\"'<>\\`\x00-\x1f\x7f]")
# Mail clients fetch images through shared proxies (one address for many readers), so the per-address
# limits are wide; they stop one client hammering, not ordinary reading.
OPEN_LIMIT = CLICK_LIMIT = 5000


def switches() -> dict[str, bool]:
	"""Marketing Settings › Track opens / Track clicks."""
	return single_flags(SETTINGS, {"track_opens": 0, "track_clicks": 0})


def is_web_url(url) -> bool:
	"""An absolute http(s) address with a host, and nothing that could break out of an attribute."""
	if not isinstance(url, str) or not url or len(url) > URL_CAP or _UNSAFE_URL.search(url):
		return False
	try:
		parts = urlsplit(url)
	except ValueError:
		return False
	return parts.scheme in ("http", "https") and bool(parts.hostname)


def open_url(email: str, campaign: str) -> str:
	"""The open pixel's address for *email* in *campaign*."""
	return site_url(OPEN_PATH) + "?" + urlencode({"c": campaign, "t": contact_token(email)})


def click_url(url: str, email: str, campaign: str) -> str:
	"""A tracked link to *url* for *email* in *campaign*."""
	return site_url(CLICK_PATH) + "?" + urlencode({"u": url, "c": campaign, "t": contact_token(email), "s": sign(CLICK, url)})


def tracker(email: str, campaign: str, *, clicks: bool) -> Callable[[str], str]:
	"""``track(url)`` for a template: the tracked link when *clicks* and *url* is a web address, else *url*."""
	def track(url) -> str:
		url = str(url or "")
		return click_url(url, email, campaign) if clicks and is_web_url(url) else url
	return track


@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=OPEN_LIMIT, seconds=3600)
def open(c: str | None = None, t: str | None = None):
	"""The open pixel of a marketing mail (Guest): always a 1x1 GIF.

	Records the first open of campaign *c* by the contact *t* stands for, while Track opens is on and
	the campaign was sent to them. 5000 calls an hour per IP address.

	Args:
	    c: The campaign.
	    t: The contact's token from the mail.

	Returns:
	    A 1x1 GIF image, never cached.
	"""
	if switches()["track_opens"]:
		_record(OPENED, c, t, "opened")
	return Response(PIXEL, mimetype="image/gif", headers={"Cache-Control": "no-store, max-age=0"})


@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=CLICK_LIMIT, seconds=3600)
def click(u: str | None = None, c: str | None = None, t: str | None = None, s: str | None = None):
	"""A tracked link in a marketing mail (Guest): a redirect to *u*.

	Only when *s* is this site's signature of *u* and *u* is an http(s) address; anything else gets a
	400 and no redirect. Records the contact's first click on that link in campaign *c* while Track
	clicks is on and the campaign was sent to them. 5000 calls an hour per IP address.

	Args:
	    u: Where the link goes.
	    c: The campaign.
	    t: The contact's token from the mail.
	    s: The signature of *u*.

	Returns:
	    A 302 redirect to *u*, or a 400 plain-text "This link is not valid."
	"""
	if not is_web_url(u) or not signed(CLICK, u, s):
		return Response("This link is not valid.", status=400, mimetype="text/plain")
	if switches()["track_clicks"]:
		_record(CLICKED, c, t, "clicked", link=u)
	return Response(status=302, headers={"Location": u, "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def _record(kind: str, campaign, token, prefix: str, link: str | None = None) -> bool:
	"""Log *kind* for the contact *token* stands for in *campaign*; once per key. True when logged.
	Never fails the response: a failure is an Error Log."""
	try:
		email = email_of(token)
		campaign = plain_text(campaign, CAMPAIGN_CAP) if isinstance(campaign, str) else ""
		if not email or not campaign:
			return False
		sent = frappe.db.get_value(EVENT, {"dedupe_key": f"sent:{campaign}:{email}"}, ["name", "mail"], as_dict=True)
		if not sent:   # never sent this campaign to this address: nothing to record
			return False
		key = f"{prefix}:{campaign}:{email}"
		if link:
			key += ":" + hashlib.sha256(link.encode()).hexdigest()[:12]
		logged = log_event(email, kind, mail=sent.mail, campaign=campaign, dedupe_key=key,
			detail=event_detail(url=link) if link else None)
		if logged and frappe.request:
			frappe.local.flags.commit = True   # a GET: Frappe commits it only when asked
		return bool(logged)
	except Exception:
		frappe.log_error(title=f"Marketing: {kind} not recorded", message=frappe.get_traceback())
		return False
