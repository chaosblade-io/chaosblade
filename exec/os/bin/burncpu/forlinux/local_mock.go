package main

import "github.com/containerd/cgroups"

type CgroupMock struct {
	cgroups.Cgroup
}

func (cgroupMock *CgroupMock) Add(process cgroups.Process) error {
	return nil
}
