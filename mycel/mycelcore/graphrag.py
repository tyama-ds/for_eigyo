"""GraphRAG（任意）: 文書から知識グラフを作り、グラフをたどって答える。

既定の質問（ai.ask）は「関連する段落を検索して LLM に渡す」通常の RAG のまま。
利用者が「GraphRAG を使う」を選んだときだけ、ここで作った索引を使う。索引作りには LLM を何度も呼ぶので、
「GraphRAG の索引を作る」を押したときだけ、変わった文書の分だけ行う。

構成
- 抽出: 段落ごとに LLM が実体（人物・組織・製品・概念・場所・出来事）と関係（説明・強さ）を JSON で返す。
  LLM が無ければ、人物・組織のルール抽出と同じ段落に出る共起で代用する
- グラフ: ``<vault>/.mycel/graphrag.sqlite``。nodes / edges / mentions（実体 ↔ 段落）
- コミュニティ: 重み付きラベル伝播で実体のかたまりを作り、かたまりごとに LLM で要約（変わったかたまりだけ作り直す）
- 質問:
  - 局所（local）: 質問に出てくる実体と、検索で当たった段落の実体から 1〜2 ホップ広げ、関係の説明・段落・
    関係するコミュニティ要約を根拠にする（「A社の田中部長は何を気にしているか」のような問い）
  - 全体（global）: コミュニティ要約ごとに部分回答を作り（map）、まとめる（reduce）（「全体の傾向は」のような問い）
  - 自動（auto）: 問いの言葉と実体の一致から局所／全体を選ぶ
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import unicodedata
from collections import Counter, defaultdict

from .config import chat_configured
from .entities import NOT_NAMES, extract_rule
from .index import Cancelled
from .llm import LLMClient, LLMError

SCHEMA_VERSION = 1
MODES = {"standard": "標準（段落の検索）", "auto": "GraphRAG（自動）", "local": "GraphRAG（局所：実体をたどる）",
         "global": "GraphRAG（全体：コミュニティ要約）"}
ENTITY_TYPES = {"person": "人物", "org": "組織", "product": "製品・サービス", "concept": "概念・技術", "place": "場所",
                "event": "出来事", "other": "その他"}
GLOBAL_WORDS = ("全体", "傾向", "まとめ", "概観", "俯瞰", "共通", "主な", "全部", "すべて", "一覧", "どんな種類", "テーマ",
                "overview", "overall", "summar", "trend", "themes")
_lock = threading.RLock()


def _key(name: str) -> str:
    s = unicodedata.normalize("NFKC", name or "").strip().lower()
    s = re.sub(r"[\s　]+", "", s)
    s = re.sub(r"(株式会社|有限会社|\(株\)|（株）|㈱)", "", s)
    return s[:60]


def _parse_json(raw: str):
    raw = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if m:
        raw = m.group(1)
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e <= s:
        return None
    try:
        return json.loads(raw[s:e + 1])
    except ValueError:
        return None


class GraphRAG:
    def __init__(self, app):
        self.app = app
        self.path = app.vault.internal / "graphrag.sqlite"
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        c = self.conn
        if c.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            for t in ("nodes", "edges", "mentions", "extracted", "communities", "meta"):
                c.execute(f"DROP TABLE IF EXISTS {t}")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS nodes(key TEXT PRIMARY KEY, name TEXT, type TEXT, descr TEXT);
            CREATE TABLE IF NOT EXISTS edges(src TEXT, dst TEXT, rel TEXT, weight REAL, path TEXT, chunk_id INTEGER);
            CREATE INDEX IF NOT EXISTS edges_src ON edges(src); CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst);
            CREATE INDEX IF NOT EXISTS edges_path ON edges(path);
            CREATE TABLE IF NOT EXISTS mentions(node TEXT, chunk_id INTEGER, path TEXT);
            CREATE INDEX IF NOT EXISTS mentions_node ON mentions(node); CREATE INDEX IF NOT EXISTS mentions_path ON mentions(path);
            CREATE TABLE IF NOT EXISTS extracted(path TEXT PRIMARY KEY, fp TEXT, method TEXT, at REAL);
            CREATE TABLE IF NOT EXISTS communities(id INTEGER PRIMARY KEY, title TEXT, summary TEXT, members TEXT,
                size INTEGER, fp TEXT, method TEXT);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """)
        c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        c.commit()

    def close(self) -> None:
        with _lock:
            self.conn.close()

    # ------------------------------------------------------------ 状態
    def _fp(self, item: dict) -> str:
        return f"{item.get('mtime_ns')}-{item.get('size')}"

    def _targets(self, prefixes=None) -> list[dict]:
        tpl = self.app.config().get("template_folder", "").strip().strip("/")
        out = []
        for n in self.app.index.notes():
            if n["status"] != "ok":
                continue
            if tpl and (n["path"] == tpl or n["path"].startswith(tpl + "/")):
                continue
            if prefixes is not None and not any(self.app.index._under(n["path"], p) for p in prefixes):
                continue
            out.append(n)
        return out

    def status(self) -> dict:
        with _lock:
            c = self.conn
            nodes = c.execute("SELECT count(*) FROM nodes").fetchone()[0]
            edges = c.execute("SELECT count(*) FROM edges").fetchone()[0]
            comms = c.execute("SELECT count(*) FROM communities").fetchone()[0]
            summarized = c.execute("SELECT count(*) FROM communities WHERE method='llm'").fetchone()[0]
            done = {r["path"]: (r["fp"], r["method"]) for r in c.execute("SELECT * FROM extracted")}
            built = c.execute("SELECT value FROM meta WHERE key='built'").fetchone()
        items = self._targets()
        pending = sum(1 for n in items if done.get(n["path"], ("", ""))[0] != self._fp(n))
        llm_docs = sum(1 for n in items if done.get(n["path"], ("", ""))[1] == "llm")
        return {"nodes": nodes, "edges": edges, "communities": comms, "summarized": summarized,
                "documents": len(items), "extracted": len(items) - pending, "pending": pending, "llm_docs": llm_docs,
                "built": float(built["value"]) if built else None, "llm": chat_configured(self.app.config()),
                "ready": nodes > 0}

    # ------------------------------------------------------------ 索引作り
    def build(self, prefixes=None, job=None, use_llm: bool = True) -> dict:
        """変わった文書だけ実体・関係を抽出し直し、コミュニティを作り直す。"""
        cfg = self.app.config()
        use_llm = use_llm and chat_configured(cfg)
        client = LLMClient(cfg) if use_llm else None
        items = self._targets(prefixes)
        with _lock:
            done = {r["path"]: (r["fp"], r["method"]) for r in self.conn.execute("SELECT * FROM extracted")}
            known = {r["path"] for r in self.conn.execute("SELECT path FROM extracted")}
        current = {n["path"] for n in self._targets()}
        for gone in known - current:
            self._forget(gone)
        todo = [n for n in items if done.get(n["path"], ("", ""))[0] != self._fp(n)
                or (use_llm and done[n["path"]][1] != "llm")]
        errors = 0
        for i, n in enumerate(todo):
            if job is not None:
                if job.cancel.is_set():
                    raise Cancelled()
                job.progress("GraphRAG: 実体と関係の抽出", i, len(todo), n["path"])
            try:
                self._extract_doc(n, client)
            except LLMError:
                errors += 1
                if errors >= 3 and errors > i // 2:
                    raise
        if job is not None:
            job.progress("GraphRAG: コミュニティの検出", 0, 0, "")
        n_comm = self._communities(client, job)
        with _lock:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES('built', ?)", (str(time.time()),))
            self.conn.commit()
        return {"extracted": len(todo) - errors, "errors": errors, "communities": n_comm, **self.status()}

    def _forget(self, path: str) -> None:
        with _lock:
            c = self.conn
            c.execute("DELETE FROM edges WHERE path=?", (path,))
            c.execute("DELETE FROM mentions WHERE path=?", (path,))
            c.execute("DELETE FROM extracted WHERE path=?", (path,))
            c.execute("DELETE FROM nodes WHERE key NOT IN (SELECT node FROM mentions)")
            c.commit()

    def _extract_doc(self, item: dict, client: LLMClient | None) -> None:
        path = item["path"]
        chunks = self.app.index.chunks_of(path)
        title = item["title"]
        found_nodes: dict[str, dict] = {}
        found_edges: list[tuple] = []
        found_mentions: list[tuple] = []
        max_chunks = int(self.app.config().get("graphrag_max_chunks") or 40)
        for c in chunks[:max_chunks]:
            text = c["text"]
            ents, rels = ([], [])
            if client is not None:
                ents, rels = self._extract_llm(client, title, text)
            if not ents:                                 # LLM なし／読めなかったときはルール抽出＋共起
                ents, rels = self._extract_rule(text)
            keys = {}
            for e in ents:
                k = _key(e["name"])
                if not k or k in NOT_NAMES or len(k) < 2:
                    continue
                keys[e["name"]] = k
                cur = found_nodes.get(k)
                if cur is None:
                    found_nodes[k] = {"name": e["name"], "type": e.get("type", "other"), "descr": e.get("descr", "")}
                elif e.get("descr") and e["descr"] not in cur["descr"]:
                    cur["descr"] = (cur["descr"] + " " + e["descr"]).strip()[:600]
                found_mentions.append((k, c["id"], path))
            for r in rels:
                a, b = keys.get(r["source"]) or _key(r["source"]), keys.get(r["target"]) or _key(r["target"])
                if a and b and a != b and a in found_nodes and b in found_nodes:
                    found_edges.append((a, b, r.get("descr", ""), float(r.get("weight") or 1.0), path, c["id"]))
        with _lock:
            cc = self.conn
            cc.execute("DELETE FROM edges WHERE path=?", (path,))
            cc.execute("DELETE FROM mentions WHERE path=?", (path,))
            for k, nd in found_nodes.items():
                row = cc.execute("SELECT name, type, descr FROM nodes WHERE key=?", (k,)).fetchone()
                if row is None:
                    cc.execute("INSERT INTO nodes VALUES(?,?,?,?)", (k, nd["name"][:80], nd["type"], nd["descr"][:600]))
                else:
                    descr = row["descr"]
                    if nd["descr"] and nd["descr"] not in descr:
                        descr = (descr + " " + nd["descr"]).strip()[:900]
                    typ = row["type"] if row["type"] != "other" else nd["type"]
                    cc.execute("UPDATE nodes SET descr=?, type=? WHERE key=?", (descr, typ, k))
            cc.executemany("INSERT INTO edges VALUES(?,?,?,?,?,?)", found_edges)
            cc.executemany("INSERT INTO mentions VALUES(?,?,?)", found_mentions)
            cc.execute("DELETE FROM nodes WHERE key NOT IN (SELECT node FROM mentions)")
            cc.execute("INSERT OR REPLACE INTO extracted VALUES(?,?,?,?)",
                       (path, self._fp(item), "llm" if client is not None else "rule", time.time()))
            cc.commit()

    def _extract_llm(self, client: LLMClient, title: str, text: str) -> tuple[list, list]:
        raw = client.chat(
            "[TASK:kg]\n次の文章から、実体（人物・組織・製品やサービス・概念や技術・場所・出来事）と、実体同士の関係を抜き出して "
            "JSON だけで出力してください。\n"
            '形式: {"entities": [{"name": "名前", "type": "person/org/product/concept/place/event/other", "descr": "一文の説明"}],'
            ' "relations": [{"source": "名前", "target": "名前", "descr": "関係の説明（一文）", "weight": 1〜10}]}\n'
            "文章に書かれていない実体・関係は出さないでください。名前は文章の表記に合わせ、敬称・肩書は除いてください。\n\n"
            f"# 文書「{title}」の一部\n{text[:3000]}", temperature=0.0)
        data = _parse_json(raw)
        if not isinstance(data, dict):
            return [], []
        ents = [{"name": str(e.get("name") or "").strip(), "type": e.get("type") if e.get("type") in ENTITY_TYPES else "other",
                 "descr": str(e.get("descr") or "").strip()[:300]}
                for e in (data.get("entities") or []) if isinstance(e, dict) and str(e.get("name") or "").strip()]
        rels = [{"source": str(r.get("source") or "").strip(), "target": str(r.get("target") or "").strip(),
                 "descr": str(r.get("descr") or "").strip()[:300], "weight": r.get("weight") or 1}
                for r in (data.get("relations") or []) if isinstance(r, dict)]
        return ents, rels

    @staticmethod
    def _extract_rule(text: str) -> tuple[list, list]:
        ents = []
        for it in extract_rule(text):
            name = it["name"]
            if it["type"] == "person":
                from .entities import person_display
                name = person_display(name)
            ents.append({"name": name, "type": it["type"] if it["type"] in ENTITY_TYPES else "other",
                         "descr": (it.get("role") or it.get("title") or "")})
        rels = []
        for i in range(len(ents)):
            for j in range(i + 1, len(ents)):
                rels.append({"source": ents[i]["name"], "target": ents[j]["name"], "descr": "同じ段落に出てくる", "weight": 1})
        return ents, rels[:30]

    # ------------------------------------------------------------ コミュニティ
    def _adjacency(self) -> dict[str, dict[str, float]]:
        adj: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        with _lock:
            for r in self.conn.execute("SELECT src, dst, weight FROM edges"):
                adj[r["src"]][r["dst"]] += r["weight"]
                adj[r["dst"]][r["src"]] += r["weight"]
        return adj

    @staticmethod
    def _label_propagation(nodes: list[str], adj, rounds: int = 20) -> dict[str, str]:
        label = {n: n for n in nodes}
        order = sorted(nodes)
        for _ in range(rounds):
            changed = 0
            for n in order:
                nb = adj.get(n)
                if not nb:
                    continue
                score: dict[str, float] = defaultdict(float)
                for m, w in nb.items():
                    if m in label:
                        score[label[m]] += w
                if not score:
                    continue
                best = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                if best != label[n]:
                    label[n] = best
                    changed += 1
            if not changed:
                break
        return label

    def _communities(self, client: LLMClient | None, job=None) -> int:
        adj = self._adjacency()
        with _lock:
            nodes = [r["key"] for r in self.conn.execute("SELECT key FROM nodes")]
            names = {r["key"]: r for r in self.conn.execute("SELECT key, name, type, descr FROM nodes")}
            old = {r["fp"]: dict(r) for r in self.conn.execute("SELECT * FROM communities")}
        label = self._label_propagation(nodes, adj)
        groups: dict[str, list[str]] = defaultdict(list)
        for n, l in label.items():
            groups[l].append(n)
        # 大きすぎるかたまりは中でもう一度分ける
        final: list[list[str]] = []
        for members in groups.values():
            if len(members) > 40:
                sub = {m: {k: w for k, w in adj.get(m, {}).items() if k in set(members)} for m in members}
                lab2 = self._label_propagation(members, sub)
                g2: dict[str, list[str]] = defaultdict(list)
                for m, l in lab2.items():
                    g2[l].append(m)
                final.extend(g2.values())
            else:
                final.append(members)
        final = [sorted(m) for m in final if len(m) >= 2]
        final.sort(key=lambda m: -len(m))
        rows = []
        for i, members in enumerate(final[:200]):
            if job is not None and job.cancel.is_set():
                raise Cancelled()
            fp = "|".join(members)
            prev = old.get(fp)
            if prev and (prev["method"] == "llm" or client is None):
                rows.append((prev["title"], prev["summary"], json.dumps(members), len(members), fp, prev["method"]))
                continue
            top = sorted(members, key=lambda m: -sum(adj.get(m, {}).values()))[:12]
            title = "・".join(names[m]["name"] for m in top[:3])
            summary, method = self._fallback_summary(members, names, adj), "rule"
            if client is not None:
                if job is not None:
                    job.progress("GraphRAG: コミュニティの要約", i, min(len(final), 200), title)
                try:
                    title, summary = self._summarize(client, members, names, adj)
                    method = "llm"
                except LLMError:
                    pass
            rows.append((title[:80], summary, json.dumps(members), len(members), fp, method))
        with _lock:
            self.conn.execute("DELETE FROM communities")
            self.conn.executemany("INSERT INTO communities(title, summary, members, size, fp, method) VALUES(?,?,?,?,?,?)", rows)
            self.conn.commit()
        return len(rows)

    def _relations_text(self, members: list[str], names: dict, limit: int = 40) -> str:
        ms = set(members)
        q = ",".join("?" * len(members))
        with _lock:
            rows = self.conn.execute(
                f"SELECT src, dst, rel, sum(weight) w FROM edges WHERE src IN ({q}) AND dst IN ({q}) "
                "GROUP BY src, dst, rel ORDER BY w DESC LIMIT ?", (*members, *members, limit)).fetchall()
        return "\n".join(f"- {names[r['src']]['name']} — {names[r['dst']]['name']}: {r['rel'] or '関係あり'}"
                         for r in rows if r["src"] in ms and r["dst"] in ms)

    def _fallback_summary(self, members: list[str], names: dict, adj) -> str:
        top = sorted(members, key=lambda m: -sum(adj.get(m, {}).values()))[:10]
        ents = "、".join(f"{names[m]['name']}（{ENTITY_TYPES.get(names[m]['type'], 'その他')}）" for m in top)
        return f"主な実体: {ents}\n主な関係:\n{self._relations_text(members, names, 15)}"

    def _summarize(self, client: LLMClient, members: list[str], names: dict, adj) -> tuple[str, str]:
        top = sorted(members, key=lambda m: -sum(adj.get(m, {}).values()))[:25]
        ents = "\n".join(f"- {names[m]['name']}（{ENTITY_TYPES.get(names[m]['type'], 'その他')}）: {names[m]['descr'][:160]}" for m in top)
        raw = client.chat(
            "[TASK:kg_community]\n次は、社内の文書から見つかった、互いに関係の深い実体のかたまりです。"
            "このかたまりが何についてのものか、題名（20 字以内）と要約（主な実体・関係・論点を 200〜400 字）を JSON だけで出力してください。\n"
            '形式: {"title": "題名", "summary": "要約"}\n実体の説明に書かれていないことは書かないでください。\n\n'
            f"# 実体\n{ents}\n# 関係\n{self._relations_text(members, names)}", temperature=0.1)
        data = _parse_json(raw)
        if not isinstance(data, dict) or not data.get("summary"):
            raise LLMError("コミュニティ要約の応答を読めませんでした")
        return str(data.get("title") or "")[:80] or names[top[0]]["name"], str(data["summary"])[:3000]

    # ------------------------------------------------------------ 質問
    def ask(self, question: str, mode: str = "auto", history=None, prefixes=None, paths=None) -> dict:
        question = (question or "").strip()
        if not question:
            raise LLMError("質問を入力してください")
        st = self.status()
        if not st["ready"]:
            return {"answer": "", "sources": [], "llm": False, "mode": mode,
                    "message": "GraphRAG の索引がまだありません。AI タブの「GraphRAG の索引を作る」で作ってください（標準の質問はそのまま使えます）。"}
        if mode == "auto":
            mode = self._pick_mode(question)
        res = self._ask_global(question, history) if mode == "global" else self._ask_local(question, history, prefixes, paths)
        res["mode"] = mode
        res["index"] = {"built": st["built"], "pending": st["pending"]}
        return res

    def _match_entities(self, question: str) -> list[str]:
        q = unicodedata.normalize("NFKC", question).lower()
        with _lock:
            rows = self.conn.execute("SELECT key, name FROM nodes").fetchall()
        hits = []
        for r in rows:
            nm = re.sub(r"[\s　]+", "", r["name"].lower())
            if len(nm) >= 2 and nm in q.replace(" ", ""):
                hits.append((len(nm), r["key"]))
        return [k for _, k in sorted(hits, reverse=True)][:10]

    def _pick_mode(self, question: str) -> str:
        ql = question.lower()
        if self._match_entities(question):
            return "local"
        if any(w in ql for w in GLOBAL_WORDS):
            return "global"
        return "local"

    def _ask_local(self, question: str, history, prefixes, paths) -> dict:
        cfg = self.app.config()
        hits = self.app.ai.retrieve(question, k=8, prefixes=prefixes, paths=paths)
        seeds = list(dict.fromkeys(self._match_entities(question)))
        hit_ids = [h["id"] for h in hits]
        with _lock:
            if hit_ids:
                q = ",".join("?" * len(hit_ids))
                for r in self.conn.execute(f"SELECT node, count(*) n FROM mentions WHERE chunk_id IN ({q}) "
                                           "GROUP BY node ORDER BY n DESC LIMIT 12", hit_ids):
                    if r["node"] not in seeds:
                        seeds.append(r["node"])
        adj = self._adjacency()
        scores: dict[str, float] = {s: 3.0 if i < 10 else 2.0 for i, s in enumerate(seeds)}
        for s in list(seeds):
            for m, w in sorted(adj.get(s, {}).items(), key=lambda kv: -kv[1])[:8]:
                scores[m] = scores.get(m, 0.0) + min(w, 5.0) / 5.0
        ents = [k for k, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:25]]
        if not ents:
            res = self.app.ai.ask(question, history=history, prefixes=prefixes, paths=paths)
            res["graph"] = {"entities": [], "relations": [], "communities": []}
            res["message"] = (res.get("message") or "") or "グラフ上に一致する実体が無かったため、標準の段落検索で答えました。"
            return res
        q = ",".join("?" * len(ents))
        with _lock:
            names = {r["key"]: dict(r) for r in self.conn.execute(f"SELECT * FROM nodes WHERE key IN ({q})", ents)}
            rels = self.conn.execute(
                f"SELECT src, dst, rel, sum(weight) w, path FROM edges WHERE src IN ({q}) AND dst IN ({q}) "
                "GROUP BY src, dst, rel ORDER BY w DESC LIMIT 30", (*ents, *ents)).fetchall()
            extra_chunks = [r["chunk_id"] for r in self.conn.execute(
                f"SELECT chunk_id, count(*) n FROM mentions WHERE node IN ({q}) GROUP BY chunk_id ORDER BY n DESC LIMIT 12", ents)]
            comms = [dict(r) for r in self.conn.execute("SELECT id, title, summary, members FROM communities")]
        ent_set = set(ents)
        comm_hits = sorted(((len(ent_set & set(json.loads(c["members"]))), c) for c in comms), key=lambda x: -x[0])
        comm_hits = [c for n, c in comm_hits if n > 0][:3]
        chunk_rows = self.app.index.get_chunks(list(dict.fromkeys(hit_ids + extra_chunks)))
        ordered = [chunk_rows[i] for i in hit_ids if i in chunk_rows] + [chunk_rows[i] for i in extra_chunks if i in chunk_rows and i not in hit_ids]
        ordered = ordered[:10]
        sources, blocks = [], []
        for n, c in enumerate(ordered, 1):
            sources.append({"n": n, "path": c["path"], "title": c["title"], "heading": c["heading"]})
            label = "ノート" if c.get("kind", "note") == "note" else "資料"
            blocks.append(f"[{n}] {label}「{c['title']}」\n{c['text'][:1200]}")
        graph = {"entities": [{"name": names[k]["name"], "type": names[k]["type"], "descr": names[k]["descr"][:160]} for k in ents if k in names],
                 "relations": [{"source": names[r["src"]]["name"], "target": names[r["dst"]]["name"], "descr": r["rel"], "path": r["path"]}
                               for r in rels if r["src"] in names and r["dst"] in names],
                 "communities": [{"id": c["id"], "title": c["title"]} for c in comm_hits]}
        if not chat_configured(cfg):
            return {"answer": "", "sources": sources, "graph": graph, "llm": False,
                    "message": "LLM が未設定のため、グラフでたどった実体・関係と関連する段落だけを表示しています。"}
        ent_txt = "\n".join(f"- {e['name']}（{ENTITY_TYPES.get(e['type'], 'その他')}）: {e['descr']}" for e in graph["entities"][:20])
        rel_txt = "\n".join(f"- {r['source']} — {r['target']}: {r['descr'] or '関係あり'}" for r in graph["relations"][:25])
        comm_txt = "\n\n".join(f"## {c['title']}\n{c['summary'][:1200]}" for c in comm_hits)
        system = ("あなたはユーザーのノート・資料から作った知識グラフと抜粋に基づいて答えるアシスタントです。"
                  "実体と関係、コミュニティの要約、文書の抜粋だけを根拠に、日本語で簡潔に答えてください。"
                  "根拠にした文書は [[名前]] の形で示し、グラフから推定した部分は「推定」と書いてください。"
                  "書かれていないことは「ノートや資料には記載がありません」と答えてください。")
        prompt = ("[TASK:kg_local]\n# 関係する実体\n" + (ent_txt or "（なし）") + "\n\n# 実体同士の関係\n" + (rel_txt or "（なし）")
                  + ("\n\n# 関係するコミュニティの要約\n" + comm_txt if comm_txt else "")
                  + "\n\n# 文書の抜粋\n" + ("\n\n".join(blocks) or "（該当なし）") + f"\n\n# 質問\n{question}")
        messages = [{"role": t["role"], "content": t["content"][:4000]} for t in (history or [])[-6:]
                    if isinstance(t, dict) and t.get("role") in ("user", "assistant") and isinstance(t.get("content"), str)]
        messages.append({"role": "user", "content": prompt})
        answer = LLMClient(cfg).chat(messages, system=system)
        return {"answer": answer, "sources": sources, "graph": graph, "llm": True}

    def _ask_global(self, question: str, history) -> dict:
        cfg = self.app.config()
        limit = int(cfg.get("graphrag_max_communities") or 12)
        with _lock:
            comms = [dict(r) for r in self.conn.execute("SELECT id, title, summary, members, size FROM communities ORDER BY size DESC LIMIT ?", (limit,))]
        graph = {"entities": [], "relations": [], "communities": [{"id": c["id"], "title": c["title"], "size": c["size"]} for c in comms]}
        if not comms:
            return {"answer": "", "sources": [], "graph": graph, "llm": False, "message": "コミュニティがまだありません（索引を作り直してください）。"}
        if not chat_configured(cfg):
            return {"answer": "", "sources": [], "graph": graph, "llm": False,
                    "message": "LLM が未設定のため、コミュニティの一覧だけを表示しています。\n\n" + "\n\n".join(f"## {c['title']}\n{c['summary'][:400]}" for c in comms[:8])}
        client = LLMClient(cfg)
        partials = []
        for c in comms:                                  # map: コミュニティごとの部分回答
            raw = client.chat(
                "[TASK:kg_map]\n次のコミュニティ要約だけを根拠に、質問に答えられる点を書いてください。"
                '関係なければ score を 0 にしてください。JSON だけ: {"answer": "部分的な答え（根拠つき）", "score": 0〜100}\n\n'
                f"# コミュニティ「{c['title']}」\n{c['summary'][:2500]}\n\n# 質問\n{question}", temperature=0.0)
            data = _parse_json(raw)
            if isinstance(data, dict):
                try:
                    score = float(data.get("score") or 0)
                except (TypeError, ValueError):
                    score = 0.0
                if score > 0 and data.get("answer"):
                    partials.append({"id": c["id"], "title": c["title"], "answer": str(data["answer"])[:1500], "score": score})
        partials.sort(key=lambda p: -p["score"])
        graph["used"] = [{"id": p["id"], "title": p["title"], "score": p["score"]} for p in partials]
        if not partials:
            return {"answer": "ノートや資料には記載がありません（どのコミュニティ要約も質問に関係しませんでした）。", "sources": [], "graph": graph, "llm": True}
        parts = "\n\n".join(f"## [{i}] {p['title']}（関連度 {int(p['score'])}）\n{p['answer']}" for i, p in enumerate(partials[:10], 1))
        answer = client.chat(
            "[TASK:kg_reduce]\nコミュニティごとの部分回答をまとめて、質問への最終回答を日本語で書いてください。"
            "重要な点から順に、根拠になったコミュニティを [番号] で示し、部分回答に無いことは書かないでください。\n\n"
            f"# 部分回答\n{parts}\n\n# 質問\n{question}", system="あなたは複数の要約を統合して全体像を答えるアシスタントです。", temperature=0.2)
        sources = [{"n": i, "path": "", "title": p["title"], "heading": "コミュニティ要約", "community": p["id"]} for i, p in enumerate(partials[:10], 1)]
        return {"answer": answer, "sources": sources, "graph": graph, "llm": True}

    # ------------------------------------------------------------ 参照（画面用）
    def communities(self, limit: int = 50) -> list[dict]:
        with _lock:
            rows = self.conn.execute("SELECT id, title, summary, members, size, method FROM communities ORDER BY size DESC LIMIT ?", (limit,)).fetchall()
            names = {r["key"]: r["name"] for r in self.conn.execute("SELECT key, name FROM nodes")}
        return [{"id": r["id"], "title": r["title"], "summary": r["summary"], "size": r["size"], "method": r["method"],
                 "members": [names.get(k, k) for k in json.loads(r["members"])][:30]} for r in rows]

    def entity(self, key: str) -> dict:
        with _lock:
            n = self.conn.execute("SELECT * FROM nodes WHERE key=?", (key,)).fetchone()
            if not n:
                return {}
            rels = self.conn.execute("SELECT src, dst, rel, weight, path FROM edges WHERE src=? OR dst=? ORDER BY weight DESC LIMIT 40", (key, key)).fetchall()
            paths = [r["path"] for r in self.conn.execute("SELECT DISTINCT path FROM mentions WHERE node=?", (key,))]
            names = {r["key"]: r["name"] for r in self.conn.execute("SELECT key, name FROM nodes")}
        return {**dict(n), "relations": [{"other": names.get(r["dst"] if r["src"] == key else r["src"], ""), "descr": r["rel"], "path": r["path"]} for r in rels],
                "paths": paths}
