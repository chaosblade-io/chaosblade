FROM alpine:3.8
LABEL maintainer="Changjun Xiao"

# The image is chaosblade image
RUN apk --no-cache add --update iproute2 bash util-linux curl \
    && mkdir -p /lib/modules/$(uname -r)

# install docker
#ARG DOCKER_CLI_VERSION="17.06.2-ce"
#ENV DOWNLOAD_URL="https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz"
#RUN mkdir -p /tmp/download && \
#    curl -L ${DOWNLOAD_URL} | tar -xz -C /tmp/download && \
#    mv /tmp/download/docker/docker /usr/local/bin/  && \
#    rm -rf /tmp/download

ARG BLADE_VERSION=0.0.1

ENV CHAOSBLADE_HOME /usr/local/chaosblade
WORKDIR ${CHAOSBLADE_HOME}

COPY chaosblade-${BLADE_VERSION} ${CHAOSBLADE_HOME}

ENV PATH ${CHAOSBLADE_HOME}:${PATH}
CMD ["sh", "-c", "tail -f /dev/null"]
