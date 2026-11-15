# Fedora 44 target: root login, gawk, bash
FROM fedora:44
RUN dnf -y install --setopt=install_weak_deps=False openssh-server wireguard-tools iproute procps-ng \
 && dnf clean all \
 && mkdir -p /etc/wireguard \
 && ssh-keygen -A \
 && usermod -p '*' root
COPY sshd-test.conf /etc/ssh/sshd-test.conf
CMD ["/usr/sbin/sshd", "-D", "-e", "-f", "/etc/ssh/sshd-test.conf"]
