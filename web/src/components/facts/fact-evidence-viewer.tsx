"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { DocumentViewerSurface } from "@/components/document-viewer-dialog";
import { QueryError, TableLoading } from "@/components/query-state";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import type { CollectionItemRead, MatterFactEvidenceRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

/**
 * Opens the cited document with the quote highlighted. The quote is matched as text rather than by paragraph number
 * because the viewer renders the native document, whose paragraph numbering can differ from the extracted text the
 * quote was verified against.
 */
export function FactEvidenceViewer({ matterId, evidence, onClose }: { matterId: string; evidence: MatterFactEvidenceRead | null; onClose: () => void }) {
  const documentId = evidence?.matter_document_id;
  const item = useQuery({
    queryKey: ["matter-document-collection-item", matterId, documentId],
    queryFn: () => coreApi<CollectionItemRead>(`/v1/matters/${matterId}/documents/${documentId}/collection-item`),
    enabled: Boolean(documentId),
  });
  const title = item.data?.email?.subject?.trim() || item.data?.original_filename || evidence?.control_number || "Document";
  return (
    <Dialog open={Boolean(evidence)} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="flex h-[min(90vh,56rem)] max-w-5xl flex-col overflow-hidden p-0">
        <div className="border-b px-5 py-3">
          <DialogTitle className="truncate text-base">{title}</DialogTitle>
          <DialogDescription className="mt-1 line-clamp-2 text-sm">
            Cited {evidence?.paragraph_id}: “{evidence?.quote}”
          </DialogDescription>
          {documentId ? (
            <Link className="mt-1 inline-block text-xs font-medium text-primary hover:underline" href={`/review/matters/${matterId}?document=${documentId}`}>
              Open in Search &amp; Review
            </Link>
          ) : null}
        </div>
        <div className="min-h-0 flex-1">
          {item.isPending ? <div className="p-5"><TableLoading /></div>
            : item.error ? <div className="p-5"><QueryError message={item.error.message} /></div>
            : item.data && evidence ? (
              <DocumentViewerSurface
                item={item.data}
                className="h-full"
                contentPath={`/v1/matters/${matterId}/documents/${evidence.matter_document_id}/content`}
                highlightTerms={[evidence.quote]}
              />
            ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}
