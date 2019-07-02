package main

import (
	"fmt"
	"github.com/chaosblade-io/chaosblade/exec/jvm"
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/chaosblade-io/chaosblade/util"
	"github.com/spf13/cobra"
	"net"
	"path"
	"strconv"
)

type PrepareJvmCommand struct {
	baseCommand
	javaHome    string
	processName string
	// sandboxHome is jvm-sandbox home, default value is CHAOSBLADE_HOME/lib
	sandboxHome string
}

func (pc *PrepareJvmCommand) Init() {
	pc.command = &cobra.Command{
		Use:   "jvm",
		Short: "Attach a type agent to the jvm process",
		Long:  "Attach a type agent to the jvm process for java framework experiment.",
		RunE: func(cmd *cobra.Command, args []string) error {
			return pc.prepareJvm()
		},
		Example: pc.prepareExample(),
	}
	pc.command.Flags().StringVarP(&pc.javaHome, "javaHome", "j", "", "the java jdk home path")
	pc.command.Flags().StringVarP(&pc.processName, "process", "p", "", "the java application process name (required)")
	pc.command.MarkFlagRequired("process")
	pc.sandboxHome = path.Join(util.GetLibHome(), "sandbox")
}

func (pc *PrepareJvmCommand) prepareExample() string {
	return `prepare jvm --process tomcat`
}

func (pc *PrepareJvmCommand) prepareJvm() error {
	record, err := GetDS().QueryRunningPreByTypeAndProcess(PrepareJvmType, pc.processName)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.DatabaseError],
			fmt.Sprintf("query attach java process record err, %s", err.Error()))
	}
	if record == nil || record.Status != "Running" {
		port, err := getAndCacheSandboxPort()
		if err != nil {
			return transport.ReturnFail(transport.Code[transport.ServerError],
				fmt.Sprintf("get sandbox port err, %s", err.Error()))
		}
		record, err = pc.insertPrepareRecord(PrepareJvmType, pc.processName, port)
		if err != nil {
			return transport.ReturnFail(transport.Code[transport.DatabaseError],
				fmt.Sprintf("insert prepare record err, %s", err.Error()))
		}
	}
	response := jvm.Attach(pc.processName, record.Port, pc.javaHome)
	return pc.handlePrepareResponse(record.Uid, pc.command, response)
}

// getSandboxPort by process name. If this process does not exist, an unbound port will be selected
func getAndCacheSandboxPort() (string, error) {
	port, err := getUnusedPort()
	if err != nil {
		return "", err
	}
	return strconv.Itoa(port), nil
}

func getUnusedPort() (int, error) {
	addr, err := net.ResolveTCPAddr("tcp", "localhost:0")
	if err != nil {
		return 0, err
	}
	listener, err := net.ListenTCP("tcp", addr)
	if err != nil {
		return 0, err
	}
	defer listener.Close()
	return listener.Addr().(*net.TCPAddr).Port, nil
}
