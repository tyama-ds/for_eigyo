/**
 * Minimal, safe markdown parser. Produces an AST that the <Markdown>
 * component renders as React elements — HTML in the source is never
 * interpreted (tags are stripped; everything else is plain text nodes),
 * so no dangerouslySetInnerHTML is needed anywhere.
 *
 * Supported: headings, paragraphs, ul/ol lists, fenced code blocks,
 * blockquotes, hr, bold, italic, inline code, links, [S1]-style citations.
 */

export type InlineNode =
  | { t: "text"; text: string }
  | { t: "strong"; children: InlineNode[] }
  | { t: "em"; children: InlineNode[] }
  | { t: "code"; text: string }
  | { t: "link"; href: string; children: InlineNode[] }
  | { t: "citation"; sid: string };

export type BlockNode =
  | { t: "heading"; level: number; children: InlineNode[] }
  | { t: "para"; children: InlineNode[] }
  | { t: "list"; ordered: boolean; items: InlineNode[][] }
  | { t: "codeblock"; text: string; lang: string | null }
  | { t: "blockquote"; children: InlineNode[] }
  | { t: "hr" };

/** Remove HTML tags (including script/style bodies) from raw text. */
export function stripHtmlTags(input: string): string {
  let s = input;
  // Drop dangerous element bodies entirely.
  s = s.replace(/<(script|style|iframe|object|embed)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, "");
  // Drop remaining tags (open, close, self-closing, comments).
  s = s.replace(/<!--[\s\S]*?-->/g, "");
  s = s.replace(/<\/?[a-zA-Z][^>]*>/g, "");
  return s;
}

/** Allow only http(s), mailto and relative URLs. Returns null when unsafe. */
export function sanitizeUrl(url: string): string | null {
  const trimmed = url.trim();
  if (trimmed === "") return null;
  // Reject control chars / whitespace tricks inside the scheme.
  const noControl = trimmed.replace(/[\u0000-\u0020]/g, "");
  const schemeMatch = /^([a-zA-Z][a-zA-Z0-9+.-]*):/.exec(noControl);
  if (schemeMatch) {
    const scheme = schemeMatch[1].toLowerCase();
    if (scheme === "http" || scheme === "https" || scheme === "mailto") {
      return trimmed;
    }
    return null;
  }
  // Relative / anchor / protocol-relative are allowed.
  return trimmed;
}

const CITATION_RE = /^\[(S\d+)\](?!\()/;

interface InlineOptions {
  citations?: boolean;
}

export function parseInline(
  raw: string,
  opts: InlineOptions = {},
): InlineNode[] {
  const src = stripHtmlTags(raw);
  const nodes: InlineNode[] = [];
  let buf = "";
  let i = 0;

  const flush = () => {
    if (buf !== "") {
      nodes.push({ t: "text", text: buf });
      buf = "";
    }
  };

  while (i < src.length) {
    const rest = src.slice(i);

    // Inline code
    if (src[i] === "`") {
      const end = src.indexOf("`", i + 1);
      if (end > i) {
        flush();
        nodes.push({ t: "code", text: src.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    }

    // Citation chip [S1]
    if (opts.citations && src[i] === "[") {
      const m = CITATION_RE.exec(rest);
      if (m) {
        flush();
        nodes.push({ t: "citation", sid: m[1] });
        i += m[0].length;
        continue;
      }
    }

    // Link [text](url)
    if (src[i] === "[") {
      const linkMatch = /^\[([^\]]*)\]\(([^)\s]*)(?:\s+"[^"]*")?\)/.exec(rest);
      if (linkMatch) {
        const href = sanitizeUrl(linkMatch[2]);
        flush();
        if (href) {
          nodes.push({
            t: "link",
            href,
            children: parseInline(linkMatch[1], opts),
          });
        } else {
          // Unsafe URL: render the label as plain text.
          nodes.push(...parseInline(linkMatch[1], opts));
        }
        i += linkMatch[0].length;
        continue;
      }
    }

    // Bold **text**
    if (rest.startsWith("**")) {
      const end = src.indexOf("**", i + 2);
      if (end > i + 1) {
        flush();
        nodes.push({
          t: "strong",
          children: parseInline(src.slice(i + 2, end), opts),
        });
        i = end + 2;
        continue;
      }
    }

    // Italic *text*
    if (src[i] === "*") {
      const end = src.indexOf("*", i + 1);
      if (end > i && src.slice(i + 1, end).trim() !== "") {
        flush();
        nodes.push({
          t: "em",
          children: parseInline(src.slice(i + 1, end), opts),
        });
        i = end + 1;
        continue;
      }
    }

    buf += src[i];
    i += 1;
  }
  flush();
  return nodes;
}

export function parseMarkdown(
  source: string | null | undefined,
  opts: InlineOptions = {},
): BlockNode[] {
  if (!source) return [];
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const blocks: BlockNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (line.trim() === "") {
      i += 1;
      continue;
    }

    // Fenced code block
    const fence = /^```(\S*)\s*$/.exec(line);
    if (fence) {
      const lang = fence[1] || null;
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // skip closing fence
      blocks.push({ t: "codeblock", text: body.join("\n"), lang });
      continue;
    }

    // Heading
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push({
        t: "heading",
        level: heading[1].length,
        children: parseInline(heading[2], opts),
      });
      i += 1;
      continue;
    }

    // Horizontal rule
    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line.trim())) {
      blocks.push({ t: "hr" });
      i += 1;
      continue;
    }

    // Blockquote (consume consecutive '>' lines)
    if (/^>\s?/.test(line)) {
      const quoted: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        quoted.push(lines[i].replace(/^>\s?/, ""));
        i += 1;
      }
      blocks.push({ t: "blockquote", children: parseInline(quoted.join(" "), opts) });
      continue;
    }

    // Unordered list
    if (/^\s*[-*+]\s+/.test(line)) {
      const items: InlineNode[][] = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(parseInline(lines[i].replace(/^\s*[-*+]\s+/, ""), opts));
        i += 1;
      }
      blocks.push({ t: "list", ordered: false, items });
      continue;
    }

    // Ordered list
    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items: InlineNode[][] = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        items.push(parseInline(lines[i].replace(/^\s*\d+[.)]\s+/, ""), opts));
        i += 1;
      }
      blocks.push({ t: "list", ordered: true, items });
      continue;
    }

    // Paragraph: consume until blank line or new block marker
    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !/^(#{1,6})\s+/.test(lines[i]) &&
      !/^```/.test(lines[i]) &&
      !/^\s*[-*+]\s+/.test(lines[i]) &&
      !/^\s*\d+[.)]\s+/.test(lines[i]) &&
      !/^>\s?/.test(lines[i])
    ) {
      para.push(lines[i]);
      i += 1;
    }
    const children = parseInline(para.join(" "), opts);
    // Skip paragraphs that became empty after HTML stripping.
    if (
      children.some(
        (n) => n.t !== "text" || n.text.trim() !== "",
      )
    ) {
      blocks.push({ t: "para", children });
    }
  }
  return blocks;
}

/** Plain-text extraction (used by tests to prove sanitization). */
export function inlineToText(nodes: InlineNode[]): string {
  return nodes
    .map((n) => {
      switch (n.t) {
        case "text":
          return n.text;
        case "code":
          return n.text;
        case "citation":
          return `[${n.sid}]`;
        case "strong":
        case "em":
          return inlineToText(n.children);
        case "link":
          return inlineToText(n.children);
      }
    })
    .join("");
}

export function blocksToText(blocks: BlockNode[]): string {
  return blocks
    .map((b) => {
      switch (b.t) {
        case "heading":
        case "para":
        case "blockquote":
          return inlineToText(b.children);
        case "list":
          return b.items.map(inlineToText).join("\n");
        case "codeblock":
          return b.text;
        case "hr":
          return "";
      }
    })
    .join("\n");
}
