FROM multiarch/alpine:arm64-edge
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

# 拷贝编译后的所有组件到容器中
# 这里拷贝的是 Makefile 中 get_build_output_dir 函数返回的目录内容
COPY target/chaosblade-${BLADE_VERSION}-${GOOS}_${GOARCH}/* ${CHAOSBLADE_HOME}/

ENV PATH=${CHAOSBLADE_HOME}:${PATH}
CMD ["sh", "-c", "tail -f /dev/null"]
