// Executes page/pending_dispositions/pending_dispositions.js under a stub Desk,
// so the three-source card actually RUNS in a test instead of only being read.
//
// Why this exists: the page grew a third order source, and the thing that made
// that dangerous is invisible when reading a diff. Every capability on the card
// used to be spelled `!isWeb`, which quietly meant "counter" — so a GunBroker
// row arriving through the same loop would have been offered Record Payment,
// Edit and Cancel Order. Cancel Order on a GunBroker sale is not a mistake that
// shows up as an error message: the sale cannot be unwound on GunBroker's side
// at all, so the button would reverse our ledger against a sale that still
// exists, with a buyer who has the gun.
//
// So the checks below are the capability table, asserted per source, plus the
// two regressions that matter: the counter and web button sets must be exactly
// what they were, and a GunBroker card must render no `undefined` (its row
// contract has ~48 keys and the page reads all of them unconditionally).
//
// It is NOT a DOM test. jQuery is a chainable fake and assertions are made on
// the HTML strings the page builds and the calls it makes.
//
// Usage: node pending_orders_page.harness.js <path to pending_dispositions.js>
// Prints one JSON object; exit code 0 = every check passed.

'use strict';
const fs = require('fs');
const vm = require('vm');

// ------------------------------------------------------------- jQuery stub
class FakeNode {
	constructor(html, el) {
		this.html = html || '';
		this.children = [];
		this.handlers = {};
		// Delegated handlers: $card.on('click', '.selector', fn). A test fires one
		// with a synthetic event to prove the wiring from DOM event to endpoint.
		this.delegated = [];
		this.length = 0;
		this.el = el || null;
	}
	appendTo(parent) { if (parent && parent.children) parent.children.push(this); return this; }
	append(x) { this.children.push(x); return this; }
	find() { return new FakeNode(); }
	empty() { this.children = []; return this; }
	on(event, a, b) {
		if (typeof a === 'string') this.delegated.push({ event, selector: a, fn: b });
		else this.handlers[event] = a;
		return this;
	}
	// A wrapped element is a plain object a test builds: { closest: {data:{...}},
	// data: {...}, val: '...' } — enough for the handlers the page registers.
	closest() { return new FakeNode('', this.el && this.el.closest); }
	val() { return this.el && this.el.val !== undefined ? this.el.val : ''; }
	html(h) { if (h === undefined) return this.html; this.html = h; return this; }
	prop(name, value) { this.props = Object.assign(this.props || {}, { [name]: value }); return this; }
	css() { return this; }
	data(key) { return this.el && this.el.data ? this.el.data[key] : null; }
	// The sectioned page (2026-09-16) restyles buttons and reads the first match.
	addClass() { return this; }
	removeClass() { return this; }
	first() { return this; }
	filter() { return this; }
}
const $ = (x) => (typeof x === 'string' ? new FakeNode(x) : new FakeNode('', x));

// -------------------------------------------------------------- frappe stub
const calls = [];
const dialogs = [];
let responses = {};

const frappe = {
	pages: { 'pending-dispositions': {} },
	ui: {
		make_app_page: (opts) => ({
			body: new FakeNode(), wrapper: opts.parent,
			set_primary_action: () => {},
		}),
		Dialog: class {
			constructor(opts) {
				this.opts = opts;
				this.values = {};
				dialogs.push(this);
			}
			show() { this.shown = true; }
			hide() { this.shown = false; }
			get_value(f) { return this.values[f]; }
			get_values() { return this.values; }
			set_value(f, v) { this.values[f] = v; }
			get_primary_btn() { return new FakeNode(); }
			get_secondary_btn() {
				this.secondaryBtn = this.secondaryBtn || new FakeNode();
				return this.secondaryBtn;
			}
			set_df_property() {}
			get_field() { return { df: {}, set_description: () => {} }; }
		},
	},
	call: (opts) => {
		calls.push({ method: opts.method, args: opts.args });
		const message = Object.prototype.hasOwnProperty.call(responses, opts.method)
			? responses[opts.method] : [];
		if (opts.callback) opts.callback({ message });
		const thenable = {
			then: (cb) => { const out = cb ? cb({ message }) : undefined;
				return { catch: () => (out === undefined ? [] : out) }; },
			catch: () => thenable,
		};
		return thenable;
	},
	confirm: (_msg, fn) => fn && fn(),
	show_alert: () => {},
	msgprint: () => {},
	set_route: () => {},
	utils: {
		escape_html: (t) => String(t)
			.replace(/&/g, '&amp;').replace(/</g, '&lt;')
			.replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
	},
	datetime: {
		str_to_user: (v) => String(v || ''),
		get_today: () => '2026-08-26',
	},
};
const __ = (text, args) => (args || []).reduce(
	(out, a, i) => out.split(`{${i}}`).join(a), text);
const format_currency = (v) => `$${Number(v || 0).toFixed(2)}`;

// ------------------------------------------------------------------ fixtures
const counterRow = {
	source: 'counter', sales_invoice: 'ACC-SINV-0001', customer: 'Walk In',
	customer_name: 'Walk In', is_business: false, paid: false, grand_total: 500,
	outstanding_amount: 500, posting_date: '2026-08-20', destination_ffl: 'FFL-1',
	destination_ffl_name: 'Far End Guns', ffl_expired: false, needs_disposition: true,
	pending_serials: [{ item_code: 'RIFLE', serial: 'SN-C' }], disposed_serials: [],
	has_disposition: false, ship_pushed: false, shipstation_enabled: true,
	can_mark_shipped: true, has_ship_address: true, ship_to_preview: { city: 'Dallas', state: 'TX' },
	legs: null, note: '', serial_errors: [], currency: 'USD',
};
const webRow = {
	source: 'woo', woo_online_order: 'WOO-ORD-0001', woo_order_id: '9001',
	woo_status: 'processing', customer: 'Buyer', customer_name: 'Buyer',
	site: 'retail', is_business: false, paid: true, grand_total: 900,
	outstanding_amount: 0, order_total: 900, total_tax: 74, shipping_total: 45,
	discount_total: 0, posting_date: '2026-08-21 10:00:00', destination_ffl: 'FFL-2',
	destination_ffl_name: 'Web FFL', ffl_expired: false, needs_disposition: true,
	awaiting_serial: false, pending_serials: [{ item_code: 'RIFLE', serial: 'SN-W' }],
	disposed_serials: [], serial_errors: [], store_pickup: false, pickup_invoice: null,
	is_cr: false, currency: 'USD', has_ship_address: true, ship_to_preview: {},
};
const gbRow = {
	source: 'gunbroker', gunbroker_order: 'GB-ORD-00001', gb_order_id: '998877',
	gb_status_label: 'Pending Shipment', stage: 'blocked', channel_stage: 'awaiting_payment',
	posting_date: '2026-08-22 09:00:00', customer: 'GB Buyer', customer_name: 'GB Buyer',
	is_business: false, paid: false, paid_total: 0, outstanding_amount: 0,
	grand_total: 1200, order_total: 1200, currency: 'USD', shipping_total: 45,
	discount_total: 0, total_tax: 99, needs_disposition: true, has_disposition: false,
	disposed: false, disposed_serials: [], awaiting_serial: false, serial_errors: [],
	pending_serials: [{ item_code: 'RIFLE', serial: 'SN-G' }], store_pickup: false,
	pickup_invoice: null, destination_ffl: 'FFL-3', destination_ffl_name: 'GB Dealer',
	ffl_expiry_date: '2027-01-01', ffl_expired: false,
	is_cr: false, cr_prefill: null, sales_invoice: null, sales_orders: [], legs: null,
	note: '', last_payment_mode: '', ship_pushed: false, can_mark_shipped: false,
	shipstation_enabled: true, shipstation_order_id: '', ship_items: [],
	has_ship_address: true, ship_to_preview: { company: 'GB Dealer', city: 'Austin', state: 'TX' },
	site: null, woo_order_id: null, woo_status: null, woo_online_order: null, ships_direct: false,
	stock_units: [],
};

const COUNTER = 'ffl_core.api.manual_order.list_pending_dispositions';
const WEB = 'ffl_woo_sync.woocommerce.fulfillment.list_pending_web_orders';
const GB = 'ffl_integrations.gunbroker.fulfillment.list_pending_gb_orders';

// -------------------------------------------------------------------- driver
const source = fs.readFileSync(process.argv[2], 'utf8');
const sandbox = { frappe, $, __, format_currency, window: {}, console };
sandbox.window.ffl = null;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'pending_dispositions.js' });

const checks = [];
function check(name, fn) {
	try { fn(); checks.push({ name, ok: true, error: null }); }
	catch (e) { checks.push({ name, ok: false, error: e.message }); }
}
function assert(cond, message) { if (!cond) throw new Error(message); }

const wrapper = {};
frappe.pages['pending-dispositions'].on_page_load(wrapper);
const app = wrapper.pending_dispositions_app;

function render(rows) {
	calls.length = 0;
	responses = { [COUNTER]: rows.counter || [], [WEB]: rows.web || [], [GB]: rows.gb || [] };
	app._load();
	const list = app.$body.find('#list');
	// `_render` appends each card to whatever `.find('#list')` returned inside
	// itself, so drive it directly to get at the cards it built.
	const cards = [];
	const nodes = [];
	const realFind = FakeNode.prototype.find;
	const holder = new FakeNode();
	FakeNode.prototype.find = function () { return holder; };
	try {
		app._render([].concat(rows.counter || [], rows.web || [], rows.gb || []));
	} finally {
		FakeNode.prototype.find = realFind;
	}
	// The sectioned page (2026-09-16) also appends a heading string per stage
	// section; only the FakeNode cards carry the card markup.
	holder.children.forEach((c) => {
		if (c && typeof c.html === 'string' && /class="card"/.test(c.html)) { cards.push(c.html); nodes.push(c); }
	});
	return { list, cards, nodes };
}

// ------------------------------------------------------------------- checks

check('the page loads and asks all three sources', () => {
	responses = { [COUNTER]: [], [WEB]: [], [GB]: [] };
	calls.length = 0;
	app._load();
	const methods = calls.map((c) => c.method);
	assert(methods.includes(COUNTER), 'counter source not queried');
	assert(methods.includes(WEB), 'web source not queried');
	assert(methods.includes(GB), `GunBroker source not queried: ${methods.join(', ')}`);
});

check('a GunBroker card renders no undefined', () => {
	const { cards } = render({ gb: [gbRow] });
	assert(cards.length === 1, `expected 1 card, got ${cards.length}`);
	assert(!/undefined/.test(cards[0]),
		`GunBroker card rendered "undefined": ${cards[0].slice(0, 400)}`);
});

check('a GunBroker card offers Mark Payment Received, not Record Payment', () => {
	const { cards } = render({ gb: [gbRow] });
	assert(/btn-gb-paid/.test(cards[0]), 'Mark Payment Received missing');
	assert(!/btn-pay"/.test(cards[0]), 'counter Record Payment rendered on a GunBroker row');
});

check('a GunBroker card offers neither Edit nor Cancel Order', () => {
	const paid = Object.assign({}, gbRow, { stage: 'ready_ship', channel_stage: 'ready_to_dispose', paid: true });
	const { cards } = render({ gb: [paid] });
	assert(!/btn-edit-order/.test(cards[0]), 'Edit rendered on a GunBroker row');
	assert(!/btn-cancel-order/.test(cards[0]),
		'Cancel Order rendered on a GunBroker row — that sale cannot be unwound');
});

check('a GunBroker card offers no Mark Shipped', () => {
	const shipped = Object.assign({}, gbRow, { stage: 'ready_ship', channel_stage: 'awaiting_ship', disposed: true,
		can_mark_shipped: true });
	const { cards } = render({ gb: [shipped] });
	assert(!/btn-mark-shipped/.test(cards[0]),
		'Mark Shipped rendered — GunBroker tracking is written back from the label');
});

check('a paid GunBroker card offers Ship before Dispose, like a counter order', () => {
	// No invoice yet (Dispose raises it), so Ship goes to push_gb_shipment,
	// which builds the shipment off the order itself.
	const ready = Object.assign({}, gbRow, { stage: 'ready_ship', channel_stage: 'ready_to_dispose', paid: true });
	const { cards } = render({ gb: [ready] });
	assert(/btn-dispose/.test(cards[0]), 'Dispose missing');
	assert(/btn-ship"/.test(cards[0]) && !/btn-ship" disabled/.test(cards[0]),
		'Ship missing or disabled on a paid GunBroker card before Dispose');
});

check('an unpaid GunBroker card renders Ship disabled', () => {
	const unpaid = Object.assign({}, gbRow, { stage: 'blocked', channel_stage: 'awaiting_payment', paid: false });
	const { cards } = render({ gb: [unpaid] });
	assert(/btn-ship" disabled/.test(cards[0]), 'Ship live before Mark Payment Received');
});

check('a GunBroker card with a unit still to name renders Ship disabled', () => {
	const open = Object.assign({}, gbRow, { stage: 'ready_ship', channel_stage: 'ready_to_dispose', paid: true,
		assign_units: [{ row: 'R1', item_code: 'P228', unit_label: 'unit 1 of 1', serial: '', refunded: false }] });
	const { cards } = render({ gb: [open] });
	assert(/btn-ship" disabled/.test(cards[0]), 'Ship live with a unit that has no gun');
});

check('Ship on an undisposed GunBroker card pushes the order, not a null invoice', () => {
	dialogs.length = 0;
	calls.length = 0;
	app._confirmShip(Object.assign({}, gbRow, { paid: true, channel_stage: 'ready_to_dispose' }));
	const d = dialogs[dialogs.length - 1];
	assert(!/null/.test(d.opts.title), `dialog title: ${d.opts.title}`);
	d.opts.primary_action();
	const call = calls[calls.length - 1];
	assert(call.method === 'ffl_integrations.gunbroker.fulfillment.push_gb_shipment',
		`dispatched ${call.method}`);
	assert(call.args.gunbroker_order === gbRow.gunbroker_order, JSON.stringify(call.args));
});

check('a GunBroker card offers Ship once it has been disposed', () => {
	// The store buys this label itself, so Ship is the next thing to do —
	// and by now the dispose has raised the invoice to push.
	const shipping = Object.assign({}, gbRow, { stage: 'ready_ship', channel_stage: 'awaiting_ship', paid: true,
		disposed: true, needs_disposition: false, sales_invoice: 'ACC-SINV-9',
		pending_serials: [], disposed_serials: ['SN-G'] });
	const { cards } = render({ gb: [shipping] });
	assert(/btn-ship/.test(cards[0]), 'Ship missing — the store buys this label');
});

check('an all-antique GunBroker card badges no FFL and ships to the buyer', () => {
	// No dealer at the far end: the server sends ships_direct with an empty
	// destination and the buyer's address as the preview. The card must not
	// claim an FFL is valid, and Dispose (which issues the antique out of
	// stock with no bound-book entry) must still be offered.
	const antique = Object.assign({}, gbRow, { stage: 'ready_ship', channel_stage: 'ready_to_dispose', paid: true,
		ships_direct: true, destination_ffl: '', destination_ffl_name: '',
		ffl_expiry_date: null, has_ship_address: true,
		ship_to_preview: { company: null, line1: '12 Elm St', city: 'Dallas', state: 'TX', zip: '75201' } });
	const { cards } = render({ gb: [antique] });
	assert(!/FFL valid|FFL expired/.test(cards[0]), 'FFL badge on an antique-only GunBroker card');
	assert(/Dallas, TX/.test(cards[0]), `card does not name the buyer's city: ${cards[0].slice(0, 400)}`);
	assert(/btn-dispose/.test(cards[0]), 'Dispose missing on an antique-only card');
	assert(!/undefined/.test(cards[0]), 'antique card rendered "undefined"');
	dialogs.length = 0;
	app._confirmDispose(antique);
	const html = JSON.stringify(dialogs[dialogs.length - 1].opts);
	assert(/the buyer directly/.test(html), 'dispose dialog still says Ship to FFL on an antique order');
	assert(!/pushes to FastBound/.test(html), 'dispose dialog promises a FastBound push for an antique');
	assert(/No bound-book entry/.test(html), 'dispose dialog does not say the antique skips the bound book');
});

check('an item-level GunBroker order lists quantities where the serials would be', () => {
	// Ammunition, optics, parts: sold by the unit off an item-level listing, so
	// there is no serial to show. The dialog is the operator's last look at what
	// the order contains before it is booked — a box of ammunition missing from
	// it reads as an order that ships one thing while the invoice bills two.
	const ammo = Object.assign({}, gbRow, { stage: 'ready_ship', channel_stage: 'ready_to_dispose', paid: true,
		ships_direct: true, destination_ffl: '', destination_ffl_name: '',
		ffl_expiry_date: null, pending_serials: [],
		stock_units: [{ item_code: 'AMMO-9MM-115', qty: 2 }],
		ship_to_preview: { company: null, line1: '12 Elm St', city: 'Dallas', state: 'TX', zip: '75201' } });
	const { cards } = render({ gb: [ammo] });
	assert(!/FFL valid|FFL expired/.test(cards[0]), 'FFL badge on an ammunition-only card');
	dialogs.length = 0;
	app._confirmDispose(ammo);
	const html = JSON.stringify(dialogs[dialogs.length - 1].opts);
	assert(/AMMO-9MM-115/.test(html), 'dispose dialog does not name the item sold');
	assert(/Item\(s\)/.test(html), 'dispose dialog still calls them Serial(s)');
	assert(!/pushes to FastBound/.test(html), 'dispose dialog promises a FastBound push for ammunition');
	assert(!/undefined/.test(html), 'dispose dialog rendered "undefined"');
});

check('a regulated GunBroker card still badges its FFL and names the dealer', () => {
	const { cards } = render({ gb: [gbRow] });
	assert(/FFL valid/.test(cards[0]), 'FFL badge lost on a regulated GunBroker card');
	assert(/GB Dealer/.test(cards[0]), 'dealer name lost on a regulated GunBroker card');
	dialogs.length = 0;
	app._confirmDispose(gbRow);
	const html = JSON.stringify(dialogs[dialogs.length - 1].opts);
	assert(/Ship to FFL/.test(html) && /pushes to FastBound/.test(html),
		'regulated dispose dialog changed');
});

check('a GunBroker card shows the GunBroker status, not a WC status', () => {
	const { cards } = render({ gb: [gbRow] });
	assert(/GB<\/span>|GB: /.test(cards[0]), 'GunBroker status badge missing');
	assert(!/WC: /.test(cards[0]), 'WooCommerce badge on a GunBroker row');
});

check('the counter card is byte-for-byte what it was (regression)', () => {
	const { cards } = render({ counter: [counterRow] });
	const html = cards[0];
	assert(/btn-pay/.test(html), 'counter Record Payment lost');
	assert(/btn-dispose/.test(html), 'counter Dispose lost');
	assert(/btn-ship/.test(html), 'counter Ship lost');
	assert(/btn-edit-order/.test(html), 'counter Edit lost');
	assert(/btn-cancel-order/.test(html), 'counter Cancel Order lost');
	assert(!/btn-gb-paid/.test(html), 'GunBroker payment button leaked onto a counter row');
	// Unpaid: both ship actions are refused by ship_payment_block, so the card
	// offers neither — Ship renders disabled with the reason, Mark Shipped is gone.
	assert(!/btn-mark-shipped/.test(html), 'a disabled Mark Shipped beside a disabled Ship');
	assert(/Record payment before shipping/.test(html), 'the unpaid reason is missing');
});

check('a paid counter row with ShipStation off keeps Mark Shipped', () => {
	const { cards } = render({ counter: [{ ...counterRow, paid: true, outstanding_amount: 0,
		shipstation_enabled: false }] });
	const html = cards[0];
	assert(/btn-mark-shipped/.test(html), 'the only clear-the-queue action is missing');
	assert(!/btn-ship\b/.test(html), 'Ship offered with ShipStation switched off');
});

check('a paid counter row with ShipStation on offers Ship alone', () => {
	const { cards } = render({ counter: [{ ...counterRow, paid: true, outstanding_amount: 0 }] });
	const html = cards[0];
	assert(/btn-ship/.test(html), 'counter Ship lost');
	assert(!/btn-mark-shipped/.test(html), 'two buttons for one action on a shippable row');
});

check('the web card is what it was (regression)', () => {
	const { cards } = render({ web: [webRow] });
	const html = cards[0];
	assert(/btn-dispose/.test(html), 'web Dispose lost');
	assert(!/btn-pay/.test(html), 'Record Payment appeared on a web row');
	assert(!/btn-edit-order/.test(html), 'Edit appeared on a web row');
	assert(!/btn-cancel-order/.test(html), 'Cancel Order appeared on a web row');
	assert(!/btn-ship/.test(html), 'Ship appeared on a web row');
	assert(!/btn-gb-paid/.test(html), 'GunBroker payment button leaked onto a web row');
});

check('Dispose routes to the right backend for each source', () => {
	const cases = [
		[counterRow, 'ffl_core.api.manual_order.dispose_order', 'sales_invoice', 'ACC-SINV-0001'],
		[webRow, 'ffl_woo_sync.woocommerce.fulfillment.dispose_web_order',
			'woo_online_order', 'WOO-ORD-0001'],
		[gbRow, 'ffl_integrations.gunbroker.fulfillment.dispose_gb_order',
			'gunbroker_order', 'GB-ORD-00001'],
	];
	cases.forEach(([row, method, argKey, argValue]) => {
		dialogs.length = 0;
		calls.length = 0;
		app._confirmDispose(row);
		const d = dialogs[dialogs.length - 1];
		d.values.confirm = 1;
		d.opts.primary_action(d.values);
		const call = calls[calls.length - 1];
		assert(call.method === method,
			`${row.source}: dispatched ${call.method}, expected ${method}`);
		assert(call.args[argKey] === argValue,
			`${row.source}: argument ${argKey} was ${JSON.stringify(call.args)}`);
	});
});

check('Mark Payment Received sends what the operator typed', () => {
	dialogs.length = 0;
	calls.length = 0;
	app._markGbPaymentReceived(gbRow);
	const d = dialogs[dialogs.length - 1];
	const fieldNames = d.opts.fields.map((f) => f.fieldname);
	['mode_of_payment', 'reference_no', 'amount', 'received_on'].forEach((f) => {
		assert(fieldNames.includes(f), `dialog is missing ${f}`);
	});
	d.opts.primary_action({ mode_of_payment: 'GunBroker Check',
		reference_no: '9001', amount: 1200, received_on: '2026-08-26' });
	const call = calls[calls.length - 1];
	assert(call.method === 'ffl_integrations.gunbroker.fulfillment.mark_payment_received',
		`wrong method ${call.method}`);
	assert(call.args.gunbroker_order === 'GB-ORD-00001', 'order not passed');
	assert(call.args.reference_no === '9001', 'cheque number not passed');
	assert(call.args.received_on === '2026-08-26', 'deposit date not passed');
});

check('an unknown source still renders as a counter row rather than throwing', () => {
	const odd = Object.assign({}, counterRow, { source: 'martian' });
	const { cards } = render({ counter: [odd] });
	assert(cards.length === 1, 'unknown source dropped the card');
});

// ------------------------------------------- Ship dialog's second exit
// The Ship dialog carries "Mark Shipped (no ShipStation)" as its secondary
// action. Both halves matter: it must be THERE (a one-button dialog is what
// sent clerks hunting for the card button), and it must not be there on a
// source that never marks shipped.

const shipRow = Object.assign({}, counterRow, { paid: true, outstanding_amount: 0,
	ship_items: [{ qty: 1, item_name: 'Rifle', serials: ['SN-C'] }] });

check('the Ship dialog offers Mark Shipped as its secondary action', () => {
	dialogs.length = 0;
	app._confirmShip(shipRow);
	const d = dialogs[dialogs.length - 1];
	assert(/Mark Shipped/.test(d.opts.secondary_action_label || ''),
		`secondary action label was ${JSON.stringify(d.opts.secondary_action_label)}`);
	assert(typeof d.opts.secondary_action === 'function', 'no secondary action wired');
});

check('the Ship dialog\'s Mark Shipped calls the shared endpoint and closes', () => {
	dialogs.length = 0;
	calls.length = 0;
	responses = { 'ffl_core.api.manual_order.mark_shipped_manually': { ok: true } };
	app._confirmShip(shipRow);
	const d = dialogs[dialogs.length - 1];
	d.opts.secondary_action();
	// _doMarkShipped reloads the queue on success, so the mark is not the
	// last call — find it.
	const call = calls.find((c) => c.method === 'ffl_core.api.manual_order.mark_shipped_manually');
	assert(call, `no mark_shipped_manually call: ${calls.map((c) => c.method).join(', ')}`);
	assert(call.args.sales_invoice === shipRow.sales_invoice, 'invoice not passed');
	assert(d.shown === false, 'dialog stayed open after marking shipped');
	assert(calls.some((c) => c.method === COUNTER), 'the queue was not reloaded');
});

check('an undisposed order is marked shipped but told the bound book is untouched', () => {
	// The gate moved: undisposed guns no longer block the mark, so the dialog
	// has to say what it does NOT do, or the clerk reads a cleared shipping
	// leg as a cleared order.
	dialogs.length = 0;
	app._confirmShip(shipRow);
	const d = dialogs[dialogs.length - 1];
	const html = JSON.stringify(d.opts.fields[0].options);
	assert(/SHIPPING half/.test(html), `dialog does not warn: ${html.slice(0, 600)}`);
	assert(!d.get_secondary_btn().props || d.get_secondary_btn().props.disabled !== true,
		'Mark Shipped disabled on an undisposed row — that gate is gone');
});

check('an unreadable-serial row still gets Mark Shipped disabled, not hidden', () => {
	const broken = Object.assign({}, shipRow, { can_mark_shipped: false });
	dialogs.length = 0;
	app._confirmShip(broken);
	const d = dialogs[dialogs.length - 1];
	assert(/Mark Shipped/.test(d.opts.secondary_action_label || ''),
		'the button vanished instead of going dead');
	assert(d.get_secondary_btn().props.disabled === true,
		'the server would refuse this row, so the button must be disabled');
});

check('a GunBroker Ship dialog offers no Mark Shipped at all', () => {
	const gbShip = Object.assign({}, gbRow, { stage: 'ready_ship', channel_stage: 'awaiting_ship', paid: true,
		disposed: true, needs_disposition: false, sales_invoice: 'ACC-SINV-9',
		pending_serials: [], disposed_serials: ['SN-G'], ship_items: [] });
	dialogs.length = 0;
	app._confirmShip(gbShip);
	const d = dialogs[dialogs.length - 1];
	assert(!d.opts.secondary_action_label,
		'GunBroker reports its own tracking — Mark Shipped must not be offered');
	assert(!/Mark Shipped/.test(JSON.stringify(d.opts.fields[0].options)),
		'GunBroker dialog explains a button it does not have');
});

// ------------------------------------- Fulfill from local stock (2026-09-16)
// The waiting-on-distributor products collapse behind ONE button, and that
// button exists only when the shelf can actually cover a line: the per-product
// pill offered "Fulfill from stock" on lines with nothing behind them, so the
// only possible outcome of the click was "No unheld X on the shelf".

const GUN_SUB = { row_name: 'r1', item_code: 'RIFLE', item_name: 'Rifle', qty: 1,
	is_firearm: true, distributor: 'RSR', do_name: 'DO-1', do_line: 'l1', available: 2 };
const ACC_SUB = { row_name: 'r2', item_code: 'HOLSTER', item_name: 'Holster', qty: 2,
	is_firearm: false, distributor: 'RSR', do_name: 'DO-1', do_line: 'l2', available: 5 };
const waitingRow = (subs, trouble) => Object.assign({}, counterRow, {
	stage: 'waiting', paid: true, outstanding_amount: 0, needs_disposition: false,
	pending_serials: [], can_mark_shipped: false,
	legs: { local: { undelivered: 0, pending_serials: 0 }, awaiting_arrival: 1,
		awaiting_goods: true, distributor_orders: [], releasable: false,
		substitutable: subs, trouble: trouble || [] },
});

check('waiting lines the shelf can cover collapse into one counted button', () => {
	const { cards } = render({ counter: [waitingRow([GUN_SUB, ACC_SUB])] });
	const html = cards[0];
	const buttons = (html.match(/btn-fulfill-stock/g) || []).length;
	assert(buttons === 1, `one button for the whole card, got ${buttons}`);
	assert(/Fulfill from local stock \(2\)/.test(html),
		`button does not say how many lines it can serve: ${html.slice(0, 600)}`);
});

check('a waiting line with nothing on the shelf offers no fulfil button', () => {
	const { cards } = render({ counter: [waitingRow([Object.assign({}, GUN_SUB, { available: 0 }),
		Object.assign({}, ACC_SUB, { available: 1 })])] });
	assert(!/btn-fulfill-stock/.test(cards[0]),
		'offered Fulfill from local stock with no unheld gun and a short shelf');
});

check('a troubled waiting line keeps Re-source and Cancel line on the card', () => {
	const trouble = [{ do_name: 'DO-1', distributor: 'RSR', kind: 'killed',
		detail: 'RIFLE', web_ref: 'W-9' }];
	const { cards } = render({ counter: [waitingRow([GUN_SUB], trouble)] });
	assert(/btn-resource/.test(cards[0]), 'Re-source lost when the pills collapsed');
	assert(/btn-cancel-line/.test(cards[0]), 'Cancel line lost when the pills collapsed');
});

check('the picker fulfils the line the operator chose, no second confirm', () => {
	dialogs.length = 0;
	calls.length = 0;
	app._pickFromStock(waitingRow([GUN_SUB, ACC_SUB]), [GUN_SUB, ACC_SUB]);
	const d = dialogs[dialogs.length - 1];
	const opts = d.opts.fields[0].options;
	assert(opts.length === 2 && opts[0].value === 'r1', 'the dialog does not list the lines');
	assert(/5 in local stock/.test(JSON.stringify(opts)),
		`the dialog does not show the units free: ${JSON.stringify(opts)}`);
	d.values.line = 'r2';
	d.opts.primary_action();
	const call = calls.find((c) => c.method === 'ffl_core.api.manual_order.fulfill_from_stock');
	assert(call, `nothing fulfilled: ${calls.map((c) => c.method).join(', ')}`);
	assert(call.args.row_name === 'r2', `fulfilled ${call.args.row_name} instead of the pick`);
	assert(d.shown === false, 'the picker stayed open');
});

check('one servable line skips the picker and goes straight to the action', () => {
	dialogs.length = 0;
	calls.length = 0;
	app._pickFromStock(waitingRow([ACC_SUB]), [ACC_SUB]);
	const call = calls.find((c) => c.method === 'ffl_core.api.manual_order.fulfill_from_stock');
	assert(call && call.args.row_name === 'r2', 'the single line was not served');
});


// ------------------------------- Model-level GunBroker units (Run 2)
// One listing sells N interchangeable guns; the card names a gun for every unit
// sold before Dispose may run. The checks below drive the page the way a clerk
// does: the rendered controls, then the events those controls fire.

const ASSIGN = 'ffl_integrations.gunbroker.fulfillment.assign_gb_unit_serial';
const REFUND = 'ffl_integrations.gunbroker.fulfillment.mark_gb_unit_refunded';
const U0 = '16087299:0';
const U1 = '16087299:1';
const unit = (row, extra) => Object.assign({
	row, item_code: 'CZ75', unit_label: `CZ 75 · unit ${Number(row.split(':')[1]) + 1} of 2`,
	serial: '', suggested: '', suggested_label: '', refunded: false, error: '',
	assigned_by: '', assigned_at: null }, extra || {});
const modelRow = (units, extra) => Object.assign({}, gbRow, {
	stage: 'ready_ship', channel_stage: 'ready_to_dispose', paid: true, pending_serials: [],
	needs_disposition: true, assign_units: units,
	assign_candidates: { CZ75: [{ serial: 'CZ-1', label: 'CZ-1 · received 2026-01-02' },
		{ serial: 'CZ-2', label: 'CZ-2 · received 2026-02-03' }] },
}, extra || {});
const fire = (node, event, selector, el) => {
	const h = node.delegated.find((d) => d.event === event && d.selector === selector);
	assert(h, `no ${event} handler on ${selector}`);
	h.fn({ currentTarget: el, key: el && el.key, preventDefault: () => {} });
};
const lastCall = (method) => calls.filter((c) => c.method === method).pop();

check('a unit with a suggestion shows it with Confirm, a scan field and a pick list', () => {
	const row = modelRow([unit(U0, { suggested: 'CZ-1', suggested_label: 'CZ-1 · received 2026-01-02' }),
		unit(U1, { suggested: 'CZ-2', suggested_label: 'CZ-2 · received 2026-02-03' })]);
	const { cards } = render({ gb: [row] });
	const html = cards[0];
	assert(!/undefined/.test(html), `card rendered "undefined": ${html.slice(0, 500)}`);
	assert((html.match(/class="assign-unit"/g) || []).length === 2, 'one row per unit expected');
	assert(/CZ-1 · received/.test(html), 'the suggested gun is not shown');
	assert((html.match(/btn-assign-confirm/g) || []).length === 2, 'a Confirm button per unit expected');
	assert((html.match(/assign-scan/g) || []).length === 2, 'a scan field per unit expected');
	assert((html.match(/assign-pick/g) || []).length === 2, 'a pick list per unit expected');
	assert(/<option value="CZ-2">/.test(html), 'the pick list does not offer the free guns');
	assert(/unit 1 of 2/.test(html) && /unit 2 of 2/.test(html), 'units are not labelled');
	assert(/btn-assign-all/.test(html), 'two suggestions should offer one Confirm all button');
});

check('Dispose is disabled until every unit has a gun or is refunded', () => {
	const open = modelRow([unit(U0, { serial: 'CZ-1' }), unit(U1, { suggested: 'CZ-2' })]);
	let { cards } = render({ gb: [open] });
	assert(/btn-dispose"\s+disabled/.test(cards[0]), 'Dispose is live with an unassigned unit');
	assert(/Assign a gun to every unit first/.test(cards[0]), 'the reason is not shown');
	const done = modelRow([unit(U0, { serial: 'CZ-1' }), unit(U1, { serial: 'CZ-2' })],
		{ pending_serials: [{ item_code: 'CZ75', serial: 'CZ-1' }, { item_code: 'CZ75', serial: 'CZ-2' }] });
	({ cards } = render({ gb: [done] }));
	assert(/btn-dispose"\s*>/.test(cards[0]) && !/btn-dispose"\s+disabled/.test(cards[0]),
		'Dispose still disabled with every unit assigned');
	const refunded = modelRow([unit(U0, { serial: 'CZ-1' }), unit(U1, { refunded: true })],
		{ pending_serials: [{ item_code: 'CZ75', serial: 'CZ-1' }] });
	({ cards } = render({ gb: [refunded] }));
	assert(!/btn-dispose"\s+disabled/.test(cards[0]), 'a refunded unit should not hold Dispose back');
	assert(/Refunded on GunBroker/.test(cards[0]) && /btn-unit-unrefund/.test(cards[0]),
		'a refunded unit should say so and offer Undo');
});

check('a unit whose gun was lost to a conflict keeps Dispose disabled', () => {
	const row = modelRow([unit(U0, { serial: 'CZ-1', error: 'Held by woo WOO-3' }), unit(U1, { serial: 'CZ-2' })]);
	const { cards } = render({ gb: [row] });
	assert(/btn-dispose"\s+disabled/.test(cards[0]), 'Dispose is live with a conflicted unit');
	assert(/Held by woo WOO-3/.test(cards[0]), 'the conflict is not shown on the unit');
});

check('an assigned unit offers Unassign instead of Confirm', () => {
	const row = modelRow([unit(U0, { serial: 'CZ-1' }), unit(U1, { serial: 'CZ-2' })]);
	const { cards } = render({ gb: [row] });
	assert(/btn-unassign/.test(cards[0]), 'Unassign missing');
	assert(!/btn-assign-confirm/.test(cards[0]), 'Confirm offered on an assigned unit');
	assert(!/btn-assign-all/.test(cards[0]), 'Confirm all offered with nothing to confirm');
});

check('a unit with no gun to suggest says so but still takes a scan or a pick', () => {
	const row = modelRow([unit(U0, { suggested: 'CZ-1' }), unit(U1)]);
	const { cards } = render({ gb: [row] });
	assert(/No free gun to suggest/.test(cards[0]), 'the empty suggestion is not explained');
	assert((html => (html.match(/assign-scan/g) || []).length)(cards[0]) === 2, 'scan missing on an empty unit');
	assert(!/btn-assign-all/.test(cards[0]), 'one suggestion needs no Confirm all');
});

check('Confirm sends the suggested serial for that unit and reloads the queue', () => {
	const row = modelRow([unit(U0, { suggested: 'CZ-1' }), unit(U1, { suggested: 'CZ-2' })]);
	const { nodes } = render({ gb: [row] });
	responses = { [ASSIGN]: { ok: true }, [GB]: [], [COUNTER]: [], [WEB]: [] };
	calls.length = 0;
	fire(nodes[0], 'click', '.btn-assign-confirm',
		{ closest: { data: { row: U1 } }, data: { serial: 'CZ-2' } });
	const call = lastCall(ASSIGN);
	assert(call, 'assign_gb_unit_serial was not called');
	assert(call.args.gunbroker_order === 'GB-ORD-00001' && call.args.row_name === U1 && call.args.serial_no === 'CZ-2',
		`wrong arguments: ${JSON.stringify(call.args)}`);
	assert(calls.some((c) => c.method === GB), 'the queue was not reloaded after assigning');
});

check('a scanned serial (Enter) assigns that unit, and an empty scan does nothing', () => {
	const row = modelRow([unit(U0, { suggested: 'CZ-1' }), unit(U1, { suggested: 'CZ-2' })]);
	const { nodes } = render({ gb: [row] });
	responses = { [ASSIGN]: { ok: true }, [GB]: [], [COUNTER]: [], [WEB]: [] };
	calls.length = 0;
	fire(nodes[0], 'keydown', '.assign-scan', { key: 'Enter', val: '  CZ-9  ', closest: { data: { row: U0 } } });
	const call = lastCall(ASSIGN);
	assert(call && call.args.serial_no === 'CZ-9' && call.args.row_name === U0,
		`scan sent ${JSON.stringify(call && call.args)}`);
	calls.length = 0;
	fire(nodes[0], 'keydown', '.assign-scan', { key: 'Enter', val: '   ', closest: { data: { row: U0 } } });
	fire(nodes[0], 'keydown', '.assign-scan', { key: 'a', val: 'CZ-9', closest: { data: { row: U0 } } });
	assert(!lastCall(ASSIGN), 'a blank scan or another key called the server');
});

check('picking a gun from the list assigns it', () => {
	const row = modelRow([unit(U0, { suggested: 'CZ-1' }), unit(U1, { suggested: 'CZ-2' })]);
	const { nodes } = render({ gb: [row] });
	responses = { [ASSIGN]: { ok: true }, [GB]: [], [COUNTER]: [], [WEB]: [] };
	calls.length = 0;
	fire(nodes[0], 'change', '.assign-pick', { val: 'CZ-2', closest: { data: { row: U0 } } });
	const call = lastCall(ASSIGN);
	assert(call && call.args.serial_no === 'CZ-2' && call.args.row_name === U0, 'pick not sent');
	calls.length = 0;
	fire(nodes[0], 'change', '.assign-pick', { val: '', closest: { data: { row: U0 } } });
	assert(!lastCall(ASSIGN), 'the empty placeholder option called the server');
});

check('Unassign sends an empty serial', () => {
	const row = modelRow([unit(U0, { serial: 'CZ-1' }), unit(U1, { serial: 'CZ-2' })]);
	const { nodes } = render({ gb: [row] });
	responses = { [ASSIGN]: { ok: true }, [GB]: [], [COUNTER]: [], [WEB]: [] };
	calls.length = 0;
	fire(nodes[0], 'click', '.btn-unassign', { closest: { data: { row: U1 } } });
	const call = lastCall(ASSIGN);
	assert(call && call.args.serial_no === '' && call.args.row_name === U1, 'unassign not sent');
});

check('Confirm all assigns every suggested unit, one after the other', () => {
	const row = modelRow([unit(U0, { suggested: 'CZ-1' }), unit(U1, { suggested: 'CZ-2' })]);
	const { nodes } = render({ gb: [row] });
	responses = { [ASSIGN]: { ok: true }, [GB]: [], [COUNTER]: [], [WEB]: [] };
	calls.length = 0;
	fire(nodes[0], 'click', '.btn-assign-all', {});
	const sent = calls.filter((c) => c.method === ASSIGN).map((c) => `${c.args.row_name}=${c.args.serial_no}`);
	assert(sent.join(',') === `${U0}=CZ-1,${U1}=CZ-2`, `sent ${sent.join(',')}`);
	assert(calls.some((c) => c.method === GB), 'the queue was not reloaded at the end');
});

check('Confirm all stops at the first refusal', () => {
	const row = modelRow([unit(U0, { suggested: 'CZ-1' }), unit(U1, { suggested: 'CZ-2' })]);
	const { nodes } = render({ gb: [row] });
	responses = { [ASSIGN]: {}, [GB]: [], [COUNTER]: [], [WEB]: [] };   // no ok:true
	calls.length = 0;
	fire(nodes[0], 'click', '.btn-assign-all', {});
	assert(calls.filter((c) => c.method === ASSIGN).length === 1, 'carried on after a refusal');
});

check('marking a unit refunded asks once, then calls the server', () => {
	const row = modelRow([unit(U0, { suggested: 'CZ-1' }), unit(U1)]);
	const { nodes } = render({ gb: [row] });
	const asked = [];
	const realConfirm = frappe.confirm;
	frappe.confirm = (msg, fn) => { asked.push(msg); fn(); };
	responses = { [REFUND]: { ok: true }, [GB]: [], [COUNTER]: [], [WEB]: [] };
	calls.length = 0;
	try {
		fire(nodes[0], 'click', '.btn-unit-refunded', { closest: { data: { row: U1 } } });
	} finally { frappe.confirm = realConfirm; }
	assert(asked.length === 1 && /refunded/.test(asked[0]), 'the one confirmation is missing');
	const call = lastCall(REFUND);
	assert(call && call.args.row_name === U1 && call.args.refunded === 1
		&& call.args.gunbroker_order === 'GB-ORD-00001', `refund sent ${JSON.stringify(call && call.args)}`);
});

check('undoing a refund needs no confirmation', () => {
	const row = modelRow([unit(U0, { serial: 'CZ-1' }), unit(U1, { refunded: true })]);
	const { nodes } = render({ gb: [row] });
	const realConfirm = frappe.confirm;
	frappe.confirm = () => { throw new Error('undo should not ask'); };
	responses = { [REFUND]: { ok: true }, [GB]: [], [COUNTER]: [], [WEB]: [] };
	calls.length = 0;
	try {
		fire(nodes[0], 'click', '.btn-unit-unrefund', { closest: { data: { row: U1 } } });
	} finally { frappe.confirm = realConfirm; }
	const call = lastCall(REFUND);
	assert(call && call.args.refunded === 0, 'undo not sent');
});

check('a per-gun GunBroker card shows no assignment controls (regression)', () => {
	const { cards } = render({ gb: [gbRow] });
	assert(!/assign-unit|assign-scan|btn-assign/.test(cards[0]), 'assignment controls on a per-gun card');
	assert(!/Assign a gun to every unit/.test(cards[0]), 'unit hint on a per-gun card');
	assert(!/btn-dispose"\s+disabled/.test(cards[0]), 'Dispose disabled on a card with no units');
});

check('counter and web cards never grow assignment controls, even handed units', () => {
	const stray = [unit(U0, { suggested: 'CZ-1' })];
	const { cards } = render({
		counter: [Object.assign({}, counterRow, { assign_units: stray })],
		web: [Object.assign({}, webRow, { assign_units: stray })] });
	cards.forEach((html) => {
		assert(!/assign-unit|btn-assign/.test(html), 'assignment controls leaked onto a non-GunBroker card');
		assert(!/Assign a gun to every unit/.test(html), 'unit hint leaked onto a non-GunBroker card');
	});
});

check('unit labels and serials are escaped', () => {
	const row = modelRow([unit(U0, { unit_label: '<img src=x onerror=1>', serial: '<b>CZ</b>' }), unit(U1, { serial: 'CZ-2' })]);
	const { cards } = render({ gb: [row] });
	assert(!/<img src=x/.test(cards[0]) && !/<b>CZ<\/b>/.test(cards[0]), 'unescaped markup in a unit row');
});

check('the kernel counts the units that still need staff', () => {
	const open = sandbox.pdOpenUnits;
	assert(typeof open === 'function', 'pdOpenUnits is not exposed');
	assert(open([]) === 0 && open(undefined) === 0, 'no units is nothing open');
	assert(open([{ serial: '' }, { serial: 'A' }, { serial: '', refunded: true }]) === 1, 'unassigned only');
	assert(open([{ serial: 'A', error: 'taken' }]) === 1, 'a conflicted unit is still open');
	assert(sandbox.pdSuggestedUnits([{ suggested: 'A' }, { serial: 'B', suggested: 'C' },
		{ suggested: 'D', refunded: true }, { suggested: '' }]).length === 1, 'only a live suggestion counts');
});

const failed = checks.filter((c) => !c.ok);
process.stdout.write(JSON.stringify({ checks, failed: failed.length }, null, 1));
process.exit(failed.length ? 1 : 0);
