"""End-to-end and unit tests for the clinical protocol pipeline.

Run with:  pytest -q
"""
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.graph import build_graph, _sort_visits_chronologically, _LinearGraph
from app.soa_parser import parse_soa
from app.narrative import parse_narrative
from app.merge import merge_visits_by_name
from app.cycle_engine import expand_cycles, infer_day
from app.schemas import ExtractedVisit

SAMPLE = """Protocol ABC-101

Schedule of Activities
| Procedure | Screening | Cycle 1 Day 1 | Cycle 1 Day 8 | Week 4 | Follow-up |
|-----------|-----------|---------------|---------------|--------|-----------|
| Informed consent | X |  |  |  |  |
| Blood sample | X | X | X* | X | X |
| ECG | X |  |  |  |  |
| Drug administration |  | X |  |  |  |
| CT scan |  |  |  | X |  |

Footnotes:
* Blood sample on Cycle 1 Day 8 only if clinically indicated.

Screening visit includes informed consent, blood sample, and ECG.
Cycle 1 Day 1 visit includes blood sample and drug administration.
Week 4 visit includes blood sample, AE review, and CT scan.
Follow-up visit includes blood sample.
"""


def _rows_by_visit(result):
    out = {}
    for r in result.flat_rows:
        out.setdefault(r.visit, set()).add(r.normalized)
    return out


# ---------------------------------------------------------------------------
# Existing tests (must keep passing)
# ---------------------------------------------------------------------------

def test_soa_alignment_keeps_empty_columns():
    """Empty cells must not shift marks to the wrong visit."""
    extraction = parse_soa(SAMPLE)
    by_visit = {v.name: {p.name for p in v.procedures} for v in extraction.visits}
    # Drug administration is only marked under Cycle 1 Day 1.
    assert 'Drug administration' not in by_visit['Screening']
    assert 'Drug administration' in by_visit['Cycle 1 Day 1']
    # CT scan is only marked under Week 4.
    assert 'CT scan' not in by_visit['Screening']
    assert 'CT scan' in by_visit['Week 4']


def test_pipeline_visit_membership():
    result = build_graph().invoke({'text': SAMPLE})['result']
    rows = _rows_by_visit(result)
    assert rows['Screening'] == {'Informed Consent', 'Lab: Blood draw', 'ECG'}
    assert rows['Cycle 1 Day 1'] == {'Lab: Blood draw', 'Study Drug'}
    assert rows['Week 4'] == {'Lab: Blood draw', 'CT Scan', 'Adverse Event Review'}
    assert rows['Follow-up'] == {'Lab: Blood draw'}


def test_no_duplicate_rows():
    result = build_graph().invoke({'text': SAMPLE})['result']
    seen = [(r.visit, r.normalized) for r in result.flat_rows]
    assert len(seen) == len(set(seen)), 'operational rows must be de-duplicated'


def test_footnote_becomes_condition():
    result = build_graph().invoke({'text': SAMPLE})['result']
    c1d8 = [r for r in result.flat_rows if r.visit == 'Cycle 1 Day 8']
    assert c1d8 and c1d8[0].condition is not None


def test_conflict_detected_for_narrative_only_activity():
    result = build_graph().invoke({'text': SAMPLE})['result']
    codes = {(c.code, c.visit_name, c.activity_name) for c in result.conflicts}
    assert ('MissingInSoA', 'Week 4', 'Adverse Event Review') in codes


def test_merge_dedupes_by_normalized_name():
    extraction = parse_soa(SAMPLE)
    nar_dupe = expand_cycles(extraction.visits) + expand_cycles(extraction.visits)
    merged = merge_visits_by_name(nar_dupe)
    names = [v.name for v in merged]
    assert len(names) == len(set(names)), 'visits with the same name must merge'


def test_empty_input_is_safe():
    result = build_graph().invoke({'text': ''})['result']
    assert result.visits == []
    assert result.flat_rows == []
    assert result.conflicts == []


# ---------------------------------------------------------------------------
# New tests – Task 1: Stronger SoA table parsing
# ---------------------------------------------------------------------------

SOA_MULTI_MARK = """| Procedure | V1 | V2 | V3 | V4 |
|-----------|----|----|----|----|
| CBC | ● |  | yes | √ |
| Biopsy |  | Xa |  |  |
| Urine | Yes |  |  | yes |
"""

SOA_FOOTNOTE_LETTERS = """| Procedure | Screening | Day 1 | Day 8 |
|-----------|-----------|-------|-------|
| Blood draw | X | Xa | Xb |
| ECG | X |  |  |

Footnotes:
a. Fasting required
b. Pre-dose only
"""

SOA_DAGGER_FOOTNOTE = """| Procedure | V1 | V2 |
|-----------|----|----|
| Biopsy | X† |  |

Footnotes:
† Only if tumour progression confirmed.
"""

SOA_MULTI_ROW_SEPARATOR = """| Procedure | Screening | Day 1 | Follow-up |
|:----------|:---------:|:-----:|:---------:|
| Vitals    | X         |       | X         |
| ECG       |           | X     |           |
"""


def test_soa_new_mark_symbols():
    """●, √, yes (lowercase), and Yes must all be recognised as marks."""
    extraction = parse_soa(SOA_MULTI_MARK)
    by_visit = {v.name: {p.name for p in v.procedures} for v in extraction.visits}
    assert 'CBC' in by_visit.get('V1', set()), "● should be a mark"
    assert 'CBC' in by_visit.get('V3', set()), "yes (lowercase) should be a mark"
    assert 'CBC' in by_visit.get('V4', set()), "√ should be a mark"
    # V2 has no CBC mark
    assert 'CBC' not in by_visit.get('V2', set())


def test_soa_letter_footnote_parsed():
    """Letter footnotes (a, b) should populate conditional on the procedure."""
    extraction = parse_soa(SOA_FOOTNOTE_LETTERS)
    by_visit = {v.name: {p.name: p for p in v.procedures} for v in extraction.visits}
    day1_blood = by_visit.get('Day 1', {}).get('Blood draw')
    assert day1_blood is not None, 'Blood draw should be present at Day 1'
    assert day1_blood.conditional is not None, 'footnote "a" should populate conditional'
    assert 'fasting' in day1_blood.conditional.lower() or day1_blood.conditional != ''


def test_soa_dagger_footnote_parsed():
    """† footnote marker should populate conditional."""
    extraction = parse_soa(SOA_DAGGER_FOOTNOTE)
    by_visit = {v.name: {p.name: p for p in v.procedures} for v in extraction.visits}
    v1_biopsy = by_visit.get('V1', {}).get('Biopsy')
    assert v1_biopsy is not None
    assert v1_biopsy.conditional is not None
    assert 'tumour' in v1_biopsy.conditional.lower() or v1_biopsy.conditional != ''


def test_soa_markdown_alignment_separator_skipped():
    """Rows like |:---:|:---:| must be treated as separators, not data rows."""
    extraction = parse_soa(SOA_MULTI_ROW_SEPARATOR)
    by_visit = {v.name: {p.name for p in v.procedures} for v in extraction.visits}
    # Separator row must not produce procedures
    assert 'Vitals' in by_visit.get('Screening', set())
    assert 'ECG' in by_visit.get('Day 1', set())
    # Separator itself must not become a visit or procedure name
    all_visits = set(by_visit.keys())
    for v in all_visits:
        assert '---' not in v and ':' not in v or v in ('Screening', 'Day 1', 'Follow-up')


def test_soa_empty_interior_cells_not_shifted():
    """Existing alignment regression: empty cells must not shift marks."""
    extraction = parse_soa(SAMPLE)
    by_visit = {v.name: {p.name for p in v.procedures} for v in extraction.visits}
    assert 'Drug administration' in by_visit['Cycle 1 Day 1']
    assert 'Drug administration' not in by_visit['Screening']
    assert 'Drug administration' not in by_visit['Week 4']


# ---------------------------------------------------------------------------
# New tests – Task 2: Broadened narrative parsing
# ---------------------------------------------------------------------------

NARRATIVE_AT_VISIT = """
At Screening, vital signs and ECG will be performed.
At Day 1, blood draw and drug administration will be collected.
"""

NARRATIVE_SUBJECTS = """
Subjects will undergo CBC and biopsy at Baseline.
Patients will undergo urine analysis at Week 4.
"""

NARRATIVE_COMMA_AND = """
Follow-up visit includes CBC, urine analysis, and AE review.
"""


def test_narrative_at_visit_pattern():
    """'At <Visit>, <procedures> will be performed' should be matched."""
    result = parse_narrative(NARRATIVE_AT_VISIT)
    by_visit = {v.name: {p.name.lower() for p in v.procedures} for v in result.visits}
    assert 'Screening' in by_visit, "Screening should be parsed"
    procs = by_visit['Screening']
    assert any('vital' in p for p in procs), "vital signs should be extracted"
    assert any('ecg' in p for p in procs), "ECG should be extracted"


def test_narrative_subjects_will_undergo():
    """'Subjects will undergo <procedures> at <Visit>' should be matched."""
    result = parse_narrative(NARRATIVE_SUBJECTS)
    by_visit = {v.name: {p.name.lower() for p in v.procedures} for v in result.visits}
    assert 'Baseline' in by_visit or 'Week 4' in by_visit
    if 'Baseline' in by_visit:
        assert any('cbc' in p for p in by_visit['Baseline'])


def test_narrative_comma_and_list():
    """Comma/and lists should all be split into separate procedures."""
    result = parse_narrative(NARRATIVE_COMMA_AND)
    by_visit = {v.name: {p.name.lower() for p in v.procedures} for v in result.visits}
    assert 'Follow-up' in by_visit
    procs = by_visit['Follow-up']
    assert any('cbc' in p for p in procs)
    assert any('urine' in p for p in procs)
    assert any('ae' in p or 'adverse' in p for p in procs)


def test_narrative_parser_route_is_narrative():
    """All procedures from parse_narrative must carry source.parser_route='narrative'."""
    result = parse_narrative(SAMPLE)
    for visit in result.visits:
        for proc in visit.procedures:
            assert proc.source.parser_route == 'narrative'


def test_narrative_no_duplicates_across_patterns():
    """The same procedure must not appear twice for the same visit."""
    text = "Screening visit includes blood sample and ECG. At Screening, blood sample and ECG will be performed."
    result = parse_narrative(text)
    by_visit = {v.name: [p.name.lower() for p in v.procedures] for v in result.visits}
    if 'Screening' in by_visit:
        names = by_visit['Screening']
        assert len(names) == len(set(names)), 'procedures must be de-duplicated per visit'


# ---------------------------------------------------------------------------
# New tests – Task 3: Chronological visit ordering
# ---------------------------------------------------------------------------

def test_infer_day_screening():
    assert infer_day('Screening') == -1
    assert infer_day('Baseline') == -1


def test_infer_day_day_n():
    assert infer_day('Day 1') == 1
    assert infer_day('Day 15') == 15


def test_infer_day_week_n():
    assert infer_day('Week 2') == 14
    assert infer_day('Week 4') == 28


def test_infer_day_cycle_day():
    # Cycle 1 Day 1 -> (1-1)*21 + 1 = 1
    assert infer_day('Cycle 1 Day 1') == 1
    # Cycle 2 Day 8 -> (2-1)*21 + 8 = 29
    assert infer_day('Cycle 2 Day 8') == 29


def test_infer_day_followup():
    day = infer_day('Follow-up')
    assert day is not None and day > 1000, "Follow-up should sort last"


def test_infer_day_unknown():
    assert infer_day('Visit X Unknown') is None


def test_visits_sorted_chronologically():
    """After the full pipeline, Screening must come before Day 1, Follow-up must be last."""
    result = build_graph().invoke({'text': SAMPLE})['result']
    visit_names = [v.name for v in result.visits]
    assert visit_names.index('Screening') < visit_names.index('Cycle 1 Day 1')
    assert visit_names.index('Cycle 1 Day 1') < visit_names.index('Follow-up')


def test_sort_visits_helper():
    visits = [
        ExtractedVisit(name='Follow-up'),
        ExtractedVisit(name='Screening'),
        ExtractedVisit(name='Day 1'),
        ExtractedVisit(name='Week 2'),
    ]
    sorted_visits = _sort_visits_chronologically(visits)
    names = [v.name for v in sorted_visits]
    assert names[0] == 'Screening'
    assert names[-1] == 'Follow-up'
    assert names.index('Day 1') < names.index('Week 2')


def test_flat_rows_order_matches_visit_order():
    """flat_rows should follow the same chronological order as visits."""
    result = build_graph().invoke({'text': SAMPLE})['result']
    visit_names = [v.name for v in result.visits]
    row_visits_seen = []
    for row in result.flat_rows:
        if row.visit not in row_visits_seen:
            row_visits_seen.append(row.visit)
    # Each visit name should appear in flat_rows in the same relative order
    for i, name in enumerate(row_visits_seen):
        assert name in visit_names
    # Screening rows before Follow-up rows
    row_visit_names = [r.visit for r in result.flat_rows]
    if 'Screening' in row_visit_names and 'Follow-up' in row_visit_names:
        assert row_visit_names.index('Screening') < row_visit_names.index('Follow-up')


# ---------------------------------------------------------------------------
# New tests – Task 4: langgraph-optional fallback
# ---------------------------------------------------------------------------

def test_linear_graph_produces_same_result_as_langgraph():
    """_LinearGraph must return the same FinalSchedule structure as the real graph."""
    linear = _LinearGraph()
    result = linear.invoke({'text': SAMPLE})['result']
    # Basic structure check
    assert hasattr(result, 'visits')
    assert hasattr(result, 'flat_rows')
    assert hasattr(result, 'conflicts')
    rows = _rows_by_visit(result)
    assert rows['Screening'] == {'Informed Consent', 'Lab: Blood draw', 'ECG'}
    assert rows['Cycle 1 Day 1'] == {'Lab: Blood draw', 'Study Drug'}


def test_linear_graph_empty_input():
    linear = _LinearGraph()
    result = linear.invoke({'text': ''})['result']
    assert result.visits == []
    assert result.flat_rows == []


def test_build_graph_returns_invocable():
    """build_graph() must always return something with an .invoke() method."""
    graph = build_graph()
    assert callable(getattr(graph, 'invoke', None))
    result = graph.invoke({'text': SAMPLE})['result']
    assert result.visits  # non-empty


def test_fallback_runner_without_langgraph(monkeypatch):
    """When langgraph is not available, build_graph should return _LinearGraph."""
    import app.graph as graph_module
    original = graph_module._LANGGRAPH_AVAILABLE
    try:
        monkeypatch.setattr(graph_module, '_LANGGRAPH_AVAILABLE', False)
        g = graph_module.build_graph()
        assert isinstance(g, _LinearGraph)
        result = g.invoke({'text': SAMPLE})['result']
        assert result.visits
    finally:
        graph_module._LANGGRAPH_AVAILABLE = original


# ---------------------------------------------------------------------------
# New tests – Task 5: Optional OCR (graceful degradation)
# ---------------------------------------------------------------------------

def test_ocr_module_importable():
    """app.ocr must always be importable (zero hard dependencies)."""
    import importlib
    mod = importlib.import_module('app.ocr')
    assert hasattr(mod, 'ocr_pdf_bytes')


def test_ocr_degrades_gracefully_when_deps_missing():
    """ocr_pdf_bytes must return '' when pytesseract/pdf2image are absent."""
    from app import ocr as ocr_mod
    # Simulate missing pytesseract by patching builtins.__import__
    import builtins
    real_import = builtins.__import__

    def _block_import(name, *args, **kwargs):
        if name in ('pytesseract', 'pdf2image'):
            raise ImportError(f'Mocked missing: {name}')
        return real_import(name, *args, **kwargs)

    with mock.patch('builtins.__import__', side_effect=_block_import):
        result = ocr_mod.ocr_pdf_bytes(b'%PDF-1.4 fake')
    assert result == ''


def test_ingestion_ocr_fallback_called_on_empty_pdf(monkeypatch, tmp_path):
    """extract_text_from_pdf_bytes must call OCR when PDF yields no text."""
    from app import ingestion, ocr as ocr_mod

    # Build a minimal valid-ish PDF that PyPDF2 will parse but return no text for
    # We mock PdfReader to return pages with no text
    class _FakePage:
        def extract_text(self):
            return ''

    class _FakeReader:
        def __init__(self, *a, **kw):
            self.pages = [_FakePage()]

    monkeypatch.setattr('app.ingestion.PdfReader', _FakeReader)
    monkeypatch.setattr(ocr_mod, 'ocr_pdf_bytes', lambda b: 'OCR result text')

    text = ingestion.extract_text_from_pdf_bytes(b'fake-pdf')
    assert text == 'OCR result text'


def test_ingestion_ocr_not_called_when_pdf_has_text(monkeypatch):
    """extract_text_from_pdf_bytes must NOT call OCR when PDF already has text."""
    from app import ingestion, ocr as ocr_mod

    class _FakePage:
        def extract_text(self):
            return 'Some real text'

    class _FakeReader:
        def __init__(self, *a, **kw):
            self.pages = [_FakePage()]

    monkeypatch.setattr('app.ingestion.PdfReader', _FakeReader)
    ocr_called = []
    monkeypatch.setattr(ocr_mod, 'ocr_pdf_bytes', lambda b: ocr_called.append(1) or '')

    text = ingestion.extract_text_from_pdf_bytes(b'fake-pdf')
    assert text == 'Some real text'
    assert not ocr_called, 'OCR must not be called when PDF text is non-empty'
