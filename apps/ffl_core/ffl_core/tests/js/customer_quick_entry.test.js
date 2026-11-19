'use strict';
const test = require('node:test');
const assert = require('node:assert');
const qe = require('../../public/js/customer_quick_entry.js');

const META = [
	{ fieldname: 'customer_name', fieldtype: 'Data', reqd: 1 },
	{ fieldname: 'customer_type', fieldtype: 'Select', reqd: 1 },
	{ fieldname: 'ffl_first_name', fieldtype: 'Data', allow_in_quick_entry: 1, description: 'old help' },
	{ fieldname: 'middle_name', fieldtype: 'Data', allow_in_quick_entry: 1, description: 'Bound-book identity…' },
	{ fieldname: 'ffl_last_name', fieldtype: 'Data', allow_in_quick_entry: 1 },
	{ fieldname: 'name_suffix', fieldtype: 'Select', allow_in_quick_entry: 1 },
	{ fieldname: 'customer_group', fieldtype: 'Link', allow_in_quick_entry: 1 },
];

test('composeName joins non-empty parts in bound-book order', () => {
	assert.strictEqual(qe.composeName({ ffl_first_name: ' Alex ', middle_name: '', ffl_last_name: 'Sample', name_suffix: 'Jr' }), 'Alex Sample Jr');
	assert.strictEqual(qe.composeName({}), '');
});

test('arrangeFields: type, scan, parts, name, rest — parts required for individuals, name read-only', () => {
	const out = qe.arrangeFields(META);
	assert.deepStrictEqual(out.map((f) => f.fieldname),
		['customer_type', 'ffl_scan_id', 'ffl_first_name', 'middle_name', 'ffl_last_name', 'name_suffix', 'customer_name', 'customer_group']);
	const by = Object.fromEntries(out.map((f) => [f.fieldname, f]));
	assert.strictEqual(by.ffl_first_name.mandatory_depends_on, qe.INDIVIDUAL);
	assert.strictEqual(by.ffl_last_name.mandatory_depends_on, qe.INDIVIDUAL);
	assert.strictEqual(by.middle_name.mandatory_depends_on, undefined);
	assert.strictEqual(by.middle_name.description, '');              // stray help text gone
	assert.strictEqual(by.customer_name.read_only_depends_on, qe.INDIVIDUAL);
	assert.strictEqual(by.customer_name.reqd, 1);                    // still required (docname)
	assert.strictEqual(by.ffl_scan_id.depends_on, qe.INDIVIDUAL);
	assert.strictEqual(by.ffl_scan_id.fieldtype, 'Small Text');       // keeps the payload's newlines
	// meta objects untouched
	assert.strictEqual(META[3].description, 'Bound-book identity…');
	assert.strictEqual(META[2].mandatory_depends_on, undefined);
});

test('arrangeFields tolerates a site without the custom parts', () => {
	const out = qe.arrangeFields(META.filter((f) => !f.fieldname.startsWith('ffl_') && f.fieldname !== 'middle_name' && f.fieldname !== 'name_suffix'));
	assert.deepStrictEqual(out.map((f) => f.fieldname), ['customer_type', 'ffl_scan_id', 'customer_name', 'customer_group']);
});

test('scanValues maps parsed AAMVA keys to dialog fields and skips blanks', () => {
	assert.deepStrictEqual(qe.scanValues({ first: 'Alex', middle: '', last: 'Sample', suffix: '', address1: '1 Main', address2: '', city: 'Springfield', state: 'TX', zip: '70001', country: 'United States' }),
		{ ffl_first_name: 'Alex', ffl_last_name: 'Sample', address_line1: '1 Main', city: 'Springfield', state: 'TX', pincode: '70001', country_address: 'United States' });
	assert.deepStrictEqual(qe.scanValues(null), {});
});

test('splitDisplayName mirrors the server split (suffix pulled out mid-name)', () => {
	assert.deepStrictEqual(qe.splitDisplayName('pat lee IV Sampleton'),
		{ ffl_first_name: 'pat', middle_name: 'lee', ffl_last_name: 'Sampleton', name_suffix: 'IV' });
	assert.deepStrictEqual(qe.splitDisplayName('Cher'), { ffl_first_name: 'Cher', middle_name: '', ffl_last_name: '', name_suffix: '' });
	assert.deepStrictEqual(qe.splitDisplayName('  John   Smith jr. '), { ffl_first_name: 'John', middle_name: '', ffl_last_name: 'Smith', name_suffix: 'Jr' });
	assert.strictEqual(qe.composeName(qe.splitDisplayName('John Q Public Sr')), 'John Q Public Sr');
});

test('stripScan removes the dialog-only scan value from the doc that update_doc built', () => {
	// update_doc()-shaped: every non-empty dialog value copied onto the doc.
	const doc = { customer_name: 'Alex Sample', ffl_first_name: 'Alex', ffl_scan_id: 'ANSI 636015…DAQ12345678\nDBB01151975' };
	qe.stripScan(doc);
	assert.deepStrictEqual(doc, { customer_name: 'Alex Sample', ffl_first_name: 'Alex' });
	assert.ok(!JSON.stringify(doc).includes('12345678'));
	assert.deepStrictEqual(qe.stripScan({ a: 1 }), { a: 1 });
	assert.strictEqual(qe.stripScan(null), null);
});

test('scanValues: a field filled by an earlier partial tick but absent now is reset to ""', () => {
	const prev = qe.scanValues({ first: 'Jane', last: 'Roe', middle: 'No' });
	const next = qe.scanValues({ first: 'Jane', last: 'Roe', city: 'Springfield' }, prev);
	assert.deepStrictEqual(next, { ffl_first_name: 'Jane', ffl_last_name: 'Roe', city: 'Springfield', middle_name: '' });
});

test('scanValues: previous holding a "" key from an earlier reset does not re-emit ""; a field missing in a partial tick and present in the full payload is filled', () => {
	const tick1 = qe.scanValues({ first: 'Jane', last: 'Roe', middle: 'No' });
	const tick2 = qe.scanValues({ first: 'Jane', last: 'Roe' }, tick1);
	assert.deepStrictEqual(tick2, { ffl_first_name: 'Jane', ffl_last_name: 'Roe', middle_name: '' });
	// the caller keeps only non-empty values as `previous` for the next tick
	const kept = Object.fromEntries(Object.entries(tick2).filter(([, v]) => v !== ''));
	const tick3 = qe.scanValues({ first: 'Jane', last: 'Roe', city: 'Springfield', zip: '75074' }, kept);
	assert.deepStrictEqual(tick3, { ffl_first_name: 'Jane', ffl_last_name: 'Roe', city: 'Springfield', pincode: '75074' });
	// and a fuller previous with '' entries (defensive) still never emits '' for them
	assert.deepStrictEqual(qe.scanValues({ first: 'Jane', last: 'Roe' }, tick2), { ffl_first_name: 'Jane', ffl_last_name: 'Roe' });
});

// Store Customer ID Numbers switch. OFF (every test above) is byte-identical to
// before the switch; ON adds the three licence fields and the customer_ids row.
test('arrangeFields: switch off adds nothing, switch on puts DOB / licence # / expiry after the name', () => {
	assert.deepStrictEqual(qe.arrangeFields(META, null, { ids: false }), qe.arrangeFields(META));
	assert.deepStrictEqual(qe.arrangeFields(META, null, {}), qe.arrangeFields(META));
	const on = qe.arrangeFields(META, null, { ids: true });
	assert.deepStrictEqual(on.map((f) => f.fieldname),
		['customer_type', 'ffl_scan_id', 'ffl_first_name', 'middle_name', 'ffl_last_name', 'name_suffix', 'customer_name',
			'date_of_birth', 'gender', 'ffl_id_number', 'ffl_id_expiry', 'ffl_carry_number', 'ffl_carry_expiry', 'customer_group']);
	const by = Object.fromEntries(on.map((f) => [f.fieldname, f]));
	assert.strictEqual(by.date_of_birth.fieldtype, 'Date');
	assert.strictEqual(by.ffl_id_expiry.fieldtype, 'Date');
	assert.strictEqual(by.ffl_id_number.fieldtype, 'Data');
	['date_of_birth', 'gender', 'ffl_id_number', 'ffl_id_expiry'].forEach((f) => assert.strictEqual(by[f].depends_on, qe.INDIVIDUAL));
	assert.deepStrictEqual([by.gender.fieldtype, by.gender.options], ['Link', 'Gender']);
	// The licence-to-carry pair shows only once a scan has filled its number.
	['ffl_carry_number', 'ffl_carry_expiry'].forEach((f) =>
		assert.strictEqual(by[f].depends_on, "eval:doc.customer_type=='Individual' && doc.ffl_carry_number"));
});

test('a Texas License to Carry scan: its number becomes a CCW row, the holder\'s licence the Driver License row', () => {
	const aamva = require('../../public/js/aamva.js');
	const raw = '@\n\x1e\rANSI 636015090001ID00410240ZT02810027IDDCSSAMPLE\nDACALEX\nDBB01151975\nDBC2\nDBA04292030\nDAG1 MAIN ST\nDAISPRINGFIELD\nDAJTX\nDAK70001\nDAQ01234567\n\rZTZTBTX 12345678\nZTCTXLTC\n\r';
	// A partial tick (the Texas subfile not typed yet) reads as a plain ID card…
	const early = qe.scanValues(aamva.parse(raw.slice(0, raw.indexOf('ZTZTB')), { ids: true }));
	assert.strictEqual(early.ffl_id_number, '01234567');
	assert.strictEqual(early.ffl_id_expiry, '2030-04-29');
	// …and the full payload puts each number where it belongs, clearing the expiry it lent.
	const values = qe.scanValues(aamva.parse(raw, { ids: true }), early);
	assert.deepStrictEqual(values, { ffl_first_name: 'Alex', ffl_last_name: 'Sample', address_line1: '1 Main St', city: 'Springfield',
		state: 'TX', pincode: '70001', date_of_birth: '1975-01-15', gender: 'Female', ffl_id_number: '12345678', ffl_id_expiry: '',
		ffl_carry_number: '01234567', ffl_carry_expiry: '2030-04-29' });

	let made = 0;
	const addRow = (doc) => { made += 1; const row = {}; (doc.customer_ids = doc.customer_ids || []).push(row); return row; };
	const doc = { customer_type: 'Individual', ...values };
	qe.applyIdRow(doc, addRow);
	qe.applyIdRow(Object.assign(doc, values), addRow);   // update_doc runs twice per save
	assert.strictEqual(made, 2);
	assert.deepStrictEqual(doc.customer_ids, [
		{ __ffl_scanned: 1, id_type: 'Driver License', id_number: '12345678', expiry_date: null },
		{ __ffl_scanned: 1, id_type: 'CCW', id_number: '01234567', expiry_date: '2030-04-29' }]);
	['ffl_id_number', 'ffl_id_expiry', 'ffl_carry_number', 'ffl_carry_expiry'].forEach((f) => assert.ok(!(f in doc), f));
	// Flipped to Company afterwards: both scanned rows go.
	qe.applyIdRow(Object.assign(doc, values, { customer_type: 'Company' }), addRow);
	assert.deepStrictEqual(doc.customer_ids, []);
});

test('scanValues: switch-on parse fills the licence fields; a switch-off parse never does', () => {
	const aamva = require('../../public/js/aamva.js');
	const raw = '@\n\x1e\rANSI 636015080002DL00410278ZT03190008DLDAQ12345678\nDCSSAMPLE\nDACALEX\nDBB01151975\nDBA12312030\nDAG1 MAIN ST\nDAISPRINGFIELD\nDAJTX\nDAK70001\n';
	const off = qe.scanValues(aamva.parse(raw));
	assert.deepStrictEqual(off, { ffl_first_name: 'Alex', ffl_last_name: 'Sample', address_line1: '1 Main St', city: 'Springfield', state: 'TX', pincode: '70001' });
	assert.deepStrictEqual(qe.scanValues(aamva.parse(raw, { ids: true })),
		{ ...off, date_of_birth: '1975-01-15', ffl_id_number: '12345678', ffl_id_expiry: '2030-12-31' });
});

test('applyIdRow: one marked Driver License row, updated in place when update_doc runs twice', () => {
	let made = 0;
	const addRow = (doc) => { made += 1; const row = { doctype: 'Customer ID Document' }; (doc.customer_ids = doc.customer_ids || []).push(row); return row; };
	const doc = { customer_name: 'Alex Sample', customer_type: 'Individual', date_of_birth: '1975-01-15', ffl_id_number: ' 12345678 ', ffl_id_expiry: '2030-12-31' };
	qe.applyIdRow(doc, addRow);
	assert.deepStrictEqual(doc, {
		customer_name: 'Alex Sample', customer_type: 'Individual', date_of_birth: '1975-01-15',
		customer_ids: [{ doctype: 'Customer ID Document', __ffl_scanned: 1, id_type: 'Driver License', id_number: '12345678', expiry_date: '2030-12-31' }],
	});
	// update_doc copies the dialog values back on and runs again (insert()).
	Object.assign(doc, { ffl_id_number: 'D999', ffl_id_expiry: '2030-01-01' });
	qe.applyIdRow(doc, addRow);
	assert.strictEqual(made, 1);
	assert.deepStrictEqual(doc.customer_ids.map((r) => [r.id_number, r.expiry_date]), [['D999', '2030-01-01']]);
	// The clerk clears the number: the row goes, rows typed by hand stay.
	doc.customer_ids.unshift({ id_type: 'CCW', id_number: 'C1' });
	Object.assign(doc, { ffl_id_number: '' });
	qe.applyIdRow(doc, addRow);
	assert.deepStrictEqual(doc.customer_ids, [{ id_type: 'CCW', id_number: 'C1' }]);
	assert.ok(!('ffl_id_number' in doc) && !('ffl_id_expiry' in doc));
	assert.strictEqual(qe.applyIdRow(null, addRow), null);
});

test('applyIdRow: a Company gets no Driver License row and no hidden licence values', () => {
	const addRow = (doc) => { const row = {}; (doc.customer_ids = doc.customer_ids || []).push(row); return row; };
	// Scanned as an Individual, then the clerk flipped the type to Company.
	const doc = { customer_name: 'ACME ARMS', customer_type: 'Company', date_of_birth: '1975-01-15', ffl_id_number: '12345678', ffl_id_expiry: '2030-12-31' };
	qe.applyIdRow(doc, addRow);
	assert.deepStrictEqual(doc, { customer_name: 'ACME ARMS', customer_type: 'Company' });
	// A row made while it was still Individual goes; one typed by hand stays.
	const second = { customer_type: 'Partnership', ffl_id_number: 'D1',
		customer_ids: [{ id_type: 'CCW', id_number: 'C1' }, { __ffl_scanned: 1, id_type: 'Driver License', id_number: 'D1' }] };
	qe.applyIdRow(second, addRow);
	assert.deepStrictEqual(second, { customer_type: 'Partnership', customer_ids: [{ id_type: 'CCW', id_number: 'C1' }] });
	// A type that is not there yet is not Individual either.
	const none = { ffl_id_number: 'D2' };
	qe.applyIdRow(none, addRow);
	assert.deepStrictEqual(none, {});
});

test('arrangeFields: the type the caller opened with is the type field\'s default, on a copy', () => {
	const withType = qe.arrangeFields(META, null, { type: 'Individual' });
	assert.strictEqual(withType.find((f) => f.fieldname === 'customer_type').default, 'Individual');
	assert.ok(!('default' in META[1]));                                   // cached meta untouched
	assert.ok(!('default' in qe.arrangeFields(META).find((f) => f.fieldname === 'customer_type')));
	assert.ok(!('default' in qe.arrangeFields(META, null, { type: '' }).find((f) => f.fieldname === 'customer_type')));
	// Nothing else moves.
	assert.deepStrictEqual(withType.map((f) => f.fieldname), qe.arrangeFields(META).map((f) => f.fieldname));
});

test('familyTitle names whose family the member joins', () => {
	const L = (s, args) => s.replace('{0}', args[0]);
	assert.strictEqual(qe.familyTitle('Maria Lopez', L), 'Add family member to Maria Lopez');
});

test('arrangeFields: the Relationship select exists only in the family flow, right after the name', () => {
	const REL = '\nSpouse\nChild';
	assert.ok(!qe.arrangeFields(META).some((f) => f.fieldname === 'relationship'));
	assert.ok(!qe.arrangeFields(META, null, { family: null }).some((f) => f.fieldname === 'relationship'));
	const out = qe.arrangeFields(META, null, { family: { relationshipOptions: REL }, ids: true });
	const names = out.map((f) => f.fieldname);
	assert.strictEqual(names[names.indexOf('customer_name') + 1], 'relationship');
	assert.ok(names.indexOf('relationship') < names.indexOf('date_of_birth'));
	const rel = out.find((f) => f.fieldname === 'relationship');
	assert.deepStrictEqual([rel.fieldtype, rel.options, rel.reqd], ['Select', REL, undefined]);
	// Nothing else moves when the family flow is switched on.
	assert.deepStrictEqual(names.filter((n) => n !== 'relationship'),
		qe.arrangeFields(META, null, { ids: true }).map((f) => f.fieldname));
});
