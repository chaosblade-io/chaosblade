/*
 * Copyright 2025 The ChaosBlade Authors
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
	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade/version"
)

type VersionCommand struct {
	baseCommand
}

func (vc *VersionCommand) Init() {
	vc.command = &cobra.Command{
		Use:     "version",
		Short:   "Print version info",
		Long:    "Print detailed version information including Git details",
		Aliases: []string{"v"},
		Run: func(cmd *cobra.Command, args []string) {
			cmd.Printf("ChaosBlade Version Information:\n")
			cmd.Printf("==============================\n")
			cmd.Printf("Version:     %s\n", version.Ver)
			cmd.Printf("Git Tag:     %s\n", version.GitTag)
			cmd.Printf("Git Commit:  %s\n", version.GitCommit)
			cmd.Printf("Git Branch:  %s\n", version.GitBranch)
			cmd.Printf("Build Time:  %s\n", version.BuildTime)

			if version.IsRelease() {
				cmd.Printf("Release:     Yes (Production)\n")
			} else {
				cmd.Printf("Release:     No (Development)\n")
			}

			cmd.Printf("==============================\n")
			return
		},
	}
}
