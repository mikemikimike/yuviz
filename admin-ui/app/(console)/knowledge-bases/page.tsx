"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { ArrowUpRight, BookOpen, CloudUpload, FileText, FileUp, Pencil, RefreshCw, Search, Trash2 } from "lucide-react";
import { ApiError, getCurrentUser } from "@/lib/api";
import { useActiveTenant } from "@/lib/useActiveTenant";
import {
  DocumentAgent,
  DocumentText,
  KbDocument,
  KnowledgeUsage,
  deleteDocument,
  getDocumentText,
  getKnowledgeUsage,
  listDocumentAgents,
  listDocuments,
  listKnowledgeBases,
  reindexDocument,
  updateDocument,
  uploadDocument,
} from "@/lib/knowledgeApi";
import { replaceKnowledgeFile } from "@/lib/replaceKnowledgeFile";
import { FILE_STATUS } from "@/lib/knowledgePresentation";
import {
  ACCEPTED_DOC_ACCEPT,
  ACCEPTED_DOC_LABEL,
  ensureDefaultKnowledgeBase,
  rejectionReasonFor,
} from "@/lib/knowledgeUpload";
import { Modal } from "@/components/Modal";

interface DocRow extends KbDocument {
  tenantName: string;
  tenantSlug: string;
}

const POLL_MS = 4000;

function sourceType(doc: KbDocument): string {
  const ct = (doc.content_type || "").toLowerCase();
  if (ct.includes("pdf")) return "PDF";
  if (ct.includes("markdown")) return "Markdown";
  if (ct.includes("csv")) return "CSV";
  if (ct.includes("html")) return "HTML";
  if (ct.includes("word") || ct.includes("docx")) return "Word";
  if (ct.includes("presentation")) return "PowerPoint";
  if (ct.includes("spreadsheet")) return "Excel";
  if (ct.includes("text")) return "Text";
  return "File";
}

function formatBytes(bytes: number | null): string {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

const preparing = (d: KbDocument) => d.status === "pending" || d.status === "processing";

export default function KnowledgeBasesPage() {
  const { tenant, allTenants, isAllTenants, loading: tenantLoading, error: accountError } = useActiveTenant();
  const [rows, setRows] = useState<DocRow[]>([]);
  const [usage, setUsage] = useState<KnowledgeUsage | null>(null);
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notices, setNotices] = useState<string[]>([]);
  const [canManage, setCanManage] = useState(false);
  const [search, setSearch] = useState("");
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [detailText, setDetailText] = useState<DocumentText | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [renameTarget, setRenameTarget] = useState<DocRow | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<{ row: DocRow; agents: DocumentAgent[] | null } | null>(null);
  const uploadInput = useRef<HTMLInputElement>(null);
  const replaceInput = useRef<HTMLInputElement>(null);
  const replaceTarget = useRef<DocRow | null>(null);
  const dropZone = useRef<HTMLDivElement>(null);
  const searchParams = useSearchParams();
  const router = useRouter();

  const scope = useMemo(() => ({ tenant, allTenants, isAllTenants }), [tenant, allTenants, isAllTenants]);
  const activeScope = useRef<typeof scope | null>(null);
  const current = () => activeScope.current === scope;
  const tenants = isAllTenants ? allTenants : tenant ? [tenant] : [];
  const uploadTenant = isAllTenants ? null : tenant;

  const load = async (background = false) => {
    if (!current()) return;
    if (!background) setLoading(true);
    setPageError(null);
    try {
      const perTenant = await Promise.all(tenants.map(async (t) => {
        const kbs = await listKnowledgeBases(t.id);
        const docs = await Promise.all(kbs.map((kb) => listDocuments(kb.id)));
        return docs.flat().map((d) => ({ ...d, tenantName: t.name, tenantSlug: t.slug }));
      }));
      if (!current()) return;
      setRows(perTenant.flat().sort((a, b) => b.created_at.localeCompare(a.created_at)));
      if (uploadTenant) setUsage(await getKnowledgeUsage(uploadTenant.id).catch(() => null));
      else setUsage(null);
    } catch (e) {
      if (current()) setPageError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      if (current()) setLoading(false);
    }
  };

  useEffect(() => {
    if (tenantLoading) return;
    activeScope.current = scope;
    // Reset per-account state when the header account selection changes.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setActionError(null);
    setNotices([]);
    setDetailId(null);
    setRenameTarget(null);
    setDeleteTarget(null);
    load();
    getCurrentUser()
      .then((me) => setCanManage(me.role === "superadmin" || me.role === "admin"))
      .catch(() => {});  // Leave canManage false: write controls fail closed.
    return () => { activeScope.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, tenantLoading]);

  // Only files still being prepared change on their own; re-read while any are.
  const anyPreparing = rows.some(preparing);
  useEffect(() => {
    if (!anyPreparing) return;
    const timer = setTimeout(() => load(true), POLL_MS);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, anyPreparing]);

  const detail = rows.find((r) => r.id === detailId) ?? null;

  const openDetail = (id: string) => {
    setDetailId(id);
    setDetailText(null);
    setDetailError(null);
    const row = rows.find((r) => r.id === id);
    if (row && (row.status === "ready" || row.status === "ready_degraded")) {
      getDocumentText(id)
        .then((t) => { if (current()) setDetailText(t); })
        .catch((e) => { if (current()) setDetailError(e instanceof ApiError ? e.detail : String(e)); });
    }
  };

  useEffect(() => {
    if (!detail || !detail.id) return;
    if (detail.status !== "ready" && detail.status !== "ready_degraded") return;
    getDocumentText(detail.id)
      .then((t) => { if (current()) setDetailText(t); })
      .catch((e) => { if (current()) setDetailError(e instanceof ApiError ? e.detail : String(e)); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail?.id, detail?.status, detail?.version]);

  // ?add=1 (the top bar's "+ Add file") and ?manage=<id> deep links.
  useEffect(() => {
    if (searchParams.get("add") === "1") {
      dropZone.current?.scrollIntoView({ block: "center" });
      dropZone.current?.focus();
      router.replace("/knowledge-bases");
      return;
    }
    const id = searchParams.get("manage");
    if (id && rows.some((r) => r.id === id)) {
      // Consume a ?manage deep link once its file has loaded.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      openDetail(id);
      router.replace("/knowledge-bases");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, rows]);

  const upload = async (files: File[]) => {
    if (!uploadTenant || files.length === 0) return;
    setUploading(true);
    setActionError(null);
    const problems: string[] = [];
    try {
      const kb = await ensureDefaultKnowledgeBase(uploadTenant.id);
      for (const file of files) {
        const reason = rejectionReasonFor(file);
        if (reason) {
          problems.push(reason);
          continue;
        }
        try {
          await uploadDocument(kb.id, file, file.name);
        } catch (e) {
          problems.push(`${file.name}: ${e instanceof ApiError ? e.detail : String(e)}`);
        }
      }
    } catch (e) {
      problems.push(e instanceof ApiError ? e.detail : String(e));
    } finally {
      if (current()) {
        setNotices(problems);
        setUploading(false);
        await load(true);
      }
    }
  };

  const run = async (row: DocRow, action: () => Promise<unknown>) => {
    setBusyId(row.id);
    setActionError(null);
    try {
      await action();
      await load(true);
    } catch (e) {
      if (current()) setActionError(e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e));
    } finally {
      if (current()) setBusyId(null);
    }
  };

  const reindex = (row: DocRow) => run(row, async () => {
    await reindexDocument(row.id);
    if (detailId === row.id) setDetailText(null);
  });

  const pickReplacement = (row: DocRow) => {
    replaceTarget.current = row;
    replaceInput.current?.click();
  };

  const replace = async (file: File | undefined) => {
    const row = replaceTarget.current;
    if (replaceInput.current) replaceInput.current.value = "";
    if (!file || !row) return;
    const reason = rejectionReasonFor(file);
    if (reason) {
      setActionError(reason);
      return;
    }
    await run(row, () => replaceKnowledgeFile(row, file, { uploadDocument, listDocuments, updateDocument, deleteDocument }, {
      isCurrent: current,
    }));
    replaceTarget.current = null;
  };

  const saveRename = async () => {
    const row = renameTarget;
    if (!row || !renameValue.trim()) return;
    setRenameTarget(null);
    await run(row, () => updateDocument(row.id, { title: renameValue.trim() }));
  };

  const prepareDelete = async (row: DocRow) => {
    const agents = await listDocumentAgents(row.id).catch(() => null);
    if (current()) setDeleteTarget({ row, agents });
  };

  const confirmDelete = async () => {
    const target = deleteTarget;
    if (!target) return;
    setDeleteTarget(null);
    if (detailId === target.row.id) setDetailId(null);
    await run(target.row, () => deleteDocument(target.row.id));
  };

  const filtered = rows.filter((r) =>
    `${r.title} ${isAllTenants ? r.tenantName : ""}`.toLowerCase().includes(search.trim().toLowerCase()),
  );
  const columns = isAllTenants ? 8 : 7;

  return (
    <div className="knowledge-library">
      <div className="knowledge-heading">
        <div>
          <h1>Knowledge</h1>
          <p>Files your agents answer from. Pick which ones each agent uses on its Knowledge tab.</p>
        </div>
        {usage && (
          <div className="knowledge-usage" aria-label="Knowledge usage">
            <div>
              <span className="knowledge-usage-label">Files</span>
              <span className="knowledge-usage-value">{usage.documents}<small>/{usage.max_documents}</small></span>
            </div>
            <div>
              <span className="knowledge-usage-label">Storage</span>
              <span className="knowledge-usage-value">
                {formatBytes(usage.storage_bytes)}<small>/{formatBytes(usage.max_storage_bytes)}</small>
              </span>
            </div>
            <div>
              <span className="knowledge-usage-label">Max file size</span>
              <span className="knowledge-usage-value">{formatBytes(usage.max_upload_bytes)}</span>
            </div>
          </div>
        )}
      </div>

      {(pageError || accountError) && <div className="error-banner" role="alert">{pageError || accountError}</div>}
      {actionError && <div className="error-banner" role="alert">{actionError}</div>}
      {notices.map((n, i) => <div key={i} className="error-banner" role="alert">{n}</div>)}

      {canManage && (
        uploadTenant ? (
          <div
            ref={dropZone}
            tabIndex={-1}
            className={`knowledge-drop${dragging ? " dragging" : ""}`}
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              upload(Array.from(e.dataTransfer.files));
            }}
          >
            <CloudUpload size={30} aria-hidden="true" />
            <button
              type="button"
              className="knowledge-drop-button"
              disabled={uploading}
              onClick={() => uploadInput.current?.click()}
            >
              {uploading ? "Uploading…" : "Drop files here or click to upload"}
            </button>
            <span className="knowledge-drop-hint">
              {ACCEPTED_DOC_LABEL}
              {usage && ` · up to ${formatBytes(usage.max_upload_bytes)} each · ${usage.documents} of ${usage.max_documents} files used`}
            </span>
            <input
              ref={uploadInput}
              type="file"
              accept={ACCEPTED_DOC_ACCEPT}
              multiple
              hidden
              onChange={(e) => {
                upload(Array.from(e.target.files ?? []));
                e.target.value = "";
              }}
            />
          </div>
        ) : (
          <div className="knowledge-drop-hint knowledge-drop-pick">Pick one account in the top bar to upload files.</div>
        )
      )}

      <div className="card knowledge-card">
        <div className="card-hdr">
          <h2 className="card-title">Documents</h2>
          <span className="card-sub">
            {loading || tenantLoading ? "Loading…" : `${rows.length} ${rows.length === 1 ? "file" : "files"}`}
          </span>
          <label className="knowledge-search">
            <Search size={14} />
            <input aria-label="Search files" placeholder="Search files…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </label>
          <button className="btn btn-ghost btn-sm" disabled={loading} onClick={() => load()}>
            <RefreshCw size={13} className={loading ? "knowledge-refreshing" : undefined} /> Refresh
          </button>
        </div>
        <div className="knowledge-table-wrap">
          <table className="tbl" aria-label="Documents">
            <thead>
              <tr>
                <th scope="col">Name</th>
                {isAllTenants && <th scope="col">Account</th>}
                <th scope="col">Type</th>
                <th scope="col">Size</th>
                <th scope="col">Tokens</th>
                <th scope="col">Status</th>
                <th scope="col">Uploaded</th>
                <th scope="col"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {loading || tenantLoading ? (
                <tr><td colSpan={columns} className="empty-state">Loading files…</td></tr>
              ) : rows.length === 0 ? (
                <tr>
                  <td colSpan={columns}>
                    <div className="empty-state knowledge-empty">
                      <FileText size={28} />
                      <h3>No documents yet</h3>
                    </div>
                  </td>
                </tr>
              ) : filtered.length === 0 ? (
                <tr><td colSpan={columns} className="empty-state">No files match your search.</td></tr>
              ) : (
                filtered.map((r) => {
                  const busy = busyId === r.id;
                  return (
                    <tr key={r.id}>
                      <td data-label="Name">
                        <button type="button" className="knowledge-row-open" onClick={() => openDetail(r.id)}>
                          <FileText size={15} aria-hidden="true" />
                          <span>{r.title}</span>
                        </button>
                      </td>
                      {isAllTenants && <td data-label="Account" className="kb-source-sub">{r.tenantName}</td>}
                      <td data-label="Type">{sourceType(r)}</td>
                      <td data-label="Size" className="tabular">{formatBytes(r.byte_size)}</td>
                      <td data-label="Tokens" className="tabular">{preparing(r) || r.token_count === undefined ? "—" : r.token_count.toLocaleString()}</td>
                      <td data-label="Status">
                        <span className={`badge ${FILE_STATUS[r.status].color}`}>{FILE_STATUS[r.status].label}</span>
                      </td>
                      <td data-label="Uploaded" className="kb-source-sub">{formatDate(r.created_at)}</td>
                      <td data-label="Actions">
                        {canManage && (
                          <div className="knowledge-row-actions">
                            <button type="button" className="btn btn-ghost btn-sm btn-icon" title="Reindex" aria-label={`Reindex ${r.title}`}
                              disabled={busy || preparing(r)} onClick={() => reindex(r)}>
                              <RefreshCw size={14} className={busy ? "knowledge-refreshing" : undefined} />
                            </button>
                            <button type="button" className="btn btn-ghost btn-sm btn-icon" title="Replace file" aria-label={`Replace ${r.title}`}
                              disabled={busy || preparing(r)} onClick={() => pickReplacement(r)}>
                              <FileUp size={14} />
                            </button>
                            <button type="button" className="btn btn-ghost btn-sm btn-icon" title="Rename" aria-label={`Rename ${r.title}`}
                              disabled={busy} onClick={() => { setRenameTarget(r); setRenameValue(r.title); }}>
                              <Pencil size={14} />
                            </button>
                            <button type="button" className="btn btn-ghost btn-sm btn-icon knowledge-danger-icon" title="Delete" aria-label={`Delete ${r.title}`}
                              disabled={busy} onClick={() => prepareDelete(r)}>
                              <Trash2 size={14} />
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="knowledge-library-guide">
        <span className="knowledge-library-guide-icon"><BookOpen size={18} /></span>
        <div>
          <strong>Put your knowledge to work</strong>
          <p>Open an agent → Knowledge → add the documents it should answer from.</p>
        </div>
        <Link href="/agents">Go to agents <ArrowUpRight size={14} /></Link>
      </div>

      <input ref={replaceInput} type="file" accept={ACCEPTED_DOC_ACCEPT} hidden onChange={(e) => replace(e.target.files?.[0])} />

      <Modal
        open={!!detail}
        title={detail?.title ?? ""}
        onClose={() => setDetailId(null)}
        trapFocus
        className="knowledge-detail"
        footer={detail && canManage ? (
          <button
            className="btn btn-ghost btn-sm"
            disabled={busyId === detail.id || preparing(detail)}
            onClick={() => reindex(detail)}
          >
            <RefreshCw size={13} /> Reindex
          </button>
        ) : undefined}
      >
        {detail && (
          <>
            <div className="knowledge-detail-meta">
              <span>{sourceType(detail)}</span>
              <span>{formatBytes(detail.byte_size)}</span>
              {!preparing(detail) && <span>{detail.token_count.toLocaleString()} tokens</span>}
              <span className={`badge ${FILE_STATUS[detail.status].color}`}>{FILE_STATUS[detail.status].label}</span>
            </div>
            {preparing(detail) ? (
              <p className="knowledge-manage-preparing">We’re preparing this file. Its text appears here when it’s ready.</p>
            ) : detail.status === "failed" ? (
              <p className="knowledge-file-warning">
                We couldn’t prepare this file{detail.error ? `: ${detail.error}` : "."} Replace it, or reindex to try again.
              </p>
            ) : detailError ? (
              <div className="error-banner" role="alert">{detailError}</div>
            ) : !detailText ? (
              <div className="empty-state">Loading text…</div>
            ) : (
              <>
                <div className="knowledge-detail-label">What the agent reads</div>
                <pre className="knowledge-text" tabIndex={0}>{detailText.text || "This file has no readable text."}</pre>
                {detailText.truncated && <p className="kb-source-sub">Showing the first part of a long file.</p>}
              </>
            )}
          </>
        )}
      </Modal>

      <Modal
        open={!!renameTarget}
        title="Rename file"
        onClose={() => setRenameTarget(null)}
        trapFocus
        className="knowledge-dialog"
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setRenameTarget(null)}>Cancel</button>
            <button className="btn btn-primary btn-sm" disabled={!renameValue.trim()} onClick={saveRename}>Save</button>
          </>
        }
      >
        <label className="form-label" htmlFor="knowledge-rename">Display name</label>
        <input
          id="knowledge-rename"
          className="form-input"
          value={renameValue}
          onChange={(e) => setRenameValue(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") saveRename(); }}
        />
      </Modal>

      <Modal
        open={!!deleteTarget}
        title={`Delete "${deleteTarget?.row.title}"?`}
        onClose={() => setDeleteTarget(null)}
        trapFocus
        className="knowledge-dialog"
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setDeleteTarget(null)}>Cancel</button>
            <button className="btn btn-danger btn-sm" onClick={confirmDelete}>Delete file</button>
          </>
        }
      >
        {deleteTarget && (
          <div className="knowledge-delete-body">
            {deleteTarget.agents === null ? (
              <p>Couldn’t check which agents use this file. Any agent using it will lose it.</p>
            ) : deleteTarget.agents.length === 0 ? (
              <p>No agent uses this file.</p>
            ) : (
              <>
                <p>
                  {deleteTarget.agents.length === 1 ? "This agent" : `These ${deleteTarget.agents.length} agents`} will
                  stop answering from it:
                </p>
                <ul>{deleteTarget.agents.map((a) => <li key={a.agent_id}>{a.agent_name}</li>)}</ul>
              </>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}
