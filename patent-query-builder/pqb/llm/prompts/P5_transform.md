現在の検索式（DSL）、RSJ 統計、決定木の節を見て、意味的に妥当な変換操作を提案してください。

# 現在の検索式（観点ごとの語・コード）
[[dsl_summary]]

# 検索観点
[[axes]]

# 統計（母集団 U の採点済み集合における識別力。正: 適合側に偏る、負: 非適合側に偏る）
[[stats]]

# 使える変換操作
[[ops]]

# 反省のためのフィードバック（前世代の探索結果。無ければ「（なし）」）
[[feedback]]

# 指示
- 各提案に op、target（axis_id、text／scheme・code など操作に必要な情報）、reason、expected_effect（narrow／widen）を付ける。
- 統計に現れない語・コードを作らない。観点の定義から外れる語は提案しない。
- 最大 10 件。

# 出力（この JSON スキーマ以外の出力は禁止。日本語）
```json
{"transforms": [{"op": "ADD_TERM", "target": {"axis_id": "A", "text": "高張力鋼板"}, "reason": "…", "expected_effect": "widen"}],
 "low_confidence": false}
```
