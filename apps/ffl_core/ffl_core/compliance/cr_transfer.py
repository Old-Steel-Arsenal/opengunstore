"""C&R (Type 03) transfer — verification capture for channel orders.

Lives in ffl_core, not in a channel app. It was written for WooCommerce web
orders and moved here when GunBroker became a second channel that ships to
collectors (GunBroker's C&R buyer share is why the channel is worth having at
all): a compliance rule that decides whether a firearm may ship to a private
address must not exist twice, once per storefront. ``ffl_woo_sync.woocommerce.
cr_transfer`` re-exports every name, so the Woo call sites are unchanged.

The order argument is duck-typed on purpose — it is whatever doctype the
channel uses (``Woo Online Order`` / ``GunBroker Order``) — with ONE exception:
the licence-number FIELD NAME differs between them (``ffl_license`` vs
``ffl_number``), so the two write paths take it as an explicit ``license_field``
keyword rather than guessing. See ``_persist_cr_details``.

A Type 03 "Curio & Relic" collector holds a real FFL, so a firearm may ship
directly to them; the receiving premises is their own home address and there is
no dealer at the far end to run a 4473. That removes the two things that make
an ordinary web transfer safe on their own — the ATF directory lookup that
supplies the destination's name/address/expiry, and a second licensee who
checks the paperwork on arrival. What replaces them is a human at this end
reading the licence copy at dispose time.

This module is that capture, and the gate over it:

* `_validate_cr_details` — the SERVER-SIDE final defense. The dispose dialog
  enforces the same rules, and is bypassable by anyone who can call
  `frappe.call`. Nothing here trusts the client.
* `_persist_cr_details` — writes what was confirmed onto the order, before the
  disposition is attempted, so a later failure never costs the operator the
  re-typing (and so the A&D trail records what was attested to, when, by whom).
* `ensure_cr_ffl_record` — projects those confirmed values into the `ATF FFL
  Record` that the shared shippable-FFL gate already knows how to judge.
* `_clear_cr_verification` — used here and by the webhook backfill: a
  verification is *about a licence number*, so when the number changes the
  verification is void.

Deliberately NOT here: any eZ Check / ATF directory lookup. The ATF's public
FFL list does not contain Type 03 collectors at all (that is why this feature
exists), so a lookup cannot succeed and pretending otherwise would replace a
human check with a failed query.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import get_datetime, getdate, now_datetime, nowdate

from ffl_core.compliance.ffl_number import (
	CR_LICENSE_RE,
	CR_LICENSE_TYPE_LABEL,
	is_cr_license,
)
from ffl_core.utils import commit  # test-safe: a no-op under frappe.flags.in_test

# The eight keys of the frozen `cr_details` payload, with the
# operator-facing label used when one is missing. `premise_street2` is
# deliberately absent — Address 2 is optional.
CR_REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
	("license_number", "C&R license number"),
	("license_expiry", "License expiration date"),
	("contact_name", "Collector's full name"),
	("premise_street", "Address 1"),
	("premise_city", "City"),
	("premise_state", "State"),
	("premise_zip", "ZIP"),
)
CR_OPTIONAL_FIELDS: tuple[str, ...] = ("premise_street2",)
CR_DETAIL_KEYS: tuple[str, ...] = tuple(k for k, _label in CR_REQUIRED_FIELDS) + CR_OPTIONAL_FIELDS

# `cr_details` key -> order fieldname. These nine names are IDENTICAL on every
# channel order doctype (Woo Online Order, GunBroker Order) and are read/written
# by attribute, so a channel that renames one of them silently drops the value
# (Frappe's get_valid_dict keeps only fields the meta knows). The licence NUMBER
# is the deliberate exception and is not in this map — it is `ffl_license` on
# Woo and `ffl_number` on GunBroker, so it travels as the `license_field`
# keyword instead of by convention.
_DETAIL_TO_FIELD: dict[str, str] = {
	"contact_name": "cr_contact_name",
	"license_expiry": "cr_license_expiry",
	"premise_street": "cr_premise_street",
	"premise_street2": "cr_premise_street2",
	"premise_city": "cr_premise_city",
	"premise_state": "cr_premise_state",
	"premise_zip": "cr_premise_zip",
}

# Every field the verification owns, including the audit stamps.
CR_VERIFICATION_FIELDNAMES: tuple[str, ...] = tuple(_DETAIL_TO_FIELD.values()) + (
	"cr_verified_by", "cr_verified_at")

_VERIFICATION_REQUIRED_TITLE = "C&R Verification Required"
_LICENSE_EXPIRED_TITLE = "C&R License Expired"


def parse_cr_details(raw) -> dict:
	"""Normalize the `cr_details` argument into a flat dict of stripped strings.

	`frappe.call` serializes a nested object as a JSON string, so the same
	argument arrives as a dict from Python callers and as `str` from the
	browser. Unknown keys are dropped rather than carried forward — the payload
	shape is frozen, and silently persisting an unexpected key is how a typo'd
	field name turns into a blank on the order.
	"""
	if not raw:
		return {}
	if isinstance(raw, str):
		try:
			raw = frappe.parse_json(raw)
		except Exception:
			return {}
	if not isinstance(raw, dict):
		return {}
	return {k: str(raw.get(k) or "").strip() for k in CR_DETAIL_KEYS}


def _parse_date(value: str):
	"""Date or None. Uses get_datetime, never getdate: getdate reports an invalid
	date through frappe.throw, which appends to message_log even when the raise
	is caught — the stray message then rides back to the browser on an unrelated
	HTTP 200 (the same trap documented on fulfillment._ffl_status)."""
	if not value:
		return None
	try:
		dt = get_datetime(value)
	except Exception:
		return None
	return dt.date() if dt else None


def _zip5(value: str) -> str:
	digits = "".join(ch for ch in (value or "") if ch.isdigit())
	return digits[:5]


def _validate_cr_details(cr_details, order) -> dict:
	"""Validate the staff-entered C&R details for `order`; throw if unusable.

	Returns the cleaned payload (stripped, state normalized). Runs BEFORE
	anything is written, so a rejected verification leaves no trace.

	Check order is chosen for the operator, not the compiler: report a blank
	field as "you left City empty" rather than as "malformed licence", and only
	talk about expiry once the date is actually parseable.
	"""
	details = parse_cr_details(cr_details)
	if not details or not any(details.values()):
		frappe.throw(_(
			"C&R order {0} needs verification before it can be disposed — click "
			"Dispose and complete the C&R form (license number, expiration, "
			"collector name, licensed premises)."
		).format(getattr(order, "name", "")), title=_(_VERIFICATION_REQUIRED_TITLE))

	missing = [label for key, label in CR_REQUIRED_FIELDS if not details.get(key)]
	if missing:
		frappe.throw(_("Missing C&R details: {0}.").format(", ".join(missing)),
			title=_(_VERIFICATION_REQUIRED_TITLE))

	license_number = details["license_number"]
	if not CR_LICENSE_RE.match(license_number):
		frappe.throw(_(
			"{0} is not a Type 03 (Curio & Relic) license — the fourth group must "
			"be 03, in the form 1-23-456-03-7C-89012."
		).format(license_number), title=_(_VERIFICATION_REQUIRED_TITLE))

	expiry = _parse_date(details["license_expiry"])
	if not expiry:
		frappe.throw(_(
			"{0} is not a readable expiration date — enter it as shown on the "
			"license."
		).format(details["license_expiry"]), title=_(_VERIFICATION_REQUIRED_TITLE))
	# A licence is valid THROUGH its expiration date, so only an earlier date is
	# expired.
	if expiry < getdate(nowdate()):
		frappe.throw(_("C&R license {0} expired on {1}.").format(license_number, expiry),
			title=_(_LICENSE_EXPIRED_TITLE))
	# Hand the PARSED date onward, not the raw string. Two reasons, both real:
	# the value we validated is then the value that gets stored (the same
	# "persisted != validated" trap the \Z anchor exists to avoid), and the
	# re-verification comparison in _persist_cr_details ends up date-vs-date. A
	# Date column reads back as datetime.date, so comparing it against the
	# submitted string would never match and would re-stamp the audit trail on
	# every dispose retry.
	details["license_expiry"] = expiry

	from ffl_core.compliance.interstate_tax import normalize_us_state
	state = normalize_us_state(details["premise_state"])
	if not state:
		frappe.throw(_('"{0}" is not a valid US state.').format(details["premise_state"]),
			title=_(_VERIFICATION_REQUIRED_TITLE))
	details["premise_state"] = state

	# The parcel ships to the ORDER's address. If the licensed premises is in a
	# different state or ZIP, shipping it there is a substantive violation, not a
	# formatting difference — block and make someone reconcile the two.
	# Street/city wording differences are deliberately allowed:
	# "123 Main St" vs "123 Main Street" is the same doorway, and blocking it
	# only teaches staff to retype the address until the gate stops complaining.
	ship_state = normalize_us_state(getattr(order, "ship_to_state", "") or "")
	if ship_state and state != ship_state:
		frappe.throw(_(
			"The licensed premises state ({0}) does not match this order's "
			"shipping state ({1}). A C&R firearm must ship to the licensed "
			"premises — correct the shipping address on the source order first."
		).format(state, ship_state), title=_("C&R Address Mismatch"))

	ship_zip = _zip5(getattr(order, "ship_to_zip", "") or "")
	if ship_zip and _zip5(details["premise_zip"]) != ship_zip:
		frappe.throw(_(
			"The licensed premises ZIP ({0}) does not match this order's shipping "
			"ZIP ({1}). A C&R firearm must ship to the licensed premises — "
			"correct the shipping address on the source order first."
		).format(details["premise_zip"], ship_zip), title=_("C&R Address Mismatch"))

	return details


def reject_cr_details_on_non_cr_order(cr_details, order, *,
		license_field: str = "ffl_license") -> None:
	"""Refuse `cr_details` on an order that is not a C&R transfer.

	Cheap guard against a mixed-up call stamping collector verification onto an
	ordinary dealer transfer, where it would be both meaningless and misleading
	in the audit trail.

	``license_field`` is a DEAD parameter here and is accepted only so the two
	write paths have the same signature — a caller shouldn't have to remember
	which of the pair takes it. This function reads nothing but ``order.name``,
	so it deliberately does NOT carry ``_persist_cr_details``' ``has_field``
	assertion: making a pure validator depend on a field it never touches would
	be a false coupling, not extra safety.
	"""
	# `any(values)`, not truthiness of the dict: parse_cr_details always returns
	# all eight keys, so an empty object would otherwise be "present" while
	# unparseable junk (which parses to {}) would slip through — exactly backwards.
	if cr_details and any(parse_cr_details(cr_details).values()):
		frappe.throw(_(
			"Order {0} is not a C&R transfer — cr_details is not accepted."
		).format(getattr(order, "name", "")))


def _persist_cr_details(order, details: dict, *,
		license_field: str = "ffl_license") -> None:
	"""Write the confirmed values onto the order (in memory; caller saves).

	Also reconciles the licence number itself: the dialog lets staff CORRECT a
	number the shopper typed, and when they do, the cached `atf_ffl_record`
	describes the previous licence. Leaving it set would send the firearm to the
	old destination, because `_resolve_and_validate_ffl` short-circuits on that
	field and never looks at the licence number again.

	`license_field` names the licence-number column on THIS channel's order
	doctype — `ffl_license` on Woo Online Order (the default, so the Woo call
	sites are unchanged), `ffl_number` on GunBroker Order. It is a parameter and
	not a duck-typed `order.ffl_license` because `Document.__setattr__` accepts
	ANY attribute while `get_valid_dict` keeps only fields the meta knows: on a
	doctype without `ffl_license`, `setattr(order, "ffl_license", corrected)` is
	silently dropped at save while the `atf_ffl_record = ""` two lines below is
	really executed. The result would be the worst possible half-update — the
	resolved destination cleared, the licence number still the one the buyer
	typed wrong — and nothing anywhere would say so.

	The audit stamp records whoever attested to the values that are now stored:
	an identical re-submission (the dispose-retry path) preserves the original
	verifier, while changed values re-stamp — otherwise a correction made by a
	second clerk would be recorded against the first.
	"""
	# Turn that silent data loss into a loud failure. Asserted on the WRITE path
	# only: this is the function that would drop the
	# correction. Gated on `doctype` being a real doctype NAME because that is
	# the only thing `get_meta` can answer for — a duck-typed stand-in (test
	# double, dict-like row) has no meta, and calling get_meta on one raises a
	# less useful error than the one this check exists to give.
	doctype = getattr(order, "doctype", None)
	if isinstance(doctype, str) and doctype \
			and not frappe.get_meta(doctype).has_field(license_field):
		frappe.throw(
			f"{doctype} has no field {license_field!r} — C&R persist would "
			f"silently drop the licence correction.")

	details = dict(details)

	def _same(field: str, value) -> bool:
		"""Compare a stored field with a submitted one without letting a type
		round-trip masquerade as a change. `cr_license_expiry` is a Date column
		and comes back as datetime.date, so a naive != against the payload would
		always differ and would re-stamp the verifier on every retry."""
		stored = getattr(order, field, None)
		if field == "cr_license_expiry":
			return (getdate(stored) if stored else None) == (getdate(value) if value else None)
		return (stored or "") == (value or "")

	changed = not all(_same(field, details.get(key))
		for key, field in _DETAIL_TO_FIELD.items())

	license_number = details.get("license_number") or ""
	if license_number and (getattr(order, license_field, "") or "").strip() != license_number:
		# ATOMIC: the number and its cached resolution move together, in one
		# branch. There is no reachable state where atf_ffl_record has been
		# cleared but the corrected number was not written — that intermediate
		# is the failure mode the has_field assertion above exists to prevent.
		setattr(order, license_field, license_number)
		# Correcting the number invalidates the resolved destination.
		order.atf_ffl_record = ""
		changed = True

	for key, field in _DETAIL_TO_FIELD.items():
		setattr(order, field, details.get(key) or "")

	if changed or not (order.cr_verified_by or ""):
		order.cr_verified_by = frappe.session.user
		order.cr_verified_at = now_datetime()


def record_cr_verification(order, cr_details, *,
		license_field: str = "ffl_license") -> str:
	"""Validate, persist and commit an operator's C&R licence check on a channel
	order (web or GunBroker); return the destination ATF FFL Record.

	Runs BEFORE the destination is resolved, because it is what makes it
	resolvable at all. ``license_field`` names the order's licence column, as in
	:func:`_persist_cr_details`.
	"""
	details = _validate_cr_details(cr_details, order)   # throws; nothing written yet
	_persist_cr_details(order, details, license_field=license_field)
	order.atf_ffl_record = ensure_cr_ffl_record(
		ffl_number=details["license_number"],
		licensee_name=details["contact_name"],
		street=details["premise_street"],
		street2=details["premise_street2"],
		city=details["premise_city"],
		state=details["premise_state"],
		zip_code=details["premise_zip"],
		expiry_date=details["license_expiry"],
		phone=order.customer_phone or "",
	)
	order.flags.ignore_permissions = True
	order.save()
	order.add_comment("Comment", _(
		"C&R verification recorded by {0}: {1} / {2} / {3} {4}."
	).format(frappe.session.user, details["license_number"], details["contact_name"],
		details["premise_city"], details["premise_state"]))
	# Commit boundary: the verification is a completed unit of work. If the
	# disposition that follows fails (a serial vanished, a stock error), Frappe
	# rolls back to the last commit — without this line the nine cr_* fields and
	# the audit Comment go with it, and the operator must re-read and re-key the
	# licence.
	commit()
	return order.atf_ffl_record


def cr_note_suffix(order) -> str:
	"""The bound-book note's C&R sentence: WHO verified the licence and WHEN.
	On this path there is no dealer at the far end and no 4473, so the store's
	own check is the entire paper trail."""
	verified_at = order.cr_verified_at
	stamp = (verified_at.strftime("%Y-%m-%d")
		if hasattr(verified_at, "strftime") else str(verified_at or "")[:10])
	return _(
		"C&R (Type 03) transfer — shipped to the collector's own licensed "
		"premises. Verified by {0} on {1}."
	).format(order.cr_verified_by or "—", stamp or "—")


def _clear_cr_verification(order) -> None:
	"""Void the verification. A verification is *about a licence number*; when
	the number changes the stored name/address/expiry belong to a different
	licensee, and pushing them to FastBound under the new number would put a
	fabricated party in the bound book."""
	for field in CR_VERIFICATION_FIELDNAMES:
		setattr(order, field, "")


def ensure_cr_ffl_record(*, ffl_number: str, licensee_name: str, street: str,
		street2: str, city: str, state: str, zip_code: str, expiry_date,
		phone: str = "", loaded_from: str = "cr_web_order") -> str:
	"""Create/update the destination `ATF FFL Record` for a C&R order; return its name.

	Every value is an explicit argument, and the caller passes what a human just
	confirmed against the licence. Reading them off the order's ship-to inside
	this function would look identical at the call site while quietly demoting
	"verified" to "guessed" — and the end of this chain is the A&D bound book.

	`live_verified_at` is left empty on purpose: a clerk reading a licence copy
	is not an ATF online verification, and stamping it would forge a check that
	never happened. `license_type` uses the same LABEL spelling the eZ Check
	importer writes (one spelling per column — a second one fails silently, by
	returning fewer rows to any equality match rather than by erroring), and
	`loaded_from` marks the provenance for audit — which channel's operator
	read the licence (a web order's dispose, a counter order's destination).
	"""
	ffl_number = (ffl_number or "").strip()
	if not is_cr_license(ffl_number):
		frappe.throw(_("{0} is not a Type 03 (Curio & Relic) license.").format(ffl_number),
			title=_(_VERIFICATION_REQUIRED_TITLE))

	if frappe.db.exists("ATF FFL Record", ffl_number):
		rec = frappe.get_doc("ATF FFL Record", ffl_number)
	else:
		rec = frappe.new_doc("ATF FFL Record")
		rec.ffl_number = ffl_number

	rec.license_type = CR_LICENSE_TYPE_LABEL
	rec.licensee_name = (licensee_name or "").strip()
	rec.street = (street or "").strip()
	rec.street2 = (street2 or "").strip()
	rec.city = (city or "").strip()
	rec.state = (state or "").strip()
	rec.zip = (zip_code or "").strip()
	if phone:
		rec.phone = phone.strip()
	rec.expiry_date = expiry_date
	rec.loaded_from = loaded_from
	rec.loaded_at = now_datetime()
	rec.flags.ignore_permissions = True
	rec.save()
	return rec.name
