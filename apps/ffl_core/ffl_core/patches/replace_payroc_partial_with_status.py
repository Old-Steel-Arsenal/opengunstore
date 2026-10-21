import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field


def execute():
	"""payroc_partial (Check) → payroc_status (Select).

	The partial flag shipped less than a day before this patch and the
	integration is inert without credentials, so real data is unlikely —
	but map defensively. Fixtures sync runs AFTER patches, so the status
	field is created here first (the later sync is idempotent).

	A site that reaches this patch after ffl_integrations'
	rename_payroc_storage_to_card (pre_model_sync) already has the status as
	card_status, and no payroc_* column left to map from."""
	if frappe.db.has_column("POS Invoice", "card_status"):
		return
	if not frappe.db.has_column("POS Invoice", "payroc_status"):
		create_custom_field("POS Invoice", {
			"fieldname": "payroc_status",
			"fieldtype": "Select",
			"label": "Payroc Status",
			"options": "\nIN_PROGRESS\nCHARGED\nPARTIAL\nDECLINED\nCANCELED\nVOIDED\nREFUNDED",
			"insert_after": "payroc_attempt",
			"read_only": 1,
			"no_copy": 1,
		})

	if frappe.db.has_column("POS Invoice", "payroc_partial"):
		frappe.db.sql("""UPDATE `tabPOS Invoice` SET payroc_status = 'PARTIAL'
			WHERE IFNULL(payroc_partial, 0) = 1""")
	frappe.db.sql("""UPDATE `tabPOS Invoice` SET payroc_status = 'CHARGED'
		WHERE IFNULL(payroc_unique_reference, '') != ''
		  AND IFNULL(payroc_status, '') = ''""")
	frappe.db.sql("""UPDATE `tabPOS Invoice` SET payroc_status = 'REFUNDED'
		WHERE IFNULL(payroc_refund_reference, '') != ''
		  AND IFNULL(payroc_status, '') = ''""")

	frappe.delete_doc_if_exists("Custom Field", "POS Invoice-payroc_partial")
