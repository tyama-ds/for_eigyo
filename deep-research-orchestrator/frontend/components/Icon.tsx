import type { JSX } from "react";

export type IconName =
  | "clock"
  | "play"
  | "search"
  | "layers"
  | "check"
  | "x"
  | "timer"
  | "ban"
  | "warn"
  | "spinner"
  | "doc"
  | "link"
  | "info";

const PATHS: Record<IconName, JSX.Element> = {
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 3" />
    </>
  ),
  play: <path d="M8 5.5v13l11-6.5z" />,
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="M21 21l-4.35-4.35" />
    </>
  ),
  layers: (
    <>
      <path d="M12 3l9 5-9 5-9-5z" />
      <path d="M3 13l9 5 9-5" />
    </>
  ),
  check: <path d="M4 12.5l5 5L20 6.5" />,
  x: (
    <>
      <path d="M6 6l12 12" />
      <path d="M18 6L6 18" />
    </>
  ),
  timer: (
    <>
      <circle cx="12" cy="13" r="8" />
      <path d="M12 9v4" />
      <path d="M9 2h6" />
    </>
  ),
  ban: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M5.5 5.5l13 13" />
    </>
  ),
  warn: (
    <>
      <path d="M12 3L2 20h20z" />
      <path d="M12 10v4" />
      <path d="M12 17.5v.5" />
    </>
  ),
  spinner: <path d="M12 3a9 9 0 1 0 9 9" />,
  doc: (
    <>
      <path d="M6 2h9l4 4v16H6z" />
      <path d="M15 2v4h4" />
    </>
  ),
  link: (
    <>
      <path d="M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1.5 1.5" />
      <path d="M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7L12.5 18.5" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5" />
      <path d="M12 7.5v.5" />
    </>
  ),
};

export function Icon({
  name,
  className = "h-4 w-4",
  spin = false,
}: {
  name: IconName;
  className?: string;
  spin?: boolean;
}) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${className}${spin ? " animate-spin" : ""} shrink-0`}
    >
      {PATHS[name]}
    </svg>
  );
}
