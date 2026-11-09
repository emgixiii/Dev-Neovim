# Local Neovim Debian builds

This wrapper builds the checked-out upstream `neovim` submodule, keeps build
profiles separate, and creates a locally versioned Debian package. Plain `make`
builds, validates, packages, and installs the selected profile. Only installation
and removal use sudo.

## Requirements

Use a Debian-based host with Git, a C compiler, CMake, Make, Python 3, and the
upstream Neovim build prerequisites. Packaging additionally requires CPack,
`dpkg-deb`, `dpkg-shlibdeps` (from `dpkg-dev`), `strip` (from `binutils`), and
`lintian`. Mold is automatically used when available; `USE_MOLD=OFF` disables it.
Lintian is required and errors prevent publication and installation. Upstream
packaging can produce lintian findings; the complete report is printed so these
can be corrected rather than silently ignored.

## Install the native optimized build

From the repository root, run:

```sh
make PROFILE=native NATIVE=ON ENABLE_LTO=ON
```

This builds Release with upstream's `-O3` optimization, `-march=native` for this
machine's CPU, and upstream LTO where supported. It uses all available CPU cores
by default, validates the package, and prompts for sudo when installing it. Run
Make as your normal user. Plain `make` uses these same defaults unless overridden
by your environment. Mold is detected automatically and speeds up linking; it is
not required for these optimizations.

The native package is intended for the machine that built it. Use
`make PROFILE=portable` when host-specific CPU instructions are unwanted. These
are the wrapper's strongest built-in optimization settings, not a guarantee of
the fastest performance for every workload.

If lintian is missing, install it first with `sudo apt-get install lintian`.
If you changed compilers, run `make distclean` before building again; downloaded
dependency archives are retained. `make native` only builds, while the command
above completes the build, package, validation, and installation workflow.

## Profiles and stages

```sh
make native                 # Release, host CPU instructions, upstream LTO
make portable               # Release, no additional host CPU instructions
make debug                  # RelWithDebInfo, assertions/symbols, LTO off by default
make PROFILE=portable JOBS=4 # Build, validate, package, and install portable profile
```

Profiles use `build/native`, `build/portable`, and `build/debug`. Bundled
dependencies use upstream Release defaults and are shared in `build/deps`.
“Portable” only controls this wrapper's CPU flags: packages still depend on the
host's architecture, libc baseline, and any explicit flags you supply.

```sh
make configure PROFILE=portable # Configure/build dependencies, configure Neovim
make build PROFILE=portable     # Configure, incrementally compile, record provenance
make verify PROFILE=portable    # Check the existing executable and Lua runtime
make package PROFILE=portable   # Package the existing build without compiling
make install PROFILE=portable   # Package, validate, then install using apt-get
make uninstall                  # Purge the explicitly named package
```

`verify`, `package`, and `install` do not rebuild. Run `make build` after changing
source or configuration. Packaging rejects a missing build record, a changed
binary/cache, or upstream source changes since the recorded build. Package
versions default to the version captured during that build; `PACKAGE_VERSION`
can override it explicitly. `build_release` and `build_debug` remain aliases for
building the native and debug profiles.

`PACKAGE_NAME` defaults to `neovim`, the identity used by upstream and distribution
packages. Installation replaces that package identity, and `make uninstall`
purges it even if the installed copy came from the distribution.

## Package validation and artifacts

Packaging installs the build into a temporary filesystem using `cmake --install`
and packages that tree, avoiding CPack's implicit preinstall compilation. It then:

1. Checks the Debian package name and version.
2. Extracts the package and runs its executable with its extracted runtime.
3. Loads the Tree-sitter Lua module and verifies the help runtime in an isolated
   temporary user environment.
4. Runs lintian and rejects a nonzero result.
5. Publishes the package, build metadata, lintian report, and SHA-256 manifest.

Artifacts live in `build/packages/PROFILE/`:

- `neovim-local.deb`: stripped binary and upstream runtime.
- `neovim-local.build-info.json`: upstream commit and dirty state, tracked patch
  hash, build time/profile, compiler version, cached flags, host CPU information,
  pinned dependency URLs/hashes and configuration, and package metadata.
- `neovim-local.lintian.txt`: lintian output.
- `neovim-local.sha256`: hashes of all three files above.

The build binary remains unstripped. Failed validation preserves previously
published artifacts. Check companion-file integrity from their directory:

```sh
cd build/packages/native
sha256sum -c neovim-local.sha256
```

The metadata records the build inputs and host; it does not claim byte-for-byte
reproducibility or CPU compatibility beyond the recorded flags.

## Download cache and cleanup

Dependency archives are cached in `build/downloads`; the upstream dependency
builder still verifies their pinned hashes. Existing dependency download folders
are migrated into this cache. Compiled objects are never cached there.

```sh
make clean PROFILE=debug # Clean one profile's build outputs
make distclean           # Remove all profiles, dependency builds, and packages
make purge-cache         # Remove downloaded archives explicitly
```

Downloads survive `distclean`, so switching compilers does not require downloading
them again. Run `make distclean` before changing `CC`. `BUILD_ROOT`,
`DOWNLOAD_CACHE`, and `PACKAGE_DIR` can be overridden; keep the download cache
outside the dependency build directory. A separately overridden `PACKAGE_DIR` is
not removed by `distclean`.

Other controls include `NATIVE=ON|OFF`, `ENABLE_LTO=ON|OFF`, `CFLAGS`, `LDFLAGS`,
`CMAKE_EXTRA_FLAGS`, and `DEPS_CMAKE_FLAGS`. The last two accept additional CMake
arguments. Extra project flags apply to Neovim; dependency optimization remains
under upstream control.

## Regression checks

```sh
make test
```

The tests use temporary repositories and fake compiler/build/privilege commands,
with real Debian archive creation and extraction. They cover profile selection,
standalone stages, metadata/checksums, extracted-runtime checks, failure handling,
source/configuration changes, cache retention/migration, and install ordering.
They do not install or remove system packages. Python 3, Git, Make, `gcc`, and
`dpkg-deb` are required to run the full suite.
