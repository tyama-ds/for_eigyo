"""Deterministic density relief for the papers already shown on a 2-D map.

The height is a relative visual density of projected points, not a forecast,
citation score, or an estimate of the number of papers outside this map.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from functools import lru_cache
import math

import numpy as np


GRID_WIDTH = 49
GRID_HEIGHT = 33
BANDWIDTH = 0.075
MAX_NODES = 400
LEVELS = (0.12, 0.24, 0.36, 0.48, 0.60, 0.72, 0.84, 0.96)


def _normalized_nodes(nodes):
    """Validate without changing stored coordinates; canonicalize cache keys."""
    if not isinstance(nodes, (list, tuple)):
        return (), 0, 0, 0
    valid = []
    excluded = 0
    for node in nodes:
        if not isinstance(node, dict):
            excluded += 1
            continue
        identifier, x, y = node.get("id"), node.get("x"), node.get("y")
        if (not isinstance(identifier, str) or not identifier.strip()
                or isinstance(x, bool) or isinstance(y, bool)
                or not isinstance(x, (int, float)) or not isinstance(y, (int, float))
                or not 0 <= x <= 1 or not 0 <= y <= 1
                or not math.isfinite(x) or not math.isfinite(y)):
            excluded += 1
            continue
        valid.append((identifier, float(x), float(y)))
    # A duplicated paper ID is one paper even if a malformed map repeats it.
    unique = {}
    for identifier, x, y in sorted(valid):
        if identifier in unique:
            excluded += 1
        else:
            unique[identifier] = (identifier, x, y)
    accepted = tuple(unique.values())
    truncated = max(0, len(accepted) - MAX_NODES)
    return accepted[:MAX_NODES], len(nodes), excluded, truncated


def _join_segments(segments):
    """Join shared cell edges into deterministic open lines or closed loops."""
    edges = set()
    for first, second in segments:
        first = tuple(round(float(v), 11) for v in first)
        second = tuple(round(float(v), 11) for v in second)
        if first != second:
            edges.add(tuple(sorted((first, second))))
    adjacent = defaultdict(set)
    for first, second in edges:
        adjacent[first].add(second)
        adjacent[second].add(first)
    unused = set(edges)
    paths = []

    def walk(start, neighbor):
        path = [start]
        previous, current = start, neighbor
        while True:
            unused.discard(tuple(sorted((previous, current))))
            path.append(current)
            if current == start or len(adjacent[current]) != 2:
                break
            choices = [p for p in sorted(adjacent[current])
                       if tuple(sorted((current, p))) in unused]
            if not choices:
                break
            previous, current = current, choices[0]
        rounded = []
        for point in path:
            value = [round(point[0], 7), round(point[1], 7)]
            if not rounded or value != rounded[-1]:
                rounded.append(value)
        if len(rounded) >= 2:
            paths.append(rounded)

    # Boundary paths and rare exact-level junctions precede closed components.
    for point in sorted(adjacent):
        if len(adjacent[point]) != 2:
            for neighbor in sorted(adjacent[point]):
                if tuple(sorted((point, neighbor))) in unused:
                    walk(point, neighbor)
    while unused:
        first, second = min(unused)
        walk(first, second)
    return paths


def contours_for_grid(values: np.ndarray, levels=LEVELS):
    """Marching squares with an asymptotic decider for ambiguous saddle cells."""
    values = np.asarray(values, dtype=float)
    if values.ndim != 2 or min(values.shape) < 2 or not np.isfinite(values).all():
        raise ValueError("等高線には有限値の二次元グリッドが必要です。")
    height, width = values.shape
    combinations = {
        1: ((0, 3),), 2: ((0, 1),), 3: ((1, 3),),
        4: ((1, 2),), 6: ((0, 2),), 7: ((2, 3),),
        8: ((2, 3),), 9: ((0, 2),), 11: ((1, 2),),
        12: ((1, 3),), 13: ((0, 1),), 14: ((0, 3),),
    }
    output = []
    for level in levels:
        segments = []
        for row in range(height - 1):
            for column in range(width - 1):
                scalar = (values[row, column], values[row, column + 1],
                          values[row + 1, column + 1], values[row + 1, column])
                case = sum((1 << i) for i, value in enumerate(scalar) if value >= level)
                if case in {0, 15}:
                    continue
                pairs = combinations.get(case)
                if case in {5, 10}:
                    a, b, c, d = (value - level for value in scalar)
                    pairs = ((0, 1), (2, 3)) if a * c - b * d >= 0 else ((0, 3), (1, 2))
                corners = ((column, row), (column + 1, row),
                           (column + 1, row + 1), (column, row + 1))
                intersections = {}
                for edge in {edge for pair in pairs for edge in pair}:
                    other = (edge + 1) % 4
                    fraction = (level - scalar[edge]) / (scalar[other] - scalar[edge])
                    first, second = corners[edge], corners[other]
                    intersections[edge] = (
                        (first[0] + fraction * (second[0] - first[0])) / (width - 1),
                        (first[1] + fraction * (second[1] - first[1])) / (height - 1),
                    )
                segments.extend((intersections[first], intersections[second]) for first, second in pairs)
        output.append({"level": float(level), "paths": _join_segments(segments)})
    return output


def _sample_grid(grid, x, y):
    """Sample bilinear density; the renderer approximates this grid with triangles."""
    row, column = y * (GRID_HEIGHT - 1), x * (GRID_WIDTH - 1)
    low_row, low_column = int(row), int(column)
    high_row, high_column = min(low_row + 1, GRID_HEIGHT - 1), min(low_column + 1, GRID_WIDTH - 1)
    dy, dx = row - low_row, column - low_column
    return float(grid[low_row, low_column] * (1 - dx) * (1 - dy)
                 + grid[low_row, high_column] * dx * (1 - dy)
                 + grid[high_row, low_column] * (1 - dx) * dy
                 + grid[high_row, high_column] * dx * dy)


@lru_cache(maxsize=32)
def _cached_terrain(nodes, input_count, excluded, truncated):
    x_grid, y_grid = np.meshgrid(np.linspace(0, 1, GRID_WIDTH), np.linspace(0, 1, GRID_HEIGHT))
    density = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=float)
    for _, x, y in nodes:
        density += np.exp(-((x_grid - x) ** 2 + (y_grid - y) ** 2) / (2 * BANDWIDTH ** 2))
    if nodes:
        # Every displayed paper contributes the same kernel, irrespective of
        # citations/year/topic. Only relative height is exposed to the browser.
        density /= len(nodes)
        density /= float(density.max())
    density = np.round(density, 7)
    return {
        "version": 1,
        "grid": {"width": GRID_WIDTH, "height": GRID_HEIGHT, "values": density.ravel().tolist()},
        "contours": contours_for_grid(density),
        "node_heights": {identifier: round(_sample_grid(density, x, y), 7) for identifier, x, y in nodes},
        "meta": {
            "method": "gaussian_kde", "bandwidth": BANDWIDTH,
            "weighting": "equal_per_displayed_paper", "normalization": "grid_peak",
            "displayed_papers": len(nodes), "input_papers": input_count,
            "excluded_papers": excluded, "truncated_papers": truncated,
            "max_papers": MAX_NODES, "coordinate_domain": [0, 1],
            "interpretation": "高さと等高線は、地図に表示した論文の二次元座標の相対密度です。引用数・将来性・分野全体の論文数を表しません。",
        },
    }


def build_terrain(nodes):
    """Return a request-owned result from a bounded, in-memory geometry cache."""
    return deepcopy(_cached_terrain(*_normalized_nodes(nodes)))
