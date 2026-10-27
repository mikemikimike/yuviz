// Knowledge Service client. Uses the Config Service JWT; Knowledge only validates tokens.

import { getToken } from "./auth";
import { ApiError } from "./api";

const BASE_URL = process.env.NEXT_PUBLIC_KNOWLEDGE_SERVICE_URL || "http://localhost:8100";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken();
  const isFormData = options?.body instanceof FormData;
  const res = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers: {
      ...(isFormData ? {} : { "Content-Type": "application/json" }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options?.headers,
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      // response body wasn't JSON — fall back to statusText
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// ── Knowledge Bases ──────────────────────────────────────────────────────

export type KnowledgeBaseStatus = "active" | "inactive";

export interface KnowledgeBase {
  id: string;
  tenant_id: string;
  slug: string;
  name: string;
  description: string;
  embedding_config_id: string | null;
  status: KnowledgeBaseStatus;
  config_version: number;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeBaseCreate {
  slug: string;
  name: string;
  description?: string;
  embedding_config_id?: string | null;
}

export interface KnowledgeBaseUpdate {
  name?: string;
  description?: string;
  embedding_config_id?: string | null;
  status?: KnowledgeBaseStatus;
}

export const listKnowledgeBases = (tenantId: string) =>
  request<KnowledgeBase[]>(`/tenants/${tenantId}/knowledge-bases`);
export const createKnowledgeBase = (tenantId: string, body: KnowledgeBaseCreate) =>
  request<KnowledgeBase>(`/tenants/${tenantId}/knowledge-bases`, { method: "POST", body: JSON.stringify(body) });
export const updateKnowledgeBase = (kbId: string, body: KnowledgeBaseUpdate) =>
  request<KnowledgeBase>(`/knowledge-bases/${kbId}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteKnowledgeBase = (kbId: string) =>
  request<void>(`/knowledge-bases/${kbId}`, { method: "DELETE" });

// ── Documents ────────────────────────────────────────────────────────────

export type DocumentStatus = "pending" | "processing" | "ready" | "ready_degraded" | "failed";
export type UsageMode = "auto" | "prompt";

export interface KbDocument {
  id: string;
  kb_id: string;
  tenant_id: string;
  title: string;
  source_ref: string;
  content_type: string;
  language: string | null;
  tags: Record<string, unknown>;
  status: DocumentStatus;
  error: string | null;
  version: number;
  usage_mode: UsageMode;
  chunk_count: number;
  // Sum of the searchable chunks' tokens; 0 until the first indexing finishes.
  token_count: number;
  // Whether Retry will work now: failed (or a failed re-index) and no job already pending or running.
  retryable?: boolean;
  // An upload, retry or reindex job is queued or running.
  job_active?: boolean;
  // null for documents uploaded before sizes were recorded.
  byte_size: number | null;
  created_at: string;
  updated_at: string;
}

export const listDocuments = (kbId: string) => request<KbDocument[]>(`/knowledge-bases/${kbId}/documents`);

export const uploadDocument = (
  kbId: string,
  file: File,
  title: string,
  opts?: { language?: string | null; tags?: Record<string, unknown>; replaces?: string },
) => {
  const form = new FormData();
  form.append("file", file);
  form.append("title", title);
  if (opts?.language) form.append("language", opts.language);
  if (opts?.tags) form.append("tags", JSON.stringify(opts.tags));
  // The document this upload replaces: the quota counts the net change and the new file keeps its agents.
  if (opts?.replaces) form.append("replaces", opts.replaces);
  return request<KbDocument & { ingestion_job_id: string }>(`/knowledge-bases/${kbId}/documents`, {
    method: "POST",
    body: form,
  });
};

export interface DocumentUpdate {
  title?: string;
  language?: string;
  tags?: Record<string, unknown>;
  usage_mode?: UsageMode;
}

export const updateDocument = (documentId: string, body: DocumentUpdate) =>
  request<KbDocument>(`/documents/${documentId}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteDocument = (documentId: string) =>
  request<void>(`/documents/${documentId}`, { method: "DELETE" });

export const retryDocument = (documentId: string) =>
  request<KbDocument>(`/documents/${documentId}/retry`, { method: "POST" });
// Ready or failed documents only; a ready one keeps answering from its old chunks until the new ones are in.
export const reindexDocument = (documentId: string) =>
  request<KbDocument>(`/documents/${documentId}/reindex`, { method: "POST" });

export interface DocumentText {
  document_id: string;
  text: string;
  // "sections": whole sections in order; "chunks": the indexed windows joined without their overlap.
  source: "extracted" | "sections" | "chunks";
  truncated: boolean;
}

export const getDocumentText = (documentId: string) => request<DocumentText>(`/documents/${documentId}/text`);

export interface DocumentAgent {
  agent_id: string;
  agent_slug: string;
  agent_name: string;
  tenant_id: string;
}

export const listDocumentAgents = (documentId: string) =>
  request<DocumentAgent[]>(`/documents/${documentId}/agents`);

export interface KnowledgeUsage {
  documents: number;
  max_documents: number;
  storage_bytes: number;
  max_storage_bytes: number;
  max_upload_bytes: number;
}

export const getKnowledgeUsage = (tenantId: string) => request<KnowledgeUsage>(`/tenants/${tenantId}/knowledge-usage`);

// ── Reverse lookup: which agents use this KB ────────────────────────────

export interface KbAgent {
  agent_id: string;
  kb_id: string;
  enabled: boolean;
  created_at: string;
  agent_slug: string;
  agent_name: string;
  tenant_id: string;
}

export const listKbAgents = (kbId: string) => request<KbAgent[]>(`/knowledge-bases/${kbId}/agents`);

// ── Agent ↔ document links ──────────────────────────────────────────────

export interface AgentDocument {
  document_id: string;
  kb_id: string;
  title: string;
  content_type: string;
  byte_size: number | null;
  status: DocumentStatus;
  linked_at: string;
}

export const listAgentDocuments = (agentId: string) => request<AgentDocument[]>(`/agents/${agentId}/documents`);
// Replaces the agent's whole set: what is not listed is unlinked.
export const replaceAgentDocuments = (agentId: string, documentIds: string[]) =>
  request<AgentDocument[]>(`/agents/${agentId}/documents`, {
    method: "PUT",
    body: JSON.stringify({ document_ids: documentIds }),
  });

// ── Retrieval Policy ─────────────────────────────────────────────────────
// Unset fields fall back to the server-side system default.

export interface RetrievalPolicy {
  agent_id: string;
  top_k?: number | null;
  max_tokens?: number | null;
  minimum_score?: number | null;
  rerank?: boolean | null;
  hybrid_search?: boolean | null;
  include_citations?: boolean | null;
}

export const getRetrievalPolicy = (agentId: string) =>
  request<RetrievalPolicy>(`/agents/${agentId}/retrieval-policy`);
export const setRetrievalPolicy = (agentId: string, body: Omit<RetrievalPolicy, "agent_id">) =>
  request<RetrievalPolicy>(`/agents/${agentId}/retrieval-policy`, { method: "PUT", body: JSON.stringify(body) });
