import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import ha_release as release

class ReleaseTests(unittest.TestCase):
    def before(self):
        return {'version':'0.5.54','version_latest':'0.5.55','state':'started','options':{'secret':'synthetic'}}

    def run_case(self, latest=None, backup=None, after=None, archive_error=False):
        before = self.before()
        latest = latest if latest is not None else before
        backup = backup if backup is not None else {'slug':'aabb1234'}
        after = after if after is not None else {**before, 'version':'0.5.55'}
        calls=[]
        def ha(*args):
            calls.append(args)
            if args[:2]==('store','reload'): return {}
            if args[:2]==('backups','new'): return backup
            if args[:2]==('apps','update'): return {}
            if args[:2]==('apps','info'): return latest if len(calls)==2 else after
            raise AssertionError(args)
        archive=Mock()
        archive.__enter__=Mock(return_value=Mock(getmembers=lambda:['member']))
        archive.__exit__=Mock(return_value=False)
        with tempfile.TemporaryDirectory() as d, patch.object(release,'ha',side_effect=ha), \
             patch.object(release.tarfile,'open',side_effect=OSError() if archive_error else None,return_value=archive), \
             patch('builtins.print'):
            try: release.install_release(Path(d),before)
            except Exception as e: return calls,e
        return calls,None

    def test_backup_precedes_update_and_unchanged_options_pass(self):
        calls,error=self.run_case()
        self.assertIsNone(error)
        self.assertLess(next(i for i,c in enumerate(calls) if c[:2]==('backups','new')),
                        calls.index(('apps','update',release.APP)))

    def test_missing_release_changed_options_or_missing_backup_prevent_update(self):
        changed=copy.deepcopy(self.before());changed['options']['secret']='changed'
        for kwargs in [{'latest':{**self.before(),'version_latest':'0.5.54'}},
                       {'latest':changed},{'backup':{}},{'archive_error':True}]:
            with self.subTest(kwargs=kwargs):
                calls,error=self.run_case(**kwargs)
                self.assertIsNotNone(error)
                self.assertNotIn(('apps','update',release.APP),calls)

    def test_post_update_options_change_is_not_success(self):
        calls,error=self.run_case(after={**self.before(),'version':'0.5.55','options':{}})
        self.assertIsInstance(error,RuntimeError)
        self.assertIn(('apps','update',release.APP),calls)

    def test_cli_failure_is_propagated(self):
        with patch.object(release.subprocess,'run',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):release.ha('apps','info',release.APP)

if __name__=='__main__': unittest.main()
