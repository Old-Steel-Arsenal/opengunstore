"""Bring the store's people into the POS as Customers: everyone who bought online and every
registered store account, one Customer per email address.

Web orders have been booked to their real buyer since July 2026 (``revenue._resolve_web_customer``);
older ones went to the shared web customer, and store accounts that never ordered were never
seen. This fills both in, once, with the same lookup and the same new-buyer defaults the order
sync uses (``customer_profile.find_customer_by_email`` / ``new_retail_customer``): an address
that already belongs to a Customer (or is a merged customer's alias) is left alone, so nothing
is duplicated and no existing record is changed. Invoices are not touched.

Run with ``dry_run=1`` first; a store-sized run is best done with ``bench execute``.
"""
from __future__ import annotations

import frappe
from frappe.utils import cint, validate_email_address

from ffl_core.customer_profile import find_customer_by_email, new_retail_customer
from ffl_woo_sync.woocommerce.client import WooCommerceClient

SITE = "retail"
PER_PAGE = 100
# Orders that mean the person bought: placed and paid (or paid then refunded). Not a
# checkout draft, an unpaid pending or failed attempt, a cancelled unpaid order, or the trash.
BUYER_STATUSES = frozenset({"processing", "completed", "on-hold", "refunded"})
SAMPLE = 25


def _pages(fetch):
	"""Every row of a paged Woo list (``fetch(page)`` returns one page)."""
	page = 1
	while True:
		batch = fetch(page) or []
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

	for o in _pages(lambda p: wc.list_orders(status="any", per_page=PER_PAGE, page=p)):
		if o.get("status") not in BUYER_STATUSES:
			continue
		b = o.get("billing") or {}
		add(b.get("email"), _name(b.get("first_name"), b.get("last_name")), b.get("phone"))
	for c in _pages(lambda p: wc.get("customers", params={"per_page": PER_PAGE, "page": p, "orderby": "id"})):
		b = c.get("billing") or {}
		add(c.get("email") or b.get("email"),
			_name(c.get("first_name"), c.get("last_name")) or _name(b.get("first_name"), b.get("last_name")),
			b.get("phone"))
	return people


@frappe.whitelist(methods=["POST"])
def import_customers(dry_run=1) -> dict:
	"""Make a Customer for every online buyer and registered store account the POS does not have.

	Args:
	    dry_run: 1 (default) reads the store and reports what would be created, creating nothing.

	Returns:
	    dict: {"dry_run", "emails" (distinct addresses found), "existing" (already a Customer),
	    "invalid" (not an email address), "created" (or would be), "errors": [{email, error}],
	    "sample": [{email, name}] of the first new ones}.
	"""
	frappe.only_for("System Manager")
	dry_run = bool(cint(dry_run))
	people = collect_people(WooCommerceClient.from_settings(SITE))
	out = {"dry_run": dry_run, "emails": len(people), "existing": 0, "invalid": 0, "created": 0,
		"errors": [], "sample": []}
	for email in sorted(people):
		person = people[email]
		if not validate_email_address(email):
			out["invalid"] += 1
			continue
		if find_customer_by_email(email):
			out["existing"] += 1
			continue
		name = person["name"] or email
		if len(out["sample"]) < SAMPLE:
			out["sample"].append({"email": email, "name": name})
		if dry_run:
			out["created"] += 1
			continue
		frappe.db.savepoint("woo_customer_import")
		try:
			doc = new_retail_customer(name, email=email, phone=person["phone"])
			doc.flags.ignore_permissions = True   # a System Manager's one-off import
			doc.insert()
			out["created"] += 1
		except Exception as exc:
			frappe.db.rollback(save_point="woo_customer_import")
			out["errors"].append({"email": email, "error": str(exc)[:200]})
	return out
