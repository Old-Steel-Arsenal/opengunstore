// POS Invoice desk form — manager "Void Card Charge" button.
//
// Reverses an UNSETTLED card capture on the processor that took it (Payroc
// has no discrete /void; its adapter PATCHes …/reverse). The primary case is a held order: under the
// charge-before-4473 flow a firearm sale takes payment then waits as a DRAFT
// for the 4473/NICS — if the buyer is denied, the manager voids the card here
// and deletes the draft. Settled/submitted sales must refund via POS Return
// instead (voiding a submitted sale would leave the books claiming payment),
// so the button is draft-only. Manager-gated, mirroring the server
// _check_refund_perm.

frappe.ui.form.on('POS Invoice', {
	refresh(frm) {
		const isManager = frappe.user.has_role('Sales Manager')
			|| frappe.user.has_role('System Manager');

		// Void Card Charge — reverse an unsettled card capture on a held draft.
		// Multi-card era: any live CHARGED row is voidable regardless of the
		// aggregate status (an in-flight sibling instruction must not hide the
		// button); legacy single-stamp invoices keep the old rule.
		const status = frm.doc.card_status;
		const pcMath = window.ffl && window.ffl.cardCharges;
		const liveRows = pcMath ? pcMath.liveChargeRows(frm.doc) : [];
		const canVoid = frm.doc.docstatus === 0            // unsettled draft only
			&& (liveRows.length
				|| (frm.doc.card_unique_reference
					&& (status === 'CHARGED' || status === 'PARTIAL')))
			&& isManager;
		if (canVoid) {
			frm.add_custom_button(__('Void Card Charge'), () => voidCardCharge(frm))
				.removeClass('btn-default').addClass('btn-danger');
		}

		// Manager Override 4473 — force-complete a firearm sale whose 4473 was
		// completed in FastBound but never synced back (a serial edited in the
		// shared account, a deleted disposition, FastBound down). Pending is the
		// usual state; N/A happens when the clerk ran the 4473 straight in
		// FastBound without the POS button, so
		// any not-yet-Completed draft with a firearm on it qualifies. Manager-
		// only; books an audited manual-override Retail Sale disposition so
		// Complete Order unlocks.
		const canOverride = frm.doc.docstatus === 0
			&& !frm.doc.is_return
			&& frm.doc.form_4473_status !== 'Completed'
			&& isManager;
		if (canOverride) {
			const addOverride = () => frm.add_custom_button(__('Manager Override 4473'),
				() => override4473(frm)).removeClass('btn-default').addClass('btn-danger');
			if (frm.doc.form_4473_status === 'Pending') {
				addOverride();
			} else {
				// N/A: only offer it when the order actually carries a firearm.
				const codes = (frm.doc.items || []).map((r) => r.item_code).filter(Boolean);
				if (codes.length) {
					frappe.call({
						method: 'ffl_core.firearm.firearm_items_in',
						args: { item_codes: codes },
					}).then((r) => {
						if (r && r.message && r.message.length && frm.doc.name) addOverride();
					});
				}
			}
		}

		// View Signed 4473 — the stored FastBound link needs the API key (a
		// browser click on it is a 401), so open this 4473's Form 4473 Log row
		// through the viewer (private copy, else FastBound; 4473 roles; Access Log).
		// Only the 4473 roles may read the log (the same set as COUNTER_4473_ROLES);
		// anyone else would get a permission popup on every such invoice.
		if (frm.doc.fastbound_4473_pdf_url && !frm.is_new()
				&& frappe.user.has_role(['Sales User', 'Sales Manager', 'System Manager'])) {
			frappe.db.get_value('Form 4473 Log', { pdf_url: frm.doc.fastbound_4473_pdf_url }, 'name')
				.then(({ message }) => {
					if (message && message.name) {
						frm.add_custom_button(__('View Signed 4473'), () => ffl.open4473Pdf(message.name));
					}
				});
		}

		// Unlock Card Refund — clear a return's UNKNOWN-OUTCOME re-refund lock
		// after the manager verified in the processor's portal that the refund did
		// NOT land. PARTIAL:* locks never unlock (the sentinel
		// is the one record of what this return already pushed back — its
		// residue goes to back-office manual processing), and real refund
		// references are completed refunds, not locks. Locked returns are
		// normally DRAFTS (the refund runs before submit), so the button shows
		// on drafts and submitted docs alike. Server re-checks everything.
		const unlockable = pcMath
			&& pcMath.isUnlockableLock(frm.doc.card_refund_reference);
		if (frm.doc.is_return && frm.doc.docstatus !== 2 && unlockable && isManager) {
			frm.add_custom_button(__('Unlock Card Refund'), () => unlockCardRefund(frm))
				.removeClass('btn-default').addClass('btn-danger');
		}

		// Confirm Portal Refund — the manager exit for the switched-off refund
		// guard: a return of a GATEWAY-charged sale cannot submit while the card
		// processor that charged it is off until the portal refund is recorded here (its own field + timeline,
		// never the tender row's reference_no — that field is force-collected by
		// the standalone-terminal rule and cannot double as this guard's
		// evidence). Shown when the original carries gateway money facts and its
		// processor is not the one switched on now (none on, or the store has moved
		// to another processor); the server re-checks everything.
		const active = frappe.boot && frappe.boot.ffl_card_processor;
		if (window.ffl && window.ffl.switches && pcMath && pcMath.chargingProcessorOff
				&& active && frm.doc.is_return && frm.doc.docstatus !== 2
				&& frm.doc.return_against && !frm.doc.card_oob_refund_reference
				&& isManager) {
			// Which processor took the original's card money (its first live row;
			// none = a stamp-only sale from before rows, which was Payroc). Only
			// asked while a processor is on: with none on, any one is off.
			const chargedBy = Number(active.enabled)
				? frappe.db.get_value('Card Charge', { parent: frm.doc.return_against,
					parenttype: 'POS Invoice', status: 'CHARGED' }, 'processor', null, 'POS Invoice')
					.then((r) => (r.message || {}).processor || '')
				: Promise.resolve('');
			chargedBy.then((code) => {
				if (!pcMath.chargingProcessorOff(code, active)) return;
				return frappe.db.get_value('POS Invoice', frm.doc.return_against,
					['card_unique_reference', 'card_status']).then((r) => {
					const o = (r.message || {});
					if (!o.card_unique_reference && !o.card_status) return;
					frm.add_custom_button(__('Confirm Portal Refund'),
						() => confirmPortalRefund(frm))
						.removeClass('btn-default').addClass('btn-danger');
				});
			});
		}
	},
});

// The active card processor's name for messages ("Payroc" on a Payroc store).
function cardLabel() {
	return window.ffl.switches.cardProcessorLabel();
}

function unlockCardRefund(frm) {
	const d = new frappe.ui.Dialog({
		title: __('Unlock Card Refund'),
		fields: [
			{
				fieldtype: 'HTML', fieldname: 'warn',
				options: `<div class="alert alert-danger" style="margin-bottom:10px">${
					__('This return\'s automatic refund is locked (<b>{0}</b>): a refund attempt ended with an UNKNOWN outcome — it may or may not have landed. <b>First verify in the {1} portal that NO refund landed for this return.</b> Unlocking re-arms a FULL-amount retry; if the original attempt actually landed, that retry refunds the same money twice. If the portal shows any refund landed, do NOT unlock — send it to the back office instead.',
						[frappe.utils.escape_html(frm.doc.card_refund_reference || ''), cardLabel()])
				}</div>`,
			},
			{
				fieldtype: 'Small Text', fieldname: 'reason', reqd: 1,
				label: __('Reason (include what you verified in {0})', [cardLabel()]),
				description: __('e.g. {0} portal shows no refund landed for the timed-out slice; credentials fixed, safe to retry.', [cardLabel()]),
			},
		],
		primary_action_label: __('I verified in {0} — unlock', [cardLabel()]),
		primary_action(values) {
			d.hide();
			frappe.dom.freeze(__('Unlocking…'));
			frappe.call({
				method: 'ffl_integrations.payments.api.unlock_refund_lock',
				args: { return_invoice_name: frm.doc.name, reason: values.reason },
				callback(r) {
					frappe.dom.unfreeze();
					if (r.message && r.message.ok) {
						frappe.show_alert({
							message: __('Refund lock cleared ({0}).', [r.message.cleared]),
							indicator: 'green',
						});
						frm.reload_doc();
					}
				},
				error() { frappe.dom.unfreeze(); },
			});
		},
	});
	d.show();
	d.get_primary_btn().removeClass('btn-primary').addClass('btn-danger');
}

function override4473(frm) {
	const d = new frappe.ui.Dialog({
		title: __('Manager Override — Force-Complete 4473'),
		fields: [
			{
				fieldtype: 'HTML', fieldname: 'warn',
				options: `<div class="alert alert-warning" style="margin-bottom:10px">${
					__('Use only when the 4473 + NICS were genuinely completed in <b>FastBound</b> but did not sync back here. This force-completes the sale and books an <b>audited manual-override</b> bound-book record (your name + reason). It does <b>not</b> update FastBound — reconcile the FastBound bound book separately.')
				}</div>`,
			},
			{
				fieldtype: 'Small Text', fieldname: 'reason', reqd: 1,
				label: __('Reason'),
			},
		],
		primary_action_label: __('Force-complete the sale'),
		primary_action(values) {
			d.hide();
			frappe.dom.freeze(__('Booking manual-override disposition…'));
			frappe.call({
				method: 'ffl_integrations.fastbound.pos_4473.manager_override_4473',
				args: { invoice_name: frm.doc.name, reason: values.reason },
				callback(r) {
					frappe.dom.unfreeze();
					if (r.message && r.message.ok) {
						frappe.show_alert({
							message: __('4473 overridden for {0} firearm(s). Complete Order is now unlocked.',
								[r.message.n_firearms]),
							indicator: 'green',
						});
						frm.reload_doc();
					}
				},
				error() { frappe.dom.unfreeze(); },
			});
		},
	});
	d.show();
	d.get_primary_btn().removeClass('btn-primary').addClass('btn-danger');
}

function voidCardCharge(frm) {
	// Multi-card sale → one charge row is reversed at a time; the manager
	// picks which. Single charge (row or legacy stamp) → no picker. Picker
	// labels are index-mapped back to unique_reference by the pure module —
	// values never round-trip through display text.
	const pcMath = window.ffl && window.ffl.cardCharges;
	const live = pcMath ? pcMath.liveChargeRows(frm.doc) : [];
	const picker = pcMath ? pcMath.buildVoidPicker(frm.doc.card_charges) : null;
	const multi = live.length > 1;
	const amount = live.length === 1 ? flt(live[0].amount)
		: (flt(frm.doc.card_charged_amount) || flt(frm.doc.grand_total));
	const fields = [
		{
			fieldtype: 'HTML', fieldname: 'warn',
			options: `<div class="alert alert-warning" style="margin-bottom:10px">${
				multi
					? __('This sale was split across {0} cards. Voiding reverses <b>one charge at a time</b> — pick which one below; the money goes back to that card. Only works on an <b>unsettled</b> charge (same business day). After voiding, delete this draft or re-tender.',
						[live.length])
					: __('This reverses the card payment of <b>{0}</b> on the card terminal — the money goes back to the customer\'s card. It only works on an <b>unsettled</b> charge (same business day, before the batch closes). After voiding, delete this draft or re-tender.',
						[format_currency(amount)])
			}</div>`,
		},
	];
	if (multi) {
		fields.push({
			fieldtype: 'Select', fieldname: 'charge', reqd: 1,
			label: __('Charge to void'),
			options: picker.labels.join('\n'),
		});
	}
	fields.push({
		fieldtype: 'Small Text', fieldname: 'reason', reqd: 1,
		label: __('Reason'),
		description: __('e.g. NICS denied, customer canceled'),
	});
	const d = new frappe.ui.Dialog({
		title: __('Void Card Charge'),
		fields: fields,
		primary_action_label: __('Void the charge'),
		primary_action(values) {
			d.hide();
			frappe.dom.freeze(__('Reversing on the terminal…'));
			frappe.call({
				method: 'ffl_integrations.payments.api.void_pos_invoice',
				args: {
					invoice_name: frm.doc.name, reason: values.reason,
					unique_reference: multi ? picker.resolve(values.charge) : undefined,
				},
				callback(r) {
					frappe.dom.unfreeze();
					if (r.message && r.message.ok) {
						frappe.show_alert({
							message: __('Card charge voided ({0})', [r.message.unique_reference || 'OK']),
							indicator: 'green',
						});
						frm.reload_doc();
					}
				},
				// On a throw (gateway declined / already settled / no payment) the
				// server message surfaces automatically; just lift the freeze.
				error() { frappe.dom.unfreeze(); },
			});
		},
	});
	d.show();
	// Make the confirm action read as destructive.
	d.get_primary_btn().removeClass('btn-primary').addClass('btn-danger');
}


function confirmPortalRefund(frm) {
	const d = new frappe.ui.Dialog({
		title: __('Confirm Portal Refund'),
		fields: [
			{
				fieldtype: 'HTML', fieldname: 'warn',
				options: `<div class="alert alert-danger" style="margin-bottom:10px">${
					__('The original sale was charged on the integrated terminal, and its card processor is switched off — POS cannot send this refund. <b>First issue the refund in that processor\'s portal.</b> Recording it here lets the return submit; it does not move any money.')
				}</div>`,
			},
			{
				fieldtype: 'Data', fieldname: 'portal_reference', reqd: 1,
				label: __('Portal refund reference'),
				description: __('From the processor\'s portal — the refund\'s own reference, never the card number.'),
			},
			{
				fieldtype: 'Small Text', fieldname: 'reason', reqd: 1,
				label: __('Reason (include what you verified in the portal)'),
			},
		],
		primary_action_label: __('I refunded it in the portal — record'),
		primary_action(values) {
			d.hide();
			frappe.dom.freeze(__('Recording…'));
			frappe.call({
				method: 'ffl_integrations.payments.api.confirm_out_of_band_refund',
				args: {
					return_invoice_name: frm.doc.name,
					portal_reference: values.portal_reference,
					reason: values.reason,
				},
				callback(r) {
					frappe.dom.unfreeze();
					if (r.message && r.message.ok) {
						frappe.show_alert({
							message: __('Portal refund {0} recorded.', [r.message.portal_reference]),
							indicator: 'green',
						});
						frm.reload_doc();
					}
				},
				error() { frappe.dom.unfreeze(); },
			});
		},
	});
	d.show();
	d.get_primary_btn().removeClass('btn-primary').addClass('btn-danger');
}
