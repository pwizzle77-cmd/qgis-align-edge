# -*- coding: utf-8 -*-
"""Find and fix every "sliver": an edge whose only difference from the
matching edge of a chosen base layer is being digitized a hair apart,
within tolerance.

Reuses the graph's own target-finding and the single-click align's own
planning and writing code, so a batch fix is held to exactly the same
safety checks (unsafe moves, curved targets, multiply-traversed features)
as aligning one edge by hand - this just finds and confirms many at once.
"""

from .edge_writer import AlignFailed, apply_changes, build_changes
from .graph_builder import FeatureCache, build_from_snapshot

STALE = "changed since the topology was built"


class SliverCandidate(object):
    __slots__ = ("src", "tgt", "shift", "plan", "key", "label")

    def __init__(self, src, tgt, shift, plan, key, label):
        self.src = src
        self.tgt = tgt
        self.shift = shift
        self.plan = plan
        self.key = key          # stable (layer id, fid, part, ring) - same across a rebuild
        self.label = label      # for display only; may vary slightly across rebuilds


def build_graph(canvas, settings, tolerance):
    """A fresh topology graph and context for the current view, read now -
    not the interactive tool's own (which rebuilds on its own schedule, in
    slices, so it may be mid-rebuild or behind the last edit). Returns
    ``(graph, ctx, reason)``; ``graph`` is None with a reason why not."""
    cache = FeatureCache(canvas, settings, lambda reason: None)
    if not cache.sync_layers():
        return None, None, "No visible line or polygon layer is being edited."
    try:
        cache.read()
        graph, ctx = build_from_snapshot(cache.snapshot(tolerance))
        return graph, ctx, None
    except Exception as ex:
        return None, None, "Could not read the topology: {}".format(ex)


def _label(ctx, key):
    if ctx is not None:
        try:
            return "{} #{}".format(ctx.label(key[:2]), key[1])
        except Exception:
            pass
    return "{}#{}".format(key[0], key[1])


def find_candidates(graph, ctx, base_layer_ids, tolerance, nearby_factor):
    """Edges not on a base layer with a valid, in-tolerance alignment to a
    base-layer edge sharing both of their end nodes (the same candidates
    ``targets()`` offers for a single hover, restricted to the base layer
    and ranked the same way: smallest shift first).

    Returns ``(candidates, skipped)``. ``skipped`` lists edges that are
    close enough (within ``tolerance``) but that ``plan_align`` itself
    refuses (a curved or multiply-traversed feature, for example) - not
    silently dropped. Each as ``(key, label, reason)``.
    """
    candidates, skipped = [], []
    for eid, edge in enumerate(graph.edges):
        if edge.is_loop:
            continue
        if any(key[0] in base_layer_ids for key in edge.owners):
            continue  # already part of the base layer
        best = None
        for tgt, shift in graph.targets(eid, nearby_factor):
            if shift > tolerance:
                continue
            if not any(key[0] in base_layer_ids for key in graph.edges[tgt].owners):
                continue
            if best is None or shift < best[1]:
                best = (tgt, shift)
        if best is None:
            continue
        tgt, shift = best
        plan = graph.plan_align(eid, tgt)
        key = next(iter(edge.owners))
        label = _label(ctx, key)
        if plan.ok:
            candidates.append(SliverCandidate(eid, tgt, shift, plan, key, label))
        else:
            skipped.append((key, label, plan.error))
    return candidates, skipped


def run_batch(canvas, settings, base_layer_ids, tolerance, nearby_factor, max_passes=200, on_pass=None):
    """Keep scanning and aligning against fresh data until nothing more can
    be done.

    Aligning one edge can move a corner another candidate in the same pass
    also needs - ``build_changes`` catches that (the topology was built
    before the earlier fix) and this re-scans from freshly read data
    instead of giving up on everything after the first fix, which is what
    reusing one pass's plans for the whole batch would do. A candidate that
    fails for any other reason is tracked by its stable feature key (not
    its display label, which could vary) and is not retried.

    Returns ``(done, failed)``: ``done`` as a list of labels, ``failed`` as
    ``(label, reason)``. ``on_pass(pass_no, found, done_so_far)`` is called
    before each pass, for progress feedback.
    """
    done, failed = [], []
    given_up = set()
    for pass_no in range(1, max_passes + 1):
        graph, ctx, reason = build_graph(canvas, settings, tolerance)
        if graph is None:
            failed.append(("(scan)", reason))
            return done, failed
        candidates, skipped = find_candidates(graph, ctx, base_layer_ids, tolerance, nearby_factor)
        candidates = [c for c in candidates if c.key not in given_up]
        if on_pass is not None:
            on_pass(pass_no, len(candidates), len(done))
        if not candidates:
            failed.extend((label, msg) for key, label, msg in skipped if key not in given_up)
            return done, failed
        restart = False
        for cand in candidates:
            try:
                changes = build_changes(cand.plan, graph, ctx)
                apply_changes(changes, ctx)
                done.append(cand.label)
            except AlignFailed as ex:
                msg = str(ex)
                if STALE in msg:
                    restart = True
                    break
                failed.append((cand.label, msg))
                given_up.add(cand.key)
            except Exception as ex:
                msg = "unexpected error: {}".format(ex)
                failed.append((cand.label, msg))
                given_up.add(cand.key)
        if restart:
            continue
        failed.extend((label, msg) for key, label, msg in skipped if key not in given_up)
        return done, failed
    failed.append(("(unfinished)", "Stopped after {} passes; run it again to continue.".format(max_passes)))
    return done, failed
