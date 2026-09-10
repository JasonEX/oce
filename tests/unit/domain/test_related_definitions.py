"""Definition selection preserves the provenance of mined names after trimming."""

from oce.domain.services.related_definitions import (
    DefinitionCandidates,
    select_related_definitions,
)
from oce.domain.services.search import DefinitionHit, SearchHit, search_hit_key


def test_names_mined_only_from_removed_primary_chunks_are_not_expanded():
    kept = SearchHit("a", "src/main.py", "return 1", 1)
    removed = SearchHit("b", "src/extra.py", "loadArchive()", 0.5)
    declared = SearchHit("c", "src/archive.py", "def loadArchive():\n    return 1", 0)
    evidence = DefinitionCandidates(
        source_hits=(kept, removed),
        calls_by_source={search_hit_key(removed): ("loadArchive",)},
        names=("loadArchive",),
        definitions=(DefinitionHit("loadArchive", "definition", declared, 1, 2),),
    )
    assert (
        select_related_definitions(
            evidence, [kept], max_chars=1000, max_symbols=3, snippet_lines=10
        )
        == []
    )
    assert select_related_definitions(
        evidence, [kept, removed], max_chars=1000, max_symbols=3, snippet_lines=10
    )
