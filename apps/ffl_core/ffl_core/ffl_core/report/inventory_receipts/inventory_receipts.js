// Inventory Receipts — client side. The server (inventory_receipts.py) owns every
// number and every classification; this file owns the period preset, the tree
// behaviour of the Receipts view (voucher → lines → serial units, with unit
// lists starting collapsed), the Type pills / flag symbols, and the legend.

(function () {
	if (typeof frappe === "undefined") return;

	// Receipt class → indicator-pill colour. Muted for the two internal /
	// not-our-inventory classes so they never read as purchases.
	const CLASS_PILL = {
		Purchase: "blue",
		"Trade-in": "orange",
		Consignment: "purple",
		Intake: "cyan",
		Return: "yellow",
		Adjustment: "gray",
		Revaluation: "gray",
		Transfer: "gray",
		Custody: "gray",
		Other: "gray",
	};

	// Flag label (as the server writes it into the Flags column) → symbol,
	// colour, explanation. The CSV keeps the words; the screen shows the symbol.
	const FLAGS = {
		"No cost": ["⚠", "--orange-600", __("No cost recorded on this unit — inventory value and its future COGS are understated.")],
		"No A&D": ["✖", "--red-600", __("A firearm entered stock with no submitted FFL Acquisition — check the bound book.")],
	};

	const CLASS_HELP = [
		["Purchase", __("Purchase Receipt from a supplier or a private seller (Receive Goods).")],
		["Trade-in", __("Gun taken in against a sale — Intake with source Trade-in, or a Receive Goods source containing “trade”.")],
		["Consignment", __("Consignor's gun received for sale on their behalf — $0 cost, no payable.")],
		["Intake", __("Stock Entry · Material Receipt: the used-gun Intake page, a FastBound backfill, or a manual Add Stock.")],
		["Return", __("Customer return — a Sales / POS Invoice (or Delivery Note) that put units back.")],
		["Adjustment", __("Stock Reconciliation count gain — units found that were not on the books.")],
		["Revaluation", __("Stock Reconciliation that only changed a unit's cost (e.g. a firearm's cost correction). 0 units; Cost is the difference, so Cost Received still moves with Stock In Hand.")],
		["Transfer", __("Internal move INTO a warehouse (e.g. back from a consignment dealer) — not new inventory. Hidden unless “Include transfers”.")],
		["Custody", __("Into a customer-custody warehouse: an FFL transfer for a customer — their property, cost 0. Hidden unless “Include custody”.")],
	];

	// ---- period preset ----------------------------------------------------

	function ir_preset_range(preset, today) {
		const d = frappe.datetime;
		switch (preset) {
			case "Today":
				return { from: today, to: today };
			case "This Week":
				return { from: d.week_start(), to: today };
			case "This Month":
				return { from: d.month_start(), to: today };
			case "Last Month": {
				const first = d.add_months(d.month_start(), -1);
				return { from: first, to: d.add_days(d.month_start(), -1) };
			}
			case "This Quarter":
				return { from: d.quarter_start(), to: today };
			case "Year to Date":
				return { from: d.year_start(), to: today };
			default:
				return null; // Custom → leave from/to alone
		}
	}

	function ir_apply_preset() {
		const range = ir_preset_range(
			frappe.query_report.get_filter_value("period_preset"),
			frappe.datetime.get_today()
		);
		if (!range) return;
		frappe.query_report.set_filter_value({ from_date: range.from, to_date: range.to });
	}

	// A date edit that leaves the range equal to the preset's own range is the
	// preset filling itself in (or a no-op); anything else is a manual edit → Custom.
	// Comparing values instead of guarding with a flag survives Frappe firing
	// on_change asynchronously after a programmatic set_filter_value.
	function ir_revert_to_custom() {
		const qr = frappe.query_report;
		const preset = qr.get_filter_value("period_preset");
		if (preset === "Custom") return;
		const range = ir_preset_range(preset, frappe.datetime.get_today());
		if (range && range.from === qr.get_filter_value("from_date") && range.to === qr.get_filter_value("to_date")) return;
		qr.set_filter_value("period_preset", "Custom");
	}

	function ir_view() {
		return (frappe.query_report && frappe.query_report.get_filter_value("view")) || "Receipts";
	}

	// ---- cell renderers ---------------------------------------------------
	function ir_pill(cls) {
		if (!cls) return "";
		const colour = CLASS_PILL[cls] || "gray";
		return `<span class="indicator-pill ${colour} ellipsis" style="font-weight:500">${frappe.utils.escape_html(__(cls))}</span>`;
	}

	function ir_flags(value) {
		if (!value) return "";
		return String(value)
			.split(";")
			.map((s) => s.trim())
			.filter(Boolean)
			.map((label) => {
				const f = FLAGS[label];
				if (!f) return frappe.utils.escape_html(label);
				return `<span title="${frappe.utils.escape_html(f[2])}" style="color:var(${f[1]});font-size:1.05em;margin-right:4px;cursor:help">${f[0]}</span>`;
			})
			.join("");
	}

	function ir_bold(html) {
		return `<span style="font-weight:600">${html}</span>`;
	}

	function ir_open_legend() {
		const classes = CLASS_HELP.map(
			([cls, desc]) =>
				`<tr><td style="white-space:nowrap">${ir_pill(cls)}</td><td class="text-muted">${frappe.utils.escape_html(desc)}</td></tr>`
		).join("");
		const flags = Object.entries(FLAGS)
			.map(
				([label, [icon, colour, desc]]) =>
					`<tr><td style="text-align:center;font-size:1.15em;color:var(${colour})">${icon}</td>
					<td style="white-space:nowrap"><b>${frappe.utils.escape_html(__(label))}</b></td>
					<td class="text-muted">${frappe.utils.escape_html(desc)}</td></tr>`
			)
			.join("");
		const d = new frappe.ui.Dialog({
			title: __("Receipt types & flags"),
			size: "large",
			fields: [
				{
					fieldtype: "HTML",
					options: `
						<p class="text-muted small" style="margin-bottom:6px">${__(
							"Before handing this to the accountant, the two flag cards should read Clean."
						)}</p>
						<h6 style="margin-top:10px">${__("Receipt type")}</h6>
						<table class="table table-sm">${classes}</table>
						<h6 style="margin-top:10px">${__("Flags")}</h6>
						<table class="table table-sm">${flags}</table>`,
				},
			],
			primary_action_label: __("Close"),
			primary_action: () => d.hide(),
		});
		d.show();
	}

	frappe.query_reports["Inventory Receipts"] = {
		// Declaration order == render order: Period → View → Segment → Include.
		filters: [
			{
				fieldname: "period_preset",
				label: __("Period"),
				fieldtype: "Select",
				options: ["Custom", "Today", "This Week", "This Month", "Last Month", "This Quarter", "Year to Date"].join("\n"),
				default: "This Month",
				on_change() {
					ir_apply_preset();
				},
			},
			{
				fieldname: "from_date",
				label: __("From"),
				fieldtype: "Date",
				default: frappe.datetime.month_start(),
				reqd: 1,
				on_change() {
					ir_revert_to_custom();
				},
			},
			{
				fieldname: "to_date",
				label: __("To"),
				fieldtype: "Date",
				default: frappe.datetime.get_today(),
				reqd: 1,
				on_change() {
					ir_revert_to_custom();
				},
			},
			{
				fieldname: "view",
				label: __("View"),
				fieldtype: "Select",
				// Stored VALUES are the Python dispatch keys; labels are the friendly part.
				options: [
					{ value: "Receipts", label: __("Receipts (tree)") },
					{ value: "Summary", label: __("Summary by category") },
					{ value: "Units", label: __("Units (flat · CSV)") },
				],
				default: "Receipts",
			},
			{
				fieldname: "category",
				label: __("Category"),
				fieldtype: "Select",
				options: ["", "Firearm", "Ammunition", "Accessory", "Other"].join("\n"),
				default: "",
			},
			{
				fieldname: "receipt_class",
				label: __("Type"),
				fieldtype: "Select",
				// Picking Transfer / Custody here shows them even without the Include ticks.
				options: ["", "Purchase", "Trade-in", "Consignment", "Intake", "Return", "Adjustment", "Revaluation", "Transfer", "Custody", "Other"].join("\n"),
				default: "",
			},
			{
				fieldname: "supplier",
				label: __("Supplier"),
				fieldtype: "Link",
				options: "Supplier",
			},
			{
				fieldname: "include_transfers",
				label: __("Include transfers"),
				fieldtype: "Check",
				default: 0,
			},
			{
				fieldname: "include_custody",
				label: __("Include custody"),
				fieldtype: "Check",
				default: 0,
			},
		],

		// Tree plumbing for Frappe (rows carry indent / row_key / parent_row). The
		// Units view has no indent → Frappe renders it flat and sortable.
		tree: true,
		name_field: "row_key",
		parent_field: "parent_row",

		onload(report) {
			report.page.add_inner_button(__("Legend…"), ir_open_legend);
		},

		get_datatable_options(options) {
			// Sorting a tree scrambles parents and children; only the flat view sorts.
			return Object.assign(options, { sortable: ir_view() === "Units" });
		},

		formatter(value, row, column, data, default_formatter) {
			if (!data) return default_formatter(value, row, column, data);
			const f = column.fieldname;
			if (data.is_total) {
				if (f === "voucher_no" || f === "group") return ir_bold(frappe.utils.escape_html(__("Total")));
				if (f === "qty" || f === "cost" || f === "receipts" || f === "no_cost" || f === "no_ad") {
					return ir_bold(default_formatter(value, row, column, data));
				}
				return "";
			}
			if (f === "receipt_class") return ir_pill(value);
			if (f === "group") {
				if (data.row_kind === "class") return ir_pill(value);
				return ir_bold(frappe.utils.escape_html(__(value || "")));
			}
			if (f === "flags") return ir_flags(value);
			if (f === "item_name" && value && data.item_code) {
				// Description is the human name; it links to the Item so the code
				// column can stay hidden by default.
				return `<a href="/app/item/${encodeURIComponent(data.item_code)}" data-doctype="Item" data-name="${frappe.utils.escape_html(data.item_code)}">${frappe.utils.escape_html(value)}</a>`;
			}
			if (f === "category" && value === "Mixed") {
				return `<span class="text-muted">${frappe.utils.escape_html(__(value))}</span>`;
			}
			let html = default_formatter(value, row, column, data);
			// Receipt rows (indent 0 in the tree) carry their totals in bold so the
			// eye lands on one row per document; lines and units stay regular.
			if (data.row_kind === "voucher" && (f === "voucher_no" || f === "source" || f === "cost" || f === "qty")) {
				html = ir_bold(html);
			}
			if (data.row_kind === "unit" && f === "serial_no" && data.indent) {
				html = `<span class="text-muted" style="margin-right:4px">↳</span>${html}`;
			}
			return html;
		},

		// The server marks the rows whose children are serial units; close them so
		// the default screen is one row per receipt line, serials one click away.
		// The tree footer's Expand All / Collapse All still works on top of this.
		after_datatable_render(datatable) {
			const rows = (frappe.query_report && frappe.query_report.data) || [];
			if (!datatable || !datatable.rowmanager || !rows.some((r) => r && r.collapse_children)) return;
			rows.forEach((r, i) => {
				if (r && r.collapse_children) {
					try {
						datatable.rowmanager.closeSingleNode(i);
					} catch (e) {
						/* a datatable build without tree nodes — nothing to collapse */
					}
				}
			});
		},
	};
})();
