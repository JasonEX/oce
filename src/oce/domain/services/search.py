"""检索领域类型 - SearchHit 值对象 + SearchStore 协议

SearchHit 是检索命中的不可变值对象；
SearchStore 是向量检索的存储抽象，
由基础设施层实现，领域层只依赖此协议。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

# 命中在最终结果中的角色：primary 是回答查询的片段，其余是按关系车道附带的摘录：
# related 是被引用符号的定义，caller 是调用方，implementation 是子类/实现，
# test 是覆盖该符号的测试，reexport 是转出该符号的入口文件。
HitRole = Literal["primary", "related", "caller", "implementation", "test", "reexport"]


@dataclass(frozen=True)
class SearchHit:
    """检索命中的代码片段"""

    blob_name: str
    path: str
    content: str
    score: float
    content_hash: str = ""
    start_line: int = 1
    end_line: int = 1
    # 切块时记录的封闭作用域签名链（如 ``class Foo > def bar``）；无 AST 时为 None。
    context: str | None = None
    role: HitRole = "primary"
    hop: int | None = None


@dataclass(frozen=True)
class SearchScope:
    """One resolved workspace scope shared by every retrieval backend.

    ``blob_names`` is the authoritative materialized scope used by Milvus.  When
    the scope came from a checkpoint, the chain metadata lets SQL stores apply
    the same scope as a relation instead of expanding every member into an
    ``IN`` clause.  Request deltas remain explicit because they have not been
    committed to the checkpoint yet.
    """

    blob_names: frozenset[str]
    chain_id: str | None = None
    chain_version: int | None = None
    added_blob_names: frozenset[str] = frozenset()
    deleted_blob_names: frozenset[str] = frozenset()


# One source occurrence: (blob_name, path, start_line, end_line, content identity).
SearchHitKey = tuple[str, str, int, int, str]


def search_hit_key(hit: SearchHit) -> SearchHitKey:
    """Identify one source occurrence, including legacy hits without a hash."""
    return (
        hit.blob_name,
        hit.path,
        hit.start_line,
        hit.end_line,
        hit.content_hash or hit.content,
    )


class SearchStore(Protocol):
    """检索存储接口（Milvus 3.0：向量检索）"""

    async def search(
        self,
        *,
        query_vector: list[float],
        allowed_blob_names: Sequence[str] | None = None,
        top_k: int = 50,
        vector_threshold: float = 0.0,
    ) -> list[SearchHit]:
        """向量检索，返回按相似度降序的命中列表

        allowed_blob_names 非空时做索引级过滤（范围外不参与排序）。
        """
        ...


@dataclass(frozen=True)
class DefinitionHit:
    """One symbol definition inside an indexed chunk.

    ``hit`` spans the whole chunk; ``start_line``/``end_line`` are the
    definition's own lines so callers can cut a signature-sized excerpt.
    """

    identifier: str
    kind: str
    hit: SearchHit
    start_line: int
    end_line: int


class ExactSearchStore(Protocol):
    """按代码标识符精确召回已索引片段。"""

    async def search_exact(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        top_k: int = 50,
        kinds: Sequence[str] | None = None,
    ) -> list[SearchHit]:
        """``kinds`` 限定 occurrence 种类（endpoint/definition/import）；None 不限。"""
        ...

    async def find_definitions(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        max_per_identifier: int = 3,
    ) -> list[DefinitionHit]:
        """Definitions/endpoints of the identifiers whose scope-wide count fits the cap."""
        ...

    async def occurrence_kinds(
        self,
        occurrences: Sequence[tuple[str, str]],
        scope: SearchScope,
    ) -> dict[tuple[str, str], frozenset[str]]:
        """Symbol occurrence kinds for scoped ``(blob_name, content_hash)`` pairs."""
        ...


class LexicalSearchStore(Protocol):
    """词法召回：对 chunk 词元索引做 term/phrase 匹配，按词法相关度排序。

    ``required`` 是必须至少命中一个的词元组：引用类查询用它把「真正用到这个
    标识符」的片段和「只是碰到同样子词」的片段分开；排序仍按全部 terms 计算。
    """

    async def search_lexical(
        self,
        *,
        terms: Sequence[str],
        phrases: Sequence[str],
        scope: SearchScope,
        top_k: int = 30,
        required: Sequence[str] = (),
    ) -> list[SearchHit]: ...


class PathLookupStore(Protocol):
    """Exact path/basename lookup inside the scope; no embedding involved."""

    async def match_paths(
        self,
        *,
        filenames: Sequence[str],
        paths: Sequence[str],
        scope: SearchScope,
        limit: int = 20,
    ) -> dict[str, float]:
        """Return ``blob_name -> score`` (1.0 full-path suffix, 0.9 basename)."""
        ...


@dataclass(frozen=True)
class VectorRecord:
    """One chunk occurrence with its vector, ready for the vector index."""

    chunk_id: str
    content_hash: str
    blob_name: str
    path: str
    content: str
    start_line: int
    end_line: int
    vector: list[float]
    context: str | None = None


class VectorIndex(Protocol):
    """向量索引写路径。"""

    async def upsert(self, records: Sequence[VectorRecord]) -> None: ...

    async def delete(self, blob_names: Sequence[str]) -> None: ...
