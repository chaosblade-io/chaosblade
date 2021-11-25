module github.com/chaosblade-io/chaosblade

go 1.16

require (
	github.com/chaosblade-io/chaosblade-exec-cri v1.3.1-0.20210906073714-7bd7d7367d76
	github.com/chaosblade-io/chaosblade-exec-docker v1.3.1-0.20210906073714-7bd7d7367d76
	github.com/chaosblade-io/chaosblade-exec-os v1.3.1-0.20210906070659-0b8e3c15c25b
	github.com/chaosblade-io/chaosblade-operator v1.3.1-0.20210906074054-831b748528b9
	github.com/chaosblade-io/chaosblade-spec-go v1.3.1-0.20210906082427-bfa0d01f5621
	github.com/mattn/go-sqlite3 v1.10.1-0.20190217174029-ad30583d8387
	github.com/olekukonko/tablewriter v0.0.5-0.20201029120751-42e21c7531a3
	github.com/shirou/gopsutil v3.21.6+incompatible
	github.com/sirupsen/logrus v1.8.1
	github.com/spf13/cobra v1.1.1
	github.com/spf13/pflag v1.0.5
	golang.org/x/crypto v0.0.0-20210711020723-a769d52b0f97
	k8s.io/apimachinery v0.20.6
	k8s.io/client-go v12.0.0+incompatible
	sigs.k8s.io/controller-runtime v0.6.0
)

replace k8s.io/client-go => k8s.io/client-go v0.20.6
