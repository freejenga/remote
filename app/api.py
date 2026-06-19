import os

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from .graph import build_graph
from .ingestion import ingest_uploaded_file
from .schemas import FinalSchedule
from .exporter import export_csv, export_xlsx
from .store import init_db
from . import compliance, dispatch, chat, auth, deid, audit, rbac, studies, docstore, assembler, scheduling

app = FastAPI(title='Clinical Research Platform', version='2.0.0')
graph = build_graph()

# Initialize the shared store and mount the incorporated modules.
init_db()
rbac.init_rbac()
# Data routers are gated by the access token (no-op until PLATFORM_AUTH_TOKEN set).
# require_auth also accepts a valid RBAC session, so logged-in users pass too.
_gate = [Depends(auth.require_auth)]
app.include_router(auth.router)
app.include_router(rbac.router)
app.include_router(compliance.router, dependencies=_gate)
app.include_router(dispatch.router, dependencies=_gate)
app.include_router(studies.router, dependencies=_gate)
app.include_router(scheduling.router, dependencies=_gate)
app.include_router(chat.router, dependencies=_gate)
app.include_router(audit.router, dependencies=_gate)

# Serve the original module UIs (compliance + Serengeti dispatch) as static
# pages when a ``ui`` directory is present next to the app package.
_UI_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'ui')
if os.path.isdir(_UI_DIR):
    app.mount('/ui', StaticFiles(directory=_UI_DIR, html=True), name='ui')


@app.get('/health')
def health():
    return {'status': 'ok'}


@app.get('/')
def index():
    return {
        'platform': 'Clinical Research Platform',
        'modules': {
            'protocol': ['/parse-text', '/parse-file', '/export-file'],
            'studies': '/studies',
            'scheduling': '/scheduling',
            'compliance': '/compliance',
            'dispatch': '/dispatch',
            'chat': '/chat',
            'documents': '/documents',
            'source_documents': '/source-documents',
        },
        'docs': '/docs',
        'ui': '/ui',
        'security': {
            'auth_required': auth.auth_enabled(),
            'deidentification': deid.enabled(),
        },
    }


@app.post('/parse-text', dependencies=_gate)
def parse_text(protocol_text: str = Form(...)):
    schedule = graph.invoke({'text': protocol_text})['result']
    return JSONResponse(schedule.model_dump())


@app.post('/parse-file', dependencies=_gate)
async def parse_file(file: UploadFile = File(...)):
    try:
        text, source_type = ingest_uploaded_file(file.filename, await file.read())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    schedule = graph.invoke({'text': text})['result']
    payload = schedule.model_dump()
    payload['filename'] = file.filename
    payload['source_type'] = source_type
    return JSONResponse(payload)


@app.post('/export-file', dependencies=_gate)
async def export_file(file: UploadFile = File(...), fmt: str = Form('xlsx')):
    try:
        text, _ = ingest_uploaded_file(file.filename, await file.read())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    schedule: FinalSchedule = graph.invoke({'text': text})['result']
    if fmt.lower() == 'csv':
        path = export_csv(schedule)
        media = 'text/csv'
    else:
        path = export_xlsx(schedule)
        media = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    audit.record('protocol.export', {'filename': file.filename, 'fmt': fmt})
    return FileResponse(path=path, filename=os.path.basename(path), media_type=media)


def _export_schedule_file(schedule: FinalSchedule, fmt: str):
    """Write a parsed schedule to CSV/XLSX and return a FileResponse. Shared
    by /export-schedule and the studies version export."""
    if fmt.lower() == 'csv':
        path = export_csv(schedule)
        media = 'text/csv'
    else:
        path = export_xlsx(schedule)
        media = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    return FileResponse(path=path, filename=os.path.basename(path), media_type=media)


@app.post('/export-schedule', dependencies=_gate)
def export_schedule(schedule: FinalSchedule, fmt: str = 'xlsx'):
    """Export an already-parsed schedule (no re-upload / re-parse needed)."""
    audit.record('protocol.export_schedule',
                 {'fmt': fmt, 'rows': len(schedule.flat_rows)})
    return _export_schedule_file(schedule, fmt)


# --- Document knowledge base (RAG) -----------------------------------------
@app.post('/documents', dependencies=_gate)
async def upload_document(file: UploadFile = File(...),
                          study_id: str = Form(None),
                          title: str = Form(None)):
    """Add a document (PDF/DOCX/TXT/…) to the searchable knowledge base.

    The file is text-extracted, de-identified, chunked, and indexed for the AI
    chat's ``search_documents`` tool. Returns the document id + chunk count.
    """
    try:
        text, source_type = ingest_uploaded_file(file.filename, await file.read())
        result = docstore.add_document(
            title or file.filename, text,
            study_id=study_id, source_type=source_type)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record('documents.add',
                 {'doc_id': result['id'], 'chunks': result['chunk_count'],
                  'source_type': source_type, 'study_id': study_id})
    return JSONResponse(result)


@app.get('/documents', dependencies=_gate)
def list_documents(study_id: str = None):
    """List documents in the knowledge base, optionally scoped to a study."""
    return {'documents': docstore.list_documents(study_id)}


@app.get('/documents/search', dependencies=_gate)
def search_documents(q: str, study_id: str = None, limit: int = 5):
    """Retrieve the most relevant document passages for a query."""
    return {'query': q, 'results': docstore.search(q, study_id=study_id, limit=limit)}


@app.delete('/documents/{doc_id}', dependencies=_gate)
def delete_document(doc_id: str):
    """Delete a document and its chunks from the knowledge base."""
    if not docstore.delete_document(doc_id):
        raise HTTPException(status_code=404, detail='document not found')
    audit.record('documents.delete', {'doc_id': doc_id})
    return {'deleted': True, 'id': doc_id}


# --- Source-document assembly (deterministic) ------------------------------
@app.post('/source-documents', dependencies=_gate)
def source_documents(schedule: FinalSchedule, subject_id: str = None,
                     enrollment_date: str = None, reference_hints: bool = False,
                     study_id: str = None, render: bool = False):
    """Assemble per-visit source documents from an already-parsed schedule.

    Deterministic: same schedule + anchor always yields the same forms. Set
    ``render=true`` to also get a fillable plain-text packet; ``reference_hints``
    attaches retrieval excerpts from the uploaded-document knowledge base.
    """
    try:
        result = assembler.build_source_documents(
            schedule, subject_id=subject_id, enrollment_date=enrollment_date,
            include_reference_hints=reference_hints, study_id=study_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if render:
        result['rendered'] = assembler.render_packet(result)
    audit.record('source_documents.assemble',
                 {'subject_id': subject_id, 'visits': result['document_count'],
                  'reference_hints': reference_hints})
    return JSONResponse(result)
