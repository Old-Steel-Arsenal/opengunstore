'use strict';
// Recent Orders draft chips: ERPNext's list HTML must come back untouched, one
// batched GET must follow a rendered Draft page, chips must land only on cards
// still on screen, and the recycle bin's wrap of the same class must keep working.
const test = require('node:test');
const assert = require('node:assert');
const status = require('../../public/js/pos_order_status.js');
const guard = require('../../public/js/pos_tender_guard.js');
const bin = require('../../public/js/pos_recycle_bin.js');

const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function fakeEnv(calls, responses, opts = {}) {
	const deferred = [];
	return {
		deferred,
		flush() { deferred.splice(0).forEach((fn) => fn()); },
		call(o) {
			calls.push(['call', o.method, o.type, JSON.parse(JSON.stringify(o.args || {}))]);
			if (opts.throwCall) throw new Error('network');
			const message = typeof responses === 'function' ? responses(o) : responses[o.method] !== undefined ? responses[o.method] : responses;
			if (o.callback) o.callback({ message });
			return Promise.resolve();
		},
		esc,
		fmt: (v) => `$${Number(v).toFixed(2)}`,
		when: (d) => `@${d}`,
		ellipsis: (s) => s,
		confirm: (msg, yes) => yes(),
		alert: () => {},
		$: () => ({}),
		defer: (fn) => deferred.push(fn),
		canRead: () => opts.canRead !== false,
		paperMode: () => !!opts.paper,
		fastboundOn: () => opts.fastbound !== false,
		state: guard.form4473State,
		summary: (facts) => guard.draftPaymentSummary(facts, { cardMop: 'Credit Card', cardProcessorOn: true }),
	};
}

// A card as the browser renders it: .invoice-wrapper > .invoice-name-customer.
function makeCard(name, doctype = 'POS Invoice') {
	const inner = {
		chips: [],
		querySelector(sel) {
			if (sel !== '.ffl-order-status' || !this.chips.length) return null;
			return { remove: () => { this.chips = []; } };
		},
		insertAdjacentHTML(pos, html) { this.chips.push(html); },
	};
	return {
		attrs: { 'data-invoice-name': name, 'data-invoice-doctype': doctype },
		isConnected: true,
		inner,
		getAttribute(k) { return this.attrs[k]; },
		querySelector(sel) { return sel === '.invoice-name-customer' ? inner : null; },
	};
}

function fakeListClass(calls) {
	return class {
		constructor(statusValue, cards) {
			this._status = statusValue;
			this.status_field = { df: { options: 'Draft\nPaid' }, get_value: () => this._status, refresh() {} };
			this.search_field = { get_value: () => '' };
			const container = { querySelectorAll: () => (cards || []).filter((c) => c.attrs['data-invoice-doctype'] === 'POS Invoice') };
			this.$invoices_container = Object.assign([container], { html() {}, append() {}, on() {} });
			this.events = { reset_summary() {} };
		}
		make_filter_section() { calls.push(['stock.make_filter_section']); }
		refresh_list() { calls.push(['stock.refresh_list']); }
		get_invoice_html(invoice) { calls.push(['stock.html', invoice.name]); return `<div class="invoice-wrapper">${invoice.name}</div>`; }
	};
}

const FACTS = {
	'PSINV-1': { due: 3084.04, currency: 'USD', charged: 712.25, held: 0, card_status: 'CHARGED', paid_amount: 712.25,
		payments: [{ mode_of_payment: 'Credit Card', amount: 3084.04 }], has_firearm: true, form_4473_status: 'Pending', nics_status: 'N/A' },
	'PSINV-2': { due: 50, currency: 'USD', charged: 0, held: 0, payments: [{ mode_of_payment: 'Cash', amount: 50 }],
		has_firearm: false, form_4473_status: 'N/A', nics_status: 'N/A' },
};

function render(list, names) {
	return names.map((n) => list.get_invoice_html({ doctype: 'POS Invoice', name: n }));
}

test('stock HTML comes back untouched and one GET follows the page, names de-duplicated', () => {
	const calls = [];
	const env = fakeEnv(calls, { [status.STATUS_METHOD]: FACTS });
	const L = fakeListClass(calls);
	assert.strictEqual(status.applyPastOrderListPatch(L, env), true);
	const cards = [makeCard('PSINV-1'), makeCard('PSINV-2'), makeCard('PSINV-1')];
	const list = new L('Draft', cards);
	const html = render(list, ['PSINV-1', 'PSINV-2', 'PSINV-1']);
	assert.deepStrictEqual(html, ['<div class="invoice-wrapper">PSINV-1</div>', '<div class="invoice-wrapper">PSINV-2</div>', '<div class="invoice-wrapper">PSINV-1</div>']);
	assert.strictEqual(calls.filter((c) => c[0] === 'call').length, 0, 'nothing before the deferred flush');
	env.flush();
	const gets = calls.filter((c) => c[0] === 'call');
	assert.deepStrictEqual(gets, [['call', status.STATUS_METHOD, 'GET', { invoices: ['PSINV-1', 'PSINV-2'] }]]);
	assert.strictEqual(cards[0].inner.chips.length, 1);
	assert.strictEqual(cards[2].inner.chips.length, 1, 'a card repeated by a search result is decorated too');
	assert.match(cards[0].inner.chips[0], /Paid \$712\.25 of \$3084\.04/);
	assert.match(cards[0].inner.chips[0], /Awaiting 4473/);
	assert.match(cards[1].inner.chips[0], /Not paid/);
	assert.match(cards[1].inner.chips[0], /Cash \$50\.00 entered · not confirmed/);
	assert.doesNotMatch(cards[1].inner.chips[0], /4473/, 'no 4473 chip without a regulated firearm');
});

test('Paid, Deleted and Sales Invoice rows are left alone', () => {
	for (const st of ['Paid', 'Deleted', 'Consolidated']) {
		const calls = [];
		const env = fakeEnv(calls, FACTS);
		const L = fakeListClass(calls);
		status.applyPastOrderListPatch(L, env);
		render(new L(st, [makeCard('PSINV-1')]), ['PSINV-1']);
		env.flush();
		assert.strictEqual(env.deferred.length, 0);
		assert.strictEqual(calls.filter((c) => c[0] === 'call').length, 0, st);
	}
	const calls = [];
	const env = fakeEnv(calls, FACTS);
	const L = fakeListClass(calls);
	status.applyPastOrderListPatch(L, env);
	new L('Draft', []).get_invoice_html({ doctype: 'Sales Invoice', name: 'SINV-1' });
	env.flush();
	assert.strictEqual(calls.filter((c) => c[0] === 'call').length, 0);
});

test('a card detached by a newer refresh gets nothing; re-decorating replaces the chips', () => {
	const calls = [];
	const env = fakeEnv(calls, { [status.STATUS_METHOD]: FACTS });
	const L = fakeListClass(calls);
	status.applyPastOrderListPatch(L, env);
	const stale = makeCard('PSINV-1');
	const live = makeCard('PSINV-2');
	const list = new L('Draft', [stale, live]);
	render(list, ['PSINV-1', 'PSINV-2']);
	stale.isConnected = false;
	env.flush();
	assert.strictEqual(stale.inner.chips.length, 0);
	assert.strictEqual(live.inner.chips.length, 1);
	render(list, ['PSINV-2']);
	env.flush();
	assert.strictEqual(live.inner.chips.length, 1, 'old chips removed before the new ones');
});

test('everything shown is escaped', () => {
	const calls = [];
	const facts = { 'PSINV-X': { due: 10, currency: 'USD', payments: [{ mode_of_payment: '<img src=x onerror=alert(1)>', amount: 5 }], has_firearm: false } };
	const env = fakeEnv(calls, { [status.STATUS_METHOD]: facts });
	const L = fakeListClass(calls);
	status.applyPastOrderListPatch(L, env);
	const card = makeCard('PSINV-X');
	render(new L('Draft', [card]), ['PSINV-X']);
	env.flush();
	assert.doesNotMatch(card.inner.chips[0], /<img/);
	assert.match(card.inner.chips[0], /&lt;img src=x/);
});

test('4473 label table across FastBound, paper mode and neither', () => {
	const env = (o) => fakeEnv([], {}, o);
	const lbl = (f, n, o) => status.form4473Label({ form_4473_status: f, nics_status: n }, env(o)).text;
	assert.strictEqual(lbl('Completed', 'Proceed', {}), '4473 Complete');
	assert.strictEqual(lbl('Pending', 'N/A', {}), 'Awaiting 4473');
	assert.strictEqual(lbl('Completed', 'Denied', {}), 'Awaiting 4473');
	assert.strictEqual(lbl('N/A', 'N/A', {}), '4473 not started');
	assert.strictEqual(lbl('Completed', 'Proceed', { paper: true }), '4473 Recorded');
	assert.strictEqual(lbl('Pending', 'N/A', { paper: true }), 'Record Paper 4473');
	assert.strictEqual(lbl('N/A', 'N/A', { fastbound: false }), '4473 Not Configured');
	assert.strictEqual(lbl('Completed', 'Proceed', { fastbound: false }), '4473 Complete', 'a recorded 4473 is never painted broken');
	// The cart's payment-screen badge (ffl_core.bundle.js getStatusBadge) names the not-started state its own way.
	const badge = (f, n) => status.form4473Label({ form_4473_status: f, nics_status: n }, env({}), { notStartedText: 'FastBound 4473' });
	assert.deepStrictEqual(badge('N/A', 'N/A'), { color: 'gray', text: 'FastBound 4473' });
	assert.deepStrictEqual(badge('Pending', 'N/A'), { color: 'orange', text: 'Awaiting 4473' });
});

test('payment labels', () => {
	const env = fakeEnv([], {});
	assert.strictEqual(status.paymentLabel(null, 'USD', env), null);
	assert.strictEqual(status.paymentLabel({ state: 'none' }, 'USD', env).text, 'Not paid');
	assert.strictEqual(status.paymentLabel({ state: 'in_progress' }, 'USD', env).text, 'Card in progress');
	assert.strictEqual(status.paymentLabel({ state: 'in_progress', paid: 0, due: 500 }, 'USD', env).text, 'Card in progress');
	assert.strictEqual(
		status.paymentLabel({ state: 'in_progress', paid: 712.25, due: 3084.04 }, 'USD', env, true).text,
		'Card in progress · Paid $712.25 of $3084.04', 'the first card of a split stays visible while the second runs');
	assert.strictEqual(status.paymentLabel({ state: 'paid', paid: 5, due: 5 }, 'USD', env, false).text, 'Paid');
	assert.strictEqual(status.paymentLabel({ state: 'paid', paid: 5, due: 5 }, 'USD', env, true).text, 'Paid $5.00');
	assert.strictEqual(status.paymentLabel({ state: 'partial', paid: 1, due: 5 }, 'USD', env).text, 'Paid $1.00 of $5.00');
	assert.strictEqual(status.rowChipsHtml({ due: 0, payments: [], has_firearm: false }, env), '', 'nothing to say → no chip line');
});

test('no read permission, or a failing call, never disturbs the list', () => {
	const calls = [];
	const env = fakeEnv(calls, FACTS, { canRead: false });
	const L = fakeListClass(calls);
	status.applyPastOrderListPatch(L, env);
	render(new L('Draft', [makeCard('PSINV-1')]), ['PSINV-1']);
	env.flush();
	assert.strictEqual(calls.filter((c) => c[0] === 'call').length, 0);

	const calls2 = [];
	const env2 = fakeEnv(calls2, FACTS, { throwCall: true });
	const L2 = fakeListClass(calls2);
	status.applyPastOrderListPatch(L2, env2);
	const list = new L2('Draft', [makeCard('PSINV-1')]);
	render(list, ['PSINV-1']);
	assert.doesNotThrow(() => env2.flush());

	const calls3 = [];
	const env3 = fakeEnv(calls3, FACTS);
	const L3 = fakeListClass(calls3);
	status.applyPastOrderListPatch(L3, env3);
	const broken = new L3('Draft', []);
	broken.status_field = { get_value() { throw new Error('boom'); } };
	assert.strictEqual(broken.get_invoice_html({ doctype: 'POS Invoice', name: 'PSINV-1' }), '<div class="invoice-wrapper">PSINV-1</div>');
});

test('the patch is idempotent and declines a class without the method', () => {
	const calls = [];
	const L = fakeListClass(calls);
	assert.strictEqual(status.applyPastOrderListPatch(L, fakeEnv(calls, {})), true);
	assert.strictEqual(status.applyPastOrderListPatch(L, fakeEnv(calls, {})), false);
	assert.strictEqual(status.applyPastOrderListPatch(class {}, fakeEnv(calls, {})), false);
	assert.strictEqual(status.applyPastOrderListPatch(null, fakeEnv(calls, {})), false);
});

test('recycle bin and draft chips on the same class work in either order', () => {
	for (const order of [['bin', 'status'], ['status', 'bin']]) {
		const calls = [];
		const env = fakeEnv(calls, (o) => (o.method === status.STATUS_METHOD ? FACTS : []));
		const L = fakeListClass(calls);
		for (const which of order) {
			if (which === 'bin') bin.applyPastOrderListPatch(L, env);
			else status.applyPastOrderListPatch(L, env);
		}
		const deleted = new L('Deleted', []);
		deleted.refresh_list();
		assert.ok(calls.some((c) => c[0] === 'call' && c[1] === bin.LIST_METHOD), `${order}: Deleted still renders the bin`);
		const card = makeCard('PSINV-1');
		render(new L('Draft', [card]), ['PSINV-1']);
		env.flush();
		assert.strictEqual(card.inner.chips.length, 1, `${order}: Draft still gets chips`);
	}
});

// ------------------------------------------------------------- summary
function fakeRoot() {
	const right = {
		chips: [],
		querySelector(sel) { return sel === '.ffl-order-status' && this.chips.length ? { remove: () => { this.chips = []; } } : null; },
		insertAdjacentHTML(pos, html) { this.chips.push(html); },
	};
	const paid = { textContent: '$3,084.04' };
	return {
		isConnected: true, right, paid,
		querySelector(sel) { return sel === '.paid-amount' ? paid : sel === '.right-section' ? right : null; },
	};
}

function fakeSummaryClass(calls) {
	return class {
		get_upper_section_html(doc) { calls.push(['stock.upper', doc.name]); return '<div class="left-section"></div>'; }
	};
}

test('summary: a draft sale shows the confirmed figure, entered tender and 4473 state', () => {
	const calls = [];
	const env = fakeEnv(calls, { [status.STATUS_METHOD]: FACTS });
	const S = fakeSummaryClass(calls);
	assert.strictEqual(status.applyPastOrderSummaryPatch(S, env), true);
	const summary = new S();
	const root = fakeRoot();
	summary.$upper_section = [root];
	summary.doc = { doctype: 'POS Invoice', name: 'PSINV-1', docstatus: 0 };
	assert.strictEqual(summary.get_upper_section_html(summary.doc), '<div class="left-section"></div>');
	env.flush();
	assert.deepStrictEqual(calls.filter((c) => c[0] === 'call').map((c) => c[3]), [{ invoices: ['PSINV-1'] }]);
	assert.strictEqual(root.paid.textContent, 'Paid $712.25 of $3084.04');
	assert.match(root.right.chips[0], /Awaiting 4473/);
});

test('summary: an answer for an order no longer open, a submitted sale or a return changes nothing', () => {
	const calls = [];
	const env = fakeEnv(calls, { [status.STATUS_METHOD]: FACTS });
	const S = fakeSummaryClass(calls);
	status.applyPastOrderSummaryPatch(S, env);
	const summary = new S();
	const root = fakeRoot();
	summary.$upper_section = [root];
	summary.doc = { doctype: 'POS Invoice', name: 'PSINV-1', docstatus: 0 };
	summary.get_upper_section_html(summary.doc);
	summary.doc = { doctype: 'POS Invoice', name: 'PSINV-9', docstatus: 0 };   // clerk clicked another order
	env.flush();
	assert.strictEqual(root.paid.textContent, '$3,084.04');
	assert.strictEqual(root.right.chips.length, 0);

	for (const doc of [{ doctype: 'POS Invoice', name: 'PSINV-1', docstatus: 1 }, { doctype: 'POS Invoice', name: 'PSINV-1', docstatus: 0, is_return: 1 }, { doctype: 'Sales Invoice', name: 'S-1', docstatus: 0 }]) {
		const c = [];
		const e = fakeEnv(c, FACTS);
		const SS = fakeSummaryClass(c);
		status.applyPastOrderSummaryPatch(SS, e);
		new SS().get_upper_section_html(doc);
		assert.strictEqual(e.deferred.length, 0);
	}
});
