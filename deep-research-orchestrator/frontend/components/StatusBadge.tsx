import { t, type MessageKey } from "@/lib/i18n";
import { Icon, type IconName } from "./Icon";

interface StatusDef {
  icon: IconName;
  labelKey: MessageKey;
  className: string;
  dotClassName: string;
  spin?: boolean;
  pulse?: boolean;
}

const ACTIVE_PILL =
  "bg-indigo-500/15 text-indigo-300 ring-1 ring-inset ring-indigo-400/30";
const NEUTRAL_PILL =
  "bg-white/5 text-slate-300 ring-1 ring-inset ring-white/15";

const STATUS_DEFS: Record<string, StatusDef> = {
  queued: {
    icon: "clock",
    labelKey: "status.queued",
    className: NEUTRAL_PILL,
    dotClassName: "bg-slate-400",
  },
  pending: {
    icon: "clock",
    labelKey: "status.pending",
    className: NEUTRAL_PILL,
    dotClassName: "bg-slate-400",
  },
  starting: {
    icon: "play",
    labelKey: "status.starting",
    className: ACTIVE_PILL,
    dotClassName: "bg-indigo-400",
    pulse: true,
  },
  running: {
    icon: "spinner",
    labelKey: "status.running",
    className: ACTIVE_PILL,
    dotClassName: "bg-indigo-400",
    spin: true,
    pulse: true,
  },
  researching: {
    icon: "search",
    labelKey: "status.researching",
    className: ACTIVE_PILL,
    dotClassName: "bg-indigo-400",
    pulse: true,
  },
  normalizing: {
    icon: "layers",
    labelKey: "status.normalizing",
    className:
      "bg-violet-500/15 text-violet-300 ring-1 ring-inset ring-violet-400/30",
    dotClassName: "bg-violet-400",
    pulse: true,
  },
  succeeded: {
    icon: "check",
    labelKey: "status.succeeded",
    className:
      "bg-emerald-500/15 text-emerald-300 ring-1 ring-inset ring-emerald-400/30",
    dotClassName: "bg-emerald-400",
  },
  failed: {
    icon: "x",
    labelKey: "status.failed",
    className: "bg-rose-500/15 text-rose-300 ring-1 ring-inset ring-rose-400/30",
    dotClassName: "bg-rose-400",
  },
  timed_out: {
    icon: "timer",
    labelKey: "status.timed_out",
    className:
      "bg-amber-500/15 text-amber-300 ring-1 ring-inset ring-amber-400/30",
    dotClassName: "bg-amber-400",
  },
  cancelled: {
    icon: "ban",
    labelKey: "status.cancelled",
    className: NEUTRAL_PILL,
    dotClassName: "bg-slate-400",
  },
  partial: {
    icon: "warn",
    labelKey: "status.partial",
    className:
      "bg-amber-500/15 text-amber-300 ring-1 ring-inset ring-amber-400/30",
    dotClassName: "bg-amber-400",
  },
  unavailable: {
    icon: "ban",
    labelKey: "status.unavailable",
    className: NEUTRAL_PILL,
    dotClassName: "bg-slate-400",
  },
  skipped: {
    icon: "ban",
    labelKey: "status.skipped",
    className: NEUTRAL_PILL,
    dotClassName: "bg-slate-400",
  },
};

/**
 * Pill-shaped status badge: pulse dot + icon + Japanese text label (status is
 * never conveyed by color alone). Unknown statuses fall back to a neutral
 * pill with the raw status text.
 */
export function StatusBadge({ status }: { status: string }) {
  const def = STATUS_DEFS[status];
  const label = def ? t(def.labelKey) : status;
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${
        def?.className ?? NEUTRAL_PILL
      }`}
    >
      <span
        aria-hidden="true"
        className={`h-1.5 w-1.5 rounded-full ${def?.dotClassName ?? "bg-slate-400"}${
          def?.pulse ? " animate-pulse" : ""
        }`}
      />
      <Icon name={def?.icon ?? "info"} className="h-3.5 w-3.5" spin={def?.spin} />
      <span>{label}</span>
    </span>
  );
}
