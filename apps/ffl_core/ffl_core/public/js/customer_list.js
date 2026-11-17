// Customer list + form-header indicator by customer_status (Profile tab).
//
// Without this the pill and the list's Status column say "Enabled" for a Banned
// customer, so a ban is invisible outside the Profile tab. Banned = red.
// Active (and a blank status) falls through
// to Frappe's own Enabled/Disabled, and a disabled customer stays "Disabled"
// whatever its status. The form header uses the same settings
// (frappe.get_indicator reads listview_settings[doctype].get_indicator).
//
// ERPNext ships its own listview_settings['Customer'] (customer_list.js, only
// add_fields); this file MERGES into whatever is already there and chains any
// get_indicator an earlier script registered.
(function (root, factory) {
	var api = factory();
	if (typeof module === 'object' && module.exports) module.exports = api;
	else {
		root.ffl = root.ffl || {};
		root.ffl.customerStatusIndicator = api;
		api.install(root.frappe);
	}
})(typeof self !== 'undefined' ? self : this, function () {
	'use strict';

	var COLORS = { Banned: 'red' };
	var ADD_FIELDS = ['customer_status', 'disabled'];

	// [label, colour, filter] for Banned, else null so Frappe shows
	// Enabled / Disabled. `L` is the translation function (identity in tests).
	function indicator(doc, L) {
		L = L || function (s) { return s; };
		if (!doc || doc.disabled) return null;
		var colour = COLORS[doc.customer_status];
		if (!colour) return null;
		return [L(doc.customer_status), colour, 'customer_status,=,' + doc.customer_status];
	}

	// Merge into frappe.listview_settings['Customer']; returns the settings.
	function install(frappeObj) {
		if (!frappeObj) return null;
		var all = frappeObj.listview_settings = frappeObj.listview_settings || {};
		var s = all.Customer = all.Customer || {};
		var fields = (s.add_fields || []).slice();
		ADD_FIELDS.forEach(function (f) { if (fields.indexOf(f) < 0) fields.push(f); });
		s.add_fields = fields;
		var previous = s.get_indicator;
		s.get_indicator = function (doc) {
			var mine = indicator(doc, typeof __ === 'function' ? __ : null);
			if (mine) return mine;
			return typeof previous === 'function' ? previous(doc) : null;
		};
		return s;
	}

	return { indicator: indicator, install: install, COLORS: COLORS, ADD_FIELDS: ADD_FIELDS };
});
