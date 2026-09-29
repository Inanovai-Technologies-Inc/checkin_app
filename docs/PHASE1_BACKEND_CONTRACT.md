# Phase 1 — Backend contract for "Log Time for a Project"

The desk page `log-time-for-a-project` is implemented and working. It contains **no
Timesheet logic**. This document specifies the five whitelisted methods it calls, so the
backend can be built against a fixed interface.

**Page source:** `checkin_app/checkin_app/page/log_time_for_a_project/`
(`.js`, `.html`, `.css`, `.json` — no Python).

**URL:** `/app/log-time-for-a-project`

---

## 1. What the UI already does without you

Project, Activity Type and Task selection use Frappe's built-in Link controls, which go
through the standard `frappe.desk.search.search_link` endpoint. **No endpoint is needed
for these** — do not write `get_projects()`, `get_activity_types()` or `get_tasks()`.

| Selector | Doctype | Filter applied by the UI |
|---|---|---|
| Project | `Project` | `status = "Open"`, `is_active = "Yes"` |
| Activity Type | `Activity Type` | none |
| Task | `Task` | `project = <selected project>`; `""` when no project is chosen, so the list is empty rather than unfiltered |

All three are stock ERPNext DocTypes. No custom Project/Task/Activity Type/Timesheet
DocType is created, and none should be.

**If you need per-employee project scoping** (requirement: "only projects the employee can
reasonably work on"), the cleanest hook is a whitelisted search query rather than a new
endpoint. Tell me the dotted path and I will switch the Project control's `get_query` to:

```js
get_query: () => ({ query: "checkin_app.api.time_tracking.project_query" })
```

Until then the status/is_active filter above is what ships.

---

## 2. Methods the UI calls

All five live at `checkin_app.api.time_tracking.*` and must be decorated with
`@frappe.whitelist()`. The employee is resolved **server-side** from `frappe.session.user`
— the UI never sends an employee ID.

### The `segment` object

Four of the five methods return the currently-running segment in this shape. Every field
is read by the UI; `null` is acceptable for the optional ones.

```jsonc
{
  "project":       "PROJ-0001",              // required — Project.name
  "project_name":  "Marsol",                 // display label; falls back to project
  "activity_type": "Execution",              // required — Activity Type.name
  "task":          "TASK-2026-00001",        // may be null
  "task_subject":  "Product Details",        // display label; falls back to task
  "start_time":    "2026-09-29 09:15:00"     // required — see note below
}
```

**`start_time` must be `"YYYY-MM-DD HH:mm:ss"` in the site's system timezone**, i.e. exactly
what `frappe.utils.now_datetime()` produces. The UI computes elapsed time as
`now_datetime() - start_time` using the same conversion on both sides, so anything else
(ISO-8601 with `T`, a UTC value, an epoch number) will render a wrong duration.

---

### `get_active_timer()`

Called on page load and again on every page show (returning to the tab, another device
stopping the timer).

- **Args:** none
- **Returns:** a `segment`, or `null` when nothing is running.

This is the **only** source of timer state. The page keeps nothing across reloads — no
localStorage, no client-side timer record. Whatever you return here is what the employee
sees, so a timer started on their phone must appear on their laptop.

---

### `start_timer(project, activity_type, task)`

- **Args:** `project` (str, required), `activity_type` (str, required), `task` (str or `null`)
- **Returns:** the new `segment`.

Called only when no timer is running — the button is unreachable otherwise. If you find an
existing open segment, treat it as a conflict and raise; do not silently close it.

---

### `change_work(project, activity_type, task)`

- **Args:** identical to `start_timer`
- **Returns:** the **new** `segment`.

Closes the current segment and opens the next one. **This must be a single atomic call.**
The UI deliberately does not call `stop_timer` followed by `start_timer` — a failure
between the two would lose time or leave two open segments.

The employee can open the Change Work panel and then cancel; that is purely a UI state and
reaches no endpoint. You will only ever see `change_work` after they confirm.

---

### `stop_timer()`

- **Args:** none
- **Returns:** `{ "timesheet": "TS-2026-00001" }` — or `{}` / `null` if you have no name to give.

Closes the open segment. When a `timesheet` key is present the UI shows a clickable toast
linking to `/app/timesheet/<name>`, which is how the employee verifies the save; returning
it is worth the effort.

---

### `log_time(project, activity_type, task, hours, date)`

The manual path behind **Submit Time**. No timer involved.

- **Args:** the three selection fields, plus `hours` (float, always > 0 — the UI blocks
  zero and negatives) and `date` (`"YYYY-MM-DD"`)
- **Returns:** `{ "timesheet": "..." }`, same as `stop_timer`.

---

## 3. Error handling

Raise `frappe.throw()` with a readable message. Frappe's own dialog shows it; the UI adds a
short toast naming which action failed and **leaves the timer untouched** — a failed
`start_timer` does not start a display timer, a failed `stop_timer` keeps the card on screen.

One thing to know about: until these methods exist, Frappe answers them with **HTTP 417**
and `"Failed to get method for command checkin_app.api.time_tracking.<x>"`. The UI detects
exactly that signature and shows an amber "backend not available yet" banner, running the
timer for display only so the page stays demoable. It matches on the 417 status **and** that
specific message, so your own `frappe.throw("... does not exist")` will surface as a normal
error rather than being mistaken for an absent backend. **The banner disappears on its own
the moment the methods land — no UI change needed.**

---

## 4. Please don't

- Don't add a custom timer DocType. If you need somewhere to hold the open segment, use the
  existing `Timesheet` / `Timesheet Detail` (`from_time` set, `to_time` empty) rather than a
  parallel model.
- Don't duplicate `Project`, `Task`, `Activity Type` or `Timesheet`.
- Don't return `employee` to the UI; it doesn't use it and shouldn't depend on it.

## 5. Out of scope for Phase 1

Biometric integration, static IP checks, Employee Checkin automation, WhatsApp integration,
production deployment.
