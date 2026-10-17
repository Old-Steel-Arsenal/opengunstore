"""Customer profile rules: status, the ID-number switch, family, merge.

The fields come from ``setup/customer_profile.py``; the doc events below are
wired in ``hooks.doc_events["Customer"]``.

* **Status** — a customer without one is Active; Banned needs a reason and a
  manager (``customer_flags.guard_ban_fields``); the counter gate and the
  manager override live in ``customer_flags``.
* **ID numbers and date of birth** — kept only while FFL Settings → Store
  Customer ID Numbers is on. Off, every save blanks ``date_of_birth`` and each
  ``customer_ids[].id_number``. This validate is the rule; the form hiding the
  inputs is a convenience. Every write path — Desk, quick entry, REST, imports,
  the merge below — ends here. Turning the switch from on to off also erases
  what is already stored, in one step (:func:`erase_stored_ids`, called by the
  FFL Settings controller), so nothing waits for the next save of each customer.
* **Family** — one level: ``primary_customer`` names the head of the family; a
  member cannot have members, a head with members cannot join another family,
  and a head with members cannot be deleted.
* **Merge** — :func:`merge_customer` copies the source's alerts and ID rows to
  the target and records the source's emails and GunBroker id on the target as
  ``customer_aliases``, then ERPNext's merge-rename moves every link and deletes
  the source.
* **Aliases** — the Woo and GunBroker order resolvers find a customer by
  ``email_id`` / ``gb_user_id`` first and by an alias second
  (:func:`find_customer_by_email`, :func:`find_customer_by_gb_id`). Only a merge
  writes aliases (``validate`` refuses any other edit unless the user is a System
  Manager), so a customer nobody merged matches exactly as before.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model import no_value_fields
from frappe.utils import escape_html, get_datetime, now_datetime

from ffl_core.compliance.ffl_number import normalize as normalize_ffl
from ffl_core.customer_name import refresh_customer_name_snapshots
from ffl_core.feature_gate import customer_id_numbers_enabled
from ffl_core.roles import CUSTOMER_MANAGER_ROLES
from ffl_core.utils import set_file_privacy

ACTIVE = "Active"
BANNED = "Banned"
ALIAS_EMAIL = "Email"
ALIAS_GB_ID = "GunBroker ID"
# The Woo aggregate customer (ffl_woo_sync books email-less web orders to it);
# ffl_woo_sync.woocommerce.revenue.WEB_CUSTOMER is this constant.
WEB_STORE_CUSTOMER = "Web Store Customer"


# ---------------------------------------------------------------- doc events


def validate_customer(doc, method=None) -> None:
	"""Customer.validate: status, the ID-number switch, alert stamps, family."""
	if not doc.get("customer_status"):
		doc.customer_status = ACTIVE
	if doc.customer_status == BANNED and not (doc.get("status_reason") or "").strip():
		frappe.throw(_("Enter a reason before marking {0} as Banned.").format(
			frappe.bold(escape_html(doc.customer_name or doc.name))), title=_("Reason required"))
	from ffl_core.customer_flags import guard_ban_fields
	guard_ban_fields(doc)
	_refuse_alias_edits(doc)
	blank_id_numbers_unless_enabled(doc)
	_stamp_alerts(doc)
	_validate_family(doc)


def _alias_rows(doc) -> list[tuple]:
	return [(r.get("alias_type"), r.get("value"), r.get("merged_from"),
		get_datetime(r.get("merged_on")) if r.get("merged_on") else None)
		for r in doc.get("customer_aliases") or []]


def _refuse_alias_edits(doc) -> None:
	"""Aliases are written only by :func:`merge_customer` (or a System Manager).

	An alias decides which customer a Woo / GunBroker order lands on, so a clerk
	who can edit a Customer must not be able to point someone else's email or
	GunBroker id at a record. The merge sets ``flags.writing_aliases`` on the
	target it saves; a System Manager may also edit them (a future data import).
	"""
	if doc.flags.get("writing_aliases") or "System Manager" in frappe.get_roles():
		return
	before = doc.get_doc_before_save() if not doc.is_new() else None
	if _alias_rows(doc) != (_alias_rows(before) if before else []):
		frappe.throw(_("Merged emails and GunBroker IDs are recorded by Merge customers only "
			"and cannot be edited."), title=_("Read-only"))


def blank_id_numbers_unless_enabled(doc) -> None:
	"""Clear the date of birth and every ID number unless the store keeps them."""
	if customer_id_numbers_enabled():
		return
	doc.date_of_birth = None
	for row in doc.get("customer_ids") or []:
		row.id_number = None


def stored_id_counts() -> dict:
	"""How many dates of birth and ID numbers are stored across all customers (a gunsmith
	work order's copy of the owner's ID number counts as one)."""
	return {
		"dates_of_birth": frappe.db.count("Customer", {"date_of_birth": ["is", "set"]}),
		"id_numbers": frappe.db.count("Customer ID Document",
			{"parenttype": "Customer", "id_number": ["is", "set"]})
			+ frappe.db.count("Gunsmith Work Order", {"id_number": ["is", "set"]}),
	}


def erase_stored_ids() -> dict:
	"""Blank every stored ``Customer.date_of_birth`` and ``Customer ID Document.id_number``.

	One server-side step for the moment FFL Settings → Store Customer ID Numbers
	goes from on to off (``FFLSettings.on_update``). Raw UPDATEs on purpose, like
	the status backfill in ``setup/customer_profile``: no ``modified`` bump and no
	validate on thousands of untouched customers; the cached copies of the
	affected customers are dropped so the next read does not serve the old value.
	Id type, issuer, dates and attached ID copies stay, and so does Version history.

	Returns:
	    {"dates_of_birth": int, "id_numbers": int} — the counts erased.
	"""
	counts = stored_id_counts()
	frappe.db.sql("UPDATE `tabCustomer` SET date_of_birth = NULL WHERE date_of_birth IS NOT NULL")
	frappe.db.sql("UPDATE `tabCustomer ID Document` SET id_number = NULL "
		"WHERE parenttype = 'Customer' AND id_number IS NOT NULL AND id_number != ''")
	# The bound book's own record (the acquisition) keeps the ID it needs; the work order's copy goes.
	frappe.db.sql("UPDATE `tabGunsmith Work Order` SET id_number = NULL WHERE id_number IS NOT NULL AND id_number != ''")
	frappe.clear_cache(doctype="Customer")
	return counts


@frappe.whitelist()
def get_stored_id_counts() -> dict:
	"""What turning off Store Customer ID Numbers would erase, for the FFL Settings confirm.

	Returns:
	    {"dates_of_birth": int, "id_numbers": int, "total": int} across all customers.
	"""
	frappe.has_permission("FFL Settings", "write", throw=True)
	counts = stored_id_counts()
	return {**counts, "total": counts["dates_of_birth"] + counts["id_numbers"]}


def privatize_id_attachments(doc, method=None) -> None:
	"""Customer.on_update: an ID document's attachment is always a private file.

	on_update, not validate: a file uploaded on a not-yet-saved form is attached
	to the form's temporary name until insert relinks it (frappe
	``relink_mismatched_files``, which runs before on_update).
	``verify_attached_field`` because the File row names the CHILD field
	(``attachment``), which Customer itself does not have — see
	``utils.set_file_privacy``.

	Only a file attached to THIS customer is accepted (an already-private
	``/private/...`` URL too: another customer's ID image cannot be pointed at).
	Anything else is refused and
	left as it is: a library file attached elsewhere (a product photo on an Item)
	would come off the website, an unattached public file (a store logo in the file
	manager) was never an upload of this ID, and a URL with no File row (an
	external link, a hand-typed ``/files/...`` path) cannot be made private at
	all. The attachment must never be left public, so each case throws the same
	plain message.
	"""
	for row in doc.get("customer_ids") or []:
		url = (row.get("attachment") or "").strip()
		if not url:
			continue
		private = url.startswith("/private/")
		owners = frappe.get_all("File", filters={"file_url": url},
			fields=["attached_to_doctype", "attached_to_name"])
		# Every File row sharing the URL is flipped together (same bytes), so all of
		# them must be this customer's.
		if not owners or any((f.attached_to_doctype, f.attached_to_name)
				!= ("Customer", doc.name) for f in owners):
			_refuse_id_attachment(url)
		if private:  # already private: the ownership check above is all it needs
			continue
		try:
			private_url = set_file_privacy(url, private=True, verify_attached_field=True)
		except Exception:
			frappe.log_error(title="customer_profile: ID attachment not made private",
				message=frappe.get_traceback())
			private_url = None
		if not private_url:
			_refuse_id_attachment(url)
		if private_url != url:
			row.db_set("attachment", private_url, update_modified=False)


def _refuse_id_attachment(url: str) -> None:
	frappe.throw(_("The ID attachment {0} cannot be made private. Remove it and upload the "
		"ID image with the Attach button.").format(escape_html(url)), title=_("ID attachment"))


def refuse_delete_with_family(doc, method=None) -> None:
	"""Customer.on_trash: a head of family with members cannot be deleted."""
	members = frappe.get_all("Customer", filters={"primary_customer": doc.name},
		pluck="name", limit=4)
	if members:
		shown = ", ".join(members[:3]) + (", …" if len(members) > 3 else "")
		frappe.throw(_("{0} has family members ({1}). Move them to another family or "
			"clear their Family Of first.").format(escape_html(doc.name), escape_html(shown)),
			title=_("Family members"))


def has_family_members(customer: str) -> bool:
	return bool(frappe.db.exists("Customer", {"primary_customer": customer}))


def find_customer_by_email(email: str | None) -> dict | None:
	"""The customer an order's email belongs to, or None.

	The ONE email lookup for the Woo and GunBroker order resolvers. Returns
	``{"name", "is_ffl_dealer"}``, plus ``via_alias = 1`` when the hit came from a
	merge alias. ``Customer.email_id`` first (case-insensitive, column collation),
	then a Customer Alias of type Email (written by :func:`merge_customer` for a
	merged-away customer's addresses). Contact emails take no part. An address
	that is an alias of more than one customer matches nobody (Error Log).
	"""
	email = (email or "").strip()
	if not email:
		return None
	hit = frappe.db.get_value("Customer", {"email_id": email}, ["name", "is_ffl_dealer"], as_dict=True)
	if hit:
		return hit
	owner = _alias_owner(ALIAS_EMAIL, email)
	if not owner:
		return None
	row = frappe.db.get_value("Customer", owner, ["name", "is_ffl_dealer"], as_dict=True)
	if row:
		row.via_alias = 1
	return row


def find_customer_by_gb_id(gb_user_id: str | None) -> str | None:
	"""The customer a merged-away GunBroker user id now belongs to (name), or None.

	The alias half of the GunBroker lookup; ``Customer.gb_user_id`` is the
	resolver's own first step. Ambiguous (several customers) matches nobody.
	"""
	return _alias_owner(ALIAS_GB_ID, (gb_user_id or "").strip())


def _alias_owner(alias_type: str, value: str) -> str | None:
	"""The one customer holding this alias; None for none or for several (logged)."""
	if not value:
		return None
	owners = frappe.get_all("Customer Alias", filters={"parenttype": "Customer",
		"parentfield": "customer_aliases", "alias_type": alias_type, "value": value},
		pluck="parent", distinct=True, parent_doctype="Customer", limit=3)
	if len(owners) > 1:
		frappe.log_error(title=f"Ambiguous customer alias ({alias_type})",
			message=f"{alias_type} {value!r} is an alias of {', '.join(owners)}. No customer "
				"was matched; fix the aliases on those customers.")
		return None
	return owners[0] if owners else None


def _stamp_alerts(doc) -> None:
	for row in doc.get("customer_alerts") or []:
		if not row.get("created_by"):
			row.created_by = frappe.session.user
		if not row.get("created_on"):
			row.created_on = now_datetime()


def _validate_family(doc) -> None:
	primary = (doc.get("primary_customer") or "").strip()
	if not primary:
		if doc.get("relationship"):
			doc.relationship = None
		return
	who = escape_html(doc.customer_name or doc.name)
	if primary == doc.name:
		frappe.throw(_("A customer cannot be in their own family."))
	# lazy: plain reads, so two clerks linking families in the same instant
	# (A→B while C→A) can both pass; locking here would need to be the first read
	# of the transaction (MariaDB snapshot isolation answers a later FOR UPDATE on
	# a changed row with error 1020), so a periodic "nested family" report is the
	# upgrade path if this ever happens.
	head = frappe.db.get_value("Customer", primary, "primary_customer")
	if head:
		frappe.throw(_("{0} is already a family member of {1}. Add {2} to {1}'s family "
			"instead.").format(escape_html(primary), escape_html(head), who),
			title=_("One family level"))
	if not doc.is_new() and has_family_members(doc.name):
		frappe.throw(_("{0} has family members of their own, so they cannot join another "
			"family.").format(who), title=_("One family level"))


# ------------------------------------------------------------------ endpoints


@frappe.whitelist()
def get_family(customer: str) -> dict:
	"""The family a customer belongs to, for the Customer form's Family section.

	Args:
	    customer: Customer name (document id).

	Returns:
	    {"primary": {"name", "customer_name"} or None, "relationship": the
	    customer's relationship to that primary or None, "members": [{"name",
	    "customer_name", "relationship"}] — empty for a member, "can_merge": whether
	    the caller may merge customers (drives the form's Merge button)}.
	"""
	frappe.has_permission("Customer", "read", doc=customer, throw=True)
	own = frappe.db.get_value("Customer", customer, ["primary_customer", "relationship"],
		as_dict=True) or {}
	primary = None
	if own.get("primary_customer"):
		primary = {"name": own.primary_customer, "customer_name": frappe.db.get_value(
			"Customer", own.primary_customer, "customer_name")}
	members = frappe.get_all("Customer", filters={"primary_customer": customer},
		fields=["name", "customer_name", "relationship"], order_by="customer_name asc")
	return {"primary": primary, "relationship": own.get("relationship") if primary else None,
		"members": members, "can_merge": bool(set(frappe.get_roles()) & set(CUSTOMER_MANAGER_ROLES))}


@frappe.whitelist()
def merge_customer(source: str, target: str) -> dict:
	"""Merge a duplicate customer into the one to keep; the duplicate is deleted.

	The source's alerts and ID documents are copied onto the target (identical
	rows skipped), a date of birth the target lacks is filled, a Banned source
	makes the target Banned, and the source's own family link is carried when
	the target has none. The source's email, the emails on its Contacts, its
	GunBroker user id and its own aliases are recorded on the target as aliases
	(values the target already has are skipped), so a later Woo / GunBroker order
	carrying one of them lands on the target instead of starting a duplicate.
	Then ERPNext's merge-rename moves every link —
	invoices, orders, 4473s, payments, contacts, addresses, family members — to
	the target and deletes the source; open sales documents get the target's
	display name. Refused for the Woo web-store aggregate and the walk-in customer,
	when both are FFL dealers with different licence numbers, when only the source
	is a dealer, when either is a register's default customer, or when the merge
	would nest one family inside another.

	Args:
	    source: Customer to merge away (deleted).
	    target: Customer to keep.

	Returns:
	    {"name": target, "customer_name": target's display name,
	    "alerts_copied": int, "ids_copied": int}.
	"""
	frappe.only_for(CUSTOMER_MANAGER_ROLES)
	source = (source or "").strip()
	target = (target or "").strip()
	if not source or not target:
		frappe.throw(_("Pick the customer to merge and the customer to keep."))
	if source == target:
		frappe.throw(_("A customer cannot be merged into itself."))
	src = frappe.get_doc("Customer", source)
	tgt = frappe.get_doc("Customer", target)
	_refuse_merge(src, tgt)

	kept_name = tgt.customer_name
	alerts = _copy_rows(src, tgt, "customer_alerts")
	ids = _copy_rows(src, tgt, "customer_ids")
	notes = _carry_profile(src, tgt)
	aliases = _carry_aliases(src, tgt)
	tgt.flags.ignore_permissions = True
	tgt.flags.writing_aliases = True   # lets validate_customer accept the new alias rows
	tgt.save()

	# Model-level rename for ignore_permissions (same reason as
	# manual_order.rename_customer: Sales Manager has no Customer write).
	# rebuild_search off: a merge renames nothing that is still indexed — the
	# source's global-search row goes with delete_doc.
	from frappe.model.rename_doc import rename_doc
	rename_doc("Customer", source, target, merge=True, ignore_permissions=True,
		show_alert=False, rebuild_search=False)
	# ERPNext's Customer.after_rename sets customer_name to the docname when
	# naming is by Customer Name; a "John Smith-1" target would be renamed.
	if frappe.db.get_value("Customer", target, "customer_name") != kept_name:
		frappe.db.set_value("Customer", target, "customer_name", kept_name)
	refresh_customer_name_snapshots(target, kept_name)

	summary = _("Merged customer {0} into this record by {1}: {2} alert(s) and {3} ID "
		"document(s) copied, {4} email / GunBroker id alias(es) recorded; invoices, orders, "
		"4473s and family members moved.").format(
		escape_html(source), escape_html(frappe.session.user), alerts, ids, aliases)
	frappe.get_doc("Customer", target).add_comment(
		"Comment", " ".join([summary, *notes]))
	return {"name": target, "customer_name": kept_name, "alerts_copied": alerts,
		"ids_copied": ids}


def _system_customer_names() -> set[str]:
	"""The web-store aggregate and the walk-in customer under the name this store uses.

	The walk-in name is the store's WALK_IN_CUSTOMER setting (store.env, read the
	way ``store_setup`` reads it), the default only when the store sets none;
	A process started without store.env falls back to the default name; the
	register's default customer (``POS Profile.customer``, which store setup points
	at the walk-in customer) is refused separately in :func:`_refuse_merge`.
	"""
	from ffl_core.setup.store_setup import OPTIONAL, config_from_env
	walk_in = config_from_env().get("WALK_IN_CUSTOMER") or OPTIONAL["WALK_IN_CUSTOMER"]
	return {WEB_STORE_CUSTOMER, walk_in}


def _refuse_merge(src, tgt) -> None:
	s, t = escape_html(src.name), escape_html(tgt.name)
	system = _system_customer_names()
	for doc in (src, tgt):
		if doc.name in system or doc.get("customer_name") in system:
			frappe.throw(_("{0} is a system customer (the web-store aggregate or the walk-in "
				"customer) and cannot be merged.").format(escape_html(doc.name)),
				title=_("System customer"))
	if src.get("is_ffl_dealer") and tgt.get("is_ffl_dealer") and \
			normalize_ffl(src.get("ffl_number")) != normalize_ffl(tgt.get("ffl_number")):
		frappe.throw(_("{0} and {1} are different FFL dealers ({2} / {3}) and cannot be "
			"merged.").format(s, t, escape_html(src.get("ffl_number") or "—"),
			escape_html(tgt.get("ffl_number") or "—")), title=_("Different dealers"))
	if src.get("is_ffl_dealer") and not tgt.get("is_ffl_dealer"):
		frappe.throw(_("{0} is an FFL dealer and {1} is not. Merge {1} into {0} instead, "
			"so the licence stays on file.").format(s, t), title=_("Dealer record"))
	for name in (src.name, tgt.name):
		if frappe.db.exists("POS Profile", {"customer": name}):
			frappe.throw(_("{0} is a register's default customer and cannot be merged.")
				.format(escape_html(name)), title=_("Register customer"))
	if src.get("primary_customer") and has_family_members(src.name):
		# Only a race can make this state (validate forbids it); carrying either side
		# would nest families, so a person has to untangle it first.
		frappe.throw(_("{0} belongs to the family of {1} and also has family members of "
			"their own. Fix the families first.").format(s, escape_html(src.get("primary_customer"))),
			title=_("One family level"))
	if tgt.get("primary_customer") == src.name:
		frappe.throw(_("{1} is a family member of {0}. Clear {1}'s Family Of first.")
			.format(s, t), title=_("One family level"))
	if tgt.get("primary_customer") and has_family_members(src.name):
		frappe.throw(_("{0} has family members and {1} belongs to another family: families "
			"are one level deep.").format(s, t), title=_("One family level"))


def _row_values(row) -> dict:
	return {df.fieldname: row.get(df.fieldname) for df in row.meta.fields
		if df.fieldtype not in no_value_fields}


def _copy_rows(src, tgt, table: str) -> int:
	"""Append the source's rows of *table* to the target, skipping exact copies."""
	seen = [_row_values(r) for r in tgt.get(table) or []]
	copied = 0
	for row in src.get(table) or []:
		values = _row_values(row)
		if values in seen:
			continue
		tgt.append(table, values)
		seen.append(values)
		copied += 1
	return copied


def _contact_emails(customer: str) -> list[str]:
	"""Every email on a Contact linked to *customer*."""
	contacts = frappe.get_all("Dynamic Link", filters={"parenttype": "Contact",
		"link_doctype": "Customer", "link_name": customer}, pluck="parent")
	if not contacts:
		return []
	return frappe.get_all("Contact Email", filters={"parenttype": "Contact",
		"parent": ("in", contacts)}, pluck="email_id")


def _carry_aliases(src, tgt) -> int:
	"""Append the source's email / GunBroker id keys to the target's aliases.

	Skips what the target already answers to (its own email_id / gb_user_id or an
	alias) and repeats within the batch; emails are stored lower-case. The
	source's own aliases keep their original merged_from / merged_on.
	"""
	def key(kind, value):
		return kind, (value or "").strip().lower()

	have = {key(ALIAS_EMAIL, tgt.get("email_id")), key(ALIAS_GB_ID, tgt.get("gb_user_id"))}
	have |= {key(r.alias_type, r.value) for r in tgt.get("customer_aliases") or []}
	now = now_datetime()
	wanted = [(ALIAS_EMAIL, e, src.name, now) for e in [src.get("email_id"), *_contact_emails(src.name)]]
	wanted.append((ALIAS_GB_ID, src.get("gb_user_id"), src.name, now))
	wanted += [(r.alias_type, r.value, r.merged_from, r.merged_on) for r in src.get("customer_aliases") or []]
	added = 0
	for kind, value, origin, when in wanted:
		value = (value or "").strip()
		if kind == ALIAS_EMAIL:
			value = value.lower()
		if not value or key(kind, value) in have:
			continue
		have.add(key(kind, value))
		tgt.append("customer_aliases", {"alias_type": kind, "value": value,
			"merged_from": origin, "merged_on": when})
		added += 1
	return added


def _carry_profile(src, tgt) -> list[str]:
	"""Fill what the target lacks from the source; return timeline notes."""
	notes = []
	if src.get("date_of_birth") and not tgt.get("date_of_birth"):
		tgt.date_of_birth = src.date_of_birth
	if src.get("customer_status") == BANNED and tgt.get("customer_status") != BANNED:
		tgt.customer_status = BANNED
		tgt.status_reason = _("Merged from {0}: {1}").format(
			src.name, src.get("status_reason") or "")
		notes.append(_("Banned status carried over from {0}.").format(escape_html(src.name)))
	head = src.get("primary_customer")
	if head and head != tgt.name and not tgt.get("primary_customer"):
		if has_family_members(tgt.name):
			notes.append(_("{0} was a family member of {1}; not carried over because this "
				"customer has family members of their own.").format(
				escape_html(src.name), escape_html(head)))
		else:
			tgt.primary_customer = head
			tgt.relationship = src.get("relationship")
	return notes
