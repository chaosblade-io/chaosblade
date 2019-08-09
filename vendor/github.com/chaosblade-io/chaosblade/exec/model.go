package exec

import (
	"gopkg.in/yaml.v2"
	"io/ioutil"
	"github.com/sirupsen/logrus"
	"io"
)

// ExpModelCommandSpec defines the command interface for the experimental plugin
type ExpModelCommandSpec interface {
	// FlagName returns the command FlagName
	Name() string

	// ShortDesc returns short description for the command
	ShortDesc() string

	// LongDesc returns full description for the command
	LongDesc() string

	// Example returns use case for the command
	Example() string

	// Actions returns the list of actions supported by the command
	Actions() []ExpActionCommandSpec

	// Flags returns the command flags
	Flags() []ExpFlagSpec

	PreExecutor() PreExecutor
}

// ExpActionCommandSpec defines the action command interface for the experimental plugin
type ExpActionCommandSpec interface {
	// FlagName returns the action FlagName
	Name() string

	// Aliases returns command alias names
	Aliases() []string

	// ShortDesc returns short description for the action
	ShortDesc() string

	// LongDesc returns full description for the action
	LongDesc() string

	// Matchers returns the list of matchers supported by the action
	Matchers() []ExpFlagSpec

	// Flags returns the list of flags supported by the action
	Flags() []ExpFlagSpec

	// Executor returns the action command executor
	Executor(channel Channel) Executor
}

type ExpFlagSpec interface {
	FlagName() string
	FlagDesc() string
	FlagNoArgs() bool
	FlagRequired() bool
}

// ExpFlag defines the action flag
type ExpFlag struct {
	// Name returns the flag FlagName
	Name string `yaml:"name"`

	// Desc returns the flag description
	Desc string `yaml:"desc"`

	// NoArgs means no arguments
	NoArgs bool `yaml:"noArgs"`

	// Required means necessary or not
	Required bool `yaml:"required"`
}

func (f *ExpFlag) FlagName() string {
	return f.Name
}

func (f *ExpFlag) FlagDesc() string {
	return f.Desc
}

func (f *ExpFlag) FlagNoArgs() bool {
	return f.NoArgs
}

func (f *ExpFlag) FlagRequired() bool {
	return f.Required
}

type ActionModel struct {
	ActionName      string    `yaml:"action"`
	ActionAliases   []string  `yaml:"aliases,flow,omitempty"`
	ActionShortDesc string    `yaml:"shortDesc"`
	ActionLongDesc  string    `yaml:"longDesc"`
	ActionMatchers  []ExpFlag `yaml:"matchers,omitempty"`
	ActionFlags     []ExpFlag `yaml:"flags,omitempty"`
	executor        Executor
}

func (am *ActionModel) Executor(Channel) Executor {
	return am.executor
}

func (am *ActionModel) Name() string {
	return am.ActionName
}

func (am *ActionModel) Aliases() []string {
	return am.ActionAliases
}

func (am *ActionModel) ShortDesc() string {
	return am.ActionShortDesc
}

func (am *ActionModel) LongDesc() string {
	return am.ActionLongDesc
}

func (am *ActionModel) Matchers() []ExpFlagSpec {
	flags := make([]ExpFlagSpec, 0)
	for idx := range am.ActionMatchers {
		flags = append(flags, &am.ActionMatchers[idx])
	}
	return flags
}

func (am *ActionModel) Flags() []ExpFlagSpec {
	flags := make([]ExpFlagSpec, 0)
	for idx := range am.ActionFlags {
		flags = append(flags, &am.ActionFlags[idx])
	}
	return flags
}

type ExpPrepareModel struct {
	PrepareType     string    `yaml:"type"`
	PrepareFlags    []ExpFlag `yaml:"flags"`
	PrepareRequired bool      `yaml:"required"`
}

type ExpCommandModel struct {
	ExpName         string          `yaml:"target"`
	ExpShortDesc    string          `yaml:"shortDesc"`
	ExpLongDesc     string          `yaml:"longDesc"`
	ExpExample      string          `yaml:"example"`
	ExpActions      []ActionModel   `yaml:"actions"`
	executor        Executor
	expFlags        []ExpFlag       `yaml:"flags,omitempty"`
	preExecutor     PreExecutor
	ExpScope        string          `yaml:"scope"`
	ExpPrepareModel ExpPrepareModel `yaml:"prepare,omitempty"`
	ExpSubTargets   []string        `yaml:"subTargets,flow,omitempty"`
}

func (ecm *ExpCommandModel) Name() string {
	return ecm.ExpName
}

func (ecm *ExpCommandModel) ShortDesc() string {
	return ecm.ExpShortDesc
}

func (ecm *ExpCommandModel) LongDesc() string {
	return ecm.ExpLongDesc
}

func (ecm *ExpCommandModel) Example() string {
	return ecm.ExpExample
}

func (ecm *ExpCommandModel) Actions() []ExpActionCommandSpec {
	specs := make([]ExpActionCommandSpec, 0)
	for idx := range ecm.ExpActions {
		ecm.ExpActions[idx].executor = ecm.executor
		specs = append(specs, &ecm.ExpActions[idx])
	}
	return specs
}

func (ecm *ExpCommandModel) Flags() []ExpFlagSpec {
	flags := make([]ExpFlagSpec, 0)
	for idx := range ecm.expFlags {
		flags = append(flags, &ecm.expFlags[idx])
	}
	return flags
}

func (ecm *ExpCommandModel) PreExecutor() PreExecutor {
	return ecm.preExecutor
}

type Models struct {
	Version string            `yaml:"version"`
	Kind    string            `yaml:"kind"`
	Models  []ExpCommandModel `yaml:"items"`
}

func ParseSpecsToModel(file string, executor Executor) (*Models, error) {
	bytes, err := ioutil.ReadFile(file)
	if err != nil {
		return nil, err
	}
	models := &Models{}
	err = yaml.Unmarshal(bytes, models)
	if err != nil {
		return nil, err
	}
	for idx := range models.Models {
		models.Models[idx].executor = executor
	}
	return models, nil
}

func MarshalModelSpec(models *Models, writer io.Writer) {
	bytes, err := yaml.Marshal(models)
	if err != nil {
		logrus.Fatalf(err.Error())
	}
	writer.Write(bytes)
}
