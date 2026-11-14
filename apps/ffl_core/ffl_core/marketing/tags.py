"""Marketing Contact tags: Frappe's own tags (``_user_tags`` + ``Tag Link``), so the Desk list filters
and the form sidebar work as on any other record. A tag's name is its slug, kept exactly as given.

These functions write the same rows ``frappe.desk.doctype.tag.tag.DocTags`` does, without its
per-record write-permission check: they run for the website's account and for imports, which have no
write right on Marketing Contact. Audiences are read from ``Tag Link`` (exact match), never from the
comma-joined ``_user_tags`` column (a LIKE would match ``gun`` inside ``gunsmith``).

The Marketing Contact table's ``_user_tags`` column is ``text`` (``marketing.setup.ensure``): the one
Frappe adds on first use is 140 characters, which a contact with a dozen tags outgrows.
"""
from __future__ import annotations

import re

import frappe

from ffl_core.marketing import CONTACT

# A slug: what public input may name, and what an import keeps.
SLUG = re.compile(r"^[a-z0-9-]{1,60}$")

# The tags the code itself reads or writes.
NEW_ARRIVALS = "new-arrivals"    # the new-arrivals digest's audience
BACK_IN_STOCK = "back-in-stock"  # asked to be told when an out-of-stock product returns
IN_STORE = "in-store"            # a store customer
INTEREST = "interest-"           # + an interest's slug


def is_slug(value) -> bool:
	return isinstance(value, str) and bool(SLUG.match(value))


def clean(values) -> list[str]:
	"""The slugs among *values* (a list from public input), in order, each once; anything else dropped."""
	out: list[str] = []
	for value in values if isinstance(values, (list, tuple)) else ():
		if is_slug(value) and value not in out:
			out.append(value)
	return out


def interest(value) -> str | None:
	"""``interest-<value>`` for an interest's slug, or None when it does not make a valid tag."""
	tag = INTEREST + value if isinstance(value, str) else ""
	return tag if is_slug(value) and is_slug(tag) else None


def of(contact: str) -> list[str]:
	"""*contact*'s tags, in the order they were added."""
	raw = frappe.db.get_value(CONTACT, contact, "_user_tags") or ""
	return [t for t in raw.split(",") if t]


def add(contact: str, *tags: str) -> list[str]:
	"""Tag *contact*; the tags that were new to it. Creates a missing Tag master."""
	current = (frappe.db.get_value(CONTACT, contact, "_user_tags", for_update=True) or "").split(",")
	new = [t for t in dict.fromkeys(tags) if t and t not in current]
	if not new:
		return []
	for tag in new:
		_ensure_master(tag)
		link = frappe.get_doc({"doctype": "Tag Link", "document_type": CONTACT, "document_name": contact,
			"title": contact, "tag": tag})
		link.flags.ignore_links = True   # both ends exist; and a Tag of another case must not recase the slug
		link.insert(ignore_permissions=True)
	_write(contact, [t for t in current if t] + new)
	return new


def remove(contact: str, *tags: str) -> list[str]:
	"""Untag *contact*; the tags it had."""
	current = [t for t in (frappe.db.get_value(CONTACT, contact, "_user_tags", for_update=True) or "").split(",") if t]
	gone = [t for t in current if t in tags]
	if gone:
		frappe.db.delete("Tag Link", {"document_type": CONTACT, "document_name": contact, "tag": ["in", gone]})
		_write(contact, [t for t in current if t not in gone])
	return gone


def contacts_with(tag: str, *, statuses: tuple[str, ...] | list[str] | None = None) -> list[str]:
	"""The emails of the contacts tagged *tag* (optionally only those in *statuses*), in email order."""
	link, contact = frappe.qb.DocType("Tag Link"), frappe.qb.DocType(CONTACT)
	query = (frappe.qb.from_(link).join(contact).on(contact.name == link.document_name)
		.select(contact.name).distinct()
		.where((link.document_type == CONTACT) & (link.tag == tag))
		.orderby(contact.name))
	if statuses:
		query = query.where(contact.status.isin(list(statuses)))
	return query.run(pluck=True)


def _write(contact: str, tags: list[str]) -> None:
	frappe.db.set_value(CONTACT, contact, "_user_tags", ",".join(tags), update_modified=False)


def _ensure_master(tag: str) -> None:
	if frappe.db.exists("Tag", tag):
		return
	frappe.db.savepoint("mkt_tag_master")
	try:
		frappe.get_doc({"doctype": "Tag", "name": tag}).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:   # made by a concurrent request since the check
		frappe.db.rollback(save_point="mkt_tag_master")
		frappe.clear_last_message()
