"""pqb — Patent Query Builder.

発明の説明を入力に、キーワード×分類コード（FI／Fターム／IPC）のブール検索式を
「広めに決めて、結果から逆算し、適合度で少しずつ改善する」再帰的改善ループで作る
（企画書 docs/patent_query_system_proposal.md の共通コア実装）。

- 標準ライブラリのみで動作する（Anaconda 同梱ライブラリも不要）
- ネットワーク呼び出しは pqb.llm.adapter と pqb.db.adapter の 2 箇所に限定
- J-PlatPat 等 Web サイトへのプログラムアクセスは実装しない（人が実行し CSV を取り込む）
"""
__version__ = "0.1.0"
