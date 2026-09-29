import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml
from app.curation import (
    DEFAULT_EDGES_PATH,
    DEFAULT_REVIEW_LOG_PATH,
    CurationFile,
    CurationValidationError,
    EndpointNotResolved,
    ResolvedEndpoint,
    ReviewLog,
    apply_curated_load,
    content_sha,
    curation_request_id,
    parse_curation_file,
    parse_review_log,
    plan_curated_load,
    read_curation_file,
    read_review_log,
    render_curation_file,
    require_reviewed,
    resolve_curated_sha,
    review_batch,
    write_review_outcome,
)
from app.graph_store import GraphWriteError, TemporalWriteResult

TODAY = date(2026, 9, 28)
EVIDENCE = {
    "url": "https://www.sec.gov/Archives/edgar/data/1046179/000104617925000010/tsm-20241231.htm",
    "source_type": "sec_filing",
    "publisher_group": "tsmc",
    "locator": "Form 20-F 2024, Item 3.D Risk Factors",
}


def edge(**overrides):
    values = {
        "source": "TSM",
        "relation": "DEPENDS_ON",
        "target": "ASML",
        "weight": 0.9,
        "directness": 1.0,
        "rationale": "Sole commercial supplier of EUV lithography systems",
        "evidence": [dict(EVIDENCE)],
        "valid_from": date(2024, 12, 31),
        "curated_by": "aaryan",
        "curated_at": date(2026, 9, 22),
        "review_by": date(2027, 3, 22),
        "proposal_id": "tsm-20260922-01",
        "drafted_by": "human",
    }
    return values | overrides


def curation_file(*edges, entities=()):
    return {"entities": list(entities), "edges": list(edges)}


def proposal(proposal_id="tsm-20260928-01", decision="accept", **edge_overrides):
    proposed = {
        key: value
        for key, value in edge().items()
        if key not in {"curated_by", "curated_at", "proposal_id", "drafted_by"}
    }
    return {
        "proposal_id": proposal_id,
        "draft": None,
        "draft_sha": None,
        "decision": decision,
        "reason": None,
        "edge": proposed | edge_overrides,
    }


def human_batch(*proposals):
    return {
        "batch_id": "tsm-20260928",
        "drafted_by": "human",
        "drafted_at": "2026-09-28T10:00:00+00:00",
        "usage_request_id": None,
        "subject": "TSM",
        "evidence": [dict(EVIDENCE)],
        "proposals": list(proposals),
    }


def llm_batch(*proposals):
    return human_batch(*proposals) | {
        "drafted_by": "llm:test-model",
        "usage_request_id": "4c5a0f3e-9d1b-4f0a-8a6e-2b3c4d5e6f70",
    }


def llm_proposal(proposal_id="tsm-20260928-01", decision="accept", **edge_overrides):
    draft = {
        "source": "TSM",
        "relation": "DEPENDS_ON",
        "target": "ASML",
        "directness": "stated",
        "rationale": "Sole commercial supplier of EUV lithography systems",
        "excerpt": "We rely on ASML as the sole supplier of EUV systems.",
        "excerpt_verified": True,
    }
    return proposal(proposal_id, decision, **edge_overrides) | {
        "draft": draft,
        "draft_sha": content_sha(draft),
    }


def accept_all(*proposals, batch=human_batch, reviewer="aaryan"):
    return review_batch(
        batch(*proposals), CurationFile((), ()), ReviewLog(()), reviewer, TODAY
    )


def reviewed_log(outcome):
    return parse_review_log(json.dumps(record.as_json()) for record in outcome.decided)


class FakeResolver:
    ENDPOINTS = {
        "TSM": ResolvedEndpoint("ent_sec_0001046179", "Taiwan Semiconductor", "Company", 1.0),
        "ASML": ResolvedEndpoint("ent_sec_0000937966", "ASML Holding N.V.", "Company", 1.0),
        "Taiwan": ResolvedEndpoint("ent_cur_country_taiwan", "Taiwan", "Country", 0.7),
    }

    def resolve(self, surface):
        try:
            return self.ENDPOINTS[surface]
        except KeyError as error:
            raise EndpointNotResolved(f"{surface!r} did not resolve") from error


class FakeGraph:
    """Mimics GraphStore receipts: a repeated request ID replays instead of duplicating."""

    def __init__(self):
        self.entities = {}
        self.edges = {}

    def upsert_entity(self, entity_id, canonical_name, entity_type):
        self.entities[entity_id] = (canonical_name, entity_type)

    def create_curated_edge_revision(self, revision):
        key = str(revision.request_id)
        if key in self.edges:
            if self.edges[key] != revision:
                raise GraphWriteError("request ID conflicts with an existing graph write")
            return TemporalWriteResult(key, replayed=True)
        self.edges[key] = revision
        return TemporalWriteResult(key, replayed=False)


# --- Done means: missing evidence/review dates fail --------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"evidence": []}, "at least one evidence item is required"),
        ({"review_by": None}, "review_by: must be a date"),
        ({"curated_at": None}, "curated_at: must be a date"),
        ({"valid_from": None}, "valid_from: must be a date"),
        ({"review_by": date(2026, 9, 1)}, "review_by: must be after curated_at"),
        ({"valid_from": date(2026, 9, 23)}, "valid_from: cannot be after curated_at"),
        ({"curated_at": date(2026, 10, 1), "review_by": date(2027, 1, 1)}, "in the future"),
        ({"relation": "RELATED_TO"}, "14 frozen relation types"),
        ({"weight": 0}, "weight: must be a number in (0, 1]"),
        ({"weight": True}, "weight: must be a number in (0, 1]"),
        ({"directness": 0.8}, "must be 1.0 (stated) or 0.6 (inferred)"),
        ({"curated_by": "llm"}, "named human curator"),
        ({"target": "TSM"}, "cannot connect an entity to itself"),
        ({"source": "usr_abc"}, "never accepts user IDs"),
        ({"drafted_by": "robot"}, "drafted_by: must match"),
    ],
)
def test_invalid_edge_fields_fail_validation(overrides, message):
    with pytest.raises(CurationValidationError) as error:
        parse_curation_file(curation_file(edge(**overrides)), TODAY)

    assert any(message in item for item in error.value.errors), error.value.errors


def test_missing_required_fields_are_all_reported_in_one_pass():
    raw = edge()
    for field in ("evidence", "review_by", "curated_at"):
        del raw[field]

    with pytest.raises(CurationValidationError) as error:
        parse_curation_file(curation_file(raw), TODAY)

    assert "missing required fields ['evidence', 'curated_at', 'review_by']" in str(error.value)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://www.sec.gov/filing.htm", "https URL"),
        ("https://example.com/filing", "real public URL"),
        ("https://fixture.invalid/filing", "real public URL"),
        ("https://intranet/filing", "real public URL"),
        ("not a url", "https URL"),
    ],
)
def test_evidence_urls_must_be_real_public_https(url, message):
    with pytest.raises(CurationValidationError) as error:
        parse_curation_file(curation_file(edge(evidence=[EVIDENCE | {"url": url}])), TODAY)

    assert message in str(error.value)


def test_evidence_needs_known_source_type_and_publisher_group():
    bad = EVIDENCE | {"source_type": "blog_post", "publisher_group": "Has Spaces"}

    with pytest.raises(CurationValidationError) as error:
        parse_curation_file(curation_file(edge(evidence=[bad])), TODAY)

    assert "source_type: must be one of" in str(error.value)
    assert "publisher_group: must match" in str(error.value)


def test_curator_supplied_confidence_is_rejected():
    with pytest.raises(CurationValidationError) as error:
        parse_curation_file(curation_file(edge(confidence=0.99)), TODAY)

    assert "confidence is computed from evidence" in str(error.value)


def test_duplicate_surface_edges_are_rejected():
    first = edge()
    second = edge(proposal_id="tsm-20260922-02", target="asml", weight=0.8)

    with pytest.raises(CurationValidationError) as error:
        parse_curation_file(curation_file(first, second), TODAY)

    assert "revise one edge instead" in str(error.value)


def test_curated_entities_are_validated():
    entities = [
        {"entity_id": "ent_sec_taiwan", "canonical_name": "Taiwan", "type": "Country",
         "aliases": ["Taiwan"]},
        {"entity_id": "ent_cur_country_taiwan", "canonical_name": "Taiwan", "type": "Nation",
         "aliases": ["Inc"]},
    ]

    with pytest.raises(CurationValidationError) as error:
        parse_curation_file(curation_file(entities=entities), TODAY)

    messages = str(error.value)
    assert "entity_id: must match ent_cur_" in messages
    assert "type: must be one of the frozen entity types" in messages
    assert "normalizes to an empty alias" in messages


# --- Done means: unreviewed proposals cannot load -----------------------------------


def test_unreviewed_edge_cannot_be_planned_for_load():
    curation = parse_curation_file(curation_file(edge()), TODAY)

    with pytest.raises(CurationValidationError) as error:
        plan_curated_load(curation, ReviewLog(()), FakeResolver())

    assert "has no accept/edit review record" in str(error.value)


def test_edge_changed_after_review_cannot_load():
    outcome = accept_all(proposal())
    log = reviewed_log(outcome)
    tampered = copy.deepcopy(yaml.safe_load(render_curation_file(outcome.curation)))
    tampered["edges"][0]["weight"] = 0.99
    curation = parse_curation_file(tampered, TODAY)

    with pytest.raises(CurationValidationError) as error:
        plan_curated_load(curation, log, FakeResolver())

    assert "changed after review" in str(error.value)


def test_pending_and_rejected_proposals_never_reach_the_edges_file():
    outcome = accept_all(
        proposal("tsm-20260928-01", "pending"),
        proposal("tsm-20260928-02", "reject", target="Taiwan") | {"reason": "Not a dependency"},
    )

    assert outcome.curation.edges == ()
    assert outcome.pending == ("tsm-20260928-01",)
    assert [record.decision for record in outcome.decided] == ["rejected"]


def test_reviewer_must_be_a_named_human():
    with pytest.raises(CurationValidationError, match="reviewer: must be one of"):
        accept_all(proposal(), reviewer="llm")


# --- Done means: SHA resolves to the reviewed source --------------------------------


def test_curated_sha_resolves_to_reviewed_source_and_record():
    outcome = accept_all(proposal())
    log = reviewed_log(outcome)
    loaded = outcome.curation.edges[0]

    current, record = resolve_curated_sha(loaded.source_id, outcome.curation, log)

    assert current == loaded
    assert record.decision == "accepted"
    assert record.reviewer == "aaryan"
    assert record.edge == loaded.canonical()
    assert content_sha(record.edge) == loaded.sha


def test_superseded_sha_still_resolves_through_the_review_log():
    outcome = accept_all(proposal())
    log = reviewed_log(outcome)
    sha = outcome.curation.edges[0].sha

    current, record = resolve_curated_sha(sha, CurationFile((), ()), log)

    assert current is None
    assert record.edge["evidence"][0]["url"] == EVIDENCE["url"]


def test_sha_ignores_yaml_formatting_and_key_order():
    raw = edge()
    reordered = dict(reversed(list(raw.items())))
    reordered["weight"] = 0.90
    reordered["valid_from"] = "2024-12-31"

    first = parse_curation_file(curation_file(raw), TODAY).edges[0]
    second = parse_curation_file(curation_file(reordered), TODAY).edges[0]

    assert first.sha == second.sha
    assert first.source_id == f"curated:{first.sha}"


def test_render_round_trip_preserves_every_sha():
    entity = {"entity_id": "ent_cur_country_taiwan", "canonical_name": "Taiwan",
              "type": "Country", "aliases": ["Taiwan", "Republic of China"]}
    no_locator = {key: value for key, value in EVIDENCE.items() if key != "locator"}
    original = parse_curation_file(
        curation_file(edge(), edge(proposal_id="tsm-20260922-02", target="Taiwan",
                                   relation="EXPOSED_TO", evidence=[no_locator]),
                      entities=[entity]),
        TODAY,
    )

    rendered = parse_curation_file(yaml.safe_load(render_curation_file(original)), TODAY)

    assert rendered == original


def test_rendering_has_no_yaml_anchors_for_shared_dates():
    outcome = accept_all(proposal(), proposal("tsm-20260928-02", target="Taiwan",
                                              relation="EXPOSED_TO"))

    rendered = render_curation_file(outcome.curation)

    assert "&id" not in rendered and "*id" not in rendered
    assert rendered.count("curated_at: 2026-09-28") == 2


# --- Done means: rejected drafts are logged -----------------------------------------


def test_rejected_llm_draft_is_logged_with_reason_and_snapshot(tmp_path):
    edges_path, log_path = tmp_path / "edges.yaml", tmp_path / "log.jsonl"
    edges_path.write_text(render_curation_file(CurationFile((), ())))
    rejected = llm_proposal(decision="reject") | {"reason": "Excerpt describes a competitor"}

    outcome = review_batch(
        llm_batch(rejected), read_curation_file(edges_path, TODAY), ReviewLog(()), "aaryan",
        TODAY,
    )
    write_review_outcome(outcome, edges_path, log_path)
    log = read_review_log(log_path)

    record = log.records[0]
    assert record.decision == "rejected"
    assert record.reason == "Excerpt describes a competitor"
    assert record.draft["excerpt"].startswith("We rely on ASML")
    assert record.usage_request_id == "4c5a0f3e-9d1b-4f0a-8a6e-2b3c4d5e6f70"
    assert record.edge is None and record.curated_sha is None
    assert read_curation_file(edges_path, TODAY).edges == ()


def test_rejection_without_reason_fails():
    with pytest.raises(CurationValidationError, match="explain every rejection"):
        accept_all(proposal(decision="reject"))


# --- Review records: accept vs edit, idempotency, tamper detection -----------------


def test_unchanged_llm_draft_is_recorded_as_accepted_and_changed_as_edited():
    outcome = accept_all(
        llm_proposal("tsm-20260928-01"),
        llm_proposal("tsm-20260928-02", target="Taiwan", relation="EXPOSED_TO"),
        batch=llm_batch,
    )

    decisions = {record.proposal_id: record.decision for record in outcome.decided}
    assert decisions == {"tsm-20260928-01": "accepted", "tsm-20260928-02": "edited"}
    assert {edge.drafted_by for edge in outcome.curation.edges} == {"llm:test-model"}
    assert {edge.curated_by for edge in outcome.curation.edges} == {"aaryan"}


def test_directness_change_counts_as_edit():
    outcome = accept_all(llm_proposal(directness=0.6), batch=llm_batch)

    assert outcome.decided[0].decision == "edited"


def test_rerunning_a_review_is_idempotent(tmp_path):
    edges_path, log_path = tmp_path / "edges.yaml", tmp_path / "log.jsonl"
    edges_path.write_text(render_curation_file(CurationFile((), ())))
    batch = human_batch(proposal())

    for _ in range(2):
        outcome = review_batch(
            batch, read_curation_file(edges_path, TODAY), read_review_log(log_path), "aaryan",
            TODAY,
        )
        write_review_outcome(outcome, edges_path, log_path)

    assert outcome.already_decided == ("tsm-20260928-01",)
    assert len(read_curation_file(edges_path, TODAY).edges) == 1
    assert len(read_review_log(log_path).records) == 1


def test_interrupted_review_can_rerun_without_duplicating_the_edge(tmp_path):
    edges_path, log_path = tmp_path / "edges.yaml", tmp_path / "log.jsonl"
    first = accept_all(proposal())
    edges_path.write_text(render_curation_file(first.curation))  # log write never happened

    outcome = review_batch(
        human_batch(proposal()), read_curation_file(edges_path, TODAY), ReviewLog(()),
        "aaryan", TODAY,
    )
    write_review_outcome(outcome, edges_path, log_path)

    curation = read_curation_file(edges_path, TODAY)
    require_reviewed(curation, read_review_log(log_path))
    assert len(curation.edges) == 1


def test_later_valid_revision_supersedes_the_file_entry_and_keeps_its_history():
    first = accept_all(proposal())
    log = reviewed_log(first)
    revision = proposal("tsm-20260928-02", valid_from=date(2025, 6, 30), weight=0.95)

    outcome = review_batch(human_batch(revision), first.curation, log, "aaryan", TODAY)
    log = parse_review_log(
        json.dumps(record.as_json()) for record in log.records + outcome.decided
    )

    [current] = outcome.curation.edges
    assert current.proposal_id == "tsm-20260928-02"
    require_reviewed(outcome.curation, log)
    _, superseded = resolve_curated_sha(first.curation.edges[0].sha, outcome.curation, log)
    assert superseded.proposal_id == "tsm-20260928-01"


def test_revision_without_later_validity_is_refused_at_review():
    first = accept_all(proposal())
    revision = proposal("tsm-20260928-02", weight=0.95)

    with pytest.raises(CurationValidationError, match="valid_from must be after 2024-12-31"):
        review_batch(human_batch(revision), first.curation, reviewed_log(first), "aaryan", TODAY)


def test_tampered_draft_snapshot_is_refused():
    tampered = llm_proposal()
    tampered["draft"]["target"] = "Netherlands"

    with pytest.raises(CurationValidationError, match="draft snapshot changed"):
        accept_all(tampered, batch=llm_batch)


def test_llm_batch_must_link_its_metered_request():
    batch = llm_batch(llm_proposal()) | {"usage_request_id": None}

    with pytest.raises(CurationValidationError, match="link their metered request"):
        review_batch(batch, CurationFile((), ()), ReviewLog(()), "aaryan", TODAY)


def test_invalid_accepted_edge_blocks_the_whole_batch():
    with pytest.raises(CurationValidationError) as error:
        accept_all(proposal("tsm-20260928-01"), proposal("tsm-20260928-02", review_by=None))

    assert "proposals[1].edge.review_by" in str(error.value)


def test_review_log_rejects_forged_or_repeated_records():
    record = accept_all(proposal()).decided[0].as_json()
    forged = record | {"curated_sha": "0" * 64}

    with pytest.raises(CurationValidationError, match="does not hash the recorded edge"):
        parse_review_log([json.dumps(forged)])
    with pytest.raises(CurationValidationError, match="decided more than once"):
        parse_review_log([json.dumps(record), json.dumps(record)])


# --- Load planning: computed confidence and deterministic replay -------------------


def test_confidence_is_computed_from_evidence_not_supplied():
    outcome = accept_all(
        proposal("tsm-20260928-01"),
        proposal(
            "tsm-20260928-02", target="Taiwan", relation="EXPOSED_TO", directness=0.6,
            evidence=[EVIDENCE | {"source_type": "major_wire", "publisher_group": "reuters"}],
        ),
    )

    plan = plan_curated_load(outcome.curation, reviewed_log(outcome), FakeResolver())

    # One SEC filing, identifier-resolved, stated: 0.35 + 0.25/3 + 0.25 + 0.15.
    assert plan[0].revision.confidence == pytest.approx(0.8333, abs=1e-4)
    # Wire, alias-resolved endpoint (0.7), inferred: 0.35*0.7 + 0.25/3 + 0.25*0.6 + 0.15*0.7.
    assert plan[1].revision.confidence == pytest.approx(0.5833, abs=1e-4)


def test_independence_counts_publisher_groups_not_urls():
    syndicated = [
        EVIDENCE,
        EVIDENCE | {"url": "https://www.sec.gov/other-copy.htm"},
        EVIDENCE | {"url": "https://investor.tsmc.com/copy", "source_type": "company_ir"},
    ]
    outcome = accept_all(proposal(evidence=syndicated))

    revision = plan_curated_load(outcome.curation, reviewed_log(outcome), FakeResolver())[0]

    assert revision.revision.confidence == pytest.approx(0.8333, abs=1e-4)
    assert revision.revision.evidence_count == 3


def test_planned_revision_is_curated_temporal_and_deterministic():
    outcome = accept_all(proposal())
    log = reviewed_log(outcome)

    first = plan_curated_load(outcome.curation, log, FakeResolver())[0].revision
    second = plan_curated_load(outcome.curation, log, FakeResolver())[0].revision

    sha = outcome.curation.edges[0].sha
    assert first == second
    assert first.request_id == curation_request_id(sha)
    assert first.origin == "curated"
    assert first.source_ids == (f"curated:{sha}",)
    assert first.valid_from == datetime(2024, 12, 31, tzinfo=UTC)
    assert first.observed_at == datetime(2026, 9, 28, tzinfo=UTC)
    assert (first.from_entity_id, first.to_entity_id) == (
        "ent_sec_0001046179", "ent_sec_0000937966"
    )


def test_unresolved_endpoint_blocks_the_whole_load():
    outcome = accept_all(proposal(target="TSMC"))

    with pytest.raises(CurationValidationError, match="'TSMC' did not resolve"):
        plan_curated_load(outcome.curation, reviewed_log(outcome), FakeResolver())


# --- Done means: reimport creates no duplicate edge ---------------------------------


def test_reimport_replays_instead_of_duplicating():
    outcome = accept_all(proposal(), proposal("tsm-20260928-02", target="Taiwan",
                                              relation="EXPOSED_TO"))
    plan = plan_curated_load(outcome.curation, reviewed_log(outcome), FakeResolver())
    graph = FakeGraph()

    first = apply_curated_load(plan, graph)
    second = apply_curated_load(plan, graph)

    assert [result.status for result in first] == ["created", "created"]
    assert [result.status for result in second] == ["replayed", "replayed"]
    assert len(graph.edges) == 2
    assert set(graph.entities) == {
        "ent_sec_0001046179", "ent_sec_0000937966", "ent_cur_country_taiwan"
    }


def test_graph_write_failure_is_reported_per_edge_with_a_fix():
    class StaleGraph(FakeGraph):
        def create_curated_edge_revision(self, revision):
            raise GraphWriteError("stale temporal revision is not supported")

    outcome = accept_all(proposal())
    plan = plan_curated_load(outcome.curation, reviewed_log(outcome), FakeResolver())

    [result] = apply_curated_load(plan, StaleGraph())

    assert result.status == "failed"
    assert "needs a valid_from later" in result.error


# --- The committed workflow files stay valid and reviewed ---------------------------


def test_committed_curated_edges_are_valid_and_all_reviewed():
    curation = read_curation_file(DEFAULT_EDGES_PATH)
    log = read_review_log(DEFAULT_REVIEW_LOG_PATH)

    require_reviewed(curation, log)


def test_committed_edges_file_is_in_canonical_rendering():
    text = Path(DEFAULT_EDGES_PATH).read_text(encoding="utf-8")

    assert text == render_curation_file(read_curation_file(DEFAULT_EDGES_PATH))


def test_endpoint_resolution_change_after_load_fails_loudly():
    outcome = accept_all(proposal())
    log = reviewed_log(outcome)
    graph = FakeGraph()
    apply_curated_load(plan_curated_load(outcome.curation, log, FakeResolver()), graph)

    class MovedTicker(FakeResolver):
        ENDPOINTS = FakeResolver.ENDPOINTS | {
            "ASML": ResolvedEndpoint("ent_sec_9999999999", "Other", "Company", 1.0)
        }

    [result] = apply_curated_load(
        plan_curated_load(outcome.curation, log, MovedTicker()), graph
    )

    assert result.status == "failed"
    assert "resolves differently" in result.error
    assert len(graph.edges) == 1
