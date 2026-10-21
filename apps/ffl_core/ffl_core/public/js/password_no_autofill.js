// Desk password fields never take the browser's saved login. The store signs in
// with one shared account, so the browser holds its email + password, and any
// form with a text field followed by a password field (a partner POS key and
// secret, a distributor's client id and secret) looks like a login form to it:
// it fills the store login in on page load, and the next Save stores it there.
// Frappe marks inputs autocomplete="off", which browsers ignore for saved logins;
// "new-password" is the value they honour (a field to set a password, not sign
// in with). The web login page is not a Desk control and keeps its autofill.
//
// Upgrade-safe: Frappe's ControlPassword is wrapped on its prototype, never
// edited. install() is pure enough to Node-test with a stand-in control class.
(function (root, factory) {
	var api = factory();
	if (typeof module === 'object' && module.exports) {
		module.exports = api;                       // Node test
	} else {
		root.ffl = root.ffl || {};
		root.ffl.passwordNoAutofill = api;
		var Control = root.frappe && frappe.ui && frappe.ui.form && frappe.ui.form.ControlPassword;
		if (Control) api.install(Control);
	}
})(typeof self !== 'undefined' ? self : this, function () {
	'use strict';

	function install(Control) {
		var proto = Control.prototype;
		if (proto.__fflNoAutofill) return;
		var makeInput = proto.make_input;
		proto.make_input = function () {
			var out = makeInput.apply(this, arguments);
			if (this.$input) this.$input.attr('autocomplete', 'new-password');
			return out;
		};
		proto.__fflNoAutofill = true;
	}

	return { install: install };
});
