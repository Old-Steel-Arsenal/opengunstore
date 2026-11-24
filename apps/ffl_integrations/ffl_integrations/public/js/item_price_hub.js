// Price Hub — every enabled distributor's cost and history for this Item, for the
// staff who order (the page itself is theirs alone). The Item is found there by its
// UPC barcode, else by its code (Items from the catalog are named by UPC).
frappe.ui.form.on('Item', {
	refresh(frm) {
		if (frm.is_new()) return;
		if (!['Purchase Manager', 'Purchase User', 'Stock Manager', 'System Manager'].some((r) => frappe.user.has_role(r))) return;
		frm.add_custom_button(__('Price Hub'), () => {
			const upc = (frm.doc.barcodes || []).find((b) => b.barcode_type === 'UPC');
			frappe.set_route('price-hub', upc ? upc.barcode : frm.doc.name);
		}, __('View'));
	},
});
