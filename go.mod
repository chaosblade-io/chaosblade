module github.com/chaosblade-io/chaosblade

go 1.13

require (
	github.com/chaosblade-io/chaosblade-exec-docker v0.6.0
	github.com/chaosblade-io/chaosblade-exec-os v0.6.0
	github.com/chaosblade-io/chaosblade-operator v0.6.0
	github.com/chaosblade-io/chaosblade-spec-go v0.6.0
	github.com/gregjones/httpcache v0.0.0-20190611155906-901d90724c79 // indirect
	github.com/mattn/go-sqlite3 v1.10.1-0.20190217174029-ad30583d8387
	github.com/peterbourgon/diskv v2.0.1+incompatible // indirect
	github.com/prometheus/common v0.9.1
	github.com/shirou/gopsutil v2.19.9+incompatible
	github.com/sirupsen/logrus v1.5.0
	github.com/spf13/cobra v0.0.5
	github.com/spf13/pflag v1.0.5
	golang.org/x/crypto v0.0.0-20200128174031-69ecbb4d6d5d
	k8s.io/apimachinery v0.17.4
	k8s.io/client-go v12.0.0+incompatible
	sigs.k8s.io/controller-runtime v0.5.3
)

// Pinned to kubernetes-1.13.11
replace (
	k8s.io/api => k8s.io/api v0.0.0-20190817221950-ebce17126a01
	k8s.io/apiextensions-apiserver => k8s.io/apiextensions-apiserver v0.0.0-20190919022157-e8460a76b3ad
	k8s.io/apimachinery => k8s.io/apimachinery v0.0.0-20190817221809-bf4de9df677c
	k8s.io/client-go => k8s.io/client-go v0.0.0-20190817222206-ee6c071a42cf
)
