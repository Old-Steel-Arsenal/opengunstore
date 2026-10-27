'use strict';
// Pure kernel of the Pending Order sections: rows carry
// a server-stamped `stage`; the page groups in a fixed order, filters by
// source, searches, and sorts each section oldest-first. No DOM / Frappe.
const test = require('node:test');
const assert = require('node:assert');
const { PD_STAGE_ORDER, pdMatchesFilter, pdMatchesQuery, pdSections, pdLocationFields, pdCollectLocations, pdEscape } =
	require('../../ffl_core/page/pending_dispositions/pending_dispositions.js');

const rows = [
	{ source: 'sales_invoice', stage: 'ready_ship', age_days: 2, customer_name: 'Ann', sales_invoice: 'SI-1', pending_serials: [{ serial: 'SN-A' }] },
	{ source: 'sales_invoice', stage: 'blocked', age_days: 9, customer_name: 'Bob', sales_invoice: 'SI-2' },
	{ source: 'woo', site: 'retail', stage: 'waiting', age_days: 4, customer_name: 'Cy', woo_order_id: '1001' },
	{ source: 'woo', site: 'retail', stage: 'ready_pickup', age_days: 1, customer_name: 'Dot', woo_order_id: '2002' },
	{ source: 'gunbroker', stage: 'blocked', age_days: 3, customer_name: 'Eve', gb_order_id: '77' },
];

test('sections come out in the page order and skip empty ones', () => {
	const secs = pdSections(rows, 'all', '');
	assert.deepStrictEqual(secs.map((s) => s.stage), ['blocked', 'waiting', 'ready_ship', 'ready_pickup']);
	assert.deepStrictEqual(PD_STAGE_ORDER, ['blocked', 'to_place', 'waiting', 'ready_ship', 'ready_pickup', 'disposing']);
});

test('within a section the oldest row comes first', () => {
	const blocked = pdSections(rows, 'all', '').find((s) => s.stage === 'blocked');
	assert.deepStrictEqual(blocked.rows.map((r) => r.customer_name), ['Bob', 'Eve']);
});

test('a row without a stage lands in ready_ship', () => {
	const secs = pdSections([{ source: 'sales_invoice' }], 'all', '');
	assert.deepStrictEqual(secs.map((s) => s.stage), ['ready_ship']);
});

test('source chips: counter / web / gunbroker', () => {
	assert.deepStrictEqual(rows.filter((r) => pdMatchesFilter(r, 'counter')).map((r) => r.customer_name), ['Ann', 'Bob']);
	assert.deepStrictEqual(rows.filter((r) => pdMatchesFilter(r, 'woo')).map((r) => r.customer_name), ['Cy', 'Dot']);
	assert.deepStrictEqual(rows.filter((r) => pdMatchesFilter(r, 'gunbroker')).map((r) => r.customer_name), ['Eve']);
	assert.strictEqual(rows.filter((r) => pdMatchesFilter(r, 'all')).length, rows.length);
});

test('search matches customer, order reference and serial, case-insensitively', () => {
	assert.ok(pdMatchesQuery(rows[0], 'ann'));
	assert.ok(pdMatchesQuery(rows[0], 'si-1'));
	assert.ok(pdMatchesQuery(rows[0], 'sn-a'));
	assert.ok(pdMatchesQuery(rows[2], '1001'));
	assert.ok(pdMatchesQuery(rows[4], '77'));
	assert.ok(!pdMatchesQuery(rows[0], 'zzz'));
	assert.ok(pdMatchesQuery(rows[0], ''));
});

test('search finds a distributor order by its name or the web reference quoted on the phone', () => {
	const web = { source: 'woo', distributor_orders: [{ name: 'DO-2610-0002', web_ref: '3203100' }] };
	const counter = { source: 'sales_invoice', legs: { distributor_orders: [{ name: 'DO-2610-0001', web_ref: '3199001' }] } };
	assert.ok(pdMatchesQuery(web, '3203100'));
	assert.ok(pdMatchesQuery(web, 'do-2610-0002'));
	assert.ok(pdMatchesQuery(counter, '3199001'));
	assert.ok(!pdMatchesQuery(web, '3199001'));
});

test('filter and search combine', () => {
	const secs = pdSections(rows, 'counter', 'bob');
	assert.deepStrictEqual(secs.map((s) => [s.stage, s.rows.length]), [['blocked', 1]]);
	assert.deepStrictEqual(pdSections(rows, 'gunbroker', 'nobody'), []);
});

// Dispose asks where loose stock came from: one dropdown per line that is in a location.
const STOCK_LINES = [
	{ line: '1', item_code: 'AMMO', item_name: '9mm FMJ', qty: 2, locations: [{ location: 'TSA-Rack 1', label: 'Rack 1', qty: 4 }, { location: 'TSA-Rack 2', label: 'Rack 2', qty: 6 }], chosen: null },
	{ line: '2', item_code: 'OPTIC', item_name: 'Red dot', qty: 1, locations: [{ location: 'TSA-Safe 1', label: 'Safe 1', qty: 3 }], chosen: 'TSA-Safe 1' },
	{ line: '3', item_code: 'PART', item_name: 'Spring', qty: 5, locations: [], chosen: null },
];

test('a stock line that is in a location gets a dropdown; one in no location has nothing to pick', () => {
	const fields = pdLocationFields(STOCK_LINES);
	assert.deepStrictEqual(fields.map((f) => f.line), ['1', '2']);
	assert.strictEqual(fields[0].label, '2 × 9mm FMJ');
	assert.deepStrictEqual(fields[0].options.map((o) => o.label), ['', 'Rack 1 (4)', 'Rack 2 (6)']);
	assert.strictEqual(fields[0].default, '');
	assert.strictEqual(fields[1].default, 'TSA-Safe 1'); // the only place: picked for them
	assert.deepStrictEqual(pdLocationFields(undefined), []);
});

test('only the dropdowns that were filled in are sent, keyed by order line', () => {
	const fields = pdLocationFields(STOCK_LINES);
	assert.deepStrictEqual(pdCollectLocations(fields, { stg_loc_0: 'TSA-Rack 2', stg_loc_1: '' }), { 1: 'TSA-Rack 2' });
	assert.deepStrictEqual(pdCollectLocations(fields, {}), {});
	assert.deepStrictEqual(pdCollectLocations(undefined, undefined), {});
});

test('an item name with markup cannot inject into the dialog label or the dropdown', () => {
	const evil = [{ line: '1', item_code: 'X', item_name: '<img src=x onerror=alert(1)> & "Co"', qty: 2,
		locations: [{ location: 'L', label: 'Rack <b>1</b>', qty: 3 }] }];
	const f = pdLocationFields(evil)[0];
	assert.ok(!/[<>]/.test(f.label), f.label);
	assert.ok(f.label.includes('&lt;img') && f.label.includes('&amp;') && f.label.includes('&quot;Co&quot;'));
	assert.ok(!/[<>]/.test(f.options[1].label));
	assert.strictEqual(pdEscape(undefined), '');
	assert.strictEqual(pdEscape(5), '5');
});

// ---- Agreements on a counter card ---------------------------------------------------
const { pdAgreementKind, pdAgreementNames } = require('../../ffl_core/page/pending_dispositions/pending_dispositions.js');

test('an agreement reads as signed, sent, link expired, expired or not signed', () => {
	assert.strictEqual(pdAgreementKind({ state: 'signed', signed: true }), 'signed');
	assert.strictEqual(pdAgreementKind({ state: 'expiring', signed: true }), 'signed');
	assert.strictEqual(pdAgreementKind({ state: 'pending', link_open: true }), 'sent');
	assert.strictEqual(pdAgreementKind({ state: 'pending', link_open: false }), 'link_expired');
	assert.strictEqual(pdAgreementKind({ state: 'expired' }), 'expired');
	assert.strictEqual(pdAgreementKind({ state: 'missing' }), 'missing');
	assert.strictEqual(pdAgreementKind(null), 'missing');
});

test('unsigned agreements are named with their gun, escaped', () => {
	assert.strictEqual(pdAgreementNames([{ template: 'Used Item Agreement' }, { template: 'Bill <b>', serial_no: 'SN1' }]),
		'Used Item Agreement, Bill &lt;b&gt; (SN1)');
	assert.strictEqual(pdAgreementNames([]), '');
});
