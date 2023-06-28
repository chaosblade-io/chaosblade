FROM golang:1.20.5 AS builder
LABEL maintainer="Changjun Xiao, Ming Cheng"

ARG BLADE_VERSION=0.0.1
ARG MUSL_VERSION=1.2.0

# Using 163 mirror for Debian Strech
RUN sed -i 's/deb.debian.org/mirrors.163.com/g' /etc/apt/sources.list
RUN apt-get update && apt-get install unzip

# # The image is used to build chaosblade for musl
RUN wget http://www.musl-libc.org/releases/musl-${MUSL_VERSION}.tar.gz \
    && tar -zxvf musl-${MUSL_VERSION}.tar.gz \
    && rm musl-${MUSL_VERSION}.tar.gz \
    && cd musl* \
    && ./configure \
    && make \
    && make install \
    && rm -rf musl*

ENV CC /usr/local/musl/bin/musl-gcc
ENV GOOS linux
ENV BLADE_BUILD_PATH /tmp/chaosblade

# Print go version
RUN ${CC} --version
RUN go version

# Build blade
COPY . ${BLADE_BUILD_PATH}
WORKDIR ${BLADE_BUILD_PATH}
RUN make clean && \
  go mod vendor && \
  env GO111MODULE=on GO15VENDOREXPERIMENT=1 make && \
  mv -f ${BLADE_BUILD_PATH}/target/chaosblade-${BLADE_VERSION} /usr/local/chaosblade

# Stage2
FROM alpine:3.13.7

# @from https://mirrors.ustc.edu.cn/help/alpine.html
RUN sed -i 's/dl-cdn.alpinelinux.org/mirrors.ustc.edu.cn/g' /etc/apk/repositories

# Install necessary package.
RUN apk --no-cache add --update iproute2 bash util-linux curl \
    && mkdir -p /lib/modules/$(uname -r)

ENV CHAOSBLADE_HOME /usr/local/chaosblade
COPY --from=builder ${CHAOSBLADE_HOME} ${CHAOSBLADE_HOME}

WORKDIR ${CHAOSBLADE_HOME}
ENV PATH ${CHAOSBLADE_HOME}:${PATH}

CMD ["blade"]
