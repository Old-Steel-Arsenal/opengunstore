"""Signing Terminal — a store tablet paired to sign agreements.

Paired and polled through ``ffl_core.signing_terminal``; only the sha256 of the
tablet's device key is kept here. Untick Active to cut a lost tablet off.
"""
from __future__ import annotations

from frappe.model.document import Document


class SigningTerminal(Document):
	pass
