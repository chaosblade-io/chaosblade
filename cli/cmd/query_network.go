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
	"fmt"
	"net"
	"strings"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/spf13/cobra"
)

type QueryNetworkCommand struct {
	baseCommand
}

const InterfaceArg = "interface"

func (qnc *QueryNetworkCommand) Init() {
	qnc.command = &cobra.Command{
		Use:     "network interface",
		Aliases: []string{"net"},
		Short:   "Query network information",
		Long:    "Query network information for chaos experiments of network",
		Args:    cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return qnc.queryNetworkInfo(cmd, args[0])
		},
		Example: qnc.queryNetworkExample(),
	}
}

func (qnc *QueryNetworkCommand) queryNetworkExample() string {
	return `blade query network interface`
}

func (qnc *QueryNetworkCommand) queryNetworkInfo(command *cobra.Command, arg string) error {
	switch arg {
	case InterfaceArg:
		interfaces, err := net.Interfaces()
		if err != nil {
			return err
		}
		names := make([]string, 0)
		for _, i := range interfaces {
			if strings.Contains(i.Flags.String(), "up") {
				names = append(names, i.Name)
			}
		}
		command.Println(spec.ReturnSuccess(names))
	default:
		return fmt.Errorf("the %s argument not found", arg)
	}
	return nil
}
