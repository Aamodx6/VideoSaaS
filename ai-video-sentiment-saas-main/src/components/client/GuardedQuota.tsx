"use client";

import { useEffect, useState } from "react";
import { z } from "zod";

const quota = z.object({
  period: z.string(), requests_used: z.number().int().nonnegative(),
  max_requests: z.number().int().positive(),
});

export function GuardedQuota({ apiKey }: { apiKey: string }) {
  const [usage, setUsage] = useState<z.infer<typeof quota>>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    async function refresh() {
      try {
        const response = await fetch("/api/async-inference/quota", {
          headers: { Authorization: "Bearer " + apiKey }, signal: controller.signal, cache: "no-store",
        });
        if (!response.ok) throw new Error("Quota unavailable");
        setUsage(quota.parse(await response.json()));
        setFailed(false);
      } catch {
        if (!controller.signal.aborted) setFailed(true);
      }
    }
    void refresh();
    const update = () => { void refresh(); };
    window.addEventListener("nuance-quota-change", update);
    return () => {
      controller.abort();
      window.removeEventListener("nuance-quota-change", update);
    };
  }, [apiKey]);
  return (
    <div className="mt-3 rounded-xl bg-gray-100 p-4 text-sm" role="status">
      <span>Monthly quota</span>
      <p className="text-gray-500">
        {failed ? "Usage is temporarily unavailable." : usage
          ? `${usage.requests_used} / ${usage.max_requests} requests (${usage.period} UTC)`
          : "Loading usage…"}
      </p>
    </div>
  );
}
