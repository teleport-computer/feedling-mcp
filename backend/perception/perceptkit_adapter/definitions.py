"""Transaction-bound PostgreSQL history for IO's live PerceptKit rules."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable, Sequence

from perceptkit.ports.definitions import DefinitionArchiveConflictError
from perceptkit.rules import EventDefinition


def _document(definition: EventDefinition) -> dict[str, Any]:
    lifecycle = asdict(definition.lifecycle)
    return {
        "id": definition.definition_id,
        "version": definition.version,
        "enabled": definition.enabled,
        "subject_id": definition.subject_id,
        "source": {
            "signal": definition.signal,
            "field": definition.field_name,
            "when": dict(definition.when),
        },
        "condition": {
            "type": definition.condition_type,
            "operator": definition.operator,
            "value": definition.value,
            "params": dict(definition.params),
        },
        "event": {"type": definition.event_type},
        "wake": {"enabled": definition.wake_enabled},
        "lifecycle": lifecycle,
        "deduplication": {"key": definition.dedupe_field},
    }


class PostgresDefinitionProvider:
    """Live rules from IO, immutable versions from the storage transaction."""

    persistent = True

    def __init__(
        self,
        conn: Any,
        live: Callable[[str], Sequence[EventDefinition]] | Sequence[EventDefinition],
    ) -> None:
        self.conn = conn
        self._live = live

    def definitions_for(self, subject_id: str) -> tuple[EventDefinition, ...]:
        source = self._live(subject_id) if callable(self._live) else self._live
        return tuple(
            definition for definition in source
            if definition.subject_id is None or definition.subject_id == subject_id
        )

    def definition_at(self, definition_id: str, version: int) -> EventDefinition | None:
        row = self.conn.execute(
            "SELECT definition FROM perceptkit_definition_history "
            "WHERE definition_id=%s AND version=%s",
            (definition_id, version),
        ).fetchone()
        return EventDefinition.parse(row[0]) if row else None

    def archive_definition(self, definition: EventDefinition) -> None:
        from psycopg.types.json import Jsonb

        document = _document(definition)
        row = self.conn.execute(
            "INSERT INTO perceptkit_definition_history "
            "(definition_id,version,definition) VALUES (%s,%s,%s) "
            "ON CONFLICT (definition_id,version) DO UPDATE "
            "SET definition=perceptkit_definition_history.definition "
            "RETURNING definition",
            (definition.definition_id, definition.version, Jsonb(document)),
        ).fetchone()
        if row is None or row[0] != document:
            raise DefinitionArchiveConflictError(
                definition.definition_id, definition.version,
            )


__all__ = ["PostgresDefinitionProvider"]
