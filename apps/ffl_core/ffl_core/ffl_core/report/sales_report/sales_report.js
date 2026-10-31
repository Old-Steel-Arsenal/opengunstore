// ===== Column picker =====
// Client-side, per-user, per-view column visibility. It ONLY toggles
// `frappe.query_report.columns[i].hidden` on screen; the server's get_columns is
// never touched, so the Product-view CSV tax worksheet always exports the full contract
// columns regardless of what is hidden here. Persistence is
// localStorage keyed by view + user. The `invoice` column is
// locked visible.
//
// The pure kernel below (normalize / hidden-set / apply / sets-equal — NO DOM, NO
// Frappe, NO localStorage) is dependency-free and unit-tested under Node
// (tests/js/sales_report_columns.test.js); the browser wrappers add the frappe /
// localStorage / dialog I/O.

const SR_LOCK_FIELD = "invoice";

// ---- pure kernel (dependency-free, unit-tested) ----

// Normalize a persisted hidden-fieldname list into a Set, ALWAYS stripping the
// lock column. The write side already excludes the lock, but a corrupt/stale
// persisted set — or a future change of SR_LOCK_FIELD to a column that some user
// had previously hidden — could name it. If the read side let it through,
// sr_apply_hidden would force that column visible (live set never contains it)
// while the saved set still names it, so saved≠live forever and
// after_datatable_render would re-render in an infinite loop. Stripping on read
// keeps read and write symmetric so the convergence guard actually converges.
function sr_normalize_hidden(rawList, lockField) {
	const s = new Set(Array.isArray(rawList) ? rawList : []);
	s.delete(lockField);
	return s;
}
function sr_hidden_set_of(columns) {
	return new Set((columns || []).filter((c) => c.hidden).map((c) => c.fieldname));
}
function sr_apply_hidden_to(columns, hiddenSet, lockField) {
	// The lock column is never hidden, whatever the saved set says.
	(columns || []).forEach((c) => {
		c.hidden = hiddenSet.has(c.fieldname) && c.fieldname !== lockField ? 1 : 0;
	});
}
function sr_sets_equal(a, b) {
	if (a.size !== b.size) return false;
	for (const x of a) if (!b.has(x)) return false;
	return true;
}
// Build the MultiCheck option list with a per-option `checked` flag. Frappe's
// MultiCheck reads `checked` on each option and IGNORES a field-level `default`,
// so the currently-shown columns must be pre-checked here — otherwise the dialog
// opens all-unchecked and a no-op Apply hides every column except the lock column.
// A column is checked (shown) unless it is in the hidden set; the
// lock column is always checked.
function sr_build_column_options(columns, hiddenSet, lockField) {
	return (columns || []).map((c) => ({
		label: c.label,
		value: c.fieldname,
		checked: c.fieldname === lockField || !hiddenSet.has(c.fieldname) ? 1 : 0,
	}));
}

// ---- pure kernel: redesign (curated defaults / serial+ref render / preset math) ----

// Curated default hidden-column set per view. Returned as a plain
// fieldname array; callers normalize (strip lock) and prune to the live columns
// before applying, so a default naming a column absent from a view can never
// wedge after_datatable_render in a render loop. Order hides Total (reads
// as Net Sales otherwise); the product views hide low-frequency support/flag
// columns to kill the 24-column horizontal overflow. All still one click back
// via the Columns picker, and the CSV tax worksheet always exports the full set.
// The two channel-fee columns are meant to be default-hidden and always
// CSV-exported, which is exactly what this mechanism does (hidden here, full set
// on export). The Texas worksheet columns (Ship-to State / Marketplace Exempt)
// stay VISIBLE in the flat Product view — that view IS the tax worksheet, and a
// worksheet whose key column has to be un-hidden first is one somebody files
// without it. Order Detail hides Ship-to State only because the order grid hides
// everything outside its 9-column order grid.
const SR_DEFAULT_HIDDEN = {
	// trade_in_applied / trade_in_payout: negative-ticket settlement —
	// zero on nearly every row, so out of the way on screen, always in the CSV.
	Order: ["total", "channel_fees", "profit_after_fees", "trade_in_applied", "trade_in_payout"],
	// Order Detail is now a 9-column order master grid (Date / Invoice /
	// Lines / Customer / Category / Net Sales / Cost / Profit / Margin). Everything
	// else in the BE's detail column set is hidden — the per-line columns (item /
	// serial / prices / line tax …) because lines live in the Dialog now, and
	// Channel / Sales Tax / Shipping / Qty / Total (restorable via the
	// picker). Lines_cell + Margin are FE-added (not in this set → visible).
	"Order Detail": [
		"channel", "item_code", "serial_no", "qty", "unit_price", "gross_merch",
		"trade_in_net", "net_amount", "line_tax", "shipping", "unclassified",
		"sales_tax", "tax_mismatch", "total", "order_ref",
		"channel_fees", "profit_after_fees", "ship_to_state",
		"trade_in_applied", "trade_in_payout",
	],
	Product: ["list_price", "discount", "gross_merch", "trade_in_net", "is_trade_in", "exempt_from_sales_tax"],
};
function sr_default_hidden(view) {
	return (SR_DEFAULT_HIDDEN[view] || []).slice();
}

// Intersect a hidden-fieldname set with the fieldnames actually present in the
// live columns. A curated default (or a stale persisted set) may name a column
// this view doesn't have; keeping it would make saved ≠ live forever and loop
// after_datatable_render (the same footgun the lock column taught us). Pruning
// guarantees saved ⊆ live-possible, so the convergence guard converges.
function sr_prune_to_columns(hiddenSet, columns) {
	const present = new Set((columns || []).map((c) => c.fieldname));
	const out = new Set();
	for (const f of hiddenSet) if (present.has(f)) out.add(f);
	return out;
}

// Split an aggregated serial string ("A; B; C") into trimmed, non-empty parts
// (mirrors the Python split_serials the BE uses to build the value).
function sr_parse_serials(raw) {
	return String(raw == null ? "" : raw)
		.split(";")
		.map((s) => s.trim())
		.filter((s) => s.length > 0);
}

// Serial cell: none → em dash; one → shown in full; many →
// "first +N more" with the full comma-joined list in the title. Purely
// count-based — a single serial is never truncated (design: single shows in
// full; column width is a CSS concern, not this function's).
function sr_serial_summary(raw) {
	const parts = sr_parse_serials(raw);
	if (parts.length === 0) return { text: "—", title: "", truncated: false };
	if (parts.length === 1) return { text: parts[0], title: "", truncated: false };
	return { text: `${parts[0]} +${parts.length - 1} more`, title: parts.join(", "), truncated: true };
}

// Order Ref cell: a POS sale carries no Woo order → em dash
// (muted); a web/B2B order shows its Woo number with a single leading "#".
function sr_order_ref_display(raw) {
	const v = String(raw == null ? "" : raw).trim();
	if (!v) return { text: "—", muted: true };
	// Strip ALL leading '#' so a value that already carries one — or several —
	// is not double-prefixed.
	return { text: `#${v.replace(/^#+/, "")}`, muted: false };
}

// Quick-range preset → {from, to} ISO dates from an explicit "today".
// Pure UTC date math so it is timezone-independent and unit-testable;
// the browser wrapper passes frappe.datetime's today. "Custom"/blank → null
// (the user drives from/to; a preset only pre-fills them).
function sr_iso_date(d) {
	return d.toISOString().slice(0, 10);
}
function sr_preset_range(preset, todayISO) {
	if (!preset || preset === "Custom") return null;
	const t = new Date(`${todayISO}T00:00:00Z`);
	const y = t.getUTCFullYear();
	const m = t.getUTCMonth();
	const day = t.getUTCDate();
	const mk = (yy, mm, dd) => sr_iso_date(new Date(Date.UTC(yy, mm, dd)));
	switch (preset) {
		case "Today":
			return { from: todayISO, to: todayISO };
		case "This Week": {
			// Week starts Monday. getUTCDay: 0=Sun..6=Sat → days back to Monday.
			const backToMonday = (t.getUTCDay() + 6) % 7;
			return { from: mk(y, m, day - backToMonday), to: todayISO };
		}
		case "This Month":
			return { from: mk(y, m, 1), to: todayISO };
		case "Last Month":
			// day 0 of this month = the last day of the previous month.
			return { from: mk(y, m - 1, 1), to: mk(y, m, 0) };
		case "This Quarter":
			return { from: mk(y, Math.floor(m / 3) * 3, 1), to: todayISO };
		case "Year to Date":
			return { from: mk(y, 0, 1), to: todayISO };
		default:
			return null;
	}
}

// Flag-card counts — mirrors the BE _report_summary scan EXACTLY so
// the split tooltip this drives always sums to the card's own number. Order view:
// Cost Flags = non-total rows with missing_cost. Product/Detail: Data Flags =
// product rows with missing_cost + DISTINCT invoices with tax_mismatch (two
// flagged lines of one order count once) + unclassified rows.
function sr_flag_counts(view, rows) {
	const body = (rows || []).filter((r) => r && !r.is_total);
	if (view === "Order Detail" || view === "Product") {
		const missing = body.filter((r) => r.row_kind === "product" && r.missing_cost).length;
		const mismatch = new Set(body.filter((r) => r.tax_mismatch).map((r) => r.invoice)).size;
		const unclassified = body.filter((r) => r.row_kind === "unclassified").length;
		return { missing, mismatch, unclassified, total: missing + mismatch + unclassified };
	}
	const missing = body.filter((r) => r.missing_cost).length;
	return { missing, mismatch: 0, unclassified: 0, total: missing };
}

// Split tooltip for the flag summary card: "N cost · N tax · N
// unclassified" on the product views; a plain understated-cost count on Order.
function sr_flag_tooltip(view, counts) {
	if (view === "Order Detail" || view === "Product") {
		return `${counts.missing} cost · ${counts.mismatch} tax · ${counts.unclassified} unclassified`;
	}
	return `${counts.missing} cost understated`;
}

// Flag counts summed from the per-invoice aggregates — the Order Detail path once
// the child rows have been removed from the datatable data. Sums to the same
// {missing, mismatch, unclassified, total} that sr_flag_counts derives by scanning
// the full flat rows (product-row missing + DISTINCT tax_mismatch invoices +
// unclassified rows), so the split tooltip stays consistent with the BE's card.
function sr_flag_counts_from_aggregates(aggregates) {
	let missing = 0;
	let mismatch = 0;
	let unclassified = 0;
	Object.values(aggregates || {}).forEach((agg) => {
		const f = (agg && agg.flags) || {};
		missing += f.missingCost || 0;
		if (f.taxMismatch) mismatch += 1;
		unclassified += f.unclassified || 0;
	});
	return { missing, mismatch, unclassified, total: missing + mismatch + unclassified };
}

// Aggregate the BE's flat Order Detail rows (order parent + product
// / shipping / unclassified children + a Total) into per-invoice order
// view-models the master grid and detail Dialog read. Everything the grid/Dialog
// need BEYOND the order row's own fields (line list, charges, line count, order
// consignment, category detail, flag counts) is DERIVED here client-side — so no
// BE change is needed.
function sr_aggregate_orders(rows) {
	const byInvoice = {};
	(rows || []).forEach((r) => {
		if (!r || r.is_total || r.row_kind === "total") return;
		const inv = r.invoice;
		if (!inv) return;
		if (!byInvoice[inv]) {
			byInvoice[inv] = { order: null, lines: [], charges: { shipping: 0, salesTax: 0, unclassified: [] } };
		}
		const agg = byInvoice[inv];
		if (r.row_kind === "order") {
			agg.order = r;
			agg.charges.shipping = Number(r.shipping) || 0;
			agg.charges.salesTax = Number(r.sales_tax) || 0;
		} else if (r.row_kind === "product") {
			agg.lines.push(r);
		} else if (r.row_kind === "unclassified") {
			agg.charges.unclassified.push({ label: r.category || "", amount: Number(r.unclassified) || 0 });
		}
		// shipping child rows are redundant — the order row already carries the
		// shipping total (read above); ignore them here.
	});
	Object.keys(byInvoice).forEach((inv) => {
		const agg = byInvoice[inv];
		agg.lineCount = agg.lines.length;
		agg.isConsignment = agg.lines.some((l) => l.is_consignment);
		agg.categoryDetail = [...new Set(agg.lines.map((l) => l.category).filter(Boolean))].sort().join(", ");
		agg.flags = {
			missingCost: agg.lines.filter((l) => l.missing_cost).length,
			// tax_mismatch is per-order; the BE stamps it on the order row AND its
			// lines. Read from either so this count matches sr_flag_counts' row scan
			// (parity: aggregates path == data-scan path).
			taxMismatch: !!((agg.order && agg.order.tax_mismatch) || agg.lines.some((l) => l.tax_mismatch)),
			unclassified: agg.charges.unclassified.length,
		};
	});
	return byInvoice;
}

// Order-level margin % from the order row: profit / gross-of-trade-in — the SAME
// base as the Order view. gross_merch = net − trade-in, so this
// matches Order-view margin exactly. gross 0 → 0 (no divide-by-zero).
function sr_order_margin(order) {
	const gross = Number((order && order.gross_merch) || 0);
	if (!gross) return 0;
	return (Number((order && order.profit) || 0) / gross) * 100;
}

// Dialog footer flag summary: "✓ No data flags on this
// order" when clean, else "⚠ …" listing each flag type present, " · "-joined.
function sr_order_footer(flags) {
	const f = flags || {};
	const missing = f.missingCost || 0;
	const uncl = f.unclassified || 0;
	if (missing === 0 && !f.taxMismatch && uncl === 0) {
		return { clean: true, text: "✓ No data flags on this order" };
	}
	const parts = [];
	if (missing > 0) parts.push(`${missing} line${missing === 1 ? "" : "s"} with understated cost`);
	if (f.taxMismatch) parts.push("tax mismatch");
	if (uncl > 0) parts.push(`${uncl} unclassified charge${uncl === 1 ? "" : "s"}`);
	return { clean: false, text: `⚠ ${parts.join(" · ")}` };
}

// Lines drill-down entry: "▸ N lines" anchor keyed by
// invoice; an order-level flag appends ⚠. An <a href="#"> so it is Tab-focusable
// + Enter-activatable; the click handler preventDefaults the "#". The
// data-invoice attribute is escaped defensively.
function sr_lines_cell_html(invoice, count, hasFlag) {
	const inv = String(invoice == null ? "" : invoice)
		.replace(/&/g, "&amp;")
		.replace(/"/g, "&quot;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;");
	const n = Number(count) || 0;
	const label = `▸ ${n} ${n === 1 ? "line" : "lines"}`;
	return `<a class="sr-lines" data-invoice="${inv}" href="#">${label}${hasFlag ? " ⚠" : ""}</a>`;
}

// Keyboard-activation keys for the ▸ Lines link: Enter + Space, the a11y
// convention for a link acting as a button. frappe-datatable swallows the
// anchor's native Enter→click, so the keydown handler wires activation explicitly.
function sr_is_activation_key(key) {
	return key === "Enter" || key === " " || key === "Spacebar";
}

// Column sort is OFF for the Order Detail master grid: the M2 reshape
// leaves the datatable's internal cell model misaligned with the columns, so
// native sort throws (frappe-datatable _sortRows). The master grid is a
// drill-down list (default date order; sort → the Order view, which is unaffected),
// so disabling sort there kills both the crash and the false ↑↓ affordance.
function sr_datatable_sortable(view) {
	return view !== "Order Detail";
}

// ---- browser wrappers (frappe / localStorage / DOM) ----

function sr_current_view() {
	return (frappe.query_report && frappe.query_report.get_filter_value("view")) || "Order";
}
function sr_storage_key() {
	return `sales_report::cols::${sr_current_view()}::${frappe.session.user}`;
}
function sr_read_hidden() {
	const stored = localStorage.getItem(sr_storage_key());
	// No saved preference yet → seed this view's curated default hidden set
	// An explicit "[]" (the user chose to show everything) is
	// distinct from absence and must NOT fall back to the default — that is how
	// "show all" survives a refresh. Prune to the live columns so a curated
	// default (or a stale set) naming a column absent from this view can't wedge
	// after_datatable_render in a render loop (same invariant as the lock column).
	let raw;
	if (stored === null) {
		raw = sr_default_hidden(sr_current_view());
	} else {
		try {
			raw = JSON.parse(stored);
		} catch (e) {
			raw = sr_default_hidden(sr_current_view());
		}
	}
	const normalized = sr_normalize_hidden(raw, SR_LOCK_FIELD);
	return sr_prune_to_columns(normalized, (frappe.query_report && frappe.query_report.columns) || []);
}
function sr_write_hidden(hiddenSet) {
	localStorage.setItem(sr_storage_key(), JSON.stringify([...hiddenSet]));
}
function sr_live_hidden_set() {
	return sr_hidden_set_of((frappe.query_report && frappe.query_report.columns) || []);
}
function sr_apply_hidden(hiddenSet) {
	sr_apply_hidden_to((frappe.query_report && frappe.query_report.columns) || [], hiddenSet, SR_LOCK_FIELD);
}

function sr_open_column_picker() {
	const cols = (frappe.query_report && frappe.query_report.columns) || [];
	if (!cols.length) {
		frappe.msgprint(__("Run the report first, then choose which columns to show."));
		return;
	}
	const view = sr_current_view();
	const hidden = sr_read_hidden();
	// per-option `checked` (NOT a field-level default) — see sr_build_column_options.
	const options = sr_build_column_options(cols, hidden, SR_LOCK_FIELD);

	const d = new frappe.ui.Dialog({
		title: __("Choose Columns — {0}", [view]),
		fields: [
			{
				fieldname: "note",
				fieldtype: "HTML",
				options: `<p class="text-muted small" style="margin-bottom:8px">${__(
					"Hidden columns only affect this screen; the Product view's CSV export always keeps the full tax-report columns."
				)}</p>`,
			},
			{
				fieldname: "cols",
				fieldtype: "MultiCheck",
				label: __("Columns"),
				options: options,
				columns: 3,
			},
		],
		primary_action_label: __("Apply"),
		primary_action(values) {
			const shownSet = new Set(values.cols || []);
			const newHidden = new Set();
			cols.forEach((c) => {
				if (c.fieldname === SR_LOCK_FIELD) return; // locked → never hidden
				if (!shownSet.has(c.fieldname)) newHidden.add(c.fieldname);
			});
			sr_write_hidden(newHidden);
			sr_apply_hidden(newHidden);
			frappe.query_report.render_datatable();
			d.hide();
		},
	});

	// Reset to default = clear this view's saved preference → the curated default
	// column set for this view, NOT all-columns. Removing the key
	// makes sr_read_hidden fall back to sr_default_hidden.
	d.set_secondary_action_label(__("Reset to default"));
	d.set_secondary_action(() => {
		localStorage.removeItem(sr_storage_key());
		sr_apply_hidden(sr_read_hidden());
		frappe.query_report.render_datatable();
		d.hide();
	});

	// "Select all" convenience (guarded: degrades gracefully if the API differs).
	try {
		if (typeof d.add_custom_action === "function") {
			d.add_custom_action(__("Select all"), () => {
				const ctrl = d.fields_dict.cols;
				if (ctrl && ctrl.$wrapper) {
					ctrl.$wrapper.find(":checkbox").prop("checked", true).trigger("change");
				}
			});
		}
	} catch (e) {
		// non-fatal — the core picker still works without Select all
	}

	d.show();

	// Visual lock for the invoice column (the FUNCTIONAL lock is already enforced
	// in primary_action; this only greys the checkbox). Guarded/non-fatal.
	setTimeout(() => {
		try {
			const ctrl = d.fields_dict.cols;
			if (ctrl && ctrl.$wrapper) {
				ctrl.$wrapper
					.find(`:checkbox[data-unit="${SR_LOCK_FIELD}"]`)
					.prop("checked", true)
					.prop("disabled", true);
			}
		} catch (e) {
			/* non-fatal */
		}
	}, 100);
}

// ---- redesign browser wiring (hint area / flags dialog / preset sync) ----

// Order-view column fieldnames for the two disambiguation columns the BE adds
// in _columns(). Keys are a FROZEN contract:
//   order_ref — the bare Woo order # ("2134"), "" for a POS sale; this formatter
//               adds the "#" prefix / em-dash presentation.
//   serials   — the invoice's serials aggregated, "; "-joined ("SNA; SNB").
// Only decorated in the Order view — the product views' own serial_no column is
// the full per-line CSV serial and is left untouched.
const SR_COL_ORDER_REF = "order_ref";
const SR_COL_SERIALS = "serials";

// Always-on flag legend. teal falls back to gray-700 where the
// Desk build doesn't ship --teal-600.
const SR_LEGEND_ITEMS = [
	{ icon: "⚠", color: "--orange-600", label: "cost understated" },
	{ icon: "⚠", color: "--orange-600", label: "tax mismatch" },
	{ icon: "◐", color: "--blue-600", label: "trade-in" },
	{ icon: "▲", color: "--teal-600, var(--gray-700)", label: "consignment" },
	{ icon: "●", color: "--orange-600", label: "unclassified charge" },
];
function sr_legend_html() {
	return SR_LEGEND_ITEMS
		.map(
			(it) =>
				`<span style="white-space:nowrap"><span style="color:var(${it.color})">${it.icon}</span> ${frappe.utils.escape_html(__(it.label))}</span>`
		)
		.join('<span style="opacity:.35"> · </span>');
}

// Always-on flag legend, injected just above the datatable.
// Idempotent — replaces any prior hint so replay/refresh passes don't stack.
// Muted, borderless, low visual weight. The view-purpose helper is
// rendered by the BE's `message` (per the lead's split: legend = FE .js, helper
// = BE message), so it is NOT duplicated here.
function sr_render_hint_area(datatable) {
	if (typeof $ === "undefined") return;
	const el =
		(datatable && datatable.wrapper) ||
		(frappe.query_report && frappe.query_report.$report && frappe.query_report.$report[0]);
	if (!el) return;
	const $wrap = $(el);
	const html = `<div class="sr-hint-area text-muted small" style="margin:2px 2px 8px;line-height:1.8">${sr_legend_html()}</div>`;
	$wrap.parent().find(".sr-hint-area").remove();
	$wrap.before(html);
}

// Grid height. Frappe sizes every report grid body as calc(100vh - 240px) — right
// when the grid sits straight under the filters. This report stacks summary cards
// (two rows of them in the detail views) and the flag legend above it, so the body
// starts 150–230px lower while keeping that height: its bottom — and the
// HORIZONTAL scrollbar that lives there — falls below the fold, and the page grows
// a second vertical scroller around the grid's own.
//
// The cap is a CSS rule, not an inline height, because frappe-datatable owns the
// inline one: setBodyStyle() strips `height` on every sort / column resize /
// filter-row toggle (none of which fire after_datatable_render), shrinks the body
// to short result sets, and decides overflow-y from the COMPUTED height. A
// `max-height … !important` survives all of that, keeps the shrink-to-content
// behaviour, feeds the right number into that overflow decision, and also beats
// Frappe's own `height … !important` on narrow screens. JS only measures where
// the body starts and publishes it as --sr-grid-max; the rule is scoped to this
// report's route, so no other query report (they share this page) is touched.
// The reserve keeps the report footer on screen — its real height plus the gap
// around it (it wraps to two lines in the wide Product view); the floor keeps a
// usable grid on a very short window (the page scrolls there, as any report does).
const SR_GRID_FOOTER_GAP = 22;
const SR_GRID_FOOTER_DEFAULT = 34;
const SR_GRID_MIN_HEIGHT = 240;
const SR_GRID_ROUTE = "query-report/Sales Report";
function sr_grid_height_css(top, footer_height) {
	const footer = Number(footer_height) > 0 ? Math.round(Number(footer_height)) : SR_GRID_FOOTER_DEFAULT;
	const offset = Math.max(0, Math.round(Number(top) || 0)) + footer + SR_GRID_FOOTER_GAP;
	return `max(${SR_GRID_MIN_HEIGHT}px, calc(100vh - ${offset}px))`;
}
function sr_grid_rule_css() {
	return `body[data-route="${SR_GRID_ROUTE}"] .datatable .dt-scrollable`
		+ ` { max-height: var(--sr-grid-max, none) !important; }`;
}

// Measured on the NEXT frame, never inside after_datatable_render: at hook time
// Frappe's 620px "loading" placeholder is still displayed above the grid (it is
// hidden right after the hook returns), which would cap the body ~620px too
// short. The live datatable is looked up at measure time too — Order Detail
// destroys and rebuilds its grid mid-render (sr_force_datatable_rebuild). A hidden
// report page (the user drilled into an invoice; frappe.query_report still points
// here) measures top = 0, so it is skipped. Calls coalesce per frame.
let sr_fit_pending = false;
function sr_fit_grid_height() {
	if (typeof requestAnimationFrame === "undefined" || sr_fit_pending) return;
	sr_fit_pending = true;
	requestAnimationFrame(() => {
		sr_fit_pending = false;
		const qr = frappe.query_report;
		if (!qr || qr.report_name !== "Sales Report" || !qr.datatable || !qr.datatable.wrapper) return;
		const body = qr.datatable.wrapper.querySelector(".dt-scrollable");
		if (!body || body.offsetParent === null) return;
		// Viewport top + how far the page is scrolled = where the body sits at scroll 0.
		const scroller = body.closest(".main-section");
		const scrolled = scroller ? scroller.scrollTop : window.scrollY || 0;
		const footer = qr.$report_footer && qr.$report_footer[0];
		// Whatever the page keeps clear under its content counts too — Frappe's
		// onboarding widget pads the scroller by 90px while it is showing.
		const padding = scroller ? parseFloat(getComputedStyle(scroller).paddingBottom) || 0 : 0;
		document.body.style.setProperty("--sr-grid-max", sr_grid_height_css(
			body.getBoundingClientRect().top + scrolled, (footer ? footer.offsetHeight : 0) + padding));
	});
}

// Bound ONCE, from onload. The observer watches the report's content column: a
// window resize, the Desk sidebar collapsing, full-width toggling, the cards
// re-wrapping, the loading placeholder going away — anything that can move the
// grid changes that box. Publishing the same value again changes no size, so the
// observer settles after one pass.
let sr_grid_observer = null;
function sr_watch_grid_height(report) {
	frappe.dom.set_style(sr_grid_rule_css(), "sr-grid-fit-style");
	const main = report && report.page && report.page.main && report.page.main[0];
	if (!main || sr_grid_observer || typeof ResizeObserver === "undefined") return;
	sr_grid_observer = new ResizeObserver(sr_fit_grid_height);
	sr_grid_observer.observe(main);
}

// Native report_summary cards carry no tooltip field, so the flag card's split
// ("N cost · N tax · N unclassified") is added here in .js.
// The flag card is ALWAYS the last summary card (Cost Flags in Order, Data Flags
// in the product views), so target the last child — locale-independent, and a
// graceful no-op if the summary isn't rendered. Counts come from the same rows
// the BE card counted (sr_flag_counts mirrors _report_summary), so the split
// always sums to the card's own number.
function sr_set_flag_card_tooltip() {
	if (typeof $ === "undefined") return;
	// Fully non-fatal (lead G3b gate): if a future Frappe summary-DOM change makes
	// the card selector miss — or anything else here goes wrong — degrade to a
	// silent no-op. Never throw, never spam the console. The card's own aggregate
	// number (rendered by the BE) stays; only the hover split is lost. Mirrors the
	// non-fatal try/catch the column picker already uses for its DOM touches.
	try {
		const view = sr_current_view();
		// Order Detail removes its child rows from the datatable data, so its
		// Data Flags split is summed from the cached per-invoice aggregates; the
		// other views scan their (intact) flat rows. Both paths give the same counts.
		const counts = view === "Order Detail"
			? sr_flag_counts_from_aggregates(sr_od_aggregates)
			: sr_flag_counts(view, (frappe.query_report && frappe.query_report.data) || []);
		const tip = sr_flag_tooltip(view, counts);
		const $card = $(".report-summary").children().last();
		if ($card && $card.length) $card.attr("title", tip);
	} catch (e) {
		/* non-fatal — summary tooltip is an enhancement, not load-bearing */
	}
}

// "Flags…" toolbar dialog: every symbol + colour + full meaning, and the
// filing gate (the Cost/Data Flags card must read Clean).
function sr_open_flags_help() {
	const rows = [
		["⚠", "--orange-600", "Cost understated", "A firearm on this sale has no recorded purchase price — cost and profit are understated."],
		["⚠", "--orange-600", "Tax mismatch", "Per-line tax disagrees with the charged sales tax — audit this order against the GL."],
		["●", "--orange-600", "Unclassified charge", "A charge on an account that is neither sales tax nor shipping — classify it before filing."],
		["◐", "--blue-600", "Trade-in allowance", "Trade-in allowance (non-cash consideration for a gun taken into inventory)."],
		["▲", "--teal-600, var(--gray-700)", "Consignment", "Consigned gun — the profit shown is the store's commission, not full margin."],
	];
	const body = rows
		.map(
			([icon, color, name, desc]) =>
				`<tr>
					<td style="text-align:center;font-size:1.15em;color:var(${color})">${icon}</td>
					<td style="white-space:nowrap"><b>${frappe.utils.escape_html(__(name))}</b></td>
					<td class="text-muted">${frappe.utils.escape_html(__(desc))}</td>
				</tr>`
		)
		.join("");
	const d = new frappe.ui.Dialog({
		title: __("Data flags"),
		fields: [
			{
				fieldtype: "HTML",
				options: `
					<p class="text-muted small" style="margin-bottom:8px">${__(
						"Before you cite this report for filing, the summary's Cost / Data Flags card must read Clean (0)."
					)}</p>
					<table class="table table-sm">${body}</table>`,
			},
		],
		primary_action_label: __("Close"),
		primary_action() {
			d.hide();
		},
	});
	d.show();
}

// Quick-range preset ⇄ from/to sync. A guard stops the programmatic
// from/to writes (when a preset fills them) from bouncing back and re-setting the
// preset to Custom; a genuine manual date edit still falls the preset back to
// Custom.
let sr_syncing_preset = false;
// Hold the guard across the current task + the synchronous change dispatch that
// set_filter_value fires (the from/to on_change), then release on the next tick.
// The report refresh() the set triggers is async and never re-enters on_change,
// so a setTimeout(0) release is enough and won't suppress a later manual edit.
function sr_release_preset_guard() {
	setTimeout(() => {
		sr_syncing_preset = false;
	}, 0);
}
function sr_apply_preset() {
	const range = sr_preset_range(
		frappe.query_report.get_filter_value("period_preset"),
		frappe.datetime.get_today()
	);
	if (!range) return; // Custom / blank → leave from/to untouched
	sr_syncing_preset = true;
	frappe.query_report.set_filter_value({ from_date: range.from, to_date: range.to });
	sr_release_preset_guard();
}
function sr_revert_preset_to_custom() {
	if (sr_syncing_preset) return; // programmatic fill, not a manual edit
	if (frappe.query_report.get_filter_value("period_preset") !== "Custom") {
		sr_syncing_preset = true;
		frappe.query_report.set_filter_value("period_preset", "Custom");
		sr_release_preset_guard();
	}
}

// ---- Order Detail: master grid reshape + per-order detail Dialog ----

// Per-invoice aggregates for the currently-rendered Order Detail rows, rebuilt
// each render. The lines_cell formatter and the Dialog read this (keyed by
// invoice) instead of re-scanning the flat rows.
let sr_od_aggregates = {};

function sr_is_order_detail() {
	return sr_current_view() === "Order Detail";
}

// Insert an FE-computed column into a columns array just AFTER `afterField`.
// Idempotent — no-op if the fieldname already exists.
function sr_raw_insert(cols, col, afterField) {
	if (!cols || cols.some((c) => c.fieldname === col.fieldname)) return false;
	const at = cols.findIndex((c) => c.fieldname === afterField);
	cols.splice(at >= 0 ? at + 1 : cols.length, 0, col);
	return true;
}

// Prepare the Order Detail master grid (client aggregation): cache the
// per-invoice aggregates, augment each order (and Total) row IN PLACE with the
// derived margin / category_detail / is_consignment the 9-col grid shows (the BE
// detail row lacks them), and add the FE Lines / Order Ref / Margin columns.
//
// CRITICAL: the FE columns are added to raw_data.columns and qr.columns is
// rebuilt through the query-report's own prepare_columns(), so they get the SAME
// per-cell `format` binding as the BE columns. Without that binding the report
// formatter is never invoked for them and the datatable renders them EMPTY
// (verified in dev). Returns true when it just added columns — the caller must
// then force a FRESH datatable (sr_force_datatable_rebuild) so the new columns
// bind and treeView clears.
function sr_order_detail_prepare() {
	const qr = frappe.query_report;
	const rc = qr && qr.raw_data && qr.raw_data.columns;
	// Already processed this server-response cycle (raw_data carries our FE Lines
	// column)? Bail early — and crucially do NOT re-aggregate, because by now the
	// datatable's data has had its child rows removed (M2 below) and re-scanning
	// would lose the line detail the Dialog needs.
	if (!rc || rc.some((c) => c.fieldname === "lines_cell")) return false;
	const data = (qr && qr.data) || [];
	sr_od_aggregates = sr_aggregate_orders(data);
	data.forEach((r) => {
		if (!r) return;
		// The Total row keeps a Margin % from its own gross_merch/profit,
		// and a Net Sales for the only visible income column. Prefer the BE's
		// own net_sales (carried since net sales became gross of trade-in and
		// equal to Σ parent rows == the Net Sales card), then gross_merch (same
		// figure), and only then legacy net_amount (older payloads, where all
		// three agreed). Backfilling net_amount unconditionally would resurrect
		// the netted figure on any trade-in period and break the "cards == Total"
		// anchor this exists to preserve.
		if (r.is_total) {
			r.margin = sr_order_margin(r);
			if (r.net_sales == null) {
				r.net_sales = r.gross_merch != null ? r.gross_merch : r.net_amount;
			}
			return;
		}
		if (r.row_kind !== "order") return;
		const agg = sr_od_aggregates[r.invoice];
		if (!agg) return;
		r.margin = sr_order_margin(r);
		r.category_detail = agg.categoryDetail;
		r.is_consignment = agg.isConsignment;
	});
	sr_raw_insert(rc, { label: __("Lines"), fieldname: "lines_cell", fieldtype: "Data", width: 100 }, "invoice");
	// Order Ref (default-hidden/restorable): the BE carries the
	// order_ref scalar on the parent order row but adds no column; add it so the
	// picker can restore it. Formatter renders it "#<woo>" / "—" like Order view.
	sr_raw_insert(rc, { label: __("Order Ref"), fieldname: "order_ref", fieldtype: "Data", width: 110 }, "customer");
	sr_raw_insert(rc, { label: __("Margin %"), fieldname: "margin", fieldtype: "Percent", width: 90 }, "profit");
	if (typeof qr.prepare_columns === "function") {
		// m1: the one Frappe-internal call not yet guarded. If a future version
		// changes its signature, degrade to the raw columns (no FE format binding)
		// rather than throwing out of the reshape.
		try {
			const prepared = qr.prepare_columns(rc);
			if (prepared) qr.columns = prepared;
		} catch (e) {
			/* non-fatal */
		}
	}
	// Belt-and-suspenders: mark every column non-sortable too, so the header
	// offers no sort affordance even if the global sortable:false option (set in
	// get_datatable_options) is applied inconsistently across datatable versions.
	(qr.columns || []).forEach((c) => {
		if (c) c.sortable = false;
	});
	// Drop the child (line/shipping/
	// unclassified) rows from the data the datatable renders — only order + Total
	// rows remain. The line detail is already captured in sr_od_aggregates for the
	// Dialog. This is the ROOT fix for the sort/column-resize fragility: a native
	// re-render can no longer resurface children (they're not in the data), which
	// retires the fragile display:none overlay. Order Detail's CSV export re-runs
	// server-side on the BE's full flat data, so it is unaffected.
	qr.data = data.filter((r) => r && (r.is_total || r.row_kind === "order"));
	return true;
}

// Force a FRESH datatable (render_datatable reuses the existing one). Needed once
// after adding the FE columns: reuse would leave them without a format binding
// AND keep treeView on — the BE detail rows carry `indent`, which frappe latches
// into tree mode and preserves across reuse. Strip indent + clear the tree flag +
// rebuild so the grid is flat (no chevrons) and the new columns render. Non-fatal.
function sr_force_datatable_rebuild() {
	const qr = frappe.query_report;
	try {
		qr.tree_report = false;
		(qr.data || []).forEach((r) => {
			if (r) delete r.indent;
		});
		if (qr.datatable && typeof qr.datatable.destroy === "function") qr.datatable.destroy();
		qr.datatable = null;
		if (qr.$report && qr.$report.find) qr.$report.find(".datatable").remove();
	} catch (e) {
		/* non-fatal — fall through to a normal render */
	}
	qr.render_datatable();
}

// Delegated ▸ Lines activation → open the order Dialog. Handles BOTH mouse click
// and keyboard (Enter/Space): frappe-datatable swallows the anchor's native
// Enter→click, so keyboard activation is wired explicitly. Idempotent
// (namespaced, rebound each render); preventDefault stops the "#" jump,
// stopPropagation keeps it independent of the Invoice cell's Dynamic Link.
function sr_bind_lines_click(datatable) {
	if (typeof $ === "undefined") return;
	try {
		const el = (datatable && datatable.wrapper) ||
			(frappe.query_report && frappe.query_report.$report && frappe.query_report.$report[0]);
		if (!el) return;
		const open = (anchor, e) => {
			e.preventDefault();
			e.stopPropagation();
			const inv = $(anchor).attr("data-invoice");
			if (inv) sr_open_order_dialog(inv);
		};
		$(el)
			.off("click.srlines keydown.srlines")
			.on("click.srlines", ".sr-lines", function (e) {
				open(this, e);
			})
			// Frappe-datatable swallows the anchor's native Enter→click, so wire
			// keyboard activation (Enter + Space) explicitly for real a11y reachability.
			.on("keydown.srlines", ".sr-lines", function (e) {
				if (sr_is_activation_key(e.key)) open(this, e);
			});
	} catch (e) {
		/* non-fatal */
	}
}

// Build + show the per-order detail Dialog: subline chips + 7-number
// summary bar + LINE ITEMS table + CHARGES section (only if any) + flag footer.
function sr_open_order_dialog(invoice) {
	try {
		const agg = (sr_od_aggregates || {})[invoice];
		if (!agg || !agg.order) {
			frappe.msgprint(__("No line items recorded for {0}.", [invoice]));
			return;
		}
		const o = agg.order;
		const esc = (s) => frappe.utils.escape_html(String(s == null ? "" : s));
		const cur = (v) => frappe.format(flt(v), { fieldtype: "Currency" });
		const pct = (v) => frappe.format(flt(v), { fieldtype: "Percent" });

		const chips = [
			o.channel ? esc(o.channel) : null,
			o.order_ref ? `${esc(__("Order Ref"))} #${esc(o.order_ref)}` : null,
			o.posting_date ? frappe.format(o.posting_date, { fieldtype: "Date" }) : null,
		].filter(Boolean).join(' <span style="opacity:.4">·</span> ');

		const summary = [
			[__("Net Sales"), cur(o.net_sales)],
			// Trade-in orders report Net Sales gross of the allowance, so the bar
			// would no longer foot to Total without naming the credit: Net Sales
			// + Trade-in + Sales Tax + Shipping == Total again.
			...(flt(o.trade_in_net)
				? [[__("Trade-in"), `<span style="color:var(--blue-600)">◐ ${cur(o.trade_in_net)}</span>`]]
				: []),
			[__("Sales Tax"), cur(agg.charges.salesTax)],
			[__("Shipping"), cur(agg.charges.shipping)],
			[__("Total"), cur(o.total)],
			// A negative ticket: the store paid the customer the difference.
			...(flt(o.trade_in_payout)
				? [[__("Paid to customer"), cur(o.trade_in_payout)]]
				: []),
			[__("Cost"), cur(o.cost)],
			[__("Goods Profit"), cur(o.profit)],
			[__("Margin %"), pct(sr_order_margin(o))],
		].map(([l, v]) => `<div style="min-width:88px"><div class="text-muted" style="font-size:11px">${l}</div><div>${v}</div></div>`).join("");

		const lineRows = agg.lines.map((ln) => {
			const item = `${esc(ln.item_name || ln.item_code || "")}${ln.item_code && ln.item_name ? ` <span class="text-muted">${esc(ln.item_code)}</span>` : ""}`;
			const serial = ln.serial_no ? esc(ln.serial_no) : '<span class="text-muted">—</span>';
			let cost = cur(ln.cost), net = cur(ln.net_amount), profit = cur(ln.profit);
			if (ln.missing_cost) cost = `<span style="color:var(--orange-600)" title="${esc(__("A firearm on this sale has no recorded purchase price — cost and profit are understated."))}">${cost} ⚠</span>`;
			if (ln.is_trade_in) { net = `<span style="color:var(--blue-600)">◐ ${net}</span>`; profit = `<span style="color:var(--blue-600)">${profit}</span>`; }
			if (ln.is_consignment) profit = `${profit} <span style="color:var(--teal-600, var(--gray-700))" title="${esc(__("Consigned gun — the profit shown is the store's commission, not full margin."))}">▲</span>`;
			return `<tr><td>${item}</td><td>${serial}</td><td class="text-right">${flt(ln.qty)}</td><td class="text-right">${cur(ln.unit_price)}</td><td class="text-right">${net}</td><td class="text-right">${cost}</td><td class="text-right">${profit}</td></tr>`;
		}).join("");

		const chargeRows = [];
		if (flt(agg.charges.shipping)) chargeRows.push([__("Shipping"), cur(agg.charges.shipping), ""]);
		if (flt(agg.charges.salesTax)) chargeRows.push([__("Sales tax"), cur(agg.charges.salesTax), ""]);
		agg.charges.unclassified.forEach((u) => chargeRows.push([esc(u.label), cur(u.amount), "color:var(--orange-600)"]));
		const chargesBlock = chargeRows.length
			? `<div class="text-muted" style="font-size:11px;margin:14px 0 4px">${__("CHARGES")}</div>
			   <table class="table table-sm" style="margin:0"><tbody>${chargeRows.map(([l, v, s]) => `<tr><td style="${s}">${l}</td><td class="text-right" style="${s}">${v}</td></tr>`).join("")}</tbody></table>`
			: "";

		const footer = sr_order_footer(agg.flags);
		const footerHtml = `<div style="margin-top:14px;${footer.clean ? "color:var(--text-muted)" : "color:var(--orange-600)"}">${esc(footer.text)}</div>`;

		const html = `
			<div class="text-muted small" style="margin:-4px 0 10px">${chips}</div>
			<div style="display:flex;gap:18px;flex-wrap:wrap;padding:10px 0;border-bottom:1px solid var(--border-color)">${summary}</div>
			<div class="text-muted" style="font-size:11px;margin:14px 0 4px">${__("LINE ITEMS")}</div>
			<table class="table table-sm" style="margin:0"><thead><tr>
				<th>${__("Item")}</th><th>${__("Serial")}</th><th class="text-right">${__("Qty")}</th>
				<th class="text-right">${__("Unit Price")}</th><th class="text-right">${__("Net")}</th>
				<th class="text-right">${__("Cost")}</th><th class="text-right">${__("Profit")}</th>
			</tr></thead><tbody>${lineRows || `<tr><td colspan="7" class="text-muted">${__("No line items recorded.")}</td></tr>`}</tbody></table>
			${chargesBlock}${footerHtml}`;

		const d = new frappe.ui.Dialog({
			title: `${esc(o.invoice)} · ${esc(o.customer || "")}`,
			size: "large",
			fields: [{ fieldtype: "HTML", options: html }],
			primary_action_label: __("Close"),
			primary_action() {
				d.hide();
			},
		});
		if (o.voucher_type && o.invoice) {
			d.set_secondary_action_label(__("Open invoice ↗"));
			d.set_secondary_action(() => frappe.set_route("Form", o.voucher_type, o.invoice));
		}
		d.show();
	} catch (e) {
		frappe.msgprint(__("Could not open the order detail."));
	}
}

if (typeof frappe !== "undefined") {
	frappe.query_reports["Sales Report"] = {
		// Declaration order == render order (Frappe lays filters out left→right).
		// Grouped for a clean mental model: Period → View → Segment.
		filters: [
			// --- Period ---
			{
				fieldname: "period_preset",
				label: __("Period"),
				fieldtype: "Select",
				// Quick range: fills from/to on change. from/to stay the
				// reqd source of truth — this only pre-fills them, so saved filters and
				// deep links are unaffected. Plain-language options, no "MTD"-style
				// jargon (P-D). A manual date edit falls this back to "Custom".
				options: ["Custom", "Today", "This Week", "This Month", "Last Month", "This Quarter", "Year to Date"].join("\n"),
				default: "Custom",
				on_change() {
					sr_apply_preset();
				},
			},
			{
				fieldname: "from_date",
				label: __("From"),
				fieldtype: "Date",
				default: frappe.datetime.month_start(),
				reqd: 1,
				on_change() {
					sr_revert_preset_to_custom();
				},
			},
			{
				fieldname: "to_date",
				label: __("To"),
				fieldtype: "Date",
				default: frappe.datetime.get_today(),
				reqd: 1,
				on_change() {
					sr_revert_preset_to_custom();
				},
			},
			// --- View ---
			{
				fieldname: "view",
				label: __("View"),
				fieldtype: "Select",
				// Stored VALUES are unchanged (Python dispatch keys off them); only the
				// display LABELS are friendlier. "Order" is the original
				// invoice-per-row view (byte-for-byte unchanged). "Order Detail" expands
				// each order into indented product/shipping/tax child rows. "Product" is
				// the flat order×product table for the CPA tax CSV.
				options: [
					{ value: "Order", label: __("Orders — one row per sale") },
					{ value: "Order Detail", label: __("Orders + line detail") },
					{ value: "Product", label: __("Tax lines (CSV export)") },
				],
				default: "Order",
			},
			// --- Segment ---
			{
				fieldname: "channel",
				label: __("Channel"),
				fieldtype: "Select",
				// The channels _channel() can return, and the only values
				// _apply_channel_filter can be sent; blank = all. (Replaces the old
				// 2-way POS/Order set; the Python filter still accepts a legacy
				// "Order" value as all direct SIs.) A channel missing from this list
				// is a branch on the server nothing can reach.
				options: ["", "POS", "Web", "GunBroker", "Manual"].join("\n"),
				default: "",
			},
			{
				fieldname: "product_type",
				label: __("Product Type"),
				fieldtype: "Select",
				// Values must match the report's Python checks ("Firearms" /
				// "Non-Firearms"); blank = all.
				options: ["", "Firearms", "Non-Firearms"].join("\n"),
				default: "",
			},
		],

		// Toolbar buttons: the column picker and the flag legend/help dialog.
		onload(report) {
			report.page.add_inner_button(__("Columns"), sr_open_column_picker);
			report.page.add_inner_button(__("Flags…"), sr_open_flags_help);
			sr_watch_grid_height(report);
		},

		// Order Detail is NO LONGER a tree — it renders as a clean order
		// master grid, with each order's lines in an on-demand Dialog. No view uses
		// treeView now. Column sort is disabled for Order Detail ONLY: its M2
		// reshape breaks native sort (see sr_datatable_sortable); Order / Product keep
		// their native sort.
		get_datatable_options(options) {
			const view = frappe.query_report.get_filter_value("view");
			return Object.assign(options, { treeView: false, sortable: sr_datatable_sortable(view) });
		},

		// Re-apply the saved per-view column preference after every render. The
		// server re-run resets all columns to visible on each refresh/filter change,
		// so this replays the user's hidden set. Convergence guard: if the live
		// hidden set already matches the saved set, do nothing — so the render it
		// triggers settles after exactly one extra pass instead of looping forever.
		after_datatable_render(datatable) {
			// Flag legend hint area + the flag-card split tooltip
			// both re-rendered idempotently on every pass. Done BEFORE the
			// convergence early-return so they are present once the replay settles.
			// (The view-purpose helper is the BE's `message`, not rendered here.)
			sr_render_hint_area(datatable);
			sr_set_flag_card_tooltip();
			// Deferred to the next frame (see sr_fit_grid_height) — every pass asks,
			// the last one wins.
			sr_fit_grid_height();
			// Order Detail master-grid reshape (client aggregation). Cache
			// aggregates + augment order rows + add the FE Lines / Margin columns FIRST;
			// if columns were just added, re-render once so they appear, then let the
			// next pass continue.
			if (sr_is_order_detail() && sr_order_detail_prepare()) {
				sr_force_datatable_rebuild();
				return;
			}
			// Replay the saved / curated-default hidden set — the server re-run resets
			// every column to visible on each refresh. sr_read_hidden already prunes to
			// the live columns, so the guard converges after one extra pass instead of
			// looping forever.
			const saved = sr_read_hidden();
			if (!sr_sets_equal(saved, sr_live_hidden_set())) {
				sr_apply_hidden(saved);
				frappe.query_report.render_datatable();
				return;
			}
			// Converged. Order Detail post-step: (re)bind the ▸ Lines clicks. Child
			// rows are already gone from the datatable data (removed in prepare, M2),
			// so no DOM hiding is needed — a native sort/column-resize re-render can't
			// resurface them.
			if (sr_is_order_detail()) {
				sr_bind_lines_click(datatable);
			}
		},

		formatter(value, row, column, data, default_formatter) {
			value = default_formatter(value, row, column, data);
			if (!data) return value;

			// --- Identity columns that FULLY replace the cell (returned first). ---
			// Order Ref (#woo / — for POS): the Order view and the Order
			// Detail master grid both carry order_ref on their order rows.
			if (
				(sr_current_view() === "Order" || sr_is_order_detail()) &&
				column.fieldname === SR_COL_ORDER_REF
			) {
				const r = sr_order_ref_display(data[SR_COL_ORDER_REF]);
				const inner = r.muted
					? `<span class="text-muted">${r.text}</span>`
					: frappe.utils.escape_html(r.text);
				return data.is_total ? `<b>${inner}</b>` : inner;
			}
			// Serial (truncate + title) — Order view ONLY; the product views' own
			// serial_no column is the full CSV serial and is left untouched.
			if (sr_current_view() === "Order" && column.fieldname === SR_COL_SERIALS) {
				const s = sr_serial_summary(data[SR_COL_SERIALS]);
				let inner;
				if (s.text === "—") {
					inner = `<span class="text-muted">—</span>`;
				} else if (s.truncated) {
					inner = `<span title="${frappe.utils.escape_html(s.title)}">${frappe.utils.escape_html(s.text)}</span>`;
				} else {
					inner = frappe.utils.escape_html(s.text);
				}
				return data.is_total ? `<b>${inner}</b>` : inner;
			}

			// --- Order Detail "Lines" drill-down cell: the ▸ N lines
			// entry, fully replacing the cell. Only order rows carry it (child rows are
			// hidden; the Total row shows none). ---
			if (sr_is_order_detail() && column.fieldname === "lines_cell") {
				if (data.is_total || data.row_kind !== "order") return "";
				const agg = (sr_od_aggregates || {})[data.invoice];
				if (!agg) return "";
				const hasFlag = agg.flags.missingCost > 0 || agg.flags.taxMismatch || agg.flags.unclassified > 0;
				return sr_lines_cell_html(data.invoice, agg.lineCount, hasFlag);
			}

			// Totals row and the Order-Detail parent (order) row — bold across the line.
			if (data.is_total || data.row_kind === "order") {
				value = `<b>${value}</b>`;
			}
			// Loss → red profit / margin.
			if (
				(column.fieldname === "profit" || column.fieldname === "margin") &&
				flt(data[column.fieldname]) < 0
			) {
				value = `<span style="color:var(--red-600)">${value}</span>`;
			}
			// Cost we couldn't fully resolve (a gun with no recorded purchase price):
			// flag the Cost cell so the inflated profit is obviously an estimate.
			if (column.fieldname === "cost" && data.missing_cost) {
				value = `<span style="color:var(--orange-600)" title="${__(
					"A firearm on this sale has no recorded purchase price — cost and profit are understated."
				)}">${value} ⚠</span>`;
			}
			// Per-line tax (item_wise_tax_details) disagrees with the charged
			// post-discount sales tax — a real item-wise/GL anomaly to audit.
			if (
				(column.fieldname === "line_tax" || column.fieldname === "sales_tax") &&
				data.tax_mismatch
			) {
				value = `<span style="color:var(--orange-600)" title="${__(
					"Per-line tax disagrees with the charged sales tax — audit this order against the GL."
				)}">${value} ⚠</span>`;
			}
			// Fail-closed: a charge on an account that is neither sales tax nor
			// shipping gets its own row (never folded into tax) — flag it so the CPA
			// classifies it before filing. Tint the whole row; the ● legend mark sits
			// once, on the charge-amount cell, so it isn't repeated across the line.
			if (data.row_kind === "unclassified") {
				const mark = column.fieldname === "unclassified" ? " ●" : "";
				value = `<span style="color:var(--orange-600)" title="${__(
					"Charge on an account that is neither sales tax nor shipping — classify before filing."
				)}">${value}${mark}</span>`;
			}
			// Trade-in credit: negative revenue and (on historical net-method sales)
			// negative tax are real — tint them so they read as an allowance, not an
			// error. The ◐ legend mark sits once, on the net (allowance) cell.
			if (
				data.is_trade_in &&
				(column.fieldname === "net_amount" || column.fieldname === "line_tax")
			) {
				const mark = column.fieldname === "net_amount" ? "◐ " : "";
				value = `<span style="color:var(--blue-600)" title="${__(
					"Trade-in allowance (non-cash consideration for a gun taken into inventory)."
				)}">${mark}${value}</span>`;
			}
			// A trade-in ORDER row (Order view / Order Detail parent): Net Sales is
			// gross of the allowance while Total stays the charged amount, so this
			// is the one row where Net Sales > Total. Mark the cell and name the
			// allowance so the gap reads as the trade-in, not a data error.
			if (data.is_trade_in && column.fieldname === "net_sales" && !data.is_total) {
				const allowance = frappe.format(Math.abs(flt(data.trade_in_net)), { fieldtype: "Currency" });
				value = `<span style="color:var(--blue-600)" title="${__(
					"Sale reported gross of a {0} trade-in allowance (a gun taken into inventory); Total is the amount actually charged.",
					[allowance]
				)}">◐ ${value}</span>`;
			}
			// "Mixed" category → tooltip listing the real item groups.
			// category_detail is an additive BE key; absent → no tooltip (graceful).
			if (column.fieldname === "category" && data.category === "Mixed" && data.category_detail) {
				value = `<span title="${frappe.utils.escape_html(data.category_detail)}">${value}</span>`;
			}
			// Consignment gun → mark the Profit cell so the reader knows the profit is
			// the store's commission, not full margin. Additive
			// ▲ after any loss-red wrapping; only fires where is_consignment is set.
			if (column.fieldname === "profit" && data.is_consignment) {
				value = `${value} <span style="color:var(--teal-600, var(--gray-700))" title="${__(
					"Consigned gun — the profit shown is the store's commission, not full margin."
				)}">▲</span>`;
			}
			return value;
		},
	};
}

// Node unit tests import the pure kernel (no DOM / Frappe). Harmless in the
// browser, where `module` is undefined.
if (typeof module !== "undefined" && module.exports) {
	module.exports = {
		SR_LOCK_FIELD,
		sr_normalize_hidden,
		sr_hidden_set_of,
		sr_apply_hidden_to,
		sr_sets_equal,
		sr_build_column_options,
		sr_default_hidden,
		sr_prune_to_columns,
		sr_parse_serials,
		sr_serial_summary,
		sr_order_ref_display,
		sr_preset_range,
		sr_flag_counts,
		sr_flag_tooltip,
		sr_set_flag_card_tooltip,
		sr_aggregate_orders,
		sr_order_margin,
		sr_order_footer,
		sr_lines_cell_html,
		sr_flag_counts_from_aggregates,
		sr_order_detail_prepare,
		sr_is_activation_key,
		sr_datatable_sortable,
		sr_grid_height_css,
		sr_grid_rule_css,
		SR_COL_ORDER_REF,
		SR_COL_SERIALS,
	};
}
