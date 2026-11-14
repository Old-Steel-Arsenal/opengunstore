"""The one-off move from the shop's previous email plugin (FluentCRM) to Marketing Contacts.

Input: the JSON the plugin's read-only export writes (one object; ``contacts`` and ``campaigns``).
Run it dry first; only an explicit ``dry_run=0`` writes::

    bench --site <site> execute ffl_core.marketing.migrate.import_fluentcrm \\
        --kwargs "{'path': '/path/export.json', 'dry_run': 1}"

What becomes what:

* status: ``subscribed`` → Subscribed, ``unsubscribed`` → Unsubscribed, ``bounced`` → Bounced,
  ``complained`` / ``spammed`` → Complained, anything else → Not Subscribed. Through
  ``contacts.set_status``, so an address already suppressed on the POS is never loosened, and an
  import that says suppressed overrides a Subscribed one;
* tags keep their slugs; ``sent-*`` / ``opened-*`` are not tags; the lists ``newsletter``,
  ``customers`` and ``cga-insider-list`` become tags of the same name; ``custom.interests``
  becomes ``interest-*`` tags. Tags are only ever added;
* ``campaigns`` (recent ``sent-`` / ``opened-`` tags) become Sent / Opened events;
* back-in-stock notes become Marketing Requests: the product by its ``(#id)`` marker first, then
  the product link, then the product name; a note marked ``[Notified …]`` is a Notified request.
  A note that names nothing goes in the report;
* each contact gets one Imported event (with its old status and source).

Running it again changes nothing that the first run wrote (every write is keyed: the contact by
email, events by ``dedupe_key``, requests by contact + target + request time). The dry run reports
exactly what the real run then writes, the same numbers.
"""
from __future__ import annotations

import json
import re
from collections import Counter

import frappe
from frappe import _
from frappe.utils import now_datetime

from ffl_core.marketing import CONTACT, contacts, requests, tags
from ffl_core.utils import commit

SOURCE = "import:fluentcrm"
STATUS_MAP = {
	"subscribed": contacts.SUBSCRIBED,
	"unsubscribed": contacts.UNSUBSCRIBED,
	"bounced": contacts.BOUNCED,
	"complained": contacts.COMPLAINED,
	"spammed": contacts.COMPLAINED,
}
LIST_TAGS = (tags.NEWSLETTER, tags.CUSTOMERS, tags.INSIDER)
CAMPAIGN_TAG_PREFIXES = ("sent-", "opened-")
CAMPAIGN_CAP = 100   # keeps "opened:<campaign>:<email>" inside the dedupe key column
INTEREST_LABEL = "interest: "   # how a refused interest is named in the report

_NOTE_PREFIX = "Asked about:"
_ID = re.compile(r"\(#(\d+)\)")
_URL = re.compile(r"https?://[^\s\"'<>\\`]+")
_NAME_END = re.compile(r" \(#\d+\)| — ")
_NOTIFIED = re.compile(r"\[Notified(?: ([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}))?(?:[^\]]*?product #(\d+))?")


def import_fluentcrm(path: str, dry_run=1) -> dict:
	"""Import a FluentCRM export file; the report (see :func:`import_document`). Writes only when
	*dry_run* is exactly 0 / "0" / False; any other value, or none, is a dry run."""
	with open(path, encoding="utf-8") as fh:
		document = json.load(fh)
	return import_document(document, write=dry_run in (0, "0", False))


def parse_note(description) -> dict | None:
	"""A back-in-stock note (``Asked about: <name> (#<id>) — <url>``, older ones without the id or the
	link, maybe followed by ``[Notified <Y-m-d H:i> — product #<id>]``) as {web_product_id, url,
	label, notified, notified_at}; None when it is not one or names no product."""
	text = str(description or "")
	first = text.strip().split("\n", 1)[0].strip()
	if not first.startswith(_NOTE_PREFIX):
		return None
	body = first[len(_NOTE_PREFIX):].strip()
	marker, link = _ID.search(body), _URL.search(body)
	name = _NAME_END.split(body, maxsplit=1)[0].strip()
	if link and name.startswith(link.group(0)):
		name = ""
	notified = _NOTIFIED.search(text)
	product_id = marker.group(1) if marker else (notified.group(2) if notified and notified.group(2) else "")
	url = link.group(0) if link else ""
	if not (product_id or url or name):
		return None
	return {"web_product_id": product_id, "url": url if len(url) <= 500 else "", "label": name,
		"notified": bool(notified),
		"notified_at": contacts.to_system_datetime(notified.group(1)) if notified and notified.group(1) else None}


def _slugify(value) -> str:
	return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")


def _tags_of(row: dict, report: dict) -> list[str]:
	out: list[str] = []
	for slug in row.get("tags") or []:
		slug = slug if isinstance(slug, str) else ""
		if slug.startswith(CAMPAIGN_TAG_PREFIXES):
			continue
		if tags.is_slug(slug):
			out.append(slug)
		else:
			report["skipped_tags"][contacts.clip(slug, 60)] += 1
	for slug in row.get("lists") or []:
		if slug in LIST_TAGS:
			out.append(slug)
		else:
			report["skipped_lists"][contacts.clip(slug, 60)] += 1
	custom = row.get("custom") if isinstance(row.get("custom"), dict) else {}
	interests = custom.get("interests")
	values = interests if isinstance(interests, list) else re.split(r"[,;|\n]+", interests) if isinstance(interests, str) else []
	for value in values:
		if not str(value or "").strip():
			continue
		tag = tags.interest(_slugify(value))
		if tag:
			out.append(tag)
		else:
			report["skipped_tags"][contacts.clip(INTEREST_LABEL + str(value), 60)] += 1
	return list(dict.fromkeys(out))



def _known_keys() -> set[str]:
	return set(frappe.db.sql_list("""select dedupe_key from `tabMarketing Event`
		where dedupe_key like 'import:%%' or dedupe_key like 'sent:%%' or dedupe_key like 'opened:%%'"""))


def _new_report(write: bool) -> dict:
	return {"dry_run": not write, "contacts": 0, "create": 0, "update": 0, "unchanged": 0, "invalid": [],
		"duplicates": [], "file_by_status": Counter(), "by_status": Counter(), "by_tag": Counter(),
		"conflicts": [], "requests": Counter(), "unparsed_notes": [], "events": Counter(),
		"campaign_emails_unknown": 0, "skipped_tags": Counter(), "skipped_lists": Counter()}


def import_document(document: dict, *, write: bool) -> dict:
	"""Import an export already parsed; the report. With *write* False nothing is written.

	The report: ``contacts`` (rows in the file), ``create`` / ``update`` / ``unchanged`` (contacts),
	``invalid`` / ``duplicates`` (rows skipped), ``file_by_status`` (the export's statuses),
	``by_status`` (the statuses those contacts have after the import), ``by_tag`` (the tags the
	import gives them), ``conflicts`` (a contact already on the POS whose status differs from the
	export's: ``{email, pos, import, result}``), ``requests`` (new ``Open`` / ``Notified``, and
	``existing`` ones skipped), ``unparsed_notes``, ``events`` (new events by kind),
	``campaign_emails_unknown``, ``skipped_tags`` / ``skipped_lists``."""
	if not isinstance(document, dict) or document.get("error") or not isinstance(document.get("contacts"), list):
		frappe.throw(_("This is not a FluentCRM export (no contact list)."), title=_("Import refused"))
	report = _new_report(write)
	exported_at = contacts.to_system_datetime(document.get("exported_at")) or now_datetime()
	state = {"keys": _known_keys(), "emails": set(), "open": set(), "at": set()}
	if write:
		frappe.db.savepoint("mkt_import")
	try:
		for row in document["contacts"]:
			if isinstance(row, dict):
				_contact(row, report, state, exported_at, write)
		for campaign in document.get("campaigns") or []:
			if isinstance(campaign, dict):
				_campaign(campaign, report, state, exported_at, write)
	except Exception:
		if write:
			frappe.db.rollback(save_point="mkt_import")
		raise
	if write:
		commit()
	return {k: dict(sorted(v.items())) if isinstance(v, Counter) else v for k, v in report.items()}


def _contact(row: dict, report: dict, state: dict, exported_at, write: bool) -> None:
	report["contacts"] += 1
	email = contacts.normalize_email(row.get("email"))
	if not email:
		report["invalid"].append(contacts.clip(row.get("email")))
		return
	if email in state["emails"]:
		report["duplicates"].append(email)
		return
	state["emails"].add(email)

	file_status = contacts.clip(row.get("status"), 40).lower()
	report["file_by_status"][file_status or "(none)"] += 1
	target = STATUS_MAP.get(file_status, contacts.NOT_SUBSCRIBED)
	current = frappe.db.get_value(CONTACT, email,
		["status", "first_name", "last_name", "source", "legacy_hash", "legacy_hash_2"], as_dict=True)
	pos_status = current.status if current else contacts.NOT_SUBSCRIBED
	result = target if contacts.RANK[target] > contacts.RANK[pos_status] else pos_status
	report["by_status"][result] += 1
	if current and target != pos_status:
		report["conflicts"].append({"email": email, "pos": pos_status, "import": target, "result": result})

	wanted = _tags_of(row, report)
	for tag in wanted:
		report["by_tag"][tag] += 1
	have = set(tags.of(email)) if current else set()
	new_tags = [t for t in wanted if t not in have]

	hashes = row.get("hashes") if isinstance(row.get("hashes"), dict) else {}
	values = {"first_name": contacts.safe_text(row.get("first_name")), "last_name": contacts.safe_text(row.get("last_name")),
		"source": SOURCE, "legacy_hash": contacts.clip(hashes.get("managed")),
		"legacy_hash_2": contacts.clip(hashes.get("plain"))}
	fills = {k: v for k, v in values.items() if v and not (current or {}).get(k)}
	for k in ("legacy_hash", "legacy_hash_2"):   # the old system's current link keys win
		if values[k] and current and current.get(k) and current.get(k) != values[k]:
			fills[k] = values[k]
	if not current:
		report["create"] += 1
	elif new_tags or fills or result != pos_status:
		report["update"] += 1
	else:
		report["unchanged"] += 1

	imported_key = f"import:fluentcrm:{email}"
	new_imported = imported_key not in state["keys"]
	if new_imported:
		state["keys"].add(imported_key)
		report["events"][contacts.IMPORTED] += 1
	if result != pos_status:
		report["events"][result] += 1
	planned = _requests_of(email, current, row, report, state, exported_at)

	if not write:
		return
	doc = contacts.get_or_create(email, first_name=fills.get("first_name"), last_name=fills.get("last_name"),
		source=SOURCE)
	keys = {k: fills[k] for k in ("legacy_hash", "legacy_hash_2") if k in fills}
	if keys:
		doc.update(keys)
		doc.save(ignore_permissions=True)
	if result != pos_status:
		suppressed = contacts.is_suppressed(result)
		contacts.set_status(doc, result, channel=SOURCE,
			reason=(contacts.safe_text(row.get("unsubscribe_reason")) or _("Imported")) if suppressed else None,
			occurred_at=None if suppressed else contacts.to_system_datetime(row.get("created_at")))
	tags.add(email, *new_tags)
	if new_imported:
		contacts.log_event(email, contacts.IMPORTED, occurred_at=now_datetime(), dedupe_key=imported_key,
			detail=contacts.event_detail(status=file_status, source=contacts.clip(row.get("source")),
				contact_type=contacts.clip(row.get("contact_type")), created_at=contacts.clip(row.get("created_at"))))
	for kind, fields, status, requested_at, notified_at in planned:
		requests.add(email, kind, **fields, item=requests.item_for("", fields["web_product_id"]), status=status,
			requested_at=requested_at, notified_at=notified_at)


def _requests_of(email: str, current, row: dict, report: dict, state: dict, exported_at) -> list[tuple]:
	planned = []
	for note in row.get("bis_notes") or []:
		description = note.get("description") if isinstance(note, dict) else None
		parsed = parse_note(description)
		if not parsed:
			report["unparsed_notes"].append({"email": email, "note": contacts.clip(description, 200)})
			continue
		kind = requests.BACK_IN_STOCK
		status = requests.NOTIFIED if parsed["notified"] else requests.OPEN
		fields = {"web_product_id": parsed["web_product_id"], "url": parsed["url"],
			"label": contacts.safe_text(parsed["label"]), "sku": "", "model_key": ""}
		requested_at = contacts.to_system_datetime(note.get("created_at")) or exported_at
		target = requests.target(kind, fields)
		at_key = (email, kind, target, requested_at)
		if (status == requests.OPEN and (email, kind, target) in state["open"]) or at_key in state["at"] \
				or (current and requests.existing(email, kind, fields, status=status, requested_at=requested_at)):
			report["requests"]["existing"] += 1
			continue
		state["at"].add(at_key)
		if status == requests.OPEN:
			state["open"].add((email, kind, target))
		report["requests"][status] += 1
		planned.append((kind, fields, status, requested_at, parsed["notified_at"]))
	return planned


# lazy: one document insert per Sent / Opened event (about a minute for 10k); upgrade path: frappe.db.bulk_insert
# with the dedupe keys pre-filtered, if a store's export runs to hundreds of thousands.
def _campaign(campaign: dict, report: dict, state: dict, exported_at, write: bool) -> None:
	name = contacts.clip(campaign.get("campaign"), CAMPAIGN_CAP)
	if not name:
		return
	for kind, prefix, emails in ((contacts.SENT, "sent", campaign.get("sent")), (contacts.OPENED, "opened", campaign.get("opened"))):
		for raw in emails if isinstance(emails, list) else []:
			email = contacts.normalize_email(raw)
			if not email or (email not in state["emails"] and not frappe.db.exists(CONTACT, email)):
				report["campaign_emails_unknown"] += 1
				continue
			key = f"{prefix}:{name}:{email}"
			if key in state["keys"]:
				continue
			state["keys"].add(key)
			report["events"][kind] += 1
			if write:
				contacts.log_event(email, kind, campaign=name, occurred_at=exported_at, dedupe_key=key)
