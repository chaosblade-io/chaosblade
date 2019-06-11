FROM golang:1.12.5 AS builder
LABEL maintainer="Changjun Xiao, Ming Cheng"

ARG BLADE_VERSION=0.0.3
ARG MUSL_VERSION=1.1.22

# Using 163 mirror for Debian Strech
RUN sed -i 's/deb.debian.org/mirrors.163.com/g' /etc/apt/sources.list \
  && apt-get update \
  && apt-get install unzip

# # The image is used to build chaosblade for musl
RUN wget https://www.musl-libc.org/releases/musl-${MUSL_VERSION}.tar.gz \
  && tar -zxvf musl-${MUSL_VERSION}.tar.gz \
  && cd musl* \
  && ./configure --prefix=/usr \
  && make && make install

ENV CC /usr/bin/musl-gcc
ENV BLADE_BUILD_PATH /tmp/chaosblade

# Print go version
RUN ${CC} --version && go version

# Build blade
COPY . ${BLADE_BUILD_PATH}
WORKDIR ${BLADE_BUILD_PATH}
RUN make clean && make && \
  mv -f ${BLADE_BUILD_PATH}/target/chaosblade-${BLADE_VERSION} /usr/local/chaosblade

# Stage2
FROM alpine:3.9.4

# @from https://mirrors.ustc.edu.cn/help/alpine.html
# RUN sed -i 's/dl-cdn.alpinelinux.org/mirrors.ustc.edu.cn/g' /etc/apk/repositories

# Install necessary package.
# RUN apk --no-cache add --update iproute2 bash util-linux curl \
#   && mkdir -p /lib/modules/$(uname -r)

ENV CHAOSBLADE_HOME /usr/local/chaosblade
COPY --from=builder ${CHAOSBLADE_HOME} ${CHAOSBLADE_HOME}

WORKDIR ${CHAOSBLADE_HOME}
ENV PATH ${CHAOSBLADE_HOME}:${PATH}

ENTRYPOINT [ "blade" ]
