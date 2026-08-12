import { useEffect, useRef, useState } from "react";
import { getJob } from "../api/client";
import type { Job } from "../types";

const POLL_INTERVAL_MS = 500;

interface IndexProgressProps {
  jobId: string;
  onComplete: () => void;
}

export function IndexProgress({ jobId, onComplete }: IndexProgressProps) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const onCompleteRef = useRef(onComplete);
  onCompleteRef.current = onComplete;

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    async function poll() {
      try {
        const j = await getJob(jobId);
        if (cancelled) return;
        setJob(j);
        if (j.state === "succeeded") {
          onCompleteRef.current();
          return; // stop polling
        }
        if (j.state === "failed") {
          return; // stop polling; error is shown from job.error
        }
        timer = window.setTimeout(poll, POLL_INTERVAL_MS);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "failed to fetch job status");
      }
    }
    poll();

    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [jobId]);

  if (error) return <p className="text-sm text-red-400">{error}</p>;
  if (!job) return <p className="text-sm text-neutral-400">Starting…</p>;

  const percent = Math.round(job.progress * 100);

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-neutral-800 bg-neutral-900 p-4">
      <div className="flex items-center justify-between text-sm">
        <span className="text-neutral-300">{job.stage ?? job.state}</span>
        <span className="text-neutral-500">{percent}%</span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-neutral-800">
        <div
          className={`h-full rounded-full transition-all ${
            job.state === "failed" ? "bg-red-500" : "bg-blue-500"
          }`}
          style={{ width: `${percent}%` }}
        />
      </div>
      {job.message && <p className="text-xs text-neutral-500">{job.message}</p>}
      {job.state === "failed" && job.error && (
        <p className="text-xs text-red-400">{job.error}</p>
      )}
    </div>
  );
}
