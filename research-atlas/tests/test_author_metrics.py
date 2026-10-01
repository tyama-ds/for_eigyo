"""Analytic graph fixtures exercise ranking meaning, scope and missingness."""
import csv
from copy import deepcopy
import io
import json

import numpy as np
import pytest

from app import author_network
from app.author_exports import network_csv
from app.author_network import build_author_network
from app.large_storage import display_network


def graph(edges=(), isolates=(), affiliations=None, group_by="institution"):
    affiliations = affiliations or {}

    def person(identifier):
        return {"id": identifier, "name": identifier, "affiliations": affiliations.get(identifier, [])}

    papers = []
    for source, target, weight in edges:
        for index in range(weight):
            papers.append({"id": f"{source}-{target}-{index}", "authors": [person(source), person(target)]})
    papers.extend({"id": "isolated-" + identifier, "authors": [person(identifier)]} for identifier in isolates)
    return build_author_network(papers, group_by)


def metrics(network):
    return {node["id"]: node["metrics"] for node in network["nodes"]}


def test_star_highlights_broker_but_not_local_cohesion():
    network = graph([("center", leaf, 1) for leaf in "abcd"])
    scores = metrics(network)
    assert scores["center"]["degree"] == scores["center"]["strength"] == 4
    assert scores["center"]["betweenness"] == pytest.approx(1)
    assert all(scores[leaf]["betweenness"] == 0 for leaf in "abcd")
    assert all(row["local_clustering"] == 0 for row in scores.values())
    assert all(scores["center"]["pagerank"] > scores[leaf]["pagerank"] for leaf in "abcd")


def test_triangle_is_cohesive_without_brokerage():
    scores = metrics(graph([("a", "b", 1), ("b", "c", 1), ("c", "a", 1)]))
    for row in scores.values():
        assert row["degree"] == row["strength"] == 2
        assert row["local_clustering"] == 1
        assert row["betweenness"] == 0
        assert row["pagerank"] == pytest.approx(1 / 3)


def test_path_uses_global_normalization_without_endpoints():
    scores = metrics(graph([("a", "b", 1), ("b", "c", 1), ("c", "d", 1)]))
    assert scores["a"]["betweenness"] == scores["d"]["betweenness"] == 0
    assert scores["b"]["betweenness"] == pytest.approx(2 / 3)
    assert scores["c"]["betweenness"] == pytest.approx(2 / 3)


def test_multiple_shortest_paths_share_credit():
    scores = metrics(graph([("a", "b", 1), ("b", "c", 1), ("c", "d", 1), ("d", "a", 1)]))
    assert all(row["betweenness"] == pytest.approx(1 / 6) for row in scores.values())


def test_disconnected_path_preserves_population_denominator_and_isolate():
    network = graph([("a", "b", 1), ("b", "c", 1)], isolates=["d"])
    scores = metrics(network)
    assert scores["b"]["betweenness"] == pytest.approx(1 / 3)
    assert scores["d"]["degree"] == scores["d"]["strength"] == 0
    assert scores["d"]["betweenness"] == scores["d"]["local_clustering"] == 0
    # Uniform dangling redistribution gives d=(.15+.85*d)/4.
    assert scores["d"]["pagerank"] == pytest.approx(.15 / 3.15)
    assert sum(row["pagerank"] for row in scores.values()) == pytest.approx(1)
    isolate = next(node for node in network["nodes"] if node["id"] == "d")
    assert isolate["neighbor_ids"] == [] and isolate["metrics_computed"]
    assert isolate["neighbor_weights"] == {}


@pytest.mark.parametrize("isolates", [[], ["a"], ["a", "b"]])
def test_empty_and_edgeless_graphs_have_finite_explicit_metrics(isolates):
    network = graph(isolates=isolates)
    json.dumps(network, allow_nan=False)
    assert network["metrics_scope"]["node_count"] == len(isolates)
    assert network["metrics_scope"]["edge_count"] == 0
    for row in metrics(network).values():
        assert row["degree"] == row["strength"] == 0
        assert row["betweenness"] == row["local_clustering"] == 0
        assert row["pagerank"] == pytest.approx(1 / len(isolates))
        assert row["institution_bridge"] is None


def test_two_connected_authors_do_not_divide_by_zero():
    scores = metrics(graph([("a", "b", 3)]))
    for row in scores.values():
        assert row["degree"] == 1 and row["strength"] == 3
        assert row["betweenness"] == row["local_clustering"] == 0
        assert row["pagerank"] == pytest.approx(.5)


def test_pagerank_respects_weights_and_matches_stationary_linear_solution():
    network = graph([("a", "b", 3), ("b", "c", 1)], isolates=["d"])
    scores = metrics(network)
    weights = np.array([[0, 3, 0, 0], [3, 0, 1, 0], [0, 1, 0, 0], [0, 0, 0, 0]], dtype=float)
    transition = np.array([row / row.sum() if row.sum() else np.ones(4) / 4 for row in weights])
    expected = np.linalg.solve(np.eye(4) - .85 * transition.T, np.full(4, .15 / 4))
    assert [scores[key]["pagerank"] for key in "abcd"] == pytest.approx(expected, abs=1e-11)
    assert scores["a"]["pagerank"] > scores["c"]["pagerank"]
    assert scores["a"]["degree"] == scores["c"]["degree"] == 1
    assert scores["a"]["strength"] == 3 and scores["c"]["strength"] == 1
    assert network["metrics_scope"]["pagerank_converged"]


def test_affiliation_bridges_count_distinct_known_primary_institutions():
    edges = [("a", other, 1) for other in "bcdef"]
    affiliation = {"a": ["University A"], "b": ["University A"], "c": ["University B"],
                   "d": ["University B"], "e": ["University C"]}
    scores = metrics(graph(edges, affiliations=affiliation))
    assert scores["a"]["institution_bridge"] == 2
    assert scores["b"]["institution_bridge"] == 0
    assert scores["c"]["institution_bridge"] == scores["e"]["institution_bridge"] == 1
    assert scores["f"]["institution_bridge"] is None
    assert metrics(graph(isolates=["a"], affiliations=affiliation))["a"]["institution_bridge"] == 0


def test_paper_affiliations_cannot_become_author_bridge_evidence():
    papers = [{"id": "p", "authors": [{"id": "a", "name": "a"}, {"id": "b", "name": "b"}],
               "affiliations": ["University A", "University B"]}]
    network = build_author_network(papers, "institution")
    assert all(row["institution_bridge"] is None for row in metrics(network).values())
    assert network["metrics_scope"]["authors_with_known_primary_institution"] == 0


def test_grouping_and_drawing_cap_do_not_change_scores(monkeypatch):
    edges = [("a", "b", 3), ("b", "c", 1), ("c", "d", 2), ("b", "d", 1)]
    affiliation = {"a": ["University A"], "b": ["University B"], "c": ["University C"]}
    baseline = graph(edges, affiliations=affiliation)
    monkeypatch.setattr(author_network, "EDGE_LIMIT", 1)
    for group_by in ("id", "name", "institution", "community", "topic"):
        network = graph(edges, affiliations=affiliation, group_by=group_by)
        assert metrics(network) == metrics(baseline)
        assert len(network["edges"]) == 1
        assert network["metrics_scope"]["edge_count"] == len(edges)
        assert network["metrics_scope"]["full_edge_set"] is True
        assert next(node for node in network["nodes"] if node["id"] == "b")["neighbor_ids"] == ["a", "c", "d"]
        assert next(node for node in network["nodes"] if node["id"] == "b")["neighbor_weights"] == {"a": 3, "c": 1, "d": 1}


def test_real_display_caps_keep_full_induced_scores_and_unknown_outside_population():
    people = [{"id": f"id-{index:03d}", "name": f"Person {index}"} for index in range(125)]
    network = build_author_network([{"id": "large", "authors": people}], "institution")
    assert len(network["edges"]) == 240
    assert network["metrics_scope"]["node_count"] == 120
    assert network["metrics_scope"]["edge_count"] == 120 * 119 // 2
    for node in network["nodes"]:
        assert node["metrics"]["degree"] == node["metrics"]["strength"] == 119
        assert node["metrics"]["local_clustering"] == 1
        assert node["metrics"]["betweenness"] == 0
        assert len(node["neighbor_ids"]) == 119
        assert node["neighbor_weights"] == {identifier: 1 for identifier in node["neighbor_ids"]}
    for node in network["export_data"]["authors"][120:]:
        assert not node["metrics_computed"] and node["neighbor_ids"] is None
        assert node["neighbor_weights"] is None
        assert all(value is None for value in node["metrics"].values())
    bounded = display_network(network)
    assert "export_data" not in bounded and len(bounded["edges"]) == 240
    last = bounded["nodes"][-1]
    assert len(last["neighbor_weights"]) == 119
    assert last["neighbor_weights"] == network["nodes"][-1]["neighbor_weights"]


def test_bounded_browser_payload_retains_observed_weights_for_undrawn_neighbors(monkeypatch):
    monkeypatch.setattr(author_network, "EDGE_LIMIT", 1)
    network = graph([("a", "b", 3), ("b", "c", 2), ("c", "d", 1)])
    bounded = display_network(network)
    assert "export_data" not in bounded
    assert [(edge["source"], edge["target"]) for edge in bounded["edges"]] == [("a", "b")]
    selected = next(node for node in bounded["nodes"] if node["id"] == "c")
    assert selected["neighbor_weights"] == {"b": 2, "d": 1}
    assert selected["neighbor_ids"] == ["b", "d"]


def test_csv_preserves_metrics_missingness_scope_and_definitions(monkeypatch):
    monkeypatch.setattr(author_network, "AUTHOR_LIMIT", 2)
    network = graph([("a", "b", 1), ("b", "c", 1)])
    network.update(id="network", result_id="result")
    rows = {row["Author ID"]: row for row in csv.DictReader(io.StringIO(network_csv(network, "authors").lstrip("\ufeff")))}
    assert rows["a"]["Degree"] == "1"
    assert rows["a"]["Betweenness centrality (normalized)"] == "0.0"
    assert rows["a"]["External primary institutions"] == ""  # Unknown affiliation.
    assert rows["c"]["Degree"] == "" and rows["c"]["Metrics computed"] == "False"
    assert json.loads(rows["a"]["Neighbor IDs (JSON)"]) == ["b"]
    assert json.loads(rows["c"]["Neighbor IDs (JSON)"]) is None
    metadata = json.loads(rows["a"]["Scope and methodology (JSON)"])
    assert metadata["metrics_scope"]["node_count"] == 2
    assert metadata["metric_definitions"]["betweenness"]["formula"]
    assert metadata["metric_definitions"]["local_clustering"]["direction"] == "descending"


def test_metrics_do_not_mutate_input_or_share_definition_state():
    papers = [{"id": "p", "authors": [{"id": "a", "name": "a"}, {"id": "b", "name": "b"}]}]
    before = deepcopy(papers)
    network = build_author_network(papers)
    assert papers == before
    network["metric_definitions"]["degree"]["label"] = "changed"
    assert build_author_network(papers)["metric_definitions"]["degree"]["label"] == "近隣著者数"
