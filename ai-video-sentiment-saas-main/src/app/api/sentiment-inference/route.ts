import {
  InvokeEndpointCommand,
  SageMakerRuntimeClient,
} from "@aws-sdk/client-sagemaker-runtime";
import { NextResponse } from "next/server";
import { z } from "zod";
import { env } from "~/env";
import { correlation, emotion, feedbackToken, logSageMakerThrottle, sentiment } from "~/lib/inference-observability";
import { checkAndUpdateQuota } from "~/lib/quota";
import { db } from "~/server/db";

const analysisSchema = z.object({
  model_version: z.string().optional(),
  utterances: z.array(z.object({
    start_time: z.number(), end_time: z.number(), text: z.string(),
    emotions: z.array(z.object({ label: emotion, confidence: z.number() })),
    sentiments: z.array(z.object({ label: sentiment, confidence: z.number() })),
  })),
});

export async function POST(req: Request) {
  try {
    // Get API key from the header
    const apiKey = req.headers.get("Authorization")?.replace("Bearer ", "");
    if (!apiKey) {
      return NextResponse.json({ error: "API key required" }, { status: 401 });
    }

    // Find the user by API key
    const quota = await db.apiQuota.findUnique({
      where: {
        secretKey: apiKey,
      },
      select: {
        userId: true,
      },
    });

    if (!quota) {
      return NextResponse.json({ error: "Invalid API key" }, { status: 401 });
    }

    const { key } = (await req.json()) as { key?: unknown };

    const prefix = env.AWS_INFERENCE_INPUT_PREFIX;
    if (typeof key !== "string" || !key.startsWith(prefix) ||
        !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\.(mp4|mov|avi)$/i.test(key.slice(prefix.length))) {
      return NextResponse.json({ error: "A valid upload key is required" }, { status: 400 });
    }

    const file = await db.videoFile.findUnique({
      where: { key },
      select: { userId: true, analyzed: true },
    });

    if (!file) {
      return NextResponse.json({ error: "File not found" }, { status: 404 });
    }

    if (file.userId !== quota.userId) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 403 });
    }

    if (file.analyzed) {
      return NextResponse.json(
        { error: "File already analyzed" },
        { status: 400 },
      );
    }

    const hasQuota = await checkAndUpdateQuota(quota.userId, true);

    if (!hasQuota) {
      return NextResponse.json(
        { error: "Monthly quota exceeded" },
        { status: 429 },
      );
    }

    // Call sagemaker endpoint
    const sagemakerClient = new SageMakerRuntimeClient({
      region: env.AWS_REGION,
    });
    const metadata = correlation(quota.userId);

    const command = new InvokeEndpointCommand({
      EndpointName: env.AWS_ENDPOINT_NAME,
      ContentType: "application/json",
      Accept: "application/json",
      CustomAttributes: JSON.stringify(metadata),
      Body: JSON.stringify({
        video_path: `s3://${env.AWS_INFERENCE_BUCKET}/${key}`,
      }),
    });

    const response = await sagemakerClient.send(command);
    const analysis = analysisSchema.parse(JSON.parse(new TextDecoder().decode(response.Body)));
    const version = analysis.model_version;
    const time = Date.now();
    const utterances = analysis.utterances.map((item, index) => ({
      ...item,
      feedback_token: version && item.emotions[0] && item.sentiments[0] ? feedbackToken(quota.userId, {
        id: `${metadata.request_id}:${index}`, version, time,
        endpoint: env.AWS_ENDPOINT_NAME, emotion: item.emotions[0].label, sentiment: item.sentiments[0].label,
      }) : undefined,
    }));

    await db.videoFile.update({
      where: { key },
      data: {
        analyzed: true,
      },
    });

    return NextResponse.json({
      analysis: { ...analysis, utterances },
    });
  } catch (error) {
    if (error instanceof Error && (
      error.name === "ThrottlingException" ||
      ("$metadata" in error && (error.$metadata as { httpStatusCode?: number })?.httpStatusCode === 429)
    )) {
      logSageMakerThrottle();
      return NextResponse.json({ error: "Inference capacity is busy" }, { status: 503 });
    }
    console.error("Sentiment inference failed");
    return NextResponse.json(
      { error: "Internal server error" },
      { status: 500 },
    );
  }
}
