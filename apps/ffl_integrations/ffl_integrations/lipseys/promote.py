"""Promote a Lipsey's catalog row to a local Item (the counter ensure seam and
the listing pipeline both call ``LipseysProvider.promote_to_item``).

Runs the shared catalog promoter (``sports_south.promote.promote_catalog_row``):
identity chain explicit item_code > UPC > Lipsey's item number, idempotent, MAP
over MSRP as the selling default, filed under the catalog master's category
(``catalog_master.classify``). Catalog data comes from the Data Service, never
from Lipsey's own feed. Only the firearm-type mapping and the caliber gate
(firearms only) are Lipsey's own.
"""
from __future__ import annotations

from ffl_integrations.sports_south.promote import promote_catalog_row

DISTRIBUTOR_CODE = "lipseys"

# Item.firearm_type per Lipsey's department (the Data Service keys it as the
# feed's `type`, case-folded). Anything else that is a firearm under the law
# (muzzleloaders, combos, NFA rifles / shotguns) reads "Other Firearm", which the
# operator can refine on the Item.
FIREARM_TYPE_BY_DEPARTMENT = {
	"semi-auto pistol": "Pistol",
	"specialty handgun": "Pistol",
	"revolver": "Revolver",
	"rifle": "Rifle",
	"shotgun": "Shotgun",
	"accessory-lowers": "Receiver",
	"accessory-frames": "Receiver",
	"accessory-receiver sets": "Receiver",
	"accessory-actions": "Receiver",
	"nfa - silencer": "Silencer",
}


def firearm_type(department) -> str:
	return FIREARM_TYPE_BY_DEPARTMENT.get(str(department or "").strip().casefold(), "Other Firearm")


def promote_to_item(sku: str, opts: dict | None = None) -> dict:
	return promote_catalog_row(DISTRIBUTOR_CODE, "Lipsey's", sku, opts,
		firearm_type=lambda r: firearm_type(r.get("department")),
		extract_caliber=lambda r, filed: filed.is_firearm)
