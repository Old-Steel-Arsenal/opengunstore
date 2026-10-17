// Agreement Template: what the counter sends out (ffl_core.agreements).
frappe.ui.form.on('Agreement Template', {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__('Signed copies'), () => {
			frappe.set_route('List', 'Customer Agreement', {template: frm.doc.name});
		});
	},
});
