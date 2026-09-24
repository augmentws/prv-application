import type { MatterBulkTagJobRead } from "@/generated/models";

export function bulkTagIsWaiting(job: MatterBulkTagJobRead) {
  return job.status === "RUNNING" && job.matched_count > 0 && job.processed_count === 0;
}

export function bulkTagDisplayStatus(job: MatterBulkTagJobRead) {
  return bulkTagIsWaiting(job) ? "WAITING" : job.status;
}
