#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <ifaddrs.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <linux/vm_sockets.h>
#include <net/if.h>
#include <poll.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/inotify.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static bool profiling;
static long long deadline;

static void fail(const char *what) {
    fprintf(stderr, "koi-ready: %s: %s\n", what, strerror(errno));
    exit(1);
}

static long long milliseconds(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) fail("clock");
    return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

static long long boottime_ns(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_BOOTTIME, &t)) fail("trace clock");
    return (long long)t.tv_sec * 1000000000 + t.tv_nsec;
}

static int remaining(void) {
    long long left = deadline - milliseconds();
    if (left <= 0) { errno = ETIMEDOUT; fail("readiness deadline"); }
    return (int)left;
}

static void trace(const char *label) {
    if (!profiling) return;
    struct timespec t;
    if (clock_gettime(CLOCK_BOOTTIME, &t)) fail("profile clock");
    FILE *f = fopen("/run/koi-boot.tsv", "a");
    if (!f) fail("profile output");
    fprintf(f, "%lld.%09ld\t%s\n", (long long)t.tv_sec, t.tv_nsec, label);
    fclose(f);
}

static int connected(int domain, const struct sockaddr *address, socklen_t length, int timeout) {
    int fd = socket(domain, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0) return -1;
    if (connect(fd, address, length) == 0) return fd;
    if (errno == EINPROGRESS) {
        struct pollfd p = { .fd = fd, .events = POLLOUT };
        int ready;
        do { ready = poll(&p, 1, timeout); } while (ready < 0 && errno == EINTR);
        int error = 0;
        socklen_t size = sizeof(error);
        if (ready > 0 && getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &size) == 0 && !error)
            return fd;
    }
    close(fd);
    return -1;
}

static void wait_socket(int domain, const struct sockaddr *address, socklen_t length) {
    for (;;) {
        int fd = connected(domain, address, length, 50);
        if (fd >= 0) { close(fd); return; }
        remaining();
        struct timespec pause = { .tv_nsec = 20000000 };
        nanosleep(&pause, NULL);
    }
}

static void wait_session(void) {
    int fd = inotify_init1(IN_CLOEXEC);
    if (fd < 0) fail("session events");
    if (inotify_add_watch(fd, "/run", IN_CREATE | IN_MOVED_TO) < 0)
        fail("session subscription");
    while (access("/run/koi-services-ready", F_OK) != 0) {
        char events[4096];
        if (read(fd, events, sizeof(events)) < 0 && errno != EINTR)
            fail("session event read");
    }
    close(fd);
    trace("services.session");
}

static bool ipv4(char *address, size_t capacity) {
    struct ifaddrs *interfaces;
    if (getifaddrs(&interfaces)) fail("interface query");
    bool found = false;
    for (struct ifaddrs *p = interfaces; p; p = p->ifa_next) {
        if (strcmp(p->ifa_name, "eth0") || !p->ifa_addr || p->ifa_addr->sa_family != AF_INET)
            continue;
        const struct sockaddr_in *v4 = (const struct sockaddr_in *)p->ifa_addr;
        if (!v4->sin_addr.s_addr) continue;
        if (!inet_ntop(AF_INET, &v4->sin_addr, address, capacity)) fail("IPv4 formatting");
        found = true;
        break;
    }
    freeifaddrs(interfaces);
    return found;
}

static void line(const char *path, char *value, size_t capacity) {
    FILE *f = fopen(path, "r");
    if (!f) fail("public identity file");
    if (!fgets(value, (int)capacity, f)) fail("public identity read");
    int next = strchr(value, '\n') ? EOF : fgetc(f);
    fclose(f);
    if (next != EOF) { errno = EINVAL; fail("oversized identity field"); }
    value[strcspn(value, "\r\n")] = 0;
}

int main(int argc, char **argv) {
    profiling = access("/etc/koi/profile-boot", F_OK) == 0;
    bool exchange = access("/run/koi-trace", F_OK) == 0;
    profiling = profiling || exchange;
    if (argc == 2 && !strcmp(argv[1], "--wait-session")) {
        wait_session();
        return 0;
    }
    if (argc == 3 && !strcmp(argv[1], "--stamp")) {
        if (!argv[2][0] || strlen(argv[2]) > 63 ||
            strspn(argv[2], "abcdefghijklmnopqrstuvwxyz0123456789_.") != strlen(argv[2])) return 2;
        trace(argv[2]);
        return 0;
    }
    if (argc != 1) return 2;
    deadline = milliseconds() + 30000;
    trace("ready.enter");
    struct sockaddr_in ssh = { .sin_family = AF_INET, .sin_port = htons(22),
                              .sin_addr = { .s_addr = htonl(INADDR_LOOPBACK) } };
    wait_socket(AF_INET, (struct sockaddr *)&ssh, sizeof(ssh));
    trace("ready.ssh");

    int changes = socket(AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC, NETLINK_ROUTE);
    if (changes < 0) fail("network events");
    struct sockaddr_nl subscription = { .nl_family = AF_NETLINK,
                                       .nl_groups = RTMGRP_LINK | RTMGRP_IPV4_IFADDR };
    if (bind(changes, (struct sockaddr *)&subscription, sizeof(subscription))) fail("network subscription");
    char address[INET_ADDRSTRLEN];
    while (!ipv4(address, sizeof(address))) {
        struct pollfd p = { .fd = changes, .events = POLLIN };
        int status = poll(&p, 1, remaining());
        if (status < 0 && errno == EINTR) continue;
        if (status <= 0) { errno = ETIMEDOUT; fail("DHCP address"); }
        char events[65536];
        if (recv(changes, events, sizeof(events), 0) < 0 && errno != ENOBUFS && errno != EINTR)
            fail("network event read");
    }
    close(changes);
    trace("ready.address");
    char id[64], name[64], key[256];
    line("/var/lib/koi/identity", id, sizeof(id));
    line("/etc/hostname", name, sizeof(name));
    line("/etc/ssh/ssh_host_ed25519_key.pub", key, sizeof(key));
    if (strlen(id) != 32 || strspn(id, "0123456789abcdef") != 32 ||
        !name[0] || strlen(name) > 31 || name[0] < 'a' || name[0] > 'z' ||
        strspn(name, "abcdefghijklmnopqrstuvwxyz0123456789-") != strlen(name)) {
        errno = EINVAL; fail("public machine identity");
    }
    const char *prefix = "ssh-ed25519 ";
    if (strncmp(key, prefix, strlen(prefix))) { errno = EINVAL; fail("public key type"); }
    char *blob = key + strlen(prefix);
    blob[strcspn(blob, " \t")] = 0;                         
    if (strlen(blob) != 68 || strspn(blob, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=") != 68) {
        errno = EINVAL; fail("public key encoding");
    }
    struct sockaddr_vm host = { .svm_family = AF_VSOCK, .svm_cid = 2, .svm_port = 1024 };
    int fd = connected(AF_VSOCK, (struct sockaddr *)&host, sizeof(host), 10000);
    if (fd < 0) fail("host readiness channel");
    char clock_fields[160] = "";
    if (exchange) {
        struct pollfd p = { .fd = fd, .events = POLLIN };
        char challenge;
        if (poll(&p, 1, remaining()) <= 0 || recv(fd, &challenge, 1, 0) != 1 || challenge != 'T') {
            errno = EPROTO; fail("trace clock challenge");
        }
        long long received = boottime_ns();
        long long sent = boottime_ns();
        snprintf(clock_fields, sizeof(clock_fields), ",\"trace\":{\"receive_ns\":%lld,\"send_ns\":%lld}", received, sent);
    }
    char packet[768];
    int length = snprintf(packet, sizeof(packet), "{\"id\":\"%s\",\"name\":\"%s\",\"address\":\"%s\",\"ssh_key\":\"%s\"%s}\n", id, name, address, key, clock_fields);
    if (length < 0 || (size_t)length >= sizeof(packet)) { errno = EOVERFLOW; fail("report size"); }
    size_t sent = 0;
    while (sent < (size_t)length) {
        ssize_t n = send(fd, packet + sent, (size_t)length - sent, MSG_NOSIGNAL);
        if (n > 0) { sent += (size_t)n; continue; }
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            struct pollfd p = { .fd = fd, .events = POLLOUT };
            if (poll(&p, 1, remaining()) > 0) continue;
        }
        fail("host readiness write");
    }
    close(fd);
    trace("ready.sent");
    return 0;
}
