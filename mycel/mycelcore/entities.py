"""人物・組織: ノートや資料に出てくる人と会社を見つけて、文書同士をつなぐ。

[[リンク]] を書かなくても、同じ人が出てくる文書は「その人」を介してつながる。
人物ごとに「どの文書に・どんな立場で出ているか」「所属（推定）」「一緒に出てくる人」を集計する。

抽出は 2 段構え
- ルール（自動・速い）: 「田中部長」「鈴木様」「株式会社○○」「A社」、メールの差出人・宛先、
  「出席者：」の行、[[田中部長（A社）]] のようなリンク。インデックスの「更新」の後や参照時に差分だけ行う
- AI（手動・ローカル LLM）: 文書ごとに人名・組織名・所属・その文書での立場を JSON で出させる。
  「AI で詳しく抽出」を押したときだけ、まだ AI で読んでいない（または変わった）文書を処理する

保存先は ``<vault>/.mycel/entities.sqlite``。AI の抽出結果と、利用者の「同一人物としてまとめる」
「人名ではない」の指定を持つので、インデックス（キャッシュ）とは別のファイルにする。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from . import links as L
from .index import Cancelled
from .scope import is_under

SCHEMA_VERSION = 1
P, O = "person", "org"

HONORIFICS = ["様", "さま", "さん", "氏", "殿", "くん", "君", "先生"]
TITLES = ["代表取締役社長", "代表取締役", "取締役", "副社長", "社長", "専務", "常務", "会長", "副本部長", "本部長",
          "事業部長", "副部長", "部長", "次長", "課長", "係長", "主任", "室長", "所長", "工場長", "支店長",
          "センター長", "グループ長", "リーダー", "マネージャー", "マネジャー", "主査", "技師長", "担当"]
_SUFFIX = sorted(HONORIFICS + TITLES, key=len, reverse=True)
# よくある姓。肩書（部長など）だけが付いた語は、姓らしいときだけ人名とみなす（「製造本部長」を人名にしない）
SURNAMES = set("""佐藤 鈴木 高橋 田中 伊藤 渡辺 渡部 山本 中村 小林 加藤 吉田 山田 佐々木 山口 松本 井上 木村 林 斎藤 斉藤
清水 山崎 森 池田 橋本 阿部 石川 山下 中島 石井 小川 前田 岡田 長谷川 藤田 後藤 近藤 村上 遠藤 青木 坂本 斉藤 福田
太田 西村 藤井 金子 岡本 藤原 中野 三浦 原田 中川 松田 竹内 小野 田村 中山 和田 石田 森田 上田 原 内田 柴田 酒井
宮崎 横山 高木 安藤 宮本 大野 小島 工藤 谷口 今井 高田 丸山 増田 杉山 村田 大塚 小山 平野 藤本 久保 松井 千葉 岩崎
桜井 木下 野口 松尾 菊地 菊池 野村 新井 渡部 佐野 杉本 古川 大西 市川 浅野 小松 西田 五十嵐 北村 安田 中田 川口
平田 川崎 飯田 吉川 本田 久保田 沢田 辻 関 吉村 渡邉 渡邊 岩田 中西 服部 樋口 福島 川上 永井 松岡 田口 山中 森本
土屋 矢野 広瀬 秋山 石原 松下 大橋 松浦 吉岡 小池 馬場 浅井 荒木 大久保 野田 小西 熊谷 川村 星野 大谷 黒田 堀
尾崎 望月 永田 内藤 松村 西川 大島 菅原 早川 平井 荒井 宮田 片山 竹田 長田 須藤 上野 高野 栗原 杉浦 篠原 吉野 武田
上原 小澤 小沢 水野 堀内 河野 江口 富田 大川 前川 西山 市村 宮下 新田 岡 東 南 北 西 白石 細川 宮内 野崎 植田 小田
島田 青山 古田 寺田 堤 坂口 本間 福井 徳田 奥田 岩本 大石 関口 戸田 冨田 矢島 長尾 小原 稲垣 杉田 片岡 今村 筒井""".split())
NOT_NAMES = {"皆", "各位", "客", "お客", "担当者", "先方", "当方", "弊社", "御社", "貴社", "当社", "自社", "社内", "社外",
             "前任", "後任", "新任", "同", "上記", "下記", "本件", "各", "全員", "関係者", "責任者", "決裁者", "窓口"}
ORG_STOP_END = ("他", "各", "同", "弊", "御", "貴", "当", "自", "本", "会", "商", "支", "親", "子", "出版", "新聞",
                "通信", "旅行", "広告", "神")
ORG_FORMS = re.compile(r"(株式会社|有限会社|合同会社|（株）|\(株\)|㈱|\binc\.?|co\.,?\s*ltd\.?|corporation|corp\.?)",
                       re.IGNORECASE)
PERSON_ROLES = [("決裁", "決裁者"), ("窓口", "窓口"), ("キーパーソン", "キーパーソン"), ("責任者", "責任者"),
                ("承認", "承認者"), ("作成", "作成者"), ("出席", "出席者"), ("参加", "出席者"), ("同席", "出席者"),
                ("担当", "担当")]
ORG_ROLES = [("顧客", "顧客"), ("お客", "顧客"), ("得意先", "顧客"), ("競合", "競合"), ("仕入", "仕入先"),
             ("協力会社", "パートナー"), ("パートナー", "パートナー"), ("代理店", "パートナー")]

_K = r"一-龥々〆ヵヶ"
_lock = threading.RLock()


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").strip()


def split_paren(name: str) -> tuple[str, str]:
    """「田中部長（A社 製造本部）」→ ("田中部長", "A社 製造本部")"""
    m = re.match(r"^(.*?)[（(]([^）)]*)[）)]\s*$", name.strip())
    return (m.group(1).strip(), m.group(2).strip()) if m else (name.strip(), "")


def strip_person(name: str) -> tuple[str, str]:
    """敬称・肩書を外す。戻り値は (名前, 肩書)。"""
    s = re.sub(r"[\s　]+", "", _nfkc(name))
    title = ""
    changed = True
    while changed and s:
        changed = False
        for suf in _SUFFIX:
            if s.endswith(suf) and len(s) > len(suf):
                if suf in TITLES and not title:
                    title = suf
                s = s[: -len(suf)]
                changed = True
                break
    return s, title


def person_display(name: str) -> str:
    """表示用: 敬称・肩書を外すが、姓と名の間の空白は残す。"""
    s = re.sub(r"[\s　]+", " ", _nfkc(split_paren(name)[0])).strip()
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIX:
            if s.endswith(suf) and len(s.replace(" ", "")) > len(suf):
                s = s[: -len(suf)].rstrip()
                changed = True
                break
    return s


def person_key(name: str) -> str:
    base, _ = strip_person(split_paren(name)[0])
    return base.lower()


def org_key(name: str) -> str:
    s = ORG_FORMS.sub("", _nfkc(name))
    s = re.sub(r"[\s　・.,]+", "", s)
    if len(s) > 1 and s.endswith("社"):
        s = s[:-1]
    return s.lower()


def org_display(name: str) -> str:
    return re.sub(r"\s+", " ", _nfkc(name)).strip()


def _line_role(line: str, table) -> str:
    for word, role in table:
        if word in line:
            return role
    return ""


def _clean_line(line: str) -> str:
    line = L.WIKILINK_RE.sub(lambda m: L.parse_wikilink(m.group(1))[2] or L.parse_wikilink(m.group(1))[0], line)
    return re.sub(r"^[\s>*#\-+]+", "", line).strip()[:140]


# ---------------------------------------------------------------- ルールによる抽出
_PERSON_RE = re.compile(
    rf"(?<![{_K}ァ-ヶーA-Za-z])([{_K}]{{1,4}}(?:[ 　][{_K}]{{1,3}})?)[ 　]?({'|'.join(_SUFFIX)})")
_KATA_RE = re.compile(r"(?<![ァ-ヶー])([ァ-ヶー]{2,10})[ 　]?(様|さん|氏)")
_ORG_RE = re.compile(
    r"(?:株式会社|有限会社|合同会社|（株）|\(株\)|㈱)[ 　]?([^\s、。,，（）()「」『』\[\]【】:：/|]{1,20})"
    r"|([^\s、。,，（）()「」『』\[\]【】:：/|]{1,20}?)[ 　]?(?:株式会社|（株）|\(株\)|㈱)"
    rf"|(?<![{_K}ァ-ヶーA-Za-z0-9Ａ-Ｚａ-ｚ])([A-Za-zＡ-Ｚａ-ｚ][A-Za-z0-9Ａ-Ｚａ-ｚ&]{{0,15}}|[{_K}ァ-ヶー]{{2,10}})社(?![員内外長会屋宅名])")
_HEADER_RE = re.compile(r"^[-*\s]*(差出人|宛先|CC|Cc|From|To|作成者|担当者?|出席者?|参加者|同席者?)\s*[:：]\s*(.+)$")


def extract_rule(text: str) -> list[dict]:
    """文章から人物・組織を拾う（LLM なし）。同じ名前は文書内で 1 件にまとめる。"""
    found: dict[tuple[str, str], dict] = {}

    def add(kind: str, name: str, line: str, role: str = "", title: str = "", org: str = ""):
        name = name.strip()
        if kind == P:
            key = person_key(name)
            if not key or key in NOT_NAMES or len(key) > 12:
                return
        else:
            key = org_key(name)
            if not key or len(key) > 30:
                return
        k = (kind, key)
        cur = found.get(k)
        if cur is None:
            found[k] = {"type": kind, "key": key, "name": name, "title": title, "org": org,
                        "role": role, "context": _clean_line(line)}
        else:
            for f, v in (("title", title), ("org", org), ("role", role)):
                if v and not cur[f]:
                    cur[f] = v

    fm, body, _ = L.split_frontmatter(text)
    for k, v in (fm or {}).items():
        if isinstance(v, str) and re.search(r"顧客|取引先|得意先|会社|企業", str(k)):
            for part in re.split(r"[、,]", v):
                if part.strip():
                    add(O, re.sub(r"\[\[|\]\]", "", part), f"{k}: {v}", "顧客")
    in_fence = False
    for raw in body.split("\n"):
        if L.FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence or not raw.strip():
            continue
        line = _nfkc(raw)
        prole = _line_role(line, PERSON_ROLES)
        orole = _line_role(line, ORG_ROLES)
        # [[田中部長（A社）]] のようなリンク
        for m in L.WIKILINK_RE.finditer(line):
            target = L.parse_wikilink(m.group(1))[0].split("/")[-1]
            base, paren = split_paren(target)
            stripped, title = strip_person(base)
            if stripped != re.sub(r"[\s　]+", "", base) or (paren and len(stripped) <= 4):
                org = paren.split()[0] if paren else ""
                add(P, base, raw, prole, title, org)
                if org:
                    add(O, org, raw, orole)
        # メールの差出人・宛先、出席者の行
        h = _HEADER_RE.match(line)
        if h:
            label, rest = h.group(1), h.group(2)
            role = {"差出人": "差出人", "From": "差出人", "宛先": "宛先", "To": "宛先", "CC": "CC", "Cc": "CC",
                    "作成者": "作成者"}.get(label, "担当" if label.startswith("担当") else "出席者")
            for part in re.split(r"[、,，/／]", L.WIKILINK_RE.sub(lambda m: m.group(1).split("|")[0], rest)):
                part = re.sub(r"<[^>]*>|\"|'|\S+@\S+", "", part).strip()
                if not part or len(part) > 30:
                    continue
                base, paren = split_paren(part)
                org = paren.split()[0] if paren else ""
                toks = base.split()
                if len(toks) >= 2 and _ORG_RE.fullmatch(toks[0]):        # 「A社 田中部長」
                    org, base = toks[0], " ".join(toks[1:])
                if re.fullmatch(rf"[{_K}ァ-ヶーA-Za-z][{_K}ァ-ヶーA-Za-z\s　.]{{0,20}}", base) and not _ORG_RE.fullmatch(base):
                    add(P, base, raw, role, strip_person(base)[1], org)
                if org:
                    add(O, org, raw, orole)
        # 本文中の「田中部長」「鈴木様」
        for m in _PERSON_RE.finditer(line):
            name, suf = m.group(1), m.group(2)
            nm = re.sub(r"[\s　]+", "", name)
            surname_ok = any(nm.startswith(s) for s in SURNAMES)
            if suf in HONORIFICS or surname_ok:
                add(P, name + suf, raw, prole, suf if suf in TITLES else "")
        for m in _KATA_RE.finditer(line):
            add(P, m.group(1) + m.group(2), raw, prole)
        for m in _ORG_RE.finditer(line):
            name = next(g for g in m.groups() if g)
            whole = m.group(0)
            if m.group(3) and (name.endswith(ORG_STOP_END) or name in ORG_STOP_END):
                continue
            add(O, whole if not m.group(3) else name + "社", raw, orole)
    return list(found.values())


# ---------------------------------------------------------------- 保存と集計
class EntityStore:
    def __init__(self, internal_dir: Path):
        self.path = internal_dir / "entities.sqlite"
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        c = self.conn
        if c.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            for t in ("mentions", "extracted"):
                c.execute(f"DROP TABLE IF EXISTS {t}")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS mentions(path TEXT, type TEXT, key TEXT, name TEXT, title TEXT,
                org TEXT, role TEXT, context TEXT, method TEXT);
            CREATE INDEX IF NOT EXISTS mentions_path ON mentions(path);
            CREATE INDEX IF NOT EXISTS mentions_key ON mentions(type, key);
            CREATE TABLE IF NOT EXISTS extracted(path TEXT PRIMARY KEY, fp TEXT, method TEXT, at REAL);
            CREATE TABLE IF NOT EXISTS aliases(type TEXT, key TEXT, target TEXT, PRIMARY KEY(type, key));
            CREATE TABLE IF NOT EXISTS hidden(path TEXT, type TEXT, key TEXT, PRIMARY KEY(path, type, key));
            CREATE TABLE IF NOT EXISTS manual(path TEXT, type TEXT, key TEXT, name TEXT, title TEXT, org TEXT,
                role TEXT, context TEXT, PRIMARY KEY(path, type, key));
        """)
        c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        c.commit()
        self._cache = None
        self.rev = 0

    def close(self) -> None:
        with _lock:
            self.conn.close()

    # ---- 書き込み
    def put(self, path: str, items: list[dict], fp: str, method: str) -> None:
        with _lock:
            c = self.conn
            c.execute("DELETE FROM mentions WHERE path=?", (path,))
            c.executemany("INSERT INTO mentions VALUES(?,?,?,?,?,?,?,?,?)",
                          [(path, it["type"], it["key"], it.get("name", ""), it.get("title", ""), it.get("org", ""),
                            it.get("role", ""), it.get("context", ""), method) for it in items if it.get("key")])
            c.execute("INSERT OR REPLACE INTO extracted VALUES(?,?,?,?)", (path, fp, method, time.time()))
            c.commit()
            self._changed()

    def forget(self, path: str) -> None:
        with _lock:
            self.conn.execute("DELETE FROM mentions WHERE path=?", (path,))
            self.conn.execute("DELETE FROM extracted WHERE path=?", (path,))
            self.conn.commit()
            self._changed()

    def rename(self, old: str, new: str) -> None:
        """ファイル・フォルダの移動。AI で抽出した結果を捨てずに付け替える。"""
        with _lock:
            c = self.conn
            for tbl in ("mentions", "extracted", "hidden", "manual"):
                rows = c.execute(f"SELECT DISTINCT path FROM {tbl}").fetchall()
                for r in rows:
                    p = r["path"]
                    if p == old or is_under(p, old):
                        np = new + p[len(old):]
                        if tbl in ("extracted", "hidden", "manual"):
                            c.execute(f"DELETE FROM {tbl} WHERE path=?", (np,))
                        c.execute(f"UPDATE {tbl} SET path=? WHERE path=?", (np, p))
            c.commit()
            self._changed()

    def set_alias(self, kind: str, key: str, target: str) -> None:
        """key を target にまとめる。target が空なら「人名（組織名）ではない」として隠す。"""
        with _lock:
            c = self.conn
            c.execute("INSERT OR REPLACE INTO aliases VALUES(?,?,?)", (kind, key, target))
            c.execute("UPDATE aliases SET target=? WHERE type=? AND target=?", (target, kind, key))
            c.commit()
            self._changed()

    def hide(self, path: str, kind: str, key: str) -> None:
        """この文書からこの人（組織）を外す。抽出し直しても外したまま。手で足したものは消す。"""
        with _lock:
            self.conn.execute("INSERT OR REPLACE INTO hidden VALUES(?,?,?)", (path, kind, key))
            self.conn.execute("DELETE FROM manual WHERE path=? AND type=? AND key=?", (path, kind, key))
            self.conn.commit()
            self._changed()

    def unhide(self, path: str, kind: str, key: str) -> None:
        with _lock:
            self.conn.execute("DELETE FROM hidden WHERE path=? AND type=? AND key=?", (path, kind, key))
            self.conn.commit()
            self._changed()

    def add_manual(self, path: str, item: dict) -> None:
        """抽出されなかった人（組織）を、この文書に手で足す。"""
        with _lock:
            self.conn.execute("DELETE FROM hidden WHERE path=? AND type=? AND key=?", (path, item["type"], item["key"]))
            self.conn.execute("INSERT OR REPLACE INTO manual VALUES(?,?,?,?,?,?,?,?)",
                              (path, item["type"], item["key"], item.get("name", ""), item.get("title", ""),
                               item.get("org", ""), item.get("role", ""), item.get("context", "")))
            self.conn.commit()
            self._changed()

    def hidden_list(self) -> list[dict]:
        with _lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM hidden ORDER BY path")]

    def alias_list(self) -> list[dict]:
        with _lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM aliases ORDER BY type, key")]

    def clear_alias(self, kind: str, key: str) -> None:
        with _lock:
            self.conn.execute("DELETE FROM aliases WHERE type=? AND key=?", (kind, key))
            self.conn.commit()
            self._changed()

    def _changed(self) -> None:
        self._cache = None
        self.rev += 1

    # ---- 状態
    def extracted(self) -> dict[str, tuple[str, str]]:
        with _lock:
            return {r["path"]: (r["fp"], r["method"]) for r in self.conn.execute("SELECT * FROM extracted")}

    def mentions_of(self, path: str) -> list[dict]:
        with _lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM mentions WHERE path=?", (path,))]

    # ---- 集計（名寄せ）
    def model(self) -> dict:
        """名寄せ済みの全体像。変更があるまでキャッシュする。"""
        with _lock:
            if self._cache is not None:
                return self._cache
            rows = [dict(r) for r in self.conn.execute("SELECT * FROM mentions")]
            rows += [{**dict(r), "method": "user"} for r in self.conn.execute("SELECT * FROM manual")]
            aliases = {(r["type"], r["key"]): r["target"] for r in self.conn.execute("SELECT * FROM aliases")}
            hidden = {(r["path"], r["type"], r["key"]) for r in self.conn.execute("SELECT * FROM hidden")}
        canon = self._canon(rows, aliases)
        ents: dict[tuple[str, str], dict] = {}
        path_ents: dict[str, set] = defaultdict(set)
        for r in rows:
            ck = canon.get((r["type"], r["key"]))
            if not ck or (r["path"], r["type"], ck) in hidden or (r["path"], r["type"], r["key"]) in hidden:
                continue
            path_ents[r["path"]].add((r["type"], ck))
            e = ents.setdefault((r["type"], ck), {"type": r["type"], "key": ck, "names": Counter(), "titles": Counter(),
                                                  "orgs": Counter(), "roles": Counter(), "paths": defaultdict(list),
                                                  "keys": set()})
            e["keys"].add(r["key"])
            e["names"][r["name"]] += 1
            if r["title"]:
                e["titles"][r["title"]] += 1
            if r["org"] and r["type"] == P:
                ok = canon.get((O, org_key(r["org"]))) or org_key(r["org"])
                if ok:
                    e["orgs"][ok] += 1
            if r["role"]:
                e["roles"][r["role"]] += 1
            e["paths"][r["path"]].append(r)
        for e in ents.values():
            e["display"] = self._display(e)
        self._cache = {"ents": ents, "canon": canon, "path_ents": path_ents, "aliases": aliases}
        return self._cache

    @staticmethod
    def _canon(rows: list[dict], aliases: dict) -> dict[tuple[str, str], str]:
        """名前の揺れをまとめる。利用者の指定 → 姓だけの呼び方（田中部長）を同じ組織のフルネーム（田中太郎）へ。"""
        keys = {(r["type"], r["key"]) for r in rows}
        orgs_of: dict[str, Counter] = defaultdict(Counter)
        for r in rows:
            if r["type"] == P and r["org"]:
                orgs_of[r["key"]][org_key(r["org"])] += 1
        canon: dict[tuple[str, str], str] = {}
        persons = [k for t, k in keys if t == P]
        for t, k in keys:
            canon[(t, k)] = k
        for k in persons:
            if len(k) > 3:
                continue
            cands = [f for f in persons if f != k and f.startswith(k) and len(f) - len(k) <= 3 and len(f) <= 7]
            if len(cands) > 1 and orgs_of[k]:
                cands = [f for f in cands if set(orgs_of[f]) & set(orgs_of[k])]
            if len(cands) == 1:
                canon[(P, k)] = cands[0]
        for (t, k), target in aliases.items():
            canon[(t, k)] = target
        # まとめ先がさらにまとめられている場合をたどる
        for tk in list(canon):
            seen, cur = set(), canon[tk]
            while cur and (tk[0], cur) in canon and canon[(tk[0], cur)] != cur and cur not in seen:
                seen.add(cur)
                cur = canon[(tk[0], cur)]
            canon[tk] = cur
        return canon

    @staticmethod
    def _display(e: dict) -> str:
        best = ""
        for name, _ in e["names"].most_common():
            base = split_paren(name)[0]
            cand = person_display(base) if e["type"] == P else org_display(base)
            if e["type"] == P and re.sub(r"[\s　]+", "", cand).lower() != e["key"]:
                continue
            if e["type"] == O and org_key(cand) != e["key"]:
                continue
            if len(cand) > len(best):
                best = cand
        if not best:
            best = e["names"].most_common(1)[0][0] if e["names"] else e["key"]
            best = person_display(best) if e["type"] == P else org_display(best)
        if e["type"] == P and " " not in best and re.fullmatch(r"[A-Za-z]+", best):
            best = best.capitalize()
        return best


def llm_items(raw_list, kinds=(P, O)) -> list[dict]:
    """LLM の JSON（[{"name","type","org","role","evidence"}]）を mentions の形にする。"""
    out: dict[tuple[str, str], dict] = {}
    for it in raw_list if isinstance(raw_list, list) else []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        kind = {"person": P, "人物": P, "org": O, "organization": O, "組織": O, "company": O, "会社": O}.get(
            str(it.get("type") or "").strip().lower(), "")
        if not name or kind not in kinds:
            continue
        key = person_key(name) if kind == P else org_key(name)
        if not key or (kind == P and key in NOT_NAMES):
            continue
        base, paren = split_paren(name)
        title = strip_person(base)[1] if kind == P else ""
        title = str(it.get("title") or title or "").strip()[:20]
        org = str(it.get("org") or (paren.split()[0] if paren else "")).strip()[:40] if kind == P else ""
        cur = out.get((kind, key))
        row = {"type": kind, "key": key, "name": base if kind == P else name, "title": title, "org": org,
               "role": str(it.get("role") or "").strip()[:20], "context": str(it.get("evidence") or "").strip()[:140]}
        if cur is None:
            out[(kind, key)] = row
        else:
            for f in ("title", "org", "role", "context"):
                if row[f] and not cur[f]:
                    cur[f] = row[f]
    return list(out.values())


def merge_items(primary: list[dict], extra: list[dict]) -> list[dict]:
    """AI の結果（primary）に、ルールで拾ったものを足す（メールの差出人など）。"""
    seen = {(i["type"], i["key"]): i for i in primary}
    out = list(primary)
    for e in extra:
        cur = seen.get((e["type"], e["key"]))
        if cur is None:
            out.append(e)
        else:
            for f in ("title", "org", "role", "context"):
                if e.get(f) and not cur.get(f):
                    cur[f] = e[f]
    return out


def parse_llm_json(raw: str):
    raw = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if m:
        raw = m.group(1)
    s, e = raw.find("["), raw.rfind("]")
    if s == -1 or e <= s:
        o1, o2 = raw.find("{"), raw.rfind("}")
        if o1 != -1 and o2 > o1:
            try:
                d = json.loads(raw[o1:o2 + 1])
                for k in ("entities", "items", "names"):
                    if isinstance(d.get(k), list):
                        return d[k]
            except ValueError:
                return None
        return None
    try:
        return json.loads(raw[s:e + 1])
    except ValueError:
        return None


__all__ = ["EntityStore", "extract_rule", "llm_items", "merge_items", "parse_llm_json", "person_key", "org_key",
           "strip_person", "split_paren", "P", "O", "Cancelled"]
