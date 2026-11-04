"""Bring the store's people into the POS as Customers: everyone who bought online and every
registered store account, one Customer per email address.

Web orders have been booked to their real buyer since July 2026 (``revenue._resolve_web_customer``);
older ones went to the shared web customer, and store accounts that never ordered were never
seen. This fills both in, once, with the same lookup and the same new-buyer defaults the order
sync uses (``customer_profile.find_customer_by_email`` / ``new_retail_customer``): an address
that already belongs to a Customer — on the Customer, as a merged customer's alias, or on a
Contact linked to one — is left alone, so nothing is duplicated and no existing record is
changed. An alias of an FFL dealer does not count, as in the order sync: a retail buyer gets
their own Customer. Invoices are not touched.

Run with ``dry_run=1`` first; a store-sized run is best done with ``bench execute``.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import validate_email_address

from ffl_core.customer_profile import find_customer_by_email, new_retail_customer
from ffl_woo_sync.woocommerce.client import WooCommerceClient

SITE = "retail"
PER_PAGE = 100
# Orders that mean the person bought: placed and paid (or paid then refunded). Not a
# checkout draft, an unpaid pending or failed attempt, a cancelled unpaid order, or the trash.
BUYER_STATUSES = frozenset({"processing", "completed", "on-hold", "refunded"})
SAMPLE = 25
# Store accounts that are staff, not shoppers.
STAFF_ROLES = frozenset({"administrator", "shop_manager", "editor", "author", "contributor"})


def _pages(fetch):
	"""Every row of a paged Woo list (``fetch(page)`` returns one page)."""
	page = 1
	while True:
		batch = fetch(page) or []
		if not isinstance(batch, list):
			frappe.throw(_("The store answered page {0} with something other than a list. Nothing was imported; check the store's API and try again.").format(page))
		yield from batch
		if len(batch) < PER_PAGE:
			return
		page += 1


def _name(first, last) -> str:
	return " ".join(p.strip() for p in (first or "", last or "") if p and p.strip())


def collect_people(wc) -> dict[str, dict]:
	"""``{email: {"name", "phone"}}`` from registered accounts and buyers' orders. Orders come
	newest first, so a buyer's latest billing name and phone win; an account fills in only what
	no order gave."""
	people: dict[str, dict] = {}

	def add(email, name, phone):
		email = (email or "").strip().lower()
		if not email:
			return
		row = people.setdefault(email, {"name": "", "phone": ""})
		row["name"] = row["name"] or (name or "").strip()
		row["phone"] = row["phone"] or (phone or "").strip()

	# The raw list, not list_orders (which turns a bad answer into an empty page): a broken orders
	# endpoint must stop the run, not pass for a store with no buyers.
	for o in _pages(lambda p: wc.get("orders", params={"per_page": PER_PAGE, "page": p, "status": "any",
			"orderby": "date", "order": "desc"})):
		if o.get("status") not in BUYER_STATUSES:
			continue
		b = o.get("billing") or {}
		add(b.get("email"), _name(b.get("first_name"), b.get("last_name")), b.get("phone"))
	# Every role, not only "customer": accounts a plugin or an older theme registered as
	# "subscriber" (or a membership role) are shoppers too. Staff accounts are not.
	for c in _pages(lambda p: wc.get("customers", params={"per_page": PER_PAGE, "page": p, "orderby": "id",
			"role": "all"})):
		if (c.get("role") or "").lower() in STAFF_ROLES:
			continue
		b = c.get("billing") or {}
		add(c.get("email") or b.get("email"),
			_name(c.get("first_name"), c.get("last_name")) or _name(b.get("first_name"), b.get("last_name")),
			b.get("phone"))
	return people


def _is_dry_run(value) -> bool:
	"""Only an explicit 0 / false / no runs for real; anything else (a typo, "true") previews."""
	return str(value).strip().lower() not in ("0", "false", "no")


def _known(email: str) -> bool:
	"""Whether *email* already belongs to a Customer: the order sync's own lookup (an FFL
	dealer's alias excepted, as there), or a Contact linked to a Customer."""
	hit = find_customer_by_email(email)
	if hit and not (hit.get("via_alias") and hit.get("is_ffl_dealer")):
		return True
	contacts = set(frappe.get_all("Contact Email", filters={"parenttype": "Contact", "email_id": email}, pluck="parent"))
	contacts |= set(frappe.get_all("Contact", filters={"email_id": email}, pluck="name"))
	return bool(contacts) and bool(frappe.get_all("Dynamic Link", filters={"parenttype": "Contact",
		"parent": ["in", list(contacts)], "link_doctype": "Customer"}, limit=1))


def _create(name: str, email: str, phone: str) -> bool:
	"""Insert the Customer; a phone the POS rejects is dropped rather than losing the person.
	Returns whether the phone was dropped. Raises when even that fails."""
	for use_phone in ([True, False] if phone else [False]):
		frappe.db.savepoint("woo_customer_import")
		try:
			doc = new_retail_customer(name, email=email, phone=phone if use_phone else None)
			doc.flags.ignore_permissions = True   # a System Manager's one-off import
			doc.insert()
			return bool(phone) and not use_phone
		except Exception:
			frappe.db.rollback(save_point="woo_customer_import")
			if not use_phone or not phone:
				raise
	return False


@frappe.whitelist(methods=["POST"])
def import_customers(dry_run=1) -> dict:
	"""Make a Customer for every online buyer and registered store account the POS does not have.

	Args:
	    dry_run: Anything but an explicit 0 / false / no (the default is 1) reads the store and
	        reports what would be created, creating nothing.

	Returns:
	    dict: {"dry_run", "emails" (distinct addresses found), "existing" (already a Customer),
	    "invalid" (not an email address), "created" (or would be), "phone_dropped" (created
	    without the store's phone, which the POS rejected), "errors": [{email, error}],
	    "sample": [{email, name}] of the first new ones}.
	"""
	frappe.only_for("System Manager")
	dry_run = _is_dry_run(dry_run)
	people = collect_people(WooCommerceClient.from_settings(SITE))
	out = {"dry_run": dry_run, "emails": len(people), "existing": 0, "invalid": 0, "created": 0,
		"phone_dropped": 0, "errors": [], "sample": []}
	for email in sorted(people):
		person = people[email]
		if not validate_email_address(email):
			out["invalid"] += 1
			continue
		if _known(email):
			out["existing"] += 1
			continue
		name = person["name"] or email
		if len(out["sample"]) < SAMPLE:
			out["sample"].append({"email": email, "name": name})
		if dry_run:
			out["created"] += 1
			continue
		try:
			out["phone_dropped"] += _create(name, email, person["phone"])
			out["created"] += 1
		except Exception as exc:
			out["errors"].append({"email": email, "error": str(exc)[:200]})
	return out
