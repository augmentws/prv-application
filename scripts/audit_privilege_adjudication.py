"""Audit privilege-adjudication false negatives against a Boolean truth field."""

from __future__ import annotations

import argparse
import json
import uuid
from collections import Counter, defaultdict
from email.utils import getaddresses
from io import BytesIO
from typing import Any

from sqlalchemy import select

from app.artifact_gateway import copy_workflow_artifact_content, get_workflow_artifact_reference
from app.database import SessionLocal
from app.document_metadata import event_value
from app.evidence_adjudication_artifacts import load_criterion_decisions_bytes
from app.evidence_first_artifacts import load_compacted_observations_bytes
from app.models import (
    DocumentMetadataCurrent,
    Matter,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewBatchRunValue,
    ReviewProgram,
    ReviewProgramVersion,
    WorkflowRun,
)
from app.privilege_evidence_context import load_privilege_evidence_packets


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-batch-run-id", type=uuid.UUID, required=True)
    parser.add_argument("--truth-metadata-definition-id", type=uuid.UUID, required=True)
    parser.add_argument("--evidence-program-id", type=uuid.UUID, required=True)
    parser.add_argument("--sample-per-path", type=int, default=3)
    return parser.parse_args()


def _artifact_bytes(
    artifact_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> bytes:
    reference = get_workflow_artifact_reference(
        artifact_id=artifact_id,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    destination = BytesIO()
    copy_workflow_artifact_content(
        artifact_id=reference.artifact_id,
        destination=destination,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    return destination.getvalue()


def main() -> None:
    arguments = _arguments()
    with SessionLocal() as db:
        run = db.get(ReviewBatchRun, arguments.review_batch_run_id)
        evidence_program = db.get(ReviewProgram, arguments.evidence_program_id)
        if run is None or evidence_program is None or evidence_program.current_version_id is None:
            raise ValueError("Review run or evidence program is unavailable")
        version = db.get(ReviewProgramVersion, evidence_program.current_version_id)
        matter = db.get(Matter, evidence_program.matter_id)
        if version is None or matter is None:
            raise ValueError("Evidence program version or matter is unavailable")
        reference = (version.configuration_snapshot or {}).get("privilege_review", {}).get(
            "evidence_packets"
        )
        if not isinstance(reference, dict):
            raise TypeError("Evidence program has no frozen evidence packets")

        predictions = {
            row.matter_document_id: str(event_value(row))
            for row in db.scalars(
                select(ReviewBatchRunValue).where(
                    ReviewBatchRunValue.review_batch_run_id == run.id
                )
            )
        }
        truth = {
            row.matter_document_id: bool(event_value(row))
            for row in db.scalars(
                select(DocumentMetadataCurrent).where(
                    DocumentMetadataCurrent.matter_id == matter.id,
                    DocumentMetadataCurrent.metadata_definition_id
                    == arguments.truth_metadata_definition_id,
                    DocumentMetadataCurrent.resolution_state == "VALUE",
                )
            )
        }
        false_negative_ids = sorted(
            document_id
            for document_id, prediction in predictions.items()
            if prediction == "not_privileged" and truth.get(document_id) is True
        )
        cleared_negative_ids = sorted(
            document_id
            for document_id, prediction in predictions.items()
            if prediction == "not_privileged" and truth.get(document_id) is False
        )
        audited_document_ids = [*false_negative_ids, *cleared_negative_ids]
        run_documents = {
            row.matter_document_id: row
            for row in db.scalars(
                select(ReviewBatchRunDocument).where(
                    ReviewBatchRunDocument.review_batch_run_id == run.id,
                    ReviewBatchRunDocument.matter_document_id.in_(audited_document_ids),
                )
            )
        }
        child_workflows = {
            row.id: row
            for row in db.scalars(
                select(WorkflowRun).where(
                    WorkflowRun.id.in_(
                        [
                            row.last_workflow_run_id
                            for row in run_documents.values()
                            if row.last_workflow_run_id is not None
                        ]
                    )
                )
            )
        }
        actor_user_id = run.initiated_by_user_id
        tenant_id = evidence_program.tenant_id
        client_id = matter.client_id
        matter_id = matter.id
        db.commit()

    packets = load_privilege_evidence_packets(
        packet_reference=reference,
        matter_id=matter_id,
        program_id=evidence_program.id,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    identity_reference = packets.get("source_identity_context")
    identity_context = (
        json.loads(
            _artifact_bytes(
                uuid.UUID(str(identity_reference["artifact_id"])),
                actor_user_id=actor_user_id,
                tenant_id=tenant_id,
                client_id=client_id,
            )
        )
        if isinstance(identity_reference, dict) and identity_reference.get("artifact_id")
        else {"entities": []}
    )
    member_by_document: dict[uuid.UUID, dict[str, Any]] = {}
    family_documents: dict[str, set[uuid.UUID]] = defaultdict(set)
    family_members: dict[str, list[dict[str, Any]]] = defaultdict(list)
    family_anchor: dict[str, uuid.UUID] = {}
    for packet in packets.get("packets", []):
        family_key = str(packet["family_key"])
        members = packet.get("members", [])
        for member in members:
            if isinstance(member, dict) and member not in family_members[family_key]:
                family_members[family_key].append(member)
            try:
                document_id = uuid.UUID(str(member["matter_document_id"]))
            except (KeyError, TypeError, ValueError):
                # Context-only family members may be retained in the evidence
                # packet without having a MatterDocument review target.
                continue
            family_documents[family_key].add(document_id)
            member_by_document[document_id] = {**member, "family_key": family_key}
            if member.get("relationship_role") == "PARENT":
                family_anchor[family_key] = document_id
        if family_key not in family_anchor and family_documents[family_key]:
            family_anchor[family_key] = min(family_documents[family_key])

    observation_cache: dict[uuid.UUID, Any] = {}
    decision_cache: dict[uuid.UUID, Any] = {}
    role_counts: Counter[str] = Counter()
    family_truth_counts: Counter[str] = Counter()
    family_size_counts: Counter[str] = Counter()
    disposition_counts: dict[str, Counter[str]] = defaultdict(Counter)
    signal_document_counts: dict[str, Counter[str]] = defaultdict(Counter)
    path_counts: Counter[str] = Counter()
    family_recovery_counts: Counter[str] = Counter()
    identity_availability_counts: Counter[str] = Counter()
    identity_match_counts: Counter[str] = Counter()
    identity_citation_counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    false_negative_near_miss_count = 0

    for document_id in false_negative_ids:
        run_document = run_documents[document_id]
        workflow = child_workflows[run_document.last_workflow_run_id]
        observations_artifact_id = uuid.UUID(str(workflow.input_snapshot["observations_artifact_id"]))
        decisions_artifact_id = uuid.UUID(
            str(workflow.progress["criterion_decisions_artifact_id"])
        )
        if observations_artifact_id not in observation_cache:
            observation_cache[observations_artifact_id] = load_compacted_observations_bytes(
                _artifact_bytes(
                    observations_artifact_id,
                    actor_user_id=actor_user_id,
                    tenant_id=tenant_id,
                    client_id=client_id,
                )
            )
        if decisions_artifact_id not in decision_cache:
            decision_cache[decisions_artifact_id] = load_criterion_decisions_bytes(
                _artifact_bytes(
                    decisions_artifact_id,
                    actor_user_id=actor_user_id,
                    tenant_id=tenant_id,
                    client_id=client_id,
                )
            )
        observations = observation_cache[observations_artifact_id]
        decisions = decision_cache[decisions_artifact_id]
        prefix = f"document:{document_id}:"
        scoped_observations = [
            value
            for value in observations.observations
            if any(citation.startswith(prefix) for citation in value.citation_ids)
        ]
        signals_by_criterion: dict[str, Counter[str]] = defaultdict(Counter)
        for observation in scoped_observations:
            signals_by_criterion[observation.criterion_key][observation.signal_type] += 1
        dispositions = {
            decision.criterion_key: decision.disposition
            for decision in decisions.criterion_decisions
        }
        for criterion_key, disposition in dispositions.items():
            disposition_counts[criterion_key][disposition] += 1
        for criterion_key, counts in signals_by_criterion.items():
            for signal_type in counts:
                signal_document_counts[criterion_key][signal_type] += 1

        all_dispositions = set(dispositions.values())
        if "NEAR_MISS" in all_dispositions:
            false_negative_near_miss_count += 1
        total_support = sum(counts.get("SUPPORT", 0) for counts in signals_by_criterion.values())
        near_miss_keys = {
            key for key, disposition in dispositions.items() if disposition == "NEAR_MISS"
        }
        near_miss_support = sum(
            signals_by_criterion[key].get("SUPPORT", 0) for key in near_miss_keys
        )
        if not scoped_observations:
            path = "no_target_observations"
        elif all_dispositions == {"NO_MATCH"} and total_support > 0:
            path = "support_rejected_as_no_match"
        elif all_dispositions == {"NO_MATCH"}:
            path = "no_support_all_no_match"
        elif "NEAR_MISS" in all_dispositions and near_miss_support == 0:
            path = "near_miss_without_same_criterion_support"
        elif "NEAR_MISS" in all_dispositions:
            path = "near_miss_with_support_unexpected_clearance"
        else:
            path = "other"
        path_counts[path] += 1

        member = member_by_document.get(document_id, {})
        family_key = str(member.get("family_key") or f"document:{document_id}")
        role = str(member.get("relationship_role") or "STANDALONE")
        role_counts[role] += 1
        family_ids = family_documents.get(family_key, {document_id})
        participant_values: list[str] = []
        for family_member in family_members.get(family_key, []):
            email = family_member.get("email")
            if not isinstance(email, dict):
                continue
            if email.get("sender"):
                participant_values.append(str(email["sender"]))
            recipients = email.get("recipients")
            if isinstance(recipients, dict):
                for raw_values in recipients.values():
                    if isinstance(raw_values, list):
                        participant_values.extend(str(value) for value in raw_values)
        participant_addresses = {
            address.casefold()
            for _name, address in getaddresses(participant_values)
            if address and "@" in address
        }
        participant_domains = {address.rsplit("@", 1)[1] for address in participant_addresses}
        if participant_addresses:
            identity_availability_counts["email_address_available"] += 1
        elif participant_values:
            identity_availability_counts["plain_names_only"] += 1
        else:
            identity_availability_counts["no_participant_values"] += 1
        matched_identity = False
        for entity in identity_context.get("entities", []):
            if not isinstance(entity, dict):
                continue
            entity_values = [entity.get("email")]
            entity_values.extend(
                variant.get("email")
                for variant in entity.get("variants", [])
                if isinstance(variant, dict)
            )
            entity_addresses = {
                address.casefold()
                for _name, address in getaddresses([str(value) for value in entity_values if value])
                if address and "@" in address
            }
            entity_domains = {
                str(value.get("normalized_domain") or value.get("domain") or "").casefold()
                for value in entity.get("domains", [])
                if isinstance(value, dict)
            }
            if participant_addresses.intersection(entity_addresses) or participant_domains.intersection(
                entity_domains
            ):
                matched_identity = True
                break
        identity_match_counts["matched_confirmed_entity" if matched_identity else "no_confirmed_entity_match"] += 1
        has_identity_citation = any(
            citation.startswith("identity:")
            for observation in scoped_observations
            for citation in observation.citation_ids
        )
        identity_citation_counts["identity_cited" if has_identity_citation else "identity_not_cited"] += 1
        family_truth = {truth.get(value) for value in family_ids if value in truth}
        if family_truth == {True}:
            family_truth_label = "all_positive"
        elif family_truth == {False}:
            family_truth_label = "all_negative"
        else:
            family_truth_label = "mixed"
        family_truth_counts[family_truth_label] += 1
        family_size = len(family_ids)
        family_size_counts[
            "1" if family_size == 1 else "2" if family_size == 2 else "3-5" if family_size <= 5 else "6+"
        ] += 1
        peer_recommendations = {
            predictions.get(value)
            for value in family_ids
            if value != document_id and predictions.get(value) is not None
        }
        if "privileged" in peer_recommendations:
            recoverability = "peer_privileged"
        elif "needs_review" in peer_recommendations:
            recoverability = "peer_needs_review"
        else:
            recoverability = "no_positive_or_review_peer"
        family_recovery_counts[recoverability] += 1

        if len(samples[path]) < arguments.sample_per_path:
            samples[path].append(
                {
                    "matter_document_id": str(document_id),
                    "family_key": family_key,
                    "family_role": role,
                    "family_size": family_size,
                    "family_truth": family_truth_label,
                    "family_recoverability": recoverability,
                    "record_type": member.get("record_type"),
                    "original_filename": member.get("original_filename"),
                    "subject": (member.get("email") or {}).get("subject"),
                    "dispositions": dispositions,
                    "signal_counts": {
                        key: dict(sorted(value.items()))
                        for key, value in sorted(signals_by_criterion.items())
                    },
                    "facts": [
                        {
                            "criterion_key": value.criterion_key,
                            "signal_type": value.signal_type,
                            "fact": value.fact,
                        }
                        for value in scoped_observations[:8]
                    ],
                }
            )

    cleared_negative_near_miss_count = 0
    for document_id in cleared_negative_ids:
        run_document = run_documents[document_id]
        workflow = child_workflows[run_document.last_workflow_run_id]
        decisions_artifact_id = uuid.UUID(
            str(workflow.progress["criterion_decisions_artifact_id"])
        )
        if decisions_artifact_id not in decision_cache:
            decision_cache[decisions_artifact_id] = load_criterion_decisions_bytes(
                _artifact_bytes(
                    decisions_artifact_id,
                    actor_user_id=actor_user_id,
                    tenant_id=tenant_id,
                    client_id=client_id,
                )
            )
        if any(
            decision.disposition == "NEAR_MISS"
            for decision in decision_cache[decisions_artifact_id].criterion_decisions
        ):
            cleared_negative_near_miss_count += 1

    family_routed_positive = 0
    family_routed_negative = 0
    for document_id, prediction in predictions.items():
        if prediction != "not_privileged" or document_id not in truth:
            continue
        member = member_by_document.get(document_id, {})
        family_key = str(member.get("family_key") or f"document:{document_id}")
        peer_recommendations = {
            predictions.get(peer_id)
            for peer_id in family_documents.get(family_key, {document_id})
            if peer_id != document_id
        }
        if not peer_recommendations.intersection({"privileged", "needs_review"}):
            continue
        if truth[document_id]:
            family_routed_positive += 1
        else:
            family_routed_negative += 1

    evaluated_ids = [document_id for document_id in predictions if document_id in truth]
    truth_positive_count = sum(truth[document_id] for document_id in evaluated_ids)
    baseline_held_ids = [
        document_id
        for document_id in evaluated_ids
        if predictions[document_id] in {"privileged", "needs_review"}
    ]
    baseline_held_positive = sum(truth[document_id] for document_id in baseline_held_ids)
    baseline_held_negative = len(baseline_held_ids) - baseline_held_positive

    def policy_metrics(*, recovered_positive: int, added_negative: int) -> dict[str, Any]:
        held_positive = baseline_held_positive + recovered_positive
        held_negative = baseline_held_negative + added_negative
        held_total = held_positive + held_negative
        return {
            "held_document_count": held_total,
            "held_positive_count": held_positive,
            "held_negative_count": held_negative,
            "false_negative_count": truth_positive_count - held_positive,
            "recall": held_positive / truth_positive_count if truth_positive_count else None,
            "held_precision": held_positive / held_total if held_total else None,
        }

    report = {
        "review_batch_run_id": str(arguments.review_batch_run_id),
        "truth_metadata_definition_id": str(arguments.truth_metadata_definition_id),
        "false_negative_count": len(false_negative_ids),
        "family_role_counts": dict(sorted(role_counts.items())),
        "family_truth_composition_counts": dict(sorted(family_truth_counts.items())),
        "family_size_counts": dict(sorted(family_size_counts.items())),
        "screening_path_counts": dict(sorted(path_counts.items())),
        "family_recoverability_counts": dict(sorted(family_recovery_counts.items())),
        "identity_availability_counts": dict(sorted(identity_availability_counts.items())),
        "identity_match_counts": dict(sorted(identity_match_counts.items())),
        "identity_citation_counts": dict(sorted(identity_citation_counts.items())),
        "screening_policy_simulations": {
            "current_affirmative_support": policy_metrics(
                recovered_positive=0,
                added_negative=0,
            ),
            "hold_any_near_miss": policy_metrics(
                recovered_positive=false_negative_near_miss_count,
                added_negative=cleared_negative_near_miss_count,
            ),
            "hold_if_family_peer_held": policy_metrics(
                recovered_positive=family_routed_positive,
                added_negative=family_routed_negative,
            ),
            "inputs": {
                "false_negative_near_miss_count": false_negative_near_miss_count,
                "cleared_negative_near_miss_count": cleared_negative_near_miss_count,
                "family_routed_positive_count": family_routed_positive,
                "family_routed_negative_count": family_routed_negative,
            },
        },
        "criterion_disposition_counts": {
            key: dict(sorted(value.items())) for key, value in sorted(disposition_counts.items())
        },
        "criterion_signal_document_counts": {
            key: dict(sorted(value.items()))
            for key, value in sorted(signal_document_counts.items())
        },
        "samples": dict(sorted(samples.items())),
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
