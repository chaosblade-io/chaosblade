package transport

import (
	"encoding/json"
	"fmt"
)

const (
	OK                      = "OK"
	InvalidTimestamp        = "InvalidTimestamp"
	Forbidden               = "Forbidden"
	HandlerNotFound         = "HandlerNotFound"
	TokenNotFound           = "TokenNotFound"
	DataNotFound            = "DataNotFound"
	GetProcessError         = "GetProcessError"
	ServerError             = "ServerError"
	HandlerClosed           = "HandlerClosed"
	Timeout                 = "Timeout"
	Uninitialized           = "Uninitialized"
	EncodeError             = "EncodeError"
	DecodeError             = "DecodeError"
	FileNotFound            = "FileNotFound"
	DownloadError           = "DownloadError"
	DeployError             = "DeployError"
	ServiceSwitchError      = "ServiceSwitchError"
	DiskNotFound            = "DiskNotFound"
	DatabaseError           = "DatabaseError"
	EnvironmentError        = "EnvironmentError"
	NoWritePermission       = "NoWritePermission"
	ParameterEmpty          = "ParameterEmpty"
	ParameterTypeError      = "ParameterTypeError"
	IllegalParameters       = "IllegalParameters"
	IllegalCommand          = "IllegalCommand"
	ExecCommandError        = "ExecCommandError"
	DuplicateError          = "DuplicateError"
	FaultInjectCmdError     = "FaultInjectCmdError"
	FaultInjectExecuteError = "FaultInjectExecuteError"
	FaultInjectNotSupport   = "FaultInjectNotSupport"
	JavaAgentCmdError       = "JavaAgentCmdError"
	K8sInvokeError          = "K8sInvokeError"
	DockerInvokeError       = "DockerInvokeError"
	DestroyNotSupported     = "DestroyNotSupported"
	PreHandleError          = "PreHandleError"
	SandboxInvokeError      = "SandboxInvokeError"
)

type CodeType struct {
	Code int32
	Msg  string
}

var Code = map[string]CodeType{
	OK:                      {200, "success"},
	InvalidTimestamp:        {401, "invalid timestamp"},
	Forbidden:               {403, "forbidden"},
	HandlerNotFound:         {404, "request handler not found"},
	TokenNotFound:           {405, "access token not found"},
	DataNotFound:            {406, "data not found"},
	DestroyNotSupported:     {407, "destroy not supported"},
	GetProcessError:         {408, "get process error"},
	ServerError:             {500, "server error"},
	HandlerClosed:           {501, "handler closed"},
	PreHandleError:          {502, "pre handle error"},
	Timeout:                 {510, "timeout"},
	Uninitialized:           {511, "uninitialized"},
	EncodeError:             {512, "encode error"},
	DecodeError:             {513, "decode error"},
	FileNotFound:            {514, "file not found"},
	DownloadError:           {515, "download file error"},
	DeployError:             {516, "deploy file error"},
	ServiceSwitchError:      {517, "service switch error"},
	DiskNotFound:            {518, "disk not found"},
	DatabaseError:           {520, "execute db error"},
	EnvironmentError:        {521, "environment error"},
	NoWritePermission:       {522, "no write permission"},
	ParameterEmpty:          {600, "parameter is empty"},
	ParameterTypeError:      {601, "parameter type error"},
	IllegalParameters:       {602, "illegal parameters"},
	IllegalCommand:          {603, "illegal command"},
	ExecCommandError:        {604, "exec command error"},
	DuplicateError:          {605, "duplicate error"},
	FaultInjectCmdError:     {701, "cannot handle the faultInject cmd"},
	FaultInjectExecuteError: {702, "execute faultInject error"},
	FaultInjectNotSupport:   {703, "the inject type not support"},
	JavaAgentCmdError:       {704, "cannot handle the javaagent cmd"},
	K8sInvokeError:          {800, "invoke k8s server api error"},
	DockerInvokeError:       {801, "invoke docker command error"},
	SandboxInvokeError:      {802, "invoke sandbox error"},
}

type Response struct {
	Code    int32       `json:"code"`
	Success bool        `json:"success"`
	Err     string      `json:"error,omitempty"`
	Result  interface{} `json:"result,omitempty"`
}

func (response *Response) Error() string {
	return response.Print()
}

func (response *Response) Print() string {
	bytes, err := json.Marshal(response)
	if err != nil {
		return fmt.Sprintf("marshall response err, %s; code: %d", err.Error(), response.Code)
	}
	return string(bytes)
}

func Return(codeType CodeType) *Response {
	return &Response{Code: codeType.Code, Success: false, Err: codeType.Msg}
}

func ReturnFail(codeType CodeType, err string) *Response {
	return &Response{Code: codeType.Code, Success: false, Err: err}
}

func ReturnSuccess(result interface{}) *Response {
	return &Response{Code: Code[OK].Code, Success: true, Result: result}
}

//ToString
func (response *Response) ToString() string {
	bytes, err := json.MarshalIndent(response, "", "\t")
	if err != nil {
		return err.Error()
	}
	return fmt.Sprintln(string(bytes))
}
