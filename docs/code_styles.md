# Code Style

Code style is a set of rules or guidelines when writing source codes of a software project. Following particular code style will definitely help contributors to read and understand source codes very well. In addition, it will help to avoid introducing errors as well.

## Code Style Tools

Project chaosblade is written in Golang. And currently we use three tools to help conform code styles in this project. These three tools are:

* [gofmt](https://golang.org/cmd/gofmt)
* [go vet](https://golang.org/cmd/vet/)

And all these tools are used in [Makefile](../Makefile).

## Code Review Comments

When collaborating in chaosblade project, we follow the style from [Go Code Review Comments](https://github.com/golang/go/wiki/CodeReviewComments). Before contributing, we treat this as a must-read.

## Additional Style Rules

For a project, existing tools and rules may not be sufficient. To align more in styles, we recommend contributors taking a thorough look at the following additional style rules:

### RULE001 - Add blank line between field's comments

When constructing a struct, if comments needed for fields in struct, keep a blank line between fields. The encouraged way is as following:

``` golang
// correct example
// ContainerManager is the default implement of interface ContainerMgr.
type ContainerManager struct {
	// Store stores containers in Backend store.
	// Element operated in store must has a type of *ContainerMeta.
	// By default, Store will use local filesystem with json format to store containers.
	Store *meta.Store

	// Client is used to interact with containerd.
	Client ctrd.APIClient

	// NameToID stores relations between container's name and ID.
	// It is used to get container ID via container name.
	NameToID *collect.SafeMap
	......
}
```

Rather than:

```golang
// wrong example
// ContainerManager is the default implement of interface ContainerMgr.
type ContainerManager struct {
	// Store stores containers in Backend store.
	// Element operated in store must has a type of *ContainerMeta.
	// By default, Store will use local filesystem with json format to store containers.
	Store *meta.Store
	// Client is used to interact with containerd.
	Client ctrd.APIClient
	// NameToID stores relations between container's name and ID.
	// It is used to get container ID via container name.
	NameToID *collect.SafeMap
	......
}
```

### RULE002 - Add parameter name in interface definition

When defining interface functions, we should always explicitly add formal parameters, and this helps a lot to code readability. For example, the following way are preferred:

``` golang
// correct example
// ContainerMgr is an interface to define all operations against container.
type ContainerMgr interface {
	// Start a container.
	Start(ctx context.Context, id, detachKeys string) error

	// Stop a container.
	Stop(ctx context.Context, name string, timeout int64) error
	......
}
```

However, missing formal parameter's name would make interface unreadable, since we would never know what the parameter's real meaning unless turning to one implementation of this interface:

``` golang
// wrong example
type ContainerMgr interface {
	// Start a container.
	Start(context.Context, string, string) error

	// Stop a container.
	Stop(context.Context, string, int64) error
	......
}

```

In addition, a blank line between function's comments is encouraged to make interface more readable.

### RULE003 - Import Packages

When importing packages, to improve readabilities, we should import package by sequence:

* Golang's built-in system packages;
* project's own packages;
* third-party packages.

And we should keep a blank line among these three kinds of packages like the following:

``` golang
import (
	"fmt"
	"strings"
	"time"

	"github.com/chaosblade-io/chaosblade/data"
	"github.com/chaosblade-io/chaosblade/util"

	"github.com/sirupsen/logrus"
	"github.com/spf13/cobra"
)
```

### RULE004 - Variable declaration position

Variable object should be declared at the beginning of the go file following package name and importing.

### RULE005 - Generation of action failure

When generating error in one function execution failure, we should generally use the following way to append string "failed to do something" and the specific err instance to construct a new error:

``` golang
fmt.Errorf("failed to do something: %v", err)
```

When an err could be thrown out, please remember to add it in the error construction.

### RULE006 - Return fast to indent less

chaosblade encourages contributors to take advantages of `return fast` to simply source code and indent less. For example, the following codes are discouraged:

``` golang
// wrong example
if retry {
	if t, err := calculateSleepTime(d); err == nil {
		time.Sleep(t)
		times++
		return retryLoad()
	}
	return fmt.Errorf("failed to calculate timeout: %v", err)
}
return nil
```

In code above, there are some indents which can be avoided. The encouraged way is like the following:

``` golang
// correct example
if !retry {
	return nil
}

t, err := calculateSleepTime(d);
if err != nil {
	return fmt.Errorf("failed to calculate timeout: %v", err)
}

time.Sleep(t)
times++

return retryLoad()
```

### RULE007 - Lowercase log and error

No matter log or error, first letter of the message must be lower-case. So, `log.Debugf("failed to add list: %v", err)` is encouraged. And `log.Debugf("Failed to add list: %v", err)` is not perferred.

### RULE008 - Nested errors

When occurring nesting errors, we recommend first considering using package `github.com/pkg/errors`.

### RULE009 - Comment correctly

Every comment must begin with `//` plus a whitespace no matter for a variable, struct, function, code block and anything else. Please don't forget the whitespace, and end up all the sentence with a `.`. In addition, it is encouraged to use third person singular to polish the majority of function's comments. For example, the following way

```golang
// wrong example
// ExecContainer execute a process in container.
func (c *Client) ExecContainer(ctx context.Context, process *Process) error {
	......
}
```

could be polished to be `executes` rather than `execute`:

```golang
// correct example
// ExecContainer executes a process in container.
func (c *Client) ExecContainer(ctx context.Context, process *Process) error {
	......
}
```

### RULE010 - Always remember DRY

We should take `DRY(Don't Repeat Yourself)` into consideration when adding anything.

### RULE011 - Welcome to your addition

If you think much more practical code styles should be introduced in chaosblade. Please submit a pull request to make this better.


## Reference
[Pouch Code Style](https://github.com/alibaba/pouch/blob/master/docs/contributions/code_styles.md)
