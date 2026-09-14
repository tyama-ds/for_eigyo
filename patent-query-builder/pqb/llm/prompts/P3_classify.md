検索観点ごとに、FI／Fターム／IPC の分類コード候補を提案してください。

# 検索観点と語
[[axes]]
[[terms_by_axis]]

# 既知文献に付与されている分類の集計（体系別・件数順）
[[seed_code_counts]]

# 参照できる分類表（抜粋）
[[known_codes]]

# 指示
- 既知文献の付与分類を第一候補とする。集計・分類表に無いコードを提案する場合は reason に明記する。
- コードは体系（FI / FT / IPC）と記法を正確に。**辞書照合はシステム側で行う**ので、分類表に無いコードは棄却され得る。
- 各候補に reason（なぜその観点に対応するか）と confidence（0〜1）を付ける。

# 出力（この JSON スキーマ以外の出力は禁止。日本語）
```json
{"codes": [{"axis_id": "A", "scheme": "FI", "code": "C22C38/00", "reason": "既知文献 3 件に付与", "confidence": 0.8}],
 "low_confidence": false}
```
