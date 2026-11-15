"""Marketing on the POS connector: the Marketing User role (what it can and cannot reach, signing in to a
connector client) and the endpoints the connector's marketing tools call (ffl_core.api.marketing):
finding contacts, one contact's card, tags, a recorded consent, reminder requests."""
from __future__ import annotations

import frappe

from ffl_core.api import marketing as api
from ffl_core.marketing import CONTACT, EVENT, REQUEST, SETTINGS, contacts, requests, tags
from ffl_core.marketing.contacts import BOUNCED, COMPLAINED, NOT_SUBSCRIBED, SUBSCRIBED, UNSUBSCRIBED, set_status
from ffl_core.roles import MARKETING_ROLE
from ffl_core.tests.crm_support import user
from ffl_core.tests.marketing_support import MarketingTestCase, address, events_of, status_of

MARKETER = "mkt-conn-marketer@example.com"
CLERK = "mkt-conn-clerk@example.com"
OUTSIDER = "mkt-conn-outsider@example.com"
MANAGER = "mkt-conn-manager@example.com"

# What a marketer must never reach. Lead / CRM Lead / CRM Deal: the sales pipeline; the rest: sales, stock,
# customers and the A&D book.
OFF_LIMITS = ("Lead", "CRM Lead", "CRM Deal", "Sales Invoice", "POS Invoice", "Sales Order", "Item", "Item Price",
	"Serial No", "Customer", "Stock Entry", "FFL Acquisition", "FFL Disposition", "Marketing Settings")

# The link-field pick list ERPNext gives every Desk user ("Desk User": select), whatever their roles.
SELECT_FOR_EVERY_DESK_USER = ("Item", "Customer")


def _contact(status: str = SUBSCRIBED, **fields) -> str:
	email = address("conn")
	contacts.get_or_create(email, **fields)
	if status != NOT_SUBSCRIBED:
		set_status(email, status, channel="footer")
	return email


def _as(who: str):
	frappe.set_user(who)


class TestRole(MarketingTestCase):
	def setUp(self):
		super().setUp()
		user(MARKETER, MARKETING_ROLE)

	def test_the_role_is_installed_with_desk_access(self):
		from ffl_core.marketing import setup

		setup.ensure()
		self.assertEqual(frappe.db.get_value("Role", MARKETING_ROLE, "desk_access"), 1)
		self.assertEqual(frappe.db.get_value("User", MARKETER, "user_type"), "System User")

	def test_it_reads_the_marketing_records_and_writes_none_of_them_directly(self):
		_as(MARKETER)
		for doctype in (CONTACT, EVENT, REQUEST):
			self.assertTrue(frappe.has_permission(doctype, "read"), doctype)
			for ptype in ("write", "create", "delete", "export"):
				self.assertFalse(frappe.has_permission(doctype, ptype), f"{doctype} {ptype}")

	def test_it_reaches_nothing_else(self):
		_as(MARKETER)
		for doctype in OFF_LIMITS:
			if not frappe.db.exists("DocType", doctype):
				continue
			self.assertFalse(frappe.has_permission(doctype, "read"), doctype)
			if frappe.get_meta(doctype).issingle:
				continue
			if frappe.has_permission(doctype, "select"):
				# ERPNext lets every Desk user pick a record in a link field (select: the name only).
				self.assertIn(doctype, SELECT_FOR_EVERY_DESK_USER, doctype)
				continue
			with self.assertRaises(frappe.PermissionError, msg=doctype):
				frappe.get_list(doctype, limit_page_length=1)

	def test_it_may_sign_in_to_a_connector_client(self):
		from ffl_core.api.connector import _require_surface

		frappe.set_user("Guest")
		client = frappe.get_doc({"doctype": "OAuth Client", "app_name": "Test connector", "scopes": "all",
			"redirect_uris": "http://localhost:33418/callback",
			"default_redirect_uri": "http://localhost:33418/callback"}).insert(ignore_permissions=True)
		_as(MARKETER)
		self.assertTrue(client.user_has_allowed_role())
		frappe.set_user("Administrator")
		self.set_single("MCP Settings", "enable_full", 1)
		_require_surface("full", MARKETER)   # no role gate on the full surface: each call's own permissions decide

	def test_the_counter_endpoints_refuse_it(self):
		from ffl_core.api import cash_drawer, crm

		_as(MARKETER)
		with self.assertRaises(frappe.PermissionError):
			crm.add_want(deal="nothing", keywords="x")
		with self.assertRaises(frappe.PermissionError):
			cash_drawer.get_log()


class TestFindContacts(MarketingTestCase):
	def test_filters_by_email_name_status_source_and_tag(self):
		a = _contact(first_name="Ada", source="fluentform:3")
		b = _contact(status=UNSUBSCRIBED, first_name="Bob", source="footer")
		tags.add(a, "new-arrivals")
		tags.add(b, "gunsmith")

		def emails(**kw):
			return {r["email"] for r in api.find_contacts(limit=200, **kw)["contacts"]} & {a, b}

		self.assertEqual(emails(email=a.split("@")[0].upper()), {a})
		self.assertEqual(emails(name="ada"), {a})
		self.assertEqual(emails(status=UNSUBSCRIBED), {b})
		self.assertEqual(emails(source="fluentform"), {a})
		self.assertEqual(emails(tag="new-arrivals"), {a})
		self.assertEqual(emails(tag="gun"), set(), "a tag matches exactly, not inside another")
		out = api.find_contacts(email=a)
		self.assertEqual(out["total"], 1)
		self.assertEqual(out["contacts"][0]["tags"], ["new-arrivals"])

	def test_an_unknown_status_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			api.find_contacts(status="Gone")

	def test_who_may_look(self):
		email = _contact()
		for who, role in ((MARKETER, MARKETING_ROLE), (CLERK, "Sales User")):
			frappe.set_user("Administrator")
			user(who, role)
			_as(who)
			self.assertEqual(api.find_contacts(email=email)["total"], 1, role)
		frappe.set_user("Administrator")
		user(OUTSIDER, "Accounts User")
		_as(OUTSIDER)
		with self.assertRaises(frappe.PermissionError):
			api.find_contacts()

	def test_refused_while_marketing_is_off(self):
		self.set_single(SETTINGS, "enabled", 0)
		with self.assertRaises(frappe.ValidationError) as ctx:
			api.find_contacts()
		self.assertIn("switched off", str(ctx.exception))
		for call in (lambda: api.contact_card(_contact()), lambda: api.list_requests(),
				lambda: api.tag_contact(_contact(), add=["x"]), lambda: api.record_consent(_contact(), "phone"),
				lambda: api.close_request("nothing")):
			with self.assertRaises(frappe.ValidationError):
				call()


class TestContactCard(MarketingTestCase):
	def test_record_tags_events_and_open_requests(self):
		email = _contact()
		tags.add(email, "back-in-stock")
		requests.add(email, requests.BACK_IN_STOCK, sku="X-1", label="Widget")
		closed, _new = requests.add(email, requests.BACK_IN_STOCK, sku="X-2", label="Other")
		requests.close(closed)
		card = api.contact_card(email)
		self.assertEqual(card["contact"]["email"], email)
		self.assertEqual(card["contact"]["status"], SUBSCRIBED)
		self.assertFalse(card["contact"]["suppressed"])
		self.assertEqual(card["contact"]["tags"], ["back-in-stock"])
		self.assertEqual([e["kind"] for e in card["events"]], [SUBSCRIBED])
		self.assertEqual([r["sku"] for r in card["open_requests"]], ["X-1"])

	def test_an_unknown_contact_is_refused(self):
		with self.assertRaises(frappe.DoesNotExistError):
			api.contact_card(address("conn"))


class TestTags(MarketingTestCase):
	def test_a_marketer_adds_and_removes_tags(self):
		email = _contact()
		tags.add(email, "old")
		user(MARKETER, MARKETING_ROLE)
		_as(MARKETER)
		out = api.tag_contact(email, add='["vip", "new-arrivals"]', remove="old")
		self.assertEqual(out, {"contact": email, "added": ["vip", "new-arrivals"], "removed": ["old"],
			"tags": ["vip", "new-arrivals"]})
		self.assertEqual(api.tag_contact(email, add=["vip"])["added"], [], "already on")
		frappe.set_user("Administrator")
		self.assertEqual(tags.contacts_with("vip"), [email], "through Tag Link, as the audience reads it")

	def test_a_tag_that_is_not_a_slug_is_refused_and_nothing_changes(self):
		email = _contact()
		for bad in (["VIP"], ["has space"], ["<b>"], ["a" * 61]):
			with self.assertRaises(frappe.ValidationError):
				api.tag_contact(email, add=["ok"] + bad)
		self.assertEqual(tags.of(email), [])
		with self.assertRaises(frappe.ValidationError):
			api.tag_contact(email)
		with self.assertRaises(frappe.ValidationError):
			api.tag_contact(email, add=["x"], remove=["x"])

	def test_a_sales_user_cannot_tag(self):
		email = _contact()
		user(CLERK, "Sales User")
		_as(CLERK)
		with self.assertRaises(frappe.PermissionError):
			api.tag_contact(email, add=["vip"])


class TestRecordConsent(MarketingTestCase):
	def test_a_marketer_records_a_consent_and_it_is_logged(self):
		email = _contact(NOT_SUBSCRIBED)
		user(MARKETER, MARKETING_ROLE)
		_as(MARKETER)
		out = api.record_consent(email, "in store", "Signed the clipboard")
		frappe.set_user("Administrator")
		self.assertEqual(out, {"contact": email, "status": SUBSCRIBED, "changed": True})
		self.assertEqual(frappe.db.get_value(CONTACT, email, "consent_channel"), "in store")
		last = events_of(email)[-1]
		self.assertEqual(last.kind, SUBSCRIBED)
		for part in ("in store", "Signed the clipboard", MARKETER):
			self.assertIn(part, last.detail)

	def test_already_subscribed_is_no_change(self):
		email = _contact()
		self.assertFalse(api.record_consent(email, "phone")["changed"])

	def test_a_suppressed_contact_is_never_put_back(self):
		for status in (UNSUBSCRIBED, BOUNCED, COMPLAINED):
			email = _contact(status)
			with self.assertRaises(frappe.ValidationError):
				api.record_consent(email, "phone")
			self.assertEqual(status_of(email), status)

	def test_the_channel_is_required(self):
		email = _contact(NOT_SUBSCRIBED)
		with self.assertRaises(frappe.MandatoryError):
			api.record_consent(email, "  ")
		self.assertEqual(status_of(email), NOT_SUBSCRIBED)

	def test_unsubscribe_stays_with_managers(self):
		email = _contact()
		user(MARKETER, MARKETING_ROLE)
		_as(MARKETER)
		with self.assertRaises(frappe.PermissionError):
			api.unsubscribe(email)
		with self.assertRaises(frappe.PermissionError):
			api.resubscribe(email, "because")
		frappe.set_user("Administrator")
		user(MANAGER, "Sales Manager")
		_as(MANAGER)
		self.assertEqual(api.unsubscribe(email)["status"], UNSUBSCRIBED)


class TestRequests(MarketingTestCase):
	def test_list_and_close(self):
		email = _contact()
		name, _new = requests.add(email, requests.MODEL_ALERT, model_key="m1", label="Model One")
		user(MARKETER, MARKETING_ROLE)
		_as(MARKETER)
		self.assertIn(name, [r["name"] for r in api.list_requests(contact=email.upper())])
		self.assertEqual(api.close_request(name), {"request": name, "status": requests.CLOSED, "changed": True})
		self.assertEqual(api.close_request(name)["changed"], False, "closing twice changes nothing")
		self.assertEqual(api.list_requests(contact=email), [])
		self.assertEqual([r["status"] for r in api.list_requests(status="all", contact=email)], [requests.CLOSED])

	def test_a_notified_request_is_not_closed(self):
		email = _contact()
		name, _new = requests.add(email, requests.BACK_IN_STOCK, sku="N-1", status=requests.NOTIFIED)
		self.assertEqual(api.close_request(name)["status"], requests.NOTIFIED)

	def test_bad_filters_and_unknown_requests_are_refused(self):
		with self.assertRaises(frappe.ValidationError):
			api.list_requests(status="Pending")
		with self.assertRaises(frappe.ValidationError):
			api.list_requests(kind="Restock")
		with self.assertRaises(frappe.DoesNotExistError):
			api.close_request("no such request")

	def test_a_sales_user_reads_but_cannot_close(self):
		email = _contact()
		name, _new = requests.add(email, requests.BACK_IN_STOCK, sku="S-1")
		user(CLERK, "Sales User")
		_as(CLERK)
		self.assertEqual(len(api.list_requests(contact=email)), 1)
		with self.assertRaises(frappe.PermissionError):
			api.close_request(name)


class TestEndpointMethods(MarketingTestCase):
	def test_reads_are_get_and_writes_are_post(self):
		for fn, method in ((api.find_contacts, "GET"), (api.contact_card, "GET"), (api.list_requests, "GET"),
				(api.tag_contact, "POST"), (api.record_consent, "POST"), (api.close_request, "POST")):
			self.assertIn(fn, frappe.whitelisted)
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], [method])
