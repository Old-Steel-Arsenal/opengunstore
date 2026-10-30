"""Gunsmith work orders: a customer's gun left for repair, from intake to pick-up.

A **Gunsmith Work Order** replaces the paper intake form. Its status moves
Received → In Progress → Ready → Picked Up (or Cancelled), only through the
actions here; the form's fields are the paper's.

* **Bound book** — the clerk decides per job. A logged job (``log_in_bound_book``)
  receives the gun through Receive Goods' body as a Gunsmithing acquisition from
  the customer into the customer-custody warehouse at zero cost (``is_ffl_transfer``:
  never for sale, never valued), with the usual synchronous bound-book push; at
  pick-up a Gunsmithing FFL Disposition to the customer takes it out
  (``compliance/disposition_stock`` issues the custody stock). An unlogged job never
  touches stock. A job may be logged later, never un-logged. Returning a repaired
  gun to the person who left it takes no 4473.
* **Authorization** — the customer signs the store's Per document agreement
  (FFL Settings › Gunsmithing) for this job on the signing tablet, by QR or email
  (``ffl_core.agreements``, ``reference_doctype`` / ``reference_name`` = the job).
* **Register** — jobs are taken in and handed back at the POS register, so the
  register's own checkout (cash, card, anything) takes the money: its Work Order
  button takes a job in (:func:`create_work_order`) and Recent Orders › Work Orders
  lists the open ones (:func:`register_work_orders`); the Desk form's Pick up sends
  the clerk there.
* **Payments towards a job** — any amount, any time the job is open, as often as the
  customer likes (Pay now at intake, Take payment later): a GUNSMITH-DEPOSIT line in a
  register sale (:func:`deposit_line`), which may carry other goods too, booked to the
  Customer Deposits liability and untaxed (``setup/gunsmith``). It is a locked line
  (``compliance/locked_lines``): the amount stamped on it when it was made is its amount.
* **Pick-up** — a POS Invoice with the job's charges (labor on the labor item from
  FFL Settings, taxed by that item's own template; parts as ordinary items) and one
  negative deposit line releasing what is held, up to the bill (:func:`complete_work`
  confirms the charges first). Its submit marks the
  job Picked Up and, for a logged gun, books the disposition. A deposit left over
  (cancelled job, or a bill smaller than the deposit) is refunded with a return of
  the deposit invoice (:func:`refund_deposit`).

What is held for a job is read from the submitted POS Invoices' deposit lines
(:func:`deposit_held`): collection +, release −, refund −.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, fmt_money, now_datetime, nowdate

from ffl_core.pos_draft import open_shift, profile_defaults
from ffl_core.price_approval import mark_system_priced
from ffl_core.roles import COUNTER_ROLES
from ffl_core.setup import gunsmith as setup
from ffl_core.setup.customer_deposits import deposit_account
from ffl_core.setup.gunsmith import DEPOSIT_ITEM, LINK_FIELD
from ffl_core.setup.locked_lines import LOCKED_RATE_FIELD, locked_items

DOCTYPE = "Gunsmith Work Order"
RECEIVED, IN_PROGRESS, READY, PICKED_UP, CANCELLED = "Received", "In Progress", "Ready", "Picked Up", "Cancelled"
CLOSED = (PICKED_UP, CANCELLED)
OPEN = (RECEIVED, IN_PROGRESS, READY)
RECEIVE_SOURCE = "Gunsmithing"
ACQUISITION_TYPE = "Gunsmithing"
NOTIFY_CHANNELS = ("Phone", "Email", "Text")
_READY_EMAIL = "Gunsmith Work Ready"
_TOL = 0.005


# ------------------------------------------------------------------ helpers


def _job(name: str, write: bool = True):
	"""The work order, after the caller's own role gate, checked against the user's permission on it."""
	doc = frappe.get_doc(DOCTYPE, name)
	doc.check_permission("write" if write else "read")
	return doc


def _save(doc) -> None:
	doc.flags.via_work_order = True
	doc.save(ignore_permissions=True)


def _money(value, company=None) -> str:
	return fmt_money(flt(value), currency=frappe.get_cached_value("Company", company, "default_currency")
		if company else None)


def charges_total(doc) -> float:
	return flt(sum(flt(r.qty) * flt(r.rate) for r in doc.get("lines") or []), 2)


def deposit_held(work_order: str, exclude: str | None = None) -> float:
	"""Deposit collected for *work_order* and not yet applied or refunded: the sum of its
	deposit lines on submitted POS Invoices (a return's line is negative). *exclude*
	leaves one invoice out (the one being validated)."""
	rows = frappe.db.sql("""
		SELECT COALESCE(SUM(i.amount), 0) FROM `tabPOS Invoice Item` i
		INNER JOIN `tabPOS Invoice` p ON p.name = i.parent
		WHERE p.docstatus = 1 AND i.item_code = %s AND i.{link} = %s AND p.name != %s
	""".format(link=LINK_FIELD), (DEPOSIT_ITEM, work_order, exclude or ""))
	return flt(rows[0][0] if rows else 0, 2)


def _open_draft(work_order: str, kind: str) -> str | None:
	"""The open draft deposit (``deposit``), pick-up (``pickup``) or refund (``refund``) invoice."""
	if not frappe.db.has_column("POS Invoice", LINK_FIELD):
		return None
	for row in frappe.get_all("POS Invoice", filters={LINK_FIELD: work_order, "docstatus": 0},
			fields=["name", "is_return"], order_by="creation"):
		amounts = frappe.get_all("POS Invoice Item", filters={"parent": row.name, "item_code": DEPOSIT_ITEM},
			pluck="amount")
		if kind == "refund" and row.is_return:
			return row.name
		if kind == "deposit" and not row.is_return and any(flt(a) > 0 for a in amounts):
			return row.name
		if kind == "pickup" and not row.is_return and not any(flt(a) > 0 for a in amounts):
			return row.name
	return None


def _refresh_held(work_order: str) -> None:
	frappe.db.set_value(DOCTYPE, work_order, "deposit_held", deposit_held(work_order), update_modified=False)


# ------------------------------------------------------------------ state for the form


@frappe.whitelist()
def get_state(name: str) -> dict:
	"""What the work order form shows and offers.

	Args:
	    name: Gunsmith Work Order name.

	Returns:
	    {"labor_item", "deposit_held", "charges", "authorization": {"template",
	    "state", "agreement"} or None, "drafts": {"deposit", "pickup", "refund"}}.
	"""
	frappe.only_for(COUNTER_ROLES)
	doc = _job(name, write=False)
	from ffl_core.agreements import agreement_status

	template = setup.agreement_template()
	auth = agreement_status(doc.customer, template, reference=(DOCTYPE, doc.name)) if template else None
	return {
		"labor_item": setup.labor_item(),
		"deposit_held": deposit_held(doc.name),
		"charges": charges_total(doc),
		"authorization": auth,
		"drafts": {k: _open_draft(doc.name, k) for k in ("deposit", "pickup", "refund")},
	}


# ------------------------------------------------------------------ the register

# What the register's intake dialog may set on a new job (the rest is the form's).
_INTAKE_FIELDS = ("customer", "manufacturer", "model", "caliber", "serial_number", "firearm_type", "accessories",
	"condition", "work_requested", "estimated_cost", "estimated_completion", "log_in_bound_book",
	"id_type", "id_number")



@frappe.whitelist(methods=["POST"])
def create_work_order(payload: str | dict) -> dict:
	"""Take a gun in at the register (its Work Order button): the job, logged in the
	bound book now when asked, and the payment line for what the customer pays now.

	Args:
	    payload: JSON object of the job's intake fields — customer, manufacturer, model,
	        serial_number and work_requested required; caliber, firearm_type, accessories,
	        condition, estimated_cost, estimated_completion; log_in_bound_book with id_type
	        and id_number to log the gun now; pay_now, an amount paid towards the job now.

	Returns:
	    {"name", "customer", "customer_name", "status", "purchase_receipt", "authorization_template": the intake
	    authorization the register asks the customer to sign next, or None, "deposit_line": the locked
	    GUNSMITH-DEPOSIT row for pay_now (as :func:`deposit_line`), or None}.
	"""
	frappe.only_for(COUNTER_ROLES)
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	if not isinstance(data, dict):
		frappe.throw(_("The work order details did not come through. Try again."))
	setup.ensure()
	pay_now = flt(data.get("pay_now"), 2)
	if pay_now < 0:
		frappe.throw(_("Pay now is an amount paid towards the job, 0 or more."))
	doc = frappe.get_doc({"doctype": DOCTYPE, **{f: data.get(f) for f in _INTAKE_FIELDS if data.get(f) not in (None, "")}})
	doc.log_in_bound_book = cint(doc.log_in_bound_book)
	doc.insert()   # the user's own create permission; a logged gun is received in on_update (log_gun)
	return {"name": doc.name, "customer": doc.customer, "customer_name": doc.customer_name, "status": doc.status,
		"purchase_receipt": doc.purchase_receipt, "authorization_template": setup.agreement_template(),
		"deposit_line": _deposit_row(doc, pay_now) if pay_now > 0 else None}


@frappe.whitelist(methods=["POST"])
def deposit_line(name: str, amount: float) -> dict:
	"""A payment towards an open job (Recent Orders › Take payment): the locked
	GUNSMITH-DEPOSIT row for the register's cart. Booked when that sale is paid.

	Args:
	    name: Gunsmith Work Order name; Received, In Progress or Ready.
	    amount: What the customer pays now, above 0.

	Returns:
	    {"customer", "deposit_line": {"item_code", "qty", "rate", "ffl_locked_rate", "income_account",
	    "ffl_work_order", "description", …}}.
	"""
	frappe.only_for(COUNTER_ROLES)
	doc = _job(name)
	setup.ensure()
	if doc.status not in OPEN:
		frappe.throw(_("A {0} work order takes no payment.").format(_(doc.status)))
	amount = flt(amount, 2)
	if amount <= 0:
		frappe.throw(_("Enter the amount the customer pays now."))
	return {"customer": doc.customer, "deposit_line": _deposit_row(doc, amount)}


@frappe.whitelist()
def register_work_orders(search_term: str | None = None, name: str | None = None) -> dict:
	"""The open jobs for the register's Recent Orders › Work Orders list, with what its
	Take payment and Complete actions need.

	Args:
	    search_term: Part of the job's name, customer, gun or serial; optional.
	    name: One job only (the Desk form sending the clerk to the register); optional.

	Returns:
	    {"labor_item", "orders": [{"name", "customer", "customer_name", "status", "manufacturer",
	    "model", "caliber", "serial_number", "work_requested", "work_performed", "estimated_cost",
	    "deposit_held", "logged", "lines": [{"item_code",
	    "description", "qty", "rate"}], "drafts": {"deposit", "pickup", "refund"}, "authorization": the intake
	    authorization's state (signed / pending / missing …) for an open job, else None}], "authorization_template"}:
	    open jobs (Ready first), then closed ones still holding a payment to refund.
	"""
	frappe.only_for(COUNTER_ROLES)
	# Open jobs, and closed ones still holding a payment (their Refund deposit).
	filters = [["status", "in", OPEN]]
	if name:
		filters.append(["name", "=", name])
	or_filters = None
	term = (search_term or "").strip()
	if term:
		like = f"%{term}%"
		or_filters = {f: ("like", like) for f in ("name", "customer", "customer_name", "manufacturer", "model",
			"serial_number")}
	# lazy: newest 100 open jobs, one query per job for its deposit / drafts; a shop has a handful open.
	# Upgrade path: one aggregate over POS Invoice Item for the page.
	fields = ["name", "customer", "customer_name", "status", "manufacturer", "model", "caliber", "serial_number",
		"work_requested", "work_performed", "estimated_cost", "purchase_receipt"]
	closed = [["status", "in", CLOSED], ["deposit_held", ">", _TOL]] + [f for f in filters if f[0] == "name"]
	rows = frappe.get_list(DOCTYPE, filters=filters, or_filters=or_filters, limit_page_length=100,
		order_by="modified desc", fields=fields)
	rows += frappe.get_list(DOCTYPE, filters=closed, or_filters=or_filters, limit_page_length=100,
		order_by="modified desc", fields=fields)
	lines = {}
	if rows:
		for r in frappe.get_all("Gunsmith Work Order Line", filters={"parent": ("in", [r.name for r in rows]),
				"parenttype": DOCTYPE}, fields=["parent", "item_code", "description", "qty", "rate"], order_by="idx"):
			lines.setdefault(r.pop("parent"), []).append(r)
	from ffl_core.agreements import agreement_status

	template = setup.agreement_template()
	rank = {READY: 0, IN_PROGRESS: 1, RECEIVED: 2}
	orders = []
	for r in sorted(rows, key=lambda r: rank.get(r.status, 3)):
		r["deposit_held"] = deposit_held(r.name)
		r["logged"] = bool(r.pop("purchase_receipt"))
		r["lines"] = lines.get(r.name, [])
		r["drafts"] = {k: _open_draft(r.name, k) for k in ("deposit", "pickup", "refund")}
		r["authorization"] = agreement_status(r.customer, template, reference=(DOCTYPE, r.name))["state"] \
			if template and r.status in OPEN else None
		orders.append(r)
	return {"labor_item": setup.labor_item(), "authorization_template": template, "orders": orders}


@frappe.whitelist()
def labor_item() -> str:
	"""The labor item a new job's first charge line uses (FFL Settings › Gunsmithing,
	which the counter can't read itself).

	Returns:
	    The Item name.
	"""
	frappe.only_for(COUNTER_ROLES)
	return setup.labor_item()


@frappe.whitelist()
def charge_details(item_code: str, customer: str | None = None) -> dict:
	"""A part picked or scanned onto a job's charges: its name and its list price for the
	customer, as Create Order prices a line (``price_approval.list_rate``). The counter
	can't read Item Price itself.

	Args:
	    item_code: The Item charged.
	    customer: The job's customer, whose price list applies; else the default selling list.

	Returns:
	    {"item_name", "rate": the list price, 0 when there is none}.
	"""
	from erpnext.accounts.party import get_default_price_list

	from ffl_core.price_approval import list_rate

	frappe.only_for(COUNTER_ROLES)
	if not item_code or not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item {0} not found.").format(escape_html(item_code or "")))
	price_list = (get_default_price_list(frappe.get_cached_doc("Customer", customer))
		if customer and frappe.db.exists("Customer", customer) else None) \
		or frappe.db.get_single_value("Selling Settings", "selling_price_list")
	doc = frappe._dict(customer=customer, selling_price_list=price_list, posting_date=nowdate())
	rate, _own = list_rate(doc, frappe._dict(item_code=item_code, qty=1))
	return {"item_name": frappe.db.get_value("Item", item_code, "item_name"), "rate": flt(rate)}


@frappe.whitelist(methods=["POST"])
def complete_work(name: str, work_performed: str, lines: str | list | None = None) -> dict:
	"""The register's Complete: the clerk confirms what was done and what it costs; the
	job keeps them, is Ready, and is handed back as :func:`open_pickup` does — at once
	with nothing to bill and nothing held, else through the draft pick-up invoice the
	register loads into its cart.

	Args:
	    name: Gunsmith Work Order name; Received, In Progress or Ready, with no pick-up
	        invoice open.
	    work_performed: What was done.
	    lines: JSON list of the charges, [{"item_code", "description", "qty", "rate"}];
	        they replace the job's.

	Returns:
	    As :func:`open_pickup`: {"pos_invoice", "already": False} or {"picked_up": True, "refund_due"}.
	"""
	frappe.only_for(COUNTER_ROLES)
	doc = _job(name)
	setup.ensure()
	if doc.status not in OPEN:
		frappe.throw(_("A {0} work order cannot be completed.").format(_(doc.status)))
	if _open_draft(doc.name, "pickup"):
		frappe.throw(_("A pick-up invoice for this work order is open at the register. Open it from Recent "
			"Orders, or delete it first."))
	if _open_draft(doc.name, "deposit"):
		frappe.throw(_("A deposit invoice for this work order is still open at the register. Complete or delete "
			"it first."))
	charges = frappe.parse_json(lines) if isinstance(lines, str) else (lines or [])
	locked = locked_items()
	rows = []
	for r in charges or []:
		item_code = (r.get("item_code") or "").strip()
		if not item_code:
			continue
		if item_code in locked or not frappe.db.exists("Item", item_code):
			frappe.throw(_("{0} cannot be a charge on a work order.").format(escape_html(item_code)))
		if flt(r.get("rate")) < 0 or flt(r.get("qty") or 1) <= 0:
			frappe.throw(_("A charge needs a quantity above 0 and a rate of 0 or more ({0}).").format(
				escape_html(item_code)))
		rows.append({"item_code": item_code, "description": (r.get("description") or "").strip() or None,
			"qty": flt(r.get("qty")) or 1, "rate": flt(r.get("rate"))})
	doc.work_performed = (work_performed or "").strip()
	if not doc.work_performed:
		frappe.throw(_("Write what was done in Work Performed first."), title=_("Work performed"))
	doc.set("lines", rows)
	if doc.status != READY:
		doc.status = READY
		doc.completed_on = nowdate()
	_save(doc)
	return _pickup(doc)


# ------------------------------------------------------------------ status


@frappe.whitelist(methods=["POST"])
def start_work(name: str) -> dict:
	"""Received (or Ready, reopened) → In Progress.

	Args:
	    name: Gunsmith Work Order name.

	Returns:
	    {"status"}.
	"""
	frappe.only_for(COUNTER_ROLES)
	doc = _job(name)
	if doc.status not in (RECEIVED, READY):
		frappe.throw(_("A {0} work order cannot be started.").format(_(doc.status)))
	if _open_draft(doc.name, "pickup"):
		frappe.throw(_("A pick-up invoice for this work order is open at the register. Delete it first."))
	doc.status = IN_PROGRESS
	doc.completed_on = None
	_save(doc)
	return {"status": doc.status}


@frappe.whitelist(methods=["POST"])
def mark_ready(name: str) -> dict:
	"""Received / In Progress → Ready: the work is done and recorded.

	Args:
	    name: Gunsmith Work Order name; needs Work Performed.

	Returns:
	    {"status", "completed_on"}.
	"""
	frappe.only_for(COUNTER_ROLES)
	doc = _job(name)
	if doc.status not in (RECEIVED, IN_PROGRESS):
		frappe.throw(_("A {0} work order cannot be marked ready.").format(_(doc.status)))
	if not (doc.work_performed or "").strip():
		frappe.throw(_("Write what was done in Work Performed first."), title=_("Work performed"))
	doc.status = READY
	doc.completed_on = nowdate()
	_save(doc)
	return {"status": doc.status, "completed_on": str(doc.completed_on)}


@frappe.whitelist(methods=["POST"])
def notify_customer(name: str, via: str) -> dict:
	"""Tell the customer the gun is ready, and record who told them, how and when.

	Email sends the store's "Gunsmith Work Ready" email now (the customer needs an
	email address). Phone and Text are made by staff; this records them.

	Args:
	    name: Gunsmith Work Order name; must be Ready.
	    via: Phone, Email or Text.

	Returns:
	    {"notified_via", "notified_on", "email": address the email went to, or None}.
	"""
	frappe.only_for(COUNTER_ROLES)
	doc = _job(name)
	if via not in NOTIFY_CHANNELS:
		frappe.throw(_("Unknown channel: {0}").format(escape_html(via)))
	if doc.status != READY:
		frappe.throw(_("Notify the customer once the work order is Ready."))
	email = None
	if via == "Email":
		email = (doc.email or frappe.db.get_value("Customer", doc.customer, "email_id") or "").strip()
		if not email:
			frappe.throw(_("{0} has no email address. Call or text them, then record that.").format(
				escape_html(doc.customer_name or doc.customer)), title=_("No email address"))
		from ffl_core.store_identity import company_brand
		tpl = frappe.get_doc("Email Template", _READY_EMAIL)
		ctx = {"customer_name": doc.customer_name or doc.customer, "work_order": doc.name,
			"firearm": " ".join(p for p in (doc.manufacturer, doc.model) if p),
			"store_name": company_brand(doc.company).get("name") or ""}
		frappe.sendmail(recipients=[email], subject=frappe.render_template(tpl.subject, ctx),
			message=frappe.render_template(tpl.response_html if tpl.use_html else tpl.response, ctx),
			reference_doctype=DOCTYPE, reference_name=doc.name, now=False)
	doc.notified_via = via
	doc.notified_on = now_datetime()
	doc.notified_by = frappe.session.user
	_save(doc)
	return {"notified_via": via, "notified_on": str(doc.notified_on), "email": email}


@frappe.whitelist(methods=["POST"])
def cancel_work_order(name: str, reason: str) -> dict:
	"""Close a job that will not be done. A gun in the bound book still goes back through
	pick-up (its disposition), so a logged job is not cancelled: clear its charges and
	pick it up instead. A deposit held is refunded afterwards (:func:`refund_deposit`).

	Args:
	    name: Gunsmith Work Order name; not Picked Up or Cancelled.
	    reason: Why.

	Returns:
	    {"status", "deposit_held"}.
	"""
	frappe.only_for(COUNTER_ROLES)
	doc = _job(name)
	if doc.status in CLOSED:
		frappe.throw(_("This work order is already {0}.").format(_(doc.status)))
	if not (reason or "").strip():
		frappe.throw(_("Say why the work order is cancelled."))
	if doc.purchase_receipt:
		frappe.throw(_("This gun is in the bound book, so it goes back to the customer through pick-up (its "
			"disposition). Clear the charges and use Pick up instead."), title=_("Logged gun"))
	if _open_draft(doc.name, "deposit"):
		frappe.throw(_("A deposit invoice is still open at the register. Complete or delete it first."))
	if _open_draft(doc.name, "pickup"):
		frappe.throw(_("A pick-up invoice for this work order is open at the register. Delete it first."))
	doc.status = CANCELLED
	doc.cancel_reason = reason.strip()
	_save(doc)
	return {"status": doc.status, "deposit_held": deposit_held(doc.name)}


# ------------------------------------------------------------------ bound book


@frappe.whitelist(methods=["POST"])
def log_in_bound_book(name: str, id_type: str | None = None, id_number: str | None = None) -> dict:
	"""Log a job's gun in the bound book now (a job created unlogged, or one whose
	first attempt failed).

	Args:
	    name: Gunsmith Work Order name; not Picked Up or Cancelled.
	    id_type: The customer's ID type, when not on the job yet.
	    id_number: The customer's ID number, when not on the job yet.

	Returns:
	    {"purchase_receipt", "serial_no", "item_code"}.
	"""
	frappe.only_for(COUNTER_ROLES)
	doc = _job(name)
	if doc.status in CLOSED:
		frappe.throw(_("A {0} work order cannot be logged.").format(_(doc.status)))
	if doc.purchase_receipt:
		return {"purchase_receipt": doc.purchase_receipt, "serial_no": doc.serial_no, "item_code": doc.item_code}
	doc.log_in_bound_book = 1
	if id_type:
		doc.id_type = id_type
	if id_number:
		doc.id_number = id_number
	_save(doc)   # the controller's on_update logs it (log_gun)
	return {"purchase_receipt": doc.purchase_receipt, "serial_no": doc.serial_no, "item_code": doc.item_code}


def log_gun(doc) -> None:
	"""Receive the job's gun into customer custody and the bound book, in the caller's
	transaction: a refused bound-book push rolls the job's save back with it.

	Through Receive Goods' own body (``receive_goods._create_receive``): an
	individual's Gunsmithing acquisition, ``is_ffl_transfer`` (custody warehouse, zero
	cost, never sellable). A serial the store has had before keeps its Item; a new one
	gets a per-gun Item as a counter intake would."""
	from ffl_core.api.firearm_intake import _resolve_item
	from ffl_core.api.receive_goods import _create_receive
	from ffl_core.api.trade_in import seller_defaults

	missing = [label for field, label in (("caliber", _("Caliber / Gauge")), ("firearm_type", _("Firearm Type")),
		("id_type", _("ID Type")), ("id_number", _("ID Number"))) if not (doc.get(field) or "").strip()]
	if not all((doc.get(f) or "").strip() for f in ("address_line1", "city", "state", "zip")):
		# The register's intake takes the address from the customer, so point there.
		missing.append(_("the customer's address (street, city, state, ZIP — on the work order, or a primary "
			"address on the customer)"))
	if missing:
		frappe.throw(_("To log the gun in the bound book, fill in: {0}.").format(", ".join(missing)),
			title=_("Bound book"))
	who = seller_defaults(doc.customer)
	if not (who.get("source_first_name") and who.get("source_last_name")):
		frappe.throw(_("The bound book needs the customer's first and last name: add them on the customer."))
	serial = doc.serial_number.strip()
	item_code = frappe.db.get_value("Serial No", serial, "item_code")
	if frappe.db.get_value("Serial No", serial, "status") == "Active":
		frappe.throw(_("Serial {0} is already in stock here. Check the serial number.").format(escape_html(serial)))
	if not item_code:
		item_code = _resolve_item({"manufacturer": doc.manufacturer, "model_name": doc.model, "caliber": doc.caliber,
			"firearm_type": doc.firearm_type, "serial": serial})
	received = _create_receive({
		"acquisition_source": "Individual",
		"source": RECEIVE_SOURCE,
		"acquisition_type": ACQUISITION_TYPE,
		"is_ffl_transfer": 1,
		"seller_customer": doc.customer,
		"seller_first_name": who.get("source_first_name"),
		"seller_middle_name": who.get("source_middle_name"),
		"seller_last_name": who.get("source_last_name"),
		"seller_suffix": who.get("source_suffix"),
		"seller_address_line1": doc.address_line1,
		"seller_city": doc.city,
		"seller_state": doc.state,
		"seller_zip": doc.zip,
		"id_type": doc.id_type,
		"id_number": doc.id_number,
		"id_document": doc.id_document,
		"notes": _("Gunsmith work order {0}").format(doc.name),
		"items": [{"item_code": item_code, "qty": 1, "rate": 0, "serials": [serial],
			"manufacturer": doc.manufacturer, "model_name": doc.model, "caliber": doc.caliber,
			"firearm_type": doc.firearm_type}],
	})
	from ffl_core.feature_gate import customer_id_numbers_enabled

	logged = {"purchase_receipt": received["name"], "serial_no": serial, "item_code": item_code}
	if not customer_id_numbers_enabled():
		# The acquisition record keeps the ID the bound book needs; the job keeps none.
		# The scan's File already went to the acquisition with the receive.
		logged["id_number"] = logged["id_document"] = None
	doc.db_set(logged, update_modified=False)


# ------------------------------------------------------------------ authorization


def link_intake_agreement(agreement, method=None) -> None:
	"""Customer Agreement on_update: a copy requested for a work order is that job's
	intake authorization (the latest one wins)."""
	if agreement.get("reference_doctype") != DOCTYPE or not agreement.get("reference_name"):
		return
	if agreement.status == "Void":
		return
	if frappe.db.get_value(DOCTYPE, agreement.reference_name, "intake_agreement") != agreement.name:
		frappe.db.set_value(DOCTYPE, agreement.reference_name, "intake_agreement", agreement.name,
			update_modified=False)


# ------------------------------------------------------------------ register invoices


def _new_invoice(doc, items: list[dict]):
	shift = open_shift(doc.company)
	inv = frappe.get_doc({
		"doctype": "POS Invoice", "company": doc.company, "pos_profile": shift.pos_profile,
		"customer": doc.customer, LINK_FIELD: doc.name, "items": items,
		**profile_defaults(shift.pos_profile),
	})
	inv.flags.ignore_permissions = True
	inv.set_missing_values()
	return inv


def _deposit_row(doc, amount: float) -> dict:
	account = deposit_account(doc.company)
	if not account:
		frappe.throw(_("{0} has no Customer Deposits account yet. Run bench migrate, or ask a manager.").format(
			doc.company))
	return {"item_code": DEPOSIT_ITEM, "qty": 1, "rate": amount, LOCKED_RATE_FIELD: amount, "price_list_rate": 0,
		"income_account": account, LINK_FIELD: doc.name,
		"description": _("Payment towards gunsmith work order {0}").format(doc.name)}


@frappe.whitelist(methods=["POST"])
def open_pickup(name: str) -> dict:
	"""Hand the gun back. With nothing to charge and no deposit held, the job is
	picked up now; otherwise this opens (or returns) the draft register invoice with
	the charges and the deposit held coming off them, and its submit picks the job up.

	Args:
	    name: Gunsmith Work Order name; must be Ready.

	Returns:
	    {"pos_invoice", "already": bool} or {"picked_up": True, "refund_due": deposit still held, refunded
	    with :func:`refund_deposit`}.
	"""
	frappe.only_for(COUNTER_ROLES)
	doc = _job(name)
	setup.ensure()
	if doc.status != READY:
		frappe.throw(_("Mark the work order Ready before pick-up."))
	existing = _open_draft(doc.name, "pickup")
	if existing:
		return {"pos_invoice": existing, "already": True}
	if _open_draft(doc.name, "deposit"):
		frappe.throw(_("A deposit invoice for this work order is still open at the register. Complete or delete "
			"it first."))
	return _pickup(doc)


def _pickup(doc) -> dict:
	"""Hand a Ready job back (no draft of either kind open, the caller checked): picked
	up now with nothing to bill, else the draft pick-up invoice — the job's charges and
	a locked negative deposit line releasing what is held, up to the bill with tax."""
	held = deposit_held(doc.name)
	labor = setup.labor_item()
	# The labor charge every job starts with, left at $0, is no labor billed; a $0 part
	# still goes on the bill (it leaves stock).
	lines = [r for r in doc.get("lines") or [] if not (r.item_code == labor and not flt(r.rate))]
	if not lines:
		# Nothing to bill: the gun goes back now, and any deposit is refunded after.
		complete_pickup(doc)
		return {"picked_up": True, "refund_due": held}
	labor_desc = (doc.work_performed or "").strip()
	items = [{"item_code": r.item_code, "qty": flt(r.qty) or 1, "rate": flt(r.rate), "price_list_rate": flt(r.rate),
		"description": (r.description or (labor_desc if r.item_code == labor else "") or None), LINK_FIELD: doc.name}
		for r in lines]
	inv = _new_invoice(doc, items)
	# The job's charges are the bill the work order set: no manager price approval for them here
	# (price_approval.mark_system_priced); a lower price at the register asks as any other.
	mark_system_priced(inv)
	inv.calculate_taxes_and_totals()
	release = min(held, flt(inv.rounded_total) or flt(inv.grand_total))
	if release > _TOL:
		inv.append("items", _deposit_row(doc, -flt(release, 2)))
		inv.set_missing_values()
	inv.insert()
	# Pricing may have moved a charge on the way in; the job's charges are the bill.
	want = {i: flt(r.rate) for i, r in enumerate(lines, 1)}
	moved = False
	for row in inv.items:
		if row.item_code != DEPOSIT_ITEM and row.idx in want and flt(row.rate) != want[row.idx]:
			row.rate = row.price_list_rate = want[row.idx]
			row.discount_amount = row.discount_percentage = 0
			moved = True
	if moved:
		inv.save()
	return {"pos_invoice": inv.name, "already": False}


@frappe.whitelist(methods=["POST"])
def refund_deposit(name: str) -> dict:
	"""Open (or return) the draft register return that pays back the deposit still held
	— a cancelled job's, or what a smaller bill left over. A return goes against one
	payment and refunds at most that payment, newest first; with several payments held
	the rest is refunded with the next return.

	Args:
	    name: Gunsmith Work Order name; Cancelled or Picked Up, with deposit held.

	Returns:
	    {"pos_invoice", "already": bool, "amount": what this return pays back}.
	"""
	frappe.only_for(COUNTER_ROLES)
	doc = _job(name)
	if doc.status not in CLOSED:
		frappe.throw(_("Refund the deposit once the work order is picked up or cancelled; until then it comes off "
			"the bill at pick-up."))
	existing = _open_draft(doc.name, "refund")
	if existing:
		return {"pos_invoice": existing, "already": True, "amount": deposit_held(doc.name)}
	held = deposit_held(doc.name)
	if held <= _TOL:
		frappe.throw(_("No deposit is held for this work order."))
	# Each payment is returned once (ERPNext counts returned qty per line) and never above itself.
	source = frappe.db.sql("""
		SELECT p.name, i.amount FROM `tabPOS Invoice` p INNER JOIN `tabPOS Invoice Item` i ON i.parent = p.name
		WHERE p.docstatus = 1 AND p.is_return = 0 AND i.item_code = %s AND i.{link} = %s AND i.amount > 0
			AND NOT EXISTS (SELECT 1 FROM `tabPOS Invoice` r WHERE r.return_against = p.name AND r.docstatus = 1)
		ORDER BY p.creation DESC LIMIT 1""".format(link=LINK_FIELD), (DEPOSIT_ITEM, doc.name))
	if not source:
		frappe.throw(_("The deposit invoice for this work order was not found."))
	from erpnext.accounts.doctype.pos_invoice.pos_invoice import make_sales_return

	amount = min(held, flt(source[0][1], 2))
	ret = make_sales_return(source[0][0])
	ret.items = [r for r in ret.items if r.item_code == DEPOSIT_ITEM]
	for r in ret.items:
		r.qty = -1
		r.rate = amount
		r.set(LOCKED_RATE_FIELD, amount)
		r.set(LINK_FIELD, doc.name)
	ret.set(LINK_FIELD, doc.name)
	ret.flags.ignore_permissions = True
	ret.set_missing_values()
	ret.calculate_taxes_and_totals()
	# The refund goes back the way the payment came in, split across the modes in the same
	# shares as the source sale ($10 cash + $20 card → a third cash, two thirds card), so a
	# card is never asked for more than it was charged; the register refunds the card part
	# when it checks the return out (Payroc refunds run there, never on the Desk).
	paid = {}
	for mode, amt in frappe.get_all("Sales Invoice Payment", filters={"parent": source[0][0],
			"parenttype": "POS Invoice"}, fields=["mode_of_payment", "amount"], as_list=True):
		paid[mode] = paid.get(mode, 0) + max(flt(amt), 0)
	total_paid = sum(paid.values())
	refund = flt(ret.rounded_total) or flt(ret.grand_total)   # negative
	rows = ret.get("payments") or []
	for p in rows:
		p.amount = p.base_amount = 0
	shares = [(p, paid.get(p.mode_of_payment, 0)) for p in rows if paid.get(p.mode_of_payment, 0) > 0]
	if not shares and rows:
		shares = [(rows[0], 1)]
		total_paid = 1
	left = refund
	for i, (p, share) in enumerate(shares):
		part = left if i == len(shares) - 1 else flt(refund * share / total_paid, 2)
		p.amount = p.base_amount = part
		left = flt(left - part, 2)
	# Totals do not re-sum a POS return's tender; the header must agree with the rows.
	ret.paid_amount = ret.base_paid_amount = sum(flt(p.amount) for p in ret.get("payments") or [])
	ret.insert()
	return {"pos_invoice": ret.name, "already": False, "amount": amount}


def complete_pickup(doc, invoice: str | None = None) -> None:
	"""The gun went back to the customer: Picked Up and, for a logged gun, the
	Gunsmithing disposition to them (which issues the custody stock)."""
	if doc.purchase_receipt and not doc.disposition:
		disp = frappe.get_doc({
			"doctype": "FFL Disposition", "disposition_date": now_datetime(), "disposition_type": "Gunsmithing",
			"company": doc.company, "customer": doc.customer, "item": doc.item_code, "serial": doc.serial_no,
			"notes": _("Returned to the owner after gunsmithing (work order {0}).").format(doc.name),
		})
		disp.flags.ignore_permissions = True
		disp.insert()
		disp.submit()
		doc.disposition = disp.name
	doc.status = PICKED_UP
	doc.picked_up_on = now_datetime()
	if invoice:
		doc.pickup_invoice = invoice
	_save(doc)


# ------------------------------------------------------------------ invoice hooks


def expected_deposit_rate(doc, row) -> float | None:
	"""``compliance/locked_lines``: the amount a deposit line is locked to, or None to
	leave it to :func:`guard_deposit_lines`. A payment (+) is what was stamped on it when
	it was made (:func:`deposit_line`); a release (−) is what was locked when the pick-up
	was built, never more than is held."""
	wo = (row.get(LINK_FIELD) if hasattr(row, "get") else getattr(row, LINK_FIELD, None)) or ""
	if not wo or cint(getattr(doc, "is_return", 0) or 0):
		return None
	locked = flt(getattr(row, LOCKED_RATE_FIELD, 0))
	if locked > 0:
		return flt(locked, 2)
	if locked < 0:
		held = deposit_held(wo, exclude=getattr(doc, "name", None))
		return -min(-locked, held) if held > _TOL else None
	return None


def guard_deposit_lines(doc, method=None) -> None:
	"""POS Invoice / Sales Invoice validate: a GUNSMITH-DEPOSIT line comes only from a
	work order, for that order's customer, once per invoice: a payment on an open job at
	the amount stamped when it was made, a release never more than is held. A consolidated
	Sales Invoice is a copy and is left alone; a Sales Invoice made by hand may not carry one."""
	rows = [r for r in doc.get("items") or [] if r.item_code == DEPOSIT_ITEM]
	if not rows or cint(doc.get("is_consolidated")):
		return
	if doc.doctype != "POS Invoice":
		frappe.throw(_("A gunsmith deposit is taken and applied at the register, from its work order."))
	if len(rows) > 1:
		frappe.throw(_("One gunsmith deposit line per invoice."))
	row = rows[0]
	wo = row.get(LINK_FIELD)
	if not wo or not frappe.db.exists(DOCTYPE, wo):
		frappe.throw(_("A gunsmith deposit line comes from its work order (Pay now / Take payment / Complete), not "
			"from the item list. Remove the line."), title=_("Gunsmith deposit"))
	job = frappe.db.get_value(DOCTYPE, wo, ["customer", "status"], as_dict=True)
	if doc.customer != job.customer:
		frappe.throw(_("Work order {0} belongs to another customer.").format(wo))
	doc.set(LINK_FIELD, wo)
	amount = flt(row.amount, 2)
	held = deposit_held(wo, exclude=doc.name)
	if cint(doc.is_return):
		if -amount > held + _TOL:
			frappe.throw(_("Only {0} of deposit is held for {1}.").format(_money(held, doc.company), wo))
		return
	locked = flt(row.get(LOCKED_RATE_FIELD), 2)
	if flt(row.qty) != 1 or (amount > 0 and (locked <= 0 or abs(amount - locked) > _TOL)):
		frappe.throw(_("A payment line for {0} comes from its work order (Pay now / Take payment), one at the "
			"amount it was made for. Remove the line and add it again.").format(wo), title=_("Gunsmith deposit"))
	if amount > 0:
		if job.status in CLOSED:
			frappe.throw(_("Work order {0} is {1}; it takes no payment.").format(wo, _(job.status)))
	else:
		if job.status != READY:
			frappe.throw(_("Work order {0} is {1}: the deposit comes off the bill at pick-up, once it is Ready.")
				.format(wo, _(job.status)))
		if -amount > held + _TOL:
			frappe.throw(_("Only {0} of deposit is held for {1}.").format(_money(held, doc.company), wo))


def _jobs_on(doc) -> list[str]:
	"""The work orders an invoice touches: its header link (one job), then its rows' — a
	register sale may carry a job's payment next to other goods, and the header is only
	set by the deposit guard or the pick-up builder."""
	out = []
	for wo in [doc.get(LINK_FIELD)] + [r.get(LINK_FIELD) for r in doc.get("items") or []]:
		if wo and wo not in out and frappe.db.exists(DOCTYPE, wo):
			out.append(wo)
	return out


def before_invoice_cancel(doc, method=None) -> None:
	"""POS Invoice before_cancel: a deposit already applied or refunded stays, and so does a
	pick-up whose gun has left the bound book."""
	for wo in _jobs_on(doc):
		if deposit_held(wo, exclude=doc.name) < -_TOL:
			frappe.throw(_("The deposit on {0} has already come off a pick-up or been refunded. Cancel that invoice "
				"first.").format(wo), title=_("Deposit already used"))
		if cint(doc.is_return):
			continue
		job = frappe.db.get_value(DOCTYPE, wo, ["pickup_invoice", "disposition"], as_dict=True) or frappe._dict()
		if job.pickup_invoice == doc.name and job.disposition \
				and frappe.db.get_value("FFL Disposition", job.disposition, "docstatus") == 1:
			frappe.throw(_("The gun on {0} was returned to its owner in the bound book ({1}). Cancel that disposition "
				"first.").format(wo, job.disposition), title=_("Gun already returned"))


def on_invoice_submit(doc, method=None) -> None:
	"""POS Invoice on_submit, per job the invoice touches: keep its deposit held current;
	a sale that pays towards it is its (latest) deposit invoice, whatever else it sells;
	any other carrying it — its charges, its deposit release, or the header link — picks
	a Ready job up."""
	for wo in _jobs_on(doc):
		_refresh_held(wo)
		if cint(doc.is_return):
			continue
		job = frappe.get_doc(DOCTYPE, wo)
		paid = sum(flt(r.amount) for r in doc.items
			if r.item_code == DEPOSIT_ITEM and r.get(LINK_FIELD) == wo and flt(r.amount) > 0)
		if paid > 0:
			# Paid at Intake is the first payment actually rung, not what the dialog said (a cart can be abandoned).
			job.db_set({"deposit_invoice": doc.name, **({} if flt(job.deposit_amount) else {"deposit_amount": paid})},
				update_modified=False)
		elif job.status == READY:
			complete_pickup(job, invoice=doc.name)


def on_invoice_cancel(doc, method=None) -> None:
	"""POS Invoice on_cancel: the deposit held follows, and a cancelled pick-up puts
	the job back to Ready."""
	for wo in _jobs_on(doc):
		_refresh_held(wo)
		job = frappe.get_doc(DOCTYPE, wo)
		if not cint(doc.is_return) and job.pickup_invoice == doc.name and job.status == PICKED_UP:
			job.status = READY
			job.picked_up_on = None
			_save(job)
