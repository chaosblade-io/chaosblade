FROM multiarch/alpine:arm64-edge
LABEL maintainer="Changjun Xiao"

RUN sed -i 's/dl-cdn.alpinelinux.org/mirrors.ustc.edu.cn/g' /etc/apk/repositories

# The image is chaosblade image
RUN apk --no-cache add --update iproute2 bash util-linux curl fuse coreutils \
    && mkdir -p /lib/modules/$(uname -r)

ARG BLADE_VERSION=0.0.1

ENV CHAOSBLADE_HOME /opt/chaosblade
WORKDIR ${CHAOSBLADE_HOME}

COPY chaosblade-${BLADE_VERSION} ${CHAOSBLADE_HOME}

ENV PATH ${CHAOSBLADE_HOME}:${PATH}
CMD ["sh", "-c", "tail -f /dev/null"]
