"""Dealer shipments: FFL validity gate + automatic bound-book dispositions.

Selling to another FFL lawfully skips the 4473 (compliance/firearm.py exempts
is_ffl_dealer customers), which historically left dealer sales with NO
bound-book exit: the disposition was a separate manual Desk step nobody
enforced — the classic "sold but never de-booked" ATF audit finding.

Wired on Delivery Note and Sales Invoice (ffl_core/hooks.py):

  validate  → require_shippable_ffl_dealer: firearms may only ship to a
			  dealer whose FFL is Verified AND unexpired. Stricter than the
			  purchase-side gate (Verified|Pending) — here WE hand the gun
			  to them. Fires at submit, like the other firearm gates.
  on_submit → create_dealer_dispositions: one submitted "Dealer Transfer"
			  FFL Disposition per serial — but only when THIS document moved
			  the stock (DN, or SI with update_stock=1; a billing-only SI
			  would otherwise trigger disposition_stock's own Material Issue
			  while the gun is still on the shelf, the one real double-issue
			  path). The disposition's own on_submit (ffl_integrations)
			  enqueues the FastBound push — never enqueue here too
			  (compliance/purchase.py learned that double-push lesson).
  on_cancel → cancel_dealer_dispositions: mirror.

No double stock issue on the happy path: doc_events run after the controller
moved stock, the serial is already status=Delivered, and
disposition_stock.issue_stock_on_submit's status gate skips. Manual Dealer
Transfer dispositions (no sales document) keep auto-issuing as before.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime, nowdate

from ffl_core.firearm import is_firearm_item, is_regulated_firearm_item
from ffl_core.serials import serials_for_line


def auto_enable_update_stock_for_dealer_shipment(doc, method: str | None = None) -> None:
	"""Sales Invoice before_insert: a dealer firearm invoice IS the shipping
	document unless a Delivery Note is in play — flip Update Stock on by
	default so staff can't accidentally submit a billing-only invoice that
	never moves the gun or books the disposition. Fires once at creation;
	unchecking afterwards is respected (the SO→DN→SI path stays available)."""
	if int(getattr(doc, "is_return", 0) or 0) or int(getattr(doc, "is_consolidated", 0) or 0):
		return
	if int(getattr(doc, "update_stock", 0) or 0):
		return
	customer = getattr(doc, "customer", None)
	if not customer or not frappe.db.get_value("Customer", customer, "is_ffl_dealer"):
		return
	rows = getattr(doc, "items", None) or []
	if any((row.get("delivery_note") or "").strip() for row in rows):
		return  # billed from a Delivery Note — stock ships (shipped) there
	# Stock movement is an INVENTORY concern, not a bound-book one — antiques are
	# serialized firearms that must ship too, or a billing-only invoice orphans the
	# serial Active forever. So gate on is_firearm_item (antiques included), unlike
	# the disposition/FFL-verify gates below which use is_regulated_firearm_item.
	if not any(is_firearm_item(row.item_code) for row in rows):
		return
	doc.update_stock = 1
	frappe.msgprint(
		_("Update Stock was enabled automatically — a dealer firearm invoice "
			"ships from this document. Uncheck it only if a Delivery Note will "
			"ship the guns instead."),
		alert=True, indicator="blue",
	)


def customer_ffl_shippable(info, *, posting_date=None) -> tuple[bool, str | None]:
	"""Non-throwing check of a dealer Customer's OWN ffl_number /
	ez_check_status / ffl_expiry_date fields — the exact condition set
	require_shippable_ffl_dealer's CUSTOMER-path branch (no explicit
	destination FFL on the document) enforces at submit, extracted here so
	the Consignment Out dealer-picker badge (consignment_out._dealer_
	shippable) and that branch can never drift on wording or ordering.
	`info` needs ffl_number / ez_check_status /
	ffl_expiry_date keys (a dict/frappe._dict of Customer field values).

	NOTE: this is NOT the same gate as ship_consignment_out's actual hard
	stop at ship time (manual_order._require_valid_ffl / atf_record_
	shippable below, ATF-FFL-Record-based) — see that function's docstring
	and _dealer_shippable's for why the two intentionally differ and when
	each applies."""
	if not (info.get("ffl_number") or "").strip():
		return False, _("No FFL number on file.")
	if info.get("ez_check_status") != "Verified":
		return False, _("FFL not verified (status: {0}).").format(
			info.get("ez_check_status") or "—")
	expiry = info.get("ffl_expiry_date")
	posting = getdate(posting_date or nowdate())
	if not expiry or getdate(expiry) < posting:
		return False, _("FFL expired {0}.").format(expiry or _("(no expiry on file)"))
	return True, None


# Reason codes for atf_record_block. A caller that must THROW with its own
# wording and dialog title (manual_order._require_valid_ffl) branches on these
# instead of keeping a second copy of the conditions; a caller that renders a
# badge just uses the message.
FFL_BLOCK_NOT_LINKED = "not_linked"
FFL_BLOCK_NOT_ON_FILE = "not_on_file"
FFL_BLOCK_EXPIRED = "expired"
FFL_BLOCK_INCOMPLETE_ADDRESS = "incomplete_address"


def atf_record_block(atf_ffl_record: str | None, *, posting_date=None,
		require_address: bool = True) -> tuple[str, str] | None:
	"""THE definition of "may a firearm leave the building to this ATF FFL
	Record?" — returns None when it may, or (reason_code, message) when it may
	not. Every other check in this codebase (atf_record_shippable's badge,
	require_shippable_ffl_dealer's submit hook, manual_order._require_valid_ffl's
	ship-time hard stop) is a presentation layer over this function; none of
	them re-states the conditions. See atf_record_shippable below for the
	require_address posture."""
	atf_ffl_record = (atf_ffl_record or "").strip()
	if not atf_ffl_record:
		return FFL_BLOCK_NOT_LINKED, _("No ATF FFL Record linked yet — run Verify FFL.")
	rec = frappe.db.get_value(
		"ATF FFL Record", atf_ffl_record, ["expiry_date", "street", "state"], as_dict=True)
	if not rec:
		return FFL_BLOCK_NOT_ON_FILE, _(
			"{0} is not on file — verify it first.").format(atf_ffl_record)
	posting = getdate(posting_date or nowdate())
	if not rec.expiry_date or getdate(rec.expiry_date) < posting:
		return FFL_BLOCK_EXPIRED, _("FFL expired {0}.").format(
			rec.expiry_date or _("(no expiry on file)"))
	if require_address:
		from ffl_core.compliance.interstate_tax import normalize_us_state
		if not (rec.street or "").strip() or not normalize_us_state(rec.state):
			return FFL_BLOCK_INCOMPLETE_ADDRESS, _(
				"FFL is missing a complete address (street + a valid state).")
	return None


def atf_record_shippable(atf_ffl_record: str | None, *, posting_date=None,
		require_address: bool = True) -> tuple[bool, str | None]:
	"""Non-throwing (bool, reason) view of atf_record_block — the REAL hard
	gate ship_consignment_out enforces before a firearm may leave the
	building: an ATF FFL Record on file, unexpired, and (require_address, the
	default) carrying a complete street + a normalizable US state (the
	destination-based sales-tax engine needs a real address).

	require_address=False is require_shippable_ffl_dealer's dest-set branch's
	own, DELIBERATELY LIGHTER posture: by the time a document reaches submit
	with an explicit destination FFL already on it, upstream
	(create_order / ship_consignment_out) already ran the full
	address-complete check before ever building that document — this is
	just re-verifying the license hasn't expired/vanished between then and
	submit, not re-litigating the address.

	Read-only: unlike dealer_transfer._resolve_destination_ffl, this never
	triggers a live eZ Check network lookup, so it is safe to call from a
	passive list/preview endpoint (the Consignment Out dealer picker) on
	every page render."""
	block = atf_record_block(
		atf_ffl_record, posting_date=posting_date, require_address=require_address)
	return (False, block[1]) if block else (True, None)


def require_shippable_ffl_dealer(doc, method: str | None = None) -> None:
	if int(getattr(doc, "docstatus", 0) or 0) != 1:
		return
	if not _moves_stock(doc):
		return  # billing-only SI: the Delivery Note will gate the shipment
	firearm_rows = [r for r in (getattr(doc, "items", None) or [])
		if is_regulated_firearm_item(r.item_code)]
	if not firearm_rows:
		return

	posting = getdate(getattr(doc, "posting_date", None) or nowdate())

	# Shipped to an explicit destination FFL (build-order page) — applies to
	# individual buyers too, who aren't FFL dealers themselves. A lighter
	# re-check than create-time (require_address=False — see atf_record_
	# shippable's docstring): upstream already ran the full address-complete
	# gate before this document ever existed.
	dest = (getattr(doc, "ffl_destination_ffl", None) or "").strip()
	if dest:
		shippable, reason = atf_record_shippable(
			dest, posting_date=posting, require_address=False)
		if not shippable:
			frappe.throw(_("Destination FFL {0}: {1}").format(dest, reason),
				title=_("Destination FFL Expired"))
		return

	customer = getattr(doc, "customer", None)
	if not customer:
		return
	info = frappe.db.get_value(
		"Customer", customer,
		["is_ffl_dealer", "ffl_number", "ez_check_status", "ffl_expiry_date"],
		as_dict=True,
	)
	if not info or not info.is_ffl_dealer:
		return  # retail path — the 4473 gate owns this case

	shippable, reason = customer_ffl_shippable(info, posting_date=posting)
	if not shippable:
		frappe.throw(_("{0}: {1}").format(customer, reason),
			title=_("Dealer FFL Not Shippable"))


def create_dealer_dispositions(doc, method: str | None = None) -> None:
	"""One submitted transfer disposition per shipped firearm serial — the
	on_submit auto-path. Only books when THIS document moved the stock
	(update_stock=1 / Delivery Note); a billing-only SI defers to the manual
	disposition step (manual_order.dispose_order) so the gun isn't de-booked
	while it's still on the shelf."""
	from ffl_core.compliance.channel_invoice import is_fulfilled_elsewhere

	if is_fulfilled_elsewhere(doc):
		# The gun's bound-book exit was booked on the record that fulfilled
		# it — a channel order, or an extension's own document (an outbound
		# consignment's SHIP leg, say, whose Dealer Transfer disposition links
		# the consignment, not this invoice). Booking a second one here — a
		# settlement SI is update_stock=1 for a dealer customer, exactly the
		# shape this hook otherwise auto-books for — would double-push the
		# same firearm to FastBound; CreateAndCommit rejects a duplicate as a
		# terminal 400 and never claims the existing disposition id.
		return
	if not _moves_stock(doc):
		return
	target = _resolve_transfer_target(doc)
	if not target:
		return
	destination, disposition_type = target
	_register_dispositions(doc, destination=destination,
		disposition_type=disposition_type, link_field=_link_field(doc))


def _resolve_transfer_target(doc) -> tuple[str, str] | None:
	"""(destination ATF FFL Record, disposition_type) for a shipped firearm
	document, or None when no bound-book transfer applies (retail to an
	individual with no destination FFL set).

	An explicit ffl_destination_ffl wins — guns mailed to that FFL, where the
	buyer (individual OR dealer) does the 4473. Otherwise fall back to the
	dealer-customer path: the buyer is itself the receiving licensee."""
	customer = getattr(doc, "customer", None)
	is_dealer = bool(customer and frappe.db.get_value(
		"Customer", customer, "is_ffl_dealer"))

	dest = (getattr(doc, "ffl_destination_ffl", None) or "").strip()
	if dest:
		return dest, ("Dealer Transfer" if is_dealer else "FFL Transfer")

	if not is_dealer:
		return None
	# The formal directory link wins; the free-text number is the fallback
	# for parties that haven't been saved since the link field existed.
	info = frappe.db.get_value(
		"Customer", customer, ["ffl_number", "atf_ffl_record"], as_dict=True)
	destination = (info and info.atf_ffl_record) or _resolve_destination_ffl(
		info.ffl_number if info else "")
	return destination, "Dealer Transfer"


def _register_dispositions(doc, *, destination: str, disposition_type: str,
		link_field: str) -> list[str]:
	"""Insert + submit one FFL Disposition per regulated-firearm serial on the
	doc. Idempotent via the {serial, link_field, docstatus<2} guard so hook
	re-runs and the manual dispose path never double-book. Returns the names
	created this call. Does NOT enqueue FastBound — the disposition's own
	on_submit hook (ffl_integrations) does, after commit."""
	customer = getattr(doc, "customer", None)
	created: list[str] = []
	for row in (getattr(doc, "items", None) or []):
		if not is_regulated_firearm_item(row.item_code):
			continue
		for serial in serials_for_line(row):
			name = book_disposition(
				serial=serial, item=row.item_code, link_field=link_field,
				link_name=doc.name, destination=destination,
				disposition_type=disposition_type, company=doc.company,
				customer=customer, sales_order=row.get("sales_order"),
				notes=_("Auto-booked from {0} {1}.").format(doc.doctype, doc.name))
			if name:
				created.append(name)
	return created


def book_disposition(*, serial: str, item: str, link_field: str, link_name: str,
		destination: str, disposition_type: str, company: str | None, notes: str,
		customer: str | None = None, sales_order: str | None = None,
		disposition_date=None) -> str | None:
	"""Insert + submit one FFL Disposition for a serial shipped out on a sales
	document (Delivery Note / Sales Invoice, Woo Online Order, GunBroker Order).
	The one bound-book write shared by the counter, web and GunBroker paths.

	Idempotent via the {serial, link_field: link_name, docstatus<2} guard so a
	retried dispose or hook re-run never double-books: returns None when one
	already exists, else the new disposition's name. Does NOT enqueue
	FastBound — the disposition's own on_submit hook (ffl_integrations) does."""
	if frappe.db.exists("FFL Disposition", {
		"serial": serial, link_field: link_name, "docstatus": ("<", 2),
	}):
		return None
	disp = frappe.get_doc({
		"doctype": "FFL Disposition",
		"disposition_date": disposition_date or now_datetime(),
		"disposition_type": disposition_type,
		"company": company,
		"customer": customer,
		"destination_ffl": destination,
		"item": item,
		"serial": serial,
		link_field: link_name,
		"sales_order": sales_order,
		"notes": notes,
	})
	disp.flags.ignore_permissions = True
	disp.insert()
	disp.submit()
	return disp.name


def serial_company(serial: str) -> str | None:
	"""The company that stocks a serial, via its warehouse — the FFL Disposition's
	required `company` on the channel paths. (The stock-out Material Issue
	derives the warehouse from the serial itself.)"""
	warehouse = frappe.db.get_value("Serial No", serial, "warehouse")
	return frappe.db.get_value("Warehouse", warehouse, "company") if warehouse else None


def cancel_dealer_dispositions(doc, method: str | None = None) -> None:
	# Both Dealer Transfer (dealer buyer) and FFL Transfer (individual buyer
	# shipped to a destination FFL) are auto-booked from this document — cancel
	# either, or an individual-buyer order leaks its bound-book + FastBound row.
	for name in frappe.get_all(
			"FFL Disposition",
			filters={_link_field(doc): doc.name, "docstatus": 1,
				"disposition_type": ("in", ("Dealer Transfer", "FFL Transfer"))},
			pluck="name"):
		disp = frappe.get_doc("FFL Disposition", name)
		disp.flags.ignore_permissions = True
		disp.cancel()


# ------------------------------------------------------------------ internals


def _moves_stock(doc) -> bool:
	if getattr(doc, "doctype", "") == "Delivery Note":
		return True
	return bool(int(getattr(doc, "update_stock", 0) or 0))


def _link_field(doc) -> str:
	return "delivery_note" if getattr(doc, "doctype", "") == "Delivery Note" \
		else "sales_invoice"


def _resolve_destination_ffl(ffl_number: str) -> str:
	"""ATF FFL Record for the dealer's license (record name == FFL number).
	Missing → verify-and-create live via eZ Check; failure blocks the
	shipment — a gun must not leave against an unresolvable license."""
	ffl_number = (ffl_number or "").strip()
	if frappe.db.exists("ATF FFL Record", ffl_number):
		return ffl_number
	try:
		from ffl_integrations.atf.ez_check import _lookup_and_create
		result = _lookup_and_create(ffl_number)  # {ok, found, name, …} — body, not the gated wrapper (on_submit/guest chain)
		if result and result.get("name"):
			return result["name"]
	except ImportError:
		pass
	except Exception:
		frappe.log_error(title="dealer_transfer: FFL lookup failed",
			message=frappe.get_traceback())
	frappe.throw(_(
		"Could not resolve FFL {0} to an ATF record — run Verify FFL on the "
		"Customer (or check the eZ Check connection) before shipping."
	).format(ffl_number), title=_("Destination FFL Unresolved"))
