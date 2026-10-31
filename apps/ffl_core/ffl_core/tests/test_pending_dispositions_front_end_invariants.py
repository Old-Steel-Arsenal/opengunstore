"""Source-level guard over a prod-active Pending Order front-end bug.

Same shape as test_consignment_out_front_end_invariants.py: this repo ships no JS
test runner, and the defect guarded here is a bug of *arrangement* — the right
call in the wrong sequence — that no Python test and no static read of the diff
catches. So pin the shape of the fix by reading the shipped source and asserting
the invariant that makes the bug impossible. The parsing helpers are reused from
that module rather than re-implemented.

  pending_dispositions.js  _confirmCancel looks up get_primary_btn()
                                   again AFTER removeClass('btn-primary'), so the
                                   second lookup — and the checkbox `change`
                                   handler, which makes the same call — resolves
                                   to an empty jQuery set. The red "Cancel Order"
                                   button never disables, and the second-
                                   confirmation gate over a destructive cancel +
                                   refund is only decorative. Identical in shape
                                   to the Void dialog's bug; higher stakes, because
                                   this one moves money.

Structural, not behavioural — QA re-verifies the real gate in a live page. What
this buys is that nobody silently un-fixes it.
"""
from __future__ import annotations

from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.front_end_invariants import block, code_only, read


def _pending_js() -> str:
	return read("ffl_core", "ffl_core", "page", "pending_dispositions", "pending_dispositions.js")


class PendingDispositionsPageInvariants(FrappeTestCase):
	# ----------------------------------------------------------------- cancel dialog

	def test_the_cancel_order_dialog_never_looks_up_its_primary_button_twice(self):
		"""frappe.ui.Dialog.get_primary_btn() is standard_actions.find('.btn-primary')
		(frappe/public/js/frappe/ui/dialog.js:195). _confirmCancel restyles that
		button as btn-danger — it must, the action cancels an order AND records a
		refund — so a second lookup returns an empty jQuery set, and every
		.prop('disabled', …) on it (the initial gate AND the checkbox `change`
		handler) silently does nothing to nothing. The red button sits enabled and
		the second-confirmation gate is only decorative. Cache the handle once,
		before the swap, and drive it through that handle."""
		body = code_only(block(_pending_js(), "\t_confirmCancel(row) {", "\t"))
		self.assertEqual(
			body.count("get_primary_btn()"),
			1,
			msg=(
				"cache the button handle once; a second get_primary_btn() after "
				"removeClass('btn-primary') resolves to nothing"
			),
		)
		self.assertLess(
			body.index("get_primary_btn()"),
			body.index("removeClass('btn-primary')"),
			msg="take the handle before removing the class that finds it",
		)
		self.assertIn(
			"$primary.prop('disabled', true);",
			body,
			msg="the primary action stays disabled until the confirm box is ticked",
		)

	def test_the_cancel_order_call_is_gated_on_the_confirm_box(self):
		"""The disabled button is the affordance; the real block is the guard inside
		primary_action. Cancelling the order and booking the refund must sit behind
		`if (!confirm) return`, and that guard must come before the frappe.call — so
		no amount of button fiddling fires the destructive path unconfirmed. This is
		the invariant the visual fix decorates, not a substitute for it."""
		body = code_only(block(_pending_js(), "\t_confirmCancel(row) {", "\t"))
		self.assertLess(
			body.index("if (!d.get_value('confirm')) return;"),
			body.index("ffl_core.api.manual_order.cancel_order"),
			msg="guard on the confirm box before the cancel + refund call",
		)

	def test_the_dispose_dialog_keeps_its_primary_class_and_may_look_it_up(self):
		"""The counter-example, so the rule above reads as a rule and not a
		superstition: _confirmDispose gates on the same kind of checkbox but never
		restyles its button, so its repeated get_primary_btn() lookups are correct
		and must not be 'fixed' to match _confirmCancel."""
		body = code_only(block(_pending_js(), "\t_confirmDispose(row) {", "\t"))
		self.assertNotIn("removeClass('btn-primary')", body)
		self.assertGreater(body.count("get_primary_btn()"), 1)

	# --------------------------------------------------- action column width

	def test_the_action_column_width_is_fixed_and_scoped_to_this_page(self):
		"""The card is `grid-template-columns: 1fr auto`, so the action column
		sizes to its content and the one card carrying the long "Mark Shipped (no
		ShipStation)" button renders wider than its neighbours. Pin a fixed
		`width` so every card's button column is identical.

		Load-bearing: the width MUST be scoped to `.ffl-pending-orders` (this page
		only), NOT the generic `.ffl-pending` root class. A desk page's <style> stays
		in the document after SPA navigation, so a bare `.ffl-pending .actions` width
		would also apply to any other page or extension that reuses that class name.
		Guard both the fixed width and its page-scoping against a silent regression."""
		src = _pending_js()
		# The width is scoped to this page's own class...
		self.assertRegex(src, r"\.ffl-pending-orders \.actions \{[^}]*\bwidth:\s*230px")
		# ...and this page's root actually carries that class.
		self.assertIn('class="ffl-pending ffl-pending-orders"', src)
		# The generic root class must NOT carry a bare width (that would leak).
		self.assertNotRegex(src, r"\.ffl-pending \.actions \{[^}]*[^-]\bwidth:")

	# --------------------------------------------- disposed serial fallback

	def test_the_card_falls_back_to_disposed_serials_when_pending_is_empty(self):
		"""`pending_serials` empties once every gun on the order is disposed, so a
		fully-disposed order used to render NO serials at all — the card went blank
		where its neighbours show pills. Fall back to the (greyed) disposed serials
		so a disposed order still shows its guns at a glance; they're on the bound
		book, not gone. Pin the fallback so it isn't silently dropped, and pin its
		greyed style to the page-scoped class (a bare .ffl-pending rule outlives this
		page after SPA navigation)."""
		src = _pending_js()
		# The card render falls back to disposed_serials when pending is empty.
		self.assertIn("row.disposed_serials", src)
		self.assertRegex(src, r'class="pill disposed"')
		# The greyed style is page-scoped, not on the generic root class.
		self.assertRegex(src, r"\.ffl-pending-orders \.serials \.pill\.disposed")
		self.assertNotRegex(src, r"\.ffl-pending \.serials \.pill\.disposed")


class CrVerificationDialogInvariants(FrappeTestCase):
	"""Source-level guard over the C&R (Type 03) verification block in _confirmDispose.

	Why this block specifically. On the C&R channel there is no receiving dealer
	and no 4473 — the clerk ticking "I have compared these details against the
	collector's C&R license" IS the entire paper trail, and what they compare
	against is the values this dialog PRE-FILLED. Get one field's mapping wrong
	and a human attests to an address they never saw: the server then receives a
	payload that is perfectly well-formed and whose state/ZIP agree with the
	order, accepts it, and writes it through ATF FFL Record → FastBound contact →
	the A&D bound book. Every server-side rule still passes. Nothing downstream
	can catch it, because nothing downstream knows what the clerk was shown.

	The server cannot guard this, and this repo ships no JS runner — so pin the
	shape of the mapping by reading the shipped source, exactly as the cancel-dialog test above
	does. Structural, not behavioural: QA re-verifies the live dialog.
	What this buys is that nobody silently un-fixes it.
	"""

	def _dispose_block(self) -> str:
		return code_only(block(_pending_js(), "\t_confirmDispose(row) {", "\t"))

	def test_cr_details_are_attached_before_the_dispose_call_is_made(self):
		"""`args.cr_details` must be set BEFORE frappe.call, or the RPC goes out
		without the verification and the server rejects a dispose the clerk just
		completed — or worse, on a non-C&R order, silently succeeds having sent
		nothing."""
		body = self._dispose_block()
		self.assertIn("if (isCr) args.cr_details", body)
		self.assertLess(
			body.index("if (isCr) args.cr_details"),
			body.index("frappe.call("),
			msg="cr_details must be attached to args before frappe.call is invoked",
		)

	def test_the_cr_branch_is_an_identity_check_not_a_truthiness_check(self):
		"""`row.is_cr === true`, never `row.is_cr`. list_pending_web_orders emits
		null for a non-C&R row, but any future truthy value (a string, a 1, a
		badge object) would silently switch ordinary dealer orders onto the
		collector path and demand a verification they have no fields for."""
		body = self._dispose_block()
		self.assertIn("row.is_cr === true", body)

	def test_every_required_gate_key_matches_the_server_contract(self):
		"""The dialog's required list and the server's CR_REQUIRED_FIELDS are the
		same seven keys. If they drift, the button enables on a payload the
		server will reject (dead end for the clerk) or blocks one it would
		accept."""
		from ffl_woo_sync.woocommerce.cr_transfer import CR_REQUIRED_FIELDS

		gate = code_only(block(_pending_js(), "\t_crGateOk(d) {", "\t"))
		server_keys = {k for k, _label in CR_REQUIRED_FIELDS}
		self.assertEqual(len(server_keys), 7)
		for key in server_keys:
			with self.subTest(key=key):
				self.assertIn(f"'{key}'", gate,
					msg=f"{key} is required server-side but not gated in the dialog")
		# Address 2 is optional on both sides — gating it would block a lawful
		# dispose for a collector whose licence has no second address line.
		self.assertNotIn("'premise_street2'", gate)

	def test_every_prefill_key_is_actually_read_into_a_field(self):
		"""The load-bearing one: each cr_prefill key the server sends must be read
		by _crFields as `p.<key>`. A key that is sent but never read leaves its
		field blank (the clerk retypes from the licence — safe), but a field
		reading the WRONG key shows one value while labelled as another, and the
		clerk attests to it."""
		from ffl_woo_sync.woocommerce.cr_transfer import CR_DETAIL_KEYS

		fields = code_only(block(_pending_js(), "\t_crFields(row, ctx) {", "\t"))
		self.assertEqual(len(CR_DETAIL_KEYS), 8)
		for key in CR_DETAIL_KEYS:
			with self.subTest(key=key):
				self.assertIn(f"p.{key}", fields,
					msg=f"cr_prefill['{key}'] is sent by the server but never read")

	def test_each_prefill_key_feeds_the_field_of_the_same_name(self):
		"""Guards the mis-wiring the previous test cannot see: `p.premise_city`
		must land on the city field, not on the state field. The dialog names its
		fields cr_<key>, so the pairing is checkable in source."""
		from ffl_woo_sync.woocommerce.cr_transfer import CR_DETAIL_KEYS

		fields = code_only(block(_pending_js(), "\t_crFields(row, ctx) {", "\t"))
		for key in CR_DETAIL_KEYS:
			with self.subTest(key=key):
				fieldname = f"cr_{key}"
				start = fields.index(f"fieldname: '{fieldname}'")
				# The default: for this field must read the matching prefill key,
				# within the same field definition (they are one per line group).
				segment = fields[start:start + 400]
				self.assertIn(f"p.{key}", segment,
					msg=f"{fieldname} does not default from cr_prefill['{key}']")


class PickupHasNoShipButton(FrappeTestCase):
	"""A counter order taken without a shipping address is a pickup: it leaves over
	the counter, so there is no label to buy. The Ship button used to render
	DISABLED on those rows — a dead control the clerk had to learn to ignore — and
	the only real action (Mark Picked Up) sat below it. Pin that `canShip` itself
	excludes a no-address row rather than the disabled flag doing it, or the dead
	button comes back the next time someone tidies the flags."""

	def _card(self) -> str:
		return code_only(block(_pending_js(), "\t_renderCard(", "\t"))

	def test_no_address_hides_ship_instead_of_disabling_it(self):
		card = self._card()
		ship_decl = card[card.index("const canShip"):card.index("const shipDisabled")]
		self.assertIn("!noAddress", ship_decl,
			msg="a pickup must not offer Ship at all")
		disabled = card[card.index("const shipDisabled"):]
		disabled = disabled[:disabled.index("\n")]
		self.assertNotIn("noAddress", disabled,
			msg="noAddress decides whether Ship EXISTS, not whether it is greyed")

	def test_the_pickup_row_still_has_its_handover_button(self):
		card = self._card()
		self.assertIn("noAddress ? __('Mark Picked Up')", card)


class ShipDialogMarkShippedInvariants(FrappeTestCase):
	"""Source-level guard over the Ship dialog's second exit — "Mark Shipped
	(no ShipStation)".

	Why this one is worth pinning. The queue has exactly two ways to clear a
	counter order, and they book different things: the ShipStation push buys a
	label, Mark Shipped issues the deferred Delivery Note leg (stock + COGS) and
	stamps the row shipped with no label at all. Offering the second one inside
	the first one's dialog puts both on the same surface — so the gate and the
	call behind that button must be THE card's gate and THE card's call, not a
	second copy that drifts a release later into clearing an order the card
	would have refused.

	The dangerous drift is specific: `row.can_mark_shipped` IS the server's
	mark_shipped_block, so a hand-rolled gate here (or a plain truthiness check)
	would promise a clerk something the server refuses — today that means firearm
	lines whose serials resolve to nothing at all, a data fault the button must
	not paper over. (Undisposed firearms deliberately no longer refuse: the mark
	clears the shipping half and leaves the row queued for its Dispose.)

	Structural, not behavioural — QA re-verifies the live dialog. What this buys
	is that nobody silently un-fixes it.
	"""

	def _ship_dialog(self) -> str:
		return code_only(block(_pending_js(), "\t_confirmShip(row) {", "\t"))

	def test_the_ship_dialog_offers_the_no_shipstation_path(self):
		"""The whole point: a one-button dialog left the clerk who opened Ship
		to hand a package over outside ShipStation with no way forward but to
		close it again."""
		body = self._ship_dialog()
		self.assertIn("secondary_action_label", body)
		self.assertIn("secondary_action:", body)

	def test_the_secondary_action_reuses_the_shared_mark_shipped_call(self):
		"""One frappe.call for both entry points. A second call site here is how
		the card button and the dialog button drift — different args, different
		success handling, and only one of them reloading the queue."""
		body = self._ship_dialog()
		self.assertIn("this._doMarkShipped(row", body)
		self.assertNotIn("mark_shipped_manually", body)

	def test_the_mark_shipped_gate_is_declared_once_for_both_entry_points(self):
		"""`row.can_mark_shipped === true` — the identity check against the
		server's own refusal rule — must exist in exactly ONE place (the
		_canMarkShipped helper), which both the card and this dialog call."""
		src = _pending_js()
		self.assertEqual(code_only(src).count("row.can_mark_shipped === true"), 1)
		self.assertIn("this._canMarkShipped(row)", self._ship_dialog())
		card = code_only(block(src, "\t_renderCard(row, $list) {", "\t"))
		self.assertIn("this._canMarkShipped(row)", card)

	def test_the_card_drops_mark_shipped_when_ship_can_be_opened(self):
		"""Two buttons for one action on one card read as two different actions.
		A usable Ship button already carries Mark Shipped as its dialog's secondary
		action, so the card keeps its own only when it can be pressed and Ship
		cannot (ShipStation off, already pushed, counter pickup, goods not in). An
		unpaid order can press neither, and gets the reason instead."""
		card = code_only(block(_pending_js(), "\t_renderCard(row, $list) {", "\t"))
		self.assertRegex(card, r"canMarkShipped\s*=[^;]*!\(canShip && !shipDisabled\)")
		self.assertRegex(card, r"canMarkShipped\s*=[^;]*!blockShipUnpaid")
		button = next(l for l in card.splitlines() if "btn-mark-shipped" in l)
		self.assertNotIn("disabled", button,
			"a disabled Mark Shipped beside a disabled Ship is the pair this rule removes")

	def test_the_ship_dialog_applies_the_payment_rule_too(self):
		"""mark_shipped_manually is refused by ship_payment_block exactly as
		push_shipment is, so the dialog's secondary action must not be live on an
		unpaid order — one predicate, asked by both surfaces."""
		src = code_only(_pending_js())
		self.assertEqual(src.count("_blockShipUnpaid(row) {"), 1)
		dialog = code_only(self._ship_dialog())
		self.assertIn("this._blockShipUnpaid(row)", dialog)

	def test_the_card_explains_itself_even_with_no_button_to_hang_it_on(self):
		"""The hint used to render only next to Dispose / a disabled Ship / a disabled
		Mark Shipped. A counter pickup whose ship-to-store goods have not arrived has
		none of the three — and that is exactly the row that needs the sentence."""
		card = code_only(block(_pending_js(), "\t_renderCard(row, $list) {", "\t"))
		self.assertIn("${disposeHint ? `<div class=\"meta\">${disposeHint}</div>` : ''}", card)

	def test_a_source_that_never_marks_shipped_gets_no_button_at_all(self):
		"""CAPABILITIES.gunbroker.markShipped is false — a GunBroker sale reports
		its own tracking once the label is bought, so the action does not exist
		for it. Absent, not disabled: a disabled button reads as "not yet", and
		this one is "never"."""
		body = self._ship_dialog()
		self.assertIn("this._can(row).markShipped", body)
		self.assertRegex(body, r"\.\.\.\(offerMarkShipped \?")

	def test_the_button_is_dead_not_missing_when_the_server_would_refuse(self):
		"""The other half of the pair: on a counter order the server would still
		refuse (firearm lines with no readable serial state) the action is
		temporarily impossible, so render it disabled — with the reason in the
		summary — rather than hide it. Hiding it is what made this dialog look
		like it had lost the option."""
		body = self._ship_dialog()
		self.assertRegex(body, r"get_secondary_btn\(\)\s*\.prop\('disabled', true\)")
		self.assertLess(
			body.index("d.show()"),
			body.index("get_secondary_btn()"),
			msg="the footer button only exists once the dialog is shown",
		)


class ScanReceiveQueueActions(FrappeTestCase):
	"""The queue offers Fulfill from stock on an unarrived
	ship-to-store line; a gun's serial comes from the unheld in-stock list."""

	def test_awaiting_rows_offer_fulfill_from_stock(self):
		src = code_only(_pending_js())
		self.assertIn("btn-fulfill-stock", src)
		body = block(src, "_fulfillFromStock(row, sub, confirmed) {", "\t")
		self.assertIn("ffl_core.api.manual_order.fulfill_from_stock", body)
		self.assertIn("ffl_core.firearm.available_serials_for_items", body)

	def test_the_button_is_gated_on_what_the_shelf_holds(self):
		"""The waiting products collapse behind one button
		and it appears only when local stock can serve a line — the gate reads the
		server's ``available`` count, which fulfill_from_stock re-checks."""
		src = code_only(_pending_js())
		gate = block(src, "_servableFromStock(subs) {", "\t")
		self.assertIn("s.available", gate)
		self.assertIn("s.is_firearm ? 1 : s.qty", gate)
		self.assertIn("this._servableFromStock(subs)", src)
		self.assertIn("Fulfill from local stock", src)

	def test_trouble_legs_offer_resource_and_cancel_with_a_distributor_warning(self):
		src = code_only(_pending_js())
		for marker in ("btn-resource", "btn-cancel-line"):
			self.assertIn(marker, src)
		warn = block(src, "_distributorWarning(sub, troubleLeg, action) {", "\t")
		self.assertIn("web_ref", warn)
		res = block(src, "_resourceLine(row, sub, troubleLeg) {", "\t")
		self.assertIn("_distributorWarning(", res)
		self.assertIn("ffl_core.api.manual_order.resource_line", res)
		cancel = block(src, "_cancelLine(row, sub, troubleLeg) {", "\t")
		self.assertIn("frappe.confirm", cancel)
		self.assertIn("_distributorWarning(", cancel)


class WebOrderDistributorLegs(FrappeTestCase):
	"""A web order's card shows its distributor orders like a counter card does,
	each with the house's web reference (WOO-ORD-02418 / RSR 3203100, 2026-10-10:
	the order was placed but the card never said so)."""

	def test_web_rows_read_their_distributor_orders(self):
		src = code_only(_pending_js())
		self.assertIn("isWeb ? (row.distributor_orders || [])", src)

	def test_each_distributor_pill_names_the_web_reference(self):
		src = code_only(_pending_js())
		pill = src[src.index('class="pill leg-do'):]
		pill = pill[:pill.index("</span>")]
		self.assertIn("d.web_ref", pill)

	def test_a_distributor_serial_is_not_asked_of_woocommerce(self):
		# Checked before the "correct it on the WooCommerce order" hint, which an
		# unresolved to_store antique's serial_errors would otherwise trigger.
		# The arrived hint is only for a waiting to_store unit — not an fds unit on
		# an order whose other, to_store leg arrived.
		src = code_only(_pending_js())
		self.assertIn("!waitLanes.includes('local')", src)
		self.assertIn("(row.arrived && waitLanes.includes('to_store'))", src)
		self.assertLess(src.index("serialFromDistributor ? ("),
			src.index("serialErrors.length ? __('Serial rejected"))


class QueueSections(FrappeTestCase):
	"""Rows render in stage sections, source chips and a
	search box filter them, overdue rows are marked, GunBroker's own stage is
	read from channel_stage now that `stage` is the page-wide section."""

	def test_rows_render_in_stage_sections(self):
		src = code_only(_pending_js())
		self.assertIn("function pdSections(rows, filter, query)", src)
		render = block(src, "_render(rows) {", "\t")
		self.assertIn("pdSections(", render)
		self.assertIn("pd-section", render)
		self.assertIn("this._renderCard(row, $list)", render)

	def test_source_filter_chips_and_search_exist(self):
		src = _pending_js()
		self.assertIn("['all', 'counter', 'woo', 'gunbroker']", src)
		# The retired second web store's chip and badge are gone (1.11.0).
		self.assertNotIn("badge dealer", src)
		self.assertNotIn("row.site === 'dealer'", src)
		self.assertIn('data-filter="${f}"', src)
		self.assertIn("pd-chip", src)
		self.assertIn("pd-search", src)

	def test_overdue_rows_are_marked_and_the_primary_button_follows_the_stage(self):
		src = code_only(_pending_js())
		card = block(src, "_renderCard(row, $list) {", "\t")
		self.assertIn("age_days", card)
		self.assertIn("overdue", card)
		self.assertIn("this._markPrimary($card, row)", card)
		self.assertIn("row.channel_stage === 'awaiting_payment'", card)


class PendingCardServerGates(FrappeTestCase):
	"""Card buttons follow the server's own refusals, so staff never get a live
	button that only earns an error, and the Banned badge reaches counter rows."""

	def test_banned_check_reads_counter_rows_through_the_kind_table(self):
		"""Counter rows carry source 'sales_invoice'; a literal 'counter' test
		never matched one, so no counter order ever showed the Banned badge."""
		body = code_only(block(_pending_js(), "\t_markBanned(rows) {", "\t"))
		self.assertNotIn("=== 'counter')", body.replace("(KIND[r.source] || 'counter') === 'counter'", "")
			.replace("(KIND[row.source] || 'counter') === 'counter'", ""))
		self.assertIn("(KIND[r.source] || 'counter') === 'counter'", body)
		self.assertIn("(KIND[row.source] || 'counter') === 'counter'", body)

	def test_gunbroker_dispose_waits_for_payment(self):
		"""dispose_gb_order refuses an order with no payment recorded."""
		card = code_only(block(_pending_js(), "\t_renderCard(row, $list) {", "\t"))
		self.assertRegex(card,
			r"disposeDisabled\s*=[^;]*\(isGb && row\.channel_stage === 'awaiting_payment'\)")

	def test_the_payment_rule_does_not_exempt_a_pickup(self):
		"""ship_payment_block refuses every unpaid individual, pickup or not, and
		Mark Picked Up clears through it — so the page's copy must not exempt a
		row without a ship address."""
		rule = code_only(block(_pending_js(), "\t_blockShipUnpaid(row) {", "\t"))
		self.assertIn("return kind === 'counter' && !row.is_business && !row.paid;", rule)
		self.assertNotIn("has_ship_address", rule)
		self.assertNotIn("destination_ffl", rule)
