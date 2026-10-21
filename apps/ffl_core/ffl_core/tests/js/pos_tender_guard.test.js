'use strict';
// RED — P2: a held, already-charged firearm
// order resumed from Recent Orders must NOT have its recorded tender reset to
// the POS Profile default. The pure predicate below decides "does this draft
// carry a live tender we must preserve?" — the single source of truth shared by
// the bundle's loadInvoiceIntoCart and the edit_order monkey-patch.
const test = require('node:test');
const assert = require('node:assert');
const {
	hasLiveTender,
	editOrderResumeSteps,
	installClassHook,
	applyHeldTenderCheckoutGuard,
	lockedRowFix,
	isLockedLine,
} = require('../../public/js/pos_tender_guard.js');

// A fake POS Controller recording every call, so the resume step-list's arg
// threading (the ERPNext edit_order arity contract) and the reset-skip rule are
// pinned without a browser.
function makeCtrl(calls, payments) {
	return {
		make_invoice_frm(d) { calls.push(['make_invoice_frm', d]); },
		sync_draft_invoice_to_frm(d, n) { calls.push(['sync_draft_invoice_to_frm', d, n]); },
		frm: {
			doc: { payments: payments || [] },
			refresh(n) { calls.push(['frm.refresh', n]); },
			call(m) { calls.push(['frm.call', m]); return Promise.resolve(); },
		},
		cart: { load_invoice() { calls.push(['cart.load_invoice']); } },
		toggle_components(v) { calls.push(['toggle_components', v]); },
	};
}

test('live full CHARGED card capture → preserve', () => {
	// card_charged_amount stays set while the capture is live (full or partial).
	assert.strictEqual(hasLiveTender({ card_charged_amount: 855.17 }), true);
});

test('non-zero split payment rows (Cash/Zelle) → preserve', () => {
	// The incident's corrected split: CC 855.17 + Zelle 584.55, saved to DB.
	assert.strictEqual(
		hasLiveTender({ payments: [{ amount: 855.17 }, { amount: 584.55 }] }),
		true,
	);
});

test('a single non-zero Zelle row with no card charge → preserve', () => {
	assert.strictEqual(
		hasLiveTender({ card_charged_amount: 0, payments: [{ amount: 584.55 }] }),
		true,
	);
});

test('unpaid draft with zeroed profile rows → do NOT preserve (vanilla reset)', () => {
	assert.strictEqual(
		hasLiveTender({ card_charged_amount: 0, payments: [{ amount: 0 }, { amount: 0 }] }),
		false,
	);
});

test('a fresh/empty doc → do NOT preserve', () => {
	assert.strictEqual(hasLiveTender({}), false);
	assert.strictEqual(hasLiveTender(null), false);
	assert.strictEqual(hasLiveTender(undefined), false);
});

test('a VOIDED charge (card_charged_amount cleared to 0) → do NOT preserve', () => {
	// payments.api void_pos_invoice clears card_charged_amount to 0 (the Currency
	// column is NOT NULL — writing None was a past incident), so an
	// un-voided charge is exactly card_charged_amount > 0. A void with no
	// remaining payment rows must fall back to the vanilla reset.
	assert.strictEqual(
		hasLiveTender({ card_status: 'VOIDED', card_charged_amount: 0, payments: [] }),
		false,
	);
	// Defensive legacy input: null is no longer a produced state, but must
	// still read as "no live tender".
	assert.strictEqual(
		hasLiveTender({ card_status: 'VOIDED', card_charged_amount: null, payments: [] }),
		false,
	);
});

test('string/garbage amounts are coerced, not thrown', () => {
	assert.strictEqual(hasLiveTender({ card_charged_amount: '855.17' }), true);
	assert.strictEqual(hasLiveTender({ payments: [{ amount: 'x' }] }), false);
	assert.strictEqual(hasLiveTender({ payments: [null, undefined] }), false);
});

// --- edit_order resume step-list (pins the ERPNext arity contract + reset-skip) ---

test('editOrderResumeSteps threads (doctype, name) into the ERPNext step contract', async () => {
	const calls = [];
	const steps = editOrderResumeSteps(makeCtrl(calls, [{ amount: 855.17 }]), 'POS Invoice', 'INV-1');
	assert.strictEqual(steps.length, 6, 'mirrors ERPNext edit_order: 6 steps');
	for (const s of steps) await s();
	// The arity contract that finding #1 worried about: doctype→make_invoice_frm,
	// (doctype,name)→sync_draft_invoice_to_frm, name→frm.refresh. A future ERPNext
	// bump that changes the call shape breaks this test instead of prod.
	assert.deepStrictEqual(calls[0], ['make_invoice_frm', 'POS Invoice']);
	assert.deepStrictEqual(calls[1], ['sync_draft_invoice_to_frm', 'POS Invoice', 'INV-1']);
	assert.deepStrictEqual(calls[2], ['frm.refresh', 'INV-1']);
	assert.deepStrictEqual(calls[calls.length - 2], ['cart.load_invoice']);
	assert.deepStrictEqual(calls[calls.length - 1], ['toggle_components', true]);
});

test('editOrderResumeSteps SKIPS reset_mode_of_payments when tender is live', async () => {
	const calls = [];
	const steps = editOrderResumeSteps(makeCtrl(calls, [{ amount: 855.17 }]), 'POS Invoice', 'INV-1');
	for (const s of steps) await s();
	assert.ok(
		!calls.some((c) => c[0] === 'frm.call' && c[1] === 'reset_mode_of_payments'),
		'a held/paid order must not reset its recorded tender',
	);
});

test('editOrderResumeSteps CALLS reset_mode_of_payments for an unpaid draft', async () => {
	const calls = [];
	const steps = editOrderResumeSteps(makeCtrl(calls, [{ amount: 0 }]), 'POS Invoice', 'INV-2');
	for (const s of steps) await s();
	assert.ok(
		calls.some((c) => c[0] === 'frm.call' && c[1] === 'reset_mode_of_payments'),
		'an unpaid draft keeps ERPNext vanilla reset behaviour',
	);
});

// --- installClassHook: the WIRING (a blind spot of the old tests). The old finite tryPatch
//     retry raced the lazily-loaded POS classes and left the wraps dead code. This
//     must engage deterministically whenever ns[key] is assigned, mirroring ERPNext:
//     frappe.provide("erpnext.PointOfSale") then plain assignments
//     `erpnext.PointOfSale.Controller = class {...}` (pos_controller.js:1) and
//     `erpnext.PointOfSale.Payment = class {...}` (pos_payment.js:2). ---

test('installClassHook wires a class defined AFTER install (lazy load — the bug)', () => {
	const ns = {};                    // erpnext.PointOfSale before the POS bundle loads
	let patched = null;
	const engaged = installClassHook(ns, 'Controller', (C) => { patched = C; });
	assert.strictEqual(engaged, true);
	assert.strictEqual(patched, null, 'not patched until the class is defined');
	class FakeController {}
	ns.Controller = FakeController;   // ERPNext lazily defines it later
	assert.strictEqual(patched, FakeController, 'the trap must fire on assignment — no retry window');
	assert.strictEqual(ns.Controller, FakeController, 'get returns the real class');
});

test('installClassHook wires a class ALREADY defined (warm path)', () => {
	class FakeController {}
	const ns = { Controller: FakeController };
	let patched = null;
	installClassHook(ns, 'Controller', (C) => { patched = C; });
	assert.strictEqual(patched, FakeController, 'an already-loaded class is patched immediately');
});

// Payment key gets the SAME four-state coverage as Controller (warm / cold /
// falsy-ns / applyPatch-throw) so the generalized hook is proven identical per key.
test('installClassHook wires a Payment defined AFTER install (lazy load)', () => {
	const ns = {};
	let payment = null;
	installClassHook(ns, 'Payment', (P) => { payment = P; });
	class FakePayment {}
	ns.Payment = FakePayment;
	assert.strictEqual(payment, FakePayment, 'the Payment class trap fires on its own assignment');
});

test('installClassHook wires a Payment ALREADY defined (warm path)', () => {
	class FakePayment {}
	const ns = { Payment: FakePayment };
	let payment = null;
	installClassHook(ns, 'Payment', (P) => { payment = P; });
	assert.strictEqual(payment, FakePayment, 'an already-loaded Payment is patched immediately');
});

test('installClassHook is a no-op on a falsy namespace for the Payment key', () => {
	assert.strictEqual(installClassHook(null, 'Payment', () => {}), false);
	assert.strictEqual(installClassHook(undefined, 'Payment', () => {}), false);
});

test('installClassHook Payment applyPatch throwing never breaks the assignment', () => {
	const ns = {};
	installClassHook(ns, 'Payment', () => { throw new Error('boom'); });
	class FakePayment {}
	assert.doesNotThrow(() => { ns.Payment = FakePayment; });
	assert.strictEqual(ns.Payment, FakePayment);
});

test('installClassHook is a no-op on a falsy namespace (degrades, does not throw)', () => {
	assert.strictEqual(installClassHook(null, 'Controller', () => {}), false);
	assert.strictEqual(installClassHook(undefined, 'Controller', () => {}), false);
});

test('installClassHook applyPatch throwing never breaks the assignment', () => {
	const ns = {};
	installClassHook(ns, 'Controller', () => { throw new Error('boom'); });
	class FakeController {}
	assert.doesNotThrow(() => { ns.Controller = FakeController; });
	assert.strictEqual(ns.Controller, FakeController);
});

// Two patches on the SAME lazily-loaded class (recycle bin + draft-status chips
// both wrap PastOrderList). A second defineProperty used to replace the first
// trap, so only the last installed patch ever ran.
test('installClassHook: two lazy hooks on one key both fire, in install order', () => {
	const ns = {};
	const fired = [];
	assert.strictEqual(installClassHook(ns, 'PastOrderList', (C) => fired.push(['bin', C])), true);
	assert.strictEqual(installClassHook(ns, 'PastOrderList', (C) => fired.push(['status', C])), true);
	class FakeList {}
	ns.PastOrderList = FakeList;
	assert.deepStrictEqual(fired, [['bin', FakeList], ['status', FakeList]]);
	assert.strictEqual(ns.PastOrderList, FakeList);
});

test('installClassHook: a throwing hook does not stop the next one', () => {
	const ns = {};
	let second = null;
	installClassHook(ns, 'PastOrderList', () => { throw new Error('boom'); });
	installClassHook(ns, 'PastOrderList', (C) => { second = C; });
	class FakeList {}
	assert.doesNotThrow(() => { ns.PastOrderList = FakeList; });
	assert.strictEqual(second, FakeList);
});

test('installClassHook: a hook installed after the class arrived is applied at once', () => {
	const ns = {};
	const fired = [];
	installClassHook(ns, 'PastOrderList', () => fired.push('early'));
	class FakeList {}
	ns.PastOrderList = FakeList;
	installClassHook(ns, 'PastOrderList', () => fired.push('late'));
	assert.deepStrictEqual(fired, ['early', 'late']);
});

// --- Draft status mapping shared by the cart badge and Recent Orders ---------
const { form4473State, draftPaymentSummary } = require('../../public/js/pos_tender_guard.js');

test('form4473State: three levels', () => {
	assert.strictEqual(form4473State('Completed', 'Proceed'), 'complete');
	assert.strictEqual(form4473State('Pending', 'N/A'), 'awaiting');
	// A finished form whose NICS did not clear is not complete — and must never
	// read as "not started" (the cart badge's old grey fall-through).
	for (const nics of ['Delayed', 'Denied', 'Cancelled', 'N/A', '']) {
		assert.strictEqual(form4473State('Completed', nics), 'awaiting', nics);
	}
	assert.strictEqual(form4473State('N/A', 'N/A'), 'not_started');
	assert.strictEqual(form4473State(undefined, undefined), 'not_started');
});

test('draftPaymentSummary: the payment screen pre-fill is entered, never confirmed', () => {
	const s = draftPaymentSummary(
		{ due: 1000, charged: 0, held: 0, paid_amount: 1000,
		  payments: [{ mode_of_payment: 'Cash', amount: 1000 }] },
		{ cardMop: 'Credit Card', cardProcessorOn: true });
	assert.deepStrictEqual(s.confirmed, { state: 'none', paid: 0, due: 1000 });
	assert.deepStrictEqual(s.entered, [{ mode: 'Cash', amount: 1000 }]);
});

test('draftPaymentSummary: a card deposit is partial; the card row is not repeated as entered', () => {
	const s = draftPaymentSummary(
		{ due: 3084.04, charged: 712.25, held: 0, card_status: 'CHARGED',
		  payments: [{ mode_of_payment: 'Credit Card', amount: 3084.04 }] },
		{ cardMop: 'Credit Card', cardProcessorOn: true });
	assert.deepStrictEqual(s.confirmed, { state: 'partial', paid: 712.25, due: 3084.04 });
	assert.deepStrictEqual(s.entered, []);
});

test('draftPaymentSummary: card captures covering the total read paid (half-cent tolerance)', () => {
	const s = draftPaymentSummary({ due: 718.78, charged: 718.776 }, { cardProcessorOn: true });
	assert.strictEqual(s.confirmed.state, 'paid');
});

test('draftPaymentSummary: a held order counts its recorded split, cash included', () => {
	const s = draftPaymentSummary(
		{ due: 1439.72, charged: 855.17, held: 1, paid_amount: 1439.72,
		  payments: [{ mode_of_payment: 'Credit Card', amount: 855.17 }, { mode_of_payment: 'Zelle', amount: 584.55 }] },
		{ cardMop: 'Credit Card', cardProcessorOn: true });
	assert.deepStrictEqual(s.confirmed, { state: 'paid', paid: 1439.72, due: 1439.72 });
	assert.deepStrictEqual(s.entered, [], 'held tender is confirmed, not listed again');
});

test('draftPaymentSummary: a web-order pickup ticket paid on WooCommerce reads paid', () => {
	// The pickup path (ffl_woo_sync pickup.py) opens the ticket with the Woo
	// tender as its payment row and no card capture; the counter must not be
	// told that money is "entered · not confirmed".
	const s = draftPaymentSummary(
		{ due: 962.34, charged: 0, held: 0, paid_online: true, paid_amount: 962.34,
		  payments: [{ mode_of_payment: 'WooCommerce', amount: 962.34 }] },
		{ cardMop: 'Credit Card', cardProcessorOn: true });
	assert.deepStrictEqual(s.confirmed, { state: 'paid', paid: 962.34, due: 962.34 });
	assert.deepStrictEqual(s.entered, [], 'the Woo tender is confirmed, not listed again');
});

test('draftPaymentSummary: a counter pickup ticket reads its ORDER\'s money — deposit, unpaid, paid', () => {
	// The $0 ticket hands a gun over for a Sales Invoice; the chip must speak
	// for that invoice (terminal deposit / Record Payment), never the ticket's
	// zero rows.
	const opts = { cardMop: 'Credit Card', cardProcessorOn: true };
	const deposit = draftPaymentSummary({ due: 0, charged: 0, held: 0, paid_amount: 0,
		payments: [{ mode_of_payment: 'Cash', amount: 0 }],
		counter_order: { name: 'SINV-1', total: 650, outstanding: 450 } }, opts);
	assert.deepStrictEqual(deposit.confirmed, { state: 'partial', paid: 200, due: 650, counter: true });
	assert.deepStrictEqual(deposit.entered, []);
	const unpaid = draftPaymentSummary({ due: 0, counter_order: { name: 'SINV-2', total: 650, outstanding: 650 } }, opts);
	assert.deepStrictEqual(unpaid.confirmed, { state: 'none', paid: 0, due: 650, counter: true });
	const paid = draftPaymentSummary({ due: 0, counter_order: { name: 'SINV-3', total: 650, outstanding: 0 } }, opts);
	assert.deepStrictEqual(paid.confirmed, { state: 'paid', paid: 650, due: 650, counter: true });
});

test('draftPaymentSummary: a held order whose cart grew afterwards is partial', () => {
	const s = draftPaymentSummary({ due: 1500, charged: 0, held: 1, paid_amount: 1439.72 }, {});
	assert.deepStrictEqual(s.confirmed, { state: 'partial', paid: 1439.72, due: 1500 });
});

test('draftPaymentSummary: a card instruction still on the terminal is in progress', () => {
	const s = draftPaymentSummary({ due: 500, charged: 0, card_status: 'IN_PROGRESS' }, {});
	assert.strictEqual(s.confirmed.state, 'in_progress');
	const split = draftPaymentSummary({ due: 3084.04, charged: 712.25, card_status: 'IN_PROGRESS' }, {});
	assert.deepStrictEqual(split.confirmed, { state: 'in_progress', paid: 712.25, due: 3084.04 }, 'the first card is carried');
});

test('draftPaymentSummary: card processor off → a typed card row is entered like cash', () => {
	const s = draftPaymentSummary(
		{ due: 500, charged: 0, payments: [{ mode_of_payment: 'Credit Card', amount: 200 }, { mode_of_payment: 'Cash', amount: 50 }] },
		{ cardMop: 'Credit Card', cardProcessorOn: false });
	assert.deepStrictEqual(s.entered, [{ mode: 'Credit Card', amount: 200 }, { mode: 'Cash', amount: 50 }]);
});

test('draftPaymentSummary: rows of one mode are summed; zero and junk rows ignored; nothing due → no confirmed', () => {
	const s = draftPaymentSummary(
		{ due: 0, payments: [{ mode_of_payment: 'Cash', amount: 20 }, { mode_of_payment: 'Cash', amount: '5' },
		  { mode_of_payment: 'Zelle', amount: 0 }, null, { amount: 9 }] },
		{ cardProcessorOn: true });
	assert.strictEqual(s.confirmed, null);
	assert.deepStrictEqual(s.entered, [{ mode: 'Cash', amount: 25 }]);
	assert.deepStrictEqual(draftPaymentSummary(null, null), { confirmed: null, entered: [] });
});

// --- applyHeldTenderCheckoutGuard: entering the payment screen runs
//     ERPNext set_default_payment, which zeroes non-default payment rows when
//     set_grand_total_to_default_mop is on and the preserved split has no default
//     (Cash) row. Disabling frm.set_default_payment for a held/paid order skips that,
//     so the recorded split survives to the payment screen. ---

test('applyHeldTenderCheckoutGuard disables default-payment fill for a live-tender order', () => {
	const frm = { set_default_payment: 1, doc: { card_charged_amount: 855.17 } };
	const guarded = applyHeldTenderCheckoutGuard(frm);
	assert.strictEqual(guarded, true);
	assert.strictEqual(frm.set_default_payment, 0, 'set_default_payment must be disabled so the split is not zeroed');
});

test('applyHeldTenderCheckoutGuard leaves an unpaid order untouched (vanilla default fill)', () => {
	const frm = { set_default_payment: 1, doc: { payments: [{ amount: 0 }] } };
	const guarded = applyHeldTenderCheckoutGuard(frm);
	assert.strictEqual(guarded, false);
	assert.strictEqual(frm.set_default_payment, 1, 'an unpaid order keeps ERPNext default-payment behaviour');
});

test('applyHeldTenderCheckoutGuard tolerates a missing frm/doc', () => {
	assert.strictEqual(applyHeldTenderCheckoutGuard(null), false);
	assert.strictEqual(applyHeldTenderCheckoutGuard({}), false);
});

// --- Payment-screen primary button: say what the click does -----------------
// Under charge-before-4473 the same button charges-and-holds (4473 pending) or
// submits (4473 cleared). The label must follow; once the money is in IN FULL
// the button goes quiet (never dead) until the 4473 lands.
const { isFullyTendered, balanceDue, hasHeldPayment, submitButtonState } = require('../../public/js/pos_tender_guard.js');

test('isFullyTendered: paid covers rounded_total (else grand_total), half-cent tolerance', () => {
	assert.strictEqual(isFullyTendered({ grand_total: 535.84, paid_amount: 535.84 }), true);
	assert.strictEqual(isFullyTendered({ grand_total: 535.84, paid_amount: 535.836 }), true);
	assert.strictEqual(isFullyTendered({ grand_total: 535.84, paid_amount: 500 }), false);
	assert.strictEqual(isFullyTendered({ grand_total: 535.84, rounded_total: 536, paid_amount: 535.84 }), false);
	assert.strictEqual(isFullyTendered({ grand_total: 535.84, rounded_total: 536, paid_amount: 536 }), true);
	assert.strictEqual(isFullyTendered({}), true);   // nothing due
	assert.strictEqual(isFullyTendered({ grand_total: 100.4, paid_amount: 100, write_off_amount: 0.4 }), true);
	assert.strictEqual(isFullyTendered({ grand_total: 100.4, paid_amount: 100, write_off_amount: 0.2 }), false);
	assert.strictEqual(balanceDue({ grand_total: 400, rounded_total: 400, paid_amount: 200 }), 200);
});

test('hasHeldPayment: only the held stamp counts', () => {
	// ERPNext pre-fills the default tender row with the grand total when the
	// payment screen opens — no money has moved yet.
	assert.strictEqual(hasHeldPayment({ payments: [{ amount: 535.84 }] }), false);
	// First card of a multi-card split: money captured, balance still owed —
	// the loop must stay reachable, so this is NOT held.
	assert.strictEqual(hasHeldPayment({ card_charged_amount: 300, card_status: 'PARTIAL' }), false);
	assert.strictEqual(hasHeldPayment({}), false);
	assert.strictEqual(hasHeldPayment({ ffl_payment_held: 1 }), true);
	assert.strictEqual(hasHeldPayment({ ffl_payment_held: '1' }), true);
	// Voided: stamp cleared server-side → not held.
	assert.strictEqual(hasHeldPayment({ card_charged_amount: 0, ffl_payment_held: 0 }), false);
});

test('submitButtonState: no firearm / return / cleared → vanilla complete', () => {
	assert.deepStrictEqual(submitButtonState({}, { hasFirearm: false, cleared: false }),
		{ key: 'complete', quiet: false });
	assert.strictEqual(submitButtonState({ is_return: 1 }, { hasFirearm: true, cleared: false }).key,
		'complete');
	assert.deepStrictEqual(submitButtonState({ ffl_payment_held: 1 }, { hasFirearm: true, cleared: true }),
		{ key: 'complete', quiet: false });
});

test('submitButtonState: firearm, 4473 pending, nothing held → pay', () => {
	assert.deepStrictEqual(
		submitButtonState({ payments: [{ amount: 535.84 }] }, { hasFirearm: true, cleared: false }),
		{ key: 'pay', quiet: false });
	// PARTIAL capture is still "pay": the balance is owed and the click resumes the loop.
	assert.strictEqual(
		submitButtonState({ card_charged_amount: 300 }, { hasFirearm: true, cleared: false }).key,
		'pay');
});

test('submitButtonState: firearm, 4473 pending, held → quiet but never disabled', () => {
	const st = submitButtonState({ ffl_payment_held: 1 }, { hasFirearm: true, cleared: false });
	assert.deepStrictEqual(st, { key: 'held', quiet: true });
	assert.strictEqual('disabled' in st, false);
});

// A past invoice: ERPNext re-priced the trade-in credit line from the
// credit item's Item Price (-869) after it was pinned — the cart showed +180.
// lockedRowFix is what the POS/Sales Invoice Item events put back: silent
// pricing-field resets plus (at most) one rate set_value.
test('lockedRowFix: a drifted locked line goes back to exactly its lock', () => {
	// The incident row: list price -869, a derived +1049 margin, rate +180.
	assert.deepStrictEqual(
		lockedRowFix({ ffl_locked_rate: -180, rate: 180, price_list_rate: -869,
			margin_type: 'Amount', margin_rate_or_amount: 1049 }),
		{ silent: { price_list_rate: 0, margin_type: '', margin_rate_or_amount: 0 }, rate: -180 });
	// An other-item pricing rule pushed a discount onto it.
	assert.deepStrictEqual(
		lockedRowFix({ ffl_locked_rate: -180, rate: -162, price_list_rate: 0,
			discount_percentage: 10, discount_amount: -18 }),
		{ silent: { discount_percentage: 0, discount_amount: 0 }, rate: -180 });
	// A late price-list response wrote rate = price_list_rate = -869.
	assert.deepStrictEqual(
		lockedRowFix({ ffl_locked_rate: -180, rate: -869, price_list_rate: -869 }),
		{ silent: { price_list_rate: 0 }, rate: -180 });
	// Only the list price drifted (rate still right): silent fix, no set_value.
	assert.deepStrictEqual(
		lockedRowFix({ ffl_locked_rate: -180, rate: -180, price_list_rate: -180 }),
		{ silent: { price_list_rate: 0 }, rate: undefined });
	// A positive deposit row (deposit invoice) is locked the same way.
	assert.deepStrictEqual(
		lockedRowFix({ ffl_locked_rate: 300, rate: 50, price_list_rate: 50 }),
		{ silent: { price_list_rate: 0 }, rate: 300 });
	// The lock is read as a number whatever the model stored.
	assert.deepStrictEqual(lockedRowFix({ ffl_locked_rate: '-180', rate: 0 }), { silent: {}, rate: -180 });
});

test('lockedRowFix: nothing to do when exact, unlocked, or the lock is unknown', () => {
	const row = { ffl_locked_rate: -180, rate: -180, price_list_rate: 0, margin_type: '',
		margin_rate_or_amount: 0, discount_percentage: 0, discount_amount: 0 };
	assert.strictEqual(lockedRowFix(row), null);
	assert.strictEqual(lockedRowFix(Object.assign({}, row, { rate: -180.004 })), null);   // sub-cent noise
	assert.strictEqual(lockedRowFix({ item_code: 'GLOCK-17', rate: 569, price_list_rate: 569 }), null);
	assert.strictEqual(lockedRowFix({ ffl_trade_in_acquisition: 'ACQ-1', rate: -869 }), null);   // pre-fix row: no lock → the submit gate decides
	assert.strictEqual(lockedRowFix({ ffl_locked_rate: '', rate: -869 }), null);
	assert.strictEqual(lockedRowFix({ ffl_locked_rate: 0, rate: -869 }), null);   // column default on pre-fix rows
	assert.strictEqual(lockedRowFix(null), null);
});

// A past invoice follow-up: a locked line's price_list_rate is
// 0, so a tap on its $0 tile matched it and POS added a unit to it (qty 2 — a
// credit booked twice). The cart refuses any add or edit of a locked line that
// does not come from its own flow.
test('isLockedLine: the locked item by code, and any row carrying a lock', () => {
	assert.strictEqual(isLockedLine({ item_code: 'TRADE-IN-CREDIT' }), true);
	assert.strictEqual(isLockedLine({ item_code: 'GUNSMITH-DEPOSIT' }), true, 'a tile tap on the deposit item');
	assert.strictEqual(isLockedLine({ item_code: 'X', ffl_locked_rate: -50 }), true);
	assert.strictEqual(isLockedLine({ item_code: 'GLOCK-17', rate: 569, ffl_locked_rate: 0 }), false);
	assert.strictEqual(isLockedLine(null), false);
});

// ---------------------------------------------------------------------------
// Negative ticket (trade-ins exceed the goods → the store pays the customer).
// Pure decisions behind the payment screen; the screen itself is
// negative_ticket_screen.test.js. Server twin: compliance/negative_ticket.
// ---------------------------------------------------------------------------
const {
	PAYOUT_METHODS, isNegativeTicket, negativeTicketState, negativeTicketFix, payoutMethodFields, payoutSummary,
} = require('../../public/js/pos_tender_guard.js');

// A cart whose trade-in credit exceeds the goods by `short` dollars (the only way a ticket may go negative).
const neg = (short, goods = 565) => [
	{ item_code: 'GLOCK', amount: goods }, { item_code: 'TRADE-IN-CREDIT', amount: -(goods + short) }];

test('negative ticket: only a non-return below zero counts (rounded total, else grand total)', () => {
	assert.strictEqual(isNegativeTicket({ grand_total: -4885, items: neg(4885) }), true);
	assert.strictEqual(isNegativeTicket({ grand_total: -0.004, items: neg(0.004) }), false);   // rounding dust
	assert.strictEqual(isNegativeTicket({ grand_total: 0, items: neg(0) }), false);
	assert.strictEqual(isNegativeTicket({ grand_total: 286.86, items: neg(0) }), false);
	assert.strictEqual(isNegativeTicket({ grand_total: -565, is_return: 1, items: neg(565) }), false);   // a return refunds
	assert.strictEqual(isNegativeTicket({ grand_total: '-12.5', items: neg(12.5) }), true);
	assert.strictEqual(isNegativeTicket(undefined), false);
});

test('negative ticket: the server\'s rule, mirrored — only trade-in credit may take a ticket below zero', () => {
	const ok = { grand_total: -100, items: neg(100) };
	assert.strictEqual(isNegativeTicket(ok), true);
	// no credit row: negative from a negative price → the ordinary screen (and the server's refusal)
	assert.strictEqual(isNegativeTicket({ grand_total: -100, items: [
		{ item_code: 'GLOCK', amount: 500 }, { item_code: 'SVC', amount: -600 }] }), false);
	assert.strictEqual(isNegativeTicket({ grand_total: -100 }), false);
	// a credit AND another negative row
	assert.strictEqual(isNegativeTicket({ grand_total: -100, items: [
		{ item_code: 'GLOCK', amount: 500 }, { item_code: 'SVC', amount: -50 },
		{ item_code: 'TRADE-IN-CREDIT', amount: -600 }] }), false);
	// a header discount
	assert.strictEqual(isNegativeTicket({ ...ok, discount_amount: 5 }), false);
	// shortfall beyond the credit (e.g. tax or a fee pushed the total further than the credit)
	assert.strictEqual(isNegativeTicket({ grand_total: -700, items: neg(100) }), false);
	assert.strictEqual(isNegativeTicket({ grand_total: -665.004, items: neg(100) }), true);   // half a cent of slack
	// not valid → no payout screen state either
	assert.strictEqual(negativeTicketState({ grand_total: -100, items: [{ item_code: 'GLOCK', amount: 500 },
		{ item_code: 'SVC', amount: -600 }] }).negative, false);
});

test('negative ticket: the payout is the rounded total when the profile rounds, like the server', () => {
	const doc = { grand_total: -4885.4, rounded_total: -4885, items: neg(4885.4), ffl_trade_in_payout_method: 'Cash' };
	assert.strictEqual(negativeTicketState(doc).due, 4885);
	assert.strictEqual(negativeTicketState({ ...doc, rounded_total: 0 }).due, 4885.4);   // rounding off: grand total
	assert.strictEqual(payoutSummary({ ...doc, docstatus: 1 }).due, 4885);
});

test('negative ticket: payout methods mirror the server (Cash/Zelle/Check/ACH, no card)', () => {
	assert.deepStrictEqual(PAYOUT_METHODS, ['Cash', 'Zelle', 'Check', 'ACH']);
});

test('negativeTicketState: example — pays $4,885.00, Cash needs no reference', () => {
	const s = negativeTicketState({ grand_total: -4885, items: neg(4885), ffl_trade_in_payout_method: 'Cash' });
	assert.deepStrictEqual(s, {
		negative: true, due: 4885, method: 'Cash', reference: '', needsReference: false, problem: null, ok: true,
	});
});

test('negativeTicketState: due is the shortfall to the cent', () => {
	assert.strictEqual(negativeTicketState({ grand_total: -4885.1234, items: neg(4885.1234), ffl_trade_in_payout_method: 'Cash' }).due, 4885.12);
	assert.strictEqual(negativeTicketState({ grand_total: -0.5, items: neg(0.5), ffl_trade_in_payout_method: 'Cash' }).due, 0.5);
});

test('negativeTicketState: a non-Cash method without a reference blocks Complete', () => {
	['Zelle', 'Check', 'ACH'].forEach((m) => {
		const s = negativeTicketState({ grand_total: -100, items: neg(100), ffl_trade_in_payout_method: m });
		assert.strictEqual(s.problem, 'reference', m);
		assert.strictEqual(s.ok, false);
		assert.strictEqual(s.needsReference, true);
	});
	// whitespace is not a reference
	assert.strictEqual(negativeTicketState({
		grand_total: -100, items: neg(100), ffl_trade_in_payout_method: 'Zelle', ffl_trade_in_payout_reference: '   ' }).problem, 'reference');
	const ok = negativeTicketState({
		grand_total: -100, items: neg(100), ffl_trade_in_payout_method: 'Check', ffl_trade_in_payout_reference: ' 1042 ' });
	assert.strictEqual(ok.ok, true);
	assert.strictEqual(ok.reference, '1042');
});

test('negativeTicketState: no / unknown method blocks (card is not a payout method)', () => {
	assert.strictEqual(negativeTicketState({ grand_total: -100, items: neg(100) }).problem, 'method');
	assert.strictEqual(negativeTicketState({ grand_total: -100, items: neg(100), ffl_trade_in_payout_method: 'Credit Card' }).problem, 'method');
	assert.strictEqual(negativeTicketState({ grand_total: -100, items: neg(100), ffl_trade_in_payout_method: 'Credit Card' }).needsReference, false);
});

test('negativeTicketState: card money already captured blocks (the server refuses a tender here)', () => {
	const s = negativeTicketState({
		grand_total: -100, items: neg(100), ffl_trade_in_payout_method: 'Cash', card_charged_amount: 40 });
	assert.strictEqual(s.problem, 'card');
	assert.strictEqual(s.ok, false);
});

test('negativeTicketState: an ordinary ticket is never blocked and pays nothing', () => {
	[{ grand_total: 286.86 }, { grand_total: 0 }, { grand_total: -565, is_return: 1 }].forEach((d) => {
		const s = negativeTicketState(d);
		assert.strictEqual(s.negative, false);
		assert.strictEqual(s.ok, true);
		assert.strictEqual(s.due, 0);
		assert.strictEqual(s.problem, null);
	});
});

test('negativeTicketFix: Cash is preselected on a negative ticket, existing choice kept', () => {
	assert.deepStrictEqual(negativeTicketFix({ grand_total: -4885, items: neg(4885) }),
		{ set: { ffl_trade_in_payout_method: 'Cash' }, zeroRows: [] });
	assert.deepStrictEqual(negativeTicketFix({ grand_total: -4885, items: neg(4885), ffl_trade_in_payout_method: 'Cash' }), null);
	assert.deepStrictEqual(negativeTicketFix({ grand_total: -4885, items: neg(4885), ffl_trade_in_payout_method: 'Zelle' }), null);
});

test('negativeTicketFix: money left on a payment row is zeroed (no tender on a negative ticket)', () => {
	const fix = negativeTicketFix({
		grand_total: -4885, items: neg(4885), ffl_trade_in_payout_method: 'Cash',
		payments: [{ name: 'p1', amount: 286.86 }, { name: 'p2', amount: 0 }, { name: 'p3', amount: '5' }],
	});
	assert.deepStrictEqual(fix, { set: {}, zeroRows: ['p1', 'p3'] });
});

test('negativeTicketFix: a cart that turned positive again drops the stale payout fields', () => {
	assert.deepStrictEqual(negativeTicketFix({
		grand_total: 286.86, ffl_trade_in_payout_method: 'Zelle', ffl_trade_in_payout_reference: 'X9' }),
		{ set: { ffl_trade_in_payout_method: '', ffl_trade_in_payout_reference: '' }, zeroRows: [] });
	assert.strictEqual(negativeTicketFix({ grand_total: 286.86 }), null);
	// …and never zeroes the rows of an ordinary ticket
	assert.strictEqual(negativeTicketFix({ grand_total: 286.86, payments: [{ name: 'p', amount: 286.86 }] }), null);
});

test('payoutMethodFields: switching method clears the reference; re-picking the same keeps it', () => {
	const doc = { ffl_trade_in_payout_method: 'Zelle', ffl_trade_in_payout_reference: 'CONF123' };
	assert.deepStrictEqual(payoutMethodFields(doc, 'Cash'),
		{ ffl_trade_in_payout_method: 'Cash', ffl_trade_in_payout_reference: '' });
	assert.deepStrictEqual(payoutMethodFields(doc, 'Check'),
		{ ffl_trade_in_payout_method: 'Check', ffl_trade_in_payout_reference: '' });
	assert.deepStrictEqual(payoutMethodFields(doc, 'Zelle'), { ffl_trade_in_payout_method: 'Zelle' });
	assert.deepStrictEqual(payoutMethodFields({}, 'Bitcoin'), { ffl_trade_in_payout_method: '' });
});

test('submitButtonState: a negative ticket with a pending 4473 says "wait", never "pay"/"held"', () => {
	assert.deepStrictEqual(submitButtonState({ grand_total: -4885, items: neg(4885) }, { hasFirearm: true, cleared: false }),
		{ key: 'wait', quiet: false });
	// cleared / no firearm: plain Complete Order
	assert.strictEqual(submitButtonState({ grand_total: -4885, items: neg(4885) }, { hasFirearm: true, cleared: true }).key, 'complete');
	assert.strictEqual(submitButtonState({ grand_total: -4885, items: neg(4885) }, { hasFirearm: false, cleared: false }).key, 'complete');
	// a positive ticket is unchanged
	assert.strictEqual(submitButtonState({ grand_total: 100 }, { hasFirearm: true, cleared: false }).key, 'pay');
});

test('payoutSummary: a submitted negative ticket reports what the store paid; nothing else does', () => {
	assert.deepStrictEqual(payoutSummary({
		docstatus: 1, grand_total: -4885, items: neg(4885), ffl_trade_in_payout_method: 'Zelle', ffl_trade_in_payout_reference: ' CONF-1 ' }),
		{ due: 4885, method: 'Zelle', reference: 'CONF-1', detail: 'Zelle, CONF-1' });
	assert.strictEqual(payoutSummary({ docstatus: 1, grand_total: -4885, items: neg(4885), ffl_trade_in_payout_method: 'Cash' }).detail, 'Cash');
	assert.strictEqual(payoutSummary({ docstatus: 1, grand_total: -4885, items: neg(4885) }).detail, '');
	assert.strictEqual(payoutSummary({ docstatus: 0, grand_total: -4885, items: neg(4885) }), null);         // a draft paid nothing yet
	assert.strictEqual(payoutSummary({ docstatus: 1, grand_total: 100 }), null);
	assert.strictEqual(payoutSummary({ docstatus: 1, grand_total: -565, is_return: 1 }), null);
	assert.strictEqual(payoutSummary(null), null);
});

test('negativeTicketFix: a card already captured is NOT zeroed away — it blocks instead', () => {
	const doc = { grand_total: -100, items: neg(100), ffl_trade_in_payout_method: 'Cash', card_charged_amount: 40,
		payments: [{ name: 'p1', amount: 40 }] };
	assert.strictEqual(negativeTicketFix(doc), null);
	assert.strictEqual(negativeTicketState(doc).problem, 'card');
	// cash typed (no card) is still cleared
	assert.deepStrictEqual(negativeTicketFix({ ...doc, card_charged_amount: 0 }), { set: {}, zeroRows: ['p1'] });
});

// Counter pickup's 4473 ticket (counter_pickup.keep_ticket_free is the server side).
test('pickupTicketState: a ticket, whether this copy carries money, never a return or a sale', () => {
	const { pickupTicketState } = require('../../public/js/pos_tender_guard.js');
	const free = { ffl_counter_order: ' SI-1 ', grand_total: 0, items: [{ rate: 0 }], payments: [{ amount: 0 }] };
	assert.deepStrictEqual(pickupTicketState(free), { order: 'SI-1', carriesMoney: false });
	assert.strictEqual(pickupTicketState({ ...free, payments: [{ amount: 670 }] }).carriesMoney, true);
	assert.strictEqual(pickupTicketState({ ...free, items: [{ rate: 670 }] }).carriesMoney, true);
	assert.strictEqual(pickupTicketState({ ...free, change_amount: 670 }).carriesMoney, true);
	assert.strictEqual(pickupTicketState({ ...free, grand_total: 670 }).carriesMoney, true);
	assert.strictEqual(pickupTicketState({ ...free, is_return: 1 }), null);
	assert.strictEqual(pickupTicketState({ ...free, ffl_counter_order: '' }), null);
	assert.strictEqual(pickupTicketState(null), null);
});
