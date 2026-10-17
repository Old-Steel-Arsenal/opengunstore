'use strict';
const test = require('node:test');
const assert = require('node:assert');
const ss = require('../../public/js/staff_switch.js');

test('idleMs: minutes to milliseconds, 0 / blank / junk = never', () => {
	assert.strictEqual(ss.idleMs(5), 300000);
	assert.strictEqual(ss.idleMs('2'), 120000);
	[0, '0', '', null, undefined, 'x', -3].forEach((v) => assert.strictEqual(ss.idleMs(v), 0, String(v)));
});

test('initials', () => {
	assert.strictEqual(ss.initials('Jane Roe'), 'JR');
	assert.strictEqual(ss.initials('  Cher '), 'C');
	assert.strictEqual(ss.initials('Mary Ann Smith'), 'MS');
	assert.strictEqual(ss.initials(''), '?');
});

test('pickAction: yourself closes (or unlocks when locked), anyone else signs in', () => {
	assert.strictEqual(ss.pickAction('a@x', 'a@x', false), 'close');
	assert.strictEqual(ss.pickAction('a@x', 'a@x', true), 'unlock');
	assert.strictEqual(ss.pickAction('b@x', 'a@x', false), 'login');
	assert.strictEqual(ss.pickAction('b@x', 'a@x', true), 'login');
});

test('withMenuItem: the entry goes first in the first group, once, and nothing else moves', () => {
	const item = { name: 'ffl-switch-user', label: 'Switch user' };
	const groups = [{ group: '', options: [{ name: 'settings' }, { name: 'reload' }] }, { group: '', options: [{ name: 'logout' }] }];
	const out = ss.withMenuItem(groups, item);
	assert.deepStrictEqual(out[0].options.map((o) => o.name), ['ffl-switch-user', 'settings', 'reload']);
	assert.deepStrictEqual(out[1], groups[1]);
	assert.deepStrictEqual(groups[0].options.map((o) => o.name), ['settings', 'reload'], 'input left as it was');
	assert.strictEqual(ss.withMenuItem(out, item)[0].options.length, 3, 'added once');
	assert.deepStrictEqual(ss.withMenuItem([], item), []);
	assert.strictEqual(ss.withMenuItem(undefined, item), undefined);
});
