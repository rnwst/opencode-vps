/* Inject the nested procfs mount failure while preserving namespace setup. */
#define _GNU_SOURCE
#include <errno.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/syscall.h>
#include <unistd.h>

int mount(const char *source, const char *target, const char *filesystemtype,
          unsigned long mountflags, const void *data) {
    if (filesystemtype != NULL && strcmp(filesystemtype, "proc") == 0 &&
        strcmp(target, "/proc") == 0) {
        errno = EPERM;
        return -1;
    }
    return syscall(SYS_mount, source, target, filesystemtype, mountflags, data);
}
