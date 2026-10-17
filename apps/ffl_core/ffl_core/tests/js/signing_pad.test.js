'use strict';
const test = require('node:test');
const assert = require('node:assert');
const pad = require('../../public/agreement/signing_pad.js');

const zigzag = [[[0.1, 0.5], [0.2, 0.3], [0.4, 0.7], [0.6, 0.4]]];

test('normalize: CSS pixels to pad-relative, clamped to the pad', () => {
	assert.deepStrictEqual(pad.normalize(300, 50, 600, 200), [0.5, 0.25]);
	assert.deepStrictEqual(pad.normalize(-5, 260, 600, 200), [0, 1]);
	assert.deepStrictEqual(pad.normalize(10, 10, 0, 0), [0, 0]);
});

test('backingSize: the canvas store follows devicePixelRatio 1, 2 and 3', () => {
	assert.deepStrictEqual(pad.backingSize(600, 200, 1), { width: 600, height: 200 });
	assert.deepStrictEqual(pad.backingSize(600, 200, 2), { width: 1200, height: 400 });
	assert.deepStrictEqual(pad.backingSize(333.4, 111.1, 3), { width: 1000, height: 333 });
	assert.deepStrictEqual(pad.backingSize(600, 200, 0), { width: 600, height: 200 });
});

test('a point lands on the same CSS spot whatever the dpr', () => {
	[1, 2, 3].forEach((dpr) => {
		const size = pad.backingSize(600, 200, dpr);
		const [x, y] = pad.normalize(150, 120, 600, 200);
		// draw() works in CSS pixels under ctx.setTransform(dpr): x * cssW is the spot.
		assert.deepStrictEqual([x * 600, y * 200], [150, 120]);
		assert.strictEqual(size.width / 600, dpr);
	});
});

test('toPixels: one signature, any size — portrait, landscape, the 1200x400 export', () => {
	assert.deepStrictEqual(pad.toPixels(zigzag, 600, 200)[0][1], [120, 60]);
	assert.deepStrictEqual(pad.toPixels(zigzag, 900, 300)[0][1], [180, 90]);
	assert.deepStrictEqual(pad.toPixels(zigzag, pad.EXPORT.width, pad.EXPORT.height)[0][3], [720, 160]);
	assert.deepStrictEqual(pad.EXPORT, { width: 1200, height: 400 });
});

test('bbox and hasInk: nothing, a dot and a tap are not a signature', () => {
	assert.strictEqual(pad.bbox([]), null);
	assert.strictEqual(pad.hasInk([]), false);
	assert.strictEqual(pad.hasInk([[]]), false);
	assert.strictEqual(pad.hasInk([[[0.5, 0.5]]]), false);
	assert.strictEqual(pad.hasInk([[[0.5, 0.5], [0.51, 0.51]]]), false);
	assert.deepStrictEqual(pad.bbox(zigzag), { minX: 0.1, minY: 0.3, maxX: 0.6, maxY: 0.7 });
	assert.strictEqual(pad.hasInk(zigzag), true);
});

test('draw: strokes become paths on the context; a single point becomes a dot', () => {
	const calls = [];
	const ctx = new Proxy({}, {
		get: (t, k) => (k in t ? t[k] : (...a) => calls.push([k, ...a])),
		set: (t, k, v) => { t[k] = v; return true; },
	});
	pad.draw(ctx, [...zigzag, [[0.5, 0.5]]], 1200, 400);
	assert.strictEqual(ctx.lineWidth, pad.lineWidth(1200));
	assert.deepStrictEqual(calls.filter((c) => c[0] === 'moveTo')[0], ['moveTo', 120, 200]);
	assert.strictEqual(calls.filter((c) => c[0] === 'quadraticCurveTo').length, 2);
	assert.strictEqual(calls.filter((c) => c[0] === 'stroke').length, 1);
	assert.strictEqual(calls.filter((c) => c[0] === 'arc').length, 1);
	assert.ok(pad.lineWidth(300) >= 2);
});

test('serverMessage: frappe errors as plain text, 429 explained', () => {
	const body = { _server_messages: JSON.stringify([JSON.stringify({ message: 'This signing link is <b>not</b> valid.' })]) };
	assert.strictEqual(pad.serverMessage(body, 417), 'This signing link is not valid.');
	assert.match(pad.serverMessage({}, 429), /Too many tries/);
	assert.strictEqual(pad.serverMessage({ exception: 'frappe.exceptions.ValidationError: Type your full name.' }, 417),
		'Type your full name.');
	assert.match(pad.serverMessage(null, 500), /Something went wrong/);
});

test('device key: kept, read back, cleared; broken or blocked storage reads as not paired', () => {
	const mem = {};
	const storage = { getItem: (k) => (k in mem ? mem[k] : null), setItem: (k, v) => { mem[k] = String(v); },
		removeItem: (k) => { delete mem[k]; } };
	const d = pad.device(storage);
	assert.strictEqual(d.get(), null);
	assert.strictEqual(d.set('s'.repeat(43), 'Counter 1 iPad'), true);
	assert.deepStrictEqual(d.get(), { secret: 's'.repeat(43), name: 'Counter 1 iPad' });
	d.clear();
	assert.strictEqual(d.get(), null);
	mem['ffl-signing-terminal'] = '{not json';
	assert.strictEqual(d.get(), null);
	const blocked = { getItem: () => { throw new Error('denied'); }, setItem: () => { throw new Error('denied'); },
		removeItem: () => { throw new Error('denied'); } };
	const b = pad.device(blocked);
	assert.strictEqual(b.get(), null);
	assert.strictEqual(b.set('x', 'y'), false);
	b.clear();
});
