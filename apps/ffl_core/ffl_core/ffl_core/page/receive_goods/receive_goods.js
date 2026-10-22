// Receive Goods — single-page scanner-driven workflow.
//
// Flow:
//   1. Pick supplier (Link to Supplier; FFL gate enforced server-side).
//   2. Date is prefilled to now.
//   3. Scan or type barcode → backend resolves to Item.
//      - If item is a firearm, a per-serial textarea appears.
//      - Else, just qty + rate.
//   4. Click "Add to Receipt" → line appended; scanner re-focused.
//   5. Click "Submit & Receive" → backend creates + submits PR.
//      Existing PR hooks fire: FFL gate, Acquisition creation, FastBound enqueue.
//      Bought from a private seller: one confirmation first, how the seller was
//      paid (Cash already picked), booked with the receipt.

// The zero-cost tiers below are a PURE KERNEL (no DOM, no frappe, no __()),
// unit-tested under Node in tests/js/receive_zero_cost.test.js — same split as
// the Sales Report column picker. The `rg`/`RG_` prefix keeps these out of the
// way of the other page scripts, which share one global scope.
//
// The SERVER is the authority (ffl_core.api.receive_goods._resolve_line_cost
// re-runs every one of these checks and refuses what this cannot see). This copy
// exists so the receiver learns about a bad cost while the card is still open,
// instead of after Submit — never so the page can decide the server would agree.

// Mirrors ffl_core.setup.zero_cost.ZERO_COST_REASONS / OTHER_REASON. Mirrored
// rather than fetched, the same way this page already mirrors the Suffix and
// firearm-Type vocabularies: the server owns the list (it refuses anything
// outside it), and a dropdown that can fail to load is a dropdown that can make
// the only legal path for a genuinely-free gun unreachable. Drift is pinned by
// tests/test_receive_goods_front_end.py, which reads both sides.
const RG_ZERO_COST_REASONS = [
	'Supplier gift',
	'Promotional item',
	'Warranty replacement',
	'Sample',
	'Other',
];
// The one reason that cannot stand alone (server: zero_cost.OTHER_REASON).
const RG_ZERO_COST_OTHER = 'Other';

// Mirrors ffl_core.firearm.CONSIGNMENT_ACQUISITION_TYPES. NOT `=== 'Consignment'`:
// the Acquisition Type dropdown is filled at runtime from FastBound's SmartList
// (_loadAcquireTypes), so an equality here stops matching the day the spelling
// gains a capital or a space — and on this page that would ask a consignor's
// legitimately-$0 gun to be "confirmed free", which it is not. Fixed set rather
// than a substring test, for the same reason the server's frozenset is one:
// "Consignment Out" is a disposition of OUR stock and must never read as "a
// consignor owns this".
const RG_CONSIGNMENT_ACQUISITION_TYPES = ['consignment'];

function rgIsConsignmentAcquisitionType(value) {
	return RG_CONSIGNMENT_ACQUISITION_TYPES.includes(
		String(value === null || value === undefined ? '' : value).trim().toLowerCase());
}

// What (if anything) is wrong with this line's cost — '' when the server will
// take it, otherwise a code the page turns into wording (_costGapMessage).
//
// `ctx` is {is_transfer, acquisition_type}. The arms mirror the server's, minus
// the one this page cannot produce: the server also has an "absent rate" arm for
// payload callers (the MCP tool, the distributor bridge), while the Cost box
// here always yields a number — an empty box arrives as 0 and lands on the
// confirm-or-enter arm below. Unreadable input is coerced the way the server's
// flt() coerces it: to 0, which is the arm that asks rather than the one that
// waves through.
function rgLineCostGap(line, ctx) {
	// A customer's gun is received into custody at $0 by the server whatever the
	// card says, so there is nothing to confirm and nothing to ask for.
	if (ctx && ctx.is_transfer) return '';
	const rate = parseFloat(line.rate) || 0;
	const confirmed = line.zero_cost_confirmed ? 1 : 0;
	const reason = (line.zero_cost_reason || '').trim();
	const note = (line.zero_cost_note || '').trim();
	if (rate < 0) return 'negative';
	// A price and an "it was free" tick contradict each other; trust neither.
	if (rate > 0) return confirmed ? 'cost_with_free_tick' : '';
	// rate == 0 — the fork this whole guard exists for. The store never bought a
	// consignor's gun, so $0 there is the fact, not an omission: that line
	// stays submittable untouched, which is the constraint the original code
	// declined to add a blanket "cost > 0" rule for.
	if (rgIsConsignmentAcquisitionType(ctx && ctx.acquisition_type)) return '';
	if (!confirmed) return 'unconfirmed_zero';
	if (!reason) return 'no_reason';
	// Membership is not re-checked here: the dropdown can only offer the list
	// above. The server checks it, for the callers that are not this page.
	if (reason === RG_ZERO_COST_OTHER && !note) return 'other_needs_note';
	return '';
}

// Scan-first receiving: every line belongs to a bucket —
// an open Distributor Order ({kind: 'do', name}: the goods are held for that order),
// an outside-POS Distributor Invoice ({kind: 'invoice', name}) or plain 'stock'.
// Pure, so the per-bucket submit can be unit-tested.
function rgBucketKey(bucket) {
	return bucket && bucket !== 'stock' ? `${bucket.kind}:${bucket.name}` : 'stock';
}

function rgBucketOf(line) {
	return line && line.bucket ? rgBucketKey(line.bucket) : 'stock';
}

// A candidate (match_incoming's, or the one a staged row carries as do_meta) is an
// order's unless it says it is an invoice's.
function rgCandKind(cand) {
	return cand && cand.kind === 'invoice' ? 'invoice' : 'do';
}

function rgCandBucket(cand) {
	return rgCandKind(cand) === 'invoice'
		? { kind: 'invoice', name: cand.invoice_name } : { kind: 'do', name: cand.do_name };
}

function rgCandLabel(cand) {
	return (cand && (cand.label || cand.do_name)) || '';
}

// Units the session already holds for an order/invoice line: confirmed rows (a staged
// row nobody has scanned yet does not count) plus the card being filled in.
function rgSessionQtyFor(lines, pending, lineName) {
	const inLines = (lines || [])
		.filter((l) => l.do_line === lineName && !(l.staged && !l.ticked))
		.reduce((s, l) => s + (l.qty || 0), 0);
	const inPending = (pending && pending.do_line === lineName) ? (pending.qty || 0) : 0;
	return inLines + inPending;
}

// The staged outside-POS invoice line a scan of this item belongs to: the next
// unconfirmed staged row (guns are one row per unit), else — once those are ticked —
// the same line again while it still has units outstanding beyond what the session
// already holds for it, so later scans keep counting up on the invoice instead of
// falling to plain stock or another order. null when there is none (or it is full).
function rgInvoiceCandidate(lines, pending, itemCode) {
	const rows = (lines || []).filter((l) => l.item_code === itemCode
		&& l.do_meta && rgCandKind(l.do_meta) === 'invoice');
	const open = rows.find((l) => l.staged && !l.ticked);
	if (open) return open.do_meta;
	// Every staged invoice line of the item (one invoice may list it twice, two
	// invoices may carry it), the first that still has room.
	const seen = new Set();
	for (const l of rows) {
		const cand = l.do_meta;
		if (seen.has(cand.line_name)) continue;
		seen.add(cand.line_name);
		if (rgHasRoom(cand, lines, pending)) return cand;
	}
	return null;
}

// Does this order/invoice line still have units beyond what the session holds for it?
function rgHasRoom(cand, lines, pending) {
	return !!cand && cand.outstanding - rgSessionQtyFor(lines, pending, cand.line_name) > 0;
}

// Does this Item / line take a serial per unit? A firearm always does, and so does any
// serial-tracked Item (create_receive counts serials for has_serial_no).
function rgNeedsSerials(x) {
	return !!(x && (x.is_firearm || x.has_serial_no));
}

// What is wrong with these serials for this Item / line at this qty, as a code the page
// turns into words: 'need_count' (serial-tracked: one per unit), 'used_qty' / 'used_serial'
// (a firearm that is not serial-tracked is one unit, one serial); '' when fine.
function rgSerialGap(x, qty, serials) {
	if (!rgNeedsSerials(x)) return '';
	if (x.has_serial_no) return serials.length !== Math.round(qty) ? 'need_count' : '';
	if (qty !== 1) return 'used_qty';
	return serials.length !== 1 ? 'used_serial' : '';
}

// Apply a saved line-editor form to a session line. Saving IS an answer: the row is
// marked `edited`, so a staged row the clerk has opened is no longer "never asked"
// (rgBucketItem then sends its antique answer). `edit.serials` undefined = the form has
// no serial box for this line: what the line holds is kept, never dropped.
function rgApplyLineEdit(line, edit) {
	line.qty = edit.qty;
	line.rate = edit.rate;
	if (edit.zero) {
		line.zero_cost_confirmed = edit.zero.zero_cost_confirmed || 0;
		line.zero_cost_reason = edit.zero.zero_cost_reason || '';
		line.zero_cost_note = edit.zero.zero_cost_note || '';
	}
	if (edit.sell_price !== undefined) line.sell_price = edit.sell_price;
	if (edit.consignment_commission_pct !== undefined) {
		line.consignment_commission_pct = edit.consignment_commission_pct;
	}
	if (edit.serials !== undefined) line.serials = edit.serials;
	if (edit.service_need !== undefined) line.service_need = edit.service_need;
	if (edit.fa) Object.assign(line, edit.fa);
	line.edited = true;
	return line;
}

// One item of an order/invoice bucket's receive call. The antique flag is the
// receiver's answer, not a default: a row that was only staged (never scanned or
// edited) has not been asked, and sending its blank 0 would clear an antique Item's
// flag. A row the clerk opened and saved in the line editor (`edited`) has answered.
// Everything else is what the stock bucket sends.
function rgBucketItem(l) {
	return {
		line_name: l.do_line, qty: l.qty, serials: l.serials, rate: l.rate,
		manufacturer: l.manufacturer || '', model_name: l.model_name || '',
		firearm_type: l.firearm_type || '', caliber: l.caliber || '', importer: l.importer || '',
		service_need: l.service_need ? 1 : 0,
		zero_cost_confirmed: l.zero_cost_confirmed ? 1 : 0,
		zero_cost_reason: l.zero_cost_reason || '', zero_cost_note: l.zero_cost_note || '',
		sell_price: l.sell_price || 0,
		storage_location: l.storage_location || '',
		...(l.staged && !l.ticked && !l.edited ? {} : { is_antique: l.is_antique ? 1 : 0, year_made: l.year_made || 0 }),
	};
}

// The slot-filling rows of a receipt, in order, for one plan_slots call: a gun line with a
// start location fills it and the next free slots, one per serial.
function rgSlotRows(lines) {
	const rows = [];
	(lines || []).forEach((l, i) => {
		if (!l.storage_location) return;
		const guns = (l.serials || []).length || 0;
		if (guns) rows.push({ key: `line${i}`, start: l.storage_location, count: guns });
	});
	return rows;
}

function rgGroupByBucket(lines) {
	const out = {};
	(lines || []).forEach((l) => {
		const b = rgBucketOf(l);
		(out[b] = out[b] || []).push(l);
	});
	return out;
}

// Guarded so Node can require the kernel above. Harmless in the browser, where
// frappe is always defined (same device as sales_report.js).
if (typeof frappe !== 'undefined') {
	frappe.pages['receive-goods'].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __('Receive Goods'),
			single_column: true,
		});

		wrapper.receive_goods_app = new ReceiveGoodsApp(page);
	};
	// The Distributor Order form hands a firearm order over with
	// route_options.distributor_order; the page stays loaded between visits, so
	// the hand-over is read on every show, not only on load.
	frappe.pages['receive-goods'].on_page_show = function (wrapper) {
		wrapper.receive_goods_app?._stageFromRoute();
	};
}


class ReceiveGoodsApp {
	constructor(page) {
		this.page = page;
		this.lines = [];
		this.pending = null;
		// choices[item_code] = the order candidate picked for that item this
		// session, or 'stock' — ten magazines ask once.
		this.session = { choices: {} };
		this.$body = $(`
			<div class="ffl-receive">
				<style>
					/* The incoming panel lives in a Dialog, which is appended to <body> —
					   outside .ffl-receive — so it carries its own class. */
					.ffl-incoming .incoming-group { margin-bottom: 10px; }
					.ffl-incoming .incoming-group > .head { display: flex; justify-content: space-between; align-items: center; padding: 4px 0; }
					.ffl-incoming .incoming-order { display: flex; gap: 8px; align-items: center; padding: 4px 0; border-top: 1px solid var(--border-color); font-size: 12px; }
					.ffl-incoming .incoming-order .grow { flex: 1; }
					.ffl-receive .line-summary.staged td { opacity: .6; }
					.ffl-receive .bucket-tag { font-size: 11px; color: var(--green-700); }
					.ffl-receive { max-width: 900px; margin: 0 auto; padding: 4px 0 80px; }
					.ffl-receive .section {
						background: var(--card-bg);
						border: 1px solid var(--border-color);
						border-radius: 8px;
						padding: 18px 20px;
						margin-bottom: 14px;
					}
					.ffl-receive .section h3 {
						margin: 0 0 14px 0;
						font-size: 15px;
						font-weight: 600;
					}
					.ffl-receive .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
					.ffl-receive .row.cols-3 { grid-template-columns: 2fr 1fr 1fr; }
					.ffl-receive .lines {
						margin-top: 12px;
						border: 1px solid var(--border-color);
						border-radius: 6px;
						overflow: hidden;
					}
					.ffl-receive .lines table { width: 100%; border-collapse: collapse; }
					.ffl-receive .lines th, .ffl-receive .lines td {
						padding: 8px 10px; border-bottom: 1px solid var(--border-color);
						font-size: 13px; vertical-align: top;
					}
					.ffl-receive .lines th { background: var(--bg-light-gray); text-align: left; font-weight: 600; }
					.ffl-receive .lines tr:last-child td { border-bottom: none; }
					.ffl-receive .lines .firearm-mark {
						display: inline-block; background: var(--blue-100); color: var(--blue-700);
						padding: 1px 6px; border-radius: 4px; font-size: 11px; margin-right: 6px;
					}
					.ffl-receive .lines .serials { color: var(--text-muted); font-size: 11px; margin-top: 2px; }
					.ffl-receive .lines .right { text-align: right; }
					.ffl-receive .pending {
						background: var(--highlight-color);
						border: 1px dashed var(--blue-300);
						border-radius: 6px;
						padding: 14px;
						margin-top: 12px;
					}
					.ffl-receive .pending .row { margin-bottom: 8px; }
					.ffl-receive .total-bar {
						display: flex; justify-content: space-between; align-items: center;
						padding: 12px 0; font-size: 14px; font-weight: 600;
						border-top: 1px solid var(--border-color); margin-top: 10px;
					}
					.ffl-receive .total-bar .total { font-size: 18px; }
					.ffl-receive .submit-bar {
						position: sticky; bottom: 0;
						background: var(--card-bg);
						border-top: 1px solid var(--border-color);
						padding: 12px 0;
						margin-top: 18px;
						display: flex; justify-content: space-between; align-items: center;
						gap: 12px;
					}
					.ffl-receive .scanner-input input { font-size: 16px !important; }
					.ffl-receive .helper { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
					.ffl-receive .btn-remove {
						background: none; border: none; color: var(--red-500);
						cursor: pointer; padding: 0 4px;
					}
					.ffl-receive .scan-wrap { position: relative; }
					.ffl-receive .scan-input { position: relative; }
					.ffl-receive .typeahead {
						position: absolute;
						top: 100%; left: 0; right: 0;
						z-index: 100;
						background: var(--card-bg);
						border: 1px solid var(--border-color);
						border-radius: 6px;
						box-shadow: 0 8px 24px rgba(0,0,0,0.08);
						max-height: 320px;
						overflow-y: auto;
						margin-top: 4px;
						display: none;
					}
					.ffl-receive .typeahead-row {
						padding: 8px 12px;
						cursor: pointer;
						display: grid;
						grid-template-columns: 1fr auto;
						gap: 8px;
						align-items: center;
						border-bottom: 1px solid var(--border-color);
					}
					.ffl-receive .typeahead-row:last-child { border-bottom: none; }
					.ffl-receive .typeahead-row:hover,
					.ffl-receive .typeahead-row.active {
						background: var(--fg-hover-color);
					}
					.ffl-receive .typeahead-row .name { font-weight: 500; }
					.ffl-receive .typeahead-row .meta {
						font-size: 11px;
						color: var(--text-muted);
					}
					.ffl-receive .typeahead-row .right { text-align: right; }
					.ffl-receive .typeahead-row .firearm-pill {
						background: var(--blue-100);
						color: var(--blue-700);
						padding: 1px 6px;
						border-radius: 4px;
						font-size: 10px;
						margin-right: 6px;
					}
					.ffl-receive .typeahead-empty {
						padding: 14px; color: var(--text-muted); font-size: 12px; text-align: center;
					}
					.ffl-receive .typeahead-create { background: var(--bg-green); }
					.ffl-receive .typeahead-create:hover { background: var(--bg-green); }
					.ffl-receive .typeahead-row .thumb {
						width: 36px; height: 36px; object-fit: cover;
						border-radius: 4px; border: 1px solid var(--border-color);
						margin-right: 10px; background: var(--bg-light-gray);
					}
					.ffl-receive .typeahead-row .thumb-wrap { display: flex; align-items: center; }
					.ffl-receive .typeahead-row .rsr-pill {
						background: var(--orange-100, #fde68a);
						color: var(--orange-700, #92400e);
						padding: 1px 6px; border-radius: 4px; font-size: 10px; margin-right: 6px;
					}
					.ffl-receive .typeahead-section-divider {
						padding: 6px 12px;
						font-size: 10px;
						font-weight: 600;
						color: var(--text-muted);
						background: var(--bg-light-gray);
						text-transform: uppercase;
						letter-spacing: 0.5px;
					}
					.ffl-receive .line-summary { cursor: pointer; }
					.ffl-receive .line-summary:hover td { background: var(--fg-hover-color); }
					.ffl-receive .line-summary.editing td { background: var(--highlight-color); }
					.ffl-receive .line-edit-row td {
						background: var(--highlight-color);
						padding: 14px 16px !important;
						border-top: none !important;
					}
					.ffl-receive .line-edit-row .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
					.ffl-receive .line-edit-row .actions {
						display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px;
					}
				</style>

				<!-- 1. SUPPLIER & SOURCE -->
				<div class="section">
					<h3>1. ${__('Supplier & Source')}</h3>
					<div class="row">
						<div id="f-acquisition_source"></div>
						<div id="f-posting_date"></div>
					</div>
					<div class="row" id="row-ffl-transfer" style="margin-top:12px">
						<div id="f-is_ffl_transfer"></div>
						<div id="f-transfer_for_customer"></div>
					</div>
					<div class="row" id="row-ffl-supplier" style="margin-top:12px">
						<div id="f-supplier"></div>
						<div></div>
					</div>
					<div class="row" id="row-individual-name" style="margin-top:12px; display:none">
						<div id="f-seller_first_name"></div>
						<div id="f-seller_middle_name"></div>
						<div id="f-seller_last_name"></div>
						<div id="f-seller_suffix"></div>
					</div>
					<div class="row" id="row-individual-seller" style="margin-top:12px; display:none">
						<div id="f-seller_customer"></div>
						<div id="f-seller_id_type"></div>
					</div>
					<div class="row" id="row-individual-id" style="margin-top:12px; display:none">
						<div id="f-seller_id_number"></div>
						<div id="f-seller_id_document"></div>
					</div>
					<div class="row" id="row-individual-address" style="margin-top:12px; display:none">
						<div id="f-seller_address_line1"></div>
						<div id="f-seller_city"></div>
						<div id="f-seller_state"></div>
						<div id="f-seller_zip"></div>
					</div>
					<div class="row" style="margin-top:12px">
						<div id="f-source"></div>
						<div id="f-acquisition_type"></div>
					</div>
					<div class="row" id="row-consignment-commission" style="margin-top:12px; display:none">
						<div id="f-consignment_commission_pct"></div>
						<div></div>
					</div>
				</div>

				<!-- 2. SCAN ITEMS -->
				<div class="section">
					<h3>2. ${__('Scan Items')}</h3>
					<div class="scan-wrap">
						<div id="f-scan" class="scan-input"></div>
						<div class="typeahead" id="typeahead"></div>
					</div>
					<div class="helper">${__('Scan barcode, type SKU or item name. Use ↑↓ + Enter to pick.')}</div>

					<div id="pending-area"></div>

					<div class="lines" id="lines-area" style="display:none">
						<table>
							<thead>
								<tr>
									<th>${__('Item')}</th>
									<th class="right">${__('Qty')}</th>
									<th class="right">${__('Cost')}</th>
									<th class="right">${__('Subtotal')}</th>
									<th></th>
								</tr>
							</thead>
							<tbody id="lines-tbody"></tbody>
						</table>
					</div>

					<div class="total-bar">
						<div>${__('Lines')}: <span id="line-count">0</span></div>
						<div class="total">${__('Total')}: <span id="total">0.00</span></div>
					</div>
				</div>

				<!-- 3. NOTES (optional) -->
				<div class="section">
					<h3>3. ${__('Notes')} <span style="font-weight:400;color:var(--text-muted);font-size:12px">${__('(optional)')}</span></h3>
					<div id="f-notes"></div>
				</div>

				<div class="submit-bar">
					<div id="readiness" class="helper"></div>
					<button class="btn btn-primary btn-lg" id="btn-submit" disabled>${__('Submit & Receive')}</button>
				</div>
			</div>
		`).appendTo(this.page.body);

		this._buildFields();
		// Start with the pickup-customer link hidden — it is revealed only by
		// the FFL Transfer checkbox, so it can never be filled in by accident.
		this._onTransferChange();
		this._loadAcquireTypes();
		this._loadReceiveSources();
		this._loadConsignmentDefault();
		this._wireKeyboard();
		this._reflectReadiness();
		this._incomingButton(0);
		this._loadIncoming();
		this.fields.scan.set_focus?.();
	}

	_buildFields() {
		const f = (parent, opts) => {
			const ctrl = frappe.ui.form.make_control({
				parent: this.$body.find(parent)[0],
				df: opts,
				render_input: true,
			});
			ctrl.refresh();
			return ctrl;
		};
		this.fields = {
			acquisition_source: f('#f-acquisition_source', {
				label: __('Acquisition Source'), fieldtype: 'Select', fieldname: 'acquisition_source',
				options: 'FFL Dealer\nIndividual', default: 'FFL Dealer', reqd: 1,
				change: () => this._onAcquisitionSourceChange(),
			}),
			supplier: f('#f-supplier', {
				label: __('Supplier'), fieldtype: 'Link', options: 'Supplier', fieldname: 'supplier',
				reqd: 1, change: () => {
					this._reflectReadiness();
					this._warnOpenOrdersFor(this.fields.supplier.get_value());
				},
			}),
			is_ffl_transfer: f('#f-is_ffl_transfer', {
				label: __('FFL Transfer (customer\'s gun)'), fieldtype: 'Check',
				fieldname: 'is_ffl_transfer',
				description: __('Tick when this shipment is a CUSTOMER\'s firearm coming in '
					+ 'for a transfer. It is received into customer custody at zero cost, '
					+ 'never enters sellable stock, and is never listed online.'),
				change: () => this._onTransferChange(),
			}),
			transfer_for_customer: f('#f-transfer_for_customer', {
				label: __('Transfer for Customer'), fieldtype: 'Link', options: 'Customer',
				fieldname: 'transfer_for_customer',
				description: __('Inbound customer transfer: who picks this gun up. '
					+ 'Feeds the Pending Transfer Pickups report — leave blank for store stock.'),
				// A transfer gun arrives either way: warn about a Banned owner, never clear.
				change: () => {
					const cf = window.ffl && ffl.customerFlags;
					if (cf && cf.warn) cf.warn(this.fields.transfer_for_customer.get_value());
				},
			}),
			seller_first_name: f('#f-seller_first_name', {
				label: __('Seller First Name'), fieldtype: 'Data', fieldname: 'seller_first_name',
				change: () => this._reflectReadiness(),
			}),
			seller_middle_name: f('#f-seller_middle_name', {
				label: __('Seller Middle Name'), fieldtype: 'Data', fieldname: 'seller_middle_name',
				description: __('Optional.'),
			}),
			seller_last_name: f('#f-seller_last_name', {
				label: __('Seller Last Name'), fieldtype: 'Data', fieldname: 'seller_last_name',
				change: () => this._reflectReadiness(),
			}),
			seller_suffix: f('#f-seller_suffix', {
				label: __('Suffix'), fieldtype: 'Select', fieldname: 'seller_suffix',
				options: '\nJr\nSr\nII\nIII\nIV\nV\nVI',
			}),
			seller_customer: f('#f-seller_customer', {
				label: __('Seller (Customer)'), fieldtype: 'Link', options: 'Customer', fieldname: 'seller_customer',
				description: __('Private party selling the firearm — the POS customer record.'),
				change: () => { this._guardCustomer('seller_customer').then((ok) => ok && this._prefillSellerName()); this._reflectReadiness(); },
			}),
			seller_id_type: f('#f-seller_id_type', {
				label: __('Seller ID Type'), fieldtype: 'Select', fieldname: 'seller_id_type',
				options: '\nDriver License\nState ID\nPassport\nMilitary ID\nOther',
				change: () => this._reflectReadiness(),
			}),
			seller_address_line1: f('#f-seller_address_line1', {
				label: __('Street Address'), fieldtype: 'Data', fieldname: 'seller_address_line1',
				description: __('As on the ID — ATF requires the seller\'s address in the bound book.'),
				change: () => this._reflectReadiness(),
			}),
			seller_city: f('#f-seller_city', {
				label: __('City'), fieldtype: 'Data', fieldname: 'seller_city',
				change: () => this._reflectReadiness(),
			}),
			seller_state: f('#f-seller_state', {
				label: __('State'), fieldtype: 'Select', fieldname: 'seller_state',
				options: ['', 'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'DC', 'FL',
					'GA', 'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME',
					'MD', 'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH',
					'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI',
					'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI',
					'WY'].join('\n'),
				change: () => this._reflectReadiness(),
			}),
			seller_zip: f('#f-seller_zip', {
				label: __('ZIP'), fieldtype: 'Data', fieldname: 'seller_zip',
				change: () => this._reflectReadiness(),
			}),
			seller_id_number: f('#f-seller_id_number', {
				label: __('Seller ID Number'), fieldtype: 'Data', fieldname: 'seller_id_number',
				description: __('ATF requires this for an individual acquisition.'),
				change: () => this._reflectReadiness(),
			}),
			seller_id_document: f('#f-seller_id_document', {
				label: __('Seller ID Document'), fieldtype: 'Attach', fieldname: 'seller_id_document',
				description: __('Photo/scan of the seller ID (stored privately).'),
			}),
			source: f('#f-source', {
				label: __('Source'), fieldtype: 'Select', fieldname: 'source',
				options: '',
				reqd: 1, change: () => this._reflectReadiness(),
			}),
			acquisition_type: f('#f-acquisition_type', {
				label: __('Acquisition Type'), fieldtype: 'Select', fieldname: 'acquisition_type',
				options: '',
				description: __('FastBound bound-book acquisition type (optional; synced from FastBound).'),
				change: () => this._onAcquisitionTypeChange(),
			}),
			consignment_commission_pct: f('#f-consignment_commission_pct', {
				label: __('Consignment Commission %'), fieldtype: 'Percent', fieldname: 'consignment_commission_pct',
				description: __('Store commission kept on consignment sales — pre-fills each firearm line below.'),
			}),
			posting_date: f('#f-posting_date', {
				label: __('Date'), fieldtype: 'Date', fieldname: 'posting_date',
				default: frappe.datetime.now_date(), reqd: 1,
			}),
			scan: f('#f-scan', {
				label: __('Barcode / Item Code'), fieldtype: 'Data', fieldname: 'scan',
				change: () => {},  // we handle Enter explicitly
			}),
			notes: f('#f-notes', {
				label: '', fieldtype: 'Small Text', fieldname: 'notes',
			}),
		};
		this.fields.posting_date.set_value(frappe.datetime.now_date());
		this.fields.acquisition_source.set_value('FFL Dealer');
	}

	_onAcquisitionSourceChange() {
		// FFL Dealer → show the FFL supplier picker (required). Individual →
		// hide it, show the seller Customer + ID fields (required instead). The
		// server uses a generic "Individual Seller" placeholder supplier.
		const individual = this.fields.acquisition_source.get_value() === 'Individual';
		this.$body.find('#row-ffl-supplier').toggle(!individual);
		this.$body.find('#row-individual-name, #row-individual-seller, #row-individual-id, #row-individual-address').toggle(individual);
		// The transfer row is hidden for a private-party purchase, as the
		// pickup-customer field was before it moved out of the supplier row: a
		// customer transfer arrives FROM ANOTHER FFL, never from an individual
		// seller. Untick on the way out rather than merely hiding it — a hidden
		// but still-ticked box would keep sending is_ffl_transfer=1 and receive
		// a bought gun into custody at zero cost.
		this.$body.find('#row-ffl-transfer').toggle(!individual);
		if (individual && this._isTransfer()) {
			this.fields.is_ffl_transfer.set_value(0);
			this._onTransferChange();
		}
		this.fields.supplier.df.reqd = individual ? 0 : 1;
		this.fields.supplier.refresh();
		this._reflectReadiness();
	}

	_isTransfer() {
		return !!(this.fields.is_ffl_transfer && this.fields.is_ffl_transfer.get_value());
	}

	_onTransferChange() {
		// The pickup-customer link only makes sense on a transfer, and a value
		// sitting there WITHOUT the box ticked is refused by the server rather
		// than guessed at — so hide it, and clear it on the way out so an
		// un-ticked form can never carry a stale customer into the payload.
		const transfer = this._isTransfer();
		this.$body.find('#f-transfer_for_customer').toggle(transfer);
		if (!transfer) this.fields.transfer_for_customer.set_value('');
		// Cost is a REQUIRED box on every line card, which is the whole reason a
		// transfer ever carried a made-up number: staff had to type something to
		// submit. On a transfer the server forces 0 regardless, so stop asking.
		// An already-open pending card is updated in place (the same df-mutate +
		// refresh idiom _onAcquisitionSourceChange uses on the supplier field)
		// rather than rebuilt, so ticking the box mid-entry doesn't discard the
		// serials and firearm details already typed into it.
		if (this.pending && this.pending.rateCtrl) {
			this.pending.rateCtrl.df.reqd = transfer ? 0 : 1;
			this.pending.rateCtrl.df.read_only = transfer ? 1 : 0;
			if (transfer) {
				this.pending.rate = 0;
				this.pending.rateCtrl.set_value(0);
			}
			this.pending.rateCtrl.refresh();
		}
		if (this.pending && this.pending.sellPriceCtrl) {
			this.pending.sellPriceCtrl.df.read_only = transfer ? 1 : 0;
			if (transfer) {
				this.pending.sell_price = 0;
				this.pending.sellPriceCtrl.set_value(0);
			}
			this.pending.sellPriceCtrl.refresh();
		}
		// The zero-cost trio follows the Cost box for the same reason: on a
		// transfer the store did not buy the gun, so "was it free?" has no
		// meaning and gets neither asked nor kept. sync() hides it AND wipes what
		// is ticked, so un-ticking the transfer box mid-entry cannot leave a
		// stale confirmation behind on the card.
		if (this.pending && this.pending.zeroCost) this.pending.zeroCost.sync();
		// Un-ticking does NOT silently convert already-added transfer lines into
		// $0 purchases: each one is marked as needing a real cost, and the submit
		// button stays disabled until it either has one or says why it is free
		// (open the line — the row editor carries the same escape hatch the card
		// does). Deliberately per-line rather than a blanket "cost must be > 0"
		// rule — a consignment line is legitimately $0 and must stay
		// submittable.
		if (!transfer) {
			this.lines.forEach((l) => {
				if (l.custody_zeroed) {
					l.custody_zeroed = 0;
					l.needs_cost = 1;
				}
			});
		}
		this._renderLines();
		this._reflectReadiness();
	}

	// Is this receipt a consignor's stock coming in? Through the shared
	// vocabulary predicate, NOT `=== 'Consignment'` — see RG_CONSIGNMENT_*
	// above. Every consignment-gated behaviour on this page (the commission %
	// fields AND the zero-cost question) now reads the same answer, because a
	// spelling one of them recognised and the other did not would collect a
	// commission on a gun it then refused at $0, or the reverse.
	_isConsignmentIntake() {
		return rgIsConsignmentAcquisitionType(
			this.fields.acquisition_type && this.fields.acquisition_type.get_value());
	}

	_onAcquisitionTypeChange() {
		// Consignment intake collects a commission %: reveal the receipt-level
		// default field only for a Consignment acquisition (hidden otherwise).
		// The per-line overrides in the pending / edit cards gate on the same value.
		const isConsignment = this._isConsignmentIntake();
		this.$body.find('#row-consignment-commission').toggle(isConsignment);
		// Whether a $0 line needs confirming is decided by this dropdown, so an
		// open card must re-ask (or stop asking) the moment it changes — an error
		// naming a box that is not on screen is the failure this feature exists
		// to remove — and already-added $0 lines can become invalid, which the
		// readiness gate is what surfaces.
		if (this.pending && this.pending.zeroCost) this.pending.zeroCost.sync();
		this._reflectReadiness();
	}

	_loadConsignmentDefault() {
		// Pre-fill the receipt-level commission % from the store default in FFL
		// Settings (fallback 20). This seeds every consignment firearm line; the
		// operator can override per gun. Toggle once here so the initial show/hide
		// matches the (async-restored) acquisition type.
		frappe.db.get_single_value('FFL Settings', 'consignment_default_commission_pct').then((val) => {
			const pct = parseFloat(val);
			this.fields.consignment_commission_pct.set_value(pct > 0 ? pct : 20);
			this._onAcquisitionTypeChange();
		});
	}

	// Banned box / alerts (customer_flags.js) for a customer picked here; a Banned
	// one is cleared from the field. Promise<boolean>: may the page go on with them.
	_guardCustomer(field) {
		const cf = window.ffl && ffl.customerFlags;
		if (!(cf && cf.guardControl)) return Promise.resolve(true);
		return cf.guardControl(this.fields[field]).then((ok) => { this._reflectReadiness(); return ok; });
	}

	_prefillSellerName() {
		// Best-effort head start via the shared server-side splitter
		// (ffl_core.api.trade_in.seller_defaults) — one home for the
		// suffix-aware name split, shared with the POS Trade-in dialog, so
		// "pat lee IV Sampleton" prefils as first/middle/last + suffix
		// instead of dumping "lee IV Sampleton" into the last name.
		const cust = this.fields.seller_customer.get_value();
		if (!cust) return;
		if (this.fields.seller_first_name.get_value() || this.fields.seller_last_name.get_value()) return;
		frappe.call({
			method: 'ffl_core.api.trade_in.seller_defaults',
			args: { customer: cust },
			callback: (r) => {
				const d = r.message || {};
				if (!d.source_first_name && !d.source_last_name) return;
				this.fields.seller_first_name.set_value(d.source_first_name || '');
				this.fields.seller_middle_name.set_value(d.source_middle_name || '');
				this.fields.seller_last_name.set_value(d.source_last_name || '');
				this.fields.seller_suffix.set_value(d.source_suffix || '');
				// Address parts: fill only what's empty; the state dropdown is
				// overwritten so its TX default can't mask an out-of-state seller.
				[['seller_address_line1', 'source_address_line1'],
					['seller_city', 'source_city'],
					['seller_zip', 'source_zip']].forEach(([field, key]) => {
					if (d[key] && !this.fields[field].get_value()) {
						this.fields[field].set_value(d[key]);
					}
				});
				if (d.source_state) this.fields.seller_state.set_value(d.source_state);
				this._reflectReadiness();
			},
		});
	}

	_loadAcquireTypes() {
		// Populate the Acquisition Type dropdown from FastBound's synced
		// AcquireType list. Soft-coupled to ffl_integrations: silent on failure.
		// Preserves a value the cashier picked before this async sync resolved
		// — Frappe's Select.refresh() clears the value when it's not in the
		// new options list; unshift it back in (mirrors ffl_acquisition.js).
		frappe.call({
			method: 'ffl_integrations.fastbound.client_api.get_acquire_types',
			callback: (r) => {
				const types = (r.message || []).slice();
				const ctrl = this.fields.acquisition_type;
				if (!ctrl) return;
				const cur = ctrl.get_value && ctrl.get_value();
				if (!types.length && !cur) return;
				if (cur && !types.includes(cur)) types.unshift(cur);
				ctrl.df.options = ['', ...types].join('\n');
				ctrl.refresh();
				if (cur) ctrl.set_value(cur);
			},
			error: () => {},
		});
	}

	_loadReceiveSources() {
		// Populate the Source dropdown from FFL Settings → Receive Sources.
		// Admin-managed list (one per line); edits take effect on next page load.
		// Same unshift-current pattern as _loadAcquireTypes above.
		frappe.call({
			method: 'ffl_core.api.receive_goods.get_receive_sources',
			callback: (r) => {
				const list = (r.message || []).slice();
				const ctrl = this.fields.source;
				if (!ctrl) return;
				const cur = ctrl.get_value && ctrl.get_value();
				if (!list.length && !cur) return;
				if (cur && !list.includes(cur)) list.unshift(cur);
				ctrl.df.options = ['', ...list].join('\n');
				ctrl.refresh();
				if (cur) ctrl.set_value(cur);
			},
		});
	}

	_wireKeyboard() {
		// Item search is the shared ffl.FirearmItemPicker (local + RSR catalog +
		// promote / create). onSelect funnels the picked/created item into the same
		// pending-card flow used before the extraction.
		this._itemPicker = new ffl.FirearmItemPicker({
			$input: this.fields.scan.$input,
			$dropdown: this.$body.find('#typeahead'),
			onSelect: (item) => this._matchThenShow(item),
		});

		this.$body.find('#btn-submit').on('click', () => this._submitBuckets());
	}

	// ------------------------------------------------ scan-first

	// Before the card opens, ask which open Distributor Order this unit belongs
	// to. One candidate → "hold it for this order?" (Enter = yes); several → pick;
	// none → plain stock. The answer for an item is remembered for the session.
	_matchThenShow(item, opts = {}) {
		if (this._isTransfer() || this.fields.acquisition_source.get_value() === 'Individual') {
			// A customer's gun / a private seller is never a distributor parcel.
			this._showPending(item);
			return;
		}
		let remembered = this.session.choices[item.item_code];
		// A remembered order line that this session has since filled is asked about again
		// (match_incoming only offers lines with room), not silently over-received.
		if (remembered && remembered !== 'stock' && !rgHasRoom(remembered, this.lines, this.pending)) {
			delete this.session.choices[item.item_code];
			remembered = null;
		}
		if (remembered) {
			this._showPending(item, remembered === 'stock' ? null : remembered);
			return;
		}
		// An item of a staged outside-POS invoice goes to that invoice line (no order
		// lookup) until the line is full; "change" skips this for the one scan.
		const invoiceCand = opts.skipInvoice ? null : this._invoiceCandidate(item);
		if (invoiceCand) { this._showPending(item, invoiceCand); return; }
		frappe.call({
			method: 'ffl_integrations.distributor.receive_bridge.match_incoming',
			args: { item_code: item.item_code },
			callback: (r) => {
				const cands = ((r.message || {}).candidates || [])
					.filter((c) => c.outstanding - this._sessionQtyFor(c.line_name) > 0);
				if (!cands.length) {
					// "Change" away from an invoice to plain stock is remembered, or the
					// next scan would offer the invoice line again.
					if (opts.skipInvoice) this.session.choices[item.item_code] = 'stock';
					this._showPending(item);
					return;
				}
				this._askBucket(item, cands);
			},
			error: () => this._showPending(item),
		});
	}

	_invoiceCandidate(item) {
		return rgInvoiceCandidate(this.lines, this.pending, item.item_code);
	}

	// Units already in this session against an order line, so the next scan of
	// the same item rolls to the next order once a line is full.
	_sessionQtyFor(lineName) {
		return rgSessionQtyFor(this.lines, this.pending, lineName);
	}

	_askBucket(item, cands) {
		const esc = frappe.utils.escape_html;
		const first = cands[0];
		const who = (c) => c.customer_name
			? `${esc(c.customer_name)} · ${esc(c.source_name)}`
			: __('Restock');
		const line = (c) => `${who(c)} · ${esc(c.distributor)} ${esc(c.do_name)}`
			+ (c.package_index ? ` · ${__('parcel {0}', [c.package_index])}` : '')
			+ ` · ${__('{0} outstanding', [c.outstanding])}`;
		const fields = [{
			fieldtype: 'HTML', fieldname: 'who',
			options: `<p><b>${esc(item.item_name)}</b> ${__('has an open order:')}</p><p>${line(first)}</p>`,
		}];
		if (cands.length > 1) {
			fields.push({
				fieldtype: 'Select', fieldname: 'pick', label: __('Or another order'),
				options: cands.map((c) => c.do_name).join('\n'), default: first.do_name,
			});
		}
		const d = new frappe.ui.Dialog({
			title: __('Hold it for this order?'),
			fields,
			primary_action_label: __('Yes, hold for this order'),
			primary_action: () => {
				const name = cands.length > 1 ? d.get_value('pick') : first.do_name;
				const chosen = cands.find((c) => c.do_name === name) || first;
				d.hide();
				this.session.choices[item.item_code] = chosen;
				this._showPending(item, chosen);
			},
			secondary_action_label: __('No — into store stock'),
			secondary_action: () => {
				d.hide();
				this.session.choices[item.item_code] = 'stock';
				this._showPending(item);
			},
		});
		d.show();
		// Enter = yes. The dialog has no input to carry Frappe's own Enter →
		// primary binding, and Bootstrap parks focus on the `.modal` element itself
		// (live-verified: document.activeElement is "modal fade show"), so the key
		// is caught there.
		d.$wrapper.on('keydown', (e) => {
			if (e.key === 'Enter') { e.preventDefault(); d.get_primary_btn().click(); }
		});
	}

	// The typed serial is checked against what the house said it sent. On the
	// chosen order → silent; on ANOTHER open order of the same item → move the
	// unit there (the invoice knows better than FIFO); on none → warn, allow.
	_checkSerialsAgainstOrder() {
		const p = this.pending;
		if (!p || !rgNeedsSerials(p) || !p.do_meta) return;
		const typed = p.serials.map((s) => s.trim()).filter(Boolean);
		if (!typed.length) return;
		const mine = new Set(p.do_meta.serials || []);
		const unknown = typed.filter((s) => !mine.has(s));
		if (!unknown.length) { this._serialNote(''); return; }
		frappe.call({
			method: 'ffl_integrations.distributor.receive_bridge.match_incoming',
			args: { item_code: p.item_code },
			callback: (r) => {
				const other = ((r.message || {}).candidates || [])
					.find((c) => !(rgCandKind(p.do_meta) === 'do' && c.do_name === p.do_meta.do_name)
						&& (c.serials || []).some((s) => unknown.includes(s)));
				if (other) {
					this._serialNote(__('Serial {0} is on {1}\'s invoice — moved to that order.',
						[unknown[0], other.do_name]), 'var(--blue-700)');
					p.bucket = rgCandBucket(other); p.do_line = other.line_name; p.do_meta = other;
					this.session.choices[p.item_code] = other;
				} else if (mine.size) {
					this._serialNote(__('Serial not on the distributor\'s invoice for this order — check the box label.'),
						'var(--orange-700)');
				}
			},
		});
	}

	_serialNote(text, color) {
		let $n = this.$body.find('#p-serial-note');
		if (!$n.length) {
			$n = $('<div id="p-serial-note" class="helper"></div>').appendTo(this.$body.find('#p-serials-wrapper'));
		}
		$n.text(text).css('color', color || 'var(--text-muted)');
	}

	// ------------------------------------------------ incoming panel

	// What is on the way lives behind a top-right button:
	// as a page section it pushed the scan box below the fold on every receive,
	// while being useful only when the parcel belongs to an order. The button
	// carries the count so a waiting parcel is still visible at a glance.
	_incomingButton(count) {
		this.page.set_secondary_action(
			count ? __('Incoming shipments ({0})', [count]) : __('Incoming shipments'),
			() => this._openIncoming());
	}

	_openIncoming() {
		const d = new frappe.ui.Dialog({
			title: __('Incoming shipments'),
			size: 'large',
			fields: [{ fieldtype: 'HTML', fieldname: 'incoming' }],
			secondary_action_label: __('Refresh'),
			secondary_action: () => this._loadIncoming(),
			on_hide: () => { this.$incoming = null; this.incomingDialog = null; },
		});
		this.incomingDialog = d;
		this.$incoming = d.fields_dict.incoming.$wrapper.addClass('ffl-incoming');
		this._renderIncoming(this.incoming);
		d.show();
		// Draw what was cached, then ask again — a parcel may have shipped since
		// the page opened.
		this._loadIncoming();
	}

	_loadIncoming() {
		frappe.call({
			method: 'ffl_integrations.distributor.receive_bridge.list_incoming',
			callback: (r) => {
				this.incoming = (r.message || {}).groups || [];
				this.incomingError = false;
				this._incomingButton(this.incoming.reduce((n, g) => n + (g.orders || []).length, 0));
				this._renderIncoming(this.incoming);
				if (this._stageOrderName) this._stageOrder(this._stageOrderName);
			},
			// Never let a failed load read as "Nothing on the way." — that is the
			// answer that strands a customer's parcel as plain restock.
			error: () => { this.incomingError = true; this._renderIncoming(this.incoming); },
		});
	}

	// Renders into the open dialog; nothing to draw while it is closed.
	_renderIncoming(groups) {
		const $list = this.$incoming;
		if (!$list) return;
		$list.empty();
		// A failed refresh says so FIRST, whatever was cached: a cached empty
		// list would otherwise read "Nothing on the way." — the one answer that
		// strands a customer's parcel as plain restock — and a cached stale
		// list would read as current. The cached rows, if any, stay visible
		// under the notice so a clerk can still act on what was known.
		if (this.incomingError) {
			$(`<div class="meta" style="color:var(--red-600)">${__('Could not load incoming orders. Showing the last known list — Refresh to retry.')}</div>`)
				.appendTo($list);
			if (!groups || !groups.length) return;
		} else if (!groups) {
			$list.text(__('Loading…'));
			return;
		}
		if (!groups.length) { $list.text(__('Nothing on the way.')); return; }
		const esc = frappe.utils.escape_html;
		groups.forEach((g) => {
			const $g = $(`<div class="incoming-group">
				<div class="head">
					<strong>${g.customer_name ? esc(g.customer_name) + ' · ' : ''}${esc(g.label)}</strong>
					${g.orders.length > 1 && g.source_name
						? `<button class="btn btn-xs btn-primary btn-load-order">${__('Receive whole order')}</button>` : ''}
				</div></div>`).appendTo($list);
			g.orders.forEach((o) => {
				const tracking = o.tracking_numbers || [];
				const state = o.hold_status ? __('Hold: {0}', [esc(o.hold_status)]) : esc(o.status);
				// An outside-POS invoice (a parcel ordered off-system) has no order to
				// poll: it stages through its own call and books against the invoice.
				const isInvoice = !!o.invoice_name;
				const $o = $(`<div class="incoming-order">
					<div class="grow">${esc(o.distributor)} · ${isInvoice
						? `<a href="/app/distributor-invoice/${encodeURIComponent(o.invoice_name)}">${__('Invoice')} ${esc(o.invoice_number)}</a>
							<span class="indicator-pill orange">${__('Outside POS')}</span>`
						: `<a href="/app/distributor-order/${encodeURIComponent(o.do_name)}">${esc(o.do_name)}</a>
						· ${state}`}
						· ${tracking.length ? __('tracking {0}', [esc(tracking.join(', '))]) : __('not shipped yet')}
						· ${__('{0} of {1} outstanding', [o.outstanding, o.expected])}</div>
					${isInvoice ? '' : `<button class="btn btn-xs btn-default btn-poll-now">${__('Check status now')}</button>`}
					<button class="btn btn-xs btn-primary btn-load-do">${__('Receive this parcel')}</button>
				</div>`).appendTo($g);
				$o.find('.btn-load-do').on('click', () => (isInvoice
					? this._stageInvoice(o) : this._loadOrderLines(o, o.lines)));
				$o.find('.btn-poll-now').on('click', () => frappe.call({
					method: 'ffl_integrations.distributor.tasks.poll_order_now',
					args: { do_name: o.do_name }, freeze: true,
					freeze_message: __('Asking {0}…', [o.distributor]),
					callback: () => this._loadIncoming(),
				}));
			});
			$g.find('.btn-load-order').on('click', () => g.orders.forEach((o) => this._loadOrderLines(o, o.lines)));
		});
	}

	// A Distributor Order handed over by its form's Receive button: stage its
	// lines (once the incoming list is in hand) so the clerk confirms each gun.
	_stageFromRoute() {
		const name = (frappe.route_options || {}).distributor_order;
		if (!name) return;
		delete frappe.route_options.distributor_order;
		if (this.incoming) this._stageOrder(name);
		else { this._stageOrderName = name; this._loadIncoming(); }
	}

	_stageOrder(name) {
		this._stageOrderName = null;
		const order = (this.incoming || []).flatMap((g) => g.orders || []).find((o) => o.do_name === name);
		if (!order) {
			frappe.msgprint({ title: __('Nothing to receive'), indicator: 'orange',
				message: __('{0} has no outstanding lines on the way.', [frappe.utils.escape_html(name)]) });
			return;
		}
		this._loadOrderLines(order, order.lines);
		frappe.show_alert({ indicator: 'blue',
			message: __('{0} staged — scan each gun, check its details against the firearm, then Receive.', [name]) }, 8);
	}

	// An outside-POS invoice: the server matches (or creates) each line's Item and
	// hands back the lines, then they stage exactly like an order's.
	_stageInvoice(inv) {
		frappe.call({
			method: 'ffl_integrations.distributor.receive_bridge.stage_invoice',
			args: { invoice_name: inv.invoice_name }, freeze: true,
			freeze_message: __('Matching {0} to items…', [inv.invoice_number]),
			callback: (r) => {
				const m = r.message || {};
				if ((m.unmatched || []).length && !m.received) {
					const esc = frappe.utils.escape_html;
					frappe.msgprint({ title: __('Some lines could not be matched'), indicator: 'orange',
						message: `<p>${__('No item could be found or made for these lines. Scan them as plain stock:')}</p>`
							+ `<ul>${m.unmatched.map((u) => `<li>${esc(u.sku)} — ${esc(u.reason)}</li>`).join('')}</ul>` });
				}
				if (m.received) {
					frappe.show_alert({ indicator: 'green', message: __('Invoice {0} closed — only $0 lines no item matches were left.', [inv.invoice_number]) }, 8);
					this._loadIncoming();
				}
				if (!(m.lines || []).length) return;
				this._loadOrderLines({ ...inv, invoice_name: m.invoice_name }, m.lines);
			},
		});
	}

	// Stage an order's outstanding lines into the session: accessories at the
	// outstanding qty, guns one row per unit with the echoed serial pre-filled.
	// A staged row is "unconfirmed" until scanned or edited — scanning the same
	// item ticks it instead of adding a new row.
	_loadOrderLines(order, lines) {
		// An outside-POS invoice is its own kind of bucket (submitted to
		// receive_invoice_lines); its label is the invoice number.
		const kind = order.invoice_name ? 'invoice' : 'do';
		const cand = (ln) => ({
			kind, label: kind === 'invoice' ? order.invoice_number : order.do_name,
			do_name: order.do_name || '', invoice_name: order.invoice_name || '',
			line_name: ln.line_name, distributor: order.distributor,
			customer_name: order.customer_name || '', source_name: order.source_name || '',
			package_index: '', outstanding: ln.outstanding, unit_cost: ln.unit_cost,
			serials: ln.serials || [],
		});
		lines.forEach((ln) => {
			if (this.lines.some((l) => l.do_line === ln.line_name)) return;
			// One row per unit when each unit carries a serial: a firearm (whatever its
			// tracking flag) or a serial-tracked Item — the server tells which.
			const perUnit = !!(ln.has_serial_no || ln.is_firearm);
			const units = perUnit ? ln.outstanding : 1;
			for (let i = 0; i < units; i++) {
				this.lines.push({
					item_code: ln.item_code, item_name: ln.item_name, is_firearm: ln.is_firearm,
					has_serial_no: perUnit, qty: perUnit ? 1 : ln.outstanding,
					rate: ln.unit_cost || 0, sell_price: 0, consignment_commission_pct: 0,
					serials: perUnit && ln.serials[i] ? [ln.serials[i]] : [],
					service_need: 0, manufacturer: ln.manufacturer || '', model_name: ln.model_name || '',
					firearm_type: ln.firearm_type || '', caliber: ln.caliber || '',
					importer: '', is_antique: 0, year_made: 0, item_year_made: 0, antique_locked: 0,
					zero_cost_confirmed: 0, zero_cost_reason: '', zero_cost_note: '', custody_zeroed: 0,
					bucket: rgCandBucket(cand(ln)), do_line: ln.line_name, do_meta: cand(ln),
					staged: true, ticked: false,
				});
			}
		});
		this.session.choices = {};
		if (this.incomingDialog) this.incomingDialog.hide();
		this._renderLines();
		this._reflectReadiness();
		this.fields.scan.set_focus?.();
	}

	// A distributor's parcel booked as a plain supplier receive silently strands
	// its order (DO never received, customer never held). If the supplier picked
	// is a distributor's and that house has open store-bound orders, say so.
	_warnOpenOrdersFor(supplier) {
		if (!supplier) return;
		frappe.call({
			method: 'ffl_integrations.distributor.receive_bridge.list_incoming',
			callback: (r) => {
				const orders = ((r.message || {}).groups || []).flatMap((g) => g.orders)
					.filter((o) => o.supplier === supplier);
				if (!orders.length) return;
				const esc = frappe.utils.escape_html;
				frappe.msgprint({
					title: __('{0} has {1} order(s) still on the way', [esc(supplier), orders.length]),
					indicator: 'orange',
					message: `<p>${__('Is this parcel one of them? Open Incoming shipments (top right) so the goods are held for their orders.')}</p>`
						+ `<ul>${orders.map((o) => `<li>${esc(o.do_name || o.invoice_number)} · ${esc(o.customer_name || __('Restock'))}</li>`).join('')}</ul>`,
				});
			},
		});
	}

	_sizeSerialsTextarea(ctrl, qty) {
		// Default Small Text renders with 4-6 rows; for Qty=1 (the common
		// firearm-receive case) that's a giant empty box. Clamp rows to
		// [1, 8] so a 1-firearm receive shows a single-line textarea and a
		// batch of 10 still fits without becoming a wall of whitespace.
		//
		// Frappe's textarea ships with a CSS min-height that overrides the
		// `rows` attribute on its own, so we ALSO drive an inline height in
		// pixels (~22px line-height + 16px padding) and unset min-height.
		if (!ctrl || !ctrl.$input || !ctrl.$input.length) return;
		const n = Math.max(1, Math.min(8, Math.round(parseFloat(qty) || 1)));
		ctrl.$input
			.attr('rows', n)
			.css({
				'min-height': '0',
				height: `${n * 22 + 16}px`,
				resize: 'vertical',
			});
	}

	// One df for both Year Made entry points (pending card + row editor) so the
	// two can't drift into two descriptions of the same box.
	//
	// The year belongs to THIS gun — it lands on the Serial No, so two guns of
	// one model can each carry their own. Deliberately blank on every receive
	// rather than pre-filled from the Item: the Item's year is one model-level
	// default, and a pre-filled number is a number nobody read off the gun.
	// It's offered as a hint instead.
	_yearMadeDf(itemYear) {
		const base = __('Optional — this gun\'s year of manufacture (antique = made in or before 1898). Leave blank if unknown.');
		return {
			label: __('Year Made'), fieldtype: 'Int', fieldname: 'year_made',
			description: itemYear
				? `${base} ${__('This model\'s item record says {0}.', [itemYear])}`
				: base,
		};
	}

	// What is wrong with this line's cost, in this receipt's context. One home
	// for the question, asked by the pending card, the row editor and the
	// readiness gate — the same reason _yearMadeDf is one df.
	_lineCostGap(line) {
		return rgLineCostGap(line, {
			is_transfer: this._isTransfer(),
			acquisition_type: this.fields.acquisition_type
				&& this.fields.acquisition_type.get_value(),
		});
	}

	// Kernel code → what the receiver should do about it. Kept beside the arms
	// it words so a new arm cannot ship without one. Deliberately says what to
	// DO, not what is wrong: the whole point of asking here rather than after
	// Submit is that the card is still open and fixable.
	_costGapMessage(code) {
		return {
			negative: __('Cost cannot be negative.'),
			cost_with_free_tick: __('This line carries a cost, so it was not free — '
				+ 'clear the cost, or untick <b>Cost really was $0</b>.'),
			unconfirmed_zero: __('Enter what the store paid. If the gun really was free, '
				+ 'tick <b>Cost really was $0</b> and pick a reason — a $0 nobody confirmed '
				+ 'cannot be told apart from a cost nobody looked up.'),
			no_reason: __('Pick a reason for the $0 cost, so a later stock-cost audit '
				+ 'does not have to ask.'),
			other_needs_note: __('Reason "{0}" needs one line saying what actually happened.',
				[RG_ZERO_COST_OTHER]),
		}[code] || __('Enter a cost for this line.');
	}

	// The zero-cost trio — tick → reason → note — built identically on the
	// pending card and in the row editor so the two cannot drift into two ideas
	// of when an explanation is required.
	//
	// Visibility IS the feature here: the tick only appears where it is
	// answerable, and it is cleared as it hides. A stale tick behind a hidden box
	// would build a payload the server refuses (cost + "it was free"), and an
	// error naming a box that is not on screen is exactly the dead end this page
	// had before. `rateOf` is passed in because the two call sites keep the Cost
	// box in different places; `sel` maps the slots to that call site's selectors.
	_buildZeroCostTrio(fp, $scope, sel, rateOf) {
		// Assembled by assignment, not as one literal: the change handlers close
		// over `trio`, and a literal would leave it in the temporal dead zone
		// while make_control runs. `trio.sync &&` covers the same window.
		const trio = {};
		const resync = () => trio.sync && trio.sync();
		trio.confirmed = fp(sel.confirmed, {
			label: __('Cost really was $0'), fieldtype: 'Check',
			fieldname: 'zero_cost_confirmed',
			description: __('Tick ONLY when the store truly paid nothing — a supplier '
				+ 'gift, a promo, a warranty replacement. NOT for a cost nobody has '
				+ 'looked up yet: that one gets entered, not confirmed.'),
			change: resync,
		});
		trio.reason = fp(sel.reason, {
			label: __('Zero Cost Reason'), fieldtype: 'Select',
			fieldname: 'zero_cost_reason',
			options: ['', ...RG_ZERO_COST_REASONS].join('\n'),
			change: resync,
		});
		trio.note = fp(sel.note, {
			label: __('What happened'), fieldtype: 'Small Text',
			fieldname: 'zero_cost_note',
			description: __('Required for "{0}" — one line a later audit can read '
				+ 'instead of asking.', [RG_ZERO_COST_OTHER]),
		});
		// Asked only where a confirmation can change the answer: not on a
		// transfer (the server forces $0), not on a consignment intake (a
		// consignor's $0 needs no confirming), and not once a real cost is typed.
		trio.isAsked = () => !this._isTransfer()
			&& !this._isConsignmentIntake()
			&& !((parseFloat(rateOf()) || 0) > 0);
		trio.sync = () => {
			const asked = trio.isAsked();
			$scope.find(sel.wrapper).toggle(asked);
			if (!asked) trio.wipe();
			const on = asked && !!trio.confirmed.get_value();
			$scope.find(sel.detail).toggle(on);
			// Hidden means "not part of the answer any more", so clear it rather
			// than keep a value the operator can no longer see: trio.read() sends
			// every field whatever is showing, so an untick after choosing a
			// reason would otherwise submit a reason with no confirmation behind
			// it, and switching away from "Other" would keep an explanation that
			// now explains nothing. Same one-bounce guard as wipe(): only assign
			// when there is something to clear (reason's change handler re-enters
			// here, and finds nothing left to do).
			if (!on && trio.reason.get_value()) trio.reason.set_value('');
			const noteAsked =
				on && (trio.reason.get_value() || '').trim() === RG_ZERO_COST_OTHER;
			if (!noteAsked && trio.note.get_value()) trio.note.set_value('');
			$scope.find(sel.note).toggle(noteAsked);
		};
		trio.wipe = () => {
			// Only touch what is set — set_value on a Check re-enters sync, and
			// this is what stops that at one bounce.
			if (trio.confirmed.get_value()) trio.confirmed.set_value(0);
			if (trio.reason.get_value()) trio.reason.set_value('');
			if (trio.note.get_value()) trio.note.set_value('');
		};
		trio.read = () => ({
			zero_cost_confirmed: trio.confirmed.get_value() ? 1 : 0,
			zero_cost_reason: (trio.reason.get_value() || '').trim(),
			zero_cost_note: (trio.note.get_value() || '').trim(),
		});
		trio.set = (vals) => {
			const v = vals || {};
			trio.confirmed.set_value(v.zero_cost_confirmed ? 1 : 0);
			trio.reason.set_value(v.zero_cost_reason || '');
			trio.note.set_value(v.zero_cost_note || '');
			trio.sync();
		};
		trio.sync();
		return trio;
	}

	// `cand` = the open-order candidate this unit is held for (match_incoming
	// shape), or null for plain stock.
	_showPending(item, cand = null) {
		this.pending = {
			item_code: item.item_code,
			item_name: item.item_name,
			is_firearm: item.is_firearm,
			has_serial_no: item.has_serial_no,
			qty: item.has_serial_no ? 1 : 1,
			rate: cand ? (cand.unit_cost || item.last_rate || 0) : (item.last_rate || 0),
			bucket: cand ? rgCandBucket(cand) : 'stock',
			do_line: cand ? cand.line_name : null,
			do_meta: cand,
			sell_price: (item.is_firearm && !item.uniform_sell_price) ? 0 : (parseFloat(item.standard_rate) || 0),
			consignment_commission_pct: 0,
			serials: [],
			service_need: 0,
			manufacturer: item.manufacturer || '',
			model_name: item.model_name || '',
			firearm_type: item.firearm_type || '',
			caliber: item.caliber || '',
			// Blank per gun (the server stopped sending an Item-level importer,
			// 2026-08-20): an import marking belongs to THIS gun — the operator
			// states it, or it stays blank. Never defaulted from the Item.
			importer: '',
			is_antique: item.is_antique ? 1 : 0,
			// Blank per gun; the Item's year rides along only as the hint text.
			year_made: 0,
			item_year_made: parseInt(item.year_made, 10) || 0,
			antique_locked: !!item.antique_locked,
			storage_location: '',
			storage_kind: '',
		};

		// Show the per-line commission % only for a firearm on a Consignment intake.
		const showConsignment = item.is_firearm && this._isConsignmentIntake();

		const $area = this.$body.find('#pending-area');
		$area.html(`
			<div class="pending" id="pending-card">
				<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
					<div>
						${item.is_firearm ? `<span style="background:var(--blue-100);color:var(--blue-700);padding:1px 6px;border-radius:4px;font-size:11px;margin-right:6px">${__('FIREARM')}</span>` : ''}
						<a href="/app/item/${encodeURIComponent(item.item_code)}" target="_blank" style="color:inherit"><strong>${frappe.utils.escape_html(item.item_name)}</strong></a>
						<span style="color:var(--text-muted)">(<a href="/app/item/${encodeURIComponent(item.item_code)}" target="_blank" style="color:inherit">${frappe.utils.escape_html(item.item_code)}</a>)</span>
					</div>
					<button class="btn btn-xs btn-secondary" id="btn-cancel-pending">${__('Cancel')}</button>
				</div>
				${cand
					? `<div class="helper bucket-tag">→ ${rgCandKind(cand) === 'invoice' ? __('invoice') : `${__('hold for')} ${frappe.utils.escape_html(cand.customer_name || __('Restock'))}`} · ${frappe.utils.escape_html(rgCandLabel(cand))}${cand.package_index ? ' · ' + __('parcel {0}', [cand.package_index]) : ''} <a href="#" id="p-rechoose">${__('change')}</a></div>`
					: `<div class="helper">→ ${__('store stock')}</div>`}
				<div class="row" style="grid-template-columns:1fr 1fr 1fr">
					<div id="p-qty"></div>
					<div id="p-rate"></div>
					<div id="p-sell_price"></div>
				</div>
				<div id="p-zero-cost-wrapper" style="display:none; margin-top:8px">
					<div id="p-zero_cost_confirmed"></div>
					<div class="row" id="p-zero-cost-detail" style="display:none; margin-top:8px">
						<div id="p-zero_cost_reason"></div>
						<div id="p-zero_cost_note"></div>
					</div>
				</div>
				<div id="p-consignment-wrapper" style="display:${showConsignment ? 'block' : 'none'}; margin-top:8px">
					<div id="p-consignment_commission_pct"></div>
				</div>
				<div id="p-firearm-wrapper" style="display:${item.is_firearm ? 'block' : 'none'}; margin-top: 8px">
					<div class="row"><div id="p-manufacturer"></div><div id="p-model_name"></div></div>
					<div class="row" style="margin-top:8px"><div id="p-firearm_type"></div><div id="p-caliber"></div></div>
					<div class="row" style="margin-top:8px"><div id="p-importer"></div><div></div></div>
					<div class="row" style="margin-top:8px"><div id="p-is_antique"></div><div id="p-year_made"></div></div>
					<div class="helper">${__('Confirm firearm details against the gun — required for the bound book. Prefilled from the distributor / catalog where available.')}</div>
				</div>
				<div id="p-serials-wrapper" style="display:${rgNeedsSerials(item) ? 'block' : 'none'}; margin-top: 8px">
					<div id="p-serials"></div>
					<div class="helper">${__(item.has_serial_no ? 'One serial per line — count must match Qty.' : 'Single serial (used firearm — qty fixed at 1).')}</div>
				</div>
				<div id="p-storage-wrapper" style="margin-top: 8px; display:${this._isTransfer() ? 'none' : 'block'}">
					<div id="p-storage"></div>
				</div>
				<div id="p-service-wrapper" style="margin-top: 8px">
					<div id="p-service"></div>
				</div>
				<div style="margin-top:12px; text-align:right">
					<button class="btn btn-primary" id="btn-add-pending">${__('Add to Receipt')}</button>
				</div>
			</div>
		`);

		const fp = (parent, opts) => {
			const ctrl = frappe.ui.form.make_control({
				parent: $area.find(parent)[0],
				df: opts,
				render_input: true,
			});
			ctrl.refresh();
			return ctrl;
		};
		this.pending.qtyCtrl = fp('#p-qty', {
			label: __('Qty'), fieldtype: 'Float', fieldname: 'qty', reqd: 1,
			change: () => {
				this.pending.qty = parseFloat(this.pending.qtyCtrl.get_value()) || 0;
				if (this.pending.has_serial_no === false && this.pending.is_firearm) {
					// Used firearm — keep qty at 1
					if (this.pending.qty !== 1) {
						this.pending.qtyCtrl.set_value(1);
						this.pending.qty = 1;
					}
				}
				// Shrink/grow the serial number textarea to match qty so it's
				// not a 6-line box for a single firearm.
				this._sizeSerialsTextarea(this.pending.serialsCtrl, this.pending.qty);
			},
		});
		this.pending.qtyCtrl.set_value(this.pending.qty);

		const transferMode = this._isTransfer();
		this.pending.rateCtrl = fp('#p-rate', {
			label: __('Cost'), fieldtype: 'Currency', fieldname: 'rate',
			reqd: transferMode ? 0 : 1,
			read_only: transferMode ? 1 : 0,
			description: transferMode
				? __('The store did not buy this gun — a transfer is received at zero cost.')
				: '',
			change: () => {
				this.pending.rate = parseFloat(this.pending.rateCtrl.get_value()) || 0;
				// Typing a real cost retires the "was it free?" question (and
				// drops anything already ticked, so the two can never disagree
				// in the payload); clearing the box asks it again.
				if (this.pending.zeroCost) this.pending.zeroCost.sync();
			},
		});
		if (transferMode) this.pending.rate = 0;
		this.pending.rateCtrl.set_value(this.pending.rate);

		// The escape hatch for a purchase that really was free — the only path
		// on this page for a genuinely $0 acquisition (the Trailblazer Lifecards),
		// and the reason the server's guard is not a dead end. Built here, next
		// to the Cost box it qualifies.
		this.pending.zeroCost = this._buildZeroCostTrio(fp, $area, {
			wrapper: '#p-zero-cost-wrapper',
			detail: '#p-zero-cost-detail',
			confirmed: '#p-zero_cost_confirmed',
			reason: '#p-zero_cost_reason',
			note: '#p-zero_cost_note',
		}, () => this.pending.rate);

		// Sell Price shows for every item; the destination differs (see _submit /
		// the backend): per-gun firearms stamp it per-serial, everything else
		// (non-firearm, uniform firearm) writes it to Item.standard_rate.
		const perGunPrice = item.is_firearm && !item.uniform_sell_price;
		this.pending.sellPriceCtrl = fp('#p-sell_price', {
			label: __('Sell Price'), fieldtype: 'Currency', fieldname: 'sell_price',
			read_only: transferMode ? 1 : 0,
			description: transferMode
				? __('A customer\'s gun is not for sale — a transfer records no price.')
				: (perGunPrice
					? __('Optional — leave blank if not priced yet. Editable later per-serial on the Item form.')
					: __('Retail price for this item — saved to the item (and POS) on receive.')),
			change: () => { this.pending.sell_price = parseFloat(this.pending.sellPriceCtrl.get_value()) || 0; },
		});
		// A transfer records no price at all (no price can be entered). The server
		// forces 0 regardless, but leaving the box editable is how the original
		// $35 placeholder came to exist — an operator asked for a number will
		// invent one.
		if (transferMode) this.pending.sell_price = 0;
		// Per-gun: blank by default (0 renders as "0.00"). Per-SKU: the seed is the
		// Item's current price so staff see + edit what POS will charge.
		if (this.pending.sell_price > 0) {
			this.pending.sellPriceCtrl.set_value(this.pending.sell_price);
		}

		// Consignment commission %: only on a Consignment intake, and only for a
		// firearm (it's stamped per-serial on submit). Seed from the receipt-level default.
		if (showConsignment) {
			this.pending.consignmentCommissionCtrl = fp('#p-consignment_commission_pct', {
				label: __('Consignment Commission %'), fieldtype: 'Percent', fieldname: 'consignment_commission_pct',
				description: __('Store commission kept on this gun. Pre-filled from the store default; override if needed.'),
				change: () => { this.pending.consignment_commission_pct = parseFloat(this.pending.consignmentCommissionCtrl.get_value()) || 0; },
			});
			const seed = parseFloat(this.fields.consignment_commission_pct.get_value()) || 0;
			this.pending.consignment_commission_pct = seed;
			if (seed > 0) this.pending.consignmentCommissionCtrl.set_value(seed);
		}

		if (rgNeedsSerials(item)) {
			this.pending.serialsCtrl = fp('#p-serials', {
				label: __('Serial Number(s)'), fieldtype: 'Small Text', fieldname: 'serials', reqd: 1,
				change: () => {
					const raw = this.pending.serialsCtrl.get_value() || '';
					this.pending.serials = raw.split('\n').map(s => s.trim()).filter(Boolean);
					this._checkSerialsAgainstOrder();
				},
			});
			// Default Small Text renders 4-6 rows; for Qty=1 (the common case)
			// that's a huge empty box. Size it to fit the current Qty.
			this._sizeSerialsTextarea(this.pending.serialsCtrl, this.pending.qty);
		}

		if (item.is_firearm) {
			this.pending.manufacturerCtrl = fp('#p-manufacturer', {
				label: __('Manufacturer'), fieldtype: 'Link', options: 'Brand', fieldname: 'manufacturer', reqd: 1,
				change: () => { this.pending.manufacturer = this.pending.manufacturerCtrl.get_value() || ''; },
			});
			this.pending.manufacturerCtrl.set_value(this.pending.manufacturer);
			this.pending.modelCtrl = fp('#p-model_name', {
				label: __('Model'), fieldtype: 'Data', fieldname: 'model_name', reqd: 1,
				change: () => { this.pending.model_name = this.pending.modelCtrl.get_value() || ''; },
			});
			this.pending.modelCtrl.set_value(this.pending.model_name);
			this.pending.typeCtrl = fp('#p-firearm_type', {
				label: __('Type'), fieldtype: 'Select', fieldname: 'firearm_type', reqd: 1,
				options: ffl.FIREARM_TYPE_OPTIONS,
				change: () => { this.pending.firearm_type = this.pending.typeCtrl.get_value() || ''; },
			});
			this.pending.typeCtrl.set_value(this.pending.firearm_type);
			this.pending.caliberCtrl = fp('#p-caliber', {
				label: __('Caliber'), fieldtype: 'Link', options: 'Caliber', fieldname: 'caliber', reqd: 1,
				change: () => { this.pending.caliber = this.pending.caliberCtrl.get_value() || ''; },
			});
			this.pending.caliberCtrl.set_value(this.pending.caliber);
			this.pending.importerCtrl = fp('#p-importer', {
				label: __('Importer'), fieldtype: 'Data', fieldname: 'importer',
				description: __('For imported firearms; leave blank if domestic.'),
				change: () => { this.pending.importer = this.pending.importerCtrl.get_value() || ''; },
			});
			this.pending.importerCtrl.set_value(this.pending.importer);
			// Year Made: OPTIONAL provenance on an antique, never a gate — the
			// Antique box alone decides how the gun is handled. Shown only while
			// Antique is ticked so it stays out of the way on a modern receive.
			this.pending.yearMadeCtrl = fp('#p-year_made', {
				...this._yearMadeDf(this.pending.item_year_made),
				change: () => { this.pending.year_made = parseInt(this.pending.yearMadeCtrl.get_value(), 10) || 0; },
			});
			const toggleYearMade = () => {
				const on = !!this.pending.is_antique;
				$area.find('#p-year_made').toggle(on);
				// Un-ticking drops the year with the claim it belonged to, so a
				// number typed and then abandoned can't ride along on submit.
				if (on) return;
				this.pending.year_made = 0;
				this.pending.yearMadeCtrl.set_value('');
			};
			this.pending.antiqueCtrl = fp('#p-is_antique', {
				label: __('Antique (pre-1899 / GCA-exempt)'), fieldtype: 'Check', fieldname: 'is_antique',
				description: __('Antique / GCA-exempt: still tracked as a serialized gun, but NOT entered in the bound book and no 4473 at sale.'),
				change: () => {
					this.pending.is_antique = this.pending.antiqueCtrl.get_value() ? 1 : 0;
					toggleYearMade();
				},
			});
			this.pending.antiqueCtrl.set_value(this.pending.is_antique);
			toggleYearMade();
			if (this.pending.antique_locked) {
				// Item already has bound-book firearms — antique can't be set here.
				this.pending.antiqueCtrl.$input && this.pending.antiqueCtrl.$input.prop('disabled', true);
				$area.find('#p-is_antique').append(
					`<div class="helper" style="color:var(--text-muted)">${__('Already in the bound book — cannot be marked Antique (a booked gun leaves only via a disposition).')}</div>`);
			}
		}

		this.pending.serviceCtrl = fp('#p-service', {
			label: __('Service needed (gunsmith / repair)'), fieldtype: 'Check', fieldname: 'service_need',
			change: () => { this.pending.service_need = this.pending.serviceCtrl.get_value() ? 1 : 0; },
		});
		this.pending.serviceCtrl.set_value(this.pending.service_need);

		// Where it goes in the store. A firearm may take any free slot (or an open
		// location); loose stock only an open one. Blank is fine: it lands in "Not placed".
		const isGun = !!(item.is_firearm || item.has_serial_no);
		const pending = this.pending;
		pending.storageCtrl = fp('#p-storage', {
			label: __('Storage location'), fieldtype: 'Link', options: 'Storage Location',
			fieldname: 'storage_location',
			description: isGun
				? __('A slot holds one firearm. Several guns on one line fill the next free slots.')
				: __('A display case, rack or safe.'),
			get_query: () => ({
				query: 'ffl_core.api.storage.location_query',
				filters: isGun ? { only_free: 1 } : { kind: 'Open' },
			}),
			change: () => {
				pending.storage_location = pending.storageCtrl.get_value() || '';
				pending.storage_kind = '';
				if (pending.storage_location) {
					frappe.db.get_value('Storage Location', pending.storage_location, 'kind')
						.then((r) => { pending.storage_kind = (r.message || {}).kind || ''; });
				}
			},
		});
		this._suggestLocation(isGun).then((name) => {
			if (name && this.pending === pending && !pending.storage_location) pending.storageCtrl.set_value(name);
		});

		$area.find('#btn-cancel-pending').on('click', () => this._cancelPending());
		$area.find('#btn-add-pending').on('click', () => this._addPending());
		$area.find('#p-rechoose').on('click', (e) => {
			e.preventDefault();
			delete this.session.choices[item.item_code];
			this._cancelPending();
			this._matchThenShow(item, { skipInvoice: true });
		});
	}

	// The location offered for the next item: the one just used (open locations), or the
	// next free slot after the slots the lines already on this receipt will fill. One server
	// call plans every row at once (a row of several guns fills the slots after its start), so
	// no slot is offered twice. Blank when the last location cannot take this kind of thing.
	async _suggestLocation(isGun) {
		const last = this._lastStorage;
		if (!last || !last.name) return '';
		if (last.kind === 'Open') return last.name;
		if (!isGun) return '';
		const rows = rgSlotRows(this.lines);
		rows.push({ key: 'next', after: last.name, count: 1 });
		const plan = await frappe.xcall('ffl_core.api.storage.plan_slots', { rows: JSON.stringify(rows) });
		return ((plan || {}).next || [])[0] || '';
	}

	_cancelPending() {
		this.pending = null;
		this.$body.find('#pending-area').empty();
		this.fields.scan.set_focus?.();
	}

	_addPending() {
		const p = this.pending;
		if (!p) return;
		if (!(p.qty > 0)) { frappe.msgprint(__('Qty must be > 0.')); return; }
		// Cost was the one box on this card marked required (reqd=1) and then
		// never checked here — a red star, and nothing behind it. That is how
		// eleven real purchases went in at $0 over three months and became
		// indistinguishable from the guns that really were free. The tiers are
		// the server's (_resolve_line_cost); the server remains the authority,
		// this only moves the refusal to while the card is still open.
		const zc = p.zeroCost ? p.zeroCost.read() : {};
		const costGap = this._lineCostGap({ rate: p.rate, ...zc });
		if (costGap) { frappe.msgprint(this._costGapMessage(costGap)); return; }
		// Sell Price intentionally NOT required for firearms — staff may not
		// know the tag price until later; managers set it per-serial on the
		// Item form's Serial Numbers tab.
		const gap = rgSerialGap(p, p.qty, p.serials);
		if (gap === 'need_count') {
			frappe.msgprint(__('Need {0} serial(s), got {1}.', [Math.round(p.qty), p.serials.length]));
			return;
		}
		if (gap) {
			frappe.msgprint(__('Used firearm needs exactly 1 serial.'));
			return;
		}
		if (p.is_firearm) {
			const missing = [];
			if (!p.manufacturer) missing.push(__('Manufacturer'));
			if (!p.model_name) missing.push(__('Model'));
			if (!p.firearm_type) missing.push(__('Type'));
			if (!p.caliber) missing.push(__('Caliber'));
			if (missing.length) {
				frappe.msgprint(__('Firearm details required: {0}', [missing.join(', ')]));
				return;
			}
		}
		// A row staged from the Incoming panel for this order line is what the
		// scan confirms — tick it with what the receiver saw, don't add a twin.
		const staged = this.lines.find((l) => l.staged && !l.ticked && l.do_line === p.do_line);
		if (staged) {
			Object.assign(staged, {
				qty: p.qty, rate: p.rate, serials: p.serials.slice(), ticked: true,
				sell_price: p.sell_price || 0, service_need: p.service_need ? 1 : 0,
				manufacturer: p.manufacturer || '', model_name: p.model_name || '',
				firearm_type: p.firearm_type || '', caliber: p.caliber || '', importer: p.importer || '',
				is_antique: p.is_antique ? 1 : 0, year_made: p.year_made || 0,
				zero_cost_confirmed: zc.zero_cost_confirmed || 0,
				zero_cost_reason: zc.zero_cost_reason || '', zero_cost_note: zc.zero_cost_note || '',
				storage_location: p.storage_location || '',
			});
			this._rememberLocation(p);
			this.pending = null;
			this.$body.find('#pending-area').empty();
			this._renderLines();
			this._reflectReadiness();
			this.fields.scan.set_focus?.();
			return;
		}
		this.lines.push({
			bucket: p.bucket || 'stock',
			do_line: p.do_line || null,
			do_meta: p.do_meta || null,
			item_code: p.item_code,
			item_name: p.item_name,
			is_firearm: p.is_firearm,
			has_serial_no: p.has_serial_no,
			qty: p.qty,
			rate: p.rate,
			sell_price: p.sell_price || 0,
			// Marks a line whose Cost was forced to 0 because the FFL Transfer box
			// was ticked when it was added. If the box is later un-ticked, the line
			// becomes an ordinary PURCHASE at $0 — no asset, no SRBNB, 100% margin
			// in every report — which is exactly the "cause-A zero-cost gun" mess
			// already on record. _onTransferChange turns this into a re-prompt.
			custody_zeroed: this._isTransfer() ? 1 : 0,
			// "This $0 is a fact, not an omission" — carried per line because
			// that is how it lands on the Serial No / PR Item, where a later
			// stock-cost audit can query it instead of asking the owner.
			zero_cost_confirmed: zc.zero_cost_confirmed || 0,
			zero_cost_reason: zc.zero_cost_reason || '',
			zero_cost_note: zc.zero_cost_note || '',
			consignment_commission_pct: p.consignment_commission_pct || 0,
			serials: p.serials.slice(),
			service_need: p.service_need ? 1 : 0,
			manufacturer: p.manufacturer || '',
			model_name: p.model_name || '',
			firearm_type: p.firearm_type || '',
			caliber: p.caliber || '',
			importer: p.importer || '',
			is_antique: p.is_antique ? 1 : 0,
			year_made: p.year_made || 0,
			item_year_made: p.item_year_made || 0,
			antique_locked: p.antique_locked ? 1 : 0,
			storage_location: p.storage_location || '',
		});
		this._rememberLocation(p);
		this.pending = null;
		this.$body.find('#pending-area').empty();
		this._renderLines();
		this._reflectReadiness();
		this.fields.scan.set_focus?.();
	}

	_rememberLocation(p) {
		if (p.storage_location) this._lastStorage = { name: p.storage_location, kind: p.storage_kind };
	}

	_renderLines() {
		const $tbody = this.$body.find('#lines-tbody');
		const $area = this.$body.find('#lines-area');
		if (!this.lines.length) { $area.hide(); return; }
		$area.show();
		$tbody.empty();
		this.lines.forEach((l, i) => {
			const subtotal = (l.qty * l.rate).toFixed(2);
			const showEditConsignment = l.is_firearm && this._isConsignmentIntake();
			const serialsHtml = l.serials.length
				? `<div class="serials">${__('Serials')}: ${l.serials.map(s => frappe.utils.escape_html(s)).join(', ')}</div>`
				: '';
			const placeHtml = l.storage_location
				? `<div class="serials">${__('Location')}: ${frappe.utils.escape_html(l.storage_location)}</div>` : '';
			const bucketHtml = l.do_meta
				? `<div class="bucket-tag">→ ${rgCandKind(l.do_meta) === 'invoice'
					? __('invoice')
					: `${__('hold for')} ${frappe.utils.escape_html(l.do_meta.customer_name || __('Restock'))}`} · ${frappe.utils.escape_html(rgCandLabel(l.do_meta))}${l.ticked ? ' ✓' : ''}</div>`
				: `<div class="serials">→ ${__('store stock')}</div>`;
			const unconfirmed = l.staged && !l.ticked;
			$tbody.append(`
				<tr class="line-summary ${unconfirmed ? 'staged' : ''}" data-idx="${i}">
					<td>
						${unconfirmed ? `<span class="firearm-mark" style="background:var(--orange-100,#fde68a);color:var(--orange-700,#92400e)">${__('UNCONFIRMED')}</span>` : ''}
						${l.is_firearm ? `<span class="firearm-mark">${__('FIREARM')}</span>` : ''}
						${l.is_antique ? `<span class="firearm-mark" style="background:var(--orange-100,#fde68a);color:var(--orange-700,#92400e)">${__('ANTIQUE')}${l.year_made ? ` ${l.year_made}` : ''}</span>` : ''}
						<a href="/app/item/${encodeURIComponent(l.item_code)}" target="_blank" style="color:inherit">${frappe.utils.escape_html(l.item_name)}</a>
						<div class="serials"><a href="/app/item/${encodeURIComponent(l.item_code)}" target="_blank" style="color:inherit">${frappe.utils.escape_html(l.item_code)}</a></div>
						${serialsHtml}
						${placeHtml}
						${bucketHtml}
					</td>
					<td class="right">${l.qty}</td>
					<td class="right">${frappe.format(l.rate, {fieldtype: 'Currency'})}</td>
					<td class="right">${frappe.format(subtotal, {fieldtype: 'Currency'})}</td>
					<td class="right"><button class="btn-remove" data-idx="${i}">${__('Remove')}</button></td>
				</tr>
			`);
			$tbody.append(`
				<tr class="line-edit-row" data-idx="${i}" style="display:none">
					<td colspan="5">
						<div class="row" style="grid-template-columns:1fr 1fr 1fr">
							<div data-edit-qty></div>
							<div data-edit-rate></div>
							<div data-edit-sell_price></div>
						</div>
						<div data-edit-zero-cost-wrapper style="margin-top:10px; display:none">
							<div data-edit-zero_cost_confirmed></div>
							<div class="row" data-edit-zero-cost-detail style="display:none; margin-top:8px">
								<div data-edit-zero_cost_reason></div>
								<div data-edit-zero_cost_note></div>
							</div>
						</div>
						<div data-edit-consignment-wrapper style="margin-top:10px; display:${showEditConsignment ? 'block' : 'none'}">
							<div data-edit-consignment_commission_pct></div>
						</div>
						<div data-edit-firearm-wrapper style="margin-top:10px; display:${l.is_firearm ? 'block' : 'none'}">
							<div class="row"><div data-edit-manufacturer></div><div data-edit-model_name></div></div>
							<div class="row" style="margin-top:8px"><div data-edit-firearm_type></div><div data-edit-caliber></div></div>
							<div class="row" style="margin-top:8px"><div data-edit-importer></div><div></div></div>
							<div class="row" style="margin-top:8px"><div data-edit-is_antique></div><div data-edit-year_made></div></div>
						</div>
						<div data-edit-serials-wrapper style="margin-top:10px; display:${rgNeedsSerials(l) ? 'block' : 'none'}">
							<div data-edit-serials></div>
							<div class="helper">${__(l.has_serial_no ? 'One serial per line — count must match Qty.' : 'Single serial (used firearm — qty fixed at 1).')}</div>
						</div>
						<div data-edit-service-wrapper style="margin-top:10px">
							<div data-edit-service></div>
						</div>
						<div class="actions">
							<button class="btn btn-secondary btn-sm" data-cancel-edit="${i}">${__('Cancel')}</button>
							<button class="btn btn-primary btn-sm" data-save-edit="${i}">${__('Save')}</button>
						</div>
					</td>
				</tr>
			`);
		});
		$tbody.find('.btn-remove').on('click', (e) => {
			const idx = parseInt(e.currentTarget.dataset.idx, 10);
			this.lines.splice(idx, 1);
			this._renderLines();
			this._reflectReadiness();
		});
		// Click row → expand into editor (ignoring clicks on links or remove)
		$tbody.find('.line-summary').on('click', (e) => {
			if ($(e.target).closest('a, .btn-remove').length) return;
			const idx = parseInt($(e.currentTarget).data('idx'), 10);
			this._expandLineEdit(idx);
		});
		$tbody.find('[data-save-edit]').on('click', (e) => {
			e.stopPropagation();
			const idx = parseInt($(e.currentTarget).data('save-edit'), 10);
			this._saveLineEdit(idx);
		});
		$tbody.find('[data-cancel-edit]').on('click', (e) => {
			e.stopPropagation();
			const idx = parseInt($(e.currentTarget).data('cancel-edit'), 10);
			this._collapseLineEdit(idx);
		});
		this._updateTotals();
	}

	_expandLineEdit(idx) {
		const line = this.lines[idx];
		if (!line) return;
		const showConsignment = line.is_firearm && this._isConsignmentIntake();
		const $tbody = this.$body.find('#lines-tbody');
		// Collapse any other open editor first.
		$tbody.find('.line-edit-row').hide();
		$tbody.find('.line-summary').removeClass('editing');

		const $editRow = $tbody.find(`.line-edit-row[data-idx="${idx}"]`);
		const $sumRow = $tbody.find(`.line-summary[data-idx="${idx}"]`);
		$sumRow.addClass('editing');
		$editRow.show();

		// Build/refresh controls inside the edit row.
		const fp = (parent, opts) => {
			const ctrl = frappe.ui.form.make_control({
				parent: $editRow.find(parent)[0],
				df: opts,
				render_input: true,
			});
			ctrl.refresh();
			return ctrl;
		};
		// Empty out any existing controls (re-expand should rebuild).
		$editRow.find('[data-edit-qty]').empty();
		$editRow.find('[data-edit-rate]').empty();
		$editRow.find('[data-edit-sell_price]').empty();
		$editRow.find('[data-edit-consignment_commission_pct]').empty();
		$editRow.find('[data-edit-zero_cost_confirmed]').empty();
		$editRow.find('[data-edit-zero_cost_reason]').empty();
		$editRow.find('[data-edit-zero_cost_note]').empty();
		$editRow.find('[data-edit-serials]').empty();
		$editRow.find('[data-edit-service]').empty();
		$editRow.find('[data-edit-manufacturer]').empty();
		$editRow.find('[data-edit-model_name]').empty();
		$editRow.find('[data-edit-firearm_type]').empty();
		$editRow.find('[data-edit-caliber]').empty();
		$editRow.find('[data-edit-importer]').empty();
		$editRow.find('[data-edit-is_antique]').empty();
		$editRow.find('[data-edit-year_made]').empty();

		this._editing = this._editing || {};
		this._editing[idx] = {
			qty: fp('[data-edit-qty]', {
				label: __('Qty'), fieldtype: 'Float', fieldname: 'qty', reqd: 1,
			}),
			rate: fp('[data-edit-rate]', {
				label: __('Cost'), fieldtype: 'Currency', fieldname: 'rate',
				reqd: this._isTransfer() ? 0 : 1,
				read_only: this._isTransfer() ? 1 : 0,
				// Same as the pending card: entering a real cost retires the
				// "was it free?" question, clearing the box asks it again.
				change: () => {
					const z = this._editing && this._editing[idx] && this._editing[idx].zeroCost;
					if (z) z.sync();
				},
			}),
			sell_price: fp('[data-edit-sell_price]', {
				label: __('Sell Price'), fieldtype: 'Currency', fieldname: 'sell_price',
				read_only: this._isTransfer() ? 1 : 0,
			}),
			consignment_commission_pct: showConsignment ? fp('[data-edit-consignment_commission_pct]', {
				label: __('Consignment Commission %'), fieldtype: 'Percent', fieldname: 'consignment_commission_pct',
			}) : null,
			// The same escape hatch as the pending card. Present here too because
			// a cost is editable after the line is added — a line edited down to
			// $0 with no way to say WHY would be a payload the server refuses and
			// this page could not fix, which is the dead end being closed.
			zeroCost: this._buildZeroCostTrio(fp, $editRow, {
				wrapper: '[data-edit-zero-cost-wrapper]',
				detail: '[data-edit-zero-cost-detail]',
				confirmed: '[data-edit-zero_cost_confirmed]',
				reason: '[data-edit-zero_cost_reason]',
				note: '[data-edit-zero_cost_note]',
			}, () => (this._editing && this._editing[idx]
				? parseFloat(this._editing[idx].rate.get_value()) || 0 : 0)),
			serials: rgNeedsSerials(line) ? fp('[data-edit-serials]', {
				label: __('Serial Number(s)'), fieldtype: 'Small Text', fieldname: 'serials', reqd: 1,
			}) : null,
			service: fp('[data-edit-service]', {
				label: __('Service needed (gunsmith / repair)'), fieldtype: 'Check', fieldname: 'service_need',
			}),
			manufacturer: line.is_firearm ? fp('[data-edit-manufacturer]', {
				label: __('Manufacturer'), fieldtype: 'Link', options: 'Brand', fieldname: 'manufacturer', reqd: 1,
			}) : null,
			model_name: line.is_firearm ? fp('[data-edit-model_name]', {
				label: __('Model'), fieldtype: 'Data', fieldname: 'model_name', reqd: 1,
			}) : null,
			firearm_type: line.is_firearm ? fp('[data-edit-firearm_type]', {
				label: __('Type'), fieldtype: 'Select', fieldname: 'firearm_type', reqd: 1,
				options: ffl.FIREARM_TYPE_OPTIONS,
			}) : null,
			caliber: line.is_firearm ? fp('[data-edit-caliber]', {
				label: __('Caliber'), fieldtype: 'Link', options: 'Caliber', fieldname: 'caliber', reqd: 1,
			}) : null,
			importer: line.is_firearm ? fp('[data-edit-importer]', {
				label: __('Importer'), fieldtype: 'Data', fieldname: 'importer',
			}) : null,
			is_antique: line.is_firearm ? fp('[data-edit-is_antique]', {
				label: __('Antique (pre-1899 / GCA-exempt)'), fieldtype: 'Check', fieldname: 'is_antique',
			}) : null,
			// Optional provenance, shown only while Antique is ticked (mirrors the
			// pending card). Never blocks the line from saving.
			year_made: line.is_firearm
				? fp('[data-edit-year_made]', this._yearMadeDf(line.item_year_made))
				: null,
		};
		// The wrapper's initial show/hide is baked into the _renderLines HTML, but
		// acquisition_type can change after a row was rendered (unlike is_firearm), so
		// keep the commission wrapper in sync with the control we just (re)built.
		$editRow.find('[data-edit-consignment-wrapper]').toggle(showConsignment);
		this._editing[idx].qty.set_value(line.qty);
		this._editing[idx].rate.set_value(line.rate);
		// Seeded AFTER the cost, because what the trio shows depends on it.
		this._editing[idx].zeroCost.set(line);
		this._editing[idx].service.set_value(line.service_need ? 1 : 0);
		// Sell price applies to every line now — seed it outside the firearm block.
		if (this._editing[idx].sell_price && line.sell_price > 0) {
			this._editing[idx].sell_price.set_value(line.sell_price);
		}
		if (this._editing[idx].consignment_commission_pct && line.consignment_commission_pct > 0) {
			this._editing[idx].consignment_commission_pct.set_value(line.consignment_commission_pct);
		}
		if (rgNeedsSerials(line)) {
			this._editing[idx].serials.set_value(line.serials.join('\n'));
			// Size the serials textarea to match the current Qty (and re-size
			// when the user edits Qty mid-edit).
			this._sizeSerialsTextarea(this._editing[idx].serials, line.qty);
			this._editing[idx].qty.$input && this._editing[idx].qty.$input.on('input change', () => {
				const q = parseFloat(this._editing[idx].qty.get_value()) || 0;
				this._sizeSerialsTextarea(this._editing[idx].serials, q);
			});
		}
		if (line.is_firearm) {
			this._editing[idx].manufacturer.set_value(line.manufacturer || '');
			this._editing[idx].model_name.set_value(line.model_name || '');
			this._editing[idx].firearm_type.set_value(line.firearm_type || '');
			this._editing[idx].caliber.set_value(line.caliber || '');
			this._editing[idx].importer.set_value(line.importer || '');
			this._editing[idx].is_antique.set_value(line.is_antique ? 1 : 0);
			if (line.year_made) this._editing[idx].year_made.set_value(line.year_made);
			const toggleYearMade = () => {
				const on = !!this._editing[idx].is_antique.get_value();
				$editRow.find('[data-edit-year_made]').toggle(on);
				if (on) return;
				this._editing[idx].year_made.set_value('');
			};
			toggleYearMade();
			this._editing[idx].is_antique.$input
				&& this._editing[idx].is_antique.$input.on('change', toggleYearMade);
			if (line.antique_locked) {
				this._editing[idx].is_antique.$input && this._editing[idx].is_antique.$input.prop('disabled', true);
			}
		}
	}

	_saveLineEdit(idx) {
		const line = this.lines[idx];
		const ctrls = this._editing && this._editing[idx];
		if (!line || !ctrls) return;
		const qty = parseFloat(ctrls.qty.get_value()) || 0;
		const rate = parseFloat(ctrls.rate.get_value()) || 0;
		const sell_price = ctrls.sell_price ? (parseFloat(ctrls.sell_price.get_value()) || 0) : 0;
		const consignment_commission_pct = ctrls.consignment_commission_pct
			? (parseFloat(ctrls.consignment_commission_pct.get_value()) || 0) : 0;
		if (qty <= 0) { frappe.msgprint(__('Qty must be > 0.')); return; }
		// The same tiers the pending card applies — through the same kernel, so
		// a cost edited down to $0 after the line was added is asked the same
		// question the card asked, rather than being refused at Submit.
		const zc = ctrls.zeroCost ? ctrls.zeroCost.read() : {};
		const costGap = this._lineCostGap({ rate, ...zc });
		if (costGap) { frappe.msgprint(this._costGapMessage(costGap)); return; }
		// Sell Price intentionally optional here too — matches pending-card.
		// No serial box for this line (not serial-tracked): what it holds is kept.
		const serials = ctrls.serials
			? (ctrls.serials.get_value() || '').split('\n').map(s => s.trim()).filter(Boolean)
			: undefined;

		const gap = serials === undefined ? '' : rgSerialGap(line, qty, serials);
		if (gap === 'need_count') {
			frappe.msgprint(__('Need {0} serial(s), got {1}.', [Math.round(qty), serials.length]));
			return;
		}
		if (gap === 'used_qty') {
			frappe.msgprint(__('Used firearm qty is fixed at 1.'));
			return;
		}
		if (gap) {
			frappe.msgprint(__('Used firearm needs exactly 1 serial.'));
			return;
		}
		let fa = null;
		if (line.is_firearm) {
			fa = {
				manufacturer: ctrls.manufacturer.get_value() || '',
				model_name: ctrls.model_name.get_value() || '',
				firearm_type: ctrls.firearm_type.get_value() || '',
				caliber: ctrls.caliber.get_value() || '',
				importer: ctrls.importer.get_value() || '',
				is_antique: ctrls.is_antique.get_value() ? 1 : 0,
				year_made: parseInt(ctrls.year_made.get_value(), 10) || 0,
			};
			const missing = [];
			if (!fa.manufacturer) missing.push(__('Manufacturer'));
			if (!fa.model_name) missing.push(__('Model'));
			if (!fa.firearm_type) missing.push(__('Type'));
			if (!fa.caliber) missing.push(__('Caliber'));
			if (missing.length) {
				frappe.msgprint(__('Firearm details required: {0}', [missing.join(', ')]));
				return;
			}
		}

		// A line that now carries a real cost — or a confirmed $0 — is no longer
		// the un-ticked-transfer leftover the re-prompt was about.
		if (line.needs_cost && (rate > 0 || zc.zero_cost_confirmed)) line.needs_cost = 0;
		rgApplyLineEdit(line, {
			qty, rate, zero: zc, sell_price, consignment_commission_pct, serials, fa,
			service_need: ctrls.service.get_value() ? 1 : 0,
		});
		delete this._editing[idx];
		this._renderLines();
		this._reflectReadiness();
	}

	_collapseLineEdit(idx) {
		const $tbody = this.$body.find('#lines-tbody');
		$tbody.find(`.line-edit-row[data-idx="${idx}"]`).hide();
		$tbody.find(`.line-summary[data-idx="${idx}"]`).removeClass('editing');
		if (this._editing) delete this._editing[idx];
	}

	_updateTotals() {
		const total = this.lines.reduce((s, l) => s + (l.qty * l.rate), 0);
		this.$body.find('#line-count').text(this.lines.length);
		this.$body.find('#total').html(frappe.format(total, {fieldtype: 'Currency'}));
	}

	_reflectReadiness() {
		const individual = this.fields.acquisition_source
			&& this.fields.acquisition_source.get_value() === 'Individual';
		// A session made only of order buckets books each receive against the
		// order's own house (receive_lines): no supplier / source to pick here.
		const ordersOnly = this.lines.length > 0 && this.lines.every((l) => rgBucketOf(l) !== 'stock');
		const supplierOk = individual || ordersOnly || !!this.fields.supplier.get_value();
		const sellerOk = !individual || (
			!!this.fields.seller_customer.get_value()
			&& !!this.fields.seller_first_name.get_value()
			&& !!this.fields.seller_last_name.get_value()
			&& !!this.fields.seller_id_type.get_value()
			&& !!this.fields.seller_id_number.get_value()
			&& !!this.fields.seller_address_line1.get_value()
			&& !!this.fields.seller_city.get_value()
			&& !!this.fields.seller_state.get_value()
			&& !!this.fields.seller_zip.get_value());
		const sourceOk = ordersOnly || !!this.fields.source.get_value();
		const linesOk = this.lines.length > 0;
		const $btn = this.$body.find('#btn-submit');
		const $rd = this.$body.find('#readiness');
		const missing = [];
		if (!supplierOk) missing.push(__('Supplier'));
		if (!sellerOk) missing.push(__('Seller + ID'));
		if (!sourceOk) missing.push(__('Source'));
		if (!linesOk) missing.push(__('At least one line'));
		// The per-line readiness gate the "no blanket cost > 0 rule" decision left
		// behind, now asking the same kernel _addPending asks. A line is
		// added valid, but a HEADER change can invalidate it afterwards —
		// un-ticking FFL Transfer (custody_zeroed → needs_cost) or moving the
		// Acquisition Type off Consignment both turn a legitimate $0 into a
		// purchase that has not said why. Both land here rather than at Submit.
		const awaitingCost = this.lines.filter((l) => this._lineCostGap(l));
		if (awaitingCost.length) {
			const codes = awaitingCost.map((l) => l.item_code).join(', ');
			missing.push(awaitingCost.some((l) => l.needs_cost)
				? __('Cost for {0} — the FFL Transfer box was un-ticked, so these are '
					+ 'purchases now. Open the line to enter it.', [codes])
				: __('Cost for {0}', [codes]));
		}
		// A staged gun (Incoming panel) whose echoed serial was blank needs the
		// serial read off the gun before it can book.
		const noSerial = this.lines.filter((l) => rgNeedsSerials(l) && l.serials.length !== Math.round(l.qty));
		if (noSerial.length) {
			missing.push(__('Serial for {0}', [noSerial.map((l) => l.item_code).join(', ')]));
		}
		if (missing.length) {
			$rd.text(__('Required: ') + missing.join(', ')).css('color', 'var(--text-muted)');
			$btn.prop('disabled', true);
		} else {
			$rd.text(__('Ready to receive.')).css('color', 'var(--green-700)');
			$btn.prop('disabled', false);
		}
		this._updateTotals();
	}

	// ------------------------------------------------ submit, one receive per bucket

	// Order buckets first (receive_lines: booked against the order's own house,
	// serials the receiver typed, then held for the customer), then the stock
	// bucket (create_receive, the classic path). A bucket that fails stays in the
	// session so it can be fixed and submitted again.
	//
	// Bought from a private seller: the server says first whether and how much the
	// seller is paid (seller_payment_preview: the rule and the amount the receipt
	// will book), then one dialog asks how (Cash already picked). The answer lasts
	// for this one Submit (finish() clears it): a retry asks again.
	_submitBuckets() {
		if (this.fields.acquisition_source.get_value() === 'Individual' && !this._sellerPayment) {
			this._askSellerPayment();
			return;
		}
		const groups = rgGroupByBucket(this.lines);
		const order = Object.keys(groups).filter((k) => k !== 'stock');
		const $btn = this.$body.find('#btn-submit');
		$btn.prop('disabled', true).text(__('Submitting...'));
		const failed = [];
		const results = [];
		const next = (i) => {
			if (i >= order.length) {
				this._submitStockBucket(groups.stock || [], failed, results);
				return;
			}
			const key = order[i];
			const lines = groups[key];
			const bucket = lines[0].bucket;
			const isInvoice = bucket.kind === 'invoice';
			const items = lines.map(rgBucketItem);
			frappe.call({
				method: isInvoice
					? 'ffl_integrations.distributor.receive_bridge.receive_invoice_lines'
					: 'ffl_integrations.distributor.receive_bridge.receive_lines',
				args: isInvoice ? { invoice_name: bucket.name, items: JSON.stringify(items) }
					: { do_name: bucket.name, items: JSON.stringify(items) },
				callback: (r) => {
					results.push({ bucket: key, name: (r.message || {}).name, lines });
					this.lines = this.lines.filter((l) => rgBucketOf(l) !== key);
					next(i + 1);
				},
				error: () => { failed.push(rgCandLabel(lines[0].do_meta) || bucket.name); next(i + 1); },
			});
		};
		next(0);
	}

	_askSellerPayment() {
		if (this._asking || (this._payDialog && this._payDialog.display)) return;  // a second click on Submit
		this._asking = true;
		const stock = this.lines.filter((l) => rgBucketOf(l) === 'stock');
		frappe.call({
			method: 'ffl_core.api.receive_goods.seller_payment_preview',
			args: { payload: JSON.stringify(this._stockPayload(stock)) },
			callback: (r) => {
				const preview = r.message || {};
				if (!preview.pays) {
					this._sellerPayment = { method: null };  // nobody is paid at receipt: submit as it is
					this._submitBuckets();
					return;
				}
				this._showPayDialog(preview);
			},
			always: () => { this._asking = false; },
		});
	}

	_showPayDialog(preview) {
		const esc = frappe.utils.escape_html;
		const seller = [this.fields.seller_first_name.get_value(), this.fields.seller_last_name.get_value()]
			.filter(Boolean).join(' ');
		const dialog = this._payDialog = new frappe.ui.Dialog({
			title: __('Pay the seller'),
			fields: [
				{ fieldtype: 'HTML', options: `<p>${__('Pay {0} <b>{1}</b> for these goods.', [
					esc(seller || __('the seller')), frappe.format(preview.amount, { fieldtype: 'Currency' })])}</p>` },
				{ fieldtype: 'Select', fieldname: 'method', label: __('Paid by'), reqd: 1,
					options: preview.methods.join('\n'), default: 'Cash',
					description: __('Cash comes out of the drawer; Zelle, Check and ACH out of the bank.') },
				{ fieldtype: 'Data', fieldname: 'reference', label: __('Check number, Zelle confirmation or ACH reference'),
					depends_on: "eval:doc.method !== 'Cash'", mandatory_depends_on: "eval:doc.method !== 'Cash'" },
			],
			primary_action_label: __('Pay & Receive'),
			primary_action: (v) => {
				// The shared check (pos_tender_guard.js), against the server's methods.
				const gap = ffl.payoutProblem(v.method, v.reference, preview.methods);
				if (gap) {
					frappe.msgprint(gap === 'reference' ? __('Enter the {0} reference.', [v.method])
						: __('Pick how the seller was paid.'));
					return;
				}
				dialog.hide();
				this._sellerPayment = { method: v.method, reference: String(v.reference || '').trim() };
				this._submitBuckets();
			},
		});
		dialog.show();
	}

	_submitStockBucket(stockLines, failed, results) {
		const finish = () => {
			this._sellerPayment = null;
			this._renderLines();
			this._reflectReadiness();
			this._loadIncoming();
			this.$body.find('#btn-submit').text(__('Submit & Receive'));
			const esc = frappe.utils.escape_html;
			const ok = results.map((x) => x.bucket === 'stock'
				? __('Stock: <a href="/app/purchase-receipt/{0}">{0}</a>', [x.name]) + (x.paid
					? ' · ' + __('seller paid {0} ({1})', [frappe.format(x.paid.amount, { fieldtype: 'Currency' }), esc(x.paid.method)])
					: '')
				: x.lines[0].bucket.kind === 'invoice'
					? __('Invoice {0} → <a href="/app/purchase-receipt/{1}">{1}</a>',
						[esc(rgCandLabel(x.lines[0].do_meta)), x.name])
					: __('{0} → <a href="/app/purchase-receipt/{1}">{1}</a> (held for {2})',
						[esc(x.lines[0].bucket.name), x.name, esc((x.lines[0].do_meta || {}).customer_name || __('restock'))]));
			if (ok.length) frappe.show_alert({ message: ok.join('<br>'), indicator: 'green' }, 10);
			if (failed.length) {
				frappe.msgprint({
					title: __('Some receives did not go through'), indicator: 'orange',
					message: __('Still in the session — fix and submit again: {0}', [esc(failed.join(', '))]),
				});
			}
			if (!this.lines.length) this._resetForNext();
		};
		if (!stockLines.length) { finish(); return; }
		this._submitStock(stockLines, (name, message) => {
			if (name) {
				results.push({ bucket: 'stock', name, paid: (message || {}).seller_payment });
				this.lines = this.lines.filter((l) => rgBucketOf(l) !== 'stock');
			} else {
				failed.push('stock');
			}
			finish();
		});
	}

	// The create_receive payload of these lines (also what seller_payment_preview reads).
	_stockPayload(lines) {
		return {
			supplier: this.fields.supplier.get_value(),
			source: this.fields.source.get_value(),
			acquisition_type: this.fields.acquisition_type.get_value(),
			acquisition_source: this.fields.acquisition_source.get_value(),
			is_ffl_transfer: this.fields.is_ffl_transfer.get_value() ? 1 : 0,
			transfer_for_customer: this.fields.transfer_for_customer.get_value(),
			seller_customer: this.fields.seller_customer.get_value(),
			seller_first_name: this.fields.seller_first_name.get_value(),
			seller_middle_name: this.fields.seller_middle_name.get_value(),
			seller_last_name: this.fields.seller_last_name.get_value(),
			seller_suffix: this.fields.seller_suffix.get_value(),
			seller_address_line1: this.fields.seller_address_line1.get_value(),
			seller_city: this.fields.seller_city.get_value(),
			seller_state: this.fields.seller_state.get_value(),
			seller_zip: this.fields.seller_zip.get_value(),
			id_type: this.fields.seller_id_type.get_value(),
			id_number: this.fields.seller_id_number.get_value(),
			id_document: this.fields.seller_id_document.get_value(),
			posting_date: this.fields.posting_date.get_value(),
			notes: this.fields.notes.get_value(),
			// How the private seller was paid (the Pay the seller dialog); absent otherwise.
			...(this._sellerPayment && this._sellerPayment.method ? {
				seller_payment_method: this._sellerPayment.method,
				seller_payment_reference: this._sellerPayment.reference,
			} : {}),
			items: lines.map((l) => ({
				item_code: l.item_code,
				qty: l.qty,
				rate: l.rate,
				// The zero-cost trio (create_receive Schema): confirmed + reason,
				// plus the note the server REQUIRES when the reason is "Other".
				// Sent on every line, not only the $0 ones — the server reads
				// them only where they matter, and a conditional here would be a
				// second place for the tiers to be decided.
				zero_cost_confirmed: l.zero_cost_confirmed ? 1 : 0,
				zero_cost_reason: l.zero_cost_reason || '',
				zero_cost_note: l.zero_cost_note || '',
				sell_price: l.sell_price || 0,
				consignment_commission_pct: l.consignment_commission_pct || 0,
				serials: l.serials,
				service_need: l.service_need ? 1 : 0,
				manufacturer: l.manufacturer || '',
				model_name: l.model_name || '',
				firearm_type: l.firearm_type || '',
				caliber: l.caliber || '',
				importer: l.importer || '',
				is_antique: l.is_antique ? 1 : 0,
				year_made: l.year_made || 0,
				storage_location: l.storage_location || '',
			})),
		};
	}

	_submitStock(lines, onDone) {
		frappe.call({
			method: 'ffl_core.api.receive_goods.create_receive',
			args: { payload: JSON.stringify(this._stockPayload(lines)) },
			callback: (r) => onDone(r.message?.ok ? r.message.name : null, r.message),
			error: () => onDone(null),
		});
	}

	_resetForNext() {
		this.lines = [];
		this.pending = null;
		this.session = { choices: {} };
		this.fields.scan.set_value('');
		this.fields.notes.set_value('');
		this.$body.find('#pending-area').empty();
		if (this._itemPicker) this._itemPicker.close();
		this._renderLines();
		this._reflectReadiness();
		this.$body.find('#btn-submit').text(__('Submit & Receive'));
		this.fields.scan.set_focus?.();
	}
}

// Node unit tests import the pure kernel (no DOM / Frappe). Harmless in the
// browser, where `module` is undefined.
if (typeof module !== 'undefined' && module.exports) {
	module.exports = {
		RG_ZERO_COST_REASONS,
		RG_ZERO_COST_OTHER,
		RG_CONSIGNMENT_ACQUISITION_TYPES,
		rgIsConsignmentAcquisitionType,
		rgLineCostGap,
		rgBucketKey,
		rgBucketOf,
		rgGroupByBucket,
		rgCandKind,
		rgCandBucket,
		rgCandLabel,
		rgSessionQtyFor,
		rgInvoiceCandidate,
		rgHasRoom,
		rgBucketItem,
		rgSlotRows,
		rgApplyLineEdit,
		rgNeedsSerials,
		rgSerialGap,
	};
}
