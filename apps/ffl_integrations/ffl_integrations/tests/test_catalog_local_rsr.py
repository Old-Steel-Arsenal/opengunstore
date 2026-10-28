"""Local catalog (L2): the RSR driver — streaming parsers, the FTPS transport, the
four lanes and the classification rule, on synthetic feed files generated from
the parser's column map (``tests/rsr_synth.py``; no real RSR data).

The lane scenarios port the Data Service's RSR lane scenarios: first run, fresh
and stale gates, force, an unchanged rerun, a changed price, the quantity and
deleted lanes, a failed catalog skipping the deleted tail, a lock skip running
it, malformed files, and an auth failure that fails its lane while the rest run.

Lanes run under a test house code (``rsrtest``) with a fake transport, so a
site's own ``rsr`` rows are never touched — except the AC 8 quote check, which
must read ``rsr`` through rsr/restrictions.py and refuses to run on a site that
already holds RSR restriction rows. The store commits, so every test purges.
"""
from __future__ import annotations

import ftplib
import os
import ssl
import tempfile
import tracemalloc
import types
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.catalog_contract import check
from ffl_integrations.catalog_contract.transports import StubTransport
from ffl_integrations.catalog_local import store, sync
from ffl_integrations.catalog_local.backend import LocalCatalog
from ffl_integrations.catalog_local.drivers import rsr, rsr_feed
from ffl_integrations.tests import catalog_stub, catalog_testhouse
from ffl_integrations.tests.rsr_synth import (
	FakeTransport,
	catalog_line,
	catalog_text,
	deleted_text,
	quantity_text,
	restrictions_text,
	sku,
	upc,
	write_catalog,
)

TCODE = "rsrtest"
LOCAL = "ffl_integrations.catalog_local.is_local"
LANES = ["catalog", "deleted", "quantity", "restrictions"]
CALLS = ["deleted", "catalog", "quantity", "restrictions"]  # download order of a full run


def _mark(tag: str) -> str:
	"""A credential-shaped value built at runtime (no literal for secret scanners)."""
	return f"MARK-{tag}-must-not-leak"


def _purge(code=TCODE) -> None:
	for table in ("dc_offer", "dc_product_move", "dc_restriction", "dc_meta"):
		frappe.db.sql(f"DELETE FROM {table} WHERE distributor = %s", (code,))
	frappe.db.sql("DELETE FROM dc_product WHERE upc LIKE %s", ("99991%",))
	frappe.db.sql("DELETE FROM dc_sync_run WHERE source LIKE %s", (code + "\\_%",))
	frappe.db.commit()


def _write(text: str) -> str:
	fd, path = tempfile.mkstemp(suffix="-rsr-test")
	with os.fdopen(fd, "w", encoding="latin-1", newline="") as fh:
		fh.write(text)
	return path


def _runs(code=TCODE) -> list:
	return [tuple(r) for r in frappe.db.sql("""SELECT source, status, error_class FROM dc_sync_run
		WHERE source LIKE %s ORDER BY id""", (code + "\\_%",))]


def _seq(code=TCODE) -> int:
	row = frappe.db.sql("SELECT change_seq FROM dc_meta WHERE distributor = %s", (code,))
	return int(row[0][0]) if row else 0


def _offer_seqs(code=TCODE) -> dict:
	return dict(frappe.db.sql("SELECT sku, change_seq FROM dc_offer WHERE distributor = %s", (code,)))


def _offer(s, code=TCODE):
	return frappe.db.sql("""SELECT cost, qty, discontinued, status_flag, ffl_required, product_class,
		department, department_name FROM dc_offer WHERE distributor = %s AND sku = %s""",
		(code, s), as_dict=True)[0]


def _changed_keys(since: str) -> list:
	"""This module's product keys in the change feed after ``since``."""
	return [k for k in LocalCatalog().products_changes(since)["keys"]
		if k.startswith(("99991", f"{TCODE}:"))]


def _exploding_parser(path):
	raise AssertionError("the unchanged restrictions file was parsed")
	yield  # a generator function, like the real parser


def _age_runs(source: str, hours: int) -> None:
	frappe.db.sql("""UPDATE dc_sync_run SET started_at = started_at - INTERVAL %s HOUR,
		finished_at = finished_at - INTERVAL %s HOUR WHERE source = %s""", (hours, hours, source))
	frappe.db.commit()


# The standard synthetic feed: 12 catalog rows; the quantity file agrees with the
# catalog's own qty column and the deleted file names only a stock number the
# catalog no longer carries (as RSR's does), so an unchanged rerun moves nothing.
CATALOG_N = range(1, 13)
QTY_PAIRS = [(sku(n), n % 7) for n in CATALOG_N]
GONE = "ZZR-999999"
RESTRICTED = [(sku(1), "ALL", "NJ", "S", "F"), (sku(1), "ALL", "NY", "S", "O"),
	(sku(2), "COOK", "IL", "T", "Y"), (sku(2), "DEERFIELD", "IL", "C", "L")]


def _feed(**over) -> dict:
	files = {"catalog": catalog_text(CATALOG_N), "quantity": quantity_text(QTY_PAIRS),
		"deleted": deleted_text([GONE]), "restrictions": restrictions_text(RESTRICTED)}
	files.update(over)
	return files


class _Base(FrappeTestCase):
	def setUp(self):
		catalog_stub.uninstall()
		_purge()
		self.transport = FakeTransport(_feed())
		self.driver = sync.register(rsr.RsrDriver(code=TCODE, transport=self.transport))
		self.addCleanup(sync.unregister, TCODE)
		self.addCleanup(_purge)
		self.addCleanup(self._no_temp_files_left)

	def _no_temp_files_left(self):
		self.assertEqual([p for p in self.transport.left_behind if os.path.exists(p)], [],
			"a lane left its downloaded file behind")

	def _other_connection(self):
		from frappe.database import get_db

		conf = frappe.local.conf
		other = get_db(socket=conf.db_socket, host=conf.db_host, port=conf.db_port,
			user=conf.db_user or conf.db_name, password=conf.db_password, cur_db_name=conf.db_name)
		other.connect()
		self.addCleanup(other.close)
		return other


# ---------------------------------------------------------------- parsers (pure)

class TestRsrFeedParsers(unittest.TestCase):
	def test_catalog_columns_map_to_store_names(self):
		path = _write(catalog_line(3, status="Allocated", qty="9", map="499.00", cost="445.50",
			msrp="599.00", ground_only="Y", dropship_blocked="Y", prop65="Y", image_name="IMG3.jpg",
			long_description="Caf\xe9 synthetic") + "\n")
		self.addCleanup(os.unlink, path)
		gen = rsr_feed.iter_catalog(path)
		self.assertIsInstance(gen, types.GeneratorType)  # streaming, not a list
		(row,) = list(gen)
		self.assertEqual((row["sku"], row["upc"], row["cost"], row["map"], row["msrp"]),
			(sku(3), upc(3), 445.5, 499.0, 599.0))
		self.assertEqual(row["qty"], 0)  # Allocated stock is not orderable
		self.assertEqual(row["status_flag"], "Allocated")
		self.assertTrue(row["ships_ground_only"] and row["dropship_blocked"] and row["prop65"])
		self.assertFalse(row["signature_required"] or row["vendor_approval_required"])
		self.assertEqual(row["image_url"], "https://img.rsrgroup.com/pimages/IMG3.jpg")
		self.assertEqual(row["image_url_hires"], "https://img.rsrgroup.com/highres-pimages/IMG3_HR.jpg")
		self.assertEqual(row["long_description"], "Caf\xe9 synthetic")  # latin-1 decoded
		# no FFL column: RSR's department says it (05 = Sporting Long Guns)
		self.assertEqual((row["department"], row["department_name"], row["ffl_required"]),
			("05", "Sporting Long Guns", True))
		self.assertEqual(row["product_class"], "Long Gun")  # "ACME TEST RIFLE …"
		self.assertFalse(set(store.REQUIRED_KEYS) - set(row))

	def test_trailing_semicolon_is_the_healthy_width(self):
		stats = {}
		path = _write(catalog_text(range(1, 201)))
		self.addCleanup(os.unlink, path)
		self.assertEqual(len(list(rsr_feed.iter_catalog(path, stats))), 200)
		self.assertNotIn("wrong_width", stats)

	def test_layout_guards_raise_at_the_end_of_the_file(self):
		shifted = ";".join(["SHIFTED"] + [""] * 59)
		cases = {
			"column layout shift": catalog_text(range(1, 50)) + (shifted + "\n") * 3,
			"layout mismatch": catalog_text(range(1, 50)) + (";" * 77 + "\n") * 3,
			"no data lines": "\n\n",
			"wrong field count": catalog_text(range(1, 30)) + catalog_line(30)[:120] + "\n",
		}
		for needle, text in cases.items():
			with self.subTest(needle):
				path = _write(text)
				self.addCleanup(os.unlink, path)
				with self.assertRaises(sync.FeedFormatError) as cm:
					list(rsr_feed.iter_catalog(path))
				self.assertIn(needle, str(cm.exception))

	def test_a_wrong_width_row_is_skipped_never_yielded(self):
		stats = {}
		shifted = catalog_line(150).replace(";", ";EXTRA;", 1)  # one column too many
		path = _write(catalog_text(range(1, 150)) + shifted + "\n" + catalog_text(range(151, 201)))
		self.addCleanup(os.unlink, path)
		rows = list(rsr_feed.iter_catalog(path, stats))  # 1 of 200 is under the 1% bar
		self.assertEqual(len(rows), 199)
		self.assertNotIn(sku(150), {r["sku"] for r in rows})
		self.assertEqual(stats["wrong_width"], 1)

	def test_departments_come_from_rsr_and_unknown_means_ffl(self):
		# the Data Service's RSR department table, its FFL rows
		self.assertEqual(rsr_feed.FFL_DEPARTMENTS, {"01", "02", "03", "05", "06", "41", "42", "43"})
		self.assertEqual({n: rsr_feed.DEPARTMENT_NAMES[n] for n in ("01", "06", "07", "42")},
			{"01": "Handguns", "06": "SOTS", "07": "Black Powder Firearms", "42": "SBR Uppers"})
		for dept, want in (("01", True), ("1", True), (" 05 ", True), ("14", False), ("07", False),
				("41", True), ("99", True), ("44", True), ("", True), ("AB", True)):
			self.assertIs(rsr_feed.department_ffl(dept), want, dept)
		path = _write(catalog_line(1, department="") + "\n" + catalog_line(2, department="77")
			+ "\n" + catalog_line(3, department="14") + "\n")
		self.addCleanup(os.unlink, path)
		got = {r["sku"]: (r["ffl_required"], r["department_name"]) for r in rsr_feed.iter_catalog(path)}
		self.assertEqual(got, {sku(1): (True, ""), sku(2): (True, ""),
			sku(3): (False, "Holsters/Pouches")})

	def test_a_runaway_quote_is_a_format_error(self):
		path = _write(catalog_line(1, title="~UNCLOSED " + "x" * 200) + "\n"
			+ ("y" * 140_000) + "\n")
		self.addCleanup(os.unlink, path)
		with self.assertRaises(sync.FeedFormatError):
			list(rsr_feed.iter_catalog(path))

	def test_quantity_deleted_restrictions(self):
		q = _write("ZZR-000001,4\nZZR-000002,1.0\n,9\nZZR-000003,abc\nshort\n")
		d = _write("ZZR-000001;DELETED\n\n ZZR-000002 ;X\n")
		r = _write('ZZR-000001;"ALL";nj;s;f\nZZR-000001;"CHICAGO";IL;C;Y\njunk\n')
		for p in (q, d, r):
			self.addCleanup(os.unlink, p)
		self.assertEqual(list(rsr_feed.iter_quantity(q)), [("ZZR-000001", 4), ("ZZR-000002", 1)])
		self.assertEqual(list(rsr_feed.iter_deleted(d)), ["ZZR-000001", "ZZR-000002"])
		stats = {}
		with self.assertRaises(sync.FeedFormatError):  # 1 junk line of 3 > 1%
			list(rsr_feed.iter_restrictions(r, stats))
		self.assertEqual(stats, {"total": 3, "skipped": 1})
		empty = _write("\n\n")
		self.addCleanup(os.unlink, empty)
		with self.assertRaises(sync.FeedFormatError) as cm:
			list(rsr_feed.iter_restrictions(empty))
		self.assertIn("no data lines", str(cm.exception))
		ok = _write('ZZR-000001;"ALL";nj;s;f\nZZR-000001;"CHICAGO";IL;C;Y\n')
		self.addCleanup(os.unlink, ok)
		self.assertEqual(list(rsr_feed.iter_restrictions(ok)), [
			("ZZR-000001", ["ALL", "NJ", "S", "F"]), ("ZZR-000001", ["CHICAGO", "IL", "C", "Y"])])


# ---------------------------------------------------------------- classification (pure)

# (department, title) -> (ffl_required, product_class), worked out by hand from
# the Data Service's department FFL column (blank/unlisted = FFL here, stricter),
# the Data Service's RSR CASE order, and the title rules of
# the Data Service's classifier.
NEUTRAL = "ACME TEST ITEM"
EVERY_DEPARTMENT = {
	"01": (True, "Handgun"), "02": (True, "Handgun"), "03": (True, "Long Gun"),
	"04": (False, "Accessory"), "05": (True, "Long Gun"), "06": (True, "NFA"),
	"07": (False, "Accessory"), "08": (False, "Accessory"), "09": (False, "Accessory"),
	"10": (False, "Accessory"), "11": (False, "Accessory"), "12": (False, "Accessory"),
	"13": (False, "Accessory"), "14": (False, "Accessory"), "15": (False, "Accessory"),
	"16": (False, "Accessory"), "17": (False, "Accessory"), "18": (False, "Ammunition"),
	"19": (False, "Accessory"), "20": (False, "Accessory"), "21": (False, "Accessory"),
	"22": (False, "Accessory"), "23": (False, "Accessory"), "24": (False, "Accessory"),
	"25": (False, "Accessory"), "26": (False, "Accessory"), "27": (False, "Accessory"),
	"28": (False, "Accessory"), "29": (False, "Accessory"), "30": (False, "Accessory"),
	"31": (False, "Accessory"), "32": (False, "Gun Parts"), "33": (False, "Accessory"),
	"34": (False, "Gun Parts"), "35": (False, "Accessory"), "36": (False, "Accessory"),
	"37": (False, "Accessory"), "38": (False, "Accessory"), "39": (False, "Accessory"),
	"40": (False, "Accessory"), "41": (True, "Gun Parts"), "42": (True, "Gun Parts"),
	"43": (True, "Gun Parts"),
	"": (True, "Other Firearm"), "44": (True, "Other Firearm"),
}
TITLED = [
	# r2 failures
	("", "SIG P320 FCU FIRE CONTROL UNIT", True, "Other Firearm"),
	("01", "ACME AR PISTOL 5.56 W/ CARBINE BRACE", True, "Handgun"),
	("12", "MTM AMMO CAN 30 CAL", False, "Accessory"),
	("07", "TRADITIONS PURSUIT .50 MUZZLELOADER", False, "Accessory"),
	("42", "ACME AR15 SBR UPPER 10.5IN 5.56", True, "Gun Parts"),
	# r1 titles, in their departments
	("01", "GLOCK 19 GEN5 9MM PISTOL 15RD 3 MAGS", True, "Handgun"),
	("01", "SIG P365 9MM PISTOL 10RD NIGHT SIGHTS", True, "Handgun"),
	("05", "MOSSBERG 500 12GA SHOTGUN 28IN W/ CHOKE TUBES", True, "Long Gun"),
	("06", "DANIEL DEFENSE MK18 SBR 10.3 BARREL", True, "NFA"),
	("05", "DANIEL DEFENSE MK18 SBR 10.3 BARREL", True, "Long Gun"),  # "barrel" vetoes NFA
	("05", "HENRY RIFLE 22LR AMMO NOT INCLUDED", True, "Long Gun"),
	("01", "ACME REVOLVER 357 W/ HARD CASE", True, "Handgun"),
	("01", "ACME TEST RIFLE 5.56 CARBINE", True, "Handgun"),  # the department decides
	("13", "ACME TEST PISTOL 9MM", False, "Accessory"),
	("13", "ACME TEST AMMO 9MM", False, "Accessory"),
	# the title marks NFA only on an FFL row, and only as the whole word
	("", "ACME 45 SUPPRESSOR", True, "NFA"),
	("", "acme sbs 12ga 14in", True, "NFA"),
	("03", "ACME TEST SILENCER 9MM", True, "NFA"),
	("13", "ACME SUPPRESSOR COVER", False, "Accessory"),
	("", "ACMESUPPRESSORS 9MM", True, "Other Firearm"),
	("01", "ACME PISTOL SUPPRESSOR READY 9MM", True, "Handgun"),
	("", "ACME 9MM SUPPRESSOR-HEIGHT SIGHTS", True, "Other Firearm"),
	("", "ACME SUPP HGT SIGHT SET", True, "Other Firearm"),
	("", "ACME 9MM THREADED SBR", True, "Other Firearm"),
	("41", "ACME SUPPRESSOR UPPER 300BLK", True, "Gun Parts"),
	("43", "ACME 22LR SUPPRESSOR CONVERSION KIT", True, "Gun Parts"),  # 41/42/43 never NFA
	("34", "ACME SBR PARTS KIT", False, "Gun Parts"),
	("18", "ACME 9MM 115GR 50RD", False, "Ammunition"),
	("1", "ACME TEST 9MM", True, "Handgun"),  # zero-padded
	("05", 'EXAMPLE 22LR 18.5" FLASH SUPPRESSOR', True, "Long Gun"),
	("", "A2 Flash Suppressor 1/2x28", True, "Other Firearm"),
	("", "ACME A2 FLASH-SUPPRESSOR 1/2X28", True, "Other Firearm"),
	("13", "A2 Flash Suppressor 1/2x28", False, "Accessory"),
	("", "SilencerCo Omega 36M Suppressor", True, "NFA"),
	("06", "OCL POLONIUM 9MM SUPPRESSOR BLK", True, "NFA"),
	("01", "GLOCK 19 GEN5 9MM SUPPRESSOR READY", True, "Handgun"),
	("05", "MOSSBERG 590 SBS 12GA 14IN", True, "NFA"),
	("", "ACME 300BLK SHORT BARREL RIFLE", True, "NFA"),
	("05", "ACME 12GA SHORT-BARRELED SHOTGUN", True, "NFA"),
	("", "ACME 9MM AOW", True, "NFA"),
]
# The titles are the Data Service's own test titles, placed
# in RSR departments.
RECEIVER_TITLED = [
	("41", "YHM AR-15 STRIPPED UPPER RECEIVER", True, "Gun Parts"),
	("41", "RUGER 10/22 RECEIVER UPPER", True, "Gun Parts"),
	("42", "DD GL/SSC RISII UP RCVR 12.5 556 FDE", True, "Gun Parts"),  # a real RSR 42 title: "UP RCVR" is an upper
	("41", "KAK AR-15 LOWER PARTS KIT COMPLETE", True, "Gun Parts"),
	("41", "SOLGW 16IN 5.56 BARREL", True, "Gun Parts"),
	("41", "PSA AR15 COMPLETE LOWER", True, "Other Firearm"),
	("41", "ACME AR-15 STRIPPED LOWER", True, "Other Firearm"),
	("41", "ACME AR15 LOWER RECEIVER STRIPPED", True, "Other Firearm"),
	("41", "ACME RECEIVER", True, "Other Firearm"),
	("41", "SOLGW BARRELED ACTION", True, "Other Firearm"),
	("42", "ACME AR15 LOWER RCVR", True, "Other Firearm"),
	("43", "ACME FRAME", True, "Other Firearm"),
	("41", "ACME BBL'D ACTION 308", True, "Other Firearm"),
	("42", "ACME AR15 SBR LOWER RECEIVER", True, "NFA"),
	# deliberately stricter than the Data Service, whose department rule keeps every RSR 41-43 row a part:
	("41", "ACME AR15 LOWER RECEIVER", True, "Other Firearm"),
	# the receiver rule needs the FFL flag, as the Data Service's does: a non-FFL department stays a part
	("34", "PSA AR15 COMPLETE LOWER", False, "Gun Parts"),  # the Data Service's own test gives this row an FFL flag and expects Other Firearm; here department 34 is not FFL, so it stays a part
	("13", "ACME RECEIVER", False, "Accessory"),
]
# the Data Service's RSR product_class test cases,
# with this port's FFL flag (the department table's); where that flag differs from
# the Data Service's, the row says so.
PRODUCT_CLASS_CASES = [
	("41", 'GEISSELE SUPER DUTY COMPLETE UPPER 16" 5.56 NATO', True, "Gun Parts"),
	("42", "SOLGW 11.5IN 5.56 SBR BARREL MID-LENGTH", True, "Gun Parts"),
	("06", "OCL POLONIUM 9MM SUPPRESSOR BLK", True, "NFA"),
	("13", "SILENCERCO BRAVO ASR MOUNT 1/2X28", False, "Accessory"),
	("41", "YHM AR-15 STRIPPED UPPER RECEIVER", True, "Gun Parts"),
	("41", "AERO AR15 LOWER PARTS KIT", True, "Gun Parts"),
	("41", "CMMG 22LR CONVERSION KIT", True, "Gun Parts"),  # the Data Service's test row has no FFL flag; department 41 is FFL here, the class is the same
	("42", "BCM COMPLETE SBR UPPER 11.5IN 5.56", True, "Gun Parts"),
	("05", "MOSSBERG 590 SBS 12GA 14IN", True, "NFA"),
	("01", "GLOCK 19 GEN5 9MM SUPPRESSOR READY", True, "Handgun"),
	("01", "SIG P320 9MM SUPPRESSOR HEIGHT SIGHTS", True, "Handgun"),
	("05", "RUGER 10/22 THREADED SUPPRESSOR", True, "Long Gun"),
	("10", "MAGPUL PMAG 30 SBR", False, "Accessory"),
	("32", 'SOLGW PRECISION SPR 16" .223 WYLDE', False, "Gun Parts"),
	("32", "SUREFIRE WARCOMP FH 5.56 1/2X28", False, "Gun Parts"),
	("34", "TIMNEY TRIG FITS AR15", False, "Gun Parts"),
]


class TestRsrClassParity(unittest.TestCase):
	"""ffl_required and product_class equal the Data Service's RSR rules."""

	def _check(self, dept, title, want):
		ffl = rsr_feed.department_ffl(dept)
		self.assertEqual((ffl, rsr_feed.product_class(dept, title, ffl)), want, (dept, title))
		# and through the parser, as the lane stores it
		path = _write(catalog_line(1, department=dept, title=title) + "\n")
		self.addCleanup(os.unlink, path)
		(row,) = list(rsr_feed.iter_catalog(path))
		self.assertEqual((row["ffl_required"], row["product_class"]), want, (dept, title))

	def test_every_seed_department_blank_and_unlisted(self):
		self.assertEqual(set(EVERY_DEPARTMENT) - {"", "44"}, set(rsr_feed.DEPARTMENT_NAMES))
		for dept, want in EVERY_DEPARTMENT.items():
			self._check(dept, NEUTRAL, want)

	def test_titles_only_mark_nfa(self):
		for dept, title, ffl, klass in TITLED:
			self._check(dept, title, (ffl, klass))

	def test_a_flagged_upper_department_row_naming_a_receiver_is_a_firearm(self):
		for dept, title, ffl, klass in RECEIVER_TITLED:
			self._check(dept, title, (ffl, klass))

	def test_the_data_service_rsr_product_class_cases(self):
		for dept, title, ffl, klass in PRODUCT_CLASS_CASES:
			self._check(dept, title, (ffl, klass))

	def test_title_rules_match_the_data_service_cases(self):
		for t in ("PSA AR15 COMPLETE LOWER", "ACME AR15 LOWER RECEIVER", "ACME FRAME",
				"SOLGW BARRELED ACTION", "ACME AR-15 STRIPPED LOWER", "ACME AR15 LOWER RECEIVER STRIPPED",
				"ACME RECEIVER", "ACME BBL'D ACTION 308", "ACME AR15 LOWER RCVR", "EXAMPLE FORGED LOWER"):
			self.assertIs(rsr_feed.names_a_receiver(t), True, msg=t)
		for t in ("YHM AR-15 STRIPPED UPPER RECEIVER", "RUGER 10/22 RECEIVER UPPER",
				"KAK AR-15 LOWER PARTS KIT COMPLETE", "SOLGW 16IN 5.56 BARREL", "ACME TEST ITEM",
				"DD GL/SSC RISII UP RCVR 12.5 556 FDE"):
			self.assertIs(rsr_feed.names_a_receiver(t), False, msg=t)
		for t in ("SilencerCo Omega 36M Suppressor", "OCL POLONIUM 9MM SUPPRESSOR BLK",
				"MOSSBERG 590 SBS 12GA 14IN", "ACME 300BLK SHORT BARREL RIFLE",
				"ACME 12GA SHORT-BARRELED SHOTGUN", "ACME 9MM AOW", "ACME ANY OTHER WEAPON 410",
				"ACME 45 SUPPRESSOR", "ACME TEST SILENCER 9MM"):
			self.assertIs(rsr_feed.nfa_title(t), True, msg=t)
		for t in ("A2 Flash Suppressor 1/2x28", "ACME A2 FLASH-SUPPRESSOR 1/2X28",
				'EXAMPLE 22LR 18.5" FLASH SUPPRESSOR', "GLOCK 19 GEN5 9MM SUPPRESSOR READY",
				"ACMESUPPRESSORS 9MM", "DANIEL DEFENSE MK18 SBR 10.3 BARREL", "ACME 9MM THREADED SBR",
				"ACME SUPP HGT SIGHT SET", "ACME AR15 SBR UPPER 10.5IN 5.56"):
			self.assertIs(rsr_feed.nfa_title(t), False, msg=t)

	def test_rsr_classes_are_the_service_classes(self):
		from ffl_integrations.distributor.util import LEGAL_FIREARM_CLASSES, PRODUCT_CLASSES

		got = {k for _f, k in EVERY_DEPARTMENT.values()} | {k for *_x, k in TITLED}
		self.assertLessEqual(got, set(PRODUCT_CLASSES))
		# every FFL-required row with a firearm class, every other one without
		for dept, (ffl, klass) in EVERY_DEPARTMENT.items():
			if klass in LEGAL_FIREARM_CLASSES:
				self.assertTrue(ffl, dept)


# ---------------------------------------------------------------- lanes (the Data Service's RSR lane scenarios)

class TestRsrLanes(_Base):
	def test_first_run_does_every_lane_in_order(self):
		result = sync.run_house(TCODE)
		self.assertEqual(self.transport.calls, CALLS)
		self.assertEqual(result["_failed"], [])
		self.assertEqual((result["catalog"]["created"], result["catalog"]["total"]), (12, 12))
		self.assertEqual(result["quantity"], {"updated": 0, "total": 12})
		self.assertEqual(result["deleted"], {"flagged": 0, "total": 1})
		self.assertEqual(result["restrictions"],
			{"rows_written": 2, "lines": 4, "skipped_unchanged": False})
		self.assertEqual(_runs(), [(f"{TCODE}_{ln}", "success", "") for ln in LANES])
		self.assertEqual(LocalCatalog().restrictions_entries(TCODE, sku(1)),
			[["ALL", "NJ", "S", "F"], ["ALL", "NY", "S", "O"]])
		health = LocalCatalog().health(TCODE)
		self.assertEqual((health["offers_total"], health["offers_active"]), (12, 12))
		self.assertIsNotNone(health["last_catalog_sync_at"])

	def test_fresh_gates_run_only_quantity_then_a_stale_catalog_brings_deleted(self):
		sync.run_house(TCODE)
		self.transport.calls.clear()
		sync.run_house(TCODE)
		self.assertEqual(self.transport.calls, ["quantity"])
		_age_runs(f"{TCODE}_catalog", 3)
		_age_runs(f"{TCODE}_deleted", 3)
		self.transport.calls.clear()
		sync.run_house(TCODE)
		self.assertEqual(self.transport.calls, ["deleted", "catalog", "quantity"])

	def test_deleted_runs_in_the_catalog_job_and_never_flips_a_listed_sku(self):
		"""the Data Service's order: the deleted lane follows the catalog in the same job. A SKU on
		both lists is stored Deleted from the start, so a rerun bumps nothing; one taken
		off the deleted list is live again."""
		self.transport.files["deleted"] = deleted_text([GONE, sku(3)])
		out = sync.run_lane(TCODE, "catalog")
		self.assertEqual((out["created"], out["total"]), (12, 12))
		self.assertEqual(self.transport.calls, ["deleted", "catalog"])
		self.assertEqual(_runs(), [(f"{TCODE}_catalog", "success", ""), (f"{TCODE}_deleted", "success", "")])
		gone = _offer(sku(3))
		self.assertEqual((gone.discontinued, gone.status_flag, gone.qty), (1, "Deleted", 0))
		seq, seqs = _seq(), _offer_seqs()
		sync.run_lane(TCODE, "catalog", force=True)
		self.assertEqual((_seq(), _offer_seqs()), (seq, seqs))
		self.assertEqual(_changed_keys(f"{TCODE}:{seq}"), [])
		self.assertEqual(sync.run_lane(TCODE, "deleted"), sync.NOT_DUE)
		# a live SKU that leaves the catalog for the deleted list is flagged in the same job
		self.transport.files["catalog"] = catalog_text([n for n in CATALOG_N if n != 4])
		self.transport.files["deleted"] = deleted_text([GONE, sku(3), sku(4)])
		sync.run_lane(TCODE, "catalog", force=True)
		gone = _offer(sku(4))
		self.assertEqual((gone.discontinued, gone.status_flag, gone.qty), (1, "Deleted", 0))
		# RSR takes 3 off its deleted list and the file carries it: live again
		self.transport.files["catalog"] = catalog_text(CATALOG_N)
		self.transport.files["deleted"] = deleted_text([GONE])
		sync.run_lane(TCODE, "catalog", force=True)
		back = _offer(sku(3))
		self.assertEqual((back.discontinued, back.status_flag, back.qty), (0, "", 3))

	def test_force_bypasses_the_gates(self):
		sync.run_house(TCODE)
		self.transport.calls.clear()
		sync.run_house(TCODE, force=True)
		self.assertEqual(self.transport.calls, CALLS)

	def test_an_unchanged_rerun_bumps_nothing(self):
		sync.run_house(TCODE)
		seq, seqs = _seq(), _offer_seqs()
		result = sync.run_house(TCODE, force=True)
		self.assertEqual((result["catalog"]["created"], result["catalog"]["updated"]), (0, 0))
		self.assertEqual(result["quantity"]["updated"], 0)
		self.assertEqual(result["deleted"]["flagged"], 0)
		self.assertTrue(result["restrictions"]["skipped_unchanged"])
		self.assertEqual((_seq(), _offer_seqs()), (seq, seqs))
		self.assertEqual(_changed_keys(f"{TCODE}:{seq}"), [])

	def test_one_price_change_bumps_exactly_one_row(self):
		sync.run_house(TCODE)
		seq, seqs = _seq(), _offer_seqs()
		self.transport.files["catalog"] = catalog_text(CATALOG_N, over={5: {"cost": "777.25"}})
		result = sync.run_lane(TCODE, "catalog", force=True)
		self.assertEqual((result["created"], result["updated"], result["unchanged"]), (0, 1, 11))
		after = _offer_seqs()
		self.assertEqual({s for s in after if after[s] != seqs[s]}, {sku(5)})
		self.assertEqual(float(_offer(sku(5)).cost), 777.25)
		self.assertEqual(_changed_keys(f"{TCODE}:{seq}"), [upc(5)])

	def test_quantity_lane(self):
		sync.run_house(TCODE)
		self.transport.files["deleted"] = deleted_text([sku(12)])
		sync.run_lane(TCODE, "deleted", force=True)
		seqs = _offer_seqs()
		self.transport.files["quantity"] = quantity_text(
			[(sku(3), 40), (sku(4), 4), (sku(3), 41), ("ZZR-UNKNOWN", 5), (sku(12), 9)])
		result = sync.run_lane(TCODE, "quantity")
		self.assertEqual(result, {"updated": 1, "total": 4})  # sku 3, its last line
		self.assertEqual(_offer(sku(3)).qty, 41)
		after = _offer_seqs()
		self.assertEqual({s for s in after if after[s] != seqs[s]}, {sku(3)})
		gone = _offer(sku(12))  # a Deleted offer keeps its flag and gets no stock
		self.assertEqual((gone.status_flag, gone.qty), ("Deleted", 0))
		self.assertLessEqual(max(after.values()), _seq())

	def test_allocated_stock_is_never_orderable(self):
		sync.run_house(TCODE)
		self.transport.files["catalog"] = catalog_text(CATALOG_N, over={4: {"status": "Allocated"}})
		sync.run_lane(TCODE, "catalog", force=True)
		self.assertEqual(_offer(sku(4)).qty, 0)  # parsed as 0 (store.not_orderable)
		local = LocalCatalog()
		self.assertEqual(local.rows(TCODE, [sku(4)])[sku(4)]["qty"], 0)
		in_stock = {p["key"] for p in local.products_page({"in_stock": True, "limit": 50,
			"distributor": [TCODE]})["products"]}
		self.assertNotIn(upc(4), in_stock)
		self.assertIn(upc(5), in_stock)
		self.transport.files["quantity"] = quantity_text([(sku(4), 30), (sku(5), 30)])
		# 4 stays 0 (allocated, whatever the file says), 5 goes to 30
		self.assertEqual(sync.run_lane(TCODE, "quantity")["updated"], 1)
		self.assertEqual((_offer(sku(4)).qty, _offer(sku(5)).qty), (0, 30))
		# 12 offers: 7 has no stock (7 % 7), 4 is allocated
		self.assertEqual(LocalCatalog().ops_flags(TCODE)["in_stock"], 10)

	def test_a_catalog_run_after_a_quantity_run_writes_the_file_qty(self):
		"""the Data Service's RSR lane: the catalog file writes qty on every run; change_seq
		moves only for a row whose stored values change. A row the deleted lane
		retired, that the deleted list no longer names and the file carries again,
		is live again."""
		sync.run_house(TCODE)
		self.transport.files["quantity"] = quantity_text([(sku(2), 33)])
		sync.run_lane(TCODE, "quantity")
		self.transport.files["deleted"] = deleted_text([sku(3)])
		sync.run_lane(TCODE, "deleted", force=True)
		seqs = _offer_seqs()
		self.transport.files["deleted"] = deleted_text([GONE])  # RSR took 3 off its deleted list
		result = sync.run_lane(TCODE, "catalog", force=True)  # the file still says 2 and carries 3
		self.assertEqual((result["updated"], _offer(sku(2)).qty), (2, 2))
		after = _offer_seqs()
		self.assertEqual({s for s in after if after[s] != seqs[s]}, {sku(2), sku(3)})
		back = _offer(sku(3))
		self.assertEqual((back.discontinued, back.status_flag, back.qty), (0, "", 3))
		self.assertEqual(sync.run_lane(TCODE, "catalog", force=True)["updated"], 0)
		self.assertEqual(_offer_seqs(), after)

	def test_a_not_orderable_row_reports_and_sorts_as_zero(self):
		"""store.not_orderable is the one rule: a Deleted, Allocated or
		discontinued row reads qty 0 in rows() and item_by_upc, and ranks as 0
		in the qty sort, whatever qty is stored."""
		path = _write(catalog_text(CATALOG_N))
		self.addCleanup(os.unlink, path)
		rows = list(rsr_feed.iter_catalog(path))
		over = {sku(3): {"status_flag": "Deleted", "qty": 50},
			sku(4): {"status_flag": "ALLOCATED", "qty": 60},
			sku(6): {"discontinued": True, "qty": 70}}
		for r in rows:
			r.update(over.get(r["sku"], {}))
		store.load_catalog(rows, distributor=TCODE)
		self.assertEqual(_offer(sku(3)).qty, 50)  # stored as given …
		local = LocalCatalog()
		got = local.rows(TCODE, [sku(3), sku(4), sku(6), sku(5)])
		self.assertEqual({s: o["qty"] for s, o in got.items()},
			{sku(3): 0, sku(4): 0, sku(6): 0, sku(5): 5})  # … read as 0
		self.assertEqual([o["qty"] for o in local.item_by_upc(upc(3))["offers"]
			if o["distributor"] == TCODE], [0])
		self.assertEqual(store.orderable_qty(got[sku(3)] | {"qty": 50}), 0)
		self.assertEqual(store.offer_sort_key(got[sku(3)] | {"qty": 50}, {})[0], 1)  # no stock
		page = local.products_page({"sort": "qty", "include_discontinued": True, "limit": 50,
			"distributor": [TCODE]})["products"]
		keys = [p["key"] for p in page]
		positive = [upc(n) for n in CATALOG_N if n not in (3, 4, 6) and n % 7]
		self.assertEqual(keys[0], upc(5))  # qty 5 (12 ties, later key); 60 and 70 rank as 0
		self.assertEqual(set(keys[:len(positive)]), set(positive))
		self.assertNotIn(upc(3), keys)  # a Deleted offer is not listed at all

	def test_deleted_lane_and_the_product_layer(self):
		sync.run_house(TCODE)
		self.assertEqual(sync.run_lane(TCODE, "deleted", force=True)["flagged"], 0)
		self.transport.files["deleted"] = deleted_text([sku(2), sku(2), sku(3)])
		seqs = _offer_seqs()
		result = sync.run_lane(TCODE, "deleted", force=True)
		self.assertEqual(result, {"flagged": 2, "total": 2})
		gone = _offer(sku(3))
		self.assertEqual((gone.discontinued, gone.status_flag, gone.qty), (1, "Deleted", 0))
		self.assertEqual(LocalCatalog().health(TCODE)["offers_active"], 10)
		after = _offer_seqs()
		self.assertEqual({s for s in after if after[s] != seqs[s]}, {sku(2), sku(3)})
		self.assertLessEqual(max(after.values()), _seq())
		# the same file again: already in that state, nothing rewritten
		self.assertEqual(sync.run_lane(TCODE, "deleted", force=True)["flagged"], 0)
		self.assertEqual(_offer_seqs(), after)
		product = LocalCatalog().item_by_upc(upc(3))["product"]  # the rifle (department 05)
		self.assertTrue(product["ffl_required"])  # availability never toggles gun-ness

	def test_a_failed_catalog_skips_the_deleted_tail(self):
		shifted = ";".join(["SHIFTED"] + [""] * 59)
		self.transport.files["catalog"] = catalog_line(1) + "\n" + (shifted + "\n") * 3
		result = sync.run_house(TCODE)
		self.assertEqual(self.transport.calls, CALLS)
		self.assertEqual(result["_failed"], [f"{TCODE}_catalog"])
		self.assertIsNone(result["deleted"])
		self.assertEqual(_runs()[0], (f"{TCODE}_catalog", "failed", "FeedFormatError"))
		self.assertEqual(frappe.db.sql("SELECT COUNT(*) FROM dc_offer WHERE distributor = %s",
			(TCODE,))[0][0], 0)

	def test_a_lock_skipped_catalog_still_runs_the_tail(self):
		other = self._other_connection()
		self.assertEqual(other.sql("SELECT GET_LOCK(%s, 0)", (store.lock_name(TCODE),))[0][0], 1)
		try:
			result = sync.run_house(TCODE)
		finally:
			other.sql("SELECT RELEASE_LOCK(%s)", (store.lock_name(TCODE),))
		self.assertEqual(self.transport.calls, CALLS)
		for lane in LANES:
			self.assertEqual(result[lane], store.LOCK_SKIPPED, lane)
		self.assertEqual(result["_failed"], [])
		self.assertEqual(_runs(), [])  # a skip is neither a success nor a failure
		self.assertEqual(frappe.db.sql("SELECT lock_skip_count FROM dc_meta WHERE distributor = %s",
			(TCODE,))[0][0], 4)

	def test_restrictions_hash_skip_and_swap(self):
		sync.run_house(TCODE)
		with patch.object(rsr_feed, "iter_restrictions", _exploding_parser):
			self.assertTrue(sync.run_lane(TCODE, "restrictions", force=True)["skipped_unchanged"])
		self.transport.files["restrictions"] = restrictions_text(
			[(sku(3), "ALL", "CA", "S", "H"), (sku(3), "ALL", "TX", "S", "F")])
		self.assertEqual(sync.run_lane(TCODE, "restrictions", force=True)["rows_written"], 1)
		local = LocalCatalog()
		self.assertEqual(local.restrictions_entries(TCODE, sku(1)), [])
		self.assertEqual(local.restrictions_entries(TCODE, sku(3)),
			[["ALL", "CA", "S", "H"], ["ALL", "TX", "S", "F"]])
		self.assertEqual(frappe.db.sql("""SELECT entry_count FROM dc_restriction
			WHERE distributor = %s""", (TCODE,))[0][0], 2)

	def test_a_malformed_restrictions_file_keeps_the_old_set(self):
		sync.run_house(TCODE)
		self.transport.files["restrictions"] = restrictions_text([(sku(5), "ALL", "CA", "S", "F")]) \
			+ "junk\n" * 3
		self.assertIsNone(sync.run_lane(TCODE, "restrictions", force=True))
		self.assertEqual(_runs()[-1], (f"{TCODE}_restrictions", "failed", "FeedFormatError"))
		self.assertEqual(LocalCatalog().restrictions_entries(TCODE, sku(1)),
			[["ALL", "NJ", "S", "F"], ["ALL", "NY", "S", "O"]])

	def test_a_truncated_snapshot_is_refused_by_the_snapshot_guard(self):
		with patch.object(store, "PARTIAL_FILE_FLOOR", 10):
			sync.run_house(TCODE)
			self.transport.files["catalog"] = catalog_text(range(1, 5), cost="1.00")
			self.assertIsNone(sync.run_lane(TCODE, "catalog", force=True))
		self.assertEqual(_runs()[-1], (f"{TCODE}_catalog", "failed", "ValueError"))
		self.assertNotEqual(float(_offer(sku(1)).cost), 1.0)  # live rows untouched
		# a download cut mid-line: one line of the wrong width in 12 is over 1%
		self.transport.files["catalog"] = catalog_text(range(1, 12), cost="2.00") \
			+ catalog_line(12)[:150]
		self.assertIsNone(sync.run_lane(TCODE, "catalog", force=True))
		self.assertEqual(_runs()[-1], (f"{TCODE}_catalog", "failed", "FeedFormatError"))
		self.assertNotEqual(float(_offer(sku(1)).cost), 2.0)

	def test_ffl_comes_from_rsr_departments_and_unknown_means_ffl(self):
		"""AC 7 for RSR: its firearm departments are FFL-required, its accessory
		departments are not, and an unlisted or blank department is (the safe
		default). A firearm department's row stays a firearm whatever its title,
		and later lanes and the product layer never downgrade it."""
		over = {1: {}, 2: {"department": ""}, 3: {"department": "77"},
			4: {"department": "01", "title": "ACME TEST HOLSTER RIG"}}
		self.transport.files["catalog"] = catalog_text(CATALOG_N, over=over)
		sync.run_house(TCODE)
		got = {n: (_offer(sku(n)).ffl_required, _offer(sku(n)).product_class) for n in (1, 2, 3, 4, 5)}
		# 2 (blank) and 3 (unlisted): FFL, so a firearm class whatever the title (an
		# ammo and a rifle title); 4: the department decides, not the holster title
		self.assertEqual(got, {1: (0, "Accessory"), 2: (1, "Other Firearm"), 3: (1, "Other Firearm"),
			4: (1, "Handgun"), 5: (1, "Handgun")})
		self.assertEqual(_offer(sku(1)).department_name, "Holsters/Pouches")
		self.transport.files["deleted"] = deleted_text([sku(4)])
		self.assertEqual(sync.run_lane(TCODE, "deleted", force=True)["flagged"], 1)
		sync.run_house(TCODE, force=True)
		self.assertEqual(_offer(sku(4)).ffl_required, 1)
		self.assertTrue(LocalCatalog().item_by_upc(upc(4))["product"]["ffl_required"])
		self.assertTrue(LocalCatalog().rows(TCODE, [sku(4)])[sku(4)]["ffl_required"])
		facets = {d["department"]: d["name"] for d in LocalCatalog().products_facets(TCODE)["departments"]}
		self.assertEqual(facets["14"], "Holsters/Pouches")

	def test_an_rsr_holster_can_drop_ship_to_a_home(self):
		"""As with the Data Service: an accessory department is not FFL-required,
		so an RSR holster can go to a buyer's door; a handgun needs an FFL; an
		upper (41/42/43) is FFL-required Gun Parts — a dealer-only part, never
		home drop-ship. A lower in an upper department is a firearm, so it is a
		legal firearm and not a dealer-only part."""
		from ffl_integrations.distributor.util import dealer_only_part, legal_firearm

		self.transport.files["catalog"] = catalog_text(CATALOG_N,
			over={2: {"department": "42", "title": "ACME TEST AR15 SBR UPPER 10.5IN"},
				3: {"department": "41", "title": "ACME TEST AR15 STRIPPED LOWER RECEIVER"}})
		sync.run_house(TCODE)
		with patch(LOCAL, return_value=True):
			rows = LocalCatalog().rows(TCODE, [sku(1), sku(5), sku(2), sku(3)])
			holster, handgun, upper = rows[sku(1)], rows[sku(5)], rows[sku(2)]
			lower = rows[sku(3)]
			self.assertEqual((lower["ffl_required"], lower["product_class"]), (True, "Other Firearm"))
			self.assertTrue(legal_firearm(lower, "rsr"))
			self.assertFalse(dealer_only_part(lower, "rsr"))
			self.assertFalse(holster["ffl_required"])
			self.assertFalse(dealer_only_part(holster, "rsr"))
			self.assertFalse(legal_firearm(holster, "rsr"))
			self.assertTrue(legal_firearm(handgun, "rsr"))
			self.assertFalse(dealer_only_part(handgun, "rsr"))
			self.assertEqual((upper["ffl_required"], upper["product_class"]), (True, "Gun Parts"))
			self.assertFalse(legal_firearm(upper, "rsr"))
			self.assertTrue(dealer_only_part(upper, "rsr"))

	def test_one_and_zero_one_are_one_department(self):
		self.transport.files["catalog"] = catalog_text(CATALOG_N, over={1: {"department": "1"}})
		sync.run_house(TCODE)
		self.assertEqual(_offer(sku(1)).department, "01")
		self.assertEqual(_offer(sku(5)).department, "01")
		deps = [d for d in LocalCatalog().products_facets(TCODE)["departments"] if d["distributor"] == TCODE]
		self.assertEqual([d["products"] for d in deps if d["department"] == "01"], [3])
		self.assertTrue(all(d["department"] != "1" for d in deps))
		page = LocalCatalog().products_page({"department": [f"{TCODE}:01"], "limit": 100})
		self.assertEqual({p["key"] for p in page["products"]}, {upc(1), upc(5), upc(10)})

	def test_a_shifted_row_is_not_written(self):
		shifted = catalog_line(500).replace(";", ";EXTRA;", 1)
		lines = catalog_text(range(1, 100)) + shifted + "\n" + catalog_text(range(100, 120))
		self.transport.files["catalog"] = lines
		self.assertEqual(sync.run_lane(TCODE, "catalog")["total"], 119)
		self.assertEqual(frappe.db.sql("""SELECT COUNT(*) FROM dc_offer WHERE distributor = %s
			AND sku = %s""", (TCODE, sku(500)))[0][0], 0)

	def test_a_truncated_restrictions_file_is_refused_unless_forced(self):
		sync.run_house(TCODE)
		self.transport.files["restrictions"] = ""
		self.assertIsNone(sync.run_lane(TCODE, "restrictions", force=True))  # empty: refused even forced
		self.assertEqual(_runs()[-1], (f"{TCODE}_restrictions", "failed", "FeedFormatError"))
		last = "SELECT status, error_msg FROM dc_sync_run WHERE source = %s ORDER BY id DESC LIMIT 1"
		with patch.object(store, "PARTIAL_FILE_FLOOR", 4):
			_age_runs(f"{TCODE}_restrictions", 3)
			self.transport.files["restrictions"] = restrictions_text(RESTRICTED[:1])
			self.assertIsNone(sync.run_lane(TCODE, "restrictions"))  # scheduled: refused
			self.assertEqual(_runs()[-1], (f"{TCODE}_restrictions", "failed", "ValueError"))
			self.assertIn("force=True", frappe.db.sql(last, (f"{TCODE}_restrictions",))[0][1])
			self.assertEqual(LocalCatalog().restrictions_entries(TCODE, sku(2)),
				[["COOK", "IL", "T", "Y"], ["DEERFIELD", "IL", "C", "L"]])
			self.assertEqual(frappe.db.sql("SELECT restrictions_lines FROM dc_meta WHERE distributor = %s",
				(TCODE,))[0][0], 4)
			out = sync.run_lane(TCODE, "restrictions", force=True)  # a forced run from bench: loaded, with a warning
			self.assertEqual(out["rows_written"], 1)
			self.assertIn("looks partial", out["warning"])
			status, msg = frappe.db.sql(last, (f"{TCODE}_restrictions",))[0]
			self.assertEqual(status, "success")
			self.assertIn("loaded anyway", msg)
		self.assertEqual(LocalCatalog().restrictions_entries(TCODE, sku(2)), [])
		self.assertEqual(frappe.db.sql("SELECT restrictions_lines FROM dc_meta WHERE distributor = %s",
			(TCODE,))[0][0], 1)

	def test_sync_now_never_loads_a_shrunk_restrictions_file(self):
		sync.run_house(TCODE)
		with patch.object(store, "PARTIAL_FILE_FLOOR", 4):
			# Not aged: the lane is not due, so only the skipped schedule lets it run.
			self.transport.files["restrictions"] = restrictions_text(RESTRICTED[:1])
			self.assertIsNone(sync.run_lane(TCODE, "restrictions", skip_schedule=True))
			self.assertEqual(_runs()[-1], (f"{TCODE}_restrictions", "failed", "ValueError"))
			self.assertEqual(LocalCatalog().restrictions_entries(TCODE, sku(2)),
				[["COOK", "IL", "T", "Y"], ["DEERFIELD", "IL", "C", "L"]])
			self.assertEqual(sync.run_lane(TCODE, "restrictions", force=True)["rows_written"], 1)
		self.assertEqual(sync.run_lane(TCODE, "deleted"), {"skipped": "not_due"})
		self.assertIn("flagged", sync.run_lane(TCODE, "deleted", skip_schedule=True))

	def test_a_deleted_lane_that_failed_in_the_catalog_job_is_retried_alone(self):
		"""The deleted list cannot be read: the catalog still loads (a SKU already Deleted
		stays Deleted), the deleted lane records the failure, and the next tick queues it
		alone; while the catalog itself is due, the deleted lane rides its job instead."""
		sync.run_house(TCODE)
		self.transport.files["deleted"] = deleted_text([GONE, sku(3)])
		self.assertEqual(sync.run_lane(TCODE, "deleted", force=True)["flagged"], 1)
		_age_runs(f"{TCODE}_catalog", 3)
		self.transport.fail["deleted"] = sync.FeedUnavailable("RSR FTPS download of rsrdeletedinv.txt failed: 550")
		self.transport.calls.clear()
		seqs = _offer_seqs()
		out = sync.run_lane(TCODE, "catalog")
		self.assertEqual(out["total"], 12)
		self.assertEqual(self.transport.calls, ["deleted", "catalog", "deleted"])
		self.assertEqual(_runs()[-2:], [(f"{TCODE}_catalog", "success", ""),
			(f"{TCODE}_deleted", "failed", "FeedUnavailable")])
		gone = _offer(sku(3))
		self.assertEqual((gone.discontinued, gone.status_flag, gone.qty), (1, "Deleted", 0))
		self.assertEqual(_offer_seqs(), seqs)
		sync.unregister("rsr")  # the shipped driver sits this one out
		self.addCleanup(sync.register, rsr.DRIVER)

		def tick():
			with patch("ffl_integrations.catalog_local.sync.is_local", return_value=True), \
					patch.object(sync, "worker_running", return_value=True), \
					patch.object(sync.Driver, "configured", return_value=True), \
					patch.object(rsr, "feed_username", return_value="u"), \
					patch.object(sync, "_heavy_in_flight", return_value=set()), \
					patch.object(frappe, "enqueue", return_value=object()):
				return sync.tick()

		out = tick()
		self.assertIn(f"{TCODE}_deleted", out["enqueued"])
		self.assertNotIn(f"{TCODE}_catalog", out["enqueued"])
		# the retry downloads the file itself
		del self.transport.fail["deleted"]
		self.transport.calls.clear()
		self.assertEqual(sync.run_lane(TCODE, "deleted"), {"flagged": 0, "total": 2})
		self.assertEqual(self.transport.calls, ["deleted"])
		self.assertEqual(sync.run_lane(TCODE, "deleted"), sync.NOT_DUE)
		# catalog due and deleted due: the deleted lane is not queued beside the catalog
		frappe.db.sql("DELETE FROM dc_sync_run WHERE source = %s AND status = 'success'",
			(f"{TCODE}_deleted",))
		frappe.db.commit()
		_age_runs(f"{TCODE}_catalog", 3)
		out = tick()
		self.assertIn(f"{TCODE}_catalog", out["enqueued"])
		self.assertNotIn(f"{TCODE}_deleted", out["enqueued"] + out["deduplicated"] + out["yielded"])

	def test_auth_failure_fails_its_lane_and_the_rest_still_run(self):
		self.transport.fail["quantity"] = sync.FeedAuthError("RSR FTPS rejected the login (530)")
		result = sync.run_house(TCODE)
		self.assertEqual(result["_failed"], [f"{TCODE}_quantity"])
		self.assertEqual([r[1:] for r in _runs()], [("success", ""), ("success", ""),
			("failed", "FeedAuthError"), ("success", "")])
		status = LocalCatalog().ops_status(TCODE)
		self.assertEqual(status["last_failure"]["error_class"], "FeedAuthError")

	def test_tick_queues_the_lanes_and_an_auth_failure_does_not_stop_the_other_house(self):
		"""Through the tick: this house's catalog first, then its quantity; one
		heavy lane per tick; its every lane failing auth leaves the other house
		syncing."""
		sync.unregister("rsr")  # the shipped driver sits this one out
		self.addCleanup(sync.register, rsr.DRIVER)
		other = catalog_testhouse.install()
		self.addCleanup(catalog_testhouse.uninstall)
		self.addCleanup(catalog_testhouse.purge)
		for kind in LANES:
			self.transport.fail[kind] = sync.FeedAuthError("rejected")
		creds = rsr.FeedCredentials("h", 1, "u", _password=_mark("rsr-ftp"))
		queued = []
		with patch("ffl_integrations.catalog_local.sync.is_local", return_value=True), \
				patch.object(sync, "worker_running", return_value=True), \
				patch.object(sync.Driver, "configured", return_value=True), \
				patch.object(rsr, "feed_username", return_value="u"), \
				patch.object(rsr, "credentials", return_value=creds), \
				patch.object(sync, "_heavy_in_flight", return_value=set()), \
				patch.object(frappe, "enqueue",
					side_effect=lambda m, **kw: queued.append(kw) or object()):
			out = sync.tick()
		self.assertEqual(out["enqueued"], [f"{TCODE}_catalog", f"{TCODE}_quantity",
			f"{catalog_testhouse.CODE}_quantity"])
		self.assertEqual(out["yielded"], [f"{TCODE}_restrictions", f"{catalog_testhouse.CODE}_snapshot"])
		self.assertEqual({kw["lane"]: kw["timeout"] for kw in queued if kw["code"] == TCODE},
			{"catalog": sync.HEAVY_TIMEOUT_SECONDS, "quantity": sync.QUANTITY_TIMEOUT_SECONDS})
		for kw in queued:
			sync.run_lane(kw["code"], kw["lane"])
		self.assertEqual(_runs(), [(f"{TCODE}_catalog", "failed", "FeedAuthError"),
			(f"{TCODE}_quantity", "failed", "FeedAuthError")])
		self.assertEqual(_runs(catalog_testhouse.CODE),
			[(f"{catalog_testhouse.CODE}_quantity", "success", "")])
		self.assertEqual(other.calls, ["quantity"])

	def test_contract_check_passes_against_an_rsr_fed_local_catalog(self):
		sync.run_house(TCODE)
		results = check(StubTransport(LocalCatalog()), TCODE, sku(1))
		failed = [r for r in results if not r["ok"]]
		self.assertEqual(failed, [], "\n".join(f"{r['name']}: {r['detail']}" for r in failed))

	def test_the_catalog_lane_streams(self):
		"""AC 6 in the suite: traced memory of parser + load barely grows from 2k
		to 20k rows — a materialised list would add ~50 MiB (the 200k-row RSS
		measurement is ``rsr_synth.measure``)."""
		fd, small = tempfile.mkstemp(suffix="-rsr-stream-2k")
		os.close(fd)
		fd, big = tempfile.mkstemp(suffix="-rsr-stream-20k")
		os.close(fd)
		self.addCleanup(os.unlink, small)
		self.addCleanup(os.unlink, big)
		write_catalog(small, 2_000, long_text=300)
		write_catalog(big, 20_000, long_text=300)
		store.load_catalog(rsr_feed.iter_catalog(small), distributor=TCODE)  # warm imports and caches
		peaks = {}
		for name, path in (("small", small), ("big", big)):
			_purge()
			tracemalloc.start()
			try:
				out = store.load_catalog(rsr_feed.iter_catalog(path), distributor=TCODE)
				peaks[name] = tracemalloc.get_traced_memory()[1]
			finally:
				tracemalloc.stop()
		self.assertEqual(out["created"], 20_000)
		growth = peaks["big"] - peaks["small"]
		self.assertLess(growth, 16 << 20, f"traced peak grew {growth >> 20} MiB for 18k more rows")

	def test_a_deleted_flag_in_any_case_or_spacing_is_deleted(self):
		"""store.is_deleted / deleted_sql are the one Deleted check: a flag stored as
		" deleted " is Deleted for search, health, ops, the quantity lane, the best offer,
		the product layer and a remote catalog row."""
		from ffl_integrations.catalog_local import backend
		from ffl_integrations.distributor.remote_catalog_mixin import remote_normalised

		sync.run_house(TCODE)
		frappe.db.sql("UPDATE dc_offer SET status_flag = %s WHERE distributor = %s AND sku = %s",
			(" deleted ", TCODE, sku(3)))
		frappe.db.commit()
		local = LocalCatalog()
		self.assertEqual(local.search(TCODE, sku(3), 5), [])
		self.assertEqual(local.ops_flags(TCODE)["deleted"], 1)
		self.assertEqual(local.health(TCODE)["offers_active"], 11)
		# the quantity lane keeps the terminal flag and publishes no stock
		store.apply_quantity([(sku(3), 9, None, "Active")], distributor=TCODE)
		self.assertTrue(store.is_deleted(_offer(sku(3)).status_flag))
		self.assertEqual(_offer(sku(3)).qty, 0)
		self.assertTrue(store.is_deleted(" DELETED "))
		self.assertFalse(store.is_deleted("Allocated"))
		self.assertFalse(store.is_deleted(""))
		self.assertFalse(store.is_deleted(None))
		# the live offer has no stock, so the sort falls to cost: only the Deleted check
		# keeps the cheaper dead offer out
		cheap = {"distributor": TCODE, "sku": "A", "status_flag": " deleted ", "discontinued": False,
			"qty": 5, "cost": 1.0}
		dear = dict(cheap, sku="B", status_flag="", qty=0, cost=9.0)
		self.assertEqual(backend._best([cheap, dear], {})["sku"], "B")
		cols = {"upc": "X", "model": "", "short_description": "", "image_url": "",
			"long_description": "", "ffl_required": 0}
		merged = store._merged([frappe._dict(cheap, manufacturer="GONE CO", **cols),
			frappe._dict(dear, manufacturer="LIVE CO", **cols)], {}, None)
		self.assertEqual(merged["manufacturer"], "LIVE CO")
		remote = {"sku": "A", "upc": "", "manufacturer": "", "model": "", "department": "",
			"cost": 0, "map": 0, "msrp": 0, "qty": 0, "ffl_required": 0, "dropship_blocked": 0,
			"vendor_approval_required": 0, "image_url": "", "discontinued": 0,
			"status_flag": " deleted "}
		self.assertTrue(remote_normalised(remote)["discontinued"])
		self.assertFalse(remote_normalised(dict(remote, status_flag="Active"))["discontinued"])


# ---------------------------------------------------------------- transport

def _fake_ftp(files: dict, *, login_error=None, connect_error=None, retr_error=None,
		short_by: int = 0, sizes: bool = True):
	"""A stand-in for ftplib.FTP_TLS over ``files`` ({name: bytes}); returns the
	class and the call log."""
	log: list = []

	class FakeFTP:
		def __init__(self, context=None, timeout=None):
			log.append(("init", timeout))
			log.append(("context", context))

		def connect(self, host, port, timeout=None):
			log.append(("connect", host, port))
			if connect_error:
				raise connect_error

		def login(self, user, passwd):
			log.append(("login", user))
			if login_error:
				raise login_error

		def prot_p(self):
			log.append(("prot_p",))

		def set_pasv(self, value):
			log.append(("pasv", value))

		def cwd(self, directory):
			log.append(("cwd", directory))

		def voidcmd(self, cmd):
			log.append(("voidcmd", cmd))

		def size(self, name):
			if not sizes:
				raise ftplib.error_perm("500 SIZE not understood")
			return len(files[name])

		def retrbinary(self, cmd, callback, blocksize=8192):
			log.append(("retr", cmd))
			if retr_error:
				raise retr_error
			data = files[cmd.split(" ", 1)[1]]
			callback(data[:len(data) - short_by])

		def nlst(self):
			log.append(("nlst",))
			return sorted(files)

		def quit(self):
			log.append(("quit",))

		def close(self):
			log.append(("close",))

	return FakeFTP, log


class TestFtpsTransport(FrappeTestCase):
	def creds(self, **over):
		values = {"host": "ftp.example.invalid", "port": 2222, "username": "feeduser",
			"_password": _mark("rsr-ftp"), "key_dealer": False}
		values.update(over)
		return rsr.FeedCredentials(**values)

	def test_download_streams_to_a_temp_file_after_a_disk_check(self):
		ftp, log = _fake_ftp({"IM-QTY-CSV.csv": b"ZZR-000001,4\n"})
		with patch.object(rsr, "FTP_TLS", ftp):
			path = rsr.FtpsTransport(self.creds()).download("quantity")
		self.addCleanup(os.unlink, path)
		with open(path, "rb") as fh:
			self.assertEqual(fh.read(), b"ZZR-000001,4\n")
		self.assertEqual([e[0] for e in log], ["init", "context", "connect", "login", "prot_p", "pasv",
			"cwd", "voidcmd", "retr", "quit"])
		context = next(e[1] for e in log if e[0] == "context")
		self.assertEqual((context.verify_mode, context.check_hostname), (ssl.CERT_REQUIRED, True))
		self.assertIn(("cwd", "ftpdownloads"), log)
		self.assertIn(("connect", "ftp.example.invalid", 2222), log)

	def test_the_server_certificate_is_verified_unless_skipped_in_settings(self):
		context = rsr.tls_context(self.creds())
		self.assertEqual((context.verify_mode, context.check_hostname), (ssl.CERT_REQUIRED, True))
		with patch.object(rsr, "_log") as log:
			context = rsr.tls_context(self.creds(skip_tls_verify=True))
		self.assertEqual((context.verify_mode, context.check_hostname), (ssl.CERT_NONE, False))
		self.assertIn("certificate check skipped", log.return_value.warning.call_args.args[0])

	def test_a_key_dealer_reads_keydealer_and_its_own_catalog_name(self):
		ftp, log = _fake_ftp({"rsrinventory-keydlr-new.txt": b"x\n"})
		with patch.object(rsr, "FTP_TLS", ftp):
			path = rsr.FtpsTransport(self.creds(key_dealer=True)).download("catalog")
		os.unlink(path)
		self.assertIn(("cwd", "keydealer"), log)
		self.assertIn(("retr", "RETR rsrinventory-keydlr-new.txt"), log)

	def test_a_short_transfer_is_refused_and_cleaned_up(self):
		ftp, _log = _fake_ftp({"IM-QTY-CSV.csv": b"ZZR-000001,4\nZZR-000002,5\n"}, short_by=5)
		before = set(os.listdir(tempfile.gettempdir()))
		with patch.object(rsr, "FTP_TLS", ftp), self.assertRaises(sync.FeedUnavailable) as cm:
			rsr.FtpsTransport(self.creds()).download("quantity")
		self.assertIn("cut short", str(cm.exception))
		self.assertEqual({n for n in set(os.listdir(tempfile.gettempdir())) - before
			if n.startswith("rsr-feed-")}, set())

	def test_no_room_on_disk_refuses_before_downloading(self):
		ftp, log = _fake_ftp({"IM-QTY-CSV.csv": b"x" * 100})
		usage = types.SimpleNamespace(total=1 << 30, used=1 << 30, free=1 << 20)
		with patch.object(rsr, "FTP_TLS", ftp), patch.object(rsr.shutil, "disk_usage",
				return_value=usage), self.assertRaises(sync.FeedUnavailable) as cm:
			rsr.FtpsTransport(self.creds()).download("quantity")
		self.assertIn("not enough free disk", str(cm.exception))
		self.assertNotIn("retr", [e[0] for e in log])
		# a server that will not tell the size is assumed large
		ftp, log = _fake_ftp({"IM-QTY-CSV.csv": b"x"}, sizes=False)
		usage = types.SimpleNamespace(total=1 << 40, used=0, free=600 << 20)
		with patch.object(rsr, "FTP_TLS", ftp), patch.object(rsr.shutil, "disk_usage",
				return_value=usage), self.assertRaises(sync.FeedUnavailable):
			rsr.FtpsTransport(self.creds()).download("quantity")

	def test_failures_map_to_the_sync_errors(self):
		cases = [
			({"login_error": ftplib.error_perm("530 Login incorrect.")}, sync.FeedAuthError),
			({"connect_error": ConnectionRefusedError(111, "Connection refused")}, sync.FeedUnavailable),
			({"connect_error": TimeoutError("timed out")}, sync.FeedUnavailable),
			({"retr_error": ftplib.error_perm("550 No such file.")}, sync.FeedUnavailable),
			({"retr_error": EOFError()}, sync.FeedUnavailable),
		]
		for kwargs, error in cases:
			with self.subTest(kwargs):
				ftp, log = _fake_ftp({"IM-QTY-CSV.csv": b"x\n"}, **kwargs)
				with patch.object(rsr, "FTP_TLS", ftp), self.assertRaises(error) as cm:
					rsr.FtpsTransport(self.creds()).download("quantity")
				self.assertNotIn(_mark("rsr-ftp"), str(cm.exception))
				self.assertEqual(log[-1][0] in ("quit", "close"), True)
		self.assertIs(type(cm.exception), sync.FeedUnavailable)
		self.assertTrue(issubclass(sync.FeedAuthError, sync.FeedUnavailable))

	def test_a_certificate_failure_names_the_skip_box(self):
		bad_cert = ssl.SSLCertVerificationError(1, "certificate verify failed")
		bad_cert.verify_message = "self-signed certificate in certificate chain"
		ftp, _log = _fake_ftp({}, login_error=bad_cert)
		with patch.object(rsr, "FTP_TLS", ftp), self.assertRaises(sync.FeedUnavailable) as cm:
			rsr.FtpsTransport(self.creds()).download("quantity")
		self.assertIs(type(cm.exception), sync.FeedUnavailable)
		for text in ("did not verify", "self-signed certificate in certificate chain",
				'"Skip TLS certificate check" in RSR Settings'):
			self.assertIn(text, str(cm.exception))
		# Test Connection says the same
		from ffl_integrations.distributor.rsr_provider import RSRProvider

		with patch.object(rsr, "FTP_TLS", ftp), patch.object(rsr, "credentials",
				return_value=self.creds()):
			out = RSRProvider.feed_login_check()
		self.assertFalse(out["ok"])
		self.assertEqual(out["detail"], str(cm.exception))

	def test_probe_logs_in_and_lists_without_downloading(self):
		ftp, log = _fake_ftp({"a.txt": b"1", "b.txt": b"2", "IM-QTY-CSV.csv": b"3"})
		with patch.object(rsr, "FTP_TLS", ftp):
			self.assertEqual(rsr.FtpsTransport(self.creds()).probe(),
				{"directory": "ftpdownloads", "files": 3})
		self.assertNotIn("retr", [e[0] for e in log])

	def test_no_credentials_is_feed_unavailable(self):
		with patch.object(rsr, "credentials", return_value=None), \
				self.assertRaises(sync.FeedUnavailable):
			rsr.FtpsTransport().download("quantity")

	def test_credentials_never_show_in_a_repr_a_traceback_or_a_run_row(self):
		creds = self.creds()
		self.assertNotIn(_mark("rsr-ftp"), repr(creds))
		ftp, _log = _fake_ftp({}, login_error=ftplib.error_perm("530 Login incorrect."))
		with patch.object(rsr, "FTP_TLS", ftp):
			try:
				rsr.FtpsTransport(creds).download("quantity")
			except sync.FeedAuthError:
				tb = frappe.get_traceback(with_context=True)
			else:
				self.fail("expected FeedAuthError")
		self.assertIn("in download", tb)
		self.assertNotIn(_mark("rsr-ftp"), tb)
		# through a lane: the dc_sync_run row keeps the message, never the password
		_purge()
		self.addCleanup(_purge)
		driver = sync.register(rsr.RsrDriver(code=TCODE, transport=rsr.FtpsTransport(creds)))
		self.addCleanup(sync.unregister, TCODE)
		with patch.object(rsr, "FTP_TLS", ftp):
			sync.run_lane(TCODE, "quantity")
		self.assertIs(sync.DRIVERS[TCODE], driver)
		row = frappe.db.sql("""SELECT error_class, error_msg FROM dc_sync_run WHERE source = %s""",
			(f"{TCODE}_quantity",))[0]
		self.assertEqual(row[0], "FeedAuthError")
		self.assertIn("530", row[1])
		self.assertNotIn(_mark("rsr-ftp"), row[1])


class TestRsrSettingsFeed(FrappeTestCase):
	"""RSR Settings › Catalog feed: credentials, configured(), Test Connection."""

	def setUp(self):
		from frappe.utils.password import get_decrypted_password

		names = ("ftp_host", "ftp_port", "ftp_username", "key_dealer")
		before = {f: frappe.db.get_single_value("RSR Settings", f) for f in names}
		pw_before = get_decrypted_password("RSR Settings", "RSR Settings", "ftp_password",
			raise_exception=False)
		self.addCleanup(self._restore, before, pw_before)

	def _restore(self, before, pw_before):
		from frappe.utils.password import remove_encrypted_password, set_encrypted_password

		for field, value in before.items():
			frappe.db.set_single_value("RSR Settings", field, value)
		if pw_before:
			set_encrypted_password("RSR Settings", "RSR Settings", pw_before, "ftp_password")
		else:
			remove_encrypted_password("RSR Settings", "RSR Settings", "ftp_password")
		frappe.db.commit()

	def _save(self, username, password, **extra):
		from frappe.utils.password import remove_encrypted_password, set_encrypted_password

		frappe.db.set_single_value("RSR Settings", "ftp_username", username)
		for field, value in extra.items():
			frappe.db.set_single_value("RSR Settings", field, value)
		if password:
			set_encrypted_password("RSR Settings", "RSR Settings", password, "ftp_password")
		else:
			remove_encrypted_password("RSR Settings", "RSR Settings", "ftp_password")
		frappe.clear_document_cache("RSR Settings", "RSR Settings")

	def _api_settings(self, **singles):
		"""Direct Connect on, with readable dealer and fulfillment passwords; all of it
		restored after the test."""
		from frappe.utils.password import (get_decrypted_password, remove_encrypted_password,
			set_encrypted_password)

		api = ("dealer_password", "fulfillment_password")
		before = {f: frappe.db.get_single_value("RSR Settings", f) for f in singles}
		pw_before = {f: get_decrypted_password("RSR Settings", "RSR Settings", f,
			raise_exception=False) for f in api}

		def restore():
			for f, v in before.items():
				frappe.db.set_single_value("RSR Settings", f, v)
			for f, v in pw_before.items():
				if v:
					set_encrypted_password("RSR Settings", "RSR Settings", v, f)
				else:
					remove_encrypted_password("RSR Settings", "RSR Settings", f)
			frappe.clear_document_cache("RSR Settings", "RSR Settings")

		self.addCleanup(restore)
		for f, v in singles.items():
			frappe.db.set_single_value("RSR Settings", f, v)
		for f in api:
			set_encrypted_password("RSR Settings", "RSR Settings", _mark(f), f)
		frappe.clear_document_cache("RSR Settings", "RSR Settings")

	def _spoil(self, *fields):
		"""The stored passwords cannot be decrypted, as after a change of the site's
		encryption key; messages are unmuted and the log emptied so the test reads it."""
		frappe.db.sql("""UPDATE `__Auth` SET `password` = %s WHERE `doctype` = 'RSR Settings'
			AND `name` = 'RSR Settings' AND `fieldname` IN %s""",
			("gAAAAABnot-a-valid-fernet-token", fields))
		frappe.clear_document_cache("RSR Settings", "RSR Settings")
		muted = frappe.flags.mute_messages
		frappe.flags.mute_messages = False
		self.addCleanup(setattr, frappe.flags, "mute_messages", muted)
		frappe.clear_messages()

	def _decrypt_messages(self):
		return [m for m in map(str, frappe.get_message_log()) if "Encryption key" in m]

	def test_credentials_read_rsr_settings_with_defaults(self):
		self._save("", "")
		self.assertIsNone(rsr.credentials())
		self._save("feeduser", "")
		self.assertIsNone(rsr.credentials())
		self._save("feeduser", _mark("rsr-ftp"), ftp_host="", ftp_port=0, key_dealer=1)
		creds = rsr.credentials()
		self.assertEqual((creds.host, creds.port, creds.username, creds.key_dealer),
			("ftp.rsrgroup.com", 2222, "feeduser", True))
		self.assertEqual(creds.password, _mark("rsr-ftp"))
		self.assertEqual(creds.feed_dir, "keydealer")

	def test_configured_needs_the_feed_username(self):
		with patch.object(sync.Driver, "configured", return_value=True):
			self._save("", "")
			self.assertFalse(rsr.DRIVER.configured())
			self._save("feeduser", "")  # the lanes then fail out loud
			self.assertTrue(rsr.DRIVER.configured())
			self._save("feeduser", _mark("rsr-ftp"))
			self.assertTrue(rsr.DRIVER.configured())
		with patch.object(sync.Driver, "configured", return_value=False):
			self.assertFalse(rsr.DRIVER.configured())  # RSR not enabled as a distributor

	def test_an_undecryptable_password_is_a_failed_run_not_a_skip(self):
		"""A password that is saved but cannot be decrypted (here: the stored
		value corrupted in __Auth) is FeedAuthError "enter it again" — a failed
		run and the Test Connection detail; no saved password stays None."""
		from ffl_integrations.distributor.rsr_provider import RSRProvider

		self._save("feeduser", _mark("rsr-ftp"))
		frappe.db.sql("""UPDATE `__Auth` SET `password` = %s WHERE `doctype` = 'RSR Settings'
			AND `name` = 'RSR Settings' AND `fieldname` = 'ftp_password'""",
			("gAAAAABnot-a-valid-fernet-token",))
		frappe.clear_document_cache("RSR Settings", "RSR Settings")
		with self.assertRaises(sync.FeedAuthError) as cm:
			rsr.credentials()
		self.assertIn("enter it again", str(cm.exception))
		_purge()
		self.addCleanup(_purge)
		sync.register(rsr.RsrDriver(code=TCODE))  # the real transport over RSR Settings
		self.addCleanup(sync.unregister, TCODE)
		with patch.object(sync.Driver, "configured", return_value=True):
			self.assertTrue(sync.DRIVERS[TCODE].configured())
		self.assertIsNone(sync.run_lane(TCODE, "quantity"))
		row = frappe.db.sql("SELECT status, error_class, error_msg FROM dc_sync_run WHERE source = %s",
			(f"{TCODE}_quantity",))[0]
		self.assertEqual(row[:2], ("failed", "FeedAuthError"))
		self.assertEqual(row[2], f"FeedAuthError: {rsr.UNDECRYPTABLE}")
		self.assertEqual(RSRProvider.feed_login_check(),
			{"label": "Catalog feed (FTPS)", "ok": False, "detail": rsr.UNDECRYPTABLE})
		self._save("feeduser", "")  # nothing saved: not an error, just no login
		self.assertIsNone(rsr.credentials())

	def test_shipped_driver_registers_with_the_five_lanes(self):
		self.assertIs(sync.load_drivers()["rsr"], rsr.DRIVER)
		self.assertEqual([(ln.name, ln.heavy) for ln in rsr.DRIVER.lanes()],
			[("catalog", True), ("deleted", False), ("quantity", False), ("restrictions", True),
				("case_price", False)])
		self.assertEqual(rsr.DRIVER.lane("catalog").timeout, sync.HEAVY_TIMEOUT_SECONDS)
		self.assertEqual(rsr.DRIVER.lane("deleted").follows, "catalog")

	def test_connection_test_probes_the_feed_only_in_local_mode(self):
		from ffl_integrations.distributor.rsr_provider import RSRProvider

		self._save("feeduser", _mark("rsr-ftp"))
		ftp, log = _fake_ftp({"a": b"", "b": b""})
		health = {"label": "Catalog service", "ok": True, "detail": ""}
		with patch.object(rsr, "FTP_TLS", ftp), \
				patch.object(RSRProvider, "catalog_health_check", return_value=health):
			with patch(LOCAL, return_value=True):
				checks = RSRProvider().connection_test()["checks"]
			feed = next(c for c in checks if c["label"] == "Catalog feed (FTPS)")
			self.assertEqual(feed, {"label": "Catalog feed (FTPS)", "ok": True,
				"detail": "Logged in; 2 files in ftpdownloads/."})
			self.assertNotIn("retr", [e[0] for e in log])
			with patch(LOCAL, return_value=False):
				labels = [c["label"] for c in RSRProvider().connection_test()["checks"]]
			self.assertNotIn("Catalog feed (FTPS)", labels)
		bad, _log = _fake_ftp({}, login_error=ftplib.error_perm("530 Login incorrect."))
		with patch.object(rsr, "FTP_TLS", bad):
			out = RSRProvider.feed_login_check()
		self.assertFalse(out["ok"])
		self.assertIn("rejected the login", out["detail"])
		self.assertNotIn(_mark("rsr-ftp"), str(out))
		self._save("", "")
		self.assertIn("no FTP username", RSRProvider.feed_login_check()["detail"])

	def test_the_form_shows_the_feed_section_only_in_local_mode(self):
		meta = frappe.get_meta("RSR Settings")
		self.assertEqual(meta.get_field("section_catalog_feed").depends_on,
			"eval:doc.__onload && doc.__onload.catalog_local")
		self.assertEqual(meta.get_field("ftp_password").fieldtype, "Password")
		doc = frappe.get_single("RSR Settings")
		for local in (True, False):
			with patch(LOCAL, return_value=local):
				doc.run_method("onload")
			self.assertIs(doc.get_onload().get("catalog_local"), local)

	def test_test_connection_reports_an_undecryptable_feed_password_once(self):
		"""Local mode, only the feed password cannot be decrypted: Test Connection shows
		our UNDECRYPTABLE on the feed leg and no "Encryption key is invalid" message of
		Frappe's next to it."""
		from ffl_integrations.distributor.rsr_provider import RSRProvider
		from ffl_integrations.rsr.api_client import RSRClient

		self._save("feeduser", _mark("rsr-ftp"))
		self._api_settings(api_enabled=1, dealer_account="TESTDEALER",
			fulfillment_account="TESTFULFIL")
		self._spoil("ftp_password")
		# the test can see Frappe's message: a plain read leaves it in the log
		self.assertIsNone(frappe.get_single("RSR Settings").get_password("ftp_password",
			raise_exception=False))
		self.assertNotEqual(self._decrypt_messages(), [])
		frappe.clear_messages()
		frappe.msgprint("sentinel-before")
		health = {"label": "Catalog service", "ok": True, "detail": ""}
		with patch(LOCAL, return_value=True), \
				patch.object(RSRProvider, "catalog_health_check", return_value=health), \
				patch.object(RSRClient, "probe_connection", return_value={}):
			checks = RSRProvider().connection_test()["checks"]
		feed = next(c for c in checks if c["label"] == "Catalog feed (FTPS)")
		self.assertEqual(feed, {"label": "Catalog feed (FTPS)", "ok": False,
			"detail": rsr.UNDECRYPTABLE})
		self.assertEqual([c["ok"] for c in checks if c["label"].startswith("Direct Connect")],
			[True, True])
		self.assertEqual(self._decrypt_messages(), [])
		self.assertTrue(any("sentinel-before" in str(m) for m in frappe.get_message_log()))
		self.assertFalse(frappe.flags.mute_messages)

	def test_direct_connect_password_reads_are_unchanged(self):
		"""Only the feed password is read quietly. A Direct Connect password that cannot
		be decrypted still reads as blank with Frappe's own message, and Data Service
		mode's Test Connection still shows that message."""
		from ffl_integrations.distributor.rsr_provider import RSRProvider
		from ffl_integrations.rsr.api_client import RSRClient, RSRConfig

		self._save("feeduser", _mark("rsr-ftp"))
		self._api_settings(api_enabled=1, dealer_account="TESTDEALER",
			fulfillment_account="TESTFULFIL")
		self._spoil("dealer_password")
		cfg = RSRConfig.from_settings()
		self.assertEqual((cfg.dealer_password, cfg.fulfillment_password),
			("", _mark("fulfillment_password")))
		self.assertNotEqual(self._decrypt_messages(), [])
		frappe.clear_messages()
		health = {"label": "Catalog service", "ok": True, "detail": ""}
		with patch(LOCAL, return_value=False), \
				patch.object(RSRProvider, "catalog_health_check", return_value=health), \
				patch.object(RSRClient, "probe_connection", return_value={}):
			labels = [c["label"] for c in RSRProvider().connection_test()["checks"]]
		self.assertNotIn("Catalog feed (FTPS)", labels)
		self.assertIn("Direct Connect (dealer account)", labels)
		self.assertNotEqual(self._decrypt_messages(), [])
		self.assertFalse(frappe.flags.mute_messages)


# ---------------------------------------------------------------- AC 8: the quote's restriction lines

class TestQuoteRestrictionsInLocalMode(FrappeTestCase):
	"""In Local mode the quote panel reads RSR's restrictions through the same
	rsr/restrictions.py path and shows the same lines as with the Data Service.
	Display information, never a gate."""

	ENTRIES = [["ALL", "NY", "S", "O"], ["ALL", "NJ", "S", "F"], ["COOK", "IL", "T", "Y"],
		["DEERFIELD", "IL", "C", "L"]]

	def setUp(self):
		catalog_stub.uninstall()
		if frappe.db.sql("SELECT 1 FROM dc_restriction WHERE distributor = 'rsr' LIMIT 1"):
			self.skipTest("this site holds RSR restriction rows (a Local trial?): the check swaps "
				"RSR's whole set — run it on a clean test site")
		meta = frappe.db.sql("SELECT restrictions_hash FROM dc_meta WHERE distributor = 'rsr'")
		self.addCleanup(self._restore, meta[0][0] if meta else None,
			frappe.db.sql("SELECT COALESCE(MAX(id), 0) FROM dc_sync_run")[0][0])
		self.sku = sku(7)
		self.transport = FakeTransport({"restrictions": restrictions_text(
			[(self.sku, *e) for e in self.ENTRIES])})
		sync.register(rsr.RsrDriver(transport=self.transport))

	def _restore(self, hash_before, last_run_id):
		sync.register(rsr.DRIVER)
		frappe.db.sql("DELETE FROM dc_restriction WHERE distributor = 'rsr'")
		if hash_before is None:
			frappe.db.sql("DELETE FROM dc_meta WHERE distributor = 'rsr'")
		else:
			frappe.db.sql("UPDATE dc_meta SET restrictions_hash = %s WHERE distributor = 'rsr'",
				(hash_before,))
		frappe.db.sql("DELETE FROM dc_sync_run WHERE id > %s AND source LIKE 'rsr\\_%%'",
			(last_run_id,))
		frappe.db.commit()

	def _quote_lines(self):
		from ffl_integrations.distributor.rsr_provider import RSRProvider
		from ffl_integrations.rsr import restrictions

		return (RSRProvider().blocked_states(self.sku),
			restrictions.check(self.sku, state="IL", city="DEERFIELD"),
			restrictions.check(self.sku, state="NY", city="ALBANY"),
			restrictions.check(self.sku, state="TX", city="AUSTIN"))

	def test_local_mode_shows_the_same_restriction_lines_as_the_data_service(self):
		self.assertEqual(sync.run_lane("rsr", "restrictions", force=True)["rows_written"], 1)
		with patch(LOCAL, return_value=True), \
				patch("ffl_integrations.data_service.client.get",
					side_effect=AssertionError("Local mode reached the Data Service")):
			local = self._quote_lines()
		stub = catalog_stub.install()
		self.addCleanup(catalog_stub.uninstall)
		stub.seed_restrictions(self.sku, self.ENTRIES)
		service = self._quote_lines()
		self.assertEqual(local, service)
		self.assertEqual(local[0], ["IL", "NJ"])
		self.assertTrue(local[1]["blocked_dropship"])
		self.assertFalse(local[2]["blocked_dropship"])  # a permit code is not a block


if __name__ == "__main__":
	unittest.main()
