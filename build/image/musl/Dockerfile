# Copyright 2025 The ChaosBlade Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

FROM golang:1.25.4
LABEL maintainer="Changjun Xiao"

# # The image is used to build chaosblade for musl
RUN wget http://www.musl-libc.org/releases/musl-1.1.21.tar.gz \
    && tar -zxvf musl-1.1.21.tar.gz \
    && rm musl-1.1.21.tar.gz \
    && cd musl* \
    && ./configure \
    && make \
    && make install \
    && rm -rf musl*

    # 提取 maven 版本号
ARG MAVEN_VERSION=3.9.11
# install maven for java project compiled
RUN wget http://mirrors.tuna.tsinghua.edu.cn/apache/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz \
    && tar -zxvf apache-maven-${MAVEN_VERSION}-bin.tar.gz \
    && rm apache-maven-${MAVEN_VERSION}-bin.tar.gz \
    && mv apache-maven-${MAVEN_VERSION} /usr/local/apache-maven-${MAVEN_VERSION}

RUN apt-get update \
    && apt-get install -y unzip  upx-ucl openjdk-21-jdk

ENV CC /usr/local/musl/bin/musl-gcc
ENV GOOS linux
ENV java /usr/bin/java
ENV PATH /usr/local/apache-maven-${MAVEN_VERSION}/bin:$PATH

ENTRYPOINT [ "make" ]
