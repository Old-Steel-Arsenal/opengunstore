"""Payroc transaction ledger — what the gateway holds for a date range, each row
matched to the POS, for the accountant.

The gateway is the master list: every sale, refund and decline on the merchant
account, counter (orderId = POS / Sales Invoice name) and web (orderId = Woo
order number) alike, including refunds and voids made in the Payroc portal that
the POS never hears about. Each row carries what the POS recorded for the same
order, and a flag where the two disagree. POS captures the gateway search did
not return are listed separately.

Read-only everywhere: GETs to the gateway, reads from the database. A flag is an
accounting question for a person, never a correction.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import flt, getdate
from frappe.utils.data import get_system_timezone

from ffl_integrations.integration_gate import payroc_ready
from ffl_integrations.notifications import safe_log_error
from ffl_integrations.payroc import reconcile
from ffl_integrations.payroc.client import PayrocClient

LEDGER_ROLES = ("System Manager", "Accounts Manager", "Accounts User")
# lazy: one gateway page per 10 txns plus a few DB reads per row; a month is a
# few hundred rows. Longer ranges are split by the caller.
MAX_DAYS = 31
# gunicorn kills a request at 120s (deploy/prod/Dockerfile); stop paging well
# before that and report truncated rather than lose the whole answer.
GATEWAY_TIME_BUDGET = 75
# _order_id suffixes attempt 2+ as "-A2", "-A3" … (payroc/api.py).
# lazy: a name trimmed to fit Payroc's 24-char orderId (long amended names plus
# a suffix) no longer names its document; such a row reads as "no POS record".
ATTEMPT_SUFFIX = re.compile(r"-A\d+$")
CENT = 0.005
# POS charge statuses under which a capture the gateway holds is expected.
POS_TOOK_MONEY = {"CHARGED", "PARTIAL", "REFUNDED"}
# Charges keyed in the Payroc Virtual Terminal get gateway orderIds VT0000000039…
# The POS books those as a plain card tender with the approval code, no reference.
VT_ORDER_PREFIX = "VT"


@frappe.whitelist()
def payroc_transactions(from_date: str, to_date: str) -> dict:
	"""Every Payroc gateway transaction from `from_date` to `to_date` (inclusive,
	store time zone, at most 31 days), matched to the POS. Read-only.

	Args:
		from_date: First day, YYYY-MM-DD.
		to_date: Last day, YYYY-MM-DD.

	Returns:
		{from_date, to_date, timezone, truncated, summary, transactions: [row],
		pos_only: [row]} — a transaction row is {date_time, type, status,
		batch_status, amount, reference, order_id, card, customer_name, terminal,
		source (counter | web | virtual terminal | ""), pos_doctype, pos_name, pos_amount,
		pos_refunded, flags: [str]}; pos_only rows are POS captures the search
		did not return, with the gateway's own status read back (skipped when
		truncated — the missing days would all land there).
	"""
	frappe.only_for(LEDGER_ROLES, message=True)
	start, end = getdate(from_date), getdate(to_date)
	if end < start:
		frappe.throw(_("to_date is before from_date."))
	if (end - start).days + 1 > MAX_DAYS:
		frappe.throw(_("At most {0} days per query — split the range.").format(MAX_DAYS))
	if not payroc_ready():
		frappe.throw(_("Payroc is not configured on this site."))

	tz_name = get_system_timezone()
	tz = ZoneInfo(tz_name)
	after = datetime.combine(start, time.min, tz)
	before = datetime.combine(end + timedelta(days=1), time.min, tz)
	client = PayrocClient.from_settings()
	gateway, truncated = client.search_transactions(
		after=after.isoformat(), before=before.isoformat(),
		time_budget=GATEWAY_TIME_BUDGET)

	rows = [_match(t) for t in gateway]
	_flag_duplicate_web_captures(rows)
	_flag_refunds(client, rows)
	pos_only = []
	if not truncated:
		seen = {r["reference"] for r in rows}
		# The POS stamps creation in naive system time, which is what `tz` is.
		pos_only = [_pos_only_row(client, c) for c in reconcile._captured_charges(
			after.replace(tzinfo=None), before.replace(tzinfo=None)) if c.ref not in seen]
	return {
		"from_date": str(start), "to_date": str(end), "timezone": tz_name,
		"truncated": truncated,
		"summary": _summary(rows, pos_only),
		"transactions": rows,
		"pos_only": pos_only,
	}


def _card(t: dict) -> str:
	last4 = str(t.get("maskedPan") or "")[-4:]
	return " ".join(x for x in (t.get("cardType") or "", f"*{last4}" if last4 else "") if x)


def _held(status) -> bool:
	return str(status or "").upper() in reconcile.HELD_STATUSES


def _pos_by_ref(ref: str) -> frappe._dict | None:
	"""The POS record of one gateway capture, by its uniqueReference."""
	if not ref:
		return None
	r = frappe.db.get_value("Card Charge", {"unique_reference": ref,
		"parenttype": "POS Invoice"}, ["parent as name", "amount", "status"], as_dict=True)
	doctype = "POS Invoice"
	if not r:
		r = frappe.db.get_value("POS Invoice", {"card_unique_reference": ref},
			["name", "card_charged_amount as amount", "card_status as status"], as_dict=True)
	if not r:
		r = frappe.db.get_value("Card Terminal Payment", {"unique_reference": ref},
			["sales_invoice as name", "amount", "status"], as_dict=True)
		doctype = "Sales Invoice"
	if not r:
		return None
	return frappe._dict(r, doctype=doctype,
		docstatus=frappe.db.get_value(doctype, r.name, "docstatus"))


def _pos_by_order(order_id: str) -> frappe._dict | None:
	"""The POS document an orderId names: a counter invoice (attempt suffix
	stripped) or a web order (Woo order number)."""
	order_id = str(order_id or "").strip()
	if not order_id:
		return None
	doc = ATTEMPT_SUFFIX.sub("", order_id)
	for doctype in ("POS Invoice", "Sales Invoice"):
		docstatus = frappe.db.get_value(doctype, doc, "docstatus")
		if docstatus is not None:
			return frappe._dict(doctype=doctype, name=doc, amount=None, docstatus=docstatus)
	woo = frappe.get_all("Woo Online Order", filters={"woo_order_id": order_id},
		fields=["name", "order_total as amount", "site"], limit=1)
	if woo:
		return frappe._dict(woo[0], doctype="Woo Online Order", docstatus=0)
	return None


def _pos_refunded(doctype: str, name: str) -> float:
	"""Card money the POS recorded as refunded against one document."""
	if doctype == "POS Invoice":
		rows = sum(flt(x) for x in frappe.get_all("Card Charge",
			filters={"parent": name, "parenttype": "POS Invoice"}, pluck="refunded_amount"))
		return max(rows, reconcile._pos_recorded_refund(name))
	if doctype == "Sales Invoice":
		return reconcile._cancel_order_refund(name)
	if doctype == "Woo Online Order":
		return sum(flt(x) for x in frappe.get_all("Woo Online Order Refund",
			filters={"parent": name, "parenttype": "Woo Online Order"},
			pluck="refund_amount"))
	return 0.0


def _match(t: dict) -> dict:
	ttype = str(t.get("type") or "").upper()
	ref = t.get("uniqueReference") or ""
	# The gateway signs refunds negative; rows carry the size, `type` the direction.
	amount = abs(flt(t.get("amount")))
	held = _held(t.get("status"))
	pos_ref = _pos_by_ref(ref) if ttype == "SALE" else None
	pos = pos_ref or _pos_by_order(t.get("orderId"))
	row = {
		"date_time": t.get("dateTime"), "type": ttype, "status": t.get("status"),
		"batch_status": t.get("batchStatus"), "amount": amount, "reference": ref,
		"order_id": t.get("orderId"), "card": _card(t),
		"customer_name": t.get("customerName"), "terminal": t.get("terminal"),
		"source": "", "pos_doctype": None, "pos_name": None, "pos_amount": None,
		"pos_refunded": None, "flags": [],
	}
	flags = row["flags"]
	if pos:
		row.update(source="web" if pos.doctype == "Woo Online Order" else "counter",
			pos_doctype=pos.doctype, pos_name=pos.name,
			pos_amount=None if pos.amount is None else flt(pos.amount))
	if ttype not in ("SALE", "REFUND"):
		if held:
			flags.append(f"gateway type {ttype or 'missing'} is not handled here — check by hand")
		return row
	if ttype == "REFUND":
		return row
	if not pos:
		if held and str(row["order_id"] or "").startswith(VT_ORDER_PREFIX):
			row["source"] = "virtual terminal"
			flags.append("virtual-terminal charge, not linked to a POS document — "
				"match it to the POS card payment by amount, date and approval code")
		elif held:
			flags.append("no POS record for this sale")
		return row
	if not held:
		if pos_ref and str(pos_ref.status or "").upper() in ("CHARGED", "PARTIAL") \
				and _pos_refunded(pos.doctype, pos.name) + CENT < amount:
			flags.append(f"POS shows charged, gateway {row['status']}")
		return row
	if pos.docstatus == 2:
		flags.append(f"POS {pos.doctype} {pos.name} is cancelled")
	if pos_ref:
		if abs(flt(pos_ref.amount) - amount) > CENT:
			flags.append(f"amount differs: gateway {amount:.2f}, POS {flt(pos_ref.amount):.2f}")
		status = str(pos_ref.status or "").upper()
		if status not in POS_TOOK_MONEY:
			flags.append(f"gateway holds this capture, POS status {status or 'blank'}")
	elif pos.doctype == "Woo Online Order":
		if abs(flt(pos.amount) - amount) > CENT:
			flags.append(f"amount differs: gateway {amount:.2f}, Woo order {flt(pos.amount):.2f}")
	else:
		flags.append("capture not recorded on the POS invoice (reference unknown to POS)")
	return row


def _flag_duplicate_web_captures(rows: list[dict]) -> None:
	"""A web order is paid once; two held captures on one order is a double
	charge (counter multi-card captures each carry their own POS reference)."""
	by_order: dict[str, list[dict]] = {}
	for r in rows:
		if r["type"] == "SALE" and _held(r["status"]) and r["pos_doctype"] == "Woo Online Order":
			by_order.setdefault(r["pos_name"], []).append(r)
	for captures in by_order.values():
		if len(captures) > 1:
			for r in captures:
				r["flags"].append(f"{len(captures)} captures for one web order")


def _gateway_refunds(client, order_ids: set[str], in_window: list[dict]) -> float:
	"""Every held gateway refund on these orderIds, ever — the POS side is
	all-time, so the gateway side must be too (an earlier recorded refund must
	not hide a later unrecorded one)."""
	refs = {r["reference"]: r["amount"] for r in in_window}
	for order_id in order_ids:
		try:
			found = (client.find_transaction(order_id=order_id) or {}).get("data") or []
		except Exception:
			safe_log_error(f"Payroc ledger: could not read refunds of {order_id}",
				frappe.get_traceback())
			continue
		for t in found:
			if str(t.get("type") or "").upper() == "REFUND" and _held(t.get("status")):
				refs[t.get("uniqueReference")] = abs(flt(t.get("amount")))
	return sum(refs.values())


def _flag_refunds(client, rows: list[dict]) -> None:
	"""Gateway refunds a document took vs what the POS recorded for it — the
	portal refund the POS never heard about."""
	by_doc: dict[tuple, list[dict]] = {}
	for r in rows:
		if r["type"] == "REFUND" and _held(r["status"]):
			by_doc.setdefault((r["pos_doctype"], r["pos_name"]), []).append(r)
	for (doctype, name), refunds in by_doc.items():
		if not name:
			for r in refunds:
				r["flags"].append("no POS record for this refund")
			continue
		gateway = _gateway_refunds(client, {r["order_id"] for r in refunds if r["order_id"]},
			refunds)
		recorded = _pos_refunded(doctype, name)
		for r in refunds:
			r["pos_refunded"] = recorded
			if gateway > recorded + CENT:
				r["flags"].append(f"refund not recorded in POS: gateway {gateway:.2f} "
					f"(all refunds on this order), POS {recorded:.2f}")


def _pos_only_row(client, charge) -> dict:
	"""A POS capture the date search did not return — read it back by reference
	(it may simply sit just outside the window)."""
	status = when = None
	try:
		tr = (client.retrieve_payment(charge.ref) or {}).get("transactionResult") or {}
		status, when = tr.get("status"), tr.get("dateTime")
		problem = reconcile._gateway_problem({"transactionResult": tr}, charge)
		flags = [f"POS shows charged, {problem}"] if problem else []
	except Exception as e:
		if " 404" in str(e):
			flags = ["not found at the gateway"]
		else:
			safe_log_error(f"Payroc ledger: could not read {charge.ref}", frappe.get_traceback())
			flags = ["gateway read failed — status unknown"]
	return {"reference": charge.ref, "pos_doctype": charge.doctype, "pos_name": charge.name,
		"pos_amount": flt(charge.amount), "pos_refunded": flt(charge.refunded_amount),
		"gateway_status": status, "gateway_date_time": when, "flags": flags}


def _summary(rows: list[dict], pos_only: list[dict]) -> dict:
	sales = [r for r in rows if r["type"] == "SALE" and _held(r["status"])]
	refunds = [r for r in rows if r["type"] == "REFUND" and _held(r["status"])]
	total = lambda xs: round(sum(r["amount"] for r in xs), 2)
	return {
		"transactions": len(rows),
		"sales": len(sales), "sales_total": total(sales),
		"refunds": len(refunds), "refunds_total": total(refunds),
		"net": round(total(sales) - total(refunds), 2),
		"counter_sales_total": total([r for r in sales if r["source"] == "counter"]),
		"web_sales_total": total([r for r in sales if r["source"] == "web"]),
		"virtual_terminal_sales_total": total([r for r in sales
			if r["source"] == "virtual terminal"]),
		"other_types": sum(1 for r in rows if r["type"] not in ("SALE", "REFUND")),
		"flagged": sum(1 for r in rows + pos_only if r["flags"]),
	}
