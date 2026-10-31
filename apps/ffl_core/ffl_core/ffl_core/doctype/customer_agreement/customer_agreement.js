// Customer Agreement: read-only; every change goes through ffl_core.agreements
// (dialogs in public/js/agreement_actions.js).
frappe.ui.form.on('Customer Agreement', {
	refresh(frm) {
		frm.disable_save();
		const manager = ['Sales Manager', 'System Manager'].some((r) => frappe.user.has_role(r));
		const d = frm.doc;
		if (d.status === 'Pending') {
			// Re-send this copy for what it covers: its guns (Each firearm sold) or its record (Per document).
			const guns = (d.firearms || []).length
				? d.firearms.map((f) => ({serial_no: f.serial_no, item_code: f.item_code, rate: f.price}))
				: (d.serial_no ? [{serial_no: d.serial_no, item_code: d.item_code}] : undefined);
			frm.add_custom_button(__('Send again'), () => ffl.agreements.request(d.customer, d.template, {
				customer_name: d.customer_name, firearms: guns,
				reference_doctype: d.reference_doctype, reference_name: d.reference_name,
				done: () => frm.reload_doc(),
			}));
			if (manager) {
				frm.add_custom_button(__('Record paper copy'), () => ffl.agreements.recordPaper(frm));
			}
		}
		if (['Signed', 'Expired'].includes(d.status) && d.signed_via !== 'Paper' && d.signature) {
			frm.add_custom_button(__('Regenerate PDF'), () => frappe.call({
				method: 'ffl_core.agreements.regenerate_signed_pdf', args: {name: d.name}, freeze: true,
			}).then(() => frm.reload_doc()));
		}
		if (manager && d.status !== 'Void') {
			frm.add_custom_button(__('Void'), () => frappe.prompt(
				{fieldtype: 'Small Text', fieldname: 'reason', label: __('Reason'), reqd: 1},
				(v) => frappe.call({method: 'ffl_core.agreements.void_agreement', args: {name: d.name, reason: v.reason},
					freeze: true}).then(() => frm.reload_doc()),
				__('Void agreement'), __('Void')));
		}
		// Opened from Request agreement → Record paper copy.
		if (manager && d.status === 'Pending' && frappe.route_options && frappe.route_options.ffl_record_paper) {
			frappe.route_options = null;
			ffl.agreements.recordPaper(frm);
		}
	},
});
