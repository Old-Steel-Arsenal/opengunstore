"""Bootinfo extensions — server constants the desk/POS JS needs."""
import frappe

from ffl_core.compliance.mode import paper_4473_mode
from ffl_core.feature_gate import SETTINGS_DOCTYPE
from ffl_core.utils import single_flags
from ffl_integrations.integration_gate import (
    card_reference_required,
    fastbound_enabled,
    gunbroker_enabled,
)
from ffl_integrations.payments.registry import active_processor
from ffl_integrations.payments.setup import MODE_OF_PAYMENT_NAME


def extend_bootinfo(bootinfo):
    # Single source of truth for the card tender name: the POS bundle reads
    # frappe.boot.card_mop instead of hard-coding "Credit Card" a second time.
    bootinfo.card_mop = MODE_OF_PAYMENT_NAME
    # Integration switches, so the POS bundle can decide BEFORE it saves a draft and
    # round-trips to a server that would only answer {"skip": true}. The server
    # gates stay authoritative — these spare the counter a dead round-trip and let
    # the UI hide buttons that would do nothing.
    try:
        bootinfo.ffl_fastbound_enabled = 1 if fastbound_enabled() else 0
        bootinfo.ffl_paper_4473_mode = 1 if paper_4473_mode() else 0
        bootinfo.ffl_require_card_reference = 1 if card_reference_required() else 0
        # The Store Customer ID Numbers switch (FFL Settings, the same Single as
        # ffl_paper_4473_mode above), with ffl_core.feature_gate's default: an
        # absent key means OFF (every store ran without them before it existed;
        # the Customer validate blanks them regardless). Extensions' switches
        # travel as frappe.boot.gunshop_extensions.
        flags = single_flags(SETTINGS_DOCTYPE, {"store_customer_id_numbers": 0})
        bootinfo.ffl_store_customer_id_numbers = 1 if flags["store_customer_id_numbers"] else 0
        # Card on file: whether the card dialogs offer "Save this card" (switched on, an
        # authorization picked, and the active processor can keep cards). Never raises.
        from ffl_integrations.payments.saved_cards import offer_enabled
        bootinfo.ffl_card_on_file = 1 if offer_enabled() else 0
    except Exception:
        # Boot must never fail on a settings read: a site mid-migrate (tabSingles
        # present, this app's doctypes not yet synced) still has to log in and
        # finish migrating. Omitting the keys makes the bundle fall back to its own
        # defaults, which are the integration-ON behaviour that shipped before these
        # switches existed.
        # WITH the traceback: this runs on every login and is designed to be
        # survivable, which is exactly how a silent breakage here would go
        # unnoticed for months. log_error itself writes to the DB, so it is the
        # last thing tried and its own failure is swallowed rather than taking
        # the login down with it.
        try:
            frappe.log_error(
                title="ffl_integrations: boot switches unavailable",
                message=frappe.get_traceback(with_context=True))
        except Exception:
            pass

    # The card processor that takes new money: {code, label, enabled}; no processor
    # on = enabled 0 (cards are plain tender on a standalone terminal). Its own try:
    # two processors switched on make active_processor() refuse, and that must not
    # take the FastBound / 4473 keys above down with it. An absent key makes the
    # bundle fall back to driving the terminal, where the charge call then says
    # what is wrong.
    try:
        card = active_processor()
        bootinfo.ffl_card_processor = {"code": card.code if card else "",
            "label": card.label if card else "", "enabled": 1 if card else 0}
    except Exception:
        try:
            frappe.log_error(
                title="ffl_integrations: card processor unavailable at boot",
                message=frappe.get_traceback(with_context=True))
        except Exception:
            pass

    # What this site can do without the Data Service (data_service.capabilities):
    # the desk's "not set up" notices read it. Own try, like GunBroker below; an
    # absent key means the notice stays hidden (the server still refuses).
    try:
        from ffl_integrations.data_service import capabilities

        bootinfo.ffl_capabilities = capabilities()
    except Exception:
        try:
            frappe.log_error(
                title="ffl_integrations: capabilities unavailable at boot",
                message=frappe.get_traceback(with_context=True))
        except Exception:
            pass

    # GunBroker gets its OWN try, and that is the point of it being separate.
    # The block above is one try around several reads: fold this one in and a
    # failure reading GunBroker Settings — the newest Single, and the one most
    # likely to be missing on a site part-way through a migrate — would take the
    # Payroc and FastBound keys down with it, silently falling the POS bundle
    # back to its pre-switch defaults for integrations that are working fine.
    #
    # The default here is the opposite of theirs, too: absent key means NO
    # GunBroker buttons. Those two switches default on because they predate the
    # switch and were already live; GunBroker is off until a store says
    # otherwise, and a boot-time hiccup must not be what turns it on.
    try:
        bootinfo.ffl_gunbroker_enabled = 1 if gunbroker_enabled() else 0
    except Exception:
        try:
            frappe.log_error(
                title="ffl_integrations: GunBroker boot switch unavailable",
                message=frappe.get_traceback(with_context=True))
        except Exception:
            pass
