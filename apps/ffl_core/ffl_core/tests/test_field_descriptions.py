"""What a counter clerk reads on a form must be about the shop, not about us.

A custom field's ``description`` renders as grey help text under the input, to
whoever has the form open — which at this store is a clerk with a customer at
the counter. Naming a function, a module, a review ticket or a private
idempotency key there tells them nothing they can act on and tells them the
software is talking to somebody else.

Two things kept letting that back in, and both are pinned here:

* ``setup/consignment_out.py`` re-creates its six fields with
  ``create_custom_fields`` at ``after_migrate``, which runs AFTER the fixtures
  are installed and overwrites them. Clean text in
  ``fixtures/custom_field.json`` therefore lost to whatever the setup module
  said, silently, on every migrate. They must be byte-identical.
* Nothing checked the wording itself.

Scope is every description that has been cleaned: the outbound-consignment
fields, ``Serial No.sell_price``, and the money-fact and pickup fields on POS
Invoice. It is still a list rather than a sweep of the whole fixture, because a
blanket rule would fail on wording nobody has looked at yet and would then be
switched off. Add the field here when you clean it.
"""
from __future__ import annotations

import json
import pathlib
import re
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase


# Shapes that only ever appear when implementation has leaked into help text.
# Every one of these was verified against the descriptions as they stood before
# 2026-09-03: between them they catch all six, which is the bar a guard like
# this has to clear to be worth having.
_DEVELOPER_SHAPES = (
	(re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+){2,}\b"), "an identifier"),
	(re.compile(r"\b_[a-z][a-z0-9_]{2,}\b"), "a private name"),
	(re.compile(r"\b[a-z_]{3,}\.[_a-z][a-z_]{2,}\b"), "a dotted module path"),
	(re.compile(r"[a-z_]{3,}\("), "a function call"),
	(re.compile(r"\.py\b"), "a source filename"),
	(re.compile(r"review-backend|\breview r\d|\b[MNmn]-\d\b"), "a review ticket"),
	(re.compile(r"\bno_copy\b|\bfor_update\b|\bdocstatus\b|\bget_mapped_doc\b"),
		"a framework internal"),
	(re.compile(r"idempoten|exclusion key|\bflag that\b"), "internal jargon"),
)


def _fixture_rows(app: str = "ffl_core") -> dict[tuple[str, str], dict]:
	path = frappe.get_app_path(app, "fixtures", "custom_field.json")
	with open(path, encoding="utf-8") as fh:
		return {(r["dt"], r["fieldname"]): r for r in json.load(fh)}


# Fields whose help text has been rewritten for the person reading the form.
# Each entry is (app, doctype, fieldname). The reasoning that used to sit in
# these descriptions is in docs/field-notes.md, verbatim.
_CLEANED = (
	("ffl_core", "POS Invoice", "card_unique_reference"),
	("ffl_core", "POS Invoice", "card_refunded_amount"),
	("ffl_core", "POS Invoice", "card_attempt"),
	("ffl_core", "POS Invoice", "custom_nics_cleared_notified"),
	("ffl_woo_sync", "POS Invoice", "woo_online_order"),
	("ffl_integrations", "POS Invoice", "card_charges"),
)

# Read-only fields the system fills in, whose label says it all: the 2026-10-30
# layout pass left them with no help text on purpose. Blank is allowed for these
# only; if help text comes back it is held to the same bar as the rest.
_LABEL_ONLY = {
	("POS Invoice", "card_refunded_amount"),
	("POS Invoice", "card_attempt"),
	("POS Invoice", "custom_nics_cleared_notified"),
}


class CleanedFieldDescriptions(FrappeTestCase):
	"""The same bar as the consignment fields, applied to the money-fact and
	pickup fields on POS Invoice. A cashier has this form open with a customer
	in front of them; naming a module or a review ticket there is noise at the
	worst moment."""

	def test_none_of_them_names_an_implementation_detail(self):
		for app, doctype, fieldname in _CLEANED:
			row = _fixture_rows(app).get((doctype, fieldname))
			self.assertIsNotNone(row, f"{app}: {doctype}.{fieldname} is missing")
			description = row.get("description") or ""
			for pattern, what in _DEVELOPER_SHAPES:
				found = pattern.search(description)
				self.assertIsNone(
					found,
					f"{doctype}.{fieldname} names {what} "
					f"({found.group(0) if found else ''!r}) in text a cashier "
					"reads: put it in docs/field-notes.md instead")

	def test_each_one_still_says_something(self):
		for app, doctype, fieldname in _CLEANED:
			description = (_fixture_rows(app)[(doctype, fieldname)]
				.get("description") or "").strip()
			if not description and (doctype, fieldname) in _LABEL_ONLY:
				continue
			self.assertGreater(
				len(description.split()), 6,
				f"{doctype}.{fieldname} was emptied rather than rewritten")

	def test_the_removed_reasoning_was_kept(self):
		"""Cleaning the form must not lose the engineering note. Every field
		above has a heading in the field notes.

		The notes live at the repo root, which is above the bench, so this walks
		up until it finds them and skips when it cannot — an app installed on
		its own, without its repo, has no docs/ to check."""
		notes = None
		for parent in pathlib.Path(frappe.get_app_path("ffl_core")).parents:
			candidate = parent / "docs" / "field-notes.md"
			if candidate.is_file():
				notes = candidate
				break
		if notes is None:
			self.skipTest("docs/field-notes.md is not on disk (app without repo)")
		text = notes.read_text(encoding="utf-8")
		for heading in ("Card Unique Reference", "Card Attempt",
				"Card Refunded Amount", "Card Charges",
				"NICS Cleared Notified", "Woo Online Order"):
			self.assertIn(f"### {heading}", text,
				f"docs/field-notes.md has no entry for {heading}")


class SellPriceDescriptionMatchesThePicker(FrappeTestCase):
	"""The description said "POS picks the lowest-priced in-stock unit by
	default". It does not. The picker lists serials cheapest first, but it only
	CHOOSES one when the choice cannot change the price: a uniformly-priced
	item, or a single unit in stock. Anything else waits for the clerk, because
	on a per-gun-priced item the serial is the price and the condition varies.

	A clerk who believed the old sentence would have expected the cheapest gun
	to be on the ticket already."""

	FIELD = ("Serial No", "sell_price")

	def _description(self) -> str:
		return _fixture_rows()[self.FIELD].get("description") or ""

	def test_it_does_not_claim_an_automatic_cheapest_pick(self):
		self.assertNotIn("lowest", self._description().lower())

	def test_it_states_the_two_conditions_that_do_auto_pick(self):
		description = self._description().lower()
		self.assertIn("same price", description)
		self.assertIn("one is left", description)

	def test_the_picker_still_works_the_way_the_description_says(self):
		"""The anti-drift half. If this fails because the picker changed, the
		description above is now wrong too — fix both."""
		path = frappe.get_app_path("ffl_core", "public", "js", "ffl_core.bundle.js")
		with open(path, encoding="utf-8") as fh:
			source = fh.read()
		self.assertIn(
			"((summary && summary.uniform) || serials.length === 1)", source,
			"the POS auto-pick condition moved; re-read it and update the "
			"Serial No.sell_price description to match")


if __name__ == "__main__":
	unittest.main()
