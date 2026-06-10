# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Deterministic state model for Forgeplane workflow execution.

This module is the foundation of the simplified workflow engine. It defines
the workflow and task state enums, the only valid transitions between them,
and the rules that connect task progress to workflow progress. The model is
deliberately small and explicit — no BPMN vocabulary, no dynamic transition
registration — so every reachable state can be enumerated and tested before
DSL, persistence, API, or visual features are layered on top.

Workflow lifecycle::

    draft -> ready -> running -> completed
                        |  ^        failed
                        v  |        cancelled
                       paused

    draft/ready/running/paused may all be cancelled by an operator.

Task lifecycle::

    pending -> ready -> running -> completed
                 ^  \\      |          failed
                 |   v     v          skipped
                 +-- blocked

    pending/ready/blocked may be skipped (for example, a branch not taken).

Invariants (verified by ``tests/test_workflow_states.py``):

1. Terminal states — workflow ``completed``/``failed``/``cancelled``, task
   ``completed``/``failed``/``skipped`` — have no outgoing transitions.
2. Self-transitions are never valid; a transition always changes the state.
3. Task states may only change while the owning workflow is ``running``
   (see :func:`tasks_may_progress`).
4. A workflow completes only when every task is terminal and none failed.
5. A workflow derived from its tasks fails as soon as any task fails
   (fail-fast: remaining work is irrelevant once one task has failed).
"""

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Final

# StrEnum keeps the wire format identical to the enum value ("running" ==
# WorkflowState.RUNNING), so future persistence and API layers can serialize
# states without a mapping table.


class WorkflowState(StrEnum):
    """Lifecycle states of one workflow instance."""

    # The definition is being authored and may still be invalid.
    DRAFT = "draft"
    # The definition validated successfully and may be started.
    READY = "ready"
    # The engine is actively progressing tasks.
    RUNNING = "running"
    # Execution is suspended by an operator; tasks must not progress.
    PAUSED = "paused"
    # Every task reached a terminal state and none failed. Terminal.
    COMPLETED = "completed"
    # At least one task failed (fail-fast). Terminal.
    FAILED = "failed"
    # An operator aborted the workflow before it finished. Terminal.
    CANCELLED = "cancelled"


class TaskState(StrEnum):
    """Lifecycle states of one task inside a workflow."""

    # The task exists but its dependencies are not satisfied yet.
    PENDING = "pending"
    # Dependencies are satisfied; the task is eligible to run.
    READY = "ready"
    # The task is executing.
    RUNNING = "running"
    # The task is waiting on an external condition (event, human input).
    BLOCKED = "blocked"
    # The task finished successfully. Terminal.
    COMPLETED = "completed"
    # The task finished unsuccessfully. Terminal.
    FAILED = "failed"
    # The task was deliberately not executed (branch not taken). Terminal.
    SKIPPED = "skipped"


class InvalidTransitionError(ValueError):
    """Raised when a state change is not allowed by the transition tables.

    Subclassing ``ValueError`` lets callers that do not care about workflow
    semantics still catch the failure as plain invalid input.
    """

    def __init__(self, kind: str, current: str, target: str) -> None:
        # Keep the offending pair on the exception so callers can report it
        # without parsing the message.
        super().__init__(f"invalid {kind} transition: {current!r} -> {target!r}")
        self.kind = kind
        self.current = current
        self.target = target


# Each state maps to the complete set of states it may move to. An empty set
# marks a terminal state. The tables are the single source of truth for the
# engine: anything not listed here is rejected, including self-transitions.
WORKFLOW_TRANSITIONS: Final[Mapping[WorkflowState, frozenset[WorkflowState]]] = {
    WorkflowState.DRAFT: frozenset({WorkflowState.READY, WorkflowState.CANCELLED}),
    WorkflowState.READY: frozenset({WorkflowState.RUNNING, WorkflowState.CANCELLED}),
    WorkflowState.RUNNING: frozenset(
        {
            WorkflowState.PAUSED,
            WorkflowState.COMPLETED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.PAUSED: frozenset({WorkflowState.RUNNING, WorkflowState.CANCELLED}),
    WorkflowState.COMPLETED: frozenset(),
    WorkflowState.FAILED: frozenset(),
    WorkflowState.CANCELLED: frozenset(),
}

TASK_TRANSITIONS: Final[Mapping[TaskState, frozenset[TaskState]]] = {
    TaskState.PENDING: frozenset({TaskState.READY, TaskState.SKIPPED}),
    TaskState.READY: frozenset(
        {TaskState.RUNNING, TaskState.BLOCKED, TaskState.SKIPPED}
    ),
    TaskState.RUNNING: frozenset(
        {TaskState.COMPLETED, TaskState.FAILED, TaskState.BLOCKED}
    ),
    # A blocked task re-enters READY when unblocked, fails when the wait is
    # abandoned (for example, a timeout), or is skipped when its branch is
    # discarded while it waits.
    TaskState.BLOCKED: frozenset(
        {TaskState.READY, TaskState.FAILED, TaskState.SKIPPED}
    ),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.SKIPPED: frozenset(),
}

# Terminal sets are derived from the tables so they can never drift apart.
TERMINAL_WORKFLOW_STATES: Final[frozenset[WorkflowState]] = frozenset(
    state for state, targets in WORKFLOW_TRANSITIONS.items() if not targets
)
TERMINAL_TASK_STATES: Final[frozenset[TaskState]] = frozenset(
    state for state, targets in TASK_TRANSITIONS.items() if not targets
)


def is_workflow_terminal(state: WorkflowState) -> bool:
    """Return ``True`` when the workflow state has no outgoing transitions."""
    return state in TERMINAL_WORKFLOW_STATES


def is_task_terminal(state: TaskState) -> bool:
    """Return ``True`` when the task state has no outgoing transitions."""
    return state in TERMINAL_TASK_STATES


def can_transition_workflow(current: WorkflowState, target: WorkflowState) -> bool:
    """Return ``True`` when ``current -> target`` is a valid workflow move."""
    return target in WORKFLOW_TRANSITIONS[current]


def can_transition_task(current: TaskState, target: TaskState) -> bool:
    """Return ``True`` when ``current -> target`` is a valid task move."""
    return target in TASK_TRANSITIONS[current]


def transition_workflow(current: WorkflowState, target: WorkflowState) -> WorkflowState:
    """Validate a workflow transition and return the new state.

    Raises:
        InvalidTransitionError: if the move is not in the transition table.
    """
    if not can_transition_workflow(current, target):
        raise InvalidTransitionError("workflow", current, target)
    return target


def transition_task(current: TaskState, target: TaskState) -> TaskState:
    """Validate a task transition and return the new state.

    Raises:
        InvalidTransitionError: if the move is not in the transition table.
    """
    if not can_transition_task(current, target):
        raise InvalidTransitionError("task", current, target)
    return target


def tasks_may_progress(workflow_state: WorkflowState | str) -> bool:
    """Return ``True`` when tasks of this workflow are allowed to change state.

    Invariant 3: task transitions are only legal while the owning workflow is
    ``running``. A paused, draft, or terminal workflow freezes its tasks; the
    engine must check this gate before applying any task transition.

    Accepts the serialized string form as well, so states loaded from
    JSON/YAML behave identically; unknown values raise ``ValueError``.
    """
    return WorkflowState(workflow_state) is WorkflowState.RUNNING


def derive_workflow_state(task_states: Iterable[TaskState | str]) -> WorkflowState:
    """Return the state a *running* workflow should hold given its tasks.

    This function encodes how task state changes affect workflow state, and
    must only be consulted while the workflow is ``running`` — pauses and
    cancellations are operator decisions, so they are never derived here.

    Rules, in priority order:

    1. Failure: any ``failed`` task fails the whole workflow immediately,
       even if other tasks are still in flight (fail-fast MVP semantics).
    2. Completion: when every task is terminal (``completed`` or ``skipped``,
       since ``failed`` was handled above), the workflow is ``completed``.
       A workflow with no tasks completes vacuously.
    3. Otherwise the workflow keeps ``running``.

    Accepts the serialized string form as well, so states loaded from
    JSON/YAML behave identically; unknown values raise ``ValueError``.
    """
    # Coerce through the enum so identity checks below cannot miss a plain
    # string ("failed" == TaskState.FAILED but is not the member), and so
    # unknown values fail loudly instead of counting as in-flight work. The
    # list also materializes the iterable consumed by both checks below.
    states = [TaskState(state) for state in task_states]
    if any(state is TaskState.FAILED for state in states):
        return WorkflowState.FAILED
    if all(is_task_terminal(state) for state in states):
        return WorkflowState.COMPLETED
    return WorkflowState.RUNNING
