# Ubuntu 24.04 target: login as “admin”, access via sudo -n (NOPASSWD), as usual on Ubuntu
FROM ubuntu:24.04
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      openssh-server wireguard-tools iproute2 procps sudo \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd /etc/wireguard \
 && chmod 700 /etc/wireguard \
 && ssh-keygen -A \
 && useradd -m -s /bin/bash admin \
 && usermod -p '*' admin \
 && echo 'admin ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/admin \
 && chmod 440 /etc/sudoers.d/admin
COPY sshd-test.conf /etc/ssh/sshd-test.conf
CMD ["/usr/sbin/sshd", "-D", "-e", "-f", "/etc/ssh/sshd-test.conf"]
