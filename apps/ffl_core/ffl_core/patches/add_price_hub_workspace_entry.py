"""Add the Price Hub link (distributor costs over time, with the order cart) to the Gun Shop
workspace's Buying card on sites that already have it. The workspace JSON covers a fresh install
(see ffl_core.workspace_shortcuts). One-time, idempotent."""
from ffl_core.workspace_shortcuts import ensure_card_link


def execute():
	ensure_card_link("Price Hub", "price-hub", card="Buying", link_type="Page", workspace="Gun Shop")
