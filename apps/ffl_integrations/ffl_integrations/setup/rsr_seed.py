"""RSR Department seed — idempotent.

Run after migrate (manually via `bench execute ffl_integrations.setup.rsr_seed.run`,
or hooked into after_install / after_migrate if desired)."""
from __future__ import annotations

import frappe


# (department_number, name, ffl_required, preferred_item_group)
# Item Groups now describe product TYPE only — condition (new vs used) is a
# separate `is_used` checkbox on Item, set during promote / firearm-intake.
# Departments 02/03 ("Used Handguns" / "Used Long Guns") still target the
# clean type group; the loader/promoter set is_used=1 based on dept.
DEPARTMENTS = [
	("01", "Handguns", 1, "Pistol"),         # Handgun split → Pistol leaf (2026-07-21)
	("02", "Used Handguns", 1, "Pistol"),    # (RSR "Handguns" dept doesn't split pistol/revolver)
	("03", "Used Long Guns", 1, "Rifle"),
	("04", "Tasers", 0, "Misc Accessory"),
	("05", "Sporting Long Guns", 1, "Rifle"),
	("06", "SOTS", 1, "NFA"),                       # SOT items = NFA parent group
	("07", "Black Powder Firearms", 0, "Other Firearm"),
	("08", "Scopes", 0, "Optic"),
	("09", "Scope Mounts", 0, "Optic"),
	("10", "Magazines", 0, "Magazine"),
	("11", "Grips/Pads/Stocks", 0, "Stocks & Grips"),
	("12", "Soft Gun Cases", 0, "Misc Accessory"),
	("13", "Misc. Accessories", 0, "Misc Accessory"),
	("14", "Holsters/Pouches", 0, "Holster"),
	("15", "Reloading Equipment", 0, "Cleaning"),
	("16", "Black Powder Accessories", 0, "Misc Accessory"),
	("17", "Closeout Accessories", 0, "Misc Accessory"),
	("18", "Ammunition", 0, "Ammunition"),
	("19", "Survival Supplies", 0, "Misc Accessory"),
	("20", "Flashlights & Batteries", 0, "Light"),
	("21", "Cleaning Equipment", 0, "Cleaning"),
	("22", "Airguns", 0, "Misc Accessory"),
	("23", "Knives", 0, "Knife"),
	("24", "High Capacity Magazines", 0, "Magazine"),
	("25", "Safes/Security", 0, "Safe"),
	("26", "Safety/Protection", 0, "Misc Accessory"),
	("27", "Non-Lethal Defense", 0, "Misc Accessory"),
	("28", "Binoculars", 0, "Optic"),
	("29", "Spotting Scopes", 0, "Optic"),
	("30", "Sights/Lasers/Lights", 0, "Optic"),
	("31", "Optical Accessories", 0, "Optic"),
	("32", "Barrels/Choke Tubes", 0, "Misc Accessory"),
	("33", "Clothing", 0, "Misc Accessory"),
	("34", "Parts", 0, "Misc Accessory"),
	("35", "Slings/Swivels", 0, "Slings & Mounts"),
	("36", "Electronics", 0, "Misc Accessory"),
	("37", "Not Used", 0, "Misc Accessory"),
	("38", "Books/Software", 0, "Misc Accessory"),
	("39", "Targets", 0, "Misc Accessory"),
	("40", "Hard Gun Cases", 0, "Misc Accessory"),
	("41", "Upper Receivers/Conv Kits", 1, "Rifle"),
	("42", "SBR Uppers", 1, "Rifle"),
	("43", "Upper/Conv Kits-High Cap", 1, "Rifle"),
]


def _seed_rsr_distributor() -> bool:
	"""Ensure the RSR Distributor row exists so the provider registry + restock
	page are coherent out of the box. Seeded DISABLED and with no creds/supplier —
	inert until go-live config enables it (mirrors api_enabled=0). Create-once: an
	existing row (with operator edits, or an rsr-provider row under another
	name) is never overwritten."""
	from ffl_integrations.distributor.policy import ensure_distributor_row

	return ensure_distributor_row("RSR", "RSR Group, Inc.", "rsr")


def run() -> dict:
	existing_groups = set(frappe.get_all("Item Group", pluck="name"))
	fallback = "Products" if "Products" in existing_groups else "All Item Groups"

	created = 0
	updated = 0
	for dept_num, name, ffl, target_group in DEPARTMENTS:
		group = target_group if target_group in existing_groups else fallback
		if frappe.db.exists("RSR Department", dept_num):
			frappe.db.set_value("RSR Department", dept_num, {
				"department_name": name,
				"ffl_required": ffl,
				"default_item_group": group,
			}, update_modified=False)
			updated += 1
		else:
			frappe.get_doc({
				"doctype": "RSR Department",
				"department_number": dept_num,
				"department_name": name,
				"ffl_required": ffl,
				"default_item_group": group,
			}).insert(ignore_permissions=True)
			created += 1
	seeded_distributor = _seed_rsr_distributor()
	# Drop-ship brand policies (P3). Idempotent and Brand-tolerant: a brand the
	# catalog hasn't produced yet is skipped and picked up by the next migrate,
	# so a fresh site can't die on the Link validation.
	from ffl_integrations.distributor.policy import ensure_seed_policies

	brand_policies = ensure_seed_policies()
	frappe.db.commit()
	return {"created": created, "updated": updated, "total": len(DEPARTMENTS),
		"distributor_seeded": seeded_distributor, "brand_policies": brand_policies}
