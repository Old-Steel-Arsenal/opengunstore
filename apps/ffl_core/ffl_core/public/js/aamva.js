// AAMVA driver-license / ID-card barcode (PDF417) parser — pure, DOM-free,
// Node-tested. A 2D scanner in keyboard-wedge mode types the whole payload:
//   @\n\x1e\rANSI 636015090002DL00410278ZT03190008DLDAQ12345678\nDCSSAMPLE\nDAC…
// Every element is a three-letter tag followed by its value up to the next
// line separator. By default only name parts and the address are returned;
// the licence number, date of birth and expiry are returned only when the
// caller asks with parse(raw, {ids: true}) — the store's FFL Settings switch
// "Store Customer ID Numbers" (frappe.boot.ffl_store_customer_id_numbers).
// Without the flag the result is exactly what it was before that switch.
//
// Tags (AAMVA 2003+; 2000-era aliases in brackets):
//   DCS last [DAB]   DAC first [DCT]   DAD middle   DCU suffix [DAE]
//   DAA legacy "LAST,FIRST,MIDDLE" (or "LAST FIRST MIDDLE")
//   DAG street   DAH street 2   DAI city   DAJ state   DAK zip   DCG country
//   DAQ licence number   DBB date of birth   DBA expiry   DBC sex (1 male, 2 female)
//   (these four only with {ids: true})
//
// A Texas License to Carry is an ID subfile whose Texas subfile says so
// (ZTC "TXLTC"). Its DAQ and DBA are the carry licence's own number and expiry;
// the holder's driver licence travels in ZTB, state first ("TX 12345678" or "TX12345678"). With
// {ids: true} such a card returns idNumber = the driver licence, expiry = ''
// (the card does not carry it), and carryNumber / carryExpiry for the licence
// to carry — so the carry number is never taken for a driver licence.
//
// Two wire shapes are handled: the spec's (one element per line, LF/CR/RS
// separators) and what a keyboard-wedge scanner actually types — the same
// elements glued into ONE run with every separator dropped
// ("…DLDCACDCBADCDNONEDBA05202029DCSROEDDENDACJANEDDFNDADNONEDDGN…"). For the
// glued shape elements are cut at the next known tag; the 2011+ truncation
// flags (DDE/DDF/DDG) sit right after last/first/middle name and anchor those
// three so a surname containing tag-shaped letters is not split.
(function (root, factory) {
	var api = factory();
	if (typeof module === 'object' && module.exports) module.exports = api;
	else { root.ffl = root.ffl || {}; root.ffl.aamva = api; }
})(typeof self !== 'undefined' ? self : this, function () {
	'use strict';

	var SUFFIXES = { jr: 'Jr', sr: 'Sr', ii: 'II', iii: 'III', iv: 'IV', v: 'V', vi: 'VI' };
	var NONE = { '': 1, NONE: 1, 'N/A': 1, UNK: 1, UNKNOWN: 1 };
	var TAGS = {
		DCS: 'last', DAB: 'last', DAC: 'first', DCT: 'first', DAD: 'middle',
		DCU: 'suffix', DAE: 'suffix', DAA: 'legacyName',
		DAG: 'address1', DAH: 'address2', DAI: 'city', DAJ: 'state', DAK: 'zip', DCG: 'country',
	};
	// Read off the card always, but returned only for parse(raw, {ids: true}).
	var ID_TAGS = { DAQ: 'idNumber', DBB: 'dob', DBA: 'expiry', DBC: 'sex', ZTB: 'holderId', ZTC: 'cardType' };
	// DBC: 1 / 2 (2003+), M / F (2000); 9 "not specified" and anything else read as ''.
	var GENDERS = { 1: 'Male', M: 'Male', 2: 'Female', F: 'Female' };
	var TEXAS_LTC = 'TXLTC';
	function keyOf(tag) { return TAGS[tag] || ID_TAGS[tag]; }
	// Every tag the DL/ID subfile can carry (AAMVA 2000–2020) — the cut points
	// for the glued shape. Jurisdiction subfiles use Z + two letters.
	var ALL_TAGS = ('DAA DAB DAC DAD DAE DAF DAG DAH DAI DAJ DAK DAL DAM DAN DAO DAP DAQ DAR DAS DAT DAU DAV DAW DAX DAY DAZ '
		+ 'DBA DBB DBC DBD DBE DBF DBG DBH DBI DBJ DBK DBL DBM DBN DBO DBP DBQ DBR DBS '
		+ 'DCA DCB DCD DCE DCF DCG DCH DCI DCJ DCK DCL DCM DCN DCO DCP DCQ DCR DCS DCT DCU '
		+ 'DDA DDB DDC DDD DDE DDF DDG DDH DDI DDJ DDK DDL PAA PAB PAC PAD PAE PAF').split(' ');
	var TAG_SET = {};
	ALL_TAGS.forEach(function (t) { TAG_SET[t] = 1; });
	var NEXT_TAG = new RegExp('(?=' + ALL_TAGS.join('|') + '|Z[A-Z]{2})');

	function looksLikeAamva(raw) {
		return /ANSI\s*\d{6}|AAMVA/.test(raw || '') || /(^|[\r\n])(DL|ID)?D(CS|AC|AQ|AA)/.test(raw || '');
	}

	// "SAMPLE" → "Sample", "O'BRIEN-SMITH" → "O'Brien-Smith", "100SAMPLE ST" →
	// "100Sample St"; "MCDONALD" stays "Mcdonald" (no surname heuristics:
	// the clerk can fix a rare one, a wrong guess is worse).
	function titleCase(s) {
		return String(s || '').toLowerCase().replace(/(^|[\s'\-\/])([a-z])/g, function (m, sep, ch) {
			return sep + ch.toUpperCase();
		});
	}

	function clean(v) {
		v = String(v || '').replace(/[\x00-\x1f]+$/g, '').trim();
		return NONE[v.toUpperCase()] ? '' : v;
	}

	function normalizeSuffix(v) {
		var key = clean(v).toLowerCase().replace(/\./g, '');
		return SUFFIXES[key] || '';
	}

	// Street lines on the card often glue number and name ("100SAMPLE ST
	// APT4211"): put a space at digit↔letter boundaries inside each token.
	function street(v) {
		var spaced = clean(v).toUpperCase()
			.replace(/(\d)(?!(?:ST|ND|RD|TH)(?![A-Z]))([A-Z])/g, '$1 $2')   // 680EXECUTIVE → 100 SAMPLE, 5TH stays
			.replace(/([A-Z])(\d)/g, '$1 $2');                                   // APT4211 → APT 4211
		return titleCase(spaced);
	}

	// AAMVA dates are MMDDCCYY (US cards, 2003+) or CCYYMMDD (Canada, and the
	// 2000 standard). They cannot collide: CCYY starts 19/20, which is no month.
	// Returns YYYY-MM-DD, or '' for anything that is not a real date.
	function isoDate(v) {
		var d = String(v || '').replace(/\D/g, '');
		if (d.length !== 8) return '';
		var ymd = /^(19|20)/.test(d) ? [d.slice(0, 4), d.slice(4, 6), d.slice(6, 8)]
			: [d.slice(4, 8), d.slice(0, 2), d.slice(2, 4)];
		var y = +ymd[0], m = +ymd[1], day = +ymd[2];
		var dt = new Date(Date.UTC(y, m - 1, day));
		if (y < 1900 || y > 2100 || dt.getUTCFullYear() !== y || dt.getUTCMonth() !== m - 1 || dt.getUTCDate() !== day) return '';
		return ymd.join('-');
	}

	// A licence number is letters, digits, spaces and dashes; anything else
	// (a glued-shape cut gone wrong) is dropped rather than shown as a number.
	function idNumber(v) {
		v = clean(v).toUpperCase();
		return /^[A-Z0-9][A-Z0-9 \-]{2,24}$/.test(v) ? v : '';
	}

	function normalizeZip(v) {
		var digits = String(v || '').replace(/\D/g, '');
		if (digits.length >= 9 && /[1-9]/.test(digits.slice(5, 9))) return digits.slice(0, 5) + '-' + digits.slice(5, 9);
		return digits.slice(0, 5);
	}

	// Only when the card says so (DCG): an unconditional country would make
	// ERPNext's variant address fields mandatory after a name-only scan.
	function countryName(code) {
		code = clean(code).toUpperCase();
		if (!code) return '';
		if (code === 'CAN') return 'Canada';
		if (code === 'MEX') return 'Mexico';
		return 'United States';
	}

	// Split the payload into tag/value pairs. The first element of a subfile is
	// glued to its designator ("DLDAQ…", "IDDCS…", "ZTZTB…"), so a leading
	// DL/ID/ZT is dropped when what follows is itself a known tag.
	function elements(raw) {
		var out = {};
		String(raw || '').split(/[\r\n\x1e]+/).forEach(function (line) {
			line = line.replace(/^[\x00-\x20@]+/, '');
			// The header line ("ANSI 636015…ZT03190008") ends with the first
			// subfile's designator glued to its first element: keep that tail.
			var header = /^ANSI\s*\d+.*?(DL|ID)([A-Z]{3}.*)$/.exec(line);
			if (header) line = header[1] + header[2];
			if (/^(DL|ID|ZT)[A-Z]{3}/.test(line) && keyOf(line.slice(2, 5))) line = line.slice(2);
			var key = keyOf(line.slice(0, 3));
			if (key && !(key in out)) out[key] = clean(line.slice(3));
		});
		return out;
	}

	// Glued shape (no separators): walk the payload cutting at known tags. The
	// name trio is anchored by its truncation flag when present. Value classes
	// are deliberately narrow — names cannot contain digits, an address line
	// cannot contain an identifier tag — so a jurisdiction that orders
	// elements differently can never spill the licence number or DOB into a
	// name or address field.
	var NAME_CHARS = "[A-Za-z' ,.\\-]{0,60}?";
	var PII_TAGS = 'DAQ|DBA|DBB|DBD|DCF|DCK|DDB|DDC|DDD|DCA|DCB|DCD|DBC|DAU|DAY|DAZ|DDK|DDL';
	var ADDR_CHARS = "(?:(?!" + PII_TAGS + ")[A-Za-z0-9' ,.#/\\-]){0,80}?";
	var CITY_CHARS = "(?:(?!DAI)[A-Za-z' ,.\\-]){0,60}?";   // the LAST DAI before DAJ starts the city
	function gluedElements(raw) {
		var body = String(raw || '').replace(/[\x00-\x1f\x7f@]/g, '');
		var start = body.search(/(DL|ID)(?=[A-Z]{3})/);
		if (start < 0) return {};
		body = body.slice(start + 2);
		var out = {}, pos = 0;
		// Sequential: each anchor is searched after the previous one, so tag-
		// shaped letters inside an earlier value ("MCDADE" holds DAD) cannot
		// start the next name element.
		function anchored(tag, flag, key) {
			var re = new RegExp(tag + '(' + NAME_CHARS + ')' + flag + '[TNU](?=[A-Z]{3}|$)', 'g');
			re.lastIndex = pos;
			var m = re.exec(body);
			if (!m) return;
			out[key] = clean(m[1]);
			pos = m.index + m[0].length;
		}
		anchored('DCS', 'DDE', 'last');
		anchored('DAC', 'DDF', 'first');
		anchored('DAD', 'DDG', 'middle');
		// The address block is anchored on its own structure — city ends at
		// "DAJ" + two letters + "DAK" + a ZIP — so a city like DALLAS (holds the
		// DAL tag) or a street like DAISY LN (holds DAI) is not cut in half.
		var addr = new RegExp('DAG(' + ADDR_CHARS + ')(?:DAH(' + ADDR_CHARS + '))?DAI(' + CITY_CHARS + ')DAJ([A-Z]{2})DAK(\\d{5}(?:[- ]?\\d{4})?)').exec(body);
		if (addr) {
			out.address1 = clean(addr[1]); out.address2 = clean(addr[2]); out.city = clean(addr[3]);
			out.state = addr[4]; out.zip = addr[5];
		}
		body.split(NEXT_TAG).forEach(function (chunk) {
			var key = keyOf(chunk.slice(0, 3));
			if (!key || key in out) return;
			var value = clean(chunk.slice(3));
			// Unanchored fallback (old cards without flags): same narrow classes.
			if (/^(last|first|middle|suffix|city|legacyName)$/.test(key) && /\d/.test(value)) return;
			out[key] = value;
		});
		return out;
	}

	// Returns null when the text is not an AAMVA payload (so a typed name never
	// gets mangled), else {first, middle, last, suffix, address1, address2,
	// city, state, zip, country} — plus {idNumber, dob, expiry, gender} (dates as
	// YYYY-MM-DD, gender Male / Female, '' when unreadable) when called with {ids: true}, and
	// {carryNumber, carryExpiry} too for a Texas License to Carry.
	function parse(raw, opts) {
		if (!looksLikeAamva(raw)) return null;
		var separated = (String(raw || '').match(/[\r\n\x1e]/g) || []).length >= 3;
		var e = separated ? elements(raw) : gluedElements(raw);
		var first = e.first, middle = e.middle, last = e.last, suffix = e.suffix;
		if (!first && !last && e.legacyName) {
			var parts = e.legacyName.indexOf(',') >= 0 ? e.legacyName.split(',') : e.legacyName.split(/\s+/);
			last = parts[0]; first = parts[1]; middle = parts.slice(2).join(' ');
		}
		// A comma-joined first name field ("JOHN,PAUL") carries the middle name.
		if (first && first.indexOf(',') >= 0) {
			var fp = first.split(','); first = fp[0]; middle = middle || fp.slice(1).join(' ');
		}
		// Without the ANSI header, a lone tag-shaped fragment is not evidence of
		// a scan: insist on both names.
		var hasHeader = /ANSI\s*\d{6}|AAMVA/.test(raw);
		if ((!first && !last) || (!hasHeader && !(first && last))) return null;
		var out = {
			first: titleCase(clean(first)),
			middle: titleCase(clean(middle)),
			last: titleCase(clean(last)),
			suffix: normalizeSuffix(suffix),
			address1: street(e.address1),
			address2: street(e.address2),
			city: titleCase(clean(e.city)),
			state: clean(e.state).toUpperCase().slice(0, 2),
			zip: normalizeZip(e.zip),
			country: countryName(e.country),
		};
		if (opts && opts.ids) {
			out.idNumber = idNumber(e.idNumber);
			out.dob = isoDate(e.dob);
			out.expiry = isoDate(e.expiry);
			out.gender = GENDERS[clean(e.sex).toUpperCase()] || '';
			if (clean(e.cardType).toUpperCase() === TEXAS_LTC) {
				out.carryNumber = out.idNumber;
				out.carryExpiry = out.expiry;
				out.idNumber = idNumber(clean(e.holderId).replace(/^[A-Za-z]{2}\s*(?=\d)/, ''));
				out.expiry = '';
			}
		}
		return out;
	}

	return { parse: parse, looksLikeAamva: looksLikeAamva, titleCase: titleCase, normalizeZip: normalizeZip, isoDate: isoDate };
});
