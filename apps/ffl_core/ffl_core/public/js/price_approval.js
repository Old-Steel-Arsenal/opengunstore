// A price lowered at the counter asks a manager before the save (server side:
// ffl_core/price_approval.py, which refuses the save on its own — this is the convenience).
//
// • POS Invoice / Sales Invoice / Sales Order (the register's Checkout and Complete Order, and
//   the desk forms): on validate the document goes to price_approval.check; when a price below
//   list (or an invoice discount), or a return, needs approval, one dialog: the manager picks themselves,
//   types their counter PIN (or password: "Use password instead", or when they have no PIN)
//   and a reason → price_approval.approve → a single-use token rides the save in
//   ffl_price_approval_token. A wrong PIN shows under the field; the dialog stays.
//   Cancel stops the save.
// • Create Order: the page calls ffl.priceApproval.forOrder(payload) before create_order /
//   update_order, which carries the token as payload.price_approval.
// • A manager signed in (frappe.boot.ffl_price_approval = 0), or the setting off: nothing asks.
//
// The pure half (shouldAsk, reasonText, summaryRows, approveArgs) is Node-tested.
(function (root, factory) {
	var api = factory();
	if (typeof module === 'object' && module.exports) module.exports = api;
	else { root.ffl = root.ffl || {}; root.ffl.priceApproval = api; }
})(typeof self !== 'undefined' ? self : this, function () {
	'use strict';

	var OTHER = 'Other';
	// Kept short on purpose (owner rule: fewest clicks); Other needs the details.
	var REASONS = ['Price match', 'Damaged or open box', 'Bundle deal', 'Regular customer', OTHER];
	var RETURN_REASONS = ['Defective or damaged', 'Customer changed mind', 'Wrong item sold', OTHER];

	// The reason list for a check answer: a return's own reasons, else the price ones.
	function reasonsFor(res) {
		return res && res.refund ? RETURN_REASONS : REASONS;
	}

	// Whether a check answer needs the dialog: something was lowered and the caller is no manager.
	function shouldAsk(res) {
		return !!(res && res.needs_approval && !res.approver);
	}

	// The reason sent to the server, or '' when it is not complete yet (Other needs details).
	function reasonText(preset, note) {
		preset = String(preset || '').trim();
		note = String(note || '').trim();
		if (!preset) return '';
		if (preset === OTHER) return note;
		return note ? preset + ': ' + note : preset;
	}

	// The dialog's table: one row per lowered line (list price → new), then the invoice discount.
	function summaryRows(res) {
		var rows = ((res && res.lines) || []).map(function (l) {
			return { what: (l.item_name || l.item_code) + (l.serial ? ' (' + l.serial + ')' : ''),
				from: l.list_rate, to: l.rate, kind: 'line' };
		});
		var d = res && res.discount;
		if (d) rows.push({ what: 'Invoice discount', from: null, to: d.percentage ? d.percentage + '%' : d.amount, kind: 'discount' });
		var r = res && res.refund;
		if (r) {
			(r.lines || []).forEach(function (l) {
				rows.push({ what: (l.item_name || l.item_code) + (l.serial ? ' (' + l.serial + ')' : '')
					+ (l.qty && l.qty !== 1 ? ' ×' + l.qty : ''), from: null, to: l.amount, kind: 'returned' });
			});
			rows.push({ what: 'Refund', from: null, to: r.total, kind: 'refund' });
		}
		return rows;
	}

	// What the screen says when the check itself failed: the save stops rather than meet the
	// server's refusal with no dialog. `detail` is the server's message, when there is one.
	function checkFailedMessage(detail) {
		return "Couldn't check the prices for manager approval" + (detail ? ': ' + detail : '')
			+ '. Ask a manager to sign in, or try again.';
	}

	// approve()'s arguments: the PIN or the password, never both.
	function approveArgs(approver, secret, mode, reason) {
		var out = { approver: approver, reason: reason };
		out[mode === 'pin' ? 'pin' : 'password'] = secret;
		return out;
	}

	return { REASONS: REASONS, RETURN_REASONS: RETURN_REASONS, OTHER: OTHER, reasonsFor: reasonsFor,
		shouldAsk: shouldAsk, reasonText: reasonText,
		summaryRows: summaryRows, approveArgs: approveArgs, checkFailedMessage: checkFailedMessage };
});

(function fflPriceApprovalWire() {
	if (typeof window === 'undefined' || typeof module === 'object') return;
	var api = ffl.priceApproval;
	var M = 'ffl_core.price_approval.';
	var TOKEN_FIELD = 'ffl_price_approval_token';
	var esc = function (s) { return frappe.utils.escape_html(s == null ? '' : String(s)); };
	var on = function () { return !!(frappe.boot && frappe.boot.ffl_price_approval); };

	// A plain POST: frappe.call's error pop-up would sit over the dialog, so the dialog shows it.
	function post(method, args, fallback) {
		return fetch('/api/method/' + method, {
			method: 'POST', credentials: 'same-origin',
			headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-Frappe-CSRF-Token': frappe.csrf_token },
			body: JSON.stringify(args || {}),
		}).then(function (r) {
			return r.json().catch(function () { return {}; }).then(function (body) {
				if (r.status === 429) throw new Error(__('Too many tries. Wait a few minutes.'));
				if (!r.ok) throw new Error((ffl.staffSwitch && ffl.staffSwitch.serverMessage(body)) || fallback || '');
				return body.message;
			});
		});
	}

	// The dialog. `request` is what the server prices again ({doc} or {payload, sales_invoice}).
	// Resolves the token, or null when cancelled.
	function ask(res, request) {
		return new Promise(function (resolve) {
			var done = false;
			var usePassword = false;
			var users = res.approvers || [];
			var byUser = {};
			users.forEach(function (u) { byUser[u.user] = u; });
			var d = new frappe.ui.Dialog({
				title: __('Manager approval needed'),
				fields: [
					{ fieldtype: 'HTML', fieldname: 'lines' },
					{ fieldtype: 'Select', fieldname: 'approver', label: __('Manager'), reqd: 1,
						options: [{ value: '', label: '' }].concat(users.map(function (u) { return { value: u.user, label: u.full_name }; })),
						change: function () { mode(); } },
					{ fieldtype: 'Password', fieldname: 'secret', label: __('PIN') },
					{ fieldtype: 'HTML', fieldname: 'switch' },
					{ fieldtype: 'Select', fieldname: 'reason', label: __('Reason'), reqd: 1,
						options: [''].concat(api.reasonsFor(res).map(function (r) { return { value: r, label: __(r) }; })) },
					{ fieldtype: 'Data', fieldname: 'note', label: __('Details') },
					{ fieldtype: 'HTML', fieldname: 'err' },
				],
				primary_action_label: __('Approve'),
				primary_action: function (v) {
					var authMode = currentMode();
					var reason = api.reasonText(v.reason, v.note);
					if (!v.approver) return error(__('Pick the manager approving.'));
					if (!v.secret) return error(authMode === 'pin' ? __('Type the PIN.') : __('Type the password.'));
					if (!reason) return error(__('Give a reason (Other needs the details).'));
					error('');
					d.get_primary_btn().prop('disabled', true);
					post(M + 'approve', Object.assign(api.approveArgs(v.approver, v.secret, authMode, reason), request), __('That PIN is not right.'))
						.then(function (m) {
							done = true;
							d.hide();
							frappe.show_alert({ message: __('Approved by {0}', [esc(m.approver_name || m.approver)]), indicator: 'green' });
							resolve(m.token);
						}, function (err) {
							d.set_value('secret', '');
							error(err.message);
						})
						.finally(function () { d.get_primary_btn().prop('disabled', false); });
				},
			});
			function currentMode() {
				var u = byUser[d.get_value('approver')];
				return ffl.staffSwitch ? ffl.staffSwitch.authMode(u, usePassword) : (u && u.has_pin && !u.pin_off && !usePassword ? 'pin' : 'password');
			}
			function mode() {
				var m = currentMode();
				d.fields_dict.secret.set_label(m === 'pin' ? __('PIN') : __('Password'));
				var u = byUser[d.get_value('approver')];
				d.fields_dict.switch.$wrapper.html(u && u.has_pin && !u.pin_off
					? '<a href="#" class="small ffl-pa-switch">' + esc(usePassword ? __('Use PIN instead') : __('Use password instead')) + '</a>' : '');
				d.fields_dict.switch.$wrapper.find('.ffl-pa-switch').on('click', function (e) {
					e.preventDefault();
					usePassword = !usePassword;
					d.set_value('secret', '');
					mode();
				});
			}
			function error(text) {
				d.fields_dict.err.$wrapper.html(text ? '<div class="text-danger small">' + esc(text) + '</div>' : '');
			}
			var table = api.summaryRows(res).map(function (r) {
				var money = function (v) { return typeof v === 'number' ? format_currency(v) : esc(v); };
				var bold = r.kind === 'returned' ? '' : '<b>';
				return '<tr><td>' + esc(r.kind === 'discount' || r.kind === 'refund' ? __(r.what) : r.what) + '</td><td class="text-right text-muted">'
					+ (r.from == null ? '' : money(r.from)) + '</td><td class="text-right">' + bold + money(r.to)
					+ (bold ? '</b>' : '') + '</td></tr>';
			}).join('');
			var isReturn = !!res.refund;
			d.fields_dict.lines.$wrapper.html('<p class="text-muted" style="margin-bottom:6px">'
				+ esc(isReturn ? __('This return gives money back. A manager approves it with their PIN.')
					: __('These prices are lower than the list price. A manager approves them with their PIN.')) + '</p>'
				+ '<table class="table table-sm"><thead><tr><th></th><th class="text-right">' + esc(isReturn ? '' : __('List'))
				+ '</th><th class="text-right">' + esc(isReturn ? __('Back') : __('Now')) + '</th></tr></thead><tbody>' + table + '</tbody></table>'
				+ (users.length ? '' : '<div class="text-danger small">' + esc(__('No manager is set up to approve prices.')) + '</div>'));
			d.fields_dict.secret.$input && d.fields_dict.secret.$input.attr({ autocomplete: 'off', inputmode: 'numeric' });
			d.onhide = function () { if (!done) resolve(null); };
			mode();
			d.show();
		});
	}

	// The check, as a plain POST: a failure is shown by us (checkFailedMessage), not as frappe's pop-up.
	function check(args) {
		return post(M + 'check', args).catch(function (err) {
			frappe.msgprint({ title: __('Manager approval'), indicator: 'red',
				message: esc(__(api.checkFailedMessage(err && err.message))) });
			throw err;
		});
	}

	// Form validate (POS checkout too): ask before a save that lowers a price. A check that fails
	// stops the save with the reason.
	function guardForm(frm) {
		if (!on() || frm.doc.docstatus !== 0) return;
		return check({ doc: frm.doc }).then(function (res) {
			if (!api.shouldAsk(res)) return;
			return ask(res, { doc: frm.doc }).then(function (token) {
				if (!token) { frappe.validated = false; return; }
				frm.doc[TOKEN_FIELD] = token;
			});
		}, function () { frappe.validated = false; });
	}

	// Create Order / an order edit: true to go on (payload.price_approval set when one was given);
	// false when cancelled or the check failed (said so already).
	function forOrder(payload, salesInvoice) {
		delete payload.price_approval;
		if (!on()) return Promise.resolve(true);
		return check({ payload: payload, sales_invoice: salesInvoice || undefined }).then(function (res) {
			if (!api.shouldAsk(res)) return true;
			return ask(res, { payload: payload, sales_invoice: salesInvoice || undefined }).then(function (token) {
				if (!token) return false;
				payload.price_approval = token;
				return true;
			});
		}, function () { return false; });
	}

	['POS Invoice', 'Sales Invoice', 'Sales Order'].forEach(function (dt) {
		frappe.ui.form.on(dt, { validate: guardForm });
	});
	api.forOrder = forOrder;
	api.guardForm = guardForm;
})();
