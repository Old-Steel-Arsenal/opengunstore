'use strict';
// The payment screen of a negative ticket (trade-ins exceed the goods). The bundle is desk code that does
// not load under Node: this runs its fflNegativeTicketScreen block alone against stand-in frappe / jQuery / Payment
// objects, and checks what the cashier is left with: no tender tiles or number pad, "Store pays customer $X",
// Cash preselected, a reference box for every other method, the primary button quiet until valid. The decisions
// themselves are pos_tender_guard.test.js; this pins that the screen obeys them.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const kernel = require('../../public/js/pos_tender_guard.js');
const BUNDLE = fs.readFileSync(path.join(__dirname, '../../public/js/ffl_core.bundle.js'), 'utf8');

function loadBlock() {
	const start = BUNDLE.indexOf('(function fflNegativeTicketScreen() {');
	assert.ok(start > 0, 'negative-ticket block not found in the bundle');
	const end = BUNDLE.indexOf('\n})();', start) + '\n})();'.length;
	const writes = [];
	const frappe = {
		utils: { escape_html: (v) => String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;') },
		model: {
			// like frappe.model.set_value: the doc moves at once, the promise is the triggers
			set_value(doctype, name, field, value) {
				writes.push([field, value]);
				const doc = sandbox.__docs[name];
				if (doc) doc[field] = value;
				return Promise.resolve();
			},
		},
	};
	const sandbox = {
		window: {}, frappe, __docs: {},
		__: (s, args) => (args ? s.replace(/\{(\d+)\}/g, (_m, i) => args[+i]) : s),
		format_currency: (v) => `$${Number(v).toFixed(2)}`,
		$: (el) => ({ attr: (k) => el.attrs[k], val: () => el.value }),
	};
	sandbox.window.ffl = {
		PAYOUT_METHODS: kernel.PAYOUT_METHODS,
		negativeTicketState: kernel.negativeTicketState,
		negativeTicketFix: kernel.negativeTicketFix,
		payoutMethodFields: kernel.payoutMethodFields,
		payoutSummary: kernel.payoutSummary,
	};
	sandbox.window.frappe = frappe;
	vm.runInNewContext(BUNDLE.slice(start, end), sandbox);
	return { api: sandbox.window.ffl, sandbox, writes };
}

// Just enough of ERPNext's Payment component + jQuery wrapper for the block.
function makePayment(sandbox, doc, { visible = true } = {}) {
	sandbox.__docs[doc.name] = doc;
	const handlers = {};
	const state = { html: null, numpadDisplay: undefined, submitClasses: new Set(), hint: '', totals: null };
	const p = {
		allow_partial_payment: 0,
		selected_mode: { stale: true },
		events: { get_frm: () => ({ doc }) },
		renders: 0,
		render_payment_mode_dom() { p.renders++; },
		$numpad: { css: (k, v) => { state.numpadDisplay = v; } },
		$component: { find: () => ({ toggleClass: (c, on) => { on ? state.submitClasses.add(c) : state.submitClasses.delete(c); },
			removeClass: (c) => state.submitClasses.delete(c) }) },
		$payment_modes: {
			is: () => visible,
			html: (h) => { state.html = h; },
			off: () => {},
			on: (evt, sel, fn) => { handlers[`${evt}|${sel}`] = fn; },
			find: () => ({ text: (t) => { state.hint = t; } }),
		},
		$totals: { is: () => visible, html: (h) => { state.totals = h; } },
	};
	return { p, state, handlers };
}

const NEG = () => ({ name: 'POS-1', doctype: 'POS Invoice', currency: 'USD', grand_total: -4885,
	items: [{ item_code: 'GLOCK', amount: 565 }, { item_code: 'TRADE-IN-CREDIT', amount: -5450 }],
	payments: [{ name: 'row1', doctype: 'Sales Invoice Payment', amount: 0 }] });

test('negative ticket: no tender tiles — the payout screen replaces them, Cash preselected', () => {
	const { api, sandbox, writes } = loadBlock();
	const doc = NEG();
	const { p, state } = makePayment(sandbox, doc);
	const drawn = api.renderNegativeTicket(p);
	assert.strictEqual(drawn, true);                                  // caller skips ERPNext's tiles
	assert.match(state.html, /Store pays customer \$4885\.00/);
	for (const m of ['Cash', 'Zelle', 'Check', 'ACH']) assert.ok(state.html.includes(`data-method="${m}"`), m);
	assert.ok(!/Credit Card|payroc/i.test(state.html));
	assert.ok(/ffl-payout-btn ffl-payout-on border-primary" data-method="Cash"/.test(state.html), 'Cash is the selected method');
	// ERPNext's Ctrl+Enter completes only while a tile is highlighted (border-primary): exactly one is, the picked method
	assert.strictEqual((state.html.match(/border-primary/g) || []).length, 1);
	assert.ok(!state.html.includes('ffl-payout-ref'), 'Cash needs no reference box');
	assert.deepStrictEqual(writes, [['ffl_trade_in_payout_method', 'Cash']]);   // written onto the doc, one click saved
	assert.strictEqual(state.numpadDisplay, 'none');                  // number pad hidden
	assert.strictEqual(p.selected_mode, '');                          // a stale tile cannot catch the keyboard
	assert.strictEqual(p.allow_partial_payment, 1);                   // ERPNext's "no payment" refusal stepped aside
	assert.ok(!state.submitClasses.has('ffl-submit-blocked'), 'Cash is valid — Complete is live');
});

test('negative ticket: a non-Cash method shows the reference box and blocks Complete until it is filled', () => {
	const { api, sandbox } = loadBlock();
	const doc = NEG();
	doc.ffl_trade_in_payout_method = 'Zelle';
	const { p, state, handlers } = makePayment(sandbox, doc);
	api.renderNegativeTicket(p);
	assert.ok(state.html.includes('ffl-payout-ref'));
	assert.match(state.html, /Zelle reference \(required\)/);
	assert.ok(state.submitClasses.has('ffl-submit-blocked'));
	assert.match(state.hint, /Enter the Zelle reference/);
	// typing the reference unblocks it (and lands on the doc)
	handlers['input.fflneg|.ffl-payout-ref'].call({ value: 'CONF-7781' });
	assert.strictEqual(doc.ffl_trade_in_payout_reference, 'CONF-7781');
	assert.ok(!state.submitClasses.has('ffl-submit-blocked'));
	assert.strictEqual(state.hint, '');
});

test('negative ticket: picking a method writes it, clears the old reference and redraws', async () => {
	const { api, sandbox } = loadBlock();
	const doc = NEG();
	doc.ffl_trade_in_payout_method = 'Zelle';
	doc.ffl_trade_in_payout_reference = 'CONF-7781';
	const { p, handlers } = makePayment(sandbox, doc);
	api.renderNegativeTicket(p);
	handlers['click.fflneg|.ffl-payout-btn'].call({ attrs: { 'data-method': 'Check' } });
	await new Promise((r) => setImmediate(r));
	assert.strictEqual(doc.ffl_trade_in_payout_method, 'Check');
	assert.strictEqual(doc.ffl_trade_in_payout_reference, '');
	assert.strictEqual(p.renders, 1);                                  // screen redrawn from the doc
});

test('negative ticket: typing in the reference box never reaches ERPNext\'s keyboard-to-tender handler', () => {
	const { api, sandbox } = loadBlock();
	const { p, handlers } = makePayment(sandbox, NEG());
	api.renderNegativeTicket(p);
	let stopped = false;
	handlers['keydown.fflneg|.ffl-payout-ref']({ stopPropagation: () => { stopped = true; } });
	assert.strictEqual(stopped, true);
});

test('negative ticket: money left on a payment row is zeroed', () => {
	const { api, sandbox, writes } = loadBlock();
	const doc = NEG();
	doc.ffl_trade_in_payout_method = 'Cash';
	doc.payments[0].amount = 286.86;
	const { p } = makePayment(sandbox, doc);
	api.renderNegativeTicket(p);
	assert.deepStrictEqual(writes, [['amount', 0]]);
});

test('negative ticket: a card already charged blocks Complete', () => {
	const { api, sandbox } = loadBlock();
	const doc = NEG();
	doc.ffl_trade_in_payout_method = 'Cash';
	doc.card_charged_amount = 40;
	const { p, state } = makePayment(sandbox, doc);
	api.renderNegativeTicket(p);
	assert.ok(state.submitClasses.has('ffl-submit-blocked'));
	assert.match(state.hint, /card was charged/);
});

test('ordinary ticket: ERPNext draws its tiles; allow_partial_payment is put back; stale payout dropped', () => {
	const { api, sandbox, writes } = loadBlock();
	const doc = NEG();
	const { p, state } = makePayment(sandbox, doc);
	api.renderNegativeTicket(p);
	assert.strictEqual(p.allow_partial_payment, 1);
	// cashier edits the cart: no longer negative
	doc.grand_total = 286.86;
	doc.ffl_trade_in_payout_method = 'Zelle';
	doc.ffl_trade_in_payout_reference = 'CONF';
	state.submitClasses.add('ffl-submit-blocked');
	writes.length = 0;
	assert.strictEqual(api.renderNegativeTicket(p), false);
	assert.strictEqual(p.allow_partial_payment, 0);
	assert.strictEqual(state.numpadDisplay, '');
	assert.ok(!state.submitClasses.has('ffl-submit-blocked'));
	assert.deepStrictEqual(writes, [['ffl_trade_in_payout_method', ''], ['ffl_trade_in_payout_reference', '']]);
});

test('ordinary ticket: nothing is written, nothing drawn', () => {
	const { api, sandbox, writes } = loadBlock();
	const doc = { name: 'POS-2', doctype: 'POS Invoice', currency: 'USD', grand_total: 100, payments: [] };
	const { p, state } = makePayment(sandbox, doc);
	assert.strictEqual(api.renderNegativeTicket(p), false);
	assert.strictEqual(writes.length, 0);
	assert.strictEqual(state.html, null);
	assert.strictEqual(p.allow_partial_payment, 0);
});

test('negative ticket on a hidden payment section: no DOM work (same early-out as ERPNext), still counts as negative', () => {
	const { api, sandbox } = loadBlock();
	const doc = NEG();
	doc.ffl_trade_in_payout_method = 'Cash';
	const { p, state } = makePayment(sandbox, doc, { visible: false });
	assert.strictEqual(api.renderNegativeTicket(p), true);
	assert.strictEqual(state.html, null);
});

test('totals strip: Grand Total (negative) | Store pays customer — and ordinary tickets keep ERPNext\'s', () => {
	const { api, sandbox } = loadBlock();
	const neg = makePayment(sandbox, NEG());
	api.renderNegativeTotals(neg.p);
	assert.match(neg.state.totals, /Grand Total/);
	assert.match(neg.state.totals, /\$-4885\.00/);
	assert.match(neg.state.totals, /Store pays customer/);
	assert.match(neg.state.totals, /\$4885\.00/);
	const pos = makePayment(sandbox, { name: 'POS-3', doctype: 'POS Invoice', currency: 'USD', grand_total: 50, payments: [] });
	api.renderNegativeTotals(pos.p);
	assert.strictEqual(pos.state.totals, null);
});

test('reason text per problem (the submit gate shows these)', () => {
	const { api } = loadBlock();
	assert.match(api.negativeTicketReason({ problem: 'method' }), /Pick how the customer is paid/);
	assert.match(api.negativeTicketReason({ problem: 'reference', method: 'Check' }), /Check reference/);
	assert.match(api.negativeTicketReason({ problem: 'card' }), /void it/);
	assert.strictEqual(api.negativeTicketReason({ problem: null }), '');
});

test('the submit wrapper blocks an invalid negative ticket before any charge or prompt', () => {
	// Source-level pin: the gate sits right after the is_return branch and before the Zelle / card prompts.
	const gate = BUNDLE.indexOf('ffl.negativeTicketState(controller.frm.doc)');
	const zelle = BUNDLE.indexOf('await ensureZelleReference(controller.frm)');
	const ret = BUNDLE.indexOf('if (controller.frm.doc.is_return) {');
	assert.ok(ret > 0 && gate > ret && gate < zelle, 'negative-ticket gate must sit between the return branch and the tender prompts');
});

// ---- the order summary after the sale: ERPNext shows paid_amount (0 by design) and no payments ----
function fakeSummary() {
	class S {
		get_upper_section_html(doc) { return `<div class="right-section"><div class="paid-amount">$ ${doc.paid_amount.toFixed(2)}</div></div>`; }
		get_payment_html(doc, p) { return `<row>${p.mode_of_payment}|${p.amount}</row>`; }
		attach_payments_info(doc) {
			this.$payment_container.html = (h) => { this.html = h; };
			this.html = '';
			doc.payments.forEach((p) => { if (p.amount) this.html += this.get_payment_html(doc, p); });
		}
	}
	return S;
}

test('summary: a submitted negative ticket shows "Paid out $X" and a "Paid to customer (method, ref)" row', () => {
	const { api } = loadBlock();
	const S = fakeSummary();
	assert.strictEqual(api.applyNegativeSummaryPatch(S), true);
	const doc = { docstatus: 1, grand_total: -4885, items: NEG().items, paid_amount: 0, currency: 'USD', payments: [{ amount: 0 }],
		ffl_trade_in_payout_method: 'Zelle', ffl_trade_in_payout_reference: 'CONF-7781' };
	const s = new S();
	assert.match(s.get_upper_section_html(doc), /<div class="paid-amount">Paid out \$4885\.00<\/div>/);
	s.$payment_container = { append(h) { s.appended = h; }, html() {} };
	s.attach_payments_info(doc);
	assert.strictEqual(s.appended, '<row>Paid to customer (Zelle, CONF-7781)|4885</row>');
});

test('summary: an ordinary or draft order is left to ERPNext, and patching twice is a no-op', () => {
	const { api } = loadBlock();
	const S = fakeSummary();
	api.applyNegativeSummaryPatch(S);
	assert.strictEqual(api.applyNegativeSummaryPatch(S), false);
	const s = new S();
	s.$payment_container = { append(h) { s.appended = h; }, html() {} };
	for (const doc of [{ docstatus: 1, grand_total: 100, paid_amount: 100, currency: 'USD', payments: [{ amount: 100, mode_of_payment: 'Cash' }] },
		{ docstatus: 0, grand_total: -4885, items: NEG().items, paid_amount: 0, currency: 'USD', payments: [] }]) {
		s.appended = undefined;
		assert.match(s.get_upper_section_html(doc), new RegExp(`paid-amount">\\$ ${doc.paid_amount.toFixed(2)}`));
		s.attach_payments_info(doc);
		assert.strictEqual(s.appended, undefined);
	}
});

test('a cart that is negative for another reason (a negative price, no trade-in credit) gets the ordinary screen', () => {
	const { api, sandbox, writes } = loadBlock();
	const doc = { name: 'POS-9', doctype: 'POS Invoice', currency: 'USD', grand_total: -100,
		items: [{ item_code: 'GLOCK', amount: 500 }, { item_code: 'SVC', amount: -600 }], payments: [] };
	const { p, state } = makePayment(sandbox, doc);
	assert.strictEqual(api.renderNegativeTicket(p), false);   // ERPNext draws its tiles; the server states the refusal
	assert.strictEqual(state.html, null);
	assert.strictEqual(writes.length, 0);
});

test('a card captured on the draft stays on its row and blocks (not zeroed away)', () => {
	const { api, sandbox, writes } = loadBlock();
	const doc = NEG();
	doc.ffl_trade_in_payout_method = 'Cash';
	doc.card_charged_amount = 40;
	doc.payments[0].amount = 40;
	const { p, state } = makePayment(sandbox, doc);
	api.renderNegativeTicket(p);
	assert.deepStrictEqual(writes, []);
	assert.strictEqual(doc.payments[0].amount, 40);
	assert.ok(state.submitClasses.has('ffl-submit-blocked'));
});
