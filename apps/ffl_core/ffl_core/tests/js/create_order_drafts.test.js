'use strict';
// Create Order drafts: a line saved by Save Draft (coPayloadItem) comes back on resume
// (coDraftLine) on the same fulfilment channel. A pre-order line resumed as a local line
// would be submitted through create_order: an invoice on the order date, no pre-order slot taken.
const test = require('node:test');
const assert = require('node:assert');
const { coPayloadItem, coDraftLine } = require('../../ffl_core/page/create_order/create_order.js');

const roundTrip = (line) => coDraftLine(JSON.parse(JSON.stringify(coPayloadItem(line))));

test('a pre-order line resumes as a pre-order line, with its ETA', () => {
	for (const isFirearm of [false, true]) {
		const back = roundTrip({ item_code: 'OPTIC', item_name: 'Optic', is_firearm: isFirearm, qty: isFirearm ? 1 : 2,
			rate: 300, serial: null, channel: 'preorder', channel_confirmed: true, eta: '2027-01-15' });
		assert.strictEqual(back.channel, 'preorder', `firearm=${isFirearm}`);
		assert.strictEqual(back.channel_confirmed, true);
		assert.strictEqual(back.eta, '2027-01-15');
		assert.strictEqual(back.qty, isFirearm ? 1 : 2);
		assert.strictEqual(back.rate, 300);
		assert.strictEqual(back.is_firearm, isFirearm);
	}
});

test('a pre-order line is sent without a fulfilment source', () => {
	const item = coPayloadItem({ item_code: 'OPTIC', qty: 1, rate: 300, channel: 'preorder', channel_confirmed: true });
	assert.strictEqual(item.source, undefined);
	assert.deepStrictEqual(item.serials, []);
});

test('other lines keep their channels as before', () => {
	const local = roundTrip({ item_code: 'BOX', item_name: 'Box', qty: 3, rate: 10, channel: 'local', channel_confirmed: true,
		storage_location: 'A1' });
	assert.strictEqual(local.channel, undefined, 'a local line is born local by _addLine');
	assert.strictEqual(local.storage_location, 'A1');
	const ds = roundTrip({ item_code: 'DS', item_name: 'DS', qty: 1, rate: 50, channel: 'dropship', channel_confirmed: true,
		distributor: 'RSR', sku: 'X1' });
	assert.deepStrictEqual([ds.channel, ds.channel_confirmed, ds.distributor, ds.sku], ['dropship', true, 'RSR', 'X1']);
	const legacy = coDraftLine({ item_code: 'DS', qty: 1, rate: 5, source: 'dropship', dropship_distributor: 'RSR' });
	assert.deepStrictEqual([legacy.channel, legacy.channel_confirmed, legacy.distributor], ['dropship', true, 'RSR']);
});
