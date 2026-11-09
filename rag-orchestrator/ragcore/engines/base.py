"""エンジン共通のインターフェースとヘルパー。

各エンジンは「ingest(コーパス→インデックス)」と「query(質問→回答+出典)」を実装する。
インデックスは JSON 化可能な dict とし、data/ 以下へ保存される。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..config import chat_configured, embed_configured
from ..llm import LLMClient


@dataclass
class EngineContext:
    """ingest / query の実行コンテキスト（LLM クライアント・進捗通知）。"""

    llm: LLMClient
    cfg: dict
    progress: Callable[[float, str], None] = lambda frac, msg: None
    logs: list[str] = field(default_factory=list)
    # 質問時に作ったキャッシュ（GraphRAG の Lazy 要約など）を保存済みインデックスへ書き足す関数。
    # query が積み、オーケストレーターが store.patch_index で保存済みのインデックスに適用する
    index_patches: list[Callable[[dict], None]] = field(default_factory=list)
    # 質問の埋め込み（同じ文は1回だけ埋め込む。組み合わせ RAG ではベクトルと GraphRAG が共用する）
    qvec_cache: dict[str, list[float]] = field(default_factory=dict, repr=False)

    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def query_vec(self, text: str) -> list[float]:
        """質問の埋め込み。失敗（LLMError）は覚えずにそのまま投げる（次の呼び出しでやり直す）。"""
        vec = self.qvec_cache.get(text)
        if vec is None:
            vec = self.llm.embed([text])[0]
            self.qvec_cache[text] = vec
        return vec

    @property
    def chat_ok(self) -> bool:
        return chat_configured(self.cfg)

    @property
    def embed_ok(self) -> bool:
        return embed_configured(self.cfg)


class Engine:
    """RAG エンジンの基底クラス。"""

    id = "base"
    name = "Base"
    kind = "builtin"            # builtin | external
    experimental = False
    description = ""
    # UI の要件表示: chat=生成LLM, embed=埋め込みAPI
    requires_chat = True
    requires_embed = False
    # True: インデックスが無い・古いときは質問の前にその場で作る（作るのが一瞬のエンジン。組み合わせ RAG）
    auto_index = False

    def availability(self, cfg: dict) -> tuple[bool, str]:
        """(利用可能か, 理由)。外部エンジンは import 可否も見る。"""
        if self.requires_chat and not chat_configured(cfg):
            return False, "LLM（チャット）が未設定です"
        if self.requires_embed and not embed_configured(cfg):
            return False, "埋め込み（Embed）が未設定です"
        return True, ""

    def ingest(self, corpus: list[dict], ctx: EngineContext) -> dict:
        raise NotImplementedError

    def query(self, index: dict, question: str, mode: str, ctx: EngineContext) -> dict:
        raise NotImplementedError

    def retrieve(self, index: dict, question: str, ctx: EngineContext, k: int = 6,
                 *, detail: dict | None = None) -> list[tuple[str, float]]:
        """検索だけ（回答は作らない）。[(チャンク id, スコア)] を順位順に最大 k 件。

        チャンク id は build_chunks の 'docID#連番'（既定の区切りで作ったエンジンどうしで共通）。
        detail に dict を渡すと、エンジンごとの内訳（判定回数・たどったエンティティ等）を書き込む。
        組み合わせ RAG がこれを呼んで RRF で統合する。query() も同じ検索を使う。
        """
        raise NotImplementedError

    def variant(self, *, stats: dict | None = None, cfg: dict | None = None) -> dict | None:
        """同じエンジンの中の方式の違い（例: GraphRAG の「高速」）。UI のバッジと統合レポートの名前に使う。

        stats を渡すと構築済みインデックスの方式、cfg を渡すと次に構築するときの方式。
        両方渡したときは、構築時に方式が決まるエンジン（GraphRAG）は stats、質問時の設定で動く
        エンジン（組み合わせ RAG）は cfg を使う。
        戻り値は {"kind", "label", "title"}（方式の違いが無いエンジンは None）。
        """
        return None

    def info(self, cfg: dict) -> dict:
        ok, reason = self.availability(cfg)
        return {
            "id": self.id, "name": self.name, "kind": self.kind,
            "experimental": self.experimental, "description": self.description,
            "requires": {"chat": self.requires_chat, "embed": self.requires_embed},
            "available": ok, "reason": reason, "auto_index": self.auto_index,
        }


# ---------------------------------------------------------------- 共通ヘルパー

def build_chunks(corpus: list[dict], *, size: int = 1200, overlap: int = 120) -> list[dict]:
    """コーパス（[{id,title,text}]）をチャンク列へ。id は 'docID#連番'。"""
    from ..textutil import split_chunks

    chunks: list[dict] = []
    for doc in corpus:
        for i, text in enumerate(split_chunks(doc["text"], size=size, overlap=overlap)):
            chunks.append({
                "id": f"{doc['id']}#{i}",
                "doc_id": doc["id"],
                "doc_title": doc.get("title") or doc["id"],
                "text": text,
            })
    return chunks


def chunk_citation(chunk: dict, score: float | None = None) -> dict:
    cite = {
        "type": "chunk",
        "ref": chunk["id"],
        "title": chunk["doc_title"],
        "snippet": chunk["text"][:200],
    }
    if score is not None:
        cite["score"] = round(float(score), 4)
    return cite


ANSWER_SYSTEM = (
    "あなたは社内文書に基づいて回答するアシスタントです。"
    "与えられた資料に書かれていることだけを根拠に、日本語で簡潔かつ正確に答えてください。"
    "資料に無いことは「資料からは分かりません」と述べてください。"
)


def generate_answer(ctx: EngineContext, question: str, passages: list[dict],
                    *, task_tag: str = "answer") -> tuple[str, str]:
    """取得パッセージから回答を生成し (回答, 推論過程) を返す。[S1] 形式の出典参照を促す。"""
    blocks = []
    for i, chunk in enumerate(passages):
        blocks.append(f"[S{i + 1}] （{chunk['doc_title']}）\n{chunk['text']}")
    context = "\n\n".join(blocks)
    prompt = (
        f"[TASK:{task_tag}]\n"
        "以下の資料を根拠に質問へ回答してください。"
        "根拠にした資料は文末に [S1] のように番号で示してください。\n\n"
        f"# 資料\n{context}\n\n# 質問\n{question}\n\n# 回答"
    )
    return ctx.llm.chat(prompt, system=ANSWER_SYSTEM, want_think=True)


def extractive_answer(question: str, passages: list[dict]) -> str:
    """LLM 未設定時のフォールバック: 上位パッセージの抜粋をそのまま返す。"""
    if not passages:
        return "関連する箇所が見つかりませんでした。"
    lines = ["（LLM 未設定のため、関連箇所の抜粋を表示しています）", ""]
    for i, chunk in enumerate(passages[:4]):
        excerpt = chunk["text"][:400]
        lines.append(f"[S{i + 1}] {chunk['doc_title']}: {excerpt}")
    return "\n\n".join(lines)
