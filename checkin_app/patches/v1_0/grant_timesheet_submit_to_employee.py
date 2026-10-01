"""Let the Employee role submit Timesheets.

ERPNext ships the Employee role with `submit = 0` on Timesheet, so an employee
cannot submit their own time. The time-tracking API needs submission to happen
-- ERPNext rolls hours into Task and Project from submitted rows only -- and
this patch makes the permission system allow it rather than having the API work
around it with `ignore_permissions`.

Writing it as a patch (instead of clicking through Role Permission Manager)
keeps the change reproducible: any site that installs this app gets it.

The first call copies Timesheet's standard permissions into Custom DocPerm,
which is how Frappe records a deviation from a DocType's shipped permissions.
"""

import frappe
from frappe.permissions import update_permission_property

DOCTYPE = "Timesheet"
ROLE = "Employee"


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		# ERPNext is not installed on this site; nothing to grant.
		return

	update_permission_property(DOCTYPE, ROLE, 0, "submit", 1)

	frappe.clear_cache()
