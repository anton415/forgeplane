# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Tests for the core workflow domain model.

Covers construction and validation of the immutable definitions (task and
workflow graph rules), the data contracts (input, result, structured error),
and the runtime instances — including the guarantee that every state change
goes through the transition tables and the running-workflow gate.
"""

import pytest

from forgeplane.workflow import (
    InvalidDefinitionError,
    InvalidTransitionError,
    TaskDefinition,
    TaskError,
    TaskInput,
    TaskInstance,
    TaskResult,
    TasksFrozenError,
    TaskState,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowState,
)


def make_diamond_definition() -> WorkflowDefinition:
    """Build the classic diamond graph: fetch -> (parse, lint) -> publish."""
    return WorkflowDefinition(
        workflow_id="diamond",
        tasks=(
            TaskDefinition(task_id="fetch", task_type="http"),
            TaskDefinition(
                task_id="parse", task_type="python", depends_on=frozenset({"fetch"})
            ),
            TaskDefinition(
                task_id="lint", task_type="shell", depends_on=frozenset({"fetch"})
            ),
            TaskDefinition(
                task_id="publish",
                task_type="http",
                depends_on=frozenset({"parse", "lint"}),
            ),
        ),
    )


def make_running_single_task_instance() -> WorkflowInstance:
    """Build a one-task instance already moved to running/ready-to-start."""
    definition = WorkflowDefinition(
        workflow_id="single",
        tasks=(TaskDefinition(task_id="only", task_type="shell"),),
    )
    instance = WorkflowInstance.from_definition(definition, instance_id="single-1")
    instance.transition_to(WorkflowState.RUNNING)
    instance.transition_task("only", TaskState.READY)
    return instance


class TestTaskError:
    """The structured error contract validates its identifying fields."""

    def test_construction_and_defaults(self) -> None:
        error = TaskError(code="timeout", message="task exceeded 30s")
        assert error.code == "timeout"
        assert error.message == "task exceeded 30s"
        assert dict(error.details) == {}
        assert error.retryable is False

    def test_carries_details_and_retryable_flag(self) -> None:
        error = TaskError(
            code="http_error",
            message="upstream returned 503",
            details={"status": 503},
            retryable=True,
        )
        assert error.details["status"] == 503
        assert error.retryable is True

    @pytest.mark.parametrize("code", ["", "   "])
    def test_blank_code_is_rejected(self, code: str) -> None:
        with pytest.raises(ValueError, match="code"):
            TaskError(code=code, message="boom")

    @pytest.mark.parametrize("message", ["", "   "])
    def test_blank_message_is_rejected(self, message: str) -> None:
        with pytest.raises(ValueError, match="message"):
            TaskError(code="boom", message=message)

    def test_details_are_read_only(self) -> None:
        error = TaskError(code="boom", message="x", details={"key": "value"})
        with pytest.raises(TypeError):
            error.details["key"] = "other"  # type: ignore[index]

    def test_details_are_snapshotted_from_the_source_dict(self) -> None:
        # Mutating the caller's dict after construction must not leak in.
        source: dict[str, object] = {"key": "value"}
        error = TaskError(code="boom", message="x", details=source)
        source["key"] = "other"
        assert error.details["key"] == "value"


class TestTaskInput:
    """The input contract is an immutable parameter snapshot."""

    def test_defaults_to_empty_parameters(self) -> None:
        assert dict(TaskInput().parameters) == {}

    def test_parameters_are_read_only(self) -> None:
        task_input = TaskInput(parameters={"url": "https://example.com"})
        with pytest.raises(TypeError):
            task_input.parameters["url"] = "other"  # type: ignore[index]

    def test_parameters_are_snapshotted_from_the_source_dict(self) -> None:
        source: dict[str, object] = {"url": "https://example.com"}
        task_input = TaskInput(parameters=source)
        source["url"] = "other"
        assert task_input.parameters["url"] == "https://example.com"

    def test_equality_is_structural(self) -> None:
        assert TaskInput(parameters={"a": 1}) == TaskInput(parameters={"a": 1})


class TestTaskResult:
    """The outcome contract is success output XOR structured error."""

    def test_success_factory(self) -> None:
        result = TaskResult.success({"rows": 10})
        assert result.is_success
        assert result.error is None
        assert result.output["rows"] == 10

    def test_success_without_output(self) -> None:
        result = TaskResult.success()
        assert result.is_success
        assert dict(result.output) == {}

    def test_failure_factory(self) -> None:
        error = TaskError(code="boom", message="x")
        result = TaskResult.failure(error)
        assert not result.is_success
        assert result.error is error
        assert dict(result.output) == {}

    def test_failed_result_with_output_is_rejected(self) -> None:
        # Downstream tasks only consume outputs of successful tasks, so a
        # failure carrying output would be an ambiguous contract.
        error = TaskError(code="boom", message="x")
        with pytest.raises(ValueError, match="output"):
            TaskResult(output={"rows": 10}, error=error)

    def test_output_is_read_only(self) -> None:
        result = TaskResult.success({"rows": 10})
        with pytest.raises(TypeError):
            result.output["rows"] = 11  # type: ignore[index]


class TestTaskDefinition:
    """Task definitions validate identity, type, and dependency ids."""

    def test_construction_and_defaults(self) -> None:
        task = TaskDefinition(task_id="fetch", task_type="http")
        assert task.task_id == "fetch"
        assert task.task_type == "http"
        assert task.depends_on == frozenset()
        assert dict(task.parameters) == {}

    def test_supports_dependencies_and_parameters(self) -> None:
        task = TaskDefinition(
            task_id="publish",
            task_type="http",
            depends_on=frozenset({"parse", "lint"}),
            parameters={"target": "registry"},
        )
        assert task.depends_on == {"parse", "lint"}
        assert task.parameters["target"] == "registry"

    @pytest.mark.parametrize("task_id", ["", "   "])
    def test_blank_task_id_is_rejected(self, task_id: str) -> None:
        with pytest.raises(InvalidDefinitionError, match="task_id"):
            TaskDefinition(task_id=task_id, task_type="http")

    @pytest.mark.parametrize("task_type", ["", "   "])
    def test_blank_task_type_is_rejected(self, task_type: str) -> None:
        with pytest.raises(InvalidDefinitionError, match="task_type"):
            TaskDefinition(task_id="fetch", task_type=task_type)

    def test_self_dependency_is_rejected(self) -> None:
        with pytest.raises(InvalidDefinitionError, match="itself"):
            TaskDefinition(
                task_id="fetch", task_type="http", depends_on=frozenset({"fetch"})
            )

    def test_blank_dependency_id_is_rejected(self) -> None:
        with pytest.raises(InvalidDefinitionError, match="dependency"):
            TaskDefinition(
                task_id="fetch", task_type="http", depends_on=frozenset({"  "})
            )


class TestWorkflowDefinition:
    """Workflow definitions describe a validated DAG of tasks."""

    def test_describes_a_workflow_graph(self) -> None:
        definition = make_diamond_definition()
        assert definition.workflow_id == "diamond"
        assert definition.task_ids == ("fetch", "parse", "lint", "publish")
        assert definition.task("publish").depends_on == {"parse", "lint"}

    def test_empty_workflow_is_allowed(self) -> None:
        # Mirrors the state model: a workflow without tasks completes
        # vacuously, so the definition itself is legal.
        assert WorkflowDefinition(workflow_id="empty").tasks == ()

    def test_unknown_task_lookup_raises_key_error(self) -> None:
        with pytest.raises(KeyError, match="missing"):
            make_diamond_definition().task("missing")

    @pytest.mark.parametrize("workflow_id", ["", "   "])
    def test_blank_workflow_id_is_rejected(self, workflow_id: str) -> None:
        with pytest.raises(InvalidDefinitionError, match="workflow_id"):
            WorkflowDefinition(workflow_id=workflow_id)

    def test_duplicate_task_ids_are_rejected(self) -> None:
        with pytest.raises(InvalidDefinitionError, match="duplicate"):
            WorkflowDefinition(
                workflow_id="dup",
                tasks=(
                    TaskDefinition(task_id="fetch", task_type="http"),
                    TaskDefinition(task_id="fetch", task_type="shell"),
                ),
            )

    def test_unknown_dependency_is_rejected(self) -> None:
        with pytest.raises(InvalidDefinitionError, match="unknown"):
            WorkflowDefinition(
                workflow_id="dangling",
                tasks=(
                    TaskDefinition(
                        task_id="parse",
                        task_type="python",
                        depends_on=frozenset({"fetch"}),
                    ),
                ),
            )

    def test_dependency_cycle_is_rejected(self) -> None:
        with pytest.raises(InvalidDefinitionError, match="cycle"):
            WorkflowDefinition(
                workflow_id="loop",
                tasks=(
                    TaskDefinition(
                        task_id="a", task_type="shell", depends_on=frozenset({"b"})
                    ),
                    TaskDefinition(
                        task_id="b", task_type="shell", depends_on=frozenset({"a"})
                    ),
                ),
            )

    def test_indirect_cycle_is_rejected(self) -> None:
        with pytest.raises(InvalidDefinitionError, match="cycle"):
            WorkflowDefinition(
                workflow_id="loop3",
                tasks=(
                    TaskDefinition(
                        task_id="a", task_type="shell", depends_on=frozenset({"c"})
                    ),
                    TaskDefinition(
                        task_id="b", task_type="shell", depends_on=frozenset({"a"})
                    ),
                    TaskDefinition(
                        task_id="c", task_type="shell", depends_on=frozenset({"b"})
                    ),
                ),
            )

    def test_execution_order_respects_dependencies(self) -> None:
        order = make_diamond_definition().execution_order()
        assert set(order) == {"fetch", "parse", "lint", "publish"}
        assert order.index("fetch") < order.index("parse")
        assert order.index("fetch") < order.index("lint")
        assert order.index("parse") < order.index("publish")
        assert order.index("lint") < order.index("publish")

    def test_execution_order_is_deterministic(self) -> None:
        # Inside each dependency wave the definition order is preserved.
        assert make_diamond_definition().execution_order() == (
            "fetch",
            "parse",
            "lint",
            "publish",
        )


class TestWorkflowInstanceCreation:
    """Instances are created from a definition with fresh runtime state."""

    def test_from_definition_initial_state(self) -> None:
        definition = make_diamond_definition()
        instance = WorkflowInstance.from_definition(definition, instance_id="run-1")
        assert instance.instance_id == "run-1"
        assert instance.definition is definition
        # The definition validated itself at construction, so the instance
        # starts ready-to-run; no task has progressed yet.
        assert instance.state is WorkflowState.READY
        assert list(instance.tasks) == ["fetch", "parse", "lint", "publish"]
        for task_id, task in instance.tasks.items():
            assert task.state is TaskState.PENDING
            assert task.definition is definition.task(task_id)
            assert task.input is None
            assert task.result is None

    def test_instances_do_not_share_runtime_state(self) -> None:
        definition = make_diamond_definition()
        first = WorkflowInstance.from_definition(definition, instance_id="run-1")
        second = WorkflowInstance.from_definition(definition, instance_id="run-2")
        first.transition_to(WorkflowState.RUNNING)
        first.transition_task("fetch", TaskState.READY)
        assert second.state is WorkflowState.READY
        assert second.task("fetch").state is TaskState.PENDING

    @pytest.mark.parametrize("instance_id", ["", "   "])
    def test_blank_instance_id_is_rejected(self, instance_id: str) -> None:
        definition = make_diamond_definition()
        with pytest.raises(ValueError, match="instance_id"):
            WorkflowInstance.from_definition(definition, instance_id=instance_id)

    def test_task_instances_must_mirror_the_definition(self) -> None:
        # The direct constructor exists for rehydration and must reject a
        # task map that diverges from the definition's task ids.
        definition = make_diamond_definition()
        with pytest.raises(ValueError, match="match the definition"):
            WorkflowInstance(instance_id="run-1", definition=definition, tasks={})

    def test_misskeyed_task_instance_is_rejected(self) -> None:
        definition = WorkflowDefinition(
            workflow_id="single",
            tasks=(TaskDefinition(task_id="only", task_type="shell"),),
        )
        other = TaskDefinition(task_id="other", task_type="shell")
        with pytest.raises(ValueError, match="only"):
            WorkflowInstance(
                instance_id="run-1",
                definition=definition,
                tasks={"only": TaskInstance(definition=other)},
            )

    def test_unknown_task_lookup_raises_key_error(self) -> None:
        definition = make_diamond_definition()
        instance = WorkflowInstance.from_definition(definition, instance_id="run-1")
        with pytest.raises(KeyError, match="missing"):
            instance.task("missing")


class TestWorkflowInstanceTransitions:
    """Workflow moves obey the tables plus the derivation invariants."""

    def test_ready_to_running(self) -> None:
        instance = WorkflowInstance.from_definition(
            make_diamond_definition(), instance_id="run-1"
        )
        instance.transition_to(WorkflowState.RUNNING)
        assert instance.state is WorkflowState.RUNNING
        assert not instance.is_terminal

    def test_invalid_move_is_rejected(self) -> None:
        instance = WorkflowInstance.from_definition(
            make_diamond_definition(), instance_id="run-1"
        )
        with pytest.raises(InvalidTransitionError):
            instance.transition_to(WorkflowState.PAUSED)

    def test_cancellation_is_always_an_operator_decision(self) -> None:
        instance = WorkflowInstance.from_definition(
            make_diamond_definition(), instance_id="run-1"
        )
        instance.transition_to(WorkflowState.CANCELLED)
        assert instance.state is WorkflowState.CANCELLED
        assert instance.is_terminal

    def test_cannot_complete_with_tasks_in_flight(self) -> None:
        # Invariant 4: completion requires every task to be terminal.
        instance = WorkflowInstance.from_definition(
            make_diamond_definition(), instance_id="run-1"
        )
        instance.transition_to(WorkflowState.RUNNING)
        with pytest.raises(ValueError, match="derive"):
            instance.transition_to(WorkflowState.COMPLETED)
        assert instance.state is WorkflowState.RUNNING

    def test_cannot_fail_without_a_failed_task(self) -> None:
        # Invariant 5: failure is derived from a failed task, never invented.
        instance = WorkflowInstance.from_definition(
            make_diamond_definition(), instance_id="run-1"
        )
        instance.transition_to(WorkflowState.RUNNING)
        with pytest.raises(ValueError, match="derive"):
            instance.transition_to(WorkflowState.FAILED)

    def test_empty_workflow_completes_vacuously(self) -> None:
        definition = WorkflowDefinition(workflow_id="empty")
        instance = WorkflowInstance.from_definition(definition, instance_id="run-1")
        instance.transition_to(WorkflowState.RUNNING)
        assert instance.derived_state() is WorkflowState.COMPLETED
        instance.transition_to(WorkflowState.COMPLETED)
        assert instance.is_terminal

    def test_full_happy_path_completes_the_workflow(self) -> None:
        definition = make_diamond_definition()
        instance = WorkflowInstance.from_definition(definition, instance_id="run-1")
        instance.transition_to(WorkflowState.RUNNING)
        # Drive every task through ready -> running -> completed in
        # dependency order, as the future engine will.
        for task_id in definition.execution_order():
            instance.transition_task(task_id, TaskState.READY)
            instance.start_task(task_id, TaskInput(parameters={"task": task_id}))
            instance.record_result(task_id, TaskResult.success({"done": True}))
        assert instance.derived_state() is WorkflowState.COMPLETED
        instance.transition_to(WorkflowState.COMPLETED)
        assert instance.state is WorkflowState.COMPLETED

    def test_failed_task_drives_workflow_failure(self) -> None:
        instance = make_running_single_task_instance()
        instance.start_task("only")
        error = TaskError(code="boom", message="exploded", retryable=False)
        instance.record_result("only", TaskResult.failure(error))
        assert instance.derived_state() is WorkflowState.FAILED
        instance.transition_to(WorkflowState.FAILED)
        assert instance.state is WorkflowState.FAILED


class TestTaskProgressGate:
    """Invariant 3: task mutations require a running workflow."""

    def test_tasks_are_frozen_while_workflow_is_ready(self) -> None:
        instance = WorkflowInstance.from_definition(
            make_diamond_definition(), instance_id="run-1"
        )
        with pytest.raises(TasksFrozenError):
            instance.transition_task("fetch", TaskState.READY)

    def test_tasks_are_frozen_while_workflow_is_paused(self) -> None:
        instance = make_running_single_task_instance()
        instance.transition_to(WorkflowState.PAUSED)
        with pytest.raises(TasksFrozenError):
            instance.start_task("only")
        with pytest.raises(TasksFrozenError):
            instance.record_result("only", TaskResult.success())

    def test_resume_unfreezes_tasks(self) -> None:
        instance = make_running_single_task_instance()
        instance.transition_to(WorkflowState.PAUSED)
        instance.transition_to(WorkflowState.RUNNING)
        instance.start_task("only")
        assert instance.task("only").state is TaskState.RUNNING


class TestTaskInstanceLifecycle:
    """Task moves are validated and carry the data contracts."""

    def test_pending_to_ready(self) -> None:
        instance = WorkflowInstance.from_definition(
            make_diamond_definition(), instance_id="run-1"
        )
        instance.transition_to(WorkflowState.RUNNING)
        instance.transition_task("fetch", TaskState.READY)
        assert instance.task("fetch").state is TaskState.READY

    def test_skip_a_branch_not_taken(self) -> None:
        instance = WorkflowInstance.from_definition(
            make_diamond_definition(), instance_id="run-1"
        )
        instance.transition_to(WorkflowState.RUNNING)
        instance.transition_task("lint", TaskState.SKIPPED)
        task = instance.task("lint")
        assert task.state is TaskState.SKIPPED
        assert task.is_terminal
        assert task.result is None

    def test_invalid_task_move_is_rejected(self) -> None:
        instance = WorkflowInstance.from_definition(
            make_diamond_definition(), instance_id="run-1"
        )
        instance.transition_to(WorkflowState.RUNNING)
        with pytest.raises(InvalidTransitionError):
            instance.transition_task("fetch", TaskState.BLOCKED)

    @pytest.mark.parametrize(
        "target", [TaskState.RUNNING, TaskState.COMPLETED, TaskState.FAILED]
    )
    def test_contract_states_require_dedicated_methods(self, target: TaskState) -> None:
        # running needs an input, the terminal outcomes need a result; the
        # generic move rejects them so the contracts can never be skipped.
        instance = make_running_single_task_instance()
        with pytest.raises(ValueError, match="contract"):
            instance.transition_task("only", target)

    def test_start_task_attaches_the_input(self) -> None:
        instance = make_running_single_task_instance()
        task_input = TaskInput(parameters={"url": "https://example.com"})
        instance.start_task("only", task_input)
        task = instance.task("only")
        assert task.state is TaskState.RUNNING
        assert task.input is task_input

    def test_start_task_requires_a_ready_task(self) -> None:
        instance = WorkflowInstance.from_definition(
            make_diamond_definition(), instance_id="run-1"
        )
        instance.transition_to(WorkflowState.RUNNING)
        with pytest.raises(InvalidTransitionError):
            instance.start_task("fetch")  # still pending

    def test_restart_after_block_keeps_the_previous_input(self) -> None:
        instance = make_running_single_task_instance()
        task_input = TaskInput(parameters={"url": "https://example.com"})
        instance.start_task("only", task_input)
        instance.transition_task("only", TaskState.BLOCKED)
        instance.transition_task("only", TaskState.READY)
        instance.start_task("only")
        assert instance.task("only").input is task_input

    def test_record_success_completes_the_task(self) -> None:
        instance = make_running_single_task_instance()
        instance.start_task("only")
        result = TaskResult.success({"rows": 10})
        instance.record_result("only", result)
        task = instance.task("only")
        assert task.state is TaskState.COMPLETED
        assert task.result is result
        assert task.is_terminal

    def test_record_failure_fails_the_task(self) -> None:
        instance = make_running_single_task_instance()
        instance.start_task("only")
        result = TaskResult.failure(TaskError(code="boom", message="x"))
        instance.record_result("only", result)
        task = instance.task("only")
        assert task.state is TaskState.FAILED
        assert task.result is result

    def test_abandoning_a_blocked_task_records_a_failure(self) -> None:
        # blocked -> failed is the "wait abandoned" path of the state model
        # (for example, a timeout), and it must carry the error contract.
        instance = make_running_single_task_instance()
        instance.start_task("only")
        instance.transition_task("only", TaskState.BLOCKED)
        result = TaskResult.failure(
            TaskError(code="timeout", message="gave up waiting", retryable=True)
        )
        instance.record_result("only", result)
        assert instance.task("only").state is TaskState.FAILED

    def test_result_can_only_be_recorded_once(self) -> None:
        # Terminal task states have no outgoing transitions, so a second
        # result is rejected before it could overwrite the first.
        instance = make_running_single_task_instance()
        instance.start_task("only")
        first = TaskResult.success({"rows": 10})
        instance.record_result("only", first)
        with pytest.raises(InvalidTransitionError):
            instance.record_result("only", TaskResult.success({"rows": 11}))
        assert instance.task("only").result is first

    def test_task_instance_exposes_definition_identity(self) -> None:
        definition = TaskDefinition(task_id="fetch", task_type="http")
        task = TaskInstance(definition=definition)
        assert task.task_id == "fetch"
        assert task.state is TaskState.PENDING
        assert not task.is_terminal
