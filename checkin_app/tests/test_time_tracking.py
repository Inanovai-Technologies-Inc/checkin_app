"""Tests for the Phase 1 time-tracking backend.

The module clock (`time_tracking.now_datetime`) is patched throughout so that
durations are exact rather than whatever the test run happened to take. All
fixture dates sit in the past, which keeps them clear of ERPNext's overlap
validation: an open segment is treated as running until *now*, so timing
fixtures around the real clock would make these tests flaky.
"""

import datetime
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from checkin_app.api import time_tracking

TEST_USER = "time-tracker@checkin-app.test"


def at(day, hour, minute=0):
	return datetime.datetime(2025, 1, day, hour, minute, 0)


class TestTimeTracking(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()

		cls.company = frappe.get_all("Company", pluck="name")[0]
		cls.activity_a, cls.activity_b = frappe.get_all("Activity Type", pluck="name", limit=2)

		cls.user = cls._make_user()
		cls.employee = cls._make_employee()
		cls.project_a = cls._make_project("Time Tracker Project A")
		cls.project_b = cls._make_project("Time Tracker Project B")
		cls.task_a = cls._make_task("Task A", cls.project_a)
		cls.task_b = cls._make_task("Task B", cls.project_a)
		cls.task_c = cls._make_task("Task C", cls.project_b)

	@classmethod
	def _make_user(cls):
		if frappe.db.exists("User", TEST_USER):
			return TEST_USER

		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": TEST_USER,
				"first_name": "Time",
				"last_name": "Tracker",
				"send_welcome_email": 0,
			}
		)
		user.append("roles", {"role": "Projects User"})
		user.insert(ignore_permissions=True)

		return user.name

	@classmethod
	def _make_employee(cls):
		employee = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": "Time",
				"last_name": "Tracker",
				"company": cls.company,
				"status": "Active",
				"gender": frappe.get_all("Gender", pluck="name")[0],
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"user_id": cls.user,
			}
		).insert(ignore_permissions=True)

		return employee.name

	@classmethod
	def _make_project(cls, title):
		return frappe.get_doc(
			{
				"doctype": "Project",
				"project_name": title,
				"status": "Open",
				"is_active": "Yes",
				"company": cls.company,
			}
		).insert(ignore_permissions=True).name

	@classmethod
	def _make_task(cls, subject, project):
		return frappe.get_doc(
			{"doctype": "Task", "subject": subject, "project": project, "status": "Open"}
		).insert(ignore_permissions=True).name

	# -- per-test isolation ------------------------------------------------
	#
	# FrappeTestCase rolls back once per class, so a test that deliberately
	# leaves a timer running would otherwise block the next one.

	def setUp(self):
		frappe.set_user("Administrator")

		for name in frappe.get_all(
			"Timesheet", filters={"employee": self.employee, "docstatus": 0}, pluck="name"
		):
			frappe.delete_doc("Timesheet", name, force=True, ignore_permissions=True)

		frappe.set_user(self.user)

	def tearDown(self):
		frappe.set_user("Administrator")

	# -- helpers -----------------------------------------------------------

	def call(self, fn, when, *args, **kwargs):
		"""Invoke an API function with the module clock pinned to `when`."""
		with patch.object(time_tracking, "now_datetime", return_value=when):
			return fn(*args, **kwargs)

	def segments(self, timesheet):
		return frappe.get_doc("Timesheet", timesheet).time_logs

	def actual_time(self, task):
		"""Hours ERPNext has rolled up onto a Task.

		Read as a delta across an action: rollback is per class, so tasks carry
		whatever earlier tests logged against them.
		"""
		return flt(frappe.db.get_value("Task", task, "actual_time"))

	# -- A: start -> stop --------------------------------------------------

	def test_a_start_and_stop_logs_the_elapsed_time(self):
		before = self.actual_time(self.task_a)

		segment = self.call(
			time_tracking.start_timer, at(6, 9), self.project_a, self.activity_a, self.task_a
		)

		self.assertEqual(segment["project"], self.project_a)
		self.assertEqual(segment["activity_type"], self.activity_a)
		self.assertEqual(segment["task"], self.task_a)
		self.assertEqual(segment["task_subject"], "Task A")
		self.assertEqual(segment["start_time"], "2025-01-06 09:00:00")

		# While it runs it is the one and only source of truth.
		running = time_tracking.get_active_timer()
		self.assertEqual(running["start_time"], "2025-01-06 09:00:00")
		self.assertEqual(running["timesheet"], segment["timesheet"])

		result = self.call(time_tracking.stop_timer, at(6, 10, 30))
		timesheet = frappe.get_doc("Timesheet", result["timesheet"])

		self.assertEqual(timesheet.docstatus, 1, "stopping must submit, or costing never sees it")
		self.assertEqual(len(timesheet.time_logs), 1)
		self.assertEqual(timesheet.total_hours, 1.5)
		self.assertEqual(str(timesheet.time_logs[0].to_time), "2025-01-06 10:30:00")

		self.assertIsNone(time_tracking.get_active_timer())

		# Requirement 7: the hours must reach ERPNext project costing.
		self.assertEqual(self.actual_time(self.task_a) - before, 1.5)

	# -- B: change task within the same project ----------------------------

	def test_b_change_task_keeps_the_earlier_segment(self):
		before_a, before_b = self.actual_time(self.task_a), self.actual_time(self.task_b)

		self.call(time_tracking.start_timer, at(7, 9), self.project_a, self.activity_a, self.task_a)

		switched = self.call(
			time_tracking.change_work, at(7, 10, 30), self.project_a, self.activity_a, self.task_b
		)
		self.assertEqual(switched["task"], self.task_b)
		self.assertEqual(switched["start_time"], "2025-01-07 10:30:00")

		result = self.call(time_tracking.stop_timer, at(7, 12))
		rows = self.segments(result["timesheet"])

		self.assertEqual(len(rows), 2, "the first segment must survive the switch")
		self.assertEqual((rows[0].task, rows[0].hours), (self.task_a, 1.5))
		self.assertEqual((rows[1].task, rows[1].hours), (self.task_b, 1.5))
		self.assertEqual(str(rows[0].to_time), str(rows[1].from_time), "no gap, no overlap")

		self.assertEqual(self.actual_time(self.task_a) - before_a, 1.5)
		self.assertEqual(self.actual_time(self.task_b) - before_b, 1.5)

	# -- C: change project, activity and task ------------------------------

	def test_c_change_project_keeps_both_segments(self):
		self.call(time_tracking.start_timer, at(8, 9), self.project_a, self.activity_a, self.task_a)

		switched = self.call(
			time_tracking.change_work, at(8, 10, 30), self.project_b, self.activity_b, self.task_c
		)
		self.assertEqual(switched["project"], self.project_b)
		self.assertEqual(switched["activity_type"], self.activity_b)

		result = self.call(time_tracking.stop_timer, at(8, 12))
		rows = self.segments(result["timesheet"])

		self.assertEqual(len(rows), 2)
		self.assertEqual(
			(rows[0].project, rows[0].activity_type, rows[0].task, rows[0].hours),
			(self.project_a, self.activity_a, self.task_a, 1.5),
		)
		self.assertEqual(
			(rows[1].project, rows[1].activity_type, rows[1].task, rows[1].hours),
			(self.project_b, self.activity_b, self.task_c, 1.5),
		)

	# -- D: second timer while one is active -------------------------------

	def test_d_second_start_is_refused(self):
		self.call(time_tracking.start_timer, at(9, 9), self.project_a, self.activity_a, self.task_a)

		with self.assertRaises(frappe.ValidationError) as caught:
			self.call(
				time_tracking.start_timer, at(9, 9, 30), self.project_b, self.activity_b, self.task_c
			)

		self.assertIn("Change Work", str(caught.exception))

		# The original segment is untouched -- still exactly one, still open.
		running = time_tracking.get_active_timer()
		self.assertEqual(running["start_time"], "2025-01-09 09:00:00")
		self.assertEqual(running["project"], self.project_a)

	# -- E: task from another project --------------------------------------

	def test_e_task_from_another_project_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self.call(
				time_tracking.start_timer, at(10, 9), self.project_a, self.activity_a, self.task_c
			)

		self.assertIn("belongs to Project", str(caught.exception))
		self.assertIsNone(time_tracking.get_active_timer(), "a rejected start must not open a timer")

	# -- remaining integrity rules -----------------------------------------

	def test_f_stop_without_a_timer_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			time_tracking.stop_timer()

		self.assertIn("No timer is running", str(caught.exception))

	def test_g_change_without_a_timer_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			time_tracking.change_work(self.project_a, self.activity_a, None)

		self.assertIn("Start Work Timer", str(caught.exception))

	def test_h_unknown_project_and_activity_are_refused(self):
		with self.assertRaises(frappe.ValidationError):
			time_tracking.start_timer("NO-SUCH-PROJECT", self.activity_a, None)

		with self.assertRaises(frappe.ValidationError):
			time_tracking.start_timer(self.project_a, "No Such Activity", None)

		with self.assertRaises(frappe.ValidationError):
			time_tracking.start_timer("", self.activity_a, None)

	def test_i_timer_without_a_task_is_allowed(self):
		segment = self.call(time_tracking.start_timer, at(13, 9), self.project_a, self.activity_a, None)

		self.assertIsNone(segment["task"])
		self.assertIsNone(segment["task_subject"])

		result = self.call(time_tracking.stop_timer, at(13, 10))
		rows = self.segments(result["timesheet"])

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].hours, 1.0)
		self.assertIsNone(rows[0].task)

	# -- manual entry ------------------------------------------------------

	def test_j_log_time_submits_a_standalone_timesheet(self):
		result = time_tracking.log_time(
			self.project_a, self.activity_a, self.task_a, hours=2, date="2025-01-14"
		)

		timesheet = frappe.get_doc("Timesheet", result["timesheet"])

		self.assertEqual(timesheet.docstatus, 1)
		self.assertEqual(timesheet.total_hours, 2.0)
		self.assertEqual(str(timesheet.time_logs[0].from_time), "2025-01-14 09:00:00")
		self.assertEqual(str(timesheet.time_logs[0].to_time), "2025-01-14 11:00:00")

		# A second entry the same day stacks after the first rather than
		# colliding with it.
		second = time_tracking.log_time(
			self.project_b, self.activity_b, self.task_c, hours=1, date="2025-01-14"
		)
		row = frappe.get_doc("Timesheet", second["timesheet"]).time_logs[0]

		self.assertEqual(str(row.from_time), "2025-01-14 11:00:00")
		self.assertEqual(str(row.to_time), "2025-01-14 12:00:00")

	def test_k_log_time_rejects_non_positive_hours(self):
		with self.assertRaises(frappe.ValidationError):
			time_tracking.log_time(self.project_a, self.activity_a, None, hours=0, date="2025-01-15")

	def test_k1_log_time_records_the_clock_times_it_is_given(self):
		result = time_tracking.log_time(
			self.project_a,
			self.activity_a,
			self.task_a,
			date="2025-01-16",
			from_time="14:15:00",
			to_time="16:45:00",
		)
		row = frappe.get_doc("Timesheet", result["timesheet"]).time_logs[0]

		self.assertEqual(str(row.from_time), "2025-01-16 14:15:00")
		self.assertEqual(str(row.to_time), "2025-01-16 16:45:00")
		self.assertEqual(row.hours, 2.5, "duration comes from the times, not the caller")

	def test_k2_clock_times_win_over_a_contradictory_hours_value(self):
		result = time_tracking.log_time(
			self.project_a,
			self.activity_a,
			None,
			hours=99,
			date="2025-01-17",
			from_time="09:00:00",
			to_time="10:00:00",
		)

		self.assertEqual(frappe.get_doc("Timesheet", result["timesheet"]).total_hours, 1.0)

	def test_k3_from_time_with_hours_derives_the_to_time(self):
		result = time_tracking.log_time(
			self.project_a, self.activity_a, None, hours=1.5, date="2025-01-18", from_time="13:00:00"
		)
		row = frappe.get_doc("Timesheet", result["timesheet"]).time_logs[0]

		self.assertEqual(str(row.from_time), "2025-01-18 13:00:00")
		self.assertEqual(str(row.to_time), "2025-01-18 14:30:00")

	def test_k4_full_datetimes_are_accepted_too(self):
		result = time_tracking.log_time(
			self.project_a,
			self.activity_a,
			None,
			date="2025-01-19",
			from_time="2025-01-19 08:00:00",
			to_time="2025-01-19 08:30:00",
		)
		row = frappe.get_doc("Timesheet", result["timesheet"]).time_logs[0]

		self.assertEqual(str(row.from_time), "2025-01-19 08:00:00")
		self.assertEqual(row.hours, 0.5)

	def test_k5_backwards_clock_times_are_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			time_tracking.log_time(
				self.project_a,
				self.activity_a,
				None,
				date="2025-01-20",
				from_time="17:00:00",
				to_time="09:00:00",
			)

		self.assertIn("later than From Time", str(caught.exception))

	def test_k6_a_to_time_without_a_from_time_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			time_tracking.log_time(
				self.project_a, self.activity_a, None, date="2025-01-21", to_time="17:00:00"
			)

		self.assertIn("From Time", str(caught.exception))

	# -- selection lists ---------------------------------------------------

	def test_l_get_tasks_is_scoped_to_its_project(self):
		names = [row["name"] for row in time_tracking.get_tasks(self.project_a)]

		self.assertCountEqual(names, [self.task_a, self.task_b])
		self.assertNotIn(self.task_c, names)

		self.assertEqual([row["name"] for row in time_tracking.get_tasks(self.project_b)], [self.task_c])
		self.assertEqual(time_tracking.get_tasks(""), [])

	def test_m_get_projects_and_activity_types(self):
		projects = [row["name"] for row in time_tracking.get_projects()]

		self.assertIn(self.project_a, projects)
		self.assertIn(self.project_b, projects)

		self.assertIn(self.activity_a, [row["name"] for row in time_tracking.get_activity_types()])
