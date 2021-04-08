module github.com/chaosblade-io/chaosblade

go 1.13

require (

	github.com/chaosblade-io/chaosblade-exec-docker v1.1.0-dev
	github.com/chaosblade-io/chaosblade-exec-os v1.1.0-dev
	github.com/chaosblade-io/chaosblade-operator v1.1.0-dev
	github.com/chaosblade-io/chaosblade-spec-go 1.1.0-dev
	github.com/mattn/go-sqlite3 v1.10.1-0.20190217174029-ad30583d8387
	github.com/olekukonko/tablewriter v0.0.5-0.20201029120751-42e21c7531a3
	github.com/prometheus/common v0.9.1
	github.com/shirou/gopsutil v2.20.5+incompatible
	github.com/sirupsen/logrus v1.5.0
	github.com/spf13/cobra v0.0.5
	github.com/spf13/pflag v1.0.5
	golang.org/x/crypto v0.0.0-20200220183623-bac4c82f6975
	k8s.io/apimachinery v0.17.4
	k8s.io/client-go v12.0.0+incompatible
	sigs.k8s.io/controller-runtime v0.5.3
)
