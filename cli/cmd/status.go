/*
 * Copyright 1999-2020 Alibaba Group Holding Ltd.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package cmd

import (
	"encoding/json"
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
	action      string
	flag        string
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
	sc.command.Flags().StringVar(&sc.commandType, "type", "", "command type, prepare|create|destroy|revoke")
	sc.command.Flags().StringVar(&sc.target, "target", "", "experiment target, for example: dubbo")
	sc.command.Flags().StringVar(&sc.action, "action", "", "sub command, for example:fullload")
	sc.command.Flags().StringVar(&sc.flag, "flag-filter", "", "flag can do fuzzy search")
	sc.command.Flags().StringVar(&sc.limit, "limit", "", "limit the count of experiments, support OFFSET clause, for example, limit 4,3 returns only 3 items starting from the 5 position item")
	sc.command.Flags().StringVar(&sc.status, "status", "", "experiment status. create type supports Created|Success|Error|Destroyed status. prepare type supports Created|Running|Error|Revoked status")
	sc.command.Flags().StringVar(&sc.uid, "uid", "", "prepare or experiment uid")
	sc.command.Flags().BoolVar(&sc.asc, "asc", false, "order by CreateTime, default value is false that means order by CreateTime desc")

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
		} else {
			result, err = GetDS().QueryExperimentModels(sc.target, sc.action, sc.flag, sc.status, sc.limit, sc.asc)
		}
	case "prepare", "revoke", "p", "r":
		if uid != "" {
			result, err = GetDS().QueryPreparationByUid(uid)
		} else {
			result, err = GetDS().QueryPreparationRecords(sc.target, sc.status, sc.action, sc.flag, sc.limit, sc.asc)
		}
	default:
		if uid == "" {
			return spec.ResponseFailWithFlags(spec.ParameterLess, "type|uid", "must specify the right type or uid")
		}
		result, err = GetDS().QueryExperimentModelByUid(uid)
		if util.IsNil(result) || err != nil {
			result, err = GetDS().QueryPreparationByUid(uid)
		}
	}
	if err != nil {
		return spec.ResponseFailWithFlags(spec.DatabaseError, "query", err)
	}
	if util.IsNil(result) {
		return spec.ResponseFailWithFlags(spec.DataNotFound, uid)
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
