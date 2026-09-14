検索観点ごとの語を、同義語・上位下位概念・表記ゆれ・略語・英訳に展開してください。
さらに、適合文献の名称・要約から技術用語を抽出し、該当する観点に割り当ててください。

# 検索観点
[[axes]]

# 現在の語（観点ごと）
[[terms_by_axis]]

# 適合文献（名称・要約）
[[relevant_docs]]

# 同義語辞書からのヒント
[[synonyms_hint]]

# 指示
- 各語に variant_kind（original / synonym / variant / broader / narrower / abbreviation / english / extracted）と
  origin（"input" または "doc:<文献番号>"）を付ける。文献由来の語は必ず由来文献を付ける。
- 観点の定義から外れる語は出さない。confidence は 0〜1。
- 入力に含まれない文献番号を作らない。

# 出力（この JSON スキーマ以外の出力は禁止。日本語）
```json
{"terms": [{"axis_id": "A", "text": "高張力鋼板", "variant_kind": "synonym", "origin": "input", "confidence": 0.9}],
 "low_confidence": false}
```
