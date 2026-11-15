# Test target AlmaLinux 9 (binary compatible with RHEL 9): root login, gawk, bash, wireguard-tools from AppStream
FROM almalinux:9
RUN dnf -y install --setopt=install_weak_deps=False openssh-server wireguard-tools iproute procps-ng \
 && dnf clean all \
 && mkdir -p /etc/wireguard \
 && ssh-keygen -A \
 && usermod -p '*' root
COPY sshd-test.conf /etc/ssh/sshd-test.conf
CMD ["/usr/sbin/sshd", "-D", "-e", "-f", "/etc/ssh/sshd-test.conf"]
