"""Closed, connected preview bodies reconstructed from print cross-sections."""

from __future__ import annotations

import math
import threading
from array import array
from collections import defaultdict
from typing import Any

import shapely  # type: ignore[import-untyped]
from shapely import LineString, Polygon, STRtree

_SIMPLIFY_LOCK = threading.Lock()


def simplify_faces(
    faces: tuple[tuple[float, ...], ...], parts: tuple[int, ...], budget: int = 6000
) -> tuple[tuple[tuple[float, ...], ...], tuple[int, ...]]:
    """Reduce each body's detail separately; retain every physical component."""
    if len(faces) <= budget:
        return faces, parts
    import fast_simplification  # type: ignore[import-untyped]
    import numpy as np

    source = np.asarray(faces, dtype=np.float64)
    labels = np.asarray(parts)
    unique, counts = np.unique(labels, return_counts=True)
    output, identities = [], []
    for part, count in zip(unique, counts, strict=True):
        group = source[labels == part]
        vertices = group[:, :12].reshape((-1, 4, 3))
        triangles = np.concatenate((vertices[:, [0, 1, 2]], vertices[:, [0, 2, 3]]))
        normals = np.concatenate((group[:, 12:15], group[:, 12:15]))
        cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
        valid = np.linalg.norm(cross, axis=1) > 1e-8
        triangles, normals, cross = triangles[valid], normals[valid], cross[valid]
        reverse = np.einsum("ij,ij->i", cross, normals) < 0
        triangles[reverse] = triangles[reverse][:, [0, 2, 1]]
        points, inverse = np.unique(
            np.round(triangles.reshape((-1, 3)), 4), axis=0, return_inverse=True
        )
        indices = inverse.reshape((-1, 3)).astype(np.int32)
        target = max(24, int((budget - 24 * len(unique)) * int(count) / len(faces)))
        original_points, original_indices = points, indices
        # Layer reconstructions contain T-junctions and microscopic ledges.
        # Weld sub-pixel detail first so the native decimator can collapse it.
        for spacing in (0.12, 0.24, 0.48, 0.96, 1.92, 3.84):
            if len(indices) <= target:
                break
            _, representatives, mapping = np.unique(
                np.rint(original_points / spacing).astype(np.int64),
                axis=0,
                return_index=True,
                return_inverse=True,
            )
            reduced = mapping[original_indices]
            valid = (
                (reduced[:, 0] != reduced[:, 1])
                & (reduced[:, 1] != reduced[:, 2])
                & (reduced[:, 2] != reduced[:, 0])
            )
            reduced = reduced[valid]
            _, first = np.unique(np.sort(reduced, axis=1), axis=0, return_index=True)
            reduced = reduced[first]
            if not len(reduced):
                break
            candidate = original_points[representatives]
            with _SIMPLIFY_LOCK:
                if len(reduced) > target:
                    reduced_points, reduced_indices = fast_simplification.simplify(
                        candidate, reduced.astype(np.int32), target_count=target, agg=7
                    )
                else:
                    reduced_points, reduced_indices = candidate, reduced
            if len(reduced_indices):
                points, indices = reduced_points, reduced_indices
        if len(indices) > target:
            with _SIMPLIFY_LOCK:
                reduced_points, reduced_indices = fast_simplification.simplify(
                    points, indices, target_count=target, agg=7
                )
            if len(reduced_indices):
                points, indices = reduced_points, reduced_indices
        for a, b, c in points[indices]:
            normal = np.cross(b - a, c - a)
            length = float(np.linalg.norm(normal))
            if length > 1e-8:
                normal /= length
                output.append(
                    tuple(float(v) for p in (a, b, c, c) for v in p)
                    + tuple(float(v) for v in normal)
                )
                identities.append(int(part))
    return tuple(output), tuple(identities)


class Components:
    def __init__(self) -> None:
        self.parents: array[int] = array("I")

    def add(self) -> int:
        index = len(self.parents)
        self.parents.append(index)
        return index

    def root(self, index: int) -> int:
        while index != self.parents[index]:
            self.parents[index] = self.parents[self.parents[index]]
            index = self.parents[index]
        return index

    def join(self, first: int, second: int) -> None:
        first, second = self.root(first), self.root(second)
        self.parents[max(first, second)] = min(first, second)


def cross_section(segments: list[tuple[float, ...]], width: float = 0.44) -> Any:
    """Use closed exterior loops with even-odd holes; buffer open extrusion paths."""
    lines = [
        LineString(((s[0], s[1]), (s[3], s[4])))
        for s in segments
        if math.hypot(s[3] - s[0], s[4] - s[1]) > 1e-5
    ]
    if not lines:
        return Polygon()
    polygons = list(shapely.get_parts(shapely.polygonize(lines)))
    if not polygons:
        return shapely.union_all(lines).buffer(width / 2, cap_style="flat", join_style="mitre")
    # Polygonize returns the inner region of a hole as another face. Classify
    # faces against all unique rings so cutouts retain their empty interior.
    rings: dict[bytes, Any] = {}
    for polygon in polygons:
        for ring in [polygon.exterior, *polygon.interiors]:
            boundary = shapely.normalize(Polygon(ring))
            rings[boundary.wkb] = boundary
    selected = []
    boundaries = list(rings.values())
    tree = STRtree(boundaries)
    for polygon in polygons:
        point = polygon.representative_point()
        depth = sum(boundaries[int(i)].covers(point) for i in tree.query(point))
        if depth % 2:
            selected.append(polygon)
    return (
        shapely.union_all(selected)
        .buffer(width / 2, join_style="mitre")
        .simplify(0.06, preserve_topology=True)
    )


def body_faces(
    positions: Any, layer_height: float = 0.2, width: float = 0.44
) -> tuple[tuple[tuple[float, ...], ...], tuple[int, ...]]:
    """Track connected material between layers, merge identical sections, cap holes."""
    layers: dict[float, list[tuple[float, ...]]] = defaultdict(list)
    for index in range(0, len(positions), 6):
        segment = tuple(round(float(n), 4) for n in positions[index : index + 6])
        if len(segment) == 6 and all(math.isfinite(n) and abs(n) <= 1000 for n in segment):
            layers[segment[2]].append(segment)
    components = Components()
    previous: list[tuple[Any, int]] = []
    previous_z: float | None = None
    runs: list[list[Any]] = []
    active: dict[bytes, int] = {}
    for z, segments in sorted(layers.items()):
        section = cross_section(segments, width)
        polygons = sorted(
            (p for p in shapely.get_parts(section) if isinstance(p, Polygon) and p.area > 1e-8),
            key=lambda p: p.bounds,
        )
        adjoining = previous_z is not None and z - previous_z <= max(0.1, layer_height * 3)
        low = previous_z if adjoining and previous_z is not None else max(0.0, z - layer_height)
        tree = STRtree([p for p, _ in previous]) if adjoining and previous else None
        current = []
        next_active = {}
        for polygon in polygons:
            node = components.add()
            if tree is not None:
                for index in tree.query(polygon, predicate="intersects"):
                    components.join(node, previous[int(index)][1])
            polygon = shapely.normalize(shapely.orient_polygons(polygon))
            key = polygon.wkb
            run_index = active.get(key) if adjoining else None
            if run_index is not None:
                runs[run_index][2] = z
                components.join(node, runs[run_index][3])
            else:
                run_index = len(runs)
                runs.append([polygon, low, z, node])
            next_active[key] = run_index
            current.append((polygon, node))
        previous, previous_z, active = current, z, next_active

    roots = sorted({components.root(run[3]) for run in runs})
    labels = {root: index for index, root in enumerate(roots)}
    upper: dict[float, list[Any]] = defaultdict(list)
    lower: dict[float, list[Any]] = defaultdict(list)
    for polygon, low, high, _node in runs:
        upper[high].append(polygon)
        lower[low].append(polygon)
    faces = []
    parts = []

    def append(
        vertices: list[tuple[float, float, float]], normal: tuple[float, ...], part: int
    ) -> None:
        if len(vertices) == 3:
            vertices.append(vertices[-1])
        faces.append(tuple(n for x, y, z in vertices for n in (x - 128, y - 128, z)) + normal)
        parts.append(part)

    for polygon, low, high, node in runs:
        part = labels[components.root(node)]
        # Normalize ring winding after key creation: outer CCW, holes CW.
        polygon = shapely.orient_polygons(polygon)
        for ring in [polygon.exterior, *polygon.interiors]:
            points = list(ring.coords)
            for (x, y), (u, v) in zip(points, points[1:], strict=False):
                length = math.hypot(u - x, v - y)
                if length > 1e-8 and high > low:
                    append(
                        [(x, y, low), (u, v, low), (u, v, high), (x, y, high)],
                        ((v - y) / length, (x - u) / length, 0.0),
                        part,
                    )
        for z, normal, neighbours in [
            (low, (0.0, 0.0, -1.0), upper),
            (high, (0.0, 0.0, 1.0), lower),
        ]:
            exposed = polygon.difference(shapely.union_all(neighbours.get(z, [])))
            for triangle in shapely.get_parts(shapely.constrained_delaunay_triangles(exposed)):
                append([(x, y, z) for x, y in list(triangle.exterior.coords)[:3]], normal, part)
    return tuple(faces), tuple(parts)


def crease_edges(faces: tuple[tuple[float, ...], ...]) -> tuple[tuple[bool, ...], ...]:
    """Ink geometric creases; coplanar triangulation retains plain faces."""
    shared: dict[tuple[Any, ...], list[tuple[int, int]]] = defaultdict(list)
    for index, face in enumerate(faces):
        vertices = [tuple(round(v, 4) for v in face[i : i + 3]) for i in range(0, 12, 3)]
        for edge in range(4):
            a, b = vertices[edge], vertices[(edge + 1) % 4]
            if a != b:
                shared[tuple(sorted((a, b)))].append((index, edge))
    flags = [[False] * 4 for _ in faces]
    for owners in shared.values():
        if len(owners) < 2:
            continue  # The visible silhouette is inked from the rendered mask.
        for index, edge in owners:
            normal = faces[index][12:15]
            flags[index][edge] = any(
                sum(a * b for a, b in zip(normal, faces[other][12:15], strict=True)) < 0.75
                for other, _ in owners
                if other != index
            )
    return tuple(tuple(row) for row in flags)


def continuous_faces(
    positions: Any, layer_height: float = 0.2
) -> tuple[tuple[tuple[float, ...], ...], tuple[int, ...]]:
    """Connected extrusion ribbons retain actual endpoint heights in vase paths."""
    import numpy as np

    chains: list[list[tuple[float, ...]]] = []
    endpoints: dict[tuple[float, ...], int] = {}
    connected = Components()
    for i in range(0, len(positions), 6):
        a, b = (
            tuple(round(float(v), 4) for v in positions[i : i + 3]),
            tuple(round(float(v), 4) for v in positions[i + 3 : i + 6]),
        )
        if len(b) != 3 or not all(math.isfinite(v) and abs(v) <= 1000 for v in (*a, *b)):
            continue
        chain = endpoints.get(a)
        if chain is None or chains[chain][-1] != a:
            chain = len(chains)
            chains.append([a])
            connected.add()
        other = endpoints.get(b)
        if other is not None:
            connected.join(chain, other)
        chains[chain].append(b)
        endpoints[a] = endpoints[b] = chain

    def simplify(points: Any, tolerance: float) -> Any:
        # Iterative 3D Douglas-Peucker preserves continuous Z and chain ends.
        keep = {0, len(points) - 1}
        pending = [(0, len(points) - 1)]
        while pending:
            start, end = pending.pop()
            if end - start < 2:
                continue
            vector = points[end] - points[start]
            length = float(np.dot(vector, vector))
            values = points[start + 1 : end] - points[start]
            t = np.clip(values @ vector / length, 0, 1) if length else np.zeros(len(values))
            distances = np.linalg.norm(values - t[:, None] * vector, axis=1)
            index = int(distances.argmax())
            if distances[index] > tolerance:
                middle = start + 1 + index
                keep.add(middle)
                pending.extend(((start, middle), (middle, end)))
        return points[sorted(keep)]

    arrays = [np.asarray(chain) for chain in chains if len(chain) > 1]
    outlines = [LineString(points[:, :2]) for points in arrays]
    tree = STRtree(outlines)
    ranges = [(float(points[:, 2].min()), float(points[:, 2].max())) for points in arrays]
    for index, outline in enumerate(outlines):
        low, high = ranges[index]
        for candidate_index in tree.query(outline.buffer(0.22)):
            neighbour = int(candidate_index)
            neighbour_low, neighbour_high = ranges[neighbour]
            if (
                neighbour < index
                and max(low, neighbour_low) - min(high, neighbour_high) <= layer_height + 0.001
            ):
                connected.join(index, neighbour)
    reduced = [simplify(points, 0.05) for points in arrays]
    for tolerance in (0.1, 0.2, 0.4, 0.8, 1.6):
        if sum(len(chain) - 1 for chain in reduced) <= 20_000:
            break
        reduced = [simplify(points, tolerance) for points in arrays]
    roots = sorted({connected.root(i) for i in range(len(chains))})
    labels = {root: i for i, root in enumerate(roots)}
    faces, parts = [], []
    for index, path in enumerate(reduced):
        for a, b in zip(path, path[1:], strict=False):
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            if length < 1e-6:
                continue
            vertices = [
                (*a[:2], max(0, a[2] - layer_height)),
                (*b[:2], max(0, b[2] - layer_height)),
                tuple(b),
                tuple(a),
            ]
            faces.append(
                tuple(v for x, y, z in vertices for v in (float(x) - 128, float(y) - 128, float(z)))
                + ((b[1] - a[1]) / length, (a[0] - b[0]) / length, 0.0)
            )
            parts.append(labels[connected.root(index)])
    return tuple(faces), tuple(parts)


def mesh_faces(
    vertices: Any, indices: Any
) -> tuple[tuple[tuple[float, ...], ...], tuple[int, ...], bool]:
    """Weld coincident vertices, identify bodies and build bounded mesh detail."""
    import numpy as np

    points = np.asarray(vertices, dtype=np.float32).reshape((-1, 3))
    triangles = np.asarray(indices, dtype=np.int64).reshape((-1, 3))
    if not len(triangles):
        return (), (), False
    if not np.isfinite(points).all() or (np.abs(points) > 1000).any():
        raise ValueError("Preview mesh coordinates exceed bounds")
    if triangles.min() < 0 or triangles.max() >= len(points):
        raise ValueError("Preview mesh indices exceed bounds")
    welded, inverse = np.unique(np.round(points, 4), axis=0, return_inverse=True)
    connected = Components()
    connected.parents = array("I", range(len(welded)))
    for a, b, c in inverse[triangles]:
        connected.join(int(a), int(b))
        connected.join(int(a), int(c))
    roots = np.fromiter((connected.root(i) for i in range(len(welded))), dtype=np.int64)
    vertex_parts = roots[inverse]
    unique_parts = np.unique(vertex_parts[triangles[:, 0]])
    labels = {int(root): index for index, root in enumerate(unique_parts)}
    part_ids = np.fromiter(
        (labels[int(root)] for root in vertex_parts[triangles[:, 0]]), dtype=np.int64
    )
    # Reduce detail at a fraction of a preview pixel. Each component has its
    # own cluster namespace, so disconnected bodies keep distinct identities.
    chosen = points
    chosen_triangles = triangles
    chosen_parts = part_ids
    spacing = max(float(np.ptp(points, axis=0).max()) / 4096, 0.001)
    for _ in range(16):
        if len(chosen_triangles) <= 20_000:
            break
        keys = np.column_stack((vertex_parts, np.rint(points / spacing).astype(np.int64)))
        _, representatives, mapping = np.unique(
            keys, axis=0, return_index=True, return_inverse=True
        )
        reduced = mapping[triangles]
        valid = (
            (reduced[:, 0] != reduced[:, 1])
            & (reduced[:, 1] != reduced[:, 2])
            & (reduced[:, 2] != reduced[:, 0])
        )
        reduced, reduced_parts = reduced[valid], part_ids[valid]
        _, first = np.unique(np.sort(reduced, axis=1), axis=0, return_index=True)
        chosen, chosen_triangles, chosen_parts = (
            points[representatives],
            reduced[first],
            reduced_parts[first],
        )
        # A very thin component can collapse under clustering. Retain its
        # original triangles rather than dropping the part from the preview.
        missing = np.setdiff1d(part_ids, chosen_parts)
        if len(missing):
            original = np.isin(part_ids, missing)
            offset = len(chosen)
            chosen = np.concatenate((chosen, points))
            chosen_triangles = np.concatenate((chosen_triangles, triangles[original] + offset))
            chosen_parts = np.concatenate((chosen_parts, part_ids[original]))
        spacing *= 2
    if len(chosen_triangles) > 20_000:
        raise ValueError("Preview mesh detail exceeds geometry budget")
    edges = np.concatenate(
        (chosen_triangles[:, [0, 1]], chosen_triangles[:, [1, 2]], chosen_triangles[:, [2, 0]])
    )
    _, counts = np.unique(np.sort(edges, axis=1), axis=0, return_counts=True)
    two_sided = bool((counts != 2).any())
    faces = []
    parts = []
    for triangle, part in zip(chosen_triangles, chosen_parts, strict=True):
        a, b, c = chosen[triangle].astype(float)
        normal = np.cross(b - a, c - a)
        length = float(np.linalg.norm(normal))
        if length <= 1e-8:
            continue
        normal /= length
        xyz = [(p[0] - 128, p[1] - 128, p[2]) for p in (a, b, c, c)]
        faces.append(
            tuple(float(value) for p in xyz for value in p) + tuple(float(n) for n in normal)
        )
        parts.append(int(part))
    return tuple(faces), tuple(parts), two_sided
