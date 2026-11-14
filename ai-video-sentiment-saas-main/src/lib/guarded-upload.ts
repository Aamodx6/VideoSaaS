import { z } from "zod";

const analysis = z.object({
  model_version: z.string().optional(),
  status: z.enum(["low_confidence", "out_of_domain"]).optional(),
  reason_code: z.string().optional(),
  utterances: z.array(z.object({
    start_time: z.number(), end_time: z.number(), text: z.string(),
    emotions: z.array(z.object({ label: z.string(), confidence: z.number() })),
    sentiments: z.array(z.object({ label: z.string(), confidence: z.number() })),
  })),
});

async function read(response: Response): Promise<unknown> {
  const payload: unknown = await response.json();
  if (!response.ok) {
    const error = z.object({ error: z.string().optional(), code: z.string().optional() }).parse(payload);
    throw new Error(error.code ?? error.error ?? "Video analysis failed");
  }
  return payload;
}

export async function guardedUpload(file: File, key: string, signal: AbortSignal) {
  if (!/\.mp4$/i.test(file.name)) throw new Error("Guarded uploads require an MP4 file.");
  if (file.size > 128 * 1024 * 1024) throw new Error("The video exceeds 128 MiB.");
  const headers = { Authorization: "Bearer " + key, "Content-Type": "application/json" };
  const signed = z.object({ url: z.string().url(), fields: z.record(z.string()), upload_id: z.string() }).parse(
    await read(await fetch("/api/async-inference/uploads", {
      method: "POST", headers, body: JSON.stringify({}), signal,
    })));
  const form = new FormData();
  for (const [name, value] of Object.entries(signed.fields)) form.append(name, value);
  form.append("file", file);
  const upload = await fetch(signed.url, { method: "POST", body: form, signal });
  if (!upload.ok) throw new Error("The video upload failed.");
  const submitted = z.object({ job_id: z.string() }).parse(await read(
    await fetch("/api/async-inference/jobs", {
      method: "POST", headers, body: JSON.stringify({ upload_id: signed.upload_id }), signal,
    })));
  window.dispatchEvent(new Event("nuance-quota-change"));
  for (let attempt = 0; attempt < 480; attempt++) {
    if (signal.aborted) throw new Error("Upload cancelled.");
    const result = z.object({
      status: z.string(), analysis: analysis.optional(),
      code: z.string().optional(),
      account_policy: z.enum(["active", "warn", "throttle", "suspend"]).optional(),
    }).parse(await read(await fetch("/api/async-inference/jobs/" + submitted.job_id, {
      headers, signal, cache: "no-store",
    })));
    if (result.status === "SUCCEEDED" && result.analysis) {
      return { analysis: result.analysis, account_policy: result.account_policy };
    }
    if (result.status === "REVIEW") {
      throw new Error("Your video needs human review. Reference " + submitted.job_id);
    }
    if (result.status === "FAILED" || result.status === "BLOCKED") {
      throw new Error(result.code ?? "Analysis could not complete.");
    }
    await new Promise<void>((resolve) => setTimeout(resolve, 3000));
  }
  throw new Error("Analysis is still queued. Reference " + submitted.job_id);
}
