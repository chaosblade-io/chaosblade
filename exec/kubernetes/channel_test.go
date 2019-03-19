package kubernetes

import (
	"testing"
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/sirupsen/logrus"
)

func TestChannel_GetBladePodByContainer(t *testing.T) {
	channel := &Channel{
		channel: exec.NewLocalChannel(),
	}
	pod, err := channel.GetBladePodByContainer("5b282c9624", "", "weave", "")
	if err != nil {
		logrus.Fatalf(err.Error())
	}
	logrus.Infof("blade pod: %s", pod)
}
