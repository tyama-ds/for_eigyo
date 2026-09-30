"""AI 取り込み: [[リンク]] などの記法を持たない文書（PDF・Word・PowerPoint など）を、
ローカル LLM に読ませて「つながったノート」の下書きにし、確認してから Vault に加える。

流れ（1 ファイルごと）
1. 本文を取り出す（extract.py）
2. 長い文書は区切って LLM に要点を書かせ（map）、それをまとめて分析する（reduce）
   → タイトル・要約・要点・登場する名前（顧客・人物・製品など）・タグ
3. RAG: 要約と名前で既存のノート・資料を検索し（BM25 ＋ 埋め込み）、
   候補を LLM に見せて本当に関係するものと理由を選ばせる
4. Markdown の下書きを作る（元の資料へのリンク・要約・要点・[[名前]]・関連ノート・#タグ）
5. 画面で確認・編集して保存。原本は必要なら Vault の資料フォルダへコピーする

LLM が未設定・接続できない場合も、本文の取り込みとキーワード検索による関連ノートだけで下書きを作る。
下書きは ``<vault>/.mycel/ingest/`` に置く（ノートとしては数えない）。
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import links as L
from .config import chat_configured
from .extract import EXT_GROUP, ExtractError, extract, is_supported
from .index import Cancelled
from .llm import LLMClient, LLMError
from .vault import VaultError, normalize_rel

DEFAULT_OPTIONS = {
    "dest_folder": "取り込み",          # ノートの保存先
    "keep_original": True,             # アップロードした原本を Vault に保存する
    "original_folder": "資料",          # 原本の保存先
    "include_body": False,             # ノートに本文も入れる（原本を保存しないときは常に入れる）
    "link_new_names": True,            # まだノートの無い名前も [[リンク]] にする
    "use_llm": True,
}
ENTITY_LABELS = [("customers", "顧客・取引先"), ("people", "人物"), ("products", "製品・サービス"),
                 ("projects", "案件・プロジェクト"), ("others", "その他")]
_BAD = re.compile(r'[<>:"|?*\\/\x00-\x1f\[\]#^]')
_lock = threading.RLock()


def safe_name(s: str, fallback: str = "無題") -> str:
    s = _BAD.sub(" ", s or "").strip().strip(".")
    s = re.sub(r"\s+", " ", s)[:80].strip()
    return s or fallback


def _split(text: str, size: int) -> list[str]:
    """段落の切れ目を優先して size 文字前後に区切る。"""
    parts, buf = [], ""
    for para in re.split(r"\n\s*\n", text):
        while len(para) > size:
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(para[:size])
            para = para[size:]
        if buf and len(buf) + len(para) + 2 > size:
            parts.append(buf)
            buf = ""
        buf = f"{buf}\n\n{para}" if buf else para
    if buf.strip():
        parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def _json(raw: str):
    raw = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if m:
        raw = m.group(1)
    pairs = [("{", "}"), ("[", "]")]
    pairs.sort(key=lambda p: raw.find(p[0]) if raw.find(p[0]) != -1 else len(raw))   # 先に現れる方を優先
    for open_, close in pairs:
        s, e = raw.find(open_), raw.rfind(close)
        if s != -1 and e > s:
            try:
                return json.loads(raw[s:e + 1])
            except ValueError:
                continue
    return None


def _strs(v, limit: int = 12) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[、,\n]", v)
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        x = str(x).strip().lstrip("#").strip()
        if x and x not in out:
            out.append(x[:80])
    return out[:limit]


class Ingestor:
    def __init__(self, app):
        self.app = app

    # ------------------------------------------------------------ 保存場所
    @property
    def dir(self) -> Path:
        d = self.app.vault.internal / "ingest"
        (d / "files").mkdir(parents=True, exist_ok=True)
        return d

    def _file(self) -> Path:
        return self.dir / "drafts.json"

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self._file().read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, drafts: list[dict]) -> None:
        tmp = self._file().with_suffix(".tmp")
        tmp.write_text(json.dumps(drafts, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._file())

    def _update(self, did: str, **fields) -> dict:
        with _lock:
            drafts = self._load()
            for d in drafts:
                if d["id"] == did:
                    d.update(fields, updated=time.time())
                    self._save(drafts)
                    return d
        raise VaultError("下書きが見つかりません", 404)

    def get(self, did: str) -> dict:
        for d in self._load():
            if d["id"] == did:
                return d
        raise VaultError("下書きが見つかりません", 404)

    def options(self) -> dict:
        saved = self.app.config().get("ingest") or {}
        return {**DEFAULT_OPTIONS, **{k: v for k, v in saved.items() if k in DEFAULT_OPTIONS}}

    # ------------------------------------------------------------ 一覧と追加
    def list(self) -> dict:
        cfg = self.app.config()
        base = cfg.get("base_url", "")
        local = bool(re.match(r"https?://(127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0)(:|/|$)", base))
        drafts = sorted(self._load(), key=lambda d: -d["created"])
        return {"drafts": drafts, "options": self.options(),
                "llm": {"chat": chat_configured(cfg), "model": cfg.get("model", ""), "base_url": base,
                        "local": local, "embed_model": cfg.get("embed_model", "")}}

    def _new(self, name: str, origin: str, source: str, size: int) -> dict:
        d = {"id": uuid.uuid4().hex[:12], "name": name, "grp": EXT_GROUP.get(Path(name).suffix.lower(), ""),
             "origin": origin, "source": source, "size": size, "status": "queued", "phase": "",
             "error": "", "title": "", "note_path": "", "markdown": "", "related": [], "llm": False,
             "model": "", "chars": 0, "original_path": "", "saved_path": "", "options": {},
             "created": time.time(), "updated": time.time()}
        with _lock:
            drafts = self._load()
            drafts.append(d)
            self._save(drafts)
        return d

    def upload(self, name: str, data: bytes) -> dict:
        name = Path((name or "").replace("\\", "/")).name
        if not name or name.startswith("."):
            raise VaultError("ファイル名が不正です")
        if not is_supported(name) or name.lower().endswith((".md", ".markdown")):
            raise VaultError(f"この形式は取り込めません: {name}（PDF・Word・Excel・PowerPoint・メール・テキストなど）")
        if len(data) > self.app.scope.max_bytes():
            raise VaultError(f"ファイルが大きすぎます（上限 {self.app.scope.data['max_mb']} MB。読み込み範囲で変更できます）", 413)
        d = self._new(name, "upload", "", len(data))
        folder = self.dir / "files" / d["id"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
        return self._update(d["id"], source=f"files/{d['id']}/{name}")

    def add_paths(self, paths: list[str]) -> list[dict]:
        """読み込み範囲にある資料（Vault 内・外部フォルダ）を取り込み対象にする。"""
        out = []
        for p in paths:
            path = self.app._path(p)
            if not self.app._is_doc(path):
                raise VaultError(f"ノートはそのまま使えます: {path}")
            abs_p = self.app.scope.abs_path(path)
            if not abs_p.is_file():
                raise VaultError(f"ファイルが見つかりません: {path}", 404)
            out.append(self._new(abs_p.name, "doc", path, abs_p.stat().st_size))
        return out

    def _abs(self, d: dict) -> Path:
        if d["origin"] == "upload":
            p = (self.dir / d["source"]).resolve()
            if self.dir.resolve() not in p.parents:
                raise VaultError("不正な下書きです")
            return p
        return self.app.scope.abs_path(d["source"])

    def discard(self, ids: list[str]) -> int:
        with _lock:
            drafts = self._load()
            keep = [d for d in drafts if d["id"] not in ids]
            for d in drafts:
                if d["id"] in ids:
                    shutil.rmtree(self.dir / "files" / d["id"], ignore_errors=True)
            self._save(keep)
        return len(drafts) - len(keep)

    def edit(self, did: str, markdown: str | None = None, note_path: str | None = None) -> dict:
        d = self.get(did)
        if d["status"] == "saved":
            raise VaultError("保存済みです（ノートを直接編集してください）")
        fields = {}
        if isinstance(markdown, str):
            fields["markdown"] = markdown
        if isinstance(note_path, str) and note_path.strip():
            fields["note_path"] = normalize_rel(note_path)
        return self._update(did, **fields)

    # ------------------------------------------------------------ 下書き作り（バックグラウンド）
    def start(self, ids: list[str] | None, options: dict | None) -> dict:
        opts = {**self.options(), **{k: v for k, v in (options or {}).items() if k in DEFAULT_OPTIONS}}
        for k in ("dest_folder", "original_folder"):
            opts[k] = str(opts[k] or "").replace("\\", "/").strip().strip("/")
        self.app.update_config({"ingest": opts})
        targets = [d["id"] for d in self._load()
                   if (ids is None and d["status"] in ("queued", "error")) or (ids is not None and d["id"] in ids)]
        targets = [t for t in targets if self.get(t)["status"] != "saved"]
        if not targets:
            raise VaultError("下書きを作るファイルがありません（ファイルを追加してください）")
        for t in targets:
            self._update(t, status="queued", error="", options=opts)

        def run(job):
            done = errors = 0
            for i, did in enumerate(targets):
                if job.cancel.is_set():
                    for rest in targets[i:]:
                        self._update(rest, status="queued", phase="")
                    raise Cancelled()
                d = self.get(did)
                job.progress("AI 取り込み", i, len(targets), d["name"])
                try:
                    self._process(d, opts, job)
                    done += 1
                except Cancelled:
                    self._update(did, status="queued", phase="")
                    raise
                except Exception as e:  # noqa: BLE001 - 1 件の失敗で全体を止めない
                    self._update(did, status="error", phase="", error=str(e))
                    errors += 1
            return {"drafted": done, "errors": errors}

        return self.app.jobs.start("ingest", f"AI 取り込み（{len(targets)} 件）", None, run)

    def _phase(self, did: str, phase: str, job) -> None:
        if job is not None and job.cancel.is_set():
            raise Cancelled()
        self._update(did, status="processing", phase=phase)
        if job is not None:
            job.phase = phase

    def _process(self, d: dict, opts: dict, job=None) -> dict:
        did = d["id"]
        cfg = self.app.config()
        self._phase(did, "本文を取り出しています", job)
        try:
            text = extract(self._abs(d)).strip()
        except ExtractError as e:
            raise VaultError(str(e)) from e
        if not text:
            raise VaultError("本文を取り出せませんでした（スキャン画像の PDF などは文字がありません）")
        stem = Path(d["name"]).stem
        use_llm = bool(opts.get("use_llm")) and chat_configured(cfg)
        info = {"title": stem, "summary": "", "points": [], "entities": {}, "tags": [], "doc_type": "", "date": ""}
        llm_error = ""
        client = LLMClient(cfg) if use_llm else None
        if use_llm:
            try:
                info.update(self._analyze(did, text, stem, client, cfg, job))
            except LLMError as e:
                llm_error = str(e)
                use_llm = False
        self._phase(did, "関連するノートを探しています（RAG）", job)
        related = self._related(did, info, text, client if use_llm else None)
        # 保存先を決める（この時点で空いている名前）
        title = safe_name(info.get("title") or stem, stem)
        folder = opts["dest_folder"]
        note_path = self._free_note(f"{folder}/{title}" if folder else title)
        original = d["source"] if d["origin"] == "doc" else ""
        if d["origin"] == "upload" and opts.get("keep_original"):
            of = opts["original_folder"]
            original = self._free_file(f"{of}/{d['name']}" if of else d["name"])
        md = self.compose(d, info, related, text, original, opts, cfg.get("model", "") if use_llm else "")
        return self._update(did, status="ready", phase="", markdown=md, title=title, note_path=note_path,
                            related=related, llm=use_llm, model=cfg.get("model", "") if use_llm else "",
                            chars=len(text), original_path=original,
                            error=f"LLM を使えなかったため、本文とキーワード検索だけで作りました: {llm_error}" if llm_error else "")

    # ---- LLM で読む
    def _analyze(self, did: str, text: str, stem: str, client: LLMClient, cfg: dict, job) -> dict:
        size = int(cfg.get("ingest_chunk_chars") or 3000)
        max_chunks = int(cfg.get("ingest_max_chunks") or 24)
        parts = _split(text, size)
        omitted = max(0, len(parts) - max_chunks)
        if omitted:                              # 長すぎる文書は先頭と末尾を優先
            head = max_chunks - max_chunks // 4
            parts = parts[:head] + parts[-(max_chunks - head):]
        if len(parts) <= 1:
            material = parts[0] if parts else text[:size]
        else:
            notes = []
            for i, part in enumerate(parts, 1):
                self._phase(did, f"AI が読んでいます {i}/{len(parts)}", job)
                raw = client.chat(
                    "[TASK:ingest_map]\n次は文書「" + stem + f"」の一部（{i}/{len(parts)}）です。"
                    "この部分の要点を、固有名詞・数字・日付・決定事項を落とさずに箇条書き 3〜8 行で書いてください。"
                    "書かれていないことは足さないでください。\n\n# 文書の一部\n" + part,
                    temperature=0.1)
                notes.append(f"## 部分 {i}\n{raw.strip()}")
            material = "\n\n".join(notes)
            if omitted:
                material += f"\n\n（長い文書のため中ほどの {omitted} 区画は読んでいません）"
        self._phase(did, "AI がまとめています", job)
        existing = [t["tag"] for t in self.app.index.tags()][:60]
        raw = client.chat(
            "[TASK:ingest]\n次の文書を分析し、ナレッジノートにするための情報を JSON だけで出力してください。\n"
            '形式: {"title": "内容が分かる短いタイトル", "doc_type": "見積書/提案書/議事録/報告書/マニュアル/メール など",'
            ' "date": "文書の日付 (YYYY-MM-DD、不明なら空)", "summary": "3〜5 文の要約",'
            ' "points": ["要点（数字・条件・決定事項）", "…"],'
            ' "entities": {"customers": ["会社・組織名"], "people": ["人名（敬称なし）"], "products": ["製品・サービス名"],'
            ' "projects": ["案件名"], "others": ["その他の重要な用語"]},'
            ' "tags": ["タグ（既存タグを優先、スペースなし）"]}\n'
            "文書に書かれていないことは書かないでください。\n"
            f"# 既存のタグ\n{', '.join(existing) or '（なし）'}\n"
            f"# 元のファイル名\n{stem}\n# 文書{'（部分ごとの要点）' if len(parts) > 1 else ''}\n{material}",
            temperature=0.1)
        data = _json(raw)
        if not isinstance(data, dict):
            return {"summary": raw.strip()[:1500]}
        ents = data.get("entities") if isinstance(data.get("entities"), dict) else {}
        return {"title": str(data.get("title") or stem).strip()[:80] or stem,
                "summary": str(data.get("summary") or "").strip(),
                "points": _strs(data.get("points"), 15),
                "entities": {k: _strs(ents.get(k), 12) for k, _ in ENTITY_LABELS},
                "tags": [t.replace(" ", "") for t in _strs(data.get("tags"), 6)],
                "doc_type": str(data.get("doc_type") or "").strip()[:30],
                "date": str(data.get("date") or "").strip()[:10]}

    # ---- RAG: 既存のノート・資料から関連を探す
    def _related(self, did: str, info: dict, text: str, client: LLMClient | None) -> list[dict]:
        names = [n for k, _ in ENTITY_LABELS for n in info.get("entities", {}).get(k, [])]
        query = " ".join([info.get("title", ""), info.get("summary", "")[:400], " ".join(names)]).strip() or text[:800]
        d = self.get(did)
        exclude = {d["source"]} if d["origin"] == "doc" else set()
        hits = self.app.ai.retrieve(query, k=16, exclude=exclude)
        cands: dict[str, dict] = {}
        for h in hits:
            if h["path"] not in cands and h["path"] not in exclude:
                cands[h["path"]] = {"path": h["path"], "title": h["title"], "kind": h.get("kind", "note"),
                                    "snippet": h["text"][:160].replace("\n", " "), "score": h["score"], "reason": ""}
        cands = list(cands.values())[:8]
        if not cands:
            return []
        if client is None:
            return cands[:5]
        subject = (f"タイトル: {info.get('title', '')}\n要約: {info.get('summary', '')[:800]}\n"
                   f"登場する名前: {', '.join(names) or '（なし）'}")
        return self.app.ai.pick_related(subject, cands, client)[:8]

    # ---- 下書きの Markdown
    def compose(self, d: dict, info: dict, related: list[dict], text: str, original: str,
                opts: dict, model: str) -> str:
        today = time.strftime("%Y-%m-%d")
        fm = ["---", "種別: 取り込み資料"]
        if info.get("doc_type"):
            fm.append(f"文書の種類: {info['doc_type']}")
        if info.get("date"):
            fm.append(f"文書の日付: {info['date']}")
        fm.append(f"元の資料: [[{original}]]" if original else f"元のファイル: {d['name']}")
        fm.append(f"取り込み日: {today}")
        if model:
            fm.append(f"AI: {model}")
        fm.append("---")
        title = info.get("title") or Path(d["name"]).stem
        out = ["\n".join(fm), f"# {title}", ""]
        if info.get("summary"):
            out += ["> **要約** " + re.sub(r"\s*\n\s*", " ", info["summary"]), ""]
        if info.get("points"):
            out += ["## 要点"] + [f"- {p}" for p in info["points"]] + [""]
        ents = info.get("entities") or {}
        rows = []
        for key, label in ENTITY_LABELS:
            vals = []
            for name in ents.get(key, []):
                hit = self._match(name)
                item = self.app.index.get(hit) if hit else None
                if item and item["kind"] == "note":
                    vals.append(f"[[{item['title']}]]" if item["title"] == name else f"[[{item['title']}|{name}]]")
                elif item:
                    vals.append(f"[[{hit}|{name}]]")
                elif opts.get("link_new_names") and key != "others":
                    vals.append(f"[[{safe_name(name, name)}]]")
                else:
                    vals.append(name)
            if vals:
                rows.append(f"- {label}: " + "、".join(vals))
        if rows:
            out += ["## 登場する名前"] + rows + [""]
        if related:
            out += ["## 関連ノート"]
            for r in related:
                target = r["title"] if r.get("kind") == "note" and self.app.index.resolve(r["title"]) == r["path"] else r["path"]
                if target.lower().endswith(".md"):
                    target = target[:-3]
                out.append(f"- [[{target}]]" + (f" — {r['reason']}" if r.get("reason") else ""))
            out.append("")
        if opts.get("include_body") or not original:
            body = text if len(text) <= 60000 else text[:60000] + "\n\n（以下省略）"
            # 取り込んだ本文の見出しがノートの見出しより上にならないよう 1 段下げる
            body = re.sub(r"^(#{1,5}) ", lambda m: "#" + m.group(1) + " ", body, flags=re.M)
            out += ["## 本文", "", body, ""]
        tags = info.get("tags") or []
        out.append(" ".join(f"#{t}" for t in ["取り込み"] + [t for t in tags if t != "取り込み"]))
        return "\n".join(out).rstrip() + "\n"

    def _match(self, name: str) -> str | None:
        """名前に当たる既存のノート・資料。完全一致のほか「田中部長」→「田中部長（A社）」も拾う。"""
        hit = self.app.index.resolve(name)
        if hit or len(name) < 2:
            return hit
        low = name.lower()
        cands = [n for n in self.app.index.notes() if n["kind"] == "note"
                 and n["title"].lower().startswith(low) and n["title"][len(name):len(name) + 1] in ("（", "(")]
        return min(cands, key=lambda n: len(n["title"]))["path"] if cands else None

    # ---- 空いている名前
    def _free_note(self, rel: str) -> str:
        rel = normalize_rel(rel)
        base, n = rel[:-3], 2
        taken = {d["note_path"] for d in self._load() if d["status"] == "ready"}
        while self.app.vault.exists(rel) or rel in taken:
            rel = f"{base} ({n}).md"
            n += 1
        return rel

    def _free_file(self, rel: str) -> str:
        rel = rel.replace("\\", "/").strip("/")
        p = Path(rel)
        stem, ext, n = p.stem, p.suffix, 2
        parent = p.parent.as_posix()
        taken = {d["original_path"] for d in self._load() if d["status"] == "ready" and d["origin"] == "upload"}
        while (self.app.vault.root / rel).exists() or rel in taken:
            rel = f"{parent}/{stem} ({n}){ext}" if parent != "." else f"{stem} ({n}){ext}"
            n += 1
        return rel

    # ------------------------------------------------------------ 保存
    def save(self, ids: list[str]) -> dict:
        saved, errors = [], []
        for did in ids:
            try:
                saved.append(self._save_one(self.get(did)))
            except (VaultError, OSError) as e:
                errors.append({"id": did, "error": str(e)})
        new_paths = [p for s in saved for p in (s["path"], s.get("original")) if p]
        return {"saved": saved, "errors": errors, "paths": new_paths}

    def _save_one(self, d: dict) -> dict:
        if d["status"] != "ready":
            raise VaultError(f"「{d['name']}」はまだ下書きができていません")
        md, original = d["markdown"], d["original_path"]
        if d["origin"] == "upload" and original:
            target = self.app.vault.root / original
            if target.exists():                          # 下書きのあとに同名ができた
                new = self._free_file(original)
                md = md.replace(f"[[{original}]]", f"[[{new}]]")
                original, target = new, self.app.vault.root / new
            normalize_rel(Path(original).stem)           # 名前の検証（例外なら保存しない）
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self._abs(d), target)
            self.app.index.refresh(original)             # 原本を資料として登録（リンク先になる）
        note = d["note_path"]
        if self.app.vault.exists(note):
            note = self._free_note(note)
        r = self.app.create(note, text=md)
        self._update(d["id"], status="saved", saved_path=r["path"], markdown=md, original_path=original)
        shutil.rmtree(self.dir / "files" / d["id"], ignore_errors=True)
        return {"id": d["id"], "path": r["path"], "original": original if d["origin"] == "upload" else ""}
