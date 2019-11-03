FROM alpine:3.8
LABEL maintainer="Changjun Xiao"

# The image is chaosblade image
RUN apk --no-cache add --update iproute2 bash util-linux curl \
    && mkdir -p /lib/modules/$(uname -r)

ARG BLADE_VERSION=0.0.1

ENV CHAOSBLADE_HOME /opt/chaosblade
WORKDIR ${CHAOSBLADE_HOME}

COPY chaosblade-${BLADE_VERSION} ${CHAOSBLADE_HOME}
COPY chaosblade-${BLADE_VERSION}.tar.gz /opt

ENV PATH ${CHAOSBLADE_HOME}:${PATH}
CMD ["sh", "-c", "tail -f /dev/null"]
