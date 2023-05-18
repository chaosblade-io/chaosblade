module github.com/chaosblade-io/chaosblade

go 1.13

require (
	github.com/chaosblade-io/chaosblade-exec-cloud v1.7.2
	github.com/chaosblade-io/chaosblade-exec-cri v1.7.2
	github.com/chaosblade-io/chaosblade-exec-middleware v1.7.2
	github.com/chaosblade-io/chaosblade-exec-os v1.7.2
	github.com/chaosblade-io/chaosblade-operator v1.7.2
	github.com/chaosblade-io/chaosblade-spec-go v1.7.2
	github.com/mattn/go-sqlite3 v1.10.1-0.20190217174029-ad30583d8387
	github.com/olekukonko/tablewriter v0.0.5-0.20201029120751-42e21c7531a3
	github.com/shirou/gopsutil v3.21.8-0.20210816101416-f86a04298073+incompatible
	github.com/spf13/cobra v1.0.0
	github.com/spf13/pflag v1.0.5
	golang.org/x/crypto v0.1.0
	k8s.io/apimachinery v0.20.6
	k8s.io/client-go v12.0.0+incompatible
	sigs.k8s.io/controller-runtime v0.6.0
)

replace k8s.io/client-go => k8s.io/client-go v0.20.6
