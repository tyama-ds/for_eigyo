import { t, type MessageKey } from "@/lib/i18n";
import { Icon, type IconName } from "./Icon";

interface StatusDef {
  icon: IconName;
  labelKey: MessageKey;
  className: string;
  spin?: boolean;
}

const STATUS_DEFS: Record<string, StatusDef> = {
  queued: {
    icon: "clock",
    labelKey: "status.queued",
    className: "bg-slate-100 text-slate-700 border-slate-300",
  },
  pending: {
    icon: "clock",
    labelKey: "status.pending",
    className: "bg-slate-100 text-slate-700 border-slate-300",
  },
  starting: {
    icon: "play",
    labelKey: "status.starting",
    className: "bg-sky-50 text-sky-800 border-sky-300",
  },
  running: {
    icon: "spinner",
    labelKey: "status.running",
    className: "bg-sky-50 text-sky-800 border-sky-300",
    spin: true,
  },
  researching: {
    icon: "search",
    labelKey: "status.researching",
    className: "bg-sky-50 text-sky-800 border-sky-300",
  },
  normalizing: {
    icon: "layers",
    labelKey: "status.normalizing",
    className: "bg-indigo-50 text-indigo-800 border-indigo-300",
  },
  succeeded: {
    icon: "check",
    labelKey: "status.succeeded",
    className: "bg-emerald-50 text-emerald-800 border-emerald-300",
  },
  failed: {
    icon: "x",
    labelKey: "status.failed",
    className: "bg-rose-50 text-rose-800 border-rose-300",
  },
  timed_out: {
    icon: "timer",
    labelKey: "status.timed_out",
    className: "bg-amber-50 text-amber-800 border-amber-300",
  },
  cancelled: {
    icon: "ban",
    labelKey: "status.cancelled",
    className: "bg-slate-100 text-slate-600 border-slate-300",
  },
  partial: {
    icon: "warn",
    labelKey: "status.partial",
    className: "bg-amber-50 text-amber-800 border-amber-300",
  },
  unavailable: {
    icon: "ban",
    labelKey: "status.unavailable",
    className: "bg-slate-100 text-slate-600 border-slate-300",
  },
  skipped: {
    icon: "ban",
    labelKey: "status.skipped",
    className: "bg-slate-100 text-slate-600 border-slate-300",
  },
};

/**
 * Status badge: icon + Japanese text label (status is never conveyed by
 * color alone). Unknown statuses fall back to a neutral badge with the
 * raw status text.
 */
export function StatusBadge({ status }: { status: string }) {
  const def = STATUS_DEFS[status];
  const label = def ? t(def.labelKey) : status;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-xs font-medium ${
        def?.className ?? "bg-slate-100 text-slate-700 border-slate-300"
      }`}
    >
      <Icon name={def?.icon ?? "info"} className="h-3.5 w-3.5" spin={def?.spin} />
      <span>{label}</span>
    </span>
  );
}
