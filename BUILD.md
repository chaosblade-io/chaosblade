# ChaosBlade Build Guide

## Overview

ChaosBlade is a powerful chaos engineering platform that supports compiling various project components on Mac, Linux, or Windows platforms. This document provides detailed instructions on how to build the ChaosBlade project.

## Requirements

### System Requirements
- **Git Version**: >= 1.8.5
- **Go Version**: Supports Go modules
- **Operating System**: macOS (Darwin), Linux, Windows

### Dependencies
- Go compiler
- Git
- Make
- Optional: Docker or Podman (for containerized builds)

## Version Management

### Automatic Version Detection
ChaosBlade supports automatically obtaining version numbers from Git Tags:
- If Git Tag exists, the version number will be automatically extracted (removing the v prefix)
- If no Git Tag exists, the default version 1.7.4 or environment variable `BLADE_VERSION` will be used

### Manual Version Setting
```bash
export BLADE_VERSION=1.8.0
make build
```

## Build Targets

### Basic Build

#### 1. Current Platform Build
```bash
# Build CLI tool
make build

# Build all components
make build_all
```

#### 2. Specific Platform Build
```bash
# Build all components for specific platform
make darwin_amd64
make darwin_arm64
make linux_amd64
make linux_arm64
make windows_amd64

# Build specific components for specific platform
make linux_amd64 MODULES=cli
make linux_amd64 MODULES=cli,os,java
make linux_amd64 MODULES=all
```

### Component List

| Component | Description | Notes |
|-----------|-------------|-------|
| `cli` | Command line tool | Core CLI tool |
| `os` | Operating system experiment scenarios | Basic resource experiment scenarios |
| `cloud` | Cloud platform experiment scenarios | Cloud service experiment scenarios |
| `middleware` | Middleware experiment scenarios | Middleware service experiment scenarios |
| `java` | Java experiment scenarios | JVM-related experiment scenarios |
| `cplus` | C/C++ experiment scenarios | C/C++ application experiment scenarios |
| `cri` | Container runtime experiment scenarios | CRI-related experiment scenarios |
| `kubernetes` | Kubernetes experiment scenarios | K8s-related experiment scenarios |
| `nsexec` | Namespace executor | Linux only, supports cross-platform compilation |
| `check_yaml` | Check specification files | Downloads check specification YAML files |

## Build Process

### 1. Pre-build Preparation
```bash
make pre_build
```
- Generate version information
- Clean and create build directories
- Set platform-specific environment variables

### 2. Component Build
Each component has an independent build process, including:
- Clone or update source code repositories
- Execute platform-specific build commands
- Copy build artifacts to target directories

### 3. Packaging
After build completion, platform-specific compressed packages are automatically generated:
- `chaosblade-{version}-{platform}_{arch}.tar.gz`

## Platform-Specific Build

### macOS (Darwin)
```bash
# AMD64 architecture
make darwin_amd64

# ARM64 architecture (Apple Silicon)
make darwin_arm64
```

### Linux
```bash
# AMD64 architecture
make linux_amd64

# ARM64 architecture
make linux_arm64
```

### Windows
```bash
# AMD64 architecture
make windows_amd64
```

## Containerized Build

### Docker Image Build
```bash
# Build Linux AMD64 image
make build_linux_amd64_image

# Build Linux ARM64 image
make build_linux_arm64_image
```

### Image Push
```bash
# Push to container image registry
make push_image
```

## Cross-Platform Compilation

### nsexec Cross-Platform Compilation
The nsexec component supports cross-compilation from macOS to Linux:

#### Automatic Compiler Detection
The system automatically detects available cross-compilation toolchains:
- `musl-gcc`
- `/usr/local/musl/bin/musl-gcc`
- `x86_64-linux-musl-gcc`
- `aarch64-linux-musl-gcc`
- `gcc`
- `aarch64-linux-gnu-gcc`

#### Containerized Compilation
If no suitable cross-compilation toolchain is available, containers can be used for compilation:
```bash
# Using Docker
make nsexec CONTAINER_RUNTIME=docker

# Using Podman
make nsexec CONTAINER_RUNTIME=podman
```

## Build Configuration

### Environment Variables
- `GOOS`: Target operating system
- `GOARCH`: Target architecture
- `BLADE_VERSION`: Version number
- `CONTAINER_RUNTIME`: Container runtime (docker/podman)
- `MODULES`: List of components to build

### Build Flags
- `CGO_ENABLED=0`: Disable CGO
- `GO111MODULE=on`: Enable Go modules
- Static linking flags: `-ldflags="-s -w"`

## Build Artifacts

### Directory Structure
```
target/
└── chaosblade-{version}-{platform}_{arch}/
    ├── bin/           # Executable files
    ├── lib/           # Library files
    └── yaml/          # Configuration files
```

### File Naming
- Executable files: `blade` (Linux/macOS) or `blade.exe` (Windows)
- Compressed packages: `chaosblade-{version}-{platform}_{arch}.tar.gz`

## Common Command Examples

### Quick Start
```bash
# Build for current platform
make build

# Build all components
make build_all
```

### Specific Platform Build
```bash
# Build for Linux AMD64 platform
make linux_amd64

# Build for macOS ARM64 platform
make darwin_arm64
```

### Component Selection Build
```bash
# Only build CLI and OS components
make linux_amd64 MODULES=cli,os

# Build all components
make linux_amd64 MODULES=all
```

### Container Image Build
```bash
# Build and push images
make build_linux_amd64_image
make build_linux_arm64_image
make push_image
```

## Troubleshooting

### Common Issues

#### 1. Git Version Too Low
```bash
# Error message
ALERTMSG="please update git to >= 1.8.5"

# Solution
# Ubuntu/Debian
sudo apt-get update && sudo apt-get install git

# macOS
brew install git
```

#### 2. Missing Cross-Compilation Toolchain
```bash
# Ubuntu/Debian
sudo apt-get install musl-tools gcc-aarch64-linux-gnu

# macOS
brew install FiloSottile/musl-cross/musl-cross
```

#### 3. Container Runtime Issues
```bash
# Check Docker status
docker info

# Check Podman status
podman info

# Manually specify container runtime
make nsexec CONTAINER_RUNTIME=docker
```

### Clean Build Artifacts
```bash
# Clean all build artifacts
make clean
```

## Testing

### Run Tests
```bash
# Run all tests
make test
```

### Test Coverage
Tests generate coverage reports:
- File: `coverage.txt`
- Mode: `atomic`

## Help Information

### View Help
```bash
make help
```

### Available Targets
```bash
# View all available make targets
make -n help
```

## Related Links

- [Project Homepage](https://github.com/chaosblade-io/chaosblade)
- [Contributing Guide](CONTRIBUTING.md)
- [Code Style Guide](docs/code_styles.md)
- [Release Process](docs/release_process.md)

