'use strict';
const test = require('node:test');
const assert = require('node:assert');
const an = require('../../public/js/assignment_notice.js');

const row = (name, extra) => Object.assign({ name, assigned_by: 'Jane Roe', reference_type: 'CRM Task',
	reference_name: 'CRM-TASK-1', title: 'Call back', description: 'Call the customer', priority: 'Medium',
	date: null, creation: '2026-11-13 09:00:00' }, extra);

test('step: waits while the screen is locked or another dialog is open, never interrupts', () => {
	const free = { open: false, locked: false, dialogs: 0 };
	assert.strictEqual(an.step([row('T1')], free), 'show');
	assert.strictEqual(an.step([row('T1')], { ...free, locked: true }), 'wait');
	assert.strictEqual(an.step([row('T1')], { ...free, dialogs: 1 }), 'wait');
	assert.strictEqual(an.step([], { ...free, locked: true }), 'none');
	assert.strictEqual(an.step(null, free), 'none');
});

test('step: an open dialog takes new rows, and closes once nothing waits', () => {
	assert.strictEqual(an.step([row('T1')], { open: true, locked: true, dialogs: 2 }), 'update');
	assert.strictEqual(an.step([], { open: true, locked: false, dialogs: 0 }), 'close');
});

test('due: throttles a look to one per gap', () => {
	assert.strictEqual(an.due(0, 1000, 60000), true, 'never looked');
	assert.strictEqual(an.due(1000, 30000, 60000), false);
	assert.strictEqual(an.due(1000, 61000, 60000), true);
});

test('merge: the server list wins; changed only when the names differ', () => {
	const shown = [row('T1')];
	let m = an.merge(shown, [row('T1'), row('T2')]);
	assert.deepStrictEqual(m.rows.map((r) => r.name), ['T1', 'T2']);
	assert.strictEqual(m.changed, true, 'a new assignment is added to the open dialog');
	m = an.merge(shown, [row('T1', { description: 'edited' })]);
	assert.strictEqual(m.changed, false);
	m = an.merge([row('T1'), row('T2')], [row('T2')]);
	assert.strictEqual(m.changed, true, 'acknowledged elsewhere drops out');
	assert.deepStrictEqual(an.merge(undefined, undefined), { rows: [], changed: false });
});

test('rowsHtml: every text is escaped, the document opens in a new tab', () => {
	const html = an.rowsHtml([row('T<1>', {
		assigned_by: '<img src=x onerror=alert(1)>', title: '"><script>x</script>',
		description: 'a & b <b>', reference_type: 'Sales Invoice', reference_name: 'SI/1 "x"', priority: 'High',
		date: '2026-11-20',
	})], { when: () => '5 minutes ago' });
	assert.ok(!/<img|<script/.test(html), html);
	assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
	assert.ok(html.includes('a &amp; b &lt;b&gt;'));
	assert.ok(html.includes('data-name="T&lt;1&gt;"'));
	assert.ok(html.includes('href="/app/sales-invoice/SI%2F1%20%22x%22" target="_blank" rel="noopener"'), html);
	assert.ok(html.includes('5 minutes ago'));
	assert.ok(html.includes('High'));
	assert.ok(!html.includes('2026-11-20'), 'no due date: Assign fills today by default');
});

test('rowsHtml: no link when the document is gone', () => {
	const html = an.rowsHtml([row('T1', { reference_type: null, reference_name: null, title: null })]);
	assert.ok(!html.includes('<a '));
	assert.ok(html.includes('Call the customer'));
});
