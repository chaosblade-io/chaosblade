.PHONY: build clean dev server install run test dist package checksums \
        dev-tui build-tui install-tui test-tui typecheck-tui clean-tui

build:
	@if [ ! -d vendor/chaosblade ]; then \
		echo "ERROR: vendor/chaosblade not found."; \
		echo "Download ChaosBlade and extract to vendor/chaosblade/ before building."; \
		echo "  mkdir -p vendor && tar xzf chaosblade-*.tar.gz -C vendor/ --strip-components=1"; \
		exit 1; \
	fi
	uv run pyinstaller blade-ai.spec --noconfirm

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
build-tui:
	cd tui && npm run build

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