'use strict';
// The distributor order cart the Distributor Orders and Price Hub pages share
// (ffl_integrations/public/js/distributor_cart.js), and that both pages use it.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const INTEGRATIONS = path.join(__dirname, '../../../../ffl_integrations/ffl_integrations');
const read = (rel) => fs.readFileSync(path.join(INTEGRATIONS, rel), 'utf8');

function load(user = 'buyer@example.com', store = {}) {
	const ctx = {
		frappe: { session: { user }, provide: (name) => { ctx[name] = ctx[name] || {}; } },
		localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } },
	};
	vm.runInNewContext(read('public/js/distributor_cart.js'), ctx);
	return ctx.ffl_integrations.cart;
}

const row = () => ({ upc: '0001', item_name: 'Widget', map: 9, msrp: 12,
	offers: [{ distributor: 'A', sku: 'A-1', cost: 5, qty: 1 }, { distributor: 'B', sku: 'B-1', cost: 6, qty: 10 }] });

test('a line follows the best offer until the buyer picks a house', () => {
	const cart = load();
	const lines = [];
	cart.add(lines, row(), 1);
	assert.strictEqual(lines[0].pick, 'A');
	cart.add(lines, row(), 2);                 // A cannot fill 3
	assert.deepStrictEqual([lines.length, lines[0].qty, lines[0].pick], [1, 3, 'B']);
	cart.add(lines, row(), 1, 'A');            // the buyer's choice holds
	cart.add(lines, row(), 5);
	assert.deepStrictEqual([lines[0].qty, lines[0].pick, lines[0].manual], [9, 'A', true]);
});

test('adding again brings the line the offers of today', () => {
	const cart = load();
	const lines = [];
	cart.add(lines, { upc: '0001', offers: [{ distributor: 'A', sku: 'A-1', cost: 5, qty: 9 },
		{ distributor: 'C', sku: 'C-1', cost: 7, qty: 9 }] }, 1);
	// Later: A costs less, B carries it now, and this search did not name C.
	const now = { upc: '0001', map: 11, offers: [{ distributor: 'B', sku: 'B-1', cost: 4, qty: 9 },
		{ distributor: 'A', sku: 'A-1', cost: 4.5, qty: 9 }] };
	cart.add(lines, now, 1, 'B');
	assert.deepStrictEqual(lines[0].offers.map((o) => [o.distributor, o.cost]), [['B', 4], ['A', 4.5], ['C', 7]]);
	assert.strictEqual(lines[0].offers.find((o) => o.distributor === lines[0].pick).sku, 'B-1');
	assert.strictEqual(lines[0].map, 11);
});

test('taking the last one back removes the line', () => {
	const cart = load();
	const lines = [];
	cart.add(lines, row(), -1);
	assert.strictEqual(lines.length, 0);
	cart.add(lines, row(), 1, 'B');
	cart.add(lines, row(), -1);
	assert.strictEqual(lines.length, 0);
});

test('a product without a UPC is keyed by its house and SKU', () => {
	const cart = load();
	assert.strictEqual(cart.keyOf({ upc: '', offers: [{ distributor: 'A', sku: 'A-1' }] }), 'A:A-1');
});

test('the cart is kept per user and a live check does not outlive the visit', () => {
	const store = {};
	const cart = load('one@example.com', store);
	const lines = [];
	cart.add(lines, row(), 1);
	lines[0].offers[0].checked = true;
	lines[0].switched_from = 'B';
	cart.save(lines);
	const stored = store['ffl_distributor_cart:one@example.com'];
	const back = cart.load();
	assert.strictEqual(back[0].qty, 1);
	assert.strictEqual(back[0].offers[0].checked, undefined);
	assert.strictEqual(back[0].switched_from, undefined);
	// The page that ran the check keeps its marks; a page that reloads and saves again
	// stores the same text, so no other page is told the cart changed.
	assert.strictEqual(lines[0].offers[0].checked, true);
	cart.save(back);
	assert.strictEqual(store['ffl_distributor_cart:one@example.com'], stored);
	assert.strictEqual(load('two@example.com', store).load().length, 0);
});

test('unreadable storage reads as an empty cart', () => {
	assert.strictEqual(load('y', { 'ffl_distributor_cart:y': '[{"key":"k"}]' }).load().length, 0);
	assert.strictEqual(load('x', { 'ffl_distributor_cart:x': '{not json' }).load().length, 0);
});

test('both pages get the cart and keep it in step', () => {
	const hooks = read('hooks.py');
	for (const page of ['distributor-restock', 'price-hub']) {
		assert.match(hooks, new RegExp(`"${page}": "public/js/distributor_cart.js"`));
	}
	for (const src of [read('ffl_integrations/page/distributor_restock/distributor_restock.js'),
		read('ffl_integrations/page/price_hub/price_hub.js')]) {
		assert.match(src, /ffl_integrations\.cart\.load\(\)/);
		assert.match(src, /ffl_integrations\.cart\.save\(this\.cart\)/);
		assert.match(src, /on_page_show/);
		// A page that is already open hears of a change made in another tab.
		assert.match(src, /ffl_integrations\.cart\.watch\(\(\) => this\.reloadCart\(\)\)/);
	}
});
