package cmd

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade/exec/kubernetes"
)

type QueryK8sCommand struct {
	baseCommand
	kubeconfig string
}

func (q *QueryK8sCommand) Init() {
	q.command = &cobra.Command{
		Use:   "k8s <UID>",
		Short: "Query status of the specify experiment by uid",
		Long:  "Query status of the specify experiment by uid",
		Args:  cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			return q.queryK8sExpStatus(cmd, args[0], args[1])
		},
		Example: q.queryK8sExample(),
	}
	q.command.Flags().StringVarP(&q.kubeconfig, "kubeconfig", "k", "", "the kubeconfig path")
}

func (q *QueryK8sCommand) queryK8sExample() string {
	return `blade query k8s create 29c3f9dab4abbc79`
}

// queryK8sExpStatus by uid
func (q *QueryK8sCommand) queryK8sExpStatus(command *cobra.Command, cmd, uid string) error {
	response, _ := kubernetes.QueryStatus(cmd, uid, q.kubeconfig)
	if response.Success {
		command.Println(response.Print())
	} else {
		return fmt.Errorf(response.Error())
	}
	return nil
}
