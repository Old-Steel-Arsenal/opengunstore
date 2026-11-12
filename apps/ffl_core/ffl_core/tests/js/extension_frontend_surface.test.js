// The front-end helpers docs/extensions/hooks.md promises extensions ("Shared front-end helpers"):
// each file, loaded the way the desk loads it (a browser global, no module), puts the documented
// function on window.ffl, and the page names each one.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const JS = path.join(__dirname, '..', '..', 'public', 'js');
const HOOKS_MD = path.join(__dirname, '..', '..', '..', '..', '..', 'docs', 'extensions', 'hooks.md');

function loadInBrowser(file) {
	const sandbox = {
		document: { readyState: 'complete', addEventListener() {} },
		frappe: { provide() {}, ui: { form: { on() {} } } },
		erpnext: {},
		__: (s) => s,
	};
	sandbox.window = sandbox;
	sandbox.self = sandbox;
	vm.runInNewContext(fs.readFileSync(path.join(JS, file), 'utf8'), sandbox);
	return sandbox.ffl;
}

test('ffl.aamva.parse(raw, {ids}) is on window.ffl and answers null for text that is not a license', () => {
	const ffl = loadInBrowser('aamva.js');
	assert.strictEqual(typeof ffl.aamva.parse, 'function');
	assert.strictEqual(ffl.aamva.parse('Jane Doe', { ids: true }), null);
});

test('ffl.customerFlags.check(customer, {key}) is on window.ffl and lets an empty customer through', async () => {
	const ffl = loadInBrowser('customer_flags.js');
	assert.strictEqual(typeof ffl.customerFlags.check, 'function');
	assert.strictEqual(await ffl.customerFlags.check('', { key: 'x' }), true);
});

test('hooks.md names both helpers', () => {
	const md = fs.readFileSync(HOOKS_MD, 'utf8');
	for (const name of ['ffl.aamva.parse(raw, { ids })', 'ffl.customerFlags.check(customer, { key })']) {
		assert.ok(md.includes(name), `hooks.md documents ${name}`);
	}
});
