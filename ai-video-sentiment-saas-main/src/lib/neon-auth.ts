import { env } from "~/env";

export const neonAuthConfig = {
  baseUrl: env.NEON_AUTH_BASE_URL,
  jwksUrl: env.NEON_AUTH_JWKS_URL,
};

export async function checkNeonAuthStatus() {
  if (!env.NEON_AUTH_JWKS_URL) {
    return { status: "unconfigured", error: "NEON_AUTH_JWKS_URL is missing" };
  }

  try {
    const res = await fetch(env.NEON_AUTH_JWKS_URL, {
      method: "GET",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });

    if (!res.ok) {
      return { status: "error", code: res.status, error: res.statusText };
    }

    const data = (await res.json()) as { keys?: unknown[] };
    return { status: "ok", jwksKeysCount: Array.isArray(data?.keys) ? data.keys.length : 0 };
  } catch (err) {
    return { status: "error", error: err instanceof Error ? err.message : String(err) };
  }
}
