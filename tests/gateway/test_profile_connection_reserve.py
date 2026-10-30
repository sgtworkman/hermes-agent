"""A profile's own live socket must follow its replacement authority after a hot restart."""
from pathlib import Path

import pytest

from tests.gateway.test_session_authorities_multiplex import _reserve_homes, _runner
from tests.gateway.test_ticket_profile_scope import _Transport


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', ['profile', 'host'])
@pytest.mark.parametrize('selector', [False, True])
async def test_primary_socket_rejoins_its_profile_after_hot_restart(tmp_path, monkeypatch, scope, selector):
    from gateway.run_runtime import initialize_gateway_runtime, serve_profile_runtime, unserve_profile_runtime
    from gateway.runtime_ownership import process_ownership
    from gateway.session_authorities import owner_scope
    from gateway.session_controls import AuthorityConnection

    root, homes = _reserve_homes(tmp_path, monkeypatch)
    process_ownership.reserve([home for _, home in homes])
    home = homes[1][1]
    retired, connection, runner = [], None, None
    try:
        runner = _runner(root, homes)
        await initialize_gateway_runtime(runner)
        authority = runner.session_authorities.for_home(home)
        store = runner.session_ticket_store
        ticket = store.mint(profile_id=str(home.resolve()), subject='uid:desk', purpose='interactive', scope=scope)
        grant = store.redeem(ticket, profile_id=None, purpose='interactive')
        identity = {'user_id': grant['subject'], 'provider': 'local', 'profile_id': grant['profile_id'],
                    'instance_id': grant['instance_id'], 'capabilities': grant['capabilities'],
                    'native_bootstrap': True, 'profile_scope': scope}
        with owner_scope(authority):
            connection = AuthorityConnection(authority, _Transport(), identity, operator=True)

        async def call(method, **params):
            if selector:
                params['profile'] = 'alpha'
            with owner_scope(authority):
                return await connection.dispatch({'id': 1, 'method': method, 'params': params})

        first = await call('session.create', request_id='before', source='gui', cwd=str(tmp_path),
                           model='m', toolsets=[])
        assert 'result' in first, first
        sid = first['result']['session_id']
        assert await unserve_profile_runtime(runner, home) is True
        runner._profile_adapters.pop('alpha', None)
        retired.append(authority)
        successor = await serve_profile_runtime(runner, 'alpha', home)
        assert successor is not authority

        resumed = await call('session.resume', session_id=sid)
        assert 'result' in resumed, resumed
        assert resumed['result']['session_id'] == sid
        assert resumed['result']['authority_epoch'] == successor.epoch
        created = await call('session.create', request_id='after', source='gui', cwd=str(tmp_path),
                             model='m', toolsets=[])
        assert 'result' in created, created
        assert created['result']['session_id'] in successor.sessions
        if scope == 'profile':
            foreign = await connection.dispatch({'id': 2, 'method': 'session.list', 'params': {'profile': 'beta'}})
            assert foreign['error']['message'] == 'profile_mismatch', foreign
    finally:
        if connection is not None:
            await connection.close()
        for authority in [*retired, *(getattr(runner, 'session_authorities', None) or [])]:
            authority.db.close()
        for _, profile_home in homes:
            process_ownership.release(Path(profile_home))
