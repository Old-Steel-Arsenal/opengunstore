"""What never goes into a customer's memory, and what is blanked before a message reaches a provider.

Pure functions, no database. Three uses (``crm.memory``):

1. :func:`redact` runs on every customer message before it is put in front of a provider: ID and card
   numbers, dates of birth and background-check numbers become ``[removed]``, and a whole sentence that
   speaks of something that can bar a purchase (a conviction, a protective order, a denial) does too.
2. :func:`memory_problems` checks a provider's answer against the context it was given; the provider may
   retry with the problems listed, and drops what is still wrong.
3. :func:`has_sensitive` / :func:`has_price_or_stock` run once more on every memory row before it is
   written: an AI row that fails is dropped, a staff edit that fails is refused.

The lists err towards removing: what is lost is one sentence of a message, or one memory row. They do not
remove an ordinary buying sentence: a model number next to "CCW" or "ID", "committed to buying", a mention
of the 4473.
"""
from __future__ import annotations

import re

REMOVED = "[removed]"
KINDS = ("want", "budget", "state", "trade_in", "preference", "question")
MAX_AMOUNT = 1_000_000

_I = re.I

# lazy: English keyword and pattern lists, matched without context; upgrade path: extend them as misses and
# false hits turn up in the stores' Memory cards, or have the provider classify a message first.

# A number right after its label: only punctuation and "no", "number", "is" between them (not "CCW, looking
# for a P365XL"), and a value with enough digits to be an ID, not a model ("P320-XCOMPACT", "1911-A1" under 5).
_GAP = r"[\s:#.]*(?:(?:no|nos|num|number|is|was)\b[\s:#.]*){0,3}"
_VALUE = r"([A-Za-z0-9][A-Za-z0-9-]*)\b"
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_SSN_LABELLED = re.compile(r"\b(?:ssn|social\s+security)\b[^\d\n]{0,20}((?:\d[\s-]?){8}\d)\b", _I)
_ID_LABEL = r"(?:driver'?s?\s+licen[cs]e|licen[cs]e|dl|id|identification|passport|permit|ccw|chl|ltc)"
_ID_LABELLED = re.compile(r"\b" + _ID_LABEL + r"(?:\s+" + _ID_LABEL + r")?\b" + _GAP + _VALUE, _I)   # "CCW permit no."
_CHECK_LABELLED = re.compile(r"\b(?:ntn|nics(?:\s+transaction)?|upin|ppn)\b" + _GAP + _VALUE, _I)
_ID_DIGITS, _CHECK_DIGITS = 5, 3
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = (r"(?:\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2}|" + _MONTH + r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
	r"|\d{1,2}\s+" + _MONTH + r",?\s+\d{4})")
_BIRTH_LABEL = r"\b(?:dob|d\.o\.b\.?|date\s+of\s+birth|born|birthday|birth\s+date)\b"
_BIRTH = re.compile(_BIRTH_LABEL + r"[^\n]{0,15}?(" + _DATE + ")", _I)
# A year alone only right after the label ("born in 1975"), not "a birthday gift, a 1911".
_BIRTH_YEAR = re.compile(_BIRTH_LABEL + r"[\s:]*(?:in\s+)?((?:19|20)\d{2})\b", _I)
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

# A sentence with one of these is about something that can bar a purchase: removed whole.
_BARRING = re.compile(r"|".join((
	r"\bfelon(?:y|ies|s)?\b", r"\bconvict(?:ed|ion|ions)?\b", r"\bmisdemeanou?r", r"\bdomestic\s+violence\b",
	r"\brestraining\s+order", r"\bprotective\s+order", r"\bprobation\b", r"\bparole", r"\bindict(?:ed|ment)?\b",
	r"\bfugitive", r"\bdishonorabl[ey]\s+discharg", r"\binvoluntar(?:y|ily)\s+commit",
	r"\bcommitted\s+(?:to\s+)?(?:a\s+|an\s+|the\s+)?(?:mental|psychiatric|psych\b|hospital|institution|asylum|facility|ward)",
	r"\bmental\s+(?:health|institution|illness)",
	r"\badjudicated\b", r"\bunlawful\s+user\b", r"\bmarijuana\b", r"\bcannabis\b",
	r"\brenounced?\s+(?:my\s+|his\s+|her\s+)?citizenship", r"\billegal(?:ly)?\s+alien", r"\bprohibited\s+person",
	r"\b(?:nics|background\s+check).{0,20}\b(?:denied|delayed|proceed)", r"\bdenied.{0,20}\b(?:nics|background)",
)), _I)
# A question on these is the draft's handoff to a person, never a memory row.
_COMPLIANCE_TOPIC = re.compile(r"|".join((
	r"\blegal", r"\ballowed\b", r"\bship\w*\s+(?:\w+\s+){0,2}to\b", r"\bage\b", r"\bbackground\s+check", r"\bnfa\b",
	r"\btransfer\s+eligib", r"\b4473\b",
)), _I)
_PRICE_STOCK = re.compile(r"|".join((
	r"\$\s?\d", r"\b\d[\d,]*(?:\.\d+)?\s*dollars?\b", r"\bin\s+stock\b", r"\bout\s+of\s+stock\b", r"\bsold\s+out\b",
	r"\bon\s+hand\b", r"\bwe\s+have\s+\d", r"\bavailable\s+now\b",
)), _I)
_SENTENCES = re.compile(r"((?<=[.!?])[ \t]+|\n+)")


def _luhn(digits: str) -> bool:
	total = 0
	for i, c in enumerate(reversed(digits)):
		d = int(c)
		if i % 2:
			d *= 2
			if d > 9:
				d -= 9
		total += d
	return total % 10 == 0


def _card(match: re.Match) -> bool:
	digits = re.sub(r"\D", "", match.group(0))
	return 13 <= len(digits) <= 19 and _luhn(digits)


def _id_number(digits: int):
	def keep(match: re.Match) -> bool:
		value = match.group(1)
		return 5 <= len(value) <= 20 and sum(c.isdigit() for c in value) >= digits
	return keep


def _sub_group(pattern: re.Pattern, text: str, keep=lambda m: True) -> str:
	"""Each match's group 1 replaced by REMOVED, the label kept."""
	def put(m: re.Match) -> str:
		if not keep(m):
			return m.group(0)
		start, end = m.span(1)
		base = m.start(0)
		whole = m.group(0)
		return whole[:start - base] + REMOVED + whole[end - base:]
	return pattern.sub(put, text)


def _numbers(text: str) -> str:
	text = _sub_group(_SSN_LABELLED, text)
	text = _SSN.sub(REMOVED, text)
	text = _CARD.sub(lambda m: REMOVED if _card(m) else m.group(0), text)
	text = _sub_group(_BIRTH, text)
	text = _sub_group(_BIRTH_YEAR, text)
	text = _sub_group(_CHECK_LABELLED, text, _id_number(_CHECK_DIGITS))
	return _sub_group(_ID_LABELLED, text, _id_number(_ID_DIGITS))


def redact(text: str | None) -> str:
	"""*text* with ID, card and check numbers and dates of birth blanked, and every sentence about
	something that can bar a purchase replaced by ``[removed]``. Line breaks are kept."""
	if not text:
		return text or ""
	parts = _SENTENCES.split(_numbers(text))
	return "".join(p if i % 2 or not _BARRING.search(p) else REMOVED for i, p in enumerate(parts))


def has_sensitive(text: str | None) -> bool:
	"""Whether *text* holds a number :func:`redact` blanks, or speaks of something that can bar a purchase."""
	return bool(text) and (_numbers(text) != text or bool(_BARRING.search(text)))


def has_price_or_stock(text: str | None) -> bool:
	"""A price (``$``, "dollars") or a word about stock: neither belongs in a memory row or a summary."""
	return bool(text) and bool(_PRICE_STOCK.search(text))


def is_compliance_question(text: str | None) -> bool:
	"""A question about what the law allows: answered by a person, not remembered."""
	return bool(text) and (bool(_COMPLIANCE_TOPIC.search(text)) or bool(_BARRING.search(text)))


def row_problem(kind: str, text: str | None, keywords: str | None = None) -> str | None:
	"""Why a memory row of *kind* with this text must not be stored, or None."""
	for value in (text, keywords):
		if has_sensitive(value):
			return "holds an ID number or eligibility details"
		if has_price_or_stock(value):
			return "holds a price or stock (a budget goes in amount only)"
	if kind == "question" and is_compliance_question(text):
		return "is a legal or compliance question"
	return None


def _amount_ok(value) -> bool:
	try:
		return 0 < float(value) <= MAX_AMOUNT
	except (TypeError, ValueError):
		return False


def memory_problems(answer, context: dict) -> list[dict]:
	"""What is wrong with a ``gunshop_crm_memory_update`` answer for *context*: one
	``{"part": "add" | "update" | "remove" | "summary" | "answer", "index": int | None, "problem": str}``
	per entry at fault, an empty list when the answer can be written as it is. ``index`` is the entry's
	position in its list. The core drops each entry at fault anyway; this lets a provider ask again."""
	if not isinstance(answer, dict):
		return [{"part": "answer", "index": None, "problem": "the answer is not an object"}]
	sources = {m.get("ref") for m in context.get("new") or []}
	rows = {m.get("ref"): m for m in context.get("memory") or []}
	out = []

	def bad(part, index, problem):
		out.append({"part": part, "index": index, "problem": problem})

	for part in ("add", "update"):
		entries = answer.get(part) or []
		if not isinstance(entries, list):
			bad(part, None, f"{part} is not a list")
			continue
		for i, entry in enumerate(entries):
			if not isinstance(entry, dict):
				bad(part, i, "not an object")
				continue
			if entry.get("source") not in sources:
				bad(part, i, "source is not one of the new messages")
				continue
			if part == "update":
				row = rows.get(entry.get("ref"))
				if not row:
					bad(part, i, "ref is not a memory row")
					continue
				if row.get("locked"):
					bad(part, i, "the row is locked (staff wrote it)")
					continue
				kind = row.get("kind")
			else:
				kind = entry.get("kind")
				if kind not in KINDS:
					bad(part, i, "unknown kind")
					continue
			if kind == "budget" and not _amount_ok(entry.get("amount")):
				bad(part, i, "a budget needs an amount above 0")
				continue
			problem = row_problem(kind, entry.get("text"), entry.get("keywords"))
			if problem:
				bad(part, i, problem)
	removes = answer.get("remove") or []
	for i, ref in enumerate(removes if isinstance(removes, list) else []):
		row = rows.get(ref)
		if not row:
			bad("remove", i, "ref is not a memory row")
		elif row.get("locked"):
			bad("remove", i, "the row is locked (staff wrote it)")
	summary = answer.get("summary")
	if summary is not None and (not isinstance(summary, str) or has_sensitive(summary) or has_price_or_stock(summary)):
		bad("summary", None, "the summary holds a price, stock, an ID number or eligibility details")
	return out
