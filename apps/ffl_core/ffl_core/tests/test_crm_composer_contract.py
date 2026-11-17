"""What Draft reply and Insert price rely on in Frappe CRM's front end (crm.setup.reply_script_js).

The page script finds CRM's mail composer by TipTap's editor on its element and by the paragraph class
attribute only the composer has, reads the quote CRM's Reply writes, and inserts above it. A CRM upgrade
that moves any of these makes the page fall back to its dialog every time; this test says which anchor
moved, so the upgrade PR re-checks the composer path (docs/versions.md). Reads the installed CRM's
source; skipped where it is not on disk."""
from __future__ import annotations

import os
import re
import unittest

import frappe

ANCHORS = {
	"components/EmailEditor.vue": [
		r"const CustomParagraph = Paragraph\.extend\(\{\s*addAttributes\(\)",
		r"buildEditorExtensions\(\{.{0,200}?extra: \[CustomParagraph\]",
	],
	"components/Activities/EmailArea.vue": [
		re.escape("editor.toEmails = [email.sender]"),
		re.escape("`Re: ${email.subject}`"),
		re.escape("`<blockquote>${message}</blockquote>`"),
		re.escape("updateAttributes('paragraph', { class: 'reply-to-content' })"),
	],
	"components/CommunicationArea.vue": [
		re.escape('v-show="showEmailBox"'),
		re.escape("content: newEmail.value"),
	],
	"data/script.js": [re.escape("new Function(")],
}


def _frontend() -> str:
	return os.path.join(os.path.dirname(frappe.get_app_path("crm")), "frontend")


class TestComposerContract(unittest.TestCase):
	def setUp(self):
		if "crm" not in frappe.get_installed_apps() or not os.path.isdir(os.path.join(_frontend(), "src")):
			self.skipTest("Frappe CRM's front-end source is not on this bench")

	def test_crm_source_anchors(self):
		for path, patterns in ANCHORS.items():
			with open(os.path.join(_frontend(), "src", path), encoding="utf-8") as f:
				source = f.read()
			for pattern in patterns:
				self.assertRegex(source, re.compile(pattern, re.S), f"{path}: {pattern}")

	def test_tiptap_keeps_its_editor_on_the_element(self):
		core = os.path.join(_frontend(), "node_modules", "@tiptap", "core")
		files = [os.path.join(core, p) for p in ("src/Editor.ts", "dist/index.js")]
		files = [f for f in files if os.path.isfile(f)]
		if not files:
			self.skipTest("CRM's node modules are not on this bench (the browser check covers it)")
		found = False
		for path in files:
			with open(path, encoding="utf-8") as f:
				found = found or bool(re.search(r"dom\.editor = this", f.read()))
		self.assertTrue(found, "TipTap no longer sets dom.editor")
