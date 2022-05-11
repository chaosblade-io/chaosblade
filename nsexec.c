#define _GNU_SOURCE
#include <stdio.h>
#include <unistd.h>
#include <errno.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <getopt.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>

extern char** environ;

int enter_ns(int pid, const char* type) {
#ifdef __NR_setns
    char path[64], selfpath[64];
    snprintf(path, sizeof(path), "/proc/%d/ns/%s", pid, type);
    snprintf(selfpath, sizeof(selfpath), "/proc/self/ns/%s", type);

    struct stat oldns_stat, newns_stat;
    if (stat(selfpath, &oldns_stat) == 0 && stat(path, &newns_stat) == 0) {
        // Don't try to call setns() if we're in the same namespace already
        if (oldns_stat.st_ino != newns_stat.st_ino) {
            int newns = open(path, O_RDONLY);
            if (newns < 0) {
                return -1;
            }

            // Some ancient Linux distributions do not have setns() function
            int result = syscall(__NR_setns, newns, 0);
            close(newns);
            return result < 0 ? -1 : 1;
        }
    }
#endif // __NR_setns
    return 0;
}

void sig(int signum){}

int main(int argc, char *argv[]) {

    int target = 0;
    char *cmd;

    int stop = 0;
    int opt;
    int option_index = 0;
    char *string = "st:mpuni";

    int ipcns = 0;
    int utsns = 0;
    int netns = 0;
    int pidns = 0;
    int mntns = 0;

    while((opt =getopt(argc, argv, string))!= -1) {
        switch (opt) {
            case 's':
                stop = 1;
                break;
            case 't':
                target = atoi(optarg);
                break;
            case 'm':
                mntns = 1;
                break;
            case 'p':
                pidns = 1;
                break;
            case 'u':
                utsns = 1;
                break;
            case 'n':
                netns = 1;
                break;
            case 'i':
                ipcns = 1;
                break;
            default:
                break;
        }
    }

    // check target pid
    if (target <= 0) {
        fprintf(stderr, "%s is not a valid process ID\n", target);
        return 1;
    }

    // pause
    if(stop) {
            char *pe = "pause";
            prctl(PR_SET_NAME, pe);
            signal(SIGCONT,sig);
            pause();
            char *nc = "nsexec";
            prctl(PR_SET_NAME, nc);
    }

    // enter namespace
    if(ipcns) {
        enter_ns(target, "ipc");
    }

    if(utsns) {
        enter_ns(target, "uts");
    }

    if(netns) {
        enter_ns(target, "net");
    }

    if(pidns) {
        enter_ns(target, "pid");
    }

    if(mntns) {
        enter_ns(target, "mnt");
    }

    // fork exec
    pid_t pid;
    int status;

    if((pid = fork())<0) {
        status = -1;
    } else if(pid == 0){
        // args
        int i,j=0;
        char *args[256] = {NULL};
        for(i = optind; i < argc; i++, j++) {
            args[j] = argv[i];
        }
        execvp(argv[optind], args);
        _exit(127);
    } else {
        while(waitpid(pid, &status, 0) < 0){
            if(errno != EINTR){
                status = -1;
                break;
            }
        }
        if(WIFEXITED(status)){
            exit(WEXITSTATUS(status));
        }
    }
    return 0;
}
