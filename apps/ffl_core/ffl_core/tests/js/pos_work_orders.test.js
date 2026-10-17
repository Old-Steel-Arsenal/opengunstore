'use strict';
// Recent Orders › Work Orders: the filter lists open gunsmith jobs from ffl_core's
// endpoint, every other status stays on the refresh_list that was there before, and a
// card's Complete reaches the register action with the job and the labor item.
const test = require('node:test');
const assert = require('node:assert');
const wo = require('../../public/js/pos_work_orders.js');

function fakeEnv(calls, response) {
	return {
		call(opts) { calls.push(['call', opts.method, opts.args]); if (opts.callback) opts.callback({ message: response }); return Promise.resolve(); },
		esc: (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])),
		fmt: (v) => `$${v}`,
		ellipsis: (s, n) => (s.length > n ? s.slice(0, n) + '...' : s),
		$: () => ({}),
		complete: (row, labor) => { calls.push(['complete', row.name, labor]); return true; },
	};
}

function fakeListClass(calls) {
	return class {
		constructor(status) {
			this.status_field = { df: { options: 'Draft\nPaid\nDeleted' }, _v: status, get_value() { return this._v; }, refresh() {} };
			this.search_field = { get_value: () => 'pat' };
			this.html = [];
			this.$invoices_container = { html: (h) => { this.html = [h]; }, append: (h) => this.html.push(h), on: (ev, sel) => calls.push(['on', sel]) };
			this.events = { reset_summary: () => calls.push(['reset_summary']) };
		}
		make_filter_section() { calls.push(['stock.make_filter_section']); }
		refresh_list() { calls.push(['stock.refresh_list']); }
	};
}

const READY = { name: 'GWO-2026-00001', customer: 'C1', customer_name: 'Pat <Owner>', status: 'Ready', manufacturer: 'Colt',
	model: '1911', serial_number: 'SN1', estimated_cost: 90, deposit_held: 100, lines: [], drafts: { deposit: null, pickup: null } };

test('withWorkOrdersOption appends once, after the other patches', () => {
	assert.strictEqual(wo.withWorkOrdersOption('Draft\nDeleted'), 'Draft\nDeleted\nWork Orders');
	assert.strictEqual(wo.withWorkOrdersOption('Draft\nWork Orders'), 'Draft\nWork Orders');
	assert.strictEqual(wo.withWorkOrdersOption(['Draft']), 'Draft\nWork Orders');
});

test('patch adds the option, binds the actions, and is idempotent', () => {
	const calls = [];
	const L = fakeListClass(calls);
	assert.strictEqual(wo.applyPastOrderListPatch(L, fakeEnv(calls, {})), true);
	assert.strictEqual(wo.applyPastOrderListPatch(L, fakeEnv(calls, {})), false);
	const l = new L('Draft');
	l.make_filter_section();
	assert.strictEqual(l.status_field.df.options, 'Draft\nPaid\nDeleted\nWork Orders');
	assert.deepStrictEqual(calls.filter((c) => c[0] === 'on').map((c) => c[1]), ['.ffl-work-order', '.ffl-wo-action']);
});

test('other statuses fall through to the previous refresh_list', () => {
	const calls = [];
	const L = fakeListClass(calls);
	wo.applyPastOrderListPatch(L, fakeEnv(calls, {}));
	new L('Deleted').refresh_list();
	assert.deepStrictEqual(calls, [['stock.refresh_list']]);
});

test('Work Orders renders the open jobs, escaped, with Complete', () => {
	const calls = [];
	const L = fakeListClass(calls);
	wo.applyPastOrderListPatch(L, fakeEnv(calls, { labor_item: 'GUNSMITHING', orders: [READY] }));
	const l = new L('Work Orders');
	l.refresh_list();
	assert.deepStrictEqual(calls.slice(0, 2), [['reset_summary'], ['call', wo.LIST_METHOD, { search_term: 'pat' }]]);
	assert.ok(!calls.some((c) => c[0] === 'stock.refresh_list'));
	const html = l.html.join('');
	assert.ok(html.includes('data-work-order="GWO-2026-00001"'));
	assert.ok(html.includes('Pat &lt;Owner&gt;'));
	assert.ok(html.includes('Colt 1911'));
	assert.ok(html.includes('Deposit held $100'));
	assert.ok(html.includes('data-action="complete"'));
	assert.ok(!html.includes('data-action="deposit"'));
	assert.strictEqual(wo.runAction(l, fakeEnv(calls, {}), 'complete', READY.name), true);
	assert.deepStrictEqual(calls[calls.length - 1], ['complete', READY.name, 'GUNSMITHING']);
	assert.strictEqual(wo.runAction(l, fakeEnv(calls, {}), 'complete', 'GWO-other'), false);
});

test('an empty list says so', () => {
	const calls = [];
	const L = fakeListClass(calls);
	wo.applyPastOrderListPatch(L, fakeEnv(calls, { orders: [] }));
	const l = new L('Work Orders');
	l.refresh_list();
	assert.ok(l.html.join('').includes('No open work orders'));
});

test('an open pick-up draft is opened, not confirmed again', () => {
	assert.deepStrictEqual(wo.cardActions(READY).map((a) => a.label), ['Complete']);
	assert.deepStrictEqual(wo.cardActions({ ...READY, drafts: { pickup: 'ACC-PSINV-1' } }).map((a) => a.label), ['Open pick-up']);
	assert.strictEqual(wo.depositText({ deposit_held: 0 }, String), '');
});

test('Complete starts from the job\'s charges, else one labor line at the estimate', () => {
	assert.deepStrictEqual(wo.defaultCharges(READY, 'GUNSMITHING'),
		[{ item_code: 'GUNSMITHING', description: '', qty: 1, rate: 90 }]);
	assert.deepStrictEqual(wo.defaultCharges({ lines: [{ item_code: 'P', qty: '2', rate: '5.5' }] }, 'GUNSMITHING'),
		[{ item_code: 'P', description: '', qty: 2, rate: 5.5 }]);
	assert.deepStrictEqual(wo.defaultCharges({ lines: [] }, ''), []);
});
