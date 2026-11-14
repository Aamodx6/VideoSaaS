import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import ts from "typescript";
import { test } from "node:test";

const require = createRequire(import.meta.url);
const directory = path.dirname(fileURLToPath(import.meta.url));

function load(relative, overrides = {}, environment = {}, extra = {}) {
  const filename = path.resolve(directory, "..", relative);
  const compiled = ts.transpileModule(readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  const realRequire = createRequire(filename);
  vm.runInNewContext(compiled, {
    exports,
    require: (name) => overrides[name] ?? realRequire(name),
    process: { env: environment },
    Request, Response, URL, Buffer, FormData, AbortSignal,
    Event, window: { dispatchEvent() {} },
    fetch: () => { throw new Error("Unexpected external request"); },
    ...extra,
  }, { filename });
  return exports;
}

const token = "a".repeat(32);
const user = "test-user";
const subject = createHash("sha256").update(user).digest("hex").slice(0, 32);

function registry(row) {
  const requests = [];
  const sdk = {
    GetItemCommand: class { constructor(input) { this.input = input; this.kind = "get"; } },
    PutItemCommand: class { constructor(input) { this.input = input; this.kind = "put"; } },
    DynamoDBClient: class {
      async send(command) {
        requests.push(command);
        return command.kind === "get" ? { Item: row } : {};
      }
    },
  };
  const code = load("src/lib/guardrails.ts", {
    "@aws-sdk/client-dynamodb": sdk,
    "~/server/db": { db: { apiQuota: { findUnique: async () => ({ userId: user, maxRequests: 100 }) } } },
  }, { AWS_API_KEYS_TABLE: "keys" });
  return { code, requests };
}

test("guarded mode is the default and only explicit zero enables legacy mode", () => {
  assert.equal(registry().code.moderationRequired(), true);
  const code = load("src/lib/guardrails.ts", { "~/server/db": {} }, { REQUIRE_MODERATION: "0" });
  assert.equal(code.moderationRequired(), false);
});

test("registration stores a digest and opaque subject without plaintext token", async () => {
  const { code, requests } = registry();
  assert.equal(await code.registerGuardedKey(token), true);
  const item = requests[1].input.Item;
  assert.equal(item.key_hash.S, createHash("sha256").update(token).digest("hex"));
  assert.equal(item.user_id.S, subject);
  assert.equal(item.monthly_limit.N, "100");
  assert.ok(!JSON.stringify(item).includes(token));
});

test("revoked, expired and foreign key records are not overwritten", async () => {
  for (const update of [
    { revoked: { BOOL: true } }, { expires_at: { N: "0" } }, { user_id: { S: "foreign" } },
  ]) {
    const { code, requests } = registry({
      user_id: { S: subject }, tenant_id: { S: subject }, expires_at: { N: "9999999999" }, ...update,
    });
    assert.equal(await code.registerGuardedKey(token), false);
    assert.equal(requests.length, 1);
  }
});

test("legacy upload and inference routes reject before database or AWS access", async () => {
  for (const route of ["upload-url", "sentiment-inference"]) {
    const code = load("src/app/api/" + route + "/route.ts", {
      "next/server": { NextResponse: { json: Response.json } },
      "~/env": {}, "~/server/db": {},
      "~/lib/guardrails": { moderationRequired: () => true },
      "~/lib/inference-observability": require("zod").z.object({}).shape,
      "~/lib/quota": {},
    });
    const response = await code.POST(new Request("https://example.invalid", { method: "POST" }));
    assert.equal(response.status, 403);
    assert.equal((await response.json()).code, "MODERATED_UPLOAD_REQUIRED");
  }
});

test("guarded browser upload signs, sends form, submits, and returns an abstention", async () => {
  const requests = [];
  const responses = [
    Response.json({ url: "https://upload.invalid", fields: { key: "object" }, upload_id: "upload" }),
    new Response(null, { status: 204 }),
    Response.json({ job_id: "job" }),
    Response.json({ status: "SUCCEEDED", analysis: {
      status: "out_of_domain", reason_code: "audio_muted", utterances: [],
    } }),
  ];
  const code = load("src/lib/guarded-upload.ts", {}, {}, {
    fetch: async (url, options) => { requests.push({ url, options }); return responses.shift(); },
  });
  const result = await code.guardedUpload(new File(["video"], "clip.mp4"), token, new AbortController().signal);
  assert.equal(result.analysis.status, "out_of_domain");
  assert.equal(requests[1].options.body.get("key"), "object");
  assert.equal(requests[2].url, "/api/async-inference/jobs");
  assert.equal(requests[3].url, "/api/async-inference/jobs/job");
});

test("blocked browser job shows the documented API code", async () => {
  const responses = [
    Response.json({ url: "https://upload.invalid", fields: {}, upload_id: "upload" }),
    new Response(null, { status: 204 }), Response.json({ job_id: "job" }),
    Response.json({ code: "CONTENT_POLICY" }, { status: 422 }),
  ];
  const code = load("src/lib/guarded-upload.ts", {}, {}, { fetch: async () => responses.shift() });
  await assert.rejects(
    code.guardedUpload(new File(["video"], "clip.mp4"), token, new AbortController().signal), /CONTENT_POLICY/);
});

test("oversize and unsupported uploads never request a signature", async () => {
  const code = load("src/lib/guarded-upload.ts");
  await assert.rejects(code.guardedUpload({ name: "x.mp4", size: 129 * 1024**2 }, token), /128 MiB/);
  await assert.rejects(code.guardedUpload({ name: "x.mov", size: 5 }, token), /MP4/);
});

test("quota proxy forwards only an authenticated guarded key", async () => {
  let forwarded;
  const code = load("src/app/api/async-inference/[[...operation]]/route.ts", {
    "next/server": { NextResponse: { json: Response.json } },
    "~/lib/guardrails": { registerGuardedKey: async () => true },
    "@aws-sdk/credential-provider-node": { defaultProvider: () => async () => ({}) },
    "@smithy/signature-v4": { SignatureV4: class { async sign(input) { return input; } } },
  }, { AWS_JOBS_API_URL: "https://jobs.invalid/prod/" }, {
    fetch: async (url, options) => {
      forwarded = { url, options };
      return Response.json({ period: "2026-11", requests_used: 7, max_requests: 100 });
    },
  });
  const response = await code.GET(new Request("https://app.invalid/api/async-inference/quota", {
    headers: { Authorization: "Bearer " + token },
  }), { params: Promise.resolve({ operation: ["quota"] }) });
  assert.equal(response.status, 200);
  assert.equal(forwarded.url.href, "https://jobs.invalid/prod/quota");
  assert.equal(forwarded.options.headers["x-aspect-api-key"], token);
});
