// Titled code panel with wrap and copy controls, shared by tool-call bodies.

import { CheckIcon, CopyIcon, WrapTextIcon } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  CodeBlock,
  CodeBlockActions,
  type CodeLanguage,
  CodeBlockHeader,
  CodeBlockTitle,
} from "@/components/ai-elements/code-block";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

export function CodePanel({
  title,
  text,
  copyText,
  copyLabel,
  language = "json",
}: {
  title: string;
  text: string;
  copyText: string;
  copyLabel: string;
  language?: CodeLanguage;
}) {
  // Soft-wrap long lines by default so the panel never needs horizontal
  // scrolling to read; the toggle restores the scrolling view for when
  // column alignment matters (same affordance as chat code blocks).
  const [wrap, setWrap] = useState(true);
  return (
    <CodeBlock code={text} language={language} wrap={wrap}>
      <CodeBlockHeader>
        <CodeBlockTitle className="min-w-0">
          <span className="truncate font-medium uppercase tracking-wide">{title}</span>
        </CodeBlockTitle>
        <CodeBlockActions>
          <WrapTextButton onToggle={() => setWrap((value) => !value)} wrap={wrap} />
          <CopyTextButton label={copyLabel} text={copyText} />
        </CodeBlockActions>
      </CodeBlockHeader>
    </CodeBlock>
  );
}

export function WrapTextButton({ wrap, onToggle }: { wrap: boolean; onToggle: () => void }) {
  const label = wrap ? "Disable word wrap" : "Enable word wrap";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={label}
          aria-pressed={wrap}
          className={cn("size-6 text-muted-foreground", wrap && "text-foreground")}
          onClick={onToggle}
          size="icon-xs"
          type="button"
          variant="ghost"
          componentId="diagnostics.tool.toggle_wrap"
        >
          <WrapTextIcon className="size-3.5" />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

interface CopyTextButtonProps {
  text: string;
  label: string;
  /** Visible text next to the icon, for when several copy buttons sit together. */
  caption?: string;
}

export function CopyTextButton({ text, label, caption }: CopyTextButtonProps) {
  const [isCopied, setIsCopied] = useState(false);
  const timeoutRef = useRef<number | null>(null);

  const copyToClipboard = useCallback(async () => {
    if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) {
      return;
    }

    try {
      await navigator.clipboard.writeText(text);
    } catch {
      return;
    }

    setIsCopied(true);
    if (timeoutRef.current !== null) {
      window.clearTimeout(timeoutRef.current);
    }
    timeoutRef.current = window.setTimeout(() => setIsCopied(false), 2000);
  }, [text]);

  useEffect(
    () => () => {
      if (timeoutRef.current !== null) {
        window.clearTimeout(timeoutRef.current);
      }
    },
    [],
  );

  const Icon = isCopied ? CheckIcon : CopyIcon;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={isCopied ? "Copied" : label}
          className={cn("text-muted-foreground", caption ? "h-6 gap-1 px-1.5" : "size-6")}
          onClick={copyToClipboard}
          size={caption ? "xs" : "icon-xs"}
          type="button"
          variant="ghost"
          componentId="diagnostics.tool.copy"
        >
          <Icon className="size-3.5" />
          {caption}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{isCopied ? "Copied" : label}</TooltipContent>
    </Tooltip>
  );
}
