'use strict';
// The POS Trade-in dialog: picking an Item filed before fills the gun's maker / model / caliber / type from it
// (ffl.tradeInModelFill in ffl_core.bundle.js; the picker rows come from receive_goods.search_items). The bundle is
// desk code that does not load under Node, so the helper is run alone.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const BUNDLE = fs.readFileSync(path.join(__dirname, '../../public/js/ffl_core.bundle.js'), 'utf8');
const [helper] = BUNDLE.match(/ffl\.TRADE_IN_MODEL_FIELDS = [\s\S]*?\n};\n/);
const ctx = { ffl: {} };
vm.runInNewContext(helper, ctx);
// Out of the vm realm into this one, so deepStrictEqual compares plain arrays.
const fill = (item) => JSON.parse(JSON.stringify(ctx.ffl.tradeInModelFill(item)));

test('a picked model fills the firearm fields', () => {
	const row = { item_code: 'GLOCK19-USED', manufacturer: 'Glock', model_name: '19', caliber: '9mm',
		firearm_type: 'Pistol', importer: 'X' };
	assert.deepStrictEqual(fill(row), [
		['manufacturer', 'Glock'], ['model_name', '19'], ['caliber', '9mm'], ['firearm_type', 'Pistol']]);
});

test('blank facts leave the typed values alone, and no pick fills nothing', () => {
	assert.deepStrictEqual(fill({ item_code: 'X', manufacturer: '', model_name: null, caliber: '45 ACP' }),
		[['caliber', '45 ACP']]);
	assert.deepStrictEqual(fill(null), []);
});

test('the dialog applies the fill on pick', () => {
	assert.match(BUNDLE, /ffl\.tradeInModelFill\(item\)\.forEach\(\(\[f, v\]\) => dialog\.set_value\(f, v\)\)/);
});
