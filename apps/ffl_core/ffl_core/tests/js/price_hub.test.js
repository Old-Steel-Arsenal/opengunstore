'use strict';
// Price Hub's chart arithmetic (ffl_integrations .../page/price_hub/price_hub.js):
// round gridlines, the cost a pointer reads at a moment, and the "at its low" mark.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname,
	'../../../../ffl_integrations/ffl_integrations/ffl_integrations/page/price_hub/price_hub.js'), 'utf8');
const ctx = { module: { exports: {} }, frappe: { pages: { 'price-hub': {} } } };
vm.runInNewContext(src, ctx);
const { ticks, costAt, atLow } = ctx.module.exports;

test('gridlines are round and cover the range', () => {
	assert.deepStrictEqual(Array.from(ticks(246.1, 258.5)), [245, 250, 255, 260]);
	assert.deepStrictEqual(Array.from(ticks(0.42, 0.49)), [0.42, 0.44, 0.46, 0.48, 0.5]);
	const flat = ticks(199, 199);
	assert.ok(flat[0] <= 199 && flat[flat.length - 1] >= 199 && flat.length >= 2);
});

test('a cost holds until the next sample and is unknown in a break', () => {
	const s = { segments: [[{ t: 10, cost: 5 }, { t: 20, cost: 4 }], [{ t: 40, cost: 3 }, { t: 50, cost: 3 }]] };
	assert.strictEqual(costAt(s, 10), 5);
	assert.strictEqual(costAt(s, 19), 5);
	assert.strictEqual(costAt(s, 20), 4);
	assert.strictEqual(costAt(s, 30), null);   // between segments: not observed
	assert.strictEqual(costAt(s, 45), 3);
	assert.strictEqual(costAt(s, 5), null);
	assert.strictEqual(costAt({ segments: [[]] }, 1), null);
});

test('at its low only when the window saw the cost move', () => {
	assert.strictEqual(atLow({ last_cost: 9, low_cost: 9, high_cost: 10 }), true);
	assert.strictEqual(atLow({ last_cost: 10, low_cost: 9, high_cost: 10 }), false);
	assert.strictEqual(atLow({ last_cost: 9, low_cost: 9, high_cost: 9 }), false);
	assert.strictEqual(atLow({ last_cost: null, low_cost: null, high_cost: null }), false);
});

// Read as text (the page needs a desk to run): the two races the page guards against.
test('an Enter or a scan still opens the product when a typed search answers instead', () => {
	assert.match(src, /if \(open\) this\._openNext = true;/);
	assert.match(src, /const opening = this\._openNext;\s+this\._openNext = false;\s+if \(this\.rows\.length && opening\) this\.select/);
});

test('only the latest period asked for is drawn', () => {
	assert.match(src, /const seq = this\._detailSeq = \(this\._detailSeq \|\| 0\) \+ 1;/);
	assert.match(src, /if \(row !== this\.row \|\| seq !== this\._detailSeq\) return;/);
});
