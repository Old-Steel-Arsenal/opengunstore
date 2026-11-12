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
