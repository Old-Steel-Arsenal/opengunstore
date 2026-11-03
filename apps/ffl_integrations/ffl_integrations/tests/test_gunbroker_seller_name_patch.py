"""The upgrade that takes the Seller name off the GunBroker Settings form.

A live store must come out of `bench migrate` still sending the registered name.
Site config is written to a scratch file so the real site_config.json is never
touched; ``frappe.conf`` is restored on exit.
"""
from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.patches.move_gunbroker_seller_name_to_site_config import (
	CONF_KEY,
	FIELD,
	SETTINGS,
	execute,
)


def _on_form(value: str) -> None:
	frappe.db.delete("Singles", {"doctype": SETTINGS, "field": FIELD})
	frappe.db.sql("insert into `tabSingles` (doctype, field, value) values (%s, %s, %s)",
		(SETTINGS, FIELD, value))


class TheSellerNameMovesOffTheForm(FrappeTestCase):
	def setUp(self):
		fd, self.path = tempfile.mkstemp(suffix=".json")
		os.write(fd, b"{}")
		os.close(fd)
		self.addCleanup(os.remove, self.path)
		conf = patch.dict(frappe.local.conf)
		conf.start()
		self.addCleanup(conf.stop)
		frappe.local.conf.pop(CONF_KEY, None)

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def _written(self) -> dict:
		with open(self.path) as f:
			return json.load(f)

	def _left_on_form(self) -> bool:
		return bool(frappe.db.exists("Singles", {"doctype": SETTINGS, "field": FIELD}))

	def test_the_form_value_lands_in_site_config_and_leaves_the_form(self):
		_on_form("  Registered Seller LLC ")
		self.assertEqual(execute(site_config_path=self.path), "moved")
		self.assertEqual(self._written(), {CONF_KEY: "Registered Seller LLC"})
		self.assertFalse(self._left_on_form())

	def test_an_operator_set_site_config_wins_over_the_form(self):
		frappe.local.conf[CONF_KEY] = "Operator Set"
		_on_form("Stale Form Name")
		self.assertEqual(execute(site_config_path=self.path), "kept")
		self.assertEqual(self._written(), {})
		self.assertFalse(self._left_on_form())

	def test_a_blank_form_writes_nothing(self):
		_on_form("")
		self.assertEqual(execute(site_config_path=self.path), "absent")
		self.assertEqual(self._written(), {})
		self.assertFalse(self._left_on_form())

	def test_running_it_again_is_a_no_op(self):
		_on_form("Registered Seller LLC")
		execute(site_config_path=self.path)
		self.assertEqual(execute(site_config_path=self.path), "absent")
		self.assertEqual(self._written(), {CONF_KEY: "Registered Seller LLC"})
