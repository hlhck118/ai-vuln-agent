# -*- coding: utf-8 -*-
"""消息通知中心：设置归一化、渠道密文遮蔽、投递记录与重试。

这一组测的都是**接口层**的行为，不发真消息：发消息要么打到外网（不可重复），
要么等 10 秒超时（测试变慢）。发送逻辑由 notify.py 自己的单测覆盖，这里只验证
控制面把什么交出去、又收得回什么。
"""
import json
import unittest

from tests import support
from pentestd import notify


class NotifyCatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store('notify.db')
        self.httpd, self.client = support.start(self.store)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def test_catalog_requires_session(self):
        directory, store = support.temporary_store('notify-anon.db')
        httpd, client = support.start(store, admin=False)
        try:
            status, _payload = client.get('/api/v1/notify')
            self.assertEqual(status, 401)
        finally:
            support.stop(httpd, store, directory)

    def test_catalog_ships_defaults_and_tables(self):
        status, payload = self.client.get('/api/v1/notify')
        self.assertEqual(status, 200)
        settings = payload['settings']
        self.assertTrue(settings['enabled'])
        self.assertEqual(settings['mode'], 'realtime')
        self.assertEqual(settings['digestIntervalS'], 300)
        self.assertIn('finding', settings['events'])
        # 下拉的取值表必须由服务端下发：前端各存一份就会与 evaluate() 的判据漂移。
        kinds = dict((item['id'], item['label']) for item in payload['kinds'])
        self.assertEqual(kinds['webhook'], '通用 Webhook')
        events = [item['id'] for item in payload['events']]
        self.assertEqual(events, ['finding', 'job', 'approval', 'test'])
        self.assertTrue(payload['admin'])
        self.assertEqual(payload['counts']['channels'], 0)

    def test_save_settings_normalizes(self):
        status, payload = self.client.post('/api/v1/notify/settings', {
            'settings': {'enabled': False, 'mode': 'bogus', 'rateLimit': 9999,
                         'digestIntervalS': 5, 'events': ['finding', 'nope'],
                         'includeWords': '安全, 内网',
                         'excludeWords': ['测试']},
        })
        self.assertEqual(status, 200)
        settings = payload['settings']
        self.assertFalse(settings['enabled'])
        # 未知模式回落到 realtime；超出上限按上限收；间隔最小 30 秒。
        self.assertEqual(settings['mode'], 'realtime')
        self.assertEqual(settings['rateLimit'], 600)
        self.assertEqual(settings['digestIntervalS'], 30)
        # 未知事件被丢掉，但不是全空（空表等于『什么都不发』，那是另一种坏法）。
        self.assertEqual(settings['events'], ['finding'])
        self.assertEqual(settings['includeWords'], ['安全', '内网'])
        self.assertEqual(settings['excludeWords'], ['测试'])
        _status, again = self.client.get('/api/v1/notify')
        self.assertFalse(again['settings']['enabled'])


class NotifyChannelTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store('notify-ch.db')
        self.httpd, self.client = support.start(self.store)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def create(self, **overrides):
        body = {'kind': 'webhook', 'label': '安全群',
                'url': 'https://oapi.dingtalk.com/robot/send?access_token=secret-token',
                'extra': {}}
        body.update(overrides)
        return self.client.post('/api/v1/notify/channels', body)

    def test_public_view_masks_url(self):
        status, payload = self.create()
        self.assertEqual(status, 200)
        channel = payload['channel']
        self.assertTrue(channel['url'].startswith('https://oapi.dingtalk.com/'))
        self.assertTrue(channel['url'].endswith('***'))
        self.assertNotIn('secret-token', channel['url'])
        self.assertTrue(channel['hasUrl'])
        self.assertEqual(channel['kindLabel'], '通用 Webhook')

    def test_webhook_without_http_url_is_rejected(self):
        status, payload = self.create(url='oapi.dingtalk.com/robot')
        self.assertEqual(status, 400)
        self.assertIn('http(s)', payload['error'])

    def test_telegram_requires_bot_token(self):
        status, payload = self.client.post('/api/v1/notify/channels', {
            'kind': 'telegram', 'label': 'tg', 'extra': {'chatId': '123'}})
        self.assertEqual(status, 400)
        self.assertIn('bot token', payload['error'])

    def test_editing_with_masked_url_keeps_real_url(self):
        _status, payload = self.create()
        channel = payload['channel']
        status, updated = self.client.post('/api/v1/notify/channels', {
            'id': channel['id'], 'kind': 'webhook', 'label': '改名之后',
            'url': channel['url'],
        })
        self.assertEqual(status, 200)
        self.assertEqual(updated['channel']['label'], '改名之后')
        # 关键：把星号地址照写回库会把能用的地址写成星号，症状是『保存成功但发不出去』。
        stored = self.store.get_notify_channel(channel['id'])
        self.assertTrue(stored['url'].endswith('secret-token'))

    def test_delete_then_missing(self):
        _status, payload = self.create()
        channel_id = payload['channel']['id']
        status, _payload = self.client.post(
            '/api/v1/notify/channels/' + channel_id + '/delete', {})
        self.assertEqual(status, 200)
        status, payload = self.client.post(
            '/api/v1/notify/channels/' + channel_id + '/delete', {})
        self.assertEqual(status, 404)
        self.assertIn(channel_id, payload['error'])

    def test_disabled_channel_survives_roundtrip(self):
        _status, payload = self.create(enabled=False)
        channel = payload['channel']
        self.assertFalse(channel['enabled'])
        _status, catalog = self.client.get('/api/v1/notify')
        self.assertEqual(catalog['counts']['channels'], 1)
        self.assertFalse(catalog['channels'][0]['enabled'])


class NotifyDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store('notify-dl.db')
        self.httpd, self.client = support.start(self.store)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def test_pending_delivery_is_listed_and_counted(self):
        self.store.add_notify_delivery({
            'event': 'finding', 'severity': 'high', 'title': '新发现：SQL 注入',
            'body': '目标：example.test', 'mode': 'realtime', 'status': 'pending',
        })
        status, payload = self.client.get('/api/v1/notify/deliveries?status=pending')
        self.assertEqual(status, 200)
        self.assertEqual(payload['total'], 1)
        row = payload['deliveries'][0]
        self.assertEqual(row['statusLabel'], '待投递')
        self.assertEqual(row['eventLabel'], '新发现')
        self.assertEqual(self.store.count_notify_deliveries(), 1)

    def test_unknown_status_is_rejected(self):
        status, payload = self.client.get('/api/v1/notify/deliveries?status=nope')
        self.assertEqual(status, 400)
        self.assertIn('状态无效', payload['error'])

    def test_retry_against_unreachable_channel_reports_error(self):
        _status, saved = self.client.post('/api/v1/notify/channels', {
            'kind': 'webhook', 'label': '本地测试', 'url': 'http://127.0.0.1:1/x',
            'minSeverity': 'low', 'extra': {}})
        channel_id = saved['channel']['id']
        delivery = self.store.add_notify_delivery({
            'channelId': channel_id, 'event': 'test', 'severity': 'info',
            'title': '测试消息', 'body': '正文', 'mode': 'realtime',
            'status': 'failed', 'attempts': 1, 'error': '上一次失败',
        })
        status, payload = self.client.post(
            '/api/v1/notify/deliveries/' + delivery['id'] + '/retry', {})
        self.assertEqual(status, 200)
        self.assertFalse(payload['ok'])
        self.assertTrue(payload['error'])
        # 失败之后仍是 failed，且 attempts 加一——重试次数是『这条记录的历史』。
        row = self.store.get_notify_delivery(delivery['id'])
        self.assertEqual(row['status'], 'failed')
        self.assertGreaterEqual(row['attempts'], 2)

    def test_digest_without_pending_sends_nothing(self):
        status, payload = self.client.post('/api/v1/notify/digest', {})
        self.assertEqual(status, 200)
        self.assertEqual(payload['sent'], 0)


class NotifyFilterTests(unittest.TestCase):
    """notify.py 的过滤规则：级别、事件、包含/排除词、停用渠道。"""

    CHANNEL = {'kind': 'webhook', 'enabled': True, 'minSeverity': ''}

    def test_min_severity(self):
        settings = notify.normalize_settings({
            'minSeverity': 'high', 'events': ['finding']})
        send, reason = notify.evaluate(settings, self.CHANNEL, 'finding',
                                      {'title': 'x', 'severity': 'medium'})
        self.assertFalse(send)
        self.assertIn('最低级别', reason)
        send, _reason = notify.evaluate(settings, self.CHANNEL, 'finding',
                                       {'title': 'x', 'severity': 'critical'})
        self.assertTrue(send)

    def test_channel_floor_overrides_global(self):
        settings = notify.normalize_settings({'minSeverity': 'low',
                                              'events': ['finding']})
        channel = dict(self.CHANNEL, minSeverity='critical')
        send, _reason = notify.evaluate(settings, channel, 'finding',
                                       {'title': 'x', 'severity': 'high'})
        self.assertFalse(send)

    def test_unsubscribed_event(self):
        settings = notify.normalize_settings({'events': ['job']})
        send, reason = notify.evaluate(settings, self.CHANNEL, 'finding',
                                      {'title': 'x', 'severity': 'high'})
        self.assertFalse(send)
        self.assertIn('未订阅', reason)
        self.assertTrue(notify.evaluate(settings, self.CHANNEL, 'job',
                                        {'title': 'x', 'severity': 'high'})[0])

    def test_include_and_exclude_words(self):
        settings = notify.normalize_settings({
            'events': ['finding'], 'includeWords': ['内网'],
            'excludeWords': ['测试环境']})
        self.assertFalse(notify.evaluate(settings, self.CHANNEL, 'finding',
                                        {'title': '公网', 'severity': 'high',
                                         'body': '其他主机'})[0])
        self.assertTrue(notify.evaluate(settings, self.CHANNEL, 'finding',
                                       {'title': '内网', 'severity': 'high',
                                        'body': '主机'})[0])
        send, reason = notify.evaluate(settings, self.CHANNEL, 'finding',
                                      {'title': '内网', 'severity': 'high',
                                       'body': '测试环境 主机'})
        self.assertFalse(send)
        self.assertIn('排除词', reason)

    def test_disabled_channel_and_unknown_event(self):
        settings = notify.normalize_settings({})
        off = dict(self.CHANNEL, enabled=False)
        self.assertFalse(notify.evaluate(settings, off, 'finding',
                                        {'title': 'x', 'severity': 'high'})[0])
        send, reason = notify.evaluate(settings, self.CHANNEL, 'whatever',
                                      {'title': 'x', 'severity': 'info'})
        self.assertFalse(send)
        self.assertIn('未知事件', reason)

class NotifySendTests(unittest.TestCase):
    """发送这一段用**注入的传输**测：组包逻辑与生产共用，但不真的发出去。"""

    def setUp(self):
        self.sent = []
        self.directory, self.store = support.temporary_store('notify-send.db')

        def fake(url, body, headers, timeout):
            self.sent.append({'url': url, 'body': body, 'headers': headers})
            return 200, ''

        notify.set_transport(fake)

    def tearDown(self):
        notify.set_transport(None)
        self.store.close()
        self.directory.cleanup()

    def test_webhook_payload_carries_title_and_text(self):
        channel = {'kind': 'webhook', 'enabled': True, 'minSeverity': '',
                   'url': 'https://example.test/hook', 'extra': {}}
        ok, detail = notify.send(channel, 'finding', {
            'title': 'SQL 注入', 'body': '目标：example.test', 'severity': 'high'})
        self.assertTrue(ok, detail)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]['url'], 'https://example.test/hook')
        payload = json.loads(self.sent[0]['body'].decode('utf-8'))
        self.assertIn('SQL 注入', payload.get('text', ''))

    def test_dingtalk_gets_markdown_envelope(self):
        channel = {'kind': 'dingtalk', 'enabled': True, 'minSeverity': '',
                   'url': 'https://oapi.dingtalk.com/robot/send?access_token=x', 'extra': {}}
        self.assertTrue(notify.send(channel, 'finding',
                                    {'title': 't', 'body': 'b', 'severity': 'high'})[0])
        body = json.loads(self.sent[0]['body'].decode('utf-8'))
        self.assertEqual(body.get('msgtype'), 'markdown')
        self.assertIn('### ', body['markdown']['text'])

    def test_telegram_puts_text_in_chat(self):
        channel = {'kind': 'telegram', 'enabled': True, 'minSeverity': '',
                   'url': '', 'secret': 'bot-token', 'extra': {'chatId': '42'}}
        self.assertTrue(notify.send(channel, 'finding',
                                    {'title': 't', 'body': 'b', 'severity': 'low'})[0])
        self.assertIn('/botbot-token/sendMessage', self.sent[0]['url'])
        body = json.loads(self.sent[0]['body'].decode('utf-8'))
        self.assertEqual(body.get('chat_id'), '42')

    def test_dispatch_blocking_records_a_sent_row(self):
        self.store.save_notify_settings(notify.normalize_settings({
            'enabled': True, 'events': ['finding'], 'minSeverity': 'low'}))
        self.store.save_notify_channel(notify.normalize_channel({
            'kind': 'webhook', 'label': 'hook',
            'url': 'https://example.test/hook'}))
        created = notify.dispatch(self.store, 'finding',
                                  {'title': '发现', 'body': '正文',
                                   'severity': 'high'}, blocking=True)
        self.assertEqual(len(created), 1)
        row = self.store.get_notify_delivery(created[0]['id'])
        self.assertEqual(row['status'], 'sent')
        self.assertEqual(row['attempts'], 1)
        payload = json.loads(self.sent[0]['body'].decode('utf-8'))
        self.assertIn('发现', payload.get('text', ''))

    def test_dispatch_off_writes_nothing(self):
        self.store.save_notify_settings(notify.normalize_settings({'enabled': False}))
        self.store.save_notify_channel(notify.normalize_channel({
            'kind': 'webhook', 'label': 'hook', 'url': 'https://example.test/hook'}))
        self.assertEqual([], notify.dispatch(self.store, 'finding',
                                            {'title': 'x', 'severity': 'high'}))
        self.assertEqual(0, self.store.count_notify_deliveries())


if __name__ == '__main__':
    unittest.main()