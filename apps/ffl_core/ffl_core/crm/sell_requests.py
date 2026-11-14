"""A sell-your-gun request: someone offers the store a gun on the website form.

Every request is its own CRM Deal (:func:`new_deal`, called by ``crm.inquiries``), never filed on a deal the
person already has: it is the store buying, not selling. The deal is marked by :data:`SELL`; the form's known
fields go in the deal's own fields (:data:`FIELDS`, on the deal so the Sell requests list can show them), what
the form sends beyond them stays in the request's note. The person is the deal's Contact: their Customer's
when they are one, else a Contact made for them (``web_orders._contact_of``). The deal is shared with the team.

The deal's title (``lead_name``, CRM's title field) is "Sell: <Manufacturer> <Model> · <person>"; its first and
last name stay the person's.

Photos are links to the store's own website, served to a request the POS signs (:func:`_signature`, the
shop's outbound webhook secret). After the request commits a job copies each into the POS as a private file
on the deal (:func:`copy_photos`): only an https link on the website's own host (WooCommerce Settings › Store
URL), no redirects, an image type, at most :data:`MAX_BYTES` each. A photo that cannot be copied keeps its
link. The website removed it (404): never asked for again. Any other failure (a website not checking the
POS's signature yet answers 403, a blank secret, the site down) is tried again by :func:`retry_photos` once a
day while the website keeps photos (:data:`RETRY_DAYS`), and only the deal's first failure writes an Error
Log.

Buying the gun, from the Deal page (``crm.setup.DEAL_SCRIPT``):

* **Make offer** stores one amount (:data:`OFFER`). It is money the store pays, so never ``deal_value``,
  which CRM's Won board and ``crm.orders`` read as sales. Nothing is emailed.
* **Seller accepted** stores the day (:data:`ACCEPTED`): the deal waits for the gun.
* **Receive gun** opens the register's Trade-in dialog filled from the deal (:func:`intake`), the seller
  being the deal's customer. Staff check the gun and submit it as any trade-in; that alone books the gun
  (Purchase Receipt or, as a credit, at checkout). Nothing here makes stock, an acquisition or a bound-book
  entry. The dialog's call names the deal; once it commits, :func:`link_intake` stores the acquisition and
  receipt on the deal and sets it Won when the gun is booked, or when its acquisition is submitted at
  checkout (:func:`acquisition_submitted`).

A sell request is no sale: it is left out of the web-order rules (``web_orders._open_deal``), Open order and
Hold gun refuse it (:func:`refuse_selling`).
"""
from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, quote, urlsplit

import frappe
from frappe import _
from frappe.utils import add_days, escape_html, flt, fmt_money, formatdate, nowdate

from ffl_core.crm import TITLE, installed
from ffl_core.crm.deals import DEAL, is_closed, open_deal_for

SELL = "ffl_sell_request"
CARD = "ffl_sell_card"
OFFER = "ffl_sell_offer"
ACCEPTED = "ffl_sell_accepted_on"
ACQUISITION = "ffl_sell_acquisition"
RECEIPT = "ffl_sell_receipt"
PHOTOS = "ffl_sell_photos"
# The website form's labels (ffl-core SellSubmission::pos_payload) and the deal field each is kept in.
FIELDS = {
	"Reference": "ffl_sell_reference",
	"Manufacturer": "ffl_sell_manufacturer",
	"Model": "ffl_sell_model",
	"Caliber": "ffl_sell_caliber",
	"Type": "ffl_sell_type",
	"Condition": "ffl_sell_condition",
	"Serial number": "ffl_sell_serial",
	"Original box": "ffl_sell_original_box",
	"Accessories": "ffl_sell_accessories",
	"Prefers": "ffl_sell_prefers",
	"Address": "ffl_sell_address",
}
_LONG = ("Accessories", "Address")
# What the form sends for a field the seller left blank.
_BLANK = {"(not provided)", "(none listed)"}
# The Trade-in dialog's words for the form's types (its Firearm Type options). The seller's condition is never
# mapped to the store's grades (owner, 2026-08-29): staff grade the gun in hand.
_TYPES = {"pistol": "Pistol", "revolver": "Revolver", "rifle": "Rifle", "shotgun": "Shotgun",
	"receiver / frame": "Receiver"}

PHOTO_JOB = "ffl_core.crm.sell_requests.copy_photos"
LINK_JOB = "ffl_core.crm.sell_requests.link_intake"
WIN_JOB = "ffl_core.crm.sell_requests.win_if_booked"
MAX_BYTES = 10 * 1024 * 1024
# What the website signs a photo request for (ffl-core 0.22.0), and how long it keeps a photo by default.
SIGNED_ACTION = "ffl_core_sell_photo"
RETRY_DAYS = 90
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
	"image/heic": ".heic"}


def _field(label: str, after: str, **extra) -> dict:
	fieldtype = "Small Text" if label in _LONG else "Data"
	return {"fieldname": FIELDS[label], "fieldtype": fieldtype, "label": label, "insert_after": after, **extra}


# CRM Deal fields (crm.setup.CUSTOM_FIELDS), in this order after the web order that won a deal.
DEAL_FIELDS = [
	{"fieldname": SELL, "fieldtype": "Check", "label": "Sell request", "read_only": 1, "no_copy": 1,
		"search_index": 1, "insert_after": "ffl_web_order"},
	# The Deal page's Gun offered card: shown for a sell request only.
	{"fieldname": CARD, "fieldtype": "HTML", "label": "Gun offered", "depends_on": f"eval:doc.{SELL}",
		"insert_after": SELL},
	*(_field(label, after) for label, after in zip(FIELDS, [CARD, *FIELDS.values()])),
	{"fieldname": OFFER, "fieldtype": "Currency", "options": "currency", "label": "Offer", "no_copy": 1,
		"insert_after": FIELDS["Address"]},
	{"fieldname": ACCEPTED, "fieldtype": "Date", "label": "Seller accepted on", "no_copy": 1, "insert_after": OFFER},
	{"fieldname": ACQUISITION, "fieldtype": "Link", "options": "FFL Acquisition", "label": "Received as",
		"read_only": 1, "no_copy": 1, "search_index": 1, "insert_after": ACCEPTED},
	{"fieldname": RECEIPT, "fieldtype": "Link", "options": "Purchase Receipt", "label": "Purchase receipt",
		"read_only": 1, "no_copy": 1, "insert_after": ACQUISITION},
	{"fieldname": PHOTOS, "fieldtype": "Long Text", "label": "Photos", "hidden": 1, "no_copy": 1,
		"insert_after": RECEIPT},
]


def ready() -> bool:
	"""Whether the deal fields are there: the first deploy of the release runs its code for a moment before
	the migrate adds them."""
	return installed() and frappe.db.has_column(DEAL, SELL)


def is_sell_request(deal) -> bool:
	"""*deal*: a CRM Deal document or name."""
	if isinstance(deal, str):
		return ready() and bool(frappe.db.get_value(DEAL, deal, SELL))
	return bool(deal.get(SELL))


def refuse_selling(deal) -> None:
	"""Open order and Hold gun: not on a gun someone sells to the store."""
	if is_sell_request(deal):
		frappe.throw(_("This deal is a gun offered to the store. Use Make offer and Receive gun."),
			title=_("Sell request"))


# ---------------------------------------------------------------- the request


def known_fields(details: dict) -> tuple[dict, dict]:
	"""(deal field -> value, the details no field takes). A value the seller left blank is no value."""
	values, rest = {}, {}
	for label, value in details.items():
		if label in FIELDS:
			if value and value not in _BLANK:
				values[FIELDS[label]] = value if label in _LONG else value[:140]
		else:
			rest[label] = value
	return values, rest


def sell_title(values: dict, person: str) -> str:
	"""The deal's title: "Sell: <Manufacturer> <Model> · <person>", a missing part left out."""
	gun = " ".join(v for v in (values.get(FIELDS["Manufacturer"]), values.get(FIELDS["Model"])) if v)
	return " · ".join(p for p in (f"Sell: {gun}" if gun else "Sell", person) if p)[:140]


def new_deal(email: str, full_name: str, phone: str, values: dict, photos: list[str]) -> str:
	"""A new sell-request deal for *email* with the form's *values* (:func:`known_fields`) and *photos*, shared
	with the team. Returns its name."""
	from ffl_core.crm.mail import share_with_team
	from ffl_core.crm.web_orders import _contact_of

	from ffl_core.crm.deals import customer_of, person_name

	contact = _contact_of(email, full_name, phone)
	person = person_name(contact, customer_of(contact), email)["lead_name"]
	deal = open_deal_for(contact, title=sell_title(values, person), new=True)
	values = {SELL: 1, **values, PHOTOS: json.dumps([{"url": u} for u in photos]) if photos else None}
	if frappe.db.exists("CRM Lead Source", "Website"):
		values["source"] = "Website"
	frappe.db.set_value(DEAL, deal, values)
	share_with_team(DEAL, deal)
	return deal


def queue_photos(deal: str) -> None:
	"""Queue :func:`copy_photos` for once the caller's transaction commits. Never raises."""
	try:
		frappe.enqueue(PHOTO_JOB, queue="short", enqueue_after_commit=True, job_id=f"{PHOTO_JOB}:{deal}",
			deduplicate=True, deal=deal)
	except Exception:  # noqa: BLE001 — the links stay on the card
		frappe.log_error(title=f"[{TITLE}] Sell request photos not queued: {deal}", message=frappe.get_traceback())


def photos_of(deal) -> list[dict]:
	"""[{"url", "file"?}] of a deal (document or the stored JSON's row)."""
	try:
		value = json.loads(deal.get(PHOTOS) or "[]")
	except ValueError:
		return []
	return [p for p in value if isinstance(p, dict) and p.get("url")] if isinstance(value, list) else []


class PhotoGone(Exception):
	"""The website removed the photo (its retention cleanup): never asked for again."""


def _signature(secret: str, timestamp: str, filename: str) -> str:
	"""The website's check on a sell photo request (ffl-core 0.22.0): hex HMAC-SHA256 of
	"<timestamp>.GET\\nffl_core_sell_photo\\n<filename>", by the signer the shop's other events use."""
	from ffl_woo_sync.woocommerce.status_events import sign

	return sign(secret, timestamp, f"GET\n{SIGNED_ACTION}\n{filename}".encode())


def _secret() -> str:
	try:
		return frappe.get_single("WooCommerce Settings").get_password("outbound_webhook_secret",
			raise_exception=False) or ""
	except Exception:  # noqa: BLE001 — settings not migrated: no secret
		return ""


def _fetch(url: str, host: str, secret: str) -> tuple[bytes, str]:
	"""The image at *url* and its file extension, asked for with the POS's signature. Refuses (ValueError) a
	link that is not https on *host*, a redirect, any answer but 200, a type that is not an image, and more
	than :data:`MAX_BYTES`; raises :class:`PhotoGone` on 404."""
	import requests

	from ffl_core.mailing_address import store_host

	parts = urlsplit(url)
	if parts.scheme != "https" or not host or store_host(url) != host or parts.port not in (None, 443) \
			or parts.username or parts.password:
		raise ValueError("not a link on the store's website")
	filename = (parse_qs(parts.query).get("filename") or [""])[0]   # parse_qs decodes it
	timestamp = str(int(time.time()))
	headers = {"x-osa-timestamp": timestamp, "x-osa-signature": _signature(secret, timestamp, filename)}
	with requests.get(url, headers=headers, timeout=15, stream=True, allow_redirects=False) as response:
		if response.status_code == 404:
			raise PhotoGone("HTTP 404")
		if response.status_code != 200:
			raise ValueError(f"HTTP {response.status_code}")
		kind = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
		if kind not in IMAGE_TYPES:
			raise ValueError(f"not an image ({kind or 'no type'})")
		content = response.raw.read(MAX_BYTES + 1, decode_content=True)
	if len(content) > MAX_BYTES:
		raise ValueError("larger than 10 MB")
	if not content:
		raise ValueError("empty")
	return content, IMAGE_TYPES[kind]


def _waiting(photo: dict) -> bool:
	return not (photo.get("file") or photo.get("gone"))


def copy_photos(deal: str) -> list[str]:
	"""Copy the deal's photos into the POS as private files on it (the module says which links are fetched and
	how the request is signed). A photo copied before, or removed by the website (404), is not asked for again;
	any other failure keeps the link for :func:`retry_photos`. The first failure on a deal writes one Error Log,
	later ones are quiet. Returns the file URLs it made."""
	from ffl_core.crm.inquiries import MAX_PHOTOS
	from ffl_core.mailing_address import shop_host

	if not ready() or not frappe.db.exists(DEAL, deal):
		return []
	photos = photos_of(frappe.db.get_value(DEAL, deal, [PHOTOS], as_dict=True))
	waiting = [p for p in photos[:MAX_PHOTOS] if _waiting(p)]
	if not waiting:
		return []
	host, secret, made, failed = shop_host(), _secret(), [], []
	logged = any(p.get("failed") for p in photos)
	for i, photo in enumerate(photos[:MAX_PHOTOS], 1):
		if not _waiting(photo):
			continue
		try:
			if not secret:
				raise ValueError("no outbound webhook secret in WooCommerce Settings")
			content, ext = _fetch(photo["url"], host, secret)
			photo["file"] = frappe.get_doc({"doctype": "File", "file_name": f"sell-request-{i}{ext}", "is_private": 1,
				"attached_to_doctype": DEAL, "attached_to_name": deal, "content": content}).insert(ignore_permissions=True).file_url
			photo.pop("failed", None)
			made.append(photo["file"])
		except PhotoGone:
			photo["gone"] = 1
		except Exception as e:  # noqa: BLE001 — the link stays; one log per deal below
			photo["failed"] = 1
			failed.append(f"{photo['url']}: {e}")
	frappe.db.set_value(DEAL, deal, PHOTOS, json.dumps(photos), update_modified=False)
	if failed and not logged:
		frappe.log_error(title=f"[{TITLE}] Sell request photos not copied yet: {deal}",
			message="Tried again once a day while the website keeps them.\n" + "\n".join(failed))
	return made


def retry_photos() -> list[str]:
	"""Daily: queue :func:`copy_photos` for each sell request made in the last :data:`RETRY_DAYS` days with a
	photo still waiting (the website not answering signed requests yet, or down). Returns the deals queued."""
	if not ready():
		return []
	# lazy: reads every recent sell request's photo list (a store gets a few a week); upgrade path: a flag column.
	deals = [r.name for r in frappe.get_all(DEAL, filters={SELL: 1, "creation": [">=", add_days(nowdate(), -RETRY_DAYS)],
		PHOTOS: ["is", "set"]}, fields=["name", PHOTOS]) if any(_waiting(p) for p in photos_of(r))]
	for deal in deals:
		queue_photos(deal)
	return deals


# ---------------------------------------------------------------- offer and receipt


def _open_sell_deal(deal) -> None:
	if not is_sell_request(deal):
		frappe.throw(_("This deal is not a sell request."), title=_("Not a sell request"))
	if is_closed(deal.status):
		frappe.throw(_("This deal is closed."), title=_("Closed"))


def make_offer(deal, amount) -> dict:
	"""Store the store's offer on a sell-request deal (a document the caller may write). Returns :func:`card`."""
	_open_sell_deal(deal)
	amount = flt(amount, 2)
	if amount <= 0:
		frappe.throw(_("Enter the amount the store offers."))
	if received(deal):
		frappe.throw(_("The gun is received already."))
	deal.db_set(OFFER, amount, notify=True)
	return card(deal)


def accepted(deal) -> dict:
	"""The seller took the offer: the deal waits for the gun from today (a second click keeps the first day)."""
	_open_sell_deal(deal)
	if not flt(deal.get(OFFER)):
		frappe.throw(_("Make an offer first."))
	if not deal.get(ACCEPTED):
		deal.db_set(ACCEPTED, nowdate(), notify=True)
	return card(deal)


def received(deal) -> bool:
	return bool(deal.get(ACQUISITION) or deal.get(RECEIPT))


def _caliber(value: str) -> str:
	return (value and frappe.db.get_value("Caliber", {"name": value}, "name")) or value


def intake(deal) -> dict:
	"""What the register's Trade-in dialog is filled with for this deal: {"deal", "source_name" (the seller,
	the deal's customer), "manufacturer", "model_name", "caliber", "firearm_type", "serial_no",
	"acquisition_cost" (the offer)}. No condition: staff grade the gun in hand. Reads only: the dialog's
	submit books the gun."""
	from ffl_core.crm.customer import linked_customer

	_open_sell_deal(deal)
	if received(deal):
		frappe.throw(_("The gun is received already."))
	customer = linked_customer(deal)
	if not customer:
		frappe.throw(_("Link this deal to a customer first."), title=_("No customer"))
	value = lambda label: deal.get(FIELDS[label]) or ""  # noqa: E731
	return {
		"deal": deal.name,
		"source_name": customer,
		"manufacturer": value("Manufacturer"),
		"model_name": value("Model"),
		"caliber": _caliber(value("Caliber")),
		"firearm_type": _TYPES.get(value("Type").strip().lower(), ""),
		"serial_no": value("Serial number"),
		"acquisition_cost": flt(deal.get(OFFER)) or None,
	}


def after_intake(deal: str | None, customer: str | None, result: dict) -> dict:
	"""``trade_in.create_trade_in_intake``: a trade-in made from a sell-request deal is linked to it once its
	transaction commits (:func:`link_intake`). Never raises, never writes into the trade-in. Returns *result*."""
	deal = str(deal or "").strip()
	if deal and result:
		try:
			frappe.enqueue(LINK_JOB, queue="short", enqueue_after_commit=True, deal=deal, customer=customer,
				acquisition=result.get("acquisition"), receipt=result.get("purchase_receipt"))
		except Exception:  # noqa: BLE001 — staff can still close the deal by hand
			frappe.log_error(title=f"[{TITLE}] Sell request not linked: {deal}", message=frappe.get_traceback())
	return result


def link_intake(deal: str, customer: str | None = None, acquisition: str | None = None,
		receipt: str | None = None) -> bool:
	"""Store the trade-in's acquisition and receipt on the sell-request deal and win it once the gun is booked.
	Only for an open sell request whose customer is the trade-in's seller. Never raises. True when linked."""
	frappe.db.savepoint("ffl_crm_sell_link")
	try:
		if not ready() or not frappe.db.exists(DEAL, deal) or not (acquisition or receipt):
			return False
		doc = frappe.get_doc(DEAL, deal, for_update=True)
		if not doc.get(SELL) or is_closed(doc.status) or (customer and doc.get("erpnext_customer") != customer):
			return False
		doc.db_set({ACQUISITION: acquisition, RECEIPT: receipt})
		_win_if_booked(doc)
		return True
	except Exception:
		frappe.db.rollback(save_point="ffl_crm_sell_link")
		frappe.log_error(title=f"[{TITLE}] Sell request not linked: {deal}", message=frappe.get_traceback())
	return False


def _booked(doc) -> bool:
	acquisition, receipt = doc.get(ACQUISITION), doc.get(RECEIPT)
	return bool((acquisition and frappe.db.get_value("FFL Acquisition", acquisition, "docstatus") == 1)
		or (receipt and frappe.db.get_value("Purchase Receipt", receipt, "docstatus") == 1))


def _win_if_booked(doc) -> None:
	from ffl_core.crm.orders import _mark_won, _save

	if _booked(doc) and frappe.db.get_value("CRM Deal Status", doc.status, "type") != "Won":
		_mark_won(doc)
		_save(doc)


def win_if_booked(deal: str) -> None:
	"""Job: the deal is Won when its gun is booked now. Never raises."""
	frappe.db.savepoint("ffl_crm_sell_win")
	try:
		if ready() and frappe.db.exists(DEAL, deal):
			_win_if_booked(frappe.get_doc(DEAL, deal, for_update=True))
	except Exception:
		frappe.db.rollback(save_point="ffl_crm_sell_win")
		frappe.log_error(title=f"[{TITLE}] Sell request not won: {deal}", message=frappe.get_traceback())


def acquisition_submitted(doc, method=None) -> None:
	"""FFL Acquisition on_submit: a trade-in taken as credit is booked at checkout; its sell-request deal is
	won after that commits. Never fails the acquisition."""
	try:
		if not ready():
			return
		for deal in frappe.get_all(DEAL, filters={ACQUISITION: doc.name}, pluck="name"):
			frappe.enqueue(WIN_JOB, queue="short", enqueue_after_commit=True, deal=deal)
	except Exception:  # noqa: BLE001
		frappe.log_error(title=f"[{TITLE}] Sell request not won: {doc.name}", message=frappe.get_traceback())


# ---------------------------------------------------------------- the card

_ROW = "margin-bottom:6px"
_MUTED = "color:#6b7280"


def _link(path: str, name: str, label: str | None = None) -> str:
	return (f'<a href="/app/{path}/{quote(str(name), safe="")}" style="color:inherit;text-decoration:underline">'
		f"{escape_html(label or name)}</a>")


def _line(*values) -> str:
	text = " · ".join(escape_html(str(v)) for v in values if v)
	return f'<div style="{_ROW}">{text}</div>' if text else ""


def _photos_html(photos: list[dict]) -> str:
	thumbs = []
	for i, photo in enumerate(photos, 1):
		target = escape_html(photo.get("file") or photo["url"])
		inner = (f'<img src="{target}" alt="" style="width:64px;height:64px;object-fit:cover;border-radius:4px">'
			if photo.get("file") else escape_html(_("Photo {0}").format(i)))
		thumbs.append(f'<a href="{target}" target="_blank" rel="noopener noreferrer">{inner}</a>')
	return f'<div style="display:flex;flex-wrap:wrap;gap:6px;{_ROW}">{"".join(thumbs)}</div>' if thumbs else ""


def _status_html(deal) -> str:
	currency = deal.get("currency")
	if received(deal):
		doc = deal.get(RECEIPT) and ("purchase-receipt", deal.get(RECEIPT))
		doc = doc or ("ffl-acquisition", deal.get(ACQUISITION))
		booked = _booked(deal)
		label = _("Received") if booked else _("Received, booked at checkout")
		return f'<div style="font-weight:600;{_ROW}">{escape_html(label)}: {_link(*doc)}</div>'
	parts = []
	if flt(deal.get(OFFER)):
		parts.append(f'<div style="font-weight:600;{_ROW}">{escape_html(_("Offer"))}: '
			f'{escape_html(fmt_money(deal.get(OFFER), currency=currency))}</div>')
	if deal.get(ACCEPTED):
		parts.append(f'<div style="color:#b45309;{_ROW}">'
			f'{escape_html(_("Accepted {0}, waiting for the gun").format(formatdate(deal.get(ACCEPTED))))}</div>')
	return "".join(parts)


def card(deal) -> dict:
	"""The Deal page's Gun offered card: {"html", "offer", "accepted_on", "received"}. Every value escaped."""
	value = lambda label: deal.get(FIELDS[label]) or ""  # noqa: E731
	box = value("Original box")
	parts = [
		f'<div style="font-weight:600;{_ROW}">{escape_html(" ".join(v for v in (value("Manufacturer"), value("Model")) if v))}</div>',
		_line(value("Caliber"), value("Type"), value("Condition")),
		_line(value("Serial number") and _("Serial {0}").format(value("Serial number"))),
		_line({"yes": _("Original box"), "no": _("No original box")}.get(box.strip().lower(), box)),
		_line(value("Accessories")),
		_line(value("Prefers") and _("Prefers {0}").format(value("Prefers"))),
		_line(value("Address")),
		_line(value("Reference")),
		_photos_html(photos_of(deal)),
		_status_html(deal),
	]
	return {"html": "".join(parts), "offer": flt(deal.get(OFFER)), "accepted_on": deal.get(ACCEPTED),
		"received": received(deal)}
