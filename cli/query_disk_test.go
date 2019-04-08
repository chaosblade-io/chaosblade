package main

import (
	"testing"

	"github.com/spf13/cobra"
	"bytes"
	"fmt"
	"github.com/chaosblade-io/chaosblade/transport"
	"encoding/json"
	"reflect"
)

func TestQueryDiskCommand_queryDiskInfo(t *testing.T) {
	command := &cobra.Command{
	}
	qdc := &QueryDiskCommand{
	}
	testQueryDiskDevice(t, command, qdc)

	testQueryDiskUnknownArg(t, command, qdc)
}

func testQueryDiskUnknownArg(t *testing.T, command *cobra.Command, qdc *QueryDiskCommand) {
	buffer := &bytes.Buffer{}
	command.SetOutput(buffer)

	arg := "unknown"
	expectedErr := fmt.Errorf("the %s argument not found", arg)

	err := qdc.queryDiskInfo(command, arg)
	if err.Error() != expectedErr.Error() {
		t.Errorf("unexpected result: %s, expected: %s", err, expectedErr)
	}
}

func testQueryDiskDevice(t *testing.T, command *cobra.Command, qdc *QueryDiskCommand) {
	buffer := &bytes.Buffer{}
	command.SetOutput(buffer)

	arg := "device"
	err := qdc.queryDiskInfo(command, arg)
	if err != nil {
		t.Errorf("unexpected result: %s, expected no error", err)
	}
	// check print value
	var response transport.Response
	err = json.Unmarshal(buffer.Bytes(), &response)
	if err != nil {
		t.Errorf("unexpected result: %s, expected no error", err)
	}
	if !response.Success {
		t.Errorf("unexpected result: %t, expected: %t", response.Success, true)
	}

	// check response result
	inters := response.Result.([]interface{})
	if len(inters) <= 0 {
		t.Errorf("unexpected result: %d, expected greater zero", len(inters))
	}
	if _, ok := inters[0].(string); !ok {
		t.Errorf("unexpected result type: %s, expected string", reflect.TypeOf(inters[0]))
	}
}
