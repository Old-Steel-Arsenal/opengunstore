// Pending 4473 Orders — filter bar, row colouring by NICS state, and the
// manager cleanup for an order nobody ever came back for (DISP-2026-02000).
frappe.query_reports["Pending 4473 Orders"] = {
	filters: [
		{
			fieldname: "nics_status",
			label: __("NICS Status"),
			fieldtype: "Select",
			options: ["", "N/A", "Proceed", "Delayed", "Denied", "Cancelled"].join("\n"),
		},
		{
			fieldname: "check_fastbound",
			label: __("Check FastBound"),
			fieldtype: "Check",
			default: 0,
			description: __("Ask FastBound whether an abandoned 4473 left a pending disposition holding each gun. One round trip per firearm — off by default."),
		},
	],

	onload(report) {
		// Managers only — the same two roles the server gate enforces
		// (fastbound.pos_4473._check_manager_perm). The server is the gate; this
		// just stops everyone else being offered a button that can only end in a
		// PermissionError after they have typed a reason.
		if (!frappe.user.has_role(["Sales Manager", "System Manager"])) return;
		report.page.add_inner_button(__("Clear Abandoned Order…"), () =>
			_p4473_open_cleanup(report));
	},
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "nics_status" && data) {
			if (data.nics_status === "Denied") {
				value = `<span style="color:var(--red-600);font-weight:600">${value}</span>`;
			} else if (data.nics_status === "Delayed") {
				value = `<span style="color:var(--orange-600);font-weight:600">${value}</span>`;
			} else if (data.nics_status === "Proceed") {
				value = `<span style="color:var(--green-600);font-weight:600">${value}</span>`;
			}
		}
		// Past the Brady date and still no Proceed — flag the row's date.
		if (column.fieldname === "brady_date" && data && data.brady_date) {
			const past = frappe.datetime.get_diff(frappe.datetime.get_today(), data.brady_date) > 0;
			if (past && data.nics_status === "Delayed") {
				value = `<span style="color:var(--orange-600);font-weight:600">${value}</span>`;
			}
		}
		return value;
	},
};


// Manager cleanup: delete FastBound's leftover pending disposition and the
// draft that has been holding the gun. Deliberately a BUTTON — the scheduled
// watch only ever raises a ToDo, because deleting a bound-book record
// unattended is not something a cron may decide.
function _p4473_open_cleanup(report) {
	const d = new frappe.ui.Dialog({
		title: __("Clear an abandoned 4473 order"),
		fields: [
			{
				fieldtype: "Link",
				fieldname: "invoice_name",
				label: __("POS Invoice"),
				options: "POS Invoice",
				reqd: 1,
				get_query: () => ({ filters: { docstatus: 0 } }),
				description: __("Draft orders only. The invoice is deleted, not cancelled — the POS recycle bin keeps it restorable."),
			},
			{
				fieldtype: "Small Text",
				fieldname: "reason",
				label: __("Reason"),
				reqd: 1,
				description: __("Recorded in the Error Log audit trail — not on the order, which is deleted."),
			},
		],
		primary_action_label: __("Delete FastBound pending disposition & close order"),
		primary_action(values) {
			frappe.confirm(
				__("This removes FastBound's pending disposition for every gun on {0} and deletes the draft. Continue?", [values.invoice_name]),
				() => {
					frappe.call({
						method: "ffl_integrations.fastbound.pos_4473_recovery.manager_abandon_4473",
						args: { invoice_name: values.invoice_name, reason: values.reason },
						freeze: true,
						freeze_message: __("Clearing the abandoned order…"),
						callback(r) {
							const m = r.message || {};
							if (!m.ok) return;
							d.hide();
							frappe.show_alert({
								message: __("Cleared {0} — {1} FastBound disposition(s) deleted", [
									m.invoice, (m.deleted || []).length]),
								indicator: "green",
							}, 7);
							report.refresh();
						},
					});
				},
			);
		},
	});
	d.show();
}
