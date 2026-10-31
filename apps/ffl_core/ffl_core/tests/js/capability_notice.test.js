'use strict';
// ffl.capabilities: the one "not set up" notice every surface shows when the server
// says a capability is off (frappe.boot.ffl_capabilities), and nothing otherwise.
// Also the surfaces that use it, read as text: each one asks the shared component
// instead of writing its own "No matches." for an off capability.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const cap = require('../../public/js/capability_notice.js');
const { xtUpdateServiceMissing } = require('../../ffl_core/page/extensions/extensions.js');

const boot = (caps) => ({ ffl_capabilities: caps });
const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');

test('off only when the server said off', () => {
	assert.strictEqual(cap.isOff(boot({ catalog: 'off' }), 'catalog'), true);
	assert.strictEqual(cap.isOff(boot({ catalog: 'local' }), 'catalog'), false);
	assert.strictEqual(cap.isOff(boot({ catalog: 'data_service' }), 'catalog'), false);
	// No key, no boot, no capabilities (ffl_integrations absent / an old boot): never off.
	assert.strictEqual(cap.isOff(boot({}), 'catalog'), false);
	assert.strictEqual(cap.isOff({}, 'catalog'), false);
	assert.strictEqual(cap.isOff(null, 'catalog'), false);
	assert.strictEqual(cap.state(boot({ ai: 'connected' }), 'ai'), 'connected');
	assert.strictEqual(cap.state(boot({}), 'ai'), null);
});

test('the catalog notice says it is not set up and links to the settings', () => {
	const html = cap.noticeHtml('catalog');
	assert.match(html, /Distributor catalog is not set up/);
	assert.match(html, /href="\/app\/data-service-settings"/);
	assert.match(html, />Set it up</);
	assert.match(html, /data-capability="catalog"/);
	assert.match(html, /role="status"/);
	assert.doesNotMatch(html, /No matches/);
});

test('someone who cannot open the settings is told to ask a manager', () => {
	const html = cap.noticeHtml('catalog', { canSetUp: false });
	assert.match(html, /Ask a manager to set it up/);
	assert.doesNotMatch(html, /href=/);
});

test('every capability has its sentence; updates read "Update service not configured"', () => {
	for (const name of ['catalog', 'category_tree', 'ffl_national_search', 'extension_updates', 'ai']) {
		assert.ok(cap.NOTICES[name], name);
		assert.match(cap.noticeHtml(name), new RegExp(`data-capability="${name}"`));
	}
	assert.match(cap.noticeHtml('extension_updates'), /Update service not configured/);
	assert.match(cap.noticeHtml('ffl_national_search'), /National FFL search is not set up/);
});

test('translated through the given function, and escaped', () => {
	const html = cap.noticeHtml('catalog', { translate: (s) => `«${s}»` });
	assert.match(html, /«Distributor catalog is not set up»/);
	assert.match(html, /«Set it up»/);
	assert.match(cap.noticeHtml('<x>'), /data-capability="&lt;x&gt;"/);
	assert.match(cap.noticeHtml('<x>'), /This feature is not set up/);
});

test('inline variant for a dropdown row', () => {
	assert.match(cap.noticeHtml('catalog', { inline: true }), /class="ffl-cap-notice ffl-cap-inline"/);
	assert.match(cap.noticeHtml('catalog'), /class="ffl-cap-notice"/);
});

test('the extensions panel footer shows only when the payload says no update service', () => {
	assert.strictEqual(xtUpdateServiceMissing({ update_service: false }), true);
	assert.strictEqual(xtUpdateServiceMissing({ update_service: true }), false);
	assert.strictEqual(xtUpdateServiceMissing({}), false);         // an older server
	assert.strictEqual(xtUpdateServiceMissing(null), false);
});

test('the five surfaces and the panel use the shared component', () => {
	const picker = read('../../public/js/ffl_core.bundle.js');
	// counter item search + Receive Goods share the picker: the notice replaces the
	// distributor group / "No matches.", and the UPC fallback does not ask the catalog.
	assert.match(picker, /ffl\.capabilities\.notice\('catalog', \{ inline: true \}\)/);
	assert.match(picker, /catalogOff\(\)\) \{\s*\/\/ No catalog to ask/);
	const restock = read('../../../../ffl_integrations/ffl_integrations/ffl_integrations/page/distributor_restock/distributor_restock.js');
	assert.match(restock, /ffl\.capabilities\.notice\('catalog'\)/);
	const hubPage = read('../../../../ffl_integrations/ffl_integrations/ffl_integrations/page/distributor_hub/distributor_hub.js');
	assert.match(hubPage, /ffl\.capabilities\.notice\('catalog'\)/);
	assert.match(read('../../public/js/ffl_dealer_atf_form.js'), /ffl\.capabilities\.notice\('ffl_national_search'\)/);
	assert.match(read('../../ffl_core/page/extensions/extensions.js'), /ffl\.capabilities\.notice\('extension_updates'\)/);
	assert.match(read('../../hooks.py'), /_hashed\("js\/capability_notice\.js"\)/);
});

// ---- the global CapabilityOff handler (r1)

const ours = JSON.stringify({ message: 'Distributor catalog is not set up. Set it up in Data Service Settings.', raise_exception: 1 });
const other = JSON.stringify({ message: 'Saved the draft first.', indicator: 'green' });
const answer = (extra) => Object.assign({ exc_type: 'CapabilityOff', capability_off: 'catalog',
	capability_off_message: 'Distributor catalog is not set up. Set it up in Data Service Settings.',
	_server_messages: JSON.stringify([other, ours]) }, extra);

test('the handler drops only the capability sentence and shows the other messages', () => {
	const { name, others } = cap.splitCapabilityOff(answer());
	assert.strictEqual(name, 'catalog');
	assert.deepStrictEqual(others, [other]);
	// a quiet guard (no sentence, no name): nothing dropped, the catalog notice
	assert.deepStrictEqual(cap.splitCapabilityOff({ _server_messages: JSON.stringify([other]) }),
		{ name: 'catalog', others: [other] });
	assert.deepStrictEqual(cap.splitCapabilityOff({ _server_messages: 'not json' }), { name: 'catalog', others: [] });
	assert.strictEqual(cap.splitCapabilityOff(answer({ capability_off: 'ffl_national_search' })).name, 'ffl_national_search');
	assert.strictEqual(cap.splitCapabilityOff(answer({ capability_off: 'bogus' })).name, 'catalog');
});

test('the notice shows every time, replacing the last one instead of hiding it', () => {
	const shown = [], removed = [], printed = [];
	const handler = cap.makeCapabilityOffHandler({
		notice: (name) => `<notice ${name}>`,
		msgprint: (m) => printed.push(m),
		showAlert: (html) => { const el = { html, remove: () => removed.push(html) }; shown.push(el); return el; },
	});
	const r = answer();
	const before = JSON.stringify(r);
	assert.strictEqual(handler(r), undefined);
	handler(r);
	handler(answer({ _server_messages: JSON.stringify([ours]) }));
	assert.deepStrictEqual(shown.map((e) => e.html), ['<notice catalog>', '<notice catalog>', '<notice catalog>']);
	assert.strictEqual(removed.length, 2);                 // the earlier two, each replaced
	assert.deepStrictEqual(printed, [[other], [other]]);   // the third answer had no other message
	assert.strictEqual(JSON.stringify(r), before);         // the answer is left as frappe passed it
});

test('installed beside other handlers; frappe.call keeps its error callback', () => {
	const existing = () => {};
	const alerts = [];
	const sandbox = {
		frappe: {
			boot: { ffl_capabilities: { catalog: 'off' } },
			user: { has_role: () => true },
			request: { error_handlers: { CapabilityOff: [existing] } },
			show_alert: (o) => { alerts.push(o); return { remove() {} }; },
			msgprint: () => {},
		},
		document: { getElementById: () => null, createElement: () => ({}), head: { appendChild() {} } },
		__: (s) => s,
	};
	sandbox.window = sandbox;
	vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../public/js/capability_notice.js'), 'utf8'), sandbox);
	const handlers = sandbox.frappe.request.error_handlers.CapabilityOff;
	assert.strictEqual(handlers.length, 2);
	assert.strictEqual(handlers[0], existing);
	const opts = { error: () => {}, callback: () => {} };
	handlers[1](answer());
	assert.strictEqual(typeof opts.error, 'function');      // nothing in the request options is touched
	assert.match(alerts[0].message, /Distributor catalog is not set up/);
	assert.strictEqual(alerts[0].indicator, 'orange');
	assert.strictEqual(sandbox.ffl.capabilities.isOff('catalog'), true);
});

test('the Item form lookups and the counter FFL field ask the shared component first', () => {
	const itemForm = read('../../public/js/item_form.js');
	// the dialog's Apply and its live preview each stop before the request
	assert.strictEqual((itemForm.match(/if \(ffl\.FirearmItemPicker\.catalogOff\(\)\)/g) || []).length, 2);
	assert.ok(itemForm.indexOf('if (ffl.FirearmItemPicker.catalogOff())') < itemForm.indexOf("method: 'ffl_integrations.rsr.promote.lookup_for_item_form'"));
	// the scan banners (Item form, Item quick entry) share gunstore.wireRsrScan, which stops first too
	assert.match(itemForm, /window\.gunstore\.wireRsrScan\(/);
	const bundle = read('../../public/js/ffl_core.bundle.js');
	assert.match(bundle, /const lookup = \(q, autoApply\) => \{\s*\/\/ No distributor catalog here[^\n]*\n\s*if \(ffl\.FirearmItemPicker\.catalogOff\(\)\)/);
	const counter = read('../../ffl_core/page/create_order/create_order.js');
	assert.match(counter, /<div id="biz-ffl-notice"/);
	assert.match(counter, /isOff\('ffl_national_search'\)/);
	assert.match(counter, /ffl\.capabilities\.notice\('ffl_national_search'\)/);
});

// ---- follow-up: silent calls and the v2 message shape

test('v2 answers: the messages list is read too, and only our sentence is dropped', () => {
	const sentence = 'Distributor catalog is not set up. Set it up in Data Service Settings.';
	const v2 = { exc_type: 'CapabilityOff', capability_off: 'catalog', capability_off_message: sentence,
		messages: [{ message: 'Saved the draft first.', indicator: 'green' }, { message: sentence }] };
	assert.deepStrictEqual(cap.splitCapabilityOff(v2).others, [{ message: 'Saved the draft first.', indicator: 'green' }]);
	// both shapes at once, and junk in either, never throw
	assert.strictEqual(cap.serverMessages(Object.assign({}, v2, { _server_messages: JSON.stringify([other]) })).length, 3);
	assert.deepStrictEqual(cap.serverMessages({ messages: 'nope', _server_messages: '{}' }), []);
	assert.deepStrictEqual(cap.serverMessages(null), []);
});

