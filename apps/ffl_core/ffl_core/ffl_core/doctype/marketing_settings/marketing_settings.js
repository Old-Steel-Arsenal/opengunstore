// Marketing Settings: the primary action sends the logged-in employee a test email through the whole
// sending path. The sender is ffl_core.api.marketing_mail.send_test_email; a server without it is
// told so plainly.
(function () {
	'use strict';

	frappe.ui.form.on('Marketing Settings', {
		refresh: function (frm) {
			frm.add_custom_button(__('Send test email'), function () { sendTestEmail(frm); }).addClass('btn-primary');
		},
	});

	function sendTestEmail(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__('Save the settings first.'));
			return;
		}
		frappe.call({
			method: 'ffl_core.api.marketing_mail.send_test_email',
			freeze: true,
			freeze_message: __('Sending...'),
			silent: true,   // messages are shown below, so a missing method is not a stack-trace dialog
			callback: function (r) {
				if (!showServerMessages(r)) {
					frappe.show_alert({ message: __('Test email sent to {0}.', [frappe.session.user_email || frappe.session.user]), indicator: 'green' });
				}
			},
			error: function (r) {
				var raw = (r && r._server_messages) || '';
				if (/Failed to get method|No module named|has no attribute/i.test(raw) || (r && r.exc_type === 'AppNotInstalledError')) {
					frappe.msgprint({ title: __('Not available'), message: __('This POS version cannot send a test email.'), indicator: 'red' });
				} else if (!showServerMessages(r)) {
					frappe.msgprint({ title: __('Test email failed'), message: __('The test email was not sent. See the Error Log.'), indicator: 'red' });
				}
			},
		});
	}

	// Show what the server said; true when it said anything.
	function showServerMessages(r) {
		if (!r || !r._server_messages) return false;
		frappe.msgprint(JSON.parse(r._server_messages));
		return true;
	}
})();
