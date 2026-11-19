'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { parse, titleCase, normalizeZip, isoDate } = require('../../public/js/aamva.js');

// AAMVA 2013 (version 08) Texas-style payload as a keyboard-wedge scanner types it.
const TX = '@\n\x1e\rANSI 636015080002DL00410278ZT03190008DLDAQ12345678\nDCSSAMPLE\nDDEN\nDACALEX\nDDFN\nDADMORGAN\nDDGN\nDCAC\nDCBNONE\nDCDNONE\nDBD01152020\nDBB01151975\nDBA12312030\nDBC1\nDAU068 IN\nDAYBLU\nDAG100 SAMPLE ST STE 5\nDAISPRINGFIELD\nDAJTX\nDAK700010000  \nDCF12345678901234567890\nDCGUSA\nDCK00000000000000000000\nDDAF\nDDB10012016\nDCUJR\nZTZTA\n\r';

test('parses name parts and address from a 2013-era payload', () => {
	const p = parse(TX);
	assert.deepStrictEqual(p, {
		first: 'Alex', middle: 'Morgan', last: 'Sample', suffix: 'Jr',
		address1: '100 Sample St Ste 5', address2: '', city: 'Springfield', state: 'TX', zip: '70001', country: 'United States',
	});
});

test('switch off (the default): never returns licence number, DOB, or expiry', () => {
	for (const p of [parse(TX), parse(TX, {}), parse(TX, { ids: false }), parse(TX, null)]) {
		assert.ok(!('licence' in p) && !('dob' in p) && !('id_number' in p) && !('idNumber' in p) && !('expiry' in p));
		assert.ok(!JSON.stringify(p).includes('12345678') && !JSON.stringify(p).includes('1975'));
		assert.deepStrictEqual(p, parse(TX));
	}
});

// Store Customer ID Numbers ON (frappe.boot.ffl_store_customer_id_numbers).
test('switch on: the separated shape also returns licence number, DOB and expiry', () => {
	assert.deepStrictEqual(parse(TX, { ids: true }), {
		first: 'Alex', middle: 'Morgan', last: 'Sample', suffix: 'Jr',
		address1: '100 Sample St Ste 5', address2: '', city: 'Springfield', state: 'TX', zip: '70001', country: 'United States',
		idNumber: '12345678', dob: '1975-01-15', expiry: '2030-12-31', gender: 'Male',
	});
});

test('switch on: DBC reads as Male / Female, anything else as none', () => {
	const sex = (v) => parse(TX.replace('DBC1', 'DBC' + v), { ids: true }).gender;
	assert.deepStrictEqual(['1', '2', '9', 'M', 'F', '', 'X'].map(sex), ['Male', 'Female', '', 'Male', 'Female', '', '']);
	assert.strictEqual(parse(GLUED.replace(/[\n\r\x1e]/g, ''), { ids: true }).gender, parse(GLUED, { ids: true }).gender);
});

test('switch on: everything else is exactly the switch-off result', () => {
	for (const raw of [TX, GLUED]) {
		const on = parse(raw, { ids: true });
		const { idNumber, dob, expiry, gender, ...rest } = on;
		assert.deepStrictEqual(rest, parse(raw));
	}
	assert.strictEqual(parse('Alex Sample', { ids: true }), null);
});

test('legacy DAA "LAST,FIRST,MIDDLE" and DAB/DAE aliases', () => {
	const legacy = '@\n\x1e\rANSI 6360000102DL00390188ZT02270036DLDAQ99\nDAASMITH,JOHN,PAUL\nDAG1 MAIN ST\nDAIAUSTIN\nDAJTX\nDAK78701\n';
	assert.deepStrictEqual(parse(legacy).first, 'John');
	assert.strictEqual(parse(legacy).middle, 'Paul');
	assert.strictEqual(parse(legacy).last, 'Smith');
	const alias = 'ANSI 636000010002DL\nDABO\'BRIEN-SMITH\nDCTMARY\nDAEIII\nDAK123456789\nDCGCAN\n';
	const a = parse(alias);
	assert.strictEqual(a.last, "O'Brien-Smith");
	assert.strictEqual(a.first, 'Mary');
	assert.strictEqual(a.suffix, 'III');
	assert.strictEqual(a.zip, '12345-6789');
	assert.strictEqual(a.country, 'Canada');
});

test('NONE placeholders and unknown suffixes are blanked; zip+4 of zeros dropped', () => {
	const p = parse('ANSI 636015080002DL\nDCSDOE\nDACJANE\nDADNONE\nDCUNONE\nDAK123450000\n');
	assert.strictEqual(p.middle, '');
	assert.strictEqual(p.suffix, '');
	assert.strictEqual(p.zip, '12345');
	assert.strictEqual(normalizeZip('70001-1234'), '70001-1234');
});

test('a name tag glued to the header line is not lost; country only when DCG is present', () => {
	const p = parse('@\n\x1e\rANSI 636015080002DL00410278ZT03190008DLDCSSAMPLE\nDACALEX\nDAISPRINGFIELD\n');
	assert.strictEqual(p.last, 'Sample');
	assert.strictEqual(p.country, '');
	assert.strictEqual(parse(TX).country, 'United States');
});

// What the store's scanner really types (2026-09-09, keyboard wedge): every
// separator dropped, one glued run after "@". Licence number / DOB / document
// discriminator digits below are synthetic.
const GLUED = '@\nANSI636015100102DL0040281ZT03220056DLDCACDCBADCDNONEDBA12312029DCSROEDDENDACJANEDDFNDADNONEDDGNDBD01152024DBB01011990DBCDAYBLUDAU068inDAG100SAMPLE ST APT4211DAISPRINGFIELDDAJTXDAK75074-1234DAQ00000000DCF00000000000000000000DCGUSADAZBLKDCK00000000000000000000DCLAPODDAZTZTANZTBAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=';

test('glued keyboard-wedge output (no separators) parses name and address', () => {
	assert.deepStrictEqual(parse(GLUED), {
		first: 'Jane', middle: '', last: 'Roe', suffix: '',
		address1: '100 Sample St Apt 4211', address2: '', city: 'Springfield', state: 'TX', zip: '75074-1234', country: 'United States',
	});
	assert.ok(!JSON.stringify(parse(GLUED)).match(/1990|0000000/));
});

test('switch on: the glued keyboard-wedge shape returns licence number, DOB and expiry', () => {
	const p = parse(GLUED, { ids: true });
	assert.strictEqual(p.idNumber, '00000000');
	assert.strictEqual(p.dob, '1990-01-01');
	assert.strictEqual(p.expiry, '2029-12-31');
	assert.strictEqual(p.last, 'Roe');
	assert.strictEqual(p.city, 'Springfield');
});

test('switch on: unreadable dates and numbers come back blank, never half-parsed', () => {
	const p = parse('ANSI 636015080002DL\nDAQ!!\nDCSDOE\nDACJANE\nDBB13452020\nDBA2020\n', { ids: true });
	assert.strictEqual(p.idNumber, '');
	assert.strictEqual(p.dob, '');
	assert.strictEqual(p.expiry, '');
	assert.strictEqual(p.last, 'Doe');
});

test('isoDate: MMDDCCYY (US) and CCYYMMDD (Canada / 2000 standard), real dates only', () => {
	assert.strictEqual(isoDate('01151975'), '1975-01-15');
	assert.strictEqual(isoDate('19750115'), '1975-01-15');
	assert.strictEqual(isoDate('20291231'), '2029-12-31');
	assert.strictEqual(isoDate('02302020'), '');      // Feb 30
	assert.strictEqual(isoDate('01020102'), '');      // year 0102
	assert.strictEqual(isoDate('0520196'), '');
	assert.strictEqual(isoDate(''), '');
});

test('glued shape: a surname containing tag-shaped letters survives via the truncation flag', () => {
	const p = parse('@\nANSI636015100102DL0040281ZT03220056DLDCACDCSMCDADEDDENDACJOHNDDFNDADNONEDDGNDAG1 MAIN STDAIAUSTINDAJTXDAK78701DCGUSA');
	assert.strictEqual(p.last, 'Mcdade');
	assert.strictEqual(p.first, 'John');
	assert.strictEqual(p.middle, '');
	assert.strictEqual(p.city, 'Austin');
});

test('glued shape: DALLAS (holds tag DAL) and DAISY LN (holds tag DAI) survive the tag cut', () => {
	const p = parse('@\nANSI636015100102DL0040281ZT03220056DLDCACDCSSMITHDDENDACANNDDFNDADNONEDDGNDAG123 DAISY LNDAIDALLASDAJTXDAK75201-1234DCGUSA');
	assert.strictEqual(p.address1, '123 Daisy Ln');
	assert.strictEqual(p.city, 'Dallas');
	assert.strictEqual(p.state, 'TX');
	assert.strictEqual(p.zip, '75201-1234');
	assert.strictEqual(p.last, 'Smith');
});

test('glued shape: identifiers never leak into name/address even when elements are out of order', () => {
	// Non-canonical ordering: DBB/DAQ right after the names, no truncation flags, DAQ between street and city.
	const odd = '@\nANSI636015100102DL0040281ZT03220056DLDCSSMITHDACJOHNDADLEEDBB01011990DAQ12345678DAG1 MAIN STDAQ99999999DAIAUSTINDAJTXDAK78701DCGUSA';
	const p = parse(odd);
	const blob = JSON.stringify(p);
	assert.ok(!/1990|12345678|99999999/.test(blob), blob);
	assert.strictEqual(p.last, 'Smith');
	assert.strictEqual(p.first, 'John');
	assert.strictEqual(p.city, 'Austin');
});

test('street keeps ordinals and splits glued numbers', () => {
	const p = parse('@\nANSI636015100102DL0040281ZT03220056DLDCSDOEDDENDACJANEDDFNDADNONEDDGNDAG123 W 5TH ST APT4211DAISPRINGFIELDDAJTXDAK75074DCGUSA');
	assert.strictEqual(p.address1, '123 W 5th St Apt 4211');
});

// Texas License to Carry: an ID subfile whose Texas subfile carries the holder's
// driver licence (ZTB, state first) and the card kind (ZTC).
const LTC = '@\n\x1e\rANSI 636015090001ID00410240ZT02810027IDDCANONE\nDCBNONE\nDCDNONE\nDCIRC\nDBA04292030\nDCSSAMPLE\nDDEN\nDACALEX\nDDFN\nDADNONE\nDDGN\nDBDNONE\nDBB01151975\nDBC1\nDAYBRO\nDAU070 in\nDAG100 SAMPLE ST\nDAISPRINGFIELD\nDAJTX\nDAK700010000  \nDAQ01234567\nDCF1234567\nDCGUSA\nDAZBLK\nDCLAP\nDAW185\n\rZTZTBTX 12345678\nZTCTXLTC\n\r';

test('Texas License to Carry: DAQ is the carry number, ZTB the driver licence — both wire shapes', () => {
	const expected = {
		first: 'Alex', middle: '', last: 'Sample', suffix: '',
		address1: '100 Sample St', address2: '', city: 'Springfield', state: 'TX', zip: '70001', country: 'United States',
		idNumber: '12345678', dob: '1975-01-15', expiry: '', gender: 'Male', carryNumber: '01234567', carryExpiry: '2030-04-29',
	};
	assert.deepStrictEqual(parse(LTC, { ids: true }), expected);
	assert.deepStrictEqual(parse(LTC.replace(/[\n\r\x1e]/g, ''), { ids: true }), expected);
	// Switch off: neither number, and nothing else changes.
	const { idNumber, dob, expiry, gender, carryNumber, carryExpiry, ...rest } = expected;
	assert.deepStrictEqual(parse(LTC), rest);
	assert.ok(!/1234567|1975|2030/.test(JSON.stringify(parse(LTC))));
});

test('Texas License to Carry without a readable ZTB: the carry number is never the driver licence', () => {
	const p = parse(LTC.replace('ZTZTBTX 12345678\n', 'ZT'), { ids: true });
	assert.strictEqual(p.idNumber, '');
	assert.strictEqual(p.carryNumber, '01234567');
	// A driver licence (no ZTC "TXLTC") has no carry fields at all.
	assert.ok(!('carryNumber' in parse(TX, { ids: true })) && !('carryExpiry' in parse(TX, { ids: true })));
});

test('typed text that is not a barcode is left alone', () => {
	assert.strictEqual(parse('Alex Sample'), null);
	assert.strictEqual(parse(''), null);
	assert.strictEqual(parse('DACALEX'), null);           // fragment without a header and no last name
});

test('titleCase keeps apostrophes and hyphens', () => {
	assert.strictEqual(titleCase("O'CONNOR-DE LA CRUZ"), "O'Connor-De La Cruz");
});
