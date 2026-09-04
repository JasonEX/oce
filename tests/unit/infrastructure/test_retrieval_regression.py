"""Head-order regression guard over a small adversarial corpus.

The corpus is built so that the wrong chunk wins every lane that mixes scores:
call sites repeat a symbol more often than its definition, a legacy shim defines
the same name twice (which damps the exact score), documentation quotes the
signature, and unrelated modules mention the file names a path query asks for.
Dense recall is a deterministic term-frequency fake, so it prefers exactly those
repetitive chunks the way a cosine index did in the field.

The assertions are the acceptance lines for symbol/path/reference queries:
a definition or the named file must hold the first slot, structural queries
must not run the lanes their strategy skips, and reference queries must reach
a use site early. Everything below dense recall runs for real on SQLite.
"""

from __future__ import annotations

import math
from collections import Counter

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from oce.application.service import compute_blob_name
from oce.domain.services.indexing import IndexingPipeline
from oce.domain.services.lexical import lexical_tokens
from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.retrieval import RetrievalPipeline
from oce.domain.services.search import SearchHit, SearchScope, VectorRecord
from oce.infrastructure.astchunk.symbol_provider import TreeSitterSymbolProvider
from oce.infrastructure.chunkers.factory import build_chunker
from oce.infrastructure.persistence.lexical_index import (
    SqlLexicalSearchStore,
    create_lexical_table,
)
from oce.infrastructure.persistence.path_content_store import SqlPathContentStore
from oce.infrastructure.persistence.path_lookup_store import SqlPathLookupStore
from oce.infrastructure.persistence.symbol_search_store import SymbolSearchStore
from oce.infrastructure.persistence.uow import SqlAlchemyUnitOfWork
from oce.infrastructure.regex_symbol_provider import RegexSymbolProvider
from oce.shared.config.settings import RetrievalSettings
from oce.shared.database.session import Base
from oce.shared.metrics import RetrievalAudit

FILES = {
    "src/billing/invoice.py": (
        "from billing.tax import compute_tax_rate\n"
        "\n"
        "\n"
        "class InvoiceBuilder:\n"
        '    """Accumulates line items before the totals are computed."""\n'
        "\n"
        "    def __init__(self, currency):\n"
        "        self.currency = currency\n"
        "        self.lines = []\n"
        "\n"
        "    def add_line(self, description, amount):\n"
        "        self.lines.append((description, amount))\n"
        "        return self\n"
        "\n"
        "\n"
        "def build_invoice(customer, lines, region):\n"
        '    """Create the invoice document for one customer."""\n'
        "    builder = InvoiceBuilder(customer.currency)\n"
        "    for description, amount in lines:\n"
        "        builder.add_line(description, amount)\n"
        "    rate = compute_tax_rate(region)\n"
        "    subtotal = sum(amount for _, amount in builder.lines)\n"
        '    return {"customer": customer.id, "total": subtotal * (1 + rate)}\n'
        "\n"
        "\n"
        "def apply_discount(document, percent):\n"
        '    """Reduce the invoice total by a percentage."""\n'
        '    document["total"] = document["total"] * (1 - percent / 100)\n'
        "    return document\n"
    ),
    "src/billing/legacy.py": (
        '"""Compatibility shim kept for the old billing CLI."""\n'
        "\n"
        "from billing import invoice as _invoice\n"
        "\n"
        "\n"
        "def build_invoice(customer, lines, region=None):\n"
        "    return _invoice.build_invoice(customer, lines, region or customer.region)\n"
    ),
    "src/billing/tax.py": (
        "TAX_RATES = {\n"
        '    "eu": 0.2,\n'
        '    "us": 0.07,\n'
        "}\n"
        "\n"
        "\n"
        "class TaxTable:\n"
        '    """Region-keyed tax rates loaded from settings."""\n'
        "\n"
        "    def __init__(self, rates):\n"
        "        self.rates = dict(rates)\n"
        "\n"
        "    def lookup(self, region):\n"
        "        return self.rates[region]\n"
        "\n"
        "\n"
        "def compute_tax_rate(region):\n"
        '    """Resolve the tax rate for a region code."""\n'
        "    if region not in TAX_RATES:\n"
        '        raise KeyError(f"unknown tax region {region}")\n'
        "    return TAX_RATES[region]\n"
    ),
    "src/billing/api.py": (
        "from billing.config.settings import load_settings\n"
        "from billing.invoice import apply_discount, build_invoice\n"
        "from billing.tax import compute_tax_rate\n"
        "\n"
        "\n"
        "def create_invoice_endpoint(request):\n"
        "    # build invoice for the primary customer, then build invoice copies\n"
        "    # for every linked account so each build_invoice call shares settings\n"
        "    settings = load_settings(request.settings_path)\n"
        "    invoice = build_invoice(request.customer, request.lines, request.region)\n"
        "    linked = [\n"
        "        build_invoice(account, request.lines, request.region)\n"
        "        for account in request.linked_accounts\n"
        "    ]\n"
        "    if request.coupon:\n"
        "        invoice = apply_discount(invoice, request.coupon.percent)\n"
        "    invoice_rate = compute_tax_rate(request.region)\n"
        "    linked_rates = [compute_tax_rate(a.region) for a in request.linked_accounts]\n"
        "    return {\n"
        '        "invoice": invoice,\n'
        '        "linked": linked,\n'
        '        "currency": settings.currency,\n'
        '        "rates": [invoice_rate, *linked_rates],\n'
        "    }\n"
        "\n"
        "\n"
        "def preview_invoice_endpoint(request):\n"
        "    # build invoice without persisting; settings decide the currency\n"
        "    settings = load_settings(request.settings_path)\n"
        "    invoice = build_invoice(request.customer, request.lines, request.region)\n"
        '    invoice["currency"] = settings.currency\n'
        "    return invoice\n"
    ),
    "src/billing/cli.py": (
        "import sys\n"
        "\n"
        "from billing.invoice import apply_discount, build_invoice\n"
        "\n"
        "\n"
        "def main(argv=None):\n"
        "    argv = list(sys.argv[1:] if argv is None else argv)\n"
        "    customer, region, percent = argv[0], argv[1], float(argv[2])\n"
        '    lines = [("consulting", 100.0)]\n'
        "    document = build_invoice(customer, lines, region)\n"
        "    if percent:\n"
        "        document = apply_discount(document, percent)\n"
        "    print(document)\n"
        "    return 0\n"
    ),
    "src/billing/config/settings.py": (
        "import json\n"
        "\n"
        'DEFAULT_CURRENCY = "USD"\n'
        "\n"
        "\n"
        "class Settings:\n"
        "    def __init__(self, currency, tax_rates):\n"
        "        self.currency = currency\n"
        "        self.tax_rates = tax_rates\n"
        "\n"
        "\n"
        "def load_settings(path):\n"
        '    """Read the billing settings file from disk."""\n'
        '    with open(path, encoding="utf-8") as handle:\n'
        "        raw = json.load(handle)\n"
        '    return Settings(raw.get("currency", DEFAULT_CURRENCY), raw.get("tax", {}))\n'
    ),
    "src/billing/__init__.py": (
        "from billing.invoice import InvoiceBuilder, apply_discount, build_invoice\n"
        "from billing.tax import TaxTable, compute_tax_rate\n"
    ),
    "tests/test_invoice.py": (
        "from billing.invoice import apply_discount, build_invoice\n"
        "\n"
        "\n"
        "def test_build_invoice_totals():\n"
        '    document = build_invoice(_customer(), [("a", 10.0)], "eu")\n'
        '    assert document["total"] == 12.0\n'
        "\n"
        "\n"
        "def test_build_invoice_rejects_unknown_region():\n"
        "    try:\n"
        '        build_invoice(_customer(), [], "mars")\n'
        "    except KeyError:\n"
        "        pass\n"
        "\n"
        "\n"
        "def test_apply_discount_after_build_invoice():\n"
        '    document = build_invoice(_customer(), [("a", 10.0)], "us")\n'
        '    assert apply_discount(document, 50)["total"] == 5.35\n'
    ),
    "docs/invoice.md": (
        "# Invoices\n"
        "\n"
        "The `build_invoice` function builds an invoice document.\n"
        "\n"
        "```python\n"
        "def build_invoice(customer, lines, region):\n"
        "    ...\n"
        "```\n"
        "\n"
        "Settings live in `settings.py`; tax rates come from `tax.py`.\n"
        "Use `apply_discount` after `build_invoice` to apply coupons.\n"
    ),
    "README.md": (
        "# billing\n"
        "\n"
        "Invoice generation. See docs/invoice.md for build_invoice and the\n"
        "settings.py file format. Tax rates are in tax.py.\n"
    ),
}


class FakeEmbedder:
    async def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]

    async def embed_query(self, text):
        return [1.0, 0.0]


class RecordingVectorIndex:
    def __init__(self):
        self.records: list[VectorRecord] = []

    async def upsert(self, records):
        self.records.extend(records)

    async def delete(self, blob_names):
        pass


class TermFrequencyDense:
    """Deterministic dense stand-in that rewards repetition like a real index.

    The pipeline never sees the query vector; the fake keeps the query text
    from the last ``embed_query`` call and scores chunks by how often its
    lexical tokens recur in the chunk, so a call site that names a symbol
    four times outranks the definition that names it once.
    """

    def __init__(self, index: RecordingVectorIndex, embedder: QueryCapture):
        self.index = index
        self.embedder = embedder

    async def search(
        self, *, query_vector, allowed_blob_names=None, top_k=50, vector_threshold=0.0
    ):
        allowed = set(allowed_blob_names or [])
        query = Counter(lexical_tokens(self.embedder.last_query))
        scored: list[tuple[float, int, VectorRecord]] = []
        for position, record in enumerate(self.index.records):
            if allowed and record.blob_name not in allowed:
                continue
            document = Counter(
                lexical_tokens(f"{record.context or ''}\n{record.content}")
            )
            overlap = sum(document[token] for token in query)
            if overlap == 0:
                continue
            score = overlap / (1.0 + math.log(1 + sum(document.values())))
            scored.append((score, position, record))
        scored.sort(key=lambda item: (-item[0], item[1]))
        top = scored[:top_k]
        best = top[0][0] if top else 1.0
        return [
            SearchHit(
                blob_name=record.blob_name,
                path=record.path,
                content=record.content,
                score=round(score / best, 4),
                content_hash=record.content_hash,
                start_line=record.start_line,
                end_line=record.end_line,
                context=record.context,
            )
            for score, _, record in top
        ]


class QueryCapture(FakeEmbedder):
    last_query = ""

    async def embed_query(self, text):
        self.last_query = text
        return [1.0, 0.0]


@pytest.fixture
async def indexed():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(create_lexical_table)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    vector_index = RecordingVectorIndex()
    provider = TreeSitterSymbolProvider(RegexSymbolProvider())
    chunker = build_chunker(
        semantic_enabled=True,
        semantic_max_chunk_chars=600,
        recursive_chunk_size=6000,
        recursive_chunk_overlap=200,
    )
    names = {}
    async with SqlAlchemyUnitOfWork(sessions, provider) as uow:
        pipeline = IndexingPipeline(
            chunker=chunker,
            embedder=FakeEmbedder(),
            vector_index=vector_index,
            blob_repo=uow.blobs,
            chunk_repo=uow.chunks,
            symbol_projection=uow.symbols,
            lexical_projection=uow.lexical,
        )
        for path, content in FILES.items():
            name = compute_blob_name(path, content)
            names[path] = name
            await pipeline.ingest(name, path, content)
        await pipeline.embed_pending(list(names.values()))
        await uow.commit()
    yield sessions, vector_index, names
    await engine.dispose()


def _pipeline(sessions, vector_index, **overrides):
    embedder = QueryCapture()
    settings = RetrievalSettings(confidence_floor=0.0, **overrides)
    return RetrievalPipeline(
        embedder=embedder,
        store=TermFrequencyDense(vector_index, embedder),
        exact_store=SymbolSearchStore(sessions),
        lexical_store=SqlLexicalSearchStore(sessions),
        path_lookup_store=SqlPathLookupStore(sessions),
        path_content_store=SqlPathContentStore(sessions),
        settings=settings,
    )


async def _search(indexed, query, **overrides):
    sessions, vector_index, names = indexed
    audit = RetrievalAudit()
    hits = await _pipeline(sessions, vector_index, **overrides).search(
        query, SearchScope(frozenset(names.values())), audit=audit
    )
    return [hit for hit in hits if hit.role == "primary"], audit


SYMBOL_QUERIES = [
    ("Where is `build_invoice` defined?", "def build_invoice"),
    ("`compute_tax_rate` 函数在哪里定义？", "def compute_tax_rate"),
    ("Where is the `InvoiceBuilder` class defined?", "class InvoiceBuilder"),
    ("`apply_discount` 在哪里定义", "def apply_discount"),
    ("Where is `load_settings` defined?", "def load_settings"),
    ("`TaxTable` 类型定义", "class TaxTable"),
]

PATH_QUERIES = [
    ("Where is settings.py?", "src/billing/config/settings.py"),
    ("tax.py 文件在哪里", "src/billing/tax.py"),
    ("Show me the invoice.py file", "src/billing/invoice.py"),
    ("cli.py", "src/billing/cli.py"),
]

REFERENCE_QUERIES = [
    (
        "Where is `build_invoice` used?",
        {"src/billing/api.py", "src/billing/cli.py"},
        "src/billing/invoice.py",
    ),
    (
        "哪些地方引用了 `compute_tax_rate`？",
        {"src/billing/api.py"},
        "src/billing/tax.py",
    ),
    (
        "Which modules import `apply_discount`?",
        {"src/billing/api.py", "src/billing/cli.py"},
        "src/billing/invoice.py",
    ),
]


@pytest.mark.parametrize(("query", "marker"), SYMBOL_QUERIES)
async def test_symbol_definition_holds_the_first_slot(indexed, query, marker):
    hits, audit = await _search(indexed, query)
    assert audit.intent == QueryIntent.SYMBOL.value
    assert hits, query
    assert marker in hits[0].content, [hit.path for hit in hits[:3]]
    assert not hits[0].path.startswith(("docs/", "tests/"))
    # The structural lane answered; the lexical fallback and the relation
    # expansion are reserved for queries without a deterministic answer.
    assert "lexical" not in audit.stages
    assert "expand" not in audit.stages
    # No reranker is wired here, so the route only proves nothing else ran.
    assert audit.rerank_route and audit.rerank_route.startswith("skip:")


@pytest.mark.parametrize(("query", "path"), PATH_QUERIES)
async def test_named_file_holds_the_first_slot(indexed, query, path):
    hits, audit = await _search(indexed, query)
    assert audit.intent == QueryIntent.PATH.value
    assert hits, query
    assert hits[0].path == path, [hit.path for hit in hits[:3]]
    assert "expand" not in audit.stages
    assert audit.rerank_route and audit.rerank_route.startswith("skip:")


@pytest.mark.parametrize(("query", "use_sites", "declaration"), REFERENCE_QUERIES)
async def test_reference_leads_with_a_use_site(indexed, query, use_sites, declaration):
    hits, audit = await _search(indexed, query)
    assert audit.intent == QueryIntent.REFERENCE.value
    assert "lexical" in audit.stages
    paths = [hit.path for hit in hits[:5]]
    # The question is where the symbol is used: a source use site leads, the
    # declaration stays available but behind it, tests and docs never lead.
    assert paths[0] in use_sites, paths
    assert declaration in {hit.path for hit in hits}, paths
    assert not paths[0].startswith(("tests/", "docs/"))


async def test_traceback_locates_the_raising_code(indexed):
    hits, audit = await _search(
        indexed,
        "Invoice creation fails for some regions:\n"
        "Traceback (most recent call last):\n"
        '  File "/srv/billing/src/billing/api.py", line 10, in create_invoice_endpoint\n'
        '  File "/srv/billing/src/billing/tax.py", line 20, in compute_tax_rate\n'
        "KeyError: 'unknown tax region mars'\n",
    )
    # Both traceback frames are deterministic path evidence; the raising
    # frame must be in the first two and carry the failing line itself.
    assert {hit.path for hit in hits[:2]} <= {
        "src/billing/api.py",
        "src/billing/tax.py",
    }
    raising = [hit for hit in hits[:2] if hit.path == "src/billing/tax.py"]
    assert raising and "unknown tax region" in raising[0].content


async def test_corpus_stays_adversarial(indexed, monkeypatch):
    """The guard above must be load-bearing.

    If fusing dense recall with the exact lane no longer pushes a call site
    ahead of the damped duplicate definition, the corpus has stopped modelling
    the failure this file exists to catch and needs to be made harder again.
    """
    monkeypatch.setattr(
        RetrievalPipeline,
        "_structural_heads",
        lambda self, state, hits, priority_factor=None: (),
    )
    hits, _ = await _search(indexed, "Where is `build_invoice` defined?")
    assert "def build_invoice" not in hits[0].content
