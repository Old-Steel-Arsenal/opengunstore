'use strict';
// This computer's card reader: the rule that decides, before a terminal charge, whether the
// counter is asked which reader to use. Never fall back to some reader quietly — it may be on
// another counter — and never ask when there is nothing to choose.
const { test } = require('node:test');
const assert = require('node:assert/strict');

const cr = require('../../public/js/card_reader.js');

test('no readers listed: nothing to choose, the charge goes out as before', () => {
	assert.deepEqual(cr.choose([], ''), { reader: '', ask: false });
	assert.deepEqual(cr.choose(undefined, 'Front counter'), { reader: '', ask: false });
});

test('one enabled reader is used without asking', () => {
	assert.deepEqual(cr.choose(['Front counter'], ''), { reader: 'Front counter', ask: false });
	// A choice remembered for a reader since switched off: the one left is used.
	assert.deepEqual(cr.choose(['Range desk'], 'Front counter'), { reader: 'Range desk', ask: false });
});

test('several readers: this computer\'s remembered choice, else ask', () => {
	const two = ['Front counter', 'Range desk'];
	assert.deepEqual(cr.choose(two, 'Range desk'), { reader: 'Range desk', ask: false });
	assert.deepEqual(cr.choose(two, ''), { reader: null, ask: true });
	// Remembered reader switched off (no longer listed): ask again, never pick the first.
	assert.deepEqual(cr.choose(['Front counter', 'Back office'], 'Range desk'), { reader: null, ask: true });
});

test('the server refusals that make this computer forget its reader', () => {
	assert.deepEqual(cr.REFUSALS, ['CardReaderRequired', 'CardReaderUnavailable']);
});

test('changing readers starts a new charge only after a plain cancel', () => {
	assert.equal(cr.afterSwitch({ ok: true, status: 'CANCELED' }), 'restart');
	// The cancel call failed (network, server error): the charge may still be live — stop.
	assert.equal(cr.afterSwitch({}), 'stuck');
	assert.equal(cr.afterSwitch(undefined), 'stuck');
	assert.equal(cr.afterSwitch({ ok: false, status: 'IN_PROGRESS' }), 'stuck');
	// An approval won the race: recorded, the sale is paid — never charge again.
	assert.equal(cr.afterSwitch({ ok: true, reconciled: true, status: 'COMPLETED' }), 'charged');
	// Finished some other way (a decline, an amount to check): stop and look.
	assert.equal(cr.afterSwitch({ ok: false, reconciled: true, status: 'COMPLETED' }), 'settled');
	assert.equal(cr.afterSwitch({ ok: false, partial: true, reconciled: true }), 'settled');
});

// A stand-in for the desk: frappe.boot, frappe.call answering card_tender_mode, localStorage.
function desk(serverReaders, bootReaders) {
	const store = {};
	const calls = [];
	global.localStorage = {
		getItem: (k) => (k in store ? store[k] : null),
		setItem: (k, v) => { store[k] = String(v); },
		removeItem: (k) => { delete store[k]; },
	};
	global.frappe = {
		boot: { ffl_card_readers: bootReaders },
		request: { error_handlers: {} },
		call: (opts) => {
			calls.push(opts.method);
			return serverReaders === null ? Promise.reject(new Error('offline'))
				: Promise.resolve({ message: { integrated: true, readers: serverReaders } });
		},
	};
	return { store, calls };
}

test('a refusal forgets the choice and reads the readers again before the next charge', async () => {
	const { store, calls } = desk(['Front counter', 'Back office'], ['Front counter', 'Range desk', 'Back office']);
	localStorage.setItem(cr.STORAGE_KEY, 'Range desk');
	assert.equal(await cr.ensure(), 'Range desk');               // what this page was loaded with
	// The manager switched the range desk off; the server refuses it.
	frappe.request.error_handlers.CardReaderUnavailable.forEach((fn) => fn({}));
	assert.equal(store[cr.STORAGE_KEY], undefined);
	assert.deepEqual(calls, ['ffl_integrations.payments.api.card_tender_mode']);
	// ensure() waits for the fresh list: two readers left, none chosen → it would ask (pick),
	// and the picker lists only what is available now.
	await new Promise((r) => setImmediate(r));
	assert.deepEqual(cr.list(), ['Front counter', 'Back office']);
	assert.equal(cr.current(), null);
	// The other refusal does the same.
	assert.ok(frappe.request.error_handlers.CardReaderRequired.length >= 1);
});

test('the fresh list down to one reader, or none, is used without asking', async () => {
	desk(['Front counter'], ['Front counter', 'Range desk']);
	cr.refused();
	assert.equal(await cr.ensure(), 'Front counter');
	desk([], ['Front counter', 'Range desk']);
	cr.refused();
	assert.equal(await cr.ensure(), '');                          // no readers: the one device
});

test('the server unreachable: the list stays as it was', async () => {
	desk(null, ['Front counter', 'Range desk']);
	await cr.refresh();
	assert.deepEqual(cr.list(), ['Front counter', 'Range desk']);
});

test('both charge paths show the reader the server says the charge is on, and switch only after a plain cancel', () => {
	const fs = require('node:fs');
	const path = require('node:path');
	const src = fs.readFileSync(path.join(__dirname, '../../public/js/ffl_core.bundle.js'), 'utf8');
	const pos = src.slice(src.indexOf('async function runSingleCharge'), src.indexOf('// Terminal charge for a document that is NOT a POS Invoice'));
	const si = src.slice(src.indexOf('window.ffl.terminalCharge = async function'), src.indexOf('window.ffl.chargeSalesInvoice'));
	for (const body of [pos, si]) {
		assert.match(body, /if \('reader' in created\) onReader = created\.reader \|\| ''/);
		assert.match(body, /showWaitingDialog\([^]*?page\.open, onReader,/);
		assert.match(body, /cardReader\.afterSwitch\(m\)/);
		assert.doesNotMatch(body, /m\.reconciled && m\.ok/);
	}
});
