"""No store-named product identifier in the core.

Two checks over ``git ls-files``:

* RETIRED: names the core has replaced must not come back — the ``OSA Category`` DocType
  and its route ``osa-category`` (now ``Catalog Category``), the ``osa_category`` field and
  hooks (now ``catalog_category``),
  the ``"osa-api"`` update source type (now ``"update-service"``) and the ``osa-ext lint``
  command name (the linter runs as ``make ext-lint`` / ``python -m ffl_core.extensions.lint``;
  there is no console script). Searched in the whole tree; the few places that must still say them
  (the rename patch, its test, the legacy reader of the old source type) are in RETIRED_ALLOWED,
  each with its reason. ``osa-api`` is the name of a private repository: it passes only as that
  quoted source-type value (``"osa-api"``, in the two files RETIRED_ALLOWED names) and in this guard;
  prose, a comment or a docstring that names it fails.
* Every other ``osa_…`` / ``osa-…`` token under ``apps/`` must be one the product keeps on
  purpose (KEEP below): the first-party extension ids (their rename waits until after the open
  source launch, owner 2026-10-10), the WooCommerce product meta keys and the ``x-osa-*``
  webhook headers (the contract with the storefront plugin and live product data on the web
  stores), the retired logo's file name (only in negative assertions), and names that only
  live in old patches. A new ``osa_`` name anywhere else fails here: give it a product name.
  A bare prefix (``osa_``, ``osa_sold_``) passes only where prose names a kept key family with
  it, ``_osa_*`` / ``_osa_sold_*`` (PREFIX_FAMILIES): in a Python comment or docstring, or on a
  line PREFIX_ALLOWED names exactly. In code (``"_osa_" + name``, ``"_osa_*"[:-1]``) it is a
  finding, since it would make keys this list never sees.

Patch modules and patches.txt are not read by the second check: a patch keeps the names of the
schema it migrates.

Run it under the bench (``make test``) or with plain Python from the repo root, no bench:
``python3 apps/ffl_core/ffl_core/tests/test_no_retired_product_ids.py --standalone``
(the api-docs job in release-image.yml does this on every PR).
"""
from __future__ import annotations

import fnmatch
import io
import re
import sys
import tokenize
import unittest
from pathlib import Path
from unittest import mock

try:
	from ffl_core.tests.test_no_store_residue import repo_root, tracked_files
except ImportError:                                   # --standalone: plain Python, no bench
	sys.path.insert(0, str(Path(__file__).resolve().parent))
	from test_no_store_residue import repo_root, tracked_files

THIS = "apps/ffl_core/ffl_core/tests/test_no_retired_product_ids.py"

RETIRED: dict[str, re.Pattern] = {
	"OSA Category": re.compile(r"osa[ -]?category", re.IGNORECASE),    # also its route, osa-category
	"osa_category": re.compile(r"osa_category", re.IGNORECASE),
	'"osa-api" source type': re.compile(r"""(["'])osa-api\1""", re.IGNORECASE),   # same quote both sides
	"osa-ext": re.compile(r"(?<![A-Za-z0-9])osa-ext(?![A-Za-z0-9])", re.IGNORECASE),
}

# ``osa-api`` is the name of a private repository, so it may not appear in a comment, a doc or a
# description, anywhere in the tree (not only under apps/). The one place it stays is the stored
# legacy source-type value, QUOTED (``"osa-api"``), in the RETIRED_ALLOWED files for that label;
# the guard itself and the rename patch's files ("*") say it freely.
PRIVATE_NAME = re.compile(r"(?<![A-Za-z0-9_-])osa-api(?![A-Za-z0-9_-])", re.IGNORECASE)


def _quoted(line: str, m: re.Match) -> bool:
	"""The match sits in a string literal's quotes: the same quote right before and after it."""
	return 0 < m.start() and m.end() < len(line) and line[m.start() - 1] == line[m.end()] and line[m.end()] in "\"'"


# (path glob, retired label, reason). Exact paths only where the old name is the point. An
# allowance that no longer allows anything fails the guard, so the list only shrinks.
RETIRED_ALLOWED: list[tuple[str, str, str]] = [
	(THIS, "*", "the guard names what it guards"),
	("apps/ffl_core/ffl_core/patches/rename_catalog_category.py", "*", "the patch renames the old names"),
	("apps/ffl_core/ffl_core/patches.txt", "*", "the patch's registration comment"),
	("apps/ffl_woo_sync/ffl_woo_sync/tests/test_rename_catalog_category.py", "*", "seeds the old shape the patch migrates"),
	("apps/ffl_core/ffl_core/extensions/updates.py", '"osa-api" source type',
		"LEGACY_SOURCE_TYPE: rows written before 1.14.0 still carry it, read as update-service"),
	("apps/ffl_core/ffl_core/tests/test_extension_updates.py", '"osa-api" source type',
		"tests the legacy name's read and write"),
	("apps/ffl_core/ffl_core/catalog_tree.py", "osa_category",
		"LEGACY_JOB_PREFIX: a job queued under its pre-rename id is still a duplicate, for one release"),
]

# Every osa_/osa- token under apps/ must be one of these, exactly (case-insensitive).
KEEP: dict[str, str] = {
	**dict.fromkeys(("osa_consignment", "osa_dropship", "osa_ai", "osa_ca_compliant", "osa_range"),
		"first-party extension ids (their rename waits until after the launch)"),
	**dict.fromkeys(("osa_restricted_states", "osa_serial", "osa_sold_price", "osa_sold_at", "osa_fulfillment_mode",
		"osa_lead_days", "osa_map_price", "osa_map_mode"),
		"WooCommerce product meta keys (_osa_*): the storefront plugin's contract and live product data"),
	**dict.fromkeys(("osa_ca_legal", "osa_compliant_service"),
		"fields of the osa_ca_compliant extension, and the WooCommerce meta keys they push as"),
	**dict.fromkeys(("osa_publish_to_dealer", "osa_dealer_price_override"),
		"retired dealer-store meta keys the push must keep stripping (tests assert they are gone)"),
	"osa_no_underscore": "a test key that must NOT pass the _osa_ meta filter",
	"osa-timestamp": "x-osa-timestamp webhook header: the storefront plugin's contract",
	"osa-signature": "x-osa-signature webhook header: the storefront plugin's contract",
	"osa-logo": "the retired logo's file name: a negative assertion checks that no receipt prints it",
	"osa_tiers": "patch module migrate_condition_grade_to_osa_tiers (historical name), its test",
	**dict.fromkeys(("osa_api_endpoint", "osa_api_client_id", "osa_api_secret"),
		"FFL Settings fields a patch moved to Data Service Settings; its test seeds them"),
}

# A bare prefix is kept only when written as the key family it names: the token, then ``*`` —
# and only in prose: a Python comment or docstring, or a line named here.
PREFIX_FAMILIES: dict[str, str] = {
	"osa_*": "prose naming the WooCommerce _osa_* product meta keys (the kept ones are listed above)",
	"osa_sold_*": "prose naming _osa_sold_price / _osa_sold_at together",
}
# (path, the exact line, stripped) -> reason: a key family named in prose that is not a Python
# comment or docstring. An entry that allows nothing fails the guard, like RETIRED_ALLOWED.
PREFIX_ALLOWED: dict[tuple[str, str], str] = {}

# A whole identifier: osa, a separator, word parts joined by - or _, and a trailing separator
# when the text uses it as a prefix (``"_osa_"``, ``"_osa_sold_"``).
TOKEN = re.compile(r"(?<![A-Za-z0-9])osa[-_](?:[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*)?[-_]?", re.IGNORECASE)
_KEEP = {k.lower() for k in KEEP}
SKIP_PREFIXES = ("docs/api/assets/",)


def _lines(root: Path, rel: str) -> list[tuple[int, str]]:
	try:
		data = (root / rel).read_bytes()
	except OSError:
		return []
	if b"\0" in data:
		return []
	try:
		return list(enumerate(data.decode("utf-8").splitlines(), 1))
	except UnicodeDecodeError:
		return []


def _kept(token: str) -> bool:
	return token.lower() in _KEEP


def _kept_family(line: str, m: re.Match) -> bool:
	return line[m.start():m.end() + 1].lower() in PREFIX_FAMILIES


def _prose(rel: str, lines: list[tuple[int, str]]) -> list[tuple[tuple[int, int], tuple[int, int]]]:
	"""(start, end) of every comment and bare string statement (docstring) of a Python file, as
	(line, column) in ``lines``' numbering; nothing for any other file or one that does not parse."""
	if not rel.endswith(".py"):
		return []
	skip = (tokenize.NL, tokenize.COMMENT)
	try:
		tokens = list(tokenize.generate_tokens(io.StringIO("\n".join(text for _, text in lines)).readline))
	except (tokenize.TokenError, SyntaxError):
		return []
	spans, code = [], [t for t in tokens if t.type not in skip]
	spans += [(t.start, t.end) for t in tokens if t.type == tokenize.COMMENT]
	for i, t in enumerate(code):
		before = code[i - 1].type if i else tokenize.NEWLINE
		after = code[i + 1].type if i + 1 < len(code) else tokenize.ENDMARKER
		if t.type == tokenize.STRING and before in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT) \
				and after in (tokenize.NEWLINE, tokenize.ENDMARKER):
			spans.append((t.start, t.end))
	return spans


def _is_patch(rel: str) -> bool:
	return "/patches/" in rel or rel.endswith("/patches.txt")


def find(root: Path, files: list[str], used: set | None = None) -> list[str]:
	"""Problems; ``used`` collects the RETIRED_ALLOWED entries (index) that allowed something."""
	problems, used = [], set() if used is None else used
	for rel in files:
		if rel.startswith(SKIP_PREFIXES):
			continue
		mine = {i: label for i, (glob, label, _) in enumerate(RETIRED_ALLOWED) if fnmatch.fnmatch(rel, glob)}
		allowed = set(mine.values())
		in_apps = rel.startswith("apps/") and not _is_patch(rel) and "*" not in allowed
		lines, prose = _lines(root, rel), None
		for number, line in lines:
			for m in PRIVATE_NAME.finditer(line):
				if "*" in allowed or (_quoted(line, m) and '"osa-api" source type' in allowed):
					continue
				problems.append(f"{rel}:{number}: private repository name 'osa-api': {line.strip()[:120]}")
			for label, rx in RETIRED.items():
				if not rx.search(line):
					continue
				hit = [i for i, a in mine.items() if a in ("*", label)]
				if hit:
					used.update(hit)
				else:
					problems.append(f"{rel}:{number}: retired name {label!r}: {line.strip()[:120]}")
			if in_apps:
				for m in TOKEN.finditer(line):
					# a retired name this file is allowed to say is judged by RETIRED alone
					if any(RETIRED[a].fullmatch(m.group(0).rstrip("-_")) for a in allowed if a in RETIRED):
						continue
					if m.group(0).lower().rstrip("-_") == "osa-api":    # judged tree-wide, below
						continue
					if _kept(m.group(0)):
						continue
					if _kept_family(line, m):
						if (rel, line.strip()) in PREFIX_ALLOWED:
							used.add((rel, line.strip()))
							continue
						prose = _prose(rel, lines) if prose is None else prose
						if any(a <= (number, m.start()) < b for a, b in prose):
							continue
					problems.append(f"{rel}:{number}: unlisted osa identifier {m.group(0)!r}: {line.strip()[:120]}")
	return problems


def check(root: Path | None = None) -> list[str]:
	root = root or repo_root()
	files = tracked_files(root)
	if files is None:
		raise RuntimeError("git ls-files is not available here")
	used: set = set()
	problems = find(root, files, used)
	for i, (glob, label, _reason) in enumerate(RETIRED_ALLOWED):
		if glob != THIS and i not in used:
			problems.append(f"stale allowance {glob} :: {label} (it allows nothing now)")
	problems += [f"stale allowance {path} :: {line} (it allows nothing now)"
		for path, line in PREFIX_ALLOWED if (path, line) not in used]
	return problems


class NoRetiredProductIds(unittest.TestCase):
	def test_the_core_carries_no_retired_or_unlisted_osa_identifier(self):
		try:
			problems = check()
		except RuntimeError as exc:
			self.skipTest(str(exc))
		self.assertEqual(problems, [], "\n" + "\n".join(problems))

	def _scan(self, files: dict[str, str], used: set | None = None) -> list[str]:
		import tempfile

		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			for name, text in files.items():
				path = root / name
				path.parent.mkdir(parents=True, exist_ok=True)
				path.write_text(text)
			return find(root, list(files), used)

	def test_the_guard_bites(self):
		found = self._scan({
			"apps/a/a/x.py": 'frappe.get_all("OSA Category")\nf = "osa_category"\nSOURCE = "osa-api"\n',
			"Makefile": "\tosa-ext lint $(DIR)\n",
			"docs/x.md": "Open /app/osa-category.\n",
			"apps/a/a/y.py": 'cache_key = "osa_master_legal:1"\n',
		})
		where = sorted(p.split(": ", 2)[0] + " " + p.split(": ", 2)[1].split(" '")[0] for p in found)
		self.assertEqual(where, [
			"Makefile:1 retired name",
			"apps/a/a/x.py:1 retired name",
			"apps/a/a/x.py:2 retired name",
			"apps/a/a/x.py:2 unlisted osa identifier",
			"apps/a/a/x.py:3 private repository name",
			"apps/a/a/x.py:3 retired name",
			"apps/a/a/y.py:1 unlisted osa identifier",
			"docs/x.md:1 retired name",
		], found)

	def test_kept_names_and_patches_pass(self):
		found = self._scan({
			"apps/a/a/x.py": 'meta["_osa_serial"] = 1  # x-osa-signature, osa_dropship, the update service docs\n',
			"apps/a/a/patches/old.py": 'frappe.db.sql("update tabItem set osa_whatever = 1")\n',
		})
		self.assertEqual(found, [])

	def test_osa_api_is_a_private_repo_name_and_passes_only_as_the_quoted_legacy_source_type(self):
		# prose, a comment or a docstring that names it fails, in any file of the tree
		for name in ("apps/a/a/x.py", "docs/x.md", "apps/a/a/y.js", "README.md"):
			found = self._scan({name: "# osa-api docs\n"})
			self.assertEqual(len(found), 1, (name, found))
			self.assertIn("private repository name 'osa-api'", found[0])
		self.assertEqual(len(self._scan({"apps/a/a/x.py": 'x = 1  # ffl_integrations registers osa-api /v1/extensions\n'})), 1)
		# the two files that read the legacy stored value may say it, quoted, and nothing else
		legacy = "apps/ffl_core/ffl_core/extensions/updates.py"
		self.assertEqual(self._scan({legacy: 'LEGACY_SOURCE_TYPE = "osa-api"   # its name before the rename\n'}), [])
		self.assertEqual(self._scan({legacy: "if x == 'osa-api':\n\treturn 1\n"}), [])
		found = self._scan({legacy: '"""Reads rows written by osa-api."""\nLEGACY = "osa-api"\n'})
		self.assertEqual(len(found), 1, found)
		self.assertIn(":1: private repository name 'osa-api'", found[0])
		self.assertEqual(len(self._scan({legacy: '# the "osa-api docs\n'})), 1)        # a quote on one side only
		self.assertEqual(len(self._scan({legacy: "# osa-api's rows\n"})), 1)
		# any other file: the quoted value is both a retired source type and a private name
		self.assertEqual(len(self._scan({"apps/a/a/other.py": 'S = "osa-api"\n'})), 2)
		self.assertEqual(self._scan({"docs/x.md": "gunstore-osa-apix, my_osa-api2, osa-apis\n"}), [])     # whole identifier only

	def test_a_kept_name_does_not_cover_a_longer_new_one(self):
		self.assertTrue(_kept("osa_dropship") and _kept("OSA_Serial"))
		for token in ("osa_dropship_price", "osa_terms", "osa-ext", "osa_aisle_map", "osa_serial_x", "osa_", "osa_sold_"):
			self.assertFalse(_kept(token), token)

	def test_a_new_osa_name_and_the_spoken_old_name_are_caught(self):
		found = self._scan({"apps/a/a/z.py": 'AISLE = "osa_aisle_map"\n# the osa category tree\n'})
		self.assertEqual(len(found), 2, found)
		self.assertIn("unlisted osa identifier 'osa_aisle_map'", found[0] + found[1])
		self.assertIn("retired name 'OSA Category'", found[0] + found[1])

	def test_a_bare_prefix_passes_only_as_a_key_family_in_prose(self):
		found = self._scan({
			"apps/a/a/w.py": 'key = "_osa_" + name\nsold = "_osa_sold_" + k\n'
				'# the _osa_sold_* keys, no duplicate _osa_* meta\nx = "_osa_*"[:-1] + "osa_tiers_"\n'
				'def f():\n\t"""Writes the\n\t``_osa_sold_*`` keys."""\n\treturn {"_osa_sold_*": 1}  # not _osa_*\n',
			"apps/a/a/w.js": "// the _osa_* meta\n",
		})
		self.assertEqual([p.split(": ", 2)[0] + " " + p.split(": ", 2)[1] for p in found], [
			"apps/a/a/w.py:1 unlisted osa identifier 'osa_'", "apps/a/a/w.py:2 unlisted osa identifier 'osa_sold_'",
			"apps/a/a/w.py:4 unlisted osa identifier 'osa_'", "apps/a/a/w.py:4 unlisted osa identifier 'osa_tiers_'",
			"apps/a/a/w.py:8 unlisted osa identifier 'osa_sold_'",
			"apps/a/a/w.js:1 unlisted osa identifier 'osa_'"], found)

	def test_a_bare_prefix_on_an_allowed_line_passes_and_a_stale_allowance_fails(self):
		allowed = {("apps/a/a/w.js", "// the _osa_* meta"): "test", ("apps/a/a/gone.js", "x"): "test"}
		with mock.patch.dict(PREFIX_ALLOWED, allowed):
			used: set = set()
			self.assertEqual(self._scan({"apps/a/a/w.js": "  // the _osa_* meta\n"}, used), [])
			self.assertEqual(used, {("apps/a/a/w.js", "// the _osa_* meta")})
			this = sys.modules[__name__]
			with mock.patch.object(this, "find", return_value=[]), mock.patch.object(this, "tracked_files", return_value=[]), \
					mock.patch.object(this, "RETIRED_ALLOWED", []):
				self.assertEqual(check(Path(".")), [
					"stale allowance apps/a/a/w.js :: // the _osa_* meta (it allows nothing now)",
					"stale allowance apps/a/a/gone.js :: x (it allows nothing now)"])

	def test_tokens_are_whole_identifiers(self):
		self.assertEqual([m.group(0) for m in TOKEN.finditer('k = "_osa_" + "_osa_sold_" ; h = "x-osa-signature"; osa-logo.png')],
			["osa_", "osa_sold_", "osa-signature", "osa-logo"])


if __name__ == "__main__":
	if "--standalone" in sys.argv:
		try:
			found = check()
		except RuntimeError as exc:
			print(f"cannot run: {exc}", file=sys.stderr)
			sys.exit(2)
		for line in found:
			print(line)
		print(f"retired product id guard: {'FAILED, ' + str(len(found)) + ' problem(s)' if found else 'clean'}")
		sys.exit(1 if found else 0)
	unittest.main()
