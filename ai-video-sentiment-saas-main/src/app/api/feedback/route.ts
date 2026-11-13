import { DynamoDBClient, TransactWriteItemsCommand } from "@aws-sdk/client-dynamodb";
import { NextResponse } from "next/server";
import { z } from "zod";
import { env } from "~/env";
import { emotion, sentiment, verifyFeedbackToken } from "~/lib/inference-observability";
import { db } from "~/server/db";

const feedback = z.object({
  token: z.string().max(2048),
  correct_emotion: emotion,
  correct_sentiment: sentiment,
}).strict();

export async function POST(request: Request) {
  const apiKey = /^Bearer ([^\s]+)$/.exec(request.headers.get("Authorization") ?? "")?.[1];
  if (!apiKey) return NextResponse.json({ error: "API key required" }, { status: 401 });
  if (!env.AWS_FEEDBACK_TABLE || !env.FEEDBACK_SIGNING_SECRET) {
    return NextResponse.json({ error: "Feedback is unavailable" }, { status: 503 });
  }
  const body = feedback.safeParse(await request.json().catch(() => null));
  if (!body.success) return NextResponse.json({ error: "Invalid feedback" }, { status: 400 });
  try {
    const quota = await db.apiQuota.findUnique({ where: { secretKey: apiKey }, select: { userId: true } });
    if (!quota) return NextResponse.json({ error: "Invalid API key" }, { status: 401 });
    const prediction = verifyFeedbackToken(body.data.token, quota.userId);
    if (!prediction) return NextResponse.json({ error: "Invalid or expired prediction token" }, { status: 403 });
    const client = new DynamoDBClient({ region: env.AWS_REGION });
    const now = Date.now();
    const hour = new Date(now).toISOString().slice(0, 13);
    const expires = String(Math.floor(now / 1000) + env.FEEDBACK_RETENTION_DAYS * 86400);
    await client.send(new TransactWriteItemsCommand({ TransactItems: [{ Put: {
      TableName: env.AWS_FEEDBACK_TABLE,
      ConditionExpression: "attribute_not_exists(id)",
      Item: {
        id: { S: `feedback#${prediction.id}` }, endpoint: { S: prediction.endpoint },
        model_version: { S: prediction.version },
        time: { N: String(now) },
        expires: { N: expires },
        predicted_emotion: { S: prediction.emotion }, predicted_sentiment: { S: prediction.sentiment },
        correct_emotion: { S: body.data.correct_emotion }, correct_sentiment: { S: body.data.correct_sentiment },
      },
    } }, { Update: {
      TableName: env.AWS_FEEDBACK_TABLE,
      Key: { id: { S: `acc#${prediction.version}#${prediction.endpoint}#${hour}#${prediction.id[0]}` } },
      UpdateExpression: "SET expires = :expires ADD feedback_count :one, emotion_correct :emotion, sentiment_correct :sentiment",
      ExpressionAttributeValues: {
        ":expires": { N: expires }, ":one": { N: "1" },
        ":emotion": { N: prediction.emotion === body.data.correct_emotion ? "1" : "0" },
        ":sentiment": { N: prediction.sentiment === body.data.correct_sentiment ? "1" : "0" },
      },
    } }] }));
    return new NextResponse(null, { status: 204 });
  } catch (error) {
    if (error instanceof Error && error.name === "TransactionCanceledException" &&
        "CancellationReasons" in error &&
        (error.CancellationReasons as Array<{ Code?: string }> | undefined)?.[0]?.Code === "ConditionalCheckFailed") {
      return NextResponse.json({ error: "Feedback already recorded" }, { status: 409 });
    }
    console.error("Feedback recording failed");
    return NextResponse.json({ error: "Could not record feedback" }, { status: 500 });
  }
}
