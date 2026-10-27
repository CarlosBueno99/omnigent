// Expanded view of a file-edit tool call: the change as a diff, with the
// tool's reported failure above it.

import { useMemo } from "react";
import { getFileEditDiff } from "@/lib/toolEditDiff";
import type { ToolCall } from "@/lib/toolResultSummary";
import { parseToolOutput, toolErrorMessage } from "@/lib/toolValue";
import { CodePanel } from "./ToolCodePanel";
import { ToolValueView } from "./ToolValueView";

export function FileEditBody({ call }: { call: ToolCall }) {
  const { name, args, output } = call;
  const diff = useMemo(() => getFileEditDiff(name, args), [name, args]);
  const error = useMemo(
    () => (output === null ? null : toolErrorMessage(parseToolOutput(output))),
    [output],
  );
  return (
    <div className="space-y-2">
      {error !== null && (
        <div className="max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-destructive text-sm">
          {error}
        </div>
      )}
      {diff === null ? (
        <>
          <p className="text-muted-foreground text-sm">No diff recorded for this edit.</p>
          {Object.keys(args).length > 0 && <ToolValueView value={args} />}
        </>
      ) : (
        <CodePanel
          title="Diff"
          text={diff.text}
          copyText={diff.text}
          copyLabel="Copy diff"
          language="diff"
        />
      )}
    </div>
  );
}
