import { NextResponse } from "next/server";
import { db } from "~/server/db";
import { checkNeonAuthStatus } from "~/lib/neon-auth";
import { getNeonAIModels } from "~/lib/neon-ai";
import { checkNeonRealtimeConfig } from "~/lib/neon-realtime";
import { timingSafeEqual } from "node:crypto";

export async function GET(request: Request) {
  const expected = process.env.DIAGNOSTICS_TOKEN;
  const supplied = request.headers.get("authorization")?.replace(/^Bearer /, "");
  if (!expected || expected.length < 32 || !supplied || Buffer.byteLength(expected) !== Buffer.byteLength(supplied) ||
      !timingSafeEqual(Buffer.from(expected), Buffer.from(supplied))) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }
  const results: Record<string, unknown> = {};

  // 1. Test Neon Postgres
  try {
    const userCount = await db.user.count();
    results.postgres = {
      status: "ok",
      driver: "@neondatabase/serverless + @prisma/adapter-neon",
      userCount,
      connected: true,
    };
  } catch (err) {
    results.postgres = {
      status: "error",
      error: err instanceof Error ? err.message : String(err),
    };
  }

  // 2. Test Neon Auth
  results.auth = await checkNeonAuthStatus();

  // 3. Test Neon AI Gateway
  results.aiGateway = await getNeonAIModels();

  // 4. Test Neon Realtime
  results.realtime = checkNeonRealtimeConfig();

  return NextResponse.json({
    timestamp: new Date().toISOString(),
    services: results,
  });
}
