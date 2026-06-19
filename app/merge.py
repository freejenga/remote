"""Merge visits coming from multiple parser routes (SoA + narrative).

The raw extraction keeps one ExtractedVisit per (visit, source) so that
conflict detection can compare what each source reported. For display and for
the CTMS-ready operational rows we want a single, de-duplicated visit list.
"""
from collections import OrderedDict
from typing import List

from .schemas import ExtractedVisit, ExtractedProcedure
from .mappings import normalize

# Lower confidence sources lose ties; SoA tables are the source of truth.
_ROUTE_PRIORITY = {'soa': 3, 'narrative': 2, 'llm': 1}


def _procedure_rank(proc: ExtractedProcedure) -> int:
    return _ROUTE_PRIORITY.get(proc.source.parser_route, 0)


def merge_visits_by_name(visits: List[ExtractedVisit]) -> List[ExtractedVisit]:
    """Collapse visits that share a name and drop duplicate procedures.

    Procedures are de-duplicated by their normalized name. When the same
    activity is reported by more than one source, the higher-priority source
    (SoA > narrative > llm) is kept, but any conditional/footnote detail from
    the other source is preserved if the winner lacked it.
    """
    merged: "OrderedDict[str, ExtractedVisit]" = OrderedDict()

    for visit in visits:
        target = merged.get(visit.name)
        if target is None:
            target = ExtractedVisit(
                name=visit.name,
                day=visit.day,
                window=visit.window,
                cycle_group=visit.cycle_group,
                procedures=[],
            )
            merged[visit.name] = target
        else:
            # Fill in any visit-level metadata the first occurrence missed.
            target.day = target.day if target.day is not None else visit.day
            target.window = target.window or visit.window
            target.cycle_group = target.cycle_group or visit.cycle_group

        for proc in visit.procedures:
            norm_name, _ = normalize(proc.name)
            existing = next(
                (p for p in target.procedures if normalize(p.name)[0] == norm_name),
                None,
            )
            if existing is None:
                target.procedures.append(proc.model_copy(deep=True))
                continue
            if _procedure_rank(proc) > _procedure_rank(existing):
                replacement = proc.model_copy(deep=True)
                replacement.conditional = replacement.conditional or existing.conditional
                replacement.timing = replacement.timing or existing.timing
                target.procedures[target.procedures.index(existing)] = replacement
            else:
                existing.conditional = existing.conditional or proc.conditional
                existing.timing = existing.timing or proc.timing

    return list(merged.values())
