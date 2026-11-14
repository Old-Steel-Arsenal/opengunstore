"""Bounces and complaints from Amazon SES, delivered by an SNS topic subscribed to :func:`notify` over HTTPS.

Every message is checked before anything is read from it:

* its ``TopicArn`` must be Marketing Settings › SNS topic ARN (403 otherwise, and while none is set);
* its signature must verify (SignatureVersion 1 = RSA SHA-1, 2 = RSA SHA-256) against the certificate
  at ``SigningCertURL``, which must be an ``https://sns.<region>.amazonaws.com/….pem`` address.

Then, by ``Type``:

* ``SubscriptionConfirmation``: the subscription is confirmed by fetching ``SubscribeURL`` (also an
  ``https://sns.<region>.amazonaws.com`` address);
* ``Notification`` carrying an SES event: a ``Permanent`` bounce moves the contact to ``Bounced``; a
  ``Transient`` or ``Undetermined`` one is only logged (a ``Bounced`` event, status unchanged); a
  complaint moves the contact to ``Complained``. Deliveries and other event types are ignored, and so
  is any address with no Marketing Contact (the same SES identity sends the store's transactional mail).

A message is processed once: its events are keyed ``sns:<MessageId>:<email>``, and a status already
reached does not move again, so SNS retrying a delivery changes nothing.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parseaddr
from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from ffl_core.marketing import CONTACT, SETTINGS
from ffl_core.marketing.contacts import BOUNCED, COMPLAINED, event_detail, log_event, normalize_email, plain_text, set_status
from ffl_core.utils import single_raw

SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com$")
# The fields signed for each message type, in the order AWS documents.
SIGNED = {
	"Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
	"SubscriptionConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"),
	"UnsubscribeConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"),
}
OPTIONAL = {"Subject"}
BODY_CAP = 512 * 1024   # an SNS message is at most 256 KB, plus the envelope
CERT_CACHE_SECONDS = 24 * 3600
# SNS posts from many AWS addresses; a bounce wave after a large send is many posts. Wide, per address.
LIMIT = 10000


class Refused(frappe.PermissionError):
	"""Not a message from the configured SNS topic (403)."""


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=LIMIT, seconds=3600)
def notify():
	"""Amazon SNS posts SES bounce and complaint notifications here (Guest).

	The body is the SNS message (JSON, sent as text/plain). Refused with a 403 unless it comes from the
	topic in Marketing Settings and its signature verifies. 10000 calls an hour per IP address.

	Returns:
	    {"type": the SNS message type, "processed": how many addresses changed or were logged}.
	"""
	request = frappe.request
	if request.content_length and request.content_length > BODY_CAP:
		raise Refused(_("Message too large."))
	return handle(request.get_data(cache=False, as_text=True))


def handle(body: str, *, http_get: Callable[[str], bytes] | None = None) -> dict:
	"""Check and process one SNS message (the request body). *http_get* fetches a URL's bytes; the
	default is an HTTPS GET whose certificates are cached a day. Raises ``Refused``."""
	try:
		message = json.loads(body)
	except (TypeError, ValueError):
		raise Refused(_("Not an SNS message."))
	if not isinstance(message, dict) or not isinstance(message.get("Type"), str):
		raise Refused(_("Not an SNS message."))
	topic = single_raw(SETTINGS, "sns_topic_arn")
	if not topic or message.get("TopicArn") != topic.strip():
		raise Refused(_("Not the configured SNS topic."))
	verify(message, http_get or _cached_get)
	kind = message["Type"]
	if kind == "SubscriptionConfirmation":
		url = message["SubscribeURL"]
		if not is_sns_url(url):
			raise Refused(_("Not an SNS address."))
		(http_get or _get)(url)
		return {"type": kind, "processed": 0}
	if kind == "Notification":
		return {"type": kind, "processed": _notification(message)}
	return {"type": kind, "processed": 0}


def is_sns_url(url, *, pem: bool = False) -> bool:
	"""An ``https://sns.<region>.amazonaws.com`` address (a ``.pem`` file when *pem*)."""
	if not isinstance(url, str):
		return False
	try:
		parts = urlsplit(url)
		port = parts.port
	except ValueError:
		return False
	return (parts.scheme == "https" and bool(SNS_HOST.match(parts.hostname or "")) and port is None
		and not parts.username and not parts.password and parts.netloc == parts.hostname
		and (not pem or (parts.path.endswith(".pem") and not parts.query)))


def string_to_sign(message: dict) -> str:
	"""What SNS signed for *message* (its type's fields, ``Key\\nValue\\n`` each). Raises ``Refused``
	for an unknown type or a missing field."""
	fields = SIGNED.get(message.get("Type"))
	if not fields:
		raise Refused(_("Unknown SNS message type."))
	out = []
	for key in fields:
		if key not in message:
			if key in OPTIONAL:
				continue
			raise Refused(_("Not an SNS message."))
		if not isinstance(message[key], str):
			raise Refused(_("Not an SNS message."))
		out.append(f"{key}\n{message[key]}\n")
	return "".join(out)


def verify(message: dict, http_get: Callable[[str], bytes]) -> None:
	"""Raise ``Refused`` unless *message*'s signature verifies against its SNS signing certificate."""
	from cryptography import x509
	from cryptography.exceptions import InvalidSignature
	from cryptography.hazmat.primitives import hashes
	from cryptography.hazmat.primitives.asymmetric import padding

	digest = {"1": hashes.SHA1, "2": hashes.SHA256}.get(message.get("SignatureVersion"))
	url = message.get("SigningCertURL")
	if not digest or not is_sns_url(url, pem=True):
		raise Refused(_("Not signed by SNS."))
	payload = string_to_sign(message)
	try:
		signature = base64.b64decode(message.get("Signature") or "", validate=True)
		cert = x509.load_pem_x509_certificate(http_get(url))
		now = datetime.now(UTC)
		if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
			raise ValueError("certificate out of date")
		cert.public_key().verify(signature, payload.encode(), padding.PKCS1v15(), digest())
	except (InvalidSignature, ValueError, TypeError, binascii.Error):
		raise Refused(_("The SNS signature does not verify."))


def _notification(message: dict) -> int:
	try:
		event = json.loads(message["Message"])
	except ValueError:
		return 0
	if not isinstance(event, dict):
		return 0
	kind = event.get("notificationType") or event.get("eventType")
	key = f"sns:{plain_text(message['MessageId'], 100)}"
	done = 0
	if kind == "Bounce":
		bounce = event.get("bounce") or {}
		permanent = bounce.get("bounceType") == "Permanent"
		for row in _rows(bounce.get("bouncedRecipients")):
			email = _known(row.get("emailAddress"))
			if not email:
				continue
			detail = event_detail(provider="ses", bounce_type=bounce.get("bounceType"), bounce_subtype=bounce.get("bounceSubType"),
				diagnostic=row.get("diagnosticCode"), message_id=message["MessageId"])
			if permanent:
				done += set_status(email, BOUNCED, reason=_reason("Bounce", bounce.get("bounceSubType")), detail=detail,
					dedupe_key=f"{key}:{email}")
			else:   # transient / undetermined: the address may work tomorrow
				done += bool(log_event(email, BOUNCED, detail=detail, dedupe_key=f"{key}:{email}"))
	elif kind == "Complaint":
		complaint = event.get("complaint") or {}
		for row in _rows(complaint.get("complainedRecipients")):
			email = _known(row.get("emailAddress"))
			if email:
				done += set_status(email, COMPLAINED, reason=_reason("Complaint", complaint.get("complaintFeedbackType")),
					detail=event_detail(provider="ses", feedback=complaint.get("complaintFeedbackType"),
						message_id=message["MessageId"]), dedupe_key=f"{key}:{email}")
	return done


def _rows(value) -> list[dict]:
	return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def _known(address) -> str | None:
	"""The contact's email for *address*, or None when it is not a Marketing Contact."""
	email = normalize_email(parseaddr(address)[1]) if isinstance(address, str) else ""
	return email if email and frappe.db.exists(CONTACT, email) else None


def _reason(what: str, kind) -> str:
	return f"{what} ({plain_text(kind, 60)})" if isinstance(kind, str) and kind else what


def _get(url: str) -> bytes:
	import requests

	response = requests.get(url, timeout=10, allow_redirects=False)
	response.raise_for_status()
	return response.content


def _cached_get(url: str) -> bytes:
	"""A signing certificate, cached by its (SNS-only) address for a day."""
	key = f"ffl_core:sns_cert:{url}"
	pem = frappe.cache.get_value(key)
	if not pem:
		pem = _get(url).decode("ascii", "replace")
		frappe.cache.set_value(key, pem, expires_in_sec=CERT_CACHE_SECONDS)
	return pem.encode() if isinstance(pem, str) else pem
