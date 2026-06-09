# Todo Module Spec

## Goal

Provide a small Todo module that exposes create, update, complete, and delete
operations through a REST API. The module is the first feature shipped on top
of the new task service and acts as a reference implementation for downstream
feature teams.

## Context

The Todo module is consumed by the mobile and web clients of the task
service. It must keep response shapes consistent with the existing
`/tasks/*` endpoints so client teams can share serializers between modules.

## Acceptance Criteria

- A user can create a todo with a title and an optional due date.
- Listing todos returns items sorted by creation time, newest first.
- Completing a todo records the completion timestamp in UTC.
- Deleting a todo removes it from all future list responses.
- The API returns RFC 7807 problem details for validation errors.

## Risks

- Date handling across timezones may produce inconsistent ordering when
  clients submit local timestamps without offsets.
- Soft-delete versus hard-delete semantics are not yet agreed upon with the
  data retention working group.

## Open Questions

- Should completed todos be hidden from the default list view, or returned
  with a `completed: true` flag?
- Do we need per-user quotas in the first release?
