/**
 * Per-engine visual identity: inline SVG icon (currentColor, no external
 * deps), accent gradient classes and a tagline i18n key. Unknown engines get
 * a neutral fallback.
 */

import type { ReactNode } from "react";
import type { MessageKey } from "./i18n";

export interface EngineMeta {
  /** Inline SVG drawn with currentColor. */
  icon: ReactNode;
  /** Complete Tailwind gradient stops (JIT-safe full class names). */
  accent: string;
  taglineKey: MessageKey;
}

function svg(children: ReactNode): ReactNode {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-full w-full"
    >
      {children}
    </svg>
  );
}

/* Magnifier + spark — autonomous exploration */
const iconResearcher = svg(
  <>
    <circle cx="10.5" cy="10.5" r="6" />
    <path d="M15 15l5.5 5.5" />
    <path d="M19 3.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z" />
  </>,
);

/* Network graph nodes */
const iconGraph = svg(
  <>
    <circle cx="5.5" cy="6" r="2.2" />
    <circle cx="18.5" cy="5.5" r="2.2" />
    <circle cx="12" cy="18" r="2.2" />
    <path d="M7.5 7.2l8.8-1.2" />
    <path d="M6.5 8l4.4 8" />
    <path d="M17.5 7.5L13 16" />
  </>,
);

/* Lightning bolt */
const iconBolt = svg(<path d="M13 2L5 13.5h5.5L10 22l8-11.5h-5.5z" />);

/* Tortoise (slow) */
const iconTortoise = svg(
  <>
    <path d="M5 14a7 6 0 0 1 14 0" />
    <path d="M3.5 14h17" />
    <path d="M6.5 14v2.5M17.5 14v2.5" />
    <path d="M19 11.5c1.5-.5 2.5.2 2.5 1.5" />
    <path d="M9 8.5l2 3M13 8l-1 3.5" />
  </>,
);

/* Cracked warning triangle */
const iconBrokenWarn = svg(
  <>
    <path d="M12 3L2.5 20h19z" />
    <path d="M12 9l-1.2 3 2.4 1.5L12 17" />
  </>,
);

/* Half-missing pie chart */
const iconHalfPie = svg(
  <>
    <path d="M12 3a9 9 0 1 0 9 9" />
    <path d="M12 3v9h9" strokeDasharray="2.5 2.5" />
  </>,
);

/* Hourglass */
const iconHourglass = svg(
  <>
    <path d="M6.5 3h11" />
    <path d="M6.5 21h11" />
    <path d="M7.5 3c0 5 4.5 5.5 4.5 9s-4.5 4-4.5 9" />
    <path d="M16.5 3c0 5-4.5 5.5-4.5 9s4.5 4 4.5 9" />
  </>,
);

/* Stop octagon */
const iconStop = svg(
  <>
    <path d="M8.2 3h7.6L21 8.2v7.6L15.8 21H8.2L3 15.8V8.2z" />
    <path d="M9 12h6" />
  </>,
);

/* Fallback: compass */
const iconCompass = svg(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="M15.5 8.5l-2 5-5 2 2-5z" />
  </>,
);

const REGISTRY: Record<string, EngineMeta> = {
  "gpt-researcher": {
    icon: iconResearcher,
    accent: "from-emerald-500 to-teal-500",
    taglineKey: "engine.tagline.gpt-researcher",
  },
  "open-deep-research": {
    icon: iconGraph,
    accent: "from-sky-500 to-indigo-500",
    taglineKey: "engine.tagline.open-deep-research",
  },
  "mock-fast": {
    icon: iconBolt,
    accent: "from-amber-500 to-orange-500",
    taglineKey: "engine.tagline.mock-fast",
  },
  "mock-slow": {
    icon: iconTortoise,
    accent: "from-violet-500 to-purple-500",
    taglineKey: "engine.tagline.mock-slow",
  },
  "mock-fail": {
    icon: iconBrokenWarn,
    accent: "from-rose-500 to-red-500",
    taglineKey: "engine.tagline.mock-fail",
  },
  "mock-partial": {
    icon: iconHalfPie,
    accent: "from-cyan-500 to-blue-500",
    taglineKey: "engine.tagline.mock-partial",
  },
  "mock-timeout": {
    icon: iconHourglass,
    accent: "from-amber-400 to-yellow-500",
    taglineKey: "engine.tagline.mock-timeout",
  },
  "mock-cancellable": {
    icon: iconStop,
    accent: "from-slate-500 to-zinc-500",
    taglineKey: "engine.tagline.mock-cancellable",
  },
};

const FALLBACK: EngineMeta = {
  icon: iconCompass,
  accent: "from-indigo-500 to-fuchsia-500",
  taglineKey: "engine.tagline.unknown",
};

export function getEngineMeta(engineId: string): EngineMeta {
  return REGISTRY[engineId] ?? FALLBACK;
}

/** Rounded-square gradient avatar with the engine icon in white. */
export function EngineAvatar({
  engineId,
  size = "h-9 w-9",
}: {
  engineId: string;
  size?: string;
}) {
  const meta = getEngineMeta(engineId);
  return (
    <span
      aria-hidden="true"
      className={`inline-flex ${size} shrink-0 items-center justify-center rounded-xl bg-gradient-to-br ${meta.accent} p-1.5 text-white shadow-lg shadow-black/30 ring-1 ring-white/20`}
    >
      {meta.icon}
    </span>
  );
}
