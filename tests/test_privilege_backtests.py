from app.privilege_backtests import select_stratified_families


def test_stratified_family_selection_is_deterministic_and_keeps_families_whole() -> None:
    family_cells = {
        "family-a": {"not_privileged:positive"},
        "family-b": {"not_privileged:positive", "not_privileged:negative"},
        "family-c": {"not_privileged:negative"},
        "family-d": {"privileged:positive"},
    }
    family_documents = {
        "family-a": ["a1", "a2"],
        "family-b": ["b1", "b2", "b3"],
        "family-c": ["c1"],
        "family-d": ["d1", "d2"],
    }

    first = select_stratified_families(
        family_cells=family_cells,
        family_documents=family_documents,
        per_stratum=1,
        random_seed="fixed-seed",
    )
    second = select_stratified_families(
        family_cells=family_cells,
        family_documents=family_documents,
        per_stratum=1,
        random_seed="fixed-seed",
    )

    assert first == second
    selected_families, selected_documents, strata = first
    assert len(selected_families) == len(set(selected_families))
    for family_key in selected_families:
        assert set(family_documents[family_key]).issubset(selected_documents)
    positive = next(value for value in strata if value["key"] == "not_privileged:positive")
    assert positive["selected_family_count"] == 1
