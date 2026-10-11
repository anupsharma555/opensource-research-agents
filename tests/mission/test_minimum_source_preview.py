"""A refined estimate cannot discard a fully checked fitting minimum packet."""

from types import SimpleNamespace

import pytest

from ora.coordination.board import ref
from ora.mission.context import ContextOverflow
from ora.mission.source_context import plan_source_turn
from tests.mission.test_selected_window_admission import _ready_selected_window


@pytest.mark.parametrize('final_bytes,accepted', [(12000, True), (21000, False)])
def test_minimum_retry_is_bounded_and_complete_size_checked(tmp_path, monkeypatch, final_bytes, accepted):
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
            size = 24000 if len(preview_sources)==1 else final_bytes
            return SimpleNamespace(body={'payload': 'x'*size}, request_sha256='verified-preview')

        monkeypatch.setattr(host.context, 'assemble', assemble)
        monkeypatch.setattr(host.session, 'preview_request', preview)
        result = host._preview_soft_source_plan(
            control, proposal, plan, total_target=6000, sequence=control['body']['sequence']+1,
        )
        assert allowances[0] == 6000 and allowances[-1] == 2000
        assert len(allowances) == 3 and len(preview_sources) == 2
        assert all(value == source_arguments[0] for value in source_arguments)
        assert preview_sources[0] == preview_sources[1]
        assert (result is not None) is accepted
        if accepted:
            assert result['context_allowance'] == 2000
            assert result['estimated_request_tokens'] <= 6000
        assert not host.store.records('model_dispatch_marker')
    finally:
        host.close()
