from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import Literal

BatchingPolicy = Literal["NONE", "AD_HOC_BATCH", "PARTITIONED_SET", "WHOLE_MATTER"]


@dataclass(frozen=True)
class ReviewWorkflowPhaseSpec:
    key: str
    name: str
    ordinal: int


@dataclass(frozen=True)
class ReviewWorkflowStageSpec:
    key: str
    name: str
    phase_key: str
    ordinal: int
    batching_policy: BatchingPolicy
    requires_human_gate: bool = False


@dataclass(frozen=True)
class ReviewWorkflowTemplateSpec:
    key: str
    version: int
    schema_version: int
    name: str
    description: str
    workflow_type: str
    phases: tuple[ReviewWorkflowPhaseSpec, ...]
    stages: tuple[ReviewWorkflowStageSpec, ...]

    def contract(self) -> dict[str, object]:
        return {
            "key": self.key,
            "version": self.version,
            "schema_version": self.schema_version,
            "name": self.name,
            "description": self.description,
            "workflow_type": self.workflow_type,
            "phases": [asdict(phase) for phase in self.phases],
            "stages": [asdict(stage) for stage in self.stages],
        }

    @property
    def content_hash(self) -> str:
        payload = json.dumps(self.contract(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


FIRST_PASS_REVIEW_V1 = ReviewWorkflowTemplateSpec(
    key="FIRST_PASS_REVIEW",
    version=1,
    schema_version=1,
    name="First Pass Review",
    description="Guided first-pass issue coding, classifier development, collection review, and quality control.",
    workflow_type="FIRST_PASS_REVIEW",
    phases=(
        ReviewWorkflowPhaseSpec("MATTER_DEFINITION", "Matter definition", 1),
        ReviewWorkflowPhaseSpec("TRAINING_SET", "Training-set development", 2),
        ReviewWorkflowPhaseSpec("CLASSIFIER", "Classifier review", 3),
        ReviewWorkflowPhaseSpec("COLLECTION_REVIEW", "Collection review", 4),
        ReviewWorkflowPhaseSpec("QC_COMPLETION", "QC and completion", 5),
    ),
    stages=(
        ReviewWorkflowStageSpec(
            "MATTER_DEFINITION", "Matter definition", "MATTER_DEFINITION", 1, "NONE", True
        ),
        ReviewWorkflowStageSpec(
            "DEFINITION_ASSESSMENT", "Definition assessment", "MATTER_DEFINITION", 2, "AD_HOC_BATCH"
        ),
        ReviewWorkflowStageSpec(
            "DEFINITION_REFINEMENT", "Definition refinement", "MATTER_DEFINITION", 3, "NONE", True
        ),
        ReviewWorkflowStageSpec(
            "DEFINITION_ACCEPTANCE", "Definition acceptance", "MATTER_DEFINITION", 4, "NONE", True
        ),
        ReviewWorkflowStageSpec("QA_TASK_SETUP", "Q&A task setup", "TRAINING_SET", 5, "NONE", True),
        ReviewWorkflowStageSpec(
            "DISCOVERY_QA", "Discovery Q&A", "TRAINING_SET", 6, "PARTITIONED_SET", True
        ),
        ReviewWorkflowStageSpec("DISCOVERY_ANALYSIS", "Discovery analysis", "TRAINING_SET", 7, "NONE"),
        ReviewWorkflowStageSpec(
            "ISSUE_SUPPORT_DECISION", "Issue support decision", "TRAINING_SET", 8, "NONE", True
        ),
        ReviewWorkflowStageSpec(
            "DISAGREEMENT_REVIEW", "Difference review", "TRAINING_SET", 9, "AD_HOC_BATCH", True
        ),
        ReviewWorkflowStageSpec(
            "TRAINING_LABEL_FINALIZATION", "Training-label finalization", "TRAINING_SET", 10, "NONE", True
        ),
        ReviewWorkflowStageSpec("CLASSIFIER_TRAINING", "Classifier training", "CLASSIFIER", 11, "NONE"),
        ReviewWorkflowStageSpec("CLASSIFIER_REVIEW", "Classifier review", "CLASSIFIER", 12, "NONE", True),
        ReviewWorkflowStageSpec(
            "CLASSIFIER_IMPROVEMENT", "Classifier improvement", "CLASSIFIER", 13, "PARTITIONED_SET", True
        ),
        ReviewWorkflowStageSpec(
            "WHOLE_COLLECTION_SCORING", "Whole-collection scoring", "COLLECTION_REVIEW", 14, "WHOLE_MATTER", True
        ),
        ReviewWorkflowStageSpec(
            "CLASSIFIER_POSITIVE_COVERAGE",
            "Classifier-positive Q&A coverage",
            "COLLECTION_REVIEW",
            15,
            "PARTITIONED_SET",
            True,
        ),
        ReviewWorkflowStageSpec("FINAL_ALIGNMENT", "Final coding alignment", "COLLECTION_REVIEW", 16, "NONE"),
        ReviewWorkflowStageSpec(
            "NO_CODE_QC", "Negative-population QC", "QC_COMPLETION", 17, "PARTITIONED_SET", True
        ),
        ReviewWorkflowStageSpec("COMPLETION", "Completion", "QC_COMPLETION", 18, "NONE", True),
    ),
)


FIRST_PASS_REVIEW_V2 = replace(
    FIRST_PASS_REVIEW_V1,
    version=2,
    stages=tuple(
        replace(stage, batching_policy="AD_HOC_BATCH")
        if stage.key == "CLASSIFIER_POSITIVE_COVERAGE"
        else stage
        for stage in FIRST_PASS_REVIEW_V1.stages
    ),
)


FIRST_PASS_REVIEW = replace(
    FIRST_PASS_REVIEW_V2,
    version=3,
    stages=tuple(
        replace(stage, batching_policy="AD_HOC_BATCH")
        if stage.key == "NO_CODE_QC"
        else stage
        for stage in FIRST_PASS_REVIEW_V2.stages
    ),
)

PRIVILEGE_REVIEW_V1 = ReviewWorkflowTemplateSpec(
    key="PRIVILEGE_REVIEW",
    version=1,
    schema_version=1,
    name="Privilege Review",
    description=(
        "Family-aware privilege review of a frozen responsive population, with identity context, "
        "evidence, adjudication, human review, exceptions, and production clearance."
    ),
    workflow_type="PRIVILEGE_REVIEW",
    phases=(
        ReviewWorkflowPhaseSpec("FOUNDATION", "Program foundation", 1),
        ReviewWorkflowPhaseSpec("ANALYSIS", "Evidence and adjudication", 2),
        ReviewWorkflowPhaseSpec("REVIEW", "Human review and exceptions", 3),
        ReviewWorkflowPhaseSpec("QC_COMPLETION", "QC and production clearance", 4),
    ),
    stages=(
        ReviewWorkflowStageSpec("PRIVILEGE_FOUNDATION", "Frozen program inputs", "FOUNDATION", 1, "NONE", True),
        ReviewWorkflowStageSpec("FAMILY_CONTEXT", "Family context", "FOUNDATION", 2, "PARTITIONED_SET"),
        ReviewWorkflowStageSpec("PRIVILEGE_EVIDENCE", "Privilege evidence", "ANALYSIS", 3, "PARTITIONED_SET"),
        ReviewWorkflowStageSpec("PRIVILEGE_ADJUDICATION", "Privilege adjudication", "ANALYSIS", 4, "PARTITIONED_SET"),
        ReviewWorkflowStageSpec("PRIVILEGE_HUMAN_REVIEW", "Configured human review", "REVIEW", 5, "AD_HOC_BATCH", True),
        ReviewWorkflowStageSpec("PRIVILEGE_EXCEPTIONS", "Exception resolution", "REVIEW", 6, "AD_HOC_BATCH", True),
        ReviewWorkflowStageSpec("PRIVILEGE_QC", "Privilege QC", "QC_COMPLETION", 7, "AD_HOC_BATCH", True),
        ReviewWorkflowStageSpec("PRIVILEGE_CLEARANCE", "Production clearance", "QC_COMPLETION", 8, "NONE", True),
    ),
)

PRIVILEGE_REVIEW = ReviewWorkflowTemplateSpec(
    key="PRIVILEGE_REVIEW",
    version=2,
    schema_version=1,
    name="Privilege Review",
    description=(
        "Family-aware privilege review of a frozen responsive population, with assessed guidance, identity context, "
        "evidence, adjudication, human review, exceptions, and production clearance."
    ),
    workflow_type="PRIVILEGE_REVIEW",
    phases=(
        ReviewWorkflowPhaseSpec("FOUNDATION", "Program foundation", 1),
        ReviewWorkflowPhaseSpec("MATTER_DEFINITION", "Matter definition", 2),
        ReviewWorkflowPhaseSpec("ANALYSIS", "Evidence and adjudication", 3),
        ReviewWorkflowPhaseSpec("REVIEW", "Human review and exceptions", 4),
        ReviewWorkflowPhaseSpec("QC_COMPLETION", "QC and production clearance", 5),
    ),
    stages=(
        ReviewWorkflowStageSpec("PRIVILEGE_FOUNDATION", "Frozen program inputs", "FOUNDATION", 1, "NONE", True),
        ReviewWorkflowStageSpec("FAMILY_CONTEXT", "Family context", "FOUNDATION", 2, "PARTITIONED_SET"),
        ReviewWorkflowStageSpec(
            "MATTER_DEFINITION", "Matter definition", "MATTER_DEFINITION", 3, "NONE", True
        ),
        ReviewWorkflowStageSpec(
            "DEFINITION_ASSESSMENT", "Definition assessment", "MATTER_DEFINITION", 4, "AD_HOC_BATCH"
        ),
        ReviewWorkflowStageSpec(
            "DEFINITION_REFINEMENT", "Definition refinement", "MATTER_DEFINITION", 5, "NONE", True
        ),
        ReviewWorkflowStageSpec(
            "DEFINITION_ACCEPTANCE", "Definition acceptance", "MATTER_DEFINITION", 6, "NONE", True
        ),
        ReviewWorkflowStageSpec(
            "PRIVILEGE_EVIDENCE", "Privilege evidence", "ANALYSIS", 7, "PARTITIONED_SET", True
        ),
        ReviewWorkflowStageSpec("PRIVILEGE_ADJUDICATION", "Privilege adjudication", "ANALYSIS", 8, "PARTITIONED_SET"),
        ReviewWorkflowStageSpec(
            "PRIVILEGE_HUMAN_REVIEW", "Configured human review", "REVIEW", 9, "AD_HOC_BATCH", True
        ),
        ReviewWorkflowStageSpec(
            "PRIVILEGE_EXCEPTIONS", "Exception resolution", "REVIEW", 10, "AD_HOC_BATCH", True
        ),
        ReviewWorkflowStageSpec("PRIVILEGE_QC", "Privilege QC", "QC_COMPLETION", 11, "AD_HOC_BATCH", True),
        ReviewWorkflowStageSpec(
            "PRIVILEGE_CLEARANCE", "Production clearance", "QC_COMPLETION", 12, "NONE", True
        ),
    ),
)


REVIEW_WORKFLOW_TEMPLATE_VERSIONS = {
    (FIRST_PASS_REVIEW_V1.key, FIRST_PASS_REVIEW_V1.version): FIRST_PASS_REVIEW_V1,
    (FIRST_PASS_REVIEW_V2.key, FIRST_PASS_REVIEW_V2.version): FIRST_PASS_REVIEW_V2,
    (FIRST_PASS_REVIEW.key, FIRST_PASS_REVIEW.version): FIRST_PASS_REVIEW,
    (PRIVILEGE_REVIEW_V1.key, PRIVILEGE_REVIEW_V1.version): PRIVILEGE_REVIEW_V1,
    (PRIVILEGE_REVIEW.key, PRIVILEGE_REVIEW.version): PRIVILEGE_REVIEW,
}
REVIEW_WORKFLOW_TEMPLATES = {
    FIRST_PASS_REVIEW.key: FIRST_PASS_REVIEW,
    PRIVILEGE_REVIEW.key: PRIVILEGE_REVIEW,
}


class ReviewWorkflowTemplateError(ValueError):
    pass


def get_review_workflow_template(key: str, *, version: int | None = None) -> ReviewWorkflowTemplateSpec:
    template = (
        REVIEW_WORKFLOW_TEMPLATES.get(key)
        if version is None
        else REVIEW_WORKFLOW_TEMPLATE_VERSIONS.get((key, version))
    )
    if template is None:
        raise ReviewWorkflowTemplateError("Review Workflow Template not found")
    return template


def validate_review_workflow_templates() -> None:
    for template in REVIEW_WORKFLOW_TEMPLATE_VERSIONS.values():
        phase_keys = {phase.key for phase in template.phases}
        if len(phase_keys) != len(template.phases):
            raise RuntimeError(f"Review Workflow Template {template.key} has duplicate phase keys")
        if [phase.ordinal for phase in template.phases] != list(range(1, len(template.phases) + 1)):
            raise RuntimeError(f"Review Workflow Template {template.key} has invalid phase ordinals")
        stage_keys = {stage.key for stage in template.stages}
        if len(stage_keys) != len(template.stages):
            raise RuntimeError(f"Review Workflow Template {template.key} has duplicate stage keys")
        if [stage.ordinal for stage in template.stages] != list(range(1, len(template.stages) + 1)):
            raise RuntimeError(f"Review Workflow Template {template.key} has invalid stage ordinals")
        if any(stage.phase_key not in phase_keys for stage in template.stages):
            raise RuntimeError(f"Review Workflow Template {template.key} references an unknown phase")

        ad_hoc_stages = {stage.key for stage in template.stages if stage.batching_policy == "AD_HOC_BATCH"}
        expected_ad_hoc_stages = {
            "DEFINITION_ASSESSMENT",
            "DISAGREEMENT_REVIEW",
            *({"CLASSIFIER_POSITIVE_COVERAGE"} if template.version >= 2 else set()),
            *({"NO_CODE_QC"} if template.version >= 3 else set()),
        }
        if template.key == "FIRST_PASS_REVIEW" and ad_hoc_stages != expected_ad_hoc_stages:
            raise RuntimeError("First Pass Review has an invalid ad hoc batching contract")


validate_review_workflow_templates()
