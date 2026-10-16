// Markdown tokenizers that read only the lines they can consume.
//
// marked offers every registered block tokenizer the whole remaining document
// at each block boundary, and asks each `start` callback for a cut position at
// every paragraph. The upstream orderedList, taskList and table tokenizers
// split that entire remainder into lines before (usually) declining it, so a
// parse costs O(blocks × lines): a 1 MiB file keeps the tab frozen for minutes.
// These variants hand the upstream tokenizers a prefix that ends where their
// own grammar must stop, so the parsed document is unchanged.

import type { MarkdownTokenizer, Node } from "@tiptap/core";
import { ORDERED_LIST_MARKER_PATTERN, OrderedList, TaskList } from "@tiptap/extension-list";
import { Table } from "@tiptap/extension-table";

/** Index of the end of the line that starts at `from` (its "\n", or the end). */
function lineEnd(src: string, from: number): number {
  const newline = src.indexOf("\n", from);
  return newline === -1 ? src.length : newline;
}

/** Number of leading whitespace characters, as upstream measures indentation. */
function indentOf(line: string): number {
  return line.length - line.trimStart().length;
}

/** `src` cut just before the line starting at `lineStart` (and its newline). */
function before(src: string, lineStart: number): string {
  return src.slice(0, Math.max(lineStart - 1, 0));
}

// Line shapes the upstream list tokenizers recognise, with their marker pattern.
const ORDERED_ITEM = new RegExp(String.raw`^\s*(?:${ORDERED_LIST_MARKER_PATTERN})[.)]\s`);
const TASK_ITEM = /^\s*[-+*]\s+\[[ xX]\]\s/;

/**
 * The prefix of `src` an ordered list starting on its first line can span, or
 * null when the first line is not an item. Upstream consumes item, blank,
 * indented and lazy-continuation lines and stops at the first other line that
 * follows a blank line, so the prefix ends before such a line.
 */
export function orderedListPrefix(src: string): string | null {
  let end = lineEnd(src, 0);
  if (!ORDERED_ITEM.test(src.slice(0, end))) return null;
  let sawBlankLine = false;
  while (end < src.length) {
    const start = end + 1;
    end = lineEnd(src, start);
    const line = src.slice(start, end);
    if (ORDERED_ITEM.test(line)) sawBlankLine = false;
    else if (line.trim() === "") sawBlankLine = true;
    else if (sawBlankLine && indentOf(line) === 0) return before(src, start);
  }
  return src;
}

/**
 * The prefix of `src` a task list can span, or null when its first non-blank
 * line is not a task item. Upstream treats deeper-indented lines as item
 * content and stops at the first non-item line indented no deeper than the
 * current item, so the prefix ends before such a line.
 */
export function taskListPrefix(src: string): string | null {
  let start = 0;
  let end = lineEnd(src, 0);
  while (src.slice(start, end).trim() === "") {
    if (end >= src.length) return null;
    start = end + 1;
    end = lineEnd(src, start);
  }
  const first = src.slice(start, end);
  if (!TASK_ITEM.test(first)) return null;
  let itemIndent = indentOf(first);
  while (end < src.length) {
    start = end + 1;
    end = lineEnd(src, start);
    const line = src.slice(start, end);
    if (line.trim() === "") continue;
    const indent = indentOf(line);
    if (TASK_ITEM.test(line)) {
      if (indent <= itemIndent) itemIndent = indent;
    } else if (indent <= itemIndent) {
      return before(src, start);
    }
  }
  return src;
}

/** The first two lines of `src`: all the upstream table `start` inspects. */
export function tableStartPrefix(src: string): string {
  return src.slice(0, lineEnd(src, lineEnd(src, 0) + 1));
}

/**
 * The prefix the upstream table tokenizer can consume: it reads up to the
 * first blank line, then re-splits the source to take at most one line more.
 */
export function tableTokenizePrefix(src: string): string {
  const blankLine = src.indexOf("\n\n");
  return blankLine === -1 ? src : src.slice(0, lineEnd(src, blankLine + 2));
}

interface Bounds {
  /** Prefix for `tokenize`; null declines without consulting upstream. */
  tokenize: (src: string) => string | null;
  /** Prefix for `start`; omitted keeps the upstream callback as is. */
  start?: (src: string) => string;
}

/** Re-create `extension` with its markdown tokenizer reading bounded prefixes. */
function withBoundedTokenizer<T extends Node>(extension: T, bounds: Bounds): T {
  const upstream = extension.config.markdownTokenizer;
  if (!upstream) return extension;
  const tokenizer: MarkdownTokenizer = {
    ...upstream,
    tokenize: (src, tokens, lexer) => {
      const prefix = bounds.tokenize(src);
      return prefix === null ? undefined : upstream.tokenize(prefix, tokens, lexer);
    },
  };
  const { start } = upstream;
  if (bounds.start && typeof start === "function") {
    const boundStart = bounds.start;
    tokenizer.start = (src) => start(boundStart(src));
  }
  return extension.extend({ markdownTokenizer: tokenizer }) as T;
}

/** Drop-in for StarterKit's OrderedList (configure StarterKit with `orderedList: false`). */
export const BoundedOrderedList = withBoundedTokenizer(OrderedList, {
  tokenize: orderedListPrefix,
});

export const BoundedTaskList = withBoundedTokenizer(TaskList, { tokenize: taskListPrefix });

export const BoundedTable = withBoundedTokenizer(Table, {
  tokenize: tableTokenizePrefix,
  start: tableStartPrefix,
});
