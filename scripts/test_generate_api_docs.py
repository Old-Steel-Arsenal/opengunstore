#!/usr/bin/env python3
"""Unit tests for scripts/generate_api_docs.py — the auth derivation above all.

The derivation writes SECURITY FACTS into published docs, so every resolution
shape it supports is locked here as a table-driven case, together with the two
sandbox counterexamples from a code audit (a business helper with a
nested non-throwing has_permission must NOT be read as a gate; a bare
has_permission without throw=True is a no-op, not a gate).

Run: python3 scripts/test_generate_api_docs.py   (stdlib unittest, no bench)
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_api_docs as g  # noqa: E402


def make_repo(tmp: Path, files: dict[str, str]) -> Path:
    """A fake repo tree: files maps 'ffl_core/api/x.py' → source."""
    for rel, source in files.items():
        app = rel.split("/")[0]
        path = tmp / "apps" / app / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        # package inits so the layout mirrors the real apps
        d = tmp / "apps" / app
        while d != tmp:
            init = d / "__init__.py" if d.name != "apps" else None
            if init and not init.exists() and d.is_dir():
                init.write_text("", encoding="utf-8")
            d = d.parent
        path.write_text(source, encoding="utf-8")
    return tmp


class DeriveCase(unittest.TestCase):
    """Each case: source files + endpoint → expected auth dict."""

    def derive(self, files: dict[str, str], module: str, func: str):
        import ast as _ast
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = make_repo(Path(tmpdir), files)
            old_repo = g.REPO
            g.REPO = tmp
            try:
                resolver = g.AuthResolver()
                path = tmp / "apps" / (module.split(".")[0]) / (
                    "/".join(module.split(".")) + ".py")
                tree = _ast.parse(path.read_text(encoding="utf-8"))
                node = next(
                    n for n in tree.body
                    if isinstance(n, _ast.FunctionDef) and n.name == func)
                return resolver.derive(node, module)
            finally:
                g.REPO = old_repo

    # ---- positive shapes -------------------------------------------------

    def test_the_core_keeps_its_first_gate(self):
        # Merging a later switch into the gate is --app only: the core's docs stay as they were.
        auth = self.derive({
            "ffl_core/extensions/__init__.py": "def require_enabled(app, *, roles=None):\n\tpass\n",
            "ffl_core/api/m.py": (
                "import frappe\nfrom ffl_core.extensions import require_enabled\n"
                "def ep():\n\tfrappe.only_for((\"Stock Manager\",))\n\trequire_enabled(\"x\")\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth, {"roles": ["Stock Manager"]})

    def test_only_for_tuple_literal(self):
        auth = self.derive({
            "ffl_core/api/m.py": (
                "import frappe\n"
                "def ep():\n"
                "\tfrappe.only_for((\"Stock Manager\", \"System Manager\"))\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth, {"roles": ["Stock Manager", "System Manager"]})

    def test_only_for_module_constant(self):
        auth = self.derive({
            "ffl_core/api/m.py": (
                "import frappe\n"
                "_ROLES = (\"Sales User\", \"System Manager\")\n"
                "def ep():\n\tfrappe.only_for(_ROLES)\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth, {"roles": ["Sales User", "System Manager"]})

    def test_cross_module_and_function_local_import(self):
        auth = self.derive({
            "ffl_core/api/src.py": "_ROLES = (\"Sales Manager\",)\n",
            "ffl_core/api/m.py": (
                "import frappe\n"
                "def ep():\n"
                "\tfrom ffl_core.api.src import _ROLES\n"
                "\tfrappe.only_for(_ROLES)\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth, {"roles": ["Sales Manager"]})

    def test_relative_import_constant(self):
        auth = self.derive({
            "ffl_core/api/roles.py": "CONSOLE_ROLE = \"Distributor Console\"\n",
            "ffl_core/api/m.py": (
                "import frappe\n"
                "from .roles import CONSOLE_ROLE\n"
                "BASE = (\"Stock Manager\",)\n"
                "ALL = BASE + (CONSOLE_ROLE,)\n"
                "def ep():\n\tfrappe.only_for(ALL)\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth, {"roles": ["Stock Manager", "Distributor Console"]})

    def test_function_call_with_tuple_wrapper_and_binop(self):
        auth = self.derive({
            "ffl_core/api/opts.py": (
                "CONSOLE_ROLE = \"Distributor Console\"\n"
                "def console_roles():\n"
                "\tfrom ffl_core.api import base\n"
                "\treturn tuple(base.ORDER_ROLES) + (CONSOLE_ROLE,)\n"
            ),
            "ffl_core/api/base.py": "ORDER_ROLES = (\"Purchase Manager\",)\n",
            "ffl_core/api/m.py": (
                "import frappe\n"
                "from ffl_core.api.opts import console_roles\n"
                "def ep():\n\tfrappe.only_for(console_roles())\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth, {"roles": ["Purchase Manager", "Distributor Console"]})

    def test_gate_helper_top_level_only_for(self):
        auth = self.derive({
            "ffl_core/api/m.py": (
                "import frappe\n"
                "def _gate():\n\tfrappe.only_for((\"System Manager\",))\n"
                "def ep():\n\t_gate()\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth, {"roles": ["System Manager"]})

    def test_gate_helper_called_on_an_imported_module(self):
        # si_reversal's `api._check_refund_perm()`: the helper lives in another module of our apps.
        files = {
            "ffl_integrations/payments/api.py": (
                "import frappe\n"
                "def _check_manager(message):\n"
                "\troles = set(frappe.get_roles())\n"
                "\tif not (roles & {\"Sales Manager\", \"System Manager\"}):\n\t\tfrappe.throw(message)\n"
                "def _check_refund_perm():\n\t_check_manager(\"no\")\n"
                "def refund():\n\tpass\n"
            ),
            "ffl_integrations/payments/si_reversal.py": (
                "import frappe\n"
                "from ffl_integrations.payments import api\n"
                "def ep():\n\tapi._check_refund_perm()\n"
                "def business():\n\tapi.refund()\n"
            )}
        auth = self.derive(files, "ffl_integrations.payments.si_reversal", "ep")
        self.assertEqual(auth, {"roles": ["Sales Manager", "System Manager"]})
        # a business call on the same module is not a gate
        self.assertIsNone(self.derive(files, "ffl_integrations.payments.si_reversal", "business"))

    def test_helper_returning_set_intersection(self):
        auth = self.derive({
            "ffl_core/api/m.py": (
                "import frappe\n"
                "_ROLES = (\"Sales User\", \"Sales Manager\")\n"
                "def _has_ship_role():\n"
                "\treturn bool(set(_ROLES).intersection(frappe.get_roles()))\n"
                "def ep():\n"
                "\tif not _has_ship_role():\n\t\treturn {}\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(
            auth, {"roles": ["Sales User", "Sales Manager"], "soft": True})

    def test_soft_vs_hard_if_gate(self):
        files = {
            "ffl_core/api/m.py": (
                "import frappe\n"
                "_R = (\"Stock Manager\",)\n"
                "def hard():\n"
                "\tif not (set(frappe.get_roles()) & set(_R)):\n"
                "\t\tfrappe.throw(\"no\")\n"
                "def soft():\n"
                "\tif not (set(frappe.get_roles()) & set(_R)):\n"
                "\t\treturn {}\n"
            )}
        self.assertEqual(
            self.derive(files, "ffl_core.api.m", "hard"),
            {"roles": ["Stock Manager"]})
        self.assertEqual(
            self.derive(files, "ffl_core.api.m", "soft"),
            {"roles": ["Stock Manager"], "soft": True})

    def test_not_in_get_roles_membership(self):
        auth = self.derive({
            "ffl_core/api/m.py": (
                "import frappe\n"
                "def ep():\n"
                "\tif \"System Manager\" not in frappe.get_roles():\n"
                "\t\tfrappe.throw(\"no\")\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth, {"roles": ["System Manager"]})

    def test_has_permission_with_throw_is_a_gate(self):
        auth = self.derive({
            "ffl_core/api/m.py": (
                "import frappe\n"
                "def ep(serial):\n"
                "\tfrappe.has_permission(\"Serial No\", \"write\", doc=serial, throw=True)\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth, {"perm": "write permission on Serial No",
            "perm_doctype": "Serial No", "perm_ptype": "write"})

    def test_late_gate_flagged(self):
        auth = self.derive({
            "ffl_core/api/m.py": (
                "import frappe\n"
                "def ep(name):\n"
                "\trow = frappe.db.get_value(\"Item\", name, \"owner\")\n"
                "\tfrappe.only_for((\"System Manager\",))\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth, {"roles": ["System Manager"], "late": True})

    def test_roles_read_ahead_of_a_helper_s_first_gate_are_part_of_it(self):
        # pos_4473._check_perm: `roles = set(frappe.get_roles())` feeds the check — not code run before it (no `late`)
        auth = self.derive({
            "ffl_core/api/m.py": (
                "import frappe\n_R = (\"Sales User\",)\n"
                "def _check_perm():\n\troles = set(frappe.get_roles())\n"
                "\tif not (roles & set(_R)):\n\t\tfrappe.throw(\"no\")\n"
                "def ep():\n\t_check_perm()\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth, {"roles": ["Sales User"]})

    def test_code_run_ahead_of_a_helper_s_first_gate_makes_it_late(self):
        # a helper reads what runs before its first gate as it reads what runs before a later one, and as an
        # endpoint body does — the roles read aside, which was the first gate's only reason to be read differently
        auth = self.derive({
            "ffl_core/api/m.py": (
                "import frappe\n_R = (\"Sales User\",)\n"
                "def _check_perm(name):\n\tfrappe.db.get_value(\"Item\", name, \"owner\")\n\tfrappe.only_for(_R)\n"
                "def ep(name):\n\t_check_perm(name)\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth, {"roles": ["Sales User"], "late": True})

    def test_roles_read_ahead_of_an_endpoint_body_s_gate_are_part_of_it_too(self):
        # an endpoint body reads the statement before its gate as a helper does (_substantive is shared)
        auth = self.derive({
            "ffl_core/api/m.py": (
                "import frappe\n_R = (\"Sales User\",)\n"
                "def ep():\n\troles = set(frappe.get_roles())\n"
                "\tif not (roles & set(_R)):\n\t\tfrappe.throw(\"no\")\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth, {"roles": ["Sales User"]})

    def test_unresolvable_degrades_to_label(self):
        auth = self.derive({
            "ffl_core/api/m.py": (
                "import frappe\n"
                "def ep():\n\tfrappe.only_for(frappe.get_system_roles())\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth, {"label": "frappe.get_system_roles()"})

    # ---- counterexamples: shapes that must NOT be read as gates ----------

    def test_r2_counterexample_business_helper_nested_has_permission(self):
        """The code-audit sandbox case: a gate-shaped business helper with
        a NESTED, non-throwing has_permission must not mask the real gate."""
        auth = self.derive({
            "ffl_core/api/m.py": (
                "import frappe\n"
                "def _checked_image(doc):\n"
                "\tif doc:\n"
                "\t\tif frappe.has_permission(\"File\", \"read\", doc=doc):\n"
                "\t\t\treturn doc\n"
                "\treturn None\n"
                "def ep(doc):\n"
                "\t_checked_image(doc)\n"
                "\tfrappe.only_for((\"Sales User\",))\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth["roles"], ["Sales User"])
        self.assertNotIn("perm", auth)

    def test_if_not_has_permission_throw_body_is_a_hard_gate(self):
        auth = self.derive({
            "ffl_core/api/m.py": (
                "import frappe\n"
                "def ep(name):\n"
                "\tif not frappe.has_permission(\"Consignment Out\", ptype=\"write\"):\n"
                "\t\tfrappe.throw(\"no\")\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth, {"perm": "write permission on Consignment Out",
            "perm_doctype": "Consignment Out", "perm_ptype": "write"})

    def test_bare_has_permission_without_throw_is_not_a_gate(self):
        auth = self.derive({
            "ffl_core/api/m.py": (
                "import frappe\n"
                "def ep(doc):\n"
                "\tfrappe.has_permission(\"File\", \"read\", doc=doc)\n"
            )}, "ffl_core.api.m", "ep")
        self.assertIsNone(auth)

    def test_business_call_in_return_not_deep_walked(self):
        """A gate inside a function reached only via `return f(...)` must not
        be claimed for the endpoint (the trade_in false-guarantee)."""
        auth = self.derive({
            "ffl_core/api/m.py": (
                "import frappe\n"
                "def create_intake(p):\n"
                "\tfrappe.only_for((\"Stock Manager\",))\n"
                "def ep(p):\n\treturn create_intake(p)\n"
            )}, "ffl_core.api.m", "ep")
        self.assertIsNone(auth)

    def test_constant_cycle_degrades_not_hangs(self):
        auth = self.derive({
            "ffl_core/api/a.py": "from ffl_core.api.b import X\nY = X\n",
            "ffl_core/api/b.py": "from ffl_core.api.a import Y\nX = Y\n",
            "ffl_core/api/m.py": (
                "import frappe\nfrom ffl_core.api.a import Y\n"
                "def ep():\n\tfrappe.only_for(Y)\n"
            )}, "ffl_core.api.m", "ep")
        self.assertEqual(auth, {"label": "Y"})


class ChecksCase(unittest.TestCase):
    def run_checks_on(self, files):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = make_repo(Path(tmpdir), files)
            old_repo = g.REPO
            g.REPO = tmp
            try:
                resolver = g.AuthResolver()
                endpoints = g.collect_endpoints(resolver)
                token_count, offenders = g.token_whitelist_scan()
                problems = [line for part in g.conformance_problems(endpoints) for line in part]
                return endpoints, token_count, offenders, problems
            finally:
                g.REPO = old_repo

    GOOD = (
        "import frappe\n"
        "@frappe.whitelist()\n"
        "def ep(x):\n"
        "\t\"\"\"Sum.\n\n"
        "\tArgs:\n\t    x: value.\n\n"
        "\tReturns:\n\t    the value.\n\t\"\"\"\n"
        "\treturn x\n"
    )

    def test_core_check_status_is_parity_and_conformance_only(self):
        # Without --app: 0 or 1, as before --app existed. A stale core docs/api is `make api-docs-check`'s git diff.
        # The core has no drift: a Permissions contradiction is its own defect (1), whatever the roles come from.
        undocumented = "import frappe\n@frappe.whitelist()\ndef ep(x):\n\treturn x\n"
        contradiction = self.GOOD.replace("\treturn x\n", "\tfrappe.only_for(ROLES)\n\treturn x\n").replace(
            "\t    the value.\n", "\t    the value.\n\n\tPermissions:\n\t    Sales Manager only.\n").replace(
            "import frappe\n", "import frappe\nfrom ffl_core.roles import ROLES\n")
        # The action catalog is the one core output --check itself gates: it ships in the image, out of docs/api.
        for name, source, status in (("conformant", self.GOOD, 0), ("undocumented", undocumented, 1),
                ("contradiction", contradiction, 1), ("stale catalog", self.GOOD, 1)):
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp = make_repo(Path(tmpdir), {"ffl_core/api/m.py": source,
                    "ffl_core/roles.py": "ROLES = (\"Sales Manager\", \"System Manager\")\n"})
                (tmp / "docs" / "api").mkdir(parents=True)
                (tmp / "docs" / "api" / "openapi.json").write_text("{}\n")      # stale on purpose
                saved = g.REPO, g.OUT
                g.REPO, g.OUT = tmp, tmp / "docs" / "api"
                for path, text in g.render_catalogs(g.collect_endpoints(g.AuthResolver())).items():
                    path.write_text("[]\n" if name == "stale catalog" else text, encoding="utf-8")
                stdout, stderr = io.StringIO(), io.StringIO()
                try:
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        got = g.main(["--check"])
                finally:
                    g.REPO, g.OUT = saved
            self.assertEqual((got, "checks OK" in stdout.getvalue(), "STALE" in stderr.getvalue(), "DRIFT" in stderr.getvalue()),
                (status, status == 0, name == "stale catalog", False), (name, stderr.getvalue()))
            if name == "contradiction":                  # its own heading, not "undocumented endpoints"
                self.assertIn("CONFORMANCE FAIL — Permissions prose contradicts the derived gate:", stderr.getvalue())
                self.assertNotIn("undocumented endpoints", stderr.getvalue())

    def test_aliased_whitelist_import_is_offender(self):
        eps, count, offenders, _ = self.run_checks_on({
            "ffl_core/api/m.py": self.GOOD,
            "ffl_core/api/alias.py": (
                "from frappe import whitelist as wl\n"
                "@wl()\n"
                "def hidden():\n\treturn 1\n"
            )})
        self.assertEqual(len(eps), 1)
        self.assertTrue(any("aliased" in o or "bare" in o for o in offenders))

    def test_empty_returns_header_fails_conformance(self):
        _, _, _, problems = self.run_checks_on({
            "ffl_core/api/m.py": (
                "import frappe\n"
                "@frappe.whitelist()\n"
                "def ep(x):\n"
                "\t\"\"\"Sum.\n\n\tArgs:\n\t    x: v.\n\n\tReturns:\n\t\"\"\"\n"
                "\treturn x\n"
            )})
        self.assertTrue(any("Returns" in p for p in problems))

    def test_prose_role_mismatch_fails_conformance(self):
        _, _, _, problems = self.run_checks_on({
            "ffl_core/api/m.py": (
                "import frappe\n"
                "@frappe.whitelist()\n"
                "def ep(x):\n"
                "\t\"\"\"Sum.\n\n\tArgs:\n\t    x: v.\n\n"
                "\tReturns:\n\t    v.\n\n"
                "\tPermissions:\n\t    Sales Manager only.\n\t\"\"\"\n"
                "\tfrappe.only_for((\"Sales Manager\", \"System Manager\"))\n"
                "\treturn x\n"
            )})
        self.assertTrue(any("contradicts" in p for p in problems))


class ExtensionCase(unittest.TestCase):
    """--app: one extension documented on its own, its gates resolved through the core."""

    ENDPOINT = (
        "import frappe\n"
        "from ffl_core.extensions import require_enabled\n"
        "from ffl_core.roles import COUNTER_ROLES\n"
        "APP = \"acme_hello\"\n"
        "@frappe.whitelist(methods=[\"POST\"])\n"
        "def greet(customer: str):\n"
        "\t\"\"\"Say hello.\n\n\tArgs:\n\t    customer: Customer name.\n\n"
        "\tReturns:\n\t    dict: {\"message\": str}\n\t\"\"\"\n"
        "\trequire_enabled(APP, roles=COUNTER_ROLES)\n"
        "\treturn {}\n"
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.repo = make_repo(tmp / "core", {
            "ffl_core/roles.py": "COUNTER_ROLES = (\"Sales User\", \"System Manager\")\n",
            "ffl_core/extensions/__init__.py": "def require_enabled(app, *, roles=None):\n\tpass\n",
        })
        self.root = tmp / "acme-hello"
        self.write({
            "pyproject.toml": "[project]\nname = \"acme_hello\"\n",
            "acme_hello/__init__.py": "__version__ = \"0.3.0\"\n",
            "acme_hello/hooks.py": "app_name = \"acme_hello\"\napp_title = \"Acme Hello\"\n"
                "app_description = \"Says hello.\"\n",
            "acme_hello/api/__init__.py": "",
            "acme_hello/api/greet.py": self.ENDPOINT,
            "acme_hello/tests/test_greet.py": "import frappe\n@frappe.whitelist()\ndef helper():\n\treturn 1\n",
            "acme_hello/acme_hello/doctype/acme_hello_note/acme_hello_note.json": (
                '{"doctype": "DocType", "name": "Acme Hello Note", "module": "Acme Hello", '
                '"fields": [{"fieldname": "note", "fieldtype": "Data", "label": "Note"}]}'),
        })
        self._saved = (g.REPO, g.EXTENSION)
        g.REPO = self.repo

    def tearDown(self):
        g.REPO, g.EXTENSION = self._saved
        self._tmp.cleanup()

    def write(self, files):
        for rel, text in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def endpoints(self):
        g.use_extension(self.root)
        return g.collect_endpoints(g.AuthResolver())

    def test_documents_only_the_extension_and_resolves_core_roles(self):
        (ep,) = self.endpoints()
        self.assertEqual((ep["dotted"], ep["file"], ep["app"]),
            ("acme_hello.api.greet.greet", "acme_hello/api/greet.py", "acme_hello"))
        self.assertEqual(ep["auth"], {"roles": ["Sales User", "System Manager"], "switch": True})
        self.assertEqual(g.auth_display(ep),
            "Roles: Sales User / System Manager — and only while the extension is switched on")

    def test_require_enabled_without_roles_is_the_switch_alone(self):
        self.write({"acme_hello/api/greet.py": self.ENDPOINT.replace(", roles=COUNTER_ROLES", "")})
        (ep,) = self.endpoints()
        self.assertEqual(ep["auth"], {"switch": True})
        self.assertIn("Any authenticated user, only while the extension is switched on", g.auth_display(ep))

    def gated(self, gate: str):
        """ENDPOINT with its first statement replaced by ``gate``."""
        self.write({"acme_hello/api/greet.py": self.ENDPOINT.replace("\trequire_enabled(APP, roles=COUNTER_ROLES)\n", gate)})
        (ep,) = self.endpoints()
        return ep

    def test_the_switch_after_a_permission_gate_is_kept(self):
        # osa_consignment update_consignment_line_prices: the write check first, then require_enabled(APP)
        ep = self.gated(
            "\tif not frappe.has_permission(\"Customer\", ptype=\"write\"):\n"
            "\t\tfrappe.throw(\"no\", frappe.PermissionError)\n"
            "\trequire_enabled(APP)\n")
        self.assertEqual(ep["auth"], {"perm": "write permission on Customer", "switch": True,
            "perm_doctype": "Customer", "perm_ptype": "write"})
        self.assertEqual(g.auth_display(ep), "Requires write permission on Customer (Frappe DocType "
            "permission check) — and only while the extension is switched on.")

    def test_roles_checked_after_a_bare_switch_are_the_role_gate(self):
        ep = self.gated("\trequire_enabled(APP)\n\tfrappe.only_for(COUNTER_ROLES)\n")
        self.assertEqual(ep["auth"], {"switch": True, "roles": ["Sales User", "System Manager"]})
        self.assertEqual(g.auth_display(ep),
            "Roles: Sales User / System Manager — and only while the extension is switched on")

    def test_code_run_before_the_second_gate_makes_the_gate_late(self):
        ep = self.gated("\trequire_enabled(APP)\n\tfrappe.db.get_value(\"Customer\", customer)\n"
            "\tfrappe.only_for(COUNTER_ROLES)\n")
        self.assertEqual(ep["auth"], {"switch": True, "roles": ["Sales User", "System Manager"], "late": True})

    def test_a_soft_permission_filter_says_the_switch_too(self):
        ep = self.gated("\trequire_enabled(APP)\n\tif not frappe.has_permission(\"Customer\"):\n\t\treturn {}\n")
        self.assertEqual(ep["auth"], {"switch": True, "perm": "read permission on Customer", "soft": True,
            "perm_doctype": "Customer", "perm_ptype": "read"})
        self.assertIn("instead of an error — and only while the extension is switched on.", g.auth_display(ep))

    def test_a_gate_helper_merges_its_switch_and_roles_the_same_way(self):
        self.write({"acme_hello/api/greet.py": self.ENDPOINT.replace(
            "\trequire_enabled(APP, roles=COUNTER_ROLES)\n", "\t_check_access()\n")
            + "def _check_access():\n\trequire_enabled(APP)\n\tfrappe.only_for(COUNTER_ROLES)\n"})
        (ep,) = self.endpoints()
        self.assertEqual(ep["auth"], {"switch": True, "roles": ["Sales User", "System Manager"]})

    def test_code_run_before_a_gate_inside_a_gate_helper_makes_it_late(self):
        # The helper is scanned on for the half still missing: a gate reached after code that did something is
        # late there too, not an entry gate (the endpoint body's rule).
        self.write({"acme_hello/api/greet.py": self.ENDPOINT.replace(
            "\trequire_enabled(APP, roles=COUNTER_ROLES)\n", "\t_check_access()\n")
            + "def _check_access():\n\trequire_enabled(APP)\n\tfrappe.db.get_value(\"Customer\", \"x\")\n"
            "\tfrappe.only_for(COUNTER_ROLES)\n"})
        (ep,) = self.endpoints()
        self.assertEqual(ep["auth"], {"switch": True, "roles": ["Sales User", "System Manager"], "late": True})
        self.assertIn("some code executes before this check", g.auth_display(ep))

    def test_reading_the_callers_roles_for_the_check_is_part_of_that_check_wherever_the_scan_goes_on(self):
        # past the switch, the scan for the role gate reads what comes before it the way the
        # first gate is read — `roles = set(frappe.get_roles())` feeding the check is part of that check, so require_enabled(APP)
        # → roles → `if not roles & set(COUNTER_ROLES): throw` is not late, in a gate helper and in the endpoint body. Code that
        # does something before the check still makes it late: a DB read after the roles are read, or inside that read.
        check = "\tif not roles & set(COUNTER_ROLES):\n\t\tfrappe.throw(\"no\")\n"
        for name, before, late in (
            ("the roles read alone", "\troles = set(frappe.get_roles())\n", False),
            ("a DB read after it", "\troles = set(frappe.get_roles())\n\tfrappe.db.get_value(\"Customer\", \"x\")\n", True),
            ("a DB read inside it", "\troles = set(frappe.get_roles(frappe.db.get_value(\"User\", \"x\", \"name\")))\n", True),
        ):
            body = "\trequire_enabled(APP)\n" + before + check
            gate = "\trequire_enabled(APP, roles=COUNTER_ROLES)\n"
            for where, source in (
                ("a gate helper", self.ENDPOINT.replace(gate, "\t_check_access()\n") + "def _check_access():\n" + body),
                ("the endpoint body", self.ENDPOINT.replace(gate, body)),
            ):
                with self.subTest(name, where=where):
                    self.write({"acme_hello/api/greet.py": source})
                    (ep,) = self.endpoints()
                    self.assertEqual(ep["auth"], {"switch": True, "roles": ["Sales User", "System Manager"], **({"late": True} if late else {})})

    def test_a_require_enabled_not_imported_from_the_sdk_is_not_a_gate(self):
        self.write({"acme_hello/api/greet.py": self.ENDPOINT.replace(
            "from ffl_core.extensions import require_enabled\n", "def require_enabled(app, roles=None):\n\tpass\n")})
        (ep,) = self.endpoints()
        self.assertIsNone(ep["auth"])

    def test_the_action_catalog_row_and_its_place(self):
        (ep,) = self.endpoints()
        catalogs = g.render_catalogs([ep])
        path = self.root.resolve() / "acme_hello" / "api_catalog.json"
        self.assertEqual(list(catalogs), [path])
        (row,) = json.loads(catalogs[path])
        self.assertEqual(row, {
            "method": "acme_hello.api.greet.greet", "module": "acme_hello.api.greet",
            "summary": "Say hello.", "description": "",
            "args": [{"name": "customer", "type": "string", "required": True, "desc": "Customer name."}],
            "returns": "dict: {\"message\": str}", "http": ["POST"], "guest": False,
            "auth": {"roles": ["Sales User", "System Manager"], "switch": True},
            "source": "acme_hello/api/greet.py:6"})
        self.assertEqual(g.first_sentence("One thing. Then  another.", 200), "One thing.")
        self.assertEqual(len(g.first_sentence("x" * 300, 200)), 200)

    def test_a_stale_action_catalog_is_stale_for_an_extension(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(g.main(["--app", str(self.root)]), 0)
        catalog = self.root / "acme_hello" / "api_catalog.json"
        self.assertTrue(catalog.is_file())
        self.assertEqual(self.check()[0], 0)
        catalog.write_text("[]\n")
        status, _, stderr = self.check()
        self.assertEqual(status, 3)
        self.assertIn("acme_hello/api_catalog.json — out of date", stderr)

    def test_render_is_the_core_format_for_one_app(self):
        endpoints = self.endpoints()
        files = g.render(endpoints, g.collect_doctypes())
        self.assertEqual(sorted(files), ["openapi.json", "reference/README.md",
            "reference/acme_hello/api.greet.md", "reference/doctypes.md"])
        spec = json.loads(files["openapi.json"])
        self.assertEqual((spec["info"]["title"], spec["info"]["version"]), ("Acme Hello API", "0.3.0"))
        self.assertEqual(list(spec["paths"]), ["/api/method/acme_hello.api.greet.greet"])
        self.assertEqual(list(spec["components"]["schemas"]), ["Acme_Hello_Note"])
        index = files["reference/README.md"]
        self.assertIn("1 whitelisted endpoints · [1 custom DocTypes](doctypes.md)", index)
        self.assertNotIn("index.html", index)          # no Redoc page ships with an extension
        self.assertNotIn("make api-docs", files["reference/acme_hello/api.greet.md"])

    def check(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = g.main(["--app", str(self.root), "--check"])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_check_fails_on_drift_and_passes_when_fresh(self):
        out = self.root / "docs" / "api"
        self.assertEqual(g.main(["--app", str(self.root)]), 0)
        self.assertTrue((out / "reference" / "acme_hello" / "api.greet.md").is_file())
        status, stdout, _ = self.check()
        self.assertEqual((status, "checks OK" in stdout), (0, True))

        page = out / "reference" / "acme_hello" / "api.greet.md"
        page.write_text(page.read_text() + "hand edit\n")
        (out / "reference" / "acme_hello" / "api.gone.md").write_text("old module\n")
        (out / "openapi.json").unlink()
        stale = g.stale_files(out, g.render(g.collect_endpoints(g.AuthResolver()), g.collect_doctypes()))
        self.assertEqual(stale, ["openapi.json — missing", "reference/acme_hello/api.gone.md — no longer generated",
            "reference/acme_hello/api.greet.md — out of date"])
        status, stdout, stderr = self.check()
        self.assertEqual(status, 3)                      # stale, nothing else wrong: 3, not the 1 of a real defect
        self.assertNotIn("checks OK", stdout)            # a stale tree is not OK, even with parity and conformance fine
        # make ext-test and CI check a copy (apps-ext/<id>, ext/<id>): the hint names no path, it says where to run
        self.assertIn("python3 scripts/generate_api_docs.py --app <extension folder>", stderr)
        self.assertNotIn(str(self.root), stderr.split("STALE")[1].split("\n", 1)[1])

    def test_check_status_tells_stale_docs_from_a_defect(self):
        # 0 all good · 3 only stale (a core generator change alone does that; CI warns) · 1 parity or conformance, stale or not
        undocumented = self.ENDPOINT.replace("\tReturns:\n\t    dict: {\"message\": str}\n", "")
        page = self.root / "docs" / "api" / "reference" / "acme_hello" / "api.greet.md"
        for name, source, stale, status in (
            ("fresh, conformant", self.ENDPOINT, False, 0),
            ("stale only", self.ENDPOINT, True, 3),
            ("non-conformant only", undocumented, False, 1),
            ("stale and non-conformant", undocumented, True, 1),
        ):
            self.write({"acme_hello/api/greet.py": source})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(g.main(["--app", str(self.root)]), 0)
            if stale:
                page.write_text(page.read_text() + "hand edit\n")
            got, stdout, stderr = self.check()
            self.assertEqual((got, "checks OK" in stdout, "STALE" in stderr, "CONFORMANCE FAIL" in stderr),
                (status, status == 0, stale, source is undocumented), (name, stderr))

    PROSE = "\tPermissions:\n\t    Sales User / System Manager, while the extension is on.\n"

    def test_a_role_the_core_adds_is_drift_while_a_defect_of_the_extension_stays_red(self):
        # The extension's Permissions prose names the roles COUNTER_ROLES has
        # today; the core adding one makes that prose contradict the gate derived through the core — drift, 3 like
        # stale files, never red. A defect of the extension itself (a missing docstring) is still 1, drift or not.
        prose = self.ENDPOINT.replace("\t\"\"\"\n\trequire_enabled", "\n" + self.PROSE + "\t\"\"\"\n\trequire_enabled")
        undocumented_other = "import frappe\n@frappe.whitelist()\ndef other(x):\n\treturn x\n"
        roles = self.repo / "apps" / "ffl_core" / "ffl_core" / "roles.py"
        for name, core_roles, other, status in (
            ("fresh, prose agrees", "(\"Sales User\", \"System Manager\")", None, 0),
            ("the core adds a role", "(\"Sales User\", \"System Manager\", \"Accounts User\")", None, 3),
            ("an endpoint without a docstring", "(\"Sales User\", \"System Manager\")", undocumented_other, 1),
            ("both", "(\"Sales User\", \"System Manager\", \"Accounts User\")", undocumented_other, 1),
        ):
            with self.subTest(name):
                roles.write_text(f"COUNTER_ROLES = {core_roles}\n")
                self.write({"acme_hello/api/greet.py": prose})
                other_path = self.root / "acme_hello" / "api" / "other.py"
                if other:
                    self.write({"acme_hello/api/other.py": other})
                elif other_path.exists():
                    other_path.unlink()
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(g.main(["--app", str(self.root)]), 0)     # fresh: only the drift or the defect is left
                got, stdout, stderr = self.check()
                self.assertEqual((got, "checks OK" in stdout, "STALE" in stderr), (status, status == 0, False), stderr)
                self.assertEqual("PERMISSIONS DRIFT" in stderr, "Accounts User" in core_roles, stderr)
                self.assertEqual("CONFORMANCE FAIL — undocumented endpoints:" in stderr, other is not None, stderr)
                self.assertNotIn("Permissions prose contradicts the derived gate:", stderr.split("PERMISSIONS DRIFT")[0], "never under a FAIL heading")
                if "Accounts User" in core_roles:
                    drift = stderr.split("PERMISSIONS DRIFT", 1)[1]
                    self.assertIn("acme_hello/api/greet.py:", drift)
                    self.assertIn("drop the hand-written role list", drift)

    def test_the_drift_and_stale_files_together_are_still_3(self):
        # the core change as it lands: the prose contradicts and the committed docs name the old roles
        prose = self.ENDPOINT.replace("\t\"\"\"\n\trequire_enabled", "\n" + self.PROSE + "\t\"\"\"\n\trequire_enabled")
        self.write({"acme_hello/api/greet.py": prose})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(g.main(["--app", str(self.root)]), 0)
        (self.repo / "apps" / "ffl_core" / "ffl_core" / "roles.py").write_text(
            "COUNTER_ROLES = (\"Sales User\", \"System Manager\", \"Accounts User\")\n")
        got, stdout, stderr = self.check()
        self.assertEqual((got, "PERMISSIONS DRIFT" in stderr, "STALE" in stderr, "CONFORMANCE FAIL" in stderr), (3, True, True, False), stderr)

    def test_a_role_the_core_adds_that_a_local_gates_prose_names_is_drift_too(self):
        # sell's gate is its own literal; its prose names Accounts User only to say it is not allowed. The word becomes a
        # "mentioned role" once the core adds it to COUNTER_ROLES (greet's gate) — the core's change, so drift
        sell = ("@frappe.whitelist()\ndef sell(x):\n\t\"\"\"Sell.\n\n\tArgs:\n\t    x: v.\n\n\tReturns:\n\t    v.\n\n"
            "\tPermissions:\n\t    Sales User; an Accounts User cannot.\n\t\"\"\"\n"
            "\trequire_enabled(APP, roles=(\"Sales User\",))\n\treturn x\n")
        self.write({"acme_hello/api/greet.py": self.ENDPOINT + sell})
        roles = self.repo / "apps" / "ffl_core" / "ffl_core" / "roles.py"
        for core_roles, status in (("(\"Sales User\", \"System Manager\")", 0), ("(\"Sales User\", \"System Manager\", \"Accounts User\")", 3)):
            roles.write_text(f"COUNTER_ROLES = {core_roles}\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(g.main(["--app", str(self.root)]), 0)
            got, stdout, stderr = self.check()
            self.assertEqual((got, "CONFORMANCE FAIL" in stderr), (status, False), stderr)
            if status:
                self.assertIn("acme_hello.api.greet.sell — Permissions prose role list (Accounts User, Sales User)", stderr.split("PERMISSIONS DRIFT", 1)[1])

    def test_a_contradiction_among_the_extensions_own_roles_is_its_own_defect(self):
        # stock's gate and the role its prose names are both the extension's own literals: the extension wrote the
        # contradiction, a core change cannot make it — red. greet, before it in the file, reads COUNTER_ROLES from the
        # core: that is greet's provenance, not stock's.
        own = (self.ENDPOINT
            + "@frappe.whitelist()\ndef stock(x):\n\t\"\"\"Stock.\n\n\tArgs:\n\t    x: v.\n\n\tReturns:\n\t    v.\n\n"
            "\tPermissions:\n\t    Stock Manager only.\n\t\"\"\"\n"
            "\trequire_enabled(APP, roles=(\"Stock User\",))\n\treturn x\n"
            "@frappe.whitelist()\ndef take(x):\n\t\"\"\"Take.\n\n\tArgs:\n\t    x: v.\n\n\tReturns:\n\t    v.\n\t\"\"\"\n"
            "\trequire_enabled(APP, roles=(\"Stock Manager\",))\n\treturn x\n")
        self.write({"acme_hello/api/greet.py": own})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(g.main(["--app", str(self.root)]), 0)
        got, stdout, stderr = self.check()
        self.assertEqual((got, "PERMISSIONS DRIFT" in stderr), (1, False), stderr)
        self.assertIn("CONFORMANCE FAIL — Permissions prose contradicts the derived gate:", stderr)
        self.assertIn("acme_hello.api.greet.stock — Permissions prose role list (Stock Manager) contradicts the derived gate (Stock User)", stderr)
        self.assertNotIn("undocumented endpoints", stderr)

    @staticmethod
    def endpoint(name: str, gate: str, prose: str = "") -> str:
        """A documented endpoint ``name`` whose body runs ``gate`` (source lines); a Permissions section when ``prose``."""
        permissions = f"\n\tPermissions:\n\t    {prose}\n" if prose else ""
        return (f"@frappe.whitelist()\ndef {name}(x):\n\t\"\"\"{name.title()}.\n\n\tArgs:\n\t    x: v.\n\n\tReturns:\n\t    v.\n"
            f"{permissions}\t\"\"\"\n{gate}\treturn x\n")

    def check_fresh(self, source: str) -> tuple[int, str]:
        """``source`` as acme_hello/api/greet.py with its docs generated fresh: the check's exit status and stderr."""
        self.write({"acme_hello/api/greet.py": source})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(g.main(["--app", str(self.root)]), 0)
        status, _, stderr = self.check()
        return status, stderr

    def test_prose_contradicting_the_extensions_own_gate_without_the_cores_role_names_is_its_own_defect(self):
        # stock's gate is its own (Stock User), its prose adds System Manager — a role
        # COUNTER_ROLES has (greet's gate). admin's gate names System Manager itself, alone or next to COUNTER_ROLES, so the
        # prose contradicts stock's gate with the core's role names left out as well: the extension's defect, 1 — also when the
        # prose names a role only the core's gate has besides (Sales User). Only when no gate of the extension names System
        # Manager could a core change alone have made the difference — with or without Stock User in the prose: drift, 3.
        for name, admin, prose, status in (
            ("admin's own gate names it", "(\"System Manager\",)", "Stock User, System Manager.", 1),
            ("a gate reading COUNTER_ROLES names it too", "COUNTER_ROLES + (\"System Manager\",)", "Stock User, System Manager.", 1),
            ("and a role only the core's gate has", "(\"System Manager\",)", "Stock User, System Manager, Sales User.", 1),
            ("only the core's gate names it", None, "Stock User, System Manager.", 3),
            ("the prose names that role alone", None, "System Manager only.", 3),
        ):
            with self.subTest(name):
                source = self.ENDPOINT + self.endpoint("stock", "\trequire_enabled(APP, roles=(\"Stock User\",))\n", prose)
                if admin:
                    source += self.endpoint("admin", f"\trequire_enabled(APP, roles={admin})\n")
                got, stderr = self.check_fresh(source)
                self.assertEqual((got, "PERMISSIONS DRIFT" in stderr), (status, status == 3), stderr)
                heading = "PERMISSIONS DRIFT" if status == 3 else "CONFORMANCE FAIL — Permissions prose contradicts the derived gate:"
                self.assertIn("acme_hello.api.greet.stock — Permissions prose role list", stderr.split(heading, 1)[1])

    def test_a_gate_helper_from_the_core_makes_the_roles_the_cores_only_when_it_supplies_them(self):
        # stock calls ffl_core's gate-shaped _check_session() — it gates no role — before
        # its own gate (Stock User), and its prose says Stock Manager (take's own gate). Reading the core's module for that
        # helper does not make stock's roles the core's: 1. _check_counter() gates on COUNTER_ROLES: those roles are the
        # core's, and prose contradicting them is drift: 3.
        make_repo(self.repo, {"ffl_core/session.py": (
            "import frappe\nfrom ffl_core.roles import COUNTER_ROLES\n"
            "def _check_session():\n\tif frappe.session.user == \"Guest\":\n\t\tfrappe.throw(\"log in\")\n"
            "def _check_counter():\n\tfrappe.only_for(COUNTER_ROLES)\n")})
        take = self.endpoint("take", "\trequire_enabled(APP, roles=(\"Stock Manager\",))\n")
        for name, gate, status in (
            ("_check_session() gates no role", "\t_check_session()\n\trequire_enabled(APP, roles=(\"Stock User\",))\n", 1),
            ("_check_counter() gates on COUNTER_ROLES", "\t_check_counter()\n\trequire_enabled(APP)\n", 3),
        ):
            with self.subTest(name):
                source = (self.ENDPOINT + "from ffl_core.session import _check_counter, _check_session\n"
                    + self.endpoint("stock", gate, "Stock Manager only.") + take)
                got, stderr = self.check_fresh(source)
                self.assertEqual((got, "PERMISSIONS DRIFT" in stderr, "CONFORMANCE FAIL" in stderr), (status, status == 3, status == 1), stderr)
                self.assertIn("acme_hello.api.greet.stock — Permissions prose role list (Stock Manager)", stderr)

    def test_a_role_added_to_a_core_constant_alone_is_still_drift(self):
        # the scenario's endpoints with prose that agrees — greet gates on COUNTER_ROLES, stock and admin on
        # roles of their own. The core adds a role to COUNTER_ROLES, one the extension names nowhere (Accounts User) or one its
        # own gate names too (Stock User): greet's gate changed with nothing changed in the extension — drift, 3.
        greet = self.ENDPOINT.replace("\t\"\"\"\n\trequire_enabled", "\n" + self.PROSE + "\t\"\"\"\n\trequire_enabled")
        source = (greet + self.endpoint("stock", "\trequire_enabled(APP, roles=(\"Stock User\",))\n", "Stock User.")
            + self.endpoint("admin", "\trequire_enabled(APP, roles=(\"System Manager\",))\n", "System Manager."))
        roles = self.repo / "apps" / "ffl_core" / "ffl_core" / "roles.py"
        for added, status in (("", 0), (", \"Accounts User\"", 3), (", \"Stock User\"", 3)):
            with self.subTest(added or "COUNTER_ROLES as it was"):
                roles.write_text(f"COUNTER_ROLES = (\"Sales User\", \"System Manager\"{added})\n")
                got, stderr = self.check_fresh(source)
                self.assertEqual((got, "PERMISSIONS DRIFT" in stderr, "CONFORMANCE FAIL" in stderr), (status, status == 3, False), stderr)

    def test_a_checkout_inside_a_folder_named_tests_is_still_scanned(self):
        moved = self.root.parent / "tests" / "acme-hello"          # e.g. deploy/prod/tests/ext_hello
        moved.parent.mkdir()
        self.root.rename(moved)
        self.root = moved
        self.assertEqual([ep["dotted"] for ep in self.endpoints()], ["acme_hello.api.greet.greet"])
        self.assertEqual(g.token_whitelist_scan(), (1, []))

    def test_app_root_must_hold_exactly_one_hooks_package(self):
        with self.assertRaises(SystemExit):
            g.use_extension(self.root / "acme_hello")


class ServerUrls(unittest.TestCase):
    """openapi "servers": API_DOCS_POS_API_URL / API_DOCS_PROD_URL, read at call time, neutral by default."""

    GOOD = ("import frappe\n\n\n@frappe.whitelist()\ndef ping():\n"
            "    \"\"\"Ping.\n\n    Returns:\n        str: pong.\n    \"\"\"\n    return 'pong'\n")

    def urls(self, **env):
        import os
        from unittest import mock

        clean = {k: v for k, v in os.environ.items() if not k.startswith("API_DOCS_")}
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True):
            return g.server_urls()

    def spec_servers(self, **env) -> list[str]:
        """The server URLs of an openapi.json generated from a one-endpoint fake repo."""
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = make_repo(Path(tmpdir), {"ffl_core/api/m.py": self.GOOD})
            saved = g.REPO, g.OUT
            g.REPO, g.OUT = tmp, tmp / "docs" / "api"
            clean = {k: v for k, v in os.environ.items() if not k.startswith("API_DOCS_")}
            try:
                with mock.patch.dict(os.environ, {**clean, **env}, clear=True), mock.patch.object(g, "app_version", return_value="1.0.0"), \
                        contextlib.redirect_stdout(io.StringIO()):
                    g.main([])
            finally:
                g.REPO, g.OUT = saved
            return [srv["url"] for srv in json.loads((tmp / "docs" / "api" / "openapi.json").read_text())["servers"]]

    def test_defaults_are_the_placeholder_host(self):
        self.assertEqual(self.urls(), ("https://pos.example.com",) * 2)

    def test_both_set(self):
        self.assertEqual(self.urls(API_DOCS_POS_API_URL="https://api.shop.test", API_DOCS_PROD_URL="https://pos.shop.test"),
                         ("https://api.shop.test", "https://pos.shop.test"))

    def test_one_set_the_other_follows_it_and_no_placeholder_leaks(self):
        self.assertEqual(self.urls(API_DOCS_POS_API_URL="https://api.shop.test"), ("https://api.shop.test",) * 2)
        self.assertEqual(self.urls(API_DOCS_PROD_URL="https://pos.shop.test"), ("https://pos.shop.test",) * 2)

    def test_the_generated_spec_follows_the_environment_with_no_duplicate_entry(self):
        self.assertEqual(self.spec_servers(), [g.DEV_URL, "https://pos.example.com"])
        self.assertEqual(self.spec_servers(API_DOCS_POS_API_URL="https://api.shop.test"), [g.DEV_URL, "https://api.shop.test"])
        self.assertEqual(self.spec_servers(API_DOCS_POS_API_URL="https://api.shop.test", API_DOCS_PROD_URL="https://pos.shop.test"),
                         [g.DEV_URL, "https://api.shop.test", "https://pos.shop.test"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
