"""Install / migrate plumbing for ffl_core.marketing."""
from __future__ import annotations

import frappe

from ffl_core.marketing import CONTACT


def ensure() -> None:
	"""after_install / after_migrate: the marketing staff role, and Marketing Contact's tags column as ``text``.

	The role (``roles.MARKETING_ROLE``) has Desk access; its permissions are the marketing DocTypes' own
	DocPerms, so every store gets the same role with the same reach.

	Frappe adds ``_user_tags`` to a table on its first tag, as a 140-character column; a contact with a
	dozen tags outgrows it, and adding the column mid-request is DDL (an implicit commit). Made here
	instead, once, wide, and widened if Frappe already added the narrow one."""
	ensure_role()
	if not frappe.db.table_exists(CONTACT):
		return
	table = f"tab{CONTACT}"
	column = frappe.db.sql("""select data_type from information_schema.columns
		where table_schema = database() and table_name = %s and column_name = '_user_tags'""", table)
	if column and column[0][0].lower() == "text":
		return
	frappe.db.sql_ddl(f"alter table `{table}` {'modify' if column else 'add'} column `_user_tags` text")


def ensure_role() -> None:
	"""The marketing staff role exists, with Desk access (a role sync from the DocTypes makes it too)."""
	from ffl_core.roles import MARKETING_ROLE

	if not frappe.db.exists("Role", MARKETING_ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": MARKETING_ROLE, "desk_access": 1}).insert(
			ignore_permissions=True)
