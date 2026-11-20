"""Form 4473 Records — every completed 4473 on file (the Form 4473 Log), with
what became of its order and its signed PDF."""
from __future__ import annotations

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.ffl_core.doctype.form_4473_log.form_4473_log import record_4473
from ffl_core.tests.test_pending_4473_orders import Pending4473Fixtures


class Form4473Records(Pending4473Fixtures, FrappeTestCase):
	def setUp(self):
		frappe.db.delete("Form 4473 Log", {"serials": ("like", "R4473-%")})

	def _run(self, **filters):
		from ffl_core.ffl_core.report.form_4473_records.form_4473_records import execute
		return execute(filters)

	def _row(self, log, **filters):
		return next((r for r in self._run(**filters)[1] if r["log"] == log), None)

	def test_newest_first_with_the_logged_fields(self):
		old = record_4473(form4473_id="R-OLD", serials=["R4473-S1"], nics_status="Proceed",
			completed_on="2026-01-01 10:00:00")
		new = record_4473(form4473_id="R-NEW", serials=["R4473-S2"], nics_status="Denied",
			completed_on="2026-02-01 10:00:00")
		logs = [r["log"] for r in self._run()[1] if r["log"] in (old, new)]
		self.assertEqual(logs, [new, old])
		row = self._row(new)
		self.assertEqual((row["serials"], row["nics_status"], row["source"], row["has_pdf"]),
			("R4473-S2", "Denied", "FastBound", 1))

	def test_order_status_follows_the_order(self):
		name = self._draft_invoice("P4473-GUN", serial="R4473-OS", form_4473="Completed")
		frappe.db.set_value("POS Invoice", name, "fastbound_disposition_id", "R-DISP-NOW")
		current = record_4473(form4473_id="R-CUR", pos_invoices=[name], serials=["R4473-C"],
			fastbound_disposition_id="R-DISP-NOW")
		redone = record_4473(form4473_id="R-REDO", pos_invoices=[name], serials=["R4473-R"],
			fastbound_disposition_id="R-DISP-OLD")
		deleted = record_4473(form4473_id="R-DEL", pos_invoices=["ACC-PSINV-GONE-1"],
			serials=["R4473-D"])
		orphan = record_4473(form4473_id="R-ORPH", serials=["R4473-O"])
		self.assertEqual(self._row(current)["order_status"], "Draft")
		self.assertEqual(self._row(redone)["order_status"], "Redone")
		self.assertEqual(self._row(deleted)["order_status"], "Deleted")
		self.assertEqual(self._row(orphan)["order_status"], "No POS order")

	def test_pdf_is_offered_only_where_there_can_be_one(self):
		paper = record_4473(pos_invoices=["ACC-PSINV-P-1"], serials=["R4473-P"],
			source="Paper / override")
		gone = record_4473(form4473_id="R-GONE", serials=["R4473-G"])
		frappe.db.set_value("Form 4473 Log", gone, "pdf_status", "Not Found")
		self.assertEqual((self._row(paper)["has_pdf"], self._row(paper)["pdf"]), (0, ""))
		# The sweep gave up, but the viewer still asks FastBound live.
		self.assertEqual(self._row(gone)["has_pdf"], 1)

	def test_filters(self):
		a = record_4473(form4473_id="R-FA", serials=["R4473-ABC123"], nics_status="Denied",
			completed_on="2026-03-10 09:00:00")
		b = record_4473(pos_invoices=["ACC-PSINV-FB-1"], serials=["R4473-XYZ999"],
			source="Paper / override", nics_status="Proceed", completed_on="2026-03-20 09:00:00")
		logs = lambda **f: {r["log"] for r in self._run(**f)[1]}  # noqa: E731
		self.assertIn(a, logs(serial="abc12"))
		self.assertNotIn(b, logs(serial="abc12"))
		self.assertEqual({a, b} & logs(source="Paper / override"), {b})
		self.assertEqual({a, b} & logs(nics_status="Denied"), {a})
		self.assertEqual({a, b} & logs(from_date="2026-03-15", to_date="2026-03-20"), {b})

	def test_has_pdf_column_is_hidden(self):
		columns, _ = self._run()
		(col,) = [c for c in columns if c["fieldname"] == "has_pdf"]
		self.assertEqual(col.get("hidden"), 1)

	def test_report_roles_match_the_4473_gate(self):
		"""Same audience as the PDF endpoint's gate (pos_4473._check_perm)."""
		from ffl_core.roles import COUNTER_4473_ROLES
		roles = {r.role for r in frappe.get_doc("Report", "Form 4473 Records").roles}
		self.assertEqual(roles, set(COUNTER_4473_ROLES))


class Form4473RecordsWorkspaceEntry(FrappeTestCase):
	"""The patch lands the tile + Compliance card link on a site whose Gun Shop
	workspace predates them, and re-running it changes nothing."""

	def test_patch_adds_tile_block_and_card_link_once(self):
		from ffl_core.patches.add_4473_records_workspace_entry import execute
		from ffl_core.workspace_shortcuts import remove_shortcut
		if not frappe.db.exists("Workspace", "Gun Shop"):
			self.skipTest("no Gun Shop workspace on this site")
		remove_shortcut("4473 Records", workspace="Gun Shop")
		ws = frappe.get_doc("Workspace", "Gun Shop")
		# the old page: a Compliance (A&D) card the link lands in (the shipped page no longer has one)
		ws.set("links", [l for l in ws.links if l.link_to != "Form 4473 Records"
			and not (l.type == "Card Break" and l.label == "Compliance (A&D)")]
			+ [{"type": "Card Break", "label": "Compliance (A&D)"}])
		ws.save(ignore_permissions=True)

		execute()
		execute()

		ws = frappe.get_doc("Workspace", "Gun Shop")
		tiles = [s for s in ws.shortcuts if s.label == "4473 Records"]
		self.assertEqual(len(tiles), 1)
		self.assertEqual((tiles[0].type, tiles[0].link_to, tiles[0].report_ref_doctype),
			("Report", "Form 4473 Records", "POS Invoice"))
		blocks = [b for b in json.loads(ws.content) if b.get("type") == "shortcut"
			and b["data"].get("shortcut_name") == "4473 Records"]
		self.assertEqual(len(blocks), 1)
		links = [i for i, l in enumerate(ws.links) if l.link_to == "Form 4473 Records"]
		self.assertEqual(len(links), 1)
		card = ws.links[links[0] - 1]
		self.assertEqual((card.type, card.label), ("Card Break", "Compliance (A&D)"))
		self.assertEqual(ws.links[links[0]].is_query_report, 1)


def _checks_provider(logs):
	"""A gunshop_4473_record_checks provider: answers for the records whose serial says so."""
	_checks_provider.asked = logs
	return {
		logs[0]["name"]: {"label": "Passed", "indicator": "green", "route": "/app/some-check?invoice=X"},
		logs[1]["name"]: {"label": "x" * 60, "indicator": "purple", "route": "https://elsewhere.example/x"},
		logs[2]["name"]: {"label": "  ", "indicator": "red"},
	}


def _failing_provider(logs):
	raise RuntimeError("boom")


class Form4473RecordChecks(FrappeTestCase):
	"""The Check column: what a gunshop_4473_record_checks provider says about each record."""

	def setUp(self):
		frappe.db.delete("Form 4473 Log", {"serials": ("like", "R4473-%")})
		self.logs = [record_4473(form4473_id=f"R-CK{n}", serials=[f"R4473-CK{n}"], pos_invoices=[f"ACC-PSINV-CK-{n}"],
			completed_on=f"2026-03-0{4 - n} 10:00:00") for n in range(3)]

	def _run(self, *providers):
		from unittest.mock import patch

		from ffl_core.ffl_core.report.form_4473_records import form_4473_records as report
		real = frappe.get_hooks
		paths = [f"{__name__}.{p.__name__}" for p in providers]
		with patch.object(frappe, "get_hooks", side_effect=lambda hook=None, *a, **k: paths if hook == report.RECORD_CHECKS_HOOK else real(hook, *a, **k)):
			columns, rows = report.execute({"serial": "R4473-CK"})
		column = next(c for c in columns if c["fieldname"] == "check")
		return column, {r["log"]: (r["check"], r["check_indicator"], r["check_route"]) for r in rows}

	def test_without_a_provider_the_column_stays_hidden(self):
		column, rows = self._run()
		self.assertEqual(column["hidden"], 1)
		self.assertEqual(set(rows.values()), {("", "", "")})

	def test_a_providers_answer_is_shown_cut_down_to_what_is_safe(self):
		column, rows = self._run(_failing_provider, _checks_provider)
		self.assertEqual(column["hidden"], 0)
		self.assertEqual([(l["name"], l["pos_invoices"], l["source"]) for l in _checks_provider.asked],
			[(name, [f"ACC-PSINV-CK-{n}"], "FastBound") for n, name in enumerate(self.logs)])
		self.assertEqual(rows[self.logs[0]], ("Passed", "green", "/app/some-check?invoice=X"))
		self.assertEqual(rows[self.logs[1]], ("x" * 40, "gray", ""))          # a long label, an unknown colour, a link out of the desk
		self.assertEqual(rows[self.logs[2]], ("", "", ""))                    # no label: nothing to show
		self.assertTrue(frappe.db.exists("Error Log", {"method": ("like", "gunshop_4473_record_checks provider failed%")}))
