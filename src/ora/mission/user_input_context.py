"""Project authenticated user submissions into exact task context and freshness checks.

This module does not accept submissions or grant source permissions. It validates
host-saved target lineage before exposing text or treating a new input as a reason
to prepare another model turn.
"""
from __future__ import annotations

import json


def scoped_user_input(reader, record, *, mission_id, task_id, actor_id, run_id=None):
    if not isinstance(record, dict) or record.get('kind') != 'mission_user_input':
        return False
    body = record.get('body', {})
    actor = reader.get('agent_instance', actor_id, 1)
    target = body.get('task_ref')
    if not isinstance(target, dict) or set(target) != {'kind', 'id', 'version'}:
        return False
    task = reader.get(**target)
    run = reader.get('mission_run', body.get('run_id')) if body.get('run_id') else None
    return bool(
        body.get('schema') == 'ora.mission_user_input.v1'
        and body.get('origin') == 'authenticated_local_user'
        and body.get('kind') in {'direction', 'unverified_pasted_evidence'}
        and isinstance(body.get('text'), str)
        and body.get('mission_id') == mission_id
        and body.get('task_id') == task_id and body.get('actor_id') == actor_id
        and actor and actor['body'].get('mission_id') == mission_id
        and actor['body'].get('run_id') == body.get('run_id')
        and (run_id is None or body.get('run_id') == run_id)
        and run and run['body'].get('mission_id') == mission_id
        and target.get('kind') == 'board_proposal' and target.get('id') == task_id
        and task and task['body'].get('mission_id') == mission_id
    )


def new_user_input(reader, db, *, mission_id, run_id, task_id, actor_id, event_through, included_refs=()):
    """Find accepted input newer than the immutable prepared-input watermark."""
    included = {(item.get("kind"), item.get("id"), item.get("version"))
                for item in included_refs if isinstance(item, dict)}
    rows = db.execute(
        "SELECT sequence,body FROM events WHERE mission_id=? AND sequence>? "
        "AND event_type='mission_user_input_accepted' ORDER BY sequence",
        (mission_id, event_through),
    )
    for row in rows:
        event = json.loads(row['body'])
        pointer = event.get('input_ref')
        if not isinstance(pointer, dict) or set(pointer) != {'kind', 'id', 'version'}:
            continue
        if (pointer.get('kind') != 'mission_user_input'
                or (pointer.get('kind'), pointer.get('id'), pointer.get('version')) in included):
            continue
        record = reader.get(**pointer)
        if scoped_user_input(reader, record, mission_id=mission_id, run_id=run_id,
                             task_id=task_id, actor_id=actor_id):
            return {'event_sequence': row['sequence'],
                    'event_type': 'mission_user_input_accepted', 'input_ref': pointer}
    return None
