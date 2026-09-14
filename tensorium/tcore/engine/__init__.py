"""学習エンジン（torch 依存部分は遅延インポート）。

- pipeline.train_run: 学習ジョブの本体（全ファミリー）
- predictor.load_predictor: 保存済み run から予測器を復元
- baseline: torch 不要のベースライン
"""
