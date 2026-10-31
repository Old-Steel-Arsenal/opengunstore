"""Drop-ship fulfilment-mode policy (the brand-policy resolution chain).

Resolution chain::

    Dropship Brand Policy table (To Store **or** Direct OK, either is an override)
    → the supplier's per-item flags (dropship_blocked / vendor_approval_required) as the fallback
    → default Direct

**Precedence (REVERSED 2026-07-29):** an explicit
``Direct OK`` brand entry now OVERRIDES the distributor's own block flags.

The original design treated the flags as a *physical* constraint ("the
distributor will refuse to drop-ship the item at all") that no business
preference could beat. Reality falsified that premise: RSR's ORDER system
accepts drop-ship orders from a dealer who holds the brand's authorization
(confirmed for Sig Sauer, 2026-07-29) while the FEED's per-SKU flags keep
saying blocked — the flags describe the default account, not ours. So a
``Direct OK`` entry means: *the operator certifies the distributor accepts our
direct drop-ship orders for this brand despite its feed flags* (brand
authorization registered with the distributor). Wrong certifications are not
silent: v1 places through a human-confirmed queue and a refused line comes
back on the ack.

Under C2 a To-Store item flips MODE rather than being delisted (it can still be
procured to store), which is what `fulfillment_mode` on Distributor Listing
carries.

The precedence core is pure; only `brand_policy_for` touches the DB.
"""
from __future__ import annotations

MODE_DIRECT = "Direct"
MODE_TO_STORE = "To Store"

POLICY_TO_STORE = "To Store"
POLICY_DIRECT_OK = "Direct OK"

BRAND_POLICY_DT = "Dropship Brand Policy"

# Seeded (idempotently, and only when the Brand master already exists) after
# migrate. These are the brands whose distributor terms / warranty
# handling make store-receipt the default.
SEED_BRAND_POLICIES = ("Holosun", "Glock", "Smith & Wesson")


def resolve_mode(*, brand_policy=None, dropship_blocked=False,
		vendor_approval_required=False) -> str:
	"""Fulfilment mode for one catalog item. Returns ``Direct`` or ``To Store``.

	An EXPLICIT brand entry wins in both directions: ``To Store`` forces
	store receipt regardless of flags, ``Direct OK`` certifies the distributor
	accepts our direct orders despite its flags. Only an UNLISTED brand falls
	through to the distributor's per-SKU flags."""
	policy = str(brand_policy or "").strip()
	if policy == POLICY_TO_STORE:
		return MODE_TO_STORE
	if policy == POLICY_DIRECT_OK:
		return MODE_DIRECT
	if dropship_blocked or vendor_approval_required:
		return MODE_TO_STORE
	return MODE_DIRECT


def mode_for_row(row, brand_policy=None) -> str:
	"""`resolve_mode` over a provider NormalizedRow (missing flags = permissive)."""
	row = row or {}
	return resolve_mode(
		brand_policy=brand_policy,
		dropship_blocked=bool(row.get("dropship_blocked")),
		vendor_approval_required=bool(row.get("vendor_approval_required")))


# ------------------------------------------------------------------ frappe


def brand_policy_entry(brand, distributor=None) -> dict | None:
	"""The applicable policy row for a brand as ``{policy, reason, distributor}``:
	the row scoped to ``distributor`` wins over the blank (all-distributors)
	row; None when the brand is unlisted for that house."""
	if not brand:
		return None
	import frappe
	rows = frappe.get_all(BRAND_POLICY_DT, filters={"brand": brand},
		fields=["policy", "reason", "distributor"])
	if not rows:
		return None
	scoped = [r for r in rows if r.distributor and distributor and r.distributor == distributor]
	global_ = [r for r in rows if not r.distributor]
	pick = (scoped or global_ or [None])[0]
	return dict(pick) if pick else None


def brand_policy_for(brand, distributor=None):
	"""The configured policy for a Brand (scoped to ``distributor`` when given),
	or None when the brand isn't listed."""
	entry = brand_policy_entry(brand, distributor)
	return (entry.get("policy") or None) if entry else None


def brand_policy_reason(brand, distributor=None, *, distributor_name=None) -> str:
	"""Clerk-facing sentence for a To-Store brand, '' when the brand is not the
	reason (unlisted, or Direct OK)."""
	entry = brand_policy_entry(brand, distributor)
	if not entry or entry.get("policy") != POLICY_TO_STORE:
		return ""
	house = distributor_name or entry.get("distributor") or ""
	why = (entry.get("reason") or "").strip() or "no drop-ship for this manufacturer"
	return f"{brand}: {why} — ship to store only" + (f" ({house})" if house else "")


def resolve_fulfillment_mode(row, brand=None, distributor=None) -> str:
	"""DB-backed chain entry point: look the brand up (scoped to the distributor
	when given), then apply the pure rules."""
	brand = brand or (row or {}).get("manufacturer")
	return mode_for_row(row, brand_policy=brand_policy_for(brand, distributor))


def ensure_seed_policies() -> dict:
	"""Idempotent after-migrate seed. Only creates a policy row when the Brand
	master exists — the Brand table is populated from feed data (promote /
	intake), so hard-depending on it would break `bench migrate` on a fresh site
	(the Link would fail validation). Brands are matched case-insensitively so a
	feed-cased ``GLOCK`` gets the policy too."""
	import frappe

	existing_brands = {str(b).strip().lower(): b for b in frappe.get_all("Brand", pluck="name")}
	created, skipped = [], []
	for wanted in SEED_BRAND_POLICIES:
		brand = existing_brands.get(wanted.strip().lower())
		if not brand:
			skipped.append(wanted)  # brand not in the catalog yet — next migrate retries
			continue
		# NULL (rows older than the column) and '' are both "global"
		if frappe.db.exists(BRAND_POLICY_DT, {"brand": brand, "distributor": ["is", "not set"]}):
			continue
		frappe.get_doc({
			"doctype": BRAND_POLICY_DT,
			"brand": brand,
			"policy": POLICY_TO_STORE,
			"reason": "store policy: received into the shop before selling",
			"notes": "Seeded default: receive into the store before selling.",
		}).insert(ignore_permissions=True)
		created.append(brand)
	return {"created": created, "skipped": skipped}


def ensure_distributor_row(code: str, legal_name: str, provider: str, *,
		poll: int = 0, priority: int = 0) -> bool:
	"""Create-once seed of a house's Distributor row, DISABLED until the operator
	enables it in the hub. Skipped when a row with that name OR that provider
	already exists, so operator edits (or a renamed row) are never overwritten."""
	import frappe
	if frappe.db.exists("Distributor", code) or frappe.db.exists(
			"Distributor", {"provider": provider}):
		return False
	frappe.get_doc({
		"doctype": "Distributor",
		"code": code,
		"distributor_name": legal_name,
		"provider": provider,
		"enabled": 0,
		"stocking_enabled": 1,
		"dropship_enabled": 1,
		"poll_enabled": poll,
		"priority": priority,
		"safety_buffer": 3,
		"display_cap": 5,
	}).insert(ignore_permissions=True)
	return True


def ensure_scoped_policies(distributor: str, entries: list, *, note: str = "") -> dict:
	"""Idempotent seed of distributor-scoped rows: ``entries`` =
	``[(brand, policy, reason), ...]``. Brand-tolerant like the global seed;
	an existing row (operator edits) is never overwritten."""
	import frappe
	existing = {str(b).strip().lower(): b for b in frappe.get_all("Brand", pluck="name")}
	created, skipped = [], []
	for wanted, pol, reason in entries:
		brand = existing.get(wanted.strip().lower())
		if not brand:
			skipped.append(wanted)
			continue
		if frappe.db.exists(BRAND_POLICY_DT, {"brand": brand, "distributor": distributor}):
			continue
		frappe.get_doc({"doctype": BRAND_POLICY_DT, "brand": brand, "distributor": distributor,
			"policy": pol, "reason": reason, "notes": note}).insert(ignore_permissions=True)
		created.append(brand)
	return {"created": created, "skipped": skipped}
