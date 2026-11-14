// A gun held for a CRM deal, at the counter (server side: ffl_core/crm/holds.py, which refuses the
// sale on its own — this is the convenience).
//
// • POS Invoice / Sales Invoice (the register's Checkout, the desk forms): on validate the document
//   goes to ffl_core.api.crm.check_sale; a gun held for another customer opens "Gun held for a deal".
//   Release and sell asks for the manager (their PIN, or nothing when a manager is signed in) and a
//   reason, ends the hold (override_hold) and the save goes on. Cancel stops the save.
// • Create Order: a scan the server refuses with DealHoldError asks the same, then scans again.
//
// The pure half (REASONS, reasonText, overrideArgs) is Node-tested.
(function (root, factory) {
	var api = factory();
	if (typeof module === 'object' && module.exports) module.exports = api;
	else { root.ffl = root.ffl || {}; root.ffl.dealHold = api; }
})(typeof self !== 'undefined' ? self : this, function () {
	'use strict';

	var OTHER = 'Other';
	var REASONS = ['Customer did not come back', 'Deal owner agreed', OTHER];

	// The reason sent to the server, or '' when it is not complete (Other needs the details).
	function reasonText(preset, note) {
		preset = String(preset || '').trim();
		note = String(note || '').trim();
		if (!preset) return '';
		return preset === OTHER ? note : preset;
	}

	// override_hold's arguments: no manager fields when one is signed in; else the PIN or the password.
	function overrideArgs(serial, reason, selfApproves, approver, secret, mode) {
		var out = { serial: serial, reason: reason };
		if (!selfApproves) {
			out.approver = approver;
			out[mode === 'pin' ? 'pin' : 'password'] = secret;
		}
		return out;
	}

	return { OTHER: OTHER, REASONS: REASONS, reasonText: reasonText, overrideArgs: overrideArgs };
});

(function fflDealHoldWire() {
	if (typeof window === 'undefined' || typeof module === 'object') return;
	var api = ffl.dealHold;
	var M = 'ffl_core.api.crm.';
	var esc = function (s) { return frappe.utils.escape_html(s == null ? '' : String(s)); };

	function post(method, args) {
		return ffl.staffSwitch.post(method, args, '').then(function (body) { return body.message; });
	}

	// The dialog. `info` = {blocked: [counter_info], self_approves, approvers}. Resolves true once
	// every hold is released, false when cancelled.
	function ask(info) {
		return new Promise(function (resolve) {
			var done = false, open = false, usePassword = false;
			var users = info.approvers || [];
			var byUser = {};
			users.forEach(function (u) { byUser[u.user] = u; });
			var d = new frappe.ui.Dialog({
				title: __('Gun held for a deal'),
				fields: [
					{ fieldtype: 'HTML', fieldname: 'what' },
					{ fieldtype: 'Select', fieldname: 'approver', label: __('Manager'), hidden: 1,
						options: [{ value: '', label: '' }].concat(users.map(function (u) { return { value: u.user, label: u.full_name }; })),
						change: function () { mode(); } },
					{ fieldtype: 'Password', fieldname: 'secret', label: __('PIN'), hidden: 1 },
					{ fieldtype: 'HTML', fieldname: 'switch' },
					{ fieldtype: 'Select', fieldname: 'reason', label: __('Reason'), hidden: 1,
						options: [''].concat(api.REASONS.map(function (r) { return { value: r, label: __(r) }; })),
						change: function () { d.set_df_property('note', 'hidden', d.get_value('reason') !== api.OTHER); } },
					{ fieldtype: 'Data', fieldname: 'note', label: __('Details'), hidden: 1 },
					{ fieldtype: 'HTML', fieldname: 'err' },
				],
				primary_action_label: __('Release and sell'),
				primary_action: function (v) {
					if (!open) {
						open = true;
						['approver', 'secret'].forEach(function (f) { d.set_df_property(f, 'hidden', info.self_approves ? 1 : 0); });
						d.set_df_property('reason', 'hidden', 0);
						d.set_primary_action(__('Release'), d.primary_action);
						mode();
						return;
					}
					var authMode = currentMode();
					var reason = api.reasonText(v.reason, v.note);
					if (!info.self_approves && !v.approver) return error(__('Pick the manager.'));
					if (!info.self_approves && !v.secret) return error(authMode === 'pin' ? __('Type the PIN.') : __('Type the password.'));
					if (!reason) return error(__('Give a reason (Other needs the details).'));
					error('');
					d.get_primary_btn().prop('disabled', true);
					info.blocked.reduce(function (p, b) {
						return p.then(function () {
							return post(M + 'override_hold', api.overrideArgs(b.serial, reason, info.self_approves, v.approver, v.secret, authMode));
						});
					}, Promise.resolve()).then(function () {
						done = true;
						d.hide();
						frappe.show_alert({ message: __('Hold released.'), indicator: 'green' });
						resolve(true);
					}, function (err) {
						d.set_value('secret', '');
						error(err.message);
					}).finally(function () { d.get_primary_btn().prop('disabled', false); });
				},
				secondary_action_label: __('Cancel'),
				secondary_action: function () { d.hide(); },
			});
			function currentMode() {
				return ffl.staffSwitch.authMode(byUser[d.get_value('approver')], usePassword);
			}
			function mode() {
				if (!open || info.self_approves) { d.fields_dict.switch.$wrapper.html(''); return; }
				var m = currentMode();
				d.fields_dict.secret.set_label(m === 'pin' ? __('PIN') : __('Password'));
				var u = byUser[d.get_value('approver')];
				d.fields_dict.switch.$wrapper.html(u && u.has_pin && !u.pin_off
					? '<a href="#" class="small ffl-dh-switch">' + esc(usePassword ? __('Use PIN instead') : __('Use password instead')) + '</a>' : '');
				d.fields_dict.switch.$wrapper.find('.ffl-dh-switch').on('click', function (e) {
					e.preventDefault();
					usePassword = !usePassword;
					d.set_value('secret', '');
					mode();
				});
			}
			function error(text) {
				d.fields_dict.err.$wrapper.html(text ? '<div class="text-danger small">' + esc(text) + '</div>' : '');
			}
			d.fields_dict.what.$wrapper.html(info.blocked.map(function (b) {
				return '<p>' + esc(b.message) + '</p>';
			}).join(''));
			d.fields_dict.secret.$input && d.fields_dict.secret.$input.attr({ autocomplete: 'off', inputmode: 'numeric' });
			d.onhide = function () { if (!done) resolve(false); };
			d.show();
		});
	}

	// Form validate (POS checkout too): a gun held for someone else asks before the save.
	function guardForm(frm) {
		if (frm.doc.docstatus !== 0 || frm.doc.is_return) return;
		var serials = (frm.doc.items || []).some(function (r) { return r.serial_no || r.serial_and_batch_bundle; });
		if (!serials) return;
		return post(M + 'check_sale', { doc: frm.doc }).then(function (res) {
			if (!res || !(res.blocked || []).length) return;
			return ask(res).then(function (ok) { if (!ok) frappe.validated = false; });
		}, function () { /* the server's own gate still decides */ });
	}

	// Create Order: the hold on `serial` (DealHoldError on a scan). Resolves true once released.
	function forSerial(serial) {
		return post(M + 'serial_hold', { serial: serial }).then(function (res) {
			if (!res || !res.hold) return false;
			return ask({ blocked: [res.hold], self_approves: res.self_approves, approvers: res.approvers });
		}, function (err) {
			frappe.msgprint(esc(err.message));
			return false;
		});
	}

	['POS Invoice', 'Sales Invoice'].forEach(function (dt) {
		frappe.ui.form.on(dt, { validate: guardForm });
	});
	api.guardForm = guardForm;
	api.forSerial = forSerial;
})();
