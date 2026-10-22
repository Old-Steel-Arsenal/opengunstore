"""Per-Company store branding: the helper, and the receipts/emails that use it.

The point of the whole feature is that a SECOND store's site prints its own
name, logo and address — so every test here runs the same render against two
Companies and asserts they differ in exactly the branded places. The
missing-data cases (no logo / no address / no footer note) get their own
asserts because a receipt that throws, or prints a broken image or an empty
red box, is worse than one with a plain header.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.store_identity import company_brand
from ffl_core.tests.settings_sandbox import SettingsSandbox

# Deliberate test-side literal (not imported from the module): this is the text
# the receipt printed before the footer moved into FFL Settings, and pinning it
# here is what makes "the original store's receipt is unchanged" checkable.
FOOTER = ("ALL SALES ARE FINAL — NO RETURNS.\n"
	"For any product issues, please contact the manufacturer.")


class StoreIdentityBase(SettingsSandbox, FrappeTestCase):
	"""Two Companies on one site: one fully branded, one with nothing filled in."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.branded = cls._company("_Test Branded Store", "TBS")
		cls.bare = cls._company("_Test Bare Store", "TBR")
		frappe.db.set_value("Company", cls.branded, "company_logo", "/files/test-brand-logo.png")
		cls._address(cls.branded, "12 Main Street 40", "Plainsville", "TX", "70001")

	@classmethod
	def _company(cls, name: str, abbr: str) -> str:
		existing = frappe.db.get_value("Company", {"company_name": name}, "name")
		if existing:
			return existing
		return frappe.get_doc({
			"doctype": "Company", "company_name": name, "abbr": abbr,
			"default_currency": "USD", "country": "United States",
		}).insert(ignore_permissions=True).name

	@classmethod
	def _address(cls, company: str, line1: str, city: str,
			state: str = "", pincode: str = "") -> str:
		"""Add a company-linked Address; ERPNext stamps is_your_company_address."""
		return frappe.get_doc({
			"doctype": "Address", "address_title": f"{company} {line1}",
			"address_type": "Office",
			"address_line1": line1, "city": city, "state": state, "pincode": pincode,
			"country": "United States",
			"links": [{"link_doctype": "Company", "link_name": company}],
		}).insert(ignore_permissions=True).name


class TestCompanyBrand(StoreIdentityBase):
	def test_branded_company_returns_name_logo_and_one_line_address(self):
		brand = company_brand(self.branded)
		self.assertEqual(brand["name"], "_Test Branded Store")
		self.assertTrue(brand["logo_url"].endswith("/files/test-brand-logo.png"))
		self.assertTrue(brand["logo_url"].startswith("http"),
			"the receipt PDF renderer needs an absolute URL, not a site-relative path")
		self.assertEqual(brand["address_line"], "12 Main Street 40, Plainsville, TX 70001")

	def test_company_with_no_logo_yields_empty_logo_url_not_a_broken_path(self):
		# An empty string is what lets the print format drop the <img>
		# entirely; a site-relative fallback would render a broken image.
		self.assertEqual(company_brand(self.bare)["logo_url"], "")

	def test_company_with_no_address_yields_empty_address_line(self):
		self.assertEqual(company_brand(self.bare)["address_line"], "")

	def test_footer_note_is_empty_when_the_setting_was_never_filled_in(self):
		self.clear_single("FFL Settings", "receipt_footer_note")
		self.assertEqual(company_brand(self.bare)["footer_note"], "")

	def test_footer_note_comes_from_ffl_settings_when_set(self):
		self.set_single("FFL Settings", "receipt_footer_note", FOOTER)
		self.assertEqual(company_brand(self.branded)["footer_note"], FOOTER)

	# A missing/blank company is what an unsaved doc or a Company-less site
	# hands us; it must degrade, not raise, or the receipt job dies.
	def test_blank_company_degrades_to_empty_values(self):
		for company in ("", None):
			brand = company_brand(company)
			self.assertEqual(brand["name"], "")
			self.assertEqual(brand["logo_url"], "")
			self.assertEqual(brand["address_line"], "")

	def test_partial_address_skips_the_missing_pieces_without_stray_commas(self):
		# Removed again so the "no address" case above stays true whatever order
		# the methods run in.
		addr = self._address(self.bare, "9 Only Street", "Nowheresville")
		self.addCleanup(frappe.delete_doc, "Address", addr, force=True,
			ignore_permissions=True)
		self.assertEqual(company_brand(self.bare)["address_line"],
			"9 Only Street, Nowheresville")

	def test_brand_offers_no_email_key(self):
		# Deliberate: a contact address is never one thing, and an `email` here
		# would be a plausible-looking value for any print format to print —
		# which is how the consignment alias reaches a customer's receipt. The
		# dealer portal reads its own consignment settings' contact_email instead.
		self.assertNotIn("email", company_brand(self.branded))


class TestCompanyAddressIsDeterministic(StoreIdentityBase):
	"""Which address a receipt prints cannot depend on row order."""

	def _second_address(self, company: str, line1: str, primary: int = 0) -> str:
		name = self._address(company, line1, "Plainsville", "TX", "70001")
		frappe.db.set_value("Address", name, "is_primary_address", primary,
			update_modified=False)
		self.addCleanup(frappe.delete_doc, "Address", name, force=True,
			ignore_permissions=True)
		return name

	def test_the_primary_address_wins_over_the_others(self):
		from ffl_core.store_identity import company_address

		self._second_address(self.branded, "77 Second Street", primary=1)
		self.assertEqual(
			frappe.db.get_value("Address", company_address(self.branded), "address_line1"),
			"77 Second Street")

	def test_repeated_calls_agree_when_nothing_is_primary(self):
		from ffl_core.store_identity import company_address

		self._second_address(self.branded, "77 Second Street")
		self._second_address(self.branded, "88 Third Street")
		picks = {company_address(self.branded) for _ in range(5)}
		self.assertEqual(len(picks), 1,
			"an unordered query would let the receipt header change between prints")

	def test_an_address_not_marked_as_the_companys_own_is_ignored(self):
		from ffl_core.store_identity import company_address

		name = self._second_address(self.branded, "99 Not Ours", primary=1)
		frappe.db.set_value("Address", name, "is_your_company_address", 0,
			update_modified=False)
		self.assertNotEqual(company_address(self.branded), name)

	def test_a_disabled_address_is_ignored(self):
		from ffl_core.store_identity import company_address

		name = self._second_address(self.branded, "55 Closed Down", primary=1)
		frappe.db.set_value("Address", name, "disabled", 1, update_modified=False)
		self.assertNotEqual(company_address(self.branded), name)


class _StubDoc:
	"""Just enough invoice for the receipt templates, minus a submitted document.

	The templates only ever read fields and call `get_formatted`; building a real
	Sales Invoice (customer, item, warehouse, tax template, stock) to assert on a
	header and a footer would test everything except the thing that changed.

	A plain object rather than `frappe._dict` on purpose: printview hands the
	template a Document, where `doc.items` is the child table — on a dict it
	would resolve to `dict.items` and the row loop would render nothing.
	"""

	def __init__(self, **fields):
		self.__dict__.update(fields)

	def get(self, fieldname, default=None):
		return self.__dict__.get(fieldname, default)

	def get_formatted(self, fieldname):
		return f"$ {self.get(fieldname) or 0}"


def _invoice(company: str) -> _StubDoc:
	return _StubDoc(
		company=company, name="ACC-SINV-TEST-0001", posting_date="2026-09-01",
		customer_name="A Buyer", net_total=100, grand_total=108.25,
		total_taxes_and_charges=8.25,
		taxes=[_StubDoc(description="Sales Tax", tax_amount=8.25)],
		card_charges=[],
		items=[_StubDoc(item_name="A Rifle", item_code="RIF-1", serial_no="SN1",
			qty=1, rate=100, amount=100)],
	)


class TestReceiptRendering(StoreIdentityBase):
	"""The two receipt Print Formats, rendered for two stores."""

	FORMATS = ("Sales Receipt", "POS Receipt")

	def _render(self, print_format: str, company: str) -> str:
		# The INSTALLED fixture, not the file — this also pins that the fixture
		# actually synced onto the site.
		html = frappe.db.get_value("Print Format", print_format, "html")
		self.assertTrue(html, f"{print_format} is not installed on this site")
		return frappe.render_template(html, {"doc": _invoice(company)})

	def test_branded_store_prints_its_own_logo_address_and_footer(self):
		self.set_single("FFL Settings", "receipt_footer_note", FOOTER)
		for fmt in self.FORMATS:
			with self.subTest(fmt):
				html = self._render(fmt, self.branded)
				self.assertIn('src="http', html)
				self.assertIn("/files/test-brand-logo.png", html)
				self.assertIn("12 Main Street 40, Plainsville, TX 70001", html)
				self.assertIn("_Test Branded Store", html)
				# The two footer lines survive as two lines, not one run-on.
				self.assertIn("ALL SALES ARE FINAL — NO RETURNS.<br>", html)
				self.assertIn("please contact the manufacturer.", html)

	def test_a_second_store_prints_none_of_the_first_stores_branding(self):
		# The whole point of the run: same site, same format, different company.
		self.set_single("FFL Settings", "receipt_footer_note", FOOTER)
		for fmt in self.FORMATS:
			with self.subTest(fmt):
				html = self._render(fmt, self.bare)
				self.assertIn("_Test Bare Store", html)
				for residue in ("Old Steel", "osa-logo", "12 Main Street 40", "test-brand-logo"):
					self.assertNotIn(residue, html,
						f"{fmt} leaked {residue!r} into another store's receipt")

	def test_the_receipt_still_starts_at_the_document_div(self):
		# The `{%- set brand -%}` trim is load-bearing: without it the tag leaves
		# a blank first line, and the original store's receipt stops being
		# byte-identical to the one that shipped before this branch.
		for fmt in self.FORMATS:
			with self.subTest(fmt):
				self.assertTrue(self._render(fmt, self.bare).startswith("<div style="))

	def test_no_logo_renders_no_img_tag_at_all(self):
		# An <img> with an empty or missing src is a broken-image icon on
		# every printed receipt, which is worse than no logo.
		for fmt in self.FORMATS:
			with self.subTest(fmt):
				self.assertNotIn("<img", self._render(fmt, self.bare))

	def test_no_company_address_renders_no_address_line(self):
		for fmt in self.FORMATS:
			with self.subTest(fmt):
				html = self._render(fmt, self.bare)
				self.assertNotIn("font-size:11px;color:#777;margin-top:4px;", html)

	def test_empty_footer_note_renders_no_footer_block(self):
		# No empty red box, no orphaned <br>, no stray punctuation.
		self.clear_single("FFL Settings", "receipt_footer_note")
		for fmt in self.FORMATS:
			with self.subTest(fmt):
				html = self._render(fmt, self.branded)
				self.assertNotIn("#b00020", html)
				self.assertIn("Thank you for your business.", html)

	def test_a_store_with_nothing_configured_still_renders(self):
		# Header degrades to the store name alone; the rest of the receipt is
		# untouched. A fresh site must be able to print a sale on day one.
		self.clear_single("FFL Settings", "receipt_footer_note")
		for fmt in self.FORMATS:
			with self.subTest(fmt):
				html = self._render(fmt, self.bare)
				self.assertIn("_Test Bare Store", html)
				self.assertIn("A Rifle", html)
				self.assertIn("Total", html)


class TestEmailTemplateRendering(StoreIdentityBase):
	"""Both customer emails name whichever store made the sale."""

	def _render_template(self, name: str, context: dict) -> tuple[str, str]:
		template = frappe.get_doc("Email Template", name)
		body = template.response_html if template.use_html else template.response
		return (frappe.render_template(template.subject, context),
			frappe.render_template(body, context))

	def test_sale_receipt_email_names_the_selling_company(self):
		for company, expected in ((self.branded, "_Test Branded Store"),
				(self.bare, "_Test Bare Store")):
			with self.subTest(company):
				subject, body = self._render_template(
					"Sale Receipt", {"doc": _invoice(company), "customer_name": "A Buyer"})
				self.assertIn(expected, subject)
				self.assertIn(expected, body)
				self.assertNotIn("Old Steel", subject + body)

	def test_nics_pickup_email_names_the_selling_company(self):
		for company, expected in ((self.branded, "_Test Branded Store"),
				(self.bare, "_Test Bare Store")):
			with self.subTest(company):
				subject, body = self._render_template("NICS Cleared Pickup", {
					"customer_name": "A Buyer", "invoice": "PINV-1", "company": company})
				self.assertIn(expected, body)
				self.assertNotIn("Old Steel", subject + body)


class TestNoStoreLiteralInShippedSource(FrappeTestCase):
	"""The regression guard: a store's name/address cannot come back as a literal.

	Source-level, in the house style of test_consignment_out_front_end_invariants
	— the files below are the ones a customer or a partner dealer reads, and a
	literal reintroduced into any of them is invisible to every behavioural test
	until the second store's site prints the first store's name again.

	Deliberately scoped to those files. Out-of-scope literals live in developer
	docstrings and in integration defaults (the Data Service endpoint description,
	the GunBroker rate comment) and are a separate piece of work.
	"""

	RESIDUE = ("Old Steel", "osa-logo")

	GUARDED = (
		("ffl_core", "fixtures", "print_format.json"),
		("ffl_core", "fixtures", "email_template.json"),
		("ffl_integrations", "fixtures", "email_template.json"),
	)

	def test_no_guarded_file_carries_a_store_literal(self):
		import os

		for app, *rest in self.GUARDED:
			path = frappe.get_app_path(app, *rest)
			with self.subTest(os.path.basename(path)):
				with open(path) as fh:
					source = fh.read()
				for residue in self.RESIDUE:
					self.assertNotIn(residue, source,
						f"{path} hardcodes {residue!r} — read it off the Company "
						"instead (ffl_core.store_identity.company_brand)")

	def test_the_letterhead_fixture_is_gone_and_unreferenced(self):
		# It shipped one store's letterhead and every migrate rewrote it, so a
		# second store could never keep its own default.
		import os

		self.assertFalse(os.path.exists(
			frappe.get_app_path("ffl_core", "fixtures", "letter_head.json")))
		self.assertNotIn("Letter Head", str(frappe.get_hooks("fixtures", app_name="ffl_core")))

	def test_no_script_block_is_closed_early_by_its_own_text(self):
		"""Tokenize every shipped page; no script element may end early.

		The HTML tokenizer does not read JavaScript: it ends a script element
		at the first `</script` it sees — inside a string or a `//` comment
		just the same. That shipped for real: a comment on the
		`| tojson` line spelled the tag out, the browser closed the block six
		lines in, and the dealer got the rest of the page's source rendered as
		body text with every button dead.

		This runs a real tokenizer rather than counting tags, because counting
		is the same kind of reading that let the bug through. html.parser
		implements the HTML5 "script data" state, so it ends a script exactly
		where a browser would. Reading the template IS reading what the browser
		sees: `tojson` escapes `<` and `>`, so only the page's own literal text
		can close a block. Code that genuinely needs the characters writes
		`<\\/script`, which is not a closer to the tokenizer either.
		"""
		import re
		from html.parser import HTMLParser

		# Only consulted for text the tokenizer reports OUTSIDE a script, where
		# anything code-shaped means the element ended somewhere it should not.
		js_tell = re.compile(r"\bfunction\s*\(|=>|\bvar\s+\w+\s*=|\}\);")

		class Tokenized(HTMLParser):
			def __init__(self):
				super().__init__(convert_charrefs=False)
				self.spans = []      # (start_line, end_line) per script element
				self.leaked = []     # (line, snippet) of JS seen as body text
				self._open = None

			def handle_starttag(self, tag, attrs):
				if tag == "script":
					self._open = self.getpos()[0]

			def handle_endtag(self, tag):
				if tag == "script" and self._open is not None:
					self.spans.append((self._open, self.getpos()[0]))
					self._open = None

			def handle_data(self, data):
				if self._open is None and js_tell.search(data):
					self.leaked.append((self.getpos()[0], data.strip()[:60]))

		for app, *rest in self.GUARDED:
			path = frappe.get_app_path(app, *rest)
			if not path.endswith(".html"):
				continue
			with self.subTest(path):
				with open(path) as fh:
					source = fh.read()
				page = Tokenized()
				page.feed(source)
				page.close()

				lines = source.splitlines()
				opened = [i + 1 for i, ln in enumerate(lines) if "<script" in ln]
				closed = [i + 1 for i, ln in enumerate(lines) if "</script" in ln]
				ended = sorted({end for _, end in page.spans})

				self.assertEqual(len(page.spans), len(opened),
					f"{path}: the tokenizer found {len(page.spans)} script "
					f"element(s) but the source opens {len(opened)}")
				# The assertion that actually catches it. "Each element ends
				# on SOME line the source wrote a closer on" does NOT: line 450
				# was a closer, just the wrong one, and the element count stayed
				# 1 == 1. Every closer the source wrote must be one the
				# tokenizer reached.
				self.assertEqual(ended, closed,
					f"{path}: the tokenizer ended script elements at {ended} but "
					f"the source writes closing tags at {closed} — everything "
					"after the first one renders as page text")
				# ...and the symptom itself, stated directly.
				self.assertEqual(page.leaked, [],
					f"{path}: JavaScript is reaching the page as body text: "
					f"{page.leaked[:3]}")
