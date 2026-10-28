"""RSR provider — DistributorProvider #1, bound to the P1 RSR Direct Connect
client (``rsr/api_client.py``). All RSR ordering hard rules live in
``plan_fulfillment``:

* **Firearms and accessories never mix** — a firearm and an accessory never ride the same RSR order.
* **ShipMethod** — handguns forced 2Day (RSR carrier rule); everything else the
  cheapest compliant lane (Ground), which also satisfies ammo/restricted=Ground.
* **>400 SKUs (H4)** — a stocking cart over RSR's per-order line cap chunks into
  several orders.
* **Classification divergence** — if RSR's ffl_required disagrees with our Item.is_firearm,
  STOP for a human (``PlanningError``) rather than guess a lane.

`plan_fulfillment`, `build_place_order_payload`, `normalize_place_response` and
`normalize_order_status` are pure (no DB / no HTTP) so the split matrix and the
order-status mapping are unit-testable without a site. ``frappe`` and the RSR
client are imported lazily inside the methods that actually reach out.
"""
from __future__ import annotations

from typing import Optional

from ffl_integrations.distributor import lanes
from ffl_integrations.distributor.base import DistributorProvider
from ffl_integrations.distributor.remote_catalog_mixin import RemoteCatalogMixin, remote_normalised
from ffl_integrations.distributor.util import setting_value

# RSR handgun departments — the only firearms RSR force-upgrades to 2Day.
HANDGUN_DEPARTMENTS = frozenset({"01", "02"})
# RSR place-order line cap ("≤400 SKU"). H4 chunks a big stocking cart.
# TODO(go-live): confirm the exact per-order SKU cap against the Direct Connect manual.
MAX_ORDER_SKUS = 400

SHIP_GROUND = "Grnd"
SHIP_2DAY = "2Day"

RSR_CAPABILITIES = frozenset({
	"search", "catalog", "check_availability", "place_order",
	"dropship", "fds", "to_store", "update_ffl", "verify_dealer",
})


DISTRIBUTOR_CODE = "rsr"


def _parse_service_ts(value):
	"""Service ISO timestamp (UTC) → naive datetime on the SITE's clock,
	comparable with frappe.utils.now_datetime().

	Delegates: the implementation moved to `remote_catalog.parse_service_ts` (the
	module that owns /v1/catalog wire shapes) when the Catalog Service page needed
	the same conversion. Kept as a lazy delegate rather than a module-level alias
	so this module stays import-light (see the module docstring) — remote_catalog
	imports frappe at module scope."""
	from ffl_integrations.distributor.remote_catalog import parse_service_ts

	return parse_service_ts(value)


# Kept as a name: tests and the console still import it from here.
_remote_normalised = remote_normalised


class RSRProvider(RemoteCatalogMixin, DistributorProvider):
	code = "rsr"
	display_name = "RSR"
	settings_doctype = "RSR Settings"
	max_order_lines = MAX_ORDER_SKUS

	def __init__(self, client=None):
		# Injectable so tests never build a real RSRClient (which reads settings).
		self._client_obj = client

	def capabilities(self) -> set:
		return set(RSR_CAPABILITIES)

	# ---------------------------------------------------------- pure: planning

	def ship_method_for(self, line: dict) -> str:
		"""Handguns are forced 2Day (RSR carrier rule); everything else takes Ground,
		the cheapest compliant lane and the required one for ammunition / restricted
		items (they never upgrade)."""
		if line.get("ship_method"):
			return line["ship_method"]
		return SHIP_2DAY if str(line.get("department") or "").strip() in HANDGUN_DEPARTMENTS \
			else SHIP_GROUND

	# ---------------------------------------------------------- catalog / search

	def blocked_states(self, sku: str) -> list:
		"""States where RSR's ship-restrictions feed blocks drop-ship of this SKU
		somewhere — the quote panel's red line.

		OPTIONAL provider method (the feed_heartbeat shape):
		the generic quote asks for it by name and quotes ``[]`` from a provider
		that has no restriction feed. Deliberately NOT part of the normalised
		catalog row — it is one extra read per SKU, fine on a quote, ruinous
		inside the 60k-row bulk normaliser."""
		from ffl_integrations.rsr import restrictions

		return restrictions.blocked_states_for(sku)

	def check_availability(self, lines: list) -> list:
		"""Pre-order availability gate. Deliberately ALWAYS the dealer account:
		it answers "does RSR have N of this at what cost" — the fulfillment
		account cannot even ask without a geolocatable ship-to (97) and, for
		firearms, a ShipFFL (84), constraints that belong to PLACEMENT, where
		build_place_order_payload supplies them. Dealer/fulfillment pricing is
		the same feed; the fulfillment-specific rejections surface at
		place-order, whose per-line ItemStatus the operator already sees."""
		skus = [ln["sku"] for ln in lines if ln.get("sku")]
		wished = {ln["sku"]: int(ln.get("qty") or 0) for ln in lines if ln.get("sku")}
		if not skus:
			return []
		live = self._get_client().check_catalog(skus, account="dealer")
		# Live check-catalog rows key on PartNum and OnHand, carry money with a
		# "$" prefix or plain ("Price"/"CCost", both live-verified on the
		# MGMPI571BLK capture), and answer misses IN-BAND (per-item StatusCode
		# 02/03) — a non-00 row is unavailable no matter what OnHand says.
		by_sku = {str(r.get("PartNum") or r.get("SKU") or r.get("Sku") or ""): r
			for r in live}
		out = []
		for sku in skus:
			r = by_sku.get(sku, {})
			code = str(r.get("StatusCode") or "").strip()
			orderable = code in ("", "00", "0")
			qty = int(r.get("OnHand") or r.get("Quantity") or r.get("Qty") or 0)
			cost = _money(r.get("Price") or r.get("CCost") or r.get("Cost") or 0)
			out.append({
				"sku": sku,
				"qty": qty,
				"cost": cost,
				"available": orderable and qty >= wished.get(sku, 0) and qty > 0,
				# Blocked outright (status code), whatever OnHand says.
				"orderable": orderable,
			})
		return out

	# ---------------------------------------------------------- ordering

	def place_order(self, do_name: str) -> dict:
		import frappe
		do = frappe.get_doc("Distributor Order", do_name)
		return self._place(do, self._get_client())

	def _place(self, do, client) -> dict:
		from ffl_integrations.rsr.api_client import RSRConfigError

		# place-order's Email is REQUIRED (StatusCode 88) — fail closed before
		# the wire, as a config fault (self-heals once RSR Settings is filled).
		email = (client.config.order_email or "").strip()
		if not email:
			raise RSRConfigError(
				"RSR Settings: order_email is not configured — place-order "
				"requires the shop's email (manual: StatusCode 88).")
		payload = build_place_order_payload(do, email)
		resp = client.place_order_idempotent(do.name, payload, do.account)
		return normalize_place_response(resp)

	def get_order_status(self, do_name: str) -> dict:
		import frappe
		do = frappe.get_doc("Distributor Order", do_name)
		resp = self._get_client().check_order(do.name, do.account)
		return normalize_order_status(resp)

	def list_invoices(self, since: str, known=()) -> list:
		"""Every invoice shipped on/after ``since`` on each CONFIGURED account
		(dealer = stocking, fulfillment = drop-ship), normalised for
		``distributor.invoices`` — whoever placed the order. ``known`` numbers skip
		their detail call. One account or one invoice failing is logged and
		skipped (retried next run, it is not ``known`` yet); the rest still land."""
		import frappe

		s = frappe.get_cached_doc("RSR Settings")
		if not s.api_enabled:
			return []
		client = self._get_client()
		known = set(known)
		out = []
		for account, number in (("dealer", s.dealer_account), ("fulfillment", s.fulfillment_account)):
			if not number:
				continue
			try:
				shipments = client.get_shipments(since, account)
			except Exception:  # noqa: BLE001 — the other account still syncs
				frappe.log_error(title=f"RSR get-shipments failed ({account})",
					message=frappe.get_traceback())
				continue
			for raw in shipments:
				no = str((raw.get("InvoiceNumber") if isinstance(raw, dict) else raw) or "").strip()
				if not no or no.lower() == "none" or no in known:
					continue
				known.add(no)
				try:
					out.append(normalize_shipment(no, client.get_shipment(no, account), account))
				except Exception:  # noqa: BLE001 — skip; retried next run
					frappe.log_error(title=f"RSR get-shipment failed: {no}",
						message=frappe.get_traceback())
		return out

	def invoice_tracking(self, invoice: dict) -> list:
		import frappe

		if not frappe.get_cached_doc("RSR Settings").api_enabled:
			return []
		account = invoice.get("account") or "dealer"
		no = invoice["invoice_number"]
		return normalize_shipment(no, self._get_client().get_shipment(no, account), account)[
			"tracking_numbers"]

	def estimate_order_freight(self, lines: list, mode: str, options=None) -> Optional[float]:
		import frappe

		return estimate_freight(lines, mode, frappe.get_cached_doc("RSR Settings"))

	def account_number(self, account: str) -> str:
		import frappe
		s = frappe.get_cached_doc(self.settings_doctype)
		field = "fulfillment_account" if account == "fulfillment" else "dealer_account"
		return (s.get(field) or "").strip()

	def hub_summary(self) -> dict:
		import frappe

		s = frappe.get_cached_doc(self.settings_doctype)
		return {"live_orders": bool(s.api_enabled), "ship_to_store": store_freight_summary(s)}

	def verify_transfer_dealer(self, ffl: str) -> Optional[dict]:
		"""Per-FFL verification via dealer-accept-transfers. The old
		get-transfer-dealers path was a false verifier: that endpoint has no
		server-side filter (SearchZip is silently ignored, live-verified), so
		``dealers[0]`` "verified" every input as the alphabetically-first
		dealer in RSR's 15k-row list.

		Truthiness IS the verdict: None unless RSR both has the FFL on file
		AND the holder accepts transfers — the two ways this dealer cannot
		receive an FDS shipment. A dict for a dealer who would refuse the box
		would let the first ``if result:`` caller ship into a refusal."""
		resp = self._get_client().dealer_accept_transfers(ffl, account="fulfillment")
		if not resp.get("on_file", True):
			return None
		if str(resp.get("AcceptTransfers") or "").strip().upper() != "Y":
			return None
		return {"ffl": str(resp.get("FFL") or ffl),
			"accepts_transfers": True, "raw": resp}

	def update_order_ffl(self, do_name: str, ffl: str) -> Optional[dict]:
		import frappe
		do = frappe.get_doc("Distributor Order", do_name)
		# The endpoint is keyed by WebRef (place-order's confirmation number),
		# not our PONum — an order that never placed has nothing to update.
		if not (do.web_ref or "").strip():
			frappe.throw(frappe._(
				"Distributor Order {0} has no WebRef — the order was never "
				"placed at RSR, so there is no FDS hold to release.").format(do_name))
		return self._get_client().update_order_ffl(
			do.web_ref, ffl, account="fulfillment")

	# ---------------------------------------------------------- P3 listing surface
	# OPTIONAL provider methods, same status as verify_transfer_dealer /
	# update_order_ffl: the frozen ABC is untouched and the generic layer
	# probes for them, so a phone-order distributor simply doesn't offer them.

	def promote_to_item(self, sku: str, opts=None) -> dict:
		"""Delegate to the RSR promoter, which already writes MAP/MSRP/dealer price/
		brand/images/listing_status (DIVERGENCE-2 — do not duplicate any of it)."""
		from ffl_integrations.rsr.promote import _promote_to_item as _promote

		payload = dict(opts or {})
		payload["rsr_stock_number"] = sku
		return _promote(payload)

	def connection_test(self) -> dict:
		"""Hub Test Connection: one check per live dependency, each guarded so a
		broken leg reports instead of aborting the others. Catalog = Data Service
		health; Direct Connect = a real authenticated check_catalog round trip
		per configured account (read-only at RSR's end). A Local-catalog site
		also proves its FTPS feed login."""
		import frappe

		from ffl_integrations.catalog_local import is_local

		checks = [self.catalog_health_check()]
		if is_local():
			checks.append(self.feed_login_check())

		s = frappe.get_cached_doc("RSR Settings")
		if not s.api_enabled:
			checks.append({
				"label": "Direct Connect API", "ok": None,
				"detail": "Direct Connect disabled in RSR Settings."})
			return {"checks": checks}

		accounts = [a for a, acct in (
			("dealer", s.dealer_account), ("fulfillment", s.fulfillment_account)) if acct]
		if not accounts:
			checks.append({
				"label": "Direct Connect API", "ok": False,
				"detail": "Enabled but no account number configured."})
		if not (getattr(s, "order_email", "") or "").strip():
			# Surface the place-order prerequisite at CONFIGURATION time —
			# without this, the first missed field is discovered by a queued
			# order landing in Error (placement fails closed on empty email).
			checks.append({
				"label": "Order email", "ok": False,
				"detail": "order_email is not set — place-order requires the "
					"shop's email and placement fails closed without it."})
		for account in accounts:
			label = f"Direct Connect ({account} account)"
			try:
				from ffl_integrations.rsr.api_client import RSRClient

				# probe_connection carries the account-appropriate shape (the
				# fulfillment account demands a geolocatable ship context on
				# every check-catalog) and never raises on a business code, so
				# we classify here: an authenticated batch probe answers with
				# NO top-level StatusCode (the bogus SKU's "not carried" stays
				# per-item); a bad login answers 99, bad POS 96.
				resp = RSRClient.from_settings().probe_connection(account=account)
				code = str(resp.get("StatusCode") or "").strip()
				if code in ("", "00", "0"):
					checks.append({"label": label, "ok": True, "detail": "Authenticated."})
				else:
					msg = str(resp.get("StatusMssg") or "").strip() or f"StatusCode {code}"
					checks.append({"label": label, "ok": False, "detail": msg})
			except Exception as exc:
				checks.append({"label": label, "ok": False, "detail": str(exc)})
		return {"checks": checks}

	@staticmethod
	def feed_login_check() -> dict:
		"""The local catalog's FTPS feed: log in and list the feed directory,
		download nothing. The detail never carries a credential."""
		from ffl_integrations.catalog_local.drivers import rsr as feed

		label = "Catalog feed (FTPS)"
		try:
			creds = feed.credentials()
			if creds is None:
				return {"label": label, "ok": False, "detail": "The catalog is Local, but RSR "
					"Settings › Catalog feed has no FTP username and password."}
			info = feed.FtpsTransport(creds).probe()
		except Exception as exc:  # noqa: BLE001 — one leg reports, the others still run
			return {"label": label, "ok": False, "detail": str(exc)}
		return {"label": label, "ok": True,
			"detail": f"Logged in; {info['files']} files in {info['directory']}/."}

	# ---------------------------------------------------------- plumbing

	def _get_client(self):
		if self._client_obj is None:
			from ffl_integrations.rsr.api_client import RSRClient
			self._client_obj = RSRClient.from_settings()
		return self._client_obj


def get_provider() -> RSRProvider:
	"""Zero-arg factory registered on the ``distributor_providers`` hook."""
	return RSRProvider()


# ------------------------------------------------------------- pure helpers

def build_place_order_payload(do, order_email: str = "") -> dict:
	"""RSR place-order body from a Distributor Order doc. Pure over the doc's
	attributes so it unit-tests without a site.

	Live-verified contract (2026-08-18, test env): line keys are PartNum/WishQty
	(the old SKU/Quantity shape is not recognised — 93 "No valid items
	submitted"), Email is required (88), and the drop-ship fields are
	mode-dependent:

	* Dropship Accessory — Storename (end consumer) + ContactNum + the
	  consumer's ShipAddress/City/State/Zip; NEVER ShipFFL (84 "FFL provided
	  with an accessories only order").
	* Dropship Firearm (FDS) — Storename + ContactNum + ShipFFL; NEVER an
	  address: RSR ships to the transfer dealer's ON-FILE premises and answers
	  a supplied address with per-field "ignored" warnings.
	* Stocking / Procure To Store — no extra fields (ships to the dealer's
	  address on file).

	FillOrKill goes out only as "Y": the manual documents no other value, and
	omitting it IS the "off" state."""
	items = []
	for ln in (do.lines or []):
		get = ln.get if isinstance(ln, dict) else lambda k: getattr(ln, k, None)
		items.append({
			"PartNum": str(get("sku") or ""),
			"WishQty": int(get("wish_qty") or 0),
			"ShipMethod": get("ship_method") or SHIP_GROUND,
		})
	payload: dict = {
		"PONum": do.name,
		"Email": order_email,
		"Items": items,
	}
	if getattr(do, "fill_or_kill", False):
		payload["FillOrKill"] = "Y"

	mode = lanes.mode_of(getattr(do, "order_type", "") or "")
	if lanes.ships_to_consumer(mode):
		payload["Storename"] = (getattr(do, "consumer_name", "") or "").strip()
		payload["ContactNum"] = (getattr(do, "contact_num", "") or "").strip()
		if mode == lanes.FDS:
			payload["ShipFFL"] = (getattr(do, "transfer_ffl_license", "") or "").strip()
		else:  # direct — accessories to the consumer's address
			payload["ShipAddress"] = (getattr(do, "ship_address_line1", "") or "").strip()
			line2 = (getattr(do, "ship_address_line2", "") or "").strip()
			if line2:
				payload["ShipAddress2"] = line2
			payload["ShipCity"] = (getattr(do, "ship_address_city", "") or "").strip()
			payload["ShipState"] = (getattr(do, "ship_address_state", "") or "").strip()
			payload["ShipZip"] = (getattr(do, "ship_address_zip", "") or "").strip()
	return payload


def estimate_freight(lines: list, mode: str, settings) -> float:
	"""RSR freight for ONE order, per RSR's published freight policy (48 states):

	* **Drop-ship** (direct / FDS) — the Fulfillment Flat Rate: Ground $10, 2Day
	  $15, whatever the order value.
	* **Shipped to the shop** (to_store / stocking) — free over $500 of
	  merchandise, else the same Ground / 2Day rates. An account with free freight
	  to the store (the RSR Settings switch) pays nothing.
	* An order that needs two ship methods pays the higher (a handgun forces 2Day
	  on the whole order); a firearm adds one adult-signature fee per order — on a
	  free-freight store order too, as RSR's policy says.

	Alaska/Hawaii, PO boxes and overnight upgrades are not modelled — it is an
	estimate; the order response's Shipping replaces it. Pure over ``settings``."""
	g = lambda k, d: setting_value(settings, k, d)  # noqa: E731
	firearm = any(ln.get("is_firearm") for ln in lines)
	signature = g("fee_signature", 5.00) if firearm else 0.0
	rate = g("rate_2day", 15.00) if any(ln.get("ship_method") == SHIP_2DAY for ln in lines) \
		else g("rate_ground", 10.00)
	if lanes.ships_to_consumer(mode):
		return round(rate + signature, 2)
	if getattr(settings, "store_free_freight", 0):
		return 0.0
	merchandise = sum(float(ln.get("unit_cost") or 0) * float(ln.get("qty") or 0) for ln in lines)
	if merchandise > g("free_freight_threshold", 500.00):
		return round(signature, 2)
	return round(rate + signature, 2)


def store_freight_summary(settings) -> str:
	"""`estimate_freight`'s ship-to-store rule in words, for the hub card: "free"
	(the free-freight switch, or a stated 0 threshold) or "free over $X" (the
	Ground / 2Day rates apply at or below it). Pure over ``settings``."""
	if getattr(settings, "store_free_freight", 0):
		return "free"
	threshold = setting_value(settings, "free_freight_threshold", 500.00)
	return f"free over ${threshold:,.2f}" if threshold > 0 else "free"


def _money(value) -> float:
	"""RSR money strings arrive dollar-signed ("$13.75", live-verified) and may
	carry thousands commas. Strip the decoration; a genuinely unparseable
	value raises (money is the one place a loud failure beats a silent zero) —
	as a terminal RSRError, so it reports through the same channel as every
	other per-order contract fault instead of a bare 500."""
	from ffl_integrations.rsr.api_client import RSRError

	s = str(value if value is not None else 0).replace("$", "").replace(",", "").strip()
	try:
		return float(s or 0)
	except ValueError as exc:
		raise RSRError(
			f"unparseable RSR money value {value!r}", terminal=True) from exc


def normalize_place_response(resp: dict) -> dict:
	"""RSR place-order response → generic shape the DO controller writes back.
	Line identity is PartNum (present in live traffic even though the manual
	only documents UPCcode); UPCcode rides along zero-padded to 13 digits, so
	it is carried raw for provenance, never used as the match key."""
	resp = resp or {}
	lines = []
	for ln in _resp_lines(resp):
		lines.append({
			"sku": str(ln.get("PartNum") or ln.get("SKU") or ln.get("Sku")
				or ln.get("sku") or ""),
			"upc": str(ln.get("UPCcode") or ln.get("UPC") or ""),
			"ack_qty": int(ln.get("AckQty") or ln.get("AcceptedQty") or 0),
			"unit_cost": _money(ln.get("Cost") or ln.get("Price") or 0),
			"item_status": str(ln.get("ItemStatus") or ln.get("Status") or ""),
			"fully_filled": str(ln.get("FullyFilled") or ""),
		})
	return {
		"po_num": str(resp.get("PONum") or resp.get("WebOrderNumber") or ""),
		"web_ref": str(resp.get("WebRef") or resp.get("ConfirmResp")
			or resp.get("WebReference") or ""),
		# HoldStatus is authoritative; the FDSHold Y/N flag contradicts it in
		# live traffic (an order holding as "FDS Hold" answered FDSHold "N"),
		# so it is deliberately not read.
		"hold_status": str(resp.get("HoldStatus") or resp.get("Hold") or ""),
		# 60 = accepted but queued for after RSR's maintenance window: no ack
		# lines yet; the poller picks the order up like any other placement.
		"queued": str(resp.get("StatusCode") or "").strip() == "60",
		"ack_lines": lines,
		"totals": {"subtotal": _money(resp.get("Subtotal") or 0),
			"total": _money(resp.get("Total") or 0)},
		# The distributor's shipping charge — the ONLY source of the PI freight line
		# Unset in the response ⇒ 0 ⇒ no freight line + a PI remark note
		# (accounting.build_pi_payload); never a silent zero pretending "no shipping".
		"freight": _money(resp.get("Shipping") or resp.get("Freight")
			or resp.get("FreightCharge") or 0),
		"raw": resp,
	}


# RSR invoices before the parcel leaves and fills check-order's CSVs with
# placeholders meanwhile: Invoiced 1, DateShipped/TrackingNum "Pending,",
# SerialNum "SDY7734,SubTooHigh," (live 2026-09-17, DO-YYMM-0004); the real
# number replaces "Pending" once it ships. Never store them as values.
RSR_PLACEHOLDERS = {"PENDING", "SUBTOOHIGH"}


def _invoiced_qty(ln: dict) -> int:
	"""Shipped quantity from a check-order row. Live shapes: unshipped Invoiced
	int 0 / InvoiceQty ""; shipped Invoiced int 1 / InvoiceQty "1,". Prefer a
	numeric ``Invoiced``, else sum the ``InvoiceQty`` per-invoice CSV (manual
	p.99: "comma delimited list of all the item quantities on each invoice").
	Non-numeric in BOTH → 0 with the raw kept on the DO.

	An invoiced row still awaiting shipment (placeholder DateShipped/TrackingNum,
	nothing real in either) counts 0: invoiced is not shipped, and counting it
	would complete the order before RSR has a tracking number."""
	if _awaiting_shipment(ln):
		return 0
	raw = str(ln.get("Invoiced") if ln.get("Invoiced") is not None else "").strip()
	if raw.lstrip("-").isdigit():
		return int(raw)
	parts = [p.strip() for p in str(ln.get("InvoiceQty") or "").split(",") if p.strip()]
	return sum(int(p) for p in parts if p.lstrip("-").isdigit())


def normalize_order_status(resp: dict) -> dict:
	"""RSR check-order response → generic order status the poller applies to a DO.

	No ack_qty here on purpose: check-order carries no AckQty (live-verified —
	its quantity fields are Invoiced/InvoiceQty/ASNQty). Emitting a defaulted 0
	would let every poll wipe the ack the placement wrote; compute_do_updates
	treats the missing key as "carry the existing value forward"."""
	resp = resp or {}
	lines = []
	for ln in _resp_lines(resp):
		lines.append({
			"sku": str(ln.get("PartNum") or ln.get("SKU") or ln.get("Sku")
				or ln.get("sku") or ""),
			"invoiced_qty": _invoiced_qty(ln),
			"item_status": str(ln.get("ItemStatus") or ln.get("Status") or ""),
			"invoices": _values(ln.get("Invoices") or ln.get("InvoiceNums")),
			"tracking_numbers": _values(ln.get("TrackingNum") or ln.get("Tracking")),
			"serials": _values(ln.get("SerialNum") or ln.get("Serials")),
			"date_shipped": _values(ln.get("DateShipped")),
			# What the invoice charged per unit, when check-order carries it (the
			# placement echo does; the manual is silent for check-order). None =
			# unknown, so the poll keeps whatever the order line already holds.
			"unit_cost": _money_or_none(ln.get("Cost")),
		})
	return {
		"po_num": str(resp.get("PONum") or ""),
		"web_ref": str(resp.get("WebRef") or ""),
		"hold_status": str(resp.get("HoldStatus") or resp.get("Hold") or ""),
		"lines": lines,
		"freight": _money_or_none(resp.get("FreightCharge") or resp.get("Shipping")),
		"raw": resp,
	}


def _money_or_none(value):
	"""'$22.99' → 22.99; blank / absent / unparseable → None (unknown, never 0)."""
	s = str(value or "").replace("$", "").replace(",", "").strip()
	try:
		return float(s) if s else None
	except ValueError:
		return None


def _resp_lines(resp: dict) -> list:
	for key in ("Items", "Lines", "OrderLines", "Products"):
		v = resp.get(key)
		if isinstance(v, list):
			return v
	return []


def _csv(value) -> list:
	if value is None:
		return []
	if isinstance(value, (list, tuple)):
		return [str(v).strip() for v in value if str(v).strip()]
	return [s.strip() for s in str(value).split(",") if s.strip()]


def _values(value) -> list:
	"""``_csv`` minus RSR's placeholders."""
	return [v for v in _csv(value) if v.upper() not in RSR_PLACEHOLDERS]


def _awaiting_shipment(ln: dict) -> bool:
	# lazy: row-level — a two-invoice row with one parcel out counts all of
	# Invoiced as shipped; pair InvoiceQty with DateShipped per invoice if RSR
	# ever splits a line across invoices.
	tracking = ln.get("TrackingNum") or ln.get("Tracking")
	raw = _csv(ln.get("DateShipped")) + _csv(tracking)
	return (any(v.upper() in RSR_PLACEHOLDERS for v in raw)
		and not (_values(ln.get("DateShipped")) or _values(tracking)))


def _slots(value) -> list:
	"""A per-invoice CSV split BY POSITION: blanks kept (an invoice with no
	tracking yet is an empty slot), only the trailing comma's empty tail
	dropped. ``",222,"`` → ``["", "222"]``; ``_csv`` would shift it to ``["222"]``."""
	parts = [p.strip() for p in str(value if value is not None else "").split(",")]
	if len(parts) > 1 and parts[-1] == "":
		parts.pop()
	return parts


def normalize_shipment(invoice_no: str, resp: dict, account: str) -> dict:
	"""get-shipment → the ``distributor.invoices`` shape. The response carries no
	PO and no header totals, only check-order-shaped Items whose Invoices /
	InvoiceQty / DateShipped / TrackingNum are parallel per-invoice CSVs
	(live-verified 2026-09-29: ``"4022784237,"``), read by position; an invoice can
	occupy several slots of one item (one per serialized unit), all summed. An item
	not on this invoice (other invoice, or not shipped yet) is skipped. No freight
	figure is exposed here, so ``freight`` is None (unknown)."""
	def at(values, i):
		v = values[i] if i < len(values) else ""
		return "" if v.upper() in RSR_PLACEHOLDERS else v

	lines, tracking, dates = [], [], []
	for ln in _resp_lines(resp or {}):
		invoices = _slots(ln.get("Invoices"))
		# One slot per shipment entry, and a serialized item gets one slot PER UNIT:
		# two NFA units on one invoice read "X,X," / InvoiceQty "1,1," / SerialNum
		# "A,B," (live-verified 2026-09-29, invoice 4022814113). Sum every slot.
		slots = [i for i, v in enumerate(invoices) if v == invoice_no]
		if not slots:
			continue
		single = len({v for v in invoices if v}) == 1
		qty_slots = _slots(ln.get("InvoiceQty"))
		qtys = [at(qty_slots, i) for i in slots]
		if not any(qtys) and single:
			qtys = [str(ln.get("Invoiced") or 0)]
		qty = sum(int(float(q)) for q in qtys if q.replace(".", "", 1).isdigit())
		cost = _money_or_none(ln.get("Cost")) or 0.0
		track_slots, date_slots = _slots(ln.get("TrackingNum")), _slots(ln.get("DateShipped"))
		tracks = []
		for i in slots:
			track = at(track_slots, i)
			if track and track not in tracks:
				tracks.append(track)
			date = at(date_slots, i)
			if len(date) == 8 and date.isdigit():
				dates.append(f"{date[:4]}-{date[4:6]}-{date[6:]}")
		tracking.extend(t for t in tracks if t not in tracking)
		# Every serial when the row bills only this invoice; else the ones in its slots.
		serials = _values(ln.get("SerialNum")) if single else \
			[v for v in (at(_slots(ln.get("SerialNum")), i) for i in slots) if v]
		lines.append({
			"sku": str(ln.get("PartNum") or ln.get("SKU") or "").strip(),
			"description": f"UPC {ln['UPCcode']}" if str(ln.get("UPCcode") or "").strip() else "",
			"upc": str(ln.get("UPCcode") or "").strip(),
			"qty": qty, "unit_cost": cost, "amount": round(qty * cost, 2),
			"tracking_number": ",".join(tracks),
			"serials": ",".join(serials),
		})
	return {
		"invoice_number": invoice_no,
		"invoice_date": min(dates) if dates else None,
		"po_number": "", "order_ref": "", "account": account,
		"total": round(sum(ln["amount"] for ln in lines), 2),
		"freight": None,
		"tracking_numbers": tracking,
		"lines": lines,
	}
