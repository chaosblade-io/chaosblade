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

void enter_namespace(char* target, char* type)
{
    char namespace_path[1024];
    sprintf(namespace_path, "/proc/%s/ns/%s", target, type);
    int fd = open(namespace_path, O_RDONLY);

    if (setns(fd, 0) == -1) {
        fprintf(stderr, "enter on %s namespace failed: %s\n", type, strerror(errno));
        exit(1);
    }
    close(fd);
}

void sig(int signum){}

int main(int argc, char *argv[]) {

    char *target;
    char *cmd;

    int stop = 0;
    int opt;
    int option_index = 0;
    char *string = "st:mpuni";

    while((opt =getopt(argc, argv, string))!= -1) {
        switch (opt) {
            case 's':
                stop = 1;
                break;
            case 't':
                target = optarg;
                break;
            case 'm':
                enter_namespace(target, "mnt");
                break;
            case 'p':
                enter_namespace(target, "pid");
                break;
            case 'u':
                enter_namespace(target, "uts");
                break;
            case 'n':
                enter_namespace(target, "net");
                break;
            case 'i':
                enter_namespace(target, "ipc");
                break;
            default:
                break;
        }

    }

    int i,j=0;

    char *args[256] = {NULL};

    for(i = optind; i < argc; i++, j++) {
        args[j] = argv[i];
    }

    if (strlen(target) < 1) {
        fprintf(stderr, "The required parameters [target] are missing\n");
        exit(1);
    }

    if(stop) {
        char *pe = "pause";
        prctl(PR_SET_NAME, pe);
        signal(SIGCONT,sig);
        pause();
        char *nc = "nsexec";
        prctl(PR_SET_NAME, nc);
    }

    pid_t pid;
    int status;

    if((pid = fork())<0) {
        status = -1;
    } else if(pid == 0){
        execvp(argv[optind], args);
        _exit(127);
    } else {
        while(waitpid(pid, &status, 0) < 0)
        {
            if(errno != EINTR)
            {
                status = -1;
                break;
            }
        }
    }

    return 0;
}