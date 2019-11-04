package cmd

import (
	"fmt"
	"strconv"

	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade/exec/cplus"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
)

type PrepareCPlusCommand struct {
	baseCommand
	port           int
	scriptLocation string
	waitTime       int
	javaHome       string
}

func (pc *PrepareCPlusCommand) Init() {
	pc.command = &cobra.Command{
		Use:   "cplus",
		Short: "Active cplus agent.",
		Long:  "Active cplus agent.",
		RunE: func(cmd *cobra.Command, args []string) error {
			return pc.prepareCPlus()
		},
		Example: pc.prepareExample(),
	}
	pc.command.Flags().IntVarP(&pc.port, "port", "p", 8703, "the server port of cplus proxy")
	pc.command.Flags().StringVarP(&pc.scriptLocation, "script-location", "l", "", "the script files directory")
	pc.command.Flags().IntVarP(&pc.waitTime, "wait-time", "w", 6, "waiting time of preparation phase, unit is second")
	pc.command.Flags().StringVarP(&pc.javaHome, "javaHome", "j", "", "the java jdk home path")
	pc.command.MarkFlagRequired("port")
}

func (pc *PrepareCPlusCommand) prepareExample() string {
	return `prepare cplus --port 8703 --wait-time 10`
}

func (pc *PrepareCPlusCommand) prepareCPlus() error {
	portStr := strconv.Itoa(pc.port)
	record, err := GetDS().QueryRunningPreByTypeAndProcess(PrepareCPlusType, portStr, "")
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.DatabaseError],
			fmt.Sprintf("query cplus agent server port record err, %s", err.Error()))
	}
	if record == nil || record.Status != "Running" {
		record, err = insertPrepareRecord(PrepareCPlusType, portStr, portStr, "")
		if err != nil {
			return spec.ReturnFail(spec.Code[spec.DatabaseError],
				fmt.Sprintf("insert prepare record err, %s", err.Error()))
		}
	}
	response := cplus.Prepare(portStr, pc.scriptLocation, pc.waitTime, pc.javaHome)
	return handlePrepareResponse(record.Uid, pc.command, response)
}
