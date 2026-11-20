"""Add the Inventory Import link to the Settings workspace's Catalog Reference card on sites that already
have it. The workspace JSON covers a fresh install (see ffl_core.workspace_shortcuts). One-time, idempotent."""
from ffl_core.workspace_shortcuts import ensure_card_link


def execute():
	ensure_card_link("Inventory Import", "Inventory Import", card="Catalog Reference", link_type="DocType",
		workspace="Settings")
