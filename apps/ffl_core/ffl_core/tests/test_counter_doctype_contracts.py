"""Guard: the columns this feature READS must actually exist.

The stubbed harness returns None from every `frappe.db.get_value`, so a query naming
a column that does not exist passes here and raises `OperationalError (1054)` on the
bench. That is exactly how `["license", ...]` on **ATF FFL Record** shipped: the
doctype's licence column is `ffl_number` (its autoname), while `license` is the
OrderPlan / transfer_dealer *contract key* — same concept, different name, and the
harness could not tell them apart.

Rather than assert a hard-coded list (which drifts the moment the code changes), this
AST-reads the field list each function actually passes and validates it against the
doctype JSON in the repo. Cheap, and it tracks the code.
"""
from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from ffl_core.api import manual_order

_ATF_JSON = (Path(manual_order.__file__).resolve().parents[1]
	/ "ffl_core" / "doctype" / "atf_ffl_record" / "atf_ffl_record.json")


def _doctype_fieldnames(path: Path) -> set:
	data = json.loads(path.read_text())
	names = {f.get("fieldname") for f in data.get("fields", []) if f.get("fieldname")}
	names |= {"name", "owner", "creation", "modified"}  # always-present core columns
	return names


def _queried_fields(module_file: str, func_name: str, doctype: str):
	"""The field list `func_name` passes to frappe.db.get_value(doctype, …)."""
	tree = ast.parse(Path(module_file).read_text())
	for node in ast.walk(tree):
		if not (isinstance(node, ast.FunctionDef) and node.name == func_name):
			continue
		for call in ast.walk(node):
			if (isinstance(call, ast.Call)
					and isinstance(call.func, ast.Attribute)
					and call.func.attr == "get_value"
					and call.args
					and isinstance(call.args[0], ast.Constant)
					and call.args[0].value == doctype):
				for arg in call.args[1:]:
					if isinstance(arg, ast.List):
						return [e.value for e in arg.elts
							if isinstance(e, ast.Constant)]
	return None


class AtfFflRecordFieldContractTest(unittest.TestCase):
	def setUp(self):
		self.fieldnames = _doctype_fieldnames(_ATF_JSON)

	def test_the_doctype_json_was_found(self):
		self.assertTrue(self.fieldnames, f"no fields parsed from {_ATF_JSON}")

	def test_licence_column_is_ffl_number_not_license(self):
		# The trap, stated once: reading "license" off this doctype is a 1054.
		self.assertIn("ffl_number", self.fieldnames)
		self.assertNotIn("license", self.fieldnames)

	def test_counter_ffl_block_reads_real_columns(self):
		fields = _queried_fields(
			manual_order.__file__, "_counter_ffl_block", "ATF FFL Record")
		self.assertTrue(fields, "could not find the ATF get_value call")
		for f in fields:
			self.assertIn(f, self.fieldnames,
				f"_counter_ffl_block reads ATF FFL Record.{f}, which does not exist")


if __name__ == "__main__":
	unittest.main()
