#!/usr/bin/env python3
"""開発進捗ダッシュボードを生成する（標準ライブラリのみ）。

    python progress/build_dashboard.py              # 生成（未登録プロジェクトは下書きを自動作成）
    python progress/build_dashboard.py --check      # 検証のみ。差分や未登録があれば終了コード 1
    python progress/build_dashboard.py --check --data-only   # 進捗ファイルだけを検証（CI 用）
    python progress/build_dashboard.py --source claudecode=/path/to/claudecode

対象リポジトリは progress/sources.json に書く。各リポジトリの progress/projects/<slug>.json を
読み込み、for_eigyo の progress/dashboard.html に1枚のダッシュボードとして書き出す。
dashboard.html はそのままブラウザで開け、claude.ai の Artifact としても公開できる。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROGRESS = ROOT / "progress"
SOURCES = PROGRESS / "sources.json"
TEMPLATE = PROGRESS / "dashboard_template.html"
OUTPUT = PROGRESS / "dashboard.html"

STATUSES = {
    "planning": "計画中",
    "active": "開発中",
    "maintenance": "保守",
    "paused": "停止中",
    "done": "完了",
}
MILESTONE_STATES = {"done", "doing", "todo"}
REQUIRED = ["slug", "name", "path", "summary", "status", "phase", "progress",
            "started", "updated", "milestones", "next", "risks", "log"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STALE_DAYS = 30


def load_sources(overrides: dict[str, str]) -> list[dict]:
    sources = json.loads(SOURCES.read_text(encoding="utf-8"))
    for s in sources:
        raw = overrides.get(s["repo"], s["path"])
        s["root"] = (ROOT / raw).resolve()
    return sources


def project_dirs(source: dict) -> list[str]:
    """隠しフォルダと ignore に挙げたもの以外のトップレベルのフォルダをプロジェクトとみなす。"""
    ignore = set(source.get("ignore", [])) | {"progress", "node_modules", "__pycache__"}
    return sorted(
        p.name for p in source["root"].iterdir()
        if p.is_dir() and not p.name.startswith((".", "_")) and p.name not in ignore
    )


def readme_summary(folder: Path) -> str:
    """README の最初の段落を下書きの概要にする。"""
    for name in ("README.ja.md", "README.md"):
        f = folder / name
        if not f.exists():
            continue
        for block in re.split(r"\n\s*\n", f.read_text(encoding="utf-8")):
            block = block.strip()
            if block and not block.startswith(("#", "```", "<", "|", "!", "[", ">")):
                return re.sub(r"\s+", "", block.replace("**", ""))[:160]
    return ""


def scaffold(source: dict, folder: str, today: str) -> dict:
    return {
        "slug": folder,
        "name": folder,
        "path": f"{folder}/",
        "summary": readme_summary(source["root"] / folder) or "（概要を記入）",
        "status": "planning",
        "phase": "ダッシュボードに自動登録（内容を記入してください）",
        "progress": 0,
        "started": today,
        "updated": today,
        "ports": [],
        "ci": False,
        "portal": False,
        "stack": [],
        "milestones": [],
        "next": [],
        "risks": [],
        "log": [{"date": today, "summary": "ダッシュボードに自動登録"}],
    }


def validate(p: dict, source: str) -> list[str]:
    errs = [f"{source}: '{k}' がありません" for k in REQUIRED if k not in p]
    if errs:
        return errs
    if p["status"] not in STATUSES:
        errs.append(f"{source}: status は {sorted(STATUSES)} のいずれか")
    if not isinstance(p["progress"], int) or not 0 <= p["progress"] <= 100:
        errs.append(f"{source}: progress は 0〜100 の整数")
    for k in ("started", "updated"):
        if not DATE_RE.match(str(p[k])):
            errs.append(f"{source}: {k} は YYYY-MM-DD")
    for m in p["milestones"]:
        if m.get("state") not in MILESTONE_STATES or not m.get("title"):
            errs.append(f"{source}: milestones の各要素に title と state（done/doing/todo）が必要")
    for e in p["log"]:
        if not DATE_RE.match(str(e.get("date", ""))) or not e.get("summary"):
            errs.append(f"{source}: log の各要素に date（YYYY-MM-DD）と summary が必要")
    if p["log"] and p["updated"] < max(e["date"] for e in p["log"]):
        errs.append(f"{source}: updated が最新の log の日付より古い")
    if not source.endswith(f"/{p['slug']}.json"):
        errs.append(f"{source}: ファイル名と slug が一致しません")
    return errs


def load_repo(source: dict, check: bool, today: str) -> tuple[list[dict], list[str]]:
    repo = source["repo"]
    projects_dir = source["root"] / "progress" / "projects"
    errors: list[str] = []
    known = {f.stem for f in projects_dir.glob("*.json")}
    for folder in project_dirs(source):
        if folder in known:
            continue
        rel = f"{repo}/progress/projects/{folder}.json"
        if check:
            errors.append(f"{repo}/{folder}/ の進捗ファイル {rel} がありません"
                          "（build_dashboard.py を実行すると下書きを作成します）")
        else:
            projects_dir.mkdir(parents=True, exist_ok=True)
            (projects_dir / f"{folder}.json").write_text(
                json.dumps(scaffold(source, folder, today), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"新しいプロジェクトを登録しました: {rel}（{repo} リポジトリでコミットしてください）")
    projects = []
    for f in sorted(projects_dir.glob("*.json")):
        name = f"{repo}/progress/projects/{f.name}"
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{name}: JSON として読めません ({exc})")
            continue
        errs = validate(p, name)
        errors.extend(errs)
        if not errs:
            p["repo"] = repo
            p["log"] = sorted(p["log"], key=lambda e: e["date"], reverse=True)
            projects.append(p)
    return projects, errors


def load(sources: list[dict], check: bool, allow_missing: bool, today: str):
    projects: list[dict] = []
    errors: list[str] = []
    missing: list[str] = []
    for s in sources:
        if not s["root"].is_dir():
            msg = (f"{s['repo']} が見つかりません: {s['root']}（{s.get('github', '')} を clone するか、"
                   f"--source {s['repo']}=<パス> で場所を指定してください）")
            if allow_missing:
                print("注意: " + msg, file=sys.stderr)
                missing.append(s["repo"])
                continue
            errors.append(msg)
            continue
        ps, es = load_repo(s, check, today)
        projects += ps
        errors += es
    # 開発中を先頭に、同じ状態の中では更新が新しい順
    order = ["active", "planning", "maintenance", "paused", "done"]
    projects.sort(key=lambda p: (p["updated"], p["repo"], p["slug"]), reverse=True)
    projects.sort(key=lambda p: order.index(p["status"]))
    return projects, errors, missing


def render(sources: list[dict], projects: list[dict]) -> str:
    as_of = max((p["updated"] for p in projects), default="")
    repos = [{"repo": s["repo"], "github": s.get("github", "")} for s in sources
             if any(p["repo"] == s["repo"] for p in projects)]
    data = json.dumps({"asOf": as_of, "staleDays": STALE_DAYS, "statuses": STATUSES,
                       "repos": repos, "projects": projects}, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", data)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="書き込まずに検証する")
    ap.add_argument("--data-only", action="store_true", help="--check で dashboard.html の差分を確認しない")
    ap.add_argument("--allow-missing", action="store_true", help="見つからないリポジトリを飛ばす")
    ap.add_argument("--source", action="append", default=[], metavar="REPO=PATH",
                    help="sources.json のパスを上書きする（for_eigyo からの相対パスまたは絶対パス）")
    ap.add_argument("--out", type=Path, default=OUTPUT, help="書き出し先（既定: progress/dashboard.html）")
    args = ap.parse_args()
    overrides = dict(s.split("=", 1) for s in args.source)
    sources = load_sources(overrides)
    today = dt.date.today().isoformat()
    projects, errors, missing = load(sources, args.check, args.allow_missing, today)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    summary = f"{len(projects)} プロジェクト / " + ", ".join(
        f"{s['repo']} {sum(p['repo'] == s['repo'] for p in projects)}" for s in sources if s["repo"] not in missing)
    if args.check:
        if not args.data_only:
            html = render(sources, projects)
            current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
            if current != html:
                print("progress/dashboard.html が最新ではありません。"
                      "python progress/build_dashboard.py を実行してコミットしてください。", file=sys.stderr)
                return 1
        print(f"OK: {summary}")
        return 0
    if missing and args.out.resolve() == OUTPUT.resolve():
        print("見つからないリポジトリがあるため progress/dashboard.html は更新しません"
              "（--out で別の場所を指定すると書き出せます）", file=sys.stderr)
        return 1
    args.out.write_text(render(sources, projects), encoding="utf-8")
    print(f"{args.out} を書き出しました（{summary}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
