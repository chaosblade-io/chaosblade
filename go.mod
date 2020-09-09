module github.com/chaosblade-io/chaosblade

go 1.13

require (
	github.com/chaosblade-io/chaosblade-exec-docker v0.6.1-0.20200805071438-6d16a1434e52
	github.com/chaosblade-io/chaosblade-exec-os v0.6.1-0.20200805070637-52adf80fc207
	github.com/chaosblade-io/chaosblade-operator v0.6.0
	github.com/chaosblade-io/chaosblade-spec-go v0.6.1-0.20200713091457-d3932a4b0129
	github.com/gregjones/httpcache v0.0.0-20190611155906-901d90724c79 // indirect
	github.com/mattn/go-sqlite3 v1.10.1-0.20190217174029-ad30583d8387
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

replace (
	github.com/chaosblade-io/chaosblade-exec-docker => github.com/dhlhust/chaosblade-exec-docker v0.6.0
	github.com/chaosblade-io/chaosblade-exec-os => github.com/dhlhust/chaosblade-exec-os v0.6.0
	github.com/chaosblade-io/chaosblade-operator => github.com/dhlhust/chaosblade-operator v0.6.0
	github.com/chaosblade-io/chaosblade-spec-go => github.com/dhlhust/chaosblade-spec-go v0.6.0
)
