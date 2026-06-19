from typing import List, Optional, Literal
from pydantic import BaseModel, Field

SourceType = Literal['soa', 'narrative', 'llm']

class SourceSpan(BaseModel):
    section: Optional[str] = None
    snippet: str
    parser_route: SourceType
    confidence: float = 0.8

class ExtractedProcedure(BaseModel):
    name: str
    timing: Optional[str] = None
    conditional: Optional[str] = None
    source: SourceSpan

class ExtractedVisit(BaseModel):
    name: str
    day: Optional[int] = None
    window: Optional[str] = None
    cycle_group: Optional[str] = None
    procedures: List[ExtractedProcedure] = Field(default_factory=list)

class ProtocolExtraction(BaseModel):
    study_name: Optional[str] = None
    visits: List[ExtractedVisit] = Field(default_factory=list)

class ConflictIssue(BaseModel):
    severity: Literal['info', 'warning', 'error']
    code: str
    message: str
    visit_name: Optional[str] = None
    activity_name: Optional[str] = None

class FinalRow(BaseModel):
    visit: str
    cycle: Optional[str] = None
    activity: str
    normalized: str
    order: Optional[str] = None
    form: Optional[str] = None
    system: Optional[str] = None
    condition: Optional[str] = None
    source: Optional[str] = None

class FinalSchedule(BaseModel):
    visits: List[ExtractedVisit] = Field(default_factory=list)
    flat_rows: List[FinalRow] = Field(default_factory=list)
    conflicts: List[ConflictIssue] = Field(default_factory=list)
