import re
from .schemas import ProtocolExtraction, ExtractedVisit, ExtractedProcedure, SourceSpan

# Symbols that mean "activity is scheduled at this visit"
MARKERS = ['X', 'x', '✓', 'Yes', 'yes', '1', 'X*', '●', '√']

# Known single-character footnote/superscript markers (beyond '*')
_LETTER_FOOTNOTE = re.compile(r'^[a-zA-Z†‡§¶]$')

# All footnote symbols we recognise in cell values
FOOTNOTE_QUALIFIERS = {
    '*': 'conditional / footnote',
}


def _split_pipe_rows(text: str):
    return [line for line in text.splitlines() if '|' in line]


def _normalize_header_cell(cell: str) -> str:
    return ' '.join(cell.strip().split())


def _split_row(line: str):
    """Split a pipe-delimited row, preserving interior (possibly empty) cells.

    Only the empty strings produced by the leading/trailing border pipes are
    dropped. Interior empty cells are kept so that column-to-visit alignment is
    preserved (an empty cell means 'activity not scheduled at this visit').
    """
    parts = line.split('|')
    if parts and parts[0].strip() == '':
        parts = parts[1:]
    if parts and parts[-1].strip() == '':
        parts = parts[:-1]
    return [_normalize_header_cell(c) for c in parts]


def _is_separator_row(cells) -> bool:
    """Return True for markdown alignment separator rows (---|:---:|---:, etc.)."""
    if not cells:
        return False
    joined = ''.join(c.strip() for c in cells)
    if not joined:
        return False
    # A separator row consists only of dashes, colons, equals signs, and spaces
    return bool(re.fullmatch(r'[-=: |]+', ''.join(cells)))


def _extract_footnotes(protocol_text: str) -> dict:
    """Parse a Footnotes section into a symbol -> description mapping.

    Handles:
    * Asterisk lines: ``* Some note``
    * Lettered lines: ``a Some note`` or ``a) Some note`` or ``(a) Some note``
    * Dagger/special markers: ``† Some note``
    """
    notes: dict = {}

    # Look for a dedicated Footnotes section first
    footnote_section_match = re.search(
        r'(?:Footnotes?|Notes?)\s*:\s*\n(.*?)(?:\n\n|\Z)',
        protocol_text,
        re.IGNORECASE | re.DOTALL,
    )
    if footnote_section_match:
        section_text = footnote_section_match.group(1)
    else:
        section_text = protocol_text

    for line in section_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Asterisk: "* text" or "** text"
        m = re.match(r'^(\*+)\s+(.*)', stripped)
        if m:
            notes[m.group(1)] = m.group(2).strip()
            continue
        # Dagger / other unicode markers: "† text" or "‡ text"
        m = re.match(r'^([†‡§¶])\s+(.*)', stripped)
        if m:
            notes[m.group(1)] = m.group(2).strip()
            continue
        # Letter markers: "a text", "a) text", "(a) text", "a. text"
        m = re.match(r'^\(?([a-z])\)?\.\s+(.*)', stripped, re.IGNORECASE)
        if m:
            notes[m.group(1).lower()] = m.group(2).strip()
            continue
        # Bare letter at start: "a Some note"
        m = re.match(r'^([a-z])\s{2,}(.*)', stripped, re.IGNORECASE)
        if m:
            notes[m.group(1).lower()] = m.group(2).strip()
            continue

    return notes


def _extract_cell_footnote_symbols(cell: str) -> list:
    """Return all footnote symbols (*, a, b, †, etc.) found in a cell value."""
    symbols = []
    # Find asterisks (one or more)
    for m in re.finditer(r'\*+', cell):
        symbols.append(m.group(0))
    # Find letter superscripts that appear after a mark symbol
    for m in re.finditer(r'[a-z†‡§¶]', cell):
        symbols.append(m.group(0))
    return symbols


def _is_marked(cell: str) -> bool:
    """Return True if the cell value indicates the procedure is scheduled."""
    if not cell:
        return False
    # Direct equality check first (fastest path)
    if cell in MARKERS:
        return True
    # Check if any marker is a substring of the cell
    # (handles "X*", "Xa", "yes", "●", etc.)
    cell_lower = cell.lower()
    for m in MARKERS:
        if m.lower() in cell_lower:
            return True
    return False


def parse_soa(protocol_text: str) -> ProtocolExtraction:
    lines = _split_pipe_rows(protocol_text)
    if len(lines) < 2:
        return ProtocolExtraction()

    header = _split_row(lines[0])
    if len(header) < 2:
        return ProtocolExtraction()

    visit_names = header[1:]
    visits = {h: ExtractedVisit(name=h) for h in visit_names}
    footnotes = _extract_footnotes(protocol_text)

    for line in lines[1:]:
        cells = _split_row(line)
        if len(cells) < 2 or _is_separator_row(cells):
            continue
        proc = cells[0]
        if not proc:
            continue
        for i, visit_name in enumerate(visit_names):
            cell = cells[i + 1] if i + 1 < len(cells) else ''
            if not _is_marked(cell):
                continue
            # Collect any footnote/conditional qualifiers from the cell
            footnote_symbols = _extract_cell_footnote_symbols(cell)
            conditional = None
            for sym in footnote_symbols:
                desc = footnotes.get(sym) or footnotes.get(sym.lower()) or FOOTNOTE_QUALIFIERS.get(sym)
                if desc:
                    conditional = desc
                    break
            visits[visit_name].procedures.append(
                ExtractedProcedure(
                    name=proc,
                    conditional=conditional,
                    source=SourceSpan(
                        section='Schedule of Activities',
                        snippet=line[:300],
                        parser_route='soa',
                        confidence=0.92,
                    ),
                )
            )
    return ProtocolExtraction(visits=list(visits.values()))
