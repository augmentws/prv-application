"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { type ReactElement, useCallback, useState } from "react";
import { toast } from "sonner";

import { SoftDeleteDialog } from "@/components/soft-delete-dialog";
import { Button } from "@/components/ui/button";
import { SplitActionButton } from "@/components/ui/split-action-button";
import { coreApi } from "@/lib/api-client";

export const TERMINAL_JOB_STATUSES = ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"];

/** Soft-deletes a job and drops it from the cached list(s) under `queryKey`. */
export function useDeleteJob(queryKey: readonly unknown[]) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: ({ jobType, jobId }: { jobType: string; jobId: string }) => coreApi<void>(`/v1/jobs/${jobType}/${jobId}`, { method: "DELETE" }),
    onSuccess: (_, { jobId }) => {
      queryClient.setQueriesData<{ id: string }[]>({ queryKey }, (current) => current?.filter((job) => job.id !== jobId));
      toast.success("The job was deleted. Its execution history and results have been retained.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The job could not be deleted."),
  });
  return useCallback((jobType: string, jobId: string) => mutation.mutateAsync({ jobType, jobId }).then(() => undefined), [mutation]);
}

export function DeletableJobActions({ canDelete, jobType, jobId, jobName, onDelete, primary }: {
  canDelete: boolean;
  jobType: string;
  jobId: string;
  jobName: string;
  onDelete: (jobType: string, jobId: string) => Promise<void>;
  primary?: ReactElement;
}) {
  const [open, setOpen] = useState(false);
  if (!canDelete) return primary ? <Button asChild size="sm" variant="outline">{primary}</Button> : null;
  return <>
    {primary ? <SplitActionButton
      label={jobName}
      primary={primary}
      variant="outline"
      actions={[{
        id: "delete",
        label: "Delete job",
        icon: <Trash2 aria-hidden="true" className="size-4" />,
        destructive: true,
        onSelect: () => setOpen(true),
      }]}
    /> : <Button type="button" size="sm" variant="outline" onClick={() => setOpen(true)}><Trash2 />Delete job</Button>}
    <SoftDeleteDialog open={open} onOpenChange={setOpen} resourceType="job" resourceName={jobName} onDelete={() => onDelete(jobType, jobId)} />
  </>;
}

export function ProgressBar({ percent }: { percent: number }) {
  return <div className="mb-1 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary" style={{ width: `${percent}%` }} /></div>;
}
