package util

import (
	"os"
	"os/signal"
	"syscall"
	"github.com/sirupsen/logrus"
	"runtime"
)

type ShutdownHook interface {
	Shutdown() error
}

func Hold(hooks ...ShutdownHook) {
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGQUIT, syscall.SIGTERM)
	buf := make([]byte, 1<<10)
	for {
		switch <-sig {
		case syscall.SIGINT, syscall.SIGTERM:
			logrus.Warningln("received SIGINT/SIGTERM, exit")
			for _, hook := range hooks {
				hook.Shutdown()
			}
			return
		case syscall.SIGQUIT:
			for _, hook := range hooks {
				hook.Shutdown()
			}
			len := runtime.Stack(buf, true)
			logrus.Warningf("received SIGQUIT\n%s\n", buf[:len])
		}
	}
}
