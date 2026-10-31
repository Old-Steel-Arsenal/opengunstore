// Data Service Settings form. Test Connection proves endpoint + credentials and
// records the contract version on the Status tab. Categories tab: the category
// mirror's last report — a banner when it could not link something — and a
// button to run it now.

frappe.ui.form.on('Data Service Settings', {
	refresh(frm) {
		const $test = frm.add_custom_button(__('Test Connection'), function () {
			frappe.call({
				method: 'ffl_integrations.ffl_integrations.doctype.data_service_settings.data_service_settings.test_connection',
				freeze: true,
				freeze_message: __('Contacting the data service…'),
				callback(resp) {
					ffl.showConnectionChecks(__('Data service'), (resp.message || {}).checks || []);
					frm.reload_doc();
				},
			});
		});
		$test.removeClass('btn-default').addClass('btn-primary');

		frm.add_custom_button(__('Run category mirror'), () => {
			frappe.call({
				method: 'ffl_integrations.catalog_master.run_category_mirror',
				callback() {
					frappe.show_alert({ message: __('Category mirror queued — its report lands on the Categories tab.'), indicator: 'blue' });
				},
			});
		}, __('Categories'));
		const issues = frm.doc.category_mirror_issues || 0;
		if (issues) {
			frm.dashboard.set_headline_alert(
				__('Category mirror: {0} categories could not be linked — see Categories › Last report. Nothing is guessed: fix them in the catalog workbench (a store category slug on the node) or on the store.', [issues]),
				'orange');
		}
	},
});
