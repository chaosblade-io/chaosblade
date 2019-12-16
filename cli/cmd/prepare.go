package cmd

import (
	"fmt"
	"time"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade/data"
)

const (
	PrepareJvmType   = "jvm"
	PrepareK8sType   = "k8s"
	PrepareCPlusType = "cplus"
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
			return spec.ReturnFail(spec.Code[spec.IllegalCommand],
				fmt.Sprintf("less command type to prepare"))
		},
		Example: pc.prepareExample(),
	}
}

func (pc *PrepareCommand) prepareExample() string {
	return `prepare jvm --process tomcat`
}

// insertPrepareRecord
func insertPrepareRecord(prepareType string, processName, port, processId string) (*data.PreparationRecord, error) {
	uid, err := util.GenerateUid()
	if err != nil {
		return nil, err
	}
	record := &data.PreparationRecord{
		Uid:         uid,
		ProgramType: prepareType,
		Process:     processName,
		Port:        port,
		Pid:         processId,
		Status:      Created,
		Error:       "",
		CreateTime:  time.Now().Format(time.RFC3339Nano),
		UpdateTime:  time.Now().Format(time.RFC3339Nano),
	}
	err = GetDS().InsertPreparationRecord(record)
	if err != nil {
		return nil, err
	}
	return record, nil
}

func handlePrepareResponse(uid string, cmd *cobra.Command, response *spec.Response) error {
	response.Result = uid
	if !response.Success {
		GetDS().UpdatePreparationRecordByUid(uid, Error, response.Err)
		return response
	}
	err := GetDS().UpdatePreparationRecordByUid(uid, Running, "")
	if err != nil {
		//logrus.Warningf("update preparation record error: %s", err.Error())
		log.V(-1).Info("update preparation record error", "err_msg", err.Error())
	}
	response.Result = uid
	cmd.Println(response.Print())
	return nil
}

func updatePreparationPort(uid, port string) error {
	return GetDS().UpdatePreparationPortByUid(uid, port)
}

func updatePreparationPid(uid, pid string) error {
	return GetDS().UpdatePreparationPidByUid(uid, pid)
}
