"""Smoke test cho Captcha Broker endpoints."""
import sys
sys.path.insert(0, '.')
from fastapi.testclient import TestClient
from flow2api.main import app
from flow2api.services.captcha_broker import get_captcha_broker

with TestClient(app) as c:
    # 1. /stats không cần auth
    r = c.get('/api/internal/captcha/stats')
    print('[1] stats:', r.status_code, r.json())

    # 2. /secret loopback-only — trong test client, client là loopback
    r = c.get('/api/internal/captcha/secret')
    print('[2] secret:', r.status_code, 'has_secret=', bool(r.json().get('secret')))
    secret = r.json().get('secret', '')

    # 3. /poll no secret → 401
    r = c.get('/api/internal/captcha/poll?centerId=c1&timeout=1')
    print('[3] poll no-secret:', r.status_code, r.json().get('detail'))

    # 4. /poll với secret + timeout ngắn → trả rỗng
    r = c.get('/api/internal/captcha/poll?centerId=c1&label=T1&timeout=1',
              headers={'X-Center-Secret': secret})
    print('[4] poll with-secret:', r.status_code, r.json())

    # 5. /event heartbeat
    r = c.post('/api/internal/captcha/event',
               headers={'X-Center-Secret': secret},
               json={'centerId': 'c1', 'type': 'heartbeat', 'label': 'T1', 'version': '0.1.0'})
    print('[5] event heartbeat:', r.status_code, r.json())

    # 6. stats sau khi c1 heartbeat
    r = c.get('/api/internal/captcha/stats')
    print('[6] stats after heartbeat:', r.status_code)
    data = r.json()
    print('    online_count=', data['online_count'], 'centers=', len(data['centers']))
    for ctr in data['centers']:
        print('   ', ctr['center_id'], 'online=', ctr['online'], 'label=', ctr['label'])

    # 7. request_captcha sẽ chọn c1 và enqueue command
    import asyncio
    broker = get_captcha_broker()

    async def scenario():
        # Simulate center polling
        async def center_poll():
            while True:
                cmds = await broker.poll('c1', label='T1', version='0.1.0', timeout=5.0)
                if cmds:
                    return cmds
        # Producer: bridge request captcha
        async def bridge_request():
            return await broker.request_captcha('IMAGE_GENERATION', bridge_profile_id='bridge-A', timeout=5.0)

        poll_task = asyncio.create_task(center_poll())
        req_task = asyncio.create_task(bridge_request())

        cmds = await poll_task
        print('[7] center got commands:', cmds)
        assert cmds and cmds[0]['method'] == 'get_captcha', cmds
        cid = cmds[0]['commandId']

        # Simulate center solving + returning token
        ok = broker.submit_result(command_id=cid, token='TEST_TOKEN_ABC', error=None, center_id='c1')
        print('[7] submit_result ok=', ok)

        token = await req_task
        print('[7] bridge received token:', token)
        assert token == 'TEST_TOKEN_ABC', token
        return True

    result = asyncio.run(scenario())
    print('[7] scenario OK:', result)

    # 8. Stats sau khi mint
    r = c.get('/api/internal/captcha/stats')
    d = r.json()
    print('[8] final stats: online=', d['online_count'],
          'mint_count=', d['centers'][0]['mint_count'] if d['centers'] else 0)
