"""T24 alias file and pre-warm list validation, and SEC state-tag aliases."""

import pytest
from app.aliases import AliasFileError, parse_alias_file, read_alias_file
from app.prewarm import read_prewarm_list
from app.sec_catalog import name_aliases


def test_the_committed_alias_file_is_valid():
    entries = read_alias_file()
    by_entity = {entry.entity: entry.names for entry in entries}
    assert by_entity["TSM"] == ("TSMC", "Taiwan Semiconductor")
    assert by_entity["ASML"] == ("ASML",) and by_entity["AMZN"] == ("Amazon",)
    assert all(entry.rationale.strip() for entry in entries)


def test_the_committed_prewarm_list_covers_the_t01_targets():
    entities = read_prewarm_list()
    assert len(entities) == 50
    assert {"AAPL", "TSM", "ASML", "SNDK", "UNH"} <= set(entities)


@pytest.mark.parametrize(("payload", "message"), [
    ({}, "aliases"),
    ({"aliases": [{"names": ["X"], "rationale": "r"}]}, "entity: required"),
    ({"aliases": [{"entity": "TSM", "names": [], "rationale": "r"}]}, "at least one name"),
    ({"aliases": [{"entity": "TSM", "names": ["TSMC"]}]}, "rationale: required"),
    ({"aliases": [{"entity": "TSM", "names": ["Inc."], "rationale": "r"}]}, "normalizes"),
    ({"aliases": [{"entity": "TSM", "names": ["X"], "rationale": "r", "weight": 1}]},
     "unknown fields"),
    ({"aliases": [{"entity": "TSM", "names": ["Taiwan Semi"], "rationale": "r"},
                  {"entity": "TSMC_OTHER", "names": ["taiwan semi"], "rationale": "r"}]},
     "listed for both"),
])
def test_invalid_alias_files_are_rejected(payload, message):
    with pytest.raises(AliasFileError, match=message):
        parse_alias_file(payload)


@pytest.mark.parametrize(("title", "aliases"), [
    ("APPLIED MATERIALS INC /DE", ("appliedmaterialsde", "appliedmaterials")),
    ("QUALCOMM INC/DE", ("qualcommde", "qualcomm")),
    ("CACI INTERNATIONAL INC /DE/", ("caciinternationalde", "caciinternational")),
    ("ARM HOLDINGS PLC /UK", ("armholdingsuk", "armholdings")),
    ("Apple Inc.", ("apple",)),
])
def test_sec_titles_also_get_an_alias_without_their_state_tag(title, aliases):
    assert name_aliases(title) == aliases
