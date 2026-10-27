import { ApiError, createProvider, listProviders } from "./api";
import { createKnowledgeBase, KnowledgeBase, listKnowledgeBases } from "./knowledgeApi";

export const ACCEPTED_DOC_EXTENSIONS = [
  ".txt",
  ".md",
  ".pdf",
  ".docx",
  ".pptx",
  ".xlsx",
  ".html",
  ".htm",
  ".csv",
  ".tsv",
];
export const ACCEPTED_DOC_ACCEPT = ACCEPTED_DOC_EXTENSIONS.join(",");
export const ACCEPTED_DOC_LABEL = "TXT, MD, PDF, DOCX, PPTX, XLSX, HTML, CSV, TSV";

// A cheap first filter only (drag-and-drop bypasses <input accept>): the server detects the real type from the
// file's bytes, so the browser-declared MIME type is not checked here.
export function rejectionReasonFor(file: File): string | null {
  const name = file.name.toLowerCase();
  const hasAcceptedExtension = ACCEPTED_DOC_EXTENSIONS.some((ext) =>
    name.endsWith(ext),
  );
  if (!hasAcceptedExtension) {
    return `"${file.name}" isn't a supported file type (text, Markdown, HTML or CSV; PDF, Word, Excel and PowerPoint only if your administrator has turned on document parsing).`;
  }
  return null;
}

const DEFAULT_SLUG = "default";

/** The tenant's one folder for files added on the Knowledge page. Ingestion needs an embedding provider on the
 *  folder, so it reuses the tenant's (embeddings always run on the local Ollama) or creates one. */
export async function ensureDefaultKnowledgeBase(tenantId: string): Promise<KnowledgeBase> {
  const find = async () => (await listKnowledgeBases(tenantId)).find((kb) => kb.slug === DEFAULT_SLUG);
  const existing = await find();
  if (existing) return existing;
  const embedding =
    (await listProviders(tenantId, { role: "embedding" }))[0] ??
    (await createProvider(tenantId, {
      name: "Ollama nomic-embed-text", role: "embedding", engine: "ollama", model: "nomic-embed-text",
    }));
  try {
    return await createKnowledgeBase(tenantId, { slug: DEFAULT_SLUG, name: "Documents", embedding_config_id: embedding.id });
  } catch (e) {
    // Two first uploads at once: the slug is unique per tenant, so the loser reads the winner's folder.
    if (e instanceof ApiError) {
      const raced = await find();
      if (raced) return raced;
    }
    throw e;
  }
}
