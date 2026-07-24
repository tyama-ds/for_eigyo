import type { ReactNode } from "react";
import {
  parseMarkdown,
  type BlockNode,
  type InlineNode,
} from "@/lib/markdown";
import { t } from "@/lib/i18n";

interface MarkdownProps {
  source: string | null | undefined;
  /** Enable [S1]-style citation chips. */
  citations?: boolean;
  onCitationClick?: (sid: string) => void;
}

function isExternal(href: string): boolean {
  return /^https?:\/\//i.test(href) || href.startsWith("//");
}

function renderInline(
  nodes: InlineNode[],
  keyPrefix: string,
  onCitationClick?: (sid: string) => void,
): ReactNode[] {
  return nodes.map((node, idx) => {
    const key = `${keyPrefix}-${idx}`;
    switch (node.t) {
      case "text":
        return <span key={key}>{node.text}</span>;
      case "strong":
        return (
          <strong key={key}>
            {renderInline(node.children, key, onCitationClick)}
          </strong>
        );
      case "em":
        return (
          <em key={key}>{renderInline(node.children, key, onCitationClick)}</em>
        );
      case "code":
        return (
          <code
            key={key}
            className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[0.85em]"
          >
            {node.text}
          </code>
        );
      case "link": {
        const external = isExternal(node.href);
        return (
          <a
            key={key}
            href={node.href}
            className="text-sky-700 underline underline-offset-2 hover:text-sky-900"
            {...(external
              ? { target: "_blank", rel: "noopener noreferrer" }
              : {})}
          >
            {renderInline(node.children, key, onCitationClick)}
          </a>
        );
      }
      case "citation":
        return (
          <button
            key={key}
            type="button"
            onClick={() => onCitationClick?.(node.sid)}
            aria-label={t("synthesis.openCitation", { sid: node.sid })}
            className="mx-0.5 inline-flex items-center rounded-full border border-sky-300 bg-sky-50 px-1.5 text-[0.75em] font-medium text-sky-800 align-baseline hover:bg-sky-100 focus:outline-none focus:ring-2 focus:ring-sky-400"
          >
            {node.sid}
          </button>
        );
    }
  });
}

function renderBlock(
  block: BlockNode,
  idx: number,
  onCitationClick?: (sid: string) => void,
): ReactNode {
  const key = `b-${idx}`;
  switch (block.t) {
    case "heading": {
      const children = renderInline(block.children, key, onCitationClick);
      const cls = "font-semibold text-slate-900 mt-4 mb-2";
      switch (block.level) {
        case 1:
          return (
            <h3 key={key} className={`text-lg ${cls}`}>
              {children}
            </h3>
          );
        case 2:
          return (
            <h4 key={key} className={`text-base ${cls}`}>
              {children}
            </h4>
          );
        default:
          return (
            <h5 key={key} className={`text-sm ${cls}`}>
              {children}
            </h5>
          );
      }
    }
    case "para":
      return (
        <p key={key} className="my-2 leading-relaxed">
          {renderInline(block.children, key, onCitationClick)}
        </p>
      );
    case "list": {
      const items = block.items.map((item, i) => (
        <li key={`${key}-li-${i}`}>
          {renderInline(item, `${key}-li-${i}`, onCitationClick)}
        </li>
      ));
      return block.ordered ? (
        <ol key={key} className="my-2 list-decimal space-y-1 pl-6">
          {items}
        </ol>
      ) : (
        <ul key={key} className="my-2 list-disc space-y-1 pl-6">
          {items}
        </ul>
      );
    }
    case "codeblock":
      return (
        <pre
          key={key}
          className="my-2 overflow-x-auto rounded bg-slate-900 p-3 text-xs text-slate-100"
        >
          <code>{block.text}</code>
        </pre>
      );
    case "blockquote":
      return (
        <blockquote
          key={key}
          className="my-2 border-l-4 border-slate-300 pl-3 text-slate-600"
        >
          {renderInline(block.children, key, onCitationClick)}
        </blockquote>
      );
    case "hr":
      return <hr key={key} className="my-4 border-slate-200" />;
  }
}

/**
 * Safe markdown renderer: parses to an AST and emits React elements only.
 * Raw HTML in the source is stripped by the parser; no
 * dangerouslySetInnerHTML is used.
 */
export function Markdown({ source, citations, onCitationClick }: MarkdownProps) {
  const blocks = parseMarkdown(source, { citations });
  return (
    <div className="text-sm text-slate-800">
      {blocks.map((b, i) => renderBlock(b, i, onCitationClick))}
    </div>
  );
}
