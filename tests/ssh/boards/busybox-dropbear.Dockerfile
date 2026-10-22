# A BusyBox board: syslogd keeps its log in a shared-memory buffer for logread, Dropbear takes root by key.
FROM alpine:3.20
RUN apk add --no-cache dropbear \
 && echo 'root:board' | chpasswd \
 && mkdir -p /root/.ssh /var/log \
 && chmod 700 /root/.ssh
CMD ["sh", "-c", "syslogd -C && exec dropbear -F -E -R -s -p 22"]
