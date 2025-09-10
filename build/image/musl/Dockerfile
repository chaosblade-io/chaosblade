FROM golang:1.25.1
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
