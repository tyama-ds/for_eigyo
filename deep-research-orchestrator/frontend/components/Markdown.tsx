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
          <strong key={key} className="font-semibold text-white">
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
            className="rounded-md bg-white/10 px-1.5 py-0.5 font-mono text-[0.85em] text-fuchsia-200 ring-1 ring-inset ring-white/10"
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
            className="text-indigo-300 underline decoration-indigo-400/40 underline-offset-2 transition-colors duration-200 hover:text-indigo-200 hover:decoration-indigo-300"
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
            className="mx-0.5 inline-flex items-center rounded-full bg-gradient-to-r from-indigo-500/20 to-fuchsia-500/20 px-1.5 align-baseline text-[0.75em] font-medium text-indigo-200 ring-1 ring-inset ring-indigo-400/40 transition-all duration-200 hover:from-indigo-500/40 hover:to-fuchsia-500/40 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-300"
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
      const cls = "font-semibold tracking-tight text-white mt-5 mb-2";
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
        <p key={key} className="my-2.5 leading-7">
          {renderInline(block.children, key, onCitationClick)}
        </p>
      );
    case "list": {
      const items = block.items.map((item, i) => (
        <li key={`${key}-li-${i}`} className="marker:text-indigo-400/70">
          {renderInline(item, `${key}-li-${i}`, onCitationClick)}
        </li>
      ));
      return block.ordered ? (
        <ol key={key} className="my-2.5 list-decimal space-y-1.5 pl-6">
          {items}
        </ol>
      ) : (
        <ul key={key} className="my-2.5 list-disc space-y-1.5 pl-6">
          {items}
        </ul>
      );
    }
    case "codeblock":
      return (
        <pre
          key={key}
          className="my-3 overflow-x-auto rounded-xl bg-black/50 p-4 font-mono text-xs leading-relaxed text-emerald-200/90 ring-1 ring-inset ring-white/10"
        >
          <code>{block.text}</code>
        </pre>
      );
    case "blockquote":
      return (
        <blockquote
          key={key}
          className="my-3 border-l-2 border-fuchsia-400/50 pl-4 italic text-slate-400"
        >
          {renderInline(block.children, key, onCitationClick)}
        </blockquote>
      );
    case "hr":
      return (
        <hr
          key={key}
          className="my-5 border-0 border-t border-white/10"
        />
      );
  }
}

/**
 * Safe markdown renderer: parses to an AST and emits React elements only.
 * Raw HTML in the source is stripped by the parser; no
 * dangerouslySetInnerHTML is used. Hand-rolled dark "prose" styling.
 */
export function Markdown({ source, citations, onCitationClick }: MarkdownProps) {
  const blocks = parseMarkdown(source, { citations });
  return (
    <div className="text-sm text-slate-300">
      {blocks.map((b, i) => renderBlock(b, i, onCitationClick))}
    </div>
  );
}
