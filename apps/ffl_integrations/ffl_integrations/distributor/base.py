"""Distributor abstraction — the provider ABC and the two frozen wire shapes
(`NormalizedRow`, `OrderPlan`) every provider maps to/from.

The generic layer (``distributor/api.py``, ``tasks.py``, ``receive_bridge.py``)
speaks ONLY this contract and the provider registry; it never imports a concrete
provider's module path. RSR is provider #1 (``rsr_provider.RSRProvider``); a
"manual"/human-order distributor can omit ``place_order`` from ``capabilities`` and
the generic layer skips the automated-placement path for it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal, Optional, TypedDict

from ffl_integrations.distributor import lanes
from ffl_integrations.distributor.util import TO_STORE_ON_REFUSAL, chunk


class NormalizedRow(TypedDict):
	"""A catalog row normalised across providers (RSR Catalog Item → this)."""

	sku: str
	upc: str
	manufacturer: str
	model: str
	department: str
	cost: float             # dealer/base cost (RSR dealer_price)
	map: float
	msrp: float
	qty: int
	ffl_required: bool      # provider's firearm flag — authoritative for split
	dropship_blocked: bool  # loader field 69
	vendor_approval_required: bool  # loader field 77
	# NB: no restricted_states. Per-SKU state answers are a separate
	# per-item read (optional provider method `blocked_states`), not bulk data.
	image_url: str
	discontinued: bool      # P3: provider dropped the product (≠ qty 0 / out of stock)


class OrderPlan(TypedDict):
	"""One placeable order after split/routing. `plan_fulfillment` returns a list
	of these — a firearm cart + accessory cart becomes ≥2 plans (firearms and accessories never share an order), and a
	>400-SKU cart chunks into several (H4)."""

	account: Literal["dealer", "fulfillment"]
	order_type: str                   # one of lanes.ORDER_TYPES
	fill_or_kill: bool
	ship_to: Optional[dict]           # {line1,line2,city,state,zip} — accessory drop-ship
	ship_ffl: Optional[str]           # FDS ShipFFL license
	transfer_dealer: Optional[dict]   # {license, premise{...}, ffl_record}
	lines: list                       # [{sku, item_code, qty, ship_method}]


class PlanningError(RuntimeError):
	"""`plan_fulfillment` cannot produce a safe plan and the order must STOP for a
	human (never silently guess a lane). Raised on the firearm-classification
	divergence (provider ffl_required disagrees with our Item.is_firearm)."""


class LaneNotSupported(PlanningError):
	"""The order asks for a lane this house does not run (see its ``capabilities``)."""


class DistributorProvider(ABC):
	"""One distributor integration. Concrete providers are registered as zero-arg
	factories on the ``distributor_providers`` hook and keyed by ``code``."""

	code: str = ""
	# Single doctype holding this provider's credentials/config (e.g. "RSR
	# Settings"). The Distributors hub shows an "Account & shipping" jump when set.
	settings_doctype: str = ""

	# The house's per-order line cap (0 = none). The plan template chunks a big cart
	# into several orders on it.
	max_order_lines: int = 0
	# Shown to staff, and named in a planning refusal.
	display_name: str = ""

	@abstractmethod
	def capabilities(self) -> set:
		"""Subset of {"search","catalog","check_availability","place_order",
		"dropship","fds","to_store","update_ffl","verify_dealer"} this provider
		supports. The three lane capabilities decide which lanes it runs:
		``dropship`` (accessories to the buyer), ``fds`` (a firearm to the transfer
		dealer) and ``to_store`` (stocking and procure-to-store) — see
		:mod:`.lanes`."""

	@abstractmethod
	def search(self, q: str, limit: int = 10) -> list:
		"""Typeahead rows: [{sku,item_name,cost,map,msrp,qty,ffl_required}]."""

	@abstractmethod
	def catalog_row(self, sku: str) -> Optional[NormalizedRow]:
		"""Full normalised catalog row for one SKU, or None if unknown."""

	def list_invoices(self, since: str, known=()) -> list:
		"""Every invoice on the house's account dated ``since``..today (orders
		placed anywhere), in the ``distributor.invoices`` shape; ``known`` invoice
		numbers may be skipped. Default: the house cannot list invoices."""
		return []

	def list_open_orders(self) -> list:
		"""Every order header on the house's account: [{order_ref, po_number,
		order_date, status}]. Default: the house cannot list orders."""
		return []

	def invoice_tracking(self, invoice: dict) -> list:
		"""Tracking numbers the house now shows for one already-mirrored invoice
		(``invoice_number``, ``account``) — a parcel can ship after its invoice posts.
		Default: the house cannot say."""
		return []

	# Does this house put OUR PO / order number on its invoices? Then an invoice that
	# matched no POS order at sync really was placed outside the POS. A house whose
	# invoices name neither (RSR) leaves a POS order's invoice "unmatched" until the
	# status poll links it, and Receive Goods must not offer that one twice.
	invoices_carry_po: bool = False

	def invoice_destination(self, invoice: dict) -> tuple:
		"""``(destination, note)`` for an invoice NO POS order matches: ``Store`` /
		``Drop-ship`` / ``Unknown`` (``lanes.DEST_*``), and for Unknown why
		(``lanes.NOTE_FAILED`` = the call failed, ask again; ``NOTE_NO_ANSWER`` = settled).
		Default: by the account the invoice was billed on (dealer ships to the shop,
		fulfillment to the buyer); a house whose invoices carry no account overrides this."""
		return lanes.with_note(lanes.destination_of_account(invoice.get("account")))

	@abstractmethod
	def check_availability(self, lines: list) -> list:
		"""Live qty/price second-confirm just before placing: [{sku,qty,cost,available,
		orderable}] — ``orderable`` False = blocked outright (discontinued, refused),
		as opposed to merely short."""

	def plan_fulfillment(self, lines: list, ship_context: dict) -> list:
		"""Enriched lines + a ship context → the OrderPlans to place.

		The shape of a plan is the same for every house — the lane decides the
		order type, the account and the destination (:mod:`.lanes`) — so a provider
		only states its own rules: which lanes it runs (``capabilities``), how a
		cart splits (``split_order``) and how a line ships (``ship_method_for``).
		Override this whole method only for a house whose plans are not one order
		per split group.
		"""
		if not lines:
			return []
		ctx = ship_context or {}
		self.assert_classification(lines)
		mode = ctx.get("mode") or lanes.DEFAULT_MODE
		if not lanes.is_lane(mode):
			# Never fall back to a lane the caller did not ask for: the fallback used to
			# be "stocking", which sends a buyer's goods to the shop instead.
			raise PlanningError(f"{mode!r} is not a way to ship an order.")
		route = lanes.lane(mode)
		if route.capability not in self.capabilities():
			raise LaneNotSupported(
				f"{self.display_name or self.code or 'This distributor'} does not "
				f"ship this way ({route.order_type}).")
		self.assert_lane_fits(route, lines)
		destination = lanes.destination(route.mode, ctx)
		fill_or_kill = bool(ctx.get("fill_or_kill", False))
		return [{
			"account": route.account,
			"order_type": route.order_type,
			"fill_or_kill": fill_or_kill,
			**destination,
			"lines": [{
				"sku": ln["sku"],
				"item_code": ln.get("item_code") or "",   # a SKU is not an Item
				"qty": ln["qty"],
				"ship_method": self.ship_method_for(ln),
			} for ln in group],
		} for group in self.split_order(lines)]

	def assert_classification(self, lines: list) -> None:
		"""Never guess a lane when the catalog's word on whether this is a
		firearm under the law (``legal_firearm``, the service class) disagrees with
		our own ``Item.is_firearm``: the catalogs are out of sync and a human must
		reconcile before we order. The house's ``ffl_required`` is its shipping rule
		(RSR sends barrels only to dealers), not a classification, so it is not
		compared; a line without ``legal_firearm`` falls back to it as before."""
		house = self.display_name or self.code
		for ln in lines:
			legal = ln["legal_firearm"] if "legal_firearm" in ln else ln.get("ffl_required")
			if bool(legal) != bool(ln.get("is_firearm")):
				raise PlanningError(
					f"SKU {ln.get('sku')!r}: {house} catalog says firearm={bool(legal)} "
					f"but Item.is_firearm={ln.get('is_firearm')} — order held "
					f"for manual classification review (#12).")

	def assert_lane_fits(self, route, lines: list) -> None:
		"""A firearm ships to an FFL and an accessory does not, so the lane and the
		goods must agree. A firearm planned on ``direct`` would be addressed to the
		buyer's own door — the one thing :mod:`.lanes` promises never happens — and an
		accessory planned on ``fds`` would be sent to the transfer dealer (and refused
		by the house). The store lanes take both.
		"""
		if route.mode == lanes.DIRECT and any(ln.get("is_firearm") for ln in lines):
			raise PlanningError(
				"A firearm cannot be drop-shipped to the buyer's address — it ships to "
				"the transfer dealer (order it on the firearm drop-ship lane).")
		if route.mode == lanes.DIRECT and any(
				ln.get("ffl_required") and "legal_firearm" in ln and not ln["legal_firearm"]
				and not ln.get(TO_STORE_ON_REFUSAL) for ln in lines):
			# A gun part the house ships only to a dealer (an RSR barrel): the house
			# may refuse it addressed to the buyer. Only a caller that re-orders a
			# refused one to the store (osa_dropship's web router marks its lines
			# TO_STORE_ON_REFUSAL) tries the buyer's door first; anyone else orders it
			# to the store.
			raise PlanningError(
				"This distributor ships the part only to a dealer — order it to the store.")
		if route.mode == lanes.FDS and not all(ln.get("is_firearm") for ln in lines):
			raise PlanningError(
				"Only firearms belong on a firearm drop-ship order — the distributor "
				"refuses an accessory sent to a transfer dealer.")

	def split_order(self, lines: list) -> list:
		"""The carts this order becomes: a firearm and an accessory never
		ride the same order — and then the house's ``max_order_lines`` cap. Firearms
		first, for a deterministic plan order."""
		firearms = [ln for ln in lines if ln.get("is_firearm")]
		others = [ln for ln in lines if not ln.get("is_firearm")]
		out = []
		for group in (firearms, others):
			if group:
				out.extend(chunk(group, self.max_order_lines or len(group)))
		return out

	def ship_method_for(self, line: dict) -> str:
		"""The DO line's ship method. The default keeps whatever the line already
		names; a house with carrier rules (handguns fly) overrides it."""
		return line.get("ship_method") or ""

	@abstractmethod
	def place_order(self, do_name: str) -> dict:
		"""Place the Distributor Order at the provider. Returns
		{po_num, web_ref, hold_status, ack_lines, totals, raw}."""

	@abstractmethod
	def get_order_status(self, do_name: str) -> dict:
		"""Live order status: {po_num, hold_status, lines[], raw}."""

	# ---- optional --------------------------------------------------------------
	# The generic layer asks for these BY NAME and coacts with a house that has none.
	# Defined below (the answer means "nothing"): connection_test, verify_transfer_dealer,
	# estimate_order_freight, delivery_options, update_order_ffl, hub_summary.
	# Deliberately NOT defined here, because callers detect support by presence
	# (``getattr(provider, name, None)``) and a no-op default would claim every house
	# supports them: blocked_states, shipping_options, catalog_row_by_upc,
	# promote_to_item, prefetch_rows, feed_heartbeat, catalog_content_heartbeat.
	def connection_test(self) -> dict:
		"""Live reachability probe for the hub's Test Connection button.
		Returns {checks: [{label, ok: bool|None, detail}]}; ok=None means
		"not applicable / not configured", not a failure. (Plain strings —
		this module stays frappe-free.)"""
		return {"checks": [{
			"label": "Connection test", "ok": None,
			"detail": f"Not implemented for provider '{self.code or '?'}'."}]}

	def verify_transfer_dealer(self, ffl: str) -> Optional[dict]:
		return None

	def estimate_order_freight(self, lines: list, mode: str, options=None) -> Optional[float]:
		"""Estimated freight for ONE distributor order (one OrderPlan), asked through
		:mod:`.freight`. ``lines`` = [{sku, qty, unit_cost, department, is_firearm,
		ship_method}]; ``mode`` = the plan's ship-context mode (direct / fds /
		to_store / stocking); ``options`` = the delivery options chosen for it, or
		None (a house whose ``delivery_options`` is None always gets None). None =
		no estimate available; the invoiced freight always replaces it. A house with
		flat per-order rates returns ``freight.flat_rate(...)``."""
		return None

	def delivery_options(self, firearm_order: bool) -> Optional[dict]:
		"""The per-order delivery options the counter may choose for this house —
		``{"signature", "adult_signature", "insurance"}`` → this kind of order's
		default — or None when the house has none to choose (its orders go out as
		its settings say)."""
		return None

	def update_order_ffl(self, do_name: str, ffl: str) -> Optional[dict]:
		return None

	def account_number(self, account: str) -> str:
		"""The house's own identifier for the account an order is placed on — what
		the Distributor Order shows as *Account* (RSR: the dealer / fulfillment
		account #; Sports South: the customer number). ``account`` is the lane's
		key ("dealer" / "fulfillment"). "" when the house has nothing to show."""
		return ""

	def hub_summary(self) -> dict:
		"""Read-only facts for the Distributors hub card, from this provider's own
		Settings: ``{"live_orders": bool, "ship_to_store": str}``, either key
		optional (live_orders = its Allow live orders switch; ship_to_store = the
		freight rule for orders shipped to the shop, in words). The default ({})
		shows no summary line."""
		return {}
