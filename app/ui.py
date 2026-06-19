import streamlit as st
import pandas as pd
from app.graph import build_graph
from app.ingestion import ingest_uploaded_file
from app.exporter import export_csv, export_xlsx
from app.chat import run_chat, ChatNotConfigured, ChatProviderError

st.set_page_config(layout='wide', page_title='Clinical Protocol Builder')
st.title('Clinical Protocol Builder (Starter Product)')


def _require_login():
    """Gate the page with the shared access token when one is configured."""
    import os
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

if 'schedule' not in st.session_state:
    st.session_state.schedule = None
if 'chat_history' not in st.session_state:
    st.session_state.chat_history = []


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

graph = build_graph()

uploaded_file = st.file_uploader('Upload protocol (.txt, .pdf, .docx)', type=['txt', 'pdf', 'docx'])

if uploaded_file is not None:
    try:
        text, source_type = ingest_uploaded_file(uploaded_file.name, uploaded_file.read())
        with st.spinner('Parsing protocol...'):
            schedule = graph.invoke({'text': text})['result']
        st.session_state.schedule = schedule
        st.success(f'Parsed {uploaded_file.name} ({source_type})')
    except Exception as e:
        st.error(f'Could not parse file: {e}')

schedule = st.session_state.schedule

if schedule is not None:
    st.header('Visits & Activities')
    edited_rows = []
    for idx, v in enumerate(schedule.visits):
        rows = []
        for p in v.procedures:
            rows.append({
                'Activity': p.name,
                'Conditional': p.conditional,
                'Source': p.source.parser_route,
            })
        df = pd.DataFrame(rows)
        with st.expander(v.name, expanded=(idx == 0)):
            edited = st.data_editor(df, num_rows='dynamic', key=f'visit_{idx}')
            for _, r in edited.iterrows():
                edited_rows.append({'visit': v.name, 'activity': r['Activity'], 'conditional': r.get('Conditional')})

    st.header('Conflicts')
    if schedule.conflicts:
        st.dataframe(pd.DataFrame([c.model_dump() for c in schedule.conflicts]), use_container_width=True)
    else:
        st.success('No conflicts detected.')

    st.header('Operational Rows (CTMS-ready)')
    rows_df = pd.DataFrame([r.model_dump() for r in schedule.flat_rows])
    st.dataframe(rows_df, use_container_width=True)

    csv_bytes = rows_df.to_csv(index=False).encode('utf-8')
    st.download_button('Download CTMS-ready CSV', csv_bytes, 'protocol_schedule.csv', 'text/csv')

    edited_df = pd.DataFrame(edited_rows)
    st.download_button('Download edited activities CSV', edited_df.to_csv(index=False).encode('utf-8'), 'edited_schedule.csv', 'text/csv')

    st.markdown('### What this app currently supports')
    st.markdown(
        '- Upload protocol\n'
        '- Review parsed visits\n'
        '- Inspect conflicts between SoA and narrative extraction\n'
        '- Export CTMS-ready rows'
    )
else:
    st.info('Upload a protocol file to begin.')

# --- Embedded AI assistant -------------------------------------------------
st.divider()
st.header('🤖 Ask the AI about this protocol')
if schedule is None:
    st.caption('Upload a protocol above, then ask about its visits, activities, '
               'and conflicts. The assistant can also look up subjects, '
               'compliance, and transport trips.')
else:
    st.caption('The assistant can see the parsed protocol above and can also '
               'look up subjects, compliance, and transport trips. It remembers '
               'preferences you tell it.')

for _m in st.session_state.chat_history:
    with st.chat_message(_m['role']):
        st.markdown(_m['content'])

_prompt = st.chat_input('Ask about visits, conflicts, a subject, trips…')
if _prompt:
    st.session_state.chat_history.append({'role': 'user', 'content': _prompt})
    with st.chat_message('user'):
        st.markdown(_prompt)
    with st.chat_message('assistant'):
        try:
            with st.spinner('Thinking…'):
                _result = run_chat(
                    st.session_state.chat_history,
                    protocol_context=_protocol_context(schedule),
                )
            _reply = _result['reply'] or '(no reply)'
            st.markdown(_reply)
            if _result['tool_calls']:
                st.caption('used: ' + ', '.join(t['name'] for t in _result['tool_calls']))
            st.session_state.chat_history.append({'role': 'assistant', 'content': _reply})
        except ChatNotConfigured as _e:
            st.warning(str(_e))
        except ChatProviderError as _e:
            st.error(str(_e))
