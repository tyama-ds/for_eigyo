"""人物・組織の画面向けの操作（抽出の同期・一覧・詳細・AI による推定・グラフ）。"""
from __future__ import annotations

import re
from collections import Counter

from . import links as L
from .config import chat_configured
from .entities import (O, P, EntityStore, extract_rule, llm_items, merge_items, org_key, parse_llm_json,
                       person_key, split_paren, strip_person)
from .index import Cancelled
from .ingest import _split
from .llm import LLMClient, LLMError
from .vault import VaultError

KIND_LABEL = {P: "人物", O: "組織"}


def _fp(item: dict) -> str:
    return f"{item.get('mtime_ns')}-{item.get('size')}"


class People:
    def __init__(self, app, store: EntityStore):
        self.app = app
        self.store = store
        self._synced_rev = -1

    @property
    def index(self):
        return self.app.index

    # ------------------------------------------------------------ 抽出（ルール・差分だけ）
    def sync(self, force: bool = False) -> int:
        """インデックスと比べて、変わった文書だけルールで抽出し直す。AI で抽出済みで変わっていない文書はそのまま。"""
        if not force and self._synced_rev == self.index.rev:
            return 0
        rev = self.index.rev
        items = {n["path"]: n for n in self.index.notes() if n["status"] == "ok"}
        done = self.store.extracted()
        n = 0
        for path in set(done) - set(items):
            self.store.forget(path)
        tpl = self._template_folder()
        for path, it in items.items():
            if tpl and (path == tpl or path.startswith(tpl + "/")):
                continue
            fp = _fp(it)
            if done.get(path, ("", ""))[0] == fp:
                continue
            full = self.index.get(path)
            if full is None:
                continue
            self.store.put(path, extract_rule(full["text"] or ""), fp, "rule")
            n += 1
        self._synced_rev = rev
        return n

    def _template_folder(self) -> str:
        return self.app.config().get("template_folder", "").strip().strip("/")

    def status(self) -> dict:
        self.sync()
        done = self.store.extracted()
        items = [n for n in self.index.notes() if n["status"] == "ok"]
        ai = sum(1 for n in items if done.get(n["path"], ("", ""))[1] == "llm" and done[n["path"]][0] == _fp(n))
        m = self.store.model()
        return {"documents": len(items), "ai_extracted": ai,
                "people": sum(1 for (t, _) in m["ents"] if t == P), "orgs": sum(1 for (t, _) in m["ents"] if t == O),
                "llm": chat_configured(self.app.config())}

    # ------------------------------------------------------------ 抽出（AI・手動）
    def extract_ai(self, prefixes: list[str] | None, job=None) -> dict:
        """まだ AI で読んでいない（または変わった）文書から、人物・組織と立場を抽出する。"""
        cfg = self.app.config()
        if not chat_configured(cfg):
            raise LLMError("LLM が未設定です（設定の「LLM」でローカル LLM を登録してください）")
        self.sync(force=True)
        client = LLMClient(cfg)
        done = self.store.extracted()
        tpl = self._template_folder()
        todo = [n for n in self.index.notes() if n["status"] == "ok"
                and (prefixes is None or any(self.index._under(n["path"], p) for p in prefixes))
                and not (tpl and (n["path"] == tpl or n["path"].startswith(tpl + "/")))
                and done.get(n["path"], ("", ""))[1] != "llm"]
        size = int(cfg.get("ingest_chunk_chars") or 3000)
        errors = 0
        for i, n in enumerate(todo):
            if job is not None:
                if job.cancel.is_set():
                    raise Cancelled()
                job.progress("人物・組織を AI で抽出", i, len(todo), n["path"])
            full = self.index.get(n["path"])
            if not full:
                continue
            try:
                items = self.extract_llm(full["title"], full["text"] or "", client, size)
            except LLMError:
                errors += 1
                if errors >= 3 and errors > i // 2:
                    raise
                continue
            self.store.put(n["path"], merge_items(items, extract_rule(full["text"] or "")), _fp(full), "llm")
        return {"extracted": len(todo) - errors, "errors": errors}

    def extract_llm(self, title: str, text: str, client: LLMClient, size: int = 3000, max_parts: int = 4) -> list[dict]:
        parts = _split(text, size) or [""]
        if len(parts) > max_parts:                  # 長い文書は先頭寄りに読む（登場人物は冒頭に多い）
            parts = parts[:max_parts - 1] + parts[-1:]
        out: list[dict] = []
        for i, part in enumerate(parts, 1):
            raw = client.chat(
                "[TASK:entities]\n次の文書" + (f"（{i}/{len(parts)} 部分）" if len(parts) > 1 else "") +
                f"「{title}」に出てくる人物と組織（会社・団体・部署を除く組織）を抜き出してください。\n"
                '出力は JSON 配列だけ: [{"name": "名前（人物は敬称・肩書を除く）", "type": "person または org",'
                ' "title": "肩書（人物のみ・不明なら空）", "org": "所属組織（人物のみ・不明なら空）",'
                ' "role": "この文書での立場（例: 決裁者・窓口・担当・出席者・差出人・宛先・作成者・顧客・競合・仕入先）",'
                ' "evidence": "根拠になる短い引用"}]\n'
                "文書に書かれていない人物・組織は出さないでください。自社の人も含めてかまいません。\n\n# 文書\n" + part,
                temperature=0.0)
            out = merge_items(out, llm_items(parse_llm_json(raw)))
        return out

    def put_from_ingest(self, path: str, ents: dict, text: str) -> None:
        """AI 取り込みで LLM が見つけた名前を、保存したノート（と原本）の人物・組織として登録する。"""
        item = self.index.get(path)
        if not item:
            return
        raw = [{"name": n, "type": "org", "role": "顧客"} for n in ents.get("customers", [])]
        raw += [{"name": n, "type": "person"} for n in ents.get("people", [])]
        self.store.put(path, merge_items(llm_items(raw), extract_rule(text)), _fp(item), "llm")

    # ------------------------------------------------------------ 一覧と詳細
    def _ent(self, kind: str, key: str) -> dict:
        self.sync()
        e = self.store.model()["ents"].get((kind, key))
        if not e:
            raise VaultError("見つかりません（名前をまとめた・外した可能性があります）", 404)
        return e

    def _org_name(self, key: str) -> str:
        e = self.store.model()["ents"].get((O, key))
        return e["display"] if e else key

    def list(self, kind: str = "", q: str = "", limit: int = 500) -> list[dict]:
        self.sync()
        q = (q or "").strip().lower()
        out = []
        for (t, k), e in self.store.model()["ents"].items():
            if kind and t != kind:
                continue
            if q and q not in e["display"].lower() and not any(q in x for x in e["keys"]):
                continue
            org = e["orgs"].most_common(1)[0][0] if e["orgs"] else ""
            out.append({"type": t, "key": k, "name": e["display"], "docs": len(e["paths"]),
                        "org": self._org_name(org) if org else "",
                        "title": e["titles"].most_common(1)[0][0] if e["titles"] else "",
                        "role": e["roles"].most_common(1)[0][0] if e["roles"] else ""})
        out.sort(key=lambda x: (-x["docs"], x["name"]))
        return out[:limit]

    def of(self, path: str) -> list[dict]:
        """この文書に出てくる人物・組織（立場つき）。"""
        self.sync()
        m = self.store.model()
        out = []
        for (t, k) in sorted(m["path_ents"].get(path, set())):
            e = m["ents"].get((t, k))
            if not e:
                continue
            rows = e["paths"].get(path, [])
            roles = sorted({r["role"] for r in rows if r["role"]})
            titles = sorted({r["title"] for r in rows if r["title"]})
            out.append({"type": t, "key": k, "name": e["display"], "roles": roles, "titles": titles,
                        "manual": any(r["method"] == "user" for r in rows),
                        "method": "llm" if any(r["method"] == "llm" for r in rows) else rows[0]["method"] if rows else "",
                        "docs": len(e["paths"])})
        out.sort(key=lambda x: (x["type"] != P, -x["docs"], x["name"]))
        return out

    def detail(self, kind: str, key: str, related: bool = True) -> dict:
        e = self._ent(kind, key)
        m = self.store.model()
        apps = []
        for path, rows in e["paths"].items():
            it = self.index.get(path)
            if not it:
                continue
            ctx = [r["context"] for r in rows if r["context"]]
            apps.append({"path": path, "title": it["title"], "kind": it["kind"], "grp": it["grp"],
                         "folder": it["folder"], "mtime_ns": it["mtime_ns"],
                         "roles": sorted({r["role"] for r in rows if r["role"]}),
                         "titles": sorted({r["title"] for r in rows if r["title"]}),
                         "context": ctx[0] if ctx else "", "manual": any(r["method"] == "user" for r in rows),
                         "method": "llm" if any(r["method"] == "llm" for r in rows) else rows[0]["method"]})
        apps.sort(key=lambda a: -(a["mtime_ns"] or 0))
        paths = {a["path"] for a in apps}
        co: Counter = Counter()
        for p in paths:
            for tk in m["path_ents"].get(p, ()):
                if tk != (kind, key):
                    co[tk] += 1
        def ent_row(tk, n):
            f = m["ents"].get(tk)
            return {"type": tk[0], "key": tk[1], "name": f["display"] if f else tk[1], "count": n}
        co_people = [ent_row(tk, n) for tk, n in co.most_common() if tk[0] == P][:15]
        co_orgs = [ent_row(tk, n) for tk, n in co.most_common() if tk[0] == O][:10]
        out = {"type": kind, "key": key, "name": e["display"], "kind_label": KIND_LABEL[kind],
               "titles": [{"name": t, "count": n} for t, n in e["titles"].most_common(5)],
               "roles": [{"name": r, "count": n} for r, n in e["roles"].most_common(8)],
               "appearances": apps, "co_people": co_people, "co_orgs": co_orgs,
               "aliases": self._aliases(kind, key, e), "note": self._note_for(kind, key)}
        if kind == P:
            explicit = [{"key": k, "name": self._org_name(k), "count": n} for k, n in e["orgs"].most_common(5)]
            ex_keys = {x["key"] for x in explicit}
            inferred = [{"key": r["key"], "name": r["name"], "count": r["count"]} for r in co_orgs
                        if r["key"] not in ex_keys][:3]
            out["affiliations"] = explicit
            out["affiliations_inferred"] = inferred if not explicit else []
            out["ambiguous"] = len(explicit) > 1
        else:
            members = Counter()
            for (t, k2), f in m["ents"].items():
                if t == P and key in f["orgs"]:
                    members[k2] += f["orgs"][key]
            out["members"] = [ent_row((P, k2), n) for k2, n in members.most_common(20)]
        out["related"] = self._related(out, paths) if related and apps else []
        return out

    def _aliases(self, kind: str, key: str, e: dict) -> list[dict]:
        m = self.store.model()
        user = {k for (t, k), tgt in m["aliases"].items() if t == kind and tgt == key}
        names = []
        for k in sorted(e["keys"] | user):
            if k == key:
                continue
            names.append({"key": k, "user": k in user})
        return names

    def _note_for(self, kind: str, key: str) -> str:
        for n in self.index.notes():
            if n["kind"] != "note":
                continue
            base, _ = split_paren(n["title"])
            if kind == P and person_key(base) == key and (strip_person(base)[0] != base.replace(" ", "")
                                                          or "人物" in n["folder"] or _ != ""):
                return n["path"]
            if kind == O and org_key(base) == key:
                return n["path"]
        return ""

    def _related(self, d: dict, paths: set[str]) -> list[dict]:
        """名前は出てこないが関係がありそうな文書（所属・一緒に出てくる人・案件の言葉で推定）。"""
        words = [d["name"]] + [x["name"] for x in d.get("affiliations", [])[:2]]
        words += [x["name"] for x in d["co_people"][:3]] + [x["name"] for x in d["co_orgs"][:2]]
        titles = " ".join(a["title"] for a in d["appearances"][:5])
        query = " ".join(words) + " " + re.sub(r"\.[a-z]+\b", "", titles)
        try:
            hits = self.app.ai.retrieve(query, k=16, exclude=paths)
        except LLMError:
            return []
        out, seen = [], set()
        for h in hits:
            if h["path"] in seen or h["path"] in paths:
                continue
            seen.add(h["path"])
            it = self.index.get(h["path"]) or {}
            out.append({"path": h["path"], "title": h["title"], "kind": h.get("kind", "note"),
                        "grp": it.get("grp", ""), "snippet": h["text"][:120].replace("\n", " ")})
            if len(out) >= 6:
                break
        return out

    # ------------------------------------------------------------ AI で人物像を推定
    def profile(self, kind: str, key: str) -> dict:
        d = self.detail(kind, key, related=True)
        cfg = self.app.config()
        sources, blocks = [], []
        for n, a in enumerate(d["appearances"][:12], 1):
            item = self.index.get(a["path"]) or {}
            lines = []
            for name in {d["name"]} | {x["key"] for x in d["aliases"]}:
                for ln in (item.get("text") or "").split("\n"):
                    if name and name.lower() in ln.lower() and ln.strip() not in lines:
                        lines.append(ln.strip()[:200])
                    if len(lines) >= 4:
                        break
            if a["context"] and a["context"] not in lines:
                lines.insert(0, a["context"])
            role = "・".join(a["roles"]) or "不明"
            sources.append({"n": n, "path": a["path"], "title": a["title"]})
            blocks.append(f"[{n}] {'ノート' if a['kind'] == 'note' else '資料'}「{a['title']}」 立場: {role}\n" + "\n".join(lines[:4]))
        base = len(sources)
        for j, r in enumerate(d["related"][:3], base + 1):
            sources.append({"n": j, "path": r["path"], "title": r["title"]})
            blocks.append(f"[{j}] 名前は出てこないが関係がありそうな文書「{r['title']}」\n{r['snippet']}")
        facts = [f"名前: {d['name']}（{d['kind_label']}）"]
        if d.get("affiliations"):
            facts.append("所属（文書に記載）: " + "、".join(f"{x['name']}（{x['count']}件）" for x in d["affiliations"]))
        if d.get("affiliations_inferred"):
            facts.append("所属の候補（一緒に出てくる組織）: " + "、".join(x["name"] for x in d["affiliations_inferred"]))
        if d["titles"]:
            facts.append("肩書: " + "、".join(x["name"] for x in d["titles"]))
        if d["roles"]:
            facts.append("文書での立場: " + "、".join(f"{x['name']}（{x['count']}件）" for x in d["roles"]))
        if d["co_people"]:
            facts.append("一緒に出てくる人: " + "、".join(f"{x['name']}（{x['count']}件）" for x in d["co_people"][:8]))
        if d.get("members"):
            facts.append("所属している人: " + "、".join(x["name"] for x in d["members"][:10]))
        if not chat_configured(cfg):
            return {"answer": "", "sources": sources, "facts": facts, "llm": False,
                    "message": "LLM が未設定のため、集計だけを表示しています。"}
        what = "この人物" if kind == P else "この組織"
        prompt = ("[TASK:profile]\n社内のノートと資料から集めた情報をもとに、" + what + "について推定してください。\n"
                  "次の見出しで、日本語で簡潔に書いてください: ## 所属と立場 / ## 関わっている案件・テーマ / "
                  "## 他の関係者との関係 / ## 次に会う（やり取りする）ときのヒント\n"
                  "根拠にした文書は [番号] で示してください。文書から言い切れないことは「推定」と明記し、"
                  "書かれていないことは作らないでください。\n\n# 集計\n" + "\n".join(facts) +
                  "\n\n# 文書からの抜粋\n" + ("\n\n".join(blocks) or "（なし）"))
        answer = LLMClient(cfg).chat(prompt, temperature=0.2)
        return {"answer": answer, "sources": sources, "facts": facts, "llm": True}

    # ------------------------------------------------------------ グラフ
    def graph_extra(self, center: str | None) -> dict:
        self.sync()
        m = self.store.model()
        nodes, edges = {}, []
        min_docs = 1 if center else 2
        for (t, k), e in m["ents"].items():
            if len(e["paths"]) < min_docs:
                continue
            nid = f"~{t[0]}:{k}"
            nodes[nid] = (e["display"], t, "")
            edges += [(p, nid) for p in e["paths"]]
        return {"nodes": nodes, "edges": edges}

    # ------------------------------------------------------------ 直す（まとめる・外す・足す）
    def merge(self, kind: str, key: str, into: str) -> dict:
        if key == into:
            raise VaultError("同じ名前です")
        self._ent(kind, into)
        self.store.set_alias(kind, key, into)
        return self.detail(kind, into, related=False)

    def unmerge(self, kind: str, key: str) -> None:
        self.store.clear_alias(kind, key)

    def ignore(self, kind: str, key: str) -> None:
        e = self._ent(kind, key)
        for k in e["keys"] | {key}:
            self.store.set_alias(kind, k, "")

    def ignored(self) -> list[dict]:
        return [{"type": r["type"], "key": r["key"]} for r in self.store.alias_list() if not r["target"]]

    def add(self, path: str, kind: str, name: str, role: str = "") -> dict:
        if kind not in (P, O):
            raise VaultError("種類が不正です")
        if self.index.get(path) is None:
            raise VaultError("読み込まれていないファイルです", 404)
        name = (name or "").strip()
        base, paren = split_paren(name)
        key = person_key(name) if kind == P else org_key(name)
        if not key:
            raise VaultError("名前を入力してください")
        canon = self.store.model()["canon"].get((kind, key), key) or key
        self.store.add_manual(path, {"type": kind, "key": canon, "name": base, "role": role.strip()[:20],
                                     "title": strip_person(base)[1] if kind == P else "",
                                     "org": paren.split()[0] if paren and kind == P else "",
                                     "context": "（手で追加）"})
        return {"entities": self.of(path)}
