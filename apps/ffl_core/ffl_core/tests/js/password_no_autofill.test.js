'use strict';
// Desk password fields are marked autocomplete="new-password" so the browser
// does not fill the store's saved login into them.
const test = require('node:test');
const assert = require('node:assert');
const pna = require('../../public/js/password_no_autofill.js');

function fakeControl() {
	class ControlPassword {
		make_input() {
			const attrs = { autocomplete: 'off' };
			this.$input = { attrs, attr(k, v) { attrs[k] = v; return this; } };
			this.made = (this.made || 0) + 1;
			return 'made';
		}
	}
	return ControlPassword;
}

test('the password input is marked new-password after Frappe builds it', () => {
	const Control = fakeControl();
	pna.install(Control);
	const c = new Control();
	assert.strictEqual(c.make_input(), 'made');
	assert.strictEqual(c.$input.attrs.autocomplete, 'new-password');
});

test('installing twice wraps once', () => {
	const Control = fakeControl();
	pna.install(Control);
	pna.install(Control);
	const c = new Control();
	c.make_input();
	assert.strictEqual(c.made, 1);
});
