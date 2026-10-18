"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { useMatter } from "@/components/matter/matter-context";
import { MatterTokenUsagePanel } from "@/components/matter-token-usage-panel";
import { QueryError, TableLoading } from "@/components/query-state";
import type { MatterProviderUsageReportRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

export function UsageSection() {
  const { matterId } = useMatter();
  const [offset, setOffset] = useState(0);
  const providerUsage = useQuery({
    queryKey: ["matter-provider-usage", matterId, offset],
    queryFn: () => coreApi<MatterProviderUsageReportRead>(`/v1/matters/${matterId}/provider-usage?offset=${offset}&limit=100`),
    placeholderData: (previous) => previous,
  });

  if (providerUsage.isPending) return <TableLoading />;
  if (providerUsage.error) return <QueryError message={providerUsage.error.message} />;
  return <MatterTokenUsagePanel report={providerUsage.data} loading={providerUsage.isFetching} onPageChange={setOffset} />;
}
