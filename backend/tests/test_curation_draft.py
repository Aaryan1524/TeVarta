import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
import yaml
from app.curation import (
    CurationFile,
    Evidence,
    ReviewLog,
    content_sha,
    review_batch,
)
from app.curation_draft import (
    DRAFT_SCHEMA,
    MAX_PROPOSALS,
    MAX_SOURCE_CHARS,
    DraftCandidate,
    DraftConfigurationError,
    DraftModelError,
    DraftRequest,
    ModelDraft,
    OpenAIDraftModel,
    TokenPrices,
    draft_batch,
    excerpt_found,
)

FILING = """Item 1A. Risk Factors
We rely on ASML as the sole supplier of EUV   lithography systems.
A substantial portion of our production is located in Taiwan."""
EVIDENCE = Evidence(
    "https://www.sec.gov/Archives/edgar/data/1046179/tsm-20241231.htm",
    "sec_filing",
    "tsmc",
    "Form 20-F 2024, Item 3.D",
)


def candidate(**overrides):
    values = {
        "source": "TSM",
        "relation": "DEPENDS_ON",
        "target": "ASML",
        "directness": "stated",
        "excerpt": "We rely on ASML as the sole supplier of EUV lithography systems.",
        "rationale": "ASML is the sole EUV supplier",
    }
    return DraftCandidate(**(values | overrides))


def request(**overrides):
    values = {"batch_id": "tsm-20260928", "subject": "TSM", "text": FILING,
              "evidence": EVIDENCE}
    return DraftRequest(**(values | overrides))


class FakeModel:
    provider, model = "fake", "fake-model-1"

    def __init__(self, candidates=(), error=None):
        self.candidates, self.error, self.calls = tuple(candidates), error, 0

    def draft(self, subject, text):
        self.calls += 1
        if self.error:
            raise self.error
        return ModelDraft(self.candidates, input_tokens=1200, output_tokens=300)


class FakeMeter:
    def __init__(self, available=True):
        self.available, self.records = available, []

    def check_available(self):
        if not self.available:
            raise DraftConfigurationError("usage metering database is unavailable")

    def record(self, request_id, outcome, **fields):
        self.records.append({"request_id": request_id, "outcome": outcome, **fields})


def fixed_now():
    return datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


def test_successful_draft_is_metered_and_linked_to_the_batch():
    meter = FakeMeter()

    batch = draft_batch(request(), FakeModel([candidate()]), meter, fixed_now)

    [record] = meter.records
    assert record["outcome"] == "success"
    assert (record["input_tokens"], record["output_tokens"]) == (1200, 300)
    assert record["provider"] == "fake" and record["model"] == "fake-model-1"
    assert batch["usage_request_id"] == str(record["request_id"])
    assert batch["drafted_by"] == "llm:fake-model-1"


def test_unavailable_meter_blocks_the_model_call():
    model = FakeModel([candidate()])

    with pytest.raises(DraftConfigurationError, match="metering"):
        draft_batch(request(), model, FakeMeter(available=False), fixed_now)

    assert model.calls == 0


def test_failed_model_call_is_metered_with_spent_tokens():
    meter = FakeMeter()
    model = FakeModel(error=DraftModelError("bad output", input_tokens=900, output_tokens=40))

    with pytest.raises(DraftModelError):
        draft_batch(request(), model, meter, fixed_now)

    [record] = meter.records
    assert record["outcome"] == "failure"
    assert (record["input_tokens"], record["output_tokens"]) == (900, 40)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"text": "x" * (MAX_SOURCE_CHARS + 1)}, "exceeds"),
        ({"text": "  "}, "empty"),
        ({"batch_id": "Bad Batch"}, "batch_id"),
        ({"subject": ""}, "subject"),
    ],
)
def test_invalid_requests_fail_before_metering_or_spend(overrides, message):
    meter, model = FakeMeter(), FakeModel([candidate()])

    with pytest.raises(DraftConfigurationError, match=message):
        draft_batch(request(**overrides), model, meter, fixed_now)

    assert model.calls == 0 and meter.records == []


def test_proposals_are_pending_with_prefilled_defaults_and_no_model_confidence():
    batch = draft_batch(
        request(),
        FakeModel([candidate(), candidate(target="Taiwan", relation="EXPOSED_TO",
                                          directness="inferred", excerpt="made up text")]),
        FakeMeter(),
        fixed_now,
    )

    first, second = batch["proposals"]
    assert first["proposal_id"] == "tsm-20260928-01"
    assert first["decision"] == "pending"
    assert first["draft"]["excerpt_verified"] is True
    assert second["draft"]["excerpt_verified"] is False
    assert first["draft_sha"] == content_sha(first["draft"])
    assert first["edge"]["weight"] == 0.90  # T01 relation default, for the reviewer to confirm
    assert second["edge"]["directness"] == 0.6
    assert first["edge"]["valid_from"] is None and first["edge"]["review_by"] is None
    assert "confidence" not in json.dumps(batch)


def test_draft_output_is_capped():
    batch = draft_batch(
        request(), FakeModel([candidate()] * (MAX_PROPOSALS + 5)), FakeMeter(), fixed_now
    )

    assert len(batch["proposals"]) == MAX_PROPOSALS


def test_excerpt_verification_ignores_whitespace_and_case_only():
    assert excerpt_found("we rely on asml as the sole supplier of euv lithography", FILING)
    assert not excerpt_found("We rely on Nikon as the sole supplier", FILING)
    assert not excerpt_found("   ", FILING)


def test_drafted_batch_round_trips_through_yaml_into_human_review():
    batch = draft_batch(
        request(),
        FakeModel([candidate(), candidate(target="Taiwan", relation="EXPOSED_TO")]),
        FakeMeter(),
        fixed_now,
    )
    reviewed = yaml.safe_load(yaml.safe_dump(batch, sort_keys=False))
    accepted, rejected = reviewed["proposals"]
    accepted["decision"] = "accept"
    accepted["edge"] |= {"valid_from": "2024-12-31", "review_by": "2027-03-28"}
    rejected["decision"], rejected["reason"] = "reject", "Location, not a dependency"

    outcome = review_batch(reviewed, CurationFile((), ()), ReviewLog(()), "aaryan",
                           date(2026, 9, 28))

    assert [record.decision for record in outcome.decided] == ["accepted", "rejected"]
    assert outcome.curation.edges[0].evidence == (EVIDENCE,)
    assert outcome.decided[1].usage_request_id == batch["usage_request_id"]


# --- OpenAI Responses adapter -------------------------------------------------------


def response_body(payload, status="completed", content_type="output_text"):
    content = (
        {"type": "refusal", "refusal": "no"}
        if content_type == "refusal"
        else {"type": "output_text", "text": json.dumps(payload), "annotations": []}
    )
    return json.dumps(
        {
            "status": status,
            "output": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "role": "assistant", "status": "completed",
                 "content": [content]},
            ],
            "usage": {"input_tokens": 1500, "output_tokens": 220, "total_tokens": 1720},
        }
    ).encode()


class RecordingTransport:
    def __init__(self, body):
        self.body, self.calls = body, []

    def __call__(self, url, body, headers):
        self.calls.append((url, json.loads(body), dict(headers)))
        return self.body


def test_openai_adapter_sends_strict_schema_and_parses_candidates():
    transport = RecordingTransport(
        response_body({"candidates": [candidate().__dict__]})
    )
    model = OpenAIDraftModel("sk-test", "configured-model", transport)

    result = model.draft("TSM", FILING)

    [(url, body, headers)] = transport.calls
    assert url == "https://api.openai.com/v1/responses"
    assert body["model"] == "configured-model"
    assert body["store"] is False
    assert body["text"]["format"] == {
        "type": "json_schema", "name": "curated_edge_candidates", "schema": DRAFT_SCHEMA,
        "strict": True,
    }
    assert "<filing_text>" in body["input"] and FILING in body["input"]
    assert headers["Authorization"] == "Bearer sk-test"
    assert result.candidates == (candidate(),)
    assert (result.input_tokens, result.output_tokens) == (1500, 220)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (response_body({}, content_type="refusal"), "refused"),
        (response_body({"candidates": []}, status="incomplete"), "status"),
        (response_body({"candidates": [candidate(relation="LIKES").__dict__]}), "frozen 14"),
        (response_body({"candidates": [candidate(directness="certain").__dict__]}),
         "stated or inferred"),
        (response_body({"candidates": [candidate(excerpt="").__dict__]}), "empty"),
        (response_body({"items": []}), "candidates list"),
    ],
)
def test_openai_adapter_rejects_unusable_output_and_keeps_token_counts(body, message):
    model = OpenAIDraftModel("sk-test", "configured-model", RecordingTransport(body))

    with pytest.raises(DraftModelError, match=message) as error:
        model.draft("TSM", FILING)

    assert (error.value.input_tokens, error.value.output_tokens) == (1500, 220)


@pytest.mark.parametrize(
    "environ",
    [{}, {"OPENAI_API_KEY": "sk-test"}, {"TEVARTA_CURATION_DRAFT_MODEL": "configured-model"}],
)
def test_openai_adapter_fails_closed_without_key_and_explicit_model(environ):
    with pytest.raises(DraftConfigurationError):
        OpenAIDraftModel.from_environ(environ)


def test_token_prices_cost_per_million():
    prices = TokenPrices(Decimal("2.50"), Decimal("10"))

    assert prices.cost(1_000_000, 100_000) == Decimal("3.5")


def test_usage_request_id_is_a_uuid():
    batch = draft_batch(request(), FakeModel([candidate()]), FakeMeter(), fixed_now)

    assert UUID(batch["usage_request_id"]).version == 4


def test_postgres_meter_fails_closed_when_its_database_is_unreachable():
    from app.curation_draft import PostgresUsageMeter

    meter = PostgresUsageMeter(
        "postgresql://nobody@127.0.0.1:1/unreachable?connect_timeout=1",
        TokenPrices(Decimal("1"), Decimal("1")),
    )

    with pytest.raises(DraftConfigurationError, match="refusing an unmetered model call"):
        meter.check_available()


def test_draft_component_is_a_reported_usage_component():
    from pathlib import Path

    from app.curation_draft import DRAFT_COMPONENT

    policy = json.loads(
        (Path(__file__).resolve().parents[2] / "config/usage_limits.json").read_text()
    )

    assert DRAFT_COMPONENT in policy["reporting_components"]


def test_realistic_excerpt_survives_the_yaml_review_round_trip():
    excerpt = (
        "We depend on a limited number of suppliers—including “ASML” "
        "for EUV systems—and\n  any disruption   could harm us. " * 6
    )[:400]
    filing = "Item 1A.\n" + excerpt + "\nMore text."
    batch = draft_batch(
        request(text=filing), FakeModel([candidate(excerpt=excerpt)]), FakeMeter(), fixed_now
    )

    reloaded = yaml.safe_load(yaml.safe_dump(batch, sort_keys=False, allow_unicode=True,
                                             width=100))

    [proposal] = reloaded["proposals"]
    assert content_sha(proposal["draft"]) == proposal["draft_sha"]
    assert proposal["draft"]["excerpt_verified"] is True
