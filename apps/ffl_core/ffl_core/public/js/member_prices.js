// Member prices on the screen as soon as a line is added or the customer changes.
//
// The server prices every save (ffl_core.membership_benefits.apply_member_prices) and is the only
// authority; this shows the same answer at once, so the cart reads right before Checkout. After an
// item, its quantity or unit, or the customer changes on a draft POS Invoice / Sales Invoice, it asks
// ffl_core.membership_benefits.preview for the whole document and writes each line's answer: the
// record fields and the list price silently, the rate through set_value (ERPNext re-totals). A line
// whose member uses are spent shows the server's notice once.
//
// The pure half (rowFix) is Node-tested.
(function (root, factory) {
	var api = factory();
	if (typeof module === 'object' && module.exports) module.exports = api;
	else { root.ffl = root.ffl || {}; root.ffl.memberPrices = api; }
})(typeof self !== 'undefined' ? self : this, function () {
	'use strict';

	var NUMBERS = ['price_list_rate', 'discount_percentage', 'discount_amount', 'ffl_member_rate'];
	var TEXTS = ['ffl_member_benefit', 'ffl_membership', 'ffl_benefit_customer', 'ffl_discount_reason'];

	function num(v) { var n = Number(v); return isFinite(n) ? n : 0; }
	function text(v) { return v == null ? '' : String(v); }

	// row: the line on screen; want: preview's answer for it. → {silent: {field: value}, rate?} or null
	// when the line already reads right.
	function rowFix(row, want) {
		if (!row || !want) return null;
		var silent = {};
		NUMBERS.forEach(function (f) { if (Math.abs(num(row[f]) - num(want[f])) > 0.00001) silent[f] = num(want[f]); });
		TEXTS.forEach(function (f) { if (text(row[f]) !== text(want[f])) silent[f] = want[f] == null ? null : want[f]; });
		var fix = { silent: silent };
		if (Math.abs(num(row.rate) - num(want.rate)) > 0.00001) fix.rate = num(want.rate);
		return Object.keys(silent).length || fix.rate !== undefined ? fix : null;
	}

	return { rowFix: rowFix };
});

(function fflMemberPricesWire() {
	if (typeof window === 'undefined' || typeof module === 'object') return;
	if (!(window.frappe && frappe.ui && frappe.ui.form && frappe.ui.form.on)) return;
	var METHOD = 'ffl_core.membership_benefits.preview';
	var timers = {};
	var asked = {};
	var shown = {};

	function eligible(frm) {
		var d = frm && frm.doc;
		return d && d.docstatus === 0 && !d.is_return && !d.is_consolidated && d.customer
			&& (d.items || []).some(function (r) { return r.item_code; });
	}

	function schedule(frm) {
		if (!eligible(frm)) return;
		clearTimeout(timers[frm.doctype]);
		// After ERPNext's own re-pricing of the line (its item details / pricing calls) has landed.
		timers[frm.doctype] = setTimeout(function () { run(frm); }, 450);
	}

	async function run(frm) {
		if (!eligible(frm)) return;
		var ticket = (asked[frm.doctype] = (asked[frm.doctype] || 0) + 1);
		var out;
		try {
			out = await frappe.xcall(METHOD, { doc: frm.doc });
		} catch (e) {
			return;   // the save prices it anyway
		}
		if (!out || ticket !== asked[frm.doctype] || !eligible(frm)) return;
		var pos = window.cur_pos;
		for (var i = 0; i < (frm.doc.items || []).length; i++) {
			var row = frm.doc.items[i];
			var fix = ffl.memberPrices.rowFix(row, (out.rows || {})[row.name]);
			if (!fix) continue;
			Object.assign(row, fix.silent);
			if (fix.rate !== undefined) await frappe.model.set_value(row.doctype, row.name, 'rate', fix.rate);
			if (pos && pos.frm === frm && typeof pos.update_cart_html === 'function') pos.update_cart_html(row);
			else frm.refresh_field('items');
		}
		(out.notices || []).forEach(function (n) {
			var key = frm.doc.name + '|' + n;
			if (shown[key]) return;
			shown[key] = 1;
			frappe.show_alert({ message: n, indicator: 'orange' }, 8);
		});
	}

	['POS Invoice', 'Sales Invoice'].forEach(function (dt) {
		frappe.ui.form.on(dt, { customer: schedule });
		frappe.ui.form.on(dt + ' Item', {
			item_code: schedule,
			qty: schedule,
			uom: schedule,
			serial_no: schedule,
		});
	});
})();
