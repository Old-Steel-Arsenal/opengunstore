'use strict';
// Multi-card charge math: the pure invariants behind the POS
// charge loop and the desk per-charge void picker. These are the numbers that
// decide whether a sale may complete — they must hold outside a browser.
const { test } = require('node:test');
const assert = require('node:assert/strict');

const pc = require('../../public/js/card_charges.js');

const doc = (rows) => ({ card_charges: rows });
const charged = (amount, extra) => Object.assign(
	{ status: 'CHARGED', amount: amount }, extra || {});

test('liveChargeRows: only CHARGED rows count; row-less/legacy docs → []', () => {
	assert.equal(pc.liveChargeRows(doc([
		charged(60), { status: 'VOIDED', amount: 40 },
		{ status: 'DECLINED', amount: 0 }, { status: 'IN_PROGRESS', amount: 0 },
	])).length, 1);
	assert.deepEqual(pc.liveChargeRows({}), []);
	assert.deepEqual(pc.liveChargeRows(null), []);
});

test('sumLiveCharges: Σ of CHARGED only, garbage amounts coerced', () => {
	// Raw float sum — consumers round at the comparison edge (remainingCardBalance).
	assert.ok(Math.abs(pc.sumLiveCharges(doc([charged(60), charged('40.02')])) - 100.02) < 1e-9);
	assert.equal(pc.sumLiveCharges(doc([charged('x'), { status: 'VOIDED', amount: 50 }])), 0);
});

test('remainingCardBalance: to the cent; negative means over-captured', () => {
	assert.equal(pc.remainingCardBalance(100, doc([charged(60)])), 40);
	assert.equal(pc.remainingCardBalance(100, doc([charged(60), charged(40.02)])), -0.02);
	assert.equal(pc.remainingCardBalance(0, doc([])), 0);
});

test('isValidSlice: > 0, ≤ remaining within the repo 0.005 tolerance', () => {
	assert.equal(pc.isValidSlice(40, 40), true);
	assert.equal(pc.isValidSlice(40.004, 40), true);     // within tolerance
	assert.equal(pc.isValidSlice(40.01, 40), false);     // over
	assert.equal(pc.isValidSlice(0, 40), false);
	assert.equal(pc.isValidSlice(-5, 40), false);
	assert.equal(pc.isValidSlice('abc', 40), false);
});

// The card tile on an order with card money captured: the control holds the
// amount to charge NOW; the row keeps the card total. A Frappe-control stand-in
// (set_value fires df.onchange, set_input does not) wired by the shipped
// chargeNowControl, plus ERPNext's row-resync handler (pos_payment.js "Sales
// Invoice Payment" amount: get_value() vs row.amount, then set_value(row.amount)).
function cardTile(capturedRef, row) {
	const real = {
		value: undefined, _label: 'Credit Card', df: { label: 'Credit Card' },
		get_value() { return this.value; },
		set_input(v) { this.value = v; },
		set_value(v) { this.value = v; if (this.df.onchange) this.df.onchange.call(this); return Promise.resolve(); },
	};
	const tile = pc.chargeNowControl({
		real, charged: () => capturedRef.v, tenderTotal: () => row.amount,
		writeTotal: (total) => { row.amount = total; resync(); },
	});
	function resync() {   // ERPNext's handler, which only ever sees the facade
		if (tile.facade.get_value() != row.amount) tile.facade.set_value(row.amount);
	}
	return { real, facade: tile.facade, arm: tile.arm, resync };
}

test('chargeNowControl: inert until the tile is picked — ERPNext pushing the card total never lands in the row', () => {
	const captured = { v: 712.25 };
	const row = { amount: 712.25 };
	const { real, facade, arm } = cardTile(captured, row);
	real.set_value(712.25);          // ERPNext render: set_value(p.amount) — a card TOTAL
	facade.set_value(row.amount);    // and its resync handler, before the counter touches the tile
	assert.equal(row.amount, 712.25);
	arm();                           // tile click
	assert.equal(real.value, 0);     // re-derived from the row: nothing more to charge yet
	assert.equal(row.amount, 712.25);
});

test('chargeNowControl: typed 1,000 on $712.25 captured → tender 1,712.25, no double count', () => {
	// A past invoice.
	const captured = { v: 712.25 };
	const row = { amount: 712.25 };
	const { real, facade, arm } = cardTile(captured, row);
	arm();
	real.set_value(1000);            // keypad types today's amount
	assert.equal(row.amount, 1712.25);
	assert.equal(real.value, 1000);  // the resync handler did not push the total back in
	assert.equal(facade.get_value(), 1712.25);
	assert.equal(pc.remainingCardBalance(row.amount, doc([charged(712.25)])), 1000);
});

test('chargeNowControl: same amount again, 0, and an outside total all map cleanly', () => {
	const captured = { v: 712.25 };
	const row = { amount: 712.25 };
	const { real, facade, arm } = cardTile(captured, row);
	arm();
	real.set_value(712.25);          // charge 712.25 again → still "now", not "unchanged"
	assert.equal(row.amount, 1424.5);
	real.set_value(0);               // rest in cash
	assert.equal(row.amount, 712.25);
	facade.set_value(2000);          // ERPNext pushing a card TOTAL in
	assert.equal(real.value, 1287.75);
	assert.equal(row.amount, 2000);
});

test('chargeNowControl: a split reads the live captured total; re-arming re-derives from the row', () => {
	const captured = { v: 712.25 };
	const row = { amount: 712.25 };
	const { real, facade, arm } = cardTile(captured, row);
	arm();
	real.set_value(1000);
	captured.v = 1312.25;            // card A took 600 of the 1,000
	arm();                           // tile picked again after the repaint
	assert.equal(real.value, 400);
	assert.equal(row.amount, 1712.25);
	assert.equal(facade.get_value(), 1712.25);
	assert.equal(facade._label, 'Credit Card');
	assert.equal(facade.df.label, 'Credit Card');
});

test('buildVoidPicker: labels are index-mapped; refs never parsed from text', () => {
	const rows = [
		charged(60, { unique_reference: 'REF — WEIRD', card_summary: 'Visa •1111' }),
		{ status: 'VOIDED', amount: 10, unique_reference: 'REF-GONE' },
		charged(40, { unique_reference: 'REF-B', card_summary: 'Amex •2222' }),
	];
	const p = pc.buildVoidPicker(rows);
	assert.equal(p.labels.length, 2);                    // VOIDED row excluded
	assert.match(p.labels[0], /^1 · Visa •1111 \$60\.00$/);
	// A ref containing the label separator must still resolve exactly.
	assert.equal(p.resolve(p.labels[0]), 'REF — WEIRD');
	assert.equal(p.resolve(p.labels[1]), 'REF-B');
	assert.equal(p.resolve('junk'), '');
	assert.equal(p.resolve(''), '');
});

test('isRefundLock: only the two server sentinels; real refs/legacy markers are not locks', () => {
	assert.equal(pc.isRefundLock('UNKNOWN-OUTCOME'), true);
	assert.equal(pc.isRefundLock('PARTIAL:RF-1,RF-2'), true);
	assert.equal(pc.isRefundLock('PARTIAL:'), true);              // armed with zero refs is still a lock
	assert.equal(pc.isRefundLock('RF-REAL-123'), false);          // completed refund
	assert.equal(pc.isRefundLock('UNKNOWN'), false);              // legacy SUCCESS marker
	assert.equal(pc.isRefundLock('MULTI'), false);                // legacy multi-ref success
	assert.equal(pc.isRefundLock(''), false);
	assert.equal(pc.isRefundLock(null), false);
	assert.equal(pc.isRefundLock(undefined), false);
});

test('isUnlockableLock: UNKNOWN-OUTCOME only — PARTIAL:* must never unlock', () => {
	// PARTIAL:* is the one record of what this return already pushed back;
	// unlocking it re-arms a full-amount retry the per-card caps cannot bound.
	assert.equal(pc.isUnlockableLock('UNKNOWN-OUTCOME'), true);
	assert.equal(pc.isUnlockableLock('PARTIAL:RF-1'), false);
	assert.equal(pc.isUnlockableLock('RF-REAL-123'), false);
	assert.equal(pc.isUnlockableLock(''), false);
	assert.equal(pc.isUnlockableLock(null), false);
});
