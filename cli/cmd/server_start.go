package cmd

import (
	"github.com/spf13/cobra"
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/sirupsen/logrus"
	"github.com/chaosblade-io/chaosblade/util"
	"path"
	"fmt"
	"time"
	"net/http"
	"os"
)

const startServerKey = "blade server start --nohup"

type StartServerCommand struct {
	baseCommand
	port  string
	nohup bool
}

func (ssc *StartServerCommand) Init() {
	ssc.command = &cobra.Command{
		Use:     "start",
		Short:   "Start server mode, exposes web services",
		Long:    "Start server mode, exposes web services. Under the mode, you can send http request to trigger experiments",
		Aliases: []string{"s"},
		RunE: func(cmd *cobra.Command, args []string) error {
			return ssc.run(cmd, args)
		},
		Example: startServerExample(),
	}
	ssc.command.Flags().StringVarP(&ssc.port, "port", "p", "9526", "service port")
	ssc.command.Flags().BoolVarP(&ssc.nohup, "nohup", "n", false, "used by internal")
}

func (ssc *StartServerCommand) run(cmd *cobra.Command, args []string) error {
	// check if the process named `blade server --start` exists or not
	pids, err := exec.GetPidsByProcessName(startServerKey, context.TODO())
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], err.Error())
	}
	if len(pids) > 0 {
		return transport.ReturnFail(transport.Code[transport.DuplicateError], "the chaosblade has been started. If you want to stop it, you can execute blade server stop command")
	}
	if ssc.nohup {
		ssc.start0()
	}
	err = ssc.start()
	if err != nil {
		return err
	}
	cmd.Printf("success, listening on %s", ssc.port)
	return nil
}

// start used nohup command and check the process
func (ssc *StartServerCommand) start() error {
	// use nohup to invoke blade server start command
	channel := exec.NewLocalChannel()
	bladeBin := path.Join(util.GetProgramPath(), "blade")
	response := channel.Run(context.TODO(), "nohup", fmt.Sprintf("%s server start --nohup --port %s > /dev/null 2>&1 &", bladeBin, ssc.port))
	if !response.Success {
		return response
	}
	time.Sleep(time.Second)
	// check process
	pids, err := exec.GetPidsByProcessName(startServerKey, context.TODO())
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], err.Error())
	}
	if len(pids) == 0 {
		// read logs
		logFile, err := util.GetLogFile(util.Blade)
		if err != nil {
			return transport.ReturnFail(transport.Code[transport.ServerError], "start blade server failed and can't get log file")
		}
		if !util.IsExist(logFile) {
			return transport.ReturnFail(transport.Code[transport.ServerError], "start blade server failed and log file does not exist")
		}
		response := channel.Run(context.TODO(), "tail", fmt.Sprintf("-1 %s", logFile))
		if !response.Success {
			return transport.ReturnFail(transport.Code[transport.ServerError], "start blade server failed and can't read log file")
		}
		return transport.ReturnFail(transport.Code[transport.ServerError], response.Result.(string))
	}
	logrus.Infof("start blade server success, listen on %s", ssc.port)
	return nil
}

// start0 starts web service
func (ssc *StartServerCommand) start0() {
	go func() {
		err := http.ListenAndServe(":"+ssc.port, nil)
		if err != nil {
			logrus.Errorf("start blade server error, %v", err)
			os.Exit(1)
		}
	}()
	Register("/chaosblade")
	util.Hold()
}

func Register(requestPath string) {
	http.HandleFunc(requestPath, func(writer http.ResponseWriter, request *http.Request) {
		err := request.ParseForm()
		if err != nil {
			fmt.Fprintf(writer,
				transport.ReturnFail(transport.Code[transport.IllegalParameters], err.Error()).Print())
			return
		}

		cmds := request.Form["cmd"]
		if len(cmds) != 1 {
			fmt.Fprintf(writer,
				transport.ReturnFail(transport.Code[transport.IllegalParameters], "illegal cmd parameter").Print())
			return
		}
		ctx := context.WithValue(context.Background(), "mode", "server")
		response := exec.NewLocalChannel().Run(ctx, path.Join(util.GetProgramPath(), "blade"), cmds[0])
		if response.Success {
			fmt.Fprintf(writer, response.Result.(string))
		} else {
			fmt.Fprintf(writer, response.Err)
		}
	})
}

func startServerExample() string {
	return `blade server start --port 8000`
}
