from .ctms_mapper import map_to_ctms
from .condition_engine import apply_conditions
from .mappings import normalize
from .schemas import FinalRow


def build_operational_rows(visits):
    rows = []
    for v in visits:
        for p in v.procedures:
            normalized, orders = normalize(p.name)
            ctms = map_to_ctms(normalized)
            order = orders[0] if orders else None
            rows.append(FinalRow(
                visit=v.name,
                cycle=v.cycle_group,
                activity=p.name,
                normalized=normalized,
                order=order,
                form=ctms.get('form'),
                system=ctms.get('system'),
                condition=apply_conditions(p, v.cycle_group),
                source=p.source.parser_route,
            ))
    return rows
