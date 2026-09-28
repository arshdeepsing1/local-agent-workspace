import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.instructions import load_project_instructions
from local_agent.reasoning import reasoning_summary
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr('local_agent.config.APP_ROOT', tmp_path)
    project = tmp_path / 'project'
    project.mkdir()
    settings = Settings(tmp_path / 'state')
    settings.values.update(workspace=str(project), env_file='')
    settings.credentials = lambda: ('https://gateway.example', 'test-token')
    store = Store(settings.state_dir / 'features.sqlite3')
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session['permission_mode'] = 'acceptEdits'
    store.save(session)
    tools = WorkspaceTools(str(project))
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance['text'], guidance['warnings'])
    yield manager, session, project, tools
    store.db.close()


def gateway(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)))


def stream(content=None, call=None):
    delta = {'content': content} if call is None else {'tool_calls': [{'index': 0, 'id': 'call', 'type': 'function', 'function': call}]}
    return httpx.Response(200, text='data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': 'stop' if call is None else 'tool_calls'}]}) + '\n\ndata: [DONE]\n\n')


async def test_agent_write_creates_checkpoint_only_after_approval(runtime):
    manager, session, project, tools = runtime
    turn = await manager.event(session, 'user', text='Update the note')
    (project / 'note.txt').write_text('before')
    session['permission_mode'] = 'manual'
    async def refuse(*args):
        return False
    manager.approve = refuse
    await manager.execute_tool(session, tools, 'write_file', {'path': 'note.txt', 'content': 'after'}, 'denied')
    assert not manager.checkpoints.list(session['id'])
    assert (project / 'note.txt').read_text() == 'before'
    session['permission_mode'] = 'acceptEdits'
    await manager.execute_tool(session, tools, 'write_file', {'path': 'note.txt', 'content': 'after'}, 'accepted')
    checkpoint = manager.checkpoints.list(session['id'])[0]
    assert checkpoint['turn_id'] == turn['id']
    preview = manager.checkpoints.preview(checkpoint['id'], tools, session['id'])
    manager.checkpoints.restore(checkpoint['id'], tools, preview['expected_current_hash'], session['id'])
    assert (project / 'note.txt').read_text() == 'before'


@pytest.mark.parametrize('is_error', [True, False])
async def test_mcp_result_outcome_is_visible_without_retry_or_losing_model_output(runtime, is_error):
    manager, session, _, tools = runtime
    session['permission_mode'] = 'bypassPermissions'
    calls = []

    class Connection:
        tool_names = {'mcp__demo__outcome'}
        definitions = [{'function': {'name': 'mcp__demo__outcome', 'parameters': {'type': 'object'}}}]

        async def call(self, name, args):
            calls.append(name)
            return {'output': 'Useful diagnostic', 'is_error': is_error, 'truncated': False}

    manager.extension_connections[session['id']] = Connection()
    result = await manager.execute_tool(session, tools, 'mcp__demo__outcome', {}, 'outcome-call')
    assert calls == ['mcp__demo__outcome']
    assert json.loads(result)['is_error'] is is_error
    assert json.loads(result)['output'] == 'Useful diagnostic'
    event = session['events'][-1]
    assert event['state'] == ('error' if is_error else 'completed')
    assert manager.store.get(session['id'])['events'][-1] == event


@pytest.mark.parametrize('mode,approved,expected', [('manual', False, 0), ('manual', True, 1), ('bypassPermissions', False, 1), ('plan', True, 0)])
async def test_mcp_uses_policy_and_live_turn_connection(runtime, mode, approved, expected):
    manager, session, _, tools = runtime
    session['permission_mode'] = mode
    called = []
    class Connection:
        tool_names = {'mcp__demo__echo'}
        definitions = [{'function': {'name': 'mcp__demo__echo', 'parameters': {'type': 'object'}}}]
        async def call(self, name, args):
            called.append(args)
            return {'ok': True}
    manager.extension_connections[session['id']] = Connection()
    async def approve(*args):
        return approved
    manager.approve = approve
    await manager.execute_tool(session, tools, 'mcp__demo__echo', {'value': 'hello'}, 'mcp')
    assert len(called) == expected
    assert session['events'][-1]['state'] == ('completed' if expected else 'rejected' if mode == 'plan' else 'running')


async def test_mcp_connection_same_task_and_tools_sent(runtime, monkeypatch):
    manager, session, _, _ = runtime
    owner, calls = [], []
    class Connection:
        definitions = [{'type': 'function', 'function': {'name': 'mcp__demo__echo', 'description': 'echo', 'parameters': {'type': 'object', 'properties': {}}}}]
        async def discover(self):
            assert asyncio.current_task() == owner[0]
            return self.definitions
        async def call(self, name, args):
            assert asyncio.current_task() == owner[0]
            calls.append(name)
            return 'echo result'
    @asynccontextmanager
    async def turn():
        owner.append(asyncio.current_task())
        try:
            yield Connection()
        finally:
            assert asyncio.current_task() == owner[0]
    manager.extensions.turn = turn
    session['permission_mode'] = 'bypassPermissions'
    requests = []
    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert any(item['function']['name'] == 'mcp__demo__echo' for item in payload['tools'])
        if len(requests) == 1:
            return stream(call={'name': 'mcp__demo__echo', 'arguments': '{}'})
        assert payload['messages'][-1]['content'] == 'echo result'
        return stream('Done')
    gateway(monkeypatch, handler)
    await manager.run(session, 'Echo')
    assert calls == ['mcp__demo__echo']
    assert session['id'] not in manager.extension_connections


def configure_hook(manager, phase='before_tool'):
    manager.extensions.update_config({'hooks': [{'id': 'check', 'enabled': True, 'event': phase, 'command': 'true'}]})


async def test_before_hook_requires_separate_approval_and_blocks_change(runtime):
    manager, session, project, tools = runtime
    configure_hook(manager)
    approvals = []
    async def deny(session, event):
        approvals.append(event['name'])
        return False
    manager.approve = deny
    await manager.execute_tool(session, tools, 'write_file', {'path': 'note.txt', 'content': 'after'}, 'edit')
    assert approvals == ['hook_before_tool']
    assert not (project / 'note.txt').exists()
    assert not manager.checkpoints.list()
    assert session['events'][0]['state'] == 'error'


async def test_before_hook_changed_file_is_not_overwritten(runtime):
    manager, session, project, tools = runtime
    configure_hook(manager)
    session['permission_mode'] = 'bypassPermissions'
    (project / 'note.txt').write_text('before')
    async def mutate(*args):
        (project / 'note.txt').write_text('hook edit')
        return {'exit_code': 0, 'output': '', 'truncated': False}
    manager.extensions.run_hook = mutate
    await manager.execute_tool(session, tools, 'write_file', {'path': 'note.txt', 'content': 'agent edit'}, 'edit')
    assert (project / 'note.txt').read_text() == 'hook edit'
    assert not manager.checkpoints.list()


async def test_after_hook_failure_keeps_successful_tool_result(runtime):
    manager, session, project, tools = runtime
    configure_hook(manager, 'after_tool')
    session['permission_mode'] = 'bypassPermissions'
    async def fail(*args):
        return {'exit_code': 1, 'output': 'failed check', 'truncated': False}
    manager.extensions.run_hook = fail
    result = await manager.execute_tool(session, tools, 'write_file', {'path': 'note.txt', 'content': 'saved'}, 'edit')
    assert '+saved' in result
    assert (project / 'note.txt').read_text() == 'saved'
    assert session['events'][0]['state'] == 'completed'
    assert session['events'][-1]['type'] == 'notice'


async def test_plan_skips_hooks_but_can_track_tasks(runtime):
    manager, session, _, tools = runtime
    configure_hook(manager)
    session['permission_mode'] = 'plan'
    async def unexpected(*args):
        pytest.fail('Plan must not execute hooks')
    manager.extensions.run_hook = unexpected
    result = await manager.execute_tool(session, tools, 'create_task', {'title': 'Inspect'}, 'plan')
    assert json.loads(result)['status'] == 'pending'
    assert len(session['events']) == 1


async def test_skill_is_loaded_explicitly_and_fresh(runtime, monkeypatch):
    manager, session, project, tools = runtime
    skill = project / '.agents/skills/review/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('Review rule: trace behavior.')
    seen = []
    async def handler(request):
        seen.append(json.loads(request.content)['messages'][0]['content'])
        return stream('Reviewed')
    gateway(monkeypatch, handler)
    await manager.run_databricks(session, '/skill review inspect the code')
    assert 'Review rule: trace behavior.' in seen[-1]
    skill.write_text('Review rule: test the change.')
    await manager.run_databricks(session, 'Continue')
    assert 'Review rule: test the change.' in seen[-1]
    assert session['active_skills'] == ['review']


async def test_child_budget_and_no_recursive_or_background_tools(runtime, monkeypatch):
    manager, session, _, tools = runtime
    session.update(is_subagent=True, max_steps=2, permission_mode='bypassPermissions')
    count = []
    async def handler(request):
        payload = json.loads(request.content)
        assert 'delegate_task' not in [tool['function']['name'] for tool in payload['tools']]
        count.append(1)
        return stream(call={'name': 'list_tasks', 'arguments': '{}'})
    gateway(monkeypatch, handler)
    await manager.run(session, 'Inspect')
    assert len(count) == 2
    assert session['events'][-1]['type'] == 'error'
    assert '2-request agent limit' in session['events'][-1]['text']
    assert 'model requests, not tool calls' in session['events'][-1]['text']
    result = await manager.execute_tool(session, tools, 'run_command', {'command': 'true', 'background': True}, 'bg')
    assert 'cannot delegate or leave background' in result
    assert not manager.jobs.list()


async def test_provider_summary_is_separate_persisted_and_not_replayed(runtime, monkeypatch):
    manager, session, _, _ = runtime
    blocks = [{'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'Checking the file.'}], 'encrypted_content': 'opaque-not-for-ui'}, {'type': 'text', 'text': 'Answer.'}]
    seen = []
    async def handler(request):
        seen.append(json.loads(request.content))
        return stream(blocks)
    gateway(monkeypatch, handler)
    await manager.run(session, 'Review')
    saved = manager.store.get(session['id'])
    reply = next(event for event in saved['events'] if event['type'] == 'assistant')
    assert reply['text'] == 'Answer.'
    assert reply['reasoning_summary'] == 'Checking the file.'
    assert 'opaque-not-for-ui' not in json.dumps(saved)
    assert saved['wire'][-1]['content'] == 'Answer.'
    assert reasoning_summary([{'type': 'thinking', 'text': 'internal'}, {'type': 'reasoning', 'summary': 'bad'}]) == ''
    assert reasoning_summary('plain answer') == ''


def test_api_save_restore_scope_stale_and_tasks(runtime):
    manager, _, project, _ = runtime
    (project / 'note.txt').write_text('before')
    with TestClient(create_app(manager.settings)) as client:
        headers = {'X-Local-Token': client.get('/api/bootstrap').json()['token']}
        def request(method, path, **kwargs):
            return client.request(method, '/api' + path, headers=headers, **kwargs)
        sid = request('POST', '/sessions').json()['id']
        other = request('POST', '/sessions').json()['id']
        scope = f'?session_id={sid}'
        assert request('PUT', '/file', json={'path': 'note.txt', 'original': 'before', 'content': 'after', 'session_id': sid}).status_code == 200
        checkpoint = request('GET', '/checkpoints' + scope).json()[0]
        assert request('GET', '/checkpoints').json() == []
        assert request('GET', '/checkpoints?session_id=' + other).json() == []
        assert request('GET', f"/checkpoints/{checkpoint['id']}/preview?session_id={other}").status_code == 400
        url = f"/checkpoints/{checkpoint['id']}"
        preview = request('GET', url + '/preview' + scope).json()
        assert preview['can_restore']
        (project / 'note.txt').write_text('external')
        assert request('POST', url + '/restore' + scope, json={'expected_current_hash': preview['expected_current_hash']}).status_code == 400
        task = request('POST', f'/sessions/{sid}/tasks', json={'title': 'Check'}).json()
        assert request('PATCH', f"/sessions/{other}/tasks/{task['id']}", json={'status': 'completed'}).status_code == 400
        assert request('PATCH', f"/sessions/{sid}/tasks/{task['id']}", json={'status': 'completed'}).json()['status'] == 'completed'
        assert request('DELETE', f'/sessions/{sid}').status_code == 200
        assert request('GET', f'/sessions/{sid}/tasks').status_code == 404


async def test_skill_activation_cannot_be_followed_by_uninformed_edit(runtime):
    manager, session, project, tools = runtime
    skill = project / '.agents/skills/check/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('Use focused changes. ' + '😀' * 1000)
    output = await manager.execute_tool(session, tools, 'use_skill', {'skill_id': 'check'}, 'skill')
    assert len(output.encode()) < 1000
    await manager.execute_tool(session, tools, 'write_file', {'path': 'note.txt', 'content': 'new'}, 'edit')
    assert not (project / 'note.txt').exists()
    assert session['events'][-1]['state'] == 'rejected'
    tools.skill_signature = manager.skill_text(session, tools)
    await manager.execute_tool(session, tools, 'write_file', {'path': 'note.txt', 'content': 'new'}, 'informed-edit')
    assert (project / 'note.txt').read_text() == 'new'


def test_metadata_pages_bound_escaped_unicode_and_no_omissions():
    items = [{'id': str(i), 'description': '😀' * 300} for i in range(30)]
    found, offset = [], 0
    while True:
        page = AgentManager.metadata_page(items, offset, 'skills')
        assert len(json.dumps(page, indent=2).encode()) <= 8000
        found.extend(page['skills'])
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    assert found == items
