"""Install-time and test-time setup for the app."""


def before_tests():
	"""Give the test site the ERPNext records the tests take for granted.

	`bench run-tests --app checkin_app` only runs *this* app's `before_tests`
	hook, so a fresh CI site would otherwise have no Company, no Fiscal Year and
	no Activity Types -- the tests read all three. ERPNext's own hook runs the
	setup wizard once, commits, and is idempotent, so this is safe to re-run.
	"""
	from erpnext.setup.utils import before_tests as erpnext_before_tests

	erpnext_before_tests()
