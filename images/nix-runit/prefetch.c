#define _POSIX_C_SOURCE 200809L
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    FILE *manifest = fopen(argv[1], "r");
    if (!manifest) return 1;
    char path[4096];
    while (fgets(path, sizeof(path), manifest)) {
        if (!strchr(path, '\n')) { fclose(manifest); return 1; }
        path[strcspn(path, "\r\n")] = 0;
        if (path[0] != '/') { fclose(manifest); return 1; }
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        if (fd < 0) continue;
        posix_fadvise(fd, 0, 0, POSIX_FADV_WILLNEED);
        close(fd);
    }
    fclose(manifest);
    return 0;
}
