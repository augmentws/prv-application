"use client";

import { useQuery } from "@tanstack/react-query";
import { Download, FileText, GripHorizontal, Mail } from "lucide-react";
import { useMemo, useRef, useState } from "react";

import { QueryError } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import type { CollectionItemRead, EmailRecipientInput } from "@/generated/models";
import { coreApiContent } from "@/lib/api-client";
import { emailBody, textDocument } from "@/lib/document-content";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

const EMAIL_HEADER_DEFAULT_HEIGHT = 168;
const EMAIL_HEADER_MIN_HEIGHT = 64;
const EMAIL_HEADER_MAX_HEIGHT = 480;
const EMAIL_HEADER_KEYBOARD_STEP = 16;

function recipientLabel(recipient: EmailRecipientInput) {
  if (recipient.display_name && recipient.email_address) return `${recipient.display_name} <${recipient.email_address}>`;
  return recipient.display_name || recipient.email_address || "Unknown recipient";
}

function recipients(item: CollectionItemRead, type: EmailRecipientInput["recipient_type"]) {
  return (item.email?.recipients ?? []).filter((recipient) => recipient.recipient_type === type).map(recipientLabel).join(", ");
}

function fullDate(value: string | null | undefined) {
  if (!value) return null;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "long" }).format(new Date(value));
}

function previewKind(item: CollectionItemRead | null) {
  if (!item) return null;
  const extension = (item.original_extension ?? "").toLowerCase().replace(/^\./, "");
  const mediaType = item.native_artifact.media_type.toLowerCase();
  if (item.record_type === "EMAIL" || extension === "eml" || mediaType === "message/rfc822") return "email";
  if (extension === "txt" || mediaType.startsWith("text/")) return "text";
  return "unsupported";
}

function EmailHeader({ item }: { item: CollectionItemRead }) {
  const fields = [
    ["From", item.email?.sender],
    ["To", recipients(item, "TO")],
    ["CC", recipients(item, "CC")],
    ["BCC", recipients(item, "BCC")],
    ["Sent", fullDate(item.email?.sent_at)],
    ["Received", fullDate(item.email?.received_at)],
  ].filter((field): field is [string, string] => Boolean(field[1]));

  if (!fields.length) return null;
  return (
    <dl className="grid gap-x-5 gap-y-2 bg-muted/35 px-5 py-4 text-sm sm:grid-cols-[5rem_minmax(0,1fr)]">
      {fields.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="font-medium text-muted-foreground">{label}</dt>
          <dd className="min-w-0 break-words text-foreground">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function DocumentViewerSurface({ item, className }: { item: CollectionItemRead; className?: string }) {
  const viewerRef = useRef<HTMLElement>(null);
  const emailHeaderDrag = useRef<{ y: number; height: number } | null>(null);
  const [emailHeaderHeight, setEmailHeaderHeight] = useState(EMAIL_HEADER_DEFAULT_HEIGHT);
  const kind = previewKind(item);
  const content = useQuery({
    queryKey: ["artifact-content", item.native_artifact.id],
    queryFn: () => coreApiContent(`/v1/artifacts/${item.native_artifact.id}/content`),
    enabled: kind !== "unsupported",
    staleTime: 5 * 60 * 1000,
  });
  const body = useMemo(() => {
    if (!content.data || !kind) return null;
    return kind === "email"
      ? emailBody(content.data.bytes).text
      : textDocument(content.data.bytes, content.data.mediaType);
  }, [content.data, kind]);

  const title = item.email?.subject?.trim() || item.original_filename || "Document";
  const downloadUrl = `/api/core/v1/artifacts/${item.native_artifact.id}/content`;
  const clampEmailHeaderHeight = (next: number) => {
    const availableHeight = viewerRef.current?.clientHeight;
    const responsiveMaximum = availableHeight
      ? Math.max(EMAIL_HEADER_MIN_HEIGHT, availableHeight - 200)
      : EMAIL_HEADER_MAX_HEIGHT;
    const maximum = Math.min(EMAIL_HEADER_MAX_HEIGHT, responsiveMaximum);
    setEmailHeaderHeight(Math.min(maximum, Math.max(EMAIL_HEADER_MIN_HEIGHT, next)));
  };

  return (
    <section ref={viewerRef} className={cn("flex min-h-0 flex-col overflow-hidden bg-card", className)} aria-label="Document viewer">
      <header className="border-b px-4 py-3">
        <div className="flex min-w-0 items-start gap-3">
          <span className="mt-0.5 grid size-9 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground">
            {kind === "email" ? <Mail className="size-4" /> : <FileText className="size-4" />}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 items-center gap-2">
              <h2 className="truncate text-base font-semibold">{title}</h2>
              <Badge variant="outline">{kind === "email" ? "Email" : kind === "text" ? "Text" : "File"}</Badge>
            </div>
            <p className="truncate text-xs text-muted-foreground">{item.original_filename} · {formatBytes(item.native_artifact.byte_length)}</p>
          </div>
          <Button asChild variant="outline" size="sm"><a href={downloadUrl} download><Download />Download</a></Button>
        </div>
      </header>

      {kind === "email" ? (
        <>
          <div className="shrink-0 overflow-y-auto bg-muted/35" style={{ height: `${emailHeaderHeight}px` }}>
            <EmailHeader item={item} />
          </div>
          <div
            role="separator"
            aria-label="Resize email metadata panel"
            aria-orientation="horizontal"
            aria-valuemin={EMAIL_HEADER_MIN_HEIGHT}
            aria-valuemax={EMAIL_HEADER_MAX_HEIGHT}
            aria-valuenow={emailHeaderHeight}
            tabIndex={0}
            title="Drag to resize email metadata"
            className="group relative z-10 flex h-2 shrink-0 touch-none cursor-row-resize items-center justify-center border-y bg-muted/40 outline-none hover:bg-primary/10 focus-visible:bg-primary/10 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
            onPointerDown={(event) => {
              emailHeaderDrag.current = { y: event.clientY, height: emailHeaderHeight };
              event.currentTarget.setPointerCapture(event.pointerId);
            }}
            onPointerMove={(event) => {
              if (!emailHeaderDrag.current) return;
              clampEmailHeaderHeight(emailHeaderDrag.current.height + event.clientY - emailHeaderDrag.current.y);
            }}
            onPointerUp={(event) => {
              emailHeaderDrag.current = null;
              if (event.currentTarget.hasPointerCapture(event.pointerId)) {
                event.currentTarget.releasePointerCapture(event.pointerId);
              }
            }}
            onPointerCancel={() => { emailHeaderDrag.current = null; }}
            onKeyDown={(event) => {
              if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
              event.preventDefault();
              clampEmailHeaderHeight(emailHeaderHeight + (event.key === "ArrowDown" ? EMAIL_HEADER_KEYBOARD_STEP : -EMAIL_HEADER_KEYBOARD_STEP));
            }}
          >
            <GripHorizontal className="size-4 text-muted-foreground opacity-55 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100" />
          </div>
        </>
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto bg-background">
        {kind === "unsupported" ? (
          <div className="grid min-h-full place-items-center p-8 text-center">
            <div><FileText className="mx-auto size-9 text-muted-foreground" /><p className="mt-3 font-medium">Preview is not available for this file type.</p><p className="mt-1 text-sm text-muted-foreground">Download the original file to open it.</p></div>
          </div>
        ) : content.isPending ? (
          <div className="space-y-3 p-5">{Array.from({ length: 8 }, (_, index) => <Skeleton key={index} className="h-5 w-full" />)}</div>
        ) : content.error ? (
          <div className="p-5"><QueryError message={content.error.message} /></div>
        ) : (
          <pre className={`min-h-full whitespace-pre-wrap break-words p-5 text-sm leading-7 text-foreground ${kind === "text" ? "font-mono" : "font-sans"}`}>
            {body || "This document has no displayable text."}
          </pre>
        )}
      </div>
    </section>
  );
}

export function DocumentViewerDialog({ item, onClose }: { item: CollectionItemRead | null; onClose: () => void }) {
  return (
    <Dialog open={Boolean(item)} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="h-[min(90vh,56rem)] max-w-5xl overflow-hidden p-0">
        <DialogTitle className="sr-only">{item?.email?.subject?.trim() || item?.original_filename || "Document"}</DialogTitle>
        {item ? <DocumentViewerSurface item={item} className="h-full" /> : null}
      </DialogContent>
    </Dialog>
  );
}
