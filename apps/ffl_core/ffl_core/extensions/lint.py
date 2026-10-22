"""The extension linter — the mechanical checks of the extension developer guide (naming,
manifest, package and wheels/ dependencies, hook whitelist,
permissions ⊇ use, first party, security, gates, release notes). There is no
separate command: run it as ``make ext-lint DIR=<app dir | zip>`` or as below.

Pure Python on purpose: no frappe import, no site, no network — it runs on a
zip a store manager just uploaded (api.extensions.inspect_upload), in the
extension's own CI, and from a developer's shell::

    python -m ffl_core.extensions.lint <app dir | zip>
    bench --site X execute ffl_core.extensions.lint.run --args '["<dir>"]'

Every finding names a rule, a file and a line where there is one. ``error``
findings block a sideload; ``warn`` findings are shown and let through. This
is the floor an extension must clear to be installed, not a review: strings
and AST are heuristics, a determined author can hide things from them.
"""
from __future__ import annotations

import ast
import email.parser
import importlib.metadata
import json
import os
import re
import sys
import tempfile
import tomllib
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ffl_core.gate_audit import find_ungated_endpoints

ID_RE = re.compile(r"^[a-z][a-z0-9]{1,11}_[a-z][a-z0-9_]*$")          # <vendor>_<name>, vendor 2–12
PERMISSION_RE = re.compile(
	r"^(first_party|pos_ui|desk_ui|scheduler|portal|files|compliance_read|public_endpoint"
	r"|woo_product_meta"
	r"|doc_events:[A-Za-z0-9 ]+|custom_fields:[A-Za-z0-9 ]+|network:[a-z0-9.-]+)$")
FRAPPE_META = {
	"app_name", "app_title", "app_publisher", "app_description", "app_email", "app_license",
	"app_version", "app_icon", "app_color", "app_logo_url", "required_apps", "app_home",
}
ALLOWED_HOOKS = {
	"doc_events", "scheduler_events", "fixtures", "doctype_js", "doctype_list_js", "doctype_tree_js",
	"doctype_calendar_js", "app_include_js", "app_include_css", "after_install", "before_uninstall",
	"after_migrate", "boot_session", "extend_bootinfo", "permission_query_conditions", "has_permission",
	"website_route_rules", "portal_menu_items", "website_redirects", "standard_help_items",
	"standard_navbar_items", "add_to_apps_screen", "jinja", "jenv",
}
GUNSHOP_HOOKS = {
	"gunshop_extension", "gunshop_counter_holds", "gunshop_pending_queue_exclusion_fields",
	"gunshop_disposition_stock_handled", "gunshop_website_user_home_page", "gunshop_extension_toggled",
	"gunshop_woo_product_meta", "gunshop_woo_product_meta_keys", "gunshop_woo_push_fields",
	"gunshop_dropship_listing", "gunshop_dropship_suggested_price", "gunshop_distributor_source_settled",
	"gunshop_distributor_feed_stale", "gunshop_distributor_hub_rows", "gunshop_sidebar_items",
	"gunshop_catalog_change_cursors", "gunshop_ai_connected", "gunshop_required_agreements",
	"gunshop_card_processors",
}
# The ids first_party may be declared by, shipped with the core: the manifest's
# own claim is not enough while packages are unsigned (lazy: an id allowlist, not a
# signature; upgrade path = package signing).
FIRST_PARTY_IDS = frozenset({"osa_consignment", "osa_ai", "osa_ca_compliant", "osa_dropship", "osa_range"})
# Hooks only a first-party extension may register (manifest permission "first_party"):
# auth_hooks lets an app authenticate a request as any user, which a marketplace
# extension must never be able to do.
# override_whitelisted_methods answers another app's endpoint path; the drop-ship
# hooks set a product's website stock, price, source and MAP mode (osa_dropship).
# distributor_stock_batch is distributor_stock_source's batching half: its context
# wraps the reconcile pass that writes website stock, so it ranks with it.
FIRST_PARTY_HOOKS = {
	"auth_hooks", "override_whitelisted_methods",
	"distributor_stock_source", "distributor_stock_batch", "dropship_source_for_item", "distributor_web_price",
	"map_mode_for_item", "distributor_listing_push_failed", "woo_order_cancelled",
}
PORTAL_HOOKS = ("website_route_rules", "portal_menu_items", "website_redirects")
CORE_APPS = ("frappe", "erpnext", "ffl_core", "ffl_integrations", "ffl_woo_sync")
COMPLIANCE_DOCTYPES = {"FFL Acquisition", "FFL Disposition"}
FORBIDDEN_NAMES = {".git", ".env", "node_modules", "setup.py", "__pycache__"}
FORBIDDEN_FILES = re.compile(r"\.(?:pem|key|sql|sql\.gz|so(?:\.\d+)*|pyc|env)$")       # a shared library may carry a version (libfoo.so.1)
# a wheel member: keys, database dumps and compiled code are refused; data files (a CA bundle's .pem) are fine
WHEEL_FORBIDDEN_FILES = re.compile(r"\.(?:key|sql|sql\.gz|env|so(?:\.\d+)*|pyd|dylib|dll|exe|pyc)$")
WHEEL_TAGS = ("py3-none-any", "py2.py3-none-any")                   # pure Python: the only wheels wheels/ may hold
FORBIDDEN_MODULES = {"subprocess", "pickle"}
FORBIDDEN_CALLS = {"eval", "exec", "os.system", "os.popen", "frappe.utils.execute_in_shell", "pickle.loads"}
FORBIDDEN_STRINGS = ("site_config.json", "common_site_config", "pip install")
LOCAL_HOSTS = ("localhost", "127.", "0.0.0.0", "example.", "test.", "schema.org", "www.w3.org")
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_MEMBERS = 20_000


@dataclass
class Finding:
	rule: str
	level: str            # "error" | "warn"
	message: str
	path: str = ""
	line: int | None = None


@dataclass
class Report:
	app_id: str = ""
	title: str = ""
	version: str = ""
	publisher: str = ""
	publisher_id: str = ""
	requires_core: str = ""
	permissions: list[str] = field(default_factory=list)
	findings: list[Finding] = field(default_factory=list)

	@property
	def errors(self) -> list[Finding]:
		return [f for f in self.findings if f.level == "error"]

	@property
	def ok(self) -> bool:
		return not self.errors

	def add(self, rule: str, level: str, message: str, path: Path | str = "", line: int | None = None, root: Path | None = None):
		p = str(path)
		if root and p:
			try:
				p = str(Path(p).relative_to(root))
			except ValueError:
				pass
		self.findings.append(Finding(rule, level, message, p, line))

	def as_dict(self) -> dict:
		return {
			"ok": self.ok, "app_id": self.app_id, "title": self.title, "version": self.version,
			"publisher": self.publisher, "publisher_id": self.publisher_id, "requires_core": self.requires_core,
			"permissions": self.permissions, "errors": len(self.errors),
			"warnings": len(self.findings) - len(self.errors),
			"findings": [asdict(f) for f in self.findings],
		}


class ZipRejected(ValueError):
	"""The archive is not something we will unpack at all (shape, size, traversal)."""


# --------------------------------------------------------------------------- zip
def extract_zip(zip_path: Path, dest: Path) -> Path:
	"""Unpack a sideload zip safely and return the app root inside ``dest``.

	Refuses: not a zip, over MAX_ZIP_BYTES, more than MAX_MEMBERS entries,
	MAX_UNPACKED_BYTES uncompressed, absolute paths or ``..`` segments,
	symlinks. Accepts the app root at the top of the archive (``git archive``)
	or under a single top-level directory (a GitHub download).
	"""
	if zip_path.stat().st_size > MAX_ZIP_BYTES:
		raise ZipRejected(f"the file is larger than {MAX_ZIP_BYTES // (1024 * 1024)} MB")
	if not zipfile.is_zipfile(zip_path):
		raise ZipRejected("the file is not a zip archive")
	with zipfile.ZipFile(zip_path) as z:
		infos = z.infolist()
		if len(infos) > MAX_MEMBERS:
			raise ZipRejected(f"the archive has more than {MAX_MEMBERS} entries")
		total = 0
		for i in infos:
			name = i.filename
			parts = Path(name).parts
			if name.startswith(("/", "\\")) or ".." in parts or (len(name) > 1 and name[1] == ":"):
				raise ZipRejected(f"unsafe path in archive: {name}")
			if (i.external_attr >> 16) & 0o170000 == 0o120000:
				raise ZipRejected(f"symlink in archive: {name}")
			total += i.file_size
			if total > MAX_UNPACKED_BYTES:
				raise ZipRejected(f"the archive unpacks to more than {MAX_UNPACKED_BYTES // (1024 * 1024)} MB")
		z.extractall(dest)
	return app_root_of(dest)


def app_root_of(dest: Path) -> Path:
	"""The directory holding pyproject.toml: ``dest`` itself or its only subdirectory."""
	if (dest / "pyproject.toml").exists():
		return dest
	entries = [p for p in dest.iterdir() if not p.name.startswith("__MACOSX")]
	if len(entries) == 1 and entries[0].is_dir() and (entries[0] / "pyproject.toml").exists():
		return entries[0]
	raise ZipRejected("the archive root must be the app root (pyproject.toml next to the <id>/ package)")


# --------------------------------------------------------------------------- helpers
def _hooks_assignments(tree: ast.Module) -> dict[str, ast.AST]:
	out: dict[str, ast.AST] = {}
	for node in tree.body:
		if isinstance(node, ast.Assign):
			for t in node.targets:
				if isinstance(t, ast.Name):
					out[t.id] = node.value
		elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
			out[node.target.id] = node.value
	return out


def _literal(node: ast.AST):
	try:
		return ast.literal_eval(node)
	except (ValueError, SyntaxError, TypeError):
		return None


def _dotted(node: ast.AST) -> str:
	if isinstance(node, ast.Name):
		return node.id
	if isinstance(node, ast.Attribute):
		base = _dotted(node.value)
		return f"{base}.{node.attr}" if base else node.attr
	return ""


def _py_files(pkg: Path, trees: dict[Path, ast.Module]):
	"""(path, tree) of every non-test module, in path order — parsed once by _rule_compile."""
	for p, tree in trees.items():
		if "tests" in p.relative_to(pkg).parts or p.name.startswith("test_"):
			continue
		yield p, tree


def _read_version(init_py: Path) -> str:
	m = re.search(r'^__version__\s*=\s*"([^"]+)"', init_py.read_text(encoding="utf-8"), re.M)
	return m.group(1) if m else ""


def _has_upper_bound(spec: str) -> bool:
	try:
		from packaging.specifiers import SpecifierSet
		return any(s.operator in ("<", "<=", "==", "~=", "===") for s in SpecifierSet(spec))
	except Exception:
		return bool(re.search(r"(<|==|~=)", spec))


def _core_version(dist: str) -> str | None:
	"""The version of ``dist`` installed in the core environment: the Python running the lint,
	minus apps-ext — the replay's .pth files put every extension's tree and .deps/<id> on
	sys.path too, and what they hold is that extension's, not the core's."""
	path = [p for p in sys.path if "apps-ext" not in Path(os.path.realpath(p or ".")).parts]
	return next((d.version for d in importlib.metadata.distributions(name=dist, path=path)), None)


def _own_doctypes(pkg: Path, entries: list[Path]) -> set[str]:
	names = set()
	for p in entries:
		if p.name.endswith(".json") and p.parent.name == p.stem and p.parent.parent.name == "doctype" and pkg in p.parents and p.is_file():
			try:
				names.add(json.loads(p.read_text(encoding="utf-8")).get("name") or p.stem)
			except ValueError:
				pass
	return names


# --------------------------------------------------------------------------- rules
def lint(root: Path | str, *, packaged: bool = False, known_ids=()) -> Report:
	"""Lint an unpacked app at ``root`` (the directory holding pyproject.toml).

	``packaged`` = the tree came out of an archive: build caches (__pycache__,
	*.pyc) are then errors; in a developer's checkout they are just noise.
	``known_ids``: the extensions already on the server (sites/extensions.json),
	whose packages a bundled wheel may not install over.
	"""
	root = Path(root).resolve()
	r = Report()
	entries = list(root.rglob("*"))                  # the one walk of the tree; the rules filter this list
	_rule_structure(root, entries, r, packaged)
	pkg = _find_package(root, entries)
	if pkg is None:
		r.add("structure", "error", "no <id>/hooks.py package found next to pyproject.toml", root, root=root)
		return r
	app_id = pkg.name
	r.app_id = app_id
	trees = _rule_compile(pkg, entries, r, root)     # the one ast.parse per module
	if any(f.rule == "compile" for f in r.errors):
		return r                                     # nothing else parses reliably; syntax alone blocks
	hooks = _hooks_assignments(trees[pkg / "hooks.py"])
	manifest = _rule_identity(root, pkg, hooks, r)
	own_doctypes = _own_doctypes(pkg, entries)
	_rule_hooks(pkg, hooks, own_doctypes, r, root, first_party="first_party" in ((manifest or {}).get("permissions") or []))
	if manifest is not None:
		_rule_permissions(root, pkg, hooks, manifest, trees, r)
	_rule_security(pkg, trees, r, root)
	_rule_gates(pkg, trees, r, root)
	_rule_release_notes(pkg, r, root)
	_rule_naming(pkg, hooks, own_doctypes, r, root)
	_rule_deps(root, entries, r, {*CORE_APPS, app_id, *known_ids})
	return r


def _find_package(root: Path, entries: list[Path]) -> Path | None:
	hits = [p for p in entries if p.parent == root and p.is_dir() and (p / "hooks.py").is_file() and (p / "__init__.py").exists()]
	return hits[0] if len(hits) == 1 else None


def _rule_structure(root: Path, entries: list[Path], r: Report, packaged: bool) -> None:
	if not (root / "pyproject.toml").exists():
		r.add("structure", "error", "pyproject.toml is missing at the app root", root, root=root)
	if not (root / "README.md").exists():
		r.add("structure", "warn", "README.md is missing", root, root=root)
	for core in CORE_APPS:
		if (root / core).is_dir() or (root / "apps" / core).exists():
			r.add("structure", "error", f"the package contains core app files ({core}); an extension never ships core code", root / core, root=root)
	for p in entries:
		rel = p.relative_to(root)
		if not packaged and ("__pycache__" in rel.parts or p.suffix == ".pyc" or rel.parts[0] == ".git"):
			continue                                     # a developer's checkout, not an archive
		if p.name in FORBIDDEN_NAMES or p.name.startswith(".env"):
			r.add("structure", "error", f"{rel} must not be in the package", p, root=root)
		elif p.is_file() and p.name.endswith(".whl") and p.parent != root / "wheels":
			r.add("structure", "error", f"{rel}: wheels are only allowed directly in wheels/ next to pyproject.toml", p, root=root)
		elif p.is_file() and FORBIDDEN_FILES.search(p.name):
			r.add("structure", "error", f"{rel}: {''.join(p.suffixes)} files must not be in the package", p, root=root)
		elif p.is_symlink():
			r.add("structure", "error", f"{rel} is a symlink", p, root=root)
		elif p.name.endswith(".whl") and not p.is_file():
			r.add("structure", "error", f"{rel} is not a regular file (pip would build a directory named .whl as a project)", p, root=root)


def _rule_compile(pkg: Path, entries: list[Path], r: Report, root: Path) -> dict[Path, ast.Module]:
	"""Parse every module once — the rules below share the trees — and compile the tree,
	which raises what only the compiler sees ('return' outside function, …) as before."""
	trees: dict[Path, ast.Module] = {}
	for p in sorted(e for e in entries if e.name.endswith(".py") and pkg in e.parents and e.is_file()):
		try:
			tree = ast.parse(p.read_text(encoding="utf-8"), str(p))
			compile(tree, str(p), "exec")
		except SyntaxError as e:
			r.add("compile", "error", f"syntax error: {e.msg}", p, e.lineno, root=root)
		except UnicodeDecodeError:
			r.add("compile", "error", "not UTF-8", p, root=root)
		else:
			trees[p] = tree
	return trees


def _rule_identity(root: Path, pkg: Path, hooks: dict[str, ast.AST], r: Report) -> dict | None:
	app_id = pkg.name
	hooks_path = pkg / "hooks.py"
	if not ID_RE.match(app_id):
		r.add("identity", "error", f"id {app_id!r} must be <vendor>_<name>, lowercase, vendor 2–12 characters", hooks_path, root=root)
	m = re.search(r'^\s*name\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8") if (root / "pyproject.toml").exists() else "", re.M)
	if m and m.group(1) != app_id:
		r.add("identity", "error", f"pyproject name {m.group(1)!r} != app id {app_id!r}", root / "pyproject.toml", root=root)
	app_name = _literal(hooks["app_name"]) if "app_name" in hooks else None
	if app_name != app_id:
		r.add("identity", "error", f"hooks.app_name {app_name!r} != package {app_id!r}", hooks_path, root=root)
	app_title = _literal(hooks["app_title"]) if "app_title" in hooks else None
	if not app_title or len(app_title) > 30:
		r.add("identity", "error", "hooks.app_title missing or longer than 30 characters", hooks_path, root=root)
	required = _literal(hooks["required_apps"]) if "required_apps" in hooks else None
	if not isinstance(required, list) or "ffl_core" not in required:
		r.add("identity", "error", 'required_apps must be a list containing "ffl_core"', hooks_path, root=root)
	node = hooks.get("gunshop_extension")
	manifest = _literal(node) if node is not None else None
	if not isinstance(manifest, dict):
		r.add("manifest", "error", "hooks.py must define gunshop_extension as a dict literal", hooks_path, root=root)
		return None
	line = getattr(node, "lineno", None)
	if manifest.get("id") != app_id:
		r.add("manifest", "error", f"manifest id {manifest.get('id')!r} != app id {app_id!r}", hooks_path, line, root=root)
	if manifest.get("title") != app_title:
		r.add("manifest", "error", "manifest title != app_title", hooks_path, line, root=root)
	version = _read_version(pkg / "__init__.py")
	if not version:
		r.add("manifest", "error", '__init__.py must contain __version__ = "x.y.z"', pkg / "__init__.py", root=root)
	elif manifest.get("version") != version:
		r.add("manifest", "error", f"manifest version {manifest.get('version')!r} != __version__ {version!r}", hooks_path, line, root=root)
	rc = manifest.get("requires_core")
	if not isinstance(rc, str) or not rc.strip():
		r.add("manifest", "error", "requires_core is required (PEP 440 range against ffl_core)", hooks_path, line, root=root)
	elif not _has_upper_bound(rc):
		r.add("manifest", "error", f"requires_core {rc!r} has no upper bound (e.g. \">=0.1.0,<1\")", hooks_path, line, root=root)
	perms = manifest.get("permissions")
	if not isinstance(perms, list):
		r.add("manifest", "error", "permissions must be a list", hooks_path, line, root=root)
		perms = []
	for p in perms:
		if not isinstance(p, str) or not PERMISSION_RE.match(p):
			r.add("manifest", "error", f"unknown permission {p!r}", hooks_path, line, root=root)
	if "first_party" in perms and manifest.get("publisher_id") != "osa":
		r.add("first_party", "error", "first_party is reserved for publisher_id \"osa\"", hooks_path, line, root=root)
	if "first_party" in perms and pkg.name not in FIRST_PARTY_IDS:
		r.add("first_party", "error", f"{pkg.name!r} is not a first-party extension this core knows", hooks_path, line, root=root)
	modules = pkg / "modules.txt"
	lines = [ln.strip() for ln in modules.read_text(encoding="utf-8").splitlines() if ln.strip()] if modules.exists() else []
	if lines != [app_title]:
		r.add("identity", "error", f"modules.txt must hold exactly one line equal to the title {app_title!r}", modules, root=root)
	elif app_title:
		# frappe imports <id>.<scrubbed module> for every module in modules.txt (install_app → sync_for);
		# a missing package there is exactly the "No module named …" a store manager cannot act on.
		module_dir = pkg / re.sub(r"[^a-z0-9_]", "_", app_title.lower().replace(" ", "_"))
		if not (module_dir / "__init__.py").exists():
			r.add("structure", "error", f"module package {module_dir.relative_to(root)}/__init__.py is missing (frappe imports it for the module {app_title!r})", modules, root=root)
	r.title = str(app_title or "")
	r.version = version
	r.publisher = str(manifest.get("publisher") or "")
	r.publisher_id = str(manifest.get("publisher_id") or "")
	r.requires_core = str(rc or "")
	r.permissions = [p for p in perms if isinstance(p, str)]
	return manifest


def _rule_hooks(pkg: Path, hooks: dict[str, ast.AST], own_doctypes: set[str], r: Report, root: Path, first_party: bool = False) -> None:
	hooks_path = pkg / "hooks.py"
	app_id = pkg.name
	for name, node in hooks.items():
		if name.startswith("_"):
			continue
		if name in FIRST_PARTY_HOOKS:
			if not first_party:
				r.add("hooks", "error", f"hook {name!r} is reserved for first-party extensions", hooks_path, node.lineno, root=root)
			continue
		if name not in FRAPPE_META and name not in ALLOWED_HOOKS and name not in GUNSHOP_HOOKS:
			r.add("hooks", "error", f"hook {name!r} is not on the whitelist", hooks_path, node.lineno, root=root)
	prefix = f"/ext/{app_id}/"
	for name in PORTAL_HOOKS:
		if name not in hooks:
			continue
		value = _literal(hooks[name]) or []
		key = "target" if name == "website_redirects" else ("from_route" if name == "website_route_rules" else "route")
		for entry in value if isinstance(value, list) else []:
			path = entry.get(key, "") if isinstance(entry, dict) else ""
			if not str(path).startswith(prefix):
				r.add("hooks", "error", f"{name}: {key} {path!r} must start with {prefix}", hooks_path, hooks[name].lineno, root=root)
	if "add_to_apps_screen" in hooks:
		value = _literal(hooks["add_to_apps_screen"]) or []
		for entry in value if isinstance(value, list) else []:
			if not (isinstance(entry, dict) and entry.get("has_permission")):
				r.add("hooks", "error", "add_to_apps_screen entries must carry a has_permission that returns False", hooks_path, hooks["add_to_apps_screen"].lineno, root=root)
	for name in ("has_permission", "permission_query_conditions"):
		if name not in hooks:
			continue
		value = _literal(hooks[name]) or {}
		for dt in value if isinstance(value, dict) else []:
			if dt not in own_doctypes:
				r.add("hooks", "error", f"{name} may only cover the extension's own DocTypes, not {dt!r}", hooks_path, hooks[name].lineno, root=root)


def _rule_permissions(root: Path, pkg: Path, hooks: dict[str, ast.AST], manifest: dict, trees: dict[Path, ast.Module], r: Report) -> None:
	declared = set(manifest.get("permissions") or [])
	hooks_path = pkg / "hooks.py"
	used: dict[str, str] = {}           # permission -> why

	def need(perm: str, why: str):
		used.setdefault(perm, why)

	if "doc_events" in hooks:
		for dt in (_literal(hooks["doc_events"]) or {}):
			need(f"doc_events:{dt}", "hooks.doc_events")
	if "scheduler_events" in hooks and _literal(hooks["scheduler_events"]):
		need("scheduler", "hooks.scheduler_events")
	cf = pkg / "fixtures" / "custom_field.json"
	if cf.exists():
		try:
			for row in json.loads(cf.read_text(encoding="utf-8")):
				if isinstance(row, dict) and row.get("dt"):
					need(f"custom_fields:{row['dt']}", "fixtures/custom_field.json")
		except ValueError:
			r.add("manifest", "error", "fixtures/custom_field.json is not valid JSON", cf, root=root)
	if "app_include_js" in hooks and not ({"pos_ui", "desk_ui"} & declared):
		r.add("permissions", "error", "app_include_js needs pos_ui or desk_ui declared", hooks_path, hooks["app_include_js"].lineno, root=root)
	if any(h in hooks for h in PORTAL_HOOKS) or (pkg / "www").is_dir():
		need("portal", "website hooks / www")
	first_party = "first_party" in declared
	for path, tree in _py_files(pkg, trees):
		docstrings = {id(n.body[0].value) for n in ast.walk(tree)
			if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
			and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant) and isinstance(n.body[0].value.value, str)}
		for node in ast.walk(tree):
			if isinstance(node, ast.Call):
				if _dotted(node.func) in ("frappe.whitelist",) and any(k.arg == "allow_guest" and _literal(k.value) is True for k in node.keywords):
					need("public_endpoint", f"{path.relative_to(root)}:{node.lineno}")
				fn = _dotted(node.func)
				if fn.startswith(("frappe.get_doc", "frappe.get_all", "frappe.get_list", "frappe.db.")) and node.args:
					first = node.args[0]
					if isinstance(first, ast.Constant) and first.value in COMPLIANCE_DOCTYPES and not first_party:
						need("compliance_read", f"{path.relative_to(root)}:{node.lineno}")
			elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings and path.name != "hooks.py":
				for host in re.findall(r"https?://([a-z0-9.-]+\.[a-z]{2,})", node.value, re.I):
					host = host.lower()
					if host.startswith(LOCAL_HOSTS) or ".example" in host:
						continue
					need(f"network:{host}", f"{path.relative_to(root)}:{node.lineno}")
	for perm, why in sorted(used.items()):
		if perm not in declared:
			r.add("permissions", "error", f"uses {perm} ({why}) but does not declare it", hooks_path, root=root)


def _rule_security(pkg: Path, trees: dict[Path, ast.Module], r: Report, root: Path) -> None:
	for path, tree in _py_files(pkg, trees):
		for node in ast.walk(tree):
			if isinstance(node, ast.Import):
				for a in node.names:
					if a.name.split(".")[0] in FORBIDDEN_MODULES:
						r.add("security", "error", f"import {a.name} is not allowed", path, node.lineno, root=root)
			elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in FORBIDDEN_MODULES:
				r.add("security", "error", f"from {node.module} import … is not allowed", path, node.lineno, root=root)
			elif isinstance(node, ast.Call):
				fn = _dotted(node.func)
				if fn in FORBIDDEN_CALLS:
					r.add("security", "error", f"{fn}() is not allowed", path, node.lineno, root=root)
				for k in node.keywords:
					if k.arg == "verify" and _literal(k.value) is False:
						r.add("security", "error", "verify=False disables TLS checks", path, node.lineno, root=root)
			elif isinstance(node, ast.Constant) and isinstance(node.value, str):
				for s in FORBIDDEN_STRINGS:
					if s in node.value:
						r.add("security", "error", f"string mentions {s!r} — extensions never touch site config or pip", path, node.lineno, root=root)
						break


def _rule_gates(pkg: Path, trees: dict[Path, ast.Module], r: Report, root: Path) -> None:
	# every whitelisted, non-guest module-level endpoint must open with a recognised gate — the
	# repo's one audit, on the trees parsed here (it would walk and parse the package again)
	for offender in find_ungated_endpoints(pkg.name, pkg, trees=trees):
		r.add("gates", "error", f"endpoint is not gated: {offender} (first statement must be require_enabled/only_for)", pkg / "hooks.py", root=root)
	for path, tree in _py_files(pkg, trees):
		for node in tree.body:
			if isinstance(node, ast.FunctionDef) and any(_dotted(d.func if isinstance(d, ast.Call) else d) == "frappe.whitelist" for d in node.decorator_list) and not ast.get_docstring(node):
				r.add("gates", "warn", f"endpoint {node.name} has no docstring (docs/api reads it)", path, node.lineno, root=root)


def _rule_release_notes(pkg: Path, r: Report, root: Path) -> None:
	if not r.version:
		return
	major = r.version.split(".")[0]
	note = pkg / "change_log" / f"v{major}" / f"v{r.version.replace('.', '_')}.md"
	if not note.exists():
		r.add("release_notes", "warn", f"no release note for {r.version} at {note.relative_to(root)}", note, root=root)


def _rule_naming(pkg: Path, hooks: dict[str, ast.AST], own_doctypes: set[str], r: Report, root: Path) -> None:
	title = r.title
	vendor = pkg.name.split("_", 1)[0]
	if title:
		for dt in sorted(own_doctypes):
			if not dt.startswith(title.split(" ")[0]):
				r.add("naming", "error", f"DocType {dt!r} must start with the title prefix {title.split(' ')[0]!r}", pkg, root=root)
	cf = pkg / "fixtures" / "custom_field.json"
	if cf.exists():
		try:
			for row in json.loads(cf.read_text(encoding="utf-8")):
				fn = row.get("fieldname", "") if isinstance(row, dict) else ""
				if fn and not fn.startswith(f"{vendor}_") and row.get("fieldtype") not in ("Section Break", "Column Break", "Tab Break"):
					r.add("naming", "error", f"custom field {fn!r} on {row.get('dt')} must start with {vendor}_", cf, root=root)
		except ValueError:
			pass


def _rule_deps(root: Path, entries: list[Path], r: Report, taken: set[str]) -> None:
	"""Pure-Python dependencies ship as wheels/*.whl; ext-replay.sh installs them with
	pip --no-index into apps-ext/.deps/<id>. Every requirement — [project].dependencies
	and each wheel's Requires-Dist — must be met by a bundled wheel or by the core
	environment, and a wheel of something the core already ships is refused (one of the
	two copies would silently shadow the other). What a wheel unpacks is on every
	process's sys.path, so its top-level names may not be ``taken`` (the core apps,
	this extension, the extensions already on the server) and it carries no compiled
	code, key or database dump (WHEEL_FORBIDDEN_FILES)."""
	pyproject = root / "pyproject.toml"
	wheels = sorted(p for p in entries if p.parent == root / "wheels" and p.name.endswith(".whl") and p.is_file())
	try:
		project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {}) if pyproject.is_file() else {}
	except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
		r.add("deps", "error", f"pyproject.toml is not valid TOML: {e}", pyproject, root=root)
		project = {}
	if not isinstance(project, dict):
		r.add("deps", "error", "pyproject.toml: [project] must be a table", pyproject, root=root)
		project = {}
	requires = project.get("dependencies") or []
	if not isinstance(requires, list) or not all(isinstance(x, str) for x in requires):
		r.add("deps", "error", "[project].dependencies must be a list of requirement strings", pyproject, root=root)
		requires = []
	if not wheels and not requires:
		return
	from packaging.markers import UndefinedComparison, UndefinedEnvironmentName
	from packaging.requirements import InvalidRequirement, Requirement
	from packaging.utils import canonicalize_name

	bundled: dict[str, str] = {}                      # canonical name -> version
	pending = [(spec, pyproject, "pyproject.toml") for spec in requires]
	unpacked = sum(p.stat().st_size for p in entries if p.is_file())
	for whl in wheels:
		rel = whl.relative_to(root)
		parts = whl.name[: -len(".whl")].split("-")
		tag = "-".join(parts[-3:])
		if len(parts) not in (5, 6) or tag not in WHEEL_TAGS:
			r.add("deps", "error", f"{rel}: only pure-Python wheels ({' / '.join(WHEEL_TAGS)}) can be bundled, not {tag!r}", whl, root=root)
			continue
		meta, reason = None, "no single .dist-info/METADATA (under 1 MB) with Name and Version"
		try:
			with zipfile.ZipFile(whl) as z:
				infos = z.infolist()
				unpacked += sum(i.file_size for i in infos)
				for top in sorted({_wheel_top_level(i.filename) for i in infos} & taken):
					r.add("deps", "error", f"{rel} installs a top-level {top!r}: the core, this extension or another extension on this server already owns that name", whl, root=root)
				for i in infos:
					if WHEEL_FORBIDDEN_FILES.search(i.filename):
						r.add("deps", "error", f"{rel}: {i.filename} must not be in the package ({''.join(Path(i.filename).suffixes)} files)", whl, root=root)
				metadata = [i for i in infos if re.fullmatch(r"[^/]+\.dist-info/METADATA", i.filename)]
				if len(metadata) == 1 and metadata[0].file_size <= 1024 * 1024:     # never inflate a bomb to read a header
					meta = email.parser.Parser().parsestr(z.read(metadata[0]).decode("utf-8"))
		except (zipfile.BadZipFile, OSError, UnicodeDecodeError, RuntimeError, NotImplementedError, EOFError) as e:
			reason = str(e)
		if meta is None or not meta["Name"] or not meta["Version"]:
			r.add("deps", "error", f"{rel} is not a valid wheel ({reason})", whl, root=root)
			continue
		core = _core_version(meta["Name"])
		if core is not None:
			r.add("deps", "error", f"{rel}: {meta['Name']} is provided by core {core}, do not bundle", whl, root=root)
			continue
		bundled[canonicalize_name(meta["Name"])] = meta["Version"]
		pending += [(spec, whl, f"{rel} Requires-Dist") for spec in meta.get_all("Requires-Dist") or []]
	if unpacked > MAX_UNPACKED_BYTES:
		r.add("deps", "error", f"the package and its wheels unpack to more than {MAX_UNPACKED_BYTES // (1024 * 1024)} MB", root / "wheels", root=root)
	for spec, path, where in pending:
		try:
			req = Requirement(spec)
		except InvalidRequirement as e:
			r.add("deps", "error", f"{where}: {spec!r} is not a valid requirement ({e})", path, root=root)
			continue
		try:
			if req.marker is not None and not req.marker.evaluate({"extra": ""}):
				continue                                 # an optional extra, or not for this Python
		except (UndefinedComparison, UndefinedEnvironmentName) as e:
			r.add("deps", "error", f"{where}: {spec!r}: its marker cannot be evaluated ({e})", path, root=root)
			continue
		have, source = bundled.get(canonicalize_name(req.name)), "wheels/"
		if have is None:
			have, source = _core_version(req.name), "core"
		try:
			if have is not None and req.specifier.contains(have, prereleases=True):
				continue
		except ValueError:                               # packaging.version.InvalidVersion
			pass
		found = f"{source} has {have}" if have is not None else "not in wheels/ or the core"
		r.add("deps", "error", f"{where}: {spec} is not satisfied ({found}) — bundle a pure-Python wheel in wheels/ or use the core's version", path, root=root)


def _wheel_top_level(name: str) -> str:
	"""The name a wheel member lands under in pip's --target: ``pkg/…`` or ``mod.py``,
	and <dist>.data/purelib|platlib/<name> (pip installs those at the top too)."""
	parts = name.split("/")
	if len(parts) > 2 and parts[0].endswith(".data") and parts[1] in ("purelib", "platlib"):
		parts = parts[2:]
	return parts[0].removesuffix(".py") if len(parts) == 1 else parts[0]


# --------------------------------------------------------------------------- entry points
def lint_path(path: Path | str) -> Report:
	"""Lint a directory or a zip (unpacked to a temp dir)."""
	path = Path(path)
	if path.is_dir():
		return lint(path)
	with tempfile.TemporaryDirectory(prefix="ext-lint-") as tmp:
		try:
			root = extract_zip(path, Path(tmp))
		except (ZipRejected, zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError) as e:
			r = Report()
			r.add("package", "error", f"{e} (encrypted, corrupt or unsafe archive)", path)
			return r
		return lint(root, packaged=True)


def run(path: str) -> dict:
	"""``bench execute ffl_core.extensions.lint.run --args '["<dir or zip>"]'``"""
	return lint_path(path).as_dict()


def main(argv: list[str] | None = None) -> int:
	argv = sys.argv[1:] if argv is None else argv
	if len(argv) != 1:
		print("usage: python -m ffl_core.extensions.lint <app dir | zip>", file=sys.stderr)
		return 2
	report = lint_path(argv[0])
	for f in report.findings:
		where = f"{f.path}:{f.line}" if f.line else f.path
		print(f"{f.level.upper():5} {f.rule:13} {where}: {f.message}")
	print(f"{'OK' if report.ok else 'FAIL'} {report.app_id} {report.version} — {len(report.errors)} error(s), {len(report.findings) - len(report.errors)} warning(s)")
	return 0 if report.ok else 1


if __name__ == "__main__":
	sys.exit(main())
