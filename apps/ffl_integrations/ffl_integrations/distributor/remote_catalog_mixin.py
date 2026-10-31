"""RemoteCatalogMixin — the catalog READ half of a DistributorProvider for any
house whose catalog is served by a Catalog API service (docs/catalog-api).

A provider that mixes this in needs only ``code`` (the distributor code the
service knows) and gets search / catalog_row / catalog_row_by_upc /
feed_heartbeat / catalog_content_heartbeat / prefetch_rows and a catalog
health check for its connection test. RSR and Sports South used to carry
ten byte-identical copies of these; the ordering half (place / status /
verify) stays per house.

``normalise_row`` is the one override hook: a house whose rows need
folding beyond the service's OfferFull → NormalizedRow mapping overrides it.
"""
from __future__ import annotations

from typing import Optional


def remote_normalised(r: dict) -> dict:
	"""Service OfferFull → the frozen NormalizedRow shape.
	unorderable = discontinued OR status Deleted."""
	from ffl_integrations.catalog_local.store import is_deleted

	return {
		"sku": r["sku"], "upc": r["upc"] or "",
		"manufacturer": r["manufacturer"], "model": r["model"],
		"department": r["department"], "cost": float(r["cost"] or 0),
		"map": float(r["map"] or 0), "msrp": float(r["msrp"] or 0),
		"qty": int(r["qty"] or 0), "ffl_required": bool(r["ffl_required"]),
		"dropship_blocked": bool(r["dropship_blocked"]),
		"vendor_approval_required": bool(r["vendor_approval_required"]),
		"image_url": r["image_url"] or "",
		"discontinued": bool(r["discontinued"]) or is_deleted(r.get("status_flag")),
		# The service's class (`pricing.PRODUCT_CLASSES`) from the house's own
		# department code — the one class table; the POS keeps none of its own.
		"product_class": r.get("product_class") or "",
		# The full-case price, per unit, and the case size (RSR's get-items). Optional:
		# a service that sends neither (one older than the fields, or a house without
		# case pricing) reads as no case price.
		"case_cost": float(r.get("case_cost") or 0),
		"units_per_case": int(r.get("units_per_case") or 0),
	}


_CLASSLESS_NOTED: dict = {}


def _classless(code) -> None:
	"""A service that sends rows without `product_class` (a Data Service older than the product_class field,
	a house on its own service that omits the optional field): no class rule
	matches its rows and the shipping class falls back to the FFL flag. Said once an
	hour a house, never raised — a read must not fail on it."""
	import time

	import frappe

	if _CLASSLESS_NOTED.get(code, 0) > time.monotonic():
		return  # this process asked Redis within the hour: not once a row
	_CLASSLESS_NOTED[code] = time.monotonic() + 3600
	key = f"dist_catalog_classless:{code}"
	try:
		if frappe.cache.get_value(key):
			return
		frappe.cache.set_value(key, 1, expires_in_sec=3600)
		frappe.log_error(title=f"Catalog service sends no product_class ({code})",
			message="Its offer rows carry no product_class, so markup and listing rules "
			"that name a class do not match them and the website shipping class falls back "
			"to the FFL flag. Update the catalog service to a version that sends product_class.")
	except Exception:  # noqa: BLE001 — logging must not fail a catalog read
		pass


class RemoteCatalogMixin:
	code: str = ""
	display_name: str = ""   # for log titles; falls back to the code

	def normalise_row(self, r: dict) -> dict:
		if "product_class" not in r:
			_classless(self.code)
		return remote_normalised(r)

	def _house(self) -> str:
		return self.display_name or self.code

	# ---------------------------------------------------------- reads

	def search(self, q: str, limit: int = 10) -> list:
		import frappe
		from ffl_integrations.distributor import remote_catalog
		from ffl_integrations.data_service import CapabilityOff
		try:
			hits = remote_catalog.search(self.code, q, limit)
		except CapabilityOff:
			return []   # no catalog set up: the page says so, nothing to log
		except remote_catalog.DataServiceUnavailable:
			frappe.log_error(title=f"{self._house()} catalog search degraded (catalog service down)",
				message=frappe.get_traceback())
			return []
		from ffl_integrations.distributor.util import legal_firearm
		offers = [dict(hit["best_offer"], distributor=self.code) for hit in hits]
		remote_catalog.classify(offers)   # search offers carry no class
		from ffl_integrations.distributor.util import master_key, prefetch_legal
		prefetch_legal([master_key(o, self.code) for o in offers])   # one read, not one a row
		rows = []
		for offer in offers:
			rows.append({
				"sku": offer["sku"], "item_name": offer["short_description"],
				"cost": offer["cost"], "map": offer["map"], "msrp": offer["msrp"],
				"qty": offer["qty"], "ffl_required": bool(offer["ffl_required"]),
				"department": offer["department"], "upc": offer["upc"] or "",
				"manufacturer": offer["manufacturer"], "model": offer["model"],
				"image_url": offer["image_url"],
				"is_firearm": legal_firearm(offer, self.code),
			})
		return rows

	def catalog_row(self, sku: str) -> Optional[dict]:
		from ffl_integrations.distributor import remote_catalog
		row = remote_catalog.cached_row(self.code, sku)
		return self.normalise_row(row) if row else None

	def catalog_availability(self, sku: str, wished: int) -> dict:
		"""One ``check_availability`` line off the catalog row: orderable while the
		house carries it and has not discontinued it; available when it also has
		``wished`` (and at least one) on hand."""
		row = self.catalog_row(sku) or {}
		qty = int(row.get("qty") or 0)
		return {"sku": sku, "qty": qty, "cost": float(row.get("cost") or 0),
			"available": bool(row) and not row.get("discontinued") and qty >= wished and qty > 0,
			"orderable": bool(row) and not row.get("discontinued")}

	def catalog_row_by_upc(self, upc: str) -> Optional[dict]:
		"""This house's offer on a UPC — the counter-ensure population has no
		listing row mapping item_code → sku and its item_code IS the UPC."""
		from ffl_integrations.distributor import remote_catalog
		hit = remote_catalog.item_by_upc((upc or "").strip())
		for offer in (hit or {}).get("offers") or []:
			if offer.get("distributor") == self.code:
				return self.normalise_row(offer)
		return None

	def feed_heartbeat(self):
		"""Last successful QUANTITY sync — the watchdog's heartbeat."""
		from ffl_integrations.distributor import remote_catalog
		return remote_catalog.parse_service_ts(
			remote_catalog.health(self.code).get("last_quantity_sync_at"))

	def catalog_content_heartbeat(self):
		from ffl_integrations.distributor import remote_catalog
		return remote_catalog.parse_service_ts(
			remote_catalog.health(self.code).get("last_catalog_sync_at"))

	def prefetch_rows(self, skus: list) -> None:
		from ffl_integrations.distributor import remote_catalog
		remote_catalog.prefetch(self.code, list(skus))

	def catalog_health_check(self) -> dict:
		"""One connection-test row for the catalog service."""
		from ffl_integrations.distributor import remote_catalog
		try:
			h = remote_catalog.health(self.code) or {}
			return {"label": "Catalog service", "ok": True,
				"detail": "catalog {0} · quantity {1} · contract {2}".format(
					h.get("last_catalog_sync_at") or "?", h.get("last_quantity_sync_at") or "?",
					h.get("contract_version") or "pre-1.0")}
		except Exception as exc:  # noqa: BLE001 — a probe reports, never aborts
			return {"label": "Catalog service", "ok": False, "detail": str(exc)}
