"""Pending Transfer Pickups — inbound customer-transfer guns awaiting pickup.

Every firearm received FOR a customer (FFL Acquisition.transfer_for_customer,
set on the Receive Goods page) whose serial is still in the safe (Serial No
status Active), aged by days in store. This is the "call the customer / chase
unclaimed guns" queue: sort by age, ring whoever's gun has sat the longest.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import date_diff, getdate, nowdate


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return _columns(), _rows(filters)


def _columns():
	return [
		{"label": _("Acquisition"), "fieldname": "acquisition", "fieldtype": "Link",
			"options": "FFL Acquisition", "width": 150},
		{"label": _("Received"), "fieldname": "received", "fieldtype": "Date", "width": 100},
		{"label": _("Days In Store"), "fieldname": "days_in_store", "fieldtype": "Int", "width": 100},
		{"label": _("Customer"), "fieldname": "customer", "fieldtype": "Link",
			"options": "Customer", "width": 170},
		{"label": _("Phone"), "fieldname": "phone", "fieldtype": "Data", "width": 120},
		{"label": _("Item"), "fieldname": "item_code", "fieldtype": "Link",
			"options": "Item", "width": 180},
		{"label": _("Serial"), "fieldname": "serial", "fieldtype": "Link",
			"options": "Serial No", "width": 150},
		{"label": _("Shipped By"), "fieldname": "source_name", "fieldtype": "Data", "width": 170},
	]


def _rows(filters):
	conditions = ["acq.docstatus = 1", "acq.transfer_for_customer IS NOT NULL",
		"acq.transfer_for_customer != ''", "sn.status = 'Active'",
		# Only the serial's current acquisition: once a transfer gun is picked up
		# and the store later receives the same serial again, the old transfer
		# acquisition must not put the earlier customer back on the call list.
		# Same "latest" order as acquisition._existing.
		"""NOT EXISTS (SELECT 1 FROM `tabFFL Acquisition` a2
			WHERE a2.serial = acq.serial AND a2.docstatus = 1 AND a2.name != acq.name
			  AND (a2.acquisition_date > acq.acquisition_date
			    OR (a2.acquisition_date = acq.acquisition_date
			        AND a2.creation > acq.creation)))"""]
	params = {}
	if filters.get("customer"):
		conditions.append("acq.transfer_for_customer = %(customer)s")
		params["customer"] = filters.customer

	rows = frappe.db.sql(
		f"""
		SELECT acq.name AS acquisition, acq.acquisition_date,
		       acq.transfer_for_customer AS customer,
		       acq.item AS item_code, acq.serial, acq.source_name,
		       COALESCE(NULLIF(c.mobile_no, ''), '') AS phone
		FROM `tabFFL Acquisition` acq
		INNER JOIN `tabSerial No` sn ON sn.name = acq.serial
		LEFT JOIN `tabCustomer` c ON c.name = acq.transfer_for_customer
		WHERE {" AND ".join(conditions)}
		ORDER BY acq.acquisition_date ASC
		""",
		params,
		as_dict=True,
	)
	today = getdate(nowdate())
	for r in rows:
		r["received"] = getdate(r.pop("acquisition_date"))
		r["days_in_store"] = date_diff(today, r["received"])
	return rows
