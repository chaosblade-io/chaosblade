package jvm

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"testing"
)

func TestExecutor_createUrl(t *testing.T) {
	jvm := NewExecutor()
	enhanceModel := &exec.ExpModel{}
	enhanceModel.Target = "jvm"
	enhanceModel.ActionName = "return"
	enhanceModel.ActionFlags = make(map[string]string)
	enhanceModel.ActionFlags["value"] = "hello world"
	geturl := jvm.createUrl("80", "006c9aa3cc26fe10", enhanceModel)
	expecturl := "http://127.0.0.1:80/sandbox/default/module/http/chaosblade/create?target=jvm&suid=006c9aa3cc26fe10&action=return&value=hello+world"
	if expecturl != geturl {
		t.Errorf("executor.createUrl failed\n"+
			"expected:%s\n"+
			"real:%s\n", expecturl, geturl)
	}

}
