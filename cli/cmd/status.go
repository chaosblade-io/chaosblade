package cmd

import (
	"encoding/json"
	"os"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/spf13/cobra"
	"golang.org/x/crypto/ssh/terminal"
)

type StatusCommand struct {
	baseCommand
	exp         *expCommand
	commandType string
	target      string
	uid         string
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
	sc.command.Flags().StringVar(&sc.uid, "uid", "", "prepare or experiment uid")

}
func (sc *StatusCommand) runStatus(command *cobra.Command, args []string) error {
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
		} else if sc.target != "" {
			result, err = GetDS().QueryExperimentModelsByCommand(sc.target)
		} else {
			result, err = GetDS().ListExperimentModels()
		}
	case "prepare", "revoke", "p", "r":
		if uid != "" {
			result, err = GetDS().QueryPreparationByUid(uid)
		} else {
			result, err = GetDS().ListPreparationRecords()
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
