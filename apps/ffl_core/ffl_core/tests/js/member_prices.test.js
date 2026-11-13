'use strict';
const test = require('node:test');
const assert = require('node:assert');
const mp = require('../../public/js/member_prices.js');

const listed = { rate: 30, price_list_rate: 30, discount_percentage: 0, discount_amount: 0,
	ffl_member_benefit: null, ffl_membership: null, ffl_benefit_customer: null, ffl_member_rate: 0, ffl_discount_reason: null };
const member = { rate: 24, price_list_rate: 30, discount_percentage: 20, discount_amount: 6,
	ffl_member_benefit: 'b1', ffl_membership: 'MEM-1', ffl_benefit_customer: 'C1', ffl_member_rate: 24,
	ffl_discount_reason: 'Member 20% (Basic)' };

test('rowFix: a line at list price takes the member price; the record goes in silently', () => {
	const fix = mp.rowFix({ ...listed }, member);
	assert.strictEqual(fix.rate, 24);
	assert.deepStrictEqual(fix.silent, { discount_percentage: 20, discount_amount: 6, ffl_member_rate: 24,
		ffl_member_benefit: 'b1', ffl_membership: 'MEM-1', ffl_benefit_customer: 'C1', ffl_discount_reason: 'Member 20% (Basic)' });
});

test('rowFix: nothing to do when the line already reads right; blanks and nulls are the same', () => {
	assert.strictEqual(mp.rowFix({ ...member, rate: '24.0' }, member), null);
	assert.strictEqual(mp.rowFix({ ...listed, ffl_discount_reason: '' }, listed), null);
	assert.strictEqual(mp.rowFix(null, member), null);
	assert.strictEqual(mp.rowFix(member, undefined), null);
});

test('rowFix: a line that lost its member price goes back to the list price', () => {
	const fix = mp.rowFix({ ...member }, listed);
	assert.strictEqual(fix.rate, 30);
	assert.strictEqual(fix.silent.ffl_member_benefit, null);
	assert.strictEqual(fix.silent.discount_amount, 0);
});

test('plan: only the lines whose answer differs, and only lines the answer names', () => {
	const items = [{ name: 'a', ...listed }, { name: 'b', ...member }, { name: 'c', ...listed }];
	const fixes = mp.plan(items, { a: member, b: member });
	assert.deepStrictEqual(fixes.map((f) => f.row.name), ['a']);
	assert.strictEqual(fixes[0].fix.rate, 24);
	assert.deepStrictEqual(mp.plan(items, undefined), []);
	assert.deepStrictEqual(mp.plan(undefined, { a: member }), []);
});

test('changeMessage: nothing when no rate moved; says which way the total went', () => {
	assert.strictEqual(mp.changeMessage([]), null);
	const recordOnly = mp.plan([{ name: 'a', ...member, ffl_discount_reason: 'old' }], { a: member });
	assert.strictEqual(recordOnly.length, 1);
	assert.strictEqual(mp.changeMessage(recordOnly), null, 'a record field alone does not change the total');
	const gained = mp.plan([{ name: 'a', ...listed }], { a: member });
	assert.match(mp.changeMessage(gained), /changed to the member price/);
	const lost = mp.plan([{ name: 'a', ...member }], { a: listed });
	assert.match(mp.changeMessage(lost), /back to the regular price/);
	const both = mp.plan([{ name: 'a', ...listed }, { name: 'b', ...member }], { a: member, b: listed });
	assert.match(mp.changeMessage(both), /member prices were updated/);
	// A member price that moved but stays a member price (another benefit now): still the member price.
	const other = mp.plan([{ name: 'a', ...member }], { a: { ...member, rate: 20, ffl_member_rate: 20 } });
	assert.match(mp.changeMessage(other), /changed to the member price/);
});
