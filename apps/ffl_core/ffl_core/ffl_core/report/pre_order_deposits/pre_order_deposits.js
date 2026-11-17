// Pre-order Deposits: the sub-ledger of money held for pre-orders, one row per order, as of a date.
frappe.query_reports["Pre-order Deposits"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "as_of_date",
			label: __("As of"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
	],
};
