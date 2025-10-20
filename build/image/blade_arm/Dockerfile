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

FROM alpine:latest
LABEL maintainer="Changjun Xiao"

RUN sed -i 's/dl-cdn.alpinelinux.org/mirrors.ustc.edu.cn/g' /etc/apk/repositories

# The image is chaosblade image
RUN apk --no-cache add --update iproute2 bash util-linux curl fuse coreutils \
    && mkdir -p /lib/modules/$(uname -r)

ARG BLADE_VERSION=0.0.1
ARG GOOS=linux
ARG GOARCH=arm64

ENV CHAOSBLADE_HOME=/opt/chaosblade
WORKDIR ${CHAOSBLADE_HOME}

COPY target/chaosblade-${BLADE_VERSION}-${GOOS}_${GOARCH}/ ${CHAOSBLADE_HOME}/

ENV PATH=${CHAOSBLADE_HOME}:${PATH}
CMD ["sh", "-c", "tail -f /dev/null"]
