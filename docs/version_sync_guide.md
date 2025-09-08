# ChaosBlade Version Sync Guide

## Overview

ChaosBlade now supports automatic synchronization of branch configurations from Makefile to dependency versions in go.mod files. This ensures that the dependency versions used during build are consistent with the branches configured in Makefile.

## Features

- **Automatic Version Sync**: Automatically updates dependency versions in go.mod based on branch configurations in Makefile
- **Smart Version Generation**: 
  - For `master`/`main` branches, uses base version number (e.g., `v1.7.4`)
  - For other branches, generates timestamped version numbers (e.g., `v1.7.4-0.20241208094701-dev-1-7-5`)
- **Cross-platform Compatibility**: Supports macOS and Linux systems
- **Safe Backup**: Automatically backs up original go.mod file
- **Syntax Validation**: Automatically validates go.mod syntax after updates

## Usage

### 1. Manual Version Sync

```bash
# Sync versions and clean backup files
make sync_go_mod

# Or run the script directly
./scripts/sync_go_mod.sh

# Keep backup files
./scripts/sync_go_mod.sh --keep-backup
```

### 2. Automatic Sync (Recommended)

Version sync is integrated into the build process and will automatically sync when executing the following commands:

```bash
# Build for current platform
make build
make build_all

# Build for specific platforms
make linux_amd64
make darwin_amd64
# ... other platforms
```

### 3. View Help Information

```bash
# View Makefile help
make help

# View script help
./scripts/sync_go_mod.sh --help
```

## Configuration

### Branch Configuration in Makefile

The following variables control branches for each project:

```makefile
# chaosblade-exec-os
BLADE_EXEC_OS_BRANCH=master

# chaosblade-exec-middleware
BLADE_EXEC_MIDDLEWARE_BRANCH=main

# chaosblade-exec-cloud
BLADE_EXEC_CLOUD_BRANCH=main

# chaosblade-exec-cri
BLADE_EXEC_CRI_BRANCH=main

# chaosblade-exec-kubernetes
BLADE_OPERATOR_BRANCH=master

# chaosblade-exec-jvm
BLADE_EXEC_JVM_BRANCH=master

# chaosblade-exec-cplus
BLADE_EXEC_CPLUS_BRANCH=master

# chaosblade-spec-go
BLADE_SPEC_GO_BRANCH=master
```

### Version Number Generation Rules

1. **Base Version Number**: Obtained from `BLADE_VERSION` variable (automatically extracted from Git Tag by default)
2. **Branch Version Number**: All branches use branch name directly
   - `master` branch: Use `master`
   - `main` branch: Use `main`
   - Other branches: Use branch name directly (e.g., `dev-1.7.5`)

### Example

Assuming current configuration:
- `BLADE_VERSION=1.7.4`
- `BLADE_EXEC_OS_BRANCH=dev-1.7.5`
- `BLADE_EXEC_CLOUD_BRANCH=main`

Generated version numbers:
- `chaosblade-exec-os`: `dev-1.7.5` (Go will resolve this to a specific commit like `v1.7.5-0.20241208094701-b2d815847b22`)
- `chaosblade-exec-cloud`: `main` (Go will resolve this to a specific commit like `v1.7.5-0.20250902042623-6caabb19d8c5`)

## Workflow

1. **Extract Configuration**: Extract branch configurations and version information from Makefile
2. **Generate Versions**: Generate corresponding version numbers based on branch types
3. **Backup Files**: Automatically backup original go.mod file
4. **Update Dependencies**: Use sed command to update dependency versions in go.mod
5. **Validate Syntax**: Run `go mod tidy` to validate syntax correctness
6. **Clean Backup**: Clean backup files after successful validation

## Troubleshooting

### Common Issues

1. **Permission Error**: Ensure the script has execution permissions
   ```bash
   chmod +x scripts/sync_go_mod.sh
   ```

2. **sed Command Error**: The script automatically handles differences between macOS and Linux

3. **go.mod Syntax Error**: The script will automatically restore backup files

### Restore Original Files

If issues occur after updates, you can manually restore:

```bash
# If backup file exists
mv go.mod.backup go.mod

# Or re-sync
make sync_go_mod
```

## Notes

1. **Version Consistency**: Ensure branch configurations in Makefile are consistent with actually used branches
2. **Dependency Availability**: Dependencies corresponding to generated version numbers must exist in the respective repositories
3. **Build Order**: Version sync executes in the `pre_build` stage, ensuring completion before build
4. **Backup Management**: It's recommended to manually backup go.mod file before important operations

## Technical Implementation

- **Script Language**: Bash
- **Dependencies**: git, go, sed
- **Compatibility**: macOS, Linux
- **Integration Method**: Makefile target dependencies

## Related Files

- `scripts/sync_go_mod.sh`: Version sync script
- `Makefile`: Build configuration and branch definitions
- `go.mod`: Go module dependency file
- `docs/version_sync_guide.md`: This document
