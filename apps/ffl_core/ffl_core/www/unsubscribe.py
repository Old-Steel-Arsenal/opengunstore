"""/unsubscribe?t=<token> — unsubscribe from the store's marketing mail (Guest).

GET shows the store's name and one button; POST (the button, or a mailbox provider's one-click
unsubscribe) unsubscribes. What each request does: ``ffl_core.marketing.unsubscribe``.
"""
from __future__ import annotations

import frappe

no_cache = 1
sitemap = 0


def get_context(context):
	from ffl_core.marketing import unsubscribe

	context.no_header = 1
	context.no_breadcrumbs = 1
	method = frappe.request.method if frappe.request else "GET"
	context.update(unsubscribe.page(frappe.form_dict, method))
	context.store_name = unsubscribe.store_name()
	context.invalid_message = unsubscribe.invalid_message()
	# A staff browser is a logged-in session, whose POSTs Frappe checks for the CSRF token; Guest needs none.
	context.csrf_token = frappe.sessions.get_csrf_token() if frappe.session.user != "Guest" else ""
	return context
