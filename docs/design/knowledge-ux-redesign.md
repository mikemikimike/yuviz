# Knowledge UX redesign: per-document agent links, reindex, text view, quotas

Status: proposed, not built. Branch: `feature/knowledge-ux-redesign`.
Mockup: https://claude.ai/artifact/U7i45RhBdWT7NtadLPVqgy (Knowledge page empty and populated, document detail panel, agent Knowledge tab).

## Goal

Make knowledge as simple as a flat list of files:

- The Knowledge page lists every document in the tenant, with upload, reindex, replace, rename and delete on the row, and a detail panel showing the extracted text.
- An agent picks any set of documents. A document can be used by many agents in the same tenant.
- The agent's Knowledge tab has two cards, Documents and Search settings, and one Save.
- Usage tiles show files and storage against per-tenant limits.

Folders, "Used by", usage modes and per-folder switches leave the UI.

## What exists today

| Area | Today | Where |
|---|---|---|
| Upload | PDF, TXT, MD, DOCX, PPTX, XLSX, HTML, CSV, TSV; 10 MB per file (`KNOWLEDGE_MAX_UPLOAD_BYTES`) | `routers/documents.py`, `AddSourceModal.tsx` |
| Folders | `knowledge_bases`; each upload goes into a folder chosen or created in the upload modal | `knowledge_bases.py`, `AddSourceModal.tsx` |
| Agent scope | `agent_knowledge_bases (agent_id, kb_id, enabled)`; toggles save immediately, one request each | `agent_kb.py`, `KnowledgeBasePanel.tsx` |
| Retrieval | On-demand `search_knowledge` tool; hybrid dense + lexical, scoped by `c.kb_id = ANY($kb_ids)` in four queries | `pipeline.py`, `vector_repository.py`, `retrieval.py` |
| Retrieval plan | Cached per (tenant, agent); invalidated by in-process writers plus a 30 s TTL | `retrieval_plan.py` |
| Search settings | `top_k`, `max_tokens`, `minimum_score`, `include_citations` per agent; separate Save | `retrieval_policies.py`, `KnowledgeBasePanel.tsx` |
| Document actions | Rename (PATCH title), Replace file, Delete, Retry (failed documents only) | `routers/documents.py`, knowledge page drawer |
| Reindex | CLI only: version bump, new job, atomic chunk swap, skips documents with a running job | `reingest.py` |
| Document fields | title, content type, `byte_size`, status, created date, chunk count | `documents.py` |
| Tokens | per chunk in `kb_chunks.token_count`; not summed per document | `knowledge_schema.sql` |
| Chunk order | `kb_chunks.chunk_index`, parents carry sections | `knowledge_schema.sql` |
| Prompt mode | `usage_mode='prompt'`: never searched, prepended to every `search_knowledge` result; files under 500 bytes switch to it automatically | `retrieval_plan.py`, `ingestion_worker.py` |
| Website import | "Coming soon" in the UI, no backend | `AddSourceModal.tsx` |
| Quotas | none | — |

## Gaps this design closes

1. Agents can attach whole folders only, not single documents.
2. Reindex has no HTTP route or button.
3. The extracted text of a document cannot be viewed.
4. The document list has no token total.
5. No file-count or storage limit per tenant, and nothing to show usage.
6. The Knowledge page shows folders, "Used by" and a Manage drawer instead of row actions.
7. The agent page shows folder switches, saves them immediately, and saves settings separately.
8. Prompt mode is shown as "Read on every call", which has not been true since retrieval became an on-demand tool.

Website import stays out of scope. A sketch is at the end so the UI and data model leave room for it.

## Design

### 1. Agent-to-document links

New table, the only source of an agent's knowledge scope once migrated:

```sql
CREATE TABLE IF NOT EXISTS agent_documents (
    agent_id    UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, document_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_documents_document ON agent_documents(document_id);
```

- No `tenant_id` column. Like `agent_knowledge_bases` and `agent_retrieval_policies`, it is a child table scoped through its parent: the RLS policy checks that the agent is visible (`WAVE_B_TABLES` in `tests/test_rls_coverage.py`).
- A trigger refuses a row whose agent and document belong to different tenants, as `agent_knowledge_bases_same_tenant()` does. It fails closed when the caller cannot see the row.
- Soft-deleted documents keep their link rows; retrieval already filters `deleted_at`. Hard deletes of either end cascade (agents are only soft-deleted in production; tests hard-delete them).

Folders stay as the storage and embedding-config grouping behind the scenes. New uploads from the flat Knowledge page go into one default folder per tenant (`slug = 'default'`, created on first upload). Existing folders and their documents are untouched.

**Migration.** The schema files are re-applied on every start and there is no migration framework, so the copy is guarded by a new `schema_data_migrations` marker table. It runs once: a link for every (agent, document) pair reachable through an enabled `agent_knowledge_bases` row and a non-deleted document of the same tenant. Agents answer from exactly the same documents the day it ships. The old `/agents/{id}/knowledge-bases` routes return 410, and the old table is dropped in a follow-up release once the links are verified.

**Behaviour change.** A newly uploaded document reaches no agent until someone selects it. Today, an upload into an attached folder reaches every agent using that folder.

### 2. Retrieval

- The plan query resolves the agent's linked, non-deleted, searchable documents and their folders. `kb_ids` becomes "folders that contain a linked document". It stays, because search is grouped by each folder's embedding config.
- The plan gains `document_ids`. Each of the four queries in `vector_repository.py` adds `AND c.document_id = ANY($n::uuid[])`. The `_load_prompt_matches` query and the chunk count are restricted the same way.
- The lexical document-frequency cache is keyed on sorted `kb_ids` today. The key gains a hash of the sorted `document_ids`, so two agents sharing a folder but not the same documents don't share stats.
- Plan invalidation: the `agent_documents` writer calls `invalidate_agent()`. Document delete already invalidates the tenant.
- The Redis "agent has knowledge" flag (`agent_kb:{tenant}:{agent}`, read by Conversation to decide whether to offer `search_knowledge`) keeps its key and is computed from document links. It is now true only when a live linked document exists; before, an attached but empty folder made it true.
- Latency: the plan is cached, so the turn path adds no query; the search gains one indexed filter. Measure with `EXPLAIN ANALYZE` on a tenant with at least 50k chunks.
- **Recall risk: already handled.** Dense search goes through HNSW and filters afterwards, so a narrow scope can return fewer than `top_k`. `PgVectorRepository._prepare_hnsw` already enables `hnsw.iterative_scan` on pgvector 0.8+ and widens `ef_search` on older versions, and `test_selective_kb_filter_still_returns_top_k_through_hnsw` covers it. The document filter is the same kind of post-filter.

Concurrency: searches are read-only and the plan is per agent, so agents sharing a document never interfere. Reindex swaps chunks in one transaction, so readers see the old or the new chunks, never a mix. All agents in a tenant share the per-tenant retrieval cap in `retrieval.py`; size it with many agents in mind.

### 3. API (knowledge service, port 8100)

| Route | Roles | Behaviour |
|---|---|---|
| `GET /agents/{agent_id}/documents` | tenant members | Linked documents with title, type, size, tokens, status |
| `PUT /agents/{agent_id}/documents` | superadmin, admin | Body `{document_ids: [...]}` replaces the whole set in one transaction. Unknown or foreign IDs get the same 404 as a missing agent. Invalidates the plan. Audited. |
| `POST /documents/{id}/reindex` | superadmin, admin | Works like retry, not like `reingest.py` (which only queues tenant-wide batches at background priority): bumps the version and queues a normal-priority job under a per-document advisory lock, so two clicks queue one job. A ready document keeps serving its old chunks until the worker swaps. 202 with the queued document; 409 if a job is pending or running or the document is not ready or failed. Audited. |
| `GET /documents/{id}/text` | tenant members | The text the agent searches, rebuilt from the indexed chunks (not a re-parse of the file): parent sections in order where they exist (`source: "sections"`), otherwise the fixed windows with each one's overlap with the previous removed (`source: "chunks"`). Capped at 200,000 characters with `truncated: true`. |
| `GET /tenants/{tenant_id}/knowledge-usage` | tenant members | `{documents, max_documents, storage_bytes, max_storage_bytes, max_upload_bytes}` |
| `GET /knowledge-bases/{kb_id}/documents` (existing) | unchanged | Gains `token_count`: the sum of child chunk tokens for the current version |

`PUT` replacing the full set is what lets the agent page have one Save: the page sends the document set, then the search settings. If the second call fails, the page says which part saved.

### 4. Quotas

- New nullable tenant columns `knowledge_max_documents INT` and `knowledge_max_storage_bytes BIGINT`, with CHECK constraints for positive values. NULL means the platform default from `KNOWLEDGE_DEFAULT_MAX_DOCUMENTS` and `KNOWLEDGE_DEFAULT_MAX_STORAGE_BYTES`.
- Only a superadmin can change them, through the config service tenant route, as with `number_purchase_enabled`.
- Enforcement is in the upload transaction, under `pg_advisory_xact_lock` keyed on the tenant, so two parallel uploads cannot both pass the last slot. Errors: 409 "file limit reached (N of N)" and 413 "storage full". Per-file size stays `KNOWLEDGE_MAX_UPLOAD_BYTES`.
- Replace must not count the file twice, and must not drop the document from agents. The upload route takes an optional `replaces` document id: the check leaves that document out, and the new document is linked to the same agents in the upload transaction. The admin UI's replace flow (upload, wait until ready, delete the original) passes it.
- Documents uploaded before `byte_size` existed count as 0 bytes. A one-off backfill reads their stored size.

### 5. Prompt mode

- Removed from the UI.
- The automatic path for files under 500 bytes stays, because those files have no embeddings and would otherwise never be found.
- The schema comment "injected into every turn" is corrected to "prepended to every `search_knowledge` result".
- Later: embed tiny files normally and retire prompt mode.

### 6. Admin UI

**Knowledge page**
- Usage tiles from the usage endpoint: files used and limit, storage used and limit, and the per-file cap.
- Upload area on the page, with the accepted types and the limit from the usage endpoint, not hard-coded copy.
- One flat Documents table: name, type, size, tokens, status, uploaded date, and row actions for reindex, replace, rename and delete. No folder or "Used by" columns.
- Clicking a ready row opens the detail panel: meta row, Reindex, and the extracted text from the text endpoint.
- Empty state "No documents yet".
- No website section until website import exists.

**Agent Knowledge tab**
- Documents card: a chip picker over the tenant's documents with search, a × on each chip, the selected count, and "Manage all files".
- Workflow editor: a stage's `knowledge_base_ids` only switches knowledge on or off for that stage (`workflow/runner.py`); scope still comes from the agent's documents. The stage picker that lists folders becomes a "Use knowledge in this stage" switch.
- Search settings card: the four existing fields.
- One page-level Cancel and Save changes.

The UI keeps its input bounds (`top_k` 1 to 50, `max_tokens` at least 100). The policy route gains the same bounds server-side; today it accepts any number.

## Network and access

- No new listeners. The new routes are on the existing knowledge service listener (port 8100) and behind its JWT auth and tenant checks. LAN peers can reach exactly what they can reach today.
- Writes are superadmin or admin only; reads are tenant members. Another tenant's IDs get the same 404 as a missing row.
- No new outbound traffic in this design. Website import would add tenant-chosen outbound HTTP; see the sketch.

## Tests

- Tenant isolation: link, text, reindex and usage routes refuse another tenant's IDs with the same 404; the same-tenant trigger refuses cross-tenant rows; RLS coverage includes `agent_documents`.
- Migration: every enabled folder link becomes document links; running it twice changes nothing.
- Retrieval scope: an agent finds only linked documents; two agents sharing a folder see different results; the lexical cache does not leak across scopes; a narrow scope in a large folder still returns `top_k` matches.
- Plan invalidation: removing a link stops answers from that document within the same process at once and within the TTL elsewhere.
- Reindex: a ready document re-queues, a running job returns 409, readers keep answering during the swap.
- Quotas: parallel uploads at the last slot admit one; replace counts the net change; a superadmin can change limits and an admin cannot.
- Text view: ordering, parent and child fallback, truncation.
- UI: page-level Save with partial-failure message, chip add and remove, row actions, detail panel.

## Phases

Phases 1 to 3 ship as one PR: after phase 1 the old folder-link routes return 410, and five admin screens still call them until phase 3 (agents list, new-agent wizard, agent Knowledge panel, workflow editor, Knowledge pages).

1. Schema, migration and retrieval switch, with the link routes and tests. Done on this branch.
2. Reindex route, text route, token totals and quotas with the usage route. Done on this branch.
3. Admin UI: Knowledge page and agent tab. Prompt mode removed from the UI.
4. Follow-up release: drop `agent_knowledge_bases`.

## Open questions

1. Default limits. Proposal: 100 documents and 100 MB per tenant, overridable per tenant.
2. Should a tenant member who is not an admin see extracted text? Proposal: yes, as they can already see titles.
3. pgvector version on production, which decides the recall fix.

## Later: website import (sketch, not in scope)

- Each crawled page becomes a document tagged with its site, so links, search, reindex and quotas work unchanged. The Websites section groups pages by site.
- The crawler fetches tenant-chosen URLs, so it needs SSRF protection: same-domain only, private and link-local addresses refused after every redirect and DNS lookup, page-count, size and time limits, and `robots.txt` respected.
- It runs in the ingestion worker, never on the call path.
