FROM golang:1.12.1
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

RUN apt-get update \
    && apt-get install unzip

ENV CC /usr/local/musl/bin/musl-gcc
ENV GOOS linux

ENTRYPOINT [ "make" ]
