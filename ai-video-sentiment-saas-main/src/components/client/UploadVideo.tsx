"use client";

import { useEffect, useRef, useState } from "react";
import { FiUpload } from "react-icons/fi";
import type { Analysis } from "./Inference";
import { guardedUpload } from "~/lib/guarded-upload";

interface UploadVideoProps {
  guarded: boolean;
  apiKey: string;
  onAnalysis: (analysis: Analysis) => void;
}

function UploadVideo({ apiKey, onAnalysis, guarded }: UploadVideoProps) {
  const active = useRef<AbortController>();
  useEffect(() => () => active.current?.abort(), []);
  const [status, setStatus] = useState<"idle" | "uploading" | "analyzing">(
    "idle",
  );
  const [error, setError] = useState<string | null>(null);

  const handleUpload = async (file: File) => {
    try {
      setStatus("uploading");
      setError(null);
      active.current = new AbortController();
      if (guarded) {
        setStatus("analyzing");
        onAnalysis(await guardedUpload(file, apiKey, active.current.signal));
        return;
      }

      const fileType = `.${file.name.split(".").pop()}`;

      // 1. Get upload URL
      const res = await fetch("/api/upload-url", {
        method: "POST",
        headers: {
          Authorization: "Bearer " + apiKey,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ fileType: fileType }),
      });

      if (!res.ok) {
        const error = (await res.json()) as { error?: string };
        throw new Error(error?.error ?? "Failed to get upload URL");
      }

      const { url, contentType, key } = (await res.json()) as {
        url: string;
        contentType: string;
        key: string;
      };

      // 2. Upload file to S3
      const uploadRes = await fetch(url, {
        method: "PUT",
        headers: { "Content-Type": contentType },
        body: file,
      });

      if (!uploadRes.ok) {
        throw new Error("Failed to upload file");
      }

      setStatus("analyzing");

      // 3. Analyze video
      const analysisRes = await fetch("/api/sentiment-inference", {
        method: "POST",
        headers: {
          Authorization: "Bearer " + apiKey,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ key }),
      });

      if (!analysisRes.ok) {
        const error = (await analysisRes.json()) as { error?: string };
        throw new Error(error?.error ?? "Failed to analyze video");
      }

      const analysis = (await analysisRes.json()) as Analysis;

      onAnalysis(analysis);
    } catch (error) {
      setError(error instanceof Error ? error.message : "Upload failed");
    } finally {
      setStatus("idle");
    }
  };

  return (
    <div className="flex w-full flex-col gap-2">
      <div className="flex w-full cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-gray-300 p-10">
        <input
          type="file"
          accept={guarded ? ".mp4" : ".mp4,.mov,.avi"}
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            e.target.value = "";
            if (file) void handleUpload(file);
          }}
          disabled={status !== "idle"}
          id="video-upload"
        />
        <label
          htmlFor="video-upload"
          className="flex cursor-pointer flex-col items-center"
        >
          <FiUpload className="min-h-8 min-w-8 text-gray-400" />
          <h3 className="text-md mt-2 from-indigo-50 text-slate-800">
            {status === "uploading"
              ? "Uploading..."
              : status === "analyzing"
                ? "Analysing..."
                : "Upload a video"}
          </h3>
          <p className="text-center text-xs text-gray-500">
            Get started with sentiment detection by uploading a video.
          </p>
        </label>
      </div>
      {error && <div className="text-sm text-red-500">{error}</div>}
    </div>
  );
}

export default UploadVideo;
