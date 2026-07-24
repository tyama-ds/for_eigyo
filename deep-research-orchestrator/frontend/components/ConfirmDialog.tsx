"use client";

import { useEffect, useRef } from "react";
import { t } from "@/lib/i18n";
import { Icon } from "./Icon";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const cancelRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (open) cancelRef.current?.focus();
  }, [open]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 p-4 backdrop-blur-sm"
      onKeyDown={(e) => {
        if (e.key === "Escape") onCancel();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="w-full max-w-md rounded-2xl bg-slate-900/95 p-5 shadow-2xl shadow-black/50 ring-1 ring-white/10 backdrop-blur"
      >
        <h3 className="mb-2 flex items-center gap-2 text-base font-semibold tracking-tight text-white">
          <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-amber-500/15 ring-1 ring-amber-400/30">
            <Icon name="warn" className="h-4 w-4 text-amber-400" />
          </span>
          {title}
        </h3>
        <p className="mb-5 text-sm leading-relaxed text-slate-300">{message}</p>
        <div className="flex justify-end gap-2">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            className="rounded-xl bg-white/5 px-3.5 py-2 text-sm text-slate-200 ring-1 ring-white/15 transition-all duration-200 hover:bg-white/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="rounded-xl bg-gradient-to-r from-indigo-500 via-violet-500 to-fuchsia-500 px-3.5 py-2 text-sm font-medium text-white shadow-lg shadow-indigo-500/25 transition-all duration-200 hover:brightness-110 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-300"
          >
            {confirmLabel ?? t("common.confirm")}
          </button>
        </div>
      </div>
    </div>
  );
}
