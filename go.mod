module github.com/chaosblade-io/chaosblade

go 1.13

require (
	github.com/chaosblade-io/chaosblade-exec-docker v1.2.1-0.20210728031159-9d3ff7ef66d9
	github.com/chaosblade-io/chaosblade-exec-os v1.2.1-0.20210728020317-5dbba71fd0f2
	github.com/chaosblade-io/chaosblade-operator v1.2.1-0.20210728085751-516b7e496510
	github.com/chaosblade-io/chaosblade-spec-go v1.2.1-0.20210727105127-cb88024e3ec2
	github.com/mattn/go-sqlite3 v1.10.1-0.20190217174029-ad30583d8387
	github.com/olekukonko/tablewriter v0.0.5-0.20201029120751-42e21c7531a3
	github.com/prometheus/common v0.9.1
	github.com/shirou/gopsutil v3.21.6+incompatible
	github.com/sirupsen/logrus v1.8.1
	github.com/spf13/cobra v0.0.5
	github.com/spf13/pflag v1.0.5
	golang.org/x/crypto v0.0.0-20210711020723-a769d52b0f97
	k8s.io/apimachinery v0.17.4
	k8s.io/client-go v12.0.0+incompatible
	sigs.k8s.io/controller-runtime v0.5.3
)

replace k8s.io/client-go => k8s.io/client-go v0.17.4
