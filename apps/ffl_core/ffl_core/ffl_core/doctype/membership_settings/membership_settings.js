// Membership Settings: the Needs attention list (Past Due, Suspended, unconfirmed card charges,
// renewals the job could not charge, sign-ups unpaid for days) with Retry now / Cancel / Drop.
// Rules: ffl_core/membership.py.
(function () {
	const M = 'ffl_core.membership.';
	const esc = (s) => frappe.utils.escape_html(s == null ? '' : String(s));
	const STATUS_COLOR = { Pending: 'orange', Active: 'green', 'Past Due': 'orange', Suspended: 'red' };

	function load(frm) {
		const field = frm.fields_dict.attention_html;
		if (!field) return;
		const $w = field.$wrapper.empty().append(`<p class="text-muted">${esc(__('Loading…'))}</p>`);
		frappe.call({ method: M + 'needs_attention' }).then((r) => {
			const out = (r && r.message) || {};
			const rows = out.rows || [];
			$w.empty();
			if (!out.renewals_on) {
				$w.append(`<div class="alert alert-warning">${esc(__('Renewals are not charged automatically: Charge renewals automatically is off (Renewals tab).'))}</div>`);
			}
			if (!rows.length) {
				$w.append(`<p class="text-muted">${esc(__('Nothing needs attention.'))}</p>`);
				return;
			}
			const btn = (act, row, label) => `<button class="btn btn-xs btn-default" style="margin-left:4px" data-act="${act}" data-name="${esc(row.membership)}">${esc(label)}</button>`;
			const $t = $(`<table class="table table-sm"><thead><tr>
				<th>${esc(__('Customer'))}</th><th>${esc(__('Plan'))}</th><th>${esc(__('Why'))}</th><th>${esc(__('Invoice'))}</th><th></th>
				</tr></thead><tbody></tbody></table>`);
			rows.forEach((row) => {
				const buttons = (row.can_retry ? btn('retry', row, __('Retry now')) : '')
					+ (row.can_cancel ? btn('cancel', row, __('Cancel')) : '')
					+ (row.can_drop ? btn('drop', row, __('Drop')) : '');
				$t.find('tbody').append(`<tr>
					<td><a href="/app/customer/${encodeURIComponent(row.customer)}">${esc(row.customer_name || row.customer)}</a></td>
					<td>${esc(row.plan)} <span class="indicator-pill ${STATUS_COLOR[row.status] || 'gray'}">${esc(__(row.status))}</span></td>
					<td>${esc(row.reason)}${row.detail ? `<div class="text-muted small">${esc(row.detail)}</div>` : ''}</td>
					<td>${row.sales_invoice ? `<a href="/app/sales-invoice/${encodeURIComponent(row.sales_invoice)}">${esc(row.sales_invoice)}</a> ${format_currency(row.outstanding)}` : ''}</td>
					<td class="text-right" style="white-space:nowrap">${buttons}</td></tr>`);
			});
			$w.append($t);
			$t.on('click', '[data-act]', (e) => act(frm, $(e.currentTarget).attr('data-act'), $(e.currentTarget).attr('data-name')));
		}, () => $w.empty());
	}

	function act(frm, what, membership) {
		if (what === 'retry') {
			frappe.call({ method: M + 'retry_renewal', args: { membership }, freeze: true }).then((r) => {
				const out = (r && r.message) || {};
				frappe.msgprint({ message: esc(out.message || ''), indicator: out.outcome === 'renewed' ? 'green' : 'orange', title: __('Retry now') });
				load(frm);
			}, () => load(frm));
			return;
		}
		const method = what === 'drop' ? 'discard_membership' : 'cancel_membership';
		const d = new frappe.ui.Dialog({
			title: what === 'drop' ? __('Drop membership {0}', [membership]) : __('Cancel membership {0}', [membership]),
			fields: [{ fieldtype: 'Small Text', fieldname: 'reason', label: __('Reason'), reqd: 1,
				description: __('An unpaid invoice of the membership is voided. Any refund is a credit note made by hand.') }],
			primary_action_label: what === 'drop' ? __('Drop') : __('Cancel membership'),
			primary_action: (v) => {
				d.hide();
				frappe.call({ method: M + method, args: { membership, reason: v.reason }, freeze: true }).then(() => load(frm), () => load(frm));
			},
		});
		d.show();
	}

	frappe.ui.form.on('Membership Settings', { refresh: load });
})();
