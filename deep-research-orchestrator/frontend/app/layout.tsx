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
        <header className="border-b border-slate-200 bg-white">
          <div className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-3">
            <div className="text-sm font-bold tracking-tight text-slate-900">
              {t("app.title")}
            </div>
            <nav
              aria-label={t("app.title")}
              className="flex items-center gap-4 text-sm"
            >
              <Link
                href="/"
                className="text-slate-700 hover:text-slate-900 hover:underline"
              >
                {t("app.nav.console")}
              </Link>
              <Link
                href="/settings"
                className="text-slate-700 hover:text-slate-900 hover:underline"
              >
                {t("app.nav.settings")}
              </Link>
            </nav>
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-4 py-4">{children}</main>
      </body>
    </html>
  );
}
