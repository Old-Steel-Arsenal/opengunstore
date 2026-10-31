'use strict';
// Pure kernel of the Cash Drawer page: money and time text, the difference wording, when a reason is needed,
// the pre-checks of the three forms, and the lines table. No DOM / Frappe.
const test = require('node:test');
const assert = require('node:assert');
const {
	cdrwMoney, cdrwEsc, cdrwTime, cdrwDifferenceText, cdrwNeedsReason, cdrwCheckAmount,
	cdrwCheckExpense, cdrwRows, cdrwVoucherUrl, cdrwPreviewHtml, cdrwLogRows, cdrwLogCsv,
} = require('../../ffl_core/page/cash_drawer/cash_drawer.js');

test('money is dollars and cents with a sign only when negative', () => {
	assert.strictEqual(cdrwMoney(1234.5), '$1,234.50');
	assert.strictEqual(cdrwMoney(-8.91), '-$8.91');
	assert.strictEqual(cdrwMoney(0), '$0.00');
	assert.strictEqual(cdrwMoney(undefined), '$0.00');
	assert.strictEqual(cdrwMoney(0.1 + 0.2), '$0.30');
});

test('a time reads the way the counter reads it', () => {
	assert.strictEqual(cdrwTime('2026-10-02 14:03:11.123456'), '2:03 PM');
	assert.strictEqual(cdrwTime('2026-10-02 00:05:00'), '12:05 AM');
	assert.strictEqual(cdrwTime('2026-10-02 12:00:00'), '12:00 PM');
	assert.strictEqual(cdrwTime(null), '');
});

test('the difference is said as short, over or matching', () => {
	assert.strictEqual(cdrwDifferenceText(-8.91), '$8.91 short');
	assert.strictEqual(cdrwDifferenceText(5), '$5.00 over');
	assert.strictEqual(cdrwDifferenceText(0), 'Matches the books');
	assert.strictEqual(cdrwDifferenceText(0.004), 'Matches the books');
});

test('a reason is needed from the second count on, at the limit and beyond', () => {
	assert.strictEqual(cdrwNeedsReason(-20, 20, false), true);
	assert.strictEqual(cdrwNeedsReason(20, 20, false), true);
	assert.strictEqual(cdrwNeedsReason(-19.99, 20, false), false);
	assert.strictEqual(cdrwNeedsReason(0, 20, false), false);
	assert.strictEqual(cdrwNeedsReason(-880, 20, true), false); // the first count sets the books
});

test('the forms say what is missing before they ask the server', () => {
	assert.strictEqual(cdrwCheckAmount(0), 'Enter the amount.');
	assert.strictEqual(cdrwCheckAmount(-3), 'Enter the amount.');
	assert.strictEqual(cdrwCheckAmount('abc'), 'Enter the amount.');
	assert.strictEqual(cdrwCheckAmount(12), '');
	const ok = { amount: 43.3, account: 'Print and Stationery - TSA', memo: 'paper', receipt: '/private/files/r.png' };
	assert.strictEqual(cdrwCheckExpense(ok, 200), '');
	assert.strictEqual(cdrwCheckExpense({ ...ok, receipt: '' }, 200), '');
	assert.strictEqual(cdrwCheckExpense({ ...ok, memo: '  ' }, 200), 'Write what the money was spent on.');
	assert.strictEqual(cdrwCheckExpense({ ...ok, account: '' }, 200), 'Pick what it was for.');
	assert.match(cdrwCheckExpense({ ...ok, amount: 200.01 }, 200), /limited to \$200\.00 each/);
	assert.strictEqual(cdrwCheckExpense({ ...ok, amount: 200 }, 200), '');
});

test('the lines table offers Undo only to a manager, only where the server said it could be undone', () => {
	const lines = [
		{ when: '2026-10-02 10:00:00', label: 'Bank deposit', detail: 'SLIP-1', voucher_type: 'Journal Entry', voucher_no: 'JV-1', amount: -100, entry: 'CDE-1', can_undo: true },
		{ when: '2026-10-02 11:00:00', label: 'Cash sale (register)', detail: '', voucher_type: 'Sales Invoice', voucher_no: 'SI-1', amount: 80, entry: null, can_undo: false },
	];
	const asManager = cdrwRows(lines, true);
	assert.strictEqual(asManager[0].undo, 'CDE-1');
	assert.strictEqual(asManager[1].undo, '');
	assert.strictEqual(asManager[0].time, '10:00 AM');
	assert.strictEqual(cdrwRows(lines, false)[0].undo, '');
	assert.strictEqual(asManager[0].fix, '');
	const payout = [{ when: '2026-10-02 10:00:00', label: 'Seller paid for a gun', voucher_type: 'Journal Entry', voucher_no: 'JV-2',
		amount: -300, entry: 'CDE-2', can_undo: false, can_fix: true, acquisition: 'ACQ-1' }];
	assert.strictEqual(cdrwRows(payout, true)[0].fix, 'ACQ-1');
	assert.strictEqual(cdrwRows(payout, true)[0].undo, '');
	assert.strictEqual(cdrwRows(payout, true)[0].fixReceipt, '');
});

test('a seller paid for received goods can be undone and its method changed, through its receipt', () => {
	const goods = [{ when: '2026-10-06 15:00:00', label: 'Seller paid for received goods', detail: 'MAT-PRE-1 Jane (Cash)',
		voucher_type: 'Journal Entry', voucher_no: 'JV-3', amount: -20, entry: 'CDE-3', can_undo: true, can_fix: true,
		acquisition: null, purchase_receipt: 'MAT-PRE-1' }];
	const row = cdrwRows(goods, true)[0];
	assert.strictEqual(row.undo, 'CDE-3');
	assert.strictEqual(row.fixReceipt, 'MAT-PRE-1');
	assert.strictEqual(row.fix, '');
	const clerk = cdrwRows([{ ...goods[0], can_undo: false, can_fix: false }], false)[0];
	assert.strictEqual(clerk.undo, '');
	assert.strictEqual(clerk.fixReceipt, '');
});


test('text from the books is escaped and vouchers link to their form', () => {
	assert.strictEqual(cdrwEsc('<b>"x" & \'y\'</b>'), '&lt;b&gt;&quot;x&quot; &amp; &#39;y&#39;&lt;/b&gt;');
	assert.strictEqual(cdrwVoucherUrl('Journal Entry', 'ACC-JV-2026-00001'), '/app/journal-entry/ACC-JV-2026-00001');
	assert.strictEqual(cdrwVoucherUrl('POS Invoice', 'A B'), '/app/pos-invoice/A%20B');
});

test('the close dialog tells the first count apart from a big difference', () => {
	const first = cdrwPreviewHtml({ expected: 1000, counted: 120, variance: -880, threshold: 20, first_count: true });
	assert.match(first, /first count/);
	assert.doesNotMatch(first, /short reason/);
	const big = cdrwPreviewHtml({ expected: 220, counted: 200, variance: -20, threshold: 20, first_count: false });
	assert.match(big, /\$20\.00 or more off: write a short reason/);
	const small = cdrwPreviewHtml({ expected: 220, counted: 219, variance: -1, threshold: 20, first_count: false });
	assert.doesNotMatch(small, /short reason/);
	assert.match(small, /\$1\.00 short/);
	assert.strictEqual(cdrwPreviewHtml(null), '');
});

test('the log rows carry date, time, who and the running balance', () => {
	const rows = cdrwLogRows([{ posting_date: '2026-10-02', when: '2026-10-02 14:03:11', label: 'Bank deposit', detail: 'S1',
		by: 'a@b.c', voucher_type: 'Journal Entry', voucher_no: 'JV-1', amount: -30, balance: 190 }]);
	assert.deepStrictEqual(rows[0], { date: '2026-10-02', time: '2:03 PM', label: 'Bank deposit', detail: 'S1', by: 'a@b.c',
		voucherType: 'Journal Entry', voucherNo: 'JV-1', amount: -30, balance: 190 });
	assert.deepStrictEqual(cdrwLogRows(null), []);
	assert.strictEqual(cdrwLogRows([{ posting_date: '2026-10-02', when: '2026-10-03 09:15:00' }])[0].time,
		'entered 2026-10-03 9:15 AM');
});

test('the log CSV has opening, in / out columns, closing, and never a live formula', () => {
	const csv = cdrwLogCsv({ from_date: '2026-10-01', to_date: '2026-10-02', opening: 100, closing: 170, cash_in: 100, cash_out: 30,
		lines: [
			{ posting_date: '2026-10-02', when: '2026-10-02 09:00:00', label: 'Cash sale (counter order)', detail: '=HYPERLINK("x")',
				by: 'u', voucher_no: 'SI-1', amount: 100, balance: 200 },
			{ posting_date: '2026-10-02', when: '2026-10-02 10:00:00', label: 'Bank deposit', detail: 'slip, 7',
				by: 'u', voucher_no: 'JV-1', amount: -30, balance: 170 },
		] });
	const lines = csv.split('\n');
	assert.strictEqual(lines[0], 'Date,Time,What,Details,By,Document,In,Out,Balance');
	assert.strictEqual(lines[1], '2026-10-01,,Opening balance,,,,,,100');
	assert.strictEqual(lines[2], `2026-10-02,9:00 AM,Cash sale (counter order),"'=HYPERLINK(""x"")",u,SI-1,100,,200`);
	assert.strictEqual(lines[3], '2026-10-02,10:00 AM,Bank deposit,"slip, 7",u,JV-1,,30,170');
	assert.strictEqual(lines[4], '2026-10-02,,Closing balance,,,,100,30,170');
	assert.match(cdrwLogCsv({ lines: [{ detail: 'safe\r=1+1', amount: 1 }] }), /"safe\r=1\+1"/);
});
