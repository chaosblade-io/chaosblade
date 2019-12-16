package cmd

import (
	"encoding/json"
	"errors"
	"os"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/spf13/cobra"
	"golang.org/x/crypto/ssh/terminal"
)

const (
	Created   = "Created"
	Success   = "Success"
	Running   = "Running"
	Error     = "Error"
	Destroyed = "Destroyed"
	Revoked   = "Revoked"
)

type StatusCommand struct {
	baseCommand
	commandType string
	target      string
	uid         string
	limit       string
	status      string
	asc         bool
}

func (sc *StatusCommand) Init() {
	sc.command = &cobra.Command{
		Use:     "status",
		Short:   "Query preparation stage or experiment status",
		Long:    "Query preparation stage or experiment status",
		Aliases: []string{"s"},
		RunE: func(cmd *cobra.Command, args []string) error {
			return sc.runStatus(cmd, args)
		},
		Example: statusExample(),
	}
	sc.command.Flags().StringVar(&sc.commandType, "type", "", "command type, attach|create|destroy|detach")
	sc.command.Flags().StringVar(&sc.target, "target", "", "experiment target, for example: dubbo")
	sc.command.Flags().StringVar(&sc.limit, "limit", "", "limit the count of experiments, support OFFSET clause, for example, limit 4,3 returns only 3 items starting from the 5 position item")
	sc.command.Flags().StringVar(&sc.status, "status", "", "experiment status. create type supports Created|Success|Error|Destroyed status. prepare type supports Created|Running|Error|Revoked status")
	sc.command.Flags().StringVar(&sc.uid, "uid", "", "prepare or experiment uid")
	sc.command.Flags().BoolVar(&sc.asc, "asc", false, "order by CreateTime, default value is false that means order by CreateTime desc")

}
func (sc *StatusCommand) runStatus(command *cobra.Command, args []string) error {
	retries := "1"
	seconds := 2
	url := "https://chaosblade.io"
	simpleError := errors.New("a simple error")
	log.Error(simpleError, "test_err", "url", url)
	log.V(4).Info("got a retry-after response when requesting url", "attempt", retries, "after seconds", seconds, "url", url)
	log.V(0).Info("test info")
	log.V(-1).Info("test warn")
	log.V(1).Info("test debug")
	log.Info("test")

	var uid = ""
	if len(args) > 0 {
		uid = args[0]
	} else {
		uid = sc.uid
	}
	var result interface{}
	var err error
	switch sc.commandType {
	case "create", "destroy", "c", "d":
		if uid != "" {
			result, err = GetDS().QueryExperimentModelByUid(uid)
		} else {
			result, err = GetDS().QueryExperimentModels(sc.target, sc.status, sc.limit, sc.asc)
		}
	case "prepare", "revoke", "p", "r":
		if uid != "" {
			result, err = GetDS().QueryPreparationByUid(uid)
		} else {
			result, err = GetDS().QueryPreparationRecords(sc.target, sc.status, sc.limit, sc.asc)
		}
	default:
		if uid == "" {
			return spec.ReturnFail(spec.Code[spec.IllegalCommand], "must specify the right type or uid")
		}
		result, err = GetDS().QueryExperimentModelByUid(uid)
		if util.IsNil(result) || err != nil {
			result, err = GetDS().QueryPreparationByUid(uid)
		}
	}
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.DatabaseError], err.Error())
	}
	if util.IsNil(result) {
		return spec.Return(spec.Code[spec.DataNotFound])
	}
	response := spec.ReturnSuccess(result)

	if terminal.IsTerminal(int(os.Stdout.Fd())) {
		bytes, err := json.MarshalIndent(response, "", "\t")
		if err != nil {
			return response
		}
		sc.command.Println(string(bytes))
	} else {
		sc.command.Println(response.Print())
	}
	return nil
}

func statusExample() string {
	return `# Query by UID
blade status cc015e9bd9c68406
# Query chaos experiments
blade status --type create
# Query preparations
blade status --type prepare`
}
