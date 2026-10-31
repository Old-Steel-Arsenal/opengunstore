// Lipseys Settings form. One button, read-only against Lipsey's: Test
// Connection proves the catalog service and each filled-in login (sign in plus
// one small read) before anyone enables ordering.

frappe.ui.form.on('Lipseys Settings', {
	refresh(frm) {
		const $test = frm.add_custom_button(__('Test Connection'), function () {
			frappe.call({
				method: 'ffl_integrations.ffl_integrations.doctype.lipseys_settings.lipseys_settings.test_connection',
				freeze: true,
				freeze_message: __("Contacting Lipsey's…"),
				callback(resp) {
					ffl.showConnectionChecks(__("Lipsey's"), (resp.message || {}).checks || []);
				},
			});
		});
		$test.removeClass('btn-default').addClass('btn-primary');
	},
});
