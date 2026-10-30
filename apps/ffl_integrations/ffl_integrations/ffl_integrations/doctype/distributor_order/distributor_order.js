// Distributor Order form — staff actions.
//
// The A&D receive leg needs a desk entry point: a "Receive" button that opens the
// staff-gated pre-fill (build_receive_payload) for review, then books it through
// receive_from_do (DIVERGENCE-3 — a human reviews and submits; never cron
// auto-submit). Confirm/Cancel drive the state machine via the whitelisted,
// role-gated distributor.api endpoints (the pure machine still enforces legality).

frappe.ui.form.on('Distributor Order', {
	refresh(frm) {
		if (frm.is_new()) return;
		const status = frm.doc.status;

		// Per-house fields (the Account shown is the house's own number, stamped at
		// creation): FillOrKill is an RSR order flag; signature / adult
		// signature / insurance are Sports South's (the only provider whose
		// delivery_options() is not None). On any other house they only ever show
		// empty, so hide them.
		frappe.db.get_value('Distributor', frm.doc.distributor, 'provider').then((r) => {
			const provider = (r.message || {}).provider;
			frm.toggle_display('fill_or_kill', provider === 'rsr');
			frm.toggle_display(['delivery_options_set', 'delivery_signature',
				'delivery_adult_signature', 'delivery_insurance'], provider === 'ss');
		});

		if (status === 'Draft') {
			frm.add_custom_button(__('Confirm & Queue'), () => _confirm(frm)).addClass('btn-primary');
			// Carrier service (Sports South: Economy / Premium / 2-Day / Next-Day per
			// lane). Shown only when the provider offers a choice for this order.
			frappe.call({
				method: 'ffl_integrations.distributor.api.shipping_options',
				args: { do_name: frm.doc.name },
				callback(r) {
					const opts = r.message || [];
					if (!opts.length) return;
					frm.add_custom_button(__('Shipping service…'), () => _choose_shipping(frm, opts));
				},
			});
		}

		// Only stocking / procure-to-store goods come to the store; a drop-ship leg
		// went straight to the buyer (the server refuses it too).
		const stocked = ['Stocking', 'Procure To Store'].includes(frm.doc.order_type);
		const receivable = stocked && (frm.doc.lines || []).some(
			(l) => (l.invoiced_qty || 0) > (l.received_qty || 0));
		if (receivable && ['Placed', 'Acknowledged', 'Hold', 'Partially Shipped', 'Complete'].includes(status)) {
			frm.add_custom_button(__('Receive'), () => _open_receive(frm)).addClass('btn-primary');
		}

		// A parcel can beat the 30-minute poll: pull the house's status on demand so
		// the invoice / tracking / serials land before the receiver opens the box.
		if (['Placed', 'Acknowledged', 'Hold', 'Partially Shipped'].includes(status)) {
			frm.add_custom_button(__('Check status now'), () => frappe.call({
				method: 'ffl_integrations.distributor.tasks.poll_order_now',
				args: { do_name: frm.doc.name }, freeze: true,
				freeze_message: __('Asking {0}…', [frm.doc.distributor]),
				callback: () => frm.reload_doc(),
			}));
		}

		// FDS Hold remedy: attach/replace the ShipFFL to release a hold. Only
		// meaningful for a firearm order the distributor is holding. The server
		// re-validates the new FFL through the canonical expiry/address chain before
		// calling RSR (a hold must not release onto an expired licence). RSR keys
		// the update by WebRef, so without one (queued placement not yet polled)
		// the action can only dead-end — hide it until the poll backfills web_ref.
		if (frm.doc.order_type === 'Dropship Firearm'
			&& (frm.doc.web_ref || '').trim()
			&& ['Placed', 'Acknowledged', 'Hold'].includes(status)) {
			frm.add_custom_button(__('Update Transfer FFL'), () => _update_ffl(frm), __('Actions'));
		}

		if (!['Complete', 'Cancelled'].includes(status)) {
			frm.add_custom_button(__('Cancel Order'), () => _cancel(frm), __('Actions'));
		}
	},
});

function _choose_shipping(frm, opts) {
	const current = opts.find((o) => o.selected) || opts.find((o) => o.default) || opts[0];
	frappe.prompt(
		[{
			fieldtype: 'Select', fieldname: 'service', label: __('Shipping service'), reqd: 1,
			options: opts.map((o) => o.label).join('\n'),
			default: current.label,
			description: __('Estimate before signature / insurance fees; the invoice amount replaces it.'),
		}],
		(v) => {
			const pick = opts.find((o) => o.label === v.service);
			frappe.call({
				method: 'ffl_integrations.distributor.api.set_shipping_service',
				args: { do_name: frm.doc.name, code: pick ? pick.code : '' },
				freeze: true,
				callback() { frm.reload_doc(); },
			});
		},
		__('Shipping service'), __('Apply'));
}

function _update_ffl(frm) {
	frappe.prompt(
		[{
			fieldtype: 'Data', fieldname: 'ffl_license', reqd: 1,
			label: __('New transfer FFL license'),
			default: frm.doc.transfer_ffl_license || '',
			description: __('Re-validated (unexpired + complete address) before the distributor is called.'),
		}],
		(v) => {
			frappe.call({
				method: 'ffl_integrations.distributor.router.update_order_ffl',
				args: { do_name: frm.doc.name, ffl_license: v.ffl_license },
				freeze: true,
				freeze_message: __('Validating and updating the transfer FFL…'),
				callback: () => {
					frappe.show_alert({ message: __('Transfer FFL updated.'), indicator: 'green' });
					frm.reload_doc();
				},
			});
		},
		__('Update Transfer FFL'), __('Update'));
}

function _confirm(frm) {
	frappe.confirm(__('Confirm and queue {0} for placement?', [frm.doc.name]), () => {
		frappe.call({
			method: 'ffl_integrations.distributor.api.confirm_order',
			args: { do_name: frm.doc.name },
			freeze: true,
			callback: () => frm.reload_doc(),
		});
	});
}

// One cancel. Cancelling here never reaches the house (RSR has no cancel API);
// while the order is live at the distributor the server alerts ops to phone it
// in — unless the operator says the house has already confirmed the
// cancellation, in which case that alert would only be noise.
function _cancel(frm) {
	const live = ['Placed', 'Acknowledged', 'Hold', 'Partially Shipped', 'Error'].includes(frm.doc.status);
	const fields = [{ fieldtype: 'Small Text', fieldname: 'reason', label: __('Reason'), reqd: 0 }];
	if (live) {
		fields.push({
			fieldtype: 'Check', fieldname: 'confirmed',
			label: __('{0} has confirmed the cancellation (phone / email)', [frm.doc.distributor]),
			description: __('This order is live at {0} (ref {1}). Left unticked, ops are alerted to arrange the cancellation with the house.',
				[frm.doc.distributor, frm.doc.web_ref || '—']),
		});
	}
	frappe.prompt(fields, (v) => {
		frappe.call({
			method: 'ffl_integrations.distributor.api.cancel_order',
			args: { do_name: frm.doc.name, reason: v.reason, confirmed: v.confirmed ? 1 : 0 },
			freeze: true,
			callback: () => frm.reload_doc(),
		});
	}, __('Cancel Distributor Order'), __('Cancel Order'));
}

// Receive. An order with a FIREARM on it is received on the Receive Goods page,
// one gun at a time, where the clerk confirms manufacturer / model / type /
// caliber / importer and the serial against the gun in hand (the bound book
// records the firearm, not the catalog row) — and where a house that echoed no
// serials is no obstacle. Accessory-only orders keep the one-click receive.
function _open_receive(frm) {
	frappe.call({
		method: 'ffl_integrations.distributor.receive_bridge.list_incoming',
		callback: (r) => {
			const orders = ((r.message || {}).groups || []).flatMap((g) => g.orders || []);
			const mine = orders.find((o) => o.do_name === frm.doc.name);
			if (mine && (mine.lines || []).some((l) => l.is_firearm)) {
				frappe.route_options = { distributor_order: frm.doc.name };
				frappe.set_route('receive-goods');
				return;
			}
			_one_click_receive(frm);
		},
		error: () => _one_click_receive(frm),
	});
}

function _one_click_receive(frm) {
	frappe.call({
		method: 'ffl_integrations.distributor.receive_bridge.build_receive_payload',
		args: { do_name: frm.doc.name },
		freeze: true,
		freeze_message: __('Preparing receive…'),
		callback: (r) => {
			const payload = r.message;
			if (!payload || !(payload.items || []).length) {
				frappe.msgprint(__('Nothing new to receive.'));
				return;
			}
			const rows = payload.items.map((it) => `<tr>
				<td>${frappe.utils.escape_html(it.item_code || '')}</td>
				<td class="text-right">${it.qty}</td>
				<td>${(it.serials || []).map(frappe.utils.escape_html).join(', ')}</td>
			</tr>`).join('');
			const d = new frappe.ui.Dialog({
				title: __('Receive from {0}', [frm.doc.name]),
				fields: [{ fieldtype: 'HTML', fieldname: 'preview' }],
				primary_action_label: __('Submit Receive Goods'),
				primary_action: () => {
					d.hide();
					frappe.call({
						method: 'ffl_integrations.distributor.receive_bridge.receive_from_do',
						args: { do_name: frm.doc.name },
						freeze: true,
						freeze_message: __('Booking stock + A&D acquisition…'),
						callback: (res) => {
							frappe.show_alert({
								message: __('Received — {0}', [(res.message || {}).name || '']),
								indicator: 'green',
							});
							frm.reload_doc();
						},
					});
				},
			});
			d.fields_dict.preview.$wrapper.html(`
				<p class="text-muted">${__('Review the pre-filled receive. Submitting books stock and the A&D acquisition — a staff action.')}</p>
				<table class="table table-bordered"><thead><tr>
					<th>${__('Item')}</th><th class="text-right">${__('Qty')}</th><th>${__('Serials')}</th>
				</tr></thead><tbody>${rows}</tbody></table>`);
			d.show();
		},
	});
}
