#define _GNU_SOURCE

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <openssl/evp.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

static const char *active_temporary_path = NULL;

static void cleanup_temporary(void) {
    if (active_temporary_path) {
        unlink(active_temporary_path);
    }
}

static void handle_signal(int signal_number) {
    cleanup_temporary();
    _exit(128 + signal_number);
}

static void install_signal_handlers(void) {
    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = handle_signal;
    sigemptyset(&action.sa_mask);
    for (int signal_number = 1; signal_number <= 3; signal_number++) {
        if (sigaction(signal_number, &action, NULL) != 0) {
            fputs("{\"result\":\"error\",\"error\":\"cannot install signal handler\"}\n", stdout);
            exit(1);
        }
    }
    if (sigaction(SIGTERM, &action, NULL) != 0) {
        fputs("{\"result\":\"error\",\"error\":\"cannot install signal handler\"}\n", stdout);
        exit(1);
    }
}

static void print_json_string(const char *value) {
    const unsigned char *cursor = (const unsigned char *)value;
    while (*cursor) {
        switch (*cursor) {
            case '"':
                fputs("\\\"", stdout);
                break;
            case '\\':
                fputs("\\\\", stdout);
                break;
            case '\b':
                fputs("\\b", stdout);
                break;
            case '\f':
                fputs("\\f", stdout);
                break;
            case '\n':
                fputs("\\n", stdout);
                break;
            case '\r':
                fputs("\\r", stdout);
                break;
            case '\t':
                fputs("\\t", stdout);
                break;
            default:
                if (*cursor < 0x20) {
                    fprintf(stdout, "\\u%04x", *cursor);
                } else {
                    fputc(*cursor, stdout);
                }
        }
        cursor++;
    }
}

static void fail(const char *format, ...) {
    char message[1024];
    va_list args;
    cleanup_temporary();
    va_start(args, format);
    vsnprintf(message, sizeof(message), format, args);
    va_end(args);
    fputs("{\"result\":\"error\",\"error\":\"", stdout);
    print_json_string(message);
    fputs("\"}\n", stdout);
    exit(1);
}

static void hex_digest(const unsigned char digest[32], char out[65]) {
    static const char hexdigits[] = "0123456789abcdef";
    for (int i = 0; i < 32; i++) {
        out[i * 2] = hexdigits[(digest[i] >> 4) & 0x0f];
        out[i * 2 + 1] = hexdigits[digest[i] & 0x0f];
    }
    out[64] = '\0';
}

static char *temporary_path(const char *destination) {
    size_t length = strlen(destination) + 64;
    char *result = calloc(length, 1);
    if (!result) {
        fail("out of memory");
    }
    snprintf(result, length, "%s.copyhash.%ld.tmp", destination, (long)getpid());
    return result;
}

static void normalize_expected_digest(const char *input, char output[65]) {
    if (input[0] == '\0') {
        output[0] = '\0';
        return;
    }
    if (strncmp(input, "sha256:", 7) == 0) {
        input += 7;
    }
    if (strlen(input) != 64) {
        fail("expected SHA-256 must contain exactly 64 hexadecimal characters");
    }
    for (int index = 0; index < 64; index++) {
        if (!isxdigit((unsigned char)input[index])) {
            fail("expected SHA-256 contains a non-hexadecimal character");
        }
        output[index] = (char)tolower((unsigned char)input[index]);
    }
    output[64] = '\0';
}

static bool same_timespec(struct timespec left, struct timespec right) {
    return left.tv_sec == right.tv_sec && left.tv_nsec == right.tv_nsec;
}

static char *parent_directory(const char *path) {
    const char *slash = strrchr(path, '/');
    if (!slash) {
        return strdup(".");
    }
    if (slash == path) {
        return strdup("/");
    }
    size_t length = (size_t)(slash - path);
    char *parent = calloc(length + 1, 1);
    if (!parent) {
        fail("out of memory");
    }
    memcpy(parent, path, length);
    return parent;
}

static void fsync_parent_directory(const char *path) {
    char *parent = parent_directory(path);
    if (!parent) {
        fail("out of memory");
    }
    int directory = open(parent, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (directory < 0) {
        int saved_errno = errno;
        free(parent);
        fail("cannot open destination directory: %s", strerror(saved_errno));
    }
    if (fsync(directory) != 0) {
        int saved_errno = errno;
        close(directory);
        free(parent);
        fail("destination directory fsync failed: %s", strerror(saved_errno));
    }
    if (close(directory) != 0) {
        int saved_errno = errno;
        free(parent);
        fail("cannot close destination directory: %s", strerror(saved_errno));
    }
    free(parent);
}

static void publish_no_replace(const char *temporary, const char *destination) {
#ifdef SYS_renameat2
    if (
        syscall(
            SYS_renameat2,
            AT_FDCWD,
            temporary,
            AT_FDCWD,
            destination,
            RENAME_NOREPLACE
        ) == 0
    ) {
        return;
    }
    if (errno == EEXIST) {
        fail("destination already exists");
    }
    if (errno != EINVAL && errno != ENOSYS && errno != EOPNOTSUPP) {
        fail("atomic no-replace rename failed: %s", strerror(errno));
    }
#endif
    if (link(temporary, destination) != 0) {
        if (errno == EEXIST) {
            fail("destination already exists");
        }
        fail("atomic no-replace link failed: %s", strerror(errno));
    }
    if (unlink(temporary) != 0) {
        int saved_errno = errno;
        unlink(destination);
        fail("cannot remove published temporary link: %s", strerror(saved_errno));
    }
}

static ssize_t read_retry(int descriptor, void *buffer, size_t count) {
    while (true) {
        ssize_t result = read(descriptor, buffer, count);
        if (result < 0 && errno == EINTR) {
            continue;
        }
        return result;
    }
}

static ssize_t write_retry(int descriptor, const void *buffer, size_t count) {
    while (true) {
        ssize_t result = write(descriptor, buffer, count);
        if (result < 0 && errno == EINTR) {
            continue;
        }
        return result;
    }
}

static void hash_only(const char *source_path, const char *expected_input) {
    char expected[65];
    normalize_expected_digest(expected_input, expected);

    struct stat source_stat;
    int source = open(source_path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (source < 0) {
        fail("cannot open source: %s", strerror(errno));
    }
    if (fstat(source, &source_stat) != 0) {
        int saved_errno = errno;
        close(source);
        fail("cannot stat open source: %s", strerror(saved_errno));
    }
    if (!S_ISREG(source_stat.st_mode)) {
        close(source);
        fail("source is not a regular file");
    }

    EVP_MD_CTX *context = EVP_MD_CTX_new();
    if (!context || EVP_DigestInit_ex(context, EVP_sha256(), NULL) != 1) {
        close(source);
        fail("cannot initialize SHA-256");
    }
    unsigned char *buffer = malloc(8 * 1024 * 1024);
    if (!buffer) {
        close(source);
        EVP_MD_CTX_free(context);
        fail("out of memory");
    }
    off_t total = 0;
    while (true) {
        ssize_t read_count = read_retry(source, buffer, 8 * 1024 * 1024);
        if (read_count < 0) {
            int saved_errno = errno;
            free(buffer);
            close(source);
            EVP_MD_CTX_free(context);
            fail("read failed: %s", strerror(saved_errno));
        }
        if (read_count == 0) {
            break;
        }
        if (EVP_DigestUpdate(context, buffer, (size_t)read_count) != 1) {
            free(buffer);
            close(source);
            EVP_MD_CTX_free(context);
            fail("SHA-256 update failed");
        }
        total += read_count;
    }
    free(buffer);

    struct stat source_after;
    if (fstat(source, &source_after) != 0) {
        int saved_errno = errno;
        close(source);
        EVP_MD_CTX_free(context);
        fail("cannot restat open source: %s", strerror(saved_errno));
    }
    if (
        total != source_stat.st_size ||
        source_after.st_dev != source_stat.st_dev ||
        source_after.st_ino != source_stat.st_ino ||
        source_after.st_size != source_stat.st_size ||
        !same_timespec(source_after.st_mtim, source_stat.st_mtim) ||
        !same_timespec(source_after.st_ctim, source_stat.st_ctim)
    ) {
        close(source);
        EVP_MD_CTX_free(context);
        fail("source changed while hashing");
    }

    unsigned char digest[32];
    unsigned int digest_length = 0;
    if (
        EVP_DigestFinal_ex(context, digest, &digest_length) != 1 ||
        digest_length != sizeof(digest)
    ) {
        close(source);
        EVP_MD_CTX_free(context);
        fail("SHA-256 finalization failed");
    }
    EVP_MD_CTX_free(context);
    if (close(source) != 0) {
        fail("cannot close source: %s", strerror(errno));
    }

    char hex[65];
    hex_digest(digest, hex);
    if (expected[0] != '\0' && strcmp(expected, hex) != 0) {
        fail("source digest differs from expected");
    }
    printf(
        "{\"result\":\"ok\",\"mode\":\"hash-only\","
        "\"sha256\":\"sha256:%s\",\"size_bytes\":%lld}\n",
        hex,
        (long long)source_stat.st_size
    );
}

int main(int argc, char **argv) {
    install_signal_handlers();
    if (
        (argc == 3 || argc == 4) &&
        strcmp(argv[1], "--hash-only") == 0
    ) {
        hash_only(argv[2], argc == 4 ? argv[3] : "");
        return 0;
    }
    if (argc < 3 || argc > 4) {
        fail(
            "usage: copyhash SOURCE DESTINATION [EXPECTED_SHA256] "
            "or copyhash --hash-only SOURCE [EXPECTED_SHA256]"
        );
    }
    const char *source_path = argv[1];
    const char *destination_path = argv[2];
    char expected[65];
    normalize_expected_digest(argc == 4 ? argv[3] : "", expected);

    struct stat source_stat;
    int source = open(source_path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (source < 0) {
        fail("cannot open source: %s", strerror(errno));
    }
    if (fstat(source, &source_stat) != 0) {
        int saved_errno = errno;
        close(source);
        fail("cannot stat open source: %s", strerror(saved_errno));
    }
    if (!S_ISREG(source_stat.st_mode)) {
        close(source);
        fail("source is not a regular file");
    }
    struct stat destination_stat;
    if (lstat(destination_path, &destination_stat) == 0) {
        fail("destination already exists");
    }
    if (errno != ENOENT) {
        fail("cannot inspect destination: %s", strerror(errno));
    }

    char *tmp_path = temporary_path(destination_path);
    active_temporary_path = tmp_path;
    int destination = open(
        tmp_path,
        O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC,
        source_stat.st_mode & 0777
    );
    if (destination < 0) {
        int saved_errno = errno;
        close(source);
        active_temporary_path = NULL;
        free(tmp_path);
        fail("cannot create temporary destination: %s", strerror(saved_errno));
    }

    EVP_MD_CTX *context = EVP_MD_CTX_new();
    if (!context || EVP_DigestInit_ex(context, EVP_sha256(), NULL) != 1) {
        close(source);
        close(destination);
        fail("cannot initialize SHA-256");
    }
    unsigned char *buffer = malloc(8 * 1024 * 1024);
    if (!buffer) {
        close(source);
        close(destination);
        EVP_MD_CTX_free(context);
        fail("out of memory");
    }
    off_t total = 0;
    while (true) {
        ssize_t read_count = read_retry(source, buffer, 8 * 1024 * 1024);
        if (read_count < 0) {
            int saved_errno = errno;
            free(buffer);
            close(source);
            close(destination);
            EVP_MD_CTX_free(context);
            fail("read failed: %s", strerror(saved_errno));
        }
        if (read_count == 0) {
            break;
        }
        if (EVP_DigestUpdate(context, buffer, (size_t)read_count) != 1) {
            free(buffer);
            close(source);
            close(destination);
            EVP_MD_CTX_free(context);
            fail("SHA-256 update failed");
        }
        ssize_t written_total = 0;
        while (written_total < read_count) {
            ssize_t written = write_retry(
                destination,
                buffer + written_total,
                (size_t)(read_count - written_total)
            );
            if (written < 0) {
                int saved_errno = errno;
                free(buffer);
                close(source);
                close(destination);
                EVP_MD_CTX_free(context);
                fail("write failed: %s", strerror(saved_errno));
            }
            if (written == 0) {
                free(buffer);
                close(source);
                close(destination);
                EVP_MD_CTX_free(context);
                fail("write returned zero bytes");
            }
            written_total += written;
        }
        total += read_count;
    }
    free(buffer);
    struct stat source_after;
    if (fstat(source, &source_after) != 0) {
        int saved_errno = errno;
        close(source);
        close(destination);
        EVP_MD_CTX_free(context);
        fail("cannot restat open source: %s", strerror(saved_errno));
    }
    if (
        total != source_stat.st_size ||
        source_after.st_dev != source_stat.st_dev ||
        source_after.st_ino != source_stat.st_ino ||
        source_after.st_size != source_stat.st_size ||
        !same_timespec(source_after.st_mtim, source_stat.st_mtim) ||
        !same_timespec(source_after.st_ctim, source_stat.st_ctim)
    ) {
        close(source);
        close(destination);
        EVP_MD_CTX_free(context);
        fail("source changed while copying");
    }
    unsigned char digest[32];
    unsigned int digest_length = 0;
    if (
        EVP_DigestFinal_ex(context, digest, &digest_length) != 1 ||
        digest_length != sizeof(digest)
    ) {
        close(source);
        close(destination);
        EVP_MD_CTX_free(context);
        fail("SHA-256 finalization failed");
    }
    EVP_MD_CTX_free(context);
    char hex[65];
    hex_digest(digest, hex);
    if (expected[0] != '\0' && strcmp(expected, hex) != 0) {
        close(source);
        close(destination);
        fail("source digest differs from expected");
    }

    if (fchmod(destination, source_stat.st_mode & 0777) != 0) {
        int saved_errno = errno;
        close(source);
        close(destination);
        fail("chmod failed: %s", strerror(saved_errno));
    }
    struct timespec times[2];
    times[0] = source_stat.st_atim;
    times[1] = source_stat.st_mtim;
    if (futimens(destination, times) != 0) {
        int saved_errno = errno;
        close(source);
        close(destination);
        fail("mtime preservation failed: %s", strerror(saved_errno));
    }
    if (fsync(destination) != 0) {
        int saved_errno = errno;
        close(source);
        close(destination);
        fail("fsync failed: %s", strerror(saved_errno));
    }
    if (close(destination) != 0) {
        int saved_errno = errno;
        close(source);
        fail("close destination failed: %s", strerror(saved_errno));
    }
    if (close(source) != 0) {
        int saved_errno = errno;
        fail("close source failed: %s", strerror(saved_errno));
    }
    publish_no_replace(tmp_path, destination_path);
    fsync_parent_directory(destination_path);
    active_temporary_path = NULL;
    free(tmp_path);
    printf(
        "{\"result\":\"ok\",\"sha256\":\"sha256:%s\",\"size_bytes\":%lld}\n",
        hex,
        (long long)source_stat.st_size
    );
    return 0;
}
