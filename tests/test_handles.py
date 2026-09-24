"""Handles are presentation/address aliases, never replacement database identities."""
import pytest

from troupe.config import parse_agents
from troupe.engine import Engine, Wake
from troupe.gui.data import Data
from troupe.store import HandleBook, Store
from troupe.team import TeamAPI


def test_derivation_and_aliases():
    names = HandleBook('My  Project! /One', [
        {'id': 'builder-2', 'role': 'builder'},
        {'id': 'builder_3', 'role': 'builder'}, {'id': 'spec', 'role': 'spec'}])
    assert names.name('builder-2') == 'builder_2@My_Project_One'
    assert names.name('spec') == 'spec_1@My_Project_One'
    for alias in ('BUILDER-2', 'Builder_2', 'BUILDER_2@MY_PROJECT_ONE'):
        assert names.resolve(alias) == ['builder-2']
    assert names.resolve('BUILDER') == ['builder-2', 'builder_3']
    assert names.resolve('TEAM', exclude='spec') == ['builder-2', 'builder_3']
    assert names.resolve('HUMAN') == ['human']
    with pytest.raises(ValueError, match='Cross-project'):
        names.resolve('builder_2@elsewhere')
    with pytest.raises(ValueError, match='builder_2@My_Project_One'):
        names.resolve('missing')


def test_handle_collision_is_config_error():
    with pytest.raises(ValueError, match='team.yaml.*collides'):
        parse_agents({'agents': [{'id': 'lead', 'role': 'lead', 'provider': 'codex'},
            {'id': 'builder-2', 'role': 'builder', 'provider': 'codex'},
            {'id': 'builder_2', 'role': 'builder', 'provider': 'codex'}]})


@pytest.mark.parametrize('address', ['builder-2', 'BUILDER_2', 'BUILDER_2@TEST-PROJECT'])
def test_tools_resolve_to_original_ids(project, address):
    cfg, store = project
    api = TeamAPI(cfg, store, 'lead')
    assert 'builder_2@test-project' in api.send_message(address, 'literal builder-2 body')
    assert store.messages()[0]['recipient'] == 'builder-2'
    assert not api.create_task('Alias', 'Brief', assignee=address).startswith('ERROR:')
    task = store.tasks()[0]
    assert task['assignee'] == 'builder-2'
    assert not api.update_task(task['id'], assignee='builder_1@test-project').startswith('ERROR:')
    assert store.task(task['id'])['assignee'] == 'builder-1'
    assert not api.update_task(task['id'], assignee=address).startswith('ERROR:')
    assert store.task(task['id'])['assignee'] == 'builder-2'
    assert 'builder_2@test-project' in api.get_task(task['id'])
    for result in (api.send_message('wrong', 'Body'), api.create_task('Bad', '', assignee='wrong'),
                   api.update_task(task['id'], assignee='wrong')):
        assert result.startswith('ERROR:') and 'builder_2@test-project' in result


def test_role_assignment_and_fanout(project):
    cfg, store = project
    api = TeamAPI(cfg, store, 'lead')
    api.send_message('BUILDER', 'Hello builders')
    assert {m['recipient'] for m in store.messages()} == {'builder-1', 'builder-2'}
    api.create_task('Role', '', assignee='BUILDER')
    task = store.tasks()[0]
    assert task['assignee'] is None and task['role'] == 'builder'


def test_history_prompts_and_gui_derive_handles(project):
    cfg, store = project
    store.send('builder-2', 'lead', 'literal builder-2 body')
    store.remember('builder-2', 'Original author')
    store.event('builder-2', 'status', 'builder-2 is ready')
    reopened = Store(cfg.db_path)
    api = TeamAPI(cfg, reopened, 'lead')
    inbox = api.check_inbox()
    assert 'from builder_2@test-project → lead_1@test-project' in inbox
    assert 'literal builder-2 body' in inbox
    assert 'builder_2@test-project' in api.recall('Original author')
    engine = Engine(cfg)
    a = cfg.agent('lead')
    assert 'builder_2@test-project' in engine.system_prompt(a)
    prompt = engine.build_prompt(a, Wake(1, a, 'messages'), store.messages(), None, {})
    assert 'builder_2@test-project' in prompt
    data = Data(cfg)
    data.refresh(force=True)
    assert data.name_of('builder-2') == 'builder_2@test-project'
    assert data.name_of('builder-2', local=True) == 'builder_2'
    assert any(e['text'] == 'builder_2@test-project is ready' for e in data.events)
    assert reopened.messages()[0]['sender'] == 'builder-2'
    assert any(e['text'] == 'builder-2 is ready' for e in reopened.events())
    assert {a['id'] for a in reopened.agents()} == {a.id for a in cfg.agents}


def test_status_prints_handles(project, monkeypatch, capsys):
    import argparse
    from troupe import cli
    cfg, store = project
    store.add_task('Work', assignee='builder-2')
    store.ask('lead', 'A choice?')
    monkeypatch.setattr(cli, 'require_root', lambda: cfg.root)
    cli.cmd_status(argparse.Namespace())
    output = capsys.readouterr().out
    assert 'builder_2@test-project' in output and 'lead_1@test-project' in output
    assert 'builder-2' not in output
