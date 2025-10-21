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

FROM multiarch/ubuntu-debootstrap:arm64-bionic
LABEL maintainer="tiny-x"

# install gcc make git
RUN apt-get update \
    && apt-get install -y build-essential git

# The image is used to build chaosblade for musl
RUN wget http://www.musl-libc.org/releases/musl-1.1.21.tar.gz \
    && tar -zxvf musl-1.1.21.tar.gz \
    && rm musl-1.1.21.tar.gz \
    && cd musl* \
    && ./configure \
    && make \
    && make install \
    && rm -rf musl*

# install go
RUN wget https://dl.google.com/go/go1.20.5.linux-arm64.tar.gz \
    && tar -C /usr/local -xzf go1.20.5.linux-arm64.tar.gz \
    && rm -rf go1.20.5.linux-arm64.tar.gz

# install maven for java project compiled
RUN wget http://mirrors.tuna.tsinghua.edu.cn/apache/maven/maven-3/3.6.3/binaries/apache-maven-3.6.3-bin.tar.gz \
    && tar -zxvf apache-maven-3.6.3-bin.tar.gz \
    && rm apache-maven-3.6.3-bin.tar.gz \
    && mv apache-maven-3.6.3 /usr/local/apache-maven-3.6.3

RUN apt-get update \
    && apt-get install -y unzip openjdk-11-jdk

ENV CC /usr/local/musl/bin/musl-gcc
ENV GOOS linux
ENV PATH /usr/local/apache-maven-3.6.3/bin:$PATH
ENV PATH /usr/local/go/bin:$PATH
ENV java /usr/bin/java

ENTRYPOINT [ "make" ]
