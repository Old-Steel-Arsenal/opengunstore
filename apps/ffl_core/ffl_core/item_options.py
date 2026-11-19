"""Items that come in options — made in one Save from the Item form, kept from the template.

ERPNext calls these a template Item (``has_variants``) with Item Attributes and variant Items
(``variant_of``). Staff never see those words: the Item form shows "Comes in options", the options
(an Item Attribute each, its values in the order the storefront lists them) and one row per
combination (a variant: its own SKU = item code, price, cost and pre-order slots).

* :func:`create_item_with_options` — the template, any missing Item Attributes / values and every
  included variant, in one transaction (all or nothing).
* :func:`item_options_state` / :func:`update_option_rows` — the saved template's panel: rows are the
  existing variants (price / cost / pre-order edited and saved row by row through an ordinary Item
  save, so the website and GunBroker doc events run), a new value adds rows created on Save, and an
  unticked row is disabled, never deleted.
* :func:`group_items` — existing Items made variants of a template, keeping their item codes. The
  checks (:func:`grouping_errors`) and the write (:func:`attach_variants`) are shared with
  ``ffl_woo_sync.woocommerce.variants.make_variants``.
* :func:`keep_variants_own` — Item validate: a template's save never copies its fields onto its
  variants (ERPNext's Item Variant Settings copy list may hold prices on some sites): shared fields
  are copied once, at creation; afterwards each variant keeps its own.

Two hooks tell the sales channels (``ffl_woo_sync`` answers both)::

    ffl_item_options_grouping(template: str, item_codes: list[str]) -> None   # before existing Items are grouped
    ffl_item_options_added(template: str, item_codes: list[str]) -> None      # after variants were added
"""
from __future__ import annotations

import copy
import json
import re

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, getdate

MAX_OPTIONS = 3
# lazy: 100 combinations per Save (one request, one transaction); upgrade path = a background job.
MAX_VARIANTS = 100
NAME_MAX = 140
MAX_SLOTS = 100000
_BAD_CHARS = re.compile(r"[<>{}\\%\x00-\x1f\x7f]")
_SKU = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]*$")
ABBR_LEN = 5

GROUPING_HOOK = "ffl_item_options_grouping"
ADDED_HOOK = "ffl_item_options_added"

# What a new variant takes from its template, once. Prices, cost, pre-order, UPC and every
# channel's product id are the variant's own (absent here on purpose).
SHARED_FIELDS = (
	"item_group", "stock_uom", "is_stock_item", "is_sales_item", "is_purchase_item", "has_serial_no",
	"uniform_sell_price", "is_firearm", "is_antique", "ffl_required_agreement", "is_used", "catalog_category",
	"brand", "manufacturer", "model_name", "caliber", "firearm_type", "importer", "barrel_length", "finish",
	"year_made", "condition_grade", "description", "image", "map_price", "msrp", "restricted_states",
	"restrict_to_companies", "valuation_method", "use_serial_no_wise_valuation", "enable_deferred_revenue",
	"no_of_months", "enable_deferred_expense", "no_of_months_exp", "grant_commission", "max_discount",
	"country_of_origin", "customs_tariff_number", "weight_per_unit", "weight_uom",
)
SHARED_TABLES = ("item_defaults", "taxes", "uoms", "allowed_companies", "image_gallery")

# The template is never sold itself: its own price, cost, stock and codes stay empty.
_TEMPLATE_CLEARED = {
	"standard_rate": 0, "dealer_price": 0, "valuation_rate": 0, "opening_stock": 0, "sale_price": 0,
	"sale_from": None, "sale_to": None, "upc": "", "barcodes": [], "actual_qty": 0,
	"ffl_preorder_available": 0, "ffl_preorder_eta": None,
}
_DROP_KEYS = {"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "doctype",
	"parent", "parenttype", "parentfield", "has_variants", "variant_of", "attributes"}


# --- trust-boundary parsing -------------------------------------------------------------------

def _json(value, what: str):
	if isinstance(value, str):
		try:
			return json.loads(value)
		except json.JSONDecodeError:
			frappe.throw(_("{0} is not valid.").format(what))
	return value


def _label(value, what: str) -> str:
	"""An option name or value: trimmed, single-spaced, 1–140 characters, no markup characters."""
	text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
	if not text:
		frappe.throw(_("{0} can't be blank.").format(what))
	if len(text) > NAME_MAX:
		frappe.throw(_("{0} is longer than {1} characters.").format(escape_html(text[:40]), NAME_MAX))
	if _BAD_CHARS.search(text):
		frappe.throw(_("{0} can't contain < > {{ }} \\ or %.").format(escape_html(text)))
	return text


def _number(value, what: str, *, integer: bool = False, upper: float | None = None) -> float:
	if value in (None, ""):
		return 0
	try:
		number = float(value)
	except (TypeError, ValueError):
		frappe.throw(_("{0} must be a number.").format(what))
	if number != number or number in (float("inf"), float("-inf")) or number < 0:
		frappe.throw(_("{0} can't be negative.").format(what))
	if upper is not None and number > upper:
		frappe.throw(_("{0} is too large.").format(what))
	if integer:
		if number != int(number):
			frappe.throw(_("{0} must be a whole number.").format(what))
		return int(number)
	return flt(number, 2)


def _date(value, what: str):
	if not value:
		return None
	try:
		return str(getdate(value))
	except Exception:
		frappe.throw(_("{0} is not a date.").format(what))


def _clean_options(options) -> list[tuple[str, list[str]]]:
	"""``[{name, values}]`` → ``[(name, [values])]``: 1–3 options, each named once and with at
	least one value, no value twice (ignoring case)."""
	options = _json(options, _("Options"))
	if not isinstance(options, list) or not options:
		frappe.throw(_("Add at least one option."))
	if len(options) > MAX_OPTIONS:
		frappe.throw(_("An item can have at most {0} options.").format(MAX_OPTIONS))
	out, names = [], set()
	for opt in options:
		if not isinstance(opt, dict):
			frappe.throw(_("Options are not valid."))
		name = _canonical_attribute(_label(opt.get("name"), _("Option name")))
		if name.casefold() in names:
			frappe.throw(_("Option {0} is listed twice.").format(escape_html(name)))
		names.add(name.casefold())
		raw = opt.get("values")
		if not isinstance(raw, list) or not raw:
			frappe.throw(_("Option {0} needs at least one value.").format(escape_html(name)))
		values, seen = [], set()
		for v in raw:
			value = _label(v, _("A value of {0}").format(name))
			if value.casefold() in seen:
				frappe.throw(_("{0} is listed twice under {1}.").format(escape_html(value), escape_html(name)))
			seen.add(value.casefold())
			values.append(value)
		out.append((name, values))
	return out


def _canonical_attribute(name: str) -> str:
	"""The existing Item Attribute's own spelling of *name* (names match ignoring case), else *name*."""
	return frappe.db.get_value("Item Attribute", name, "name") or name


# --- options (Item Attributes) ------------------------------------------------------------------

def abbreviation(value: str, taken) -> str:
	"""A short code for *value*, unique (ignoring case) among *taken*: a several-word value its
	initials (numbers kept whole: "17 Round" → "17R"), one word its first five letters, upper case;
	a clash gets 2, 3, … (the Item form asks :func:`option_codes`)."""
	words = re.findall(r"[A-Za-z0-9]+", value or "")
	if len(words) > 1:
		base = "".join(w if w.isdigit() else w[0] for w in words)
	elif words:
		base = words[0]
	else:
		base = "V"
	base = base.upper()[:ABBR_LEN]
	used = {str(t).upper() for t in taken or []}
	abbr, n = base, 2
	while abbr in used:
		suffix = str(n)
		abbr = base[:ABBR_LEN - len(suffix)] + suffix
		n += 1
	return abbr


def _ensure_attribute(name: str, values: list[str]) -> tuple[str, dict[str, str]]:
	"""The Item Attribute *name* with every one of *values* (created when missing, a new value
	with a generated abbreviation), *values* in the given order among its slots — the order the
	storefront lists them; values other items use keep their places. Returns the attribute's name
	and {given value: its stored spelling}."""
	row = frappe.db.get_value("Item Attribute", name, ["name", "numeric_values", "disabled"], as_dict=True)
	if row:
		if row.numeric_values:
			frappe.throw(_("Option {0} is a number range and can't be used here.").format(escape_html(row.name)))
		if row.disabled:
			frappe.throw(_("Option {0} is turned off.").format(escape_html(row.name)))
		doc = frappe.get_doc("Item Attribute", row.name)
	else:
		doc = frappe.new_doc("Item Attribute")
		doc.attribute_name = name
	by_fold = {r.attribute_value.casefold(): r for r in doc.item_attribute_values}
	changed = doc.is_new()
	stored = {}
	for value in values:
		r = by_fold.get(value.casefold())
		if r is None:
			r = doc.append("item_attribute_values", {"attribute_value": value,
				"abbr": abbreviation(value, [x.abbr for x in doc.item_attribute_values])})
			by_fold[value.casefold()] = r
			changed = True
		stored[value] = r.attribute_value
	ours = [by_fold[v.casefold()] for v in values]
	mine = {id(r) for r in ours}
	order = list(doc.item_attribute_values)
	slots = [i for i, r in enumerate(order) if id(r) in mine]
	for i, r in zip(slots, ours):
		order[i] = r
	if [id(r) for r in order] != [id(r) for r in doc.item_attribute_values]:
		doc.item_attribute_values = order
		changed = True
	for i, r in enumerate(doc.item_attribute_values, start=1):
		r.idx = i
	if changed:
		# A supporting master of the Item the caller may create (checked by the endpoints).
		doc.flags.ignore_permissions = True
		doc.save()
	return doc.name, stored


def _abbreviations(attribute: str) -> dict[str, str]:
	return {r.attribute_value: r.abbr for r in frappe.get_all("Item Attribute Value",
		filters={"parent": attribute, "parenttype": "Item Attribute"}, fields=["attribute_value", "abbr"])}


# --- rows (variants) ----------------------------------------------------------------------------

def _variant_name(template_name: str, values: list[str]) -> str:
	return f"{template_name} – {' / '.join(values)}"[:NAME_MAX]


def _combo_label(values: list[str]) -> str:
	return " / ".join(values)


def _clean_row_numbers(row: dict, label: str) -> dict:
	return {
		"price": _number(row.get("price"), _("{0}: price").format(label)),
		"cost": _number(row.get("cost"), _("{0}: cost").format(label)),
		"preorder": _number(row.get("preorder"), _("{0}: pre-order").format(label), integer=True, upper=MAX_SLOTS),
		"eta": _date(row.get("eta"), _("{0}: pre-order date").format(label)),
	}


def _clean_new_rows(rows, options: list[tuple[str, list[str]]], stored: dict[str, dict[str, str]],
		template_code: str, taken_combos: dict[tuple, str] | None = None) -> list[dict]:
	"""The rows to create: each a combination of the options' values (stored spelling), its SKU,
	price, cost, pre-order slots and date. Unticked rows are dropped. Every problem of every row is
	collected and thrown at once."""
	errors: list[str] = []
	out: list[dict] = []
	combos = dict(taken_combos or {})
	skus: dict[str, str] = {}
	for raw in rows:
		if not isinstance(raw, dict):
			frappe.throw(_("Rows are not valid."))
		given = raw.get("values") or {}
		if not isinstance(given, dict):
			frappe.throw(_("Rows are not valid."))
		folded = {str(k).strip().casefold(): v for k, v in given.items()}
		values = []
		for name, allowed in options:
			v = re.sub(r"\s+", " ", str(folded.get(name.casefold()) or "")).strip()
			match = next((a for a in allowed if a.casefold() == v.casefold()), None)
			if match is None:
				frappe.throw(_("A row has {0} for {1}, which is not one of its values.").format(
					escape_html(v or _("nothing")), escape_html(name)))
			values.append(stored[name][match])
		label = escape_html(_combo_label(values))
		if not cint(raw.get("include", 1)):
			continue
		key = tuple(v.casefold() for v in values)
		if key in combos:
			errors.append(_("{0} is already {1}.").format(label, escape_html(combos[key])) if combos[key]
				else _("{0} is listed twice.").format(label))
			continue
		combos[key] = ""
		nums = _clean_row_numbers(raw, label)
		sku = str(raw.get("sku") or "").strip()
		if not sku:
			errors.append(_("{0}: enter a SKU.").format(label))
		elif len(sku) > NAME_MAX:
			errors.append(_("{0}: the SKU is longer than {1} characters.").format(label, NAME_MAX))
		elif not _SKU.match(sku):
			errors.append(_("{0}: SKU {1} may only use letters, numbers and . _ / -").format(label, escape_html(sku)))
		elif sku.casefold() in skus:
			errors.append(_("SKU {0} is used by {1} and {2}.").format(escape_html(sku), escape_html(skus[sku.casefold()]), label))
		elif sku.casefold() == (template_code or "").casefold() or frappe.db.exists("Item", sku):
			errors.append(_("{0}: SKU {1} is already an item.").format(label, escape_html(sku)))
		skus.setdefault(sku.casefold(), label)
		if nums["price"] <= 0:
			errors.append(_("{0}: enter a price.").format(label))
		out.append({"values": values, "sku": sku, **nums})
	if len(out) > MAX_VARIANTS:
		errors.append(_("At most {0} combinations can be saved at once.").format(MAX_VARIANTS))
	if errors:
		frappe.throw("<br>".join(errors), title=_("Check the options"))
	return out


def _has_field(fieldname: str) -> bool:
	return frappe.get_meta("Item").has_field(fieldname)


def _insert_variant(template, names: list[str], row: dict) -> str:
	"""One variant of *template*: the shared fields copied, its own SKU, price, cost and pre-order."""
	variant = frappe.new_doc("Item")
	for field in SHARED_FIELDS:
		if _has_field(field) and template.get(field) not in (None, ""):
			variant.set(field, template.get(field))
	for table in SHARED_TABLES:
		if _has_field(table):
			rows = []
			for d in template.get(table) or []:
				r = copy.deepcopy(d.as_dict())
				for key in ("name", "parent", "parenttype", "parentfield", "creation", "modified", "owner", "modified_by"):
					r.pop(key, None)
				rows.append(r)
			variant.set(table, rows)
	variant.update({
		"item_code": row["sku"],
		"item_name": _variant_name(template.item_name or template.name, row["values"]),
		"variant_of": template.name,
		"variant_based_on": "Item Attribute",
		"has_variants": 0,
		"standard_rate": row["price"],
		"dealer_price": row["cost"],
	})
	variant.set("attributes", [{"attribute": n, "attribute_value": v} for n, v in zip(names, row["values"])])
	if _has_field("ffl_preorder_available"):
		variant.set("ffl_preorder_available", row["preorder"])
		variant.set("ffl_preorder_eta", row["eta"] if row["preorder"] else None)
	variant.insert()
	return variant.name


def _fire(hook: str, template: str, item_codes: list[str]) -> None:
	"""Tell the sales channels. A channel that fails is logged: its own sweep catches up."""
	for path in frappe.get_hooks(hook):
		try:
			frappe.get_attr(path)(template, list(item_codes))
		except Exception:  # noqa: BLE001
			frappe.log_error(title=f"{hook} failed: {path} ({template})", message=frappe.get_traceback())


def _clean_doc_dict(data: dict) -> dict:
	out = {}
	for key, value in (data or {}).items():
		if key.startswith("__") or key in _DROP_KEYS:
			continue
		if isinstance(value, list):
			value = [{k: v for k, v in row.items() if not k.startswith("__") and k not in _DROP_KEYS}
				for row in value if isinstance(row, dict)]
		out[key] = value
	return out


def _per_gun(doc) -> bool:
	return bool(doc.get("has_serial_no") and not doc.get("uniform_sell_price"))


def _quiet_price_alerts() -> None:
	"""Drop the passing alerts the saves queued (ERPNext's "Item Price created" per variant): the
	form shows one line for the whole Save. Messages that are not alerts stay."""
	log = getattr(frappe.local, "message_log", None) or []
	keep = []
	for m in log:
		data = m if isinstance(m, dict) else frappe.parse_json(m) if isinstance(m, str) and m.startswith("{") else None
		if not (isinstance(data, dict) and data.get("alert")):
			keep.append(m)
	frappe.local.message_log = keep


def _savepoint_call(name: str, fn):
	"""Run *fn* all or nothing. A deadlock / snapshot conflict (MariaDB 1020) has already rolled
	the whole transaction back, savepoint included: then the original error is what the caller sees."""
	frappe.db.savepoint(name)
	try:
		return fn()
	except Exception:
		try:
			frappe.db.rollback(save_point=name)
		except Exception:  # noqa: BLE001 — the savepoint went with the transaction
			frappe.db.rollback()
		raise


# --- endpoints ----------------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def create_item_with_options(item, options, rows) -> dict:
	"""Create an item that comes in options: the template Item, any missing options (Item
	Attributes) and values, and one variant Item per included combination — all or nothing.

	Each variant copies the template's shared fields once (group, brand, description, photos,
	tax and accounting defaults, flags) and has its own item code (its SKU), price (with the
	register's Item Price), cost and pre-order slots. The template keeps no price, cost, UPC or
	stock of its own.

	Args:
	    item: The Item form's document (dict or JSON), as the form would save it. ``item_code``
	        is required.
	    options: ``[{name, values: [value, ...]}]`` in order (1–3 options); values in the order
	        the storefront lists them. Names and values are matched to existing ones ignoring case.
	    rows: ``[{values: {option: value}, include, sku, price, cost, preorder, eta}]`` — one per
	        combination; unticked rows (``include`` 0) are skipped. An included row needs a SKU
	        (letters, numbers and ``. _ / -``, not an existing item code) and a price above 0.

	Returns:
	    {"template": the template's item code, "variants": [the variants' item codes]}.

	Raises:
	    ValidationError: Any row or option problem (all listed at once); nothing is written.
	"""
	frappe.has_permission("Item", "create", throw=True)
	item = _json(item, _("The item"))
	if not isinstance(item, dict):
		frappe.throw(_("The item is not valid."))
	opts = _clean_options(options)
	rows = _json(rows, _("Rows"))
	if not isinstance(rows, list):
		frappe.throw(_("Rows are not valid."))
	code = str(item.get("item_code") or "").strip()
	if not code:
		frappe.throw(_("Enter the item code first."))
	if frappe.db.exists("Item", code):
		frappe.throw(_("Item {0} already exists.").format(escape_html(code)))
	draft = _clean_new_rows(rows, opts, {n: {v: v for v in vals} for n, vals in opts}, code)
	if not draft:
		frappe.throw(_("Tick at least one combination."))

	def run():
		stored, names = {}, []
		for name, values in opts:
			attribute, spelled = _ensure_attribute(name, values)
			names.append(attribute)
			stored[name] = spelled
		for row in draft:
			row["values"] = [stored[n][v] for (n, _vals), v in zip(opts, row["values"])]
		data = _clean_doc_dict(item)
		data.update(_TEMPLATE_CLEARED)
		data = {k: v for k, v in data.items() if k in ("barcodes",) or _has_field(k) or k == "item_code"}
		data.update({"doctype": "Item", "item_code": code, "has_variants": 1, "variant_based_on": "Item Attribute",
			"attributes": [{"attribute": n} for n in names]})
		template = frappe.get_doc(data)
		template.insert()
		if _per_gun(template):
			frappe.throw(_("Each unit of this item has its own price, so it can't come in options. "
				"Turn on Uniform Pricing first."))
		made = [_insert_variant(template, names, row) for row in draft]
		return template.name, made

	template, made = _savepoint_call("ffl_item_options_create", run)
	_quiet_price_alerts()
	_fire(ADDED_HOOK, template, made)
	return {"template": template, "variants": made}


def _template(template: str, ptype: str = "read"):
	if not template or not frappe.db.exists("Item", template):
		frappe.throw(_("Item {0} not found.").format(escape_html(template or "")))
	doc = frappe.get_doc("Item", template)
	if not doc.has_variants:
		frappe.throw(_("Item {0} does not come in options.").format(escape_html(template)))
	doc.check_permission(ptype)
	return doc


def template_options(template: str) -> list[str]:
	"""The template's options (Item Attribute names) in order. Throws when *template* is not an
	Item that comes in options or has none."""
	if not frappe.db.exists("Item", template):
		frappe.throw(_("Item {0} does not exist.").format(template))
	if not frappe.db.get_value("Item", template, "has_variants"):
		frappe.throw(_("Item {0} is not a template (Has Variants is off).").format(template))
	names = frappe.get_all("Item Variant Attribute",
		filters={"parenttype": "Item", "parent": template}, order_by="idx asc", pluck="attribute")
	if not names:
		frappe.throw(_("Template {0} has no variant attributes.").format(template))
	return names


def _variant_values(codes: list[str]) -> dict[str, dict[str, str]]:
	out: dict[str, dict[str, str]] = {c: {} for c in codes}
	if codes:
		for r in frappe.get_all("Item Variant Attribute", filters={"parenttype": "Item", "parent": ["in", codes]},
				fields=["parent", "attribute", "attribute_value"]):
			out[r.parent][r.attribute] = r.attribute_value
	return out


def _all_variants(template: str) -> list[str]:
	return frappe.get_all("Item", filters={"variant_of": template}, order_by="creation asc", pluck="name")


@frappe.whitelist()
def item_options_state(template: str) -> dict:
	"""A saved item's options and one row per variant, for the Item form's panel.

	Args:
	    template: The Item that comes in options.

	Returns:
	    {"template", "item_name", "preorder": whether pre-order fields exist,
	    "options": [{name, values: [in storefront order], abbr: {value: abbreviation}}],
	    "rows": [{item_code, item_name, values: {option: value}, disabled, price, cost, preorder, eta}]}.
	"""
	frappe.has_permission("Item", "read", throw=True)
	doc = _template(template)
	names = [r.attribute for r in doc.attributes]
	codes = _all_variants(doc.name)
	values = _variant_values(codes)
	preorder = _has_field("ffl_preorder_available")
	fields = ["name", "item_name", "disabled", "standard_rate", "dealer_price"] + (
		["ffl_preorder_available", "ffl_preorder_eta"] if preorder else [])
	items = {r.name: r for r in frappe.get_all("Item", filters={"name": ["in", codes or [""]]}, fields=fields)}
	options = []
	for name in names:
		used = {values[c].get(name) for c in codes} - {None, ""}
		order = frappe.get_all("Item Attribute Value", filters={"parent": name, "parenttype": "Item Attribute"},
			order_by="idx asc", pluck="attribute_value")
		options.append({"name": name, "values": [v for v in order if v in used] + sorted(used - set(order)),
			"abbr": _abbreviations(name)})
	rows = []
	for code in codes:
		r = items[code]
		rows.append({"item_code": code, "item_name": r.item_name, "values": values[code], "disabled": cint(r.disabled),
			"price": flt(r.standard_rate), "cost": flt(r.dealer_price),
			"preorder": cint(r.get("ffl_preorder_available")), "eta": str(r.ffl_preorder_eta) if r.get("ffl_preorder_eta") else None})
	return {"template": doc.name, "item_name": doc.item_name, "preorder": preorder, "options": options, "rows": rows}


def _differs(old, new) -> bool:
	if isinstance(new, (int, float)):
		return flt(old) != flt(new)
	return str(old or "") != str(new or "")


def _update_variant(code: str, template: str, raw: dict) -> bool:
	"""Apply a panel row to an existing variant through an ordinary Item save. True when it changed."""
	if frappe.db.get_value("Item", code, "variant_of") != template:
		frappe.throw(_("{0} is not one of this item's options.").format(escape_html(code)))
	doc = frappe.get_doc("Item", code)
	doc.check_permission("write")
	label = escape_html(doc.item_name or code)
	changes = {}
	if "include" in raw:
		changes["disabled"] = 0 if cint(raw.get("include")) else 1
	if "price" in raw:
		changes["standard_rate"] = _number(raw.get("price"), _("{0}: price").format(label))
	if "cost" in raw:
		changes["dealer_price"] = _number(raw.get("cost"), _("{0}: cost").format(label))
	if _has_field("ffl_preorder_available"):
		if "preorder" in raw:
			changes["ffl_preorder_available"] = _number(raw.get("preorder"), _("{0}: pre-order").format(label),
				integer=True, upper=MAX_SLOTS)
		if "eta" in raw:
			changes["ffl_preorder_eta"] = _date(raw.get("eta"), _("{0}: pre-order date").format(label))
	enabled = not changes.get("disabled", doc.disabled)
	if enabled and flt(changes.get("standard_rate", doc.standard_rate)) <= 0:
		frappe.throw(_("{0}: enter a price.").format(label))
	if enabled and doc.disabled:
		# Grouping checks only the rows on sale, so a row switched off may share its values.
		mine = {(a.attribute, a.attribute_value) for a in doc.attributes}
		others = frappe.get_all("Item", filters={"variant_of": template, "disabled": 0, "name": ["!=", code]}, pluck="name")
		for other, values in _variant_values(others).items():
			if mine and set(values.items()) == mine:
				frappe.throw(_("{0} has the same options as {1}; switch that one off first.").format(
					label, escape_html(other)))
	changed = {k: v for k, v in changes.items() if _differs(doc.get(k), v)}
	if not changed:
		return False
	doc.update(changed)
	doc.save()
	if "standard_rate" in changed:
		from ffl_core.pricing import _sync_selling_item_price

		_sync_selling_item_price(code, flt(changed["standard_rate"]))
	return True


@frappe.whitelist(methods=["POST"])
def update_option_rows(template: str, rows, options=None) -> dict:
	"""Save the panel of an item that comes in options: edits to existing rows, and new rows for
	values added to its options — one transaction.

	An existing row is saved through an ordinary Item save (so the website and GunBroker pushes
	run); a new price also becomes the register's price. Unticking a row disables that variant —
	never deletes it — and ticking it again enables it.

	Args:
	    template: The Item that comes in options.
	    rows: ``[{item_code, include?, price?, cost?, preorder?, eta?}]`` for existing rows (only the
	        keys sent change), and ``[{values: {option: value}, include, sku, price, cost, preorder, eta}]``
	        for new combinations (created when included; same checks as
	        :func:`create_item_with_options`).
	    options: ``[{name, values}]`` — the item's options in order with every value it shows, new ones
	        included, in the order the storefront lists them (needed when new rows are sent; the option
	        names can't change). Unused new values are kept on the option.

	Returns:
	    :func:`item_options_state` after the save, plus ``"created"``: the new variants' item codes.
	"""
	frappe.has_permission("Item", "write", throw=True)
	doc = _template(template, "write")
	rows = _json(rows, _("Rows"))
	if not isinstance(rows, list):
		frappe.throw(_("Rows are not valid."))
	existing = [r for r in rows if isinstance(r, dict) and r.get("item_code")]
	new = [r for r in rows if not (isinstance(r, dict) and r.get("item_code"))]
	names = [r.attribute for r in doc.attributes]
	draft, opts = [], []
	if _json(options, _("Options")) or new:
		opts = _clean_options(options)
		if [n.casefold() for n, _v in opts] != [n.casefold() for n in names]:
			frappe.throw(_("The options of an existing item can't be renamed, added or removed."))
		opts = [(n, vals) for n, (_x, vals) in zip(names, opts)]
	if new:
		frappe.has_permission("Item", "create", throw=True)
		values = _variant_values(_all_variants(doc.name))
		taken = {tuple((v.get(n) or "").casefold() for n in names): code for code, v in values.items()}
		draft = _clean_new_rows(new, opts, {n: {v: v for v in vals} for n, vals in opts}, doc.name, taken)

	def run():
		for raw in existing:
			_update_variant(str(raw["item_code"]), doc.name, raw)
		made = []
		if draft:
			stored = {}
			for name, values in opts:
				_attr, stored[name] = _ensure_attribute(name, values)
			for row in draft:
				row["values"] = [stored[n][v] for (n, _vals), v in zip(opts, row["values"])]
				made.append(_insert_variant(doc, names, row))
		elif opts:
			for name, values in opts:
				_ensure_attribute(name, values)
		return made

	made = _savepoint_call("ffl_item_options_update", run)
	_quiet_price_alerts()
	if made:
		_fire(ADDED_HOOK, doc.name, made)
	return {**item_options_state(doc.name), "created": made}


@frappe.whitelist()
def option_suggestions(option: str | None = None, txt: str = "", limit=20) -> list[dict]:
	"""Existing option names, or one option's values, for the Item form's typeahead.

	Args:
	    option: An option (Item Attribute) name: its values are returned (in storefront order).
	        Blank: option names.
	    txt: What was typed (matched anywhere, ignoring case).
	    limit: At most this many (default 20, up to 500).

	Returns:
	    [{"value", "abbr"?}] — option names without abbreviations.
	"""
	frappe.has_permission("Item", "read", throw=True)
	txt = str(txt or "").strip()[:NAME_MAX]
	like = f"%{txt}%"
	limit = min(max(cint(limit), 1), 500)
	if not option:
		return [{"value": n} for n in frappe.get_all("Item Attribute",
			filters={"numeric_values": 0, "disabled": 0, "attribute_name": ["like", like]},
			order_by="attribute_name asc", pluck="name", limit=limit)]
	name = frappe.db.get_value("Item Attribute", str(option).strip()[:NAME_MAX], "name")
	if not name:
		return []
	return [{"value": r.attribute_value, "abbr": r.abbr} for r in frappe.get_all("Item Attribute Value",
		filters={"parent": name, "parenttype": "Item Attribute", "attribute_value": ["like", like]},
		fields=["attribute_value", "abbr"], order_by="idx asc", limit=limit)]


@frappe.whitelist()
def option_codes(option: str, values) -> dict:
	"""The short code each value of an option has, or will get when saved — what the Item form
	builds default SKUs from.

	Args:
	    option: The option (Item Attribute) name; it need not exist yet.
	    values: The option's values in order (list or JSON).

	Returns:
	    {value: code} — an existing value's own code, a new value the code saving gives it.
	"""
	frappe.has_permission("Item", "read", throw=True)
	values = _json(values, _("Values"))
	if not isinstance(values, list):
		frappe.throw(_("Values are not valid."))
	name = frappe.db.get_value("Item Attribute", str(option or "").strip()[:NAME_MAX], "name")
	have = {} if not name else {v.casefold(): a for v, a in _abbreviations(name).items()}
	taken = list(have.values())
	out = {}
	for raw in values[:200]:
		value = re.sub(r"\s+", " ", str(raw or "")).strip()[:NAME_MAX]
		if not value:
			continue
		code = have.get(value.casefold())
		if code is None:
			code = abbreviation(value, taken)
			have[value.casefold()] = code
			taken.append(code)
		out[value] = code
	return out


@frappe.whitelist()
def taken_item_codes(codes) -> list[str]:
	"""Which of *codes* are already item codes (ignoring case) — the Item form's SKU check.

	Args:
	    codes: Item codes (list or JSON), up to 500.

	Returns:
	    The ones that exist, as given.
	"""
	frappe.has_permission("Item", "read", throw=True)
	codes = _json(codes, _("Codes"))
	if not isinstance(codes, list):
		frappe.throw(_("Codes are not valid."))
	codes = [str(c).strip() for c in codes[:500] if str(c or "").strip()]
	if not codes:
		return []
	found = {n.casefold() for n in frappe.get_all("Item", filters={"name": ["in", codes]}, pluck="name")}
	return [c for c in codes if c.casefold() in found]


# --- grouping existing Items (shared with ffl_woo_sync.woocommerce.variants.make_variants) -------

def _check_variant(code: str, values, template: str, names: list[str]) -> list[str]:
	"""Why *code* cannot become a variant of *template* with *values* (empty: it can)."""
	row = frappe.db.get_value("Item", code, ["has_variants", "variant_of"], as_dict=True)
	if not row:
		return [f"{code}: no such Item"]
	errors = []
	if code == template:
		errors.append(f"{code}: is the template itself")
	if row.has_variants:
		errors.append(f"{code}: is a template itself")
	if row.variant_of and row.variant_of != template:
		errors.append(f"{code}: already a variant of {row.variant_of}")
	if not isinstance(values, dict) or set(values) != set(names):
		return errors + [f"{code}: needs exactly these attributes: {', '.join(names)}"]
	for name in names:
		value = str(values[name] or "").strip()
		numeric = frappe.db.get_value("Item Attribute", name, "numeric_values")
		if not value:
			errors.append(f"{code}: {name} is blank")
		elif not numeric and not frappe.db.exists("Item Attribute Value",
				{"parent": name, "attribute_value": value}):
			errors.append(f"{code}: {value!r} is not a value of Item Attribute {name}")
	return errors


def grouping_errors(template: str, variants: dict, names: list[str]) -> list[str]:
	"""Why *variants* ({item_code: {attribute: value}}) cannot all become variants of *template*:
	each Item's own checks, and no two with the same values (nor one with the values of an
	existing variant). Empty when they can."""
	def combo(values) -> tuple:
		return tuple(str((values or {}).get(n) or "").strip() for n in names)

	errors: list[str] = []
	# Existing variants only as something a requested Item may not repeat — their
	# own gaps or clashes are not this call's to judge.
	seen: dict[tuple, str] = {}
	for code, values in _variant_values([c for c in frappe.get_all("Item",
			filters={"variant_of": template, "disabled": 0}, order_by="creation asc", pluck="name")
			if c not in variants]).items():
		key = combo(values)
		if all(key):
			seen.setdefault(key, code)
	for code, values in variants.items():
		errors += _check_variant(code, values, template, names)
		key = combo(values)
		if key in seen:
			errors.append(f"{code}: same attributes as {seen[key]}")
		seen.setdefault(key, code)
	return errors


def attach_variants(template: str, variants: dict, names: list[str]) -> None:
	"""Make each Item of *variants* a variant of *template* with its values, keeping its item
	code (ERPNext's ``variant_of`` is set-once on a saved Item, hence the direct writes). Checked
	first by :func:`grouping_errors`."""
	for code, values in variants.items():
		frappe.db.delete("Item Variant Attribute", {"parenttype": "Item", "parent": code})
		for idx, name in enumerate(names, start=1):
			frappe.get_doc({
				"doctype": "Item Variant Attribute", "parenttype": "Item", "parentfield": "attributes",
				"parent": code, "idx": idx, "attribute": name,
				"attribute_value": str(values[name]).strip(), "variant_of": template,
			}).db_insert()
		frappe.db.set_value("Item", code, {"variant_of": template, "has_variants": 0})
		frappe.get_doc("Item", code).add_comment("Comment",
			_("Made a variant of {0} ({1}).").format(template,
				", ".join(f"{n}: {values[n]}" for n in names)))


@frappe.whitelist(methods=["POST"])
def group_items(template: str, variants) -> dict:
	"""Group existing Items under an item that comes in options, each with its option values,
	keeping their item codes. Nothing is written when any Item fails a check.

	Args:
	    template: The Item that comes in options.
	    variants: ``{item_code: {option: value}}`` (dict or JSON); every option needs one of its
	        existing values, and no two Items (nor an Item and an existing row) the same values.

	Returns:
	    {"ok", "errors": [one line per problem], "variants": [the item codes grouped]}.
	"""
	frappe.has_permission("Item", "write", throw=True)
	doc = _template(template, "write")
	variants = _json(variants, _("Items"))
	if not isinstance(variants, dict) or not variants:
		frappe.throw(_("Pick at least one item."))
	if len(variants) > MAX_VARIANTS:
		frappe.throw(_("At most {0} items can be grouped at once.").format(MAX_VARIANTS))
	variants = {str(k).strip(): v for k, v in variants.items()}
	for code in variants:
		if frappe.db.exists("Item", code):
			frappe.get_doc("Item", code).check_permission("write")
	names = template_options(doc.name)
	errors = grouping_errors(doc.name, variants, names)
	if errors:
		return {"ok": False, "errors": errors, "variants": []}

	def run():
		_fire(GROUPING_HOOK, doc.name, list(variants))
		attach_variants(doc.name, variants, names)

	_savepoint_call("ffl_item_options_group", run)
	_fire(ADDED_HOOK, doc.name, list(variants))
	return {"ok": True, "errors": [], "variants": sorted(variants)}


# --- doc event ----------------------------------------------------------------------------------

def keep_variants_own(doc, method=None) -> None:
	"""Item validate: a template's save never copies its fields onto its variants (ERPNext's
	``Item.update_variants``, whatever Item Variant Settings say)."""
	if doc.get("has_variants"):
		doc.flags.dont_update_variants = True
