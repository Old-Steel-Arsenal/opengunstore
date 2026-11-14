"""Marketing mail from the Desk: the test email (Marketing Settings › Send test email)."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import get_url, now_datetime

from ffl_core.marketing import CONTACT
from ffl_core.marketing.contacts import get_or_create, normalize_email
from ffl_core.marketing.mailer import SERVICE, send
from ffl_core.roles import MARKETING_MANAGER_ROLES

TEST_MAIL = "test"


@frappe.whitelist(methods=["POST"])
def send_test_email() -> dict:
	"""Send the signed-in staff member a test marketing email at their own address.

	It goes the way every marketing email goes: the mail queue from the marketing Email Account, the
	List-Unsubscribe headers, tracking as Marketing Settings has it, and a working unsubscribe link
	(using it unsubscribes this address, and the next test is refused until a manager puts it back).
	Each call is its own campaign, so it can be sent again.

	Returns:
	    {"queued": true when it is in the mail queue, "email": the address, "status": that address's
	    marketing status (a test is refused for an unsubscribed, bounced or complained one)}.
	"""
	frappe.only_for(MARKETING_MANAGER_ROLES)
	user = frappe.db.get_value("User", frappe.session.user, ["email", "first_name", "last_name"], as_dict=True)
	email = normalize_email(user.email if user else "")
	if not email:
		frappe.throw(_("Your user account has no valid email address to send the test to."))
	contact = get_or_create(email, first_name=user.first_name, last_name=user.last_name, source="staff-test")
	campaign = f"test:{now_datetime():%Y%m%d%H%M%S}:{frappe.generate_hash(length=8)}"
	queued = send(contact, TEST_MAIL, _("Test email"), "test", {"sent_by": frappe.session.user, "link": get_url()},
		category=SERVICE, campaign=campaign)
	return {"queued": queued, "email": email, "status": frappe.db.get_value(CONTACT, email, "status")}
