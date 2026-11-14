"""Website events: what visitors did on the shop, relayed by its POS bridge
(``ffl_woo_sync.woocommerce.client_api.web_events``).

The shop sends ``{"events": [{id, type, occurred_at, data}]}`` and is answered
``{"accepted": [id, ...], "rejected": [{id, reason}, ...]}``:

* accepted: done, including an id seen before (``Marketing Event.dedupe_key = "web:<id>"``);
* rejected: never taken, for a reason that will not change (``unknown_type``, ``invalid_email``,
  ``invalid_data``); the reason is a code and never echoes the data;
* neither: an unexpected error; logged here, and the shop sends the event again later.

Each event runs in its own savepoint, so one that fails leaves nothing behind and does not stop the
rest. Adding an event type is adding a handler to :data:`HANDLERS`. Nothing here sends mail.

Every field is public input: capped, tags kept only when they are slugs, URLs only when they are
plain http(s) (no quotes, brackets or spaces), and text kept to one line (``contacts.plain_text``).
Text is stored as plain text; whatever renders it (Desk, a mail template) escapes it.
"""
from __future__ import annotations

from collections.abc import Callable

import frappe
from frappe import _

from ffl_core.marketing import EVENT, contacts, enabled, requests, tags

UNKNOWN_TYPE, INVALID_EMAIL, INVALID_DATA = "unknown_type", "invalid_email", "invalid_data"
# lazy: 100 events per call (the shop sends one today); upgrade path: raise it with the shop's batch size.
MAX_EVENTS = 100
CAP = {"id": 80, "name": 140, "source": 140, "url": 500, "ip": 45, "code": 60, "label": 140, "key": 140}
ALERT_KINDS = {"back_in_stock": requests.BACK_IN_STOCK, "model": requests.MODEL_ALERT}


class Rejected(Exception):
	"""A deterministic refusal; ``code`` is what the shop is told."""

	def __init__(self, code: str):
		super().__init__(code)
		self.code = code


def _text(value, cap: str) -> str:
	return contacts.plain_text(value if isinstance(value, (str, int, float)) and not isinstance(value, bool) else "",
		CAP[cap])


def url(value) -> str:
	"""*value* when it is a plain http(s) URL of at most 500 characters, else ""."""
	text = value.strip() if isinstance(value, str) else ""
	if len(text) > CAP["url"] or not text.lower().startswith(("http://", "https://")) \
			or any(c.isspace() or c in "\"'<>\\`" for c in text):
		return ""
	return text


def _email(data: dict) -> str:
	address = contacts.normalize_email(data.get("email") if isinstance(data.get("email"), str) else "")
	if not address:
		raise Rejected(INVALID_EMAIL)
	return address


def _list(value) -> list:
	return value if isinstance(value, list) else []


def contact_subscribed(data: dict, key: str, when) -> None:
	"""``contact.subscribed``: someone gave their email on a website form. The contact is made or
	updated and becomes ``Subscribed`` (one already unsubscribed, bounced or complained stays so),
	takes the event's tags, and the event is logged with its source page, IP and coupon: the record of
	the consent."""
	email = _email(data)
	source = _text(data.get("source"), "source") or "web"
	wanted = tags.clean(data.get("tags")) + [t for t in map(tags.interest, _list(data.get("interests"))) if t]
	coupon = data.get("coupon") if isinstance(data.get("coupon"), dict) else {}
	contact = contacts.get_or_create(email, first_name=_text(data.get("first_name"), "name"),
		last_name=_text(data.get("last_name"), "name"), source=source)
	tags.add(contact.name, *wanted)
	detail = dict(source=source, page_url=url(data.get("page_url")), ip=_text(data.get("ip"), "ip"),
		tags=wanted, coupon=_text(coupon.get("code"), "code"), coupon_amount=_text(coupon.get("amount"), "code"),
		coupon_expires_at=_text(coupon.get("expires_at"), "code"))
	if not contacts.set_status(contact, contacts.SUBSCRIBED, channel=source, occurred_at=when,
			detail=contacts.event_detail(**detail), dedupe_key=key):
		kept = contact.status if contacts.is_suppressed(contact.status) else None
		contacts.log_event(contact.name, contacts.SUBSCRIBED, occurred_at=when,
			detail=contacts.event_detail(**detail, kept_status=kept), dedupe_key=key)


def alert_requested(data: dict, key: str, when) -> None:
	"""``alert.requested``: someone asked to hear when a product is back in stock (``kind`` =
	``back_in_stock``) or a model is listed (``kind`` = ``model``). The contact is made if new (its
	status unchanged), the request filed once per target, and a ``Requested`` event logged."""
	email = _email(data)
	kind = ALERT_KINDS.get(data.get("kind") if isinstance(data.get("kind"), str) else "")
	if not kind:
		raise Rejected(INVALID_DATA)
	product_id = data.get("product_id")
	fields = {
		"sku": _text(data.get("sku"), "label"),
		"web_product_id": str(product_id) if type(product_id) is int and product_id > 0 else "",
		"model_key": _text(data.get("model_key"), "key"),
		"label": _text(data.get("product_name") if kind == requests.BACK_IN_STOCK else data.get("model_label"), "label"),
		"url": url(data.get("url")),
	}
	if (kind == requests.MODEL_ALERT and not fields["model_key"]) or not requests.target(kind, fields):
		raise Rejected(INVALID_DATA)
	contact = contacts.get_or_create(email, source="alert")
	name, _created = requests.add(contact.name, kind, **fields, item=requests.item_for(fields["sku"], fields["web_product_id"]),
		requested_at=when)
	contacts.log_event(contact.name, contacts.REQUESTED, occurred_at=when, dedupe_key=key,
		detail=contacts.event_detail(kind=kind, request=name))


HANDLERS: dict[str, Callable[[dict, str, object], None]] = {
	"contact.subscribed": contact_subscribed,
	"alert.requested": alert_requested,
}


def receive(events) -> dict:
	"""Process a batch from the shop (see the module docstring); refuses the whole call while
	marketing is off, which the shop retries."""
	if not enabled():
		frappe.throw(_("Marketing is turned off on this POS (Marketing Settings)."), title=_("Marketing is off"))
	if isinstance(events, str):
		try:
			events = frappe.parse_json(events)
		except ValueError:   # orjson's decode error is one
			events = None
	if not isinstance(events, list) or len(events) > MAX_EVENTS:
		frappe.throw(_("Send a list of at most {0} events.").format(MAX_EVENTS), title=_("Events refused"))
	accepted, rejected = [], []
	for event in events:
		event_id = event.get("id") if isinstance(event, dict) else None
		event_id = event_id.strip() if isinstance(event_id, str) else ""
		if not event_id:
			continue   # nothing to answer with
		try:
			if len(event_id) > CAP["id"]:
				raise Rejected(INVALID_DATA)
			_one(event_id, event)
		except Rejected as refusal:
			rejected.append({"id": event_id, "reason": refusal.code})
		except Exception:
			# Neither list: the shop sends it again. The id and type only; the data holds an email.
			frappe.log_error(title="Marketing web event failed",
				message=f"event {event_id} ({_text(event.get('type'), 'label')})\n{frappe.get_traceback()}")
		else:
			accepted.append(event_id)
	return {"accepted": accepted, "rejected": rejected}


def _one(event_id: str, event: dict) -> None:
	key = f"web:{event_id}"
	if frappe.db.exists(EVENT, {"dedupe_key": key}):
		return
	handler = HANDLERS.get(event.get("type") if isinstance(event.get("type"), str) else "")
	if not handler:
		raise Rejected(UNKNOWN_TYPE)
	data = event.get("data")
	if not isinstance(data, dict):
		raise Rejected(INVALID_DATA)
	when = contacts.to_system_datetime(event.get("occurred_at")) or frappe.utils.now_datetime()
	frappe.db.savepoint("mkt_web_event")
	try:
		handler(data, key, when)
	except BaseException:
		frappe.db.rollback(save_point="mkt_web_event")
		frappe.clear_messages()
		raise
