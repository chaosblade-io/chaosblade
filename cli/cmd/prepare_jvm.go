package cmd

import (
	"fmt"
	"path"
	"strconv"
	"strings"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/sirupsen/logrus"
	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade/exec/jvm"
)

type PrepareJvmCommand struct {
	baseCommand
	javaHome    string
	processName string
	// sandboxHome is jvm-sandbox home, default value is CHAOSBLADE_HOME/lib
	sandboxHome string
	port        int
	processId   string
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
	pc.command.Flags().StringVarP(&pc.processId, "pid", "", "", "the target java process id")
	pc.sandboxHome = path.Join(util.GetLibHome(), "sandbox")
}

func (pc *PrepareJvmCommand) prepareExample() string {
	return `prepare jvm --process tomcat`
}

// prepareJvm means attaching java agent
func (pc *PrepareJvmCommand) prepareJvm() error {
	if pc.processName == "" && pc.processId == "" {
		return spec.ReturnFail(spec.Code[spec.IllegalParameters],
			fmt.Sprintf("less --process or --pid flags"))
	}
	pid, response := jvm.CheckFlagValues(pc.processName, pc.processId)
	if !response.Success {
		return response
	}
	pc.processId = pid
	record, err := GetDS().QueryRunningPreByTypeAndProcess(PrepareJvmType, pc.processName, pc.processId)
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.DatabaseError],
			fmt.Sprintf("query attach java process record err, %s", err.Error()))
	}
	if record == nil {
		var port string
		if pc.port != 0 {
			// get port from flag value user passed
			port = strconv.Itoa(pc.port)
		} else {
			// get port from local port
			port, err = getAndCacheSandboxPort()
			if err != nil {
				return spec.ReturnFail(spec.Code[spec.ServerError],
					fmt.Sprintf("get sandbox port err, %s", err.Error()))
			}
		}
		record, err = insertPrepareRecord(PrepareJvmType, pc.processName, port, pc.processId)
		if err != nil {
			return spec.ReturnFail(spec.Code[spec.DatabaseError],
				fmt.Sprintf("insert prepare record err, %s", err.Error()))
		}
	} else {
		if pc.port != 0 && strconv.Itoa(pc.port) != record.Port {
			return spec.ReturnFail(spec.Code[spec.IllegalParameters],
				fmt.Sprintf("the process has been executed prepare command, if you wan't re-prepare, "+
					"please append or modify the --port %s argument in prepare command for retry", record.Port))
		}
	}
	response, username := jvm.Attach(record.Port, pc.javaHome, pc.processId)
	if !response.Success && username != "" && strings.Contains(response.Err, "connection refused") {
		// if attach failed, search port from ~/.sandbox.token
		port, err := jvm.CheckPortFromSandboxToken(username)
		if err == nil {
			logrus.Infof("use %s port to retry", port)
			response, username = jvm.Attach(port, pc.javaHome, pc.processId)
			if response.Success {
				// update port
				err := updatePreparationPort(record.Uid, port)
				if err != nil {
					logrus.Warningf("update preparation port failed, %v", err)
				}
			}
		}
	}
	if record.Pid != pc.processId {
		// update pid
		updatePreparationPid(record.Uid, pc.processId)
	}
	return handlePrepareResponse(record.Uid, pc.command, response)
}

// getSandboxPort by process name. If this process does not exist, an unbound port will be selected
func getAndCacheSandboxPort() (string, error) {
	port, err := util.GetUnusedPort()
	if err != nil {
		return "", err
	}
	return strconv.Itoa(port), nil
}
