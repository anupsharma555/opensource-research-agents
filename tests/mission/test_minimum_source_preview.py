"""A refined estimate cannot discard a fully checked fitting minimum packet."""

import hashlib
from types import SimpleNamespace

import pytest

from ora.coordination.board import ref
from ora.live.provider import MODEL
from ora.mission.context import ContextOverflow
from ora.mission.source_context import plan_source_turn
from ora.models.token_estimate import TOKENIZER_BASIS, estimate_request_tokens
from ora.state.store import canonical
from tests.mission.test_selected_window_admission import _ready_selected_window


@pytest.mark.parametrize('final_units,accepted', [(1000, True), (1300, False)])
def test_minimum_retry_is_bounded_and_complete_size_checked(tmp_path, monkeypatch, final_units, accepted):
    host, run, actor, _, _, _, _ = _ready_selected_window(tmp_path, monkeypatch)
    try:
        control = host._control(run.run_id, actor)
        proposal = host.store.get('board_proposal', control['body']['task_id'])
        plan = plan_source_turn(host.store, ref(control), source_input_allowance_tokens=8000)
        allowances = []
        source_arguments = []
        preview_sources = []
        monkeypatch.setattr(host.context, 'checked_window_is_selected_input', lambda *a, **k: False)

        def assemble(*args, **kwargs):
            allowance = kwargs['allowance']
            allowances.append(allowance)
            source_arguments.append(kwargs['source_refs'])
            if len(allowances) == 2:
                assert allowance < 2000
                raise ContextOverflow('Exact minimum', required_tokens=2000)
            assert len(allowances) <= 3
            return SimpleNamespace(estimated_tokens=3000 if len(allowances)==1 else 2000,
                                   messages=[{'role': 'user', 'content': 'checked-packet'}])

        def preview(**kwargs):
            preview_sources.append(kwargs['sources'])
            # Keep the mock a supported complete request, so this exercises
            # tokenizer admission rather than unsupported-shape byte fallback.
            units = 0 if len(preview_sources) == 1 else 1500 if len(preview_sources) == 2 else final_units
            body = {
                'model': MODEL,
                'messages': [
                    {'role': 'developer', 'content': 'Synthetic preview instructions.'},
                    {'role': 'user', 'content': ' 0123456789' * units},
                    *kwargs['messages'],
                ],
                'reasoning_effort': 'high', 'include_reasoning': True,
                'max_completion_tokens': 8192, 'stream': False,
                'tools': [{'type': 'function', 'function': {
                    'name': 'read_source_window',
                    'parameters': {'type': 'object', 'properties': {},
                                   'required': [], 'additionalProperties': False},
                }}],
                'tool_choice': 'auto', 'parallel_tool_calls': False,
            }
            raw = canonical(body).encode()
            assert estimate_request_tokens(raw).basis == TOKENIZER_BASIS
            return SimpleNamespace(body=body,
                                   request_sha256=hashlib.sha256(raw).hexdigest())

        monkeypatch.setattr(host.context, 'assemble', assemble)
        monkeypatch.setattr(host.session, 'preview_request', preview)
        result = host._preview_soft_source_plan(
            control, proposal, plan, total_target=6000, sequence=control['body']['sequence']+1,
        )
        assert allowances[0] == 6000 and allowances[-1] == 2000
        assert len(allowances) == 3 and len(preview_sources) == 3
        assert all(value == source_arguments[0] for value in source_arguments)
        assert preview_sources[0] == preview_sources[1] == preview_sources[2]
        assert (result is not None) is accepted
        if accepted:
            assert result['context_allowance'] == 2000
            assert result['estimated_request_tokens'] <= 6000
        assert not host.store.records('model_dispatch_marker')
    finally:
        host.close()
