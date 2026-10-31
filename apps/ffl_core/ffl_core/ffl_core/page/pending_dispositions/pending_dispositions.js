// Pending Order — the queue of orders that still need work: firearms not yet
// disposed (de-booked), payment not yet collected, and/or (counter orders) the
// shipment not yet pushed to ShipStation. Two sources, rendered together:
//   • Counter orders — a submitted Sales Invoice, with a destination FFL
//     (firearm shipment) or without one — a "direct" order: accessories
//     shipping to the buyer, or a counter pickup (ffl_core.api.manual_order).
//     Two INDEPENDENT actions, either order:
//     "Dispose" (review + confirm-checkbox dialog) books the transfer, takes
//     the gun out of stock, and pushes to FastBound; "Ship" pushes the order
//     to ShipStation (ffl_integrations.shipstation.api.push_shipment) so staff
//     buy the label there — shown only while ShipStation is enabled and the
//     order isn't pushed yet. Its dialog also carries "Mark Shipped (no
//     ShipStation)" as a secondary action, which is where that escape hatch
//     lives whenever Ship can be opened; the card offers its own Mark Shipped
//     / Picked Up button only when Ship is absent or unusable (ShipStation
//     off, already pushed, counter pickup, unpaid, goods not in yet). Both ship
//     paths (Ship and Mark Shipped) first book the Delivery Note leg for
//     deferred non-firearm stock lines, so inventory and COGS settle with the
//     goods. NEITHER waits on Dispose — they clear the shipping half only; a
//     gun still on the bound book keeps the row here (needs_disposition) with
//     its Dispose button, and both surfaces say so before they stamp. The row
//     leaves once there is nothing left to dispose, collect, or ship.
//   • Web orders — paid WooCommerce orders (Woo Online Order), tagged "web";
//     they wait for the dealer to enter the serial on the WC Edit Order screen
//     (ffl_woo_sync.woocommerce.fulfillment). Single "Dispose" action — the
//     store's own ShipStation WooCommerce plugin creates the shipment, so the
//     POS never pushes web orders to ShipStation.
// "Record Payment" books a later payment against an on-account counter order.
// "Cancel Order" (counter rows only) reverses the whole order in one call —
// dispositions/stock/FastBound via the invoice cancel cascade, linked Sales
// Order, refund PE for what was paid, best-effort ShipStation void
// (ffl_core.api.manual_order.cancel_order).

//   • GunBroker orders — sales made on GunBroker (ffl_integrations.gunbroker).
//     Unlike a web order the store buys this label itself, and unlike a counter
//     order the money arrives as a cheque somebody has to record before the gun
//     may ship: "Mark Payment Received" is what opens Dispose. Nothing here may
//     be edited or cancelled — a GunBroker sale cannot be unwound from the
//     seller's side, so those buttons are not offered rather than offered and
//     refused. Once paid, Ship works as on a counter order, before or after
//     Dispose. Tracking goes back to GunBroker automatically once the order is
//     disposed and the label bought, so there is no Mark Shipped either.

// row.source → which of the three a card is. Anything unrecognised reads as a
// counter order, which is the conservative default: it is the only kind whose
// row shape this page has always been able to render.
const KIND = { woo: 'woo', gunbroker: 'gunbroker' };

// What each kind of order may do. A table, not a pile of `!isWeb` conditions:
// the negations are what made "not web" quietly mean "counter", and a third
// source turned every one of them into a wrong answer.
const CAPABILITIES = {
	counter: {
		// pickup: a counter pickup whose gun was ordered to the store is handed
		// over on a 4473 ticket (ffl_core.api.counter_pickup), like a web pickup.
		ship: true, markShipped: true, recordPayment: true,
		markPaymentReceived: false, edit: true, cancel: true, pickup: true,
		wcStatusBadge: false, gbStatusBadge: false, progressBadges: true,
		assignSerial: false,
	},
	woo: {
		// WooCommerce's own ShipStation plugin buys the web label, so POS never
		// pushes a web order; and a web order is paid before it reaches us.
		ship: false, markShipped: false, recordPayment: false,
		markPaymentReceived: false, edit: false, cancel: false, pickup: true,
		wcStatusBadge: true, gbStatusBadge: false, progressBadges: false,
		assignSerial: false,
	},
	gunbroker: {
		ship: true, markShipped: false, recordPayment: false,
		markPaymentReceived: true, edit: false, cancel: false, pickup: false,
		wcStatusBadge: false, gbStatusBadge: true, progressBadges: true,
		// A model-level listing sells interchangeable guns: the card names a gun
		// for each unit sold (assign_units) before Dispose may run.
		assignSerial: true,
	},
};

// Dispose is one button over three different backends.
const DISPOSE_METHOD = {
	counter: 'ffl_core.api.manual_order.dispose_order',
	woo: 'ffl_woo_sync.woocommerce.fulfillment.dispose_web_order',
	gunbroker: 'ffl_integrations.gunbroker.fulfillment.dispose_gb_order',
};
const DISPOSE_ARG = {
	counter: 'sales_invoice',
	woo: 'woo_online_order',
	gunbroker: 'gunbroker_order',
};
// Web and GunBroker orders carry loose-stock lines (ammo, optics, parts): Dispose asks
// where in the store each came from. A counter order named it when it was rung up.
const PD_STOCK_LINES_METHOD = {
	woo: 'ffl_woo_sync.woocommerce.fulfillment.get_dispose_stock_lines',
	gunbroker: 'ffl_integrations.gunbroker.fulfillment.get_dispose_stock_lines',
};

// ---- Where the loose stock came from — PURE KERNEL ------------------------------
// One dropdown per stock line that is in a location; a line that is in no location
// (all unassigned) has nothing to pick and is left out. Blank = "not said": the units
// then show on the Storage Locations "Not placed" tab to be confirmed.
// Dialog labels and <option> text are written as HTML, and an item name is free text: escaped here.
function pdEscape(text) {
	return String(text === null || text === undefined ? '' : text)
		.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
		.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function pdLocationFields(lines) {
	return (lines || []).filter((l) => (l.locations || []).length).map((l, i) => ({
		fieldname: `stg_loc_${i}`,
		line: String(l.line),
		label: `${pdEscape(l.qty)} × ${pdEscape(l.item_name || l.item_code)}`,
		options: [{ value: '', label: '' }].concat(l.locations.map((o) => ({ value: o.location, label: `${pdEscape(o.label)} (${pdEscape(o.qty)})` }))),
		default: l.chosen || '',
	}));
}

// {line: location} for the dropdowns the clerk filled in.
function pdCollectLocations(fields, values) {
	const out = {};
	(fields || []).forEach((f) => {
		const v = (values || {})[f.fieldname];
		if (v) out[f.line] = v;
	});
	return out;
}

// ---- Model-level units — PURE KERNEL ------------------------------------------
// A unit still needs staff when it has no gun (or its gun was lost to a conflict)
// and was not refunded on GunBroker. Dispose stays disabled while any does.
function pdOpenUnits(units) {
	return (units || []).filter((u) => !u.refunded && (!u.serial || u.error)).length;
}

// The units a "Confirm all" press would assign: the ones holding a suggestion.
function pdSuggestedUnits(units) {
	return (units || []).filter((u) => !u.refunded && !u.serial && u.suggested);
}

// ---- Agreements — PURE KERNEL ----------------------------------------------------
// What a counter order's sale still needs signed (manual_order._agreement_rows),
// one row per agreement (per gun for an Each firearm sold one). The kind picks the
// label and colour; an unsigned one never blocks the card — Dispose may override.
function pdAgreementKind(a) {
	if (!a) return 'missing';
	if (a.signed) return 'signed';
	if (a.state === 'pending') return a.link_open ? 'sent' : 'link_expired';
	if (a.state === 'expired') return 'expired';
	return 'missing';
}

// "Used Item Agreement, Bill of Sale (SN1)" — the unsigned ones, for a message (escaped).
function pdAgreementNames(rows) {
	return (rows || []).map((a) => pdEscape(a.template) + (a.serial_no ? ` (${pdEscape(a.serial_no)})` : '')).join(', ');
}

// ---- Sections — PURE KERNEL, no DOM / frappe / __() ----
// The server stamps every row (counter, web, GunBroker) with `stage`; the page
// groups by it in this order, filters by source, searches, and sorts each
// section oldest-first. Unit-tested under Node in tests/js/pending_sections.test.js.
const PD_STAGE_ORDER = ['blocked', 'to_place', 'waiting', 'ready_ship', 'ready_pickup', 'disposing'];
// Which button is THE next action in each section; every other button on the
// card drops to secondary styling.
const PD_PRIMARY = {
	blocked: '.btn-fulfill-stock, .btn-resource, .btn-pay, .btn-gb-paid',
	to_place: '.btn-release',
	waiting: '',
	ready_ship: '.btn-ship, .btn-mark-shipped, .btn-dispose, .btn-gb-invoice',
	ready_pickup: '.btn-pickup, .btn-mark-shipped, .btn-dispose',
	disposing: '.btn-dispose',
};

function pdMatchesFilter(row, filter) {
	if (!filter || filter === 'all') return true;
	if (filter === 'woo') return row.source === 'woo';
	if (filter === 'gunbroker') return row.source === 'gunbroker';
	return !row.source || row.source === 'sales_invoice';
}

function pdMatchesQuery(row, query) {
	const q = String(query || '').trim().toLowerCase();
	if (!q) return true;
	const hay = [row.customer_name, row.customer, row.sales_invoice, row.woo_order_id, row.gb_order_id,
		...(row.pending_serials || []).map((p) => p && p.serial),
		...(row.disposed_serials || []).map((p) => (p && p.serial) || p),
		// The DO and the house's web reference are what a distributor quotes on the phone.
		...((row.legs && row.legs.distributor_orders) || row.distributor_orders || [])
			.flatMap((d) => [d && d.name, d && d.web_ref])]
		.filter(Boolean).join(' ').toLowerCase();
	return hay.includes(q);
}

function pdSections(rows, filter, query) {
	const kept = (rows || []).filter((r) => pdMatchesFilter(r, filter) && pdMatchesQuery(r, query));
	return PD_STAGE_ORDER.map((stage) => ({
		stage,
		// A row whose stage the page does not know (an older server, a channel's
		// own vocabulary) still has to show somewhere: it lands in ready_ship.
		rows: kept.filter((r) => (PD_STAGE_ORDER.includes(r.stage) ? r.stage : 'ready_ship') === stage)
			.sort((a, b) => (b.age_days || 0) - (a.age_days || 0)),
	})).filter((s) => s.rows.length);
}

if (typeof frappe !== 'undefined') {
frappe.pages['pending-dispositions'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('Pending Order'),
		single_column: true,
	});
	wrapper.pending_dispositions_app = new PendingDispositionsApp(page);
};

// Reload on every visit — including returning here after saving an edit — so a
// cancelled-and-replaced invoice never lingers as a stale pending row.
frappe.pages['pending-dispositions'].on_page_show = function (wrapper) {
	wrapper.pending_dispositions_app?._load();
};
}


class PendingDispositionsApp {
	constructor(page) {
		this.page = page;
		this.$body = $(`
			<div class="ffl-pending ffl-pending-orders">
				<style>
					.ffl-pending { max-width: 960px; margin: 0 auto; padding: 8px 0 60px; }
					.ffl-dispose-cr .form-section + .form-section { border-top: 0; padding-top: 0; padding-bottom: 0; }
					.ffl-dispose-cr .form-section + .form-section .section-body { padding-top: 0; margin-top: 0; }
					.ffl-pending .card {
						background: var(--card-bg); border: 1px solid var(--border-color);
						border-radius: 8px; padding: 16px 18px; margin-bottom: 12px;
						display: grid; grid-template-columns: 1fr auto; gap: 14px; align-items: center;
					}
					.ffl-pending .who { font-weight: 600; }
					.ffl-pending .meta { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
					.ffl-pending .serials { margin-top: 6px; font-size: 12px; }
					.ffl-pending .serial-error {
						margin-top: 6px; font-size: 12px; color: var(--red-600, #dc2626);
					}
					.ffl-pending .serials .pill {
						display: inline-block; background: var(--blue-100); color: var(--blue-700);
						padding: 1px 8px; border-radius: 10px; margin: 0 6px 4px 0;
					}
					/* Disposed serials are shown greyed (informational, already on the
					   bound book) vs the blue pending pills that still need a disposition.
					   Page-scoped to .ffl-pending-orders — a bare .ffl-pending rule would
					   outlive this page after SPA navigation. */
					.ffl-pending-orders .serials .pill.disposed {
						background: var(--gray-200, #e5e7eb); color: var(--gray-600, #4b5563);
					}
					.ffl-pending .badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px; margin-left: 8px; }
					.ffl-pending .badge.ok { background: var(--bg-green); color: var(--green-700); }
					.ffl-pending .badge.warn { background: var(--bg-red); color: var(--red-700); }
					.ffl-pending .badge.amber { background: var(--bg-yellow); color: var(--yellow-700); }
					.ffl-pending .badge.biz { background: var(--blue-500); color: #fff; font-weight: 600; }
					.ffl-pending .badge.ind { background: var(--gray-200); color: var(--gray-700); }
					.ffl-pending .badge.web { background: var(--purple, #6e56cf); color: #fff; font-weight: 600; }
					.ffl-pending .badge.wc { background: var(--gray-700, #4b5563); color: #fff; }
					.ffl-pending .amounts { font-weight: 500; }
					.ffl-pending .actions { display: flex; flex-direction: column; gap: 6px; align-items: stretch; min-width: 190px; }
					/* Fixed width (not min-width): the grid's auto action column otherwise
					   sizes to content, so a card carrying the long "Mark Shipped (no
					   ShipStation)" button renders wider than its neighbours. Pin the width
					   so every card's button column lines up; 230px fits that label. Scoped
					   to .ffl-pending-orders (this page only) — a desk page's style stays in
					   the document after SPA navigation, so an unscoped width would also
					   apply to any other page that reuses the .ffl-pending class. */
					.ffl-pending-orders .actions { width: 230px; }
					.ffl-pending-orders .assign-units { margin-top: 8px; font-size: 12px; }
					.ffl-pending-orders .assign-unit {
						display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-bottom: 6px;
					}
					.ffl-pending-orders .assign-unit .unit-label { min-width: 150px; font-weight: 500; }
					.ffl-pending-orders .assign-unit .pill {
						background: var(--blue-100); color: var(--blue-700); padding: 1px 8px; border-radius: 10px;
					}
					.ffl-pending-orders .assign-unit .pill.suggested { background: var(--gray-100, #f3f4f6); color: var(--gray-700, #374151); }
					.ffl-pending-orders .assign-unit .assign-scan { width: 150px; }
					.ffl-pending-orders .assign-unit .assign-pick { width: 190px; }
					.ffl-pending .empty { text-align: center; color: var(--text-muted); padding: 48px 0; }
					.ffl-pending-orders .pd-toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
					.ffl-pending-orders .pd-section { margin: 18px 0 8px; font-size: 13px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: .04em; }
					.ffl-pending-orders .overdue { color: var(--red-600, #dc2626); font-weight: 600; }
					.ffl-pending-orders .agreements { margin-top: 6px; font-size: 12px; display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: center; }
					.ffl-pending-orders .agreements .agr { padding: 1px 8px; border-radius: 10px; }
					.ffl-pending-orders .agreements .agr.signed { background: var(--bg-green); color: var(--green-700); }
					.ffl-pending-orders .agreements .agr.sent { background: var(--bg-yellow); color: var(--yellow-700); }
					.ffl-pending-orders .agreements .agr.missing, .ffl-pending-orders .agreements .agr.link_expired,
					.ffl-pending-orders .agreements .agr.expired { background: var(--bg-red); color: var(--red-700); }
					/* Frappe v15 names the dialog's action .btn-primary, v16 .btn-modal-primary. */
					.ffl-dispose-unsigned .standard-actions .btn-primary, .ffl-dispose-unsigned .standard-actions .btn-modal-primary { background: var(--red-600, #c62828); border-color: var(--red-600, #c62828); color: #fff; }
					.ffl-agreement-warning { background: var(--bg-red); color: var(--red-700); border-radius: 6px; padding: 10px 12px; margin-bottom: 10px; font-weight: 500; }
					.ffl-dispose-summary { line-height: 1.7; }
					.ffl-dispose-summary ul { margin: 4px 0 0; padding-left: 20px; }
					/* Phone: side gutter, and the action column drops under the order. */
					@media (max-width: 767px) {
						.ffl-pending { padding-inline: 15px; }
						.ffl-pending .card { grid-template-columns: minmax(0, 1fr); }
						.ffl-pending-orders .actions { width: auto; min-width: 0; }
						/* Stacked C&R columns keep Frappe's gutters: City/State/ZIP would sit indented. */
						.ffl-dispose-cr .form-column { padding-inline: 0; }
					}
				</style>
				<div class="pd-toolbar">
					<div class="pd-chips">
						${['all', 'counter', 'woo', 'gunbroker'].map((f) => `<button class="btn btn-xs ${f === 'all' ? 'btn-primary' : 'btn-default'} pd-chip" data-filter="${f}">${{ all: __('All'), counter: __('Counter'), woo: __('Web'), gunbroker: __('GunBroker') }[f]}</button>`).join(' ')}
					</div>
					<input type="text" class="form-control input-sm" id="pd-search" placeholder="${__('Customer, order, serial…')}" style="max-width:260px">
				</div>
				<div id="list"></div>
			</div>
		`).appendTo(this.page.body);
		this.rows = [];
		this.filter = 'all';
		this.query = '';
		this.$body.find('.pd-chip').on('click', (e) => {
			this.filter = $(e.currentTarget).data('filter');
			this.$body.find('.pd-chip').removeClass('btn-primary').addClass('btn-default');
			$(e.currentTarget).removeClass('btn-default').addClass('btn-primary');
			this._render(this.rows);
		});
		// Debounced: every keystroke would otherwise rebuild every card.
		let searchTimer = null;
		this.$body.find('#pd-search').on('input', (e) => {
			this.query = e.currentTarget.value || '';
			clearTimeout(searchTimer);
			searchTimer = setTimeout(() => this._render(this.rows), 150);
		});

		this.page.set_primary_action(__('Refresh'), () => this._load(), 'refresh');
		// A customer signing (agreements.submit_signature / a paper copy) updates the
		// cards waiting on them; opening the page again reloads everything anyway.
		if (frappe.realtime) {
			frappe.realtime.doctype_subscribe('Customer Agreement');
			frappe.realtime.on('customer_agreement_signed', (msg) => this._onAgreementSigned(msg || {}));
		}
		this._load();
	}

	_onAgreementSigned(msg) {
		(this.rows || []).filter((r) => (KIND[r.source] || 'counter') === 'counter' && r.customer === msg.customer
			&& (r.agreements || []).length).forEach((r) => this._refreshAgreements(r).then(() => {
			frappe.show_alert({ message: __('{0} signed {1}.', [frappe.utils.escape_html(r.customer_name || r.customer),
				pdAgreementNames([{ template: msg.agreement, serial_no: msg.serial_no }])]), indicator: 'green' }, 7);
		}));
	}

	// Re-read one counter order's agreements and redraw just its card.
	_refreshAgreements(row, callOpts = {}) {
		return frappe.call({ method: 'ffl_core.api.manual_order.order_agreements',
			args: { sales_invoice: row.sales_invoice }, ...callOpts }).then((r) => {
			const m = r.message || {};
			row.agreements = m.rows || [];
			row.agreements_missing = row.agreements.filter((a) => !a.signed);
			row.agreement_email = m.email || '';
			this._redrawCard(row);
		});
	}

	_redrawCard(row) {
		if (!row._$card || !row._$card.closest('body').length) return;
		const $tmp = $('<div>');
		const $old = row._$card;
		this._renderCard(row, $tmp);
		$old.replaceWith($tmp.children());
	}

	_resendAgreement(row, a) {
		const what = pdAgreementNames([a]);
		const d = new frappe.ui.Dialog({
			title: __('Resend agreement link'),
			fields: [
				{ fieldtype: 'HTML', fieldname: 'note', options: `<div class="text-muted" style="margin-bottom:8px">${__('Emails {0} a new link to sign {1}. The old link stops working.', [frappe.utils.escape_html(row.customer_name || row.customer), what])}</div>` },
				{ fieldtype: 'Data', fieldname: 'email', options: 'Email', label: __('Email'), reqd: 1, default: row.agreement_email || '' },
			],
			primary_action_label: __('Send'),
			primary_action: (v) => frappe.call({
				method: 'ffl_core.api.manual_order.resend_order_agreement',
				args: { sales_invoice: row.sales_invoice, template: a.template, serial_no: a.serial_no || null,
					email: (v.email || '').trim() },
				freeze: true, freeze_message: __('Sending…'),
				callback: (r) => {
					if (!r.message) return;
					d.hide();
					frappe.show_alert({ message: __('Signing link sent to {0}.', [frappe.utils.escape_html(r.message.email)]), indicator: 'green' }, 7);
					this._refreshAgreements(row);
				},
			}),
		});
		d.show();
	}

	_load() {
		// Three sources: counter orders (Sales Invoice), web orders (Woo Online
		// Order) and GunBroker orders. Each channel source is soft-guarded so the
		// page still works when its app isn't installed — a store that has never
		// enabled GunBroker must not lose its counter queue to a 404.
		const si = frappe.call({ method: 'ffl_core.api.manual_order.list_pending_dispositions' })
			.then((r) => (r && r.message) || []).catch(() => []);
		const web = frappe.call({ method: 'ffl_woo_sync.woocommerce.fulfillment.list_pending_web_orders' })
			.then((r) => (r && r.message) || []).catch(() => []);
		const gb = frappe.call({ method: 'ffl_integrations.gunbroker.fulfillment.list_pending_gb_orders' })
			.then((r) => (r && r.message) || []).catch(() => []);
		Promise.all([si, web, gb]).then(([siRows, webRows, gbRows]) => {
			// SI rows carry a date, channel rows a datetime — compare on the date
			// part so same-day rows from the three sources order consistently.
			const day = (r) => String(r.posting_date || '').slice(0, 10);
			const rows = siRows.concat(webRows, gbRows)
				.sort((a, b) => day(a).localeCompare(day(b)));
			this.rows = rows;
			this._render(rows);
			this._markBanned(rows);
		});
	}

	// A Banned buyer's order gets a red badge (ffl_core.customer_flags.banned_order_rows):
	// web and GunBroker orders still come in (booked after the goods leave), so staff
	// decide here whether to ship or cancel. Re-renders only when something matched.
	_markBanned(rows) {
		const pick = (k) => rows.map((r) => r[k]).filter(Boolean);
		frappe.call({
			method: 'ffl_core.customer_flags.banned_order_rows',
			args: { woo_orders: pick('woo_online_order'), gb_orders: pick('gunbroker_order'),
				customers: rows.filter((r) => (KIND[r.source] || 'counter') === 'counter').map((r) => r.customer).filter(Boolean) },
		}).then((r) => {
			const hit = r.message || {};
			const woo = new Set(hit.woo || []), gb = new Set(hit.gb || []), cust = new Set(hit.customers || []);
			let any = false;
			rows.forEach((row) => {
				row.banned_customer = woo.has(row.woo_online_order) || gb.has(row.gunbroker_order)
					|| ((KIND[row.source] || 'counter') === 'counter' && cust.has(row.customer));
				any = any || row.banned_customer;
			});
			if (any && this.rows === rows) this._render(rows);
		}).catch(() => { /* the badge is a hint; the queue works without it */ });
	}

	_render(rows) {
		const $list = this.$body.find('#list').empty();
		if (!rows.length) {
			$list.html(`<div class="empty">${__('Nothing waiting — every order is shipped and paid.')}</div>`);
			return;
		}
		const sections = pdSections(rows, this.filter, this.query);
		if (!sections.length) {
			$list.append(`<div class="empty">${__('Nothing matches this filter.')}</div>`);
			return;
		}
		const titles = {
			blocked: __('Needs attention'), to_place: __('To place with distributor'),
			waiting: __('Waiting on distributor'), ready_ship: __('Ready to ship'),
			ready_pickup: __('Ready for pickup'), disposing: __('Shipped — dispose'),
		};
		sections.forEach((sec) => {
			$list.append(`<div class="pd-section">${titles[sec.stage] || sec.stage} <span class="badge ind">${sec.rows.length}</span></div>`);
			sec.rows.forEach((row) => this._renderCard(row, $list));
		});
	}

	// The section's next action is the one primary button on the card; the rest
	// step back so a queue wall reads at a glance.
	_markPrimary($card, row) {
		$card.find('.btn').removeClass('btn-primary').addClass('btn-default');
		// Selectors in PD_PRIMARY are in preference order (not DOM order), the
		// trouble-leg buttons live in the legs strip rather than .actions, and a
		// disabled button is never the next action.
		const sels = (PD_PRIMARY[row.stage] || '').split(',').map((x) => x.trim()).filter(Boolean);
		for (const sel of sels) {
			const $btn = $card.find(sel).filter((i, el) => !el.disabled).first();
			if ($btn.length) { $btn.removeClass('btn-default').addClass('btn-primary'); return; }
		}
	}

	_renderCard(row, $list) {
		{
			// Three sources, one card. What each one may DO is a table rather
			// than a chain of negated conditions, because the negations were
			// what made "not web" silently mean "counter" — and a GunBroker row
			// reading as a counter row would offer Edit and Cancel Order on a
			// sale that cannot be unwound from the seller's side at all.
			const kind = KIND[row.source] || 'counter';
			const can = this._can(row);
			const isCounter = kind === 'counter';
			const isWeb = kind === 'woo';
			const isGb = kind === 'gunbroker';
			const typeBadge = isGb
				? `<span class="badge wc">${__('GunBroker')}</span>`
				: isWeb
				? `<span class="badge web">${__('Web')}</span>`
				: (row.is_business
					? `<span class="badge biz">${__('Business (FFL)')}</span>`
					: `<span class="badge ind">${__('Individual')}</span>`);
			// A direct order (no destination FFL — accessories to the buyer, a
			// counter pickup, or a GunBroker order that is all antiques) has no
			// license to badge or expire.
			const isDirect = (isCounter && !row.destination_ffl) || row.ships_direct === true;
			const fflBadge = isDirect ? ''
				: (row.source === 'gunbroker' && !row.destination_ffl)
					? `<span class="badge warn">${__('No FFL yet')}</span>`
				: (row.ffl_expired
					? `<span class="badge warn">${__('FFL expired / unverified')}</span>`
					: `<span class="badge ok">${__('FFL valid')}</span>`);
			const paid = row.paid;
			// A GunBroker order has no invoice until it is paid, so its outstanding
			// reads 0; only money we actually recorded makes it partly paid.
			const partly = !paid && row.grand_total && (row.source === 'gunbroker'
				? row.paid_total > 0.005
				: row.outstanding_amount < row.grand_total - 0.005);
			const payBadge = paid
				? `<span class="badge ok">${__('Paid')}</span>`
				: (partly ? `<span class="badge amber">${__('Partly paid')}</span>`
					: `<span class="badge amber">${__('Unpaid')}</span>`);
			// Web orders are pre-paid; a counter individual can't ship until paid.
			const blockShipUnpaid = this._blockShipUnpaid(row);
			const awaiting = !isCounter && row.awaiting_serial;
			// A serial DID arrive from WooCommerce but was rejected (not in stock /
			// wrong item / held by another order) — say so, or the row reads as
			// "Woo never sent it" and stalls silently.
			const serialErrors = !isCounter ? (row.serial_errors || []) : [];
			// A direct row with no shipping address can't push a label — it
			// clears through Mark Shipped / Picked Up instead.
			const noAddress = isDirect && !row.has_ship_address;
			// Ship-to-store goods not received yet: the parcel would go out short
			// (push_shipment refuses the same way).
			const awaitingGoods = !!(row.legs && row.legs.awaiting_goods);
			// In-store pickup: the buyer chose OUR licence in WooCommerce's FFL
			// list and collects here. There is no far-end dealer, so Dispose is
			// wrong — the server refuses it, because booking it would put a
			// transfer to ourselves in the bound book — and Pickup opens the
			// counter order instead. `awaiting` can still be true on one of
			// these, so exclude it explicitly rather than leaning on
			// needs_disposition alone.
			const isPickup = can.pickup && row.store_pickup === true;
			const pickupInvoice = isPickup ? (row.pickup_invoice || null) : null;
			// A counter pickup says server-side why its ticket cannot open yet
			// (gun not arrived, balance owed); a web pickup has no such key.
			const pickupBlock = isPickup && !pickupInvoice ? (row.pickup_block || '') : '';
			// A model-level unit with no gun yet: Dispose would ship one short.
			const openUnits = can.assignSerial ? pdOpenUnits(row.assign_units) : 0;
			// GunBroker: nothing disposes before Mark Payment Received (the server's
			// _require_ready_to_dispose refuses it, same stage as shipDisabled below).
			const disposeDisabled = row.ffl_expired || blockShipUnpaid || awaiting || openUnits > 0
				|| (isGb && row.channel_stage === 'awaiting_payment');
			// A pickup with a gun on it is NOT "hand it over and Mark Picked Up":
			// the server refuses Mark Picked Up for exactly those rows, and the
			// gun leaves on its 4473 ticket. Its hint is the server's pickup_block
			// (rendered below); a pickup of accessories only still gets the
			// no-address hint.
			const noAddressHint = noAddress && !(isPickup && (pickupBlock || pickupInvoice));
			// Every unit still waiting gets its serial from the distributor (no
			// `local` lane among them): never send staff to WooCommerce for it (a
			// serial typed there would reserve one of our own guns) — unless a
			// to_store unit has arrived and the auto-assign missed, where the Woo
			// order IS the manual fix (receive_bridge says the same).
			const waitLanes = row.awaiting_serial_lanes || [];
			const serialFromDistributor = awaiting && waitLanes.length > 0 && !waitLanes.includes('local');
			const disposeHint = row.ffl_expired ? __('Verify the FFL first.')
				: (serialFromDistributor ? ((row.arrived && waitLanes.includes('to_store'))
					? __('Arrived — assign the received serial on the WooCommerce order.')
					: __('Serial comes with the distributor order when it arrives.'))
				: (openUnits ? __('Assign a gun to every unit first.')
				: (serialErrors.length ? __('Serial rejected — correct it on the WooCommerce order.')
					: (awaiting ? __('Awaiting serial from WooCommerce.')
						: (blockShipUnpaid ? (noAddress ? __('Record payment before it is picked up.')
							: __('Record payment before shipping.'))
							: (noAddressHint ? __('Counter pickup — hand it over and Mark Picked Up.')
								: (awaitingGoods ? __('Waiting for ship-to-store goods to arrive.') : '')))))));
			// A direct row never disposes — there is no FFL transfer to book;
			// the server refuses it anyway (fail closed), so don't render it.
			const showDispose = isCounter
				? (row.needs_disposition && !isDirect)
				: ((row.needs_disposition || awaiting) && !isPickup);
			// Ship is independent of Dispose but shares its gates: never queue a
			// label for an expired FFL, an unpaid order, or a gun not yet named.
			// A GunBroker order has no invoice before Dispose, so its Ship goes
			// through push_gb_shipment, which builds the shipment off the order.
			// A pickup has no destination to ship to — the order leaves over the
			// counter, so Ship is not offered at all (it used to render disabled).
			// With an address (or an FFL) the Ship button behaves exactly as before.
			// A disposed GunBroker order whose invoice failed to post waits for
			// Create Invoice: push_gb_shipment only builds off a pending order.
			const needsGbInvoice = isGb && row.disposed && !row.sales_invoice_posted;
			const canShip = can.ship && row.shipstation_enabled && !row.ship_pushed && !noAddress
				&& !needsGbInvoice;
			// GunBroker: nothing ships before Mark Payment Received.
			const shipDisabled = row.ffl_expired || blockShipUnpaid || awaitingGoods
				|| openUnits > 0 || serialErrors.length > 0
				|| (isGb && row.channel_stage === 'awaiting_payment');
			// Mark Shipped / Picked Up renders off the server-computed gate
			// (can_mark_shipped mirrors mark_shipped_manually's refusals), so
			// the button and the API can't drift — and it stays independent
			// of the ShipStation toggle, or a firearm-less row could never
			// clear with the integration off. One method, because the Ship
			// dialog offers the same action and must gate it the same way.
			// It is only on the CARD when it can be PRESSED and Ship cannot: a
			// usable Ship button already carries it as the dialog's secondary
			// action, and a disabled pair (an unpaid order greys out both, for
			// the same reason) reads as two buttons for one action. The reason
			// is under the card either way, and the dialog greys the action out
			// on the same payment rule.
			const canMarkShipped = this._canMarkShipped(row)
				&& !blockShipUnpaid && !(canShip && !shipDisabled);
			const disposedBadge = (can.progressBadges && row.disposed)
				? `<span class="badge ok">✓ ${__('Disposed')}</span>` : '';
			const shippedBadge = (can.progressBadges && row.ship_pushed)
				? `<span class="badge ok">✓ ShipStation${row.shipstation_order_id
					? ' #' + frappe.utils.escape_html(row.shipstation_order_id) : ''}</span>`
				: '';
			// Pending (still-to-dispose) serials render as blue actionable pills.
			// Once every serial is disposed that list empties — fall back to the
			// disposed serials, greyed, so a disposed order still shows its guns
			// at a glance (they're on the bound book, not gone).
			const pendingSerials = row.pending_serials || [];
			const serials = pendingSerials.length
				? pendingSerials.map((s) =>
					`<span class="pill">${frappe.utils.escape_html(s.serial)}</span>`).join('')
				: (row.disposed_serials || []).map((s) =>
					`<span class="pill disposed" title="${__('On the bound book')}">${frappe.utils.escape_html(s.serial)}</span>`).join('');
			const conflictBadge = serialErrors.length
				? `<span class="badge warn">${__('Serial conflict')}</span>` : '';
			// Says WHY this row has no Dispose button, so nobody goes looking for
			// one — or worse, decides the order is broken and hand-books it.
			const bannedBadge = row.banned_customer
				? `<span class="badge warn" title="${__('This buyer is marked Banned. Decide whether to ship or cancel and refund.')}">${__('Banned customer')}</span>` : '';
			const pickupBadge = (can.pickup && row.store_pickup === true)
				? `<span class="badge amber">${row.pickup_invoice
					? __('In-store pickup · counter order open')
					: __('In-store pickup')}</span>` : '';
			// The agreements the gun waits for: signed / sent (until when) / not signed,
			// each unsigned one with its own Resend. A pill on the name line, not a
			// blocked stage — Dispose can still go ahead (and says it is recorded).
			const agreementRows = isCounter ? (row.agreements || []) : [];
			const agreementBadge = (isCounter && (row.agreements_missing || []).length)
				? `<span class="badge amber" title="${__('Dispose asks before shipping without it.')}">${__('Awaiting signature')}</span>` : '';
			const agreementLabels = {
				signed: () => __('signed'), missing: () => __('not signed'), expired: () => __('signature expired'),
				link_expired: () => __('link expired'),
				sent: (a) => __('sent, link open until {0}', [frappe.datetime.str_to_user(a.token_expires)]),
			};
			const agreementsStrip = agreementRows.length
				? `<div class="agreements"><span>${__('Agreements')}:</span>${agreementRows.map((a, i) => {
					const kind = pdAgreementKind(a);
					return `<span class="agr ${kind}">${pdAgreementNames([a])} · ${agreementLabels[kind](a)}</span>`
						+ (a.signed ? '' : `<button class="btn btn-xs btn-default btn-resend-agreement" data-i="${i}">${__('Resend link')}</button>`);
				}).join('')}</div>` : '';
			const serialErrorLines = serialErrors.map((e) =>
				`<div class="serial-error">⚠ ${frappe.utils.escape_html(e.error)}</div>`).join('');
			// WooCommerce live status + amounts (web rows mirror the WC order).
			const wcStatusBadge = (can.wcStatusBadge && row.woo_status)
				? `<span class="badge wc">${__('WC')}: ${frappe.utils.escape_html(row.woo_status)}</span>` : '';
			const gbStatusBadge = (can.gbStatusBadge && row.gb_status_label)
				? `<span class="badge wc">${__('GB')}: ${frappe.utils.escape_html(row.gb_status_label)}</span>` : '';
			const cur = row.currency || undefined;
			const amountsLine = !isCounter
				? `<div class="meta amounts">${__('Total')} ${format_currency(row.order_total || 0, cur)}`
					+ ` · ${__('Tax')} ${format_currency(row.total_tax || 0, cur)}`
					+ ` · ${__('Shipping')} ${format_currency(row.shipping_total || 0, cur)}`
					+ (row.discount_total ? ` · ${__('Discount')} −${format_currency(row.discount_total, cur)}` : '')
					+ `</div>`
				: '';
			// P1: per-leg strip — local leg (counter only) = deferred serials/stock
			// lines still owed; each DO is a distributor leg with its state-machine
			// status and the house's web reference (pill opens the DO form). A web
			// order carries its DOs as row.distributor_orders.
			const legs = (isCounter && row.legs) ? row.legs : null;
			const doLegs = legs ? (legs.distributor_orders || []) : (isWeb ? (row.distributor_orders || []) : []);
			const legPills = [];
			if (legs && (legs.local.undelivered > 0 || legs.local.pending_serials > 0)) {
				const bits = [];
				if (legs.local.pending_serials > 0) bits.push(__('{0} serial(s) to dispose', [legs.local.pending_serials]));
				if (legs.local.undelivered > 0) bits.push(__('{0} line(s) to ship', [legs.local.undelivered]));
				legPills.push(`<span class="pill">${__('Local')}: ${bits.join(' · ')}</span>`);
			}
			if (legs && legs.awaiting_arrival > 0) {
				legPills.push(`<span class="pill" style="background:var(--orange-100); color:var(--orange-700)">${__('{0} firearm(s) awaiting arrival', [legs.awaiting_arrival])}</span>`);
			}
			doLegs.forEach((d) => {
				// A ship-to-store leg is done when it has ARRIVED here, not when the
				// distributor ships it.
				const toStore = d.order_type === 'Procure To Store';
				const arrived = toStore && d.receive_status === 'Received';
				const done = toStore ? arrived : d.status === 'Complete';
				// Status tone: trouble states must read at a glance on a queue wall.
				const tone = done ? '' : ({
					Error: 'background:var(--bg-red); color:var(--red-700)',
					Hold: 'background:var(--orange-100); color:var(--orange-700)',
					Cancelled: 'opacity:.55',
				}[d.status] || '');
				const state = !toStore ? d.status
					: (arrived ? __('arrived')
						: (d.receive_status === 'Partial' ? __('partly arrived') : d.status));
				legPills.push(`<span class="pill leg-do ${done ? 'disposed' : ''}" data-do="${frappe.utils.escape_html(d.name)}" style="cursor:pointer; ${tone}" title="${__('Open Distributor Order')}">${frappe.utils.escape_html(d.distributor)}${toStore ? ' · ' + __('to store') : ''} · ${frappe.utils.escape_html(d.name)} · ${frappe.utils.escape_html(state)}${d.web_ref ? ' · ref ' + frappe.utils.escape_html(d.web_ref) : ''}</span>`);
			});
			// Ship-to-store lines still on their way that the shelf could serve now
			// One small button per line; the parcel, when it
			// lands, becomes restock — the Distributor Order is never cancelled.
			const subs = legs ? (legs.substitutable || []) : [];
			// Legs a person must act on: the house killed a line, shipped
			// short, or has held the order past its limit. Named with the web
			// reference — the number to quote when phoning the distributor.
			const trouble = legs ? (legs.trouble || []) : [];
			trouble.forEach((t) => {
				const label = { killed: __('{0} killed {1}'), short: __('{0} short-shipped {1}'),
					hold: __('{0} hold {1}') }[t.kind] || '{0} {1}';
				legPills.push(`<span class="pill" style="background:var(--bg-red); color:var(--red-700)">${label.replace('{0}', frappe.utils.escape_html(t.distributor)).replace('{1}', frappe.utils.escape_html(t.detail || ''))} · ${frappe.utils.escape_html(t.do_name)}${t.web_ref ? ' · ref ' + frappe.utils.escape_html(t.web_ref) : ''}</span>`);
			});
			// The waiting lines collapse behind one button:
			// a pill per product buried the card, and Fulfill from stock was offered
			// on lines the shelf could not cover — a click that only ever ended in
			// "nothing on the shelf". The button now appears only when stock can
			// serve a line, says how many, and opens the picker.
			const servable = this._servableFromStock(subs);
			if (servable.length) {
				legPills.push(`<button class="btn btn-xs btn-default btn-fulfill-stock">${__('Fulfill from local stock ({0})', [servable.length])}</button>`);
			}
			// A troubled line still needs its two escape hatches on the card — the
			// house killed it or shipped short, and that is not a stock question.
			subs.filter((s) => trouble.some((t) => t.do_name === s.do_name)).forEach((s) => {
				legPills.push(`<span class="pill" style="background:var(--gray-100); color:var(--gray-700)">${frappe.utils.escape_html(s.item_name || s.item_code)}`
					+ ` <button class="btn btn-xs btn-default btn-resource" data-row="${frappe.utils.escape_html(s.row_name)}" style="margin-left:4px">${__('Re-source')}</button>`
					+ ` <button class="btn btn-xs btn-default btn-cancel-line" data-row="${frappe.utils.escape_html(s.row_name)}" style="color:var(--red-600)">${__('Cancel line')}</button>`
					+ `</span>`);
			});
			const legsStrip = legPills.length
				? `<div class="serials" style="margin-top:4px">${legPills.join('')}</div>` : '';
			const canRelease = !!(legs && legs.releasable);
			// Reference link: a Sales Invoice or the Woo Online Order.
			const ref = isWeb
				? `<a href="/app/woo-online-order/${encodeURIComponent(row.woo_online_order)}">${__('Order #{0}', [frappe.utils.escape_html(row.woo_order_id)])}</a>`
				: (isGb
					? `<a href="/app/gunbroker-order/${encodeURIComponent(row.gunbroker_order)}">${__('GunBroker #{0}', [frappe.utils.escape_html(row.gb_order_id)])}</a>`
					: `<a href="/app/sales-invoice/${row.sales_invoice}">${row.sales_invoice}</a>`);
			const $card = $(`
				<div class="card">
					<div>
						<div class="who">${frappe.utils.escape_html(row.customer_name || row.customer)} ${typeBadge} ${fflBadge} ${payBadge} ${disposedBadge} ${shippedBadge} ${wcStatusBadge} ${gbStatusBadge} ${conflictBadge} ${pickupBadge} ${bannedBadge} ${agreementBadge}</div>
						<div class="meta">
							${ref}
							· ${__('to')} ${frappe.utils.escape_html(row.destination_ffl_name || row.destination_ffl
								|| (row.has_ship_address
									? this._cityState(row.ship_to_preview) || __('shipping address')
									: __('Counter pickup')))}
							· ${frappe.datetime.str_to_user(row.posting_date)}
						<span class="${row.overdue ? 'overdue' : ''}">· ${__('{0}d', [row.age_days || 0])}</span>
							${row.outstanding_amount > 0 ? `· ${__('outstanding')} ${format_currency(row.outstanding_amount)}` : ''}
						</div>
						${amountsLine}
						${row.note ? `<div class="meta" style="font-style:italic">📝 ${frappe.utils.escape_html(row.note)}</div>` : ''}
						${row.sync_error ? `<div class="meta" style="color:var(--orange-600)" title="${frappe.utils.escape_html(row.sync_error)}">⚠ ${__('Could not refresh this order from GunBroker ({0} tries), so it may be out of date. Details are on the {1}.', [row.sync_fail_count || 1, `<a href="/app/gunbroker-order/${encodeURIComponent(row.gunbroker_order)}">${__('order')}</a>`])}</div>` : ''}
						<div class="serials">${serials}</div>
						${this._assignUnitsHtml(row)}
						${legsStrip}
						${agreementsStrip}
						${serialErrorLines}
					</div>
					<div class="actions">
						${canRelease ? `<button class="btn btn-primary btn-sm btn-release">${__('Place order')}</button>` : ''}
						${(can.recordPayment && !paid) ? `<button class="btn btn-default btn-sm btn-pay">${__('Record Payment')}</button>` : ''}
						${can.markPaymentReceived && row.channel_stage === 'awaiting_payment' ? `<button class="btn btn-primary btn-sm btn-gb-paid">${__('Mark Payment Received')}</button>` : ''}
						${showDispose ? `<button class="btn btn-primary btn-sm btn-dispose" ${disposeDisabled ? 'disabled' : ''}>${__('Dispose')}</button>` : ''}
						${isPickup && !pickupInvoice ? `<button class="btn btn-primary btn-sm btn-pickup" ${pickupBlock ? 'disabled' : ''}>${__('Pickup')}</button>` : ''}
						${pickupInvoice ? `<a class="btn btn-default btn-sm" href="/app/pos-invoice/${encodeURIComponent(pickupInvoice)}">${__('Open counter order')}</a>` : ''}
						${needsGbInvoice ? `<button class="btn btn-primary btn-sm btn-gb-invoice">${__('Create Invoice')}</button>
							<div class="meta">${__('Disposed, but the invoice did not post. Create it to ship.')}</div>` : ''}
						${canShip ? `<button class="btn ${row.disposed ? 'btn-primary' : 'btn-default'} btn-sm btn-ship" ${shipDisabled ? 'disabled' : ''}>${__('Ship')}</button>` : ''}
						${canMarkShipped ? `<button class="btn btn-default btn-sm btn-mark-shipped">${noAddress ? __('Mark Picked Up') : __('Mark Shipped (no ShipStation)')}</button>` : ''}
						${(can.edit && !row.has_disposition && !row.ship_pushed) ? `<button class="btn btn-default btn-sm btn-edit-order">${__('Edit')}</button>` : ''}
						${can.cancel ? `<button class="btn btn-default btn-sm btn-cancel-order" style="color:var(--red-600); border-color:var(--red-400)">${__('Cancel Order')}</button>` : ''}
						${disposeHint ? `<div class="meta">${disposeHint}</div>` : ''}
						${!disposeHint && row.blocked_reason ? `<div class="meta">${frappe.utils.escape_html(row.blocked_reason)}</div>` : ''}
						${!disposeHint && !row.blocked_reason && pickupBlock ? `<div class="meta">${frappe.utils.escape_html(pickupBlock)}</div>` : ''}
					</div>
				</div>`).appendTo($list);
			row._$card = $card;
			$card.find('.btn-resend-agreement').on('click', (e) => this._resendAgreement(row,
				agreementRows[$(e.currentTarget).data('i')]));
			$card.find('.btn-release').on('click', () => this._releaseLegs(row));
			$card.find('.pill.leg-do').on('click', (e) => frappe.set_route(
				'Form', 'Distributor Order', $(e.currentTarget).data('do')));
			$card.find('.btn-dispose').on('click', () => this._startDispose(row));
			$card.find('.btn-pickup').on('click', () => this._pickup(row));
			$card.find('.btn-ship').on('click', () => this._confirmShip(row));
			$card.find('.btn-mark-shipped').on('click', () => this._markShippedManually(row));
			$card.find('.btn-pay').on('click', () => this._recordPayment(row));
			$card.find('.btn-gb-paid').on('click', () => this._markGbPaymentReceived(row));
			$card.find('.btn-gb-invoice').on('click', () => this._createGbInvoice(row));
			$card.find('.btn-edit-order').on('click', () => frappe.set_route('create-order', 'edit', row.sales_invoice));
			$card.find('.btn-cancel-order').on('click', () => this._confirmCancel(row));
			// Model-level units (GunBroker): delegated, because there is one
			// control set per unit and the card may carry several.
			$card.on('click', '.btn-assign-confirm', (e) => this._assignUnit(row,
				$(e.currentTarget).closest('.assign-unit').data('row'), $(e.currentTarget).data('serial')));
			$card.on('click', '.btn-assign-all', () => this._confirmAllSuggested(row));
			$card.on('click', '.btn-unassign', (e) => this._assignUnit(row,
				$(e.currentTarget).closest('.assign-unit').data('row'), ''));
			// A scanner types the serial and presses Enter.
			$card.on('keydown', '.assign-scan', (e) => {
				if (e.key !== 'Enter') return;
				e.preventDefault();
				const serial = String($(e.currentTarget).val() || '').trim();
				if (serial) this._assignUnit(row, $(e.currentTarget).closest('.assign-unit').data('row'), serial);
			});
			$card.on('change', '.assign-pick', (e) => {
				const serial = String($(e.currentTarget).val() || '').trim();
				if (serial) this._assignUnit(row, $(e.currentTarget).closest('.assign-unit').data('row'), serial);
			});
			$card.on('click', '.btn-unit-refunded', (e) => this._markUnitRefunded(row,
				$(e.currentTarget).closest('.assign-unit').data('row'), 1));
			$card.on('click', '.btn-unit-unrefund', (e) => this._markUnitRefunded(row,
				$(e.currentTarget).closest('.assign-unit').data('row'), 0));
			$card.find('.btn-fulfill-stock').on('click', () => this._pickFromStock(row, servable));
			$card.find('.btn-resource').on('click', (e) => {
				const sub = subs.find((s) => s.row_name === $(e.currentTarget).data('row'));
				if (sub) this._resourceLine(row, sub, trouble.find((t) => t.do_name === sub.do_name));
			});
			$card.find('.btn-cancel-line').on('click', (e) => {
				const sub = subs.find((s) => s.row_name === $(e.currentTarget).data('row'));
				if (sub) this._cancelLine(row, sub, trouble.find((t) => t.do_name === sub.do_name));
			});
			this._markPrimary($card, row);
			// Confirm on a suggested gun is the next action on its card until Dispose
			// opens; _markPrimary resets every button, so it is restored here.
			$card.find('.btn-assign-confirm, .btn-assign-all').removeClass('btn-default').addClass('btn-primary');
		}
	}

	// ---- GunBroker model-level units ----------------------------------------------
	// One row per unit sold: the suggested gun with a Confirm button, a scan field and
	// a pick list for any other gun. Nothing is held until Confirm (or a scan / pick)
	// reaches the server, and the server re-checks everything the page cannot.
	_assignUnitsHtml(row) {
		const units = row.assign_units || [];
		if (!this._can(row).assignSerial || !units.length) return '';
		const esc = frappe.utils.escape_html;
		const candidates = row.assign_candidates || {};
		const lines = units.map((u) => {
			const key = esc(u.row);
			if (u.refunded) {
				return `<div class="assign-unit" data-row="${key}">
					<span class="unit-label">${esc(u.unit_label)}</span>
					<span class="badge amber">${__('Refunded on GunBroker')}</span>
					<button class="btn btn-xs btn-link btn-unit-unrefund">${__('Undo')}</button>
				</div>`;
			}
			const state = u.serial
				? `<span class="pill">${esc(u.serial)}</span> <button class="btn btn-xs btn-default btn-unassign">${__('Unassign')}</button>`
				: (u.suggested
					? `<span class="pill suggested">${esc(u.suggested_label || u.suggested)}</span> <button class="btn btn-xs btn-primary btn-assign-confirm" data-serial="${esc(u.suggested)}">${__('Confirm')}</button>`
					: `<span class="text-muted">${__('No free gun to suggest')}</span>`);
			const options = (candidates[u.item_code] || []).map((c) =>
				`<option value="${esc(c.serial)}">${esc(c.label || c.serial)}</option>`).join('');
			return `<div class="assign-unit" data-row="${key}">
				<span class="unit-label">${esc(u.unit_label)}</span> ${state}
				<input type="text" class="form-control input-xs assign-scan" placeholder="${__('Scan serial')}">
				<select class="form-control input-xs assign-pick"><option value="">${__('Pick another gun…')}</option>${options}</select>
				<button class="btn btn-xs btn-link btn-unit-refunded">${__('Refunded on GunBroker')}</button>
				${u.error ? `<div class="serial-error" style="flex-basis:100%">⚠ ${esc(u.error)}</div>` : ''}
			</div>`;
		}).join('');
		const all = pdSuggestedUnits(units).length > 1
			? `<button class="btn btn-xs btn-primary btn-assign-all">${__('Confirm all suggested ({0})', [pdSuggestedUnits(units).length])}</button>` : '';
		return `<div class="assign-units">${all}${lines}</div>`;
	}

	// Name `serial` for one unit; '' unassigns it. The queue is reloaded either way:
	// the server's answer (and the suggestions on every other card) is the truth.
	_assignUnit(row, unitRow, serial, then) {
		frappe.call({
			method: 'ffl_integrations.gunbroker.fulfillment.assign_gb_unit_serial',
			args: { gunbroker_order: row.gunbroker_order, row_name: unitRow, serial_no: serial || '' },
			freeze: true, freeze_message: __('Assigning…'),
			callback: (r) => {
				if (!r.message?.ok) return;
				if (then) return then();
				frappe.show_alert({
					message: serial ? __('{0} assigned.', [serial]) : __('Unassigned.'),
					indicator: 'green',
				}, 4);
				this._load();
			},
			error: () => this._load(),
		});
	}

	// One press for the common case: every unit takes the gun suggested for it.
	// Sequential on purpose (each call locks the order and the model), stopping at
	// the first refusal.
	_confirmAllSuggested(row) {
		const todo = pdSuggestedUnits(row.assign_units);
		const next = () => {
			const u = todo.shift();
			if (!u) { this._load(); return; }
			this._assignUnit(row, u.row, u.suggested, next);
		};
		next();
	}

	// Oversold: the buyer was refunded on GunBroker for a unit that cannot be filled.
	// One confirmation; the unit then needs no gun and is left off the invoice.
	_markUnitRefunded(row, unitRow, refunded) {
		const go = () => frappe.call({
			method: 'ffl_integrations.gunbroker.fulfillment.mark_gb_unit_refunded',
			args: { gunbroker_order: row.gunbroker_order, row_name: unitRow, refunded },
			freeze: true, freeze_message: __('Saving…'),
			callback: (r) => { if (r.message?.ok) this._load(); },
			error: () => this._load(),
		});
		if (!refunded) return go();
		frappe.confirm(
			__('Mark this unit as refunded on GunBroker? It is left off the invoice and the bound book. Only do this once the buyer has really been refunded for it on GunBroker.'),
			go);
	}

	// The one sentence every distributor-side action must show:
	// RSR and Sports South cancel by phone only, so anything done
	// here changes THIS system alone, and the operator has to make the call.
	_distributorWarning(sub, troubleLeg, action) {
		const esc = frappe.utils.escape_html;
		const ref = (troubleLeg && troubleLeg.web_ref) ? troubleLeg.web_ref : '—';
		return __('{0} changes only this store\'s system. The order at {1} stays live — call {1} to cancel it (order {2}, ref {3}). If it ships anyway it comes in as restock.',
			[action, esc(sub.distributor), esc(sub.do_name), esc(ref)]);
	}

	_resourceLine(row, sub, troubleLeg) {
		frappe.call({
			method: 'frappe.client.get_list',
			args: { doctype: 'Distributor', filters: { enabled: 1, stocking_enabled: 1 }, fields: ['name'] },
			callback: (r) => {
				const houses = (r.message || []).map((d) => d.name).filter((n) => n !== sub.distributor);
				if (!houses.length) { frappe.msgprint(__('No other house can ship to the store.')); return; }
				const d = new frappe.ui.Dialog({
					title: __('Re-source {0}', [frappe.utils.escape_html(sub.item_name || sub.item_code)]),
					fields: [
						{ fieldtype: 'Select', fieldname: 'house', label: __('Order from'), options: houses.join('\n'), reqd: 1, default: houses[0] },
						{ fieldtype: 'HTML', fieldname: 'warn', options: `<p class="text-muted">${this._distributorWarning(sub, troubleLeg, __('Re-source'))}</p>` },
					],
					primary_action_label: __('I have noted it — re-source'),
					primary_action: () => {
						const house = d.get_value('house');
						d.hide();
						frappe.call({
							method: 'ffl_core.api.manual_order.resource_line',
							args: { sales_invoice: row.sales_invoice, row_name: sub.row_name, distributor: house },
							freeze: true,
							callback: (res) => {
								frappe.show_alert({ message: __('Draft order {0} created — place it from this queue.', [(res.message || {}).do_name || '']), indicator: 'green' }, 8);
								this._load();
							},
						});
					},
				});
				d.show();
			},
		});
	}

	// Cancelling a line is the existing Edit flow (cancel + rebuild without the
	// line); the warning is what the user asked for, the edit page does the rest.
	_cancelLine(row, sub, troubleLeg) {
		frappe.confirm(this._distributorWarning(sub, troubleLeg, __('Cancel line')) + ' '
			+ __('Continue to Edit, where you remove the line?'),
			() => frappe.set_route('create-order', 'edit', row.sales_invoice));
	}

	// Ship-to-store lines the shelf can cover right now. The server counts
	// `available` the way fulfill_from_stock re-checks it: unheld serials for a
	// gun (one serial serves the hold), on-hand units for an accessory.
	_servableFromStock(subs) {
		return (subs || []).filter((s) => (s.available || 0) >= (s.is_firearm ? 1 : s.qty));
	}

	// The collapsed list: which waiting product to serve from the shelf, with the
	// units free behind each one. One line skips straight to it.
	_pickFromStock(row, servable) {
		const esc = frappe.utils.escape_html;
		const label = (s) => __('{0} — need {1}, {2} in local stock',
			[s.item_name || s.item_code, s.qty, s.available]);
		if (!servable.length) return;
		if (servable.length === 1) { this._fulfillFromStock(row, servable[0]); return; }
		const d = new frappe.ui.Dialog({
			title: __('Fulfill from local stock'),
			fields: [
				{ fieldtype: 'Select', fieldname: 'line', label: __('Product'), reqd: 1,
					options: servable.map((s) => ({ label: label(s), value: s.row_name })),
					default: servable[0].row_name },
				{ fieldtype: 'HTML', fieldname: 'note', options: `<p class="text-muted">${esc(
					__('The distributor order stays placed; when that parcel arrives it is restock.'))}</p>` },
			],
			primary_action_label: __('Fulfill from local stock'),
			primary_action: () => {
				const sub = servable.find((s) => s.row_name === d.get_value('line'));
				d.hide();
				if (sub) this._fulfillFromStock(row, sub, true);
			},
		});
		d.show();
	}

	// Serve an unarrived ship-to-store line from the shelf. A gun needs the
	// serial picked from the unheld in-stock list (the server re-checks it); an
	// accessory just confirms — unless the picker dialog already did.
	_fulfillFromStock(row, sub, confirmed) {
		const esc = frappe.utils.escape_html;
		const go = (serial) => frappe.call({
			method: 'ffl_core.api.manual_order.fulfill_from_stock',
			args: { sales_invoice: row.sales_invoice, row_name: sub.row_name, serial_no: serial || null },
			freeze: true,
			callback: () => {
				frappe.show_alert({ message: __('Served from stock.'), indicator: 'green' }, 6);
				this._load();
			},
		});
		const stays = __('{0} {1} stays placed; when it arrives it is restock.', [esc(sub.distributor), esc(sub.do_name)]);
		if (!sub.is_firearm) {
			if (confirmed) { go(null); return; }
			frappe.confirm(__('Serve {0} × {1} from shelf stock? {2}', [sub.qty, esc(sub.item_name || sub.item_code), stays]),
				() => go(null));
			return;
		}
		frappe.call({
			method: 'ffl_core.firearm.available_serials_for_items',
			args: { item_codes: JSON.stringify([sub.item_code]) },
			callback: (r) => {
				const list = (r.message || {})[sub.item_code] || [];
				if (!list.length) {
					frappe.msgprint(__('No unheld {0} on the shelf.', [esc(sub.item_name || sub.item_code)]));
					return;
				}
				const d = new frappe.ui.Dialog({
					title: __('Fulfill {0} from stock', [esc(sub.item_name || sub.item_code)]),
					fields: [
						{ fieldtype: 'Select', fieldname: 'serial', label: __('Serial'), options: list.join('\n'), reqd: 1, default: list[0] },
						{ fieldtype: 'HTML', fieldname: 'note', options: `<p class="text-muted">${stays}</p>` },
					],
					primary_action_label: __('Hold this serial'),
					primary_action: () => { const s = d.get_value('serial'); d.hide(); go(s); },
				});
				d.show();
			},
		});
	}

	// What this row's source may DO (CAPABILITIES keyed off row.source), in
	// one place so the card and the dialogs read the same table.
	_can(row) {
		return CAPABILITIES[KIND[row.source] || 'counter'];
	}

	// The server's pre-ship payment rule (manual_order.ship_payment_block) as the
	// page sees it: a counter individual's order ships once it is paid. Both ship
	// actions ask it — push_shipment and mark_shipped_manually are both refused by
	// that rule, so neither surface may offer a live button without it. The rule
	// does not exempt a counter pickup: Mark Picked Up clears through the same
	// gate, so an unpaid individual pickup is paid before it is handed over.
	_blockShipUnpaid(row) {
		const kind = KIND[row.source] || 'counter';
		return kind === 'counter' && !row.is_business && !row.paid;
	}

	// THE Mark Shipped gate, shared by the card's button and the Ship
	// dialog's secondary action so the two entry points can't drift from
	// each other or from the server (row.can_mark_shipped IS
	// mark_shipped_block). Payment stays out of it: the dialog renders the
	// action disabled for an unpaid order, and the card (which has the hint
	// under it) leaves the button out — see _render.
	_canMarkShipped(row) {
		return this._can(row).markShipped && !row.ship_pushed
			&& row.can_mark_shipped === true;
	}

	// "City, ST" label shared by the card's "to" line and the Ship dialog —
	// one expression, so the two can't render the same destination two ways.
	_cityState(p) {
		return [(p || {}).city, (p || {}).state].filter(Boolean).join(', ');
	}

	_releaseLegs(row) {
		const n = ((row.legs || {}).distributor_orders || [])
			.filter((d) => d.status === 'Draft').length;
		const owed = flt(row.outstanding_amount) > 0
			? ' ' + __('This order still owes {0} — the goods come to the store and the balance is collected at pickup.', [format_currency(row.outstanding_amount)])
			: '';
		frappe.confirm(
			__('Place {0} distributor order(s) with the distributor now?', [n]) + owed,
			() => this._doReleaseLegs(row));
	}

	_doReleaseLegs(row) {
		frappe.call({
			method: 'ffl_core.api.manual_order.release_order_legs',
			args: { sales_invoice: row.sales_invoice },
			freeze: true, freeze_message: __('Placing the distributor order(s)…'),
			callback: (r) => {
				const m = r.message || {};
				if ((m.released || []).length) {
					frappe.show_alert({ message: __('{0} distributor order(s) confirmed and queued for placement.', [m.released.length]), indicator: 'green' }, 6);
				}
				if ((m.failed || []).length) {
					frappe.msgprint({
						title: __('Some distributor orders were not placed'), indicator: 'orange',
						message: m.failed.map((f) => `${frappe.utils.escape_html(f.name)} — ${frappe.utils.escape_html(f.error)}`).join('<br>')
							+ `<p>${__('They stay in Draft — fix and place again.')}</p>`,
					});
				}
				this._load();
			},
		});
	}

	// GunBroker's own "payment received" flag is a click on their website, so it
	// is not what opens Dispose — this is. It records what was actually banked,
	// which is also what gets replayed onto a Payment Entry once the invoice
	// exists (a GunBroker order is paid long before it is invoiced, so there is
	// nothing to book the receipt against until the guns are disposed).
	// Dispose books the bound-book exit first and the invoice after; when the
	// invoice fails the guns are already out, so the retry is its own button.
	_createGbInvoice(row) {
		frappe.call({
			method: 'ffl_integrations.gunbroker.revenue.create_gb_invoice_now',
			args: { gunbroker_order: row.gunbroker_order },
			freeze: true, freeze_message: __('Creating invoice…'),
			callback: (r) => {
				const res = r.message || {};
				const posted = res.status === 'created' || res.status === 'exists';
				frappe.show_alert({
					message: posted
						? __('Invoice {0} posted — this order can now ship.', [res.sales_invoice])
						: __('Invoice {0} was left in Draft. Open it to see why.', [res.sales_invoice]),
					indicator: posted ? 'green' : 'orange',
				}, 7);
				this._load();
			},
		});
	}

	_markGbPaymentReceived(row) {
		const d = new frappe.ui.Dialog({
			title: __('Mark Payment Received — GunBroker #{0}', [row.gb_order_id]),
			fields: [
				{
					fieldtype: 'Select', fieldname: 'mode_of_payment',
					label: __('Payment Method'),
					options: 'GunBroker Check\nGunBroker Card',
					default: 'GunBroker Check', reqd: 1,
					description: __('Card is refused while no payment gateway is connected to the GunBroker account.'),
				},
				{
					fieldtype: 'Data', fieldname: 'reference_no',
					label: __('Cheque / Money Order Number'),
					mandatory_depends_on: "eval:doc.mode_of_payment=='GunBroker Check'",
					description: __('The only thing that ties this order to a line on the bank statement.'),
				},
				{
					fieldtype: 'Currency', fieldname: 'amount', label: __('Amount Banked'),
					default: row.order_total, reqd: 1,
					description: __('Order total: {0}', [format_currency(row.order_total, row.currency)]),
				},
				{
					fieldtype: 'Date', fieldname: 'received_on', label: __('Deposited On'),
					default: frappe.datetime.get_today(), reqd: 1,
					description: __('The day it went INTO the bank, not the day it arrived in the post.'),
				},
			],
			primary_action_label: __('Record'),
			primary_action: (values) => {
				frappe.call({
					method: 'ffl_integrations.gunbroker.fulfillment.mark_payment_received',
					args: { gunbroker_order: row.gunbroker_order, ...values },
					freeze: true, freeze_message: __('Recording…'),
					// No error branch: a server throw skips this callback and the
					// dialog stays open with what the operator typed still in it.
					callback: (r) => {
						if (r.message?.ok) {
							d.hide();
							frappe.show_alert({
								message: __('Payment recorded — this order can now be disposed.'),
								indicator: 'green',
							}, 7);
							this._load();
						}
					},
				});
			},
		});
		d.show();
	}

	async _recordPayment(row) {
		// Card on file: "Save this card" only when the server offers it for this customer
		// (no round trip when the store has it off).
		const cof = window.ffl && window.ffl.cardOnFile;
		const saveOffer = cof ? await cof.offer(row.customer) : null;
		const d = new frappe.ui.Dialog({
			title: __('Record Payment — {0}', [row.sales_invoice]),
			fields: [
				{
					fieldtype: 'Select', fieldname: 'mode', label: __('Payment Method'),
					options: 'Cash\nCredit Card\nZelle\nACH\nCheck/Money Order', default: 'Cash', reqd: 1,
				},
				// Shared desk definition (ffl_core.bundle window.ffl.tenderReference):
				// Zelle/ACH/Check/Money Order always need their reference; the card tender
				// needs its approval code on a standalone terminal with the rule on,
				// or once "Already charged on Virtual Terminal" is ticked. Fallbacks =
				// older bundles (single field / the Zelle-only field).
				...((tr) => (tr && tr.fields ? tr.fields({ terminal: true }) : [tr ? tr.field() : {
					fieldtype: 'Data', fieldname: 'transaction_number', label: __('Zelle Transaction Number'),
					depends_on: "eval:doc.mode=='Zelle'", mandatory_depends_on: "eval:doc.mode=='Zelle'",
				}]))(window.ffl && window.ffl.tenderReference),
				{
					fieldtype: 'Currency', fieldname: 'amount', label: __('Amount'),
					default: row.outstanding_amount, reqd: 1,
					description: __('Outstanding: {0}', [format_currency(row.outstanding_amount)]),
				},
				...(saveOffer ? [Object.assign(cof.field(), {
					depends_on: "eval:doc.mode=='Credit Card' && !doc.charged_outside" })] : []),
				{
					// The day the money arrived, not the day it was typed in — a
					// payment logged late books on its real date. The server owns
					// the window (not before the invoice, never in the future).
					fieldtype: 'Date', fieldname: 'received_on', label: __('Payment received on'),
					default: frappe.datetime.get_today(), reqd: 1,
					description: __('Set it back if the money arrived on an earlier day.'),
				},
			],
			primary_action_label: __('Record Payment'),
			primary_action: async (v) => {
				// Card + processor live: run the terminal instead of typing a record —
				// the server books the Payment Entry on approval (same booking as
				// below, reference = the gateway's). The shared helper reports
				// declines / cancels itself; the queue reloads either way. Already
				// charged on the Virtual Terminal → typed record below, never the A80.
				const cardMop = window.ffl.switches.cardMop();
				const card = frappe.boot && frappe.boot.ffl_card_processor;
				if (v.mode === cardMop && Number(card && card.enabled)
						&& !v.charged_outside && window.ffl && window.ffl.chargeSalesInvoice) {
					const saveCard = saveOffer && v.save_card ? 1 : 0;
					d.hide();
					if (saveCard && !(await cof.ensureSigned(row.customer, saveOffer, row.customer_name))) {
						d.show();   // not signed: back to the dialog
						return;
					}
					const res = await window.ffl.chargeSalesInvoice(row.sales_invoice, flt(v.amount), { saveCard });
					if (res && res.skip) {
						frappe.msgprint({ title: __('Terminal not configured'), indicator: 'orange',
							message: __('The {0} device is not set up on this store. Take the card on the Virtual Terminal or your standalone terminal first, then tick "Already charged on Virtual Terminal" and enter its approval code.', [(card && card.label) || __('card processor')]) });
						d.show();
						return;
					}
					if (res && res.ok) {
						frappe.show_alert({
							message: res.paid
								? __('Card approved — {0} fully paid.', [row.sales_invoice])
								: __('Card approved — {0} still outstanding.', [format_currency(res.outstanding)]),
							indicator: 'green',
						}, 7);
					}
					this._load();
					return;
				}
				frappe.call({
					method: 'ffl_core.api.manual_order.record_payment',
					args: { sales_invoice: row.sales_invoice, mode_of_payment: v.mode, amount: v.amount, transaction_number: v.transaction_number || '', received_on: v.received_on || '' },
					freeze: true, freeze_message: __('Recording payment…'),
					callback: (r) => {
						if (r.message?.ok) {
							d.hide();
							frappe.show_alert({
								message: r.message.paid
									? __('Payment recorded — {0} fully paid.', [row.sales_invoice])
									: __('Payment recorded — {0} still outstanding.', [format_currency(r.message.outstanding)]),
								indicator: 'green',
							}, 7);
							this._load();
						}
					},
				});
			},
		});
		if (window.ffl && window.ffl.tenderReference) window.ffl.tenderReference.wire(d);
		d.show();
	}

	// Dispose — a deliberate second confirmation. Shows the customer,
	// destination FFL, and serial(s); the button stays disabled until the
	// operator ticks the "I've verified…" checkbox. Shared by both sources;
	// the backend method is chosen by row.source.
	// In-store pickup. For a firearm this opens an already-paid DRAFT counter
	// order and routes straight to it, because the next step is the 4473 and the
	// only place to run one is that document. No confirmation dialog: nothing
	// irreversible happens — no bound-book entry, no stock movement, no charge —
	// and a second click returns the same draft rather than a second one.
	// An order with no firearm has no 4473 to run, so it just closes here.
	_pickup(row) {
		// Two sources, one button: a web pickup closes or opens its counter
		// invoice in ffl_woo_sync; a counter pickup opens its 4473 ticket in
		// ffl_core. Both answer {ok, pos_invoice?} the same way.
		const counter = (KIND[row.source] || 'counter') === 'counter';
		frappe.call({
			method: counter
				? 'ffl_core.api.counter_pickup.open_pickup_invoice'
				: 'ffl_woo_sync.woocommerce.pickup.pickup_web_order',
			args: counter
				? { sales_invoice: row.sales_invoice }
				: { woo_online_order: row.woo_online_order },
			freeze: true,
			freeze_message: __('Opening counter order…'),
			callback: (r) => {
				if (!r.message?.ok) return;
				if (r.message.pos_invoice) {
					frappe.set_route('Form', 'POS Invoice', r.message.pos_invoice);
					return;
				}
				frappe.show_alert({
					message: __('Picked up — order closed.'), indicator: 'green',
				}, 7);
				this._load();
			},
		});
	}

	// Dispose, step one: ask the server what loose stock the order carries, so the confirm
	// dialog can ask where it came from in the same single step. The answer rides on the
	// row as `stock_lines` for _confirmDispose to read.
	_startDispose(row) {
		const kind = KIND[row.source] || 'counter';
		// Unsigned agreements: read them again (the customer may have just signed),
		// so the dialog asks about exactly what is still missing.
		if (kind === 'counter' && (row.agreements_missing || []).length) {
			this._refreshAgreements(row, { freeze: true, freeze_message: __('Checking the order…') })
				.then(() => this._confirmDispose(row));
			return;
		}
		const method = PD_STOCK_LINES_METHOD[kind];
		if (!method) return this._confirmDispose(row);
		frappe.call({
			method, args: { [DISPOSE_ARG[kind]]: row[DISPOSE_ARG[kind]] },
			freeze: true, freeze_message: __('Checking the order…'),
			callback: (r) => { row.stock_lines = r.message || []; this._confirmDispose(row); },
			// Tracking only: a failed lookup never stops the order shipping.
			error: () => { row.stock_lines = []; this._confirmDispose(row); },
		});
	}

	_confirmDispose(row) {
		const kind = KIND[row.source] || 'counter';
		const isWeb = kind === 'woo';
		const isGb = kind === 'gunbroker';
		const isCounter = kind === 'counter';
		const actionLabel = __('Dispose');
		const ref = isWeb ? __('order #{0}', [row.woo_order_id])
			: (isGb ? __('GunBroker #{0}', [row.gb_order_id]) : row.sales_invoice);
		const buyerType = isWeb
			? __('Web')
			: (isGb ? __('GunBroker')
				: (row.is_business ? __('Business (FFL)') : __('Individual')));
		const exp = row.ffl_expiry_date ? frappe.datetime.str_to_user(row.ffl_expiry_date) : __('unknown');
		const expStyle = row.ffl_expired ? ' style="color:var(--red-600);font-weight:600"' : '';
		// Item-level GunBroker lines sell a quantity of stock with no serial —
		// ammunition, optics, parts. They belong in the same list as the guns:
		// this is the operator's last look at what the order actually contains
		// before it is booked, and a box of ammunition missing from it reads as
		// an order that ships one thing while the invoice bills two.
		const serialItems = (row.pending_serials || []).map((s) =>
			`<li>${frappe.utils.escape_html(s.serial)}</li>`);
		const stockItems = (row.stock_units || []).map((u) =>
			`<li>${u.qty} &times; ${frappe.utils.escape_html(u.item_code)}</li>`);
		const units = serialItems.concat(stockItems);
		const serials = units.join('') || `<li>${__('(none)')}</li>`;
		const unitsLabel = (serialItems.length && stockItems.length) ? __('Sold')
			: (serialItems.length ? __('Serial(s)') : __('Item(s)'));
		const summary = `
			<div class="ffl-dispose-summary">
				<div><b>${__('Customer')}:</b> ${frappe.utils.escape_html(row.customer_name || row.customer)} (${buyerType})</div>
				${row.ships_direct === true
					? `<div style="margin-top:6px"><b>${__('Ships to')}:</b> ${__('the buyer directly — nothing here needs an FFL')}</div>`
					: `<div style="margin-top:6px"><b>${__('Ship to FFL')}:</b> ${frappe.utils.escape_html(row.destination_ffl_name || row.destination_ffl)}</div>
				<div class="text-muted" style="font-size:12px">
					${frappe.utils.escape_html(row.destination_ffl || '')} · ${__('exp')} <span${expStyle}>${exp}</span>
				</div>`}
				<div style="margin-top:8px"><b>${unitsLabel}:</b><ul>${serials}</ul></div>
				<div class="text-muted" style="margin-top:8px">
					${row.ships_direct === true
						? __('This takes the goods out of stock and raises the invoice. No bound-book entry, and nothing goes to FastBound.')
						: __('This books the bound-book transfer, removes the gun from stock, and pushes to FastBound.')}
				</div>
			</div>`;
		// A Type 03 (Curio & Relic) collector has no dealer at the far end and no
		// 4473 — this dialog IS the verification. The fields below are added only
		// for those rows; every other row keeps the dialog it has always had.
		const isCr = !isCounter && row.is_cr === true;
		// Per-invocation holder: the field `change` handlers are built before the
		// Dialog exists and need to reach it. Instance state would be overwritten
		// by the next row's dialog and leave the previous handlers pointing at it.
		const ctx = {};
		const locFields = pdLocationFields(row.stock_lines);
		// A counter order whose buyer has not signed what the sale needs: say so in
		// red, take an optional reason, and dispose with the override (the server
		// writes who, what and why on the invoice).
		const unsigned = isCounter ? (row.agreements_missing || []) : [];
		const d = new frappe.ui.Dialog({
			title: `${actionLabel} — ${ref}`,
			fields: [
				...(unsigned.length ? [
					{ fieldtype: 'HTML', fieldname: 'agreement_warning', options: `<div class="ffl-agreement-warning">${__('{0} has not signed {1}. Disposing without the signed acknowledgment is recorded on the order. Continue?', [frappe.utils.escape_html(row.customer_name || row.customer), pdAgreementNames(unsigned)])}</div>` },
					{ fieldtype: 'Small Text', fieldname: 'agreement_reason', label: __('Reason (optional)') },
				] : []),
				{ fieldtype: 'HTML', fieldname: 'summary', options: summary },
				...(locFields.length ? [{
					fieldtype: 'HTML', fieldname: 'stg_head',
					options: `<div style="font-weight:600;margin:6px 0 2px">${__('Where did these come from?')}</div>
						<div class="text-muted" style="font-size:12px">${__('Leave blank if you are not sure; it goes on the Not placed list.')}</div>`,
				}] : []),
				...locFields.map((f) => ({ fieldtype: 'Select', fieldname: f.fieldname, label: f.label,
					options: f.options, default: f.default })),
				...(isCr ? this._crFields(row, ctx) : []),
				{
					fieldtype: 'Check', fieldname: 'confirm',
					label: isCr
						? __("I have compared these details against the collector's C&R license")
						: __("I've verified the FFL and serial number(s)"),
					change: () => d.get_primary_btn().prop('disabled', !this._disposeGateOk(d, isCr)),
				},
			],
			primary_action_label: unsigned.length ? __('Dispose without signature') : actionLabel,
			primary_action: () => {
				if (!d.get_value('confirm')) return;
				if (isCr && !this._crGateOk(d)) return;
				const method = DISPOSE_METHOD[kind];
				const args = { [DISPOSE_ARG[kind]]: row[DISPOSE_ARG[kind]] };
				if (isCr) args.cr_details = this._crDetails(d);
				if (unsigned.length) {
					args.override_agreement = 1;
					args.reason = (d.get_value('agreement_reason') || '').trim();
				}
				const picks = pdCollectLocations(locFields, d.get_values(true) || {});
				if (Object.keys(picks).length) args.locations = JSON.stringify(picks);
				frappe.call({
					method, args,
					freeze: true, freeze_message: __('Disposing…'),
					// The error branch never closes the dialog: a server throw skips
					// this callback, so the dialog stays open with everything the
					// operator typed still in it. The server has already
					// committed the verification, so a retry re-prefills anyway.
					callback: (r) => {
						if (r.message?.ok) {
							d.hide();
							// GunBroker raises its invoice after the bound-book exit; a
							// failure there must not read as a clean Dispose.
							const inv = r.message.invoice_status;
							const invFailed = kind === 'gunbroker' && inv && inv !== 'created' && inv !== 'exists';
							frappe.show_alert({
								message: invFailed
									? __('Disposed {0} firearm(s), but the invoice did not post. Use Create Invoice on the card.', [r.message.disposed_count])
									: __('Disposed {0} firearm(s); FastBound push queued.', [r.message.disposed_count]),
								indicator: invFailed ? 'orange' : 'green',
							}, 7);
							this._load();
						}
					},
					// The dialog stays open (see above); only a counter row's agreements are
					// read again, so a signature that lapsed meanwhile shows up on the card
					// and the next Dispose asks about it.
					error: () => { if (isCounter) this._refreshAgreements(row); },
				});
			},
		});
		// _crFields() built its change handlers before `d` existed; give them the
		// dialog now, before the first keystroke can reach one.
		ctx.dialog = d;
		d.show();
		// Gate the action on the checkbox (and, for C&R, on the fields).
		d.get_primary_btn().prop('disabled', true);
		// Red, but still .btn-primary: the checkbox handlers look the button up by
		// that class on every change (see the front-end invariant test).
		if (unsigned.length) d.$wrapper.addClass('ffl-dispose-unsigned');
		// The C&R rows are split into short sections; drop the rules between them.
		if (isCr) d.$wrapper.addClass('ffl-dispose-cr');
	}

	// ---- C&R (Type 03) verification block -------------------------------------
	// Fourth segment of an ATF licence number is the type; 03 is the collector.
	// Kept byte-identical to the PHP/Python copies (contract C2).
	static get CR_LICENSE_RE() { return /^\d-\d{2}-\d{3}-03-[0-9A-Za-z]{2}-\d{5}$/; }

	// license_number is intentionally editable: the shopper typed it at checkout
	// and this is the first time a human compares it with the licence itself.
	_crFields(row, ctx) {
		const p = row.cr_prefill || {};
		const relabel = () => ctx.dialog && ctx.dialog.get_primary_btn()
			.prop('disabled', !this._disposeGateOk(ctx.dialog, true));
		const f = (o) => Object.assign({ change: relabel }, o);
		return [
			{
				fieldtype: 'HTML', fieldname: 'cr_header',
				options: `<hr><div style="font-weight:600;margin-bottom:4px">${__('C&R (Type 03) verification')}</div>
					<div class="text-muted" style="font-size:12px;margin-bottom:8px">
						${__('Enter these exactly as on the license; the parcel ships to this address, the licensed premises.')}
					</div>`,
			},
			// Short fields in pairs (license | expiry, address lines, city | state | ZIP);
			// the closing Section Break keeps the confirm checkbox below at full width.
			{ fieldtype: 'Section Break' },
			f({ fieldtype: 'Data', fieldname: 'cr_license_number', reqd: 1,
				label: __('C&R license number'), default: p.license_number || '',
				description: __('Fourth group must be 03 — e.g. 1-23-456-03-7C-89012') }),
			{ fieldtype: 'Column Break' },
			// Deliberately not pre-filled from a previous order: a licence that
			// was valid then may have lapsed since, so it is read fresh every time.
			f({ fieldtype: 'Date', fieldname: 'cr_license_expiry', reqd: 1,
				label: __('License expiration date'), default: p.license_expiry || '' }),
			{ fieldtype: 'Section Break' },
			f({ fieldtype: 'Data', fieldname: 'cr_contact_name', reqd: 1,
				label: __("Collector's full name"), default: p.contact_name || '' }),
			{ fieldtype: 'Section Break' },
			f({ fieldtype: 'Data', fieldname: 'cr_premise_street', reqd: 1,
				label: __('Address 1'), default: p.premise_street || '' }),
			{ fieldtype: 'Column Break' },
			f({ fieldtype: 'Data', fieldname: 'cr_premise_street2',
				label: __('Address 2'), default: p.premise_street2 || '' }),
			{ fieldtype: 'Section Break' },
			f({ fieldtype: 'Data', fieldname: 'cr_premise_city', reqd: 1,
				label: __('City'), default: p.premise_city || '' }),
			{ fieldtype: 'Column Break' },
			f({ fieldtype: 'Data', fieldname: 'cr_premise_state', reqd: 1,
				label: __('State'), default: p.premise_state || '' }),
			{ fieldtype: 'Column Break' },
			f({ fieldtype: 'Data', fieldname: 'cr_premise_zip', reqd: 1,
				label: __('ZIP'), default: p.premise_zip || '' }),
			{ fieldtype: 'Section Break' },
		];
	}

	_crDetails(d) {
		return {
			license_number: (d.get_value('cr_license_number') || '').trim(),
			license_expiry: d.get_value('cr_license_expiry') || '',
			contact_name: (d.get_value('cr_contact_name') || '').trim(),
			premise_street: (d.get_value('cr_premise_street') || '').trim(),
			premise_street2: (d.get_value('cr_premise_street2') || '').trim(),
			premise_city: (d.get_value('cr_premise_city') || '').trim(),
			premise_state: (d.get_value('cr_premise_state') || '').trim(),
			premise_zip: (d.get_value('cr_premise_zip') || '').trim(),
		};
	}

	// Every rule here is re-enforced server-side (cr_transfer._validate_cr_details).
	// This is the courtesy copy, not the gate — it can be bypassed by anyone who
	// can call frappe.call.
	_crGateOk(d) {
		const v = this._crDetails(d);
		const required = ['license_number', 'license_expiry', 'contact_name',
			'premise_street', 'premise_city', 'premise_state', 'premise_zip'];
		if (required.some((k) => !v[k])) return false;   // Address 2 is optional
		if (!PendingDispositionsApp.CR_LICENSE_RE.test(v.license_number)) return false;
		// ISO dates compare correctly as strings; a licence is valid THROUGH its
		// expiration date, so only an earlier date is expired.
		return v.license_expiry >= frappe.datetime.get_today();
	}

	// Whether Dispose may fire: the operator ticked the confirmation and, for a
	// C&R row, every verification field is present and well-formed. Returns a
	// bool rather than touching the button, so each caller keeps its own
	// get_primary_btn() lookup (see the front-end invariant test).
	_disposeGateOk(d, isCr) {
		return !!d.get_value('confirm') && (!isCr || this._crGateOk(d));
	}

	// Mark Shipped / Picked Up (no ShipStation) — the escape hatch for an
	// order that can't or shouldn't go through ShipStation: destination FFL
	// expired after dispose, integration misconfigured, label bought outside
	// ShipStation, or a direct order handed over at the counter. The server
	// also books the Delivery Note leg for deferred non-firearm stock lines
	// (stock + COGS) before stamping the row shipped. It does NOT require a
	// dispose (same as the ShipStation push) — when guns are still open the
	// confirm says so, and the row stays queued for them.
	_markShippedManually(row) {
		const undisposed = (row.pending_serials || []).length;
		frappe.confirm(
			undisposed
				? __('Clear the SHIPPING half of {0} without creating a ShipStation shipment? {1} firearm(s) on it still have no disposition — the bound book is NOT touched and the order stays on this queue until you Dispose. Deferred non-firearm stock is issued (Delivery Note) now.', [row.sales_invoice, undisposed])
				: __('Clear {0} from the queue without creating a ShipStation shipment? Deferred non-firearm stock is issued (Delivery Note) now. Use this when the goods were handed over or the label was handled outside ShipStation.', [row.sales_invoice]),
			() => this._doMarkShipped(row));
	}

	// THE mark-shipped call, shared by the card's button (behind the confirm
	// above) and the Ship dialog's secondary action (whose summary — every
	// item, the whole address — IS its confirmation). One frappe.call, so
	// the two entry points can't drift in what they send or how they settle
	// the page afterwards. `onOk` closes whatever surface invoked it.
	_doMarkShipped(row, onOk) {
		frappe.call({
			method: 'ffl_core.api.manual_order.mark_shipped_manually',
			args: { sales_invoice: row.sales_invoice },
			freeze: true, freeze_message: __('Marking shipped…'),
			callback: (r) => {
				if (r.message?.ok) {
					if (onOk) onOk();
					frappe.show_alert({
						message: __('{0} marked shipped.', [row.sales_invoice]),
						indicator: 'green',
					}, 5);
					this._load();
				}
			},
		});
	}

	// Ship — push the order to ShipStation so the label is bought there.
	// Counter (Sales Invoice) rows only; fully independent of Dispose. The
	// package is addressed to the CUSTOMER, care of the destination FFL.
	_confirmShip(row) {
		const esc = frappe.utils.escape_html;
		// A GunBroker order before Dispose has no invoice: it ships as itself.
		const isGb = KIND[row.source] === 'gunbroker';
		const shipRef = row.sales_invoice || (isGb ? __('GunBroker #{0}', [row.gb_order_id]) : '');
		// The no-ShipStation escape hatch rides along as the dialog's
		// secondary action — the clerk who opened Ship to hand the package
		// over shouldn't have to close this and hunt for the card button.
		// Offered only where the source may mark shipped at all (a GunBroker
		// sale reports its own tracking, so it never may), and dead — not
		// missing — while the server would refuse it, so the reason is
		// legible instead of the button just not being there.
		const offerMarkShipped = this._can(row).markShipped;
		// Payment is part of the gate HERE too: mark_shipped_manually is refused by
		// ship_payment_block exactly as push_shipment is, so an unpaid order must
		// not get a live secondary button that only earns a server error.
		const canMarkShipped = this._canMarkShipped(row) && !this._blockShipUnpaid(row);
		// Undisposed guns no longer block the mark — they change what it
		// MEANS, so say it here rather than behind a dead button: the
		// shipping half clears, the bound book does not, and the row stays
		// on the queue for its Dispose.
		const undisposed = (row.pending_serials || []).length;
		const p = row.ship_to_preview || {};
		const exp = row.ffl_expiry_date ? frappe.datetime.str_to_user(row.ffl_expiry_date) : __('unknown');
		const expStyle = row.ffl_expired ? ' style="color:var(--red-600);font-weight:600"' : '';
		const items = (row.ship_items || []).map((i) => {
			const sn = (i.serials || []).length
				? ` — SN: ${i.serials.map(esc).join(', ')}` : '';
			return `<li>${i.qty}× ${esc(i.item_name || i.item_code)}${sn}</li>`;
		}).join('') || `<li>${__('(none)')}</li>`;
		const cityLine = this._cityState(p) + (p.zip ? ` ${p.zip}` : '');
		// A direct order ships straight to the buyer: no c/o line, no FFL
		// footer — the address block alone is the whole destination.
		const coLine = (p.company || row.destination_ffl_name)
			? `<div><b>${__('c/o')}:</b> ${esc(p.company || row.destination_ffl_name || '')}</div>` : '';
		const fflLine = row.destination_ffl
			? `<br>${__('FFL')} ${esc(row.destination_ffl)} · ${__('exp')} <span${expStyle}>${exp}</span>` : '';
		const summary = `
			<div class="ffl-dispose-summary">
				<div><b>${__('Recipient')}:</b> ${esc(row.customer_name || row.customer)}</div>
				${coLine}
				<div class="text-muted" style="font-size:12px">
					${!p.line1 && row.destination_ffl ? __('Address comes from the FFL licence.') + '<br>' : ''}${esc(p.line1 || '')}${p.line2 ? ', ' + esc(p.line2) : ''}<br>
					${esc(cityLine)}${p.phone ? ' · ' + esc(p.phone) : ''}${fflLine}
				</div>
				<div style="margin-top:8px"><b>${__('Items')}:</b><ul>${items}</ul></div>
				<div class="text-muted" style="margin-top:8px">
					${__('Creates the shipment in ShipStation — pick the carrier and buy the label there. Does not dispose or touch the bound book.')}
				</div>
				${offerMarkShipped ? `<div class="text-muted" style="margin-top:8px">
					${!canMarkShipped
						? (this._blockShipUnpaid(row)
							? __('Mark Shipped (no ShipStation) is unavailable until the balance is recorded.')
							: __('Mark Shipped (no ShipStation) is unavailable: this order has firearm lines with no readable serial state. Fix the serials first.'))
						: (undisposed
							? __('Mark Shipped (no ShipStation) clears the SHIPPING half only — {0} firearm(s) here still have no disposition. The bound book is not touched, and the order stays on this queue until you Dispose.', [undisposed])
							: __('Mark Shipped (no ShipStation) clears the order from the queue without a label — deferred non-firearm stock is issued (Delivery Note) now. Use it when the goods were handed over or the label was bought outside ShipStation.'))}
				</div>` : ''}
			</div>`;
		const d = new frappe.ui.Dialog({
			title: __('Ship — {0}', [shipRef]),
			fields: [{ fieldtype: 'HTML', fieldname: 'summary', options: summary }],
			primary_action_label: __('Push to ShipStation'),
			// Same gate and same call as the card's button
			// (_canMarkShipped / _doMarkShipped), so the two entry points
			// can't drift. No nested frappe.confirm: this dialog's summary
			// already lays out recipient, address and every item, which is
			// more than the card's confirm ever showed.
			...(offerMarkShipped ? {
				secondary_action_label: __('Mark Shipped (no ShipStation)'),
				secondary_action: () => this._doMarkShipped(row, () => d.hide()),
			} : {}),
			primary_action: () => {
				frappe.call({
					...(isGb
						? { method: 'ffl_integrations.gunbroker.fulfillment.push_gb_shipment',
							args: { gunbroker_order: row.gunbroker_order } }
						: { method: 'ffl_integrations.shipstation.api.push_shipment',
							args: { sales_invoice: row.sales_invoice } }),
					freeze: true, freeze_message: __('Pushing to ShipStation…'),
					callback: (r) => {
						if (r.message?.ok) {
							d.hide();
							frappe.show_alert({
								message: r.message.already
									? __('{0} is already in ShipStation.', [shipRef])
									: __('Sent to ShipStation — shipment {0}.', [r.message.shipstation_order_id]),
								indicator: 'green',
							}, 7);
							this._load();
						}
					},
				});
			},
		});
		d.show();
		// A disabled button is the honest rendering of a call the server
		// (mark_shipped_block) would throw on — now only the unreadable-
		// serial anomaly, since undisposed guns are allowed through. The
		// summary above says why.
		if (offerMarkShipped && !canMarkShipped)
			d.get_secondary_btn().prop('disabled', true);
	}

	// Cancel Order — the full counter-order reversal: dispositions (bound
	// book + FastBound), stock, linked Sales Order, refund of what was paid,
	// best-effort ShipStation void. Deliberate confirmation like Dispose;
	// the backend recomputes every figure — the row only feeds the summary.
	//
	// get_primary_btn() is standard_actions.find('.btn-primary')
	// (frappe/public/js/frappe/ui/dialog.js:195). The btn-danger restyle below
	// makes every LATER get_primary_btn() an empty jQuery set, so the initial
	// gate and the checkbox `change` handler both .prop('disabled', …) on
	// nothing: the red button sat enabled and this cancel + refund lost its
	// visual second-confirmation gate (identical to the Void dialog's). Grab
	// the node once, after show(), and drive both through that handle.
	_confirmCancel(row) {
		const esc = frappe.utils.escape_html;
		const paid = (row.paid_total || 0) > 0.005;
		const will = [];
		if (row.disposed)
			will.push(__('Return the firearm(s) to stock and the FastBound book (undo any disposition).'));
		if (row.shipstation_order_id)
			will.push(__('Void ShipStation shipment {0}.', [esc(row.shipstation_order_id)]));
		if (paid)
			will.push(__('Record a {0} refund to the customer.', [format_currency(row.paid_total)]));
		(row.sales_orders || []).forEach((so) => will.push(__('Cancel Sales Order {0}.', [esc(so)])));
		will.push(__('Cancel invoice {0} — cancelled documents are kept as the audit trail.', [esc(row.sales_invoice)]));
		const serials = (row.pending_serials || []).map((s) =>
			`<li>${esc(s.serial)}</li>`).join('');
		const summary = `
			<div class="ffl-dispose-summary">
				<div><b>${__('Customer')}:</b> ${esc(row.customer_name || row.customer)}</div>
				${(row.pending_serials || []).length
					? `<div style="margin-top:6px"><b>${__('Serial(s)')}:</b><ul>${serials}</ul></div>` : ''}
				<div style="margin-top:6px"><b>${__('This will')}:</b><ul>${will.map((w) => `<li>${w}</li>`).join('')}</ul></div>
			</div>`;
		const fields = [
			{ fieldtype: 'HTML', fieldname: 'summary', options: summary },
			{ fieldtype: 'Small Text', fieldname: 'reason', label: __('Cancellation Reason'), reqd: 1 },
		];
		if (paid) {
			fields.push({
				fieldtype: 'Select', fieldname: 'refund_mode', label: __('Refund Method'),
				options: 'Cash\nCredit Card\nZelle\nACH\nCheck/Money Order',
				default: row.last_payment_mode || 'Cash', reqd: 1,
			});
			// Same modes the server's cancel_order demands a reference for — the
			// Zelle/ACH confirmation, or the number of the check written back.
			const needsRef = 'eval:' + JSON.stringify(window.ffl && window.ffl.tenderReference
				? window.ffl.tenderReference.modes() : ['Zelle', 'ACH', 'Check/Money Order'])
				+ '.includes(doc.refund_mode)';
			fields.push({
				fieldtype: 'Data', fieldname: 'refund_reference', label: __('Refund Reference'),
				description: __('The confirmation, check or approval number for the refund method.'),
				depends_on: needsRef, mandatory_depends_on: needsRef,
			});
		}
		let $primary = null;
		fields.push({
			fieldtype: 'Check', fieldname: 'confirm',
			label: paid
				? __('I understand this cancels the order and records a {0} refund', [format_currency(row.paid_total)])
				: __('I understand this cancels the order'),
			change: () => $primary.prop('disabled', !d.get_value('confirm')),
		});
		const d = new frappe.ui.Dialog({
			title: __('Cancel Order — {0}', [row.sales_invoice]),
			fields,
			primary_action_label: __('Cancel Order'),
			primary_action: (v) => {
				if (!d.get_value('confirm')) return;
				frappe.call({
					method: 'ffl_core.api.manual_order.cancel_order',
					args: {
						sales_invoice: row.sales_invoice,
						reason: v.reason,
						refund_mode: v.refund_mode || '',
						refund_reference: v.refund_reference || '',
					},
					freeze: true, freeze_message: __('Cancelling…'),
					callback: (r) => {
						const m = r.message || {};
						if (!m.ok) return;
						d.hide();
						frappe.show_alert({
							message: m.refunded_amount
								? __('Order cancelled — {0} refund recorded.', [format_currency(m.refunded_amount)])
								: __('Order cancelled.'),
							indicator: 'green',
						}, 7);
						if (m.warning) {
							frappe.msgprint({ title: __('ShipStation'), indicator: 'red', message: m.warning });
						}
						this._load();
					},
				});
			},
		});
		d.show();
		$primary = d.get_primary_btn();
		$primary.removeClass('btn-primary').addClass('btn-danger');
		$primary.prop('disabled', true);
	}
}

// Node unit tests import the pure kernel (no DOM / Frappe). Harmless in the
// browser, where `module` is undefined.
if (typeof module !== 'undefined' && module.exports) {
	module.exports = { PD_STAGE_ORDER, PD_PRIMARY, pdMatchesFilter, pdMatchesQuery, pdSections,
		pdLocationFields, pdCollectLocations, pdEscape, pdAgreementKind, pdAgreementNames };
}
