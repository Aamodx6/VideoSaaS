import { env } from "~/env";

export const neonAIConfig = {
  baseUrl: env.NEON_AI_GATEWAY_BASE_URL,
  token: env.NEON_AI_GATEWAY_TOKEN,
};

export async function getNeonAIModels() {
  if (!env.NEON_AI_GATEWAY_BASE_URL || !env.NEON_AI_GATEWAY_TOKEN) {
    return { status: "unconfigured", error: "Neon AI Gateway env vars missing" };
  }

  try {
    const res = await fetch(`${env.NEON_AI_GATEWAY_BASE_URL}/v1/models`, {
      headers: {
        Authorization: `Bearer ${env.NEON_AI_GATEWAY_TOKEN}`,
      },
      cache: "no-store",
    });

    if (!res.ok) {
      return { status: "error", code: res.status, error: res.statusText };
    }

    const json = (await res.json()) as { data?: Array<{ id: string; name?: string }> };
    return {
      status: "ok",
      modelCount: json.data?.length ?? 0,
      sampleModels: json.data?.slice(0, 5).map((m) => m.id) ?? [],
    };
  } catch (err) {
    return { status: "error", error: err instanceof Error ? err.message : String(err) };
  }
}
