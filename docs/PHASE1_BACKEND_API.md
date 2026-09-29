# Phase 1 — Time tracking backend

Implements the interface specified in [PHASE1_BACKEND_CONTRACT.md](PHASE1_BACKEND_CONTRACT.md).
All five methods the page calls exist and are whitelisted, so the page's amber
*"backend not available yet"* banner no longer appears.

**The page has one addition:** From Time and To Time controls on the manual-entry row, so a
manual log records when the work happened instead of a placeholder slot (§6). The change is
additive — `log_time` still accepts the original `hours` + `date` call unchanged, and the
timer path is untouched.

---

## 1. How time is stored

No new DocType. A work session is a stock ERPNext **Timesheet**, and each segment of that
session is a row in its **Timesheet Detail** child table:

```
Timesheet TS-2026-00001  (Draft while the timer runs)
  ├─ Timesheet Detail   09:00 → 10:30   Project A / Execution / Task A   1.5 h
  └─ Timesheet Detail   10:30 →         Project B / Planning  / Task C   ← open
```

The **open segment is the Timesheet Detail row with an empty `to_time` inside a Draft
Timesheet**. That one fact is the entire timer state. There is no timer DocType, no cache
and no client-side record — which is why a timer started on a phone shows up on a laptop.

`stop_timer` closes the last row and **submits** the Timesheet. Submitting is the point:
ERPNext rolls hours and costs into Task and Project from submitted rows only
(`Task.update_time_and_costing` filters on `docstatus = 1`), so an unsubmitted timesheet
would never appear in project costing.

`change_work` closes the current row and appends the next one **on the same Timesheet in a
single save**, so a switch either happens completely or not at all. One start→stop session
therefore produces exactly one submitted Timesheet, whatever happened in between.

---

## 2. Files

| File | Status | What it is |
|---|---|---|
| `checkin_app/api/time_tracking.py` | new | the whole backend — 9 whitelisted methods |
| `checkin_app/tests/test_time_tracking.py` | new | 19 tests covering scenarios A–E and more |
| `checkin_app/patches/v1_0/grant_timesheet_submit_to_employee.py` | new | lets the Employee role submit Timesheets |
| `checkin_app/patches.txt` | modified | registers that patch |
| `checkin_app/demo/sample_data.py` | new | sample Projects/Tasks/Activity Types for a dev site |
| `.../page/log_time_for_a_project.js` | modified | From Time / To Time controls |
| `.../page/log_time_for_a_project.html` | modified | slots for those two controls |
| `checkin_app/api/__init__.py`, `tests/__init__.py`, `demo/__init__.py`, `patches/v1_0/__init__.py` | new | package markers |
| `docs/PHASE1_BACKEND_API.md` | new | this document |

**No DocType or schema change.** The one permission change ships as a patch, so
`bench --site <site> migrate` applies it on every site that installs the app.

---

## 3. API

Everything lives at `checkin_app.api.time_tracking.*` and is decorated with
`@frappe.whitelist()`. The employee is resolved server-side from `frappe.session.user`;
**no method accepts an employee from the caller.**

### The `segment` object

```jsonc
{
  "project":       "PROJ-0001",
  "project_name":  "Marsol",                 // falls back to project
  "activity_type": "Execution",
  "task":          "TASK-2026-00001",        // null when none was chosen
  "task_subject":  "Product Details",        // null when task is null
  "start_time":    "2026-09-29 09:15:00",    // site timezone, second precision
  "timesheet":     "TS-2026-00001"           // extra, informational — ignore it freely
}
```

`start_time` is exactly `"YYYY-MM-DD HH:mm:ss"` in the site's timezone, as the contract
requires. `timesheet` is an addition the contract does not mention; it is there so a caller
can link to the session in progress. The page does not read it and does not have to.

### Methods the page calls

| Method | Args | Returns |
|---|---|---|
| `get_active_timer()` | — | `segment` or `null` |
| `start_timer(project, activity_type, task)` | `task` optional | the new `segment` |
| `change_work(project, activity_type, task)` | `task` optional | the **new** `segment` |
| `stop_timer()` | — | `{"timesheet": "TS-2026-00001"}`, or `{}` (see below) |
| `log_time(project, activity_type, task, hours, date, from_time, to_time)` | see below | `{"timesheet": "..."}` |

`stop_timer` returns `{}` in one case: the session was closed in the same instant it
started, so there were no hours to record and the empty draft was removed rather than left
behind. The page already handles a result with no `timesheet` key — it shows the plain
"Timer stopped" toast without a link.

### Methods the page does not call

The contract says not to write `get_projects` / `get_activity_types` / `get_tasks`, because
the page's Link controls already go through `frappe.desk.search.search_link`. That still
holds and **the page should keep using its Link controls.** These exist for callers that
have no Link control — a mobile client, a report, an integration — and they apply the same
filters the page does, so both paths agree on what is selectable.

| Method | Args | Returns |
|---|---|---|
| `get_projects()` | — | `[{name, project_name, status, company}]` — Open + Active, employee's company |
| `get_activity_types()` | — | `[{name, costing_rate, billing_rate}]` — not disabled |
| `get_tasks(project)` | `project` | `[{name, subject, status, project}]` — that project only; `[]` for an empty project |
| `project_query(...)` | Frappe search-query signature | Link-field search, scoped to the employee's company |

`project_query` is the hook the contract offered for per-employee project scoping. To use
it, change the Project control in `log_time_for_a_project.js`:

```js
get_query: () => ({ query: "checkin_app.api.time_tracking.project_query" })
```

Scoping beyond company is better done with **User Permissions** on Project than by editing
this function — `get_projects` already applies them.

---

## 4. What the backend refuses

Every rejection is a `frappe.throw()` with a readable message, which Frappe surfaces in its
own dialog. The page adds its short toast and, as the contract requires, leaves the timer
display untouched.

| Situation | Message |
|---|---|
| Timer already running, `start_timer` called | *A timer has been running on X since …. Stop it, or use Change Work.* |
| `stop_timer` / `change_work` with nothing running | *No timer is running…* |
| Task belongs to another project | *Task X belongs to Project A, not Project B.* |
| Project / Activity Type / Task does not exist | *… does not exist.* |
| No Project or no Activity Type | *Select a Project before timing work.* |
| `log_time` with `hours <= 0` | *Hours Worked must be greater than zero.* |
| Session user has no Employee record | *No active Employee record is linked to …* |

Two active timers for one employee cannot arise: `start_timer` looks for an open segment
first and refuses, and `change_work` closes the old row and opens the new one in the same
save. The three methods that move a segment take a `for update` lock on the Employee row
first, so two requests racing — Start tapped on a phone and a laptop at once — are
serialised and the second one sees the first one's segment instead of opening its own.
ERPNext's `validate_overlap` runs on top of all this and independently rejects rows that
overlap in time for the same employee.

---

## 5. Authentication and permissions

`_require_employee()` maps `frappe.session.user` to an **Active** Employee record via
`Employee.user_id`, and throws if there is none. Every read and write is scoped to that
employee. Nothing accepts an employee ID from the client.

ERPNext ships the Employee role with **`submit = 0`** on Timesheet, which would stop an
employee submitting their own time. `checkin_app/patches/v1_0/grant_timesheet_submit_to_employee.py`
grants it, so **the permission system enforces these writes rather than the API bypassing
them** — no `ignore_permissions` on insert, save or submit. Verified with a user whose only
role is Employee: `has_permission("Timesheet", "submit")` is `True` and a full
start → change → stop cycle produces a submitted Timesheet.

The patch leaves `cancel` and `delete` at 0. An employee can therefore submit time but not
unwind it, which means **a mis-stopped timer needs someone with Projects User or HR User to
cancel and amend it.** Grant `cancel` as well if self-service correction matters more than
the audit trail.

One `ignore_permissions` remains, on the delete in `stop_timer`: it removes a draft created
seconds earlier in the same request when a session turned out to have no duration. Granting
the Employee role `delete` on Timesheet to cover that would give away much more than it
buys.

**A user with no Employee record cannot use the page at all** — the API resolves the
employee from the session and has nowhere to file the time otherwise. Administrator has no
Employee record on a stock site.

---

## 6. Manual entries and clock times

The page now has **From Time** and **To Time** controls next to Hours Worked, so a manual
entry records when the work actually happened. `log_time` accepts three shapes:

| What the caller sends | What is stored |
|---|---|
| `from_time` + `to_time` | both exactly as given; `hours` is derived from them |
| `from_time` + `hours` | `to_time` derived from the duration |
| `hours` alone | placed after the day's last entry, starting at 09:00 |

`from_time` and `to_time` may be a time of day (`"14:15:00"`, read against `date`) or a
full datetime (`"2026-09-28 14:15:00"`). The page sends times of day, because it already
has a Date field.

**Clock times win over a contradictory `hours`.** Sending `09:00`–`10:00` with `hours: 99`
stores 1 hour; the times are the record, `hours` is derived. `to_time` earlier than
`from_time` is refused, as is a `to_time` with no `from_time`.

The third row is the fallback for callers that only know a duration — the timer path never
uses it. ERPNext needs a from/to pair to check overlaps at all, so without a placement rule
every manual entry for a day would land on the same instant and the second one would be
rejected as an overlap. `MANUAL_ENTRY_DAY_START` (09:00) is where the first one goes; two
entries of 2 h and 1 h become 09:00–11:00 and 11:00–12:00. In that shape the times are a
convention and only the durations are meaningful — which is exactly why the From/To fields
now exist.

In the page, filling both times fills Hours Worked so the employee can see what will be
logged. That figure is a preview: the backend derives the duration again rather than
trusting it.

---

## 7. Tests

`checkin_app/tests/test_time_tracking.py` — 19 tests, all passing. The module clock is
patched so durations are exact rather than however long the test run took.

```
$ bench --site checkin.local run-tests --module checkin_app.tests.test_time_tracking
```

`bench` is not on `PATH` in this bench directory, so the recorded run used the venv
directly — same tests, same site:

```
$ cd ~/frappe-bench-v15/sites
$ ../env/bin/python -c "import frappe, unittest, sys; \
    frappe.init(site='checkin.local'); frappe.connect(); frappe.flags.in_test = True; \
    r = unittest.TextTestRunner(verbosity=2).run( \
        unittest.TestLoader().loadTestsFromName('checkin_app.tests.test_time_tracking')); \
    frappe.db.rollback(); sys.exit(0 if r.wasSuccessful() else 1)"
```

Result against `checkin.local`:

```
test_a_start_and_stop_logs_the_elapsed_time ... ok
test_b_change_task_keeps_the_earlier_segment ... ok
test_c_change_project_keeps_both_segments ... ok
test_d_second_start_is_refused ... ok
test_e_task_from_another_project_is_refused ... ok
test_f_stop_without_a_timer_is_refused ... ok
test_g_change_without_a_timer_is_refused ... ok
test_h_unknown_project_and_activity_are_refused ... ok
test_i_timer_without_a_task_is_allowed ... ok
test_j_log_time_submits_a_standalone_timesheet ... ok
test_k_log_time_rejects_non_positive_hours ... ok
test_k1_log_time_records_the_clock_times_it_is_given ... ok
test_k2_clock_times_win_over_a_contradictory_hours_value ... ok
test_k3_from_time_with_hours_derives_the_to_time ... ok
test_k4_full_datetimes_are_accepted_too ... ok
test_k5_backwards_clock_times_are_refused ... ok
test_k6_a_to_time_without_a_from_time_is_refused ... ok
test_l_get_tasks_is_scoped_to_its_project ... ok
test_m_get_projects_and_activity_types ... ok

Ran 19 tests in 3.193s

OK
```

Mapped to the requested scenarios:

| Scenario | Test | What it proves |
|---|---|---|
| A — start → stop | `test_a` | 09:00→10:30 logs 1.5 h on a submitted Timesheet, and `Task.actual_time` gains 1.5 |
| B — change task, same project | `test_b` | two rows, 1.5 h each, first row's `to_time` == second row's `from_time` — no gap, no overlap, no lost time |
| C — change project, activity and task | `test_c` | two rows on one Timesheet carrying different projects and activity types |
| D — second start while active | `test_d` | raises; the original segment is still the only one and still open |
| E — task from another project | `test_e` | raises *"belongs to Project"*; no timer is opened |

The remaining tests cover stop/change with no timer, unknown or blank
Project/Activity Type, a timer with no task, the six `log_time` shapes (clock times, times
beating a contradictory `hours`, derived `to_time`, full datetimes, backwards times, a
`to_time` with no `from_time`) plus same-day stacking, and that `get_tasks` never returns
another project's tasks.

Fixtures (a user, an employee, two projects, three tasks) are created inside the test
transaction and rolled back — the run leaves the site exactly as it found it, which was
verified afterwards.

### Sample data

`checkin_app/demo/sample_data.py` seeds a development site with something to click on:

```
bench --site <site> console
>>> from checkin_app.demo.sample_data import create_sample_data
>>> create_sample_data(user="someone@example.com")   # user is optional
```

It adds 8 Activity Types (Development, Testing, Code Review, Design, Meeting,
Documentation, Support, Deployment) alongside ERPNext's stock five, four Projects with 16
Tasks between them, and — when given a `user` — an Active Employee linked to it. Every step
is idempotent and existing records are left alone. It is scaffolding, not a fixture:
nothing in the app imports it.

---

## 8. Not in Phase 1

Biometric device integration, static IP checks, Employee Checkin automation, WhatsApp
integration, attendance logic and production deployment. None of them are referenced by
this code.
