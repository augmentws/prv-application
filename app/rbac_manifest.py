import uuid
from dataclasses import dataclass

RBAC_NAMESPACE = uuid.UUID("c803b7f5-9fe8-4af3-b68a-e9900dfbe192")


@dataclass(frozen=True)
class PermissionSpec:
    key: str
    display_name: str
    description: str
    category: str
    resource_type: str
    dangerous: bool = False

    @property
    def id(self) -> uuid.UUID:
        return uuid.uuid5(RBAC_NAMESPACE, f"permission:{self.key}")


@dataclass(frozen=True)
class SystemRoleSpec:
    key: str
    display_name: str
    description: str
    permission_keys: frozenset[str]

    @property
    def id(self) -> uuid.UUID:
        return uuid.uuid5(RBAC_NAMESPACE, f"system-role:{self.key}")


def _permission(
    key: str,
    category: str,
    resource_type: str,
    *,
    dangerous: bool = False,
) -> PermissionSpec:
    display_name = key.split(".", 1)[1].replace("_", " ").title()
    return PermissionSpec(
        key=key,
        display_name=display_name,
        description=f"Allows {key.replace('.', ' ').replace('_', ' ')}.",
        category=category,
        resource_type=resource_type,
        dangerous=dangerous,
    )


PERMISSIONS = (
    _permission("tenant.view", "Tenant", "TENANT"),
    _permission("tenant.configure", "Tenant", "TENANT", dangerous=True),
    _permission("tenant.manage_users", "Tenant", "TENANT", dangerous=True),
    _permission("tenant.manage_roles", "Tenant", "TENANT", dangerous=True),
    _permission("tenant.manage_groups", "Tenant", "TENANT", dangerous=True),
    _permission("tenant.view_audit", "Tenant", "TENANT"),
    _permission("tenant.view_usage", "Tenant", "TENANT"),
    _permission("client.view", "Client", "CLIENT"),
    _permission("client.configure", "Client", "CLIENT", dangerous=True),
    _permission("client.manage_members", "Client", "CLIENT", dangerous=True),
    _permission("client.manage_custodians", "Client", "CLIENT"),
    _permission("collection.view", "Collection", "COLLECTION"),
    _permission("collection.create", "Collection", "CLIENT"),
    _permission("collection.configure", "Collection", "COLLECTION"),
    _permission("collection.ingest", "Collection", "COLLECTION"),
    _permission("collection.process", "Collection", "COLLECTION"),
    _permission("collection.publish", "Collection", "COLLECTION", dangerous=True),
    _permission("collection.delete", "Collection", "COLLECTION", dangerous=True),
    _permission("matter.view", "Matter", "MATTER"),
    _permission("matter.configure", "Matter", "MATTER", dangerous=True),
    _permission("matter.manage_access", "Matter", "MATTER", dangerous=True),
    _permission("metadata.view", "Metadata", "MATTER"),
    _permission("metadata.manage", "Metadata", "MATTER", dangerous=True),
    _permission("document.view", "Documents", "MATTER"),
    _permission("document.review", "Documents", "MATTER"),
    _permission("document.view_history", "Documents", "MATTER"),
    _permission("document.bulk_code", "Documents", "MATTER", dangerous=True),
    _permission("document.import", "Documents", "MATTER", dangerous=True),
    _permission("document.export", "Documents", "MATTER", dangerous=True),
    _permission("batch.view", "Review batches", "MATTER"),
    _permission("batch.create", "Review batches", "MATTER"),
    _permission("batch.configure", "Review batches", "MATTER"),
    _permission("batch.assign", "Review batches", "MATTER"),
    _permission("batch.review", "Review batches", "MATTER"),
    _permission("batch.run_analysis", "Review batches", "MATTER"),
    _permission("batch.delete", "Review batches", "MATTER", dangerous=True),
    _permission("batch.publish_coding", "Review batches", "MATTER", dangerous=True),
    _permission("batch.publish_analysis", "Review batches", "MATTER", dangerous=True),
    _permission("matter_definition.view", "Matter definition", "MATTER"),
    _permission("matter_definition.manage", "Matter definition", "MATTER"),
    _permission("matter_definition.publish", "Matter definition", "MATTER", dangerous=True),
    _permission("review_program.view", "Review programs", "MATTER"),
    _permission("review_program.manage", "Review programs", "MATTER", dangerous=True),
    _permission("analysis_task.view", "Analysis", "MATTER"),
    _permission("analysis_task.manage", "Analysis", "MATTER"),
    _permission("analysis_task.run", "Analysis", "MATTER"),
    _permission("job.view", "Processing", "MATTER"),
    _permission("job.start", "Processing", "MATTER"),
    _permission("job.cancel", "Processing", "MATTER", dangerous=True),
    _permission("job.delete", "Processing", "MATTER", dangerous=True),
    _permission("search.configure", "Search", "MATTER"),
    _permission("search.rebuild", "Search", "MATTER", dangerous=True),
    _permission("embedding.manage", "Processing", "MATTER"),
    _permission("classification.view", "Classification", "MATTER"),
    _permission("classification.train", "Classification", "MATTER"),
    _permission("classification.approve", "Classification", "MATTER", dangerous=True),
    _permission("classification.activate", "Classification", "MATTER", dangerous=True),
    _permission("classification.score", "Classification", "MATTER"),
    _permission("classification.publish", "Classification", "MATTER", dangerous=True),
    _permission("fact.view", "Facts", "MATTER"),
    _permission("fact.review", "Facts", "MATTER"),
    _permission("fact.export", "Facts", "MATTER", dangerous=True),
    _permission("entity.view", "Entities", "MATTER"),
    _permission("entity.manage", "Entities", "MATTER"),
)

PERMISSION_BY_KEY = {permission.key: permission for permission in PERMISSIONS}
ALL_PERMISSION_KEYS = frozenset(PERMISSION_BY_KEY)

TENANT_ADMINISTRATOR_PERMISSIONS = ALL_PERMISSION_KEYS
CLIENT_ADMINISTRATOR_PERMISSIONS = frozenset(
    key for key in ALL_PERMISSION_KEYS if not key.startswith("tenant.")
)
COLLECTION_MANAGER_PERMISSIONS = frozenset(
    {
        "client.view",
        "collection.view",
        "collection.create",
        "collection.configure",
        "collection.ingest",
        "collection.process",
        "collection.publish",
        "collection.delete",
    }
)
MATTER_ADMINISTRATOR_PERMISSIONS = frozenset(
    key
    for key in ALL_PERMISSION_KEYS
    if key.startswith(
        (
            "matter.",
            "metadata.",
            "document.",
            "batch.",
            "matter_definition.",
            "review_program.",
            "analysis_task.",
            "job.",
            "search.",
            "embedding.",
            "classification.",
            "fact.",
            "entity.",
        )
    )
)
REVIEWER_PERMISSIONS = frozenset(
    {
        "matter.view",
        "metadata.view",
        "document.view",
        "document.review",
        "document.view_history",
        "batch.view",
        "batch.review",
        "matter_definition.view",
        "review_program.view",
        "analysis_task.view",
        "fact.view",
        "fact.review",
        "entity.view",
        "entity.manage",
    }
)
ANALYSIS_OPERATOR_PERMISSIONS = frozenset(
    {
        "matter.view",
        "metadata.view",
        "document.view",
        "document.view_history",
        "batch.view",
        "batch.run_analysis",
        "matter_definition.view",
        "review_program.view",
        "analysis_task.view",
        "analysis_task.run",
        "job.view",
        "fact.view",
        "entity.view",
    }
)
CLASSIFICATION_MANAGER_PERMISSIONS = frozenset(
    {
        "matter.view",
        "metadata.view",
        "document.view",
        "document.view_history",
        "job.view",
        "classification.view",
        "review_program.view",
        "classification.train",
        "classification.approve",
        "classification.activate",
        "classification.score",
        "classification.publish",
    }
)
READ_ONLY_PERMISSIONS = frozenset(
    {
        "tenant.view",
        "tenant.view_audit",
        "tenant.view_usage",
        "client.view",
        "collection.view",
        "matter.view",
        "metadata.view",
        "document.view",
        "document.view_history",
        "batch.view",
        "matter_definition.view",
        "review_program.view",
        "analysis_task.view",
        "job.view",
        "classification.view",
        "fact.view",
        "entity.view",
    }
)

SYSTEM_ROLES = (
    SystemRoleSpec(
        key="ROOT_ADMINISTRATOR",
        display_name="Root Administrator",
        description="Platform bootstrap and recovery administrator.",
        permission_keys=ALL_PERMISSION_KEYS,
    ),
    SystemRoleSpec(
        key="TENANT_ADMINISTRATOR",
        display_name="Tenant Administrator",
        description="Full administration within one tenant.",
        permission_keys=TENANT_ADMINISTRATOR_PERMISSIONS,
    ),
    SystemRoleSpec(
        key="CLIENT_ADMINISTRATOR",
        display_name="Client Administrator",
        description="Client configuration and descendant administration.",
        permission_keys=CLIENT_ADMINISTRATOR_PERMISSIONS,
    ),
    SystemRoleSpec(
        key="COLLECTION_MANAGER",
        display_name="Collection Manager",
        description="Collection ingestion, processing, publication, and deletion.",
        permission_keys=COLLECTION_MANAGER_PERMISSIONS,
    ),
    SystemRoleSpec(
        key="MATTER_ADMINISTRATOR",
        display_name="Matter Administrator",
        description="Matter configuration, review, processing, and model administration.",
        permission_keys=MATTER_ADMINISTRATOR_PERMISSIONS,
    ),
    SystemRoleSpec(
        key="REVIEWER",
        display_name="Reviewer",
        description="Document and assigned-batch review access.",
        permission_keys=REVIEWER_PERMISSIONS,
    ),
    SystemRoleSpec(
        key="ANALYSIS_OPERATOR",
        display_name="Analysis Operator",
        description="Read access plus analysis execution without schema administration.",
        permission_keys=ANALYSIS_OPERATOR_PERMISSIONS,
    ),
    SystemRoleSpec(
        key="CLASSIFICATION_MANAGER",
        display_name="Classification Manager",
        description="Classification lifecycle and scoring administration.",
        permission_keys=CLASSIFICATION_MANAGER_PERMISSIONS,
    ),
    SystemRoleSpec(
        key="READ_ONLY",
        display_name="Read Only",
        description="Read-only access at the granted scope and descendants.",
        permission_keys=READ_ONLY_PERMISSIONS,
    ),
)

SYSTEM_ROLE_BY_KEY = {role.key: role for role in SYSTEM_ROLES}
