import { ApiError, createTestSession, getAgent } from "@/lib/api";
import { isChatAgent } from "@/lib/agentReadiness";

// Minted before the mic opens, so a refusal never leaves the mic captured.
export async function mintTestCredential(tenantSlug: string, agentSlug: string): Promise<string> {
  try {
    const agent = await getAgent(tenantSlug, agentSlug);
    if (isChatAgent(agent)) throw new Error("Voice test calls are not available for chat agents.");
    return (await createTestSession(tenantSlug, agent.id)).credential;
  } catch (e) {
    if (e instanceof ApiError && e.status === 403) throw new Error("Only admins can start a test call for this agent.");
    if (e instanceof ApiError && e.status === 429) throw new Error("Too many test calls just now. Wait a minute and try again.");
    throw e;
  }
}

// The webcall bridge refuses any session whose first frame isn't a credential minted for this agent.
export function openTestCallSocket(baseUrl: string, tenantSlug: string, agentSlug: string, credential: string): WebSocket {
  const ws = new WebSocket(
    `${baseUrl}/webcall?tenant=${encodeURIComponent(tenantSlug)}&agent=${encodeURIComponent(agentSlug)}`,
  );
  ws.addEventListener("open", () => ws.send(JSON.stringify({ type: "test_credential", credential })), { once: true });
  return ws;
}
