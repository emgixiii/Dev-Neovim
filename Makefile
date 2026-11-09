# Build upstream Neovim and package it for this Debian-based host.
# `make` builds and installs; `make package` only packages an existing build.
.DEFAULT_GOAL := all
.DELETE_ON_ERROR:

NEOVIM_DIR ?= $(CURDIR)/neovim
BUILD_ROOT ?= $(CURDIR)/build
PROFILE ?= native
BUILD_DIR := $(BUILD_ROOT)/$(PROFILE)
DEPS_BUILD_DIR := $(BUILD_ROOT)/deps
DOWNLOAD_CACHE ?= $(BUILD_ROOT)/downloads
PACKAGE_DIR ?= $(BUILD_ROOT)/packages/$(PROFILE)
DEB_PACKAGE := $(if $(filter /%,$(PACKAGE_DIR)),$(PACKAGE_DIR),$(CURDIR)/$(PACKAGE_DIR))/neovim-local.deb
SUPPORT := $(CURDIR)/scripts/build_support.py

ifeq ($(origin CC),default)
CC := gcc
endif
CMAKE ?= cmake
CPACK ?= cpack
PYTHON ?= python3
LINTIAN ?= lintian
JOBS ?= $(shell nproc)
SUDO ?= sudo
NATIVE ?= $(if $(filter native,$(PROFILE)),ON,OFF)
USE_MOLD ?= $(if $(shell command -v mold 2>/dev/null),ON,OFF)
ENABLE_LTO ?= $(if $(filter debug,$(PROFILE)),OFF,ON)
PACKAGE_NAME ?= neovim
PACKAGE_MAINTAINER ?= $(shell git config user.name) <$(shell git config user.email)>
BUILD_TYPE := $(if $(filter debug,$(PROFILE)),RelWithDebInfo,Release)

# Upstream controls dependency optimization and Release's -O3/NDEBUG flags.
CFLAGS ?=
LDFLAGS ?=
CMAKE_EXTRA_FLAGS ?=
DEPS_CMAKE_FLAGS ?=
NATIVE_FLAGS := $(if $(filter ON,$(NATIVE)),-march=native)
MOLD_FLAGS := $(if $(filter ON,$(USE_MOLD)),-fuse-ld=mold)

VERSION_BASE = $(shell sed -n 's/^set(NVIM_VERSION_\(MAJOR\|MINOR\|PATCH\) \([0-9]*\)).*/\2/p' "$(NEOVIM_DIR)/CMakeLists.txt" | paste -sd .)
VERSION_PRERELEASE = $(shell sed -n 's/^set(NVIM_VERSION_PRERELEASE "\([^"]*\)").*/\1/p' "$(NEOVIM_DIR)/CMakeLists.txt")
SOURCE_REVISION = $(shell git -C "$(NEOVIM_DIR)" log -1 --format=%ct.g%h)
# Optional override; the default version is captured at build time.
PACKAGE_VERSION ?=
BUILD_VERSION = $(if $(PACKAGE_VERSION),$(PACKAGE_VERSION),$(VERSION_BASE)$(if $(VERSION_PRERELEASE),~dev.,+local.)git$(SOURCE_REVISION))

.PHONY: all check-tools check-package-tools deps configure build verify package install \
 uninstall clean distclean purge-cache help strip native portable debug build_release build_debug test

# The recursive invocation enforces build-before-package even under `make -j`.
all: check-package-tools
	$(MAKE) build
	$(MAKE) install

check-tools:
	@set -eu; \
	for tool in "$(CMAKE)" "$(CC)" "$(PYTHON)"; do command -v "$$tool" >/dev/null || { echo "Missing tool: $$tool" >&2; exit 1; }; done
	$(PYTHON) "$(SUPPORT)" check --compiler "$(CC)" --profile "$(PROFILE)" \
		--native "$(NATIVE)" --mold "$(USE_MOLD)" --lto "$(ENABLE_LTO)" \
		--build "$(BUILD_DIR)" --deps "$(DEPS_BUILD_DIR)"

check-package-tools:
	@set -eu; for tool in "$(PYTHON)" "$(CMAKE)" "$(CPACK)" dpkg-deb dpkg-shlibdeps strip "$(LINTIAN)"; do \
		command -v "$$tool" >/dev/null || { echo "Missing packaging tool: $$tool" >&2; exit 1; }; done

# Downloads survive distclean. Only downloaded archives are cached, never objects.
deps: check-tools
	$(PYTHON) "$(SUPPORT)" prepare-cache --deps "$(DEPS_BUILD_DIR)" --cache "$(DOWNLOAD_CACHE)"
	$(CMAKE) -S "$(NEOVIM_DIR)/cmake.deps" -B "$(DEPS_BUILD_DIR)" \
		-DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER="$(CC)" $(DEPS_CMAKE_FLAGS)
	$(CMAKE) --build "$(DEPS_BUILD_DIR)" --parallel "$(JOBS)"

configure: deps
	$(CMAKE) -S "$(NEOVIM_DIR)" -B "$(BUILD_DIR)" \
		-DCMAKE_BUILD_TYPE=$(BUILD_TYPE) -DCMAKE_C_COMPILER="$(CC)" \
		-DDEPS_PREFIX="$(DEPS_BUILD_DIR)/usr" -DCMAKE_INSTALL_PREFIX=/usr \
		-DENABLE_LTO=$(ENABLE_LTO) -DCMAKE_C_FLAGS="$(CFLAGS) $(NATIVE_FLAGS)" \
		-DCMAKE_EXE_LINKER_FLAGS="$(LDFLAGS) $(MOLD_FLAGS)" $(CMAKE_EXTRA_FLAGS)

build: configure
	$(CMAKE) -E rm -f "$(BUILD_DIR)/build-info.json"
	$(CMAKE) --build "$(BUILD_DIR)" --parallel "$(JOBS)"
	$(PYTHON) "$(SUPPORT)" record --source "$(NEOVIM_DIR)" --build "$(BUILD_DIR)" \
		--deps "$(DEPS_BUILD_DIR)" --profile "$(PROFILE)" --version "$(BUILD_VERSION)"

# Standalone stages deliberately do not configure or compile.
verify:
	$(PYTHON) "$(SUPPORT)" smoke --binary "$(BUILD_DIR)/bin/nvim"

package: check-package-tools
	$(PYTHON) "$(SUPPORT)" package --build "$(BUILD_DIR)" --output "$(PACKAGE_DIR)" \
		--name "$(PACKAGE_NAME)" --maintainer "$(PACKAGE_MAINTAINER)" --version "$(PACKAGE_VERSION)" --cmake "$(CMAKE)" --cpack "$(CPACK)" --lintian "$(LINTIAN)"

install: package
	$(SUDO) apt-get install "$(DEB_PACKAGE)"

# This identity is also used by distribution builds; no wildcard/name guessing.
uninstall:
	$(SUDO) dpkg --purge "$(PACKAGE_NAME)"

native portable debug:
	$(MAKE) PROFILE=$@ build

# Compatibility aliases.
build_release:
	$(MAKE) PROFILE=native build
build_debug:
	$(MAKE) PROFILE=debug build
strip: package
	@echo 'The package is stripped; build binaries retain their symbols.'

clean:
	@if [ -f "$(BUILD_DIR)/CMakeCache.txt" ]; then "$(CMAKE)" --build "$(BUILD_DIR)" --target clean; fi

# Preserve downloads across compiler changes; purge-cache explicitly removes them.
distclean:
	$(CMAKE) -E rm -rf "$(BUILD_ROOT)/native" "$(BUILD_ROOT)/portable" "$(BUILD_ROOT)/debug" \
		"$(DEPS_BUILD_DIR)" "$(BUILD_ROOT)/packages"
purge-cache:
	$(CMAKE) -E rm -rf "$(DOWNLOAD_CACHE)"

test:
	$(PYTHON) -m unittest discover -s tests -v

help:
	@echo 'make             Build, validate, package and install the selected profile'
	@echo 'make native|portable|debug   Build a named profile without installing'
	@echo 'make configure|build        Configure dependencies/project; optionally compile'
	@echo 'make verify      Smoke-test an existing build without compiling'
	@echo 'make package     Package an existing build; run lintian and extracted-runtime checks'
	@echo 'make install     Package, validate and install an existing build using apt-get'
	@echo 'make uninstall   Purge PACKAGE_NAME (default: neovim, including distro builds)'
	@echo 'make clean       Clean the selected profile; keep configuration/dependencies'
	@echo 'make distclean   Remove all profiles/dependencies/packages; preserve downloads'
	@echo 'make purge-cache Remove cached downloads; make test runs workflow regression checks'
	@echo 'Options: PROFILE=native|portable|debug CC=gcc JOBS=4 NATIVE=ON|OFF USE_MOLD=ON|OFF'
	@echo 'Overrides: ENABLE_LTO, CFLAGS, LDFLAGS, CMAKE_EXTRA_FLAGS, DEPS_CMAKE_FLAGS, PACKAGE_VERSION'
