import { createHash } from "node:crypto";
import { DynamoDBClient, GetItemCommand, PutItemCommand } from "@aws-sdk/client-dynamodb";
import { db } from "~/server/db";

export const moderationRequired = () => process.env.REQUIRE_MODERATION !== "0";

// The hosted app currently has one tenant per user. Only opaque identifiers
// cross into the moderation and model services.
export async function registerGuardedKey(token: string): Promise<boolean> {
  const quota = await db.apiQuota.findUnique({
    where: { secretKey: token },
    select: { userId: true, maxRequests: true },
  });
  if (!quota) return false;
  const table = process.env.AWS_API_KEYS_TABLE;
  if (!table) throw new Error("Guarded keys are not configured");
  const keyHash = createHash("sha256").update(token).digest("hex");
  const subject = createHash("sha256").update(quota.userId).digest("hex").slice(0, 32);
  const client = new DynamoDBClient({ region: process.env.AWS_REGION ?? "ap-southeast-2" });
  const current = await client.send(new GetItemCommand({
    TableName: table, Key: { key_hash: { S: keyHash } }, ConsistentRead: true,
  }));
  if (current.Item) {
    return current.Item.user_id?.S === subject && current.Item.tenant_id?.S === subject &&
      current.Item.revoked?.BOOL !== true && Number(current.Item.expires_at?.N ?? 0) > Date.now() / 1000;
  }
  try {
    await client.send(new PutItemCommand({
      TableName: table, ConditionExpression: "attribute_not_exists(key_hash)",
      Item: {
        key_hash: { S: keyHash }, user_id: { S: subject }, tenant_id: { S: subject },
        monthly_limit: { N: String(quota.maxRequests) }, rate_per_minute: { N: "60" },
        expires_at: { N: String(Math.floor(Date.now() / 1000) + 90 * 86400) },
      },
    }));
  } catch (error) {
    if (!(error instanceof Error) || error.name !== "ConditionalCheckFailedException") throw error;
    // A concurrent request created this exact digest for the same user.
    return registerGuardedKey(token);
  }
  return true;
}
