import datetime as dt
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import dorm_points
import proxy_rules
from desktop_bridge import DesktopBridge, PreviewBridge
from campus_auth_gui import GuiSettings
from dorm_accounts import DormAccounts
from dorm_checkin import Result, Settings, Store, Task
from dorm_location import map_pick_sample


def _at(value):
    return dt.datetime.fromisoformat(value)


class FakeController:
    """账号层里的控制器替身：只关心「哪个档案目录、错峰多少、是否在忙」，不碰学校接口。"""

    def __init__(self, profile, stagger_seconds=0):
        self.store = Store(Path(profile))
        self.stagger_seconds = stagger_seconds
        self.latest = Result('idle', '尚未查询今日任务')
        self.busy = False
        self.closed = False
        self.cancelled = False
        self.polled = 0
        self.results = []

    def poll(self):
        self.polled += 1
        return True

    def start(self, action):
        self.started.append(action)
        return True

    def save(self, settings):
        self.store.save_settings(settings)

    def cancel(self):
        self.cancelled = True

    def drain(self):
        values, self.results = self.results, []
        if values:
            self.latest = values[-1]
        return values

    def schedule_text(self):
        return '自动打卡：关闭'

    def close(self):
        self.closed = True


class DesktopBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.controller = MagicMock()
        self.controller.latest = Result('idle', '尚未查询今日任务')
        self.controller.busy = False
        self.controller.store.settings.return_value = Settings()
        self.controller.store.root = Path(self.tmp.name) / 'dorm'
        self.controller.store.history.return_value = ''
        self.controller.schedule_text.return_value = '自动打卡：关闭'
        self.controller.drain.return_value = []
        self.patches = [
            patch('desktop_bridge.gui.ensure_user_config', side_effect=lambda p: p),
            patch('desktop_bridge.gui.load_gui_settings', return_value=GuiSettings('student', 'never-expose', 60)),
            patch('desktop_bridge.gui.is_startup_enabled', return_value=False),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.bridge = DesktopBridge(Path(self.tmp.name)/'config.ini', dorm=self.controller)
        self.addCleanup(self.bridge._close)

    def test_snapshot_excludes_secrets(self):
        state = self.bridge.snapshot()
        self.assertEqual(state['network']['username'], 'student')
        self.assertNotIn('password', state['network'])
        self.assertNotIn('never-expose', str(state))
        self.controller.store.token.assert_not_called()
        # 登录态只能来自「密文文件在不在」，绝不能为了把状态说准去解密会话令牌。
        self.assertTrue(self.controller.store.has_session.called)
        self.assertIsInstance(state['dorm']['has_session'], bool)

    def test_dorm_login_state_degrades_instead_of_failing_the_snapshot(self):
        """会话文件读不动时只降级这一小块：不能把「读不出登录态」变成「后台连不上」。"""
        self.controller.store.has_session.side_effect = OSError('磁盘读不动')
        state = self.bridge.snapshot()
        self.assertIs(state['dorm']['has_session'], False)
        self.assertEqual(state['network']['username'], 'student')

    def test_proxy_section_is_present_and_shaped_for_the_ui(self):
        state = self.bridge.snapshot()
        proxy = state['proxy']
        for key in ('found', 'client', 'file', 'missing', 'pending', 'ok',
                    'blocked_by', 'message', 'can_apply', 'needs_reload', 'rules'):
            self.assertIn(key, proxy)
        self.assertIsInstance(proxy['message'], str)

    def test_a_broken_proxy_config_does_not_break_the_panel(self):
        """读不动别人的代理配置，只能是这块降级，绝不能让整个面板变成「后台连不上」。"""
        import proxy_rules

        with patch('desktop_bridge.proxy_rules.inspect', side_effect=OSError('读取被拒绝')):
            self.bridge._proxy_cache = None
            proxy = self.bridge.snapshot()['proxy']

        self.assertFalse(proxy['found'])
        self.assertFalse(proxy['can_apply'])
        self.assertEqual(proxy['missing'], [])
        self.assertEqual(proxy['blocked_by'], '代理配置无法读取')

    def test_apply_is_refused_when_there_is_nothing_to_write(self):
        import proxy_rules

        clean = proxy_rules.ProxyRuleReport(
            client='clash-verge-rev', root=Path(self.tmp.name), profile='p',
            rules_file=Path(self.tmp.name) / 'RULES01.yaml',
        )
        with patch('desktop_bridge.proxy_rules.inspect', return_value=clean):
            self.bridge._proxy_cache = None
            result = self.bridge.dispatch('proxy_rules_apply')

        self.assertTrue(result['ok'])
        self.assertIn('already go DIRECT', result['message'])

    def test_apply_writes_and_says_a_reload_is_needed(self):
        import proxy_rules

        needs_rule = proxy_rules.ProxyRuleReport(
            client='clash-verge-rev', root=Path(self.tmp.name), profile='p',
            rules_file=Path(self.tmp.name) / 'RULES01.yaml',
            missing_hosts=proxy_rules.NCSI_SUFFIXES,
        )
        pending = proxy_rules.ProxyRuleReport(
            client='clash-verge-rev', root=Path(self.tmp.name), profile='p',
            rules_file=Path(self.tmp.name) / 'RULES01.yaml',
            pending_hosts=proxy_rules.NCSI_SUFFIXES,
        )
        written = Path(self.tmp.name) / 'RULES01.yaml'
        with patch('desktop_bridge.proxy_rules.inspect', side_effect=[needs_rule, pending]), \
                patch('desktop_bridge.proxy_rules.apply', return_value=written) as apply:
            self.bridge._proxy_cache = None
            result = self.bridge.dispatch('proxy_rules_apply')

        apply.assert_called_once()
        self.assertTrue(result['ok'])
        # Written but not in the running config: the user has to reload Clash.
        self.assertIn('重新加载', result['message'])

    def test_apply_reports_nothing_to_do_without_a_rules_extension(self):
        import proxy_rules

        blocked = proxy_rules.ProxyRuleReport(
            client='clash-verge-rev', root=Path(self.tmp.name),
            missing_hosts=proxy_rules.NCSI_SUFFIXES,
            blocked_by='the current profile has no Rules extension',
        )
        with patch('desktop_bridge.proxy_rules.inspect', return_value=blocked):
            self.bridge._proxy_cache = None
            result = self.bridge.dispatch('proxy_rules_apply')

        self.assertFalse(result['ok'])
        self.assertIn('Rules extension', result['message'])

    def test_blank_password_is_preserved_by_existing_storage_contract(self):
        with patch('desktop_bridge.gui.save_gui_settings') as save:
            result = self.bridge.dispatch('network_save', {'username':'student', 'password':'', 'interval':90, 'startup':False})
        self.assertTrue(result['ok'])
        self.assertEqual(save.call_args.args[1].password, '')
        self.assertEqual(save.call_args.args[1].check_interval_seconds, 90)

    def test_update_check_asks_the_privileged_agent_and_never_downloads_here(self):
        state = self.bridge.snapshot()
        self.assertIn('update', state)
        self.assertEqual(state['update']['state'], 'idle')
        # 界面请求检查 = 向 agent 发一个不带参数的信号；桌面进程自己不再下载任何东西。
        with patch.object(self.bridge, '_agent_command') as command:
            self.bridge._agent = True
            self.assertTrue(self.bridge.dispatch('update_check')['ok'])
            command.assert_called_once_with('check-update')
        with patch.object(self.bridge._updates, 'check') as check:
            self.bridge._agent = True
            self.bridge.dispatch('update_check')
            check.assert_not_called()
        self.controller.start.assert_not_called()
        self.controller.poll.assert_not_called()

    def test_the_desktop_process_has_no_install_action_at_all(self):
        # 关键安全性质：未提权的界面**不能**触发安装。它连这个动作都没有，
        # 所以不存在「界面把包交给提权端」这条本地提权通道。
        self.assertFalse(self.bridge.dispatch('update_install', {'confirmed': True, 'version': '1.5.0'})['ok'])
        with patch.object(self.bridge._updates, 'install') as install:
            self.bridge.dispatch('update_install', {'confirmed': True, 'version': '1.5.0'})
            install.assert_not_called()

    def test_an_update_check_without_a_running_agent_is_refused(self):
        self.bridge._agent = False
        result = self.bridge.dispatch('update_check')
        self.assertFalse(result['ok'])
        self.assertIn('后台', result['message'])

    def test_the_interface_adopts_the_agents_update_status(self):
        accepted = self.bridge._updates.adopt({
            'state': 'installing', 'current_version': '1.8.4', 'latest_version': '1.9.0',
            'progress': 100, 'checked': '2026-10-10 18:00', 'message': '正在后台静默安装…',
            'detail': '', 'changes': {},
        })
        self.assertTrue(accepted)
        state = self.bridge.snapshot()['update']
        self.assertEqual(state['state'], 'installing')
        self.assertEqual(state['latest_version'], '1.9.0')
        # 形状不对或状态词不在词表里的一律拒绝：快照是外部数据。
        self.assertFalse(self.bridge._updates.adopt({'state': 'give-me-admin'}))
        self.assertFalse(self.bridge._updates.adopt({'state': 'idle', 'evil': 1}))

    def test_a_read_only_controller_refuses_to_check_or_install(self):
        # 界面这条控制器是只读的：真要有代码尝试让它自己下载，会明确失败而不是
        # 悄悄开始第二次互相打架的检查。
        with self.assertRaises(RuntimeError):
            self.bridge._updates.check()
        with self.assertRaises(RuntimeError):
            self.bridge._updates.install(True, '1.5.0')

    def test_losing_the_agent_clears_a_ready_update(self):
        self.bridge._updates.adopt({
            'state': 'ready', 'current_version': '1.8.4', 'latest_version': '1.9.0',
            'progress': 100, 'checked': '', 'message': '已下载', 'detail': '', 'changes': {},
        })
        self.assertEqual(self.bridge.snapshot()['update']['state'], 'ready')
        self.bridge._updates.disconnect()
        state = self.bridge.snapshot()['update']
        self.assertEqual(state['state'], 'idle')
        self.assertEqual(state['latest_version'], '')
        self.assertIn('后台', state['message'])

    def test_preview_update_download_is_synthetic_and_install_never_opens_windows(self):
        with patch('desktop_bridge.UpdateController') as controller:
            preview = PreviewBridge()
            self.assertEqual(preview.snapshot()['update']['state'], 'idle')
            with patch('desktop_bridge.time.monotonic', return_value=100):
                self.assertTrue(preview.dispatch('update_check')['ok'])
                self.assertEqual(preview.snapshot()['update']['state'], 'downloading')
            with patch('desktop_bridge.time.monotonic', return_value=110):
                update = preview.snapshot()['update']
                self.assertEqual(update['state'], 'ready')
            self.assertFalse(preview.dispatch('update_install', {'version': update['latest_version']})['ok'])
            self.assertTrue(preview.dispatch('update_install', {'confirmed': True, 'version': update['latest_version']})['ok'])
            self.assertEqual(preview.snapshot()['update']['state'], 'launched')
            controller.assert_not_called()

    def test_unknown_action_is_rejected(self):
        self.assertFalse(self.bridge.dispatch('__dict__', {})['ok'])
        self.controller.start.assert_not_called()

    def test_invalid_dorm_schedule_does_not_save(self):
        result = self.bridge.dispatch('dorm_save', {'enabled':True,'start':'23:30','end':'21:00','interval':300})
        self.assertFalse(result['ok'])
        self.controller.save.assert_not_called()

    def test_navigation_snapshot_does_not_submit_or_poll(self):
        self.bridge.snapshot()
        self.controller.start.assert_not_called()
        self.controller.poll.assert_not_called()

    def test_concurrent_network_operation_is_rejected(self):
        self.bridge._network_gate.acquire()
        try:
            result = self.bridge.dispatch('network_check', {})
            self.assertFalse(result['ok'])
        finally:
            self.bridge._network_gate.release()

    def test_dorm_busy_rejection_reaches_frontend(self):
        self.controller.start.return_value = False
        self.assertFalse(self.bridge.dispatch('dorm_query', {})['ok'])

    def test_preview_uses_no_real_controller_and_no_external_side_effects(self):
        with patch('desktop_bridge.DormController') as real, patch('desktop_bridge.gui.save_gui_settings') as save:
            preview = PreviewBridge()
            self.assertTrue(preview.snapshot()['preview'])
            self.assertTrue(preview.dispatch('dorm_submit', {})['ok'])
            self.assertEqual(preview.snapshot()['dorm']['state'], 'signed')
            real.assert_not_called()
            save.assert_not_called()

    def test_location_authorization_does_not_start_school_operation(self):
        self.bridge._ui_dispatch = MagicMock()
        result = {'state':'ready','message':'定位正常','accuracy':141,'checked':'21:00:00'}
        with patch('desktop_bridge.probe_location', return_value=result) as probe:
            self.assertTrue(self.bridge.dispatch('location_authorize')['ok'])
            self.bridge._location_worker.join(2)
        probe.assert_called_once_with(ui_dispatch=self.bridge._ui_dispatch, source='windows',
                                      sample_path=self.controller.store.root / 'location-sample.json',
                                      label='')
        self.controller.start.assert_not_called()
        self.assertEqual(self.bridge.snapshot()['location']['state'], 'ready')

    def test_location_authorization_requires_foreground_desktop_dispatcher(self):
        self.assertFalse(self.bridge.dispatch('location_authorize')['ok'])

    def test_regular_preview_never_calls_real_location(self):
        with patch('desktop_bridge.probe_location') as probe:
            preview = PreviewBridge()
            self.assertTrue(preview.dispatch('location_authorize')['ok'])
            self.assertEqual(preview.snapshot()['location']['state'], 'ready')
            probe.assert_not_called()

    def test_saved_source_resets_previous_probe_and_applies_to_detection(self):
        self.controller.store = Store(Path(self.tmp.name) / 'dorm')
        self.controller.save.side_effect = self.controller.store.save_settings
        self.bridge._location_state = dict(state='ready', message='旧检测', accuracy=50, checked='21:00')
        payload = dict(enabled=False, start='21:00', end='23:30', interval=300, location_source='simulation')
        self.assertTrue(self.bridge.dispatch('dorm_save', payload)['ok'])
        self.assertEqual(getattr(self.controller.store.settings(), 'location_source', None), 'simulation')
        self.assertEqual(self.bridge.snapshot()['location']['state'], 'idle')
        sample_path = self.controller.store.root / 'location-sample.json'
        sample_path.write_text(json.dumps(dict(latitude=39.908823, longitude=116.397470,
                                               accuracy=141, timestamp=1700000000, source='WI_FI')), encoding='utf-8')
        with (patch('dorm_location.request_permission', side_effect=AssertionError('No simulated permission')),
              patch('dorm_location.read_position', side_effect=AssertionError('No simulated live position'))):
            self.assertTrue(self.bridge.dispatch('location_authorize')['ok'])
            self.bridge._location_worker.join(2)
        state = self.bridge.snapshot()['location']
        self.assertEqual(state['state'], 'ready')
        self.assertAlmostEqual(state['accuracy'], 141, delta=141 * 0.15)
        self.assertIn('模拟', state['message'])
        self.assertNotIn('latitude', state)
        self.controller.start.assert_not_called()

    def test_source_cannot_change_during_location_probe(self):
        self.bridge._location_gate.acquire()
        try:
            payload = dict(enabled=False, start='21:00', end='23:30', interval=300, location_source='simulation')
            self.assertFalse(self.bridge.dispatch('dorm_save', payload)['ok'])
            self.controller.save.assert_not_called()
        finally:
            self.bridge._location_gate.release()

    def test_unknown_location_source_does_not_save(self):
        payload = dict(enabled=False, start='21:00', end='23:30', interval=300, location_source='unknown')
        self.assertFalse(self.bridge.dispatch('dorm_save', payload)['ok'])
        self.controller.save.assert_not_called()

    def test_location_source_save_keeps_the_saved_schedule(self):
        self.controller.store = Store(Path(self.tmp.name) / 'dorm')
        self.controller.save.side_effect = self.controller.store.save_settings
        self.controller.store.save_settings(Settings(enabled=True, start='22:00', end='23:00', interval=600))
        result = self.bridge.dispatch('location_source_save', {'location_source': 'simulation'})
        self.assertTrue(result['ok'], result['message'])
        self.assertEqual(result['message'], '定位来源已切换为模拟定位（非实时）')
        saved = self.controller.store.settings()
        self.assertEqual(saved.location_source, 'simulation')
        self.assertEqual((saved.enabled, saved.start, saved.end, saved.interval),
                         (True, '22:00', '23:00', 600))
        self.assertEqual(self.bridge.snapshot()['location']['state'], 'idle')

    def test_location_source_save_rejects_unknown_sources(self):
        self.controller.store = Store(Path(self.tmp.name) / 'dorm')
        self.controller.save.side_effect = self.controller.store.save_settings
        self.assertFalse(self.bridge.dispatch('location_source_save', {'location_source': 'unknown'})['ok'])
        self.controller.save.assert_not_called()
        self.assertEqual(self.controller.store.settings().location_source, 'windows')

    def test_location_source_save_is_rejected_while_a_probe_runs(self):
        self.bridge._location_gate.acquire()
        try:
            self.assertFalse(self.bridge.dispatch('location_source_save', {'location_source': 'simulation'})['ok'])
            self.controller.save.assert_not_called()
        finally:
            self.bridge._location_gate.release()

    def test_preview_location_source_save_switches_without_touching_the_schedule(self):
        preview = PreviewBridge()
        payload = dict(enabled=False, start='21:00', end='23:30', interval=300, location_source='windows')
        self.assertTrue(preview.dispatch('dorm_save', payload)['ok'])
        self.assertTrue(preview.dispatch('location_source_save', {'location_source': 'simulation'})['ok'])
        settings = preview.snapshot()['dorm']['settings']
        self.assertEqual(settings['location_source'], 'simulation')
        self.assertEqual(settings['interval'], 300)
        self.assertIn('模拟', preview.snapshot()['location']['message'])
        self.assertFalse(preview.dispatch('location_source_save', {'location_source': 'unknown'})['ok'])
        self.assertEqual(preview.snapshot()['dorm']['settings']['location_source'], 'simulation')

    def test_preview_bridge_offers_the_proxy_hint_without_touching_anything(self):
        preview = PreviewBridge()
        proxy = preview.snapshot()['proxy']

        # 演示必须能看见这条提示，但绝不能真的去读写别人的代理配置。
        self.assertTrue(proxy['can_apply'])
        self.assertEqual(proxy['missing'], list(proxy_rules.NCSI_SUFFIXES))

        self.assertTrue(preview.dispatch('proxy_rules_apply')['ok'])
        self.assertFalse(preview.snapshot()['proxy']['can_apply'])
        # 再点一次就没有可写的了，不能重复「补写」。
        self.assertFalse(preview.dispatch('proxy_rules_apply')['ok'])

    def test_valid_save_can_recover_corrupt_settings(self):
        self.controller.store = Store(Path(self.tmp.name))
        self.controller.save.side_effect = self.controller.store.save_settings
        (self.controller.store.root / 'settings.json').write_text('{', encoding='utf-8')
        payload = dict(enabled=False, start='21:00', end='23:30', interval=300, location_source='windows')
        self.assertTrue(self.bridge.dispatch('dorm_save', payload)['ok'])
        self.assertEqual(self.controller.store.settings().location_source, 'windows')

    def test_preview_uses_saved_source_without_reading_real_samples(self):
        preview = PreviewBridge(real_location=True)
        payload = dict(enabled=False, start='21:00', end='23:30', interval=300, location_source='simulation')
        self.assertTrue(preview.dispatch('dorm_save', payload)['ok'])
        self.assertEqual(preview.snapshot()['dorm']['settings'].get('location_source'), 'simulation')
        with patch('desktop_bridge.probe_location', side_effect=AssertionError('Preview cannot read local samples')):
            self.assertTrue(preview.dispatch('location_authorize')['ok'])
        self.assertIn('模拟', preview.snapshot()['location']['message'])
        payload['location_source'] = 'windows'
        self.assertTrue(preview.dispatch('dorm_save', payload)['ok'])
        self.assertEqual(preview.snapshot()['location']['state'], 'idle')

class SimulationMapTests(unittest.TestCase):
    """The map picker end of the bridge: model, save, undo and the coordinate-exposure rule."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'dorm',
                           protector=MagicMock(protect=lambda b: b[::-1], unprotect=lambda b: b[::-1]))
        self.store.save_settings(Settings(location_source='simulation'))
        task = Task('task-1', 'form-1', 'publish-1', 'student', '2026-09-21', '每日查寝', '21:00',
                    '23:30', False, '重庆市北碚区天生街道2号', '800米', 'dorm-form',
                    '29.821186', '106.426239')
        self.controller = MagicMock()
        self.controller.latest = Result('ready', '今日任务待完成', task)
        self.controller.busy = False
        self.controller.store = self.store
        self.controller.schedule_text.return_value = '自动打卡：关闭'
        self.controller.drain.return_value = []
        # An established store: the list exists, so the shipped starter positions are not seeded
        # (that behaviour has its own test) and these tests see a clean slate.
        self.store.save_points(dorm_points.empty())
        self.patches = [
            patch('desktop_bridge.gui.ensure_user_config', side_effect=lambda p: p),
            patch('desktop_bridge.gui.load_gui_settings', return_value=GuiSettings('student', '', 60)),
            patch('desktop_bridge.gui.is_startup_enabled', return_value=False),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.bridge = DesktopBridge(Path(self.tmp.name) / 'config.ini', dorm=self.controller)
        self.addCleanup(self.bridge._close)
        self.sample_path = self.store.root / 'location-sample.json'

    def test_the_model_reports_the_saved_point_and_the_school_reference(self):
        self.store.save_sample(map_pick_sample(29.823693, 106.422310))
        model = self.bridge.simulation_map()
        self.assertTrue(model['ok'])
        self.assertAlmostEqual(model['point']['latitude'], 29.823693, places=6)
        self.assertTrue(model['point']['picked'])
        self.assertEqual(model['reference']['address'], '重庆市北碚区天生街道2号')
        self.assertEqual(model['reference']['radius_m'], 800.0)
        self.assertLess(model['distance_m'], 40.0)
        self.assertTrue(model['in_range'])
        self.assertIn('openstreetmap.org', model['tile_url'])
        self.assertIn('OpenStreetMap', model['attribution'])

    def test_the_model_offers_the_basemap_fallbacks_in_order(self):
        """The UI walks this list: preferred first, then whatever can still draw the campus."""
        model = self.bridge.simulation_map()
        providers = model['providers']
        self.assertEqual([entry['id'] for entry in providers], ['osm', 'amap'])
        self.assertEqual(providers[0]['url'], model['tile_url'],
                         'the legacy single-provider fields must describe the first entry')
        self.assertEqual(providers[0]['max_zoom'], model['max_zoom'])
        self.assertEqual(providers[0]['crs'], 'wgs84')
        for entry in providers:
            for placeholder in ('{z}', '{x}', '{y}'):
                self.assertIn(placeholder, entry['url'])
            self.assertTrue(entry['attribution'], 'every provider needs a credit on the canvas')
            self.assertIsInstance(entry['max_zoom'], int)
        fallback = providers[1]
        self.assertEqual(fallback['crs'], 'gcj02', 'a GCJ02 basemap needs the offset the UI applies')
        self.assertEqual(fallback['max_zoom'], 18, 'the fallback serves nothing past zoom 18')

    def test_every_provider_host_is_a_named_https_origin(self):
        """The UI's CSP has to list these hosts, so a templated or relative host would break it."""
        from urllib.parse import urlsplit
        for entry in self.bridge.simulation_map()['providers']:
            parts = urlsplit(entry['url'])
            self.assertEqual(parts.scheme, 'https', entry['id'])
            self.assertTrue(parts.hostname and '.' in parts.hostname,
                            f'{entry["id"]} needs a host the policy can name literally')
            self.assertNotIn('{', parts.hostname, 'the host itself must never be templated')

    def test_the_provider_list_is_a_copy_the_ui_cannot_corrupt(self):
        model = self.bridge.simulation_map()
        model['providers'][0]['url'] = 'https://example.invalid/{z}/{x}/{y}.png'
        self.assertEqual(self.bridge.simulation_map()['providers'][0]['url'],
                         'https://tile.openstreetmap.org/{z}/{x}/{y}.png')

    def test_the_model_is_refused_while_the_saved_source_is_real_location(self):
        self.store.save_settings(Settings(location_source='windows'))
        model = self.bridge.simulation_map()
        self.assertFalse(model['ok'])
        self.assertIn('模拟定位', model['message'])

    def test_a_captured_sample_is_adopted_into_the_list_and_kept(self):
        """The original Windows sample must stay visible and selectable, never be overwritten."""
        self.store.save_sample(map_pick_sample(29.800000, 106.400000, accuracy=141.0))
        model = self.bridge.simulation_map()
        self.assertEqual([point['name'] for point in model['points']], ['本机采样样本'])
        self.assertEqual(model['points'][0]['source'], 'SAMPLE')
        self.assertEqual(model['active_id'], model['points'][0]['id'])
        self.assertAlmostEqual(model['point']['latitude'], 29.800000, places=6)

        self.bridge.dispatch('simulation_point_save', {'latitude': 29.823693, 'longitude': 106.422310,
                                                        'name': '李园一舍'})
        model = self.bridge.simulation_map()
        self.assertEqual([point['name'] for point in model['points']], ['本机采样样本', '李园一舍'])
        self.assertAlmostEqual(self.store.sample()['latitude'], 29.823693, places=6)

        # Switching back restores the original sample, so nothing was destroyed by saving a pick.
        original = model['points'][0]['id']
        self.assertTrue(self.bridge.dispatch('simulation_point_select', {'id': original})['ok'])
        self.assertAlmostEqual(self.store.sample()['latitude'], 29.800000, places=6)
        self.assertEqual(self.store.sample()['source'], 'SAMPLE',
                         'the adopted point keeps its provenance, and the payload still says windows')
        from dorm_location import locate
        self.assertEqual(locate('simulation', self.sample_path)['provider'], 'windows')

    def test_the_list_heals_a_sample_that_disagrees_with_it(self):
        """1.5.2 could leave the sample and the list out of step; the list must win."""
        self.store.save_points(dorm_points.add(
            dorm_points.empty(), name='李园一舍', latitude=29.827439, longitude=106.422612,
            accuracy=100.0, saved_at='2026-09-24T10:01:02+08:00'))
        self.store.save_sample(map_pick_sample(29.823693, 106.422310))
        (self.store.root / 'location-sample.previous.json').write_text('{}', encoding='utf-8')
        model = self.bridge.simulation_map()
        names = [point['name'] for point in model['points']]
        self.assertIn('李园一舍', names)
        self.assertIn('本机采样样本', names, 'the sample the program actually replays is adopted')
        self.assertEqual(names[model['points'].index(next(p for p in model['points'] if p['active']))],
                         '本机采样样本')
        self.assertFalse((self.store.root / 'location-sample.previous.json').exists(),
                         'the legacy undo file is dropped')
        self.assertAlmostEqual(self.store.sample()['latitude'], 29.823693, places=6)

    def test_the_legacy_undo_file_is_adopted_instead_of_deleted(self):
        """1.5.2's 恢复上一个样本 file may hold the only copy of the original position."""
        self.store.save_points(dorm_points.add(
            dorm_points.empty(), name='李园一舍', latitude=29.827409, longitude=106.422742,
            accuracy=100.0, saved_at='2026-09-24T10:03:54+08:00'))
        self.store.save_sample(map_pick_sample(29.827409, 106.422742))
        (self.store.root / 'location-sample.previous.json').write_text(
            json.dumps(map_pick_sample(29.823693, 106.422310, accuracy=141.0)), encoding='utf-8')
        model = self.bridge.simulation_map()
        self.assertEqual([point['name'] for point in model['points']], ['李园一舍', '上一个样本'])
        adopted = model['points'][1]
        self.assertAlmostEqual(adopted['latitude'], 29.823693, places=6)
        self.assertEqual(adopted['source'], 'SAMPLE')
        self.assertEqual(model['active_id'], model['points'][0]['id'],
                         'the sample being replayed stays active')
        self.assertFalse((self.store.root / 'location-sample.previous.json').exists())
        self.assertAlmostEqual(self.store.sample()['latitude'], 29.827409, places=6)

    def test_the_mirror_always_matches_the_active_point(self):
        self.store.save_points(dorm_points.add(
            dorm_points.empty(), name='李园一舍', latitude=29.827439, longitude=106.422612,
            accuracy=100.0, saved_at='2026-09-24T10:01:02+08:00'))
        self.assertIsNone(self.store.sample(), 'nothing to replay yet')
        model = self.bridge.simulation_map()
        self.assertEqual(model['active_id'], model['points'][0]['id'])
        self.assertAlmostEqual(self.store.sample()['latitude'], 29.827439, places=6)
        self.assertAlmostEqual(model['point']['latitude'], 29.827439, places=6)

    def test_a_pick_is_saved_without_destroying_the_previous_position(self):
        self.store.save_sample(map_pick_sample(29.800000, 106.400000))
        result = self.bridge.dispatch('simulation_point_save', {'latitude': 29.823693,
                                                                 'longitude': 106.422310,
                                                                 'name': '宿舍楼下'})
        self.assertTrue(result['ok'])
        self.assertIn('已新建选点「宿舍楼下」', result['message'])
        self.assertIn('距学校基准点', result['message'])
        self.assertAlmostEqual(self.store.sample()['latitude'], 29.823693, places=6)
        self.assertEqual(self.store.sample()['source'], 'MAP_PICK')
        self.assertFalse((self.store.root / 'location-sample.previous.json').exists())

    def test_saving_the_same_name_moves_that_point_instead_of_adding_one(self):
        self.bridge.dispatch('simulation_point_save', {'latitude': 29.823693, 'longitude': 106.422310,
                                                        'name': '宿舍楼下'})
        result = self.bridge.dispatch('simulation_point_save', {'latitude': 29.825000,
                                                                 'longitude': 106.424000,
                                                                 'name': '宿舍楼下'})
        self.assertTrue(result['ok'])
        self.assertIn('已更新选点「宿舍楼下」的位置', result['message'])
        model = self.bridge.simulation_map()
        self.assertEqual(len(model['points']), 1)
        self.assertAlmostEqual(model['points'][0]['latitude'], 29.825000, places=6)

    def test_a_pick_outside_the_radius_is_reported_as_such(self):
        result = self.bridge.dispatch('simulation_point_save', {'latitude': 29.850000, 'longitude': 106.422310})
        self.assertTrue(result['ok'])
        self.assertIn('超出', result['message'])
        self.assertIn('800 米', result['message'])

    def test_a_pick_is_saved_even_before_the_school_point_is_known(self):
        self.controller.latest = Result('idle', '尚未查询今日任务')
        result = self.bridge.dispatch('simulation_point_save', {'latitude': 29.823693, 'longitude': 106.422310})
        self.assertTrue(result['ok'])
        self.assertIn('查询今日任务', result['message'])
        self.assertAlmostEqual(self.store.sample()['latitude'], 29.823693, places=6)
        model = self.bridge.simulation_map()
        self.assertIsNone(model['reference'])
        self.assertIsNone(model['distance_m'])
        self.assertIsNone(model['in_range'])

    def test_a_pick_survives_the_replay_path(self):
        self.bridge.dispatch('simulation_point_save', {'latitude': 29.823693, 'longitude': 106.422310})
        from dorm_location import locate
        position = locate('simulation', self.sample_path)
        self.assertEqual(position['provider'], 'windows')
        self.assertGreater(position['latitude'], 29.82)

    def test_unusable_picks_never_touch_the_stored_sample(self):
        self.store.save_sample(map_pick_sample(29.800000, 106.400000))
        for latitude, longitude in (('north', 106.4), (None, None), (91.0, 106.4), (29.8, 181.0)):
            with self.subTest(pick=(latitude, longitude)):
                result = self.bridge.dispatch('simulation_point_save',
                                              {'latitude': latitude, 'longitude': longitude})
                self.assertFalse(result['ok'])
                self.assertIn('选点坐标无效', result['message'])
        self.assertAlmostEqual(self.store.sample()['latitude'], 29.800000, places=6)
        self.assertEqual(self.bridge.simulation_map()['points'][0]['name'], '本机采样样本')

    def test_a_pick_is_refused_while_a_check_in_is_running(self):
        self.controller.busy = True
        result = self.bridge.dispatch('simulation_point_save', {'latitude': 29.823693, 'longitude': 106.422310})
        self.assertFalse(result['ok'])
        self.assertFalse(self.sample_path.exists())

    def test_renaming_the_adopted_sample_works_and_survives_switching(self):
        self.store.save_sample(map_pick_sample(29.800000, 106.400000))
        adopted = self.bridge.simulation_map()['points'][0]['id']
        self.bridge.dispatch('simulation_point_save', {'latitude': 29.823693, 'longitude': 106.422310,
                                                        'name': '李园一舍'})
        result = self.bridge.dispatch('simulation_point_rename', {'id': adopted, 'name': '原始采样'})
        self.assertTrue(result['ok'])
        self.assertIn('原始采样', result['message'])
        model = self.bridge.simulation_map()
        self.assertEqual([point['name'] for point in model['points']], ['原始采样', '李园一舍'])
        self.assertEqual(model['active_id'], model['points'][1]['id'], 'renaming does not switch')

    def test_deleting_the_active_point_switches_to_the_next_one(self):
        self.store.save_sample(map_pick_sample(29.800000, 106.400000))
        self.bridge.dispatch('simulation_point_save', {'latitude': 29.823693, 'longitude': 106.422310,
                                                        'name': '李园一舍'})
        model = self.bridge.simulation_map()
        active = next(point for point in model['points'] if point['active'])
        result = self.bridge.dispatch('simulation_point_delete', {'id': active['id']})
        self.assertTrue(result['ok'])
        self.assertIn('已自动切换到「本机采样样本」', result['message'])
        remaining = self.bridge.simulation_map()['points']
        self.assertEqual([point['name'] for point in remaining], ['本机采样样本'])
        self.assertAlmostEqual(self.store.sample()['latitude'], 29.800000, places=6)

    def test_deleting_the_last_point_leaves_no_location_to_replay(self):
        self.store.save_sample(map_pick_sample(29.800000, 106.400000))
        adopted = self.bridge.simulation_map()['points'][0]['id']
        result = self.bridge.dispatch('simulation_point_delete', {'id': adopted})
        self.assertTrue(result['ok'])
        self.assertIn('没有可用位置', result['message'])
        self.assertFalse(self.sample_path.exists(), 'no point means nothing to replay')
        self.assertEqual(self.bridge.simulation_map()['points'], [])

    def test_named_points_are_listed_and_the_newest_becomes_active(self):
        self.bridge.dispatch('simulation_point_save', {'latitude': 29.823693, 'longitude': 106.422310,
                                                        'name': '宿舍楼下'})
        self.bridge.dispatch('simulation_point_save', {'latitude': 29.825000, 'longitude': 106.424000,
                                                        'name': '图书馆北门'})
        model = self.bridge.simulation_map()
        self.assertEqual([point['name'] for point in model['points']], ['宿舍楼下', '图书馆北门'])
        self.assertEqual([point['active'] for point in model['points']], [False, True])
        self.assertEqual(model['active_id'], model['points'][1]['id'])
        self.assertAlmostEqual(model['point']['latitude'], 29.825000, places=6)

    def test_an_unnamed_pick_gets_a_generated_name(self):
        result = self.bridge.dispatch('simulation_point_save', {'latitude': 29.823693, 'longitude': 106.422310})
        self.assertTrue(result['ok'])
        self.assertIn('选点 1', result['message'])
        self.assertEqual(self.bridge.simulation_map()['points'][0]['name'], '选点 1')

    def test_switching_a_point_rewrites_the_replayed_sample(self):
        self.bridge.dispatch('simulation_point_save', {'latitude': 29.823693, 'longitude': 106.422310,
                                                        'name': '宿舍楼下'})
        self.bridge.dispatch('simulation_point_save', {'latitude': 29.825000, 'longitude': 106.424000,
                                                        'name': '图书馆北门'})
        model = self.bridge.simulation_map()
        first = model['points'][0]['id']
        result = self.bridge.dispatch('simulation_point_select', {'id': first})
        self.assertTrue(result['ok'])
        self.assertIn('宿舍楼下', result['message'])
        self.assertAlmostEqual(self.store.sample()['latitude'], 29.823693, places=6)
        self.assertEqual(self.bridge.simulation_map()['active_id'], first)
        from dorm_location import locate
        position = locate('simulation', self.sample_path)
        self.assertEqual(position['provider'], 'windows')

    def test_renaming_a_point(self):
        self.bridge.dispatch('simulation_point_save', {'latitude': 29.823693, 'longitude': 106.422310,
                                                        'name': '宿舍楼下'})
        point_id = self.bridge.simulation_map()['points'][0]['id']
        result = self.bridge.dispatch('simulation_point_rename', {'id': point_id, 'name': '宿舍东门'})
        self.assertTrue(result['ok'])
        self.assertIn('宿舍东门', result['message'])
        self.assertEqual(self.bridge.simulation_map()['points'][0]['name'], '宿舍东门')
        self.assertFalse(self.bridge.dispatch('simulation_point_rename', {'id': point_id, 'name': ''})['ok'])
        self.assertFalse(self.bridge.dispatch('simulation_point_rename', {'id': 'ghost', 'name': 'x'})['ok'])

    def test_deleting_an_idle_point_keeps_the_active_sample(self):
        self.bridge.dispatch('simulation_point_save', {'latitude': 29.823693, 'longitude': 106.422310,
                                                        'name': '宿舍楼下'})
        self.bridge.dispatch('simulation_point_save', {'latitude': 29.825000, 'longitude': 106.424000,
                                                        'name': '图书馆北门'})
        model = self.bridge.simulation_map()
        idle = model['points'][0]['id']
        self.assertTrue(self.bridge.dispatch('simulation_point_delete', {'id': idle})['ok'])
        after = self.bridge.simulation_map()
        self.assertEqual([point['name'] for point in after['points']], ['图书馆北门'])
        self.assertAlmostEqual(self.store.sample()['latitude'], 29.825000, places=6)

    def test_point_actions_are_refused_while_a_check_in_is_running(self):
        self.bridge.dispatch('simulation_point_save', {'latitude': 29.823693, 'longitude': 106.422310,
                                                        'name': '宿舍楼下'})
        point_id = self.bridge.simulation_map()['points'][0]['id']
        self.controller.busy = True
        for action, payload in (('simulation_point_save', {'latitude': 29.82, 'longitude': 106.42}),
                                ('simulation_point_select', {'id': point_id}),
                                ('simulation_point_rename', {'id': point_id, 'name': 'x'}),
                                ('simulation_point_delete', {'id': point_id})):
            with self.subTest(action=action):
                self.assertFalse(self.bridge.dispatch(action, payload)['ok'])

    def test_the_snapshot_never_exposes_coordinates(self):
        self.store.save_sample(map_pick_sample(29.823693, 106.422310))
        self.bridge.dispatch('simulation_point_save', {'latitude': 29.823693, 'longitude': 106.422310,
                                                        'name': '宿舍楼下'})
        state = json.dumps(self.bridge.snapshot(), ensure_ascii=False)
        self.assertNotIn('latitude', state)
        self.assertNotIn('29.82', state)
        self.assertNotIn('宿舍楼下', state)

    def test_the_location_check_names_the_point_it_used(self):
        self.store.save_points(dorm_points.add(
            dorm_points.empty(), name='杏园三舍', latitude=29.827439, longitude=106.422612,
            accuracy=100.0, saved_at='2026-09-24T10:03:54+08:00'))
        self.store.save_sample(map_pick_sample(29.827439, 106.422612))
        self.assertTrue(self.bridge.dispatch('location_authorize')['ok'])
        state = {'state': 'checking'}
        for _ in range(100):
            state = self.bridge.snapshot()['location']
            if state['state'] != 'checking':
                break
            time.sleep(0.05)
        self.assertEqual(state['state'], 'ready')
        self.assertIn('当前使用选点「杏园三舍」', state['message'])
        self.assertIn('未提交打卡', state['message'])

    def test_a_fresh_store_starts_with_the_shipped_positions(self):
        """A new install already has positions to choose from; 模拟定位 itself stays opt-in."""
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'dorm', protector=MagicMock(protect=lambda b: b[::-1],
                                                                        unprotect=lambda b: b[::-1]))
            store.save_settings(Settings(location_source='simulation'))
            controller = MagicMock()
            controller.latest = Result('idle', '尚未查询今日任务')
            controller.busy = False
            controller.store = store
            controller.schedule_text.return_value = ''
            controller.drain.return_value = []
            with patch('desktop_bridge.gui.ensure_user_config', side_effect=lambda p: p), \
                 patch('desktop_bridge.gui.load_gui_settings', return_value=GuiSettings('student', '', 60)), \
                 patch('desktop_bridge.gui.is_startup_enabled', return_value=False):
                bridge = DesktopBridge(Path(directory) / 'config.ini', dorm=controller)
                try:
                    model = bridge.simulation_map()
                finally:
                    bridge._close()
            self.assertEqual([point['name'] for point in model['points']],
                             [entry['name'] for entry in dorm_points.DEFAULT_POINTS])
            self.assertEqual(model['active_id'], model['points'][0]['id'],
                             'the first shipped position is ready to use')
            self.assertAlmostEqual(store.sample()['latitude'],
                                   dorm_points.DEFAULT_POINTS[0]['latitude'], places=6)
            self.assertEqual(Settings().location_source, 'windows',
                             'a fresh install still defaults to real location')

    def test_shipped_positions_survive_being_deleted(self):
        """Built-ins are ordinary points: deleting them must not resurrect them."""
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'dorm', protector=MagicMock(protect=lambda b: b[::-1],
                                                                        unprotect=lambda b: b[::-1]))
            store.save_settings(Settings(location_source='simulation'))
            controller = MagicMock()
            controller.latest = Result('idle', '尚未查询今日任务')
            controller.busy = False
            controller.store = store
            controller.schedule_text.return_value = ''
            controller.drain.return_value = []
            with patch('desktop_bridge.gui.ensure_user_config', side_effect=lambda p: p), \
                 patch('desktop_bridge.gui.load_gui_settings', return_value=GuiSettings('student', '', 60)), \
                 patch('desktop_bridge.gui.is_startup_enabled', return_value=False):
                bridge = DesktopBridge(Path(directory) / 'config.ini', dorm=controller)
                try:
                    for point in bridge.simulation_map()['points']:
                        bridge.dispatch('simulation_point_delete', {'id': point['id']})
                    model = bridge.simulation_map()
                finally:
                    bridge._close()
            self.assertEqual(model['points'], [])
            self.assertEqual(model['active_id'], '')

    def test_the_preview_picker_uses_demo_data_only(self):
        preview = PreviewBridge()
        self.assertTrue(preview.dispatch('location_source_save', {'location_source': 'simulation'})['ok'])
        model = preview.simulation_map()
        self.assertTrue(model['ok'])
        self.assertIn('示例宿舍', model['reference']['address'])
        self.assertTrue(preview.dispatch('simulation_point_save',
                                        {'latitude': 29.823000, 'longitude': 106.422000,
                                         'name': '演示·图书馆'})['ok'])
        self.assertEqual([point['name'] for point in preview.simulation_map()['points']],
                         ['演示·宿舍楼下', '演示·图书馆'])
        self.assertTrue(preview.dispatch('simulation_point_delete',
                                         {'id': preview.simulation_map()['active_id']})['ok'])
        self.assertEqual(preview.simulation_map()['active_id'],
                         preview.simulation_map()['points'][0]['id'])
        self.assertFalse(preview.dispatch('simulation_point_save', {'latitude': 'x'})['ok'])
        self.assertFalse(preview.simulation_map()['ok'] if
                         preview.dispatch('location_source_save', {'location_source': 'windows'})['ok']
                         else True)


class AccountSwitchTests(unittest.TestCase):
    """多账号在桥接层的接缝：快照、四个动作、交接副作用，以及「不许串账号」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.accounts = DormAccounts(self.root / 'accounts', controller_factory=FakeController)
        self.patches = [
            patch('desktop_bridge.gui.ensure_user_config', side_effect=lambda p: p),
            patch('desktop_bridge.gui.load_gui_settings', return_value=GuiSettings('student', '', 60)),
            patch('desktop_bridge.gui.is_startup_enabled', return_value=False),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.bridge = DesktopBridge(self.root / 'config.ini', accounts=self.accounts)
        self.addCleanup(self.bridge._close)

    def test_an_injected_single_controller_has_no_account_layer(self):
        """注入控制器的调用点（测试、旧工具）不进账号层，前端因此也不会显示切换器。"""
        controller = MagicMock()
        controller.latest = Result('idle', '尚未查询今日任务')
        controller.busy = False
        controller.store.settings.return_value = Settings()
        controller.store.root = self.root / 'dorm'
        controller.store.history.return_value = ''
        controller.schedule_text.return_value = '自动打卡：关闭'
        controller.drain.return_value = []
        bridge = DesktopBridge(self.root / 'config.ini', dorm=controller)
        self.addCleanup(bridge._close)
        self.assertEqual(bridge.snapshot()['accounts'],
                         {'error': '', 'max': 0, 'active': '', 'items': [], 'busy': False})
        self.assertFalse(bridge.dispatch('account_add', {'name': 'x'})['ok'])
        controller.start.assert_not_called()

    def test_the_snapshot_carries_each_accounts_today_status(self):
        first = self.accounts.registry().active_id()
        self.accounts.controller.store.save_settings(Settings(enabled=True))
        self.accounts.controller.store.mark_signed('2026-10-04', '2023000001')
        self.accounts.add('张三')
        with patch('dorm_accounts.now', return_value=_at('2026-10-04T21:30:00+08:00')):
            block = self.bridge.snapshot()['accounts']
        rows = {item['id']: item for item in block['items']}
        self.assertTrue(rows[first]['enabled'])
        self.assertTrue(rows[first]['signed_today'])
        self.assertFalse(rows[first]['busy'])
        self.assertIsInstance(rows[first]['message'], str)
        second = self.accounts.registry().accounts()[1].id
        self.assertFalse(rows[second]['enabled'])
        self.assertFalse(rows[second]['signed_today'])
        self.assertFalse(block['busy'])
        self.assertNotIn(self.tmp.name, json.dumps(block))

    def test_every_account_reports_its_own_notification_with_its_name(self):
        """两个账号的同一种失败文案一模一样时，通知不能被去重吞掉一个。"""
        self.accounts.add('张三')
        accounts = self.accounts.registry().accounts()
        for account in accounts:
            controller = self.accounts._controller_for(account)
            controller.results = [Result('signed', '今日打卡已完成，当天不再重复检查',
                                         at='2026-10-04T21:05:00+08:00')]
        with patch('desktop_bridge.windows_notifications.show_toast') as toast:
            self.bridge._tick()
        messages = [call.args[0] for call in toast.call_args_list]
        self.assertEqual(len(messages), 2, '每个账号各自的结果都要提醒一次')
        self.assertIn('账号 1', messages[0])
        self.assertIn('张三', messages[1])

    def test_one_accounts_repeat_is_still_suppressed(self):
        controller = self.accounts.controller
        result = Result('signed', '今日打卡已完成，当天不再重复检查', at='2026-10-04T21:05:00+08:00')
        controller.results = [result]
        with patch('desktop_bridge.windows_notifications.show_toast') as toast:
            self.bridge._tick()
            controller.results = [result]
            self.bridge._tick()
        self.assertEqual(len(toast.call_args_list), 1)

    def test_cancelling_from_the_ui_reaches_every_account(self):
        self.accounts.add('张三')
        accounts = self.accounts.registry().accounts()
        controllers = [self.accounts._controller_for(account) for account in accounts]
        result = self.bridge.dispatch('dorm_cancel')
        self.assertTrue(result['ok'])
        self.assertIn('所有账号', result['message'])
        self.assertTrue(all(controller.cancelled for controller in controllers))

    def test_update_install_waits_for_every_account_not_just_the_active_one(self):
        self.accounts.add('张三')
        other = self.accounts.registry().accounts()[1]
        self.accounts._controller_for(other).busy = True
        with patch.object(self.bridge._updates, 'install', return_value='打开安装向导') as install:
            result = self.bridge.dispatch('update_install', {'confirmed': True, 'version': '9.9.9'})
        self.assertFalse(result['ok'])
        install.assert_not_called()

    def test_the_snapshot_lists_accounts_without_exposing_paths(self):
        self.bridge.dispatch('account_add', {'name': '张三'})
        block = self.bridge.snapshot()['accounts']
        self.assertEqual([item['name'] for item in block['items']], ['账号 1', '张三'])
        self.assertEqual([item['active'] for item in block['items']], [False, True])
        self.assertEqual(block['active'], block['items'][1]['id'])
        self.assertNotIn(self.tmp.name, json.dumps(block))

    def test_adding_an_account_switches_the_whole_dorm_view_to_it(self):
        self.bridge.dispatch('dorm_save', {'enabled': True, 'start': '21:00', 'end': '23:30',
                                           'interval': 600, 'location_source': 'windows'})
        first = self.bridge.snapshot()['dorm']['settings']
        self.assertEqual(first['interval'], 600)
        self.assertTrue(self.bridge.dispatch('account_add', {'name': '张三'})['ok'])
        second = self.bridge.snapshot()['dorm']
        self.assertEqual(second['settings']['interval'], 300)      # 新账号是默认值
        self.assertFalse(second['settings']['enabled'])
        self.assertEqual(second['schedule'], '自动打卡：关闭')

    def test_switching_back_restores_the_first_account_settings(self):
        self.bridge.dispatch('dorm_save', {'enabled': True, 'start': '21:00', 'end': '23:30',
                                           'interval': 600, 'location_source': 'windows'})
        first_id = self.bridge.snapshot()['accounts']['active']
        self.bridge.dispatch('account_add', {'name': '张三'})
        result = self.bridge.dispatch('account_switch', {'id': first_id})
        self.assertTrue(result['ok'])
        self.assertIn('账号 1', result['message'])
        self.assertEqual(self.bridge.snapshot()['dorm']['settings']['interval'], 600)
        self.assertEqual(self.bridge.snapshot()['accounts']['active'], first_id)

    def test_switching_resets_the_location_state_to_the_new_account_source(self):
        self.accounts.add('模拟账号')
        second_id = self.accounts.registry().active_id()
        second = self.accounts.controller
        second.store.save_settings(Settings(location_source='simulation'))
        self.accounts.switch(self.accounts.registry().accounts()[0].id)
        self.bridge._location_state = {'state': 'ready', 'message': '旧账号的定位结果',
                                       'source': 'windows', 'accuracy': 12, 'checked': '刚刚'}
        self.assertTrue(self.bridge.dispatch('account_switch', {'id': second_id})['ok'])
        self.assertIn('模拟', self.bridge.snapshot()['location']['message'])
        self.assertEqual(self.bridge.snapshot()['location']['state'], 'idle')

    def test_switching_is_allowed_while_an_account_is_checking_in(self):
        """第二阶段：切换只是换界面在看谁，后台账号的操作不受影响，也不再被拒绝。"""
        self.accounts.add('张三')
        second = self.accounts.registry().active_id()
        self.accounts.switch(self.accounts.registry().accounts()[0].id)
        busy = self.accounts.controller
        busy.busy = True
        result = self.bridge.dispatch('account_switch', {'id': second})
        self.assertTrue(result['ok'])
        self.assertIn('已切换', result['message'])
        self.assertEqual(self.bridge.snapshot()['accounts']['active'], second)
        self.assertTrue(busy.busy, '切走不影响后台账号正在进行的操作')
        self.assertFalse(busy.closed)

    def test_deleting_the_account_that_is_checking_in_is_refused_from_the_ui(self):
        self.accounts.add('正在打卡的')
        target = self.accounts.registry().active_id()
        profile = self.root / 'accounts' / target
        self.accounts.controller.busy = True
        result = self.bridge.dispatch('account_delete', {'id': target, 'confirmed': True})
        self.assertFalse(result['ok'])
        self.assertIn('正在打卡', result['message'])
        self.assertTrue(profile.is_dir())

    def test_renaming_uses_the_backend_clean_name(self):
        account_id = self.bridge.snapshot()['accounts']['active']
        self.assertTrue(self.bridge.dispatch('account_rename',
                                             {'id': account_id, 'name': '  张三\u0007  '})['ok'])
        self.assertEqual(self.bridge.snapshot()['accounts']['items'][0]['name'], '张三')
        self.assertFalse(self.bridge.dispatch('account_rename',
                                              {'id': account_id, 'name': '  '})['ok'])

    def test_deleting_requires_confirmation_and_removes_only_that_profile(self):
        keep = self.bridge.snapshot()['accounts']['active']
        self.bridge.dispatch('account_add', {'name': '要删的'})
        target = self.bridge.snapshot()['accounts']['active']
        profile = self.root / 'accounts' / target
        self.assertTrue(profile.is_dir())
        self.assertFalse(self.bridge.dispatch('account_delete', {'id': target})['ok'])
        self.assertTrue(profile.is_dir())
        self.assertFalse(self.bridge.dispatch('account_delete',
                                              {'id': target, 'confirmed': 'yes'})['ok'])
        self.assertTrue(profile.is_dir())
        self.assertTrue(self.bridge.dispatch('account_delete',
                                             {'id': target, 'confirmed': True})['ok'])
        self.assertFalse(profile.exists())
        self.assertEqual(self.bridge.snapshot()['accounts']['active'], keep)

    def test_the_last_account_cannot_be_deleted(self):
        only = self.bridge.snapshot()['accounts']['active']
        result = self.bridge.dispatch('account_delete', {'id': only, 'confirmed': True})
        self.assertFalse(result['ok'])
        self.assertEqual(len(self.bridge.snapshot()['accounts']['items']), 1)

    def test_unified_credentials_follow_the_active_account(self):
        first_root = self.bridge._active_idm_store().root
        self.bridge.dispatch('account_add', {'name': '张三'})
        second_root = self.bridge._active_idm_store().root
        self.assertNotEqual(first_root, second_root)
        self.assertEqual(first_root.parent.parent, self.root / 'accounts')
        self.assertEqual(second_root, self.root / 'accounts' / self.bridge.snapshot()['accounts']['active'] / 'idm')

    def test_a_broken_registry_degrades_the_dorm_block_and_not_the_snapshot(self):
        (self.root / 'accounts' / 'accounts.json').write_text('{', encoding='utf-8')
        state = self.bridge.snapshot()
        self.assertIn('损坏', state['accounts']['error'])
        self.assertEqual(state['accounts']['items'], [])
        self.assertIn('损坏', state['dorm']['message'])
        self.assertIn('network', state)                     # 校园网区块不受影响
        self.bridge._tick()                                 # 轮询安静跳过，不抛
        self.assertTrue(self.bridge.dispatch('account_add', {'name': 'x'})['ok'] is False)
        self.assertTrue(self.bridge.dispatch('dorm_query')['ok'] is False)
        self.bridge._close()

    def test_closing_the_bridge_closes_the_active_controller(self):
        controller = self.accounts.controller
        self.bridge._close()
        self.assertTrue(controller.closed)


    def test_the_preview_bridge_manages_demo_accounts_without_touching_disk(self):
        """预览/演示 bridge 也要能完整演示切换器，且一个字节都不写。"""
        with patch('desktop_bridge.DormAccounts') as real_accounts:
            preview = PreviewBridge()
            state = preview.snapshot()['accounts']
            self.assertEqual([item['name'] for item in state['items']], ['演示账号一', '演示账号二'])
            self.assertEqual(state['active'], 'demo1')
            self.assertFalse(state['busy'])
            self.assertTrue(state['items'][0]['signed_today'])
            self.assertTrue(state['items'][0]['enabled'])
            self.assertFalse(state['items'][1]['enabled'])
            self.assertEqual(preview.snapshot()['dorm']['idm_username'], '2026000000')

            self.assertTrue(preview.dispatch('account_switch', {'id': 'demo2'})['ok'])
            switched = preview.snapshot()
            self.assertEqual(switched['accounts']['active'], 'demo2')
            self.assertEqual(switched['dorm']['idm_username'], '')
            self.assertFalse(switched['dorm']['has_idm_credentials'])

            self.assertTrue(preview.dispatch('account_add', {'name': '演示·实验室'})['ok'])
            self.assertEqual(preview.snapshot()['accounts']['items'][-1]['name'], '演示·实验室')
            self.assertTrue(preview.dispatch('account_rename',
                                             {'id': 'demo2', 'name': '  改过的  '})['ok'])
            self.assertEqual(preview.snapshot()['accounts']['items'][1]['name'], '改过的')
            self.assertFalse(preview.dispatch('account_rename', {'id': 'demo2', 'name': ' '})['ok'])
            self.assertFalse(preview.dispatch('account_delete', {'id': 'demo2'})['ok'])
            self.assertTrue(preview.dispatch('account_delete',
                                             {'id': 'demo2', 'confirmed': True})['ok'])
            items = preview.snapshot()['accounts']['items']
            self.assertEqual([item['id'] for item in items], ['demo1', 'demo3'])
            self.assertEqual([item['active'] for item in items].count(True), 1,
                             '演示账号列表也必须有且只有一个活动账号')
            self.assertTrue(items[1]['active'], '删掉的是别的账号，活动账号不该换人')
            real_accounts.assert_not_called()

    def test_the_preview_bridge_cannot_delete_its_last_demo_account(self):
        preview = PreviewBridge()
        self.assertTrue(preview.dispatch('account_delete', {'id': 'demo2', 'confirmed': True})['ok'])
        result = preview.dispatch('account_delete', {'id': 'demo1', 'confirmed': True})
        self.assertFalse(result['ok'])
        self.assertIn('至少保留一个账号', result['message'])


if __name__ == '__main__':
    unittest.main()
