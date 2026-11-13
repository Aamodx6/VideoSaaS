import { createEnv } from "@t3-oss/env-nextjs";
import { z } from "zod";

export const env = createEnv({
  /**
   * Specify your server-side environment variables schema here. This way you can ensure the app
   * isn't built with invalid env vars.
   */
  server: {
    AUTH_SECRET:
      process.env.NODE_ENV === "production"
        ? z.string()
        : z.string().optional(),
    DATABASE_URL: z.string().url(),
    NODE_ENV: z
      .enum(["development", "test", "production"])
      .default("development"),
    AWS_REGION: z.string(),
    AWS_INFERENCE_BUCKET: z.string(),
    AWS_INFERENCE_INPUT_PREFIX: z.string().regex(/^(?:[A-Za-z0-9_-]+\/)+$/).default("inference/uploads/"),
    AWS_ENDPOINT_NAME: z.string(),
    MODEL_VERSION: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/).optional(),
    TELEMETRY_HMAC_SECRET: z.string().min(32).optional(),
    FEEDBACK_SIGNING_SECRET: z.string().min(32).optional(),
    AWS_FEEDBACK_TABLE: z.string().optional(),
    FEEDBACK_RETENTION_DAYS: z.coerce.number().int().min(14).max(365).default(30),
    DATABASE_URL_UNPOOLED: z.string().optional(),
    NEON_AUTH_BASE_URL: z.string().optional(),
    NEON_AUTH_JWKS_URL: z.string().optional(),
    NEON_REALTIME_URL: z.string().optional(),
    NEON_REALTIME_SECRET: z.string().optional(),
    NEON_AI_GATEWAY_TOKEN: z.string().optional(),
    NEON_AI_GATEWAY_BASE_URL: z.string().optional(),
  },

  /**
   * Specify your client-side environment variables schema here. This way you can ensure the app
   * isn't built with invalid env vars. To expose them to the client, prefix them with
   * `NEXT_PUBLIC_`.
   */
  client: {
    // NEXT_PUBLIC_CLIENTVAR: z.string(),
  },

  /**
   * You can't destruct `process.env` as a regular object in the Next.js edge runtimes (e.g.
   * middlewares) or client-side so we need to destruct manually.
   */
  runtimeEnv: {
    AUTH_SECRET: process.env.AUTH_SECRET,
    DATABASE_URL: process.env.DATABASE_URL,
    NODE_ENV: process.env.NODE_ENV,
    AWS_REGION: process.env.AWS_REGION,
    AWS_INFERENCE_BUCKET: process.env.AWS_INFERENCE_BUCKET,
    AWS_INFERENCE_INPUT_PREFIX: process.env.AWS_INFERENCE_INPUT_PREFIX,
    AWS_ENDPOINT_NAME: process.env.AWS_ENDPOINT_NAME,
    MODEL_VERSION: process.env.MODEL_VERSION,
    TELEMETRY_HMAC_SECRET: process.env.TELEMETRY_HMAC_SECRET,
    FEEDBACK_SIGNING_SECRET: process.env.FEEDBACK_SIGNING_SECRET,
    AWS_FEEDBACK_TABLE: process.env.AWS_FEEDBACK_TABLE,
    FEEDBACK_RETENTION_DAYS: process.env.FEEDBACK_RETENTION_DAYS,
    DATABASE_URL_UNPOOLED: process.env.DATABASE_URL_UNPOOLED,
    NEON_AUTH_BASE_URL: process.env.NEON_AUTH_BASE_URL,
    NEON_AUTH_JWKS_URL: process.env.NEON_AUTH_JWKS_URL,
    NEON_REALTIME_URL: process.env.NEON_REALTIME_URL,
    NEON_REALTIME_SECRET: process.env.NEON_REALTIME_SECRET,
    NEON_AI_GATEWAY_TOKEN: process.env.NEON_AI_GATEWAY_TOKEN,
    NEON_AI_GATEWAY_BASE_URL: process.env.NEON_AI_GATEWAY_BASE_URL,
  },
  /**
   * Run `build` or `dev` with `SKIP_ENV_VALIDATION` to skip env validation. This is especially
   * useful for Docker builds.
   */
  skipValidation: !!process.env.SKIP_ENV_VALIDATION,
  /**
   * Makes it so that empty strings are treated as undefined. `SOME_VAR: z.string()` and
   * `SOME_VAR=''` will throw an error.
   */
  emptyStringAsUndefined: true,
});
