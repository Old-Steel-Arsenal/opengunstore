"""Local catalog write side — a port of the Data Service's store to MariaDB.

**Change sequence.** Each house keeps its own counter in ``dc_meta.change_seq``,
read ``FOR UPDATE`` and advanced only inside that house's write lock. The change
feed's cursor is already one number per house (``rsr:12;ss:5``), and a per-house
counter advanced under the house lock makes sequence order exactly commit order
within the house — the property the cursor depends on. A MariaDB SEQUENCE or an
AUTO_INCREMENT column allocates at statement time, not commit time (and burns a
value on every ``ON DUPLICATE KEY`` hit), so it gives no such guarantee and adds
an object backups must carry. A write stages its rows with a running number
``rn``; a changed offer gets ``base + 2*rn`` and the product it moved off (a UPC
change) ``base + 2*rn - 1``, so every number in one write is unique and above
everything committed before it. Gaps are fine: readers only compare.

**Change detection.** ``change_seq`` moves only when a compared column differs,
null-safe (``<=>``); re-loading an unchanged file writes nothing that matters.

**Locking.** The big writers try ``GET_LOCK('dc_write:<house>', 0)``. A miss means
another lane of the same house is mid-write: the caller gets ``LOCK_SKIPPED``, the
skip is counted in ``dc_meta`` and no ``dc_sync_run`` row is written (a skip is
neither a success nor a failure). The lock is released in ``finally`` after the
commit, so the next writer of the house sees every row this one wrote.

**Isolation.** Writers commit before they start and stage into a temporary table
(no implicit commit in MariaDB; Frappe's guard needs no pending writes when it is
created). Lane jobs run at READ COMMITTED (``read_committed``): under REPEATABLE
READ a writer that read a row before another session committed a change to it
fails with error 1020 on its UPDATE (memory: gunstore-pos-repeatable-read). A
session reopened inside ``read_committed`` (``sync._reconnect``) is set back to
READ COMMITTED before the next lane.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from contextlib import contextmanager

import frappe

from ffl_integrations.catalog_local.schema import (
	BOOL_COLUMNS,
	DERIVED_COLUMNS,
	MONEY_COLUMNS,
	OFFER_COLUMNS,
)

LOCK_SKIPPED = {"lock_skipped": True}

PARTIAL_FILE_FLOOR = 10_000     # guard only when the house already has real data
PARTIAL_FILE_FRACTION = 0.5
UNCONFIGURED_PRIORITY = 10_000  # a house no Distributor row ranks
# Staging chunk: rows per INSERT, and a byte budget well under max_allowed_packet
# (16 MiB by default; a long description can be tens of KB).
CHUNK_ROWS = 1000
CHUNK_BYTES = 1 << 20
REFRESH_CHUNK = 500

CONTENT_COLS = tuple(name for name, _ddl in OFFER_COLUMNS)
# A parser that stops emitting one of these must fail the load, not write blanks.
REQUIRED_KEYS = ("sku", "upc", "manufacturer", "model", "department", "cost", "qty",
	"short_description")
_MAXLEN = {name: int(m.group(1)) for name, ddl in OFFER_COLUMNS
	if (m := re.match(r"VARCHAR\((\d+)\)", ddl))}
# Text columns compare byte-wise: the table collation is case-insensitive, and a
# case-only edit (an image URL, a brand spelling) is a real change.
TEXT_COLS = frozenset(name for name, ddl in OFFER_COLUMNS if re.match(r"(VARCHAR|MEDIUMTEXT|TEXT)", ddl))
# search_text's sources after sku, in search_text()'s order: the IODKU rebuilds
# it from the row's final values with the same order and separator.
SEARCH_TEXT_COLS = ("upc", "mfg_number", "manufacturer", "model", "short_description")
SKU_MAXLEN = 64
_DEFAULTS = {name: (1 if name == "ffl_required" else 0) if name in BOOL_COLUMNS
	else 0.0 if name in MONEY_COLUMNS else 0 if name == "qty" else "" for name in CONTENT_COLS}


def _log():
	return frappe.logger("catalog_local", allow_site=True)


# ------------------------------------------------------------------ pure helpers

def norm_sku(raw) -> str:
	"""The one SKU spelling every writer stores and matches: trimmed, at most the
	column's length."""
	return str(raw if raw is not None else "").strip()[:SKU_MAXLEN]


def same(a: str, b: str, col: str) -> str:
	"""Null-safe equality of two SQL expressions for column ``col`` — byte-wise
	for text, so a case-only change counts."""
	if col in TEXT_COLS:
		return f"CAST({a} AS BINARY) <=> CAST({b} AS BINARY)"
	return f"{a} <=> {b}"


def normalize_upc(raw) -> tuple[str | None, bool]:
	"""``(upc or None, rejected)`` — the Data Service's boundary rule. Blank is an orphan
	(no UPC), not a reject; 12 digits verbatim; 13 digits with a leading zero
	become the 12-digit UPC-A; any other 13-digit code is a true EAN-13 and is
	kept; anything else is stored as an orphan and counted as a reject."""
	v = str(raw or "").strip()
	if not v:
		return None, False
	if v.isdigit():
		if len(v) == 12:
			return v, False
		if len(v) == 13:
			return (v[1:], False) if v[0] == "0" else (v, False)
	return None, True


def brand_key(value) -> str:
	"""The brand folded for matching: case, punctuation and '&' vs 'and' ignored,
	spelling kept (the service's fold)."""
	s = str(value or "").strip().lower().replace("&", " and ")
	return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def product_key(distributor: str, sku: str, upc) -> str:
	return upc or f"{distributor}:{sku}"


def default_class(row: dict) -> str:
	"""The offer's product class when the driver gives none (the shipped RSR
	driver sets its own, ``drivers/rsr_feed.product_class``).
	# lazy: FFL flag only (firearm vs accessory); upgrade path: each driver ports
	# its house's branch of the Data Service's product_class rule."""
	return "Other Firearm" if row["ffl_required"] else "Accessory"


def search_text(sku: str, row: dict) -> str:
	parts = (sku, *(row.get(c) for c in SEARCH_TEXT_COLS))
	return " ".join(str(p) for p in parts if p).lower()


# The one not-orderable rule: stock that cannot be ordered whatever its qty —
# allocated (spoken for), Deleted, or discontinued. ``not_orderable`` and its SQL
# twin ``not_orderable_sql`` are its only spellings: the RSR parser, the quantity
# lane, the readers' in-stock filters, ``orderable_qty``, the offers the backend
# serves and the qty sort all go through them. (The table collation is
# case-insensitive, so the SQL matches 'ALLOCATED' as the Python lower() does.)
# ``is_deleted``/``deleted_sql`` are the one Deleted check (the live filter, the best
# offer, the product layer, the quantity lane).
NOT_ORDERABLE_STATUS = ("Allocated", "Deleted")
_NOT_ORDERABLE_FOLDED = frozenset(s.lower() for s in NOT_ORDERABLE_STATUS)


def _status_key(status_flag) -> str:
	return str(status_flag or "").strip().lower()


def is_deleted(status_flag) -> bool:
	"""A Deleted offer, whatever the case or the spaces around the flag."""
	return _status_key(status_flag) == "deleted"


def deleted_sql(status: str = "status_flag") -> str:
	"""SQL twin of ``is_deleted`` (the table collation is case-insensitive)."""
	return f"(TRIM({status}) = 'Deleted')"


def not_orderable(status_flag, discontinued=False) -> bool:
	return bool(discontinued) or _status_key(status_flag) in _NOT_ORDERABLE_FOLDED


def not_orderable_sql(status: str = "status_flag", discontinued: str = "discontinued") -> str:
	"""SQL twin of ``not_orderable`` over two column expressions."""
	flags = ", ".join(f"'{s}'" for s in NOT_ORDERABLE_STATUS)
	return f"({discontinued} OR TRIM({status}) IN ({flags}))"


ORDERABLE_SQL = f"NOT {not_orderable_sql()}"
# The qty a buyer can order, per offer row (what the qty sort ranks by).
ORDERABLE_QTY_SQL = f"IF({not_orderable_sql()}, 0, GREATEST(qty, 0))"


def orderable_qty(offer: dict) -> int:
	"""The offer's qty as stock a buyer can order (0 when not orderable)."""
	if not_orderable(offer.get("status_flag"), offer.get("discontinued")):
		return 0
	return max(int(offer.get("qty") or 0), 0)


def offer_sort_key(offer: dict, priorities: dict):
	"""The cross-house merge rule (the Data Service's merge rule): orderable
	stock first, then lowest known cost (``<= 0`` = unknown = worst), then house
	priority, then sku."""
	cost = float(offer.get("cost") or 0)
	return (0 if orderable_qty(offer) > 0 else 1,
		cost if cost > 0 else float("inf"),
		priorities.get(offer["distributor"], UNCONFIGURED_PRIORITY),
		offer["sku"])


def utc_now():
	"""Naive UTC — how every local catalog timestamp is stored."""
	from datetime import UTC, datetime

	return datetime.now(UTC).replace(tzinfo=None)


def priority_map() -> dict:
	"""``{house code: priority}`` — the best (lowest) Distributor.priority among
	the rows buying through each house."""
	out: dict = {}
	for r in frappe.get_all("Distributor", fields=["provider", "priority"]):
		if r.provider:
			p = int(r.priority or 0) or UNCONFIGURED_PRIORITY
			out[r.provider] = min(out.get(r.provider, p), p)
	return out


def _clean(row: dict, active=CONTENT_COLS) -> dict:
	"""A parsed feed row → the stored column values (types, defaults, lengths).
	Only the columns this lane writes (``active``) are required."""
	missing = [k for k in REQUIRED_KEYS if k not in row and (k == "sku" or k in active)]
	if missing:
		raise KeyError(f"catalog row is missing {', '.join(missing)}")
	out = {}
	for c in CONTENT_COLS:
		v = row.get(c, _DEFAULTS[c])
		if c == "ffl_required":
			# The distributor's explicit flag only; no flag (None/absent) = a
			# firearm. Nothing here may downgrade a flagged one.
			v = 1 if v is None else int(bool(v))
		elif c in BOOL_COLUMNS:
			v = int(bool(v))
		elif c in MONEY_COLUMNS:
			v = round(float(v or 0), 3 if c == "weight_lb" else 2)
		elif c == "qty":
			v = int(v or 0)
		elif c == "upc":
			continue
		elif c == "long_description":
			v = str(v or "")
		else:
			v = str(v or "").strip()[:_MAXLEN.get(c, 10_000)]
		out[c] = v
	return out


# ------------------------------------------------------------------ session plumbing

_RC_FLAG = "catalog_local_read_committed"


@contextmanager
def read_committed():
	"""Run a lane at READ COMMITTED, then put the session back as it was. The
	setting applies to the NEXT transaction, hence the commits around it. While
	inside, a reconnected session gets it again (``reapply_isolation``)."""
	previous = _isolation()
	outer = getattr(frappe.local, _RC_FLAG, False)
	frappe.db.commit()
	_set_read_committed()
	setattr(frappe.local, _RC_FLAG, True)
	try:
		yield
	finally:
		setattr(frappe.local, _RC_FLAG, outer)
		frappe.db.rollback()
		frappe.db.sql(f"SET SESSION TRANSACTION ISOLATION LEVEL {previous}")
		frappe.db.commit()


def _set_read_committed() -> None:
	frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
	frappe.db.commit()


def reapply_isolation() -> None:
	"""After a reconnect: a fresh session starts at the server default, so put it
	back at READ COMMITTED when the caller runs inside ``read_committed``."""
	if getattr(frappe.local, _RC_FLAG, False):
		_set_read_committed()


def _isolation() -> str:
	for var in ("@@SESSION.transaction_isolation", "@@SESSION.tx_isolation"):
		try:
			value = frappe.db.sql(f"SELECT {var}")[0][0]
		except Exception:  # noqa: BLE001 — the variable's name moved in MariaDB 11.1
			continue
		if value:
			return str(value).replace("-", " ").upper()
	return "REPEATABLE READ"


def lock_name(distributor: str, db_name: str | None = None) -> str:
	"""GET_LOCK names are server-wide: the database is part of the name, so two
	sites on one MariaDB never block each other. MariaDB caps a name at 64
	characters; a longer one is hashed."""
	name = f"dc_write:{db_name or frappe.conf.db_name or ''}:{distributor}"
	if len(name) > 64:
		import hashlib

		name = "dc_write:" + hashlib.sha1(name.encode()).hexdigest()
	return name


@contextmanager
def write_lock(distributor: str):
	"""Yield whether this session now holds the house's write lock (never waits).
	Released on exit — after the caller's commit. A release that fails (the
	connection died) is logged, never raised: a dead session's locks are gone
	with it, and raising here would turn a committed write — or the caller's own
	error — into a different failure."""
	got = frappe.db.sql("SELECT GET_LOCK(%s, 0)", (lock_name(distributor),))[0][0] == 1
	try:
		yield got
	finally:
		if got:
			try:
				frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name(distributor),))
			except Exception:  # noqa: BLE001
				_log().exception("catalog %s: RELEASE_LOCK failed", distributor)


def record_lock_skip(distributor: str) -> None:
	frappe.db.sql("""INSERT INTO dc_meta (distributor, lock_skip_count, lock_skipped_at)
		VALUES (%s, 1, UTC_TIMESTAMP(6))
		ON DUPLICATE KEY UPDATE lock_skip_count = lock_skip_count + 1,
			lock_skipped_at = UTC_TIMESTAMP(6)""", (distributor,))
	frappe.db.commit()


def _seq_base(distributor: str) -> int:
	frappe.db.sql("INSERT IGNORE INTO dc_meta (distributor) VALUES (%s)", (distributor,))
	return int(frappe.db.sql(
		"SELECT change_seq FROM dc_meta WHERE distributor = %s FOR UPDATE", (distributor,))[0][0])


def _drop_temp(name: str) -> None:
	try:
		frappe.db.sql(f"DROP TEMPORARY TABLE IF EXISTS `{name}`")
	except Exception:  # noqa: BLE001 — the session ends with it anyway
		pass


def _chunk_budget() -> int:
	try:
		packet = int(frappe.db.sql("SELECT @@max_allowed_packet")[0][0])
	except Exception:  # noqa: BLE001
		packet = 16 << 20
	return max(64 << 10, min(CHUNK_BYTES, packet // 4))


def _insert_chunks(table: str, cols: tuple, tuples, *, upsert: bool = False) -> int:
	"""Multi-row INSERTs of at most CHUNK_ROWS rows and a byte budget under
	``max_allowed_packet``. ``upsert``: a later row with the same key replaces
	the earlier one (last occurrence wins). Returns how many rows went in."""
	col_sql = ", ".join(f"`{c}`" for c in cols)
	one = "(" + ", ".join(["%s"] * len(cols)) + ")"
	tail = (" ON DUPLICATE KEY UPDATE " + ", ".join(f"`{c}` = VALUES(`{c}`)" for c in cols)
		if upsert else "")
	budget = _chunk_budget()
	batch, flat, size, total = 0, [], 0, 0

	def flush():
		nonlocal batch, flat, size
		if batch:
			frappe.db.sql(f"INSERT INTO `{table}` ({col_sql}) VALUES "
				+ ", ".join([one] * batch) + tail, flat)
		batch, flat, size = 0, [], 0

	for values in tuples:
		flat.extend(values)
		batch += 1
		total += 1
		size += sum(len(v) if isinstance(v, str) else 8 for v in values)
		if batch >= CHUNK_ROWS or size >= budget:
			flush()
	flush()
	return total


def _guarded(distributor: str, temps: tuple, body):
	"""The writer prologue/epilogue every big write shares: commit first, try the
	house lock (a miss = LOCK_SKIPPED), run ``body`` and commit, roll back on
	error; drop the temp tables after the transaction ended."""
	frappe.db.commit()
	with write_lock(distributor) as got:
		if not got:
			record_lock_skip(distributor)
			return dict(LOCK_SKIPPED)
		try:
			out = body()
			frappe.db.commit()
			return out
		except BaseException:
			# The body's error is the one the caller records: a rollback that
			# fails too (dead connection) is logged, not raised over it.
			try:
				frappe.db.rollback()
			except Exception:  # noqa: BLE001
				_log().exception("catalog %s: rollback after a failed write failed", distributor)
			raise
		finally:
			for t in temps:
				_drop_temp(t)


# ------------------------------------------------------------------ writers

_STAGE = "_dc_stage"


def load_catalog(rows, *, distributor: str, mode: str = "snapshot",
		exclude_cols: tuple = (), insert_only_cols: tuple = (), revive_flags: tuple = (),
		min_rows: int = 0) -> dict:
	"""Upsert a catalog feed for one house → ``{created, updated, unchanged, total}``
	or ``LOCK_SKIPPED``.

	``rows``: any iterable of parsed row dicts, streamed into a temporary table in
	chunks (a generator never materialises in memory). ``total`` counts staged
	lines before in-file de-dup (last occurrence of a SKU wins).

	``mode="snapshot"`` (a full file) refuses a file below ``min_rows``, or below
	half of a house that already holds PARTIAL_FILE_FLOOR offers, before any live
	row is touched. ``"delta"`` is exempt.

	``exclude_cols``: never written by this loader (another lane owns them).
	``insert_only_cols``: seed new rows only; neither compared nor updated (a
	daily file must not overwrite the 15-minute quantity lane's values).
	``revive_flags``: a stored row whose status_flag is one of these (a row a
	disappeared diff retired) and that the file carries again is re-seeded like a
	new row: its insert-only columns take the file's values. A house whose
	status_flag another lane owns (Lipsey's) still brings such a row back. Counted
	as ``revived``.
	"""
	if mode not in ("snapshot", "delta"):
		raise ValueError(f"unknown load mode: {mode!r}")
	active = tuple(c for c in CONTENT_COLS if c not in exclude_cols)
	cmp_cols = tuple(c for c in active if c not in insert_only_cols)
	priorities = priority_map()
	return _guarded(distributor, (_STAGE,), lambda: _load_locked(
		rows, distributor, mode, active, cmp_cols, min_rows, priorities, tuple(revive_flags)))


def _load_locked(rows, distributor, mode, active, cmp_cols, min_rows, priorities,
		revive_flags=()) -> dict:
	ddl = dict(OFFER_COLUMNS)
	stage_cols = ("rn", "sku", *active, *(n for n, _d in DERIVED_COLUMNS))
	_drop_temp(_STAGE)
	frappe.db.sql(f"""CREATE TEMPORARY TABLE `{_STAGE}` (
		`rn` INT NOT NULL,
		`sku` VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
		{", ".join(f"`{c}` {ddl[c]}" for c in active)},
		{", ".join(f"`{n}` {d}" for n, d in DERIVED_COLUMNS)},
		PRIMARY KEY (`sku`)) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")

	counts = {"staged": 0, "rejected": 0}

	def staged():
		for r in rows:
			sku = norm_sku(r.get("sku"))
			if not sku:
				raise KeyError("catalog row has no sku")
			clean = _clean(r, active)
			clean["upc"], rejected = normalize_upc(r["upc"]) if "upc" in active else (None, False)
			if not clean.get("product_class"):
				clean["product_class"] = default_class(clean)
			counts["staged"] += 1
			counts["rejected"] += rejected
			yield (counts["staged"], sku, *(clean[c] for c in active),
				product_key(distributor, sku, clean["upc"]),
				brand_key(clean["manufacturer"])[:255], search_text(sku, clean))

	_insert_chunks(_STAGE, stage_cols, staged(), upsert=True)
	staged_n = counts["staged"]

	existing = int(frappe.db.sql(
		"SELECT COUNT(*) FROM dc_offer WHERE distributor = %s", (distributor,))[0][0])
	if mode == "snapshot":
		if staged_n < min_rows:
			raise ValueError(f"catalog snapshot too small: {staged_n} rows < required "
				f"floor {min_rows} — refusing the file")
		if existing >= PARTIAL_FILE_FLOOR and staged_n < existing * PARTIAL_FILE_FRACTION:
			raise ValueError(f"catalog file looks partial: {staged_n} rows vs {existing} "
				f"existing offers — refusing the file")

	base = _seq_base(distributor)
	same_oi = " AND ".join(same(f"o.`{c}`", f"i.`{c}`", c) for c in cmp_cols) or "1"
	created, updated, seen = (int(x or 0) for x in frappe.db.sql(f"""
		SELECT SUM(o.sku IS NULL), SUM(o.sku IS NOT NULL AND NOT ({same_oi})), COUNT(o.sku)
		FROM `{_STAGE}` i LEFT JOIN dc_offer o ON o.distributor = %s AND o.sku = i.sku""",
		(distributor,))[0])

	# A UPC move exists only when this lane updates upc: an insert-only upc never
	# leaves the stored one, so its product did not lose the offer.
	upc_scope: set = set()
	moved = 0
	if "upc" in cmp_cols:
		upc_scope |= {r[0] for r in frappe.db.sql(f"""SELECT DISTINCT o.upc FROM dc_offer o
			JOIN `{_STAGE}` i ON o.distributor = %s AND o.sku = i.sku
			WHERE o.upc IS NOT NULL AND NOT (o.upc <=> i.upc)""", (distributor,))}
		moves = f"""FROM dc_offer o JOIN `{_STAGE}` i ON o.distributor = %s AND o.sku = i.sku
			WHERE NOT (o.upc <=> i.upc)"""
		moved = int(frappe.db.sql(f"SELECT COUNT(*) {moves}", (distributor,))[0][0])
		if moved:
			frappe.db.sql(f"""INSERT INTO dc_product_move (distributor, product_key, change_seq,
				created_at) SELECT o.distributor, o.product_key, %s + 2 * i.rn - 1,
				UTC_TIMESTAMP(6) {moves}""", (base, distributor))

	derived_cols = tuple(n for n, _d in DERIVED_COLUMNS)
	insert_cols = ("distributor", "sku", *active, *derived_cols, "change_seq", "created_at",
		"updated_at")
	select_cols = ("%s", "i.sku", *(f"i.`{c}`" for c in (*active, *derived_cols)),
		"%s + 2 * i.rn", "UTC_TIMESTAMP(6)", "UTC_TIMESTAMP(6)")
	unchanged = " AND ".join(same(f"dc_offer.`{c}`", f"VALUES(`{c}`)", c) for c in cmp_cols) or "1"
	# change_seq and updated_at FIRST: MariaDB applies the assignments left to
	# right, so the compare still sees the row's old content ...
	sets = [f"`change_seq` = IF({unchanged}, dc_offer.`change_seq`, VALUES(`change_seq`))",
		f"`updated_at` = IF({unchanged}, dc_offer.`updated_at`, VALUES(`updated_at`))"]
	sets += [f"`{c}` = VALUES(`{c}`)" for c in cmp_cols]
	# ... and the derived columns LAST, from the row's final values, so a lane
	# that writes only some of their sources still leaves them consistent.
	# brand_key is the Python fold (readers filter with store.brand_key, so it
	# has one implementation): staged when this lane writes manufacturer, kept
	# otherwise — the stored manufacturer, and so its fold, did not change.
	sets.append("`product_key` = COALESCE(NULLIF(dc_offer.`upc`, ''), "
		"CONCAT(dc_offer.`distributor`, ':', dc_offer.`sku`))")
	if "manufacturer" in cmp_cols:
		sets.append("`brand_key` = VALUES(`brand_key`)")
	sets.append("`search_text` = LOWER(CONCAT_WS(' ', dc_offer.`sku`, "
		+ ", ".join(f"NULLIF(dc_offer.`{c}`, '')" for c in SEARCH_TEXT_COLS) + "))")
	frappe.db.sql(f"""INSERT INTO dc_offer ({", ".join(f"`{c}`" for c in insert_cols)})
		SELECT {", ".join(select_cols)} FROM `{_STAGE}` i
		ON DUPLICATE KEY UPDATE {", ".join(sets)}""", (distributor, base))

	# A retired row the file carries again: the upsert above wrote its compared
	# columns (discontinued among them) but not its insert-only ones, so seed those.
	revived = 0
	seed_cols = tuple(c for c in active if c not in cmp_cols)
	if revive_flags and seed_cols:
		args = {"d": distributor, "flags": revive_flags, "base": base}
		revive_join = f"""dc_offer o JOIN `{_STAGE}` i ON o.distributor = %(d)s AND o.sku = i.sku
			WHERE TRIM(o.status_flag) IN %(flags)s"""
		revived = int(frappe.db.sql(f"SELECT COUNT(*) FROM {revive_join}", args)[0][0])
		if revived:
			frappe.db.sql(f"""UPDATE dc_offer o JOIN `{_STAGE}` i ON o.distributor = %(d)s
				AND o.sku = i.sku SET {", ".join(f"o.`{c}` = i.`{c}`" for c in seed_cols)},
				o.change_seq = %(base)s + 2 * i.rn, o.updated_at = UTC_TIMESTAMP(6)
				WHERE TRIM(o.status_flag) IN %(flags)s""", args)

	# Every number handed out (offers base+2·rn, moves base+2·rn−1, rn <= staged_n)
	# stays at or below the new counter, so none is ever handed out twice.
	if created or updated or moved or revived:
		frappe.db.sql("UPDATE dc_meta SET change_seq = %s WHERE distributor = %s",
			(base + 2 * staged_n, distributor))
	frappe.db.sql("UPDATE dc_meta SET upc_reject_count = %s WHERE distributor = %s",
		(counts["rejected"], distributor))

	# every staged offer's UPC as it now stands (the staged one, or the stored
	# one when this lane does not write upc)
	upc_scope |= {r[0] for r in frappe.db.sql(f"""SELECT DISTINCT o.upc FROM dc_offer o
		JOIN `{_STAGE}` i ON o.distributor = %s AND o.sku = i.sku WHERE o.upc IS NOT NULL""",
		(distributor,))}
	refresh_products(upc_scope, priorities)
	out = {"created": created, "updated": updated,
		"unchanged": max(seen - updated, 0), "total": staged_n, "upc_rejected": counts["rejected"]}
	if revive_flags:
		out["revived"] = revived
	return out


_QTY = "_dc_qty"


def apply_quantity(pairs, *, distributor: str) -> dict:
	"""The quantity lane: ``(sku, qty)``, ``(sku, qty, cost)`` or ``(sku, qty,
	cost, status_flag)``, any iterable (a parser generator streams straight into
	the staging table). A None cost keeps the stored one; a None status keeps
	the stored flag, and a discontinued or Deleted row keeps its terminal flag.
	An Allocated, Deleted or discontinued offer gets qty 0 whatever the file says
	(not orderable). The last line of a SKU wins; unknown SKUs are ignored. → ``{updated, total}``
	(total = distinct SKUs) or ``LOCK_SKIPPED``."""
	priorities = priority_map()
	return _guarded(distributor, (_QTY,), lambda: _quantity_locked(pairs, distributor, priorities))


def _quantity_tuples(pairs):
	"""``(rn, sku, qty, cost, status)`` per non-blank line; ``rn`` is the line
	number, so the last line of a SKU carries the highest one."""
	rn = 0
	for p in pairs:
		sku = norm_sku(p[0])
		if not sku:
			continue
		rn += 1
		yield (rn, sku, int(p[1] or 0),
			None if len(p) < 3 or p[2] is None else round(float(p[2]), 2),
			None if len(p) < 4 else p[3])


def _quantity_locked(pairs, distributor: str, priorities: dict) -> dict:
	_drop_temp(_QTY)
	frappe.db.sql(f"""CREATE TEMPORARY TABLE `{_QTY}` (`rn` INT NOT NULL,
		`sku` VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
		`qty` INT NOT NULL, `cost` DECIMAL(12,2) NULL, `status_flag` VARCHAR(32) NULL,
		PRIMARY KEY (`sku`)) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
	# upsert: a later line of a SKU replaces the earlier one (last write wins)
	lines = _insert_chunks(_QTY, ("rn", "sku", "qty", "cost", "status_flag"),
		_quantity_tuples(pairs), upsert=True)
	total = int(frappe.db.sql(f"SELECT COUNT(*) FROM `{_QTY}`")[0][0])
	base = _seq_base(distributor)
	status = (f"CASE WHEN i.status_flag IS NULL OR o.discontinued OR {deleted_sql('o.status_flag')} "
		"THEN o.status_flag ELSE i.status_flag END")
	qty = f"IF({not_orderable_sql(status, 'o.discontinued')}, 0, i.qty)"
	changed = (f"NOT (o.qty <=> {qty} AND o.cost <=> COALESCE(i.cost, o.cost) "
		f"AND {same('o.status_flag', status, 'status_flag')})")
	join = f"dc_offer o JOIN `{_QTY}` i ON o.distributor = %s AND o.sku = i.sku"
	touched = {r[0] for r in frappe.db.sql(
		f"SELECT DISTINCT o.upc FROM {join} WHERE o.upc IS NOT NULL AND {changed}", (distributor,))}
	updated = int(frappe.db.sql(f"SELECT COUNT(*) FROM {join} WHERE {changed}", (distributor,))[0][0])
	if updated:
		# MariaDB promises no assignment order for a multi-table UPDATE, and none
		# is needed: `status` gives the same answer over the new status_flag as over
		# the old one (it keeps a terminal or unreported flag and otherwise takes
		# the file's), so qty reads the same status in either order.
		frappe.db.sql(f"""UPDATE {join} SET o.change_seq = %s + 2 * i.rn, o.qty = {qty},
			o.cost = COALESCE(i.cost, o.cost), o.status_flag = {status},
			o.qty_updated_at = UTC_TIMESTAMP(6), o.updated_at = UTC_TIMESTAMP(6)
			WHERE {changed}""", (distributor, base))
		# rn <= lines, so every number handed out stays at or below the counter
		frappe.db.sql("UPDATE dc_meta SET change_seq = %s WHERE distributor = %s",
			(base + 2 * lines, distributor))
		# Stock and cost move the per-UPC winner: re-elect the touched products.
		refresh_products(touched, priorities)
	return {"updated": updated, "total": total}


_DEL = "_dc_deleted"


def apply_deleted(skus, *, distributor: str, status_flag: str = "Deleted") -> dict:
	"""The removal channel (the Data Service's apply_deleted): every listed SKU becomes
	discontinued, ``status_flag`` and qty 0. ``skus`` is any iterable (streamed
	into a staging table). A row already in exactly that state is not rewritten
	and keeps its change_seq; unknown SKUs are ignored. Removed offers stop
	leading their product's content, but the product's FFL OR still counts them
	(availability never toggles gun-ness). → ``{flagged, total}`` (total =
	distinct SKUs) or ``LOCK_SKIPPED``."""
	priorities = priority_map()
	return _guarded(distributor, (_DEL,), lambda: _deleted_locked(skus, distributor,
		str(status_flag), priorities))


def _deleted_locked(skus, distributor: str, status_flag: str, priorities: dict) -> dict:
	_drop_temp(_DEL)
	frappe.db.sql(f"""CREATE TEMPORARY TABLE `{_DEL}` (`rn` INT NOT NULL,
		`sku` VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
		PRIMARY KEY (`sku`)) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
	lines = _insert_chunks(_DEL, ("rn", "sku"),
		enumerate(filter(None, (norm_sku(s) for s in skus)), 1), upsert=True)
	total = int(frappe.db.sql(f"SELECT COUNT(*) FROM `{_DEL}`")[0][0])
	base = _seq_base(distributor)
	args = {"d": distributor, "flag": status_flag, "base": base}
	join = f"dc_offer o JOIN `{_DEL}` i ON o.distributor = %(d)s AND o.sku = i.sku"
	changed = f"NOT (o.discontinued <=> 1 AND {same('o.status_flag', '%(flag)s', 'status_flag')} AND o.qty <=> 0)"
	touched = {r[0] for r in frappe.db.sql(
		f"SELECT DISTINCT o.upc FROM {join} WHERE o.upc IS NOT NULL AND {changed}", args)}
	flagged = int(frappe.db.sql(f"SELECT COUNT(*) FROM {join} WHERE {changed}", args)[0][0])
	if flagged:
		frappe.db.sql(f"""UPDATE {join} SET o.change_seq = %(base)s + 2 * i.rn, o.discontinued = 1,
			o.status_flag = %(flag)s, o.qty = 0, o.updated_at = UTC_TIMESTAMP(6)
			WHERE {changed}""", args)
		frappe.db.sql("UPDATE dc_meta SET change_seq = %s WHERE distributor = %s",
			(base + 2 * lines, distributor))
		refresh_products(touched, priorities)
	return {"flagged": flagged, "total": total}


_TXT = "_dc_text"


def apply_text(pairs, *, distributor: str) -> dict:
	"""The long-description lane: ``(sku, text)`` pairs, any iterable (streamed
	into a staging table; the last line of a SKU wins). Empty texts are skipped
	(one never overwrites a description); unknown SKUs are ignored. A changed text
	bumps the offer's change_seq and re-merges its product. -> ``{updated, total}``
	or ``LOCK_SKIPPED``."""
	priorities = priority_map()
	return _guarded(distributor, (_TXT,), lambda: _text_locked(pairs, distributor, priorities))


def _text_tuples(pairs):
	"""``(rn, sku, text)`` per line with a SKU and a non-empty text; ``rn`` is the
	count of kept lines, so the last line of a SKU carries the highest one."""
	rn = 0
	for s, t in pairs:
		sku = norm_sku(s)
		if sku and t:
			rn += 1
			yield (rn, sku, str(t))


def _text_locked(pairs, distributor: str, priorities: dict) -> dict:
	_drop_temp(_TXT)
	frappe.db.sql(f"""CREATE TEMPORARY TABLE `{_TXT}` (`rn` INT NOT NULL,
		`sku` VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
		`long_description` MEDIUMTEXT NOT NULL,
		PRIMARY KEY (`sku`)) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
	lines = _insert_chunks(_TXT, ("rn", "sku", "long_description"), _text_tuples(pairs),
		upsert=True)
	total = int(frappe.db.sql(f"SELECT COUNT(*) FROM `{_TXT}`")[0][0])
	base = _seq_base(distributor)
	join = f"dc_offer o JOIN `{_TXT}` i ON o.distributor = %s AND o.sku = i.sku"
	changed = f"NOT ({same('o.long_description', 'i.long_description', 'long_description')})"
	touched = {r[0] for r in frappe.db.sql(
		f"SELECT DISTINCT o.upc FROM {join} WHERE o.upc IS NOT NULL AND {changed}", (distributor,))}
	updated = int(frappe.db.sql(f"SELECT COUNT(*) FROM {join} WHERE {changed}", (distributor,))[0][0])
	if updated:
		frappe.db.sql(f"""UPDATE {join} SET o.change_seq = %s + 2 * i.rn,
			o.long_description = i.long_description, o.updated_at = UTC_TIMESTAMP(6)
			WHERE {changed}""", (distributor, base))
		frappe.db.sql("UPDATE dc_meta SET change_seq = %s WHERE distributor = %s",
			(base + 2 * lines, distributor))
		refresh_products(touched, priorities)
	return {"updated": updated, "total": total}


_CASE = "_dc_case"


def apply_case_prices(rows, *, distributor: str) -> dict:
	"""The case-price lane: ``(sku, case_cost, units_per_case)`` for the house's
	WHOLE catalog (a snapshot: an offer the rows do not name, or name with no case
	price, is set back to none). ``case_cost`` is per unit. A pair with a case size or
	price of 0 or below stores as none (0, 0); the last line of a SKU wins; unknown
	SKUs are ignored. Only ``case_cost`` / ``units_per_case`` are written — no
	change_seq, no product refresh (a ship-to-store cost, not listing content).
	-> ``{updated, total}`` (total = distinct SKUs staged) or ``LOCK_SKIPPED``."""
	return _guarded(distributor, (_CASE,), lambda: _case_locked(rows, distributor))


def _case_tuples(rows):
	for s, cost, per_case in rows:
		sku = norm_sku(s)
		if not sku:
			continue
		cost, per_case = round(float(cost or 0), 2), int(per_case or 0)
		yield (sku, cost, per_case) if cost > 0 and per_case > 0 else (sku, 0, 0)


def _case_locked(rows, distributor: str) -> dict:
	_drop_temp(_CASE)
	frappe.db.sql(f"""CREATE TEMPORARY TABLE `{_CASE}` (
		`sku` VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
		`case_cost` DECIMAL(12,2) NOT NULL, `units_per_case` INT NOT NULL,
		PRIMARY KEY (`sku`)) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
	_insert_chunks(_CASE, ("sku", "case_cost", "units_per_case"), _case_tuples(rows), upsert=True)
	total = int(frappe.db.sql(f"SELECT COUNT(*) FROM `{_CASE}`")[0][0])
	join = f"dc_offer o LEFT JOIN `{_CASE}` i ON i.sku = o.sku"
	changed = """o.distributor = %s AND NOT (o.case_cost <=> COALESCE(i.case_cost, 0)
		AND o.units_per_case <=> COALESCE(i.units_per_case, 0))"""
	updated = int(frappe.db.sql(f"SELECT COUNT(*) FROM {join} WHERE {changed}", (distributor,))[0][0])
	if updated:
		frappe.db.sql(f"""UPDATE {join} SET o.case_cost = COALESCE(i.case_cost, 0),
			o.units_per_case = COALESCE(i.units_per_case, 0) WHERE {changed}""", (distributor,))
	return {"updated": updated, "total": total}


_RST = "_dc_restriction"
# GROUP_CONCAT's cap for one SKU's entries while the swap runs (the server
# default, 1 MiB, would silently truncate a long list into broken JSON).
_GROUP_CONCAT_MAX = 64 << 20


def _restriction_pairs(entries):
	if isinstance(entries, Mapping):
		# two spellings of one SKU are one row (the last one wins)
		for sku, rows in {norm_sku(k): v for k, v in entries.items()}.items():
			for entry in rows:
				yield sku, entry
		return
	for sku, entry in entries:
		yield norm_sku(sku), entry


def load_restrictions(entries, content_hash: str, *, distributor: str,
		mode: str = "delta", force: bool = False) -> dict:
	"""Replace a house's ship restrictions in one transaction — readers see the
	old set until the commit. ``entries``: ``{sku: [[muni, state, type,
	detail]]}``, or a stream of ``(sku, entry)`` pairs in file order (the RSR
	lane passes its parser; lines of one SKU are gathered in that order). An
	unchanged file (same ``content_hash``) writes nothing and never reads the
	stream. ``mode="snapshot"`` (a whole feed file) refuses a file of fewer than
	half the lines last loaded once that count reached PARTIAL_FILE_FLOOR — the
	catalog's partial-file rule — keeping the current set. A forced run from bench (never Sync now)
	loads such a file anyway, logs a warning and returns it under "warning" (the
	run's dc_sync_run row keeps it); the scheduled run keeps refusing and its error
	names the forced call. → ``{rows_written, lines, skipped_unchanged}`` (plus
	"warning" when set) or ``LOCK_SKIPPED``."""
	if mode not in ("snapshot", "delta"):
		raise ValueError(f"unknown load mode: {mode!r}")
	def body():
		_drop_temp(_RST)  # DDL first: no write may be pending (implicit commit)
		frappe.db.sql(f"""CREATE TEMPORARY TABLE `{_RST}` (`ln` INT NOT NULL,
			`sku` VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
			`entry` TEXT NOT NULL, PRIMARY KEY (`sku`, `ln`))
			DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
		frappe.db.sql("INSERT IGNORE INTO dc_meta (distributor) VALUES (%s)", (distributor,))
		current, last_lines = frappe.db.sql("""SELECT restrictions_hash, restrictions_lines
			FROM dc_meta WHERE distributor = %s""", (distributor,))[0]
		if current == content_hash:
			return {"rows_written": 0, "lines": int(last_lines or 0), "skipped_unchanged": True}
		lines = _insert_chunks(_RST, ("ln", "sku", "entry"),
			((ln, sku, json.dumps(entry, separators=(",", ":")))
				for ln, (sku, entry) in enumerate(_restriction_pairs(entries), 1) if sku))
		last_lines = int(last_lines or 0)
		warning = None
		if mode == "snapshot" and last_lines >= PARTIAL_FILE_FLOOR \
				and lines < last_lines * PARTIAL_FILE_FRACTION:
			message = f"restrictions file looks partial: {lines} lines vs {last_lines} last loaded"
			if not force:
				raise ValueError(f"{message} — refusing the file (if the drop is real, a forced "
					f"sync loads it: sync.run_lane({distributor!r}, 'restrictions', force=True))")
			warning = f"{message} — loaded anyway (forced sync)"
			_log().warning("catalog %s: %s", distributor, warning)
		frappe.db.sql("DELETE FROM dc_restriction WHERE distributor = %s", (distributor,))
		previous = frappe.db.sql("SELECT @@SESSION.group_concat_max_len")[0][0]
		frappe.db.sql(f"SET SESSION group_concat_max_len = {_GROUP_CONCAT_MAX}")
		try:
			frappe.db.sql(f"""INSERT INTO dc_restriction (distributor, sku, entries, entry_count)
				SELECT %s, sku, CONCAT('[', GROUP_CONCAT(entry ORDER BY ln SEPARATOR ','), ']'),
					COUNT(*) FROM `{_RST}` GROUP BY sku""", (distributor,))
		finally:
			frappe.db.sql(f"SET SESSION group_concat_max_len = {int(previous)}")
		written = int(frappe.db.sql(f"SELECT COUNT(DISTINCT sku) FROM `{_RST}`")[0][0])
		frappe.db.sql("""UPDATE dc_meta SET restrictions_hash = %s, restrictions_lines = %s
			WHERE distributor = %s""", (content_hash, lines, distributor))
		out = {"rows_written": written, "lines": lines, "skipped_unchanged": False}
		if warning:
			out["warning"] = warning
		return out
	return _guarded(distributor, (_RST,), body)


# ------------------------------------------------------------------ product layer

_PRODUCT_COLS = ("manufacturer", "model", "description", "image_url", "long_description")


def refresh_products(upcs, priorities: dict | None = None) -> int:
	"""Re-elect the per-UPC product rows for ``upcs`` (the Data Service's refresh_products,
	in Python: a UPC has one to three offers). Content takes each column's first
	non-empty value in merge order over live (not Deleted) offers; ``content_source``
	is the first offer's house. ``ffl_required`` is an OR over EVERY offer, live or
	not — availability never toggles gun-ness. A UPC with no live offer keeps its
	content. Writes only rows that differ; returns how many."""
	upcs = sorted({u for u in upcs or () if u})
	if priorities is None:
		priorities = priority_map()
	written = 0
	now = utc_now()
	for start in range(0, len(upcs), REFRESH_CHUNK):
		chunk = tuple(upcs[start:start + REFRESH_CHUNK])
		offers: dict = {}
		for o in frappe.db.sql("""SELECT upc, distributor, sku, manufacturer, model,
				short_description, image_url, long_description, qty, cost, ffl_required,
				status_flag FROM dc_offer WHERE upc IN %(u)s""", {"u": chunk}, as_dict=True):
			offers.setdefault(o.upc, []).append(o)
		current = {r.upc: r for r in frappe.db.sql(f"""SELECT upc, {", ".join(_PRODUCT_COLS)},
			content_source, ffl_required FROM dc_product WHERE upc IN %(u)s""",
			{"u": chunk}, as_dict=True)}
		rows = []
		for upc, group in offers.items():
			want = _merged(group, priorities, current.get(upc))
			have = current.get(upc)
			if have is None or int(have.ffl_required) != want["ffl_required"] or any(
					(have.get(k) or "") != (want[k] or "") for k in (*_PRODUCT_COLS, "content_source")):
				rows.append((upc, *(want[k] for k in (*_PRODUCT_COLS, "content_source", "ffl_required")),
					now))
		written += _insert_chunks("dc_product", ("upc", *_PRODUCT_COLS, "content_source",
			"ffl_required", "updated_at"), rows, upsert=True)
	return written


def _merged(group: list, priorities: dict, have) -> dict:
	ffl = int(any(o.ffl_required for o in group))
	live = sorted((o for o in group if not is_deleted(o.status_flag)),
		key=lambda o: offer_sort_key(o, priorities))
	if not live:
		keep = {k: (have.get(k) if have else "") or "" for k in (*_PRODUCT_COLS, "content_source")}
		return dict(keep, ffl_required=ffl)

	def first(field):
		return next((o[field] for o in live if o[field]), "") or ""

	return {"manufacturer": first("manufacturer"), "model": first("model"),
		"description": first("short_description"), "image_url": first("image_url"),
		"long_description": first("long_description"), "content_source": live[0].distributor,
		"ffl_required": ffl}
