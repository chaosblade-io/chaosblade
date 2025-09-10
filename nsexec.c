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
#ifdef __linux__
#include <sys/prctl.h>
#endif
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
    char *string = "st:mpuni:";
    int use_absolute_path = 0;

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
        fprintf(stderr, "%d is not a valid process ID\n", target);
        return 1;
    }

    // pause
    if(stop) {
#ifdef __linux__
            char *pe = "pause";
            prctl(PR_SET_NAME, pe);
#endif
            signal(SIGCONT,sig);
            pause();
#ifdef __linux__
            char *nc = "nsexec";
            prctl(PR_SET_NAME, nc);
#endif
    }

    // 保存原始环境变量
    char *original_path = getenv("PATH");
    char *original_home = getenv("HOME");
    
    // enter namespace
    if(ipcns) {
        if (enter_ns(target, "ipc") < 0) {
            fprintf(stderr, "Failed to enter IPC namespace\n");
            return 1;
        }
    }

    if(utsns) {
        if (enter_ns(target, "uts") < 0) {
            fprintf(stderr, "Failed to enter UTS namespace\n");
            return 1;
        }
    }

    if(netns) {
        if (enter_ns(target, "net") < 0) {
            fprintf(stderr, "Failed to enter NET namespace\n");
            return 1;
        }
    }

    if(pidns) {
        if (enter_ns(target, "pid") < 0) {
            fprintf(stderr, "Failed to enter PID namespace\n");
            return 1;
        }
    }

    if(mntns) {
        if (enter_ns(target, "mnt") < 0) {
            fprintf(stderr, "Failed to enter MNT namespace\n");
            return 1;
        }
    }

    // fork exec
    pid_t pid;
    int status;

    if((pid = fork())<0) {
        status = -1;
    } else if(pid == 0){
        // 如果PATH为空或无效，尝试恢复原始PATH
        char *current_path = getenv("PATH");
        if (current_path == NULL || strlen(current_path) == 0) {
            if (original_path != NULL) {
                setenv("PATH", original_path, 1);
            }
        }
        
        // 确保PATH包含/bin路径
        current_path = getenv("PATH");
        if (current_path != NULL) {
            // 检查PATH中是否包含独立的/bin路径（不是/usr/bin等）
            char *path_copy = strdup(current_path);
            char *dir = strtok(path_copy, ":");
            int has_bin = 0;
            while (dir != NULL) {
                if (strcmp(dir, "/bin") == 0) {
                    has_bin = 1;
                    break;
                }
                dir = strtok(NULL, ":");
            }
            free(path_copy);
            
            if (!has_bin) {
                char new_path[2048];
                snprintf(new_path, sizeof(new_path), "%s:/bin", current_path);
                setenv("PATH", new_path, 1);
            }
        }
        
        // 检查命令是否存在和可执行
        if (access(argv[optind], F_OK) != 0) {
            // 尝试在PATH中查找命令
            char *path = getenv("PATH");
            if (path != NULL) {
                char *path_copy = strdup(path);
                char *dir = strtok(path_copy, ":");
                char found_path[1024] = {0};
                
                while (dir != NULL) {
                    char full_path[1024];
                    snprintf(full_path, sizeof(full_path), "%s/%s", dir, argv[optind]);
                    if (access(full_path, F_OK) == 0) {
                        strncpy(found_path, full_path, sizeof(found_path) - 1);
                        break;
                    }
                    dir = strtok(NULL, ":");
                }
                free(path_copy);
                
                // 如果找到了命令，更新argv[optind]
                if (strlen(found_path) > 0) {
                    argv[optind] = found_path;
                }
            }
        }
        
        // args
        int i,j=0;
        char *args[256] = {NULL};
        for(i = optind; i < argc; i++, j++) {
            args[j] = argv[i];
        }
        execvp(argv[optind], args);
        
        // 如果execvp失败，输出错误信息
        fprintf(stderr, "execvp failed: %s\n", strerror(errno));
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
