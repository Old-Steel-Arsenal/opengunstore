"""Is an external integration switched on for this site?

One home for the "should we talk to Payroc / FastBound at all" question, so the
POS bundle, the doc_event hooks, the scheduler jobs and the whitelisted API
surface can never answer it differently. (RSR and ShipStation still answer it
their own way — `RSR Settings.api_enabled`, `shipstation/api._enabled` — folding
those in is a deliberate non-goal of this change, not an oversight. Search before
assuming this module is authoritative for a third vendor.)

Two DIFFERENT questions live here and must not be conflated:

* ``*_enabled()`` — the operator's explicit statement of intent (the ``enabled``
  Check on the settings Single). A store with no Payroc terminal, or one keeping
  its bound book on paper, unchecks this and the corresponding machinery goes
  quiet: no gateway calls, no pushes, no cron noise, no buttons.
* ``payroc_ready()`` — enabled AND actually usable (credentials present). This is
  the pre-existing go-live gate: an armed-but-unconfigured Payroc is inert rather
  than an error, because a half-filled settings form must not stop the counter
  from taking money.

There is deliberately NO ``fastbound_ready()``. FastBound work is gated on
``fastbound_enabled()`` alone: an ENABLED-but-misconfigured FastBound must keep
failing loudly (the failure lands in ``fastbound_sync_error`` and the daily stuck
digest). Swallowing that as "not ready" would turn a broken bound-book sync into
silence, which is the one outcome an A&D book cannot afford.

Defaults are ON for the Payroc and FastBound switches — the docfields say so,
and ``ffl_core.utils.single_flag`` honours those defaults on a site that has
never saved the Single, so shipping these switches cannot silently disable a
live integration at migrate time.

``gunbroker_enabled()`` defaults the other way, OFF, and for the mirror-image
reason: those two switches were retrofitted over integrations that were already
running, so the safe default was "keep running". GunBroker is new everywhere,
nobody is depending on it yet, and switching it on means publishing real offers
to real buyers on a marketplace where a sale cannot be unwound from the seller
side. A store states that intent by ticking the box.
"""
from __future__ import annotations

from ffl_core.utils import single_flag

PAYROC_SETTINGS = "Payroc Settings"
FASTBOUND_SETTINGS = "FastBound Settings"
GUNBROKER_SETTINGS = "GunBroker Settings"


def payroc_enabled() -> bool:
	"""True unless the operator unchecked Payroc Settings → Enable Payroc."""
	return single_flag(PAYROC_SETTINGS, "enabled", default=1)


def fastbound_enabled() -> bool:
	"""True unless the operator unchecked FastBound Settings → Enable FastBound."""
	return single_flag(FASTBOUND_SETTINGS, "enabled", default=1)


def gunbroker_enabled() -> bool:
	"""True only when the operator ticked GunBroker Settings -> Enable GunBroker.

	Default OFF (see the module docstring). Every outbound path funnels through
	this: the client refuses to build, the scheduled jobs no-op, and the Serial No
	form renders no GunBroker buttons. Consulted BEFORE any credential is read, so
	a store that has switched GunBroker off but left its DevKey in the form cannot
	keep listing guns on a marketplace it has stopped using.
	"""
	return single_flag(GUNBROKER_SETTINGS, "enabled", default=0)


def card_processor_enabled() -> bool:
	"""True while any card processor (payments.registry) is ON — switched on and
	configured: "is the gateway live", asked without naming a vendor. Never raises."""
	from ffl_integrations.payments.registry import any_on

	return any_on()


def card_processor_ready(require_device: bool = False) -> bool:
	"""The active card processor's go-live gate (``CardProcessor.ready``): switched on
	AND holding usable credentials — and, with ``require_device``, a card device. No
	processor on = False."""
	from ffl_integrations.payments.registry import active_processor

	p = active_processor()
	return bool(p and p.ready(require_device=require_device))


def card_reference_required() -> bool:
	"""True when a manual-terminal card tender must carry an approval code.

	Named as a predicate, not ``require_*``: everything else called ``require_*``
	in these two apps is a ``(doc, method)`` doc_event validator that throws
	(``compliance.firearm.require_serial_for_firearm``,
	``compliance.payment_mode.require_zelle_transaction_no``, …), and one of them
	— ``payments.api.require_card_reference_on_manual_tender`` — is this flag's own
	consumer.

	Only meaningful with every card processor switched OFF: with a gateway live the
	approval code comes back from the terminal and there is nothing for staff to
	type. The docfield hides the checkbox accordingly; this mirrors that, so a value
	left checked from a spell on the standalone terminal cannot start demanding a
	hand-typed reference the moment the processor is switched back on. The switch
	itself still lives on Payroc Settings (where it shipped) whichever processor
	the store uses.

	Defaults ON (on accountant review): because it is inert
	while the gateway is live, defaulting it on costs stores using Payroc exactly
	nothing and only reaches the population that needs it — and without a reference
	a standalone-terminal swipe leaves nothing in this system that can be matched
	against the terminal's batch. A store that wants the counter faster can still
	uncheck it.
	"""
	if card_processor_enabled():
		return False
	return single_flag(PAYROC_SETTINGS, "require_card_reference", default=1)


def payroc_ready(require_device: bool = False) -> bool:
	"""The documented go-live gate: switched on AND holding usable credentials.

	With the switch off, no Merchant API Key, or (for device charges) no A80
	serial, the integration is INERT — POS skips the gateway entirely and card rows
	record as plain tender, swiped on a standalone terminal. Filling in real
	credentials on an enabled integration is what arms it.

	The credential half is answered by ``PayrocConfig.from_settings()`` rather than
	re-listed here, so "what counts as configured" has one definition: the moment
	the client starts requiring another field, this gate follows automatically
	instead of cheerfully reporting ready and then failing at the first call.
	"""
	if not payroc_enabled():
		return False
	# Imported lazily: client.py pulls in requests and the whole HTTP stack, which
	# a boot-time switch read has no business loading.
	from ffl_integrations.payroc.client import PayrocConfig, PayrocConfigError

	try:
		cfg = PayrocConfig.from_settings()
	except PayrocConfigError:
		return False
	if require_device and not cfg.default_serial_number:
		return False
	return True
