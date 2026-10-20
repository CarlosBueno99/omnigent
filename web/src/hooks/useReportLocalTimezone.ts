import { useEffect } from "react";
import { useViewerId } from "@/hooks/useViewerId";
import { reportLocalTimezone } from "@/lib/scheduledTasksApi";

/** Once the viewer is known, record the browser's zone as their automation default. */
export function useReportLocalTimezone(): void {
  const viewerId = useViewerId();
  useEffect(() => {
    if (viewerId !== null) void reportLocalTimezone();
  }, [viewerId]);
}
