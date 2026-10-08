"""Read-boundary validation independent of a running database."""

import pytest
from app.onboarding import OnboardingError, candidate, prepared_graph


@pytest.mark.parametrize("value", [
    None,
    {"nodes": [], "edges": []},
    {"nodes": [{"id": "ent_root", "name": "Root", "entity_type": "Company"}],
     "edges": [{"source_id": "ent_root", "target_id": "ent_missing",
                "relation": "SUPPLIES", "confidence": 0.9}]},
    {"nodes": [{"id": "ent_root", "name": "Root", "entity_type": "Company"}] * 2,
     "edges": []},
    {"nodes": [], "edges": [], "user_id": "usr_other"},
])
def test_corrupt_prepared_graph_fails_closed(value):
    with pytest.raises(OnboardingError) as error:
        prepared_graph("ent_root", value)
    assert error.value.status == 503
    assert error.value.code == "graph_unavailable"


def test_unsupported_catalog_type_is_not_silently_recategorized():
    with pytest.raises(OnboardingError) as error:
        candidate({"type": "Topic"})
    assert error.value.code == "unsupported_entity_type"


def test_prepared_graph_preserves_the_worker_result():
    value = {"nodes": [{"id": "ent_root", "name": "Root", "entity_type": "Company"}],
             "edges": []}
    assert prepared_graph("ent_root", value).model_dump() == value
