// GunBroker — listing buttons on the Item form.
//
// The other half of the channel. A gun priced one by one is listed from its
// Serial No form (ffl_integrations/public/js/serial_no.js). Ammunition, optics and
// parts are listed by model from here, as one listing carrying the quantity in
// stock — and so is a UNIFORM-PRICE gun: one listing standing for the guns that are
// free to sell, each sale of which staff later name a serial for.
//
// Gated before a button is drawn: the integration must be switched on, and the Item
// must not be a gun priced one by one (that Item has no listing of its own — its
// guns do — and offering Send here would be a button that can only come back
// refused). A uniform-price gun gets a Send dialog with its own figures and paid
// promotions; everything else keeps the shipping prompt. The server re-checks every
// one of them.

frappe.ui.form.on('Item', {
	refresh(frm) {
		if (frm.is_new()) return;

		// Defaults to FALSE when the server did not say, so a boot hiccup hides
		// the buttons rather than offering actions that would come back refused.
		const sw = window.ffl && window.ffl.switches;
		if (!sw || !sw.gunbrokerEnabled()) return;

		// cint(), not Boolean(): a Check field arrives as "0"/"1" on some paths
		// and Boolean("0") is true — which would put listing buttons on exactly
		// the Items that must not have them.
		if (cint(frm.doc.is_firearm) && !cint(frm.doc.uniform_sell_price)) return;

		_gbItemButtons(frm);
	},
});

// Every listing carries a buyer-paid shipping rate, and the person pressing
// Send decides it. The store default is offered; a different figure is saved
// on the record and used from then on. Leaving the default as it is sends
// nothing, so the record keeps following the default when that changes.
function _gbAskShipping(frm, doctype, then) {
	frappe.call({
		method: 'ffl_integrations.gunbroker.client_api.listing_ship_cost',
		args: { doctype: doctype, name: frm.doc.name },
		callback(resp) {
			const m = resp.message || {};
			const own = flt(m.own), dflt = flt(m.default);
			frappe.prompt([{
				fieldtype: 'Currency', fieldname: 'ship_cost', reqd: 1,
				label: __('Shipping cost (buyer pays)'),
				default: own > 0 ? own : dflt,
				description: own > 0
					? __('Saved on this record. The store default is {0}.', [format_currency(dflt)])
					: __('The store default. Change it to set a rate for this record only.'),
			}], function (values) {
				const chosen = flt(values.ship_cost);
				then(own <= 0 && chosen === dflt ? null : chosen);
			}, __('Send to GunBroker'), __('Send'));
		},
	});
}

// A uniform-price gun's model listing: what it would carry now, so the person
// pressing Send decides with the figures in front of them. The title, price and
// category follow the Item (and the web store's price today), so the only choices
// here are the shipping rate and GunBroker's paid promotions. Those cost extra,
// can only be bought now (GunBroker has no call that adds one to a live listing),
// and are NOT bought again when POS puts a sold-out model back up by itself.
function _gbAskModel(frm, then) {
	frappe.call({
		method: 'ffl_integrations.gunbroker.client_api.item_listing_offer',
		args: { item_code: frm.doc.name },
		callback(resp) {
			const o = resp.message || {};
			const ship = o.shipping || {};
			const own = flt(ship.own), dflt = flt(ship.default);
			const cat = o.category || {};
			const premium = o.premium || {};
			const esc = frappe.utils.escape_html;
			const catText = cat.id
				? `${cat.id} ${esc(cat.name || '')}`.trim()
				: `<span class="text-danger">${__('none configured')}</span>`;
			const priceText = o.on_sale
				? __('{0} — on sale{1} (regular {2})', [format_currency(o.price),
					o.sale_to ? __(' until {0}', [frappe.datetime.str_to_user(o.sale_to)]) : '',
					format_currency(o.regular_price)])
				: format_currency(o.price);
			const sellable = o.sellable === null || o.sellable === undefined
				? `<span class="text-danger">${__('could not be worked out')}</span>`
				: esc(String(o.sellable));
			const rows = [
				[__('Guns to offer'), sellable],
				[__('Price'), priceText],
				[__('Category'), catText + (cat.ambiguous
					? ` <span class="text-danger">${__('— needs a year made or a GunBroker Category ID on the Item')}</span>`
					: '')],
			];
			frappe.prompt([
				{ fieldtype: 'HTML', fieldname: 'summary',
					options: `<table class="table table-bordered" style="margin-bottom:10px">${rows.map(
						([k, v]) => `<tr><td style="width:40%"><b>${k}</b></td><td>${v}</td></tr>`,
					).join('')}</table>
					<div class="text-muted small">${__('The listing follows this Item: the price changes with the web store\'s (promotions included), and the number of guns with what can be sold on every channel.')}</div>` },
				{ fieldtype: 'Currency', fieldname: 'ship_cost', reqd: 1,
					label: __('Shipping cost (buyer pays)'),
					default: own > 0 ? own : dflt,
					description: own > 0
						? __('Saved on this Item. The store default is {0}.', [format_currency(dflt)])
						: __('The store default. Change it to set a rate for this Item only.') },
				// Same wording as the Serial No dialog: it is the same GunBroker charge.
				{ fieldtype: 'Section Break', fieldname: 'gb_sec_premium', label: __('Paid promotion') },
				{ fieldtype: 'HTML', fieldname: 'premium_note',
					options: `<div class="text-muted small" style="margin-bottom:8px">${
						__('GunBroker charges extra for each one ticked, every time the listing is created. They can only be chosen now, when the listing is created: POS cannot add them to a listing that is already live. If GunBroker renumbers the listing itself the charge may repeat. A listing POS puts back up after a sell-out never buys them.')
						}${premium.featured || premium.showcase || premium.sponsored_onsite || premium.view_counter
							? ` <b>${__('Ticked because this model had them last time.')}</b>` : ''}</div>` },
				{ fieldtype: 'Check', fieldname: 'premium_featured', label: __('Featured'),
					default: premium.featured ? 1 : 0 },
				{ fieldtype: 'Check', fieldname: 'premium_showcase', label: __('Showcase'),
					default: premium.showcase ? 1 : 0 },
				{ fieldtype: 'Column Break', fieldname: 'gb_col_premium' },
				{ fieldtype: 'Check', fieldname: 'premium_sponsored_onsite', label: __('Sponsored on-site'),
					default: premium.sponsored_onsite ? 1 : 0 },
				{ fieldtype: 'Check', fieldname: 'premium_view_counter', label: __('View counter'),
					default: premium.view_counter ? 1 : 0 },
			], function (v) {
				const chosen = flt(v.ship_cost);
				// All four every time, so unticking a recorded one clears it.
				const listing = {
					type: 'Fixed Price',
					premium: {
						featured: cint(v.premium_featured), showcase: cint(v.premium_showcase),
						sponsored_onsite: cint(v.premium_sponsored_onsite),
						view_counter: cint(v.premium_view_counter),
					},
				};
				then(own <= 0 && chosen === dflt ? null : chosen, listing);
			}, __('Send to GunBroker'), __('Send'));
		},
	});
}

function _gbItemButtons(frm) {
	const GROUP = __('GunBroker');
	const isModel = cint(frm.doc.is_firearm) && cint(frm.doc.uniform_sell_price);

	if (!frm.doc.gb_item_id) {
		const relist = cint(frm.doc.gb_delisted) ? 1 : 0;
		const $send = frm.add_custom_button(__('Send to GunBroker'), function () {
			const ask = isModel
				? _gbAskModel
				: function (f, cb) { _gbAskShipping(f, 'Item', function (ship) { cb(ship, null); }); };
			ask(frm, function (ship_cost, listing) {
				const args = { item_code: frm.doc.name, relist: relist };
				if (ship_cost !== null) args.ship_cost = ship_cost;
				if (listing) args.listing = JSON.stringify(listing);
				_gbItemCall(frm, {
					method: 'ffl_integrations.gunbroker.client_api.push_item_now',
					args: args,
					freeze_message: __('Listing on GunBroker…'),
					onOk(m) {
						frappe.show_alert({
							message: __('Listed on GunBroker — item {0}, {1} available.',
								[m.gb_item_id, m.quantity]),
							indicator: 'green',
						}, 7);
						_gbItemWarnings(m.warnings, m.gb_url);
					},
				});
			});
		}, GROUP);
		$send.removeClass('btn-default').addClass('btn-success');
	}

	if (frm.doc.gb_item_id) {
		frm.add_custom_button(__('Reprice on GunBroker'), function () {
			_gbItemCall(frm, {
				method: 'ffl_integrations.gunbroker.client_api.reprice_item_now',
				args: { item_code: frm.doc.name },
				freeze_message: __('Updating the GunBroker price…'),
				onOk(m) {
					frappe.show_alert({
						message: __('GunBroker price is now {0}.',
							[format_currency(m.fixed_price)]),
						indicator: 'green',
					});
				},
			});
		}, GROUP);

		// The quantity normally looks after itself — every stock movement pushes
		// it. This is the repair for a count that has drifted, which is why it
		// says what the two numbers were rather than just "done".
		frm.add_custom_button(__('Sync GunBroker Quantity'), function () {
			_gbItemCall(frm, {
				method: 'ffl_integrations.gunbroker.client_api.sync_item_quantity_now',
				args: { item_code: frm.doc.name },
				freeze_message: __('Updating the GunBroker quantity…'),
				onOk(m) {
					frappe.show_alert({
						message: m.unchanged
							? __('GunBroker already had {0}.', [m.quantity])
							: __('GunBroker quantity is now {0}.', [m.quantity]),
						indicator: 'green',
					});
				},
			});
		}, GROUP);

		frm.add_custom_button(__('GunBroker Status'), function () {
			frappe.call({
				method: 'ffl_integrations.gunbroker.client_api.item_listing_status',
				args: { item_code: frm.doc.name },
				freeze: true,
				freeze_message: __('Checking GunBroker…'),
				callback(resp) { _gbItemStatusDialog(resp.message || {}); },
			});
		}, GROUP);

		const $end = frm.add_custom_button(__('End GunBroker Listing'), function () {
			frappe.confirm(
				__('End this model\'s GunBroker listing? It will not go back up on its own, even when more stock arrives — send it again by hand.'),
				function () {
					_gbItemCall(frm, {
						method: 'ffl_integrations.gunbroker.client_api.end_item_listing_now',
						args: { item_code: frm.doc.name },
						freeze_message: __('Ending the GunBroker listing…'),
						onOk(m) {
							if (m.confirmed) {
								frappe.show_alert({
									message: __('Ended on GunBroker (confirmed — the listing reads as inactive).'),
									indicator: 'green',
								});
								return;
							}
							// Not ended. Until somebody ends it there, it can still
							// be bought — and the stock behind it is spoken for.
							frappe.msgprint({
								title: __('End it on GunBroker by hand'),
								message: `${frappe.utils.escape_html(m.message || '')}
									<br><br><a href="${frappe.utils.escape_html(m.gb_url || '')}" target="_blank" rel="noopener">${__('Open item {0} on GunBroker', [frappe.utils.escape_html(String(m.gb_item_id || ''))])}</a>`,
								indicator: 'red',
							});
						},
					});
				});
		}, GROUP);
		$end.removeClass('btn-default').addClass('btn-danger');
	}
}

function _gbItemCall(frm, opts) {
	frappe.call({
		method: opts.method,
		args: opts.args,
		freeze: true,
		freeze_message: opts.freeze_message,
		callback(resp) {
			const m = resp.message || {};
			if (m.ok) {
				opts.onOk(m);
			} else {
				// Neither ok nor a named refusal: say so rather than reloading
				// the form and leaving the operator to guess whether it worked.
				frappe.msgprint({
					title: __('Not sent to GunBroker'),
					message: m.message || __('GunBroker did not answer. Nothing was changed.'),
					indicator: 'orange',
				});
			}
			frm.reload_doc();
		},
	});
}

function _gbItemWarnings(warnings, url) {
	// Show every warning the server sent (unknown keys escaped) so a new one is
	// never silently dropped; same table as _gbShowWarnings on the Serial No form.
	const notable = (warnings || []).filter((w) => w !== 'claimed_existing');
	if (!notable.length) return;
	const link = url
		? `<a href="${encodeURI(url)}" target="_blank" rel="noopener">${__('open the listing')}</a>`
		: __('open the listing on GunBroker');
	const LABELS = {
		no_upc: __('No UPC, so GunBroker could not fill Manufacturer, Model and Caliber. Add them by hand: {0}, then Edit.', [link]),
		title_truncated: __('The title was shortened to GunBroker\'s 75-character limit.'),
		pictures_truncated: __('Some photos were left off — the listing hit the per-listing picture limit.'),
		pictures_partial: __('The listing is live, but some of the extra photos failed to upload. Add them on GunBroker.'),
		condition_antique_used: __('An antique is always listed as Used, so the condition you picked was not used.'),
		image_format_skipped: __('A photo was skipped: GunBroker takes JPG, PNG and GIF only.'),
		image_unreadable: __('A photo could not be read and was left off the listing.'),
		insecure_image_removed: __('An image in the description pointed at a local address no buyer could reach, and was removed.'),
		upgraded_to_https: __('Links in the description were switched to https so buyers\' browsers do not block them.'),
		plain_http_in_text: __('The description mentions an http:// address in its text. Buyers can read it, but it will not be clickable as an asset.'),
	};
	frappe.msgprint({
		title: __('Listed, with notes'),
		message: notable.map((w) => `<div>${LABELS[w] || frappe.utils.escape_html(w)}</div>`).join(''),
		indicator: 'orange',
	});
}

function _gbItemStatusDialog(m) {
	if (!m.ok) { frappe.msgprint(__('Could not read the GunBroker status.')); return; }
	const remote = m.remote || {};
	const rows = [
		[__('Local state'), m.state || '—'],
		[__('GunBroker item'), m.gb_item_id || '—'],
		[__('In stock here'), m.quantity_local],
		[__('Last sent to GunBroker'), m.quantity_pushed],
		[__('GunBroker answered'), remote.state || '—'],
		[__('Active on GunBroker'), remote.is_active === undefined ? '—' : String(remote.is_active)],
		[__('Quantity on GunBroker'), remote.quantity === undefined ? '—' : String(remote.quantity)],
		[__('Price on GunBroker'), remote.fixed_price === undefined || remote.fixed_price === null
			? '—' : format_currency(remote.fixed_price)],
		[__('Last error'), m.gb_sync_error || '—'],
	];
	frappe.msgprint({
		title: __('GunBroker status'),
		message: `<table class="table table-bordered" style="margin:0">${rows.map(
			([k, v]) => `<tr><td style="width:45%"><b>${k}</b></td><td>${frappe.utils.escape_html(String(v))}</td></tr>`,
		).join('')}</table>`,
		wide: true,
	});
}
