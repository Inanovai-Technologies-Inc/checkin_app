"""Phase 1 backend for the "Log Time for a Project" desk page.

Storage model
-------------
No new DocType is introduced. A work session is a stock ERPNext **Timesheet**
and each segment of that session is a row in its **Timesheet Detail** child
table::

    Timesheet (Draft)                 <- the session, while the timer runs
      Timesheet Detail  09:00-10:30   <- closed segment (Project A / Task A)
      Timesheet Detail  10:30-        <- the open segment; to_time is empty

An **open segment is a Timesheet Detail row with an empty `to_time` inside a
Draft Timesheet**. That single fact is the whole timer state: nothing is cached
client-side, so a timer started on one device is visible on another.

`stop_timer` closes the last row and submits the Timesheet. Submitting is what
makes the time count -- ERPNext rolls hours and costs up into Task and Project
from submitted rows only (`Task.update_time_and_costing` filters `docstatus=1`).

The employee is always resolved from `frappe.session.user`; no endpoint accepts
an employee from the caller.

See docs/PHASE1_BACKEND_API.md for the frontend-facing contract.
"""

import datetime

import frappe
from frappe import _
from frappe.query_builder import DocType, Order
from frappe.query_builder.functions import Max
from frappe.utils import (
	flt,
	get_datetime,
	get_time,
	getdate,
	now_datetime,
	time_diff_in_hours,
	today,
)

#: The UI computes elapsed time as `now_datetime() - start_time`, so every
#: datetime handed to it must be site-local and second-precision.
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Manual entries (`log_time`) carry a date and a duration but no clock time.
#: The first entry of a day is placed here and later ones are appended after it,
#: which keeps ERPNext's overlap validation meaningful instead of stacking every
#: manual entry on the same instant.
MANUAL_ENTRY_DAY_START = datetime.time(9, 0)


# ---------------------------------------------------------------------------
# Session / employee
# ---------------------------------------------------------------------------


def _require_employee():
	"""Resolve the Employee behind the session user, or explain why we cannot."""
	user = frappe.session.user

	if not user or user == "Guest":
		frappe.throw(_("Sign in to track time."), frappe.PermissionError)

	employee = frappe.db.get_value(
		"Employee",
		{"user_id": user, "status": "Active"},
		["name", "employee_name", "company"],
		as_dict=True,
	)

	if not employee:
		frappe.throw(
			_(
				"No active Employee record is linked to {0}. Set that user on an Employee record to track time."
			).format(frappe.bold(user))
		)

	return employee


def _lock_employee(employee):
	"""Serialise segment changes for one employee.

	The "is a timer already running?" check and the write that answers it must
	not interleave with the same pair from a second request, or an employee
	clicking Start on two devices could open two segments. Locking the Employee
	row makes the second request wait and then see the first one's segment.
	"""
	frappe.db.get_value("Employee", employee.name, "name", for_update=True)


def _company_for(employee):
	return (
		employee.company
		or frappe.defaults.get_user_default("Company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
	)


# ---------------------------------------------------------------------------
# Selection validation
# ---------------------------------------------------------------------------


def _validate_selection(project, activity_type, task=None):
	"""Normalise and check a Project / Activity Type / Task triple.

	Returns the cleaned triple. Raises on anything the Timesheet would either
	reject later or, worse, accept and silently misfile -- in particular a Task
	belonging to a different Project.
	"""
	project = (project or "").strip()
	activity_type = (activity_type or "").strip()
	task = (task or "").strip() or None

	if not project:
		frappe.throw(_("Select a Project before timing work."))

	if not activity_type:
		frappe.throw(_("Select an Activity Type before timing work."))

	if not frappe.db.exists("Project", project):
		frappe.throw(_("Project {0} does not exist.").format(frappe.bold(project)))

	if not frappe.db.exists("Activity Type", activity_type):
		frappe.throw(_("Activity Type {0} does not exist.").format(frappe.bold(activity_type)))

	if task:
		if not frappe.db.exists("Task", task):
			frappe.throw(_("Task {0} does not exist.").format(frappe.bold(task)))

		task_project = frappe.db.get_value("Task", task, "project")

		if task_project != project:
			frappe.throw(
				_("Task {0} belongs to Project {1}, not {2}.").format(
					frappe.bold(task),
					frappe.bold(task_project or _("no project")),
					frappe.bold(project),
				)
			)

	return project, activity_type, task


# ---------------------------------------------------------------------------
# Open segment
# ---------------------------------------------------------------------------


def _get_open_segment(employee):
	"""The employee's running segment, or None.

	Ordered newest-first so a corrupt double-open state -- only reachable by
	editing Timesheets directly -- still resolves to the segment the employee
	started most recently rather than an arbitrary one.
	"""
	timesheet = DocType("Timesheet")
	detail = DocType("Timesheet Detail")

	rows = (
		frappe.qb.from_(detail)
		.join(timesheet)
		.on(detail.parent == timesheet.name)
		.select(
			detail.name.as_("row"),
			detail.parent.as_("timesheet"),
			detail.project,
			detail.activity_type,
			detail.task,
			detail.from_time,
		)
		.where(
			(timesheet.employee == employee)
			& (timesheet.docstatus == 0)
			& (detail.parenttype == "Timesheet")
			& detail.to_time.isnull()
		)
		.orderby(detail.from_time, order=Order.desc)
	).run(as_dict=True)

	return rows[0] if rows else None


def _format_datetime(value):
	value = get_datetime(value)

	return value.strftime(DATETIME_FORMAT) if value else None


def _segment(row, timesheet=None):
	"""Shape an open row the way the page expects it."""
	task = row.get("task")

	return {
		"project": row.get("project"),
		"project_name": frappe.db.get_value("Project", row.get("project"), "project_name")
		or row.get("project"),
		"activity_type": row.get("activity_type"),
		"task": task,
		"task_subject": (frappe.db.get_value("Task", task, "subject") or task) if task else None,
		"start_time": _format_datetime(row.get("from_time")),
		# Informational: lets a caller jump to the session in progress. The page
		# does not depend on it.
		"timesheet": timesheet or row.get("timesheet"),
	}


def _row_by_name(timesheet, row_name):
	for row in timesheet.time_logs:
		if row.name == row_name:
			return row

	frappe.throw(_("The running segment is no longer on Timesheet {0}.").format(timesheet.name))


def _close_row(timesheet, row, end):
	"""Close `row` at `end`.

	Returns False and drops the row when nothing elapsed. A zero-hour row cannot
	be submitted (ERPNext rejects it), and keeping it would strand the employee
	with a timer they cannot stop -- there is no time to lose by removing it.
	"""
	if get_datetime(end) <= get_datetime(row.from_time):
		timesheet.time_logs = [r for r in timesheet.time_logs if r.name != row.name]

		for idx, remaining in enumerate(timesheet.time_logs, start=1):
			remaining.idx = idx

		return False

	row.to_time = end
	row.hours = time_diff_in_hours(end, row.from_time)

	return True


def _new_timesheet(employee):
	timesheet = frappe.new_doc("Timesheet")
	timesheet.employee = employee.name
	timesheet.company = _company_for(employee)
	# `user` is deliberately left unset, matching ERPNext's own Timesheet form:
	# the employee link already drives overlap validation and costing.

	return timesheet


def _append_segment(timesheet, project, activity_type, task, from_time, hours=None, to_time=None):
	return timesheet.append(
		"time_logs",
		{
			"activity_type": activity_type,
			"project": project,
			"task": task,
			"from_time": from_time,
			"to_time": to_time,
			"hours": flt(hours) if hours else 0,
		},
	)


def _as_datetime(value, date):
	"""Read a clock time against `date`, or pass a full datetime through.

	The page sends times of day ("14:30:00") because it already has a Date
	field; other callers may send a whole datetime. Both are accepted so the
	endpoint does not dictate which one a client must build.
	"""
	if value in (None, ""):
		return None

	if isinstance(value, datetime.datetime):
		return value

	if isinstance(value, datetime.timedelta):
		# Frappe's Time control hands back a timedelta since midnight.
		return datetime.datetime.combine(date, get_time(value))

	text = str(value).strip()
	if not text:
		return None

	if " " in text or "T" in text:
		return get_datetime(text)

	return datetime.datetime.combine(date, get_time(text))


# ---------------------------------------------------------------------------
# Timer API
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_active_timer():
	"""The running segment for the logged-in employee, or None.

	The single source of timer state -- called on page load and on every page
	show, so it stays a single indexed read.
	"""
	employee = _require_employee()
	segment = _get_open_segment(employee.name)

	return _segment(segment) if segment else None


@frappe.whitelist()
def start_timer(project, activity_type, task=None):
	"""Open a new work session. Refuses if one is already running."""
	employee = _require_employee()
	project, activity_type, task = _validate_selection(project, activity_type, task)
	_lock_employee(employee)

	running = _get_open_segment(employee.name)
	if running:
		frappe.throw(
			_("A timer has been running on {0} since {1}. Stop it, or use Change Work.").format(
				frappe.bold(running.project), _format_datetime(running.from_time)
			)
		)

	timesheet = _new_timesheet(employee)
	row = _append_segment(timesheet, project, activity_type, task, now_datetime())
	timesheet.insert()

	return _segment(row.as_dict(), timesheet.name)


@frappe.whitelist()
def change_work(project, activity_type, task=None):
	"""Close the running segment and open the next one, in one transaction.

	Both rows live on the same Timesheet, so the switch either happens entirely
	or not at all -- there is no window in which time is lost or two segments
	are open.
	"""
	employee = _require_employee()
	project, activity_type, task = _validate_selection(project, activity_type, task)
	_lock_employee(employee)

	running = _get_open_segment(employee.name)
	if not running:
		frappe.throw(_("No timer is running. Use Start Work Timer instead."))

	timesheet = frappe.get_doc("Timesheet", running.timesheet)

	switched_at = now_datetime()
	_close_row(timesheet, _row_by_name(timesheet, running.row), switched_at)
	row = _append_segment(timesheet, project, activity_type, task, switched_at)
	timesheet.save()

	return _segment(row.as_dict(), timesheet.name)


@frappe.whitelist()
def stop_timer():
	"""Close the running segment and submit the session.

	Submitting is what publishes the hours to Task and Project costing, so the
	result is immediately visible in ERPNext's project reporting.
	"""
	employee = _require_employee()
	_lock_employee(employee)

	running = _get_open_segment(employee.name)
	if not running:
		frappe.throw(_("No timer is running."))

	timesheet = frappe.get_doc("Timesheet", running.timesheet)
	_close_row(timesheet, _row_by_name(timesheet, running.row), now_datetime())

	if not timesheet.time_logs:
		# The whole session was shorter than a moment; leave nothing behind.
		# The Employee role has no delete permission on Timesheet and does not
		# need one -- this removes a draft we created moments ago, in the same
		# request, on behalf of its own employee.
		timesheet.delete(ignore_permissions=True)
		return {}

	timesheet.submit()

	return {"timesheet": timesheet.name}


@frappe.whitelist()
def log_time(project, activity_type, task=None, hours=None, date=None, from_time=None, to_time=None):
	"""Record completed work without a timer.

	Real clock times are recorded whenever the caller knows them:

	* ``from_time`` + ``to_time`` -- both stored as given, duration derived
	* ``from_time`` + ``hours``   -- ``to_time`` derived from the duration
	* ``hours`` alone             -- placed after the day's last entry (see
	  MANUAL_ENTRY_DAY_START), because ERPNext needs a from/to pair to check
	  overlaps at all

	The first form is the one to prefer: it is the only one that records when
	the work actually happened rather than a convention.
	"""
	employee = _require_employee()
	project, activity_type, task = _validate_selection(project, activity_type, task)

	date = getdate(date) if date else getdate(today())
	from_time = _as_datetime(from_time, date)
	to_time = _as_datetime(to_time, date)
	hours = flt(hours)

	if to_time and not from_time:
		frappe.throw(_("Enter a From Time to go with the To Time."))

	if from_time and to_time:
		if to_time <= from_time:
			frappe.throw(_("To Time must be later than From Time."))

		hours = time_diff_in_hours(to_time, from_time)
	elif hours <= 0:
		frappe.throw(_("Enter Hours Worked, or a From Time and a To Time."))
	elif not from_time:
		from_time = _next_free_slot(employee.name, date)

	timesheet = _new_timesheet(employee)
	_append_segment(timesheet, project, activity_type, task, from_time, hours=hours, to_time=to_time)
	timesheet.insert()
	timesheet.submit()

	return {"timesheet": timesheet.name}


def _next_free_slot(employee, date):
	"""Where a manual entry for `date` starts: after the day's last logged time."""
	timesheet = DocType("Timesheet")
	detail = DocType("Timesheet Detail")

	day_start = datetime.datetime.combine(date, datetime.time.min)
	day_end = datetime.datetime.combine(date, datetime.time.max)

	latest = (
		frappe.qb.from_(detail)
		.join(timesheet)
		.on(detail.parent == timesheet.name)
		.select(Max(detail.to_time).as_("to_time"))
		.where(
			(timesheet.employee == employee)
			& (timesheet.docstatus < 2)
			& (detail.parenttype == "Timesheet")
			& (detail.from_time >= day_start)
			& (detail.from_time <= day_end)
		)
	).run(as_dict=True)

	last_to_time = latest[0].to_time if latest else None

	if last_to_time:
		return get_datetime(last_to_time)

	return datetime.datetime.combine(date, MANUAL_ENTRY_DAY_START)


# ---------------------------------------------------------------------------
# Selection lists
#
# The page uses Frappe's built-in Link search for Project, Activity Type and
# Task, so it calls none of these. They exist for callers with no Link control
# -- a mobile client, a report, an integration -- and apply the same filters the
# page does, so both paths agree on what is selectable.
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_projects():
	"""Projects the logged-in employee can log against.

	Scoping beyond this belongs in User Permissions (or in `project_query`), not
	here: `frappe.get_all` already applies them.
	"""
	employee = _require_employee()
	filters = {"status": "Open", "is_active": "Yes"}

	company = _company_for(employee)
	if company:
		filters["company"] = company

	return frappe.get_all(
		"Project",
		filters=filters,
		fields=["name", "project_name", "status", "company"],
		order_by="modified desc",
	)


@frappe.whitelist()
def get_activity_types():
	"""Every Activity Type still in use."""
	return frappe.get_all(
		"Activity Type",
		filters={"disabled": 0},
		fields=["name", "costing_rate", "billing_rate"],
		order_by="name asc",
	)


@frappe.whitelist()
def get_tasks(project):
	"""Tasks belonging to `project` -- and only to `project`."""
	project = (project or "").strip()

	if not project:
		return []

	if not frappe.db.exists("Project", project):
		frappe.throw(_("Project {0} does not exist.").format(frappe.bold(project)))

	return frappe.get_all(
		"Task",
		filters={
			"project": project,
			"status": ["not in", ["Cancelled", "Template"]],
			"is_template": 0,
		},
		fields=["name", "subject", "status", "project"],
		order_by="modified desc",
	)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def project_query(doctype, txt, searchfield, start, page_len, filters):
	"""Link-field search for Project, scoped to the employee's company.

	Wire it up from the page with::

	    get_query: () => ({ query: "checkin_app.api.time_tracking.project_query" })
	"""
	employee = _require_employee()
	company = _company_for(employee)

	conditions = ["p.status = 'Open'", "p.is_active = 'Yes'"]
	values = {"txt": f"%{txt}%", "start": start, "page_len": page_len}

	if company:
		conditions.append("p.company = %(company)s")
		values["company"] = company

	return frappe.db.sql(
		"""
		select p.name, p.project_name
		from `tabProject` p
		where {conditions}
			and (p.name like %(txt)s or p.project_name like %(txt)s)
		order by p.modified desc
		limit %(start)s, %(page_len)s
		""".format(conditions=" and ".join(conditions)),
		values,
	)
