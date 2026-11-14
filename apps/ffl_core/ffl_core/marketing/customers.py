"""Store customers on the marketing list, and which Customer a marketing contact is.

* **Intake** (Marketing Settings › Subscribe store customers): when a Customer, or the primary Contact
  of one, is saved with a usable address, :func:`add_customer` makes sure the address has a Marketing
  Contact. A new one is ``Subscribed`` (channel ``pos``, source ``pos``) and tagged ``in-store`` and
  ``new-arrivals``. One that already exists gets only the ``in-store`` tag and its customer link: its
  status is never touched, a suppressed one least of all. Off by default; :func:`backfill_customers`
  does the same once for the customers already on file.
* **Link** (the contact's ``customer``): :func:`resolve_customer` is Customer.email_id and merge aliases
  (``customer_profile.find_customer_by_email``), then the Contacts that hold the address
  (``crm.customer``); an address that belongs to more than one customer is linked to none.
  :func:`relink` recomputes it when a contact is made, a Customer or Contact is saved, and a customer
  is merged or renamed.
* **A new address** for a customer takes over a suppression from the old one
  (``contacts.carry_suppression``): an unsubscribe follows the person.
* :func:`card` is what the Deal page's customer card shows.

The Customer / Contact hooks never fail the save they ride on: a problem goes to the Error Log and
the save goes through (:func:`_guarded`).
"""
from __future__ import annotations

import frappe

from ffl_core.mailing_address import clean_email, shop_host, split_name
from ffl_core.marketing import CONTACT, SETTINGS, enabled, tags
from ffl_core.marketing.contacts import SUBSCRIBED, carry_suppression, event_detail, get_or_create, normalize_email, set_status
from ffl_core.utils import commit, single_flag

SOURCE = "pos"
CHANNEL = "pos"
CREATED, EXISTING, SKIPPED = "created", "existing", "skipped"
SAVEPOINT = "mkt_customer_hook"
RELINK_SAVEPOINT = "mkt_relink"
BACKFILL_SAVEPOINT = "mkt_backfill"
COMMIT_EVERY = 200   # customers per commit in a backfill
ERROR_CAP = 20       # failures a backfill report lists
CARD_TAG_CAP = 8     # tags the customer card lists


def auto_subscribe() -> bool:
	"""The module is on and Marketing Settings › Subscribe store customers is ticked (off by default)."""
	return enabled() and single_flag(SETTINGS, "auto_subscribe_customers")


def usable_email(raw, host: str | None = None) -> str:
	"""The address a store customer is listed under, or "" when there is none to use: blank, not an
	address, or at the shop's own domain (staff and test mail, not shoppers)."""
	return normalize_email(clean_email(raw, shop_host() if host is None else host))


def _names(customer_type: str | None, customer_name: str | None, contact: str | None) -> tuple[str, str]:
	"""First and last name for a customer's list entry: the primary Contact's, and for a person the
	customer's own name split when the Contact has none. A business's name is not split."""
	first, last = (frappe.db.get_value("Contact", contact, ["first_name", "last_name"]) or (None, None)) if contact else (None, None)
	if customer_type == "Company":
		return (first or "").strip(), (last or "").strip()
	return split_name(first, last, customer_name)


# ----------------------------------------------------------------- intake


def add_customer(email, *, customer: str | None = None, first_name: str = "", last_name: str = "", host: str | None = None) -> str:
	"""Make sure a store customer's address is on the marketing list; ``created``, ``existing`` or
	``skipped`` (no usable address). Does not check the switch: the hooks and the backfill do."""
	address = usable_email(email, host)
	if not address:
		return SKIPPED
	if frappe.db.exists(CONTACT, address):
		outcome = EXISTING
	else:
		get_or_create(address, first_name=first_name, last_name=last_name, source=SOURCE)
		set_status(address, SUBSCRIBED, channel=CHANNEL, detail=event_detail(source=SOURCE, customer=customer))
		tags.add(address, tags.NEW_ARRIVALS)
		outcome = CREATED
	tags.add(address, tags.IN_STORE)
	relink(address)
	return outcome


def customer_saved(doc, method=None) -> None:
	"""Customer on_update: a changed address takes the old one's suppression; a customer with an
	address is listed (when the switch is on); the contact's customer link follows."""
	_guarded("Customer saved", _customer_saved, doc)


def _customer_saved(doc) -> None:
	if not enabled():
		return
	before = doc.get_doc_before_save()
	email = doc.get("email_id") or doc.get("ffl_email")
	_changed_address(before and before.get("email_id"), email)
	if email and not doc.get("disabled") and auto_subscribe():
		first, last = _names(doc.get("customer_type"), doc.get("customer_name"), doc.get("customer_primary_contact"))
		add_customer(email, customer=doc.name, first_name=first, last_name=last)
	for address in (before and before.get("email_id"), email):
		relink(address)


def contact_saved(doc, method=None) -> None:
	"""Contact on_update: the same for the customers this is the primary Contact of, and every address
	on it is linked again."""
	_guarded("Contact saved", _contact_saved, doc)


def _contact_saved(doc) -> None:
	if not enabled():
		return
	before = doc.get_doc_before_save()
	old = before and before.get("email_id")
	_changed_address(old, doc.get("email_id"))
	listed = auto_subscribe() and doc.get("email_id")
	if listed:
		for customer in frappe.get_all("Customer", filters={"customer_primary_contact": doc.name, "disabled": 0},
				fields=["name", "customer_name", "customer_type"]):
			first, last = _names(customer.customer_type, customer.customer_name, doc.name)
			add_customer(doc.email_id, customer=customer.name, first_name=first, last_name=last)
	rows = [*(doc.get("email_ids") or []), *((before and before.get("email_ids")) or [])]
	for address in {r.email_id for r in rows} | {doc.get("email_id"), old}:
		relink(address)


def _changed_address(old, new) -> None:
	"""A customer's address changed: a suppression on the old one goes to the new one."""
	old, new = normalize_email(old), normalize_email(new)
	if old and new and old != new:
		carry_suppression(old, new)


def customer_renamed(doc, method=None, old: str | None = None, new: str | None = None, merge: bool = False) -> None:
	"""Customer after_rename (a merge ends here too): ERPNext has moved the contacts' links to the
	surviving customer (the merged-away one is deleted right after this); every address that customer
	answers to is linked again."""
	_guarded("Customer renamed", relink_customer, doc.name, old if merge else None)


def customer_trashed(doc, method=None) -> None:
	"""Customer on_trash: a deleted customer leaves its contacts unlinked (a link would refuse the
	delete)."""
	_guarded("Customer deleted", lambda: frappe.db.set_value(CONTACT, {"customer": doc.name}, "customer", None, update_modified=False))


def _guarded(what: str, fn, *args) -> None:
	"""Run a hook's work so a failure costs the marketing list, never the save that triggered it: undone
	back to a savepoint, then one Error Log entry (the traceback, which carries no customer data
	beyond an address)."""
	frappe.db.savepoint(SAVEPOINT)
	try:
		fn(*args)
	except Exception:
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.log_error(title=f"Marketing: {what} failed", message=frappe.get_traceback())
	else:
		frappe.db.release_savepoint(SAVEPOINT)


# ----------------------------------------------------------------- link


def resolve_customer(email, exclude: str | None = None) -> str | None:
	"""The one Customer *email* belongs to, or None for none or for several. *exclude* is a customer
	about to be deleted (the source of a merge), which owns nothing any more."""
	from ffl_core.crm.customer import _customers_by_contact_email
	from ffl_core.customer_profile import ALIAS_EMAIL, _alias_owner, find_customer_by_email

	address = normalize_email(email)
	if not address or frappe.db.count("Customer", {"email_id": address, "name": ["!=", exclude or ""]}) > 1:
		return None
	hit = find_customer_by_email(address)
	owner = hit["name"] if hit else None
	if owner and owner == exclude:
		owner = _alias_owner(ALIAS_EMAIL, address)   # the survivor took the address as an alias
	if owner and owner != exclude:
		return owner
	found = [c for c in _customers_by_contact_email(address) if c != exclude]
	return found[0] if len(found) == 1 else None


def relink(email, exclude: str | None = None) -> bool:
	"""Set the Marketing Contact's customer link from :func:`resolve_customer` (cleared when there is
	none); True when it changed. No contact for *email*: nothing."""
	address = normalize_email(email)
	if not address or not frappe.db.exists(CONTACT, address):
		return False
	current = frappe.db.get_value(CONTACT, address, "customer")
	customer = resolve_customer(address, exclude)
	if (customer or None) == (current or None):
		return False
	frappe.db.set_value(CONTACT, address, "customer", customer, update_modified=False)
	return True


def relink_safely(email) -> None:
	""":func:`relink` for a contact just made: a problem is logged and the contact stands."""
	frappe.db.savepoint(RELINK_SAVEPOINT)
	try:
		relink(email)
	except Exception:
		frappe.db.rollback(save_point=RELINK_SAVEPOINT)
		frappe.log_error(title="Marketing: linking a contact to its customer failed", message=frappe.get_traceback())
	else:
		frappe.db.release_savepoint(RELINK_SAVEPOINT)


def relink_customer(customer: str, exclude: str | None = None) -> int:
	"""Link again every contact this customer owns or answers to: the ones linked to it, its address,
	its merge aliases and the addresses on its Contacts. How many changed."""
	from ffl_core.customer_profile import ALIAS_EMAIL, _contact_emails

	row = frappe.db.get_value("Customer", customer, ["email_id"], as_dict=True)
	addresses = set(frappe.get_all(CONTACT, filters={"customer": customer}, pluck="name"))
	addresses |= set(_contact_emails(customer))
	addresses |= set(frappe.get_all("Customer Alias", filters={"parenttype": "Customer", "parentfield": "customer_aliases",
		"parent": customer, "alias_type": ALIAS_EMAIL}, pluck="value", parent_doctype="Customer"))
	if row and row.email_id:
		addresses.add(row.email_id)
	return sum(relink(address, exclude) for address in addresses)


# ----------------------------------------------------------------- customer card


def card(customer: str) -> dict | None:
	"""``{"status", "tags"}`` of the contact for this customer's own address (else the first contact
	linked to it), or None when the module is off or no contact is linked."""
	if not enabled():
		return None
	rows = frappe.get_all(CONTACT, filters={"customer": customer}, fields=["name", "status"],
		order_by="creation asc, name asc", limit=20)
	if not rows:
		return None
	own = normalize_email(frappe.db.get_value("Customer", customer, "email_id"))
	row = next((r for r in rows if r.name == own), rows[0])
	return {"status": row.status, "tags": tags.of(row.name)}


# ----------------------------------------------------------------- backfill


def backfill_customers(dry_run=1) -> dict:
	"""List the customers already on file (the same as saving each one with the switch on), once.

	Run it dry first (``bench --site <site> execute ffl_core.marketing.customers.backfill_customers``);
	only an explicit ``dry_run=0`` writes, and then only with Marketing Settings › Enabled on.
	Customers that are disabled are left out. Not a whitelisted endpoint.

	Returns:
	    {"dry_run", "customers": looked at, "create": new contacts, "existing": addresses already on
	    the list (a second customer with the same address counts here), "skipped_no_email",
	    "skipped_store_domain", "skipped_invalid", "failed", "errors": the first few failures}.
	"""
	write = dry_run in (0, "0", False)
	if write and not enabled():
		frappe.throw("Turn on Marketing Settings > Enabled before listing customers.")
	host = shop_host()
	report = {"dry_run": not write, "customers": 0, "create": 0, "existing": 0, "skipped_no_email": 0,
		"skipped_store_domain": 0, "skipped_invalid": 0, "failed": 0, "errors": []}
	seen: set[str] = set()
	# lazy: all customers' ids and addresses in memory (a few MB per 100k), one pass; page it if a store passes ~1M
	rows = frappe.get_all("Customer", filters={"disabled": 0}, fields=["name", "email_id", "customer_name", "customer_type",
		"customer_primary_contact"], order_by="name", limit_page_length=0)
	for done, row in enumerate(rows, 1):
		report["customers"] += 1
		raw = (row.email_id or "").strip()
		if not raw:
			report["skipped_no_email"] += 1
		elif not clean_email(raw, host):
			report["skipped_store_domain" if "@" in raw else "skipped_no_email"] += 1
		elif not normalize_email(raw):
			report["skipped_invalid"] += 1
		elif write:
			_backfill_one(row, host, report)
		else:
			address = normalize_email(raw)
			report["existing" if address in seen or frappe.db.exists(CONTACT, address) else "create"] += 1
			seen.add(address)
		if write and done % COMMIT_EVERY == 0:
			commit()
	if write:
		commit()
	return report


def _backfill_one(row, host: str, report: dict) -> None:
	frappe.db.savepoint(BACKFILL_SAVEPOINT)
	try:
		first, last = _names(row.customer_type, row.customer_name, row.customer_primary_contact)
		outcome = add_customer(row.email_id, customer=row.name, first_name=first, last_name=last, host=host)
	except Exception as exc:
		frappe.db.rollback(save_point=BACKFILL_SAVEPOINT)
		report["failed"] += 1
		if len(report["errors"]) < ERROR_CAP:
			report["errors"].append({"customer": row.name, "error": type(exc).__name__})
	else:
		frappe.db.release_savepoint(BACKFILL_SAVEPOINT)
		report[{CREATED: "create", EXISTING: "existing", SKIPPED: "skipped_invalid"}[outcome]] += 1
