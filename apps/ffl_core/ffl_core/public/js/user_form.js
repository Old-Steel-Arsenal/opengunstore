// My Settings: a staff member connects their own mailbox to the POS (ffl_core.api.crm.connect_my_mailbox).
frappe.ui.form.on("User", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.name !== frappe.session.user) return;
		if (!frappe.user.has_role(["Sales User", "Sales Manager"])) return;
		frm.add_custom_button(__("Connect my mailbox"), () => {
			frappe.call({
				method: "ffl_core.api.crm.connect_my_mailbox",
				type: "POST",
				args: { success_uri: window.location.pathname },
				callback: (r) => r.message && window.open(r.message, "_self"),
			});
		});
	},
});
