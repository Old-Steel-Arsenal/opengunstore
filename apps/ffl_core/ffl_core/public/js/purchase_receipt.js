// Purchase Receipt — populate the FFL Source dropdown from FFL Settings →
// Receive Sources (admin-edited list). The Receive Goods page already does
// this; this hook keeps the desk form's dropdown in sync with the same list
// so edits on the standard PR form stay consistent.
frappe.ui.form.on('Purchase Receipt', {
	refresh(frm) {
		// A seller marked as paid outside the system (Cash Drawer page): a manager can take the mark back here.
		if (frm.doc.docstatus === 1 && frm.doc.ffl_seller_paid_outside
				&& frappe.user.has_role(['Sales Manager', 'System Manager'])) {
			frm.add_custom_button(__('Clear "paid outside the system"'), () => {
				frappe.confirm(__('Take back the mark "{0}"? The purchase is listed again and its seller payment can be recorded.',
					[frappe.utils.escape_html(frm.doc.ffl_seller_paid_outside)]), () => {
					frappe.call({ method: 'ffl_core.api.cash_drawer.clear_paid_outside', args: { purchase_receipt: frm.doc.name },
						freeze: true, callback: (r) => { if (r.message) frm.reload_doc(); } });
				});
			});
		}
		if (!frm.fields_dict.ffl_source) return;  // custom field not installed
		ffl.loadSelectOptions({
			method: 'ffl_core.api.receive_goods.get_receive_sources',
			get: () => frm.doc.ffl_source,
			set: (options) => {
				frm.set_df_property('ffl_source', 'options', options);
				frm.refresh_field('ffl_source');
			},
		});
	},
});
