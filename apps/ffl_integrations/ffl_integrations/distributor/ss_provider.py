"""Sports South provider — DistributorProvider #2, bound to
``sports_south/api_client.py`` (orders.asmx / invoices.asmx). Catalog reads go
through the shared Data Service catalog service under distributor code ``ss``,
exactly like RSR; on a Local-catalog site the site's own tables serve them
(catalog_local/drivers/ss.py).

Ordering hard rules (SS Order Fulfillment Overview 2022-08-22, design run
``ss-dropship-decisions.md``):

* **Firearms and non-firearms must be separate orders** — the same
  firearm/accessory split RSR has.
* **Same transfer dealer per firearm order** — inherent: one DO has
  one destination.
* **Handguns ship air only** (2-Day $10.95 / Next-Day); long guns, ammo and
  accessories default to Ground Premium (trackable, interceptable). Ammo is
  HAZMAT and can never go air.
* **No backorders**: a short is cancelled (before invoicing) or credited
  (after). ORQTYF < ORQTY on GetDetail is the short signal; the daily
  GetCreditsByDate poll catches the post-invoice case.
* **OrderPrice is not a price lock** — ORPRCF after Submit is what SS charges;
  drift beyond the settings threshold alerts.
* **Firearm orders need the transfer dealer's FFL emailed within 3 business
  days** — ``sports_south.ffl_email`` sends it after Submit.

``plan_fulfillment``, ``ship_method_for``, ``estimate_freight``,
``normalize_detail`` and ``normalize_order_status`` are pure so the split
matrix and the status mapping unit-test without a site.
"""
from __future__ import annotations

from typing import Optional

from ffl_integrations.distributor import lanes
from ffl_integrations.distributor.base import DistributorProvider
from ffl_integrations.distributor.remote_catalog_mixin import RemoteCatalogMixin
from ffl_integrations.distributor.util import setting_value

DISTRIBUTOR_CODE = "ss"

# SS CATIDs (catalog_departments, 93 rows, 2026-09-08). Handguns → air only.
HANDGUN_CATEGORIES = frozenset({"26", "64", "96"})
LONG_GUN_CATEGORIES = frozenset({"40", "48", "57", "58", "59", "94"})
RECEIVER_CATEGORIES = frozenset({"63", "66", "67"})
OTHER_FIREARM_CATEGORIES = frozenset({"65"})
SUPPRESSOR_CATEGORIES = frozenset({"93"})
AMMO_CATEGORIES = frozenset({"4", "5", "6", "49", "69", "70", "71", "72", "90", "91"})
FIREARM_CATEGORIES = (HANDGUN_CATEGORIES | LONG_GUN_CATEGORIES | RECEIVER_CATEGORIES
	| OTHER_FIREARM_CATEGORIES | SUPPRESSOR_CATEGORIES)

# Item.firearm_type per firearm CATID (promote). Receivers/uppers/lowers/frames
# are all "Receiver" in the bound book.
FIREARM_TYPE_BY_CATEGORY = {
	**{c: "Pistol" for c in ("26", "96")},
	"64": "Revolver",
	**{c: "Rifle" for c in ("40", "57", "58", "94")},
	**{c: "Shotgun" for c in ("48", "59")},
	**{c: "Receiver" for c in RECEIVER_CATEGORIES},
	"65": "Other Firearm",
	"93": "Silencer",
}

# DO line ship_method labels (the Distributor Order Line Select) → SS ShipVIA
# comes from Sports South Settings at placement; the plan only picks the lane.
SHIP_GROUND = "Grnd"
SHIP_2DAY = "2Day"

# SS has no per-order line cap documented; keep one well under anything a
# form-encoded AddDetail loop should do in one job.
MAX_ORDER_SKUS = 200

# Manufacturers SS will not fulfil and those needing
# pre-approval. Seeded by sports_south.seed as Dropship Brand Policy
# rows SCOPED to the SS Distributor (never global — RSR keeps drop-shipping
# Ruger/Springfield/...). The operator flips a pre-approval row to Direct OK
# once the rep clears the brand.
BLOCKED_BRANDS = ("Beretta USA", "Beretta", "Glock", "HK", "Heckler & Koch", "IWI US",
	"IWI", "Moultrie", "Outdoor Edge", "Ruger", "Smith & Wesson", "Springfield Armory",
	"Tikka", "Thermacell", "T/C Arms", "Thompson Center", "Trijicon EO", "Walther Arms",
	"Walther")
PREAPPROVAL_BRANDS = ("1791 Gunleather", "Leupold", "Sig Sauer", "Trijicon")

# Carrier services (SS ShipVIA codes, confirmed 2026-09-08). Handguns are
# air-only; ammunition is HAZMAT and cannot fly.
SERVICE_ECONOMY = ""
SERVICE_GROUND = "G"
SERVICE_2DAY = "2"
SERVICE_NEXTDAY = "N"
AIR_SERVICES = frozenset({SERVICE_2DAY, SERVICE_NEXTDAY})

SS_CAPABILITIES = frozenset({
	"search", "catalog", "check_availability", "place_order",
	"dropship", "fds", "to_store", "verify_dealer",
})

# Line item_status values the generic layer reads as killed (tasks.KILLED_ITEM_STATUSES).
STATUS_CANCELLED = "CANCELLED"
STATUS_SHORT = "SHORT"
# Placement-time 0 fill: SS's fill timing after Submit is unverified (the
# sandbox only proved 0 BEFORE Submit), so a 0 at placement is "unfilled",
# not killed — the invoice poll decides (0 after invoicing = CANCELLED).
STATUS_UNFILLED = "UNFILLED"

def line_class(department: str | None) -> str:
	"""``handgun`` / ``long_gun`` / ``ammo`` / ``accessory`` from an SS CATID."""
	d = str(department or "").strip()
	if d in HANDGUN_CATEGORIES:
		return "handgun"
	if d in FIREARM_CATEGORIES:
		return "long_gun"
	if d in AMMO_CATEGORIES:
		return "ammo"
	return "accessory"


def ship_method_for(line: dict) -> str:
	if line.get("ship_method"):
		return line["ship_method"]
	return SHIP_2DAY if line_class(line.get("department")) == "handgun" else SHIP_GROUND


class SSProvider(RemoteCatalogMixin, DistributorProvider):
	code = DISTRIBUTOR_CODE
	display_name = "Sports South"
	settings_doctype = "Sports South Settings"
	max_order_lines = MAX_ORDER_SKUS
	# Content (DailyItemUpdate / ListNewText) is a DAILY lane on the service
	# (the Data Service SS_CATALOG_STALE = 20h; its own ops threshold 36h) — the 6h
	# RSR-paced default alarmed every afternoon. Quantity keeps the 90-min watch.
	catalog_content_stale_seconds = 36 * 3600

	def __init__(self, client=None):
		self._client_obj = client

	def capabilities(self) -> set:
		return set(SS_CAPABILITIES)

	# ---------------------------------------------------------- pure: planning

	def ship_method_for(self, line: dict) -> str:
		"""Handguns fly (2-Day / Next-Day); long guns, ammunition and accessories
		default to Ground Premium."""
		return ship_method_for(line)

	# catalog reads: RemoteCatalogMixin

	def promote_to_item(self, sku: str, opts=None) -> dict:
		from ffl_integrations.sports_south.promote import promote_to_item
		return promote_to_item(sku, opts or {})

	def check_availability(self, lines: list) -> list:
		"""Pre-order gate off the catalog service's quantity lane (≤30 min old
		by design). SS has no free per-cart live check we can use
		without an order; the authoritative answer is ORQTYF after Submit."""
		out = []
		for ln in lines:
			sku = ln.get("sku")
			if not sku:
				continue
			row = self.catalog_row(sku) or {}
			qty = int(row.get("qty") or 0)
			wished = int(ln.get("qty") or 0)
			out.append({"sku": sku, "qty": qty, "cost": float(row.get("cost") or 0),
				"available": bool(row) and not row.get("discontinued") and qty >= wished and qty > 0,
				"orderable": bool(row) and not row.get("discontinued")})
		return out

	# ---------------------------------------------------------- shipping choice

	def estimate_order_freight(self, lines: list, mode: str, options=None) -> Optional[float]:
		import frappe

		settings = frappe.get_cached_doc("Sports South Settings")
		firearm_order = any(ln.get("is_firearm") for ln in lines)
		flags = clean_delivery_options(options) if options is not None \
			else signature_flags(settings, firearm_order)
		priced = [{**ln, "wish_qty": ln.get("qty")} for ln in lines]
		return estimate_freight(priced, settings, firearm_order,
			store=not lanes.ships_to_consumer(mode), **flags)

	def delivery_options(self, firearm_order: bool) -> dict:
		import frappe

		return signature_flags(frappe.get_cached_doc(self.settings_doctype), firearm_order)

	def account_number(self, account: str) -> str:
		import frappe
		return (frappe.get_cached_doc(self.settings_doctype).get("customer_number") or "").strip()

	def hub_summary(self) -> dict:
		import frappe

		s = frappe.get_cached_doc(self.settings_doctype)
		return {"live_orders": bool(s.ordering_enabled), "ship_to_store": store_freight_summary(s)}

	def shipping_options(self, do) -> list:
		"""OPTIONAL provider hook: the services this order may ship by, with
		the flat-rate estimate, and which one is the default. Pure over the
		DO's lines + Settings (read here so the counter/DO form gets one
		call)."""
		import frappe
		settings = frappe.get_cached_doc(self.settings_doctype)
		lines = [_line(ln) for ln in (getattr(do, "lines", None) or [])]
		return shipping_options_for(lines, settings, is_firearm_order(do),
			has_ammo=_has_ammo(do), store=_ships_to_store(do))

	# ---------------------------------------------------------- ordering

	def place_order(self, do_name: str) -> dict:
		import frappe
		do = frappe.get_doc("Distributor Order", do_name)
		return self._place(do, self._get_client(), frappe.get_cached_doc(self.settings_doctype))

	def _place(self, do, client, settings) -> dict:
		"""AddHeader → AddDetail×N → Submit → GetDetail. The SS web order number
		is written to ``web_ref`` (DB + the in-memory doc, own commit) right
		after AddHeader so a crash mid-way resumes or deletes instead of
		double-ordering. Resume reads GetHeader's STATUS: ``W`` = still open →
		delete and start clean; missing (REPLY) → start clean; anything else =
		submitted → read it back, never re-place."""
		import frappe

		from ffl_integrations.sports_south.api_client import SSConfigError, SSError

		if not client.config.ordering_enabled:
			raise SSConfigError("Sports South Settings: ordering is disabled.")
		mode = lanes.mode_of(do.order_type) or lanes.DEFAULT_MODE
		lines = [_line(ln) for ln in (do.lines or [])]
		if not lines:
			raise SSError(f"{do.name}: no lines to place", terminal=True)
		firearm_order = is_firearm_order(do)

		prior = (do.web_ref or "").strip()
		if prior:
			state = header_state(client.get_header(int(prior)))
			if state == "submitted":
				return self._placed_result(client, do, int(prior), lines, settings,
					firearm_order, resumed=True)
			if state == "open":
				client.delete_open_order(int(prior))
			_set_web_ref(do, "")

		ship_to = build_ship_to(do, mode, settings)
		if not lanes.ships_to_consumer(mode) and not all(
				ship_to.get(k) for k in ("line1", "city", "state", "zip")):
			# A store-bound order ships to the Settings override or the company's own
			# address; incomplete, SS would be handed an order with nowhere (or
			# somewhere unverified) to go.
			if store_ship_override(settings):
				raise SSConfigError("Sports South Settings: the ship-to address override "
					"(Ordering tab) is incomplete — complete it, or clear it to ship to the "
					"company address.")
			raise SSConfigError(f"The company address of {ship_to.get('name') or 'the company'} "
				"is incomplete — add its street, city, state and ZIP (Company → Address), or "
				"fill the ship-to address override in Sports South Settings.")
		flags = order_flags(do, settings, firearm_order)
		service = chosen_service(do, lines, settings, firearm_order, has_ammo=_has_ammo(do))
		header = client.add_header(
			po=do.name, ship_to=ship_to, ship_via=service,
			customer_order_number=(do.source_name or "")[:30] if do.source_name else "",
			sales_message=(settings.sales_message or "")[:200],
			**flags)
		_set_web_ref(do, str(header))
		from ffl_core.utils import commit as safe_commit  # no-op inside the test txn
		safe_commit()  # the SS order number must survive whatever happens next

		refused = []
		for ln in lines:
			ok = client.add_detail(header, sku=ln["sku"], qty=ln["wish_qty"],
				order_price=ln["unit_cost"], customer_item=ln.get("item_code") or "",
				customer_desc=ln.get("description") or "")
			if not ok:
				refused.append(ln["sku"])
		if refused:
			client.delete_open_order(header)
			_set_web_ref(do, "")
			raise SSError(
				f"Sports South refused line(s) {', '.join(refused)} (AddDetail=false, no "
				f"reason given) — order withdrawn, nothing placed. Check item number / "
				f"stock / fulfilment eligibility.", terminal=True)
		if not client.submit(header):
			client.delete_open_order(header)
			_set_web_ref(do, "")
			raise SSError(f"Sports South Submit=false for web order {header} — order "
				f"withdrawn, nothing placed.", terminal=True)
		return self._placed_result(client, do, header, lines, settings, firearm_order)

	def _placed_result(self, client, do, header: int, lines: list, settings,
			firearm_order: bool, *, resumed: bool = False) -> dict:
		import frappe

		from ffl_integrations.notifications import notify_admins

		detail = client.get_detail(header)
		result = normalize_placement(header, detail, lines,
			freight=estimate_freight(lines, settings, firearm_order,
				service=getattr(do, "shipping_service", None), store=_ships_to_store(do),
				**order_flags(do, settings, firearm_order)))
		result["resumed"] = resumed
		if all(a["ack_qty"] == 0 for a in result["ack_lines"]):
			notify_admins(
				f"Sports South filled nothing on {do.name} (web order {header})",
				"Every line reads ORQTYF=0 right after Submit. Either SS has no stock "
				"(the invoice poll will settle it) or the fill is computed later than we "
				"assumed — check the order on SS's site.",
				dedup_key=f"ss_zero_fill_{do.name}")
		drift = price_drift(detail, lines, float(settings.price_drift_alert_pct or 0))
		if drift:
			notify_admins(
				f"Sports South price drift on {do.name}",
				"OrderPrice is not a price lock at SS; these lines will invoice above "
				"the quoted cost:\n\n" + "\n".join(
					f"  {d['sku']}: quoted {d['quoted']:.2f} → filled {d['filled']:.2f} "
					f"(+{d['pct']:.1f}%)" for d in drift),
				dedup_key=f"ss_drift_{do.name}")
		# Resumed or not: the FFL email is idempotent on the DO (ffl_email marks
		# notes_posted), so a crash between Submit and enqueue cannot lose it.
		if firearm_order and settings.auto_email_ffl:
			from ffl_integrations.sports_south.ffl_email import enqueue_ffl_email
			enqueue_ffl_email(do.name)
		return result

	def get_order_status(self, do_name: str) -> dict:
		import frappe
		do = frappe.get_doc("Distributor Order", do_name)
		client = self._get_client()
		web_ref = (do.web_ref or "").strip()
		if not web_ref:
			raise RuntimeError(f"{do_name} has no Sports South web order number")
		detail = client.get_detail(int(web_ref))
		invoices = client.get_invoices_by_po(do.name)
		tracking = client.get_tracking_by_po(do.name) if invoices else []
		serials = []
		if invoices and is_firearm_order(do):
			for inv in invoices:
				no = inv.get("S1INV") or ""
				if no:
					try:
						serials.extend(client.get_serials_for_invoice(no))
					except Exception:  # noqa: BLE001 — serials are a bonus, never block a poll
						frappe.log_error(title=f"SS serials read failed {do_name}/{no}",
							message=frappe.get_traceback())
		return normalize_order_status(web_ref, detail, invoices, tracking, serials)

	def list_invoices(self, since: str, known=()) -> list:
		"""Every invoice on the account dated ``since``..today, normalised for
		``distributor.invoices`` — whoever placed the order. ``known`` invoice
		numbers are skipped before their detail calls."""
		import frappe
		from frappe.utils import nowdate

		client = self._get_client()
		out = []
		for header in client.get_invoices_by_date(since, nowdate()):
			no = str(header.get("S1INV") or "").strip()
			if not no or no in known:
				continue
			try:
				detail = client.get_invoice_detail(no)
				tracking = client.get_tracking_for_invoice(no)
				try:
					# Asked for every new invoice: the invoice rows carry no product class
					# and serials also ride on NFA / non-"firearm" serialized items, so
					# there is nothing reliable to skip on. One call per NEW invoice.
					serials = client.get_serials_for_invoice(no)
				except Exception:  # noqa: BLE001 — serials are a bonus, never block a mirror
					frappe.log_error(title=f"Sports South invoice serials read failed: {no}",
						message=frappe.get_traceback())
					serials = []
				out.append(normalize_invoice(header, detail, tracking, serials))
			except Exception:  # noqa: BLE001 — skip; retried next run
				frappe.log_error(title=f"Sports South invoice read failed: {no}",
					message=frappe.get_traceback())
		return out

	def invoice_tracking(self, invoice: dict) -> list:
		return _tracking_numbers(self._get_client().get_tracking_for_invoice(invoice["invoice_number"]))

	# SS puts our PO (the Distributor Order name) on its invoices: matched at sync.
	invoices_carry_po = True

	def invoice_destination(self, invoice: dict) -> tuple:
		"""``(destination, note)`` for an unmatched SS invoice, from GetShipTo on each
		order it bills: Store when EVERY order ships to the shop's address, Drop-ship
		when every one ships elsewhere, Unknown otherwise — no order number, an empty
		answer (SS returns nothing for a consumer drop-ship), a mixed invoice, or a
		failed call. Never Store on doubt. A failed call is noted ``NOTE_FAILED`` (the
		sync asks again); every other Unknown is a settled answer."""
		import frappe

		refs = [r.strip() for r in (invoice.get("order_ref") or "").split(",") if r.strip()]
		if not refs:
			return lanes.with_note(lanes.DEST_UNKNOWN)
		try:
			store = self._store_address()
			client = self._get_client()
			verdicts = {ship_to_verdict(client.get_ship_to(ref), store) for ref in refs}
		except Exception:  # noqa: BLE001 — Unknown is the safe answer
			frappe.log_error(title=f"Sports South ship-to read failed: {invoice.get('invoice_number')}",
				message=frappe.get_traceback())
			return lanes.DEST_UNKNOWN, lanes.NOTE_FAILED
		return lanes.with_note(verdicts.pop() if len(verdicts) == 1 else lanes.DEST_UNKNOWN)

	def _store_address(self) -> dict:
		"""The address orders ship to today, normalised the way ``company_ship_to``
		writes it (trimmed, state as its USPS code): the Settings override when it can
		be compared — it names a street and a ZIP — else the company's. A partly filled
		override (say, only a phone) is what ``build_ship_to`` would send too, but it
		cannot identify the shop, so the comparison uses the company address."""
		import frappe

		from ffl_core.compliance.interstate_tax import normalize_us_state

		override = store_ship_override(frappe.get_cached_doc(self.settings_doctype))
		block = override if override and str(override.get("line1") or "").strip() \
			and str(override.get("zip") or "").strip() else company_ship_to(None)
		state = str(block.get("state") or "").strip()
		return {**block, "line1": str(block.get("line1") or "").strip(),
			"zip": str(block.get("zip") or "").strip(),
			"state": normalize_us_state(state) or state.upper()}

	def list_open_orders(self) -> list:
		"""Every order header on the account: [{order_ref, po_number, order_date,
		status}]. The caller drops the invoiced ones and those placed from here."""
		return [{
			"order_ref": str(h.get("ORDNO") or "").strip(),
			"po_number": str(h.get("ORPO") or "").strip(),
			"order_date": _yymmdd(h.get("ORDATE")),
			"status": str(h.get("STATUS") or "").strip(),
		} for h in self._get_client().get_order_headers() if str(h.get("ORDNO") or "").strip()]

	def verify_transfer_dealer(self, ffl: str) -> Optional[dict]:
		"""FFLAcceptsTransfer: expiry date = accepts; ``0`` = refuses; ``UNKNOWN``
		= not in SS's file (they will not ship to it). Truthiness IS the verdict."""
		verdict = (self._get_client().ffl_accepts_transfer(ffl) or "").strip()
		if not verdict or verdict.upper() in ("0", "UNKNOWN", "AUTHFAIL", "F"):
			return None
		return {"ffl": ffl, "accepts_transfers": True, "expires": verdict, "raw": verdict}

	def connection_test(self) -> dict:
		"""A Local-catalog site also proves its inventory.asmx login."""
		import frappe

		from ffl_integrations.catalog_local import is_local
		from ffl_integrations.sports_south.api_client import SSOrdersClient, probe_connection

		checks = [self.catalog_health_check()]
		if is_local():
			checks.append(self.feed_login_check())
		s = frappe.get_cached_doc(self.settings_doctype)
		if not s.ordering_enabled:
			checks.append({"label": "Ordering API", "ok": None,
				"detail": "Ordering disabled in Sports South Settings."})
			return {"checks": checks}
		for key, label in (("ship_via_ground", "Ground ship code"), ("ship_via_2day", "2-Day ship code")):
			if not (getattr(s, key, "") or "").strip():
				checks.append({"label": label, "ok": False,
					"detail": f"{key} is empty — AddHeader needs a ShipVIA code (SS Fulfillment Shipping Guidelines)."})
		try:
			checks.extend(probe_connection(SSOrdersClient.from_settings()))
		except Exception as exc:  # noqa: BLE001
			checks.append({"label": "Ordering API", "ok": False, "detail": str(exc)})
		if s.sandbox_mode:
			checks.append({"label": "Sandbox", "ok": None,
				"detail": "Source=99994 — orders route to SS's test environment."})
		return {"checks": checks}

	@staticmethod
	def feed_login_check() -> dict:
		"""The local catalog's inventory.asmx feed: log in and fetch the category list,
		nothing else. The detail never carries a credential."""
		from ffl_integrations.catalog_local.drivers import ss as feed

		label = "Catalog feed (inventory.asmx)"
		try:
			creds = feed.credentials()
			if creds is None:
				return {"label": label, "ok": False, "detail": "The catalog is Local, but Sports South "
					"Settings has no account number and API password."}
			info = feed.InventoryTransport(creds).probe()
		except Exception as exc:  # noqa: BLE001 — one leg reports, the others still run
			return {"label": label, "ok": False, "detail": str(exc)}
		return {"label": label, "ok": True, "detail": f"Logged in; {info['categories']} categories."}

	def _get_client(self):
		if self._client_obj is None:
			from ffl_integrations.sports_south.api_client import SSOrdersClient
			self._client_obj = SSOrdersClient.from_settings()
		return self._client_obj


def get_provider() -> SSProvider:
	return SSProvider()


# ------------------------------------------------------------- helpers

def _set_web_ref(do, value: str) -> None:
	"""DB + the in-memory doc: place_order_job holds ``do`` across the call and
	saves it on error — a DB-only write would be clobbered by that save."""
	import frappe
	do.web_ref = value
	frappe.db.set_value("Distributor Order", do.name, "web_ref", value, update_modified=False)


def is_firearm_order(do) -> bool:
	"""Firearm lane by the LINES, not just order_type: plan_fulfillment splits
	firearms into their own order for stocking / to_store too, where
	order_type stays Stocking / Procure To Store."""
	if lanes.mode_of(getattr(do, "order_type", "")) == lanes.FDS:
		return True
	import frappe
	for ln in (getattr(do, "lines", None) or []):
		code = ln.get("item_code") if isinstance(ln, dict) else getattr(ln, "item_code", None)
		if code and frappe.db.get_value("Item", code, "is_firearm"):
			return True
	return False


def header_state(header_rows: list) -> str:
	"""GetHeader → ``open`` (STATUS W, unsubmitted), ``missing`` (REPLY row /
	no ORDNO — deleted or never existed) or ``submitted`` (any other STATUS).
	Sandbox-verified 2026-09-08: a deleted order answers a Table/REPLY row."""
	for r in header_rows or []:
		if r.get("ORDNO"):
			return "open" if (r.get("STATUS") or "").strip().upper() == "W" else "submitted"
	return "missing"


def _line(ln) -> dict:
	get = ln.get if isinstance(ln, dict) else lambda k: getattr(ln, k, None)
	return {
		"sku": str(get("sku") or "").strip(),
		"item_code": get("item_code") or "",
		"description": get("description") or "",
		"wish_qty": int(get("wish_qty") or 0),
		"unit_cost": float(get("unit_cost") or 0),
		"ship_method": get("ship_method") or SHIP_GROUND,
		"department": get("department") or "",
	}


def signature_flags(settings, firearm_order: bool) -> dict:
	"""AddHeader's three booleans from Settings, one set for firearm orders and
	one for everything else."""
	pfx = "firearm" if firearm_order else "other"
	return {
		"adult_signature": bool(getattr(settings, f"{pfx}_adult_signature", 0)),
		"signature": bool(getattr(settings, f"{pfx}_signature", 0)),
		"insurance": bool(getattr(settings, f"{pfx}_insurance", 0)),
	}


def clean_delivery_options(options) -> dict:
	"""The three AddHeader booleans out of whatever the counter sent (missing = off)."""
	options = options if isinstance(options, dict) else {}
	return {k: bool(options.get(k)) for k in ("adult_signature", "signature", "insurance")}


def order_flags(do, settings, firearm_order: bool) -> dict:
	"""The options this order goes out with: the ones picked in Create Order when the
	Distributor Order records them, else Settings for its kind of order."""
	if getattr(do, "delivery_options_set", 0):
		return {
			"adult_signature": bool(getattr(do, "delivery_adult_signature", 0)),
			"signature": bool(getattr(do, "delivery_signature", 0)),
			"insurance": bool(getattr(do, "delivery_insurance", 0)),
		}
	return signature_flags(settings, firearm_order)


def _has_ammo(do) -> bool:
	import frappe
	for ln in (getattr(do, "lines", None) or []):
		code = ln.get("item_code") if isinstance(ln, dict) else getattr(ln, "item_code", None)
		if code and (frappe.db.get_value("Item", code, "item_group") or "").lower().find("ammo") >= 0:
			return True
	return False


def _ships_to_store(do) -> bool:
	return not lanes.ships_to_consumer(lanes.mode_of(do.order_type) or lanes.DEFAULT_MODE)


def shipping_options_for(lines: list, settings, firearm_order: bool, *, has_ammo: bool = False,
		store: bool = False) -> list:
	"""``[{code, label, estimate, default}]`` for this order's lane. Handgun
	(any 2Day line) → air only; ammunition → ground only (HAZMAT); otherwise
	every service. Default = Settings (Ground Premium unless the Economy
	switch is on for non-firearm orders; 2-Day for handguns). ``store``: the
	order ships to the shop, so the lane's base service (2-Day for handguns,
	Ground otherwise) is priced by ``estimate_store_freight``; air upgrades keep
	their drop-ship rates. Pure."""
	handgun = any(ln.get("ship_method") == SHIP_2DAY for ln in lines)
	g = lambda k, d: float(getattr(settings, k, None) or d)  # noqa: E731
	base_rate = (lambda k, d: estimate_store_freight(lines, settings)) if store else g  # noqa: E731
	if handgun:
		lane_rate = base_rate("rate_handgun_2day", 10.95)
		base = [(SERVICE_2DAY, "2-Day Air", lane_rate), (SERVICE_NEXTDAY, "Next-Day Air", g("rate_next_day", 42.95))]
	elif firearm_order:
		base = [(SERVICE_GROUND, "Ground Premium", base_rate("rate_long_gun_ground", 10.95)),
			(SERVICE_2DAY, "2-Day Air", g("rate_2day_air", 27.95)),
			(SERVICE_NEXTDAY, "Next-Day Air", g("rate_next_day", 42.95))]
	else:
		base = [(SERVICE_ECONOMY, "Ground Economy (no claims / intercepts)",
				base_rate("rate_accessory_economy", 7.95)),
			(SERVICE_GROUND, "Ground Premium", base_rate("rate_accessory_ground", 10.95))]
		if not has_ammo:
			base += [(SERVICE_2DAY, "2-Day Air", g("rate_2day_air", 27.95)),
				(SERVICE_NEXTDAY, "Next-Day Air", g("rate_next_day", 42.95))]
	default = ship_via_for(lines, settings, firearm_order, strict=False)
	out = []
	for code, label, rate in base:
		out.append({"code": code, "label": f"{label} — ${rate:.2f}", "estimate": rate,
			"default": code == default})
	if not any(o["default"] for o in out):
		out[0]["default"] = True
	return out


def chosen_service(do, lines: list, settings, firearm_order: bool, *, has_ammo: bool = False) -> str:
	"""The service to send: the DO's choice when it is one this order may use,
	else the default. A stale/incompatible choice (a handgun added after
	Ground was picked) is refused loudly rather than shipped wrong."""
	options = shipping_options_for(lines, settings, firearm_order, has_ammo=has_ammo)
	chosen = getattr(do, "shipping_service", None)
	if chosen is None or (chosen == "" and not getattr(do, "shipping_service_label", "")):
		return next(o["code"] for o in options if o["default"])
	if any(o["code"] == chosen for o in options):
		return chosen
	from ffl_integrations.sports_south.api_client import SSError
	raise SSError(f"{do.name}: shipping service {chosen!r} is not allowed for this order "
		f"(allowed: {', '.join(repr(o['code']) for o in options)}) — re-select it on the "
		f"Draft order.", terminal=True)


def ship_via_for(lines: list, settings, firearm_order: bool = True, *, strict: bool = True) -> str:
	"""Default ShipVIA per order (SS codes, confirmed by SS 2026-09-08: Ground
	Premium ``G``, 2-Day Air ``2``, Next-Day Air ``N``, Ground Economy =
	EMPTY). Any air-only line (handgun) → the 2-Day code; a non-firearm order
	with the Economy switch on → empty string on purpose; otherwise Ground."""
	if any((ln.get("ship_method") == SHIP_2DAY) for ln in lines):
		code = (getattr(settings, "ship_via_2day", "") or "").strip()
	elif not firearm_order and getattr(settings, "accessory_ground_economy", 0):
		return SERVICE_ECONOMY
	else:
		code = (getattr(settings, "ship_via_ground", "") or "").strip()
	if not code and strict:
		from ffl_integrations.sports_south.api_client import SSConfigError
		raise SSConfigError("Sports South Settings: ship via code is empty — Ground Premium "
			"is G, 2-Day Air is 2 (Sports South ship codes).")
	return code


_STORE_SHIP_KEYS = ("name", "attn", "line1", "line2", "city", "state", "zip", "phone")


def store_ship_override(settings) -> Optional[dict]:
	"""Sports South Settings' ship-to address override as a ShipTo block, or None
	when every ``store_ship_*`` field is blank. Any one filled field makes the
	whole block the override: it is never topped up from the company address."""
	block = {k: getattr(settings, f"store_ship_{k}", "") or "" for k in _STORE_SHIP_KEYS}
	return block if any(str(v).strip() for v in block.values()) else None


def company_ship_to(company) -> dict:
	"""The company's own address — the one its receipts print
	(``ffl_core.store_identity``) — as a ShipTo block; blank company = the
	default company. Missing pieces come back empty for the caller's check. The
	state is free text on an Address ("Texas" and "TX" both exist) while the header
	keeps two characters, so it goes out as its USPS code; one nobody can map comes
	back empty and the address counts as incomplete."""
	import frappe

	from ffl_core.compliance.interstate_tax import normalize_us_state
	from ffl_core.store_identity import company_address, company_brand
	from ffl_integrations.distributor.accounting import default_company

	company = company or default_company()
	brand = company_brand(company)
	address = company_address(company)
	a = frappe.db.get_value("Address", address, ["address_line1", "address_line2", "city",
		"state", "pincode", "phone"], as_dict=True) if address else None
	a = a or {}
	return {
		"name": brand["name"], "attn": "",
		"line1": a.get("address_line1") or "", "line2": a.get("address_line2") or "",
		"city": a.get("city") or "", "state": normalize_us_state(a.get("state")) or "",
		"zip": a.get("pincode") or "",
		"phone": a.get("phone") or brand["phone"],
	}


def build_ship_to(do, mode: str, settings) -> dict:
	"""AddHeader ShipTo block per lane. ``fds`` ships to the transfer dealer's
	premises ONLY (attn = the consumer, per SS's "company name → put the
	person in Attn" rule); ``direct`` to the consumer; stocking / to_store to
	the shop — the Settings override when any of it is filled, else the DO's
	company address."""
	get = lambda k: (getattr(do, k, "") or "")  # noqa: E731
	if mode == "fds":
		name = ""
		phone = ""
		if get("transfer_ffl_record"):
			import frappe
			rec = frappe.db.get_value("ATF FFL Record", do.transfer_ffl_record,
				["licensee_dba", "licensee_name", "phone"], as_dict=True) or {}
			name = rec.get("licensee_dba") or rec.get("licensee_name") or ""
			phone = rec.get("phone") or ""
		return {
			"name": name or f"FFL {get('transfer_ffl_license')}",
			"attn": get("consumer_name"),
			"line1": get("transfer_ffl_premise_street"), "line2": "",
			"city": get("transfer_ffl_premise_city"), "state": get("transfer_ffl_premise_state"),
			"zip": get("transfer_ffl_premise_zip"), "phone": phone or get("contact_num"),
		}
	if mode == "direct":
		return {
			"name": get("consumer_name"), "attn": "",
			"line1": get("ship_address_line1"), "line2": get("ship_address_line2"),
			"city": get("ship_address_city"), "state": get("ship_address_state"),
			"zip": get("ship_address_zip"), "phone": get("contact_num"),
		}
	return store_ship_override(settings) or company_ship_to(get("company"))


def estimate_freight(lines: list, settings, firearm_order: bool, *, service=None, store=False,
		adult_signature=False, signature=False, insurance=False) -> float:
	"""Flat-rate freight for the chosen ``service`` (or the lane default) +
	per-order fees from the Overview fee table (Settings hold the numbers).
	An ESTIMATE — the invoice's SHPAMT is authoritative once a parcel ships."""
	options = shipping_options_for(lines, settings, firearm_order, store=store)
	pick = next((o for o in options if service is not None and o["code"] == service), None) \
		or next(o for o in options if o["default"])
	rate = float(pick["estimate"]) + delivery_fees(lines, settings, adult_signature=adult_signature,
		signature=signature, insurance=insurance)
	return round(rate, 2)


def delivery_fees(lines: list, settings, *, adult_signature=False, signature=False,
		insurance=False) -> float:
	"""The per-order fees of the chosen delivery options (Settings hold the numbers);
	insurance is a share of the order's value with a floor."""
	g = lambda k, d: float(getattr(settings, k, None) or d)  # noqa: E731
	fee = 0.0
	if adult_signature:
		fee += g("fee_adult_signature", 2.75)
	if signature:
		fee += g("fee_signature", 2.00)
	if insurance:
		total = sum(float(ln.get("unit_cost") or 0) * int(ln.get("wish_qty") or 0) for ln in lines)
		fee += max(g("fee_insurance_min", 2.90), total * g("fee_insurance_pct", 0.95) / 100.0)
	return fee


def estimate_store_freight(lines: list, settings) -> float:
	"""Freight for ONE Sports South order shipped to the shop, per SS's Shipping
	Rules for open accounts / ACH:

	* handguns — free with 3+ handguns or $750+ of handguns on the order, else the
	  handgun 2-Day rate;
	* long guns, ammo and accessories — free at $500+, else a flat $5.

	An order holding both kinds ships as one parcel, so it pays the higher charge.
	SS's third handgun rule (a 180-day average) is not modelled,
	and AR/AK pistols sit in the handgun categories here — SS's
	catalog does not separate them. Thresholds and rates live in Settings. Pure."""
	g = lambda k, d: setting_value(settings, k, d)  # noqa: E731
	# Counter lines carry qty + department; Distributor Order lines carry wish_qty
	# and no department, only the 2Day ship method handguns get.
	qty = lambda ln: float(ln.get("qty") or ln.get("wish_qty") or 0)  # noqa: E731
	value = lambda group: sum(float(ln.get("unit_cost") or 0) * qty(ln) for ln in group)  # noqa: E731
	is_handgun = lambda ln: line_class(ln.get("department")) == "handgun" or ln.get("ship_method") == SHIP_2DAY  # noqa: E731,E501
	handguns = [ln for ln in lines if is_handgun(ln)]
	others = [ln for ln in lines if not is_handgun(ln)]
	charges = []
	if handguns:
		count = sum(qty(ln) for ln in handguns)
		free = count >= g("store_free_handgun_qty", 3) \
			or value(handguns) >= g("store_free_handgun_amount", 750.00)
		charges.append(0.0 if free else g("rate_handgun_2day", 10.95))
	if others:
		free = value(others) >= g("store_free_other_amount", 500.00)
		charges.append(0.0 if free else g("store_other_rate", 5.00))
	return round(max(charges), 2) if charges else 0.0


def store_freight_summary(settings) -> str:
	"""`estimate_store_freight`'s two thresholds in one line, for the hub card. A
	stated 0 makes that class ship free on every order. Pure over ``settings``."""
	g = lambda k, d: setting_value(settings, k, d)  # noqa: E731
	qty, amount = g("store_free_handgun_qty", 3), g("store_free_handgun_amount", 750.00)
	other = g("store_free_other_amount", 500.00)
	handguns = f"handguns free from {qty:g} or ${amount:,.2f}" if qty > 0 and amount > 0 \
		else "handguns free"
	rest = f"everything else free from ${other:,.2f}" if other > 0 else "everything else free"
	return f"{handguns}; {rest}"


def price_drift(detail: list, lines: list, threshold_pct: float) -> list:
	"""Lines whose ORPRCF exceeds our quoted cost by more than ``threshold_pct``."""
	if threshold_pct <= 0:
		return []
	quoted = {ln["sku"]: float(ln.get("unit_cost") or 0) for ln in lines}
	out = []
	for r in detail:
		q = quoted.get(r["sku"])
		f = float(r.get("filled_price") or 0)
		if not q or not f or r.get("filled_qty", 0) <= 0:
			continue
		pct = (f - q) / q * 100.0
		if pct > threshold_pct:
			out.append({"sku": r["sku"], "quoted": q, "filled": f, "pct": pct})
	return out


def normalize_placement(header: int, detail: list, lines: list, *, freight: float = 0.0) -> dict:
	"""Post-Submit GetDetail → the generic place-order shape. ack_qty = ORQTYF
	(SS's fill), unit_cost = ORPRCF when SS quoted one. A line filled 0 is
	CANCELLED (SS never backorders); a partial fill is SHORT."""
	by_sku = {r["sku"]: r for r in detail}
	ack = []
	subtotal = 0.0
	for ln in lines:
		r = by_sku.get(ln["sku"])
		if r is None:
			ack.append({"sku": ln["sku"], "ack_qty": 0, "unit_cost": ln["unit_cost"],
				"item_status": STATUS_UNFILLED, "fully_filled": "N"})
			continue
		filled = int(r.get("filled_qty") or 0)
		cost = float(r.get("filled_price") or 0) or float(r.get("order_price") or 0) or ln["unit_cost"]
		status = "" if filled >= ln["wish_qty"] else (STATUS_UNFILLED if filled == 0 else STATUS_SHORT)
		subtotal += filled * cost
		ack.append({"sku": ln["sku"], "ack_qty": filled, "unit_cost": cost,
			"item_status": status, "fully_filled": "Y" if filled >= ln["wish_qty"] else "N"})
	return {
		"po_num": "", "web_ref": str(header), "hold_status": "", "queued": False,
		"ack_lines": ack,
		"totals": {"subtotal": round(subtotal, 2), "total": round(subtotal + freight, 2)},
		"freight": freight,
		"raw": {"web_order": header, "detail": detail},
	}


def normalize_order_status(web_ref: str, detail: list, invoices: list, tracking: list,
		serials: list) -> dict:
	"""GetDetail + invoices + tracking + serials → the generic poll shape.

	invoiced_qty is only ever > 0 once an invoice exists for the PO: before
	that a filled line is merely acknowledged. A 0-fill after invoicing is a
	CANCELLED line (SS shorts are cancelled, never backordered); before it,
	UNFILLED (pending)."""
	invoiced = bool(invoices)
	invoice_nos = [i.get("S1INV") for i in invoices if i.get("S1INV")]
	tracking_nos = [t.get("TRACKNO") for t in tracking if t.get("TRACKNO")]
	ship_dates = [t.get("SHPDTE") for t in tracking if t.get("SHPDTE")]
	# Flat-rate freight is per ORDER; every parcel row repeats it — take the
	# first non-zero, never the sum.
	freight = next((_f(t.get("SHPAMT")) for t in tracking if _f(t.get("SHPAMT"))), 0.0)
	serials_by_sku: dict[str, list] = {}
	for s in serials:
		if s.get("ITEMNO") and s.get("SERIAL"):
			serials_by_sku.setdefault(s["ITEMNO"], []).append(s["SERIAL"])
	lines = []
	for r in detail:
		filled = int(r.get("filled_qty") or 0)
		ordered = int(r.get("ordered_qty") or 0)
		if filled == 0:
			# SS never backorders — but only an INVOICED 0 is final; before the
			# invoice the fill may simply not be computed yet.
			status = STATUS_CANCELLED if invoiced else STATUS_UNFILLED
		elif filled < ordered:
			status = STATUS_SHORT
		else:
			status = ""
		lines.append({
			"sku": r["sku"],
			"ack_qty": filled,
			"invoiced_qty": filled if invoiced else 0,
			"item_status": status,
			"invoices": invoice_nos if invoiced else [],
			# SS tracking is per PO/invoice, not per line: every shipped line
			# carries the order's parcels (GetPackageContents refines later).
			"tracking_numbers": tracking_nos if invoiced else [],
			"serials": serials_by_sku.get(r["sku"], []),
			# SS bills its current price, not the quote: the filled price is what
			# the invoice charged. None when the detail row carries no price.
			"unit_cost": (_f(r.get("filled_price")) or _f(r.get("order_price")) or None),
			"date_shipped": ship_dates if invoiced else [],
		})
	return {"po_num": "", "web_ref": web_ref, "hold_status": "", "lines": lines,
		"freight": round(freight, 2) if freight else None,
		"raw": {"detail": detail, "invoices": invoices, "tracking": tracking}}


def _f(v) -> float:
	try:
		return float(str(v).replace("$", "").replace(",", "").strip() or 0)
	except ValueError:
		return 0.0


# ------------------------------------------------------------- invoices (pure)

def _yymmdd(value) -> str:
	"""GetHeaders' ORDATE ``260916`` → ``2026-09-16``; anything else → ""."""
	s = str(value or "").strip()
	return f"20{s[0:2]}-{s[2:4]}-{s[4:6]}" if len(s) == 6 and s.isdigit() else ""


FEE_ORDNO = "0"  # SS's service-charge lines belong to no order (live-verified 2026-09-29)
STORED_FEE_PREFIX = "*PROCESSING FEE"


def _is_freight_line(desc: str) -> bool:
	return desc.strip().upper().startswith("*SHIPPING")


def _is_fee_line(row: dict) -> bool:
	"""A GetDetail row that is an SS service charge (the one live-verified shape:
	``*PROCESSING FEE ORDERS LESS THAN $500``, SINUM 64100): ORDNO "0" and not
	freight. Any other ``*`` line stays a normal line — unknown shapes are not
	silently folded into fees. The one place the rule lives; the backfill patch
	(``patches.split_sports_south_invoice_fees``) can only see stored rows, which
	have no ORDNO column, so it matches ``STORED_FEE_PREFIX`` on the description
	instead (the only fee shape ever mirrored)."""
	return (str(row.get("ORDNO") or "").strip() == FEE_ORDNO
		and not _is_freight_line(str(row.get("IDESC") or "")))


_STREET_WORDS = {"drive": "dr", "street": "st", "road": "rd", "avenue": "ave", "boulevard": "blvd",
	"lane": "ln", "court": "ct", "parkway": "pkwy", "highway": "hwy", "circle": "cir",
	"place": "pl", "suite": "ste", "north": "n", "south": "s", "east": "e", "west": "w"}


def _street_key(line: str) -> str:
	"""A street line reduced for comparing two spellings of one address: lower case,
	no punctuation, common words abbreviated ("Premier Drive" = "PREMIER DR.")."""
	import re

	words = re.sub(r"[^a-z0-9 ]", " ", str(line or "").lower()).split()
	return " ".join(_STREET_WORDS.get(w, w) for w in words)


def ship_to_verdict(record: dict, store: dict) -> str:
	"""GetShipTo record vs the shop's address block. Store when the ZIP (5 digits) AND
	street line 1 both match; Drop-ship only for a clearly different place (another
	state, or another 5-digit ZIP); Unknown for everything else — an empty answer, the
	SAME ZIP with a street we cannot match (a suite suffix, another spelling: it may
	well be the shop), or a shop address we cannot read (blank ZIP or street).
	# lazy: line 1 compares after abbreviating common street words; a house that
	# spells the shop's street differently reads Unknown (safe), never Store."""
	zip5 = str(record.get("WEBZIP") or "").strip()[:5]
	street = _street_key(record.get("WEBAD1"))
	from ffl_core.compliance.interstate_tax import normalize_us_state

	state = str(record.get("WEBSTA") or "").strip()
	state = normalize_us_state(state) or state.upper()
	if not (zip5 or street or state):
		return lanes.DEST_UNKNOWN
	store_zip, store_street = str(store.get("zip") or "").strip()[:5], _street_key(store.get("line1"))
	if not (len(store_zip) == 5 and store_street):
		return lanes.DEST_UNKNOWN
	store_state = str(store.get("state") or "").strip()
	store_state = normalize_us_state(store_state) or store_state.upper()
	if state and store_state and state != store_state:
		return lanes.DEST_DROPSHIP
	if len(zip5) == 5 and zip5 != store_zip:
		return lanes.DEST_DROPSHIP
	if zip5 == store_zip and street == store_street:
		return lanes.DEST_STORE
	return lanes.DEST_UNKNOWN


def _tracking_numbers(rows) -> list:
	out = []
	for t in rows or []:
		no = str(t.get("TRACKNO") or "").strip()
		if no and no not in out:
			out.append(no)
	return out


def normalize_invoice(header: dict, detail: list, tracking: list, serials: list | None = None) -> dict:
	"""invoices.asmx GetByDate header + GetDetail rows + GetTrackingForInvoice rows
	→ the ``distributor.invoices`` shape. Freight is its own ``*SHIPPING ...*``
	detail line (live-verified); service-charge lines (``_is_fee_line``) are summed
	into ``fees`` rather than listed as merchandise. SGROS is the billed amount
	(SPRC plus SS's surcharges), so INVGS = sum(SGROS)."""
	from ffl_integrations.sports_south.api_client import _to_float, _to_int

	# GetSerialsForInvoice rows (ITEMNO, SERIAL) → per item number, handed to that
	# item's lines in order, each line taking as many as it bills.
	pool: dict = {}
	for s in serials or []:
		if str(s.get("SERIAL") or "").strip():
			pool.setdefault(str(s.get("ITEMNO") or "").strip(), []).append(str(s["SERIAL"]).strip())
	lines, freight, fees = [], 0.0, 0.0
	for r in detail or []:
		desc = str(r.get("IDESC") or "").strip()
		amount = _to_float(r.get("SGROS"))
		if _is_freight_line(desc):
			freight += amount
			continue
		if _is_fee_line(r):
			fees += amount
			continue
		sku, qty = str(r.get("SINUM") or "").strip(), _to_int(r.get("SQTY"))
		take = max(qty, 0)  # a credit/negative line takes no serials (a [:-1] slice would take all but one)
		mine, pool[sku] = pool.get(sku, [])[:take], pool.get(sku, [])[take:]
		lines.append({"sku": sku, "description": desc, "qty": qty,
			"unit_cost": _to_float(r.get("SPRC")), "amount": amount, "serials": ",".join(mine)})
	tracking_numbers = _tracking_numbers(tracking)
	order_refs = {str(r.get("ORDNO") or "").strip() for r in detail or []} - {"", FEE_ORDNO}
	return {
		"invoice_number": str(header.get("S1INV") or "").strip(),
		"invoice_date": str(header.get("S1DATE") or "")[:10] or None,
		"po_number": str(header.get("PONBR") or "").strip(),
		# Every SS order this invoice bills (one invoice can close several).
		"order_ref": ",".join(sorted(order_refs)),
		"account": "",
		"total": _to_float(header.get("INVGS")),
		"freight": round(freight, 2),
		"fees": round(fees, 2),
		"tracking_numbers": tracking_numbers,
		"lines": lines,
	}
