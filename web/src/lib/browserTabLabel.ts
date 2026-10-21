/** Label for a browser tab that has not loaded a page yet. */
export const NEW_BROWSER_TAB_LABEL = "New tab";

/** Longest manual browser-tab name the rename field accepts. */
export const BROWSER_TAB_NAME_MAX_CHARS = 80;

/** Longest label for a URL without a host, such as a `data:` page. */
const RAW_URL_LABEL_MAX_CHARS = 60;

function clampRawLabel(text: string): string {
  return text.length > RAW_URL_LABEL_MAX_CHARS
    ? `${text.slice(0, RAW_URL_LABEL_MAX_CHARS)}…`
    : text;
}

function decodeSegment(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

/**
 * Short label for a page URL: host plus at most the last two path segments,
 * e.g. `github.com/…/pull/123` or `localhost:3000/settings`. Query and hash
 * are dropped; a blank or `about:blank` URL reads as a new tab.
 */
export function browserUrlLabel(url: string | null | undefined): string {
  const trimmed = url?.trim() ?? "";
  if (trimmed === "" || trimmed === "about:blank") return NEW_BROWSER_TAB_LABEL;
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return clampRawLabel(trimmed);
  }
  const segments = parsed.pathname.split("/").filter(Boolean).map(decodeSegment);
  if (parsed.protocol === "http:" || parsed.protocol === "https:") {
    const host = parsed.host.replace(/^www\./, "");
    if (segments.length === 0) return host;
    const tail = segments.slice(-2).join("/");
    return segments.length > 2 ? `${host}/…/${tail}` : `${host}/${tail}`;
  }
  if (parsed.protocol === "file:") return segments.at(-1) ?? trimmed;
  return clampRawLabel(trimmed);
}

/** The visible name of a browser tab: its manual name, else its URL label. */
export function browserTabLabel(
  manualName: string | null | undefined,
  url: string | null | undefined,
): string {
  const name = manualName?.trim() ?? "";
  return name !== "" ? name : browserUrlLabel(url);
}
