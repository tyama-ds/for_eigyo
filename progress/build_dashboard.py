#!/usr/bin/env python3
"""開発進捗ダッシュボードを生成する（標準ライブラリのみ）。

    python progress/build_dashboard.py           # 生成（未登録プロジェクトは下書きを自動作成）
    python progress/build_dashboard.py --check   # 検証のみ（CI 用。差分や未登録があれば終了コード 1）

progress/projects/<slug>.json を読み込み、progress/dashboard.html を書き出す。
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
PROJECTS = PROGRESS / "projects"
OUTPUT = PROGRESS / "dashboard.html"

# プロジェクトとして扱わないトップレベルのフォルダ
IGNORED_DIRS = {"progress", "docs", "scripts", "node_modules"}

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


def project_dirs() -> list[str]:
    """README.md を持つトップレベルのフォルダをプロジェクトとみなす。"""
    return sorted(
        p.name for p in ROOT.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name not in IGNORED_DIRS
        and (p / "README.md").exists()
    )


def readme_summary(folder: str) -> str:
    """README の最初の段落を下書きの概要にする。"""
    text = (ROOT / folder / "README.md").read_text(encoding="utf-8")
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if block and not block.startswith(("#", "```", "<", "|", "!", "[")):
            return re.sub(r"\s+", "", block.replace("**", ""))[:160]
    return ""


def scaffold(folder: str, today: str) -> dict:
    return {
        "slug": folder,
        "name": folder,
        "path": f"{folder}/",
        "summary": readme_summary(folder) or "（概要を記入）",
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
    if source != f"{p['slug']}.json":
        errs.append(f"{source}: ファイル名と slug が一致しません")
    return errs


def load(check: bool, today: str) -> tuple[list[dict], list[str]]:
    PROJECTS.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    known = {f.stem for f in PROJECTS.glob("*.json")}
    for folder in project_dirs():
        if folder in known:
            continue
        if check:
            errors.append(f"{folder}/ の進捗ファイル progress/projects/{folder}.json がありません"
                          "（build_dashboard.py を実行すると下書きを作成します）")
        else:
            path = PROJECTS / f"{folder}.json"
            path.write_text(json.dumps(scaffold(folder, today), ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
            print(f"新しいプロジェクトを登録しました: {path.relative_to(ROOT)}")
    projects = []
    for f in sorted(PROJECTS.glob("*.json")):
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{f.name}: JSON として読めません ({exc})")
            continue
        errs = validate(p, f.name)
        errors.extend(errs)
        if not errs:
            p["log"] = sorted(p["log"], key=lambda e: e["date"], reverse=True)
            projects.append(p)
    # 開発中を先頭に、同じ状態の中では更新が新しい順
    order = ["active", "planning", "maintenance", "paused", "done"]
    projects.sort(key=lambda p: (p["updated"], p["slug"]), reverse=True)
    projects.sort(key=lambda p: order.index(p["status"]))
    return projects, errors


def render(projects: list[dict]) -> str:
    as_of = max((p["updated"] for p in projects), default="")
    data = json.dumps({"asOf": as_of, "staleDays": STALE_DAYS, "statuses": STATUSES,
                       "projects": projects}, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    template = (PROGRESS / "dashboard_template.html").read_text(encoding="utf-8")
    return template.replace("__DATA__", data)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="書き込まずに検証する")
    args = ap.parse_args()
    today = dt.date.today().isoformat()
    projects, errors = load(args.check, today)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    html = render(projects)
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != html:
            print("progress/dashboard.html が最新ではありません。"
                  "python progress/build_dashboard.py を実行してコミットしてください。", file=sys.stderr)
            return 1
        print(f"OK: {len(projects)} プロジェクト")
        return 0
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"{OUTPUT.relative_to(ROOT)} を書き出しました（{len(projects)} プロジェクト）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
