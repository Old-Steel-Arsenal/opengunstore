"""FFL Settings — single doctype: store identity and receipt strings, the
4473 compliance mode, the live EZ Check provider config (rate limit / cache
TTL / provider switch; the data-service connection itself is Data Service
Settings in ffl_integrations), and the Store Customer ID Numbers switch read
by ffl_core.feature_gate. Field rationale lives in
docs/field-notes.md."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class FFLSettings(Document):
	def validate(self):
		"""The peace-officer excise exemption is California's: no account for it on a
		store elsewhere (compliance/le_excise_exempt)."""
		from ffl_core.compliance import le_excise_exempt

		account = self.get(le_excise_exempt.SETTING)
		if account and self.has_value_changed(le_excise_exempt.SETTING) and not le_excise_exempt.in_exempt_state(
				frappe.db.get_value("Account", account, "company")):
			frappe.throw(_("The Peace-Officer Exempt Tax Account is for California stores only "
				"(the store's own address, in Company › Addresses, is not in California)."))

	def on_update(self):
		"""Carry a switch change into the two places that cache what it decides.

		The server gates need nothing here: they read the switch per call.
		The two that do:

		* **The Gun Shop workspace.** Its shortcuts are stored rows, so something
		  has to add and remove them, and a settings save is the moment the
		  operator states the intent.
		* **The per-user bootinfo hash.** ``sessions.get()`` serves an already
		  logged-in Desk session out of ``cache.hget("bootinfo", user)``
		  (frappe 16.22.0, sessions.py:137). The switch flags themselves
		  survive that, because ``extend_bootinfo`` hooks run at sessions.py:167,
		  OUTSIDE the ``if not bootinfo`` block — every boot recomputes them
		  whether or not the blob was cached. What does NOT survive is the rest
		  of the blob, ``allowed_workspaces`` included: a workspace save drops
		  this hash (``Workspace.on_update``, workspace.py:116), a settings save
		  did not. Drop it here instead of depending on which layers a save
		  happened to touch.

		Never raises. A settings save that a workspace hiccup could reject would
		strand the operator with the switch they were trying to set, and the next
		migrate reconciles it anyway.
		"""
		# Outside the try below, on purpose: if the erase fails, the save must fail
		# with it (the switch stays on, nothing half-erased) rather than report an
		# "off" store that still holds ID numbers.
		self._erase_customer_ids_when_switched_off()
		try:
			# Whole key, not hdel(user): the switches are store-wide, so every
			# signed-in session's blob is stale, not just this operator's.
			frappe.cache.delete_key("bootinfo")
		except Exception:
			frappe.log_error(
				title="FFL Settings: workspace shortcut sync failed",
				message=frappe.get_traceback())

	def _erase_customer_ids_when_switched_off(self) -> None:
		"""Store Customer ID Numbers 1 -> 0: erase every stored DOB and ID number.

		Only that transition: 0 -> 0 and 1 -> 1 changed nothing, 0 -> 1 starts
		keeping them. The counts go into a Comment on this record as the trail.
		"""
		previous = self.get_doc_before_save()
		if not (previous and cint(previous.store_customer_id_numbers)
				and self.has_value_changed("store_customer_id_numbers")
				and not cint(self.store_customer_id_numbers)):
			return
		from ffl_core.customer_profile import erase_stored_ids
		erased = erase_stored_ids()
		self.add_comment("Comment", _(
			"{0} turned off Store Customer ID Numbers: erased {1} date(s) of birth and "
			"{2} ID number(s) from customer records.").format(
				frappe.session.user, erased["dates_of_birth"], erased["id_numbers"]))
