"use client";

import * as React from "react";
import { useSearchParams } from "next/navigation";

import { useApp } from "@/components/features/app-shell";
import { ConflictInbox } from "@/components/features/conflict-inbox";
import { Skeleton } from "@/components/ui/skeleton";

function ConflictInboxPageContent() {
  const params = useSearchParams();
  const { timezone } = useApp();

  return (
    <ConflictInbox
      initialSourceId={params.get("source")?.trim() || null}
      initialDocumentId={params.get("document")?.trim() || ""}
      timezone={timezone}
    />
  );
}

export default function ConflictInboxPage() {
  return (
    <React.Suspense
      fallback={
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 md:p-6">
          <Skeleton className="h-12 rounded-lg" />
          <Skeleton className="h-48 rounded-lg" />
        </div>
      }
    >
      <ConflictInboxPageContent />
    </React.Suspense>
  );
}
