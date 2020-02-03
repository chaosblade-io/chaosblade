.PHONY: build clean

export BLADE_VERSION=0.4.0

ALLOWGITVERSION=1.8.5
GITVERSION:=$(shell git --version | grep ^git | sed 's/^.* //g')

ifneq ($(strip $(firstword $(sort $(GITVERSION), $(ALLOWGITVERSION)))),$(ALLOWGITVERSION))
	ALERTMSG="please update git to >= $(ALLOWGITVERSION)"
endif

BLADE_BIN=blade
BLADE_EXPORT=chaosblade-$(BLADE_VERSION).tgz
BLADE_SRC_ROOT=`pwd`

GO_ENV=CGO_ENABLED=1
GO_MODULE=GO111MODULE=on
VERSION_PKG=github.com/chaosblade-io/chaosblade/version
GO_FLAGS=-ldflags="-X ${VERSION_PKG}.Ver=$(BLADE_VERSION) -X '${VERSION_PKG}.Env=`uname -mv`' -X '${VERSION_PKG}.BuildTime=`date`'"
GO=env $(GO_ENV) $(GO_MODULE) go

UNAME := $(shell uname)

BUILD_TARGET=target
BUILD_TARGET_FOR_JAVA_CPLUS=build-target
BUILD_TARGET_DIR_NAME=chaosblade-$(BLADE_VERSION)
BUILD_TARGET_PKG_DIR=$(BUILD_TARGET)/chaosblade-$(BLADE_VERSION)
BUILD_TARGET_PKG_NAME=$(BUILD_TARGET)/chaosblade-$(BLADE_VERSION).tar.gz
BUILD_TARGET_BIN=$(BUILD_TARGET_PKG_DIR)/bin
BUILD_TARGET_LIB=$(BUILD_TARGET_PKG_DIR)/lib
BUILD_TARGET_TAR_NAME=$(BUILD_TARGET_DIR_NAME).tar.gz
BUILD_TARGET_PKG_FILE_PATH=$(BUILD_TARGET)/$(BUILD_TARGET_TAR_NAME)
BUILD_IMAGE_PATH=build/image/blade
# cache downloaded file
BUILD_TARGET_CACHE=$(BUILD_TARGET)/cache

# chaosblade-exec-os
BLADE_EXEC_OS_PROJECT=https://github.com/chaosblade-io/chaosblade-exec-os.git
BLADE_EXEC_OS_BRANCH=master

# chaosblade-exec-docker
BLADE_EXEC_DOCKER_PROJECT=https://github.com/chaosblade-io/chaosblade-exec-docker.git
BLADE_EXEC_DOCKER_BRANCH=master

# chaosblade-exec-kubernetes
BLADE_OPERATOR_PROJECT=https://github.com/chaosblade-io/chaosblade-operator.git
BLADE_OPERATOR_BRANCH=master

# chaosblade-exec-jvm
BLADE_EXEC_JVM_PROJECT=https://github.com/chaosblade-io/chaosblade-exec-jvm.git
BLADE_EXEC_JVM_BRANCH=master

# chaosblade-exec-cplus
BLADE_EXEC_CPLUS_PROJECT=https://github.com/chaosblade-io/chaosblade-exec-cplus.git
BLADE_EXEC_CPLUS_BRANCH=master

# docker yaml
DOCKER_YAML_FILE_NAME=chaosblade-docker-spec-$(BLADE_VERSION).yaml
DOCKER_YAML_FILE_PATH=$(BUILD_TARGET_BIN)/$(DOCKER_YAML_FILE_NAME)

ifeq ($(GOOS), linux)
	GO_FLAGS=-ldflags="-linkmode external -extldflags -static -X ${VERSION_PKG}.Ver=$(BLADE_VERSION) -X '${VERSION_PKG}.Env=`uname -mv`' -X '${VERSION_PKG}.BuildTime=`date`'"
endif

# build chaosblade package and image
build: pre_build build_cli build_os build_docker build_kubernetes build_java build_cplus
	# tar package
	tar zcvf $(BUILD_TARGET_PKG_FILE_PATH) -C $(BUILD_TARGET) $(BUILD_TARGET_DIR_NAME)

# alias
cli: build_cli
os: build_os
os_darwin: build_os_darwin
docker: build_docker
kubernetes: build_kubernetes
java: build_java
cplus: build_cplus

# for example: make build_with cli os_darwin
build_with: pre_build

# for example: make build_with_linux cli os
build_with_linux: pre_build build_linux_with_arg

# build chaosblade cli: blade
build_cli:
	# build blade cli
	$(GO) build $(GO_FLAGS) -o $(BUILD_TARGET_PKG_DIR)/blade ./cli

# build os
build_os:
ifneq ($(BUILD_TARGET_CACHE)/chaosblade-exec-os, $(wildcard $(BUILD_TARGET_CACHE)/chaosblade-exec-os))
	git clone -b $(BLADE_EXEC_OS_BRANCH) $(BLADE_EXEC_OS_PROJECT) $(BUILD_TARGET_CACHE)/chaosblade-exec-os
else
ifdef ALERTMSG
	$(error $(ALERTMSG))
endif
	git -C $(BUILD_TARGET_CACHE)/chaosblade-exec-os pull origin $(BLADE_EXEC_OS_BRANCH)
endif
	make -C $(BUILD_TARGET_CACHE)/chaosblade-exec-os
	cp $(BUILD_TARGET_CACHE)/chaosblade-exec-os/$(BUILD_TARGET_BIN)/* $(BUILD_TARGET_BIN)

build_os_darwin:
ifneq ($(BUILD_TARGET_CACHE)/chaosblade-exec-os, $(wildcard $(BUILD_TARGET_CACHE)/chaosblade-exec-os))
	git clone -b $(BLADE_EXEC_OS_BRANCH) $(BLADE_EXEC_OS_PROJECT) $(BUILD_TARGET_CACHE)/chaosblade-exec-os
else
ifdef ALERTMSG
	$(error $(ALERTMSG))
endif
	git -C $(BUILD_TARGET_CACHE)/chaosblade-exec-os pull origin $(BLADE_EXEC_OS_BRANCH)
endif
	make build_darwin -C $(BUILD_TARGET_CACHE)/chaosblade-exec-os
	cp $(BUILD_TARGET_CACHE)/chaosblade-exec-os/$(BUILD_TARGET_BIN)/* $(BUILD_TARGET_BIN)

build_docker:
ifneq ($(BUILD_TARGET_CACHE)/chaosblade-exec-docker, $(wildcard $(BUILD_TARGET_CACHE)/chaosblade-exec-docker))
	git clone -b $(BLADE_EXEC_DOCKER_BRANCH) $(BLADE_EXEC_DOCKER_PROJECT) $(BUILD_TARGET_CACHE)/chaosblade-exec-docker
else
	git -C $(BUILD_TARGET_CACHE)/chaosblade-exec-docker pull origin $(BLADE_EXEC_DOCKER_BRANCH)
endif
	make -C $(BUILD_TARGET_CACHE)/chaosblade-exec-docker
	cp $(BUILD_TARGET_CACHE)/chaosblade-exec-docker/$(BUILD_TARGET_BIN)/* $(BUILD_TARGET_BIN)

build_kubernetes:
ifneq ($(BUILD_TARGET_CACHE)/chaosblade-operator, $(wildcard $(BUILD_TARGET_CACHE)/chaosblade-operator))
	git clone -b $(BLADE_OPERATOR_BRANCH) $(BLADE_OPERATOR_PROJECT) $(BUILD_TARGET_CACHE)/chaosblade-operator
else
	git -C $(BUILD_TARGET_CACHE)/chaosblade-operator pull origin $(BLADE_OPERATOR_BRANCH)
endif
	make -C $(BUILD_TARGET_CACHE)/chaosblade-operator
	cp $(BUILD_TARGET_CACHE)/chaosblade-operator/$(BUILD_TARGET_BIN)/* $(BUILD_TARGET_BIN)

build_java:
ifneq ($(BUILD_TARGET_CACHE)/chaosblade-exec-jvm, $(wildcard $(BUILD_TARGET_CACHE)/chaosblade-exec-jvm))
	git clone -b $(BLADE_EXEC_JVM_BRANCH) $(BLADE_EXEC_JVM_PROJECT) $(BUILD_TARGET_CACHE)/chaosblade-exec-jvm
else
ifdef ALERTMSG
	$(error $(ALERTMSG))
endif
	git -C $(BUILD_TARGET_CACHE)/chaosblade-exec-jvm pull origin $(BLADE_EXEC_JVM_BRANCH)
endif
	make -C $(BUILD_TARGET_CACHE)/chaosblade-exec-jvm
	cp -R $(BUILD_TARGET_CACHE)/chaosblade-exec-jvm/$(BUILD_TARGET_FOR_JAVA_CPLUS)/$(BUILD_TARGET_DIR_NAME)/* $(BUILD_TARGET_PKG_DIR)

build_cplus:
ifneq ($(BUILD_TARGET_CACHE)/chaosblade-exec-cplus, $(wildcard $(BUILD_TARGET_CACHE)/chaosblade-exec-cplus))
	git clone -b $(BLADE_EXEC_CPLUS_BRANCH) $(BLADE_EXEC_CPLUS_PROJECT) $(BUILD_TARGET_CACHE)/chaosblade-exec-cplus
else
ifdef ALERTMSG
	$(error $(ALERTMSG))
endif
	git -C $(BUILD_TARGET_CACHE)/chaosblade-exec-cplus pull origin $(BLADE_EXEC_CPLUS_BRANCH)
endif
	make -C $(BUILD_TARGET_CACHE)/chaosblade-exec-cplus
	cp -R $(BUILD_TARGET_CACHE)/chaosblade-exec-cplus/$(BUILD_TARGET_FOR_JAVA_CPLUS)/$(BUILD_TARGET_DIR_NAME)/* $(BUILD_TARGET_PKG_DIR)

# create dir or download necessary file
pre_build:mkdir_build_target
	rm -rf $(BUILD_TARGET_PKG_DIR) $(BUILD_TARGET_PKG_FILE_PATH)
	mkdir -p $(BUILD_TARGET_BIN) $(BUILD_TARGET_LIB)

# create cache dir
mkdir_build_target:
ifneq ($(BUILD_TARGET_CACHE), $(wildcard $(BUILD_TARGET_CACHE)))
	mkdir -p $(BUILD_TARGET_CACHE)
endif

# build dawrin version on mac system
build_darwin: pre_build build_cli build_os_darwin build_docker build_kubernetes build_java build_cplus
	# tar package
	tar zcvf $(BUILD_TARGET_PKG_FILE_PATH) -C $(BUILD_TARGET) $(BUILD_TARGET_DIR_NAME)

# build chaosblade linux version by docker image
build_linux:
	docker build -f build/image/musl/Dockerfile -t chaosblade-build-musl:latest build/image/musl
	docker run --rm \
		-v $(shell echo -n ${GOPATH}):/go \
		-w /go/src/github.com/chaosblade-io/chaosblade \
		chaosblade-build-musl:latest

build_linux_with_arg:
	docker build -f build/image/musl/Dockerfile -t chaosblade-build-musl:latest build/image/musl
	docker run --rm \
		-v $(shell echo -n ${GOPATH}):/go \
		-w /go/src/github.com/chaosblade-io/chaosblade \
		chaosblade-build-musl:latest build_with $$ARGS

# build chaosblade image for chaos
build_image:
	rm -rf $(BUILD_IMAGE_PATH)/$(BUILD_TARGET_DIR_NAME)
	cp -R $(BUILD_TARGET_PKG_NAME) $(BUILD_IMAGE_PATH)
	tar zxvf $(BUILD_TARGET_PKG_NAME) -C $(BUILD_IMAGE_PATH)
	docker build -f $(BUILD_IMAGE_PATH)/Dockerfile \
		--build-arg BLADE_VERSION=$(BLADE_VERSION) \
		-t chaosblade-tool:$(BLADE_VERSION) \
		$(BUILD_IMAGE_PATH)
	rm -rf $(BUILD_IMAGE_PATH)/$(BUILD_TARGET_DIR_NAME)

# build docker image with multi-stage builds
docker_image: clean
	docker build -f ./Dockerfile \
		--build-arg BLADE_VERSION=$(BLADE_VERSION) \
		-t chaosblade:$(BLADE_VERSION) $(BLADE_SRC_ROOT)

# test
test:
	$(GO) test -race -coverprofile=coverage.txt -covermode=atomic ./...
# clean all build result
clean:
	$(GO) clean ./...
	rm -rf $(BUILD_TARGET)
	rm -rf $(BUILD_IMAGE_PATH)/$(BUILD_TARGET_DIR_NAME)
