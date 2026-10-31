frappe.query_reports["Firearms In Stock"] = {
	filters: [
		{
			fieldname: "status",
			label: __("Stock Status"),
			fieldtype: "Select",
			options: ["Active", "Delivered", "Inactive", "Expired"].join("\n"),
			default: "Active",
		},
		{
			fieldname: "warehouse",
			label: __("Warehouse"),
			fieldtype: "Link",
			options: "Warehouse",
		},
		{
			fieldname: "manufacturer",
			label: __("Manufacturer"),
			fieldtype: "Link",
			options: "Brand",
		},
	],

	// Correct one gun's cost straight from the row a manager is already
	// looking at, rather than navigating to the Item page and picking the serial
	// back out of a list. Opens window.gunstore.correctCostDialog — the SAME
	// dialog and server method as the Item and Serial No forms.
	//
	// A link in the Serial No cell rather than a toolbar button: the action is
	// per-gun, and a toolbar button would need its own "which row?" picker, which
	// is the round trip this entry point exists to remove. Only rendered for a
	// manager on an in-stock row.
	formatter(value, row, column, data, default_formatter) {
		const html = default_formatter(value, row, column, data);
		const g = window.gunstore;
		if (column.fieldname !== "serial_no" || !data || !data.serial_no) return html;
		if (!g || !g.correctCostDialog || !g.canCorrectSerial || !g.canCorrectSerial()) return html;
		// Status is a FILTER here, not a column, so the row carries no status to
		// read — ask the filter. (Reading data.status would silently be undefined
		// and offer the action on a Delivered listing, where the server refuses.)
		const statusFilter = (frappe.query_report
			&& frappe.query_report.get_filter_value
			&& frappe.query_report.get_filter_value("status")) || "Active";
		if (statusFilter !== "Active") return html;
		const serial = frappe.utils.escape_html(data.serial_no);
		const item = frappe.utils.escape_html(data.item_code || "");
		return `${html} <a class="ffl-correct-cost" data-serial="${serial}" data-item="${item}"
			title="${__("Correct this gun's cost")}"
			style="margin-left:6px; cursor:pointer; font-size:11px">$</a>`;
	},

	onload(report) {
		// Delegated once on the report wrapper: the datatable re-renders rows on
		// every scroll/refresh, so per-row handlers would be lost (or stack up).
		// onload runs again each time this report is reopened on the shared
		// query-report page, so unbind first or the cost dialog opens twice.
		$(report.page.main).off("click.fflCost").on("click.fflCost", "a.ffl-correct-cost", function (e) {
			e.preventDefault();
			e.stopPropagation();
			const g = window.gunstore;
			if (!g || !g.correctCostDialog) return;
			g.correctCostDialog({
				item_code: $(this).data("item"),
				serial: $(this).data("serial"),
				onDone: () => report.refresh(),
			});
		});

		report.page.add_inner_button(__("Sync Bound Book"), () => {
			const method = "ffl_integrations.fastbound.inventory_sync.sync_in_stock_from_boundbook";
			frappe.call({
				method,
				args: { dry_run: 1 },
				freeze: true,
				freeze_message: __("Checking FastBound bound book…"),
				callback(r) {
					const list = (r.message || {}).would_remove || [];
					// Guns out on consignment are disposed in FastBound and in stock
					// here BY DESIGN, so the server withholds them from would_remove
					// (incident CONO-2026-01544). Say so — a silent omission reads as
					// "they vanished" and the next person reconciles them by hand.
					const held = (r.message || {}).consignment_held || [];
					// "more" only reads right next to a list of other firearms; with
					// nothing to remove there is no "more" to be had.
					const heldNote = (more) => (held.length
						? `<br><br><span class="text-muted">${(more
							? __("{0} more firearm(s) are out on consignment at a dealer — disposed in FastBound but still our stock, so they are never removed here.", [held.length])
							: __("{0} firearm(s) are out on consignment at a dealer — disposed in FastBound but still our stock, so they are never removed here.", [held.length]))}</span>`
						: "");
					if (!list.length) {
						frappe.msgprint({
							title: __("Bound Book Sync"),
							indicator: "green",
							message: (held.length
								? __("In sync — no in-stock firearm needs removing.")
								: __("In sync — no in-stock firearm is marked disposed in FastBound.")
							) + heldNote(false),
						});
						return;
					}
					const rows = list
						.map((x) => `${frappe.utils.escape_html(x.serial)} <span class="text-muted">(${frappe.utils.escape_html(x.item_code)})</span>`)
						.join("<br>");
					frappe.confirm(
						__("FastBound reports these {0} in-stock firearm(s) as disposed. Remove them from ERPNext stock (Material Issue)?", [list.length]) +
							`<br><br>${rows}` + heldNote(true),
						() => {
							frappe.call({
								method,
								// Pass back exactly the FastBound item ids the operator just
								// approved, so a gun that became disposed between preview and
								// confirm is never removed without being shown.
								args: { dry_run: 0, item_ids: list.map((x) => x.fb_id) },
								freeze: true,
								freeze_message: __("Removing disposed firearms…"),
								callback(r2) {
									const m = r2.message || {};
									const errs = m.errors || [];
									frappe.msgprint({
										title: __("Bound Book Sync"),
										indicator: errs.length ? "orange" : "green",
										message: __("Removed {0} firearm(s) from stock. Errors: {1}.", [m.removed_count || 0, errs.length]) +
											(errs.length
												? "<br><br>" + errs
														.map((e) => `${frappe.utils.escape_html(e.serial)}: ${frappe.utils.escape_html(e.error)}`)
														.join("<br>")
												: ""),
									});
									report.refresh();
								},
							});
						},
					);
				},
			});
		});

		report.page.add_inner_button(__("Check FastBound differences"), () => {
			frappe.call({
				method: "ffl_integrations.fastbound.reconcile.scan_in_stock_fb_diffs",
				freeze: true,
				freeze_message: __("Starting FastBound scan…"),
				callback() {
					frappe.show_alert({ message: __("Scan started — you'll get a summary when it finishes."), indicator: "blue" });
				},
			});
		});

		// Same reason: a stable handler reference so off() removes the previous one.
		frappe.realtime.off("fb_diff_scan_done", fis_on_fb_diff_scan_done);
		frappe.realtime.on("fb_diff_scan_done", fis_on_fb_diff_scan_done);
	},
};

function fis_on_fb_diff_scan_done(data) {
	const list = (data || {}).divergent || [];
	if (!list.length) {
		frappe.msgprint({ title: __("FastBound Scan"), indicator: "green",
			message: __("All in-stock firearms match FastBound.") });
		return;
	}
	const rows = list.map((x) =>
		`${frappe.utils.escape_html(x.serial)} <span class="text-muted">(${frappe.utils.escape_html((x.fields || []).join(", "))})</span>`).join("<br>");
	frappe.msgprint({ title: __("FastBound Scan: {0} gun(s) differ", [list.length]),
		indicator: "orange",
		message: __("Open each gun's Serial No and use 'Sync with FastBound':") + `<br><br>${rows}` });
}
