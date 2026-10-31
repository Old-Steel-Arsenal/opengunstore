"""Customer profile: status, the ID-number switch, family, merge.

Site tests against the real Customer doctype — the fields come from
``setup/customer_profile.ensure``, which ``make test``'s bootstrap runs with the
other after_migrate ensures. Nothing on the paths under test is mocked: the
switch is moved through ``SettingsSandbox`` (raw tabSingles, restored), the
merge runs frappe's real merge-rename.
"""
from __future__ import annotations

import ast
import json
import os

import random

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core import customer_profile
from ffl_core.setup import agreements as agreements_setup
from ffl_core.setup import customer_profile as profile_setup
from ffl_core.tests.settings_sandbox import SettingsSandbox

_SWITCH = ("FFL Settings", "store_customer_id_numbers")
_OUR_FIELDS = [f["fieldname"] for f in profile_setup.FIELDS]


def _uniq(prefix: str) -> str:
	return f"_Test {prefix} {frappe.generate_hash(length=6)}"


def _customer(name: str | None = None, **fields):
	doc = frappe.get_doc({"doctype": "Customer", "customer_name": name or _uniq("Profile"),
		"customer_type": "Individual", "customer_group": "Individual",
		"territory": "All Territories", **fields})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc


def _contact(customer: str, email: str) -> str:
	"""A Contact carrying *email*, linked to *customer*."""
	contact = frappe.get_doc({"doctype": "Contact", "first_name": _uniq("Contact"),
		"email_ids": [{"email_id": email, "is_primary": 1}],
		"links": [{"link_doctype": "Customer", "link_name": customer}]})
	contact.insert(ignore_permissions=True)
	return contact.name


def _email() -> str:
	return f"t-{frappe.generate_hash(length=8)}@example.com"


def _public_file(attached_to: tuple[str, str] | None = None):
	"""A public File with fresh content, optionally attached to (doctype, name)."""
	doc = {"doctype": "File", "file_name": f"_test-{frappe.generate_hash(length=10)}.png",
		"is_private": 0, "content": frappe.generate_hash(length=32).encode()}
	if attached_to:
		doc.update(attached_to_doctype=attached_to[0], attached_to_name=attached_to[1])
	file_doc = frappe.get_doc(doc)
	file_doc.flags.ignore_permissions = True
	file_doc.insert()
	return file_doc


def _user(email: str, *roles) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0],
			"send_welcome_email": 0})
		u.flags.ignore_permissions = True
		u.insert()
	frappe.db.delete("Has Role", {"parent": email, "parenttype": "User"})
	frappe.get_doc("User", email).add_roles(*roles)
	frappe.clear_cache(user=email)
	return email


def _hooks() -> dict:
	"""Read hooks.py itself (docs/testing.md: frappe.get_hooks can be stubbed)."""
	path = os.path.join(frappe.get_app_path("ffl_core"), "hooks.py")
	with open(path) as f:
		tree = ast.parse(f.read())
	out = {}
	for node in tree.body:
		if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) \
				and node.targets[0].id in ("after_install", "after_migrate", "doc_events"):
			out[node.targets[0].id] = ast.literal_eval(node.value)
	return out


class ProfileSetup(FrappeTestCase):
	"""The fields exist on install AND migrate, existing customers end up
	Active, and ensure() can run again without moving anything."""

	def test_ensure_is_registered_on_install_and_on_migrate(self):
		hooks = _hooks()
		for key in ("after_install", "after_migrate"):
			with self.subTest(hook=key):
				self.assertIn("ffl_core.setup.customer_profile.ensure", hooks[key])

	def test_customer_doc_events_are_wired(self):
		events = _hooks()["doc_events"]["Customer"]
		self.assertIn("ffl_core.customer_profile.validate_customer", events["validate"])
		self.assertEqual(events["on_update"], "ffl_core.customer_profile.privatize_id_attachments")
		self.assertEqual(events["on_trash"], "ffl_core.customer_profile.refuse_delete_with_family")

	def test_an_existing_customer_without_a_status_becomes_active_and_nothing_else_moves(self):
		doc = _customer()
		frappe.db.sql("update `tabCustomer` set customer_status=NULL where name=%s", doc.name)
		before = frappe.db.get_value("Customer", doc.name, ["modified", "customer_name"], as_dict=True)

		self.assertEqual(profile_setup.ensure(), {"ok": 1})
		after = frappe.db.get_value("Customer", doc.name,
			["customer_status", "modified", "customer_name"], as_dict=True)
		self.assertEqual(after.customer_status, "Active")
		self.assertEqual(after.modified, before.modified)
		self.assertEqual(after.customer_name, before.customer_name)

	def test_a_second_ensure_changes_no_field_definition(self):
		def stamps():
			return dict(frappe.get_all("Custom Field",
				filters={"dt": "Customer", "fieldname": ("in", _OUR_FIELDS)},
				fields=["fieldname", "modified"], as_list=True))

		first = stamps()
		self.assertEqual(sorted(first), sorted(_OUR_FIELDS))
		self.assertEqual(profile_setup.ensure(), {"ok": 1})
		self.assertEqual(stamps(), first)

	def test_a_new_customer_is_active(self):
		self.assertEqual(_customer().customer_status, "Active")

	def test_the_profile_tab_survives_a_customize_form_field_order(self):
		"""A Customize Form save pins every field it saw in a field_order
		Property Setter (the Item form lost a tab to one). Fields that setter has
		never seen must still land by insert_after."""
		self.addCleanup(frappe.clear_cache, doctype="Customer")
		# Nor has it seen the Agreements section that ships at the end of the tab.
		ours = _OUR_FIELDS + [f["fieldname"] for f in agreements_setup.FIELDS["Customer"]]
		order = [f.fieldname for f in frappe.get_meta("Customer").fields
			if f.fieldname not in ours]
		ps = frappe.get_doc({"doctype": "Property Setter", "doctype_or_field": "DocType",
			"doc_type": "Customer", "property": "field_order", "property_type": "Data",
			"value": json.dumps(order)})
		ps.flags.ignore_permissions = True
		ps.insert()
		self.addCleanup(frappe.db.delete, "Property Setter", {"name": ps.name})
		frappe.clear_cache(doctype="Customer")

		names = [f.fieldname for f in frappe.get_meta("Customer").fields]
		start = names.index("loyalty_program_tier") + 1
		self.assertEqual(names[start:start + len(ours)], ours)


class DefaultCustomerType(FrappeTestCase):
	"""A new customer is an Individual unless a path says otherwise."""

	_FILTERS = {"doc_type": "Customer", "field_name": "customer_type", "property": "default"}

	def test_a_new_customer_defaults_to_individual(self):
		self.assertEqual(frappe.new_doc("Customer").customer_type, "Individual")

	def test_ensure_seeds_it_once_and_never_overrides_the_stores_own_choice(self):
		from ffl_core.setup import customer_identity
		customer_identity.ensure()
		customer_identity.ensure()
		self.assertEqual(frappe.db.count("Property Setter", self._FILTERS), 1)
		name = frappe.db.get_value("Property Setter", self._FILTERS, "name")
		self.assertEqual(frappe.db.get_value("Property Setter", name, "value"), "Individual")
		frappe.db.set_value("Property Setter", name, "value", "Company")
		customer_identity.ensure()
		self.assertEqual(frappe.db.get_value("Property Setter", name, "value"), "Company")


class StatusRules(FrappeTestCase):
	"""Banned needs a reason; the other statuses do not."""

	def test_banned_without_a_reason_is_refused(self):
		doc = _customer()
		for reason in (None, "", "   "):
			with self.subTest(reason=reason):
				doc.customer_status = "Banned"
				doc.status_reason = reason
				with self.assertRaises(frappe.ValidationError):
					doc.save()
				doc.reload()

	def test_banned_with_a_reason_saves(self):
		doc = _customer()
		doc.customer_status = "Banned"
		doc.status_reason = "Threatened staff"
		doc.save()
		self.assertEqual(frappe.db.get_value("Customer", doc.name, "customer_status"), "Banned")

	def test_inactive_needs_no_reason(self):
		doc = _customer(customer_status="Inactive")
		self.assertEqual(doc.customer_status, "Inactive")

	def test_a_blank_status_is_saved_as_active(self):
		doc = _customer()
		doc.customer_status = ""
		doc.save()
		self.assertEqual(doc.customer_status, "Active")

	def test_alert_rows_are_stamped(self):
		doc = _customer(customer_alerts=[{"message": "Pays cash only"}])
		row = doc.customer_alerts[0]
		self.assertEqual(row.created_by, frappe.session.user)
		self.assertTrue(row.created_on)
		self.assertEqual(row.active, 1)


class IdNumberSwitch(SettingsSandbox, FrappeTestCase):
	"""Switch off → DOB and every ID number are blanked on the server,
	whatever path wrote them; switch on → they are kept."""

	_ID_ROW = {"id_type": "Driver License", "id_number": "D1234567",
		"issuing_authority": "TX", "expiry_date": "2030-01-31"}

	def _with_ids(self, **extra):
		return _customer(date_of_birth="1980-02-03", customer_ids=[dict(self._ID_ROW)], **extra)

	def test_off_blanks_them_on_insert(self):
		self.set_single(*_SWITCH, 0)
		doc = self._with_ids()
		stored = frappe.get_doc("Customer", doc.name)
		self.assertIsNone(stored.date_of_birth)
		self.assertEqual(len(stored.customer_ids), 1)
		row = stored.customer_ids[0]
		self.assertFalse(row.id_number)
		# Only the number goes; type, issuer and dates stay.
		self.assertEqual((row.id_type, row.issuing_authority, str(row.expiry_date)),
			("Driver License", "TX", "2030-01-31"))

	def test_off_is_the_default_on_a_site_that_never_saved_the_switch(self):
		self.clear_single(*_SWITCH)
		doc = self._with_ids()
		self.assertIsNone(frappe.db.get_value("Customer", doc.name, "date_of_birth"))

	def test_off_blanks_them_on_a_later_save(self):
		self.set_single(*_SWITCH, 1)
		doc = self._with_ids()
		self.set_single(*_SWITCH, 0)
		doc.reload()
		doc.save()
		stored = frappe.get_doc("Customer", doc.name)
		self.assertIsNone(stored.date_of_birth)
		self.assertFalse(stored.customer_ids[0].id_number)

	def test_off_blanks_them_through_the_rest_api(self):
		from frappe import client
		self.set_single(*_SWITCH, 0)
		saved = client.insert({"doctype": "Customer", "customer_name": _uniq("REST"),
			"customer_type": "Individual", "customer_group": "Individual",
			"territory": "All Territories", "date_of_birth": "1980-02-03",
			"customer_ids": [dict(self._ID_ROW)]})
		stored = frappe.get_doc("Customer", saved["name"])
		self.assertIsNone(stored.date_of_birth)
		self.assertFalse(stored.customer_ids[0].id_number)

	def test_on_keeps_them(self):
		self.set_single(*_SWITCH, 1)
		doc = self._with_ids()
		stored = frappe.get_doc("Customer", doc.name)
		self.assertEqual(str(stored.date_of_birth), "1980-02-03")
		self.assertEqual(stored.customer_ids[0].id_number, "D1234567")

	def test_boot_tells_the_browser(self):
		from ffl_integrations.boot import extend_bootinfo
		for value in (0, 1):
			with self.subTest(switch=value):
				self.set_single(*_SWITCH, value)
				boot = frappe._dict()
				extend_bootinfo(boot)
				self.assertEqual(boot.ffl_store_customer_id_numbers, value)

	def test_an_id_attachment_is_made_private(self):
		doc = _customer()
		name = f"_test-id-{frappe.generate_hash(length=10)}.png"
		file_doc = frappe.get_doc({"doctype": "File", "file_name": name, "is_private": 0,
			"content": frappe.generate_hash(length=32).encode(),
			"attached_to_doctype": "Customer", "attached_to_name": doc.name,
			"attached_to_field": "attachment"})
		file_doc.flags.ignore_permissions = True
		file_doc.insert()
		self.assertTrue(file_doc.file_url.startswith("/files/"))

		doc.append("customer_ids", {"id_type": "State ID", "attachment": file_doc.file_url})
		doc.save()

		url = frappe.db.get_value("Customer ID Document", doc.customer_ids[0].name, "attachment")
		self.assertTrue(url.startswith("/private/files/"), url)
		self.assertEqual(frappe.db.get_value("File", file_doc.name, "is_private"), 1)
		# The parent has no `attachment` column: the File keeps naming the child field.
		self.assertEqual(frappe.db.get_value("File", file_doc.name, "attached_to_field"), "attachment")


	def test_a_file_attached_elsewhere_is_never_made_private(self):
		"""A product photo picked from the library as an ID image must stay on the website."""
		item = frappe.get_doc({"doctype": "Item", "item_code": _uniq("Photo Item"),
			"item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 0})
		item.insert(ignore_permissions=True)
		photo = _public_file(attached_to=("Item", item.name))
		doc = _customer()
		doc.append("customer_ids", {"id_type": "State ID", "attachment": photo.file_url})
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		self.assertEqual(frappe.db.get_value("File", photo.name, "is_private"), 0)
		self.assertTrue(frappe.db.get_value("File", photo.name, "file_url").startswith("/files/"))

	def test_another_customers_public_file_is_not_taken_either(self):
		other = _customer()
		theirs = _public_file(attached_to=("Customer", other.name))
		doc = _customer()
		doc.append("customer_ids", {"id_type": "State ID", "attachment": theirs.file_url})
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		self.assertEqual(frappe.db.get_value("File", theirs.name, "is_private"), 0)

	def test_an_unattached_public_file_is_refused_and_left_alone(self):
		"""A store logo in the file manager is not an upload of this customer's ID."""
		loose = _public_file()
		doc = _customer()
		doc.append("customer_ids", {"id_type": "State ID", "attachment": loose.file_url})
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		self.assertEqual(frappe.db.get_value("File", loose.name, ["is_private", "file_url"]),
			(0, loose.file_url))

	def test_an_already_private_id_file_is_accepted_only_on_its_own_customer(self):
		"""Another customer's /private ID image cannot be pointed at (it would be served to
		whoever opens the second record); the owner re-saving its own is untouched."""
		owner = _customer()
		file_doc = _public_file(attached_to=("Customer", owner.name))
		owner.append("customer_ids", {"id_type": "State ID", "attachment": file_doc.file_url})
		owner.save()
		private_url = frappe.db.get_value("Customer ID Document", owner.customer_ids[0].name, "attachment")
		self.assertTrue(private_url.startswith("/private/files/"), private_url)

		owner.reload()
		owner.save()  # its own private file stays accepted
		self.assertEqual(frappe.db.get_value("Customer ID Document", owner.customer_ids[0].name,
			"attachment"), private_url)

		for url in (private_url, "/private/files/no-such-file.png"):
			with self.subTest(url=url):
				other = _customer()
				other.append("customer_ids", {"id_type": "State ID", "attachment": url})
				with self.assertRaises(frappe.ValidationError):
					other.save()

	def test_an_attachment_with_no_file_row_is_refused_not_left_public(self):
		for url in ("https://example.com/id-scan.png", f"/files/hand-typed-{frappe.generate_hash(length=6)}.png"):
			with self.subTest(url=url):
				doc = _customer()
				doc.append("customer_ids", {"id_type": "State ID", "attachment": url})
				with self.assertRaises(frappe.ValidationError):
					doc.save()

	def test_a_privacy_flip_that_returns_nothing_is_refused(self):
		from unittest import mock
		doc = _customer()
		own = _public_file(attached_to=("Customer", doc.name))
		doc.append("customer_ids", {"id_type": "State ID", "attachment": own.file_url})
		with mock.patch("ffl_core.customer_profile.set_file_privacy", return_value=None), \
				self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_an_upload_on_a_new_form_is_relinked_then_made_private(self):
		temp = f"new-customer-{frappe.generate_hash(length=8)}"
		upload = _public_file(attached_to=("Customer", temp))
		doc = frappe.get_doc({"doctype": "Customer", "customer_name": _uniq("NewForm"),
			"customer_type": "Individual", "customer_group": "Individual",
			"territory": "All Territories", "__temporary_name": temp,
			"customer_ids": [{"id_type": "State ID", "attachment": upload.file_url}]})
		doc.flags.ignore_permissions = True
		doc.insert()
		self.assertEqual(frappe.db.get_value("File", upload.name, "attached_to_name"), doc.name)
		self.assertEqual(frappe.db.get_value("File", upload.name, "is_private"), 1)

class SwitchingIdNumbersOff(SettingsSandbox, FrappeTestCase):
	"""Store Customer ID Numbers 1 -> 0 erases what is stored, once, in one step;
	every other transition leaves the data alone."""

	_ID_ROW = {"id_type": "Driver License", "id_number": "D7654321",
		"issuing_authority": "TX", "expiry_date": "2031-02-03"}

	def _stored_customer(self):
		"""A customer holding a DOB and an ID number (stored while the switch is on)."""
		self.set_single(*_SWITCH, 1)
		return _customer(date_of_birth="1981-04-05", customer_ids=[dict(self._ID_ROW)])

	def _save_settings(self, value):
		"""A real FFL Settings save, so the controller's on_update runs."""
		settings = frappe.get_doc("FFL Settings")
		settings.store_customer_id_numbers = value
		settings.save()
		return settings

	def _stored(self, doc):
		row = frappe.db.get_value("Customer ID Document", {"parent": doc.name},
			["id_number", "id_type", "expiry_date"], as_dict=True)
		return frappe.db.get_value("Customer", doc.name, "date_of_birth"), row

	def _erase_comments(self):
		return frappe.get_all("Comment", filters={"reference_doctype": "FFL Settings",
			"comment_type": "Comment", "content": ["like", "%turned off Store Customer ID Numbers%"]},
			pluck="content", order_by="creation asc")

	def test_one_to_zero_erases_every_stored_number_and_date_of_birth(self):
		mine = self._stored_customer()
		before = customer_profile.stored_id_counts()
		self.assertGreaterEqual(before["dates_of_birth"], 1)
		self.assertGreaterEqual(before["id_numbers"], 1)
		frappe.get_cached_doc("Customer", mine.name)  # warm the cache

		self._save_settings(0)

		dob, row = self._stored(mine)
		self.assertIsNone(dob)
		self.assertFalse(row.id_number)
		# Only the number goes; type and dates stay.
		self.assertEqual((row.id_type, str(row.expiry_date)), ("Driver License", "2031-02-03"))
		self.assertEqual(customer_profile.stored_id_counts(), {"dates_of_birth": 0, "id_numbers": 0})
		cached = frappe.get_cached_doc("Customer", mine.name)
		self.assertIsNone(cached.date_of_birth)
		self.assertFalse(cached.customer_ids[0].id_number)
		note = self._erase_comments()[-1]
		self.assertIn(f"erased {before['dates_of_birth']} date(s) of birth and "
			f"{before['id_numbers']} ID number(s)", note)

	def test_zero_to_zero_changes_nothing(self):
		mine = self._stored_customer()
		self.set_single(*_SWITCH, 0)
		comments = len(self._erase_comments())
		self._save_settings(0)
		dob, row = self._stored(mine)
		self.assertEqual((str(dob), row.id_number), ("1981-04-05", "D7654321"))
		self.assertEqual(len(self._erase_comments()), comments)

	def test_one_to_one_changes_nothing(self):
		mine = self._stored_customer()
		comments = len(self._erase_comments())
		self._save_settings(1)
		dob, row = self._stored(mine)
		self.assertEqual((str(dob), row.id_number), ("1981-04-05", "D7654321"))
		self.assertEqual(len(self._erase_comments()), comments)

	def test_zero_to_one_changes_nothing(self):
		mine = self._stored_customer()
		self.set_single(*_SWITCH, 0)
		comments = len(self._erase_comments())
		self._save_settings(1)
		dob, row = self._stored(mine)
		self.assertEqual((str(dob), row.id_number), ("1981-04-05", "D7654321"))
		self.assertEqual(len(self._erase_comments()), comments)

	def test_the_counts_for_the_confirm_need_write_access_to_the_settings(self):
		self._stored_customer()
		counts = customer_profile.get_stored_id_counts()
		self.assertEqual(counts["total"], counts["dates_of_birth"] + counts["id_numbers"])
		self.assertGreaterEqual(counts["total"], 2)
		user = _user("no-settings-write@example.com", "Sales User")
		self.addCleanup(frappe.set_user, frappe.session.user)
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			customer_profile.get_stored_id_counts()


class FamilyRules(FrappeTestCase):
	"""One level of family; a head with members cannot be deleted."""

	def test_a_member_links_to_the_head(self):
		head = _customer()
		member = _customer(primary_customer=head.name, relationship="Spouse")
		family = customer_profile.get_family(head.name)
		self.assertIsNone(family["primary"])
		self.assertEqual([(m.name, m.relationship) for m in family["members"]],
			[(member.name, "Spouse")])
		mine = customer_profile.get_family(member.name)
		self.assertEqual(mine["primary"]["name"], head.name)
		self.assertEqual(mine["relationship"], "Spouse")
		self.assertEqual(mine["members"], [])

	def test_a_member_cannot_have_members(self):
		head = _customer()
		member = _customer(primary_customer=head.name)
		with self.assertRaises(frappe.ValidationError):
			_customer(primary_customer=member.name)

	def test_a_head_with_members_cannot_join_another_family(self):
		head = _customer()
		_customer(primary_customer=head.name)
		other = _customer()
		head.primary_customer = other.name
		with self.assertRaises(frappe.ValidationError):
			head.save()

	def test_a_customer_cannot_be_in_their_own_family(self):
		doc = _customer()
		doc.primary_customer = doc.name
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_clearing_the_head_clears_the_relationship(self):
		head = _customer()
		member = _customer(primary_customer=head.name, relationship="Child")
		member.primary_customer = None
		member.save()
		self.assertFalse(frappe.db.get_value("Customer", member.name, "relationship"))

	def test_a_head_with_members_cannot_be_deleted_until_they_are_moved(self):
		head = _customer()
		member = _customer(primary_customer=head.name)
		# force=True skips frappe's own "is linked" check, leaving on_trash as
		# the only guard — the path a cleanup script or bench console takes.
		for force in (False, True):
			with self.subTest(force=force), self.assertRaises(frappe.ValidationError):
				frappe.delete_doc("Customer", head.name, ignore_permissions=True, force=force)
		self.assertTrue(frappe.db.exists("Customer", head.name))

		member.primary_customer = None
		member.save()
		frappe.delete_doc("Customer", head.name, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Customer", head.name))


	def test_get_family_tells_the_form_who_may_merge(self):
		doc = _customer()
		self.addCleanup(frappe.set_user, "Administrator")
		for roles, expected in ((("Sales Manager",), True), (("Accounts User",), False)):
			with self.subTest(roles=roles):
				frappe.set_user("Administrator")
				frappe.set_user(_user(f"viewer-{roles[0].split()[0].lower()}@example.com", *roles))
				self.assertIs(customer_profile.get_family(doc.name)["can_merge"], expected)

class MergeCustomer(SettingsSandbox, FrappeTestCase):
	"""The source's records all end up on the target; two different
	dealers never merge; only a manager may merge."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")

	def _raw(self, doctype: str, **fields) -> str:
		"""A bare row carrying a Customer link — what the merge must move is the
		link, not a valid document, so the row skips its own validation."""
		doc = frappe.get_doc({"doctype": doctype, **fields})
		doc.name = f"_T-{frappe.generate_hash(length=10)}"
		doc.db_insert()
		return doc.name

	def test_records_move_to_the_target_and_the_source_is_gone(self):
		self.set_single(*_SWITCH, 1)
		licence = {"id_type": "Driver License", "id_number": "D7654321", "expiry_date": "2031-03-04"}
		src = _customer(_uniq("Merge Src"), customer_alerts=[{"message": "Owes $40"}],
			customer_ids=[dict(licence), {"id_type": "CCW", "id_number": "CHL99"}])
		tgt = _customer(_uniq("Merge Tgt"), customer_ids=[dict(licence)])
		member = _customer(primary_customer=src.name, relationship="Child")
		contact = frappe.get_doc({"doctype": "Contact", "first_name": "Merge",
			"links": [{"link_doctype": "Customer", "link_name": src.name}]})
		contact.insert(ignore_permissions=True)
		open_si = self._raw("Sales Invoice", customer=src.name, customer_name=src.customer_name,
			docstatus=0)
		cancelled_si = self._raw("Sales Invoice", customer=src.name,
			customer_name=src.customer_name, docstatus=2)
		disposition = self._raw("FFL Disposition", customer=src.name)

		out = customer_profile.merge_customer(src.name, tgt.name)

		self.assertEqual(out, {"name": tgt.name, "customer_name": tgt.customer_name,
			"alerts_copied": 1, "ids_copied": 1})
		self.assertFalse(frappe.db.exists("Customer", src.name))
		kept = frappe.get_doc("Customer", tgt.name)
		self.assertEqual(kept.customer_name, tgt.customer_name)
		self.assertEqual([a.message for a in kept.customer_alerts], ["Owes $40"])
		self.assertEqual(sorted((r.id_type, r.id_number) for r in kept.customer_ids),
			[("CCW", "CHL99"), ("Driver License", "D7654321")])
		self.assertEqual(frappe.db.get_value("Customer", member.name, "primary_customer"), tgt.name)
		self.assertEqual(frappe.get_doc("Contact", contact.name).links[0].link_name, tgt.name)
		self.assertEqual(frappe.db.get_value("Sales Invoice", open_si,
			["customer", "customer_name"]), (tgt.name, tgt.customer_name))
		# A cancelled document keeps its historical spelling.
		self.assertEqual(frappe.db.get_value("Sales Invoice", cancelled_si,
			["customer", "customer_name"]), (tgt.name, src.customer_name))
		self.assertEqual(frappe.db.get_value("FFL Disposition", disposition, "customer"), tgt.name)
		self.assertEqual(frappe.db.count("Customer Alert", {"parent": src.name}), 0)
		comments = frappe.get_all("Comment", filters={"reference_doctype": "Customer",
			"reference_name": tgt.name, "comment_type": "Comment"}, pluck="content")
		self.assertTrue(any(src.name in c for c in comments), comments)

	def test_every_open_document_with_a_name_copy_gets_the_kept_name(self):
		src, tgt = _customer(_uniq("Snap Src")), _customer(_uniq("Snap Tgt"))
		docs = {
			"POS Invoice": dict(customer=src.name),
			"Delivery Note": dict(customer=src.name),
			"Quotation": dict(quotation_to="Customer", party_name=src.name),
		}
		open_ = {dt: self._raw(dt, customer_name=src.customer_name, docstatus=0, **f)
			for dt, f in docs.items()}
		cancelled = {dt: self._raw(dt, customer_name=src.customer_name, docstatus=2, **f)
			for dt, f in docs.items()}
		lead_quote = self._raw("Quotation", quotation_to="Lead", party_name=src.name,
			customer_name="A Lead", docstatus=0)

		customer_profile.merge_customer(src.name, tgt.name)

		for dt in docs:
			with self.subTest(doctype=dt):
				self.assertEqual(frappe.db.get_value(dt, open_[dt], "customer_name"), tgt.customer_name)
				self.assertEqual(frappe.db.get_value(dt, cancelled[dt], "customer_name"), src.customer_name)
		self.assertEqual(frappe.db.get_value("Quotation", lead_quote, "customer_name"), "A Lead")

	def test_an_id_image_moves_with_its_row(self):
		"""The copied ID row keeps the source's private image, which is then the target's."""
		src, tgt = _customer(_uniq("Img Src")), _customer(_uniq("Img Tgt"))
		file_doc = _public_file(attached_to=("Customer", src.name))
		src.append("customer_ids", {"id_type": "State ID", "attachment": file_doc.file_url})
		src.save()
		private_url = frappe.db.get_value("Customer ID Document", src.customer_ids[0].name, "attachment")
		self.assertTrue(private_url.startswith("/private/files/"), private_url)

		self.assertEqual(customer_profile.merge_customer(src.name, tgt.name)["ids_copied"], 1)

		kept = frappe.get_doc("Customer", tgt.name)
		self.assertEqual([r.attachment for r in kept.customer_ids], [private_url])
		self.assertEqual(frappe.db.get_value("File", file_doc.name, "attached_to_name"), tgt.name)

	def test_two_different_dealers_are_never_merged(self):
		src, tgt = _customer(), _customer()
		frappe.db.set_value("Customer", src.name, {"is_ffl_dealer": 1, "ffl_number": "1-23-456-01-2A-11111"})
		frappe.db.set_value("Customer", tgt.name, {"is_ffl_dealer": 1, "ffl_number": "1-23-456-01-2A-22222"})
		with self.assertRaises(frappe.ValidationError):
			customer_profile.merge_customer(src.name, tgt.name)
		self.assertTrue(frappe.db.exists("Customer", src.name))

	def test_the_same_dealer_twice_is_merged(self):
		src, tgt = _customer(), _customer()
		frappe.db.set_value("Customer", src.name, {"is_ffl_dealer": 1, "ffl_number": "1-23-456-01-2A-33333"})
		frappe.db.set_value("Customer", tgt.name, {"is_ffl_dealer": 1, "ffl_number": "123456012A33333"})
		customer_profile.merge_customer(src.name, tgt.name)
		self.assertFalse(frappe.db.exists("Customer", src.name))

	def test_a_dealer_is_not_merged_into_a_non_dealer(self):
		src, tgt = _customer(), _customer()
		frappe.db.set_value("Customer", src.name, {"is_ffl_dealer": 1, "ffl_number": "1-23-456-01-2A-44444"})
		with self.assertRaises(frappe.ValidationError):
			customer_profile.merge_customer(src.name, tgt.name)

	def test_a_non_manager_is_refused(self):
		src, tgt = _customer(), _customer()
		frappe.set_user(_user("profile-clerk@example.com", "Sales User"))
		with self.assertRaises(frappe.PermissionError):
			customer_profile.merge_customer(src.name, tgt.name)
		frappe.set_user("Administrator")
		self.assertTrue(frappe.db.exists("Customer", src.name))

	def test_a_sales_manager_may_merge(self):
		src, tgt = _customer(), _customer()
		frappe.set_user(_user("profile-manager@example.com", "Sales Manager"))
		customer_profile.merge_customer(src.name, tgt.name)
		frappe.set_user("Administrator")
		self.assertFalse(frappe.db.exists("Customer", src.name))

	def test_a_banned_source_makes_the_target_banned(self):
		src = _customer(customer_status="Banned", status_reason="Straw purchase attempt")
		tgt = _customer()
		customer_profile.merge_customer(src.name, tgt.name)
		row = frappe.db.get_value("Customer", tgt.name, ["customer_status", "status_reason"],
			as_dict=True)
		self.assertEqual(row.customer_status, "Banned")
		self.assertIn("Straw purchase attempt", row.status_reason)

	def test_merges_that_would_nest_families_are_refused(self):
		head = _customer()
		member = _customer(primary_customer=head.name)
		with self.assertRaises(frappe.ValidationError):
			customer_profile.merge_customer(head.name, member.name)   # target belongs to source

		other_head = _customer()
		elsewhere = _customer(primary_customer=other_head.name)
		with self.assertRaises(frappe.ValidationError):
			customer_profile.merge_customer(head.name, elsewhere.name)   # source has members

	def test_a_member_source_hands_its_family_to_a_target_without_one(self):
		head = _customer()
		src = _customer(primary_customer=head.name, relationship="Sibling")
		tgt = _customer()
		customer_profile.merge_customer(src.name, tgt.name)
		self.assertEqual(frappe.db.get_value("Customer", tgt.name,
			["primary_customer", "relationship"]), (head.name, "Sibling"))

	def test_a_registers_default_customer_is_never_merged(self):
		src, tgt = _customer(), _customer()
		self._raw("POS Profile", customer=src.name)
		with self.assertRaises(frappe.ValidationError):
			customer_profile.merge_customer(src.name, tgt.name)
		self.assertTrue(frappe.db.exists("Customer", src.name))

	def test_a_customer_cannot_be_merged_into_itself(self):
		doc = _customer()
		with self.assertRaises(frappe.ValidationError):
			customer_profile.merge_customer(doc.name, doc.name)

	def test_a_family_head_that_is_also_a_member_is_never_merged(self):
		"""Validate forbids this state; only a race makes it. Merging would nest families."""
		head = _customer()
		src = _customer(primary_customer=head.name)
		member = _customer()
		frappe.db.set_value("Customer", member.name, "primary_customer", src.name)  # past validate
		tgt = _customer()
		with self.assertRaises(frappe.ValidationError):
			customer_profile.merge_customer(src.name, tgt.name)
		self.assertTrue(frappe.db.exists("Customer", src.name))
		self.assertEqual(frappe.db.get_value("Customer", member.name, "primary_customer"), src.name)
		self.assertFalse(frappe.db.get_value("Customer", tgt.name, "primary_customer"))


class MergeKeepsOrderMatching(SettingsSandbox, FrappeTestCase):
	"""A merge records the source's email, Contact emails and GunBroker
	id as aliases on the target; the Woo and GunBroker resolvers read them, and
	nothing but a merge ever makes a customer answer to someone else's address."""

	def setUp(self):
		frappe.set_user("Administrator")

	def _gb_order(self, user_id, email):
		return frappe._dict(buyer_user_id=user_id, customer_email=email, customer_name="GB Buyer",
			buyer_username="gbbuyer", customer_phone="")

	def _woo(self, email):
		from ffl_woo_sync.woocommerce.revenue import _resolve_web_customer
		return _resolve_web_customer(frappe._dict(customer_email=email, name="WOO-T",
			customer_name="Woo Buyer", customer_phone=""))

	def _gb(self, gb_id, email):
		from ffl_integrations.gunbroker.revenue import resolve_customer
		return resolve_customer(self._gb_order(gb_id, email))

	def _no_new_customer(self, fn, *args):
		before = frappe.db.count("Customer")
		out = fn(*args)
		self.assertEqual(frappe.db.count("Customer"), before, "an order created a new Customer")
		return out

	def _gb_id(self):
		return f"gb-{frappe.generate_hash(length=8)}"

	def _aliases(self, customer):
		return sorted((r.alias_type, r.value) for r in
			frappe.get_doc("Customer", customer).customer_aliases)

	def _merged(self, tgt_gb_id=None):
		"""A merged pair: the source has an email, a Contact email and a GB id."""
		src_email, src_contact_email, tgt_email = _email(), _email(), _email()
		src = _customer(gb_user_id=self._gb_id())
		tgt = _customer(**({"gb_user_id": tgt_gb_id} if tgt_gb_id else {}))
		frappe.db.set_value("Customer", src.name, "email_id", src_email)
		frappe.db.set_value("Customer", tgt.name, "email_id", tgt_email)
		_contact(src.name, src_contact_email)
		customer_profile.merge_customer(src.name, tgt.name)
		return frappe._dict(src=src, tgt=tgt, src_email=src_email,
			src_contact_email=src_contact_email, tgt_email=tgt_email)

	def test_the_merge_records_emails_and_the_gb_id_as_aliases(self):
		m = self._merged(tgt_gb_id="gb-target-1")
		self.assertEqual(self._aliases(m.tgt.name), sorted([
			("Email", m.src_email), ("Email", m.src_contact_email),
			("GunBroker ID", m.src.gb_user_id)]))
		row = frappe.get_doc("Customer", m.tgt.name).customer_aliases[0]
		self.assertEqual(row.merged_from, m.src.name)
		self.assertTrue(row.merged_on)
		# The target's own keys are untouched (nothing is carried onto them).
		self.assertEqual(frappe.db.get_value("Customer", m.tgt.name, ["email_id", "gb_user_id"]),
			(m.tgt_email, "gb-target-1"))

	def test_an_order_with_the_sources_email_or_gb_id_lands_on_the_target(self):
		"""Woo and GunBroker, every key, no new customer, gb_user_id unchanged."""
		m = self._merged(tgt_gb_id="gb-target-1")
		other_id = self._gb_id()
		for email in (m.src_email, m.src_contact_email, m.src_email.upper()):
			with self.subTest(email=email):
				self.assertEqual(self._no_new_customer(self._woo, email), m.tgt.name)
				self.assertEqual(self._no_new_customer(self._gb, other_id, email), m.tgt.name)
		self.assertEqual(self._no_new_customer(self._gb, m.src.gb_user_id, _email()), m.tgt.name)
		self.assertEqual(self._no_new_customer(self._gb, m.src.gb_user_id, m.src_email), m.tgt.name)
		# The kept customer's own keys still work, and nothing flipped its GB id.
		self.assertEqual(self._no_new_customer(self._woo, m.tgt_email), m.tgt.name)
		self.assertEqual(frappe.db.get_value("Customer", m.tgt.name, "gb_user_id"), "gb-target-1")

	def test_a_woo_order_with_an_alias_email_never_lands_on_a_dealer(self):
		"""A retail buyer merged into a dealer: the alias email gets a fresh retail customer
		(mirrors the GunBroker guard); the dealer's own email still matches the dealer."""
		src, tgt = _customer(), _customer()
		src_email, tgt_email = _email(), _email()
		frappe.db.set_value("Customer", src.name, "email_id", src_email)
		frappe.db.set_value("Customer", tgt.name, {"email_id": tgt_email, "is_ffl_dealer": 1,
			"ffl_number": f"1-23-456-01-2A-{random.randint(10000, 99999)}"})
		customer_profile.merge_customer(src.name, tgt.name)
		self.assertIn(("Email", src_email), self._aliases(tgt.name))

		landed = self._woo(src_email)
		self.assertNotEqual(landed, tgt.name)
		self.assertEqual(frappe.db.get_value("Customer", landed, ["email_id", "is_ffl_dealer"]),
			(src_email, 0))
		self.assertEqual(self._woo(tgt_email), tgt.name)

	def test_a_target_without_a_gb_id_does_not_get_one_from_the_merge_or_the_orders(self):
		m = self._merged()
		self.assertFalse(frappe.db.get_value("Customer", m.tgt.name, "gb_user_id"))
		self.assertEqual(self._no_new_customer(self._gb, m.src.gb_user_id, m.src_email), m.tgt.name)
		self.assertEqual(self._no_new_customer(self._gb, self._gb_id(), m.src_contact_email), m.tgt.name)
		self.assertFalse(frappe.db.get_value("Customer", m.tgt.name, "gb_user_id"))

	def test_an_unmerged_customer_matched_by_email_still_gets_the_orders_gb_id(self):
		"""The old behaviour, for everyone who was never merged."""
		email, gb_id = _email(), self._gb_id()
		cust = _customer()
		frappe.db.set_value("Customer", cust.name, "email_id", email)
		self.assertEqual(self._no_new_customer(self._gb, gb_id, email), cust.name)
		self.assertEqual(frappe.db.get_value("Customer", cust.name, "gb_user_id"), gb_id)

	def test_a_secondary_contact_email_without_a_merge_matches_nobody(self):
		"""Contact emails take no part in matching."""
		cust, for_woo, for_gb = _customer(), _email(), _email()
		frappe.db.set_value("Customer", cust.name, "email_id", _email())
		_contact(cust.name, for_woo)
		_contact(cust.name, for_gb)
		before = frappe.db.count("Customer")
		woo = self._woo(for_woo)
		gb = self._gb(self._gb_id(), for_gb)
		self.assertNotIn(cust.name, (woo, gb))
		self.assertEqual(frappe.db.count("Customer"), before + 2)
		self.assertIsNone(customer_profile.find_customer_by_email(_email()))

	def test_values_the_target_already_has_are_not_repeated(self):
		shared = _email()
		src, tgt = _customer(gb_user_id="gb-same"), _customer(gb_user_id="gb-same")
		frappe.db.set_value("Customer", src.name, "email_id", shared)
		frappe.db.set_value("Customer", tgt.name, "email_id", shared.upper())
		_contact(src.name, shared)
		customer_profile.merge_customer(src.name, tgt.name)
		self.assertEqual(self._aliases(tgt.name), [])

		again = _customer()
		frappe.db.set_value("Customer", again.name, "email_id", shared)
		_contact(again.name, shared)           # the same address as email_id and on a Contact
		customer_profile.merge_customer(again.name, tgt.name)
		self.assertEqual(self._aliases(tgt.name), [])

	def test_a_second_merge_keeps_the_first_ones_aliases(self):
		first = self._merged()
		last = _customer()
		customer_profile.merge_customer(first.tgt.name, last.name)
		self.assertEqual(self._no_new_customer(self._woo, first.src_email), last.name)
		self.assertEqual(self._no_new_customer(self._woo, first.tgt_email), last.name)
		self.assertEqual(self._no_new_customer(self._gb, first.src.gb_user_id, _email()), last.name)
		rows = frappe.get_doc("Customer", last.name).customer_aliases
		self.assertEqual({r.merged_from for r in rows if r.value == first.src_email}, {first.src.name})

	def test_an_alias_held_by_two_customers_matches_nobody_and_is_logged(self):
		email, gb_id = _email(), self._gb_id()
		for _i in range(2):
			holder = _customer()
			holder.append("customer_aliases", {"alias_type": "Email", "value": email})
			holder.append("customer_aliases", {"alias_type": "GunBroker ID", "value": gb_id})
			holder.flags.ignore_permissions = True
			holder.save()
		logs = lambda: frappe.db.count("Error Log", {"method": ["like", "Ambiguous customer alias%"]})
		before = logs()
		self.assertIsNone(customer_profile.find_customer_by_email(email))
		self.assertEqual(logs(), before + 1)
		self.assertIsNone(customer_profile.find_customer_by_gb_id(gb_id))
		self.assertEqual(logs(), before + 2)
		# The resolvers fall through to a new customer rather than guess.
		count = frappe.db.count("Customer")
		self._woo(email)
		self.assertEqual(frappe.db.count("Customer"), count + 1)

	def test_a_gb_alias_never_beats_a_customer_that_owns_the_id(self):
		m = self._merged()
		owner = _customer(gb_user_id=m.src.gb_user_id)   # someone now holds that id outright
		self.assertEqual(self._no_new_customer(self._gb, m.src.gb_user_id, _email()), owner.name)

	def test_an_email_nobody_has_still_makes_a_new_customer(self):
		before = frappe.db.count("Customer")
		self._woo(_email())
		self.assertEqual(frappe.db.count("Customer"), before + 1)

	def test_the_aliases_table_is_read_only_hidden_when_empty_and_not_copied(self):
		df = frappe.get_meta("Customer").get_field("customer_aliases")
		self.assertEqual((df.read_only, df.no_copy, df.options), (1, 1, "Customer Alias"))
		self.assertEqual(df.depends_on, profile_setup.HAS_ALIASES)
		self.assertTrue(frappe.get_meta("Customer Alias").istable)


	def test_a_gb_alias_that_now_belongs_to_a_dealer_is_not_booked_to_the_dealer(self):
		"""A retail buyer merged into a dealer record: the merged GB id must not put a
		retail marketplace order on the dealer (same rule as the email match)."""
		src = _customer(gb_user_id=self._gb_id())
		dealer = _customer()
		frappe.db.set_value("Customer", dealer.name,
			{"is_ffl_dealer": 1, "ffl_number": f"1-23-456-01-2A-{random.randint(10000, 99999)}"})
		customer_profile.merge_customer(src.name, dealer.name)
		self.assertEqual(self._aliases(dealer.name), [("GunBroker ID", src.gb_user_id)])

		before = frappe.db.count("Customer")
		got = self._gb(src.gb_user_id, _email())
		self.assertNotEqual(got, dealer.name)
		self.assertEqual(frappe.db.count("Customer"), before + 1)
		self.assertEqual(frappe.db.get_value("Customer", got, ["is_ffl_dealer", "gb_user_id"]),
			(0, src.gb_user_id))

	def test_a_gb_alias_on_a_dealer_still_falls_through_to_a_retail_email_match(self):
		src_email = _email()
		src = _customer(gb_user_id=self._gb_id())
		frappe.db.set_value("Customer", src.name, "email_id", src_email)
		dealer = _customer()
		frappe.db.set_value("Customer", dealer.name, {"is_ffl_dealer": 1, "ffl_number": "1-23-456-01-2A-66666"})
		customer_profile.merge_customer(src.name, dealer.name)
		retail = _customer()
		other_email = _email()
		frappe.db.set_value("Customer", retail.name, "email_id", other_email)
		self.assertEqual(self._no_new_customer(self._gb, src.gb_user_id, other_email), retail.name)


class AliasesAreWrittenOnlyByMerge(FrappeTestCase):
	"""An alias decides where orders land: only a merge (or a System Manager) writes one."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")
		self.src = _customer(gb_user_id=f"gb-{frappe.generate_hash(length=8)}")
		self.tgt = _customer()
		customer_profile.merge_customer(self.src.name, self.tgt.name)
		self.rows = [(r.alias_type, r.value) for r in
			frappe.get_doc("Customer", self.tgt.name).customer_aliases]
		self.assertTrue(self.rows)

	def _clerk(self):
		# Sales User has Customer write; Sales Manager is the merge role.
		frappe.set_user(_user("alias-clerk@example.com", "Sales User", "Sales Manager"))

	def _stored(self):
		return [(r.alias_type, r.value) for r in
			frappe.get_doc("Customer", self.tgt.name).customer_aliases]

	def test_a_sales_manager_cannot_add_edit_or_remove_alias_rows(self):
		from frappe import client
		self._clerk()
		forged = frappe.generate_hash(length=6)
		edits = {
			"add": lambda d: d.append("customer_aliases", {"alias_type": "Email",
				"value": f"{forged}@example.com"}),
			"edit": lambda d: setattr(d.customer_aliases[0], "value", f"{forged}@example.com"),
			"remove": lambda d: d.set("customer_aliases", []),
		}
		for label, edit in edits.items():
			with self.subTest(edit=label):
				doc = frappe.get_doc("Customer", self.tgt.name)
				edit(doc)
				with self.assertRaises(frappe.ValidationError) as ctx:
					doc.save()
				self.assertIn("cannot be edited", str(ctx.exception))
		# Through the REST client, the way Desk saves.
		doc = frappe.get_doc("Customer", self.tgt.name).as_dict()
		doc["customer_aliases"] = []
		with self.assertRaises(frappe.ValidationError):
			client.save(doc)
		frappe.set_user("Administrator")
		self.assertEqual(self._stored(), self.rows)

	def test_an_ordinary_save_with_the_aliases_untouched_is_fine(self):
		self._clerk()
		doc = frappe.get_doc("Customer", self.tgt.name)
		doc.mobile_no = "5125550100"
		doc.save()
		# And a Desk-style save that sends the rows back as strings.
		sent = frappe.get_doc("Customer", self.tgt.name).as_dict()
		frappe.get_doc(sent).save()
		frappe.set_user("Administrator")
		self.assertEqual(self._stored(), self.rows)

	def test_a_merge_still_writes_them_for_a_sales_manager(self):
		frappe.set_user(_user("alias-manager@example.com", "Sales Manager"))
		extra = _customer(gb_user_id=f"gb-{frappe.generate_hash(length=8)}")
		customer_profile.merge_customer(extra.name, self.tgt.name)
		frappe.set_user("Administrator")
		self.assertIn(("GunBroker ID", extra.gb_user_id), self._stored())

	def test_a_system_manager_may_edit_them(self):
		frappe.set_user(_user("alias-sysman@example.com", "System Manager", "Sales User"))
		doc = frappe.get_doc("Customer", self.tgt.name)
		doc.set("customer_aliases", [])
		doc.save()
		frappe.set_user("Administrator")
		self.assertEqual(self._stored(), [])

	def test_a_new_customer_cannot_arrive_with_aliases(self):
		self._clerk()
		doc = frappe.get_doc({"doctype": "Customer", "customer_name": _uniq("Forged"),
			"customer_type": "Individual", "customer_group": "Individual",
			"territory": "All Territories",
			"customer_aliases": [{"alias_type": "Email", "value": "victim@example.com"}]})
		with self.assertRaises(frappe.ValidationError):
			doc.insert()


class DuplicateCarriesNoPersonalRecords(FrappeTestCase):
	"""The form's Duplicate (frappe.copy_doc with no_copy honoured) starts a new person."""

	def setUp(self):
		frappe.set_user("Administrator")

	def test_no_id_rows_alerts_aliases_family_or_dob_are_copied(self):
		head = _customer()
		src = _customer(customer_alerts=[{"message": "Owes $40"}],
			customer_ids=[{"id_type": "State ID", "issuing_authority": "TX"}],
			primary_customer=head.name, relationship="Child", date_of_birth="1980-02-03")
		merged = _customer(gb_user_id=f"gb-{frappe.generate_hash(length=8)}")
		customer_profile.merge_customer(merged.name, src.name)
		src.reload()
		self.assertTrue(src.customer_aliases and src.customer_ids and src.customer_alerts)

		# Desk's Duplicate honours no_copy; the server helper only when asked to.
		copy = frappe.copy_doc(src, ignore_no_copy=False)
		self.assertEqual((copy.customer_alerts, copy.customer_ids, copy.customer_aliases), ([], [], []))
		self.assertFalse(copy.primary_customer)
		self.assertFalse(copy.relationship)
		self.assertFalse(copy.date_of_birth)

	def test_ensure_flips_an_existing_definition_without_duplicating_it(self):
		names = ("customer_ids", "customer_alerts", "primary_customer", "relationship")
		frappe.db.set_value("Custom Field", {"dt": "Customer", "fieldname": ("in", names)},
			"no_copy", 0)
		frappe.clear_cache(doctype="Customer")
		self.assertEqual(profile_setup.ensure(), {"ok": 1})
		for fieldname in names:
			with self.subTest(field=fieldname):
				rows = frappe.get_all("Custom Field", filters={"dt": "Customer", "fieldname": fieldname},
					fields=["no_copy"])
				self.assertEqual([r.no_copy for r in rows], [1])
		self.assertTrue(frappe.get_meta("Customer").get_field("customer_ids").no_copy)


class MergeRefusesSystemCustomers(FrappeTestCase):
	"""The web-store aggregate and the walk-in customer are shared by every order
	of their kind; merging either away, or into another customer, rewrites history."""

	def setUp(self):
		frappe.set_user("Administrator")

	def _system(self, customer_name: str) -> str:
		existing = frappe.db.get_value("Customer", {"customer_name": customer_name})
		return existing or _customer(customer_name).name

	def test_the_aggregate_and_the_walk_in_customer_are_refused_either_way_round(self):
		from ffl_core.setup.store_setup import OPTIONAL
		names = (customer_profile.WEB_STORE_CUSTOMER, OPTIONAL["WALK_IN_CUSTOMER"])
		for system in (self._system(n) for n in names):
			ordinary = _customer().name
			for src, tgt in ((system, ordinary), (ordinary, system)):
				with self.subTest(src=src, tgt=tgt), self.assertRaises(frappe.ValidationError):
					customer_profile.merge_customer(src, tgt)
				self.assertTrue(frappe.db.exists("Customer", src))

	def test_the_woo_resolver_and_the_refusal_name_the_same_aggregate(self):
		from ffl_woo_sync.woocommerce import revenue
		self.assertEqual(revenue.WEB_CUSTOMER, customer_profile.WEB_STORE_CUSTOMER)
		self.assertEqual(frappe.db.get_value("Customer", revenue._ensure_web_customer(), "customer_name"),
			customer_profile.WEB_STORE_CUSTOMER)

	def test_the_stores_configured_walk_in_name_is_refused_not_just_the_default(self):
		from unittest import mock
		name = _uniq("Counter Sale")
		walk_in = self._system(name)
		ordinary = _customer().name
		# Without the setting the name is an ordinary customer's.
		with mock.patch.dict(os.environ, {"WALK_IN_CUSTOMER": ""}):
			self.assertNotIn(name, customer_profile._system_customer_names())
		with mock.patch.dict(os.environ, {"WALK_IN_CUSTOMER": name}):
			self.assertIn(name, customer_profile._system_customer_names())
			for src, tgt in ((walk_in, ordinary), (ordinary, walk_in)):
				with self.subTest(src=src, tgt=tgt), self.assertRaises(frappe.ValidationError) as ctx:
					customer_profile.merge_customer(src, tgt)
				self.assertIn("system customer", str(ctx.exception))
				self.assertTrue(frappe.db.exists("Customer", src))

