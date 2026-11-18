'use strict';
// "Comes in options" on the Item form: the pure kernel of public/js/item_options.js —
// every combination in order, the default SKUs, and the checks that turn a cell red.
const test = require('node:test');
const assert = require('node:assert');

const K = require('../../public/js/item_options.js');

test('every combination, option order then value order', () => {
	assert.deepStrictEqual(K.combinations([{ name: 'Color', values: ['Black', 'FDE'] }, { name: 'Size', values: ['S', 'L'] }]),
		[['Black', 'S'], ['Black', 'L'], ['FDE', 'S'], ['FDE', 'L']]);
	assert.deepStrictEqual(K.combinations([{ name: 'Color', values: ['Black'] }, { name: 'Size', values: [] }]), []);
	assert.deepStrictEqual(K.combinations([]), []);
});

test('a combination is the same whatever its case or spaces', () => {
	assert.strictEqual(K.comboKey(['Black ', 'l']), K.comboKey(['black', 'L']));
	assert.notStrictEqual(K.comboKey(['Black', 'L']), K.comboKey(['L', 'Black']));
});

test('the default SKU is the item code and each value code', () => {
	assert.strictEqual(K.defaultSku('GRIP-1', ['BLACK', 'L']), 'GRIP-1-BLACK-L');
	assert.strictEqual(K.defaultSku('', ['BLACK', 'L']), 'BLACK-L');
	assert.strictEqual(K.defaultSku('GRIP-1', ['', 'L']), 'GRIP-1-L');
});

test('red cells: a missing or bad SKU, one used twice or already an item, no price', () => {
	const rows = [
		{ key: 'a', include: true, sku: 'G-BLACK', price: 10 },
		{ key: 'b', include: true, sku: 'g-black', price: 10 },
		{ key: 'c', include: true, sku: 'TAKEN', price: 10 },
		{ key: 'd', include: true, sku: 'has space', price: 0 },
		{ key: 'e', include: true, sku: '', price: '12', cost: -1, preorder: 1.5 },
		{ key: 'f', include: false, sku: '', price: '' },
		{ key: 'g', include: true, existing: true, sku: '', price: 0 },
		{ key: 'h', include: true, sku: 'G', price: 5 },
	];
	const errs = K.rowErrors(rows, { taken: new Set(['taken']), templateCode: 'G' });
	assert.deepStrictEqual(errs.a, { sku: 'Used twice' });
	assert.deepStrictEqual(errs.b, { sku: 'Used twice' });
	assert.deepStrictEqual(errs.c, { sku: 'Already an item' });
	assert.deepStrictEqual(errs.d, { sku: 'Letters, numbers and . _ / - only', price: 'Enter a price' });
	assert.deepStrictEqual(errs.e, { sku: 'Enter a SKU', cost: 'Not a price', preorder: 'A whole number' });
	assert.deepStrictEqual(errs.h, { sku: 'Already an item' });
	assert.ok(!errs.f && !errs.g, 'unticked and existing rows are not checked here');
});
