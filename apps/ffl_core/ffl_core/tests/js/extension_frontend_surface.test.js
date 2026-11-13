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

function loadInBrowser(file, frappe) {
	const sandbox = {
		document: { readyState: 'complete', addEventListener() {} },
		frappe: frappe || { provide() {}, ui: { form: { on() {} } } },
		erpnext: {},
		__: (s) => s,
		setTimeout,
	};
	sandbox.window = sandbox;
	sandbox.self = sandbox;
	vm.runInNewContext(fs.readFileSync(path.join(JS, file), 'utf8'), sandbox);
	return sandbox.ffl;
}

// ERPNext's New Customer dialog, as far as newFromScan sees it: make_quick_entry hands the dialog to
// init_callback (or, with no quick entry, the bare doc), the dialog's onhide fires on close, and a
// save calls after_insert with the saved doc.
function quickEntryStub({ dialog = true, loading = false } = {}) {
	const calls = [];
	const later = [];   // loading: the dialog comes up when the test calls later.shift()()
	const frappe = {
		provide() {}, _from_link: { stale: true },
		ui: { form: {
			on() {},
			CustomerQuickEntryForm: class {},
			make_quick_entry(doctype, afterInsert, initCallback, doc) {
				const qe = { scans: [], onhide: () => { calls.push('stock-onhide'); },
					__fflApplyScan(raw) { this.scans.push(raw); } };
				calls.push({ doctype, doc, qe, afterInsert });
				if (loading) {
					return new Promise((resolve) => later.push(() => { initCallback(qe); resolve(qe); }));
				}
				if (dialog) initCallback(qe); else initCallback({ doctype });
				return Promise.resolve(qe);
			},
		} },
	};
	return { frappe, calls, later };
}
const tick = () => new Promise((r) => setTimeout(r, 5));

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

test('newFromScan: opens New Customer for an individual, fills it from the scan, resolves the saved name', async () => {
	const { frappe, calls } = quickEntryStub();
	const ffl = loadInBrowser('customer_quick_entry.js', frappe);
	const p = ffl.customerQuickEntry.newFromScan('RAW-1');
	assert.strictEqual(frappe._from_link, undefined);            // a stale Link target cannot take the save
	assert.strictEqual(calls[0].doctype, 'Customer');
	assert.deepStrictEqual({ ...calls[0].doc }, { doctype: 'Customer', customer_type: 'Individual' });
	assert.deepStrictEqual(calls[0].qe.scans, ['RAW-1']);
	calls[0].afterInsert({ name: 'CUST-0001' });
	calls[0].qe.onhide();
	assert.strictEqual(await p, 'CUST-0001');
	assert.ok(calls.includes('stock-onhide'));                   // the dialog's own onhide still runs
});

test('newFromScan: closed without saving resolves null', async () => {
	const { frappe, calls } = quickEntryStub();
	const ffl = loadInBrowser('customer_quick_entry.js', frappe);
	const p = ffl.customerQuickEntry.newFromScan('RAW-1');
	calls[0].qe.onhide();
	assert.strictEqual(await p, null);
});

test('newFromScan: called again while its dialog is open, re-fills that dialog and returns the same promise', async () => {
	const { frappe, calls } = quickEntryStub();
	const ffl = loadInBrowser('customer_quick_entry.js', frappe);
	const first = ffl.customerQuickEntry.newFromScan('RAW-1');
	const second = ffl.customerQuickEntry.newFromScan('RAW-2');
	assert.strictEqual(second, first);
	assert.strictEqual(calls.filter((c) => c.doctype).length, 1);
	assert.deepStrictEqual(calls[0].qe.scans, ['RAW-1', 'RAW-2']);
	calls[0].afterInsert({ name: 'CUST-0002' });
	assert.strictEqual(await first, 'CUST-0002');
	// Settled: the next call opens a new dialog.
	ffl.customerQuickEntry.newFromScan('RAW-3');
	assert.strictEqual(calls.filter((c) => c.doctype).length, 2);
});

test('newFromScan: scans while the dialog is still loading share one dialog, filled from the newest', async () => {
	const { frappe, calls, later } = quickEntryStub({ loading: true });
	const ffl = loadInBrowser('customer_quick_entry.js', frappe);
	const first = ffl.customerQuickEntry.newFromScan('RAW-1');
	const second = ffl.customerQuickEntry.newFromScan('RAW-2');
	const third = ffl.customerQuickEntry.newFromScan('RAW-3');
	assert.strictEqual(second, first);
	assert.strictEqual(third, first);
	assert.strictEqual(calls.filter((c) => c.doctype).length, 1, 'one dialog');
	later.shift()();
	await tick();
	assert.deepStrictEqual(calls[0].qe.scans, ['RAW-3']);
	calls[0].afterInsert({ name: 'CUST-0003' });
	assert.strictEqual(await first, 'CUST-0003');
});

test('newFromScan: an answered call does not clear the next one', async () => {
	const { frappe, calls } = quickEntryStub();
	const ffl = loadInBrowser('customer_quick_entry.js', frappe);
	const first = ffl.customerQuickEntry.newFromScan('RAW-1');
	calls[0].afterInsert({ name: 'CUST-A' });
	assert.strictEqual(await first, 'CUST-A');
	const second = ffl.customerQuickEntry.newFromScan('RAW-2');
	calls[0].qe.onhide();          // the first dialog's late hide settles only its own call
	await tick();
	assert.strictEqual(ffl.customerQuickEntry.newFromScan('RAW-3'), second, 'the second dialog is still the open one');
	assert.deepStrictEqual(calls[1].qe.scans, ['RAW-2', 'RAW-3']);
	calls[1].afterInsert({ name: 'CUST-B' });
	assert.strictEqual(await second, 'CUST-B');
});

test('newFromScan: "Use this customer" hides the dialog first, then answers: the name still wins', async () => {
	const { frappe, calls } = quickEntryStub();
	const ffl = loadInBrowser('customer_quick_entry.js', frappe);
	const p = ffl.customerQuickEntry.newFromScan('RAW-1');
	calls[0].qe.onhide();
	calls[0].afterInsert({ name: 'CUST-EXISTING' });
	assert.strictEqual(await p, 'CUST-EXISTING');
});

test('newFromScan: no quick-entry dialog on the site resolves null', async () => {
	const { frappe } = quickEntryStub({ dialog: false });
	const ffl = loadInBrowser('customer_quick_entry.js', frappe);
	const p = ffl.customerQuickEntry.newFromScan('RAW-1');
	await tick();
	assert.strictEqual(await p, null);
});

test('hooks.md names the helpers', () => {
	const md = fs.readFileSync(HOOKS_MD, 'utf8');
	for (const name of ['ffl.aamva.parse(raw, { ids })', 'ffl.customerFlags.check(customer, { key })',
		'ffl.customerQuickEntry.newFromScan(raw)']) {
		assert.ok(md.includes(name), `hooks.md documents ${name}`);
	}
});
