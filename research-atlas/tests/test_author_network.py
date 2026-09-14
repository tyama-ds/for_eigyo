import copy
import json

import pytest

from app.analytics import _build_network
from app.author_network import build_author_network


def author(identifier=None, name="Alice Smith", **extra):
    return {"name": name, **({"id": identifier} if identifier is not None else {}), **extra}


def paper(identifier, authors, year=2025, citations=2, topic_id="topic-1", **extra):
    return {"id": identifier, "title": identifier, "authors": authors, "year": year,
            "citations": citations, "topic_id": topic_id, **extra}


def node_map(network):
    return {node["id"]: node for node in network["nodes"]}


def assert_valid(network):
    identifiers = {node["id"] for node in network["nodes"]}
    assert len(network["nodes"]) <= 120
    assert len(network["edges"]) <= 240
    assert all(edge["source"] != edge["target"] and {edge["source"], edge["target"]} <= identifiers
               for edge in network["edges"])
    assert all(0 <= node["x"] <= 1 and 0 <= node["y"] <= 1 for node in network["nodes"])
    assert sum(cluster["author_count"] for cluster in network["clusters"]) == len(network["nodes"])
    json.dumps(network, allow_nan=False)


@pytest.mark.parametrize("mode", ["id", "name", "institution", "community", "topic"])
def test_every_grouping_preserves_identity_paper_counts_and_citation_missingness(mode):
    a = author("scopus:42", aliases=["orcid:0000-0001-2345-6789"], affiliations=["University A"])
    same_a = author("orcid:0000-0001-2345-6789", "Smith, Alice", affiliations=["University A"])
    b = author("scopus:43", "Bob Brown")
    records = [paper("p1", [a, a, same_a, b], citations=4),
               paper("p2", [same_a, b], citations=None, topic_id="topic-2")]
    before = copy.deepcopy(records)
    network = build_author_network(records, mode)
    assert records == before
    assert_valid(network)
    assert network["stats"]["authors_total"] == 2
    assert network["edges"] == [{"source": "orcid:0000-0001-2345-6789", "target": "scopus:43", "weight": 2, "paper_ids": ["p1", "p2"]}]
    node = node_map(network)["orcid:0000-0001-2345-6789"]
    assert node["count"] == 2 and node["citations"] == 4 and node["citation_known_papers"] == 1
    assert node["paper_ids"] == ["p1", "p2"]
    assert set(node["original_ids"]) == {"scopus:42", "orcid:0000-0001-2345-6789"}
    assert node["identity_basis"] == "explicit_alias"
    assert set(node["names"]) == {"Alice Smith", "Smith, Alice"}


def test_same_name_different_explicit_ids_are_candidate_group_not_one_person():
    records = [paper("p1", [author("scopus:one"), author("scopus:two")]),
               paper("p2", [author(None, "ALICE SMITH")])]
    identities = None
    for mode in ("id", "name", "institution", "community", "topic"):
        network = build_author_network(records, mode)
        assert network["stats"]["authors_total"] == 3
        current = {node["id"] for node in network["nodes"]}
        if identities is not None:
            assert current == identities
        identities = current
    by_name = build_author_network(records, "name")
    assert len(by_name["clusters"]) == 1
    assert by_name["clusters"][0]["author_count"] == 3
    assert by_name["stats"]["ambiguous_name_groups"] == 1
    assert {edge["source"] for edge in by_name["edges"]} == {"scopus:one"}


def test_missing_ids_match_conservative_normalized_full_names_only():
    records = [paper("p1", [author(None, " Smith, Alice ")]),
               paper("p2", [author("name:older-hash", "Alice Smith")]),
               paper("p3", [author(None, "A. Smith")])]
    network = build_author_network(records)
    assert network["stats"]["authors_total"] == 2
    full_name = next(node for node in network["nodes"] if node["count"] == 2)
    assert full_name["identity_basis"] == "name_estimate"
    assert full_name["paper_ids"] == ["p1", "p2"]


def test_ambiguous_name_alias_cannot_bridge_two_explicit_ids():
    records = [paper("p1", [author("orcid:a", aliases=["name:shared"])]),
               paper("p2", [author("orcid:b", aliases=["name:shared"])]),
               paper("p3", [author("name:shared")])]
    network = build_author_network(records)
    assert network["stats"]["authors_total"] == 3
    assert network["stats"]["ambiguous_aliases"] >= 1
    assert network["edges"] == []


def test_transitive_name_aliases_do_not_absorb_different_explicit_identities():
    records = [paper("a", [author("orcid:a", aliases=["name:a"])]),
               paper("b", [author("orcid:b", aliases=["name:b"])]),
               paper("weak", [author("name:a", aliases=["name:b"])])]
    network = build_author_network(records)
    nodes = node_map(network)
    assert "orcid:a" in nodes and "orcid:b" in nodes
    assert network["stats"]["authors_total"] == 2
    assert network["stats"]["ambiguous_aliases"] >= 1
    assert max(nodes["orcid:a"]["count"], nodes["orcid:b"]["count"]) == 2


def test_paper_affiliations_are_not_distributed_to_every_author():
    records = [paper("a", [author("a", affiliations=["University A"]), author("b", "Bob")],
                     affiliations=["University A", "University B"])]
    network = build_author_network(records, "institution")
    nodes = node_map(network)
    assert nodes["a"]["affiliations"][0]["name"] == "University A"
    assert nodes["b"]["affiliations"] == []
    assert nodes["b"]["cluster_label"] == "所属未取得"
    assert network["stats"]["affiliation_coverage_pct"] == 50
    assert network["stats"]["author_paper_affiliation_coverage_pct"] == 50


def test_multiple_affiliations_retain_all_evidence_and_select_most_frequent():
    records = [paper("p1", [author("a", affiliations=["University B", "University A"])]),
               paper("p2", [author("a", affiliations=["University B", "University B"])]),
               paper("p3", [author("a")])]
    node = build_author_network(records, "institution")["nodes"][0]
    assert node["cluster_label"] == "University B"
    assert [(aff["name"], aff["paper_count"]) for aff in node["affiliations"]] == [("University B", 2), ("University A", 1)]
    assert node["affiliation_known_papers"] == 2
    assert node["original_affiliations"] == ["University A", "University B"]
    tie = build_author_network(records[:1], "institution")["nodes"][0]
    assert tie["cluster_label"] == "University A"


def test_department_prefix_and_suffix_are_grouped_but_location_is_preserved():
    affiliations = ["Department of Physics, Aster University, Tokyo, Japan",
                    "Aster University, School of Engineering, Tokyo, Japan",
                    "Aster University, Department of Materials Science, Tokyo, Japan",
                    "Aster University, School of Engineering, Osaka, Japan"]
    records = [paper(str(i), [author(str(i), f"Person {i}", affiliations=[aff])]) for i, aff in enumerate(affiliations)]
    network = build_author_network(records, "institution")
    assert len(network["clusters"]) == 2
    tokyo = next(cluster for cluster in network["clusters"] if "Tokyo" in cluster["label"])
    assert tokyo["author_count"] == 3
    assert all(node["original_affiliations"] == [affiliations[int(node["id"])]] for node in network["nodes"])


def test_three_universities_plus_unknown_have_distinct_visible_colors():
    records = []
    for index, university in enumerate(["Aster", "Birch", "Cedar"]):
        records.append(paper(f"{index}-a", [author(f"{index}-a", affiliations=[f"{university} University, Department of Science, Tokyo, Japan"])]))
        records.append(paper(f"{index}-b", [author(f"{index}-b", affiliations=[f"{university} University, School of Engineering, Tokyo, Japan"])]))
        records.append(paper(f"{index}-c", [author(f"{index}-c", affiliations=[f"Materials Laboratory, {university} University, Tokyo, Japan"])]))
    records.append(paper("unknown", [author("unknown")]))
    network = build_author_network(records, "institution")
    assert len(network["clusters"]) == 4
    assert len({cluster["color"] for cluster in network["clusters"]}) == 4
    assert next(cluster["color"] for cluster in network["clusters"] if cluster["label"] == "所属未取得") == "#8b96aa"


def test_real_weighted_communities_separate_two_cliques_and_leave_isolate_singleton():
    left = [author(identifier, identifier) for identifier in "abc"]
    right = [author(identifier, identifier) for identifier in "def"]
    records = [paper(f"left-{i}", left) for i in range(5)] + [paper(f"right-{i}", right) for i in range(5)]
    records += [paper("bridge", [left[-1], right[0]]), paper("isolated", [author("g", "g")])]
    network = build_author_network(records, "community")
    nodes = node_map(network)
    assert nodes["a"]["cluster_id"] == nodes["b"]["cluster_id"] == nodes["c"]["cluster_id"]
    assert nodes["d"]["cluster_id"] == nodes["e"]["cluster_id"] == nodes["f"]["cluster_id"]
    assert len({nodes["a"]["cluster_id"], nodes["d"]["cluster_id"], nodes["g"]["cluster_id"]}) == 3
    # Each triangle contains 15 edge-weight units, plus one bridge: Q=30/31-1/2.
    assert network["stats"]["community_modularity"] == pytest.approx(30 / 31 - 0.5, abs=1e-8)
    assert_valid(network)


@pytest.mark.parametrize("mode", ["id", "name", "institution", "community", "topic"])
def test_all_results_are_stable_after_paper_author_and_alias_order_reversal(mode):
    records = [paper("a", [author("x", "X", aliases=["orcid:x", "scopus:x"], affiliations=["University B", "University A"]),
                           author("y", "Y", affiliations=["University B"])]),
               paper("b", [author("orcid:x", "X"), author("z", "Z")], topic_id="topic-2")]
    reordered = copy.deepcopy(list(reversed(records)))
    for record in reordered:
        record["authors"].reverse()
        for item in record["authors"]:
            for key in ("aliases", "affiliations"):
                if key in item:
                    item[key].reverse()
    assert build_author_network(records, mode) == build_author_network(reordered, mode)


def test_display_cap_keeps_full_author_export_and_uncapped_induced_edges():
    people = [author(f"id-{i:03d}", f"Person {i:03d}") for i in range(125)]
    records = [paper("large-paper", people)]
    network = build_author_network(records, "community")
    assert_valid(network)
    assert network["stats"]["authors_total"] == len(network["export_data"]["authors"]) == 125
    assert network["stats"]["displayed_authors"] == 120
    assert network["stats"]["edges_total"] == len(network["export_data"]["edges"]) == 120 * 119 // 2
    assert network["stats"]["edges_scope"] == network["stats"]["clusters_scope"] == "displayed_authors"
    assert len(network["edges"]) == 240
    assert len(network["clusters"]) == 1  # Uses all induced edges before drawing cap.
    hidden = network["export_data"]["authors"][120:]
    assert all(node["cluster_id"] is None and node["x"] is None and node["y"] is None for node in hidden)
    assert all(node["cluster_label"] == "未分類（表示範囲外）" for node in hidden)
    by_id = build_author_network(records, "id")
    assert all(node["cluster_id"] for node in by_id["export_data"]["authors"])


def test_topic_group_uses_full_paper_count_and_stable_tie_break():
    records = [paper("a", [author("a")], topic_id="topic-2"),
               paper("b", [author("a")], topic_id="topic-1")]
    network = build_author_network(records, "topic", [{"id": "topic-1", "label": "Steel"}, {"id": "topic-2", "label": "Laser"}])
    node = network["nodes"][0]
    assert node["topic_id"] == "topic-1" and node["cluster_label"] == "Steel"
    assert node["topic_counts"] == {"topic-1": 1, "topic-2": 1}


def test_empty_unknown_and_invalid_metadata_are_finite_and_explicit():
    assert_valid(build_author_network([]))
    network = build_author_network([paper("a", [{}, {"id": "known"}], citations=float("nan"), year=None)])
    assert network["stats"]["authors_total"] == 1
    assert network["stats"]["author_records_without_id_or_name"] == 1
    assert network["nodes"][0]["years"] == []
    assert network["nodes"][0]["citation_known_papers"] == 0
    assert_valid(network)


def test_wrapper_retains_tuple_contract_and_total_authors():
    network, count = _build_network([paper("p1", [author("a"), author("b", "B")])])
    assert count == network["stats"]["authors_total"] == 2
    assert network["group_by"] == "community"


def test_invalid_grouping_and_duplicate_paper_ids_are_rejected():
    with pytest.raises(ValueError, match="表示方法"):
        build_author_network([], "unknown")
    with pytest.raises(ValueError, match="重複"):
        build_author_network([paper("same", []), paper("same", [])])
