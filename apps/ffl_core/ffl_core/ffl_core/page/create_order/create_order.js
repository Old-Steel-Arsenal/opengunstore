// Create Order — manual build-and-ship order for firearms mailed to a
// destination FFL (the buyer does the 4473 there, not at our counter).
//
// Flow (top-to-bottom = the real counter workflow):
//   0. Buyer type: Individual or Business (FFL).
//   1. Buyer: pick/create the Customer; Business buyers ARE an FFL.
//   2. Destination FFL (when firearms are on the order): where the guns ship.
//   3. Recipient & Ship-To: locked to the FFL address for firearm orders.
//   4. Items: scan/search; firearms pick an in-stock serial (price-first).
//   5. Payment: Cash / Credit Card / Zelle / ACH / Check/Money Order + a "received" toggle.
//   6. Fulfillment: dispose & ship now, or queue for Pending Dispositions.
//
// "Create Order" → ffl_core.api.manual_order.create_order builds a submitted
// Sales Order → Sales Invoice; the receipt is emailed and (when shipping now)
// one FFL/Dealer Transfer disposition is booked per serial.

frappe.pages['create-order'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('Create Order'),
		single_column: true,
	});
	wrapper.create_order_app = new CreateOrderApp(page);
};

// Route /app/create-order/edit/<SI> enters edit mode; plain /app/create-order
// leaves it. on_page_show fires on every visit INCLUDING a browser refresh,
// so a refreshed edit page re-fetches and stays in edit mode — a stale form
// can never silently double-create.
frappe.pages['create-order'].on_page_show = function (wrapper) {
	wrapper.create_order_app?._syncEditRoute();
};


class CreateOrderApp {
	// One table drives BOTH draft serialization (_collectPayload) and
	// hydration (_loadDraft) for the flat scalar fields — register a new form
	// field here once and it round-trips automatically. `key` is the
	// create_order payload name, `field` the page control name.
	static DRAFT_SCALARS = [
		{ key: 'mode_of_payment', field: 'mode', dflt: 'Cash' },
		// payment_received itself is DERIVED in _collectPayload (pay now unless
		// Pay later); these two are the controls the operator actually touches.
		{ key: 'pay_later', field: 'pay_later', type: 'check' },
		{ key: 'charge_terminal', field: 'charge_terminal', type: 'check' },
		{ key: 'paid_amount', field: 'amount', type: 'flt' },
		{ key: 'transaction_number', field: 'transaction_number' },
		{ key: 'shipping_amount', field: 'shipping', type: 'flt' },
		{ key: 'contact_email', field: 'email' },
		{ key: 'recipient_name', field: 'recipient_name' },
		{ key: 'recipient_phone', field: 'recipient_phone' },
		{ key: 'notes', field: 'note' },
		{ key: 'pickup_in_store', field: 'pickup', type: 'check' },
	];

	static SHIP_TO_FIELDS = [
		['address_line1', 'ship_line1'],
		['address_line2', 'ship_line2'],
		['city', 'ship_city'],
		['state', 'ship_state'],
		['pincode', 'ship_zip'],
	];

	constructor(page) {
		this.page = page;
		this.lines = [];
		this.pending = null;
		this.buyerType = 'individual';
		this.destinationFfl = null;
		// Signature / adult signature / insurance per distributor order, keyed
		// "house|order type|firearm" as fulfillment_freight names each order.
		this.deliveryOptions = {};
		this.$body = $(`
			<div class="ffl-build">
				<style>
					.ffl-build { max-width: 900px; margin: 0 auto; padding: 4px 0 90px; }
					.ffl-build .section {
						background: var(--card-bg); border: 1px solid var(--border-color);
						border-radius: 8px; padding: 18px 20px; margin-bottom: 14px;
					}
					.ffl-build .section.muted { opacity: 0.55; }
					.ffl-build .section h3 { margin: 0 0 14px 0; font-size: 15px; font-weight: 600; }
					.ffl-build .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
					.ffl-build .row.cols-3 { grid-template-columns: 2fr 1fr 1fr; }
					.ffl-build .ffl-row { display: flex; gap: 8px; align-items: flex-end; }
					.ffl-build .ffl-row .grow { flex: 1; }
					/* Frappe controls add a bottom margin + empty help-box; zero them so
					   the buttons line up with the input box, not below it. */
					.ffl-build .ffl-row .frappe-control,
					.ffl-build .ffl-row .form-group { margin-bottom: 0; }
					.ffl-build .ffl-row .help-box { display: none; }
					.ffl-build .ffl-row .btn { margin-bottom: 1px; }
					.ffl-build .lines {
						margin-top: 12px; border: 1px solid var(--border-color);
						border-radius: 6px; overflow-x: auto;
					}
					/* A phone scrolls the table inside its box rather than crushing the Item column. */
					.ffl-build .lines table { width: 100%; min-width: 560px; border-collapse: collapse; }
					.ffl-build .lines th, .ffl-build .lines td {
						padding: 8px 10px; border-bottom: 1px solid var(--border-color);
						font-size: 13px; vertical-align: top;
					}
					.ffl-build .lines th { background: var(--bg-light-gray); text-align: left; font-weight: 600; }
					.ffl-build .lines tr:last-child td { border-bottom: none; }
					.ffl-build .lines .firearm-mark, .plan-grid .firearm-mark {
						display: inline-block; background: var(--blue-100); color: var(--blue-700);
						padding: 1px 6px; border-radius: 4px; font-size: 11px; margin-right: 6px;
					}
					.ffl-build .lines .dropship-mark {
						display: inline-block; background: var(--orange-100); color: var(--orange-700);
						padding: 1px 6px; border-radius: 4px; font-size: 11px; margin-right: 6px;
					}
					.ffl-build .lines .serials { color: var(--text-muted); font-size: 11px; margin-top: 2px; }
					.ffl-build .lines .right { text-align: right; white-space: nowrap; }
					.ffl-build .pending {
						background: var(--highlight-color); border: 1px dashed var(--blue-300);
						border-radius: 6px; padding: 14px; margin-top: 12px;
					}
					.ffl-build .pending .row { margin-bottom: 8px; }
					.ffl-build .total-bar {
						display: flex; justify-content: space-between; align-items: center;
						padding: 12px 0; font-size: 14px; font-weight: 600;
						border-top: 1px solid var(--border-color); margin-top: 10px;
					}
					.ffl-build .total-bar .total { font-size: 18px; }
					.ffl-build .submit-bar {
						position: sticky; bottom: 0; background: var(--card-bg);
						border-top: 1px solid var(--border-color); padding: 12px 0; margin-top: 18px;
						display: flex; justify-content: space-between; align-items: center; gap: 12px;
					}
					.ffl-build .submit-left {
						display: flex; flex-direction: column; align-items: flex-start; gap: 4px;
						min-width: 0;
					}
					.ffl-build .submit-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
					.ffl-build .drafts-link {
						background: none; border: none; padding: 0; cursor: pointer;
						display: inline-flex; align-items: center; gap: 5px;
						font-size: 12px; color: var(--text-muted);
					}
					.ffl-build .drafts-link:hover { color: var(--blue-500); }
					.ffl-build .drafts-count {
						min-width: 17px; padding: 0 5px; border-radius: 9px; text-align: center;
						background: var(--control-bg); color: var(--text-color);
						font-size: 11px; font-weight: 600; line-height: 17px;
					}
					.ffl-build .drafts-link:hover .drafts-count { background: var(--blue-100); color: var(--blue-600); }
					.ffl-build .line-rate, .ffl-build .line-qty {
						width: 90px; text-align: right; padding: 3px 6px;
						border: 1px solid var(--border-color); border-radius: 5px;
						background: var(--control-bg); color: inherit; font: inherit;
					}
					.ffl-build .line-qty { width: 56px; }
					.ffl-build .line-rate:focus, .ffl-build .line-qty:focus { border-color: var(--blue-300); outline: none; }
					.ffl-build .helper { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
					.ffl-build .btn-remove { background: none; border: none; color: var(--red-500); cursor: pointer; padding: 0 4px; }
					.ffl-build .scan-wrap { position: relative; }
					.ffl-build .typeahead {
						position: absolute; top: 100%; left: 0; right: 0; z-index: 100;
						background: var(--card-bg); border: 1px solid var(--border-color);
						border-radius: 6px; box-shadow: 0 8px 24px rgba(0,0,0,0.08);
						max-height: 320px; overflow-y: auto; margin-top: 4px; display: none;
					}
					.ffl-build .seg { display: inline-flex; border: 1px solid var(--border-color); border-radius: 6px; overflow: hidden; }
					.ffl-build .seg button {
						border: none; background: var(--card-bg); padding: 7px 16px; cursor: pointer;
						font-size: 13px; color: var(--text-color);
					}
					/* FE-B2 (b), 2026-07-14: active segment = Frappe native btn-primary
					   (var(--btn-primary)/var(--neutral)), not --blue-500 — matches this
					   page's primary buttons. Kept in sync with ship_consignment's two seg
					   blocks (family-wide, per lead). */
					.ffl-build .seg button.active { background: var(--btn-primary); color: var(--neutral); }
					.ffl-build .seg button + button { border-left: 1px solid var(--border-color); }
					.ffl-build .ffl-card {
						margin-top: 12px; padding: 12px 14px; border: 1px solid var(--border-color);
						border-radius: 6px; background: var(--bg-light-gray); font-size: 13px;
					}
					.ffl-build .ffl-card .lic { font-weight: 600; }
					.ffl-build .badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px; margin-left: 6px; }
					.ffl-build .badge.ok { background: var(--bg-green); color: var(--green-700); }
					.ffl-build .badge.warn { background: var(--bg-red); color: var(--red-700); }
				</style>

				<!-- 0. BUYER TYPE -->
				<div class="section">
					<h3>${__('Buyer Type')}</h3>
					<div class="seg" id="seg-buyer">
						<button data-v="individual" class="active">${__('Individual')}</button>
						<button data-v="business">${__('Business (FFL)')}</button>
					</div>
					<div class="helper">${__('Business buyers are themselves an FFL — the guns ship to their license.')}</div>
				</div>

				<!-- 1. BUYER -->
				<div class="section">
					<h3>1. ${__('Buyer')}</h3>
					<div id="buyer-individual">
						<div class="row">
							<div class="ffl-row">
								<div class="grow" id="f-customer"></div>
								<button class="btn btn-default" id="btn-rename-cust" title="${__('Fix customer name spelling')}">✎</button>
							</div>
							<div id="f-email"></div>
						</div>
						<div class="row" style="margin-top:12px">
							<div id="f-phone"></div>
							<div></div>
						</div>
					</div>
					<div id="buyer-business" style="display:none">
						<div class="ffl-row">
							<div class="grow" id="f-biz_ffl"></div>
							<button class="btn btn-default btn-sm" id="btn-verify-biz">${__('Verify FFL')}</button>
							<button class="btn btn-default btn-sm" id="btn-lookup-biz">${__('Lookup by FFL #')}</button>
						</div>
						<div id="biz-ffl-notice" style="margin-top:6px"></div>
						<div id="biz-ffl-card"></div>
						<div id="biz-tax" style="display:none; margin-top:12px">
							<div id="f-tax_exempt"></div>
							<div id="tax-cert-area" class="helper" style="margin-top:4px"></div>
						</div>
					</div>
					<div id="le-exempt" style="display:none; margin-top:12px">
						<div id="f-le_excise_exempt"></div>
						<div id="le-exempt-detail" style="display:none">
							<div id="f-le_exempt_buyer"></div>
							<div id="le-cert-area" class="helper" style="margin-top:4px"></div>
						</div>
					</div>
				</div>

				<!-- 2. DESTINATION FFL -->
				<div class="section" id="sec-dest" style="display:none">
					<h3>2. ${__('Destination FFL')}</h3>
					<div id="f-cr_transfer"></div>
					<div id="dest-standard">
						<div class="ffl-row">
							<div class="grow" id="f-dest_ffl"></div>
							<button class="btn btn-default btn-sm" id="btn-verify-dest">${__('Verify FFL')}</button>
							<button class="btn btn-default btn-sm" id="btn-lookup-dest">${__('Lookup by FFL #')}</button>
						</div>
					</div>
					<div id="dest-cr" style="display:none">
						<div class="helper">${__('The ATF directory holds no Type 03 collectors, so nothing can be looked up — read the license copy and type what it says. The gun ships to the licensed premises.')}</div>
						<div class="row" style="margin-top:12px">
							<div id="f-cr_license_number"></div>
							<div id="f-cr_license_expiry"></div>
						</div>
						<div class="row" style="margin-top:12px">
							<div id="f-cr_contact_name"></div>
							<div></div>
						</div>
						<div class="row" style="margin-top:12px">
							<div id="f-cr_premise_street"></div>
							<div id="f-cr_premise_street2"></div>
						</div>
						<div class="row cols-3" style="margin-top:12px">
							<div id="f-cr_premise_city"></div>
							<div id="f-cr_premise_state"></div>
							<div id="f-cr_premise_zip"></div>
						</div>
						<div style="margin-top:12px">
							<button class="btn btn-default btn-sm" id="btn-apply-cr">${__('Use this C&R license')}</button>
						</div>
					</div>
					<div id="dest-ffl-card"></div>
				</div>

				<!-- 3. RECIPIENT & SHIP-TO -->
				<div class="section">
					<h3>3. ${__('Recipient & Ship-To')}</h3>
					<div id="f-pickup"></div>
					<div class="row">
						<div id="f-recipient_name"></div>
						<div id="f-recipient_phone"></div>
					</div>
					<div class="row" style="margin-top:12px">
						<div id="f-ship_line1"></div>
						<div id="f-ship_line2"></div>
					</div>
					<div class="row cols-3" style="margin-top:12px">
						<div id="f-ship_city"></div>
						<div id="f-ship_state"></div>
						<div id="f-ship_zip"></div>
					</div>
					<div class="helper" id="shipto-note"></div>
				</div>

				<!-- 4. ITEMS -->
				<div class="section">
					<h3>4. ${__('Items')}</h3>
					<div class="scan-wrap">
						<div id="f-scan"></div>
						<div class="typeahead" id="typeahead"></div>
					</div>
					<div class="helper">${__('Scan barcode, type SKU / item name — or scan a firearm serial # to add that gun, its serial, and its tag price in one step. Use ↑↓ + Enter to pick. Store stock and distributor drop-ship results appear together; picking a drop-ship result quotes it below.')}</div>
					<div id="pending-area"></div>
					<div class="lines" id="lines-area" style="display:none">
						<table>
							<thead><tr>
								<th>${__('Item')}</th>
								<th class="right">${__('Qty')}</th>
								<th class="right">${__('Cost')}</th>
								<th class="right">${__('Price')}</th>
								<th class="right">${__('Subtotal')}</th>
								<th></th>
							</tr></thead>
							<tbody id="lines-tbody"></tbody>
						</table>
					</div>
					<div class="total-bar">
						<div>${__('Lines')}: <span id="line-count">0</span>
							<a href="#" id="btn-plan" style="margin-left:10px; font-size:12px; font-weight:400">${__('Plan fulfillment')}</a></div>
						<div style="text-align:right">
							<div id="f-shipping" style="max-width:220px; margin:0 0 6px auto"></div>
							<div class="helper" id="tax-line" style="margin:0 0 2px"></div>
							<div class="total">${__('Total')}: <span id="total">0.00</span></div>
							<div class="helper" style="display:flex; gap:6px; justify-content:flex-end; align-items:center; margin-top:4px">
								<label for="target-total" style="margin:0">${__('Charge exactly')}</label>
								<input type="number" id="target-total" class="form-control input-sm" min="0" step="0.01"
									style="width:120px; text-align:right" placeholder="${__('total incl. tax')}"
									title="${__('Type the total the customer pays; line prices are worked back from it before tax.')}">
							</div>
						</div>
					</div>
				</div>

				<!-- 5. PAYMENT -->
				<div class="section">
					<h3>5. ${__('Payment')}</h3>
					<div class="row">
						<div id="f-mode"></div>
						<div id="f-amount"></div>
					</div>
					<div class="row" style="margin-top:12px">
						<div id="f-charge-terminal"></div>
						<div id="f-pay-later"></div>
					</div>
					<div class="helper" id="deposit-note" style="margin-top:4px"></div>
					<div class="helper" id="terminal-note" style="margin-top:4px;display:none"></div>
					<div class="row" id="f-txn-row" style="margin-top:12px;display:none"><div id="f-txn"></div></div>
				</div>

				<!-- 6. FULFILLMENT -->
				<div class="section">
					<h3>6. ${__('Fulfillment')}</h3>
					<div class="seg" id="seg-dispose">
						<button data-v="later" class="active">${__('Create only — dispose later')}</button>
						<button data-v="now">${__('Dispose & ship now')}</button>
					</div>
					<div class="helper" id="dispose-note">${__('Defer: the order joins the Pending Dispositions queue; the gun stays in stock and on the bound book until you ship it.')}</div>
					<div class="helper" id="legs-preview" style="display:none; margin-top:8px"></div>
						<div style="margin-top:12px" id="f-note"></div>
				</div>

				<div class="submit-bar">
					<div class="submit-left">
						<div id="readiness" class="helper"></div>
						<button class="drafts-link" id="btn-drafts" type="button" title="${__('Open saved drafts')}">
							<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M2 3.5h4l1.5 2H14v7H2z"/></svg>
							${__('Drafts')}<span class="drafts-count" id="drafts-count" style="display:none"></span>
						</button>
					</div>
					<div class="submit-actions">
						<button class="btn btn-default btn-lg" id="btn-save-draft">${__('Save Draft')}</button>
						<button class="btn btn-primary btn-lg" id="btn-submit" disabled>${__('Create Order')}</button>
					</div>
				</div>
			</div>
		`).appendTo(this.page.body);

		this._buildFields();
		// make_control does not apply df.default to a fresh Check — set it.
		this.fields.charge_terminal.set_value(1);
		this._syncPaymentFields();
		this._wireSegments();
		this._wireKeyboard();
		this._fflSearchNotice();
		this._loadTaxConfig();
		this._reflectReadiness();
	}

	// The FFL field searches this store's ATF FFL Records; with national search off
	// (frappe.boot.ffl_capabilities) the shared notice says so under it.
	_fflSearchNotice() {
		const off = window.ffl && ffl.capabilities && ffl.capabilities.isOff('ffl_national_search');
		this.$body.find('#biz-ffl-notice').html(off ? ffl.capabilities.notice('ffl_national_search') : '');
	}

	_f(parent, opts) {
		const ctrl = frappe.ui.form.make_control({
			parent: this.$body.find(parent)[0], df: opts, render_input: true,
		});
		ctrl.refresh();
		return ctrl;
	}

	_buildFields() {
		const rr = () => this._reflectReadiness();
		this.fields = {
			customer: this._f('#f-customer', {
				label: __('Customer'), fieldtype: 'Link', options: 'Customer', fieldname: 'customer',
				get_query: () => ({ filters: { is_ffl_dealer: 0 } }),
				change: () => this._onCustomerChange(),
			}),
			email: this._f('#f-email', { label: __('Receipt Email'), fieldtype: 'Data', options: 'Email', fieldname: 'email', change: rr }),
			phone: this._f('#f-phone', { label: __('Phone'), fieldtype: 'Data', fieldname: 'phone' }),
			biz_ffl: this._f('#f-biz_ffl', {
				label: __('FFL (ATF Record)'), fieldtype: 'Link', options: 'ATF FFL Record', fieldname: 'biz_ffl',
				change: () => this._onBizFflChange(),
			}),
			tax_exempt: this._f('#f-tax_exempt', {
				label: __('Tax Exempt (resale/exemption certificate on file)'),
				fieldtype: 'Check', fieldname: 'tax_exempt',
				change: () => this._onTaxExemptChange(),
			}),
			le_excise_exempt: this._f('#f-le_excise_exempt', {
				label: __('Peace officer / law enforcement agency — excise tax exempt'),
				description: __('Active or retired peace officer, or the agency employing one, with a CDTFA-230-FET certificate for this sale. Takes off the excise tax only; sales tax stays.'),
				fieldtype: 'Check', fieldname: 'le_excise_exempt',
				change: () => this._onLeExemptChange(),
			}),
			le_exempt_buyer: this._f('#f-le_exempt_buyer', {
				label: __('Exempt buyer'), fieldtype: 'Select', fieldname: 'le_exempt_buyer',
				options: ['', 'Active Peace Officer', 'Retired Peace Officer', 'Law Enforcement Agency'].join('\n'),
				change: () => this._reflectReadiness(),
			}),
			dest_ffl: this._f('#f-dest_ffl', {
				label: __('Destination FFL'), fieldtype: 'Link', options: 'ATF FFL Record', fieldname: 'dest_ffl',
				change: () => this._onDestFflChange(),
			}),
			cr_transfer: this._f('#f-cr_transfer', {
				label: __('C&R (Type 03) — ships to the collector\'s own license'),
				fieldtype: 'Check', fieldname: 'cr_transfer',
				change: () => this._onCrToggle(),
			}),
			cr_license_number: this._f('#f-cr_license_number', {
				label: __('C&R License Number'), fieldtype: 'Data', fieldname: 'cr_license_number',
				description: __('As printed: 1-23-456-03-7C-89012'),
				change: () => this._prefillFromCrRecord(),
			}),
			cr_license_expiry: this._f('#f-cr_license_expiry', { label: __('License Expires'), fieldtype: 'Date', fieldname: 'cr_license_expiry' }),
			cr_contact_name: this._f('#f-cr_contact_name', { label: __('Collector Name'), fieldtype: 'Data', fieldname: 'cr_contact_name' }),
			cr_premise_street: this._f('#f-cr_premise_street', { label: __('Licensed Premises Address'), fieldtype: 'Data', fieldname: 'cr_premise_street' }),
			cr_premise_street2: this._f('#f-cr_premise_street2', { label: __('Address 2'), fieldtype: 'Data', fieldname: 'cr_premise_street2' }),
			cr_premise_city: this._f('#f-cr_premise_city', { label: __('City'), fieldtype: 'Data', fieldname: 'cr_premise_city' }),
			cr_premise_state: this._f('#f-cr_premise_state', { label: __('State'), fieldtype: 'Data', fieldname: 'cr_premise_state' }),
			cr_premise_zip: this._f('#f-cr_premise_zip', { label: __('ZIP'), fieldtype: 'Data', fieldname: 'cr_premise_zip' }),
			pickup: this._f('#f-pickup', {
				label: __('Pickup in store — the customer collects at the counter'),
				fieldtype: 'Check', fieldname: 'pickup',
				change: () => this._onPickupChange(),
			}),
			recipient_name: this._f('#f-recipient_name', { label: __('Recipient Name'), fieldtype: 'Data', fieldname: 'recipient_name' }),
			recipient_phone: this._f('#f-recipient_phone', { label: __('Recipient Phone'), fieldtype: 'Data', fieldname: 'recipient_phone' }),
			ship_line1: this._f('#f-ship_line1', { label: __('Street Address'), fieldtype: 'Data', fieldname: 'ship_line1' }),
			ship_line2: this._f('#f-ship_line2', { label: __('Street Address 2'), fieldtype: 'Data', fieldname: 'ship_line2' }),
			ship_city: this._f('#f-ship_city', { label: __('City'), fieldtype: 'Data', fieldname: 'ship_city' }),
			ship_state: this._f('#f-ship_state', { label: __('State'), fieldtype: 'Data', fieldname: 'ship_state', change: () => this._updateTotals() }),
			ship_zip: this._f('#f-ship_zip', { label: __('ZIP'), fieldtype: 'Data', fieldname: 'ship_zip' }),
			scan: this._f('#f-scan', { label: __('Add Item'), fieldtype: 'Data', fieldname: 'scan' }),
			mode: this._f('#f-mode', {
				label: __('Payment Method'), fieldtype: 'Select', fieldname: 'mode',
				change: () => this._syncPaymentFields(),
				options: 'Cash\nCredit Card\nZelle\nACH\nCheck/Money Order', default: 'Cash',
			}),
			amount: this._f('#f-amount', { label: __('Amount'), fieldtype: 'Currency', fieldname: 'amount', change: () => {
				// Frappe's Currency change fires async, so a synchronous flag can't tell
				// our own set_value apart from a user edit. Compare against the last value
				// we auto-filled instead, with a cent of tolerance so the control's 2dp
				// rounding of a higher-precision grand total isn't mistaken for a user edit.
				if (Math.abs(flt(this.fields.amount.get_value()) - flt(this._lastAutoAmount)) > 0.005) this._amountEdited = true;
				this._syncDepositNote();
			} }),
			shipping: this._f('#f-shipping', {
				label: __('Shipping'), fieldtype: 'Currency', fieldname: 'shipping',
				change: () => this._updateTotals(),
			}),
			// Payment is collected NOW with the method above unless Pay later is
			// ticked. Credit Card on a card-processor store adds Charge via Terminal (on by
			// default): the card runs on the A80 right after the order books;
			// unticked, the card was taken outside the integration and is only recorded.
			charge_terminal: this._f('#f-charge-terminal', {
				label: __('Charge via Terminal'), fieldtype: 'Check', fieldname: 'charge_terminal',
				default: 1, change: () => this._syncPaymentFields(),
			}),
			pay_later: this._f('#f-pay-later', {
				label: __('Pay later'), fieldtype: 'Check', fieldname: 'pay_later',
				description: __('No payment now — the order books on account; collect it from Pending Order.'),
				change: () => this._syncPaymentFields(),
			}),
			note: this._f('#f-note', {
				label: __('Order Note (staff)'), fieldtype: 'Small Text', fieldname: 'note',
				description: __('Shown on the Pending Order queue and saved on the invoice.'),
			}),
			transaction_number: this._f('#f-txn', {
				label: __('Transaction Number'), fieldtype: 'Data', fieldname: 'transaction_number',
				change: rr,
			}),
		};
	}

	// Which manually-settled tender, if any, the current mode is — and therefore
	// what reference the server will demand at submit. Both gates live in
	// ffl_core.compliance.payment_mode; this is the desk half so the page can never
	// build an order its own backend refuses (there would be nowhere to fix it).
	//
	// Only when collecting payment NOW: deferred (on-account) orders capture the
	// reference later, at Record Payment.
	_payLater() {
		return !!this.fields.pay_later.get_value();
	}

	_tenderReference() {
		// Nothing is collected now, or the terminal returns the approval itself.
		if (this._payLater() || this._terminalPath()) return null;
		const mode = this.fields.mode.get_value();
		// The server's _REFERENCED_BANK_MODES: each always needs its number.
		if (mode === 'Zelle' || mode === 'ACH') {
			return {
				label: __('{0} Transaction Number', [mode]),
				missing: __('{0} Transaction Number', [mode]),
			};
		}
		if (mode === 'Check/Money Order') {
			return {
				label: __('Check / money order number'),
				missing: __('Check / money order number'),
			};
		}
		// Credit Card needs one on a store running the standalone terminal with the
		// requirement switched on — and with a card processor on but "Charge via Terminal"
		// unticked, where the card was taken on the Virtual Terminal (the terminal
		// path returned above: its code comes back from the device). frappe.boot is
		// the same source the POS bundle reads, and an ABSENT key means a server
		// older than the switch.
		const need = frappe.boot && frappe.boot.ffl_require_card_reference;
		if (mode === (frappe.boot && frappe.boot.card_mop) && (Number(need) || this._cardOnTerminal())) {
			return {
				label: __('Card Approval Code'),
				missing: __('Card approval code'),
			};
		}
		return null;
	}

	_needsZelleTxn() {
		return !!this._tenderReference();
	}

	// Card + processor live: the card is charged on the terminal right after the order
	// books (never money without an invoice), so the server sees an on-account
	// order and the page runs the terminal against the new Sales Invoice. The
	// amount typed is what goes on the terminal — less than the total is a
	// deposit. With no processor on the card stays a standalone-terminal record.
	_cardOnTerminal() {
		return this.fields.mode.get_value() === ((frappe.boot && frappe.boot.card_mop) || 'Credit Card')
			&& !!Number(frappe.boot && frappe.boot.ffl_card_processor && frappe.boot.ffl_card_processor.enabled);
	}

	_terminalPath() {
		return !this._payLater() && this._cardOnTerminal() && !!this.fields.charge_terminal.get_value();
	}

	_syncTerminalNote() {
		const $n = this.$body.find('#terminal-note');
		if (!this._terminalPath()) { $n.hide().empty(); return; }
		const dispose = this.disposeNow
			? ` ${__('Dispose & Ship Now is skipped for a terminal charge — the order ships from Pending Order once the card is approved.')}`
			: '';
		$n.show().html(`<span style="color:var(--blue-600,#1d4ed8)">${__('The card is charged on the terminal right after the order is created.')}${dispose}</span>`);
	}

	_syncPaymentFields() {
		// Show + require the reference precisely when needed, labelled for the tender
		// in play; clear it when hidden so a stale number is never submitted.
		const ref = this._tenderReference();
		const needTxn = !!ref;
		if (ref) this.fields.transaction_number.df.label = ref.label;
		this.fields.transaction_number.refresh();
		this.$body.find('#f-txn-row').toggle(needTxn);
		if (!needTxn) this.fields.transaction_number.set_value('');
		const payLater = this._payLater();
		this.$body.find('#f-charge-terminal').toggle(!payLater && this._cardOnTerminal());
		this.$body.find('#f-amount').toggle(!payLater);
		this._syncDepositNote();
		this._syncTerminalNote();
		this._reflectReadiness();
	}

	_wireSegments() {
		this.$body.find('#seg-buyer button').on('click', (e) => {
			const v = $(e.currentTarget).data('v');
			this.buyerType = v;
			this.$body.find('#seg-buyer button').removeClass('active');
			$(e.currentTarget).addClass('active');
			this.$body.find('#buyer-individual').toggle(v === 'individual');
			this.$body.find('#buyer-business').toggle(v === 'business');
			if (v === 'individual') {
				this._clearTaxExempt();
			} else {
				// Refresh the exemption + checkbox for the (maybe already-resolved)
				// business customer so the box matches their standing status.
				this._loadBizTaxStatus();
			}
			this._syncDestSection();
			this._reflectReadiness();
			this._updateTotals();
		});
		this.$body.find('#seg-dispose button').on('click', (e) => {
			const v = $(e.currentTarget).data('v');
			this.disposeNow = (v === 'now');
			this.$body.find('#seg-dispose button').removeClass('active');
			$(e.currentTarget).addClass('active');
			this.$body.find('#dispose-note').text(this.disposeNow
				? __('Ship now: book the transfer, take the gun out of stock, push to FastBound.')
				: __('Defer: the order joins the Pending Dispositions queue; the gun stays in stock and on the bound book until you ship it.'));
			this._syncDepositNote();
			this._syncTerminalNote();
		});
		this.disposeNow = false;

		this.$body.find('#btn-verify-biz').on('click', () => this._verify(this.fields.biz_ffl, () => this._onBizFflChange()));
		this.$body.find('#btn-lookup-biz').on('click', () => this._lookupDialog(this.fields.biz_ffl, () => this._onBizFflChange()));
		this.$body.find('#btn-verify-dest').on('click', () => this._verify(this.fields.dest_ffl, () => this._onDestFflChange()));
		this.$body.find('#btn-lookup-dest').on('click', () => this._lookupDialog(this.fields.dest_ffl, () => this._onDestFflChange()));
		this.$body.find('#btn-apply-cr').on('click', () => this._applyCrLicense());
	}

	_wireKeyboard() {
		this._itemPicker = new ffl.FirearmItemPicker({
			$input: this.fields.scan.$input,
			$dropdown: this.$body.find('#typeahead'),
			onSelect: (item) => this._showPending(item),
			// Enter with no item match falls back to a firearm serial-number
			// lookup, so one box serves barcodes, SKUs, names AND serials —
			// a scanned serial lands the gun as a finished line in one step.
			onNoMatch: (term) => this._addBySerial(term),
			// And while TYPING, silently probe the term as a serial so the gun
			// shows up as a pickable suggestion — staff hand-keying a serial
			// expect a dropdown, not an Enter-only shortcut. Uses the
			// non-throwing bulk checker so a miss stays quiet.
			serialLookup: (term) => new Promise((resolve) => {
				frappe.call({
					method: 'ffl_core.api.manual_order.check_serials_available',
					args: { serials: JSON.stringify([term]), ignore_invoice: this._editing?.si || undefined },
					callback: (r) => {
						const c = (r.message || {})[term];
						resolve(c && c.ok ? c : null);
					},
					error: () => resolve(null),
				});
			}),
			// Unified search: the drop-ship catalog rides the SAME box —
			// local group first, distributor group appended as it lands. Picking a
			// remote row quotes it in the pending card below (NOT the promote
			// dialog — that stays the "sell as a local line" secondary path).
			remoteAlways: true,
			remoteSearch: (term, limit) => new Promise((resolve) => {
				frappe.call({
					method: 'ffl_integrations.distributor.api.search',
					args: { query: term, limit },
					callback: (r) => resolve(r.message || []),
					error: () => resolve([]),
				});
			}),
			onRemoteSelect: (row) => this._showRemotePending(row),
		});
		this.$body.find('#btn-submit').on('click', () => this._submit());
		this.$body.find('#btn-plan').on('click', (e) => { e.preventDefault(); this._openPlanner(); });
		this.$body.find('#target-total').on('change', (e) => {
			const $inp = $(e.currentTarget);
			this._applyTargetTotal(flt($inp.val()));
			$inp.val('');
		});
		this.$body.find('#btn-rename-cust').on('click', () => this._renameCustomerDialog());
		this.$body.find('#btn-save-draft').on('click', () => this._saveDraft());
		this.$body.find('#btn-drafts').on('click', () => this._showDrafts());
		this._draftName = null;
		this._refreshDraftCount();
	}

	// --------------------------------------------- remote (drop-ship) line entry
	// P1, phase one: adding items decides NOTHING about
	// fulfilment. A remote pick becomes a line with channel="dropship" and
	// channel_confirmed=false; the Fulfillment planner (matrix dialog) confirms
	// channels, runs ensure_dropship_item, and only then can the order submit.

	_showRemotePending(row) {
		this.pending = null;
		const $area = this.$body.find('#pending-area').empty();
		if (!row.distributor) {
			// An "API" pick: one product, N possible houses. No quote here — the
			// house (and with it cost / mode) is chosen in the Fulfillment planner.
			this._renderApiPending(row);
			return;
		}
		$area.html(`<div class="pending">${__('Quoting {0}…', [frappe.utils.escape_html(row.sku)])}</div>`);
		frappe.call({
			method: 'ffl_integrations.distributor.api.quote',
			args: { sku: row.sku, distributor: row.distributor || undefined },
			callback: (r) => this._renderRemotePending(r.message || {}, row),
			error: () => $area.empty(),
		});
	}

	_renderApiPending(row) {
		const $area = this.$body.find('#pending-area').empty();
		const esc = frappe.utils.escape_html;
		const isFirearm = !!(row.is_firearm ?? row.ffl_required);
		const name = (row.item_name || ((row.manufacturer || '') + ' ' + (row.model || '')).trim() || row.upc || row.sku);
		const offers = row.offers || [];
		const known = offers.filter((o) => flt(o.cost) > 0);
		const lowest = known.length ? Math.min(...known.map((o) => flt(o.cost))) : 0;
		const facts = [
			__('{0} source(s)', [offers.length || (row.sources || []).length || 1]),
			lowest ? __('lowest cost {0}', [format_currency(lowest)]) : __('cost unknown'),
			`${__('Total qty')} ${cint(row.qty)}`,
			__('source chosen in the Fulfillment plan'),
		].join(' · ');
		const $card = $(`
			<div class="pending">
				<div style="font-weight:600;margin-bottom:8px">
					${isFirearm ? `<span class="firearm-mark">${__('Firearm')}</span>` : ''}
					<span class="dropship-mark">${__('API')}</span>
					${esc(name)}
				</div>
				<div class="helper" style="margin-bottom:8px">${facts}</div>
				<div class="ffl-row">
					<div><label>${__('Qty')}</label><br><input type="number" id="rp-qty" class="form-control" value="1" min="1" step="1" ${isFirearm ? 'readonly' : ''} style="width:80px"></div>
					<div><label>${__('Price')}</label><br><input type="number" id="rp-rate" class="form-control" value="${flt(row.map || row.msrp || 0).toFixed(2)}" min="0" step="0.01" style="width:130px"></div>
					<div class="grow"></div>
					<button class="btn btn-default btn-sm" id="rp-cancel">${__('Cancel')}</button>
					<button class="btn btn-primary btn-sm" id="rp-add">${__('Add to order')}</button>
				</div>
			</div>`).appendTo($area);
		$card.find('#rp-cancel').on('click', () => { $area.empty(); this.fields.scan.set_focus?.(); });
		$card.find('#rp-add').on('click', () => {
			const qty = isFirearm ? 1 : Math.max(1, cint($card.find('#rp-qty').val()));
			const rate = Math.max(0, flt($card.find('#rp-rate').val()));
			if (!this._addLine({
				item_code: null, sku: null, upc: row.upc || null,
				item_name: name,
				is_firearm: isFirearm, is_antique: false,
				qty, rate, serial: null,
				channel: 'dropship', channel_confirmed: false,
				distributor: null, sources: row.sources || [], cost: null,
			})) return;
			$area.empty();
			this.fields.scan.set_value('');
			this.fields.scan.set_focus?.();
		});
	}

	_renderRemotePending(q, row) {
		const $area = this.$body.find('#pending-area').empty();
		if (!q.found) {
			frappe.show_alert({ message: __('This distributor item is not currently orderable.'), indicator: 'orange' });
			this.fields.scan.set_focus?.();
			return;
		}
		const esc = frappe.utils.escape_html;
		const isFirearm = !!(q.is_firearm ?? q.ffl_required);
		const name = ((q.manufacturer ? q.manufacturer + ' ' : '') + (q.model || q.sku)).trim();
		const mode = (q.fulfillment_mode || '').trim();
		const blocked = mode ? mode !== 'Direct' : !!q.dropship_blocked;
		// Informational only — channel feasibility is judged in the planner, so a
		// blocked/OOS item may still be ADDED (the planner will show why direct is
		// off and, from P2 on, offer procure-to-store).
		const facts = [
			`${__('Cost')} ${format_currency(flt(q.cost))}`,
			`${__('Live qty')} ${cint(q.qty)}`,
			blocked
				? `<span class="text-danger">${esc(q.fulfillment_reason || __('cannot drop-ship — ship to store only'))}</span>`
				: __('drop-ship OK'),
		].join(' · ');
		const $card = $(`
			<div class="pending">
				<div style="font-weight:600;margin-bottom:8px">
					${isFirearm ? `<span class="firearm-mark">${__('Firearm')}</span>` : ''}
					<span class="dropship-mark">${esc(q.distributor || __('Distributor'))}</span>
					${esc(name)}
				</div>
				<div class="helper" style="margin-bottom:8px">${facts}</div>
				<div class="ffl-row">
					<div><label>${__('Qty')}</label><br><input type="number" id="rp-qty" class="form-control" value="1" min="1" step="1" ${isFirearm ? 'readonly' : ''} style="width:80px"></div>
					<div><label>${__('Price')}</label><br><input type="number" id="rp-rate" class="form-control" value="${flt(q.suggested_price)}" min="0" step="0.01" style="width:130px"></div>
					<div class="grow"></div>
					<button class="btn btn-default btn-sm" id="rp-cancel">${__('Cancel')}</button>
					<button class="btn btn-primary btn-sm" id="rp-add">${__('Add to order')}</button>
				</div>
			</div>`).appendTo($area);
		$card.find('#rp-cancel').on('click', () => { $area.empty(); this.fields.scan.set_focus?.(); });
		$card.find('#rp-add').on('click', () => {
			const qty = isFirearm ? 1 : Math.max(1, cint($card.find('#rp-qty').val()));
			const rate = Math.max(0, flt($card.find('#rp-rate').val()));
			if (!this._addLine({
				item_code: null, sku: q.sku,
				item_name: name || q.sku,
				is_firearm: isFirearm, is_antique: false,
				qty, rate, serial: null,
				channel: 'dropship', channel_confirmed: false,
				distributor: q.distributor, cost: flt(q.cost) || null,
			})) return;
			$area.empty();
			this.fields.scan.set_value('');
			this.fields.scan.set_focus?.();
		});
	}

	// ----------------------------------------------- fulfillment planner (P1)
	// One dialog, one batch quote: rows = order lines, columns = local stock +
	// every enabled distributor. Applying the plan is the ONLY path that runs
	// ensure_dropship_item and flips lines to channel_confirmed — submitting
	// stays gated until every line is confirmed.

	_openPlanner() {
		if (!this.lines.length) {
			frappe.show_alert({ message: __('Add items first.'), indicator: 'orange' });
			return;
		}
		const lines = this.lines.map((l) => ({
			item_code: l.item_code || undefined, sku: l.sku || undefined,
			upc: l.upc || undefined, qty: l.qty }));
		frappe.call({
			method: 'ffl_integrations.distributor.api.fulfillment_matrix',
			args: { lines },
			freeze: true, freeze_message: __('Quoting every channel…'),
			callback: (r) => { if (r.message) this._renderPlanner(r.message); },
		});
	}

	_renderPlanner(matrix) {
		this._plannerMatrix = matrix;
		// Selection per line = house ('local' | distributor | null) + that house's
		// mode ('direct' | 'to_store'). Keep a confirmed line's choice while it is
		// still feasible; else the endpoint's rule-chain preselect.
		this._planMode = [];
		// A counter pickup has no address to drop-ship to: every distributor line
		// on it is ordered to the store (the server refuses anything else).
		const pickup = this._isPickup();
		this._planSel = this.lines.map((l, i) => {
			const row = matrix.rows[i] || {};
			if (l.ordered_do) {
				// Bought already — the plan may not re-route it (and must not leave it
				// unselected, which would block Apply on an otherwise fine plan).
				this._planMode[i] = l.channel === 'to_store' ? 'to_store' : 'direct';
				return l.distributor || 'local';
			}
			if (l.channel_confirmed && l.channel === 'local') return 'local';
			if ((l.channel === 'dropship' || l.channel === 'to_store') && l.distributor) {
				const cell = (row.channels || []).find((c) => c.distributor === l.distributor);
				const want = (l.channel === 'to_store' || pickup) ? 'to_store' : 'direct';
				if (cell && ((cell.modes || {})[want] || {}).feasible) {
					this._planMode[i] = want;
					return l.distributor;
				}
			}
			if (pickup) {
				const cell = (row.channels || []).find((c) => ((c.modes || {}).to_store || {}).feasible);
				this._planMode[i] = cell ? 'to_store' : null;
				return row.local.feasible ? 'local' : (cell ? cell.distributor : null);
			}
			this._planMode[i] = row.preselect_mode || null;
			return row.preselect || null;
		});
		this._planFreight = null;
		this._planFreightKey = null;
		this._planFreightOrders = [];
		this._planOptions = { ...this.deliveryOptions };
		if (this._plannerDialog) this._plannerDialog.hide();
		const d = new frappe.ui.Dialog({
			title: __('Fulfillment plan'),
			size: 'extra-large',
			fields: [{ fieldtype: 'HTML', fieldname: 'grid' }],
			primary_action_label: __('Apply plan'),
			primary_action: () => this._applyPlan(),
		});
		this._plannerDialog = d;
		d.show();
		this._renderPlannerGrid();
	}

	_planCell(i) {
		const sel = this._planSel[i];
		if (!sel || sel === 'local') return null;
		return (((this._plannerMatrix.rows || [])[i] || {}).channels || []).find((c) => c.distributor === sel) || null;
	}

	// Select a house for line i. A mode click asks for that mode; a plain cell
	// click keeps the line's current mode on the same house, else the house's
	// best feasible mode (drop-ship first).
	_planPick(i, ch, mode) {
		if (this.lines[i]?.ordered_do) return;   // already bought — not re-routable
		if (ch === 'local') {
			this._planSel[i] = 'local'; this._planMode[i] = null;
			return;
		}
		const cell = (((this._plannerMatrix.rows || [])[i] || {}).channels || []).find((c) => c.distributor === ch);
		if (!cell || !cell.modes) return;
		const ok = (m) => !!(cell.modes[m] || {}).feasible;
		// Pickup: to_store is the only channel, whichever button was pressed.
		let want = this._isPickup() ? 'to_store' : mode;
		if (!want) {
			want = (this._planSel[i] === ch && ok(this._planMode[i])) ? this._planMode[i]
				: cell.mode;   // the house's own default for this line, or null
		}
		if (!ok(want)) return;
		this._planSel[i] = ch;
		this._planMode[i] = want;
	}

	_renderPlannerGrid() {
		const d = this._plannerDialog;
		const matrix = this._plannerMatrix;
		const esc = frappe.utils.escape_html;
		const pickup = this._isPickup();
		const cols = matrix.columns || [];
		let head = `<th style="text-align:left">${__('Item')}</th><th>${__('Local stock')}</th>`;
		cols.forEach((c) => { head += `<th>${esc(c)}</th>`; });
		let body = '';
		this.lines.forEach((l, i) => {
			const row = matrix.rows[i] || { local: {}, channels: [] };
			const sel = this._planSel[i];
			if (l.ordered_do) {
				body += `<tr><td style="text-align:left">${esc(l.item_name)} ×${l.qty}</td>`
					+ `<td class="plan-off" colspan="${cols.length + 1}">`
					+ `${__('Already ordered on {0} — not re-routable here', [esc(l.ordered_do)])}</td></tr>`;
				return;
			}
			const localCell = (() => {
				const st = row.local || {};
				const can = !!st.feasible;
				const splittable = !l.serial && st.in_stock > 0 && st.in_stock < l.qty;
				const cls = sel === 'local' ? 'plan-sel' : (can || splittable ? 'plan-can' : 'plan-off');
				const label = l.serial ? `${__('serial in hand')}`
					: `${cint(st.in_stock)} ${__('in stock')}`;
				const extra = splittable ? `<div class="helper">${__('short {0} — click to split', [l.qty - cint(st.in_stock)])}</div>` : '';
				return `<td class="${cls}" data-i="${i}" data-ch="local" data-split="${splittable ? 1 : 0}">${label}${extra}</td>`;
			})();
			let cells = '';
			(row.channels || []).forEach((c) => {
				if (c.error) { cells += `<td class="plan-off">${__('unavailable')}</td>`; return; }
				if (!c.modes) { cells += `<td class="plan-off">—</td>`; return; }
				const picked = sel === c.distributor;
				const cls = picked ? 'plan-sel' : (c.feasible ? 'plan-can' : 'plan-off');
				const btn = (mode, label) => {
					const m = c.modes[mode] || {};
					const on = picked && this._planMode[i] === mode;
					// A pickup order can only be ordered to the store.
					const blocked = pickup && mode === 'direct';
					const why = blocked ? __('Pickup in store — order it to the store') : (m.reason || '');
					return `<button type="button" class="plan-mode${on ? ' on' : ''}" data-i="${i}" data-ch="${esc(c.distributor)}" data-mode="${mode}"`
						+ `${(m.feasible && !blocked) ? '' : ' disabled'} title="${esc(why)}">${label}</button>`;
				};
				// The reason is what the clerk needs to read — for a To-Store brand it
				// names the manufacturer restriction (e.g. "Ruger: Sports South does not
				// fulfil this manufacturer — ship to store only").
				const why = c.reason ? `<div class="helper">${esc(c.reason)}</div>` : '';
				// Only the ways this house can ever ship get a button: a greyed-out one
				// means "not for this line"; a missing one means the house has no such
				// lane at all (fulfillment_matrix leaves the key out).
				const labels = { direct: __('Dropship'), to_store: __('Ship to store') };
				const buttons = Object.keys(labels)
					.filter((mode) => c.modes[mode])
					.map((mode) => btn(mode, labels[mode])).join('');
				cells += `<td class="${cls}" data-i="${i}" data-ch="${esc(c.distributor)}">`
					+ `${format_currency(flt(c.cost))} · ${__('qty')} ${cint(c.qty)}`
					+ `<div class="plan-modes">${buttons}</div>${why}</td>`;
			});
			body += `<tr><td style="text-align:left">${l.is_firearm ? `<span class="firearm-mark">${__('Firearm')}</span>` : ''}${esc(l.item_name)} ×${l.qty}</td>${localCell}${cells}</tr>`;
		});
		// Summary: sale total vs selected distributor cost + estimated freight
		// (local cost not quoted here).
		let dsCost = 0; let saleTotal = 0; const legNames = new Set();
		this.lines.forEach((l, i) => {
			saleTotal += l.qty * l.rate;
			const sel = this._planSel[i];
			if (sel === 'local') { legNames.add(__('Local')); return; }
			const cell = this._planCell(i);
			if (cell) {
				dsCost += flt(cell.cost) * l.qty;
				legNames.add(`${sel} ${this._planMode[i] === 'to_store' ? __('ship to store') : __('dropship')}`);
			}
		});
		const unresolved = this._planSel.filter((x) => !x).length;
		const fr = this._planFreight;
		const freightKnown = fr && fr.key === this._planFreightRequestKey();
		const freight = freightKnown ? flt(fr.total) : 0;
		const freightText = !dsCost ? ''
			: (freightKnown
				? ` · ${__('freight est.')} ${format_currency(freight)}${fr.complete ? '' : ' ' + __('(partial)')}`
				: ` · ${__('freight est.')} …`);
		// The last answer's orders stay on screen while a changed option re-prices,
		// so the checkboxes don't blink away; their figures read "…" until it lands.
		if (freightKnown) this._planFreightOrders = fr.orders || [];
		const detailOrders = dsCost ? (this._planFreightOrders || []) : [];
		const optionLabels = [['signature', __('Signature')], ['adult_signature', __('Adult signature')],
			['insurance', __('Insurance')]];
		const freightDetail = detailOrders.length
			? `<div class="helper" style="margin-top:4px">${detailOrders.map((o) => {
				const text = esc(`${o.distributor} ${o.mode === 'to_store' ? __('to store') : (o.mode === 'fds' ? __('dropship firearm') : __('dropship'))}`
					+ ` · ${o.line_count} ${__('line(s)')} · ${!freightKnown ? '…' : (o.freight == null ? __('freight unknown') : (flt(o.freight) ? format_currency(o.freight) : __('free')))}`);
				if (!o.options) return text;
				const picked = this._planOptions[o.options_key] || o.options;
				return text + `<span class="plan-opts">${optionLabels.map(([k, label]) =>
					`<label><input type="checkbox" class="plan-opt" data-key="${esc(o.options_key)}" data-opt="${k}"${picked[k] ? ' checked' : ''}> ${label}</label>`).join('')}</span>`;
			}).join('<br>')}</div>`
			: '';
		const summary = unresolved
			? `<span class="text-danger">${__('{0} line(s) have no feasible channel — remove them.', [unresolved])}</span>`
			: `${__('Legs')}: ${Array.from(legNames).map(esc).join(' + ')} · ${__('sale')} ${format_currency(saleTotal)} · ${__('distributor cost')} ${format_currency(dsCost)}${freightText} · ${__('margin excl. local cost')} ${format_currency(saleTotal - dsCost - freight)}${freightDetail}`;
		const html = `
			<style>
				.plan-grid { width:100%; border-collapse:collapse; }
				.plan-grid th, .plan-grid td { border:1px solid var(--border-color); padding:8px 10px; font-size:12.5px; text-align:center; }
				.plan-grid td.plan-can { cursor:pointer; }
				.plan-grid td.plan-can:hover { background: var(--control-bg); }
				.plan-grid td.plan-sel { background: var(--blue-100); box-shadow: inset 0 0 0 2px var(--blue-500); font-weight:600; }
				.plan-grid td.plan-off { color: var(--text-muted); opacity:.65; }
				.plan-modes { display:flex; gap:4px; justify-content:center; margin-top:6px; flex-wrap:wrap; }
				.plan-mode { border:1px solid var(--border-color); background: var(--bg-color); border-radius:10px; padding:1px 8px; font-size:11.5px; font-weight:500; }
				.plan-mode.on { background: var(--blue-500); border-color: var(--blue-500); color:#fff; }
				.plan-mode:disabled { opacity:.45; cursor:not-allowed; }
				.plan-summary { margin-top:10px; font-size:13px; }
				.plan-opts { margin-left:10px; white-space:nowrap; }
				.plan-opts label { margin:0 10px 0 0; font-weight:normal; cursor:pointer; }
				.plan-opts input { vertical-align:middle; margin:0 3px 0 0; }
			</style>
			<div style="overflow-x:auto"><table class="plan-grid">
				<thead><tr>${head}</tr></thead><tbody>${body}</tbody>
			</table></div>
			<div class="plan-summary">${summary}</div>`;
		const $w = d.fields_dict.grid.$wrapper.html(html);
		$w.find('input.plan-opt').on('change', (e) => {
			const $c = $(e.currentTarget);
			const key = String($c.data('key'));
			const order = (this._planFreightOrders || []).find((o) => o.options_key === key);
			this._planOptions[key] = { ...(this._planOptions[key] || (order && order.options) || {}),
				[String($c.data('opt'))]: $c.is(':checked') };
			this._renderPlannerGrid();
		});
		$w.find('button.plan-mode').on('click', (e) => {
			e.stopPropagation();
			const $b = $(e.currentTarget);
			this._planPick(cint($b.data('i')), String($b.data('ch') || ''), String($b.data('mode') || ''));
			this._renderPlannerGrid();
		});
		$w.find('td.plan-can, td.plan-sel').on('click', (e) => {
			const $td = $(e.currentTarget);
			const i = cint($td.data('i'));
			const ch = String($td.data('ch') || '');
			if (!ch) return;
			if (ch === 'local' && cint($td.data('split')) === 1) { this._plannerSplit(i); return; }
			this._planPick(i, ch, null);
			this._renderPlannerGrid();
		});
		this._refreshPlanFreight();
	}

	// The freight request for the current selection — distributor lines only.
	_planFreightLines() {
		const out = [];
		this.lines.forEach((l, i) => {
			// Already bought: its freight was settled on the order that bought it —
			// quoting it again inflates what this plan is about to cost.
			if (l.ordered_do) return;
			const cell = this._planCell(i);
			if (!cell) return;
			// No item_code: Apply fills it in, and the estimate must still match after.
			out.push({ distributor: cell.distributor, mode: this._planMode[i], sku: cell.sku,
				qty: l.qty, cost: flt(cell.cost),
				department: cell.department || '', ffl_required: cell.ffl_required ? 1 : 0,
				is_firearm: (cell.is_firearm ?? cell.ffl_required) ? 1 : 0 });
		});
		return out;
	}

	_planFreightRequestKey() {
		return JSON.stringify({ lines: this._planFreightLines(), options: this._planOptions || {} });
	}

	// Re-price freight when the selection changed; the grid re-renders once the
	// answer for the CURRENT selection lands (a stale answer is ignored).
	_refreshPlanFreight() {
		const key = this._planFreightRequestKey();
		if (key === this._planFreightKey) return;
		this._planFreightKey = key;
		const { lines, options } = JSON.parse(key);
		if (!lines.length) { this._planFreight = { key, total: 0, orders: [], complete: true }; return; }
		frappe.call({
			method: 'ffl_integrations.distributor.api.fulfillment_freight',
			args: { lines, options },
			callback: (r) => {
				if (key !== this._planFreightKey || !this._plannerDialog) return;
				this._planFreight = { key, ...(r.message || {}) };
				this._renderPlannerGrid();
			},
			// No estimate is not a stuck "…": show it as unknown and let the plan apply.
			error: () => {
				if (key !== this._planFreightKey || !this._plannerDialog) return;
				this._planFreight = { key, total: 0, orders: [], complete: false };
				this._renderPlannerGrid();
			},
		});
	}

	// Short local stock: split the line — what's on the shelf stays local, the
	// remainder becomes its own line for a distributor channel. Requote after.
	_plannerSplit(i) {
		const matrix = this._plannerMatrix;
		const have = cint(((matrix.rows[i] || {}).local || {}).in_stock);
		const l = this.lines[i];
		if (!l || have <= 0 || have >= l.qty) return;
		const rest = l.qty - have;
		l.qty = have;
		l.channel = 'local';
		l.channel_confirmed = true;
		this.lines.splice(i + 1, 0, {
			...l, qty: rest, serial: null,
			channel: 'dropship', channel_confirmed: false, distributor: null, cost: null,
		});
		this._renderLines();
		this._openPlanner();
	}

	async _applyPlan() {
		const matrix = this._plannerMatrix;
		if (this._planSel.some((x) => !x)) {
			frappe.show_alert({ message: __('Some lines have no feasible channel — resolve them first.'), indicator: 'orange' });
			return;
		}
		// ensure_dropship_item is a network round-trip per distributor line — say
		// so, and make a double-click impossible while the loop runs.
		const $primary = this._plannerDialog.get_primary_btn();
		if ($primary.prop('disabled')) return;
		$primary.prop('disabled', true).text(__('Applying…'));
		const failures = [];
		for (let i = 0; i < this.lines.length; i++) {
			const l = this.lines[i];
			const sel = this._planSel[i];
			if (l.ordered_do) continue;   // already bought — the plan leaves it alone
			if (sel === 'local') {
				if (l.channel !== 'local') l.cost = undefined;
				l.channel = 'local'; l.channel_confirmed = true;
				l.distributor = null;
				continue;
			}
			const cell = ((matrix.rows[i] || {}).channels || []).find((c) => c.distributor === sel);
			if (!cell) { failures.push(`${l.item_name} — ${__('channel vanished, requote')}`); continue; }
			const toStore = this._planMode[i] === 'to_store';
			if (toStore && l.item_code) {
				// Ordered into the shop for an Item we already have: nothing to set up,
				// and stamping it drop-ship would change how the webshop publishes it.
				l.sku = cell.sku;
				// What the server enforces: the Item's own flag, not a card's earlier guess.
				const own = (matrix.rows[i] || {}).is_firearm;
				l.is_firearm = !!(own ?? cell.is_firearm ?? cell.ffl_required);
				l.channel = 'to_store'; l.channel_confirmed = true;
				l.distributor = sel; l.cost = flt(cell.cost) || null;
				continue;
			}
			// ensure: dual-source guard → idempotent promote → dropship_distributor stamp.
			// A ship-to-store line only needs the Item: the stamp says who drop-ships
			// it on the web, which ordering it into the shop does not decide.
			try {
				const m = await new Promise((resolve, reject) => {
					frappe.call({
						method: 'ffl_integrations.distributor.api.ensure_dropship_item',
						args: { sku: cell.sku, distributor: sel, claim: toStore ? 0 : 1 },
						callback: (r) => resolve(r.message || {}),
						error: reject,
					});
				});
				if (!m.item_code) { failures.push(`${l.item_name} — ${__('could not be set up')}`); continue; }
				l.item_code = m.item_code;
				l.sku = cell.sku;
				l.is_firearm = !!(m.is_firearm ?? cell.is_firearm ?? cell.ffl_required);
				l.channel = toStore ? 'to_store' : 'dropship'; l.channel_confirmed = true;
				l.distributor = sel; l.cost = flt(cell.cost) || null;
			} catch (e) {
				failures.push(`${l.item_name} — ${__('setup failed')}`);
			}
		}
		const fr = this._planFreight;
		const frCurrent = !!(fr && fr.key === this._planFreightRequestKey());
		// Remembered for the legs preview only while the lines stay as planned.
		this._planFreightTotal = frCurrent
			? { total: flt(fr.total), complete: !!fr.complete, sig: this._legsSig() } : null;
		// The delivery options of the orders this plan makes, as shown when applied
		// (defaults included) — create_order writes them onto those Distributor Orders.
		this.deliveryOptions = {};
		(frCurrent ? (fr.orders || []) : (this._planFreightOrders || [])).forEach((o) => {
			if (o.options) this.deliveryOptions[o.options_key] = this._planOptions[o.options_key] || o.options;
		});
		this._renderLines();
		this._syncDestSection();
		this._reflectReadiness();
		$primary.prop('disabled', false).text(__('Apply plan'));
		if (failures.length) {
			frappe.msgprint({ title: __('Plan partially applied'), indicator: 'orange',
				message: failures.map(frappe.utils.escape_html).join('<br>') });
			this._renderPlannerGrid();
			return;
		}
		this._plannerDialog.hide();
		frappe.show_alert({ message: __('Fulfillment plan applied.'), indicator: 'green' }, 4);
		this.fields.scan.set_focus?.();
	}

	// What the remembered freight estimate was priced for: every distributor
	// line's house, channel, SKU and quantity.
	_legsSig() {
		return JSON.stringify(this.lines
			.filter((l) => l.channel === 'dropship' || l.channel === 'to_store')
			.map((l) => [l.distributor, l.channel, l.sku, l.qty]));
	}

	// Section 6 becomes a legs preview once confirmed distributor lines exist —
	// the dispose seg only means something for a pure local order.
	_renderLegsPreview() {
		const $host = this.$body.find('#legs-preview');
		const $seg = this.$body.find('#seg-dispose');
		const $note = this.$body.find('#dispose-note');
		const byLeg = {};
		let localCount = 0;
		let unconfirmed = 0;
		const esc = frappe.utils.escape_html;
		this.lines.forEach((l) => {
			if (!l.channel_confirmed) { unconfirmed++; return; }
			if (l.channel === 'dropship' || l.channel === 'to_store') {
				const key = `${esc(l.distributor || '?')} ${l.channel === 'to_store' ? __('ship to store') : __('drop-ship')}`;
				byLeg[key] = (byLeg[key] || 0) + 1;
			} else localCount++;
		});
		const legs = Object.keys(byLeg);
		if (!legs.length && !unconfirmed) {
			$host.hide().empty(); $seg.show(); $note.show();
			return;
		}
		const parts = [];
		if (localCount) parts.push(`▸ ${__('Local shipment')} × ${localCount} ${__('line(s)')}`);
		legs.forEach((k) => parts.push(`▸ ${k} × ${byLeg[k]} ${__('line(s)')}`));
		// The leg count counts LEGS — the awaiting-plan warning rides below it,
		// never inside it (a "3 legs" headline over 2 legs + 1 warning misleads).
		const legCount = (localCount ? 1 : 0) + legs.length;
		const fr = this._planFreightTotal;
		const freight = (fr && fr.sig === this._legsSig() && legs.length)
			? `<br>▸ ${__('Estimated distributor freight')}: ${format_currency(fr.total)}${fr.complete ? '' : ' ' + __('(partial)')}` : '';
		const warn = unconfirmed
			? `<br><span class="text-danger">▸ ${__('{0} line(s) awaiting the fulfillment plan', [unconfirmed])}</span>` : '';
		$seg.hide(); $note.hide();
		$host.show().html(
			`${__('This order fulfills as {0} leg(s) — released on full payment from the Pending Order queue:', [legCount])}<br>`
			+ parts.join('<br>') + freight + warn);
	}

	// ------------------------------------------------------------- drafts

	_collectPayload() {
		const customer = this.buyerType === 'business' ? this._businessCustomer : this.fields.customer.get_value();
		const payload = {
			buyer_type: this.buyerType,
			customer,
			destination_ffl: this.destinationFfl || null,
			dispose_now: this.disposeNow ? 1 : 0,
			ship_to: {},
			// Full line objects: create_order reads item_code/qty/rate/serials.
			// The display fields serve resumed ACCESSORY lines (no refetch);
			// firearm lines are rebuilt from re-validation on load, which also
			// refreshes their display data — only their staff-set rate is kept.
			items: this.lines.map((l) => ({
				item_code: l.item_code, item_name: l.item_name,
				is_firearm: !!l.is_firearm, is_antique: !!l.is_antique,
				qty: l.qty, rate: l.rate,
				serials: l.serial ? [l.serial] : [],
				// Planner state rides along for drafts; the SERVER contract stays
				// `source` and only a CONFIRMED drop-ship line earns it (an
				// unconfirmed one has no ensured item_code yet — submitting it is
				// exactly the old hole, so _submit gates on confirmation).
				channel: l.channel, channel_confirmed: !!l.channel_confirmed,
				distributor: l.distributor || undefined, sku: l.sku || undefined,
				upc: l.upc || undefined,
				// Where in the store a loose-stock line is taken from (blank = not said).
				storage_location: l.storage_location || undefined,
				...(l.channel === 'dropship' && l.channel_confirmed
					? { source: 'dropship', dropship_distributor: l.distributor || undefined }
					: {}),
				...(l.channel === 'to_store' && l.channel_confirmed ? { source: 'to_store' } : {}),
			})),
		};
		payload.distributor_options = this.deliveryOptions || {};
		for (const f of CreateOrderApp.DRAFT_SCALARS) {
			const v = this.fields[f.field].get_value();
			payload[f.key] = f.type === 'flt' ? (flt(v) || 0)
				: (f.type === 'check' ? (v ? 1 : 0) : (v || ''));
		}
		// The server contract: money is collected now unless the order is Pay
		// later (_submit then lifts it again for the terminal path, which pays
		// AFTER the order books).
		payload.payment_received = payload.pay_later ? 0 : 1;
		const pickup = this._isPickup();
		for (const [key, field] of CreateOrderApp.SHIP_TO_FIELDS) {
			// A pickup books with NO address — that is what makes the invoice a
			// counter pickup server-side; anything typed earlier is not sent.
			payload.ship_to[key] = pickup ? '' : (this.fields[field].get_value() || '');
		}
		if (this._leExempt) {
			payload.le_excise_exempt = 1;
			payload.le_exempt_buyer = this.fields.le_exempt_buyer.get_value() || '';
			payload.le_exempt_certificate = this._leCert || '';
		}
		if (this.buyerType === 'business') {
			payload.tax_exempt = this._taxExempt ? 1 : 0;
			if (this._taxCert) payload.tax_certificate = this._taxCert;
			payload.biz_ffl = this.fields.biz_ffl.get_value() || '';
		}
		return payload;
	}

	_saveDraft() {
		const p = this._collectPayload();
		const hasAnything = p.customer || p.biz_ffl || p.items.length;
		if (!hasAnything) {
			frappe.show_alert({ message: __('Nothing to save yet.'), indicator: 'orange' });
			return;
		}
		frappe.call({
			method: 'ffl_core.api.manual_order.save_order_draft',
			args: { payload: JSON.stringify(p), name: this._draftName || undefined },
			freeze: true, freeze_message: __('Saving draft…'),
			callback: (r) => {
				if (!r.message?.name) return;
				this._draftName = r.message.name;
				frappe.show_alert({
					message: __('Draft saved — {0}. Safe to leave; resume from the Drafts button.', [r.message.title]),
					indicator: 'green',
				}, 6);
				this._refreshDraftCount();
			},
		});
	}

	_refreshDraftCount() {
		frappe.call({
			method: 'ffl_core.api.manual_order.list_order_drafts',
			callback: (r) => {
				this._drafts = r.message || [];
				const n = this._drafts.length;
				this.$body.find('#drafts-count').text(n).toggle(n > 0);
			},
		});
	}

	_showDrafts() {
		const esc = frappe.utils.escape_html;
		const drafts = this._drafts || [];
		const rows = drafts.map((d, i) => `
			<tr>
				<td>${esc(d.title || d.name)}<div class="text-muted" style="font-size:11px">
					${esc(d.modified_by || '')} · ${frappe.datetime.prettyDate(d.modified)}</div></td>
				<td class="right">${format_currency(d.total || 0)}</td>
				<td class="right">
					<button class="btn btn-xs btn-primary dr-load" data-i="${i}">${__('Load')}</button>
					<button class="btn btn-xs btn-danger dr-del" data-i="${i}">✕</button>
				</td>
			</tr>`).join('');
		const d = new frappe.ui.Dialog({
			title: __('Order Drafts'),
			fields: [{ fieldtype: 'HTML', fieldname: 'list', options: drafts.length
				? `<table class="table table-bordered" style="margin:0"><tbody>${rows}</tbody></table>`
				: `<div class="text-muted">${__('No drafts saved.')}</div>` }],
		});
		d.show();
		d.$wrapper.find('.dr-load').on('click', (e) => {
			const row = drafts[$(e.currentTarget).data('i')];
			d.hide();
			const dirty = this.lines.length || this.fields.customer.get_value() || this.fields.biz_ffl.get_value();
			if (dirty) {
				frappe.confirm(__('Loading a draft replaces everything on this page. Continue?'),
					() => this._loadDraft(row.name));
			} else {
				this._loadDraft(row.name);
			}
		});
		d.$wrapper.find('.dr-del').on('click', (e) => {
			const row = drafts[$(e.currentTarget).data('i')];
			frappe.call({
				method: 'ffl_core.api.manual_order.delete_order_draft',
				args: { name: row.name },
				callback: () => {
					if (this._draftName === row.name) this._draftName = null;
					frappe.show_alert({ message: __('Draft deleted.'), indicator: 'orange' }, 4);
					d.hide();
					this._refreshDraftCount();
				},
			});
		});
	}

	_loadDraft(name) {
		frappe.call({
			method: 'ffl_core.api.manual_order.get_order_draft',
			args: { name },
			freeze: true, freeze_message: __('Loading draft…'),
			callback: (r) => {
				const p = r.message?.payload;
				if (!p) return;
				this._resetForNext();
				this._draftName = name;
				this._applyPayload(p);
			},
		});
	}

	// Hydrate the whole form from a create_order payload — shared by draft
	// resume (_loadDraft) and edit mode (_enterEditMode). Verbatim from the old
	// inline _loadDraft callback, so the draft round-trip is unchanged.
	_applyPayload(p) {
		this.deliveryOptions = (p.distributor_options && typeof p.distributor_options === 'object')
			? { ...p.distributor_options } : {};
		// Buyer type first — it shows/hides the sections the rest fill.
		this.$body.find(`#seg-buyer button[data-v="${p.buyer_type === 'business' ? 'business' : 'individual'}"]`).click();
		if (p.buyer_type === 'business') {
			if (p.biz_ffl) this.fields.biz_ffl.set_value(p.biz_ffl);
		} else if (p.customer) {
			this.fields.customer.set_value(p.customer);
		}
		// A payload from before the two checkboxes (an older draft, get_order_for_edit)
		// carries only payment_received: read Pay later off it, terminal on.
		if (p.pay_later === undefined) {
			p = { ...p, pay_later: p.payment_received ? 0 : 1, charge_terminal: 1 };
		}
		// Same table _collectPayload serializes from — one field map.
		for (const f of CreateOrderApp.DRAFT_SCALARS) {
			if (f.field === 'amount') continue; // below, with the edited-flag
			const v = p[f.key];
			this.fields[f.field].set_value(
				f.type === 'check' ? (v ? 1 : 0) : (v ?? f.dflt ?? ''));
		}
		for (const [key, field] of CreateOrderApp.SHIP_TO_FIELDS) {
			this.fields[field].set_value((p.ship_to || {})[key] || '');
		}
		if (p.paid_amount) { this.fields.amount.set_value(p.paid_amount); this._amountEdited = true; }
		this._setLeExempt(!!p.le_excise_exempt, p.le_exempt_buyer, p.le_exempt_certificate);
		this.$body.find(`#seg-dispose button[data-v="${p.dispose_now ? 'now' : 'later'}"]`).click();

		const items = p.items || [];
		const withSerial = items.filter((i) => (i.serials || []).length);
		if (withSerial.length) {
			frappe.call({
				method: 'ffl_core.api.manual_order.check_serials_available',
				args: { serials: JSON.stringify(withSerial.map((i) => i.serials[0])), ignore_invoice: this._editing?.si || undefined },
				callback: (rr) => this._applyDraftLines(p, rr.message || {}),
			});
		} else {
			this._applyDraftLines(p, {});
		}
	}

	// Rebuild the line table from a draft. Drafts reserve nothing, so every
	// parked firearm arrives with a check_serials_available verdict — sold or
	// held guns are dropped, with the reason reported.
	_applyDraftLines(p, checks) {
		const dropped = [];
		(p.items || []).forEach((i) => {
			const serial = (i.serials || [])[0] || null;
			if (serial) {
				const c = checks[serial] || { ok: false, reason: __('validation unavailable') };
				if (!c.ok) { dropped.push(`${serial} — ${c.reason}`); return; }
				this._addLine({ item_code: c.item_code, item_name: c.item_name,
					is_firearm: true, is_antique: !!c.is_antique,
					qty: 1, rate: flt(i.rate) || 0, serial: c.serial });
			} else {
				const channel = i.channel || i.source;
				const ordered = channel === 'dropship' || channel === 'to_store';
				this._addLine({ item_code: i.item_code, item_name: i.item_name || i.item_code,
					is_firearm: !!i.is_firearm, is_antique: !!i.is_antique,
					qty: flt(i.qty) || 1, rate: flt(i.rate) || 0, serial: null,
					storage_location: i.storage_location || '',
					// A drop-ship line resumes AS a drop-ship line. Legacy drafts
					// (pre-planner) carry only `source` + an ensured item_code —
					// resume those as confirmed; planner-era drafts round-trip
					// their own channel/confirmed state.
					...(ordered ? {
						channel,
						channel_confirmed: i.channel ? !!i.channel_confirmed : true,
						distributor: i.distributor || i.dropship_distributor || undefined,
						upc: i.upc || undefined,
						sku: i.sku || undefined,
						// Bought already (get_order_for_edit says so): the line rides the
						// edit locked — its order moves to the new invoice untouched.
						ordered_do: i.ordered_do || undefined,
					} : {}) });
			}
		});
		if (p.buyer_type !== 'business' && p.destination_ffl) {
			this.fields.dest_ffl.set_value(p.destination_ffl);
		}
		if (dropped.length) {
			frappe.msgprint({
				title: __('Some firearms are no longer available'),
				message: `<ul>${dropped.map((x) => `<li>${frappe.utils.escape_html(x)}</li>`).join('')}</ul>`
					+ `<p>${__('They were removed from the draft — pick replacements.')}</p>`,
				indicator: 'orange',
			});
		}
		frappe.show_alert({ message: this._editing
			? __('Order loaded for editing.') : __('Draft loaded.'), indicator: 'green' }, 4);
	}

	_syncDepositNote() {
		const $n = this.$body.find('#deposit-note');
		const received = !this._payLater();
		const amt = flt(this.fields.amount.get_value());
		const total = this._lastTotal || 0;
		if (!received || !total || amt <= 0 || amt + 0.005 >= total) {
			$n.hide().empty();
			return;
		}
		const due = total - amt;
		const base = __('Deposit: {0} now — {1} still due.', [format_currency(amt), format_currency(due)]);
		const warn = (this.disposeNow && this.buyerType === 'individual')
			? ` <span style="color:var(--red-600);font-weight:600">${__('Individual orders need FULL payment to Dispose & ship now — switch Fulfillment to "dispose later" or collect the balance.')}</span>`
			: '';
		$n.show().html(`<span style="color:var(--orange-700,#92400e)">${base}</span>${warn}`);
	}

	_addBySerial(term) {
		const ctrl = this.fields.scan;
		term = (term || '').trim();
		if (!term) return;
		frappe.call({
			method: 'ffl_core.api.manual_order.find_serial_for_order',
			args: { serial: term, ignore_invoice: this._editing?.si || undefined },
			freeze: true, freeze_message: __('Looking up serial…'),
			callback: (r) => {
				const m = r.message;
				if (!m) return;
				if (this._addLine({
					item_code: m.item_code, item_name: m.item_name,
					is_firearm: true, is_antique: !!m.is_antique,
					qty: 1, rate: flt(m.sell_price) || 0,
					serial: m.serial,
				})) {
					frappe.show_alert({
						message: __('{0} — SN {1} added.', [m.item_name, m.serial]),
						indicator: 'green',
					}, 5);
				}
				// Keep focus here so a scanner can rattle through several guns.
				ctrl.set_value(''); this._itemPicker?.close?.(); ctrl.set_focus?.();
			},
		});
	}

	// ---------------------------------------------------------------- buyer

	_onCustomerChange() {
		const cust = this.fields.customer.get_value();
		// Editing + buyer changed: if the email still holds the ORIGINAL buyer's
		// carried address, drop it so it can't email the wrong person — the
		// fill-if-empty below then loads the new buyer's own (or leaves it blank).
		const cur = (this.fields.email.get_value() || '').trim();
		if (this._editing && cur
			&& cur.toLowerCase() === (this._editing.ctx.contact_email || '').trim().toLowerCase()) {
			this.fields.email.set_value('');
		}
		// Banned box / alerts first (customer_flags.js): a Banned customer is cleared
		// and nothing of theirs is filled in.
		const cf = window.ffl && ffl.customerFlags;
		const allowed = cust && cf && cf.guardControl ? cf.guardControl(this.fields.customer) : Promise.resolve(true);
		if (cust) {
			allowed.then((ok) => ok && frappe.db.get_value('Customer', cust, ['email_id', 'mobile_no', 'customer_name'])).then((r) => {
				if (!r) return;
				const m = r.message || {};
				if (m.email_id && !this.fields.email.get_value()) this.fields.email.set_value(m.email_id);
				if (m.mobile_no && !this.fields.phone.get_value()) this.fields.phone.set_value(m.mobile_no);
				if (!this.fields.recipient_name.get_value()) this.fields.recipient_name.set_value(m.customer_name || cust);
			});
		}
		this._reflectReadiness();
	}

	_onBizFflChange() {
		const ffl = this.fields.biz_ffl.get_value();
		this.$body.find('#biz-ffl-card').empty();
		if (!ffl) {
			// FFL cleared — no business customer / destination, and the ship-to is
			// no longer governed by an FFL, so reset that state and unlock it.
			this.destinationFfl = null;
			this._businessCustomer = null;
			this._clearTaxExempt();
			this._syncDestSection();
			this._updateTotals();
			this._reflectReadiness();
			return;
		}
		// Business buyer IS the FFL — resolve/create the dealer Customer and set
		// the destination to their own license.
		frappe.call({
			method: 'ffl_core.api.atf_link.create_party_from_atf',
			args: { record: ffl, party_type: 'Customer' },
			callback: (r) => {
				if (r.message?.name) {
					this._businessCustomer = r.message.name;
					this.destinationFfl = ffl;
					this._renderFflCard('#biz-ffl-card', ffl);
					this._applyFflToShipTo(ffl);
					this._loadBizTaxStatus();
					this._reflectReadiness();
				}
			},
		});
	}

	// -------------------------------------------------------------- pickup
	// A counter pickup has no destination: no address to ship to, so nothing
	// can be drop-shipped (a drop-ship parcel needs the buyer's door) and a
	// distributor line can only be ordered INTO the store. A gun from the shelf
	// is out of scope — a gun the customer takes home today needs a 4473 on the
	// register, not a counter order — so the toggle is unavailable while one is
	// on the order, and adding one turns it back off. A gun ORDERED to the store
	// is the exception: it is handed over on its own POS Invoice, with the 4473,
	// when it arrives (api/counter_pickup), so it needs no destination FFL here.

	_isPickup() {
		return !!cint(this.fields.pickup?.get_value());
	}

	_onPickupChange() {
		// _hasStockRegulated, not _hasRegulated / _hasFirearm: an antique needs no
		// 4473 and no destination FFL, and a regulated gun ordered to the store is
		// handed over on its pickup ticket — the server's own pickup rule
		// (manual_order._assert_pickup_order) reads the same.
		if (this._isPickup() && this._hasStockRegulated()) {
			this.fields.pickup.set_value(0);
			frappe.msgprint({
				title: __('Not for a firearm from stock'), indicator: 'orange',
				message: __('A firearm the customer collects here is a retail sale with a 4473 — ring it on the POS register, or give this order a destination FFL to ship to. Only a gun ordered to the store can be picked up here later.'),
			});
			return;
		}
		this._syncPickup();
	}

	// One place that makes the form agree with the toggle: the address is not
	// asked for, an FFL destination cannot stand beside it, and any line already
	// planned as a drop-ship goes back to the planner (the server would refuse it).
	_syncPickup() {
		const pickup = this._isPickup();
		if (pickup) {
			if (this.fields.dest_ffl.get_value()) this.fields.dest_ffl.set_value('');
			CreateOrderApp.SHIP_TO_FIELDS.forEach(([, f]) => this.fields[f].set_value(''));
			this._lockShipTo(true);
			this.$body.find('#shipto-note').text(
				__('Counter pickup — no shipping address needed. Distributor lines can only be ordered to the store.'));
			let unplanned = 0;
			this.lines.forEach((l) => {
				if (l.channel === 'dropship') { l.channel_confirmed = false; unplanned++; }
			});
			if (unplanned) {
				frappe.show_alert({ indicator: 'orange', message: __(
					'{0} drop-ship line(s) need re-planning — a pickup order can only ship them to the store.',
					[unplanned]) }, 6);
			}
			this._renderLines();
		} else if (!this.destinationFfl) {
			this._lockShipTo(false);
			this.$body.find('#shipto-note').empty();
		}
		// A pickup is handed over at the counter, so there is nothing to charge
		// shipping for — and the totals line reads the field.
		if (pickup && flt(this.fields.shipping.get_value())) this.fields.shipping.set_value(0);
		this.fields.pickup.$input?.prop('disabled', this._hasStockRegulated());
		this._syncDestSection();
		this._reflectReadiness();
		this._updateTotals();
	}

	// ----------------------------------------------------------- destination

	_syncDestSection() {
		// Show the standalone destination FFL section only for INDIVIDUAL buyers
		// with at least one firearm line — business buyers ship to their own FFL,
		// and a pickup's gun is handed over here (no destination at all).
		const show = this.buyerType === 'individual' && this._hasFirearm() && !this._isPickup();
		if (this._hasStockRegulated() && this._isPickup()) {
			// A gun from stock arrived after the toggle went on (scan, draft
			// resume): that is a register sale or an FFL shipment, so pickup
			// cannot stand.
			this.fields.pickup.set_value(0);
			this._lockShipTo(false);
			this.$body.find('#shipto-note').empty();
			frappe.show_alert({ indicator: 'orange', message: __(
				'Pickup in store turned off — a firearm from stock ships to a destination FFL.') }, 6);
		}
		this.fields.pickup?.$input?.prop('disabled', this._hasStockRegulated());
		this.$body.find('#sec-dest').toggle(show);
		if (this.buyerType === 'individual') this.destinationFfl = this.fields.dest_ffl.get_value() || null;
		// No FFL governs the ship-to (individual without a destination FFL, or a
		// business buyer that cleared its FFL) — unlock the address fields, which
		// _applyFflToShipTo locked. They re-lock when an FFL is picked again.
		// A pickup keeps them locked: there is no address to type (this runs from
		// _syncPickup too, and would otherwise undo it).
		if (!this.destinationFfl && !this._isPickup()) {
			this._lockShipTo(false);
			this.$body.find('#shipto-note').empty();
		}
	}

	_onDestFflChange() {
		const ffl = this.fields.dest_ffl.get_value();
		this.$body.find('#dest-ffl-card').empty();
		this.destinationFfl = ffl || null;
		if (ffl) { this._renderFflCard('#dest-ffl-card', ffl); this._applyFflToShipTo(ffl); }
		this._reflectReadiness();
	}

	// ------------------------------------------------------------ C&R (03)
	// A Type 03 collector holds a real FFL, so a gun may be mailed straight to
	// their home — but the ATF's public list contains no 03s (that is why this
	// exists), so Verify/Lookup can never resolve one and the number alone
	// carries no address or expiry. The clerk reads them off the license copy
	// here; save_cr_ffl validates them with the same server-side rules the web
	// queue uses and returns an ordinary ATF FFL Record. From that point this
	// is a normal destination: the ship-to lock, the destination-based sales
	// tax and the FFL Transfer disposition all run unchanged.

	_isCr() { return !!cint(this.fields.cr_transfer?.get_value()); }

	// Which of the two ways to name a destination is on screen. Separate from
	// the change handler so a programmatic reset can re-sync the form without
	// depending on whether Frappe fired `change` for it.
	_syncCrSection() {
		const cr = this._isCr();
		this.$body.find('#dest-standard').toggle(!cr);
		this.$body.find('#dest-cr').toggle(cr);
	}

	_onCrToggle() {
		this._syncCrSection();
		// Whichever way the toggle moved, the destination on file was chosen in
		// the OTHER mode — drop it rather than let a half-switched form ship a
		// gun to the FFL that is no longer on screen. The C&R block re-supplies
		// one through "Use this C&R license".
		if (this.fields.dest_ffl.get_value()) this.fields.dest_ffl.set_value('');
		this._syncDestSection();
		this._reflectReadiness();
	}

	// A collector who has ordered before is already on file — fill the blanks
	// from that record so the clerk retypes nothing. Only BLANKS: a value the
	// clerk just read off the license copy outranks a stored one (that is how a
	// renewed expiry or a move gets corrected).
	_prefillFromCrRecord() {
		const num = (this.fields.cr_license_number.get_value() || '').trim();
		if (!num) return;
		frappe.db.get_value('ATF FFL Record', num,
			['licensee_name', 'street', 'street2', 'city', 'state', 'zip', 'expiry_date']).then((r) => {
			const m = r.message || {};
			if (!m.licensee_name && !m.street) return;  // not on file — it gets typed
			const fill = (field, value) => {
				if (value && !(this.fields[field].get_value() || '').toString().trim()) {
					this.fields[field].set_value(value);
				}
			};
			fill('cr_contact_name', m.licensee_name);
			fill('cr_license_expiry', m.expiry_date);
			fill('cr_premise_street', m.street);
			fill('cr_premise_street2', m.street2);
			fill('cr_premise_city', m.city);
			fill('cr_premise_state', m.state);
			fill('cr_premise_zip', m.zip);
		});
	}

	_crDetails() {
		const g = (field) => (this.fields[field].get_value() || '').toString().trim();
		return {
			license_number: g('cr_license_number'),
			license_expiry: g('cr_license_expiry'),
			contact_name: g('cr_contact_name'),
			premise_street: g('cr_premise_street'),
			premise_street2: g('cr_premise_street2'),
			premise_city: g('cr_premise_city'),
			premise_state: g('cr_premise_state'),
			premise_zip: g('cr_premise_zip'),
		};
	}

	// No client-side copy of the license/expiry/state rules: save_cr_ffl throws
	// the operator-facing message, and a second statement of those rules here
	// is one that can drift away from the one that actually decides.
	_applyCrLicense() {
		frappe.call({
			method: 'ffl_core.api.manual_order.save_cr_ffl',
			args: { cr_details: JSON.stringify(this._crDetails()) },
			freeze: true, freeze_message: __('Saving C&R license…'),
			callback: (r) => {
				if (!r.message?.name) return;
				// Feeds the ordinary destination field — its change handler draws
				// the card and locks the ship-to to the licensed premises.
				this.fields.dest_ffl.set_value(r.message.name);
				frappe.show_alert({ indicator: 'green', message: __(
					'C&R license saved — the order ships to the licensed premises.') }, 5);
			},
		});
	}

	_renderFflCard(sel, ffl) {
		frappe.db.get_value('ATF FFL Record', ffl,
			['licensee_name', 'street', 'street2', 'city', 'state', 'zip', 'phone', 'expiry_date']).then((r) => {
			const m = r.message || {};
			const expired = !m.expiry_date || frappe.datetime.get_diff(m.expiry_date, frappe.datetime.now_date()) < 0;
			const badge = expired
				? `<span class="badge warn">${__('Expired / unverified')}</span>`
				: `<span class="badge ok">${__('Valid to')} ${frappe.datetime.str_to_user(m.expiry_date)}</span>`;
			this.$body.find(sel).html(`
				<div class="ffl-card">
					<div class="lic">${frappe.utils.escape_html(m.licensee_name || ffl)} ${badge}</div>
					<div>${frappe.utils.escape_html([m.street, m.street2, m.city, m.state, m.zip].filter(Boolean).join(', '))}</div>
					${m.phone ? `<div>${frappe.utils.escape_html(m.phone)}</div>` : ''}
					<div class="helper">FFL #: ${frappe.utils.escape_html(ffl)}</div>
				</div>`);
		});
	}

	_applyFflToShipTo(ffl) {
		frappe.db.get_value('ATF FFL Record', ffl,
			['licensee_name', 'street', 'street2', 'city', 'state', 'zip', 'phone']).then((r) => {
			const m = r.message || {};
			this.fields.recipient_name.set_value(m.licensee_name || ffl);
			if (m.phone) this.fields.recipient_phone.set_value(m.phone);
			this.fields.ship_line1.set_value(m.street || '');
			this.fields.ship_line2.set_value(m.street2 || '');
			this.fields.ship_city.set_value(m.city || '');
			this.fields.ship_state.set_value(m.state || '');
			this.fields.ship_zip.set_value(m.zip || '');
			// Firearm orders ship to the FFL — lock the ship-to so it can't drift.
			this._lockShipTo(true);
			this.$body.find('#shipto-note').text(__('Locked to the destination FFL address (firearms ship to the FFL).'));
			this._updateTotals();
		});
	}

	_verify(ctrl, cb) {
		const ffl = ctrl.get_value();
		if (!ffl) { frappe.show_alert({ message: __('Pick or enter an FFL first.'), indicator: 'orange' }); return; }
		frappe.call({
			method: 'ffl_core.api.manual_order.verify_ffl', args: { ffl_number: ffl },
			freeze: true, freeze_message: __('Verifying FFL…'),
			callback: (r) => { if (r.message?.ok) { frappe.show_alert({ message: __('FFL verified.'), indicator: 'green' }); cb && cb(); } },
		});
	}

	_lookupDialog(ctrl, cb) {
		const d = new frappe.ui.Dialog({
			title: __('Lookup FFL by Number'),
			fields: [{ label: __('FFL Number'), fieldname: 'ffl', fieldtype: 'Data', reqd: 1 }],
			primary_action_label: __('Lookup'),
			primary_action: (v) => {
				frappe.call({
					method: 'ffl_core.api.manual_order.verify_ffl', args: { ffl_number: v.ffl },
					freeze: true, freeze_message: __('Looking up…'),
					callback: (r) => { if (r.message?.name) { d.hide(); ctrl.set_value(r.message.name); cb && cb(); } },
				});
			},
		});
		d.show();
	}

	// ---------------------------------------------------------------- items

	_hasFirearm() { return this.lines.some((l) => l.is_firearm); }
	_hasRegulated() { return this.lines.some((l) => l.is_firearm && !l.is_antique); }
	// A regulated gun that is on the shelf now (a serial was picked) — as
	// opposed to one still to be ordered from a distributor (no serial yet).
	_hasStockRegulated() { return this.lines.some((l) => l.is_firearm && !l.is_antique && !!l.serial); }

	_showPending(item) {
		// Prefill the SELLING price (Item.standard_rate). `uniform_sell_price` is a
		// bool flag and `last_rate` is the purchase cost — neither is the sale price.
		// For per-gun firearms standard_rate is 0; the serial pick fills the real price.
		this.pending = { item, qty: 1, rate: flt(item.standard_rate || 0), serial: null };
		const $area = this.$body.find('#pending-area').empty();
		const isFirearm = !!item.is_firearm;
		const $card = $(`
			<div class="pending">
				<div style="font-weight:600;margin-bottom:8px">
					${isFirearm ? `<span class="firearm-mark">${__('Firearm')}</span>` : ''}
					${frappe.utils.escape_html(item.item_name || item.item_code)}
				</div>
				<div class="row cols-3">
					<div id="p-serial"></div><div id="p-qty"></div><div id="p-rate"></div>
				</div>
				<div style="margin-top:10px;display:flex;gap:8px;justify-content:flex-end">
					<button class="btn btn-default btn-sm" id="p-cancel">${__('Cancel')}</button>
					<button class="btn btn-primary btn-sm" id="p-add">${__('Add to Order')}</button>
				</div>
			</div>`).appendTo($area);

		const qtyCtrl = frappe.ui.form.make_control({ parent: $card.find('#p-qty')[0],
			df: { label: __('Qty'), fieldtype: 'Float', fieldname: 'pqty' }, render_input: true });
		qtyCtrl.refresh(); qtyCtrl.set_value(1);
		const rateCtrl = frappe.ui.form.make_control({ parent: $card.find('#p-rate')[0],
			df: { label: __('Price'), fieldtype: 'Currency', fieldname: 'prate' }, render_input: true });
		rateCtrl.refresh(); rateCtrl.set_value(this.pending.rate);
		this.pending.qtyCtrl = qtyCtrl; this.pending.rateCtrl = rateCtrl;

		if (isFirearm) {
			// One unit per serial: a serial Select replaces free qty.
			qtyCtrl.$input?.prop('readonly', true);
			const serialCtrl = frappe.ui.form.make_control({ parent: $card.find('#p-serial')[0],
				df: { label: __('Serial (in stock)'), fieldtype: 'Select', fieldname: 'pserial', options: '' }, render_input: true });
			serialCtrl.refresh();
			this.pending.serialCtrl = serialCtrl;
			frappe.call({
				method: 'ffl_core.api.manual_order.available_serials', args: { item_codes: [item.item_code] },
				callback: (r) => {
					const rows = (r.message || {})[item.item_code] || [];
					this._serialPrices = {};
					const opts = rows.map((row) => {
						this._serialPrices[row.serial] = flt(row.sell_price);
						return `${row.serial}${row.sell_price != null ? `  —  ${format_currency(row.sell_price)}` : ''}`;
					});
					serialCtrl.df.options = ['', ...opts.map((_, i) => rows[i].serial)].join('\n');
					serialCtrl.refresh();
					// Build a friendly label map for the dropdown.
					serialCtrl.$input?.find('option').each((i, o) => { if (rows[i - 1]) $(o).text(opts[i - 1]); });
					if (rows.length === 1) { serialCtrl.set_value(rows[0].serial); this._onSerialPick(rows[0].serial); }
				},
			});
			serialCtrl.df.onchange = () => this._onSerialPick(serialCtrl.get_value());
			serialCtrl.$input?.on('change', () => this._onSerialPick(serialCtrl.get_value()));
		} else {
			$card.find('#p-serial').remove();
			$card.find('.row').removeClass('cols-3');
			qtyCtrl.df.onchange = () => { this.pending.qty = flt(qtyCtrl.get_value()) || 0; };
		}
		rateCtrl.$input?.on('change', () => { this.pending.rate = flt(rateCtrl.get_value()) || 0; });

		$card.find('#p-cancel').on('click', () => { this.pending = null; $area.empty(); this.fields.scan.set_value(''); });
		$card.find('#p-add').on('click', () => this._addPending());
	}

	_onSerialPick(serial) {
		if (!serial) return;
		this.pending.serial = serial;
		// Price follows the selling price: uniform-priced items return the item's
		// standard sell price for every serial; otherwise each serial's own price.
		const price = (this._serialPrices || {})[serial];
		if (price != null) { this.pending.rate = price; this.pending.rateCtrl.set_value(price); }
	}

	// Single entry point for a new order line — dedupes by serial, pushes,
	// and runs the render/destination/readiness refresh. Shared by the
	// pending card (_addPending), the serial scan (_addBySerial), and draft
	// resume (_applyDraftLines) so the line shape and refresh sequence can't
	// drift between the three.
	_addLine(line) {
		// P1: every line carries a fulfilment channel.
		// Paths that don't say one (local pending card, serial scan, legacy
		// drafts) mean local stock — unambiguous, so born confirmed.
		if (!line.channel) {
			line.channel = 'local';
			line.channel_confirmed = true;
		}
		if (line.serial && this.lines.some((l) => l.serial === line.serial)) {
			frappe.show_alert({ message: __('Serial {0} is already on the order.', [line.serial]), indicator: 'orange' });
			return false;
		}
		this.lines.push(line);
		this._renderLines();
		this._syncDestSection();
		this._reflectReadiness();
		return true;
	}

	_addPending() {
		const p = this.pending;
		if (!p) return;
		const isFirearm = !!p.item.is_firearm;
		if (isFirearm && !p.serial) {
			frappe.show_alert({ message: __('Pick an in-stock serial.'), indicator: 'orange' }); return;
		}
		if (!isFirearm) p.qty = flt(p.qtyCtrl.get_value()) || 0;
		if (!isFirearm && p.qty <= 0) { frappe.show_alert({ message: __('Quantity must be > 0.'), indicator: 'orange' }); return; }
		if (!this._addLine({
			item_code: p.item.item_code, item_name: p.item.item_name || p.item.item_code,
			is_firearm: isFirearm, is_antique: !!p.item.is_antique,
			qty: isFirearm ? 1 : p.qty, rate: flt(p.rateCtrl.get_value()) || 0,
			serial: isFirearm ? p.serial : null,
		})) return;
		this.pending = null;
		this.$body.find('#pending-area').empty();
		this.fields.scan.set_value('');
		this._itemPicker?.close?.();
		this.fields.scan.set_focus?.();
	}

	_renderLines() {
		const $tb = this.$body.find('#lines-tbody').empty();
		this.$body.find('#lines-area').toggle(this.lines.length > 0);
		this.lines.forEach((l, idx) => {
			const unconfirmed = l.channel_confirmed ? '' : 'style="opacity:.6;border:1px dashed var(--orange-400)"';
			$(`<tr>
				<td>${l.is_firearm ? `<span class="firearm-mark">${__('Firearm')}</span>` : ''}${l.channel === 'dropship' ? `<span class="dropship-mark" ${unconfirmed}>${l.distributor ? `${__('Drop-ship')} · ${frappe.utils.escape_html(l.distributor)}` : __('API')}${l.channel_confirmed ? '' : ' · ' + (l.distributor ? __('unconfirmed') : __('choose source in the plan'))}</span>` : ''}${l.channel === 'to_store' ? `<span class="dropship-mark" ${unconfirmed}>${__('Ship to store')} · ${frappe.utils.escape_html(l.distributor || '')}${l.channel_confirmed ? '' : ' · ' + __('unconfirmed')}</span>` : ''}${frappe.utils.escape_html(l.item_name)}
					${l.serial ? `<div class="serials">${__('Serial')}: ${frappe.utils.escape_html(l.serial)}</div>` : ''}
					${l.channel === 'dropship' ? `<div class="serials">${__('Ships from distributor — no in-store serial')}</div>` : ''}
					${l.channel === 'to_store' ? `<div class="serials">${l.is_firearm ? __('Ordered to the store — serial assigned and held when it arrives') : __('Ordered to the store — ships once it arrives')}</div>` : ''}
					${l.ordered_do ? `<div class="serials">${__('Already ordered on {0} — quantity and source are fixed; the price can still change.', [frappe.utils.escape_html(l.ordered_do)])}</div>` : ''}
					${this._takesLocation(l) ? `<div class="loc-wrap" data-idx="${idx}" style="display:none;margin-top:4px">
						<select class="line-loc form-control input-sm" data-idx="${idx}" style="max-width:260px;display:inline-block"></select></div>` : ''}</td>
				<td class="right">${(l.is_firearm || l.ordered_do) ? l.qty : `<input type="number" class="line-qty" data-idx="${idx}"
					value="${l.qty}" min="1" step="1">`}</td>
				<td class="right line-cost">${this._costHtml(l)}</td>
				<td class="right"><input type="number" class="line-rate" data-idx="${idx}"
					value="${flt(l.rate)}" min="0" step="0.01"></td>
				<td class="right">${format_currency(l.qty * l.rate)}</td>
				<td class="right">${l.ordered_do ? '' : `<button class="btn-remove" data-idx="${idx}">✕</button>`}</td>
			</tr>`).appendTo($tb);
		});
		// Price is editable in place — a scanned gun lands with its tag price
		// (or 0 when unpriced) and staff adjust it right on the line. Quantity too,
		// for non-serialized lines (a firearm stays one unit per serial).
		$tb.find('.line-rate').on('change', (e) => {
			const $inp = $(e.currentTarget);
			const i = $inp.data('idx');
			this.lines[i].rate = Math.max(0, flt($inp.val()));
			this._renderLines();
		});
		$tb.find('.line-qty').on('change', (e) => {
			const $inp = $(e.currentTarget);
			const l = this.lines[$inp.data('idx')];
			const qty = Math.max(1, cint($inp.val()));
			// A distributor line was planned for its old quantity (the house's
			// stock, the freight estimate): a new one goes back through the plan
			// instead of riding the old quote.
			if (qty !== l.qty && (l.channel === 'dropship' || l.channel === 'to_store')) {
				l.channel_confirmed = false;
			}
			l.qty = qty;
			this._renderLines();
			this._reflectReadiness();
		});
		$tb.find('.btn-remove').on('click', (e) => {
			this.lines.splice($(e.currentTarget).data('idx'), 1);
			this._renderLines(); this._syncDestSection(); this._reflectReadiness();
		});
		$tb.find('.line-loc').on('change', (e) => {
			const $sel = $(e.currentTarget);
			const line = this.lines[$sel.data('idx')];
			line.storage_location = $sel.val() || '';
			line._locTouched = true; // a deliberate blank stays blank
			$sel.css({ borderColor: '' });
		});
		this._loadLocations();
		this._renderLegsPreview();
		this._updateTotals();
		// Deferred so lines added in one go (a resumed draft) share one request.
		clearTimeout(this._costTimer);
		this._costTimer = setTimeout(() => this._refreshCosts());
	}

	// A loose-stock line taken from our own shelf names where in the store it comes
	// from; guns (by serial) and distributor lines have no location to name.
	_takesLocation(l) {
		return !l.is_firearm && !l.serial && (l.channel || 'local') === 'local';
	}

	// Where each loose-stock item sits, fetched once per item and painted into the
	// line's dropdown. Only the dropdowns are touched, never the whole table, so a Qty
	// or Price being typed survives. The only place an item can have come from is picked
	// for the clerk; with several places they choose (blank is allowed: the units then
	// show on the Storage Locations "Not placed" tab).
	_loadLocations() {
		const need = [...new Set(this.lines.filter((l) => this._takesLocation(l)
			&& !(l.item_code in (this._locCache || {}))).map((l) => l.item_code))];
		this._locCache = this._locCache || {};
		if (!need.length) return this._paintLocations();
		frappe.call({
			method: 'ffl_core.api.storage.stock_locations', args: { item_codes: JSON.stringify(need) },
			freeze: false, no_spinner: true,
			callback: (r) => {
				need.forEach((c) => { this._locCache[c] = ((r.message || {})[c]) || { locations: [], unassigned: 0 }; });
				this._paintLocations();
			},
			// A failed lookup is not cached: the next render asks again.
			error: () => {},
		});
	}

	_paintLocations() {
		this.$body.find('#lines-tbody .loc-wrap').each((_i, el) => {
			const idx = $(el).data('idx');
			const l = this.lines[idx];
			const info = l && (this._locCache || {})[l.item_code];
			if (!info || !info.locations.length) return $(el).hide();
			if (l.storage_location === undefined || l.storage_location === null) l.storage_location = '';
			// The only place it can have come from is picked for the clerk (server rule).
			if (!l.storage_location && !l._locTouched && info.sole) l.storage_location = info.sole;
			const opts = [`<option value="">${__('Taken from: not said')}</option>`].concat(
				info.locations.map((x) => `<option value="${frappe.utils.escape_html(x.location)}"
					${x.location === l.storage_location ? 'selected' : ''}>${frappe.utils.escape_html(x.label)} (${flt(x.qty)})</option>`));
			const $sel = $(el).find('select').html(opts.join(''));
			$sel.css({ borderColor: !l.storage_location ? 'var(--orange-500, #f57c00)' : '' });
			$(el).show();
		});
	}

	// Cost column. A line costed from a quote in hand (the pending card, the plan)
	// carries it; the rest (cost undefined, shown "…") ask the server, which costs
	// each through the store's own figure — the edited order's Distributor Orders
	// included. Unknown comes back null and shows "—", never $0.00.
	_costHtml(l) {
		return l.cost > 0 ? format_currency(l.cost)
			: `<span class="text-muted">${l.cost === undefined ? '…' : '—'}</span>`;
	}

	_refreshCosts() {
		const todo = this.lines.filter((l) => l.cost === undefined);
		if (!todo.length) return;
		// Only the cost cells are redrawn: a full re-render would wipe a Qty or
		// Price the clerk is typing when a slow quote lands.
		const settle = (costs) => {
			const $cells = this.$body.find('#lines-tbody .line-cost');
			todo.forEach((l, k) => {
				if (l.cost === undefined) l.cost = costs[k] || null;
				const i = this.lines.indexOf(l);
				if (i >= 0) $cells.eq(i).html(this._costHtml(l));
			});
		};
		frappe.call({
			method: 'ffl_core.api.manual_order.order_line_costs',
			args: {
				items: todo.map((l) => ({ item_code: l.item_code, serials: l.serial ? [l.serial] : [],
					channel: l.channel, distributor: l.distributor, sku: l.sku })),
				sales_invoice: this._editing?.si,
			},
			callback: (r) => settle(r.message || []),
			error: () => settle([]),
		});
	}

	// Destination-based sales-tax preview: in-state (or no destination yet) is
	// taxed at the home rate; out-of-state (or an exempt buyer) is 0. The server
	// recomputes the authoritative tax from the shipping address at create time.
	_taxPct() {
		return this._inState() ? Math.max(0, (this._taxRate || 0) - (this._leExempt ? this._leExciseRate || 0 : 0)) : 0;
	}

	// Taxed unless the ship-to reads as ANOTHER US state — the server's rule
	// (interstate_tax._ships_out_of_state): "Texas" is TX through the server's own
	// name table, and a state it cannot read is taxed, never guessed exempt.
	_inState() {
		// No home state from the server (quote_config failed, e.g. the Company has no
		// address): no tax is previewed rather than one for a guessed state.
		if (this._taxExempt || !this._homeState) return false;
		const raw = (this.fields.ship_state.get_value() || '').trim().toUpperCase()
			.replace(/\s+/g, ' ').replace(/\.+$/, '');
		// Before the table loads (or if it can't), a two-letter entry still reads
		// as a code — the rule the page had before it knew state names.
		const codes = this._stateCodes || new Set();
		const known = codes.size ? codes.has(raw) : /^[A-Z]{2}$/.test(raw);
		const code = (this._usStates || {})[raw] || (known ? raw : null);
		return !code || code === this._homeState;
	}

	// Freight tax the server adds ON TOP of the shipping on an in-state taxable
	// order (compliance/shipping_charge.py: rate% × the full freight, rounded
	// once). 0 when the goods aren't taxed (goodsTax, unrounded — semantics Y: an
	// Item-Tax-Template-exempt order's freight carries no tax either), in a home
	// state that doesn't tax freight at all (California), and when the shipping
	// already includes its tax (nothing is added then).
	_shippingTax(shipping, pct, goodsTax) {
		return this._freightTaxed(goodsTax) && !this._shipTaxIncluded() ? flt(shipping * pct / 100, 2) : 0;
	}

	_freightTaxed(goodsTax) {
		return goodsTax > 0 && this._freightTaxable;
	}

	// A line's sales-tax rate: the item's own (item_tax_rates — its Item Tax
	// Template applied to the default template, as the server resolves it) once
	// known, else the template rate. 0 whenever the order isn't taxed at all.
	_lineRate(l, pct) {
		if (!(pct > 0)) return 0;
		const r = (this._itemRates || {})[l.item_code];
		return typeof r === 'number' ? r : pct;
	}

	// Goods tax, UNROUNDED — the server's "any positive goods tax" test reads it so.
	_goodsTax(pct) {
		return this.lines.reduce((s, l) => s + l.qty * l.rate * this._lineRate(l, pct) / 100, 0);
	}

	// The lines' blended rate (exempt lines count 0) — the template rate itself
	// while every line carries it, so the common case computes exactly as before.
	_blendedPct(pct) {
		if (this.lines.every((l) => this._lineRate(l, pct) === pct)) return pct;
		const net = this.lines.reduce((s, l) => s + l.qty * l.rate, 0);
		return net > 0 ? 100 * this._goodsTax(pct) / net : pct;
	}

	// Fetch the per-item rates the preview doesn't have yet, then re-price.
	_fetchItemRates() {
		this._itemRates = this._itemRates || {};
		const missing = [...new Set(this.lines.map((l) => l.item_code))]
			.filter((c) => c && !(c in this._itemRates));
		if (!missing.length) return;
		missing.forEach((c) => { this._itemRates[c] = null; });
		const asked = this._itemRates;
		frappe.call({
			method: 'ffl_core.api.manual_order.item_tax_rates',
			args: { item_codes: JSON.stringify(missing), le_excise_exempt: this._leExempt ? 1 : 0 },
			callback: (r) => {
				if (asked !== this._itemRates) return; // the exemption flipped meanwhile
				Object.assign(this._itemRates, r.message || {});
				this._updateTotals();
			},
		});
	}

	// FFL Settings "Shipping Price Includes Sales Tax" for a new order; an order
	// being edited keeps the basis it was created with (the server does the same).
	_shipTaxIncluded() {
		return this._editing ? !!this._editing.ctx.shipping_tax_inclusive : !!this._shipInclusive;
	}

	// "Charge exactly $X": work the pre-tax net back from a tax-inclusive total
	// (less shipping and any tax added on top of it, exactly as the preview does) and
	// spread it over the lines in proportion to their current subtotals — evenly
	// when every line is still $0. Tax rounds to the cent, so the net is nudged a
	// cent either way until the preview lands on the typed total; the last line
	// absorbs the spread's rounding (a multi-unit last line may still miss by a cent).
	_applyTargetTotal(target) {
		if (!this.lines.length || !(target > 0)) return;
		const shipping = flt(this.fields.shipping.get_value()) || 0;
		const pct = this._blendedPct(this._taxPct());
		const goal = flt(target - shipping - this._shippingTax(shipping, this._taxPct(), pct), 2);
		if (goal <= 0) {
			frappe.show_alert({ message: __('Total must be more than the shipping charge.'), indicator: 'orange' });
			return;
		}
		let net = flt(goal / (1 + pct / 100), 2);
		for (const d of [0, -0.01, 0.01, -0.02, 0.02]) {
			const n = flt(net + d, 2);
			if (flt(n + flt(n * pct / 100, 2), 2) === goal) { net = n; break; }
		}
		const cur = this.lines.map((l) => l.qty * l.rate);
		const sum = cur.reduce((s, v) => s + v, 0);
		let left = net;
		this.lines.forEach((l, i) => {
			const last = i === this.lines.length - 1;
			const share = last ? left : flt(sum > 0 ? net * cur[i] / sum : net / this.lines.length, 2);
			l.rate = Math.max(0, flt(share / (l.qty || 1), 2));
			left = flt(left - l.rate * l.qty, 2);
		});
		this._renderLines();
	}

	_updateTotals() {
		const net = this.lines.reduce((s, l) => s + l.qty * l.rate, 0);
		this.$body.find('#line-count').text(this.lines.length);
		const state = (this.fields.ship_state.get_value() || '').trim();
		const taxed = this._inState();
		const rate = this._taxPct();
		this._fetchItemRates();
		const goodsTax = this._goodsTax(rate);
		const tax = flt(goodsTax, 2);
		const shipping = flt(this.fields.shipping.get_value()) || 0;
		const shipTax = this._shippingTax(shipping, rate, goodsTax);
		const grand = net + tax + shipping + shipTax;
		this.$body.find('#total').html(format_currency(grand));
		const shippingLabel = shipping > 0
			? `  ·  ${__('Shipping')} ${format_currency(shipping)}${shipTax > 0 ? ` (+${format_currency(shipTax)} ${__('tax')})`
				: (this._freightTaxed(goodsTax) && this._shipTaxIncluded() ? ` ${__('(sales tax included)')}` : '')}` : '';
		this.$body.find('#tax-line').text(
			net > 0 && rate > 0
				? `${__('Subtotal')} ${format_currency(net)}  ·  ${__('Sales Tax')} ${rate}% ${format_currency(tax)}${
					this._leExempt ? ` (${__('excise exempt')})` : ''}${shippingLabel}`
				: (net > 0 && this._taxExempt ? `${__('Tax exempt — no sales tax')}${shippingLabel}`
				   : (net > 0 && !this._homeState ? this._noHomeStateNotice()
				      : (net > 0 && state && !taxed ? `${__('Out-of-state — no sales tax')}${shippingLabel}` : ''))));
		if (!this._amountEdited) {
			this._lastAutoAmount = grand;
			this.fields.amount.set_value(grand);
		}
		this._lastTotal = grand;
		this._syncDepositNote();
		if (this._editing) this._syncEditDelta();
	}

	// Shown in the totals area while the home state is unknown, so a blank tax line is
	// never mistaken for "no tax due". The server's own message is shown by frappe.
	_noHomeStateNotice() {
		return `${__('Sales tax not calculated')}: ${this._taxConfigFailed
			? __("the store's tax settings could not be loaded (set the Company's address, with a US state, in Company › Addresses)")
			: __("the store's tax settings have not loaded yet")}`;
	}

	_loadTaxConfig() {
		this._taxConfigFailed = false;
		this._taxRate = 0; this._homeState = ''; this._freightTaxable = true; this._itemRates = {};
		// The server's default until quote_config answers — a stale or failed load
		// must never preview tax ON TOP of a shipping price that already includes it.
		this._shipInclusive = true;
		this._usStates = {}; this._stateCodes = new Set();
		frappe.call({
			method: 'ffl_core.api.manual_order.quote_config',
			// No custom error handler: frappe shows the server's own message (e.g. "Set the
			// company's address ..."), and without a home state no tax is previewed.
			error: () => { this._taxConfigFailed = true; this._updateTotals(); },
			callback: (r) => {
				const m = r.message || {};
				this._taxRate = flt(m.rate) || 0;
				this._homeState = (m.home_state || '').toUpperCase();
				this._taxConfigFailed = !this._homeState;
				this._freightTaxable = m.freight_taxable !== false;
				this._shipInclusive = !!m.shipping_tax_inclusive;
				this._leExciseRate = flt(m.le_excise_rate) || 0;
				this.$body.find('#le-exempt').toggle(!!m.le_exempt_offered);
				this._usStates = m.us_states || {};
				this._stateCodes = new Set(Object.values(this._usStates));
				this.fields.shipping.set_description(this._freightTaxable ? '' : __(
					'The actual carrier cost — shipping charged at cost is not taxed here. Put any mark-up or handling on its own taxable line.'));
				this._updateTotals();
			},
		});
	}

	// ------------------------------------------------------------- readiness

	_reflectReadiness() {
		const missing = [];
		const buyerOk = this.buyerType === 'business'
			? !!this._businessCustomer
			: !!this.fields.customer.get_value();
		if (!buyerOk) missing.push(__('Buyer'));
		// Recommended, not required: no email just means no receipt is sent.
		this.fields.email.set_description(this.fields.email.get_value() ? '' : __(
			'Recommended — without an email no receipt is sent.'));
		if (!this.lines.length) missing.push(__('At least one item'));
		if (this._hasRegulated() && !this.destinationFfl && !this._isPickup()) {
			// Typing the C&R block is not choosing a destination — the license
			// has to be saved before there is one. Name the button, or the
			// operator reads "Destination FFL" while looking at a filled form.
			missing.push(this._isCr() ? __('Destination FFL (click "Use this C&R license")') : __('Destination FFL'));
		}
		if (this._leExempt && !this.fields.le_exempt_buyer.get_value()) missing.push(__('Exempt buyer'));
		if (this._leExempt && !this._leCert) missing.push(__('CDTFA-230-FET certificate'));
		const tenderRef = this._tenderReference();
		if (tenderRef && !(this.fields.transaction_number.get_value() || '').trim()) {
			missing.push(tenderRef.missing);
		}
		const needsPlan = this.lines.some((l) => !l.channel_confirmed);
		if (needsPlan) {
			missing.push(__('Fulfillment plan (unconfirmed distributor lines)'));
		}
		// The submit button is DISABLED while lines are unconfirmed, so its
		// _openPlanner fallback is unreachable — the planner link must carry the
		// weight instead: promote it to a highlighted pill while planning is due.
		this.$body.find('#btn-plan').css(needsPlan
			? { background: 'var(--orange-100)', color: 'var(--orange-700)',
				padding: '2px 10px', 'border-radius': '10px', 'font-weight': '600' }
			: { background: '', color: '', padding: '', 'border-radius': '', 'font-weight': '' });

		const $btn = this.$body.find('#btn-submit');
		const $rd = this.$body.find('#readiness');
		if (missing.length) {
			$rd.text(__('Required: ') + missing.join(', ')).css('color', 'var(--text-muted)');
			$btn.prop('disabled', true);
		} else {
			$rd.text(this._editing ? __('Ready to save.') : __('Ready to create.')).css('color', 'var(--green-700)');
			$btn.prop('disabled', false);
		}
		this._updateTotals();
	}

	// ---------------------------------------------------------------- edit mode
	// Route /app/create-order/edit/<SI> drives edit mode: enter re-fetches the
	// invoice into the form and swaps in edit chrome; exit restores create mode.
	// Bound from on_page_show, so a browser refresh re-enters instead of leaving
	// a stale form that could double-create.

	_syncEditRoute() {
		const r = frappe.get_route(); // ['create-order'] | ['create-order','edit',SI]
		const si = r[1] === 'edit' ? r[2] : null;
		if (si && this._editing?.si !== si) this._enterEditMode(si);
		else if (!si && this._editing) this._exitEditMode();
	}

	_enterEditMode(si) {
		frappe.call({
			method: 'ffl_core.api.manual_order.get_order_for_edit',
			args: { sales_invoice: si },
			freeze: true, freeze_message: __('Loading order…'),
			callback: (r) => {
				const m = r.message;
				if (!m?.payload) return;
				this._resetForNext();
				// Set _editing BEFORE hydrating: _applyPayload re-validates serials and
				// must pass ignore_invoice=si, or the order's own held guns get rejected
				// as "already held" and stripped from the prefilled form.
				this._editing = { si, ctx: m.edit_ctx };
				this._applyPayload(m.payload);
				this._renderEditChrome();
			},
			error: () => frappe.set_route('pending-dispositions'),
		});
	}

	_exitEditMode() {
		this._editing = null;
		// Re-home the Order Note BEFORE removing #edit-note, or removing that
		// block would take the live control with it.
		this._restoreNoteHome();
		this.$body.find('#edit-banner, #edit-money, #edit-note').remove();
		this.fields.mode.$wrapper.closest('.section').show();
		this.$body.find('#seg-dispose').closest('.section').show();
		this.$body.find('#btn-save-draft, #btn-drafts').show();
		this._resetForNext(); // also restores the 'Create Order' button label
	}

	// The staff Order Note control lives in the Fulfillment section, which edit
	// mode hides (dispose is N/A while editing). Edit mode relocates the control
	// into a visible #edit-note block; this returns it to its markup home so
	// create mode — and the next _renderEditChrome — find it where they expect.
	// #dispose-note is static (never removed), so it's a stable re-home anchor.
	_restoreNoteHome() {
		this.$body.find('#dispose-note').after(this.$body.find('#f-note'));
	}

	_renderEditChrome() {
		// Idempotent — a re-enter (edit A → pending → edit B without a create-mode
		// exit) must not leave A's banner + money summary mounted. Re-home the note
		// control first so removing #edit-note never takes the live control with it.
		this._restoreNoteHome();
		this.$body.find('#edit-banner, #edit-money, #edit-note').remove();
		const ctx = this._editing.ctx;
		const esc = frappe.utils.escape_html;
		this.$body.prepend(`
			<div id="edit-banner" class="section" style="border-color: var(--orange-400); background: var(--orange-50, var(--card-bg));">
				<b>${__('Editing {0}', [esc(ctx.sales_invoice)])}</b> —
				${__('saving cancels it and creates a replacement invoice.')}
				<a href="#" id="edit-exit" style="float:right">${__('Exit without saving')}</a>
			</div>`);
		this.$body.find('#edit-exit').on('click', (e) => {
			e.preventDefault();
			frappe.set_route('create-order');
		});
		// Payments were already recorded on the original invoice — they carry
		// over server-side. Hide the payment + fulfillment sections and show a
		// read-only money summary instead.
		this.fields.mode.$wrapper.closest('.section').hide();
		this.$body.find('#seg-dispose').closest('.section').hide();
		this.$body.find('#btn-save-draft, #btn-drafts').hide();
		const paidLine = ctx.paid_total > 0
			? __('Paid so far: {0} ({1}) — carries to the edited order on save.',
				[format_currency(ctx.paid_total), esc(ctx.last_payment_mode || __('recorded payments'))])
			: __('Nothing paid yet.');
		this.$body.find('#edit-banner').after(`
			<div id="edit-money" class="section">
				<h3>${__('Money')}</h3>
				<div>${paidLine}</div>
				<div id="edit-delta" style="margin-top:6px"></div>
				<div id="edit-refund-controls" style="display:none; margin-top:8px; max-width:340px"></div>
			</div>`);
		this._refundModeCtl = this._f('#edit-refund-controls', {
			label: __('Refund Method'), fieldtype: 'Select', fieldname: 'refund_mode',
			options: ['Cash', 'Credit Card', 'Zelle', 'ACH', 'Check/Money Order'].join('\n'),
			change: () => this._syncEditDelta(),
		});
		this._refundModeCtl.set_value(ctx.last_payment_mode || 'Cash');
		this._refundRefCtl = this._f('#edit-refund-controls', {
			label: __('Refund Reference'), fieldtype: 'Data',
			fieldname: 'refund_reference', change: () => this._reflectReadiness(),
			description: __('Zelle / ACH: the confirmation number. Check/Money Order: the number of the check you wrote. Credit Card: the terminal approval code.'),
		});
		// Surface the staff Order Note (normally in the edit-hidden Fulfillment
		// section) right under the money summary, so an edit can read and change
		// it. Move the LIVE control — its value is already prefilled by
		// _applyPayload — and its own "Order Note (staff)" label heads the block.
		this.$body.find('#edit-money').after('<div id="edit-note" class="section"></div>');
		this.$body.find('#edit-note').append(this.$body.find('#f-note'));

		this.$body.find('#btn-submit').text(__('Save Changes'));
		this._syncEditDelta();
	}

	_currentCustomer() {
		// Same resolution _collectPayload uses.
		return this.buyerType === 'business'
			? this._businessCustomer : this.fields.customer.get_value();
	}

	_projectedRefund() {
		if (!this._editing) return 0;
		const ctx = this._editing.ctx;
		if (ctx.paid_total <= 0) return 0;
		if ((this._currentCustomer() || '') !== ctx.customer) return ctx.paid_total;
		return Math.max(0, ctx.paid_total - (this._lastTotal || 0));
	}

	_syncEditDelta() {
		if (!this._editing) return;
		const ctx = this._editing.ctx;
		const $d = this.$body.find('#edit-delta');
		const refund = this._projectedRefund();
		const swapped = (this._currentCustomer() || '') !== ctx.customer;
		let html = '';
		if (ctx.paid_total > 0 && swapped) {
			html = `<span style="color:var(--orange-600)">${__('Buyer changed — the full {0} refunds to {1}; the new order starts unpaid.',
				[format_currency(ctx.paid_total), frappe.utils.escape_html(ctx.customer_name || ctx.customer)])}</span>`;
		} else if (refund > 0.01) {
			html = `<span style="color:var(--orange-600)">${__('New total is lower — {0} will be refunded.', [format_currency(refund)])}</span>`;
		} else if (ctx.paid_total > 0 && (this._lastTotal || 0) - ctx.paid_total > 0.01) {
			html = __('Balance to collect after save: {0}', [format_currency(this._lastTotal - ctx.paid_total)]);
		}
		$d.html(html);
		this.$body.find('#edit-refund-controls').toggle(refund > 0.01);
		this._refundRefCtl?.$wrapper.toggle(
			refund > 0.01 && this._refundNeedsReference());
	}

	// Mirrors update_order's refund gate: Zelle / ACH / Check/Money Order always,
	// the card on a standalone terminal with approval codes required.
	_refundNeedsReference() {
		const tr = window.ffl && window.ffl.tenderReference;
		const modes = tr ? tr.modes() : ['Zelle', 'ACH', 'Check/Money Order'];
		return modes.includes(this._refundModeCtl?.get_value());
	}

	_submitEdit() {
		const payload = this._collectPayload();
		const args = {
			sales_invoice: this._editing.si,
			payload: JSON.stringify(payload),
		};
		const refund = this._projectedRefund();
		if (refund > 0.01) {
			args.refund_mode = this._refundModeCtl.get_value();
			args.refund_reference = (this._refundRefCtl.get_value() || '').trim();
			if (this._refundNeedsReference() && !args.refund_reference) {
				frappe.msgprint(__('Enter the {0} reference for the refund.', [args.refund_mode]));
				return;
			}
		}
		const $btn = this.$body.find('#btn-submit');
		$btn.prop('disabled', true).text(__('Saving…'));
		frappe.call({
			method: 'ffl_core.api.manual_order.update_order',
			args,
			callback: (r) => {
				const m = r.message;
				if (!m?.ok) return;
				const bits = [
					__('Replaced {0} with {1}.', [m.replaced, `<a href="/app/sales-invoice/${m.sales_invoice}">${m.sales_invoice}</a>`]),
					m.carried_amount > 0 ? __('Carried payment: {0}.', [format_currency(m.carried_amount)]) : '',
					m.refund_entry ? __('Refunded {0} ({1}).', [format_currency(m.refunded_amount), m.refund_entry]) : '',
					m.outstanding > 0 ? __('Outstanding: {0} — collect via Record Payment.', [format_currency(m.outstanding)]) : '',
				].filter(Boolean);
				frappe.msgprint({ title: __('Order Updated'),
					message: bits.join('<br>'), indicator: 'green' });
				this._exitEditMode();
				frappe.set_route('pending-dispositions');
			},
			error: () => { $btn.prop('disabled', false).text(__('Save Changes')); },
		});
	}

	_renameCustomerDialog() {
		const cust = this._currentCustomer();
		if (!cust) {
			frappe.show_alert({ message: __('Pick a customer first.'), indicator: 'orange' });
			return;
		}
		const d = new frappe.ui.Dialog({
			title: __('Fix Customer Name'),
			fields: [
				{ fieldtype: 'HTML', fieldname: 'note', options:
					`<div class="text-muted" style="margin-bottom:8px">${__('Renames {0} everywhere — use this for spelling fixes, not to switch buyers (pick a different customer for that).', [frappe.utils.escape_html(cust)])}</div>` },
				{ fieldtype: 'Data', fieldname: 'new_name', label: __('Correct Name'), reqd: 1, default: cust },
			],
			primary_action_label: __('Rename'),
			primary_action: (v) => {
				frappe.call({
					method: 'ffl_core.api.manual_order.rename_customer',
					args: { customer: cust, new_name: v.new_name },
					freeze: true,
					callback: (r) => {
						d.hide();
						if (!r.message?.name) return;
						if (this.buyerType !== 'business') {
							this.fields.customer.set_value(r.message.name);
						}
						// The rename changes the Customer PK system-wide; a stale
						// business handle would send a dead PK in the edited payload.
						if (this.buyerType === 'business' && this._businessCustomer) {
							this._businessCustomer = r.message.name;
						}
						// Keep the edit context in step so the delta doesn't read a
						// phantom buyer swap after a spelling fix.
						if (this._editing) {
							this._editing.ctx.customer = r.message.name;
							this._editing.ctx.customer_name = r.message.customer_name;
							this._syncEditDelta?.();
						}
						frappe.show_alert({ message: __('Customer renamed.'), indicator: 'green' });
					},
				});
			},
		});
		d.show();
	}

	// ---------------------------------------------------------------- submit

	_submit() {
		if (this.lines.some((l) => !l.channel_confirmed)) {
			this._openPlanner();
			return;
		}
		if (this._editing) { this._submitEdit(); return; }
		// Same collector the Save Draft button uses — one payload shape. Only
		// business buyers carry the exemption fields (sending them persists the
		// customer's standing flag; individuals' untouched flag is never written).
		const payload = this._collectPayload();
		if (this._draftName) payload.draft = this._draftName;
		// The basis this page priced the shipping on: the server refuses the order
		// if FFL Settings changed since, instead of booking a different total.
		payload.shipping_tax_inclusive = this._shipTaxIncluded() ? 1 : 0;
		// Terminal path: the order books on account and the card is run against
		// the new invoice below (see _terminalPath). Dispose & Ship Now needs full
		// payment BEFORE submit, which a not-yet-charged card cannot satisfy — the
		// order ships from Pending Order once the approval lands.
		const terminalAmount = this._terminalPath() ? (flt(payload.paid_amount) || 0) : 0;
		if (this._terminalPath()) {
			payload.payment_received = 0; payload.paid_amount = 0; payload.transaction_number = '';
			payload.dispose_now = 0;
		}
		const $btn = this.$body.find('#btn-submit');
		$btn.prop('disabled', true).text(__('Creating…'));
		frappe.call({
			method: 'ffl_core.api.manual_order.create_order',
			args: { payload: JSON.stringify(payload) },
			callback: async (r) => {
				const m = r.message;
				if (m?.ok) {
					const so = m.sales_order, si = m.sales_invoice;
					let pay = m.outstanding > 0
						? __('On account — outstanding {0}', [format_currency(m.outstanding)])
						: __('Paid {0}', [format_currency(m.paid_amount)]);
					let retryCard = null;
					if (terminalAmount > 0 && m.outstanding > 0) {
						const card = await this._chargeOnTerminal(si, Math.min(terminalAmount, m.outstanding), m.outstanding);
						pay = card.text;
						retryCard = card.retry;
					}
					let ship = m.disposed
						? __('Disposed & shipping; FastBound push queued.')
						: __('Queued for <a href="/app/pending-dispositions">Pending Dispositions</a>.');
					if (m.ship_pushed) {
						ship += ' ' + __('Pushed to ShipStation — buy the label there.');
					} else if (m.disposed && m.ship_error) {
						ship += ` <span style="color:var(--orange-600)">${__('ShipStation push failed — use Ship on the Pending Order page.')}</span>`;
					}
					// Dedicated dialog instead of the shared frappe.msgprint msg_dialog:
					// saving the SI auto-creates an Item Price whose _server_messages
					// were clobbering the success body, leaving the dialog blank. The
					// primary action prints the invoice (browser print view).
					const resultDialog = new frappe.ui.Dialog({
						title: __('Order Created'),
						primary_action_label: __('Print Invoice'),
						primary_action: () => window.open(`/app/print/Sales Invoice/${encodeURIComponent(si)}`, '_blank'),
						// A declined / cancelled terminal charge: try the card again
						// from here (the order already exists — nothing is re-created).
						...(retryCard ? { secondary_action_label: __('Retry card'), secondary_action: async () => {
							const card = await retryCard();
							resultDialog.$body.find('.pay-line').html(card.text);
							if (!card.retry) resultDialog.get_secondary_btn().hide();
						} } : {}),
					});
					resultDialog.$body.html(`
						<p><span class="indicator-pill green"></span> ${__('Sales Order')}: <a href="/app/sales-order/${so}">${so}</a></p>
						<p>${__('Sales Invoice')}: <a href="/app/sales-invoice/${si}">${si}</a></p>
						<p class="pay-line">${pay}</p>
						<p>${ship}</p>
						${m.email_to ? `<p>${__('Receipt emailed to {0}.', [frappe.utils.escape_html(m.email_to)])}</p>` : ''}`);
					resultDialog.show();
					// The consumed draft (if any) was deleted server-side.
					this._draftName = null;
					this._refreshDraftCount();
					this._resetForNext();
				}
			},
			error: () => { $btn.prop('disabled', false).text(__('Create Order')); },
		});
	}

	// Run the card on the terminal against the new Sales Invoice (the server
	// books the Payment Entry on approval). Returns {text, retry}: the payment
	// line for the result dialog and, when the charge did not go through, a
	// function that runs it again for the same amount.
	async _chargeOnTerminal(si, amount, outstanding) {
		const outcome = (res) => {
			if (res.skip) {
				return { text: __('On account — outstanding {0}. Terminal not configured: record the card payment from Pending Order.', [format_currency(outstanding)]), retry: null };
			}
			if (res.ok) {
				return { text: res.paid
					? __('Paid {0} on the terminal (auth {1}).', [format_currency(res.amount), res.approval_code || res.unique_reference || 'OK'])
					: __('Deposit {0} on the terminal (auth {1}) — {2} still due.', [format_currency(res.amount), res.approval_code || res.unique_reference || 'OK', format_currency(res.outstanding)]),
					retry: null };
			}
			return { text: `<span style="color:var(--orange-700,#92400e)">${__('Card not charged — on account, {0} outstanding. Retry the card here or collect it from Pending Order → Record Payment.', [format_currency(outstanding)])}</span>`,
				retry: () => this._chargeOnTerminal(si, amount, outstanding) };
		};
		if (!(window.ffl && window.ffl.terminalCharge)) return outcome({ skip: true });
		const res = await window.ffl.terminalCharge({
			amount,
			start: { method: 'ffl_integrations.payments.api.charge_sales_invoice', args: { sales_invoice: si, amount } },
			poll: { method: 'ffl_integrations.payments.api.poll_sales_invoice_charge', args: { sales_invoice: si } },
			cancel: { method: 'ffl_integrations.payments.api.cancel_sales_invoice_charge', args: { sales_invoice: si } },
		});
		return outcome(res || { ok: false });
	}

	_resetForNext() {
		this._locCache = {}; // a new order asks the shelves afresh: stock and locations move
		this.lines = []; this.pending = null; this.destinationFfl = null; this._businessCustomer = null;
		this.deliveryOptions = {};
		this.buyerType = 'individual'; this.disposeNow = false; this._amountEdited = false;
		this._draftName = null;
		// Clearing amount to '' matches _lastAutoAmount ('' → 0), so the async change
		// handler won't mistake the reset for a user edit and re-pin _amountEdited.
		this._lastAutoAmount = '';
		this._clearTaxExempt();
		this._setLeExempt(false);
		['customer', 'email', 'phone', 'biz_ffl', 'dest_ffl', 'recipient_name', 'recipient_phone',
			'ship_line1', 'ship_line2', 'ship_city', 'ship_state', 'ship_zip', 'scan', 'amount', 'shipping', 'note',
			'cr_license_number', 'cr_license_expiry', 'cr_contact_name', 'cr_premise_street',
			'cr_premise_street2', 'cr_premise_city', 'cr_premise_state', 'cr_premise_zip']
			.forEach((k) => this.fields[k]?.set_value(''));
		this.fields.cr_transfer?.set_value(0);
		this._syncCrSection();
		this.fields.pay_later.set_value(0);
		this.fields.charge_terminal.set_value(1);
		this.fields.pickup?.set_value(0);
		this.fields.pickup?.$input?.prop('disabled', false);
		this.fields.mode.set_value('Cash');
		this._lockShipTo(false);
		// Restore the Buyer-Type and Fulfillment toggles to their defaults.
		this.$body.find('#seg-buyer button').removeClass('active');
		this.$body.find('#seg-buyer button[data-v="individual"]').addClass('active');
		this.$body.find('#buyer-individual').show();
		this.$body.find('#buyer-business').hide();
		this.$body.find('#seg-dispose button').removeClass('active');
		this.$body.find('#seg-dispose button[data-v="later"]').addClass('active');
		this.$body.find('#dispose-note').text(__('Defer: the order joins the Pending Dispositions queue; the gun stays in stock and on the bound book until you ship it.'));
		this.$body.find('#pending-area, #biz-ffl-card, #dest-ffl-card, #shipto-note').empty();
		this._renderLines();
		this._syncDestSection();
		this._reflectReadiness();
		this.$body.find('#btn-submit').text(__('Create Order'));
	}

	// ------------------------------------------------- peace-officer exemption
	// The sale's own flag (compliance/le_excise_exempt): excise off, sales tax
	// stays. The certificate is attached to the buyer's Customer record and
	// required — the server refuses the order without it.

	_onLeExemptChange() {
		const on = !!this.fields.le_excise_exempt.get_value();
		// A control's change fires after set_value settles, so _setLeExempt's own
		// write comes back here: only a real flip goes on.
		if (on === !!this._leExempt) return;
		this._setLeExempt(on, this.fields.le_exempt_buyer.get_value(), this._leCert);
	}

	_setLeExempt(on, buyer, cert) {
		const changed = on !== !!this._leExempt;
		this._leExempt = on;
		this._leCert = on ? (cert || null) : null;
		const setIf = (field, v) => { if ((this.fields[field].get_value() || '') !== v) this.fields[field].set_value(v); };
		setIf('le_excise_exempt', on ? 1 : '');
		setIf('le_exempt_buyer', on ? (buyer || '') : '');
		this.$body.find('#le-exempt-detail').toggle(on);
		this._renderLeCert();
		if (changed) this._itemRates = {}; // per-item rates differ with the excise off
		this._updateTotals();
		this._reflectReadiness();
	}

	_renderLeCert() {
		const $a = this.$body.find('#le-cert-area').empty();
		if (!this._leExempt) return;
		if (this._leCert) {
			$a.html(`${__('CDTFA-230-FET')}: <a href="${frappe.utils.escape_html(this._leCert)}" target="_blank">${__('view')}</a> · <a href="#" id="le-reupload">${__('replace')}</a>`);
			$a.find('#le-reupload').on('click', (e) => { e.preventDefault(); this._uploadLeCert(); });
		} else {
			$a.html(`<button class="btn btn-xs btn-default" id="le-upload">${__('Upload CDTFA-230-FET')}</button> <span class="text-muted">${__('(required)')}</span>`);
			$a.find('#le-upload').on('click', () => this._uploadLeCert());
		}
	}

	_uploadLeCert() {
		const cust = this.buyerType === 'business' ? this._businessCustomer : this.fields.customer.get_value();
		if (!cust) {
			frappe.show_alert({ message: __('Pick the buyer first — the certificate is kept on their record.'), indicator: 'orange' });
			return;
		}
		new frappe.ui.FileUploader({
			doctype: 'Customer', docname: cust, allow_multiple: false,
			restrictions: { allowed_file_types: ['.pdf', '.png', '.jpg', '.jpeg'] },
			on_success: (file_doc) => {
				this._leCert = file_doc.file_url;
				this._renderLeCert();
				this._reflectReadiness();
			},
		});
	}

	// ------------------------------------------------------------- tax exempt

	_loadBizTaxStatus() {
		const cust = this._businessCustomer;
		this.$body.find('#biz-tax').toggle(!!cust);
		if (!cust) { this._clearTaxExempt(); return; }
		frappe.call({
			method: 'ffl_core.api.manual_order.get_customer_tax_status',
			args: { customer: cust },
			callback: (r) => {
				const m = r.message || {};
				this._taxExempt = !!m.is_tax_exempt;
				this._taxCert = m.certificate || null;
				this._settingExempt = true;
				this.fields.tax_exempt.set_value(this._taxExempt ? 1 : 0);
				this._settingExempt = false;
				this._renderTaxCert();
				this._updateTotals();
				this._reflectReadiness();
			},
		});
	}

	_renderTaxCert() {
		const $a = this.$body.find('#tax-cert-area').empty();
		if (!this.fields.tax_exempt.get_value()) return;
		if (this._taxCert) {
			$a.html(`${__('Certificate on file')}: <a href="${frappe.utils.escape_html(this._taxCert)}" target="_blank">${__('view')}</a> · <a href="#" id="reupload-cert">${__('replace')}</a>`);
			$a.find('#reupload-cert').on('click', (e) => { e.preventDefault(); this._uploadCert(); });
		} else {
			$a.html(`<button class="btn btn-xs btn-default" id="upload-cert">${__('Upload Certificate')}</button> <span class="text-muted">${__('(optional)')}</span>`);
			$a.find('#upload-cert').on('click', () => this._uploadCert());
		}
	}

	_onTaxExemptChange() {
		if (this._settingExempt) return;
		const on = !!this.fields.tax_exempt.get_value();
		if (on && !this._businessCustomer) {
			this._revertExemptCheckbox();
			return;
		}
		// Checking the box exempts the order — the certificate is OPTIONAL (upload
		// it via the cert area if available). Preview only — the standing Customer
		// flag is persisted by create_order, not on this toggle.
		this._taxExempt = on;
		this._renderTaxCert();
		this._updateTotals();
		this._reflectReadiness();
	}

	_revertExemptCheckbox() {
		this._taxExempt = false;
		this._settingExempt = true;
		this.fields.tax_exempt.set_value(0);
		this._settingExempt = false;
	}

	// Lock (firearm ships to the FFL) or unlock the ship-to address fields.
	_lockShipTo(locked) {
		['recipient_name', 'recipient_phone', 'ship_line1', 'ship_line2', 'ship_city', 'ship_state', 'ship_zip']
			.forEach((k) => this.fields[k].$input?.prop('readonly', locked));
	}

	// Reset the tax-exempt preview state + UI (no DB write — that's create_order).
	_clearTaxExempt() {
		this._taxCert = null;
		this._revertExemptCheckbox();
		this.$body.find('#biz-tax').hide();
		this.$body.find('#tax-cert-area').empty();
	}

	_uploadCert() {
		const cust = this._businessCustomer;
		if (!cust) return;
		new frappe.ui.FileUploader({
			doctype: 'Customer', docname: cust, allow_multiple: false,
			restrictions: { allowed_file_types: ['.pdf', '.png', '.jpg', '.jpeg'] },
			on_success: (file_doc) => {
				this._taxCert = file_doc.file_url;
				this._taxExempt = true;
				this._settingExempt = true;
				this.fields.tax_exempt.set_value(1);
				this._settingExempt = false;
				this._renderTaxCert();
				this._updateTotals();
				this._reflectReadiness();
				frappe.show_alert({ message: __('Certificate uploaded — the exemption applies when you create the order.'), indicator: 'green' });
			},
		});
	}

}
