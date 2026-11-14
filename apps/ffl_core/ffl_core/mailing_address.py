"""Who a store mailing list may take: a customer's address and name as a list entry.

The storefront customer push (``ffl_woo_sync.woocommerce.customer_events``) and the marketing list's
store-customer intake (``ffl_core.marketing.customers``) read a Customer the same way, so the rules
are written once, here: skip a blank address and the shop's own domain (staff and test mail, not
shoppers), and split a customer's name into first and last.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from ffl_core.utils import single_raw

_DEDUPE_SUFFIX = re.compile(r"\s+-\s*\d+$")


def store_host(store_url: str) -> str:
	"""The shop's host, lower-cased and without a leading ``www.``."""
	host = (urlparse(store_url if "//" in store_url else "//" + store_url).hostname or "").lower()
	return host[4:] if host.startswith("www.") else host


def shop_host() -> str:
	"""The host of this store's website (WooCommerce Settings › Store URL), or "" when none is set."""
	return store_host(single_raw("WooCommerce Settings", "store_url") or "")


def clean_email(raw, shop_host: str = "") -> str:
	"""The address to send, or "" to skip the customer: blank, no ``@``, or an
	address at the shop's own domain (staff and test mail, not shoppers)."""
	email = str(raw or "").strip().lower()
	if "@" not in email:
		return ""
	if shop_host and email.rsplit("@", 1)[1] == shop_host:
		return ""
	return email


def split_name(contact_first, contact_last, customer_name) -> tuple[str, str]:
	"""``(first, last)``. A Contact with both names wins. Otherwise the whole
	name is split into its first and last words — the Contact's lone first name
	when it has one (ERPNext often stores the full name there), else the
	Customer's, minus a trailing `` - <digits>`` duplicate-name suffix."""
	first, last = str(contact_first or "").strip(), str(contact_last or "").strip()
	if first and last:
		return first, last
	full = _DEDUPE_SUFFIX.sub("", " ".join((first or str(customer_name or "")).split()))
	words = full.split()
	if not words:
		return "", last
	return words[0], last or (words[-1] if len(words) > 1 else "")
