package cmd

import (
	"os"

	"github.com/chaosblade-io/chaosblade/util"
	"github.com/spf13/cobra"
)

type Cli struct {
	rootCmd *cobra.Command
}

//NewCli returns the cli instance used to register and execute command
func NewCli() *Cli {
	cli := &Cli{
		rootCmd: &cobra.Command{
			Use:   "blade",
			Short: "An easy to use and powerful chaos toolkit",
			Long:  "An easy to use and powerful chaos engineering experiment toolkit",
		},
	}
	cli.rootCmd.SetOutput(os.Stdout)
	cli.setFlags()
	return cli
}

// setFlags defines flags for root command
func (cli *Cli) setFlags() {
	flags := cli.rootCmd.PersistentFlags()
	flags.BoolVarP(&util.Debug, "debug", "d", false, "Set client to DEBUG mode")
}

//Run command
func (cli *Cli) Run() error {
	return cli.rootCmd.Execute()
}
