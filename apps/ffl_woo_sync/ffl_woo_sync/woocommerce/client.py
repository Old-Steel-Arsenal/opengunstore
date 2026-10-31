"""WooCommerce REST API v3 client.

Thin HTTP wrapper around `{store_url}/wp-json/wc/v3/`. Credentials read from
the WooCommerce Settings single doctype. Retries on 429/5xx with exponential
back-off.

WooCommerce auth: HTTP Basic — consumer_key as username, consumer_secret as
password. This is the documented scheme for HTTPS connections; all credential
fields are read via Frappe's encrypted get_password().

Usage:
    from ffl_woo_sync.woocommerce.client import WooCommerceClient
    client = WooCommerceClient.from_settings()
    info = client.probe_connection()
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import frappe
from ffl_core.credentials import RedactedBasicAuth, RedactedRepr
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Permit http:// only for local development (never sends creds over public net).
_LOCAL_HTTP_RE = re.compile(r"^http://(localhost|127\.)")


def _is_term_exists(exc: "WooCommerceError") -> bool:
	"""True when WooCommerce refused a term create because it already exists.

	WooCommerce answers ``400`` with a ``term_exists`` code (wording varies by
	taxonomy and WC version, so both the code and the phrase are accepted).
	"""
	if exc.status_code not in (400, 409):
		return False
	body = str(exc).casefold()
	return "term_exists" in body or "already exists" in body


def _conflict_resource_id(exc: "WooCommerceError") -> int | None:
	"""Pull ``data.resource_id`` — the winning term's id — out of a conflict body."""
	match = re.search(r'"resource_id"\s*:\s*(\d+)', str(exc))
	return int(match.group(1)) if match else None


SETTINGS_DOCTYPE = "WooCommerce Settings"
RETAIL_SITE = "retail"

# The stores this POS runs, by ``site`` — the ONE registry every per-store lookup in this app reads
# (Settings doctype, listing fields, Catalog Category term field, follow-the-tree default). One store:
# the retail storefront (1.11.0 removed the dealer store; neither shop ever took an order on it).
# ``site`` stays as a parameter because Woo Online Order rows and the endpoints carry one. A site
# that is not here is refused: :func:`assert_store_site` at a whitelisted endpoint (the user sees
# why), :func:`store_entry` everywhere else (an exception, no dialog).
STORES = {
	RETAIL_SITE: {
		"settings": SETTINGS_DOCTYPE,
		# The Item / Serial No fields that hold this store's listing state (``sync._f``).
		"fields": {
			"product_id": "woo_product_id",
			"listed": "woo_listed",
			"synced_at": "woo_synced_at",
			"sync_error": "woo_sync_error",
			"delisted": "woo_delisted",
			# Serial No only: ``delisted`` was raised by a GunBroker exit, not by a person.
			"channel_delisted": "woo_channel_delisted",
			"sold_archived": "woo_sold_archived",
		},
		# Catalog Category's term id on this store, and whether the store follows the category
		# tree when its Settings row has never been saved (owner 2026-09-26: retail does).
		"term_field": "woo_term_retail",
		"follow_tree_default": 1,
	},
}
SITES = tuple(STORES)


class WooCommerceError(RuntimeError):
	"""Raised on non-recoverable WooCommerce API errors. Carries the HTTP
	`status_code` (None for non-HTTP failures) so callers can detect specific
	HTTP status codes without substring-matching the message."""

	def __init__(self, *args, status_code: int | None = None):
		super().__init__(*args)
		self.status_code = status_code


class WooCommerceConfigError(WooCommerceError):
	"""Settings missing or incomplete."""


class UnknownStoreError(frappe.ValidationError, WooCommerceConfigError):
	"""A ``site`` this POS runs no store for. A ValidationError for the caller who asked for it;
	a WooCommerceConfigError — "no usable client" — for every path that already handles a store
	it cannot build a client for."""


def _is_store(site) -> bool:
	return isinstance(site, str) and site in STORES


def resolve_site(value) -> str | None:
	"""The store a ``site`` value names, or None when this POS runs no such store. Never raises.
	Trimmed and lower-cased; blank / None / whitespace is the retail store. The one place that
	reads a free-form ``site`` (webhook query param, job argument, hook payload, row field)."""
	site = str(value or "").strip().lower() or RETAIL_SITE
	return site if site in STORES else None


def _unknown_store_message(site) -> str:
	return frappe._("Unknown WooCommerce store {0}: only the retail store is connected.").format(
		repr(site))


def assert_store_site(site) -> None:
	"""A whitelisted endpoint's ``site`` argument: refuse a store this POS does not run with
	``frappe.throw`` (:class:`UnknownStoreError`), so the user sees why. Only for whitelisted entry
	points — everything below them uses :func:`store_entry`, which shows no dialog."""
	if not _is_store(site):
		frappe.throw(_unknown_store_message(site), UnknownStoreError)


def store_entry(site) -> dict:
	"""*site*'s :data:`STORES` entry, for code below the entry points (field / settings / term
	lookups, client construction, hooks). A store this POS does not run raises
	:class:`UnknownStoreError` directly — no message dialog, so a caller that handles the error
	(a job, a doc_event, a sweep) leaves nothing on the user's screen."""
	if not _is_store(site):
		raise UnknownStoreError(_unknown_store_message(site))
	return STORES[site]


@dataclass(repr=False)
class WooCommerceConfig(RedactedRepr):
	store_url: str
	consumer_key: str
	consumer_secret: str
	timeout: int

	@classmethod
	def from_settings(cls, site: str = "retail") -> "WooCommerceConfig":
		s = frappe.get_single(store_entry(site)["settings"])
		if not s.store_url:
			raise WooCommerceConfigError("WooCommerce Settings: Store URL is empty.")
		consumer_key = s.get_password("consumer_key", raise_exception=False) or ""
		if not consumer_key:
			raise WooCommerceConfigError("WooCommerce Settings: Consumer Key is missing.")
		consumer_secret = s.get_password("consumer_secret", raise_exception=False) or ""
		if not consumer_secret:
			raise WooCommerceConfigError("WooCommerce Settings: Consumer Secret is missing.")
		cleaned_url = s.store_url.rstrip("/")
		if not cleaned_url.startswith("https://") and not _LOCAL_HTTP_RE.match(cleaned_url):
			raise WooCommerceConfigError(
				"WooCommerce Settings: Store URL must use HTTPS (Basic auth credentials "
				"would otherwise be sent in plaintext)."
			)
		return cls(
			store_url=cleaned_url,
			consumer_key=consumer_key,
			consumer_secret=consumer_secret,
			timeout=int(s.request_timeout_seconds or 30),
		)


class WooCommerceClient:
	def __init__(self, config: WooCommerceConfig, session: requests.Session | None = None) -> None:
		self.config = config
		self.session = session or self._build_session()
		# Memoize the two P3 taxonomies per instance: {casefolded name: brand id} and
		# {slug: shipping class id}.  A listing sweep touches the same handful of
		# brands / shipping classes across hundreds of products.
		self._brand_cache: dict[str, int] = {}
		self._shipping_class_cache: dict[str, int] = {}

	@classmethod
	def from_settings(cls, site: str = "retail") -> "WooCommerceClient":
		return cls(WooCommerceConfig.from_settings(site=site))

	# -------------------------------------------------- HTTP plumbing

	def _build_session(self) -> requests.Session:
		s = requests.Session()
		# Retry on 5xx and 429 with exponential back-off; GET/HEAD/OPTIONS only —
		# POST/PUT are not idempotent in general so they retry at the method level.
		retry = Retry(
			total=3,
			backoff_factor=0.5,
			status_forcelist=(429, 500, 502, 503, 504),
			allowed_methods=frozenset(["GET", "HEAD", "OPTIONS"]),
		)
		adapter = HTTPAdapter(max_retries=retry)
		s.mount("https://", adapter)
		s.mount("http://", adapter)
		# WooCommerce Basic auth: consumer_key:consumer_secret over HTTPS.
		# Not a bare tuple: Session.auth is expanded on every requests frame
		# the Error Log prints (ffl_core.credentials).
		s.auth = RedactedBasicAuth(self.config.consumer_key, self.config.consumer_secret)
		return s

	def _url(self, path: str) -> str:
		"""Build the full wc/v3 endpoint URL."""
		return f"{self.config.store_url}/wp-json/wc/v3/{path.lstrip('/')}"

	def _request(self, method: str, path: str, **kwargs) -> dict | list | None:
		response = self.session.request(
			method,
			self._url(path),
			timeout=self.config.timeout,
			**kwargs,
		)
		if response.status_code >= 400:
			raise WooCommerceError(
				f"{method} {path} → {response.status_code}: {response.text[:500]}",
				status_code=response.status_code,
			)
		if not response.content:
			return None
		try:
			return response.json()
		except ValueError:
			raise WooCommerceError(
				f"{method} {path}: non-JSON response: {response.text[:200]}"
			)

	# -------------------------------------------------- public methods

	def get(self, path: str, **kwargs) -> dict | list | None:
		return self._request("GET", path, **kwargs)

	def post(self, path: str, **kwargs) -> dict | list | None:
		return self._request("POST", path, **kwargs)

	def put(self, path: str, **kwargs) -> dict | list | None:
		return self._request("PUT", path, **kwargs)

	def delete(self, path: str, **kwargs) -> dict | list | None:
		return self._request("DELETE", path, **kwargs)

	# -------------------------------------------------- product domain methods

	def get_attribute_ids(self) -> dict[str, int]:
		"""Fetch global WC attribute taxonomy IDs.

		``GET /wc/v3/products/attributes`` → ``{slug: id}`` map, e.g.
		``{"pa_caliber": 5, "pa_condition": 7}``.

		Call once per sync run and thread the result into the mapping layer so
		pushed products link to the real WooCommerce taxonomies instead of
		creating per-product custom attributes.
		"""
		attrs = self.get("products/attributes") or []
		return {a["slug"]: a["id"] for a in attrs if a.get("slug")}

	def _get_or_create_term(
		self,
		*,
		path: str,
		cache: dict[str, int],
		key: str,
		params: dict,
		match_field: str,
		create_json: dict,
		match_value: str | None = None,
	) -> int:
		"""Resolve a WooCommerce taxonomy term by an exact key, creating it if absent.

		One resolver for every taxonomy this app touches (product categories, brands,
		shipping classes): they differ only in endpoint, lookup params, the field the
		key matches and the create body — the skeleton (cache → GET → exact match →
		POST → cache) was otherwise identical three times over.

		``key`` must already be normalised the way ``match_field`` compares
		(casefolded name, lowercased slug); it doubles as the cache key.

		**Create conflicts resolve rather than fail.**  Two workers — a listing batch
		and a reconcile sweep, say — can POST the same missing term in the same
		instant; WooCommerce answers the loser 400 ``term_exists``.  That is not a
		failure, it is the term existing: take the ``resource_id`` WooCommerce hands
		back, else re-read the endpoint.  A conflict that still cannot be resolved
		raises — callers must never receive an invented id.
		"""
		cached = cache.get(key)
		if cached is not None:
			return cached

		def _lookup() -> int | None:
			for row in self.get(path, params=params) or []:
				if (row.get(match_field) or "").strip().casefold() == (match_value or key):
					return int(row["id"])
			return None

		found = _lookup()
		if found is not None:
			cache[key] = found
			return found

		try:
			created = self.post(path, json=create_json) or {}
		except WooCommerceError as exc:
			if not _is_term_exists(exc):
				raise
			term_id = _conflict_resource_id(exc)
			if term_id is None:
				term_id = _lookup()
			if term_id is None:
				raise
			cache[key] = term_id
			return term_id

		term_id = int(created["id"])
		cache[key] = term_id
		return term_id

	def list_categories(self) -> list[dict]:
		"""Every product category on the store: ``[{id, name, slug, parent,
		menu_order, count}]`` with the name unescaped (the REST API sends
		``Slings &amp; Mounts``). Product categories are linked to the catalog
		master's tree by id (``woocommerce.categories``), never looked up by name
		per product."""
		import html

		out, page = [], 1
		while True:
			rows = self.get("products/categories", params={"per_page": 100, "page": page,
				"orderby": "id"}) or []
			for r in rows:
				out.append({"id": int(r["id"]), "name": html.unescape(r.get("name") or "").strip(),
					"slug": r.get("slug") or "", "parent": int(r.get("parent") or 0),
					"menu_order": int(r.get("menu_order") or 0), "count": int(r.get("count") or 0)})
			if len(rows) < 100:
				return out
			page += 1

	def get_or_create_brand(self, name: str) -> int:
		"""Resolve a WooCommerce **brand** term by name, creating it if absent.

		Brands are the ``product_brand`` taxonomy (WooCommerce ≥ 9.4 core, or the
		WooCommerce Brands plugin), NOT a ``pa_*`` product attribute — the MAP
		pricing plugin matches products by brand term, so an attribute would be
		invisible to it.

		Raises :class:`WooCommerceError` (incl. 404 on a store without brand
		support) — callers that must not fail a whole push over a brand catch it;
		see ``sync._brand_id_for_item``.
		"""
		key = (name or "").strip()
		if not key:
			raise WooCommerceError("get_or_create_brand: empty brand name")
		return self._get_or_create_term(
			path="products/brands",
			cache=self._brand_cache,
			key=key.casefold(),
			params={"search": key, "per_page": 100},
			match_field="name",
			create_json={"name": key},
		)

	def get_or_create_shipping_class(self, slug: str, name: str | None = None) -> int:
		"""Resolve a WooCommerce shipping class by **slug**, creating it if absent.

		Slug, not name, is the identity: a product carries its shipping class as a
		slug, and WooCommerce **silently ignores** a slug that does not exist — the
		product would then quietly ship at the default rate.  So the dropship classes
 are ensured before the product push, not assumed.

		The lookup filters on ``slug=`` (exact) rather than ``search=`` (fuzzy).
		"""
		key = (slug or "").strip().lower()
		if not key:
			raise WooCommerceError("get_or_create_shipping_class: empty slug")
		return self._get_or_create_term(
			path="products/shipping_classes",
			cache=self._shipping_class_cache,
			key=key,
			params={"slug": key, "per_page": 100},
			match_field="slug",
			create_json={"name": name or key, "slug": key},
		)

	def list_shipping_classes(self) -> dict[str, int]:
		"""``GET /wc/v3/products/shipping_classes`` → ``{slug: id}`` for the store.

		One round-trip for the whole table, so a listing batch can ensure the three
		shipping classes once instead of probing per SKU.  Rows without a slug are
		skipped rather than keyed on ``""``.
		"""
		rows = self.get("products/shipping_classes", params={"per_page": 100}) or []
		index = {}
		for row in rows:
			slug = (row.get("slug") or "").strip().lower()
			if slug:
				index[slug] = int(row["id"])
				self._shipping_class_cache[slug] = index[slug]
		return index

	def create_product(self, payload: dict) -> dict:
		"""``POST /wc/v3/products`` — create a new WooCommerce product."""
		return self.post("products", json=payload)

	def update_product(self, woo_id: str | int, payload: dict) -> dict:
		"""``PUT /wc/v3/products/{woo_id}`` — update an existing product."""
		return self.put(f"products/{woo_id}", json=payload)

	def batch_update_products(self, updates: list[dict]) -> dict:
		"""``POST /wc/v3/products/batch`` — update up to 100 products in ONE request.

		Each entry needs its ``id``. The response is ``{"update": [...]}`` in
		request order; an entry that failed carries an ``error`` object
		(``code`` / ``message`` / ``data.status``) instead of the product, while
		the others still applied — the HTTP status is 200 either way.
		"""
		return self.post("products/batch", json={"update": updates}) or {}

	def get_product(self, woo_id: str | int) -> dict:
		"""``GET /wc/v3/products/{woo_id}`` — fetch a product by ID."""
		return self.get(f"products/{woo_id}")

	def get_products(self, ids, *, status: str = "any", per_page: int = 100) -> dict[str, dict]:
		"""``GET /wc/v3/products?include=…`` — many products in one request per
		``per_page`` ids: ``{str(id): product}``, the same product objects
		:meth:`get_product` returns.

		Best effort, and the caller owns the gaps: an id that is not in the answer
		is simply absent — ``status=any`` leaves out trashed products, a deleted
		one is not there, a non-numeric id is never sent, and a page that fails
		(HTTP error, timeout, a non-list body) is logged and skipped. A caller
		that must treat those exactly as before reads each missing id with
		:meth:`get_product` (its 404 / trash answers are the ones every caller
		already handles)."""
		wanted = list(dict.fromkeys(str(i).strip() for i in ids or [] if str(i or "").strip().isdigit()))
		out: dict[str, dict] = {}
		for start in range(0, len(wanted), per_page):
			chunk = wanted[start:start + per_page]
			try:
				rows = self.get("products", params={"include": ",".join(chunk),
					"per_page": per_page, "status": status})
			except (WooCommerceError, requests.RequestException) as exc:
				frappe.logger("woo.sync").warning(
					f"get_products: page of {len(chunk)} failed, its ids fall back to "
					f"single reads: {str(exc)[:200]}")
				continue
			for row in rows if isinstance(rows, list) else []:
				if isinstance(row, dict) and row.get("id") is not None:
					out[str(row["id"])] = row
		return out

	# -------------------------------------------------- order domain methods

	def get_order(self, woo_order_id: str | int) -> dict:
		"""``GET /wc/v3/orders/{woo_order_id}`` — fetch a single order by ID.

		Used as a fallback when the webhook payload omits underscore-prefixed
		meta_data keys (behaviour varies across WooCommerce versions).
		Returns the full WooCommerce Order object.
		"""
		return self.get(f"orders/{woo_order_id}") or {}

	def list_order_refunds(self, woo_order_id: str | int, per_page: int = 100,
			max_pages: int = 50) -> list[dict]:
		"""``GET /wc/v3/orders/{id}/refunds`` — every refund on the order with its
		line_items / shipping_lines / fee_lines breakdown (the webhook's
		``refunds[]`` carries only id, reason and total), page by page. Stops at a
		short page, a page that brings no id not already seen (a server ignoring
		``page``), or ``max_pages``."""
		refunds: list[dict] = []
		seen: set[str] = set()
		# lazy: 50 pages × 100 = 5,000 refunds on one order, far past any real
		# order; upgrade path = follow Woo's X-WP-TotalPages header instead.
		for page in range(1, max_pages + 1):
			result = self.get(f"orders/{woo_order_id}/refunds",
				params={"per_page": per_page, "page": page})
			batch = result if isinstance(result, list) else []
			fresh = [r for r in batch if str(r.get("id")) not in seen]
			if not fresh:
				break
			seen.update(str(r.get("id")) for r in fresh)
			refunds.extend(fresh)
			if len(batch) < per_page:
				break
		return refunds

	def list_orders(self, *, after: str | None = None, status: str = "any",
			per_page: int = 100, page: int = 1) -> list[dict]:
		"""``GET /wc/v3/orders`` — one page of orders, newest first. Powers the
		daily order-integrity audit (D2): ``after`` is an ISO-8601 cutoff,
		``status='any'`` includes cancelled/trash so the audit can tell a missing
		order apart from a released one. Returns [] on a non-list response."""
		params = {
			"per_page": per_page, "page": page, "status": status,
			"orderby": "date", "order": "desc",
		}
		if after:
			params["after"] = after
		result = self.get("orders", params=params)
		return result if isinstance(result, list) else []

	def update_order(self, woo_order_id: str | int, payload: dict) -> dict | list | None:
		"""``PUT /wc/v3/orders/{id}`` — write order fields back to WooCommerce.

		The store normally drives its own order status; a drop-ship order is the
		exception, because the goods leave RSR's dock and WooCommerce never learns
		of it otherwise.

		An empty payload is refused rather than sent: WooCommerce answers a blank
		PUT with 200 and the unchanged order, which reads as success and would let
		a completion path believe it had marked something.
		"""
		if not payload:
			raise ValueError("update_order() requires at least one field to write")
		return self._request("PUT", f"orders/{woo_order_id}", json=payload)

	def complete_order(self, woo_order_id: str | int) -> dict | list | None:
		"""Mark a WooCommerce order ``completed`` (the shipped-and-done state)."""
		return self.update_order(woo_order_id, {"status": "completed"})

	def create_order_note(self, woo_order_id: str | int, note: str,
			customer_note: bool = False) -> dict | list | None:
		"""``POST /wc/v3/orders/{id}/notes`` — attach a (private by default) order
		note. The serial-rejection feedback loop posts through here so a rejected
		dealer-entered serial is visible right on the WC Edit Order screen."""
		return self.post(
			f"orders/{woo_order_id}/notes",
			json={"note": note, "customer_note": customer_note},
		)

	def probe_connection(self) -> dict:
		"""GET /wp-json/wc/v3/products?per_page=1 — lightweight read-auth probe.

		Uses the products endpoint rather than /system_status because products
		only requires WooCommerce read credentials; system_status requires admin.
		Raises WooCommerceError on any HTTP error.
		Returns {"store_url": ..., "products_reachable": True} on success.
		"""
		self._request("GET", "products", params={"per_page": 1})
		return {"store_url": self.config.store_url, "products_reachable": True}
