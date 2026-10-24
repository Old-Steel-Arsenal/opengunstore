__version__ = "0.2.1"

# Where a Website User goes when the page they belong on is switched off. A
# Website route, deliberately: see get_website_user_home_page below for why
# "no opinion" (None) is not the same thing as "the default landing page".
WEBSITE_USER_FALLBACK_HOME_PAGE = "me"


def check_app_permission() -> bool:
	"""Gate for hooks.add_to_apps_screen — resolved by frappe.apps.get_apps().

	Mirrors erpnext.check_app_permission (erpnext/__init__.py:157). Two callers,
	and the second one is why this exists at all:

	* the app launcher / apps screen — a Website User can't open a Desk
	  workspace, so don't offer them the tile;
	* frappe.apps.get_default_path(), which frappe/auth.py:201 consults BEFORE
	  website.utils.get_home_page() when a Website User logs in:

	      response["home_page"] = get_default_path() or "/" + get_home_page()

	  With erpnext's tile already gated and ours not, a Consignment Dealer's
	  get_apps() returned exactly one app, get_default_path() took its
	  `len(_apps) == 1` branch, and every dealer logging in at /login was sent
	  to the Desk — /app/gun-shop then, /desk/gun-shop today — where they
	  read "Not Permitted".
	  Return False and get_default_path() returns None, so get_home_page() —
	  and with it get_website_user_home_page() below — is finally what decides.

	get_apps() swallows an exception from this callback by skipping the app, so
	a raise here would silently hide Gun Shop from staff. Keep it total.
	"""
	import frappe
	from frappe.utils.user import is_website_user

	if frappe.session.user == "Administrator":
		return True
	return not is_website_user()


def get_website_user_home_page(user: str | None = None) -> str | None:
	"""Hook: where a Website User lands — asked of the extensions' providers.

	Core keeps the gate (is_website_user) and the fall-through; who lands
	where is declared by whoever owns a portal, through hooks
	``gunshop_website_user_home_page`` (docs/extensions/hooks.md): each provider
	is called with the user and answers a Website route or None; the first
	answer wins. Originally written to send a Consignment Dealer to the
	portal — the reasoning below is why it is a callable
	and why the gate lives here.

	Registered as hooks.get_website_user_home_page, read by
	website.utils.get_home_page_via_hooks() (frappe 16.22.0, website/utils.py:149):

	    home_page_method = frappe.get_hooks("get_website_user_home_page")
	    if home_page_method:
	        home_page = frappe.get_attr(home_page_method[-1])(frappe.session.user)

	Two traps in those three lines, both load-bearing here:

	1. Despite the name, frappe calls this for EVERY user — the call is not
	   gated on user_type. A Desk user reaches it too, so this function decides
	   who it answers for, and returns None for everyone else. Returning None
	   falls through to hooks.role_home_page and then hooks.home_page, i.e.
	   "desk/gun-shop" — the staff landing page, unchanged. Note what that makes
	   None mean: not "use the default", but "hand this user the DESK". It is
	   the right answer for staff and the wrong one for anybody who cannot open
	   a Desk page, which is why the switched-off branch below names a route.

	2. `frappe.get_roles(user)` alone is NOT a dealer test. permissions.py:544:

	       if user == "Administrator":
	           return frappe.get_all("Role", pluck="name")  # return all roles

	   Administrator implicitly holds every role on the site, this one included.
	   That is precisely how it happened once, one layer up (a Role.home_page on
	   the dealer role sent the store owner to /consignment, which throws for a
	   session with no dealer behind it). The `is_website_user` gate is what
	   makes the role check safe: a dealer is a Website User, Administrator and
	   every staff member are System Users.

	The same trap disqualifies the two neighbouring hooks: `website_user_home_page`
	is a bare string handed to whoever reaches it, and `role_home_page` is
	another `for role in frappe.get_roles()` loop. Only a callable can tell an
	Administrator apart from the dealer whose role it happens to hold.
	"""
	import frappe
	from frappe.utils.user import is_website_user

	user = user or frappe.session.user
	# Guest is a Website User too, but holds only the Guest role (permissions.py:540).
	if not is_website_user(user):
		return None
	# A provider whose feature is switched off must still answer with a
	# Website route for the users it created (WEBSITE_USER_FALLBACK_HOME_PAGE,
	# frappe's own account page): None here does NOT mean "the default landing
	# page", it means "carry on down get_home_page_via_hooks()" — past
	# role_home_page into hooks.home_page = "desk/gun-shop", a Desk route a
	# Website User cannot open.
	for hook in frappe.get_hooks("gunshop_website_user_home_page"):
		try:
			route = frappe.get_attr(hook)(user)
		except Exception:
			frappe.log_error(title=f"gunshop_website_user_home_page failed: {hook}")
			continue
		if route:
			return str(route)
	return None
