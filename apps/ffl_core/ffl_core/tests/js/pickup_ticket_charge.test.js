'use strict';
// Complete Order on a counter pickup's 4473 ticket (POS Invoice.ffl_counter_order). The money is on the order the
// ticket hands over; a register copy re-priced to the gun's list price once asked to charge the customer again.
// The bundle is desk code that does not load under Node: this runs its setupCardPosHook block alone against
// stand-in frappe / POS controller objects and checks that a ticket never opens the card dialog or reaches the
// terminal, is saved back to the server's $0 and repainted, and still submits — while an ordinary card sale on the
// same harness does reach the card dialog (so the harness exercises the real path).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const kernel = require('../../public/js/pos_tender_guard.js');
const cardCharges = require('../../public/js/card_charges.js');
const BUNDLE = fs.readFileSync(path.join(__dirname, '../../public/js/ffl_core.bundle.js'), 'utf8');

function flt(v) { const n = parseFloat(v); return isNaN(n) ? 0 : n; }

function zeroed(doc) {
	// what keep_ticket_free sends back
	doc.items.forEach((r) => { r.rate = 0; r.price_list_rate = 0; r.amount = 0; });
	doc.payments.forEach((p) => { p.amount = 0; p.base_amount = 0; });
	Object.assign(doc, { grand_total: 0, rounded_total: 0, paid_amount: 0, change_amount: 0 });
}

// opts.kernel=false: pos_tender_guard.js missing (the bundle's own fallback decides).
// opts.saveKeepsPrice: the save comes back still priced (something re-priced the copy again).
function harness(doc, opts = {}) {
	const seen = { calls: [], dialogs: [], alerts: [], msgprints: [], saves: 0, submits: 0, repaints: [], loads: 0 };
	const frm = {
		doc,
		is_dirty: () => false,
		dirty() { frm._dirty = true; },
		save() {
			seen.saves++;
			if (doc.ffl_counter_order && !opts.saveKeepsPrice) zeroed(doc);
			return Promise.resolve();
		},
		reload_doc: () => Promise.resolve(),
	};
	class Dialog {
		constructor(opts) {
			seen.dialogs.push(opts.title);
			this.handlers = {};
			this.$wrapper = { on: (evt, fn) => { this.handlers[evt] = fn; }, is: () => false };
		}
		show() { setTimeout(() => this.handlers['hidden.bs.modal'] && this.handlers['hidden.bs.modal'](), 0); }
		hide() {}
	}
	const frappe = {
		ui: { Dialog },
		call: async (opts) => {
			seen.calls.push(opts.method);
			if (opts.method === 'ffl_core.firearm.firearm_items_in') return { message: [] };
			return { message: {} };
		},
		db: { get_value: async () => ({ message: { outstanding_amount: 0 } }) },
		show_alert: (m) => { seen.alerts.push(m.message); },
		msgprint: (m) => { seen.msgprints.push(m.message || m); },
		utils: { escape_html: (v) => String(v) },
		provide() {},
	};
	// ERPNext's ItemCart, just enough: load_invoice paints the cart from the frm.
	class ItemCart {
		constructor() { this.events = { get_frm: () => frm }; }
		load_invoice() { seen.loads++; }
	}
	const controller = {
		frm,
		cart: { load_invoice: () => seen.repaints.push('cart') },
		payment: {
			events: { submit_invoice: () => { seen.submits++; } },
			render_payment_section: () => seen.repaints.push('payment'),
			render_payment_mode_dom() {}, update_totals_section() {},
		},
	};
	const sandbox = {
		frappe, flt, cint: (v) => parseInt(v, 10) || 0,
		__: (s, args) => (args ? s.replace(/\{(\d+)\}/g, (_m, i) => args[+i]) : s),
		format_currency: (v) => `$${Number(v).toFixed(2)}`,
		document: { readyState: 'complete' },
		location: { hash: '#point-of-sale', pathname: '/app/point-of-sale' },
		setTimeout,
		erpnext: { PointOfSale: { Controller: function Controller() {}, ItemCart } },
		cur_pos: controller,
		ffl: {
			cardCharges,
			negativeTicketState: kernel.negativeTicketState,
			installClassHook: kernel.installClassHook,
		},
	};
	if (opts.kernel !== false) sandbox.ffl.pickupTicketState = kernel.pickupTicketState;
	sandbox.window = sandbox;
	const start = BUNDLE.indexOf('(function setupCardPosHook() {');
	assert.ok(start > 0, 'card processor block not found in the bundle');
	const end = BUNDLE.indexOf('\n})();', start) + '\n})();'.length;
	vm.runInNewContext(BUNDLE.slice(start, end), sandbox);
	assert.ok(controller.payment.__cardWrapped, 'the block wrapped Complete Order');
	return {
		seen, submit: () => controller.payment.events.submit_invoice(),
		// the register loading the ticket into its cart (Recent Orders → Edit, Open counter order)
		load: async () => { new ItemCart().load_invoice(); for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0)); },
	};
}

const REPRICED_TICKET = () => ({
	name: 'POS-T1', doctype: 'POS Invoice', ffl_counter_order: 'SI-1', is_return: 0,
	grand_total: 670, rounded_total: 670, paid_amount: 670, change_amount: 0,
	items: [{ item_code: 'GUN', rate: 670, price_list_rate: 670, amount: 670 }],
	payments: [{ mode_of_payment: 'Credit Card', amount: 670, base_amount: 670 }],
	card_charges: [],
});

test('a re-priced pickup ticket never opens the card dialog or the terminal, and submits at $0', async () => {
	const { seen, submit } = harness(REPRICED_TICKET());
	await submit();
	assert.deepStrictEqual(seen.dialogs, [], 'no card dialog');
	assert.ok(!seen.calls.some((m) => /payments\.api/.test(m)), `no terminal call: ${seen.calls}`);
	assert.strictEqual(seen.saves, 1, 'saved so the server pins it back to $0');
	assert.deepStrictEqual(seen.repaints, ['cart', 'payment'], 'the screen shows the server\'s $0');
	assert.ok(seen.alerts.some((m) => /pickup ticket for SI-1/.test(m)), 'the cashier is told why');
	assert.strictEqual(seen.submits, 1, 'Complete Order goes through');
});

test('a $0 pickup ticket submits without a save, a prompt or a terminal call', async () => {
	const doc = REPRICED_TICKET();
	zeroed(doc);
	const { seen, submit } = harness(doc);
	await submit();
	assert.deepStrictEqual([seen.dialogs, seen.saves, seen.alerts], [[], 0, []]);
	assert.ok(!seen.calls.some((m) => /payments\.api/.test(m)));
	assert.strictEqual(seen.submits, 1);
});

test('control: an ordinary card sale on the same harness reaches the card dialog', async () => {
	const doc = REPRICED_TICKET();
	doc.ffl_counter_order = '';
	const { seen, submit } = harness(doc);
	await submit();
	assert.deepStrictEqual(seen.dialogs, ['Card payment']);
	assert.strictEqual(seen.submits, 0, 'the cashier backed out of the dialog');
});

test('kernel missing: the bundle fallback reads the money off the doc like the kernel does', async () => {
	const free = REPRICED_TICKET();
	zeroed(free);
	let h = harness(free, { kernel: false });
	await h.submit();
	assert.deepStrictEqual([h.seen.saves, h.seen.submits, h.seen.dialogs], [0, 1, []], 'a $0 ticket still completes');
	h = harness(REPRICED_TICKET(), { kernel: false });
	await h.submit();
	assert.deepStrictEqual([h.seen.saves, h.seen.submits, h.seen.dialogs], [1, 1, []], 'a priced one is saved to $0 first');
	assert.ok(!h.seen.calls.some((m) => /payments\.api/.test(m)));
});

test('a ticket still priced after the save stops Complete Order out loud', async () => {
	const { seen, submit } = harness(REPRICED_TICKET(), { saveKeepsPrice: true });
	await submit();
	assert.strictEqual(seen.submits, 0);
	assert.ok(seen.msgprints.some((m) => /still showing a price/.test(m) && /[Rr]eload/.test(m) && /payment/.test(m)),
		`clear message: ${seen.msgprints}`);
	assert.deepStrictEqual(seen.dialogs, []);
});

test('loading a priced ticket into the cart pins it once: save, repaint, "take no payment"', async () => {
	const doc = REPRICED_TICKET();
	const { seen, load } = harness(doc);
	await load();
	assert.strictEqual(seen.saves, 1, 'saved so the server pins $0 — and no save loop from the repaint');
	assert.strictEqual(seen.loads, 2, 'the load, then one repaint from the pinned doc');
	assert.ok(seen.alerts.some((m) => /order SI-1/.test(m) && /take no payment/i.test(m)), `${seen.alerts}`);
	assert.strictEqual(flt(doc.grand_total), 0);
});

test('loading a $0 ticket, a submitted one or an ordinary sale does nothing', async () => {
	const free = REPRICED_TICKET();
	zeroed(free);
	const submitted = { ...REPRICED_TICKET(), docstatus: 1 };
	const sale = { ...REPRICED_TICKET(), ffl_counter_order: '' };
	for (const doc of [free, submitted, sale]) {
		const { seen, load } = harness(doc);
		await load();
		assert.deepStrictEqual([seen.saves, seen.loads, seen.alerts], [0, 1, []]);
	}
});

test('a ticket the save cannot bring to $0 on load says so once, no loop', async () => {
	const { seen, load } = harness(REPRICED_TICKET(), { saveKeepsPrice: true });
	await load();
	assert.strictEqual(seen.saves, 1);
	assert.strictEqual(seen.loads, 2);
	assert.strictEqual(seen.msgprints.filter((m) => /still showing a price/.test(m)).length, 1);
});
