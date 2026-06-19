import os
import uuid
import pandas as pd
from .schemas import FinalSchedule


def export_csv(schedule: FinalSchedule, output_dir='outputs'):
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f'protocol_rows_{uuid.uuid4().hex[:8]}.csv')
    pd.DataFrame([r.model_dump() for r in schedule.flat_rows]).to_csv(path, index=False)
    return path


def export_xlsx(schedule: FinalSchedule, output_dir='outputs'):
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f'protocol_review_{uuid.uuid4().hex[:8]}.xlsx')
    rows_df = pd.DataFrame([r.model_dump() for r in schedule.flat_rows])
    conflicts_df = pd.DataFrame([c.model_dump() for c in schedule.conflicts])
    visits_df = pd.DataFrame([
        {
            'visit_name': v.name,
            'cycle_group': v.cycle_group,
            'procedure_count': len(v.procedures),
        }
        for v in schedule.visits
    ])
    with pd.ExcelWriter(path, engine='openpyxl') as writer:
        visits_df.to_excel(writer, sheet_name='visits', index=False)
        rows_df.to_excel(writer, sheet_name='operational_rows', index=False)
        conflicts_df.to_excel(writer, sheet_name='conflicts', index=False)
    return path
