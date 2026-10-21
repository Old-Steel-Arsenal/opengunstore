"""/agreement/terminal — the page a store's signing tablet keeps open (Guest).

A static shell like ``/agreement/sign``: pair once with a code from the Signing
Terminal record, then wait; when the counter sends an agreement the page opens it
on the signing screen, and comes back here once it is signed. Talks only to
``ffl_core.signing_terminal.pair_terminal`` / ``terminal_poll``. No desk bundle;
the only link is Start Form 4473 (FastBound's customer page, when the store uses it).
"""
from __future__ import annotations

import frappe

from ffl_core.hooks import _hashed

no_cache = 1
sitemap = 0


def get_context(context):
	context.no_header = 1
	context.no_breadcrumbs = 1
	# Same as /agreement/sign: a logged-in browser's POSTs need the CSRF token.
	context.csrf_token = frappe.sessions.get_csrf_token() if frappe.session.user != "Guest" else ""
	context.pad_js = _hashed("agreement/signing_pad.js")
	context.page_js = _hashed("agreement/terminal_page.js")
	context.page_css = _hashed("agreement/signing.css")
	return context
