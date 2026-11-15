# Test target AlmaLinux 10 (binary compatible with RHEL 10): login as user "admin", access via sudo -n (NOPASSWD)
FROM almalinux:10
RUN dnf -y install --setopt=install_weak_deps=False openssh-server wireguard-tools iproute procps-ng sudo \
 && dnf clean all \
 && mkdir -p /etc/wireguard \
 && chmod 700 /etc/wireguard \
 && ssh-keygen -A \
 && useradd -m -s /bin/bash admin \
 && usermod -p '*' admin \
 && echo 'admin ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/admin \
 && chmod 440 /etc/sudoers.d/admin
COPY sshd-test.conf /etc/ssh/sshd-test.conf
CMD ["/usr/sbin/sshd", "-D", "-e", "-f", "/etc/ssh/sshd-test.conf"]
