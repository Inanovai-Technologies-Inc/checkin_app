// Log Time for a Project — Phase 1 (UI only).
//
// This page owns presentation and user flow ONLY. All Timesheet business logic
// lives behind the whitelisted methods listed in BACKEND below, which are
// implemented separately. See docs/PHASE1_BACKEND_CONTRACT.md.
//
// Reads (Project / Activity Type / Task lists) deliberately use Frappe's built-in
// Link control search, so no custom backend endpoint is needed for selection.

frappe.provide("checkin_app.time_tracking");

// Whitelisted methods this UI expects the backend to expose.
const BACKEND = {
	get_active_timer: "checkin_app.api.time_tracking.get_active_timer",
	start_timer: "checkin_app.api.time_tracking.start_timer",
	change_work: "checkin_app.api.time_tracking.change_work",
	stop_timer: "checkin_app.api.time_tracking.stop_timer",
	log_time: "checkin_app.api.time_tracking.log_time",
};

frappe.pages["log-time-for-a-project"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Log Time for a Project"),
		single_column: true,
	});

	wrapper.time_tracker = new checkin_app.time_tracking.TimeTracker(page);
};

frappe.pages["log-time-for-a-project"].on_page_show = function (wrapper) {
	// Another tab or the Timesheet itself may have changed the active segment.
	wrapper.time_tracker && wrapper.time_tracker.refresh_from_backend();
};

checkin_app.time_tracking.TimeTracker = class TimeTracker {
	constructor(page) {
		this.page = page;

		// mode: "idle" | "running" | "changing"
		this.mode = "idle";

		// The active segment as reported by the backend. Never invented locally
		// except in degraded mode, which is labelled in the UI.
		this.timer = null;

		// True once a backend call has failed with "method not found", meaning
		// the Phase 1 backend is not deployed yet.
		this.degraded = false;

		this.tick_handle = null;

		this.make();
		this.refresh_from_backend();
	}

	make() {
		$(frappe.render_template("log_time_for_a_project", {})).appendTo(this.page.main);

		this.$root = this.page.main.find(".ltp-root");
		this.$active = this.$root.find(".ltp-active");
		this.$selection = this.$root.find(".ltp-selection");
		this.$degraded = this.$root.find(".ltp-degraded");

		this.make_fields();
		this.bind_events();
		this.render();
	}

	// ---------------------------------------------------------------
	// Controls
	// ---------------------------------------------------------------

	make_fields() {
		const me = this;

		// Project — the top of the dependency chain.
		// Scoped to projects that are open and active, i.e. ones an employee
		// could reasonably book time against.
		this.project_field = frappe.ui.form.make_control({
			parent: this.$root.find('[data-field="project"]'),
			render_input: true,
			df: {
				fieldname: "project",
				label: __("Project"),
				fieldtype: "Link",
				options: "Project",
				reqd: 1,
				placeholder: __("Select a project"),
				get_query: () => ({
					filters: {
						status: "Open",
						is_active: "Yes",
					},
				}),
				onchange: () => me.on_project_change(),
			},
		});

		// Activity Type — second in the chain.
		this.activity_type_field = frappe.ui.form.make_control({
			parent: this.$root.find('[data-field="activity_type"]'),
			render_input: true,
			df: {
				fieldname: "activity_type",
				label: __("Activity Type"),
				fieldtype: "Link",
				options: "Activity Type",
				reqd: 1,
				placeholder: __("Select an activity type"),
				onchange: () => me.update_action_state(),
			},
		});

		// Task — depends on the selected Project. The filter below is what keeps
		// unrelated tasks out of the dropdown.
		this.task_field = frappe.ui.form.make_control({
			parent: this.$root.find('[data-field="task"]'),
			render_input: true,
			df: {
				fieldname: "task",
				label: __("Task"),
				fieldtype: "Link",
				options: "Task",
				placeholder: __("Select a project first"),
				get_query: () => ({
					filters: {
						// Empty string matches nothing, so an unset Project yields
						// an empty list rather than every Task in the system.
						project: me.get_value("project") || "",
					},
				}),
				onchange: () => me.update_action_state(),
			},
		});

		this.date_field = frappe.ui.form.make_control({
			parent: this.$root.find('[data-field="date"]'),
			render_input: true,
			df: {
				fieldname: "date",
				label: __("Date"),
				fieldtype: "Date",
				reqd: 1,
				onchange: () => me.update_action_state(),
			},
		});
		this.date_field.set_value(frappe.datetime.get_today());

		// From / To Time record when the work actually happened. Filling both
		// derives Hours Worked; leaving them empty falls back to Hours alone,
		// which the backend places after the day's last entry.
		this.from_time_field = frappe.ui.form.make_control({
			parent: this.$root.find('[data-field="from_time"]'),
			render_input: true,
			df: {
				fieldname: "from_time",
				label: __("From Time"),
				fieldtype: "Time",
				description: __("When you started. Optional."),
				onchange: () => me.sync_hours_from_times(),
			},
		});

		this.to_time_field = frappe.ui.form.make_control({
			parent: this.$root.find('[data-field="to_time"]'),
			render_input: true,
			df: {
				fieldname: "to_time",
				label: __("To Time"),
				fieldtype: "Time",
				description: __("When you finished. Optional."),
				onchange: () => me.sync_hours_from_times(),
			},
		});

		this.hours_field = frappe.ui.form.make_control({
			parent: this.$root.find('[data-field="hours"]'),
			render_input: true,
			df: {
				fieldname: "hours",
				label: __("Hours Worked"),
				fieldtype: "Float",
				precision: 2,
				description: __("Only needed when submitting time manually."),
				onchange: () => me.update_action_state(),
			},
		});

		this.toggle_task_field(false);
		this.update_action_state();
	}

	bind_events() {
		this.$root.find(".ltp-start-timer").on("click", () => this.start_timer());
		this.$root.find(".ltp-submit-time").on("click", () => this.submit_time());
		this.$root.find(".ltp-change-work").on("click", () => this.open_change_work());
		this.$root.find(".ltp-cancel-change").on("click", () => this.cancel_change_work());
		this.$root.find(".ltp-switch-work").on("click", () => this.switch_work());
		this.$root.find(".ltp-stop-timer").on("click", () => this.confirm_stop_timer());
	}

	// ---------------------------------------------------------------
	// Selection helpers
	// ---------------------------------------------------------------

	get_value(fieldname) {
		const field = {
			project: this.project_field,
			activity_type: this.activity_type_field,
			task: this.task_field,
			date: this.date_field,
			hours: this.hours_field,
			from_time: this.from_time_field,
			to_time: this.to_time_field,
		}[fieldname];

		return field ? field.get_value() : null;
	}

	// Both times given? Then the duration is a fact, not an estimate — show it
	// in Hours Worked so the employee sees what will be logged before they
	// submit. The backend derives it again rather than trusting this value.
	sync_hours_from_times() {
		const from_time = this.get_value("from_time");
		const to_time = this.get_value("to_time");

		if (from_time && to_time) {
			const date = this.get_value("date") || frappe.datetime.get_today();
			const start = frappe.datetime.str_to_obj(`${date} ${from_time}`);
			const end = frappe.datetime.str_to_obj(`${date} ${to_time}`);
			const hours = (end - start) / 3600000;

			this.hours_field.set_value(hours > 0 ? flt(hours, 2) : 0);
		}

		this.update_action_state();
	}

	on_project_change() {
		// Changing the Project invalidates any Task picked under the old one.
		if (this.task_field.get_value()) {
			this.task_field.set_value("");
		}
		this.toggle_task_field(!!this.get_value("project"));
		this.update_action_state();
	}

	toggle_task_field(enabled) {
		const $input = this.task_field.$input;
		if (!$input) return;

		$input.prop("disabled", !enabled);
		$input.attr("placeholder", enabled ? __("Select a task") : __("Select a project first"));
	}

	get_selection() {
		return {
			project: this.get_value("project"),
			activity_type: this.get_value("activity_type"),
			task: this.get_value("task") || null,
		};
	}

	// A Project and an Activity Type are required to time work. Task is optional
	// because a project may legitimately have no tasks defined.
	has_valid_selection() {
		const sel = this.get_selection();
		return !!(sel.project && sel.activity_type);
	}

	clear_selection() {
		this.project_field.set_value("");
		this.activity_type_field.set_value("");
		this.task_field.set_value("");
		this.hours_field.set_value(0);
		this.from_time_field.set_value("");
		this.to_time_field.set_value("");
		this.toggle_task_field(false);
		this.update_action_state();
	}

	// Disable the actions that cannot succeed, rather than letting the user
	// click through to a validation error.
	update_action_state() {
		const valid = this.has_valid_selection();
		const hours = flt(this.get_value("hours"));

		this.$root.find(".ltp-start-timer").prop("disabled", !valid);
		this.$root.find(".ltp-switch-work").prop("disabled", !valid);
		this.$root
			.find(".ltp-submit-time")
			.prop("disabled", !(valid && hours > 0 && this.get_value("date")));
	}

	// ---------------------------------------------------------------
	// Rendering
	// ---------------------------------------------------------------

	render() {
		const running = this.mode === "running";
		const changing = this.mode === "changing";

		// The active card stays visible during "changing" so the running timer is
		// never visually discarded before the switch is confirmed.
		this.$active.toggleClass("hidden", !(running || changing));
		this.$active.toggleClass("ltp-compact", changing);

		this.$selection.toggleClass("hidden", running);

		this.$root.find(".ltp-start-timer").toggleClass("hidden", changing);
		this.$root.find(".ltp-submit-time").toggleClass("hidden", changing);
		this.$root.find(".ltp-switch-work").toggleClass("hidden", !changing);
		this.$root.find(".ltp-cancel-change").toggleClass("hidden", !changing);

		this.$root
			.find(".ltp-selection-title")
			.text(changing ? __("Change Work") : __("Log Time for a Project"));
		this.$root
			.find(".ltp-selection-hint")
			.text(
				changing
					? __("Select the work to switch to. The current segment will be closed.")
					: __("Pick a Project, then an Activity Type, then a Task.")
			);

		this.render_active_card();
		this.update_action_state();
	}

	render_active_card() {
		if (!this.timer) return;

		const dash = "—";
		this.$root
			.find(".ltp-cur-project")
			.text(this.timer.project_name || this.timer.project || dash);
		this.$root.find(".ltp-cur-activity").text(this.timer.activity_type || dash);
		this.$root.find(".ltp-cur-task").text(this.timer.task_subject || this.timer.task || dash);

		const started = this.timer.start_time
			? frappe.datetime.str_to_user(this.timer.start_time, true)
			: dash;
		this.$root.find(".ltp-started-value").text(started);

		this.update_elapsed();
	}

	// ---------------------------------------------------------------
	// Elapsed time
	// ---------------------------------------------------------------

	// Elapsed is derived from the backend's start_time every tick rather than
	// being counted up locally, so a throttled or slept tab still shows the
	// true duration on wake.
	get_elapsed_seconds() {
		if (!this.timer || !this.timer.start_time) return 0;

		// Both sides go through the same conversion, which keeps the result free
		// of browser-vs-server timezone drift.
		const start = frappe.datetime.str_to_obj(this.timer.start_time);
		const now = frappe.datetime.str_to_obj(frappe.datetime.now_datetime());

		return Math.max(0, Math.floor((now - start) / 1000));
	}

	format_duration(total_seconds) {
		const hours = Math.floor(total_seconds / 3600);
		const minutes = Math.floor((total_seconds % 3600) / 60);
		const seconds = total_seconds % 60;
		const pad = (n) => String(n).padStart(2, "0");

		return `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`;
	}

	update_elapsed() {
		this.$root.find(".ltp-elapsed").text(this.format_duration(this.get_elapsed_seconds()));
	}

	start_ticking() {
		this.stop_ticking();
		this.update_elapsed();
		this.tick_handle = setInterval(() => this.update_elapsed(), 1000);
	}

	stop_ticking() {
		if (this.tick_handle) {
			clearInterval(this.tick_handle);
			this.tick_handle = null;
		}
	}

	// ---------------------------------------------------------------
	// Backend calls
	// ---------------------------------------------------------------

	// Wraps frappe.call so a missing Phase 1 backend degrades to a labelled
	// display-only mode instead of an unexplained failure.
	call_backend(method, args, opts = {}) {
		return frappe
			.call({ method, args, freeze: opts.freeze, freeze_message: opts.freeze_message })
			.then((r) => {
				this.set_degraded(false);
				return r ? r.message : null;
			})
			.catch((err) => {
				if (this.is_missing_method_error(err)) {
					this.set_degraded(true);
					throw { __ltp_backend_missing: true, method };
				}
				throw err;
			});
	}

	// Frappe answers an unresolvable whitelisted method with HTTP 417 and the
	// message "Failed to get method for command <dotted.path>". Both are checked,
	// because 417 + ValidationError is also what the backend's own validation
	// raises — matching on exc_type alone would read a genuine business error
	// (say "Project X does not exist") as a missing backend and start a phantom
	// timer.
	is_missing_method_error(err) {
		if (!err || err.status !== 417) return false;

		const body = err.responseText || err.message || "";
		return /Failed to get method for command/i.test(body);
	}

	set_degraded(is_degraded) {
		this.degraded = is_degraded;
		this.$degraded.toggleClass("hidden", !is_degraded);
		this.$degraded
			.find(".ltp-degraded-text")
			.text(
				__(
					"The time-tracking backend is not available yet. The timer below runs for display only and nothing is being saved."
				)
			);
	}

	// Pull the authoritative active segment. This page holds no timer state of
	// its own across reloads — the backend's Timesheet is the single source.
	refresh_from_backend() {
		return this.call_backend(BACKEND.get_active_timer, {})
			.then((timer) => {
				this.apply_timer(timer);
			})
			.catch((err) => {
				if (err && err.__ltp_backend_missing) return;
				// A read failure should not blank out a timer that is already on screen.
				console.error("Could not load active timer", err);
			});
	}

	apply_timer(timer) {
		this.timer = timer || null;

		if (this.timer) {
			this.mode = "running";
			this.start_ticking();
		} else {
			this.mode = "idle";
			this.stop_ticking();
		}

		this.render();
	}

	// ---------------------------------------------------------------
	// Actions
	// ---------------------------------------------------------------

	start_timer() {
		if (!this.has_valid_selection()) return;
		const selection = this.get_selection();

		this.call_backend(BACKEND.start_timer, selection, {
			freeze: true,
			freeze_message: __("Starting timer..."),
		})
			.then((timer) => {
				this.apply_timer(timer || this.local_timer(selection));
				this.clear_selection();
				frappe.show_alert({ message: __("Timer started"), indicator: "green" });
			})
			.catch((err) => {
				if (err && err.__ltp_backend_missing) {
					// Degraded: run the display timer so the flow stays testable.
					this.apply_timer(this.local_timer(selection));
					this.clear_selection();
					return;
				}
				this.show_error(err, __("Could not start the timer"));
			});
	}

	open_change_work() {
		this.mode = "changing";
		this.clear_selection();
		this.render();
		this.project_field.$input && this.project_field.$input.focus();
	}

	cancel_change_work() {
		// The original segment was never closed, so this is a pure UI revert.
		this.mode = this.timer ? "running" : "idle";
		this.clear_selection();
		this.render();
	}

	// One call closes the current segment and opens the next, so the switch is
	// atomic on the backend rather than a stop followed by a start.
	switch_work() {
		if (!this.has_valid_selection()) return;
		const selection = this.get_selection();

		this.call_backend(BACKEND.change_work, selection, {
			freeze: true,
			freeze_message: __("Switching work..."),
		})
			.then((timer) => {
				this.apply_timer(timer || this.local_timer(selection));
				this.clear_selection();
				frappe.show_alert({ message: __("Switched work"), indicator: "green" });
			})
			.catch((err) => {
				if (err && err.__ltp_backend_missing) {
					this.apply_timer(this.local_timer(selection));
					this.clear_selection();
					return;
				}
				this.show_error(err, __("Could not switch work"));
			});
	}

	confirm_stop_timer() {
		const elapsed = this.format_duration(this.get_elapsed_seconds());
		const project = this.timer ? this.timer.project_name || this.timer.project || "" : "";

		frappe.confirm(
			__("Stop the timer and log {0} against {1}?", [
				`<b>${frappe.utils.escape_html(elapsed)}</b>`,
				`<b>${frappe.utils.escape_html(project)}</b>`,
			]),
			() => this.stop_timer()
		);
	}

	stop_timer() {
		this.call_backend(
			BACKEND.stop_timer,
			{},
			{ freeze: true, freeze_message: __("Stopping timer...") }
		)
			.then((result) => {
				this.apply_timer(null);
				this.announce_saved(result, __("Timer stopped"));
			})
			.catch((err) => {
				if (err && err.__ltp_backend_missing) {
					this.apply_timer(null);
					return;
				}
				this.show_error(err, __("Could not stop the timer"));
			});
	}

	// Manual entry path — no timer involved.
	submit_time() {
		if (!this.has_valid_selection()) return;

		const hours = flt(this.get_value("hours"));
		const date = this.get_value("date");

		if (!(hours > 0)) {
			frappe.msgprint({
				title: __("Hours Worked required"),
				message: __(
					"Enter a From Time and a To Time, or the number of hours worked. Or use Start Work Timer instead."
				),
				indicator: "orange",
			});
			return;
		}

		// Send the clock times when the employee gave them, so the Timesheet
		// records when the work happened rather than a placeholder slot.
		const args = Object.assign(this.get_selection(), {
			hours: hours,
			date: date,
			from_time: this.get_value("from_time") || null,
			to_time: this.get_value("to_time") || null,
		});

		this.call_backend(BACKEND.log_time, args, {
			freeze: true,
			freeze_message: __("Logging time..."),
		})
			.then((result) => {
				this.clear_selection();
				this.date_field.set_value(frappe.datetime.get_today());
				this.announce_saved(result, __("Time logged"));
			})
			.catch((err) => {
				if (err && err.__ltp_backend_missing) return;
				this.show_error(err, __("Could not log time"));
			});
	}

	// ---------------------------------------------------------------
	// Feedback
	// ---------------------------------------------------------------

	// Links straight to whatever record the backend created, so the employee can
	// verify it without hunting through the Timesheet list.
	announce_saved(result, fallback_message) {
		const timesheet = result && result.timesheet;

		if (timesheet) {
			frappe.show_alert({
				message: __("Saved to {0}", [
					`<a href="/app/timesheet/${encodeURIComponent(
						timesheet
					)}">${frappe.utils.escape_html(timesheet)}</a>`,
				]),
				indicator: "green",
			});
		} else {
			frappe.show_alert({ message: fallback_message, indicator: "green" });
		}
	}

	show_error(err, title) {
		// frappe.call has already surfaced the server traceback; this adds the
		// context of which action failed.
		console.error(title, err);
		frappe.show_alert({ message: title, indicator: "red" });
	}

	// Display-only stand-in used when the backend is not deployed yet. It is
	// never persisted and never treated as a source of truth.
	local_timer(selection) {
		return {
			project: selection.project,
			project_name: selection.project,
			activity_type: selection.activity_type,
			task: selection.task,
			task_subject: selection.task,
			start_time: frappe.datetime.now_datetime(),
		};
	}
};
