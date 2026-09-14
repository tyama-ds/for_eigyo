from copy import deepcopy
import json

import numpy as np
import pytest

from app import map_terrain as terrain


def node(identifier, x, y, **extra):
    return {"id": identifier, "x": x, "y": y, **extra}


def grid(result):
    return np.array(result["grid"]["values"]).reshape(result["grid"]["height"], result["grid"]["width"])


def test_gaussian_peak_and_node_height_share_relative_grid():
    result = terrain.build_terrain([node("center", .5, .5)])
    values = grid(result)
    assert values.shape == (33, 49)
    assert values[16, 24] == result["node_heights"]["center"] == 1
    assert values[16, 24] > values[16, 28] > values[16, 32] > values[16, 36]
    np.testing.assert_array_equal(values, np.flipud(values))
    np.testing.assert_array_equal(values, np.fliplr(values))
    assert all(path[0] == path[-1] for contour in result["contours"] for path in contour["paths"])
    assert result["meta"]["displayed_papers"] == 1
    assert result["meta"]["normalization"] == "grid_peak"
    json.dumps(result, allow_nan=False)


def test_two_separated_groups_form_two_high_density_islands():
    result = terrain.build_terrain([node("left", .25, .5), node("right", .75, .5)])
    high = next(c for c in result["contours"] if c["level"] == .72)
    assert len(high["paths"]) == 2
    assert all(path[0] == path[-1] and len(path) > 4 for path in high["paths"])
    left, right = sorted(high["paths"], key=lambda path: min(point[0] for point in path))
    assert max(p[0] for p in left) < .5 < min(p[0] for p in right)


def test_heights_count_papers_equally_and_ignore_citations_topics_years():
    nodes = [node("a", .2, .5), node("b", .8, .5), node("c", .8, .5)]
    result = terrain.build_terrain(nodes)
    assert result["node_heights"]["b"] == result["node_heights"]["c"]
    assert result["node_heights"]["b"] > 1.8 * result["node_heights"]["a"]
    modified = [dict(p, citations=999999 * i, year=2100 - i, topic=i) for i, p in enumerate(nodes)]
    assert terrain.build_terrain(modified) == result
    assert result["meta"]["weighting"] == "equal_per_displayed_paper"


@pytest.mark.parametrize("nodes", [[], None, {}, "not a map"])
def test_empty_maps_have_zero_height_and_no_contours(nodes):
    result = terrain.build_terrain(nodes)
    assert not np.any(grid(result))
    assert result["node_heights"] == {}
    assert all(c["paths"] == [] for c in result["contours"])
    assert result["meta"]["displayed_papers"] == 0


def test_invalid_nodes_and_duplicate_ids_are_excluded_without_moving_points():
    nodes = [node("valid", .5, .5), node("valid", .1, .1), node("nan", float("nan"), .2),
             node("infinity", .2, float("inf")), node("outside", -.01, .2),
             node("bool", True, .2), node("str", ".4", .2), node("", .2, .2), None]
    before = deepcopy(nodes)
    result = terrain.build_terrain(nodes)
    assert result["meta"]["input_papers"] == 9
    assert result["meta"]["displayed_papers"] == 1
    assert result["meta"]["excluded_papers"] == 8
    assert result["meta"]["truncated_papers"] == 0
    assert terrain.build_terrain(list(reversed(nodes))) == result
    assert nodes[0] == before[0] and nodes[-1] is None
    json.dumps(result, allow_nan=False)


def test_coincident_papers_and_boundary_points_remain_finite():
    coincident = terrain.build_terrain([node(str(i), .5, .5) for i in range(10)])
    single = terrain.build_terrain([node("0", .5, .5)])
    assert coincident["grid"] == single["grid"]
    assert coincident["contours"] == single["contours"]
    boundary = terrain.build_terrain([node("edge", 0, .5)])
    assert boundary["node_heights"]["edge"] == 1
    assert all(path[0] != path[-1] for c in boundary["contours"] for path in c["paths"])
    for contour in boundary["contours"]:
        for path in contour["paths"]:
            assert path[0][0] == path[-1][0] == 0
    json.dumps(boundary, allow_nan=False)


def test_linear_gradient_contour_has_interpolated_position_and_stitched_endpoints():
    values = np.tile(np.linspace(0, 1, 7), (8, 1))
    contour = terrain.contours_for_grid(values, [.25])[0]
    assert len(contour["paths"]) == 1
    path = contour["paths"][0]
    assert all(point[0] == .25 for point in path)
    assert sorted([path[0], path[-1]]) == [[.25, 0], [.25, 1]]
    assert len(path) == 8


@pytest.mark.parametrize("values,expected_edge_pairs", [
    ([[3, 0], [0, 3]], {(0, 1), (2, 3)}),
    ([[1.5, 0], [0, 1.5]], {(0, 3), (1, 2)}),
    ([[0, 3], [3, 0]], {(0, 3), (1, 2)}),
    ([[0, 1.5], [1.5, 0]], {(0, 1), (2, 3)}),
])
def test_saddle_decider_preserves_connectivity(values, expected_edge_pairs):
    paths = terrain.contours_for_grid(np.array(values), [1])[0]["paths"]
    def edge(point):
        x, y = point
        return 0 if y == 0 else 1 if x == 1 else 2 if y == 1 else 3
    assert {tuple(sorted((edge(path[0]), edge(path[-1])))) for path in paths} == expected_edge_pairs


@pytest.mark.parametrize("values", [np.ones((3, 3)), np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]])])
def test_exact_level_vertices_and_plateaus_do_not_divide_by_zero(values):
    contours = terrain.contours_for_grid(values, [1])
    json.dumps(contours, allow_nan=False)
    for contour in contours:
        for path in contour["paths"]:
            assert len(path) >= 2
            assert all(0 <= coordinate <= 1 for point in path for coordinate in point)


def test_large_map_is_bounded_and_canonical():
    nodes = [node(f"paper-{i:04}", (i % 20) / 20, (i // 20) / 25) for i in range(450)]
    result = terrain.build_terrain(nodes)
    assert result["meta"]["displayed_papers"] == 400
    assert result["meta"]["truncated_papers"] == 50
    assert len(result["node_heights"]) == 400
    assert terrain.build_terrain(nodes[::-1]) == result
    assert np.isfinite(grid(result)).all()
    assert 0 <= grid(result).min() <= grid(result).max() == 1


def test_cache_tracks_coordinates_is_bounded_and_does_not_share_mutable_results():
    terrain._cached_terrain.cache_clear()
    nodes = [node("a", .2, .3), node("b", .4, .5)]
    original = terrain.build_terrain(nodes)
    original["grid"]["values"][0] = "mutated"
    original["node_heights"].clear()
    again = terrain.build_terrain(nodes[::-1])
    assert again["grid"]["values"][0] != "mutated"
    assert len(again["node_heights"]) == 2
    assert terrain._cached_terrain.cache_info().hits == 1
    assert terrain.build_terrain([node("a", .9, .9), nodes[1]])["grid"] != again["grid"]
    assert terrain._cached_terrain.cache_info().maxsize == 32


def test_node_height_interpolates_surrounding_surface_cells():
    x, y = .173, .617
    result = terrain.build_terrain([node("offgrid", x, y), node("neighbor", .25, .6)])
    values = grid(result)
    x_index, y_index = x * 48, y * 32
    column, row = int(x_index), int(y_index)
    dx, dy = x_index - column, y_index - row
    expected = ((1 - dy) * ((1 - dx) * values[row, column] + dx * values[row, column + 1])
                + dy * ((1 - dx) * values[row + 1, column] + dx * values[row + 1, column + 1]))
    assert result["node_heights"]["offgrid"] == pytest.approx(expected, abs=1e-7)
