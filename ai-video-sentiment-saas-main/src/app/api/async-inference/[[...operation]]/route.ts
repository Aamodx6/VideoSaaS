import { defaultProvider } from "@aws-sdk/credential-provider-node";
import { Hash } from "@smithy/hash-node";
import { SignatureV4 } from "@smithy/signature-v4";
import { NextResponse } from "next/server";

type Context = { params: Promise<{ operation?: string[] }> };

async function proxy(request: Request, context: Context) {
  const root = process.env.AWS_JOBS_API_URL;
  if (!root) {
    return NextResponse.json({ error: "Async inference is not configured" }, { status: 503 });
  }
  const { operation = [] } = await context.params;
  const [name, id] = operation;
  const submit = request.method === "POST" && operation.length === 1 &&
    (name === "uploads" || name === "jobs");
  const status = request.method === "GET" && operation.length === 2 &&
    name === "jobs" && !!id && /^[a-f0-9]{32}$/.test(id);
  if (!submit && !status) {
    return NextResponse.json({ error: "Unknown inference operation" }, { status: 404 });
  }
  const token = request.headers.get("authorization")?.match(/^Bearer ([A-Za-z0-9_-]{32,128})$/i)?.[1];
  if (!token) {
    return NextResponse.json({ error: "API key required" }, { status: 401 });
  }
  const url = new URL(operation.join("/"), root.replace(/\/?$/, "/"));
  const body = submit ? await request.text() : undefined;
  if (body && body.length > 4096) {
    return NextResponse.json({ error: "Request too large" }, { status: 413 });
  }
  const signer = new SignatureV4({
    credentials: defaultProvider(),
    region: process.env.AWS_REGION ?? "ap-southeast-2",
    service: "execute-api",
    sha256: Hash.bind(null, "sha256"),
  });
  try {
    const signed = await signer.sign({
      method: request.method,
      protocol: url.protocol,
      hostname: url.hostname,
      path: url.pathname,
      headers: { host: url.host, "content-type": "application/json", "x-aspect-api-key": token },
      body,
    });
    const response = await fetch(url, {
      method: request.method, headers: signed.headers, body, cache: "no-store",
      signal: AbortSignal.timeout(20_000),
    });
    return new Response(response.body, {
      status: response.status,
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
  } catch {
    return NextResponse.json({ error: "Async inference service unavailable" }, { status: 503 });
  }
}

export const GET = proxy;
export const POST = proxy;
