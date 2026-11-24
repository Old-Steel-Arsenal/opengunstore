"""RSR — ``distributor_listing_push``: the inverted hook, registered HERE.

The listing pipeline lives in ffl_integrations, but the code that
talks to WooCommerce lives in ffl_woo_sync — so this hook runs the other way
round from the rest of the table.  ``ffl_integrations`` **calls**
``fn(item_code: str, payload: dict) -> None``; ``ffl_woo_sync`` registers the
implementation in its own ``hooks.py``.  Neither app imports the other.

What the implementation must do:

* enqueue the app's OWN push wrapper (``push_item`` / ``push_stock`` tasks) —
  never push inline, so a 500-SKU listing batch does not block on HTTP;
* honour ``_skip_push()`` (inbound-webhook echo, install / migrate / import);
* thread the shipping-class slug through to the push;
* fail loudly on a malformed call (the hook "may raise") — a listing run
  that silently pushed nothing would look like success.

The payload's ``stock`` / ``price`` copies are advisory: the push reads the Item
(POS is the SSOT) after the caller's transaction commits.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import CallbackManager

from ffl_woo_sync.woocommerce.distributor import (
    DROPSHIP_SHIPPING_CLASSES,
    distributor_listing_push,
    shipping_class_name,
)

_IMPL = "ffl_woo_sync.woocommerce.distributor.distributor_listing_push"
_FULL_TASK = "ffl_woo_sync.woocommerce.tasks.distributor_listing_push_task"
_STOCK_TASK = "ffl_woo_sync.woocommerce.tasks.push_stock_batch_task"


_DELIST_TASK = "ffl_woo_sync.woocommerce.tasks.delist_item_task"


def _isolate_stock_state(case: FrappeTestCase) -> None:
    """Fresh commit / rollback callback queues and no accumulated stock state, so
    a test drives "the transaction commits" by ``frappe.db.after_commit.run()``
    (the test transaction never really commits) without firing callbacks other
    fixtures registered. Frappe's ``db.rollback()`` does ``after_commit.reset()``
    then ``after_rollback.run()`` — the tests spell those two calls out."""
    for attr in ("after_commit", "after_rollback"):
        patcher = patch.object(frappe.db, attr, CallbackManager())
        patcher.start()
        case.addCleanup(patcher.stop)
    frappe.flags.pop("woo_stock_refresh", None)
    case.addCleanup(frappe.flags.pop, "woo_stock_refresh", None)


def _commit() -> None:
    frappe.db.after_commit.run()


def _rollback() -> None:
    frappe.db.after_commit.reset()
    frappe.db.after_rollback.run()


def _payload(**kwargs) -> dict:
    base = {
        "stock": 7,
        "price": 349.99,
        "product_brand": "Holosun",
        "_osa_restricted_states": "CA,NY",
        "shipping_class": "dropship-accessory",
    }
    base.update(kwargs)
    return base


def _spec17_payload(**kwargs) -> dict:
    """Every key the payload contract froze, as ffl_integrations actually sends it."""
    base = {
        "sku": "RSR-ACC-1",
        "item_code": "RSR-ACC-1",
        "price": 349.99,
        "stock": 7,
        "shipping_class": "dropship-accessory",
        "fulfillment_mode": "Direct",
        "listing_status": "Active",
        "is_firearm": False,
        "meta": {"_osa_restricted_states": ""},
        "restricted_states": [],
        "product_brand": "Holosun",
    }
    base.update(kwargs)
    return base


# ===========================================================================
# Registration (the inversion itself)
# ===========================================================================

class TestHookRegistration(FrappeTestCase):
    def test_hooks_py_registers_the_implementation(self):
        from ffl_woo_sync import hooks as woo_hooks

        self.assertIn(_IMPL, woo_hooks.distributor_listing_push)

    def test_registered_path_resolves_to_the_callable(self):
        """``frappe.get_attr(fn)`` is how the caller invokes it."""
        self.assertIs(frappe.get_attr(_IMPL), distributor_listing_push)

    def test_no_ffl_integrations_import_anywhere_in_the_app(self):
        """The whole point of the inversion — and G2 accepts it for the WHOLE
        app, not just this module, so the scan is app-wide (naming the other app
        in a comment or docstring is fine)."""
        import re
        from pathlib import Path

        import ffl_woo_sync

        app_root = Path(ffl_woo_sync.__file__).parent
        offenders = [
            str(py.relative_to(app_root))
            for py in app_root.rglob("*.py")
            # Shipped modules only.  The suite itself is allowed to import the
            # other app — test_caliber_fb_isolation.py reads ffl_integrations'
            # source precisely to assert an isolation, which is the same rule
            # being enforced here, not a violation of it.
            if "tests" not in py.parts
            and re.search(r"^\s*(from|import)\s+ffl_integrations", py.read_text(), re.M)
        ]
        self.assertEqual(offenders, [])


# ===========================================================================
# Enqueue behaviour
# ===========================================================================

class TestDistributorListingPush(FrappeTestCase):
    def setUp(self):
        super().setUp()
        _isolate_stock_state(self)
        # The stock-only path needs a product to update; the test Item has none.
        patcher = patch("ffl_woo_sync.woocommerce.distributor._site_product_id",
                        return_value="4711")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_zero_and_a_refresh_share_the_one_batch_job(self):
        """The quantity is read when the job runs, so a take-down's zero is not
        different work from a refresh — both are just "this item needs a push"."""
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _payload(mode="stock", stock=0))
            distributor_listing_push("RSR-ACC-2", _payload(mode="stock"))
            _commit()
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.kwargs["item_codes"],
                         ["RSR-ACC-1", "RSR-ACC-2"])

    def test_a_stock_push_before_the_product_exists_is_skipped(self):
        """Nothing to update yet: the full push carries the quantity, and a
        queued stock job would only fail and be retried."""
        with patch("ffl_woo_sync.woocommerce.distributor._site_product_id",
                   return_value=""), \
             patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _payload(mode="stock"))
            _commit()
        mock_enqueue.assert_not_called()

    def test_enqueues_the_full_push_wrapper(self):
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _payload())
        mock_enqueue.assert_called_once_with(
            _FULL_TASK,
            queue="long",
            enqueue_after_commit=True,
            job_id="woo-listing:retail:RSR-ACC-1:full",
            deduplicate=True,
            item_code="RSR-ACC-1",
            site="retail",
            shipping_class="dropship-accessory",
            pos_fields_only=False,
        )

    def test_lean_payload_threads_the_flag_and_gets_its_own_dedup_action(self):
        """The `*/15` drift path marks its payload pos_fields_only. The flag must
        reach the task, and lean/full must dedup SEPARATELY — a queued lean push
        whose args win would otherwise swallow an operator's forced full re-sync."""
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _payload(pos_fields_only=True))
        self.assertIs(mock_enqueue.call_args.kwargs["pos_fields_only"], True)
        self.assertEqual(
            mock_enqueue.call_args.kwargs["job_id"], "woo-listing:retail:RSR-ACC-1:lean")

    def _job_id(self, payload) -> str:
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", payload)
        return mock_enqueue.call_args.kwargs["job_id"]

    def test_repeat_of_the_same_action_deduplicates(self):
        """`sync_listings` ticks every 15 min over hundreds of SKUs; without a
        stable job_id a slow batch stacks a second copy on top of itself
        (ffl_integrations/shipstation/tasks.py:52 is the house pattern)."""
        for payload, task in (
            (_payload(), _FULL_TASK),
            (_payload(delist=True), _DELIST_TASK),
        ):
            with self.subTest(task=task):
                with patch("frappe.enqueue") as mock_enqueue:
                    distributor_listing_push("RSR-ACC-1", payload)
                    distributor_listing_push("RSR-ACC-1", payload)
                first, second = mock_enqueue.call_args_list
                self.assertEqual(first.kwargs["job_id"], second.kwargs["job_id"])
                self.assertTrue(first.kwargs["deduplicate"])

    def test_different_actions_never_share_a_job_id(self):
        """The dedup key carries the ACTION.

        Sharing one key per item means whichever action queues first silently
        eats the other: a `delist` arriving while a push is still queued is
        dropped, and the discontinued product stays on the shop — precisely the
        C2 hole the delist branch exists to close.  The reverse (a restock push
        swallowed by a queued delist) leaves a live SKU dark.
        """
        ids = {
            "full": self._job_id(_payload()),
            "delist": self._job_id(_payload(delist=True)),
        }
        self.assertEqual(len(set(ids.values())), 2, ids)
        for action, job_id in ids.items():
            with self.subTest(action=action):
                self.assertEqual(job_id, f"woo-listing:retail:RSR-ACC-1:{action}")

    def test_a_queued_push_cannot_swallow_a_delist(self):
        """The bug this key shape prevents, stated as the scenario."""
        self.assertNotEqual(self._job_id(_payload()),
                            self._job_id(_payload(delist=True)))

    def test_job_id_is_scoped_per_item_and_site(self):
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _payload())
            distributor_listing_push("RSR-ACC-2", _payload())
        ids = [c.kwargs["job_id"] for c in mock_enqueue.call_args_list]
        self.assertEqual(len(set(ids)), 2, ids)
        self.assertTrue(all(":retail:" in i for i in ids), ids)

    def test_called_positionally_per_the_spec_signature(self):
        """fn(item_code: str, payload: dict) — two positional args."""
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", {})
        self.assertEqual(mock_enqueue.call_args.kwargs["item_code"], "RSR-ACC-1")

    def test_payload_may_be_omitted(self):
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1")
        self.assertEqual(mock_enqueue.call_args.kwargs["shipping_class"], "")

    def test_stock_mode_waits_for_the_commit_then_queues_one_batch_job(self):
        """A 68-item sweep queues ONE job — and only once the transaction that
        changed the state has committed."""
        with patch("frappe.enqueue") as mock_enqueue:
            for n in range(68):
                distributor_listing_push(f"RSR-ACC-{n}", _payload(mode="stock"))
            distributor_listing_push("RSR-ACC-3", _payload(mode="stock"))  # a repeat
            mock_enqueue.assert_not_called()
            # ONE commit callback and ONE rollback callback, however many items.
            self.assertEqual(len(frappe.db.after_commit._functions), 1)
            self.assertEqual(len(frappe.db.after_rollback._functions), 1)
            _commit()
        mock_enqueue.assert_called_once_with(
            _STOCK_TASK, queue="long", site="retail",
            item_codes=[f"RSR-ACC-{n}" for n in range(68)])

    def test_a_rolled_back_trigger_queues_nothing_and_leaves_nothing_behind(self):
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _payload(mode="stock"))
            _rollback()
            _commit()
            # a later transaction in the same process carries only its own items
            distributor_listing_push("RSR-ACC-2", _payload(mode="stock"))
            _commit()
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.kwargs["item_codes"], ["RSR-ACC-2"])

    def test_every_transaction_queues_its_own_batch(self):
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _payload(mode="stock"))
            _commit()
            distributor_listing_push("RSR-ACC-2", _payload(mode="stock"))
            _commit()
        self.assertEqual([c.kwargs["item_codes"] for c in mock_enqueue.call_args_list],
                         [["RSR-ACC-1"], ["RSR-ACC-2"]])

    def test_stock_batches_name_their_site(self):
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _payload(mode="stock"))
            _commit()
        self.assertEqual([c.kwargs["site"] for c in mock_enqueue.call_args_list], ["retail"])

    def test_a_background_job_holds_its_commits_and_queues_once_at_the_end(self):
        """The drop-ship sync commits after EVERY listing: per-commit queueing would
        be one job per listing again. Inside a job the committed items wait for
        the job's end (`frappe.job.after_job`, run after its final commit)."""
        frappe.local.job = frappe._dict(after_job=CallbackManager())
        self.addCleanup(delattr, frappe.local, "job")
        with patch("frappe.enqueue") as mock_enqueue:
            for n in range(3):
                distributor_listing_push(f"RSR-ACC-{n}", _payload(mode="stock"))
                _commit()
            distributor_listing_push("RSR-ACC-9", _payload(mode="stock"))
            _rollback()  # this listing's transaction failed: not pushed
            mock_enqueue.assert_not_called()
            self.assertEqual(len(frappe.job.after_job._functions), 1)
            frappe.job.after_job.run()
        mock_enqueue.assert_called_once_with(
            _STOCK_TASK, queue="long", site="retail",
            item_codes=["RSR-ACC-0", "RSR-ACC-1", "RSR-ACC-2"])

    def test_a_failed_enqueue_is_reported_per_item(self):
        """Redis down at the hand-off: the listing must learn its push never
        happened (it records the error and its own sync retries)."""
        from ffl_woo_sync.woocommerce import tasks
        with patch("frappe.enqueue", side_effect=RuntimeError("redis down")), \
             patch.object(tasks, "report_stock_push_failure") as report, \
             patch.object(tasks, "safe_commit"):
            distributor_listing_push("RSR-ACC-1", _payload(mode="stock"))
            distributor_listing_push("RSR-ACC-2", _payload(mode="stock"))
            _commit()
        self.assertEqual([c.args[:2] for c in report.call_args_list],
                         [("RSR-ACC-1", "retail"), ("RSR-ACC-2", "retail")])
        self.assertIsInstance(report.call_args.args[2], RuntimeError)

    def test_copy_mode_sends_name_and_description_only(self):
        """Optimized copy loaded after listing: no full push (it would sideload
        every image again), and nothing before the product exists."""
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", {"mode": "copy"})
        self.assertEqual((mock_enqueue.call_args.args[0], mock_enqueue.call_args.kwargs["job_id"]),
                         ("ffl_woo_sync.woocommerce.tasks.push_copy_task",
                          "woo-listing:retail:RSR-ACC-1:copy"))
        with patch("ffl_woo_sync.woocommerce.distributor._site_product_id", return_value=""), \
             patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", {"mode": "copy"})
        mock_enqueue.assert_not_called()

        from ffl_woo_sync.woocommerce import tasks
        client = MagicMock()
        with patch("frappe.db.get_value", return_value=frappe._dict(
                item_name="Magpul PMAG 30", description="<p>30 rounds.</p>", woo_product_id="555")), \
             patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client):
            tasks.push_copy_task("RSR-ACC-1")
        client.update_product.assert_called_once_with(
            "555", {"name": "Magpul PMAG 30", "description": "<p>30 rounds.</p>"})

    def test_copy_mode_sends_short_description_and_seo_meta_when_set(self):
        """The Item's short description and SEO fields ride the copy push — each
        only when set, so an empty one leaves the store's value (an SEO routine's,
        or wp-admin's) where it is."""
        from ffl_woo_sync.woocommerce import tasks
        client = MagicMock()
        row = frappe._dict(item_name="Magpul PMAG 30", description="<p>30 rounds.</p>",
            woo_product_id="555", woo_short_description="<p>Thirty rounds.</p>",
            woo_seo_title="Magpul PMAG 30 Gen M3 Magazine", woo_seo_description="",
            woo_focus_keyword="magpul pmag 30")
        with patch("frappe.db.get_value", return_value=row), \
             patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client):
            tasks.push_copy_task("RSR-ACC-1")
        client.update_product.assert_called_once_with("555", {
            "name": "Magpul PMAG 30", "description": "<p>30 rounds.</p>",
            "short_description": "<p>Thirty rounds.</p>",
            "meta_data": [
                {"key": "_ffl_seo_title", "value": "Magpul PMAG 30 Gen M3 Magazine"},
                {"key": "_ffl_seo_focus_keyword", "value": "magpul pmag 30"}]})

    def test_mode_is_case_insensitive(self):
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _payload(mode=" Stock "))
            _commit()
        self.assertEqual(mock_enqueue.call_args.args[0], _STOCK_TASK)

    def test_unknown_mode_falls_back_to_a_full_push(self):
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _payload(mode="whatever"))
        self.assertEqual(mock_enqueue.call_args.args[0], _FULL_TASK)

    def test_shipping_class_slug_normalised(self):
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _payload(shipping_class=" Dropship-Handgun "))
        self.assertEqual(mock_enqueue.call_args.kwargs["shipping_class"],
                         "dropship-handgun")

    def test_malformed_shipping_class_is_dropped(self):
        """A value that is not even a slug cannot identify a class — drop it
        rather than create garbage on the store."""
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _payload(shipping_class="Free Shipping!"))
        self.assertEqual(mock_enqueue.call_args.kwargs["shipping_class"], "")

    def test_off_table_slug_is_passed_through_with_a_warning(self):
        """The slug table is the one we know; a well-formed slug outside it is a
        caller decision, so it travels — but it is announced, because it will
        create a shipping class on the live store."""
        with patch("frappe.enqueue") as mock_enqueue, \
             patch("frappe.logger") as mock_logger:
            distributor_listing_push("RSR-ACC-1", _payload(shipping_class="free-shipping"))
        self.assertEqual(mock_enqueue.call_args.kwargs["shipping_class"], "free-shipping")
        mock_logger.return_value.warning.assert_called_once()

    def test_table_slugs_do_not_warn(self):
        with patch("frappe.enqueue"), patch("frappe.logger") as mock_logger:
            distributor_listing_push("RSR-ACC-1", _payload(shipping_class="dropship-handgun"))
        mock_logger.return_value.warning.assert_not_called()

    def test_the_dealer_site_raises(self):
        """The hook contract (docs/extensions/hooks.md): a site with no store is a
        ValueError, like any other malformed call."""
        with patch("frappe.enqueue") as mock_enqueue:
            with self.assertRaises(ValueError):
                distributor_listing_push("RSR-ACC-1", _payload(site="dealer"))
        mock_enqueue.assert_not_called()

    def test_unknown_site_raises(self):
        with patch("frappe.enqueue") as mock_enqueue:
            with self.assertRaises(ValueError):
                distributor_listing_push("RSR-ACC-1", _payload(site="storefront"))
        mock_enqueue.assert_not_called()

    def test_blank_item_code_raises(self):
        with patch("frappe.enqueue") as mock_enqueue:
            with self.assertRaises(ValueError):
                distributor_listing_push("", _payload())
        mock_enqueue.assert_not_called()


# ===========================================================================
# delist branch (the gap found when the two sides were compared)
# ===========================================================================

class TestDelistBranch(FrappeTestCase):
    """`delist: true` routes to the delist wrapper, not a push.

    Without it a discontinued / dropship-blocked product hangs on the shop at
    "out of stock" forever: the listing sweep stops pushing it, so nothing ever
    takes it down (C2).
    """

    def test_delist_routes_to_the_delist_wrapper(self):
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _payload(delist=True, stock=0))
        mock_enqueue.assert_called_once_with(
            _DELIST_TASK,
            queue="long",
            enqueue_after_commit=True,
            job_id="woo-listing:retail:RSR-ACC-1:delist",
            deduplicate=True,
            item_code="RSR-ACC-1",
            site="retail",
        )

    def test_delist_wins_over_mode(self):
        """A payload carrying both must come down, not be re-stocked."""
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _payload(delist=True, mode="stock"))
        self.assertEqual(mock_enqueue.call_args.args[0], _DELIST_TASK)

    def test_falsy_delist_is_a_normal_push(self):
        for value in (False, 0, None, ""):
            with self.subTest(value=value):
                with patch("frappe.enqueue") as mock_enqueue:
                    distributor_listing_push("RSR-ACC-1", _payload(delist=value))
                self.assertEqual(mock_enqueue.call_args.args[0], _FULL_TASK)

    def test_delist_is_not_marked_deliberate(self):
        """`delist_item_task` must reach `delist_item` WITHOUT `_deliberate`:
        that flag is the human "keep it off the shop" sentinel, and setting it
        from a machine would make a restock unable to republish the product."""
        from ffl_woo_sync.woocommerce.tasks import delist_item_task

        with patch("ffl_woo_sync.woocommerce.sync.delist_item") as mock_delist:
            delist_item_task("RSR-ACC-1")
        self.assertNotIn("_deliberate", mock_delist.call_args.kwargs)


# ===========================================================================
# The frozen payload-key contract, pinned from this side
# ===========================================================================

class TestSpec17PayloadContract(FrappeTestCase):
    """The payload keys were frozen after both sides' deliveries were compared.

    This is the routing half of the contract; the meta half (`_osa_restricted_states`
    always present on a listing push, never touched on a retail push) is pinned in
    test_woo_listing_meta.py.
    """

    def test_full_spec17_payload_routes_to_a_full_push(self):
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _spec17_payload())
        self.assertEqual(mock_enqueue.call_args.args[0], _FULL_TASK)
        self.assertEqual(mock_enqueue.call_args.kwargs["shipping_class"],
                         "dropship-accessory")

    def test_advisory_keys_are_ignored_not_trusted(self):
        """price / stock / product_brand are advisory — the push reads the Item
        (SSOT), so none of them may reach the enqueue call."""
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _spec17_payload())
        passed = set(mock_enqueue.call_args.kwargs)
        self.assertEqual(
            passed & {"price", "stock", "product_brand", "meta",
                      "restricted_states", "fulfillment_mode", "listing_status",
                      "is_firearm", "sku"},
            set(),
        )

    def test_the_three_routing_keys(self):
        _isolate_stock_state(self)
        cases = [
            ({}, _FULL_TASK),
            ({"mode": "stock"}, _STOCK_TASK),
            ({"delist": True}, _DELIST_TASK),
        ]
        for extra, task in cases:
            with self.subTest(extra=extra):
                with patch("frappe.enqueue") as mock_enqueue, \
                     patch("ffl_woo_sync.woocommerce.distributor._site_product_id",
                           return_value="4711"):
                    distributor_listing_push("RSR-ACC-1", _spec17_payload(**extra))
                    _commit()  # the stock route only queues once its transaction commits
                self.assertEqual(mock_enqueue.call_args.args[0], task)

    def test_site_defaults_to_retail_the_only_v1_value(self):
        with patch("frappe.enqueue") as mock_enqueue:
            distributor_listing_push("RSR-ACC-1", _spec17_payload())
        self.assertEqual(mock_enqueue.call_args.kwargs["site"], "retail")

    def test_the_three_shipping_class_slugs_are_accepted_verbatim(self):
        for slug in ("dropship-handgun", "dropship-firearm", "dropship-accessory"):
            with self.subTest(slug=slug):
                with patch("frappe.enqueue") as mock_enqueue:
                    distributor_listing_push(
                        "RSR-1", _spec17_payload(shipping_class=slug))
                self.assertEqual(mock_enqueue.call_args.kwargs["shipping_class"], slug)


# ===========================================================================
# _skip_push guard (reused verbatim from doc_events)
# ===========================================================================

class TestSkipPushGuard(FrappeTestCase):
    def test_inbound_webhook_echo_is_skipped(self):
        frappe.flags.woo_sync_inbound = True
        try:
            with patch("frappe.enqueue") as mock_enqueue:
                distributor_listing_push("RSR-ACC-1", _payload())
            mock_enqueue.assert_not_called()
        finally:
            frappe.flags.woo_sync_inbound = False

    def test_migrate_is_skipped(self):
        frappe.flags["in_migrate"] = True
        try:
            with patch("frappe.enqueue") as mock_enqueue:
                distributor_listing_push("RSR-ACC-1", _payload())
            mock_enqueue.assert_not_called()
        finally:
            frappe.flags.pop("in_migrate", None)


# ===========================================================================
# Shipping-class table
# ===========================================================================

class TestShippingClassTable(FrappeTestCase):
    def test_three_dropship_classes(self):
        self.assertEqual(sorted(DROPSHIP_SHIPPING_CLASSES),
                         ["dropship-accessory", "dropship-firearm", "dropship-handgun"])

    def test_display_names(self):
        self.assertEqual(shipping_class_name("dropship-handgun"), "Dropship Handgun")

    def test_unknown_slug_gets_a_titled_name(self):
        self.assertEqual(shipping_class_name("dropship-nfa"), "Dropship Nfa")


# ===========================================================================
# The enqueue target
# ===========================================================================

class TestDistributorListingPushTask(FrappeTestCase):
    def test_calls_push_item_with_the_shipping_class(self):
        from ffl_woo_sync.woocommerce.tasks import distributor_listing_push_task

        with patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_push:
            distributor_listing_push_task("RSR-ACC-1", shipping_class="dropship-handgun")
        mock_push.assert_called_once_with(
            "RSR-ACC-1", site="retail", shipping_class="dropship-handgun", listing=True,
            pos_fields_only=False)

    def test_lean_flag_threads_through_to_push_item(self):
        from ffl_woo_sync.woocommerce.tasks import distributor_listing_push_task

        with patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_push:
            distributor_listing_push_task("RSR-ACC-1", pos_fields_only=True)
        self.assertIs(mock_push.call_args.kwargs["pos_fields_only"], True)

    def test_marks_the_push_as_a_listing_push(self):
        """`listing=True` is what unlocks the brand term and the always-present
        restricted-states meta — the two fields a plain retail push must not
        touch."""
        from ffl_woo_sync.woocommerce.tasks import distributor_listing_push_task

        with patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_push:
            distributor_listing_push_task("RSR-ACC-1")
        self.assertIs(mock_push.call_args.kwargs["listing"], True)

    def test_absorbs_and_logs_errors(self):
        """Same contract as every other enqueue target: a failed push must not
        kill the worker (push_item already writes woo_sync_error to the Item)."""
        from ffl_woo_sync.woocommerce.tasks import distributor_listing_push_task

        with patch("ffl_woo_sync.woocommerce.sync.push_item",
                   side_effect=RuntimeError("boom")), \
             patch("frappe.log_error") as mock_log:
            distributor_listing_push_task("RSR-ACC-1")
        mock_log.assert_called_once()

    def test_reports_the_failure_to_the_listing_pipeline(self):
        """A push that fails inside the queue is the one the listing side cannot
        see (`fire_listing_push` returned ok at hand-off): every queued push
        task tells it through `distributor_listing_push_failed` (item, site,
        error). A failing hook is logged, never raised."""
        from ffl_woo_sync.woocommerce.tasks import (
            delist_item_task, distributor_listing_push_task, push_stock_task)

        reports = []
        boom = RuntimeError("boom")
        with patch("ffl_woo_sync.woocommerce.sync.push_item", side_effect=boom), \
             patch("ffl_woo_sync.woocommerce.sync.push_stock", side_effect=boom), \
             patch("ffl_woo_sync.woocommerce.sync.delist_item", side_effect=boom), \
             patch("frappe.log_error") as mock_log, \
             patch("frappe.get_hooks", return_value=["stub.report"]), \
             patch("frappe.get_attr", return_value=lambda *a: reports.append(a)):
            distributor_listing_push_task("RSR-ACC-1")
            push_stock_task("RSR-ACC-1", site="retail")
            delist_item_task("RSR-ACC-1")
        self.assertEqual(reports, [
            ("RSR-ACC-1", "retail", "RuntimeError: boom"),
            ("RSR-ACC-1", "retail", "RuntimeError: boom"),
            ("RSR-ACC-1", "retail", "RuntimeError: boom")])
        self.assertEqual(mock_log.call_count, 3)

        with patch("ffl_woo_sync.woocommerce.sync.push_item", side_effect=boom), \
             patch("frappe.log_error") as mock_log, \
             patch("frappe.get_hooks", return_value=["stub.report"]), \
             patch("frappe.get_attr", return_value=lambda *a: 1 / 0):
            distributor_listing_push_task("RSR-ACC-1")
        self.assertEqual(mock_log.call_count, 2, "the push failure and the hook failure")
