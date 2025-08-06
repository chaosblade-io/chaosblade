# GitHub Actions CI/CD for ChaosBlade

This document describes the GitHub Actions workflows configured for the ChaosBlade project.

## Overview

The CI/CD pipeline consists of multiple workflows that handle different aspects of the development lifecycle:

## Workflows

### 1. CI Workflow (`ci.yml`)

**Triggers:**
- Push to `master`, `main`, `develop` branches
- Pull requests to `master`, `main`, `develop` branches  
- Git tags starting with `v*`

**Jobs:**

#### Test Job
- Runs on Ubuntu (Linux AMD64)
- Executes unit tests with coverage reporting
- Uploads coverage to Codecov
- Verifies Go module dependencies
- Runs `go vet` for code analysis

#### Multi-Platform CLI Build
- Builds CLI binary for multiple platforms:
  - Linux AMD64/ARM64
  - macOS (Darwin) AMD64/ARM64  
  - Windows AMD64
- Uses matrix strategy for parallel builds
- Uploads build artifacts for each platform
- Installs musl-gcc for static linking on Linux

#### Full Linux AMD64 Build
- Complete build with all components (CLI, OS, Java, C++, etc.)
- Includes all ChaosBlade executors and dependencies
- Used for integration testing and full releases

#### Integration Testing
- Downloads full build artifacts
- Tests basic binary functionality
- Validates core commands work correctly

#### Release Job
- Triggered only on tag pushes (`v*`)
- Creates GitHub releases automatically
- Uploads all build artifacts to the release
- Generates release notes

### 2. Docker Workflow (`docker.yml`)

**Triggers:**
- Push to `master`, `main` branches
- Pull requests to `master`, `main` branches
- Git tags starting with `v*`

**Features:**
- Multi-architecture Docker builds (AMD64, ARM64)
- Pushes to GitHub Container Registry
- Uses Docker BuildKit caching
- Automated tagging based on git refs

### 3. Security Workflow (`security.yml`)

**Triggers:**
- Push to `master`, `main` branches
- Pull requests
- Daily scheduled runs (2:00 AM UTC)

**Security Checks:**
- **govulncheck**: Go vulnerability scanning
- **CodeQL**: Static code analysis for security issues
- **Dependency Review**: Checks for vulnerable dependencies in PRs

### 4. Lint Workflow (`lint.yml`)

**Triggers:**
- Push to `master`, `main`, `develop` branches
- Pull requests

**Linting:**
- **Go**: golangci-lint with comprehensive rule set
- **Markdown**: Documentation linting
- **YAML**: GitHub Actions workflow validation
- **Dockerfile**: Container security and best practices

### 5. Benchmark Workflow (`benchmark.yml`)

**Triggers:**
- Push to `master`, `main` branches
- Pull requests

**Features:**
- Runs Go benchmarks
- Tracks performance over time
- Alerts on performance regressions
- Comments benchmark results on PRs

## Configuration Files

### `.golangci.yml`
Configures Go linting rules including:
- Code complexity limits
- Import formatting
- Security checks (gosec)
- Style consistency

### `.yamllint.yml`
YAML formatting rules for GitHub Actions workflows

### `.markdownlint.json`
Markdown formatting rules for documentation

## Usage Examples

### Manual Workflow Triggers

```bash
# Trigger a release build
git tag v1.8.0
git push origin v1.8.0

# The CI will automatically:
# 1. Run all tests
# 2. Build for all platforms  
# 3. Create GitHub release
# 4. Upload all artifacts
```

### Local Development

```bash
# Run the same checks locally
make test                    # Run tests
make lint                   # Run linters (if configured)
make build                  # Build current platform
make linux_amd64 MODULES=all  # Full Linux build
```

### Platform-Specific Builds

The workflows support building individual components:

```bash
# In Makefile (referenced by workflows)
make linux_amd64 MODULES=cli           # CLI only
make linux_amd64 MODULES=cli,os,java   # Multiple components  
make linux_amd64 MODULES=all           # All components
```

## Artifact Downloads

Build artifacts are available for 30 days:
- **CLI builds**: `chaosblade-cli-{platform}`
- **Full builds**: `chaosblade-full-linux-amd64`

## Environment Variables

Key environment variables used:
- `GO_VERSION`: Go version for builds (currently 1.20)
- `BLADE_VERSION`: ChaosBlade version from Makefile

## Security Considerations

- All workflows use pinned action versions
- Secrets are properly scoped
- Docker images use official base images
- Static analysis catches common vulnerabilities

## Troubleshooting

### Failed Builds
1. Check the specific job logs in GitHub Actions
2. Verify Go module compatibility
3. Ensure all required build dependencies are available

### Release Issues
1. Verify tag format matches `v*` pattern
2. Check that all required jobs pass
3. Ensure GitHub token has appropriate permissions

### Performance Issues
1. Review benchmark results in PR comments
2. Check for performance regressions in alerts
3. Optimize based on benchmark feedback