"""コマンドライン: python -m pqb <command>（企画書 §8.2: UI と同じ操作を CLI でも）。

    python -m pqb serve --port 8740 --open       # Web UI
    python -m pqb demo                            # 同梱サンプルを記録済み判断でエンドツーエンド実行
    python -m pqb demo --auto                     # 同上を Policy 決定者（案A）で
    python -m pqb new --name 案件名 --purpose prior_art --input 説明.txt --seeds JP2020-000001A ...
    python -m pqb structure CASE / g1 CASE --accept / expand CASE / g2 CASE --adopt-known / build CASE
    python -m pqb import CASE --variant standard --csv hits.csv --hits 1234
    python -m pqb score CASE / analyze CASE / g4 CASE --action iterate|finalize
    python -m pqb report CASE --format md -o report.md / excel CASE -o design.xlsx / excel-import CASE design.xlsx
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import config as cfgmod
from .gates.human import ScriptedHumanDecider
from .llm.adapter import LLMError, PendingConfirmation, PendingManualResponse
from .orchestrator import Orchestrator, OrchestratorError
from .store.db import Store


def _orc(args) -> Orchestrator:
    db = Path(args.db) if args.db else cfgmod.data_dir() / "pqb.sqlite"
    return Orchestrator(Store(db))


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def _run(fn):
    try:
        return fn()
    except PendingManualResponse as e:
        print("LLM 返答待ち（manual モード）。以下のプロンプトを LLM 画面に貼り、返答 JSON を prompts_in/ に保存するか "
              "`python -m pqb manual CASE --key <call_key> --file 返答.json` で渡してください。")
        for p in e.prompts:
            print(f"--- call_key: {p['call_key']} ({p['prompt_id']} #{p['sample_no']})")
        out_dir = cfgmod.data_dir() / "prompts_out"
        print(f"プロンプトは {out_dir} に書き出されています。")
        sys.exit(3)
    except PendingConfirmation as e:
        print(f"production=true のため外部送信前の確認が必要です（{e.prompt_id}）。--confirm を付けて再実行してください。")
        print(e.prompt_text[:2000])
        sys.exit(4)
    except (OrchestratorError, LLMError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="pqb", description="Patent Query Builder")
    ap.add_argument("--db", help="SQLite ファイル（既定: data/pqb.sqlite）")
    ap.add_argument("--version", action="version", version=f"pqb {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="Web UI サーバを起動")
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--open", action="store_true")

    sp = sub.add_parser("demo", help="同梱サンプルでエンドツーエンド実行")
    sp.add_argument("--auto", action="store_true", help="Policy 決定者（案A）で回す")
    sp.add_argument("--max-iterations", type=int, default=3)
    sp.add_argument("-o", "--out", help="レポート出力先（.md / .html）")

    sp = sub.add_parser("cases", help="案件一覧")
    sp = sub.add_parser("show", help="案件の状態"); sp.add_argument("case")
    sp = sub.add_parser("delete", help="案件を削除"); sp.add_argument("case")

    sp = sub.add_parser("new", help="案件を登録")
    sp.add_argument("--name", required=True)
    sp.add_argument("--purpose", default="prior_art", choices=sorted(cfgmod.load_purposes()))
    sp.add_argument("--input", required=True, help="技術説明のテキストファイル")
    sp.add_argument("--seeds", nargs="*", default=[], help="既知文献番号、または seeds.json")
    sp.add_argument("--date-from", default="")
    sp.add_argument("--date-to", default="")
    sp.add_argument("--mask", nargs="*", default=[], help="マスキング語（例: 社名:○○製鉄）")

    for name, helptext in (("structure", "P1 構造化 → G1 待ち"), ("expand", "P2/P3 展開 → G2 待ち"), ("build", "3 案を組み立て"),
                           ("score", "P4 採点（上位K＋標本）"), ("analyze", "RSJ・変換候補・推定 → G4 待ち"), ("run-local", "local_index で 3 案を実行")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("case")
        sp.add_argument("--confirm", action="store_true")

    sp = sub.add_parser("g1", help="観点を確定"); sp.add_argument("case")
    sp.add_argument("--axes", help="観点 JSON（省略時 --accept）"); sp.add_argument("--accept", action="store_true")
    sp = sub.add_parser("g2", help="候補の採否"); sp.add_argument("case")
    sp.add_argument("--decisions", help="[{candidate_id,status,reason_code}] の JSON")
    sp.add_argument("--adopt-known", action="store_true", help="語は入力由来、コードは辞書照合済みを採用（既定）")
    sp.add_argument("--adopt-all", action="store_true")
    sp = sub.add_parser("import", help="DB 実行結果 CSV を取り込む"); sp.add_argument("case")
    sp.add_argument("--variant", default="standard", choices=["broad", "standard", "narrow"])
    sp.add_argument("--csv", required=True); sp.add_argument("--hits", type=int)
    sp = sub.add_parser("g4", help="確定または再反復"); sp.add_argument("case")
    sp.add_argument("--action", default="finalize", choices=["finalize", "iterate"])
    sp.add_argument("--transforms", help="[{transform_id,status}] の JSON")
    sp.add_argument("--judgments", help="[{doc_id,overall,per_axis}] の JSON")
    sp.add_argument("--confirm", action="store_true")
    sp = sub.add_parser("auto", help="Policy 決定者で回す（案A）"); sp.add_argument("case"); sp.add_argument("--max-iterations", type=int)
    sp = sub.add_parser("report", help="根拠レポート"); sp.add_argument("case")
    sp.add_argument("--format", default="md", choices=["md", "html"]); sp.add_argument("-o", "--out")
    sp = sub.add_parser("excel", help="Excel 設計シートを書き出す"); sp.add_argument("case"); sp.add_argument("-o", "--out", required=True)
    sp = sub.add_parser("excel-import", help="編集した設計シートを読み戻す"); sp.add_argument("case"); sp.add_argument("file")
    sp = sub.add_parser("manual", help="manual モードの返答を渡す"); sp.add_argument("case")
    sp.add_argument("--key", required=True); sp.add_argument("--file", required=True); sp.add_argument("--all", action="store_true")
    sp = sub.add_parser("pending", help="manual モードの返答待ち一覧"); sp.add_argument("case")
    sp = sub.add_parser("dict-import", help="分類表辞書 CSV を取り込む"); sp.add_argument("file")
    sp = sub.add_parser("local-index", help="文献 CSV を local_index に読み込む（オフライン DB）"); sp.add_argument("file"); sp.add_argument("--replace", action="store_true")
    sp = sub.add_parser("config", help="実効設定を表示")

    args = ap.parse_args(argv)
    if args.cmd == "serve":
        from server import main as serve_main  # noqa: PLC0415
        argv2 = []
        if args.port:
            argv2 += ["--port", str(args.port)]
        if args.open:
            argv2.append("--open")
        serve_main(argv2)
        return
    if args.cmd == "config":
        _print(cfgmod.public_config(cfgmod.load_config()))
        return

    orc = _orc(args)
    if args.cmd == "demo":
        def go():
            case = orc.load_sample_case()
            cid = case["case_id"]
            if args.auto:
                summary = orc.run_auto(cid, max_iterations=args.max_iterations)
            else:
                script = json.loads((cfgmod.BASE / "sample_data" / "case_0001" / "decisions.json").read_text(encoding="utf-8"))
                summary = orc.run_case(cid, ScriptedHumanDecider(script), max_iterations=args.max_iterations)
            _print(summary)
            if args.out:
                from .report.build import build_html, build_markdown
                b = orc.case_bundle(cid)
                text = build_html(b) if args.out.endswith(".html") else build_markdown(b)
                Path(args.out).write_text(text, encoding="utf-8")
                print(f"レポート: {args.out}")
        _run(go)
    elif args.cmd == "cases":
        _print([{k: c.get(k) for k in ("case_id", "name", "purpose", "status", "iteration", "updated_at")} for c in orc.store.list_cases()])
    elif args.cmd == "show":
        _print(_run(lambda: orc.summary(args.case)))
    elif args.cmd == "delete":
        _run(lambda: orc.delete_case(args.case))
        print("deleted")
    elif args.cmd == "new":
        seeds = []
        for s in args.seeds:
            if s.endswith(".json"):
                seeds.extend(json.loads(Path(s).read_text(encoding="utf-8")))
            else:
                seeds.append(s)
        case = _run(lambda: orc.create_case(name=args.name, purpose=args.purpose, input_text=Path(args.input).read_text(encoding="utf-8"),
                                            seeds=seeds, date_from=args.date_from, date_to=args.date_to,
                                            settings={"mask_terms": args.mask} if args.mask else None))
        _print(case)
    elif args.cmd == "structure":
        _print(_run(lambda: orc.structure(args.case, confirmed=args.confirm)))
    elif args.cmd == "g1":
        axes = json.loads(Path(args.axes).read_text(encoding="utf-8")) if args.axes else orc.store.get_axes(args.case)
        _print(_run(lambda: orc.decide_g1(args.case, axes)))
    elif args.cmd == "expand":
        _print(_run(lambda: orc.expand(args.case, confirmed=args.confirm)))
    elif args.cmd == "g2":
        if args.decisions:
            decisions = json.loads(Path(args.decisions).read_text(encoding="utf-8"))
        else:
            rules = {"terms": "adopt_all", "codes": "adopt_all"} if args.adopt_all else {"terms": "adopt_input_and_rsj", "codes": "adopt_known"}
            decisions = ScriptedHumanDecider({"G2": {"rules": rules}}).g2({"candidates": orc.store.get_candidates(args.case)}).payload["decisions"]
        _print(_run(lambda: orc.decide_g2(args.case, decisions)))
    elif args.cmd == "build":
        _print(_run(lambda: orc.build_queries(args.case)))
    elif args.cmd == "import":
        _print(_run(lambda: orc.import_run(args.case, args.variant, data=Path(args.csv).read_bytes(), hit_count=args.hits, filename=args.csv)))
    elif args.cmd == "run-local":
        _print(_run(lambda: orc.run_local(args.case, actor="human")))
    elif args.cmd == "score":
        _print(_run(lambda: orc.score(args.case, confirmed=args.confirm)))
    elif args.cmd == "analyze":
        _print(_run(lambda: orc.analyze(args.case, confirmed=args.confirm)))
    elif args.cmd == "g4":
        tf = json.loads(Path(args.transforms).read_text(encoding="utf-8")) if args.transforms else []
        jd = json.loads(Path(args.judgments).read_text(encoding="utf-8")) if args.judgments else []
        _print(_run(lambda: orc.decide_g4(args.case, jd, tf, args.action, confirmed=args.confirm)))
    elif args.cmd == "auto":
        _print(_run(lambda: orc.run_auto(args.case, max_iterations=args.max_iterations)))
    elif args.cmd == "report":
        from .report.build import build_html, build_markdown
        b = _run(lambda: orc.case_bundle(args.case))
        text = build_html(b) if args.format == "html" else build_markdown(b)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"レポート: {args.out}")
        else:
            print(text)
    elif args.cmd == "excel":
        from .ui.excel import design_workbook
        Path(args.out).write_bytes(design_workbook(_run(lambda: orc.case_bundle(args.case))))
        print(f"設計シート: {args.out}")
    elif args.cmd == "excel-import":
        _print(_run(lambda: orc.apply_excel(args.case, Path(args.file).read_bytes())))
    elif args.cmd == "manual":
        _print(_run(lambda: orc.answer_manual(args.case, args.key, Path(args.file).read_text(encoding="utf-8"), apply_all=args.all)))
    elif args.cmd == "pending":
        _print(orc.store.pending_manual_prompts(args.case))
    elif args.cmd == "dict-import":
        from .util import read_text_auto
        text, enc = read_text_auto(args.file)
        print(f"{orc.import_code_dictionary(text)} 件を取り込み（{enc}）")
    elif args.cmd == "local-index":
        from .db.csv_import import import_csv_bytes
        docs, info = import_csv_bytes(Path(args.file).read_bytes(), cfgmod.load_csv_dialect(cfgmod.load_config().get("csv_dialect") or "jplatpat"))
        n = orc.load_local_index(docs, replace=args.replace)
        print(f"local_index: {n} 件（取り込み {len(docs)} 件, {info.get('encoding')}, 列 {list(info.get('columns', {}))}）")


if __name__ == "__main__":
    main()
