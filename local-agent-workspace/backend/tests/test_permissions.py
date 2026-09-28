import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.permissions import tool_decision
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


@pytest.fixture
def setup(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    settings = Settings(tmp_path / 'state')
    settings.values.update(workspace=str(project), env_file='')
    store = Store(settings.state_dir / 'tests.sqlite3')
    yield settings, store, WorkspaceTools(str(project))
    store.db.close()


async def pending(manager):
    for _ in range(100):
        if manager.pending:
            return next(iter(manager.pending))
        await asyncio.sleep(.01)
    raise AssertionError('Expected a permission prompt')


@pytest.mark.parametrize('allow', [True, False])
async def test_external_csv_listing_requires_access_and_remembers_grant(setup, tmp_path, allow):
    settings, store, tools = setup
    downloads = tmp_path / 'Downloads'
    downloads.mkdir()
    (downloads / 'report.CSV').write_text('value\n1\n')
    (downloads / 'notes.txt').write_text('ignore')
    (downloads / '.env').write_text('secret')
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    task = asyncio.create_task(manager.execute_tool(session, tools, 'list_files', {'path': str(downloads), 'glob': '*.csv'}, 'csv'))
    sid, event_id = await pending(manager)
    assert session['events'][-1]['name'] == 'access_directory'
    manager.decide(sid, event_id, allow)
    result = await task
    if not allow:
        assert 'declined' in result
        assert session['allowed_directories'] == []
        return
    assert [row['name'] for row in json.loads(result)['entries']] == ['report.CSV']
    saved = store.get(session['id'])
    assert saved['allowed_directories'] == [str(downloads)]
    restored_tools = WorkspaceTools(str(tools.root), allowed_directories=saved['allowed_directories'])
    assert restored_tools.read_file(str(downloads / 'report.CSV')) == 'value\n1\n'
    matches = restored_tools.search_files('value', path=str(downloads))['matches']
    assert [(match['path'], match['line'], match['text']) for match in matches] == [(str(downloads / 'report.CSV'), 1, 'value')]
    with pytest.raises(ValueError):
        restored_tools.read_file(str(downloads / '.env'))
    # A sibling of the approved directory is still outside the grant.
    with pytest.raises(ValueError):
        restored_tools.path(str(tmp_path / 'another-folder' / 'note.txt'))


async def test_external_single_file_search_requests_its_parent_folder(setup, tmp_path):
    settings, store, tools = setup
    external = tmp_path / 'Downloads' / 'report.txt'
    external.parent.mkdir()
    external.write_text('find this marker\n')
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    task = asyncio.create_task(manager.execute_tool(
        session, tools, 'search_files', {'path': str(external), 'query': 'marker'}, 'single-file'))
    sid, event_id = await pending(manager)
    event = session['events'][-1]
    assert event['name'] == 'access_directory'
    assert event['input'] == {'path': str(external.parent)}
    manager.decide(sid, event_id, True)
    result = json.loads(await task)
    assert [(match['path'], match['line']) for match in result['matches']] == [(str(external), 1)]
    assert session['allowed_directories'] == [str(external.parent)]


async def test_symlink_cannot_expand_a_folder_grant(setup, tmp_path):
    _, _, tools = setup
    approved = tmp_path / 'approved'
    approved.mkdir()
    outside = tmp_path / 'private.txt'
    outside.write_text('outside')
    (approved / 'link.txt').symlink_to(outside)
    tools.allowed_directories.append(approved)
    with pytest.raises(ValueError):
        tools.read_file(str(approved / 'link.txt'))
    assert tools.list_files(str(approved)) == []


@pytest.mark.parametrize('mode', ['acceptEdits', 'auto', 'bypassPermissions'])
async def test_automatic_edits_do_not_wait_for_approval(setup, mode):
    settings, store, tools = setup
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session['permission_mode'] = mode
    await asyncio.wait_for(manager.execute_tool(session, tools, 'write_file', {'path': 'result.txt', 'content': 'done'}, 'edit'), 1)
    assert (tools.root / 'result.txt').read_text() == 'done'
    assert not manager.pending


async def test_plan_blocks_writes_and_commands(setup):
    settings, store, tools = setup
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session['permission_mode'] = 'plan'
    for name, arguments in [('write_file', {'path': 'blocked.txt', 'content': 'no'}),
                            ('run_command', {'command': 'touch blocked.txt'})]:
        result = await manager.execute_tool(session, tools, name, arguments, name)
        assert 'read-only' in result
    assert not (tools.root / 'blocked.txt').exists()
    assert not manager.pending
    assert json.loads(await manager.execute_tool(session, tools, 'list_files', {}, 'read'))['entries'] == []


@pytest.mark.parametrize('command', ['pwd; touch changed', 'ls > changed', 'ls $(touch changed)', 'python -c "print(1)"', 'ls ../private', 'ls\ntouch changed'])
def test_auto_does_not_classify_arbitrary_shell_as_read_only(command):
    assert tool_decision('auto', 'run_command', {'command': command}) == 'ask'


async def test_auto_runs_basic_command_using_os_binary(setup, monkeypatch):
    settings, store, tools = setup
    (tools.root / 'pwd').write_text('#!/bin/sh\ntouch should-not-run\n')
    (tools.root / 'pwd').chmod(0o755)
    monkeypatch.setenv('PATH', str(tools.root))
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session['permission_mode'] = 'auto'
    result = json.loads(await asyncio.wait_for(manager.execute_tool(session, tools, 'run_command', {'command': 'pwd'}, 'cmd'), 1))
    assert result['output'].strip() == str(tools.root)
    assert not (tools.root / 'should-not-run').exists()


async def test_bypass_allows_external_files_but_preserves_credential_exclusions(setup, tmp_path):
    settings, store, _ = setup
    session = store.create(settings.values)
    session['permission_mode'] = 'bypassPermissions'
    tools = WorkspaceTools(session['workspace'], unrestricted=True)
    manager = AgentManager(store, settings)
    target = tmp_path / 'outside.txt'
    await asyncio.wait_for(manager.execute_tool(session, tools, 'write_file', {'path': str(target), 'content': 'allowed'}, 'write'), 1)
    assert target.read_text() == 'allowed'
    result = await manager.execute_tool(session, tools, 'read_file', {'path': str(tmp_path / '.env')}, 'secret')
    assert 'Credential files are excluded' in result
    assert not manager.pending


def test_permissions_api_validates_persists_and_rejects_active_changes(setup):
    settings, _, _ = setup
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {'X-Local-Token': client.get('/api/bootstrap').json()['token']}
        session = client.post('/api/sessions', json={'permission_mode': 'plan'}, headers=headers).json()
        sid = session['id']
        assert session['permission_mode'] == 'plan'
        route = f'/api/sessions/{sid}/permissions'
        assert client.put(route, json={'permission_mode': 'invalid'}, headers=headers).status_code == 422
        assert client.put(route, json={'permission_mode': 'acceptEdits'}, headers=headers).status_code == 200
        assert client.get(f'/api/sessions/{sid}', headers=headers).json()['permission_mode'] == 'acceptEdits'
        app.state.manager.statuses[sid] = 'awaiting_approval'
        assert client.put(route, json={'permission_mode': 'bypassPermissions'}, headers=headers).status_code == 409


async def test_os_folder_denial_is_distinct_from_chat_permissions(setup, monkeypatch):
    settings, store, tools = setup
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    monkeypatch.setattr('local_agent.tools.sys.platform', 'darwin')

    def denied(*args, **kwargs):
        raise PermissionError(1, 'Operation not permitted', str(tools.root))

    monkeypatch.setattr(tools, 'list_files', denied)
    output = await manager.execute_tool(session, tools, 'list_files', {}, 'os-denied')
    assert 'macOS denied access' in output
    assert 'Files and Folders' in output
    assert 'Bypass permissions in chat cannot override' in output
    assert session['events'][-1]['state'] == 'error'


def test_search_does_not_report_no_matches_when_os_blocks_walk(setup, monkeypatch):
    _, _, tools = setup

    def blocked_walk(path, *, followlinks, onerror):
        onerror(PermissionError(1, 'Operation not permitted', str(path)))
        return iter(())

    monkeypatch.setattr('local_agent.tools.os.walk', blocked_walk)
    with pytest.raises(PermissionError):
        tools.search_files('hello')


def test_browser_folder_check_grant_and_remove(setup, tmp_path):
    settings, _, _ = setup
    folder = tmp_path / 'Downloads'
    folder.mkdir()
    (folder / 'example.csv').write_text('hello')
    with TestClient(create_app(settings)) as client:
        headers = {'X-Local-Token': client.get('/api/bootstrap').json()['token']}
        session = client.post('/api/sessions', headers=headers).json()
        sid = session['id']
        params = {'path': str(folder), 'session_id': sid}
        checked = client.post('/api/folder-access/check', json={'path': str(folder)}, headers=headers).json()
        assert checked['accessible'] is True
        assert 'example.csv' not in json.dumps(checked)
        assert client.get('/api/files', params=params, headers=headers).status_code == 400
        route = f'/api/sessions/{sid}/folders'
        for _ in range(2):
            granted = client.post(route, json={'path': str(folder)}, headers=headers).json()
            assert granted['allowed_directories'] == [str(folder)]
        assert client.get('/api/files', params=params, headers=headers).json()[0]['name'] == 'example.csv'
        removed = client.request('DELETE', route, json={'path': str(folder)}, headers=headers).json()
        assert removed['allowed_directories'] == []
        assert client.get('/api/files', params=params, headers=headers).status_code == 400


def test_browser_folder_access_never_grants_an_unreadable_directory(setup, tmp_path, monkeypatch):
    from pathlib import Path
    settings, _, _ = setup
    folder = tmp_path / 'Downloads'
    folder.mkdir()
    original_iterdir = Path.iterdir

    def denied(self):
        if self == folder:
            raise PermissionError(1, 'Operation not permitted', str(folder))
        return original_iterdir(self)

    monkeypatch.setattr(Path, 'iterdir', denied)
    monkeypatch.setattr('local_agent.tools.sys.platform', 'darwin')
    with TestClient(create_app(settings)) as client:
        headers = {'X-Local-Token': client.get('/api/bootstrap').json()['token']}
        sid = client.post('/api/sessions', headers=headers).json()['id']
        checked = client.post('/api/folder-access/check', json={'path': str(folder)}, headers=headers).json()
        assert checked['accessible'] is False
        assert 'macOS denied' in checked['error']
        assert checked['python_executable']
        assert client.post(f'/api/sessions/{sid}/folders', json={'path': str(folder)}, headers=headers).status_code == 400
        assert client.get(f'/api/sessions/{sid}', headers=headers).json()['allowed_directories'] == []


def test_browser_folder_grants_require_idle_session_and_local_auth(setup, tmp_path):
    settings, _, _ = setup
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {'X-Local-Token': client.get('/api/bootstrap').json()['token']}
        sid = client.post('/api/sessions', headers=headers).json()['id']
        route = f'/api/sessions/{sid}/folders'
        assert client.post(route, json={'path': str(tmp_path)}).status_code == 403
        app.state.manager.statuses[sid] = 'running'
        assert client.post(route, json={'path': str(tmp_path)}, headers=headers).status_code == 409
        assert client.request('DELETE', route, json={'path': str(tmp_path)}, headers=headers).status_code == 409
