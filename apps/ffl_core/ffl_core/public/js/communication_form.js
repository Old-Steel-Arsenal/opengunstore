// A mail the AI screening judged "not a customer" has no lead: Make lead files it by the normal
// rules (ffl_core.api.crm.make_lead_from_mail) and opens it in CRM.
frappe.ui.form.on("Communication", {
	refresh(frm) {
		if (frm.is_new() || !(frm.doc.__onload && frm.doc.__onload.ffl_screened_out)) return;
		if (!frappe.user.has_role(["Sales User", "Sales Manager", "System Manager"])) return;
		frm.add_custom_button(__("Make lead"), () => {
			frappe.call({
				method: "ffl_core.api.crm.make_lead_from_mail",
				type: "POST",
				args: { communication: frm.doc.name },
				freeze: true,
				callback: (r) => {
					if (!r.message) return;
					window.open(`/crm/${r.message.doctype === "CRM Deal" ? "deals" : "leads"}/${encodeURIComponent(r.message.name)}`, "_self");
				},
			});
		});
	},
});
