// Test Connection and Connect for the WooCommerce Settings single.
//
// Test Connection: one whitelisted probe (GET /products?per_page=1, read auth only),
// the thing to press after every change on this page; it writes nothing to a store.
// Connect: the one-click connection (below), which does write — it replaces this
// POS's webhooks on the store and pushes the storefront plugin its POS credentials.
const WOO_SETTINGS_SITES = {
	'WooCommerce Settings': 'retail',
};

Object.entries(WOO_SETTINGS_SITES).forEach(([doctype, site]) => {
	frappe.ui.form.on(doctype, {
		refresh(frm) {
			const $test = frm.add_custom_button(__('Test Connection'), () => {
				frappe.call({
					method: 'ffl_woo_sync.woocommerce.client_api.test_connection',
					args: { site },
					freeze: true,
					freeze_message: __('Contacting WooCommerce…'),
					callback(r) {
						const m = r.message || {};
						if (!m.ok) return;
						frappe.show_alert({
							message: __('WooCommerce connection OK: {0}', [m.store_url || frm.doc.store_url]),
							indicator: 'green',
						});
					},
				});
			});
			$test.removeClass('btn-default').addClass('btn-primary');

			frm.add_custom_button(__('Connect'), () => woo_connect(frm));
			woo_connect_after_return(frm);
		},
	});
});

// Connect: save the Store URL if it changed, then hand the browser to the store's own
// approval page (woocommerce/connect.py). The store posts the key back to the POS; the
// rest runs in the background and reports in Status → Connect.
function woo_connect(frm) {
	const go = () => frappe.call({
		method: 'ffl_woo_sync.woocommerce.connect.start_connect',
		freeze: true,
		callback(r) {
			if (r.message && r.message.url) window.location.href = r.message.url;
		},
	});
	if (frm.is_dirty()) frm.save().then(go);
	else go();
}

// Back from the store's approval page (?success=…): say so when it was declined, and reload
// the form until the background run has finished.
function woo_connect_after_return(frm) {
	const params = new URLSearchParams(window.location.search);
	if (!params.has('success') || frm._woo_connect_polling) return;
	frm._woo_connect_polling = true;
	window.history.replaceState(null, '', window.location.pathname);
	if (params.get('success') !== '1') {
		frappe.show_alert({ message: __('Not approved on the store.'), indicator: 'red' });
		return;
	}
	let tries = 0;
	const poll = () => {
		const running = (frm.doc.connect_status || '').startsWith('Running');
		if (!running || tries++ > 40) return;
		setTimeout(() => frm.reload_doc().then(poll), 3000);
	};
	frm.scroll_to_field('connect_status');
	poll();
}
