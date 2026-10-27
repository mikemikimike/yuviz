"""Shared fixtures for the retrieval tests and the latency benchmark: a deterministic bag-of-words embedder (texts
sharing words really are closer in cosine terms) and helpers that seed a KB with parent/child chunks."""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
import re
from typing import Any

from services.config import provider_configs
from services.knowledge import agent_kb as agent_kb_service
from services.knowledge import cache
from services.knowledge import retrieval_plan as plans
from services.knowledge import knowledge_bases as kb_service
from services.knowledge.embedding_manager import EmbeddingProviderManager
from services.knowledge.secret_resolver import CompositeSecretResolver

DIMS = 768
_TOKEN_RE = re.compile(r"\w+")
_word_vectors: dict[str, list[float]] = {}


def _word_vector(word: str) -> list[float]:
    vec = _word_vectors.get(word)
    if vec is None:
        rng = random.Random(int(hashlib.sha256(word.encode()).hexdigest()[:16], 16))
        vec = [rng.gauss(0.0, 1.0) for _ in range(DIMS)]
        _word_vectors[word] = vec
    return vec


def embed_text(text: str) -> list[float]:
    total = [0.0] * DIMS
    for word in _TOKEN_RE.findall(text.casefold()):
        for i, x in enumerate(_word_vector(word)):
            total[i] += x
    norm = math.sqrt(sum(x * x for x in total)) or 1.0
    return [x / norm for x in total]


class FakeEmbedder:
    """latency_s sleeps before answering (inject 60 ms for the miss benchmark, 5 s for a hung embedder);
    fail=True raises; calls counts embed() invocations."""

    def __init__(self, latency_s: float = 0.0, fail: bool = False) -> None:
        self.latency_s = latency_s
        self.fail = fail
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if self.fail:
            raise RuntimeError("embedding provider down")
        return [embed_text(t) for t in texts]


def make_manager(provider: Any) -> EmbeddingProviderManager:
    async def factory(cfg, api_key):
        return provider
    return EmbeddingProviderManager(CompositeSecretResolver(), registry={"ollama": factory})


def vec_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


async def make_kb(pool, tenant, agent, slug: str, *, embedding: bool = True, assign: bool = True) -> dict[str, Any]:
    cfg_id = None
    if embedding:
        cfg = await provider_configs.create_provider_config(
            tenant_id=tenant["id"], name=f"Embed-{slug}", role="embedding", engine="ollama",
            allow_pointer_schemes=False,
        )
        cfg_id = cfg["id"]
    kb = await kb_service.create_knowledge_base(
        tenant_id=tenant["id"], slug=slug, name=slug, embedding_config_id=cfg_id,
    )
    if assign:
        await use_kb(pool, agent["id"], kb["id"])
    return kb


# Tests still think in "this agent uses this KB": each such pair links the KB's documents, present and future.
_kb_agents: dict[str, set[str]] = {}


async def _refresh(pool, agent_id: Any) -> None:
    """agent_kb._refresh_flag through the superuser pool, so callers need no tenant scope."""
    row = await pool.fetchrow(
        "SELECT a.slug AS agent_slug, t.slug AS tenant_slug FROM agents a JOIN tenants t ON t.id = a.tenant_id "
        "WHERE a.id = $1", agent_id,
    )
    plans.invalidate_agent(row["tenant_slug"], row["agent_slug"])
    has = await pool.fetchval(agent_kb_service._HAS_DOCUMENTS_SQL.format(where="WHERE ad.agent_id = $1"), agent_id)
    await cache.set_has_enabled_kb(row["tenant_slug"], row["agent_slug"], bool(has))


async def use_kb(pool, agent_id: Any, kb_id: Any) -> None:
    _kb_agents.setdefault(str(kb_id), set()).add(str(agent_id))
    await pool.execute(
        "INSERT INTO agent_documents (agent_id, document_id) "
        "SELECT $1, d.id FROM kb_documents d WHERE d.kb_id = $2 ON CONFLICT DO NOTHING",
        agent_id, kb_id,
    )
    await _refresh(pool, agent_id)


async def link_to_kb_agents(pool, kb_id: Any, document_id: Any) -> None:
    """Link a document uploaded through the API to the agents using its KB, as add_document does."""
    for agent_id in _kb_agents.get(str(kb_id), ()):
        await pool.execute(
            "INSERT INTO agent_documents (agent_id, document_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            agent_id, document_id,
        )
        await _refresh(pool, agent_id)


async def add_document(
    pool, tenant, kb, title: str, chunks: list[str], *, parent: str | None = None, status: str = "ready",
    usage_mode: str = "auto", embed: bool = True, deleted: bool = False,
) -> dict[str, Any]:
    """Insert a document whose children are `chunks`; with `parent`, one parent row holds the section text."""
    doc = dict(await pool.fetchrow(
        "INSERT INTO kb_documents (kb_id, tenant_id, title, source_ref, content_type, status, usage_mode, deleted_at) "
        "VALUES ($1, $2, $3, 'inline', 'text/plain', $4, $5, CASE WHEN $6 THEN now() END) RETURNING *",
        kb["id"], tenant["id"], title, status, usage_mode, deleted,
    ))
    parent_id = None
    if parent is not None:
        parent_id = await pool.fetchval(
            "INSERT INTO kb_chunks (document_id, kb_id, tenant_id, chunk_index, content, token_count, is_parent) "
            "VALUES ($1, $2, $3, -1, $4, $5, true) RETURNING id",
            doc["id"], kb["id"], tenant["id"], parent, len(parent.split()),
        )
    ids = []
    for i, text in enumerate(chunks):
        ids.append(await pool.fetchval(
            "INSERT INTO kb_chunks (document_id, kb_id, tenant_id, chunk_index, content, token_count, embedding, "
            "parent_id) VALUES ($1, $2, $3, $4, $5, $6, $7::vector, $8) RETURNING id",
            doc["id"], kb["id"], tenant["id"], i, text, len(text.split()),
            vec_literal(embed_text(text)) if embed else None, parent_id,
        ))
    doc["chunk_ids"] = [str(i) for i in ids]
    doc["parent_id"] = str(parent_id) if parent_id is not None else None
    await link_to_kb_agents(pool, kb["id"], doc["id"])
    return doc
