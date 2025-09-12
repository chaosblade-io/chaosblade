.PHONY: build build_all

# Version information management
# Priority: use environment variable BLADE_VERSION, otherwise auto-get version from Git Tag
ifneq ($(BLADE_VERSION),)
    # If environment variable BLADE_VERSION is set, use it directly
    # BLADE_VERSION is already defined in environment variables
else
    # If environment variable BLADE_VERSION is not set, try to get from Git Tag
    GIT_TAG := $(shell git describe --tags --abbrev=0 2>/dev/null || echo "")
    ifeq ($(GIT_TAG),)
        # If no Git Tag exists, use default version
        BLADE_VERSION := 1.7.4
    else
        # Extract version number from Git Tag (remove v prefix)
        BLADE_VERSION := $(shell echo $(GIT_TAG) | sed 's/^v//')
    endif
endif

# Export version number for use by other scripts
export BLADE_VERSION

ALLOWGITVERSION=1.8.5
GITVERSION:=$(shell git --version | grep ^git | sed 's/^.* //g')

ifneq ($(strip $(firstword $(sort $(GITVERSION), $(ALLOWGITVERSION)))),$(ALLOWGITVERSION))
	ALERTMSG="please update git to >= $(ALLOWGITVERSION)"
endif

BLADE_BIN=blade
BLADE_EXPORT=chaosblade-$(BLADE_VERSION).tgz
BLADE_SRC_ROOT=$(shell pwd)
BUILD_TARGET=target

GO_ENV=CGO_ENABLED=0
GO_MODULE=GO111MODULE=on
GO=go
VERSION_PKG=github.com/chaosblade-io/chaosblade/version
CRI_BLADE_VERSION=github.com/chaosblade-io/chaosblade-exec-cri/version
OS_BLADE_VERSION=github.com/chaosblade-io/chaosblade-exec-os/version
JVM_BLADE_VERSION=github.com/chaosblade-io/chaosblade-exec-jvm/version
K8S_BLADE_VERSION=github.com/chaosblade-io/chaosblade-operator/version

GO_X_FLAGS=-X ${VERSION_PKG}.Ver=$(BLADE_VERSION) -X '${VERSION_PKG}.Env=`uname -mv`' -X '${VERSION_PKG}.BuildTime=`date`' -X ${CRI_BLADE_VERSION}.BladeVersion=$(BLADE_VERSION) -X ${OS_BLADE_VERSION}.BladeVersion=$(BLADE_VERSION) -X ${JVM_BLADE_VERSION}.BladeVersion=$(BLADE_VERSION) -X ${K8S_BLADE_VERSION}.BladeVersion=$(BLADE_VERSION)
GO_FLAGS=-ldflags="$(GO_X_FLAGS) -s -w"

# Build parameters for different platforms
BUILD_CMD = env CGO_ENABLED=0 GOOS=$(1) GOARCH=$(2) $(GO_MODULE) go build -ldflags="$(GO_X_FLAGS) -s -w" -o $(3) ./cli

UNAME := $(shell uname)
# Default platform variables
GOOS ?= $(shell go env GOOS)
GOARCH ?= $(shell go env GOARCH)

# Platform-specific directory and package names
get_platform_dir_name = chaosblade-$(BLADE_VERSION)-$(1)_$(2)
get_platform_pkg_name = $(BUILD_TARGET)/chaosblade-$(BLADE_VERSION)-$(1)_$(2).tar.gz

# Common build directory function
get_build_output_dir = $(BUILD_TARGET)/$(call get_platform_dir_name,$(GOOS),$(GOARCH))

# Use functions to uniformly manage platform-related path variables
BUILD_TARGET_LIB=$(call get_build_output_dir)/lib
BUILD_TARGET_BIN=$(call get_build_output_dir)/bin
BUILD_TARGET_YAML=$(call get_build_output_dir)/yaml
BUILD_TARGET_PKG_FILE_PATH=$(call get_platform_pkg_name,$(GOOS),$(GOARCH))

BUILD_IMAGE_PATH=build/image/blade
BUILD_ARM_IMAGE_PATH=build/image/blade_arm
# cache downloaded file
BUILD_TARGET_CACHE=$(BUILD_TARGET)/cache

# chaosblade-exec-os
BLADE_EXEC_OS_PROJECT=https://github.com/chaosblade-io/chaosblade-exec-os.git
BLADE_EXEC_OS_BRANCH=master

# chaosblade-exec-middleware
BLADE_EXEC_MIDDLEWARE_PROJECT=https://github.com/chaosblade-io/chaosblade-exec-middleware.git
BLADE_EXEC_MIDDLEWARE_BRANCH=main

# chaosblade-exec-cloud
BLADE_EXEC_CLOUD_PROJECT=https://github.com/chaosblade-io/chaosblade-exec-cloud.git
BLADE_EXEC_CLOUD_BRANCH=main

# chaosblade-exec-cri
BLADE_EXEC_CRI_PROJECT=https://github.com/chaosblade-io/chaosblade-exec-cri.git
BLADE_EXEC_CRI_BRANCH=main

# chaosblade-exec-kubernetes
BLADE_OPERATOR_PROJECT=https://github.com/chaosblade-io/chaosblade-operator.git
BLADE_OPERATOR_BRANCH=master

# chaosblade-exec-jvm
BLADE_EXEC_JVM_PROJECT=https://github.com/chaosblade-io/chaosblade-exec-jvm.git
BLADE_EXEC_JVM_BRANCH=master

# chaosblade-exec-cplus
BLADE_EXEC_CPLUS_PROJECT=https://github.com/chaosblade-io/chaosblade-exec-cplus.git
BLADE_EXEC_CPLUS_BRANCH=master

# chaosblade-spec-go
BLADE_SPEC_GO_PROJECT=https://github.com/chaosblade-io/chaosblade-spec-go.git
BLADE_SPEC_GO_BRANCH=master

# cri yaml
CRI_YAML_FILE_NAME=chaosblade-cri-spec-$(BLADE_VERSION).yaml
CRI_YAML_FILE_PATH=$(BUILD_TARGET_BIN)/$(CRI_YAML_FILE_NAME)

# check yaml
CHECK_YAML_FILE_NAME=chaosblade-check-spec-$(BLADE_VERSION).yaml
CHECK_YANL_FILE_OSS=https://chaosblade.oss-cn-hangzhou.aliyuncs.com/agent/github/chaosblade-check-spec.yaml
CHECK_YAML_FILE_PATH=$(BUILD_TARGET_YAML)/$(CHECK_YAML_FILE_NAME)

# Cross-compilation CC detection for nsexec
define detect_cc
$(strip $(if $(and $(filter amd64,$(GOARCH)),$(shell command -v musl-gcc 2>/dev/null)),musl-gcc,\
$(if $(and $(filter amd64,$(GOARCH)),$(wildcard /usr/local/musl/bin/musl-gcc)),/usr/local/musl/bin/musl-gcc,\
$(if $(and $(filter amd64,$(GOARCH)),$(shell command -v x86_64-linux-musl-gcc 2>/dev/null)),x86_64-linux-musl-gcc,\
$(if $(and $(filter arm64,$(GOARCH)),$(shell command -v aarch64-linux-musl-gcc 2>/dev/null)),aarch64-linux-musl-gcc,\
$(if $(and $(filter amd64,$(GOARCH)),$(shell command -v gcc 2>/dev/null)),gcc,\
$(if $(and $(filter arm64,$(GOARCH)),$(shell command -v gcc 2>/dev/null)),gcc,\
$(if $(and $(filter arm64,$(GOARCH)),$(shell command -v aarch64-linux-gnu-gcc 2>/dev/null)),aarch64-linux-gnu-gcc,\
container))))))))
endef
CC_FOR_NSEXEC := $(call detect_cc)

# Container runtime configuration - compatible with Docker and Podman
# Auto-detect available container runtime
ifeq ($(CONTAINER_RUNTIME),)
    ifeq ($(shell command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1 && echo "podman"),podman)
        CONTAINER_RUNTIME := podman
    else ifeq ($(shell command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && echo "docker"),docker)
        CONTAINER_RUNTIME := docker
    else
        CONTAINER_RUNTIME := docker
    endif
endif


##@ Build

# Common build target supporting specified platform and components
# Usage examples:
#   make build                            # Build cli for current platform
#   make darwin_amd64 MODULES=cli         # Build cli for darwin_amd64 platform
#   make darwin_amd64 MODULES=cli,os,java # Build cli, os, java for darwin_amd64 platform
#   make build_all                        # Build all components for current platform
build: pre_build cli

# Generate version information
generate_version: ## Generate version information from Git
	@echo "Generating version information..."
	@echo "Git Tag: $(GIT_TAG)"
	@echo "Blade Version: $(BLADE_VERSION)"
	@chmod +x scripts/version.sh
	@./scripts/version.sh

# Sync go.mod dependency versions
sync_go_mod: ## Sync go.mod dependencies with Makefile branch configuration
	@echo "Syncing go.mod dependencies with Makefile branch configuration..."
	@chmod +x scripts/sync_go_mod.sh
	@./scripts/sync_go_mod.sh

build_all: pre_build nsexec os cloud middleware java cplus cri kubernetes cli upx package check_yaml  ## Build all components for current platform
	@echo "Build all components for current platform completed"

pre_build: generate_version sync_go_mod ## Prepare build environment
	@if [ -n "$(GOOS)" ] && [ -n "$(GOARCH)" ]; then \
		rm -rf $(BUILD_TARGET)/$(call get_platform_dir_name,$(GOOS),$(GOARCH)) $(call get_platform_pkg_name,$(GOOS),$(GOARCH)); \
		mkdir -p $(BUILD_TARGET)/$(call get_platform_dir_name,$(GOOS),$(GOARCH))/bin $(BUILD_TARGET)/$(call get_platform_dir_name,$(GOOS),$(GOARCH))/lib $(BUILD_TARGET)/$(call get_platform_dir_name,$(GOOS),$(GOARCH))/yaml; \
	else \
		rm -rf $(call get_build_output_dir) $(BUILD_TARGET_PKG_FILE_PATH); \
		mkdir -p $(BUILD_TARGET_BIN) $(BUILD_TARGET_LIB) $(BUILD_TARGET_YAML); \
	fi

#----------------------------------------------------------------------------------
# Multi-platform build targets
.PHONY: darwin_amd64 linux_amd64 windows_amd64 darwin_arm64 linux_arm64

# Common build target supporting specified platform and components
# Usage: make [platform] MODULES=[components]
# Example: make linux_amd64 MODULES=cli,os,java
darwin_amd64 linux_amd64 windows_amd64 darwin_arm64 linux_arm64:
	@$(eval GOOS := $(word 1,$(subst _, ,$@)))
	@$(eval GOARCH := $(word 2,$(subst _, ,$@)))
	@$(MAKE) pre_build GOOS=$(GOOS) GOARCH=$(GOARCH)
	@MODULES_VAL="$(MODULES)"; \
	if [ -n "$$MODULES_VAL" ]; then \
		$(MAKE) _build_platform GOOS=$(GOOS) GOARCH=$(GOARCH) COMPONENTS="$$MODULES_VAL"; \
	else \
		$(MAKE) _build_platform GOOS=$(GOOS) GOARCH=$(GOARCH) COMPONENTS=""; \
	fi

# Prevent make from treating comma-separated components as separate targets
%:
	@:

# Common platform build function
.PHONY: _build_platform
_build_platform:
	@echo "Building for $(GOOS)/$(GOARCH)"
	@$(eval PLATFORM_DIR_NAME := $(call get_platform_dir_name,$(GOOS),$(GOARCH)))
	@$(eval OUTPUT_DIR := $(BUILD_TARGET)/$(PLATFORM_DIR_NAME))
	@mkdir -p $(OUTPUT_DIR)/bin $(OUTPUT_DIR)/lib $(OUTPUT_DIR)/yaml
	@if [ -n "$(COMPONENTS)" ]; then \
		if [ "$(COMPONENTS)" = "all" ]; then \
			components="os cloud middleware java cri kubernetes cli nsexec upx check_yaml"; \
		else \
			components=`echo "$(COMPONENTS)" | tr ',' ' '`; \
		fi; \
		for component in $$components; do \
			case "$$component" in \
				"cli") $(MAKE) cli GOOS=$(GOOS) GOARCH=$(GOARCH); ;; \
				"os") $(MAKE) os GOOS=$(GOOS) GOARCH=$(GOARCH); ;; \
				"cloud") $(MAKE) cloud GOOS=$(GOOS) GOARCH=$(GOARCH); ;; \
				"middleware") $(MAKE) middleware GOOS=$(GOOS) GOARCH=$(GOARCH); ;; \
				"java") $(MAKE) java GOOS=$(GOOS) GOARCH=$(GOARCH); ;; \
				"cplus") $(MAKE) cplus GOOS=$(GOOS) GOARCH=$(GOARCH); ;; \
				"cri") $(MAKE) cri GOOS=$(GOOS) GOARCH=$(GOARCH); ;; \
				"kubernetes") $(MAKE) kubernetes GOOS=$(GOOS) GOARCH=$(GOARCH); ;; \
				"nsexec") $(MAKE) nsexec GOOS=$(GOOS) GOARCH=$(GOARCH); ;; \
				"upx") $(MAKE) upx GOOS=$(GOOS) GOARCH=$(GOARCH); ;; \
				"check_yaml") $(MAKE) check_yaml; ;; \
				*) echo "Unknown component: $$component"; ;; \
			esac; \
		done; \
	else \
		$(MAKE) cli GOOS=$(GOOS) GOARCH=$(GOARCH); \
	fi
	@$(MAKE) _package_$(GOOS)_$(GOARCH) PLATFORM_DIR_NAME=$(PLATFORM_DIR_NAME)

#----------------------------------------------------------------------------------
# UPX compression for binary files
.PHONY: upx

upx: ## Compress binary files using UPX for maximum compression
	@$(eval OUTPUT_DIR := $(call get_build_output_dir))
	@echo "Compressing binary files with UPX in $(OUTPUT_DIR)..."
	@if command -v upx >/dev/null 2>&1; then \
		if [ "$(GOOS)" = "linux" ] || [ "$(GOOS)" = "windows" ]; then \
			echo "UPX found, compressing binaries for $(GOOS)..."; \
			find $(OUTPUT_DIR) -type f \( -name "blade" -o -name "blade.exe" -o -name "chaos_*" -o -name "nsexec" -o -name "*.exe" \) | while read file; do \
				if [ -x "$$file" ]; then \
					echo "Compressing: $$file"; \
					upx --best --lzma "$$file" || echo "Warning: Failed to compress $$file"; \
				fi; \
			done; \
			echo "UPX compression completed"; \
		else \
			echo "UPX compression skipped for $(GOOS) - not supported by UPX"; \
			echo "UPX currently supports: Linux, Windows"; \
		fi; \
	else \
		echo "Warning: UPX not found, skipping compression"; \
		echo "To install UPX:"; \
		echo "  - macOS: brew install upx"; \
		echo "  - Ubuntu/Debian: apt-get install upx-ucl"; \
		echo "  - CentOS/RHEL: yum install upx"; \
		echo "  - Or download from: https://upx.github.io/"; \
	fi

#----------------------------------------------------------------------------------
# Platform-specific packaging
.PHONY: _package_darwin_amd64 _package_linux_amd64 _package_windows_amd64 _package_darwin_arm64 _package_linux_arm64

_package_darwin_amd64:
	@echo "Packaging for darwin amd64..."
	@$(MAKE) upx GOOS=darwin GOARCH=amd64
	@tar --no-xattrs -zcvf $(call get_platform_pkg_name,darwin,amd64) -C $(BUILD_TARGET) $(PLATFORM_DIR_NAME)

_package_linux_amd64:
	@echo "Packaging for linux amd64..."
	@$(MAKE) upx GOOS=linux GOARCH=amd64
	@COPYFILE_DISABLE=1 tar --no-xattrs -zcvf $(call get_platform_pkg_name,linux,amd64) -C $(BUILD_TARGET) $(PLATFORM_DIR_NAME)

_package_windows_amd64:
	@echo "Packaging for windows amd64..."
	@$(MAKE) upx GOOS=windows GOARCH=amd64
	@$(eval OUTPUT_DIR := $(BUILD_TARGET)/$(PLATFORM_DIR_NAME))
	@if [ -f "$(OUTPUT_DIR)/blade-windows-amd64.exe" ]; then mv $(OUTPUT_DIR)/blade-windows-amd64.exe $(OUTPUT_DIR)/blade.exe; fi
	@COPYFILE_DISABLE=1 tar --no-xattrs -zcvf $(call get_platform_pkg_name,windows,amd64) -C $(BUILD_TARGET) $(PLATFORM_DIR_NAME)

_package_darwin_arm64:
	@echo "Packaging for darwin arm64..."
	@$(MAKE) upx GOOS=darwin GOARCH=arm64
	@tar --no-xattrs -zcvf $(call get_platform_pkg_name,darwin,arm64) -C $(BUILD_TARGET) $(PLATFORM_DIR_NAME)

_package_linux_arm64:
	@echo "Packaging for linux arm64..."
	@$(MAKE) upx GOOS=linux GOARCH=arm64
	@COPYFILE_DISABLE=1 tar --no-xattrs -zcvf $(call get_platform_pkg_name,linux,arm64) -C $(BUILD_TARGET) $(PLATFORM_DIR_NAME)
	
#----------------------------------------------------------------------------------

# build chaosblade cli: blade
.PHONY:cli
cli: ## Build blade cli
	@$(eval OUTPUT_DIR := $(call get_build_output_dir))
	@echo "Building blade cli for $(GOOS)/$(GOARCH) to $(OUTPUT_DIR)"
	@$(call BUILD_CMD,$(GOOS),$(GOARCH),$(OUTPUT_DIR)/blade)

nsexec: ## Build nsexec for Linux (supports cross-compilation from macOS)
ifeq ($(GOOS),linux)
	@echo "Detected CC for nsexec: $(CC_FOR_NSEXEC)"
	@$(eval OUTPUT_DIR := $(call get_build_output_dir))
	@if [ "$(CC_FOR_NSEXEC)" != "container" ]; then \
		echo "Building nsexec for Linux $(GOARCH) using $(CC_FOR_NSEXEC)..."; \
		$(CC_FOR_NSEXEC) -static nsexec.c -o $(OUTPUT_DIR)/bin/nsexec; \
	elif command -v $(CONTAINER_RUNTIME) >/dev/null 2>&1 && $(CONTAINER_RUNTIME) info >/dev/null 2>&1; then \
		echo "Building nsexec for Linux $(GOARCH) using $(CONTAINER_RUNTIME)..."; \
		$(CONTAINER_RUNTIME) run --rm -v $(PWD):/src:Z -w /src --platform linux/$(GOARCH) alpine:latest sh -c "apk add --no-cache musl-dev gcc && gcc -static nsexec.c -o /src/$(OUTPUT_DIR)/bin/nsexec"; \
	else \
		echo "Warning: No suitable cross-compilation toolchain found for nsexec"; \
		echo "Available options:"; \
		echo "  1. Install musl-tools: apt-get install musl-tools (Ubuntu/Debian)"; \
		echo "  2. Install musl-gcc: brew install FiloSottile/musl-cross/musl-cross (macOS)"; \
		echo "  3. Install specific cross-compilers for ARM64: apt-get install gcc-aarch64-linux-gnu g++-aarch64-linux-gnu"; \
		echo "  4. Use Docker/Podman with proper platform emulation"; \
	fi
else
	@echo "Skipping nsexec build on $(GOOS) for target - Linux only"
endif

os: ## Build basic resource experimental scenarios.
ifneq ($(BUILD_TARGET_CACHE)/chaosblade-exec-os, $(wildcard $(BUILD_TARGET_CACHE)/chaosblade-exec-os))
	git clone -b $(BLADE_EXEC_OS_BRANCH) $(BLADE_EXEC_OS_PROJECT) $(BUILD_TARGET_CACHE)/chaosblade-exec-os
else
ifdef ALERTMSG
	$(error $(ALERTMSG))
endif
	git -C $(BUILD_TARGET_CACHE)/chaosblade-exec-os pull origin $(BLADE_EXEC_OS_BRANCH)
endif
	@if [ -z "$(GOOS)" ]; then \
		make -C $(BUILD_TARGET_CACHE)/chaosblade-exec-os; \
	else \
		make -C $(BUILD_TARGET_CACHE)/chaosblade-exec-os $(GOOS)_$(GOARCH); \
	fi
	@$(eval OUTPUT_DIR := $(call get_build_output_dir)) \
	cp -R $(BUILD_TARGET_CACHE)/chaosblade-exec-os/target/$(call get_platform_dir_name,$(GOOS),$(GOARCH))/* $(OUTPUT_DIR)/

middleware: ## Build middleware experimental scenarios.
ifneq ($(BUILD_TARGET_CACHE)/chaosblade-exec-middleware, $(wildcard $(BUILD_TARGET_CACHE)/chaosblade-exec-middleware))
	git clone -b $(BLADE_EXEC_MIDDLEWARE_BRANCH) $(BLADE_EXEC_MIDDLEWARE_PROJECT) $(BUILD_TARGET_CACHE)/chaosblade-exec-middleware
else
ifdef ALERTMSG
	$(error $(ALERTMSG))
endif
	git -C $(BUILD_TARGET_CACHE)/chaosblade-exec-middleware pull origin $(BLADE_EXEC_MIDDLEWARE_BRANCH)
endif
	@if [ -z "$(GOOS)" ]; then \
		make -C $(BUILD_TARGET_CACHE)/chaosblade-exec-middleware; \
	else \
		make -C $(BUILD_TARGET_CACHE)/chaosblade-exec-middleware $(GOOS)_$(GOARCH); \
	fi
	@$(eval OUTPUT_DIR := $(call get_build_output_dir)) \
	cp -R $(BUILD_TARGET_CACHE)/chaosblade-exec-middleware/target/$(call get_platform_dir_name,$(GOOS),$(GOARCH))/* $(OUTPUT_DIR)/

cloud: ## Build cloud experimental scenarios.
ifneq ($(BUILD_TARGET_CACHE)/chaosblade-exec-cloud, $(wildcard $(BUILD_TARGET_CACHE)/chaosblade-exec-cloud))
	git clone -b $(BLADE_EXEC_CLOUD_BRANCH) $(BLADE_EXEC_CLOUD_PROJECT) $(BUILD_TARGET_CACHE)/chaosblade-exec-cloud
else
ifdef ALERTMSG
	$(error $(ALERTMSG))
endif
	git -C $(BUILD_TARGET_CACHE)/chaosblade-exec-cloud pull origin $(BLADE_EXEC_CLOUD_BRANCH)
endif
	@if [ -z "$(GOOS)" ]; then \
		make -C $(BUILD_TARGET_CACHE)/chaosblade-exec-cloud; \
	else \
		make -C $(BUILD_TARGET_CACHE)/chaosblade-exec-cloud $(GOOS)_$(GOARCH); \
	fi
	@$(eval OUTPUT_DIR := $(call get_build_output_dir)) \
	cp -R $(BUILD_TARGET_CACHE)/chaosblade-exec-cloud/target/$(call get_platform_dir_name,$(GOOS),$(GOARCH))/* $(OUTPUT_DIR)/



java: ## Build java experimental scenarios.
ifneq ($(BUILD_TARGET_CACHE)/chaosblade-exec-jvm, $(wildcard $(BUILD_TARGET_CACHE)/chaosblade-exec-jvm))
	git clone -b $(BLADE_EXEC_JVM_BRANCH) $(BLADE_EXEC_JVM_PROJECT) $(BUILD_TARGET_CACHE)/chaosblade-exec-jvm
else
ifdef ALERTMSG
	$(error $(ALERTMSG))
endif
	git -C $(BUILD_TARGET_CACHE)/chaosblade-exec-jvm pull origin $(BLADE_EXEC_JVM_BRANCH)
endif
	make -C $(BUILD_TARGET_CACHE)/chaosblade-exec-jvm; 
	@$(eval OUTPUT_DIR := $(call get_build_output_dir))
	cp -R $(BUILD_TARGET_CACHE)/chaosblade-exec-jvm/build-target/chaosblade-$(BLADE_VERSION)/* $(OUTPUT_DIR)/

cplus: ## Build c/c++ experimental scenarios.
ifneq ($(BUILD_TARGET_CACHE)/chaosblade-exec-cplus, $(wildcard $(BUILD_TARGET_CACHE)/chaosblade-exec-cplus))
	git clone -b $(BLADE_EXEC_CPLUS_BRANCH) $(BLADE_EXEC_CPLUS_PROJECT) $(BUILD_TARGET_CACHE)/chaosblade-exec-cplus
else
ifdef ALERTMSG
	$(error $(ALERTMSG))
endif
	git -C $(BUILD_TARGET_CACHE)/chaosblade-exec-cplus pull origin $(BLADE_EXEC_CPLUS_BRANCH)
endif
	@if [ -z "$(GOOS)" ]; then \
		make -C $(BUILD_TARGET_CACHE)/chaosblade-exec-cplus; \
	else \
		make -C $(BUILD_TARGET_CACHE)/chaosblade-exec-cplus $(GOOS)_$(GOARCH); \
	fi
	@$(eval OUTPUT_DIR := $(call get_build_output_dir))
	@cp -R $(BUILD_TARGET_CACHE)/chaosblade-exec-cplus/target/$(call get_platform_dir_name,$(GOOS),$(GOARCH))/* $(OUTPUT_DIR)/


cri: ## Build cri experimental scenarios.
ifneq ($(BUILD_TARGET_CACHE)/chaosblade-exec-cri, $(wildcard $(BUILD_TARGET_CACHE)/chaosblade-exec-cri))
	git clone -b $(BLADE_EXEC_CRI_BRANCH) $(BLADE_EXEC_CRI_PROJECT) $(BUILD_TARGET_CACHE)/chaosblade-exec-cri
else
	git -C $(BUILD_TARGET_CACHE)/chaosblade-exec-cri pull origin $(BLADE_EXEC_CRI_BRANCH)
endif
	@$(eval OUTPUT_DIR := $(call get_build_output_dir)) 
	@if [ -z "$(GOOS)" ]; then \
		make -C $(BUILD_TARGET_CACHE)/chaosblade-exec-cri; \
	else \
		make -C $(BUILD_TARGET_CACHE)/chaosblade-exec-cri $(GOOS)_$(GOARCH); \
	fi
	@cp -R $(BUILD_TARGET_CACHE)/chaosblade-exec-cri/target/$(call get_platform_dir_name,$(GOOS),$(GOARCH))/* $(OUTPUT_DIR)/

kubernetes: ## Build kubernetes experimental scenarios.
ifneq ($(BUILD_TARGET_CACHE)/chaosblade-operator, $(wildcard $(BUILD_TARGET_CACHE)/chaosblade-operator))
	git clone -b $(BLADE_OPERATOR_BRANCH) $(BLADE_OPERATOR_PROJECT) $(BUILD_TARGET_CACHE)/chaosblade-operator
else
	git -C $(BUILD_TARGET_CACHE)/chaosblade-operator pull origin $(BLADE_OPERATOR_BRANCH)
endif
	@$(eval OUTPUT_DIR := $(call get_build_output_dir))
	@if [ "$(GOOS)_$(GOARCH)" == "linux_amd64" ] || [ "$(GOOS)_$(GOARCH)" == "linux_arm64" ]; then \
		make -C $(BUILD_TARGET_CACHE)/chaosblade-operator $(GOOS)_$(GOARCH); \
	else \
		make -C $(BUILD_TARGET_CACHE)/chaosblade-operator only_yaml; \
	fi; \
	cp -R $(BUILD_TARGET_CACHE)/chaosblade-operator/$(BUILD_TARGET)/chaosblade-$(BLADE_VERSION)/* $(OUTPUT_DIR)/


#----------------------------------------------------------------------------------
# build image with all components

build_linux_amd64_image:
	@echo "Building linux amd64 image..."
	@if [ -n "$(MODULES)" ]; then \
		make linux_amd64 MODULES=$(MODULES); \
	else \
		make linux_amd64 MODULES=all; \
	fi
	$(CONTAINER_RUNTIME) buildx build \
		--build-arg BLADE_VERSION=${BLADE_VERSION} \
		--build-arg GOOS=linux \
		--build-arg GOARCH=amd64 \
		-f build/image/blade/Dockerfile \
		--platform=linux/amd64 \
		-t ghcr.io/chaosblade-io/chaosblade-tool:${BLADE_VERSION} .

build_linux_arm64_image:
	@echo "Building linux arm64 image..."
	@if [ -n "$(MODULES)" ]; then \
		make linux_arm64 MODULES=$(MODULES); \
	else \
		make linux_arm64 MODULES=all; \
	fi
	$(CONTAINER_RUNTIME) buildx build \
		--build-arg BLADE_VERSION=${BLADE_VERSION} \
		--build-arg GOOS=linux \
		--build-arg GOARCH=arm64 \
		-f build/image/blade_arm/Dockerfile \
		--platform=linux/arm64 \
		-t ghcr.io/chaosblade-io/chaosblade-tool-arm64:${BLADE_VERSION} .

push_image:
	$(CONTAINER_RUNTIME) push ghcr.io/chaosblade-io/chaosblade-tool:${BLADE_VERSION}
	$(CONTAINER_RUNTIME) push ghcr.io/chaosblade-io/chaosblade-tool-arm64:${BLADE_VERSION}

#----------------------------------------------------------------------------------


#----------------------------------------------------------------------------------
# Keep using mirrors to achieve cross-platform compilation and solve the problem of generating yaml files according to the target platform
## Select scenario build linux version by docker image
# ghcr.io/chaosblade-io/chaosblade-build-musl image is built by build/image/musl/Dockerfile
cross_build_linux_amd64_by_container:
	$(CONTAINER_RUNTIME) run --rm \
		-v $(shell echo "$${GOPATH:-$$HOME/go}"):/go \
		-w /go/src/github.com/chaosblade-io/chaosblade \
		-v ~/.m2/repository:/root/.m2/repository \
        -v $(shell pwd):/go/src/github.com/chaosblade-io/chaosblade \
		-e MODULES=$(MODULES) \
		-e BLADE_VERSION=$(BLADE_VERSION) \
		ghcr.io/chaosblade-io/chaosblade-build-musl:latest linux_amd64


## Select scenario build linux arm version by docker image
#  ghcr.io/chaosblade-io/chaosblade-build-arm image is built by build/image/arm/Dockerfile
cross_build_linux_arm64_by_container:
	$(CONTAINER_RUNTIME) run --rm --privileged multiarch/qemu-user-static:register --reset
	$(CONTAINER_RUNTIME) run --rm \
		-v $(shell echo "$${GOPATH:-$$HOME/go}"):/go \
		-w /go/src/github.com/chaosblade-io/chaosblade \
		-v ~/.m2/repository:/root/.m2/repository \
		-v $(shell pwd):/go/src/github.com/chaosblade-io/chaosblade \
		-e MODULES=$(MODULES) \
		-e BLADE_VERSION=$(BLADE_VERSION) \
		ghcr.io/chaosblade-io/chaosblade-build-arm:latest linux_arm64


#----------------------------------------------------------------------------------

test: ## Test
	$(GO) test -race -coverprofile=coverage.txt -covermode=atomic ./build/spec ./data ./version ./exec/jvm ./exec/os ./exec/docker ./exec/kubernetes ./exec/cri ./exec/cplus ./exec/cloud ./exec/middleware

# clean all build result
clean: ## Clean
	$(GO) clean ./...
	rm -rf $(BUILD_TARGET)
	rm -rf $(BUILD_IMAGE_PATH)/chaosblade-$(BLADE_VERSION)-$(GOOS)_$(GOARCH)

package: ## Generate the tar packages
	tar --no-xattrs -zcvf $(BUILD_TARGET_PKG_FILE_PATH) -C $(BUILD_TARGET) chaosblade-$(BLADE_VERSION)-$(GOOS)_$(GOARCH)

check_yaml:
	@$(eval OUTPUT_DIR := $(call get_build_output_dir)) \
	OUTPUT_PATH=$(OUTPUT_DIR)/yaml/$(CHECK_YAML_FILE_NAME); \
	if command -v wget >/dev/null 2>&1; then \
		wget "$(CHECK_YANL_FILE_OSS)" -O $$OUTPUT_PATH; \
	elif command -v curl >/dev/null 2>&1; then \
		curl -sSL "$(CHECK_YANL_FILE_OSS)" -o $$OUTPUT_PATH; \
	else \
		echo "Warning: Neither wget nor curl found, skipping check_yaml"; \
	fi

help:
	@echo ''
	@echo 'ChaosBlade is a powerful and versatile chaos engineering platform.'
	@echo 'You can compile each project of ChaosBlade on Mac, Linux or Windows platform.'
	@echo ''
	@echo 'Usage:'
	@echo '  make <target>'
	@echo ''
	@echo 'Main targets:'
	@printf '  \033[36m%-20s\033[0m  %s\n' "build" "Build for current platform (backward compatibility)"
	@printf '  \033[36m%-20s\033[0m  %s\n' "build_all" "Build for current platform with all dependencies"
	@printf '  \033[36m%-20s\033[0m  %s\n' "darwin_amd64" "Build for Darwin/macOS AMD64"
	@printf '  \033[36m%-20s\033[0m  %s\n' "darwin_arm64" "Build for Darwin/macOS ARM64"
	@printf '  \033[36m%-20s\033[0m  %s\n' "linux_amd64" "Build for Linux AMD64"
	@printf '  \033[36m%-20s\033[0m  %s\n' "linux_arm64" "Build for Linux ARM64"
	@printf '  \033[36m%-20s\033[0m  %s\n' "windows_amd64" "Build for Windows AMD64"
	@printf '  \033[36m%-20s\033[0m  %s\n' "sync_go_mod" "Sync go.mod dependencies with Makefile branch config"
	@printf '  \033[36m%-20s\033[0m  %s\n' "build_linux_amd64_image" "Build Docker image for Linux AMD64 (supports MODULES)"
	@printf '  \033[36m%-20s\033[0m  %s\n' "build_linux_arm64_image" "Build Docker image for Linux ARM64 (supports MODULES)"
	@printf '  \033[36m%-20s\033[0m  %s\n' "push_image" "Push Docker images to registry"
	@printf '  \033[36m%-20s\033[0m  %s\n' "clean" "Clean build artifacts"
	@printf '  \033[36m%-20s\033[0m  %s\n' "test" "Run tests"
	@echo ''
	@echo 'Examples:'
	@echo '  make build                                  # Build cli for current platform'
	@echo '  make linux_amd64 MODULES=cli                # Build cli for linux_amd64'
	@echo '  make linux_amd64 MODULES=cli,os,java        # Build cli, os, java for linux_amd64'
	@echo '  make linux_amd64 MODULES=all                # Build all components for linux_amd64'
	@echo '  make build_all                              # Build all components for current platform'
	@echo '  make sync_go_mod                            # Sync go.mod with Makefile branch config'
	@echo '  make build_linux_amd64_image                # Build Docker image for Linux AMD64'
	@echo '  make build_linux_amd64_image MODULES=middleware  # Build Docker image with only middleware'
	@echo '  make build_linux_arm64_image                # Build Docker image for Linux ARM64'
	@echo '  make push_image                             # Push Docker images to registry'
	@echo ''
	@echo 'Component list:'
	@echo '  cli, os, cloud, middleware, cri, cplus, java, kubernetes, nsexec, upx, check_yaml'
	@echo '  Use "all" to build all components'
	@echo ''
	@echo 'For more details, visit https://github.com/chaosblade-io/chaosblade'