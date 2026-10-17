"""/agreement/sign#t=<token> — where a customer signs an agreement (Guest).

A static shell: nothing about the customer is rendered on the server. The page
reads the one-time token from the URL fragment (never sent to the server with
the page, so it stays out of access logs and Referer headers) and calls the two
Guest endpoints in ``ffl_core.agreements``. No desk or website bundle, no links
anywhere. Built for the store's tablet; works on a phone or a desktop browser.
"""
from __future__ import annotations

import frappe

from ffl_core.hooks import _hashed

no_cache = 1
sitemap = 0


def get_context(context):
	context.no_header = 1
	context.no_breadcrumbs = 1
	# A staff browser opening the link (the counter's "Open on this screen") is a
	# logged-in session, whose POSTs frappe checks for the CSRF token; Guest needs none.
	context.csrf_token = frappe.sessions.get_csrf_token() if frappe.session.user != "Guest" else ""
	context.pad_js = _hashed("agreement/signing_pad.js")
	context.page_js = _hashed("agreement/sign_page.js")
	context.page_css = _hashed("agreement/signing.css")
	return context
