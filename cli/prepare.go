package main

import (
	"github.com/spf13/cobra"
	"fmt"
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/sirupsen/logrus"
	"github.com/chaosblade-io/chaosblade/util"
	"github.com/chaosblade-io/chaosblade/data"
	"time"
)

const (
	PrepareJvmType = "jvm"
	PrepareK8sType = "k8s"
)

// PrepareCommand defines attach command
type PrepareCommand struct {
	// baseCommand is basic implementation of command interface
	baseCommand
}

// Init attach command operators includes create instance and bind flags
func (pc *PrepareCommand) Init() {
	pc.command = &cobra.Command{
		Use:     "prepare",
		Aliases: []string{"p"},
		Short:   "Prepare to experiment",
		Long:    "Prepare to experiment, for example, attach agent to java process or deploy agent to kubernetes cluster.",
		RunE: func(cmd *cobra.Command, args []string) error {
			return transport.ReturnFail(transport.Code[transport.IllegalCommand],
				fmt.Sprintf("less command type to prepare"))
		},
		Example: pc.prepareExample(),
	}
}

func (pc *PrepareCommand) prepareExample() string {
	return `prepare jvm --process tomcat`
}

// insertPrepareRecord
func (pc *PrepareJvmCommand) insertPrepareRecord(prepareType string, flags ...string) (*data.PreparationRecord, error) {
	uid, err := util.GenerateUid()
	if err != nil {
		return nil, err
	}
	record := &data.PreparationRecord{
		Uid:         uid,
		ProgramType: prepareType,
		Process:     flags[0],
		Status:      "Created",
		Error:       "",
		CreateTime:  time.Now().Format(time.RFC3339Nano),
		UpdateTime:  time.Now().Format(time.RFC3339Nano),
	}
	if len(flags) > 1 {
		record.Port = flags[1]
	}
	err = GetDS().InsertPreparationRecord(record)
	if err != nil {
		return nil, err
	}
	return record, nil
}

func (pc *PrepareJvmCommand) handlePrepareResponse(uid string, cmd *cobra.Command, response *transport.Response) error {
	if !response.Success {
		GetDS().UpdatePreparationRecordByUid(uid, "Error", response.Err)
		return response
	}
	err := GetDS().UpdatePreparationRecordByUid(uid, "Running", "")
	if err != nil {
		logrus.Warningf("update preparation record error: %s", err.Error())
	}
	response.Result = uid
	cmd.Println(response.Print())
	return nil
}
