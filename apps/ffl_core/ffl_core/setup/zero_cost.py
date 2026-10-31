"""Zero-cost confirmation fields — telling "didn't fill it in" apart from "it was free".

`receive_goods` used to collapse both into one number::

    rate = 0.0 if is_custody else float(line.get("rate") or 0)

`or 0` turns a missing cost into $0, and once written the two facts are
indistinguishable forever. Eleven real purchases went in that way across three
months, and every later stock-cost audit had to ask the owner gun by gun which
ones were genuinely free (the Trailblazer Lifecards were; the rest were not).

These fields make "it was free" a thing a human states on purpose, and a thing
a later audit can QUERY instead of reading someone's comment:

  - Serial No: zero_cost_confirmed (Check), zero_cost_reason (Select),
    zero_cost_note (Small Text) — so a stock-cost sweep can skip a genuinely-free
    gun without a human in the loop (custody.audit_legacy_custody_candidates
    excludes them).
  - Purchase Receipt Item: the same trio, ffl_-prefixed to match every other
    field this app puts on that doctype (ffl_sell_price / ffl_dealer_price /
    ffl_service_need / ffl_consignment_commission_pct) — the audit source for
    lines that have no Serial No at all (accessories, ammo).

Created via create_custom_fields at after_migrate (module=None → NOT captured
by the ffl_core Custom Field fixture filter), the same route pay_on_sale.ensure
and consignment.ensure take. Lazy + never raises: a thrown after_migrate hook
aborts the whole `bench migrate`.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ffl_core.utils import commit

# The owner-approved list. "Other" is deliberately last and
# forces a written explanation (OTHER_REASON below + the note field) — it is the
# escape hatch for a case nobody foresaw, not a way to skip the question.
ZERO_COST_REASONS = (
    "Supplier gift",
    "Promotional item",
    "Warranty replacement",
    "Sample",
    "Other",
)

# The one reason that cannot stand alone. Named rather than spelled out at the
# call site so the list and the rule that references it can never drift apart.
OTHER_REASON = "Other"

_REASON_OPTIONS = "\n".join(("",) + ZERO_COST_REASONS)

_DESC = "The cost really was $0, so stock-cost audits skip it."
_NOTE_DESC = (
    "Required when the reason is \"Other\": what actually happened. Without it "
    "\"Other\" tells a later audit no more than an empty cost did."
)


def _fields(prefix: str, anchor: str, no_copy: int = 0) -> list:
    """The same three fields for both doctypes — they differ only in the field
    prefix, the anchor they sort after, and whether an amendment carries them.

    Read-only on BOTH, on purpose: these are system state written by the receive
    path (like pay_on_sale_settled), not a box staff tick on the form. Ticking
    "this $500 gun was free" straight on a Serial No would hide it from the
    stock-cost sweep with no receipt behind the claim and no audit line — the
    receive path writes them together with firearm.log_zero_cost_confirmation.
    Correcting a wrong reason is therefore not a form edit. On the RECEIPT side
    it is cancel + amend: no_copy=1 (below) means the amended receipt comes back
    WITHOUT the marking, so the operator cannot re-submit the mis-tick by
    accident, and submitting clears the trio off the Serial No and re-stamps the
    real cost.

    Two things the operator must still do by hand on that amended receipt, both
    because ERPNext owns those fields, not us:

      - restore the real cost. ``rate`` is editable but ``is_free_item`` is
        ERPNext-native ``read_only`` (no Property Setter in this repo lifts
        it), so the untick cannot be clicked on the form — **delete the row
        and re-add it** (item + serial + real rate) instead; ordinary child-
        table editing, and on submit the stamping block's ``elif row.rate > 0``
        re-stamps the real cost; and
      - **re-enter the serial number** — ERPNext marks ``serial_no`` and
        ``serial_and_batch_bundle`` ``no_copy`` as well, so an amended receipt
        arrives with no gun on the row. Without this step the amendment submits
        against nothing and the mis-marked gun is never re-stamped. (Measured,
        not assumed: test_receive_goods.ZeroCostStampIsClearable walks the whole
        path.)

    On the Serial No side there is no amendment, so those fields carry no
    no_copy and a direct correction is still a Stock-Manager db.set_value — the
    Serial No cost-correction entry points are where that gets a UI.
    """
    return [
        {
            "fieldname": f"{prefix}zero_cost_confirmed",
            "fieldtype": "Check",
            "label": "Zero Cost Confirmed",
            # Anchor on a CORE field (always present) rather than a sibling
            # custom field, so ordering never depends on which setup module
            # ran first.
            "insert_after": anchor,
            "read_only": 1,
            "no_copy": no_copy,
            "description": _DESC,
        },
        {
            "fieldname": f"{prefix}zero_cost_reason",
            "fieldtype": "Select",
            "label": "Zero Cost Reason",
            "options": _REASON_OPTIONS,
            "insert_after": f"{prefix}zero_cost_confirmed",
            "read_only": 1,
            "no_copy": no_copy,
            "depends_on": f"eval:doc.{prefix}zero_cost_confirmed",
        },
        {
            "fieldname": f"{prefix}zero_cost_note",
            "fieldtype": "Small Text",
            "label": "Zero Cost Explanation",
            "insert_after": f"{prefix}zero_cost_reason",
            "read_only": 1,
            "no_copy": no_copy,
            "depends_on": f"eval:doc.{prefix}zero_cost_reason=='{OTHER_REASON}'",
            "description": _NOTE_DESC,
        },
    ]


# Serial No takes the bare names (is_consignment / pay_on_sale / srbnb_cost are
# all bare there); Purchase Receipt Item takes the ffl_ prefix every other field
# this app adds to that doctype carries, plus no_copy so that amending a receipt
# hands back an UNMARKED row — that is what turns "I mis-ticked it" from a
# scripted field write into cancel + amend + submit. ERPNext's own is_free_item
# is left alone: it is not ours to re-flag, and it is editable on the form.
_FIELDS = {
    "Serial No": _fields("", "purchase_rate"),
    "Purchase Receipt Item": _fields("ffl_", "rate", no_copy=1),
}


def ensure() -> dict:
    """Create the zero-cost confirmation fields. Idempotent; never raises (runs
    in after_migrate — failures are logged, the next call retries)."""
    try:
        create_custom_fields(_FIELDS, ignore_validate=True)
        commit()
        return {"ok": 1}
    except Exception:
        frappe.log_error(title="zero_cost.ensure failed", message=frappe.get_traceback())
        return {"error": 1}
