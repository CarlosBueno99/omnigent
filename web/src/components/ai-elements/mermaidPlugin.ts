import {
  mermaid as streamdownMermaid,
  type DiagramPlugin,
  type MermaidInstance,
} from "@streamdown/mermaid";

function parseSvgAsHtml(markup: string): SVGSVGElement | null {
  return new DOMParser().parseFromString(markup, "text/html").querySelector("svg");
}

// The HTML parser keeps xmlns attributes that XMLSerializer then emits again
// beside its own declarations; neither changes the tree the HTML parser builds.
function removeNamespaceDeclarations(root: Element): void {
  for (const element of [root, ...Array.from(root.querySelectorAll("*"))]) {
    for (const { name } of Array.from(element.attributes)) {
      if (name === "xmlns" || name.startsWith("xmlns:")) element.removeAttribute(name);
    }
  }
}

/**
 * Re-serialise mermaid's HTML-serialised SVG (bare `<br>`, `&nbsp;` in
 * labels) as well-formed XML, so it opens as a standalone file and loads
 * through `<img>`. Streamdown injects the same string inline, so the XML is
 * used only when the HTML parser builds the same tree from it as from the
 * sanitiser's markup; otherwise the input is returned unchanged.
 */
export function serializeSvgAsXml(svg: string): string {
  if (typeof DOMParser === "undefined" || typeof XMLSerializer === "undefined") return svg;
  const root = parseSvgAsHtml(svg);
  if (!root) return svg;
  removeNamespaceDeclarations(root);
  const xml = new XMLSerializer().serializeToString(root);
  const reparsed = parseSvgAsHtml(xml);
  if (!reparsed) return svg;
  removeNamespaceDeclarations(reparsed);
  return reparsed.isEqualNode(root) ? xml : svg;
}

function withXmlSvg(instance: MermaidInstance): MermaidInstance {
  return {
    ...instance,
    render: async (id, source) => {
      const result = await instance.render(id, source);
      return { ...result, svg: serializeSvgAsXml(result.svg) };
    },
  };
}

/**
 * Streamdown's mermaid plugin with standalone-file SVG output. Streamdown's
 * download menu renders through the plugin, so this is the only seam the app
 * controls on that path.
 */
export const mermaid: DiagramPlugin = {
  ...streamdownMermaid,
  getMermaid: (config) => withXmlSvg(streamdownMermaid.getMermaid(config)),
};
