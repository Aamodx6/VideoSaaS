import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";
import { z } from "zod";
import { env } from "~/env";

export const emotion = z.enum(["anger", "disgust", "fear", "joy", "neutral", "sadness", "surprise"]);
export const sentiment = z.enum(["negative", "neutral", "positive"]);
const receipt = z.object({
  id: z.string().regex(/^[a-f0-9]{32}:\d+$/),
  owner: z.string().regex(/^[a-f0-9]{64}$/),
  version: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/),
  endpoint: z.string(),
  time: z.number().int().positive(),
  emotion,
  sentiment,
});

function digest(secret: string, purpose: string, value: string) {
  return createHmac("sha256", secret).update(purpose).update("\0").update(value).digest("hex");
}

export function correlation(userId: string, tenantId?: string) {
  const secret = env.TELEMETRY_HMAC_SECRET;
  const request_id = randomUUID().replaceAll("-", "");
  return { request_id, ...(secret && {
    user_id: digest(secret, "user", userId),
    // The current schema has no organizations. A user is one tenant.
    tenant_id: digest(secret, "tenant", tenantId ?? userId),
  }) };
}

export function feedbackToken(
  userId: string,
  data: Omit<z.infer<typeof receipt>, "owner">,
) {
  const secret = env.FEEDBACK_SIGNING_SECRET;
  if (!secret || !env.AWS_FEEDBACK_TABLE) return undefined;
  const payload = Buffer.from(JSON.stringify(receipt.parse({
    ...data, owner: digest(secret, "feedback-owner", userId),
  }))).toString("base64url");
  return `${payload}.${digest(secret, "feedback-receipt", payload)}`;
}

export function verifyFeedbackToken(token: string, userId: string) {
  const secret = env.FEEDBACK_SIGNING_SECRET;
  const [payload, signature, extra] = token.split(".");
  if (!secret || !payload || !signature || extra !== undefined || !/^[a-f0-9]{64}$/.test(signature)) return;
  const expected = digest(secret, "feedback-receipt", payload);
  if (!timingSafeEqual(Buffer.from(signature, "hex"), Buffer.from(expected, "hex"))) return;
  try {
    const parsed = receipt.parse(JSON.parse(Buffer.from(payload, "base64url").toString("utf8")));
    const age = Date.now() - parsed.time;
    if (parsed.owner === digest(secret, "feedback-owner", userId) &&
        parsed.endpoint === env.AWS_ENDPOINT_NAME && age >= 0 && age < 7 * 86_400_000) return parsed;
  } catch {
    return;
  }
}

export function logSageMakerThrottle() {
  if (!env.MODEL_VERSION) return;
  console.log(JSON.stringify({
    endpoint: env.AWS_ENDPOINT_NAME, model_version: env.MODEL_VERSION, ThrottledRequests: 1,
    _aws: { Timestamp: Date.now(), CloudWatchMetrics: [{
      Namespace: "VideoSentiment/SaaS", Dimensions: [["endpoint", "model_version"]],
      Metrics: [{ Name: "ThrottledRequests", Unit: "Count" }],
    }] },
  }));
}
