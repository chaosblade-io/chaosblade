module github.com/chaosblade-io/chaosblade

go 1.13

require (
	github.com/chaosblade-io/chaosblade-exec-docker v0.4.1
	github.com/chaosblade-io/chaosblade-exec-os v0.4.0
	github.com/chaosblade-io/chaosblade-operator v0.4.1-0.20191216080032-f02da7845646
	github.com/chaosblade-io/chaosblade-spec-go v0.4.1-0.20191225105920-8d7c5f186698
	github.com/mattn/go-sqlite3 v1.10.1-0.20190217174029-ad30583d8387
	github.com/prometheus/common v0.2.0
	github.com/shirou/gopsutil v2.19.9+incompatible
	github.com/sirupsen/logrus v1.4.2
	github.com/spf13/cobra v0.0.4-0.20190109003409-7547e83b2d85
	github.com/spf13/pflag v1.0.4-0.20181223182923-24fa6976df40
	golang.org/x/crypto v0.0.0-20191011191535-87dc89f01550
	k8s.io/apimachinery v0.17.0
	k8s.io/client-go v11.0.0+incompatible
	sigs.k8s.io/controller-runtime v0.1.12
)

// Pinned to kubernetes-1.13.11
replace (
	k8s.io/api => k8s.io/api v0.0.0-20190817221950-ebce17126a01
	k8s.io/apiextensions-apiserver => k8s.io/apiextensions-apiserver v0.0.0-20190919022157-e8460a76b3ad
	k8s.io/apimachinery => k8s.io/apimachinery v0.0.0-20190817221809-bf4de9df677c
	k8s.io/client-go => k8s.io/client-go v0.0.0-20190817222206-ee6c071a42cf
)
