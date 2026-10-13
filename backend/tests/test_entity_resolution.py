import pytest
from app.entity_resolution import ResolutionCandidate, alias_key, choose_candidate


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        ("0001000184", ("cik:0001000184", 1)),
        ("SNDK", ("ticker:sndk", 1)),
        ("NASDAQ: SNDK", ("ticker:sndk", 1)),
        ("2330.TW", ("ticker:2330", 1)),
        ("SanDisk Corp.", ("sandisk", 2)),
    ],
)
def test_resolution_rungs_normalize_identifiers_and_aliases(
    surface: str, expected: tuple[str, int]
) -> None:
    assert alias_key(surface) == expected


@pytest.mark.parametrize(
    "surface", ["SanDisk", "San Disk", "SanDisk Corp.", "SNDK", "NASDAQ: SNDK"]
)
def test_five_sandisk_spellings_resolve_to_the_catalog_alias_or_identifier(surface: str) -> None:
    key, _ = alias_key(surface)
    assert key in {"sandisk", "ticker:sndk"}


def test_cooccurrence_precedes_seed_count() -> None:
    candidates = (
        ResolutionCandidate("ent_apple_company", "Apple Inc.", 2),
        ResolutionCandidate("ent_apple_fruit", "Apple fruit", 100),
    )
    resolution = choose_candidate(candidates, {"ent_apple_company": 1})
    assert resolution.entity_id == "ent_apple_company"


def test_seed_count_breaks_alias_ties_but_equal_scores_remain_ambiguous() -> None:
    candidates = (
        ResolutionCandidate("ent_mercury_company", "Mercury Systems", 3),
        ResolutionCandidate("ent_mercury_planet", "Mercury", 1),
    )
    assert choose_candidate(candidates, {}).entity_id == "ent_mercury_company"
    tied = tuple(ResolutionCandidate(f"ent_{index}", "Apple", 1) for index in range(2))
    assert choose_candidate(tied, {}).status == "ambiguous"


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        ("$AAPL", ("ticker:aapl", 1)),
        ("NYSE: BRK.B", ("ticker:brk-b", 1)),
        ("BRK.B", ("ticker:brk-b", 1)),
        ("BRK-B", ("ticker:brk-b", 1)),
        ("BRK/B", ("ticker:brk-b", 1)),
        ("AAPL.O", ("ticker:aapl", 1)),
        ("Update: AI", ("updateai", 2)),
    ],
)
def test_cashtags_share_classes_and_known_exchanges_normalize(
    surface: str, expected: tuple[str, int]
) -> None:
    assert alias_key(surface) == expected


@pytest.mark.parametrize("surface", ["AI", "IT", "ON", "A", "SNDK", "2026", "0001000184"])
def test_article_text_never_treats_bare_words_or_numbers_as_identifiers(surface: str) -> None:
    key, rung = alias_key(surface, bare=False)

    assert rung == 2
    assert not key.startswith(("ticker:", "cik:"))


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        ("$AAPL", "ticker:aapl"),
        ("NASDAQ: SNDK", "ticker:sndk"),
        ("NYSE: BRK.B", "ticker:brk-b"),
        ("2330.TW", "ticker:2330"),
        ("CIK 320193", "cik:0000320193"),
    ],
)
def test_article_text_accepts_explicit_identifier_syntax(surface: str, expected: str) -> None:
    assert alias_key(surface, bare=False) == (expected, 1)
