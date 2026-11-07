"""Email and Phone on the Customer form, kept equal to the customer's primary Contact.

ERPNext keeps a customer's email and mobile on its primary Contact; the Customer's own ``email_id`` /
``mobile_no`` are read-only copies on the Address & Contact tab. ``ffl_email`` / ``ffl_phone``
(``setup.customer_identity``) sit on the Details tab and take an edit (a virtual field would not:
the Desk form shows those read-only).

- Customer validate (:func:`sync_email_phone`): a value typed there becomes the Contact's primary
  one, the old value staying on the Contact as a secondary. A customer with no primary Contact gets
  one from ERPNext's own ``Customer.create_primary_contact`` (on_update), which reads ``email_id`` /
  ``mobile_no``. Otherwise the fields take the Contact's values.
- Contact on_update (:func:`contact_changed`): an edit on the Contact itself reaches the customers it
  is the primary Contact of.
- :func:`backfill` fills the fields once for customers made before them (setup, every migrate).
"""
from __future__ import annotations

import frappe

_PAIRS = (("ffl_email", "email_id"), ("ffl_phone", "mobile_no"))


def sync_email_phone(doc, method=None) -> None:
	"""Customer validate. A blank field changes nothing on the Contact (lazy: clearing a value is
	done on the Contact itself)."""
	typed = {}
	for field, source in _PAIRS:
		value = (doc.get(field) or "").strip()
		if value and value != (doc.get(source) or "") and doc.has_value_changed(field):
			typed[source] = value
	picked = not doc.is_new() and doc.has_value_changed("customer_primary_contact")
	if typed and doc.customer_primary_contact and not picked:   # a newly picked Contact brings its own values
		_make_primary(doc.customer_primary_contact, typed.get("email_id"), typed.get("mobile_no"))
	if not picked:
		doc.update(typed)
	for field, source in _PAIRS:
		doc.set(field, doc.get(source) or "")


def _make_primary(contact: str, email: str | None, phone: str | None) -> None:
	doc = frappe.get_doc("Contact", contact)
	if email:
		if not any(row.email_id.lower() == email.lower() for row in doc.email_ids):
			doc.append("email_ids", {"email_id": email})
		for row in doc.email_ids:
			row.is_primary = int(row.email_id.lower() == email.lower())
	if phone:
		if not any(row.phone == phone for row in doc.phone_nos):
			doc.append("phone_nos", {"phone": phone})
		for row in doc.phone_nos:
			row.is_primary_mobile_no = int(row.phone == phone)
	doc.flags.ignore_permissions = True   # the user may edit the Customer; ERPNext's Contact rights differ
	doc.save()


def contact_changed(doc, method=None) -> None:
	"""Contact on_update: its customers' copies follow it."""
	frappe.db.set_value("Customer", {"customer_primary_contact": doc.name},
		{"email_id": doc.email_id or "", "mobile_no": doc.mobile_no or "",
		"ffl_email": doc.email_id or "", "ffl_phone": doc.mobile_no or ""}, update_modified=False)


def backfill() -> None:
	"""Customers made before the fields: copy ``email_id`` / ``mobile_no`` in. Idempotent."""
	for field, source in _PAIRS:
		frappe.db.sql(f"""update `tabCustomer` set `{field}` = `{source}`
			where ifnull(`{field}`, '') = '' and ifnull(`{source}`, '') != ''""")
