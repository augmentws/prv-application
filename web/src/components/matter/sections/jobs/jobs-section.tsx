"use client";

import Link from "next/link";

import { useMatter } from "@/components/matter/matter-context";
import { BulkTagJobs } from "@/components/matter/sections/jobs/bulk-tag-jobs";
import { ClassificationAnalysisJobs } from "@/components/matter/sections/jobs/classification-analysis-jobs";
import { EmbeddingJobs } from "@/components/matter/sections/jobs/embedding-jobs";
import { ImportJobs } from "@/components/matter/sections/jobs/import-jobs";
import { QuestionAnsweringJobs } from "@/components/matter/sections/jobs/question-answering-jobs";
import { SearchExportJobs } from "@/components/matter/sections/jobs/search-export-jobs";
import { TopicJobs } from "@/components/matter/sections/jobs/topic-jobs";
import { subtabClass } from "@/components/matter/sections/subtabs";
import { MATTER_JOB_TYPES, type MatterJobType, matterJobsHref } from "@/lib/matter-routes";

/** One job type's page; the job types are links so each has its own URL and Back works between them. */
export function JobsSection({ jobType, selectedJobId, batchSetId }: { jobType: MatterJobType; selectedJobId?: string; batchSetId?: string }) {
  const { clientId, matterId } = useMatter();

  return <div className="space-y-6">
    <nav aria-label="Job types" className="flex overflow-x-auto border-b">
      {MATTER_JOB_TYPES.map((item) => (
        <Link
          key={item.type}
          href={matterJobsHref(clientId, matterId, item.type)}
          aria-current={item.type === jobType ? "page" : undefined}
          className={`shrink-0 whitespace-nowrap ${subtabClass(item.type === jobType)}`}
        >
          {item.label}
        </Link>
      ))}
    </nav>
    {jobType === "BULK_TAGGING" ? <BulkTagJobs />
      : jobType === "DOCUMENT_EMBEDDINGS" ? <EmbeddingJobs />
      : jobType === "TOPIC_CLUSTERING" ? <TopicJobs />
      : jobType === "DOCUMENT_IMPORTS" ? <ImportJobs selectedJobId={selectedJobId} />
      : jobType === "QUESTION_ANSWERING" ? <QuestionAnsweringJobs batchSetId={batchSetId} />
      : jobType === "CLASSIFICATION_ANALYSIS" ? <ClassificationAnalysisJobs selectedJobId={selectedJobId} />
      : <SearchExportJobs />}
  </div>;
}
