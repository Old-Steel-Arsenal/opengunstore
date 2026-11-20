// Form 4473 Records — every completed 4473 on file (the Form 4473 Log); "View PDF"
// opens the signed form through ffl.open4473Pdf (ffl_core.bundle.js). The Check column is an
// extension's (hook gunshop_4473_record_checks): where the record's check stands, linked to
// the extension's own page for it.
frappe.query_reports["Form 4473 Records"] = {
	filters: [
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date" },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date" },
		{ fieldname: "customer", label: __("Customer"), fieldtype: "Link", options: "Customer" },
		{ fieldname: "serial", label: __("Serial"), fieldtype: "Data" },
		{
			fieldname: "nics_status",
			label: __("NICS Status"),
			fieldtype: "Select",
			options: ["", "Proceed", "Delayed", "Denied", "Cancelled"].join("\n"),
		},
		{
			fieldname: "source",
			label: __("Source"),
			fieldtype: "Select",
			options: ["", "FastBound", "Paper / override"].join("\n"),
		},
	],

	formatter(value, row, column, data, default_formatter) {
		if (column.fieldname === "pdf" && data && data.has_pdf) {
			const arg = frappe.utils.escape_html(JSON.stringify(data.log));
			return `<a href="#" onclick="ffl.open4473Pdf(${arg}); return false;">${__("View PDF")}</a>`;
		}
		if (column.fieldname === "check" && data && data.check) {
			const pill = `<span class="indicator-pill ${frappe.utils.escape_html(data.check_indicator || "gray")}">${frappe.utils.escape_html(data.check)}</span>`;
			return data.check_route ? `<a href="${frappe.utils.escape_html(data.check_route)}">${pill}</a>` : pill;
		}
		if (column.fieldname === "pos_invoice" && data && data.pos_invoice) {
			return data.pos_invoice.split(", ").map((name) =>
				`<a href="/desk/pos-invoice/${encodeURIComponent(name)}">${frappe.utils.escape_html(name)}</a>`
			).join(", ");
		}
		return default_formatter(value, row, column, data);
	},
};
