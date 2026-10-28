import { ApiError, createTestSession, getAgent } from "@/lib/api";

// The webcall bridge refuses any session whose first frame isn't a credential minted for this agent.
export async function openTestCallSocket(baseUrl: string, tenantSlug: string, agentSlug: string): Promise<WebSocket> {
  let credential: string;
  try {
    const agent = await getAgent(tenantSlug, agentSlug);
    ({ credential } = await createTestSession(tenantSlug, agent.id));
  } catch (e) {
    if (e instanceof ApiError && e.status === 403) throw new Error("Only admins can start a test call for this agent.");
    if (e instanceof ApiError && e.status === 429) throw new Error("Too many test calls just now. Wait a minute and try again.");
    throw e;
  }
  const ws = new WebSocket(
    `${baseUrl}/webcall?tenant=${encodeURIComponent(tenantSlug)}&agent=${encodeURIComponent(agentSlug)}`,
  );
  ws.addEventListener("open", () => ws.send(JSON.stringify({ type: "test_credential", credential })), { once: true });
  return ws;
}
