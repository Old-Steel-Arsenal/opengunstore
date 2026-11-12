// Sales Summary: the period's sales, tax and payments (Summary), or the same sales grouped by
// category, register or employee. Export and print are the report's own.
frappe.query_reports["Sales Summary"] = {
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
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "view",
			label: __("View"),
			fieldtype: "Select",
			options: ["Summary", "Category", "Register", "Employee"],
			default: "Summary",
			reqd: 1,
		},
	],

	formatter(value, row, column, data, default_formatter) {
		if (data && data.is_section && column.fieldname !== "line") return "";
		if (data && data.is_note && column.fieldname !== "line") return "";
		value = default_formatter(value, row, column, data);
		if (!data) return value;
		if (data.is_note) return `<span class="text-muted">${value}</span>`;
		if (column.fieldname === "line" && data.sub) value = `<span style="padding-left:18px">${value}</span>`;
		if (data.is_alert) return `<span style="color:var(--red-600);font-weight:600">${value}</span>`;
		if (data.bold || data.is_section) return `<b>${value}</b>`;
		return value;
	},
};
