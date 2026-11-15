# Debian 13 target: root login, mawk, dash, no systemd; wireguard-go for tunnels without a kernel module
FROM debian:13
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      openssh-server wireguard-tools wireguard-go iproute2 procps \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd /etc/wireguard \
 && ssh-keygen -A \
 && usermod -p '*' root
COPY sshd-test.conf /etc/ssh/sshd-test.conf
CMD ["/usr/sbin/sshd", "-D", "-e", "-f", "/etc/ssh/sshd-test.conf"]
