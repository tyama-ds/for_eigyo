"""Portable static figures from saved values; never refit or fetch a corpus.

``options.landscape`` and ``options.citation_flow`` may contain already saved
snapshots. Without them the original result's small display map is used, with
its sampling scope stated explicitly. No analysis or LLM function is invoked.
An optional ``options.notes`` list receives reasons for omitted figures.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from functools import lru_cache
from io import BytesIO
import math
import os
from pathlib import Path
import re
import threading

_LOCK = threading.RLock()
_COLORS = ("#087f8c", "#6554b5", "#d38126", "#2478b4", "#9c4977", "#4d8860", "#586579")
_RED = "#d83442"


def _number(value):
    return float(value) if type(value) in {int, float} and -1e15 < value < 1e15 and math.isfinite(value) else None


def _count(value):
    value = _number(value)
    return value if value is not None and value >= 0 else None


def _label(value, maximum=54):
    text = re.sub(r"[\x00-\x1f]", " ", str(value or ""))
    return text if len(text) <= maximum else text[:maximum-1] + "…"


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


@lru_cache(maxsize=8)
def _font_path(preferred):
    from matplotlib import font_manager
    from matplotlib.ft2font import FT2Font
    candidates = [preferred, "C:/Windows/Fonts/meiryo.ttc", "C:/Windows/Fonts/YuGothR.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansJP-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf"]
    candidates += [entry.fname for entry in font_manager.fontManager.ttflist
                   if any(name in entry.name.casefold() for name in ("noto sans jp", "noto sans cjk", "ipa", "meiryo", "yu gothic"))]
    for name in dict.fromkeys(candidates):
        if name and Path(name).is_file():
            try:
                if all(ord(character) in FT2Font(name).get_charmap() for character in "研究論文年"):
                    return name
            except (OSError, RuntimeError, ValueError):
                continue
    raise ValueError("日本語の図表には日本語フォントが必要です。ATLAS_PDF_FONT に TrueType フォントを指定してください。")


def _new(title, *, three_d=False):
    from matplotlib.figure import Figure
    fig = Figure(figsize=(10, 6), dpi=150, facecolor="white")
    ax = fig.add_subplot(111, projection="3d" if three_d else None)
    ax.set_title(title, loc="left", fontsize=15, pad=20, color="#183148")
    if not three_d:
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color="#dbe4e9", linewidth=.6, alpha=.7)
        ax.set_axisbelow(True)
        ax.tick_params(labelsize=9)
    return fig, ax


def _render(fig, title, caption, *, left=.13):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.font_manager import FontProperties
    from matplotlib.text import Text
    path = _font_path(os.getenv("ATLAS_PDF_FONT", ""))
    for item in fig.findobj(match=Text):
        item.set_fontproperties(FontProperties(fname=path, size=item.get_fontsize(), weight=item.get_fontweight()))
        item.set_parse_math(False)
    fig.subplots_adjust(left=left, right=.96, bottom=.20, top=.87)
    data = BytesIO()
    FigureCanvasAgg(fig).print_png(data)
    width, height = (int(round(value * fig.dpi)) for value in fig.get_size_inches())
    fig.clear()
    return {"title": title, "caption": caption, "png": data.getvalue(), "width": width, "height": height}


def _annual_series(result, report, kind, options):
    """Return labeled saved series. None is unknown, never an imputed zero."""
    if kind in {"foresight", "assessment"}:
        candidates = _rows(report.get("candidates"))
        selected = options.get("candidate_id")
        return [(_label(candidate.get("label", candidate.get("id"))),
                 [(str(row.get("year")), _count(row.get("count", row.get("papers"))))
                  for row in _rows((candidate.get("growth") or candidate.get("metrics", {}).get("growth") or {}).get("series"))
                  if row.get("year") is not None])
                for candidate in candidates if not selected or candidate.get("id") == selected]
    annual = _rows(report.get("annual"))
    if annual and any("focus_count" in row for row in annual):
        values = [(_label(report.get("focus", {}).get("label", "選択分野")),
                   [(str(row.get("year")), _count(row.get("focus_count"))) for row in annual])]
        if report.get("neighbor"):
            values.append((_label(report["neighbor"].get("label", "隣接分野")),
                           [(str(row.get("year")), _count(row.get("neighbor_count"))) for row in annual]))
        return values
    annual_rows = _rows(report.get("annual_rows"))
    if annual_rows:
        return [(_label(report.get("topic", {}).get("label", "選択分野")),
                 [(str(row.get("year")), _count(row.get("count")) if row.get("observed", True) else None) for row in annual_rows])]
    if annual:
        return [("対象論文", [(str(row.get("year")), _count(row.get("count", row.get("total"))))
                            for row in annual if row.get("year") is not None])]
    return [("元分析の対象論文", [(str(row.get("year")), _count(row.get("papers")))
                                    for row in _rows(result.get("timeline")) if row.get("year") is not None])]


def _annual(result, report, kind, options):
    series = [(name, rows) for name, rows in _annual_series(result, report, kind, options) if rows]
    if not series or not any(value is not None for _, rows in series for _, value in rows):
        return None
    total_series = len(series)
    series = series[:8]
    years = sorted({year for _, rows in series for year, _ in rows})
    title = "収録論文の年次件数"
    fig, ax = _new(title)
    width = .8 / len(series)
    for index, (name, rows) in enumerate(series):
        lookup = dict(rows)
        indices = [(i, lookup.get(year)) for i, year in enumerate(years) if lookup.get(year) is not None]
        ax.bar([i - .4 + width / 2 + index * width for i, _ in indices], [value for _, value in indices],
               width=width, label=name, color=_COLORS[index % len(_COLORS)])
    step = max(1, math.ceil(len(years) / 16))
    ticks = list(range(0, len(years), step))
    ax.set_xticks(ticks, [years[i] for i in ticks], rotation=35, ha="right")
    ax.set_ylabel("論文件数")
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    caption = "保存済みの年次件数です。欠測は 0 件に置き換えていません。収録集合内の観測であり、分野全体の流行や将来予測ではありません。"
    if kind in {"foresight", "assessment"}:
        caption += " 各候補の保存済み growth.series を使用しています。これは初期集合の固定系列であり、反復探索で追加した論文の年次増加を表しません。"
    if total_series > len(series):
        caption += f" {total_series} 系列のうち保存順の {len(series)} 系列を表示しています。"
    return _render(fig, title, caption)


def _topics(result, report, kind):
    if kind in {"foresight", "assessment"}:
        return None  # The original topic totals do not describe an augmented collection.
    rows = _rows(report.get("topics")) or _rows(result.get("topics"))
    values = sorted([(_label(row.get("label", row.get("id", row.get("topic_id", "未分類")))),
                       _count(row.get("count", row.get("total")))) for row in rows
                     if _count(row.get("count", row.get("total"))) is not None], key=lambda row: (-row[1], row[0]))
    if not values:
        return None
    count = len(values)
    values = values[:12][::-1]
    title = "分野別の収録論文件数" if _rows(report.get("topics")) else "元分析全体の分野別件数"
    fig, ax = _new(title)
    ax.barh(range(len(values)), [value for _, value in values], color="#087f8c")
    ax.set_yticks(range(len(values)), [_label(name, 27) for name, _ in values])
    ax.set_xlabel("論文件数")
    ax.set_xlim(left=0)
    ax.tick_params(axis="y", labelsize=8)
    caption = "保存済みの分類別件数です。マップの表示点だけを数えた値ではありません。"
    if count > 12:
        caption += f" 全 {count} 分野のうち件数上位 12 分野を表示しています。"
    return _render(fig, title, caption, left=.35)


def _snapshot(result, report, options):
    snapshot = options.get("landscape")
    if isinstance(snapshot, dict) and (not result.get("id") or snapshot.get("result_id") == result.get("id")):
        return snapshot, True
    return {"map": result.get("map") or {}, "topics": result.get("topics") or [],
            "meta": {"scope": "sample", "corpus_papers": result.get("summary", {}).get("papers", result.get("meta", {}).get("paper_count"))}}, False


def _points(snapshot, options):
    source = _rows(snapshot.get("map", {}).get("nodes"))
    valid = [row for row in source if _number(row.get("x")) is not None and _number(row.get("y")) is not None]
    requested = options.get("max_points", 400)
    limit = max(1, min(2000, requested)) if type(requested) is int else 400
    if len(valid) > limit:
        valid = [valid[index * len(valid) // limit] for index in range(limit)]
    return valid, len(source)


def _scope(snapshot, points, source_count, saved_snapshot):
    meta = snapshot.get("meta") or {}
    corpus = _count(meta.get("corpus_papers", meta.get("analysis_papers")))
    text = f"保存された共通座標の {len(points):,} 点を表示"
    if source_count > len(points):
        text += f"（保存表示点 {source_count:,} 点から有効座標を抽出・間引き）"
    if corpus is not None:
        text += f"。元の分析対象は {corpus:,.0f} 件"
    text += "。出力時の再投影・再クラスタリングは行っていません。"
    text += " 保存済みランドスケープの座標です。" if saved_snapshot else " 元分析の初期マップを使用しています。画面で切り替えた投影と異なる場合があります。"
    return text


def _palette(snapshot):
    rows = _rows(snapshot.get("topics"))
    colors, labels = {}, {}
    for index, row in enumerate(rows):
        identifier = str(row.get("id", row.get("topic_id", "unknown")))
        color = str(row.get("color") or "")
        colors[identifier] = color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else _COLORS[index % len(_COLORS)]
        labels[identifier] = _label(row.get("label", identifier), 29)
    return colors, labels


def _map(snapshot, points, caption):
    if not points:
        return None
    title = "技術ランドスケープ（保存済み共通座標）"
    fig, ax = _new(title)
    colors, labels = _palette(snapshot)
    groups = defaultdict(list)
    for point in points:
        groups[str(point.get("topic_id", "unknown"))].append(point)
    for index, (identifier, rows) in enumerate(sorted(groups.items(), key=lambda value: -len(value[1]))):
        ax.scatter([row["x"] for row in rows], [row["y"] for row in rows], s=21, alpha=.7,
                   color=colors.get(identifier, "#7a8791"), edgecolors="none",
                   label=labels.get(identifier, identifier) if index < 8 else None)
    ax.set_xlabel("共通投影 X（任意単位）")
    ax.set_ylabel("共通投影 Y（任意単位）")
    ax.legend(loc="best", fontsize=7, frameon=True)
    caption += " 点は論文です。軸・距離・密度は技術成熟度、引用数、将来性を表しません。"
    return _render(fig, title, caption)


def _period(point, interval):
    # Reuse the app's date-precision rules without invoking any projection.
    from .landscape import _period_number, _period
    number, _ = _period_number(point, interval)
    return (number, _period(number, interval)) if number is not None else (None, None)


def _parse_period(value, interval):
    text = str(value or "")
    if interval == "year" and re.fullmatch(r"\d{4}", text):
        return int(text)
    if interval == "quarter" and (match := re.fullmatch(r"(\d{4})-Q([1-4])", text)):
        return int(match[1]) * 4 + int(match[2]) - 1
    if interval == "month" and (match := re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", text)):
        return int(match[1]) * 12 + int(match[2]) - 1
    return None


def _temporal(snapshot, points, caption, report, options, saved_snapshot):
    interval = options.get("interval") or snapshot.get("meta", {}).get("interval") or report.get("interval", "year")
    interval = interval if interval in {"year", "quarter", "month"} else "year"
    dated = [(row, *_period(row, interval)) for row in points]
    dated = [(row, number, label) for row, number, label in dated if number is not None]
    if not dated:
        return None
    title = "時層と重心の推移（同一座標）"
    fig, ax = _new(title, three_d=True)
    colors, _ = _palette(snapshot)
    origin = min(number for _, number, _ in dated)
    scatter_groups = defaultdict(list)
    for row, number, _ in dated:
        scatter_groups[str(row.get("topic_id"))].append((row, number))
    for topic, rows in scatter_groups.items():
        ax.scatter([row["x"] for row, _ in rows], [row["y"] for row, _ in rows], [number-origin for _, number in rows],
                   s=11, alpha=.65, color=colors.get(topic, "#6d8493"), depthshade=False)
    labels = {number: label for _, number, label in dated}
    periods = sorted(labels)
    ticks = periods[::max(1, math.ceil(len(periods)/12))]
    ax.set_zticks([number-origin for number in ticks], [labels[number] for number in ticks], fontsize=8)
    ax.set_xlabel("共通投影 X")
    ax.set_ylabel("共通投影 Y")
    ax.set_zlabel({"year": "出版年", "quarter": "出版四半期", "month": "出版月"}[interval])
    ax.view_init(elev=24, azim=-59)
    ax.grid(True, alpha=.18)
    topic_id = options.get("topic_id") or report.get("topic", {}).get("id")
    groups = defaultdict(list)
    for row, number, label in dated:
        groups[(str(row.get("topic_id", "unknown")), number)].append(row)
    chosen = [str(topic_id)] if topic_id and topic_id != "all" else [value for value, _ in Counter(
        str(row.get("topic_id", "unknown")) for row, _, _ in dated).most_common(4)]
    centers = []
    if saved_snapshot and snapshot.get("meta", {}).get("interval") == interval:
        for row in _rows(snapshot.get("centroids")):
            number = _parse_period(row.get("period_id"), interval)
            if number is not None and str(row.get("topic_id")) in chosen and all(_number(row.get(axis)) is not None for axis in ("x", "y")):
                centers.append((str(row["topic_id"]), number, row["x"], row["y"]))
    exact = bool(centers)
    if not centers:
        centers = [(topic, number, sum(row["x"] for row in rows)/len(rows), sum(row["y"] for row in rows)/len(rows))
                   for (topic, number), rows in groups.items() if topic in chosen]
    for topic in chosen:
        history = sorted((number, x, y) for value, number, x, y in centers if value == topic)
        if history:
            ax.scatter([x for _, x, _ in history], [y for _, _, y in history], [number-origin for number, _, _ in history],
                       marker="D", s=31, color=_RED, depthshade=False)
        for first, second in zip(history, history[1:]):
            a, x1, y1 = first; b, x2, y2 = second
            ax.quiver(x1, y1, a-origin, x2-x1, y2-y1, b-a, color=_RED, linewidth=1.5, arrow_length_ratio=.13)
    caption += f" 日付精度が不足する {len(points)-len(dated):,} 点は層から除外し、年のみの文献を特定の月へ割り当てていません。"
    if exact:
        scope = "分析対象全件" if snapshot.get("meta", {}).get("scope") == "full" else "表示標本" if snapshot.get("meta", {}).get("scope") == "sample" else "保存された分析範囲"
        caption += f" 赤は保存済み重心の期間差です。重心の集計範囲は{scope}です。"
    else:
        caption += " 赤はこの図に描画した点の算術平均の期間差です。全件の内容重心や検定結果ではありません。"
    caption += f" 重心軌跡は {'選択分野' if topic_id and topic_id != 'all' else '表示件数上位 '+str(len(chosen))+' 分野'}を表示。矢印は研究者の移動・因果関係を意味しません。"
    return _render(fig, title, caption)


def _saved_centroids(report):
    movements, centers = [], []
    if isinstance(report.get("movement"), dict):
        movements.append(report["movement"])
    if isinstance(report.get("centroid"), dict):
        centers.append(report["centroid"])
    for year in _rows(report.get("years")):
        child = year.get("report") or {}
        if isinstance(child.get("centroid"), dict):
            centers.append(child["centroid"])
    for transition in _rows(report.get("transitions")):
        child = transition.get("report") or {}
        if isinstance(child.get("movement"), dict):
            movements.append(child["movement"])
    return centers, movements


def _centroid_figure(report):
    centers, movements = _saved_centroids(report)
    valid = [row for row in centers if all(_number(row.get(axis)) is not None for axis in ("x", "y"))]
    movements = [row for row in movements if all(isinstance(row.get(side), dict) and
        all(_number(row[side].get(axis)) is not None for axis in ("x", "y")) for side in ("from", "to"))]
    if not valid and not movements:
        return None
    title = "レポートに保存された重心位置・移動"
    fig, ax = _new(title)
    seen = set()
    def point(value, label):
        key = (str(label), value["x"], value["y"])
        if key not in seen:
            seen.add(key)
            ax.scatter([value["x"]], [value["y"]], s=60, color=_RED, marker="D")
            ax.annotate(_label(label, 20), (value["x"], value["y"]), xytext=(5, 7), textcoords="offset points", fontsize=9)
    for center in valid:
        point(center, center.get("period_id", ""))
    for movement in movements:
        point(movement["from"], movement.get("from_period"))
        point(movement["to"], movement.get("to_period"))
        ax.annotate("", xy=(movement["to"]["x"], movement["to"]["y"]),
                    xytext=(movement["from"]["x"], movement["from"]["y"]),
                    arrowprops={"arrowstyle": "-|>", "color": _RED, "lw": 2})
    ax.margins(.23)
    ax.set_xlabel("レポート作成時の共通投影 X")
    ax.set_ylabel("レポート作成時の共通投影 Y")
    scope = "全件分析" if report.get("scope") == "full" else "レポートの選択範囲"
    caption = f"{scope}で保存した重心・移動座標をそのまま使用しています。異なる投影の初期マップとは重ね合わせていません。"
    caption += " 赤い矢印は保存された比較の方向です。2 次元の移動量は高次元の内容差や有意性と同じではありません。"
    return _render(fig, title, caption)


def _citation(flow):
    if not isinstance(flow, dict):
        return None
    interval = flow.get("interval", "year")
    rows = [row for row in _rows(flow.get("nodes")) if _number(row.get("x")) is not None and _parse_period(row.get("period_id"), interval) is not None][:400]
    lookup = {str(row.get("id")): row for row in rows}
    edges = [row for row in _rows(flow.get("edges")) if str(row.get("source_id")) in lookup and str(row.get("target_id")) in lookup][:300]
    if not rows or not edges:
        return None
    title = "引用の時間方向（引用元 → 参照先）"
    fig, ax = _new(title)
    for edge in edges:
        source, target = lookup[str(edge["source_id"])], lookup[str(edge["target_id"])]
        ax.annotate("", xy=(_parse_period(target["period_id"], interval), target["x"]),
                    xytext=(_parse_period(source["period_id"], interval), source["x"]),
                    arrowprops={"arrowstyle": "->", "color": "#bf5b69", "alpha": .28, "lw": .7, "connectionstyle": "arc3,rad=.06"})
    ax.scatter([_parse_period(row["period_id"], interval) for row in rows], [row["x"] for row in rows], s=19, color="#087f8c")
    labels = {_parse_period(row["period_id"], interval): row["period_id"] for row in rows}
    ticks = sorted(labels)[::max(1, math.ceil(len(labels)/16))]
    ax.set_xticks(ticks, [labels[value] for value in ticks], rotation=35, ha="right")
    ax.set_xlabel("保存済み出版期")
    ax.set_ylabel("引用マップの共通投影 X")
    caption = f"保存済みの引用リンクから {len(rows):,} 点・{len(edges):,} 辺を描画。横軸は出版期、縦軸は保存共通座標 X です。被引用回数から辺を補っていません。"
    if _count(flow.get("coverage", {}).get("valid_edges")) is not None:
        caption += f" 集計対象の有効引用は {flow['coverage']['valid_edges']:,} 辺です。"
    caption += " 収録外・識別子未照合・日付不明の引用を含まず、因果的影響の証明ではありません。"
    return _render(fig, title, caption)


def build_report_figures(result: dict, report: dict, *, kind: str, options: dict | None = None) -> list[dict]:
    """Create PNGs from saved counts/coordinates, with explicit display scopes.

    Supported options: landscape, citation_flow (existing snapshots), interval,
    topic_id, candidate_id, max_points (1..2000), and notes (an output list).
    Figures with no usable saved data are omitted, not populated with examples.
    """
    options = options or {}
    notes = options.get("notes")
    figures = []
    with _LOCK:
        snapshot, saved_snapshot = _snapshot(result, report, options)
        points, source_count = _points(snapshot, options)
        caption = _scope(snapshot, points, source_count, saved_snapshot)
        tasks = [("年次件数", lambda: _annual(result, report, kind, options)),
                 ("分野別件数", lambda: _topics(result, report, kind)),
                 ("保存済み技術マップ", lambda: _map(snapshot, points, caption)),
                 ("時層図", lambda: _temporal(snapshot, points, caption, report, options, saved_snapshot)),
                 ("重心の比較図", lambda: _centroid_figure(report)),
                 ("引用図", lambda: _citation(options.get("citation_flow") or report.get("citation_flow")))]
        for label, make in tasks:
            value = make()
            if value:
                if result.get("is_demo") or result.get("meta", {}).get("is_demo") or report.get("is_demo") or report.get("meta", {}).get("is_demo"):
                    value["caption"] = "合成データの図です。実研究の観測ではありません。 " + value["caption"]
                figures.append(value)
            elif isinstance(notes, list):
                notes.append(f"{label}：対応する保存済みデータがないため図を省略しました。")
    return figures
