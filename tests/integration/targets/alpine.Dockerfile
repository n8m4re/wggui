# Alpine 3.22 target: root login, BusyBox ash/awk, OpenRC; wireguard-go for running tunnels
FROM alpine:3.22
RUN apk add --no-cache openssh-server wireguard-tools wireguard-go iproute2 \
 && mkdir -p /etc/wireguard \
 && ssh-keygen -A \
 && sed -i 's/^root:[^:]*:/root:*:/' /etc/shadow
COPY sshd-test.conf /etc/ssh/sshd-test.conf
CMD ["/usr/sbin/sshd", "-D", "-e", "-f", "/etc/ssh/sshd-test.conf"]
