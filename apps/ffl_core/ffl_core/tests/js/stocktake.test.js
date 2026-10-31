'use strict';
// Pure kernel of the Inventory Count page: merging what was scanned over the expected
// shelf lists, the "uncounted only" filter, and the finalize selection. No DOM / Frappe.
const test = require('node:test');
const assert = require('node:assert');
const {
	stkcKey, stkcMergeSerials, stkcMergeItems, stkcFilterSerials,
	stkcFilterItems, stkcTouched, stkcMissingRows, stkcBlockedRows, stkcItemStatus, stkcTotals, stkcPatchCounted, stkcDefaultTicked,
	stkcSelectedRows, stkcNeverScanned,
} = require('../../ffl_core/page/stocktake/stocktake.js');

const GUNS = [
	{ serial: 'A1', item_code: 'G', item_name: 'Glock 19', manufacturer: 'Glock', model_name: '19', caliber: '9mm', warehouse: 'Shelf', reason: '', label: '' },
	{ serial: 'B2', item_code: 'G', item_name: 'Glock 19', manufacturer: 'Glock', model_name: '19', caliber: '9mm', warehouse: 'Shelf', reason: 'custody', label: "Customer's gun" },
];

test('a found gun is marked found and carries who and when', () => {
	const rows = stkcMergeSerials(GUNS, { A1: { entry: 'e1', by: 'amy', at: '2026-10-01 10:00:00', method: 'scan', item_code: 'G' } });
	assert.strictEqual(rows.length, 2);
	assert.deepStrictEqual([rows[0].found, rows[0].by, rows[0].entry, rows[0].extra], [true, 'amy', 'e1', false]);
	assert.deepStrictEqual([rows[1].found, rows[1].by], [false, null]);
	assert.strictEqual(rows[1].label, "Customer's gun"); // the shelf-state label survives the merge
});

test('a gun scanned that is not on the shelf list comes last and is flagged extra', () => {
	const rows = stkcMergeSerials(GUNS, {
		Z9: { entry: 'e9', by: 'bob', at: 'x', method: 'scan', item_code: 'H' },
		A1: { entry: 'e1', by: 'amy', at: 'x', method: 'manual', item_code: 'G' },
	});
	assert.deepStrictEqual(rows.map((r) => r.serial), ['A1', 'B2', 'Z9']);
	assert.strictEqual(rows[2].extra, true);
	assert.strictEqual(rows[2].found, true);
});

test('with nothing found every gun is uncounted, and no input is mutated', () => {
	const rows = stkcMergeSerials(GUNS, undefined);
	assert.ok(rows.every((r) => !r.found));
	assert.strictEqual(GUNS[0].found, undefined);
});

test('the uncounted-only filter hides found guns and keeps the rest', () => {
	const rows = stkcMergeSerials(GUNS, { A1: { entry: 'e1', by: 'amy', at: 'x', method: 'scan', item_code: 'G' } });
	assert.deepStrictEqual(stkcFilterSerials(rows, { uncountedOnly: true }).map((r) => r.serial), ['B2']);
	assert.deepStrictEqual(stkcFilterSerials(rows, { uncountedOnly: false }).map((r) => r.serial), ['A1', 'B2']);
});

test('the text filter matches serial, model and caliber, ignoring case', () => {
	const rows = stkcMergeSerials(GUNS, {});
	assert.deepStrictEqual(stkcFilterSerials(rows, { text: 'b2' }).map((r) => r.serial), ['B2']);
	assert.strictEqual(stkcFilterSerials(rows, { text: ' 9MM ' }).length, 2);
	assert.strictEqual(stkcFilterSerials(rows, { text: 'zzz' }).length, 0);
});

const ITEMS = [
	{ item_code: 'AMMO', item_name: '9mm FMJ', warehouse: 'Shelf', upc: '0123', expected: 10 },
	{ item_code: 'AMMO', item_name: '9mm FMJ', warehouse: 'Back', upc: '0123', expected: 4 },
	{ item_code: 'OPT', item_name: 'Red dot', warehouse: 'Shelf', upc: '', expected: 3 },
];

test('counted quantities land on the right item and warehouse; the rest read zero', () => {
	const rows = stkcMergeItems(ITEMS, [{ item_code: 'AMMO', warehouse: 'Back', counted: 4, last_entry: 'e5', last_delta: 1 }]);
	assert.deepStrictEqual(rows.map((r) => r.counted), [0, 4, 0]);
	assert.strictEqual(rows[1].last_entry, 'e5');
	assert.strictEqual(rows[0].last_entry, null);
});

test('the item filters: warehouse, uncounted only, text', () => {
	const rows = stkcMergeItems(ITEMS, [{ item_code: 'AMMO', warehouse: 'Shelf', counted: 7, last_entry: 'e', last_delta: 1 }]);
	assert.deepStrictEqual(stkcFilterItems(rows, { warehouse: 'Shelf' }).map((r) => r.item_code), ['AMMO', 'OPT']);
	assert.deepStrictEqual(stkcFilterItems(rows, { warehouse: 'Shelf', uncountedOnly: true }).map((r) => r.item_code), ['OPT']);
	assert.deepStrictEqual(stkcFilterItems(rows, { text: 'red' }).map((r) => r.item_code), ['OPT']);
	assert.deepStrictEqual(stkcFilterItems(rows, { text: '0123' }).length, 2);
	assert.strictEqual(stkcFilterItems(rows, {}).length, 3);
});

test('an item counted down from 8 is still counted — uncounted means untouched', () => {
	const rows = stkcMergeItems(ITEMS, [{ item_code: 'AMMO', warehouse: 'Shelf', counted: 8, last_entry: 'e', last_delta: 1 }]);
	assert.ok(!stkcFilterItems(rows, { uncountedOnly: true, warehouse: 'Shelf' }).some((r) => r.item_code === 'AMMO'));
});

test('a typed 0 counts as touched', () => {
	const rows = stkcMergeItems(ITEMS, [{ item_code: 'OPT', warehouse: 'Shelf', counted: 0, last_entry: 'e', last_delta: 0 }]);
	assert.strictEqual(stkcTouched(rows[2]), true);
	assert.deepStrictEqual(stkcFilterItems(rows, { uncountedOnly: true, warehouse: 'Shelf' }).map((r) => r.item_code), ['AMMO']);
});

test('item status: none, short, match, over', () => {
	assert.strictEqual(stkcItemStatus({ counted: 0, expected: 5 }), 'none');
	assert.strictEqual(stkcItemStatus({ counted: 3, expected: 5, last_entry: 'e' }), 'short');
	assert.strictEqual(stkcItemStatus({ counted: 5, expected: 5, last_entry: 'e' }), 'match');
	assert.strictEqual(stkcItemStatus({ counted: 6, expected: 5, last_entry: 'e' }), 'over');
	assert.strictEqual(stkcItemStatus({ counted: 0, expected: 0 }), 'match');
	// a typed 0 is a count: nothing on the shelf, against 5 on the books
	assert.strictEqual(stkcItemStatus({ counted: 0, expected: 5, last_entry: 'e' }), 'short');
});

test('totals count expected and found guns apart from extras', () => {
	const guns = stkcMergeSerials(GUNS, {
		A1: { entry: 'e1', by: 'amy', at: 'x', method: 'scan', item_code: 'G' },
		Z9: { entry: 'e9', by: 'bob', at: 'x', method: 'scan', item_code: 'H' },
	});
	const items = stkcMergeItems(ITEMS, [{ item_code: 'OPT', warehouse: 'Shelf', counted: 3, last_entry: 'e', last_delta: 1 }]);
	assert.deepStrictEqual(stkcTotals(guns, items), { gunsExpected: 2, gunsFound: 1, gunsExtra: 1, itemsTouched: 1, itemsTotal: 3 });
});

const FIN_ROWS = [{ item_code: 'A', warehouse: 'W', never_scanned: true }, { item_code: 'B', warehouse: 'W', never_scanned: false },
	{ item_code: 'C', warehouse: 'W2', never_scanned: true }, { item_code: 'D', warehouse: 'W', never_scanned: false }];

test('finalize starts with the scanned rows ticked and the never-scanned ones not', () => {
	const ticked = stkcDefaultTicked(FIN_ROWS);
	assert.deepStrictEqual(ticked, [stkcKey('B', 'W'), stkcKey('D', 'W')]);
	assert.deepStrictEqual(stkcSelectedRows(FIN_ROWS, ticked).map((r) => r.item_code), ['B', 'D']);
	assert.deepStrictEqual(stkcNeverScanned(FIN_ROWS).map((r) => r.item_code), ['A', 'C']);
});

test('set all to 0 ticks every never-scanned row and keeps the rest', () => {
	const ticked = new Set(stkcDefaultTicked(FIN_ROWS));
	stkcNeverScanned(FIN_ROWS).forEach((r) => ticked.add(stkcKey(r.item_code, r.warehouse)));
	assert.strictEqual(stkcSelectedRows(FIN_ROWS, [...ticked]).length, 4);
	ticked.delete(stkcKey('A', 'W'));
	assert.deepStrictEqual(stkcSelectedRows(FIN_ROWS, [...ticked]).map((r) => r.item_code), ['B', 'C', 'D']);
});

test('patching the counted list from a response replaces the item and drops it when no entry is left', () => {
	const counted = [{ item_code: 'A', warehouse: 'W', counted: 2, last_entry: 'e1', last_delta: 1 },
		{ item_code: 'B', warehouse: 'W', counted: 5, last_entry: 'e2', last_delta: 5 }];
	const next = stkcPatchCounted(counted, { item_code: 'A', warehouse: 'W', counted: 3, last_entry: 'e3', last_delta: 1 });
	assert.deepStrictEqual(next.find((c) => c.item_code === 'A').counted, 3);
	assert.strictEqual(next.length, 2);
	assert.strictEqual(counted[0].counted, 2); // input untouched
	const gone = stkcPatchCounted(next, { item_code: 'B', warehouse: 'W', counted: 0, last_entry: null, last_delta: null });
	assert.deepStrictEqual(gone.map((c) => c.item_code), ['A']);
	assert.strictEqual(stkcPatchCounted([], { item_code: 'N', warehouse: 'W', counted: 1, last_entry: 'x', last_delta: 1 }).length, 1);
});

test('missing rows come back as item and warehouse pairs to fetch', () => {
	const counted = [{ item_code: 'NEW', warehouse: 'Shelf', counted: 1 }, { item_code: 'OPT', warehouse: 'Shelf', counted: 2 }];
	assert.deepStrictEqual(stkcMissingRows(ITEMS, counted), [{ item_code: 'NEW', warehouse: 'Shelf' }]);
	assert.deepStrictEqual(stkcMissingRows(ITEMS, []), []);
});

test('rows with an open register sale are the ones that block finalize', () => {
	const rows = [{ item_code: 'A', open_register_sales: [{ invoice: 'P1', posted: '2026-10-01 10:00:00' }] },
		{ item_code: 'B', open_register_sales: [] }, { item_code: 'C' }];
	assert.deepStrictEqual(stkcBlockedRows(rows).map((r) => r.item_code), ['A']);
	assert.deepStrictEqual(stkcBlockedRows([rows[1], rows[2]]), []);
});
