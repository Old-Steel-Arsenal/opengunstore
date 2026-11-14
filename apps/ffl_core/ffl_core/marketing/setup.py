"""Install / migrate plumbing for ffl_core.marketing."""
from __future__ import annotations

import frappe

from ffl_core.marketing import CONTACT


def ensure() -> None:
	"""after_install / after_migrate: Marketing Contact's tags column as ``text``.

	Frappe adds ``_user_tags`` to a table on its first tag, as a 140-character column; a contact with a
	dozen tags outgrows it, and adding the column mid-request is DDL (an implicit commit). Made here
	instead, once, wide, and widened if Frappe already added the narrow one."""
	if not frappe.db.table_exists(CONTACT):
		return
	table = f"tab{CONTACT}"
	column = frappe.db.sql("""select data_type from information_schema.columns
		where table_schema = database() and table_name = %s and column_name = '_user_tags'""", table)
	if column and column[0][0].lower() == "text":
		return
	frappe.db.sql_ddl(f"alter table `{table}` {'modify' if column else 'add'} column `_user_tags` text")
