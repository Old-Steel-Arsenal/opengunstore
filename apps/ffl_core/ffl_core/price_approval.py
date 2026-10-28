"""Manager approval for a price a person lowers at the counter.

FFL Settings → Require Manager Approval for Price Reductions (:func:`enabled`; on for a
new store, off on a store that predates it until a manager turns it on). While it is on,
a line below its list price — a rate typed lower, a line discount % or amount — or an
invoice discount saves only with a manager's approval. A Sales Manager or System Manager
(``roles.PRICE_APPROVER_ROLES``) signed in approves their own; anyone else asks one, who
picks themselves on the counter screen and types their own counter PIN (their password
when they have none) with a reason — ``staff_switch``'s PIN and its lockouts.

* **What asks** — :func:`reductions`, the same answer for the validate gate and the
  screens' check: a line whose rate is under its *floor*. The floor is the line's list
  price (:func:`list_rate`: a firearm's own price with its sale running — the cheapest
  such gun of the model while the line has no serial yet — else the Item Price of the
  document's price list), lowered by any Pricing Rule ERPNext applied to the
  line on this save; never above the rate the line was saved at before, nor (an invoice
  line made from a Sales Order line) the rate that line was approved at on the order. So
  raising a price, keeping one, or a discount the system gives asks nothing: only what a
  person lowered in THIS save does. The invoice discount the same way: above what was
  saved and above a transaction Pricing Rule's.
* **The gate** — :func:`enforce_price_approval` (POS Invoice / Sales Invoice / Sales
  Order validate; the client prompt is a convenience, a REST save meets the same refusal).
  Only a save a person sends is judged (:func:`_client_save`): the desk / register save
  (``savedocs``), ``frappe.client`` writes and the REST document API — or a save our own
  endpoint opts in (:func:`attach`: Create Order and its edit, whose line prices a clerk
  types). Every other save — a builder's ticket, a sync, a hook saving another document — is
  our code's own pricing and is not asked, with or without :func:`mark_system_priced`.
  Approved by: an approval token, the approval our endpoint took (``doc.flags``), or the
  signed-in manager. **Returns** (owner decision 2026-10-28): a return a person sends that gives
  more back than it did as saved asks the same way — the dialog lists the returned lines and the
  refund; one approval covers that return up to its refund total. Never asked either (see
  :func:`_exempt`): POS consolidation; a channel mirror or an invoice an extension books
  (``channel_invoice.is_fulfilled_elsewhere``) whose claim was already stored — a save
  cannot buy its way out by setting the field; counter pickup tickets (pinned to $0); lines a
  distributor fills that our code marked, or that were saved so before; background jobs,
  migrate, install and import.
* **The token** — :func:`approve` checks the approver and returns a single-use token,
  kept 15 minutes in the cache with who approved, why, who asked and what: the lowest rate
  approved per gun (a line carrying one serial) or else per item, and the invoice discount.
  The screen sends it back on the save in
  ``ffl_price_approval_token`` (a hidden field this gate empties, so it is never stored);
  validate takes it once, for the user who asked for it, and puts it back if the save
  rolls back. A lower rate later needs a new approval; raising one back never does.
* **The record** — ``ffl_price_approved_by`` / ``ffl_price_approval_reason`` /
  ``ffl_price_approved_on`` on the document, written here only
  (:func:`guard_approval_fields`; cleared once nothing on the document is below list any
  more; an invoice made from an approved Sales Order takes the order's while it still
  carries a lowered line), and a timeline comment listing each line, list price → approved
  rate.
"""
from __future__ import annotations

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, fmt_money, get_datetime, now_datetime, nowdate

from ffl_core.roles import PRICE_APPROVER_ROLES, PRICE_CHECK_ROLES
from ffl_core.utils import single_flag, split_serials

GATED = ("POS Invoice", "Sales Invoice", "Sales Order")
SETTING = "require_price_approval"
BY, REASON, ON = "ffl_price_approved_by", "ffl_price_approval_reason", "ffl_price_approved_on"
RECORD_FIELDS = (BY, REASON, ON)
TOKEN_FIELD = "ffl_price_approval_token"
TOKEN_TTL = 15 * 60
_TOKEN_KEY = "ffl-price-approval:"
ORDER = "order"   # what a Create Order token approves (no document yet)
# doc.flags, server-only (frappe never loads `flags` from a client's JSON: RESERVED_KEYWORDS).
SYSTEM_PRICED = "ffl_system_priced"   # our own code set these prices on purpose
APPROVAL = "ffl_price_approval"        # the approval this document is saved under
PRIOR_RATES = "ffl_prior_rates"        # {(item_code, serial or None): rate} an order edit carries over
CHECKED = "ffl_price_checked"          # our endpoint saves a person's prices: judge it like a client save
QUOTED = "ffl_quoted_line"             # row.flags: a distributor line our code priced from the quote
# The client entry points that save a document a person sent (frappe.desk.form.save / frappe.client).
_CLIENT_SAVES = frozenset(("frappe.desk.form.save.savedocs", "frappe.client.save", "frappe.client.insert",
	"frappe.client.insert_many", "frappe.client.set_value", "frappe.client.submit", "frappe.client.bulk_update"))
_REST_DOCUMENTS = ("/api/resource/", "/api/v2/document/")
_RPC = ("/api/method/", "/api/v2/method/")
_COMMENT = "ffl_price_approval_comment"
_EPS = 1e-9


class PriceApprovalRequired(frappe.ValidationError):
	"""A price was lowered and nothing approves it."""


def enabled() -> bool:
	"""FFL Settings → Require Manager Approval for Price Reductions (on until a store says otherwise)."""
	return single_flag("FFL Settings", SETTING, default=1)


def is_approver(user: str | None = None) -> bool:
	return bool(set(frappe.get_roles(user)) & set(PRICE_APPROVER_ROLES))


def mark_system_priced(doc):
	"""Our own builder priced *doc* on purpose (a $0 transfer gun, a gunsmith job's bill): this
	request's saves of it are not asked. A builder's save is not a client save anyway
	(:func:`_client_save`); this states it on the document. Later saves by a person are judged
	against what was saved."""
	doc.flags[SYSTEM_PRICED] = True
	return doc


def ensure_default() -> None:
	"""after_install: store the ON default (patches don't run on a fresh install, and a Single
	whose rows exist shows a never-saved Check unticked — a first Save would turn it off)."""
	from ffl_core.utils import single_raw

	if single_raw("FFL Settings", SETTING) in (None, ""):
		frappe.db.set_single_value("FFL Settings", SETTING, 1)


def extend_bootinfo(bootinfo) -> None:
	"""Whether this user's screens ask before a save: the switch is on and they are no manager."""
	if frappe.session.user == "Guest":
		return
	bootinfo.ffl_price_approval = 1 if enabled() and not is_approver() else 0


# ------------------------------------------------------------------ what asks


def _exempt(doc, before=None) -> bool:
	"""Documents never asked; see the module docstring. *before*: the document as stored."""
	from ffl_core.api.counter_pickup import _is_ticket

	flags = getattr(doc, "flags", None) or {}
	return bool(cint(doc.get("is_consolidated")) or flags.get(SYSTEM_PRICED)
		or frappe.flags.in_import or frappe.flags.in_patch or frappe.flags.in_migrate or frappe.flags.in_install
		or getattr(frappe.local, "job", None)
		or _stored_claim(doc, before)
		or (doc.get("doctype") == "POS Invoice" and _is_ticket(doc)))


def _stored_claim(doc, before) -> bool:
	"""A channel mirror or an extension's invoice (``is_fulfilled_elsewhere``) whose claiming field
	was already stored as it is now. Set or changed in this save it proves nothing: a form or REST
	save can write it. (Our syncs and builders set it on saves that are not judged at all.)"""
	from ffl_core.compliance.channel_invoice import (CHANNEL_INVOICE_FIELDS, extension_exclusion_fields,
		is_fulfilled_elsewhere)

	if before is None or not is_fulfilled_elsewhere(doc):
		return False
	claimed = [f for f in CHANNEL_INVOICE_FIELDS + extension_exclusion_fields() if doc.get(f)]
	return all(before.get(f) == doc.get(f) for f in claimed)


def _client_save(doc) -> bool:
	"""Whether this save of *doc* is one a person sent: the desk / register save, a frappe.client
	write or the REST document API for its doctype — or our endpoint opted in (:func:`attach`)."""
	from urllib.parse import unquote

	if (getattr(doc, "flags", None) or {}).get(CHECKED):
		return True
	request = getattr(frappe.local, "request", None)
	if request is None:
		return False
	path = getattr(request, "path", "") or ""
	for prefix in _REST_DOCUMENTS:
		if path.startswith(prefix):
			return (getattr(request, "method", "") or "").upper() in ("POST", "PUT", "PATCH") \
				and unquote(path[len(prefix):].split("/")[0]) == doc.doctype
	form = frappe.local.form_dict or {}
	cmd = form.get("cmd") or next((path[len(p):] for p in _RPC if path.startswith(p)), "")
	if cmd.strip("/") not in _CLIENT_SAVES:
		return False
	sent = _sent_doctypes(form)
	return not sent or doc.doctype in sent


def _sent_doctypes(form) -> set[str]:
	"""The doctypes a frappe.client / savedocs request carries (empty when it cannot tell: judged)."""
	out = set()
	for key in ("doc", "docs"):
		value = form.get(key)
		try:
			value = frappe.parse_json(value) if isinstance(value, str) else value
		except Exception:
			return set()
		for d in value if isinstance(value, list) else [value]:
			if isinstance(d, dict) and d.get("doctype"):
				out.add(d["doctype"])
	if form.get("doctype"):
		out.add(form.get("doctype"))
	return out


def _floor_key(item_code: str, serial: str | None) -> str:
	"""What an approved rate is bound to: the gun for a line carrying one serial (approving one gun's
	price never covers another gun of the same model Item), else the item."""
	return f"{item_code}|{serial}" if serial else item_code


def _distributor_flags(row) -> tuple:
	return (cint(row.get("delivered_by_supplier")), (row.get("ffl_to_store_distributor") or "").strip())


def _distributor_line(row, was=None) -> bool:
	"""A line a distributor fills (drop-ship, or ordered in to the store): its counter price comes from
	the distributor's quote, not from a list price here. Only when our code marked it so (Create Order,
	row.flags) or it was already saved so: the two fields are a form's to write.
	# lazy: such a line is not judged at all; upgrade path: a floor from the quote's suggested price
	# (distributor.api.quote)."""
	if (getattr(row, "flags", None) or {}).get(QUOTED):
		return True
	return any(_distributor_flags(row)) and was is not None and _distributor_flags(was) == _distributor_flags(row)


def mark_quoted(row):
	"""A distributor line our code priced from the distributor's quote (create_order)."""
	if getattr(row, "flags", None) is None:   # a plain dict row (tests): give it somewhere to keep it
		row.flags = frappe._dict()
	row.flags[QUOTED] = True
	return row


def list_rate(doc, row, cache: dict | None = None) -> tuple[float, bool]:
	"""The line's list price and whether it is the gun's own: a firearm line carrying one serial is
	priced from that serial (``firearm.serial_price_now`` — what the register's picker and Create
	Order charge), a firearm line without one from the cheapest priced gun of its model in stock,
	else (or when none is priced) the Item Price of the document's selling price list for the
	line's unit (0 when there is none: nothing to lower it from)."""
	from ffl_core.firearm import is_firearm_item, lowest_counter_price, serial_price_now

	cache = {} if cache is None else cache
	serials = split_serials(row.get("serial_no"))
	if is_firearm_item(row.item_code):
		if len(serials) == 1:
			price = serial_price_now(serials[0])
			if price:
				return price, True
		else:
			# No gun picked yet (a Sales Order line never carries one): the cheapest priced gun of
			# the model in stock — what the register seeds the line with.
			key = ("lowest", row.item_code)
			if key not in cache:
				cache[key] = lowest_counter_price(row.item_code)
			if cache[key]:
				return cache[key], False
	stock_uom = row.get("stock_uom") or frappe.get_cached_value("Item", row.item_code, "stock_uom")
	uom = row.get("uom") or stock_uom   # a row the screen has not filled in yet sells in stock units
	key = (row.item_code, uom, flt(row.get("conversion_factor")) or 1)
	if key not in cache:
		cache[key] = _item_price(doc, row, uom, stock_uom)
	return cache[key], False


def _item_price(doc, row, uom, stock_uom) -> float:
	from erpnext.stock.get_item_details import get_price_list_rate_for

	price_list = doc.get("selling_price_list")
	if not price_list:
		return 0.0
	ctx = frappe._dict(price_list=price_list, customer=doc.get("customer"), uom=uom, stock_uom=stock_uom,
		transaction_date=doc.get("posting_date") or doc.get("transaction_date") or nowdate(),
		qty=flt(row.get("qty")) or 1, conversion_factor=flt(row.get("conversion_factor")) or 1,
		batch_no=row.get("batch_no"))
	price = get_price_list_rate_for(ctx, row.item_code)
	if price is None:
		variant_of = frappe.db.get_value("Item", row.item_code, "variant_of")
		price = get_price_list_rate_for(ctx, variant_of) if variant_of else None
	return flt(price) * (flt(doc.get("plc_conversion_rate")) or 1) / (flt(doc.get("conversion_rate")) or 1)


def _rule_floor(doc, row, base: float) -> float | None:
	"""The lowest rate a Pricing Rule ERPNext applied to this line on this save gives it, or None.
	``doc.pricing_rules`` is rebuilt by ERPNext's own validate (set_missing_item_details), so a
	rule a client merely names counts for nothing.
	# lazy: each rule on its own; stacked rules (apply_multiple_pricing_rules) ask for approval."""
	names = {d.pricing_rule for d in doc.get("pricing_rules") or []
		if d.get("pricing_rule") and d.get("child_docname") == row.get("name") and d.get("item_code") == row.item_code}
	lows = []
	for name in names:
		r = frappe.get_cached_value("Pricing Rule", name,
			["rate_or_discount", "rate", "discount_percentage", "discount_amount"], as_dict=True)
		if not r:
			continue
		if r.rate_or_discount == "Rate":
			lows.append(flt(r.rate))
		elif r.rate_or_discount == "Discount Percentage":
			lows.append(base * (1 - flt(r.discount_percentage) / 100))
		elif r.rate_or_discount == "Discount Amount":
			lows.append(base - flt(r.discount_amount))
	return min(lows) if lows else None


def _rule_discount(doc) -> tuple[float, float]:
	"""(percentage, amount) the transaction Pricing Rules ERPNext applied on this save give the invoice."""
	pct = amount = 0.0
	for d in doc.get("pricing_rules") or []:
		if d.get("item_code") or d.get("child_docname") or not d.get("pricing_rule"):
			continue
		r = frappe.get_cached_value("Pricing Rule", d.pricing_rule, ["discount_percentage", "discount_amount"],
			as_dict=True) or {}
		pct, amount = max(pct, flt(r.get("discount_percentage"))), max(amount, flt(r.get("discount_amount")))
	return pct, amount


def _order_rates(doc) -> dict:
	"""{so_detail: rate} of the Sales Order lines an invoice's lines were made from."""
	names = [r.so_detail for r in doc.get("items") or [] if r.get("so_detail")]
	if not names or doc.get("doctype") != "Sales Invoice":
		return {}
	return {r.name: (r.item_code, flt(r.rate)) for r in frappe.get_all("Sales Order Item",
		filters={"name": ("in", names)}, fields=["name", "item_code", "rate"])}


def _same_line(was, row) -> bool:
	return was.item_code == row.item_code \
		and split_serials(was.get("serial_no")) == split_serials(row.get("serial_no")) \
		and (was.get("uom") or "") == (row.get("uom") or "")


def reductions(doc, before=None, *, against_list: bool = False) -> dict:
	"""What in *doc* a person lowered below its floor (module docstring), with *before* the
	document as saved (None for a new one). *against_list*: below the list price at all, whatever
	was saved or approved before (whether an approval record still means anything).

	Returns:
	    {"lines": [{"row", "idx", "item_code", "item_name", "serial", "list_rate", "rate"}],
	    "discount": {"percentage", "amount"} or None}.
	"""
	from ffl_core.compliance.locked_lines import rate_precision
	from ffl_core.setup.locked_lines import locked_items

	if cint(doc.get("is_return")):
		# Money or credit going back: the whole return asks (owner decision), not its prices.
		return {"lines": [], "discount": None, "refund": _returned(doc, None if against_list else before)}
	p = rate_precision(doc.get("doctype") or "Sales Invoice")
	locked = set(locked_items())
	saved = {r.name: r for r in ((before.get("items") or []) if before else []) if r.get("name")}
	ordered = {} if against_list else _order_rates(doc)
	flags = getattr(doc, "flags", None) or {}
	prior = {} if against_list else (flags.get(PRIOR_RATES) or {})
	cache: dict = {}
	lines = []
	for row in doc.get("items") or []:
		was = saved.get(row.get("name"))
		was = was if was is not None and _same_line(was, row) else None
		if not row.get("item_code") or row.item_code in locked or flt(row.get("qty")) <= 0 \
				or _distributor_line(row, was):
			continue
		rate = flt(row.get("rate"), p)
		listed, own_price = list_rate(doc, row, cache)
		listed = flt(listed, p)
		floor = listed
		rule = _rule_floor(doc, row, listed)
		if rule is not None:
			floor = min(floor, flt(rule, p))
		if was is not None and not against_list:
			floor = min(floor, flt(was.rate, p))
		# The order line was asked against the same Item Price; a gun's own price it never saw.
		source = ordered.get(row.get("so_detail"))
		if source and source[0] == row.item_code and not own_price:
			floor = min(floor, flt(source[1], p))
		serials = split_serials(row.get("serial_no"))
		for key in ((row.item_code, serials[0] if len(serials) == 1 else None), (row.item_code, None)):
			if key in prior:
				floor = min(floor, flt(prior[key], p))
				break
		if floor - rate > _EPS:
			lines.append({"row": row.get("name"), "idx": row.get("idx"), "item_code": row.item_code,
				"item_name": row.get("item_name") or row.item_code, "serial": serials[0] if len(serials) == 1 else None,
				"list_rate": listed, "rate": rate})
	return {"lines": lines, "discount": _discount_reduction(doc, None if against_list else before),
		"refund": None}


def _refund_total(doc) -> float:
	return abs(flt(doc.get("grand_total"), 2))


def _returned(doc, before) -> dict | None:
	"""A return that gives more back than it did as saved: {"total", "lines": [{"item_code",
	"item_name", "serial", "qty", "amount"}]}, or None."""
	total = _refund_total(doc)
	if total - (_refund_total(before) if before else 0.0) <= _EPS:
		return None
	return {"total": total, "lines": [{"item_code": r.item_code, "item_name": r.get("item_name") or r.item_code,
		"serial": (split_serials(r.get("serial_no")) or [None])[0], "qty": abs(flt(r.get("qty"))),
		"amount": abs(flt(r.get("amount"), 2))} for r in doc.get("items") or [] if r.get("item_code")]}


def _any(found: dict) -> bool:
	return bool(found["lines"] or found["discount"] or found.get("refund"))


def _discount_reduction(doc, before) -> dict | None:
	pct, amount = flt(doc.get("additional_discount_percentage")), flt(doc.get("discount_amount"), 2)
	if pct <= 0 and amount <= 0:
		return None
	was_pct = flt(before.get("additional_discount_percentage")) if before else 0.0
	was_amount = flt(before.get("discount_amount"), 2) if before else 0.0
	rule_pct, rule_amount = _rule_discount(doc)
	d = {"percentage": pct, "amount": amount}
	return None if _within(d, max(was_pct, rule_pct), max(was_amount, rule_amount)) else d


def _within(d: dict, pct: float, amount: float) -> bool:
	"""An invoice discount no bigger than *pct* / *amount*: a percentage is judged as one (ERPNext
	works the amount out of it, so a larger cart may carry a larger amount), an amount as an amount."""
	if flt(d["percentage"]) > 0:
		return flt(d["percentage"]) - flt(pct) <= _EPS
	return flt(d["amount"]) - flt(amount) <= _EPS


def _approved_floor(floors: dict, line: dict):
	"""The rate approved for *line*: its gun's, or its item's. A line with no gun yet (the Sales Order
	line of a gun the invoice carries) takes the lowest rate approved for any gun of its item."""
	key = _floor_key(line["item_code"], line["serial"])
	if key in floors or line["serial"]:
		return floors.get(key)
	guns = [v for k, v in floors.items() if k.startswith(line["item_code"] + "|")]
	return min(guns) if guns else None


def _uncovered(found: dict, approval) -> dict:
	"""What in *found* the approval does not reach (a manager signed in reaches everything)."""
	if approval.get("all"):
		return {"lines": [], "discount": None, "refund": None}
	floors = approval.get("lines") or {}
	lines = [l for l in found["lines"] if (f := _approved_floor(floors, l)) is None or flt(f) - l["rate"] > _EPS]
	d, ok = found["discount"], approval.get("discount")
	if d and ok and _within(d, ok.get("percentage"), ok.get("amount")):
		d = None
	r = found.get("refund")
	if r and approval.get("refund") is not None and r["total"] - flt(approval["refund"]) <= _EPS:
		r = None
	return {"lines": lines, "discount": d, "refund": r}


# ------------------------------------------------------------------ doc events


def guard_approval_fields(doc, before=None) -> None:
	"""The record fields keep what is stored: only :func:`enforce_price_approval` writes them, so a
	form or REST save can neither forge an approval nor wipe one. A new invoice made from approved
	Sales Order lines takes the order's record. Either is dropped once nothing on the document is
	below its list price any more: it would name an approval of a price no longer there."""
	source = before
	if source is None and doc.doctype == "Sales Invoice":
		orders = list(dict.fromkeys(r.sales_order for r in doc.get("items") or [] if r.get("sales_order")))
		source = next((row for row in (frappe.db.get_value("Sales Order", so, list(RECORD_FIELDS), as_dict=True)
			for so in orders) if row and row.get(BY)), None)
	if source is not None and source.get(BY):
		below = reductions(doc, against_list=True)
		if not _any(below):
			source = None
	for field in RECORD_FIELDS:
		doc.set(field, source.get(field) if source else None)


def enforce_price_approval(doc, method: str | None = None) -> None:
	"""POS Invoice / Sales Invoice / Sales Order validate: a lowered price saves only approved."""
	token = (doc.get(TOKEN_FIELD) or "").strip()
	doc.set(TOKEN_FIELD, None)   # carried in, never stored
	before = None if doc.is_new() else doc.get_doc_before_save()
	guard_approval_fields(doc, before)
	if not enabled() or not _client_save(doc) or _exempt(doc, before):
		return
	found = reductions(doc, before)
	if not _any(found):
		return
	approval = (take_token(token, doc) if token else None) or doc.flags.get(APPROVAL) or own_approval(doc)
	if not approval:
		frappe.throw(_describe(doc, found, _("Ask a manager to approve this return: {0}.") if found["refund"]
			else _("Ask a manager to approve this price: {0}.")),
			PriceApprovalRequired, title=_("Manager approval needed"))
	left = _uncovered(found, approval)
	if _any(left):
		frappe.throw(_describe(doc, left, _("More back than the manager approved — ask again: {0}.") if left["refund"]
			else _("Lower than the manager approved — ask again: {0}.")),
			PriceApprovalRequired, title=_("Manager approval needed"))
	doc.flags[APPROVAL] = approval
	doc.set(BY, approval["approver"])
	doc.set(REASON, approval["reason"])
	doc.set(ON, now_datetime())
	# The comment waits for on_update: a new document is not in the database yet.
	doc.flags[_COMMENT] = _comment_text(doc, approval, found)


def post_approval_comment(doc, method: str | None = None) -> None:
	"""on_update: the timeline comment of an approval this save recorded."""
	text = doc.flags.pop(_COMMENT, None)
	if text:
		doc.add_comment("Comment", text)


def own_approval(doc=None):
	"""A manager signed in approves their own price, or return."""
	if not is_approver():
		return None
	reason = _("Manager's own return") if doc is not None and cint(doc.get("is_return")) else _("Manager's own price")
	return frappe._dict(approver=frappe.session.user, reason=reason, all=True)


def attach(doc, approval=None, prior: dict | None = None):
	"""Our own code saving *doc* with prices a person typed (Create Order): judged as a client save,
	under *approval* (taken once for several documents, the order and its invoice) and with *prior*
	rates an edit carries over."""
	doc.flags[CHECKED] = True
	if approval:
		doc.flags[APPROVAL] = approval
	if prior:
		doc.flags[PRIOR_RATES] = prior
	return doc


def prior_rates(doc) -> dict:
	"""{(item_code, serial or None): rate} of an order being edited: its lines keep their prices."""
	out: dict = {}
	for row in doc.get("items") or []:
		serials = split_serials(row.get("serial_no"))
		if len(serials) == 1:
			out[(row.item_code, serials[0])] = flt(row.rate)
		key = (row.item_code, None)
		out[key] = min(out.get(key, flt(row.rate)), flt(row.rate))
	return out


def _money(doc, value) -> str:
	return fmt_money(value, currency=doc.get("currency"))


def _line_text(doc, line) -> str:
	what = escape_html(line["item_name"]) + (f" ({escape_html(line['serial'])})" if line.get("serial") else "")
	return f"{what} {_money(doc, line['list_rate'])} → {_money(doc, line['rate'])}"


def _discount_text(doc, d) -> str:
	pct = f"{flt(d['percentage'])}%" if flt(d["percentage"]) else _money(doc, d["amount"])
	return _("invoice discount {0}").format(pct)


def _describe(doc, found, template: str) -> str:
	parts = [_line_text(doc, l) for l in found["lines"]]
	if found["discount"]:
		parts.append(_discount_text(doc, found["discount"]))
	if found.get("refund"):
		r = found["refund"]
		parts.append(_("return of {0}, {1} back").format(", ".join(
			escape_html(l["item_name"]) + (f" ({escape_html(l['serial'])})" if l["serial"] else "")
			+ (f" ×{flt(l['qty']):g}" if flt(l["qty"]) != 1 else "") for l in r["lines"]), _money(doc, r["total"])))
	return template.format("; ".join(parts))


def _comment_text(doc, approval, found) -> str:
	who = frappe.utils.get_fullname(approval["approver"]) or approval["approver"]
	what = _("Return approved") if found.get("refund") else _("Price approved")
	return _describe(doc, found, (what + " " + _("by {0} ({1}): {{0}}.")).format(
		escape_html(who), escape_html(approval["reason"])))


# ------------------------------------------------------------------ token


def take_token(token: str, doc=None):
	"""The approval behind *token*, taken (single use) for *doc* — or for a Create Order when *doc*
	is None. Refused when expired, used, asked for by someone else, or given for another sale."""
	key = _TOKEN_KEY + (token or "")
	data = frappe.cache.get_value(key, expires=True)
	if not data or get_datetime(data["expires"]) <= now_datetime():
		frappe.throw(_("This price approval has expired or was used. Ask a manager again."),
			PriceApprovalRequired, title=_("Manager approval needed"))
	target = doc.doctype if doc is not None else ORDER
	if data["requested_by"] != frappe.session.user or data["target"] != target \
			or (doc is not None and data.get("docname") and data["docname"] != doc.name):
		frappe.throw(_("This price approval was given for another sale. Ask a manager again."),
			PriceApprovalRequired, title=_("Manager approval needed"))
	frappe.cache.delete_value(key)
	# A save that fails after this (another check, a lost lock) rolls back: the approval comes back.
	frappe.db.after_rollback.add(lambda: _keep(key, data))
	return frappe._dict(data)


def _keep(key: str, data: dict) -> None:
	left = int((get_datetime(data["expires"]) - now_datetime()).total_seconds())
	if left > 0:
		frappe.cache.set_value(key, data, expires_in_sec=left)


# ------------------------------------------------------------------ endpoints


def _target(doc=None, payload=None, sales_invoice: str | None = None):
	"""(document to judge, saved version or None, target name, docname) from a screen's request,
	permission checked. *doc*: a form's document (POS Invoice / Sales Invoice / Sales Order);
	*payload*: a Create Order payload (*sales_invoice*: the order it edits)."""
	if doc is not None:
		d = frappe.get_doc(frappe.parse_json(doc) if isinstance(doc, str) else doc)
		if d.doctype not in GATED:
			frappe.throw(_("Only a sale or an order is checked."))
		saved = None
		if not d.get("__islocal") and d.name and frappe.db.exists(d.doctype, d.name):
			frappe.has_permission(d.doctype, "write", doc=d.name, throw=True)
			saved = frappe.get_doc(d.doctype, d.name)
		else:
			frappe.has_permission(d.doctype, "create", throw=True)
		return d, saved, d.doctype, (saved.name if saved else None)
	from ffl_core.roles import COUNTER_ROLES

	frappe.only_for(COUNTER_ROLES)
	return _order_doc(payload, sales_invoice), None, ORDER, None


def _order_doc(payload, sales_invoice: str | None = None):
	"""A Create Order payload as the invoice create_order will build from it, enough to price: its
	customer's price list (as ERPNext picks it), and each line not filled by a distributor."""
	from erpnext.accounts.party import get_default_price_list

	from ffl_core.api.manual_order import _ORDERED_SOURCES, _coerce_payload

	payload = _coerce_payload(payload)
	customer = (str(payload.get("customer") or "")).strip()
	if not customer or not frappe.db.exists("Customer", customer):
		frappe.throw(_("Pick the buyer (Customer) for this order."))
	price_list = get_default_price_list(frappe.get_cached_doc("Customer", customer)) \
		or frappe.db.get_single_value("Selling Settings", "selling_price_list")
	doc = frappe._dict(doctype="Sales Invoice", customer=customer, selling_price_list=price_list,
		posting_date=nowdate(), currency=frappe.db.get_default("currency"), items=[], flags=frappe._dict())
	for i, line in enumerate(payload.get("items") or [], start=1):
		item_code = (str(line.get("item_code") or "")).strip()
		if not item_code or (str(line.get("source") or "")).strip().lower() in _ORDERED_SOURCES:
			continue
		uom = frappe.db.get_value("Item", item_code, "stock_uom")
		doc["items"].append(frappe._dict(name=f"line-{i}", idx=i, item_code=item_code,
			item_name=frappe.db.get_value("Item", item_code, "item_name") or item_code,
			qty=flt(line.get("qty")) or 1, rate=flt(line.get("rate")), uom=uom, stock_uom=uom, conversion_factor=1,
			serial_no="\n".join(s for s in (line.get("serials") or []) if s)))
	if sales_invoice:
		frappe.has_permission("Sales Invoice", "read", doc=sales_invoice, throw=True)
		doc.flags[PRIOR_RATES] = prior_rates(frappe.get_doc("Sales Invoice", sales_invoice))
	return doc


def _public(found: dict) -> dict:
	return {"lines": [{k: v for k, v in l.items() if k != "row"} for l in found["lines"]],
		"discount": found["discount"], "refund": found.get("refund")}


@frappe.whitelist()
def check(doc=None, payload=None, sales_invoice: str | None = None) -> dict:
	"""What a save would need a manager for — the screens ask before saving.

	Args:
	    doc: The form's document (POS Invoice, Sales Invoice or Sales Order) as JSON.
	    payload: Or a Create Order payload (create_order's shape).
	    sales_invoice: With *payload*: the order it edits (its lines keep their prices).

	Returns:
	    {"needs_approval": bool, "approver": whether the caller approves their own,
	    "lines": [{"idx", "item_code", "item_name", "serial", "list_rate", "rate"}],
	    "discount": {"percentage", "amount"} or None,
	    "refund": a return's {"total", "lines": [{"item_code", "item_name", "serial", "qty", "amount"}]} or None,
	    "approvers": [{"user", "full_name", "image", "has_pin", "pin_off"}] when it needs one}.
	"""
	frappe.only_for(PRICE_CHECK_ROLES)
	d, saved, _target_name, _docname = _target(doc, payload, sales_invoice)
	empty = {"needs_approval": False, "approver": is_approver(), "lines": [], "discount": None, "refund": None,
		"approvers": []}
	if not enabled() or (payload is None and _exempt(d, saved)):
		return empty
	found = _public(reductions(d, saved))
	needs = _any(found)
	return {**empty, **found, "needs_approval": needs,
		"approvers": approvers() if needs and not empty["approver"] else []}


def approvers() -> list[dict]:
	"""Who may approve: enabled desk users holding a manager role (never Administrator), by name."""
	from ffl_core.staff_switch import _pin_off, _users_with_pin

	holders = set(frappe.get_all("Has Role", filters={"role": ("in", PRICE_APPROVER_ROLES), "parenttype": "User"},
		pluck="parent")) - {"Guest", "Administrator"}
	if not holders:
		return []
	rows = frappe.get_all("User", filters={"name": ("in", list(holders)), "enabled": 1, "user_type": "System User"},
		fields=["name", "full_name", "user_image"], order_by="full_name asc")
	pins = _users_with_pin([r.name for r in rows])
	return [{"user": r.name, "full_name": r.full_name or r.name, "image": r.user_image, "has_pin": r.name in pins,
		"pin_off": r.name in pins and _pin_off(r.name)} for r in rows]


def _check_approver(approver: str, pin: str | None, password: str | None) -> None:
	"""*approver* is a manager here and typed their own PIN (or password): staff_switch's checks,
	with its lockouts — five wrong PINs turn the PIN off, ten wrong passwords pause for 10 minutes."""
	from ffl_core import staff_switch

	if approver not in {a["user"] for a in approvers()}:
		frappe.throw(_("Pick a manager from the list."), staff_switch.WrongPassword, title=_("Approval"))
	if pin and not password:
		staff_switch._check_pin(approver, pin)
	else:
		staff_switch._check_password(approver, password)


@frappe.whitelist(methods=["POST"])
def approve(approver: str, reason: str, pin: str | None = None, password: str | None = None, doc=None,
		payload=None, sales_invoice: str | None = None) -> dict:
	"""A manager approves the lowered prices of a sale, or a return, before it is saved.

	The approver picks themselves and types their own counter PIN, or their password when they
	have no PIN (or it is off). What is approved is worked out here from the same document, never
	taken from the screen. The token is good for 15 minutes, once, for the user who asked.

	Args:
	    approver: The manager's user id (from :func:`check`'s approvers).
	    reason: Why (required; kept on the document and its timeline).
	    pin: The approver's counter PIN.
	    password: Or their password.
	    doc: The form's document as JSON (as :func:`check`).
	    payload: Or a Create Order payload.
	    sales_invoice: With *payload*: the order it edits.

	Returns:
	    {"token", "approver", "approver_name", "expires_in": seconds, "lines", "discount", "refund"}.
	"""
	frappe.only_for(PRICE_CHECK_ROLES)
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Enter a reason for the lower price."), title=_("Reason required"))
	d, saved, target, docname = _target(doc, payload, sales_invoice)
	found = reductions(d, saved) if enabled() and (payload is not None or not _exempt(d, saved)) \
		else {"lines": [], "discount": None}
	if not _any(found):
		frappe.throw(_("Nothing on this sale needs a manager's approval."))
	_check_approver(approver, pin, password)
	floors: dict = {}
	for l in found["lines"]:
		key = _floor_key(l["item_code"], l["serial"])
		floors[key] = min(floors.get(key, l["rate"]), l["rate"])
	token = frappe.generate_hash(length=32)
	frappe.cache.set_value(_TOKEN_KEY + token, {
		"approver": approver, "reason": reason[:140], "requested_by": frappe.session.user, "target": target,
		"docname": docname, "lines": floors, "discount": found["discount"],
		"refund": found["refund"]["total"] if found.get("refund") else None,
		"expires": str(now_datetime() + timedelta(seconds=TOKEN_TTL)),
	}, expires_in_sec=TOKEN_TTL)
	return {"token": token, "approver": approver, "approver_name": frappe.utils.get_fullname(approver),
		"expires_in": TOKEN_TTL, **_public(found)}
