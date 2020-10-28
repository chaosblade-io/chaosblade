FROM alpine:3.8 as builder
LABEL maintainer="tiny-x"

# install upx
RUN apk add --no-cache xz \
    && wget https://github.com/upx/upx/releases/download/v3.96/upx-3.96-amd64_linux.tar.xz \
    && tar -xvf upx-3.96-amd64_linux.tar.xz \
    && mv upx-3.96-amd64_linux/upx /usr/bin

FROM busybox:latest

COPY --from=builder /usr/bin/upx /usr/bin/upx
ENTRYPOINT ["/usr/bin/upx"]
