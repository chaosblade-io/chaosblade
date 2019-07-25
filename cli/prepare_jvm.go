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
	"strings"
)

type PrepareJvmCommand struct {
	baseCommand
	javaHome    string
	processName string
	// sandboxHome is jvm-sandbox home, default value is CHAOSBLADE_HOME/lib
	sandboxHome string
	port        int
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
	pc.command.Flags().IntVarP(&pc.port, "port", "P", 0, "the port used for agent server")
	pc.command.MarkFlagRequired("process")
	pc.sandboxHome = path.Join(util.GetLibHome(), "sandbox")
}

func (pc *PrepareJvmCommand) prepareExample() string {
	return `prepare jvm --process tomcat`
}

// prepareJvm means attaching java agent
func (pc *PrepareJvmCommand) prepareJvm() error {
	// query record from sqlite by process name
	record, err := GetDS().QueryRunningPreByTypeAndProcess(PrepareJvmType, pc.processName)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.DatabaseError],
			fmt.Sprintf("query attach java process record err, %s", err.Error()))
	}
	if record == nil || record.Status != "Running" {
		var port string
		if pc.port != 0 {
			// get port from flag value user passed
			port = strconv.Itoa(pc.port)
		} else {
			// get port from local port
			port, err = getAndCacheSandboxPort()
			if err != nil {
				return transport.ReturnFail(transport.Code[transport.ServerError],
					fmt.Sprintf("get sandbox port err, %s", err.Error()))
			}
		}
		record, err = pc.insertPrepareRecord(PrepareJvmType, pc.processName, port)
		if err != nil {
			return transport.ReturnFail(transport.Code[transport.DatabaseError],
				fmt.Sprintf("insert prepare record err, %s", err.Error()))
		}
	} else {
		if pc.port != 0 && strconv.Itoa(pc.port) != record.Port {
			return transport.ReturnFail(transport.Code[transport.IllegalParameters],
				fmt.Sprintf("the process has been executed prepare command, if you wan't re-prepare, "+
					"please append or modify the --port %s argument in prepare command for retry", record.Port))
		}
	}
	response := jvm.Attach(pc.processName, record.Port, pc.javaHome)
	if !response.Success {
		// if attach failed, search port from ~/.sandbox.token
		port, err := jvm.CheckPortFromSandboxToken()
		if err == nil && strings.Contains(response.Err, "connection refused") {
			response.Err = fmt.Sprintf("%s, append or modify the --port %s argument in prepare command for retry",
				response.Err, port)
		}
	}
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
