#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc < 2) return 2;
    int fd = open("/runtime/exec-start.ns", O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (fd < 0) { perror("trace exec output"); return 1; }
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t) ||
        dprintf(fd, "%lld\n", (long long)t.tv_sec * 1000000000 + t.tv_nsec) < 0 || close(fd)) {
        perror("trace exec timestamp"); return 1;
    }
    execv(argv[1], argv + 1);
    perror("trace VMM exec");
    return 1;
}
