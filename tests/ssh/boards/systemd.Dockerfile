# A board that runs systemd as PID 1 with journald and OpenSSH: root and `tester` log in by key.
# `tester` is not in systemd-journal, so it sees only its own journal. The key arrives at run time.
ARG BASE=ubuntu:24.04
FROM ${BASE}
ARG ARCHIVE=0
ENV DEBIAN_FRONTEND=noninteractive container=docker
# debian:11 left its mirrors for the archive.
RUN if [ "$ARCHIVE" = 1 ]; then \
      rm -f /etc/apt/sources.list.d/*; \
      printf 'deb http://archive.debian.org/debian bullseye main\ndeb http://archive.debian.org/debian-security bullseye-security main\n' > /etc/apt/sources.list; \
    fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends systemd systemd-sysv dbus openssh-server util-linux procps bsdutils ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd /root/.ssh /var/log/journal \
 && useradd -m -s /bin/sh tester \
 && mkdir -p /home/tester/.ssh \
 && (systemctl enable ssh 2>/dev/null || systemctl enable sshd)
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
