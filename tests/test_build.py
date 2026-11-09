"""Workflow tests use fake build tools and real dpkg archives; never use sudo."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_support', ROOT / 'scripts/build_support.py')
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)

# Fake only expensive/external actions. Metadata, cache preparation, package
# extraction, identity checks, checksums and Make dependency ordering stay real.
TOOL = r'''#!/usr/bin/env python3
import json, os, pathlib, shutil, subprocess, sys
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
with open(os.environ['TEST_LOG'],'a') as f:
    f.write(json.dumps([name,args,os.environ.get('VIMRUNTIME')])+'\n')
if os.environ.get('FAIL_TOOL') == name: sys.exit(23)
if name == 'cmake' and args[:1] == ['--build'] and not args[1].endswith('/deps') and os.environ.get('FAIL_BUILD'): sys.exit(23)
if name == 'cmake':
    if '-S' in args:
        build=pathlib.Path(args[args.index('-B')+1]); build.mkdir(parents=True,exist_ok=True)
        values={'CMAKE_C_COMPILER':shutil.which('gcc')}
        for arg in args:
            if arg.startswith('-D'):
                key,value=arg[2:].split('=',1); values[key]=shutil.which(value) if key=='CMAKE_C_COMPILER' else value
        (build/'CMakeCache.txt').write_text(''.join(k+':STRING='+v+'\n' for k,v in values.items()))
        (build/'CPackConfig.cmake').touch()
    elif args[:1] == ['--build'] and not args[1].endswith('/deps') and '--target' not in args:
        build=pathlib.Path(args[1]); (build/'bin').mkdir(exist_ok=True)
        shutil.copy(os.environ['TEST_RUNNER'],build/'bin/nvim')
    elif args[:1] == ['--install']:
        build=pathlib.Path(args[1]); payload=pathlib.Path(os.environ['DESTDIR'])
        (payload/'usr/bin').mkdir(parents=True)
        shutil.copy(build/'bin/nvim',payload/'usr/bin/nvim')
        runtime=payload/'usr/share/nvim/runtime'
        (runtime/'doc').mkdir(parents=True); (runtime/'doc/help.txt').write_text('help')
        if not os.environ.get('BREAK_RUNTIME'):
            (runtime/'lua/vim').mkdir(parents=True)
            (runtime/'lua/vim/treesitter.lua').write_text('return {}')
    elif args[:2] == ['-E','rm']:
        for arg in args[3:]:
            path=pathlib.Path(arg)
            if path.is_dir() and not path.is_symlink(): shutil.rmtree(path)
            elif path.exists() or path.is_symlink(): path.unlink()
if name == 'cpack':
    values={args[i+1].split('=',1)[0]:args[i+1].split('=',1)[1] for i,a in enumerate(args) if a=='-D'}
    payload=pathlib.Path(values['CPACK_INSTALLED_DIRECTORIES'].split(';')[0])
    control=payload/'DEBIAN'; control.mkdir()
    (control/'control').write_text('Package: '+values['CPACK_DEBIAN_PACKAGE_NAME']+'\nVersion: '+values['CPACK_DEBIAN_PACKAGE_VERSION']+'\nArchitecture: amd64\nMaintainer: Tester <test@example.com>\nDepends: libc6\nDescription: test fixture\n')
    dest=pathlib.Path(args[args.index('-B')+1])/'neovim-local.deb'
    subprocess.run(['dpkg-deb','--build',str(payload),str(dest)],check=True,stdout=subprocess.DEVNULL)
if name == 'nvim' and '--version' not in args:
    runtime=os.environ.get('VIMRUNTIME')
    if runtime and not (pathlib.Path(runtime)/'doc/help.txt').exists(): sys.exit(1)
if name == 'lintian' and args==['--version']: print('lintian test fixture')
'''


@unittest.skipUnless(shutil.which('dpkg-deb') and shutil.which('gcc'), 'requires dpkg-deb and gcc')
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='neovim-workflow-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.build = self.root / 'output with spaces'
        self.source = self.root / 'source'
        (self.source / 'cmake.deps').mkdir(parents=True)
        (self.source / 'CMakeLists.txt').write_text('set(NVIM_VERSION_MAJOR 1)\nset(NVIM_VERSION_MINOR 2)\nset(NVIM_VERSION_PATCH 3)\nset(NVIM_VERSION_PRERELEASE "-dev")\n')
        (self.source / 'cmake.deps/deps.txt').write_text('LIBUV_URL https://example.com/libuv-1.0.tar.gz\nLIBUV_SHA256 abc\n')
        subprocess.run(['git', 'init', '-q', self.source], check=True)
        subprocess.run(['git', '-C', self.source, 'add', '.'], check=True)
        subprocess.run(['git', '-C', self.source, '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'fixture'], check=True)
        tools = self.root / 'bin'
        tools.mkdir()
        runner = tools / 'runner'
        runner.write_text(TOOL)
        runner.chmod(0o755)
        for name in ('cmake', 'cpack', 'sudo', 'lintian'):
            (tools / name).symlink_to(runner)
        self.log = self.root / 'calls.jsonl'
        self.env = dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH'],
                        TEST_LOG=str(self.log), TEST_RUNNER=str(runner))
        self.artifacts = self.build / 'packages/native'

    def make(self, *targets, fail=None, success=True, **environment):
        self.log.write_text('')
        env = dict(self.env, **environment)
        if fail:
            env['FAIL_TOOL'] = fail
        result = subprocess.run(['make', '--no-print-directory', *targets,
                                 f'BUILD_ROOT={self.build}', f'NEOVIM_DIR={self.source}',
                                 'USE_MOLD=OFF', 'JOBS=2', 'PACKAGE_VERSION=1.2.3'],
                                cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        self.assertEqual(result.returncode == 0, success, result.stdout)
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_profiles_and_standalone_stages(self):
        for profile, buildtype, native, lto in (
            ('native', 'Release', True, 'ON'), ('portable', 'Release', False, 'ON'),
            ('debug', 'RelWithDebInfo', False, 'OFF')):
            self.make(profile)
            values = support.cache_values(self.build / profile)
            self.assertEqual(values['CMAKE_BUILD_TYPE'], buildtype)
            self.assertEqual('-march=native' in values['CMAKE_C_FLAGS'], native)
            self.assertEqual(values['ENABLE_LTO'], lto)
        self.make('verify')
        calls = self.make('package')
        self.assertFalse(any(name == 'cmake' and ('--build' in args or '-S' in args)
                             for name, args, _ in calls))
        self.assertFalse(any(name == 'sudo' for name, _, _ in calls))
        runtime_calls = [runtime for name, args, runtime in calls if name == 'nvim' and '--version' not in args]
        self.assertIn('/extracted/usr/share/nvim/runtime', runtime_calls[0])

    def test_artifact_metadata_and_checksums(self):
        self.make('build')
        self.make('package')
        info = json.loads((self.artifacts / 'neovim-local.build-info.json').read_text())
        self.assertEqual(info['profile'], 'native')
        self.assertIn('compiler_version', info)
        self.assertIn('libuv-1.0', info['dependencies']['manifest'])
        self.assertEqual(info['package']['version'], '1.2.3')
        for line in (self.artifacts / 'neovim-local.sha256').read_text().splitlines():
            expected, name = line.split('  ', 1)
            self.assertEqual(support.digest(self.artifacts / name), expected)

    def test_validation_failure_preserves_published_artifact_and_blocks_install(self):
        self.make('build')
        self.make('package')
        checksum = support.digest(self.artifacts / 'neovim-local.sha256')
        for tool in ('cpack', 'nvim', 'lintian'):
            calls = self.make('install', fail=tool, success=False)
            self.assertFalse(any(name == 'sudo' for name, _, _ in calls))
            self.assertEqual(support.digest(self.artifacts / 'neovim-local.sha256'), checksum)
        self.make('package', BREAK_RUNTIME='1', success=False)

    def test_changed_configuration_or_source_requires_rebuild(self):
        self.make('build')
        cache = self.build / 'native/CMakeCache.txt'
        original = cache.read_text()
        cache.write_text(original + '\n# changed\n')
        self.make('package', success=False)
        cache.write_text(original)
        with (self.source / 'CMakeLists.txt').open('a') as stream:
            stream.write('# changed source\n')
        self.make('package', success=False)

    def test_compiler_change_and_failed_build_invalidate_provenance(self):
        self.make('build')
        cache = self.build / 'deps/CMakeCache.txt'
        cache.write_text('CMAKE_C_COMPILER:FILEPATH=/different/compiler\n')
        self.make('check-tools', success=False)
        self.make('distclean')
        self.make('build')
        # A failed compiler/build command must not leave an old build manifest.
        self.make('build', FAIL_BUILD='1', success=False)
        self.assertFalse((self.build / 'native/build-info.json').exists())
        self.make('package', success=False)
        self.make('check-tools', 'NATIVE=invalid', success=False)

    def test_download_cache_survives_distclean(self):
        self.make('build')
        cache = self.build / 'downloads'
        (cache / 'archive.tar.gz').write_bytes(b'cached archive')
        self.assertEqual((self.build / 'deps/build/downloads').resolve(), cache)
        self.make('distclean')
        self.assertEqual((cache / 'archive.tar.gz').read_bytes(), b'cached archive')
        self.make('build')
        self.assertEqual((self.build / 'deps/build/downloads/archive.tar.gz').read_bytes(), b'cached archive')
        self.make('purge-cache')
        self.assertFalse(cache.exists())

    def test_uninstall_is_independent_and_propagates_failures(self):
        calls = self.make('uninstall')
        self.assertEqual(calls, [['sudo', ['dpkg', '--purge', 'neovim'], None]])
        self.make('uninstall', fail='sudo', success=False)
        self.make('build')
        calls = self.make('install')
        self.assertEqual(calls[-1][:2], ['sudo', ['apt-get', 'install', str(self.artifacts / 'neovim-local.deb')]])

    def test_default_workflow_orders_build_before_install(self):
        calls = self.make('-j4')
        names = [name for name, _, _ in calls]
        self.assertEqual(names[-1], 'sudo')
        self.assertLess(names.index('cmake'), names.index('cpack'))
        self.assertLess(names.index('lintian'), names.index('sudo'))


class CacheMigrationTests(unittest.TestCase):
    def test_old_download_directory_is_migrated_and_reused(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / 'deps/build/downloads/libuv'
            old.mkdir(parents=True)
            (old / 'libuv.tar.gz').write_bytes(b'archive')
            args = Namespace(deps=root / 'deps', cache=root / 'downloads')
            support.prepare_cache(args)
            self.assertTrue((root / 'deps/build/downloads').is_symlink())
            self.assertEqual((root / 'downloads/libuv/libuv.tar.gz').read_bytes(), b'archive')
            support.prepare_cache(args)


if __name__ == '__main__':
    unittest.main()
