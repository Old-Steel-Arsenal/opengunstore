// This computer's card reader (ffl_integrations/payments/card_readers.py). A store that lists
// several card readers has each computer pick its own once; the choice lives in this browser
// (localStorage) and every terminal charge names it. No readers listed (frappe.boot
// .ffl_card_readers empty or absent): nothing here shows or asks, and charges go out exactly
// as before. choose() is the pure rule (Node-tested); the rest is the picker dialog.
(function (factory) {
	'use strict';
	var api = factory();
	if (typeof module !== 'undefined' && module.exports) module.exports = api;   // Node test
	if (typeof window !== 'undefined') {                                          // desk
		window.ffl = window.ffl || {};
		window.ffl.cardReader = api;
	}
})(function () {
	'use strict';

	var STORAGE_KEY = 'ffl-card-reader';
	// The server's refusals that mean "this computer's choice is no good": forget it.
	var REFUSALS = ['CardReaderRequired', 'CardReaderUnavailable'];

	// readers: the enabled readers' names; stored: this computer's remembered choice.
	// → {reader, ask}: reader '' = no readers (the processor's own device); a name = use it;
	// ask = several readers and none chosen here yet (never fall back to one: it may be on
	// another counter). A single enabled reader is used without asking, and not remembered,
	// so adding a second one later makes every computer choose.
	function choose(readers, stored) {
		var list = (readers || []).filter(Boolean);
		if (!list.length) return { reader: '', ask: false };
		if (stored && list.indexOf(stored) !== -1) return { reader: stored, ask: false };
		if (list.length === 1) return { reader: list[0], ask: false };
		return { reader: null, ask: true };
	}

	function list() {
		var b = (typeof frappe !== 'undefined' && frappe.boot) || {};
		return Array.isArray(b.ffl_card_readers) ? b.ffl_card_readers : [];
	}

	function stored() {
		try { return localStorage.getItem(STORAGE_KEY) || ''; } catch (e) { return ''; }
	}

	function remember(name) {
		try { localStorage.setItem(STORAGE_KEY, name); } catch (e) { /* no storage */ }
	}

	function forget() {
		try { localStorage.removeItem(STORAGE_KEY); } catch (e) { /* no storage */ }
	}

	var hooked = false;
	function hookRefusals() {
		if (hooked || typeof frappe === 'undefined' || !frappe.request || !frappe.request.error_handlers) return;
		hooked = true;
		REFUSALS.forEach(function (t) {
			(frappe.request.error_handlers[t] = frappe.request.error_handlers[t] || []).push(forget);
		});
	}

	// The reader in use here without asking: a name, '' (no readers), or null (must choose).
	function current() {
		return choose(list(), stored()).reader;
	}

	// One click: a button per reader. Resolves the chosen name (remembered), or null when closed.
	function pick() {
		var names = list();
		var now = current();
		var esc = frappe.utils.escape_html;
		return new Promise(function (resolve) {
			var done = false;
			var dlg = new frappe.ui.Dialog({
				title: __('Card reader'),
				fields: [{ fieldtype: 'HTML', fieldname: 'readers', options: names.map(function (n, i) {
					return '<button type="button" class="btn btn-lg btn-block ffl-card-reader-option '
						+ (n === now ? 'btn-primary' : 'btn-default') + '" data-i="' + i + '" '
						+ 'style="margin:0 0 10px;padding:14px;font-size:16px">' + esc(n) + '</button>';
				}).join('') }],
			});
			dlg.$wrapper.find('.ffl-card-reader-option').on('click', function () {
				var name = names[Number(this.getAttribute('data-i'))];
				remember(name);
				done = true;
				dlg.hide();
				resolve(name);
			});
			dlg.$wrapper.on('hidden.bs.modal', function () { if (!done) { done = true; resolve(null); } });
			dlg.show();
		});
	}

	// Before a terminal charge: the reader to name ('' = none listed), asking once when this
	// computer has not chosen; null = the cashier closed the picker (no charge).
	function ensure() {
		hookRefusals();
		var r = current();
		return r === null ? pick() : Promise.resolve(r);
	}

	// The waiting dialog's line: the reader's name, a link to change it when there is a choice.
	function label(name) {
		if (!name) return '';
		var esc = frappe.utils.escape_html;
		return list().length > 1
			? '<a href="#" class="ffl-card-reader-switch" style="font-weight:600;text-decoration:underline">' + esc(name) + '</a>'
			: '<span style="font-weight:600">' + esc(name) + '</span>';
	}

	return { choose: choose, list: list, current: current, pick: pick, ensure: ensure,
		forget: forget, label: label, STORAGE_KEY: STORAGE_KEY, REFUSALS: REFUSALS };
});
