"""Canonical role sets for whitelisted-endpoint gates.

Single source: the counter tuple used to live only as
``manual_order._ROLES``; the endpoint-gates hardening (osa/endpoint-gates,
2026-08-07 adjudication) needs it from modules manual_order itself imports
(firearm.py, receive_goods.py), so it moved here and manual_order now
aliases it. The API docs generator derives every endpoint's published Auth
line from these constants — renaming or editing them shows up as a docs
diff and trips CI.
"""
from __future__ import annotations

# Every role that rings or services a counter sale. The widest gate handed
# to read-endpoints that POS/desk pages call for cashiers.
COUNTER_ROLES = ("Sales User", "Sales Manager", "Stock Manager", "System Manager")

# Who may run or record a 4473 at the register. Deliberately NARROWER than
# COUNTER_ROLES: Stock Manager receives and books inventory but never transacts
# with a buyer, and a 4473 is a transaction with one. Shared by the whitelisted
# endpoints (fastbound.pos_4473._check_perm) and the FFL Disposition guard that
# backs them, so the back wall can never end up wider than the door it protects.
COUNTER_4473_ROLES = ("Sales User", "Sales Manager", "System Manager")

# Inventory-shaping actions (receive, intake, listing pushes).
STOCK_ROLES = ("Stock Manager", "System Manager")

# Who may take a POS trade-in (api/trade_in.create_trade_in_intake and the POS Trade-in button, which mirrors
# this list in ffl_core.bundle.js). The counter's Sales User, plus the
# stock roles that always could. Sales Manager is deliberately NOT in it: a Sales Manager without the Sales
# User role is refused (the trade-in's stock receipt rests on DocPerms only Sales User gets,
# setup/pos_compat.ROLE_GRANTS).
TRADE_IN_ROLES = ("Sales User",) + STOCK_ROLES

# Who may scan into a stocktake (Inventory Count page). Same set as the page's own
# role list; finalizing (the adjustment) is STOCK_ROLES.
COUNT_ROLES = ("Stock User", "Stock Manager", "System Manager")

# Who may undo cash entries and counts and change a seller payment (Cash Drawer page).
# The counter roles record deposits, cash from the bank, expenses and the count itself.
CASH_MANAGER_ROLES = ("Sales Manager", "System Manager")

# Who may read the cash log: the counter, and the bookkeeper auditing it (the MCP cpa surface signs in as one).
CASH_LOG_ROLES = COUNTER_ROLES + ("Accounts User", "Accounts Manager")

# Who may look at the store map and move things between locations: everyone who
# rings a sale or counts stock. Creating / disabling zones is STOCK_ROLES.
STORAGE_ROLES = ("Sales User", "Sales Manager", "Stock User", "Stock Manager", "System Manager")

# Who may merge two customer records (customer_profile.merge_customer). Same set as
# CASH_MANAGER_ROLES and the 4473 override, kept separate because it guards a
# different decision; the ban override reuses this one.
CUSTOMER_MANAGER_ROLES = ("Sales Manager", "System Manager")

# Who may approve a price a counter clerk lowers (price_approval.py), and whose own lowered
# prices need no one else's approval.
PRICE_APPROVER_ROLES = ("Sales Manager", "System Manager")
# Who may ask whether a sale needs that approval, and ask for it: everyone who saves a sale —
# the counter, and the bookkeeper on a Sales Invoice form. Each document's own permission still applies.
PRICE_CHECK_ROLES = COUNTER_ROLES + ("Accounts User", "Accounts Manager")

# Who lands on the Gun Shop Overview page: every role hooks.role_home_page sends there, plus Sales
# Manager. Same set as the page's own role list; each block on the page then checks its own.
OVERVIEW_ROLES = ("System Manager", "Sales User", "Sales Manager", "Stock User", "Stock Manager", "Accounts User")

# Admin-only integration/settings actions.
SYSTEM_ROLES = ("System Manager",)
