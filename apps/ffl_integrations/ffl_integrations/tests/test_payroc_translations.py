"""Payroc money-path messages: English in the source, Chinese from zh.csv.

The refund, void and settlement messages used to be written in Chinese in
payroc/api.py. The source is English now; a user whose language is Chinese must
still read exactly the Chinese the code used to throw, so the zh.csv rows carry
the original text verbatim. This pins both halves: no CJK left in the source
strings, and every source string has a row.
"""
from __future__ import annotations

import ast
import csv
import os
import re

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.payments import api as payroc_api

CJK = re.compile(r"[一-鿿　-〿＀-￯]")

# Three messages as they were thrown before the source went English.
ORIGINAL_ZH = {
	"Refunds require manager permission (Sales Manager).":
		"退款需经理权限(Sales Manager)。",
	"Return invoice {0} has already been refunded ({1}) and cannot be refunded twice.":
		"退货单 {0} 已退过款({1}),不能重复退。",
	"Refund refused by the gateway ({0}).":
		"退款被网关拒绝({0})。",
}


def _zh_rows() -> dict[str, str]:
	path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "translations", "zh.csv")
	with open(path, encoding="utf-8") as fh:
		return {row[0]: row[1] for row in csv.reader(fh) if len(row) == 2}


def _translated_literals() -> list[str]:
	tree = ast.parse(open(payroc_api.__file__, encoding="utf-8").read())
	found = []
	for node in ast.walk(tree):
		if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_"
				and node.args and isinstance(node.args[0], ast.Constant)
				and isinstance(node.args[0].value, str)):
			found.append(node.args[0].value)
	return found


class PayrocMessagesAreEnglishSource(FrappeTestCase):
	def test_no_chinese_is_left_in_the_source_strings(self):
		src = open(payroc_api.__file__, encoding="utf-8").read()
		self.assertIsNone(CJK.search(src), "payroc/api.py still holds a Chinese character")

	def test_every_cjk_row_in_zh_csv_belongs_to_an_english_key(self):
		rows = _zh_rows()
		for en, zh in ORIGINAL_ZH.items():
			self.assertEqual(rows.get(en), zh, en)
		for en in rows:
			self.assertIsNone(CJK.search(en), f"zh.csv key is Chinese: {en!r}")

	def test_a_translated_message_keeps_its_placeholders_in_zh(self):
		rows = _zh_rows()
		for en in _translated_literals():
			if en in rows:
				self.assertEqual(sorted(re.findall(r"\{\d*\}", en)),
					sorted(re.findall(r"\{\d*\}", rows[en])), en)
				self.assertRegex(rows[en], CJK, f"zh row has no Chinese: {en!r}")

	def test_zh_users_read_the_original_chinese(self):
		from frappe.translate import clear_cache

		clear_cache()
		for en, zh in ORIGINAL_ZH.items():
			self.assertEqual(frappe._(en, lang="zh"), zh)
			self.assertEqual(frappe._(en, lang="en"), en)
