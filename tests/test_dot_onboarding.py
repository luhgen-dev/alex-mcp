import os
import stat
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'alex-mcp'/'app'))
from config import Settings
from dot_runtime import prepare_private_tunnel, configured, owner_binding, watch_configuration

class OnboardingTests(unittest.TestCase):
    def settings(self):
        return Settings(ai_provider='dot',dot_enabled=True,dot_transport='private_tunnel',
                        dot_tunnel_id='tunnel_'+'a'*32,dot_tunnel_runtime_key='test-only-'+('x'*40))

    def test_disabled_or_other_brain_does_not_touch_disk_or_generate(self):
        with tempfile.TemporaryDirectory() as d:
            absent=os.path.join(d,'never-created')
            for s in [Settings(),replace(self.settings(),dot_enabled=False),replace(self.settings(),ai_provider='grok')]:
                with patch('dot_runtime.secrets.token_urlsafe',side_effect=AssertionError('must not generate')):
                    self.assertFalse(prepare_private_tunnel(s,data_dir=absent)['prepared'])
                self.assertFalse(os.path.exists(absent))

    def test_explicit_enable_provisions_private_files_once_and_rotates_key(self):
        s=self.settings()
        self.assertTrue(configured(s))
        self.assertEqual(owner_binding(s),'private-tunnel:'+s.dot_tunnel_id)
        self.assertNotIn(s.dot_tunnel_runtime_key,repr(s))
        with tempfile.TemporaryDirectory() as d:
            result=prepare_private_tunnel(s,data_dir=d)
            self.assertTrue(result['generated_bridge_token'])
            token=Path(d,'dot-bridge-token').read_text()
            self.assertEqual(len(token),43)
            self.assertEqual(Path(d,'dot-tunnel-api-key').read_text(),s.dot_tunnel_runtime_key)
            for name in ['dot-bridge-token','dot-tunnel-api-key']:
                self.assertEqual(stat.S_IMODE(os.stat(Path(d,name)).st_mode),0o600)
            result=prepare_private_tunnel(replace(s,dot_tunnel_runtime_key='new-test-only-'+('y'*40)),data_dir=d)
            self.assertFalse(result['generated_bridge_token'])
            self.assertEqual(Path(d,'dot-bridge-token').read_text(),token)
            self.assertNotIn(token,str(result))
            self.assertEqual(Path(d,'dot-tunnel-api-key').read_text(),'new-test-only-'+('y'*40))

    def test_missing_key_or_invalid_tunnel_has_no_provisioning_effect(self):
        with tempfile.TemporaryDirectory() as d:
            for s in [replace(self.settings(),dot_tunnel_runtime_key=''),replace(self.settings(),dot_tunnel_id='wrong')]:
                with self.assertRaises(ValueError):prepare_private_tunnel(s,data_dir=d)
                self.assertEqual(os.listdir(d),[])

    def test_disable_watcher_does_not_wait_for_blocked_delivery(self):
        import threading
        blocked=threading.Event()
        entered=threading.Event()
        def delivery():
            entered.set()
            blocked.wait(2)
        worker=threading.Thread(target=delivery,daemon=True)
        worker.start()
        self.assertTrue(entered.wait(1))
        stop=threading.Event()
        shutdown=threading.Event()
        watcher=threading.Thread(target=watch_configuration,
            args=(self.settings(),stop,shutdown.set),
            kwargs={'read_settings':lambda:replace(self.settings(),dot_enabled=False),'interval':0.01})
        watcher.start()
        try:
            self.assertTrue(shutdown.wait(0.5))
            self.assertTrue(worker.is_alive())
        finally:
            stop.set();blocked.set();worker.join();watcher.join()

    def test_bridge_symlink_and_open_permissions_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d,'unrelated');target.write_text('untouched')
            link=Path(d,'dot-bridge-token');link.symlink_to(target)
            with self.assertRaises(OSError):prepare_private_tunnel(self.settings(),data_dir=d)
            self.assertEqual(target.read_text(),'untouched')
            link.unlink();link.write_text('x'*43);os.chmod(link,0o644)
            with self.assertRaises(ValueError):prepare_private_tunnel(self.settings(),data_dir=d)

if __name__=='__main__':unittest.main()
