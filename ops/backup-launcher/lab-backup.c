/*
 * lab-backup: the one executable that holds Full Disk Access for the nightly
 * backup (#287).
 *
 * Under launchd the backup job reaches the removable backup volume only through
 * a Full Disk Access grant, and macOS ties that grant to an executable, not to
 * an account. Granted to the lab's Python interpreter, it reached every service
 * that runs that interpreter, the root-run ones included. This program holds
 * the grant instead. launchd starts it, so it is the responsible process for
 * the one command it starts. On the Mac mini, macOS decided a child's access
 * on the grant of a parent like this one, not on the child's own (tested
 * 2026-10-08, #287).
 *
 * So that the grant cannot be lent to anything else by changing how this is
 * called, it takes no arguments, reads nothing from its environment but
 * LAB_BACKUP_DIR, and starts one fixed command with a fixed argument list and
 * an environment of only PATH and LAB_BACKUP_DIR. Python runs with -I, so
 * neither the environment nor the working directory can add code to that run.
 * The command does the rest: it refuses a missing or placeholder
 * LAB_BACKUP_DIR and sends the failure alert itself.
 *
 * Build it as the operator, never as root, and install it root-owned:
 * ops/runbook-lab-account-and-daemons.md, step 4. The linker signs it ad hoc,
 * and an ad hoc signature names that exact build, so a rebuilt binary needs
 * its Full Disk Access grant again.
 *
 * Only the tests override the paths below, with -D at build time.
 */
#include <errno.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>

#ifndef LAB_BACKUP_PYTHON
#define LAB_BACKUP_PYTHON "/opt/homelab/.venv/bin/python"
#endif
#ifndef LAB_BACKUP_DB
#define LAB_BACKUP_DB "/var/homelab/lab.db"
#endif
#ifndef LAB_BACKUP_ALERT_CONFIG
#define LAB_BACKUP_ALERT_CONFIG "/etc/homelab/alert.json"
#endif
#define LAB_BACKUP_KEEP "14"

int main(int argc, char *argv[]) {
    (void)argv;
    if (argc != 1) {
        fprintf(stderr, "lab-backup: takes no arguments\n");
        return 2;
    }

    char *const command[] = {
        LAB_BACKUP_PYTHON, "-I", "-m", "lab.cli", "--db", LAB_BACKUP_DB, "backup",
        "--keep", LAB_BACKUP_KEEP, "--alert-config", LAB_BACKUP_ALERT_CONFIG, NULL,
    };

    static char path[] = "PATH=/usr/bin:/bin:/usr/sbin:/sbin";
    char *environment[] = {path, NULL, NULL};
    const char *dir = getenv("LAB_BACKUP_DIR");
    if (dir != NULL) {
        size_t size = strlen("LAB_BACKUP_DIR=") + strlen(dir) + 1;
        char *entry = malloc(size);
        if (entry == NULL) {
            fprintf(stderr, "lab-backup: out of memory\n");
            return 1;
        }
        snprintf(entry, size, "LAB_BACKUP_DIR=%s", dir);
        environment[1] = entry;
    }

    pid_t pid;
    int failed = posix_spawn(&pid, LAB_BACKUP_PYTHON, NULL, NULL, command, environment);
    if (failed != 0) {
        fprintf(stderr, "lab-backup: cannot start %s: %s\n", LAB_BACKUP_PYTHON,
                strerror(failed));
        return 127;
    }

    int status;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) {
            fprintf(stderr, "lab-backup: lost the backup process: %s\n", strerror(errno));
            return 1;
        }
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        fprintf(stderr, "lab-backup: the backup was ended by signal %d\n", WTERMSIG(status));
        return 128 + WTERMSIG(status);
    }
    return 1;
}
