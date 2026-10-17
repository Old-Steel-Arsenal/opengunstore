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

test('authMode: the PIN when there is one that is on, unless the password was chosen', () => {
	assert.strictEqual(ss.authMode({ has_pin: true, pin_off: false }, false), 'pin');
	assert.strictEqual(ss.authMode({ has_pin: true, pin_off: false }, true), 'password');
	assert.strictEqual(ss.authMode({ has_pin: true, pin_off: true }, false), 'password', 'five wrong PINs: the password');
	assert.strictEqual(ss.authMode({ has_pin: false }, false), 'password');
	assert.strictEqual(ss.authMode(null, false), 'password');
});

test('pinProblem mirrors staff_switch.pin_problem', () => {
	assert.strictEqual(ss.pinProblem('4827', '4827'), null);
	assert.strictEqual(ss.pinProblem('120934'), null);
	assert.strictEqual(ss.pinProblem('1212'), null, 'up and down is not a run');
	['123', '1234567', '12a4', '', null, '١٢٣٤'].forEach((p) => assert.strictEqual(ss.pinProblem(p), 'format', String(p)));
	['1111', '000000', '1234', '3456', '123456', '9876', '654321'].forEach((p) => assert.strictEqual(ss.pinProblem(p), 'trivial', p));
	assert.strictEqual(ss.pinProblem('4827', '4828'), 'mismatch');
});

test('serverMessage: frappe\'s refusal text, else null', () => {
	const body = { _server_messages: JSON.stringify([JSON.stringify({ message: 'That PIN is not right. <b>4</b> tries left.' })]) };
	assert.strictEqual(ss.serverMessage(body), 'That PIN is not right. 4 tries left.');
	assert.strictEqual(ss.serverMessage({}), null);
	assert.strictEqual(ss.serverMessage({ _server_messages: 'junk' }), null);
	assert.strictEqual(ss.serverMessage(null), null);
});
