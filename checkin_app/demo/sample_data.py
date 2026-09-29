"""Sample Projects, Tasks and Activity Types for exercising the time tracker.

This is scaffolding for a development site, not a fixture: nothing in the app
imports it and nothing depends on the names below. Run it to get something to
click on, edit it to match real work, or delete the records and forget it.

    bench --site <site> console
    >>> from checkin_app.demo.sample_data import create_sample_data
    >>> create_sample_data()

Every step is idempotent, so re-running it adds only what is missing.
"""

import frappe

ACTIVITY_TYPES = [
	"Development",
	"Testing",
	"Code Review",
	"Design",
	"Meeting",
	"Documentation",
	"Support",
	"Deployment",
]

PROJECTS = {
	"Checkin App": [
		"Time tracking backend",
		"Log Time desk page",
		"Biometric device integration",
		"Employee Checkin automation",
		"QA and regression testing",
	],
	"Marsol Website": [
		"Content migration",
		"Page templates",
		"SEO setup",
		"Launch checklist",
	],
	"ERPNext Implementation": [
		"Requirement gathering",
		"Master data migration",
		"User training",
		"Go-live support",
	],
	"Internal Operations": [
		"Team meetings",
		"Internal documentation",
		"Recruitment support",
	],
}


def create_sample_data(company=None, user=None):
	"""Create the sample records. Returns a summary of what was added."""
	company = company or frappe.defaults.get_global_default("company") or _first("Company")

	if not company:
		frappe.throw("No Company on this site; create one before seeding sample data.")

	created = {
		"activity_types": _create_activity_types(),
		"projects": [],
		"tasks": [],
	}

	for project_name, subjects in PROJECTS.items():
		project = _ensure_project(project_name, company)

		if project["created"]:
			created["projects"].append(project["name"])

		for subject in subjects:
			task = _ensure_task(subject, project["name"])

			if task["created"]:
				created["tasks"].append(f"{project_name} / {subject}")

	if user:
		created["employee"] = ensure_employee(user, company)

	frappe.db.commit()

	return created


def ensure_employee(user, company=None):
	"""Link `user` to an Active Employee, creating one if there is none.

	The time-tracking API resolves the employee from the session user, so a desk
	user with no Employee record cannot use the page at all.
	"""
	existing = frappe.db.get_value("Employee", {"user_id": user}, ["name", "status"], as_dict=True)

	if existing:
		if existing.status != "Active":
			frappe.db.set_value("Employee", existing.name, "status", "Active")

		return existing.name

	company = company or frappe.defaults.get_global_default("company") or _first("Company")
	first_name, last_name = _split_name(user)

	employee = frappe.get_doc(
		{
			"doctype": "Employee",
			"first_name": first_name,
			"last_name": last_name,
			"company": company,
			"status": "Active",
			"gender": _first("Gender") or "Other",
			"date_of_birth": "1995-01-01",
			"date_of_joining": "2024-01-01",
			"user_id": user,
		}
	).insert(ignore_permissions=True)

	return employee.name


def _create_activity_types():
	created = []

	for name in ACTIVITY_TYPES:
		if frappe.db.exists("Activity Type", name):
			continue

		frappe.get_doc({"doctype": "Activity Type", "activity_type": name}).insert(
			ignore_permissions=True
		)
		created.append(name)

	return created


def _ensure_project(project_name, company):
	existing = frappe.db.get_value("Project", {"project_name": project_name}, "name")

	if existing:
		return {"name": existing, "created": False}

	project = frappe.get_doc(
		{
			"doctype": "Project",
			"project_name": project_name,
			"status": "Open",
			"is_active": "Yes",
			"company": company,
		}
	).insert(ignore_permissions=True)

	return {"name": project.name, "created": True}


def _ensure_task(subject, project):
	existing = frappe.db.get_value("Task", {"subject": subject, "project": project}, "name")

	if existing:
		return {"name": existing, "created": False}

	task = frappe.get_doc(
		{"doctype": "Task", "subject": subject, "project": project, "status": "Open"}
	).insert(ignore_permissions=True)

	return {"name": task.name, "created": True}


def _first(doctype):
	names = frappe.get_all(doctype, pluck="name", limit=1)

	return names[0] if names else None


def _split_name(user):
	full_name = frappe.db.get_value("User", user, ["first_name", "last_name"], as_dict=True)

	if full_name and full_name.first_name:
		return full_name.first_name, full_name.last_name or ""

	local_part = user.split("@")[0].replace(".", " ").replace("_", " ").title()
	parts = local_part.split(" ", 1)

	return parts[0], (parts[1] if len(parts) > 1 else "")
