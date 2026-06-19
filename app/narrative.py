import re
from .schemas import ProtocolExtraction, ExtractedVisit, ExtractedProcedure, SourceSpan

# ---------------------------------------------------------------------------
# Visit name fragment (reused in all patterns below)
# ---------------------------------------------------------------------------
_VISIT_FRAG = (
    r'(?P<visit>'
    r'Screening|Baseline'
    r'|Day\s*\d+'
    r'|Week\s*\d+'
    r'|Cycle\s*\d+\s*Day\s*\d+'
    r'|Follow-up|End\s+of\s+Treatment'
    r')'
)

# Procedures fragment: everything up to a sentence boundary or end-of-string.
# We allow commas, 'and', semicolons but stop at a period that is followed by
# whitespace/end (to avoid greedily consuming the next sentence).
_PROCS_FRAG = r'(?P<procedures>.+?)(?=\s*\.|$)'

# ---------------------------------------------------------------------------
# Pattern 1 (original): "<Visit> visit includes/consists of/…"
# ---------------------------------------------------------------------------
_P1 = re.compile(
    _VISIT_FRAG
    + r'\s+visit\s+(?:includes|consists\s+of|will\s+include|requires)\s+'
    + _PROCS_FRAG,
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# Pattern 2: "At <Visit>, <procedures> will be performed/collected/assessed/done"
# The procedures fragment ends at "will be <verb>" (consumed, not lookahead).
# ---------------------------------------------------------------------------
_P2 = re.compile(
    r'[Aa]t\s+' + _VISIT_FRAG + r',\s+'
    r'(?P<procedures>.+?)'
    r'\s+will\s+be\s+(?:performed|collected|assessed|done|obtained|measured|evaluated)',
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# Pattern 3: "Subjects/Patients will undergo <procedures> at <Visit>"
# ---------------------------------------------------------------------------
_P3 = re.compile(
    r'(?:Subjects?|Patients?)\s+will\s+undergo\s+'
    + r'(?P<procedures>.+?)'
    + r'\s+at\s+'
    + _VISIT_FRAG,
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# Pattern 4: "<Visit> visit will involve/comprise <procedures>"
# ---------------------------------------------------------------------------
_P4 = re.compile(
    _VISIT_FRAG
    + r'\s+visit\s+will\s+(?:involve|comprise|consist\s+of)\s+'
    + _PROCS_FRAG,
    re.IGNORECASE | re.DOTALL,
)

# Collect all patterns
_ALL_PATTERNS = [_P1, _P2, _P3, _P4]


def _split_items(text: str):
    """Split a comma/and/semicolon list into individual procedure strings."""
    # Normalise conjunctions and separators
    text = re.sub(r'\s+and\s+', ', ', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*;\s*', ', ', text)
    return [p.strip(' .;:') for p in text.split(',') if p.strip(' .;:')]


def _make_source(snippet: str) -> SourceSpan:
    return SourceSpan(
        section='Narrative procedures',
        snippet=snippet[:300],
        parser_route='narrative',
        confidence=0.80,
    )


def parse_narrative(text: str) -> ProtocolExtraction:
    """Extract visit–procedure pairs from free-text protocol narrative.

    Returns a ProtocolExtraction whose source.parser_route is 'narrative'.
    Duplicate (visit_name, procedure_name) pairs that happen to be matched by
    more than one pattern are collapsed to a single ExtractedProcedure.
    """
    # Use an ordered dict so that insertion order == textual order.
    visits: dict = {}  # visit_name -> ExtractedVisit

    for pattern in _ALL_PATTERNS:
        for m in pattern.finditer(text):
            visit_name = ' '.join(m.group('visit').split())
            procedures_text = m.group('procedures')

            if visit_name not in visits:
                visits[visit_name] = ExtractedVisit(name=visit_name, procedures=[])

            existing_names = {p.name.lower() for p in visits[visit_name].procedures}

            for item in _split_items(procedures_text):
                if item.lower() in existing_names:
                    continue  # de-duplicate across pattern matches
                visits[visit_name].procedures.append(
                    ExtractedProcedure(
                        name=item,
                        source=_make_source(m.group(0)),
                    )
                )
                existing_names.add(item.lower())

    return ProtocolExtraction(visits=list(visits.values()))
