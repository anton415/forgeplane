# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Workflow engine state and domain model.

Re-exports the public surface of :mod:`forgeplane.workflow.states` and
:mod:`forgeplane.workflow.model` so callers can import from the package root
while the engine grows new modules.
"""

from forgeplane.workflow.model import (
    InvalidDefinitionError,
    TaskDefinition,
    TaskError,
    TaskInput,
    TaskInstance,
    TaskResult,
    TasksFrozenError,
    WorkflowDefinition,
    WorkflowInstance,
)
from forgeplane.workflow.states import (
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

__all__ = [
    "TASK_TRANSITIONS",
    "TERMINAL_TASK_STATES",
    "TERMINAL_WORKFLOW_STATES",
    "WORKFLOW_TRANSITIONS",
    "InvalidDefinitionError",
    "InvalidTransitionError",
    "TaskDefinition",
    "TaskError",
    "TaskInput",
    "TaskInstance",
    "TaskResult",
    "TaskState",
    "TasksFrozenError",
    "WorkflowDefinition",
    "WorkflowInstance",
    "WorkflowState",
    "can_transition_task",
    "can_transition_workflow",
    "derive_workflow_state",
    "is_task_terminal",
    "is_workflow_terminal",
    "tasks_may_progress",
    "transition_task",
    "transition_workflow",
]
