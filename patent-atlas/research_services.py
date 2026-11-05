"""Shared research and invention workspace services; no route imports."""

import copy
import re
import time
import uuid
from dataclasses import dataclass
from fastapi import HTTPException
import classification_explorer as explorer
from catalog import keywords
from common import fingerprint
from query_formats import Node, build_tree, describe_tree, group, terms_of, tree_data, tree_from_data, walk_tree
from workspace import Workspace
from services import Services
from workspace_defaults import new_workbench as new_workbench

MAX_SAVED_TARGETS = 5000


def merge_target_references(existing, selected):
    """Retain deselected seeds separately from the current result CSV/selection."""
    references = {row["id"]: copy.deepcopy(row) for row in existing}
    for row in selected:
        references[row["id"]] = copy.deepcopy(row)
    if len(references) > MAX_SAVED_TARGETS:
        raise ValueError("保存するターゲット参照は5,000件までです。既存参照は削除せず、今回の変更を中止しました。")
    return list(references.values())


def checked_brief(body, existing, patents):
    if not isinstance(body, dict):
        raise ValueError("調査条件を確認してください。")
    brief = copy.deepcopy(existing)
    for key, limit in (("purpose", 300), ("goal", 5000), ("keywords", 2000)):
        if key in body:
            value = body[key]
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f"{key} は{limit}文字以内の文字列にしてください。")
            brief[key] = value.strip()
    mode = body.get("entry_mode", brief.get("entry_mode", "target"))
    if mode not in ("target", "discover", "examples", "tools"):
        raise ValueError("調査の入口を選んでください。")
    brief["entry_mode"] = mode
    for key, limit, length in (("user_aspects", 8, 160), ("target_ids", 20, 200)):
        value = body.get(key, brief.get(key, []))
        if (
            not isinstance(value, list)
            or len(value) > limit
            or any(not isinstance(v, str) or not v.strip() or len(v) > length for v in value)
        ):
            raise ValueError(f"{key} は{limit}件以内で指定してください。")
        brief[key] = list(dict.fromkeys(v.strip() for v in value))
    known = {p["id"] for p in patents}
    if any(identifier not in known for identifier in brief["target_ids"]):
        raise ValueError("ターゲット特許が現在の特許群にありません。選び直してください。")
    if brief["keywords"]:
        keywords(brief["keywords"])
    return brief


def concept_conditions(plan, body):
    values = body.get("concepts")
    if not isinstance(values, list) or not values or len(values) > 8:
        raise ValueError("採用する観点を1〜8件選んでください。")
    available = {c["id"]: c for c in plan["concepts"]}
    positive, negative, facets, seen = {}, [], [], set()
    for item in values:
        if not isinstance(item, dict) or item.get("id") not in available or item["id"] in seen:
            raise ValueError("観点が重複しているか、計画に存在しません。")
        seen.add(item["id"])
        source = available[item["id"]]
        terms = item.get("terms")
        if (
            not isinstance(terms, list)
            or not 1 <= len(terms) <= 24
            or any(not isinstance(t, str) or not t.strip() or len(t) > 160 for t in terms)
        ):
            raise ValueError("各観点の語句は1〜24個、各160文字以内で指定してください。")
        terms = list(dict.fromkeys(t.strip() for t in terms))
        role = item.get("role", source["role"])
        if role not in ("required", "exclude"):
            raise ValueError("採用する観点は必須条件か除外条件を選んでください。")
        or_group = item.get("or_group", source.get("or_group", "") if role != "exclude" else "")
        if not isinstance(or_group, str) or (or_group and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", or_group)):
            raise ValueError("和集合グループを選び直してください。")
        locked = source.get("locked_and", False) or (
            source["id"].startswith("aspect") and re.match(r"^(必須|required)\s*[:：]", source["name"], re.I)
        )
        if or_group and (role == "exclude" or locked):
            raise ValueError("明示した必須観点・除外観点は、他の観点との和集合にできません。")
        node = group("or", [Node("text", t) for t in terms])
        if role == "exclude":
            negative.append(node)
        else:
            # Namespaced keys keep an independent condition distinct even when
            # an untrusted group ID happens to equal another concept ID.
            key = ("union", or_group) if or_group else ("single", source["id"])
            positive.setdefault(key, []).append(node)
        facets.append(
            {
                **copy.deepcopy(source),
                "terms": terms,
                "role": role,
                "or_group": or_group,
                "edited": terms != source["terms"] or role != source["role"] or or_group != source.get("or_group", ""),
            }
        )
    if not positive:
        raise ValueError("少なくとも1つは検索に含める観点が必要です。")
    node = group("and", [group("or", alternatives) for alternatives in positive.values()])
    if negative:
        node = Node("not", children=(node, group("or", negative)))
    tree_from_data(tree_data(node))
    return node, facets


def refined_tree(previous, classifications, include, exclude):
    if previous.get("concept_tree"):
        base = tree_from_data(previous["concept_tree"])
        if classifications:
            base = group("and", [base, group("or", [Node("class", c["code"], c["kind"]) for c in classifications])])
    else:
        before = {(c["kind"], c["code"]) for c in previous.get("classifications", [])}
        after = {(c["kind"], c["code"]) for c in classifications}
        if before != after:
            raise ValueError(
                "取り込み式の分類はOR・NOTの位置を保持するため、「過去の式から作る」で個別に置換してください。特徴語の追加・除外はこの画面で更新できます。"
            )
        base = tree_from_data(previous.get("base_boolean_tree", previous["boolean_tree"]))
    result = base
    if include:
        result = group("and", [result, group("or", [Node("text", t) for t in include])])
    if exclude:
        result = Node("not", children=(result, group("or", [Node("text", t) for t in exclude])))
    return tree_data(base), tree_data(result)


@dataclass
class ResearchServices:
    ws: Workspace
    app: Services

    fingerprint = staticmethod(fingerprint)
    checked_brief = staticmethod(checked_brief)
    merge_target_references = staticmethod(merge_target_references)

    def workbench(self):
        if not isinstance(self.ws.state.get("research_workbench"), dict):
            self.ws.state["research_workbench"] = new_workbench()
        value = self.ws.state["research_workbench"]
        for key, default in new_workbench().items():
            value.setdefault(key, default)
        return value

    def check_revision(self, body):
        if body.get("revision") is not None and body["revision"] != self.workbench()["revision"]:
            raise HTTPException(409, "調査条件が別の操作で更新されました。再読み込みして確認してください。")

    def source_patents(self):
        # Seed references survive replacement of the search-result CSV.
        by_id = {p["id"]: copy.deepcopy(p) for p in self.workbench()["target_patents"]}
        for row in self.ws.state["patents"]:
            previous = by_id.get(row["id"], {})
            combined = copy.deepcopy(row)
            filled = []
            for field in ("title", "abstract", "ipc", "fi", "fterm", "cpc"):
                if not combined.get(field) and previous.get(field):
                    combined[field] = copy.deepcopy(previous[field])
                    filled.append(field)
            if filled:
                combined["seed_reference_fields"] = filled
            by_id[row["id"]] = combined
        return list(by_id.values())

    def commit(self, action):
        # Call under the same workspace lock; a failed save cannot partially apply.
        with self.ws.transaction(save=self.app.save, restore_pending=True):
            action()
            self.workbench()["revision"] += 1
        return self.app.public_state()

    def enrich_query(self, query):
        query = copy.deepcopy(query)
        node = build_tree(query)
        unique = {(n.system, n.value): n for n in walk_tree(node) if n.op == "class"}
        metadata = {(c["kind"], c["code"]): c for c in query.get("classifications", [])}
        query["classifications"] = [
            explorer.enrich(
                {
                    **copy.deepcopy(metadata.get((kind, code), {})),
                    "kind": kind,
                    "code": code,
                    "origins": copy.deepcopy(metadata.get((kind, code), {}).get("origins", []))
                    + [{"type": "research_query"}],
                }
            )
            for kind, code in unique
        ]
        query["boolean_tree"] = tree_data(node)
        query["term_catalog"] = terms_of(query)
        query["expression"] = describe_tree(node)
        return query

    def save_query(self, query, kind):
        query = self.enrich_query(query)
        if any(not c.get("selectable") for c in query["classifications"]):
            raise ValueError("選択できない分類が含まれます。上位IPCは辞書で存在を確認できるコードへ編集してください。")
        self.ws.state["candidates"] = explorer.merge_candidates(self.ws.state["candidates"], query["classifications"])
        self.ws.state["selected"] = [c["key"] for c in query["classifications"]]
        query.update(
            id=uuid.uuid4().hex,
            version=len(self.ws.state["queries"]) + 1,
            created_at=time.strftime("%Y-%m-%d %H:%M"),
            type=kind,
            keyword_context=self.ws.state["keywords"],
            note="同じ和集合グループはOR、独立した条件はAND。取り込み式は元の括弧とNOTの範囲を保持します。",
        )
        query.setdefault("keywords", keywords(self.ws.state["keywords"]))
        query.setdefault("include_terms", [])
        query.setdefault("exclude_terms", [])
        query.setdefault("base_boolean_tree", copy.deepcopy(query["boolean_tree"]))
        self.ws.state["queries"].append(query)
        self.workbench()["active_query_id"] = query["id"]
        self.app.classification_overview()
        return query
