// Waivers and agreements at the counter (rules: ffl_core/agreements.py).
//
//   ffl.agreements.request(customer, template?) — the Request agreement dialog:
//     • Send to tablet (default once a Signing Terminal is paired): the agreement
//       opens on the store's signing tablet by itself (ffl_core/signing_terminal.py).
//     • Show QR code: the store's tablet, or the customer's phone, scans it
//       and signs on the page /agreement/sign. The link works once, for 72 hours.
//     • Email link: the same link, sent to the customer's email address.
//     • Record paper copy (managers): opens the customer's Pending copy, where the
//       signed scan is attached and recorded.
//   ffl.agreements.recordPaper(frm) — the paper-copy dialog on a Customer Agreement.
// The counter panel's agreement chips (customer_flags.js) and the Customer form's
// Agreements section open request(). ffl.agreements.forSale(frm) — the register's
// Agreements button: every active agreement for this customer and cart, each
// Each firearm sold one once per gun, with a Send per row.
(function () {
	if (typeof window === 'undefined' || typeof module === 'object') return;
	window.ffl = window.ffl || {};
	var M = 'ffl_core.agreements.';
	var esc = function (s) { return frappe.utils.escape_html(s == null ? '' : String(s)); };
	var managers = ['Sales Manager', 'System Manager'];
	function isManager() { return managers.some(function (r) { return frappe.user.has_role(r); }); }

	function showQr(r, customerName) {
		var d = new frappe.ui.Dialog({
			title: __('Scan to sign'),
			fields: [{ fieldtype: 'HTML', fieldname: 'qr', options:
				'<div style="text-align:center">'
				+ '<p>' + __('{0}: scan with the store tablet or the customer\'s phone.', ['<b>' + esc(customerName) + '</b>']) + '</p>'
				+ '<img src="' + r.qr_svg + '" alt="QR" style="width:280px; max-width:100%; image-rendering:pixelated">'
				+ '<p class="text-muted small" style="margin-top:8px">' + __('The link works once, until {0}.', [esc(frappe.datetime.str_to_user(r.token_expires))]) + '</p>'
				+ '<p><a href="' + esc(r.sign_url) + '" target="_blank" rel="noopener">' + __('Open on this screen') + '</a></p>'
				+ '</div>' }],
			primary_action_label: __('Done'),
			primary_action: function () { d.hide(); },
		});
		d.show();
		return d;
	}

	// The pending copy may still have a working link — the customer could be signing
	// on the tablet right now. Ask before a new link cancels it.
	// extra: {serial_no, price} for an agreement signed for each firearm sold;
	// {reference_doctype, reference_name} for one signed for a record (Per document).
	function send(customer, template, deliver, replace, terminal, extra) {
		extra = extra || {};
		return frappe.call({ method: M + 'request_agreement', freeze: true,
			args: { customer: customer, template: template, deliver: deliver, replace: replace ? 1 : 0, terminal: terminal || null,
				serial_no: extra.serial_no || null, price: extra.price || null,
				reference_doctype: extra.reference_doctype || null, reference_name: extra.reference_name || null } })
			.then(function (r) {
				var m = r.message || {};
				if (!m.link_open) return m;
				return new Promise(function (resolve) {
					frappe.confirm(__('A signing link for this agreement is still open (until {0}); the customer may be signing it now. Make a new one? The open link will stop working.',
						[esc(frappe.datetime.str_to_user(m.token_expires))]),
					function () { resolve(send(customer, template, deliver, true, terminal, extra)); });
				});
			});
	}

	// Which signing tablet to send to: the one used last on this computer, else the one
	// set up for this register, else the first paired one.
	var LAST_TERMINAL = 'ffl-agreement-terminal';
	function pickTerminal(terminals) {
		var paired = terminals.filter(function (t) { return t.paired; });
		var last = null;
		try { last = localStorage.getItem(LAST_TERMINAL); } catch (e) { /* no storage */ }
		var profile = window.cur_pos && cur_pos.frm && cur_pos.frm.doc && cur_pos.frm.doc.pos_profile;
		var hit = paired.filter(function (t) { return t.name === last; })[0]
			|| paired.filter(function (t) { return profile && t.pos_profile === profile; })[0]
			|| paired[0];
		return hit ? hit.name : '';
	}

	function request(customer, template, opts) {
		opts = opts || {};
		var done = opts.done || function () {};
		var manager = isManager();
		var who = opts.customer_name || customer;
		// One gun's copy of an Each firearm sold agreement, or one record's copy of a Per
		// document one: the template and what it is for are fixed.
		var extra = opts.serial_no ? { serial_no: opts.serial_no, price: opts.price }
			: opts.reference_name ? { reference_doctype: opts.reference_doctype, reference_name: opts.reference_name } : null;
		return frappe.call({ method: 'ffl_core.signing_terminal.list_terminals' })
			.then(function (r) { return (r.message || []).filter(function (t) { return t.paired; }); },
				function () { return []; })
			.then(function (terminals) { return dialog(terminals); });

		function dialog(terminals) {
			var tablets = terminals.length > 0;
			var fields = [
				// A gun's copy is fixed (and an Each firearm sold template is not one the picker offers).
				extra ? { fieldtype: 'Data', fieldname: 'template', label: __('Agreement'), read_only: 1, default: template }
					: { fieldtype: 'Link', fieldname: 'template', label: __('Agreement'), options: 'Agreement Template', reqd: 1,
						default: template || '', get_query: function () { return { filters: { active: 1, scope: 'Per customer' } }; } },
			];
			if (extra && extra.reference_name) {
				fields.push({ fieldtype: 'HTML', fieldname: 'record', options: '<p>' + __('For {0} {1}',
					[esc(__(extra.reference_doctype)), '<b>' + esc(extra.reference_name) + '</b>']) + '</p>' });
			} else if (extra) {
				fields.push({ fieldtype: 'HTML', fieldname: 'gun', options: '<p>' + __('For the firearm {0}', ['<b>'
					+ esc((opts.item_name ? opts.item_name + ' · ' : '') + __('serial {0}', [opts.serial_no])) + '</b>']) + '</p>' });
			}
			if (tablets) {
				fields.push({ fieldtype: 'Select', fieldname: 'terminal', label: __('Signing tablet'),
					options: terminals.map(function (t) { return t.name; }), default: pickTerminal(terminals) });
			}
			fields.push({ fieldtype: 'HTML', fieldname: 'help', options: '<p class="text-muted small">'
				+ (tablets ? __('Send to tablet: it opens on the store tablet for the customer to sign.') + ' ' : '')
				+ __('Show QR code: the customer scans it with a tablet or their phone. Email link: they sign from home.')
				+ (manager ? ' ' + __('Record paper copy: attach a copy the customer signed on paper.') : '') + '</p>' });
			var showQrCode = function (v) {
				send(customer, v.template, 'qr', false, null, extra).then(function (r) {
					d.hide();
					showQr(r, who).onhide = done;
				});
			};
			var d = new frappe.ui.Dialog({
				title: __('Request agreement'),
				fields: fields,
				primary_action_label: tablets ? __('Send to tablet') : __('Show QR code'),
				primary_action: function (v) {
					if (!tablets) return showQrCode(v);
					send(customer, v.template, 'tablet', false, v.terminal, extra).then(function (r) {
						try { localStorage.setItem(LAST_TERMINAL, v.terminal); } catch (e) { /* no storage */ }
						d.hide();
						frappe.show_alert({ message: __('Sent to {0}. {1} can sign there now.', [esc(r.terminal), esc(who)]), indicator: 'green' });
						done();
					});
				},
				secondary_action_label: tablets ? __('Show QR code') : __('Email link'),
				secondary_action: function () {
					var v = d.get_values();
					if (!v) return;
					if (tablets) return showQrCode(v);
					emailLink(v);
				},
			});
			var emailLink = function (v) {
				send(customer, v.template, 'email', false, null, extra).then(function (r) {
					d.hide();
					frappe.show_alert({ message: __('Signing link emailed to {0}', [esc(r.email)]), indicator: 'green' });
					done();
				});
			};
			if (tablets) {
				d.add_custom_action(__('Email link'), function () {
					var v = d.get_values();
					if (v) emailLink(v);
				});
			}
			if (manager) {
				d.add_custom_action(__('Record paper copy'), function () {
					var v = d.get_values();
					if (!v) return;
					send(customer, v.template, 'none', false, null, extra).then(function (r) {
						d.hide();
						// A new tab: the register's cart (or the customer form) stays as it is.
						window.open('/app/customer-agreement/' + encodeURIComponent(r.name) + '?ffl_record_paper=1', '_blank');
						done();
					});
				});
			}
			d.show();
			return d;
		}
	}

	function recordPaper(frm) {
		var d = new frappe.ui.Dialog({
			title: __('Record paper copy'),
			fields: [
				{ fieldtype: 'HTML', options: '<p class="text-muted small">'
					+ __('Attach a scan or photo of the paper the customer signed. It is kept private.') + '</p>' },
				{ fieldtype: 'Attach', fieldname: 'file_url', label: __('Signed paper'), reqd: 1 },
				{ fieldtype: 'Data', fieldname: 'signer_name', label: __('Name as signed'), reqd: 1,
					default: frm.doc.customer_name },
				{ fieldtype: 'Date', fieldname: 'paper_signed_on', label: __('Date on the paper'), reqd: 1,
					default: frappe.datetime.get_today() },
				{ fieldtype: 'Check', fieldname: 'participant_is_minor', label: __('Participant is under 18') },
				{ fieldtype: 'Select', fieldname: 'signer_relationship', label: __('Signed by'),
					options: ['Self', 'Parent', 'Guardian'], default: 'Self' },
			],
			primary_action_label: __('Record'),
			primary_action: function (v) {
				frappe.call({ method: M + 'record_paper_agreement', freeze: true, args: {
					name: frm.doc.name, file_url: v.file_url, signer_name: v.signer_name,
					paper_signed_on: v.paper_signed_on, participant_is_minor: v.participant_is_minor ? 1 : 0,
					signer_relationship: v.signer_relationship,
				} }).then(function () {
					d.hide();
					frappe.show_alert({ message: __('Paper copy recorded'), indicator: 'green' });
					frm.reload_doc();
				});
			},
		});
		// Attach uploads onto this agreement, so the server can tell the scan is its own.
		d.fields_dict.file_url.df.options = { doctype: frm.doctype, docname: frm.doc.name };
		d.show();
		return d;
	}

	// The cart as agreements_for_sale reads it.
	function cartOf(frm) {
		return (frm.doc.items || []).filter(function (r) { return r.item_code; })
			.map(function (r) { return { item_code: r.item_code, serial_no: r.serial_no || '', rate: r.rate || 0 }; });
	}

	var STATE = {
		signed: [__('Signed'), 'green'], expiring: [__('Expires soon'), 'yellow'], pending: [__('Sent, not signed'), 'yellow'],
		expired: [__('Expired'), 'orange'], missing: [__('Not signed'), 'gray'],
	};

	// The register's Agreements button: every active agreement and where this customer
	// stands for this cart — an Each firearm sold one once per gun. Each row sends it
	// (tablet / QR / email) through request().
	function forSale(frm) {
		var customer = frm.doc.customer;
		if (!customer) { frappe.msgprint(__('Pick the customer first.')); return; }
		var d = new frappe.ui.Dialog({ title: __('Agreements for {0}', [esc(frm.doc.customer_name || customer)]),
			fields: [{ fieldtype: 'HTML', fieldname: 'rows' }], size: 'large' });
		var load = function () {
			frappe.call({ method: M + 'agreements_for_sale', args: { customer: customer, items: cartOf(frm),
				sale: frm.doc.__islocal ? null : frm.doc.name } }).then(function (r) {
				var rows = (r.message || {}).rows || [];
				if (!rows.length) {
					d.fields_dict.rows.$wrapper.html('<p class="text-muted">' + __('No agreements are switched on. Add one under Customers → Agreement templates.') + '</p>');
					return;
				}
				var html = rows.map(function (row, i) {
					var st = STATE[row.state] || STATE.missing;
					var done = row.state === 'signed' || row.state === 'expiring';
					var gun = row.serial_no ? '<div class="small text-muted">' + esc((row.item_name || row.item_code || '') + ' · ' + __('serial {0}', [row.serial_no])) + '</div>'
						: (row.scope === 'Each firearm sold' ? '<div class="small text-muted">' + __('Signed for each firearm sold; no gun with a serial in the cart yet.') + '</div>' : '');
					var needs = row.required ? ' <span class="indicator-pill red">' + __('This sale needs it') + '</span>' : '';
					var act = (row.scope === 'Each firearm sold' && !row.serial_no) || done ? ''
						: '<button class="btn btn-xs btn-primary" data-ffl-send="' + i + '">' + __('Send') + '</button>';
					return '<div style="display:flex; align-items:center; justify-content:space-between; gap:12px; padding:8px 0; border-bottom:1px solid var(--border-color)">'
						+ '<div><b>' + esc(row.template) + '</b>' + needs + gun + '</div>'
						+ '<div style="display:flex; align-items:center; gap:8px; white-space:nowrap"><span class="indicator-pill ' + st[1] + '">' + esc(st[0]) + '</span>' + act + '</div></div>';
				}).join('');
				d.fields_dict.rows.$wrapper.html(html);
				d.fields_dict.rows.$wrapper.find('[data-ffl-send]').on('click', function () {
					var row = rows[+$(this).attr('data-ffl-send')];
					request(customer, row.template, { customer_name: frm.doc.customer_name, serial_no: row.serial_no,
						price: row.rate, item_name: row.item_name, done: load });
				});
			});
		};
		d.show();
		load();
		return d;
	}

	// The register: once the cart holds something this customer must sign (an Item or
	// its Item Group that names an agreement, a gun for an Each firearm sold one, or an
	// agreement every sale needs), say so once and offer the Agreements list. The submit
	// is refused anyway (agreements.refuse_missing_required).
	var prompted = {};
	var promptTimer = null;
	function promptIfNeeded(frm) {
		var customer = frm.doc.customer;
		if (!customer || frm.doc.is_return || frm.doc.docstatus !== 0) return;
		clearTimeout(promptTimer);
		promptTimer = setTimeout(function () {
			frappe.call({ method: M + 'agreements_for_sale', args: { customer: customer, items: cartOf(frm),
				sale: frm.doc.__islocal ? null : frm.doc.name } }).then(function (r) {
				var fresh = ((r.message || {}).rows || []).filter(function (row) {
					var key = customer + '|' + row.template + '|' + (row.serial_no || '');
					if (!row.required || row.state === 'signed' || row.state === 'expiring' || prompted[key]) return false;
					prompted[key] = true;
					return true;
				});
				if (!fresh.length) return;
				var list = fresh.map(function (row) {
					return '<li><b>' + esc(row.template) + '</b>' + (row.serial_no ? ' — ' + esc((row.item_name || '') + ' · ' + row.serial_no) : '') + '</li>';
				}).join('');
				frappe.confirm('<p>' + __('This sale needs:') + '</p><ul>' + list + '</ul><p>' + __('Open Agreements to send them?') + '</p>',
					function () { forSale(frm); });
			}, function () { /* a hint; the submit gate holds */ });
		}, 600);
	}
	frappe.ui.form.on('POS Invoice Item', {
		item_code: function (frm) { promptIfNeeded(frm); },
		serial_no: function (frm) { promptIfNeeded(frm); },
	});

	// Counter panel chips (customer_flags.stripHtml): click to ask for the agreement.
	$(document).on('click', '[data-ffl-agreement-template]', function (e) {
		e.preventDefault();
		request($(this).attr('data-ffl-agreement-customer'), $(this).attr('data-ffl-agreement-template'));
	});

	window.ffl.agreements = { request: request, recordPaper: recordPaper, showQr: showQr, forSale: forSale };
})();
