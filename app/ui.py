import os

import streamlit as st
import pandas as pd

from app.graph import build_graph
from app.ingestion import ingest_uploaded_file
from app.chat import run_chat, ChatNotConfigured, ChatProviderError
from app.store import init_db
from app import docstore, assembler, sourcedoc_agents, scheduling, billing, studies

st.set_page_config(layout='wide', page_title='Clinical Research Platform')
st.title('Clinical Research Platform')

init_db()


def _require_login():
    """Gate the page with the shared access token when one is configured."""
    token = os.environ.get('PLATFORM_AUTH_TOKEN')
    if not token:
        return  # auth disabled
    if st.session_state.get('authed'):
        return
    st.info('This platform is access-protected. Enter the access token to continue.')
    entered = st.text_input('Access token', type='password')
    if entered:
        if entered == token:
            st.session_state.authed = True
            st.rerun()
        else:
            st.error('Invalid token.')
    st.stop()


_require_login()

st.session_state.setdefault('schedule', None)
st.session_state.setdefault('protocol_text', None)
st.session_state.setdefault('chat_history', [])

graph = build_graph()


def _protocol_context(schedule):
    """Build a compact text summary of the parsed protocol for the AI chat."""
    if schedule is None:
        return None
    lines = []
    for v in schedule.visits:
        acts = ', '.join(p.name for p in v.procedures) or '(none)'
        cg = f' [{v.cycle_group}]' if v.cycle_group else ''
        lines.append(f'- {v.name}{cg}: {acts}')
    conflicts = [f'- {c.code} @ {c.visit_name}: {c.activity_name}'
                 for c in schedule.conflicts]
    return (
        'Visits and activities:\n' + '\n'.join(lines)
        + '\nConflicts:\n' + ('\n'.join(conflicts) if conflicts else '- none')
        + f'\nOperational rows: {len(schedule.flat_rows)}'
    )


tab_protocol, tab_docs, tab_src, tab_sched = st.tabs(
    ['📋 Protocol & Chat', '📚 Documents (RAG)', '📝 Source Documents',
     '🗓️ Scheduling & Billing'])


# ===========================================================================
# Tab 1 — Protocol parsing, review, export, and the embedded AI assistant
# ===========================================================================
with tab_protocol:
    uploaded_file = st.file_uploader(
        'Upload protocol (.txt, .pdf, .docx)', type=['txt', 'pdf', 'docx'],
        key='protocol_uploader')

    if uploaded_file is not None:
        try:
            text, source_type = ingest_uploaded_file(uploaded_file.name,
                                                     uploaded_file.read())
            with st.spinner('Parsing protocol...'):
                schedule = graph.invoke({'text': text})['result']
            st.session_state.schedule = schedule
            st.session_state.protocol_text = text
            st.success(f'Parsed {uploaded_file.name} ({source_type})')
        except Exception as e:
            st.error(f'Could not parse file: {e}')

    schedule = st.session_state.schedule

    if schedule is not None:
        st.header('Visits & Activities')
        for idx, v in enumerate(schedule.visits):
            rows = [{'Activity': p.name, 'Conditional': p.conditional,
                     'Source': p.source.parser_route} for p in v.procedures]
            with st.expander(v.name, expanded=(idx == 0)):
                st.data_editor(pd.DataFrame(rows), num_rows='dynamic',
                               key=f'visit_{idx}')

        st.header('Conflicts')
        if schedule.conflicts:
            st.dataframe(pd.DataFrame([c.model_dump() for c in schedule.conflicts]),
                         use_container_width=True)
        else:
            st.success('No conflicts detected.')

        st.header('Operational Rows (CTMS-ready)')
        rows_df = pd.DataFrame([r.model_dump() for r in schedule.flat_rows])
        st.dataframe(rows_df, use_container_width=True)
        st.download_button('Download CTMS-ready CSV',
                           rows_df.to_csv(index=False).encode('utf-8'),
                           'protocol_schedule.csv', 'text/csv')
    else:
        st.info('Upload a protocol file to begin.')

    st.divider()
    st.header('🤖 Ask the AI about this protocol')
    if schedule is None:
        st.caption('Upload a protocol above, then ask about its visits, '
                   'activities, and conflicts. The assistant can also look up '
                   'subjects, compliance, transport, and uploaded documents.')
    else:
        st.caption('The assistant can see the parsed protocol above and can '
                   'search uploaded documents and look up subjects/compliance.')

    for _m in st.session_state.chat_history:
        with st.chat_message(_m['role']):
            st.markdown(_m['content'])

    _prompt = st.chat_input('Ask about visits, conflicts, a subject, documents…')
    if _prompt:
        st.session_state.chat_history.append({'role': 'user', 'content': _prompt})
        with st.chat_message('user'):
            st.markdown(_prompt)
        with st.chat_message('assistant'):
            try:
                with st.spinner('Thinking…'):
                    _result = run_chat(
                        st.session_state.chat_history,
                        protocol_context=_protocol_context(schedule))
                _reply = _result['reply'] or '(no reply)'
                st.markdown(_reply)
                if _result['tool_calls']:
                    st.caption('used: ' + ', '.join(
                        t['name'] for t in _result['tool_calls']))
                st.session_state.chat_history.append(
                    {'role': 'assistant', 'content': _reply})
            except ChatNotConfigured as _e:
                st.warning(str(_e))
            except ChatProviderError as _e:
                st.error(str(_e))


# ===========================================================================
# Tab 2 — Document knowledge base (RAG): upload + semantic search
# ===========================================================================
with tab_docs:
    st.header('Document knowledge base')
    st.caption('Upload trial documents (protocols, SOPs, memos). They are '
               'de-identified, chunked, and indexed for retrieval — and become '
               'searchable by the AI assistant. Set PLATFORM_EMBEDDINGS=1 for '
               'hybrid semantic search; otherwise TF-IDF is used.')

    doc_file = st.file_uploader('Add a document (.txt, .pdf, .docx, .xlsx, .csv)',
                                type=['txt', 'pdf', 'docx', 'xlsx', 'csv'],
                                key='doc_uploader')
    if doc_file is not None and st.button('Index document', key='index_doc'):
        try:
            text, source_type = ingest_uploaded_file(doc_file.name, doc_file.read())
            rec = docstore.add_document(doc_file.name, text, source_type=source_type)
            st.success(f"Indexed “{rec['title']}” into {rec['chunk_count']} chunks.")
        except Exception as e:
            st.error(f'Could not index document: {e}')

    st.subheader('Indexed documents')
    docs = docstore.list_documents()
    if docs:
        st.dataframe(pd.DataFrame(docs)[['title', 'source_type', 'chunk_count',
                                         'created_at']],
                     use_container_width=True)
    else:
        st.info('No documents indexed yet.')

    st.subheader('Search')
    q = st.text_input('Query (e.g. "inclusion criteria for diabetes")',
                      key='doc_query')
    if q:
        hits = docstore.search(q, limit=5)
        if not hits:
            st.warning('No matching passages.')
        for h in hits:
            sec = f" · {h['section']}" if h.get('section') else ''
            with st.expander(f"{h['title']}{sec} · score {h['score']}"):
                st.write(h['content'])


# ===========================================================================
# Tab 3 — Source documents: deterministic packet + optional AI generation
# ===========================================================================
with tab_src:
    st.header('Source documents')
    schedule = st.session_state.schedule
    if schedule is None:
        st.info('Parse a protocol in the Protocol tab first.')
    else:
        col1, col2 = st.columns(2)
        subject_id = col1.text_input('Subject ID (optional)', key='src_subject')
        enrollment = col2.text_input('Enrollment date (YYYY-MM-DD, optional)',
                                     key='src_enroll')

        st.subheader('Deterministic packet')
        st.caption('Built entirely by code from the parsed schedule — no AI, '
                   'byte-identical for the same inputs.')
        try:
            assembled = assembler.build_source_documents(
                schedule, subject_id=subject_id or None,
                enrollment_date=enrollment or None)
            st.write(f"{assembled['document_count']} visit form(s).")
            packet = assembler.render_packet(assembled)
            st.download_button('Download fillable packet (.txt)',
                               packet.encode('utf-8'), 'source_documents.txt',
                               'text/plain')
            with st.expander('Preview packet'):
                st.text(packet)
        except ValueError as e:
            st.error(str(e))

        st.subheader('AI-generated + critic-verified')
        st.caption('Generation → formatting → critic loop, layered on the '
                   'deterministic skeleton. Requires ANTHROPIC_API_KEY.')
        if st.button('Generate & verify', key='gen_src'):
            try:
                with st.spinner('Running generation/critic pipeline…'):
                    out = sourcedoc_agents.generate_source_documents(
                        schedule, subject_id=subject_id or None,
                        enrollment_date=enrollment or None)
                st.success(f"{out['approved_count']}/{out['document_count']} "
                           'visit docs critic-approved.')
                for d in out['documents']:
                    badge = '✅' if d['approved'] else '⚠️'
                    with st.expander(f"{badge} {d['visit']} "
                                     f"(iterations: {d['iterations']})"):
                        st.json(d['generated'])
                        if d['critique'].get('issues'):
                            st.caption('Critic issues: '
                                       + '; '.join(d['critique']['issues']))
            except ValueError as e:
                st.error(str(e))
            except sourcedoc_agents.SourceDocNotConfigured as e:
                st.warning(str(e))


# ===========================================================================
# Tab 4 — Scheduling & billing (study-based; deterministic)
# ===========================================================================
with tab_sched:
    st.header('Scheduling & billing')

    # Bridge: persist the current parsed protocol as a study so the
    # study-based scheduling/billing flows have data to work on.
    with st.expander('Save current parsed protocol as a study'):
        if st.session_state.protocol_text is None:
            st.caption('Parse a protocol in the Protocol tab first.')
        else:
            new_name = st.text_input('Study name', key='new_study_name')
            if st.button('Create study + version', key='create_study') and new_name:
                try:
                    s = studies.create_study(studies.StudyIn(name=new_name))
                    studies.create_version(
                        s['id'],
                        studies.VersionIn(label='v1',
                                          protocol_text=st.session_state.protocol_text))
                    st.success(f"Created study {s['name']} ({s['id']}).")
                except Exception as e:
                    st.error(f'Could not create study: {e}')

    study_list = studies.list_studies()['data']
    if not study_list:
        st.info('No studies yet. Create one from a parsed protocol above.')
    else:
        labels = {f"{s['name']} ({s['id']})": s['id'] for s in study_list}
        chosen = st.selectbox('Study', list(labels.keys()))
        sid = labels[chosen]
        today = st.date_input('“Today” for compliance').isoformat()

        st.subheader('1. Schedule of Events')
        if st.button('Materialize SoE from latest version', key='mat_soe'):
            res = scheduling.materialize_soe(sid)
            st.success(f"Materialized {res['event_count']} SoE rows.")
        soe = scheduling.get_soe(sid)['events'] if True else []
        if soe:
            st.dataframe(pd.DataFrame(soe), use_container_width=True, height=200)

        st.subheader('2. Subject visit calendar')
        c1, c2 = st.columns(2)
        subj = c1.text_input('Subject ID', key='sched_subject')
        enroll = c2.text_input('Enrollment date (YYYY-MM-DD)', key='sched_enroll')
        if st.button('Generate calendar', key='gen_cal') and subj and enroll:
            try:
                r = scheduling.generate_calendar(subj, sid, enroll)
                st.success(f"Generated {r['visit_count']} visits for {subj}.")
            except ValueError as e:
                st.error(str(e))

        st.subheader('3. Compliance & next due')
        report = scheduling.compliance_report(study_id=sid, today=today)
        if report['visits']:
            st.write('Summary:', report['summary'])
            st.dataframe(pd.DataFrame(report['visits'])[
                ['subject_id', 'visit_name', 'target_date', 'earliest_date',
                 'latest_date', 'actual_date', 'status']],
                use_container_width=True)
            nd = scheduling.next_due_report(study_id=sid, today=today)
            if nd['next_overall']:
                n = nd['next_overall']
                st.info(f"Next due: {n['subject_id']} — {n['visit_name']} "
                        f"on {n['target_date']} ({n['status']})")
        else:
            st.caption('No subject visits yet — generate a calendar above.')

        st.divider()
        st.subheader('4. Budget & reconciliation')
        existing = billing.list_budget(sid)
        seed = pd.DataFrame(existing)[['item_name', 'unit_cost', 'category']] \
            if existing else pd.DataFrame(
                [{'item_name': '', 'unit_cost': 0.0, 'category': ''}])
        edited = st.data_editor(seed, num_rows='dynamic', key='budget_editor')
        if st.button('Save budget', key='save_budget'):
            items = [r for r in edited.to_dict(orient='records')
                     if str(r.get('item_name') or '').strip()]
            res = billing.set_budget(sid, items)
            st.success(f"Saved {res['item_count']} budget items.")
        if st.button('Auto-map activities → budget', key='auto_map'):
            res = billing.auto_map(sid)
            st.success(f"Mapped {res['matched']}/{res['activity_count']} activities.")

        if st.button('Run reconciliation', key='reconcile'):
            rec = billing.reconcile(sid, today=today)
            st.write('Summary:', rec['summary'])
            if rec['line_items']:
                df = pd.DataFrame(rec['line_items'])
                st.dataframe(df, use_container_width=True)
                st.download_button('Download reconciliation CSV',
                                   df.to_csv(index=False).encode('utf-8'),
                                   'reconciliation.csv', 'text/csv')
