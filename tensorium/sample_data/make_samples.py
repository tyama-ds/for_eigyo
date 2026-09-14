#!/usr/bin/env python3
"""サンプルデータ生成（決定論的）。生成物は同じフォルダにコミット済み。

    python sample_data/make_samples.py
"""
from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
rng = random.Random(7)


# ---------------------------------------------------------------- 1. 商品レビュー → 評価（分類）
CATS = {
    "家電": ["掃除機", "炊飯器", "ドライヤー", "電気ケトル", "空気清浄機", "イヤホン"],
    "食品": ["コーヒー豆", "オリーブオイル", "レトルトカレー", "チョコレート", "緑茶", "パスタ"],
    "日用品": ["洗剤", "タオル", "収納ボックス", "歯ブラシ", "ハンドソープ", "マスク"],
    "衣料": ["スニーカー", "ダウンジャケット", "Tシャツ", "リュック", "レインコート", "手袋"],
    "書籍": ["ビジネス書", "小説", "料理本", "参考書", "写真集", "漫画"],
}
POS = ["期待以上でした", "とても使いやすいです", "コスパが最高", "買ってよかった", "リピート確定です",
       "品質が良く長持ちしそう", "家族にも好評", "デザインが気に入っています", "想像より軽くて扱いやすい",
       "梱包も丁寧で好印象", "友人にも勧めたい", "毎日使っています"]
NEU = ["特に不満はありません", "値段相応だと思います", "普通に使えます", "まだ数回しか使っていません",
       "可もなく不可もなく", "説明書どおりの性能", "他と比べていないので分かりません", "思ったより大きめでした",
       "色が写真と少し違いました", "到着は予定どおりでした"]
NEG = ["すぐに壊れました", "説明と違う商品が届いた", "期待外れでがっかり", "音がうるさくて使えない",
       "サイズが合わず返品しました", "匂いが気になります", "二度と買いません", "値段の割に安っぽい",
       "サポートの対応が悪かった", "初期不良でした", "説明書が分かりにくい", "写真と全然違う"]
OPEN = ["{item}を購入。", "{item}を家族用に買いました。", "セールで{item}を注文しました。", "初めて{item}を試しました。",
        "{item}のレビューです。", "口コミを見て{item}を選びました。"]
CLOSE = ["星{n}つです。", "{n}点。", "総合的には{judge}。", "また利用したいと思います。", "以上、参考になれば。", ""]


def make_reviews(n=600):
    rows = []
    for _ in range(n):
        cat = rng.choice(list(CATS))
        item = rng.choice(CATS[cat])
        label = rng.choices(["高評価", "普通", "低評価"], weights=[0.45, 0.25, 0.30])[0]
        pool = {"高評価": POS, "普通": NEU, "低評価": NEG}[label]
        k = rng.randint(1, 3)
        body = "。".join(rng.sample(pool, k)) + "。"
        if rng.random() < 0.25:                     # 別感情の一文を混ぜてノイズにする
            other = rng.choice([p for p in (POS, NEU, NEG) if p is not pool])
            body += rng.choice(other) + "。"
        star = {"高評価": rng.choice([4, 5, 5]), "普通": rng.choice([3, 3, 4, 2]), "低評価": rng.choice([1, 1, 2])}[label]
        judge = {"高評価": "満足", "普通": "まあまあ", "低評価": "不満"}[label]
        text = rng.choice(OPEN).format(item=item) + body + rng.choice(CLOSE).format(n=star, judge=judge)
        price = {"家電": rng.randint(30, 400) * 100, "食品": rng.randint(5, 40) * 100,
                 "日用品": rng.randint(3, 30) * 100, "衣料": rng.randint(20, 200) * 100,
                 "書籍": rng.randint(8, 40) * 100}[cat]
        if rng.random() < 0.04:
            label = rng.choice(["高評価", "普通", "低評価"])   # ラベルノイズ
        rows.append([text, cat, item, price, rng.randint(1, 6), label])
    write_csv(HERE / "reviews_ja.csv", ["レビュー本文", "カテゴリ", "商品", "価格", "購入回数", "評価"], rows)


# ---------------------------------------------------------------- 2. 中古車 → 価格（回帰）
MAKERS = {
    "トヨタ": [("プリウス", 230), ("アクア", 170), ("ハリアー", 380), ("アルファード", 450), ("カローラ", 190)],
    "ホンダ": [("フィット", 160), ("N-BOX", 150), ("ヴェゼル", 260), ("ステップワゴン", 300)],
    "日産": [("ノート", 170), ("セレナ", 290), ("エクストレイル", 320), ("リーフ", 330)],
    "マツダ": [("CX-5", 310), ("デミオ", 150), ("ロードスター", 280)],
    "スバル": [("フォレスター", 320), ("インプレッサ", 220), ("レヴォーグ", 340)],
    "スズキ": [("スイフト", 140), ("ジムニー", 190), ("ハスラー", 140)],
}
GOOD = [("ワンオーナー", 8), ("禁煙車", 6), ("ディーラー整備記録簿あり", 10), ("純正ナビ", 5), ("フルセグTV", 2),
        ("バックカメラ", 3), ("衝突被害軽減ブレーキ", 7), ("本革シート", 12), ("サンルーフ", 9), ("新品タイヤ交換済", 6),
        ("ドライブレコーダー", 2), ("ETC", 1), ("LEDヘッドライト", 4), ("両側パワースライドドア", 8)]
BAD = [("修復歴あり", -45), ("小キズ多数", -10), ("内装に汚れ", -8), ("エアコン効き弱い", -15), ("タイヤ溝少なめ", -6),
       ("ナビ地図データ古い", -2), ("バッテリー要交換", -7), ("車検切れ", -12), ("鍵1本のみ", -3)]


def make_cars(n=520):
    rows = []
    for _ in range(n):
        maker = rng.choice(list(MAKERS))
        model, base = rng.choice(MAKERS[maker])
        year = rng.randint(2009, 2023)
        age = 2024 - year
        mileage = round(max(0.1, rng.gauss(age * 0.9, 1.8)), 1)          # 万 km
        disp = rng.choice([660, 1000, 1200, 1500, 1800, 2000, 2500]) if model != "リーフ" else 0
        grade = rng.choice(["S", "G", "X", "Z", "ハイブリッド", "ターボ", "4WD", "特別仕様車"])
        goods = rng.sample(GOOD, rng.randint(0, 5))
        bads = rng.sample(BAD, rng.choices([0, 1, 2, 3], weights=[0.5, 0.3, 0.15, 0.05])[0])
        price = base * (0.88 ** age) * (1 - 0.018 * mileage)
        price *= 1 + sum(w for _, w in goods) / 100 + sum(w for _, w in bads) / 100
        if grade in ("ハイブリッド", "ターボ", "4WD", "特別仕様車"):
            price *= 1.08
        price *= rng.gauss(1.0, 0.06)
        price = round(max(15.0, price), 1)
        parts = [f"{year}年式 {maker} {model} {grade}", f"走行{mileage}万km"] + [g for g, _ in goods] + [b for b, _ in bads]
        rng.shuffle(parts[2:])
        text = " ".join(parts)
        shaken = rng.choice(["あり", "あり", "なし"])
        color = rng.choice(["白", "黒", "シルバー", "赤", "青", "パール"])
        rows.append([text, maker, year, mileage, disp, color, shaken, price])
    write_csv(HERE / "used_cars_ja.csv",
              ["車両説明", "メーカー", "年式", "走行距離_万km", "排気量_cc", "色", "車検", "価格_万円"], rows)


# ---------------------------------------------------------------- 3. 商談メモ → 結果（分類・XLSX）
INDUSTRY = ["製造業", "建設業", "小売業", "物流業", "IT", "医療", "自治体", "金融"]
STAFF = ["佐藤", "鈴木", "高橋", "田中", "伊藤", "渡辺"]
WIN = ["決裁者が同席し前向きな反応", "予算は確保済みとのこと", "競合比較で当社が優位", "導入時期を具体的に相談された",
       "現場担当者も強く推薦", "見積の即日提示を依頼された", "既存設備との互換性を高評価", "トップダウンで進めたい意向",
       "追加のデモ依頼あり", "契約書の雛形を求められた"]
LOSE = ["予算が取れないと明言", "他社製品で決まりそう", "担当者が異動予定", "価格が想定の倍と言われた",
        "決裁者に会えていない", "要件が当社製品と合わない", "検討は来期以降に延期", "返信が途絶えている",
        "既存ベンダーとの関係を優先", "稟議が否決された"]
CONT = ["資料を持ち帰って検討", "次回は技術者を同席させる予定", "他部署へも展開したいとの声", "要件の詳細ヒアリングを継続",
        "概算見積を再提出予定", "ROI試算を求められた", "PoC の範囲を調整中", "上長への説明資料を作成依頼",
        "競合と並行して比較中", "導入効果の事例を追加送付"]


def make_sales(n=420):
    try:
        import openpyxl
    except ImportError:
        print("openpyxl が無いため XLSX サンプルはスキップ")
        return
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "商談ログ"
    ws.append(["商談日", "顧客業種", "担当", "商談メモ", "提案金額_万円", "商談回数", "結果"])
    start = date(2024, 4, 1)
    for _ in range(n):
        label = rng.choices(["受注", "失注", "継続"], weights=[0.35, 0.3, 0.35])[0]
        pool = {"受注": WIN, "失注": LOSE, "継続": CONT}[label]
        memo = "。".join(rng.sample(pool, rng.randint(1, 3)))
        if rng.random() < 0.3:
            memo += "。" + rng.choice(rng.choice([WIN, LOSE, CONT]))
        memo += "。"
        amount = rng.choice([50, 80, 120, 200, 350, 500, 800, 1200, 2000])
        if label == "失注":
            amount = int(amount * rng.choice([1, 1.5, 2]))
        visits = {"受注": rng.randint(2, 6), "失注": rng.randint(1, 3), "継続": rng.randint(1, 5)}[label]
        d = start + timedelta(days=rng.randint(0, 420))
        ws.append([d, rng.choice(INDUSTRY), rng.choice(STAFF), memo, amount, visits, label])
    for cell in ws["A"][1:]:
        cell.number_format = "yyyy-mm-dd"
    ws.column_dimensions["D"].width = 60
    wb.save(HERE / "sales_memo_ja.xlsx")


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


if __name__ == "__main__":
    make_reviews()
    make_cars()
    make_sales()
    print("generated:", sorted(p.name for p in HERE.iterdir() if p.suffix in (".csv", ".xlsx")))
