"""One-click connection to the WooCommerce store (WooCommerce Settings → Connect).

The owner fills in the Store URL and presses Connect:

1. :func:`start_connect` makes a one-time state (15 minutes, Redis) and sends the
   browser to WooCommerce's own approval page, ``{store}/wc-auth/v1/authorize``.
2. On approval WooCommerce POSTs a fresh read/write REST key to
   :func:`wc_auth_callback`. The state must be one this POS issued and has not seen
   back yet; it is spent on the first callback whatever the outcome.
3. :func:`run_connect` (background job) does the rest, one step at a time, and writes
   each result to the Settings page's *Connect* status:

   * probe the store with the new key;
   * a new ``webhook_secret``; every store webhook delivering to this POS's
     ``woo_event`` is deleted, then order.created / order.updated / order.deleted
     are created, so a reconnect never leaves duplicate deliveries;
   * ``GET ffl-core/v1/pos-connection`` — the storefront plugin. Absent (404): done,
     product and order sync already work. Present: a dedicated API user
     (``webshop@<site>``) gets a fresh API secret, a new ``outbound_webhook_secret``
     is made, and both are pushed with ``PUT ffl-core/v1/pos-connection``; the plugin
     probes this POS with them before it stores anything.

Every Connect starts over and rotates every secret. There is no disconnect, and
one POS still connects one store (``client.STORES``).
"""
from __future__ import annotations

import json
import secrets
from urllib.parse import urlencode, urlparse

import frappe
from frappe.utils import get_url, now_datetime
from frappe.utils.password import set_encrypted_password

from ffl_core.alerts import safe_log_error
from ffl_core.roles import SYSTEM_ROLES
from ffl_core.utils import atomic, commit
from ffl_woo_sync.woocommerce.client import (
	RETAIL_SITE, SETTINGS_DOCTYPE, WooCommerceClient, WooCommerceError, clean_store_url)

STATE_TTL_SECONDS = 15 * 60
_STATE_KEY = "ffl_woo_sync:wc_auth_state:{}"

APP_NAME = "OpenGunstore"
SETTINGS_ROUTE = "/desk/woocommerce-settings"
CALLBACK_METHOD = "ffl_woo_sync.woocommerce.connect.wc_auth_callback"
WEBHOOK_METHOD = "ffl_woo_sync.woocommerce.webhook.woo_event"
WEBHOOK_TOPICS = ("order.created", "order.updated", "order.deleted")

PLUGIN_NAMESPACE = "ffl-core/v1"
PLUGIN_ROUTE = "pos-connection"

API_USER_LOCAL_PART = "webshop"
# lazy: the two existing roles are the smallest set every current plugin call passes (product
# push/pull, web_inquiry, web_events need Stock Manager; the drop-ship console's order actions
# take Distributor Console); a narrower role of its own before the plugin goes public.
API_USER_ROLES = ("Stock Manager", "Distributor Console")

# Status lines. The first line is the state; the Settings form polls while it is Running.
WAITING = "Waiting"
RUNNING = "Running"
CONNECTED = "Connected"
FAILED = "Failed"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _state_key(state: str) -> str:
	return frappe.cache.make_key(_STATE_KEY.format(state))


def _set_password(fieldname: str, value: str) -> None:
	"""Store a Password field on the Settings single the way ``Document.save`` does (encrypted in
	__Auth, a same-length mask in the form) without a full save — the callback runs as Guest and
	the job must not trip the form's validation over unrelated fields."""
	set_encrypted_password(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, value, fieldname)
	frappe.db.set_single_value(SETTINGS_DOCTYPE, fieldname, "*" * len(value))


def _new_secret() -> str:
	return secrets.token_hex(32)


def _delivery_url(pos_url: str) -> str:
	return f"{pos_url}/api/method/{WEBHOOK_METHOD}"


def _points_here(delivery_url: str | None, target: str) -> bool:
	"""A store webhook delivers to this POS's ``woo_event``: same host and path. Scheme and query
	(``?site=``) are ignored, so an old http:// or per-site hook is still recognised as ours."""
	got, want = urlparse(str(delivery_url or "").strip()), urlparse(target)
	return (got.netloc.lower() == want.netloc.lower()
		and got.path.rstrip("/") == want.path.rstrip("/"))


def _reason(exc: Exception) -> str:
	"""The store's own message out of a WooCommerce/WordPress error body, else the exception."""
	text = str(exc)
	start = text.find("{")
	if start >= 0:
		try:
			body = json.loads(text[start:])
		except ValueError:
			body = None
		if isinstance(body, dict) and body.get("message"):
			code = getattr(exc, "status_code", None)
			return f"{body['message']} ({code})" if code else str(body["message"])
	return text[:300]


class _Status:
	"""The Settings page's *Connect* field: a state line, a start stamp, then one line per step.
	Written with ``set_single_value`` and committed per step so a reload shows progress."""

	def __init__(self, lines: list[str] | None = None):
		self.lines = list(lines or [])

	@classmethod
	def start(cls, first: str, state: str = RUNNING) -> "_Status":
		status = cls([str(now_datetime())[:19], first])
		status._write(state)
		return status

	@classmethod
	def resume(cls) -> "_Status":
		text = frappe.db.get_single_value(SETTINGS_DOCTYPE, "connect_status") or ""
		return cls(text.splitlines()[1:])

	def _write(self, state: str) -> None:
		frappe.db.set_single_value(SETTINGS_DOCTYPE, "connect_status",
			"\n".join([state] + self.lines), update_modified=False)
		commit()

	def step(self, line: str) -> None:
		self.lines.append(line)
		self._write(RUNNING)

	def done(self, line: str) -> None:
		self.lines.append(line)
		self._write(CONNECTED)

	def fail(self, where: str, why: str) -> None:
		self.lines.append(f"{where} failed: {why}")
		self._write(FAILED)


# ---------------------------------------------------------------------------
# 1. Start: one-time state → WooCommerce's approval page
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def start_connect() -> dict:
	"""Begin a one-click connection: issue a one-time state and return the store's approval URL.

	The saved Store URL is used; the browser is sent to WooCommerce's
	``/wc-auth/v1/authorize`` page, which posts a new read/write REST key back to
	``wc_auth_callback`` once the store owner approves.

	Returns:
	    {"url": <the store's wc-auth approval URL>}.

	Raises:
	    ValidationError: the Store URL is blank or not HTTPS.
	"""
	frappe.only_for(SYSTEM_ROLES)
	try:
		store_url = clean_store_url(frappe.db.get_single_value(SETTINGS_DOCTYPE, "store_url"))
	except WooCommerceError as exc:
		frappe.throw(str(exc))
	# This request's own address: the one the store owner reached the POS on, and the one the
	# webhooks and the plugin are given later (a background job has no request to read it from).
	pos_url = get_url().rstrip("/")
	state = secrets.token_urlsafe(32)
	frappe.cache.set(_state_key(state), json.dumps({"user": frappe.session.user,
		"store_url": store_url, "pos_url": pos_url}), ex=STATE_TTL_SECONDS)
	_Status.start("Waiting for approval on the store", WAITING)
	query = urlencode({
		"app_name": APP_NAME,
		"scope": "read_write",
		"user_id": state,
		"return_url": pos_url + SETTINGS_ROUTE,
		"callback_url": f"{pos_url}/api/method/{CALLBACK_METHOD}",
	})
	return {"url": f"{store_url}/wc-auth/v1/authorize?{query}"}


# ---------------------------------------------------------------------------
# 2. Callback: WooCommerce posts the approved key
# ---------------------------------------------------------------------------

def _refuse(reason: str) -> dict:
	"""A 400, which makes WooCommerce show the owner an error and delete the key it just made."""
	frappe.local.response["http_status_code"] = 400
	frappe.logger("woo.connect", allow_site=True).info(f"wc_auth_callback refused: {reason}")
	return {"ok": False, "error": reason}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def wc_auth_callback(user_id: str | None = None, consumer_key: str | None = None,
		consumer_secret: str | None = None, key_permissions: str | None = None,
		key_id=None) -> dict:
	"""WooCommerce wc-auth callback: receives the REST key the store owner approved.

	Payload:
	    WooCommerce's JSON body ``{key_id, user_id, consumer_key, consumer_secret,
	    key_permissions}``. ``user_id`` carries the one-time state from
	    ``start_connect``.

	Args:
	    user_id: The one-time state; valid for 15 minutes and spent on first use.
	    consumer_key: The new WooCommerce consumer key.
	    consumer_secret: The new WooCommerce consumer secret.
	    key_permissions: Must be ``read_write``.
	    key_id: WooCommerce's id for the key (not used).

	Returns:
	    200 ``{"ok": true}`` once the key is stored and the rest of the connection is
	    queued; 400 ``{"ok": false, "error"}`` for an unknown, expired or used state, a
	    key that is not read/write, or a store that is no longer the one in Settings.
	"""
	raw = frappe.cache.getdel(_state_key(user_id)) if user_id else None
	if not raw:
		return _refuse("unknown, expired or already used state")
	state = json.loads(raw)
	if key_permissions != "read_write":
		_Status.resume().fail("Store key", "the key must be read/write")
		return _refuse("key_permissions must be read_write")
	consumer_key = str(consumer_key or "").strip()
	consumer_secret = str(consumer_secret or "").strip()
	if not consumer_key or not consumer_secret:
		return _refuse("consumer_key and consumer_secret are required")
	try:
		current = clean_store_url(frappe.db.get_single_value(SETTINGS_DOCTYPE, "store_url"))
	except WooCommerceError:
		current = ""
	if current != state.get("store_url"):
		return _refuse("the Store URL changed since Connect was pressed")

	_set_password("consumer_key", consumer_key)
	_set_password("consumer_secret", consumer_secret)
	_Status.start("Store key approved")
	frappe.enqueue(f"{__name__}.run_connect", queue="long", enqueue_after_commit=True,
		run_as=state.get("user") or "Administrator", pos_url=state.get("pos_url") or get_url())
	return {"ok": True}


# ---------------------------------------------------------------------------
# 3. Background: probe → webhooks → storefront plugin
# ---------------------------------------------------------------------------

def _list_webhooks(client: WooCommerceClient) -> list[dict]:
	out, page = [], 1
	while True:
		rows = client.get("webhooks", params={"per_page": 100, "page": page}) or []
		out.extend(rows)
		if len(rows) < 100:
			return out
		page += 1


def replace_webhooks(client: WooCommerceClient, secret: str, pos_url: str) -> tuple[int, int]:
	"""Delete every store webhook delivering to this POS's ``woo_event``, then create one per
	:data:`WEBHOOK_TOPICS` signed with *secret*. Returns ``(created, removed)``."""
	target = _delivery_url(pos_url)
	ours = [w for w in _list_webhooks(client) if _points_here(w.get("delivery_url"), target)]
	for hook in ours:
		client.delete(f"webhooks/{hook['id']}", params={"force": True})
	for topic in WEBHOOK_TOPICS:
		client.post("webhooks", json={"name": f"{APP_NAME} {topic}", "topic": topic,
			"delivery_url": target, "secret": secret, "status": "active"})
	return len(WEBHOOK_TOPICS), len(ours)


def plugin_connection(client: WooCommerceClient) -> dict | None:
	"""``GET ffl-core/v1/pos-connection``: the plugin's ``{connected, base_url}``, or None when the
	route does not exist (plugin not installed, or older than 0.7.0)."""
	try:
		return client.get(PLUGIN_ROUTE, namespace=PLUGIN_NAMESPACE) or {}
	except WooCommerceError as exc:
		if exc.status_code == 404:
			return None
		raise


def _ensure_role(role: str) -> None:
	"""Same seed as the drop-ship extension's: an API role with no Desk access of its own."""
	if frappe.db.exists("Role", role):
		return
	doc = frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 0})
	doc.flags.ignore_permissions = True
	doc.insert()


def api_user_email() -> str:
	return f"{API_USER_LOCAL_PART}@{frappe.local.site}"


def issue_api_user() -> tuple[str, str, str]:
	"""Create or reuse the storefront plugin's POS user and give it a fresh API secret.

	The user is never given a password and gets no welcome email, so nobody can sign in as it;
	it only answers to its API key. Its roles are :data:`API_USER_ROLES` (added when missing;
	roles someone else gave it are left alone). The key is kept, the secret rotated — the old
	pair stops working at once. Returns ``(user, api_key, api_secret)``."""
	for role in API_USER_ROLES:
		_ensure_role(role)
	email = api_user_email()
	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
	else:
		user = frappe.new_doc("User")
		user.email = email
		user.first_name = "Web Shop"
		user.send_welcome_email = 0
	user.enabled = 1
	held = {r.role for r in user.get("roles") or []}
	for role in API_USER_ROLES:
		if role not in held:
			user.append("roles", {"role": role})
	if not user.api_key:
		user.api_key = frappe.generate_hash(length=15)
	api_secret = frappe.generate_hash(length=15)
	user.api_secret = api_secret
	user.flags.ignore_permissions = True
	if user.is_new():
		user.insert()
	else:
		user.save()
	return user.name, user.api_key, api_secret


def run_connect(run_as: str = "Administrator", pos_url: str | None = None) -> None:
	"""Background job queued by :func:`wc_auth_callback`. Each step's outcome goes to the
	*Connect* status; the first failure stops the run and names the step."""
	frappe.set_user(run_as)
	pos_url = (pos_url or get_url()).rstrip("/")
	status = _Status.resume()
	step = "Store"
	try:
		client = WooCommerceClient.from_settings(RETAIL_SITE)
		client.probe_connection()
		status.step("Store reachable")

		step = "Webhooks"
		webhook_secret = _new_secret()
		_set_password("webhook_secret", webhook_secret)
		commit()
		created, removed = replace_webhooks(client, webhook_secret, pos_url)
		status.step(f"Webhooks: {created} created, {removed} removed")

		step = "FFL Core plugin"
		if plugin_connection(client) is None:
			status.done("FFL Core plugin not installed")
			return
		with atomic("WooCommerce Connect API user"):
			user, api_key, api_secret = issue_api_user()
		outbound_secret = _new_secret()
		# The plugin signs in with these before it answers, from another request: commit first.
		commit()
		answer = client.put(PLUGIN_ROUTE, namespace=PLUGIN_NAMESPACE, json={
			"base_url": pos_url,
			"api_key": api_key,
			"api_secret": api_secret,
			"webhook_secret": outbound_secret,
		}) or {}
		if not answer.get("connected"):
			raise WooCommerceError("the plugin did not confirm the connection")
		_set_password("outbound_webhook_secret", outbound_secret)
		status.done(f"FFL Core plugin connected as {user}")
	except Exception as exc:  # noqa: BLE001 — every failure is reported on the page, never raised
		status.fail(step, _reason(exc))
		safe_log_error(title=f"WooCommerce Connect: {step} failed", message=frappe.get_traceback())
