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

/** Accessible tablist with arrow-key navigation. */
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
      className="flex flex-wrap gap-1 border-b border-slate-200"
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
            className={`rounded-t px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-sky-500 ${
              selected
                ? "border border-b-0 border-slate-200 bg-white font-semibold text-slate-900"
                : "text-slate-600 hover:text-slate-900"
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
      className="rounded-b-lg border border-t-0 border-slate-200 bg-white p-4 focus:outline-none"
    >
      {children}
    </div>
  );
}
