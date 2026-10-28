"""The local catalog's tables — plain SQL, created by the
``create_local_catalog_tables`` patch and re-ensured on every migrate.

Not DocTypes: a feed writes tens of thousands of rows in bulk, and a DocType
row carries name/owner/modified plus a per-row ORM path (the old in-POS RSR
loader showed that is the slow path). Columns follow the Data Service's OfferFull and
product shapes.

Key columns (distributor, sku, upc, product_key) are ``utf8mb4_bin``: SKUs and
product keys are exact identifiers (the Data Service compares them byte-wise on
Postgres), and a case-insensitive primary key would merge two SKUs that differ
only in case. Everything else is utf8mb4 with the site's usual collation.
``search_text`` is stored lower-cased, so search stays case-insensitive.
"""
from __future__ import annotations

import os
import re

import frappe

_BIN = "CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"
_TABLE_OPTS = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"

# The offer columns a feed writes, in OfferFull order, with their DDL. The
# store's staging table and change compare are built from this one list.
OFFER_COLUMNS = (
	("upc", f"VARCHAR(20) {_BIN} NULL"),
	("manufacturer", "VARCHAR(255) NOT NULL DEFAULT ''"),
	("model", "VARCHAR(255) NOT NULL DEFAULT ''"),
	("department", "VARCHAR(64) NOT NULL DEFAULT ''"),
	("department_name", "VARCHAR(140) NOT NULL DEFAULT ''"),
	("cost", "DECIMAL(12,2) NOT NULL DEFAULT 0"),
	("map", "DECIMAL(12,2) NOT NULL DEFAULT 0"),
	("msrp", "DECIMAL(12,2) NOT NULL DEFAULT 0"),
	("map_policy", "VARCHAR(8) NOT NULL DEFAULT ''"),
	("qty", "INT NOT NULL DEFAULT 0"),
	("ffl_required", "TINYINT(1) NOT NULL DEFAULT 1"),
	("dropship_blocked", "TINYINT(1) NOT NULL DEFAULT 0"),
	("vendor_approval_required", "TINYINT(1) NOT NULL DEFAULT 0"),
	("image_url", "VARCHAR(1000) NOT NULL DEFAULT ''"),
	("discontinued", "TINYINT(1) NOT NULL DEFAULT 0"),
	("status_flag", "VARCHAR(32) NOT NULL DEFAULT ''"),
	("short_description", "VARCHAR(1000) NOT NULL DEFAULT ''"),
	("mfg_number", f"VARCHAR(140) {_BIN} NOT NULL DEFAULT ''"),
	("caliber", "VARCHAR(140) NOT NULL DEFAULT ''"),
	("long_description", "MEDIUMTEXT NULL"),
	("image_name", "VARCHAR(255) NOT NULL DEFAULT ''"),
	("image_url_hires", "VARCHAR(1000) NOT NULL DEFAULT ''"),
	("weight_lb", "DECIMAL(10,3) NOT NULL DEFAULT 0"),
	("prop65", "TINYINT(1) NOT NULL DEFAULT 0"),
	("ships_ground_only", "TINYINT(1) NOT NULL DEFAULT 0"),
	("signature_required", "TINYINT(1) NOT NULL DEFAULT 0"),
	("product_class", "VARCHAR(32) NOT NULL DEFAULT ''"),
)
# Written by the loader from the columns above (never by a feed directly).
DERIVED_COLUMNS = (
	("product_key", f"VARCHAR(100) {_BIN} NOT NULL"),
	("brand_key", "VARCHAR(255) NOT NULL DEFAULT ''"),
	("search_text", "TEXT NOT NULL"),
)

BOOL_COLUMNS = frozenset(name for name, ddl in OFFER_COLUMNS if ddl.startswith("TINYINT"))
MONEY_COLUMNS = frozenset(("cost", "map", "msrp", "weight_lb"))


def _cols(defs) -> str:
	return ",\n\t".join(f"`{name}` {ddl}" for name, ddl in defs)


# dc_offer's case_cost (the full-case price PER UNIT) and units_per_case sit outside
# OFFER_COLUMNS: no feed file carries them (RSR's get-items does, through
# store.apply_case_prices), so a catalog load never writes or compares them.
TABLES = {
	"dc_offer": f"""CREATE TABLE IF NOT EXISTS `dc_offer` (
	`distributor` VARCHAR(32) {_BIN} NOT NULL,
	`sku` VARCHAR(64) {_BIN} NOT NULL,
	{_cols(OFFER_COLUMNS)},
	{_cols(DERIVED_COLUMNS)},
	`change_seq` BIGINT NOT NULL DEFAULT 0,
	`case_cost` DECIMAL(12,2) NOT NULL DEFAULT 0,
	`units_per_case` INT NOT NULL DEFAULT 0,
	`qty_updated_at` DATETIME(6) NULL,
	`created_at` DATETIME(6) NOT NULL,
	`updated_at` DATETIME(6) NOT NULL,
	PRIMARY KEY (`distributor`, `sku`),
	KEY `dc_offer_change` (`distributor`, `change_seq`),
	KEY `dc_offer_upc` (`upc`),
	KEY `dc_offer_product` (`product_key`),
	KEY `dc_offer_dept` (`distributor`, `department`),
	KEY `dc_offer_mfg` (`mfg_number`),
	KEY `dc_offer_brand` (`brand_key`)
) {_TABLE_OPTS}""",
	# The per-UPC product layer: merged content (first non-empty value in the
	# merge order) and the cross-house FFL OR. Offers without a UPC have none.
	"dc_product": f"""CREATE TABLE IF NOT EXISTS `dc_product` (
	`upc` VARCHAR(20) {_BIN} NOT NULL,
	`manufacturer` VARCHAR(255) NOT NULL DEFAULT '',
	`model` VARCHAR(255) NOT NULL DEFAULT '',
	`description` VARCHAR(1000) NOT NULL DEFAULT '',
	`image_url` VARCHAR(1000) NOT NULL DEFAULT '',
	`long_description` MEDIUMTEXT NULL,
	`content_source` VARCHAR(32) NOT NULL DEFAULT '',
	`ffl_required` TINYINT(1) NOT NULL DEFAULT 1,
	`updated_at` DATETIME(6) NOT NULL,
	PRIMARY KEY (`upc`)
) {_TABLE_OPTS}""",
	# A product an offer moved OFF (its UPC changed): the change feed must name
	# the old product too, and the offer row only carries the new key.
	"dc_product_move": f"""CREATE TABLE IF NOT EXISTS `dc_product_move` (
	`id` BIGINT NOT NULL AUTO_INCREMENT,
	`distributor` VARCHAR(32) {_BIN} NOT NULL,
	`product_key` VARCHAR(100) {_BIN} NOT NULL,
	`change_seq` BIGINT NOT NULL,
	`created_at` DATETIME(6) NOT NULL,
	PRIMARY KEY (`id`),
	KEY `dc_move_change` (`distributor`, `change_seq`)
) {_TABLE_OPTS}""",
	"dc_restriction": f"""CREATE TABLE IF NOT EXISTS `dc_restriction` (
	`distributor` VARCHAR(32) {_BIN} NOT NULL,
	`sku` VARCHAR(64) {_BIN} NOT NULL,
	`entries` LONGTEXT NOT NULL,
	`entry_count` INT NOT NULL DEFAULT 0,
	PRIMARY KEY (`distributor`, `sku`)
) {_TABLE_OPTS}""",
	# One row per house: its change sequence (see store.py), lock-skip and
	# UPC-reject counters, the restrictions file hash and line count (the
	# partial-file guard's reference), and each lane's latest
	# attempt ({lane: ISO UTC}, the tick's fairness order), and the Sports South
	# quantity cursor (its server time, echoed back verbatim), and when the house's
	# login was last saved (Lipsey's: an auth hold lifts once it is newer than the
	# rejected attempt).
	"dc_meta": f"""CREATE TABLE IF NOT EXISTS `dc_meta` (
	`distributor` VARCHAR(32) {_BIN} NOT NULL,
	`change_seq` BIGINT NOT NULL DEFAULT 0,
	`lock_skip_count` INT NOT NULL DEFAULT 0,
	`lock_skipped_at` DATETIME(6) NULL,
	`upc_reject_count` INT NOT NULL DEFAULT 0,
	`restrictions_hash` VARCHAR(64) NOT NULL DEFAULT '',
	`restrictions_lines` INT NOT NULL DEFAULT 0,
	`lane_attempts` LONGTEXT NULL,
	`qty_cursor` VARCHAR(64) {_BIN} NOT NULL DEFAULT '',
	`creds_saved_at` DATETIME(6) NULL,
	PRIMARY KEY (`distributor`)
) {_TABLE_OPTS}""",
	# One row per lane run that reached its body. Source = `<house>_<lane>`
	# (frozen: health, gates and ops read these strings). Times are UTC.
	"dc_sync_run": f"""CREATE TABLE IF NOT EXISTS `dc_sync_run` (
	`id` BIGINT NOT NULL AUTO_INCREMENT,
	`source` VARCHAR(64) {_BIN} NOT NULL,
	`status` VARCHAR(16) NOT NULL,
	`started_at` DATETIME(6) NOT NULL,
	`finished_at` DATETIME(6) NOT NULL,
	`rows_total` INT NOT NULL DEFAULT 0,
	`error_class` VARCHAR(140) NOT NULL DEFAULT '',
	`error_msg` TEXT NULL,
	PRIMARY KEY (`id`),
	KEY `dc_run_source` (`source`, `status`, `finished_at`),
	KEY `dc_run_finished` (`finished_at`)
) {_TABLE_OPTS}""",
}

def declared_columns(table: str) -> list[tuple[str, str]]:
	"""``[(column, definition)]`` as TABLES declares them, in order."""
	out = []
	for line in TABLES[table].splitlines():
		m = re.match(r"\s*`(\w+)` (.+?),?$", line)
		if m:
			out.append((m.group(1), m.group(2)))
	return out


def ensure() -> None:
	"""after_migrate + the create patch: every table exists with every declared
	column (a table from an older version, or one edited by hand, gets the
	missing columns added with their declared types), and the catalog queue is
	declared. Idempotent; DDL commits first (sql_ddl)."""
	for table, ddl in TABLES.items():
		frappe.db.sql_ddl(ddl)
		live = {r[0].lower() for r in frappe.db.sql("""SELECT column_name FROM information_schema.columns
			WHERE table_schema = DATABASE() AND table_name = %s""", (table,))}
		for column, definition in declared_columns(table):
			if column.lower() not in live:
				frappe.db.sql_ddl(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")
	ensure_queue_config()


def ensure_queue_config() -> None:
	"""Declare the ``catalog`` queue in common_site_config ``workers`` (merged:
	other queues and an operator's own timeout stay). The queue-catalog
	container does the same before it starts (``declare_queue``)."""
	from frappe.installer import update_site_config

	from ffl_integrations.catalog_local.declare_queue import merged

	path = os.path.join(frappe.local.sites_path, "common_site_config.json")
	if not os.path.exists(path):
		return
	config, changed = merged(frappe.get_file_json(path))
	if changed:
		update_site_config("workers", config["workers"], site_config_path=path)
