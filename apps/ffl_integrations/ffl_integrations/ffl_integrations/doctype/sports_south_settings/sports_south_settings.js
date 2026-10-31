// Sports South Settings form. One button, read-only against Sports South:
// Test Connection proves the catalog service and the ordering credentials
// (both asmx services) before anyone enables ordering.

frappe.ui.form.on('Sports South Settings', {
	refresh(frm) {
		if (frm.doc.ordering_enabled && frm.doc.sandbox_mode) {
			frm.dashboard.set_headline(
				__('Sandbox — orders are sent with Source 99994 (Sports South test environment).'),
				'blue');
		} else {
			frm.dashboard.clear_headline();
		}
		const $test = frm.add_custom_button(__('Test Connection'), function () {
			frappe.call({
				method: 'ffl_integrations.ffl_integrations.doctype.sports_south_settings.sports_south_settings.test_connection',
				freeze: true,
				freeze_message: __('Contacting Sports South…'),
				callback(resp) {
					ffl.showConnectionChecks(__('Sports South'), (resp.message || {}).checks || []);
				},
			});
		});
		$test.removeClass('btn-default').addClass('btn-primary');
	},
});
