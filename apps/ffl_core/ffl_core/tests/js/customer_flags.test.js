'use strict';
const test = require('node:test');
const assert = require('node:assert');
const cf = require('../../public/js/customer_flags.js');

const base = { name: 'C1', customer_name: 'Jane Roe', status: 'Active', banned: false, alerts: [], tags: [],
	family: { primary: null, members: 0 }, ids: [] };

test('summarize: an active customer with nothing on file says nothing', () => {
	assert.deepStrictEqual(cf.summarize(base), { blocked: false, alerts: [], chips: [] });
	assert.strictEqual(cf.stripHtml(base), '');
});

test('summarize: Banned blocks; an open override does not, and says until when', () => {
	const banned = cf.summarize({ ...base, status: 'Banned', banned: true });
	assert.strictEqual(banned.blocked, true);
	assert.deepStrictEqual(banned.chips, [{ label: 'Banned', tone: 'red' }]);
	const window = cf.summarize({ ...base, status: 'Banned', banned: false, override_until: '2026-10-16 14:35:00.123' });
	assert.strictEqual(window.blocked, false);
	assert.deepStrictEqual(window.chips, [{ label: 'Banned — override until 14:35', tone: 'red' }]);
});

test('summarize: other statuses, IDs, family, tags and alerts', () => {
	const s = cf.summarize({ ...base, status: 'Inactive', tags: ['VIP'],
		alerts: [{ message: 'Owes $40' }, { message: '' }],
		family: { primary: { name: 'C0', customer_name: 'John Roe' }, members: 0 },
		ids: [{ id_type: 'Driver License', expiry_date: '2026-10-01', expired: true },
			{ id_type: 'CCW', expiry_date: '2026-11-01', expired: false }] });
	assert.deepStrictEqual(s.alerts, ['Owes $40']);
	assert.deepStrictEqual(s.chips, [
		{ label: 'Inactive', tone: 'orange' },
		{ label: 'Driver License expired 2026-10-01', tone: 'orange' },
		{ label: 'CCW expires 2026-11-01', tone: 'yellow' },
		{ label: 'Family of John Roe', tone: 'blue' },
		{ label: 'VIP', tone: 'gray' },
	]);
	assert.deepStrictEqual(cf.summarize({ ...base, family: { primary: null, members: 3 } }).chips,
		[{ label: '3 family members', tone: 'blue' }]);
});

test('stripHtml escapes what customers and clerks typed', () => {
	const html = cf.stripHtml({ ...base, tags: ['<img src=x onerror=alert(1)>'], alerts: [{ message: '"quoted" <b>' }] });
	assert.ok(!html.includes('<img'), html);
	assert.ok(html.includes('&lt;img'), html);
	assert.ok(html.includes('&quot;quoted&quot; &lt;b&gt;'), html);
	assert.ok(html.includes('1 alert(s)'), html);
});

test('dupLabel and maskId', () => {
	assert.strictEqual(cf.dupLabel({ matched: ['email', 'id_number'] }), 'email, ID number');
	assert.strictEqual(cf.maskId('D1234567'), '••4567');
	assert.strictEqual(cf.maskId('123'), '123');
});

test('summarize: agreements on the counter panel — what to ask for and what is fine', () => {
	const s = cf.summarize({ ...base, agreements: [
		{ template: 'Range Waiver', state: 'missing', expires_on: null, agreement: null },
		{ template: 'Rental', state: 'expired', expires_on: '2026-10-01', agreement: 'AGR-1' },
		{ template: 'Class', state: 'pending', expires_on: null, agreement: 'AGR-2' },
		{ template: 'Lane Rules', state: 'expiring', expires_on: '2026-11-01', agreement: 'AGR-3' },
		{ template: 'Photo', state: 'signed', expires_on: null, agreement: 'AGR-4' },
	] });
	assert.deepStrictEqual(s.chips, [
		{ label: 'Range Waiver needed', tone: 'orange', request: { customer: 'C1', template: 'Range Waiver' } },
		{ label: 'Rental expired 2026-10-01', tone: 'orange', request: { customer: 'C1', template: 'Rental' } },
		{ label: 'Class sent, not signed', tone: 'yellow', request: { customer: 'C1', template: 'Class' } },
		{ label: 'Lane Rules expires 2026-11-01', tone: 'yellow' },
		{ label: 'Photo signed', tone: 'green' },
	]);
	assert.strictEqual(s.blocked, false);
});

test('stripHtml: an agreement to ask for is a clickable, escaped chip', () => {
	const html = cf.stripHtml({ ...base, name: 'C"1', agreements: [
		{ template: 'Range <Waiver>', state: 'missing' }, { template: 'Photo', state: 'signed' }] });
	assert.match(html, /data-ffl-agreement-customer="C&quot;1"/);
	assert.match(html, /data-ffl-agreement-template="Range &lt;Waiver&gt;"/);
	assert.match(html, /Range &lt;Waiver&gt; needed/);
	assert.match(html, /indicator-pill green" style="margin:2px 4px 0 0">Photo signed/);
	assert.strictEqual((html.match(/data-ffl-agreement-template/g) || []).length, 1);
});

test('summarize: an Optional agreement not signed reads neutral, a required one orange', () => {
	const chips = cf.summarize({ ...base, agreements: [
		{ template: 'Range Waiver', state: 'missing', requirement: 'Optional' },
		{ template: 'Lane Rules', state: 'missing', requirement: 'Required by item' },
	] }).chips;
	assert.deepStrictEqual(chips.map((c) => [c.label, c.tone]),
		[['Range Waiver not signed', 'gray'], ['Lane Rules needed', 'orange']]);
});
