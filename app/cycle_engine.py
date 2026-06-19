import re
from .schemas import ExtractedVisit


def _copy_visit(visit: ExtractedVisit) -> ExtractedVisit:
    return visit.model_copy(deep=True)


def infer_day(name: str) -> int | None:
    """Infer a numeric day offset from a visit name for chronological sorting.

    Rules:
    * Screening / Baseline / Enrolment  -> -1 (before Day 1)
    * Day N                              -> N
    * Week N                             -> N * 7
    * Cycle C Day D                      -> (C - 1) * 21 + D  (assumes 21-day cycle)
    * Follow-up / EOS / EOT              -> very large number (sort last)

    Returns None if no rule matches so the visit keeps its original position.
    """
    low = name.strip().lower()

    if re.search(r'\bscreening\b|\bbaseline\b|\benrolment\b|\benrollment\b', low):
        return -1

    m = re.match(r'day\s*(\d+)', low)
    if m:
        return int(m.group(1))

    m = re.match(r'week\s*(\d+)', low)
    if m:
        return int(m.group(1)) * 7

    m = re.match(r'cycle\s*(\d+)\s*day\s*(\d+)', low)
    if m:
        cycle_num = int(m.group(1))
        day_num = int(m.group(2))
        return (cycle_num - 1) * 21 + day_num

    if re.search(r'\bfollow.?up\b|end\s+of\s+(?:study|treatment|trial)\b|\beos\b|\beot\b', low):
        return 99999

    return None


def expand_cycles(visits, max_cycles=6):
    expanded = []
    for visit in visits:
        name = visit.name.strip()
        low = name.lower()
        m = re.match(r'cycle\s*(\d+)\s*day\s*(\d+)', low)
        if m:
            cycle_num = int(m.group(1))
            v = _copy_visit(visit)
            v.cycle_group = f'C{cycle_num}'
            expanded.append(v)
            continue
        if 'cycle 2+' in low:
            for c in range(2, max_cycles + 1):
                v = _copy_visit(visit)
                v.name = f'Cycle {c}'
                v.cycle_group = f'C{c}'
                expanded.append(v)
            continue
        expanded.append(visit)
    return expanded
