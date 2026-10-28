'use strict';
const test = require('node:test');
const assert = require('node:assert');
const pa = require('../../public/js/price_approval.js');

test('shouldAsk: only something lowered, and only for someone who is no manager', () => {
	assert.strictEqual(pa.shouldAsk({ needs_approval: true, approver: false }), true);
	assert.strictEqual(pa.shouldAsk({ needs_approval: true, approver: true }), false);
	assert.strictEqual(pa.shouldAsk({ needs_approval: false, approver: false }), false);
	[null, undefined, {}].forEach((v) => assert.strictEqual(pa.shouldAsk(v), false));
});

test('reasonText: a preset, with details when given; Other needs the details', () => {
	assert.strictEqual(pa.reasonText('Price match', ''), 'Price match');
	assert.strictEqual(pa.reasonText('Price match', '  Cabela\'s $89 '), 'Price match: Cabela\'s $89');
	assert.strictEqual(pa.reasonText(pa.OTHER, ''), '');
	assert.strictEqual(pa.reasonText(pa.OTHER, ' scuffed box '), 'scuffed box');
	assert.strictEqual(pa.reasonText('', 'anything'), '');
	assert.ok(pa.REASONS.includes(pa.OTHER));
});

test('summaryRows: each line list → now, a gun with its serial, then the invoice discount', () => {
	const rows = pa.summaryRows({
		lines: [
			{ item_code: 'A', item_name: 'Holster', list_rate: 50, rate: 40 },
			{ item_code: 'G', item_name: 'Model 10', serial: 'SN1', list_rate: 900, rate: 800 },
		],
		discount: { percentage: 5, amount: 42 },
	});
	assert.deepStrictEqual(rows.map((r) => [r.what, r.from, r.to, r.kind]), [
		['Holster', 50, 40, 'line'], ['Model 10 (SN1)', 900, 800, 'line'], ['Invoice discount', null, '5%', 'discount']]);
	assert.deepStrictEqual(pa.summaryRows({ lines: [], discount: { percentage: 0, amount: 20 } }).map((r) => r.to), [20]);
	assert.deepStrictEqual(pa.summaryRows(null), []);
});

test('approveArgs: the PIN or the password, never both', () => {
	assert.deepStrictEqual(pa.approveArgs('m@x', '4826', 'pin', 'Price match'),
		{ approver: 'm@x', reason: 'Price match', pin: '4826' });
	assert.deepStrictEqual(pa.approveArgs('m@x', 'secret', 'password', 'Other'),
		{ approver: 'm@x', reason: 'Other', password: 'secret' });
});

test('checkFailedMessage: says the check failed, with the server reason when there is one', () => {
	assert.strictEqual(pa.checkFailedMessage('Not permitted'),
		"Couldn't check the prices for manager approval: Not permitted. Ask a manager to sign in, or try again.");
	assert.strictEqual(pa.checkFailedMessage(''),
		"Couldn't check the prices for manager approval. Ask a manager to sign in, or try again.");
});
