# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Core domain model for the Forgeplane workflow engine.

This module defines the objects the engine reasons about, layered on top of
the deterministic state model in :mod:`forgeplane.workflow.states`:

- :class:`TaskDefinition` / :class:`WorkflowDefinition` — the immutable,
  validated description of a workflow graph (what *should* run and in which
  dependency order).
- :class:`TaskInstance` / :class:`WorkflowInstance` — the mutable runtime
  record of one execution (what *is* running and in which state).
- :class:`TaskInput` / :class:`TaskResult` / :class:`TaskError` — the data
  contracts that cross a task boundary: the payload a task receives, the
  outcome it produces, and the structured shape of a failure.

Design rules:

1. The model depends only on the standard library and the sibling state
   module — no HTTP, API, or persistence framework — so it can be embedded
   anywhere and tested in isolation.
2. Definitions are frozen dataclasses validated at construction time: an
   invalid graph (duplicate ids, unknown or self dependencies, cycles) can
   never exist as a ``WorkflowDefinition`` value.
3. Instances keep runtime state separate from the shared definition, and
   every state change goes through the transition tables of
   :mod:`forgeplane.workflow.states`, including the invariant that tasks may
   only progress while the owning workflow is ``running``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Self

from forgeplane.workflow import states
from forgeplane.workflow.states import TaskState, WorkflowState


class InvalidDefinitionError(ValueError):
    """Raised when a workflow or task definition violates a structural rule.

    Subclassing ``ValueError`` keeps the contract of the workflow package:
    callers that do not care about workflow semantics can still catch the
    failure as plain invalid input.
    """


class TasksFrozenError(ValueError):
    """Raised when a task state change is attempted on a non-running workflow.

    This is invariant 3 of the state model surfacing at the domain level:
    tasks of a draft, ready, paused, or terminal workflow must not progress.
    """


def _read_only_payload(payload: Mapping[str, object]) -> Mapping[str, object]:
    """Return an immutable snapshot of a payload mapping.

    Copying first means later mutation of the caller's dict cannot leak into
    a frozen domain object; the proxy then rejects writes through the field.
    Payload values are expected to stay JSON-serializable by convention so
    future persistence and API layers can store them without adapters.
    """
    return MappingProxyType(dict(payload))


@dataclass(frozen=True, slots=True)
class TaskError:
    """Structured description of a task failure (the error contract).

    A failure is always machine-readable (``code``) and human-readable
    (``message``) at the same time, so the engine can branch on the code
    while operators read the message. ``details`` carries arbitrary context
    (offending values, upstream ids); ``retryable`` tells the engine whether
    re-running the task could ever succeed.
    """

    code: str
    message: str
    details: Mapping[str, object] = field(default_factory=dict)
    retryable: bool = False

    def __post_init__(self) -> None:
        # Blank identifiers would make failures impossible to dispatch on,
        # so they are rejected eagerly instead of surfacing downstream.
        if not self.code.strip():
            raise ValueError("task error code must be a non-empty string")
        if not self.message.strip():
            raise ValueError("task error message must be a non-empty string")
        object.__setattr__(self, "details", _read_only_payload(self.details))


@dataclass(frozen=True, slots=True)
class TaskInput:
    """Immutable input contract handed to a task when it starts.

    ``parameters`` is the complete payload the task is allowed to see; the
    engine assembles it (definition parameters, upstream outputs) before the
    task runs, so task implementations never reach back into the workflow.
    """

    parameters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", _read_only_payload(self.parameters))


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Outcome contract of a finished task: success output or a failure.

    ``error is None`` marks success. A failed result must not carry output
    data — downstream tasks may only consume outputs of successful tasks, so
    allowing both would create an ambiguous contract.
    """

    output: Mapping[str, object] = field(default_factory=dict)
    error: TaskError | None = None

    def __post_init__(self) -> None:
        if self.error is not None and self.output:
            raise ValueError("a failed task result must not carry output data")
        object.__setattr__(self, "output", _read_only_payload(self.output))

    @property
    def is_success(self) -> bool:
        """Return ``True`` when the task finished without an error."""
        return self.error is None

    @classmethod
    def success(cls, output: Mapping[str, object] | None = None) -> Self:
        """Build a successful result with an optional output payload."""
        return cls(output=output if output is not None else {})

    @classmethod
    def failure(cls, error: TaskError) -> Self:
        """Build a failed result carrying the structured error."""
        return cls(error=error)


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    """Static description of one unit of work inside a workflow.

    ``task_type`` names the kind of work the engine dispatches (for example
    ``"shell"`` or ``"llm_review"``); the registry of types is an engine
    concern, so the definition only requires a non-empty identifier.
    ``depends_on`` lists the ids of tasks that must reach a successful
    terminal state before this task becomes ready. ``parameters`` holds the
    authored inputs that seed the runtime :class:`TaskInput`.
    """

    task_id: str
    task_type: str
    depends_on: frozenset[str] = frozenset()
    parameters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise InvalidDefinitionError("task_id must be a non-empty string")
        if not self.task_type.strip():
            raise InvalidDefinitionError(
                f"task {self.task_id!r}: task_type must be a non-empty string"
            )
        # Normalize whatever iterable the caller provided into a frozenset so
        # graph validation can rely on set semantics.
        depends_on = frozenset(self.depends_on)
        if self.task_id in depends_on:
            raise InvalidDefinitionError(
                f"task {self.task_id!r} must not depend on itself"
            )
        if any(not dependency.strip() for dependency in depends_on):
            raise InvalidDefinitionError(
                f"task {self.task_id!r}: dependency ids must be non-empty strings"
            )
        object.__setattr__(self, "depends_on", depends_on)
        object.__setattr__(self, "parameters", _read_only_payload(self.parameters))


def _topological_order(
    workflow_id: str, tasks: Sequence[TaskDefinition]
) -> tuple[str, ...]:
    """Return task ids in dependency order, or raise on a cycle.

    The sweep repeatedly takes every task whose dependencies are already
    placed, preserving definition order inside each wave, so the result is
    deterministic for a given definition. When no task can be placed the
    remainder contains at least one cycle.
    """
    placed: set[str] = set()
    order: list[str] = []
    remaining = list(tasks)
    while remaining:
        wave = [task for task in remaining if task.depends_on <= placed]
        if not wave:
            stuck = ", ".join(sorted(task.task_id for task in remaining))
            raise InvalidDefinitionError(
                f"workflow {workflow_id!r} has a dependency cycle; "
                f"unable to order tasks: {stuck}"
            )
        for task in wave:
            order.append(task.task_id)
            placed.add(task.task_id)
        remaining = [task for task in remaining if task.task_id not in placed]
    return tuple(order)


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """Validated, immutable description of a workflow graph.

    The graph is the set of tasks plus their ``depends_on`` edges, and it
    must be a DAG: construction rejects duplicate task ids, dependencies on
    unknown tasks, and dependency cycles. A definition that exists is
    therefore always startable; instances created from it begin in the
    ``ready`` workflow state.
    """

    workflow_id: str
    tasks: tuple[TaskDefinition, ...] = ()

    def __post_init__(self) -> None:
        if not self.workflow_id.strip():
            raise InvalidDefinitionError("workflow_id must be a non-empty string")
        # Normalize whatever sequence the caller provided into a tuple so the
        # definition stays hash-independent of the caller's container.
        tasks = tuple(self.tasks)
        object.__setattr__(self, "tasks", tasks)
        known_ids: set[str] = set()
        for task in tasks:
            if task.task_id in known_ids:
                raise InvalidDefinitionError(
                    f"workflow {self.workflow_id!r}: duplicate task id {task.task_id!r}"
                )
            known_ids.add(task.task_id)
        for task in tasks:
            unknown = task.depends_on - known_ids
            if unknown:
                missing = ", ".join(sorted(unknown))
                raise InvalidDefinitionError(
                    f"workflow {self.workflow_id!r}: task {task.task_id!r} "
                    f"depends on unknown tasks: {missing}"
                )
        # Reject cycles at construction time; the order itself is recomputed
        # on demand by execution_order().
        _topological_order(self.workflow_id, tasks)

    @property
    def task_ids(self) -> tuple[str, ...]:
        """Return the task ids in definition order."""
        return tuple(task.task_id for task in self.tasks)

    def task(self, task_id: str) -> TaskDefinition:
        """Return the definition of one task by id.

        Raises:
            KeyError: if the workflow has no task with this id.
        """
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise KeyError(f"workflow {self.workflow_id!r} has no task {task_id!r}")

    def execution_order(self) -> tuple[str, ...]:
        """Return task ids in a deterministic dependency-respecting order."""
        return _topological_order(self.workflow_id, self.tasks)


@dataclass(slots=True)
class TaskInstance:
    """Runtime record of one task inside a workflow instance.

    The definition stays immutable and shared between instances; the fields
    owned here — ``state``, ``input``, ``result`` — are the per-execution
    data. State changes must go through the owning
    :class:`WorkflowInstance`, which enforces the running-workflow gate and
    the transition tables.
    """

    definition: TaskDefinition
    state: TaskState = TaskState.PENDING
    input: TaskInput | None = None
    result: TaskResult | None = None

    @property
    def task_id(self) -> str:
        """Return the id of the underlying task definition."""
        return self.definition.task_id

    @property
    def is_terminal(self) -> bool:
        """Return ``True`` when the task can no longer change state."""
        return states.is_task_terminal(self.state)


@dataclass(slots=True)
class WorkflowInstance:
    """Runtime record of one execution of a :class:`WorkflowDefinition`.

    Create instances with :meth:`from_definition`; the direct constructor
    exists for future rehydration (for example, loading a persisted run) and
    validates that the task instances mirror the definition exactly. All
    state changes go through the methods below so the transition tables and
    the cross-object invariants of the state model hold by construction.
    """

    instance_id: str
    definition: WorkflowDefinition
    state: WorkflowState = WorkflowState.READY
    tasks: dict[str, TaskInstance] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.instance_id.strip():
            raise ValueError("instance_id must be a non-empty string")
        # The task map must mirror the definition exactly — a missing or
        # extra task instance would silently break the derivation rules.
        expected = set(self.definition.task_ids)
        if set(self.tasks) != expected:
            raise ValueError(
                f"workflow instance {self.instance_id!r}: task instances must "
                f"match the definition's task ids exactly"
            )
        for task_id, task in self.tasks.items():
            if task.definition.task_id != task_id:
                raise ValueError(
                    f"workflow instance {self.instance_id!r}: key {task_id!r} "
                    f"holds a task instance for {task.definition.task_id!r}"
                )

    @classmethod
    def from_definition(cls, definition: WorkflowDefinition, instance_id: str) -> Self:
        """Create a fresh instance of a validated definition.

        The workflow starts in ``ready`` (the definition proved itself valid
        at construction time) and every task starts in ``pending``; tasks are
        keyed by id in definition order.
        """
        tasks = {
            task.task_id: TaskInstance(definition=task) for task in definition.tasks
        }
        return cls(instance_id=instance_id, definition=definition, tasks=tasks)

    @property
    def is_terminal(self) -> bool:
        """Return ``True`` when the workflow can no longer change state."""
        return states.is_workflow_terminal(self.state)

    def task(self, task_id: str) -> TaskInstance:
        """Return the runtime record of one task by id.

        Raises:
            KeyError: if the instance has no task with this id.
        """
        try:
            return self.tasks[task_id]
        except KeyError:
            raise KeyError(
                f"workflow instance {self.instance_id!r} has no task {task_id!r}"
            ) from None

    def derived_state(self) -> WorkflowState:
        """Return the state a *running* workflow should hold given its tasks.

        Thin wrapper over :func:`forgeplane.workflow.states.derive_workflow_state`;
        only meaningful while the workflow is ``running``.
        """
        return states.derive_workflow_state(task.state for task in self.tasks.values())

    def transition_to(self, target: WorkflowState) -> None:
        """Move the workflow to ``target``, validating the transition.

        Operator moves (``running``, ``paused``, ``cancelled``) only need to
        be valid per the transition table. The outcome states enforce the
        derivation invariants on top: ``completed`` and ``failed`` are
        accepted only when the task states actually derive them, so a
        workflow cannot complete with work in flight (invariant 4) nor fail
        without a failed task (invariant 5).

        Raises:
            InvalidTransitionError: if the move is not in the transition table.
            ValueError: if an outcome state contradicts the task states.
        """
        if target in {WorkflowState.COMPLETED, WorkflowState.FAILED}:
            derived = self.derived_state()
            if derived is not target:
                raise ValueError(
                    f"workflow instance {self.instance_id!r} cannot move to "
                    f"{target!r}: task states derive {derived!r}"
                )
        self.state = states.transition_workflow(self.state, target)

    def _progressable_task(self, task_id: str) -> TaskInstance:
        """Return a task after enforcing the running-workflow gate.

        Every task mutation funnels through here so invariant 3 — tasks only
        change state while the owning workflow is ``running`` — cannot be
        bypassed.
        """
        if not states.tasks_may_progress(self.state):
            raise TasksFrozenError(
                f"tasks of workflow instance {self.instance_id!r} cannot change "
                f"state while the workflow is {self.state!r}"
            )
        return self.task(task_id)

    def transition_task(self, task_id: str, target: TaskState) -> None:
        """Move a task to ``target`` (ready, blocked, or skipped).

        The states that carry a data contract are excluded on purpose:
        ``running`` requires an input (use :meth:`start_task`) and the
        terminal outcomes require a result (use :meth:`record_result`), so a
        task can never end up in those states with the contract missing.

        Raises:
            ValueError: if ``target`` is a contract-carrying state.
            TasksFrozenError: if the workflow is not running.
            InvalidTransitionError: if the move is not in the transition table.
        """
        if target in {TaskState.RUNNING, TaskState.COMPLETED, TaskState.FAILED}:
            raise ValueError(
                f"target {target!r} carries a data contract: use start_task for "
                f"'running' and record_result for terminal outcomes"
            )
        task = self._progressable_task(task_id)
        task.state = states.transition_task(task.state, target)

    def start_task(self, task_id: str, task_input: TaskInput | None = None) -> None:
        """Move a ready task to ``running`` and attach its input contract.

        On a re-run after ``blocked`` the input may be omitted to keep the
        previously attached one.

        Raises:
            TasksFrozenError: if the workflow is not running.
            InvalidTransitionError: if the task is not ready to start.
        """
        task = self._progressable_task(task_id)
        task.state = states.transition_task(task.state, TaskState.RUNNING)
        if task_input is not None:
            task.input = task_input

    def record_result(self, task_id: str, result: TaskResult) -> None:
        """Finish a task with its outcome contract.

        The terminal state follows from the result — ``completed`` on
        success, ``failed`` when it carries an error — so state and result
        can never contradict each other. Because terminal states have no
        outgoing transitions, a result can only be recorded once.

        Raises:
            TasksFrozenError: if the workflow is not running.
            InvalidTransitionError: if the task cannot finish from its state.
        """
        task = self._progressable_task(task_id)
        target = TaskState.COMPLETED if result.is_success else TaskState.FAILED
        task.state = states.transition_task(task.state, target)
        task.result = result
