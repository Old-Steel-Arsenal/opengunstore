// RSR Settings form. One button, read-only against RSR: Test Connection
// proves the catalog service and the Direct Connect credentials.

frappe.ui.form.on('RSR Settings', {
	refresh(frm) {
		const $test = frm.add_custom_button(__('Test Connection'), function () {
			frappe.call({
				method: 'ffl_integrations.ffl_integrations.doctype.rsr_settings.rsr_settings.test_connection',
				freeze: true,
				freeze_message: __('Contacting RSR…'),
				callback(resp) {
					ffl.showConnectionChecks(__('RSR'), (resp.message || {}).checks || []);
				},
			});
		});
		$test.removeClass('btn-default').addClass('btn-primary');
	},
});
