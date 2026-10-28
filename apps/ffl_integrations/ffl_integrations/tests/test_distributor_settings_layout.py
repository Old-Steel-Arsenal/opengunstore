"""RSR Settings / Sports South Settings / Lipseys Settings: one tab layout
(Account / Ordering / Freight, and Sports South's Status), same fields as before.
Lipseys Settings has no Freight tab (Lipsey's publishes no freight table; a
drop-ship order's answer carries its freight) and a Status tab for its shipping
poll.

Read off disk, no site. It pins what the re-layout may and may not change:

1. **Data fields are untouched.** MCP `get_settings` / `update_settings`, both
   providers and the docs address these pages by fieldname, so the fields below
   are exactly the develop (065ba286) set. Only layout breaks (Tab / Section /
   Column) are free to change: they hold no value.
2. **Every field sits on its tab** and the tabs read Account, Ordering, Freight
   (and Status on Sports South, its credit poll's), with the same tab fieldnames
   on both pages (the Distributors hub card links to two of them). RSR's Status
   tab held only its per-house catalog cursor, gone since the listing sync reads
   the product change feed.
3. **The Settings page standard**: one sentence per description, Test Connection
   stays the primary toolbar button, the endpoint URL sits in a collapsed
   Advanced section.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import ffl_integrations

_DOCTYPES = Path(ffl_integrations.__file__).resolve().parent / "ffl_integrations" / "doctype"
_LAYOUT = frozenset({"Tab Break", "Section Break", "Column Break"})

# (fieldname, label). The Distributors hub card links to tab_account and
# tab_freight by fieldname (distributor/hub.py SUMMARY_TABS), on both pages.
_SHARED_TABS = [("tab_account", "Account"), ("tab_ordering", "Ordering"),
	("tab_freight", "Freight")]
TABS = {"rsr_settings": _SHARED_TABS,
	"sports_south_settings": [*_SHARED_TABS, ("tab_status", "Status")],
	"lipseys_settings": [("tab_account", "Account"), ("tab_ordering", "Ordering"),
		("tab_freight", "Freight"), ("tab_status", "Status")]}

# Tab label -> the develop data fields that belong on it.
EXPECTED = {
	"rsr_settings": {
		# + the local catalog's FTPS feed login (local catalog L2)
		"Account": {"api_enabled", "dealer_account", "dealer_password",
			"fulfillment_account", "fulfillment_password", "api_base_url",
			"ftp_host", "ftp_port", "key_dealer", "ftp_username", "ftp_password",
			"ftps_skip_verify"},
		"Ordering": {"pos_indicator", "order_email", "hold_escalate_days"},
		"Freight": {"store_free_freight", "free_freight_threshold", "rate_ground",
			"rate_2day", "fee_signature", "freight_variance_alert"},
	},
	"sports_south_settings": {
		"Account": {"ordering_enabled", "sandbox_mode", "customer_number", "username",
			"password", "base_url"},
		"Ordering": {"auto_email_ffl", "fulfillment_email", "order_email_cc", "sales_message",
			"ship_via_ground", "ship_via_2day", "accessory_ground_economy",
			"firearm_signature", "firearm_adult_signature", "firearm_insurance",
			"other_signature", "other_adult_signature", "other_insurance",
			"price_drift_alert_pct", "hold_escalate_days",
			"store_ship_name", "store_ship_attn", "store_ship_line1", "store_ship_line2",
			"store_ship_city", "store_ship_state", "store_ship_zip", "store_ship_phone"},
		"Freight": {"rate_handgun_2day", "rate_long_gun_ground", "rate_accessory_ground",
			"rate_accessory_economy", "rate_2day_air", "rate_next_day",
			"fee_signature", "fee_adult_signature", "fee_insurance_pct", "fee_insurance_min",
			"store_free_handgun_qty", "store_free_handgun_amount",
			"store_free_other_amount", "store_other_rate", "freight_variance_alert"},
		"Status": {"credits_polled_through", "last_credits_poll_at", "last_error"},
	},
	"lipseys_settings": {
		"Account": {"ordering_enabled", "customer_number", "dealer_email", "dealer_password",
			"dropship_email", "dropship_password", "base_url"},
		"Ordering": {"disable_email", "dropship_overnight"},
		"Freight": {"rate_dropship_firearm", "rate_dropship_accessory"},
		"Status": {"shipping_polled_through", "dropship_shipping_polled_through",
			"last_shipping_poll_at", "last_error"},
	},
}
MASTER_SWITCH = {"rsr_settings": "api_enabled", "sports_south_settings": "ordering_enabled",
	"lipseys_settings": "ordering_enabled"}
ENDPOINT = {"rsr_settings": "api_base_url", "sports_south_settings": "base_url",
	"lipseys_settings": "base_url"}


def _schema(slug: str) -> dict:
	return json.loads((_DOCTYPES / slug / f"{slug}.json").read_text(encoding="utf-8"))


def _walk(slug: str):
	"""(field, tab break, section break) for every field, in form order."""
	schema = _schema(slug)
	by_name = {f["fieldname"]: f for f in schema["fields"]}
	tab = section = None
	for name in schema["field_order"]:
		f = by_name[name]
		if f["fieldtype"] == "Tab Break":
			tab, section = f, None
		elif f["fieldtype"] == "Section Break":
			section = f
		yield f, tab, section


class FourTabLayout(unittest.TestCase):
	def test_field_order_lists_every_field_once(self):
		for slug in EXPECTED:
			with self.subTest(slug):
				schema = _schema(slug)
				self.assertEqual(schema["field_order"], [f["fieldname"] for f in schema["fields"]])

	def test_tabs_read_account_ordering_freight_status(self):
		for slug in EXPECTED:
			with self.subTest(slug):
				tabs = [(f["fieldname"], f["label"]) for f in _schema(slug)["fields"]
					if f["fieldtype"] == "Tab Break"]
				self.assertEqual(tabs, TABS[slug])

	def test_every_data_field_is_on_its_tab_and_none_was_added_or_dropped(self):
		for slug, expected in EXPECTED.items():
			with self.subTest(slug):
				placed: dict = {}
				for f, tab, _section in _walk(slug):
					if f["fieldtype"] not in _LAYOUT:
						self.assertIsNotNone(tab, f"{f['fieldname']} sits above the first tab")
						placed.setdefault(tab["label"], set()).add(f["fieldname"])
				self.assertEqual(placed, expected)

	def test_the_master_switch_reads_allow_live_orders(self):
		for slug, field in MASTER_SWITCH.items():
			with self.subTest(slug):
				f = next(f for f in _schema(slug)["fields"] if f["fieldname"] == field)
				self.assertEqual(f["label"], "Allow live orders")

	def test_endpoint_url_is_in_a_collapsed_advanced_section_on_account(self):
		for slug, field in ENDPOINT.items():
			with self.subTest(slug):
				_f, tab, section = next(w for w in _walk(slug) if w[0]["fieldname"] == field)
				self.assertEqual(tab["label"], "Account")
				self.assertEqual(section["label"], "Advanced")
				self.assertEqual(section.get("collapsible"), 1)

	def test_ss_ship_to_fields_form_one_override_section(self):
		sections = {section["fieldname"] for f, _tab, section in _walk("sports_south_settings")
			if f["fieldname"].startswith("store_ship_")}
		self.assertEqual(len(sections), 1, sections)
		section = next(f for f in _schema("sports_south_settings")["fields"]
			if f["fieldname"] in sections)
		self.assertEqual(section["label"], "Ship-to address override")
		self.assertIn("leave blank to use the company address", section["description"].lower())

	def test_every_description_is_one_sentence(self):
		long = []
		for slug in EXPECTED:
			for f in _schema(slug)["fields"]:
				text = (f.get("description") or "").strip()
				# A sentence ends in . ! or ? before a space or the end: "$7.95" and
				# "test.rsrgroup.com" do not count.
				if len(re.findall(r"[.!?](?=\s|$)", text)) > 1:
					long.append(f"{slug}.{f['fieldname']}: {text}")
		self.assertEqual(long, [], "move the reasoning to docs/field-notes.md:\n" + "\n".join(long))

	def test_test_connection_stays_the_primary_button(self):
		for slug in EXPECTED:
			with self.subTest(slug):
				js = (_DOCTYPES / slug / f"{slug}.js").read_text(encoding="utf-8")
				self.assertIn("add_custom_button(__('Test Connection')", js)
				self.assertIn(".addClass('btn-primary')", js)


if __name__ == "__main__":
	unittest.main()
