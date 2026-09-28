import json

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.instructions import load_project_instructions
from local_agent.store import Store
from local_agent.tool_profiles import READ_TOOLS, child_profile, tool_allowed
from local_agent.tools import WorkspaceTools


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr('local_agent.config.APP_ROOT', tmp_path)
    settings = Settings(tmp_path / 'state')
    settings.env = {}
    settings.values.update(workspace=str(tmp_path), env_file='')
    settings.credentials = lambda: ('https://gateway.example', 'synthetic-token')
    store = Store(settings.state_dir / 'profiles.sqlite3')
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    tools = WorkspaceTools(str(tmp_path))
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance['text'], guidance['warnings'])
    yield manager, session, tools
    store.db.close()


@pytest.mark.parametrize('ceiling,own,requested,expected', [
    ('inherit', 'inherit', None, 'inherit'),
    ('read_only', 'inherit', 'inherit', 'read_only'),
    ('file_editor', 'inherit', 'read_only', 'read_only'),
    ('inherit', 'file_editor', 'inherit', 'file_editor'),
    ('file_editor', 'read_only', 'inherit', 'read_only'),
])
def test_requested_profile_cannot_widen_capability_ceiling(ceiling, own, requested, expected):
    assert child_profile({'subagent_tool_profile': ceiling, 'tool_profile': own}, requested) == expected


@pytest.mark.parametrize('name,arguments', [
    ('write_file', {'path': 'blocked.txt', 'content': 'not allowed'}),
    ('run_command', {'command': 'touch blocked.txt'}),
    ('mcp__server__write', {}),
    ('create_task', {'title': 'not allowed'}),
    ('stop_job', {'job_id': 'foreign'}),
    ('delegate_task', {'task': 'not allowed'}),
])
async def test_read_only_denies_side_effects_even_in_bypass(runtime, name, arguments):
    manager, session, tools = runtime
    session.update(tool_profile='read_only', permission_mode='bypassPermissions')
    async def unexpected(*args, **kwargs):
        pytest.fail('Restricted tool reached approvals or hooks')
    manager.approve = manager.run_hooks = unexpected
    output = await manager.execute_tool(session, tools, name, arguments, 'blocked')
    assert 'does not permit' in output
    assert session['events'][-1]['state'] == 'rejected'
    assert not (tools.root / 'blocked.txt').exists()


async def test_file_only_profiles_never_launch_hooks(runtime):
    manager, session, tools = runtime
    session.update(tool_profile='read_only', permission_mode='bypassPermissions')
    manager.extensions.update_config({'hooks': [{'id': 'hook', 'event': 'before_tool', 'enabled': True, 'command': 'touch blocked.txt'}]})
    async def unexpected(*args, **kwargs):
        pytest.fail('Read-only profile launched a hook')
    manager.extensions.run_hook = unexpected
    (tools.root / 'note.txt').write_text('Safe reading')
    result = await manager.execute_tool(session, tools, 'read_file', {'path': 'note.txt'}, 'read')
    assert 'Safe reading' in result
    assert session['events'][-1]['state'] == 'completed'


async def test_file_editor_keeps_normal_approval_policy(runtime):
    manager, session, tools = runtime
    session.update(tool_profile='file_editor', permission_mode='manual')
    called = []
    async def deny(*args):
        called.append(True)
        return False
    manager.approve = deny
    await manager.execute_tool(session, tools, 'write_file', {'path': 'blocked.txt', 'content': 'no'}, 'write')
    assert called == [True]
    assert not (tools.root / 'blocked.txt').exists()
    assert tool_allowed(session, 'edit_file')
    assert not tool_allowed(session, 'run_command')


async def test_read_only_request_filters_schemas_and_never_starts_mcp(runtime, monkeypatch):
    manager, session, _ = runtime
    session.update(tool_profile='read_only', is_subagent=True, title_generated=True)
    def unexpected():
        pytest.fail('Read-only profile tried to start MCP')
    manager.extensions.turn = unexpected
    requests = []
    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert {tool['function']['name'] for tool in body['tools']} == READ_TOOLS
        return httpx.Response(200, text='data: ' + json.dumps({'choices': [{'delta': {'content': 'Inspected.'}, 'finish_reason': 'stop'}]}) + '\n\ndata: [DONE]\n\n')
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)))
    await manager.run(session, 'Inspect only')
    assert len(requests) == 1
    assert session['terminal_reason'] == 'completed'


async def test_delegation_reads_authoritative_ceiling_and_persists_child_profile(runtime):
    manager, parent, _ = runtime
    parent['subagent_tool_profile'] = 'read_only'
    manager.store.save(parent)
    async def worker(session, prompt):
        await manager.event(session, 'assistant', text='Read complete')
    manager.run_databricks = worker
    result = await manager.delegates.delegate({**parent, 'subagent_tool_profile': 'inherit'}, 'Read', tool_profile='inherit')
    child = manager.store.get(result['child_session_id'])
    assert child['tool_profile'] == 'read_only'
    restarted = AgentManager(manager.store, manager.settings)
    assert restarted.get(child['id'])['tool_profile'] == 'read_only'
    with pytest.raises(ValueError, match='Tool profile'):
        await manager.delegates.delegate(parent, 'Invalid', tool_profile='admin')
    assert len(manager.store.list()) == 2


def test_profile_api_requires_idle_parent_and_valid_value(runtime):
    manager, _, _ = runtime
    app = create_app(manager.settings)
    with TestClient(app) as client:
        token = {'X-Local-Token': client.get('/api/bootstrap').json()['token']}
        parent = client.post('/api/sessions', json={}, headers=token).json()
        path = f"/api/sessions/{parent['id']}/subagent-profile"
        assert client.put(path, json={'tool_profile': 'read_only'}).status_code == 403
        response = client.put(path, json={'tool_profile': 'read_only'}, headers=token)
        assert response.status_code == 200
        assert response.json()['subagent_tool_profile'] == 'read_only'
        assert client.put(path, json={'tool_profile': 'admin'}, headers=token).status_code == 400
        app.state.manager.statuses[parent['id']] = 'running'
        assert client.put(path, json={'tool_profile': 'inherit'}, headers=token).status_code == 409
        app.state.manager.statuses[parent['id']] = 'idle'
        child = app.state.manager.store.get(parent['id'])
        child.update(is_subagent=True, tool_profile='read_only')
        app.state.manager.store.save(child)
        assert client.put(path, json={'tool_profile': 'inherit'}, headers=token).status_code == 400
