#!/usr/bin/env python3
"""Build metadata, persistent download caching, and Debian artifact validation."""

import argparse
import hashlib
import gzip
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from email.utils import format_datetime


def run(args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def output(args):
    return run(args, stdout=subprocess.PIPE, text=True).stdout.strip()


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def cache_values(build):
    result = {}
    for line in (Path(build) / 'CMakeCache.txt').read_text().splitlines():
        if line and not line.startswith(('#', '//')) and '=' in line:
            key, value = line.split('=', 1)
            result[key.split(':', 1)[0]] = value
    return result


def check(args):
    if args.profile not in ('native', 'portable', 'debug'):
        raise ValueError('PROFILE must be native, portable or debug')
    for name in ('native', 'mold', 'lto'):
        if getattr(args, name) not in ('ON', 'OFF'):
            raise ValueError(f'{name.upper()} must be ON or OFF')
    if args.mold == 'ON' and not shutil.which('mold'):
        raise ValueError('USE_MOLD=ON requires mold')
    compiler = shutil.which(args.compiler)
    if not compiler:
        raise ValueError(f'Missing compiler: {args.compiler}')
    for build in (args.build, args.deps):
        if (Path(build) / 'CMakeCache.txt').exists():
            cached = cache_values(build).get('CMAKE_C_COMPILER')
            if cached and Path(cached).resolve() != Path(compiler).resolve():
                raise ValueError(f'Compiler changed in {build}; run make distclean first')


def prepare_cache(args):
    deps, cache = Path(args.deps).resolve(), Path(args.cache).resolve()
    if cache == deps or deps in cache.parents:
        raise ValueError('DOWNLOAD_CACHE must be outside the dependency build directory')
    cache.mkdir(parents=True, exist_ok=True)
    downloads = deps / 'build' / 'downloads'
    downloads.parent.mkdir(parents=True, exist_ok=True)
    if downloads.is_symlink():
        if downloads.resolve() != cache:
            raise ValueError(f'{downloads} points to another cache; run make distclean first')
        return
    if downloads.exists():
        # Migrate archives from older wrapper builds; upstream still verifies hashes.
        shutil.copytree(downloads, cache, dirs_exist_ok=True)
        shutil.rmtree(downloads)
    downloads.symlink_to(cache, target_is_directory=True)


def source_state(source):
    source = Path(source).resolve()
    patch = run(['git', '-C', source, 'diff', 'HEAD', '--binary'], stdout=subprocess.PIPE).stdout
    return {
        'path': str(source),
        'commit': output(['git', '-C', source, 'rev-parse', 'HEAD']),
        'status': output(['git', '-C', source, 'status', '--porcelain']),
        'tracked_diff_sha256': hashlib.sha256(patch).hexdigest(),
    }


def record(args):
    source, build, deps = Path(args.source).resolve(), Path(args.build), Path(args.deps)
    values = cache_values(build)
    compiler = values['CMAKE_C_COMPILER']
    manifest = source / 'cmake.deps' / 'deps.txt'
    cpu = {}
    cpuinfo = Path('/proc/cpuinfo')
    if cpuinfo.exists():
        for line in cpuinfo.read_text().split('\n\n')[0].splitlines():
            if ':' in line:
                key, value = line.split(':', 1)
                if key.strip() in ('model name', 'flags', 'Features', 'CPU implementer', 'CPU part'):
                    cpu[key.strip()] = value.strip()
    info = {
        'schema_version': 1,
        'built_at_utc': datetime.now(timezone.utc).isoformat(),
        'profile': args.profile,
        'package_version': args.version,
        'source': source_state(source),
        'binary_sha256': digest(build / 'bin/nvim'),
        'compiler_version': output([compiler, '--version']),
        'host': {'machine': platform.machine(), 'cpu': cpu},
        'cmake_cache_sha256': digest(build / 'CMakeCache.txt'),
        'cmake': {key: value for key, value in values.items()
                  if key.startswith(('CMAKE_C_', 'CMAKE_EXE_LINKER_FLAGS'))
                  or key in ('CMAKE_BUILD_TYPE', 'CMAKE_INSTALL_PREFIX', 'ENABLE_LTO')},
        'dependencies': {
            'manifest_sha256': digest(manifest),
            'manifest': manifest.read_text(),
            'configuration': cache_values(deps),
        },
    }
    # Avoid publishing a partial manifest if the process is interrupted.
    pending = build / 'build-info.json.tmp'
    pending.write_text(json.dumps(info, indent=2, sort_keys=True) + '\n')
    pending.replace(build / 'build-info.json')


def smoke(binary, runtime=None):
    binary = Path(binary).resolve()
    if not binary.is_file():
        raise ValueError(f'No executable at {binary}; run make build first')
    with tempfile.TemporaryDirectory(prefix='neovim-smoke-') as temporary:
        env = dict(os.environ, NVIM_LOG_FILE=str(Path(temporary) / 'nvim.log'))
        # Isolate the check from user state and any external runtime override.
        env.pop('VIM', None)
        env.pop('VIMRUNTIME', None)
        for name in ('CONFIG', 'DATA', 'STATE', 'CACHE'):
            env[f'XDG_{name}_HOME'] = str(Path(temporary) / name.lower())
        if runtime:
            env['VIMRUNTIME'] = str(Path(runtime).resolve())
        run([binary, '--version'], env=env)
        lua = ('local ok, err = pcall(function() assert(vim.uv); '
               'require("vim.treesitter"); assert(vim.fn.filereadable('
               'vim.env.VIMRUNTIME .. "/doc/help.txt") == 1) end); '
               'if not ok then io.stderr:write(tostring(err), "\\n"); '
               'vim.cmd("cquit 1") end')
        run([binary, '--clean', '--headless', '-i', 'NONE', '-c', 'lua ' + lua, '-c', 'qa!'], env=env)


def compress(path):
    """Use reproducible gzip headers for Debian documentation and manual pages."""
    target = path.with_name(path.name + '.gz')
    with path.open('rb') as source, target.open('wb') as destination:
        with gzip.GzipFile(filename='', mode='wb', fileobj=destination, mtime=0, compresslevel=9) as archive:
            shutil.copyfileobj(source, archive)
    path.unlink()


def prepare_payload(payload, info, name, version, maintainer, strip):
    source = Path(info['source']['path'])
    documentation = payload / 'usr/share/doc' / name
    documentation.mkdir(parents=True, exist_ok=True)
    license_file = source / 'LICENSE.txt'
    shutil.copyfile(license_file, documentation / 'upstream-license.txt')
    copyright_notice = license_file.read_text(encoding='utf-8').splitlines()[0]
    (documentation / 'copyright').write_text(
        copyright_notice + '\n\n'
        'Upstream source: https://github.com/neovim/neovim\n\n'
        'The complete upstream copyright and licensing notices, including the\n'
        'Vim license and component-specific terms, are preserved verbatim in\n'
        f'/usr/share/doc/{name}/upstream-license.txt. Additional bundled-library\n'
        f'and parser notices are in /usr/share/doc/{name}/licenses/.\n\n'
        'On Debian systems, standard license texts are also available at:\n'
        '  /usr/share/common-licenses/Apache-2.0\n'
        '  /usr/share/common-licenses/LGPL-2\n'
        '  /usr/share/common-licenses/LGPL-2.1\n'
        '  /usr/share/common-licenses/LGPL-3\n'
        '  /usr/share/common-licenses/GPL-2\n', encoding='utf-8')
    # Keep the exact upstream notices for bundled libraries and parser grammars.
    deps = info['dependencies']['configuration'].get('CMAKE_CACHEFILE_DIR')
    if deps:
        for dependency in sorted((Path(deps) / 'build/src').glob('*')):
            if not dependency.is_dir() or dependency.name.endswith(('-build', '-stamp')):
                continue
            for notice in sorted(dependency.iterdir()):
                if notice.is_file() and notice.name.upper().split('.')[0] in ('LICENSE', 'COPYING', 'COPYRIGHT', 'NOTICE'):
                    destination = documentation / 'licenses' / dependency.name / notice.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(notice, destination)
    date = format_datetime(datetime.fromisoformat(info['built_at_utc']))
    changelog = documentation / 'changelog'
    changelog.write_text(
        f'{name} ({version}) unstable; urgency=medium\n\n'
        f'  * Local build of upstream Neovim {info["source"]["commit"]}.\n'
        '  * Package the optimized executable, bundled parsers and runtime.\n\n'
        f' -- {maintainer}  {date}\n', encoding='utf-8')
    compress(changelog)
    for path in sorted((payload / 'usr/share/man').rglob('*')):
        if path.is_file() and not path.is_symlink() and path.suffix != '.gz':
            compress(path)
    # CMake's --strip misses parsers installed as files rather than targets.
    for path in sorted(payload.rglob('*')):
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as stream:
                elf = stream.read(4) == b'\x7fELF'
            if elf:
                run([strip, '--strip-unneeded', path])
    remote_ssh = payload / 'usr/share/nvim/runtime/scripts/remote_ssh.sh'
    if remote_ssh.exists():
        remote_ssh.chmod(remote_ssh.stat().st_mode | 0o111)


def package(args):
    if not re.fullmatch(r'[^<>\r\n]+ <[^<>\s@]+@[^<>\s@]+>', args.maintainer):
        raise ValueError('PACKAGE_MAINTAINER must be Name <email>; set it explicitly or configure Git user.name/user.email')
    build, destination = Path(args.build).resolve(), Path(args.output).resolve()
    metadata = build / 'build-info.json'
    if not metadata.exists():
        raise ValueError('Build provenance is missing; run make build first')
    info = json.loads(metadata.read_text())
    if digest(build / 'CMakeCache.txt') != info['cmake_cache_sha256']:
        raise ValueError('Configuration changed since the build; run make build first')
    if digest(build / 'bin/nvim') != info['binary_sha256']:
        raise ValueError('Build binary changed; run make build first')
    if source_state(info['source']['path']) != info['source']:
        raise ValueError('Source changed since the build; run make build first')
    version = args.version or info['package_version']
    destination.mkdir(parents=True, exist_ok=True)
    # Validate in a staging directory so failures cannot publish a new artifact.
    with tempfile.TemporaryDirectory(prefix='.package-', dir=destination) as staging:
        staging = Path(staging)
        payload = staging / 'payload'
        # `cmake --install` never compiles. Disable CPack's generator-dependent
        # preinstall build by packaging this explicitly staged filesystem instead.
        run([args.cmake, '--install', build, '--prefix', '/usr', '--strip'],
            env=dict(os.environ, DESTDIR=str(payload)))
        prepare_payload(payload, info, args.name, version, args.maintainer,
                        cache_values(build).get('CMAKE_STRIP', 'strip'))
        description = ('Neovim is a text editor based on Vim with Lua scripting, an RPC API,\n'
                       'asynchronous jobs, and Tree-sitter syntax parsing.\n'
                       'This package contains a local build and its runtime files.')
        run([args.cpack, '--config', build / 'CPackConfig.cmake', '-G', 'DEB',
             '-B', staging, '-D', 'CPACK_PACKAGE_FILE_NAME=neovim-local',
             '-D', 'CPACK_INSTALL_CMAKE_PROJECTS=',
             '-D', f'CPACK_INSTALLED_DIRECTORIES={payload};/',
             '-D', 'CPACK_PACKAGING_INSTALL_PREFIX=/',
             '-D', f'CPACK_DEBIAN_PACKAGE_NAME={args.name}',
             '-D', f'CPACK_DEBIAN_PACKAGE_VERSION={version}',
             '-D', f'CPACK_DEBIAN_PACKAGE_MAINTAINER={args.maintainer}',
             '-D', 'CPACK_DEBIAN_PACKAGE_SECTION=editors',
             '-D', 'CPACK_DEBIAN_PACKAGE_HOMEPAGE=https://neovim.io/',
             '-D', 'CPACK_PACKAGE_DESCRIPTION_SUMMARY=extensible text editor',
             '-D', f'CPACK_DEBIAN_PACKAGE_DESCRIPTION={description}',
             '-D', 'CPACK_STRIP_FILES=ON'])
        deb = staging / 'neovim-local.deb'
        for field, expected in (('Package', args.name), ('Version', version)):
            actual = output(['dpkg-deb', '-f', deb, field])
            if actual != expected:
                raise ValueError(f'Package {field}: expected {expected!r}, got {actual!r}')
        extracted = staging / 'extracted'
        run(['dpkg-deb', '--extract', deb, extracted])
        runtime = extracted / 'usr/share/nvim/runtime'
        if not (runtime / 'lua/vim/treesitter.lua').is_file():
            raise ValueError('Package is missing its Lua runtime')
        smoke(extracted / 'usr/bin/nvim', runtime)
        report = staging / 'neovim-local.lintian.txt'
        lint = subprocess.run([args.lintian, str(deb)], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        report.write_text(lint.stdout)
        print(lint.stdout, end='', flush=True)
        if lint.returncode:
            shutil.copyfile(report, destination / 'neovim-local.failed.lintian.txt')
            raise ValueError(f'lintian failed with exit status {lint.returncode}; package was not published')
        info['package'] = {
            'name': args.name, 'version': version, 'maintainer': args.maintainer, 'sha256': digest(deb),
            'architecture': output(['dpkg-deb', '-f', deb, 'Architecture']),
            'depends': output(['dpkg-deb', '-f', deb, 'Depends']),
            'lintian_version': output([args.lintian, '--version']),
        }
        provenance = staging / 'neovim-local.build-info.json'
        provenance.write_text(json.dumps(info, indent=2, sort_keys=True) + '\n')
        artifacts = (deb, provenance, report)
        checksums = staging / 'neovim-local.sha256'
        checksums.write_text(''.join(f'{digest(path)}  {path.name}\n' for path in artifacts))
        # The checksum manifest is published last and covers every companion file.
        for path in (*artifacts, checksums):
            path.replace(destination / path.name)
    print(f'Validated package: {destination / "neovim-local.deb"}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    cmd = commands.add_parser('check')
    for name in ('compiler', 'profile', 'native', 'mold', 'lto', 'build', 'deps'):
        cmd.add_argument('--' + name, required=True)
    cmd.set_defaults(action=check)
    cmd = commands.add_parser('prepare-cache')
    for name in ('deps', 'cache'):
        cmd.add_argument('--' + name, required=True)
    cmd.set_defaults(action=prepare_cache)
    cmd = commands.add_parser('record')
    for name in ('source', 'build', 'deps', 'profile', 'version'):
        cmd.add_argument('--' + name, required=True)
    cmd.set_defaults(action=record)
    cmd = commands.add_parser('smoke')
    cmd.add_argument('--binary', required=True)
    cmd.set_defaults(action=lambda args: smoke(args.binary))
    cmd = commands.add_parser('package')
    for name in ('build', 'output', 'name', 'maintainer', 'cmake', 'cpack', 'lintian'):
        cmd.add_argument('--' + name, required=True)
    cmd.add_argument('--version', default='')
    cmd.set_defaults(action=package)
    args = parser.parse_args()
    try:
        args.action(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Error: {error}\n')


if __name__ == '__main__':
    main()
