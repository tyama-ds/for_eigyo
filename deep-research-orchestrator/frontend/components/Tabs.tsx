"use client";

import { useRef } from "react";

export interface TabDef {
  id: string;
  label: string;
}

interface TabsProps {
  tabs: TabDef[];
  active: string;
  onChange: (id: string) => void;
  ariaLabel: string;
}

/** Accessible pill-style segmented tablist with arrow-key navigation. */
export function Tabs({ tabs, active, onChange, ariaLabel }: TabsProps) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});

  const onKeyDown = (e: React.KeyboardEvent) => {
    const idx = tabs.findIndex((tab) => tab.id === active);
    let nextIdx: number | null = null;
    if (e.key === "ArrowRight") nextIdx = (idx + 1) % tabs.length;
    else if (e.key === "ArrowLeft") nextIdx = (idx - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") nextIdx = 0;
    else if (e.key === "End") nextIdx = tabs.length - 1;
    if (nextIdx !== null) {
      e.preventDefault();
      const next = tabs[nextIdx];
      onChange(next.id);
      refs.current[next.id]?.focus();
    }
  };

  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      className="inline-flex max-w-full flex-wrap gap-1 rounded-2xl bg-white/5 p-1 ring-1 ring-white/10 backdrop-blur"
      onKeyDown={onKeyDown}
    >
      {tabs.map((tab) => {
        const selected = tab.id === active;
        return (
          <button
            key={tab.id}
            ref={(el) => {
              refs.current[tab.id] = el;
            }}
            role="tab"
            type="button"
            id={`tab-${tab.id}`}
            aria-selected={selected}
            aria-controls={`panel-${tab.id}`}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(tab.id)}
            className={`rounded-xl px-3.5 py-1.5 text-sm transition-all duration-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 ${
              selected
                ? "bg-gradient-to-r from-indigo-500 via-violet-500 to-fuchsia-500 font-semibold text-white shadow-lg shadow-indigo-500/25"
                : "text-slate-400 hover:bg-white/5 hover:text-slate-100"
            }`}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({
  id,
  active,
  children,
}: {
  id: string;
  active: string;
  children: React.ReactNode;
}) {
  if (id !== active) return null;
  return (
    <div
      role="tabpanel"
      id={`panel-${id}`}
      aria-labelledby={`tab-${id}`}
      tabIndex={0}
      className="mt-3 rounded-2xl bg-slate-900/80 p-5 shadow-xl shadow-black/20 ring-1 ring-white/10 backdrop-blur focus:outline-none focus-visible:ring-indigo-400"
    >
      {children}
    </div>
  );
}
