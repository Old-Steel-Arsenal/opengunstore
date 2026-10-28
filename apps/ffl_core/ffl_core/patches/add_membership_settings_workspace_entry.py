"""Add the Membership Settings link (renewal switch, yearly reminder, Needs attention) to the Settings
workspace's POS & Sales card on sites that already have it. The workspace JSON covers a fresh install
(see ffl_core.workspace_shortcuts). One-time, idempotent."""
from ffl_core.workspace_shortcuts import ensure_card_link


def execute():
	ensure_card_link("Membership Settings", "Membership Settings", card="POS & Sales", link_type="DocType",
		workspace="Settings")
