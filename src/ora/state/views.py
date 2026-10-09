"""Expose exact saved research relationships as small, read-only SQLite views.

Views include historical revisions and an explicit current-record flag. They
omit prompts, source bodies and raw tool payloads; record references lead to
permitted detail. No view treats a prepared manifest as model execution.
"""

VIEW_SQL = {
    "record_provenance": """
        SELECT r.kind,r.id,r.version AS record_revision,r.created_at,
          (h.version=r.version) AS is_current,
          o.record_format_version,o.operation_id,o.proof_basis,o.timing_basis,o.implementation_ref,
          json_extract(r.body,'$.mission_id') AS mission_id,
          coalesce(json_extract(r.body,'$.instance_id'),json_extract(r.body,'$.agent_instance_id'),
                   json_extract(r.body,'$.actor_ref.id')) AS agent_instance_id,
          coalesce(json_extract(r.body,'$.task_id'),json_extract(r.body,'$.taskId')) AS task_id,
          json_extract(r.body,'$.attempt_id') AS attempt_id,
          json_extract(r.body,'$.call_id') AS call_id,
          CASE WHEN o.id IS NULL THEN 'not_recorded' ELSE 'recorded_at_write' END AS provenance_basis
        FROM records r JOIN heads h ON h.kind=r.kind AND h.id=r.id
        LEFT JOIN record_origins o ON o.kind=r.kind AND o.id=r.id AND o.version=r.version
    """,
    "agent_overview": """
        SELECT r.id AS instance_id,r.version AS record_revision,(h.version=r.version) AS is_current,
          json_extract(r.body,'$.display_name') AS display_name,
          json_extract(r.body,'$.role_id') AS role_id,json_extract(r.body,'$.kind') AS instance_kind,
          json_extract(r.body,'$.mission_id') AS mission_id,json_extract(r.body,'$.run_id') AS run_id,
          json_extract(r.body,'$.predecessor_id') AS predecessor_id,r.created_at
        FROM records r JOIN heads h ON h.kind=r.kind AND h.id=r.id WHERE r.kind='agent_instance'
    """,
    "call_overview": """
        SELECT c.id AS call_id,c.version AS manifest_revision,(h.version=c.version) AS is_current,
          json_extract(c.body,'$.mission_id') AS mission_id,json_extract(c.body,'$.task_id') AS task_id,
          json_extract(c.body,'$.attempt_id') AS attempt_id,
          json_extract(c.body,'$.instance_id') AS instance_id,
          json_extract(a.body,'$.display_name') AS display_name,json_extract(a.body,'$.role_id') AS role_id,
          json_extract(c.body,'$.configuration_id') AS configuration_id,g.version AS configuration_revision,
          json_extract(c.body,'$.provider') AS requested_provider,json_extract(c.body,'$.model_id') AS requested_model,
          json_extract(c.body,'$.configuration_sha256') AS configuration_sha256,
          json_extract(c.body,'$.instructions_sha256') AS instructions_sha256,
          'prepared' AS manifest_state,
          coalesce(json_extract(c.body,'$.execution_status'),'not_recorded') AS execution_status,
          p.proof_basis,p.timing_basis,p.implementation_ref,c.created_at
        FROM records c JOIN heads h ON h.kind=c.kind AND h.id=c.id
        LEFT JOIN records a ON a.kind='agent_instance' AND a.id=json_extract(c.body,'$.instance_id') AND a.version=1
        LEFT JOIN records g ON g.kind='agent_configuration' AND g.id=json_extract(c.body,'$.configuration_id') AND g.version=1
        LEFT JOIN record_origins p ON p.kind=c.kind AND p.id=c.id AND p.version=c.version
        WHERE c.kind='agent_call'
    """,
    "call_skills": """
        SELECT c.id AS call_id,c.version AS manifest_revision,(h.version=c.version) AS is_current,
          s.key AS skill_id,json_extract(s.value,'$.version') AS skill_version,
          json_extract(s.value,'$.instructions_sha256') AS instructions_sha256,
          json_extract(s.value,'$.manifest_sha256') AS manifest_sha256,
          json_extract(s.value,'$.helper_sha256') AS helper_sha256,
          EXISTS(SELECT 1 FROM json_each(c.body,'$.loaded_skill_ids') l WHERE l.value=s.key) AS instructions_loaded,
          json_extract(s.value,'$.value_test_result_ref.id') AS value_test_result_id,
          'association_only' AS execution_basis
        FROM records c JOIN heads h ON h.kind=c.kind AND h.id=c.id
        JOIN json_each(c.body,'$.skills') s WHERE c.kind='agent_call'
    """,
    "call_tools": """
        SELECT c.id AS call_id,c.version AS manifest_revision,(h.version=c.version) AS is_current,
          t.key AS tool_id,json_extract(t.value,'$.version') AS tool_version,
          json_extract(t.value,'$.schema_sha256') AS schema_sha256,
          json_extract(t.value,'$.implementation_ref') AS implementation_ref,
          json_extract(t.value,'$.permission') AS permission,
          json_extract(t.value,'$.side_effects') AS side_effects,
          1 AS permitted,
          EXISTS(SELECT 1 FROM json_each(c.body,'$.exposed_tools') e WHERE e.value=t.key) AS exposed,
          (SELECT json_extract(d.value,'$.local_handler_available') FROM json_each(g.body,'$.catalog_snapshot') d WHERE d.key=t.key) AS local_handler_available,
          'association_only' AS execution_basis
        FROM records c JOIN heads h ON h.kind=c.kind AND h.id=c.id
        JOIN json_each(c.body,'$.permitted_tools') t
        LEFT JOIN records g ON g.kind='agent_configuration' AND g.id=json_extract(c.body,'$.configuration_id') AND g.version=1
        WHERE c.kind='agent_call'
    """,
    "invocation_overview": """
        SELECT r.kind,r.id AS invocation_id,r.version AS record_revision,(h.version=r.version) AS is_current,
          json_extract(r.body,'$.mission_id') AS mission_id,json_extract(r.body,'$.call_id') AS call_id,
          json_extract(r.body,'$.manifest_version') AS manifest_revision,
          coalesce(json_extract(r.body,'$.tool_id'),json_extract(r.body,'$.skill_id')) AS capability_id,
          json_extract(r.body,'$.status') AS execution_status,
          json_extract(r.body,'$.arguments_sha256') AS arguments_sha256,
          json_extract(r.body,'$.result_sha256') AS result_sha256,
          r.created_at AS receipt_created_at,o.proof_basis,o.timing_basis,
          'saved_invocation_receipt' AS execution_basis
        FROM records r JOIN heads h ON h.kind=r.kind AND h.id=r.id
        LEFT JOIN record_origins o ON o.kind=r.kind AND o.id=r.id AND o.version=r.version
        WHERE r.kind IN ('capability_invocation','skill_invocation')
    """,
    "retrieval_overview": """
        SELECT r.id AS delivery_id,r.version AS record_revision,(h.version=r.version) AS is_current,
          json_extract(r.body,'$.envelope.request.missionId') AS mission_id,
          json_extract(r.body,'$.envelope.request.taskId') AS task_id,
          json_extract(r.body,'$.envelope.request.agentInstanceId') AS instance_id,
          json_extract(r.body,'$.envelope.request.requestId') AS request_id,
          json_extract(r.body,'$.envelope.request.attemptId') AS attempt_id,
          json_extract(r.body,'$.envelope.request.provider') AS provider,
          json_extract(r.body,'$.envelope.request.route') AS route,
          json_extract(r.body,'$.envelope.request.operation') AS operation,
          json_extract(r.body,'$.envelope.request.querySha256') AS query_sha256,
          '$.envelope.request.query' AS private_query_record_path,
          json_extract(r.body,'$.envelope.outcome') AS outcome,
          json_extract(r.body,'$.envelope.mappingVersion') AS mapping_version,
          json_extract(r.body,'$.envelope.returnedWindow.acceptedCount') AS accepted_count,
          json_extract(r.body,'$.envelope.returnedWindow.providerTotal') AS provider_total,
          json_extract(r.body,'$.envelope.returnedWindow.moreAvailable') AS more_available,
          json_extract(r.body,'$.envelope.returnedWindow.providerStopReason') AS provider_stop_reason,
          json_array_length(r.body,'$.envelope.usage') AS usage_entry_count,
          json_extract(r.body,'$.envelope.retrievedAt') AS retrieved_at,o.proof_basis,o.timing_basis
        FROM records r JOIN heads h ON h.kind=r.kind AND h.id=r.id
        LEFT JOIN record_origins o ON o.kind=r.kind AND o.id=r.id AND o.version=r.version
        WHERE r.kind='retrieval_delivery'
    """,
    "retrieval_usage": """
        SELECT r.id AS delivery_id,r.version AS record_revision,u.key AS usage_position,
          json_extract(u.value,'$.unit') AS unit,json_extract(u.value,'$.amount') AS amount,
          json_extract(u.value,'$.basis') AS usage_basis
        FROM records r JOIN json_each(r.body,'$.envelope.usage') u WHERE r.kind='retrieval_delivery'
    """,
    "source_overview": """
        SELECT s.id AS copy_id,s.version AS record_revision,(h.version=s.version) AS is_current,
          json_extract(s.body,'$.publication.id') AS publication_id,
          json_extract(s.body,'$.publication.version') AS publication_revision,
          json_extract(p.body,'$.titles[0]') AS publication_title,
          json_extract(s.body,'$.encounter.id') AS encounter_id,
          json_extract(s.body,'$.encounter.version') AS encounter_revision,
          json_extract(s.body,'$.source_version') AS document_version,
          json_extract(s.body,'$.access_surface') AS access_surface,
          json_extract(s.body,'$.rights_basis') AS rights_basis,
          json_extract(s.body,'$.inspection_state') AS inspection_state,
          json_extract(s.body,'$.validation_record') AS validation_record,
          json_extract(s.body,'$.retain_bytes') AS retained_content,
          json_extract(s.body,'$.content_sha256') AS content_sha256,
          json_extract(s.body,'$.mapping_version') AS mapping_version,
          json_extract(s.body,'$.scientific_metadata.publication_date') AS publication_date,
          json_extract(s.body,'$.scientific_metadata.venue') AS venue,
          json_extract(s.body,'$.scientific_metadata.language') AS language,
          json_extract(s.body,'$.retrieved_at') AS retrieved_at
        FROM records s JOIN heads h ON h.kind=s.kind AND h.id=s.id
        LEFT JOIN records p ON p.kind='publication' AND p.id=json_extract(s.body,'$.publication.id') AND p.version=json_extract(s.body,'$.publication.version')
        WHERE s.kind='source_copy'
    """,
    "record_dependencies": """
        SELECT source_kind,source_id,source_version,relation,target_kind,target_id,target_version FROM links
    """,
}


def migration_statements() -> list[str]:
    return [f"CREATE VIEW {name} AS {statement}" for name, statement in VIEW_SQL.items()]
