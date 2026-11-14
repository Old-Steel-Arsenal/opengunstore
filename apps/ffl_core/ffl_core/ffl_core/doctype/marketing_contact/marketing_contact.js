// Marketing Contact: the status is changed only by the Unsubscribe / Resubscribe buttons (the field is
// read-only; ffl_core.api.marketing is the gate), and the form lists what happened to this person and
// the reminders still waiting for them.
(function () {
	'use strict';

	var EVENT_LIMIT = 20;
	var REQUEST_LIMIT = 20;
	var SUPPRESSED = ['Unsubscribed', 'Bounced', 'Complained'];
	var MANAGER_ROLES = ['Sales Manager', 'System Manager'];
	var esc = function (value) { return frappe.utils.escape_html(String(value == null ? '' : value)); };

	function isManager() {
		return MANAGER_ROLES.some(function (role) { return frappe.user.has_role(role); });
	}

	// A log entry's detail is JSON written by the code; show it as "key: value" pairs.
	function detailText(detail) {
		if (!detail) return '';
		try {
			var data = JSON.parse(detail);
			if (data && typeof data === 'object' && !Array.isArray(data)) {
				detail = Object.keys(data).map(function (k) {
					var v = data[k];
					return k + ': ' + (typeof v === 'object' ? JSON.stringify(v) : v);
				}).join(', ');
			}
		} catch (e) { /* plain text */ }
		return detail.length > 160 ? detail.slice(0, 157) + '...' : detail;
	}

	function safeLink(url, label) {
		return /^https?:\/\//i.test(url || '')
			? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(label || url) + '</a>'
			: esc(label || url || '');
	}

	function table(head, rows, empty) {
		if (!rows.length) return '<div class="text-muted">' + esc(empty) + '</div>';
		return '<table class="table table-sm table-borderless" style="margin:0"><thead><tr>' +
			head.map(function (h) { return '<th class="text-muted font-weight-normal">' + esc(h) + '</th>'; }).join('') +
			'</tr></thead><tbody>' + rows.map(function (cells) {
				return '<tr>' + cells.map(function (c) { return '<td>' + c + '</td>'; }).join('') + '</tr>';
			}).join('') + '</tbody></table>';
	}

	function when(value) {
		return value ? esc(frappe.datetime.str_to_user(value)) : '';
	}

	function renderEvents(frm) {
		var field = frm.fields_dict.activity_html;
		if (!field) return;
		frappe.db.get_list('Marketing Event', {
			filters: { contact: frm.doc.name },
			fields: ['kind', 'occurred_at', 'mail', 'campaign', 'detail'],
			order_by: 'occurred_at desc, creation desc',
			limit: EVENT_LIMIT + 1,
		}).then(function (rows) {
			var more = rows.length > EVENT_LIMIT;
			var html = table([__('When'), __('Event'), __('Mail'), __('Detail')],
				rows.slice(0, EVENT_LIMIT).map(function (r) {
					return [when(r.occurred_at), esc(__(r.kind)), esc(r.mail || r.campaign || ''), esc(detailText(r.detail))];
				}), __('Nothing yet.'));
			if (more) html += '<a class="mkt-all-events" href="#">' + esc(__('All activity')) + '</a>';
			field.$wrapper.html(html);
			field.$wrapper.find('.mkt-all-events').on('click', function (e) {
				e.preventDefault();
				frappe.set_route('List', 'Marketing Event', { contact: frm.doc.name });
			});
		});
	}

	function renderRequests(frm) {
		var field = frm.fields_dict.requests_html;
		if (!field) return;
		frappe.db.get_list('Marketing Request', {
			filters: { contact: frm.doc.name, status: 'Open' },
			fields: ['kind', 'label', 'sku', 'url', 'requested_at'],
			order_by: 'requested_at desc, creation desc',
			limit: REQUEST_LIMIT,
		}).then(function (rows) {
			field.$wrapper.html(table([__('Asked'), __('Kind'), __('What')],
				rows.map(function (r) {
					return [when(r.requested_at), esc(__(r.kind)), safeLink(r.url, r.label || r.sku || r.url)];
				}), __('No reminders waiting.')));
		});
	}

	function unsubscribeDialog(frm) {
		frappe.prompt([
			{ fieldname: 'reason', fieldtype: 'Data', label: __('Reason (optional)') },
		], function (values) {
			frappe.call({
				method: 'ffl_core.api.marketing.unsubscribe',
				args: { contact: frm.doc.name, reason: values.reason || null },
				freeze: true,
				callback: function (r) {
					var out = r.message || {};
					frappe.show_alert({
						message: out.changed ? __('Unsubscribed.') : __('Already {0}.', [out.status]),
						indicator: out.changed ? 'green' : 'orange',
					});
					frm.reload_doc();
				},
			});
		}, __('Unsubscribe {0}?', [frm.doc.name]), __('Unsubscribe'));
	}

	function resubscribeDialog(frm) {
		frappe.prompt([
			{ fieldname: 'reason', fieldtype: 'Data', label: __('Why may this contact be emailed again?'), reqd: 1 },
		], function (values) {
			frappe.call({
				method: 'ffl_core.api.marketing.resubscribe',
				args: { contact: frm.doc.name, reason: values.reason },
				freeze: true,
				callback: function () {
					frappe.show_alert({ message: __('Subscribed again.'), indicator: 'green' });
					frm.reload_doc();
				},
			});
		}, __('Resubscribe {0}?', [frm.doc.name]), __('Resubscribe'));
	}

	frappe.ui.form.on('Marketing Contact', {
		refresh: function (frm) {
			if (frm.is_new()) return;
			if (isManager()) {
				if (SUPPRESSED.indexOf(frm.doc.status) === -1) {
					frm.add_custom_button(__('Unsubscribe'), function () { unsubscribeDialog(frm); });
				} else {
					frm.add_custom_button(__('Resubscribe'), function () { resubscribeDialog(frm); });
				}
			}
			renderRequests(frm);
			renderEvents(frm);
		},
	});
})();
