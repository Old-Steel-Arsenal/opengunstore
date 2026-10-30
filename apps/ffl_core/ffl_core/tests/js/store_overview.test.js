'use strict';
// Pure kernel of the Overview page: the 4473 tabs, the pickup amber, the header sentence, the sales
// figures, arrivals and the records filter.
const test = require('node:test');
const assert = require('node:assert');
const {
	sovMoney, sovEyebrow, sovWeekday, sovStarted, sovAgo, sovInitials, sovNicsState, sovNicsPill, sovTabs, sovTabRows,
	sovRowAction, sovGunLabel, sovDaysClass, sovSales, sovBars, sovArrival, sovSummary,
	sovFilterRecords, sovRoute, sovRecordHref, sovNeedRecords,
} = require('../../ffl_core/page/store_overview/store_overview.js');

// Report order (oldest first); started = when the order was opened. New orders carry 'N/A' (pos_4473).
const ROWS = [
	{ invoice: 'A', nics_status: 'Proceed', started: '2026-10-01 09:00:00' },
	{ invoice: 'B', nics_status: 'Delayed', started: '2026-10-02 09:00:00' },
	{ invoice: 'C', nics_status: 'N/A', started: '2026-10-03 09:00:00' },
	{ invoice: 'D', nics_status: 'Denied', started: '2026-10-04 09:00:00' },
	{ invoice: 'E', nics_status: 'N/A', started: '2026-10-05 09:00:00' },
	{ invoice: 'F', nics_status: 'Proceed', started: '2026-10-06 09:00:00' },
	{ invoice: 'G', nics_status: '', started: '2026-10-07 09:00:00' },
	{ invoice: 'H', nics_status: 'N/A', started: '2026-10-08 08:00:00' },
	{ invoice: 'I', nics_status: 'N/A', started: '2026-10-08 10:00:00' },
	{ invoice: 'J', nics_status: 'N/A', started: '2026-10-08 09:00:00' },
];

test('the tabs count each NICS state; N/A is no result yet; Denied and Cancelled only under All', () => {
	assert.deepStrictEqual(sovTabs(ROWS), [
		{ key: 'All', n: 10 }, { key: 'Ready', n: 2 }, { key: 'Delayed', n: 1 }, { key: 'In progress', n: 6 },
	]);
	assert.strictEqual(sovNicsState('Proceed'), 'Ready');
	assert.strictEqual(sovNicsState('Delayed'), 'Delayed');
	assert.strictEqual(sovNicsState('N/A'), 'In progress');
	assert.strictEqual(sovNicsState(''), 'In progress');
	assert.strictEqual(sovNicsState(null), 'In progress');
	assert.strictEqual(sovNicsState('Cancelled'), '');
	assert.deepStrictEqual(sovTabs([]), [{ key: 'All', n: 0 }, { key: 'Ready', n: 0 }, { key: 'Delayed', n: 0 }, { key: 'In progress', n: 0 }]);
});

test('without a final result (Denied / Cancelled) the three tabs add up to All', () => {
	const open = ROWS.filter((r) => !['Denied', 'Cancelled'].includes(r.nics_status));
	const [all, ...tabs] = sovTabs(open);
	assert.strictEqual(tabs.reduce((n, t) => n + t.n, 0), all.n);
	assert.strictEqual(all.n, 9);
});

test('a tab shows its own rows, newest order first, five at most', () => {
	assert.deepStrictEqual(sovTabRows(ROWS, 'All').map((r) => r.invoice), ['I', 'J', 'H', 'G', 'F']);
	assert.deepStrictEqual(sovTabRows(ROWS, 'Ready').map((r) => r.invoice), ['F', 'A']);
	assert.deepStrictEqual(sovTabRows(ROWS, 'Delayed').map((r) => r.invoice), ['B']);
	assert.deepStrictEqual(sovTabRows(ROWS, 'In progress').map((r) => r.invoice), ['I', 'J', 'H', 'G', 'E']);
	assert.deepStrictEqual(sovTabRows([], 'Ready'), []);
	assert.deepStrictEqual(ROWS.map((r) => r.invoice).join(''), 'ABCDEFGHIJ');      // the input is not reordered
});

test('the pill and the row button follow the NICS result', () => {
	assert.deepStrictEqual(sovNicsPill('Proceed'), { cls: 'ok', label: 'Proceed' });
	assert.deepStrictEqual(sovNicsPill('Delayed'), { cls: 'warn', label: 'Delayed' });
	assert.deepStrictEqual(sovNicsPill('Denied'), { cls: 'bad', label: 'Denied' });
	assert.deepStrictEqual(sovNicsPill('N/A'), { cls: 'mute', label: 'No NICS result' });
	assert.deepStrictEqual(sovNicsPill(''), { cls: 'mute', label: 'No NICS result' });
	assert.strictEqual(sovRowAction('Proceed'), 'Complete');
	assert.strictEqual(sovRowAction('N/A'), 'Continue');
	assert.strictEqual(sovRowAction(''), 'Continue');
	assert.strictEqual(sovRowAction('Delayed'), 'View');
});

test('one gun is named, more are counted, every serial is listed', () => {
	assert.deepStrictEqual(sovGunLabel([{ serial: 'BX1', item_name: 'Glock 19' }]), { name: 'Glock 19', serials: 'BX1' });
	assert.deepStrictEqual(sovGunLabel([{ serial: 'BX1', item_name: 'Glock 19' }, { serial: 'Z2', item_name: 'P365' }]),
		{ name: 'Glock 19 +1', serials: 'BX1, Z2' });
	assert.deepStrictEqual(sovGunLabel([{ serial: 'NO-ITEM', item_name: '' }]), { name: 'NO-ITEM', serials: 'NO-ITEM' });
	assert.deepStrictEqual(sovGunLabel([]), { name: '', serials: '' });
});

test('a pickup turns amber only past 21 days', () => {
	assert.strictEqual(sovDaysClass(22), 'old');
	assert.strictEqual(sovDaysClass(21), '');
	assert.strictEqual(sovDaysClass(0), '');
	assert.strictEqual(sovDaysClass('26'), 'old');
});

test('the header sentence names only the blocks this user has, joined in plain English', () => {
	const full = {
		open_4473: { rows: [{}, {}, {}] }, pickups: { total: 5 }, arriving: { total: 1 },
		ad: { off: false, retrying: 1, needs_person: 1 },
	};
	assert.strictEqual(sovSummary(full),
		'3 forms open, 5 transfers waiting on pickup, 1 distributor order arriving, and 2 A&D records not in the bound book yet.');
	assert.strictEqual(sovSummary({ ...full, ad: { off: false, retrying: 0, needs_person: 0 } }),
		'3 forms open, 5 transfers waiting on pickup, and 1 distributor order arriving.');
	assert.strictEqual(sovSummary({ open_4473: { rows: [{}] }, pickups: null, arriving: null, ad: null }), '1 form open.');
	assert.strictEqual(sovSummary({ open_4473: { rows: [] }, pickups: { total: 1 }, arriving: { total: 2 } }),
		'1 transfer waiting on pickup and 2 distributor orders arriving.');                     // nothing to say about 0
	assert.strictEqual(sovSummary({ open_4473: { rows: [] }, pickups: { total: 0 } }), 'Nothing is waiting on anyone right now.');
	assert.strictEqual(sovSummary({ open_4473: { error: 'x' }, pickups: { error: 'x' }, ad: { off: true } }), '');
	assert.strictEqual(sovSummary({}), '');
});

test('sales today is Net Sales; count and average leave returns out; last week is its whole day', () => {
	// ticket_net = Net Sales of the sales only (the server drops returns from it and from transactions)
	const days = [100, 0, 50, 80, 0, 120, 60, 130].map((n, i) => ({ date: `2026-10-0${i + 1}`, net_sales: n, transactions: n ? 2 : 0, ticket_net: n }));
	days[7] = { date: '2026-10-08', net_sales: 130, transactions: 2, ticket_net: 150 };   // two sales of 150 and a 20 return
	const k = sovSales(days);
	assert.strictEqual(k.net, 130);
	assert.strictEqual(k.tx, 2);
	assert.strictEqual(k.avg, 75);
	assert.strictEqual(k.lastWeek, 100);
	assert.strictEqual(k.bars.length, 7);
	assert.strictEqual(k.bars[6], 36); // today is the tallest
	assert.strictEqual(k.bars[0], 4);  // a day with no sales is the stub
	assert.strictEqual(sovSales(days.map((d, i) => (i === 0 ? { ...d, net_sales: 0 } : d))).lastWeek, 0);   // "No sales last ..."
	assert.strictEqual(sovSales(days.map((d, i) => (i === 0 ? { ...d, net_sales: -45.5 } : d))).lastWeek, -45.5);
	assert.strictEqual(sovSales(days.slice(1)).lastWeek, 0);                                              // no such day
	assert.strictEqual(sovSales([{ net_sales: -20, transactions: 0, ticket_net: 0 }]).avg, 0);            // only a return
	const empty = sovSales([]);
	assert.deepStrictEqual([empty.net, empty.tx, empty.avg, empty.lastWeek], [0, 0, 0, 0]);
	assert.deepStrictEqual(sovBars([0, 0, 0]), [4, 4, 4]);
});

test('All records is asked for again until it has come back without an error', () => {
	assert.strictEqual(sovNeedRecords(null), true);
	assert.strictEqual(sovNeedRecords({ error: 'x' }), true);
	assert.strictEqual(sovNeedRecords([]), false);
	assert.strictEqual(sovNeedRecords([{ title: 'Sales', items: [] }]), false);
});

test('an All records link goes through the route builder, a URL as it is, anything else nowhere', () => {
	const seen = [];
	const gen = (o) => { seen.push(o); return `/desk/built/${o.type}`; };
	assert.strictEqual(sovRecordHref({ link_type: 'Report', link_to: 'Stock Ledger', is_query_report: true, ref_doctype: 'Stock Ledger Entry' }, gen),
		'/desk/built/Report');
	assert.deepStrictEqual(seen[0], { type: 'Report', name: 'Stock Ledger', public: true, is_query_report: true, report_ref_doctype: 'Stock Ledger Entry' });
	assert.strictEqual(sovRecordHref({ link_type: 'Report', link_to: 'Builder', is_query_report: false, ref_doctype: 'Item' }, gen), '/desk/built/Report');
	assert.strictEqual(seen[1].is_query_report, false);
	assert.strictEqual(sovRecordHref({ link_type: 'Dashboard', link_to: 'Stock' }, gen), '/desk/built/Dashboard');
	assert.strictEqual(sovRecordHref({ link_type: 'URL', link_to: '/desk/range-board' }, gen), '/desk/range-board');
	assert.strictEqual(sovRecordHref({ link_type: 'Help', link_to: 'x' }, gen), '');
	assert.strictEqual(seen.length, 3);
});

test('an arriving order shows progress once something came in, its state before', () => {
	assert.deepStrictEqual(sovArrival({ received: 8, expected: 14, status: 'Partially Shipped' }), { kind: 'progress', got: 8, of: 14, pct: 57 });
	assert.deepStrictEqual(sovArrival({ received: 0, expected: 6, status: 'Placed' }), { kind: 'status', label: 'Placed' });
	assert.deepStrictEqual(sovArrival({ received: 0, expected: 6, status: 'Partially Shipped' }), { kind: 'status', label: 'Shipped' });
	assert.deepStrictEqual(sovArrival({ received: 0, expected: 6, status: 'Complete' }), { kind: 'status', label: 'Shipped' });
	assert.deepStrictEqual(sovArrival({ received: 0, expected: 6, status: 'Hold' }), { kind: 'status', label: 'On hold' });
	assert.deepStrictEqual(sovArrival({ received: 0, expected: 0, status: 'Acknowledged' }), { kind: 'status', label: 'Acknowledged' });
});

test('the records filter keeps matching links and drops empty groups; links route without a shell', () => {
	const groups = [
		{ title: 'Sales', items: [{ label: 'POS invoices', link_type: 'DocType', link_to: 'POS Invoice' }, { label: 'Sales report', link_type: 'Report', link_to: 'Sales Report' }] },
		{ title: 'Setup', items: [{ label: 'Brands', link_type: 'DocType', link_to: 'Brand' }] },
	];
	assert.deepStrictEqual(sovFilterRecords(groups, ''), groups);
	assert.deepStrictEqual(sovFilterRecords(groups, '  REPORT '), [{ title: 'Sales', items: [groups[0].items[1]] }]);
	assert.deepStrictEqual(sovFilterRecords(groups, 'zzz'), []);
	assert.strictEqual(sovRoute('DocType', 'POS Invoice'), '/desk/pos-invoice');
	assert.strictEqual(sovRoute('Report', 'Sales Report'), '/desk/query-report/Sales%20Report');
	assert.strictEqual(sovRoute('Page', 'distributor-restock'), '/desk/distributor-restock');
	assert.strictEqual(sovRoute('Workspace', 'Settings'), '/desk/settings');
});

test('dates and times read the way the counter reads them', () => {
	assert.strictEqual(sovEyebrow('2026-10-08'), 'THU · OCT 8');
	assert.strictEqual(sovWeekday('2026-10-08'), 'Thursday');
	assert.strictEqual(sovEyebrow(''), '');
	assert.strictEqual(sovStarted('2026-10-08 10:41:00.123', '2026-10-08'), '10:41 AM');
	assert.strictEqual(sovStarted('2026-10-08 00:05:00', '2026-10-08'), '12:05 AM');
	assert.strictEqual(sovStarted('2026-10-07 16:00:00', '2026-10-08'), 'Yesterday');
	assert.strictEqual(sovStarted('2026-10-01 16:00:00', '2026-10-08'), 'Oct 1');
	assert.strictEqual(sovStarted('', '2026-10-08'), '');
	assert.strictEqual(sovAgo('2026-10-08 10:00:00', '2026-10-08 10:04:30'), '4 min ago');
	assert.strictEqual(sovAgo('2026-10-08 10:00:00', '2026-10-08 10:00:20'), 'just now');
	assert.strictEqual(sovAgo('2026-10-08 07:00:00', '2026-10-08 10:00:00'), '3 h ago');
	assert.strictEqual(sovAgo('2026-10-06 10:00:00', '2026-10-08 10:00:00'), '2 days ago');
	assert.strictEqual(sovAgo('', '2026-10-08 10:00:00'), '');
	assert.strictEqual(sovInitials('Marcus Hale'), 'MH');
	assert.strictEqual(sovInitials('Cher'), 'C');
	assert.strictEqual(sovInitials(''), '?');
	assert.strictEqual(sovMoney(-40), '-$40.00');
});
