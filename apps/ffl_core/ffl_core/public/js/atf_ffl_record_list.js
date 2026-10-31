// ATF FFL Record list view — "+ Lookup by FFL #" quick-add.
//
// Replaces the manual flow ("New" → fill 8 address fields by hand) with a
// one-click lookup against the configured EZ Check provider. Backend
// (ffl_integrations.atf.ez_check.lookup_and_create) handles fetch + cache
// upsert; we just collect the FFL number and refresh the list.

frappe.listview_settings = frappe.listview_settings || {};
frappe.listview_settings['ATF FFL Record'] = frappe.listview_settings['ATF FFL Record'] || {};

const _origOnload = frappe.listview_settings['ATF FFL Record'].onload;

frappe.listview_settings['ATF FFL Record'].onload = function (listview) {
	if (typeof _origOnload === 'function') {
		try { _origOnload(listview); } catch (e) { /* keep going */ }
	}
	overridePrimaryAction(listview);
};

const _origRefresh = frappe.listview_settings['ATF FFL Record'].refresh;
frappe.listview_settings['ATF FFL Record'].refresh = function (listview) {
	if (typeof _origRefresh === 'function') {
		try { _origRefresh(listview); } catch (e) { /* keep going */ }
	}
	// Frappe re-sets the primary action on every refresh (its default is
	// "+ Add ATF FFL Record"). Re-hijack so the EZ Check lookup is the
	// only path users see — manual address entry is the wrong default
	// for a doctype whose canonical data source is ATF.
	overridePrimaryAction(listview);
};

function overridePrimaryAction(listview) {
	listview.page.set_primary_action(__('+ Lookup by FFL #'),
		() => openLookupDialog(listview));
}

function openLookupDialog(listview) {
	const d = new frappe.ui.Dialog({
		title: __('Lookup FFL Record'),
		size: 'small',
		fields: [
			{
				fieldtype: 'Data',
				fieldname: 'ffl_number',
				label: __('FFL Number'),
				reqd: 1,
				description: __('Format: 1-99-999-99-9X-99999; hyphens are optional. The lookup hits ATF EZ Check live and caches the result here.'),
			},
		],
		primary_action_label: __('Lookup & Save'),
		primary_action(values) {
			const num = (values.ffl_number || '').trim();
			if (!num) return;
			frappe.call({
				method: 'ffl_integrations.atf.ez_check.lookup_and_create',
				args: { ffl_number: num },
				freeze: true,
				freeze_message: __('Verifying with ATF EZ Check…'),
				callback: (r) => {
					const m = r.message || {};
					if (!m.ok) return;
					d.hide();
					frappe.show_alert({
						message: __('Saved {0} · {1}', [m.name, m.licensee_name || m.status]),
						indicator: m.status === 'Verified' ? 'green' : 'orange',
					});
					if (listview && listview.refresh) listview.refresh();
				},
			});
		},
	});
	d.show();
	setTimeout(() => d.fields_dict.ffl_number.input?.focus(), 60);
}
