"""Signed values in marketing mail links: a contact's token (unsubscribe, open tracking) and a click's target.

A contact's token is ``base64url(email) + "." + HMAC-SHA256(key, email)`` cut to 32 hex characters. Every
key is derived from the site's ``encryption_key`` with a purpose label, so there is no new secret to
keep; changing ``encryption_key`` invalidates the links in mail already sent.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac

from frappe.utils.password import get_encryption_key

from ffl_core.marketing.contacts import normalize_email

SIGNATURE_LENGTH = 32   # hex characters: 128 bits
CONTACT = "contact"
CLICK = "click"
_TOKEN_CAP = 400


def _key(purpose: str) -> bytes:
	return hmac.new(get_encryption_key().encode(), f"ffl_core.marketing:{purpose}".encode(), hashlib.sha256).digest()


def sign(purpose: str, value: str) -> str:
	"""The signature of *value* for *purpose*."""
	return hmac.new(_key(purpose), value.encode(), hashlib.sha256).hexdigest()[:SIGNATURE_LENGTH]


def signed(purpose: str, value, signature) -> bool:
	"""True when *signature* is *value*'s for *purpose* (constant-time)."""
	if not isinstance(value, str) or not isinstance(signature, str) or len(signature) != SIGNATURE_LENGTH:
		return False
	return hmac.compare_digest(sign(purpose, value), signature)


def contact_token(email: str) -> str:
	"""The token that stands for *email* in a link."""
	head = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")
	return f"{head}.{sign(CONTACT, email)}"


def email_of(token) -> str | None:
	"""The email a token stands for, or None when the token is not one this site signed."""
	if not isinstance(token, str) or len(token) > _TOKEN_CAP or token.count(".") != 1:
		return None
	head, signature = token.split(".")
	try:
		email = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)).decode()
	except (binascii.Error, ValueError, UnicodeDecodeError):
		return None
	if not signed(CONTACT, email, signature) or normalize_email(email) != email:
		return None
	return email
