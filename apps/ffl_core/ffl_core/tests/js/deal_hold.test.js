'use strict';
const test = require('node:test');
const assert = require('node:assert');
const dh = require('../../public/js/deal_hold.js');

test('REASONS: the three the counter picks from, Other last', () => {
	assert.deepStrictEqual(dh.REASONS, ['Customer did not come back', 'Deal owner agreed', 'Other']);
	assert.strictEqual(dh.REASONS[dh.REASONS.length - 1], dh.OTHER);
});

test('reasonText: a preset as it is; Other needs the details', () => {
	assert.strictEqual(dh.reasonText('Deal owner agreed', 'ignored'), 'Deal owner agreed');
	assert.strictEqual(dh.reasonText(dh.OTHER, '  walked in with cash '), 'walked in with cash');
	assert.strictEqual(dh.reasonText(dh.OTHER, ''), '');
	assert.strictEqual(dh.reasonText('', 'anything'), '');
});

test('overrideArgs: no manager fields for a manager signed in; else the PIN or the password', () => {
	assert.deepStrictEqual(dh.overrideArgs('SN1', 'r', true, 'm@x', '1357', 'pin'), { serial: 'SN1', reason: 'r' });
	assert.deepStrictEqual(dh.overrideArgs('SN1', 'r', false, 'm@x', '1357', 'pin'),
		{ serial: 'SN1', reason: 'r', approver: 'm@x', pin: '1357' });
	assert.deepStrictEqual(dh.overrideArgs('SN1', 'r', false, 'm@x', 'secret', 'password'),
		{ serial: 'SN1', reason: 'r', approver: 'm@x', password: 'secret' });
});
