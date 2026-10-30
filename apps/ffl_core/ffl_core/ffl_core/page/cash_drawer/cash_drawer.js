// Cash Drawer — what the drawer should hold, the four things staff do with it, and the log.
//
//   Close the day   count the drawer, one confirmation (a reason only when it is far off)
//   Deposit         cash taken to the bank
//   Add from bank   cash taken out of the bank to fill the drawer
//   Cash expense    a small purchase paid from the drawer, receipt photo optional
//   Log (tab)       every cash movement in a date range with the running balance, CSV export
//
// The books decide what the drawer should hold; the page only shows it. The functions below are a
// PURE KERNEL (no DOM, no frappe, no __()), unit-tested under Node in tests/js/cash_drawer.test.js.
// The `cdrw` prefix keeps them clear of the other desk pages, which share one global scope.

function cdrwMoney(n) {
	const v = Math.round((Number(n) || 0) * 100) / 100;
	const s = Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
	return (v < 0 ? '-$' : '$') + s;
}

function cdrwEsc(text) {
	return String(text == null ? '' : text).replace(/[&<>"']/g, (c) => (
		{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// "2026-10-02 14:03:11.123456" -> "2:03 PM"
function cdrwTime(text) {
	const m = /(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(String(text || ''));
	if (!m) return '';
	const h = Number(m[4]);
	return `${h % 12 || 12}:${m[5]} ${h < 12 ? 'AM' : 'PM'}`;
}

// counted minus expected, said the way staff say it.
function cdrwDifferenceText(variance) {
	const v = Math.round((Number(variance) || 0) * 100) / 100;
	if (v === 0) return 'Matches the books';
	return `${cdrwMoney(Math.abs(v))} ${v < 0 ? 'short' : 'over'}`;
}

// A reason is asked for only from the second count on, and only when the drawer is far enough off.
function cdrwNeedsReason(variance, threshold, firstCount) {
	if (firstCount) return false;
	return Math.abs(Math.round((Number(variance) || 0) * 100) / 100) >= Number(threshold);
}

// The message a form should show before it even asks the server, or ''.
function cdrwCheckAmount(amount) {
	const a = Number(amount);
	if (!(a > 0)) return 'Enter the amount.';
	return '';
}

function cdrwCheckExpense(v, cap) {
	const bad = cdrwCheckAmount(v.amount);
	if (bad) return bad;
	if (!v.account) return 'Pick what it was for.';
	if (!String(v.memo || '').trim()) return 'Write what the money was spent on.';
	if (Number(v.amount) > Number(cap)) {
		return `Cash expenses are limited to ${cdrwMoney(cap)} each. Pay this one by card or from the bank.`;
	}
	return '';
}

// The log's lines as table rows: date, time, who, and the drawer balance after each line. The time is when the line
// was entered; one entered on another day than it is dated for says so.
function cdrwLogRows(lines) {
	return (lines || []).map((l) => ({
		date: String(l.posting_date || ''),
		time: String(l.when || '').slice(0, 10) === String(l.posting_date || '') || !l.when
			? cdrwTime(l.when) : `entered ${String(l.when).slice(0, 10)} ${cdrwTime(l.when)}`,
		label: l.label,
		detail: l.detail || '',
		by: l.by || '',
		voucherType: l.voucher_type,
		voucherNo: l.voucher_no,
		amount: Number(l.amount) || 0,
		balance: Number(l.balance) || 0,
	}));
}

// The log as CSV text (quoted where needed), opening and closing balances first and last.
function cdrwLogCsv(log) {
	const cell = (v) => {
		// Text that a spreadsheet would run as a formula (a customer named "=..." ) is kept as text.
		const t = (typeof v === 'string' && /^[=+\-@\t\r]/.test(v) ? "'" : '') + String(v == null ? '' : v);
		return /[",\r\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
	};
	const out = [['Date', 'Time', 'What', 'Details', 'By', 'Document', 'In', 'Out', 'Balance']];
	out.push([log.from_date, '', 'Opening balance', '', '', '', '', '', log.opening]);
	cdrwLogRows(log.lines).forEach((r) => out.push([r.date, r.time, r.label, r.detail, r.by, r.voucherNo,
		r.amount > 0 ? r.amount : '', r.amount < 0 ? -r.amount : '', r.balance]));
	out.push([log.to_date, '', 'Closing balance', '', '', '', log.cash_in, log.cash_out, log.closing]);
	return out.map((row) => row.map(cell).join(',')).join('\n');
}

// Today's lines as table rows, newest last. A line a manager can undo carries its entry name; a seller payment whose
// method may be changed carries its acquisition (a gun) or its Purchase Receipt (received goods).
function cdrwRows(lines, isManager) {
	return (lines || []).map((l) => ({
		time: cdrwTime(l.when),
		label: l.label,
		detail: l.detail || '',
		voucherType: l.voucher_type,
		voucherNo: l.voucher_no,
		amount: Number(l.amount) || 0,
		undo: !!(isManager && l.can_undo && l.entry) ? l.entry : '',
		fix: l.can_fix && l.acquisition ? l.acquisition : '',
		fixReceipt: l.can_fix && l.purchase_receipt ? l.purchase_receipt : '',
	}));
}

function cdrwVoucherUrl(doctype, name) {
	return `/app/${String(doctype).toLowerCase().replace(/ /g, '-')}/${encodeURIComponent(name)}`;
}

// What the Close dialog shows under the counted field.
function cdrwPreviewHtml(p) {
	if (!p) return '';
	const rows = [
		`<div>The books say the drawer should hold <b>${cdrwMoney(p.expected)}</b>.</div>`,
		`<div>You counted <b>${cdrwMoney(p.counted)}</b>: <b>${cdrwEsc(cdrwDifferenceText(p.variance))}</b>.</div>`,
	];
	if (p.first_count) {
		rows.push('<div class="text-muted">This is the first count. The books will be set to what you counted '
			+ 'and your accountant gets a note; nothing is booked as a loss.</div>');
	} else if (cdrwNeedsReason(p.variance, p.threshold, p.first_count)) {
		rows.push(`<div class="text-danger">That is ${cdrwMoney(p.threshold)} or more off: write a short reason below.</div>`);
	}
	return rows.join('');
}

// Guarded so Node can require the kernel above. Harmless in the browser, where frappe is always defined.
if (typeof frappe !== 'undefined') {
	frappe.pages['cash-drawer'].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __('Cash Drawer'),
			single_column: true,
		});
		wrapper.cash_drawer_app = new CashDrawerApp(page);
	};
	frappe.pages['cash-drawer'].on_page_show = function (wrapper) {
		const app = wrapper.cash_drawer_app;
		if (app) app.showTab(app.tab);
	};
}

const CDRW_API = 'ffl_core.api.cash_drawer';

class CashDrawerApp {
	constructor(page) {
		this.page = page;
		this.data = null;
		this.$body = $(`
			<div class="cdrw">
				<style>
					.cdrw { max-width: 900px; margin: 0 auto; padding: 4px 0 80px; }
					.cdrw .card-box { background: var(--card-bg); border: 1px solid var(--border-color);
						border-radius: 8px; padding: 14px 16px; margin-bottom: 12px; }
					.cdrw .cdrw-expected { font-size: 34px; font-weight: 700; line-height: 1.1; }
					.cdrw .cdrw-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
					.cdrw .cdrw-actions .btn { min-height: 44px; padding: 8px 18px; font-size: 14px; }
					.cdrw table { width: 100%; border-collapse: collapse; }
					.cdrw th, .cdrw td { padding: 7px 8px; border-bottom: 1px solid var(--border-color);
						font-size: 13px; vertical-align: middle; text-align: left; }
					.cdrw td.num, .cdrw th.num { text-align: right; white-space: nowrap; }
					.cdrw .in { color: var(--green-600); } .cdrw .out { color: var(--red-600); }
					.cdrw .undo { cursor: pointer; margin-left: 10px; }
				</style>
				<div class="btn-group cdrw-tabs" style="margin-bottom:12px">
					<button class="btn btn-default btn-sm" data-tab="today">${__('Today')}</button>
					<button class="btn btn-default btn-sm" data-tab="log">${__('Log')}</button>
				</div>
				<div class="cdrw-today">
					<div class="cdrw-summary"></div>
					<div class="cdrw-actions"></div>
					<div class="cdrw-lines"></div>
				</div>
				<div class="cdrw-log" style="display:none">
					<div class="card-box cdrw-log-filters" style="display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end"></div>
					<div class="cdrw-log-body"></div>
				</div>
			</div>`).appendTo(this.page.main);
		this.$summary = this.$body.find('.cdrw-summary');
		this.$actions = this.$body.find('.cdrw-actions');
		this.$lines = this.$body.find('.cdrw-lines');
		this.$logBody = this.$body.find('.cdrw-log-body');
		this.tab = 'today';
		this.$body.find('.cdrw-tabs .btn').on('click', (e) => this.showTab($(e.currentTarget).data('tab')));
		this.makeLogFilters();
		this.showTab('today');
	}

	showTab(tab) {
		this.tab = tab;
		this.$body.find('.cdrw-tabs .btn').each((_i, b) => $(b).toggleClass('btn-primary', $(b).data('tab') === tab)
			.toggleClass('btn-default', $(b).data('tab') !== tab));
		this.$body.find('.cdrw-today').toggle(tab === 'today');
		this.$body.find('.cdrw-log').toggle(tab === 'log');
		return tab === 'log' ? this.loadLog() : this.refresh();
	}

	makeLogFilters() {
		const $f = this.$body.find('.cdrw-log-filters');
		const field = (fieldname, label, value) => {
			const $w = $('<div style="min-width:160px"></div>').appendTo($f);
			const c = frappe.ui.form.make_control({ parent: $w, render_input: true,
				df: { fieldtype: 'Date', fieldname, label, change: () => this.loadLog() } });
			c.set_value(value);
			return c;
		};
		this.logFrom = field('from_date', __('From'), frappe.datetime.add_days(frappe.datetime.get_today(), -6));
		this.logTo = field('to_date', __('To'), frappe.datetime.get_today());
		$(`<button class="btn btn-default btn-sm" style="margin-bottom:10px">${__('Export CSV')}</button>`).appendTo($f)
			.on('click', () => this.exportLog());
	}

	loadLog() {
		if (this.tab !== 'log' || !this.logFrom) return Promise.resolve();
		const args = { from_date: this.logFrom.get_value(), to_date: this.logTo.get_value() };
		if (!args.from_date || !args.to_date) return Promise.resolve();
		const seq = (this.logSeq = (this.logSeq || 0) + 1);
		this.log = null; // nothing to export until THIS range has loaded
		this.$logBody.html(`<div class="card-box text-muted">${__('Loading…')}</div>`);
		const failed = () => {
			if (seq === this.logSeq) this.$logBody.html(`<div class="card-box text-muted">${__('The log could not be loaded. Check the dates and try again.')}</div>`);
		};
		return this.call('get_log', args, { error: failed }).then((r) => {
			if (seq !== this.logSeq) return; // a newer request owns the table
			if (!r.message) return failed();
			this.log = r.message;
			this.renderLog();
		});
	}

	renderLog() {
		const log = this.log;
		const rows = cdrwLogRows(log.lines);
		const body = rows.map((r) => `
			<tr>
				<td style="white-space:nowrap">${cdrwEsc(r.date)} ${cdrwEsc(r.time)}</td>
				<td>${cdrwEsc(r.label)}</td>
				<td>${cdrwEsc(r.detail)} <a href="${cdrwVoucherUrl(r.voucherType, r.voucherNo)}" class="text-muted">${cdrwEsc(r.voucherNo)}</a></td>
				<td class="text-muted">${cdrwEsc(r.by)}</td>
				<td class="num in">${r.amount > 0 ? cdrwMoney(r.amount) : ''}</td>
				<td class="num out">${r.amount < 0 ? cdrwMoney(-r.amount) : ''}</td>
				<td class="num">${cdrwMoney(r.balance)}</td>
			</tr>`).join('');
		const empty = rows.length ? '' : `<tr><td colspan="7" class="text-muted">${__('No cash moved in these dates.')}</td></tr>`;
		this.$logBody.html(`<div class="card-box" style="overflow-x:auto"><table>
			<thead><tr><th>${__('When')}</th><th>${__('What')}</th><th>${__('Details')}</th><th>${__('By')}</th>
				<th class="num">${__('In')}</th><th class="num">${__('Out')}</th><th class="num">${__('Balance')}</th></tr></thead>
			<tbody>
				<tr><td colspan="6" class="text-muted">${__('Opening balance (books)')}</td><td class="num">${cdrwMoney(log.opening)}</td></tr>
				${body}${empty}
				<tr><td colspan="4"><b>${__('Totals')}</b></td><td class="num in">${cdrwMoney(log.cash_in)}</td>
					<td class="num out">${cdrwMoney(log.cash_out)}</td><td class="num"><b>${cdrwMoney(log.closing)}</b></td></tr>
			</tbody></table>
			<div class="text-muted small" style="margin-top:6px">${__('Count differences and the first count change the balance but are not counted as cash in or out.')}</div>
			</div>`);
	}

	exportLog() {
		if (!this.log) return;
		const blob = new Blob([cdrwLogCsv(this.log)], { type: 'text/csv' });
		const a = document.createElement('a');
		a.href = URL.createObjectURL(blob);
		a.download = `cash-log-${this.log.from_date}-to-${this.log.to_date}.csv`;
		a.click();
		setTimeout(() => URL.revokeObjectURL(a.href), 1000);
	}

	call(method, args, opts) {
		return frappe.call(Object.assign({ method: `${CDRW_API}.${method}`, args }, opts || {}));
	}

	refresh() {
		return this.call('get_today', {}).then((r) => {
			if (!r.message) return;
			this.data = r.message;
			this.render();
		});
	}

	render() {
		const d = this.data;
		const counted = d.closed_today;
		const status = counted
			? `<span class="indicator-pill green">${__('Counted at {0}', [cdrwTime(counted.counted_at)])}: `
				+ `${cdrwMoney(counted.counted)} · ${counted.is_cutoff ? __('first count, books set to it')
					: cdrwEsc(cdrwDifferenceText(counted.variance))}</span>`
			: `<span class="indicator-pill orange">${__('Not counted yet today')}</span>`;
		const shifts = d.open_shifts
			? ` <span class="text-muted">${__('{0} register shift(s) still open; closing the day closes them.', [d.open_shifts])}</span>` : '';
		const pending = d.pending
			? `<div class="text-muted small">${__('Includes {0} from register sales still being posted.', [cdrwMoney(d.pending)])}</div>` : '';
		this.$summary.html(`
			<div class="card-box">
				<div class="text-muted">${__('The drawer should hold')}</div>
				<div class="cdrw-expected">${cdrwMoney(d.expected)}</div>
				${pending}
				<div class="text-muted small" style="margin-top:6px">
					${__('Opening {0} · in {1} · out {2} since the last close', [cdrwMoney(d.opening), cdrwMoney(d.cash_in), cdrwMoney(d.cash_out)])}
				</div>
				<div style="margin-top:8px">${status}${shifts}</div>
			</div>`);
		this.renderActions();
		this.renderLines();
	}

	renderActions() {
		const d = this.data;
		this.$actions.empty();
		const add = (label, cls, fn) => {
			$(`<button class="btn ${cls}">${label}</button>`).appendTo(this.$actions).on('click', fn);
		};
		add(__('Close the day'), 'btn-primary', () => this.closeDialog());
		add(__('Deposit to bank'), 'btn-default', () => this.depositDialog());
		add(__('Add cash from bank'), 'btn-default', () => this.fromBankDialog());
		add(__('Cash expense'), 'btn-default', () => this.expenseDialog());
		if (d.can_undo_close) {
			$(`<a class="undo text-muted" style="align-self:center">${__('Undo the last count')}</a>`)
				.appendTo(this.$actions).on('click', () => this.undoClose());
		}
	}

	renderLines() {
		const d = this.data;
		const rows = cdrwRows(d.lines, d.is_manager);
		let html = '';
		if (!rows.length && !d.other && !d.older_lines) {
			html = `<div class="card-box text-muted">${__('No cash has moved since the last close.')}</div>`;
		} else {
			const body = rows.map((r) => `
				<tr>
					<td>${cdrwEsc(r.time)}</td>
					<td>${cdrwEsc(r.label)}</td>
					<td>${cdrwEsc(r.detail)} <a href="${cdrwVoucherUrl(r.voucherType, r.voucherNo)}" class="text-muted">${cdrwEsc(r.voucherNo)}</a></td>
					<td class="num ${r.amount < 0 ? 'out' : 'in'}">${cdrwMoney(r.amount)}${
	r.undo ? `<a class="undo text-muted" data-entry="${cdrwEsc(r.undo)}">${__('Undo')}</a>` : ''}${
	r.fix ? `<a class="undo text-muted" data-fix="${cdrwEsc(r.fix)}">${__('Change method')}</a>` : ''}${
	r.fixReceipt ? `<a class="undo text-muted" data-fix-receipt="${cdrwEsc(r.fixReceipt)}">${__('Change method')}</a>` : ''}</td>
				</tr>`).join('');
			const older = d.older_lines ? `<tr><td></td><td colspan="2" class="text-muted">${
				__('+{0} older lines (before today), net', [d.older_lines])}</td><td class="num">${cdrwMoney(d.older_net)}</td></tr>` : '';
			const other = d.other ? `<tr><td></td><td colspan="2" class="text-muted">${__('Other changes the list does not show')}</td>
				<td class="num">${cdrwMoney(d.other)}</td></tr>` : '';
			html = `<div class="card-box"><table>
				<thead><tr><th>${__('Time')}</th><th>${__('What')}</th><th>${__('Details')}</th><th class="num">${__('Amount')}</th></tr></thead>
				<tbody>${older}${body}${other}</tbody></table></div>`;
		}
		// Corrections to the days before the first count: in the books, never in the drawer, left out of the figure above.
		if (Math.abs(Number(d.history) || 0) >= 0.005) {
			html += `<div class="card-box text-muted small">${__('Corrections to days before the first count, not in the drawer: {0}. The next count books them to the first count.',
				[cdrwMoney(d.history)])}</div>`;
		}
		// Seller payments that went to a bank account: not in the drawer, listed so they can be undone or their method changed.
		if ((d.bank_payouts || []).length) {
			const bank = d.bank_payouts.map((b) => `
				<tr><td>${cdrwEsc(b.method)}</td><td>${cdrwEsc(b.detail)}</td><td class="num">${cdrwMoney(b.amount)}${
	!b.can_fix ? '' : b.purchase_receipt
		? `<a class="undo text-muted" data-fix-receipt="${cdrwEsc(b.purchase_receipt)}">${__('Change method')}</a>`
		: `<a class="undo text-muted" data-fix="${cdrwEsc(b.acquisition)}">${__('Change method')}</a>`}${
	b.can_undo ? `<a class="undo text-muted" data-entry="${cdrwEsc(b.entry)}">${__('Undo')}</a>` : ''}</td></tr>`).join('');
			html += `<div class="card-box"><div class="text-muted small" style="margin-bottom:6px">${__('Seller payments by bank today (not in the drawer)')}</div>
				<table><tbody>${bank}</tbody></table></div>`;
		}
		// Receive Goods purchases from private sellers with no seller payment standing (never recorded, or undone):
		// a manager records it from here.
		if ((d.unpaid_goods_receipts || []).length) {
			const unpaid = d.unpaid_goods_receipts.map((u) => `
				<tr><td>${cdrwEsc(u.purchase_receipt)}</td><td>${cdrwEsc(u.posting_date)}</td><td>${cdrwEsc(u.customer || '')}${
	u.method ? ` (${__('was')} ${cdrwEsc(u.method)})` : ''}</td><td class="num">${cdrwMoney(u.amount)}${
	u.can_fix ? `<a class="undo text-muted" data-fix-receipt="${cdrwEsc(u.purchase_receipt)}">${u.method ? __('Record again') : __('Record payment')}</a>${
		''}<a class="undo text-muted" data-paid-outside="${cdrwEsc(u.purchase_receipt)}">${__('Paid outside the system')}</a>` : ''}</td></tr>`).join('');
			html += `<div class="card-box"><div class="text-muted small" style="margin-bottom:6px">${__('Purchases from private sellers with no seller payment recorded')}</div>
				<table><tbody>${unpaid}</tbody></table></div>`;
		}
		this.$lines.html(html);
		this.$lines.find('a.undo[data-entry]').on('click', (e) => this.undoEntry($(e.currentTarget).data('entry')));
		this.$lines.find('a.undo[data-fix]').on('click', (e) => this.payoutDialog($(e.currentTarget).data('fix')));
		this.$lines.find('a.undo[data-fix-receipt]').on('click', (e) => this.goodsPayoutDialog($(e.currentTarget).data('fix-receipt')));
		this.$lines.find('a.undo[data-paid-outside]').on('click', (e) => this.paidOutsideDialog($(e.currentTarget).data('paid-outside')));
	}

	done(message) {
		frappe.show_alert({ message, indicator: 'green' }, 6);
		this.log = null;
		return this.refresh();
	}

	// ---- the four actions ------------------------------------------------

	depositDialog() {
		const d = this.data;
		const dialog = new frappe.ui.Dialog({
			title: __('Deposit to the bank'),
			fields: [
				{ fieldtype: 'Currency', fieldname: 'amount', label: __('Amount'), reqd: 1 },
				{ fieldtype: 'Data', fieldname: 'reference', label: __('Deposit slip or reference (optional)') },
				{ fieldtype: 'HTML', options: `<div class="text-muted small">${__('Goes to {0}.', [cdrwEsc(d.bank_account)])}</div>` },
			],
			primary_action_label: __('Record deposit'),
			primary_action: (v) => {
				const bad = cdrwCheckAmount(v.amount);
				if (bad) return frappe.msgprint(bad);
				this.call('record_deposit', { company: d.company, amount: v.amount, reference: v.reference }, { freeze: true })
					.then((r) => { if (r.message) { dialog.hide(); this.done(__('Deposit recorded: {0}', [cdrwMoney(v.amount)])); } });
			},
		});
		dialog.show();
	}

	fromBankDialog() {
		const d = this.data;
		const dialog = new frappe.ui.Dialog({
			title: __('Add cash from the bank'),
			fields: [
				{ fieldtype: 'Currency', fieldname: 'amount', label: __('Amount put in the drawer'), reqd: 1 },
				{ fieldtype: 'Data', fieldname: 'reference', label: __('Withdrawal slip or reference (optional)') },
				{ fieldtype: 'HTML', options: `<div class="text-muted small">${d.bank_account
					? __('Taken from {0}.', [cdrwEsc(d.bank_account)]) : __("Taken from the store's bank account (set up on first use).")}</div>` },
			],
			primary_action_label: __('Record'),
			primary_action: (v) => {
				const bad = cdrwCheckAmount(v.amount);
				if (bad) return frappe.msgprint(bad);
				this.call('record_from_bank', { company: d.company, amount: v.amount, reference: v.reference }, { freeze: true })
					.then((r) => { if (r.message) { dialog.hide(); this.done(__('Cash added from bank: {0}', [cdrwMoney(v.amount)])); } });
			},
		});
		dialog.show();
	}

	expenseDialog() {
		const d = this.data;
		const dialog = new frappe.ui.Dialog({
			title: __('Cash expense'),
			fields: [
				{ fieldtype: 'Currency', fieldname: 'amount', label: __('Amount paid (tax included)'), reqd: 1,
					description: __('Up to {0} each.', [cdrwMoney(d.expense_cap)]) },
				{ fieldtype: 'Select', fieldname: 'account', label: __('What it was for'), reqd: 1,
					options: [{ value: '', label: '' }, ...d.expense_accounts.map((a) => ({ value: a.name, label: a.label }))] },
				{ fieldtype: 'Small Text', fieldname: 'memo', label: __('What was bought'), reqd: 1 },
				{ fieldtype: 'Attach', fieldname: 'receipt', label: __('Photo of the receipt (optional)') },
			],
			primary_action_label: __('Record expense'),
			primary_action: (v) => {
				const bad = cdrwCheckExpense(v, d.expense_cap);
				if (bad) return frappe.msgprint(bad);
				this.call('record_expense', {
					company: d.company, amount: v.amount, expense_account: v.account, memo: v.memo, receipt: v.receipt,
				}, { freeze: true }).then((r) => {
					if (r.message) { dialog.hide(); this.done(__('Expense recorded: {0}', [cdrwMoney(v.amount)])); }
				});
			},
		});
		dialog.show();
	}

	closeDialog() {
		const d = this.data;
		let preview = null;
		let timer = null;
		const hasCount = (v) => !(v === undefined || v === null || v === '');
		const dialog = new frappe.ui.Dialog({
			title: __('Close the day'),
			fields: [
				{ fieldtype: 'Currency', fieldname: 'counted', label: __('Cash counted in the drawer'), reqd: 1,
					onchange: () => schedule() },
				{ fieldtype: 'HTML', fieldname: 'preview' },
				{ fieldtype: 'Small Text', fieldname: 'reason', label: __('Why is it off?'), hidden: 1 },
			],
			primary_action_label: __('Close the day'),
			primary_action: (v) => {
				if (!hasCount(v.counted)) return frappe.msgprint(__('Enter the cash you counted.'));
				// The figures on screen must be for THIS count: look again first when they are not (a quick
				// confirm before the preview arrived), so the reason box is there when it is needed.
				const current = preview && Number(preview.counted) === Number(v.counted) ? Promise.resolve() : refreshPreview();
				current.then(() => {
					const values = dialog.get_values(true) || v;
					if (preview && cdrwNeedsReason(preview.variance, preview.threshold, preview.first_count)
							&& !String(values.reason || '').trim()) {
						return frappe.msgprint(__('Write a short reason for the difference.'));
					}
					this.call('close_day', {
						company: d.company, counted: values.counted, reason: values.reason,
						expected_seen: preview ? preview.expected : null,
					}, {
						freeze: true, freeze_message: __('Closing the day…'),
						// Refused (for instance the books moved while counting): show the new expected figure and
						// let the person confirm once more, on that figure.
						error: () => { preview = null; refreshPreview(); },
					}).then((r) => {
						if (!r.message) return;
						dialog.hide();
						const m = r.message;
						if (m.pos_errors && m.pos_errors.length) {
							frappe.msgprint({ title: __('A register shift did not close'), indicator: 'orange',
								message: __('Your count is saved, but this shift needs a manager: {0}', [cdrwEsc(m.pos_errors.join('; '))]) });
						}
						this.done(__('Day closed: {0}', [cdrwDifferenceText(m.variance)]));
					});
				});
			},
		});
		const refreshPreview = () => {
			const counted = dialog.get_value('counted');
			if (!hasCount(counted)) return Promise.resolve();
			return this.call('preview_close', { company: d.company, counted }).then((r) => {
				if (!r.message) return;
				preview = r.message;
				dialog.fields_dict.preview.$wrapper.html(cdrwPreviewHtml(preview));
				dialog.set_df_property('reason', 'hidden', cdrwNeedsReason(preview.variance, preview.threshold, preview.first_count) ? 0 : 1);
			});
		};
		const schedule = () => {
			clearTimeout(timer);
			timer = setTimeout(refreshPreview, 300);
		};
		dialog.fields_dict.counted.$input.on('input', schedule);
		dialog.onhide = () => clearTimeout(timer);
		dialog.show();
		dialog.fields_dict.counted.$input.trigger('focus');
	}

	payoutDialog(acquisition) {
		const dialog = new frappe.ui.Dialog({
			title: __('How was the seller paid?'),
			fields: [
				{ fieldtype: 'Select', fieldname: 'method', label: __('Paid by'), reqd: 1, options: this.data.payout_methods.join('\n'), default: 'Cash' },
				{ fieldtype: 'HTML', options: `<div class="text-muted small">${__('Replaces the payment recorded for {0}.', [cdrwEsc(acquisition)])}</div>` },
			],
			primary_action_label: __('Record'),
			primary_action: (v) => {
				this.call('record_payout', { acquisition, method: v.method }, { freeze: true })
					.then((r) => { if (r.message) { dialog.hide(); this.done(__('Seller payment recorded: {0}', [v.method])); } });
			},
		});
		dialog.show();
	}

	// A seller paid for received goods (Receive Goods): the method, and its reference unless Cash. Replaces the payment
	// recorded at receive, or records an undone one again (refused once a count relied on it).
	goodsPayoutDialog(receipt) {
		const methods = this.data.payout_methods;
		const dialog = new frappe.ui.Dialog({
			title: __('How was the seller paid?'),
			fields: [
				{ fieldtype: 'Select', fieldname: 'method', label: __('Paid by'), reqd: 1, options: methods.join('\n'), default: 'Cash' },
				{ fieldtype: 'Data', fieldname: 'reference', label: __('Check number, Zelle confirmation or ACH reference'),
					depends_on: "eval:doc.method !== 'Cash'", mandatory_depends_on: "eval:doc.method !== 'Cash'" },
				{ fieldtype: 'HTML', options: `<div class="text-muted small">${__('The seller payment for {0}.', [cdrwEsc(receipt)])}</div>` },
			],
			primary_action_label: __('Record'),
			primary_action: (v) => {
				// The shared check (pos_tender_guard.js), against the server's methods.
				const bad = ffl.payoutProblem(v.method, v.reference, methods);
				if (bad) {
					return frappe.msgprint(bad === 'reference' ? __('Enter the {0} reference.', [v.method]) : __('Pick how the seller was paid.'));
				}
				this.call('record_goods_payout', { purchase_receipt: receipt, method: v.method, reference: v.reference }, { freeze: true })
					.then((r) => {
						// Recorded only when the server names the entry it booked.
						if (r.message && r.message.entry) { dialog.hide(); this.done(__('Seller payment recorded: {0}', [v.method])); }
					});
			},
		});
		dialog.show();
	}

	// The seller of this purchase was paid outside the system (a hand journal entry, a check booked in QuickBooks):
	// one click opens this, the note is required, the button confirms. Books nothing; the purchase leaves the list and
	// cannot be paid here until the mark is cleared (on the receipt).
	paidOutsideDialog(receipt) {
		const dialog = new frappe.ui.Dialog({
			title: __('Paid outside the system'),
			fields: [
				{ fieldtype: 'HTML', options: `<div class="text-muted small">${__('Nothing is booked. {0} leaves this list and no seller payment can be recorded for it until the mark is cleared.', [cdrwEsc(receipt)])}</div>` },
				{ fieldtype: 'Small Text', fieldname: 'note', label: __('How was the seller paid? (journal entry, check number)'), reqd: 1 },
			],
			primary_action_label: __('Mark as paid outside'),
			primary_action: (v) => {
				if (!String(v.note || '').trim()) return frappe.msgprint(__('Write how the seller was paid.'));
				this.call('mark_paid_outside', { purchase_receipt: receipt, note: v.note }, { freeze: true })
					.then((r) => { if (r.message && r.message.purchase_receipt) { dialog.hide(); this.done(__('Marked as paid outside the system')); } });
			},
		});
		dialog.show();
	}

	undoEntry(entry) {
		frappe.confirm(__('Undo this entry? The cash goes back into the drawer total.'), () => {
			this.call('undo_entry', { entry }, { freeze: true }).then((r) => { if (r.message) this.done(__('Entry undone')); });
		});
	}

	undoClose() {
		const last = this.data.last_close;
		frappe.confirm(__('Undo the last count? The day reads as not counted again; closed register shifts stay closed.'), () => {
			this.call('undo_close', { close: last.name }, { freeze: true }).then((r) => { if (r.message) this.done(__('Count undone')); });
		});
	}
}

// Node (the unit tests) has `module`; the browser does not.
if (typeof module !== 'undefined' && module.exports) {
	module.exports = {
		cdrwMoney, cdrwEsc, cdrwTime, cdrwDifferenceText, cdrwNeedsReason, cdrwCheckAmount,
		cdrwCheckExpense, cdrwRows, cdrwVoucherUrl, cdrwPreviewHtml, cdrwLogRows, cdrwLogCsv,
	};
}
