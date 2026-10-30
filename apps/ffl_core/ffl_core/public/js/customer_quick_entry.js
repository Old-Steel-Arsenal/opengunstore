// New Customer quick-entry (POS "+ new customer" and Desk "+" on Customer):
// for an Individual the clerk types the bound-book name parts ONCE —
// First / Middle / Last / Suffix on top (First + Last required), Customer Name
// composed live and read-only; a Company keeps the single Customer Name.
// A "Scan ID" box takes the PDF417 payload from the licence's back (keyboard-
// wedge scanner) and fills name + address through ffl.aamva. Licence number,
// date of birth and expiry are read ONLY when the store keeps them (FFL
// Settings → Store Customer ID Numbers, frappe.boot.ffl_store_customer_id_numbers):
// then the dialog shows Date of Birth / Driver License # / License Expiry and
// the licence becomes a Driver License row in customer_ids. Switch off, the
// dialog is exactly what it was before the switch existed.
//
// "Add family member" (Customer form → Family) opens this same dialog with
// route_options {customer_type: 'Individual', primary_customer, ffl_last_name};
// primary_customer on the doc is the signal: the title names the family, a
// Relationship select appears (and only then), and the type stays Individual.
// ERPNext's dialog otherwise lets the Customer Type control's default overwrite
// the type route_options put on the doc (the dialog opened as Company): the type
// the caller asked for becomes the default on the dialog's own docfield copy
// (arrangeFields opts.type) and get_field_default_value hands that copy's
// default to FieldGroup.make, which writes it before anything else can.
//
// Server twin: ffl_core.customer_name.sync_individual_name (compose / split
// on save) so the rule holds for every creation path, not just this dialog.
// Upgrade-safe: subclasses ERPNext's CustomerQuickEntryForm (trapped via
// ffl.installClassHook like the POS patches), never edits it.
//
// Lives outside ffl_core.bundle.js (unlike Item/SupplierQuickEntryForm) on
// purpose: the UMD half below is required by Node tests, and customer_form.js
// reads ffl.customerQuickEntry.composeName / splitDisplayName from it.
(function (root, factory) {
	var api = factory();
	if (typeof module === 'object' && module.exports) module.exports = api;
	else { root.ffl = root.ffl || {}; root.ffl.customerQuickEntry = api; }
})(typeof self !== 'undefined' ? self : this, function () {
	'use strict';

	var INDIVIDUAL = "eval:doc.customer_type=='Individual'";
	var PARTS = ['ffl_first_name', 'middle_name', 'ffl_last_name', 'name_suffix'];
	var SCAN_FIELD = 'ffl_scan_id';
	// Pinned to setup/customer_identity._SUFFIX_OPTIONS by test_customer_name.
	var SUFFIXES = { jr: 'Jr', sr: 'Sr', ii: 'II', iii: 'III', iv: 'IV', v: 'V', vi: 'VI' };
	// Parsed AAMVA key → dialog field. The address fields are ERPNext's own
	// ContactAddressQuickEntryForm variant fields — note `country_address`,
	// not `country` (same trap SupplierQuickEntryForm documents).
	var SCAN_MAP = {
		first: 'ffl_first_name', middle: 'middle_name', last: 'ffl_last_name', suffix: 'name_suffix',
		address1: 'address_line1', address2: 'address_line2', city: 'city', state: 'state',
		zip: 'pincode', country: 'country_address',
	};
	// Only present when parse() was asked for IDs (switch on). date_of_birth is
	// the real Customer field; the other two are dialog-only and become a
	// customer_ids row in applyIdRow.
	var ID_SCAN_MAP = { dob: 'date_of_birth', idNumber: 'ffl_id_number', expiry: 'ffl_id_expiry' };
	var ID_NUMBER_FIELD = 'ffl_id_number';
	var ID_EXPIRY_FIELD = 'ffl_id_expiry';
	var SCANNED_ID_TYPE = 'Driver License';

	// "First Middle Last Suffix" from the parts (JS twin of
	// ffl_core.customer_name.compose_display_name; customer_form.js uses it too).
	function composeName(doc) {
		return PARTS.map(function (f) { return String((doc || {})[f] || '').trim(); })
			.filter(Boolean).join(' ');
	}

	// Twin of ffl_core.customer_name.split_display_name: a generational suffix
	// is pulled out even mid-name; a single token is a first name only.
	function splitDisplayName(name) {
		var suffix = '', core = [];
		String(name || '').split(/\s+/).filter(Boolean).forEach(function (tok) {
			var canonical = SUFFIXES[tok.toLowerCase().replace(/\./g, '')];
			if (!suffix && canonical && core.length) suffix = canonical;
			else core.push(tok);
		});
		return {
			ffl_first_name: core[0] || '',
			middle_name: core.length > 2 ? core.slice(1, -1).join(' ') : '',
			ffl_last_name: core.length > 1 ? core[core.length - 1] : '',
			name_suffix: suffix,
		};
	}

	// Title of the dialog opened from a customer's Family section.
	function familyTitle(name, L) {
		return (L || function (s) { return s; })('Add family member to {0}', [name]);
	}

	// Reorder / annotate the quick-entry docfields (copies — never the cached
	// meta): [customer_type, Scan ID, parts…, customer_name, rest…]; with
	// opts.family ({relationshipOptions}) a Relationship select follows
	// customer_name, with opts.ids (switch on) the three licence fields follow;
	// opts.type (the type the caller opened the dialog with) is the type
	// field's default.
	function arrangeFields(docfields, labels, opts) {
		var L = labels || function (s) { return s; };
		var byName = {};
		(docfields || []).forEach(function (df) { byName[df.fieldname] = df; });
		var used = {};
		function take(name, patch) {
			var df = byName[name];
			if (!df) return null;
			used[name] = true;
			return Object.assign({}, df, patch || {});
		}
		var out = [];
		var type = take('customer_type', opts && opts.type ? { default: opts.type } : null);
		if (type) out.push(type);
		out.push({
			fieldname: SCAN_FIELD, fieldtype: 'Small Text', label: L('Scan ID'),
			depends_on: INDIVIDUAL,
			description: L('Scan the barcode on the back of a driver license or state ID to fill name and address.'),
		});
		var partPatch = { description: '' };
		var first = take('ffl_first_name', Object.assign({ mandatory_depends_on: INDIVIDUAL }, partPatch));
		var middle = take('middle_name', partPatch);
		var last = take('ffl_last_name', Object.assign({ mandatory_depends_on: INDIVIDUAL }, partPatch));
		var suffix = take('name_suffix', partPatch);
		[first, middle, last, suffix].forEach(function (df) { if (df) out.push(df); });
		var name = take('customer_name', {
			read_only_depends_on: INDIVIDUAL,
			description: L('Built from the name parts above.'),
		});
		if (name) out.push(name);
		if (opts && opts.family) {
			out.push({ fieldname: 'relationship', fieldtype: 'Select', label: L('Relationship'),
				options: opts.family.relationshipOptions || '' });
		}
		if (opts && opts.ids) {
			out.push(take('date_of_birth', { depends_on: INDIVIDUAL })
				|| { fieldname: 'date_of_birth', fieldtype: 'Date', label: L('Date of Birth'), depends_on: INDIVIDUAL });
			out.push({ fieldname: ID_NUMBER_FIELD, fieldtype: 'Data', label: L('Driver License #'), depends_on: INDIVIDUAL });
			out.push({ fieldname: ID_EXPIRY_FIELD, fieldtype: 'Date', label: L('License Expiry'), depends_on: INDIVIDUAL });
		}
		(docfields || []).forEach(function (df) { if (!used[df.fieldname]) out.push(df); });
		return out;
	}

	// Values to set from a parsed scan: only non-empty ones, mapped to fields.
	// `previous` = the NON-EMPTY values an earlier partial parse of the same
	// scan wrote; a field in it that this parse no longer carries comes back
	// as '' so a mid-value cut ("DADNO" → middle "No") cannot outlive the full
	// payload. Fields never filled are simply absent (no '' churn per tick).
	function scanValues(parsed, previous) {
		var out = {};
		if (!parsed) return out;
		Object.keys(SCAN_MAP).forEach(function (k) {
			if (parsed[k]) out[SCAN_MAP[k]] = parsed[k];
		});
		Object.keys(ID_SCAN_MAP).forEach(function (k) {
			if (parsed[k]) out[ID_SCAN_MAP[k]] = parsed[k];
		});
		Object.keys(previous || {}).forEach(function (f) {
			if (previous[f] && !(f in out)) out[f] = '';
		});
		return out;
	}

	// The Scan ID box is dialog-only: whatever is left in it (a failed or
	// half-finished scan can hold the licence number and DOB) must never ride
	// along in the posted doc. Applied on the doc after update_doc().
	function stripScan(doc) {
		if (doc && SCAN_FIELD in doc) delete doc[SCAN_FIELD];
		return doc;
	}

	// Switch on: turn the dialog-only licence fields into ONE customer_ids row
	// and drop them from the doc. update_doc() runs more than once per save
	// (the primary action calls it, then insert() calls it again), so the row
	// is marked and updated in place, never appended twice; clearing the number
	// removes it. `addRow(doc)` makes the child (frappe.model.add_child in the
	// browser). The server still blanks the number if the switch is off.
	// The three licence fields are hidden unless the type is Individual, so a
	// scan followed by a switch to Company leaves values the clerk cannot see:
	// those are dropped (and a row made earlier removed), never written.
	function applyIdRow(doc, addRow) {
		if (!doc) return doc;
		var number = String(doc[ID_NUMBER_FIELD] || '').trim();
		var expiry = doc[ID_EXPIRY_FIELD] || null;
		delete doc[ID_NUMBER_FIELD];
		delete doc[ID_EXPIRY_FIELD];
		var rows = doc.customer_ids || [];
		var row = rows.filter(function (r) { return r && r.__ffl_scanned; })[0];
		if (doc.customer_type !== 'Individual') {
			delete doc.date_of_birth;
			number = '';
		}
		if (!number) {
			if (row) doc.customer_ids = rows.filter(function (r) { return r !== row; });
			return doc;
		}
		if (!row) {
			row = addRow(doc);
			row.__ffl_scanned = 1;
		}
		row.id_type = SCANNED_ID_TYPE;
		row.id_number = number;
		row.expiry_date = expiry;
		return doc;
	}

	return {
		INDIVIDUAL: INDIVIDUAL, PARTS: PARTS, SCAN_FIELD: SCAN_FIELD, SCAN_MAP: SCAN_MAP, SUFFIXES: SUFFIXES,
		ID_SCAN_MAP: ID_SCAN_MAP, ID_NUMBER_FIELD: ID_NUMBER_FIELD,
		composeName: composeName, splitDisplayName: splitDisplayName, arrangeFields: arrangeFields, familyTitle: familyTitle,
		scanValues: scanValues, stripScan: stripScan, applyIdRow: applyIdRow,
	};
});

// Browser wiring: subclass ERPNext's CustomerQuickEntryForm the instant the
// erpnext bundle assigns it (ffl.installClassHook from pos_tender_guard.js —
// the same trap the POS patches use), with the same bounded fallback retry.
(function fflCustomerQuickEntryWire() {
	if (typeof window === 'undefined' || typeof module === 'object') return;
	var api = ffl.customerQuickEntry;

	function applyPatch(Base) {
		var F = frappe.ui.form;
		if (!Base || Base.__fflCustomerQuickEntry) return;

		var Patched = class FFLCustomerQuickEntryForm extends Base {
			render_dialog() {
				// Read once per dialog: the FFL Settings switch (boot key absent = off).
				this.__fflIds = !!(frappe.boot && frappe.boot.ffl_store_customer_id_numbers);
				// Family flow: Family Of is already on the new doc (route_options).
				var relDf = this.__fflIsFamilyFlow() && frappe.meta.get_docfield('Customer', 'relationship');
				var family = relDf ? { relationshipOptions: relDf.options || '' } : null;
				this.docfields = api.arrangeFields(this.docfields, __, {
					ids: this.__fflIds, family: family, type: this.doc && this.doc.customer_type });
				super.render_dialog();
				this.__fflBindNameParts();
				this.__fflBindScan();
				this.__fflGuardPosScanner();
				this.__fflApplyType();
				this.__fflFocusScan();
			}

			__fflIsFamilyFlow() {
				return !!(this.doc && this.doc.__islocal && this.doc.primary_customer);
			}

			// Layout re-points every control's df at the Customer meta docfield, and
			// FieldGroup.make then writes that df's default over the doc, so the
			// default on our docfield copy would be lost. Hand it over here: the
			// caller's type wins, deterministically, with no timer.
			get_field_default_value(field) {
				if (field.df.fieldname === 'customer_type') {
					var mine = (this.docfields || []).filter(function (df) { return df.fieldname === 'customer_type'; })[0];
					if (mine && mine.default) return mine.default;
				}
				return super.get_field_default_value(field);
			}

			get_title() {
				if (!this.__fflIsFamilyFlow()) return super.get_title();
				var primary = this.doc.primary_customer;
				var who = (frappe.model.get_value && frappe.model.get_value('Customer', primary, 'customer_name')) || primary;
				return api.familyTitle(who, __);
			}

			// The POS page runs onScan.js on `document`: any fast keystroke burst
			// ending in Enter is treated as an item barcode — it focuses the item
			// search and dumps the buffer there. A licence payload starts with
			// "@" + Enter, so the whole card would land in the item search (real
			// scan, 2026-09-09). While this dialog is open, scans typed into a
			// textarea are left alone; restored on hide.
			__fflGuardPosScanner() {
				var data = document.scannerDetectionData;
				if (!(window.onScan && data && data.options)) return;
				var previous = data.options.ignoreIfFocusOn;
				onScan.setOptions(document, { ignoreIfFocusOn: 'textarea' });
				var me = this, stockOnHide = me.onhide;
				me.onhide = function () {
					try { onScan.setOptions(document, { ignoreIfFocusOn: previous }); } catch (e) { /* never break hide */ }
					return stockOnHide && stockOnHide.apply(me, arguments);
				};
			}

			// The scanner types wherever the caret is: land it in the scan box on
			// open, and if a scan starts ("@" is its first character — no name,
			// email or address begins with one) while another empty field has
			// focus, move the caret before the character is inserted.
			__fflFocusScan() {
				var me = this, scan = me.fields_dict[api.SCAN_FIELD];
				if (!scan || !scan.$input) return;
				if (me.doc.customer_type === 'Individual') setTimeout(function () { scan.$input.focus(); }, 150);
				me.$wrapper.on('keydown', function (e) {
					if (e.key === 'Enter' && me.__fflScanQuietUntil && Date.now() < me.__fflScanQuietUntil) {
						e.preventDefault(); e.stopImmediatePropagation();   // trailing terminator, not a save
						return;
					}
					if (e.key !== '@' || me.doc.customer_type !== 'Individual') return;
					var active = document.activeElement;
					if (!active || active === scan.$input[0] || !/^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) return;
					if ((active.value || '') !== '') return;
					scan.$input.focus();
				});
			}

			// mandatory_depends_on / read_only_depends_on are re-evaluated by
			// Layout.refresh_dependency, which only runs off DOM change events;
			// programmatic set_value (the POS default type, a scan, our own
			// composition) never triggers it. Applying the two rules here on
			// every type change makes them hold on both paths.
			__fflApplyType() {
				var individual = this.doc.customer_type === 'Individual';
				if (typeof this.toggle_reqd === 'function') this.toggle_reqd(['ffl_first_name', 'ffl_last_name'], individual);
				this.set_df_property('customer_name', 'read_only', individual ? 1 : 0);
				if (individual && !api.composeName(this.doc) && (this.doc.customer_name || '').trim()) {
					// The Customer link prefilled customer_name with what the clerk
					// typed before "Create New": seed the parts from it instead of
					// blanking it on the flip to Individual.
					var seeded = api.splitDisplayName(this.doc.customer_name);
					var me = this;
					Object.keys(seeded).forEach(function (f) { me.__fflSetIf(f, seeded[f]); });
				}
				this.__fflSetName();
			}

			__fflSetName() {
				if (this.doc.customer_type !== 'Individual') return;
				var composed = api.composeName(this.doc);
				if ((this.doc.customer_name || '') !== composed) this.set_value('customer_name', composed);
			}

			// Same shape as SupplierQuickEntryForm's setIf: through the control
			// when the field is in the dialog, else straight onto the doc.
			__fflSetIf(fieldname, value) {
				if (value === null || value === undefined || value === '') return;
				if (this.fields_dict[fieldname]) this.set_value(fieldname, value);
				else this.doc[fieldname] = value;
			}

			// Controls call df.onchange for typed AND programmatic changes; the
			// dialog clones the docfields, so bind on the live df objects after
			// render. The 'input' binding only adds live composition while typing.
			__fflBindNameParts() {
				var me = this;
				var type = me.fields_dict.customer_type;
				if (type) type.df.onchange = function () { me.__fflApplyType(); };
				api.PARTS.forEach(function (f) {
					var field = me.fields_dict[f];
					if (!field) return;
					field.df.onchange = function () { me.__fflSetName(); };
					if (field.$input) field.$input.on('input', function () {
						me.doc[f] = field.get_value();
						me.__fflSetName();
					});
				});
			}

			// Keyboard-wedge scanners type fast and end with Enter; a Small Text
			// keeps the payload's newlines (Enter in a textarea does not submit
			// the dialog). Two clocks, both driven by KEYSTROKES (never by parse
			// results): a short tick re-parses everything typed so far, and a
			// long quiet period finishes the scan. A 400-char licence takes 1–3 s
			// to type and the stream can pause longer than a tick (HID chunking,
			// main-thread jank), so a tick is NOT the end of the scan — the name
			// comes first on the card and the address later, a partial parse only
			// fills the name. Real-store bug 2026-09-19: the first tick cleared
			// the box mid-scan, the tail (no header, no name) parsed to null, the
			// address was lost and leftovers typed into Mobile Number.
			__fflBindScan() {
				var me = this, field = me.fields_dict[api.SCAN_FIELD];
				if (!field || !field.$input) return;
				field.$input.attr('placeholder', __('Scan ID…')).attr('rows', 2).css({ height: '56px', minHeight: '56px' });
				field.$input.on('input', function () {
					clearTimeout(me.__fflScanTickTimer);
					me.__fflScanTickTimer = setTimeout(function () { me.__fflApplyScan(field.get_value()); }, SCAN_TICK_MS);
					// Once a parse has succeeded every further keystroke pushes the
					// finish (and the Enter swallow) out again — a long unbroken run
					// of address characters never sees a tick.
					if (me.__fflScanFilled) me.__fflArmScanFinish();
				});
				// Save/close inside the quiet window: never finish on a dead dialog
				// (alert after the fact, focus() stealing the POS caret).
				var stockOnHide = me.onhide;
				me.onhide = function () {
					clearTimeout(me.__fflScanTickTimer); clearTimeout(me.__fflScanFinishTimer);
					return stockOnHide && stockOnHide.apply(me, arguments);
				};
			}

			// The scanner's terminator (CR / CR+LF) can trail the payload; an
			// Enter landing in a text input is "save" for a Frappe dialog (real
			// scan 2026-09-09 auto-saved before phone/email were typed). Swallow
			// Enter and hold the box until SCAN_QUIET_MS pass with no keystroke.
			__fflArmScanFinish() {
				var me = this;
				me.__fflScanQuietUntil = Date.now() + SCAN_QUIET_MS;
				clearTimeout(me.__fflScanFinishTimer);
				me.__fflScanFinishTimer = setTimeout(function () { me.__fflFinishScan(); }, SCAN_QUIET_MS);
			}

			// Parse the accumulated payload; later ticks override earlier ones,
			// and a field an earlier partial tick filled that the fuller payload
			// no longer carries is cleared (scanValues' `previous`). Unchanged
			// fields are skipped — every set_value costs a dependency refresh and
			// an onchange cascade, and a scan yields several ticks.
			__fflApplyScan(raw) {
				var parsed = window.ffl && ffl.aamva && ffl.aamva.parse(raw, { ids: this.__fflIds });
				if (!parsed) return;
				var me = this, values = api.scanValues(parsed, me.__fflScanFilled), filled = {};
				Object.keys(values).forEach(function (f) {
					if (values[f] !== '') filled[f] = values[f];
					if ((me.doc[f] || '') === values[f]) return;
					// Same two branches as __fflSetIf, but '' is a real write here.
					if (me.fields_dict[f]) me.set_value(f, values[f]);
					else me.doc[f] = values[f];
				});
				me.__fflScanFilled = filled;
				me.__fflSetName();
				me.__fflArmScanFinish();
			}

			// Clear the box, announce, hand the caret to Mobile Number — the clerk
			// saves explicitly.
			__fflFinishScan() {
				var me = this;
				me.__fflScanFilled = null;
				me.set_value(api.SCAN_FIELD, '');
				frappe.show_alert({
					message: __('ID scanned: {0}', [frappe.utils.escape_html(api.composeName(me.doc))]),
					indicator: 'green',
				});
				var next = me.fields_dict.mobile_number;
				if (next && next.$input) next.$input.focus();
			}

			// update_doc() copies every non-empty dialog value onto the doc right
			// before frappe.client.save — strip the scan box there, so nothing
			// left in it (licence number, DOB) can reach the server or an
			// Error Log traceback.
			update_doc() {
				var doc = super.update_doc();
				if (this.__fflIds) {
					api.applyIdRow(this.doc, function (d) { return frappe.model.add_child(d, 'customer_ids'); });
				}
				api.stripScan(this.doc);
				if (this.dialog && this.dialog.doc !== this.doc) api.stripScan(this.dialog.doc);
				return doc;
			}

			// Before the save: customers that already hold this email, phone or (IDs
			// on) licence number (ffl.customerFlags.confirmNotDuplicate). "Use this
			// customer" hands the existing one to whoever opened the dialog, the way
			// a save would; "Create new anyway" saves as before. Asked once per dialog.
			insert() {
				var me = this, cf = window.ffl && ffl.customerFlags;
				if (me.__fflDupAsked || !(cf && cf.confirmNotDuplicate) || me.__fflIsFamilyFlow()) return super.insert();
				var get = function (f) { return me.dialog.fields_dict[f] ? (me.dialog.get_value(f) || '') : ''; };
				var values = { email: get('email_address'), phone: get('mobile_number'),
					id_number: me.__fflIds ? get(api.ID_NUMBER_FIELD) : '' };
				var stock = function () { return Base.prototype.insert.call(me); };
				return cf.confirmNotDuplicate(values).then(function (answer) {
					me.dialog.working = false;
					if (answer.use) {
						me.dialog.hide();
						var doc = { doctype: 'Customer', name: answer.use };
						if (frappe._from_link) frappe.ui.form.update_calling_link(doc);
						else if (me.after_insert) me.after_insert(doc);
						return new Promise(function () {});   // no "saved" toast: nothing was saved
					}
					if (answer.cancel) return new Promise(function () {});
					me.__fflDupAsked = true;
					me.dialog.working = true;
					return stock();
				}, stock);
			}
		};
		// Flag BEFORE assigning: under the installClassHook trap the assignment
		// re-enters applyPatch with the subclass, which must be a no-op.
		Patched.__fflCustomerQuickEntry = true;
		F.CustomerQuickEntryForm = Patched;
	}

	var SCAN_TICK_MS = 250;    // re-parse cadence while the scanner is typing
	var SCAN_QUIET_MS = 1200;  // no keystroke for this long after a parse = scan finished

	function fallbackRetry(left) {
		var F = window.frappe && frappe.ui && frappe.ui.form;
		if (F && F.CustomerQuickEntryForm) { applyPatch(F.CustomerQuickEntryForm); return; }
		if (left > 0) setTimeout(function () { fallbackRetry(left - 1); }, 250);
	}
	function wire() {
		if (!window.frappe || typeof frappe.provide !== 'function') return;
		frappe.provide('frappe.ui.form');
		var hook = window.ffl && ffl.installClassHook;
		if (hook && hook(frappe.ui.form, 'CustomerQuickEntryForm', applyPatch)) return;
		fallbackRetry(60);
	}
	if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
	else wire();
})();
