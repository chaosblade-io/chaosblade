# ChaosBlade CI/CD Status

[![CI](https://github.com/chaosblade-io/chaosblade/actions/workflows/ci.yml/badge.svg)](https://github.com/chaosblade-io/chaosblade/actions/workflows/ci.yml)
[![Docker Build](https://github.com/chaosblade-io/chaosblade/actions/workflows/docker.yml/badge.svg)](https://github.com/chaosblade-io/chaosblade/actions/workflows/docker.yml)
[![Security Scan](https://github.com/chaosblade-io/chaosblade/actions/workflows/security.yml/badge.svg)](https://github.com/chaosblade-io/chaosblade/actions/workflows/security.yml)
[![Lint](https://github.com/chaosblade-io/chaosblade/actions/workflows/lint.yml/badge.svg)](https://github.com/chaosblade-io/chaosblade/actions/workflows/lint.yml)
[![Benchmark](https://github.com/chaosblade-io/chaosblade/actions/workflows/benchmark.yml/badge.svg)](https://github.com/chaosblade-io/chaosblade/actions/workflows/benchmark.yml)

## Quick Reference

### Supported Platforms
- Linux AMD64/ARM64 (with static linking)
- macOS (Darwin) AMD64/ARM64
- Windows AMD64

### Build Commands
```bash
# CLI only for specific platform
make linux_amd64 MODULES=cli

# Multiple components  
make linux_amd64 MODULES=cli,os,java

# All components
make linux_amd64 MODULES=all
make build_all  # Current platform
```

### Workflow Triggers
- **Push** to master/main/develop → Full CI pipeline
- **PR** → Tests + builds + security checks  
- **Tag** (`v*`) → Release with all artifacts
- **Daily** → Security vulnerability scan

### Artifact Retention
- Build artifacts: 30 days
- Docker images: Per registry settings
- Release assets: Permanent

For detailed documentation, see [README.md](.github/README.md)