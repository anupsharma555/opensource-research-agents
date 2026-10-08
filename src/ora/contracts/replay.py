"""Ordered replay with explicit gaps; receiving an event is not acknowledging it."""

from .models import Event


class ReplayProjection:
    def __init__(self, cursor: int = 0):
        self.cursor = cursor
        self.pending: dict[int, Event] = {}
        self.seen: dict[str, str] = {}
        self.entities: dict[tuple[str, str], dict] = {}
        self.mission_id: str | None = None

    def receive(self, event: Event):
        if self.mission_id is not None and self.mission_id != event.mission_id:
            raise ValueError("a replay projection consumes one mission stream")
        body = event.model_dump_json(by_alias=True)
        if event.id in self.seen:
            if self.seen[event.id] != body:
                raise ValueError("event ID collision: reload the affected stream")
            return
        if event.sequence <= self.cursor:
            # A persisted cursor represents already accepted earlier events.
            return
        if event.sequence in self.pending:
            raise ValueError("two event IDs claim the same stream position")
        pending = {**self.pending, event.sequence: event}
        entities = dict(self.entities)
        cursor = self.cursor
        while cursor + 1 in pending:
            next_event = pending.pop(cursor + 1)
            key = (next_event.entity_kind, next_event.entity_id)
            previous = entities.get(key)
            if previous and next_event.version <= previous["version"]:
                # Discard a previously buffered bad event too; never poison its ID.
                self.pending.pop(next_event.sequence, None)
                self.seen.pop(next_event.id, None)
                raise ValueError("entity version did not advance")
            entities[key] = {"state": next_event.state, "version": next_event.version}
            cursor += 1
        self.mission_id = event.mission_id
        self.seen[event.id] = body
        self.pending, self.entities, self.cursor = pending, entities, cursor

    @property
    def missing_sequence(self):
        return self.cursor + 1 if self.pending else None
