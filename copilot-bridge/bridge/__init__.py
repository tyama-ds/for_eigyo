"""Copilot Bridge — M365 Copilot のチャット画面を OpenAI 互換 API に見せかける中継サーバ。

構成:
- ``config``   : 設定（config.json / 環境変数 / 引数）
- ``protocol`` : 会話とツール定義を Copilot 向けの文章にし、回答からツール呼び出しを読み取る
- ``session``  : API の要求が「どの会話の続きか」を見分け、送る差分を決める
- ``pool``     : ウインドウ（作業担当）の割り当てと順番待ち
- ``edge``     : Selenium で Edge の Copilot を操作する作業担当
- ``fake``     : ブラウザ無しで動く作業担当（テスト・開発用）
"""

__version__ = "0.1.0"
