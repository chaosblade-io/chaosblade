.PHONY: build clean

export BLADE_VERSION=0.4.0

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
BLADE_EXEC_OS_BRANCH=v0.4.0

# chaosblade-exec-docker
BLADE_EXEC_DOCKER_PROJECT=https://github.com/chaosblade-io/chaosblade-exec-docker.git
BLADE_EXEC_DOCKER_BRANCH=v0.4.0

# chaosblade-exec-kubernetes
BLADE_OPERATOR_PROJECT=https://github.com/chaosblade-io/chaosblade-operator.git
BLADE_OPERATOR_BRANCH=v0.4.0

# oss url
BLADE_OSS_URL=https://chaosblade.oss-cn-hangzhou.aliyuncs.com/agent/release
# used to transform java class
JVM_SANDBOX_VERSION=1.2.0
JVM_SANDBOX_NAME=sandbox-$(JVM_SANDBOX_VERSION)-bin.zip
JVM_SANDBOX_OSS_URL=https://ompc.oss-cn-hangzhou.aliyuncs.com/jvm-sandbox/release/$(JVM_SANDBOX_NAME)
JVM_SANDBOX_DEST_PATH=$(BUILD_TARGET_CACHE)/$(JVM_SANDBOX_NAME)
# used to execute jvm chaos
BLADE_JAVA_AGENT_VERSION=0.4.0
BLADE_JAVA_AGENT_NAME=chaosblade-java-agent-$(BLADE_JAVA_AGENT_VERSION).jar
BLADE_JAVA_AGENT_DOWNLOAD_URL=$(BLADE_OSS_URL)/$(BLADE_JAVA_AGENT_NAME)
BLADE_JAVA_AGENT_DEST_PATH=$(BUILD_TARGET_CACHE)/$(BLADE_JAVA_AGENT_NAME)
# used to invoke by chaosblade
BLADE_JAVA_AGENT_SPEC=chaosblade-jvm-spec-$(BLADE_VERSION).yaml
BLADE_JAVA_AGENT_SPEC_DEST_PATH=$(BUILD_TARGET_CACHE)/$(BLADE_JAVA_AGENT_SPEC)
BLADE_JAVA_AGENT_SPEC_DOWNLOAD_URL=$(BLADE_OSS_URL)/$(BLADE_JAVA_AGENT_SPEC)
# used to java agent attachp
BLADE_JAVA_TOOLS_JAR_NAME=tools.jar
BLADE_JAVA_TOOLS_JAR_DEST_PATH=$(BUILD_TARGET_CACHE)/$(BLADE_JAVA_TOOLS_JAR_NAME)
BLADE_JAVA_TOOLS_JAR_DOWNLOAD_URL=$(BLADE_OSS_URL)/$(BLADE_JAVA_TOOLS_JAR_NAME)
# cplus zip contains jar and scripts
BLADE_CPLUS_ZIP_VERSION=0.0.1
BLADE_CPLUS_LIB_DIR_NAME=cplus
BLADE_CPLUS_DIR_NAME=chaosblade-exec-cplus-$(BLADE_CPLUS_ZIP_VERSION)
BLADE_CPLUS_ZIP_NAME=$(BLADE_CPLUS_DIR_NAME).zip
BLADE_CPLUS_ZIP_DOWNLOAD_URL=$(BLADE_OSS_URL)/$(BLADE_CPLUS_ZIP_NAME)
BLADE_CPLUS_ZIP_DEST_PATH=$(BUILD_TARGET_CACHE)/$(BLADE_CPLUS_ZIP_NAME)
# cplus jar
BLADE_CPLUS_AGENT_NAME=chaosblade-exec-cplus-$(BLADE_CPLUS_ZIP_VERSION).jar
# important!! the name is related to the blade program
BLADE_CPLUS_AGENT_DEST_NAME=chaosblade-exec-cplus.jar

# cplus spec is used to invoke by chaosblade
BLADE_CPLUS_AGENT_SPEC=chaosblade-cplus-spec.yaml
BLADE_CPLUS_AGENT_SPEC_DEST_PATH=$(BUILD_TARGET_CACHE)/chaosblade-cplus-spec.yaml
BLADE_CPLUS_AGENT_SPEC_DOWNLOAD_URL=$(BLADE_OSS_URL)/$(BLADE_CPLUS_AGENT_SPEC)

# docker yaml
DOCKER_YAML_FILE_NAME=chaosblade-docker-spec-$(BLADE_VERSION).yaml
DOCKER_YAML_FILE_PATH=$(BUILD_TARGET_BIN)/$(DOCKER_YAML_FILE_NAME)

ifeq ($(GOOS), linux)
	GO_FLAGS=-ldflags="-linkmode external -extldflags -static -X ${VERSION_PKG}.Ver=$(BLADE_VERSION) -X '${VERSION_PKG}.Env=`uname -mv`' -X '${VERSION_PKG}.BuildTime=`date`'"
endif

# build chaosblade package and image
build: pre_build build_cli build_os build_docker build_kubernetes
	# tar package
	tar zcvf $(BUILD_TARGET_PKG_FILE_PATH) -C $(BUILD_TARGET) $(BUILD_TARGET_DIR_NAME)

# build chaosblade cli: blade
build_cli:
	# build blade cli
	$(GO) build $(GO_FLAGS) -o $(BUILD_TARGET_PKG_DIR)/blade ./cli

# build os
build_os:
ifneq ($(BUILD_TARGET_CACHE)/chaosblade-exec-os, $(wildcard $(BUILD_TARGET_CACHE)/chaosblade-exec-os))
	git clone -b $(BLADE_EXEC_OS_BRANCH) $(BLADE_EXEC_OS_PROJECT) $(BUILD_TARGET_CACHE)/chaosblade-exec-os
else
	git -C $(BUILD_TARGET_CACHE)/chaosblade-exec-os pull origin $(BLADE_EXEC_OS_BRANCH)
endif
	make -C $(BUILD_TARGET_CACHE)/chaosblade-exec-os
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

# create dir or download necessary file
pre_build:mkdir_build_target download_sandbox download_blade_java_agent download_cplus_agent
	rm -rf $(BUILD_TARGET_PKG_DIR) $(BUILD_TARGET_PKG_FILE_PATH)
	mkdir -p $(BUILD_TARGET_BIN) $(BUILD_TARGET_LIB)
	# unzip jvm-sandbox
	unzip $(JVM_SANDBOX_DEST_PATH) -d $(BUILD_TARGET_LIB)
	# cp chaosblade-java-agent
	cp $(BLADE_JAVA_AGENT_DEST_PATH) $(BUILD_TARGET_LIB)/sandbox/module/
	# cp jvm.spec.yaml to bin
	cp $(BLADE_JAVA_AGENT_SPEC_DEST_PATH) $(BUILD_TARGET_BIN)
	# cp tools.jar to bin
	cp $(BLADE_JAVA_TOOLS_JAR_DEST_PATH) $(BUILD_TARGET_BIN)
	# unzip chaosblade-exec-cplus
	unzip $(BLADE_CPLUS_ZIP_DEST_PATH) -d $(BUILD_TARGET_LIB)
	# rename chaosblade-exec-cplus-VERSION.jar to chaosblade-exec-cplus.jar
	mv $(BUILD_TARGET_LIB)/$(BLADE_CPLUS_DIR_NAME)/$(BLADE_CPLUS_AGENT_NAME) $(BUILD_TARGET_LIB)/$(BLADE_CPLUS_DIR_NAME)/$(BLADE_CPLUS_AGENT_DEST_NAME)
	# rename chaosblade-exec-cplus to cplus
	mv $(BUILD_TARGET_LIB)/$(BLADE_CPLUS_DIR_NAME) $(BUILD_TARGET_LIB)/$(BLADE_CPLUS_LIB_DIR_NAME)
	# cp chaosblade-cplus-spec.yaml  to bin
	mv $(BLADE_CPLUS_AGENT_SPEC_DEST_PATH) $(BUILD_TARGET_BIN)

# download sandbox for java chaos experiment
download_sandbox:
ifneq ($(JVM_SANDBOX_DEST_PATH), $(wildcard $(JVM_SANDBOX_DEST_PATH)))
	wget "$(JVM_SANDBOX_OSS_URL)" -O $(JVM_SANDBOX_DEST_PATH)
endif

# download java agent and spec config file
download_blade_java_agent:
ifneq ($(BLADE_JAVA_AGENT_DEST_PATH), $(wildcard $(BLADE_JAVA_AGENT_DEST_PATH)))
	wget "$(BLADE_JAVA_AGENT_DOWNLOAD_URL)" -O $(BLADE_JAVA_AGENT_DEST_PATH)
endif
ifneq ($(BLADE_JAVA_TOOLS_JAR_DEST_PATH), $(wildcard $(BLADE_JAVA_TOOLS_JAR_DEST_PATH)))
	wget "$(BLADE_JAVA_TOOLS_JAR_DOWNLOAD_URL)" -O $(BLADE_JAVA_TOOLS_JAR_DEST_PATH)
endif
	wget "$(BLADE_JAVA_AGENT_SPEC_DOWNLOAD_URL)" -O $(BLADE_JAVA_AGENT_SPEC_DEST_PATH)

download_cplus_agent:
ifneq ($(BLADE_CPLUS_ZIP_DEST_PATH), $(wildcard $(BLADE_CPLUS_ZIP_DEST_PATH)))
	wget "$(BLADE_CPLUS_ZIP_DOWNLOAD_URL)" -O $(BLADE_CPLUS_ZIP_DEST_PATH)
endif
	wget "$(BLADE_CPLUS_AGENT_SPEC_DOWNLOAD_URL)" -O $(BLADE_CPLUS_AGENT_SPEC_DEST_PATH)

# create cache dir
mkdir_build_target:
ifneq ($(BUILD_TARGET_CACHE), $(wildcard $(BUILD_TARGET_CACHE)))
	mkdir -p $(BUILD_TARGET_CACHE)
endif

# build chaosblade linux version by docker image
build_linux:
	docker build -f build/image/musl/Dockerfile -t chaosblade-build-musl:latest build/image/musl
	docker run --rm \
		-v $(shell echo -n ${GOPATH}):/go \
		-w /go/src/github.com/chaosblade-io/chaosblade \
		chaosblade-build-musl:latest

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
