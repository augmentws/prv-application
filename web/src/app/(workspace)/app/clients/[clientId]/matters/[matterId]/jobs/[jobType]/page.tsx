import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { JobsSection } from "@/components/matter/sections/jobs/jobs-section";
import { jobTypeFromSlug, MATTER_JOB_TYPES } from "@/lib/matter-routes";

export async function generateMetadata({ params }: PageProps<"/app/clients/[clientId]/matters/[matterId]/jobs/[jobType]">): Promise<Metadata> {
  const { jobType } = await params;
  const label = MATTER_JOB_TYPES.find((item) => item.slug === jobType)?.label;
  return { title: label ? `${label} · Jobs` : "Jobs" };
}

export default async function MatterJobTypePage({
  params,
  searchParams,
}: PageProps<"/app/clients/[clientId]/matters/[matterId]/jobs/[jobType]"> & {
  searchParams: Promise<{ job?: string; batch_set?: string }>;
}) {
  const jobType = jobTypeFromSlug((await params).jobType);
  if (!jobType) notFound();
  const { job, batch_set: batchSetId } = await searchParams;
  return <JobsSection jobType={jobType} selectedJobId={job} batchSetId={batchSetId} />;
}
