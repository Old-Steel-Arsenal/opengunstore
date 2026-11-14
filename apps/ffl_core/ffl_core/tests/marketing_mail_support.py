"""Test support for marketing mail: MarketingTestCase plus a marketing Email Account that sends, the
tracking switches off, readers for the mail queue, and fake web requests for the Guest pages."""
from __future__ import annotations

import email as email_lib
from contextlib import contextmanager

import frappe
from frappe.utils import set_request

from ffl_core.marketing import CONTACT, SETTINGS, contacts
from ffl_core.marketing.contacts import NOT_SUBSCRIBED
from ffl_core.tests.marketing_support import MarketingTestCase, address

ACCOUNT = "_T Marketing Mail"
SENDER = "mkt-news@store.test"
_ip = [0]


def contact(status: str = NOT_SUBSCRIBED, **names) -> str:
	"""A new contact in *status* (reached the way the code reaches it)."""
	email = address("mail")
	contacts.get_or_create(email, **names)
	if status != NOT_SUBSCRIBED:
		contacts.set_status(email, status, channel="test")
	return email


def queue_rows(email: str) -> list[dict]:
	return frappe.get_all("Email Queue", filters={"reference_doctype": CONTACT, "reference_name": email},
		fields=["name", "message", "sender", "status"], order_by="creation asc")


def queue_rows_to(email: str) -> list[str]:
	"""Every Email Queue row with *email* among its recipients, whatever its reference."""
	return frappe.get_all("Email Queue Recipient", filters={"recipient": email}, pluck="parent")


def head_and_body(message: str) -> tuple[str, str]:
	"""A raw MIME message split at its first blank line."""
	head, _, body = message.partition("\r\n\r\n")
	return head, body


def html_of(message: str) -> str:
	parsed = email_lib.message_from_string(message)
	for part in parsed.walk():
		if part.get_content_type() == "text/html":
			return part.get_payload(decode=True).decode()
	return ""


class MailTestCase(MarketingTestCase):
	def setUp(self):
		super().setUp()
		# Frappe memoises the outgoing account per request; a rolled-back account must not be reused.
		frappe.local.outgoing_email_account = {}
		self.addCleanup(setattr, frappe.local, "outgoing_email_account", {})
		if not frappe.db.exists("Email Account", ACCOUNT):
			frappe.get_doc({"doctype": "Email Account", "email_account_name": ACCOUNT, "email_id": SENDER,
				"enable_incoming": 0, "enable_outgoing": 0, "awaiting_password": 1}).insert(ignore_permissions=True)
		# Set past the account's own save, which would connect to a mail server.
		frappe.db.set_value("Email Account", ACCOUNT, {"enable_outgoing": 1, "smtp_server": "smtp.invalid",
			"smtp_port": 587, "default_outgoing": 0}, update_modified=False)
		self.set_single(SETTINGS, "email_account", ACCOUNT)
		self.set_single(SETTINGS, "track_opens", 0)
		self.set_single(SETTINGS, "track_clicks", 0)

	@contextmanager
	def web_request(self, method: str, path: str, form: dict | None = None, user: str = "Guest"):
		"""A request to a website page as *user* from a fresh IP address (the rate limits count per
		address); request, form and user put back afterwards."""
		saved = (getattr(frappe.local, "request", None), frappe.local.form_dict, getattr(frappe.local, "request_ip", None))
		frappe.set_user(user)   # first: it empties form_dict
		_ip[0] += 1
		frappe.local.request_ip = f"10.77.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
		frappe.local.form_dict = frappe._dict(form or {})
		set_request(method=method, path=path)
		try:
			yield
		finally:
			frappe.set_user("Administrator")
			frappe.local.request, frappe.local.form_dict, frappe.local.request_ip = saved
