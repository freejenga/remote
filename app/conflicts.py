from collections import defaultdict
from .schemas import ProtocolExtraction, ConflictIssue
from .mappings import normalize


def detect_conflicts(extraction: ProtocolExtraction):
    matrix = defaultdict(lambda: defaultdict(set))
    for visit in extraction.visits:
        for p in visit.procedures:
            norm_name, _ = normalize(p.name)
            matrix[visit.name][p.source.parser_route].add(norm_name)

    conflicts = []
    for visit, sources in matrix.items():
        soa = sources.get('soa', set())
        nar = sources.get('narrative', set())
        if soa and nar:
            for a in sorted(soa - nar):
                conflicts.append(ConflictIssue(
                    severity='warning',
                    code='MissingInNarrative',
                    message='Activity appears in SoA but not in narrative extraction.',
                    visit_name=visit,
                    activity_name=a,
                ))
            for a in sorted(nar - soa):
                conflicts.append(ConflictIssue(
                    severity='warning',
                    code='MissingInSoA',
                    message='Activity appears in narrative extraction but not in SoA.',
                    visit_name=visit,
                    activity_name=a,
                ))
    return conflicts
