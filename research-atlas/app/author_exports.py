"""Export author identities, observed coauthorship and display clusters."""
from .field_exports import _csv, _json


def network_csv(network: dict, kind: str) -> str:
    context = [network["id"], network["result_id"], network["group_by"]]
    headers = ["Network ID", "Analysis ID", "Grouping"]
    exported = network.get("export_data", {})
    clusters = {row["id"]: row for row in network.get("clusters", [])}
    metadata = _json({key: network.get(key) for key in ("scope", "stats", "warnings", "methodology")})
    if kind == "authors":
        headers += ["Author ID", "Name", "Original IDs (JSON)", "Names (JSON)", "Identity basis",
                    "Affiliations (JSON)", "Original affiliations (JSON)", "Paper count", "Cumulative citations",
                    "Papers with known citation count", "Years (JSON)", "Paper IDs (JSON)", "Cluster ID",
                    "Cluster", "X", "Y", "Author metadata (JSON)", "Scope and methodology (JSON)"]
        rows = []
        for node in exported.get("authors", network.get("nodes", [])):
            rows.append(context + [node["id"], node["label"], _json(node.get("original_ids", [])),
                        _json(node.get("names", [])), node.get("identity_basis"), _json(node.get("affiliations", [])),
                        _json(node.get("original_affiliations", [])), node["count"],
                        node.get("citations") if node.get("citation_known_papers", 0) else None,
                        node.get("citation_known_papers", 0), _json(node.get("years", [])),
                        _json(node.get("paper_ids", [])), node.get("cluster_id"),
                        clusters.get(node.get("cluster_id"), {}).get("label") or node.get("cluster_label", ""), node.get("x"), node.get("y"),
                        _json(node), metadata])
    elif kind == "edges":
        headers += ["Source author ID", "Target author ID", "Coauthored papers", "Paper IDs (JSON)",
                    "Edge metadata (JSON)", "Scope and methodology (JSON)"]
        rows = [context + [row["source"], row["target"], row["weight"], _json(row.get("paper_ids", [])),
                           _json(row), metadata] for row in exported.get("edges", network.get("edges", []))]
    elif kind == "clusters":
        headers += ["Cluster ID", "Cluster", "Authors in display population", "Papers in display population",
                    "Author IDs (JSON)", "Paper IDs (JSON)", "Cluster metadata (JSON)", "Scope and methodology (JSON)"]
        rows = [context + [row["id"], row["label"], row["author_count"], row["paper_count"],
                           _json(row.get("node_ids", [])), _json(row.get("paper_ids", [])), _json(row), metadata]
                for row in exported.get("clusters", network.get("clusters", []))]
    else:
        raise ValueError("著者ネットワークCSVの種類が不正です。")
    return _csv(headers, rows)
