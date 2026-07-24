import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { t } from "@/lib/i18n";

export const metadata: Metadata = {
  title: t("app.title"),
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ja">
      <body className="min-h-screen">
        <header className="sticky top-0 z-40 border-b border-white/10 bg-slate-950/70 backdrop-blur-xl">
          <div className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-3">
            <div className="flex items-center gap-2.5">
              <span
                aria-hidden="true"
                className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-indigo-500 via-violet-500 to-fuchsia-500 text-white shadow-lg shadow-indigo-500/30 ring-1 ring-white/20"
              >
                <svg
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="h-4.5 w-4.5 p-0.5"
                >
                  <circle cx="10.5" cy="10.5" r="6" />
                  <path d="M15 15l5.5 5.5" />
                  <path d="M8 10.5h5M10.5 8v5" />
                </svg>
              </span>
              <span className="bg-gradient-to-r from-indigo-300 via-violet-300 to-fuchsia-300 bg-clip-text text-sm font-semibold tracking-tight text-transparent">
                {t("app.title")}
              </span>
            </div>
            <nav
              aria-label={t("app.title")}
              className="flex items-center gap-1 text-sm"
            >
              <Link
                href="/"
                className="rounded-lg px-3 py-1.5 text-slate-300 transition-all duration-200 hover:bg-white/5 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
              >
                {t("app.nav.console")}
              </Link>
              <Link
                href="/settings"
                className="rounded-lg px-3 py-1.5 text-slate-300 transition-all duration-200 hover:bg-white/5 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
              >
                {t("app.nav.settings")}
              </Link>
            </nav>
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
