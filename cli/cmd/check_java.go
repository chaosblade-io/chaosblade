/*
 * Copyright 1999-2020 Alibaba Group Holding Ltd.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package cmd

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/spf13/cobra"
)

const (
	minJdkVersion    = "1.6"
	cmdJavaHome      = "echo $JAVA_HOME"
	cmdJavaVersion   = "java -version"
	javaToolsSubPath = "/lib/tools.jar"
)

var javaHome string

type CheckJavaCommand struct {
	command *cobra.Command
	object  string
}

func (djc *CheckJavaCommand) CobraCmd() *cobra.Command {
	return djc.command
}

func (djc *CheckJavaCommand) Name() string {
	return ""
}

func (djc *CheckJavaCommand) Init() {
	djc.command = &cobra.Command{
		Use:   "java",
		Short: "Check the environment of java for chaosblade",
		Long:  "Check the environment of java for chaosblade",
		RunE: func(cmd *cobra.Command, args []string) error {
			return djc.checkJavaRunE()
		},
		Example: djc.detectExample(),
	}

	djc.command.Flags().StringVar(&djc.object, "object", "jdk,tools", "the object of java need to be checked")
}

func (djc *CheckJavaCommand) detectExample() string {
	return "check os"
}

func (djc *CheckJavaCommand) checkJavaRunE() error {
	objects := strings.Split(djc.object, ",")
	if len(objects) == 0 || djc.object == "" {
		return spec.ResponseFailWithFlags(spec.CommandIllegal, "less object parameter")
	}

	for _, object := range objects {
		object = strings.TrimSpace(object)
		if object == "" {
			continue
		}
		switch object {
		case "jdk":
			err := djc.checkJdk()
			if err != nil {
				fmt.Printf("[failed] %s \n", err.Error())
			} else {
				fmt.Print("[success] check jdk version success! \n")
			}
		case "tools":
			err := djc.checkTools()
			if err != nil {
				fmt.Printf("[failed] %s \n", err.Error())
			} else {
				fmt.Printf("[success] check tools.jar success! \n")
			}
		default:
			fmt.Printf("[failed] object parameter is wrong, object : %s", object)
		}

	}
	return nil
}

// check jdk
func (djc *CheckJavaCommand) checkJdk() error {
	// 1. check jdk by javaHome
	if javaHome != "" {
		jdkVersion, err := djc.getJdkVersionFromJdkHome()
		if err != nil {
			return err
		}

		if ok, err := djc.checkJdkVersion(jdkVersion); !ok {
			return errors.New("check jdk version failed. err: " + err.Error())
		}
		return nil
	}

	// 2. check jdk by $JAVA_HOME
	var jdkVersion string
	response := channel.NewLocalChannel().Run(context.Background(), "", cmdJavaHome)
	if response.Success {
		javaResult := response.Result.(string)
		javaHome = strings.Trim(javaResult, "\n")
		jdkVersion, err := djc.getJdkVersionFromJdkHome()
		if err != nil {
			return errors.New(fmt.Sprintf("check java jdk version failed! err: %s", err.Error()))
		}

		ok, err := djc.checkJdkVersion(jdkVersion)
		if !ok || err != nil {
			return errors.New(fmt.Sprintf("check java jdk version failed! err: %s", err.Error()))
		}
		return nil
	}

	// 3. check jdk by `java -version`
	response = channel.NewLocalChannel().Run(context.Background(), "", cmdJavaVersion)
	if !response.Success {
		return errors.New(fmt.Sprintf("check java jdk version failed! err: %s", response.Err))
	}
	javaResult := response.Result.(string)

	jdkVersion, err := djc.getJdkVersionFromJavaVer(string(javaResult))
	if err != nil {
		return errors.New(fmt.Sprintf("check java jdk version failed! err: %s", err.Error()))
	}
	ok, err := djc.checkJdkVersion(jdkVersion)
	if !ok || err != nil {
		return errors.New(fmt.Sprintf("check java jdk version failed! err: %s", err.Error()))
	}
	return nil
}

// check java tools
func (djc *CheckJavaCommand) checkTools() error {
	// 1. get java tools.jar path
	var javaToolsPrePath string
	if javaHome != "" {
		javaToolsPrePath = javaHome
	} else {
		response := channel.NewLocalChannel().Run(context.Background(), "", cmdJavaHome)
		if !response.Success {
			return errors.New("check java tools.jar failed, $JAVA_HOME is nil")
		}
		javaToolsPrePath = response.Result.(string)
	}

	// check the path of tools.jar is exists or not
	if util.IsExist(javaToolsPrePath + javaToolsSubPath) {
		return nil
	}
	return errors.New("check java tools.jar failed, file: $JAVA_HOME/lib/tools.jar not exists")
}

// check jdk version. if current jdk version less than 1.6, return false, else return true
func (djc *CheckJavaCommand) checkJdkVersion(currentVersion string) (bool, error) {
	// 1. split jdk version, eg: 1.8.0_151
	currentVersions := strings.Split(currentVersion, ".")
	minVersions := strings.Split(minJdkVersion, ".")
	if len(currentVersions) < 2 {
		return false, errors.New("jdk version error, current jdk version: " + currentVersion)
	}

	// 2. check current jdk
	currFirst, _ := strconv.Atoi(currentVersions[0])
	currSecond, _ := strconv.Atoi(currentVersions[1])
	minFirst, _ := strconv.Atoi(minVersions[0])
	minSecond, _ := strconv.Atoi(minVersions[1])
	if ok := (currFirst*100 + currSecond) >= (minFirst*100 + minSecond); ok {
		return ok, nil
	}

	return false, errors.New("jdk version less than 1.6, current jdk version: " + currentVersion)
}

// get jdk version from $JAVA_HOME eg: /Library/Java/JavaVirtualMachines/jdk1.8.0_151.jdk/Contents/Home
func (djc *CheckJavaCommand) getJdkVersionFromJdkHome() (string, error) {
	// 1. check $JAVA_HOME
	if !strings.HasPrefix(javaHome, "/") {
		return "", errors.New("get jdk version failed, JavaHome is error, JavaHome : " + javaHome)
	}

	// 2. split $JAVA_HOME by `/`
	javaHomeArr := strings.Split(javaHome, "/")
	if len(javaHomeArr) == 0 {
		return "", errors.New("get jdk version failed, JavaHome is error, JavaHome : " + javaHome)
	}

	// 3. check substr have java version or not by regexp, get java verison
	reg, err := regexp.Compile(`jdk[\d+\_?\d+\.?]+[jdk]??`)
	if err != nil {
		return "", errors.New("get jdk version failed, regxp is wrong, err : " + err.Error())
	}
	for _, javaHomeOne := range javaHomeArr {
		if !reg.MatchString(javaHomeOne) {
			continue
		}

		javaVersion := javaHomeOne[len("jdk"):]
		if javaVersion != "" {
			return javaVersion, nil
		}
	}

	return "", errors.New("get jdk version failed, JavaHome : " + javaHome)
}

// get jdk version from `java version` eg: java version "1.8.0_261"
//										   Java(TM) SE Runtime Environment (build 1.8.0_261-b12)
//										   Java HotSpot(TM) 64-Bit Server VM (build 25.261-b12, mixed mode)
func (djc *CheckJavaCommand) getJdkVersionFromJavaVer(jdkVer string) (string, error) {
	// 1. check `java version`
	if jdkVer == "" {
		return "", errors.New("get jdk version failed, jdkVer is error, jdkVer : " + jdkVer)
	}

	// 2. split `java version` by `\n`
	jdkVerArr := strings.Split(jdkVer, "\n")
	if len(jdkVerArr) < 1 {
		return "", errors.New("get jdk version failed, jdkVer is error, jdkVer : " + jdkVer)
	}

	// 3. get java version
	if ok := strings.Contains(jdkVerArr[0], "java version \""); !ok {
		return "", errors.New("get jdk version failed, jdkVer is error, jdkVer : " + jdkVer)
	}
	jdkVersion := jdkVerArr[0][len("java version \"") : len(jdkVerArr[0])-1]
	if jdkVersion == "" {
		return "", errors.New("get jdk version failed, jdkVer : " + jdkVer)
	}
	return jdkVersion, nil
}
