// Signing Terminal: pair a store tablet (ffl_core/signing_terminal.py).
frappe.ui.form.on('Signing Terminal', {
	refresh(frm) {
		const esc = frappe.utils.escape_html;
		const page = `${location.origin}/kiosk`;
		const seen = frm.doc.last_seen ? frappe.datetime.prettyDate(frm.doc.last_seen) : '';
		const state = !frm.doc.active ? __('Switched off: the tablet no longer works.')
			: frm.doc.paired_on ? __('Paired {0} by {1}. Last seen {2}.', [esc(frappe.datetime.str_to_user(frm.doc.paired_on)),
				esc(frm.doc.paired_by || ''), esc(seen || __('never'))])
			: __('Not paired yet.');
		frm.get_field('pairing_help').$wrapper.html(`<p>${state}</p>
			<p class="text-muted small">${__('On the tablet, open {0}, add it to the home screen and keep it there, logged out of the POS.', [`<b>${esc(page)}</b>`])}</p>`);
		if (frm.is_new() || !frm.doc.active || !frappe.user.has_role(['Sales Manager', 'System Manager'])) return;
		frm.add_custom_button(frm.doc.paired_on ? __('Pair again') : __('Pair a tablet'), () => {
			frappe.call({ method: 'ffl_core.signing_terminal.start_pairing', args: { terminal: frm.doc.name } }).then((r) => {
				const m = r.message || {};
				frappe.msgprint({
					title: __('Pair {0}', [esc(frm.doc.name)]),
					indicator: 'blue',
					message: `<p>${__('On the tablet, open {0} and type this code:', [`<b>${esc(m.page)}</b>`])}</p>
						<p style="font-size:40px; font-weight:700; letter-spacing:0.25em; text-align:center; margin:16px 0">${esc(m.code)}</p>
						<p class="text-muted small">${__('The code works once, for {0} minutes. Pairing again cuts off the tablet paired before.', [m.expires_in_minutes])}</p>`,
				});
			});
		});
	},
});
