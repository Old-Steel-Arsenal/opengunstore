'use strict';
// Pure kernel of the Sales Report UI redesign.
// No DOM / Frappe / localStorage — the browser wrappers add that. These pin:
//   - the curated default hidden-column set per view
//   - column-pruning that keeps after_datatable_render converging when a
//     curated default names a column absent from the live view (the default
//     seed + the render-loop invariant the column picker already fixed)
//   - the Serial / Order Ref cell renderers
//   - the quick-range preset date math
const test = require('node:test');
const assert = require('node:assert');
const {
	SR_LOCK_FIELD,
	SR_COL_ORDER_REF,
	SR_COL_SERIALS,
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
} = require('../../ffl_core/report/sales_report/sales_report.js');

// ---------------------- Order Detail column sort disabled ----------------------
// The reshape (child-row removal + datatable rebuild) leaves the datatable's
// internal cell model misaligned with the columns, so native column sort throws
// (frappe-datatable _sortRows reads undefined.content). The master grid is a
// drill-down list (default date order; sort → the Order view), so column sort is
// turned OFF for Order Detail only — killing the crash AND the false ↑↓ affordance.

test('datatable sortable: OFF for Order Detail, ON for the other views (M-02)', () => {
	assert.strictEqual(sr_datatable_sortable('Order Detail'), false);
	assert.strictEqual(sr_datatable_sortable('Order'), true);
	assert.strictEqual(sr_datatable_sortable('Product'), true);
	assert.strictEqual(sr_datatable_sortable(undefined), true); // default view = Order → sortable
});

// ---------------------- keyboard activation of the ▸ Lines link ----------------------
// frappe-datatable swallows the anchor's native Enter→click, so a keydown handler
// activates the Dialog explicitly. sr_is_activation_key decides which keys count.

test('activation key: Enter and Space activate; other keys do not (M-01 a11y)', () => {
	assert.ok(sr_is_activation_key('Enter'));
	assert.ok(sr_is_activation_key(' '));         // modern browsers report Space as " "
	assert.ok(sr_is_activation_key('Spacebar'));  // legacy Space key value
	assert.ok(!sr_is_activation_key('a'));
	assert.ok(!sr_is_activation_key('Tab'));
	assert.ok(!sr_is_activation_key('ArrowDown'));
	assert.ok(!sr_is_activation_key(''));
	assert.ok(!sr_is_activation_key(undefined));
});

// =============================================================================
// Order Detail redesign (master grid + Dialog), FE client aggregation.
// Pure aggregation of the BE's flat "order + child" rows into per-invoice order
// view-models. No BE change: everything the master grid / Dialog need
// beyond the order row is derived here from the child rows.
// =============================================================================

// Realistic BE-shaped flat Order Detail rows for one order (INV1): the order
// (parent) row + two product child rows + a shipping child row.
function od_rows_one_order() {
	return [
		{ row_kind: 'order', invoice: 'INV1', posting_date: '2026-07-10', customer: 'Mike R.',
		  channel: 'Web', order_ref: '2088', category: 'Mixed', qty: 2, net_sales: 2236.50,
		  gross_merch: 2236.50, trade_in_net: 0, total: 2281.50, cost: 1724.87, profit: 511.63,
		  sales_tax: 0, shipping: 45, missing_cost: false, tax_mismatch: false },
		{ row_kind: 'product', invoice: 'INV1', item_code: 'CZ-SHADOW2', item_name: 'CZ Shadow 2',
		  serial_no: 'B00323', category: 'Handguns', qty: 1, unit_price: 1724.87, net_amount: 1724.87,
		  cost: 1724.87, profit: 0, is_consignment: false, is_trade_in: false, missing_cost: false },
		{ row_kind: 'product', invoice: 'INV1', item_code: 'RMR-TYPE2', item_name: 'RMR Type 2',
		  serial_no: '', category: 'Optics', qty: 1, unit_price: 511.63, net_amount: 511.63,
		  cost: 0, profit: 511.63, is_consignment: false, is_trade_in: false, missing_cost: false },
		{ row_kind: 'shipping', invoice: 'INV1', shipping: 45 },
		{ row_kind: 'total', is_total: true, net_sales: 2236.50 },
	];
}

test('aggregate: groups an order + its child lines under the invoice', () => {
	const agg = sr_aggregate_orders(od_rows_one_order());
	assert.ok(agg.INV1, 'INV1 aggregated');
	assert.strictEqual(agg.INV1.order.invoice, 'INV1');
	assert.strictEqual(agg.INV1.lines.length, 2);
	assert.strictEqual(agg.INV1.lineCount, 2);
	assert.deepStrictEqual(agg.INV1.lines.map((l) => l.item_code), ['CZ-SHADOW2', 'RMR-TYPE2']);
});

test('aggregate: charges pull shipping/sales_tax from the order row', () => {
	const agg = sr_aggregate_orders(od_rows_one_order());
	assert.strictEqual(agg.INV1.charges.shipping, 45);
	assert.strictEqual(agg.INV1.charges.salesTax, 0);
	assert.strictEqual(agg.INV1.charges.excise, 0);
	assert.deepStrictEqual(agg.INV1.charges.unclassified, []);
});

test('aggregate: derives categoryDetail (distinct sorted line groups) + isConsignment', () => {
	const agg = sr_aggregate_orders(od_rows_one_order());
	assert.strictEqual(agg.INV1.categoryDetail, 'Handguns, Optics');
	assert.strictEqual(agg.INV1.isConsignment, false);
});

test('aggregate: flags = line missing_cost count + order tax_mismatch + unclassified count', () => {
	const agg = sr_aggregate_orders(od_rows_one_order());
	assert.deepStrictEqual(agg.INV1.flags, { missingCost: 0, taxMismatch: false, unclassified: 0 });
});

test('aggregate: excludes the Total row; multiple invoices are independent', () => {
	const rows = [
		{ row_kind: 'order', invoice: 'A', category: 'Ammo', net_sales: 100, gross_merch: 100, profit: 30, cost: 70, shipping: 0, sales_tax: 8, tax_mismatch: false },
		{ row_kind: 'product', invoice: 'A', item_code: 'X', category: 'Ammo', missing_cost: true, is_consignment: false },
		{ row_kind: 'order', invoice: 'B', category: 'Handguns', net_sales: 500, gross_merch: 500, profit: 60, cost: 440, shipping: 0, sales_tax: 0, tax_mismatch: true },
		{ row_kind: 'product', invoice: 'B', item_code: 'G', category: 'Handguns', is_consignment: true, missing_cost: false },
		{ row_kind: 'unclassified', invoice: 'B', category: 'Unclassified: 5400 - Misc', unclassified: 12.5 },
		{ row_kind: 'total', is_total: true },
	];
	const agg = sr_aggregate_orders(rows);
	assert.strictEqual(Object.keys(agg).length, 2); // A + B, no Total
	assert.deepStrictEqual(agg.A.flags, { missingCost: 1, taxMismatch: false, unclassified: 0 });
	assert.strictEqual(agg.B.isConsignment, true);
	assert.deepStrictEqual(agg.B.flags, { missingCost: 0, taxMismatch: true, unclassified: 1 });
	assert.strictEqual(agg.B.charges.unclassified[0].amount, 12.5);
	assert.strictEqual(agg.B.charges.unclassified[0].label, 'Unclassified: 5400 - Misc');
});

test('order margin: profit / gross_merch * 100 (matches Order-view formula)', () => {
	assert.ok(Math.abs(sr_order_margin({ profit: 511.63, gross_merch: 2236.50 }) - 22.8763) < 0.01);
});

test('order margin: gross_merch 0 → 0 (no divide-by-zero)', () => {
	assert.strictEqual(sr_order_margin({ profit: 100, gross_merch: 0 }), 0);
	assert.strictEqual(sr_order_margin({ profit: 0, gross_merch: 0 }), 0);
});

// -------- Dialog footer flag summary --------

test('order footer: no flags → clean checkmark line', () => {
	assert.deepStrictEqual(sr_order_footer({ missingCost: 0, taxMismatch: false, unclassified: 0 }),
		{ clean: true, text: '✓ No data flags on this order' });
});

test('order footer: understated cost → singular/plural line', () => {
	assert.deepStrictEqual(sr_order_footer({ missingCost: 1, taxMismatch: false, unclassified: 0 }),
		{ clean: false, text: '⚠ 1 line with understated cost' });
	assert.strictEqual(sr_order_footer({ missingCost: 3, taxMismatch: false, unclassified: 0 }).text,
		'⚠ 3 lines with understated cost');
});

test('order footer: combines multiple flag types with " · "', () => {
	assert.strictEqual(
		sr_order_footer({ missingCost: 1, taxMismatch: true, unclassified: 2 }).text,
		'⚠ 1 line with understated cost · tax mismatch · 2 unclassified charges');
});

test('order footer: tax mismatch only', () => {
	assert.deepStrictEqual(sr_order_footer({ missingCost: 0, taxMismatch: true, unclassified: 0 }),
		{ clean: false, text: '⚠ tax mismatch' });
});

// -------- lines_cell drill-down entry HTML --------

test('lines cell: "▸ N lines" anchor with data-invoice, plural aware', () => {
	assert.strictEqual(sr_lines_cell_html('INV1', 2, false),
		'<a class="sr-lines" data-invoice="INV1" href="#">▸ 2 lines</a>');
	assert.strictEqual(sr_lines_cell_html('INV1', 1, false),
		'<a class="sr-lines" data-invoice="INV1" href="#">▸ 1 line</a>');
});

test('lines cell: order-level flag appends ⚠', () => {
	assert.strictEqual(sr_lines_cell_html('INV1', 2, true),
		'<a class="sr-lines" data-invoice="INV1" href="#">▸ 2 lines ⚠</a>');
});

test('lines cell: escapes the data-invoice attribute (defensive)', () => {
	assert.strictEqual(sr_lines_cell_html('A"><b', 1, false),
		'<a class="sr-lines" data-invoice="A&quot;&gt;&lt;b" href="#">▸ 1 line</a>');
});

// ------------------------------------------------ frozen key contract

test('Order-view identity columns track the frozen keys (order_ref / serials)', () => {
	// These are the fieldnames the formatter decorates; they MUST equal the BE's
	// row/column keys or the Order Ref / Serial cells silently render nothing.
	// The BE's v1 snapshot (po_no / serial_no) is void — do not re-align to it.
	assert.strictEqual(SR_COL_ORDER_REF, 'order_ref');
	assert.strictEqual(SR_COL_SERIALS, 'serials');
});

// ------------------------------------------------ curated default hidden

test('default hidden: Order view hides Total + the channel-fee pair', () => {
	// Channel Fees / Profit after Fees are default-hidden and always
	// CSV-exported. Total is hidden because it reads as Net Sales otherwise.
	assert.deepStrictEqual(sr_default_hidden('Order'),
		['total', 'channel_fees', 'profit_after_fees', 'trade_in_applied', 'trade_in_payout']);
});

test('default hidden: Order Detail hides all non-9 columns', () => {
	// The master grid replaces the old Order Detail default: it shows
	// only the 9 order-level columns (Date/Invoice/Lines/Customer/Category/Net
	// Sales/Cost/Profit/Margin), so everything else in the BE detail set is hidden
	// — the per-line columns + Channel/Sales Tax/Shipping/Qty/Total/Order Ref.
	assert.deepStrictEqual(sr_default_hidden('Order Detail'), [
		'channel', 'item_code', 'serial_no', 'qty', 'unit_price', 'gross_merch',
		'trade_in_net', 'net_amount', 'line_tax', 'shipping', 'unclassified',
		'sales_tax', 'excise_tax', 'tax_mismatch', 'total', 'order_ref',
		// The fee pair (default-hidden) and Ship-to State, which
		// is not one of the 9 order-grid columns either.
		'channel_fees', 'profit_after_fees', 'ship_to_state',
		// negative-ticket settlement: zero on nearly every row, so default-hidden
		'trade_in_applied', 'trade_in_payout',
	]);
});

test('default hidden: Product hides the 6 low-frequency support/flag columns', () => {
	assert.deepStrictEqual(sr_default_hidden('Product'),
		['list_price', 'discount', 'gross_merch', 'trade_in_net', 'is_trade_in', 'exempt_from_sales_tax']);
});

test('default hidden: unknown / missing view → empty (defensive, no phantom hides)', () => {
	assert.deepStrictEqual(sr_default_hidden('Nope'), []);
	assert.deepStrictEqual(sr_default_hidden(undefined), []);
});

test('default hidden never names the lock column (would loop after_datatable_render)', () => {
	for (const v of ['Order', 'Order Detail', 'Product']) {
		assert.ok(!sr_default_hidden(v).includes(SR_LOCK_FIELD),
			`${v} default must not name ${SR_LOCK_FIELD}`);
	}
});

// ---------------------------------------------- prune to live columns

test('prune keeps only fieldnames present in the live columns', () => {
	const cols = [{ fieldname: 'total' }, { fieldname: 'cost' }, { fieldname: 'profit' }];
	const pruned = sr_prune_to_columns(new Set(['total', 'phantom', 'cost']), cols);
	assert.ok(pruned.has('total') && pruned.has('cost'));
	assert.ok(!pruned.has('phantom')); // a curated default naming a column absent here is dropped
	assert.strictEqual(pruned.size, 2);
});

test('prune of a set with no live-column overlap → empty (convergence safe)', () => {
	const cols = [{ fieldname: 'a' }, { fieldname: 'b' }];
	assert.strictEqual(sr_prune_to_columns(new Set(['x', 'y']), cols).size, 0);
});

test('prune tolerates empty columns / empty set', () => {
	assert.strictEqual(sr_prune_to_columns(new Set(['total']), []).size, 0);
	assert.strictEqual(sr_prune_to_columns(new Set(), [{ fieldname: 'total' }]).size, 0);
});

// -------------------------------------------------------- serial parse

test('parse serials splits on ";" and trims, dropping blanks', () => {
	assert.deepStrictEqual(sr_parse_serials('SNA; SNB'), ['SNA', 'SNB']);
	assert.deepStrictEqual(sr_parse_serials('A;B ; C'), ['A', 'B', 'C']);
	assert.deepStrictEqual(sr_parse_serials('A;; B'), ['A', 'B']);
});

test('parse serials tolerates empty / null / single', () => {
	assert.deepStrictEqual(sr_parse_serials(''), []);
	assert.deepStrictEqual(sr_parse_serials(null), []);
	assert.deepStrictEqual(sr_parse_serials('F7550'), ['F7550']);
});

// ------------------------------------------------------ serial summary

test('serial summary: empty → em dash, not truncated', () => {
	assert.deepStrictEqual(sr_serial_summary(''), { text: '—', title: '', truncated: false });
});

test('serial summary: single serial shows in full, no title', () => {
	assert.deepStrictEqual(sr_serial_summary('F7550'), { text: 'F7550', title: '', truncated: false });
});

test('serial summary: multiple → "first +N more" + comma-joined title', () => {
	assert.deepStrictEqual(sr_serial_summary('SNA; SNB'),
		{ text: 'SNA +1 more', title: 'SNA, SNB', truncated: true });
	assert.deepStrictEqual(sr_serial_summary('A; B; C'),
		{ text: 'A +2 more', title: 'A, B, C', truncated: true });
});

// ----------------------------------------------------- order ref render

test('order ref: blank (POS sale) → em dash, muted', () => {
	assert.deepStrictEqual(sr_order_ref_display(''), { text: '—', muted: true });
	assert.deepStrictEqual(sr_order_ref_display(null), { text: '—', muted: true });
});

test('order ref: a Woo number gets a single "#" prefix', () => {
	assert.deepStrictEqual(sr_order_ref_display('2134'), { text: '#2134', muted: false });
	assert.deepStrictEqual(sr_order_ref_display('#2134'), { text: '#2134', muted: false }); // no double #
});

// ---------------------------------------------------- quick-range preset

test('preset Custom / blank → null (no auto-fill, user drives from/to)', () => {
	assert.strictEqual(sr_preset_range('Custom', '2026-07-15'), null);
	assert.strictEqual(sr_preset_range('', '2026-07-15'), null);
	assert.strictEqual(sr_preset_range(null, '2026-07-15'), null);
});

test('preset Today → from == to == today', () => {
	assert.deepStrictEqual(sr_preset_range('Today', '2026-07-15'),
		{ from: '2026-07-15', to: '2026-07-15' });
});

test('preset This Week → Monday of the week .. today (2026-07-15 is Wed → Mon 07-13)', () => {
	assert.deepStrictEqual(sr_preset_range('This Week', '2026-07-15'),
		{ from: '2026-07-13', to: '2026-07-15' });
});

test('preset This Month → 1st of month .. today', () => {
	assert.deepStrictEqual(sr_preset_range('This Month', '2026-07-15'),
		{ from: '2026-07-01', to: '2026-07-15' });
});

test('preset Last Month → full previous month (June: 06-01 .. 06-30)', () => {
	assert.deepStrictEqual(sr_preset_range('Last Month', '2026-07-15'),
		{ from: '2026-06-01', to: '2026-06-30' });
});

test('preset This Quarter → 1st of quarter .. today (Jul → Q3 starts 07-01)', () => {
	assert.deepStrictEqual(sr_preset_range('This Quarter', '2026-07-15'),
		{ from: '2026-07-01', to: '2026-07-15' });
});

test('preset Year to Date → Jan 1 .. today', () => {
	assert.deepStrictEqual(sr_preset_range('Year to Date', '2026-07-15'),
		{ from: '2026-01-01', to: '2026-07-15' });
});

test('preset Last Month across a year boundary (Jan → prev Dec)', () => {
	assert.deepStrictEqual(sr_preset_range('Last Month', '2026-01-10'),
		{ from: '2025-12-01', to: '2025-12-31' });
});

// ---------------------------------------- flag-card counts + tooltip
// These MUST mirror the BE _report_summary scan exactly, so the split tooltip
// always sums to the flag card's own number (no divergence).

test('flag counts: Order view = non-total rows with missing_cost', () => {
	const rows = [
		{ missing_cost: true, invoice: 'A' },
		{ missing_cost: false, invoice: 'B' },
		{ missing_cost: true, invoice: 'C' },
		{ is_total: true, missing_cost: true }, // Total row excluded
	];
	assert.deepStrictEqual(sr_flag_counts('Order', rows),
		{ missing: 2, mismatch: 0, unclassified: 0, total: 2 });
});

test('flag counts: Product = product-missing + DISTINCT mismatch invoices + unclassified', () => {
	const rows = [
		{ row_kind: 'product', missing_cost: true, invoice: 'INV1', tax_mismatch: true },
		{ row_kind: 'product', missing_cost: false, invoice: 'INV1', tax_mismatch: true }, // same invoice → mismatch counts ONCE
		{ row_kind: 'product', missing_cost: true, invoice: 'INV2' },
		{ row_kind: 'unclassified', invoice: 'INV2' },
		{ row_kind: 'shipping', invoice: 'INV2' }, // not a flag
		{ is_total: true },
	];
	assert.deepStrictEqual(sr_flag_counts('Product', rows),
		{ missing: 2, mismatch: 1, unclassified: 1, total: 4 });
});

test('flag counts: product view ignores missing_cost on non-product rows (BE row_kind gate)', () => {
	const rows = [
		{ row_kind: 'order', missing_cost: true, invoice: 'INV1' }, // parent — BE counts only row_kind==product
		{ row_kind: 'product', missing_cost: true, invoice: 'INV1' },
	];
	assert.strictEqual(sr_flag_counts('Product', rows).missing, 1);
});

test('flag counts: empty / all-total → zeros', () => {
	assert.deepStrictEqual(sr_flag_counts('Order', []),
		{ missing: 0, mismatch: 0, unclassified: 0, total: 0 });
	assert.deepStrictEqual(sr_flag_counts('Product', [{ is_total: true }]),
		{ missing: 0, mismatch: 0, unclassified: 0, total: 0 });
});

test('flag tooltip: Product/Detail = "N cost · N tax · N unclassified" split', () => {
	assert.strictEqual(
		sr_flag_tooltip('Product', { missing: 2, mismatch: 0, unclassified: 0, total: 2 }),
		'2 cost · 0 tax · 0 unclassified');
	assert.strictEqual(
		sr_flag_tooltip('Order Detail', { missing: 1, mismatch: 1, unclassified: 0, total: 2 }),
		'1 cost · 1 tax · 0 unclassified');
});

test('flag tooltip: Order view = understated-cost count', () => {
	assert.strictEqual(
		sr_flag_tooltip('Order', { missing: 3, mismatch: 0, unclassified: 0, total: 3 }),
		'3 cost understated');
});

// -------- graceful degradation of the DOM attach (lead G3b hard condition) --------
// The tooltip attach must NEVER throw: if a future Frappe summary-DOM change makes
// the card selector miss, or the DOM layer errors, it silently no-ops (the card's
// own aggregate number, rendered server-side, is untouched). Stub the browser
// globals to drive the two failure modes.

test('flag-card attach: selector miss (no card) → silent no-op, never throws', () => {
	const o$ = global.$, oF = global.frappe;
	try {
		let attrCalled = false;
		// $(".report-summary").children().last() → an EMPTY jQuery set (length 0).
		global.$ = () => ({ children: () => ({ last: () => ({ length: 0, attr: () => { attrCalled = true; } }) }) });
		global.frappe = { query_report: { data: [{ missing_cost: true }], get_filter_value: () => 'Product' } };
		assert.doesNotThrow(() => sr_set_flag_card_tooltip());
		assert.strictEqual(attrCalled, false); // card not found ⇒ attr never touched
	} finally {
		global.$ = o$;
		global.frappe = oF;
	}
});

test('flag-card attach: a DOM-layer error is swallowed, never throws', () => {
	const o$ = global.$, oF = global.frappe;
	try {
		// Simulate a deeper future breakage: the selector call itself throws.
		global.$ = () => { throw new Error('summary DOM changed'); };
		global.frappe = { query_report: { data: [], get_filter_value: () => 'Order' } };
		assert.doesNotThrow(() => sr_set_flag_card_tooltip());
	} finally {
		global.$ = o$;
		global.frappe = oF;
	}
});

test('flag-card attach: happy path sets the split title on the found card', () => {
	const o$ = global.$, oF = global.frappe;
	try {
		let title = null;
		global.$ = () => ({ children: () => ({ last: () => ({ length: 1, attr: (k, v) => { if (k === 'title') title = v; } }) }) });
		global.frappe = {
			query_report: {
				data: [
					{ row_kind: 'product', missing_cost: true, invoice: 'INV1' },
					{ row_kind: 'unclassified', invoice: 'INV1' },
					{ is_total: true },
				],
				get_filter_value: () => 'Product',
			},
		};
		assert.doesNotThrow(() => sr_set_flag_card_tooltip());
		assert.strictEqual(title, '1 cost · 0 tax · 1 unclassified');
	} finally {
		global.$ = o$;
		global.frappe = oF;
	}
});

// ---------------------- flag counts from aggregates (children removed from data) ----------------------
// After the reshape the datatable's data has only order rows, so the Data Flags tooltip
// must derive its split from the cached aggregates, not from re-scanning data.

test('flag counts from aggregates: sums per-invoice flags', () => {
	const aggregates = {
		A: { flags: { missingCost: 1, taxMismatch: false, unclassified: 0 } },
		B: { flags: { missingCost: 2, taxMismatch: true, unclassified: 1 } },
		C: { flags: { missingCost: 0, taxMismatch: true, unclassified: 0 } },
	};
	assert.deepStrictEqual(sr_flag_counts_from_aggregates(aggregates),
		{ missing: 3, mismatch: 2, unclassified: 1, total: 6 });
});

test('flag counts from aggregates: empty / null → zeros', () => {
	assert.deepStrictEqual(sr_flag_counts_from_aggregates({}),
		{ missing: 0, mismatch: 0, unclassified: 0, total: 0 });
	assert.deepStrictEqual(sr_flag_counts_from_aggregates(null),
		{ missing: 0, mismatch: 0, unclassified: 0, total: 0 });
});

test('flag counts: aggregates path == data-scan path (Order Detail parity, no divergence)', () => {
	const rows = [
		{ row_kind: 'product', missing_cost: true, invoice: 'INV1', tax_mismatch: true },
		{ row_kind: 'product', missing_cost: false, invoice: 'INV1', tax_mismatch: true },
		{ row_kind: 'product', missing_cost: true, invoice: 'INV2' },
		{ row_kind: 'unclassified', invoice: 'INV2', category: 'Unclassified: X', unclassified: 5 },
		{ row_kind: 'shipping', invoice: 'INV2' },
		{ is_total: true },
	];
	const fromData = sr_flag_counts('Product', rows);
	const fromAgg = sr_flag_counts_from_aggregates(sr_aggregate_orders(rows));
	assert.deepStrictEqual(fromAgg, fromData);
});

// ---------------------- n1: order_ref strips ALL leading # ----------------------

test('order ref: strips ALL leading # then re-prefixes once (n1)', () => {
	assert.deepStrictEqual(sr_order_ref_display('##2134'), { text: '#2134', muted: false });
	assert.deepStrictEqual(sr_order_ref_display('###2134'), { text: '#2134', muted: false });
});

// ---------------------- prepare_columns throw is swallowed ----------------------

test('order-detail prepare: a throwing prepare_columns is swallowed, never throws (m1)', () => {
	const oF = global.frappe, o__ = global.__;
	try {
		global.__ = (s) => s;
		global.frappe = {
			query_report: {
				raw_data: { columns: [{ fieldname: 'invoice' }, { fieldname: 'customer' }, { fieldname: 'profit' }] },
				data: [
					{ row_kind: 'order', invoice: 'A', gross_merch: 100, profit: 30 },
					{ row_kind: 'product', invoice: 'A', missing_cost: false },
					{ is_total: true, net_amount: 100, gross_merch: 100, profit: 30 },
				],
				get_filter_value: () => 'Order Detail',
				prepare_columns: () => { throw new Error('prepare_columns signature changed'); },
			},
		};
		assert.doesNotThrow(() => sr_order_detail_prepare());
		// Children removed from the datatable data (order + total kept).
		assert.ok(global.frappe.query_report.data.every((r) => r.is_total || r.row_kind === 'order'));
		// The Total row's net_sales is backfilled from gross_merch
		// (net_amount is only the legacy fallback; here the fixture has both = 100).
		const total = global.frappe.query_report.data.find((r) => r.is_total);
		assert.strictEqual(total.net_sales, 100);
	} finally {
		global.frappe = oF;
		global.__ = o__;
	}
});

// ---------------------- trade-in: Total net_sales must be GROSS ----------------------
// The BE Totals row's net_amount stays net of
// the allowance (tax worksheet), while the visible Net Sales column is gross. A fixture
// where the two DIVERGE pins that the backfill never reads the netted figure --
// the old `net_sales = net_amount` backfill would render Total 500 under parent
// rows and a summary card both showing 1300.

test('order-detail prepare: Total net_sales is gross of trade-in, never net_amount', () => {
	const oF = global.frappe, o__ = global.__;
	try {
		global.__ = (s) => s;
		global.frappe = {
			query_report: {
				raw_data: { columns: [{ fieldname: 'invoice' }, { fieldname: 'customer' }, { fieldname: 'profit' }] },
				data: [
					{ row_kind: 'order', invoice: 'TI', net_sales: 1300, gross_merch: 1300, trade_in_net: -800, is_trade_in: true, profit: 100, cost: 1200 },
					{ row_kind: 'product', invoice: 'TI', is_trade_in: true, net_amount: -800 },
					// BE net_sales deliberately ABSENT: exercises the backfill path.
					{ is_total: true, net_amount: 500, gross_merch: 1300, profit: 100 },
				],
				get_filter_value: () => 'Order Detail',
				prepare_columns: (c) => c,
			},
		};
		sr_order_detail_prepare();
		const total = global.frappe.query_report.data.find((r) => r.is_total);
		assert.strictEqual(total.net_sales, 1300); // == Σ parent rows == the Net Sales card
	} finally {
		global.frappe = oF;
		global.__ = o__;
	}
});

test('order-detail prepare: a BE-provided Total net_sales is kept as-is', () => {
	const oF = global.frappe, o__ = global.__;
	try {
		global.__ = (s) => s;
		global.frappe = {
			query_report: {
				raw_data: { columns: [{ fieldname: 'invoice' }, { fieldname: 'customer' }, { fieldname: 'profit' }] },
				data: [
					{ row_kind: 'order', invoice: 'TI', net_sales: 1300, gross_merch: 1300, trade_in_net: -800, is_trade_in: true, profit: 100, cost: 1200 },
					{ is_total: true, net_sales: 1300, net_amount: 500, gross_merch: 1300, profit: 100 },
				],
				get_filter_value: () => 'Order Detail',
				prepare_columns: (c) => c,
			},
		};
		sr_order_detail_prepare();
		const total = global.frappe.query_report.data.find((r) => r.is_total);
		assert.strictEqual(total.net_sales, 1300);
	} finally {
		global.frappe = oF;
		global.__ = o__;
	}
});

test('grid height is sized from where the grid body actually starts', () => {
	const { sr_grid_height_css } = require('../../ffl_core/report/sales_report/sales_report.js');
	// Order view at 1440x900: body starts at 380, one-line footer 34 + 22 gap.
	assert.strictEqual(sr_grid_height_css(380, 34), 'max(240px, calc(100vh - 436px))');
	// Product view: cards wrap (body at 462) and the footer is two lines (73px).
	assert.strictEqual(sr_grid_height_css(461.6, 73), 'max(240px, calc(100vh - 557px))');
	assert.strictEqual(sr_grid_height_css(380), 'max(240px, calc(100vh - 436px))', 'no footer measured → one-line default');
	assert.strictEqual(sr_grid_height_css(-40, 0), 'max(240px, calc(100vh - 56px))', 'never a negative offset');
});

test('the grid cap is a route-scoped max-height rule, never an inline height', () => {
	const { sr_grid_rule_css } = require('../../ffl_core/report/sales_report/sales_report.js');
	const css = sr_grid_rule_css();
	// frappe-datatable strips inline `height` on every sort / column resize and
	// Frappe's narrow-screen rule is `height … !important` — only max-height wins both.
	assert.match(css, /max-height: var\(--sr-grid-max, none\) !important/);
	assert.doesNotMatch(css, /[^-]height:/);
	assert.ok(css.startsWith('body[data-route="query-report/Sales Report"] '), 'other query reports share the page');
});
