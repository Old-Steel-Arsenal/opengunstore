// FastBound bound-book sync for post-acquisition per-unit corrections.
//
// When an operator edits a firearm's per-unit Manufacturer / Importer on a
// Serial No that is ALREADY in the FastBound bound book (has fastbound_item_id),
// offer to push the correction to FastBound (PUT /Items/{id}). The local edit
// always saves; only the FastBound update is gated behind the confirm — per the
// product decision that fixing the book is the operator's explicit choice.

frappe.ui.form.on('Serial No', {
	refresh(frm) {
		// Reassign this gun to a different model Item (ERPNext ties a serial to one
		// Item, so the backend re-keys it). Resolves the sync's regroup list and
		// general "wrong SKU" fixes; offers to push the new model/caliber to FastBound.
		if (frm.is_new() || !frm.doc.item_code) return;
		frm.add_custom_button(__('Reassign Item'), () => _reassignDialog(frm));
	},

	manufacturer(frm) { _markFbDirty(frm); },
	importer(frm) { _markFbDirty(frm); },
	model_name(frm) { _markFbDirty(frm); },
	caliber(frm) { _markFbDirty(frm); },

	after_save(frm) {
		if (!frm._fbAttrsDirty) return;
		frm._fbAttrsDirty = false;
		_confirmPush(frm,
			__('This firearm is already in the FastBound bound book. Push the updated firearm details (manufacturer / importer / model / caliber) to FastBound?'),
			__('Saved locally — FastBound was not updated.'));
	},
});

// Flag a FastBound-relevant edit. Ignore brand-new docs (no acquisition yet)
// and programmatic loads (Frappe only fires field handlers on real changes).
function _markFbDirty(frm) {
	if (!frm.is_new()) frm._fbAttrsDirty = true;
}

// Offer to push the just-saved change to the FastBound bound book. Only guns
// already in the book have something to update; an unsynced gun's eventual
// acquisition push carries the current values. The confirm gates the push; the
// dismiss path reassures the operator the local save stuck even though FastBound
// wasn't touched.
function _confirmPush(frm, promptMsg, dismissMsg) {
	if (!frm.doc.fastbound_item_id) return;
	frappe.confirm(
		promptMsg,
		() => _pushToFastBound(frm),
		() => frappe.show_alert({ message: dismissMsg, indicator: 'orange' }),
	);
}

function _reassignDialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __('Reassign to another Item'),
		fields: [{
			fieldtype: 'HTML',
			options: `<div class="alert alert-info" style="margin-bottom:10px">${
				__('Moves this firearm to a different model Item; its serial, A&D record, price, photos, condition, manufacturer / importer and stock stay as they are. FastBound is not changed.')}</div>`,
		}, {
			fieldtype: 'Link', fieldname: 'target_item', label: __('Target Item'),
			options: 'Item', reqd: 1,
			get_query: () => ({ filters: { is_firearm: 1, name: ['!=', frm.doc.item_code] } }),
		}],
		primary_action_label: __('Reassign'),
		primary_action(values) {
			d.hide();
			frappe.call({
				method: 'ffl_integrations.fastbound.importer.reassign_serial_to_item',
				args: { serial: frm.doc.name, target_item: values.target_item },
				freeze: true,
				freeze_message: __('Reassigning…'),
				callback: (r) => {
					const m = r.message || {};
					if (!m.ok) return;
					// Reassign is a POS-internal re-bucketing — it never touches the
					// FastBound bound book. FastBound only changes when the operator
					// actively edits the serial's fields (see after_save).
					frappe.show_alert({ message: __('Reassigned {0} → {1}.', [m.from, m.to]), indicator: 'green' }, 6);
					frm.reload_doc();
				},
			});
		},
	});
	d.show();
}

function _pushToFastBound(frm) {
	frappe.call({
		method: 'ffl_integrations.fastbound.tasks.push_serial_to_fastbound',
		args: { serial: frm.doc.name },
		freeze: true,
		freeze_message: __('Updating FastBound…'),
		callback: (r) => {
			const m = r.message || {};
			if (m.ok) {
				frappe.show_alert({
					message: __('FastBound bound book updated.'), indicator: 'green',
				});
			} else if (m.reason === 'not_in_fastbound') {
				frappe.show_alert({
					message: __('Not in FastBound yet — nothing to update.'), indicator: 'blue',
				});
			} else if (m.reason === 'not_configured') {
				frappe.show_alert({
					message: __('FastBound is not configured — skipped.'), indicator: 'orange',
				});
			}
		},
	});
}

// Per-gun "Sync with FastBound": fetch the FB record, show a field-by-field diff,
// let the operator choose per field which side wins, then apply (FB→POS write or
// POS→FB push). Complements the after_save push-on-edit above.
frappe.ui.form.on('Serial No', {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.item_code) return;
		// Nothing on the other end when the integration is switched off — the server
		// answers "not_configured" and the diff would always be empty. Offer nothing
		// rather than a button whose only outcome is a shrug.
		const sw = window.ffl && window.ffl.switches;
		if (sw && !sw.fastboundEnabled()) return;
		frm.add_custom_button(__('Sync with FastBound'), () => _fbSyncStart(frm));
	},
});

function _fbSyncStart(frm) {
	frappe.call({
		method: 'ffl_integrations.fastbound.reconcile.compute_serial_fb_diff',
		args: { serial: frm.doc.name },
		freeze: true,
		freeze_message: __('Checking FastBound…'),
		callback: (r) => _fbSyncHandle(frm, r.message || {}),
	});
}

function _fbSyncHandle(frm, m) {
	if (m.status === 'not_in_fastbound') {
		frappe.confirm(
			__('This gun has no FastBound link. Try to match it by serial number?'),
			() => frappe.call({
				method: 'ffl_integrations.fastbound.reconcile.match_serial_to_fastbound',
				args: { serial: frm.doc.name }, freeze: true,
				callback: (r) => {
					const mm = r.message || {};
					if (mm.ok) { _fbSyncStart(frm); }
					else { frappe.show_alert({ message: __('No FastBound match found ({0}).', [mm.reason || '']), indicator: 'orange' }); }
				},
			}),
		);
		return;
	}
	if (m.status === 'not_configured') { frappe.msgprint(__('FastBound is not configured.')); return; }
	if (m.status === 'fb_missing') { frappe.msgprint(__('FastBound has no record for this gun.')); return; }
	if (m.status === 'fb_disposed') { frappe.msgprint(__('FastBound shows this gun as DISPOSED — resolve the stock mismatch first; not field-syncing.')); return; }
	if (m.status !== 'ok') { frappe.msgprint(__('Could not check FastBound.')); return; }
	if (m.in_sync) { frappe.show_alert({ message: __('✓ Already in sync with FastBound.'), indicator: 'green' }); return; }
	_fbSyncDialog(frm, m.diffs);
}

function _fbSyncDialog(frm, diffs) {
	const fields = [{
		fieldtype: 'HTML', fieldname: 'intro',
		options: `<div class="text-muted" style="margin-bottom:8px">${
			__('"Use POS" pushes your value to the FastBound bound book.')}</div>`,
	}];
	diffs.forEach((d) => {
		fields.push({
			fieldtype: 'Select', fieldname: `choice_${d.field}`,
			label: `${d.label}: POS="${frappe.utils.escape_html(d.pos_value || '—')}" vs FastBound="${frappe.utils.escape_html(d.fb_value || '—')}"`,
			options: ['Use FastBound', 'Use POS'].join('\n'), default: 'Use FastBound',
		});
	});
	const d = new frappe.ui.Dialog({
		title: __('Reconcile with FastBound'),
		fields,
		primary_action_label: __('Apply'),
		primary_action(values) {
			const resolutions = {};
			diffs.forEach((df) => {
				resolutions[df.field] = {
					choice: values[`choice_${df.field}`] === 'Use POS' ? 'pos' : 'fb',
					fb_value: df.fb_value,
				};
			});
			d.hide();
			frappe.call({
				method: 'ffl_integrations.fastbound.reconcile.apply_serial_fb_resolution',
				args: { serial: frm.doc.name, resolutions: JSON.stringify(resolutions) },
				freeze: true, freeze_message: __('Applying…'),
				callback: (r) => {
					const m = r.message || {};
					if (!m.ok) { frappe.show_alert({ message: __('Could not apply ({0}).', [m.reason || '']), indicator: 'red' }); return; }
					const skipped = (m.skipped || []).filter((s) => s.reason === 'changed' || s.reason === 'already_resolved');
					frappe.show_alert({
						message: __('Applied. FastBound updated: {0}.', [m.fastbound_updated ? __('yes') : __('no')]),
						indicator: 'green',
					}, 6);
					frm.reload_doc().then(() => { if (skipped.length) { _fbSyncStart(frm); } });
				},
			});
		},
	});
	d.show();
}

// ---------------------------------------------------------------------------
// GunBroker — per-gun listing buttons.
//
// A third sales channel alongside the web store's own buttons (which live in
// ffl_woo_sync's own file — three apps register handlers for Serial No and none
// of them can see the others, so each block gates itself independently).
//
// Everything is gated three ways before a single button is drawn: the
// integration must be switched on, the gun must belong to a per-serial firearm
// Item, and each button has its own state condition. The server re-checks all
// of it — these gates exist so staff are not offered actions that can only come
// back refused.
// ---------------------------------------------------------------------------
frappe.ui.form.on('Serial No', {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.item_code) return;

		// Nothing on the other end when GunBroker is switched off: the server
		// would answer "disabled" to every one of these. Defaults to FALSE when
		// the server did not say, so a boot hiccup hides the buttons rather than
		// offering them.
		const sw = window.ffl && window.ffl.switches;
		if (!sw || !sw.gunbrokerEnabled()) return;

		frappe.db
			.get_value('Item', frm.doc.item_code, ['is_firearm', 'uniform_sell_price'])
			.then(function (r) {
				const item = (r && r.message) || {};
				// cint(), not Boolean(): frappe.db.get_value hands Check fields back
				// as the strings "0"/"1", and Boolean("0") is true — which would put
				// listing buttons on exactly the guns that must not have them.
				const isPerSerial = cint(item.is_firearm) && !cint(item.uniform_sell_price);
				if (!isPerSerial) return;

				_gbAddButtons(frm);
			});
	},
});

// Everything about the listing is decided by the person pressing Send, with
// the figures it would otherwise carry offered as the defaults: the title,
// fixed price or auction, the auction's terms, and the buyer-paid shipping.
// Leaving the title or the fixed price as offered saves nothing, so the gun
// keeps following its item name and the POS price when those change; the
// auction figures and the type are recorded every time.
function _gbAskListing(frm, then) {
	frappe.call({
		method: 'ffl_integrations.gunbroker.client_api.listing_offer',
		args: { serial_no: frm.doc.name },
		callback(resp) {
			const o = resp.message || {};
			const ship = o.shipping || {};
			const own = flt(ship.own), dflt = flt(ship.default), free = cint(ship.free);
			const cat = o.category || {};
			const premium = o.premium || {};
			const COND = { 1: __('Factory New'), 2: __('New Old Stock'), 3: __('Used') };
			const condCodes = (o.condition_options || []).length ? o.condition_options : [1, 2, 3];
			const condOptions = condCodes.map((c) => `${c} ${COND[c] || ''}`.trim());
			const derivedCond = cint(o.derived_condition);
			const condStart = condCodes.indexOf(cint(o.condition)) !== -1 ? cint(o.condition) : derivedCond;
			const condDefault = condOptions.find((x) => cint(x) === condStart) || condOptions[0];
			const catOptions = (cat.options || []).map((x) => `${x.id} ${x.name}`.trim());
			const catDefault = catOptions.find((x) => cint(x) === cint(cat.id)) || catOptions[0];
			const auction = 'eval:doc.listing_type === "Auction"';
			const fixed = 'eval:doc.listing_type !== "Auction"';
			const relistOptions = (o.auto_relist_options || [])
				.map((x) => `${x.value} ${x.label}`);
			const relistDefault = relistOptions.find((x) => cint(x) === cint(o.auto_relist)) || relistOptions[0];
			frappe.prompt([
				{ fieldtype: 'Data', fieldname: 'title', label: __('Title'), reqd: 1,
					default: o.title,
					description: __('Up to 75 characters. Leave it as offered to follow the item name.') },
				{ fieldtype: 'Select', fieldname: 'listing_type', label: __('Listing type'), reqd: 1,
					options: (o.types || []).join('\n'), default: o.type },
				// No category resolves → no field; the push refuses with the reason.
				{ fieldtype: 'Select', fieldname: 'category', label: __('Category'),
					options: catOptions.join('\n'), default: catDefault,
					hidden: catOptions.length ? 0 : 1, read_only: catOptions.length < 2 ? 1 : 0,
					description: __('Saved on the model: every gun of this item lists here.') },
				// Sent as 0 when it equals what the item gives, so the gun keeps
				// following the model; only a different choice is recorded.
				{ fieldtype: 'Select', fieldname: 'condition', label: __('GunBroker condition'),
					options: condOptions.join('\n'), default: condDefault,
					read_only: condOptions.length < 2 ? 1 : 0,
					description: condOptions.length < 2
						? __('An antique is always listed as Used.')
						: __('From the item: {0}. New Old Stock is chosen per gun.', [COND[derivedCond] || derivedCond]) },
				{ fieldtype: 'Column Break', fieldname: 'gb_col_1' },
				{ fieldtype: 'Currency', fieldname: 'price', label: __('Fixed price'),
					default: o.price, depends_on: fixed, mandatory_depends_on: fixed,
					description: __('POS sell price: {0}. A different figure is saved on this gun.',
						[format_currency(o.pos_price)]) },
				{ fieldtype: 'Currency', fieldname: 'starting_bid', label: __('Starting bid'),
					default: o.starting_bid, depends_on: auction, mandatory_depends_on: auction },
				{ fieldtype: 'Currency', fieldname: 'reserve_price', label: __('Reserve (optional)'),
					default: o.reserve_price || null, depends_on: auction,
					description: __('GunBroker charges for a reserve, and refuses one on a 1- or 3-day auction.') },
				{ fieldtype: 'Currency', fieldname: 'buy_now_price', label: __('Buy Now (optional)'),
					default: o.buy_now_price || null, depends_on: auction,
					description: __('At least the starting bid and the reserve.') },
				{ fieldtype: 'Section Break', fieldname: 'gb_sec_1' },
				{ fieldtype: 'Select', fieldname: 'duration', label: __('Auction length (days)'),
					options: (o.durations || []).join('\n'), default: String(o.duration), depends_on: auction },
				{ fieldtype: 'Select', fieldname: 'auto_relist', label: __('If it does not sell'),
					options: relistOptions.join('\n'), default: relistDefault, depends_on: auction },
				{ fieldtype: 'Column Break', fieldname: 'gb_col_2' },
				// A checkbox, not a typed 0: a dialog's refresh_input turns a number
				// field's 0 into empty (`doc[field] || ""`), so 0 never reaches here.
				{ fieldtype: 'Check', fieldname: 'free_shipping', label: __('Free shipping (seller pays)'),
					default: free },
				{ fieldtype: 'Currency', fieldname: 'ship_cost', label: __('Shipping cost (buyer pays)'),
					default: own > 0 ? own : dflt,
					depends_on: 'eval:!doc.free_shipping', mandatory_depends_on: 'eval:!doc.free_shipping',
					description: own > 0
						? __('Saved on this gun. The store default is {0}.', [format_currency(dflt)])
						: __('The store default. Change it to set a rate for this gun only.') },
				// GunBroker's paid promotions. They are bought by the listing request
				// itself and can never be added to a live listing, so this is the one
				// place to choose them. Pre-ticked from what the gun last carried: a
				// POS relist sends them again, and GunBroker charges again.
				{ fieldtype: 'Section Break', fieldname: 'gb_sec_premium', label: __('Paid promotion') },
				{ fieldtype: 'HTML', fieldname: 'premium_note',
					options: `<div class="text-muted small" style="margin-bottom:8px">${
						__('GunBroker charges extra for each one ticked, every time the gun is listed; they cannot be added to a live listing.')
						}${premium.featured || premium.showcase || premium.sponsored_onsite || premium.view_counter
							? ` <b>${__('Ticked because this gun had them last time.')}</b>` : ''}</div>` },
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
				const type = v.listing_type;
				const listing = { type: type, title: v.title === o.derived_title ? '' : v.title };
				const chosenCond = cint(v.condition);
				listing.condition = chosenCond === derivedCond ? 0 : chosenCond;
				// All four every time, so unticking a recorded one clears it.
				listing.premium = {
					featured: cint(v.premium_featured), showcase: cint(v.premium_showcase),
					sponsored_onsite: cint(v.premium_sponsored_onsite),
					view_counter: cint(v.premium_view_counter),
				};
				if (type === 'Auction') {
					listing.starting_bid = flt(v.starting_bid);
					listing.reserve_price = flt(v.reserve_price);
					listing.buy_now_price = flt(v.buy_now_price);
					listing.duration = cint(v.duration);
					listing.auto_relist = cint(v.auto_relist);
				} else {
					listing.price = flt(v.price) === flt(o.pos_price) ? 0 : flt(v.price);
				}
				const freeNow = cint(v.free_shipping);
				const chosen = flt(v.ship_cost);
				const category = cint(v.category);
				then(freeNow || (own <= 0 && chosen === dflt) ? null : chosen, listing,
					category && category !== cint(cat.id) ? category : null,
					freeNow !== free ? freeNow : null);
			}, __('Send to GunBroker'), __('Send'));
		},
	});
}

function _gbAddButtons(frm) {
	const GROUP = __('GunBroker');

	// An auction is the one listing whose state changes without POS doing
	// anything: bids arrive, and with the first one the gun is spoken for.
	if (frm.doc.gb_item_id && frm.doc.gb_listing_type === 'Auction') {
		const bids = cint(frm.doc.gb_bid_count);
		const ends = frm.doc.gb_auction_ends_at
			? __(' · ends {0}', [frappe.datetime.str_to_user(frm.doc.gb_auction_ends_at)]) : '';
		frm.dashboard.add_indicator(
			__('GunBroker auction · {0} bid(s){1}', [bids, ends]),
			cint(frm.doc.gb_bid_locked) ? 'red' : 'blue');
	}

	if (frm.doc.status === 'Active' && !frm.doc.gb_item_id && !frm.doc.gb_sold_unknown) {
		const relist = cint(frm.doc.gb_delisted) ? 1 : 0;
		const $send = frm.add_custom_button(__('Send to GunBroker'), function () {
			_gbAskListing(frm, function (ship_cost, listing, category_id, free_shipping) {
				const args = { serial_no: frm.doc.name, relist: relist, listing: JSON.stringify(listing) };
				if (ship_cost !== null) args.ship_cost = ship_cost;
				if (category_id !== null) args.category_id = category_id;
				if (free_shipping !== null) args.free_shipping = free_shipping;
				_gbCall(frm, {
					method: 'ffl_integrations.gunbroker.client_api.push_serial_now',
					args: args,
					freeze_message: __('Listing on GunBroker…'),
					onOk(m) {
						// "claimed_existing" means the gun was ALREADY listed and this
						// press adopted the listing rather than creating one. Saying so
						// matters: otherwise an operator reads a fresh item number and
						// assumes their click made it.
						const adopted = (m.warnings || []).indexOf('claimed_existing') !== -1;
						frappe.show_alert({
							message: adopted
								? __('Already listed on GunBroker — linked to item {0}.', [m.gb_item_id])
								: __('Listed on GunBroker. Item {0}.', [m.gb_item_id]),
							indicator: 'green',
						}, 7);
						_gbShowWarnings(m.warnings, m.gb_url);
					},
				});
			});
		}, GROUP);
		$send.removeClass('btn-default').addClass('btn-success');
	}

	if (frm.doc.gb_item_id) {
		frm.add_custom_button(__('Reprice on GunBroker'), function () {
			_gbCall(frm, {
				method: 'ffl_integrations.gunbroker.client_api.reprice_serial_now',
				args: { serial_no: frm.doc.name },
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

		frm.add_custom_button(__('GunBroker Status'), function () {
			frappe.call({
				method: 'ffl_integrations.gunbroker.client_api.listing_status',
				args: { serial_no: frm.doc.name },
				freeze: true,
				freeze_message: __('Checking GunBroker…'),
				callback(resp) { _gbStatusDialog(resp.message || {}); },
			});
		}, GROUP);

		const $end = frm.add_custom_button(__('End GunBroker Listing'), function () {
			frappe.confirm(
				__('End this gun\'s GunBroker listing? Re-listing it later creates a new listing with a new item number.'),
				function () {
					_gbCall(frm, {
						method: 'ffl_integrations.gunbroker.client_api.end_listing_now',
						args: { serial_no: frm.doc.name },
						freeze_message: __('Ending the GunBroker listing…'),
						onOk(m) { _gbEndResult(m); },
					});
				},
			);
		}, GROUP);
		$end.removeClass('btn-default').addClass('btn-danger');
	}

	if (cint(frm.doc.gb_sold_unknown)) {
		const $clear = frm.add_custom_button(__('Clear "Sold Unknown"'), function () {
			frappe.prompt(
				[{
					fieldtype: 'HTML', fieldname: 'warn',
					options: `<div class="alert alert-warning">${__(
						'Pressing this says you have looked at GunBroker and confirmed this gun was NOT sold there. It is what stops the same gun being sold twice, so your note is recorded against the serial.')}</div>`,
				}, {
					fieldtype: 'Small Text', fieldname: 'note', reqd: 1,
					label: __('What did you check on GunBroker?'),
				}],
				function (values) {
					_gbCall(frm, {
						method: 'ffl_integrations.gunbroker.client_api.clear_sold_unknown_now',
						args: { serial_no: frm.doc.name, note: values.note },
						freeze_message: __('Clearing…'),
						onOk() {
							frappe.show_alert({
								message: __('Cleared. This gun can be sold again.'),
								indicator: 'green',
							});
						},
					});
				},
				__('Confirm with GunBroker'), __('Clear the flag'),
			);
		}, GROUP);
		$clear.removeClass('btn-default').addClass('btn-danger');
	}
}

// One call shape for every button: a guard refusal comes back as ok:false with
// a message, NOT as an error, so without this branch the button would appear to
// do nothing at all — the failure mode that let mispriced guns sit unnoticed on
// the web store.
function _gbCall(frm, opts) {
	frappe.call({
		method: opts.method,
		args: opts.args,
		freeze: true,
		freeze_message: opts.freeze_message,
		callback(resp) {
			const m = resp.message || {};
			if (m.ok) {
				opts.onOk(m);
			} else if (m.skipped) {
				frappe.msgprint({
					title: __('Not sent to GunBroker'),
					message: m.message,
					indicator: 'orange',
				});
			}
			frm.reload_doc();
		},
	});
}

function _gbShowWarnings(warnings, url) {
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
		message: '<ul>' + notable.map(
			(w) => `<li>${LABELS[w] || frappe.utils.escape_html(w)}</li>`).join('') + '</ul>',
		indicator: 'orange',
	});
}

// Ending a listing has exactly two outcomes worth distinguishing HERE: it is
// finished, or it is not and somebody has to go and finish it on GunBroker.
//
// Every not-finished case gets the identical dialog — the setting says do it by
// hand, the call failed, the read-back still says active, or the authorisation
// was withdrawn. They differ in the logs and in whether the system retries, and
// they do not differ at all in what the person in front of the screen must now
// do. Splitting them into different-looking messages would only invite reading
// one of them as less urgent than it is.
function _gbEndResult(m) {
	if (m.confirmed) {
		frappe.show_alert({
			message: __('Ended on GunBroker (confirmed — the listing reads as inactive).'),
			indicator: 'green',
		}, 7);
		return;
	}

	const url = m.gb_url || 'https://www.gunbroker.com/My/Selling';
	// Only the automatic strategy retries on its own. Under Manual Only nothing
	// will happen until a person acts, and saying "the system will retry" there
	// would be a straightforward lie.
	const retryLine = m.strategy === 'delete_endpoint'
		? `<br>${__('The system will keep retrying on each hourly check, but do not wait for it.')}`
		: '';
	const downgradeLine = m.downgraded
		? `<br><br>${__('GunBroker has withdrawn permission for automatic ending, so End Listing Strategy has been switched to Manual Only. Every listing now has to be ended by hand until that is restored.')}`
		: '';

	frappe.msgprint({
		title: __('End it on GunBroker by hand'),
		message: `${__('This listing was not ended: {0}.', [m.detail || __('no reason given')])}
			<br><br><b>${__('Until you end it on GunBroker, it can still be bought')}</b> —
			${__('and this gun is no longer available to sell.')}${retryLine}${downgradeLine}
			<br><br>${__('Open')} <a href="${url}" target="_blank" rel="noopener">${__('item {0} on GunBroker', [m.gb_item_id || ''])}</a>
			${__('and use End Item Early.')}`,
		indicator: 'orange',
	});
}

function _gbStatusDialog(m) {
	if (!m.ok) { frappe.msgprint(__('Could not read the GunBroker status.')); return; }
	const remote = m.remote || {};
	const rows = [
		[__('State in this POS'), m.state],
		[__('GunBroker item'), m.gb_item_id || '—'],
		[__('GunBroker answered'), remote.state || '—'],
		[__('Active on GunBroker'), remote.is_active === undefined ? '—' : String(remote.is_active)],
		[__('Has a winning bid'), remote.has_winning_bid === undefined ? '—' : String(remote.has_winning_bid)],
		[__('Price on GunBroker'), remote.fixed_price === undefined || remote.fixed_price === null
			? '—' : format_currency(remote.fixed_price)],
		[__('Last error'), m.gb_sync_error || '—'],
	];
	frappe.msgprint({
		title: __('GunBroker status'),
		message: '<table class="table table-bordered"><tbody>' + rows.map(
			([k, v]) => `<tr><td style="width:45%">${k}</td><td>${frappe.utils.escape_html(String(v))}</td></tr>`,
		).join('') + '</tbody></table>',
	});
}
