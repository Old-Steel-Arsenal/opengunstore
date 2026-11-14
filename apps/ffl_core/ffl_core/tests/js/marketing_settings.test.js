'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(
	path.join(__dirname, '../../ffl_core/doctype/marketing_settings/marketing_settings.js'), 'utf8');

// Run the doctype script against a stand-in `frappe`; returns the alerts and the call it made.
function press(message) {
	const alerts = [];
	const seen = {};
	const handlers = {};
	const frappe = {
		session: { user: 'clerk@example.com', user_email: 'clerk@example.com' },
		ui: { form: { on: (doctype, h) => { handlers[doctype] = h; } } },
		call: (opts) => { seen.opts = opts; opts.callback({ message }); },
		show_alert: (a) => alerts.push(a),
		msgprint: () => { throw new Error('unexpected msgprint'); },
	};
	const __ = (text, args) => (args || []).reduce((t, v, i) => t.replace('{' + i + '}', v), text);
	vm.runInNewContext(SOURCE, { frappe, __ });
	const buttons = [];
	handlers['Marketing Settings'].refresh({
		add_custom_button: (label, fn) => { buttons.push(fn); return { addClass: () => {} }; },
		is_dirty: () => false,
	});
	buttons[0]();
	return { alerts: JSON.parse(JSON.stringify(alerts)), seen };   // plain objects of this realm
}

test('a queued test email says queued, not sent', () => {
	const { alerts, seen } = press({ queued: true, email: 'me@example.org', status: 'Not Subscribed' });
	assert.strictEqual(seen.opts.method, 'ffl_core.api.marketing_mail.send_test_email');
	assert.deepStrictEqual(alerts, [{ message: 'Test email queued for me@example.org.', indicator: 'green' }]);
});

test('a test email that was not queued says why, in orange', () => {
	const { alerts } = press({ queued: false, email: 'me@example.org', status: 'Unsubscribed' });
	assert.deepStrictEqual(alerts, [{ message: 'Not sent: me@example.org is Unsubscribed.', indicator: 'orange' }]);
});

test('an answer without a status is still not reported as sent', () => {
	const { alerts } = press({ queued: false });
	assert.strictEqual(alerts.length, 1);
	assert.strictEqual(alerts[0].indicator, 'orange');
	assert.match(alerts[0].message, /^Not sent/);
});
