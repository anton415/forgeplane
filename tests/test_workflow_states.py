# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Tests for the workflow/task state model and its invariants.

Valid transitions are asserted from explicit expected tables (so a table
edit in the module must be mirrored here deliberately), and invalid
transitions are asserted exhaustively as the complement over the full
state-pair product — every pair is either valid or rejected, with no gaps.
"""

import pytest

from forgeplane.workflow import (
    TASK_TRANSITIONS,
    TERMINAL_TASK_STATES,
    TERMINAL_WORKFLOW_STATES,
    WORKFLOW_TRANSITIONS,
    InvalidTransitionError,
    TaskState,
    WorkflowState,
    can_transition_task,
    can_transition_workflow,
    derive_workflow_state,
    is_task_terminal,
    is_workflow_terminal,
    tasks_may_progress,
    transition_task,
    transition_workflow,
)

# Expected transitions restated independently from the implementation so the
# tests fail if the module tables change without a matching test update.
EXPECTED_WORKFLOW_TRANSITIONS: dict[WorkflowState, set[WorkflowState]] = {
    WorkflowState.DRAFT: {WorkflowState.READY, WorkflowState.CANCELLED},
    WorkflowState.READY: {WorkflowState.RUNNING, WorkflowState.CANCELLED},
    WorkflowState.RUNNING: {
        WorkflowState.PAUSED,
        WorkflowState.COMPLETED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.PAUSED: {WorkflowState.RUNNING, WorkflowState.CANCELLED},
    WorkflowState.COMPLETED: set(),
    WorkflowState.FAILED: set(),
    WorkflowState.CANCELLED: set(),
}

EXPECTED_TASK_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.PENDING: {TaskState.READY, TaskState.SKIPPED},
    TaskState.READY: {TaskState.RUNNING, TaskState.BLOCKED, TaskState.SKIPPED},
    TaskState.RUNNING: {TaskState.COMPLETED, TaskState.FAILED, TaskState.BLOCKED},
    TaskState.BLOCKED: {TaskState.READY, TaskState.FAILED, TaskState.SKIPPED},
    TaskState.COMPLETED: set(),
    TaskState.FAILED: set(),
    TaskState.SKIPPED: set(),
}


class TestStateEnums:
    """The enums expose exactly the states required by the MVP scope."""

    def test_workflow_state_values(self) -> None:
        # StrEnum members must serialize to the documented wire values.
        assert {state.value for state in WorkflowState} == {
            "draft",
            "ready",
            "running",
            "paused",
            "completed",
            "failed",
            "cancelled",
        }

    def test_task_state_values(self) -> None:
        assert {state.value for state in TaskState} == {
            "pending",
            "ready",
            "running",
            "blocked",
            "completed",
            "failed",
            "skipped",
        }


class TestTransitionTables:
    """The transition tables are total and match the documented model."""

    def test_workflow_table_covers_every_state(self) -> None:
        # A missing key would make can_transition_workflow raise KeyError
        # instead of returning False, so totality is itself an invariant.
        assert set(WORKFLOW_TRANSITIONS) == set(WorkflowState)

    def test_task_table_covers_every_state(self) -> None:
        assert set(TASK_TRANSITIONS) == set(TaskState)

    def test_workflow_table_matches_expected(self) -> None:
        assert {
            state: set(targets) for state, targets in WORKFLOW_TRANSITIONS.items()
        } == EXPECTED_WORKFLOW_TRANSITIONS

    def test_task_table_matches_expected(self) -> None:
        assert {
            state: set(targets) for state, targets in TASK_TRANSITIONS.items()
        } == EXPECTED_TASK_TRANSITIONS


class TestWorkflowTransitions:
    """Valid workflow moves succeed; everything else is rejected."""

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (current, target)
            for current, targets in EXPECTED_WORKFLOW_TRANSITIONS.items()
            for target in sorted(targets)
        ],
    )
    def test_valid_transitions_are_accepted(
        self, current: WorkflowState, target: WorkflowState
    ) -> None:
        assert can_transition_workflow(current, target)
        assert transition_workflow(current, target) is target

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (current, target)
            for current in WorkflowState
            for target in WorkflowState
            if target not in EXPECTED_WORKFLOW_TRANSITIONS[current]
        ],
    )
    def test_invalid_transitions_are_rejected(
        self, current: WorkflowState, target: WorkflowState
    ) -> None:
        # The complement product includes every self-transition and every
        # move out of a terminal state, so both invariants are covered here.
        assert not can_transition_workflow(current, target)
        with pytest.raises(InvalidTransitionError):
            transition_workflow(current, target)


class TestTaskTransitions:
    """Valid task moves succeed; everything else is rejected."""

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (current, target)
            for current, targets in EXPECTED_TASK_TRANSITIONS.items()
            for target in sorted(targets)
        ],
    )
    def test_valid_transitions_are_accepted(
        self, current: TaskState, target: TaskState
    ) -> None:
        assert can_transition_task(current, target)
        assert transition_task(current, target) is target

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (current, target)
            for current in TaskState
            for target in TaskState
            if target not in EXPECTED_TASK_TRANSITIONS[current]
        ],
    )
    def test_invalid_transitions_are_rejected(
        self, current: TaskState, target: TaskState
    ) -> None:
        assert not can_transition_task(current, target)
        with pytest.raises(InvalidTransitionError):
            transition_task(current, target)


class TestTerminalStates:
    """Terminal states are derived from the tables and have no exits."""

    def test_terminal_workflow_states(self) -> None:
        assert {
            WorkflowState.COMPLETED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        } == TERMINAL_WORKFLOW_STATES

    def test_terminal_task_states(self) -> None:
        assert {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.SKIPPED,
        } == TERMINAL_TASK_STATES

    @pytest.mark.parametrize("state", sorted(WorkflowState))
    def test_is_workflow_terminal_matches_table(self, state: WorkflowState) -> None:
        # Invariant 1: a state is terminal exactly when it has no exits.
        assert is_workflow_terminal(state) == (not WORKFLOW_TRANSITIONS[state])

    @pytest.mark.parametrize("state", sorted(TaskState))
    def test_is_task_terminal_matches_table(self, state: TaskState) -> None:
        assert is_task_terminal(state) == (not TASK_TRANSITIONS[state])

    @pytest.mark.parametrize("state", sorted(WorkflowState))
    def test_no_workflow_self_transitions(self, state: WorkflowState) -> None:
        # Invariant 2: a transition always changes the state.
        assert state not in WORKFLOW_TRANSITIONS[state]

    @pytest.mark.parametrize("state", sorted(TaskState))
    def test_no_task_self_transitions(self, state: TaskState) -> None:
        assert state not in TASK_TRANSITIONS[state]


class TestTasksMayProgress:
    """Invariant 3: tasks only change state while the workflow runs."""

    def test_running_workflow_allows_task_progress(self) -> None:
        assert tasks_may_progress(WorkflowState.RUNNING)

    @pytest.mark.parametrize(
        "state",
        [
            state
            for state in sorted(WorkflowState)
            if state is not WorkflowState.RUNNING
        ],
    )
    def test_non_running_workflow_freezes_tasks(self, state: WorkflowState) -> None:
        assert not tasks_may_progress(state)

    def test_serialized_strings_are_coerced(self) -> None:
        # JSON/YAML payloads carry the plain string form of the StrEnum.
        assert tasks_may_progress("running")
        assert not tasks_may_progress("paused")

    def test_unknown_workflow_state_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="exploded"):
            tasks_may_progress("exploded")


class TestDeriveWorkflowState:
    """Completion and failure rules for a running workflow."""

    def test_all_tasks_completed_completes_workflow(self) -> None:
        states = [TaskState.COMPLETED, TaskState.COMPLETED]
        assert derive_workflow_state(states) is WorkflowState.COMPLETED

    def test_skipped_tasks_count_as_done(self) -> None:
        # Completion rule: skipped is a successful terminal outcome.
        states = [TaskState.COMPLETED, TaskState.SKIPPED]
        assert derive_workflow_state(states) is WorkflowState.COMPLETED

    def test_workflow_without_tasks_completes_vacuously(self) -> None:
        assert derive_workflow_state([]) is WorkflowState.COMPLETED

    def test_any_failed_task_fails_workflow(self) -> None:
        states = [TaskState.COMPLETED, TaskState.FAILED]
        assert derive_workflow_state(states) is WorkflowState.FAILED

    def test_failure_wins_even_with_tasks_in_flight(self) -> None:
        # Failure rule has priority: fail-fast even though work remains.
        states = [TaskState.RUNNING, TaskState.PENDING, TaskState.FAILED]
        assert derive_workflow_state(states) is WorkflowState.FAILED

    @pytest.mark.parametrize(
        "in_flight",
        [TaskState.PENDING, TaskState.READY, TaskState.RUNNING, TaskState.BLOCKED],
    )
    def test_any_non_terminal_task_keeps_workflow_running(
        self, in_flight: TaskState
    ) -> None:
        states = [TaskState.COMPLETED, in_flight]
        assert derive_workflow_state(states) is WorkflowState.RUNNING

    def test_accepts_any_iterable(self) -> None:
        # The implementation must materialize the iterable before consuming
        # it twice; a generator would otherwise be exhausted mid-check.
        states = (state for state in [TaskState.COMPLETED, TaskState.SKIPPED])
        assert derive_workflow_state(states) is WorkflowState.COMPLETED

    def test_serialized_string_failure_fails_workflow(self) -> None:
        # JSON/YAML payloads carry the plain string form of the StrEnum; a
        # string "failed" must trigger fail-fast exactly like the member.
        assert derive_workflow_state(["completed", "failed"]) is WorkflowState.FAILED

    def test_serialized_string_completion_completes_workflow(self) -> None:
        states: list[TaskState | str] = ["completed", TaskState.SKIPPED]
        assert derive_workflow_state(states) is WorkflowState.COMPLETED

    def test_unknown_task_state_is_rejected(self) -> None:
        # Garbage input must fail loudly instead of counting as in-flight.
        with pytest.raises(ValueError, match="exploded"):
            derive_workflow_state(["completed", "exploded"])


class TestInvalidTransitionError:
    """The error carries structured context and stays a ValueError."""

    def test_is_a_value_error(self) -> None:
        assert issubclass(InvalidTransitionError, ValueError)

    def test_carries_kind_and_states(self) -> None:
        with pytest.raises(InvalidTransitionError) as excinfo:
            transition_workflow(WorkflowState.COMPLETED, WorkflowState.RUNNING)
        error = excinfo.value
        assert error.kind == "workflow"
        assert error.current == WorkflowState.COMPLETED
        assert error.target == WorkflowState.RUNNING
        assert "completed" in str(error)
        assert "running" in str(error)

    def test_task_error_reports_task_kind(self) -> None:
        with pytest.raises(InvalidTransitionError) as excinfo:
            transition_task(TaskState.SKIPPED, TaskState.RUNNING)
        assert excinfo.value.kind == "task"
