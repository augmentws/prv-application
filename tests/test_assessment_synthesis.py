import copy
import uuid

from app.assessment_synthesis import (
    assemble_synthesis_result,
    compile_synthesis_manifest,
    manifest_summary,
    normalize_unit_output,
    synthesis_unit_execution_limits,
    unit_instructions,
    validate_unit_output,
)

ATTORNEY_CLIENT = "attorney_client_privilege"
WORK_PRODUCT = "attorney_work_product"


def _retrieval_plan():
    return {
        "criteria": [
            {
                "criterion_key": ATTORNEY_CLIENT,
                "criterion_label": "Attorney-client privilege",
                "description": "Confidential legal advice communications.",
            },
            {
                "criterion_key": WORK_PRODUCT,
                "criterion_label": "Attorney work product",
                "description": "Material prepared in anticipation of litigation.",
            },
        ]
    }


def _analysis(document_id, matches):
    return {
        "matter_document_id": str(document_id),
        "analysis": {
            "determination": "PRIVILEGED",
            "confidence": 0.8,
            "criterion_matches": matches,
            "clarification_requests": [],
            "limitations": [],
        },
    }


def _match(key, match_type, paragraph, confidence=0.8):
    return {
        "criterion_key": key,
        "match_type": match_type,
        "confidence": confidence,
        "reasoning": f"Evidence for {key}",
        "citation_ids": [paragraph],
    }


def test_compile_manifest_canonicalizes_known_privilege_aliases():
    first = uuid.uuid4()
    second = uuid.uuid4()
    analyses = [
        _analysis(
            first,
            [
                _match("legal_advice_or_legal_services", "NEAR_MISS", "¶1"),
                _match("work_product", "NEAR_MISS", "¶2"),
            ],
        ),
        _analysis(
            second,
            [
                _match(ATTORNEY_CLIENT, "NEAR_MISS", "¶3"),
                _match("work_product_protection", "NEAR_MISS", "¶4"),
            ],
        ),
    ]

    manifest = compile_synthesis_manifest(
        analyses,
        {"selected_document_count": 2},
        _retrieval_plan(),
    )

    signals = [unit for unit in manifest["units"] if unit["unit_type"] == "SIGNAL_DISPOSITION"]
    assert [(unit["criterion_key"], unit["document_count"]) for unit in signals] == [
        (ATTORNEY_CLIENT, 2),
        (WORK_PRODUCT, 2),
    ]
    assert manifest["canonicalization"]["unmapped"] == []
    assert {
        (item["source_key"], item["canonical_key"], item["occurrence_count"])
        for item in manifest["canonicalization"]["aliases"]
    } == {
        ("legal_advice_or_legal_services", ATTORNEY_CLIENT, 1),
        ("work_product", WORK_PRODUCT, 1),
        ("work_product_protection", WORK_PRODUCT, 1),
    }


def test_compile_manifest_creates_deterministic_assignments_and_preserves_unmapped_keys():
    document_id = uuid.uuid4()
    analyses = [
        _analysis(
            document_id,
            [
                _match(ATTORNEY_CLIENT, "SECONDARY", "¶2", confidence=0.6),
                _match("legal_advice_or_legal_services", "PRIMARY", "¶1", confidence=0.9),
                _match("novel_privilege_theory", "PRIMARY", "¶3"),
            ],
        )
    ]

    manifest = compile_synthesis_manifest(
        analyses,
        {"selected_document_count": 1},
        _retrieval_plan(),
    )
    topic = next(
        unit
        for unit in manifest["units"]
        if unit["unit_type"] == "TOPIC_FINDING" and unit["unit_key"] == ATTORNEY_CLIENT
    )

    assert topic["assignments"] == [
        {
            "matter_document_id": str(document_id),
            "confidence": 0.9,
            "match_type": "PRIMARY",
            "evidence": [
                {
                    "matter_document_id": str(document_id),
                    "paragraph_ids": ["¶2"],
                    "reason": f"Evidence for {ATTORNEY_CLIENT}",
                },
                {
                    "matter_document_id": str(document_id),
                    "paragraph_ids": ["¶1"],
                    "reason": "Evidence for legal_advice_or_legal_services",
                },
            ],
        }
    ]
    assert manifest["canonicalization"]["unmapped"] == [{"source_key": "novel_privilege_theory", "occurrence_count": 1}]
    assert manifest["unmapped_criterion_occurrences"] == [
        {
            "source_key": "novel_privilege_theory",
            "occurrences": [
                {
                    "matter_document_id": str(document_id),
                    "source_key": "novel_privilege_theory",
                    "label": "novel_privilege_theory",
                    "match_type": "PRIMARY",
                    "confidence": 0.8,
                    "evidence": [
                        {
                            "matter_document_id": str(document_id),
                            "paragraph_ids": ["¶3"],
                            "reason": "Evidence for novel_privilege_theory",
                        }
                    ],
                }
            ],
        }
    ]
    other = manifest["other_unmapped_criteria"]
    assert other["reporting_only"] is True
    assert other["occurrence_count"] == 1
    assert other["document_count"] == 1
    assert other["items"][0]["source_key"] == "novel_privilege_theory"
    catch_all = next(
        unit
        for unit in manifest["units"]
        if unit["unit_type"] == "SIGNAL_DISPOSITION" and unit["unit_key"] == "OTHER_UNMAPPED_CRITERIA"
    )
    assert catch_all["reporting_only"] is True
    assert catch_all["occurrence_count"] == 1


def test_compile_manifest_keeps_authorized_metadata_and_drops_other_metadata():
    document_id = uuid.uuid4()
    match = _match(ATTORNEY_CLIENT, "PRIMARY", "¶1")
    match["citation_ids"] = [
        "¶1",
        "metadata:email.sender",
        "metadata:email.subject",
        "metadata:email.sent_at",
        "metadata:file_date",
    ]

    manifest = compile_synthesis_manifest(
        [_analysis(document_id, [match])],
        {"selected_document_count": 1},
        _retrieval_plan(),
    )
    topic = next(
        unit
        for unit in manifest["units"]
        if unit["unit_type"] == "TOPIC_FINDING" and unit["unit_key"] == ATTORNEY_CLIENT
    )

    assert topic["assignments"][0]["evidence"][0]["paragraph_ids"] == [
        "¶1",
        "metadata:email.sender",
        "metadata:email.subject",
    ]


def test_assembly_drops_unsupported_metadata_from_a_legacy_manifest():
    document_id = uuid.uuid4()
    manifest = compile_synthesis_manifest(
        [_analysis(document_id, [_match(ATTORNEY_CLIENT, "PRIMARY", "¶1")])],
        {"selected_document_count": 1},
        _retrieval_plan(),
    )
    topic = next(
        unit
        for unit in manifest["units"]
        if unit["unit_type"] == "TOPIC_FINDING" and unit["unit_key"] == ATTORNEY_CLIENT
    )
    topic["assignments"][0]["evidence"][0]["paragraph_ids"].extend(
        ["metadata:email.sender", "metadata:email.sent_at"]
    )

    result, _, _ = assemble_synthesis_result(manifest, {}, [])
    evidence = result["topics"][0]["assignments"][0]["evidence"][0]

    assert evidence["paragraph_ids"] == ["¶1", "metadata:email.sender"]


def test_obvious_aliases_map_before_the_generic_catch_all():
    document_id = uuid.uuid4()
    manifest = compile_synthesis_manifest(
        [
            _analysis(
                document_id,
                [
                    _match("attorney_client", "PRIMARY", "¶1"),
                    _match("work_product_litigation", "SECONDARY", "¶2"),
                ],
            )
        ],
        {"selected_document_count": 1},
        _retrieval_plan(),
    )

    assert manifest["canonicalization"]["unmapped"] == []
    assert manifest["other_unmapped_criteria"]["items"] == []
    assert {(item["source_key"], item["canonical_key"]) for item in manifest["canonicalization"]["aliases"]} == {
        ("attorney_client", ATTORNEY_CLIENT),
        ("work_product_litigation", WORK_PRODUCT),
    }


def test_unmapped_catch_all_is_reporting_only_and_can_propose_guidance_question():
    document_id = uuid.uuid4()
    manifest = compile_synthesis_manifest(
        [_analysis(document_id, [_match("novel_scope", "NEAR_MISS", "¶4")])],
        {"selected_document_count": 1},
        _retrieval_plan(),
    )
    output = {
        "signal_id": "OTHER_UNMAPPED_CRITERIA",
        "disposition": "QUESTION_PROPOSED",
        "rationale": "The unmatched label may describe a missing scope rule.",
        "question": "Should novel-scope documents be included?",
        "priority": "MEDIUM",
        "blocking": False,
        "instruction_references": [],
        "evidence": [
            {
                "matter_document_id": str(document_id),
                "paragraph_ids": ["¶4"],
                "reason": "Evidence for novel_scope",
            }
        ],
        "suggested_answers": ["Include them.", "Exclude them."],
    }

    result, disposition, diagnostics = assemble_synthesis_result(
        manifest,
        {("SIGNAL_DISPOSITION", "OTHER_UNMAPPED_CRITERIA"): output},
        [],
    )

    assert disposition == "READY_WITH_WARNINGS"
    assert result["other_unmapped_criteria"]["reporting_only"] is True
    assert result["other_unmapped_criteria"]["synthesis"] == output
    assert result["clarification_questions"][0]["question"] == output["question"]
    assert diagnostics["other_unmapped_criteria"]["items"][0]["source_key"] == "novel_scope"


def test_manifest_hash_and_summary_are_stable():
    document_id = uuid.uuid4()
    other_document_id = uuid.uuid4()
    analyses = [
        _analysis(document_id, [_match(ATTORNEY_CLIENT, "PRIMARY", "¶1")]),
        _analysis(other_document_id, [_match(WORK_PRODUCT, "PRIMARY", "¶2")]),
    ]
    first = compile_synthesis_manifest(
        analyses,
        {"selected_document_count": 2},
        _retrieval_plan(),
    )
    second = compile_synthesis_manifest(
        list(reversed(copy.deepcopy(analyses))),
        {"selected_document_count": 2},
        copy.deepcopy(_retrieval_plan()),
    )

    assert first["content_hash"] == second["content_hash"]
    assert manifest_summary(first)["unit_counts"] == {
        "FINAL_NARRATIVE": 1,
        "REFINEMENT_DIMENSION": 6,
        "TOPIC_FINDING": 2,
    }


def test_required_unit_failure_is_action_required_but_keeps_partial_result():
    manifest = compile_synthesis_manifest([], {"selected_document_count": 3}, _retrieval_plan())
    outputs = {
        ("FINAL_NARRATIVE", "FINAL"): {"narrative": "The available synthesis units completed."},
    }
    failed = [
        {
            "unit_type": "REFINEMENT_DIMENSION",
            "unit_key": "TEMPORAL_SCOPE",
            "attempt_count": 1,
            "error_code": "USAGE_LIMIT_EXCEEDED",
            "error_message": "Output limit exceeded",
        }
    ]

    result, disposition, diagnostics = assemble_synthesis_result(manifest, outputs, failed)

    assert disposition == "ACTION_REQUIRED"
    assert result["status"] == "COMPLETED_WITH_ERRORS"
    assert result["narrative"] == "The available synthesis units completed."
    temporal = next(
        item
        for item in result["refinement_assessment"]["evaluated_dimensions"]
        if item["dimension"] == "TEMPORAL_SCOPE"
    )
    assert temporal["conclusion"] == "NOT_EVALUATED"
    assert diagnostics["required_failure_count"] == 1


def test_optional_signal_failure_is_ready_with_warnings():
    first = uuid.uuid4()
    second = uuid.uuid4()
    analyses = [
        _analysis(first, [_match(ATTORNEY_CLIENT, "NEAR_MISS", "¶1")]),
        _analysis(second, [_match(ATTORNEY_CLIENT, "NEAR_MISS", "¶2")]),
    ]
    manifest = compile_synthesis_manifest(
        analyses,
        {"selected_document_count": 2},
        _retrieval_plan(),
    )
    failed = [
        {
            "unit_type": "SIGNAL_DISPOSITION",
            "unit_key": "NEAR_MISS_ATTORNEY_CLIENT_PRIVILEGE",
            "attempt_count": 1,
            "error_code": "INVALID_OUTPUT",
            "error_message": "Invalid question",
        }
    ]

    _, disposition, diagnostics = assemble_synthesis_result(manifest, {}, failed)

    assert disposition == "READY_WITH_WARNINGS"
    assert diagnostics["optional_failure_count"] == 1


def test_dimension_question_is_promoted_to_public_clarification_question():
    manifest = compile_synthesis_manifest([], {"selected_document_count": 3}, _retrieval_plan())
    document_id = str(uuid.uuid4())
    output = {
        "dimension": "TEMPORAL_SCOPE",
        "conclusion": "QUESTION_NEEDED",
        "rationale": "The guidance does not define the start date.",
        "evidence": [
            {
                "matter_document_id": document_id,
                "paragraph_ids": ["¶1"],
                "reason": "The document predates the stated event.",
            }
        ],
        "question": "Should documents created before the event date be included?",
        "priority": "HIGH",
        "blocking": True,
        "instruction_references": ["Temporal scope"],
        "suggested_answers": ["Include earlier documents.", "Exclude earlier documents."],
    }

    result, disposition, _ = assemble_synthesis_result(
        manifest,
        {("REFINEMENT_DIMENSION", "TEMPORAL_SCOPE"): output},
        [],
    )

    assert disposition == "READY"
    assert result["refinement_assessment"]["outcome"] == "QUESTIONS_PROPOSED"
    assert result["clarification_questions"][0]["question"] == output["question"]
    temporal = next(
        item
        for item in result["refinement_assessment"]["evaluated_dimensions"]
        if item["dimension"] == "TEMPORAL_SCOPE"
    )
    assert set(temporal) == {"dimension", "conclusion", "rationale", "evidence"}


def test_unit_validation_rejects_evidence_outside_bounded_input():
    document_id = str(uuid.uuid4())
    unit = {
        "unit_type": "TOPIC_FINDING",
        "unit_key": ATTORNEY_CLIENT,
        "assignments": [
            {
                "matter_document_id": document_id,
                "evidence": [
                    {
                        "matter_document_id": document_id,
                        "paragraph_ids": ["¶1"],
                        "reason": "Allowed",
                    }
                ],
            }
        ],
    }
    output = {
        "topic_key": ATTORNEY_CLIENT,
        "evidence": [
            {
                "matter_document_id": document_id,
                "paragraph_ids": ["¶99"],
                "reason": "Not supplied",
            }
        ],
    }

    try:
        validate_unit_output(unit, output)
    except ValueError as exc:
        assert "evidence not present" in str(exc)
    else:
        raise AssertionError("unknown evidence should fail validation")


def test_signal_normalization_restores_unit_id_and_drops_inapplicable_question_content():
    document_id = str(uuid.uuid4())
    unit = {
        "unit_type": "SIGNAL_DISPOSITION",
        "unit_key": "NEAR_MISS_GENERAL",
        "evidence": [
            {
                "matter_document_id": document_id,
                "paragraph_ids": ["¶1"],
                "reason": "Allowed",
            }
        ],
    }
    output = {
        "signal_id": "GENERAL",
        "disposition": "GUIDANCE_SUFFICIENT",
        "rationale": "The existing guidance resolves the signal.",
        "question": "Should we add another rule?",
        "priority": "LOW",
        "blocking": False,
        "instruction_references": [],
        "evidence": [
            {
                "matter_document_id": document_id,
                "paragraph_ids": ["¶1"],
                "reason": "Representative evidence",
            }
        ],
        "suggested_answers": ["Yes", "No"],
    }

    normalized, warnings = normalize_unit_output(unit, output)

    assert normalized["signal_id"] == "NEAR_MISS_GENERAL"
    assert normalized["question"] == ""
    assert normalized["suggested_answers"] == []
    assert {warning["code"] for warning in warnings} == {
        "AUTHORITATIVE_UNIT_KEY_RESTORED",
        "INAPPLICABLE_QUESTION_CONTENT_DROPPED",
    }
    validate_unit_output(unit, normalized)


def test_signal_normalization_drops_unknown_evidence_and_downgrades_question():
    document_id = str(uuid.uuid4())
    unit = {
        "unit_type": "SIGNAL_DISPOSITION",
        "unit_key": "NEAR_MISS_ATTORNEY_WORK_PRODUCT",
        "evidence": [
            {
                "matter_document_id": document_id,
                "paragraph_ids": ["¶1"],
                "reason": "Allowed",
            }
        ],
    }
    output = {
        "signal_id": unit["unit_key"],
        "disposition": "QUESTION_PROPOSED",
        "rationale": "A possible ambiguity was detected.",
        "question": "Should these documents be included?",
        "priority": "MEDIUM",
        "blocking": False,
        "instruction_references": [],
        "evidence": [
            {
                "matter_document_id": document_id,
                "paragraph_ids": ["¶99"],
                "reason": "This paragraph was not supplied.",
            }
        ],
        "suggested_answers": ["Include", "Exclude"],
    }

    normalized, warnings = normalize_unit_output(unit, output)

    assert normalized["disposition"] == "INSUFFICIENT_EVIDENCE"
    assert normalized["evidence"] == []
    assert normalized["question"] == ""
    assert normalized["suggested_answers"] == []
    assert "UNSUPPORTED_EVIDENCE_DROPPED" in {warning["code"] for warning in warnings}
    assert "UNSUPPORTED_SIGNAL_DISPOSITION_DOWNGRADED" in {
        warning["code"] for warning in warnings
    }
    validate_unit_output(unit, normalized)


def test_topic_normalization_drops_finding_when_all_citations_are_outside_input():
    document_id = str(uuid.uuid4())
    unit = {
        "unit_type": "TOPIC_FINDING",
        "unit_key": ATTORNEY_CLIENT,
        "criterion": {
            "criterion_label": "Attorney-client privilege",
            "description": "Confidential legal advice communications.",
        },
        "assignments": [
            {
                "matter_document_id": document_id,
                "evidence": [
                    {
                        "matter_document_id": document_id,
                        "paragraph_ids": ["¶1"],
                        "reason": "Allowed",
                    }
                ],
            }
        ],
    }
    output = {
        "topic_key": ATTORNEY_CLIENT,
        "label": "Invented label",
        "description": "Invented description",
        "finding": "The documents establish a broad finding.",
        "limitations": [],
        "evidence": [
            {
                "matter_document_id": document_id,
                "paragraph_ids": ["¶99"],
                "reason": "Unsupported",
            }
        ],
    }

    normalized, warnings = normalize_unit_output(unit, output)

    assert normalized["label"] == unit["criterion"]["criterion_label"]
    assert normalized["description"] == unit["criterion"]["description"]
    assert normalized["finding"] == ""
    assert normalized["evidence"] == []
    assert "UNSUPPORTED_FINDING_DROPPED" in {warning["code"] for warning in warnings}
    validate_unit_output(unit, normalized)


def test_normalization_warnings_make_completed_synthesis_ready_with_warnings():
    manifest = compile_synthesis_manifest([], {"selected_document_count": 3}, _retrieval_plan())
    outputs = {("FINAL_NARRATIVE", "FINAL"): {"narrative": "Synthesis completed."}}
    warnings = [
        {
            "unit_type": "SIGNAL_DISPOSITION",
            "unit_key": "NEAR_MISS_GENERAL",
            "code": "INAPPLICABLE_QUESTION_CONTENT_DROPPED",
            "path": "question",
        }
    ]

    result, disposition, diagnostics = assemble_synthesis_result(
        manifest,
        outputs,
        [],
        warnings,
    )

    assert result["status"] == "COMPLETED"
    assert disposition == "READY_WITH_WARNINGS"
    assert diagnostics["normalization_warning_count"] == 1
    assert diagnostics["normalization_warnings"] == warnings


def test_unit_limits_inherit_managed_skill_output_ceiling():
    limits = synthesis_unit_execution_limits(
        "TOPIC_FINDING",
        {"max_requests": 20, "max_output_tokens": 20_000, "max_output_retries": 4},
    )

    assert limits == {
        "max_requests": 4,
        "max_output_retries": 2,
        "max_output_tokens": 20_000,
    }


def test_unit_prompt_explicitly_allows_no_finding_without_overfitting():
    instructions = unit_instructions({"unit_type": "TOPIC_FINDING", "unit_key": ATTORNEY_CLIENT})

    assert "Do not force a finding" in instructions
    assert "return an empty finding" in instructions
    assert "three representative evidence items" in instructions
