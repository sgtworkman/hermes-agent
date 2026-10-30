"""Keep the native primary WebSocket usable across repeated profile restarts, without redialling."""
import asyncio
import json

import pytest
from websockets.asyncio.client import connect

from tests.gateway.test_multiplex_hot_serve_live import control, start_daemon, stop, tail

pytestmark = [pytest.mark.platforms('posix'), pytest.mark.spawns_gateway_lookalike]
pytest_plugins = ['tests.gateway.test_multiplex_hot_serve_live']


def test_primary_socket_survives_profile_restarts_and_completes_a_turn(mux, peer):
    proc, description = start_daemon(mux)

    async def probe():
        home = mux['boot']
        grant = control(mux['root'], 'session-ticket', {
            'profile_id': str(home.resolve()), 'instance_id': description['instance_id'],
            'purpose': 'interactive', 'scope': 'host'})
        url = description['api_origin'].replace('http:', 'ws:') + '/api/ws'
        protocols = ['hermes-gateway-v1', 'hermes-gateway-ticket.' + grant['ticket']]
        async with connect(url, subprotocols=protocols, proxy=None) as ws:
            sequence, events = 0, []

            async def rpc(method, **params):
                nonlocal sequence
                sequence += 1
                await ws.send(json.dumps({'jsonrpc': '2.0', 'id': sequence,
                                          'method': method, 'params': params}))
                async with asyncio.timeout(30):
                    while True:
                        frame = json.loads(await ws.recv())
                        if frame.get('id') == sequence:
                            assert 'result' in frame, (frame, tail(mux))
                            return frame['result']
                        events.append(frame)

            first = await rpc('session.create', request_id='before', source='gui', toolsets=[])
            sid = first['session_id']
            epoch = first['authority_epoch']
            for restart in range(2):
                unserved = await asyncio.to_thread(control, mux['root'], 'unserve-profile', {'name': 'boot'})
                assert unserved['unserved'] == 'boot', unserved
                served = await asyncio.to_thread(control, mux['root'], 'serve-profile', {'name': 'boot'})
                assert served['served'] == 'boot', served
                resumed = await rpc('session.resume', session_id=sid)
                assert resumed['session_id'] == sid
                assert resumed['authority_epoch'] > epoch
                epoch = resumed['authority_epoch']
                created = await rpc('session.create', request_id=f'after-{restart}', source='gui', toolsets=[])
                assert created['authority_epoch'] == epoch

            admission = await rpc('prompt.submit', session_id=sid, input_id='after-restarts', text='WS_SHARED')
            async with asyncio.timeout(45):
                while not any(frame.get('params', {}).get('type') == 'message.complete' for frame in events):
                    events.append(json.loads(await ws.recv()))
            receipt = await rpc('prompt.receipt', session_id=sid,
                                admission_id=admission['admission_id'], include_result=True)
            assert receipt['status'] == 'terminal' and receipt['outcome'] == 'completed', receipt
            assert receipt['result']['final_response'] == 'LOCAL_ACK_WS_SHARED', receipt

    try:
        asyncio.run(probe())
        assert proc.poll() is None
        assert any('WS_SHARED' in json.dumps(request) for request in peer.requests)
    finally:
        stop(proc, expect=None)
