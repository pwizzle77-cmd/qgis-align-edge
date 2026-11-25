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


class SliverCandidate(object):
    __slots__ = ("src", "tgt", "shift", "plan", "label")

    def __init__(self, src, tgt, shift, plan, label):
        self.src = src
        self.tgt = tgt
        self.shift = shift
        self.plan = plan
        self.label = label


def find_candidates(graph, base_layer_ids, tolerance, nearby_factor, label_of):
    """Edges not on a base layer with a valid, in-tolerance alignment to a
    base-layer edge sharing both of their end nodes (the same candidates
    ``targets()`` offers for a single hover, restricted to the base layer
    and ranked the same way: smallest shift first).

    Returns ``(candidates, skipped)``. ``skipped`` lists edges that are
    close enough (within ``tolerance``) but that ``plan_align`` itself
    refuses (a curved or multiply-traversed feature, for example) - not
    silently dropped.
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
        label = label_of(eid)
        if plan.ok:
            candidates.append(SliverCandidate(eid, tgt, shift, plan, label))
        else:
            skipped.append((label, plan.error))
    return candidates, skipped


def apply_candidates(candidates, graph, ctx):
    """Align each candidate's already-built plan, one at a time.

    ``build_changes`` checks every source and target feature against the
    live cache before writing, so a candidate whose feature an earlier one
    in this batch already changed is refused here, not written over -
    running this again afterwards picks up anything left for that reason.

    Returns ``(done, failed)``; ``failed`` as ``(label, reason)``.
    """
    done, failed = [], []
    for cand in candidates:
        try:
            changes = build_changes(cand.plan, graph, ctx)
            apply_changes(changes, ctx)
            done.append(cand.label)
        except AlignFailed as ex:
            failed.append((cand.label, str(ex)))
        except Exception as ex:
            failed.append((cand.label, "unexpected error: {}".format(ex)))
    return done, failed
