"""Control-plane regressions; no model calls or real desktop actions."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

from echopath.run_state import RunRegistry, RunControl, RunStopped, checkpoint
from echopath.dashboard import TaskRunner, build_handler, vm_health
from echopath.gui_agent import GUIAgent


class ControlsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'config.json'
        self.config.write_text('{}')
        self.registry = RunRegistry(self.config)

    def test_paused_acknowledgement_and_stop(self):
        async def scenario(control):
            self.registry.request(control.id, 'pause')
            task = asyncio.create_task(checkpoint())
            await asyncio.sleep(.05)
            self.assertFalse(task.done())
            self.assertEqual(self.registry.list()[0]['status'], 'paused')
            self.registry.request(control.id, 'resume')
            await task
            self.assertEqual(self.registry.list()[0]['status'], 'running')
            self.registry.request(control.id, 'pause')
            task = asyncio.create_task(checkpoint())
            await asyncio.sleep(.05)
            self.registry.request(control.id, 'stop')
            await task
        with self.assertRaises(RunStopped):
            with RunControl(self.registry, 'Test', 'local') as control:
                asyncio.run(scenario(control))
        self.assertEqual(self.registry.list()[0]['status'], 'stopped')

    def test_resume_discards_old_gui_plan(self):
        # The old path would click stale coordinates; after resume only fresh planning runs.
        class FakeGUI:
            _run_desktop_task = GUIAgent._run_desktop_task
            def __init__(self):
                self.actions = []
            async def _run_desktop_task_uninterrupted(self, *args):
                await checkpoint()
                self.actions.append('stale click')
            async def _run_planned_desktop_task(self, context, goal, payload):
                self.actions.append('fresh observation and plan')
                return payload
        gui = FakeGUI()
        async def scenario(control):
            self.registry.request(control.id, 'pause')
            worker = asyncio.create_task(gui._run_desktop_task(None, 'task', {'actions':[{'click':1}]}))
            await asyncio.sleep(.05)
            self.registry.request(control.id, 'resume')
            result = await worker
            self.assertNotIn('actions', result)
        with RunControl(self.registry, 'Test', 'local') as control:
            asyncio.run(scenario(control))
            control.finish('completed', 'Done')
        self.assertEqual(gui.actions, ['fresh observation and plan'])

    def test_lock_blocks_second_process_but_vm_can_run(self):
        with RunControl(self.registry, 'First', 'local') as control:
            script = 'from pathlib import Path; from echopath.run_state import *; c=RunControl(RunRegistry(Path(__import__("sys").argv[1])), "second", "local"); c.__enter__()'
            result = subprocess.run([sys.executable, '-c', script, str(self.config)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('already owns', result.stderr)
            with RunControl(self.registry, 'VM', 'http://127.0.0.1:8790') as vm:
                vm.finish('completed', 'Done')
            control.finish('completed', 'Done')

    def test_pausing_waits_for_inflight_action(self):
        async def scenario(control):
            async def action():
                await asyncio.sleep(.1)
                await checkpoint()
            worker = asyncio.create_task(action())
            self.registry.request(control.id, 'pause')
            await asyncio.sleep(.02)
            self.assertEqual(self.registry.list()[0]['status'], 'pausing')
            await asyncio.sleep(.12)
            self.assertEqual(self.registry.list()[0]['status'], 'paused')
            self.registry.request(control.id, 'resume')
            await worker
        with RunControl(self.registry, 'Test', 'local') as control:
            asyncio.run(scenario(control))
            control.finish('completed', 'Done')

    def test_ui_api_origin_checks_and_controls(self):
        runner = TaskRunner(self.config)
        server = ThreadingHTTPServer(('127.0.0.1', 0), build_handler(runner))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_port}'
        with urlopen(url) as response:
            self.assertIn(b'Ongoing & paused', response.read())
        with RunControl(self.registry, 'Test', 'local') as control:
            data = json.dumps({'id':control.id, 'action':'pause'}).encode()
            bad = Request(url+'/api/control', data=data, headers={'Content-Type':'application/json','Origin':'https://evil.example'})
            with self.assertRaises(HTTPError) as error:
                urlopen(bad)
            self.assertEqual(error.exception.code, 403)
            good = Request(url+'/api/control', data=data, headers={'Content-Type':'application/json'})
            with urlopen(good) as response:
                self.assertEqual(json.load(response)['runs'][0]['status'], 'pausing')
            control.finish('completed', 'Done')

    def test_local_desktop_authorization_requires_tray_app(self):
        runner = TaskRunner(self.config)
        self.assertFalse(runner.local_desktop_authorization()['ready'])
        runner.enable_local_app_host()
        with patch('echopath.macos_permissions.local_desktop_control_status', return_value={
            'accessibility': True,
            'screen_recording': True,
        }):
            authorization = runner.local_desktop_authorization()
        self.assertTrue(authorization['app_host'])
        self.assertTrue(authorization['ready'])
        self.assertEqual(Path(authorization['config_path']), self.config.resolve())

    def test_vm_agent_round_trip_with_fake_desktop(self):
        from PIL import Image
        from echopath.amazon_vm_agent import AmazonVMDesktopAgentServer, AmazonVMDesktopAgentRequestHandler
        from echopath.amazon_vm_service import AmazonVMDesktopController
        image_path = Path(self.temp.name) / 'guest.png'
        Image.new('RGB', (30, 20), 'red').save(image_path)
        class FakeDesktop:
            def observe_sync(self, **kwargs):
                return {'path':str(image_path), 'image_size':{'width':30,'height':20},'mouse_position':{'x':2,'y':3}}
        server = AmazonVMDesktopAgentServer(('127.0.0.1',0), AmazonVMDesktopAgentRequestHandler, controller=FakeDesktop(), api_key='test-secret')
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        endpoint = f'http://127.0.0.1:{server.server_port}'
        self.assertTrue(vm_health(endpoint)['auth_required'])
        client = AmazonVMDesktopController(endpoint=endpoint, screenshot_directory=Path(self.temp.name)/'received')
        with patch.dict(os.environ, {'ECHOPATH_AMAZON_VM_API_KEY':'test-secret'}):
            result = client.observe_sync()
        self.assertTrue(result['capture_verified'])
        with Image.open(result['path']) as image:
            self.assertEqual(image.size, (30,20))

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS menu-bar masking')
    def test_mask_retains_coordinate_system_and_hides_status_pixels(self):
        from PIL import Image
        from echopath.tray import mask_controls
        with RunControl(self.registry, 'Test', 'local') as control:
            (self.registry.root/'tray.json').write_text(json.dumps({'pid':os.getpid(),'rect':[5,0,10,4]}))
            image = Image.new('RGB', (200,100), 'white')
            with patch('Quartz.CGWindowListCopyWindowInfo', return_value=[]):
                result = mask_controls(image, None, (100,50))
            self.assertEqual(result.size, (200,100))
            self.assertEqual(result.getpixel((15,3)), (16,23,36))
            self.assertEqual(result.getpixel((80,40)), (255,255,255))
            control.finish('completed', 'Done')


if __name__ == '__main__':
    unittest.main()


class DashboardProceduresTest(unittest.TestCase):
    """Stored procedures are inspectable step by step without leaking record paths."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = root / 'config.json'
        self.config.write_text('{}')
        records = root / 'memory' / 'consolidated' / 'records'
        records.mkdir(parents=True)
        record = {'record_type': 'consolidated_memory', 'memory_id': 'demo-stocks', 'status': 'active', 'replayable': True,
                  'platform': 'darwin', 'tags': ['stocks'], 'purpose_summary': 'Show AMZN for a week.',
                  'match_keys': ['Direct replay: show amazon'], 'updated_at': '2026-09-01T00:00:00+00:00',
                  'generalization': {'name': 'Stock history', 'parameters': [{'name': 'ticker', 'canonical_value': 'AMZN'}]},
                  'consolidated_operations': [
                      {'action': 'hotkey', 'keys': ['command', 'space'], 'expected_outcome': 'Spotlight opens.'},
                      {'action': 'paste_text', 'text': 'Stocks'},
                      {'action': 'click', 'x': 5, 'y': 6, 'clicks': 1},
                      {'command': 'rg --files'}]}
        (records / 'consolidated-demo-stocks.json').write_text(json.dumps(record))
        server = ThreadingHTTPServer(('127.0.0.1', 0), build_handler(TaskRunner(self.config)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f'http://127.0.0.1:{server.server_port}'

    def test_procedure_detail(self):
        with urlopen(self.url + '/api/procedures/consolidated-library/demo-stocks') as response:
            body = response.read()
        self.assertNotIn(self.temp.name.encode(), body)
        detail = json.loads(body)['procedure']
        self.assertEqual((detail['title'], detail['query'], detail['replayable']), ('Stock history', 'Direct replay: show amazon', True))
        self.assertEqual(detail['parameters'], [{'name': 'ticker', 'value': 'AMZN'}])
        self.assertEqual([s['detail'] for s in detail['steps']], ['command + space', '“Stocks”', 'at 5, 6', 'rg --files'])
        self.assertEqual(detail['steps'][3]['action'], 'shell')
        for missing in ('/api/procedures/consolidated-library/missing', '/api/procedures/memory-repo/missing', '/api/procedures/other/x'):
            with self.assertRaises(HTTPError) as error:
                urlopen(self.url + missing)
            self.assertEqual(error.exception.code, 404)

    def test_tray_page_served(self):
        for path in ('/tray', '/tray.js', '/common.js'):
            with urlopen(self.url + path) as response:
                self.assertEqual(response.status, 200)


class MCPAuthorizationGatewayTest(unittest.TestCase):
    def test_local_desktop_task_is_submitted_to_authorized_app(self):
        from echopath.mcp.server import _start_local_task_via_app
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = Path(temp.name) / 'config.json'
        config.write_text('{}')
        authorization = {
            'app_host': True,
            'ready': True,
            'accessibility': True,
            'screen_recording': True,
            'config_path': str(config),
        }
        with patch('echopath.mcp.server._local_app_request', side_effect=[
            authorization,
            {'task': {'id': 'run-1', 'status': 'queued'}},
        ]) as request:
            task = _start_local_task_via_app(config, 'Open Clock')
        self.assertEqual(task['id'], 'run-1')
        self.assertEqual(request.call_args_list[1].args[0], '/api/tasks')
        self.assertEqual(request.call_args_list[1].kwargs['payload']['desktop'], True)
        self.assertEqual(request.call_args_list[1].kwargs['payload']['native_handoff'], False)

    def test_local_desktop_task_rejects_unready_app(self):
        from echopath.mcp.server import _start_local_task_via_app
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = Path(temp.name) / 'config.json'
        config.write_text('{}')
        with patch('echopath.mcp.server._local_app_request', return_value={
            'app_host': True,
            'ready': False,
            'config_path': str(config),
            'detail': 'Enable Accessibility and Screen Recording for EchoPath.app, then reopen it.',
        }):
            with self.assertRaisesRegex(RuntimeError, 'Enable Accessibility'):
                _start_local_task_via_app(config, 'Open Clock')


class TrackedNativeHandoffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'config.json'
        self.config.write_text('{}')
        self.registry = RunRegistry(self.config)

    def _awaiting(self, handoff):
        with RunControl(self.registry, 'Test', 'local') as control:
            control.finish('awaiting_native', 'Waiting for the native continuation.')
        self.registry.offer_native(control.id, handoff)
        return control.id

    def test_answer_round_trip(self):
        with self.assertRaisesRegex(ValueError, 'not waiting'):
            self.registry.answer_native('missing', {})
        run_id = self._awaiting({'segment_id': 'seg-02', 'required_outputs': ['price']})
        self.assertEqual(self.registry.get(run_id)['native_handoff']['segment_id'], 'seg-02')
        self.assertIsNone(self.registry.take_native_answer(run_id))
        self.registry.answer_native(run_id, {'price': '4.99'})
        self.assertNotIn('native_handoff', self.registry.get(run_id))
        with self.assertRaisesRegex(ValueError, 'not waiting'):
            self.registry.answer_native(run_id, {'price': '5'})
        self.assertEqual(self.registry.take_native_answer(run_id),
                         {'segment_id': 'seg-02', 'outputs': {'price': '4.99'}, 'cancel': False})
        self.assertIsNone(self.registry.take_native_answer(run_id))

    def test_stop_cancels_the_pending_segment(self):
        run_id = self._awaiting({'segment_id': 'seg-02', 'required_outputs': []})
        self.registry.request(run_id, 'stop')
        self.assertTrue(self.registry.take_native_answer(run_id)['cancel'])
        with self.assertRaisesRegex(ValueError, 'native continuation'):
            self.registry.request(run_id, 'pause')

    def test_worker_waits_for_matching_outputs_then_resumes(self):
        from echopath.cli import _await_native
        calls = []
        class Service:
            def resume_native(self, **kwargs):
                calls.append(kwargs)
                return {'result': {'success': True}}
        run_id = self._awaiting({})
        response = {'native_handoff': {'segment_id': 'seg-02', 'required_outputs': ['price'],
                                       'continuation_token': 'secret', 'session_id': 's1'}}
        def host():
            for outputs in ({}, {'price': '4.99'}):
                while 'native_handoff' not in self.registry.get(run_id):
                    pass
                self.assertNotIn('continuation_token', self.registry.get(run_id)['native_handoff'])
                self.registry.answer_native(run_id, outputs)
        thread = threading.Thread(target=host)
        thread.start()
        result = _await_native(Service(), self.registry, run_id, response, wait_seconds=20)
        thread.join()
        self.assertTrue(result['result']['success'])
        self.assertEqual(calls, [{'session_id': 's1', 'continuation_token': 'secret',
                                  'outputs': {'price': '4.99'}, 'cancel': False}])

    def test_worker_cancels_when_the_host_never_answers(self):
        from echopath.cli import _await_native
        calls = []
        class Service:
            def resume_native(self, **kwargs):
                calls.append(kwargs)
                return {'status': 'cancelled'}
        run_id = self._awaiting({})
        _await_native(Service(), self.registry, run_id,
                      {'native_handoff': {'segment_id': 's', 'required_outputs': [],
                                          'continuation_token': 't', 'session_id': 's1'}}, wait_seconds=0)
        self.assertTrue(calls[0]['cancel'])

    def test_app_worker_gets_the_handoff_flag(self):
        runner = TaskRunner(self.config)
        with self.assertRaisesRegex(ValueError, 'Allow desktop control'):
            runner.start('Open Clock', False, '', True)
        with patch('echopath.dashboard.subprocess.Popen') as popen:
            popen.return_value.poll.return_value = None
            popen.return_value.wait.return_value = 0
            runner.start('Open Clock', True, '', True)
        self.assertIn('--native-handoff', popen.call_args.args[0])


class GoalVerifierBeforeFrameTest(unittest.TestCase):
    def test_replay_verifier_sees_the_frame_before_the_actions(self):
        from echopath.planning_agent import PlanningAgent
        build = PlanningAgent._build_desktop_goal_verifier_request
        after = {'path': '/tmp/after.png'}
        plain = build(None, 'Quit Clock', after, iteration=1, history=[], payload={})
        self.assertEqual([i['type'] for i in plain.input], ['text', 'local_image'])
        same = build(None, 'Quit Clock', after, iteration=1, history=[],
                     payload={'goal_verification_before_observation': after})
        self.assertEqual(len(same.input), 2)
        both = build(None, 'Quit Clock', after, iteration=1, history=[],
                     payload={'goal_verification_before_observation': {'path': '/tmp/before.png'}})
        self.assertEqual([i.get('path') for i in both.input[1:]], ['/tmp/after.png', '/tmp/before.png'])
        self.assertIn('FIRST is the current screen', both.instructions)
        self.assertIn('before.png', both.input[0]['text'])


class SegmentContractCheckTest(unittest.TestCase):
    def test_reuse_condition_is_checked_as_a_condition_not_task_completion(self):
        from echopath.segment_execution import CONTRACT_GUIDANCE, run_segmented_replay
        seen = []
        class Context:
            class registry:
                get_service = staticmethod(lambda name: None)
            async def emit(self, *args):
                pass
        class Agent:
            async def _observe_desktop(self, context, payload, label):
                return {'path': '/tmp/entry.png'}
            async def _request_desktop_goal_verification(self, context, goal, **kwargs):
                seen.append(kwargs['payload'].get('verification_prompt'))
                return {'goal_completed': False}
        segment = {'segment_id': 'seg-01', 'source_step_ids': ['step-0001'], 'goal': 'Start the timer',
                   'execution_policy': 'conditional', 'replay_conditions': ['Start is enabled'],
                   'completion_condition': 'A countdown is running', 'inputs': ['request'], 'outputs': [],
                   'output_bindings': [], 'rationale': 'r', 'start_action_index': 1, 'end_action_index': 1}
        payload = {'actions': [{'action': 'press', 'key': 'return'}], 'memory_segments': [segment],
                   'native_handoff_supported': True, 'verification_prompt': 'Existing guidance.'}
        result = asyncio.run(run_segmented_replay(Agent(), Context(), 'Start a timer', payload))
        self.assertEqual(result['status'], 'awaiting_native')
        self.assertEqual(seen, ['Existing guidance.\n' + CONTRACT_GUIDANCE])
        self.assertNotIn(CONTRACT_GUIDANCE, str(result['_checkpoint_payload'].get('verification_prompt')))


class VMSetupHelperTest(unittest.TestCase):
    def test_missing_guest_permissions_are_named(self):
        from echopath.vm_setup import missing_permissions
        self.assertEqual(missing_permissions({'platform': 'linux', 'permissions': None}), [])
        self.assertEqual(missing_permissions({}), [])  # A bridge installed before this field existed.
        self.assertEqual(missing_permissions({'permissions': {'accessibility': False, 'screen_recording': True}}),
                         ['Accessibility'])

    def test_guest_host_key_is_added_once_and_replaces_a_reused_address(self):
        from echopath import vm_cli
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.dict(os.environ, {'HOME': temp.name}), patch('pathlib.Path.home', return_value=Path(temp.name)):
            known = Path(temp.name) / '.ssh' / 'known_hosts'
            def keygen(command, **kwargs):
                lines = known.read_text().splitlines() if known.exists() else []
                if command[1] == '-R':
                    known.write_text(''.join(line + '\n' for line in lines if not line.startswith(command[2] + ' ')))
                    return subprocess.CompletedProcess(command, 0, '', '')
                found = [line for line in lines if line.startswith(command[2] + ' ')]
                return subprocess.CompletedProcess(command, 0 if found else 1, '\n'.join(found), '')
            with patch('echopath.vm_cli.subprocess.run', side_effect=keygen):
                self.assertEqual(vm_cli.trust_host_key('192.168.64.2', 'ssh-ed25519 AAAAfirst comment'), 'added')
                self.assertEqual(vm_cli.trust_host_key('192.168.64.2', 'ssh-ed25519 AAAAfirst'), 'already trusted')
                self.assertEqual(vm_cli.trust_host_key('192.168.64.2', 'ssh-ed25519 AAAAsecond'), 'replaced a stale entry')
            self.assertEqual(known.read_text(), '192.168.64.2 ssh-ed25519 AAAAsecond\n')

    def test_saving_requires_the_integration_to_be_off(self):
        from echopath import vm_cli
        from echopath.vm_setup import VMSetup, VMSetupError
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = Path(temp.name) / 'config.json'
        config.write_text('{}')
        vm = VMSetup(config)
        profile = {'id': 'default', 'name': 'VM', 'mode': 'ssh', 'ssh': {'host': 'my-vm'}}
        self.assertTrue(vm_cli.save(vm, profile)['configured'])
        payload = json.loads(config.read_text())
        payload['vm']['enabled'] = True
        config.write_text(json.dumps(payload))
        with self.assertRaisesRegex(VMSetupError, 'disable'):
            vm_cli.save(vm, profile)

    def test_same_installation_may_switch_between_venv_and_app_launcher(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'packaging'))
        self.addCleanup(sys.path.pop, 0)
        import setup as helper
        root = Path('/opt/EchoPath')
        config = root / 'echopath.local.json'
        expected = helper.expected_server(root, config, '/Users/me/Applications/EchoPath.app/Contents/MacOS/EchoPath')
        ours = helper.expected_server(root, config)
        self.assertFalse(helper.same_server(ours, expected))
        self.assertTrue(helper.own_server(ours, expected, root))
        self.assertFalse(helper.own_server({**ours, 'command': '/other/python'}, expected, root))
        self.assertFalse(helper.own_server({**ours, 'args': ['-m', 'echopath.mcp.server', '--config', '/other.json']}, expected, root))
        self.assertIn('echopath-vm-setup', helper.SKILLS)

    def test_installed_skills_update_in_place_for_the_same_package_only(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'packaging'))
        self.addCleanup(sys.path.pop, 0)
        import setup as helper
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(__file__).resolve().parent.parent
        skills = Path(temp.name) / 'skills'
        with patch.object(helper, 'agent_paths', return_value=(Path(temp.name) / 'config', skills)):
            self.assertEqual({dest.parts[-2] for _, dest in helper.skill_updates(root, 'claude') if dest.name == 'SKILL.md'},
                             set(helper.SKILLS))
            stale = skills / 'echopath' / 'SKILL.md'
            stale.parent.mkdir(parents=True)
            stale.write_text('an older or edited copy')
            with self.assertRaisesRegex(helper.SetupError, 'differs'):
                helper.skill_updates(root, 'claude')
            (stale.parent / 'origin.json').write_text(json.dumps({'package_root': str(root)}))
            helper.skill_updates(root, 'claude')
            (stale.parent / 'origin.json').write_text(json.dumps({'package_root': '/another/checkout'}))
            with self.assertRaisesRegex(helper.SetupError, 'another checkout'):
                helper.skill_updates(root, 'claude')


class TrayTitleTest(unittest.TestCase):
    def test_labels(self):
        from echopath.tray import status_title
        run = lambda status, **kw: {'id': status, 'status': status, 'started': 100, 'updated': 150, **kw}
        self.assertEqual(status_title([run('running')], set(), 202), '1:42')
        self.assertEqual(status_title([run('paused')], set(), 202), 'Paused')
        self.assertEqual(status_title([run('completed')], set(), 180), 'Done')
        self.assertEqual(status_title([run('completed')], set(), 400), '')
        self.assertEqual(status_title([run('error')], set(), 400), 'Failed')
        self.assertEqual(status_title([run('error')], {'error'}, 400), '')
        self.assertEqual(status_title([], set(), 0), '')


class ReplayFallbackConfigTest(unittest.TestCase):
    """Learned-path replays recover by planning unless the config asks for strict replay."""
    def test_default_and_override(self):
        from echopath.config import ConfigError, parse_global_model_router_config_payload
        from echopath.memory_replay import apply_memory_replay_execution_overrides
        base = {'model_routing': {'mode': 'external_command'}, 'external_command': {'command': ['true']}}
        self.assertTrue(parse_global_model_router_config_payload(base).desktop.replay_fallback_to_planning)
        strict = parse_global_model_router_config_payload({**base, 'desktop': {'replay_fallback_to_planning': False}})
        self.assertFalse(strict.desktop.replay_fallback_to_planning)
        with self.assertRaises(ConfigError):
            parse_global_model_router_config_payload({**base, 'desktop': {'replay_fallback_to_planning': 'yes'}})
        task = apply_memory_replay_execution_overrides({'payload': {}}, fallback_to_planning=True)
        self.assertTrue(task['payload']['memory_replay_fallback_to_planning'])


class LaunchErrorTest(unittest.TestCase):
    def test_summary_drops_progress_json(self):
        from echopath.dashboard import worker_error_summary
        tail = 'artial"}\n{"event": "x"}\nTraceback (most recent call last):\n  boom\nValueError: bad config\n'
        self.assertEqual(worker_error_summary(tail, partial=True), 'Traceback (most recent call last):\n  boom\nValueError: bad config')
        self.assertEqual(worker_error_summary('{"event": "x"}\n'), 'The worker exited before starting the task.')

    def test_registered_run_is_not_a_launch_error(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = Path(temp.name) / 'config.json'
        config.write_text('{}')
        runner = TaskRunner(config)
        log = Path(temp.name) / 'worker.log'
        log.write_text('{"event": "x"}\nTask failed\n')
        class Done:
            pid = os.getpid()
            def wait(self):
                return 1
        with RunControl(runner.registry, 'Task', 'local') as control:
            control.finish('error', 'Task failed')
        runner._reap('local', Done(), log)
        self.assertEqual(runner.launch_errors, [])
        Done.pid = -1
        runner._reap('local', Done(), log)
        self.assertEqual(runner.launch_errors[0]['detail'], 'Task failed')


class PyAutoGUIFixesTest(unittest.TestCase):
    """Retina captures are resized to click space; macOS events go to the session tap."""
    def test_hidpi_capture_normalized(self):
        from PIL import Image
        from echopath.pyautogui_service import PyAutoGUIController
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / 'shot.png'
        Image.new('RGB', (200, 100), 'white').save(path)
        controller = PyAutoGUIController.__new__(PyAutoGUIController)
        controller.screen_size_sync = lambda: {'width': 100, 'height': 50}
        size, scale = controller._normalize_capture_to_logical_space(path, captured_size=(200, 100), region=None)
        self.assertEqual((size, scale), ((100, 50), 2.0))
        self.assertEqual(Image.open(path).size, (100, 50))
        self.assertEqual(controller._normalize_capture_to_logical_space(path, captured_size=(100, 50), region=None), ((100, 50), 1.0))

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS only')
    def test_session_tap(self):
        import Quartz
        from echopath.pyautogui_service import _route_macos_events_to_session_tap
        original = Quartz.CGEventPost
        self.addCleanup(setattr, Quartz, 'CGEventPost', original)
        seen = []
        Quartz.CGEventPost = lambda tap, event: seen.append(tap)
        with patch.dict(os.environ, {'ECHOPATH_MACOS_HID_TAP': ''}):
            self.assertTrue(_route_macos_events_to_session_tap())
            self.assertTrue(_route_macos_events_to_session_tap())
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, None)
        self.assertEqual(seen, [Quartz.kCGSessionEventTap])


class ReplayDecisionModelTest(unittest.TestCase):
    """The OCR + Jev rung clicks a confident text choice and falls through otherwise."""
    def setUp(self):
        from PIL import Image, ImageDraw
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        image = Image.new('RGB', (400, 120), 'white')
        draw = ImageDraw.Draw(image)
        draw.text((20, 40), 'Where from?', fill='black')
        draw.text((220, 40), 'Where to?', fill='black')
        self.shot = Path(self.temp.name) / 'shot.png'
        image.resize((1200, 360)).save(self.shot)

    def choose(self, confidence, pick='Where to?'):
        from echopath.jev_client import Decision
        from echopath.replay_decision import choose_replay_target
        test = self
        class FakeJev:
            def choose(self, *, instructions, options, state):
                test.question = instructions
                key = next(k for k, v in options.items() if v.startswith(pick))
                return Decision(choice=key, confidence=confidence, cost=0.00005, latency_ms=300)
        actions = [{'action': 'click', 'x': 700, 'y': 130}, {'action': 'paste_text', 'text': 'New York'}]
        return choose_replay_target(
            goal='Find flights to New York', observation={'path': str(self.shot), 'image_size': {'width': 1200, 'height': 360}},
            stored_action={'action': 'click', 'x': 700, 'y': 130, 'expected_outcome': 'The destination field is focused.'},
            visual_binding={}, actions=actions, iteration=1, settings={'min_confidence': 0.9},
            working_directory=Path(self.temp.name), client=FakeJev())

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS Vision OCR')
    def test_confident_choice_is_clicked(self):
        report = self.choose(0.97)
        self.assertTrue(report['located'])
        self.assertEqual(report['choice_text'], 'Where to?')
        self.assertGreater(report['x'], 600)
        self.assertIn("types 'New York'", self.question)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS Vision OCR')
    def test_unsure_choice_falls_through(self):
        report = self.choose(0.6)
        self.assertFalse(report['located'])
        self.assertIn('not confident', report['reason'])

    def test_config_default_off(self):
        from echopath.config import parse_global_model_router_config_payload
        base = {'model_routing': {'mode': 'external_command'}, 'external_command': {'command': ['true']}}
        self.assertFalse(parse_global_model_router_config_payload(base).desktop.replay_decision_model)
        on = parse_global_model_router_config_payload({**base, 'desktop': {'replay_decision_model': True}})
        self.assertTrue(on.desktop.replay_decision_model)


class VMRetinaCaptureTest(unittest.TestCase):
    def test_guest_capture_downscaled_to_logical_screen(self):
        from PIL import Image
        from echopath.amazon_vm_service import _downscale_to_screen
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        shot = Path(temp.name) / 'guest.png'
        Image.new('RGB', (3100, 1938), 'white').save(shot)
        size = _downscale_to_screen(shot, {'width': 3100, 'height': 1938}, {'width': 1550, 'height': 969})
        self.assertEqual(size, {'width': 1550, 'height': 969})
        self.assertEqual(Image.open(shot).size, (1550, 969))
        # Already logical, or a screen reported larger than the image: left alone.
        self.assertEqual(_downscale_to_screen(shot, {'width': 1550, 'height': 969}, {'width': 1550, 'height': 969}), {'width': 1550, 'height': 969})
        self.assertEqual(_downscale_to_screen(shot, {'width': 1550, 'height': 969}, {'width': 3100, 'height': 1938}), {'width': 1550, 'height': 969})


class StepLogTest(unittest.TestCase):
    """Agent events fold into one readable line per step, with model calls on the right step."""
    def test_planned_then_replayed_steps(self):
        from types import SimpleNamespace as E
        from echopath.run_steps import StepLog
        log = StepLog()
        step1 = {'iteration': 1, 'action': 'Click "Sort by"', 'expected_outcome': 'The sort menu opens', 'status': 'running',
                 'timestamp': '2026-09-30T22:00:00+00:00', 'updated_at': '2026-09-30T22:00:05+00:00'}
        log.merge([], E(name='agent.model.requested', payload={}))            # plans step 1
        log.merge([step1], E(name='agent.desktop.planned', payload={'iteration': 1}))
        log.merge([step1], E(name='agent.model.requested', payload={}))       # verifies step 1
        done = {**step1, 'status': 'completed', 'verification': 'Menu is open', 'verification_status': 'succeeded'}
        log.merge([done], E(name='agent.desktop.step.verified', payload={'iteration': 1}))
        log.merge([done], E(name='agent.model.requested', payload={}))        # plans step 2
        log.merge([done], E(name='agent.replay.coordinate_copy', payload={'iteration': 2}))
        log.merge([done], E(name='agent.replay.coordinate_copy', payload={'iteration': 2}))
        steps = log.as_list()
        self.assertEqual([s['n'] for s in steps], [1, 2])
        self.assertEqual((steps[0]['model_calls'], steps[0]['verification'], steps[0]['source']), (2, 'Menu is open', 'planned'))
        self.assertEqual((steps[1]['model_calls'], steps[1]['source'], steps[1]['notes']), (1, 'replay', ['Clicked the saved position']))

    def test_steps_stored_with_the_run(self):
        from types import SimpleNamespace as E
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = Path(temp.name) / 'config.json'
        config.write_text('{}')
        registry = RunRegistry(config)
        from datetime import datetime, timezone
        with RunControl(registry, 'Task', 'local') as control:
            event = E(name='agent.desktop.planned', payload={'iteration': 1}, timestamp=datetime.now(timezone.utc))
            control.update('Planned', event, steps=[{'iteration': 1, 'action': 'Press cmd + space', 'status': 'running'}])
            control.finish('completed', 'Done')
        self.assertEqual(registry.list()[0]['steps'][0]['action'], 'Press cmd + space')


class ReplayStepTimingTest(unittest.TestCase):
    def test_replay_steps_complete_and_time_from_their_own_events(self):
        from datetime import datetime, timezone, timedelta
        from types import SimpleNamespace as E
        from echopath.run_steps import StepLog
        seeded = [{'iteration': n, 'action': f'Step {n}', 'status': 'running', 'timestamp': '2026-09-30T22:00:00+00:00'} for n in (1, 2)]
        t0 = datetime(2026, 9, 30, 22, 0, 7, tzinfo=timezone.utc)
        log = StepLog()
        log.merge(seeded, E(name='agent.replay.coordinate_copy', payload={'iteration': 1}, timestamp=t0))
        log.merge(seeded, E(name='agent.replay.visual_reaimed', payload={'iteration': 2}, timestamp=t0 + timedelta(seconds=3)))
        first, second = log.as_list()
        self.assertEqual((first['status'], first['started'], second['started']), ('completed', t0.isoformat(), (t0 + timedelta(seconds=3)).isoformat()))
        self.assertEqual(second['status'], 'running')
        log.finish('completed')
        self.assertEqual(log.as_list()[1]['status'], 'completed')


class RecordingTest(unittest.TestCase):
    """Guest recordings are copied in chunks, converted, cleaned up and served with byte ranges."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_agent_read_and_fetch(self):
        from echopath.amazon_vm_agent import ScreenRecorder
        from echopath import run_recording
        recorder = ScreenRecorder(self.root / 'guest')
        (self.root / 'guest').mkdir()
        movie = bytes(range(256)) * 40000  # ~10 MB, three chunks
        recorder.path('run1').write_bytes(movie)
        with self.assertRaises(ValueError):
            recorder.path('../etc')
        class FakeBridge:
            def _post_json(self, path, payload, request_timeout_seconds=None):
                action = path.rsplit('/', 1)[-1]
                if action == 'stop':
                    return {'size': len(movie)}
                if action == 'read':
                    return recorder.read(payload['recording_id'], payload['offset'], 4 * 1024 * 1024)
                return recorder.delete(payload['recording_id'])
        with patch.object(run_recording, '_convert', lambda source, target, preset: target.write_bytes(source.read_bytes())):
            saved = run_recording.fetch_recording(FakeBridge(), 'run1', self.root / 'host' / 'run1.mp4')
        self.assertEqual(saved.read_bytes(), movie)
        self.assertFalse(recorder.path('run1').exists())
        self.assertFalse((self.root / 'host' / 'run1.download.mov').exists())

    def test_recording_served_with_ranges(self):
        config = self.root / 'config.json'
        config.write_text('{}')
        (self.root / 'local_data' / 'recordings').mkdir(parents=True)
        (self.root / 'local_data' / 'recordings' / 'abc.mp4').write_bytes(b'0123456789')
        server = ThreadingHTTPServer(('127.0.0.1', 0), build_handler(TaskRunner(config)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_port}/api/runs/abc/recording'
        with urlopen(Request(url, headers={'Range': 'bytes=2-5'})) as response:
            self.assertEqual((response.status, response.read(), response.headers['Content-Range']), (206, b'2345', 'bytes 2-5/10'))
        with urlopen(url) as response:
            self.assertEqual(response.read(), b'0123456789')
        with self.assertRaises(HTTPError) as error:
            urlopen(f'http://127.0.0.1:{server.server_port}/api/runs/nope/recording')
        self.assertEqual(error.exception.code, 404)


class ReplayGoalFallbackTest(unittest.TestCase):
    """A replay that runs its steps but misses the goal finishes live instead of reporting success."""
    def run_replay(self, payload):
        class FakeContext:
            events = []
            async def emit(self, name, message, data=None):
                self.events.append(name)
        class FakeGUI:
            _run_desktop_task_uninterrupted = GUIAgent._run_desktop_task_uninterrupted
            _plan_after_replay = GUIAgent._plan_after_replay
            planned_with = None
            async def _run_scripted_desktop_task(self, context, goal, payload, actions):
                return {'completed': False, 'halt_reason': 'Opened a closed pinned issue, not the newest open one.'}
            async def _run_planned_desktop_task(self, context, goal, payload):
                self.planned_with = payload
                return {'completed': True}
        gui, context = FakeGUI(), FakeContext()
        result = asyncio.run(gui._run_desktop_task_uninterrupted(context, 'Open the newest open issue', payload))
        return gui, context, result

    def test_unmet_goal_hands_over_to_planning(self):
        gui, context, result = self.run_replay({'actions': [{'action': 'click'}], 'memory_replay_visual_bindings': [],
                                                'memory_replay_fallback_to_planning': True, 'verify_goal_completion': True})
        self.assertTrue(result['completed'])
        self.assertIn('agent.replay.goal_not_met.fallback', context.events)
        self.assertNotIn('actions', gui.planned_with)
        self.assertTrue(gui.planned_with['verify_goal_completion'])
        self.assertIn('closed pinned issue', result['replay_fallback']['reason'])

    def test_strict_replay_reports_the_miss(self):
        gui, context, result = self.run_replay({'actions': [{'action': 'click'}], 'memory_replay_visual_bindings': [],
                                                'memory_replay_fallback_to_planning': False})
        self.assertFalse(result['completed'])
        self.assertIsNone(gui.planned_with)


class StepIntentTest(unittest.TestCase):
    """Learned steps remember what they were for, and replay fallbacks look for that."""
    def test_export_and_fallback_use_intent(self):
        from echopath.memory_repo import MemoryRepository
        from echopath.gui_agent import build_memory_replay_step_grounding_next_step, build_memory_replay_step_grounding_prompt
        from echopath.memory_replay import MemoryReplayReaimingError
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        repo = MemoryRepository(root_directory=Path(temp.name))
        intent = {'target_description': 'the first issue in the open-issues list (below any pinned issues)',
                  'expected_outcome': "That issue's page opens"}
        (repo.node_directory / 'n1.json').write_text(json.dumps({'node_id': 'n1', 'action': {'action': 'click', 'x': 535, 'y': 349}, 'intent': intent}))
        (repo.path_directory / 'p1.json').write_text(json.dumps({'path_id': 'p1', 'node_ids': ['n1'], 'task_intent': 'Open the newest issue'}))
        action = repo.export_path_to_replay_task('p1', goal='Open the newest issue')['payload']['actions'][0]
        self.assertEqual((action['x'], action['target_description']), (535, intent['target_description']))
        error = MemoryReplayReaimingError('no match', action_index=1, binding_report={})
        step = build_memory_replay_step_grounding_next_step(action, {'action_index': 1}, reaiming_error=error, fallback_attempt=1)
        self.assertTrue(step['target_description'].startswith('the first issue in the open-issues list'))
        prompt = build_memory_replay_step_grounding_prompt('', {'action_index': 1}, reaiming_error=error, fallback_attempt=1)
        self.assertIn('Never pick a different element only because it', prompt)
