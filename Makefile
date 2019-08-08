.PHONY: build clean

BLADE_VERSION=0.2.0

BLADE_BIN=blade
BLADE_EXPORT=chaosblade-$(BLADE_VERSION).tgz
BLADE_SRC_ROOT=`pwd`

GO_ENV=CGO_ENABLED=1
GO_FLAGS=-ldflags="-X main.ver=$(BLADE_VERSION) -X 'main.env=`uname -mv`' -X 'main.buildTime=`date`'"
GO=env $(GO_ENV) go

UNAME := $(shell uname)

BUILD_TARGET=target
BUILD_TARGET_DIR_NAME=chaosblade-$(BLADE_VERSION)
BUILD_TARGET_PKG_DIR=$(BUILD_TARGET)/chaosblade-$(BLADE_VERSION)
BUILD_TARGET_BIN=$(BUILD_TARGET_PKG_DIR)/bin
BUILD_TARGET_LIB=$(BUILD_TARGET_PKG_DIR)/lib
BUILD_TARGET_TAR_NAME=$(BUILD_TARGET_DIR_NAME).tar.gz
BUILD_TARGET_PKG_FILE_PATH=$(BUILD_TARGET)/$(BUILD_TARGET_TAR_NAME)
BUILD_IMAGE_PATH=build/image/blade
# cache downloaded file
BUILD_TARGET_CACHE=$(BUILD_TARGET)/cache
# oss url
BLADE_OSS_URL=https://chaosblade.oss-cn-hangzhou.aliyuncs.com/agent/release

# used to transform java class
JVM_SANDBOX_VERSION=1.2.0
JVM_SANDBOX_NAME=sandbox-$(JVM_SANDBOX_VERSION)-bin.zip
JVM_SANDBOX_OSS_URL=https://ompc.oss-cn-hangzhou.aliyuncs.com/jvm-sandbox/release/$(JVM_SANDBOX_NAME)
JVM_SANDBOX_DEST_PATH=$(BUILD_TARGET_CACHE)/$(JVM_SANDBOX_NAME)
# used to execute jvm chaos
BLADE_JAVA_AGENT_VERSION=0.2.0
BLADE_JAVA_AGENT_NAME=chaosblade-java-agent-$(BLADE_JAVA_AGENT_VERSION).jar
BLADE_JAVA_AGENT_DOWNLOAD_URL=$(BLADE_OSS_URL)/$(BLADE_JAVA_AGENT_NAME)
BLADE_JAVA_AGENT_DEST_PATH=$(BUILD_TARGET_CACHE)/$(BLADE_JAVA_AGENT_NAME)
# used to invoke by chaosblade
BLADE_JAVA_AGENT_SPEC=jvm.spec.yaml
BLADE_JAVA_AGENT_SPEC_DEST_PATH=$(BUILD_TARGET_CACHE)/jvm.spec.yaml
BLADE_JAVA_AGENT_SPEC_DOWNLOAD_URL=$(BLADE_OSS_URL)/$(BLADE_JAVA_AGENT_SPEC)
# used to java agent attach
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
BLADE_CPLUS_AGENT_SPEC=cplus-chaosblade.spec.yaml
BLADE_CPLUS_AGENT_SPEC_DEST_PATH=$(BUILD_TARGET_CACHE)/cplus-chaosblade.spec.yaml
BLADE_CPLUS_AGENT_SPEC_DOWNLOAD_URL=$(BLADE_OSS_URL)/$(BLADE_CPLUS_AGENT_SPEC)

ifeq ($(GOOS), linux)
	GO_FLAGS=-ldflags="-linkmode external -extldflags -static -X main.ver=$(BLADE_VERSION) -X 'main.env=`uname -mv`' -X 'main.buildTime=`date`'"
endif

# build chaosblade package and image
build: pre_build build_osbin build_cli
	# tar package
	tar zcvf $(BUILD_TARGET_PKG_FILE_PATH) -C $(BUILD_TARGET) $(BUILD_TARGET_DIR_NAME)

# build chaosblade cli: blade
build_cli:
	# build blade cli
	$(GO) build $(GO_FLAGS) -o $(BUILD_TARGET_PKG_DIR)/blade ./cli

build_osbin: build_burncpu build_burnmem build_burnio build_killprocess build_stopprocess build_changedns build_delaynetwork build_dropnetwork build_lossnetwork build_filldisk

# build burn-cpu chaos tools
build_burncpu: exec/os/bin/burncpu/forlinux/burncpu.go exec/os/bin/burncpu/burncpu.go
ifeq ($(UNAME), Linux)
	$(GO) build $(GO_FLAGS) -o $(BUILD_TARGET_BIN)/chaos_burncpu $<
else
	$(GO) build $(GO_FLAGS) -o $(BUILD_TARGET_BIN)/chaos_burncpu exec/os/bin/burncpu/burncpu.go
endif

# build burn-mem chaos tools
build_burnmem: exec/os/bin/burnmem/burnmem.go
	$(GO) build $(GO_FLAGS) -o $(BUILD_TARGET_BIN)/chaos_burnmem $<

# build burn-io chaos tools
build_burnio: exec/os/bin/burnio/burnio.go
	$(GO) build $(GO_FLAGS) -o $(BUILD_TARGET_BIN)/chaos_burnio $<

# build kill-process chaos tools
build_killprocess: exec/os/bin/killprocess/killprocess.go
	$(GO) build $(GO_FLAGS) -o $(BUILD_TARGET_BIN)/chaos_killprocess $<

# build stop-process chaos tools
build_stopprocess: exec/os/bin/stopprocess/stopprocess.go
	$(GO) build $(GO_FLAGS) -o $(BUILD_TARGET_BIN)/chaos_stopprocess $<

build_changedns: exec/os/bin/changedns/changedns.go
	$(GO) build $(GO_FLAGS) -o $(BUILD_TARGET_BIN)/chaos_changedns $<

build_delaynetwork: exec/os/bin/delaynetwork/delaynetwork.go
	$(GO) build $(GO_FLAGS) -o $(BUILD_TARGET_BIN)/chaos_delaynetwork $<

build_dropnetwork: exec/os/bin/dropnetwork/dropnetwork.go
	$(GO) build $(GO_FLAGS) -o $(BUILD_TARGET_BIN)/chaos_dropnetwork $<

build_lossnetwork: exec/os/bin/lossnetwork/lossnetwork.go
	$(GO) build $(GO_FLAGS) -o $(BUILD_TARGET_BIN)/chaos_lossnetwork $<

build_filldisk: exec/os/bin/filldisk/filldisk.go
	$(GO) build $(GO_FLAGS) -o $(BUILD_TARGET_BIN)/chaos_filldisk $<

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
	# cp cplus-chaosblade.spec.yaml to bin
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
	wget "$(BLADE_CPLUS_ZIP_DOWNLOAD_URL)" -O $(BLADE_CPLUS_ZIP_DEST_PATH)
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
build_image: build_linux
	rm -rf $(BUILD_IMAGE_PATH)/$(BUILD_TARGET_DIR_NAME)

	cp -R $(BUILD_TARGET_PKG_DIR) $(BUILD_IMAGE_PATH)
	docker build -f $(BUILD_IMAGE_PATH)/Dockerfile \
		--build-arg BLADE_VERSION=$(BLADE_VERSION) \
		-t chaosblade-agent:$(BLADE_VERSION) \
		$(BUILD_IMAGE_PATH)

	rm -rf $(BUILD_IMAGE_PATH)/$(BUILD_TARGET_DIR_NAME)

# build docker image with multi-stage builds
docker_image: clean
	docker build -f ./Dockerfile \
		--build-arg BLADE_VERSION=$(BLADE_VERSION) \
		-t chaosblade:$(BLADE_VERSION) $(BLADE_SRC_ROOT)

# test
test:
	go test -race -coverprofile=coverage.txt -covermode=atomic ./...
# clean all build result
clean:
	go clean ./...
	rm -rf $(BUILD_TARGET)
	rm -rf $(BUILD_IMAGE_PATH)/$(BUILD_TARGET_DIR_NAME)
