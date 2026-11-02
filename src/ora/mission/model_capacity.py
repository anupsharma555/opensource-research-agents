"""Add model-specific allowance lanes without discarding older quota debt."""

from ora.coordination.capacity import CapacityPolicy, CapacityScheduler
from ora.models.profiles import SUPPORTED_MODELS, model_quota_scope


def with_model_quota_scopes(store, policy: CapacityPolicy, domains) -> CapacityPolicy:
    """Retain legacy lanes and conservatively bridge their known or unknown work."""
    CapacityScheduler.add_resource_scopes(store, policy)
    for domain in sorted(set(domains)):
        if domain not in policy.quotas:
            continue  # An explicit policy may deliberately exclude a credential route.
        for model in sorted(SUPPORTED_MODELS):
            scope = model_quota_scope(domain, model)
            if scope in policy.quotas:
                continue
            updated = CapacityPolicy(
                policy.permits, {**policy.quotas, scope: policy.quotas[domain]},
                policy.review_reserve,
            )
            if store.get("capacity_config", "default") is not None:
                CapacityScheduler.reconcile_additive_quota_domain(
                    store, updated, from_scope=domain, to_scope=scope, model_id=model,
                )
            policy = updated
    return policy
