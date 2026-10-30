"""Every gate that asks "is this gun spoken for?" asks the one predicate.

Before a second sales channel existed there was one flag, `woo_reserved`, and
fifteen places tested it by hand. Adding a channel that way means auditing all
fifteen and getting some of them right — and each one missed is a gun that can
be sold twice, which on a marketplace cannot be undone from the seller's side.

So the rule is: `channel_reservation` (or its set form, or `serial_shelf_state`
which wraps it) is the ONLY thing that answers that question, and this file is
what stops the next person adding a sixteenth site that reads a field name
directly.

Asserted against the SOURCE rather than by exercising each gate. Behaviour
tests for these live with the gates themselves and need a bench with real
serials; this one runs anywhere and answers a different question — "did anybody
add a read point without wiring it up?" — which is the one that actually gets
answered wrong. A gate that consults the predicate and then ignores the answer
is a different bug, and the behaviour tests are what catch that.

Deliberately NOT a blanket ban on the string `woo_reserved`. WooCommerce still
sets and clears its own flag, and must: the predicate answers WHO HOLDS a gun,
while releasing one stays with whoever took it. An implementation that
helpfully cleared another channel's reservation would be the regression this
split exists to prevent.
"""
from __future__ import annotations

import inspect
import re
import unittest

# Two questions, and telling them apart is the whole point of this table.
#
# ANY     "is this gun spoken for at all?" — a gate deciding whether the gun may
#         be SOLD or picked. The holder's identity does not matter; ours counts.
# OTHERS  "is somebody who is NOT us holding it?" — a decision this app makes
#         about its OWN storefront. Our own pending web order is mid-fulfilment
#         (stock 0, page still published, SOLD archive waiting at the end); a
#         marketplace sale has left the shelf and its page must go to draft.
#
# Asking ANY where OTHERS belongs is the quiet one: every gun on a pending web
# order gets drafted AND stamped `{site}_delisted=1`, after which
# `route_serial_exit` answers EXIT_SKIP at fulfilment and the SOLD archive page
# is gone with nothing left to notice.
ANY = "any"
OTHERS = "others"

# (import path, function name, the predicate it must consult, which question,
#  why it matters)
READ_POINTS = (
	("ffl_core.api.manual_order", "_serial_status", "serial_shelf_state", ANY,
		"scan-to-add at the counter"),
	("ffl_core.api.manual_order", "_serial_status", "channel_claim_block_reason", ANY,
		"scan-to-add: units any channel has sold and not yet matched to a gun"),
	("ffl_core.compliance.web_hold", "refuse_claimed_firearm_units",
		"channel_claim_block_reason", ANY,
		"the same refusal where every counter ticket passes (POS Invoice submit)"),
	("ffl_core.firearm", "_pos_picker_excluded_serials", "channel_reserved_serials",
		ANY, "all three POS serial pickers"),
	("ffl_woo_sync.woocommerce.webhook", "_serial_availability",
		"channel_reservation", ANY,
		"a web order naming one specific gun — the double-sale main line"),
	("ffl_woo_sync.woocommerce.webhook", "_pick_oldest_unreserved_serial",
		"channel_reserved_serials", ANY,
		"the placeholder picker for a web order that claims a unit"),
	("ffl_woo_sync.woocommerce.webhook", "_evict_blocking_hold",
		"channel_reservations", OTHERS,
		"eviction moves a placeholder WITHIN Woo, so it must refuse only "
		"another channel's paid sale — and see EVERY holder, not the first"),
	("ffl_woo_sync.woocommerce.webhook", "_suggest_instock_serial",
		"channel_reserved_serials", ANY,
		"a 'did you mean…?' must never point at a gun somebody bought"),
	("ffl_woo_sync.woocommerce.webhook", "_apply_dealer_serial",
		"channel_reservation", ANY, "a staff-typed serial on a web order"),
	("ffl_core.reservations", "uniform_free_serials", "channel_reserved_serials",
		ANY, "the shared pool every channel's quantity is derived from — the "
		"storefront count, and the web store's `free_serials` delegates to it"),
	("ffl_woo_sync.woocommerce.mapping", "serial_to_product_payload",
		"other_channel_reservation", OTHERS,
		"a per-serial product's status: draft for another channel's gun, "
		"published-at-stock-0 for one of our own web orders"),
	("ffl_woo_sync.woocommerce.tasks", "sync_serial_listing_task",
		"serial_shelf_state", ANY,
		"the per-serial listing reconcile"),
	("ffl_woo_sync.woocommerce.tasks", "_run_reconcile", "channel_reservation",
		ANY,
		"reconcile's POS ground truth — how many are for sale is 0 for either "
		"kind of hold; the self-heal runs backwards without it"),
	("ffl_woo_sync.woocommerce.tasks", "_drift_repair",
		"other_channel_reservation", OTHERS,
		"WHICH repair a drifted product gets — taken down, or pushed as it "
		"always was"),
	("ffl_woo_sync.woocommerce.tasks", "_delist_or_filters",
		"CHANNEL_RESERVE_FIELDS", OTHERS,
		"Pass 3b's net: a second chance for a delist that failed, and NOT a "
		"trawl through everything being packed"),
	("ffl_woo_sync.woocommerce.sync", "push_serial", "other_channel_reservation",
		OTHERS, "the fifth push guard, which `_relist` may not lift"),
)

# "asks for it by this exact name" — `channel_reservation` must not be satisfied
# by `other_channel_reservation` or by `channel_reservations`. Those are three
# different questions, and a substring test answers all three with the first.
def _asks_for(name: str, source: str) -> bool:
	return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
		source) is not None


class _AppMissing(Exception):
	"""The app itself is not on this bench — a skip, not a finding."""


def _source(module_path: str, func_name: str) -> str:
	"""The source of one read point, or _AppMissing when its app is absent.

	The distinction matters: a bench without ffl_woo_sync legitimately cannot
	check the web-store half, while a module path that no longer imports on a
	bench that HAS the app is a finding — most likely a rename that left this
	list pointing at nothing, which would turn the whole file green and blind.
	"""
	import importlib
	import importlib.util

	app = module_path.split(".", 1)[0]
	if importlib.util.find_spec(app) is None:
		raise _AppMissing(app)
	try:
		module = importlib.import_module(module_path)
	except ImportError as exc:
		raise _AppMissing(f"{module_path} ({exc})") from exc
	func = getattr(module, func_name, None)
	if func is None:
		raise AssertionError(
			f"{module_path}.{func_name} no longer exists — if it was renamed, "
			f"update this list; if it was deleted, make sure whatever replaced it "
			f"is in here.")
	return inspect.getsource(func)


class EveryChannelReadPointUsesThePredicate(unittest.TestCase):
	def test_each_read_point_consults_it(self):
		"""Counts what it actually checked and fails if that is nothing.

		Without the count, an environment where every import happened to fail
		would report a green test that asserted precisely nothing — which is the
		worst possible outcome for a file whose entire job is to notice an
		omission.
		"""
		checked = 0
		skipped: list[str] = []
		for module_path, func_name, predicate, _scope, why in READ_POINTS:
			try:
				source = _source(module_path, func_name)
			except _AppMissing as missing:
				skipped.append(str(missing))
				continue
			checked += 1
			self.assertTrue(_asks_for(predicate, source), "\n".join([
				f"{module_path}.{func_name} decides availability for {why},",
				f"but does not call {predicate}().",
				"Testing a channel flag by name here means the NEXT channel is",
				"invisible to this gate, and an invisible gate is a gun sold",
				"twice. Ask ffl_core.reservations instead.",
			]))
		self.assertGreaterEqual(len(READ_POINTS), 12)
		self.assertGreater(checked, 0,
			"no read point could be inspected, so nothing was checked: "
			+ "; ".join(skipped))

	def test_the_push_guard_is_not_liftable_by_relist(self):
		"""Ranked with custody, not with the delist sentinel.

		`_relist` lets a human answer "may a machine republish this?". It cannot
		answer "has somebody already bought it?", and one press of the form's
		Send button passes `_relist=True` — so a guard placed below that flag
		would put a sold gun back on the storefront at stock 1.
		"""
		try:
			source = _source("ffl_woo_sync.woocommerce.sync", "push_serial")
		except _AppMissing as missing:
			self.skipTest(str(missing))
		guard = re.search(r"(?<![A-Za-z0-9_])other_channel_reservation\(", source)
		self.assertIsNotNone(guard, "the guard no longer asks the predicate")
		guard = guard.start()
		relist_gate = source.index("if not _relist")
		self.assertLess(guard, relist_gate,
			"the channel guard must run BEFORE anything _relist can lift")


	def test_a_read_point_asks_about_the_right_holders(self):
		"""ANY vs OTHERS, enforced rather than left to the table's prose.

		The two predicates differ by one word and a prefix, and the gap between
		them is a whole class of bug in both directions: ask OTHERS at a SALE
		gate and the same gun goes out twice; ask ANY on our own storefront and
		every gun being packed is drafted and loses its SOLD archive page.
		"""
		checked = 0
		for module_path, func_name, _predicate, scope, why in READ_POINTS:
			try:
				source = _source(module_path, func_name)
			except _AppMissing:
				continue
			checked += 1
			excludes_us = (_asks_for("other_channel_reservation", source)
				or _asks_for("CHANNEL_WOO", source))
			if scope == OTHERS:
				self.assertTrue(excludes_us, "\n".join([
					f"{module_path}.{func_name} ({why}) is a decision about our",
					"OWN storefront, so it must exclude our own hold —",
					"other_channel_reservation(own=CHANNEL_WOO), or CHANNEL_WOO",
					"in whatever shape the read point takes.",
				]))
			else:
				self.assertFalse(excludes_us, "\n".join([
					f"{module_path}.{func_name} ({why}) decides whether a gun",
					"may be SOLD or picked. Excluding our own hold there hands",
					"out a gun a web order has already paid for.",
				]))
		self.assertGreater(checked, 0)


class ClearingAReservationStaysWithItsOwner(unittest.TestCase):
	"""The other half of the split, which is easy to lose while tidying.

	The predicate answers who holds a gun. Releasing one belongs to whoever took
	it — WooCommerce clears `woo_reserved`, GunBroker clears `gb_reserved`, and
	neither touches the other's. An implementation that "helpfully" cleared both
	would free a gun that is still sold somewhere.
	"""

	def test_the_web_release_path_writes_only_woo_fields(self):
		try:
			source = _source("ffl_woo_sync.woocommerce.webhook", "_release_serial")
		except _AppMissing as missing:
			self.skipTest(str(missing))
		self.assertIn("woo_reserved", source)
		self.assertNotIn("gb_reserved", source)
