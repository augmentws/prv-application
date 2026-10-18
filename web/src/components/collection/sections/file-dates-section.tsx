"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { useCollection } from "@/components/collection/collection-context";
import { DateHistogram, type DateHistogramInterval } from "@/components/date-histogram";
import { Card } from "@/components/ui/card";
import type { CollectionDateHistogramResponse } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { collectionFilterSearch, emptyCollectionFilters } from "@/lib/collection-filters";
import { collectionHref } from "@/lib/collection-routes";

/** Canonical file dates by week, month or year. A bucket opens Documents filtered to its dates; Back returns here. */
export function FileDatesSection() {
  const { clientId, collectionId } = useCollection();
  const router = useRouter();
  const [histogramInterval, setHistogramInterval] = useState<DateHistogramInterval>("month");
  const histogram = useQuery({
    queryKey: ["collection-date-histogram", collectionId, histogramInterval],
    queryFn: () => coreApi<CollectionDateHistogramResponse>(`/v1/collections/${collectionId}/date-histogram?interval=${histogramInterval}`),
    placeholderData: (previous) => previous,
  });

  return (
    <Card className="p-5">
      <DateHistogram
        buckets={histogram.data?.buckets ?? []}
        interval={histogramInterval}
        onIntervalChange={setHistogramInterval}
        loading={histogram.isPending}
        error={Boolean(histogram.error)}
        onSelect={(range) => router.push(collectionHref(clientId, collectionId, "documents", collectionFilterSearch({ ...emptyCollectionFilters(), fileDateRange: range })))}
      />
      {histogram.data?.missing_count ? (
        <p className="mt-4 text-sm text-muted-foreground">
          {histogram.data.missing_count.toLocaleString()} documents do not have a file date.
        </p>
      ) : null}
    </Card>
  );
}
