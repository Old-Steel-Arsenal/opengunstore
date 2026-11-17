'use strict';
const test = require('node:test');
const assert = require('node:assert');
const ind = require('../../public/js/customer_list.js');

test('indicator: Banned is red and filterable', () => {
	assert.deepStrictEqual(ind.indicator({ customer_status: 'Banned' }), ['Banned', 'red', 'customer_status,=,Banned']);
});

test('indicator: Active, blank, retired and unknown fall through to Frappe (Enabled / Disabled)', () => {
	[{ customer_status: 'Active' }, { customer_status: '' }, {}, { customer_status: 'Inactive' }, { customer_status: 'Whatever' }, null]
		.forEach((doc) => assert.strictEqual(ind.indicator(doc), null));
});

test('indicator: a disabled customer still shows Disabled whatever its status', () => {
	assert.strictEqual(ind.indicator({ customer_status: 'Banned', disabled: 1 }), null);
});

test('indicator translates the label', () => {
	assert.strictEqual(ind.indicator({ customer_status: 'Banned' }, (s) => `<${s}>`)[0], '<Banned>');
});

test('install merges into ERPNext settings instead of replacing them and chains an earlier get_indicator', () => {
	const f = { listview_settings: { Customer: { add_fields: ['customer_name', 'territory', 'disabled'], other: 1 } } };
	const s = ind.install(f);
	assert.strictEqual(s, f.listview_settings.Customer);
	assert.deepStrictEqual(s.add_fields, ['customer_name', 'territory', 'disabled', 'customer_status']);
	assert.strictEqual(s.other, 1);
	assert.deepStrictEqual(s.get_indicator({ customer_status: 'Banned' }), ['Banned', 'red', 'customer_status,=,Banned']);
	assert.strictEqual(s.get_indicator({ customer_status: 'Active' }), null);

	const g = { listview_settings: { Customer: { get_indicator: () => ['Mine', 'blue', 'x,=,1'] } } };
	const t = ind.install(g);
	assert.deepStrictEqual(t.get_indicator({ customer_status: 'Active' }), ['Mine', 'blue', 'x,=,1']);
	assert.deepStrictEqual(t.get_indicator({ customer_status: 'Banned' })[1], 'red');
});

test('install creates the settings when none exist', () => {
	const f = {};
	assert.deepStrictEqual(ind.install(f).add_fields, ['customer_status', 'disabled']);
	assert.strictEqual(ind.install(null), null);
});
