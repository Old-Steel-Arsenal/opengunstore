// Counter customer panel: what the clerk must know the moment a customer is picked.
//
// One shared module for the places a counter screen picks a customer — the POS
// register, Create Order, the trade-in dialog and Receive Goods' seller (its
// transfer owner only gets warn(): the gun arrives either way):
//   ffl.customerFlags.check(customer, {key}) → Promise<boolean>
// reads ffl_core.customer_flags.get_customer_flags and
//   • Banned (no open manager override): a red box; the clerk cannot go on. A
//     manager (Sales Manager) can "Override ban" with a reason — the server opens
//     a 30-minute window (override_customer_ban) — and the answer becomes true.
//     The server refuses the sale on its own too (refuse_banned_customer).
//   • Active alerts: shown once per `key` (the POS uses the invoice + customer).
//   • Everything else (status, family, tags, IDs about to expire, agreements
//     shown on the panel): a one-line strip — ffl.customerFlags.strip(flags) —
//     the register shows under the customer's name. An agreement still to sign
//     is a clickable chip that opens ffl.agreements.request (agreement_actions.js).
// The POS wiring is here too: the POS Invoice `customer` event (both the register
// and the Desk form), the ItemCart customer section (strip + Agreements button + "Scan ID" lookup,
// shown only while FFL Settings → Store Customer ID Numbers is on), wrapped on
// ERPNext's prototype via ffl.installClassHook — never edited.
// New Customer dialog: confirmNotDuplicate(values) — customer_quick_entry.js asks
// it before saving.
//
// The pure half (summarize / stripHtml / dupLabel / maskId) is Node-tested.
(function (root, factory) {
	var api = factory();
	if (typeof module === 'object' && module.exports) module.exports = api;
	else { root.ffl = root.ffl || {}; root.ffl.customerFlags = Object.assign(root.ffl.customerFlags || {}, api); }
})(typeof self !== 'undefined' ? self : this, function () {
	'use strict';

	// Stand-in for frappe's __ outside the browser: fills {0}, {1}…
	function id(s, args) {
		return String(s).replace(/\{(\d+)\}/g, function (m, i) { return args && args[i] != null ? args[i] : m; });
	}
	function escText(s) {
		return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
			return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
		});
	}

	// flags (get_customer_flags) → {blocked, alerts: [message], chips: [{label, tone, request?}]}.
	// tone: red | orange | yellow | green | blue | gray. request: {customer, template} —
	// the chip asks for that agreement when clicked.
	function summarize(flags, L) {
		L = L || id;
		var f = flags || {};
		var chips = [];
		if (f.status === 'Banned') {
			chips.push(f.banned
				? { label: L('Banned'), tone: 'red' }
				: { label: L('Banned — override until {0}', [String(f.override_until || '').slice(11, 16)]), tone: 'red' });
		} else if (f.status && f.status !== 'Active') {
			chips.push({ label: L(f.status), tone: 'orange' });
		}
		(f.ids || []).forEach(function (d) {
			chips.push(d.expired
				? { label: L('{0} expired {1}', [L(d.id_type), d.expiry_date]), tone: 'orange' }
				: { label: L('{0} expires {1}', [L(d.id_type), d.expiry_date]), tone: 'yellow' });
		});
		(f.agreements || []).forEach(function (a) {
			var ask = { customer: f.name, template: a.template };
			// Optional: nothing waits for it, so it reads neutral, never "needed".
			if (a.requirement === 'Optional' && (a.state === 'missing' || a.state === 'expired')) {
				chips.push({ label: L('{0} not signed', [a.template]), tone: 'gray', request: ask });
			}
			else if (a.state === 'missing') chips.push({ label: L('{0} needed', [a.template]), tone: 'orange', request: ask });
			else if (a.state === 'expired') {
				chips.push({ label: a.expires_on ? L('{0} expired {1}', [a.template, a.expires_on]) : L('{0} expired', [a.template]),
					tone: 'orange', request: ask });
			}
			else if (a.state === 'pending') chips.push({ label: L('{0} sent, not signed', [a.template]), tone: 'yellow', request: ask });
			else if (a.state === 'expiring') chips.push({ label: L('{0} expires {1}', [a.template, a.expires_on]), tone: 'yellow' });
			else if (a.state === 'signed') chips.push({ label: L('{0} signed', [a.template]), tone: 'green' });
		});
		var fam = f.family || {};
		if (fam.primary) {
			chips.push({ label: L('Family of {0}', [fam.primary.customer_name || fam.primary.name]), tone: 'blue' });
		} else if (fam.members) {
			chips.push({ label: fam.members === 1 ? L('1 family member') : L('{0} family members', [fam.members]), tone: 'blue' });
		}
		(f.tags || []).forEach(function (t) { chips.push({ label: t, tone: 'gray' }); });
		return {
			blocked: !!f.banned,
			alerts: (f.alerts || []).map(function (a) { return a.message; }).filter(Boolean),
			chips: chips,
		};
	}

	var TONES = { red: 'red', orange: 'orange', yellow: 'yellow', green: 'green', blue: 'blue', gray: 'gray' };

	// One line of Frappe indicator pills; '' when there is nothing to say.
	function stripHtml(flags, L) {
		var s = summarize(flags, L);
		if (!s.chips.length && !s.alerts.length) return '';
		var pills = s.chips.map(function (c) {
			var ask = c.request ? ' role="button" data-ffl-agreement-customer="' + escText(c.request.customer)
				+ '" data-ffl-agreement-template="' + escText(c.request.template) + '"' : '';
			return '<span class="indicator-pill ' + (TONES[c.tone] || 'gray') + '"' + ask
				+ ' style="margin:2px 4px 0 0' + (ask ? '; cursor:pointer' : '') + '">'
				+ escText(c.label) + '</span>';
		});
		if (s.alerts.length) {
			pills.push('<span class="indicator-pill red" style="margin:2px 4px 0 0" title="'
				+ escText(s.alerts.join('\n')) + '">' + escText((L || id)('{0} alert(s)', [s.alerts.length])) + '</span>');
		}
		return '<div class="ffl-customer-flags" style="display:flex; flex-wrap:wrap; padding:4px 0">' + pills.join('') + '</div>';
	}

	// "email, phone" for a duplicate-candidate row.
	function dupLabel(row, L) {
		L = L || id;
		var names = { email: L('email'), phone: L('phone'), id_number: L('ID number') };
		return (row.matched || []).map(function (m) { return names[m] || m; }).join(', ');
	}

	// Last four characters only — an ID number is never shown whole.
	function maskId(v) {
		var s = String(v || '');
		return s.length > 4 ? '••' + s.slice(-4) : s;
	}

	return { summarize: summarize, stripHtml: stripHtml, dupLabel: dupLabel, maskId: maskId, escText: escText };
});

// Browser wiring.
(function fflCustomerFlagsWire() {
	if (typeof window === 'undefined' || typeof module === 'object') return;
	var api = ffl.customerFlags;
	var METHOD = 'ffl_core.customer_flags.';
	var cache = {};       // customer → last flags (the POS strip reads it)
	var fetchedAt = {};   // customer → when its flags were read (ms)
	var CACHE_MS = 60000; // a resumed draft re-reads flags older than this
	var inflight = {};    // customer → pending check (a link fires change more than once)
	var alerted = {};     // key → true once its alerts were shown

	function fetchFlags(customer) {
		return frappe.call({ method: METHOD + 'get_customer_flags', args: { customer: customer } })
			.then(function (r) { cache[customer] = r.message; fetchedAt[customer] = Date.now(); return r.message; });
	}

	function showAlerts(flags, key) {
		var s = api.summarize(flags, __);
		if (!s.alerts.length || alerted[key]) return;
		alerted[key] = true;
		frappe.msgprint({
			title: __('Customer alert: {0}', [api.escText(flags.customer_name)]),
			indicator: 'orange',
			message: s.alerts.map(function (m) { return '<p>' + api.escText(m) + '</p>'; }).join(''),
		});
	}

	// Banned: resolve true only after a manager override.
	function bannedBox(flags) {
		// The register freezes the screen around a customer pick (once per onchange,
		// and the Link fires onchange again when focus moves into this box) and
		// unfreezes when the pick is answered. Keep the screen unfrozen while the box is
		// open so it can be used; nothing is re-frozen afterwards — frappe's unfreeze is
		// a no-op at zero, so ERPNext's own unfreezes stay harmless.
		var lift = function () { while (frappe.dom.freeze_count > 0) frappe.dom.unfreeze(); };
		lift();
		var watch = setInterval(lift, 100);
		return new Promise(function (resolve) {
			var done = false;
			var finish = function (ok) {
				if (done) return;
				done = true;
				clearInterval(watch);
				lift();
				resolve(ok);
			};
			var d = new frappe.ui.Dialog({
				title: __('Banned customer'),
				indicator: 'red',
				fields: [
					{ fieldtype: 'HTML', fieldname: 'why', options:
						'<div class="alert alert-danger" style="margin:0">'
						+ '<p><b>' + api.escText(flags.customer_name) + '</b> ' + __('is Banned. Do not serve this customer.') + '</p>'
						+ (flags.status_reason ? '<p>' + __('Reason') + ': ' + api.escText(flags.status_reason) + '</p>' : '')
						+ (flags.can_override ? '' : '<p>' + __('Only a manager can override.') + '</p>')
						+ '</div>' },
					{ fieldtype: 'Small Text', fieldname: 'reason', label: __('Override reason'),
						hidden: flags.can_override ? 0 : 1 },
				],
				primary_action_label: flags.can_override ? __('Override ban (30 min)') : __('Close'),
				primary_action: function (v) {
					if (!flags.can_override) { d.hide(); return; }
					if (!(v.reason || '').trim()) {
						frappe.msgprint(__('Enter a reason for serving this customer.'));
						return;
					}
					frappe.call({
						method: METHOD + 'override_customer_ban',
						args: { customer: flags.name, reason: v.reason },
						freeze: true,
					}).then(function () {
						frappe.show_alert({ message: __('Ban overridden for 30 minutes.'), indicator: 'orange' });
						// Fresh flags (the strip now says "override until …"), then go on.
						return fetchFlags(flags.name).catch(function () { delete cache[flags.name]; });
					}).then(function () {
						finish(true);
						d.hide();
					});
				},
			});
			d.onhide = function () { finish(false); };
			d.show();
		});
	}

	// Promise<boolean>: may the counter go on with this customer?
	function check(customer, opts) {
		if (!customer) return Promise.resolve(true);
		var key = (opts && opts.key) || customer;
		if (inflight[key]) return inflight[key];
		var p = fetchFlags(customer).then(function (flags) {
			if (!flags) return true;
			var go = flags.banned ? bannedBox(flags) : Promise.resolve(true);
			return go.then(function (ok) {
				if (ok) showAlerts(flags, key);
				return ok;
			});
		}, function () { return true; /* a failed read never blocks; the server gate still holds */ })
			.then(function (ok) { delete inflight[key]; return ok; });
		inflight[key] = p;
		return p;
	}

	// For a page's Link control: clear it when the customer may not be served.
	function guardControl(control, opts) {
		var customer = control && control.get_value();
		return check(customer, opts).then(function (ok) {
			if (!ok && control.get_value() === customer) control.set_value('');
			return ok;
		});
	}

	// Warn only, never clear: for a pick that records a fact rather than a sale (a
	// customer's transfer gun arriving at Receive Goods comes in either way). A
	// Banned customer gets a red notice; alerts show as usual.
	function warn(customer, opts) {
		if (!customer) return Promise.resolve();
		var key = (opts && opts.key) || customer;
		return fetchFlags(customer).then(function (flags) {
			if (!flags) return;
			// The Link fires change more than once per pick: one notice per pick.
			if (flags.status === 'Banned' && !alerted['banned|' + key]) {
				alerted['banned|' + key] = true;
				setTimeout(function () { delete alerted['banned|' + key]; }, 2000);
				frappe.msgprint({
					title: __('Banned customer'), indicator: 'red',
					message: __('{0} is Banned. The gun is still received into custody for them; a manager decides at pickup.',
						['<b>' + api.escText(flags.customer_name) + '</b>'])
						+ (flags.status_reason ? '<p>' + __('Reason') + ': ' + api.escText(flags.status_reason) + '</p>' : ''),
				});
			}
			showAlerts(flags, key);
		}, function () { /* a hint only */ });
	}

	// ---- New Customer: possible duplicates --------------------------------

	// Promise<{use: name} | {create: true}>.
	function confirmNotDuplicate(values) {
		var args = { email: values.email || '', phone: values.phone || '', id_number: values.id_number || '' };
		if (!args.email && !args.phone && !args.id_number) return Promise.resolve({ create: true });
		return frappe.call({ method: METHOD + 'find_possible_duplicates', args: args }).then(function (r) {
			var rows = r.message || [];
			if (!rows.length) return { create: true };
			return new Promise(function (resolve) {
				var picked = null;
				var html = rows.map(function (row) {
					return '<div style="display:flex; justify-content:space-between; align-items:center; padding:6px 0; border-bottom:1px solid var(--border-color)">'
						+ '<div><b>' + api.escText(row.customer_name) + '</b>'
						+ (row.name !== row.customer_name ? ' <span class="text-muted">' + api.escText(row.name) + '</span>' : '')
						+ (row.customer_status && row.customer_status !== 'Active'
							? ' <span class="indicator-pill ' + (row.customer_status === 'Banned' ? 'red' : 'orange') + '">' + api.escText(__(row.customer_status)) + '</span>' : '')
						+ '<div class="small text-muted">' + __('Same {0}', [api.escText(api.dupLabel(row, __))]) + '</div></div>'
						+ '<button class="btn btn-xs btn-default ffl-use-customer" data-name="' + api.escText(row.name) + '">' + __('Use this customer') + '</button></div>';
				}).join('');
				var d = new frappe.ui.Dialog({
					title: __('This customer may already exist'),
					fields: [{ fieldtype: 'HTML', fieldname: 'rows', options: html }],
					primary_action_label: __('Create new anyway'),
					primary_action: function () { picked = { create: true }; d.hide(); },
				});
				d.$wrapper.on('click', '.ffl-use-customer', function () {
					picked = { use: $(this).attr('data-name') };
					d.hide();
				});
				d.onhide = function () { resolve(picked || { cancel: true }); };
				d.show();
			});
		});
	}

	// ---- Scan ID at the register (Store Customer ID Numbers on) ----------

	function idsOn() { return !!(frappe.boot && frappe.boot.ffl_store_customer_id_numbers); }

	// Promise<customer name | null>.
	function scanForCustomer() {
		return new Promise(function (resolve) {
			var result = null, timer = null, picking = false, restoreScanner = function () {};
			var d = new frappe.ui.Dialog({
				title: __('Find customer by ID'),
				fields: [{ fieldtype: 'Small Text', fieldname: 'scan', label: __('Scan the barcode on the back of the licence') }],
			});
			var lookup = function (raw) {
				var parsed = window.ffl && ffl.aamva && ffl.aamva.parse(raw, { ids: true });
				if (!parsed || !parsed.idNumber) return;
				d.set_value('scan', '');
				frappe.call({ method: METHOD + 'find_customers_by_id_number', args: { id_number: parsed.idNumber } })
					.then(function (r) {
						var rows = r.message || [];
						if (rows.length === 1) { result = rows[0].name; d.hide(); return; }
						if (!rows.length) {
							d.hide();
							frappe.msgprint(__('No customer has ID {0}. Add one with "Create a new Customer" and scan the licence there.',
								[api.maskId(parsed.idNumber)]));
							return;
						}
						var pick = new frappe.ui.Dialog({
							title: __('Several customers have this ID'),
							fields: [{ fieldtype: 'Select', fieldname: 'name', label: __('Customer'), reqd: 1,
								options: rows.map(function (x) { return { value: x.name, label: x.customer_name + ' (' + x.name + ')' }; }) }],
							primary_action_label: __('Use'),
							primary_action: function (v) { result = v.name; pick.hide(); },
						});
						pick.onhide = function () { resolve(result); };
						picking = true;   // the picker resolves instead
						d.hide();
						pick.show();
					});
			};
			d.onhide = function () {
				restoreScanner();
				if (!picking) resolve(result);
			};
			d.show();
			var $in = d.fields_dict.scan.$input;
			// Same scanner handling as the New Customer dialog: keep the POS item
			// scanner off the textarea, finish once the keystrokes go quiet.
			var data = document.scannerDetectionData;
			if (window.onScan && data && data.options) {
				var previous = data.options.ignoreIfFocusOn;
				onScan.setOptions(document, { ignoreIfFocusOn: 'textarea' });
				restoreScanner = function () {
					try { onScan.setOptions(document, { ignoreIfFocusOn: previous }); } catch (e) { /* ignore */ }
				};
			}
			$in.on('input', function () {
				clearTimeout(timer);
				timer = setTimeout(function () { lookup($in.val()); }, 1200);
			});
			setTimeout(function () { $in.focus(); }, 150);
		});
	}

	// ---- POS register ------------------------------------------------------

	function isRegister(frm) { return !!(window.cur_pos && cur_pos.frm === frm); }

	function patchCart(ItemCart) {
		var proto = ItemCart && ItemCart.prototype;
		if (!proto || proto.__fflCustomerFlags) return;
		proto.__fflCustomerFlags = true;
		var update = proto.update_customer_section;
		proto.update_customer_section = function () {
			var out = update.apply(this, arguments);
			try {
				var cart = this, c = this.customer_info && this.customer_info.customer;
				var paint = function () {
					var html = c && cache[c] ? api.stripHtml(cache[c], __) : '';
					var $details = cart.$customer_section.find('.customer-details').first();
					cart.$customer_section.find('.ffl-customer-flags, .ffl-agreements-btn').remove();
					if (html) $details.append(html);
					// Agreements for this customer and cart (agreement_actions.js).
					if (c && window.ffl && ffl.agreements && ffl.agreements.forSale) {
						$('<button class="btn btn-xs btn-default ffl-agreements-btn" style="margin-top:4px">' + __('Agreements') + '</button>')
							.appendTo($details)
							.on('click', function (e) { e.stopPropagation(); ffl.agreements.forSale(cart.events.get_frm()); });
					}
				};
				paint();
				if (c) listenForSignatures();
				// A resumed held draft (load_invoice) never fires the customer event, so its
				// flags may be missing or old: read them again, then paint if still showing.
				if (c && !(cache[c] && Date.now() - (fetchedAt[c] || 0) < CACHE_MS)) {
					fetchFlags(c).then(function () {
						if (cart.customer_info && cart.customer_info.customer === c) paint();
					}, function () { /* the strip is a hint */ });
				}
			} catch (e) { /* never break the cart */ }
			return out;
		};
		var make = proto.make_customer_selector;
		proto.make_customer_selector = function () {
			var out = make.apply(this, arguments);
			try {
				var cart = this;
				if (idsOn() && !cart.$customer_section.find('.ffl-scan-id').length) {
					$('<button class="btn btn-xs btn-default ffl-scan-id" style="margin-top:4px">' + __('Scan ID') + '</button>')
						.appendTo(cart.$customer_section)
						.on('click', function () {
							scanForCustomer().then(function (name) {
								if (name && cart.customer_field) cart.customer_field.set_value(name);
							});
						});
				}
			} catch (e) { /* never break the cart */ }
			return out;
		};
	}

	frappe.ui.form.on('POS Invoice', {
		customer: function (frm) {
			var customer = frm.doc.customer;
			if (!customer || frm.doc.is_return || frm.doc.docstatus !== 0) return;
			return check(customer, { key: (frm.doc.name || '') + '|' + customer }).then(function (ok) {
				if (ok) return;
				if (isRegister(frm)) {
					cur_pos.cart.reset_customer_selector();
					// Hold ERPNext's own follow-up (load + show the customer) for good:
					// the selector was just reset, nothing must re-show the Banned one.
					return new Promise(function () {});
				}
				if (frm.doc.customer === customer) frm.set_value('customer', '');
			});
		},
	});

	// A signature lands from the tablet or the customer's phone: re-read the register
	// customer's flags and repaint, so the clerk never refreshes the page (and loses an
	// unsaved cart) to see it. Event from agreements._announce_signed.
	function onAgreementSigned(msg) {
		var cart = window.cur_pos && cur_pos.cart;
		var c = cart && cart.customer_info && cart.customer_info.customer;
		if (!c || !msg || msg.customer !== c) return;
		fetchFlags(c).then(function () {
			if (cart.customer_info && cart.customer_info.customer === c) cart.update_customer_section();
			frappe.show_alert({ message: __('{0} signed {1}.', [api.escText(cart.customer_info.customer_name || c),
				api.escText(msg.agreement || '')]), indicator: 'green' }, 7);
		}, function () { /* the strip is a hint */ });
	}

	// Once, from the cart: at page load frappe.realtime has no socket yet (on() is a no-op).
	var listening = false;
	function listenForSignatures() {
		if (listening || !(frappe.realtime && frappe.realtime.socket) || !frappe.model.can_read('Customer Agreement')) return;
		listening = true;
		frappe.realtime.doctype_subscribe('Customer Agreement');
		frappe.realtime.on('customer_agreement_signed', onAgreementSigned);
	}

	function wire() {
		frappe.provide('erpnext.PointOfSale');
		var hook = window.ffl && ffl.installClassHook;
		if (hook) hook(erpnext.PointOfSale, 'ItemCart', patchCart);
	}
	if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
	else wire();

	Object.assign(api, {
		check: check, guardControl: guardControl, warn: warn, fetch: fetchFlags,
		strip: function (flags) { return api.stripHtml(flags, __); },
		confirmNotDuplicate: confirmNotDuplicate, scanForCustomer: scanForCustomer, patchCart: patchCart,
	});
})();
