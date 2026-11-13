// Member prices on the screen as soon as a line is added or the customer changes.
//
// The server prices every save (ffl_core.membership_benefits.apply_member_prices) and is the only
// authority; this shows the same answer at once, so the cart reads right before Checkout. After an
// item, its quantity, unit or price, a removed line, or the customer changes on a draft POS Invoice /
// Sales Invoice, it asks ffl_core.membership_benefits.preview for the whole document and writes each
// line's answer: the record fields and the list price silently, the rate through set_value (ERPNext
// re-totals). A line whose member uses are spent shows the server's notice once.
//
// ffl.memberPrices.sync(frm) runs that now and resolves to {changed, failed}: changed when a line's
// rate moved. The register calls it before Complete Order (ffl_core.bundle.js wrapSubmitInvoice) and
// does not complete a sale whose total just changed: a submit is never repriced on the server.
//
// The pure half (rowFix, plan, changeMessage) is Node-tested.
(function (root, factory) {
	var api = factory();
	if (typeof module === 'object' && module.exports) module.exports = api;
	else { root.ffl = root.ffl || {}; root.ffl.memberPrices = Object.assign(root.ffl.memberPrices || {}, api); }
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

	// items: the lines on screen; rows: preview's {line name: answer}. → [{row, fix}] for the lines
	// that need writing (lines the answer does not name are left alone).
	function plan(items, rows) {
		var out = [];
		(items || []).forEach(function (row) {
			var fix = rowFix(row, (rows || {})[row && row.name]);
			if (fix) out.push({ row: row, fix: fix });
		});
		return out;
	}

	// fixes: plan()'s list. → what to tell the cashier when the total moved, or null when no rate did.
	// A line that gained a member price says so; one that lost it (back to its regular price) too.
	function changeMessage(fixes) {
		var moved = (fixes || []).filter(function (f) { return f.fix.rate !== undefined; });
		if (!moved.length) return null;
		function member(f) {
			return !!('ffl_member_benefit' in f.fix.silent ? f.fix.silent.ffl_member_benefit : f.row.ffl_member_benefit);
		}
		var gained = moved.some(member);
		var lost = moved.some(function (f) { return !member(f); });
		if (gained && !lost) return 'The total changed to the member price. Collect the new total, then press Complete Order again.';
		if (lost && !gained) return 'The total changed back to the regular price. Collect the new total, then press Complete Order again.';
		return 'The total changed: member prices were updated. Collect the new total, then press Complete Order again.';
	}

	return { rowFix: rowFix, plan: plan, changeMessage: changeMessage };
});

(function fflMemberPricesWire() {
	if (typeof window === 'undefined' || typeof module === 'object') return;
	if (!(window.frappe && frappe.ui && frappe.ui.form && frappe.ui.form.on)) return;
	var METHOD = 'ffl_core.membership_benefits.preview';
	var timers = {};
	var asked = {};
	var latest = {};
	var shown = {};

	function eligible(frm) {
		var d = frm && frm.doc;
		return d && d.docstatus === 0 && !d.is_return && !d.is_consolidated && d.customer
			&& (d.items || []).some(function (r) { return r.item_code; });
	}

	function schedule(frm) {
		if (!eligible(frm)) return;
		clearTimeout(timers[frm.doctype]);
		// Usually after ERPNext's own re-pricing of the line; when its reply lands later, the
		// price_list_rate it writes schedules another look, and Complete Order syncs regardless.
		timers[frm.doctype] = setTimeout(function () { sync(frm); }, 450);
	}

	// Ask the server now and write its answer. Resolves to {changed: a line's rate moved, failed: the
	// call did not answer}. A newer call in flight answers for this one.
	function sync(frm) {
		if (!frm) return Promise.resolve({ changed: false, failed: false });
		clearTimeout(timers[frm.doctype]);
		var p = run(frm);
		latest[frm.doctype] = p;
		return p;
	}

	async function run(frm) {
		if (!eligible(frm)) return { changed: false, failed: false };
		var dt = frm.doctype;
		var ticket = (asked[dt] = (asked[dt] || 0) + 1);
		var out;
		try {
			out = await frappe.xcall(METHOD, { doc: frm.doc });
		} catch (e) {
			return { changed: false, failed: true };   // the save prices it anyway
		}
		if (ticket !== asked[dt]) return latest[dt];   // a newer look was asked for: its answer counts
		if (!out || !eligible(frm)) return { changed: false, failed: !out };
		var fixes = ffl.memberPrices.plan(frm.doc.items, out.rows);
		var pos = window.cur_pos;
		for (var i = 0; i < fixes.length; i++) {
			var row = fixes[i].row, fix = fixes[i].fix;
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
		var message = ffl.memberPrices.changeMessage(fixes);
		return { changed: !!message, failed: false, message: message };
	}

	ffl.memberPrices.sync = sync;

	['POS Invoice', 'Sales Invoice'].forEach(function (dt) {
		// items_remove: a removed line may free a daily use for another (the Desk grid; the register
		// sets the line's qty to 0 first, which the qty handler below catches).
		frappe.ui.form.on(dt, { customer: schedule, items_remove: schedule });
		frappe.ui.form.on(dt + ' Item', {
			item_code: schedule,
			qty: schedule,
			uom: schedule,
			conversion_factor: schedule,
			serial_no: schedule,
			// ERPNext's item details / pricing reply landing after the look above: look again. This
			// module writes price_list_rate silently, so it never wakes itself.
			price_list_rate: schedule,
		});
	});
})();
