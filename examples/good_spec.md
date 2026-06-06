# Todo Module Spec

## Goal

Provide a small Todo module that exposes create, update, complete, and delete operations through a REST API.

## Context

The Todo module is the first feature shipped on top of the new task service. It serves as a reference implementation for downstream feature teams.

## Acceptance Criteria

- A user can create a todo with a title and an optional due date.
- Listing todos returns items sorted by creation time.
- Completing a todo records the completion timestamp.
- Deleting a todo removes it from all future list responses.

## Risks

- Date handling across timezones may produce inconsistent ordering.
- Soft-delete versus hard-delete semantics are not yet agreed upon.

## Open Questions

- Should completed todos be hidden from the default list view?
- Do we need per-user quotas for the first release?
