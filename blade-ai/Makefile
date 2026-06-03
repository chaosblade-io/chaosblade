.PHONY: build wheel wheel-platform clean dev server install run test dist package checksums \
        dev-tui build-tui install-tui test-tui typecheck-tui clean-tui

# PyInstaller standalone binary (curl-install / offline distribution).
# Self-contained: the ChaosBlade tool is bundled INTO the binary, so it
# must exist in vendor/ at build time. Auto-download the platform-matching
# tarball when missing (same version + checksums as the runtime installer
# via download_chaosblade) so a fresh checkout builds without a manual
# step. build-tui is a prerequisite — blade-ai.spec force-bundles
# tui/dist/cli.js and fails loud at Analysis time if it's absent.
build: build-tui
	@HOST=$$(uv run python -c "from chaos_agent.chaosblade_installer import _detect_platform; print(_detect_platform())"); \
	CUR=$$(cat vendor/chaosblade/.arch 2>/dev/null || echo none); \
	if [ "$$CUR" != "$$HOST" ]; then \
		echo "vendor blade arch=$$CUR != host=$$HOST — (re)downloading host ChaosBlade..."; \
		uv run python -c "from pathlib import Path; from chaos_agent.chaosblade_installer import download_chaosblade; download_chaosblade(Path('vendor/chaosblade'))"; \
	fi
	uv run pyinstaller blade-ai.spec --noconfirm

# Build the UNIVERSAL pip wheel (blade_ai-*-py3-none-any.whl). The TS TUI
# bundle is force-included by pyproject.toml, so it must exist BEFORE
# `uv build` — depend on build-tui to guarantee that. Skills are
# force-included from skills/ (always in-tree); ChaosBlade is NOT bundled —
# universal-wheel users fetch it at first injection. Platform-agnostic.
wheel: build-tui
	uv build --wheel
	@echo "Built universal wheel in dist/"

# Build a PLATFORM wheel (blade_ai-*-py3-none-<platform>.whl) with the
# ChaosBlade binary baked in, so pip-install users on that platform get it
# unpacked at install time (no runtime download).
#
#   make wheel-platform                     # host platform (auto-detected)
#   make wheel-platform TARGET=darwin_amd64 # CROSS-build for another target
#
# Cross-building works because a wheel is packaging, not compilation: we
# just download the target's prebuilt ChaosBlade binary and pack it with a
# matching platform tag — nothing here runs it. Valid TARGET values are the
# keys of _CHECKSUMS: darwin_arm64 / darwin_amd64 / linux_amd64 / linux_arm64.
# The arch→tag map lives in chaosblade_installer (single source of truth).
wheel-platform: build-tui
	@TARGET="$(TARGET)"; \
	if [ -z "$$TARGET" ]; then \
		TARGET=$$(uv run python -c "from chaos_agent.chaosblade_installer import _detect_platform; print(_detect_platform())"); \
	fi; \
	TAG=$$(uv run python -c "import sys; from chaos_agent.chaosblade_installer import wheel_platform_tag; print(wheel_platform_tag(sys.argv[1]))" "$$TARGET"); \
	echo "Building wheel  target=$$TARGET  tag=$$TAG"; \
	CUR=$$(cat vendor/chaosblade/.arch 2>/dev/null || echo none); \
	if [ "$$CUR" != "$$TARGET" ]; then \
		echo "vendor blade arch=$$CUR != target=$$TARGET — downloading $$TARGET ChaosBlade..."; \
		uv run python -c "import sys; from pathlib import Path; from chaos_agent.chaosblade_installer import download_chaosblade; download_chaosblade(Path('vendor/chaosblade'), target=sys.argv[1])" "$$TARGET"; \
	fi; \
	BLADE_AI_WHEEL_PLATFORM=$$TAG uv build --wheel; \
	echo "Built $$TAG wheel in dist/"

# --onedir mode: dist/blade-ai/ is a directory, not a single file
dist: build
	@echo "Built distribution in dist/blade-ai/"

# Package as tar.gz for release (macOS/Linux)
# File naming: blade-ai-{platform}.tar.gz (version in URL path, not filename)
package: build
	@PLATFORM=$(shell python3 -c "import platform; m=platform.machine().lower(); m={'x86_64':'amd64','aarch64':'arm64','arm64':'arm64'}.get(m,m); o=platform.system().lower(); o={'darwin':'darwin','linux':'linux'}.get(o,o); print(f'{o}-{m}')"); \
	tar -czf "blade-ai-$$PLATFORM.tar.gz" -C dist blade-ai; \
	echo "Created: blade-ai-$$PLATFORM.tar.gz"

# Generate SHA256 checksums
checksums: package
	@sha256sum blade-ai-*.tar.gz > checksums.txt || shasum -a 256 blade-ai-*.tar.gz > checksums.txt; \
	cat checksums.txt

clean:
	rm -rf build dist
	rm -f blade-ai-*.tar.gz checksums.txt
	find ./src -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
	find ./tests -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null

dev:
	uv sync

run:  ## Run any CLI command, e.g. make run cmd=list
	uv run blade-ai $(cmd)

test:
	uv run pytest tests/ -v

install:
	uv sync --extra dev

server:
	uv run blade-ai-server

# --------------------------------------------------------------------
# TS TUI (M1+)
# --------------------------------------------------------------------

# Run the TS TUI in dev mode against an already-running server.
# Start the server first in another terminal: `make server`.
dev-tui:
	cd tui && BLADE_AI_SERVER=http://127.0.0.1:8080 npm run dev

# Run the TS TUI in fully embedded mode (auto-spawn Python server).
start-tui:
	cd tui && npm start

# Bundle the TS TUI to tui/dist/cli.js (single ESM file).
# Auto-installs node_modules on a fresh checkout so `make wheel` works
# end-to-end without a manual `npm install` first.
build-tui:
	@if ! command -v npm >/dev/null 2>&1; then \
		echo "npm not found — skipping TUI build (Python-only wheel)"; \
		mkdir -p tui/dist && touch tui/dist/cli.js; \
	else \
		if [ ! -d tui/node_modules ]; then \
			echo "tui/node_modules missing — running npm install..."; \
			cd tui && npm install; \
		fi; \
		cd tui && npm run build; \
	fi

# Install the TS TUI globally (creates `blade-ai-tui` in PATH).
install-tui: build-tui
	cd tui && npm link

# Run TS TUI unit tests.
test-tui:
	cd tui && npm test

# TypeScript-only typecheck (faster than full build).
typecheck-tui:
	cd tui && npm run typecheck

clean-tui:
	cd tui && npm run clean
	rm -rf tui/node_modules